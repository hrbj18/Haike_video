"""编辑判决落盘（``backlot/remake_editorial_verdict.py``）测试。

正题：消费端 CLI 在**每个出口**（0 / 2 / 3 / 4）都把一份判决落盘，供上游**只读**消费；
判决带 ``stage`` + ``blockers[]`` 原文、``episode_id`` / ``pack_id`` / ``content_sha256`` /
``decided_at`` / ``copywriter_injected``；判决与「成功成稿」产物**物理分开**。

红线（本文件逐条锁死）：
1. 退出码 ``0/2/3/4`` 的语义**一个都没变**；
2. 既有产物结构没变（``<out_dir>`` 里仍然只有 spec 与 provenance 两个文件）；
3. 判决落盘失败**不改变退出码、不让 CLI 崩**（判决是旁路观测物）；
4. 判决绝不写进仓库 ``.backlot``——``out_dir`` 关进 tmp_path 时它跟着走。

全程离线：用 ``build_pack`` 造与生产端同构的研究包，不联网、不调 TTS/LLM、不进生产队列。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backlot import remake_editorial_verdict as verdict
from scripts import remake_editorial_cli as cli
from tests.backlot.test_copy_skill_research_pack import (
    EPISODE_ID,
    build_pack,
    default_argument_node,
    default_payloads,
    with_argument_nodes,
)

NOW = "2026-09-16T23:00:00+08:00"
REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# fixture：合规研究包（含可渲染素材），与 CLI 测试同构
# --------------------------------------------------------------------------- #
def renderable_payloads(*, disposition: str = "ready") -> dict:
    """把权威 fixture 的平台素材合法改造成「可渲染」资产（producer_owned + cleared）。"""
    payloads = default_payloads(disposition=disposition)
    record = payloads["rights.json"]["rights"][0]
    record["origin"] = "producer_owned"
    record["rights_status"] = "cleared"
    record["render_eligible"] = True
    record["redistribution_allowed"] = True
    return with_argument_nodes(payloads, default_argument_node())


@pytest.fixture
def episode_root(tmp_path: Path) -> Path:
    root, _ = build_pack(tmp_path / "root", payloads=renderable_payloads())
    return root


def _run(episode_root: Path, tmp_path: Path, **kwargs) -> dict:
    """默认把账本/快照/输出全部关进 tmp_path ⇒ 判决也跟着进 tmp_path，绝不碰仓库 .backlot。"""
    kwargs.setdefault("ledger_path", tmp_path / "ledger.json")
    kwargs.setdefault("snapshot_root", tmp_path / "snapshots")
    kwargs.setdefault("out_dir", tmp_path / "specs")
    kwargs.setdefault("now", NOW)
    return cli.run(episode_root, **kwargs)


def _verdict_file(tmp_path: Path, episode_id: str = EPISODE_ID) -> Path:
    return tmp_path / verdict.VERDICT_DIRNAME / f"{episode_id}.json"


def _verdict(tmp_path: Path, episode_id: str = EPISODE_ID) -> dict:
    return json.loads(_verdict_file(tmp_path, episode_id).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 判决词 ↔ 退出码：语义一个字都不能变
# --------------------------------------------------------------------------- #
def test_exit_code_to_verdict_map_matches_cli_constants():
    """判决词表与 CLI 的 ``EXIT_*`` 字面量必须一一对应（两处刻意重复，靠本用例锁死）。"""
    assert verdict.EXIT_CODE_VERDICTS == {
        cli.EXIT_OK: "produced",
        cli.EXIT_INPUT: "rejected",
        cli.EXIT_BLOCKED: "blocked",
        cli.EXIT_NOT_SCRIPTED: "draft",
    }
    assert (cli.EXIT_OK, cli.EXIT_INPUT, cli.EXIT_BLOCKED, cli.EXIT_NOT_SCRIPTED) == (0, 2, 3, 4)


def test_build_verdict_does_not_guess_unknown_exit_codes():
    payload = verdict.build_verdict(exit_code=7, stage="?", reason="?")
    assert payload["verdict"] == "unknown"
    assert payload["exit_code"] == 7
    assert verdict.verdict_for_exit(None) == "unknown"
    assert verdict.verdict_for_exit("4") == "draft"


# --------------------------------------------------------------------------- #
# 判决目录：与成稿产物物理分开，且跟着 out_dir 走
# --------------------------------------------------------------------------- #
def test_default_verdict_dir_is_a_sibling_of_the_default_spec_dir():
    assert cli.DEFAULT_SPEC_DIR == REPO / ".backlot" / "remake-specs"
    assert verdict.DEFAULT_VERDICT_DIR == REPO / ".backlot" / verdict.VERDICT_DIRNAME
    assert verdict.DEFAULT_VERDICT_DIR.parent == cli.DEFAULT_SPEC_DIR.parent
    assert verdict.DEFAULT_VERDICT_DIR != cli.DEFAULT_SPEC_DIR


def test_verdict_dir_follows_out_dir_into_the_temp_tree(tmp_path: Path):
    assert verdict.verdict_dir_for(tmp_path / "specs") == tmp_path / verdict.VERDICT_DIRNAME
    assert verdict.verdict_dir_for(None) == verdict.DEFAULT_VERDICT_DIR
    # 单段相对路径（"specs"）没有可信的父目录 ⇒ 退回仓库默认，不瞎猜。
    assert verdict.verdict_dir_for("specs") == verdict.DEFAULT_VERDICT_DIR


def test_safe_episode_name_never_escapes_the_verdict_dir():
    assert verdict.safe_episode_name("2026-09-16-测试主题") == "2026-09-16-测试主题"
    for hostile in ("a/b", "a\\b", "..", "", None, "  "):
        name = verdict.safe_episode_name(hostile)
        assert "/" not in name and "\\" not in name and name not in {"", ".", ".."}
    assert verdict.safe_episode_name("") == "unknown-episode"


def test_write_verdict_swallows_disk_failures(tmp_path: Path):
    """判决落盘失败必须返回 ``None`` 而不抛——它是旁路观测物，不允许弄崩调用方。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("占位文件，不是目录", encoding="utf-8")
    payload = verdict.build_verdict(exit_code=cli.EXIT_INPUT, stage="input", reason="坏输入")
    assert verdict.write_verdict(payload, verdict_dir=blocked / "sub") is None


