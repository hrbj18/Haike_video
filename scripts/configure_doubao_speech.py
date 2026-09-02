"""Safely configure Doubao Speech without putting the API key in shell history."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / ".env.secrets.local"
LOCAL = ROOT / ".env.local"
KEYS = (
    "DOUBAO_SPEECH_API_KEY",
    "DOUBAO_SPEECH_YAYA_VOICE_TYPE",
    "DOUBAO_SPEECH_YAYA_RESOURCE_ID",
    "DOUBAO_SPEECH_MENGMENG_VOICE_TYPE",
    "DOUBAO_SPEECH_MENGMENG_RESOURCE_ID",
)


def _read(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return lines, values


def _write_values(path: Path, updates: dict[str, str]) -> None:
    lines, _ = _read(path)
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# Volcengine Doubao Speech. Local only; never commit this file.")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".doubao-speech-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
        Path(temporary).replace(path)
        if os.name != "nt":
            path.chmod(0o600)
    except Exception:
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure local Doubao Speech credentials safely.")
    parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="Read the API key from the first stdin line instead of an interactive prompt.",
    )
    return parser.parse_args(argv)


def _default_resource_id(voice_id: str) -> str:
    return "seed-icl-2.0" if voice_id.strip().startswith("S_") else "seed-tts-2.0"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _, secrets = _read(TARGET)
    _, local = _read(LOCAL)
    current = {**local, **secrets}
    api_key = (
        sys.stdin.readline().strip()
        if args.api_key_stdin
        else getpass.getpass("豆包语音 API Key（输入不会显示）：").strip()
    )
    if not api_key:
        print("未输入 API Key，未修改配置。")
        return 2
    yaya_default = current.get("DOUBAO_SPEECH_YAYA_VOICE_TYPE", "")
    mengmeng_default = current.get("DOUBAO_SPEECH_MENGMENG_VOICE_TYPE", "")
    yaya = input(f"雅雅音色 ID{f' [{yaya_default}]' if yaya_default else ''}：").strip() or yaya_default
    mengmeng = input(f"檬檬音色 ID{f' [{mengmeng_default}]' if mengmeng_default else ''}：").strip() or mengmeng_default
    if not yaya or not mengmeng:
        print("两个角色音色 ID 都必须配置，未修改配置。")
        return 2
    _write_values(TARGET, {
        "DOUBAO_SPEECH_API_KEY": api_key,
        "DOUBAO_SPEECH_YAYA_VOICE_TYPE": yaya,
        "DOUBAO_SPEECH_YAYA_RESOURCE_ID": _default_resource_id(yaya),
        "DOUBAO_SPEECH_MENGMENG_VOICE_TYPE": mengmeng,
        "DOUBAO_SPEECH_MENGMENG_RESOURCE_ID": _default_resource_id(mengmeng),
    })
    print("豆包语音密钥与两位角色音色已写入 Git 忽略的 .env.secrets.local；未显示密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
