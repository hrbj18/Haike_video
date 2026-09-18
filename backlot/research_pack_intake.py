"""At-least-once Intake/Reconcile：把已发布的研究包收进 Haike 侧账本与快照。

职责边界（刻意保持轻量，不进重型媒体队列）：

* **不是**生产者：只读 CopySkill 已发布目录，永不写生产端。
* 状态与处置分离：``state`` 走 ``discovered → validated → admitted``，旁路
  ``failed`` / ``rejected`` / ``stale``；``disposition`` 走
  ``ready`` / ``partial`` / ``research_required`` / ``rejected``。
  技术无效（``failed``）**不产生** disposition。
* 账本按 revision 追加历史，绝不覆盖：每个 revision 一行，幂等键
  ``episode-research-pack-v1:<episode_id>:<content_sha256>``。
* Reconcile 可在启动 / 定时 / notify 时反复调用；同一 current 绝不重复快照。

账本默认 ``.backlot/research_pack_intake.json``，路径可注入（测试用 tmp_path）。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from lib.paths import REPO_ROOT

from backlot.copy_skill_research_pack import (
    CONTRACT_KIND,
    CURRENT_NAME,
    ResearchPackError,
    ResearchPackPartialError,
    load_research_pack,
    read_current_pointer,
    snapshot_research_pack,
)

LEDGER_SCHEMA = "episode-research-pack-intake-v1"
LEDGER_VERSION = 1
DEFAULT_LEDGER_PATH = REPO_ROOT / ".backlot" / "research_pack_intake.json"
DEFAULT_SNAPSHOT_ROOT = REPO_ROOT / ".backlot" / "research-pack-snapshots"
EPISODE_SUFFIX = "_研究包"

#: 状态机：正常梯级 + 旁路。
STATES: tuple[str, ...] = ("discovered", "validated", "admitted", "failed", "rejected", "stale")

#: revision 决策取值。
DECISIONS: tuple[str, ...] = (
    "ingest",
    "noop",
    "stale_revision",
    "revision_collision",
    "revision_gap",
    "invalid_contract",
)

_MAX_EVENTS = 200


def _now_iso(now: object = None) -> str:
    if isinstance(now, datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        moment = datetime.now(timezone.utc)
    return moment.isoformat(timespec="seconds")


def idempotency_key(episode_id: str, content_sha256: str) -> str:
    return f"{CONTRACT_KIND}:{episode_id}:{content_sha256}"


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


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


def _empty_ledger() -> dict[str, Any]:
    return {"schema": LEDGER_SCHEMA, "version": LEDGER_VERSION, "updated_at": None, "episodes": {}}


class ResearchPackLedger:
    """JSON 账本；损坏时容忍为空账本（快照复用保证重入幂等）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_LEDGER_PATH
        self.warning: str | None = None
        self.loaded: dict[str, Any] = _empty_ledger()

    def load(self) -> dict[str, Any]:
        self.warning = None
        if not self.path.is_file():
            return _empty_ledger()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.warning = f"账本损坏，已按空账本继续：{exc}"
            return _empty_ledger()
        if not isinstance(payload, dict) or not isinstance(payload.get("episodes"), dict):
            self.warning = "账本结构非法，已按空账本继续"
            return _empty_ledger()
        payload.setdefault("schema", LEDGER_SCHEMA)
        payload.setdefault("version", LEDGER_VERSION)
        payload.setdefault("updated_at", None)
        return payload

    def save(self, ledger: Mapping[str, Any]) -> None:
        _atomic_json(self.path, dict(ledger))


def _episode_entry(ledger: dict[str, Any], episode_id: str) -> dict[str, Any] | None:
    episodes = ledger.setdefault("episodes", {})
    entry = episodes.get(episode_id)
    return entry if isinstance(entry, dict) else None


