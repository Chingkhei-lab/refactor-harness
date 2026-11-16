"""Workspace model (Task 1) and patch semantics (Task 4's apply engine)."""

from __future__ import annotations

import hashlib

import pytest

from engine.paths import PathDenied, validate_path
from engine.workspace import (
    FILE_MAX_BYTES,
    MAX_FILES,
    PATCH_MAX_BYTES,
    PatchEdit,
    PatchError,
    WorkspaceTree,
    canonical_patch_json,
    decode_patch_json,
    sha256_hex,
)


def make_tree(**files: str) -> WorkspaceTree:
    """Bare names map to `name + '.txt'` so `make_tree(a='x')` pairs with
    edits on 'a.txt'; explicit names ('src/a.py') are used verbatim."""
    return WorkspaceTree(
        {
            (name if "." in name else f"{name}.txt"): value.encode("utf-8")
            for name, value in files.items()
        }
    )


# ---------------------------------------------------------------------------
# state_hash: a pure function of the sorted (path, sha256) manifest
# ---------------------------------------------------------------------------


def test_state_hash_is_order_independent() -> None:
    # same content in different dict insertion orders -> same hash
    a = WorkspaceTree({"z.txt": b"2", "a.txt": b"1", "m.txt": b"3"})
    b = WorkspaceTree({"m.txt": b"3", "z.txt": b"2", "a.txt": b"1"})
    assert a.state_hash() == b.state_hash()


def test_state_hash_distinguishes_content_and_names() -> None:
    base = make_tree(a="x")
    content_changed = make_tree(a="y")
    name_changed = make_tree(b="x")
    assert base.state_hash() != content_changed.state_hash()
    assert base.state_hash() != name_changed.state_hash()


def test_state_hash_is_cached_and_invalidated_by_commit() -> None:
    tree = make_tree(a="x")
    before = tree.state_hash()
    plan = tree.plan_edits(
        [
            PatchEdit(
                path="a.txt", expected_file_hash=sha256_hex(b"x"), old_string="x", new_string="y"
            )
        ]
    )
    tree.commit_plan(plan)
    assert tree.state_hash() == plan.new_state_hash
    assert tree.state_hash() != before


def test_state_hash_matches_the_documented_manifest() -> None:
    tree = make_tree(a="x", b="yy")
    manifest = "".join(
        f"{p}\t{hashlib.sha256(tree.get(p)).hexdigest()}\n" for p in sorted(tree.files)
    )
    assert tree.state_hash() == sha256_hex(manifest.encode("utf-8"))


# ---------------------------------------------------------------------------
# snapshots: round trip through the shared safe extractor
# ---------------------------------------------------------------------------


def test_snapshot_round_trip_preserves_bytes_and_hash() -> None:
    tree = make_tree(**{"src/a.py": "print(1)\n", "b.txt": "ünïcode\n", "empty": ""})
    tar = tree.snapshot_tar()
    rebuilt = WorkspaceTree.from_snapshot(tar)
    assert rebuilt.files == tree.files
    assert rebuilt.state_hash() == tree.state_hash()


def test_snapshot_is_deterministic() -> None:
    tree = make_tree(a="x", b="y")
    assert tree.snapshot_tar() == tree.snapshot_tar()


def test_snapshot_refuses_links_as_tampering() -> None:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("a.txt")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(PatchError, match="tampering"):
        WorkspaceTree.from_snapshot(buffer.getvalue())


def test_snapshot_caps() -> None:
    tree = WorkspaceTree({"big": b"x" * (FILE_MAX_BYTES + 1)})
    with pytest.raises(PatchError, match="file cap"):
        tree.snapshot_tar()
    tree_many = WorkspaceTree({f"f{i}": b"x" for i in range(MAX_FILES + 1)})
    with pytest.raises(PatchError, match="files"):
        tree_many.snapshot_tar()


def test_from_snapshot_snapshot_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = make_tree(a="x")
    tar = tree.snapshot_tar()
    monkeypatch.setattr("engine.workspace.SNAPSHOT_MAX_BYTES", len(tar) - 1)
    with pytest.raises(PatchError, match="too_large|snapshot"):
        WorkspaceTree.from_snapshot(tar)


def test_from_snapshot_refuses_unsafe_member_names() -> None:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("../escape.txt")
        data = b"x"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(PatchError, match="escape"):
        WorkspaceTree.from_snapshot(buffer.getvalue())


# ---------------------------------------------------------------------------
# patch semantics
# ---------------------------------------------------------------------------


