"""RunningHub LongCat talking-avatar provider.

The workflow itself is frozen in a local ComfyUI API JSON file.  OpenMontage
only replaces the presenter image and driving-audio inputs, then submits the
published RunningHub workflow id.  Keeping this client separate from the
project orchestration makes paid-task resume and provider mocking testable.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

import requests

from tools.base_tool import (
    BaseTool, DependencyError, Determinism, ExecutionMode, ResourceProfile,
    RetryPolicy, ToolResult, ToolRuntime, ToolStability, ToolTier,
)


DEFAULT_BASE_URL = "https://www.runninghub.cn"
DEFAULT_TEMPLATE_PATH = "config/runninghub/longcat_avatar_api.json"
DEFAULT_WORKFLOW_PROFILE = "longcat_duration_safe"
INFINITETALK_384X480_PROFILE = "infinitetalk_384x480_short"
INFINITETALK_384X480_MANIFEST_PATH = "config/runninghub/InfiniteTalk 工作流 384×480推荐档 V2.manifest.json"
INFINITETALK_448X560_LONG_PROFILE = "infinitetalk_448x560_long"
INFINITETALK_448X560_LONG_TEMPLATE_PATH = "config/runninghub/workflow-2093219950461808641.api.json"
INFINITETALK_448X560_EXACT_CLOCK_PROFILE = "infinitetalk_448x560_exact_clock_v2"
INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID = "2094449979141218305"
INFINITETALK_448X560_EXACT_CLOCK_TEMPLATE_PATH = (
    "config/runninghub/workflow-2094449979141218305.api.json"
)
INFINITETALK_448X560_LEGACY_WORKFLOW_ID = "2093219950461808641"
INFINITETALK_EXACT_FRAMES_NODE_ID = "35"
INFINITETALK_EXACT_FRAMES_FIELD = "value"
PRESENTER_NODE_ID = "176"
PRESENTER_FIELD = "image"
AUDIO_NODE_ID = "524"
AUDIO_FIELD = "audio"
OUTPUT_NODE_ID = "352"
DURATION_MS_NODE_ID = "556"
DURATION_SECONDS_NODE_ID = "529"
ALIGNED_FRAME_COUNT_NODE_ID = "531"
RAW_FRAME_COUNT_NODE_ID = "532"
SEGMENT_FRAME_COUNT_NODE_ID = "471"
OVERLAP_FRAME_COUNT_NODE_ID = "148"
CONTINUATION_COUNT_NODE_ID = "546"
PREVIEW_OUTPUT_NODE_ID = "292"
DURATION_SECONDS_EXPRESSION = "a / 1000.0"
CONTINUATION_COUNT_EXPRESSION = "max(0, ceil((a - b) / (b - c)))"
# Published workflows exported before this repair do not have node 546.c wired.
# API submissions therefore use the equivalent expression with the frozen
# overlap value.  The local repaired workflow keeps the graph-driven version.
REMOTE_CONTINUATION_COUNT_EXPRESSION = "max(0, ceil((a - b) / (b - 13)))"


class RunningHubAvatarError(RuntimeError):
    """A safe, Chinese provider error suitable for the workbench UI."""


def _message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("msg", "message", "detail", "errorMessage", "error"):
            if payload.get(key):
                return str(payload[key])[:500]
    return fallback


def _safe_failure_details(payload: Any) -> dict[str, Any]:
    """Keep actionable V2 failure evidence without provider traces or URLs."""
    if not isinstance(payload, dict):
        return {}
    failed = payload.get("failedReason") if isinstance(payload.get("failedReason"), dict) else {}
    details = {
        "error_code": str(payload.get("errorCode") or "")[:80],
        "error_message": str(payload.get("errorMessage") or "")[:500],
        "exception_type": str(failed.get("exception_type") or "")[:160],
        "node_id": str(failed.get("node_id") or "")[:80],
        "node_name": str(failed.get("node_name") or "")[:160],
        "exception_message": str(failed.get("exception_message") or "")[:500],
    }
    return {key: value for key, value in details.items() if value}


def _failure_message(payload: Any, fallback: str) -> tuple[str, dict[str, Any]]:
    details = _safe_failure_details(payload)
    summary = str(details.get("error_message") or _message(payload, fallback))
    exception_type = str(details.get("exception_type") or "")
    node_name = str(details.get("node_name") or details.get("node_id") or "")
    if exception_type:
        summary = f"{summary}（{exception_type}{f'，节点 {node_name}' if node_name else ''}）"
    return summary[:500], details


def _first_numeric(payload: Any, keys: tuple[str, ...]) -> float | None:
    """Find one provider billing value without depending on response nesting.

    RunningHub's consumer and enterprise query responses expose billing fields
    at different levels.  Returning the first finite non-negative value keeps
    the caller's budget ledger useful while preserving the raw response for
    audit and future schema changes.
    """
    if isinstance(payload, dict):
        for key in keys:
            if key not in payload:
                continue
            try:
                value = float(payload[key])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                return value
        for value in payload.values():
            found = _first_numeric(value, keys)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _first_numeric(value, keys)
            if found is not None:
                return found
    return None


def _usage_dict(payload: Any) -> dict[str, Any]:
    """Return the documented task usage object without guessing at nesting.

    The V2 query response documents ``usage`` beside the task result.  Older
    code recursively searched *every* numeric field, which made it possible to
    label an unrelated number as a CNY charge.  Keep the original provider
    values and let callers display the evidence verbatim.
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    source = data if isinstance(data, dict) else payload
    usage = source.get("usage") if isinstance(source, dict) else None
    if isinstance(usage, dict):
        return dict(usage)
    # V1/early V2 responses put the same documented fields directly on the
    # task object.  Support that explicit legacy shape, but never recurse into
    # arbitrary results or nested metadata.
    if isinstance(source, dict):
        return {
            key: source.get(key)
            for key in ("consumeMoney", "consumeCoins", "thirdPartyConsumeMoney", "taskCostTime")
            if key in source
        }
    return {}


