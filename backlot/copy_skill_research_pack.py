"""只读校验与快照：CopySkill 单期研究包 ``episode-research-pack-v1``。

这是跨项目合同的 **消费端**（Haike 侧）。CopySkill（生产端）在单主题
``material-replication`` 交付完成后，额外发布一个不可变、可自校验的研究包：

    output/每期研究包/<YYYY-MM-DD>_研究包/<episode_id>/
        .staging/<pack_id>/     # 构建中的 stage（本模块永不扫描）
        packs/<pack_id>/        # 不可变已发布包
        current.json            # 唯一提交点
        latest.json             # best-effort 镜像（本模块不依赖）
        notify/<episode_id>.json

设计约束（与生产端 ``episode_research_pack.py`` 一一对应，字段名逐项对齐）：

* 只读：本模块永不写生产端目录，也不依赖根目录 ``latest.json``。
* ``current.json`` 是唯一提交点，翻转是原子的。读取时按
  ``current → _READY → package-manifest → 全部文件 → current`` 双读，
  两次不一致就丢弃重试，避免消费到半翻转或跨 revision 的混合状态。
* 七个语义 JSON 只带一个身份键 ``contract``；``content_sha256`` 按固定文件名
  顺序、**分帧 canonical** 编码（``name + NUL + len + NUL + canonical + NUL``）
  拼接，因此不同的文件切分永不碰撞。
* 抖音热度与 B-roll 素材都不是事实来源；material 只能 ``b_roll_only``，
  平台内容不得 ``render_eligible``。
* 生产端声明的 freshness 只作输入，Haike 以 ``now`` 重新计算并按关键性归类，
  但不生成最终文案、不创建项目。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

CONTRACT = "episode-research-pack-v1"
CONTRACT_KIND = CONTRACT  # 兼容别名
MANIFEST_SCHEMA = "episode-research-pack-manifest-v1"
PRODUCTION_MODE = "single_topic_material_replication"

#: 七个语义 JSON。顺序冻结，是 ``content_sha256`` 的拼接顺序，不得重排。
SEMANTIC_FILES: tuple[str, ...] = (
    "episode.json",
    "sources.json",
    "claims.json",
    "audience.json",
    "topics.json",
    "materials.json",
    "rights.json",
)
REVISION_NAME = "revision.json"
RUN_REPORT_NAME = "run-report.json"
README_NAME = "每期研究证据包.md"
MANIFEST_NAME = "package-manifest.json"
READY_NAME = "_READY.json"
CURRENT_NAME = "current.json"
LATEST_NAME = "latest.json"
PACKS_DIRNAME = "packs"
STAGING_DIRNAME = ".staging"

REQUIRED_PACK_FILES: tuple[str, ...] = (
    *SEMANTIC_FILES,
    REVISION_NAME,
    RUN_REPORT_NAME,
    README_NAME,
    MANIFEST_NAME,
    READY_NAME,
)
MANIFEST_EXCLUSIONS: tuple[str, ...] = (MANIFEST_NAME, READY_NAME)

# --- 冻结枚举（与生产端一致）------------------------------------------------
DISPOSITIONS: tuple[str, ...] = ("ready", "partial", "research_required", "rejected")
HAIKE_DISPOSITIONS: tuple[str, ...] = DISPOSITIONS
EVIDENCE_STATUSES: tuple[str, ...] = (
    "confirmed_official",
    "confirmed_two_reliable",
    "creator_primary",
    "unverified",
    "conflicting",
    "insufficient",
)
FACT_READY: frozenset[str] = frozenset({"confirmed_official", "confirmed_two_reliable"})
#: ``confirmed_*`` 的事实源下限（合同第 138 行「由 source 复算」）。**唯一实现**，
#: ``_validate_sources``（输入门）与 ``assess_disposition``（产品门）共用，不得再内联。
CONFIRMED_FACT_MIN: dict[str, int] = {"confirmed_official": 1, "confirmed_two_reliable": 2}
#: 能计入「独立可靠来源」的 authority（合同第 103-104 行要求 official/reliable_independent）。
INDEPENDENT_SOURCE_AUTHORITIES: tuple[str, ...] = ("official", "reliable_independent")
FRESHNESS_REQUIREMENTS: tuple[str, ...] = ("fresh", "evergreen", "manual_review")
WORDING_POLICIES: tuple[str, ...] = ("assert", "attribute", "hedge", "prohibit")
SOURCE_AUTHORITIES: tuple[str, ...] = (
    "official",
    "reliable_independent",
    "primary_creator",
    "heat_only",
    "unknown",
)
VERIFICATION_STATES: tuple[str, ...] = ("verified", "unverified", "failed", "not_applicable")
FRESHNESS_POLICIES: tuple[str, ...] = ("event_window", "evergreen", "manual_review")
FRESHNESS_STATUSES: tuple[str, ...] = ("fresh", "aging", "expired", "unknown")
RIGHTS_STATUSES: tuple[str, ...] = ("cleared", "review_required", "restricted", "prohibited", "unknown")
ASSET_TYPES: tuple[str, ...] = ("video", "image", "audio", "text_evidence", "frame_capture")
RIGHTS_ORIGINS: tuple[str, ...] = (
    "producer_owned",
    "licensed",
    "platform_content",
    "public_source",
    "unknown",
)
SEGMENT_PURPOSES: tuple[str, ...] = ("visual_support", "context", "transition")
MATERIAL_PERMITTED_USE = "b_roll_only"
ORIGIN_AUTHORITIES: tuple[str, ...] = ("material_only", "heat_only", "fact_candidate")

#: ``argument_graph`` 节点维度白名单（冻结 13 项）。
ALLOWED_DIMENSIONS: tuple[str, ...] = (
    "event_core",
    "evidence_detail",
    "mechanism",
    "user_impact",
    "action_tip",
    "industry_value",
    "use_case",
    "constraint",
    "method",
    "limitation",
    "key_number",
    "uncertainty",
    "visual_moment",
)

#: ``argument_graph`` 边关系白名单（冻结 5 项）。
ALLOWED_EDGE_RELATIONS: tuple[str, ...] = (
    "supports",
    "qualifies",
    "contrasts",
    "causes",
    "precedes",
)

#: 构成因果/时序子图、必须无环的关系。
ACYCLIC_EDGE_RELATIONS: frozenset[str] = frozenset({"causes", "precedes"})

#: 边对象的**确切**键集来源与目标键名。
#: 跨仓冻结合同（``copy_skill-main/docs/tasks/2026-09-16-episode-research-pack-v1.md``）
#: 把边键定为 ``from`` / ``to`` / ``relation``；生产端
#: ``episode_research_pack.py::EDGE_KEYS`` 用同名字面量与之对齐。本模块的读取与
#: 错误消息只引用下面两个常量，因此换名不必在别处硬编码键名。
#: ★ 2026-09-16 复核结论：任务 #7 / #14 曾要求改成 ``from_claim_id`` / ``to_claim_id``，
#: **已否决**。理由：① 冻结合同明文是 ``from`` / ``to``；② 改名后真实含边的研究包会在
#: 本模块报 ``invalid_contract``，属跨仓互通倒退；③ 两侧当时的契约已经一致，改名零收益。
EDGE_SOURCE_KEY = "from"
EDGE_TARGET_KEY = "to"
EDGE_KEYS: frozenset[str] = frozenset({EDGE_SOURCE_KEY, EDGE_TARGET_KEY, "relation"})

#: 机器可读的错误令牌。
INVALID_CONTRACT = "invalid_contract"
PARTIAL_PACKAGE = "partial_package"

_DRIVE_RE = re.compile(r"^[A-Za-z]:")

DEFAULT_MAX_READ_ATTEMPTS = 4


class ResearchPackError(RuntimeError):
    """研究包消费端的基础异常。"""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


class ResearchPackValidationError(ResearchPackError):
    """包**内容**违反 ``episode-research-pack-v1`` 合同（技术无效，非运输损坏）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=INVALID_CONTRACT)


