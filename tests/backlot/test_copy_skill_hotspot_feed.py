from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest

from backlot import daily_automation
from backlot.copy_skill_hotspot_feed import (
    CopySkillHotspotFeedError,
    feed_to_discovery_candidates,
    feed_to_heat_signals,
    load_copy_skill_hotspot_feed,
    try_load_copy_skill_hotspot_feed,
)
from backlot.news_selection_v2 import prepare_selection_events


REAL_ROOT = Path(os.environ.get("COPY_SKILL_HOTSPOT_ROOT", "__copy_skill_hotspot_not_configured__"))
REAL_CURRENT = REAL_ROOT / "2026-08-28_每日素材" / "current.json"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_pack(root: Path, business_date: str = "2026-08-28") -> tuple[Path, Path]:
    day = root / f"{business_date}_每日素材"
    run_id = "run-test-feed"
    pack = day / "packs" / run_id
    pack.mkdir(parents=True)
    image = pack / "images" / "event-hot" / "primary.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"review-required-image")
    candidates = [
        {
            "event_id": "event-hot",
            "story_id": "event-hot",
            "rank": 1,
            "title": "小鹏第二代VLA重大升级",
            "aliases": ["小鹏第二代 VLA 发布升级"],
            "business_date": business_date,
            "published_at_min": f"{business_date}T10:00:00+08:00",
            "published_at_max": f"{business_date}T12:00:00+08:00",
            "source_lanes": ["search"],
            "matched_keywords": ["AI新品"],
            "contributing_videos": [
                {
                    "video_id": "v1",
                    "title": "小鹏第二代VLA重大升级",
                    "author": "作者",
                    "account_id": "a1",
                    "share_url": "https://www.douyin.com/video/v1",
                    "published_at": f"{business_date}T12:00:00+08:00",
                    "interactions": {"like": 100, "comment": 10, "collect": 5, "share": 2},
                    "source_lanes": ["search"],
                    "matched_keywords": ["AI新品"],
                }
            ],
            "aggregate_interactions": {"like": 100, "comment": 10, "collect": 5, "share": 2},
            "video_count": 1,
            "account_count": 1,
            "truth_status": "not_checked",
            "evidence_status": "not_checked",
            "disclaimer": "仅供选题与后续核验",
            "image_status": {"attempted": True, "state": "succeeded", "reason": None},
            "images": [
                {
                    "role": "primary",
                    "relative_path": "images/event-hot/primary.jpg",
                    "rights_status": "review_required",
                    "width": 1200,
                    "height": 800,
                    "bytes": image.stat().st_size,
                    "sha256": _sha(image),
                }
            ],
            "score_components": {"like": 10.0},
            "heat_score": 88.0,
        },
        {
            "event_id": "event-no-image",
            "story_id": "event-no-image",
            "rank": 2,
            "title": "国产开源模型发布",
            "aliases": [],
            "business_date": business_date,
            "published_at_min": f"{business_date}T09:00:00+08:00",
            "published_at_max": f"{business_date}T09:00:00+08:00",
            "source_lanes": ["account"],
            "matched_keywords": ["开源大模型"],
            "contributing_videos": [],
            "aggregate_interactions": {},
            "video_count": 1,
            "account_count": 0,
            "truth_status": "not_checked",
            "evidence_status": "not_checked",
            "disclaimer": "仅供选题与后续核验",
            "image_status": {"attempted": True, "state": "failed", "reason": "尺寸不足"},
            "images": [],
            "score_components": {},
            "heat_score": 60.0,
        },
    ]
    pool = {
        "schema": "daily-hot-candidate-pool-v2",
        "contract_version": "2.0",
        "producer": "copy_skill",
        "business_date": business_date,
        "generated_at": f"{business_date}T23:00:00+08:00",
        "run_id": run_id,
        "status": "partial",
        "truth_status": "not_checked",
        "disclaimer": "copy_skill 未核验真实性",
        "candidates": candidates,
    }
    raw = {
        "business_date": business_date,
        "videos": [{"video_id": "v1"}, {"video_id": "v2"}],
        "excluded": [{"video_id": "old"}],
    }
    report = {
        "schema": "daily-hot-candidate-pool-v2",
        "business_date": business_date,
        "run_id": run_id,
        "status": "partial",
        "account_attempts": [
            {
                "account_id": "approved-1",
                "status": "failed",
                "returncode": 124,
                "timeout_seconds": 120,
                "error": "crawler timed out after 120 seconds",
                "raw_count": 10,
                "date_count": 2,
            }
        ],
        "errors": [{"phase": "approved_accounts", "message": "raw cap retained 40 of 162 account records"}],
    }
    _write_json(pack / "candidate-pool.json", pool)
    _write_json(pack / "raw-videos.json", raw)
    _write_json(pack / "run-report.json", report)
    (pack / "昨日抖音科技热点候选.md").write_text("# 候选\n", encoding="utf-8")
    (pack / "consumer-dry-run.json").write_text("{}\n", encoding="utf-8")
    (pack / "manifest.json").write_text("{}\n", encoding="utf-8")
    listed = [
        "candidate-pool.json",
        "consumer-dry-run.json",
        "images/event-hot/primary.jpg",
        "manifest.json",
        "raw-videos.json",
        "run-report.json",
        "昨日抖音科技热点候选.md",
    ]
    manifest = {
        "manifest_version": "1.0",
        "self_excluded": ["_READY.json", "package-manifest.json"],
        "files": [
            {"path": relative, "bytes": (pack / relative).stat().st_size, "sha256": _sha(pack / relative)}
            for relative in listed
        ],
    }
    _write_json(pack / "package-manifest.json", manifest)
    manifest_hash = _sha(pack / "package-manifest.json")
    ready = {
        "contract_version": "2.0",
        "producer": "copy_skill",
        "business_date": business_date,
        "run_id": run_id,
        "generated_at": f"{business_date}T23:00:00+08:00",
        "status": "partial",
        "human_brief": "昨日抖音科技热点候选.md",
        "machine_contract": "candidate-pool.json",
        "manifest": "package-manifest.json",
        "package_manifest_sha256": manifest_hash,
        "counts": {"candidates": 2, "raw_records": 3, "target_day_videos": 2, "images": 1},
    }
    _write_json(pack / "_READY.json", ready)
    current = {
        "contract_version": "2.0",
        "business_date": business_date,
        "status": "partial",
        "pack_relative_path": f"packs/{run_id}",
        "ready_relative_path": f"packs/{run_id}/_READY.json",
        "package_manifest_sha256": manifest_hash,
    }
    _write_json(day / "current.json", current)
    return day, pack


