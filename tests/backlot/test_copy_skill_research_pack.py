"""episode-research-pack-v1 只读校验器与快照的离线测试。

全部用 ``tmp_path`` 构造 golden 包；不触网、不依赖真实 CopySkill runs。
golden 包与生产端 ``episode_research_pack.py`` 逐字段同构。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backlot import copy_skill_research_pack as rp
from backlot import research_pack_snapshot as rps

CONTRACT = "episode-research-pack-v1"
MANIFEST_SCHEMA = "episode-research-pack-manifest-v1"
BUSINESS_DATE = "2026-09-16"
EPISODE_ID = f"{BUSINESS_DATE}-测试主题"


# --- 独立实现的 canonical / 分帧 hash（不调用被测代码，避免自证）-------------


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _framed(name: str, payload: object) -> bytes:
    canonical = _canonical(payload).encode("utf-8")
    return name.encode("utf-8") + b"\x00" + str(len(canonical)).encode("ascii") + b"\x00" + canonical + b"\x00"


def _csha(payloads: dict) -> str:
    digest = hashlib.sha256()
    for name in rp.SEMANTIC_FILES:
        digest.update(_framed(name, payloads[name]))
    return digest.hexdigest()


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(str(value), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _clear_dir(path: Path) -> None:
    """重建同名包目录（生产端不可变，测试里需要覆盖以模拟 revision 变化）。"""
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def _identity(episode_id: str, business_date: str) -> dict:
    return {"contract": CONTRACT, "episode_id": episode_id, "business_date": business_date}


def _rewrite_revision_hash_consistently(pack: Path, wrong: str) -> None:
    """把 revision/_READY 记录的 content_sha256 改成错误值，但**重建 manifest** 让其自洽。

    用于区分两类失败：字节被篡改（manifest 逐文件 SHA 不符 → 运输损坏） vs
    每个文件都自洽、只是记录的语义 hash 不一致（→ 合同违规）。
    """
    revision = _read(pack / "revision.json")
    revision["content_sha256"] = wrong
    _write(pack / "revision.json", revision)

    manifest = _read(pack / "package-manifest.json")
    listed = sorted(p.relative_to(pack).as_posix() for p in pack.rglob("*") if p.is_file())
    manifest["files"] = [
        {"path": rel, "bytes": (pack / rel).stat().st_size, "sha256": _sha(pack / rel)}
        for rel in listed
        if rel not in ("package-manifest.json", "_READY.json")
    ]
    _write(pack / "package-manifest.json", manifest)

    ready = _read(pack / "_READY.json")
    ready["content_sha256"] = wrong
    ready["file_count"] = len(manifest["files"])
    ready["manifest_sha256"] = _sha(pack / "package-manifest.json")
    _write(pack / "_READY.json", ready)


def default_payloads(
    *,
    variant: str = "a",
    episode_id: str = EPISODE_ID,
    business_date: str = BUSINESS_DATE,
    disposition: str = "ready",
) -> dict:
    identity = _identity(episode_id, business_date)
    episode = {
        **identity,
        "production_mode": "single_topic_material_replication",
        "theme": "测试主题",
        "disposition": disposition,
        "keywords": ["测试"],
        "origin_ref": {
            "origin_contract": "material_replication_delivery",
            "origin_pack_id": None,
            "origin_item_id": None,
            "delivery_folder": f"{business_date}-测试主题复刻视频",
            "manifest_path": "清单.json",
            "manifest_sha256": "a" * 64,
            "asset_path": None,
            "asset_sha256": None,
            "asset_bytes": None,
            "authority": "material_only",
            "discovery_only": True,
            "items": [],
            "assets": [],
        },
        "warnings": [],
    }
    sources = {
        **identity,
        "sources": [
            {
                "source_id": "s1",
                "authority": "official",
                "verification_state": "verified",
                "heat_only": False,
                "freshness": {
                    "observed_at": f"{business_date}T10:00:00+08:00",
                    "policy": "event_window",
                    "status_at_publish": "fresh",
                },
                "publisher": "官方发布",
                "title": "官方消息",
                "url": "https://example.com/news",
            }
        ],
    }
    claims = {
        **identity,
        "claims": [
            {
                "claim_id": "c1",
                "evidence_status": "confirmed_official",
                "wording_policy": "assert",
                "freshness_requirement": "fresh",
                "text": f"第一手事实陈述-{variant}",
                "claims_to_verify": [],
                "do_not_claim": [],
                "material_refs": ["m1"],
                "source_ids": ["s1"],
                "fact_sources_present": 1,
                "fact_sources_min": 1,
            }
        ],
    }
    audience = {**identity, "audience": {"status": "known", "summary": "", "segments": []}}
    topics = {
        **identity,
        "keyword_graph": {
            "seed": "测试主题",
            "expanded": [],
            "subject_terms": [],
            "event_terms": [],
            "keywords_requested": [],
            "keywords_used": ["测试"],
            "keywords_truncated": False,
        },
        "topic_candidates": [
            {
                "topic_id": "topic-01",
                "title": "测试主题",
                "keywords": ["测试"],
                "selection_basis": "single_theme",
                "producer_proposal": True,
            }
        ],
        "selected_topic": {"topic_id": "topic-01", "selection_basis": "single_theme", "producer_proposal": True},
        # 与生产端 ``episode_research_pack.py:666-678`` 的 ``default_topics`` 逐项同构：
        # ``topics.json`` 是 baseline **原样透传**，生产端全 ``src/`` 只有 ``:676`` 一处写
        # ``nodes``，且硬编码 ``[]`` ⇒ **真实包的 nodes 恒为空**。
        # 这里曾默认塞一个 c1 节点，与生产端不同构，于是「消费端偷偷要求 nodes 非空」这类
        # 越合同约束在夹具上永远看不见（2026-09-16 教训）。需要「有论证」形态的用例必须
        # 显式调用 ``default_argument_node()`` / ``_node()`` 取节点，不许白拿默认值。
        "argument_graph": {"topic_id": "topic-01", "nodes": [], "edges": []},
    }
    materials = {
        **identity,
        "materials": [
            {
                "material_id": "m1",
                "kind": "b_roll",
                "origin": {
                    "video_id": "v1",
                    "author": "作者",
                    "title": "素材",
                    "share_url": "https://www.douyin.com/video/v1",
                },
                "permitted_use": "b_roll_only",
                "freshness_status": "unknown",
                "duration_ms": 60000,
                "segments": [
                    {
                        "segment_id": "seg1",
                        "start_ms": 0,
                        "end_ms": 10000,
                        "claim_ids": ["c1"],
                        "purpose": "visual_support",
                        "transcript_excerpt": "",
                        "frame_evidence_ids": [],
                    }
                ],
            }
        ],
    }
    rights = {
        **identity,
        "rights": [
            {
                "asset_id": "asset-m1",
                "asset_type": "video",
                "origin": "platform_content",
                "rights_status": "review_required",
                "license": None,
                "attribution": None,
                "redistribution_allowed": False,
                "render_eligible": False,
                "review_reason": "抖音素材仅为发现与关注度证据；需人工复核权利。",
                "material_id": "m1",
            }
        ],
    }
    return {
        "episode.json": episode,
        "sources.json": sources,
        "claims.json": claims,
        "audience.json": audience,
        "topics.json": topics,
        "materials.json": materials,
        "rights.json": rights,
    }


def build_pack(
    root: Path,
    *,
    revision: int = 1,
    variant: str = "a",
    business_date: str = BUSINESS_DATE,
    episode_id: str = EPISODE_ID,
    payloads: dict | None = None,
    mutate=None,
    honor_payload_identity: bool = False,
) -> tuple[Path, Path]:
    """落盘一个与生产端逐字节同构的包，返回 (episode_root, pack_dir)。"""
    episode_root = root / f"{business_date}_研究包" / episode_id
    pack_id = f"{episode_id}-r{revision}"
    pack = episode_root / "packs" / pack_id
    _clear_dir(pack)

    semantic = payloads if payloads is not None else default_payloads(
        variant=variant, episode_id=episode_id, business_date=business_date
    )
    # 包内身份必须与目录/current 一致，否则 contract 校验会（正确地）拒绝
    semantic = {name: json.loads(json.dumps(payload)) for name, payload in semantic.items()}
    if not honor_payload_identity:
        for name in rp.SEMANTIC_FILES:
            semantic[name]["contract"] = CONTRACT
            semantic[name]["episode_id"] = episode_id
            semantic[name]["business_date"] = business_date

    for name in rp.SEMANTIC_FILES:
        _write(pack / name, semantic[name])

    csha = _csha(semantic)
    disposition = semantic["episode.json"]["disposition"]
    _write(pack / "revision.json", {
        "contract": CONTRACT,
        "episode_id": episode_id,
        "business_date": business_date,
        "revision": revision,
        "pack_id": pack_id,
        "content_sha256": csha,
        "disposition": disposition,
        "generated_at": f"{business_date}T23:00:00+08:00",
        "supersedes": None,
        "change": "initial",
    })
    _write(pack / "run-report.json", {
        "contract": CONTRACT,
        "episode_id": episode_id,
        "business_date": business_date,
        "revision": revision,
        "pack_id": pack_id,
        "generated_at": f"{business_date}T23:00:00+08:00",
        "disposition": disposition,
        "counts": {"sources": 1, "claims": 1, "topic_candidates": 1, "materials": 1, "rights": 1},
    })
    _write(pack / "每期研究证据包.md", f"# 每期研究证据包（{episode_id}）\n")

    listed = sorted(child.relative_to(pack).as_posix() for child in pack.rglob("*") if child.is_file())
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "contract": CONTRACT,
        "episode_id": episode_id,
        "business_date": business_date,
        "revision": revision,
        "pack_id": pack_id,
        "content_sha256": csha,
        "files": [{"path": rel, "bytes": (pack / rel).stat().st_size, "sha256": _sha(pack / rel)} for rel in listed],
        "exclusions": ["package-manifest.json", "_READY.json"],
    }
    _write(pack / "package-manifest.json", manifest)
    manifest_sha = _sha(pack / "package-manifest.json")
    _write(pack / "_READY.json", {
        "contract": CONTRACT,
        "episode_id": episode_id,
        "business_date": business_date,
        "revision": revision,
        "pack_id": pack_id,
        "content_sha256": csha,
        "manifest_sha256": manifest_sha,
        "file_count": len(manifest["files"]),
        "supersedes": None,
        "ready_at": f"{business_date}T23:00:00+08:00",
    })
    _write(episode_root / "current.json", {
        "contract": CONTRACT,
        "episode_id": episode_id,
        "business_date": business_date,
        "pack_id": pack_id,
        "revision": revision,
        "content_sha256": csha,
        "manifest_sha256": manifest_sha,
        "disposition": disposition,
        "pack_path": f"packs/{pack_id}",
        "ready_path": f"packs/{pack_id}/_READY.json",
        "updated_at": f"{business_date}T23:00:00+08:00",
    })
    (episode_root / "latest.json").write_text('{"decoy": true}\n', encoding="utf-8")
    if mutate is not None:
        mutate({"episode_root": episode_root, "pack": pack, "current": episode_root / "current.json"})
    return episode_root, pack


# --- 正常包 ----------------------------------------------------------------


def test_valid_pack_loads_and_matches_contract(tmp_path: Path):
    episode_root, pack = build_pack(tmp_path / "root")
    result = rp.load_research_pack(episode_root)

    assert result["status"] == "loaded"
    assert result["episode_id"] == EPISODE_ID
    assert result["business_date"] == BUSINESS_DATE
    assert result["production_mode"] == "single_topic_material_replication"
    assert result["revision"] == 1
    assert result["pack_id"] == f"{EPISODE_ID}-r1"
    assert result["disposition"] == "ready"
    assert result["producer_disposition"] == "ready"
    assert result["partial_package"] is False
    assert result["content_sha256"] == _csha(default_payloads())
    listed = {row["path"] for row in result["verified_files"]}
    assert "package-manifest.json" not in listed and "_READY.json" not in listed
    assert "episode.json" in listed


def test_content_sha256_is_framed_and_order_independent():
    payloads = default_payloads()
    shuffled = {name: json.loads(json.dumps(payloads[name])) for name in reversed(rp.SEMANTIC_FILES)}
    assert rp.content_sha256(payloads) == rp.content_sha256(shuffled)
    assert rp.content_sha256(payloads) == _csha(payloads)
    # 分帧编码：简单拼接会产生不同 hash，故必须与独立实现一致
    naive = hashlib.sha256(
        b"".join(_canonical(payloads[name]).encode("utf-8") for name in rp.SEMANTIC_FILES)
    ).hexdigest()
    assert rp.content_sha256(payloads) != naive


def test_never_reads_staging_or_root_latest(tmp_path: Path):
    episode_root, pack = build_pack(tmp_path / "root")
    (episode_root / ".staging" / "x").mkdir(parents=True)
    (episode_root / ".staging" / "x" / "current.json").write_text('{"revision": 99}\n', encoding="utf-8")
    result = rp.load_research_pack(episode_root)
    assert result["revision"] == 1
    assert result["pack_dir"] == str(pack)


# --- manifest 完整性 -------------------------------------------------------


@pytest.mark.parametrize("failure", ["missing_entry", "extra_entry", "bad_bytes", "bad_hash", "extra_file"])
def test_manifest_completeness_failures_are_transport_damage(tmp_path: Path, failure: str):
    """manifest 双向覆盖 / bytes / SHA 失败 = 技术/运输损坏，不是合同违规。"""
    def mutate(ctx):
        manifest_path = ctx["pack"] / "package-manifest.json"
        manifest = _read(manifest_path)
        if failure == "missing_entry":
            manifest["files"] = [row for row in manifest["files"] if row["path"] != "claims.json"]
        elif failure == "extra_entry":
            manifest["files"].append({"path": "ghost.json", "bytes": 0, "sha256": "0" * 64})
        elif failure == "bad_bytes":
            manifest["files"][0]["bytes"] = 1
        elif failure == "bad_hash":
            manifest["files"][0]["sha256"] = "0" * 64
        else:
            (ctx["pack"] / "stray.txt").write_text("x\n", encoding="utf-8")
            return
        _write(manifest_path, manifest)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackPartialError) as excinfo:
        rp.load_research_pack(episode_root)
    assert excinfo.value.code == rp.PARTIAL_PACKAGE


@pytest.mark.parametrize(
    "bad_path",
    ["../evil.json", "/etc/passwd", "C:/windows/x", "a\\b.json", "a//b.json", "./x.json", "a/../../b.json"],
)
def test_manifest_rejects_unsafe_paths(tmp_path: Path, bad_path: str):
    def mutate(ctx):
        manifest_path = ctx["pack"] / "package-manifest.json"
        manifest = _read(manifest_path)
        manifest["files"][0]["path"] = bad_path
        _write(manifest_path, manifest)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_manifest_may_not_list_self_or_ready(tmp_path: Path):
    def mutate(ctx):
        manifest_path = ctx["pack"] / "package-manifest.json"
        manifest = _read(manifest_path)
        manifest["files"].append({"path": "_READY.json", "bytes": 1, "sha256": "0" * 64})
        _write(manifest_path, manifest)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_manifest_schema_is_frozen(tmp_path: Path):
    def mutate(ctx):
        manifest_path = ctx["pack"] / "package-manifest.json"
        manifest = _read(manifest_path)
        manifest["schema"] = "something-else"
        _write(manifest_path, manifest)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_missing_required_file_is_transport_damage(tmp_path: Path):
    def mutate(ctx):
        (ctx["pack"] / "rights.json").unlink()

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackPartialError) as excinfo:
        rp.load_research_pack(episode_root)
    assert excinfo.value.code == rp.PARTIAL_PACKAGE


def test_truncated_json_is_transport_damage(tmp_path: Path):
    def mutate(ctx):
        (ctx["pack"] / "claims.json").write_text('{"contract": ', encoding="utf-8")

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackPartialError):
        rp.load_research_pack(episode_root)


# --- current / READY 一致性 -------------------------------------------------


def test_current_pack_path_escape_is_rejected(tmp_path: Path):
    def mutate(ctx):
        current = _read(ctx["current"])
        current["pack_path"] = "../../evil"
        _write(ctx["current"], current)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_current_absolute_pack_path_is_rejected(tmp_path: Path):
    def mutate(ctx):
        current = _read(ctx["current"])
        current["pack_path"] = "C:/tmp/pack"
        _write(ctx["current"], current)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_current_ready_path_must_match_pack(tmp_path: Path):
    def mutate(ctx):
        current = _read(ctx["current"])
        current["ready_path"] = "packs/other/_READY.json"
        _write(ctx["current"], current)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_ready_and_current_disagreement_is_rejected(tmp_path: Path):
    def mutate(ctx):
        ready = _read(ctx["pack"] / "_READY.json")
        ready["revision"] = 7
        _write(ctx["pack"] / "_READY.json", ready)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_revision_record_tamper_is_transport_damage(tmp_path: Path):
    # 篡改 manifest 收录的 revision.json → 逐文件字节完整性先失败 = 运输损坏
    def mutate(ctx):
        revision = _read(ctx["pack"] / "revision.json")
        revision["content_sha256"] = "0" * 64
        _write(ctx["pack"] / "revision.json", revision)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackPartialError) as excinfo:
        rp.load_research_pack(episode_root)
    assert excinfo.value.code == rp.PARTIAL_PACKAGE


def test_semantic_hash_disagreement_with_intact_bytes_is_invalid_contract(tmp_path: Path):
    # 每个文件都与 manifest 自洽，只有 revision/_READY 记录的 content_sha256 与语义集不符
    def mutate(ctx):
        _rewrite_revision_hash_consistently(ctx["pack"], "0" * 64)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_semantic_identity_contract_must_match(tmp_path: Path):
    payloads = default_payloads()
    payloads["claims.json"]["contract"] = "episode-research-pack-v2"
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads, honor_payload_identity=True)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_semantic_identity_episode_id_must_match(tmp_path: Path):
    payloads = default_payloads()
    payloads["audience.json"]["episode_id"] = "别的期"
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads, honor_payload_identity=True)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


# --- 语义：外键 / 时间码 / 权限门 ------------------------------------------


def test_claim_material_reverse_reference_is_enforced(tmp_path: Path):
    payloads = default_payloads()
    payloads["materials.json"]["materials"][0]["segments"][0]["claim_ids"] = []
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_segment_claim_id_without_reverse_ref_is_rejected(tmp_path: Path):
    payloads = default_payloads()
    payloads["claims.json"]["claims"][0]["material_refs"] = []
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


@pytest.mark.parametrize("start_ms,end_ms", [(5000, 5000), (10000, 3000), (0, 999999)])
def test_integer_timecode_bounds_are_enforced(tmp_path: Path, start_ms: int, end_ms: int):
    payloads = default_payloads()
    payloads["materials.json"]["materials"][0]["segments"][0].update({"start_ms": start_ms, "end_ms": end_ms})
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_non_integer_timecode_is_rejected(tmp_path: Path):
    payloads = default_payloads()
    payloads["materials.json"]["materials"][0]["segments"][0]["end_ms"] = 9500.5
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_duplicate_material_segment_pair_is_rejected(tmp_path: Path):
    payloads = default_payloads()
    segment = payloads["materials.json"]["materials"][0]["segments"][0]
    payloads["materials.json"]["materials"][0]["segments"].append(dict(segment))
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_permitted_use_must_be_b_roll_only(tmp_path: Path):
    payloads = default_payloads()
    payloads["materials.json"]["materials"][0]["permitted_use"] = "final_asset"
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_platform_content_must_not_be_render_eligible(tmp_path: Path):
    payloads = default_payloads()
    payloads["rights.json"]["rights"][0]["render_eligible"] = True
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_material_cannot_be_counted_as_fact_source(tmp_path: Path):
    payloads = default_payloads()
    payloads["claims.json"]["claims"][0]["source_ids"] = ["m1"]
    payloads["claims.json"]["claims"][0]["fact_sources_present"] = 0
    payloads["claims.json"]["claims"][0]["fact_sources_min"] = 0
    payloads["claims.json"]["claims"][0]["evidence_status"] = "unverified"
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_heat_only_source_cannot_be_fact_source(tmp_path: Path):
    payloads = default_payloads()
    payloads["sources.json"]["sources"][0].update(
        {"authority": "heat_only", "heat_only": True, "url": "https://www.douyin.com/video/123"}
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_fact_sources_present_must_match(tmp_path: Path):
    payloads = default_payloads()
    payloads["claims.json"]["claims"][0]["fact_sources_present"] = 3
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_unverified_claim_may_cite_unverified_sources(tmp_path: Path):
    """生产端 ``build_research_semantic`` 的真实形态：unverified 断言引用 pub-* 未核验来源。

    合同 Data constraints 只要求 ``claim.source_ids`` 存在于 ``sources``（「只能是 sources 的
    source_id」），且只对 ``confirmed_official`` / ``confirmed_two_reliable`` 复算
    ``fact_sources_*``；生产端自校验同款。曾经本层**无条件**要求引用已在事实源里，于是这条
    真实生产路径被直接判死。
    """
    payloads = default_payloads()
    payloads["sources.json"]["sources"][0].update(
        {"authority": "unknown", "verification_state": "unverified", "heat_only": False}
    )
    payloads["claims.json"]["claims"][0].update(
        {"evidence_status": "unverified", "fact_sources_present": 0, "fact_sources_min": 0}
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)

    loaded = rp.load_research_pack(episode_root)  # 不抛异常
    assert loaded["status"] == "loaded"


def test_confirmed_claim_still_requires_verified_fact_sources(tmp_path: Path):
    """放宽只作用于不要求事实源的 evidence_status；confirmed_* 仍必须指向已核验事实源。

    ★ 措辞随口径变过：事实源从「逐个 ref 必须已在事实源集合里」改成**按数量复算**
    （合同第 138 行「由 source 复算」）后，未核验来源不再是「逐-ref 被拒」，而是
    **不计入**已核验事实源数 —— confirmed_official 的 min=1 因此无法达成。
    禁止的语义一字未变，变的只是拒绝理由的表达位置，故断言改为钉住「quantity 口径」
    的实际文案（同时保留对 state 与 min 的断言，避免退化成只匹配一个宽泛词）。
    """
    payloads = default_payloads()
    payloads["sources.json"]["sources"][0].update(
        {"authority": "unknown", "verification_state": "unverified", "heat_only": False}
    )
    payloads["claims.json"]["claims"][0]["fact_sources_present"] = 0  # confirmed_official 仍要求 min=1
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)

    with pytest.raises(rp.ResearchPackValidationError) as excinfo:
        rp.load_research_pack(episode_root)
    message = str(excinfo.value)
    assert "confirmed_official" in message
    assert "已核验事实源仅 0 个" in message  # 未核验来源确实没被计入
    assert "少于要求的 1 个" in message


# --- confirmed_* 的 authority/publisher 复算（合同第 103-104 行）--------------


def _with_second_source(
    payloads: dict, *, authority: str, publisher: str, first_publisher: str | None = None
) -> dict:
    """补一条已核验事实源 ``s2``：复用 ``s1`` 的形状，只换 authority/publisher。

    ``first_publisher`` 用来把 ``s1`` 也改成同一家 —— 注意 ``s1`` 出厂 publisher 是
    「官方发布」，不显式覆盖的话「同 publisher」用例其实是两家，会假绿（2026-09-16 踩过）。
    """
    second = json.loads(json.dumps(payloads["sources.json"]["sources"][0]))
    second.update({"source_id": "s2", "authority": authority, "publisher": publisher})
    payloads["sources.json"]["sources"].append(second)
    if first_publisher is not None:
        payloads["sources.json"]["sources"][0]["publisher"] = first_publisher
    return payloads


def _as_confirmed_two_reliable(payloads: dict) -> dict:
    payloads["claims.json"]["claims"][0].update(
        {
            "evidence_status": "confirmed_two_reliable",
            "source_ids": ["s1", "s2"],
            "fact_sources_present": 2,
            "fact_sources_min": 2,
        }
    )
    return payloads


def test_confirmed_two_reliable_same_publisher_is_rejected(tmp_path: Path):
    """合同第 103-104 行：``confirmed_two_reliable`` 必须由 **≥2 个独立 publisher** 复算。

    ★ 纯数量口径（已核验源 ≥2）会放过「两条来自同一家」这种被夸大的证据等级，
    而这个等级经 ``research_pack_snapshot`` 直接决定编辑层的 ``fact_ready`` 门 ⇒
    必须在消费端按 publisher 去重后复算，不能信调用方写的字符串。
    """
    payloads = _as_confirmed_two_reliable(
        _with_second_source(
            default_payloads(), authority="official", publisher="同一家", first_publisher="同一家"
        )
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError) as excinfo:
        rp.load_research_pack(episode_root)
    assert "不足两个独立可靠来源" in str(excinfo.value)


def test_confirmed_official_requires_official_authority(tmp_path: Path):
    """合同第 103-104 行：``confirmed_official`` 的事实源里必须有 ``authority=official``。

    只有一条 ``reliable_independent`` 已核验源时数量下限（1）是满足的，纯数量口径会放行。
    """
    payloads = default_payloads()
    payloads["sources.json"]["sources"][0]["authority"] = "reliable_independent"
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError) as excinfo:
        rp.load_research_pack(episode_root)
    assert "没有官方已核验来源" in str(excinfo.value)


def test_confirmed_two_reliable_independent_publishers_pass(tmp_path: Path):
    """正向：``official`` + ``reliable_independent``、两个不同 publisher ⇒ 复算通过。"""
    payloads = _as_confirmed_two_reliable(
        _with_second_source(default_payloads(), authority="reliable_independent", publisher="另一家")
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    assert rp.load_research_pack(episode_root)["status"] == "loaded"


def test_product_gate_shares_the_fact_source_recomputation(tmp_path: Path):
    """输入门与产品门必须**共用同一份**复算：两处拒绝理由逐字相同。

    ``assess_disposition`` 不做输入校验（它直接吃 raw payloads），因此能单独触发产品门；
    若两处各写一套口径，这里的文案立刻分叉 —— 这是「一处实现、多处共用」的回归钉子。
    """
    payloads = _as_confirmed_two_reliable(
        _with_second_source(
            default_payloads(), authority="official", publisher="同一家", first_publisher="同一家"
        )
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError) as excinfo:
        rp.load_research_pack(episode_root)
    input_gate_reason = str(excinfo.value)

    assessed = rp.assess_disposition(payloads)
    assert assessed["disposition"] == "rejected"
    assert assessed["product_gate"] == [input_gate_reason]


def test_production_mode_is_frozen(tmp_path: Path):
    payloads = default_payloads()
    payloads["episode.json"]["production_mode"] = "daily_news"
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_origin_ref_authority_is_enforced(tmp_path: Path):
    payloads = default_payloads()
    payloads["episode.json"]["origin_ref"]["authority"] = "whatever"
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def _node(claim_id: str, dim: str = "mechanism") -> dict:
    return {"claim_id": claim_id, "dim": dim, "claim": f"节点 {claim_id}", "source_candidate_ids": []}


def default_argument_node(*, variant: str = "a", dim: str = "event_core") -> dict:
    """默认 claim（``claims.json`` 的 ``c1``）对应的论证节点，与旧夹具默认值逐字一致。

    ``default_payloads`` 现在与生产端同构（``nodes: []``），所以凡是需要「有论证」形态的
    用例都必须**显式**取这个节点 —— 不许再从默认值白拿。
    """
    return {
        "claim_id": "c1",
        "dim": dim,
        "claim": f"第一手事实陈述-{variant}",
        "source_candidate_ids": ["s1"],
    }


def with_argument_nodes(payloads: dict, *nodes: dict) -> dict:
    """显式把论证节点装进 ``topics.json.argument_graph``（就地修改并返回）。

    供本模块与其它测试模块统一使用：``with_argument_nodes(default_payloads(),
    default_argument_node())`` 就是旧的「夹具自带 c1 节点」形态，现在必须写明。
    """
    payloads["topics.json"]["argument_graph"]["nodes"] = [dict(node) for node in nodes]
    return payloads


def _graph_with_edge(*edges: dict, extra_nodes: list[dict] | None = None) -> dict:
    """构造带边的 ``argument_graph``：``c1`` 节点必须随边显式入图，其余由调用方给出。"""
    payloads = default_payloads()
    with_argument_nodes(payloads, default_argument_node(), *(extra_nodes or []))
    graph = payloads["topics.json"]["argument_graph"]
    graph["edges"] = [dict(edge) for edge in edges]
    return payloads


def test_fixture_argument_graph_is_isomorphic_with_producer_default():
    """夹具默认 ``argument_graph`` 必须与生产端 ``default_topics`` 逐项同构。

    生产端 ``episode_research_pack.py:676`` 硬编码 ``"nodes": []``，是全 ``src/`` 唯一写入点。
    本断言把「夹具偷偷给 nodes 塞节点」钉死 —— 那正是让消费端越合同的「nodes 非空」约束
    在 CI 里全绿潜伏的放大器。
    """
    graph = default_payloads()["topics.json"]["argument_graph"]
    assert graph == {"topic_id": "topic-01", "nodes": [], "edges": []}


def test_topics_argument_graph_edge_must_reference_nodes(tmp_path: Path):
    payloads = default_payloads()
    graph = payloads["topics.json"]["argument_graph"]
    # 只有 c1 入图：唯一缺陷是边指向了不存在的 c404。
    graph["nodes"] = [default_argument_node()]
    graph["edges"] = [{"from": "c1", "to": "c404", "relation": "supports"}]
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_all_frozen_dimensions_and_relations_are_accepted(tmp_path: Path):
    payloads = default_payloads()
    nodes = [
        {"claim_id": f"n{i}", "dim": dim, "claim": f"节点 {i}", "source_candidate_ids": []}
        for i, dim in enumerate(rp.ALLOWED_DIMENSIONS)
    ]
    # 五种 relation 全部作为合法边出现；causes/precedes 部分构成无环链
    edges = [
        {"from": f"n{i}", "to": f"n{i + 1}", "relation": relation}
        for i, relation in enumerate(rp.ALLOWED_EDGE_RELATIONS)
    ]
    payloads["topics.json"]["argument_graph"] = {
        "topic_id": "topic-01",
        "nodes": nodes,
        "edges": edges,
    }
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    assert rp.load_research_pack(episode_root)["status"] == "loaded"
    assert len(rp.ALLOWED_DIMENSIONS) == 13
    assert len(rp.ALLOWED_EDGE_RELATIONS) == 5


@pytest.mark.parametrize("bad_dim", ["事实", "unknown", "event-core", "EVENT_CORE", ""])
def test_unknown_dimension_is_rejected(tmp_path: Path, bad_dim: str):
    payloads = default_payloads()
    payloads["topics.json"]["argument_graph"]["nodes"] = [default_argument_node(dim=bad_dim)]
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


@pytest.mark.parametrize("bad_relation", ["implies", "SUPPORTS", "caused_by", ""])
def test_unknown_relation_is_rejected(tmp_path: Path, bad_relation: str):
    payloads = _graph_with_edge(
        {"from": "c1", "to": "c2", "relation": bad_relation}, extra_nodes=[_node("c2")]
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_edge_extra_key_is_rejected(tmp_path: Path):
    payloads = _graph_with_edge(
        {"from": "c1", "to": "c2", "relation": "supports", "weight": 1}, extra_nodes=[_node("c2")]
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_edge_missing_key_is_rejected(tmp_path: Path):
    payloads = _graph_with_edge(
        {"from": "c1", "relation": "supports"}, extra_nodes=[_node("c2")]
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_renamed_edge_keys_are_rejected(tmp_path: Path):
    # 跨仓冻结合同的边键就是 from/to/relation；from_claim_id/to_claim_id 不是本合同。
    # 2026-09-16 复核已否决该改名（理由见 backlot/copy_skill_research_pack.py 常量注释）。
    # 两套边键并存会让研究包在两仓之间无法互通，因此改名后的写法必须被拒绝。
    payloads = _graph_with_edge(
        {"from_claim_id": "c1", "to_claim_id": "c2", "relation": "supports"},
        extra_nodes=[_node("c2")],
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_self_loop_edge_is_rejected(tmp_path: Path):
    payloads = _graph_with_edge({"from": "c1", "to": "c1", "relation": "supports"})
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_duplicate_edge_triple_is_rejected(tmp_path: Path):
    edge = {"from": "c1", "to": "c2", "relation": "supports"}
    payloads = _graph_with_edge(edge, dict(edge), extra_nodes=[_node("c2")])
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_causes_cycle_is_rejected(tmp_path: Path):
    payloads = _graph_with_edge(
        {"from": "c1", "to": "c2", "relation": "causes"},
        {"from": "c2", "to": "c3", "relation": "causes"},
        {"from": "c3", "to": "c1", "relation": "causes"},
        extra_nodes=[_node("c2"), _node("c3")],
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_precedes_cycle_is_rejected(tmp_path: Path):
    payloads = _graph_with_edge(
        {"from": "c1", "to": "c2", "relation": "precedes"},
        {"from": "c2", "to": "c1", "relation": "precedes"},
        extra_nodes=[_node("c2")],
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    with pytest.raises(rp.ResearchPackValidationError):
        rp.load_research_pack(episode_root)


def test_non_acyclic_relations_may_form_a_cycle(tmp_path: Path):
    # 只有 causes/precedes 要求无环；supports/qualifies 互相支撑是合法的。
    payloads = _graph_with_edge(
        {"from": "c1", "to": "c2", "relation": "supports"},
        {"from": "c2", "to": "c1", "relation": "qualifies"},
        extra_nodes=[_node("c2")],
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    assert rp.load_research_pack(episode_root)["status"] == "loaded"


# --- current 双读 ----------------------------------------------------------


def test_current_changing_between_reads_is_retried_then_rejected(tmp_path: Path, monkeypatch):
    episode_root, _ = build_pack(tmp_path / "root")
    real = rp.read_json
    counter = {"n": 0}

    def flaky(path, *, label=None):
        payload = real(path, label=label)
        if Path(path).name == rp.CURRENT_NAME:
            counter["n"] += 1
            flipped = dict(payload)
            flipped["revision"] = ((counter["n"] - 1) % 2) + 1
            return flipped
        return payload

    monkeypatch.setattr(rp, "read_json", flaky)
    with pytest.raises(rp.ResearchPackUnstableError):
        rp.load_research_pack(episode_root, max_attempts=3)
    assert counter["n"] >= 4


def test_try_load_returns_failure_envelope(tmp_path: Path):
    envelope = rp.try_load_research_pack(tmp_path / "does-not-exist")
    assert envelope["status"] == "failed"
    assert envelope["error_code"] == "invalid_contract"


def test_current_is_read_twice_around_full_validation(tmp_path: Path, monkeypatch):
    """current-B 必须在 READY/manifest/七语义文件/全部 hash 之后才读。"""
    episode_root, _ = build_pack(tmp_path / "root")
    order: list[str] = []
    real = rp.read_json

    def spy(path, *, label=None):
        order.append(Path(path).name)
        return real(path, label=label)

    monkeypatch.setattr(rp, "read_json", spy)
    rp.load_research_pack(episode_root)

    assert order.count(rp.CURRENT_NAME) == 2
    first = order.index(rp.CURRENT_NAME)
    last = len(order) - 1 - order[::-1].index(rp.CURRENT_NAME)
    between = order[first + 1:last]
    assert rp.READY_NAME in between
    assert rp.MANIFEST_NAME in between
    for name in rp.SEMANTIC_FILES:
        assert name in between


def test_current_second_read_is_skipped_when_validation_fails(tmp_path: Path, monkeypatch):
    """校验失败时 current-B 从不发生：current 只被读一次。"""
    def mutate(ctx):
        manifest_path = ctx["pack"] / "package-manifest.json"
        manifest = _read(manifest_path)
        manifest["files"][0]["sha256"] = "0" * 64
        _write(manifest_path, manifest)

    episode_root, _ = build_pack(tmp_path / "root", mutate=mutate)
    order: list[str] = []
    real = rp.read_json

    def spy(path, *, label=None):
        order.append(Path(path).name)
        return real(path, label=label)

    monkeypatch.setattr(rp, "read_json", spy)
    with pytest.raises(rp.ResearchPackPartialError):
        rp.load_research_pack(episode_root)
    assert order.count(rp.CURRENT_NAME) == 1


# --- 处置重算：freshness / facts / rights 门 -------------------------------


def test_assess_maps_ready_to_ready():
    detail = rp.assess_disposition(default_payloads(), now="2026-09-16T23:00:00+08:00")
    assert detail["disposition"] == "ready"
    assert detail["gaps"] == []


def test_expired_critical_source_forces_research_required():
    payloads = default_payloads()
    payloads["sources.json"]["sources"][0]["freshness"]["valid_until"] = "2026-09-01T00:00:00+00:00"
    detail = rp.assess_disposition(payloads, now="2026-09-16T00:00:00+00:00")
    assert detail["disposition"] == "research_required"
    assert detail["reason"] == "critical_freshness_gap"


def test_unknown_freshness_on_non_critical_source_is_partial():
    payloads = default_payloads(disposition="partial")
    payloads["claims.json"]["claims"][0].update(
        {"evidence_status": "unverified", "source_ids": ["s1"], "fact_sources_present": 1, "fact_sources_min": 0}
    )
    payloads["sources.json"]["sources"][0]["freshness"]["status_at_publish"] = "unknown"
    detail = rp.assess_disposition(payloads, now="2026-09-16T00:00:00+00:00")
    assert detail["disposition"] == "partial"
    assert detail["reason"] == "non_critical_gaps"


def test_cleared_rights_on_platform_content_is_rejected():
    payloads = default_payloads()
    payloads["rights.json"]["rights"][0]["rights_status"] = "cleared"
    detail = rp.assess_disposition(payloads, now="2026-09-16T00:00:00+00:00")
    assert detail["disposition"] == "rejected"
    assert detail["reason"] == "product_or_rights_gate"


def test_declared_ready_without_first_hand_facts_downgrades():
    payloads = default_payloads()
    payloads["sources.json"]["sources"] = []
    payloads["claims.json"]["claims"] = []
    payloads["materials.json"]["materials"][0]["segments"][0]["claim_ids"] = []
    detail = rp.assess_disposition(payloads, now="2026-09-16T00:00:00+00:00")
    assert detail["disposition"] == "research_required"
    assert detail["reason"] == "facts_gate_not_met"


def test_partial_disposition_is_distinct_from_partial_package(tmp_path: Path):
    # 生产端声明 partial、包结构完整、无 freshness 缺口：
    # disposition=partial 但 partial_package=False —— 两个维度互不耦合。
    payloads = default_payloads(disposition="partial")
    payloads["claims.json"]["claims"][0].update(
        {"evidence_status": "unverified", "source_ids": ["s1"], "fact_sources_present": 1, "fact_sources_min": 0}
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    result = rp.load_research_pack(episode_root, now="2026-09-16T00:00:00+00:00")
    assert result["disposition"] == "partial"
    assert result["partial_package"] is False

    # 非关键 freshness 缺口同样只降 disposition，绝不置 partial_package
    gap_payloads = default_payloads(disposition="partial")
    gap_payloads["claims.json"]["claims"][0].update(
        {"evidence_status": "unverified", "source_ids": ["s1"], "fact_sources_present": 1, "fact_sources_min": 0}
    )
    gap_payloads["sources.json"]["sources"][0]["freshness"]["status_at_publish"] = "unknown"
    episode_root2, _ = build_pack(tmp_path / "root2", payloads=gap_payloads)
    gapped = rp.load_research_pack(episode_root2, now="2026-09-16T00:00:00+00:00")
    assert gapped["disposition"] == "partial"
    assert gapped["partial_package"] is False

    # 关键 freshness 缺口 → research_required，同样不改 partial_package
    critical_payloads = default_payloads()
    critical_payloads["sources.json"]["sources"][0]["freshness"]["valid_until"] = "2026-09-01T00:00:00+00:00"
    episode_root3, _ = build_pack(tmp_path / "root3", payloads=critical_payloads)
    critical = rp.load_research_pack(episode_root3, now="2026-09-16T00:00:00+00:00")
    assert critical["disposition"] == "research_required"
    assert critical["partial_package"] is False

    # 控制：完整 ready 包
    episode_root4, _ = build_pack(tmp_path / "root4")
    assert rp.load_research_pack(episode_root4, now="2026-09-16T00:00:00+00:00")["partial_package"] is False


# --- 快照 ------------------------------------------------------------------


def test_snapshot_copies_machine_files_and_writes_own_manifest(tmp_path: Path):
    episode_root, pack = build_pack(tmp_path / "root")
    result = rp.load_research_pack(episode_root)
    snapshot = rp.snapshot_research_pack(result, tmp_path / "snapshots")
    snapshot_dir = Path(snapshot["snapshot_dir"])
    assert snapshot["reused"] is False
    assert (snapshot_dir / "snapshot.json").is_file()
    manifest = _read(snapshot_dir / "snapshot-manifest.json")
    assert manifest["content_sha256"] == result["content_sha256"]
    listed = {row["path"] for row in manifest["files"]}
    assert "pack/episode.json" in listed and "pack/_READY.json" in listed
    for row in manifest["files"]:
        target = snapshot_dir / row["path"]
        assert target.stat().st_size == row["bytes"]
        assert _sha(target) == row["sha256"]
    descriptor = _read(snapshot_dir / "snapshot.json")
    assert descriptor["external_refs"][0]["copied"] is False
    assert descriptor["external_refs"][0]["availability"] == "external_reference_only"
    assert not (snapshot_dir / "pack" / "materials" / "m1.mp4").exists()


def test_snapshot_is_idempotent_for_same_content(tmp_path: Path):
    episode_root, _ = build_pack(tmp_path / "root")
    result = rp.load_research_pack(episode_root)
    first = rp.snapshot_research_pack(result, tmp_path / "snapshots")
    second = rp.snapshot_research_pack(result, tmp_path / "snapshots")
    assert second["reused"] is True
    assert first["snapshot_dir"] == second["snapshot_dir"]


def test_snapshot_rejects_conflicting_existing_dir(tmp_path: Path):
    episode_root, _ = build_pack(tmp_path / "root")
    result = rp.load_research_pack(episode_root)
    snapshot_dir = Path(rp.snapshot_research_pack(result, tmp_path / "snapshots")["snapshot_dir"])
    _write(snapshot_dir / "snapshot.json", {"content_sha256": "different"})
    with pytest.raises(rp.ResearchPackValidationError):
        rp.snapshot_research_pack(result, tmp_path / "snapshots")


def test_snapshot_does_not_touch_producer_directory(tmp_path: Path):
    episode_root, pack = build_pack(tmp_path / "root")
    before = {p: _sha(p) for p in pack.rglob("*") if p.is_file()}
    result = rp.load_research_pack(episode_root)
    rp.snapshot_research_pack(result, tmp_path / "snapshots")
    after = {p: _sha(p) for p in pack.rglob("*") if p.is_file()}
    assert before == after


# --- normalized editorial snapshot（编辑层对接面）----------------------------


def test_normalize_editorial_snapshot_shape(tmp_path: Path):
    payloads = default_payloads()
    payloads["topics.json"]["argument_graph"]["nodes"] = [default_argument_node()]
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    loaded = rp.load_research_pack(episode_root)
    snap = rps.normalize_editorial_snapshot(loaded, now="2026-09-16T23:00:00+08:00")

    assert snap["schema"] == "openmontage-editorial-snapshot-v1"
    assert snap["contract"] == CONTRACT
    assert snap["disposition"] == "ready"
    assert snap["partial_package"] is False
    # 主题面
    assert snap["keywords"]["seed"] == "测试主题"
    assert snap["selected_topic"]["topic_id"] == "topic-01"
    assert snap["argument_graph"]["nodes"][0]["claim_id"] == "c1"
    # 证据面
    assert snap["claims"][0]["evidence_status"] == "confirmed_official"
    assert snap["claims"][0]["fact_ready"] is True
    assert snap["claims"][0]["source_ids"] == ["s1"]
    assert snap["claims"][0]["material_refs"] == ["m1"]
    assert snap["sources"][0]["fact_source"] is True
    assert snap["sources"][0]["effective_freshness"] == "fresh"
    # 素材面（整数毫秒）
    segment = snap["materials"][0]["segments"][0]
    assert (segment["start_ms"], segment["end_ms"]) == (0, 10000)
    assert segment["claim_ids"] == ["c1"]
    assert snap["materials"][0]["duration_ms"] == 60000
    # 门
    assert snap["freshness_gates"]["gate"] == "pass"
    assert snap["product_gate"] == []


def test_load_editorial_snapshot_end_to_end(tmp_path: Path):
    episode_root, _ = build_pack(tmp_path / "root")
    snap = rps.load_editorial_snapshot(episode_root, now="2026-09-16T23:00:00+08:00")
    assert snap["episode_id"] == EPISODE_ID
    assert snap["revision"] == 1
    assert snap["disposition"] == "ready"


def test_normalize_projects_empty_argument_graph_of_real_producer_shape(tmp_path: Path):
    """真实生产形态（``nodes: []``）必须能一路投影成 snapshot，不得被投影层悄悄塞节点。

    这是「夹具与生产端同构」在投影层的对应断言：夹具默认已与 ``episode_research_pack.py:676``
    一致，故这里直接复用默认包。
    """
    episode_root, _ = build_pack(tmp_path / "root")
    snap = rps.load_editorial_snapshot(episode_root, now="2026-09-16T23:00:00+08:00")
    assert snap["argument_graph"]["nodes"] == []
    assert snap["argument_graph"]["edges"] == []
    assert snap["argument_graph"]["topic_id"] == "topic-01"
    assert snap["claims"][0]["claim_id"] == "c1"  # 断言仍在，只是没有对应论证节点


def test_normalize_rejects_unvalidated_envelope():
    with pytest.raises(rp.ResearchPackError):
        rps.normalize_editorial_snapshot({"status": "failed", "error": "boom"})


def test_snapshot_gate_marks_critical_freshness_gap(tmp_path: Path):
    payloads = default_payloads()
    payloads["sources.json"]["sources"][0]["freshness"]["valid_until"] = "2026-09-01T00:00:00+00:00"
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    snap = rps.load_editorial_snapshot(episode_root, now="2026-09-16T00:00:00+00:00")
    assert snap["disposition"] == "research_required"
    assert snap["freshness_gates"]["gate"] == "research_required"
    assert snap["freshness_gates"]["critical_gaps"][0]["source_id"] == "s1"
    assert snap["sources"][0]["effective_freshness"] == "expired"


def test_snapshot_does_not_upgrade_partial_to_ready(tmp_path: Path):
    payloads = default_payloads(disposition="partial")
    payloads["claims.json"]["claims"][0].update(
        {"evidence_status": "unverified", "source_ids": ["s1"], "fact_sources_present": 1, "fact_sources_min": 0}
    )
    episode_root, _ = build_pack(tmp_path / "root", payloads=payloads)
    snap = rps.load_editorial_snapshot(episode_root, now="2026-09-16T00:00:00+00:00")
    assert snap["disposition"] == "partial"
    assert snap["claims"][0]["fact_ready"] is False
    assert snap["freshness_gates"]["gate"] == "pass"
    assert snap["partial_package"] is False


# --- 跨仓钉定：与真实 producer 同 hash / 端到端互操作 -----------------------

#: producer ``episode_research_pack.py::FROZEN_CONTENT_SHA256`` 的离线副本。
#: 拿不到 producer 仓库时，这条也能钉死「分帧 canonical」算法不漂移。
PRODUCER_FROZEN_CONTENT_SHA256 = "e5bc839ff60a91f29474c9bec50c8afca41fdaf367dc5e6978eae620b1cbaab8"
PRODUCER_TEST_VECTOR_PAYLOADS = {
    "episode.json": {"a": 1},
    "sources.json": {"b": [1, 2]},
    "claims.json": {"c": "文"},
    "audience.json": {"d": None},
    "topics.json": {"e": True},
    "materials.json": {"f": 0},
    "rights.json": {"g": {"h": "i"}},
}


def test_content_sha256_pins_producer_frozen_vector():
    assert rp.content_sha256(PRODUCER_TEST_VECTOR_PAYLOADS) == PRODUCER_FROZEN_CONTENT_SHA256


def _producer_module():
    import importlib
    import sys

    from lib.paths import REPO_ROOT

    src = Path(REPO_ROOT).parent / "copy_skill" / "copy_skill-main" / "src"
    if not (src / "douyin_intelligence" / "episode_research_pack.py").is_file():
        pytest.skip("CopySkill producer 仓库不在本机，跳过跨仓互操作")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return importlib.import_module("douyin_intelligence.episode_research_pack")


def test_producer_frozen_constant_still_matches_algorithm():
    producer = _producer_module()
    vector = producer.content_sha256_test_vector()
    assert producer.FROZEN_CONTENT_SHA256 == vector["content_sha256"]
    assert producer.FROZEN_CONTENT_SHA256 == PRODUCER_FROZEN_CONTENT_SHA256


def test_end_to_end_with_real_producer_module(tmp_path: Path):
    """真实 producer 发布 → Haike 校验/快照/Intake，全链路字节级一致。"""
    producer = _producer_module()
    payloads = default_payloads()
    out = tmp_path / "producer-out"
    result = producer.publish_episode_research_pack(
        {},
        episode_id=EPISODE_ID,
        semantic=payloads,
        business_date=BUSINESS_DATE,
        output_root=str(out),
        clock=lambda: f"{BUSINESS_DATE}T23:00:00+08:00",
    )
    assert result["status"] == "published"
    assert result["content_sha256"] == producer.content_sha256(payloads)
    assert result["content_sha256"] == _csha(payloads)

    episode_root = out / f"{BUSINESS_DATE}{producer.EPISODE_SUFFIX}" / EPISODE_ID
    loaded = rp.load_research_pack(episode_root, now=f"{BUSINESS_DATE}T23:30:00+08:00")
    assert loaded["status"] == "loaded"
    assert loaded["content_sha256"] == result["content_sha256"]
    assert loaded["partial_package"] is False

    snap = rp.snapshot_research_pack(loaded, tmp_path / "snaps")
    assert Path(snap["snapshot_dir"], "snapshot.json").is_file()

    from backlot import research_pack_intake as rpi

    summary = rpi.reconcile(
        episode_root, ledger_path=tmp_path / "ledger.json", snapshot_root=tmp_path / "snaps2"
    )
    assert summary["counts"]["admitted"] == 1
    assert summary["results"][0]["partial_package"] is False


# ---------------------------------------------------------------------------
# 跨仓互通守卫（2026-09-16 教训）
# ---------------------------------------------------------------------------


def test_cross_repo_edge_keys_agree_with_frozen_contract():
    """边键集必须在「冻结合同文档 / 生产端 / 消费端」三处逐字一致。

    2026-09-16 教训：生产端被一条写进冻结合同、实际并不存在的「主控裁定」改成
    ``from_claim_id`` / ``to_claim_id``，消费端同步跟进，两边"自洽"却与合同相反 ——
    后果是任何真实含边的研究包都会在消费端报 ``invalid_contract``，而两侧测试全绿。
    本断言让「双端同步跑偏」立刻失败，而不是等真实跨仓发布才暴露。
    """
    producer = _producer_module()

    assert set(producer.EDGE_KEYS) == {"from", "to", "relation"}
    assert set(rp.EDGE_KEYS) == {"from", "to", "relation"}
    assert frozenset(producer.EDGE_KEYS) == frozenset(rp.EDGE_KEYS)

    from lib.paths import REPO_ROOT

    contract = (
        Path(REPO_ROOT).parent
        / "copy_skill"
        / "copy_skill-main"
        / "docs"
        / "tasks"
        / "2026-09-16-episode-research-pack-v1.md"
    )
    if not contract.is_file():
        pytest.skip("CopySkill 冻结合同文档不在本机，跳过合同一致性断言")
    text = contract.read_text(encoding="utf-8")
    # 冻结合同只描述接口，不承载过程叙述（裁定记录写在 handoff 与代码注释里）。
    assert "edges[{from,to,relation}]" in text
    assert "from_claim_id" not in text


def _payloads_with_edge() -> dict:
    """夹具补一条 c1→c2 的合法边：c1/c2 都**显式**进 ``argument_graph.nodes``。"""
    payloads = default_payloads()
    claims = payloads["claims.json"]["claims"]
    second = dict(claims[0])
    second["claim_id"] = "c2"
    second["text"] = "第二手事实陈述-c2"
    claims.append(second)
    # claim ↔ material 是双向外键：c2 同时要出现在素材片段的 claim_ids 里。
    payloads["materials.json"]["materials"][0]["segments"][0]["claim_ids"].append("c2")
    graph = payloads["topics.json"]["argument_graph"]
    graph["nodes"] = [
        default_argument_node(),
        {
            "claim_id": "c2",
            "dim": "mechanism",
            "claim": second["text"],
            "source_candidate_ids": ["s1"],
        },
    ]
    graph["edges"] = [{"from": "c1", "to": "c2", "relation": "supports"}]
    return payloads


def test_end_to_end_with_non_empty_argument_graph(tmp_path: Path):
    """含真实边的研究包必须能跨仓互通 —— 边键集的真正验收，而不只是比常量。

    既有两个跨仓用例的夹具 ``argument_graph.edges`` 恒为空，所以从没有用例真正走过边键，
    改名跑偏正是钻了这个空子。本用例让 producer 发布一个带边的包，再逐层校验到底。
    """
    producer = _producer_module()
    payloads = _payloads_with_edge()
    out = tmp_path / "producer-out"
    result = producer.publish_episode_research_pack(
        {},
        episode_id=EPISODE_ID,
        semantic=payloads,
        business_date=BUSINESS_DATE,
        output_root=str(out),
        clock=lambda: f"{BUSINESS_DATE}T23:00:00+08:00",
    )
    assert result["status"] == "published"

    episode_root = out / f"{BUSINESS_DATE}{producer.EPISODE_SUFFIX}" / EPISODE_ID
    written = sorted((episode_root / "packs").glob("*/topics.json"))
    assert len(written) == 1, written
    on_disk = json.loads(written[0].read_text(encoding="utf-8"))
    assert on_disk["argument_graph"]["edges"] == [
        {"from": "c1", "to": "c2", "relation": "supports"}
    ]

    loaded = rp.load_research_pack(episode_root, now=f"{BUSINESS_DATE}T23:30:00+08:00")
    assert loaded["status"] == "loaded", loaded
    assert loaded["partial_package"] is False

    from backlot import research_pack_intake as rpi

    summary = rpi.reconcile(
        episode_root, ledger_path=tmp_path / "ledger.json", snapshot_root=tmp_path / "snaps"
    )
    assert summary["counts"]["admitted"] == 1