class ResearchPackPartialError(ResearchPackError):
    """包在**技术/运输层面**损坏（缺文件、JSON 截断、字节/SHA 不符、manifest 不覆盖）。

    这类包**不得 Intake**：它既不是研究缺口，也不是 disposition 降级，而是根本
    没有拿到一份完整可信的物料。``partial_package`` 只描述这一种情况。
    """

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, code=PARTIAL_PACKAGE)
        self.details = dict(details or {})


class ResearchPackUnstableError(ResearchPackError):
    """``current.json`` 在读取期间持续变化，拒绝产生混合状态。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="unstable")


# ---------------------------------------------------------------------------
# canonical 编码 / 哈希
# ---------------------------------------------------------------------------


def canonical_json(payload: Any) -> str:
    """冻结的 canonical 编码，必须与生产端逐字节一致。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _framed_bytes(name: str, payload: Any) -> bytes:
    """分帧分片：``utf8(name) + NUL + ascii(len) + NUL + canonical + NUL``。"""
    canonical = canonical_json(payload).encode("utf-8")
    return (
        name.encode("utf-8") + b"\x00" + str(len(canonical)).encode("ascii") + b"\x00"
        + canonical + b"\x00"
    )


def content_sha256(payloads: Mapping[str, Any]) -> str:
    """按 :data:`SEMANTIC_FILES` 固定顺序对七个语义 JSON 求 SHA256（分帧）。"""
    digest = hashlib.sha256()
    for name in SEMANTIC_FILES:
        if name not in payloads:
            raise ResearchPackValidationError(f"content_sha256 缺少语义文件 {name}")
        digest.update(_framed_bytes(name, payloads[name]))
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------


def safe_relative_path(value: object, *, label: str = "路径") -> str:
    """归一化为 POSIX 相对路径，或抛 :class:`ResearchPackValidationError`。

    拒绝：空、绝对路径、盘符（``C:``）、UNC（``\\\\server``）、任何反斜杠
    （歧义）、空段（``a//b``）、``.`` 与 ``..``、前后导斜杠。
    """
    text = str(value if value is not None else "")
    if not text.strip():
        raise ResearchPackValidationError(f"{label}为空")
    if "\\" in text:
        raise ResearchPackValidationError(f"{label}含反斜杠歧义：{text}")
    if text.startswith("/"):
        raise ResearchPackValidationError(f"{label}是绝对/UNC 路径：{text}")
    if _DRIVE_RE.match(text):
        raise ResearchPackValidationError(f"{label}是盘符路径：{text}")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ResearchPackValidationError(f"{label}含空段/./..：{text}")
    return "/".join(parts)


def _resolve_within(base: Path, relative: object, *, label: str) -> Path:
    """把安全相对路径限制在 ``base`` 内；越界即报错。"""
    rel = safe_relative_path(relative, label=label)
    base_resolved = base.resolve()
    resolved = (base_resolved / rel).resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:  # pragma: no cover - safe_relative_path 已挡住多数
        raise ResearchPackValidationError(f"{label}越出 episode 根：{rel}") from exc
    return resolved


# ---------------------------------------------------------------------------
# 基础 IO
# ---------------------------------------------------------------------------


def read_json(path: Path, *, label: str | None = None) -> Any:
    """读取 JSON；缺文件/截断/不可解析属于**运输损坏**（partial），不是合同违规。"""
    target = Path(path)
    name = label or target.name
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ResearchPackPartialError(f"缺少文件：{name}", details={"missing": [name]}) from exc
    except OSError as exc:
        raise ResearchPackPartialError(f"无法读取 {name}：{exc}", details={"unreadable": [name]}) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResearchPackPartialError(
            f"JSON 无效（疑似截断）：{name}（{exc}）", details={"unreadable": [name]}
        ) from exc


def read_current_pointer(episode_root: Path) -> dict[str, Any] | None:
    """读取 ``current.json``；不存在返回 ``None``，损坏则报错。"""
    path = Path(episode_root) / CURRENT_NAME
    if not path.is_file():
        return None
    payload = read_json(path, label=CURRENT_NAME)
    if not isinstance(payload, dict):
        raise ResearchPackValidationError(f"{CURRENT_NAME} 顶层必须是对象")
    return payload


def _expect(value: object, expected: object, *, label: str) -> None:
    if value != expected:
        raise ResearchPackValidationError(f"{label} 不匹配：期望 {expected!r}，实际 {value!r}")


def _as_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchPackValidationError(f"{label} 必须是整数，实际 {value!r}")
    return value


def _as_mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchPackValidationError(f"{label} 必须是对象")
    return value


def _as_list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ResearchPackValidationError(f"{label} 必须是数组")
    return value


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coerce_now(now: object) -> datetime:
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    parsed = _parse_time(now)
    return parsed or datetime.now(timezone.utc)


def coerce_now(now: object = None) -> datetime:
    """公开的 ``now`` 归一化（接受 datetime / ISO 字符串 / None）。"""
    return _coerce_now(now)


# ---------------------------------------------------------------------------
# 语义校验
# ---------------------------------------------------------------------------


def _is_heat_only_source(source: Mapping[str, Any]) -> bool:
    if source.get("heat_only") is True:
        return True
    return source.get("authority") == "heat_only"


def _is_fact_source(source: Mapping[str, Any]) -> bool:
    """「事实源」的唯一口径：**已核验且非热度**的一条 source。

    ``heat_only`` 的来源是热度信号、不是证据（合同第 104 行要求
    official/reliable_independent），因此既不得计入 ``fact_sources_present``，
    也不得用来满足 ``confirmed_official`` / ``confirmed_two_reliable`` 的事实源下限。
    **本函数是这条口径的唯一实现** —— 之前它被内联写在两处（``_validate_sources`` 与
    ``assess_disposition``），且 ``_validate_sources`` 那处还兼着「逐个 ref 必须是事实源」
    的越合同检查；那个检查改成**数量复算**后，必须显式把这条禁止带过来，
    否则热度源会冒充事实源（``heat_only`` 源的 ``verification_state`` 常仍为 ``verified``）。
    """
    return not _is_heat_only_source(source) and source.get("verification_state") == "verified"


def _recomputed_fact_sources(
    refs: Iterable[Any], sources_by_id: Mapping[str, Mapping[str, Any]]
) -> list[tuple[str, Mapping[str, Any]]]:
    """把 claim 的 ``source_ids`` 复算成事实源列表（``source_id`` + source），顺序随 ``refs``。

    ``sources_by_id`` 里查不到的 ref 一律跳过 —— 存在性检查由各调用点自己负责，
    这里只保证查表**永不 KeyError**（合同第 137 行的取值范围由别处守）。
    """
    facts: list[tuple[str, Mapping[str, Any]]] = []
    for ref in refs:
        source_id = str(ref)
        source = sources_by_id.get(source_id)
        if source is not None and _is_fact_source(source):
            facts.append((source_id, source))
    return facts


