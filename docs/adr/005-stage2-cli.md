# ADR 005 — Stage 2: a real CLI target, observed as a CLI

## Status
Accepted (checkpoint 12b, design fixed before the target runs).

## Context

Stage 1 (`parse` 1.22.4) is a library observed through function calls. It answered
"can the harness see a behaviour change at all"; it cannot answer "can it see one
in a program a user runs". Stage 2 needs a target with a command line, config
files, exit codes and written output, and a migration that deliberately changes
some of that output.

Verified from the released sdists (not from memory):

| | `pycodestyle` 2.15.0 | `pyflakes` 4.1.0 | `mccabe` 0.7.0 |
| --- | --- | --- | --- |
| License | MIT | MIT | MIT (Expat) |
| Production lines | 2 717 (single module) | 2 956 (package) | 346 (single module) |
| Runtime deps | none | none | none |
| Option parser | `optparse` | `argparse` | `optparse` |

`pycodestyle` is the stage-2 target: one module, no dependencies, `optparse`, and
it reads user and project configuration, which forces the environment controls to
be real rather than decorative. `mccabe` is the cheap second migration (346 lines,
also `optparse`). `pyflakes` already uses `argparse`, so it is a control, not a
migration.

## Decisions

**D1 — Two entry kinds, both observed.** A case names its `entry` kind:

- `cli`: argv, stdin, files copied into a fixed working directory, exit code,
  stdout, stderr, and a manifest of written files (relative path, size, sha256).
- `api`: a named library entry (`Checker(path)` plus `.check_all()`,
  `StyleGuide.check_files`), observed the same way as a stage-1 case.

The CLI form is the one that matters for a migration, because the migration is
about argument handling. The API form keeps the library contract in view: a
migration that fixes the parser but breaks `StyleGuide` is still a regression.

**D2 — The public parser object is in scope.** `pycodestyle.get_parser()` is public
and documented, and third-party code calls it to add options. An
`optparse`-to-`argparse` migration necessarily changes its return type, so:

- the migration **does** touch `get_parser`, and it is observed through the `api`
  entry (`get_parser().parse_args([...])` for a fixed argv, plus `--help` text
  through the CLI entry);
- the change is a **declared** one, not an accidental one: every observable
  difference in `--help` layout, usage lines, option ordering and unknown-option
  error text must be either approved (D5) or reported as a regression;
- a case that calls `get_parser()` and then a `.parse_args` that only `argparse`
  provides would be testing the implementation, not the behaviour, so the cases
  call it through a small adapter that works on either parser type. The adapter is
  part of the *target tree* under test, not of the harness, and it is the same
  file in both versions — otherwise the comparison would be measuring the adapter.

**D3 — Case inputs are the sdist's own fixtures, inputs only.** Source files are
seeded from `pycodestyle`'s testsuite fixtures: the file contents, never the
annotated expected codes. The expected codes are the upstream suite's oracle; using
them as case inputs would import the answer key into the harness, which is exactly
what stage 1 avoided with `parse`.

**D4 — The environment is controlled per case.** `HOME` and `XDG_CONFIG_HOME` point
at an empty directory; the project config (`setup.cfg`, `tox.ini`) is fixed to the
case's working directory; `COLUMNS`, `LANG`, `PYTHONIOENCODING` are fixed; stdin is
`/dev/null`; there is no TTY; `--jobs` is never greater than 1. Absolute paths and
temp directories are normalised by declared rules, not by a blanket rule on every
string.

**D5 — Approved differences are a separate, hash-pinned channel.** A migration
changes some output on purpose. A normalisation rule says "these two values are
equivalent", which would hide the very change the migration is about. So
`harness/approvals/<target>.json` holds one entry per case and JSON path:

```json
{"case_id": "0042-cli-help", "path": "$.stdout", "reason": "...",
 "reviewer": "...", "date": "2026-11-20", "sha256": "..."}
```

Approved differences are reported in their own class `different (approved)` and are
**never** counted as `equivalent`. The file's hash is pinned in every report, like
the rules file. An approval whose diff no longer matches (same case, different
paths or different values) is a `different` again — an approval cannot silently
follow a change.

**D6 — Known nondeterminism is declared, not tuned away.** From the source:
`--benchmark` prints elapsed times, so those cases are `expect: unstable` with a
declared rule for the timing fields (and the report says the rule exists);
`--jobs > 1` can reorder output, so any case that uses it is `expect: unordered`
or `unsupported` with the reason stated. Neither is allowed to become a
`different` that blames the migration.

**D7 — Mutation on stage 2 uses the stage-1 discipline, smaller.** Fixed seed, a
few hundred mutants, the plan written before results, the upstream testsuite as a
second oracle, and the same three-way reading (first pass, tuned-on-the-same
mutants, held-out) if the case set is tuned at all.

## Consequences

- The case file grows a `kind` field and a `files` map; the observation body grows
  `argv/exit/stdout/stderr/files` for CLI cases. Both are schema-versioned, so
  stage-1 blobs stay readable.
- The harness must run inside the sandbox for stage 2 (ADR 004 D10k): a CLI that
  reads `HOME` and writes files is exactly the case where "it ran on the host" is
  not acceptable.
- The migration itself is written by hand as the change under test, and the report
  lists the approved differences, the unapproved ones, and at least one real
  behaviour change that was not intended — or says plainly that none was found.
