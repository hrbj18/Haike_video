"""编辑层入口（normalized editorial snapshot）测试。

输入是 intake 权威层（``copy_skill_research_pack`` 严格校验 + ``research_pack_snapshot``
投影）产出的**冻结** snapshot。本层只做两件事：① 薄转发读取；② 在 snapshot 上做编辑层
自有的语义校验（``ALLOWED_DIMENSIONS`` / ``ALLOWED_EDGE_RELATIONS`` 白名单、外键、整数时间码、
rights/material 可用性）。**不重复**权威层的 raw 包校验，不形成第二套验证权威。

全部纯本地：不联网、不建项目、不调真实 TTS/LLM。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from backlot import research_pack_snapshot as rps
from backlot.copy_skill_research_pack import (
    ALLOWED_EDGE_RELATIONS,
    CONTRACT,
    ResearchPackError,
)
from backlot.news_selection_v2 import ALLOWED_DIMENSIONS
from backlot.remake_editorial import EditorialBlocked, build_editorial_decision
from backlot.remake_intake import (
    DISPOSITION_PARTIAL,
    DISPOSITION_READY,
    EditorialSnapshotError,
    SNAPSHOT_SCHEMA,
    load_editorial_snapshot,
    validate_editorial_snapshot,
)
from tests.backlot.test_copy_skill_research_pack import build_pack, default_argument_node

NOW = "2026-09-16T23:00:00+08:00"


@pytest.fixture
def snapshot(tmp_path: Path) -> dict:
    """从权威 fixture（producer 同构包）投影出的 frozen editorial snapshot。"""
    episode_root, _ = build_pack(tmp_path / "root")
    return rps.load_editorial_snapshot(episode_root, now=NOW)


def _mutated(snapshot: dict) -> dict:
    return copy.deepcopy(snapshot)


# --------------------------------------------------------------------------- #
# 薄转发：读取权威层投影出的 snapshot
# --------------------------------------------------------------------------- #
def test_forwards_snapshot_from_episode_root(tmp_path: Path):
    episode_root, _ = build_pack(tmp_path / "root")
    snap = load_editorial_snapshot(episode_root, now=NOW)
    assert snap["schema"] == SNAPSHOT_SCHEMA
    assert snap["disposition"] == DISPOSITION_READY
    assert snap["episode_id"] == "2026-09-16-测试主题"
    assert snap["selected_topic"]["topic_id"] == "topic-01"


def test_forwards_snapshot_from_current_json(tmp_path: Path):
    episode_root, _ = build_pack(tmp_path / "root")
    snap = load_editorial_snapshot(episode_root / "current.json", now=NOW)
    assert snap["schema"] == SNAPSHOT_SCHEMA
    assert snap["revision"] == 1


def test_missing_source_raises_research_pack_error(tmp_path: Path):
    with pytest.raises(ResearchPackError):
        load_editorial_snapshot(tmp_path / "does-not-exist", now=NOW)


def test_editorial_snapshot_error_is_a_research_pack_error():
    assert issubclass(EditorialSnapshotError, ResearchPackError)
    with pytest.raises(ResearchPackError):
        validate_editorial_snapshot(["not", "a", "mapping"])


# --------------------------------------------------------------------------- #
# 编辑层自有语义校验
# --------------------------------------------------------------------------- #
def test_valid_snapshot_passes_and_is_deep_copied(snapshot: dict):
    result = validate_editorial_snapshot(snapshot)
    # 校验层会在返回体上追加派生键 ``consistency_advisories``（非阻断提醒的载体），
    # ⇒ 不能再拿 ``result == snapshot`` 当「原样转发」的判据。
    assert {k: v for k, v in result.items() if k != "consistency_advisories"} == snapshot
    assert result["consistency_advisories"] == []
    assert result is not snapshot
    result["selected_topic"]["topic_id"] = "mutated"
    assert snapshot["selected_topic"]["topic_id"] == "topic-01"  # 输入不被污染


def test_wrong_snapshot_schema_is_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    broken["schema"] = "openmontage-editorial-snapshot-v0"
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("schema" in issue for issue in excinfo.value.issues)


def test_unknown_disposition_is_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    broken["disposition"] = "maybe"
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("disposition" in issue for issue in excinfo.value.issues)


def test_missing_theme_is_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    broken["theme"] = "  "
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("theme" in issue for issue in excinfo.value.issues)


def test_selected_topic_outside_candidates_is_only_recorded(snapshot: dict):
    """合同未要求 selected_topic 自洽 ⇒ 约束去向是**非阻断提醒**，不是输入不合格。"""
    broken = _mutated(snapshot)
    broken["selected_topic"]["topic_id"] = "topic-99"
    result = validate_editorial_snapshot(broken)  # 不抛异常
    assert any("不在 topic_candidates 内" in a for a in result["consistency_advisories"])


def test_selected_topic_none_reaches_the_editorial_gate(snapshot: dict):
    """``selected_topic: null`` 是合同明文允许的值 ⇒ intake **不得**判输入不合格。

    它与「对象但缺 topic_id」是**两种不同的输入错误**，退出码有意区分：
    这里（合同允许的空 ⇒ 编辑层需求）走 ``exit 3``；那边（形状不合声明）走 ``exit 2``。
    本用例钉住前者，防止有人「顺手统一」成 intake 直接拒。
    """
    broken = _mutated(snapshot)
    broken["selected_topic"] = None
    validated = validate_editorial_snapshot(broken)  # 不抛异常
    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(validated)
    assert excinfo.value.stage == "gate"
    assert any("selected_topic 为空" in reason for reason in excinfo.value.reasons)


def test_selected_topic_without_topic_id_is_rejected(snapshot: dict):
    """对象形状不合合同声明 ⇒ 这才是输入不合格（``exit 2``）。

    默认适配器恒写 ``{topic_id, selection_basis, producer_proposal}`` ⇒ 这条约束拒绝不了
    任何真实包（合法），故保留；与上面的 ``null`` 有意区分，不可合并。
    """
    broken = _mutated(snapshot)
    broken["selected_topic"] = {}
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("selected_topic 缺少 topic_id" in issue for issue in excinfo.value.issues)


def test_argument_graph_topic_mismatch_is_only_recorded(snapshot: dict):
    """合同第 81 行只声明 argument_graph{topic_id,...}，未要求与提案相等。"""
    broken = _mutated(snapshot)
    broken["argument_graph"]["topic_id"] = "topic-99"
    result = validate_editorial_snapshot(broken)  # 不抛异常
    assert any("不一致" in a for a in result["consistency_advisories"])


def test_unknown_dimension_is_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    broken["argument_graph"]["nodes"] = [default_argument_node(dim="事实")]  # 夹具已同构 ⇒ 显式给节点
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("非法信息维度" in issue for issue in excinfo.value.issues)


def test_unknown_edge_relation_is_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    broken["argument_graph"]["nodes"].append(
        {"claim_id": "c2", "dim": "mechanism", "claim": "节点 c2", "source_candidate_ids": []}
    )
    broken["argument_graph"]["edges"] = [{"from": "c1", "to": "c2", "relation": "implies"}]
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("非法关系" in issue for issue in excinfo.value.issues)


def test_edge_must_reference_known_nodes(snapshot: dict):
    broken = _mutated(snapshot)
    broken["argument_graph"]["edges"] = [{"from": "c1", "to": "c404", "relation": "supports"}]
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("未引用已知节点" in issue for issue in excinfo.value.issues)


def test_dangling_node_is_blocked_by_the_editorial_layer(snapshot: dict):
    """悬空节点是**编辑语义**问题（合同未要求节点命中 claims），由编辑层阻断。"""
    broken = _mutated(snapshot)
    broken["argument_graph"]["nodes"] = [
        {"claim_id": "c404", "dim": "mechanism", "claim": "幽灵节点", "source_candidate_ids": []}
    ]
    assert validate_editorial_snapshot(broken)  # intake 放行
    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(validate_editorial_snapshot(broken))
    assert excinfo.value.stage == "theme_selected"
    assert any("引用了不存在的 claim" in reason for reason in excinfo.value.reasons)


def test_duplicate_node_claim_ids_are_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    node = default_argument_node()
    broken["argument_graph"]["nodes"] = [dict(node), dict(node)]
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("claim_id 必须唯一" in issue for issue in excinfo.value.issues)


def test_non_integer_timecode_is_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    broken["materials"][0]["segments"][0]["start_ms"] = 1.5
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("整数毫秒" in issue for issue in excinfo.value.issues)


def test_timecode_out_of_bounds_is_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    broken["materials"][0]["segments"][0]["end_ms"] = 999999
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("越界" in issue for issue in excinfo.value.issues)


def test_duplicate_material_id_is_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    broken["materials"].append(copy.deepcopy(broken["materials"][0]))
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("material_id 必须唯一" in issue for issue in excinfo.value.issues)


def test_duplicate_segment_pair_is_rejected(snapshot: dict):
    broken = _mutated(snapshot)
    broken["materials"][0]["segments"].append(copy.deepcopy(broken["materials"][0]["segments"][0]))
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("重复" in issue for issue in excinfo.value.issues)


def test_material_requires_rights_record(snapshot: dict):
    broken = _mutated(snapshot)
    broken["rights"] = []
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("缺少 rights 记录" in issue for issue in excinfo.value.issues)


def test_render_eligible_must_be_boolean(snapshot: dict):
    broken = _mutated(snapshot)
    broken["rights"][0]["render_eligible"] = "yes"
    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("render_eligible 必须是布尔值" in issue for issue in excinfo.value.issues)


def test_all_frozen_dimensions_and_relations_are_accepted(snapshot: dict):
    """编辑层不得收窄权威合同的 13 维 / 5 关系。"""
    from backlot.copy_skill_research_pack import ALLOWED_DIMENSIONS as FROZEN_DIMENSIONS

    # 编辑层白名单取自 news_selection_v2；必须与冻结合同同集合。
    assert set(ALLOWED_DIMENSIONS) == set(FROZEN_DIMENSIONS)
    assert len(ALLOWED_DIMENSIONS) == 13
    assert len(ALLOWED_EDGE_RELATIONS) == 5

    broken = _mutated(snapshot)
    dims = sorted(ALLOWED_DIMENSIONS)
    claims = []
    nodes = []
    for index, dim in enumerate(dims, 1):
        claim_id = f"c{index}"
        claims.append(
            {
                "claim_id": claim_id,
                "evidence_status": "confirmed_official",
                "fact_ready": True,
                "text": f"主张 {index}",
                "material_refs": [],
                "source_ids": [],
            }
        )
        nodes.append(
            {"claim_id": claim_id, "dim": dim, "claim": f"节点 {index}", "source_candidate_ids": []}
        )
    edges = [
        {"from": f"c{index}", "to": f"c{index + 1}", "relation": relation}
        for index, relation in enumerate(ALLOWED_EDGE_RELATIONS, 1)
    ]
    broken["claims"] = claims
    broken["argument_graph"]["nodes"] = nodes
    broken["argument_graph"]["edges"] = edges

    result = validate_editorial_snapshot(broken)
    assert {node["dim"] for node in result["argument_graph"]["nodes"]} == set(dims)
    assert result["argument_graph"]["edges"] == edges
    assert result["contract"] == CONTRACT


def test_partial_disposition_is_accepted_by_editorial_contract(tmp_path: Path):
    """编辑层只校验 snapshot 合同：partial 由决定层转成 advisory，而非在这里被拒。"""
    episode_root, _ = build_pack(tmp_path / "root", payloads=_partial_payloads())
    snap = rps.load_editorial_snapshot(episode_root, now=NOW)
    assert snap["disposition"] == DISPOSITION_PARTIAL
    validated = validate_editorial_snapshot(snap)
    assert validated["disposition"] == DISPOSITION_PARTIAL


def _partial_payloads() -> dict:
    from tests.backlot.test_copy_skill_research_pack import default_payloads

    payloads = default_payloads(disposition="partial")
    payloads["claims.json"]["claims"][0].update(
        {"evidence_status": "unverified", "source_ids": ["s1"], "fact_sources_present": 1, "fact_sources_min": 0}
    )
    return payloads


# --------------------------------------------------------------------------- #
# argument_graph.nodes 的「非空」是条件要求，不是无条件要求
#
# 跨仓合同（episode-research-pack-v1）只冻结 dim 闭集 / relation 闭集 / edge 键集 /
# 无自指 / 无重复三元组 / causes·precedes 无环，**从未要求 nodes 非空**。生产端在
# research_required 缺口期本就产出 ``"argument_graph": {"topic_id": ..., "nodes": [],
# "edges": []}``（topics.json 原样透传 baseline），生产端自校验也只要求 nodes 是 list。
# 消费端曾把它写成无条件非空，导致任何真实研究包都在 validated_snapshot 被拒、整条
# 跨仓链路对真实包不可达。
# --------------------------------------------------------------------------- #
def _research_gap_payloads() -> dict:
    """与真实研究包等价的 semantic：claims 空、nodes/edges 空、research_required。"""
    from tests.backlot.test_copy_skill_research_pack import default_payloads

    payloads = default_payloads(disposition="research_required")
    payloads["claims.json"]["claims"] = []
    payloads["topics.json"]["argument_graph"]["nodes"] = []
    payloads["topics.json"]["argument_graph"]["edges"] = []
    payloads["materials.json"]["materials"][0]["segments"][0]["claim_ids"] = []
    return payloads


def test_research_gap_snapshot_is_accepted(snapshot: dict):
    """claims 空 + research_required + nodes 空 ⇒ 研究缺口期的正常形态，不得报 issue。"""
    gap = _mutated(snapshot)
    gap["disposition"] = "research_required"
    gap["claims"] = []
    gap["argument_graph"]["nodes"] = []
    gap["argument_graph"]["edges"] = []

    result = validate_editorial_snapshot(gap)  # 不抛异常
    assert result["disposition"] == "research_required"
    assert result["claims"] == []
    assert result["argument_graph"]["nodes"] == []


def test_empty_nodes_with_claims_is_accepted_and_blocked_by_the_editorial_gate(snapshot: dict):
    """有断言却无论证节点：生产端 `topics.json` 原样透传 ⇒ nodes 恒空，本层不得判输入不合格。

    「没有可裁决的论证」由编辑层在 `theme_selected` 阶段阻断（exit 3）表达，不是坏输入。
    """
    empty_nodes = _mutated(snapshot)
    empty_nodes["argument_graph"]["nodes"] = []

    validated = validate_editorial_snapshot(empty_nodes)  # 不抛异常
    assert validated["argument_graph"]["nodes"] == []
    assert validated["claims"]  # 但确实有断言

    with pytest.raises(EditorialBlocked) as excinfo:
        build_editorial_decision(empty_nodes)
    assert excinfo.value.stage == "theme_selected"


def test_empty_claims_and_nodes_is_accepted_regardless_of_disposition(snapshot: dict):
    """生产端 `_derive_disposition` 在「有 sources 无 claims」时自动得出 partial ⇒ 必须放行。"""
    empty = _mutated(snapshot)
    empty["disposition"] = DISPOSITION_PARTIAL
    empty["claims"] = []
    empty["argument_graph"]["nodes"] = []
    empty["argument_graph"]["edges"] = []

    validated = validate_editorial_snapshot(empty)  # 不抛异常
    assert validated["claims"] == []


def test_real_research_gap_pack_passes_intake(tmp_path: Path):
    """真实包回归：r1 这类包现在能通过 intake 校验，不再卡在 validated_snapshot。"""
    episode_root, _ = build_pack(tmp_path / "root", payloads=_research_gap_payloads())
    snap = rps.load_editorial_snapshot(episode_root, now=NOW)
    assert snap["disposition"] == "research_required"
    assert snap["claims"] == []
    assert snap["argument_graph"]["nodes"] == []

    validated = validate_editorial_snapshot(snap)
    assert validated["disposition"] == "research_required"


def test_non_list_nodes_is_rejected(snapshot: dict):
    """nodes 不是数组时仍照旧报 issue（守卫不得被放宽成「任意值都行」）。"""
    broken = _mutated(snapshot)
    broken["argument_graph"]["nodes"] = None

    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("argument_graph.nodes" in issue for issue in excinfo.value.issues)


# --------------------------------------------------------------------------- #
# selected_topic 的「必须存在」也是条件要求（合同明文 ``|null``）
#
# 冻结合同第 80 行写作 ``selected_topic{topic_id,selection_basis,producer_proposal}|null``；
# 权威校验层（``copy_skill_research_pack``）也显式接受 null（「必须是对象或 null」）。
# ⇒ null 是**合同合法输入**，本层不得据此判输入不合格（exit 2）；
# 「编辑层需要明确的选题提案」由编辑门表达（gate 阶段阻断，exit 3）。
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "disposition", [DISPOSITION_READY, DISPOSITION_PARTIAL, "research_required", "rejected"]
)
def test_null_selected_topic_is_accepted_for_every_disposition(snapshot: dict, disposition: str):
    gap = _mutated(snapshot)
    gap["disposition"] = disposition
    if disposition == "research_required":
        gap["claims"] = []
        gap["argument_graph"]["nodes"] = []
        gap["argument_graph"]["edges"] = []
    gap["selected_topic"] = None

    result = validate_editorial_snapshot(gap)  # 不抛异常：合同允许 null
    assert result["selected_topic"] is None


def test_absent_selected_topic_key_is_accepted(snapshot: dict):
    """键缺失与显式 null 等价（投影层 ``topics.get(...)`` 也会得到 None）。"""
    gap = _mutated(snapshot)
    gap.pop("selected_topic", None)

    assert validate_editorial_snapshot(gap).get("selected_topic") is None


def test_non_mapping_selected_topic_is_rejected(snapshot: dict):
    """对象或 null 之外的取值仍是合同违规（守卫不得被放宽成「任意值都行」）。"""
    broken = _mutated(snapshot)
    broken["selected_topic"] = "topic-01"

    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("必须是对象或 null" in issue for issue in excinfo.value.issues)


def test_asset_level_rights_without_material_id_is_allowed(snapshot: dict):
    """合同写作 ``material_id?`` ⇒ 资产级权利记录（不挂 material）合法，不得报 issue。"""
    broken = _mutated(snapshot)
    asset_level = dict(broken["rights"][0])
    asset_level.pop("material_id", None)
    asset_level["asset_id"] = "asset-episode-level"
    broken["rights"].append(asset_level)

    result = validate_editorial_snapshot(broken)  # 不抛异常
    assert any("material_id" not in record for record in result["rights"])
    assert len(result["rights"]) == 2


def test_material_without_rights_record_is_still_rejected(snapshot: dict):
    """放宽 material_id 可选后，「material 无对应 rights」这条不变量必须仍然生效。"""
    broken = _mutated(snapshot)
    for record in broken["rights"]:
        record.pop("material_id", None)

    with pytest.raises(EditorialSnapshotError) as excinfo:
        validate_editorial_snapshot(broken)
    assert any("缺少 rights 记录" in issue for issue in excinfo.value.issues)
