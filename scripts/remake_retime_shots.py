"""外站素材复刻 · 局部换镜（源窗口重定时）

为什么需要这个脚本：
  数字人原声母版落地后，每段场景的时长被配音时钟接管，显示窗口同比缩短
  （渲染端只取 ``-ss source_in`` 加上缩短后的时长，等于自动「掐尾」），
  但 ``source_out_seconds`` 仍是规格书里的原始值。因此任何后续改镜都必须
  显式给出 ``source_out = source_in + 显示时长``，否则触发
  「源区间与显示区间必须按帧一致」的校验。

  换镜只动「用哪段素材的哪一段」，不动时长、不动配音、不动数字人，
  所以完全本地、零付费。这也是修掉「漏进烧入字幕 / 屏内人脸 / 语义错配」
  这类问题的标准动作。

用法：
  python scripts/remake_retime_shots.py --project projects/<id> --patch shots-patch.json
  python scripts/remake_retime_shots.py --project projects/<id> --patch shots-patch.json --apply

patch 文件格式（key 是场景 id，数组顺序与现有画面区间一一对应）：
  {
    "T005": [
      {"asset_id": "S-001", "source_in_seconds": 19.6, "label": "双手持机，内屏内容"},
      {"asset_id": "S-001", "source_in_seconds": 22.8, "label": "双手持机，桌面滑动"}
    ]
  }

注意：
  * 默认只做「干跑」并打印计划；写盘必须显式 --apply。
  * 生产队列是唯一执行权威；本脚本只在队列空闲时改「素材选择」这一层状态，
    改完要重新排队 ``full_preview``（本地合成）才会反映到成片。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backlot import workbench as wb  # noqa: E402


def build_blocks(scene: dict, patches: list[dict], assets: dict[str, dict]) -> list[dict]:
    """把「只给源入点」的补丁展开成完整的画面区间列表。"""
    current = ((scene.get("visual_timeline") or {}).get("blocks") or [])
    if len(patches) != len(current):
        raise SystemExit(
            f"{scene['id']} 现有 {len(current)} 个画面区间，补丁给了 {len(patches)} 个；"
            "换镜不改变区间数量与时长，请补齐或拆分。"
        )
    blocks: list[dict] = []
    for old, patch in zip(current, patches):
        start = float(old["start_seconds"])
        end = float(old["end_seconds"])
        display = round(end - start, 3)
        asset_id = str(patch["asset_id"])
        asset = assets.get(asset_id)
        if not asset:
            raise SystemExit(f"{scene['id']} 引用了不存在的素材 {asset_id}")
        source_in = round(float(patch["source_in_seconds"]), 3)
        source_out = round(source_in + display, 3)
        available = float(asset.get("duration_seconds") or 0)
        if available and source_out > available + 0.001:
            raise SystemExit(
                f"{scene['id']} 的 {source_in:.3f}+{display:.3f}={source_out:.3f} "
                f"超过 {asset_id} 的实际时长 {available:.3f}"
            )
        blocks.append({
            "id": old.get("id"),
            "start_seconds": start,
            "end_seconds": end,
            "asset_id": asset_id,
            "source_mode": str(old.get("source_mode") or "web_download"),
            "source_in_seconds": source_in,
            "source_out_seconds": source_out,
            "label": str(patch.get("label") or asset.get("name") or asset_id)[:160],
        })
    return blocks


def main() -> int:
    parser = argparse.ArgumentParser(description="复刻项目局部换镜（源窗口重定时）")
    parser.add_argument("--project", required=True)
    parser.add_argument("--patch", required=True, help="换镜补丁 JSON")
    parser.add_argument("--apply", action="store_true", help="真正写盘（默认只干跑）")
    args = parser.parse_args()

    project = Path(args.project)
    if not project.is_absolute():
        project = REPO / project
    patch = json.loads(Path(args.patch).read_text(encoding="utf-8"))
    state = wb.read_workbench(project) if hasattr(wb, "read_workbench") else None
    if state is None:
        state = json.loads((project / "artifacts" / "workbench.json").read_text(encoding="utf-8"))
    assets = {str(a["id"]): a for a in state.get("assets", [])}
    scenes = {str(s["id"]): s for s in state.get("scenes", [])}

    for scene_id, patches in patch.items():
        if str(scene_id).startswith("_"):
            continue                                   # 允许 _comment 之类的说明键
        scene = scenes.get(str(scene_id))
        if not scene:
            raise SystemExit(f"找不到场景 {scene_id}")
        blocks = build_blocks(scene, patches, assets)
        print(f"\n== {scene_id}  {scene['start_seconds']:.3f}-{scene['end_seconds']:.3f}")
        for old, new in zip(scene["visual_timeline"]["blocks"], blocks):
            same = (old.get("asset_id") == new["asset_id"]
                    and abs(float(old.get("source_in_seconds") or 0) - new["source_in_seconds"]) < 0.0005)
            print(f"   {new['id']} 显示 {new['start_seconds']:6.3f}-{new['end_seconds']:6.3f} "
                  f"({new['end_seconds'] - new['start_seconds']:.3f}s)  "
                  f"{old.get('asset_id')}@{float(old.get('source_in_seconds') or 0):.3f} -> "
                  f"{new['asset_id']}@{new['source_in_seconds']:.3f}"
                  f" (out {new['source_out_seconds']:.3f}){'  未变' if same else ''}")
        if args.apply:
            wb.update_scene_visual_timeline(project, scene_id, {"blocks": blocks})
            print(f"   APPLIED {scene_id}")

    print("\n干跑完成；加 --apply 才会写盘。" if not args.apply else "\n已写盘。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
