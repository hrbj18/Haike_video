"""Validate the lightweight OpenMontage handoff package."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = REPO_ROOT / "docs" / "handoff" / "context-policy.json"
UPDATED_PATTERN = re.compile(r"更新时间[：:]\s*(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class AuditRow:
    path: str
    limit: int
    characters: int
    exists: bool
    stale_days: int | None

    @property
    def ok(self) -> bool:
        return self.exists and self.characters <= self.limit


def count_non_whitespace(text: str) -> int:
    return sum(1 for character in text if not character.isspace())


def _read_policy(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = payload.get("required_files")
    if not isinstance(required, dict) or not required:
        raise ValueError("context-policy.json is missing required_files")
    return payload


def audit(policy_path: Path = DEFAULT_POLICY, *, root: Path = REPO_ROOT) -> list[AuditRow]:
    policy = _read_policy(policy_path)
    rows: list[AuditRow] = []
    for relative, raw_limit in policy["required_files"].items():
        target = root / relative
        exists = target.is_file()
        text = target.read_text(encoding="utf-8") if exists else ""
        match = UPDATED_PATTERN.search(text)
        stale_days = None
        if match:
            stale_days = (date.today() - date.fromisoformat(match.group(1))).days
        rows.append(AuditRow(
            path=str(relative), limit=int(raw_limit),
            characters=count_non_whitespace(text), exists=exists,
            stale_days=stale_days,
        ))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the OpenMontage lightweight context package")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    rows = audit(args.policy)
    if args.as_json:
        print(json.dumps([row.__dict__ | {"ok": row.ok} for row in rows], ensure_ascii=False, indent=2))
    else:
        for row in rows:
            state = "OK" if row.ok else "FAIL"
            age = f", updated {row.stale_days} day(s) ago" if row.stale_days is not None else ""
            print(f"[{state}] {row.path}: {row.characters}/{row.limit} chars{age}")
    if any(not row.ok for row in rows):
        print("Context handoff audit failed: a required file is missing or oversized.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
