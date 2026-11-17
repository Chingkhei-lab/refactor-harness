"""Patch-log persistence (checkpoint 8, Tasks 2–3): blobs, workspaces, and
the compare-and-swap append.

The log is the durable workspace: a content-addressed base snapshot plus
an ordered log of patches (ADR 003 D3). Any worker rebuilds the tree by
fetching the base blob and applying the patches in seq order; the state
hash chain (parent_state_hash -> new_state_hash) is what makes a wrong or
forged order detectable instead of silent.

The append is a CAS on the head, not a separate lock (the checkpoint
review's addition): `UPDATE workspaces SET head_seq = head_seq + 1,
head_hash = :new WHERE id = :ws AND head_hash = :parent RETURNING
head_seq`. Zero rows = stale or conflicting append = the caller's whole
transaction rolls back. This serializes appends without adding a new
edge to the lock order — the row lock is taken inside the step's advance
transaction, after the job, step and workflow locks (recorded in ADR 002).

Idempotent redelivery is keyed on STEP IDENTITY (`run_ref` = workflow id +
step position, partial UNIQUE index): content keys cannot distinguish a
redelivered step from a different step producing the same A->B patch after
an A->B->A revert, because state hashes are content hashes.

Everything here runs on the CALLER's connection and never commits: the
append must be inside the step's advance transaction, which is enforced —
calling it outside a transaction raises, because two statements (CAS +
insert) committing separately could leave a head advanced with no patch
row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

import psycopg

from .db import Row
from .paths import PathDenied, validate_path
from .workspace import (
    PATCH_MAX_BYTES,
    SNAPSHOT_MAX_BYTES,
    PatchError,
    WorkspaceTree,
    decode_patch_json,
    sha256_hex,
)

_SQL_DIR = Path(__file__).parent / "sql"


def _load_sql(name: str) -> str:
    return (_SQL_DIR / name).read_text(encoding="utf-8")


_BLOB_INSERT_SQL = _load_sql("blob_insert.sql")
_BLOB_GET_SQL = _load_sql("blob_get.sql")
_WORKSPACE_INSERT_SQL = _load_sql("workspace_insert.sql")
_WORKSPACE_GET_SQL = _load_sql("workspace_get.sql")
_PATCH_CAS_SQL = _load_sql("patch_cas.sql")
_PATCH_INSERT_SQL = _load_sql("patch_insert.sql")
_PATCH_AT_SEQ_SQL = _load_sql("patch_at_seq.sql")
_PATCH_BY_RUN_REF_SQL = _load_sql("patch_by_run_ref.sql")
_PATCH_LOG_LOAD_SQL = _load_sql("patch_log_load.sql")


class PatchLogError(Exception):
    """Patch-log failures that are run outcomes, not crashes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BlobCorrupt(PatchLogError):
    """A stored blob's bytes no longer hash to its key."""

    def __init__(self, blob_hash: str) -> None:
        super().__init__("internal_error", f"blob {blob_hash} failed hash verification")


class BlobMissing(PatchLogError):
    def __init__(self, blob_hash: str) -> None:
        super().__init__("internal_error", f"blob {blob_hash} is missing")


class StaleAppend(PatchLogError):
    """The CAS matched zero rows: the head moved on (a newer attempt or a
    concurrent append won). The caller's transaction rolls back."""

    def __init__(self, message: str) -> None:
        super().__init__("stale_append", message)


class PatchConflict(PatchLogError):
    """The seq this append targets is taken by a DIFFERENT patch."""

    def __init__(self, message: str) -> None:
        super().__init__("patch_conflict", message)


@dataclass(frozen=True)
class AppendPatchResult:
    """`applied` on a fresh append, `already_applied` on idempotent
    redelivery (same parent + same patch bytes already in the log)."""

    status: str
    seq: int | None
    head_seq: int
    head_hash: str


@dataclass(frozen=True)
class PatchRecord:
    workspace_id: int
    seq: int
    parent_state_hash: str
    new_state_hash: str
    patch_blob_hash: str
    run_ref: str
    created_at: datetime


def _in_transaction(conn: psycopg.Connection[Row]) -> bool:
    # psycopg3 exposes the libpq status as conn.info.transaction_status;
    # a connection-like test double without .info gets the caller's contract
    info = getattr(conn, "info", None)
    if info is None or not hasattr(info, "transaction_status"):
        return True
    from psycopg.pq import TransactionStatus

    return bool(info.transaction_status != TransactionStatus.IDLE)


def store_blob(conn: psycopg.Connection[Row], data: bytes, *, cap_bytes: int) -> str:
    """Insert a content-addressed blob; duplicate upload is a no-op."""
    if len(data) > cap_bytes:
        raise PatchError("too_large", f"blob exceeds {cap_bytes} bytes")
    blob_hash = sha256_hex(data)
    conn.execute(
        _BLOB_INSERT_SQL,
        {"hash": blob_hash, "data": psycopg.Binary(data), "size": len(data)},
    )
    return blob_hash


