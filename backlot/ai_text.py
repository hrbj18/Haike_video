"""Secure OpenAI-compatible text configuration and visual-copy planning.

The workbench only exposes masked configuration metadata to the browser.  API
keys live in ``.env.secrets.local`` and are never written into project state,
task payloads, or error messages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backlot.state import REPO_ROOT


class TextAIError(RuntimeError):
    """A user-safe text intelligence error."""


CONFIG_ROOT = REPO_ROOT
SECRETS_FILENAME = ".env.secrets.local"
CONFIG_KEYS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_TEXT_MODEL")
DEFAULT_TEXT_MODEL = "gpt-5.6-luna"
# 豆包（火山方舟）作为第二文本模型，专门负责写台词/口语化文风。
# 与主模型共用 OpenAI 兼容协议，只是走独立的密钥与端点。
DOUBAO_CONFIG_KEYS = ("DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_TEXT_MODEL")
DEFAULT_DOUBAO_MODEL = "doubao-seed-2-1-pro-260628"
DEFAULT_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
TEXT_PROVIDERS = ("default", "doubao")
# Daily news scripts carry a larger candidate packet than a single visual-copy
# request.  The former 45 second HTTP limit was short enough to turn a healthy
# but queued relay response into a false pipeline failure.
DEFAULT_JSON_REQUEST_TIMEOUT_SECONDS = 120
ALLOWED_RECIPES = {
    "headline_statement",
    "relationship_map",
    "single_metric",
    "comparison",
    "process",
    "quote_evidence",
    "closing_question",
}
ALLOWED_VISUAL_ROUTES = {
    "stock_video",
    "stock_image",
    "ai_image",
    "hyperframes",
}
DEFAULT_LAYOUT_VARIANTS = {
    "headline_statement": (
        {"id": "editorial_headline", "motion_variant": "stamp_in"},
        {"id": "signal_stack", "motion_variant": "tags_lock"},
    ),
    "relationship_map": (
        {"id": "radial_map", "motion_variant": "node_bloom"},
        {"id": "causal_chain", "motion_variant": "step_through"},
        {"id": "convergence", "motion_variant": "converge_in"},
    ),
    "single_metric": (
        {"id": "hero_metric", "motion_variant": "metric_pop"},
        {"id": "metric_ledger", "motion_variant": "ledger_reveal"},
    ),
    "comparison": (
        {"id": "split_columns", "motion_variant": "opposing_slide"},
        {"id": "stacked_duel", "motion_variant": "top_bottom_lock"},
        {"id": "balance_axis", "motion_variant": "axis_balance"},
    ),
    "process": (
        {"id": "vertical_rail", "motion_variant": "rail_build"},
        {"id": "zigzag_steps", "motion_variant": "zigzag_step"},
    ),
    "quote_evidence": ({"id": "claim_evidence", "motion_variant": "evidence_stack"},),
    "closing_question": ({"id": "question_hold", "motion_variant": "question_land"},),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _secrets_path() -> Path:
    return Path(CONFIG_ROOT) / SECRETS_FILENAME


def _read_env_file(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    values: dict[str, str] = {}
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
        if not match:
            continue
        raw = match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            raw = raw[1:-1]
        values[match.group(1)] = raw
    return lines, values


def _effective_value(key: str, file_values: dict[str, str]) -> str:
    # The UI-managed secret file is authoritative for values it contains.
    # Process variables remain a supported deployment override otherwise.
    if key in file_values:
        return file_values[key].strip()
    return str(os.environ.get(key) or "").strip()


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "已保存"
    return f"{value[:3]}••••••{value[-4:]}"


def read_text_ai_config() -> dict[str, Any]:
    _, values = _read_env_file(_secrets_path())
    key = _effective_value("OPENAI_API_KEY", values)
    base_url = _effective_value("OPENAI_BASE_URL", values)
    model = (
        _effective_value("OPENAI_TEXT_MODEL", values)
        or str(os.environ.get("OPENAI_SCRIPT_MODEL") or "").strip()
        or DEFAULT_TEXT_MODEL
    )
    return {
        "configured": bool(key and model),
        "api_key_masked": _mask_secret(key),
        "base_url": base_url,
        "model": model,
        "storage": SECRETS_FILENAME,
    }


def read_doubao_text_ai_config() -> dict[str, Any]:
    """Return browser-safe status for the daily brief editorial model."""
    _, values = _read_env_file(_secrets_path())
    key = _effective_value("DOUBAO_API_KEY", values)
    base_url = _effective_value("DOUBAO_BASE_URL", values) or DEFAULT_DOUBAO_BASE_URL
    model = _effective_value("DOUBAO_TEXT_MODEL", values) or DEFAULT_DOUBAO_MODEL
    return {
        "provider": "doubao",
        "configured": bool(key and model),
        "api_key_masked": _mask_secret(key),
        "base_url": base_url,
        "model": model,
        "storage": SECRETS_FILENAME,
        "role": "daily_editorial_owner",
    }


def daily_editorial_provider() -> str:
    """Preferred provider for selection, Chinese copy and cold review.

    Doubao owns the audience judgement for the Chinese short-video edition.
    The default model remains a technical fallback so missing Doubao
    credentials never make an unattended run impossible.
    """
    return "doubao" if doubao_configured() else "default"


def read_text_provider_status() -> dict[str, Any]:
    """Expose the editorial route without leaking either credential."""
    editorial_provider = daily_editorial_provider()
    return {
        "default": {**read_text_ai_config(), "provider": "default", "role": "technical_fallback"},
        "doubao": read_doubao_text_ai_config(),
        "selection_provider": editorial_provider,
        "writer_provider": editorial_provider,
        "reviewer_provider": editorial_provider,
        "technical_fallback_provider": "default",
    }


def _quote_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except Exception:
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise


def save_text_ai_config(payload: dict[str, Any]) -> dict[str, Any]:
    path = _secrets_path()
    lines, current = _read_env_file(path)
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    model = str(payload.get("model") or "").strip()
    supplied_key = str(payload.get("api_key") or "").strip()
    clear_key = payload.get("clear_api_key") is True

    if base_url and not re.match(r"^https?://[^\s]+$", base_url, re.IGNORECASE):
        raise TextAIError("接口地址必须是以 http:// 或 https:// 开头的完整地址")
    if not model or len(model) > 160 or re.search(r"[\r\n]", model):
        raise TextAIError("请填写有效的文本模型名称")
    if supplied_key and (len(supplied_key) > 1000 or re.search(r"[\r\n]", supplied_key)):
        raise TextAIError("API 密钥格式无效")

    updates = {
        "OPENAI_BASE_URL": base_url,
        "OPENAI_TEXT_MODEL": model,
    }
    if clear_key:
        updates["OPENAI_API_KEY"] = ""
    elif supplied_key:
        updates["OPENAI_API_KEY"] = supplied_key
    elif "OPENAI_API_KEY" in current:
        updates["OPENAI_API_KEY"] = current["OPENAI_API_KEY"]

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
        if key in updates and key not in seen:
            output.append(f"{key}={_quote_env_value(updates[key])}")
    _atomic_write_text(path, "\n".join(output).rstrip() + "\n")

    # Keep this process in sync immediately; a workbench restart is not
    # required after a user saves configuration.
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return read_text_ai_config()


def save_doubao_text_ai_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist only the Doubao editorial fields, preserving the main model."""
    path = _secrets_path()
    lines, current = _read_env_file(path)
    base_url = str(payload.get("base_url") or DEFAULT_DOUBAO_BASE_URL).strip().rstrip("/")
    model = str(payload.get("model") or DEFAULT_DOUBAO_MODEL).strip()
    supplied_key = str(payload.get("api_key") or "").strip()
    clear_key = payload.get("clear_api_key") is True

    if not re.match(r"^https://[^\s]+$", base_url, re.IGNORECASE):
        raise TextAIError("豆包接口地址必须是以 https:// 开头的完整地址")
    if not model or len(model) > 160 or re.search(r"[\r\n]", model):
        raise TextAIError("请填写有效的豆包文本模型名称")
    if supplied_key and (len(supplied_key) > 1000 or re.search(r"[\r\n]", supplied_key)):
        raise TextAIError("豆包 API 密钥格式无效")

    updates = {"DOUBAO_BASE_URL": base_url, "DOUBAO_TEXT_MODEL": model}
    if clear_key:
        updates["DOUBAO_API_KEY"] = ""
    elif supplied_key:
        updates["DOUBAO_API_KEY"] = supplied_key
    elif "DOUBAO_API_KEY" in current:
        updates["DOUBAO_API_KEY"] = current["DOUBAO_API_KEY"]

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
    for key in DOUBAO_CONFIG_KEYS:
        if key in updates and key not in seen:
            output.append(f"{key}={_quote_env_value(updates[key])}")
    _atomic_write_text(path, "\n".join(output).rstrip() + "\n")
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return read_doubao_text_ai_config()