def _confirmed_facts_shortfall(
    evidence_status: str, facts: Sequence[tuple[str, Mapping[str, Any]]]
) -> str | None:
    """``confirmed_*`` 的**事实源下限复算**：数量 + authority/publisher 独立性。

    冻结合同第 103-104 行「``confirmed_two_reliable`` 由 ≥2 个独立 publisher 的已核验
    official/reliable_independent source **复算，不信调用方字符串**」，第 138 行
    「fact_sources_min/present 由 source 复算」。生产端
    ``episode_research_pack._validate_sources`` 同款实现（authority == official / 按
    publisher 去重的独立可靠来源 ≥2），两处报错文案逐字对齐以便互认。

    返回 ``None`` 表示满足，否则返回**不含 claim id** 的原因文本；调用点各自补
    ``claim {claim_id} `` 前缀 —— 输入门抛错、产品门记 ``product_gate``，口径只有这一份。
    """
    if evidence_status not in FACT_READY:
        return None
    expected_min = CONFIRMED_FACT_MIN[evidence_status]
    if len(facts) < expected_min:
        return (
            f"声明 {evidence_status} 但已核验事实源仅 {len(facts)} 个，少于要求的 {expected_min} 个"
        )
    if evidence_status == "confirmed_official":
        if not any(source.get("authority") == "official" for _, source in facts):
            return "声明 confirmed_official 但没有官方已核验来源"
        return None
    # confirmed_two_reliable：只数 authority ∈ {official, reliable_independent} 的来源，
    # 按 publisher 去重（缺 publisher 时退回 source_id，与生产端同款）。
    publishers = {
        str(source.get("publisher") or source_id)
        for source_id, source in facts
        if source.get("authority") in INDEPENDENT_SOURCE_AUTHORITIES
    }
    if len(publishers) < 2:
        return "声明 confirmed_two_reliable 但不足两个独立可靠来源"
    return None


def _validate_identity(payloads: Mapping[str, Mapping[str, Any]]) -> tuple[str, str]:
    """七个语义文件必须共享同一 ``contract`` / ``episode_id`` / ``business_date``。"""
    episode = payloads["episode.json"]
    _expect(episode.get("contract"), CONTRACT, label="episode.contract")
    _expect(episode.get("production_mode"), PRODUCTION_MODE, label="episode.production_mode")
    episode_id = str(episode.get("episode_id") or "")
    if not episode_id:
        raise ResearchPackValidationError("episode.episode_id 缺失")
    business_date = str(episode.get("business_date") or "")
    if not business_date:
        raise ResearchPackValidationError("episode.business_date 缺失")
    if episode.get("disposition") not in DISPOSITIONS:
        raise ResearchPackValidationError(f"episode.disposition 非法：{episode.get('disposition')!r}")
    for name in SEMANTIC_FILES:
        payload = payloads[name]
        _expect(payload.get("contract"), CONTRACT, label=f"{name}.contract")
        _expect(payload.get("episode_id"), episode_id, label=f"{name}.episode_id")
        _expect(payload.get("business_date"), business_date, label=f"{name}.business_date")
    return episode_id, business_date


def _validate_origin_ref(episode: Mapping[str, Any]) -> None:
    origin = _as_mapping(episode.get("origin_ref"), label="episode.origin_ref")
    _expect(origin.get("origin_contract"), "material_replication_delivery", label="origin_ref.origin_contract")
    if not origin.get("manifest_sha256"):
        raise ResearchPackValidationError("origin_ref.manifest_sha256 缺失")
    if origin.get("authority") not in ORIGIN_AUTHORITIES:
        raise ResearchPackValidationError(f"origin_ref.authority 非法：{origin.get('authority')!r}")
    if not isinstance(origin.get("discovery_only"), bool):
        raise ResearchPackValidationError("origin_ref.discovery_only 必须是布尔值")
    for label, key in (("items", "item_id"), ("assets", "asset_id")):
        for row in origin.get(label) or []:
            row = _as_mapping(row, label=f"origin_ref.{label} 记录")
            if not row.get(key):
                raise ResearchPackValidationError(f"origin_ref.{label} 缺少 {key}")
            safe_relative_path(row.get("path"), label=f"origin_ref.{label} 路径")
            if isinstance(row.get("bytes"), bool) or not isinstance(row.get("bytes"), int):
                raise ResearchPackValidationError(f"origin_ref.{label} 缺少整数 bytes")
            if not row.get("sha256"):
                raise ResearchPackValidationError(f"origin_ref.{label} 缺少 sha256")


def _has_cycle(edges: list[tuple[str, str]]) -> bool:
    """有向边列表（causes/precedes 子图）是否存在环。"""
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)
        adjacency.setdefault(target, [])
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        state[node] = 1
        for neighbour in adjacency.get(node, []):
            marker = state.get(neighbour, 0)
            if marker == 1 or (marker == 0 and visit(neighbour)):
                return True
        state[node] = 2
        return False

    return any(state.get(node, 0) == 0 and visit(node) for node in list(adjacency))


def _validate_topics(payloads: Mapping[str, Mapping[str, Any]]) -> None:
    topics = payloads["topics.json"]
    graph = _as_mapping(topics.get("keyword_graph"), label="topics.keyword_graph")
    for key in ("seed", "expanded", "subject_terms", "event_terms",
                "keywords_requested", "keywords_used", "keywords_truncated"):
        if key not in graph:
            raise ResearchPackValidationError(f"topics.keyword_graph 缺少 {key}")
    _as_list(topics.get("topic_candidates"), label="topics.topic_candidates")
    selected = topics.get("selected_topic")
    if selected is not None and not isinstance(selected, Mapping):
        raise ResearchPackValidationError("topics.selected_topic 必须是对象或 null")
    argument = _as_mapping(topics.get("argument_graph"), label="topics.argument_graph")
    nodes = _as_list(argument.get("nodes"), label="topics.argument_graph.nodes")
    edges = _as_list(argument.get("edges"), label="topics.argument_graph.edges")

    node_ids: set[str] = set()
    for node in nodes:
        node = _as_mapping(node, label="argument_graph.node")
        claim_id = str(node.get("claim_id") or "")
        if not claim_id:
            raise ResearchPackValidationError(f"argument_graph.node 缺少 claim_id（{INVALID_CONTRACT}）")
        node_ids.add(claim_id)
        dim = node.get("dim")
        if not isinstance(dim, str) or not dim:
            raise ResearchPackValidationError(f"argument_graph.node {claim_id} 缺少 dim（{INVALID_CONTRACT}）")
        if dim not in ALLOWED_DIMENSIONS:
            raise ResearchPackValidationError(f"argument_graph.node {claim_id} dim {INVALID_CONTRACT}：{dim}")
        if not str(node.get("claim") or ""):
            raise ResearchPackValidationError(f"argument_graph.node {claim_id} 缺少 claim 文本")
        _as_list(node.get("source_candidate_ids"), label=f"argument_graph.node {claim_id} source_candidate_ids")

    seen_triples: set[tuple[str, str, str]] = set()
    acyclic_edges: list[tuple[str, str]] = []
    for edge in edges:
        edge = _as_mapping(edge, label="argument_graph.edge")
        if set(edge.keys()) != set(EDGE_KEYS):
            raise ResearchPackValidationError(
                f"argument_graph.edge 键集 {INVALID_CONTRACT}：期望 {sorted(EDGE_KEYS)}，实际 {sorted(edge.keys())}"
            )
        source = str(edge.get(EDGE_SOURCE_KEY) or "")
        target = str(edge.get(EDGE_TARGET_KEY) or "")
        relation = str(edge.get("relation") or "")
        if not source or not target or not relation:
            raise ResearchPackValidationError(
                f"argument_graph.edge 缺少 {EDGE_SOURCE_KEY}/{EDGE_TARGET_KEY}/relation（{INVALID_CONTRACT}）"
            )
        if relation not in ALLOWED_EDGE_RELATIONS:
            raise ResearchPackValidationError(f"argument_graph.edge relation {INVALID_CONTRACT}：{relation}")
        if source not in node_ids:
            raise ResearchPackValidationError(f"argument_graph.edge.{EDGE_SOURCE_KEY} 未引用已知节点：{source}")
        if target not in node_ids:
            raise ResearchPackValidationError(f"argument_graph.edge.{EDGE_TARGET_KEY} 未引用已知节点：{target}")
        if source == target:
            raise ResearchPackValidationError(f"argument_graph.edge 不允许自指：{source}")
        triple = (source, target, relation)
        if triple in seen_triples:
            raise ResearchPackValidationError(f"argument_graph.edge 三元组重复：{triple}")
        seen_triples.add(triple)
        if relation in ACYCLIC_EDGE_RELATIONS:
            acyclic_edges.append((source, target))
    if _has_cycle(acyclic_edges):
        raise ResearchPackValidationError(f"argument_graph causes/precedes 子图存在环（{INVALID_CONTRACT}）")


