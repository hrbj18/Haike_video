from __future__ import annotations

from pathlib import Path

import pytest

from scripts.import_dashscope_credentials import read_credentials, update_env_file


def test_imports_only_required_dashscope_fields(tmp_path: Path) -> None:
    source = tmp_path / "credentials.csv"
    source.write_text(
        "id,example\n"
        "apiKey,sk-test-secret\n"
        "apiHost,example.invalid\n"
        "workspaceId,workspace_123456\n",
        encoding="utf-8-sig",
    )
    target = tmp_path / ".env.secrets.local"
    target.write_text("PEXELS_API_KEY=keep-me\nDASHSCOPE_API_KEY=old\n", encoding="utf-8")

    update_env_file(target, read_credentials(source))

    content = target.read_text(encoding="utf-8")
    assert "PEXELS_API_KEY=keep-me" in content
    assert "DASHSCOPE_API_KEY=sk-test-secret" in content
    assert "DASHSCOPE_WORKSPACE_ID=workspace_123456" in content
    assert "apiHost" not in content


def test_rejects_missing_workspace_id(tmp_path: Path) -> None:
    source = tmp_path / "credentials.csv"
    source.write_text("id,example\napiKey,sk-test-secret\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="DASHSCOPE_WORKSPACE_ID"):
        read_credentials(source)
