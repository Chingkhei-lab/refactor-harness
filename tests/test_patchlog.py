"""Patch-log persistence (Tasks 2–3): blobs, the CAS append, idempotent
redelivery, stale-worker rollback, and the base+log rebuild round trip."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from engine import db, jobs, patchlog
from engine.workspace import (
    PatchEdit,
    WorkspaceTree,
    canonical_patch_json,
    sha256_hex,
)


@pytest.fixture
def conn(clean_db) -> Iterator[psycopg.Connection[db.Row]]:
    with db.connect(clean_db, application_name="test-patchlog") as connection:
        yield connection


def _tree_with(*files: str) -> WorkspaceTree:
    return WorkspaceTree({f"f{i}.txt": content.encode("utf-8") for i, content in enumerate(files)})


# ---------------------------------------------------------------------------
# blobs: content-addressing, ON CONFLICT no-op, hash re-verification
# ---------------------------------------------------------------------------


def test_store_blob_duplicate_is_noop(conn) -> None:
    h1 = patchlog.store_blob(conn, b"same bytes", cap_bytes=1024)
    h2 = patchlog.store_blob(conn, b"same bytes", cap_bytes=1024)
    assert h1 == h2 == sha256_hex(b"same bytes")
    count = conn.execute("SELECT count(*) AS n FROM blobs").fetchone()
    assert int(count["n"]) == 1  # one row, not two


def test_store_blob_cap(conn) -> None:
    with pytest.raises(Exception, match="exceeds"):
        patchlog.store_blob(conn, b"x" * 2049, cap_bytes=2048)


def test_load_blob_reverifies_hash(conn) -> None:
    h = patchlog.store_blob(conn, b"payload", cap_bytes=1024)
    # tamper: the row's bytes no longer hash to the key
    conn.execute("UPDATE blobs SET data = %s WHERE hash = %s", (psycopg.Binary(b"tampered"), h))
    with pytest.raises(patchlog.BlobCorrupt, match="failed hash verification"):
        patchlog.load_blob(conn, h)


def test_load_blob_missing(conn) -> None:
    with pytest.raises(patchlog.BlobMissing):
        patchlog.load_blob(conn, "0" * 64)


# ---------------------------------------------------------------------------
# workspaces + the CAS append
# ---------------------------------------------------------------------------


def test_create_workspace(conn) -> None:
    tree = _tree_with("hello\n")
    ws_id = patchlog.create_workspace(conn, tree.files)
    head_seq, head_hash = patchlog.load_workspace_head(conn, ws_id)
    assert head_seq == 0
    assert head_hash == tree.state_hash()
    row = conn.execute(
        "SELECT base_snapshot_hash FROM workspaces WHERE id = %s", (ws_id,)
    ).fetchone()
    assert row["base_snapshot_hash"] == sha256_hex(tree.snapshot_tar())


def test_create_workspace_validates_paths(conn) -> None:
    with pytest.raises(Exception, match="absolute"):
        patchlog.create_workspace(conn, {"/abs": b"x"})


def test_append_and_rebuild_round_trip_on_a_second_connection(clean_db, conn) -> None:
    files = {"src/a.py": "print(1)\n", "b.txt": "hello\n"}
    tree = WorkspaceTree({k: v.encode("utf-8") for k, v in files.items()})
    ws_id = patchlog.create_workspace(conn, tree.files)
    parent = tree.state_hash()

    # patch 1: create b2.txt
    edits1 = [PatchEdit(path="b2.txt", expected_file_hash=None, new_string="created\n")]
    plan1 = tree.apply_edits(edits1)
    blob1 = canonical_patch_json(parent, plan1.new_state_hash, edits1)
    with conn.transaction():
        r1 = patchlog.append_patch(
            conn,
            workspace_id=ws_id,
            parent_state_hash=parent,
            new_state_hash=plan1.new_state_hash,
            patch_bytes=blob1,
            run_ref="wf-1/step-1",
        )
    assert r1.status == "applied"
    assert r1.seq == 1

    # patch 2: edit the ORIGINAL file (chained on patch 1's new hash)
    edits2 = [
        PatchEdit(
            path="b.txt",
            expected_file_hash=sha256_hex(b"hello\n"),
            old_string="hello",
            new_string="goodbye",
        )
    ]
    plan2 = tree.apply_edits(edits2)
    blob2 = canonical_patch_json(plan1.new_state_hash, plan2.new_state_hash, edits2)
    with conn.transaction():
        r2 = patchlog.append_patch(
            conn,
            workspace_id=ws_id,
            parent_state_hash=plan1.new_state_hash,
            new_state_hash=plan2.new_state_hash,
            patch_bytes=blob2,
            run_ref="wf-1/step-2",
        )
    assert r2.status == "applied"
    assert r2.seq == 2

    head_seq, head_hash = patchlog.load_workspace_head(conn, ws_id)
    assert (head_seq, head_hash) == (2, plan2.new_state_hash)

    # THE round trip: a SECOND connection rebuilds the same tree from base
    # snapshot plus the ordered log, byte-for-byte and hash-for-hash.
    with db.connect(clean_db, application_name="test-rebuild") as conn2:
        rebuilt = patchlog.rebuild_workspace(conn2, ws_id)
    assert rebuilt.files == tree.files
    assert rebuilt.state_hash() == tree.state_hash()


def test_duplicate_append_is_idempotent_success(conn) -> None:
    files = {"a.txt": "x\n"}
    tree = WorkspaceTree({k: v.encode("utf-8") for k, v in files.items()})
    ws_id = patchlog.create_workspace(conn, tree.files)
    edits = [PatchEdit(path="new.txt", expected_file_hash=None, new_string="n\n")]
    plan = tree.apply_edits(edits)
    base_hash = _base_hash(ws_id, conn)
    patch_bytes = canonical_patch_json(base_hash, plan.new_state_hash, edits)
    with conn.transaction():
        r1 = patchlog.append_patch(
            conn,
            workspace_id=ws_id,
            parent_state_hash=base_hash,
            new_state_hash=plan.new_state_hash,
            patch_bytes=patch_bytes,
            run_ref="wf/step",
        )
    assert r1.status == "applied"
    # the retried step re-appends its IDENTICAL patch: success, no second row
    with conn.transaction():
        r2 = patchlog.append_patch(
            conn,
            workspace_id=ws_id,
            parent_state_hash=base_hash,
            new_state_hash=plan.new_state_hash,
            patch_bytes=patch_bytes,
            run_ref="wf/step",
        )
    assert r2.status == "already_applied"
    assert r2.seq == r1.seq == 1
    count = conn.execute(
        "SELECT count(*) AS n FROM workspace_patches WHERE workspace_id = %s", (ws_id,)
    ).fetchone()
    assert int(count["n"]) == 1
    head_seq, _ = patchlog.load_workspace_head(conn, ws_id)
    assert head_seq == 1


def _base_hash(ws_id: int, conn: psycopg.Connection[db.Row]) -> str:
    row = conn.execute("SELECT head_hash FROM workspaces WHERE id = %s", (ws_id,)).fetchone()
    return str(row["head_hash"])


def test_stale_append_rolls_back_the_whole_transaction(conn) -> None:
    files = {"a.txt": "v1\n"}
    tree = WorkspaceTree({k: v.encode("utf-8") for k, v in files.items()})
    ws_id = patchlog.create_workspace(conn, tree.files)
    base_hash = _base_hash(ws_id, conn)

    # a newer attempt already advanced the log
    edits1 = [
        PatchEdit(
            path="a.txt", expected_file_hash=sha256_hex(b"v1\n"), old_string="v1", new_string="v2"
        )
    ]
    plan1 = tree.apply_edits(edits1)
    with conn.transaction():
        patchlog.append_patch(
            conn,
            workspace_id=ws_id,
            parent_state_hash=base_hash,
            new_state_hash=plan1.new_state_hash,
            patch_bytes=canonical_patch_json(base_hash, plan1.new_state_hash, edits1),
            run_ref="wf/step-2",
        )

    # the STALE worker (attempt 1, redelivered late) still holds the base hash
    stale_edits = [
        PatchEdit(
            path="a.txt",
            expected_file_hash=sha256_hex(b"v1\n"),
            old_string="v1",
            new_string="STALE",
        )
    ]
    stale_bytes = canonical_patch_json(base_hash, "d" * 64, stale_edits)
    stale_blob_hash = sha256_hex(stale_bytes)
    with pytest.raises(patchlog.StaleAppend, match="parent_state_hash"):
        with conn.transaction():
            patchlog.append_patch(
                conn,
                workspace_id=ws_id,
                parent_state_hash=base_hash,
                new_state_hash="d" * 64,
                patch_bytes=stale_bytes,
                run_ref="wf/step-1-stale",
            )
    # the exception rolled back EVERYTHING the append had done
    assert conn.info.transaction_status == TransactionStatus.IDLE
    head_seq, head_hash = patchlog.load_workspace_head(conn, ws_id)
    assert (head_seq, head_hash) == (1, plan1.new_state_hash)
    count = conn.execute(
        "SELECT count(*) AS n FROM workspace_patches WHERE workspace_id = %s AND seq = 2", (ws_id,)
    ).fetchone()
    assert int(count["n"]) == 0
    # the blob it stored before the CAS is rolled back too — no orphans
    blob_count = conn.execute(
        "SELECT count(*) AS n FROM blobs WHERE hash = %s", (stale_blob_hash,)
    ).fetchone()
    assert int(blob_count["n"]) == 0


def test_append_refuses_outside_a_transaction(conn) -> None:
    files = {"a.txt": "x\n"}
    tree = WorkspaceTree({k: v.encode("utf-8") for k, v in files.items()})
    ws_id = patchlog.create_workspace(conn, tree.files)
    base_hash = _base_hash(ws_id, conn)
    # autocommit connection with NO transaction open: the CAS and the insert
    # could commit separately, so this is a hard contract error
    with pytest.raises(Exception, match="advance transaction"):
        patchlog.append_patch(
            conn,
            workspace_id=ws_id,
            parent_state_hash=base_hash,
            new_state_hash="e" * 64,
            patch_bytes=canonical_patch_json(
                base_hash,
                "e" * 64,
                [PatchEdit(path="n.txt", expected_file_hash=None, new_string="n")],
            ),
            run_ref="wf/step",
        )


def test_conflicting_patch_at_seq_is_an_error(conn) -> None:
    files = {"a.txt": "x\n"}
    tree = WorkspaceTree({k: v.encode("utf-8") for k, v in files.items()})
    ws_id = patchlog.create_workspace(conn, tree.files)
    base_hash = _base_hash(ws_id, conn)
    # CONSTRUCTED tamper: a foreign row already occupies seq 1 (bypassing the
    # CAS — an external writer), so the next legit append collides
    other_blob = patchlog.store_blob(conn, b"foreign patch bytes", cap_bytes=1024)
    conn.execute(
        "INSERT INTO workspace_patches"
        " (workspace_id, seq, parent_state_hash, new_state_hash, patch_blob_hash, run_ref)"
        " VALUES (%s, 1, %s, %s, %s, 'tamper')",
        (ws_id, base_hash, "c" * 64, other_blob),
    )
    legit = canonical_patch_json(
        base_hash, "b" * 64, [PatchEdit(path="n.txt", expected_file_hash=None, new_string="n")]
    )
    with pytest.raises(patchlog.PatchConflict, match="occupied"):
        with conn.transaction():
            patchlog.append_patch(
                conn,
                workspace_id=ws_id,
                parent_state_hash=base_hash,
                new_state_hash="b" * 64,
                patch_bytes=legit,
                run_ref="wf/step",
            )


def test_rebuild_detects_corrupted_patch_blob(clean_db, conn) -> None:
    files = {"a.txt": "x\n"}
    tree = WorkspaceTree({k: v.encode("utf-8") for k, v in files.items()})
    ws_id = patchlog.create_workspace(conn, tree.files)
    base_hash = _base_hash(ws_id, conn)
    edits = [
        PatchEdit(
            path="a.txt", expected_file_hash=sha256_hex(b"x\n"), old_string="x", new_string="y"
        )
    ]
    plan = tree.apply_edits(edits)
    with conn.transaction():
        patchlog.append_patch(
            conn,
            workspace_id=ws_id,
            parent_state_hash=base_hash,
            new_state_hash=plan.new_state_hash,
            patch_bytes=canonical_patch_json(base_hash, plan.new_state_hash, edits),
            run_ref="wf/step",
        )
    # corrupt the patch blob IN PLACE (bit rot simulation)
    row = conn.execute(
        "SELECT patch_blob_hash FROM workspace_patches WHERE workspace_id = %s AND seq = 1",
        (ws_id,),
    ).fetchone()
    conn.execute(
        "UPDATE blobs SET data = %s WHERE hash = %s",
        (psycopg.Binary(b"corrupted"), row["patch_blob_hash"]),
    )
    with pytest.raises(patchlog.BlobCorrupt):
        with db.connect(clean_db, application_name="test-corrupt") as conn2:
            patchlog.rebuild_workspace(conn2, ws_id)


# ---------------------------------------------------------------------------
# two concurrent appends at the same seq: exactly one winner
# ---------------------------------------------------------------------------


def test_two_concurrent_appends_exactly_one_winner(clean_db) -> None:
    files = {"a.txt": "base\n"}
    tree = WorkspaceTree({k: v.encode("utf-8") for k, v in files.items()})
    setup = db.connect(clean_db)
    ws_id = patchlog.create_workspace(setup, tree.files)
    base_hash = _base_hash(ws_id, setup)
    setup.close()

    # the SAME patch from two workers (one step, delivered twice)
    edits = [PatchEdit(path="b.txt", expected_file_hash=None, new_string="created\n")]
    plan = tree.apply_edits(edits)
    patch_bytes = canonical_patch_json(base_hash, plan.new_state_hash, edits)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        with db.connect(clean_db, application_name="test-cas-race") as connection:
            barrier.wait()
            try:
                with connection.transaction():
                    result = patchlog.append_patch(
                        connection,
                        workspace_id=ws_id,
                        parent_state_hash=base_hash,
                        new_state_hash=plan.new_state_hash,
                        patch_bytes=patch_bytes,
                        run_ref="wf/step-race",
                    )
                with lock:
                    outcomes.append(result.status)
            except patchlog.StaleAppend:
                with lock:
                    outcomes.append("stale_append")

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # exactly one winner committed; the loser either rolled back as stale or
    # observed the winner's row as an idempotent redelivery
    assert outcomes.count("applied") == 1
    assert all(o in {"applied", "already_applied", "stale_append"} for o in outcomes)
    check = db.connect(clean_db)
    count = check.execute(
        "SELECT count(*) AS n FROM workspace_patches WHERE workspace_id = %s", (ws_id,)
    ).fetchone()
    assert int(count["n"]) == 1  # the log has ONE patch, not two
    head_seq, head_hash = patchlog.load_workspace_head(check, ws_id)
    assert head_seq == 1 and head_hash == plan.new_state_hash
    check.close()


def test_sequential_different_patch_after_head_moved_is_stale(conn) -> None:
    files = {"a.txt": "v1\n"}
    tree = WorkspaceTree({k: v.encode("utf-8") for k, v in files.items()})
    ws_id = patchlog.create_workspace(conn, tree.files)
    base_hash = _base_hash(ws_id, conn)
    edits1 = [
        PatchEdit(
            path="a.txt", expected_file_hash=sha256_hex(b"v1\n"), old_string="v1", new_string="v2"
        )
    ]
    plan1 = tree.apply_edits(edits1)
    with conn.transaction():
        patchlog.append_patch(
            conn,
            workspace_id=ws_id,
            parent_state_hash=base_hash,
            new_state_hash=plan1.new_state_hash,
            patch_bytes=canonical_patch_json(base_hash, plan1.new_state_hash, edits1),
            run_ref="wf/step-1",
        )
    # a DIFFERENT patch with the now-stale parent: refused, log unchanged
    other = [
        PatchEdit(
            path="a.txt",
            expected_file_hash=sha256_hex(b"v1\n"),
            old_string="v1",
            new_string="OTHER",
        )
    ]
    with pytest.raises(patchlog.StaleAppend):
        with conn.transaction():
            patchlog.append_patch(
                conn,
                workspace_id=ws_id,
                parent_state_hash=base_hash,
                new_state_hash="a" * 64,
                patch_bytes=canonical_patch_json(base_hash, "a" * 64, other),
                run_ref="wf/step-late",
            )
    head_seq, head_hash = patchlog.load_workspace_head(conn, ws_id)
    assert head_seq == 1 and head_hash == plan1.new_state_hash


# ---------------------------------------------------------------------------
# integration seam: the append runs inside the step's advance transaction —
# a workflow-step failure rolls the patch back with the job completion.
# (The callback wiring itself is exercised in test_tools.py.)
# ---------------------------------------------------------------------------


def test_patch_and_job_output_commit_together_or_neither(clean_db, conn) -> None:
    """Simulate the advance transaction: handler output + patch append in one
    transaction block; a failed fenced completion rolls both back."""
    from engine.jobs import LeaseLost

    files = {"a.txt": "base\n"}
    tree = WorkspaceTree({k: v.encode("utf-8") for k, v in files.items()})
    ws_id = patchlog.create_workspace(conn, tree.files)
    job = jobs.enqueue(conn, job_type="noop")
    claimed = jobs.claim(conn, locked_by="w1", lease_seconds=clean_db.lease_seconds, types=["noop"])
    assert claimed is not None and claimed.id == job.id
    base_hash = _base_hash(ws_id, conn)
    edits = [PatchEdit(path="b.txt", expected_file_hash=None, new_string="n\n")]
    plan = tree.apply_edits(edits)
    patch_bytes = canonical_patch_json(base_hash, plan.new_state_hash, edits)

    # happy path: complete + append commit together
    with conn.transaction():
        jobs.complete(
            conn, job_id=job.id, locked_by="w1", attempts=claimed.attempts, result={"step": 1}
        )
        patchlog.append_patch(
            conn,
            workspace_id=ws_id,
            parent_state_hash=base_hash,
            new_state_hash=plan.new_state_hash,
            patch_bytes=patch_bytes,
            run_ref=f"{job.id}/1",
        )
    head_seq, _ = patchlog.load_workspace_head(conn, ws_id)
    assert head_seq == 1

    # fencing path: this worker's lease was taken over; the stale attempt's
    # complete raises LeaseLost and its patch append rolls back with it
    job2 = jobs.enqueue(conn, job_type="noop")
    claimed2 = jobs.claim(
        conn, locked_by="w2", lease_seconds=clean_db.lease_seconds, types=["noop"]
    )
    edits2 = [PatchEdit(path="c.txt", expected_file_hash=None, new_string="n\n")]
    plan2 = tree.apply_edits(edits2)
    patch2 = canonical_patch_json(plan.new_state_hash, plan2.new_state_hash, edits2)
    # the fenced predicate the real attempt holds (w2, its own token): the
    # stale append below uses w1's token and MUST lose
    jobs.complete(
        conn, job_id=job2.id, locked_by="w2", attempts=claimed2.attempts, result={"step": 9}
    )
    with pytest.raises(LeaseLost):
        with conn.transaction():
            # stale worker (w1's token) tries to complete AND append its patch
            jobs.complete(
                conn, job_id=job2.id, locked_by="w1", attempts=claimed2.attempts, result={"step": 9}
            )
            patchlog.append_patch(
                conn,
                workspace_id=ws_id,
                parent_state_hash=plan.new_state_hash,
                new_state_hash=plan2.new_state_hash,
                patch_bytes=patch2,
                run_ref=f"{job2.id}/stale",
            )
    head_seq, head_hash = patchlog.load_workspace_head(conn, ws_id)
    assert (head_seq, head_hash) == (1, plan.new_state_hash)
    count = conn.execute(
        "SELECT count(*) AS n FROM workspace_patches WHERE workspace_id = %s AND seq = 2", (ws_id,)
    ).fetchone()
    assert int(count["n"]) == 0