def test_valid_partial_pack_is_normalized_and_snapshotted(tmp_path: Path):
    root = tmp_path / "source"
    _make_pack(root)
    snapshot = tmp_path / "snapshot"
    feed = load_copy_skill_hotspot_feed(root, "2026-08-28", snapshot_dir=snapshot)
    assert feed["feed_status"] == "partial"
    assert feed["counts"] == {"candidates": 2, "raw_records": 3, "target_day_videos": 2, "images": 1}
    assert feed["manifest_validation"]["valid"] is True
    assert "返回 124" in feed["coverage_warning"]
    assert (snapshot / "source-index.json").is_file()
    assert (snapshot / "candidate-snapshot.json").is_file()
    assert (snapshot / "top10-report.json").is_file()


def test_truth_image_rights_and_no_image_semantics_are_preserved(tmp_path: Path):
    root = tmp_path / "source"
    _make_pack(root)
    feed = load_copy_skill_hotspot_feed(root, "2026-08-28")
    assert all(item["truth_status"] == "not_checked" for item in feed["candidates"])
    assert feed["candidates"][0]["images"][0]["rights_status"] == "review_required"
    assert feed["candidates"][0]["images"][0]["render_eligible"] is False
    assert feed["candidates"][1]["images"] == []
    assert len(feed["candidates"]) == 2


def test_missing_current_degrades_without_throwing(tmp_path: Path):
    result = try_load_copy_skill_hotspot_feed(tmp_path, "2026-08-28")
    assert result["feed_status"] == "missing"
    assert result["counts"]["candidates"] == 0


