"""`run_verify` wired to a real harness run (Checkpoint 15).

The design (ADR 006 D2, D20; design §6) says a verify is a *candidate run compared
against a golden record*, and that is all it is here:

* the golden observations are captured once, from a pristine tree, and pinned with
  the case-set hash — so a verify cannot be scored against a different set of cases
  than the one the report describes;
* a verify stages the candidate workspace to a fixed path, runs **one** pass over
  the **visible** cases, and compares that pass against the golden record with the
  project's own comparator, rules and approvals;
* there is **no alternating old/new protocol** here: that protocol exists to give a
  difference a witness, and it costs two containers per round. It is used for the
  final gate, where a wrong number is expensive, not for the eight calls a model
  makes while it is still working.

Cases that varied in the A/A baseline are **quarantined**: a case that is not stable
against itself cannot be evidence about a patch, so it is compared but never counted
as a difference.

Timing is measured per call and reported, because the verify budget is a wall-clock
decision: at ~45 s a pass, eight calls is ~6 minutes of a run's budget.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.cases import Case, case_set_hash
from harness.compare import CLASS_DIFFERENT, compare_sets
from harness.determinism import write_shim
from harness.observation import Observation
from harness.rules import load_rules
from harness.runner import Pass, run_pass

from .verify import CaseDifference, VerifyResult

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Golden:
    """A pinned baseline: observations per case, and the set they were made from."""

    target: str
    module: str
    case_set_hash: str
    digests: dict[str, str]
    observations: dict[str, dict[str, Any]]
    path: Path | None = None

    @property
    def manifest_sha256(self) -> str:
        payload = json.dumps(
            {"case_set_hash": self.case_set_hash, "digests": self.digests}, sort_keys=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "agent-golden/1",
                    "target": self.target,
                    "module": self.module,
                    "case_set_hash": self.case_set_hash,
                    "digests": self.digests,
                    "observations": self.observations,
                },
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Golden:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != "agent-golden/1":
            raise ValueError(f"{path}: not a golden record")
        return cls(
            target=payload["target"],
            module=payload["module"],
            case_set_hash=payload["case_set_hash"],
            digests=payload["digests"],
            observations=payload["observations"],
            path=path,
        )


def stage(tree: Path, dest: Path) -> Path:
    """Copy a candidate tree to one fixed path.

    Fixed, not unique: a case that prints its own path must print the same text as
    the golden run, or every verify reports a difference about a directory name.
    The copy is replaced wholesale each call, so a deleted file is really gone.
    """
    from harness.child import _rmtree_hard

    _rmtree_hard(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tree, dest, ignore=shutil.ignore_patterns("__pycache__", ".git", ".harness"))
    return dest


def golden_from_pass(pass_: Pass, *, target: str, module: str, cases: list[Case]) -> Golden:
    from harness.runner import digest_map

    return Golden(
        target=target,
        module=module,
        case_set_hash=case_set_hash(cases),
        digests=digest_map(pass_),
        observations={
            case_id: dict(observation.body()) for case_id, observation in pass_.by_case().items()
        },
    )


@dataclass
class HarnessVerifier:
    """One verify call: run the visible cases once, compare with the golden record."""

    golden: Golden
    cases: list[Case]
    rules_path: Path
    workspace: Path
    workdir: Path
    target_paths: list[Path] = field(default_factory=list)
    python: str = sys.executable
    case_budget_seconds: float = 30.0
    cpu_budget_seconds: float = 0.0
    timings: list[float] = field(default_factory=list)
    calls: int = 0

    def __post_init__(self) -> None:
        computed = case_set_hash(self.cases)
        if computed != self.golden.case_set_hash:
            raise ValueError(
                "the golden record was made from a different case set: "
                f"golden {self.golden.case_set_hash[:12]}, cases {computed[:12]}. "
                "Refusing to compare a candidate against a baseline it does not match."
            )
        self.rules = load_rules(self.rules_path, case_set_hash=computed)

    def run(self, *, differences: int = 5) -> VerifyResult:
        started = time.monotonic()
        self.calls += 1
        index = 1000 + self.calls
        staged = stage(self.workspace, self.workdir / "tree")
        pass_ = run_pass(
            python=self.python,
            checkout=staged,
            shim_dir=write_shim(REPO_ROOT / ".harness" / "shim"),
            src_dir=REPO_ROOT / "src",
            target_paths=self.target_paths or [staged],
            cases=self.cases,
            index=index,
            module=self.golden.module,
            workdir=self.workdir / f"call-{self.calls}",
            expected_case_set_hash=self.golden.case_set_hash,
            case_budget_seconds=self.case_budget_seconds,
            cpu_budget_seconds=self.cpu_budget_seconds,
        )
        golden_observations = {
            case_id: Observation.from_body(body)
            for case_id, body in self.golden.observations.items()
        }
        verdicts = compare_sets(golden_observations, pass_.by_case(), self.rules)

        identical = different = approved = unsupported = 0
        shown: list[CaseDifference] = []
        for case_id in sorted(verdicts):
            verdict = verdicts[case_id]
            kind = verdict.report_class
            if kind in ("identical", "equivalent"):
                identical += 1
            elif kind == CLASS_DIFFERENT:
                different += 1
                if len(shown) < differences:
                    shown.append(
                        CaseDifference(
                            case_id=case_id,
                            entry=self.golden.module,
                            path=verdict.path or "$",
                            left=json.dumps(verdict.left, sort_keys=True, default=str),
                            right=json.dumps(verdict.right, sort_keys=True, default=str),
                        )
                    )
            elif kind == "unsupported":
                unsupported += 1
            else:
                approved += 1
        self.timings.append(time.monotonic() - started)
        return VerifyResult(
            identical=identical,
            different=different,
            approved=approved,
            unsupported=unsupported,
            differences=tuple(shown),
            truncated=different > len(shown),
        )

    @property
    def seconds_per_call(self) -> float:
        return sum(self.timings) / len(self.timings) if self.timings else 0.0
