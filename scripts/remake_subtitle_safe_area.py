#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""竖屏字幕安全区：字号/落点试算 → 应用 → 生效复核。

为什么需要这个脚本
------------------
字幕最终由 ASS 渲染（`VideoCompose._write_ass_subtitles`），定位靠
`{\\an2\\pos(540, y*1920)}`。两个坑必须实测，不能靠算：

1. **libass + Microsoft YaHei 的 CJK 字宽约 0.727 em**，不是 1.0 em。
   所以 18 字长句在 Fontsize=42 时只占画布宽的 0.508，放大空间比直觉大得多；
   反过来若按 1.0 em 估算会误判为「不能再大」。
   → 一律以 `probe` 的实测像素为准。
2. **`WrapStyle: 2` = 不自动换行**。超宽不会折行，而是直接从画布边缘裁掉。
   所以「最长一句 × 字宽 ≤ 画布宽」是硬约束，必须验。

9:16 竖屏发布的落点约束
----------------------
抖音等平台的底部作者/描述/话题块会覆盖画面底部。保守取 **0.80** 为安全线
（字幕底边必须 < 0.80），本项目实测取值 **font_size 64 / position.y 0.75**
→ 18 字长句行宽 0.776 画布宽，文字底边 0.7469，距底 486px（25.3%），
距 0.80 安全线 97px。旧默认（42 / 0.89）底边 0.888，**已落在安全线以下 174px**，
上传后必被遮挡。

子命令
------
    probe    在合成画布上试算候选 (font_size, y, width)，打印实测像素框（不写盘）
    apply    把选定样式 POST 给在跑的工作台，并（可选）排队重出全片预览
    measure  量成片里字幕的真实像素框与安全余量

用法
----
    python scripts/remake_subtitle_safe_area.py probe --project projects/<id>
    python scripts/remake_subtitle_safe_area.py apply --project projects/<id> \
        --font-size 64 --y 0.75 --queue-preview
    python scripts/remake_subtitle_safe_area.py measure --project projects/<id>

★ `apply --queue-preview` 走 `full_preview`（纯本地 ffmpeg，零付费），但它会被
  「声音设置已修改：请先生成并确认第一段音量样板」挡下（HTTP 422）。这是设计内的
  门：只要 `music_policy.enabled` 为真且样板未 approved 就会拦。先跑：
    POST /api/project/<id>/workbench/music-sample/jobs
    POST /api/project/<id>/workbench/music-sample/approve
  样板是本地合成，零付费；它记录的 `policy_signature` 就是当前混音指纹，
  字幕改动不会改变它，所以生成+确认一次即可长期复用。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import lib.ffmpeg_locator as L  # noqa: E402

# 竖屏短剧/口播的默认安全区取值（本项目实测得出）
SAFE_BOTTOM_RATIO = 0.80
DEFAULT_FONT_SIZE = 64
DEFAULT_Y = 0.75
DEFAULT_WIDTH = 0.90


def _load_video_compose():
    """按路径加载生产渲染器，避免 import 整个 tools 包。"""
    spec = importlib.util.spec_from_file_location(
        "video_compose", ROOT / "tools" / "video" / "video_compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _imread_gray(path: Path):
    import cv2

    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)


def _imread_color(path: Path):
    import cv2

    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def _style_dict(font_size: int, y: float, width: float, *, height_ref: int = 1080) -> dict:
    """构造与 `_subtitle_video_style()` 输出同形的样式（响应式分支）。"""
    return {
        "font": "Microsoft YaHei",
        "font_size_ratio": round(font_size / height_ref, 5),
        "font_size": font_size,
        "bold": True,
        "primary_color": "&H00FFFFFF",
        "secondary_color": "&H00FFFFFF",
        "outline_color": "&H141F1107",
        "back_color": "&H521F1107",
        "border_style": 1,
        "shadow": 0,
        "alignment": 2,
        "position_x_ratio": 0.5,
        "position_y_ratio": y,
        "caption_width_ratio": width,
        "responsive": True,
    }