def _numeric_usage(usage: dict[str, Any], key: str) -> float | None:
    try:
        value = float(usage.get(key))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def billing_evidence(payload: Any) -> dict[str, Any]:
    """Normalize only documented RunningHub usage fields for audit.

    ``consumeMoney`` plus ``taskCostTime`` are returned by the provider.  The
    official enterprise page states Lite/Standard/Plus are billed by seconds,
    so their ratio gives a strong *observed* machine classification even when
    the query response does not contain an explicit ``instanceType`` field.
    We never pretend the inferred class was directly returned by the provider.
    """
    usage = _usage_dict(payload)
    money = _numeric_usage(usage, "consumeMoney")
    seconds = _numeric_usage(usage, "taskCostTime")
    third_party = _numeric_usage(usage, "thirdPartyConsumeMoney")
    coins = _numeric_usage(usage, "consumeCoins")
    hourly_rate = (money * 3600.0 / seconds) if money is not None and seconds and seconds > 0 else None
    inferred = "unverified"
    if hourly_rate is not None:
        known = {"lite": 0.4, "standard_24gb": 4.0, "plus_48gb": 6.0}
        name, rate = min(known.items(), key=lambda item: abs(hourly_rate - item[1]))
        if abs(hourly_rate - rate) <= max(0.03, rate * 0.03):
            inferred = name
    return {
        "provider_usage": {
            "consume_money": money,
            "consume_coins": coins,
            "third_party_consume_money": third_party,
            "task_cost_seconds": seconds,
        },
        "observed_hourly_rate_cny": round(hourly_rate, 4) if hourly_rate is not None else None,
        "observed_instance": inferred,
        "instance_evidence": "provider_usage_rate" if inferred != "unverified" else "provider_did_not_report_resolvable_rate",
        "provider_reported_instance": None,
    }


