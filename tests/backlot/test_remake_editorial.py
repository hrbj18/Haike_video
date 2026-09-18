"""主题先行编辑转换层测试（纯本地；TTS 时长只用 stub，禁止真实 TTS/网络/LLM）。

输入是 intake 权威层产出的 **normalized editorial snapshot**
（``openmontage-editorial-snapshot-v1``）。核心是「主题先行」：``selected_topic`` 只是提案，
Haike 必须重过 disposition/freshness/product/rights/facts 全部门；``claim.text`` 只是论证骨架，
真正的口播必须由注入的 ``copywriter`` 产出；段数由论证容量决定，固定 7 段只作**成稿后检查表**。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from backlot import research_pack_snapshot as rps
from backlot.copy_skill_research_pack import CONTRACT
from backlot.remake_editorial import (
    MANUAL_DURATION_HINT_SECONDS,
    DurationProbeError,
    EditorialBlocked,
    RemakeSpecError,
    audit_narrative_shape,
    build_editorial_decision,
    build_editorial_package,
    build_remake_spec,
    frames_to_seconds,
    measure_sections,
    quantize_frames,
    quantize_source_window,
    validate_remake_spec,
)
from backlot.remake_intake import SNAPSHOT_SCHEMA
from backlot.remake_project import build_script
from tests.backlot.test_copy_skill_research_pack import (
    build_pack,
    default_argument_node,
    default_payloads,
    with_argument_nodes,
)

REPO = Path(__file__).resolve().parents[2]
SCRIPT_SCHEMA = json.loads((REPO / "schemas" / "artifacts" / "script.schema.json").read_text(encoding="utf-8"))
SCRIPT_SECTION_KEYS = set(SCRIPT_SCHEMA["properties"]["sections"]["items"]["properties"])
REMAKE_SCHEMA = json.loads((REPO / "schemas" / "remake-spec-v1.json").read_text(encoding="utf-8"))

NOW = "2026-09-16T23:00:00+08:00"

#: 无阿拉伯数字的确定性 claim 文本（便于分离「禁止引入未引用事实数字」这一条）。
_CLAIM_TEXT = {
    "event_core": "新机铰链通过四十万次折叠测试。",
    "mechanism": "铰链改用液态金属材质。",
    "user_impact": "用户维修成本随之下降。",
}


# --------------------------------------------------------------------------- #
# normalized editorial snapshot 构造器（供本模块与 project 适配层复用）
# --------------------------------------------------------------------------- #
def editorial_snapshot(
    *,
    disposition: str = "ready",
    render_eligible: bool = True,
    fact_ready: bool = True,
    dims: tuple[str, ...] = ("event_core", "mechanism"),
    product_gate: list[str] | None = None,
    freshness_gate: str = "pass",
    edges: list[dict] | None = None,
    duration_ms: int = 60000,
    segments: list[dict] | None = None,
) -> dict:
    """构造一个合法的冻结 snapshot（默认两条论证、两块可渲染素材窗口）。"""
    claim_ids = [f"c{index}" for index in range(1, len(dims) + 1)]
    claim_text = {}
    claims = []
    nodes = []
    for claim_id, dim in zip(claim_ids, dims):
        text = _CLAIM_TEXT.get(dim, f"围绕维度 {dim} 的论证陈述。")
        claim_text[claim_id] = text
        claims.append(
            {
                "claim_id": claim_id,
                "evidence_status": "confirmed_official",
                "fact_ready": fact_ready,
                "wording_policy": "assert",
                "freshness_requirement": "fresh",
                "text": text,
                "claims_to_verify": [],
                "do_not_claim": [],
                "material_refs": ["m1"],
                "source_ids": ["s1"],
                "fact_sources_present": 1,
                "fact_sources_min": 1,
            }
        )
        nodes.append(
            {"claim_id": claim_id, "dim": dim, "claim": text, "source_candidate_ids": ["s1"]}
        )
    if segments is None:
        segments = [
            {
                "segment_id": f"seg{index}",
                "start_ms": (index - 1) * 10000,
                "end_ms": index * 10000,
                "claim_ids": [claim_id],
                "purpose": "visual_support",
                "transcript_excerpt": "",
                "frame_evidence_ids": [],
            }
            for index, claim_id in enumerate(claim_ids, 1)
        ]
    return {
        "schema": SNAPSHOT_SCHEMA,
        "contract": CONTRACT,
        "episode_id": "2026-09-16-测试主题",
        "business_date": "2026-09-16",
        "theme": "折叠屏的真正门槛是铰链寿命。",
        "production_mode": "single_topic_material_replication",
        "revision": 1,
        "pack_id": "2026-09-16-测试主题-r1",
        "content_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "disposition": disposition,
        "producer_disposition": "ready",
        "disposition_reason": "",
        "partial_package": False,
        "keywords": {
            "seed": "折叠屏",
            "expanded": [],
            "subject_terms": [],
            "event_terms": [],
            "keywords_requested": [],
            "keywords_used": ["折叠屏"],
            "keywords_truncated": False,
        },
        "topic_candidates": [
            {
                "topic_id": "topic-01",
                "title": "折叠屏铰链",
                "keywords": ["折叠屏"],
                "selection_basis": "single_theme",
                "producer_proposal": True,
            }
        ],
        "selected_topic": {
            "topic_id": "topic-01",
            "selection_basis": "single_theme",
            "producer_proposal": True,
        },
        "argument_graph": {"topic_id": "topic-01", "nodes": nodes, "edges": list(edges or [])},
        "sources": [
            {
                "source_id": "s1",
                "authority": "official",
                "verification_state": "verified",
                "heat_only": False,
                "fact_source": True,
                "freshness": {
                    "observed_at": "2026-09-16T10:00:00+08:00",
                    "policy": "event_window",
                    "status_at_publish": "fresh",
                },
                "effective_freshness": "fresh",
                "publisher": "官方发布",
                "title": "官方消息",
                "url": "https://example.com/news",
            }
        ],
        "claims": claims,
        "materials": [
            {
                "material_id": "m1",
                "kind": "b_roll",
                "origin": {
                    "video_id": "v1",
                    "author": "作者",
                    "title": "折叠屏素材",
                    "share_url": "https://www.douyin.com/video/v1",
                },
                "permitted_use": "b_roll_only",
                "freshness_status": "unknown",
                "duration_ms": duration_ms,
                "segments": segments,
            }
        ],
        "rights": [
            {
                "asset_id": "asset-m1",
                "asset_type": "video",
                "origin": "producer_owned" if render_eligible else "platform_content",
                "rights_status": "cleared" if render_eligible else "review_required",
                "license": "remake-only",
                "attribution": "作者",
                "redistribution_allowed": bool(render_eligible),
                "render_eligible": bool(render_eligible),
                "material_id": "m1",
            }
        ],
        "audience": {"status": "known", "summary": "", "segments": []},
        "freshness_gates": {
            "gate": freshness_gate,
            "critical_gaps": [],
            "non_critical_gaps": [],
            "checked_at": "",
        },
        "product_gate": list(product_gate or []),
        "counts": {"sources": 1, "claims": len(claims), "materials": 1, "rights": 1},
        "verified_files": [],
    }


# --------------------------------------------------------------------------- #
# 注入式 stub：copywriter（成稿）与 duration_probe（真实时长）
# --------------------------------------------------------------------------- #
def stub_copywriter(context: dict) -> list[dict]:
    """确定性 stub 成稿器：一条论证 → 一段原创（非 verbatim）口播。"""
    rows = []
    for argument in context["argument_map"]:
        rows.append(
            {
                "label": argument["dim"],
                "text": f"{argument['claim']}——值得展开说。",
                "claim_refs": [argument["claim_id"]],
                "theme_support": f"以「{argument['claim']}」支撑主题「{context['theme']['statement']}」",
            }
        )
    return rows


def stub_probe(text, *, speaker=None, voice=None):
    return {"seconds": 4.0, "provider": "stub-local-tts", "model": "stub-voice-v1", "version": "2026.09"}


def make_probe(seconds: float):
    def probe(text, *, speaker=None, voice=None):
        return {
            "seconds": seconds,
            "provider": "stub-local-tts",
            "model": "stub-voice-v1",
            "version": "2026.09",
        }

    return probe


def scripted_decision(snapshot: dict | None = None, **kwargs) -> dict:
    return build_editorial_decision(snapshot or editorial_snapshot(), copywriter=stub_copywriter, **kwargs)


def build_package(snapshot: dict | None = None, *, probe=stub_probe, **kwargs) -> dict:
    return build_editorial_package(
        snapshot or editorial_snapshot(),
        duration_probe=probe,
        project_id="demo-1",
        copywriter=stub_copywriter,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 主题裁决 / 论证图
# --------------------------------------------------------------------------- #
def test_selected_topic_becomes_theme_and_argument_map():
    decision = build_editorial_decision(editorial_snapshot())
    assert decision["theme"]["theme_id"] == "topic-01"
    assert decision["theme"]["statement"] == "折叠屏的真正门槛是铰链寿命。"
    assert decision["theme"]["rationale"] == "single_theme"
    assert [argument["dim"] for argument in decision["argument_map"]] == ["event_core", "mechanism"]
    assert decision["provenance"]["selected_theme"]["theme_id"] == "topic-01"
    assert decision["provenance"]["selected_theme"]["proposal_overruled"] is False


def test_every_section_explains_theme_support():
    decision = build_editorial_decision(editorial_snapshot())
    for section in decision["sections"]:
        assert section["theme_support"]
        assert decision["theme"]["statement"] in section["theme_support"]
        assert section["claim_refs"]


def test_section_count_is_dynamic_not_fixed_seven():
    assert len(build_editorial_decision(editorial_snapshot())["sections"]) == 2
    three = editorial_snapshot(dims=("event_core", "mechanism", "user_impact"))
    assert len(build_editorial_decision(three)["sections"]) == 3
    assert len(build_editorial_decision(three)["sections"]) != 7


def test_fixed_seven_parts_is_only_a_post_hoc_checklist():
    decision = build_editorial_decision(editorial_snapshot())
    audit = audit_narrative_shape(decision["sections"])
    assert audit["checklist_only"] is True
    assert audit["section_count_is_dynamic"] is True
    assert audit["section_count"] == len(decision["sections"])
    assert {part["part"] for part in audit["missing_parts"]}  # 2 段必然缺部件


def test_argument_graph_edges_are_passed_through_verbatim():
    edges = [{"from": "c1", "to": "c2", "relation": "supports"}]
    snapshot = editorial_snapshot(edges=edges)
    decision = build_editorial_decision(snapshot)
    assert decision["argument_graph"]["edges"] == edges
    assert decision["provenance"]["argument_graph"]["edges"] == edges
    assert decision["argument_graph"]["topic_id"] == "topic-01"


def test_illegal_dimension_fails_closed():
    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(dims=("事实", "mechanism")))
    assert excinfo.value.stage == "validated_snapshot"
    assert any("非法信息维度" in reason for reason in excinfo.value.reasons)


def test_illegal_edge_relation_fails_closed():
    edges = [{"from": "c1", "to": "c2", "relation": "implies"}]
    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(edges=edges))
    assert excinfo.value.stage == "validated_snapshot"
    assert any("非法关系" in reason for reason in excinfo.value.reasons)


def test_claim_not_fact_ready_fails_closed():
    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(fact_ready=False))
    assert excinfo.value.stage == "theme_selected"
    assert any("c1" in reason and "事实门" in reason for reason in excinfo.value.reasons)


def test_min_supported_claims_is_enforced():
    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(dims=("event_core",)), min_supported_claims=2)
    assert excinfo.value.stage == "theme_selected"
    assert any("少于最低" in reason for reason in excinfo.value.reasons)


@pytest.mark.parametrize("disposition", ["research_required", "rejected"])
def test_non_ready_disposition_fails_closed(disposition):
    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(disposition=disposition))
    assert excinfo.value.stage == "gate"
    assert any(disposition in reason for reason in excinfo.value.reasons)


def test_product_gate_fails_closed():
    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(product_gate=["material m1 permitted_use 非 b_roll_only"]))
    assert excinfo.value.stage == "gate"
    assert any("product_gate" in reason for reason in excinfo.value.reasons)


def test_critical_freshness_gate_fails_closed():
    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(freshness_gate="research_required"))
    assert excinfo.value.stage == "gate"
    assert any("research_required" in reason for reason in excinfo.value.reasons)


def test_missing_selected_topic_is_blocked_at_gate():
    """合同允许 ``selected_topic=null`` ⇒ 校验层放过；编辑层的「必须有提案」在 gate 表达。"""
    snapshot = editorial_snapshot()
    snapshot["selected_topic"] = None

    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(snapshot)
    assert excinfo.value.stage == "gate"
    assert any("selected_topic" in reason for reason in excinfo.value.reasons)
    assert all(reason.strip() for reason in excinfo.value.reasons)


def test_partial_disposition_is_advisory_not_blocking():
    decision = build_editorial_decision(editorial_snapshot(disposition="partial"))
    assert decision["stages"]["theme_selected"] is True
    assert decision["sections"]
    assert any("partial" in advisory for advisory in decision["advisories"])
    # partial_package 只表示技术/运输损坏，不得被编辑层当成研究缺口信号。
    assert not any("partial_package" in advisory for advisory in decision["advisories"])
    assert decision["provenance"]["gates"]["partial_package"] is False


def test_gap_when_no_render_eligible_material():
    snapshot = editorial_snapshot(render_eligible=False)
    decision = scripted_decision(snapshot)
    assert decision["gaps"]
    assert all(gap["reason"] for gap in decision["gaps"])
    measurements = measure_sections(decision["sections"], duration_probe=stub_probe)
    with pytest.raises(RemakeSpecError) as excinfo:
        build_remake_spec(decision, measurements, project_id="demo-1")
    assert any("没有可用素材" in issue for issue in excinfo.value.issues)
    with pytest.raises(RemakeSpecError):
        build_package(snapshot)


# --------------------------------------------------------------------------- #
# 成稿层（copywriter）：claim.text 只是论证骨架，原创口播必须由注入 copywriter 产出
# --------------------------------------------------------------------------- #
def test_without_copywriter_only_reaches_argument_map_ready():
    decision = build_editorial_decision(editorial_snapshot())
    assert decision["stages"]["argument_map_ready"] is True
    assert decision["stages"]["script_ready"] is False
    assert all(section["status"] == "draft" for section in decision["sections"])
    # 草案不得被送去测 TTS
    with pytest.raises(DurationProbeError):
        measure_sections(decision["sections"], duration_probe=stub_probe)
    # 草案不得生成可排队 spec
    with pytest.raises(RemakeSpecError):
        build_remake_spec(decision, {"sections": [], "total_measured_seconds": 0.0}, project_id="demo-1")
    with pytest.raises((RemakeSpecError, DurationProbeError)):
        build_editorial_package(editorial_snapshot(), duration_probe=stub_probe, project_id="demo-1")


def test_copywriter_produces_scripted_sections():
    decision = scripted_decision()
    assert decision["stages"]["script_ready"] is True
    claim_texts = {claim["text"] for claim in editorial_snapshot()["claims"]}
    for section in decision["sections"]:
        assert section["status"] == "scripted"
        assert section["claim_refs"]
        assert section["theme_support"]
        assert section["text"] not in claim_texts


def test_copywriter_output_preserves_theme_argument_and_claim_refs():
    decision = scripted_decision()
    argument_claim_ids = {argument["claim_id"] for argument in decision["argument_map"]}
    covered: set[str] = set()
    for section in decision["sections"]:
        covered.update(section["claim_refs"])
        assert decision["theme"]["statement"] in section["theme_support"]
    assert argument_claim_ids <= covered


def test_copywriter_must_preserve_argument_coverage():
    def partial(context):
        argument = context["argument_map"][0]
        return [
            {
                "text": f"{argument['claim']}（改写一版）",
                "claim_refs": [argument["claim_id"]],
                "theme_support": "只覆盖一条论证",
            }
        ]

    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(), copywriter=partial)
    assert excinfo.value.stage == "script_ready"
    assert any("未覆盖" in reason for reason in excinfo.value.reasons)


def test_copywriter_must_not_copy_claim_verbatim():
    def verbatim(context):
        return [
            {"text": argument["claim"], "claim_refs": [argument["claim_id"]], "theme_support": "原文照搬"}
            for argument in context["argument_map"]
        ]

    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(), copywriter=verbatim)
    assert any("原创" in reason or "原文" in reason for reason in excinfo.value.reasons)


def test_copywriter_must_explain_theme_support():
    def no_support(context):
        return [
            {"text": f"{argument['claim']}（改写）", "claim_refs": [argument["claim_id"]]}
            for argument in context["argument_map"]
        ]

    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(), copywriter=no_support)
    assert any("支撑主题" in reason for reason in excinfo.value.reasons)


def test_copywriter_must_not_introduce_unreferenced_numbers():
    def invents(context):
        return [
            {
                "text": f"{argument['claim']}（据说能折 999 次）",
                "claim_refs": [argument["claim_id"]],
                "theme_support": "引入未引用数字",
            }
            for argument in context["argument_map"]
        ]

    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(), copywriter=invents)
    assert any("未引用的事实数字" in reason for reason in excinfo.value.reasons)


def test_copywriter_must_reference_existing_claims():
    def ghost(context):
        return [{"text": "没有出处的台词。", "claim_refs": ["c999"], "theme_support": "无"}]

    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(editorial_snapshot(), copywriter=ghost)
    assert any("不存在" in reason for reason in excinfo.value.reasons)


# --------------------------------------------------------------------------- #
# 真实时长探针（stub）
# --------------------------------------------------------------------------- #
def test_measure_sections_uses_probe_as_master_clock():
    decision = scripted_decision()
    measurements = measure_sections(decision["sections"], duration_probe=stub_probe, speaker="yaya", voice="雅雅")
    assert measurements["sections"][0]["measured_seconds"] == 4.0
    assert measurements["probe"]["provider"] == "stub-local-tts"
    assert measurements["probe"]["model"] == "stub-voice-v1"
    assert measurements["probe"]["version"] == "2026.09"
    assert measurements["probe"]["sections"][0]["section_id"] == "T001"
    assert measurements["total_measured_seconds"] == 8.0


def test_measure_sections_requires_positive_seconds():
    decision = scripted_decision()
    with pytest.raises(DurationProbeError):
        measure_sections(decision["sections"], duration_probe=lambda text, **kwargs: 0.0)
    with pytest.raises(DurationProbeError):
        measure_sections(decision["sections"], duration_probe=None)


# --------------------------------------------------------------------------- #
# 帧量化 / 画面适配音频
# --------------------------------------------------------------------------- #
def test_frame_quantization_matches_workbench_contract():
    assert quantize_frames(4.396, 30) == 132
    assert frames_to_seconds(132, 30) == 4.4
    assert quantize_frames(2.15, 30) == 65  # floor(64.5 + .5)
    window = quantize_source_window(9000, 11150, 30)
    assert window == {"in": 9.0, "out": 11.167}  # 270 → 335 帧
    assert quantize_frames(window["out"]) - quantize_frames(window["in"]) == 65


def test_spec_visual_timeline_covers_section_and_matches_frames():
    package = build_package()
    spec = package["spec"]
    assert validate_remake_spec(spec)["valid"] is True
    for section in spec["sections"]:
        shots = section["shots"]
        assert shots[0]["start_seconds"] == 0.0
        assert shots[-1]["end_seconds"] == section["duration_seconds"]
        previous_end = None
        for shot in shots:
            if previous_end is not None:
                assert shot["start_seconds"] == previous_end
            previous_end = shot["end_seconds"]
            display = quantize_frames(shot["end_seconds"]) - quantize_frames(shot["start_seconds"])
            source = quantize_frames(shot["out"]) - quantize_frames(shot["in"])
            assert display == source
    assert spec["sections"][0]["duration_seconds"] == 4.0
    assert spec["duration"]["total_frames"] == sum(
        quantize_frames(section["duration_seconds"]) for section in spec["sections"]
    )


def test_spec_maps_material_id_video_id_and_share_url():
    package = build_package()
    spec = package["spec"]
    source = spec["sources"][0]
    assert source["key"] == "m1"
    assert source["aweme_id"] == "v1"
    assert source["url"] == "https://www.douyin.com/video/v1"
    assert spec["pipeline_type"] == "avatar-spokesperson"
    for section in spec["sections"]:
        for shot in section["shots"]:
            assert shot["source"] in {s["key"] for s in spec["sources"]}


def test_spec_shot_out_never_exceeds_material_duration():
    package = build_package()
    spec = package["spec"]
    durations = {source["key"]: source["duration_seconds"] for source in spec["sources"]}
    for section in spec["sections"]:
        for shot in section["shots"]:
            assert shot["out"] <= durations[shot["source"]] + 1 / spec["fps"]


def test_insufficient_capacity_yields_gap():
    snapshot = editorial_snapshot(
        segments=[
            {"segment_id": "seg1", "start_ms": 0, "end_ms": 3000, "claim_ids": ["c1"],
             "purpose": "visual_support", "transcript_excerpt": "", "frame_evidence_ids": []},
            {"segment_id": "seg2", "start_ms": 3000, "end_ms": 6000, "claim_ids": ["c2"],
             "purpose": "visual_support", "transcript_excerpt": "", "frame_evidence_ids": []},
        ]
    )
    with pytest.raises(RemakeSpecError) as excinfo:
        build_package(snapshot)
    assert any("容量不足" in issue for issue in excinfo.value.issues)


def test_validator_flags_source_out_beyond_duration():
    spec = copy.deepcopy(build_package()["spec"])
    spec["sections"][0]["shots"][0]["out"] = 999.0
    report = validate_remake_spec(spec)
    assert report["valid"] is False
    assert any("超过素材时长" in issue for issue in report["issues"])


def test_validator_flags_discontinuous_and_duplicate_ids():
    spec = copy.deepcopy(build_package()["spec"])
    spec["sections"][1]["id"] = spec["sections"][0]["id"]
    spec["sections"][0]["shots"][0]["start_seconds"] = 0.5
    report = validate_remake_spec(spec)
    assert report["valid"] is False
    assert any("唯一" in issue for issue in report["issues"])
    assert any("首尾相接" in issue for issue in report["issues"])


def test_validator_flags_wrong_pipeline_type():
    spec = copy.deepcopy(build_package()["spec"])
    spec["pipeline_type"] = "avatar-secondary"
    report = validate_remake_spec(spec)
    assert report["valid"] is False
    assert any("pipeline_type" in issue for issue in report["issues"])


# --------------------------------------------------------------------------- #
# provenance 与 canonical script
# --------------------------------------------------------------------------- #
def test_provenance_is_a_sidecar_and_never_pollutes_script_section():
    package = build_package()
    provenance = package["provenance"]
    assert provenance["selected_theme"]["theme_id"] == "topic-01"
    assert provenance["keywords"]["seed"] == "折叠屏"
    assert provenance["duration_probe"]["provider"] == "stub-local-tts"
    assert provenance["duration_probe"]["sections"][0]["seconds"] == 4.0

    script = build_script(package["spec"])
    for section in script["sections"]:
        # canonical script.json section 只允许 schema 里的键，provenance 不得混入
        assert set(section.keys()) <= SCRIPT_SECTION_KEYS
        assert "provenance" not in section
        assert "material_refs" not in section
        assert "theme_support" not in section
    assert "provenance" not in json.dumps(script)


def test_provenance_records_claim_source_and_material_refs():
    decision = build_editorial_decision(editorial_snapshot())
    provenance = decision["provenance"]
    assert provenance["argument_map"][0]["dim"] == "event_core"
    assert provenance["argument_map"][0]["source_refs"] == ["s1"]
    assert provenance["argument_map"][0]["material_refs"] == [{"material_id": "m1", "segment_id": "seg1"}]
    assert provenance["sections"][0]["claim_refs"] == ["c1"]
    assert provenance["gates"]["disposition"] == "ready"
    assert provenance["selected_theme"]["gap_hook"]


# --------------------------------------------------------------------------- #
# 确定性 / 安全提示
# --------------------------------------------------------------------------- #
def test_output_is_deterministic():
    first = build_package()
    second = build_package()
    assert json.dumps(first["spec"], ensure_ascii=False, sort_keys=True) == json.dumps(
        second["spec"], ensure_ascii=False, sort_keys=True
    )
    assert json.dumps(first["provenance"], ensure_ascii=False, sort_keys=True) == json.dumps(
        second["provenance"], ensure_ascii=False, sort_keys=True
    )


def test_over_120_seconds_only_warns_and_never_compresses():
    snapshot = editorial_snapshot(
        duration_ms=300000,
        segments=[
            {"segment_id": "seg1", "start_ms": 0, "end_ms": 120000, "claim_ids": ["c1"],
             "purpose": "visual_support", "transcript_excerpt": "", "frame_evidence_ids": []},
            {"segment_id": "seg2", "start_ms": 120000, "end_ms": 240000, "claim_ids": ["c2"],
             "purpose": "visual_support", "transcript_excerpt": "", "frame_evidence_ids": []},
        ],
    )
    package = build_package(snapshot, probe=make_probe(61.0))
    spec = package["spec"]
    assert spec["duration"]["measured_seconds"] == 122.0
    assert spec["advisories"]
    assert any(str(int(MANUAL_DURATION_HINT_SECONDS)) in advisory for advisory in spec["advisories"])
    # 没有自动压缩：总时长仍来自探针
    assert spec["duration"]["total_seconds"] == 122.0


def test_stages_progress_through_frozen_flow():
    stages = build_package()["decision"]["stages"]
    for stage in (
        "validated_snapshot",
        "keywords_ready",
        "theme_candidates_ready",
        "theme_selected",
        "argument_map_ready",
        "script_ready",
        "duration_measured",
        "remake_spec_ready",
    ):
        assert stages[stage] is True, stage


def test_remake_spec_schema_matches_python_validator():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(REMAKE_SCHEMA)
    spec = build_package()["spec"]
    jsonschema.validate(spec, REMAKE_SCHEMA)
    assert validate_remake_spec(spec)["valid"] is True

    # 两侧必须同样拒绝同一处非法改动（pipeline_type）。
    broken = copy.deepcopy(spec)
    broken["pipeline_type"] = "avatar-secondary"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, REMAKE_SCHEMA)
    assert validate_remake_spec(broken)["valid"] is False


# --------------------------------------------------------------------------- #
# 端到端：权威研究包（producer 同构 fixture）→ snapshot → copywriter → spec
# --------------------------------------------------------------------------- #
def _renderable_payloads() -> dict:
    """把权威 fixture 的平台素材合法改造成「可渲染」资产（producer_owned + cleared）。

    平台内容**不得** render_eligible（权威校验强制），因此成功出镜只能靠非平台来源。
    """
    payloads = default_payloads()
    record = payloads["rights.json"]["rights"][0]
    record["origin"] = "producer_owned"
    record["rights_status"] = "cleared"
    record["render_eligible"] = True
    record["redistribution_allowed"] = True
    # 夹具已与生产端同构（``nodes`` 默认 ``[]``）⇒ 「可渲染链路」显式补上论证节点，
    # 否则编辑层会在 ``theme_selected`` 阶段以「仅 0 条可用论证」阻断。
    return with_argument_nodes(payloads, default_argument_node())


def test_integration_authoritative_pack_to_remake_spec(tmp_path):
    episode_root, _ = build_pack(tmp_path / "root", payloads=_renderable_payloads())
    snapshot = rps.load_editorial_snapshot(episode_root, now=NOW)
    assert snapshot["schema"] == SNAPSHOT_SCHEMA
    assert snapshot["disposition"] == "ready"

    # selected_topic → candidate → argument_graph 全链一致
    chosen = snapshot["selected_topic"]["topic_id"]
    assert chosen in {row["topic_id"] for row in snapshot["topic_candidates"]}
    assert snapshot["argument_graph"]["topic_id"] == chosen

    package = build_editorial_package(
        snapshot, duration_probe=stub_probe, project_id="demo-1", copywriter=stub_copywriter
    )
    spec = package["spec"]
    assert validate_remake_spec(spec)["valid"] is True
    assert spec["pipeline_type"] == "avatar-spokesperson"
    assert spec["theme"]["theme_id"] == chosen
    assert spec["sources"][0]["key"] == "m1"
    assert spec["sources"][0]["aweme_id"] == "v1"
    assert spec["sections"][0]["claim_refs"] == ["c1"]
    # provenance 记录 snapshot 身份（content hash），但不污染 canonical script section。
    assert package["provenance"]["content_sha256"] == snapshot["content_sha256"]
    assert package["decision"]["snapshot"]["content_sha256"] == snapshot["content_sha256"]
