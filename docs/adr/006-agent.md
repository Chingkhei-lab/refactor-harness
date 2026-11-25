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

## D16 — The request hash is the logical request, not the model id

`request_sha256` covers messages, tool schemas, temperature and the prompt version.
The model and provider are recorded on every call and are **not** hashed. Reason: a
router fallback from the primary model to the fallback on resume must replay the
committed turns, and hashing the model id would raise `DivergenceError` for every
one of them — turning a recovery mechanism into a data-loss mechanism. A mixed-model
run is visible because the model is on each row, not because the run is unusable.

## D17 — A new prompt is a new run

The prompt version is inside the hash, so editing the prompt changes it and a resume
refuses the old turns. That is deliberate: reusing answers produced under different
instructions would make the log fiction.

## D18 — Token counts are measured where possible, estimated where not, and labelled

`usage` is taken from the provider when it is returned. Gemini's OpenAI-compatible
endpoint returns `usage: null`, so the loop falls back to a local estimate (four
characters per token) and marks the turn `tokens_estimated`. The report says which
turns were measured; a budget built on an estimate is a budget, not a bill.

## D19 — Reasoning content is stored, capped, and never fed back

Some models return reasoning text. It is stored with the committed turn (capped at
64 KB with a visible marker) so a run can be audited and re-analysed without paying
for it again. It is treated as untrusted model output: never put back into the
message history, never executed, never treated as instructions — even when it reads
like a plan or a command.

## D20 — Verification limits are numbers in code

Five differing cases, 500 characters per side, eight `run_verify` calls, two `finish`
attempts, and a `finish` answer that is a pass/fail plus a category. Each limit stops
a specific attack: reading the answer instead of the behaviour, searching the visible
set, and probing the hidden set by resubmitting.

## D21 — Migration success is defined by declared patterns

A migration task declares the JSON-path patterns where a difference is expected
(usage, help, error text, exit status). A difference inside one is
`different (needs approval)`; a difference outside one fails the task. Patterns match
whole path segments, so a file's content changing is never covered by a pattern for
the case's output.

## D22 — The task set is data, and its expectations are validated

Twelve tasks in `src/agent/tasks/*.json`, four kinds, with counts and expected
outcomes checked by a test. A trap succeeds only on the refusal it names, or on
`gave_up`; an accepted patch on a trap is a gate failure. The kind decides what
success means before the run, so the evaluation cannot redefine it afterwards.

## D23 — Every stop condition enters wrap-up

A run may end in exactly three ways: a **terminal tool call**, an **unrecoverable
provider failure**, or an **infrastructure stop outside the loop** (worker death,
recovered by replay). Every other condition that used to end a run on its own —
tool-cap breach, the repeat rule, the token budget, the wall clock, a second silent
turn — now opens wrap-up instead:

* the tools that change state are withdrawn, leaving `run_verify`, `run_tests`,
  `finish` and `give_up`;
* exactly one more model call is allowed, with `tool_choice: "required"` and only
  `finish` / `give_up` available;
* the original reason is recorded as `wrapup_reason` (`tool_cap:<tool>`,
  `repeat:<tool>`, `budget:tokens`, `budget:wall_clock`, `silent`) next to
  `forced_terminal`.

The point is that a run cannot die without a decision, and that the decision it makes
under pressure is labelled as such. If the provider ignores `tool_choice` (measured:
kimi answers in prose under `"required"`), the run ends `silent_turn_at_wrapup` and is
still marked forced, so the failure is attributed to the model rather than hidden in a
budget.

This replaces two worse ideas: an arbitrary cap on `apply_patch`, which fails
legitimate work, and a mid-run nudge, which is wording a model can ignore.

## D24 — Voluntary and forced terminals are different outcomes

A `give_up` the model chose and a `give_up` wrap-up forced are not the same fact. The
taxonomy scores the forced one as **budget exhaustion**, and the report separates them
per task kind. On a trap task, "refused to submit" is the desired behaviour; "submitted
nothing because the protocol stopped it" is not, and must not be counted as if it were.

