"""The workspace model (checkpoint 8, Task 1): an in-memory tree.

A `WorkspaceTree` is a mapping of validated path to bytes — regular files
only. Tools never touch a host filesystem: reads, searches and patches
operate on this map, the symlink door is closed for the host side by
construction, and nothing here can follow a link or escape a root because
there is no filesystem to escape into. The durable form of the workspace
is the patch log (patchlog.py); the tree itself is rebuildable from the
content-addressed base snapshot plus the ordered log, on any worker.

`state_hash` is the hash of the sorted (path, sha256) manifest — a pure
function of the tree, so any worker that rebuilds base+log to the same
files computes the same hash. It is the CAS token of the patch log
(patchlog.py) and the precondition currency of apply_patch.

Snapshots are tars serialized by `snapshot_tar` and parsed back by the
SAME safe extractor the sandbox copy-out uses (`sandbox.parse_workspace_tar`):
one extractor, the hostile suite's table applies to both call sites.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from typing import Any

from pydantic import BaseModel, ConfigDict

from .paths import is_sha256_hex, validate_path
from .sandbox import SandboxError, SandboxUnsafeArchive, parse_workspace_tar

# The tree mirrors the sandbox's copy-out boundary: what can come OUT of a
# sandbox can go INTO a workspace, and no further.
FILE_MAX_BYTES = 4 * 1024 * 1024  # per-file cap inside the tree
MAX_FILES = 20_000  # == sandbox _COPY_OUT_MAX_ENTRIES
SNAPSHOT_MAX_BYTES = 32 * 1024 * 1024  # == copy_out_mb default
PATCH_MAX_BYTES = 8 * 1024 * 1024  # a patch is O(edit), never O(workspace)
MAX_EDITS_PER_CALL = 64

_EDITABLE_ERROR_CODES = frozenset(
    {
        "stale_precondition",
        "no_match",
        "ambiguous_match",
        "path_denied",
        "too_large",
        "binary_file",
    }
)

_BINARY_SNIFF = 8000  # NUL within the first 8 KiB => binary (git's rule)


class PatchError(Exception):
    """A patch precondition or cap failure; `.code` is the tool error code."""

    def __init__(self, code: str, message: str) -> None:
        if code not in _EDITABLE_ERROR_CODES:
            raise ValueError(f"unknown patch error code {code!r}")
        super().__init__(message)
        self.code = code


class PatchEdit(BaseModel):
    """One exact-text edit. More robust for models than diffs: the old
    text must appear EXACTLY once, so a drifted base fails loudly.

    expected_file_hash=None means the file must NOT exist (create);
    otherwise it is the sha256 hex of the file the edit was computed
    against — a mismatch is stale_precondition. delete=True removes the
    file: old_string must be the file's full content, new_string empty.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    expected_file_hash: str | None = None
    old_string: str = ""
    new_string: str = ""
    delete: bool = False

    @property
    def old_bytes(self) -> bytes:
        return self.old_string.encode("utf-8")

    @property
    def new_bytes(self) -> bytes:
        return self.new_string.encode("utf-8")


class PlannedEdits:
    """The result of planning: an overlay to commit, or nothing."""

    __slots__ = ("overlay", "new_state_hash")

    def __init__(self, overlay: dict[str, bytes | None], new_state_hash: str) -> None:
        self.overlay = overlay
        self.new_state_hash = new_state_hash


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_bytes(files: dict[str, bytes]) -> bytes:
    lines = []
    for path in sorted(files):
        lines.append(f"{path}\t{sha256_hex(files[path])}\n")
    return "".join(lines).encode("utf-8")


def _tree_hash(files: dict[str, bytes]) -> str:
    return sha256_hex(_manifest_bytes(files))


