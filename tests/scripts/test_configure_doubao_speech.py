"""Safe local Doubao Speech configuration helper tests."""

from __future__ import annotations

from scripts import configure_doubao_speech


def test_parse_args_can_select_stdin_without_exposing_secret():
    args = configure_doubao_speech._parse_args(["--api-key-stdin"])

    assert args.api_key_stdin is True


def test_clone_voice_defaults_to_icl_while_public_voice_defaults_to_tts():
    assert configure_doubao_speech._default_resource_id("S_clone") == "seed-icl-2.0"
    assert configure_doubao_speech._default_resource_id("zh_female_vv_uranus_bigtts") == "seed-tts-2.0"


def test_write_values_preserves_unrelated_secrets_and_replaces_target_keys(tmp_path):
    target = tmp_path / ".env.secrets.local"
    target.write_text("OPENAI_API_KEY=keep-me\nDOUBAO_SPEECH_API_KEY=old\n", encoding="utf-8")

    configure_doubao_speech._write_values(target, {
        "DOUBAO_SPEECH_API_KEY": "new-secret",
        "DOUBAO_SPEECH_YAYA_VOICE_TYPE": "voice-yaya",
        "DOUBAO_SPEECH_MENGMENG_VOICE_TYPE": "voice-mengmeng",
    })

    text = target.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=keep-me" in text
    assert "DOUBAO_SPEECH_API_KEY=new-secret" in text
    assert text.count("DOUBAO_SPEECH_API_KEY=") == 1
    assert "DOUBAO_SPEECH_YAYA_VOICE_TYPE=voice-yaya" in text
    assert "DOUBAO_SPEECH_MENGMENG_VOICE_TYPE=voice-mengmeng" in text
