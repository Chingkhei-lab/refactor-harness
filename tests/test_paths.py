"""Path-rule table (Task 4) and the Hypothesis property: any input is
either rejected outright, or it is a workspace-safe relative path — no
absolute form, no traversal, no separators or components that could mean
something else once it becomes a tree key."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from engine.paths import (
    MAX_PATH_BYTES,
    PathDenied,
    is_sha256_hex,
    validate_path,
)

# Every rule in Task 4, one row per behavior.
# (path, expected: None = accepted, or a substring of the refusal message)
PATH_TABLE = [
    # accepts: ordinary relative paths, dots in names, deep nesting, unicode
    ("src/engine/tools.py", None),
    ("README.md", None),
    ("a/b/c/d/e/f/g.txt", None),
    (".hidden/not.so.hidden", None),
    ("naïve/文件.txt", None),
    ("a..b/looks..dodgy.but.is.a.name", None),  # '..' only as a WHOLE component
    (".gitignore", None),  # '.gitignore' is a file, not the metadata dir
    ("x" * 255, None),  # exactly NAME_MAX: fine
    # refuses: empties and non-paths
    ("", "non-empty"),
    # refuses: NUL
    ("a\x00b", "NUL"),
    # refuses: backslashes outright (POSIX tree; Windows confusion)
    ("a\\b", "backslash"),
    ("\\windows", "backslash"),
    ("C:\\x", "backslash"),
    # refuses: absolute
    ("/etc/passwd", "absolute"),
    ("//etc", "absolute"),
    # refuses: traversal
    ("..", "'..'"),
    ("../etc/passwd", "'..'"),
    ("a/../b", "'..'"),
    ("a/..", "'..'"),
    ("a/b/../../../../c", "'..'"),
    # refuses: '.' components (no normalization in the tree)
    (".", "'.'"),
    ("./a", "'.'"),
    ("a/./b", "'.'"),
    ("a/.", "'.'"),
    # refuses: empty components (//, trailing /)
    ("a//b", "empty path component"),
    ("a/", "empty path component"),
    # refuses: the .git metadata component, any case, any depth
    (".git", "'.git'"),
    (".GIT/config", "'.git'"),
    (".Git/hooks/pre-commit", "'.git'"),
    ("a/.git/config", "'.git'"),
    # refuses: over-long
    ("x" * (MAX_PATH_BYTES + 1), "longer"),
    (f"{'y' * 256}/b", "longer"),
    # refuses: not valid UTF-8 text (lone surrogate from a JSON \uD800 escape)
    ("a\ud800b", "UTF-8"),
]


@pytest.mark.parametrize(("path", "expected_msg"), PATH_TABLE)
def test_path_rules(path: str, expected_msg: str | None) -> None:
    if expected_msg is None:
        assert validate_path(path) == path  # returned unchanged: exact key
        return
    with pytest.raises(PathDenied, match=expected_msg):
        validate_path(path)


def test_non_string_is_denied() -> None:
    with pytest.raises(PathDenied):
        validate_path(123)  # type: ignore[arg-type]


def test_sha256_hex() -> None:
    assert is_sha256_hex("a" * 64)
    assert is_sha256_hex("0123456789abcdef" * 4)
    assert not is_sha256_hex("A" * 64)  # uppercase refused
    assert not is_sha256_hex("a" * 63)
    assert not is_sha256_hex("a" * 65)
    assert not is_sha256_hex("z" * 64)


# THE property: for ANY string input, the validator either refuses it or
# hands back a path that provably resolves inside the root — relative, no
# '..' component, and (joined onto a root) never escaping it. Accepted but
# escaping is the bug this test exists to catch.
@given(st.text(max_size=200))
@hyp_settings(max_examples=500)
def test_fuzz_any_input_rejected_or_root_confined(path: str) -> None:
    try:
        result = validate_path(path)
    except PathDenied:
        return  # rejected outright: fine
    # returned unchanged and provably safe
    assert result == path
    assert not path.startswith("/")
    assert not path.startswith("\\")
    components = path.split("/")
    assert all(components)  # no empty components
    assert ".." not in components and "." not in components
    # joining onto a root never escapes: no '..' above means the join is a
    # pure descent; assert the joined form stays under the root prefix
    joined = "/root/" + path
    assert joined.startswith("/root/") and "//" not in joined[6:]
