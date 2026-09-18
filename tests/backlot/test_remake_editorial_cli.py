"""离线 CLI（``scripts/remake_editorial_cli.py``）测试：研究包 → 编辑决策 → remake-spec-v1。

全程离线：用 `build_pack` 造与生产端同构的研究包，copywriter 用内置 stub，
时长探针用每段恒定的离线估算。不联网、不调真实 TTS/LLM、不进生产队列、不写生产端目录。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from backlot.remake_editorial import validate_remake_spec
from scripts import remake_editorial_cli as cli
from tests.backlot.test_copy_skill_research_pack import (
    BUSINESS_DATE,
    EPISODE_ID,
    build_pack,
    default_argument_node,
    default_payloads,
    with_argument_nodes,
)

NOW = "2026-09-16T23:00:00+08:00"
REPO = Path(__file__).resolve().parents[2]
REMAKE_SCHEMA = json.loads((REPO / "schemas" / "remake-spec-v1.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# fixture：合规研究包（含可渲染素材）
# --------------------------------------------------------------------------- #
def renderable_payloads(*, disposition: str = "ready") -> dict:
    """把权威 fixture 的平台素材合法改造成「可渲染」资产（producer_owned + cleared）。

    平台内容不得 render_eligible（权威校验强制），因此成功出镜只能靠非平台来源。
    """
    payloads = default_payloads(disposition=disposition)
    record = payloads["rights.json"]["rights"][0]
    record["origin"] = "producer_owned"
    record["rights_status"] = "cleared"
    record["render_eligible"] = True
    record["redistribution_allowed"] = True
    # 夹具已与生产端同构（``nodes`` 默认 ``[]``）⇒ 显式补上论证节点，否则会在
    # ``theme_selected`` 阶段以「仅 0 条可用论证」阻断（exit 3），到不了后续阶段。
    return with_argument_nodes(payloads, default_argument_node())


@pytest.fixture
def episode_root(tmp_path: Path) -> Path:
    root, _ = build_pack(tmp_path / "root", payloads=renderable_payloads())
    return root


def _run(episode_root: Path, tmp_path: Path, **kwargs) -> dict:
    """默认把账本/快照/输出全部关进 tmp_path，绝不碰仓库里的 .backlot。"""
    kwargs.setdefault("ledger_path", tmp_path / "ledger.json")
    kwargs.setdefault("snapshot_root", tmp_path / "snapshots")
    kwargs.setdefault("out_dir", tmp_path / "specs")
    kwargs.setdefault("now", NOW)
    return cli.run(episode_root, **kwargs)


def _tree(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


# --------------------------------------------------------------------------- #
# 输入错误：中文 + 非零退出码
# --------------------------------------------------------------------------- #
def test_missing_episode_root_is_chinese_error(tmp_path: Path):
    report = _run(tmp_path / "does-not-exist", tmp_path)
    assert report["exit_code"] == cli.EXIT_INPUT
    assert report["stage"] == "input"
    assert "不存在" in report["message"]
    assert not (tmp_path / "specs").exists()
    assert not (tmp_path / "ledger.json").exists()  # 位置检查先于收编，不产生账本


def test_missing_current_json_is_chinese_error(tmp_path: Path):
    root = tmp_path / "root" / f"{BUSINESS_DATE}_研究包" / EPISODE_ID
    root.mkdir(parents=True)
    report = _run(root, tmp_path)
    assert report["exit_code"] == cli.EXIT_INPUT
    assert "current.json" in report["message"]


def test_current_json_path_is_accepted(episode_root: Path, tmp_path: Path):
    report = _run(episode_root / "current.json", tmp_path, copywriter_spec="stub")
    assert report["exit_code"] == cli.EXIT_OK
    assert report["episode_id"] == EPISODE_ID


# --------------------------------------------------------------------------- #
# 编辑门阻断：disposition=research_required / rejected
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("disposition", ["research_required", "rejected"])
def test_non_ready_disposition_is_blocked_by_editorial_gate(disposition: str, tmp_path: Path):
    root, _ = build_pack(tmp_path / "root", payloads=renderable_payloads(disposition=disposition))
    report = _run(root, tmp_path, copywriter_spec="stub")
    assert report["exit_code"] == cli.EXIT_BLOCKED
    assert report["stage"] == "gate"
    assert any(disposition in reason for reason in report["blockers"])
    assert all(reason.strip() for reason in report["blockers"])
    assert "阻断" in report["message"]
    assert not (tmp_path / "specs").exists()


# --------------------------------------------------------------------------- #
# 合同合法的空 selected_topic：必须是「编辑门阻断」（exit 3），不是「输入不合格」（exit 2）
# --------------------------------------------------------------------------- #
def test_null_selected_topic_reaches_the_gate_not_the_input_error(tmp_path: Path):
    """冻结合同明文 ``selected_topic{...}|null``、权威层也显式接受 null。

    因此链路必须**穿过** validated_snapshot 走到编辑门；曾经它卡在 validate 层报
    ``validated_snapshot``（exit 2），把合同合法输入误判成坏输入。
    """
    payloads = renderable_payloads(disposition="research_required")
    payloads["topics.json"]["selected_topic"] = None
    root, _ = build_pack(tmp_path / "root", payloads=payloads)

    report = _run(root, tmp_path, copywriter_spec="stub")
    assert report["exit_code"] == cli.EXIT_BLOCKED
    assert report["stage"] == "gate"
    assert any("selected_topic" in reason for reason in report["blockers"])
    assert report["snapshot"]["disposition"] == "research_required"
    assert not (tmp_path / "specs").exists()


# --------------------------------------------------------------------------- #
# 真实生产形态：生产端 ``build_research_semantic`` 产出的包必须走到编辑门，而不是被拒成坏输入
# --------------------------------------------------------------------------- #
def producer_shaped_payloads() -> dict:
    """与生产端 ``build_research_semantic`` 产出同形的包（逐条对应其真实写法）。

    * ``topics.json`` 是 baseline **原样透传** ⇒ ``argument_graph.nodes`` 恒为空；
    * ``claims`` 至多一条 ``unverified`` / ``hedge`` 断言，``source_ids`` 指向
      ``authority="unknown"`` + ``verification_state="unverified"`` 的 ``pub-*`` 来源；
    * ``episode.disposition`` 沿用 baseline（无第一手事实 ⇒ ``research_required``）。
    """
    payloads = default_payloads(disposition="research_required")
    pub = "pub-0123456789abcdef"
    payloads["sources.json"]["sources"] = [
        {
            "source_id": pub,
            "authority": "unknown",
            "verification_state": "unverified",
            "heat_only": False,
            "publisher": "作者",
            "title": "",
            "url": "https://www.douyin.com/video/123",
            "published_at": "",
            "freshness": {
                "observed_at": "2026-09-16T00:00:00+08:00",
                "policy": "event_window",
                "status_at_publish": "unknown",
            },
            "method": "share_page",
            "excerpt": "证据文本",
        }
    ]
    payloads["claims.json"]["claims"] = [
        {
            "claim_id": "claim-01",
            "topic_id": "topic-01",
            "text": "折叠屏铰链寿命",
            "evidence_status": "unverified",
            "wording_policy": "hedge",
            "freshness_requirement": "fresh",
            "source_ids": [pub],
            "fact_sources_min": 0,
            "fact_sources_present": 0,
            "claims_to_verify": [pub],
            "do_not_claim": ["未经核验的具体数字"],
            "material_refs": [],
        }
    ]
    payloads["topics.json"]["argument_graph"]["nodes"] = []
    payloads["topics.json"]["argument_graph"]["edges"] = []
    payloads["materials.json"]["materials"][0]["segments"][0]["claim_ids"] = []
    return payloads


def test_real_producer_shape_pack_reaches_the_gate(tmp_path: Path):
    """正题：真实包必须**收编成功**并在编辑门 fail-closed（exit 3），不能停在校验层（exit 2）。"""
    root, _ = build_pack(tmp_path / "root", payloads=producer_shaped_payloads())

    report = _run(root, tmp_path, copywriter_spec="stub")
    assert report["intake"]["state"] != "failed"
    assert report["exit_code"] == cli.EXIT_BLOCKED
    assert report["stage"] == "gate"
    assert report["snapshot"]["disposition"] == "research_required"
    assert not (tmp_path / "specs").exists()


# --------------------------------------------------------------------------- #
# 默认不注入 copywriter：只到 argument_map_ready 草案，不落盘规格
# --------------------------------------------------------------------------- #
def test_default_without_copywriter_stops_at_argument_map_ready(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path)
    assert report["exit_code"] == cli.EXIT_NOT_SCRIPTED
    assert report["stage"] == "argument_map_ready"
    assert report["stages"]["argument_map_ready"] is True
    assert report["stages"]["script_ready"] is False
    assert "copywriter" in report["message"]
    assert "spec_path" not in report
    assert not (tmp_path / "specs").exists()


def test_off_reference_means_no_injection(episode_root: Path, tmp_path: Path):
    for token in ("", "none", "off"):
        report = _run(episode_root, tmp_path, copywriter_spec=token)
        assert report["exit_code"] == cli.EXIT_NOT_SCRIPTED, token
        assert report["stages"]["script_ready"] is False, token


def test_bad_copywriter_spec_is_chinese_error(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path, copywriter_spec="not-a-module-spec")
    assert report["exit_code"] == cli.EXIT_INPUT
    assert "--copywriter" in report["message"]


# --------------------------------------------------------------------------- #
# 显式注入 copywriter：产出合法 remake-spec-v1 并落盘
# --------------------------------------------------------------------------- #
def test_injected_stub_copywriter_produces_valid_remake_spec(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path, copywriter_spec="stub")
    assert report["exit_code"] == cli.EXIT_OK
    assert report["stage"] == "remake_spec_ready"
    assert report["stages"]["script_ready"] is True
    assert report["stages"]["duration_measured"] is True
    assert report["stages"]["remake_spec_ready"] is True

    spec_path = Path(report["spec_path"])
    provenance_path = Path(report["provenance_path"])
    assert spec_path == tmp_path / "specs" / f"{EPISODE_ID}.json"
    assert spec_path.is_file() and provenance_path.is_file()

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["schema_version"] == "remake-spec-v1"
    assert spec["pipeline_type"] == "avatar-spokesperson"
    assert spec["sections"] and spec["sources"]
    assert validate_remake_spec(spec, fps=spec["fps"])["valid"] is True
    # 成稿是原创口播，不是 claim 原文的 verbatim 复述。
    claim_text = renderable_payloads()["claims.json"]["claims"][0]["text"]
    assert spec["sections"][0]["text"] != claim_text
    assert spec["sections"][0]["text"].endswith("值得展开说。")

    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(spec, REMAKE_SCHEMA)


def test_injected_copywriter_via_module_path(episode_root: Path, tmp_path: Path):
    reference = "tests.backlot.test_remake_editorial:stub_copywriter"
    report = _run(episode_root, tmp_path, copywriter_spec=reference)
    assert report["exit_code"] == cli.EXIT_OK
    spec = json.loads(Path(report["spec_path"]).read_text(encoding="utf-8"))
    assert spec["sections"][0]["theme_support"].startswith("以「")


def test_offline_probe_is_flagged_in_spec_and_provenance(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path, copywriter_spec="stub", probe_seconds=3.5)
    spec = json.loads(Path(report["spec_path"]).read_text(encoding="utf-8"))
    provenance = json.loads(Path(report["provenance_path"]).read_text(encoding="utf-8"))

    assert any(cli.OFFLINE_PROBE_PROVIDER in line for line in spec["advisories"])
    assert provenance["duration_probe"]["provider"] == cli.OFFLINE_PROBE_PROVIDER
    assert provenance["duration_probe"]["total_measured_seconds"] == pytest.approx(
        spec["duration"]["measured_seconds"]
    )
    assert provenance["cli"]["offline_probe"] is True
    assert provenance["cli"]["copywriter"] == "stub"
    assert provenance["gates"]["disposition"] == "ready"
    assert provenance["content_sha256"]


def test_spec_is_frame_consumable_by_project_layer(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path, copywriter_spec="stub")
    layer = report["project_layer"]
    assert layer["issues"] == []
    assert layer["blocks"] == layer["shots"] == 1
    assert layer["sections"] == 1
    assert layer["script_total_seconds"] == pytest.approx(4.0)


def test_explicit_parameters_reach_the_spec(episode_root: Path, tmp_path: Path):
    report = _run(
        episode_root,
        tmp_path,
        copywriter_spec="stub",
        project_id="demo-offline",
        title="折叠屏",
        fps=25,
        aspect="landscape",
        material_root="materials/",
    )
    spec = json.loads(Path(report["spec_path"]).read_text(encoding="utf-8"))
    assert spec["project_id"] == "demo-offline"
    assert spec["title"] == "折叠屏"
    assert spec["fps"] == 25
    assert spec["aspect"] == "landscape"
    assert spec["material_root"] == "materials/"
    assert spec["duration"]["fps"] == 25
    assert spec["duration"]["total_frames"] == 100  # 4.0s @ 25fps
    assert spec["duration"]["total_seconds"] == pytest.approx(4.0)
    assert report["validation"]["engine"].startswith(("jsonschema-", "python-validator"))


def test_min_supported_claims_is_enforced(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path, copywriter_spec="stub", min_supported_claims=5)
    assert report["exit_code"] == cli.EXIT_BLOCKED
    assert any("低于最低" in reason or "少于最低" in reason for reason in report["blockers"])


# --------------------------------------------------------------------------- #
# 收编与边界
# --------------------------------------------------------------------------- #
def test_intake_writes_only_haiske_side_state(episode_root: Path, tmp_path: Path):
    before = _tree(episode_root)
    report = _run(episode_root, tmp_path, copywriter_spec="stub")
    assert report["intake"]["state"] == "admitted"
    assert report["intake"]["decision"] == "ingest"
    assert report["pack_path"].endswith(f"{EPISODE_ID}-r1")
    # 生产端目录逐文件不变：只读消费，绝不写回。
    assert _tree(episode_root) == before
    # Haike 侧账本与快照落在注入的（tmp）路径下
    assert (tmp_path / "ledger.json").is_file()
    snapshot_dir = Path(report["intake"]["snapshot_dir"])
    assert snapshot_dir.is_relative_to(tmp_path / "snapshots")


def test_rerun_is_idempotent(episode_root: Path, tmp_path: Path):
    first = _run(episode_root, tmp_path, copywriter_spec="stub")
    second = _run(episode_root, tmp_path, copywriter_spec="stub")
    assert first["exit_code"] == second["exit_code"] == cli.EXIT_OK
    assert second["intake"]["decision"] == "noop"
    assert json.loads(Path(second["spec_path"]).read_text(encoding="utf-8")) == json.loads(
        Path(first["spec_path"]).read_text(encoding="utf-8")
    )


def test_not_written_when_write_is_false(episode_root: Path, tmp_path: Path):
    report = _run(episode_root, tmp_path, copywriter_spec="stub", write=False)
    assert report["exit_code"] == cli.EXIT_OK
    assert not (tmp_path / "specs").exists()


def test_uncoverable_material_fails_closed(episode_root: Path, tmp_path: Path):
    # 平台素材不得 render_eligible：成稿后仍无可用素材窗口。
    root, _ = build_pack(
        tmp_path / "platform",
        payloads=with_argument_nodes(default_payloads(), default_argument_node()),
    )
    strict = _run(root, tmp_path, copywriter_spec="stub")
    assert strict["exit_code"] == cli.EXIT_INPUT
    assert strict["stage"] == "remake_spec"
    assert any("没有可用素材" in issue for issue in strict["blockers"])

    partial = _run(root, tmp_path, copywriter_spec="stub", allow_partial=True)
    assert partial["exit_code"] == cli.EXIT_INPUT
    assert partial["stage"] == "remake_spec_validated"
    assert partial["blockers"]


def test_default_out_dir_is_backlot_remake_specs():
    assert cli.DEFAULT_SPEC_DIR == REPO / ".backlot" / "remake-specs"


def test_snapshot_input_is_not_mutated(episode_root: Path, tmp_path: Path):
    """CLI 不得就地改写从研究包投影出的 snapshot（只传递深拷贝）。"""
    original = json.loads((episode_root / "current.json").read_text(encoding="utf-8"))
    frozen = copy.deepcopy(original)
    _run(episode_root, tmp_path, copywriter_spec="stub")
    assert json.loads((episode_root / "current.json").read_text(encoding="utf-8")) == frozen


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def test_main_returns_exit_codes_and_prints_chinese(episode_root: Path, tmp_path: Path, capsys):
    code = cli.main(
        [
            "--episode-root", str(episode_root),
            "--out-dir", str(tmp_path / "specs"),
            "--ledger-path", str(tmp_path / "ledger.json"),
            "--snapshot-root", str(tmp_path / "snapshots"),
            "--now", NOW,
        ]
    )
    assert code == cli.EXIT_NOT_SCRIPTED
    err = capsys.readouterr().err
    assert "argument_map_ready" in err
    assert "copywriter" in err

    code = cli.main(
        [
            "--episode-root", str(episode_root),
            "--out-dir", str(tmp_path / "specs"),
            "--ledger-path", str(tmp_path / "ledger.json"),
            "--snapshot-root", str(tmp_path / "snapshots"),
            "--now", NOW,
            "--copywriter", "stub",
            "--json",
        ]
    )
    assert code == cli.EXIT_OK
    captured = capsys.readouterr()
    payload = json.loads(captured.out[captured.out.index("{") :])
    assert payload["exit_code"] == cli.EXIT_OK
    assert payload["stages"]["script_ready"] is True
    assert Path(payload["spec_path"]).is_file()
