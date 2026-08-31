"""Read-only adapter for copy_skill daily Douyin hotspot candidate packs.

The producer package is a discovery and public-heat input.  It is deliberately
not a fact source: normalized candidates keep ``truth_status=not_checked`` and
images keep their original rights-review status.  This module never imports
copy_skill code and never writes to the producer directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "2.0"
POOL_SCHEMA = "daily-hot-candidate-pool-v2"
DEFAULT_COPY_SKILL_HOTSPOT_ROOT = Path(
    os.environ.get("COPY_SKILL_HOTSPOT_ROOT")
    or "integrations/copy_skill_hotspot"
)
REQUIRED_PACKAGE_FILES = (
    "_READY.json",
    "package-manifest.json",
    "candidate-pool.json",
    "raw-videos.json",
    "run-report.json",
    "昨日抖音科技热点候选.md",
)


class CopySkillHotspotFeedError(RuntimeError):
    """Raised when a producer package is missing or violates its contract."""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CopySkillHotspotFeedError(f"缺少文件：{path}", code="missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CopySkillHotspotFeedError(f"无法读取有效 JSON：{path.name}") from exc
    if not isinstance(value, dict):
        raise CopySkillHotspotFeedError(f"JSON 顶层必须是对象：{path.name}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise CopySkillHotspotFeedError(f"缺少文件：{path}", code="missing") from exc
    return digest.hexdigest()


def _safe_child(base: Path, relative: object, *, label: str) -> Path:
    raw = str(relative or "").strip()
    if not raw:
        raise CopySkillHotspotFeedError(f"{label} 为空")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise CopySkillHotspotFeedError(f"{label} 必须是相对路径")
    base_resolved = base.resolve()
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise CopySkillHotspotFeedError(f"{label} 越出目标日期目录") from exc
    return resolved


def _expect(value: object, expected: str, *, label: str) -> None:
    if str(value or "") != expected:
        raise CopySkillHotspotFeedError(
            f"{label} 不匹配：期望 {expected}，实际 {value or '[空]'}"
        )


def _atomic_json_if_changed(path: Path, value: dict[str, Any]) -> bool:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == payload:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp"
    ) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    temp_path.replace(path)
    return True


def _coverage_details(report: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    risks: list[dict[str, Any]] = []
    timed_out = []
    for item in report.get("account_attempts") or []:
        if not isinstance(item, dict):
            continue
        normalized = {
            "phase": "approved_accounts",
            "account_id": str(item.get("account_id") or ""),
            "status": str(item.get("status") or ""),
            "returncode": item.get("returncode"),
            "timeout_seconds": item.get("timeout_seconds"),
            "error": str(item.get("error") or ""),
            "raw_count": int(item.get("raw_count") or 0),
            "date_count": int(item.get("date_count") or 0),
        }
        if normalized["status"] != "ok":
            risks.append(normalized)
        if normalized["returncode"] == 124:
            timed_out.append(normalized)
    for item in report.get("errors") or []:
        if isinstance(item, dict):
            risks.append(
                {
                    "phase": str(item.get("phase") or "unknown"),
                    "message": str(item.get("message") or ""),
                }
            )
    warnings: list[str] = []
    if timed_out:
        timeout_seconds = sorted(
            {int(item.get("timeout_seconds") or 0) for item in timed_out if item.get("timeout_seconds")}
        )
        timeout_label = "/".join(str(value) for value in timeout_seconds) or "未知"
        warnings.append(
            f"{len(timed_out)} 个批准账号采集子进程在 {timeout_label} 秒上限返回 124"
        )
    warnings.extend(
        str(item.get("message") or "")
        for item in report.get("errors") or []
        if isinstance(item, dict) and str(item.get("message") or "").strip()
    )
    return risks, "；".join(dict.fromkeys(warnings)) or None


def _normalize_image(asset: dict[str, Any], pack_dir: Path) -> dict[str, Any]:
    relative_path = str(asset.get("relative_path") or "")
    absolute_path = _safe_child(pack_dir, relative_path, label="图片路径") if relative_path else None
    return {
        "role": str(asset.get("role") or ""),
        "relative_path": relative_path,
        "source_absolute_path": str(absolute_path) if absolute_path else None,
        "source_exists": bool(absolute_path and absolute_path.is_file()),
        "image_source_url": str(asset.get("image_source_url") or ""),
        "source_article_url": str(asset.get("source_article_url") or ""),
        "source_name": str(asset.get("source_name") or ""),
        "selection_reason": str(asset.get("selection_reason") or ""),
        "rights_status": str(asset.get("rights_status") or ""),
        "mime_type": str(asset.get("mime_type") or ""),
        "width": int(asset.get("width") or 0),
        "height": int(asset.get("height") or 0),
        "bytes": int(asset.get("bytes") or 0),
        "sha256": str(asset.get("sha256") or ""),
        "render_eligible": False,
    }


def _normalize_candidate(
    candidate: dict[str, Any],
    *,
    pack_dir: Path,
    package_status: str,
    run_id: str,
    manifest_sha256: str,
    partial_risks: list[dict[str, Any]],
    coverage_warning: str | None,
) -> dict[str, Any]:
    image_status = candidate.get("image_status") if isinstance(candidate.get("image_status"), dict) else {}
    raw_assets = candidate.get("images") if isinstance(candidate.get("images"), list) else []
    images = [_normalize_image(item, pack_dir) for item in raw_assets if isinstance(item, dict)]
    contributing_videos = []
    for item in candidate.get("contributing_videos") or []:
        if not isinstance(item, dict):
            continue
        contributing_videos.append(
            {
                "video_id": str(item.get("video_id") or ""),
                "title": str(item.get("title") or ""),
                "author": str(item.get("author") or ""),
                "account_id": str(item.get("account_id") or ""),
                "share_url": str(item.get("share_url") or ""),
                "published_at": str(item.get("published_at") or ""),
                "interactions": dict(item.get("interactions") or {}),
                "source_lanes": list(item.get("source_lanes") or []),
                "matched_keywords": list(item.get("matched_keywords") or []),
            }
        )
    truth_status = str(candidate.get("truth_status") or "not_checked")
    if truth_status != "not_checked":
        raise CopySkillHotspotFeedError(
            f"候选 {candidate.get('event_id') or '[未知]'} 的 truth_status 不是 not_checked"
        )
    return {
        "event_id": str(candidate.get("event_id") or ""),
        "story_id": str(candidate.get("story_id") or candidate.get("event_id") or ""),
        "rank": int(candidate.get("rank") or 0),
        "title": str(candidate.get("title") or ""),
        "aliases": [str(item) for item in candidate.get("aliases") or [] if str(item).strip()],
        "business_date": str(candidate.get("business_date") or ""),
        "published_at_min": str(candidate.get("published_at_min") or ""),
        "published_at_max": str(candidate.get("published_at_max") or ""),
        "heat_score": float(candidate.get("heat_score") or 0),
        "score_components": dict(candidate.get("score_components") or {}),
        "contributing_videos": contributing_videos,
        "source_lanes": list(candidate.get("source_lanes") or []),
        "matched_keywords": list(candidate.get("matched_keywords") or []),
        "video_count": int(candidate.get("video_count") or 0),
        "account_count": int(candidate.get("account_count") or 0),
        "aggregate_interactions": dict(candidate.get("aggregate_interactions") or {}),
        "truth_status": truth_status,
        "evidence_status": str(candidate.get("evidence_status") or "not_checked"),
        "disclaimer": str(candidate.get("disclaimer") or ""),
        "image_status": {
            "attempted": bool(image_status.get("attempted")),
            "state": str(image_status.get("state") or "not_attempted"),
            "reason": image_status.get("reason"),
            "asset_count": len(images),
        },
        "images": images,
        "package": {
            "status": package_status,
            "run_id": run_id,
            "manifest_sha256": manifest_sha256,
            "partial_risks": partial_risks,
            "coverage_warning": coverage_warning,
        },
    }


def _snapshot_feed(feed: dict[str, Any], snapshot_dir: Path) -> dict[str, str]:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    source_index_path = snapshot_dir / "source-index.json"
    existing = {}
    if source_index_path.is_file():
        try:
            loaded = json.loads(source_index_path.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    same_source = (
        existing.get("manifest_sha256") == feed["manifest_validation"]["manifest_sha256"]
        and existing.get("business_date") == feed["business_date"]
        and existing.get("run_id") == feed["run_id"]
    )
    read_at = str(existing.get("read_at") or "") if same_source else ""
    if not read_at:
        read_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    source_index = {
        "schema": "openmontage-copy-skill-hotspot-source-index-v1",
        "business_date": feed["business_date"],
        "run_id": feed["run_id"],
        "feed_status": feed["feed_status"],
        "source_root": feed["source_root"],
        "source_day_dir": feed["source_day_dir"],
        "source_pack_dir": feed["source_pack_dir"],
        "manifest_sha256": feed["manifest_validation"]["manifest_sha256"],
        "manifest_validation": feed["manifest_validation"],
        "read_at": read_at,
        "candidate_count": feed["counts"]["candidates"],
        "coverage_warning": feed.get("coverage_warning"),
        "partial_risks": feed.get("partial_risks") or [],
        "source_files": feed["manifest_validation"]["files"],
        "producer_modified": False,
    }
    candidate_snapshot = {
        "schema": "openmontage-copy-skill-hotspot-candidates-v1",
        "business_date": feed["business_date"],
        "run_id": feed["run_id"],
        "feed_status": feed["feed_status"],
        "manifest_sha256": feed["manifest_validation"]["manifest_sha256"],
        "truth_semantics": "discovery_and_heat_only_not_fact_evidence",
        "coverage_warning": feed.get("coverage_warning"),
        "counts": feed["counts"],
        "candidates": feed["candidates"],
    }
    top10 = {
        "schema": "openmontage-copy-skill-hotspot-top10-v1",
        "business_date": feed["business_date"],
        "run_id": feed["run_id"],
        "feed_status": feed["feed_status"],
        "coverage_warning": feed.get("coverage_warning"),
        "top10": [
            {
                "rank": item["rank"],
                "event_id": item["event_id"],
                "title": item["title"],
                "heat_score": item["heat_score"],
                "video_count": item["video_count"],
                "image_state": item["image_status"]["state"],
                "image_rights": [asset["rights_status"] for asset in item["images"]],
            }
            for item in feed["candidates"][:10]
        ],
    }
    _atomic_json_if_changed(source_index_path, source_index)
    candidate_path = snapshot_dir / "candidate-snapshot.json"
    top10_path = snapshot_dir / "top10-report.json"
    _atomic_json_if_changed(candidate_path, candidate_snapshot)
    _atomic_json_if_changed(top10_path, top10)
    return {
        "source_index": str(source_index_path),
        "candidate_snapshot": str(candidate_path),
        "top10_report": str(top10_path),
    }


def load_copy_skill_hotspot_feed(
    root: str | Path,
    business_date: str | date,
    *,
    snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and normalize the package selected by an explicit business date."""
    business_date_value = (
        business_date.isoformat() if isinstance(business_date, date) else str(business_date)
    )
    try:
        date.fromisoformat(business_date_value)
    except ValueError as exc:
        raise CopySkillHotspotFeedError("business_date 必须是 YYYY-MM-DD") from exc
    root_path = Path(root)
    day_dir = root_path / f"{business_date_value}_每日素材"
    current_path = day_dir / "current.json"
    current = _read_json(current_path)
    _expect(current.get("contract_version"), CONTRACT_VERSION, label="current contract_version")
    _expect(current.get("business_date"), business_date_value, label="current business_date")
    pack_dir = _safe_child(day_dir, current.get("pack_relative_path"), label="pack_relative_path")
    ready_path = _safe_child(day_dir, current.get("ready_relative_path"), label="ready_relative_path")
    if ready_path.parent != pack_dir:
        raise CopySkillHotspotFeedError("READY 文件与 current 指向的包目录不一致")
    for filename in REQUIRED_PACKAGE_FILES:
        if not (pack_dir / filename).is_file():
            raise CopySkillHotspotFeedError(f"READY 包缺少文件：{filename}", code="missing")

    ready = _read_json(ready_path)
    pool = _read_json(pack_dir / "candidate-pool.json")
    raw_videos = _read_json(pack_dir / "raw-videos.json")
    report = _read_json(pack_dir / "run-report.json")
    manifest_path = pack_dir / "package-manifest.json"
    manifest = _read_json(manifest_path)
    run_id = str(ready.get("run_id") or "")
    _expect(ready.get("contract_version"), CONTRACT_VERSION, label="READY contract_version")
    _expect(pool.get("contract_version"), CONTRACT_VERSION, label="candidate-pool contract_version")
    _expect(pool.get("schema"), POOL_SCHEMA, label="candidate-pool schema")
    for label, document in (
        ("READY", ready),
        ("candidate-pool", pool),
        ("raw-videos", raw_videos),
        ("run-report", report),
    ):
        _expect(document.get("business_date"), business_date_value, label=f"{label} business_date")
    for label, document in (("candidate-pool", pool), ("run-report", report)):
        _expect(document.get("run_id"), run_id, label=f"{label} run_id")

    manifest_sha256 = _sha256(manifest_path)
    _expect(current.get("package_manifest_sha256"), manifest_sha256, label="current manifest hash")
    _expect(ready.get("package_manifest_sha256"), manifest_sha256, label="READY manifest hash")
    manifest_files = manifest.get("files") if isinstance(manifest.get("files"), list) else None
    if not manifest_files:
        raise CopySkillHotspotFeedError("package-manifest 没有文件清单")
    verified_files: list[dict[str, Any]] = []
    for entry in manifest_files:
        if not isinstance(entry, dict):
            raise CopySkillHotspotFeedError("package-manifest 文件条目格式无效")
        path = _safe_child(pack_dir, entry.get("path"), label="manifest path")
        if not path.is_file():
            raise CopySkillHotspotFeedError(f"manifest 文件不存在：{entry.get('path')}", code="missing")
        actual_bytes = path.stat().st_size
        actual_sha256 = _sha256(path)
        if actual_bytes != int(entry.get("bytes") or -1) or actual_sha256 != str(entry.get("sha256") or ""):
            raise CopySkillHotspotFeedError(f"manifest 文件校验失败：{entry.get('path')}")
        verified_files.append(
            {
                "path": str(entry.get("path") or ""),
                "bytes": actual_bytes,
                "sha256": actual_sha256,
            }
        )

    candidates = pool.get("candidates") if isinstance(pool.get("candidates"), list) else None
    videos = raw_videos.get("videos") if isinstance(raw_videos.get("videos"), list) else None
    excluded = raw_videos.get("excluded") if isinstance(raw_videos.get("excluded"), list) else []
    if candidates is None or videos is None:
        raise CopySkillHotspotFeedError("候选池或 raw-videos schema 无效")
    ready_counts = ready.get("counts") if isinstance(ready.get("counts"), dict) else {}
    expected_candidates = int(ready_counts.get("candidates") or 0)
    expected_target_videos = int(ready_counts.get("target_day_videos") or 0)
    expected_raw_records = int(ready_counts.get("raw_records") or 0)
    if len(candidates) != expected_candidates:
        raise CopySkillHotspotFeedError("候选数量与 READY 计数不一致")
    if len(videos) != expected_target_videos:
        raise CopySkillHotspotFeedError("目标日视频数量与 READY 计数不一致")
    if len(videos) + len(excluded) != expected_raw_records:
        raise CopySkillHotspotFeedError("原始记录数量与 READY 计数不一致")

    statuses = {
        str(current.get("status") or ""),
        str(ready.get("status") or ""),
        str(pool.get("status") or ""),
        str(report.get("status") or ""),
    }
    if len(statuses) != 1 or next(iter(statuses)) not in {"ready", "complete", "partial"}:
        raise CopySkillHotspotFeedError("current、READY、候选池与运行报告状态不一致")
    package_status = next(iter(statuses))
    partial_risks, coverage_warning = _coverage_details(report)
    normalized = [
        _normalize_candidate(
            item,
            pack_dir=pack_dir,
            package_status=package_status,
            run_id=run_id,
            manifest_sha256=manifest_sha256,
            partial_risks=partial_risks,
            coverage_warning=coverage_warning,
        )
        for item in candidates
        if isinstance(item, dict)
    ]
    normalized.sort(key=lambda item: (item["rank"] or 999, -item["heat_score"]))
    feed = {
        "schema": "openmontage-copy-skill-hotspot-feed-v1",
        "feed_status": "partial" if package_status == "partial" else "loaded",
        "package_status": package_status,
        "contract_version": CONTRACT_VERSION,
        "business_date": business_date_value,
        "run_id": run_id,
        "generated_at": str(ready.get("generated_at") or pool.get("generated_at") or ""),
        "source_root": str(root_path.resolve()),
        "source_day_dir": str(day_dir.resolve()),
        "source_pack_dir": str(pack_dir),
        "manifest_validation": {
            "valid": True,
            "manifest_sha256": manifest_sha256,
            "current_hash_match": True,
            "ready_hash_match": True,
            "file_count": len(verified_files),
            "files": verified_files,
        },
        "counts": {
            "candidates": len(normalized),
            "raw_records": expected_raw_records,
            "target_day_videos": len(videos),
            "images": int(ready_counts.get("images") or 0),
        },
        "truth_status": str(pool.get("truth_status") or "not_checked"),
        "disclaimer": str(pool.get("disclaimer") or ""),
        "partial_risks": partial_risks,
        "coverage_warning": coverage_warning,
        "candidates": normalized,
    }
    if feed["truth_status"] != "not_checked":
        raise CopySkillHotspotFeedError("候选池 truth_status 不是 not_checked")
    if snapshot_dir is not None:
        feed["snapshot_paths"] = _snapshot_feed(feed, Path(snapshot_dir))
    return feed


