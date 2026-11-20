"""The CI status publisher: counts and skip reasons parsed from JUnit XML."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load() -> ModuleType:
    """Load the script by path: `scripts/` is not a package, and CI is stricter
    about sys.path than a developer shell, so importing it by name collected
    fine locally and failed on the runner."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "ci_status.py"
    spec = importlib.util.spec_from_file_location("ci_status_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_status = _load()

XML = '''<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4" failures="1" errors="0" skipped="2">
  <testcase classname="a" name="ok"/>
  <testcase classname="a" name="bad"><failure message="boom"/></testcase>
  <testcase classname="a" name="skip1">
    <skipped message="needs Docker" type="pytest.skip"/>
  </testcase>
  <testcase classname="a" name="skip2">
    <skipped message="needs Docker" type="pytest.skip"/>
  </testcase>
</testsuite></testsuites>
'''


def test_counts_reports_totals_and_skip_reasons(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(XML, encoding="utf-8")
    totals, failed, reasons = ci_status.counts(path)
    assert totals["collected"] == 4
    assert totals["passed"] == 1
    assert totals["skipped"] == 2
    assert failed == 1
    assert reasons == ["2x needs Docker"]