class WorkspaceTree:
    """Regular files only: exact path -> content bytes."""

    __slots__ = ("_files", "_hash")

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self._files: dict[str, bytes] = dict(files or {})
        self._hash: str | None = None

    @property
    def files(self) -> dict[str, bytes]:
        """A copy; mutate through apply/delete paths only."""
        return dict(self._files)

    def __contains__(self, path: object) -> bool:
        return path in self._files

    def get(self, path: str) -> bytes | None:
        return self._files.get(path)

    def file_count(self) -> int:
        return len(self._files)

    def state_hash(self) -> str:
        if self._hash is None:
            self._hash = _tree_hash(self._files)
        return self._hash

    # --- snapshot transport -------------------------------------------------

    def snapshot_tar(self) -> bytes:
        """Serialize the tree as a tar of regular files, sorted order.

        Deterministic: same tree -> byte-identical tar -> same blob hash.
        """
        if len(self._files) > MAX_FILES:
            raise PatchError("too_large", f"tree has {len(self._files)} files")
        for path, content in self._files.items():
            if len(content) > FILE_MAX_BYTES:
                raise PatchError("too_large", f"file {path!r} exceeds the file cap")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for path in sorted(self._files):
                info = tarfile.TarInfo(name=path)
                info.size = len(self._files[path])
                tar.addfile(info, io.BytesIO(self._files[path]))
        data = buffer.getvalue()
        if len(data) > SNAPSHOT_MAX_BYTES:
            raise PatchError("too_large", f"snapshot exceeds {SNAPSHOT_MAX_BYTES} bytes")
        return data

    @classmethod
    def from_snapshot(cls, data: bytes) -> WorkspaceTree:
        """Rebuild a tree from a snapshot blob through the safe extractor.

        A snapshot WE authored contains regular files only, so any link or
        special entry in it is tampering: refused, not counted.
        """
        if len(data) > SNAPSHOT_MAX_BYTES:
            raise PatchError("too_large", f"snapshot exceeds {SNAPSHOT_MAX_BYTES} bytes")
        try:
            files, symlinks_skipped = parse_workspace_tar(
                data, max_bytes=SNAPSHOT_MAX_BYTES, max_entries=MAX_FILES
            )
        except SandboxUnsafeArchive as exc:
            raise PatchError("path_denied", f"unsafe snapshot: {exc}") from exc
        except SandboxError as exc:
            raise PatchError("too_large", f"snapshot refused: {exc}") from exc
        if symlinks_skipped:
            raise PatchError(
                "path_denied",
                f"snapshot contains {symlinks_skipped} link entries; tampering",
            )
        tree = cls(files)
        for path, content in files.items():
            try:
                validate_path(path)
            except ValueError as exc:
                raise PatchError("path_denied", f"snapshot member {path!r}: {exc}") from exc
            if len(content) > FILE_MAX_BYTES:
                raise PatchError("too_large", f"file {path!r} exceeds the file cap")
        return tree

    # --- patch application --------------------------------------------------

    def plan_edits(self, edits: list[PatchEdit]) -> PlannedEdits:
        """Validate and plan ALL edits against the current tree, atomically.

        Returns the overlay; raises PatchError (typed codes) on the FIRST
        failure without touching the tree. Sequenced edits see prior edits'
        effects (create-then-edit within one call works); preconditions
        hash against the planned state, not the committed one.
        """
        overlay: dict[str, bytes | None] = {}
        for edit in edits:
            try:
                validate_path(edit.path)
            except ValueError as exc:
                raise PatchError("path_denied", str(exc)) from exc
            if edit.delete and edit.new_string != "":
                raise PatchError("no_match", "delete edit requires empty new_string")
            self._plan_one(edit, overlay)
        merged = dict(self._files)
        for path, content in overlay.items():
            if content is None:
                merged.pop(path, None)
            else:
                merged[path] = content
        if len(merged) > MAX_FILES:
            raise PatchError("too_large", "tree would exceed the file-count cap")
        return PlannedEdits(overlay=overlay, new_state_hash=_tree_hash(merged))

    def _planned_content(self, path: str, overlay: dict[str, bytes | None]) -> bytes | None:
        if path in overlay:
            return overlay[path]
        return self._files.get(path)

    def _plan_one(
        self, edit: PatchEdit, overlay: dict[str, bytes | None]
    ) -> None:
        current = self._planned_content(edit.path, overlay)
        if edit.expected_file_hash is None:
            # create: the file must not exist, old_string must be empty
            if current is not None:
                raise PatchError(
                    "stale_precondition",
                    f"{edit.path!r} already exists; create requires absent file",
                )
            if edit.old_string != "":
                raise PatchError(
                    "no_match", "create edit requires empty old_string"
                )
            overlay[edit.path] = edit.new_bytes
            return
        if not is_sha256_hex(edit.expected_file_hash):
            raise PatchError(
                "stale_precondition",
                "expected_file_hash must be 64-hex sha256 or null",
            )
        if current is None:
            raise PatchError(
                "stale_precondition",
                f"{edit.path!r} does not exist but the edit expects it",
            )
        if sha256_hex(current) != edit.expected_file_hash:
            raise PatchError(
                "stale_precondition",
                f"{edit.path!r} does not match expected_file_hash",
            )
        if edit.delete:
            if edit.old_string.encode("utf-8") != current:
                raise PatchError(
                    "no_match", "delete edit requires the file's full content"
                )
            overlay[edit.path] = None
            return
        if edit.old_string == "":
            raise PatchError("no_match", "empty old_string matches nothing")
        # same binary rule as read_file/search: NUL in the first 8 KiB means
        # the file is not text the model can have read coherently
        if b"\x00" in current[:_BINARY_SNIFF]:
            raise PatchError("binary_file", f"{edit.path!r} looks binary (NUL byte)")
        try:
            text = current.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchError("binary_file", f"{edit.path!r} is not UTF-8 text: {exc}") from exc
        occurrences = text.count(edit.old_string)
        if occurrences == 0:
            raise PatchError("no_match", f"{edit.old_string!r} not found in {edit.path!r}")
        if occurrences > 1:
            raise PatchError(
                "ambiguous_match",
                f"{edit.old_string!r} matches {occurrences} times in {edit.path!r}",
            )
        replaced = text.replace(edit.old_string, edit.new_string, 1)
        encoded = replaced.encode("utf-8")
        if len(encoded) > FILE_MAX_BYTES:
            raise PatchError("too_large", f"{edit.path!r} would exceed the file cap")
        overlay[edit.path] = encoded

    def commit_plan(self, plan: PlannedEdits) -> None:
        for path, content in plan.overlay.items():
            if content is None:
                self._files.pop(path, None)
            else:
                self._files[path] = content
        self._hash = plan.new_state_hash

    def apply_edits(self, edits: list[PatchEdit]) -> PlannedEdits:
        """plan + commit in one step; tree untouched on any failure."""
        plan = self.plan_edits(edits)
        self.commit_plan(plan)
        return plan


