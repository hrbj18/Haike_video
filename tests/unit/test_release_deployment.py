from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_windows_deployment_contract_is_routed_and_reproducible() -> None:
    deployment = ROOT / "docs" / "DEPLOYMENT_WINDOWS_ZH-CN.md"
    handoff = ROOT / "docs" / "handoff" / "DEPLOYMENT.md"
    policy = json.loads((ROOT / "docs" / "handoff" / "context-policy.json").read_text(encoding="utf-8"))
    setup = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")

    assert deployment.is_file()
    assert handoff.is_file()
    assert "docs/handoff/DEPLOYMENT.md" in policy["required_files"]
    assert "3.12" in setup
    assert "requirements-dev.txt" in setup
    assert "requirements-asr.txt" in setup
    assert re.search(r"\bci\s+--no-audit\s+--no-fund\b", setup)


def test_checked_in_daily_config_is_portable_and_opt_in() -> None:
    config = json.loads((ROOT / "config" / "daily_tech_brief.json").read_text(encoding="utf-8"))
    avatar_directory = config["avatar"]["source_directory"]
    feed = config["copy_skill_hotspot_feed"]

    assert avatar_directory == ""
    assert feed["enabled"] is False
    assert not Path(feed["root"]).is_absolute()
    assert not re.match(r"^[A-Za-z]:[\\/]", feed["root"])


def test_release_contains_only_supported_runninghub_templates() -> None:
    runninghub = ROOT / "config" / "runninghub"
    required = {
        "longcat_avatar_api.json",
        "workflow-2093219950461808641.api.json",
        "workflow-2094449979141218305.api.json",
    }

    assert all((runninghub / name).is_file() for name in required)
