"""主题先行编辑转换层（normalized editorial snapshot → 编辑决策 → 时长探针 → remake-spec-v1）。

输入是 intake 层（``copy_skill_research_pack`` 权威校验 + ``research_pack_snapshot`` 投影）
产出的**冻结** editorial snapshot。本层是**纯本地、确定性、无副作用**的转换：不建项目、
不排队、不付费、不联网、不调用真实 TTS / LLM。所有外部能力（配音时长、成稿）都以**注入的**
回调进入，测试用 stub 替换。

冻结流程（每一步记在决策对象的 ``stages`` 里）：

    validated_snapshot → keywords_ready → theme_candidates_ready → theme_selected
    → argument_map_ready → script_ready → duration_measured → remake_spec_ready

硬约束：

* `argument_graph` 的 ``dim`` 只能来自 ``news_selection_v2.ALLOWED_DIMENSIONS``（13 值）；
  ``edges[].relation`` 只能来自 ``copy_skill_research_pack.ALLOWED_EDGE_RELATIONS``（5 值）。
  relation/edge 一律以 intake 的 snapshot 为准，编辑层不自造也不放宽。
* 只有 ``fact_ready`` 的 claim 才能作为论证节点（关键 claim 未过事实门即 fail closed）。
* ★ 论证节点**只**来自生产端 ``argument_graph.nodes``，编辑层**绝不从 `claims` 回填**。
  合同第 81 行只规定节点形状、从不要求非空；而生产端默认适配器
  （``episode_research_pack.build_research_semantic``）恒产 ``nodes: []``（生产端全 ``src/``
  唯一写 ``nodes`` 处在 ``episode_research_pack.py:676``，硬编码空数组）⇒ 用默认适配器时
  链路必然终止在 ``theme_selected`` 阻断（exit 3）。这是**正确**结果而非缺陷：该适配器产出的
  claims 是 ``unverified`` + ``hedge``、``fact_sources_present=0``，``_argument_nodes`` 的
  ``fact_ready`` 门同样会拦 ⇒ 从 claims 造节点等于绕过事实门、让未核验结论拿到
  ``argument_map_ready``。要走到 ``argument_map_ready``，必须由生产端注入真实
  ``research_builder``（上游包带非空 nodes）。
* 段数由论证容量决定，固定 7 段只作**成稿后检查表**。
* 只有 ``rights.render_eligible`` 且可用的素材才能成为镜头；否则返回明确缺口，绝不伪造。
* 音频是主时钟：段时长来自注入 ``duration_probe`` 的真实秒数，画面适配音频。
* ``claim.text`` 只是论证骨架；原创口播必须来自注入的 ``copywriter``。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Iterable, Mapping

from backlot.remake_intake import (
    DISPOSITION_PARTIAL,
    DISPOSITION_READY,
    DISPOSITION_REJECTED,
    DISPOSITION_RESEARCH_REQUIRED,
    EditorialSnapshotError,
    RENDER_BLOCKING_RIGHTS_STATUSES,
    validate_editorial_snapshot,
)

REMAKE_SPEC_SCHEMA_VERSION = "remake-spec-v1"
PIPELINE_TYPE = "avatar-spokesperson"

#: 主题先行的七个叙事部件——**只用于成稿后自检**，永远不决定段数。
SEVEN_PART_CHECKLIST = (
    ("hook", "钩子", ("event_core", "visual_moment")),
    ("context", "背景", ("evidence_detail", "key_number", "industry_value")),
    ("mechanism", "机制", ("mechanism", "method")),
    ("impact", "影响", ("user_impact", "use_case", "action_tip")),
    ("constraint", "限制", ("constraint", "limitation")),
    ("uncertainty", "不确定性", ("uncertainty",)),
    ("closing", "收尾", ("industry_value", "action_tip")),
)

#: 维度 → 中文段落标签（写稿器尚未介入时的确定性占位标签）。
DIM_LABELS = {
    "event_core": "事件核心",
    "evidence_detail": "证据细节",
    "mechanism": "机制",
    "user_impact": "用户影响",
    "action_tip": "行动提示",
    "industry_value": "行业价值",
    "use_case": "使用场景",
    "constraint": "约束",
    "method": "方法",
    "limitation": "局限",
    "key_number": "关键数字",
    "uncertainty": "不确定性",
    "visual_moment": "视觉瞬间",
}

#: 超过该总时长只给**人工安全提示**，绝不自动压缩。
MANUAL_DURATION_HINT_SECONDS = 120.0

DEFAULT_MIN_SUPPORTED_CLAIMS = 1

#: 成稿层注入接口：``copywriter(context) -> list[section]``（纯函数，禁止网络/LLM）。
Copywriter = Callable[[dict[str, Any]], list[dict[str, Any]]]


class EditorialBlocked(Exception):
    """编辑决策无法继续（disposition 非 ready / 关键 claim 未过门 / 主题不可裁决）。"""

    def __init__(self, reasons: Iterable[str], *, stage: str = ""):
        self.reasons = list(dict.fromkeys(str(reason) for reason in reasons if str(reason).strip()))
        self.stage = stage
        super().__init__("；".join(self.reasons) or "编辑决策被阻断")


class RemakeSpecError(Exception):
    """remake-spec-v1 无法满足硬约束（含明确缺口）。"""

    def __init__(self, issues: Iterable[str]):
        self.issues = list(dict.fromkeys(str(issue) for issue in issues if str(issue).strip()))
        super().__init__("；".join(self.issues) or "remake spec 校验失败")


class DurationProbeError(Exception):
    """注入的时长探针返回了不可用结果。"""


# --------------------------------------------------------------------------- #
# 帧口径（与 workbench 保持一致，独立实现以避免 import 重型模块）
# --------------------------------------------------------------------------- #
def _fps(fps: Any) -> int:
    try:
        value = int(fps)
    except (TypeError, ValueError):
        value = 30
    return max(1, value)


def quantize_frames(seconds: Any, fps: Any = 30) -> int:
    """秒 → 帧：`floor(seconds*fps + 0.5)`（负值归零）。"""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    return max(0, int(math.floor(max(0.0, value) * _fps(fps) + 0.5)))


def frames_to_seconds(frames: int, fps: Any = 30) -> float:
    """帧 → 秒（3 位小数）。"""
    return round(max(0, int(frames)) / _fps(fps), 3)


def quantize_source_window(in_ms: Any, out_ms: Any, fps: Any = 30) -> dict[str, float]:
    """素材毫秒区间 → 按目标 fps 显式量化的秒区间（先转秒，再各自落到最近帧）。"""
    in_seconds = max(0.0, float(in_ms) / 1000.0)
    out_seconds = max(in_seconds, float(out_ms) / 1000.0)
    in_frame = quantize_frames(in_seconds, fps)
    out_frame = quantize_frames(out_seconds, fps)
    if out_frame <= in_frame:
        out_frame = in_frame + 1
    return {"in": frames_to_seconds(in_frame, fps), "out": frames_to_seconds(out_frame, fps)}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


# --------------------------------------------------------------------------- #
# snapshot → 素材索引（把 rights 合并进 material，用于编辑层判定）
# --------------------------------------------------------------------------- #
def _materials_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rights_by_material = {str(record.get("material_id") or ""): record for record in snapshot.get("rights") or []}
    index: dict[str, dict[str, Any]] = {}
    for material in snapshot.get("materials") or []:
        material_id = str(material.get("material_id") or "")
        rights = rights_by_material.get(material_id) or {}
        render_eligible = bool(rights.get("render_eligible"))
        rights_status = _clean(rights.get("rights_status"))
        available = render_eligible and rights_status not in RENDER_BLOCKING_RIGHTS_STATUSES
        origin = material.get("origin") if isinstance(material.get("origin"), dict) else {}
        index[material_id] = {
            "material_id": material_id,
            "video_id": _clean(origin.get("video_id")),
            "share_url": _clean(origin.get("share_url")),
            "title": _clean(origin.get("title")),
            "author": _clean(origin.get("author")),
            "duration_seconds": round(int(material.get("duration_ms") or 0) / 1000.0, 3),
            "crop_bottom_ratio": 0.0,
            "availability": "available" if available else "unavailable",
            "rights": {
                "render_eligible": render_eligible,
                "license": _clean(rights.get("license")),
                "attribution": _clean(rights.get("attribution")),
                "note": rights_status,
            },
            "segments": [
                {
                    "segment_id": _clean(segment.get("segment_id")),
                    "in_ms": int(segment.get("start_ms") or 0),
                    "out_ms": int(segment.get("end_ms") or 0),
                    "note": _clean(segment.get("purpose")),
                    "claim_ids": [str(ref) for ref in segment.get("claim_ids") or []],
                }
                for segment in material.get("segments") or []
            ],
        }
    return index


def _eligible_segments(
    materials: dict[str, dict[str, Any]], claim_id: str, material_refs: list[str]
) -> list[dict[str, Any]]:
    """claim 的候选素材 → 可渲染且可用的细分区间（只保留该 claim 关联的片段）。"""
    rows: list[dict[str, Any]] = []
    for material_id in material_refs:
        material = materials.get(str(material_id))
        if not material or not material["rights"]["render_eligible"] or material["availability"] != "available":
            continue
        for segment in material["segments"]:
            if segment["claim_ids"] and claim_id not in segment["claim_ids"]:
                continue
            rows.append(
                {
                    "material_id": material["material_id"],
                    "segment_id": segment["segment_id"],
                    "in_ms": segment["in_ms"],
                    "out_ms": segment["out_ms"],
                    "note": segment["note"],
                }
            )
    return rows


# --------------------------------------------------------------------------- #
# 主题裁决：selected_topic 只是提案，Haike 必须重过全部门
# --------------------------------------------------------------------------- #
def _argument_nodes(
    snapshot: dict[str, Any], materials: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """把 argument_graph 的节点转成论证条目；不可用节点成为阻断原因。"""
    claims = {str(claim.get("claim_id") or ""): claim for claim in snapshot.get("claims") or []}
    theme_statement = _clean(snapshot.get("theme"))
    arguments: list[dict[str, Any]] = []
    blockers: list[str] = []
    notes: list[str] = []
    for position, node in enumerate(snapshot.get("argument_graph", {}).get("nodes") or [], 1):
        claim_id = str(node.get("claim_id") or "")
        dim = _clean(node.get("dim"))
        claim = claims.get(claim_id)
        if claim is None:
            blockers.append(f"论证节点 {claim_id} 引用了不存在的 claim")
            continue
        if not claim.get("fact_ready"):
            blockers.append(
                f"关键 claim {claim_id}（{dim}）未通过事实门（evidence_status={claim.get('evidence_status')}）"
            )
            continue
        claim_text = _clean(node.get("claim")) or _clean(claim.get("text"))
        material_refs = [str(ref) for ref in claim.get("material_refs") or []]
        eligible = _eligible_segments(materials, claim_id, material_refs)
        if not eligible:
            notes.append(f"claim {claim_id} 没有 rights.render_eligible 且可用的素材窗口")
        arguments.append(
            {
                "arg_id": f"A{position:03d}",
                "dim": dim,
                "claim_id": claim_id,
                "claim": claim_text,
                "source_refs": list(
                    node.get("source_candidate_ids") or claim.get("source_ids") or []
                ),
                "material_candidates": eligible,
                "supports_theme": _theme_support_text(theme_statement, claim_text),
            }
        )
    return arguments, blockers, notes


def _theme_support_text(theme_statement: str, claim_text: str) -> str:
    return f"以「{claim_text}」支撑主题「{theme_statement}」"


def _theme_gap_hook(theme_statement: str) -> str:
    """把立场句转成可口播的「缺口钩子」（确定性派生，非臆造事实）。"""
    return f"{theme_statement}——但真正被忽略的关键在这里。"


def build_editorial_decision(
    snapshot: dict[str, Any],
    *,
    copywriter: Copywriter | None = None,
    min_supported_claims: int = DEFAULT_MIN_SUPPORTED_CLAIMS,
) -> dict[str, Any]:
    """把 normalized editorial snapshot 转成主题先行编辑决策（含 provenance sidecar）。

    fail closed：snapshot 不合规、disposition 为 rejected/research_required、
    product gate 非空、关键 claim 未过事实门，或可用论证少于 ``min_supported_claims``。

    **成稿层**：``claim.text`` 只是论证骨架；只有注入 ``copywriter`` 才产出真正的成稿并
    把 ``script_ready`` 置真，否则只到 ``argument_map_ready`` 草案（不可测 TTS / 生成可排队 spec）。
    """
    try:
        snap = validate_editorial_snapshot(snapshot)
    except EditorialSnapshotError as exc:
        raise EditorialBlocked(exc.issues, stage="validated_snapshot") from exc

    disposition = snap["disposition"]
    blockers: list[str] = []
    if disposition == DISPOSITION_REJECTED:
        blockers.append("disposition=rejected：产品/权利门未通过")
    elif disposition == DISPOSITION_RESEARCH_REQUIRED:
        blockers.append("disposition=research_required：关键事实/时效门未通过")
    for reason in snap.get("product_gate") or []:
        blockers.append(f"product_gate：{reason}")
    if (snap.get("freshness_gates") or {}).get("gate") == DISPOSITION_RESEARCH_REQUIRED:
        blockers.append("freshness 门为 research_required（存在关键时效缺口）")
    # 冻结合同明文 ``selected_topic{...}|null`` ⇒ 校验层不拦 null（合同合法输入）。
    # 编辑层需要明确提案，这件事在这里以**门**的形式表达（exit 3），不是输入错误（exit 2）。
    selected_topic = snap.get("selected_topic")
    if not isinstance(selected_topic, Mapping) or not _clean(selected_topic.get("topic_id")):
        blockers.append("selected_topic 为空：研究包未给出可裁决的选题提案，编辑层无法起步")
    if blockers:
        raise EditorialBlocked(blockers, stage="gate")

    advisories: list[str] = []
    if disposition == DISPOSITION_PARTIAL:
        advisories.append("disposition=partial：存在非关键缺口，可继续但需人工知悉")
    # 校验层对「合同没要求、因此不判输入不合格」的一致性问题只做提醒
    # （selected_topic 不在候选集内 / argument_graph.topic_id 与提案不一致）。
    # 这里原样透传，让不一致出现在 provenance 里而不是变成一道假的输入错误。
    advisories.extend(str(item) for item in snap.get("consistency_advisories") or [])
    # 注意：snapshot.partial_package 只表示技术/运输损坏；这类包在权威校验层即被拒、
    # 到不了编辑层，因此这里**不**把它当作研究缺口信号（研究缺口由 disposition /
    # freshness_gates 表达），只在 provenance.gates 里原样留证。

    topic_id = _clean(snap["selected_topic"].get("topic_id"))
    selection_basis = _clean(snap["selected_topic"].get("selection_basis"))
    theme = {
        "theme_id": topic_id,
        "statement": _clean(snap.get("theme")),
        "stance": _clean(snap.get("theme")),
        "rationale": selection_basis,
        "proposal_topic_id": topic_id,
        "proposal_overruled": False,
    }

    materials = _materials_index(snap)
    arguments, arg_blockers, arg_notes = _argument_nodes(snap, materials)
    if arg_blockers:
        raise EditorialBlocked(arg_blockers, stage="theme_selected")
    if len(arguments) < max(1, int(min_supported_claims)):
        raise EditorialBlocked(
            [f"选题 {topic_id} 仅 {len(arguments)} 条可用论证，少于最低 {min_supported_claims} 条"],
            stage="theme_selected",
        )

    gaps: list[dict[str, Any]] = []
    draft_sections: list[dict[str, Any]] = []
    for position, argument in enumerate(arguments, 1):
        section_id = f"T{position:03d}"
        if not argument["material_candidates"]:
            gaps.append(
                {
                    "section_id": section_id,
                    "claim_id": argument["claim_id"],
                    "reason": "没有 rights.render_eligible 且可用的素材窗口",
                }
            )
        draft_sections.append(
            {
                "id": section_id,
                "turn_id": section_id,
                "label": DIM_LABELS.get(argument["dim"], argument["dim"]),
                "text": argument["claim"],
                "dim": argument["dim"],
                "theme_support": argument["supports_theme"],
                "claim_refs": [argument["claim_id"]],
                "source_refs": list(argument["source_refs"]),
                "material_candidates": [dict(item) for item in argument["material_candidates"]],
                "status": "draft",
            }
        )

    if copywriter is None:
        sections = draft_sections
    else:
        context = _copywriter_context(snap, theme, arguments, gaps, advisories)
        sections = _run_copywriter(copywriter, context, snap, arguments)
    script_ready = copywriter is not None and bool(sections)

    stages = {
        "validated_snapshot": True,
        "keywords_ready": bool(snap.get("keywords")),
        "theme_candidates_ready": bool(snap.get("topic_candidates")),
        "theme_selected": True,
        "argument_map_ready": bool(arguments),
        "script_ready": script_ready,
        "duration_measured": False,
        "remake_spec_ready": False,
    }

    edges = [dict(edge) for edge in snap.get("argument_graph", {}).get("edges") or []]
    provenance = {
        "pack_id": snap.get("pack_id", ""),
        "episode_id": snap.get("episode_id", ""),
        "revision": snap.get("revision", 0),
        "content_sha256": snap.get("content_sha256", ""),
        "manifest_sha256": snap.get("manifest_sha256", ""),
        "keywords": snap.get("keywords") or {},
        "topic_candidates": [dict(item) for item in snap.get("topic_candidates") or []],
        "selected_topic": dict(snap.get("selected_topic") or {}),
        "selected_theme": {
            "theme_id": topic_id,
            "statement": theme["statement"],
            "rationale": selection_basis,
            "gap_hook": _theme_gap_hook(theme["statement"]),
            "proposal_overruled": False,
            "selection_reason": (
                f"选题 {topic_id} 通过 Haike 全部重裁决门（disposition/freshness/product/rights/facts），"
                f"支撑 {len(arguments)} 条论证"
            ),
        },
        "argument_map": [
            {
                "arg_id": argument["arg_id"],
                "dim": argument["dim"],
                "claim_id": argument["claim_id"],
                "source_refs": list(argument["source_refs"]),
                "material_refs": [
                    {"material_id": item["material_id"], "segment_id": item["segment_id"]}
                    for item in argument["material_candidates"]
                ],
            }
            for argument in arguments
        ],
        "argument_graph": {
            "topic_id": snap.get("argument_graph", {}).get("topic_id", ""),
            "edges": edges,
            "notes": list(arg_notes),
        },
        "sections": [
            {
                "id": section["id"],
                "claim_refs": list(section["claim_refs"]),
                "source_refs": list(section["source_refs"]),
                "material_refs": [
                    {"material_id": item["material_id"], "segment_id": item["segment_id"]}
                    for item in section["material_candidates"]
                ],
                "theme_support": section["theme_support"],
            }
            for section in sections
        ],
        "gates": {
            "disposition": disposition,
            "disposition_reason": snap.get("disposition_reason", ""),
            "producer_disposition": snap.get("producer_disposition", ""),
            "partial_package": bool(snap.get("partial_package")),
            "freshness_gate": (snap.get("freshness_gates") or {}).get("gate", ""),
            "product_gate": list(snap.get("product_gate") or []),
        },
        "advisories": advisories,
        "gaps": [dict(gap) for gap in gaps],
        "duration_probe": None,
    }

    return {
        "schema_version": REMAKE_SPEC_SCHEMA_VERSION,
        "snapshot": {
            "episode_id": snap.get("episode_id", ""),
            "pack_id": snap.get("pack_id", ""),
            "revision": snap.get("revision", 0),
            "content_sha256": snap.get("content_sha256", ""),
        },
        "keywords": snap.get("keywords") or {},
        "topic_candidates": [dict(item) for item in snap.get("topic_candidates") or []],
        "theme": theme,
        "argument_map": arguments,
        "argument_graph": {"topic_id": snap.get("argument_graph", {}).get("topic_id", ""), "edges": edges},
        "sections": sections,
        "materials": materials,
        "stages": stages,
        "advisories": advisories,
        "gaps": [dict(gap) for gap in gaps],
        "provenance": provenance,
    }


# --------------------------------------------------------------------------- #
# 成稿层
# --------------------------------------------------------------------------- #
def _copywriter_context(
    snapshot: dict[str, Any],
    theme: dict[str, Any],
    arguments: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    advisories: list[str],
) -> dict[str, Any]:
    """交给 copywriter 的只读上下文（纯数据，无网络/LLM/IO）。"""
    claim_ids = {argument["claim_id"] for argument in arguments}
    return {
        "theme": {
            "theme_id": theme["theme_id"],
            "statement": theme["statement"],
            "rationale": theme["rationale"],
            "gap_hook": _theme_gap_hook(theme["statement"]),
        },
        "keywords": snapshot.get("keywords") or {},
        "argument_map": [
            {
                "arg_id": argument["arg_id"],
                "dim": argument["dim"],
                "claim_id": argument["claim_id"],
                "claim": argument["claim"],
                "source_refs": list(argument["source_refs"]),
            }
            for argument in arguments
        ],
        "claims": {
            str(claim.get("claim_id") or ""): dict(claim)
            for claim in snapshot.get("claims") or []
            if str(claim.get("claim_id") or "") in claim_ids
        },
        "gaps": [dict(gap) for gap in gaps],
        "advisories": list(advisories),
        "constraints": {
            "must_reference_claim_ids": sorted(claim_ids),
            "must_explain_theme_support": True,
            "must_be_original_text": True,
            "no_unreferenced_facts": True,
        },
    }


def _number_tokens(text: str) -> set[str]:
    """阿拉伯数字 token（用于「禁止引入未引用事实」的确定性近似检查）。"""
    return {token.rstrip("，。；、") for token in re.findall(r"\d+(?:\.\d+)?", str(text or ""))}


def _run_copywriter(
    copywriter: Copywriter,
    context: dict[str, Any],
    snapshot: dict[str, Any],
    arguments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """调用注入的 copywriter 并严格校验其产物；不合规一律 fail closed。"""
    if not callable(copywriter):
        raise EditorialBlocked(["copywriter 必须是可调用对象"], stage="script_ready")
    try:
        raw = copywriter(context)
    except EditorialBlocked:
        raise
    except Exception as exc:  # noqa: BLE001 - copywriter 抛错即阻断成稿
        raise EditorialBlocked([f"copywriter 执行失败：{exc}"], stage="script_ready") from exc
    if not isinstance(raw, list) or not raw:
        raise EditorialBlocked(["copywriter 必须返回非空的 section 列表"], stage="script_ready")

    claims = {str(claim.get("claim_id") or ""): claim for claim in snapshot.get("claims") or []}
    dim_by_claim = {argument["claim_id"]: argument["dim"] for argument in arguments}
    expected_claim_ids = set(dim_by_claim)
    issues: list[str] = []
    sections: list[dict[str, Any]] = []
    covered: set[str] = set()
    for position, item in enumerate(raw, 1):
        section_id = f"T{position:03d}"
        if not isinstance(item, dict):
            issues.append(f"section {section_id} 不是对象")
            continue
        text = _clean(item.get("text"))
        if not text:
            issues.append(f"section {section_id} 缺少台词文本")
            continue
        claim_refs = [str(ref) for ref in (item.get("claim_refs") or [])]
        if not claim_refs:
            issues.append(f"section {section_id} 未引用任何 claim（禁止无源事实）")
            continue
        unknown = [ref for ref in claim_refs if ref not in claims]
        if unknown:
            issues.append(f"section {section_id} 引用了不存在的 claim：{'、'.join(unknown)}")
            continue
        theme_support = _clean(item.get("theme_support"))
        if not theme_support:
            issues.append(f"section {section_id} 未说明如何支撑主题")
            continue
        referenced_texts = [_clean(claims[ref].get("text")) for ref in claim_refs]
        if text in referenced_texts:
            issues.append(f"section {section_id} 直接复用了 claim 原文，必须重写成原创口播")
            continue
        grounded = " ".join(referenced_texts)
        ungrounded = _number_tokens(text) - _number_tokens(grounded)
        if ungrounded:
            issues.append(f"section {section_id} 引入了未引用的事实数字：{'、'.join(sorted(ungrounded))}")
            continue
        material_candidates: list[dict[str, Any]] = []
        source_refs: list[str] = []
        seen: set[tuple[str, str]] = set()
        for argument in arguments:
            if argument["claim_id"] not in claim_refs:
                continue
            source_refs.extend(argument["source_refs"])
            for candidate in argument["material_candidates"]:
                key = (candidate["material_id"], candidate["segment_id"])
                if key not in seen:
                    seen.add(key)
                    material_candidates.append(dict(candidate))
        covered.update(claim_refs)
        dims = {dim_by_claim[ref] for ref in claim_refs if ref in dim_by_claim}
        dim = next(iter(dims)) if len(dims) == 1 else "mixed"
        sections.append(
            {
                "id": section_id,
                "turn_id": section_id,
                "label": _clean(item.get("label")) or DIM_LABELS.get(dim, dim),
                "text": text,
                "dim": dim,
                "theme_support": theme_support,
                "claim_refs": list(dict.fromkeys(claim_refs)),
                "source_refs": list(dict.fromkeys(source_refs)),
                "material_candidates": material_candidates,
                "status": "scripted",
            }
        )

    missing = expected_claim_ids - covered
    if missing:
        issues.append(f"成稿未覆盖全部论证 claim：缺 {'、'.join(sorted(missing))}")
    if issues:
        raise EditorialBlocked(issues, stage="script_ready")
    return sections


# --------------------------------------------------------------------------- #
# 真实时长探针（注入式；测试只用 stub，禁止真实 TTS/网络）
# --------------------------------------------------------------------------- #
DurationProbe = Callable[..., Any]


def _probe_seconds(result: Any) -> tuple[float, dict[str, Any]]:
    if isinstance(result, dict):
        raw = result.get("seconds", result.get("duration_seconds", result.get("duration")))
        meta = {
            "provider": _clean(result.get("provider")),
            "model": _clean(result.get("model")),
            "version": _clean(result.get("version")),
        }
    else:
        raw = result
        meta = {"provider": "", "model": "", "version": ""}
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise DurationProbeError(f"时长探针返回了非数值结果：{raw!r}") from exc
    if seconds <= 0:
        raise DurationProbeError(f"时长探针返回了非正数秒数：{seconds}")
    return seconds, meta


def measure_sections(
    sections: list[dict[str, Any]],
    *,
    duration_probe: DurationProbe,
    speaker: str | None = None,
    voice: str | None = None,
    measured_at: str | None = None,
) -> dict[str, Any]:
    """对每段台词调用注入探针，得到**真实秒数**（音频是主时钟）。

    探针签名：``duration_probe(text, *, speaker=None, voice=None)`` → 秒数或
    ``{"seconds": float, "provider": str, "model": str, "version": str}``。
    本函数只调用注入对象，绝不内建任何 TTS/网络路径。

    **先成稿再量时长**：只有 ``status == "scripted"`` 的段才能测；论证骨架草案禁止被送去 TTS。
    """
    if duration_probe is None or not callable(duration_probe):
        raise DurationProbeError("必须注入可调用的 duration_probe")
    not_scripted = [str(section.get("id")) for section in sections if section.get("status") != "scripted"]
    if not_scripted:
        raise DurationProbeError(
            f"以下段落尚未成稿（无 copywriter 时只是论证骨架），禁止测 TTS：{'、'.join(not_scripted)}"
        )
    measured: list[dict[str, Any]] = []
    probe_rows: list[dict[str, Any]] = []
    provider = model = version = ""
    total = 0.0
    for section in sections:
        text = _clean(section.get("text"))
        if not text:
            raise DurationProbeError(f"section {section.get('id')} 缺少台词文本")
        result = duration_probe(text, speaker=speaker, voice=voice)
        seconds, meta = _probe_seconds(result)
        provider = provider or meta["provider"]
        model = model or meta["model"]
        version = version or meta["version"]
        total += seconds
        measured.append({**section, "measured_seconds": round(seconds, 3)})
        probe_rows.append(
            {"section_id": section.get("id"), "turn_id": section.get("turn_id"), "seconds": round(seconds, 3)}
        )
    return {
        "sections": measured,
        "probe": {
            "provider": provider,
            "model": model,
            "version": version,
            "speaker": _clean(speaker),
            "voice": _clean(voice),
            "measured_at": _clean(measured_at),
            "sections": probe_rows,
        },
        "total_measured_seconds": round(total, 3),
    }


# --------------------------------------------------------------------------- #
# 组装 remake-spec-v1
# --------------------------------------------------------------------------- #
def build_remake_spec(
    decision: dict[str, Any],
    measurements: dict[str, Any],
    *,
    project_id: str,
    fps: int = 30,
    aspect: str = "portrait",
    title: str = "",
    brief: str = "",
    style_playbook: str = "clean-professional",
    voice: dict[str, Any] | None = None,
    music: dict[str, Any] | None = None,
    material_root: str = "",
    allow_partial: bool = False,
) -> dict[str, Any]:
    """把编辑决策 + 真实时长合并成 remake-spec-v1。

    画面时长适配配音：每段总帧数由 measured_seconds 量化而来，段内镜头首尾相接覆盖，
    源窗口 ``out - in`` 的帧数与显示帧数一致。任何无法覆盖的段落 → 抛 `RemakeSpecError`
    （`allow_partial=True` 时改为写进 ``gaps``，同样不伪造镜头）。

    只有 ``stages.script_ready`` 为真（经 copywriter 成稿）的决策才允许生成**可排队** spec。
    """
    if not (decision.get("stages") or {}).get("script_ready"):
        raise RemakeSpecError(
            ["编辑决策尚未成稿（缺 copywriter，仅到 argument_map_ready 草案），禁止生成可排队 spec"]
        )
    fps_value = _fps(fps)
    measured_by_id = {section["id"]: section for section in measurements.get("sections", [])}
    materials = decision.get("materials") or {}
    issues: list[str] = []
    gaps: list[dict[str, Any]] = list(decision.get("gaps", []))

    spec_sources: dict[str, dict[str, Any]] = {}
    spec_sections: list[dict[str, Any]] = []
    total_frames = 0
    total_seconds = 0.0

    for section in decision.get("sections", []):
        measured = measured_by_id.get(section["id"])
        if not measured or "measured_seconds" not in measured:
            issues.append(f"section {section['id']} 缺少真实时长")
            continue
        section_frames = quantize_frames(measured["measured_seconds"], fps_value)
        if section_frames <= 0:
            issues.append(f"section {section['id']} 量化后不足一帧")
            continue

        candidates = _usable_candidates(section, materials)
        if not candidates:
            issue = f"section {section['id']} 没有可用素材（rights.render_eligible + 可用）"
            issues.append(issue)
            gaps.append({"section_id": section["id"], "reason": issue})
            continue

        shots: list[dict[str, Any]] = []
        cursor_frame = 0
        for candidate in candidates:
            if cursor_frame >= section_frames:
                break
            material = candidate["material"]
            segment = candidate["segment"]
            window = quantize_source_window(segment["in_ms"], segment["out_ms"], fps_value)
            capacity = quantize_frames(window["out"], fps_value) - quantize_frames(window["in"], fps_value)
            if capacity <= 0:
                continue
            take = min(capacity, section_frames - cursor_frame)
            source_in_frame = quantize_frames(window["in"], fps_value)
            source_out_frame = source_in_frame + take
            display_start = cursor_frame
            display_end = cursor_frame + take
            key = _source_key(material)
            spec_sources.setdefault(key, _spec_source(material))
            shots.append(
                {
                    "source": key,
                    "in": frames_to_seconds(source_in_frame, fps_value),
                    "out": frames_to_seconds(source_out_frame, fps_value),
                    "intent": segment["note"] or material["title"] or key,
                    "start_seconds": frames_to_seconds(display_start, fps_value),
                    "end_seconds": frames_to_seconds(display_end, fps_value),
                }
            )
            cursor_frame = display_end

        if cursor_frame < section_frames:
            issue = (
                f"section {section['id']} 素材容量不足：需要 {section_frames} 帧，"
                f"仅覆盖 {cursor_frame} 帧"
            )
            issues.append(issue)
            gaps.append({"section_id": section["id"], "reason": issue})
            if not (allow_partial and shots):
                continue

        if not shots:
            continue
        total_frames += cursor_frame
        total_seconds = frames_to_seconds(total_frames, fps_value)
        spec_sections.append(
            {
                "id": section["id"],
                "turn_id": section["turn_id"],
                "label": section["label"],
                "text": section["text"],
                "dim": section["dim"],
                "theme_support": section["theme_support"],
                "claim_refs": list(section["claim_refs"]),
                "source_refs": list(section["source_refs"]),
                "measured_seconds": round(float(measured["measured_seconds"]), 3),
                "duration_seconds": frames_to_seconds(cursor_frame, fps_value),
                "shots": shots,
            }
        )

    if issues and not allow_partial:
        raise RemakeSpecError(issues)

    advisories: list[str] = list(decision.get("advisories") or [])
    if total_seconds > MANUAL_DURATION_HINT_SECONDS:
        advisories.append(
            f"预计成片 {total_seconds:.1f} 秒，超过 {MANUAL_DURATION_HINT_SECONDS:.0f} 秒安全提示线；"
            "请人工确认取舍，系统不会自动压缩"
        )

    theme = decision.get("theme") or {}
    spec = {
        "schema_version": REMAKE_SPEC_SCHEMA_VERSION,
        "project_id": project_id,
        "title": title or theme.get("statement") or "",
        "brief": brief,
        "aspect": aspect,
        "pipeline_type": PIPELINE_TYPE,
        "style_playbook": style_playbook,
        "fps": fps_value,
        "theme": {
            "theme_id": theme.get("theme_id", ""),
            "statement": theme.get("statement", ""),
            "stance": theme.get("stance", ""),
            "rationale": theme.get("rationale", ""),
        },
        "voice": dict(voice or {}),
        "music": dict(music or {}),
        "material_root": material_root,
        "sources": list(spec_sources.values()),
        "rejected_sources": [],
        "sections": spec_sections,
        "duration": {
            "measured_seconds": round(float(measurements.get("total_measured_seconds", 0.0)), 3),
            "total_seconds": total_seconds,
            "total_frames": total_frames,
            "fps": fps_value,
        },
        "gaps": gaps,
        "advisories": advisories,
    }
    return spec


def _source_key(material: dict[str, Any]) -> str:
    return material["material_id"]


def _spec_source(material: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": _source_key(material),
        "aweme_id": material["video_id"],
        "title": material["title"],
        "author": material["author"],
        "url": material["share_url"],
        "crop_bottom_ratio": material["crop_bottom_ratio"],
        "duration_seconds": material["duration_seconds"],
        "rights": dict(material["rights"]),
        "availability": material["availability"],
        "note": "",
    }


def _usable_candidates(section: dict[str, Any], materials: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in section.get("material_candidates", []):
        material = materials.get(candidate["material_id"])
        if not material or not material["rights"]["render_eligible"]:
            continue
        if material["availability"] != "available":
            continue
        segment = next(
            (seg for seg in material["segments"] if seg["segment_id"] == candidate["segment_id"]),
            None,
        )
        if not segment:
            continue
        rows.append({"material": material, "segment": segment})
    return rows


# --------------------------------------------------------------------------- #
# 校验器（与 schemas/remake-spec-v1.json 保持一致）
# --------------------------------------------------------------------------- #
def validate_remake_spec(spec: dict[str, Any], *, fps: int | None = None) -> dict[str, Any]:
    """校验 remake-spec-v1，返回 ``{"valid": bool, "issues": [...]}``（不抛异常）。"""
    issues: list[str] = []
    if not isinstance(spec, dict):
        return {"valid": False, "issues": ["spec 必须是 JSON 对象"]}

    if spec.get("schema_version") != REMAKE_SPEC_SCHEMA_VERSION:
        issues.append(f"schema_version 必须是 {REMAKE_SPEC_SCHEMA_VERSION}")
    if spec.get("pipeline_type") != PIPELINE_TYPE:
        issues.append(f"pipeline_type 必须是 {PIPELINE_TYPE}")

    fps_value = _fps(fps if fps is not None else spec.get("fps", 30))

    sources = spec.get("sources") or []
    if not isinstance(sources, list) or not sources:
        issues.append("sources 不能为空")
        sources = []
    source_keys = [str(source.get("key") or "") for source in sources]
    if any(not key for key in source_keys):
        issues.append("sources[].key 不能为空")
    if len(source_keys) != len(set(source_keys)):
        issues.append("sources[].key 必须唯一")
    source_by_key = {str(source.get("key")): source for source in sources}

    sections = spec.get("sections") or []
    if not isinstance(sections, list) or not sections:
        issues.append("sections 不能为空")
        sections = []
    section_ids: list[str] = []
    turn_ids: list[str] = []
    for section in sections:
        section_id = str(section.get("id") or "")
        turn_id = str(section.get("turn_id") or "")
        section_ids.append(section_id)
        turn_ids.append(turn_id)
        if not section_id:
            issues.append("section id 不能为空")
        if not turn_id:
            issues.append(f"section {section_id or '[空]'} 缺少 turn_id")
        elif turn_id != section_id:
            issues.append(f"section {section_id} 的 turn_id 必须与 id 一致（当前 {turn_id}）")
        _validate_section(section, fps=fps_value, source_by_key=source_by_key, issues=issues)
    if len(section_ids) != len(set(section_ids)):
        issues.append("section id 必须唯一")
    if len(turn_ids) != len(set(turn_ids)):
        issues.append("turn_id 必须唯一")

    duration = spec.get("duration") or {}
    if not isinstance(duration, dict):
        issues.append("duration 必须是对象")
    else:
        if int(duration.get("fps", fps_value)) != fps_value:
            issues.append("duration.fps 必须与 spec.fps 一致")
        total_frames = int(duration.get("total_frames", -1))
        if total_frames < 0:
            issues.append("duration.total_frames 缺失")
        elif abs(frames_to_seconds(total_frames, fps_value) - float(duration.get("total_seconds", -1))) > 0.002:
            issues.append("duration.total_seconds 与 total_frames 不一致")

    return {"valid": not issues, "issues": issues}


def _validate_section(
    section: dict[str, Any],
    *,
    fps: int,
    source_by_key: dict[str, dict[str, Any]],
    issues: list[str],
) -> None:
    section_id = str(section.get("id") or "[空]")
    shots = section.get("shots") or []
    if not isinstance(shots, list) or not shots:
        issues.append(f"section {section_id} 至少需要一个镜头")
        return
    section_frames = quantize_frames(section.get("duration_seconds"), fps)
    previous_end_frame = 0
    for index, shot in enumerate(shots, 1):
        source_key = str(shot.get("source") or "")
        source = source_by_key.get(source_key)
        if not source:
            issues.append(f"section {section_id} 第 {index} 镜引用了不存在的 source：{source_key or '[空]'}")
            continue
        rights = source.get("rights") or {}
        if not rights.get("render_eligible"):
            issues.append(f"section {section_id} 第 {index} 镜的素材无渲染授权")
        if str(source.get("availability")) != "available":
            issues.append(f"section {section_id} 第 {index} 镜的素材不可用")
        try:
            in_seconds = float(shot.get("in"))
            out_seconds = float(shot.get("out"))
        except (TypeError, ValueError):
            issues.append(f"section {section_id} 第 {index} 镜缺少源窗口")
            continue
        if out_seconds <= in_seconds:
            issues.append(f"section {section_id} 第 {index} 镜源出点必须晚于源入点")
            continue
        available = float(source.get("duration_seconds") or 0.0)
        if available > 0 and out_seconds > available + (1.0 / fps):
            issues.append(f"section {section_id} 第 {index} 镜源出点超过素材时长")
        start_frame = quantize_frames(shot.get("start_seconds"), fps)
        end_frame = quantize_frames(shot.get("end_seconds"), fps)
        in_frame = quantize_frames(in_seconds, fps)
        out_frame = quantize_frames(out_seconds, fps)
        if start_frame != previous_end_frame:
            issues.append(f"section {section_id} 第 {index} 镜未首尾相接（视觉时间线存在空白或重叠）")
        if end_frame <= start_frame:
            issues.append(f"section {section_id} 第 {index} 镜显示时长不足一帧")
        if abs((end_frame - start_frame) - (out_frame - in_frame)) > 1:
            issues.append(f"section {section_id} 第 {index} 镜源区间与显示区间未按帧一致")
        previous_end_frame = end_frame
    if previous_end_frame != section_frames:
        issues.append(
            f"section {section_id} 视觉时间线必须连续覆盖整段（{section_frames} 帧，实际 {previous_end_frame} 帧）"
        )
    measured = section.get("measured_seconds")
    if measured is not None:
        body = frames_to_seconds(section_frames, fps)
        if abs(float(measured) - body) > (1.0 / fps) + 0.002:
            issues.append(f"section {section_id} 显示时长与实测秒数偏差超过一帧")


# --------------------------------------------------------------------------- #
# 成稿后检查表（只提示，不定段数）
# --------------------------------------------------------------------------- #
def audit_narrative_shape(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """把固定七部件当**成稿后自检**：报告覆盖了哪些、缺哪些，不改变段数。"""
    dims = {str(section.get("dim") or "") for section in sections}
    present = [
        {"part": key, "label": label, "matched_dims": sorted(dims & set(part_dims))}
        for key, label, part_dims in SEVEN_PART_CHECKLIST
        if dims & set(part_dims)
    ]
    missing = [
        {"part": key, "label": label, "expected_dims": sorted(part_dims)}
        for key, label, part_dims in SEVEN_PART_CHECKLIST
        if not (dims & set(part_dims))
    ]
    return {
        "section_count": len(sections),
        "section_count_is_dynamic": True,
        "present_parts": present,
        "missing_parts": missing,
        "checklist_only": True,
    }


# --------------------------------------------------------------------------- #
# 编排入口（供后续 orchestrator 调用）
# --------------------------------------------------------------------------- #
def build_editorial_package(
    snapshot: dict[str, Any],
    *,
    duration_probe: DurationProbe,
    project_id: str,
    copywriter: Copywriter | None = None,
    fps: int = 30,
    speaker: str | None = None,
    voice: str | None = None,
    measured_at: str | None = None,
    allow_partial: bool = False,
    **spec_kwargs: Any,
) -> dict[str, Any]:
    """端到端：editorial snapshot → 编辑决策 → 真实时长 → remake-spec-v1（+ provenance）。

    必须注入 ``copywriter``（成稿层）与 ``duration_probe``（真实时长）；缺 copywriter 时
    会因 ``script_ready`` 未达而在量时长/生成 spec 阶段 fail closed。
    """
    decision = build_editorial_decision(snapshot, copywriter=copywriter)
    measurements = measure_sections(
        decision["sections"],
        duration_probe=duration_probe,
        speaker=speaker,
        voice=voice,
        measured_at=measured_at,
    )
    decision["stages"]["duration_measured"] = True
    decision["provenance"]["duration_probe"] = dict(measurements["probe"])
    spec = build_remake_spec(
        decision,
        measurements,
        project_id=project_id,
        fps=fps,
        allow_partial=allow_partial,
        **spec_kwargs,
    )
    validation = validate_remake_spec(spec, fps=fps)
    if not validation["valid"]:
        raise RemakeSpecError(validation["issues"])
    decision["stages"]["remake_spec_ready"] = True
    decision["provenance"]["duration_probe"]["total_measured_seconds"] = measurements["total_measured_seconds"]
    return {
        "decision": decision,
        "provenance": decision["provenance"],
        "measurements": measurements,
        "spec": spec,
        "validation": validation,
    }


def dump_package(package: dict[str, Any], *, directory: str | None = None) -> dict[str, str]:
    """把 spec 与 provenance sidecar 分别落盘（provenance 绝不写进 script section）。"""
    from pathlib import Path

    payloads = {
        "spec": json.dumps(package["spec"], ensure_ascii=False, indent=2),
        "provenance": json.dumps(package["provenance"], ensure_ascii=False, indent=2),
    }
    if not directory:
        return payloads
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    spec_path = root / "remake-spec.json"
    provenance_path = root / "remake-provenance.json"
    spec_path.write_text(payloads["spec"], encoding="utf-8")
    provenance_path.write_text(payloads["provenance"], encoding="utf-8")
    return {"spec": str(spec_path), "provenance": str(provenance_path)}


__all__ = [
    "DEFAULT_MIN_SUPPORTED_CLAIMS",
    "DIM_LABELS",
    "DurationProbeError",
    "EditorialBlocked",
    "MANUAL_DURATION_HINT_SECONDS",
    "PIPELINE_TYPE",
    "REMAKE_SPEC_SCHEMA_VERSION",
    "RemakeSpecError",
    "SEVEN_PART_CHECKLIST",
    "audit_narrative_shape",
    "build_editorial_decision",
    "build_editorial_package",
    "build_remake_spec",
    "dump_package",
    "frames_to_seconds",
    "measure_sections",
    "quantize_frames",
    "quantize_source_window",
    "validate_remake_spec",
]