def _read_workflow_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RunningHubAvatarError(f"RunningHub 工作流模板不存在：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunningHubAvatarError("RunningHub 工作流模板不是有效的 API JSON") from exc
    if not isinstance(value, dict):
        raise RunningHubAvatarError("RunningHub 工作流模板顶层必须是节点对象")
    return value


def _require_node(workflow: dict[str, Any], node_id: str, class_type: str) -> dict[str, Any]:
    node = workflow.get(node_id)
    if not isinstance(node, dict) or node.get("class_type") != class_type:
        raise RunningHubAvatarError(f"工作流缺少已约定的节点 {node_id}（{class_type}）")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise RunningHubAvatarError(f"工作流节点 {node_id} 缺少 inputs")
    return node


def validate_longcat_workflow_template(value: dict[str, Any]) -> None:
    """Reject unsafe duration graphs before any paid task can be submitted."""
    presenter = value.get(PRESENTER_NODE_ID)
    audio = value.get(AUDIO_NODE_ID)
    output = value.get(OUTPUT_NODE_ID)
    if not isinstance(presenter, dict) or presenter.get("class_type") != "LoadImage" or PRESENTER_FIELD not in (presenter.get("inputs") or {}):
        raise RunningHubAvatarError("工作流缺少已约定的人物图输入节点 176.image")
    if not isinstance(audio, dict) or audio.get("class_type") != "LoadAudio" or AUDIO_FIELD not in (audio.get("inputs") or {}):
        raise RunningHubAvatarError("工作流缺少已约定的驱动音频输入节点 524.audio")
    if not isinstance(output, dict) or output.get("class_type") != "VHS_VideoCombine":
        raise RunningHubAvatarError("工作流缺少已约定的视频输出节点 352")

    duration_ms = _require_node(value, DURATION_MS_NODE_ID, "Audio Duration (mtb)")
    duration_seconds = _require_node(value, DURATION_SECONDS_NODE_ID, "MathExpression|pysssss")
    raw_frames = _require_node(value, RAW_FRAME_COUNT_NODE_ID, "SimpleMath+")
    aligned_frames = _require_node(value, ALIGNED_FRAME_COUNT_NODE_ID, "MathExpression|pysssss")
    segment_frames = _require_node(value, SEGMENT_FRAME_COUNT_NODE_ID, "INTConstant")
    overlap_frames = _require_node(value, OVERLAP_FRAME_COUNT_NODE_ID, "INTConstant")
    continuation_count = _require_node(value, CONTINUATION_COUNT_NODE_ID, "MathExpression|pysssss")
    preview_output = _require_node(value, PREVIEW_OUTPUT_NODE_ID, "VHS_VideoCombine")

    if duration_ms["inputs"].get("audio") != [AUDIO_NODE_ID, 0]:
        raise RunningHubAvatarError("工作流音频时长节点 556 未连接到 524.audio")
    if duration_seconds["inputs"].get("a") != [DURATION_MS_NODE_ID, 0]:
        raise RunningHubAvatarError("工作流节点 529 未读取毫秒时长")
    if str(duration_seconds["inputs"].get("expression") or "").replace(" ", "") != "a/1000.0":
        raise RunningHubAvatarError("工作流节点 529 必须把毫秒除以 1000 后再作为秒使用")
    if raw_frames["inputs"].get("a") != [DURATION_SECONDS_NODE_ID, 1]:
        raise RunningHubAvatarError("工作流总帧数节点 532 未读取秒制音频时长")
    if raw_frames["inputs"].get("b") != ["526", 0]:
        raise RunningHubAvatarError("工作流总帧数节点 532 未连接输出帧率")
    if aligned_frames["inputs"].get("a") != [RAW_FRAME_COUNT_NODE_ID, 0]:
        raise RunningHubAvatarError("工作流帧数对齐节点 531 连接错误")

    segment = segment_frames["inputs"].get("value")
    overlap = overlap_frames["inputs"].get("value")
    if not isinstance(segment, int) or not isinstance(overlap, int) or segment <= overlap or overlap <= 0:
        raise RunningHubAvatarError("LongCat 分段帧数必须大于重叠帧数，且两者必须为正整数")
    continuation_inputs = continuation_count["inputs"]
    if continuation_inputs.get("a") != [ALIGNED_FRAME_COUNT_NODE_ID, 0] or continuation_inputs.get("b") != [SEGMENT_FRAME_COUNT_NODE_ID, 0] or continuation_inputs.get("c") != [OVERLAP_FRAME_COUNT_NODE_ID, 0]:
        raise RunningHubAvatarError("工作流续段次数节点 546 未同时读取总帧数、单段帧数和重叠帧数")
    if str(continuation_inputs.get("expression") or "").replace(" ", "") != CONTINUATION_COUNT_EXPRESSION.replace(" ", ""):
        raise RunningHubAvatarError("工作流续段次数没有按扣除重叠帧后的有效新增帧数计算")

    output_inputs = output.get("inputs") or {}
    if output_inputs.get("audio") != ["528", 1] or output_inputs.get("trim_to_audio") is not True:
        raise RunningHubAvatarError("最终输出节点 352 必须连接原始音频并启用 trim_to_audio")
    if preview_output["inputs"].get("trim_to_audio") is not True:
        raise RunningHubAvatarError("首段预览节点 292 必须启用 trim_to_audio")


def repair_longcat_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return a repaired copy of the known LongCat 1.5 API workflow."""
    repaired = copy.deepcopy(workflow)
    _require_node(repaired, DURATION_MS_NODE_ID, "Audio Duration (mtb)")
    duration_seconds = _require_node(repaired, DURATION_SECONDS_NODE_ID, "MathExpression|pysssss")
    continuation = _require_node(repaired, CONTINUATION_COUNT_NODE_ID, "MathExpression|pysssss")
    preview = _require_node(repaired, PREVIEW_OUTPUT_NODE_ID, "VHS_VideoCombine")
    output = _require_node(repaired, OUTPUT_NODE_ID, "VHS_VideoCombine")

    duration_seconds["inputs"]["expression"] = DURATION_SECONDS_EXPRESSION
    duration_seconds.setdefault("_meta", {})["title"] = "自动换算音频总时长：毫秒 ÷ 1000 = 秒（请勿修改）"
    continuation["inputs"].update({
        "expression": CONTINUATION_COUNT_EXPRESSION,
        "c": [OVERLAP_FRAME_COUNT_NODE_ID, 0],
    })
    continuation.setdefault("_meta", {})["title"] = "按有效新增帧数自动计算续段次数（自动）"
    preview["inputs"]["trim_to_audio"] = True
    output["inputs"]["trim_to_audio"] = True
    output.setdefault("_meta", {})["title"] = "最终输出：严格按音频时长裁切的 MP4（自动）"
    validate_longcat_workflow_template(repaired)
    return repaired


def repair_longcat_workflow_template(path: Path) -> dict[str, Any]:
    return repair_longcat_workflow(_read_workflow_json(path))


def load_longcat_workflow_template(path: Path) -> dict[str, Any]:
    """Load the immutable, duration-safe workflow contract."""
    value = _read_workflow_json(path)
    validate_longcat_workflow_template(value)
    return value


def longcat_duration_plan(
    audio_duration_seconds: float,
    *,
    fps: float = 25.0,
    segment_frames: int = 101,
    overlap_frames: int = 13,
) -> dict[str, Any]:
    """Mirror the repaired graph's frame planning for tests and UI diagnostics."""
    duration = float(audio_duration_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise RunningHubAvatarError("音频时长必须是大于 0 的有限秒数")
    if not math.isfinite(float(fps)) or fps <= 0:
        raise RunningHubAvatarError("输出帧率必须大于 0")
    if segment_frames <= overlap_frames or overlap_frames <= 0:
        raise RunningHubAvatarError("单段帧数必须大于重叠帧数")
    raw_frames = duration * float(fps)
    aligned_frames = int(((raw_frames + 3) // 4) * 4 + 1)
    effective_new_frames = segment_frames - overlap_frames
    continuation_count = max(0, math.ceil((aligned_frames - segment_frames) / effective_new_frames))
    generated_frames = segment_frames + continuation_count * effective_new_frames
    return {
        "audio_duration_seconds": duration,
        "fps": float(fps),
        "raw_frames": raw_frames,
        "aligned_frames": aligned_frames,
        "segment_frames": segment_frames,
        "overlap_frames": overlap_frames,
        "effective_new_frames": effective_new_frames,
        "continuation_count": continuation_count,
        "generated_frames_before_trim": generated_frames,
        "generated_duration_before_trim": generated_frames / float(fps),
        "expected_output_duration": duration,
    }


def workflow_template_sha256(path: Path) -> str:
    load_longcat_workflow_template(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_workflow_profile(value: object) -> str:
    profile = str(value or DEFAULT_WORKFLOW_PROFILE).strip().lower()
    if profile not in {
        DEFAULT_WORKFLOW_PROFILE,
        INFINITETALK_384X480_PROFILE,
        INFINITETALK_448X560_LONG_PROFILE,
        INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
    }:
        raise RunningHubAvatarError(f"不支持的 RunningHub 工作流配置：{profile}")
    return profile


def _validate_workflow_profile_binding(workflow_id: str, workflow_profile: str) -> None:
    """Prevent a published exact-clock graph from receiving a legacy payload."""
    expected_for_known_id = {
        INFINITETALK_448X560_LEGACY_WORKFLOW_ID: INFINITETALK_448X560_LONG_PROFILE,
        INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID: INFINITETALK_448X560_EXACT_CLOCK_PROFILE,
    }.get(str(workflow_id or "").strip())
    if expected_for_known_id and workflow_profile != expected_for_known_id:
        raise RunningHubAvatarError(
            f"RunningHub 工作流 {workflow_id} 必须使用配置档 {expected_for_known_id}"
        )
    if (
        workflow_profile == INFINITETALK_448X560_EXACT_CLOCK_PROFILE
        and str(workflow_id or "").strip() != INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID
    ):
        raise RunningHubAvatarError(
            "精确帧时钟配置档只能用于已验收的新 RunningHub 工作流 "
            f"{INFINITETALK_448X560_EXACT_CLOCK_WORKFLOW_ID}"
        )


def _validate_infinitetalk_448x560_long_template(path: Path) -> str:
    """Validate the frozen long-audio workflow before a paid submission."""
    workflow = _read_workflow_json(path)
    image = _require_node(workflow, "36", "LoadImage")
    audio = _require_node(workflow, "34", "LoadAudio")
    scale = _require_node(workflow, "2", "ImageScale")
    split_audio = _require_node(workflow, "8", "AudioSeparation")
    generator = _require_node(workflow, "14", "WanVideoImageToVideoMultiTalk")
    output = _require_node(workflow, "24", "VHS_VideoCombine")

    if "image" not in image["inputs"] or "audio" not in audio["inputs"]:
        raise RunningHubAvatarError("InfiniteTalk 长音频工作流缺少人物图或驱动音频输入")
    if scale["inputs"].get("image") != ["36", 0]:
        raise RunningHubAvatarError("InfiniteTalk 长音频工作流没有读取人物图节点 36")
    if int(scale["inputs"].get("width") or 0) != 448 or int(scale["inputs"].get("height") or 0) != 560:
        raise RunningHubAvatarError("InfiniteTalk 长音频工作流必须输出 448×560 的 4:5 画面")
    if split_audio["inputs"].get("audio") != ["34", 0] or float(split_audio["inputs"].get("chunk_length") or 0) <= 0:
        raise RunningHubAvatarError("InfiniteTalk 长音频工作流的自动音频分块连接错误")
    if str(generator["inputs"].get("mode") or "") != "infinitetalk":
        raise RunningHubAvatarError("InfiniteTalk 长音频工作流生成模式错误")
    output_inputs = output["inputs"]
    if output_inputs.get("audio") != ["34", 0] or output_inputs.get("trim_to_audio") is not True:
        raise RunningHubAvatarError("InfiniteTalk 长音频输出必须连接原始音频并按音频裁切")
    if int(output_inputs.get("frame_rate") or 0) != 25 or str(output_inputs.get("format") or "") != "video/h264-mp4":
        raise RunningHubAvatarError("InfiniteTalk 长音频输出必须为 25 FPS H.264 MP4")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_infinitetalk_448x560_exact_clock_template(path: Path) -> str:
    """Validate the deployed 25 FPS exact-clock graph before paid use."""
    workflow = _read_workflow_json(path)
    for removed_node in ("8", "38", "41"):
        if removed_node in workflow:
            raise RunningHubAvatarError(
                f"InfiniteTalk 精确帧工作流仍包含旧时长节点 {removed_node}"
            )
    image = _require_node(workflow, "36", "LoadImage")
    audio = _require_node(workflow, "34", "LoadAudio")
    frame_input = _require_node(workflow, INFINITETALK_EXACT_FRAMES_NODE_ID, "JWInteger")
    scale = _require_node(workflow, "2", "ImageScale")
    audio_features = _require_node(workflow, "18", "MultiTalkWav2VecEmbeds")
    generator = _require_node(workflow, "14", "WanVideoImageToVideoMultiTalk")
    sampler = _require_node(workflow, "13", "WanVideoSampler")
    output = _require_node(workflow, "24", "VHS_VideoCombine")

    if "image" not in image["inputs"] or "audio" not in audio["inputs"]:
        raise RunningHubAvatarError("InfiniteTalk 精确帧工作流缺少人物图或驱动音频输入")
    if scale["inputs"].get("image") != ["36", 0]:
        raise RunningHubAvatarError("InfiniteTalk 精确帧工作流没有读取人物图节点 36")
    if int(scale["inputs"].get("width") or 0) != 448 or int(scale["inputs"].get("height") or 0) != 560:
        raise RunningHubAvatarError("InfiniteTalk 精确帧工作流必须输出 448×560 的 4:5 画面")
    placeholder = frame_input["inputs"].get(INFINITETALK_EXACT_FRAMES_FIELD)
    if isinstance(placeholder, bool) or not isinstance(placeholder, int) or placeholder <= 0:
        raise RunningHubAvatarError("InfiniteTalk 精确帧工作流的节点 35 必须是正整数帧数入口")
    if audio_features["inputs"].get("audio_1") != ["34", 0]:
        raise RunningHubAvatarError("InfiniteTalk 精确帧工作流的口型音频必须直接来自节点 34")
    if audio_features["inputs"].get("num_frames") != [INFINITETALK_EXACT_FRAMES_NODE_ID, 0]:
        raise RunningHubAvatarError("InfiniteTalk 精确帧工作流的总帧数必须只来自节点 35")
    if int(audio_features["inputs"].get("fps") or 0) != 25:
        raise RunningHubAvatarError("InfiniteTalk 精确帧工作流的音频特征必须按 25 FPS 生成")
    generator_inputs = generator["inputs"]
    if (
        str(generator_inputs.get("mode") or "") != "infinitetalk"
        or int(generator_inputs.get("frame_window_size") or 0) != 81
        or int(generator_inputs.get("motion_frame") or 0) != 9
    ):
        raise RunningHubAvatarError("InfiniteTalk 精确帧工作流必须保留 81 帧窗口和 9 帧衔接")
    if int(sampler["inputs"].get("steps") or 0) != 4:
        raise RunningHubAvatarError("InfiniteTalk 精确帧工作流必须使用已验收的 4 步采样")
    output_inputs = output["inputs"]
    if output_inputs.get("audio") != ["34", 0] or output_inputs.get("trim_to_audio") is not True:
        raise RunningHubAvatarError("InfiniteTalk 精确帧输出必须复用节点 34 原音频并按音频裁切")
    if int(output_inputs.get("frame_rate") or 0) != 25 or str(output_inputs.get("format") or "") != "video/h264-mp4":
        raise RunningHubAvatarError("InfiniteTalk 精确帧输出必须为 25 FPS H.264 MP4")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_infinitetalk_manifest(path: Path, *, repo_root: Path) -> tuple[str, Path]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunningHubAvatarError("InfiniteTalk 384×480 工作流清单不可读") from exc
    inputs = manifest.get("input_nodes") if isinstance(manifest.get("input_nodes"), dict) else {}
    render = manifest.get("render") if isinstance(manifest.get("render"), dict) else {}
    if inputs != {"image": "36", "audio": "34"} or str(manifest.get("output_node") or "") != "24":
        raise RunningHubAvatarError("InfiniteTalk 384×480 工作流输入节点清单与已发布工作流不一致")
    if int(render.get("width") or 0) != 384 or int(render.get("height") or 0) != 480:
        raise RunningHubAvatarError("InfiniteTalk 工作流必须输出 384×480")
    if int(render.get("max_audio_seconds") or 0) != 10:
        raise RunningHubAvatarError("InfiniteTalk 短测工作流必须限制为10秒音频")
    project_path = Path(str(manifest.get("project_path") or ""))
    if not project_path.is_absolute():
        project_path = repo_root / project_path
    if not project_path.is_file():
        raise RunningHubAvatarError("InfiniteTalk 384×480 项目模板不存在")
    return hashlib.sha256(project_path.read_bytes()).hexdigest(), project_path


def runninghub_configuration(*, repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or Path(__file__).resolve().parents[2]
    try:
        workflow_profile = _normalized_workflow_profile(os.environ.get("RUNNINGHUB_WORKFLOW_PROFILE"))
    except RunningHubAvatarError as exc:
        workflow_profile = str(os.environ.get("RUNNINGHUB_WORKFLOW_PROFILE") or "")
        profile_issue = str(exc)
    else:
        profile_issue = ""
    if workflow_profile == INFINITETALK_384X480_PROFILE:
        default_template = INFINITETALK_384X480_MANIFEST_PATH
    elif workflow_profile == INFINITETALK_448X560_EXACT_CLOCK_PROFILE:
        default_template = INFINITETALK_448X560_EXACT_CLOCK_TEMPLATE_PATH
    elif workflow_profile == INFINITETALK_448X560_LONG_PROFILE:
        default_template = INFINITETALK_448X560_LONG_TEMPLATE_PATH
    else:
        default_template = DEFAULT_TEMPLATE_PATH
    raw_template = str(os.environ.get("RUNNINGHUB_WORKFLOW_TEMPLATE") or default_template).strip()
    template = Path(raw_template)
    if not template.is_absolute():
        template = root / template
    api_key = str(os.environ.get("RUNNINGHUB_API_KEY") or "").strip()
    workflow_id = str(os.environ.get("RUNNINGHUB_WORKFLOW_ID") or "").strip()
    issues: list[str] = []
    if profile_issue:
        issues.append(profile_issue)
    if not api_key:
        issues.append("缺少 RUNNINGHUB_API_KEY")
    if not workflow_id:
        issues.append("缺少 RUNNINGHUB_WORKFLOW_ID（发布工作流后取得的编号）")
    elif not profile_issue:
        try:
            _validate_workflow_profile_binding(workflow_id, workflow_profile)
        except RunningHubAvatarError as exc:
            issues.append(str(exc))
    template_hash = None
    try:
        if workflow_profile == INFINITETALK_384X480_PROFILE:
            template_hash, validated_template = _validate_infinitetalk_manifest(template, repo_root=root)
            template = validated_template
        elif workflow_profile == INFINITETALK_448X560_EXACT_CLOCK_PROFILE:
            template_hash = _validate_infinitetalk_448x560_exact_clock_template(template)
        elif workflow_profile == INFINITETALK_448X560_LONG_PROFILE:
            template_hash = _validate_infinitetalk_448x560_long_template(template)
        else:
            template_hash = workflow_template_sha256(template)
    except RunningHubAvatarError as exc:
        issues.append(str(exc))
    return {
        "configured": not issues,
        "api_key_configured": bool(api_key),
        "workflow_id_configured": bool(workflow_id),
        "workflow_id": workflow_id,
        "workflow_id_suffix": workflow_id[-6:] if workflow_id else None,
        "workflow_profile": workflow_profile,
        "base_url": str(os.environ.get("RUNNINGHUB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        "template_path": str(template),
        "template_sha256": template_hash,
        "issues": issues,
    }


class RunningHubLongCatClient:
    """Minimal native client for RunningHub's asynchronous workflow API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        workflow_id: str | None = None,
        base_url: str | None = None,
        session: requests.Session | None = None,
        workflow_profile: str | None = None,
    ):
        self.api_key = str(
            api_key if api_key is not None else (os.environ.get("RUNNINGHUB_API_KEY") or "")
        ).strip()
        self.workflow_id = str(
            workflow_id if workflow_id is not None else (os.environ.get("RUNNINGHUB_WORKFLOW_ID") or "")
        ).strip()
        self.base_url = str(
            base_url if base_url is not None else (os.environ.get("RUNNINGHUB_BASE_URL") or DEFAULT_BASE_URL)
        ).rstrip("/")
        inferred_profile = (
            workflow_profile
            if workflow_profile is not None
            else (DEFAULT_WORKFLOW_PROFILE if workflow_id is not None else os.environ.get("RUNNINGHUB_WORKFLOW_PROFILE"))
        )
        self.workflow_profile = _normalized_workflow_profile(inferred_profile)
        if not self.api_key:
            raise RunningHubAvatarError("尚未配置 RUNNINGHUB_API_KEY")
        if not self.workflow_id:
            raise RunningHubAvatarError("尚未配置 RUNNINGHUB_WORKFLOW_ID；API JSON 本身不包含已发布工作流编号")
        _validate_workflow_profile_binding(self.workflow_id, self.workflow_profile)
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _json(response: requests.Response, action: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RunningHubAvatarError(f"RunningHub {action}返回了无效数据") from exc
        if not response.ok:
            raise RunningHubAvatarError(f"RunningHub {action}失败：{_message(payload, f'HTTP {response.status_code}')}")
        if not isinstance(payload, dict):
            raise RunningHubAvatarError(f"RunningHub {action}返回了无效数据")
        code = payload.get("code")
        if code not in (None, 0, 200, "0", "200", "SUCCESS", "success"):
            raise RunningHubAvatarError(f"RunningHub {action}失败：{_message(payload, str(code))}")
        return payload

    def upload_file(self, path: Path, *, file_type: str) -> str:
        if not path.is_file():
            raise RunningHubAvatarError("待上传的数字人输入文件不存在")
        if path.stat().st_size > 30 * 1024 * 1024:
            raise RunningHubAvatarError("RunningHub 单个上传文件不能超过 30 MB")
        with path.open("rb") as source:
            response = self.session.post(
                f"{self.base_url}/openapi/v2/media/upload/binary",
                headers=self._headers(),
                files={"file": (path.name, source)},
                timeout=180,
            )
        payload = self._json(response, "上传文件")
        data = payload.get("data")
        if isinstance(data, dict):
            filename = data.get("fileName") or data.get("filename") or data.get("name")
        else:
            filename = data if isinstance(data, str) else None
        if not filename:
            raise RunningHubAvatarError("RunningHub 上传成功但未返回文件名")
        remote_type = str(data.get("type") or "").lower() if isinstance(data, dict) else ""
        if remote_type and file_type and remote_type != file_type.lower():
            raise RunningHubAvatarError(f"RunningHub 将上传文件识别为 {remote_type}，预期为 {file_type}")
        return str(filename)

    @staticmethod
    def node_info_list(
        *,
        presenter_filename: str,
        audio_filename: str,
        workflow_profile: str = DEFAULT_WORKFLOW_PROFILE,
        exact_total_frames: int | None = None,
    ) -> list[dict[str, Any]]:
        """Build user inputs plus non-negotiable duration safety overrides.

        The safety fields also protect calls that still reference an older
        published RunningHub workflow.  They are intentionally sent on every
        task instead of relying on the remote workflow having been republished.
        """
        profile = _normalized_workflow_profile(workflow_profile)
        if profile == INFINITETALK_448X560_EXACT_CLOCK_PROFILE:
            if (
                isinstance(exact_total_frames, bool)
                or not isinstance(exact_total_frames, int)
                or exact_total_frames <= 0
            ):
                raise RunningHubAvatarError(
                    "InfiniteTalk 精确帧工作流必须提供由最终 WAV 采样数计算的正整数总帧数"
                )
            return [
                {"nodeId": "36", "fieldName": "image", "fieldValue": presenter_filename},
                {"nodeId": "34", "fieldName": "audio", "fieldValue": audio_filename},
                {
                    "nodeId": INFINITETALK_EXACT_FRAMES_NODE_ID,
                    "fieldName": INFINITETALK_EXACT_FRAMES_FIELD,
                    "fieldValue": exact_total_frames,
                },
                {"nodeId": "24", "fieldName": "trim_to_audio", "fieldValue": True},
            ]
        if exact_total_frames is not None:
            raise RunningHubAvatarError("只有 InfiniteTalk 精确帧工作流可以接收总帧数覆盖")
        if profile in {INFINITETALK_384X480_PROFILE, INFINITETALK_448X560_LONG_PROFILE}:
            return [
                {"nodeId": "36", "fieldName": "image", "fieldValue": presenter_filename},
                {"nodeId": "34", "fieldName": "audio", "fieldValue": audio_filename},
                {"nodeId": "24", "fieldName": "trim_to_audio", "fieldValue": True},
            ]
        return [
            {"nodeId": PRESENTER_NODE_ID, "fieldName": PRESENTER_FIELD, "fieldValue": presenter_filename},
            {"nodeId": AUDIO_NODE_ID, "fieldName": AUDIO_FIELD, "fieldValue": audio_filename},
            {"nodeId": DURATION_SECONDS_NODE_ID, "fieldName": "expression", "fieldValue": DURATION_SECONDS_EXPRESSION},
            {"nodeId": CONTINUATION_COUNT_NODE_ID, "fieldName": "expression", "fieldValue": REMOTE_CONTINUATION_COUNT_EXPRESSION},
            {"nodeId": PREVIEW_OUTPUT_NODE_ID, "fieldName": "trim_to_audio", "fieldValue": True},
            {"nodeId": OUTPUT_NODE_ID, "fieldName": "trim_to_audio", "fieldValue": True},
        ]

    def submit(
        self,
        *,
        presenter_filename: str,
        audio_filename: str,
        instance_type: str | None = None,
        exact_total_frames: int | None = None,
    ) -> dict[str, Any]:
        # RunningHub 的公开文档只把 ``default`` 和 ``plus`` 作为显式规格，
        # Lite 则描述为“由系统算法自动调度”。因此 Lite 请求必须彻底省略
        # ``instanceType``；不能发送空串、null 或未公开的 ``lite`` 值。
        # 省略字段仍不能替代账单核验，上层必须以实际 0.4 元/小时为准。
        normalized_instance = str(instance_type or "").strip().lower()
        if normalized_instance not in {"", "default", "plus"}:
            raise RunningHubAvatarError("RunningHub 实例类型只支持 default（Standard 24GB）或 plus（48GB）；Lite 必须省略该字段")
        request_body: dict[str, Any] = {
            "apiKey": self.api_key,
            "workflowId": self.workflow_id,
            "nodeInfoList": self.node_info_list(
                presenter_filename=presenter_filename,
                audio_filename=audio_filename,
                workflow_profile=self.workflow_profile,
                exact_total_frames=exact_total_frames,
            ),
        }
        if normalized_instance:
            # RunningHub only honors instanceType for enterprise-shared keys.
            # ``default`` explicitly selects the Standard 24GB billed runtime.
            request_body["instanceType"] = normalized_instance
        else:
            # Keep this invariant visible and testable.  A fresh request body
            # currently has no such key, but pop protects against future
            # request-body refactors that accidentally retain ``default``.
            request_body.pop("instanceType", None)
        response = self.session.post(
            f"{self.base_url}/task/openapi/create",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=request_body,
            timeout=90,
        )
        payload = self._json(response, "提交付费任务")
        data = payload.get("data")
        task_id = None
        if isinstance(data, dict):
            task_id = data.get("taskId") or data.get("task_id")
        elif isinstance(data, str):
            task_id = data
        task_id = task_id or payload.get("taskId") or payload.get("task_id")
        if not task_id:
            prompt_tips = payload.get("promptTips") or (data.get("promptTips") if isinstance(data, dict) else None)
            suffix = f"：{prompt_tips}" if prompt_tips else ""
            raise RunningHubAvatarError(f"RunningHub 未返回任务编号{suffix}")
        return {
            "task_id": str(task_id),
            "raw": payload,
            "request_contract": {
                "keys": sorted(key for key in request_body if key != "apiKey"),
                "instance_type_present": "instanceType" in request_body,
                "workflow_profile": self.workflow_profile,
                "instance_type_value": request_body.get("instanceType"),
                "exact_total_frames": exact_total_frames,
            },
        }

    def poll(self, task_id: str) -> dict[str, Any]:
        """Query the maintained V2 endpoint for both state and result.

        RunningHub has stopped maintaining the legacy ``status`` and
        ``outputs`` endpoints.  V2 returns the task state and final result in
        one response, so a resumed OpenMontage task does not need a second
        provider request after completion.
        """
        response = self.session.post(
            f"{self.base_url}/openapi/v2/query",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"taskId": task_id},
            timeout=60,
        )
        payload = self._json(response, "查询任务")
        data = payload.get("data")
        status_source = data if isinstance(data, dict) else payload
        raw_status = str(
            status_source.get("status")
            or status_source.get("taskStatus")
            or status_source.get("task_status")
            or status_source.get("state")
            or "RUNNING"
        ).upper()
        if raw_status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "FINISH", "FINISHED"}:
            raw_results = status_source.get("results") or status_source.get("outputs") or []
            results = [item for item in raw_results if isinstance(item, dict)] if isinstance(raw_results, list) else []
            videos = []
            for item in results:
                url = str(item.get("url") or item.get("fileUrl") or "")
                output_type = str(item.get("outputType") or item.get("fileType") or "").lower()
                if output_type in {"mp4", "video"} or url.lower().split("?")[0].endswith(".mp4"):
                    videos.append((item, url))
            selected = next(
                ((item, url) for item, url in videos if str(item.get("nodeId") or "") == OUTPUT_NODE_ID),
                None,
            ) or (videos[0] if videos else None)
            if selected and selected[1]:
                item, url = selected
                billing = billing_evidence(payload)
                return {
                    "status": "SUCCEEDED",
                    "video_url": url,
                    "consume_coins": item.get("consumeCoins") or billing["provider_usage"].get("consume_coins"),
                    # ``consumeMoney`` is the documented enterprise balance
                    # field.  It is intentionally not discovered by a broad
                    # recursive search.
                    "consume_money_cny": billing["provider_usage"].get("consume_money"),
                    "billing": billing,
                    "raw": payload,
                }
            return {
                "status": "FAILED",
                "video_url": None,
                "error": "RunningHub 任务已完成，但没有返回 MP4 视频结果",
                "raw": payload,
            }
        if raw_status in {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED"}:
            billing = billing_evidence(payload)
            error, failure_details = _failure_message(status_source, raw_status)
            return {
                "status": "FAILED",
                "video_url": None,
                "error": error,
                "failure_details": failure_details,
                "consume_money_cny": billing["provider_usage"].get("consume_money"),
                "billing": billing,
                "raw": payload,
            }
        return {"status": "RUNNING", "video_url": None, "raw": payload}

    def download(self, url: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # A deterministic ``.download`` name allowed the web-server recovery
        # process and a scheduled CLI process to open the same temporary file
        # on Windows.  Use a process-unique name and keep the HTTP response
        # inside a context manager so every handle is closed before replace.
        temporary = target.with_suffix(target.suffix + f".{os.getpid()}-{time.time_ns()}.download")
        try:
            response = self.session.get(url, stream=True, timeout=300)
            try:
                if not response.ok:
                    raise RunningHubAvatarError(f"RunningHub 结果下载失败：HTTP {response.status_code}")
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise RunningHubAvatarError("RunningHub 生成结果为空")
            for attempt in range(5):
                try:
                    os.replace(temporary, target)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.15 * (attempt + 1))
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass


class RunningHubLongCatAvatar(BaseTool):
    name = "runninghub_longcat_avatar"
    version = "1.0.0"
    tier = ToolTier.GENERATE
    capability = "avatar"
    provider = "runninghub"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API
    dependencies = ["env:RUNNINGHUB_API_KEY", "env:RUNNINGHUB_WORKFLOW_ID"]
    install_instructions = "配置 RUNNINGHUB_API_KEY、RUNNINGHUB_WORKFLOW_ID，并导入冻结的 LongCat 工作流模板。"
    agent_skills = ["avatar-video"]
    capabilities = ["avatar_video", "audio_driven_avatar", "native_async_task", "durable_resume"]
    supports = {"audio_driven_animation": True, "cloud_render": True, "offline": False}
    best_for = ["逐轮次中文数字人口播", "多角色串行生成", "局部重试"]
    not_good_for = ["未发布到 RunningHub 的本地 JSON", "未经确认的批量付费任务"]
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=800, network_required=True)
    retry_policy = RetryPolicy(max_retries=0, backoff_seconds=5.0, retryable_errors=[])
    side_effects = ["调用 RunningHub 积分任务", "写入本地 MP4 结果"]
    user_visible_verification = ["试听原声与口型", "检查角色身份一致性", "核对片段时长"]
    input_schema = {
        "type": "object",
        "required": ["image_path", "audio_path", "output_path"],
        "properties": {
            "image_path": {"type": "string"}, "audio_path": {"type": "string"},
            "output_path": {"type": "string"}, "timeout_seconds": {"type": "integer", "default": 1800},
            "poll_interval": {"type": "number", "default": 15},
            "instance_type": {"type": "string", "enum": ["default", "plus"]},
            "exact_total_frames": {"type": "integer", "minimum": 1},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            self.check_dependencies()
            client = RunningHubLongCatClient()
            image = client.upload_file(Path(str(inputs["image_path"])), file_type="image")
            audio = client.upload_file(Path(str(inputs["audio_path"])), file_type="audio")
            submitted = client.submit(
                presenter_filename=image,
                audio_filename=audio,
                instance_type=inputs.get("instance_type"),
                exact_total_frames=inputs.get("exact_total_frames"),
            )
            deadline = time.monotonic() + int(inputs.get("timeout_seconds") or 1800)
            while time.monotonic() < deadline:
                result = client.poll(submitted["task_id"])
                if result["status"] == "SUCCEEDED" and result.get("video_url"):
                    target = Path(str(inputs["output_path"]))
                    client.download(str(result["video_url"]), target)
                    return ToolResult(success=True, data={"task_id": submitted["task_id"], "output_path": str(target)}, artifacts=[str(target)], model="LongCat-1.5")
                if result["status"] == "FAILED":
                    raise RunningHubAvatarError(str(result.get("error") or "RunningHub 任务失败"))
                time.sleep(max(1.0, float(inputs.get("poll_interval") or 15)))
            raise RunningHubAvatarError("等待 RunningHub 数字人任务超时；任务编号已保存，可继续跟踪")
        except (RunningHubAvatarError, DependencyError, OSError, KeyError) as exc:
            return ToolResult(success=False, data={"provider": self.provider}, error=str(exc))
