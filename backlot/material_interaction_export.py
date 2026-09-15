"""剪辑决策导出：把已冻结的二次精剪计划导出为中立、可导入的剪辑清单。

设计边界（见 `docs/team-runs/2026-09-12-cut-v2/02-ARCHITECTURE.md` 第 1 节）：

* 本模块是**纯函数**：只吃「已冻结的计划字典 + 契约字典」，产出「字符串/字典」。
  **不碰网络、不碰 FFmpeg、不改计划文件**，因此导出天然零付费、零副作用。
* 落盘与 HTTP 接线在 `workbench.py` / `server.py`，UI 在 `workbench.ui.workbench.js`。
* 三种格式：JSON ``cut-list-v1``、FCP7 ``xmeml`` v4、P1-1 手写 OTIO JSON。

帧号规则统一为 ``round_half_up``：``frame = floor(seconds * fps + 0.5)``。变速片段在
FCP7 里优先写 ``Time Remap``；无法导入时退化为「等长片段 + 标记」并写
``fcp7_speed_degraded:<segment_id>``（两条路径都实现，由 ``speed_mode`` 选择）。

``timeline.timebase`` 是整数：标准帧率（整数、23.976/29.97/59.94）可精确表达，其余
非标准帧率（例如 22.965）会被取整，并写入 ``degradations``（``fcp7_timebase_rounded:…``）
与 FCP7 XML 注释。落盘遵循「**每次导出生成新文件（按内容哈希命名）、绝不覆盖**」，
而不是「字节幂等」（未固定 ``generated_at`` 时两次导出仅生成时间字段不同）。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import quote
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET

SCHEMA_NAME = "cut-list-v1"
SCHEMA_VERSION = 1
FRAME_ROUNDING = "round_half_up"
GENERATOR = {"name": "haike_video", "module": "material_interaction_export", "version": SCHEMA_NAME}

SUPPORTED_FORMATS = ("json", "fcp7_xml", "otio")
MEDIA_REFERENCE_POLICIES = ("review_proxy", "original")
# FCP7 变速的两条路径：写入 Time Remap，或退化为等长片段 + 标记。
FCP7_SPEED_MODES = ("timeremap", "constant_length")
DEFAULT_FCP7_SPEED_MODE = "timeremap"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
# FCP7 的 `<timebase>` 只能写整数。整帧率（24/25/30/50/60…）与标准 NTSC 帧率
# （24000/1001、30000/1001、60000/1001）都能被精确表达；其余**非标准帧率**（例如 22.965）
# 只能取整，必须在导出物里显式记录（JSON `degradations` + XML 注释），否则导入后
# 时间线总时长与逐段帧号会与预览对不上。
STANDARD_NTSC_RATES = ((24000 / 1001, 24), (30000 / 1001, 30), (60000 / 1001, 60))
NTSC_RATE_TOLERANCE = 0.01


class InteractionExportError(ValueError):
    """导出失败。所有文案中文且给出可执行的补救动作。"""


def frame_number(seconds: float, fps: float) -> int:
    """``round_half_up``：``floor(seconds * fps + 0.5)``。"""
    try:
        value = float(seconds)
        rate = float(fps)
    except (TypeError, ValueError) as exc:
        raise InteractionExportError("导出需要合法的时间与帧率，请重新生成剪辑计划后重试") from exc
    if not math.isfinite(value) or not math.isfinite(rate) or rate <= 0:
        raise InteractionExportError("导出需要合法的帧率（大于 0），请重新生成剪辑计划后重试")
    if value < 0:
        raise InteractionExportError("导出遇到负数时间戳，请先在预览里核对剪辑计划")
    return int(math.floor(value * rate + 0.5))


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _audio_contract(raw: Any) -> dict[str, int] | None:
    if raw is None or raw is False:
        return None
    if isinstance(raw, dict):
        return {"sample_rate": _positive_int(raw.get("sample_rate"), default=DEFAULT_SAMPLE_RATE),
                "channels": _positive_int(raw.get("channels"), default=DEFAULT_CHANNELS)}
    if isinstance(raw, str) and raw.strip():
        # 渲染契约把音频写成 ``aac/48kHz/128k`` 之类的短串：解析出采样率即可。
        sample_rate = DEFAULT_SAMPLE_RATE
        for token in raw.replace("/", " ").replace("Hz", "Hz ").split():
            digits = "".join(ch for ch in token if ch.isdigit())
            if token.lower().endswith("khz") and digits:
                sample_rate = int(digits) * 1000
        return {"sample_rate": sample_rate, "channels": DEFAULT_CHANNELS}
    return {"sample_rate": DEFAULT_SAMPLE_RATE, "channels": DEFAULT_CHANNELS}


def _actions_for(occurrence: dict[str, Any]) -> list[dict[str, Any]]:
    actions = occurrence.get("actions")
    if isinstance(actions, list) and actions:
        return deepcopy(actions)
    return [{"kind": "speed", "value": float(occurrence.get("speed") or 1.0),
             "unit": "ratio", "reason": "preset"}]


def _media_block(source: dict[str, Any] | None, media_reference: str) -> tuple[dict[str, Any], list[str]]:
    """Normalise the source descriptor and report a missing review proxy."""
    degradations: list[str] = []
    source = deepcopy(source) if isinstance(source, dict) else {}
    original = source.get("original") if isinstance(source.get("original"), dict) else {}
    reference = source.get("reference") if isinstance(source.get("reference"), dict) else {}
    if media_reference == "review_proxy" and str(reference.get("kind") or "") != "review_proxy":
        reference = {"kind": "original", "path": str(original.get("path") or ""),
                     "fingerprint": str(source.get("fingerprint") or "")}
        degradations.append("proxy_missing:未找到可用的审核代理，已改用原片引用；HEVC/TS 原片在部分软件中可能时间戳断裂")
    elif media_reference == "original":
        reference = {"kind": "original", "path": str(original.get("path") or ""),
                     "fingerprint": str(source.get("fingerprint") or "")}
    block = {
        "fingerprint": str(source.get("fingerprint") or ""),
        "original": {
            "path": str(original.get("path") or ""),
            "codec": original.get("codec"),
            "container": original.get("container"),
            "r_frame_rate": original.get("r_frame_rate"),
            "note": original.get("note") or "Premiere 很可能无法直接打开（HEVC + TS，时间戳可能断裂）",
        },
        "reference": reference,
    }
    return block, degradations


def _timebase_note(fps: float, timebase: int) -> str | None:
    """非标准帧率被取整为整数 ``timebase`` 时的显式降级说明。

    标准帧率（整数帧率，以及 23.976 / 29.97 / 59.94 这类标准 NTSC 帧率）能被 FCP7
    精确表达，返回 ``None``；其余非标准帧率（例如 22.965）只能取整，必须留下可读记录：
    含义是「导入后时间线总时长与逐段帧号按取整帧率计算，相较原片可能有毫秒级累积偏差」。
    """
    rate = float(fps)
    base = int(timebase)
    if abs(rate - base) <= 1e-6:
        return None  # 整数帧率，精确表达。
    for ntsc_rate, ntsc_timebase in STANDARD_NTSC_RATES:
        if ntsc_timebase == base and abs(rate - ntsc_rate) <= NTSC_RATE_TOLERANCE:
            return None  # 标准 NTSC 帧率，FCP7 用 timebase + ntsc=TRUE 精确表达。
    return (
        f"fcp7_timebase_rounded:{rate:g}->{base}:源帧率非标准，FCP7 时间基只能写整数，"
        f"已取整为 {base} fps；导入后时间线总时长与逐段帧号按 {base} fps 计算，"
        f"相较原片可能有毫秒级累积偏差（帧号本身仍按真实帧率 {rate:g} fps 换算）"
    )


def build_cut_list(plan: dict[str, Any], *, source: dict[str, Any] | None = None,
                   contract: dict[str, Any] | None = None, media_reference: str = "review_proxy",
                   generated_at: str | None = None) -> dict[str, Any]:
    """Build the neutral ``cut-list-v1`` document from a frozen second-pass plan."""
    if not isinstance(plan, dict):
        raise InteractionExportError("剪辑计划格式无效，请重新生成后再导出")
    if media_reference not in MEDIA_REFERENCE_POLICIES:
        raise InteractionExportError("媒体引用策略无效，请选择「审核代理」或「原片」后重试")
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id.startswith("ISP-"):
        raise InteractionExportError("剪辑计划编号无效，请重新生成后再导出")
    mapping = plan.get("timeline_mapping")
    if not isinstance(mapping, list) or not mapping:
        raise InteractionExportError("剪辑计划没有可用片段，请先生成候选预览再导出")

    contract = contract if isinstance(contract, dict) else {}
    fps = contract.get("fps")
    if fps is None:
        raise InteractionExportError("导出缺少帧率契约，请重新生成剪辑计划后重试")
    rate = float(fps)
    if not math.isfinite(rate) or not 1 <= rate <= 120:
        raise InteractionExportError("帧率契约无效（须在 1–120 之间），请重新生成剪辑计划后重试")
    timebase = max(1, int(round(rate)))

    occurrences = {str(row.get("occurrence_id") or ""): row for row in plan.get("occurrences") or []
                   if isinstance(row, dict)}
    segments: list[dict[str, Any]] = []
    for row in mapping:
        if not isinstance(row, dict):
            raise InteractionExportError("剪辑计划的片段结构无效，请重新生成后再导出")
        source_start = float(row.get("source_start") or 0.0)
        source_end = float(row.get("source_end") or 0.0)
        output_start = float(row.get("output_start") or 0.0)
        output_end = float(row.get("output_end") or 0.0)
        speed = float(row.get("speed") or 1.0)
        if source_end < source_start or output_end < output_start or speed <= 0:
            raise InteractionExportError("剪辑计划的片段区间无效，请重新生成后再导出")
        occurrence = occurrences.get(str(row.get("occurrence_id") or "")) or {}
        segments.append({
            "segment_id": str(row.get("occurrence_id") or ""),
            "role": str(row.get("role") or "body"),
            "group_ids": list(row.get("group_ids") or []),
            "source_start": round(source_start, 6), "source_end": round(source_end, 6),
            "speed": round(speed, 6),
            "actions": _actions_for({"speed": speed, **occurrence}),
            "output_start": round(output_start, 6), "output_end": round(output_end, 6),
            "source_start_frame": frame_number(source_start, rate),
            "source_end_frame": frame_number(source_end, rate),
            "output_start_frame": frame_number(output_start, rate),
            "output_end_frame": frame_number(output_end, rate),
        })

    output_duration = round(sum(row["output_end"] - row["output_start"] for row in segments), 6)
    frame_count = segments[-1]["output_end_frame"]
    # 完整性校验：帧号必须非负、单调不减，且源帧号不得超过原片长度。
    source_frame_count = None
    if contract.get("duration") is not None:
        source_frame_count = frame_number(float(contract["duration"]), rate)
    else:
        source_frame_count = max(row["source_end_frame"] for row in segments)
    _check_frames(segments, source_frame_count=source_frame_count, frame_count=frame_count)

    media, degradations = _media_block(source, media_reference)
    timebase_note = _timebase_note(rate, timebase)
    if timebase_note:
        degradations.append(timebase_note)
    plan_options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
    cut_list: dict[str, Any] = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "generator": dict(GENERATOR),
        "plan": {
            "plan_id": plan_id, "revision": int(plan.get("revision") or 0),
            "status": str(plan.get("status") or "pending_review"),
            "created_at": plan.get("created_at"),
        },
        "approved": str(plan.get("status") or "") == "approved",
        "source": media,
        "contract": {
            "fps": rate,
            "width": _positive_int(contract.get("width"), default=0),
            "height": _positive_int(contract.get("height"), default=0),
            "audio": _audio_contract(contract.get("audio")),
        },
        "timeline": {"output_duration": output_duration, "frame_count": int(frame_count),
                     "timebase": timebase},
        "segments": segments,
        "subtitles": [{
            "cue_id": str(cue.get("cue_id") or ""),
            "output_start": round(float(cue.get("output_start") or 0.0), 6),
            "output_end": round(float(cue.get("output_end") or 0.0), 6),
            "text": str(cue.get("text") or ""),
        } for cue in plan.get("subtitle_cues") or [] if isinstance(cue, dict)],
        "pause_handling": str(plan_options.get("pause_handling") or "remove"),
        "pause_trims": deepcopy(plan.get("pause_trims") or []),
        "speed_segments": deepcopy(plan.get("speed_segments") or []),
        "degradations": degradations,
        "warnings": [str(row) for row in plan.get("warnings") or []],
        "media_reference_policy": media_reference,
        "frame_rounding": FRAME_ROUNDING,
    }
    return cut_list


def _check_frames(segments: list[dict[str, Any]], *, source_frame_count: int, frame_count: int) -> None:
    previous_source = previous_output = 0
    for row in segments:
        for key, upper in (("source_start_frame", source_frame_count), ("source_end_frame", source_frame_count),
                           ("output_start_frame", frame_count), ("output_end_frame", frame_count)):
            value = int(row[key])
            if value < 0 or value > upper:
                raise InteractionExportError(
                    f"片段 {row['segment_id']} 的帧号越出素材范围，请核对剪辑计划后重试")
        if row["source_end_frame"] < previous_source or row["output_end_frame"] < previous_output:
            raise InteractionExportError("剪辑计划的帧号不是单调递增，请重新生成后再导出")
        previous_source = row["source_end_frame"]
        previous_output = row["output_end_frame"]


# --------------------------------------------------------------------------- #
# FCP7 xmeml v4
# --------------------------------------------------------------------------- #

def _path_url(path: str) -> str:
    if not path:
        return ""
    return "file://localhost/" + quote(Path(path).as_posix(), safe="/:")


def _rate_element(parent: ET.Element, timebase: int, ntsc: bool) -> None:
    rate = ET.SubElement(parent, "rate")
    ET.SubElement(rate, "timebase").text = str(int(timebase))
    ET.SubElement(rate, "ntsc").text = "TRUE" if ntsc else "FALSE"


def _library_name(cut_list: dict[str, Any], *, policy: str) -> str:
    reference = (cut_list.get("source") or {}).get("reference") or {}
    path = str(reference.get("path") or "")
    if path:
        return Path(path).name
    return f"{cut_list['plan']['plan_id']}-{'proxy' if policy == 'review_proxy' else 'source'}"


def fcp7_speed_degradations(cut_list: dict[str, Any], *, speed_mode: str = DEFAULT_FCP7_SPEED_MODE) -> list[str]:
    """变速片段在 FCP7 里的降级记录（两条路径都记录，界面可见）。"""
    if speed_mode not in FCP7_SPEED_MODES:
        raise InteractionExportError("FCP7 变速模式无效，请选择「时间重映射」或「等长片段」后重试")
    rows: list[str] = []
    for segment in cut_list.get("segments") or []:
        if abs(float(segment.get("speed") or 1.0) - 1.0) <= 1e-9:
            continue
        rows.append(f"fcp7_speed_degraded:{segment['segment_id']}")
    return rows


def build_fcp7_xml(cut_list: dict[str, Any], *, speed_mode: str = DEFAULT_FCP7_SPEED_MODE) -> str:
    """Serialise a ``cut-list-v1`` document as FCP7 ``xmeml`` v4."""
    if speed_mode not in FCP7_SPEED_MODES:
        raise InteractionExportError("FCP7 变速模式无效，请选择「时间重映射」或「等长片段」后重试")
    timeline = cut_list.get("timeline") or {}
    contract = cut_list.get("contract") or {}
    timebase = _positive_int(timeline.get("timebase"), default=30)
    fps = float(contract.get("fps") or timebase)
    ntsc = abs(fps - timebase) > 1e-6
    frame_count = int(timeline.get("frame_count") or 0)
    policy = str(cut_list.get("media_reference_policy") or "review_proxy")

    xmeml = ET.Element("xmeml", {"version": "4"})
    sequence = ET.SubElement(xmeml, "sequence")
    ET.SubElement(sequence, "name").text = str(cut_list["plan"]["plan_id"])
    frame_rate_note = _timebase_note(fps, timebase)
    if frame_rate_note:
        # FCP7 时间基只能写整数，非标准帧率被取整：在导出物内留注释说明（解析器忽略注释）。
        sequence.insert(0, ET.Comment(f" {frame_rate_note} "))
    ET.SubElement(sequence, "duration").text = str(frame_count)
    _rate_element(sequence, timebase, ntsc)
    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    characteristics = ET.SubElement(ET.SubElement(video, "format"), "samplecharacteristics")
    ET.SubElement(characteristics, "width").text = str(_positive_int(contract.get("width"), default=1920))
    ET.SubElement(characteristics, "height").text = str(_positive_int(contract.get("height"), default=1080))
    _rate_element(characteristics, timebase, ntsc)
    track = ET.SubElement(video, "track")

    reference = (cut_list.get("source") or {}).get("reference") or {}
    library_name = _library_name(cut_list, policy=policy)
    duration_frames = int(timeline.get("frame_count") or 0)
    for index, segment in enumerate(cut_list.get("segments") or [], start=1):
        clipitem = ET.SubElement(track, "clipitem", {"id": f"clipitem-{index}"})
        ET.SubElement(clipitem, "name").text = str(segment["segment_id"])
        ET.SubElement(clipitem, "enabled").text = "TRUE"
        speed = float(segment.get("speed") or 1.0)
        slow = speed_mode == "constant_length" and abs(speed - 1.0) > 1e-9
        if slow:
            # 降级路径：等长片段（按源片长度播放）+ 标记；不写 Time Remap。
            ET.SubElement(clipitem, "start").text = str(int(segment["source_start_frame"]))
            ET.SubElement(clipitem, "end").text = str(int(segment["source_end_frame"]))
        else:
            ET.SubElement(clipitem, "start").text = str(int(segment["output_start_frame"]))
            ET.SubElement(clipitem, "end").text = str(int(segment["output_end_frame"]))
        ET.SubElement(clipitem, "in").text = str(int(segment["source_start_frame"]))
        ET.SubElement(clipitem, "out").text = str(int(segment["source_end_frame"]))
        _rate_element(clipitem, timebase, ntsc)
        file_element = ET.SubElement(clipitem, "file", {"id": f"file-{index}"})
        ET.SubElement(file_element, "name").text = library_name or f"{cut_list['plan']['plan_id']}.mp4"
        ET.SubElement(file_element, "pathurl").text = _path_url(str(reference.get("path") or ""))
        _rate_element(file_element, timebase, ntsc)
        ET.SubElement(file_element, "duration").text = str(duration_frames)
        file_media = ET.SubElement(file_element, "media")
        file_video = ET.SubElement(file_media, "video")
        file_characteristics = ET.SubElement(ET.SubElement(file_video, "format"), "samplecharacteristics")
        ET.SubElement(file_characteristics, "width").text = str(_positive_int(contract.get("width"), default=1920))
        ET.SubElement(file_characteristics, "height").text = str(_positive_int(contract.get("height"), default=1080))
        if abs(speed - 1.0) > 1e-9 and not slow:
            # 首选路径：Time Remap，速度写成百分数（110 == 1.1x）。
            effect = ET.SubElement(ET.SubElement(clipitem, "filter"), "effect")
            ET.SubElement(effect, "name").text = "Time Remap"
            ET.SubElement(effect, "effectid").text = "timeremap"
            parameter = ET.SubElement(effect, "parameter")
            ET.SubElement(parameter, "parameterid").text = "speed"
            ET.SubElement(parameter, "value").text = str(int(round(speed * 100)))
        if slow:
            marker = ET.SubElement(clipitem, "marker")
            ET.SubElement(marker, "comment").text = f"计划倍速 {speed:.2f}x（目标软件不支持变速，已按等长片段导出）"
            ET.SubElement(marker, "in").text = "-1"
            ET.SubElement(marker, "out").text = "-1"

    # 字幕：时间线上的标记（generatoritem），同时旁挂 SRT。
    subtitle_rows = [row for row in cut_list.get("subtitles") or [] if str(row.get("text") or "").strip()]
    if subtitle_rows:
        generator = ET.SubElement(track, "generatoritem", {"id": "titles-1"})
        ET.SubElement(generator, "name").text = "Titles"
        _rate_element(generator, timebase, ntsc)
        ET.SubElement(generator, "in").text = "-1"
        ET.SubElement(generator, "out").text = "-1"
        ET.SubElement(generator, "duration").text = "1"
        for row in subtitle_rows:
            marker = ET.SubElement(generator, "marker")
            ET.SubElement(marker, "comment").text = str(row["text"])
            ET.SubElement(marker, "in").text = str(frame_number(row.get("output_start"), fps))
            ET.SubElement(marker, "out").text = str(frame_number(row.get("output_end"), fps))

    audio_contract = contract.get("audio")
    if audio_contract:
        audio = ET.SubElement(media, "audio")
        audio_characteristics = ET.SubElement(ET.SubElement(audio, "format"), "samplecharacteristics")
        ET.SubElement(audio_characteristics, "samplerate").text = str(int(audio_contract.get("sample_rate") or DEFAULT_SAMPLE_RATE))
        ET.SubElement(audio_characteristics, "depth").text = "16"
        audio_track = ET.SubElement(audio, "track")
        ET.SubElement(audio_track, "enabled").text = "TRUE"

    raw = ET.tostring(xmeml, encoding="utf-8")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + minidom.parseString(raw).toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8").split("\n", 1)[1]


# --------------------------------------------------------------------------- #
# P1-1 手写 OTIO（纯 JSON schema）
# --------------------------------------------------------------------------- #

def _rational_time(value: int, rate: float) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": int(value)}


def build_otio(cut_list: dict[str, Any]) -> dict[str, Any]:
    """Hand-write an OTIO timeline (no third-party dependency)."""
    contract = cut_list.get("contract") or {}
    fps = float(contract.get("fps") or (cut_list.get("timeline") or {}).get("timebase") or 30)
    reference = (cut_list.get("source") or {}).get("reference") or {}
    children = []
    for segment in cut_list.get("segments") or []:
        duration_frames = int(segment["source_end_frame"]) - int(segment["source_start_frame"])
        children.append({
            "OTIO_SCHEMA": "Clip.2",
            "name": str(segment["segment_id"]),
            "source_range": {
                "OTIO_SCHEMA": "TimeRange.1",
                "start_time": _rational_time(int(segment["source_start_frame"]), fps),
                "duration": _rational_time(duration_frames, fps),
            },
            "media_reference": {
                "OTIO_SCHEMA": "ExternalReference.1",
                "target_url": str(reference.get("path") or ""),
                "available_range": {
                    "OTIO_SCHEMA": "TimeRange.1",
                    "start_time": _rational_time(0, fps),
                    "duration": _rational_time(int((cut_list.get("timeline") or {}).get("frame_count") or 0), fps),
                },
            },
            "metadata": {
                "haike": {"role": segment.get("role"), "group_ids": segment.get("group_ids"),
                          "speed": segment.get("speed"), "actions": segment.get("actions")},
            },
        })
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": str(cut_list["plan"]["plan_id"]),
        "global_start_time": None,
        "metadata": {"cut_list_schema": SCHEMA_NAME, "plan": cut_list.get("plan"),
                     "media_reference_policy": cut_list.get("media_reference_policy")},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "children": [{"OTIO_SCHEMA": "Track.1", "name": "V1", "kind": "Video", "children": children}],
        },
    }


# --------------------------------------------------------------------------- #
# 旁挂 SRT
# --------------------------------------------------------------------------- #

def _srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(cut_list: dict[str, Any]) -> str:
    rows = [row for row in cut_list.get("subtitles") or [] if str(row.get("text") or "").strip()]
    blocks = []
    for index, row in enumerate(rows, start=1):
        blocks.append(f"{index}\n{_srt_timestamp(row.get('output_start'))} --> "
                      f"{_srt_timestamp(row.get('output_end'))}\n{str(row['text']).strip()}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #

def _write_once(path: Path, text: str) -> tuple[Path, bool]:
    """Write without ever overwriting a different artefact.

    内容相同则复用同名文件（``reused=True``）；内容不同则另存为 ``<name>-<sha8>``，
    **绝不覆盖**既有产物。因此当调用方不固定 ``generated_at``（默认取当前时间）时，
    每次导出都会新增一个带内容哈希的文件——这是「零覆盖」，而非「字节幂等」。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_text(encoding="utf-8") == text:
            return path, True
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        path = path.with_name(f"{path.stem}-{digest}{path.suffix}")
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return path, True
    path.write_text(text, encoding="utf-8")
    return path, False