def _ensure_episode_entry(ledger: dict[str, Any], episode_id: str, *, business_date: str = "") -> dict[str, Any]:
    episodes = ledger.setdefault("episodes", {})
    entry = episodes.get(episode_id)
    if not isinstance(entry, dict):
        entry = {
            "episode_id": episode_id,
            "business_date": business_date,
            "highest_revision": 0,
            "current": None,
            "history": [],
            "events": [],
        }
        episodes[episode_id] = entry
    if business_date and not entry.get("business_date"):
        entry["business_date"] = business_date
    return entry


def _append_event(entry: dict[str, Any], *, kind: str, message: str, revision: object = None,
                  content_sha256: object = None, at: str | None = None) -> None:
    events = entry.setdefault("events", [])
    events.append(
        {
            "at": at or _now_iso(),
            "kind": kind,
            "revision": revision,
            "content_sha256": content_sha256,
            "message": message,
        }
    )
    del events[: max(0, len(events) - _MAX_EVENTS)]


def _known_hashes(entry: Mapping[str, Any]) -> set[str]:
    hashes: set[str] = set()
    current = entry.get("current")
    if isinstance(current, Mapping) and current.get("content_sha256"):
        hashes.add(str(current["content_sha256"]))
    for row in entry.get("history") or []:
        if isinstance(row, Mapping) and row.get("content_sha256"):
            hashes.add(str(row["content_sha256"]))
    return hashes


def classify_revision(
    entry: Mapping[str, Any] | None,
    revision: int,
    content_sha256: str,
) -> dict[str, Any]:
    """把 (revision, content_sha256) 与账本历史比对，返回决策。

    规则（冻结）：

    * 与 current 同 hash → ``noop``（内容未变）
    * 命中历史 hash 但 revision 更高 → ``invalid_contract``（升 revision 复用旧内容）
    * 命中历史 hash 且 revision 未升 → ``noop``（已见 hash）
    * revision 低于历史最高 → ``stale_revision``（current 回退，不回退账本）
    * revision 等于历史最高但 hash 不同 → ``revision_collision``
    * revision 跳号（> 最高+1）→ ``revision_gap``（默认不生产）
    * 其余（最高+1）→ ``ingest``
    """
    revision = int(revision)
    current = entry.get("current") if isinstance(entry, Mapping) else None
    current = current if isinstance(current, Mapping) else None
    highest = int((entry or {}).get("highest_revision") or 0)
    history_hashes = _known_hashes(entry or {}) - (
        {str(current["content_sha256"])} if current and current.get("content_sha256") else set()
    )

    if current is not None and str(current.get("content_sha256") or "") == content_sha256:
        return {"decision": "noop", "reason": "content_unchanged"}

    if content_sha256 in history_hashes:
        if revision > highest:
            return {"decision": "invalid_contract", "reason": "bumped_revision_reuses_old_content"}
        return {"decision": "noop", "reason": "seen_content"}

    if revision < highest:
        return {"decision": "stale_revision", "reason": "revision_below_highest"}

    if revision == highest:
        return {"decision": "revision_collision", "reason": "same_revision_different_content"}

    if revision > highest + 1:
        return {"decision": "revision_gap", "reason": "revision_gap_not_produced"}

    return {"decision": "ingest", "reason": "next_revision"}


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------


def _ledger_stale_roots(base: Path, *, business_date: str | None, ledger: Mapping[str, Any]) -> list[Path]:
    """账本已见、但当前 ``current.json`` 缺失的 episode 根：判为 stale 但仍需回访。"""
    extra: list[Path] = []
    episodes = ledger.get("episodes") if isinstance(ledger, Mapping) else None
    if not isinstance(episodes, Mapping):
        return extra
    for episode_id, entry in episodes.items():
        if not isinstance(entry, Mapping) or not entry.get("current"):
            continue
        entry_date = str(entry.get("business_date") or "")
        if not entry_date or (business_date and entry_date != business_date):
            continue
        candidate = base / f"{entry_date}{EPISODE_SUFFIX}" / str(episode_id)
        if candidate.is_dir() and not (candidate / CURRENT_NAME).is_file():
            extra.append(candidate)
    return extra