def _resolved_credentials(provider: str = "default") -> tuple[str, str, str]:
    if provider not in TEXT_PROVIDERS:
        raise TextAIError(f"不支持的文本提供方：{provider or '[空]'}")
    _, values = _read_env_file(_secrets_path())
    if provider == "doubao":
        api_key = _effective_value("DOUBAO_API_KEY", values)
        base_url = _effective_value("DOUBAO_BASE_URL", values) or DEFAULT_DOUBAO_BASE_URL
        model = _effective_value("DOUBAO_TEXT_MODEL", values) or DEFAULT_DOUBAO_MODEL
        if not api_key:
            raise TextAIError("尚未配置豆包 API 密钥，请在 .env.secrets.local 中填写 DOUBAO_API_KEY")
        return api_key, base_url, model
    api_key = _effective_value("OPENAI_API_KEY", values)
    base_url = _effective_value("OPENAI_BASE_URL", values)
    model = (
        _effective_value("OPENAI_TEXT_MODEL", values)
        or str(os.environ.get("OPENAI_SCRIPT_MODEL") or "").strip()
        or DEFAULT_TEXT_MODEL
    )
    if not api_key:
        raise TextAIError("尚未配置 AI API 密钥，请先打开右上角“AI 配置”")
    return api_key, base_url, model


