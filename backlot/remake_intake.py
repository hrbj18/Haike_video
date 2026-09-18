"""编辑层入口：消费 intake-dev 输出的 **normalized editorial snapshot**。

分工（与 team-lead 的边界一致）：

* ``backlot.copy_skill_research_pack`` 是**跨仓权威校验**（current → READY → manifest →
  七语义文件、hash、路径安全、外键、时间码、权限门、snapshot 落盘）。
* ``backlot.research_pack_snapshot`` 把已校验的包**投影**成冻结的 editorial snapshot。
* 本模块只做两件事：① 转发读取（薄适配）；② 在 snapshot 上做编辑层**自有的语义校验**
  （dim/edge relation 白名单、外键、整数时间码、rights/material 可用性）。

本模块**不再**实现 raw producer 包的字段白名单——那会形成第二套验证权威。所有 raw
校验都在权威层完成；这里只校验冻结的 snapshot 合同。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Iterable, Mapping

from backlot.copy_skill_research_pack import (
    ALLOWED_EDGE_RELATIONS,
    DISPOSITIONS,
    EDGE_SOURCE_KEY,
    EDGE_TARGET_KEY,
    ResearchPackError,
)
from backlot.news_selection_v2 import ALLOWED_DIMENSIONS
from backlot.research_pack_snapshot import (
    SNAPSHOT_SCHEMA,
    load_editorial_snapshot as _load_editorial_snapshot,
)

#: 生产/编排入口只接受 ``ready``；``partial`` 允许但记软提示；另两者 fail closed。
DISPOSITION_READY = "ready"
DISPOSITION_PARTIAL = "partial"
DISPOSITION_RESEARCH_REQUIRED = "research_required"
DISPOSITION_REJECTED = "rejected"

#: 编辑层视为「素材可渲染」的 rights 状态。
RENDER_BLOCKING_RIGHTS_STATUSES = frozenset({"prohibited", "restricted", "unknown"})


class EditorialSnapshotError(ResearchPackError):
    """normalized editorial snapshot 不满足编辑层合同（fail closed）。"""

    def __init__(self, issues: Iterable[str]):
        self.issues = list(dict.fromkeys(str(issue) for issue in issues if str(issue).strip()))
        super().__init__("；".join(self.issues) or "editorial snapshot 校验失败", code="invalid_editorial_snapshot")


def load_editorial_snapshot(source: str | Path, *, now: object = None) -> dict[str, Any]:
    """薄转发：严格校验 + 投影一期研究包（source 可为 episode 根或 current.json）。

    技术无效/不稳定时抛 :class:`ResearchPackError` 子类；绝不返回半可信快照。
    """
    return _load_editorial_snapshot(source, now=now)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def validate_editorial_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """校验并返回 editorial snapshot 的深拷贝；不合规抛 :class:`EditorialSnapshotError`。

    只校验**冻结合同**与编辑层自有语义，不重复权威层的 raw 校验，也不写盘。
    """
    issues: list[str] = []
    #: 非阻断的一致性提醒。冻结合同没有要求的「自洽」类判断都放这里，**不判输入不合格**，
    #: 由编辑决策把它们透传进 provenance，让操作者看得见生产端的不一致。
    advisories: list[str] = []
    if not isinstance(snapshot, Mapping):
        raise EditorialSnapshotError(["editorial snapshot 必须是对象"])

    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        issues.append(f"schema 必须是 {SNAPSHOT_SCHEMA}，实际 {snapshot.get('schema')!r}")

    disposition = _clean(snapshot.get("disposition"))
    if disposition not in DISPOSITIONS:
        issues.append(f"disposition 取值非法：{disposition or '[空]'}")

    if not _clean(snapshot.get("episode_id")):
        issues.append("缺少 episode_id")
    if not _clean(snapshot.get("theme")):
        issues.append("缺少 theme")
    if not _clean(snapshot.get("content_sha256")):
        issues.append("缺少 content_sha256")

    keywords = snapshot.get("keywords")
    if not isinstance(keywords, Mapping):
        issues.append("keywords 必须是对象（keyword_graph）")

    topic_candidates = snapshot.get("topic_candidates")
    if not isinstance(topic_candidates, list):
        issues.append("topic_candidates 必须是数组")
        topic_candidates = []
    candidate_ids = [str((item or {}).get("topic_id") or "") for item in topic_candidates if isinstance(item, Mapping)]

    # selected_topic 的「必须存在」同样是**条件**要求。冻结合同
    # （episode-research-pack-v1）明文写作 ``selected_topic{...}|null``，权威校验层
    # （``copy_skill_research_pack``）也显式接受 null。⇒ null 属**合同合法输入**，
    # 本层不得据此判输入不合格（exit 2）；「编辑层需要明确的选题提案」这件事
    # 由编辑门表达（``remake_editorial.build_editorial_decision`` 在 gate 阶段阻断，exit 3）。
    # 本层只守住取值类型（对象或 null）与「给了就必须有 topic_id」。
    # ★ 这两种「不可用」是**有意区分**的，不要顺手统一：
    #   · ``selected_topic: null`` —— 合同明文允许的空 ⇒ 属**编辑层需求**，intake 放行，
    #     由编辑门以 **exit 3** 承接；
    #   · ``selected_topic`` 是对象但缺 ``topic_id`` —— 违反合同声明的形状 ⇒ **exit 2**。
    # 判据是「该约束是否会拒绝默认适配器实际产出的包」：默认适配器恒写
    # ``{topic_id, selection_basis, producer_proposal}`` ⇒ 前者拦不住任何真实包（故不能判 2），
    # 后者能（故该判 2）。两条路径各有测试钉住：
    # ``test_selected_topic_none_reaches_the_editorial_gate`` /
    # ``test_selected_topic_without_topic_id_is_rejected``。
    # ★「topic_id 必须命中 topic_candidates」**不是**合同要求（合同第 80 行只给形状，还特意
    # 用 ``producer_proposal`` 标注它只是**提案**；权威层与生产端 `_validate_topics` 都不查自洽）
    # ⇒ 判 exit 2 就会让生产端自校验通过的包在本仓被判「输入不合格」。降级为非阻断提醒。
    selected = snapshot.get("selected_topic")
    selected_id = ""
    if isinstance(selected, Mapping):
        selected_id = _clean(selected.get("topic_id"))
        if not selected_id:
            issues.append("selected_topic 缺少 topic_id")
        elif candidate_ids and selected_id not in candidate_ids:
            advisories.append(
                f"selected_topic.topic_id={selected_id} 不在 topic_candidates 内"
                "（合同未要求自洽，仅记录）"
            )
    elif selected is not None:
        issues.append("selected_topic 必须是对象或 null")

    argument_graph = snapshot.get("argument_graph")
    if not isinstance(argument_graph, Mapping):
        issues.append("缺少 argument_graph")
        argument_graph = {}
    graph_topic = _clean(argument_graph.get("topic_id"))
    if not graph_topic:
        issues.append("argument_graph 缺少 topic_id")
    elif selected_id and graph_topic != selected_id:
        # 合同第 81 行只声明 ``argument_graph{topic_id, ...}``，**没有**要求它与
        # ``selected_topic.topic_id`` 相等（生产端两侧都硬编码 ``topic-01`` 纯属巧合，
        # 其自校验也不查这条）⇒ 同上，降级为非阻断提醒。
        advisories.append(
            f"argument_graph.topic_id 与 selected_topic.topic_id 不一致："
            f"{graph_topic} != {selected_id}（合同未要求一致，仅记录）"
        )

    claims = snapshot.get("claims")
    if not isinstance(claims, list):
        issues.append("claims 必须是数组")
        claims = []
    # ★ 已知未守项（**故意完全不守**）：本层对 ``claim.topic_id`` **零代码** —— 既不要求存在，
    # 也不校验取值，更不要求它与 ``selected_topic.topic_id`` 一致。理由是它**对编辑层不承重**：
    # 论证门只读 ``argument_graph.nodes``（见 ``remake_editorial._argument_nodes``），
    # 而生产端默认适配器（``episode_research_pack.build_research_semantic``）产出的 claim
    # **根本不写这个键**（见该文件 claim 构造处）⇒ 任何以它为条件的守卫都会拒绝真实包。
    # 约束的去向：**无**。
    # （说明：此处注释曾经自称「只在该键存在时校验其类型（非空字符串）」，但实现里从来没有
    # 这段代码 —— 「声明做了、实际没做」正是上一轮 ``argument_graph`` 那族问题能长期潜伏的
    # 成因之一，故按实现重写，不留任何言过其实的表述。）
    claim_ids: list[str] = []
    for index, claim in enumerate(claims, 1):
        if not isinstance(claim, Mapping):
            issues.append(f"claims[{index}] 不是对象")
            continue
        claim_id = _clean(claim.get("claim_id"))
        if not claim_id:
            issues.append(f"claims[{index}] 缺少 claim_id")
            continue
        claim_ids.append(claim_id)
        if not _clean(claim.get("text")):
            issues.append(f"claim {claim_id} 缺少 text")
    if len(claim_ids) != len(set(claim_ids)):
        issues.append("claims 的 claim_id 必须唯一")

    # argument_graph 只守结构与闭集：nodes/edges 必须是数组、节点 dim 合法、边键集与关系合法。
    # **不判 nodes / claims 的「空」**——冻结合同从未要求它们非空，而生产端 `topics.json` 是
    # baseline 原样透传 ⇒ 真实包的 `argument_graph` 恒为 ``{"topic_id": ..., "nodes": [],
    # "edges": []}``，claims 也可能为空（``_derive_disposition`` 在「有 sources 无 claims」时
    # 自动得出 ``partial``）。把这些判成输入不合格（exit 2）会让整条跨仓链路对真实包不可达。
    # 「没有可裁决的论证」是**编辑层自身**的门：``build_editorial_decision`` 在
    # ``theme_selected`` 阶段阻断（exit 3），那才是诚实的表达位置。
    nodes = argument_graph.get("nodes")
    if not isinstance(nodes, list):
        issues.append("argument_graph.nodes 必须是数组")
        nodes = []
    node_ids: list[str] = []
    for index, node in enumerate(nodes, 1):
        if not isinstance(node, Mapping):
            issues.append(f"argument_graph.nodes[{index}] 不是对象")
            continue
        claim_id = _clean(node.get("claim_id"))
        if not claim_id:
            issues.append(f"argument_graph.nodes[{index}] 缺少 claim_id")
            continue
        node_ids.append(claim_id)
        dim = _clean(node.get("dim"))
        if dim not in ALLOWED_DIMENSIONS:
            issues.append(f"argument_graph 节点 {claim_id} 使用了非法信息维度：{dim or '[空]'}")
        if not _clean(node.get("claim")):
            issues.append(f"argument_graph 节点 {claim_id} 缺少 claim 文本")
    # ★「node.claim_id 必须命中 claims」在**本层撤掉**：合同第 81 行只声明节点形状，
    # 未要求节点必须引用已存在的 claim（权威层同款不查）。等价语义已由编辑层承接 ——
    # ``remake_editorial._argument_nodes`` 对悬空节点报
    # 「论证节点 X 引用了不存在的 claim」并在 ``theme_selected`` 阶段阻断（exit 3）。
    # 悬空节点是**编辑语义问题**，不是「输入不合格」。
    if len(node_ids) != len(set(node_ids)):
        issues.append("argument_graph.nodes 的 claim_id 必须唯一")

    edges = argument_graph.get("edges")
    if not isinstance(edges, list):
        issues.append("argument_graph.edges 必须是数组")
        edges = []
    # ★ 边结构里本层只守两条：``relation`` 属闭集、边必须引用已知节点（下面三个 if）。
    # 合同第 170-171 行另外四条 —— 边键集恰为 ``{from, to, relation}``、禁自指、禁重复、
    # ``causes``/``precedes`` 子图无环 —— 本层**不守**，因为**权威层**
    # ``copy_skill_research_pack._validate_topics`` 已完整守（键集 :539、自指 :556、
    # 重复三元组 :558、环 :562 + ``_has_cycle`` :484），且权威层在同进程内是**前置层**，
    # 拦得比这里早。**不要在本层补那四条**：这份跨仓合同今天所有的坑（``edges`` 键集曾改名后
    # 测试全绿潜伏、``heat_only`` 口径两端分叉、``publisher`` 语义消费端零实现）都源于
    # 「同一条规则的多份独立实现」。再复制第三份只会提高下一次分叉的概率 —— 分叉时没有测试会红。
    known = set(node_ids)
    for index, edge in enumerate(edges, 1):
        if not isinstance(edge, Mapping):
            issues.append(f"argument_graph.edges[{index}] 不是对象")
            continue
        source = _clean(edge.get(EDGE_SOURCE_KEY))
        target = _clean(edge.get(EDGE_TARGET_KEY))
        relation = _clean(edge.get("relation"))
        if relation not in ALLOWED_EDGE_RELATIONS:
            issues.append(f"argument_graph.edges[{index}] 使用了非法关系：{relation or '[空]'}")
        if source not in known:
            issues.append(
                f"argument_graph.edges[{index}].{EDGE_SOURCE_KEY} 未引用已知节点：{source or '[空]'}"
            )
        if target not in known:
            issues.append(
                f"argument_graph.edges[{index}].{EDGE_TARGET_KEY} 未引用已知节点：{target or '[空]'}"
            )

    rights_by_material: dict[str, Mapping[str, Any]] = {}
    rights = snapshot.get("rights")
    if not isinstance(rights, list):
        issues.append("rights 必须是数组")
        rights = []
    for index, record in enumerate(rights, 1):
        if not isinstance(record, Mapping):
            issues.append(f"rights[{index}] 不是对象")
            continue
        if not isinstance(record.get("render_eligible"), bool):
            issues.append(f"rights[{index}].render_eligible 必须是布尔值")
        # 冻结合同写作 ``rights[{..., material_id?}]`` ⇒ material_id 是**可选**键
        # （资产级权利记录本来就不挂 material）。权威校验层同款写法：只在存在时校验引用。
        material_id = _clean(record.get("material_id"))
        if material_id:
            if material_id in rights_by_material:
                issues.append(f"rights 记录重复：material_id {material_id}")
            rights_by_material[material_id] = record

    materials = snapshot.get("materials")
    if not isinstance(materials, list):
        issues.append("materials 必须是数组")
        materials = []
    material_ids: list[str] = []
    segment_pairs: set[tuple[str, str]] = set()
    for index, material in enumerate(materials, 1):
        if not isinstance(material, Mapping):
            issues.append(f"materials[{index}] 不是对象")
            continue
        material_id = _clean(material.get("material_id"))
        if not material_id:
            issues.append(f"materials[{index}] 缺少 material_id")
            continue
        material_ids.append(material_id)
        duration_ms = _as_int(material.get("duration_ms"))
        if duration_ms is None or duration_ms < 0:
            issues.append(f"material {material_id} duration_ms 必须是非负整数")
            duration_ms = duration_ms or 0
        segments = material.get("segments")
        if not isinstance(segments, list):
            issues.append(f"material {material_id} segments 必须是数组")
            continue
        for seg_index, segment in enumerate(segments, 1):
            if not isinstance(segment, Mapping):
                issues.append(f"material {material_id} segments[{seg_index}] 不是对象")
                continue
            segment_id = _clean(segment.get("segment_id"))
            if not segment_id:
                issues.append(f"material {material_id} segments[{seg_index}] 缺少 segment_id")
                continue
            pair = (material_id, segment_id)
            if pair in segment_pairs:
                issues.append(f"(material_id, segment_id) 重复：{pair}")
            segment_pairs.add(pair)
            start_ms = _as_int(segment.get("start_ms"))
            end_ms = _as_int(segment.get("end_ms"))
            if start_ms is None or end_ms is None:
                issues.append(f"片段 {pair} 时间码必须是整数毫秒")
            elif not (0 <= start_ms < end_ms <= duration_ms):
                issues.append(f"片段 {pair} 时间码越界：0 <= {start_ms} < {end_ms} <= {duration_ms}")
    if len(material_ids) != len(set(material_ids)):
        issues.append("materials 的 material_id 必须唯一")
    for material_id in material_ids:
        if material_id not in rights_by_material:
            issues.append(f"material {material_id} 缺少 rights 记录")

    if issues:
        raise EditorialSnapshotError(issues)
    validated = copy.deepcopy(dict(snapshot))
    # 非阻断提醒随 snapshot 一起带给编辑层（``build_editorial_decision`` 会把它并入
    # ``advisories``，最终落进 provenance sidecar）。合同不要求的一致性只在这里留证。
    validated["consistency_advisories"] = list(advisories)
    return validated


__all__ = [
    "DISPOSITION_PARTIAL",
    "DISPOSITION_READY",
    "DISPOSITION_REJECTED",
    "DISPOSITION_RESEARCH_REQUIRED",
    "EditorialSnapshotError",
    "RENDER_BLOCKING_RIGHTS_STATUSES",
    "ResearchPackError",
    "SNAPSHOT_SCHEMA",
    "load_editorial_snapshot",
    "validate_editorial_snapshot",
]
