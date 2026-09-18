"""episode-research-pack-v1 Intake/Reconcile 的离线测试（at-least-once + 幂等）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from backlot import research_pack_intake as rpi
from tests.backlot.test_copy_skill_research_pack import (
    BUSINESS_DATE,
    EPISODE_ID,
    _read,
    _write,
    build_pack,
    default_payloads,
)


def _ledger(ledger_path: Path) -> dict:
    return json.loads(ledger_path.read_text(encoding="utf-8"))


def _run(root: Path, tmp_path: Path, **kwargs) -> dict:
    return rpi.reconcile(
        root,
        ledger_path=tmp_path / "ledger.json",
        snapshot_root=tmp_path / "snapshots",
        **kwargs,
    )


# --- 首次收编 --------------------------------------------------------------


def test_first_pack_is_admitted_with_snapshot(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root)
    summary = _run(root, tmp_path)

    assert summary["scanned"] == 1
    assert summary["counts"]["admitted"] == 1
    row = summary["results"][0]
    assert row["state"] == "admitted"
    assert row["decision"] == "ingest"
    assert row["disposition"] == "ready"
    assert row["partial_package"] is False
    assert Path(row["snapshot_dir"]).is_dir()

    ledger = _ledger(tmp_path / "ledger.json")
    entry = ledger["episodes"][EPISODE_ID]
    assert entry["highest_revision"] == 1
    assert entry["current"]["revision"] == 1
    assert entry["current"]["disposition"] == "ready"
    history = entry["history"]
    assert len(history) == 1
    assert history[0]["idempotency_key"] == (
        f"episode-research-pack-v1:{EPISODE_ID}:{row['content_sha256']}"
    )


def test_default_ledger_path_is_dot_backlot():
    from lib.paths import REPO_ROOT

    assert rpi.DEFAULT_LEDGER_PATH == REPO_ROOT / ".backlot" / "research_pack_intake.json"


# --- 幂等 ------------------------------------------------------------------


def test_repeated_reconcile_is_noop_and_does_not_resnapshot(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root)
    first = _run(root, tmp_path)["results"][0]
    snapshot_file = Path(first["snapshot_dir"]) / "snapshot.json"
    stamp = os.stat(snapshot_file).st_mtime_ns

    second = _run(root, tmp_path)["results"][0]
    assert second["decision"] == "noop"
    assert second["state"] == "admitted"
    assert second["snapshot_dir"] == first["snapshot_dir"]
    assert os.stat(snapshot_file).st_mtime_ns == stamp

    ledger = _ledger(tmp_path / "ledger.json")
    assert len(ledger["episodes"][EPISODE_ID]["history"]) == 1


def test_standalone_intake_is_idempotent(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root)
    ledger_path = tmp_path / "ledger.json"
    first = rpi.intake_episode(
        root / f"{BUSINESS_DATE}_研究包" / EPISODE_ID,
        ledger_path=ledger_path,
        snapshot_root=tmp_path / "snapshots",
    )
    second = rpi.intake_episode(
        root / f"{BUSINESS_DATE}_研究包" / EPISODE_ID,
        ledger_path=ledger_path,
        snapshot_root=tmp_path / "snapshots",
    )
    assert first["state"] == "admitted"
    assert second["decision"] == "noop"
    assert second["snapshot_dir"] == first["snapshot_dir"]


# --- revision 规则 ---------------------------------------------------------


def test_lower_revision_with_new_content_is_stale_revision(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root, revision=1, variant="a")
    _run(root, tmp_path)
    build_pack(root, revision=2, variant="b")
    _run(root, tmp_path)

    # 用 r1 槽位塞入新内容（revision 1 < 最高 2，且 hash 未见）
    build_pack(root, revision=1, variant="c")
    row = _run(root, tmp_path)["results"][0]
    assert row["decision"] == "stale_revision"
    assert row["state"] == "admitted"  # 已入账的 episode 仍处 admitted
    assert row["admitted"] is True

    ledger = _ledger(tmp_path / "ledger.json")
    entry = ledger["episodes"][EPISODE_ID]
    assert entry["highest_revision"] == 2
    assert entry["current"]["revision"] == 2  # 不回退


def test_same_revision_different_hash_is_revision_collision(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root, revision=1, variant="a")
    _run(root, tmp_path)
    build_pack(root, revision=1, variant="b")  # 同 revision，异 hash
    row = _run(root, tmp_path)["results"][0]
    assert row["decision"] == "revision_collision"
    assert row["state"] == "failed"
    assert row["disposition"] is None
    assert row["admitted"] is False


def test_revision_gap_is_not_produced(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root, revision=1, variant="a")
    _run(root, tmp_path)
    build_pack(root, revision=3, variant="c")
    row = _run(root, tmp_path)["results"][0]
    assert row["decision"] == "revision_gap"
    assert row["state"] == "validated"
    assert row["admitted"] is False
    assert row["snapshot_dir"] is None

    ledger = _ledger(tmp_path / "ledger.json")
    assert ledger["episodes"][EPISODE_ID]["highest_revision"] == 1


def test_bumped_revision_reusing_old_content_is_invalid_contract(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root, revision=1, variant="a")
    _run(root, tmp_path)
    build_pack(root, revision=2, variant="b")
    _run(root, tmp_path)
    build_pack(root, revision=3, variant="a")  # 升 revision 却复用旧 hash
    row = _run(root, tmp_path)["results"][0]
    assert row["decision"] == "invalid_contract"
    assert row["state"] == "failed"
    assert row["admitted"] is False

    ledger = _ledger(tmp_path / "ledger.json")
    assert ledger["episodes"][EPISODE_ID]["highest_revision"] == 2


def test_higher_revision_ingests_and_preserves_history(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root, revision=1, variant="a")
    first = _run(root, tmp_path)["results"][0]
    build_pack(root, revision=2, variant="b")
    second = _run(root, tmp_path)["results"][0]

    assert second["decision"] == "ingest"
    assert second["revision"] == 2
    assert second["snapshot_dir"] != first["snapshot_dir"]

    ledger = _ledger(tmp_path / "ledger.json")
    entry = ledger["episodes"][EPISODE_ID]
    assert entry["highest_revision"] == 2
    assert [row["revision"] for row in entry["history"]] == [1, 2]
    assert entry["current"]["revision"] == 2


def test_current_rollback_does_not_rollback_ledger(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root, revision=1, variant="a")
    _run(root, tmp_path)
    build_pack(root, revision=2, variant="b")
    _run(root, tmp_path)

    # 生产端把 current 回退到 r1（同旧内容）
    build_pack(root, revision=1, variant="a")
    row = _run(root, tmp_path)["results"][0]
    assert row["decision"] == "noop"

    ledger = _ledger(tmp_path / "ledger.json")
    entry = ledger["episodes"][EPISODE_ID]
    assert entry["highest_revision"] == 2
    assert entry["current"]["revision"] == 2


# --- stale / failed --------------------------------------------------------


def test_missing_current_marks_stale_and_keeps_snapshot(tmp_path: Path):
    root = tmp_path / "root"
    episode_root, _ = build_pack(root)
    first = _run(root, tmp_path)["results"][0]

    (episode_root / "current.json").unlink()
    row = _run(root, tmp_path)["results"][0]
    assert row["state"] == "stale"
    assert row["reason"] == "current_missing"
    assert row["disposition"] == first["disposition"]  # 保留旧结论
    assert row["snapshot_dir"] == first["snapshot_dir"]
    assert Path(first["snapshot_dir"]).is_dir()  # 不删旧快照

    ledger = _ledger(tmp_path / "ledger.json")
    assert ledger["episodes"][EPISODE_ID]["current"]["revision"] == 1


def test_technical_invalid_produces_no_disposition_and_no_snapshot(tmp_path: Path):
    def mutate(ctx):
        manifest_path = ctx["pack"] / "package-manifest.json"
        manifest = _read(manifest_path)
        manifest["files"][0]["sha256"] = "0" * 64
        _write(manifest_path, manifest)

    root = tmp_path / "root"
    build_pack(root, mutate=mutate)
    row = _run(root, tmp_path)["results"][0]
    assert row["state"] == "failed"
    assert row["disposition"] is None
    assert row["snapshot_dir"] is None
    assert not (tmp_path / "snapshots").exists()

    ledger = _ledger(tmp_path / "ledger.json")
    entry = ledger["episodes"].get(EPISODE_ID)
    assert entry is None or entry.get("current") is None


def test_rights_gate_rejection_is_not_admitted(tmp_path: Path):
    payloads = default_payloads()
    payloads["rights.json"]["rights"][0]["rights_status"] = "cleared"
    root = tmp_path / "root"
    build_pack(root, payloads=payloads)
    row = _run(root, tmp_path)["results"][0]
    assert row["state"] == "rejected"
    assert row["disposition"] == "rejected"
    assert row["admitted"] is False
    assert row["snapshot_dir"] is None


# --- partial_package 只表示技术/运输损坏，绝不 Intake ----------------------


def test_transport_damaged_pack_is_not_admitted(tmp_path: Path):
    def mutate(ctx):
        (ctx["pack"] / "claims.json").write_text('{"contract": ', encoding="utf-8")

    root = tmp_path / "root"
    build_pack(root, mutate=mutate)
    row = _run(root, tmp_path)["results"][0]
    assert row["state"] == "failed"
    assert row["reason"] == "partial_package"
    assert row["partial_package"] is True
    assert row["disposition"] is None
    assert row["admitted"] is False
    assert row["snapshot_dir"] is None
    assert not (tmp_path / "snapshots").exists()

    ledger = _ledger(tmp_path / "ledger.json")
    entry = ledger["episodes"].get(EPISODE_ID)
    assert entry is None or entry.get("current") is None


def test_missing_file_pack_is_not_admitted(tmp_path: Path):
    def mutate(ctx):
        (ctx["pack"] / "rights.json").unlink()

    root = tmp_path / "root"
    build_pack(root, mutate=mutate)
    row = _run(root, tmp_path)["results"][0]
    assert row["state"] == "failed"
    assert row["reason"] == "partial_package"
    assert row["admitted"] is False
    assert row["snapshot_dir"] is None


def test_ledger_records_partial_package_event(tmp_path: Path):
    def mutate(ctx):
        (ctx["pack"] / "sources.json").write_text("not json at all", encoding="utf-8")

    root = tmp_path / "root"
    build_pack(root, mutate=mutate)
    _run(root, tmp_path)
    ledger = _ledger(tmp_path / "ledger.json")
    events = ledger["episodes"][EPISODE_ID]["events"]
    assert any(event["kind"] == "partial_package" for event in events)


def test_admitted_pack_never_carries_partial_package_flag(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root)
    row = _run(root, tmp_path)["results"][0]
    assert row["state"] == "admitted"
    assert row["partial_package"] is False
    ledger = _ledger(tmp_path / "ledger.json")
    assert ledger["episodes"][EPISODE_ID]["current"]["partial_package"] is False


# --- 崩溃恢复 --------------------------------------------------------------


def test_snapshot_survives_ledger_loss_and_is_reused(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root)
    first = _run(root, tmp_path)["results"][0]
    snapshot_dir = Path(first["snapshot_dir"])

    # 模拟「快照已提交、账本未落盘」的崩溃窗口
    (tmp_path / "ledger.json").unlink()
    row = _run(root, tmp_path)["results"][0]
    assert row["state"] == "admitted"
    assert row["snapshot_reused"] is True
    assert Path(row["snapshot_dir"]) == snapshot_dir

    episode_snapshots = list((tmp_path / "snapshots" / EPISODE_ID).iterdir())
    assert len(episode_snapshots) == 1  # 没有产生第二份快照
    ledger = _ledger(tmp_path / "ledger.json")
    assert ledger["episodes"][EPISODE_ID]["highest_revision"] == 1


def test_corrupt_ledger_is_tolerated(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root)
    (tmp_path / "ledger.json").write_text("{not json", encoding="utf-8")
    summary = _run(root, tmp_path)
    assert summary["results"][0]["state"] == "admitted"
    assert "warning" in summary


# --- 发现 ------------------------------------------------------------------


def test_discover_filters_by_business_date(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root, business_date="2026-09-16", episode_id="2026-09-16-甲")
    build_pack(root, business_date="2026-09-17", episode_id="2026-09-17-乙")

    assert len(rpi.discover_episode_roots(root)) == 2
    assert len(rpi.discover_episode_roots(root, business_date="2026-09-16")) == 1
    assert rpi.discover_episode_roots(root / "missing") == []


def test_reconcile_accepts_single_current_path(tmp_path: Path):
    root = tmp_path / "root"
    episode_root, _ = build_pack(root)
    summary = _run(episode_root / "current.json", tmp_path)
    assert summary["scanned"] == 1
    assert summary["results"][0]["state"] == "admitted"


def test_reconcile_accepts_single_episode_root(tmp_path: Path):
    root = tmp_path / "root"
    episode_root, _ = build_pack(root)
    row = _run(episode_root, tmp_path)["results"][0]
    assert row["episode_id"] == EPISODE_ID
    assert row["state"] == "admitted"


def test_reconcile_multiple_episodes(tmp_path: Path):
    root = tmp_path / "root"
    build_pack(root, business_date="2026-09-16", episode_id="2026-09-16-甲")
    build_pack(root, business_date="2026-09-16", episode_id="2026-09-16-乙")
    summary = _run(root, tmp_path)
    assert summary["scanned"] == 2
    assert summary["counts"]["admitted"] == 2
    ledger = _ledger(tmp_path / "ledger.json")
    assert set(ledger["episodes"]) == {"2026-09-16-甲", "2026-09-16-乙"}


def test_classify_revision_rules_are_pure():
    entry = {
        "highest_revision": 2,
        "current": {"revision": 2, "content_sha256": "B"},
        "history": [
            {"revision": 1, "content_sha256": "A"},
            {"revision": 2, "content_sha256": "B"},
        ],
    }
    assert rpi.classify_revision(entry, 2, "B")["decision"] == "noop"
    assert rpi.classify_revision(entry, 1, "A")["decision"] == "noop"
    assert rpi.classify_revision(entry, 1, "Z")["decision"] == "stale_revision"
    assert rpi.classify_revision(entry, 2, "Z")["decision"] == "revision_collision"
    assert rpi.classify_revision(entry, 3, "A")["decision"] == "invalid_contract"
    assert rpi.classify_revision(entry, 4, "Z")["decision"] == "revision_gap"
    assert rpi.classify_revision(entry, 3, "Z")["decision"] == "ingest"
    assert rpi.classify_revision(None, 1, "A")["decision"] == "ingest"


# --- intake_from_current_json：具名适配边界的直接单测 -----------------------
#
# 这四条不走 subprocess，直接调函数，锁住三件事：
# ① 缺失 / 不可读 / 悬空 pack_path 各自的**明确**终态；
# ② 只读性——生产端 episode 目录树在调用前后**逐字节不变**；
# ③ 幂等——同一 current 连续两次不新增账本条目、不新增快照目录。
#
# 注意 ①：``current.json`` **缺失**按既有合同是「尚未发布」而不是技术故障，
# 终态是 ``discovered``（reason ``current_missing``），不是异常；
# 只有**存在但不可读**的 current.json 才落到 ``failed`` / ``current_unreadable``。


def _tree_digest(root: Path) -> dict[str, str]:
    """生产端目录树的字节指纹：相对路径 → 文件 sha256。"""
    import hashlib

    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def _adapter(episode_root: Path, tmp_path: Path) -> dict:
    return rpi.intake_from_current_json(
        episode_root,
        ledger_path=tmp_path / "ledger.json",
        snapshot_root=tmp_path / "snapshots",
    )


def test_adapter_missing_current_is_discovered_and_creates_no_snapshot(tmp_path: Path):
    root = tmp_path / "root"
    episode_root, _ = build_pack(root)
    (episode_root / "current.json").unlink()

    row = _adapter(episode_root, tmp_path)
    assert row["state"] == "discovered"
    assert row["reason"] == "current_missing"
    assert row["admitted"] is False
    assert row["disposition"] is None
    assert row["snapshot_dir"] is None
    assert not (tmp_path / "snapshots").exists()

    ledger = _ledger(tmp_path / "ledger.json")
    assert ledger["episodes"] == {}
    assert not list(episode_root.rglob("ledger.json"))


def test_adapter_unreadable_current_reports_failure_with_contract_file_name(tmp_path: Path):
    root = tmp_path / "root"
    episode_root, _ = build_pack(root)
    (episode_root / "current.json").write_text('{"contract": ', encoding="utf-8")

    row = _adapter(episode_root, tmp_path)
    assert row["state"] == "failed"
    assert row["reason"] == "current_unreadable"
    assert row["admitted"] is False
    # 明确的中文消息，且指明是哪个合同文件读不出来
    assert "current.json" in row["error"]
    assert row["snapshot_dir"] is None
    assert not (tmp_path / "snapshots").exists()


def test_adapter_dangling_pack_path_is_partial_package_with_path(tmp_path: Path):
    root = tmp_path / "root"
    episode_root, _ = build_pack(root)

    pointer = _read(episode_root / "current.json")
    pointer["pack_path"] = "packs/does-not-exist-r9"
    _write(episode_root / "current.json", pointer)

    row = _adapter(episode_root, tmp_path)
    assert row["state"] == "failed"
    assert row["reason"] == "partial_package"
    assert row["partial_package"] is True
    assert row["admitted"] is False
    assert row["disposition"] is None
    # 中文消息里带上指向的路径，便于定位坏包
    assert "current 指向的包目录不存在" in row["error"]
    assert "packs/does-not-exist-r9" in row["error"]
    assert row["snapshot_dir"] is None
    assert not (tmp_path / "snapshots").exists()


def test_adapter_is_read_only_on_producer_tree_and_idempotent(tmp_path: Path):
    root = tmp_path / "root"
    episode_root, _ = build_pack(root)
    before = _tree_digest(episode_root)
    assert before  # 生产端确实有内容可比

    first = _adapter(episode_root, tmp_path)
    assert first["state"] == "admitted"
    assert first["decision"] == "ingest"
    assert first["disposition"] == "ready"
    assert first["admitted"] is True
    assert Path(first["snapshot_dir"]).is_dir()

    # ★ 只读性：账本与快照只落在 Haike 侧，生产端目录树逐字节不变
    assert _tree_digest(episode_root) == before
    assert not list(episode_root.rglob("snapshot.json"))
    ledger_path = tmp_path / "ledger.json"
    snapshot_root = tmp_path / "snapshots"
    assert ledger_path.is_file()
    # 账本与快照都在生产端 episode 目录之外
    assert episode_root.resolve() not in ledger_path.resolve().parents
    assert episode_root.resolve() not in snapshot_root.resolve().parents

    ledger_after_first = _ledger(ledger_path)
    history_after_first = list(ledger_after_first["episodes"][EPISODE_ID]["history"])
    snapshots_after_first = sorted(path.name for path in (tmp_path / "snapshots").iterdir())

    # ★ 幂等：同一 current 再跑一次，账本不追加、快照目录不新增
    second = _adapter(episode_root, tmp_path)
    assert second["decision"] == "noop"
    assert second["state"] == "admitted"
    assert second["snapshot_dir"] == first["snapshot_dir"]
    assert _tree_digest(episode_root) == before

    ledger_after_second = _ledger(ledger_path)
    assert ledger_after_second["episodes"][EPISODE_ID]["history"] == history_after_first
    assert ledger_after_second["episodes"][EPISODE_ID]["highest_revision"] == 1
    assert sorted(path.name for path in (tmp_path / "snapshots").iterdir()) == snapshots_after_first
