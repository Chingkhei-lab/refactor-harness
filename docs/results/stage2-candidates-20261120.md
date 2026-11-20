# Stage 2 candidates: verified from the sdists, not from memory

Every fact below was read out of the released sdist (downloaded from PyPI, unpacked
under `.harness/candidates/`), not recalled. Line counts exclude test code unless
stated. "Runtime deps" is `info.requires_dist` from the PyPI metadata, which is
`None` for all three.

| | `pycodestyle` 2.15.0 | `pyflakes` 4.1.0 | `mccabe` 0.7.0 |
| --- | --- | --- | --- |
| License | MIT (`LICENSE`, "Permission is hereby granted, free of charge…") | MIT (`LICENSE`, Divmod/Florent Xicluna) | MIT (Expat, `LICENSE`) |
| Layout | **single module**: `pycodestyle/pycodestyle.py` | package: `pyflakes/{api,checker,messages,reporter,script}.py` | **single module**: `mccabe/mccabe.py` |
| Production lines | **2 717** (one file) | **2 956** (excluding `flakes/test/`) | **346** |
| Runtime deps | none | none | none |
| Needs Python | >= 3.10 | >= 3.11 | >= 3.6 |
| Option parser | **`optparse`** (`from optparse import OptionParser`, `parser = OptionParser(...)` at line 2 484) | **`argparse`** already (`argparse.ArgumentParser` in `api.py`) | **`optparse`** (`optparse.OptionParser()` at line 318) |
| Tests shipped in sdist | yes (`testsuite.py`, per-check test modules) | yes (15 modules, 7 233 lines, 568 test functions) | yes (`test_mccabe.py`, 16 test functions) |
| CLI surface | `pycodestyle [opts] paths…`, exit 0/1 on findings, reads `setup.cfg`/`tox.ini` config | `pyflakes [opts] paths…`, exit 1 on findings | `mccabe [opts] files…`, `--min-complexity`, exit code, JSON/printer output |

## What each one is good for

**`pycodestyle` — the recommendation for the 12b demo.** It reads user config
from `~/.pycodestyle`, `$XDG_CONFIG_HOME/pycodestyle` and project config from
`setup.cfg`/`tox.ini` in the working directory (lines 71-80), which is precisely
why the case's `HOME`, `XDG_CONFIG_HOME` and working directory have to be fixed. A single 2 717-line
module, MIT, no runtime deps, and it parses options with `optparse`, so an
optparse-to-argparse migration is a *real* one on real code. It also writes
nothing, reads `setup.cfg`/`tox.ini` from the working directory and honours
`HOME`/`XDG_CONFIG_HOME`, which exercises exactly the environment controls the 12b
design calls for. Its own test suite is large enough to be a second oracle.

**`pyflakes` — the control, not the demo.** It already uses `argparse`, so it
cannot demonstrate the migration, but it is the strongest candidate for a pure
"does the harness agree with a mature suite" comparison: the same size class as
`parse` but with a 7 233-line, 568-test suite. Useful as a stage-2 second target
*after* the CLI observation layer exists (`--select` output on files, exit codes).

**`mccabe` — the smallest possible CLI.** 346 lines, one module, `optparse`. Good
for validating the CLI observation layer cheaply (argv, stdin, exit code, stdout)
and for a second optparse migration with an output format (`--json`) that a
migration may legitimately change. Too small to say much about kill rates.

## Why this matters for the harness

An optparse-to-argparse migration changes `--help` layout, the program name in
usage lines, and the wording of error messages for unknown options. Those changes
are **intended**, and a normalization rule is the wrong tool for them: a rule says
"these two values are equivalent", which would hide the very change the refactor
is about. 12b therefore adds a separate, hash-pinned `harness/approvals/<target>.json`
where each entry names a case and a JSON path, with a reason, reviewer and date;
approved differences are reported in their own class (`different (approved)`) and
never counted as equivalent. An approval whose diff has changed is a `different`
again.

## Verified commands

```text
curl -sS https://pypi.org/pypi/pycodestyle/json   # info.license, info.requires_dist, urls
find pycodestyle -name '*.py' -not -path '*/tests/*' | xargs wc -l
grep -n 'optparse\|argparse' pycodestyle/pycodestyle.py
```

The PyPI JSON is the authority for license and dependency metadata here; the
`LICENSE` file in each sdist was read to confirm the classifier is not stale.
