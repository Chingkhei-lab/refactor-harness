# v0.2.2 — checkpoint 9 review rounds 1–2

Supersedes [v0.2.1](https://github.com/Chingkhei-lab/refactor-harness/releases/tag/v0.2.1),
whose tree and body disagreed about the concurrency result. This release carries
the corrected documentation, the evidence-durability fix and the CI that reports
its own counts.

## Corrected finding: concurrency

| n | Deadline | Result |
| --- | --- | --- |
| 1…80 | 25 s | clean |
| 96 | **25 s** | 6/96 exit 137 |
| 80, 88, 96 | 60 s | clean (×2 each) |
| 96, 128 | 60 s | clean |

**No memory wall was observed up to n=128 with sleep payloads.** The single
96 failure correlates with the 25 s per-sandbox deadline (p50 24.4 s), not the
3.83 GiB Docker VM; raising the deadline removed it. Real capacity will be
measured later with a memory-touching payload. Residual unknown stated in the
report: a marker-less 137 leaves no readable cgroup counter (ADR 003 D7 r3).

## Evidence durability

Demos and the test suite shared `app_test`, and `clean_db` truncates it — a
`pytest` run destroyed a chaos run's `sandbox_runs` rows. Demos now require
`ENGINE_DEMO_DATABASE_URL`, refuse any `_test` name, and refuse the test target
after host normalisation (`localhost` = `host.docker.internal`).

## CI reports on itself

Both jobs emit JUnit XML and write collected/executed/skipped counts with skip
reasons into the run page summary; the XML is uploaded as an artifact.

## Install network

`InstallLimits.network` / `ENGINE_INSTALL_NETWORK` names the network the one
networked container attaches to, and a hostile test *measures* whether it can
reach the host's Postgres port. Measured on Docker 28.3.2/WSL2: not reachable
(`host.docker.internal` does not resolve, gateway times out, localhost refused).

## Also in this release

- Install-phase mounts and `commit()` semantics documented in the results report.
- Design-doc growth/GC item covers snapshot/patch blobs, terminal workflows and
  install images.
- `v0.2.1` is superseded; do not cite its concurrency line.