def test_patch_create_edit() -> None:
    tree = make_tree()
    plan = tree.plan_edits([PatchEdit(path="new.txt", expected_file_hash=None, new_string="hello")])
    tree.commit_plan(plan)
    assert tree.get("new.txt") == b"hello"


def test_patch_create_refuses_existing_file() -> None:
    tree = make_tree(a="x")
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits([PatchEdit(path="a.txt", expected_file_hash=None, new_string="y")])
    assert excinfo.value.code == "stale_precondition"


def test_patch_delete_edit() -> None:
    tree = make_tree(a="line1\nline2\n")
    plan = tree.plan_edits(
        [
            PatchEdit(
                path="a.txt",
                expected_file_hash=sha256_hex(b"line1\nline2\n"),
                old_string="line1\nline2\n",
                new_string="",
                delete=True,
            )
        ]
    )
    tree.commit_plan(plan)
    assert tree.get("a.txt") is None


def test_patch_delete_requires_exact_full_content() -> None:
    tree = make_tree(a="line1\nline2\n")
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="a.txt",
                    expected_file_hash=sha256_hex(b"line1\nline2\n"),
                    old_string="line1\n",
                    new_string="",
                    delete=True,
                )
            ]
        )
    assert excinfo.value.code == "no_match"


def test_patch_replace_unique_match() -> None:
    tree = make_tree(a="hello world\n")
    plan = tree.plan_edits(
        [
            PatchEdit(
                path="a.txt",
                expected_file_hash=sha256_hex(b"hello world\n"),
                old_string="world",
                new_string="there",
            )
        ]
    )
    tree.commit_plan(plan)
    assert tree.get("a.txt") == b"hello there\n"


def test_patch_no_match() -> None:
    tree = make_tree(a="hello\n")
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="a.txt",
                    expected_file_hash=sha256_hex(b"hello\n"),
                    old_string="goodbye",
                    new_string="x",
                )
            ]
        )
    assert excinfo.value.code == "no_match"


def test_patch_empty_old_string_matches_nothing() -> None:
    tree = make_tree(a="hello\n")
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="a.txt",
                    expected_file_hash=sha256_hex(b"hello\n"),
                    old_string="",
                    new_string="x",
                )
            ]
        )
    assert excinfo.value.code == "no_match"


def test_patch_ambiguous_match() -> None:
    tree = make_tree(a="dup dup\n")
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="a.txt",
                    expected_file_hash=sha256_hex(b"dup dup\n"),
                    old_string="dup",
                    new_string="x",
                )
            ]
        )
    assert excinfo.value.code == "ambiguous_match"


def test_patch_stale_precondition_wrong_hash() -> None:
    tree = make_tree(a="current\n")
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="a.txt",
                    expected_file_hash=sha256_hex(b"stale\n"),
                    old_string="current",
                    new_string="next",
                )
            ]
        )
    assert excinfo.value.code == "stale_precondition"


def test_patch_stale_precondition_missing_file() -> None:
    tree = make_tree()
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="missing.txt",
                    expected_file_hash=sha256_hex(b"x"),
                    old_string="x",
                    new_string="y",
                )
            ]
        )
    assert excinfo.value.code == "stale_precondition"


def test_patch_path_denied() -> None:
    tree = make_tree(a="x")
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits([PatchEdit(path="/etc/passwd", expected_file_hash=None, new_string="x")])
    assert excinfo.value.code == "path_denied"
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits([PatchEdit(path="a/../b", expected_file_hash=None, new_string="x")])
    assert excinfo.value.code == "path_denied"


def test_patch_result_too_large() -> None:
    tree = make_tree(a="x")
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="a.txt",
                    expected_file_hash=sha256_hex(b"x"),
                    old_string="x",
                    new_string="y" * (FILE_MAX_BYTES + 1),
                )
            ]
        )
    assert excinfo.value.code == "too_large"


def test_patch_file_count_cap() -> None:
    tree = WorkspaceTree({f"f{i}": b"x" for i in range(MAX_FILES)})
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits([PatchEdit(path="overflow.txt", expected_file_hash=None, new_string="x")])
    assert excinfo.value.code == "too_large"


def test_patch_multi_edit_atomic_across_files() -> None:
    tree = make_tree(a="aaa\n", b="bbb\n")
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="a.txt",
                    expected_file_hash=sha256_hex(b"aaa\n"),
                    old_string="aaa",
                    new_string="zzz",
                ),
                PatchEdit(
                    path="b.txt",
                    expected_file_hash=sha256_hex(b"bbb\n"),
                    old_string="MISSING",
                    new_string="yyy",
                ),
            ]
        )
    assert excinfo.value.code == "no_match"
    # NOTHING applied: the failure of one edit must not commit the others
    assert tree.get("a.txt") == b"aaa\n"
    assert tree.get("b.txt") == b"bbb\n"


