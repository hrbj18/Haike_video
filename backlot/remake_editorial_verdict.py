"""编辑判决落盘（**旁路观测物**）：消费端 CLI 的每个出口都留一份可被只读消费的判决。

为什么需要它
------------
``scripts/remake_editorial_cli.py`` 的退出码（0 / 2 / 3 / 4）只说明「结局」，不说明
「为什么」：exit 3 的 ``stage`` + ``blockers[]`` 原文、exit 2 的校验错误原文，此前**只
print 到 stderr**，跑完就没了；exit 4 的「停在 ``argument_map_ready`` 草案」同理。于是
上游（CopySkill 工作台 / 交接文档）即便愿意**只读**消费端落盘物，也拿不到「这个研究包在
消费端究竟被判成了什么、卡在哪一步、原文理由是什么」——它能看到的只有收编账本与收编
快照（那只覆盖「接收」这一环，覆盖不到编辑门与成稿）。

本模块把判决**旁路落盘**成一份 JSON：``<verdict-dir>/<episode_id>.json``。

它**不是**成稿产物，也与成稿产物**物理分开**：默认目录是 spec 输出目录的**兄弟目录**
（``.backlot/remake-specs`` ↔ ``.backlot/remake-editorial-verdicts``）。它不参与任何
校验 / 门禁 / 调度，读它的进程不需要 import 本模块。

判决词 ↔ 退出码（与 ``scripts/remake_editorial_cli.py`` 的 ``EXIT_*`` 一一对应）
------------------------------------------------------------------------------
* ``0`` → ``produced``：已产出并落盘合法 ``remake-spec-v1``，带 ``spec_path``
* ``2`` → ``rejected``：输入 / 技术 / 合同问题，``blockers`` 里是校验错误原文
* ``3`` → ``blocked``： 编辑门阻断，``stage`` + ``blockers[]`` 都是原文
* ``4`` → ``draft``：  停在 ``argument_map_ready`` 草案（未注入 copywriter，按合同不成稿）

硬约束（调用方必须遵守）
------------------------
1. **判决落盘绝不影响退出码语义**：``write_verdict`` 吞掉一切异常并返回 ``None``。
2. 判决目录默认取「spec 输出目录的兄弟目录」⇒ 调用方把 ``out_dir`` 关进临时目录
   （测试 / 干跑）时，判决跟着进临时目录，不会污染仓库 ``.backlot``。
3. 只写判决文件，不改任何既有产物（``remake-specs`` / ``research-pack-snapshots`` /
   ``research_pack_intake.json``）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[1]

VERDICT_SCHEMA = "remake-editorial-verdict-v1"
VERDICT_DIRNAME = "remake-editorial-verdicts"
DEFAULT_VERDICT_DIR = REPO / ".backlot" / VERDICT_DIRNAME

VERDICT_PRODUCED = "produced"
VERDICT_REJECTED = "rejected"
VERDICT_BLOCKED = "blocked"
VERDICT_DRAFT = "draft"
VERDICT_UNKNOWN = "unknown"

#: 退出码 → 判决词。这里**刻意重复**数字字面量：本模块不能反向 import
#: ``scripts/remake_editorial_cli.py``（那会成环，且那脚本假定可独立执行）；
#: 一致性由 ``tests/backlot/test_remake_editorial_verdict.py`` 锁死。
EXIT_CODE_VERDICTS = {
    0: VERDICT_PRODUCED,
    2: VERDICT_REJECTED,
    3: VERDICT_BLOCKED,
    4: VERDICT_DRAFT,
}

_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def verdict_for_exit(exit_code: object) -> str:
    """退出码 → 判决词；未知退出码返回 ``unknown``（不猜）。"""
    try:
        return EXIT_CODE_VERDICTS[int(exit_code)]  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return VERDICT_UNKNOWN


def safe_episode_name(episode_id: object) -> str:
    """判决文件名 slug。

    ``--episode-root`` 可能指向一个**不存在**的路径（那是 exit 2 的一大来源），此时期次名
    直接来自路径最后一段，可能带分隔符或非法字符，必须洗掉再当文件名。
    """
    text = _UNSAFE_NAME.sub("_", str(episode_id or "").strip()).strip(". ")
    return text or "unknown-episode"


def utc_now() -> str:
    """判决时刻（UTC，秒精度）。用真实墙钟，不用被测的 ``--now`` 时效时钟。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def verdict_dir_for(out_dir: object = None) -> Path:
    """判决目录：给了 spec 输出目录就取它的**兄弟目录**，否则取仓库默认。

    取兄弟目录是刻意的：调用方把 ``out_dir`` 关进临时目录，判决自然一起进临时目录；
    生产默认 ``.backlot/remake-specs`` ⇒ 判决落在 ``.backlot/remake-editorial-verdicts``，
    与成稿产物物理分开。``out_dir`` 是单段相对路径（如 ``specs``）时退回仓库默认。
    """
    if out_dir is not None:
        parent = Path(out_dir).parent
        if str(parent) not in ("", "."):
            return parent / VERDICT_DIRNAME
    return DEFAULT_VERDICT_DIR


def build_verdict(
    *,
    exit_code: object,
    stage: object = "",
    reason: object = "",
    blockers: Sequence[object] = (),
    episode_id: object = "",
    episode_root: object = "",
    pack_id: object = "",
    content_sha256: object = "",
    revision: object = None,
    snapshot_dir: object = "",
    copywriter: object = None,
    copywriter_injected: bool = False,
    spec_path: object = None,
    spec_written: bool = False,
    decided_at: object = None,
) -> dict[str, Any]:
    """构造判决 payload（纯函数，不碰磁盘）。

    ``reason`` / ``blockers`` / ``stage`` 一律**原样透传**调用方给的原文，不做改写或截断
    —— 上游要的是「消费端到底说了什么」，不是二次翻译。
    """
    code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else -1
    return {
        "schema": VERDICT_SCHEMA,
        "verdict": verdict_for_exit(code),
        "exit_code": code,
        "stage": str(stage or ""),
        "reason": str(reason or ""),
        "blockers": [str(item) for item in blockers],
        "episode_id": str(episode_id or ""),
        "episode_root": str(episode_root or ""),
        "pack_id": str(pack_id or ""),
        "content_sha256": str(content_sha256 or ""),
        "revision": revision,
        "snapshot_dir": str(snapshot_dir or ""),
        "copywriter": str(copywriter) if copywriter else "none",
        "copywriter_injected": bool(copywriter_injected),
        "spec_path": str(spec_path) if spec_path else None,
        "spec_written": bool(spec_written),
        "decided_at": str(decided_at or utc_now()),
    }


def write_verdict(
    payload: Mapping[str, Any],
    *,
    out_dir: object = None,
    verdict_dir: object = None,
) -> str | None:
    """把判决落盘并返回判决文件路径；**任何失败都返回 ``None``，绝不抛**。

    判决是旁路观测物：磁盘满、无权限、目录被占，都不允许影响 CLI 的退出码，
    也不允许让主流程崩。
    """
    try:
        directory = Path(verdict_dir) if verdict_dir is not None else verdict_dir_for(out_dir)
        path = directory / f"{safe_episode_name(payload.get('episode_id'))}.json"
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - 判决落盘失败不得改变退出码，刻意全吞
        return None
