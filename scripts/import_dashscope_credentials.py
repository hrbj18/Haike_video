"""Safely import Alibaba Bailian credentials from an exported CSV.

The exporter writes a two-column key/value document whose first row is a
heading rather than data.  This utility only copies the fields Haike Video
needs and never prints credential values.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import tempfile
from pathlib import Path


ENV_NAMES = {
    "apikey": "DASHSCOPE_API_KEY",
    "workspaceid": "DASHSCOPE_WORKSPACE_ID",
}


def read_credentials(csv_path: Path) -> dict[str, str]:
    if not csv_path.is_file():
        raise SystemExit(f"未找到百炼凭证文件：{csv_path}")
    rows = list(csv.reader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
    values: dict[str, str] = {}
    for row in rows:
        if len(row) < 2:
            continue
        name = re.sub(r"[^a-z0-9]", "", row[0].strip().lower())
        if name in ENV_NAMES and row[1].strip():
            values[ENV_NAMES[name]] = row[1].strip()
    missing = sorted(set(ENV_NAMES.values()) - set(values))
    if missing:
        raise SystemExit(f"CSV 缺少必要字段：{', '.join(missing)}")
    if not values["DASHSCOPE_API_KEY"].startswith("sk-"):
        raise SystemExit("CSV 中的 apiKey 格式不正确")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", values["DASHSCOPE_WORKSPACE_ID"]):
        raise SystemExit("CSV 中的 workspaceId 格式不正确")
    return values


def update_env_file(env_path: Path, values: dict[str, str]) -> None:
    lines = env_path.read_text(encoding="utf-8-sig").splitlines() if env_path.is_file() else []
    pending = dict(values)
    updated: list[str] = []
    for line in lines:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in pending:
            key = match.group(1)
            updated.append(f"{key}={pending.pop(key)}")
        else:
            updated.append(line)
    if pending and updated and updated[-1].strip():
        updated.append("")
    if pending:
        updated.append("# 阿里云百炼（北京业务空间，仅本机使用）")
        updated.extend(f"{key}={value}" for key, value in pending.items())

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{env_path.name}.", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(updated).rstrip() + "\n")
        os.replace(temporary_name, env_path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="安全导入阿里云百炼凭证（不会显示密钥）")
    parser.add_argument("csv_path", type=Path, help="百炼控制台导出的 API Key CSV")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent / ".env.secrets.local",
        help="目标秘密配置文件",
    )
    args = parser.parse_args()
    update_env_file(args.env_file, read_credentials(args.csv_path))
    print("已配置 DASHSCOPE_API_KEY 与 DASHSCOPE_WORKSPACE_ID；凭证值未显示。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
