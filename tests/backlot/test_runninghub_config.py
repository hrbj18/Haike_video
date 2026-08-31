from __future__ import annotations

from pathlib import Path

from backlot import ai_text
from backlot.runninghub_config import read_runninghub_config, save_runninghub_config


def test_runninghub_config_is_masked_and_persists_only_to_local_secret_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ai_text, "CONFIG_ROOT", tmp_path)
    monkeypatch.delenv("RUNNINGHUB_API_KEY", raising=False)
    monkeypatch.delenv("RUNNINGHUB_WORKFLOW_ID", raising=False)
    monkeypatch.setenv("RUNNINGHUB_WORKFLOW_TEMPLATE", str(Path("config/runninghub/longcat_avatar_api.json").resolve()))

    saved = save_runninghub_config({
        "api_key": "rh-test-secret-value",
        "workflow_id": "12345678",
        "base_url": "https://www.runninghub.cn",
    })

    assert saved["configured"] is True
    assert saved["api_key_configured"] is True
    assert "rh-test-secret-value" not in str(saved)
    assert saved["workflow_id"] == "12345678"
    secret_text = (tmp_path / ".env.secrets.local").read_text(encoding="utf-8")
    assert "RUNNINGHUB_API_KEY=" in secret_text
    assert "rh-test-secret-value" in secret_text
    assert read_runninghub_config()["api_key_masked"] != "rh-test-secret-value"
