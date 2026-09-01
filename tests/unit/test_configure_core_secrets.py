from __future__ import annotations

from pathlib import Path

import pytest

from scripts.configure_core_secrets import (
    DEFAULTS,
    configuration_status,
    print_status,
    read_env,
    update_env_file,
    validate_value,
)


def test_update_env_file_preserves_unrelated_lines_and_replaces_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.secrets.local"
    env_file.write_text(
        "# existing\nUNRELATED=value\nOPENAI_API_KEY=old\nOPENAI_API_KEY=stale\n",
        encoding="utf-8",
    )

    update_env_file(
        env_file,
        {
            "OPENAI_API_KEY": "new-secret",
            "RUNNINGHUB_API_KEY": "running-secret",
            "RUNNINGHUB_WORKFLOW_ID": DEFAULTS["RUNNINGHUB_WORKFLOW_ID"],
        },
    )

    rendered = env_file.read_text(encoding="utf-8")
    assert "# existing" in rendered
    assert "UNRELATED=value" in rendered
    assert "OPENAI_API_KEY=new-secret" in rendered
    assert "OPENAI_API_KEY=old" not in rendered
    assert "OPENAI_API_KEY=stale" not in rendered
    assert rendered.count("OPENAI_API_KEY=") == 1
    assert "RUNNINGHUB_API_KEY=running-secret" in rendered
    assert read_env(env_file)["RUNNINGHUB_WORKFLOW_ID"] == DEFAULTS[
        "RUNNINGHUB_WORKFLOW_ID"
    ]


def test_status_never_prints_secret_values(capsys: pytest.CaptureFixture[str]) -> None:
    values = {
        "OPENAI_API_KEY": "openai-super-secret",
        "OPENAI_BASE_URL": "https://relay.example/v1",
        "OPENAI_TEXT_MODEL": "gpt-example",
        "DOUBAO_API_KEY": "doubao-super-secret",
        "DOUBAO_BASE_URL": DEFAULTS["DOUBAO_BASE_URL"],
        "DOUBAO_TEXT_MODEL": DEFAULTS["DOUBAO_TEXT_MODEL"],
        "RUNNINGHUB_API_KEY": "runninghub-super-secret",
        "RUNNINGHUB_WORKFLOW_ID": DEFAULTS["RUNNINGHUB_WORKFLOW_ID"],
        "RUNNINGHUB_BASE_URL": DEFAULTS["RUNNINGHUB_BASE_URL"],
        "RUNNINGHUB_WORKFLOW_TEMPLATE": DEFAULTS["RUNNINGHUB_WORKFLOW_TEMPLATE"],
        "RUNNINGHUB_WORKFLOW_PROFILE": DEFAULTS["RUNNINGHUB_WORKFLOW_PROFILE"],
    }

    assert print_status(values) is True
    output = capsys.readouterr().out
    assert "openai-super-secret" not in output
    assert "doubao-super-secret" not in output
    assert "runninghub-super-secret" not in output
    assert "OPENAI_API_KEY: 已配置" in output
    assert configuration_status(values)["DOUBAO_SPEECH_API_KEY"] is False


@pytest.mark.parametrize("value", ["abc\ndef", "abc\rdef", "abc\x00def"])
def test_validate_value_rejects_control_characters(value: str) -> None:
    with pytest.raises(ValueError):
        validate_value("API_KEY", value, required=True)


def test_required_value_cannot_be_empty() -> None:
    with pytest.raises(ValueError):
        validate_value("API_KEY", "   ", required=True)


def test_read_env_treats_quoted_empty_value_as_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.secrets.local"
    env_file.write_text('OPENAI_API_KEY=""\n', encoding="utf-8")

    assert read_env(env_file)["OPENAI_API_KEY"] == ""