def load_blob(conn: psycopg.Connection[Row], blob_hash: str) -> bytes:
    """Fetch a blob and RE-VERIFY its hash on read (Task 2)."""
    row = conn.execute(_BLOB_GET_SQL, {"hash": blob_hash}).fetchone()
    if row is None:
        raise BlobMissing(blob_hash)
    data: bytes = row["data"]
    if sha256_hex(data) != blob_hash:
        raise BlobCorrupt(blob_hash)
    return data


def create_workspace(conn: psycopg.Connection[Row], files: dict[str, bytes]) -> int:
    """Store the base snapshot and open the patch log at seq 0.

    The snapshot goes through WorkspaceTree, which enforces the path rules
    and the per-file caps on everything that enters a workspace.
    """
    for path in files:
        try:
            validate_path(path)
        except PathDenied as exc:
            raise PatchError("path_denied", str(exc)) from exc
    tree = WorkspaceTree(files)
    try:
        tar = tree.snapshot_tar()
    except PatchError as exc:
        raise PatchLogError(exc.code, exc.args[0]) from exc
    base_hash = store_blob(conn, tar, cap_bytes=SNAPSHOT_MAX_BYTES)
    row = conn.execute(
        _WORKSPACE_INSERT_SQL,
        {"base_snapshot_hash": base_hash, "head_hash": tree.state_hash()},
    ).fetchone()
    if row is None:
        raise PatchLogError("internal_error", "workspace insert returned no row")
    return int(row["id"])


def append_patch(
    conn: psycopg.Connection[Row],
    *,
    workspace_id: int,
    parent_state_hash: str,
    new_state_hash: str,
    patch_bytes: bytes,
    run_ref: str,
) -> AppendPatchResult:
    """Append one patch inside the caller's transaction: CAS on the head.

    Redelivery is keyed on STEP IDENTITY — `run_ref` MUST carry the workflow
    id and step position (e.g. "wf:<uuid>/step:<pos>"), and a partial UNIQUE
    index allows one recorded patch per (workspace, run_ref). Content keys
    cannot do this job: state hashes are content hashes, so a workspace can
    legitimately return to an earlier state (A->B, revert B->A) and a
    DIFFERENT step can then produce the very same A->B patch bytes. A
    (parent hash, patch bytes) lookup would misreport that genuine new
    append as redelivery and silently drop it; a step identity cannot.

    Outcomes, in the order they are checked:
    - a row for this run_ref already exists and carries the SAME parent
      hash and patch blob -> idempotent success (`already_applied`): the
      step's advance transaction committed once, and this is the same step
      re-executing. Writes nothing;
    - a row for this run_ref exists with DIFFERENT content -> PatchConflict:
      the same step cannot produce two different patches;
    - the head no longer matches `parent_state_hash` -> StaleAppend: a
      newer attempt or a concurrent append won; the caller rolls back;
    - the CAS wins -> the patch row is inserted at the returned seq in the
      same transaction (the UNIQUE index is the backstop: two rows for one
      step identity cannot commit);
    - a row already occupies the target seq with a different patch ->
      PatchConflict (defensive: unreachable while appends are atomic, so
      it also guards against external tampering with the log).

    Enforces the advance-transaction contract: refuses to run outside a
    transaction, because the CAS and the insert must commit together.
    """
    if not _in_transaction(conn):
        raise PatchLogError(
            "internal_error",
            "append_patch must run inside the step's advance transaction "
            "(open one with conn.transaction()); the CAS and the patch "
            "insert must commit together",
        )
    if len(patch_bytes) > PATCH_MAX_BYTES:
        raise PatchLogError("too_large", f"patch exceeds {PATCH_MAX_BYTES} bytes")
    patch_blob_hash = store_blob(conn, patch_bytes, cap_bytes=PATCH_MAX_BYTES)

    head = conn.execute(_WORKSPACE_GET_SQL, {"workspace_id": workspace_id}).fetchone()
    if head is None:
        raise PatchLogError("unknown_workspace", f"workspace {workspace_id} not found")

    if run_ref != "":
        recorded = conn.execute(
            _PATCH_BY_RUN_REF_SQL,
            {"workspace_id": workspace_id, "run_ref": run_ref},
        ).fetchone()
        if recorded is not None:
            same_patch = (
                str(recorded["parent_state_hash"]) == parent_state_hash
                and str(recorded["patch_blob_hash"]) == patch_blob_hash
            )
            if same_patch:
                return AppendPatchResult(
                    status="already_applied",
                    seq=int(recorded["seq"]),
                    head_seq=int(head["head_seq"]),
                    head_hash=str(head["head_hash"]),
                )
            raise PatchConflict(
                f"run_ref {run_ref!r} already recorded a DIFFERENT patch at "
                f"seq {int(recorded['seq'])} (parent "
                f"{recorded['parent_state_hash']}, blob "
                f"{recorded['patch_blob_hash']}): the same step cannot "
                "append twice with different content"
            )

    taken = conn.execute(
        _PATCH_AT_SEQ_SQL,
        {"workspace_id": workspace_id, "seq": int(head["head_seq"]) + 1},
    ).fetchone()
    if taken is not None:
        raise PatchConflict(
            f"seq {int(head['head_seq']) + 1} is occupied by patch "
            f"{taken['patch_blob_hash']!r}, a different patch"
        )

    if str(head["head_hash"]) != parent_state_hash:
        raise StaleAppend(
            f"parent_state_hash {parent_state_hash} != head "
            f"{head['head_hash']} (seq {int(head['head_seq'])}): "
            "a newer attempt or concurrent append advanced the log"
        )

    cas = conn.execute(
        _PATCH_CAS_SQL,
        {
            "workspace_id": workspace_id,
            "parent_state_hash": parent_state_hash,
            "new_state_hash": new_state_hash,
        },
    ).fetchone()
    if cas is None:
        # the head moved between the read above and the CAS: lost the race
        raise StaleAppend(
            f"head moved between read and CAS: parent {parent_state_hash} no longer matches"
        )
    seq = int(cas["head_seq"])
    conn.execute(
        _PATCH_INSERT_SQL,
        {
            "workspace_id": workspace_id,
            "seq": seq,
            "parent_state_hash": parent_state_hash,
            "new_state_hash": new_state_hash,
            "patch_blob_hash": patch_blob_hash,
            "run_ref": run_ref,
        },
    )
    return AppendPatchResult(status="applied", seq=seq, head_seq=seq, head_hash=new_state_hash)