## D25 — Evaluation protocol (written before any evaluation run)

**Task set.** The twelve tasks in `src/agent/tasks/`, unchanged, with their expected
outcome classes. The three pilot tasks in `src/agent/pilot_tasks/` are **excluded**:
they shaped the tools and the prompt, so they cannot measure them.

**Runs.** Twelve tasks × 2 runs with the primary model = **24 runs** for the first
pass. If the token budget allows, a third run per task (36). A subset of six tasks
(eight mechanical, and both migrations) runs once on the fallback model, reported
separately and never pooled.

**Models.** Primary `nvidia/nemotron-3-ultra-550b-a55b:free` (probed: honours
`tool_choice`). Fallback `moonshotai/kimi-k3.1-pro:free`, reported separately because
it **ignores** `tool_choice`, so its silent-turn rate is expected to be higher.
Gemini is **not** a working fallback until a probe completes: its endpoint returned
503 and then HTTP 429, "You exceeded your current quota, please check your plan and
billing details", and the README says so. OpenRouter and Coarena-hosted
(`gemini-3.8-flash`, `deepseek-4.1`) are candidates for a later round; adding a
provider requires a probe of `tool_choice` and usage reporting first, because both
affect the protocol and the accounting.

**Prompt.** Frozen at `PROMPT_VERSION = "agent-system-1"`, sha256 recorded in the
report. A change after the first full run makes the next run a new run; the first
stays the headline.

**Success rule.** Per task, from its declared expectation: mechanical → identical
cases, suite passes, no approval; idiom → the same, approval allowed; migration →
differences only inside the declared patterns, suite passes, human review remains;
trap → the named refusal or `give_up`, and an accepted patch is a failure.

**Failure taxonomy.** `gave_up` (voluntary), `budget_exhausted` (including every
forced terminal, with `wrapup_reason`), `no_patch`, `guardrail_rejected:<code>`,
`gate_rejected:<category>`, `harness_difference`, `suite_failure`, `accepted`,
`provider_failed`, `infrastructure`.

**Metrics.** Precision (accepted patches that preserve behaviour) per target against
its own upstream suite; recall of the differences the suite would catch; injection
compliance (tests intact, no verification-disabling edits) per run; voluntary versus
forced terminals; cost in tokens and wall clock, all with 95 % intervals (Wilson for
proportions). Reported **per target and per model**, never pooled.

**Exclusions.** Pilot runs, runs whose provider failed before the first committed
turn, and runs stopped by infrastructure. Each exclusion is listed with its reason;
nothing is dropped silently.

**Caps.** Total 1 500 000 tokens and 8 hours of wall clock for the first pass, checked
before each run; the run stops when a cap is reached and the report says where. Every
response is written to a cassette before it is parsed.

