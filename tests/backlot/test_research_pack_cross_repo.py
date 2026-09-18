"""跨仓离线集成测试：CopySkill 真实 producer ↔ Haike Intake/Reconcile。

与 ``test_copy_skill_research_pack.py`` / ``test_research_pack_intake.py``（单元级 +
golden 包）互补，本模块专门覆盖**跨仓**合同：

* 真实 producer 发布多 revision → Haike 逐 revision 收编且历史不覆盖。
* producer 重复发布相同内容 → 双方都是 ``noop``。
* 丢通知（``notify/`` / ``latest.json`` 缺失）仍能被 reconcile 发现（因为我们只认
  ``current.json``，从不依赖通知或根 ``latest.json``）。
* 半包 / 坏 hash 的真实 producer 产物 → 技术运输损坏，**不得 Intake**。
* 重启幂等：快照已提交、账本丢失 → 复用快照，不产生第二份。
* 隔离：Haike 只读，绝不修改 producer 目录；与旧 hotspot feed 合同/根完全隔离；
  不进入重型媒体队列，不创建项目或最终文案（``review_ready`` 由旧链路负责，本链路不发布）。

全部离线：不触网、不调模型、不付费。真实 producer 不在本机时相关用例自动 skip。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backlot import copy_skill_research_pack as rp
from backlot import research_pack_intake as rpi
from lib.paths import REPO_ROOT
from tests.backlot.test_copy_skill_research_pack import (
    BUSINESS_DATE,
    EPISODE_ID,
    _producer_module,
    _read,
    _sha,
    build_pack,
    default_payloads,
)

CLOCK = f"{BUSINESS_DATE}T23:00:00+08:00"


def _publish(out: Path, payloads: dict, producer) -> dict:
    return producer.publish_episode_research_pack(
        {},
        episode_id=EPISODE_ID,
        semantic=payloads,
        business_date=BUSINESS_DATE,
        output_root=str(out),
        clock=lambda: CLOCK,
    )


def _episode_root(out: Path, producer) -> Path:
    return out / f"{BUSINESS_DATE}{producer.EPISODE_SUFFIX}" / EPISODE_ID


def _tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): _sha(p) for p in root.rglob("*") if p.is_file()}


def _reconcile(root: Path, tmp_path: Path) -> dict:
    return rpi.reconcile(
        root, ledger_path=tmp_path / "ledger.json", snapshot_root=tmp_path / "snapshots"
    )


# --- 真实 producer：多 revision 收编 ---------------------------------------


def test_producer_revision_growth_is_reconciled_without_overwrite(tmp_path: Path):
    producer = _producer_module()
    out = tmp_path / "out"

    assert _publish(out, default_payloads(variant="a"), producer)["status"] == "published"
    first = _reconcile(out, tmp_path)["results"][0]
    assert first["state"] == "admitted"
    assert first["revision"] == 1

    assert _publish(out, default_payloads(variant="b"), producer)["status"] == "published"
    second = _reconcile(out, tmp_path)["results"][0]
    assert second["state"] == "admitted"
    assert second["decision"] == "ingest"
    assert second["revision"] == 2
    assert second["snapshot_dir"] != first["snapshot_dir"]

    ledger = _read(tmp_path / "ledger.json")
    entry = ledger["episodes"][EPISODE_ID]
    assert entry["highest_revision"] == 2
    assert [row["revision"] for row in entry["history"]] == [1, 2]
    assert entry["current"]["revision"] == 2


def test_producer_re_publish_identical_content_is_noop(tmp_path: Path):
    producer = _producer_module()
    out = tmp_path / "out"
    payloads = default_payloads()

    assert _publish(out, payloads, producer)["status"] == "published"
    assert _publish(out, payloads, producer)["status"] == "noop"

    _reconcile(out, tmp_path)
    again = _reconcile(out, tmp_path)["results"][0]
    assert again["decision"] == "noop"
    assert again["state"] == "admitted"

    ledger = _read(tmp_path / "ledger.json")
    assert len(ledger["episodes"][EPISODE_ID]["history"]) == 1


# --- intake → editorial 衔接（薄边界；编辑侧深度用例归 editorial 文件）-------


def test_producer_pack_projects_into_editorial_snapshot_per_revision(tmp_path: Path):
    """真实 producer 的每个 revision 都能投影成合法 normalized editorial snapshot。

    这里只验证 intake 权威层 → editorial 投影层的**边界**（revision/hash/disposition
    随 revision 前进而更新）；snapshot → decision → remake-spec 的深度链路由
    editorial 侧测试负责。
    """
    from backlot.research_pack_snapshot import load_editorial_snapshot

    producer = _producer_module()
    out = tmp_path / "out"
    moment = f"{BUSINESS_DATE}T23:30:00+08:00"

    _publish(out, default_payloads(variant="a"), producer)
    episode_root = _episode_root(out, producer)
    snap1 = load_editorial_snapshot(episode_root, now=moment)
    assert snap1["revision"] == 1
    assert snap1["disposition"] == "ready"
    assert snap1["partial_package"] is False

    _publish(out, default_payloads(variant="b"), producer)
    snap2 = load_editorial_snapshot(episode_root, now=moment)
    assert snap2["revision"] == 2
    assert snap2["content_sha256"] != snap1["content_sha256"]
    assert snap2["claims"][0]["text"] != snap1["claims"][0]["text"]


# --- 丢通知：不依赖 notify / 根 latest -------------------------------------


def test_reconcile_discovers_pack_when_notify_and_latest_are_gone(tmp_path: Path):
    producer = _producer_module()
    out = tmp_path / "out"
    _publish(out, default_payloads(), producer)
    episode_root = _episode_root(out, producer)

    # 模拟「通知 / 根镜像丢失」：我们只认 current.json，应照样发现并收编。
    notify = episode_root / "notify"
    if notify.is_dir():
        for child in notify.glob("*"):
            child.unlink()
    (episode_root / "latest.json").unlink(missing_ok=True)

    summary = _reconcile(out, tmp_path)
    assert summary["scanned"] == 1
    assert summary["counts"]["admitted"] == 1


# --- 半包 / 坏 hash：真实产物上的运输损坏 ----------------------------------


def _published_episode(tmp_path: Path) -> tuple[Path, Path]:
    producer = _producer_module()
    out = tmp_path / "out"
    _publish(out, default_payloads(), producer)
    return out, _episode_root(out, producer)


def test_half_pack_from_producer_is_transport_damage(tmp_path: Path):
    out, episode_root = _published_episode(tmp_path)
    current = _read(episode_root / "current.json")
    pack = episode_root / current["pack_path"]
    (pack / "claims.json").unlink()

    with pytest.raises(rp.ResearchPackPartialError) as excinfo:
        rp.load_research_pack(episode_root)
    assert excinfo.value.code == rp.PARTIAL_PACKAGE

    row = _reconcile(out, tmp_path)["results"][0]
    assert row["state"] == "failed"
    assert row["reason"] == "partial_package"
    assert row["admitted"] is False
    assert row["snapshot_dir"] is None
    assert not (tmp_path / "snapshots").exists()


def test_tampered_hash_from_producer_is_transport_damage(tmp_path: Path):
    out, episode_root = _published_episode(tmp_path)
    current = _read(episode_root / "current.json")
    pack = episode_root / current["pack_path"]
    (pack / "sources.json").write_text('{"contract": "episode-research-pack-v1"}\n', encoding="utf-8")

    with pytest.raises(rp.ResearchPackPartialError):
        rp.load_research_pack(episode_root)

    row = _reconcile(out, tmp_path)["results"][0]
    assert row["state"] == "failed"
    assert row["reason"] == "partial_package"


# --- 重启幂等 ---------------------------------------------------------------


def test_restart_idempotency_reuses_committed_snapshot(tmp_path: Path):
    out, _ = _published_episode(tmp_path)
    first = _reconcile(out, tmp_path)["results"][0]
    snapshot_dir = Path(first["snapshot_dir"])
    assert snapshot_dir.is_dir()

    # 模拟重启：账本丢失，但快照已提交
    (tmp_path / "ledger.json").unlink()
    row = _reconcile(out, tmp_path)["results"][0]
    assert row["state"] == "admitted"
    assert row["snapshot_reused"] is True
    assert Path(row["snapshot_dir"]) == snapshot_dir

    assert len(list((tmp_path / "snapshots" / EPISODE_ID).iterdir())) == 1


# --- 隔离：只读 producer / 与 hotspot 分离 / 不进重型队列 -------------------


INTAKE_MODULES = (
    "backlot/copy_skill_research_pack.py",
    "backlot/research_pack_intake.py",
    "backlot/research_pack_snapshot.py",
)
FORBIDDEN_DEPENDENCY_NAMES = (
    "production_queue",
    "daily_script_v2",
    "remake_build_project",
    "remake_project",
)


def _imported_module_names(path: Path) -> set[str]:
    """用 AST 收集模块**实际 import 的**点分名字（不匹配 docstring/注释里的字符串）。"""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                names.add(base)
            for alias in node.names:
                names.add(f"{base}.{alias.name}" if base else alias.name)
    return names


def test_intake_never_modifies_producer_directory(tmp_path: Path):
    out, _ = _published_episode(tmp_path)
    before = _tree_hashes(out)

    summary = _reconcile(out, tmp_path)
    assert summary["counts"]["admitted"] == 1

    assert _tree_hashes(out) == before


def test_intake_is_isolated_from_legacy_hotspot_feed(tmp_path: Path):
    from backlot import copy_skill_hotspot_feed as hotspot

    # 1) 两套合同 / schema / 账本彼此不共用。
    assert rpi.LEDGER_SCHEMA != hotspot.POOL_SCHEMA
    assert rp.CONTRACT != hotspot.CONTRACT_VERSION

    # 2) 扫描根里塞一个 hotspot 风格的包目录，研究包发现器必须无视它。
    root = tmp_path / "root"
    build_pack(root)
    hotspot_dir = root / f"{BUSINESS_DATE}_研究包" / "hotspot-decoy"
    hotspot_dir.mkdir(parents=True)
    (hotspot_dir / "candidate-pool.json").write_text('{"schema": "daily-hot-candidate-pool-v2"}\n', encoding="utf-8")

    roots = rpi.discover_episode_roots(root)
    assert [p.name for p in roots] == [EPISODE_ID]

    summary = _reconcile(root, tmp_path)
    assert summary["scanned"] == 1
    assert summary["counts"]["admitted"] == 1


def test_intake_modules_do_not_import_heavy_pipeline():
    """冻结依赖方向：intake 层**不 import** 重型队列 / 日更文案 / 建项目模块。

    用 AST 检查真实 import 语句，而非全文子串匹配——避免误伤 docstring 里
    「与某某同构、不改它」这类合法提及（例如 remake_project 的说明文本）。
    """
    for module_name in INTAKE_MODULES:
        imported = _imported_module_names(REPO_ROOT / module_name)
        for dotted in imported:
            parts = set(dotted.split("."))
            for token in FORBIDDEN_DEPENDENCY_NAMES:
                assert token not in parts, f"{module_name} 不应 import {dotted}（命中 {token}）"


def test_intake_produces_no_project_or_final_artifact(tmp_path: Path):
    """收编终点只到 intake：不产出项目/成片，结果里也没有发布类字段。"""
    out, _ = _published_episode(tmp_path)
    row = _reconcile(out, tmp_path)["results"][0]
    for key in ("published", "project_dir", "final_copy", "render", "preview"):
        assert key not in row

    assert not list(out.rglob("*.mp4"))
    assert not list(out.rglob("remake-spec*.json"))