def test_patch_sequenced_edits_see_prior_effects() -> None:
    # create-then-edit within ONE call: the second edit sees the first's bytes
    tree = make_tree()
    plan = tree.plan_edits(
        [
            PatchEdit(path="new.txt", expected_file_hash=None, new_string="hello world"),
            PatchEdit(
                path="new.txt",
                expected_file_hash=sha256_hex(b"hello world"),
                old_string="world",
                new_string="!",
            ),
        ]
    )
    tree.commit_plan(plan)
    assert tree.get("new.txt") == b"hello !"


def test_apply_edits_untouched_on_any_failure() -> None:
    tree = make_tree(a="x\n")
    before = tree.state_hash()
    with pytest.raises(PatchError):
        tree.apply_edits(
            [
                PatchEdit(
                    path="a.txt",
                    expected_file_hash=sha256_hex(b"x\n"),
                    old_string="x",
                    new_string="y",
                ),
                PatchEdit(path="/abs", expected_file_hash=None, new_string="z"),
            ]
        )
    assert tree.state_hash() == before
    assert tree.get("a.txt") == b"x\n"


def test_patch_edits_binary_file_refused() -> None:
    tree = WorkspaceTree({"a.bin": b"\x00\x01binary"})
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="a.bin",
                    expected_file_hash=sha256_hex(b"\x00\x01binary"),
                    old_string="x",
                    new_string="y",
                )
            ]
        )
    assert excinfo.value.code == "binary_file"


def test_patch_edits_non_utf8_refused() -> None:
    tree = WorkspaceTree({"a.bin": b"\xff\xfe\x00"})
    with pytest.raises(PatchError) as excinfo:
        tree.plan_edits(
            [
                PatchEdit(
                    path="a.bin",
                    expected_file_hash=sha256_hex(b"\xff\xfe\x00"),
                    old_string="x",
                    new_string="y",
                )
            ]
        )
    assert excinfo.value.code == "binary_file"


# ---------------------------------------------------------------------------
# canonical patch: byte-stable JSON, the durable blob
# ---------------------------------------------------------------------------


def test_canonical_patch_is_byte_stable() -> None:
    edits = [
        PatchEdit(path="a.txt", expected_file_hash=sha256_hex(b"x"), old_string="x", new_string="y")
    ]
    parent, new = "0" * 64, "1" * 64
    blob1 = canonical_patch_json(parent, new, edits)
    blob2 = canonical_patch_json(parent, new, [PatchEdit.model_validate(edits[0].model_dump())])
    assert blob1 == blob2  # same logical patch -> same bytes -> same hash


def test_canonical_patch_decode_round_trip() -> None:
    edits = [
        PatchEdit(
            path="a.txt",
            expected_file_hash=sha256_hex(b"abc\n"),
            old_string="abc",
            new_string="xyz",
        ),
        PatchEdit(path="new.txt", expected_file_hash=None, new_string="created"),
    ]
    parent = sha256_hex(b"whatever")
    blob = canonical_patch_json(parent, "f" * 64, edits)
    got_parent, got_new, got_edits = decode_patch_json(blob)
    assert got_parent == parent
    assert got_new == "f" * 64
    assert got_edits == edits


def test_canonical_patch_requires_at_least_one_edit() -> None:
    with pytest.raises(ValueError, match="at least one edit"):
        canonical_patch_json("0" * 64, "1" * 64, [])


def test_canonical_patch_cap() -> None:
    huge = PatchEdit(
        path="big.txt", expected_file_hash=None, new_string="x" * (PATCH_MAX_BYTES + 1)
    )
    with pytest.raises(PatchError, match="exceeds"):
        canonical_patch_json("0" * 64, "1" * 64, [huge])


def test_decode_patch_rejects_garbage() -> None:
    for blob in (
        b"not json",
        b'{"edits": []}',
        b'{"edits": [{}], "parent_state_hash": "x", "new_state_hash": "y"}',
    ):
        with pytest.raises(PatchError):
            decode_patch_json(blob)


# the PathDenied contract: patch paths go through the same validator
def test_patchedit_path_validation_shared() -> None:
    with pytest.raises(PathDenied):
        validate_path(".git/config")