def discover_episode_roots(root: str | Path, *, business_date: str | None = None) -> list[Path]:
    """扫描 ``<date>_研究包/*/current.json``，返回含 current 的 episode 根目录。"""
    base = Path(root)
    if not base.is_dir():
        return []
    if business_date:
        date_dirs = [base / f"{business_date}{EPISODE_SUFFIX}"]
    else:
        date_dirs = sorted(path for path in base.glob(f"*{EPISODE_SUFFIX}") if path.is_dir())
    roots: list[Path] = []
    for date_dir in date_dirs:
        if not date_dir.is_dir():
            continue
        for episode in sorted(date_dir.iterdir()):
            if episode.is_dir() and (episode / CURRENT_NAME).is_file():
                roots.append(episode)
    return roots


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


def _record_admission(
    entry: dict[str, Any],
    *,
    loaded: Mapping[str, Any],
    snapshot_dir: str,
    now: object,
    at: str,
) -> None:
    revision = int(loaded["revision"])
    row = {
        "revision": revision,
        "pack_id": loaded.get("pack_id"),
        "content_sha256": loaded.get("content_sha256"),
        "manifest_sha256": loaded.get("manifest_sha256"),
        "state": "admitted",
        "disposition": loaded.get("disposition"),
        "partial_package": bool(loaded.get("partial_package")),
        "snapshot_dir": snapshot_dir,
        "idempotency_key": idempotency_key(str(loaded["episode_id"]), str(loaded["content_sha256"])),
        "admitted_at": at,
    }
    history = entry.setdefault("history", [])
    history = [item for item in history if int(item.get("revision") or -1) != revision]
    history.append(row)
    history.sort(key=lambda item: int(item.get("revision") or 0))
    entry["history"] = history
    entry["highest_revision"] = max(int(entry.get("highest_revision") or 0), revision)
    entry["current"] = {
        "revision": revision,
        "pack_id": loaded.get("pack_id"),
        "content_sha256": loaded.get("content_sha256"),
        "manifest_sha256": loaded.get("manifest_sha256"),
        "state": "admitted",
        "disposition": loaded.get("disposition"),
        "partial_package": bool(loaded.get("partial_package")),
        "snapshot_dir": snapshot_dir,
        "admitted_at": at,
    }
    entry["business_date"] = str(loaded.get("business_date") or entry.get("business_date") or "")
    _append_event(
        entry, kind="admitted", revision=revision, content_sha256=loaded.get("content_sha256"),
        message=f"revision r{revision} 已入库并快照", at=at,
    )


def _finalize(result: dict[str, Any]) -> dict[str, Any]:
    """入库与否只由最终 state 决定，避免各分支口误。"""
    result["admitted"] = result.get("state") == "admitted"
    return result