def _cue_stats(srt: Path) -> tuple[int, list[int]]:
    """返回（最长句字符数, 每句字符数）。用于判断放大后会不会顶到画布边缘。"""
    import re

    blocks = re.split(r"\r?\n\r?\n+", srt.read_text(encoding="utf-8-sig").strip())
    lengths = []
    for block in blocks:
        lines = block.splitlines()
        index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if index < 0:
            continue
        text = "".join(lines[index + 1:]).strip()
        if text:
            lengths.append(len(text))
    return (max(lengths) if lengths else 0), lengths


def _subtitle_srt(project: Path) -> Path:
    """优先用审核预览字幕；没有就退回数字人分句字幕。"""
    for name in ("avatar-review-subtitles.srt", "avatar-dialogue-subtitles.srt"):
        candidate = project / "renders" / "avatar" / name
        if candidate.is_file():
            return candidate
    raise SystemExit(f"找不到字幕文件：{project}/renders/avatar/*.srt")


def _srt_time_of_longest(srt: Path) -> float:
    import re

    blocks = re.split(r"\r?\n\r?\n+", srt.read_text(encoding="utf-8-sig").strip())
    best = None
    for block in blocks:
        lines = block.splitlines()
        index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if index < 0:
            continue
        text = "".join(lines[index + 1:]).strip()
        match = re.search(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})",
                          lines[index])
        if not match or not text:
            continue
        values = [int(v) for v in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        if best is None or len(text) > best[0]:
            best = (len(text), (start + end) / 2)
    if best is None:
        raise SystemExit("字幕文件里没有可用条目")
    return best[1]


def _frame_at(video: Path, at: float, out: Path) -> Path:
    ff = L.resolve_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(ff), "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{at}", "-i", str(video), "-frames:v", "1", str(out)], check=True)
    return out


def _box_in_band(path: Path, band_top: int, band_bottom: int, threshold: int = 235):
    gray = _imread_gray(path)
    height, width = gray.shape
    strip = gray[band_top:band_bottom, :]
    ys, xs = np.nonzero(strip >= threshold)
    if not len(ys):
        return None
    return {
        "width": int(xs.max() - xs.min() + 1),
        "height": int(ys.max() - ys.min() + 1),
        "top": int(ys.min()) + band_top,
        "bottom": int(ys.max()) + band_top,
        "left": int(xs.min()),
        "right": int(xs.max()),
        "canvas_width": width,
        "canvas_height": height,
    }


# --------------------------------------------------------------------------- probe


def cmd_probe(args) -> int:
    project = Path(args.project).resolve()
    srt = _subtitle_srt(project)
    longest, lengths = _cue_stats(srt)
    print(f"字幕文件：{srt}")
    print(f"共 {len(lengths)} 条，最长 {longest} 字（分布 {sorted(set(lengths))}）\n")

    compose = _load_video_compose()
    width, height = 1080, 1920
    at = args.at if args.at is not None else _srt_time_of_longest(srt)
    print(f"试算时间点 t={at:.2f}s（默认取最长句中点）\n")

    tmp = ROOT / ".backlot" / "_tmp_subtitle_safe_area"
    tmp.mkdir(parents=True, exist_ok=True)
    ff = L.resolve_ffmpeg()

    candidates = [(int(fs), float(y), DEFAULT_WIDTH, 42)  # 分组标签
                  for fs in args.font_sizes for y in args.ys]
    print(f"{'font':>5} {'y':>6} {'宽px':>6} {'占画布':>7} {'高px':>5} {'底边':>7} {'距底px':>7} {'距0.80':>7}  判定")
    print("-" * 92)
    ok_rows = []
    for font_size, y, caption_width, _tag in candidates:
        ass = tmp / f"p_{font_size}_{int(y * 100)}.ass"
        compose.VideoCompose._write_ass_subtitles(srt, ass, _style_dict(font_size, y, caption_width),
                                                  width, height)
        png = ass.with_suffix(".png")
        escaped = str(ass.resolve()).replace("\\", "/").replace(":", "\\:")
        subprocess.run([str(ff), "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=90:r=1",
                        "-ss", f"{at}", "-vf", f"ass='{escaped}'", "-frames:v", "1", str(png)],
                       check=True)
        gray = _imread_gray(png)
        ys, xs = np.nonzero(gray >= 200)
        if not len(ys):
            print(f"{font_size:>5} {y:>6.2f}   —— 该时间点无字幕")
            continue
        box_w = int(xs.max() - xs.min() + 1)
        box_h = int(ys.max() - ys.min() + 1)
        top, bottom = int(ys.min()), int(ys.max())
        overflow = int(xs.min()) < 2 or int(xs.max()) > width - 3
        gap = int(height * SAFE_BOTTOM_RATIO) - bottom
        if overflow:
            verdict = "★溢出画布"
        elif gap < 0:
            verdict = f"✗侵入底部遮挡区 {-gap}px"
        else:
            verdict = f"✓安全 余量{gap}px"
            ok_rows.append((font_size, y, box_w, bottom))
        print(f"{font_size:>5} {y:>6.2f} {box_w:>6} {box_w / width:>7.3f} {box_h:>5} "
              f"{bottom / height:>7.4f} {height - bottom:>7} {gap:>7}  {verdict}")

    print()
    if ok_rows:
        # 只把「余量够、行宽不贴边」的组合纳入建议，否则会出现贴着安全线的假最优。
        min_gap = int(height * args.min_gap_ratio)
        eligible = [row for row in ok_rows if int(height * SAFE_BOTTOM_RATIO) - row[3] >= min_gap]
        eligible = [row for row in eligible if row[2] / width <= args.max_width_ratio]
        if eligible:
            best = max(eligible, key=lambda r: (r[0], r[1]))
            print(f"安全区内的可选上限（余量 ≥ {min_gap}px、行宽 ≤ {args.max_width_ratio:.2f} 画布宽）："
                  f"font_size={best[0]}  position.y={best[1]}  "
                  f"行宽 {best[2]}px（{best[2] / width:.3f}），余量 "
                  f"{int(height * SAFE_BOTTOM_RATIO) - best[3]}px")
            print("★ 这是上限而非推荐值：字号越大左右留边越少，成片前先用 measure 复核。")
        else:
            print("没有组合同时满足余量与行宽要求，请缩小字号或上移落点。")
    print(f"安全线取值：底边 < {SAFE_BOTTOM_RATIO}（平台底部作者/描述块保守上沿）")
    return 0