def doubao_configured() -> bool:
    """Whether the Doubao daily-editorial model is available.

    When false, the pipeline transparently falls back to the default model so a
    missing key never breaks production.
    """
    try:
        _resolved_credentials("doubao")
    except TextAIError:
        return False
    return True


def _safe_provider_error(error: object) -> str:
    message = str(error or "").strip()
    secret_keys: list[str] = []
    for provider in ("default", "doubao"):
        try:
            api_key, _, _ = _resolved_credentials(provider)
        except TextAIError:
            continue
        if api_key:
            secret_keys.append(api_key)
    for api_key in secret_keys:
        message = message.replace(api_key, "[密钥已隐藏]")
    message = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[密钥已隐藏]", message)
    lowered = message.lower()
    if "401" in lowered or "unauthorized" in lowered or "invalid api key" in lowered:
        return "身份验证失败，请检查 API 密钥"
    if "404" in lowered or "model_not_found" in lowered:
        return "接口或模型不存在，请检查接口地址和模型名称"
    if "429" in lowered or "rate limit" in lowered:
        return "请求过于频繁或额度不足，请稍后重试并检查账户额度"
    if "timeout" in lowered or "timed out" in lowered:
        return "AI 服务响应超时，请稍后重试"
    return (message or "AI 服务未返回具体原因")[:500]


