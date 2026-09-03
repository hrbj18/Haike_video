"""Evidence-first planning for local footage inside an OpenMontage project.

This module intentionally has no filesystem, provider, renderer, or workbench
write side effects.  It turns the *already verified* V2 media indexes into a
draft that can later be adopted by the workbench.  Keeping this layer pure is
important: a recommendation must never download a clip, invoke a model, or
silently replace an editor's selected visual.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any


class LocalMaterialOrchestrationError(ValueError):
    """A human-readable planning input error."""


INPUT_MODES = {"existing_script", "topic_with_materials", "materials_only"}
VISUAL_ROLES = {
    "local_full_bleed",
    "local_focus_card",
    "stock_full_bleed",
    "hyperframes_full_bleed",
    "supporting_background",
}
CUT_POLICIES = {"atomic", "safe_cut", "interruptible"}


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _seconds(value: Any, default: float = 0.0) -> float:
    return round(max(0.0, _number(value, default)), 3)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _terms(value: Any) -> set[str]:
    text = _compact(value, 3000).lower()
    latin = set(re.findall(r"[a-z0-9][a-z0-9_-]{1,}", text))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    return latin | {chinese[index:index + 2] for index in range(max(0, len(chinese) - 1))}


def _asset_resolution(asset: dict[str, Any]) -> tuple[int, int]:
    raw = asset.get("resolution")
    if isinstance(raw, dict):
        width, height = int(_number(raw.get("width"))), int(_number(raw.get("height")))
        if width > 0 and height > 0:
            return width, height
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", str(raw or ""))
    if match:
        return int(match.group(1)), int(match.group(2))
    return 0, 0


def _scene_text(scene: dict[str, Any]) -> str:
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else {}
    enhancement_cues = scene.get("enhancement_cues") if isinstance(scene.get("enhancement_cues"), list) else []
    cue_text = " ".join(
        _compact(cue.get("description"), 1000)
        for cue in enhancement_cues
        if isinstance(cue, dict)
    )
    chunks = [
        _compact(scene.get(key), 1500)
        for key in ("title", "description", "shot_intent")
        if _compact(scene.get(key), 1500)
    ]
    narration_text = _compact(narration.get("text"), 2500)
    # Workbench scenes normally mirror narration in ``description``.  Do not
    # append it twice: a cue at the end of the description could otherwise be
    # incorrectly paired with the duplicated narration after it.
    if narration_text and narration_text not in chunks:
        chunks.append(narration_text)
    if cue_text:
        chunks.append(cue_text)
    return " ".join(chunks)


def script_fingerprint(state: dict[str, Any]) -> str:
    """Fingerprint only editor-owned script/scene content, not render state."""
    payload = [
        {
            "id": str(scene.get("id") or ""),
            "start_seconds": _seconds(scene.get("start_seconds")),
            "end_seconds": _seconds(scene.get("end_seconds")),
            "text": _scene_text(scene),
        }
        for scene in state.get("scenes") or []
        if isinstance(scene, dict)
    ]
    return _hash(payload)


def material_indexes_fingerprint(indexes: dict[str, dict[str, Any]]) -> str:
    """Fingerprint semantic index identities without persisting their paths."""
    payload = {
        str(asset_id): {
            "signature": str((index or {}).get("signature") or ""),
            "source_fingerprint": str(((index or {}).get("source") or {}).get("fingerprint") or ""),
            "status": str((index or {}).get("status") or ""),
            "vision_status": str(((index or {}).get("vision") or {}).get("status") or ""),
        }
        for asset_id, index in sorted(indexes.items())
        if isinstance(index, dict)
    }
    return _hash(payload)


def _confirmed_continuity(request: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    values = request.get("continuity_confirmations")
    if values is None:
        return {}
    if not isinstance(values, list):
        raise LocalMaterialOrchestrationError("连续动作确认必须是列表")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            raise LocalMaterialOrchestrationError("连续动作确认格式无效")
        asset_id, shot_id = str(raw.get("asset_id") or ""), str(raw.get("shot_id") or "")
        if not asset_id or not re.fullmatch(r"SHOT-\d{4}", shot_id):
            raise LocalMaterialOrchestrationError("连续动作确认必须指定素材和镜头编号")
        if raw.get("confirmed") is not True:
            raise LocalMaterialOrchestrationError("连续动作必须由用户明确确认后才可采用完整事件策略")
        start, end = _seconds(raw.get("source_in_seconds")), _seconds(raw.get("source_out_seconds"))
        if end <= start:
            raise LocalMaterialOrchestrationError("连续动作确认的源时间范围无效")
        result[(asset_id, shot_id)] = {
            "source_in_seconds": start,
            "source_out_seconds": end,
            "continuity_group_id": _compact(raw.get("continuity_group_id"), 80) or f"CG-{asset_id}-{shot_id}",
            "label": _compact(raw.get("label"), 160),
        }
    return result


def _description_parts(shot: dict[str, Any]) -> tuple[str, list[str], list[str], list[str]]:
    description = shot.get("description") if isinstance(shot.get("description"), dict) else {}
    summary = _compact(description.get("summary"), 500)
    entities = [
        _compact(value.get("name"), 120)
        for value in description.get("entities") or []
        if isinstance(value, dict) and _compact(value.get("name"), 120)
    ]
    actions = [
        _compact(value.get("name"), 120)
        for value in description.get("actions") or []
        if isinstance(value, dict) and _compact(value.get("name"), 120)
    ]
    unknowns = [_compact(value, 120) for value in description.get("unknowns") or [] if _compact(value, 120)]
    return summary, entities, actions, unknowns


def build_material_capability_map(
    state: dict[str, Any],
    indexes: dict[str, dict[str, Any]],
    request: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Create evidence-only, per-shot local-material capabilities.

    V2 descriptions confirm what is visible in a shot, but do not prove a
    start-to-finish event.  Therefore all shots default to ``safe_cut``.  An
    ``atomic`` action only appears after an explicit user confirmation with a
    bounded source range.
    """
    assets = {str(asset.get("id") or ""): asset for asset in state.get("assets") or [] if isinstance(asset, dict)}
    confirmations = _confirmed_continuity(request)
    capabilities: list[dict[str, Any]] = []
    warnings: list[str] = []
    ordinal = 0
    for asset_id, index in sorted(indexes.items()):
        asset = assets.get(str(asset_id))
        if not asset or str(asset.get("type") or "").lower() != "video":
            continue
        vision = index.get("vision") if isinstance(index.get("vision"), dict) else {}
        if str(index.get("status") or "") != "completed" or vision.get("status") != "completed":
            warnings.append(f"素材 {asset_id} 尚未完成视觉理解 2.0，不能作为自动编排依据")
            continue
        width, height = _asset_resolution(asset)
        for shot in index.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            shot_id = str(shot.get("shot_id") or "")
            start, end = _seconds(shot.get("start_seconds")), _seconds(shot.get("end_seconds"))
            if not re.fullmatch(r"SHOT-\d{4}", shot_id) or end - start < .4:
                continue
            summary, entities, actions, unknowns = _description_parts(shot)
            if not summary and not entities and not actions:
                continue
            confirmation = confirmations.get((asset_id, shot_id))
            source_start, source_end = start, end
            cut_policy = "safe_cut"
            continuity_group_id = None
            if confirmation:
                source_start = max(start, confirmation["source_in_seconds"])
                source_end = min(end, confirmation["source_out_seconds"])
                if source_end - source_start < .4:
                    raise LocalMaterialOrchestrationError(f"{asset_id} {shot_id} 的连续动作范围不在已验证镜头内")
                cut_policy = "atomic"
                continuity_group_id = confirmation["continuity_group_id"]
            ordinal += 1
            capabilities.append({
                "capability_id": f"LMC-{ordinal:03d}",
                "asset_id": asset_id,
                "asset_name": _compact(asset.get("name") or asset_id, 160),
                "shot_id": shot_id,
                "source_in_seconds": source_start,
                "source_out_seconds": source_end,
                "duration_seconds": round(source_end - source_start, 3),
                "source_width": width,
                "source_height": height,
                "source_aspect_ratio": round(width / height, 6) if width > 0 and height > 0 else None,
                "summary": summary,
                "entities": entities,
                "actions": actions,
                "unknowns": unknowns,
                "cut_policy": cut_policy,
                "continuity_group_id": continuity_group_id,
                "evidence": {
                    "source": "vision_v2",
                    "shot_id": shot_id,
                    "index_fingerprint": str(index.get("signature") or ""),
                    "frame_ids": [
                        str(frame.get("frame_id") or "")
                        for frame in shot.get("frames") or []
                        if isinstance(frame, dict) and frame.get("selected_for_vision") and frame.get("frame_id")
                    ][:5],
                },
            })
    return capabilities, warnings


