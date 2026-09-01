"""Interactively configure Haike Video's core provider credentials.

The script intentionally accepts secrets only through hidden terminal prompts.
It never calls a provider and never prints credential values.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env.secrets.local"

CORE_SECRET_KEYS = (
    "OPENAI_API_KEY",
    "DOUBAO_API_KEY",
    "RUNNINGHUB_API_KEY",
)
STATUS_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_TEXT_MODEL",
    "DOUBAO_API_KEY",
    "DOUBAO_BASE_URL",
    "DOUBAO_TEXT_MODEL",
    "RUNNINGHUB_API_KEY",
    "RUNNINGHUB_WORKFLOW_ID",
    "RUNNINGHUB_BASE_URL",
    "RUNNINGHUB_WORKFLOW_TEMPLATE",
    "RUNNINGHUB_WORKFLOW_PROFILE",
    "DOUBAO_SPEECH_API_KEY",
    "DOUBAO_SPEECH_VOICE_TYPE",
)
CORE_REQUIRED_KEYS = STATUS_KEYS[:-2]

DEFAULTS = {
    "OPENAI_TEXT_MODEL": "gpt-5.6-luna",
    "DOUBAO_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
    "DOUBAO_TEXT_MODEL": "doubao-seed-2-1-pro-260628",
    "RUNNINGHUB_WORKFLOW_ID": "2094449979141218305",
    "RUNNINGHUB_BASE_URL": "https://www.runninghub.cn",
    "RUNNINGHUB_WORKFLOW_TEMPLATE": (
        "config/runninghub/workflow-2094449979141218305.api.json"
    ),
    "RUNNINGHUB_WORKFLOW_PROFILE": "infinitetalk_448x560_exact_clock_v2",
}

_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def validate_value(name: str, value: str, *, required: bool = False) -> str:
    """Validate a value before it is written to a dotenv file."""

    cleaned = value.strip()
    if "\r" in cleaned or "\n" in cleaned or "\x00" in cleaned:
        raise ValueError(f"{name} 不能包含换行符或空字符")
    if required and not cleaned:
        raise ValueError(f"{name} 不能为空")
    return cleaned


def read_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without expanding or logging values."""

    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            cleaned = value.strip()
            if (
                len(cleaned) >= 2
                and cleaned[0] == cleaned[-1]
                and cleaned[0] in {"'", '"'}
            ):
                cleaned = cleaned[1:-1]
            values[key] = cleaned
    return values