# --------------------------------------------------------------------------- apply


def _post(url: str, payload: dict, timeout: float = 60.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("detail") or detail
        except Exception:
            pass
        raise SystemExit(f"HTTP {exc.code}: {detail}") from exc


def cmd_apply(args) -> int:
    project = Path(args.project).resolve()
    project_id = args.project_id or project.name
    base = f"http://127.0.0.1:{args.port}"
    srt = _subtitle_srt(project)
    longest, _ = _cue_stats(srt)

    style = {
        "enabled": True,
        "display_mode": "phrase",
        "font": args.font,
        "font_size": args.font_size,
        "bold": True,
        "text_color": "#FFFFFF",
        "outline_color": "#07111F",
        "outline_width": max(1, round(args.font_size * 0.06)),
        "background_enabled": False,
        "background_color": "#07111F",
        "background_opacity": 68,
        "position": {"x": 0.5, "y": args.y, "width": args.width, "anchor": "bottom-center"},
        "max_lines": 2,
    }
    print(f"目标项目：{project_id}   最长句 {longest} 字")
    print(f"新样式：font_size={args.font_size}  position={style['position']}")

    state = _post(f"{base}/api/project/{project_id}/workbench/subtitle-styles", {
        "template_id": args.template_id,
        "name": args.name,
        "style": style,
        "set_default": True,
        "apply_scope": "all",
    })
    if "detail" in state:
        raise SystemExit(state["detail"])
    template = next((t for t in state["subtitle_styles"]["templates"] if t["id"] == args.template_id), None)
    if template:
        saved = template["style"]
        print(f"已保存：rev={template['revision']}  font_size={saved['font_size']}  "
              f"position={saved['position']}")
    preview = ((state.get("automation") or {}).get("preview_render") or {})
    print(f"preview_render.status = {preview.get('status')}（{preview.get('stale_reason') or '-'}）")

    if not args.queue_preview:
        print("\n未排队（加 --queue-preview 才重出预览）")
        return 0

    request = {
        "project_id": project_id,
        "kind": "full_preview",
        "priority": "normal",
        "idempotency_key": f"{project_id}-fp-{args.font_size}-{int(args.y * 100)}",
        "request": {"confirmed": True},
    }
    result = _post(f"{base}/api/production-queue/jobs", request)
    job = result.get("queue_job") or {}
    print(f"\n已排队 full_preview：{job.get('job_id')}  {job.get('status')}  {job.get('stage')}")
    print("全片预览是纯本地 ffmpeg 合成：零付费。")
    return 0


# --------------------------------------------------------------------------- measure


def cmd_measure(args) -> int:
    project = Path(args.project).resolve()
    video = Path(args.video) if args.video else project / "renders" / "previews" / "full-preview-v004.mp4"
    if not video.is_file():
        candidates = sorted((project / "renders" / "previews").glob("full-preview-v*.mp4"))
        if not candidates:
            raise SystemExit(f"找不到成片：{video}")
        video = candidates[-1]
    print(f"成片：{video}\n")

    tmp = ROOT / ".backlot" / "_tmp_subtitle_safe_area"
    tmp.mkdir(parents=True, exist_ok=True)
    band_top, band_bottom = args.band
    print(f"{'t(s)':>7} {'宽px':>6} {'占画布':>7} {'高px':>5} {'底边':>7} {'距底px':>7} {'距0.80':>7}  判定")
    print("-" * 82)
    for at in args.at:
        png = _frame_at(video, float(at), tmp / f"m_{at}.png")
        box = _box_in_band(png, band_top, band_bottom)
        if not box:
            print(f"{float(at):>7.2f}   —— 带内未检测到字幕像素")
            continue
        gap = int(box["canvas_height"] * SAFE_BOTTOM_RATIO) - box["bottom"]
        verdict = "✓安全" if gap >= 0 else f"★侵入 {-gap}px"
        print(f"{float(at):>7.2f} {box['width']:>6} {box['width'] / box['canvas_width']:>7.3f} "
              f"{box['height']:>5} {box['bottom'] / box['canvas_height']:>7.4f} "
              f"{box['canvas_height'] - box['bottom']:>7} {gap:>7}  {verdict}")
    print()
    print("说明：`--band` 必须框住当前字幕位置。旧落点(0.89)在 y≈1674，新落点(0.75)在 y≈1387；"
          "用错带会把画面本身的高亮当成字幕。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="竖屏字幕安全区：试算 / 应用 / 复核",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="在合成画布上试算候选字号与落点")
    p_probe.add_argument("--project", required=True)
    p_probe.add_argument("--font-sizes", type=int, nargs="+", default=[42, 52, 60, 64, 68])
    p_probe.add_argument("--ys", type=float, nargs="+", default=[0.86, 0.80, 0.78, 0.75])
    p_probe.add_argument("--at", type=float, default=None, help="试算时间点，默认取最长句中点")
    p_probe.add_argument("--min-gap-ratio", type=float, default=0.05,
                         help="纳入建议所需的距安全线余量（占画布高，默认 0.05）")
    p_probe.add_argument("--max-width-ratio", type=float, default=0.85,
                         help="纳入建议允许的最大行宽（占画布宽，默认 0.85）")
    p_probe.set_defaults(func=cmd_probe)

    p_apply = sub.add_parser("apply", help="写入样式并可选重出预览")
    p_apply.add_argument("--project", required=True)
    p_apply.add_argument("--project-id", default="")
    p_apply.add_argument("--port", type=int, default=4754)
    p_apply.add_argument("--template-id", default="subtitle-default")
    p_apply.add_argument("--name", default="标准中文短句字幕（竖屏安全区）")
    p_apply.add_argument("--font", default="Microsoft YaHei")
    p_apply.add_argument("--font-size", type=int, default=DEFAULT_FONT_SIZE)
    p_apply.add_argument("--y", type=float, default=DEFAULT_Y)
    p_apply.add_argument("--width", type=float, default=DEFAULT_WIDTH)
    p_apply.add_argument("--queue-preview", action="store_true")
    p_apply.set_defaults(func=cmd_apply)

    p_measure = sub.add_parser("measure", help="量成片里字幕的像素框与安全余量")
    p_measure.add_argument("--project", required=True)
    p_measure.add_argument("--video", default="")
    p_measure.add_argument("--at", type=float, nargs="+", default=[1.5, 13.0, 36.0, 42.6])
    p_measure.add_argument("--band", type=int, nargs=2, default=[1330, 1580],
                           metavar=("TOP", "BOTTOM"))
    p_measure.set_defaults(func=cmd_measure)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
