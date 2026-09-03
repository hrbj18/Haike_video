"""Independent multimodal adapter for evidence-backed material descriptions.

The adapter accepts ordered local evidence frames, never a whole video.  Model
timestamps are intentionally ignored: the application owns time and maps frame IDs
back to the source timeline after schema validation.
"""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw

from backlot.ai_text import (
    _effective_value,
    _parse_json_object,
    _read_env_file,
    _resolved_credentials,
    _safe_provider_error,
    _secrets_path,
)


VISION_PROMPT_VERSION = "material-shot-description-v2"
VISION_SCHEMA_VERSION = 1
MAX_SHOTS_PER_REQUEST = 4
MAX_IMAGES_PER_REQUEST = 12
MAX_SOURCE_IMAGE_BYTES = 32 * 1024 * 1024


class VisionAIError(RuntimeError):
    def __init__(self, message: str, *, status: str = "failed") -> None:
        super().__init__(message)
        self.status = status


def _vision_runtime(provider: str = "default") -> tuple[str, str, str]:
    api_key, base_url, text_model = _resolved_credentials(provider)
    if provider != "default":
        raise VisionAIError("当前视觉理解只支持项目默认 AI 提供方")
    _, values = _read_env_file(_secrets_path())
    model = _effective_value("OPENAI_VISION_MODEL", values) or text_model
    return api_key, base_url, model


def vision_runtime_identity(provider: str = "default") -> dict[str, str]:
    """Return a cache-safe identity without exposing credentials or endpoints."""
    _, _, model = _vision_runtime(provider)
    return {
        "provider": provider,
        "model": model,
        "prompt_version": VISION_PROMPT_VERSION,
        "schema_version": str(VISION_SCHEMA_VERSION),
        "image_detail": "auto",
        "image_longest_edge": "768",
    }


