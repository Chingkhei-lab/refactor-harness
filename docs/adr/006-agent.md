# ADR 006: the agent loop

**Status:** accepted (Checkpoint 13).
**Evidence, not proof of equivalence.** This ADR decides how a model is allowed to
propose a patch; it does not claim that an accepted patch is behaviour-preserving.
Every acceptance is "nothing we measured changed".

Context: Phases 1–4 built a harness that compares two trees and refuses to call
anything equivalent without a witness. Phase 5 puts a model in front of it. The
bake-off (`docs/results/model-bakeoff-20261122.md`) settled *which* model and
proved that the tool-calling layer works: 639 calls, 100 % valid arguments, zero
malformed JSON. It did not settle anything about safety, durability or honesty, so
those are decided here.

## D1 — The agent proposes; a human approves

No code path applies a candidate patch to the real patch log without an approval
row. The approval stores `sha256(patch)` and `sha256(report)`; changing either
invalidates it. A boolean "approved" would approve whatever the patch became after
the click, which is the failure mode the harness exists to prevent, applied to its
own workflow.

## D2 — Every model call is a committed turn, keyed `(run, turn)`

`llm_calls` has `UNIQUE (run_id, turn)` and stores the request hash, the full
response, provider, model, tokens, latency, attempts and status. The turn is
committed in the same transaction as the workflow step's advance, so "committed"
and "will not be called again" are the same event.

Reason: a model call cannot be repeated exactly. Without persistence, a restart
either loses the run's history or pays for it again with different answers, and a
"resumed" run would be a different run wearing the same id.

## D3 — Replay is checked, not trusted

On resume, a committed turn is replayed only if the request rebuilt now hashes to
the recorded `request_sha256`. A mismatch — prompt version, tool schema, message
history — aborts the run. Reusing a stored response for a different request makes
the log fiction.

## D4 — Effects are derived from committed arguments, not re-performed

A replayed `apply_patch` is re-applied from its committed arguments with its
recorded precondition hashes. Read-only tools may be re-executed; write tools are
derived. If a precondition no longer holds, the run fails rather than continuing on
a tree that disagrees with the model's history.

## D5 — Budgets are data with slack, and a budget is not a test

Per run: model calls, tokens, wall clock, and per-tool call caps. The per-tool cap
is `expected + slack`, `slack = max(2, ceil(expected / 2))`, because the bake-off
showed a hard 4-call cap failing every model on tasks that need five. Exhaustion is
an outcome (`budget_exhausted`), classified and reported, never an exception and
never a silent truncation.

## D6 — Repeated identical calls are detected, not punished immediately

Three identical `(tool, arguments)` calls in a row are refused with a nudge
explaining the repetition; the fourth ends the run as `budget_exhausted` with
`reason="repeat"`. Rationale: one repeat is a legitimate retry after a transient
tool error; a loop of identical calls is the model failing to read its own result,
and the honest response is to stop and say so.

## D7 — Tool output is untrusted data, truncated with a marker

Every tool result is capped (bytes and lines) with an explicit
`…[truncated: N bytes]` marker, and wrapped as `<tool_output untrusted="true">`.
Instructions inside tool output are evidence, not orders. The wrapper is in the
harness so the model cannot be handed text that the harness itself treats as
trusted.

## D8 — The system prompt is versioned and hashed

`PROMPT_VERSION` and `sha256(prompt)` are recorded per run, alongside the model id
and temperature. A run whose prompt hash does not match the deployment refuses to
resume (D3). Without this, "the same run" means two different experiments.

## D9 — The verifier is split into visible and hidden sets

`run_verify` uses the visible set and returns counts plus a few truncated differing
cases. The final gate uses a hidden set with disjoint inputs, never shown and never
writable. Reason: a model that can read every case it is graded on can special-case
inputs, and the resulting patch passes the harness while being worthless on real
code. The gate is the only place a patch is accepted.

## D10 — `give up` is a first-class outcome

A trap task whose correct answer is "this cannot be done safely" must not score as
a failure to produce a patch. `gave_up` carries a reason and is reported alongside
successes, not folded into them.

## D11 — Precision is the headline metric

Among patches the harness accepted, the reported number is how many fail the
upstream suite or the hidden cases. Success rate alone rewards a harness that
accepts everything; precision is the number that punishes it.

## D12 — Guardrails are code with tests, evaluated before the gate

Protected paths, size caps, AST bans (`eval`, `exec`, `compile`, `subprocess`,
`os.system`, sockets, `urllib`, `requests`, `pickle.loads`), secret-shaped
literals, and literals copied from case inputs are refused by function, with the
guardrail's name recorded. A deliberately malicious proposal suite must be rejected
in full; a guardrail tested only on attacks we imagined is not a guardrail.

## D13 — Execution stays inside ADR 003's sandbox

The candidate workspace is copied in as a tar stream into a size-capped tmpfs; the
run phase has no network; no host mount. The agent never executes in the review
process.

## D14 — Data policy: prompts are assumed logged

Only open-source target code is sent. No secrets, no credentials, no private
repositories. Stated in the README and in every report that involves a provider,
because a model provider's retention policy is not something this project can
verify.

## D15 — Model choice is configuration, recorded per run

Primary `nvidia/nemotron-3-ultra-550b-a55b:free` through numes.ai, fallback
`moonshotai/kimi-k3.1-pro:free`, provider fallback `gemini-3.8-flash`. The model id
is stored on every call row, so a mixed-model run is visible rather than averaged
away. Changing the model is a configuration change, not a code change.
