"""Path-rule table (Task 4) and the Hypothesis property: any input is
either rejected outright, or it is a workspace-safe relative path — no
absolute form, no traversal, no separators or components that could mean
something else once it becomes a tree key."""

from __future__ import annotations

import unicodedata

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
    # refuses: control characters (Unicode Cc) — newline, tab, CR, DEL, C1
    ("a\nb", "control character"),
    ("a\tb", "control character"),
    ("a\rb", "control character"),
    ("a\x7fb", "control character"),
    ("a\x85b", "control character"),
    ("a\x1bb", "control character"),
    # refuses: bidirectional overrides — they reorder DISPLAYED text, so a
    # diff-review screen can show "report.txt.exe" while the name ends .exe
    ("report\u202exe", "bidirectional override"),
    ("a\u2066b", "bidirectional override"),
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


# THE property, with an adversarial alphabet: plain st.text() almost never
# generates '..' or '.git' by chance, so N random strings prove little.
# These components are exactly the forbidden / separator-adjacent shapes,
# joined by both separators, plus an optional leading slash.
_ADVERSARIAL_COMPONENTS = ["", ".", "..", ".git", ".GIT", "a", "\x00", "é"]


@st.composite
def _adversarial_paths(draw) -> str:
    components = draw(st.lists(st.sampled_from(_ADVERSARIAL_COMPONENTS), min_size=1, max_size=6))
    separator = draw(st.sampled_from(["/", "\\"]))
    path = separator.join(components)
    if draw(st.booleans()):
        path = "/" + path  # absolute-shaped half the time
    return path


def _assert_accepted_path_is_clean(path: str) -> None:
    """The final assertion, INDEPENDENT of the validator: if validate_path
    accepted, the path provably violates none of the rules — restated here
    inlined (no import of the validator's internals), so a bug in
    validate_path cannot hide behind the validator itself. Checked against
    the RAW input string, not the return value (which is asserted equal
    separately)."""
    assert not path.startswith("/"), f"absolute path accepted: {path!r}"
    assert "\\" not in path, f"backslash accepted: {path!r}"
    for component in path.split("/"):
        assert component != "", f"empty component accepted: {path!r}"
        assert component != ".", f"'.' component accepted: {path!r}"
        assert component != "..", f"'..' component accepted: {path!r}"
        assert component.lower() != ".git", f"'.git' component accepted: {path!r}"
    for ch in path:
        code = ord(ch)
        assert unicodedata.category(ch) != "Cc", (
            f"control character U+{code:04X} accepted: {path!r}"
        )
        assert not (0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069), (
            f"bidirectional override U+{code:04X} accepted: {path!r}"
        )
    # root-confined: with no '..' component, joining onto a root is a pure
    # descent and cannot escape it
    assert ("/root/" + path).startswith("/root/")


@given(_adversarial_paths())
@hyp_settings(max_examples=500)
def test_fuzz_adversarial_components_rejected_or_clean(path: str) -> None:
    try:
        result = validate_path(path)
    except PathDenied:
        return  # rejected outright: fine
    assert result == path  # returned unchanged: the exact tree key
    _assert_accepted_path_is_clean(path)


# kept as a second layer: arbitrary text can still hit rules the adversarial
# alphabet does not encode (control characters, bidi overrides, surrogates)
@given(st.text(max_size=200))
@hyp_settings(max_examples=500)
def test_fuzz_any_input_rejected_or_root_confined(path: str) -> None:
    try:
        result = validate_path(path)
    except PathDenied:
        return  # rejected outright: fine
    assert result == path
    _assert_accepted_path_is_clean(path)