def _jpeg_data_url(path: Path, *, longest_edge: int = 768) -> str:
    source = path.resolve()
    if not source.is_file():
        raise VisionAIError(f"视觉证据帧不存在：{source.name}")
    if source.stat().st_size > MAX_SOURCE_IMAGE_BYTES:
        raise VisionAIError(f"视觉证据帧过大：{source.name}")
    try:
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((longest_edge, longest_edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
    except (OSError, ValueError) as exc:
        raise VisionAIError(f"无法读取视觉证据帧：{source.name}") from exc
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _response_content(response: Any) -> str:
    try:
        data = response.json()
    except Exception as exc:
        raise VisionAIError("视觉服务没有返回可解析的 JSON 响应") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise VisionAIError("视觉服务没有返回可用结果")
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    if not isinstance(content, str) or not content.strip():
        raise VisionAIError("视觉服务返回了空结果")
    return content


def _post_vision_json(
    system_prompt: str,
    content: list[dict[str, Any]],
    *,
    provider: str = "default",
    timeout_seconds: int = 120,
    post: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], str]:
    api_key, base_url, model = _vision_runtime(provider)
    endpoint = f"{(base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions"
    request = {
        "model": model,
        "temperature": 0,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        if post is None:
            import requests

            post = requests.post
        response = post(endpoint, headers=headers, json=request, timeout=max(20, int(timeout_seconds)))
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower():
            raise VisionAIError("视觉服务响应超时，结果状态不明确；系统没有自动重试", status="ambiguous") from exc
        raise VisionAIError(_safe_provider_error(exc)) from exc
    if int(getattr(response, "status_code", 500)) >= 400:
        message = str(getattr(response, "text", "") or "")[:500]
        raise VisionAIError(_safe_provider_error(f"HTTP {response.status_code}: {message}"))
    return _parse_json_object(_response_content(response)), model


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise VisionAIError("视觉描述包含无效置信度") from exc
    if not 0 <= number <= 1:
        raise VisionAIError("视觉描述置信度必须在 0 到 1 之间")
    return round(number, 4)


def _short_text(value: Any, maximum: int, *, required: bool = False) -> str:
    text = " ".join(str(value or "").split()).strip()
    if required and not text:
        raise VisionAIError("视觉描述缺少必要文本字段")
    return text[:maximum]


def _normalize_evidence_items(values: Any, valid_frame_ids: set[str], label: str) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise VisionAIError(f"视觉描述的{label}字段不是数组")
    output: list[dict[str, Any]] = []
    for value in values[:20]:
        if not isinstance(value, dict):
            raise VisionAIError(f"视觉描述的{label}条目无效")
        frame_ids = [str(item) for item in value.get("evidence_frame_ids") or []]
        if not frame_ids or not set(frame_ids).issubset(valid_frame_ids):
            raise VisionAIError(f"视觉描述的{label}引用了未输入的证据帧")
        item = {
            "name": _short_text(value.get("name"), 80, required=True),
            "confidence": _confidence(value.get("confidence")),
            "evidence_frame_ids": list(dict.fromkeys(frame_ids)),
        }
        if label == "主体" and value.get("type"):
            item["type"] = _short_text(value.get("type"), 60)
        if label == "动作" and value.get("subject"):
            item["subject"] = _short_text(value.get("subject"), 80)
        output.append(item)
    return output


def _normalize_description(raw: dict[str, Any], shot: dict[str, Any]) -> dict[str, Any]:
    valid_frame_ids = {str(frame["frame_id"]) for frame in shot.get("frames") or [] if frame.get("selected_for_vision")}
    if not valid_frame_ids:
        raise VisionAIError(f"{shot['shot_id']} 没有送入视觉模型的证据帧")
    state_changes = raw.get("state_changes") if isinstance(raw.get("state_changes"), list) else []
    screen_text = raw.get("screen_text") if isinstance(raw.get("screen_text"), list) else []
    unknowns = raw.get("unknowns") if isinstance(raw.get("unknowns"), list) else []
    quality = raw.get("quality") if isinstance(raw.get("quality"), dict) else {}
    return {
        "summary": _short_text(raw.get("summary"), 240, required=True),
        "entities": _normalize_evidence_items(raw.get("entities"), valid_frame_ids, "主体"),
        "actions": _normalize_evidence_items(raw.get("actions"), valid_frame_ids, "动作"),
        "environment": _short_text(raw.get("environment"), 120),
        "shot_type": _short_text(raw.get("shot_type"), 80),
        "camera_motion": _short_text(raw.get("camera_motion"), 80),
        "state_changes": [_short_text(value, 160) for value in state_changes[:10] if _short_text(value, 160)],
        "screen_text": [_short_text(value, 160) for value in screen_text[:20] if _short_text(value, 160)],
        "quality": {
            "blur": _short_text(quality.get("blur"), 40),
            "occlusion": _short_text(quality.get("occlusion"), 80),
            "notes": _short_text(quality.get("notes"), 160),
        },
        "unknowns": [_short_text(value, 160) for value in unknowns[:10] if _short_text(value, 160)],
        "overall_confidence": _confidence(raw.get("overall_confidence")),
        "evidence_frame_ids": sorted(valid_frame_ids),
    }


SHOT_SYSTEM_PROMPT = """你是视频素材证据分析器。输入由多个镜头及其按时间排序的证据帧组成。
只描述图片直接支持的事实，不根据文件名、脚本或常识补全产品型号、人物身份、因果和用途。
只输出 JSON：
{"shots":[{"shot_id":"SHOT-0001","summary":"镜头事实摘要","entities":[{"name":"主体","type":"类别","confidence":0.0,"evidence_frame_ids":["FRAME-00001"]}],"actions":[{"name":"动作","subject":"主体","confidence":0.0,"evidence_frame_ids":["FRAME-00001"]}],"environment":"环境","shot_type":"景别","camera_motion":"摄像机运动","state_changes":["可观察变化"],"screen_text":["可见文字候选"],"quality":{"blur":"low|medium|high","occlusion":"遮挡情况","notes":"质量说明"},"unknowns":["无法确认的信息"],"overall_confidence":0.0}]}
规则：必须为每个输入 shot_id 恰好返回一项；evidence_frame_ids 只能引用输入帧；不要输出时间戳；无法确认时写入 unknowns。
同一镜头内要跨帧追踪主要运动主体，尽量使用证据支持的通用类别（如机器人、人物、动物、车辆），不要因为主体较小就只写“物体”；若形态仍不足以分类，保留通用描述并在 unknowns 说明。"""


def describe_shots(
    shots: list[dict[str, Any]],
    *,
    provider: str = "default",
    post: Callable[..., Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not shots:
        raise VisionAIError("没有可供视觉理解的镜头")
    descriptions: list[dict[str, Any]] = []
    model = ""
    request_count = 0
    image_count = 0
    batches: list[list[dict[str, Any]]] = []
    batch: list[dict[str, Any]] = []
    batch_images = 0
    for shot in shots:
        shot_images = sum(1 for frame in shot.get("frames") or [] if frame.get("selected_for_vision"))
        if shot_images < 1:
            raise VisionAIError(f"{shot.get('shot_id') or '镜头'} 没有可用证据帧")
        if shot_images > MAX_IMAGES_PER_REQUEST:
            raise VisionAIError(f"{shot.get('shot_id') or '镜头'} 的证据帧超过单请求上限")
        if batch and (len(batch) >= MAX_SHOTS_PER_REQUEST or batch_images + shot_images > MAX_IMAGES_PER_REQUEST):
            batches.append(batch)
            batch = []
            batch_images = 0
        batch.append(shot)
        batch_images += shot_images
    if batch:
        batches.append(batch)

    for batch in batches:
        selected = [frame for shot in batch for frame in shot.get("frames") or [] if frame.get("selected_for_vision")]
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": "下面按 shot_id 和 frame_id 给出证据帧；同一镜头内顺序即时间顺序。",
        }]
        for shot in batch:
            content.append({"type": "text", "text": f"镜头 {shot['shot_id']}"})
            for frame in shot.get("frames") or []:
                if not frame.get("selected_for_vision"):
                    continue
                content.append({"type": "text", "text": f"证据帧 {frame['frame_id']}"})
                content.append({"type": "image_url", "image_url": {"url": _jpeg_data_url(Path(frame["path"])), "detail": "auto"}})
        raw, model = _post_vision_json(SHOT_SYSTEM_PROMPT, content, provider=provider, post=post)
        rows = raw.get("shots") if isinstance(raw.get("shots"), list) else None
        if rows is None:
            raise VisionAIError("视觉服务返回结果缺少 shots 数组")
        supplied = {str(row.get("shot_id")): row for row in rows if isinstance(row, dict)}
        expected = {str(shot["shot_id"]) for shot in batch}
        if set(supplied) != expected or len(rows) != len(expected):
            raise VisionAIError("视觉服务没有为每个输入镜头恰好返回一项描述")
        for shot in batch:
            descriptions.append({"shot_id": shot["shot_id"], **_normalize_description(supplied[shot["shot_id"]], shot)})
        request_count += 1
        image_count += len(selected)
    return descriptions, {
        "provider": provider,
        "model": model,
        "prompt_version": VISION_PROMPT_VERSION,
        "schema_version": VISION_SCHEMA_VERSION,
        "request_count": request_count,
        "image_count": image_count,
    }


def test_vision_ai_connection(*, provider: str = "default", post: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Verify that a relay truly forwards ordered image input using synthetic data."""
    colors = [(220, 35, 35), (35, 170, 70), (40, 90, 220)]
    labels = ["K7", "M4", "R9"]
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": "按图片顺序读取每张图中央的两字符代码和背景主色，只输出 JSON："
                '{"sequence":[{"code":"","color":"red|green|blue"}]}。',
    }]
    for color, label in zip(colors, labels):
        image = Image.new("RGB", (320, 180), color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((105, 55, 215, 125), fill="white")
        draw.text((145, 78), label, fill="black")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92)
        url = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})
    raw, model = _post_vision_json(
        "你是视觉连接测试器。只能根据图片内容作答，不要猜测。",
        content,
        provider=provider,
        timeout_seconds=90,
        post=post,
    )
    sequence = raw.get("sequence") if isinstance(raw.get("sequence"), list) else []
    received_codes = [str(item.get("code") or "").upper() for item in sequence if isinstance(item, dict)]
    received_colors = [str(item.get("color") or "").lower() for item in sequence if isinstance(item, dict)]
    if received_codes != labels or received_colors != ["red", "green", "blue"]:
        raise VisionAIError("AI 服务已响应，但没有通过图片内容与顺序测试")
    return {
        "ok": True,
        "status": "passed",
        "message": "图片输入、多图顺序和结构化输出均可用",
        "provider": provider,
        "model": model,
    }