@pytest.mark.parametrize("failure", ["date", "ready", "manifest", "entry"])
def test_contract_failures_are_rejected(tmp_path: Path, failure: str):
    root = tmp_path / "source"
    day, pack = _make_pack(root)
    if failure == "date":
        current = json.loads((day / "current.json").read_text(encoding="utf-8"))
        current["business_date"] = "2026-08-27"
        _write_json(day / "current.json", current)
    elif failure == "ready":
        (pack / "_READY.json").unlink()
    elif failure == "manifest":
        current = json.loads((day / "current.json").read_text(encoding="utf-8"))
        current["package_manifest_sha256"] = "0" * 64
        _write_json(day / "current.json", current)
    else:
        (pack / "candidate-pool.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(CopySkillHotspotFeedError):
        load_copy_skill_hotspot_feed(root, "2026-08-28")


def test_explicit_date_ignores_mismatched_latest(tmp_path: Path):
    root = tmp_path / "source"
    _make_pack(root, "2026-08-28")
    _write_json(root / "latest.json", {"business_date": "2026-08-27", "pack_relative_path": "wrong"})
    feed = load_copy_skill_hotspot_feed(root, date(2026, 8, 28))
    assert feed["business_date"] == "2026-08-28"


def test_snapshot_is_idempotent_for_same_manifest(tmp_path: Path):
    root = tmp_path / "source"
    _make_pack(root)
    snapshot = tmp_path / "snapshot"
    first = load_copy_skill_hotspot_feed(root, "2026-08-28", snapshot_dir=snapshot)
    before = {path.name: path.read_bytes() for path in snapshot.iterdir()}
    second = load_copy_skill_hotspot_feed(root, "2026-08-28", snapshot_dir=snapshot)
    after = {path.name: path.read_bytes() for path in snapshot.iterdir()}
    assert before == after
    assert first["snapshot_paths"] == second["snapshot_paths"]


def test_reading_never_modifies_producer_pack(tmp_path: Path):
    root = tmp_path / "source"
    day, pack = _make_pack(root)
    before = {
        path.relative_to(day).as_posix(): (path.stat().st_size, _sha(path))
        for path in day.rglob("*")
        if path.is_file()
    }
    load_copy_skill_hotspot_feed(root, "2026-08-28", snapshot_dir=tmp_path / "openmontage-snapshot")
    after = {
        path.relative_to(day).as_posix(): (path.stat().st_size, _sha(path))
        for path in day.rglob("*")
        if path.is_file()
    }
    assert before == after


def test_status_can_fall_back_to_validated_snapshot_index(tmp_path: Path, monkeypatch):
    root = tmp_path / "source"
    _make_pack(root)
    runs = tmp_path / "runs"
    snapshot = runs / "2026-08-28" / "inputs" / "copy_skill_hotspot"
    load_copy_skill_hotspot_feed(root, "2026-08-28", snapshot_dir=snapshot)
    monkeypatch.setattr(daily_automation, "RUNS_ROOT", runs)
    status = daily_automation.copy_skill_hotspot_status(
        {"copy_skill_hotspot_feed": {"enabled": True, "root": str(root)}},
        {"target_date": "2026-08-28"},
    )
    assert status["state"] == "partial"
    assert status["candidate_count"] == 2
    assert status["manifest_validation"]["valid"] is True


def test_discovery_candidate_merges_with_independent_fact_and_keeps_provenance(tmp_path: Path):
    root = tmp_path / "source"
    _make_pack(root)
    feed = load_copy_skill_hotspot_feed(root, "2026-08-28")
    factual = {
        "candidate_id": "N-FACT",
        "title": "小鹏第二代 VLA 迎来重大升级",
        "summary": "官方发布第二代 VLA 升级信息。",
        "url": "https://example.com/xpeng-vla",
        "published_at": "2026-08-28T13:00:00+08:00",
        "source_id": "official",
        "source_name": "小鹏汽车",
        "authority": "official",
        "evidence_status": "ok",
        "evidence_excerpt": "小鹏汽车正式发布第二代 VLA 升级。",
        "evidence_resolved_url": "https://example.com/xpeng-vla",
        "china_short_video_hint": {"likely_china_relevance": "high"},
    }
    research = {
        "candidates": [factual, *feed_to_discovery_candidates(feed)],
        "heat_signals": feed_to_heat_signals(feed),
    }
    events = prepare_selection_events(research)
    event = next(item for item in events if "小鹏" in item["canonical_title"])
    assert event["evidence_gate"] == "pass"
    assert event["independent_publisher_count"] == 1
    assert event["evidence_candidate_ids"] == ["N-FACT"]
    assert event["discovery_provenance"][0]["truth_status"] == "not_checked"
    assert event["external_heat_matches"][0]["provenance"]["producer"] == "copy_skill"


def test_collect_news_candidates_consumes_feed_without_using_it_as_evidence(tmp_path: Path):
    root = tmp_path / "source"
    _make_pack(root)

    class Response:
        content = """<rss><channel><item><title>小鹏第二代 VLA 迎来重大升级</title><link>https://example.com/news</link><pubDate>Fri, 28 Aug 2026 05:00:00 GMT</pubDate><description>official update</description></item></channel></rss>""".encode("utf-8")

        def raise_for_status(self):
            return None

    research = daily_automation.collect_news_candidates(
        "2026-08-28",
        sources=[{"id": "official", "name": "官方", "url": "https://example.com/rss", "authority": "official"}],
        request_get=lambda *args, **kwargs: Response(),
        copy_skill_root=root,
        copy_skill_enabled=True,
        copy_skill_snapshot_dir=tmp_path / "snapshot",
    )
    assert research["copy_skill_feed"]["feed_status"] == "partial"
    heat = [item for item in research["heat_signals"] if item.get("source_id") == "copy_skill-douyin-hotspot-v2"]
    discovery = [item for item in research["candidates"] if item.get("discovery_only")]
    assert len(heat) == 2
    assert len(discovery) == 2
    assert all(item["evidence_status"] == "not_applicable_heat_only" for item in discovery)
    assert research["quality"]["distinct_source_count"] == 1


@pytest.mark.skipif(not REAL_CURRENT.is_file(), reason="本机没有 2026-08-28 copy_skill READY 包")
def test_real_20260828_package_end_to_end(tmp_path: Path):
    feed = load_copy_skill_hotspot_feed(REAL_ROOT, "2026-08-28", snapshot_dir=tmp_path / "snapshot")
    assert feed["run_id"] == "run-20260828-ca183055c8a0"
    assert feed["feed_status"] == "partial"
    assert feed["counts"] == {"candidates": 24, "raw_records": 81, "target_day_videos": 31, "images": 2}
    assert len(feed["candidates"][:10]) == 10
    assert feed["candidates"][0]["image_status"]["state"] == "failed"
    assert [item["images"][0]["rights_status"] for item in feed["candidates"][1:3]] == [
        "review_required",
        "review_required",
    ]