def load_workspace_head(conn: psycopg.Connection[Row], workspace_id: int) -> tuple[int, str]:
    row = conn.execute(_WORKSPACE_GET_SQL, {"workspace_id": workspace_id}).fetchone()
    if row is None:
        raise PatchLogError("unknown_workspace", f"workspace {workspace_id} not found")
    return int(row["head_seq"]), str(row["head_hash"])


def rebuild_workspace(conn: psycopg.Connection[Row], workspace_id: int) -> WorkspaceTree:
    """Base snapshot + ordered log -> the current tree (the round trip).

    Verifies the hash chain as it goes: each patch must carry the hash of
    the tree it was computed against, and the last patch's new_state_hash
    must equal the workspaces head hash. Any mismatch is corruption, not
    a silently-rebuilt tree.
    """
    row = conn.execute(_WORKSPACE_GET_SQL, {"workspace_id": workspace_id}).fetchone()
    if row is None:
        raise PatchLogError("unknown_workspace", f"workspace {workspace_id} not found")
    base_tar = load_blob(conn, str(row["base_snapshot_hash"]))
    try:
        tree = WorkspaceTree.from_snapshot(base_tar)
    except PatchError as exc:
        raise PatchLogError(exc.code, exc.args[0]) from exc
    if int(row["head_seq"]) == 0:
        if tree.state_hash() != str(row["head_hash"]):
            raise BlobCorrupt(str(row["base_snapshot_hash"]))
        return tree
    patches = [
        _to_patch_record(r)
        for r in conn.execute(_PATCH_LOG_LOAD_SQL, {"workspace_id": workspace_id}).fetchall()
    ]
    for record in patches:
        blob = load_blob(conn, record.patch_blob_hash)
        parent, new, edits = decode_patch_json(blob)
        if parent != record.parent_state_hash or new != record.new_state_hash:
            raise BlobCorrupt(record.patch_blob_hash)
        if tree.state_hash() != record.parent_state_hash:
            raise BlobCorrupt(record.patch_blob_hash)
        tree.apply_edits(edits)
        if tree.state_hash() != record.new_state_hash:
            raise BlobCorrupt(record.patch_blob_hash)
    if tree.state_hash() != str(row["head_hash"]):
        raise BlobCorrupt(f"workspace {workspace_id} head")
    return tree


def _to_patch_record(row: Row) -> PatchRecord:
    return PatchRecord(
        workspace_id=int(row["workspace_id"]),
        seq=int(row["seq"]),
        parent_state_hash=str(row["parent_state_hash"]),
        new_state_hash=str(row["new_state_hash"]),
        patch_blob_hash=str(row["patch_blob_hash"]),
        run_ref=str(row["run_ref"]),
        created_at=row["created_at"],
    )


__all__ = [
    "AppendPatchResult",
    "BlobCorrupt",
    "BlobMissing",
    "PatchConflict",
    "PatchLogError",
    "PatchRecord",
    "StaleAppend",
    "append_patch",
    "create_workspace",
    "load_blob",
    "load_workspace_head",
    "rebuild_workspace",
    "store_blob",
    "UUID",
]
