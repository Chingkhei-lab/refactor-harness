# Harmless refactors and one seeded bug

Target `parse` 1.22.4 at 2026-11-19T21:53:00.266618+00:00; case set `273` cases, sha256 `283155b0bbb98167…`.

Evidence, not proof: this says these refactors did not change what the cases observe. It does not say the code is equivalent, and a refactor outside what the cases reach would look identical for the wrong reason.

| Refactor | Kind | Verdict | identical | equivalent_bounded | different | Lines |
| --- | --- | --- | --- | --- | --- | --- |
| `ruff-format` | mechanical: `ruff format` | **clean** | 272 | 1 | 0 | -8 |
| `reorder-definitions` | mechanical: move a definition | **clean** | 272 | 1 | 0 | +0 |
| `libcst-rename-local` | mechanical: libcst codemod renames a local | **clean** | 272 | 1 | 0 | +0 |
| `percent-to-fstring` | mechanical: `%` to an f-string | **clean** | 272 | 1 | 0 | +0 |
| `loop-to-comprehension` | mechanical: loop to a comprehension | **clean** | 272 | 1 | 0 | -2 |
| `extract-method` | mechanical: extract a helper | **clean** | 272 | 1 | 0 | +3 |
| `hand-rename-locals` | hand: rename every local in one method | **clean** | 272 | 1 | 0 | +0 |
| `hand-restructure-branch` | hand: hoist a default out of if/elif/else | **clean** | 272 | 1 | 0 | -2 |
| `hand-docstring` | hand: reword a docstring | **clean** | 272 | 1 | 0 | +0 |
| `hand-constant-hoist` | hand: hoist a literal to a constant | **clean** | 272 | 1 | 0 | +3 |
| `hand-comment-churn` | hand: comment and blank-line churn | **clean** | 272 | 1 | 0 | +0 |
| `hand-class-alias` | hand: add an alias for an existing class | **clean** | 272 | 1 | 0 | +3 |
| `seeded-bug` | CONTROL: refactor with one real change inside | **different** | 268 | 1 | 4 | +3 |

False positives: **0** (none).
Seeded bug inside a harmless refactor: **caught** on 4 cases — a clean run of twelve refactors means nothing without this control.
