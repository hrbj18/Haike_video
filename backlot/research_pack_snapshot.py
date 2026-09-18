"""编辑层消费的 **normalized editorial snapshot**：把已严格校验的研究包投影成稳定结构。

分工（与 team-lead 的划分一致）：

* ``copy_skill_research_pack`` 负责**权威校验**（current → READY → manifest → 七语义
  文件、hash、路径安全、外键、时间码、权限门）与快照。
* 本模块只做**纯投影**：输入是校验通过的 ``load_research_pack`` 结果，输出是
  编辑层（``remake_intake`` / ``build_editorial_decision``）可直接消费的稳定快照。
  这一层不写盘、不判定最终文案、不创建项目，也不新增任何信任假设——
  它只在已验证数据上重命名与补充派生标记。

投影结果字段（冻结；编辑层按此对接）：

- 身份：``contract`` / ``episode_id`` / ``business_date`` / ``theme`` /
  ``production_mode`` / ``revision`` / ``pack_id`` / ``content_sha256`` / ``manifest_sha256``
- 处置：``disposition``（Haike 重算）/ ``producer_disposition`` /
  ``disposition_reason`` / ``partial_package``。注意 ``partial_package`` 只表示
  **技术/运输损坏**，而这类包在 ``load_research_pack`` 阶段就抛错、根本到不了本层；
  因此本投影里的 ``partial_package`` 恒为 ``False``（保留字段只为编辑层接口稳定）。
  研究缺口 / disposition 降级用 ``disposition`` 与 ``freshness_gates`` 表达，绝不复用本字段。
- 主题：``keywords``（= topics.keyword_graph）/ ``topic_candidates`` /
  ``selected_topic`` / ``argument_graph``
- 证据：``sources``（含 ``fact_source``、``effective_freshness``）/ ``claims``
  （含 ``fact_ready``）/ ``materials`` / ``rights`` / ``audience``
- 门：``freshness_gates`` / ``product_gate`` / ``counts`` / ``verified_files``

编辑层稳定接口由 editorial-dev 维护，本模块不改动它。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from backlot.copy_skill_research_pack import (
    CONTRACT,
    FACT_READY,
    ResearchPackError,
    assess_disposition,
    coerce_now,
    load_research_pack,
    source_effective_freshness,
)

SNAPSHOT_SCHEMA = "openmontage-editorial-snapshot-v1"

_REQUIRED_STATUS = "loaded"


def normalize_editorial_snapshot(
    loaded: Mapping[str, Any],
    *,
    now: object = None,
) -> dict[str, Any]:
    """把 ``load_research_pack`` 的结果投影为 normalized editorial snapshot（纯函数）。"""
    if not isinstance(loaded, Mapping):
        raise ResearchPackError("normalize_editorial_snapshot 需要 load_research_pack 的结果字典")
    if loaded.get("status") != _REQUIRED_STATUS:
        raise ResearchPackError(
            f"只能投影已校验通过的包（status={loaded.get('status')!r}）；技术无效的包不产生快照"
        )
    payloads = loaded.get("payloads")
    if not isinstance(payloads, Mapping):
        raise ResearchPackError("load 结果缺少 payloads")

    detail = loaded.get("disposition_detail")
    if not isinstance(detail, Mapping):
        detail = assess_disposition(payloads, now=now)
    moment = coerce_now(now if now is not None else detail.get("checked_at"))

    episode = payloads["episode.json"]
    topics = payloads["topics.json"]

    sources: list[dict[str, Any]] = []
    for source in payloads["sources.json"].get("sources") or []:
        effective = source_effective_freshness(source, moment)
        sources.append(
            {
                "source_id": str(source.get("source_id") or ""),
                "authority": str(source.get("authority") or ""),
                "verification_state": str(source.get("verification_state") or ""),
                "heat_only": bool(source.get("heat_only")),
                "fact_source": (
                    not bool(source.get("heat_only"))
                    and source.get("authority") != "heat_only"
                    and source.get("verification_state") == "verified"
                ),
                "freshness": dict(source.get("freshness") or {}),
                "effective_freshness": effective,
                "publisher": str(source.get("publisher") or ""),
                "title": str(source.get("title") or ""),
                "url": str(source.get("url") or ""),
            }
        )

    claims: list[dict[str, Any]] = []
    for claim in payloads["claims.json"].get("claims") or []:
        claims.append(
            {
                "claim_id": str(claim.get("claim_id") or ""),
                "evidence_status": str(claim.get("evidence_status") or ""),
                "fact_ready": str(claim.get("evidence_status") or "") in FACT_READY,
                "wording_policy": str(claim.get("wording_policy") or ""),
                "freshness_requirement": str(claim.get("freshness_requirement") or ""),
                "text": str(claim.get("text") or ""),
                "claims_to_verify": list(claim.get("claims_to_verify") or []),
                "do_not_claim": list(claim.get("do_not_claim") or []),
                "material_refs": [str(ref) for ref in claim.get("material_refs") or []],
                "source_ids": [str(ref) for ref in claim.get("source_ids") or []],
                "fact_sources_present": int(claim.get("fact_sources_present") or 0),
                "fact_sources_min": int(claim.get("fact_sources_min") or 0),
            }
        )

    materials: list[dict[str, Any]] = []
    for material in payloads["materials.json"].get("materials") or []:
        materials.append(
            {
                "material_id": str(material.get("material_id") or ""),
                "kind": str(material.get("kind") or ""),
                "origin": dict(material.get("origin") or {}),
                "permitted_use": str(material.get("permitted_use") or ""),
                "freshness_status": str(material.get("freshness_status") or ""),
                "duration_ms": int(material.get("duration_ms") or 0),
                "segments": [
                    {
                        "segment_id": str(segment.get("segment_id") or ""),
                        "start_ms": int(segment.get("start_ms") or 0),
                        "end_ms": int(segment.get("end_ms") or 0),
                        "claim_ids": [str(ref) for ref in segment.get("claim_ids") or []],
                        "purpose": str(segment.get("purpose") or ""),
                        "transcript_excerpt": str(segment.get("transcript_excerpt") or ""),
                        "frame_evidence_ids": list(segment.get("frame_evidence_ids") or []),
                    }
                    for segment in material.get("segments") or []
                ],
            }
        )

    gaps = list(detail.get("gaps") or [])
    critical_gaps = [gap for gap in gaps if gap.get("critical")]
    non_critical_gaps = [gap for gap in gaps if not gap.get("critical")]
    if critical_gaps:
        gate = "research_required"
    elif gaps:
        gate = "partial"
    else:
        gate = "pass"

    return {
        "schema": SNAPSHOT_SCHEMA,
        "contract": str(episode.get("contract") or CONTRACT),
        "episode_id": str(loaded.get("episode_id") or episode.get("episode_id") or ""),
        "business_date": str(loaded.get("business_date") or episode.get("business_date") or ""),
        "theme": str(episode.get("theme") or ""),
        "production_mode": str(loaded.get("production_mode") or episode.get("production_mode") or ""),
        "revision": int(loaded.get("revision") or 0),
        "pack_id": str(loaded.get("pack_id") or ""),
        "content_sha256": str(loaded.get("content_sha256") or ""),
        "manifest_sha256": str(loaded.get("manifest_sha256") or ""),
        "disposition": str(loaded.get("disposition") or detail.get("disposition") or ""),
        "producer_disposition": str(loaded.get("producer_disposition") or ""),
        "disposition_reason": str(loaded.get("disposition_reason") or detail.get("reason") or ""),
        "partial_package": bool(loaded.get("partial_package")),  # 运输损坏已在校验层抛错，此处恒 False
        "keywords": dict(topics.get("keyword_graph") or {}),
        "topic_candidates": list(topics.get("topic_candidates") or []),
        "selected_topic": topics.get("selected_topic"),
        "argument_graph": dict(topics.get("argument_graph") or {}),
        "sources": sources,
        "claims": claims,
        "materials": materials,
        "rights": [dict(record) for record in payloads["rights.json"].get("rights") or []],
        "audience": dict(payloads["audience.json"].get("audience") or {}),
        "freshness_gates": {
            "gate": gate,
            "critical_gaps": critical_gaps,
            "non_critical_gaps": non_critical_gaps,
            "checked_at": str(detail.get("checked_at") or ""),
        },
        "product_gate": list(detail.get("product_gate") or []),
        "counts": dict(loaded.get("counts") or {}),
        "verified_files": list(loaded.get("verified_files") or []),
    }


def load_editorial_snapshot(
    source: str | Path,
    *,
    now: object = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """读取并投影一期研究包（严格校验 + 归一化），一步到位。

    ``source`` 可以是 episode 根目录，也可以是其中的 ``current.json``。
    技术无效/读取不稳定时抛 :class:`ResearchPackError` 子类——本函数不返回
    半可信快照。
    """
    kwargs: dict[str, Any] = {"now": now}
    if max_attempts is not None:
        kwargs["max_attempts"] = max_attempts
    loaded = load_research_pack(Path(source), **kwargs)
    return normalize_editorial_snapshot(loaded, now=now)
