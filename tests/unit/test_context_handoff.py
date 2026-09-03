from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_context_handoff import audit, count_non_whitespace


def test_count_non_whitespace_handles_chinese_and_newlines() -> None:
    assert count_non_whitespace("中文 文档\nabc") == 7


def test_audit_reports_missing_and_oversized_files(tmp_path: Path) -> None:
    policy_dir = tmp_path / "docs" / "handoff"
    policy_dir.mkdir(parents=True)
    policy_path = policy_dir / "context-policy.json"
    policy_path.write_text(
        json.dumps({"required_files": {"short.md": 3, "missing.md": 5}}),
        encoding="utf-8",
    )
    (tmp_path / "short.md").write_text("一二三四", encoding="utf-8")
    rows = audit(policy_path, root=tmp_path)
    assert rows[0].exists is True
    assert rows[0].characters == 4
    assert rows[0].ok is False
    assert rows[1].exists is False
    assert rows[1].ok is False


def test_repository_handoff_package_stays_within_limits() -> None:
    rows = audit()
    assert rows
    assert all(row.ok for row in rows)