def _scene_duration(scene: dict[str, Any]) -> float:
    return max(.04, _seconds(scene.get("end_seconds")) - _seconds(scene.get("start_seconds")))


def _background_role(scene: dict[str, Any]) -> str:
    plan = scene.get("visual_plan") if isinstance(scene.get("visual_plan"), dict) else {}
    return "hyperframes_full_bleed" if str(plan.get("engine") or "") == "hyperframes" else "stock_full_bleed"


_GENERIC_MATCH_TERMS = {
    "一个", "一台", "一只", "机器", "器人", "画面", "背景", "本地", "素材", "视频",
    "动作", "官方", "显示", "说明", "本段", "这个", "可见", "使用", "展示", "出现",
    "双足", "足机", "足人", "小型", "型机", "的小", "在地", "厅地",
}


def _semantic_tags(value: Any, *, capability: bool = False) -> set[str]:
    """Map evidence-backed Chinese visual concepts to a small stable vocabulary.

    The tags are deliberately about *visible* subjects/actions, never inferred
    product capability.  They prevent generic terms such as ``机器人`` from
    making a group shot look like valid evidence for a hardware-spec segment.
    """
    text = _compact(value, 6000)
    tags: set[str] = set()
    if any(term in text for term in ("纸箱", "箱子", "箱旁", "收纳框", "打开的箱")):
        tags.add("box")
    motion_terms = ("移动", "行走", "迈步", "转向", "轮滑")
    if any(term in text for term in motion_terms) or (not capability and any(term in text for term in ("地面", "客厅"))):
        tags.add("ground_motion")
    if any(term in text for term in ("倾斜", "失去直立", "失去平衡", "真实失败", "失败", "扶住", "倒地")):
        tags.add("failure_or_instability")
    elif "平衡" in text:
        tags.add("balance_state")
    subject_pattern = r"(?:机器人|机器鸭|双足机器人|人形机器人)"
    group_pattern = r"(?:四个|多个|同框|群体|不同颜色|四色|多机|颜色不同|几只)"
    # “机器人……四个关键词” is not a group shot.  A group cue must appear
    # close to an actual robot subject in the same visual claim.
    group_subject_adjacent = (
        re.search(rf"{subject_pattern}.{{0,18}}{group_pattern}", text)
        or re.search(rf"{group_pattern}.{{0,18}}{subject_pattern}", text)
    )
    if group_subject_adjacent:
        tags.add("group_display")
    return tags


