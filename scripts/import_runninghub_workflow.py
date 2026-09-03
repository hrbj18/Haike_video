"""Freeze and validate a RunningHub ComfyUI API workflow for OpenMontage."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.avatar.runninghub_avatar import repair_longcat_workflow_template, workflow_template_sha256


def main() -> int:
    parser = argparse.ArgumentParser(description="导入并冻结 RunningHub LongCat 工作流")
    parser.add_argument("source", type=Path)
    parser.add_argument("--target", type=Path, default=Path("config/runninghub/longcat_avatar_api.json"))
    args = parser.parse_args()
    source = args.source.resolve()
    target = args.target.resolve()
    workflow = repair_longcat_workflow_template(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    print(json.dumps({
        "ok": True,
        "target": str(target),
        "node_count": len(workflow),
        "sha256": workflow_template_sha256(target),
        "mutable_fields": ["176.image", "524.audio"],
        "safety_repairs": [
            "529.expression: milliseconds_to_seconds",
            "546.expression: overlap_aware_continuation_count",
            "292.trim_to_audio: true",
            "352.trim_to_audio: true",
        ],
        "output_node": "352",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