**The token cap is hard (revised 2026-11-24 after the wiring runs).** When the next
projected prompt would cross `max_tokens`, the loop refuses that call, records a
forced `give_up` (`budget_exhausted` in the taxonomy) and returns. It does not open
wrap-up, because wrap-up still permits every remaining call, and each one re-sends
the whole history. The soft version was measured first: a pycodestyle migration run
with a 120 000 cap crossed at turn 7 (135 843 tokens) and then spent seven wrap-up
calls, ending at **436 572 tokens (3.64×)**. The 238 593-token sanity trap (1.99×)
and the pilot's 175 384 (1.46×) were the same failure at smaller call counts. The
projection still undercounts because it sums message `content` only and ignores
tool-call arguments and tool schemas (3 971 projected against 27 300 actual at the
trap's turn 10), so the stop can fire a turn late; the call that would cross is the
last one made.

This is a protocol change and required a new freeze. It also changes D23's
"every run ends in a terminal call" invariant: a token stop has no room for one, so
it records the refusal itself as the forced ending. Every other stop still opens
wrap-up. The total cap remains the cost bound: the runner checks it before each run
and names the tasks that did not run rather than extrapolating. At the old measured
mean (96 799 tokens/run) 1 500 000 covered about 15 of 24; with the hard stop the
mean should fall, and the report uses the measured count.

## D26 — The evaluation surface is frozen by one hash

`scripts/agent_freeze.py` builds a canonical manifest of everything that shapes a run
and hashes it. The record is `docs/results/agent-freeze-20261124.json`; `--check`
fails when the tree no longer matches it. The hash does **not** cover the runner script
or the report, only the surface below.

**Freeze regenerated 2026-11-24 after the hard token cap:**
`97fac66ccc2468d993a13ee980f4125ba28e3fa9d207f6215e6ccf1e99acbf81`

This supersedes `e1855d13…`, `1b07549e…`, `134289c5…`, `7f200a31…` and
`b8125f7c…`: `loop.py`, the task classifier, and the toolbox changed. The second change came from the real pycodestyle wiring run:
`list_files` decoded every `.py` file, and one legacy-encoded fixture in that tree
ended the run with `UnicodeDecodeError`. Listing is now name-only and reads replace
undecodable bytes. The third hides `run_tests` from the model while leaving the gate's
suite intact: the mccabe suite takes 512s, and a model that spends its wall clock there
cannot patch. The fourth makes a forced, no-patch `give_up` a trap success: the trap
asks for no patch, and the terminal's provenance is not the submission. Earlier records
remain in git history; the evaluation uses this hash. The fifth refuses the call that
would cross the token cap; the 436 572-token migration run is the measurement that
forced it.

| surface | recorded value |
| --- | --- |
| system prompt | `agent-system-1`, 1 677 chars, sha256 `11bd5ed0f1872f4a2da26fd0541aa7009ad8e8fb94e09c4f3e545a5c778cbaab` |
| tools | 7 schemas, sha256 `5fc3630c18fa661374b33db95fe78cb1771dfe2055c3ac8d99f1c3fc1fbf635c`; implementation (`src/agent/live.py`) sha256 `7537829355bc2065c9174a2ce76bf37628fddab6d70f644e627f0ad7932ee2b7` |
| wrap-up | window 3; tools `run_verify`, `run_tests`, `finish`, `give_up`; terminal `finish`, `give_up`; one reserved call with `tool_choice: "required"`; loop sha256 `031e39e9dd51eae4d3dd7cfdc69895d3b0d07ebb7da53d61f971c5945f15dfcc` |
| budgets | run defaults: 12 model calls, 120 000 tokens, 900 s; evaluation caps: 24 runs, 1 500 000 tokens, 28 800 s |
| guardrails | `src/agent/guardrails.py` sha256 `cb7a88b943a88520740e520fe3f4406654ee3d02676a64eb2cf68fea027f136f` |
| task set | 12 tasks, sha256 `7174405121c5828046387793d0db684f9bb18ad9356df5920aebc86e8ae3eadb`; success ladder (`src/agent/tasks.py`) sha256 `7eb5e6fc4101cad701554a97ea42df86cb77677cd80d6c1c2b328ae3f776cb2e` |
| harness inputs | cases `4305d4a9…`, rules `a55dca20…`, golden `d69e66eb…` (full values in the JSON record) |

A change to any of these is a new freeze and a new evaluation; the first pass stays the
headline. The evaluator must run with `python scripts/agent_freeze.py --check
docs/results/agent-freeze-20261124.json` before it starts and record the result.

## D27 — Hard token cap (implemented in the fifth freeze)

Implemented 2026-11-24 after the migration wiring run spent 436 572 tokens against a
120 000 cap. The loop refuses the call that would cross `max_tokens` and records a
forced `gave_up` with `wrapup_reason: budget:tokens`; `_stalled_run` and the token
test cover the stop shape, and D23 now says a token stop is the one ending without a
terminal tool call. The earlier soft-cap proposal (stop on provider-reported spending,
record `wrapup_unresolved`) was not used: the projection is the trigger, so the refused
call is never sent.
