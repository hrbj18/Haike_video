"""离线 CLI：研究包 → 编辑决策 → remake-spec-v1 的**显式注入层**。

为什么需要它
------------
``research_pack_intake`` / ``research_pack_snapshot`` / ``remake_intake`` /
``remake_editorial`` / ``remake_project`` 此前只有单元测试在调用，**没有任何生产入口**，
于是「论证编排 → 原创口播成稿」这一层只存在于测试里：没有一条可离线复现的路径能证明
它跑得通。本脚本把这条链串成一个可重复运行的命令：

1. 只读校验 + 收编：``copy_skill_research_pack.read_current_pointer``（拿 ``pack_path``）→
   ``research_pack_intake.intake_episode``（进 Haike 侧账本与快照，**绝不写生产端目录**）。
2. 归一化：``copy_skill_research_pack.load_research_pack`` →
   ``research_pack_snapshot.normalize_editorial_snapshot`` →
   ``remake_intake.validate_editorial_snapshot``（编辑层自有语义校验）。
3. 编辑决策：``remake_editorial.build_editorial_decision(snapshot, copywriter=..., min_supported_claims=...)``。
4. 成稿后量时长再出规格：``remake_editorial.measure_sections`` → ``build_remake_spec`` →
   ``validate_remake_spec``（并用 ``schemas/remake-spec-v1.json`` 复核）；再用
   ``remake_project.asset_key_map`` / ``build_script`` / ``build_visual_blocks`` 验证 spec 能被
   项目层按帧消费（**只验证**，不建项目、不写工作台状态）。
5. 落盘：``<out-dir>/<episode_id>.json``（remake-spec-v1）与
   ``<out-dir>/<episode_id>.provenance.json``（编辑层 provenance sidecar）。
   ``--out-dir`` 默认 ``<repo>/.backlot/remake-specs``。

copywriter 的显式注入（本脚本的核心合同）
----------------------------------------
``build_editorial_decision`` 的 ``copywriter`` 决定链路能走多远：

* **默认不注入**（不传 ``--copywriter``）：只到 ``argument_map_ready`` 草案，
  ``script_ready=false``，**不生成也不落盘任何 remake-spec**，退出码 ``4``。
  这是刻意的 fail closed —— ``claim.text`` 只是论证骨架、不是口播成稿，草案不允许被测
  TTS 或排队渲染。
* **显式注入** ``--copywriter stub``：用脚本内置的确定性离线 stub（一条论证 → 一段原创
  口播：非 verbatim、不引入未引用的事实数字、覆盖全部论证）。
* **显式注入** ``--copywriter 包.模块:可调用对象``：注入你自己的纯函数（签名见
  ``remake_editorial.Copywriter``：``copywriter(context) -> list[section]``）。本脚本只做
  ``importlib`` 加载与调用，**不内置任何网络/LLM/TTS 路径**。

时长探针同样是**注入式**的：``--probe-seconds`` 给的是每段恒定的离线估算
（``provider=offline-estimate``），只用于打通链路与校验帧口径，**不是真实配音时钟**；
落盘规格的 ``advisories`` 会明示这一点，真实生产必须换成真实 TTS 探针重测。

退出码
------
* ``0`` 成功产出并落盘合法 remake-spec-v1
* ``2`` 输入/技术/合同问题（找不到研究包、技术无效、snapshot 或 spec 不合规、素材容量不足）
* ``3`` 编辑门阻断（``EditorialBlocked``：disposition / product / freshness / 事实门 / 成稿合规）
* ``4`` 已到 ``argument_map_ready`` 草案但未成稿（未注入 copywriter）

判决落盘（旁路观测物，``backlot/remake_editorial_verdict.py``）
--------------------------------------------------------------
退出码只说结局，说不清「卡在哪、原文理由是什么」。因此本脚本在**每个出口**都把一份判决
落盘成 ``<verdict-dir>/<episode_id>.json``，供上游**只读**消费（上游不需要 import 本模块）：

* ``0`` → ``produced``（带 ``spec_path``）／``4`` → ``draft``（``stage=argument_map_ready``）
* ``3`` → ``blocked``（``stage`` + ``blockers[]`` 原文）／``2`` → ``rejected``（校验错误原文）
* 另带 ``episode_id`` / ``pack_id`` / ``content_sha256`` / ``revision`` / ``decided_at`` /
  ``copywriter_injected``（是否注入 copywriter）。

``<verdict-dir>`` 默认是 spec 输出目录的**兄弟目录**（``.backlot/remake-specs`` ↔
``.backlot/remake-editorial-verdicts``），与成稿产物物理分开；``--out-dir`` 指向别处时
判决跟着走。判决落盘**失败一律静默**：绝不让 CLI 崩、绝不改变退出码。``--no-write``
只抑制成稿产物，判决照落（它本来就是旁路观测物，不是成稿）。

边界（硬约束）
--------------
不触发真实生产任务、不进统一生产队列、不启动工作台/浏览器、不发网络、不调 LLM/TTS/付费服务、
不改 ``backlot/`` 下任何既有模块的行为、不写生产端（CopySkill）目录。

用法
----
    python scripts/remake_editorial_cli.py --episode-root <episode 目录或 current.json>
    python scripts/remake_editorial_cli.py --episode-root <...> --copywriter stub
    python scripts/remake_editorial_cli.py --episode-root <...> --copywriter my_pkg.writers:offline --json
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backlot.copy_skill_research_pack import (  # noqa: E402
    CURRENT_NAME,
    ResearchPackError,
    coerce_now,
    load_research_pack,
    read_current_pointer,
)
from backlot.remake_editorial import (  # noqa: E402
    DEFAULT_MIN_SUPPORTED_CLAIMS,
    DurationProbeError,
    EditorialBlocked,
    RemakeSpecError,
    build_editorial_decision,
    build_remake_spec,
    measure_sections,
    quantize_frames,
    validate_remake_spec,
)
from backlot.remake_editorial_verdict import build_verdict, write_verdict  # noqa: E402
from backlot.remake_intake import (  # noqa: E402
    EditorialSnapshotError,
    validate_editorial_snapshot,
)
from backlot.remake_project import asset_key_map, build_script, build_visual_blocks  # noqa: E402
from backlot.research_pack_intake import intake_episode  # noqa: E402
from backlot.research_pack_snapshot import normalize_editorial_snapshot  # noqa: E402

DEFAULT_SPEC_DIR = REPO / ".backlot" / "remake-specs"
REMAKE_SPEC_SCHEMA_FILE = REPO / "schemas" / "remake-spec-v1.json"

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_BLOCKED = 3
EXIT_NOT_SCRIPTED = 4

OFFLINE_PROBE_PROVIDER = "offline-estimate"
OFFLINE_PROBE_NOTE = (
    "时长来自离线估算探针（provider=offline-estimate，每段恒定秒数），不是真实 TTS 测量；"
    "排队渲染前必须换成真实探针重测，否则成片配音时钟不成立"
)

Copywriter = Callable[[dict[str, Any]], list[dict[str, Any]]]


class CliError(Exception):
    """CLI 自身的输入/合同问题（一律映射到 ``EXIT_INPUT`` + 中文说明）。"""


# --------------------------------------------------------------------------- #
# 注入式 copywriter / 时长探针（离线确定性实现）
# --------------------------------------------------------------------------- #
def stub_copywriter(context: dict[str, Any]) -> list[dict[str, Any]]:
    """确定性离线 stub 成稿器：一条论证 → 一段原创口播。

    满足 ``remake_editorial._run_copywriter`` 的全部硬约束：非空文本、引用已存在的 claim、
    说明如何支撑主题、非 verbatim 复述、不引入未引用的事实数字、覆盖全部论证。
    **不是**写稿模型，只用于打通并验证「成稿 → 量时长 → 规格」这条链路。
    """
    rows: list[dict[str, Any]] = []
    for argument in context["argument_map"]:
        rows.append(
            {
                "label": argument["dim"],
                "text": f"{argument['claim']}——值得展开说。",
                "claim_refs": [argument["claim_id"]],
                "theme_support": f"以「{argument['claim']}」支撑主题「{context['theme']['statement']}」",
            }
        )
    return rows


def make_offline_probe(seconds_per_section: float) -> Callable[..., dict[str, Any]]:
    """每段恒定秒数的离线时长探针（签名与 ``measure_sections`` 约定一致）。"""

    def probe(text: str, *, speaker: str | None = None, voice: str | None = None) -> dict[str, Any]:
        return {
            "seconds": float(seconds_per_section),
            "provider": OFFLINE_PROBE_PROVIDER,
            "model": "fixed-per-section-v1",
            "version": "offline",
        }

    return probe


def resolve_copywriter(spec_value: str | None) -> Copywriter | None:
    """``--copywriter`` 取值 → 可调用对象；空 / ``none`` 表示**不注入**。"""
    token = (spec_value or "").strip()
    if not token or token.lower() in {"none", "off"}:
        return None
    if token == "stub":
        return stub_copywriter
    module_name, separator, attribute = token.partition(":")
    if not separator or not module_name or not attribute:
        raise CliError("--copywriter 需为 'stub' 或 '包.模块:可调用对象'，例如 my_pkg.writers:offline")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 加载失败即中文报错
        raise CliError(f"--copywriter 无法导入模块 {module_name}：{exc}") from exc
    function = getattr(module, attribute, None)
    if not callable(function):
        raise CliError(f"--copywriter 模块 {module_name} 上找不到可调用对象 {attribute}")
    return function


# --------------------------------------------------------------------------- #
# 项目层消费验证（纯函数，不建项目、不写工作台状态）
# --------------------------------------------------------------------------- #
def verify_project_layer(spec: dict[str, Any]) -> dict[str, Any]:
    """用 ``remake_project`` 的纯函数消费 spec，验证帧口径能被项目层无缝接住。"""
    mapping = asset_key_map(spec, {source["key"]: f"offline:{source['key']}" for source in spec["sources"]})
    script = build_script(spec)
    fps = int(spec.get("fps") or 30)
    issues: list[str] = []
    blocks_total = 0
    if abs(float(script["total_duration_seconds"]) - float(spec["duration"]["total_seconds"])) > 0.002:
        issues.append("script 总时长与 spec.duration.total_seconds 不一致")
    if [section["id"] for section in script["sections"]] != [section["id"] for section in spec["sections"]]:
        issues.append("script 段顺序与 spec 不一致")
    for section in spec["sections"]:
        blocks = build_visual_blocks(spec, section, mapping)
        blocks_total += len(blocks)
        if not blocks:
            issues.append(f"section {section['id']} 没有视觉块")
            continue
        covered = abs(float(blocks[-1]["end_seconds"]) - float(section["duration_seconds"])) <= 0.001
        if abs(float(blocks[0]["start_seconds"])) > 0.001 or not covered:
            issues.append(f"section {section['id']} 视觉块未连续覆盖整段")
            continue
        previous_end = None
        for block in blocks:
            if previous_end is not None and abs(float(block["start_seconds"]) - previous_end) > 0.001:
                issues.append(f"section {section['id']} 视觉块未首尾相接")
                break
            previous_end = float(block["end_seconds"])
            display = quantize_frames(block["end_seconds"], fps) - quantize_frames(block["start_seconds"], fps)
            source = quantize_frames(block["source_out_seconds"], fps) - quantize_frames(
                block["source_in_seconds"], fps
            )
            if display != source:
                issues.append(f"section {section['id']} 视觉块显示帧数与源帧数不一致")
                break
    return {
        "sections": len(spec["sections"]),
        "shots": sum(len(section.get("shots") or []) for section in spec["sections"]),
        "blocks": blocks_total,
        "script_total_seconds": script["total_duration_seconds"],
        "issues": issues,
    }


def validate_against_schema_file(spec: dict[str, Any]) -> tuple[str, list[str]]:
    """按 ``schemas/remake-spec-v1.json`` 复核；环境缺 jsonschema 时退回 python 校验器。"""
    try:
        import jsonschema  # noqa: PLC0415 - 可选依赖
    except ImportError:
        return "python-validator（环境缺 jsonschema）", []
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    schema = json.loads(REMAKE_SPEC_SCHEMA_FILE.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(spec), key=lambda error: [str(item) for item in error.path])
    try:
        engine_version = version("jsonschema")
    except PackageNotFoundError:  # pragma: no cover - 源码直跑才会命中
        engine_version = "unknown"
    return f"jsonschema-{engine_version}", [error.message for error in errors]


def resolve_episode_source(raw: str | Path) -> tuple[Path, Path]:
    """``--episode-root`` 支持 episode 目录或其中的 ``current.json``，返回 (根目录, current 路径)。"""
    source = Path(raw)
    if source.name == CURRENT_NAME:
        root, current = source.parent, source
    else:
        root, current = source, source / CURRENT_NAME
    if not root.is_dir():
        raise CliError(f"研究包 episode 根目录不存在：{root}")
    if not current.is_file():
        raise CliError(f"研究包缺少 {CURRENT_NAME}（无法解析 pack_path）：{current}")
    return root, current


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(
    episode_root: str | Path,
    *,
    out_dir: str | Path | None = None,
    verdict_dir: str | Path | None = None,
    ledger_path: str | Path | None = None,
    snapshot_root: str | Path | None = None,
    project_id: str | None = None,
    title: str = "",
    brief: str = "",
    style_playbook: str = "clean-professional",
    material_root: str = "",
    fps: int = 30,
    aspect: str = "portrait",
    min_supported_claims: int = DEFAULT_MIN_SUPPORTED_CLAIMS,
    copywriter_spec: str | None = None,
    probe_seconds: float = 4.0,
    allow_partial: bool = False,
    now: object = None,
    write: bool = True,
) -> dict[str, Any]:
    """跑完整条链并返回结构化报告（不抛异常；退出码写在 ``exit_code``）。

    ``write=False`` 时只做校验与验证，不落盘**规格**（测试与干跑用）；**判决**照落，
    它是与成稿产物物理分开的旁路观测物（见模块 docstring）。

    ``verdict_dir`` 缺省时按 ``out_dir`` 取兄弟目录（``.backlot/remake-specs`` →
    ``.backlot/remake-editorial-verdicts``）；传了就写那里，落盘失败静默。
    """
    report: dict[str, Any] = {
        "ok": False,
        "exit_code": EXIT_INPUT,
        "stage": "start",
        "episode_id": "",
        "episode_root": str(episode_root),
        "pack_id": "",
        "content_sha256": None,
        "message": "",
        "blockers": [],
        "lines": [],
    }
    # 位置检查都还没做，先按路径最后一段给个期次名兜底（否则 exit 2 的判决会无名可落）。
    hinted = Path(episode_root)
    report["episode_id"] = (hinted.parent if hinted.name == CURRENT_NAME else hinted).name
    copywriter: Copywriter | None = None

    def record_verdict(code: int, stage: str, message: str) -> None:
        """旁路落盘一份判决；任何失败都只留 ``verdict_path=None``，不改变退出码。"""
        try:
            payload = build_verdict(
                exit_code=code,
                stage=stage,
                reason=message,
                blockers=report.get("blockers") or [],
                episode_id=report.get("episode_id") or hinted.name,
                episode_root=report.get("episode_root") or str(episode_root),
                pack_id=report.get("pack_id") or "",
                content_sha256=report.get("content_sha256") or "",
                revision=report.get("revision"),
                snapshot_dir=(report.get("intake") or {}).get("snapshot_dir") or "",
                copywriter=copywriter_spec,
                copywriter_injected=copywriter is not None,
                spec_path=report.get("spec_path"),
                spec_written=bool(write and report.get("spec_path")),
            )
            report["verdict_path"] = write_verdict(payload, out_dir=out_dir, verdict_dir=verdict_dir)
        except Exception:  # noqa: BLE001 - 判决是旁路观测物：落盘异常不得让 CLI 崩、不得改退出码
            report["verdict_path"] = None

    def fail(code: int, stage: str, message: str, blockers: list[str] | None = None) -> dict[str, Any]:
        report.update(ok=False, exit_code=code, stage=stage, message=message)
        report["blockers"] = [str(item) for item in (blockers or [])]
        report["lines"].append(f"[退出码 {code}] {message}")
        report["lines"].extend(f"  - {item}" for item in report["blockers"])
        record_verdict(code, stage, message)
        return report

    # --- 1. 位置检查 + 只读校验 + 收编 -------------------------------------- #
    try:
        root, _ = resolve_episode_source(episode_root)
        copywriter = resolve_copywriter(copywriter_spec)
    except CliError as exc:
        return fail(EXIT_INPUT, "input", str(exc))

    report["episode_root"] = str(root)
    try:
        pointer = read_current_pointer(root)
        report["pack_path"] = str((pointer or {}).get("pack_path") or "")
        report["pack_id"] = str((pointer or {}).get("pack_id") or "")
        report["content_sha256"] = (pointer or {}).get("content_sha256")
        intake = intake_episode(root, ledger_path=ledger_path, snapshot_root=snapshot_root, now=coerce_now(now))
    except ResearchPackError as exc:
        return fail(EXIT_INPUT, "intake", f"研究包无法收编：{exc}")
    except OSError as exc:
        return fail(EXIT_INPUT, "intake", f"研究包收编时发生 IO 错误：{exc}")

    report["episode_id"] = str(intake.get("episode_id") or root.name)
    report["revision"] = intake.get("revision")
    report["content_sha256"] = intake.get("content_sha256") or report.get("content_sha256")
    report["intake"] = {
        "state": intake.get("state"),
        "disposition": intake.get("disposition"),
        "decision": intake.get("decision"),
        "reason": intake.get("reason"),
        "admitted": bool(intake.get("admitted")),
        "snapshot_dir": intake.get("snapshot_dir"),
    }
    report["lines"].append(
        f"研究包 {report['episode_id']}（r{report.get('revision')}）收编：state={intake.get('state')}、"
        f"disposition={intake.get('disposition')}、decision={intake.get('decision')}、reason={intake.get('reason')}"
    )
    report["lines"].append(f"current.json → pack_path = {report.get('pack_path')}")

    if intake.get("state") == "failed":
        detail = intake.get("error") or intake.get("reason") or ""
        reason = "研究包技术/运输损坏，拒绝收编（partial_package）" if intake.get("partial_package") else "研究包合同技术无效"
        return fail(EXIT_INPUT, "intake", f"{reason}：{detail}")

    # --- 2. 归一化成 editorial snapshot（只读再校验，不采信收编结论） -------- #
    try:
        loaded = load_research_pack(root, now=now)
        snapshot = validate_editorial_snapshot(normalize_editorial_snapshot(loaded, now=now))
    except EditorialSnapshotError as exc:
        return fail(EXIT_INPUT, "validated_snapshot", "editorial snapshot 未通过编辑层合同校验", exc.issues)
    except ResearchPackError as exc:
        return fail(EXIT_INPUT, "validated_snapshot", f"研究包只读校验失败：{exc}")
    except OSError as exc:
        return fail(EXIT_INPUT, "validated_snapshot", f"读取研究包时发生 IO 错误：{exc}")

    report["stage"] = "validated_snapshot"
    report["snapshot"] = {
        "schema": snapshot.get("schema"),
        "disposition": snapshot.get("disposition"),
        "freshness_gate": (snapshot.get("freshness_gates") or {}).get("gate"),
        "product_gate": list(snapshot.get("product_gate") or []),
        "claims": len(snapshot.get("claims") or []),
        "materials": len(snapshot.get("materials") or []),
    }
    report["lines"].append(
        f"editorial snapshot 合规：disposition={snapshot.get('disposition')}、"
        f"freshness_gate={report['snapshot']['freshness_gate']}、"
        f"claims={report['snapshot']['claims']}、materials={report['snapshot']['materials']}"
    )

    # --- 3. 编辑决策（copywriter 是否注入决定链路能走多远） ----------------- #
    try:
        decision = build_editorial_decision(
            snapshot, copywriter=copywriter, min_supported_claims=min_supported_claims
        )
    except EditorialBlocked as exc:
        return fail(
            EXIT_BLOCKED,
            exc.stage or "editorial",
            f"编辑决策被阻断（阶段 {exc.stage or 'unknown'}）",
            exc.reasons,
        )

    stages = dict(decision.get("stages") or {})
    report["stages"] = stages
    report["advisories"] = list(decision.get("advisories") or [])
    report["gaps"] = [dict(gap) for gap in decision.get("gaps") or []]
    report["lines"].append(
        f"编辑决策：论证 {len(decision.get('argument_map') or [])} 条、段落 {len(decision.get('sections') or [])} 段，"
        f"argument_map_ready={stages.get('argument_map_ready')}、script_ready={stages.get('script_ready')}"
    )

    if not stages.get("script_ready"):
        return fail(
            EXIT_NOT_SCRIPTED,
            "argument_map_ready",
            "已到 argument_map_ready 草案：未注入 copywriter，按合同不产出可测 TTS 的成稿，"
            "因此不生成也不落盘 remake-spec-v1；如需成稿请显式传 --copywriter stub 或 包.模块:可调用对象",
            [f"论证缺口 {len(report['gaps'])} 处（多为素材授权/可用性问题，需先补可渲染素材）"]
            if report["gaps"]
            else [],
        )

    # --- 4. 成稿后量时长 → remake-spec-v1 --------------------------------- #
    try:
        measurements = measure_sections(
            decision["sections"], duration_probe=make_offline_probe(probe_seconds), measured_at=str(now or "")
        )
    except DurationProbeError as exc:
        return fail(EXIT_INPUT, "duration_measured", f"时长探针失败：{exc}")

    decision["stages"]["duration_measured"] = True
    decision["provenance"]["duration_probe"] = dict(measurements["probe"])
    decision["provenance"]["duration_probe"]["total_measured_seconds"] = measurements["total_measured_seconds"]
    report["lines"].append(
        f"离线时长探针：{len(measurements['sections'])} 段 × {probe_seconds}s = "
        f"{measurements['total_measured_seconds']}s（provider={OFFLINE_PROBE_PROVIDER}，不是真实 TTS）"
    )

    try:
        spec = build_remake_spec(
            decision,
            measurements,
            project_id=project_id or str(snapshot.get("episode_id") or root.name),
            fps=fps,
            aspect=aspect,
            title=title,
            brief=brief,
            style_playbook=style_playbook,
            material_root=material_root,
            allow_partial=allow_partial,
        )
    except RemakeSpecError as exc:
        return fail(EXIT_INPUT, "remake_spec", "remake-spec-v1 无法满足硬约束", exc.issues)

    spec["advisories"] = list(spec.get("advisories") or []) + [OFFLINE_PROBE_NOTE]

    validation = validate_remake_spec(spec, fps=fps)
    engine, schema_errors = validate_against_schema_file(spec)
    if not validation["valid"] or schema_errors:
        return fail(
            EXIT_INPUT,
            "remake_spec_validated",
            f"remake-spec-v1 校验失败（{engine}）",
            list(validation["issues"]) + list(schema_errors),
        )
    decision["stages"]["remake_spec_ready"] = True
    report["stages"] = dict(decision["stages"])
    report["validation"] = {"valid": True, "engine": engine, "issues": []}
    report["lines"].append(
        f"remake-spec-v1 校验通过（{engine}）：{len(spec['sections'])} 段 / "
        f"{sum(len(section['shots']) for section in spec['sections'])} 镜 / "
        f"{spec['duration']['total_frames']} 帧 @ {spec['fps']}fps"
    )

    # --- 5. 项目层按帧消费验证 --------------------------------------------- #
    try:
        project_layer = verify_project_layer(spec)
    except KeyError as exc:
        return fail(EXIT_INPUT, "project_layer", f"项目层无法映射 spec 素材：{exc}")
    if project_layer["issues"]:
        return fail(EXIT_INPUT, "project_layer", "项目层帧口径校验失败", project_layer["issues"])
    report["project_layer"] = project_layer
    report["lines"].append(
        f"项目层消费验证通过：build_script 总时长 {project_layer['script_total_seconds']}s，"
        f"build_visual_blocks 生成 {project_layer['blocks']} 块，显示帧数与源帧数逐块一致"
    )

    # --- 6. 落盘 ----------------------------------------------------------- #
    spec_dir = Path(out_dir) if out_dir else DEFAULT_SPEC_DIR
    spec_path = spec_dir / f"{report['episode_id']}.json"
    provenance_path = spec_dir / f"{report['episode_id']}.provenance.json"
    if write:
        provenance = dict(decision["provenance"])
        provenance["cli"] = {
            "episode_root": str(root),
            "pack_path": report.get("pack_path", ""),
            "intake_state": intake.get("state"),
            "intake_decision": intake.get("decision"),
            "snapshot_dir": intake.get("snapshot_dir"),
            "copywriter": copywriter_spec or "none（本报告不应出现）",
            "offline_probe": True,
            "note": OFFLINE_PROBE_NOTE,
            "spec_path": str(spec_path),
            "spec_validation_engine": engine,
            "project_layer": project_layer,
            "stages": dict(decision["stages"]),
        }
        try:
            spec_dir.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            provenance_path.write_text(
                json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            return fail(EXIT_INPUT, "write", f"规格落盘失败：{exc}")

    report.update(
        ok=True,
        exit_code=EXIT_OK,
        stage="remake_spec_ready",
        spec_path=str(spec_path) if write else None,
        provenance_path=str(provenance_path) if write else None,
        message=(
            f"已产出合法 remake-spec-v1 并落盘：{spec_path}（provenance：{provenance_path}）"
            if write
            else "已产出合法 remake-spec-v1（--no-write：只校验不落盘）"
        ),
    )
    report["lines"].append(f"[退出码 0] {report['message']}")
    record_verdict(EXIT_OK, "remake_spec_ready", report["message"])
    return report


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remake_editorial_cli.py",
        description="离线串联「研究包 → 编辑决策 → remake-spec-v1」，不触网、不调 TTS/LLM、不进生产队列。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 成功；2 输入/技术/合同问题；3 编辑门阻断；4 已到 argument_map_ready 草案未成稿。\n"
            "默认不注入 copywriter，只到草案且不落盘；成稿需显式 --copywriter。"
        ),
    )
    parser.add_argument("--episode-root", required=True, help="研究包 episode 根目录，或其中的 current.json")
    parser.add_argument("--out-dir", help=f"规格输出目录，默认 {DEFAULT_SPEC_DIR}")
    parser.add_argument("--ledger-path", help="Haike 侧收编账本路径，默认模块默认值 .backlot/research_pack_intake.json")
    parser.add_argument("--snapshot-root", help="Haike 侧收编快照目录，默认 .backlot/research-pack-snapshots")
    parser.add_argument("--project-id", help="spec.project_id，默认取 snapshot.episode_id")
    parser.add_argument("--title", default="", help="spec.title，默认取主题立场句")
    parser.add_argument("--brief", default="", help="spec.brief")
    parser.add_argument("--style-playbook", default="clean-professional", help="spec.style_playbook")
    parser.add_argument("--material-root", default="", help="spec.material_root")
    parser.add_argument("--fps", type=int, default=30, help="规格帧率，默认 30")
    parser.add_argument("--aspect", choices=("portrait", "landscape", "square"), default="portrait")
    parser.add_argument("--min-supported-claims", type=int, default=DEFAULT_MIN_SUPPORTED_CLAIMS)
    parser.add_argument(
        "--copywriter",
        help="'stub'（内置离线确定性成稿器）或 '包.模块:可调用对象'；缺省=不注入（只到草案，退出码 4）",
    )
    parser.add_argument("--probe-seconds", type=float, default=4.0, help="离线估算探针的每段恒定秒数，默认 4.0")
    parser.add_argument("--allow-partial", action="store_true", help="素材容量不足时写入 gaps 而不是直接失败")
    parser.add_argument("--now", help="ISO 时间串（重算时效门用），默认取系统时间")
    parser.add_argument("--no-write", action="store_true", help="只校验与验证，不落盘")
    parser.add_argument("--json", action="store_true", help="额外输出机器可读报告 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run(
        args.episode_root,
        out_dir=args.out_dir,
        ledger_path=args.ledger_path,
        snapshot_root=args.snapshot_root,
        project_id=args.project_id,
        title=args.title,
        brief=args.brief,
        style_playbook=args.style_playbook,
        material_root=args.material_root,
        fps=args.fps,
        aspect=args.aspect,
        min_supported_claims=args.min_supported_claims,
        copywriter_spec=args.copywriter,
        probe_seconds=args.probe_seconds,
        allow_partial=args.allow_partial,
        now=args.now,
        write=not args.no_write,
    )
    stream = sys.stdout if report["exit_code"] == EXIT_OK else sys.stderr
    for line in report["lines"]:
        print(line, file=stream)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stdout)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