def _score_scene_capability(scene: dict[str, Any], capability: dict[str, Any]) -> tuple[int, list[str]]:
    scene_terms = _terms(_scene_text(scene))
    capability_text = " ".join([
        capability.get("summary") or "",
        *(capability.get("entities") or []),
        *(capability.get("actions") or []),
    ])
    capability_terms = _terms(capability_text)
    matched = sorted((scene_terms & capability_terms) - _GENERIC_MATCH_TERMS)
    semantic_matches = sorted(_semantic_tags(_scene_text(scene)) & _semantic_tags(capability_text, capability=True))
    # Generic two-character overlap is not visual evidence.  We permit an
    # adoptable match only when the text shares at least two specific terms, or
    # when a visible-action tag agrees.  The returned score intentionally
    # prefers explicit evidence over broad words such as “机器人”.
    if len(matched) < 2 and not semantic_matches:
        return 0, []
    labels = [*matched, *(f"tag:{tag}" for tag in semantic_matches)]
    tag_weight = {
        "group_display": 15,
        "box": 12,
        "failure_or_instability": 10,
        "balance_state": 5,
        "ground_motion": 5,
    }
    return len(matched) * 2 + sum(tag_weight[tag] for tag in semantic_matches), labels


def _role_for(capability: dict[str, Any], scene_duration: float) -> str:
    aspect = _number(capability.get("source_aspect_ratio"), 0)
    # A short safe-cut clip must never be stretched, looped or left to create a
    # blank tail.  It becomes a local hero window while stock/rendered footage
    # continues underneath.  Full bleed is reserved for a landscape clip that
    # can actually cover the entire narration segment at normal speed.
    if aspect >= 1.35 and (
        capability.get("cut_policy") == "atomic"
        or _number(capability.get("duration_seconds")) >= scene_duration - .05
    ):
        return "local_full_bleed"
    return "local_focus_card"