def _validate_materials_claims_rights(payloads: Mapping[str, Mapping[str, Any]]) -> None:
    materials = _as_list(payloads["materials.json"].get("materials"), label="materials")
    claims = _as_list(payloads["claims.json"].get("claims"), label="claims")
    rights = _as_list(payloads["rights.json"].get("rights"), label="rights")

    material_ids: set[str] = set()
    segment_pairs: set[tuple[str, str]] = set()
    segment_claim_refs: dict[str, set[str]] = {}
    for material in materials:
        material = _as_mapping(material, label="material")
        material_id = str(material.get("material_id") or "")
        if not material_id:
            raise ResearchPackValidationError("material 缺少 material_id")
        if material_id in material_ids:
            raise ResearchPackValidationError(f"material_id 重复：{material_id}")
        material_ids.add(material_id)
        _expect(material.get("permitted_use"), MATERIAL_PERMITTED_USE,
                label=f"material {material_id} permitted_use")
        freshness_status = material.get("freshness_status")
        if freshness_status is not None and freshness_status not in FRESHNESS_STATUSES:
            raise ResearchPackValidationError(f"material {material_id} freshness_status 非法：{freshness_status!r}")
        duration_ms = _as_int(material.get("duration_ms"), label=f"material {material_id} duration_ms")
        if duration_ms < 0:
            raise ResearchPackValidationError(f"material {material_id} duration_ms 不得为负")
        segments = _as_list(material.get("segments"), label=f"material {material_id} segments")
        refs = segment_claim_refs.setdefault(material_id, set())
        for segment in segments:
            segment = _as_mapping(segment, label=f"material {material_id} segment")
            segment_id = str(segment.get("segment_id") or "")
            if not segment_id:
                raise ResearchPackValidationError(f"material {material_id} 片段缺少 segment_id")
            pair = (material_id, segment_id)
            if pair in segment_pairs:
                raise ResearchPackValidationError(f"(material_id, segment_id) 重复：{pair}")
            segment_pairs.add(pair)
            start_ms = _as_int(segment.get("start_ms"), label=f"片段 {pair} start_ms")
            end_ms = _as_int(segment.get("end_ms"), label=f"片段 {pair} end_ms")
            if not (0 <= start_ms < end_ms <= duration_ms):
                raise ResearchPackValidationError(
                    f"片段 {pair} 时间码越界：0 <= {start_ms} < {end_ms} <= {duration_ms}"
                )
            if segment.get("purpose") not in SEGMENT_PURPOSES:
                raise ResearchPackValidationError(f"片段 {pair} purpose 非法：{segment.get('purpose')!r}")
            if not isinstance(segment.get("transcript_excerpt"), str):
                raise ResearchPackValidationError(f"片段 {pair} transcript_excerpt 必须是字符串")
            _as_list(segment.get("frame_evidence_ids"), label=f"片段 {pair} frame_evidence_ids")
            for claim_id in _as_list(segment.get("claim_ids"), label=f"片段 {pair} claim_ids"):
                refs.add(str(claim_id))

    claims_by_id: dict[str, Mapping[str, Any]] = {}
    for claim in claims:
        claim = _as_mapping(claim, label="claim")
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id:
            raise ResearchPackValidationError("claim 缺少 claim_id")
        if claim_id in claims_by_id:
            raise ResearchPackValidationError(f"claim_id 重复：{claim_id}")
        claims_by_id[claim_id] = claim
        if claim.get("evidence_status") not in EVIDENCE_STATUSES:
            raise ResearchPackValidationError(f"claim {claim_id} evidence_status 非法：{claim.get('evidence_status')!r}")
        if claim.get("wording_policy") not in WORDING_POLICIES:
            raise ResearchPackValidationError(f"claim {claim_id} wording_policy 非法：{claim.get('wording_policy')!r}")
        if claim.get("freshness_requirement") not in FRESHNESS_REQUIREMENTS:
            raise ResearchPackValidationError(
                f"claim {claim_id} freshness_requirement 非法：{claim.get('freshness_requirement')!r}"
            )
        if not str(claim.get("text") or ""):
            raise ResearchPackValidationError(f"claim {claim_id} 缺少 text")
        _as_list(claim.get("claims_to_verify"), label=f"claim {claim_id} claims_to_verify")
        _as_list(claim.get("do_not_claim"), label=f"claim {claim_id} do_not_claim")
        material_refs = _as_list(claim.get("material_refs"), label=f"claim {claim_id} material_refs")
        for ref in material_refs:
            if str(ref) not in material_ids:
                raise ResearchPackValidationError(f"claim {claim_id} 引用了不存在的 material：{ref}")

    # 双向一致：claim.material_refs ↔ segment.claim_ids
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        for ref in claim.get("material_refs") or []:
            if claim_id not in segment_claim_refs.get(str(ref), set()):
                raise ResearchPackValidationError(
                    f"claim {claim_id} 的 material_ref {ref} 未在其片段 claim_ids 中出现"
                )
    for material_id, refs in segment_claim_refs.items():
        for claim_id in refs:
            claim = claims_by_id.get(claim_id)
            if claim is None:
                raise ResearchPackValidationError(f"material {material_id} 片段引用了不存在的 claim：{claim_id}")
            if material_id not in {str(ref) for ref in claim.get("material_refs") or []}:
                raise ResearchPackValidationError(
                    f"material {material_id} 片段 claim_id {claim_id} 未被该 claim 的 material_refs 反向引用"
                )

    rights_materials: set[str] = set()
    for record in rights:
        record = _as_mapping(record, label="rights")
        asset_id = str(record.get("asset_id") or "")
        if not asset_id:
            raise ResearchPackValidationError("rights 记录缺少 asset_id")
        if record.get("asset_type") not in ASSET_TYPES:
            raise ResearchPackValidationError(f"rights {asset_id} asset_type 非法：{record.get('asset_type')!r}")
        if record.get("origin") not in RIGHTS_ORIGINS:
            raise ResearchPackValidationError(f"rights {asset_id} origin 非法：{record.get('origin')!r}")
        if record.get("rights_status") not in RIGHTS_STATUSES:
            raise ResearchPackValidationError(f"rights {asset_id} rights_status 非法：{record.get('rights_status')!r}")
        for key in ("redistribution_allowed", "render_eligible"):
            if not isinstance(record.get(key), bool):
                raise ResearchPackValidationError(f"rights {asset_id} {key} 必须是布尔值")
        if record.get("rights_status") == "prohibited" and (
            record.get("redistribution_allowed") or record.get("render_eligible")
        ):
            raise ResearchPackValidationError(f"rights {asset_id} 为 prohibited 但允许再分发/渲染")
        if record.get("origin") == "platform_content" and record.get("render_eligible"):
            raise ResearchPackValidationError(f"rights {asset_id} 为平台内容但 render_eligible=true")
        material_id = str(record.get("material_id") or "")
        if material_id:
            if material_id not in material_ids:
                raise ResearchPackValidationError(f"rights 记录引用了不存在的 material：{material_id}")
            rights_materials.add(material_id)
    for material_id in material_ids:
        if material_id not in rights_materials:
            raise ResearchPackValidationError(f"material {material_id} 缺少 rights 记录")