def intake_episode(
    episode_root: str | Path,
    *,
    ledger: ResearchPackLedger | None = None,
    ledger_path: str | Path | None = None,
    snapshot_root: str | Path | None = None,
    now: object = None,
) -> dict[str, Any]:
    """对单个 episode 根做一次 at-least-once 收编，返回分类结果。

    ``ledger`` 传入时由调用方负责最终 ``save``（reconcile 批量复用）；否则本函数
    自行加载并落盘。
    """
    episode_root = Path(episode_root)
    store = ledger if ledger is not None else ResearchPackLedger(ledger_path)
    own_ledger = ledger is None
    data = store.load() if own_ledger else store.loaded
    snap_root = Path(snapshot_root) if snapshot_root else DEFAULT_SNAPSHOT_ROOT
    at = _now_iso(now)

    result: dict[str, Any] = {
        "episode_root": str(episode_root),
        "episode_id": episode_root.name,
        "state": "discovered",
        "disposition": None,
        "partial_package": False,
        "decision": None,
        "reason": None,
        "revision": None,
        "content_sha256": None,
        "snapshot_dir": None,
        "admitted": False,
    }

    try:
        pointer = read_current_pointer(episode_root)
    except ResearchPackError as exc:
        entry = _ensure_episode_entry(data, episode_root.name)
        _append_event(entry, kind="failed", message=f"current.json 无法读取：{exc}", at=at)
        result.update(state="failed", reason="current_unreadable", error=str(exc))
        if own_ledger:
            store.save(data)
        return _finalize(result)

    entry = _episode_entry(data, episode_root.name)
    if pointer is None:
        if entry is not None and entry.get("current"):
            _append_event(entry, kind="stale", revision=entry["current"].get("revision"),
                          content_sha256=entry["current"].get("content_sha256"),
                          message="生产端 current.json 缺失，判定为 stale（保留旧快照）", at=at)
            result.update(
                state="stale",
                reason="current_missing",
                disposition=entry["current"].get("disposition"),
                revision=entry["current"].get("revision"),
                content_sha256=entry["current"].get("content_sha256"),
                snapshot_dir=entry["current"].get("snapshot_dir"),
            )
        else:
            result.update(state="discovered", reason="current_missing")
        if own_ledger:
            store.save(data)
        return _finalize(result)

    try:
        loaded = load_research_pack(episode_root, now=now)
    except ResearchPackPartialError as exc:
        # 技术/运输损坏（缺文件、JSON 截断、字节/SHA 不符、manifest 不覆盖）：
        # 这不是研究缺口，也不是 disposition 降级——严禁 Intake、严禁快照、严禁 disposition。
        entry = _ensure_episode_entry(
            data, str(pointer.get("episode_id") or episode_root.name),
            business_date=str(pointer.get("business_date") or ""),
        )
        _append_event(entry, kind="partial_package", revision=pointer.get("revision"),
                      content_sha256=pointer.get("content_sha256"),
                      message=f"研究包技术/运输损坏，拒绝 Intake：{exc}", at=at)
        result.update(
            state="failed",
            reason="partial_package",
            error=str(exc),
            partial_package=True,
            episode_id=str(pointer.get("episode_id") or episode_root.name),
            revision=pointer.get("revision"),
            content_sha256=pointer.get("content_sha256"),
        )
        if own_ledger:
            store.save(data)
        return _finalize(result)
    except ResearchPackError as exc:
        entry = _ensure_episode_entry(
            data, str(pointer.get("episode_id") or episode_root.name),
            business_date=str(pointer.get("business_date") or ""),
        )
        _append_event(entry, kind="failed", revision=pointer.get("revision"),
                      content_sha256=pointer.get("content_sha256"),
                      message=f"合同技术无效：{exc}", at=at)
        result.update(
            state="failed",
            reason=exc.code,
            error=str(exc),
            episode_id=str(pointer.get("episode_id") or episode_root.name),
            revision=pointer.get("revision"),
            content_sha256=pointer.get("content_sha256"),
        )
        if own_ledger:
            store.save(data)
        return _finalize(result)

    episode_id = str(loaded["episode_id"])
    revision = int(loaded["revision"])
    csha = str(loaded["content_sha256"])
    entry = _ensure_episode_entry(data, episode_id, business_date=str(loaded["business_date"]))

    result.update(
        episode_id=episode_id,
        revision=revision,
        content_sha256=csha,
        disposition=loaded["disposition"],
        partial_package=bool(loaded["partial_package"]),
        state="validated",
    )

    # 权利/产品门可以否决整包：技术有效但不得生产。
    if loaded["disposition"] == "rejected":
        _append_event(entry, kind="rejected", revision=revision, content_sha256=csha,
                      message=f"rights/product gate：{loaded['disposition_reason']}", at=at)
        result.update(state="rejected", reason=loaded["disposition_reason"])
        if own_ledger:
            store.save(data)
        return _finalize(result)

    decision = classify_revision(entry, revision, csha)
    result["decision"] = decision["decision"]
    result["reason"] = decision["reason"]

    if decision["decision"] != "ingest":
        kind = decision["decision"]
        if kind == "revision_collision":
            result.update(state="failed", disposition=None)
        elif kind == "invalid_contract":
            result.update(state="failed")
        elif kind == "revision_gap":
            result.update(state="validated")
        else:  # noop / stale_revision：沿用账本里已入账的结论与快照
            current = entry.get("current") if isinstance(entry.get("current"), Mapping) else None
            if current:
                result.update(
                    state="admitted",
                    disposition=current.get("disposition"),
                    partial_package=bool(current.get("partial_package")),
                    snapshot_dir=current.get("snapshot_dir"),
                )
            else:
                result.update(state="validated")
        _append_event(entry, kind=kind, revision=revision, content_sha256=csha,
                      message=f"{kind}：{decision['reason']}", at=at)
        if own_ledger:
            store.save(data)
        return _finalize(result)

    snapshot = snapshot_research_pack(loaded, snap_root, now=now)
    _record_admission(entry, loaded=loaded, snapshot_dir=snapshot["snapshot_dir"], now=now, at=at)
    result.update(
        state="admitted",
        admitted=True,
        snapshot_dir=snapshot["snapshot_dir"],
        snapshot_reused=bool(snapshot.get("reused")),
    )
    if own_ledger:
        store.save(data)
    return result


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def reconcile(
    root_or_current: str | Path,
    *,
    business_date: str | None = None,
    ledger_path: str | Path | None = None,
    snapshot_root: str | Path | None = None,
    now: object = None,
) -> dict[str, Any]:
    """批量对账：支持传入研究包根目录、单个 episode 根或单个 current.json。

    同一 current 重复调用是幂等的：已入账的 hash 直接 ``noop``，不会再快照。
    """
    target = Path(root_or_current)
    store = ResearchPackLedger(ledger_path)
    store.loaded = store.load()

    if target.is_file() and target.name == CURRENT_NAME:
        episode_roots = [target.parent]
    elif (target / CURRENT_NAME).is_file():
        episode_roots = [target]
    else:
        episode_roots = discover_episode_roots(target, business_date=business_date)
        episode_roots.extend(
            _ledger_stale_roots(target, business_date=business_date, ledger=store.loaded)
        )

    at = _now_iso(now)
    results = [
        intake_episode(episode, ledger=store, snapshot_root=snapshot_root, now=now)
        for episode in episode_roots
    ]
    store.loaded["updated_at"] = at
    if results or store.loaded.get("episodes"):
        store.save(store.loaded)

    summary = {
        "schema": LEDGER_SCHEMA,
        "scanned": len(episode_roots),
        "at": at,
        "ledger_path": str(store.path),
        "snapshot_root": str(Path(snapshot_root) if snapshot_root else DEFAULT_SNAPSHOT_ROOT),
        "results": results,
        "counts": {state: sum(1 for row in results if row["state"] == state) for state in STATES},
    }
    if store.warning:
        summary["warning"] = store.warning
    return summary


