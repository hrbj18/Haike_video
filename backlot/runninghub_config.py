"""Secure, software-wide RunningHub configuration for the local workbench."""

from __future__ import annotations

import os
import re
from typing import Any

from backlot.ai_text import (
    _atomic_write_text,
    _effective_value,
    _mask_secret,
    _quote_env_value,
    _read_env_file,
    _secrets_path,
)
from tools.avatar.runninghub_avatar import DEFAULT_BASE_URL, runninghub_configuration


class RunningHubConfigError(RuntimeError):
    """A user-correctable RunningHub configuration error."""


CONFIG_KEYS = (
    "RUNNINGHUB_API_KEY",
    "RUNNINGHUB_WORKFLOW_ID",
    "RUNNINGHUB_BASE_URL",
    "RUNNINGHUB_WORKFLOW_TEMPLATE",
    "RUNNINGHUB_WORKFLOW_PROFILE",
)


def read_runninghub_config() -> dict[str, Any]:
    _, values = _read_env_file(_secrets_path())
    api_key = _effective_value("RUNNINGHUB_API_KEY", values)
    workflow_id = _effective_value("RUNNINGHUB_WORKFLOW_ID", values)
    workflow_profile = _effective_value("RUNNINGHUB_WORKFLOW_PROFILE", values)
    status = runninghub_configuration()
    return {
        "configured": bool(status["configured"]),
        "api_key_configured": bool(api_key),
        "api_key_masked": _mask_secret(api_key),
        "workflow_id": workflow_id,
        "workflow_profile": workflow_profile,
        "workflow_template": _effective_value("RUNNINGHUB_WORKFLOW_TEMPLATE", values),
        "base_url": _effective_value("RUNNINGHUB_BASE_URL", values) or DEFAULT_BASE_URL,
        "template_sha256": status.get("template_sha256"),
        "issues": status.get("issues") or [],
        "storage": ".env.secrets.local",
    }


def save_runninghub_config(payload: dict[str, Any]) -> dict[str, Any]:
    path = _secrets_path()
    lines, current = _read_env_file(path)
    api_key = str(payload.get("api_key") or "").strip()
    workflow_id = str(payload.get("workflow_id") or "").strip()
    base_url = str(payload.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    workflow_profile = str(payload.get("workflow_profile") or current.get("RUNNINGHUB_WORKFLOW_PROFILE") or "longcat_duration_safe").strip()
    workflow_template = str(payload.get("workflow_template") or current.get("RUNNINGHUB_WORKFLOW_TEMPLATE") or "config/runninghub/longcat_avatar_api.json").strip()
    if api_key and (len(api_key) > 1000 or re.search(r"[\r\n]", api_key)):
        raise RunningHubConfigError("RunningHub API 密钥格式无效")
    if workflow_id and not re.fullmatch(r"[0-9]{4,32}", workflow_id):
        raise RunningHubConfigError("RunningHub 工作流 ID 应为发布页面显示的 4–32 位数字")
    if not workflow_id:
        raise RunningHubConfigError("请填写已发布 RunningHub 工作流的 workflowId；下载的 API JSON 不包含该编号")
    if not re.match(r"^https://[^\s]+$", base_url, re.IGNORECASE):
        raise RunningHubConfigError("RunningHub 接口地址必须是 https:// 开头的完整地址")
    updates = {
        "RUNNINGHUB_API_KEY": api_key or current.get("RUNNINGHUB_API_KEY", ""),
        "RUNNINGHUB_WORKFLOW_ID": workflow_id,
        "RUNNINGHUB_BASE_URL": base_url,
        "RUNNINGHUB_WORKFLOW_TEMPLATE": workflow_template,
        "RUNNINGHUB_WORKFLOW_PROFILE": workflow_profile,
    }
    if not updates["RUNNINGHUB_API_KEY"]:
        raise RunningHubConfigError("请填写 RunningHub API 密钥")
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else ""
        if key in updates:
            if key not in seen:
                output.append(f"{key}={_quote_env_value(updates[key])}")
                seen.add(key)
            continue
        output.append(line)
    if output and output[-1].strip():
        output.append("")
    for key in CONFIG_KEYS:
        if key not in seen:
            output.append(f"{key}={_quote_env_value(updates[key])}")
    _atomic_write_text(path, "\n".join(output).rstrip() + "\n")
    for key, value in updates.items():
        os.environ[key] = value
    return read_runninghub_config()