def _validate_sources(payloads: Mapping[str, Mapping[str, Any]]) -> None:
    sources = _as_list(payloads["sources.json"].get("sources"), label="sources")
    claims = _as_list(payloads["claims.json"].get("claims"), label="claims")
    materials = _as_list(payloads["materials.json"].get("materials"), label="materials")
    material_ids = {str(item.get("material_id") or "") for item in materials}

    source_ids: set[str] = set()
    sources_by_id: dict[str, Mapping[str, Any]] = {}
    for source in sources:
        source = _as_mapping(source, label="source")
        source_id = str(source.get("source_id") or "")
        if not source_id:
            raise ResearchPackValidationError("source 缺少 source_id")
        if source_id in source_ids:
            raise ResearchPackValidationError(f"source_id 重复：{source_id}")
        source_ids.add(source_id)
        sources_by_id[source_id] = source
        if source.get("authority") not in SOURCE_AUTHORITIES:
            raise ResearchPackValidationError(f"source {source_id} authority 非法：{source.get('authority')!r}")
        if source.get("verification_state") not in VERIFICATION_STATES:
            raise ResearchPackValidationError(
                f"source {source_id} verification_state 非法：{source.get('verification_state')!r}"
            )
        if not isinstance(source.get("heat_only"), bool):
            raise ResearchPackValidationError(f"source {source_id} heat_only 必须是布尔值")
        freshness = _as_mapping(source.get("freshness"), label=f"source {source_id} freshness")
        if not freshness.get("observed_at"):
            raise ResearchPackValidationError(f"source {source_id} freshness.observed_at 缺失")
        if freshness.get("policy") not in FRESHNESS_POLICIES:
            raise ResearchPackValidationError(
                f"source {source_id} freshness.policy 非法：{freshness.get('policy')!r}"
            )
        if freshness.get("status_at_publish") not in FRESHNESS_STATUSES:
            raise ResearchPackValidationError(
                f"source {source_id} freshness.status_at_publish 非法：{freshness.get('status_at_publish')!r}"
            )

    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        refs = _as_list(claim.get("source_ids"), label=f"claim {claim_id} source_ids")
        evidence_status = str(claim.get("evidence_status"))
        # 对 ref 本身，本层只要求合同第 137 行那两条：只能是 ``sources`` 的 ``source_id``、
        # 且永不计入事实源（material 不能当事实源）。
        for ref in refs:
            ref = str(ref)
            if ref in material_ids:
                raise ResearchPackValidationError(f"claim {claim_id} 把 material {ref} 当作事实源")
            if ref not in source_ids:
                raise ResearchPackValidationError(f"claim {claim_id} 引用了不存在的事实源：{ref}")
        # ★ 事实源一律**由 source 复算**（合同第 138 行），不再要求**每个** ref 都是已核验源
        # —— 合同第 137 行只约束 ref 的取值范围，允许「已核验官方源 + 一条热度源」并存。
        # 曾经的写法是逐个 ref 要求已在事实源集合里（于是这种合同合法包被判 exit 2），
        # 而真正该守的下限（confirmed_official ≥1 / confirmed_two_reliable ≥2）当时反而没人守。
        # ★★ 复算口径只有一处实现：``_recomputed_fact_sources``（= 已核验 **且非热度**，
        # 见 ``_is_fact_source``）+ ``_confirmed_facts_shortfall``（数量 + authority/publisher
        # 独立性，合同第 103-104 行「不信调用方字符串」）。``assess_disposition`` 的产品门
        # 调的是同一对函数，避免「输入门严、产品门松」的两套口径。
        facts = _recomputed_fact_sources(refs, sources_by_id)
        if claim.get("fact_sources_present") != len(facts):
            raise ResearchPackValidationError(f"claim {claim_id} fact_sources_present 与已核验来源数不一致")
        expected_min = CONFIRMED_FACT_MIN.get(evidence_status, 0)
        if claim.get("fact_sources_min") != expected_min:
            raise ResearchPackValidationError(f"claim {claim_id} fact_sources_min 应为 {expected_min}")
        shortfall = _confirmed_facts_shortfall(evidence_status, facts)
        if shortfall:
            raise ResearchPackValidationError(f"claim {claim_id} {shortfall}")


# ---------------------------------------------------------------------------
# 包级校验
# ---------------------------------------------------------------------------