def try_load_copy_skill_hotspot_feed(
    root: str | Path,
    business_date: str | date,
    *,
    snapshot_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return a non-fatal status envelope suitable for the daily fallback chain."""
    business_date_value = business_date.isoformat() if isinstance(business_date, date) else str(business_date)
    try:
        return load_copy_skill_hotspot_feed(root, business_date_value, snapshot_dir=snapshot_dir)
    except CopySkillHotspotFeedError as exc:
        return {
            "schema": "openmontage-copy-skill-hotspot-feed-v1",
            "feed_status": "missing" if exc.code == "missing" else "invalid",
            "business_date": business_date_value,
            "source_root": str(Path(root)),
            "run_id": None,
            "coverage_warning": None,
            "manifest_validation": {"valid": False, "error": str(exc)},
            "counts": {"candidates": 0, "raw_records": 0, "target_day_videos": 0, "images": 0},
            "partial_risks": [],
            "candidates": [],
            "error": str(exc),
        }


def feed_to_heat_signals(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate normalized candidates into non-evidentiary public heat signals."""
    if feed.get("feed_status") not in {"loaded", "partial"}:
        return []
    signals: list[dict[str, Any]] = []
    for candidate in feed.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        interactions = candidate.get("aggregate_interactions") or {}
        aggregate_total = sum(int(interactions.get(key) or 0) for key in ("like", "comment", "collect", "share"))
        signals.append(
            {
                "signal_id": f"copy-skill-{candidate.get('event_id')}",
                "source_id": "copy_skill-douyin-hotspot-v2",
                "source_name": "copy_skill 昨日抖音科技热点候选池 V2",
                "title": str(candidate.get("title") or "")[:300],
                "aliases": list(candidate.get("aliases") or []),
                "rank": int(candidate.get("rank") or 999),
                "heat_value": aggregate_total,
                "heat_score": float(candidate.get("heat_score") or 0),
                "scope": "douyin_public_heat_candidate_v2",
                "captured_at": str(feed.get("generated_at") or ""),
                "truth_status": "not_checked",
                "disclaimer": str(candidate.get("disclaimer") or feed.get("disclaimer") or ""),
                "provenance": {
                    "producer": "copy_skill",
                    "event_id": str(candidate.get("event_id") or ""),
                    "run_id": str(feed.get("run_id") or ""),
                    "manifest_sha256": str((feed.get("manifest_validation") or {}).get("manifest_sha256") or ""),
                    "package_status": str(feed.get("package_status") or ""),
                    "video_count": int(candidate.get("video_count") or 0),
                    "account_count": int(candidate.get("account_count") or 0),
                    "source_lanes": list(candidate.get("source_lanes") or []),
                    "contributing_videos": list(candidate.get("contributing_videos") or []),
                },
            }
        )
    return signals


def feed_to_discovery_candidates(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """Create heat-only leads that may cluster with independently sourced facts."""
    if feed.get("feed_status") not in {"loaded", "partial"}:
        return []
    output: list[dict[str, Any]] = []
    for item in feed.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        videos = item.get("contributing_videos") or []
        first_video = next((video for video in videos if isinstance(video, dict)), {})
        event_id = str(item.get("event_id") or "")
        output.append(
            {
                "candidate_id": f"CS-{event_id}",
                "title": str(item.get("title") or "")[:300],
                "summary": str(item.get("disclaimer") or feed.get("disclaimer") or "")[:1200],
                "url": str(first_video.get("share_url") or ""),
                "published_at": str(item.get("published_at_max") or item.get("published_at_min") or ""),
                "source_id": "copy_skill-douyin-hotspot-v2",
                "source_name": "copy_skill 昨日抖音科技热点候选池 V2",
                "authority": "heat_only",
                "discovery_only": True,
                "truth_status": "not_checked",
                "evidence_status": "not_applicable_heat_only",
                "copy_skill_hotspot": {
                    "event_id": event_id,
                    "story_id": str(item.get("story_id") or event_id),
                    "rank": int(item.get("rank") or 999),
                    "heat_score": float(item.get("heat_score") or 0),
                    "aliases": list(item.get("aliases") or []),
                    "score_components": dict(item.get("score_components") or {}),
                    "video_count": int(item.get("video_count") or 0),
                    "account_count": int(item.get("account_count") or 0),
                    "aggregate_interactions": dict(item.get("aggregate_interactions") or {}),
                    "source_lanes": list(item.get("source_lanes") or []),
                    "matched_keywords": list(item.get("matched_keywords") or []),
                    "contributing_videos": list(item.get("contributing_videos") or []),
                    "image_status": dict(item.get("image_status") or {}),
                    "images": list(item.get("images") or []),
                    "rights_statuses": [
                        str(asset.get("rights_status") or "")
                        for asset in item.get("images") or []
                        if isinstance(asset, dict)
                    ],
                    "run_id": str(feed.get("run_id") or ""),
                    "manifest_sha256": str((feed.get("manifest_validation") or {}).get("manifest_sha256") or ""),
                    "coverage_warning": feed.get("coverage_warning"),
                },
            }
        )
    return output
