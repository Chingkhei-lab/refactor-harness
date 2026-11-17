"""Path rules for workspace tools (checkpoint 8, Task 4).

The workspace tree is an exact-string map of POSIX-style relative paths —
no resolution, no normalization — so the rules here are what make a path
unambiguous and safe before it ever becomes a key in that map. Every rule
violation is `path_denied`; the caller surfaces the code, never a raw
exception.

Decisions the spec asked for:

- **Backslashes are rejected outright** (`path_denied`). The tree is a
  Linux tree; a backslash in model output is almost always a Windows-path
  confusion attack — either a separator that will not separate (`a\b` is
  ONE component here) or a traversal attempt. Accepting it buys a rare
  legal filename at the cost of every Windows-shaped input silently
  meaning the wrong thing.
- **No Unicode normalization.** Linux filenames are byte strings; NFC and
  NFD spellings of the same word are DIFFERENT files, and the tree must
  never merge them silently (a normalization pass would be a
  collision-making rewrite of untrusted names). Paths must decode as
  strict UTF-8 and are then compared byte-exactly.
- **`.` components are rejected too**, not just `..`: with no
  normalization in the tree, `a/./b` would be a different key from `a/b`
  — the same confusion class `..` protects against, one step weaker.
- The `.git` component is refused case-insensitively (`.GIT`, `.Git`):
  the workspace must never carry repository metadata the model could
  rewrite (hooks, config) — D3's symlink-door rule applied to names.
- **Control characters and bidirectional overrides are refused**:
  everything in Unicode category Cc (C0/C1 controls — newline, tab, CR,
  DEL) and the bidi overrides U+202A–U+202E / U+2066–U+2069. A newline in
  a path breaks line-oriented tooling and logs; a bidi override reorders
  the DISPLAYED text, so a reviewer's diff screen can show
  `report.txt.exe` while the real name ends in `.exe` — the filename is
  an attack surface for human review, not just for the tree.
"""

from __future__ import annotations

import unicodedata

MAX_PATH_BYTES = 1024  # total path length (bytes, UTF-8)
MAX_COMPONENT_BYTES = 255  # NAME_MAX on Linux, per component

_HEX_HASH_LENGTH = 64

# Bidirectional text overrides: they reorder the DISPLAYED characters, so a
# path can LOOK benign in a diff-review screen while its real name ends in
# ".exe". Ranges per the Unicode Bidi_Class=ON override/isolate set.
_BIDI_OVERRIDE_RANGES = (
    (0x202A, 0x202E),  # LRE, RLE, PDF, LRO, RLO
    (0x2066, 0x2069),  # LRI, RLI, FSI, PDI
)


def _is_bidi_override(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _BIDI_OVERRIDE_RANGES)


class PathDenied(ValueError):
    """A path violated the workspace rules. `.code` is the tool error code."""

    code = "path_denied"


def is_sha256_hex(value: str) -> bool:
    """Content hashes are lowercase 64-hex sha256 digests."""
    if len(value) != _HEX_HASH_LENGTH:
        return False
    return all(ch in "0123456789abcdef" for ch in value)


def validate_path(path: str) -> str:
    """Validate a workspace-relative path and return it unchanged.

    The tree stores exact strings, so there is nothing to normalize: the
    input is either a valid key or refused. Raises PathDenied on every
    rule violation.
    """
    if not isinstance(path, str) or not path:
        raise PathDenied("path must be a non-empty string")
    if "\x00" in path:
        raise PathDenied("NUL byte in path")
    if "\\" in path:
        raise PathDenied("backslash in path (POSIX separator expected)")
    for ch in path:
        if unicodedata.category(ch) == "Cc":
            # all C0/C1 controls: newline, tab, CR, DEL, C1 bytes — a
            # newline breaks line-oriented logs; the rest never belong
            # in a workspace filename
            raise PathDenied(f"control character U+{ord(ch):04X} in path")
        if _is_bidi_override(ch):
            raise PathDenied(
                f"bidirectional override U+{ord(ch):04X} in path (reorders displayed text)"
            )
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError as exc:
        # lone surrogates from JSON \\uD800 escapes: not real text
        raise PathDenied(f"path is not valid UTF-8 text: {exc}") from exc
    if path.startswith("/"):
        raise PathDenied("absolute path")
    if len(encoded) > MAX_PATH_BYTES:
        raise PathDenied(f"path longer than {MAX_PATH_BYTES} bytes")
    for component in path.split("/"):
        if not component:
            raise PathDenied("empty path component (//, leading or trailing /)")
        if component == ".":
            raise PathDenied("'.' component (no normalization in the tree)")
        if component == "..":
            raise PathDenied("'..' component")
        if component.encode("utf-8") and len(component.encode("utf-8")) > MAX_COMPONENT_BYTES:
            raise PathDenied(f"component longer than {MAX_COMPONENT_BYTES} bytes")
        if component.lower() == ".git":
            raise PathDenied("'.git' component (case-insensitive)")
    return path