# --------------------------------------------------------------------------- #
# 出口 0 ⇒ produced
# --------------------------------------------------------------------------- #
def test_produced_verdict_carries_spec_path_and_identity(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path, copywriter_spec="stub")
    assert report["exit_code"] == cli.EXIT_OK
    assert Path(report["verdict_path"]) == _verdict_file(tmp_path)

    payload = _verdict(tmp_path)
    assert payload["schema"] == verdict.VERDICT_SCHEMA
    assert payload["verdict"] == "produced"
    assert payload["exit_code"] == cli.EXIT_OK
    assert payload["stage"] == "remake_spec_ready"
    assert payload["reason"] == report["message"]
    assert payload["spec_path"] == report["spec_path"]
    assert Path(payload["spec_path"]).is_file()
    assert payload["spec_written"] is True
    assert payload["episode_id"] == EPISODE_ID
    assert payload["pack_id"] == f"{EPISODE_ID}-r1"
    assert payload["revision"] == 1
    assert payload["content_sha256"] == report["content_sha256"]
    assert payload["snapshot_dir"] == report["intake"]["snapshot_dir"]
    assert payload["copywriter"] == "stub"
    assert payload["copywriter_injected"] is True
    assert payload["blockers"] == []
    assert payload["decided_at"]

    # 既有成稿产物结构没变：spec 目录里仍然只有 spec 与 provenance 两个文件。
    assert sorted(path.name for path in (tmp_path / "specs").iterdir()) == [
        f"{EPISODE_ID}.json",
        f"{EPISODE_ID}.provenance.json",
    ]
    # 判决与成稿产物物理分开。
    assert not Path(report["verdict_path"]).is_relative_to(tmp_path / "specs")


# --------------------------------------------------------------------------- #
# 出口 4 ⇒ draft（说明停在 argument_map_ready）
# --------------------------------------------------------------------------- #
def test_draft_verdict_says_it_stopped_at_argument_map_ready(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path)  # 不注入 copywriter
    assert report["exit_code"] == cli.EXIT_NOT_SCRIPTED

    payload = _verdict(tmp_path)
    assert payload["verdict"] == "draft"
    assert payload["exit_code"] == cli.EXIT_NOT_SCRIPTED
    assert payload["stage"] == "argument_map_ready"
    assert "argument_map_ready" in payload["reason"]
    assert payload["copywriter"] == "none"
    assert payload["copywriter_injected"] is False
    assert payload["spec_path"] is None
    assert payload["spec_written"] is False
    assert not (tmp_path / "specs").exists()


# --------------------------------------------------------------------------- #
# 出口 3 ⇒ blocked（stage + blockers[] 原文）
# --------------------------------------------------------------------------- #
def test_blocked_verdict_carries_stage_and_blockers_verbatim(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path, copywriter_spec="stub", min_supported_claims=5)
    assert report["exit_code"] == cli.EXIT_BLOCKED

    payload = _verdict(tmp_path)
    assert payload["verdict"] == "blocked"
    assert payload["exit_code"] == cli.EXIT_BLOCKED
    # stage 原样透传（具体停在哪一阶段由 ``remake_editorial`` 决定，这里只锁「不丢、不改写」）。
    assert payload["stage"] == report["stage"]
    assert payload["stage"] and isinstance(payload["stage"], str)
    assert payload["reason"] == report["message"]
    assert payload["blockers"] == report["blockers"]
    assert payload["blockers"]
    assert any("低于最低" in item or "少于最低" in item for item in payload["blockers"])


def test_blocked_verdict_keeps_the_non_ready_disposition_reason(tmp_path: Path):
    """非 ``ready`` 处置的阻断原文（``remake_editorial`` 关于 disposition 的原话）要进来。"""
    root, _ = build_pack(tmp_path / "root", payloads=renderable_payloads(disposition="research_required"))
    report = _run(root, tmp_path, copywriter_spec="stub")
    assert report["exit_code"] == cli.EXIT_BLOCKED

    payload = _verdict(tmp_path)
    assert payload["verdict"] == "blocked"
    assert payload["stage"] == "gate"
    assert payload["blockers"] == report["blockers"]
    assert any("research_required" in item for item in payload["blockers"])


