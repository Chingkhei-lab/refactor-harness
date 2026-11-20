"""Approved differences: a human said yes to a specific diff, and only that one.

A migration changes some output on purpose — help layout, usage lines, error text.
A normalisation rule is the wrong tool for that: a rule says "these two values are
equivalent", which would hide the very change the migration is about. An approval
says "these two differ, here is the diff I accepted, here is who accepted it".

So an approval is bound to the **content** of the difference:

    {"case_id": "...", "path": "$.value.stdout", "reason": "...",
     "reviewer": "...", "date": "2026-11-20", "sha256": "<of the diff>"}

`apply_approvals` compares that hash with the difference actually observed. A
mismatch is a **stale approval** and the case goes back to being a plain
`different` — an approval cannot follow a change it was not written for. Approvals
are never counted as equivalents, and the file's hash is pinned in every report so
a report can be tied to the approvals it used.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "harness-approvals/1"


class ApprovalError(ValueError):
    """The approvals file is malformed or points at a case that does not exist."""


@dataclass(frozen=True)
class Approval:
    case_id: str
    path: str
    reason: str
    reviewer: str
    date: str
    sha256: str

    def body(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "path": self.path,
            "reason": self.reason,
            "reviewer": self.reviewer,
            "date": self.date,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ApprovalSet:
    target: str
    approvals: tuple[Approval, ...] = ()
    sha256: str = ""
    source: str = ""


@dataclass
class Applied:
    approved: list[dict[str, Any]] = field(default_factory=list)
    stale: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)


def diff_digest(path: str, left: Any, right: Any) -> str:
    """The identity of ONE observed difference: path plus both values."""
    payload = json.dumps(
        {"path": path, "left": left, "right": right},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_approvals(path: Path) -> ApprovalSet:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ApprovalError(f"{path}: schema must be {SCHEMA!r}")
    target = str(payload.get("target") or "")
    if not target:
        raise ApprovalError(f"{path}: target is required")
    approvals: list[Approval] = []
    seen: set[tuple[str, str]] = set()
    for entry in payload.get("approvals", []):
        for field_name in ("case_id", "path", "reason", "reviewer", "date", "sha256"):
            if not str(entry.get(field_name) or "").strip():
                raise ApprovalError(
                    f"{path}: approval {entry!r} is missing {field_name!r}"
                )
        if len(str(entry["reason"])) < 12:
            raise ApprovalError(
                f"{path}: approval for {entry['case_id']} needs a reason, not a word"
            )
        if len(str(entry["sha256"])) != 64 or any(
            ch not in "0123456789abcdef" for ch in str(entry["sha256"])
        ):
            raise ApprovalError(
                f"{path}: approval for {entry['case_id']} has no usable sha256"
            )
        if not str(entry["path"]).startswith("$"):
            raise ApprovalError(f"{path}: approval path must start with '$'")
        key: tuple[str, str] = (str(entry["case_id"]), str(entry["path"]))
        if key in seen:
            raise ApprovalError(f"{path}: duplicate approval for {key}")
        seen.add(key)
        approvals.append(
            Approval(
                case_id=str(entry["case_id"]),
                path=str(entry["path"]),
                reason=str(entry["reason"]),
                reviewer=str(entry["reviewer"]),
                date=str(entry["date"]),
                sha256=str(entry["sha256"]),
            )
        )
    return ApprovalSet(
        target=target,
        approvals=tuple(approvals),
        sha256=hashlib.sha256(raw).hexdigest(),
        source=str(path),
    )


def apply_approvals(
    approvals: ApprovalSet, differences: list[dict[str, Any]]
) -> Applied:
    """Split observed differences into approved, stale and unmatched.

    `differences` are `{"case_id", "path", "left", "right"}` records — one per
    first difference the comparator reported. A matching case and path with a
    matching digest is approved; a matching case and path with a different digest
    is stale (and stays a `different`); an approval that nothing matched is
    reported so a reader can see it has stopped applying.
    """
    index = {(item.case_id, item.path): item for item in approvals.approvals}
    applied = Applied()
    used: set[tuple[str, str]] = set()
    for difference in differences:
        key: tuple[str, str] = (str(difference["case_id"]), str(difference["path"]))
        approval = index.get(key)
        digest = diff_digest(
            str(difference["path"]), difference.get("left"), difference.get("right")
        )
        if approval is None:
            applied.unmatched.append(difference)
            continue
        used.add(key)
        record = {
            "case_id": key[0],
            "path": key[1],
            "reason": approval.reason,
            "reviewer": approval.reviewer,
            "date": approval.date,
            "expected": approval.sha256,
            "observed": digest,
        }
        if digest == approval.sha256:
            applied.approved.append(record)
        else:
            applied.stale.append(record)
    for pair, approval in index.items():
        if pair not in used:
            applied.unmatched.append(
                {
                    "case_id": approval.case_id,
                    "path": approval.path,
                    "reason": approval.reason,
                    "note": "approval matched no observed difference",
                }
            )
    return applied