def _parse_json_object(content: str) -> dict[str, Any]:
    cleaned = (content or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise TextAIError("AI 返回内容不是有效的结构化文案")
        try:
            parsed = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise TextAIError("AI 返回内容不是有效的结构化文案") from exc
    if not isinstance(parsed, dict):
        raise TextAIError("AI 返回内容不是有效的结构化文案")
    return parsed


def _streamed_chat_content(response: Any, *, max_elapsed_seconds: int | None = None) -> str:
    """Collect OpenAI-compatible SSE deltas without waiting behind a proxy idle timeout."""
    chunks: list[str] = []
    saw_event = False
    started_at = time.monotonic()
    try:
        # Some OpenAI-compatible relays return ``text/event-stream`` without
        # a charset.  requests then guesses Latin-1 when decode_unicode=True,
        # turning every Chinese character into mojibake.  SSE JSON is UTF-8 by
        # contract, so retain bytes and decode deterministically here.
        for raw_line in response.iter_lines(decode_unicode=False):
            if max_elapsed_seconds is not None and time.monotonic() - started_at > max_elapsed_seconds:
                raise TextAIError("AI 流式请求超过总时限，可安全重试")
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            line = str(raw_line or "").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            saw_event = True
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") if isinstance(event, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta") if isinstance(choices[0].get("delta"), dict) else {}
            content = delta.get("content")
            if isinstance(content, str):
                chunks.append(content)
    except TextAIError:
        raise
    except Exception as exc:
        partial = "".join(chunks).strip()
        if partial:
            try:
                json.loads(partial)
                return partial
            except json.JSONDecodeError:
                pass
        raise TextAIError("AI 流式连接中断，响应内容不完整，可安全重试") from exc
    if saw_event and not chunks:
        raise TextAIError("AI 流式响应没有返回可用的文本结果")
    return "".join(chunks)


