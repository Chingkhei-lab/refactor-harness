# v0.4 — the guardrail fix and the first evaluation pass

**Supersedes `v0.3.1`.** This release closes a guardrail defect, makes the token
budget a hard stop, records the model's own finish on a reserved call as its own
outcome, and reports the first end-to-end evaluation. It does **not** ship an
approval UI; that remains specified in `docs/design-agent-approvals.md`.

If you read one thing: **the guardrail judged the whole patch, not the added lines.**
A patch could add a harmless line and delete a protected one, and the dangerous
addition/removal pair was not compared. The guardrail now judges the added lines,
parses the candidate with `ast.parse`/`compile` rather than importing it, and runs
the per-kind dangerous-call differential on unparseable fragments so a syntax-error
candidate cannot hide a call.

## What changed

- **Guardrails on added lines only**, with a differential refusal for parse failures.
  The name-based dangerous-call check is documented as evadable; it is a speed bump,
  not the boundary. The AST validity check catches malformed patches before they
  reach the target.
- **The token cap is hard.** A call whose projected prompt would cross the budget is
  refused and recorded as a forced `give_up` (`budget_exhausted`). It is checked
  during wrap-up too. The soft version was measured at **436 572 tokens against a
  120 000 cap** on a pycodestyle migration; the first hard version still leaked to
  **367 865** because it skipped the check inside wrap-up. Both runs are kept in
  `docs/results/agent-wiring-20261124-prefix3.jsonl` and the invalid evaluation
  attempts.
- **`finished_open_difference` is its own outcome.** When the model's `finish` is
  accepted on the reserved call while `run_verify` still reports a difference, the
  run is not folded into `accepted` or `budget_exhausted`. It succeeds only if the
  hidden gate passes **and** the difference is inside a declared allowed pattern; a
  failing gate is a failure. A test pins that.
- **The trap rule is explicit.** A trap that the model patches and finishes is a
  trap failure even if the gate catches the patch: the gate is safety evidence, not
  task success. A forced `give_up` without a patch is a trap success. The wiring
  run refused at 75 312 tokens and is recorded as a success on the trap.
- **A task instruction was wrong.** `mech-rename-local` named a `graph` local inside
  `PathGraphingAstVisitor.visit`; neither exists. The wiring model patched something
  else that preserved behaviour and the gate passed. The task now names `name` in
  `visitWith`, the local the earlier pilots renamed.

## First evaluation

Full report: `docs/results/agent-eval-20261124.md`.

| measure | value |
| --- | --- |
| runs completed | **14 of 24** (seven tasks, both rounds) |
| accepted | **4** |
| `budget_exhausted` | 8 |
| `gate_rejected` | 2 |
| tokens | 1 593 630 (mean 113 831; largest 197 457) |
| wall clock | 3 141 s |
| per-run cap | 200 000, hard (evaluation override of 120 000) |

Three budget-exhausted runs had a **passing hidden gate**: both
`mig-manual-argparse` runs and `idiom-fstring#1`. The patch was correct but no
terminal submission was accepted before the context filled. The table keeps the gate
result next to the classification; calling those runs successes would hide the
protocol failure.

The gate rejected two patches: `idiom-with-open#2` differed outside the allowed
patterns while the visible suite passed, and `mech-dead-branch#2` failed the oracle
suite. Five tasks, including both traps, were not reached before the total cap, so
this is a coverage result, not a precision number.

## Reproduce

```
python scripts/agent_eval.py --validate
python scripts/agent_eval.py --run --out docs/results/agent-eval.jsonl
python scripts/agent_freeze.py --check docs/results/agent-freeze-20261124.json
```

Freeze: `d8332a00980b4348e57c3147ffd95191e2264b3e155badd58241d8cd541a7f89`
(`d41ef8a`). The evaluation runner is `scripts/agent_eval.py`; the task set hash is
`aa1a3d091b7c2b61499bbb60d65ef13cef2371651edb31f6e4a1bc31f6e807ca`.