def intake_from_current_json(
    episode_root: str | Path,
    *,
    ledger: ResearchPackLedger | None = None,
    ledger_path: str | Path | None = None,
    snapshot_root: str | Path | None = None,
    now: object = None,
) -> dict[str, Any]:
    """单一 episode 的显式收编边界（队列之外，只读）。

    读 ``<episode_root>/current.json`` → ``pack_path``，其余一律不碰：不扫
    ``.staging``、不读 ``latest.json``、不写生产端目录。幂等：同一
    ``content_sha256`` 重复调用只记 ``noop``。

    为什么**不**把它注册成统一生产队列的任务类型：``production_queue`` 的
    ``QUEUE_KINDS`` 三个 kind 都要求 ``projects_root/<project_id>`` 下的媒体生产
    项目，worker 的 handler 直接驱动预览/合成付费流程；而 intake 的终点是账本 +
    editorial snapshot，不产出项目也不产出成片。这条隔离由
    ``tests/backlot/test_research_pack_cross_repo.py`` 显式守住（禁止 intake 模块
    import ``production_queue``）。需要调度时由外部调用本函数或
    ``python -m backlot research-pack``，语义等价、不引入队列耦合。
    """
    return intake_episode(
        episode_root,
        ledger=ledger,
        ledger_path=ledger_path,
        snapshot_root=snapshot_root,
        now=now,
    )