def canonical_patch_json(
    parent_state_hash: str, new_state_hash: str, edits: list[PatchEdit]
) -> bytes:
    """The durable patch blob: canonical JSON, byte-stable for hash keys.

    sort_keys + compact separators + ensure_ascii=False: re-serializing
    the same edits always produces the same bytes, so the blob hash is a
    pure function of the patch content (idempotent-redelivery currency).
    """
    if len(edits) == 0:
        raise ValueError("a patch needs at least one edit")
    payload: dict[str, Any] = {
        "parent_state_hash": parent_state_hash,
        "new_state_hash": new_state_hash,
        "edits": [edit.model_dump() for edit in edits],
    }
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    data = text.encode("utf-8")
    if len(data) > PATCH_MAX_BYTES:
        raise PatchError("too_large", f"patch exceeds {PATCH_MAX_BYTES} bytes")
    return data


def decode_patch_json(data: bytes) -> tuple[str, str, list[PatchEdit]]:
    """Parse a patch blob back into (parent, new, edits) — rebuild path."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatchError("path_denied", f"unreadable patch blob: {exc}") from exc
    try:
        edits = [PatchEdit.model_validate(item) for item in payload["edits"]]
        parent = payload["parent_state_hash"]
        new = payload["new_state_hash"]
    except (KeyError, ValueError) as exc:
        raise PatchError("path_denied", f"malformed patch blob: {exc}") from exc
    if not is_sha256_hex(parent) or not is_sha256_hex(new):
        raise PatchError("path_denied", "patch blob has malformed state hashes")
    return parent, new, edits


# re-exported for tests/tools; the import keeps the dependency explicit
__all__ = [
    "FILE_MAX_BYTES",
    "MAX_EDITS_PER_CALL",
    "MAX_FILES",
    "PATCH_MAX_BYTES",
    "SNAPSHOT_MAX_BYTES",
    "PatchEdit",
    "PatchError",
    "PlannedEdits",
    "WorkspaceTree",
    "canonical_patch_json",
    "decode_patch_json",
    "sha256_hex",
]
