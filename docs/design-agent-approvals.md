# Agent approvals and review UI — specification

Status: specified 2026-11-24; not implemented. This file is the source of truth for
the approval flow, so a later session does not have to reconstruct it.

## Data

`agent_approvals`:

| column | type | rule |
| --- | --- | --- |
| `run_id` | uuid, primary key | one decision per run; the key makes double submit a no-op |
| `decision` | text | `approved`, `rejected`, `retry` |
| `reviewer` | text | who decided |
| `reason` | text | required for `rejected` and `retry` |
| `patch_hash` | text | the hash the form displayed |
| `report_hash` | text | the report the form displayed |
| `gate_hash` | text | the hidden-gate result the form displayed |
| `decided_at` | timestamptz | server clock |

## Workflow

1. A run ends in a **waiting** step with no job. The reconciler must leave that step
   alone; it only reconciles runs that hold a job. A test pins this.
2. A waiting run **expires after 7 days** without a decision; expiry is a separate
   terminal state and is recorded, not deleted.
3. The review form carries the three hashes it displayed. On submit, the server
   compares them with the current values and rejects the decision as **stale** if any
   differs. No partial apply.
4. One transaction applies the decision:
   - `approved`: append to the patch log by compare-and-swap with the run id as
     `run_ref`. The CAS makes a retried apply idempotent: the same run id cannot
     append twice.
   - `rejected`: record the reason, no patch.
   - `retry`: create a **new run** (new run id); never replay the old one.
5. Concurrent approve and reject: the unique `run_id` means exactly one insert wins;
   the loser gets a conflict, not a second decision.

## Review UI

Pages:

- **Run list**: task, target, model, outcome, taxonomy class, tokens, wall clock,
  wrap-up flags, decision state, waiting age.
- **Run detail**: outcome; `forced_terminal` and `wrapup_reason`;
  `finished_open_difference` shown prominently (never as a success); hidden-gate
  category; harness report; guardrail refusals; timeline of turns with per-turn
  prompt/completion tokens and latency.
- **Diff**: the patch, rendered as plain text in `<pre>`, with sizes.
- **Actions**: approve, reject with a reason, retry.

## Safety

Everything displayed — task instruction, transcript, tool output, diff — is untrusted.

- Template autoescape on. Diffs are text inside `<pre>`; no HTML rendering.
- Large payloads are truncated with their original byte and line counts.
- Strict Content-Security-Policy: no inline script, no CDN; htmx served from this app.
- CSRF token on every POST.
- The server binds to `127.0.0.1` only.

## Tests

- A `<script>` and an `<img onerror=...>` payload in a diff, a transcript, a tool
  output and a task instruction render inert.
- Stale approval: change the patch after the form was rendered, submit the old hashes,
  get a refusal and no append.
- Double submit: two identical approvals, one row, one append.
- Concurrent approve and reject: exactly one row; the other is a conflict.
- The reconciler ignores a waiting run.
- Apply is idempotent on retry (same `run_ref` does not append twice).
- Expiry at 7 days.

## Ordering

This is built after the first evaluation is running: it must not change the prompt,
tools, budgets or guardrails inside a freeze.
