"""Behavior harness (Phase 4): evidence about behaviour, never a proof.

Modules: `observation` (canonical encoding + content-addressed storage),
`determinism` (the shim), `cases` (case sets), `runner` (controlled passes),
`child` (the in-sandbox executor).
"""

__all__ = ["cases", "child", "determinism", "observation", "runner"]