def export_plan(plan: dict[str, Any], *, output_dir: Path, formats: list[str],
                media_reference: str = "review_proxy", include_srt: bool = False,
                include_clips: bool = False, source: dict[str, Any] | None = None,
                contract: dict[str, Any] | None = None, generated_at: str | None = None,
                speed_mode: str = DEFAULT_FCP7_SPEED_MODE) -> dict[str, Any]:
    """Materialise one plan as neutral cut-decision files. Pure of any network/FFmpeg use.

    ``include_clips`` 目前只影响返回摘要（是否附带逐片段明细），为后续「分包导出」预留。
    """
    requested = [str(row) for row in (formats or ["json"])]
    unknown = [row for row in requested if row not in SUPPORTED_FORMATS]
    if unknown:
        raise InteractionExportError(
            f"不支持的导出格式：{'、'.join(unknown)}；可选 json、fcp7_xml、otio")
    if not isinstance(output_dir, Path):
        output_dir = Path(output_dir)

    cut_list = build_cut_list(plan, source=source, contract=contract,
                              media_reference=media_reference, generated_at=generated_at)
    cut_list["degradations"] = sorted(set(cut_list["degradations"]
                                          + fcp7_speed_degradations(cut_list, speed_mode=speed_mode)))
    plan_id = str(cut_list["plan"]["plan_id"])
    files: list[dict[str, Any]] = []

    if "json" in requested:
        path, reused = _write_once(output_dir / f"{plan_id}.json",
                                   json.dumps(cut_list, ensure_ascii=False, indent=2) + "\n")
        files.append({"format": "json", "path": str(path), "reused": reused})
    if "fcp7_xml" in requested:
        path, reused = _write_once(output_dir / f"{plan_id}.xml", build_fcp7_xml(cut_list, speed_mode=speed_mode))
        files.append({"format": "fcp7_xml", "path": str(path), "reused": reused,
                      "speed_mode": speed_mode})
    if "otio" in requested:
        path, reused = _write_once(output_dir / f"{plan_id}.otio",
                                   json.dumps(build_otio(cut_list), ensure_ascii=False, indent=2) + "\n")
        files.append({"format": "otio", "path": str(path), "reused": reused})
    if include_srt:
        path, reused = _write_once(output_dir / f"{plan_id}.srt", build_srt(cut_list))
        files.append({"format": "srt", "path": str(path), "reused": reused})

    notice = ("此计划尚未确认入库，导出内容仅用于人工核对" if not cut_list["approved"]
              else "计划已确认入库")
    return {"plan_id": plan_id, "files": files, "degradations": list(cut_list["degradations"]),
            "notice": notice, "cut_list": cut_list if include_clips else None}


__all__ = [
    "DEFAULT_FCP7_SPEED_MODE",
    "FCP7_SPEED_MODES",
    "FRAME_ROUNDING",
    "GENERATOR",
    "InteractionExportError",
    "MEDIA_REFERENCE_POLICIES",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "SUPPORTED_FORMATS",
    "build_cut_list",
    "build_fcp7_xml",
    "build_otio",
    "build_srt",
    "export_plan",
    "fcp7_speed_degradations",
    "frame_number",
]