# --------------------------------------------------------------------------- #
# 出口 2 ⇒ rejected（校验错误原文）
# --------------------------------------------------------------------------- #
def test_rejected_verdict_carries_validation_errors_verbatim(tmp_path: Path):
    # 平台素材不得 render_eligible ⇒ 成稿后仍无可用素材窗口：这是「校验错误原文」的出口。
    root, _ = build_pack(
        tmp_path / "platform",
        payloads=with_argument_nodes(default_payloads(), default_argument_node()),
    )
    report = _run(root, tmp_path, copywriter_spec="stub")
    assert report["exit_code"] == cli.EXIT_INPUT
    assert report["stage"] == "remake_spec"

    payload = _verdict(tmp_path)
    assert payload["verdict"] == "rejected"
    assert payload["exit_code"] == cli.EXIT_INPUT
    assert payload["stage"] == "remake_spec"
    assert payload["blockers"] == report["blockers"]
    assert any("没有可用素材" in item for item in payload["blockers"])


def test_rejected_verdict_survives_a_missing_pack_location(tmp_path: Path):
    """连 episode 目录都不存在时也要有一份判决（期次名按路径最后一段兜底）。"""
    report = _run(tmp_path / "does-not-exist", tmp_path)
    assert report["exit_code"] == cli.EXIT_INPUT

    payload = _verdict(tmp_path, "does-not-exist")
    assert payload["verdict"] == "rejected"
    assert payload["stage"] == "input"
    assert payload["episode_id"] == "does-not-exist"
    assert "不存在" in payload["reason"]
    assert payload["pack_id"] == ""
    assert payload["content_sha256"] == ""
    assert payload["spec_path"] is None


# --------------------------------------------------------------------------- #
# 旁路观测物的铁律：落盘失败不改变退出码、不影响成稿产物
# --------------------------------------------------------------------------- #
def test_verdict_write_failure_keeps_exit_code_and_spec(episode_root: Path, tmp_path: Path):
    blocked = tmp_path / "blocked"
    blocked.write_text("占位文件，不是目录", encoding="utf-8")
    nowhere = blocked / "sub"

    produced = _run(episode_root, tmp_path, copywriter_spec="stub", verdict_dir=nowhere)
    assert produced["exit_code"] == cli.EXIT_OK  # 退出码一个字都没变
    assert produced["verdict_path"] is None
    assert Path(produced["spec_path"]).is_file()
    assert Path(produced["provenance_path"]).is_file()

    draft = _run(episode_root, tmp_path, verdict_dir=nowhere)
    assert draft["exit_code"] == cli.EXIT_NOT_SCRIPTED
    assert draft["verdict_path"] is None

    blocked_gate = _run(
        episode_root, tmp_path, copywriter_spec="stub", min_supported_claims=5, verdict_dir=nowhere
    )
    assert blocked_gate["exit_code"] == cli.EXIT_BLOCKED
    assert blocked_gate["verdict_path"] is None
    assert blocked_gate["blockers"]


def test_no_write_still_records_the_verdict(episode_root: Path, tmp_path: Path):
    """``--no-write`` 只抑制成稿产物；判决是旁路观测物，照落。"""
    report = _run(episode_root, tmp_path, copywriter_spec="stub", write=False)
    assert report["exit_code"] == cli.EXIT_OK
    assert not (tmp_path / "specs").exists()

    payload = _verdict(tmp_path)
    assert payload["verdict"] == "produced"
    assert payload["spec_path"] is None
    assert payload["spec_written"] is False


def test_verdict_dir_override_wins(episode_root: Path, tmp_path: Path):
    custom = tmp_path / "elsewhere"
    report = _run(episode_root, tmp_path, copywriter_spec="stub", verdict_dir=custom)
    assert report["exit_code"] == cli.EXIT_OK
    assert Path(report["verdict_path"]) == custom / f"{EPISODE_ID}.json"
    assert not (tmp_path / verdict.VERDICT_DIRNAME).exists()


# --------------------------------------------------------------------------- #
# 同一期次每个出口都覆盖写同一份判决（不堆历史、不串期次）
# --------------------------------------------------------------------------- #
def test_every_exit_overwrites_the_single_verdict_file_for_that_episode(episode_root: Path, tmp_path: Path):
    _run(episode_root, tmp_path)
    assert _verdict(tmp_path)["verdict"] == "draft"

    _run(episode_root, tmp_path, copywriter_spec="stub", min_supported_claims=5)
    assert _verdict(tmp_path)["verdict"] == "blocked"

    _run(episode_root, tmp_path, copywriter_spec="stub")
    assert _verdict(tmp_path)["verdict"] == "produced"

    assert sorted(path.name for path in (tmp_path / verdict.VERDICT_DIRNAME).iterdir()) == [
        f"{EPISODE_ID}.json"
    ]