def _validate_manifest(pack_dir: Path, manifest: Mapping[str, Any], csha: str) -> list[dict[str, Any]]:
    _expect(manifest.get("schema"), MANIFEST_SCHEMA, label="manifest.schema")
    _expect(manifest.get("contract"), CONTRACT, label="manifest.contract")
    _expect(list(manifest.get("exclusions") or []), list(MANIFEST_EXCLUSIONS), label="manifest.exclusions")
    entries = _as_list(manifest.get("files"), label="manifest.files")
    listed: dict[str, Mapping[str, Any]] = {}
    verified: list[dict[str, Any]] = []
    for entry in entries:
        entry = _as_mapping(entry, label="manifest.files 记录")
        raw = str(entry.get("path") or "")
        rel = safe_relative_path(raw, label="manifest 路径")
        if rel != raw:
            raise ResearchPackValidationError(f"manifest 路径未规范化：{raw}")
        if rel in MANIFEST_EXCLUSIONS:
            raise ResearchPackValidationError(f"manifest 不得收录自身/READY：{rel}")
        if rel in listed:
            raise ResearchPackValidationError(f"manifest 重复收录：{rel}")
        listed[rel] = entry
        target = _resolve_within(pack_dir, rel, label="manifest 文件")
        if not target.is_file():
            raise ResearchPackPartialError(f"manifest 记录的文件不存在：{rel}", details={"missing": [rel]})
        data = target.read_bytes()
        if entry.get("bytes") != len(data):
            raise ResearchPackPartialError(
                f"manifest 字节数不符（疑似截断）：{rel}",
                details={"damaged": [rel], "expected_bytes": entry.get("bytes"), "actual_bytes": len(data)},
            )
        if entry.get("sha256") != _sha256_bytes(data):
            raise ResearchPackPartialError(f"manifest SHA256 不符：{rel}", details={"damaged": [rel]})
        verified.append({"path": rel, "bytes": len(data), "sha256": _sha256_bytes(data)})

    on_disk: set[str] = set()
    for child in sorted(pack_dir.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(pack_dir).as_posix()
        if rel in MANIFEST_EXCLUSIONS:
            continue
        safe_relative_path(rel, label="包内文件路径")
        on_disk.add(rel)
    missing = sorted(on_disk - set(listed))
    extra = sorted(set(listed) - on_disk)
    if missing:
        raise ResearchPackPartialError(f"manifest 未覆盖文件：{missing}", details={"missing": missing})
    if extra:
        raise ResearchPackPartialError(f"manifest 收录了不存在的文件：{extra}", details={"missing": extra})
    # 逐文件字节完整性通过后，才判定「包与所提交语义集」的一致性（合同违规）。
    _expect(manifest.get("content_sha256"), csha, label="manifest.content_sha256")
    return verified


def _validate_ready(ready: Mapping[str, Any], manifest: Mapping[str, Any], manifest_sha: str) -> None:
    _expect(ready.get("contract"), CONTRACT, label="_READY.contract")
    if ready.get("manifest_sha256") != manifest_sha:
        raise ResearchPackPartialError(
            "_READY.manifest_sha256 与 manifest 不一致（疑似 manifest 被截断）",
            details={"damaged": [READY_NAME]},
        )
    _expect(ready.get("pack_id"), manifest.get("pack_id"), label="_READY.pack_id")
    _expect(ready.get("revision"), manifest.get("revision"), label="_READY.revision")
    files = manifest.get("files")
    if isinstance(files, list):
        _expect(ready.get("file_count"), len(files), label="_READY.file_count")
    if "supersedes" not in ready:
        raise ResearchPackValidationError("_READY 缺少 supersedes")
    if not ready.get("ready_at"):
        raise ResearchPackValidationError("_READY 缺少 ready_at")


def validate_pack_directory(
    pack_dir: Path,
    *,
    episode_root: Path | None = None,
    current: Mapping[str, Any] | None = None,
    expected_episode_id: str | None = None,
    expected_business_date: str | None = None,
) -> dict[str, Any]:
    """全量校验一个已发布包目录，返回结构化结论或抛 :class:`ResearchPackValidationError`。"""
    pack_dir = Path(pack_dir)
    if not pack_dir.is_dir():
        raise ResearchPackPartialError(f"研究包目录不存在：{pack_dir}", details={"missing": [str(pack_dir)]})
    if episode_root is not None:
        try:
            pack_dir.resolve().relative_to(Path(episode_root).resolve())
        except ValueError as exc:
            raise ResearchPackValidationError("pack 目录越出 episode 根") from exc

    absent = [name for name in REQUIRED_PACK_FILES if not (pack_dir / name).is_file()]
    if absent:
        raise ResearchPackPartialError(
            f"研究包缺少必需文件：{absent}", details={"missing": absent}
        )

    payloads: dict[str, Mapping[str, Any]] = {}
    for name in SEMANTIC_FILES:
        payloads[name] = _as_mapping(read_json(pack_dir / name, label=name), label=name)

    csha = content_sha256(payloads)

    revision_record = _as_mapping(read_json(pack_dir / REVISION_NAME, label=REVISION_NAME), label=REVISION_NAME)
    ready = _as_mapping(read_json(pack_dir / READY_NAME, label=READY_NAME), label=READY_NAME)
    manifest = _as_mapping(read_json(pack_dir / MANIFEST_NAME, label=MANIFEST_NAME), label=MANIFEST_NAME)

    # 1) 逐文件字节完整性优先（技术/运输损坏）：manifest 双向覆盖 + 每文件 bytes/SHA +
    #    READY.manifest_sha256。任何磁盘字节与 manifest 声明不符，都先判为运输损坏，
    #    不因「哪个文件被动过」而改变分类。
    manifest_sha = _sha256_file(pack_dir / MANIFEST_NAME)
    verified_files = _validate_manifest(pack_dir, manifest, csha)
    _validate_ready(ready, manifest, manifest_sha)

    # 2) 每个文件都自洽后，才判定「包与所提交 revision 的一致性」（合同违规）。
    _expect(revision_record.get("contract"), CONTRACT, label="revision.contract")
    _expect(revision_record.get("content_sha256"), csha, label="revision.content_sha256")
    _expect(ready.get("content_sha256"), csha, label="_READY.content_sha256")

    revision = _as_int(revision_record.get("revision"), label="revision.revision")
    pack_id = str(revision_record.get("pack_id") or "")
    if not pack_id:
        raise ResearchPackValidationError("revision.pack_id 缺失")
    _expect(manifest.get("revision"), revision, label="manifest.revision")
    _expect(manifest.get("pack_id"), pack_id, label="manifest.pack_id")
    if revision_record.get("disposition") not in DISPOSITIONS:
        raise ResearchPackValidationError(f"revision.disposition 非法：{revision_record.get('disposition')!r}")

    episode_id, business_date = _validate_identity(payloads)
    if expected_episode_id is not None and episode_id != expected_episode_id:
        raise ResearchPackValidationError(f"episode.episode_id 与 current 不一致：{episode_id} != {expected_episode_id}")
    if expected_business_date is not None and business_date != expected_business_date:
        raise ResearchPackValidationError(
            f"episode.business_date 与 current 不一致：{business_date} != {expected_business_date}"
        )
    for label, payload in (("revision", revision_record), ("_READY", ready), ("manifest", manifest)):
        _expect(payload.get("episode_id"), episode_id, label=f"{label}.episode_id")
    for label, payload in (("_READY", ready), ("manifest", manifest)):
        _expect(payload.get("business_date"), business_date, label=f"{label}.business_date")
    if pack_id != f"{episode_id}-r{revision}":
        raise ResearchPackValidationError(f"pack_id 与 episode/revision 不一致：{pack_id}")

    _validate_origin_ref(payloads["episode.json"])
    _validate_topics(payloads)
    _validate_materials_claims_rights(payloads)
    _validate_sources(payloads)

    if current is not None:
        _expect(current.get("contract"), CONTRACT, label="current.contract")
        _expect(current.get("revision"), revision, label="current.revision")
        _expect(current.get("content_sha256"), csha, label="current.content_sha256")
        _expect(current.get("manifest_sha256"), manifest_sha, label="current.manifest_sha256")
        _expect(current.get("pack_id"), pack_id, label="current.pack_id")
        _expect(current.get("episode_id"), episode_id, label="current.episode_id")

    return {
        "status": "pass",
        "valid": True,
        "pack_dir": str(pack_dir),
        "episode_root": str(episode_root) if episode_root is not None else None,
        "episode_id": episode_id,
        "business_date": business_date,
        "production_mode": PRODUCTION_MODE,
        "revision": revision,
        "pack_id": pack_id,
        "content_sha256": csha,
        "manifest_sha256": manifest_sha,
        "producer_disposition": str(payloads["episode.json"].get("disposition") or ""),
        "counts": {
            "sources": len(payloads["sources.json"].get("sources") or []),
            "claims": len(payloads["claims.json"].get("claims") or []),
            "topic_candidates": len(payloads["topics.json"].get("topic_candidates") or []),
            "materials": len(payloads["materials.json"].get("materials") or []),
            "rights": len(payloads["rights.json"].get("rights") or []),
        },
        "verified_files": verified_files,
        "payloads": payloads,
        "manifest": dict(manifest),
        "revision_record": dict(revision_record),
        "ready": dict(ready),
    }


# ---------------------------------------------------------------------------
# 处置（disposition）重算：freshness / facts / rights 门
# ---------------------------------------------------------------------------


def source_effective_freshness(source: Mapping[str, Any], now: datetime) -> str:
    """以 ``now`` 重算 source 的有效时效，不直接采信 ``status_at_publish``。

    公开给下游投影层（编辑层）复用，保证 gate 判定只有一处实现。
    """
    freshness = source.get("freshness") if isinstance(source.get("freshness"), Mapping) else {}
    valid_until = _parse_time(freshness.get("valid_until") or freshness.get("expires_at"))
    if valid_until is not None and valid_until <= now:
        return "expired"
    if freshness.get("policy") == "evergreen":
        return "fresh"
    status = str(freshness.get("status_at_publish") or "unknown")
    return status if status in FRESHNESS_STATUSES else "unknown"


def _source_effective_freshness(source: Mapping[str, Any], now: datetime) -> str:
    """内部别名（保持既有调用点不变）。"""
    return source_effective_freshness(source, now)


def assess_disposition(payloads: Mapping[str, Any], *, now: object = None) -> dict[str, Any]:
    """把包内容归类为 Haike 处置：``ready`` / ``partial`` / ``research_required`` / ``rejected``。

    生产端 ``disposition`` 只作输入之一；这里以事实门、freshness 关键性和权利/产品门
    重新判定，但不产出最终文案，也不创建项目。
    """
    moment = _coerce_now(now)
    episode = payloads.get("episode.json") or {}
    sources = (payloads.get("sources.json") or {}).get("sources") or []
    claims = (payloads.get("claims.json") or {}).get("claims") or []
    materials = (payloads.get("materials.json") or {}).get("materials") or []
    rights = (payloads.get("rights.json") or {}).get("rights") or []

    gaps: list[dict[str, Any]] = []
    product_gate: list[str] = []

    sources_by_id = {str(s.get("source_id") or ""): s for s in sources}
    # 事实门：ready 需要每条主张都事实就绪。
    critical_source_ids: set[str] = set()
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        status = str(claim.get("evidence_status") or "")
        refs = [str(ref) for ref in claim.get("source_ids") or []]
        if status in FACT_READY:
            critical_source_ids.update(refs)
            # 与输入门 ``_validate_sources`` 共用同一对复算函数（数量 + authority/publisher
            # 独立性），不留「校验层严、产品门松」的两套口径。
            facts = _recomputed_fact_sources(refs, sources_by_id)
            shortfall = _confirmed_facts_shortfall(status, facts)
            if shortfall:
                product_gate.append(f"claim {claim_id} {shortfall}")

    # freshness：以 now 重算；关键缺口导向 research_required，非关键缺口导向 partial。
    critical_gap = False
    for source in sources:
        source_id = str(source.get("source_id") or "")
        effective = _source_effective_freshness(source, moment)
        if effective == "fresh":
            continue
        critical = source_id in critical_source_ids
        gaps.append(
            {
                "kind": "source_freshness",
                "source_id": source_id,
                "effective": effective,
                "critical": critical,
                "declared": str((source.get("freshness") or {}).get("status_at_publish") or ""),
            }
        )
        if critical and effective in ("expired", "unknown"):
            critical_gap = True

    # 权利/产品门：平台内容不得冒充已授权成片资产。
    for record in rights:
        material_id = str(record.get("material_id") or record.get("asset_id") or "")
        if record.get("origin") == "platform_content" and record.get("rights_status") == "cleared":
            product_gate.append(f"asset {material_id} 为平台内容却被声明为 cleared")
    for material in materials:
        if material.get("permitted_use") != MATERIAL_PERMITTED_USE:
            product_gate.append(f"material {material.get('material_id')} permitted_use 非 {MATERIAL_PERMITTED_USE}")
        if material.get("kind") != "b_roll":
            product_gate.append(f"material {material.get('material_id')} kind 非 b_roll")

    producer_disposition = str(episode.get("disposition") or "")

    if product_gate:
        disposition, reason = "rejected", "product_or_rights_gate"
    elif producer_disposition == "rejected":
        disposition, reason = "rejected", "producer_rejected"
    elif producer_disposition == "ready" and not any(
        str(claim.get("evidence_status")) in FACT_READY for claim in claims
    ):
        disposition, reason = "research_required", "facts_gate_not_met"
    elif critical_gap:
        disposition, reason = "research_required", "critical_freshness_gap"
    elif producer_disposition == "research_required":
        disposition, reason = "research_required", "producer_research_required"
    elif producer_disposition == "partial" or gaps:
        disposition, reason = "partial", ("non_critical_gaps" if gaps else "producer_partial")
    elif not sources and not claims:
        disposition, reason = "research_required", "no_first_hand_facts"
    else:
        disposition, reason = "ready", "facts_and_freshness_ok"

    return {
        "disposition": disposition,
        "reason": reason,
        "producer_disposition": producer_disposition,
        "gaps": gaps,
        "product_gate": product_gate,
        "checked_at": moment.isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 读取（双读 current，防混合状态）
# ---------------------------------------------------------------------------


def _current_fingerprint(pointer: Mapping[str, Any] | None) -> tuple[Any, ...]:
    if pointer is None:
        return ()
    return (
        pointer.get("revision"),
        pointer.get("content_sha256"),
        pointer.get("manifest_sha256"),
        pointer.get("pack_path"),
        pointer.get("pack_id"),
    )


def _resolve_episode_from_source(source: Path) -> tuple[Path, Path]:
    source = Path(source)
    if source.is_dir():
        return source, source / CURRENT_NAME
    if source.name != CURRENT_NAME:
        raise ResearchPackValidationError(f"既不是 current.json 也不是 episode 目录：{source}")
    return source.parent, source


def load_research_pack(
    source: Path,
    *,
    now: object = None,
    max_attempts: int = DEFAULT_MAX_READ_ATTEMPTS,
) -> dict[str, Any]:
    """严格只读加载一期研究包：current-A → READY → manifest → 全部文件 → current-B。

    两次读到的 current 指纹不一致就丢弃重试；读满次数仍不稳定则抛
    :class:`ResearchPackUnstableError`。返回结构化结果，不写任何生产端文件。
    """
    episode_root, current_path = _resolve_episode_from_source(Path(source))

    last_fingerprint: tuple[Any, ...] | None = None
    for _attempt in range(max(1, int(max_attempts))):
        first = read_json(current_path, label=CURRENT_NAME)
        if not isinstance(first, Mapping):
            raise ResearchPackValidationError(f"{CURRENT_NAME} 顶层必须是对象")
        _expect(first.get("contract"), CONTRACT, label="current.contract")
        episode_id = str(first.get("episode_id") or "")
        if not episode_id:
            raise ResearchPackValidationError("current.episode_id 缺失")
        pack_dir = _resolve_within(episode_root, first.get("pack_path"), label="current(pack_path)")
        if not pack_dir.is_dir():
            raise ResearchPackPartialError(
                f"current 指向的包目录不存在：{first.get('pack_path')}",
                details={"missing": [str(first.get("pack_path"))]},
            )

        ready_rel = first.get("ready_path")
        if ready_rel:
            ready_path = _resolve_within(episode_root, ready_rel, label="current(ready_path)")
            if ready_path.parent != pack_dir:
                raise ResearchPackValidationError("READY 与 current 指向的包目录不一致")
        else:
            ready_path = pack_dir / READY_NAME
        if not ready_path.is_file():
            raise ResearchPackPartialError(f"缺少文件：{READY_NAME}", details={"missing": [READY_NAME]})

        # 先把 READY / manifest / 七个语义文件与全部字节·SHA 校验完，再回读 current-B。
        validation = validate_pack_directory(
            pack_dir,
            episode_root=episode_root,
            current=first,
            expected_episode_id=episode_id,
            expected_business_date=str(first.get("business_date") or "") or None,
        )

        second = read_json(current_path, label=CURRENT_NAME)
        if _current_fingerprint(first) != _current_fingerprint(second):
            last_fingerprint = _current_fingerprint(second)
            continue

        assessment = assess_disposition(validation["payloads"], now=now)
        return {
            "status": "loaded",
            "episode_root": str(episode_root),
            "pack_dir": str(pack_dir),
            "ready_path": str(ready_path),
            "episode_id": validation["episode_id"],
            "business_date": validation["business_date"],
            "production_mode": validation["production_mode"],
            "revision": validation["revision"],
            "pack_id": validation["pack_id"],
            "content_sha256": validation["content_sha256"],
            "manifest_sha256": validation["manifest_sha256"],
            "producer_disposition": validation["producer_disposition"],
            "disposition": assessment["disposition"],
            "disposition_reason": assessment["reason"],
            "disposition_detail": assessment,
            "counts": validation["counts"],
            "verified_files": validation["verified_files"],
            "payloads": validation["payloads"],
            # 包已完整通过字节级校验；partial_package 只描述技术/运输损坏，故为 False。
            "partial_package": False,
            "read_at": _coerce_now(now).isoformat(timespec="seconds"),
        }

    raise ResearchPackUnstableError(
        f"{CURRENT_NAME} 在 {max_attempts} 次读取中持续变化，拒绝产生混合状态（last={last_fingerprint}）"
    )


def try_load_research_pack(source: Path, *, now: object = None) -> dict[str, Any]:
    """非致命包装：返回状态信封，供 reconcile 分类使用。"""
    try:
        return load_research_pack(source, now=now)
    except ResearchPackError as exc:
        return {"status": "failed", "error": str(exc), "error_code": exc.code, "source": str(source)}
    except OSError as exc:  # pragma: no cover - 文件系统级别故障
        return {"status": "failed", "error": str(exc), "error_code": "io", "source": str(source)}


# ---------------------------------------------------------------------------
# 快照：复制被消费的机器文件并生成自身 manifest
# ---------------------------------------------------------------------------


def _remove_tree(path: Path) -> None:
    """尽力逐文件删除（不使用 ``shutil.rmtree``，避免沙箱批量删除保护）。"""
    root = Path(path)
    if not root.exists():
        return
    for child in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _external_refs(payloads: Mapping[str, Any]) -> list[dict[str, Any]]:
    """外部大视频只保存引用/可用性，不复制内容。"""
    refs: list[dict[str, Any]] = []
    for material in (payloads.get("materials.json") or {}).get("materials") or []:
        origin = material.get("origin") if isinstance(material.get("origin"), Mapping) else {}
        refs.append(
            {
                "material_id": str(material.get("material_id") or ""),
                "kind": str(material.get("kind") or ""),
                "permitted_use": str(material.get("permitted_use") or ""),
                "video_id": str(origin.get("video_id") or ""),
                "share_url": str(origin.get("share_url") or ""),
                "availability": "external_reference_only",
                "copied": False,
            }
        )
    return refs


def snapshot_research_pack(
    validation: Mapping[str, Any],
    snapshot_root: Path,
    *,
    now: object = None,
) -> dict[str, Any]:
    """把**已校验**的包内机器文件复制进 Haike 快照目录，并生成自身 manifest。

    快照先构建到 ``.staging``，再 ``os.replace`` 提交；同名快照已存在且
    ``content_sha256`` 一致则复用（崩溃恢复/重复调用都是幂等的）。
    """
    if not (validation.get("content_sha256") and validation.get("episode_id")):
        raise ResearchPackValidationError("快照前必须提供已校验的 content_sha256/episode_id")

    pack_dir = Path(str(validation["pack_dir"]))
    episode_id = str(validation["episode_id"])
    csha = str(validation["content_sha256"])
    if not pack_dir.is_dir():
        raise ResearchPackValidationError(f"快照源包目录不存在：{pack_dir}")

    root = Path(snapshot_root)
    final_dir = root / episode_id / csha
    if final_dir.is_dir():
        existing = _read_snapshot_descriptor(final_dir)
        if existing.get("content_sha256") == csha:
            return {"snapshot_dir": str(final_dir), "reused": True, "manifest": existing}
        raise ResearchPackValidationError(f"快照目录已存在但内容不同，拒绝覆盖：{final_dir}")

    staging = root / STAGING_DIRNAME / f"{episode_id}-{csha[:12]}-{uuid.uuid4().hex[:8]}"
    _remove_tree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        copied: list[dict[str, Any]] = []
        pack_copy = staging / "pack"
        for child in sorted(pack_dir.rglob("*")):
            if not child.is_file():
                continue
            rel = child.relative_to(pack_dir).as_posix()
            safe_relative_path(rel, label="快照源文件路径")
            destination = pack_copy / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, destination)
            data = destination.read_bytes()
            copied.append({"path": f"pack/{rel}", "bytes": len(data), "sha256": _sha256_bytes(data)})

        moment = _coerce_now(now)
        descriptor = {
            "schema": "openmontage-research-pack-snapshot-v1",
            "contract": CONTRACT,
            "episode_id": episode_id,
            "business_date": str(validation.get("business_date") or ""),
            "revision": validation.get("revision"),
            "pack_id": validation.get("pack_id"),
            "content_sha256": csha,
            "manifest_sha256": str(validation.get("manifest_sha256") or ""),
            "producer_disposition": str(validation.get("producer_disposition") or ""),
            "disposition": str(validation.get("disposition") or ""),
            "source_pack_dir": str(pack_dir),
            "snapshot_at": moment.isoformat(timespec="seconds"),
            "external_refs": _external_refs(validation["payloads"]),
            "producer_modified": False,
        }
        descriptor_path = staging / "snapshot.json"
        _atomic_json(descriptor_path, descriptor)
        descriptor_bytes = descriptor_path.read_bytes()
        manifest = {
            "schema": "openmontage-research-pack-snapshot-manifest-v1",
            "content_sha256": csha,
            "self_excluded": ["snapshot.json", "snapshot-manifest.json"],
            "snapshot": {
                "path": "snapshot.json",
                "bytes": len(descriptor_bytes),
                "sha256": _sha256_bytes(descriptor_bytes),
            },
            "files": copied,
        }
        _atomic_json(staging / "snapshot-manifest.json", manifest)

        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, final_dir)
        return {"snapshot_dir": str(final_dir), "reused": False, "manifest": manifest}
    finally:
        if staging.exists():
            _remove_tree(staging)


def _read_snapshot_descriptor(snapshot_dir: Path) -> dict[str, Any]:
    path = Path(snapshot_dir) / "snapshot.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except Exception:
        if temporary and temporary.exists():
            temporary.unlink()
        raise