def build_orchestration_draft(
    state: dict[str, Any],
    indexes: dict[str, dict[str, Any]],
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, non-mutating local-material plan draft."""
    request = request if isinstance(request, dict) else {}
    input_mode = str(request.get("input_mode") or "existing_script")
    if input_mode not in INPUT_MODES:
        raise LocalMaterialOrchestrationError("素材驱动编排的输入模式无效")
    direction = _compact(request.get("direction"), 600)
    scenes = sorted(
        (scene for scene in state.get("scenes") or [] if isinstance(scene, dict)),
        key=lambda scene: (_seconds(scene.get("start_seconds")), str(scene.get("id") or "")),
    )
    has_script = bool(scenes and any(_scene_text(scene).strip() for scene in scenes))
    if input_mode == "existing_script" and not has_script:
        raise LocalMaterialOrchestrationError("当前没有可保持不变的完整脚本，请选择“主题加素材”或先导入脚本")
    if input_mode == "topic_with_materials" and not direction:
        raise LocalMaterialOrchestrationError("主题加素材模式需要先填写本期主题或方向")

    capabilities, warnings = build_material_capability_map(state, indexes, request)
    preparation = {
        "script_status": "provided" if has_script else "not_provided",
        "local_material_status": "ready" if capabilities else "needs_analysis",
        "direction_status": "confirmed" if direction or input_mode == "existing_script" else "needs_direction",
    }
    draft: dict[str, Any] = {
        "version": 1,
        "revision": 1,
        "status": "draft",
        "input_mode": input_mode,
        "direction": direction,
        "script_fingerprint": script_fingerprint(state),
        "asset_index_fingerprint": material_indexes_fingerprint(indexes),
        "preparation": preparation,
        "material_capability_map": capabilities,
        "sequences": [],
        "scene_plans": [],
        "warnings": list(dict.fromkeys(warnings)),
    }
    if input_mode == "materials_only" and not direction:
        draft["status"] = "needs_direction"
        draft["warnings"].append("仅有素材时系统只展示能力地图；请先确认本期内容方向，再生成完整脚本与画面计划")
        draft["fingerprint"] = _hash({key: value for key, value in draft.items() if key not in {"fingerprint"}})
        return draft

    unused_capabilities = list(capabilities)
    for scene in scenes:
        scene_id = str(scene.get("id") or "")
        duration = _scene_duration(scene)
        scored = []
        for capability in unused_capabilities:
            score, matched = _score_scene_capability(scene, capability)
            if score >= 2:
                scored.append((score, matched, capability))
        scored.sort(key=lambda item: (-item[0], -_number(item[2].get("duration_seconds")), item[2]["capability_id"]))
        fallback_role = _background_role(scene)
        if not scored:
            draft["scene_plans"].append({
                "scene_id": scene_id,
                "status": "needs_background",
                "visual_role": fallback_role,
                "background_requirement": {
                    "role": fallback_role,
                    "purpose": "本地素材未提供可核验的对应画面；保留网络下载或已物化渲染画面的背景职责",
                    "query_hint": _compact(scene.get("shot_intent") or scene.get("description") or scene.get("title"), 500),
                },
                "warnings": ["没有命中本段台词的本地视觉证据，系统不会强行塞入本地素材"],
            })
            continue
        _, matched, capability = scored[0]
        source_duration = _number(capability.get("duration_seconds"))
        if capability.get("cut_policy") == "atomic" and source_duration > duration + .001:
            draft["scene_plans"].append({
                "scene_id": scene_id,
                "status": "needs_timing_decision",
                "visual_role": fallback_role,
                "capability_id": capability["capability_id"],
                "warnings": [f"已确认的完整动作长 {source_duration:.1f} 秒，超过本段 {duration:.1f} 秒；系统不会截断动作或变速"],
            })
            continue
        display_duration = min(source_duration, duration)
        role = _role_for(capability, duration)
        sequence_id = f"LMS-{len(draft['sequences']) + 1:03d}"
        sequence = {
            "sequence_id": sequence_id,
            "scene_id": scene_id,
            "capability_id": capability["capability_id"],
            "asset_id": capability["asset_id"],
            "visual_role": role,
            "cut_policy": capability["cut_policy"],
            "continuity_group_id": capability.get("continuity_group_id"),
            "source_in_seconds": capability["source_in_seconds"],
            "source_out_seconds": round(capability["source_in_seconds"] + display_duration, 3),
            "display_start_seconds": 0.0,
            "display_end_seconds": round(display_duration, 3),
            "muted": True,
            "playback_rate": 1.0,
            "evidence": deepcopy(capability["evidence"]),
        }
        draft["sequences"].append(sequence)
        draft["scene_plans"].append({
            "scene_id": scene_id,
            "status": "ready_for_adoption",
            "sequence_id": sequence_id,
            "capability_id": capability["capability_id"],
            "visual_role": role,
            "matched_terms": matched,
            "background_requirement": (
                {
                    "role": "supporting_background",
                    "purpose": "本地素材作为主角窗；背景保持低信息密度并覆盖本段其余时长",
                    "fallback_role": fallback_role,
                }
                if role == "local_focus_card" or display_duration < duration - .001
                else None
            ),
            "warnings": (
                [f"本地动作仅覆盖 {display_duration:.1f}/{duration:.1f} 秒；其余时间仍需连续背景"]
                if display_duration < duration - .001 else []
            ),
        })
        unused_capabilities.remove(capability)
    draft["fingerprint"] = _hash({key: value for key, value in draft.items() if key not in {"fingerprint"}})
    return draft


def find_scene_plan(draft: dict[str, Any], scene_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an adoptable scene-plan/sequence pair or a clear error."""
    plan = next((item for item in draft.get("scene_plans") or [] if str(item.get("scene_id") or "") == str(scene_id)), None)
    if not isinstance(plan, dict) or plan.get("status") != "ready_for_adoption":
        raise LocalMaterialOrchestrationError("当前片段没有可采用的本地素材草案")
    sequence = next((item for item in draft.get("sequences") or [] if str(item.get("sequence_id") or "") == str(plan.get("sequence_id") or "")), None)
    if not isinstance(sequence, dict):
        raise LocalMaterialOrchestrationError("本地素材草案缺少连续动作序列")
    if str(sequence.get("visual_role") or "") not in VISUAL_ROLES or str(sequence.get("cut_policy") or "") not in CUT_POLICIES:
        raise LocalMaterialOrchestrationError("本地素材草案合同无效，请重新生成")
    return plan, sequence
