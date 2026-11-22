"""Capture the golden records `run_verify` and the hidden gate compare against.

Two records are captured from the **pristine pinned tree**, once, before any agent
run, and their hashes are written next to them:

* `harness/golden/pycodestyle-visible.json` — the 86 visible cases;
* `harness/golden/pycodestyle-hidden.json` — the 103 hidden cases.

The golden record is a pass, not a guess: it is the same `run_pass` the reports use,
with the same shim, the same budgets and the same case-set hash. After capture the
script times one `HarnessVerifier` call on the pristine tree (expecting zero
differences) and one on a tree with a single deliberate change (expecting at least
one) — a verifier that cannot see a real change is worse than no verifier, because
it says "identical" with confidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent.harness_verify import Golden, HarnessVerifier, golden_from_pass, stage  # noqa: E402
from harness.cases import case_set_hash, load_cases  # noqa: E402
from harness.determinism import write_shim  # noqa: E402
from harness.runner import run_pass  # noqa: E402

TARGETS = {
    "pycodestyle": {
        "tree": REPO_ROOT / ".harness" / "oracle" / "pycodestyle",
        "visible": REPO_ROOT / "harness" / "cases" / "pycodestyle-cli.json",
        "hidden": REPO_ROOT / "harness" / "cases" / "pycodestyle-cli-hidden.json",
        "rules": REPO_ROOT / "harness" / "rules" / "pycodestyle.json",
        "label": "pycodestyle 2.15.0",
    },
    "mccabe": {
        "tree": REPO_ROOT / ".harness" / "oracle" / "mccabe",
        "visible": REPO_ROOT / "harness" / "cases" / "mccabe.json",
        "hidden": REPO_ROOT / "harness" / "cases" / "mccabe-hidden.json",
        "rules": REPO_ROOT / "harness" / "rules" / "mccabe.json",
        "label": "mccabe 0.7.0",
    },
}
GOLDEN = REPO_ROOT / "harness" / "golden"
WORK = REPO_ROOT / ".harness" / "agent-verify"


def capture(cases_path: Path, out: Path, *, label: str, kebab: str = "pycodestyle") -> Golden:
    spec = TARGETS[kebab]
    cases = load_cases(cases_path)
    workdir = WORK / f"golden-{label}"
    pass_ = run_pass(
        python=sys.executable,
        checkout=stage(spec["tree"], WORK / "tree"),
        shim_dir=write_shim(REPO_ROOT / ".harness" / "shim"),
        src_dir=REPO_ROOT / "src",
        target_paths=[WORK / "tree"],
        cases=cases,
        index=1,
        module=kebab,
        workdir=workdir,
        expected_case_set_hash=case_set_hash(cases),
        case_budget_seconds=30.0,
    )
    golden = golden_from_pass(pass_, target=str(spec["label"]), module=kebab, cases=cases)
    golden.save(out)
    print(
        f"{label}: {len(cases)} cases, manifest {golden.manifest_sha256[:12]}, "
        f"set hash {golden.case_set_hash[:12]} -> {out.relative_to(REPO_ROOT)}"
    )
    return golden


def _mutate(copy_of: Path) -> Path:
    """One deliberate behaviour change, so the verifier has something to find.

    `max_line_length = options.max_line_length` becomes `= 79`: every case that
    relies on a non-default limit (or on the reported limit) changes, and the change
    is a contiguous byte splice on one line, not a reformat.
    """
    target = copy_of / "pycodestyle.py"
    text = target.read_text(encoding="utf-8")
    old = "        self.max_line_length = options.max_line_length"
    if old not in text:
        raise SystemExit("the mutation site moved; this probe would prove nothing")
    target.write_text(text.replace(old, "        self.max_line_length = 79", 1), encoding="utf-8")
    return copy_of


def timed(label: str, verifier: HarnessVerifier) -> dict[str, object]:
    started = time.monotonic()
    result = verifier.run(differences=5)
    elapsed = time.monotonic() - started
    print(
        f"{label}: identical={result.identical} different={result.different} "
        f"approved={result.approved} unsupported={result.unsupported} "
        f"in {elapsed:.1f}s"
    )
    return {
        "label": label,
        "seconds": round(elapsed, 2),
        "identical": result.identical,
        "different": result.different,
        "approved": result.approved,
        "unsupported": result.unsupported,
        "sample": [difference.as_dict() for difference in result.differences[:2]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--target", choices=sorted(TARGETS), default="pycodestyle")
    parser.add_argument("--out", default="docs/results/agent-verify-probe.json")
    args = parser.parse_args(argv)

    spec_name = args.target
    spec = TARGETS[spec_name]
    if not Path(spec["tree"]).is_dir():
        raise SystemExit(f"{spec['tree']} missing: fetch the target first")
    GOLDEN.mkdir(parents=True, exist_ok=True)
    visible = capture(Path(spec["visible"]), GOLDEN / f"{spec_name}-visible.json", label="visible",
                      kebab=spec_name)
    hidden = capture(Path(spec["hidden"]), GOLDEN / f"{spec_name}-hidden.json", label="hidden",
                     kebab=spec_name)
    if args.capture_only:
        return 0
    if spec_name != "pycodestyle":
        # The deliberate-change probe below mutates a pycodestyle line; the capture
        # is what the mccabe pilot needs.
        return 0

    cases = load_cases(Path(spec["visible"]))
    pristine = stage(Path(spec["tree"]), WORK / "pristine")
    timings = [timed("pristine (expect 0 differences)", HarnessVerifier(
        golden=Golden.load(GOLDEN / "pycodestyle-visible.json"),
        cases=cases,
        rules_path=Path(spec["rules"]),
        workspace=pristine,
        workdir=WORK / "verify-pristine",
    ))]
    broken = _mutate(stage(Path(spec["tree"]), WORK / "broken"))
    timings.append(timed("one deliberate change (expect >0 differences)", HarnessVerifier(
        golden=Golden.load(GOLDEN / "pycodestyle-visible.json"),
        cases=cases,
        rules_path=Path(spec["rules"]),
        workspace=broken,
        workdir=WORK / "verify-broken",
    )))
    payload = {
        "visible_case_set_hash": visible.case_set_hash,
        "hidden_case_set_hash": hidden.case_set_hash,
        "visible_manifest": visible.manifest_sha256,
        "hidden_manifest": hidden.manifest_sha256,
        "timings": timings,
    }
    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