def update_env_file(path: Path, updates: Mapping[str, str]) -> None:
    """Atomically update selected keys while preserving unrelated content."""

    normalized = {key: validate_value(key, value) for key, value in updates.items()}
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    lines = original.splitlines()
    seen: set[str] = set()
    output: list[str] = []

    for line in lines:
        match = _ENV_LINE.match(line)
        key = match.group(1) if match else None
        if key in normalized:
            if key not in seen:
                output.append(f"{key}={normalized[key]}")
                seen.add(key)
        else:
            output.append(line)

    missing = [(key, value) for key, value in normalized.items() if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Haike Video core credentials (managed by configure_core_secrets.py)")
        output.extend(f"{key}={value}" for key, value in missing)

    rendered = "\n".join(output).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def configuration_status(values: Mapping[str, str]) -> dict[str, bool]:
    """Return presence flags only; callers must not print the values mapping."""

    return {key: bool(str(values.get(key, "")).strip()) for key in STATUS_KEYS}


def print_status(values: Mapping[str, str]) -> bool:
    """Print redacted configuration status and return core readiness."""

    status = configuration_status(values)
    print("核心密钥配置状态（不显示任何值）：")
    for key in STATUS_KEYS:
        label = "已配置" if status[key] else "未配置"
        optional = "（可选）" if key.startswith("DOUBAO_SPEECH_") else ""
        print(f"- {key}{optional}: {label}")
    ready = all(status.get(key, False) for key in CORE_REQUIRED_KEYS)
    print(f"核心配置：{'就绪' if ready else '未就绪'}")
    return ready


def _prompt_visible(
    label: str,
    current: str,
    *,
    default: str = "",
    required: bool = False,
    input_fn: Callable[[str], str] = input,
) -> str:
    fallback = current or default
    hint = "回车保留当前值" if current else (f"回车使用 {default}" if default else "必填")
    while True:
        value = input_fn(f"{label}（{hint}）：").strip()
        candidate = value or fallback
        try:
            return validate_value(label, candidate, required=required)
        except ValueError as exc:
            print(f"输入无效：{exc}")


def _prompt_secret(
    key: str,
    current: str,
    *,
    required: bool = True,
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> str:
    hint = "已配置，回车保留" if current else ("必填" if required else "可选")
    while True:
        value = getpass_fn(f"{key}（隐藏输入；{hint}）：")
        candidate = value.strip() or current
        try:
            return validate_value(key, candidate, required=required)
        except ValueError as exc:
            print(f"输入无效：{exc}")


def collect_updates(
    current: Mapping[str, str],
    *,
    include_doubao_speech: bool = False,
) -> dict[str, str]:
    """Collect the three core credentials and their non-secret routing fields."""

    print("请输入三组核心密钥。密钥输入不会回显；回车可保留已有值。")
    updates = {
        "OPENAI_API_KEY": _prompt_secret(
            "OPENAI_API_KEY", current.get("OPENAI_API_KEY", "")
        ),
        "OPENAI_BASE_URL": _prompt_visible(
            "GPT 中转站 Base URL",
            current.get("OPENAI_BASE_URL", ""),
            required=True,
        ),
        "OPENAI_TEXT_MODEL": _prompt_visible(
            "GPT 文本模型",
            current.get("OPENAI_TEXT_MODEL", ""),
            default=DEFAULTS["OPENAI_TEXT_MODEL"],
            required=True,
        ),
        "DOUBAO_API_KEY": _prompt_secret(
            "DOUBAO_API_KEY", current.get("DOUBAO_API_KEY", "")
        ),
        "DOUBAO_BASE_URL": current.get("DOUBAO_BASE_URL", "")
        or DEFAULTS["DOUBAO_BASE_URL"],
        "DOUBAO_TEXT_MODEL": current.get("DOUBAO_TEXT_MODEL", "")
        or DEFAULTS["DOUBAO_TEXT_MODEL"],
        "RUNNINGHUB_API_KEY": _prompt_secret(
            "RUNNINGHUB_API_KEY", current.get("RUNNINGHUB_API_KEY", "")
        ),
        "RUNNINGHUB_WORKFLOW_ID": current.get("RUNNINGHUB_WORKFLOW_ID", "")
        or DEFAULTS["RUNNINGHUB_WORKFLOW_ID"],
        "RUNNINGHUB_BASE_URL": current.get("RUNNINGHUB_BASE_URL", "")
        or DEFAULTS["RUNNINGHUB_BASE_URL"],
        "RUNNINGHUB_WORKFLOW_TEMPLATE": current.get(
            "RUNNINGHUB_WORKFLOW_TEMPLATE", ""
        )
        or DEFAULTS["RUNNINGHUB_WORKFLOW_TEMPLATE"],
        "RUNNINGHUB_WORKFLOW_PROFILE": current.get(
            "RUNNINGHUB_WORKFLOW_PROFILE", ""
        )
        or DEFAULTS["RUNNINGHUB_WORKFLOW_PROFILE"],
    }
    if include_doubao_speech:
        updates["DOUBAO_SPEECH_API_KEY"] = _prompt_secret(
            "DOUBAO_SPEECH_API_KEY",
            current.get("DOUBAO_SPEECH_API_KEY", ""),
            required=False,
        )
        updates["DOUBAO_SPEECH_VOICE_TYPE"] = _prompt_visible(
            "豆包语音 Voice Type",
            current.get("DOUBAO_SPEECH_VOICE_TYPE", ""),
            required=False,
        )
    return updates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="隐藏输入并一次性配置 GPT 中转站、豆包文本和 RunningHub 密钥。"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只输出已配置/未配置状态，不显示值、不联网",
    )
    parser.add_argument(
        "--with-doubao-speech",
        action="store_true",
        help="额外配置独立的豆包语音 TTS 密钥（不等同于豆包文本密钥）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_file = DEFAULT_ENV_FILE.resolve()
    current = read_env(env_file)
    if args.check:
        return 0 if print_status(current) else 1

    try:
        updates = collect_updates(
            current, include_doubao_speech=args.with_doubao_speech
        )
        update_env_file(env_file, updates)
    except (KeyboardInterrupt, EOFError):
        print("\n已取消；未写入新的密钥配置。", file=sys.stderr)
        return 130

    print(f"配置已安全写入：{env_file}")
    print("脚本未连接供应商，也未触发任何付费调用。")
    return 0 if print_status(read_env(env_file)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