def _chat_json(
    system_prompt: str,
    user_payload: dict[str, Any],
    *,
    timeout_seconds: int = DEFAULT_JSON_REQUEST_TIMEOUT_SECONDS,
    temperature: float = 0.2,
    provider: str = "default",
) -> tuple[dict[str, Any], str]:
    api_key, base_url, model = _resolved_credentials(provider)
    try:
        import requests

        endpoint = f"{(base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions"
        request: dict[str, Any] = {
            "model": model,
            "temperature": max(0.0, min(1.0, float(temperature))),
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        if provider == "doubao":
            # Daily scripts are short constrained JSON, not reasoning tasks.
            # Doubao Seed's automatic thinking can otherwise keep an SSE
            # stream alive for minutes even for a one-line repair.
            request["thinking"] = {"type": "disabled"}
            request["max_tokens"] = 4096
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeout = max(15, int(timeout_seconds))
        response = requests.post(endpoint, headers=headers, json=request, timeout=timeout, stream=True)
        if response.status_code >= 400 and "thinking" in response.text.lower():
            # Keep compatibility with older Ark-compatible deployments while
            # preferring disabled thinking on models that support it.
            request.pop("thinking", None)
            response = requests.post(endpoint, headers=headers, json=request, timeout=timeout, stream=True)
        if response.status_code >= 400 and (
            "response_format" in response.text.lower() or "json_object" in response.text.lower()
        ):
            request.pop("response_format", None)
            response = requests.post(endpoint, headers=headers, json=request, timeout=timeout, stream=True)
        if response.status_code >= 400:
            raise TextAIError(_safe_provider_error(f"HTTP {response.status_code}: {response.text[:500]}"))
        content = ""
        if hasattr(response, "iter_lines"):
            content = _streamed_chat_content(response, max_elapsed_seconds=timeout)
        if content:
            return _parse_json_object(content), model
        data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise TextAIError("AI 服务没有返回可用的文本结果")
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        content = str((message or {}).get("content") or "")
        return _parse_json_object(content), model
    except TextAIError:
        raise
    except Exception as exc:
        raise TextAIError(_safe_provider_error(exc)) from exc


VISUAL_CANDIDATE_DIRECTOR_PROMPT_VERSION = "visual-candidate-director-v1"


def evaluate_visual_candidates(context: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Ask the project's configured text model to judge *pre-download* stock candidates.

    This deliberately exposes no API credentials or signed download URLs to the
    model.  Callers still own candidate whitelisting and score validation: this
    function only makes a structured recommendation, never performs a download.
    """
    system = """你是 Haike Video 的自动视觉导演。请从输入的候选素材中，为一个已经绑定音频时间轴的画面格选择最合适的一条。
只输出 JSON，不要 Markdown：
{
  "selected_asset_id": "必须是输入候选的 asset_id，或空字符串",
  "semantic_score": 0,
  "aesthetic_score": 0,
  "continuity_score": 0,
  "technical_score": 0,
  "confidence": 0,
  "decision": "accept|retry|fallback",
  "reason": "不超过160字的中文理由",
  "retry_queries": ["至多2条英文素材检索词"]
}
规则：
1. 画面必须先符合当前 slot_text、visual_intent 与前后文；语义不合格不能因画面高级而入选。
2. 选择真实、简洁、现代、具有科技纪录片质感的画面；避免第二主播、演播室、正面大脸、广告感、明显水印或与当前数字人重复的人像。
3. 优先主体明确、比例/时长/清晰度适配的候选，并避免与 recently_used_asset_ids 重复。
4. confidence 为 0 到 1；任一评分为 0 到 100。若候选都不可靠，选择 retry 并给出新的英文检索词；无可用重试时选 fallback。
5. 不得捏造 asset_id、时间、事实或素材内容；不得输出下载链接。"""
    return _chat_json(system, context, temperature=0.0)


def test_text_ai_connection(provider: str = "default") -> dict[str, Any]:
    system = '你是连接测试器。只输出 JSON：{"ok":true,"message":"连接正常"}。'
    result, model = _chat_json(system, {"task": "connection_test"}, provider=provider)
    if result.get("ok") is not True:
        raise TextAIError("AI 服务已响应，但未通过结构化输出测试")
    return {"ok": True, "message": "连接正常，结构化文本能力可用", "model": model, "provider": provider}


def test_doubao_text_ai_connection() -> dict[str, Any]:
    return test_text_ai_connection("doubao")


def visual_copy_fingerprint(context: dict[str, Any], model: str) -> str:
    source = json.dumps({"context": context, "model": model, "version": 1}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]


def _clean_short_text(value: Any, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ，。！？；:：、")
    return text[:maximum].strip(" ，。！？；:：、")


def _layout_choices(context: dict[str, Any], recipe: str) -> list[dict[str, str]]:
    """Read the frozen style contract carried by the workbench context.

    The local fallback keeps existing single-scene calls compatible while the
    batch planner always receives the reviewed pack catalog from the server.
    """
    raw_catalog = context.get("hyperframes_layout_variants") if isinstance(context, dict) else None
    raw_choices = raw_catalog.get(recipe) if isinstance(raw_catalog, dict) else None
    choices: list[dict[str, str]] = []
    for item in (raw_choices if isinstance(raw_choices, list) else DEFAULT_LAYOUT_VARIANTS.get(recipe, ())):
        if not isinstance(item, dict):
            continue
        variant_id = _clean_short_text(item.get("id"), 80)
        motion_id = _clean_short_text(item.get("motion_variant"), 80)
        if variant_id and motion_id and not any(existing["id"] == variant_id for existing in choices):
            choices.append({"id": variant_id, "motion_variant": motion_id})
    if not choices:
        choices.append({"id": "radial_map", "motion_variant": "node_bloom"})
    return choices


def _normalize_layout_variant(raw: dict[str, Any], recipe: str, context: dict[str, Any]) -> dict[str, str]:
    choices = _layout_choices(context, recipe)
    requested = _clean_short_text(raw.get("layout_variant"), 80)
    selected = next((item for item in choices if item["id"] == requested), choices[0])
    # The style-pack owns motion.  Do not let a model invent animation IDs.
    return {"layout_variant": selected["id"], "motion_variant": selected["motion_variant"]}


def _normalize_visual_copy(raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    headline = _clean_short_text(raw.get("headline"), 22)
    center = _clean_short_text(raw.get("center_label") or raw.get("conclusion"), 12)
    source_nodes = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
    nodes: list[str] = []
    for item in source_nodes:
        cleaned = _clean_short_text(item, 12)
        if cleaned and cleaned not in nodes and cleaned not in {headline, center}:
            nodes.append(cleaned)
    recipe = str(raw.get("scene_recipe") or "relationship_map")
    if recipe not in ALLOWED_RECIPES:
        recipe = "relationship_map"
    scene_goal = _clean_short_text(raw.get("scene_goal"), 48)
    if not headline or not center or len(nodes) < 3 or not scene_goal:
        raise TextAIError("AI 提炼结果缺少标题、结论或至少 3 个有效要点，请重新生成")
    return {
        "scene_goal": scene_goal,
        "headline": headline,
        "supporting_statement": _clean_short_text(raw.get("supporting_statement"), 44),
        "center_label": center,
        "nodes": nodes[:4],
        "scene_recipe": recipe,
        **_normalize_layout_variant(raw, recipe, context),
    }


def _normalize_route_graphic_copy(row: dict[str, Any], recipe: str) -> dict[str, Any]:
    """Validate the reviewed words that HyperFrames is allowed to render.

    Routing and copy planning happen in one model call so a batch does not pay
    for a second request per slot.  Non-HyperFrames routes may omit the copy;
    HyperFrames routes must return a real semantic draft rather than forcing
    the renderer to chop up narration text.
    """
    raw = row.get("graphic_copy") if isinstance(row.get("graphic_copy"), dict) else {}
    headline = _clean_short_text(raw.get("headline"), 22)
    scene_goal = _clean_short_text(raw.get("scene_goal"), 48)
    supporting = _clean_short_text(raw.get("supporting_statement"), 44)
    center = _clean_short_text(raw.get("center_label") or raw.get("conclusion"), 12)
    nodes: list[str] = []
    for value in raw.get("nodes") if isinstance(raw.get("nodes"), list) else []:
        cleaned = _clean_short_text(value, 12)
        if cleaned and cleaned not in nodes and cleaned not in {headline, center}:
            nodes.append(cleaned)
    if str(row.get("route") or "") != "hyperframes":
        return {
            "scene_goal": scene_goal,
            "headline": headline,
            "supporting_statement": supporting,
            "center_label": center,
            "nodes": nodes[:4],
        }
    minimum_nodes = {
        "relationship_map": 3,
        "process": 3,
        "comparison": 2,
        "quote_evidence": 2,
        "closing_question": 2,
    }.get(recipe, 0)
    if not scene_goal or not headline:
        raise TextAIError("AI 为 HyperFrames 槽位返回的画面目标或归纳标题不完整")
    if recipe == "relationship_map" and not center:
        raise TextAIError("AI 为关系图返回的核心概念不完整")
    if len(nodes) < minimum_nodes:
        raise TextAIError(f"AI 为 {recipe} 返回的有效要点不足，请重新识别")
    return {
        "scene_goal": scene_goal,
        "headline": headline,
        "supporting_statement": supporting,
        "center_label": center or headline[:12],
        "nodes": nodes[:4],
    }


def plan_visual_copy(context: dict[str, Any]) -> dict[str, Any]:
    system = """你是中文科技短视频的信息设计编辑。你的任务不是改写字幕，而是把当前片段及前后文提炼成可放进动态图形画面的极短文案。
只输出 JSON，不要 Markdown，不要解释：
{
  "scene_goal": "这幅画面要让观众理解的单一结论，最多48字",
  "headline": "经过归纳的画面标题，最多22字",
  "center_label": "关系图中心概念，最多12字",
  "nodes": ["短要点1", "短要点2", "短要点3", "短要点4"],
  "scene_recipe": "headline_statement|relationship_map|single_metric|comparison|process|quote_evidence|closing_question",
  "layout_variant": "从输入的 hyperframes_layout_variants 中选择的版式 ID"
}
要求：
1. 理解前后文，但只表达当前片段的事实，不虚构数据、因果、产品或结论。
2. 必须归纳概念，禁止按字数截断原句，禁止把完整口播直接塞进卡片。
3. 每个 nodes 最多12字，3到4项，互不重复；优先使用名词短语或动作结果。
4. 画面已有独立数字人和字幕，不写主持人提示，不写“欢迎收听”，不生成第二主播。
5. scene_recipe 按信息关系选择；没有明确数字时不要用 single_metric。
6. layout_variant 只能从输入的 hyperframes_layout_variants 中、当前 scene_recipe 对应的列表选择；不要自造名称。
7. 使用简体中文。"""
    raw, model = _chat_json(system, context)
    normalized = _normalize_visual_copy(raw, context)
    normalized["model"] = model
    normalized["generated_at"] = _now()
    normalized["fingerprint"] = visual_copy_fingerprint(context, model)
    return normalized


def visual_route_fingerprint(context: dict[str, Any], model: str) -> str:
    """Identify one AI routing decision without including credentials."""
    source = json.dumps({"context": context, "model": model, "version": 1}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]


def _normalize_visual_routes(raw: dict[str, Any], context: dict[str, Any], *, allow_missing: bool = False) -> dict[str, Any]:
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for scene in context.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        scene_id = str(scene.get("scene_id") or "")
        for slot in scene.get("slots") or []:
            if isinstance(slot, dict) and scene_id and slot.get("block_id"):
                expected[(scene_id, str(slot["block_id"]))] = slot
    if not expected:
        raise TextAIError("没有可供 AI 规划的画面槽位")

    rows = raw.get("blocks") if isinstance(raw.get("blocks"), list) else []
    supplied: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("scene_id") or ""), str(row.get("block_id") or ""))
        if key in expected and key not in supplied:
            supplied[key] = row
    missing_keys = [(scene_id, block_id) for scene_id, block_id in expected if (scene_id, block_id) not in supplied]
    if missing_keys and not allow_missing:
        missing = [f"{scene_id}/{block_id}" for scene_id, block_id in missing_keys]
        raise TextAIError(f"AI 画面规划缺少槽位：{'、'.join(missing[:8])}")

    normalized: list[dict[str, Any]] = []
    for (scene_id, block_id), slot in expected.items():
        row = supplied.get((scene_id, block_id))
        if row is None:
            continue
        route = str(row.get("route") or "")
        if route not in ALLOWED_VISUAL_ROUTES:
            raise TextAIError(f"AI 为 {scene_id}/{block_id} 返回了不支持的画面类型")
        fallback = str(row.get("fallback_route") or "")
        if fallback not in ALLOWED_VISUAL_ROUTES or fallback == route:
            fallback = ""
        try:
            confidence = float(row.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.5
        recipe = str(row.get("scene_recipe") or "relationship_map")
        if recipe not in ALLOWED_RECIPES:
            recipe = "relationship_map"
        graphic_copy = _normalize_route_graphic_copy(row, recipe)
        layout = _normalize_layout_variant(row, recipe, context)
        normalized.append({
            "scene_id": scene_id,
            "block_id": block_id,
            "route": route,
            "visual_intent": _clean_short_text(row.get("visual_intent"), 80),
            "reason": _clean_short_text(row.get("reason"), 100),
            "confidence": round(max(0.0, min(1.0, confidence)), 2),
            "search_query": _clean_short_text(row.get("search_query"), 160),
            "scene_recipe": recipe,
            **layout,
            "graphic_copy": graphic_copy,
            "fallback_route": fallback,
            # Timing is owned by Haike Video. Never trust model-generated cuts.
            "start_seconds": slot.get("start_seconds"),
            "end_seconds": slot.get("end_seconds"),
        })
    summary = _clean_short_text(raw.get("summary"), 160)
    return {
        "summary": summary or "已根据台词语义、时长与画面职责完成智能分配",
        "blocks": normalized,
        "missing": [{"scene_id": scene_id, "block_id": block_id} for scene_id, block_id in missing_keys],
    }


def plan_visual_routes(context: dict[str, Any], *, allow_missing: bool = False) -> dict[str, Any]:
    """Route every pre-cut visual slot to one explicit production engine.

    The model is a planner only. Haike Video owns slot ids/timing, validates
    the response, presents it to the reviewer, and executes only the reviewed
    immutable contract.
    """
    system = """你是中文视频工作台的画面导演。请为已经切好时间的每个画面槽位选择一种生产方式。只输出 JSON，不要 Markdown：
{
  "summary": "本次规划摘要",
  "blocks": [{
    "scene_id": "T001",
    "block_id": "VB-001",
    "route": "stock_video|stock_image|ai_image|hyperframes",
    "visual_intent": "这一格具体应该让观众看到什么",
    "reason": "为什么这种方式最适合当前语境",
    "confidence": 0.0,
    "search_query": "stock 路线使用的简短英文可视化检索词",
    "scene_recipe": "headline_statement|relationship_map|single_metric|comparison|process|quote_evidence|closing_question",
    "layout_variant": "从 hyperframes_layout_variants 中选择当前结构的版式 ID",
    "graphic_copy": {
      "scene_goal": "这一格只需要传达的单一结论，最多48字",
      "headline": "经过归纳的画面标题，最多22字",
      "supporting_statement": "补充判断或证据，最多44字",
      "center_label": "关系图核心或本格概念，最多12字",
      "nodes": ["短要点1", "短要点2", "短要点3", "短要点4"]
    },
    "fallback_route": "失败后建议人工切换的另一种路线"
  }]
}
判断原则：
1. 真实地点、设备运动、制造过程、产品操作优先 stock_video。
2. 稳定的具体物件、档案、单一产品特写可用 stock_image；需要专门构图且事实不会被误画时才用 ai_image。
3. 抽象关系、流程、对比、概念变化、数据结构优先 hyperframes。
4. 每格先根据当前台词、前后文和镜头职责独立判断；同时严格服从 preferences.allowed_routes，不得输出未允许的路线。整片比例由 Haike Video 在返回后按时长复核。
5. 已有独立数字人、模块化字幕和按 story_id 统一的新闻小标题层，主体画面禁止再生成主播；HyperFrames 不烧录字幕，也不得在右上角渲染新闻标题。graphic_copy.headline 仅用于 HY 内部语义构图，不能占用右上角标题安全区。
6. 不得修改 scene_id、block_id、起止时间；必须为输入中的每个槽位恰好返回一项。
7. search_query 使用 3 到 8 个英文实体/动作词，不使用 no people 等负面词。
8. 没有明确数字时不要选择 single_metric。使用简体中文说明理由。
9. 输入中的 slot_text 是当前时间格真正对应的台词，画面文案必须以它为主，整段 transcript 和前后文只用于消歧。
10. route 为 hyperframes 时必须返回 graphic_copy；必须归纳语义，禁止复制整段口播、禁止按字数截断、禁止“内容提示词”等占位词。
11. route 为 hyperframes 时，layout_variant 只能从输入的 hyperframes_layout_variants 中当前 scene_recipe 对应的列表选择。不要自造版式或动效名称。
12. 相邻 HyperFrames 槽位不得机械复用相同标题、节点和版式；只有信息关系确实相同时才可重复，并在 reason 中说明。
13. 不同版式按信息关系选择：单结论用 headline_statement；关系用 relationship_map；数字用 single_metric；两项差异用 comparison；先后步骤用 process；判断与证据用 quote_evidence；收束问题用 closing_question。"""
    raw, model = _chat_json(system, context)
    normalized = _normalize_visual_routes(raw, context, allow_missing=allow_missing)
    normalized["model"] = model
    normalized["generated_at"] = _now()
    normalized["fingerprint"] = visual_route_fingerprint(context, model)
    return normalized
