# v0.3 — superseded by v0.3.1

> **Superseded by [v0.3.1](https://github.com/Chingkhei-lab/refactor-harness/releases/tag/v0.3.1).**
> Its stage-2 mutation result — 100 of 100 mutants killed by both oracles — is
> wrong. The mutator serialised the whole module with `ast.unparse()` after a
> one-node edit, so every mutant rewrote all 2 717 lines of `pycodestyle.py` into
> 2 095, and two CLI cases that print a traceback and a warning naming the target's
> file and line differed for 77 and 75 of the 100. The corrected figures are
> **harness 62/100 (62.0 %)** and **upstream suite 74/100 (74.0 %)**. The tag is not
> moved; the rest of checkpoint 12 stands.
>
> Why: [`docs/results/harness-20261122.md`](https://github.com/Chingkhei-lab/refactor-harness/blob/main/docs/results/harness-20261122.md) ·
> audit: [`docs/results/checkpoint12b-audit-20261121.md`](https://github.com/Chingkhei-lab/refactor-harness/blob/main/docs/results/checkpoint12b-audit-20261121.md)

Checkpoint 12, as published: the CLI observation layer, approvals bound to the
sha256 of the observed diff, the `optparse` → `argparse` migration of
`pycodestyle` 2.15.0, and the cross-target report
([`docs/results/harness-20261120.md`](https://github.com/Chingkhei-lab/refactor-harness/blob/main/docs/results/harness-20261120.md)).

Everything in that release except the stage-2 mutation figure and the timing
paragraph is unchanged by the correction, and both are re-measured in v0.3.1.
