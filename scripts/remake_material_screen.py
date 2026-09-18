"""外站素材复刻 · 素材双筛（人脸 + 烧入字幕）

为什么需要这个脚本：
  「不能有人脸」这类要求不能靠肉眼翻素材，也不能只靠一次人脸检测——
  低阈值会误报（圆形镜头模组、铰链的 V 形结构），高阈值会漏检
  （背景小人群、被 AI 打码的脸）。同样，「不要字幕画面」在手机 UI 素材上
  用单帧检测必然误报（iOS 桌面全是小字）。所以这里用两条互补的证据：

  1) 人脸：整片均匀抽帧 + YuNet，两种面积阈值都扫；命中的时刻一律再单独
     出「带检测框」的复核图，供人工判定是真是假。
  2) 烧入字幕：字幕的真实特征不是「有字」而是「同一段 y 区间反复出现」。
     于是求整片每行边缘密度的时序均值，位置稳定的高峰带才是字幕带；
     它同时给出「裁切几何」与「该带在各时刻是否活跃」。

用法：
  python scripts/remake_material_screen.py screen  --video-dir <dir> [--json out.json]
  python scripts/remake_material_screen.py pick    --video-dir <dir> --id <aweme_id> --times 0.5,3,5.5
  python scripts/remake_material_screen.py verify  --video-dir <dir> --id <aweme_id> --times 14.5
  python scripts/remake_material_screen.py audit   --project projects/<id> [--out dir]

关于 audit（复刻项目的最终门禁）：
  规格书里的素材窗口是在「原始素材」上验证的，但成片真正用的是
  「清洗母版 + 源区间」。而且数字人母版落地会把每段场景时长缩到配音时钟，
  显示窗口同比缩短（渲染端只取 -ss source_in 与缩短后的时长，等于掐尾），
  于是**实际生效窗口 = 原窗口的前缀**。前缀仍是原子集，理论上继承「无脸无字幕」，
  但这条推理链条太长，必须实测：audit 直接在母版上、在实际生效窗口内重跑
  人脸 + 字幕带检测，并出联系表供人工复核。

注意（本机踩过的坑）：
  * 路径含中文时 cv2.imread / imwrite 会静默失败 → 统一走 fromfile/imdecode。
  * 抽帧必须用 JPEG（png 会显著拖慢），并加 -pix_fmt yuvj420p。
  * ffmpeg 用 lib/ffmpeg_locator.py 解析，不要依赖 PATH 上的版本。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.ffmpeg_locator import resolve_ffmpeg_with_option  # noqa: E402


SAMPLE_FPS = 2.0
AUDIT_FPS = 3.0                    # 生效窗口很短（3s 级），密一点才有统计意义
ROW_ACTIVE_RATIO = 0.30
BAND_MIN_RATIO = 0.012
BAND_MAX_RATIO = 0.16
BAND_ACTIVE_MIN_RATIO = 0.25
DEFAULT_FACE_MIN_AREA = 0.015     # 主扫描：明确的出镜人脸
SMALL_FACE_MIN_AREA = 0.0035      # 复核扫描：背景小脸 / 屏内人像


def resolve_ffmpeg() -> tuple[Path, Path]:
    """返回 (ffmpeg, ffprobe)。必须挑支持 -filter_complex_script 的那一对——
    PATH 上的某些 master 构建不支持它，数字人母版合成会直接失败。"""
    pair = resolve_ffmpeg_with_option("-filter_complex_script")
    if not pair:
        raise SystemExit("未找到可用的 ffmpeg（需要支持 -filter_complex_script）")
    return Path(pair[0]), Path(pair[1])


def imread_u(path: Path, flags: int = cv2.IMREAD_COLOR):
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, flags) if data.size else None


def imwrite_u(path: Path, image, params=None) -> bool:
    ok, buf = cv2.imencode(Path(path).suffix, image, params or [])
    if ok:
        buf.tofile(str(path))
    return ok


def probe(path: Path, ffprobe: Path) -> dict:
    cmd = [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return {"duration": float((out.stdout or "0").strip() or 0.0)}


def model_path() -> Path:
    """YuNet 模型：优先用项目内副本，其次从 copy_skill 参考项目借。"""
    candidates = [
        REPO / ".backlot/models/face_detection_yunet_2023mar.onnx",
        Path(r"D:\刘宇钊\codex_work\copy_skill\copy_skill-main\data\models\face\face_detection_yunet_2023mar.onnx"),
    ]
    for src in candidates:
        if src.is_file():
            work = Path(tempfile.gettempdir()) / "haike-face-scan"
            work.mkdir(parents=True, exist_ok=True)
            dst = work / "yunet.onnx"          # ASCII 路径，避开中文路径问题
            if not dst.is_file():
                shutil.copy2(src, dst)
            return dst
    raise SystemExit("未找到 YuNet 人脸模型（face_detection_yunet_2023mar.onnx）")


def row_edge_profile(gray: np.ndarray) -> np.ndarray:
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    width = gray.shape[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(8, width // 14), 1))
    merged = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)
    return (merged > 0).sum(axis=1).astype(np.float32) / float(width)


def detect_bands(profiles: np.ndarray, height: int) -> list[dict]:
    mean = profiles.mean(axis=0)
    presence = (profiles > BAND_ACTIVE_MIN_RATIO).mean(axis=0)
    rows = (mean * presence) > ROW_ACTIVE_RATIO * 0.55
    bands: list[dict] = []
    i = 0
    while i < len(rows):
        if not rows[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(rows) and rows[j + 1]:
            j += 1
        ratio = (j - i + 1) / height
        if BAND_MIN_RATIO <= ratio <= BAND_MAX_RATIO:
            seg = profiles[:, i:j + 1]
            active_rows = (seg > BAND_ACTIVE_MIN_RATIO).sum(axis=1) / (j - i + 1)
            bands.append({
                "top_ratio": round(i / height, 4),
                "bottom_ratio": round((j + 1) / height, 4),
                "height_ratio": round(ratio, 4),
                "active_frame_ratio": round(float((active_rows > 0.35).mean()), 4),
            })
        i = j + 1
    merged: list[dict] = []
    for band in bands:
        if merged and band["top_ratio"] - merged[-1]["bottom_ratio"] < 0.006:
            merged[-1]["bottom_ratio"] = band["bottom_ratio"]
            merged[-1]["height_ratio"] = round(
                merged[-1]["bottom_ratio"] - merged[-1]["top_ratio"], 4)
        else:
            merged.append(dict(band))
    return merged


class FrameSampleError(RuntimeError):
    """单条源抽帧失败。★ 设计成可捕获异常，让 `cmd_screen` 跳过该条继续跑整批。"""


def safe_dir_stem(stem: str, limit: int = 24) -> str:
    """把文件名干熔成「能安全出现在 ffmpeg 输出模板里」的目录名。

    ★ 2026-09-17 踩到（musk 期 `极说_80%合并概率！拆解特斯拉与SpaceX_7660134287102268323.mp4`）：
      `sample_frames` 把 `out_dir / "%05d.jpg"` 这**整条路径**交给 ffmpeg，而 image2
      muxer 会扫描路径里**所有** `%` 找序列占位符。词干里的 `80%` 把 `%合` 变成了
      "非法占位符" ⇒ ffmpeg 判定"这不是序列模式"，于是拒绝写第二个文件：
        `Cannot write more than one file with the same name. Are you missing
         the -update option or a sequence pattern?`
      后果是**整条源被跳过**（musk 双筛 rc=1、六期里那一期直接没有 json）。
      ⇒ `%` 一律替换掉；顺带挡掉路径分隔符与通配符，目录名永远可预测。
    """
    bad = '%"*/:<>?\\|\n\r\t'
    out = "".join("_" if ch in bad else ch for ch in stem[:limit]).strip()
    return out or "video"


def sample_frames(ffmpeg: Path, path: Path, out_dir: Path, fps: float) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(ffmpeg), "-y", "-v", "error", "-i", str(path), "-vf", f"fps={fps}",
           "-pix_fmt", "yuvj420p", "-q:v", "3", str(out_dir / "%05d.jpg")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        # 抛可捕获异常而不是 SystemExit：一条源坏掉不该让整批（6 期）白跑。
        raise FrameSampleError(f"抽帧失败：{(r.stderr or '').strip()[:300]}")
    return sorted(out_dir.glob("*.jpg"))


def scan_faces(path: Path, model: Path, fps: float, min_area: float) -> dict:
    detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 5000)
    cap = cv2.VideoCapture(str(path))
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(video_fps / fps)))
    hits, sampled, index = [], 0, 0
    while True:
        if not cap.grab():
            break
        if index % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                sampled += 1
                h, w = frame.shape[:2]
                detector.setInputSize((w, h))
                _, faces = detector.detect(frame)
                if faces is not None and len(faces):
                    best = float(max(f[2] * f[3] for f in faces) / (w * h))
                    if best >= min_area:
                        hits.append({"t": round(index / video_fps, 2),
                                     "area_ratio": round(best, 5), "count": int(len(faces))})
        index += 1
    cap.release()
    return {
        "sampled_frames": sampled,
        "min_area_ratio": min_area,
        "hits": hits,
        "hit_times": [h["t"] for h in hits],
        "hit_ratio": round(len(hits) / max(1, sampled), 4),
        "max_area_ratio": round(max([h["area_ratio"] for h in hits], default=0.0), 5),
    }


def cmd_screen(args: argparse.Namespace) -> int:
    ffmpeg, ffprobe = resolve_ffmpeg()
    model = model_path()
    out_dir = Path(args.out or (REPO / ".backlot/material-screen"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, dict] = {}
    for path in sorted(Path(args.video_dir).iterdir()):
        if path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
            continue
        duration = probe(path, ffprobe)["duration"]
        frames_dir = out_dir / f".frames-{safe_dir_stem(path.stem)}"
        try:
            frames = sample_frames(ffmpeg, path, frames_dir, SAMPLE_FPS)
        except FrameSampleError as exc:
            print(f"跳过（{exc}）: {path.name}", flush=True)
            continue
        if not frames:
            print(f"跳过（无帧）: {path.name}", flush=True)
            continue
        first = imread_u(frames[0])
        height, width = first.shape[:2]
        profiles = [row_edge_profile(imread_u(f, cv2.IMREAD_GRAYSCALE)) for f in frames]
        profiles = [p for p in profiles if p is not None]
        matrix = np.vstack(profiles) if profiles else np.zeros((1, height), np.float32)
        bands = detect_bands(matrix, height)
        main_faces = scan_faces(path, model, SAMPLE_FPS, DEFAULT_FACE_MIN_AREA)
        small_faces = scan_faces(path, model, SAMPLE_FPS, SMALL_FACE_MIN_AREA)
        report[path.name] = {
            "file": path.name,
            "duration": round(duration, 2),
            "size": f"{width}x{height}",
            "subtitle_bands": bands,
            "faces_primary": main_faces,
            "faces_small": small_faces,
            "verdict": (
                "face_free_no_subtitle" if not main_faces["hits"] and not bands
                else "needs_review"
            ),
        }
        shutil.rmtree(frames_dir, ignore_errors=True)
        band_text = ", ".join(f"{b['top_ratio']:.3f}-{b['bottom_ratio']:.3f}" for b in bands) or "无"
        print(f"{path.name[:40]:<42} {width}x{height} {duration:6.1f}s "
              f"字幕带[{band_text}] 人脸命中 {main_faces['hit_ratio']:.3f} "
              f"小脸 {small_faces['hit_ratio']:.3f}", flush=True)
    target = Path(args.json or "screen-report.json")
    # ★ 2026-09-17 踩到：`--json` 传 `.backlot/_diag/screen-leijun-bak.json`（带目录的
    #   相对路径）时，旧写法 `out_dir / args.json` 会拼成
    #   `<out_dir>/.backlot/_diag/...` ⇒ 目录不存在，FileNotFoundError 抛在**最后一行**，
    #   前面整轮抽帧/人脸/字幕带检测全部白跑（leijun 备份目录 5 条就这么丢过一次结果）。
    #   约定：裸文件名 → 仍落在 `--out` 目录下（保持旧行为）；带目录成分 → 按调用方 cwd 解析。
    if not target.is_absolute() and target.parent != Path("."):
        target = Path.cwd() / target
    else:
        target = out_dir / target
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("WROTE", target)
    return 0


def _grab(ffmpeg: Path, path: Path, t: float, width: int) -> np.ndarray | None:
    tmp = Path(tempfile.gettempdir()) / f"haike-grab-{abs(hash((str(path), t, width))) % 10**10}.jpg"
    cmd = [str(ffmpeg), "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path),
           "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2",
           "-pix_fmt", "yuvj420p", "-q:v", "3", str(tmp)]
    subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    img = imread_u(tmp)
    tmp.unlink(missing_ok=True)
    return img


def _sheet(tiles: list[np.ndarray], cols: int, target: Path) -> None:
    height = max(t.shape[0] for t in tiles)
    width = max(t.shape[1] for t in tiles)
    padded = [cv2.copyMakeBorder(t, 0, height - t.shape[0], 0, width - t.shape[1],
                                 cv2.BORDER_CONSTANT, value=(20, 20, 20)) for t in tiles]
    rows = []
    for i in range(0, len(padded), cols):
        chunk = padded[i:i + cols]
        while len(chunk) < cols:
            chunk.append(np.zeros_like(padded[0]))
        rows.append(np.hstack(chunk))
    imwrite_u(target, np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 86])


def cmd_pick(args: argparse.Namespace) -> int:
    ffmpeg, _ = resolve_ffmpeg()
    out_dir = Path(args.out or (REPO / ".backlot/material-screen/pick"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = next(p for p in Path(args.video_dir).iterdir() if args.id in p.name)
    times = [float(t) for t in args.times.split(",")]
    tiles = []
    for t in times:
        img = _grab(ffmpeg, path, t, 480)
        if img is None:
            continue
        cv2.rectangle(img, (0, 0), (118, 26), (0, 0, 0), -1)
        cv2.putText(img, f"{t:.1f}s", (6, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2,
                    cv2.LINE_AA)
        tiles.append(img)
    target = out_dir / f"{args.id}-pick.jpg"
    _sheet(tiles, args.cols, target)
    print("WROTE", target)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    ffmpeg, _ = resolve_ffmpeg()
    model = model_path()
    out_dir = Path(args.out or (REPO / ".backlot/material-screen/verify"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = next(p for p in Path(args.video_dir).iterdir() if args.id in p.name)
    detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 5000)
    tiles, findings = [], []
    for t in [float(x) for x in args.times.split(",")]:
        img = _grab(ffmpeg, path, t, 640)
        if img is None:
            continue
        h, w = img.shape[:2]
        detector.setInputSize((w, h))
        _, faces = detector.detect(img)
        found = []
        if faces is not None:
            for f in faces:
                x, y, fw, fh = [float(v) for v in f[:4]]
                ratio = (fw * fh) / (w * h)
                if ratio < SMALL_FACE_MIN_AREA:
                    continue
                found.append({"area_ratio": round(ratio, 5),
                              "box": [round(v) for v in (x, y, fw, fh)]})
                cv2.rectangle(img, (int(x), int(y)), (int(x + fw), int(y + fh)), (0, 0, 255), 2)
                cv2.putText(img, f"{ratio:.4f}", (int(x), max(14, int(y) - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
        cv2.rectangle(img, (0, 0), (96, 22), (0, 0, 0), -1)
        cv2.putText(img, f"{t:.2f}s", (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1,
                    cv2.LINE_AA)
        findings.append({"t": t, "faces": found})
        tiles.append(img)
    target = out_dir / f"{args.id}-facebox.jpg"
    _sheet(tiles, max(1, len(tiles)), target)
    (out_dir / f"{args.id}-facebox.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=1), encoding="utf-8")
    print("WROTE", target)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """在「母版 + 生效窗口」上复检人脸与字幕带——复刻项目的最终门禁。"""
    ffmpeg, _ = resolve_ffmpeg()
    model = model_path()
    project = Path(args.project)
    if not project.is_absolute():
        project = REPO / project
    workbench = project / "artifacts" / "workbench.json"
    if not workbench.is_file():
        raise SystemExit(f"找不到 {workbench}")
    state = json.loads(workbench.read_text(encoding="utf-8"))
    assets = {str(a.get("id")): a for a in state.get("assets", [])}
    fps = float((state.get("settings") or {}).get("frame_rate") or 30)

    scales = _scene_scales(project, state)
    out_dir = Path(args.out or (project / "audit"))
    out_dir.mkdir(parents=True, exist_ok=True)

    windows: list[dict] = []
    for scene in sorted(state.get("scenes", []), key=lambda s: int(s.get("order") or 0)):
        scale = scales.get(str(scene.get("id")), 1.0)
        for block in ((scene.get("visual_timeline") or {}).get("blocks") or []):
            src_in = block.get("source_in_seconds")
            if src_in is None:
                continue
            start = float(block.get("start_seconds") or 0)
            end = float(block.get("end_seconds") or 0)
            disp = end - start
            spec_out = float(block.get("source_out_seconds") or 0)
            # 渲染端只取 `-ss source_in` 加「显示时长」（workbench: `-t` = end - start），
            # 所以真实生效窗口恒为 [source_in, source_in + disp]；`scale` 只在
            # 「场景被配音时钟缩短、而 source_out 没跟着改」时用来推断掐尾位置。
            # 按 scale 取窗口会在音频反而变长时少测尾部，故这里一律按 disp 覆盖全长。
            eff = round(disp, 3)
            disp_frames = _audit_frame(end, fps) - _audit_frame(start, fps)
            src_frames = _audit_frame(spec_out, fps) - _audit_frame(src_in, fps)
            asset = assets.get(str(block.get("asset_id"))) or {}
            windows.append({
                "scene_id": scene.get("id"), "block_id": block.get("id"),
                "asset_id": block.get("asset_id"),
                "path": str(project / str(asset.get("path") or "")),
                "scene_scale": round(scale, 4),
                "display_seconds": round(eff, 3),
                "eff_in": round(float(src_in), 3),
                "eff_out": round(float(src_in) + eff, 3),
                "spec_in": round(float(src_in), 3),
                "spec_out": round(spec_out, 3),
                # 源出点与显示区间按帧不一致 = 母版落地/重定时后 source_out 已过期，
                # 渲染仍会按 disp 取源，等于悄悄越过了规格书里已核验的窗口。
                "stale_source_out": abs(disp_frames - src_frames) > 1,
                "within_verified": False,
            })
    # 生效窗口是否落在「本项目已核验过的安全窗口」之外，只能靠人工/上游筛选记录判断；
    # 这里退化为「是否越出素材片长」这一可自动判定的硬门槛。
    for win in windows:
        asset = assets.get(str(win["asset_id"])) or {}
        available = float(asset.get("duration_seconds") or 0)
        win["within_verified"] = (not available) or win["eff_out"] <= available + 1.0 / fps + 1e-9

    detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 5000)
    tiles: list[np.ndarray] = []
    for win in windows:
        frames_dir = out_dir / f".w-{win['block_id']}-{win['scene_id']}"
        frames_dir.mkdir(parents=True, exist_ok=True)
        span = max(0.2, win["display_seconds"])
        cmd = [str(ffmpeg), "-y", "-v", "error", "-ss", f"{win['eff_in']:.3f}",
               "-t", f"{span:.3f}", "-i", win["path"],
               "-vf", f"fps={AUDIT_FPS}", "-pix_fmt", "yuvj420p", "-q:v", "3",
               str(frames_dir / "%03d.jpg")]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise SystemExit(f"{win['block_id']} 抽帧失败：{r.stderr[:200]}")
        files = sorted(frames_dir.glob("*.jpg"))
        if not files:
            raise SystemExit(f"{win['block_id']} 生效窗口内没有抽到帧")
        hits, profiles = [], []
        for index, file in enumerate(files):
            img = imread_u(file)
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            profiles.append(row_edge_profile(gray))
            height, width = img.shape[:2]
            detector.setInputSize((width, height))
            _, faces = detector.detect(img)
            if faces is not None and len(faces):
                best = float(max(f[2] * f[3] for f in faces) / (width * height))
                if best >= SMALL_FACE_MIN_AREA:
                    hits.append({"t": round(win["eff_in"] + index / AUDIT_FPS, 3),
                                 "area_ratio": round(best, 5), "count": int(len(faces))})
        matrix = np.vstack(profiles) if profiles else np.zeros((1, 1), np.float32)
        bands = [b for b in detect_bands(matrix, matrix.shape[1])
                 if b["active_frame_ratio"] >= 0.6] if matrix.shape[1] > 1 else []
        win.update({
            "frames": len(files),
            "face_hits": hits,
            "face_free": not hits,
            "stable_bands": bands,
            "subtitle_free": not bands,
            "verdict": "pass" if not hits and not bands else "review",
        })
        for index in sorted({0, len(files) // 2, len(files) - 1}):
            img = imread_u(files[index])
            if img is None:
                continue
            if img.shape[1] > 320:
                ratio = 320 / img.shape[1]
                img = cv2.resize(img, (320, max(2, int(img.shape[0] * ratio))))
            cv2.rectangle(img, (0, 0), (176, 24), (0, 0, 0), -1)
            cv2.putText(img, f"{win['block_id']} {win['eff_in']:.1f}+{index / AUDIT_FPS:.1f}",
                        (4, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (0, 255, 255), 1, cv2.LINE_AA)
            if hits:
                cv2.putText(img, "FACE", (250, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (0, 0, 255), 2, cv2.LINE_AA)
            tiles.append(img)
        shutil.rmtree(frames_dir, ignore_errors=True)
        print(f"{win['scene_id']}/{win['block_id']} {win['eff_in']:7.3f}-{win['eff_out']:7.3f} "
              f"({win['display_seconds']:5.2f}s) 人脸 {len(hits)} 字幕带 {len(bands)} "
              f"{'OK' if win['verdict'] == 'pass' else '★需复核'}"
              f"{'  [源出点已过期]' if win.get('stale_source_out') else ''}"
              f"{'  [越出素材片长]' if not win['within_verified'] else ''}", flush=True)

    if tiles:
        _sheet(tiles, 6, out_dir / "window-audit.jpg")
    report = {
        "project": str(project),
        "fps": fps,
        "scene_scales": scales,
        "windows": windows,
        "summary": {
            "windows": len(windows),
            "face_hits": sum(1 for w in windows if w["face_hits"]),
            "band_hits": sum(1 for w in windows if w["stable_bands"]),
            "not_within_verified": [w["block_id"] for w in windows if not w["within_verified"]],
            "stale_source_out": [w["block_id"] for w in windows if w.get("stale_source_out")],
            "total_display_seconds": round(sum(w["display_seconds"] for w in windows), 3),
        },
    }
    target = out_dir / "window-audit.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("WROTE", target)
    print("SUMMARY", json.dumps(report["summary"], ensure_ascii=False))
    return 0


def _audit_frame(value: float, fps: float) -> int:
    """与工作台 _nonnegative_frame 同款取帧：floor(t * fps + .5)。"""
    return max(0, int(math.floor(max(0.0, value) * max(1.0, fps) + .5)))


def _scene_scales(project: Path, state: dict) -> dict[str, float]:
    """每个场景「配音时钟 / 分镜时长」的比值。没有时间轴清单就返回全 1（未落地）。"""
    manifest = project / "assets/audio/avatar-review-preview/timing-manifest.json"
    scenes = {str(s.get("id")): s for s in state.get("scenes", [])}
    if not manifest.is_file():
        return {scene_id: 1.0 for scene_id in scenes}
    data = json.loads(manifest.read_text(encoding="utf-8"))
    turns = {str(t.get("turn_id")): t for t in data.get("turns", [])}
    script = json.loads((project / "artifacts/script.json").read_text(encoding="utf-8"))
    turn_of_section = {str(s.get("id")): str(s.get("turn_id") or s.get("id"))
                       for s in script.get("sections", [])}
    scales: dict[str, float] = {}
    for scene_id, scene in scenes.items():
        old = max(0.04, float(scene.get("end_seconds") or 0) - float(scene.get("start_seconds") or 0))
        turn = turns.get(turn_of_section.get(str(scene.get("script_section_id")), ""))
        if not turn:
            scales[scene_id] = 1.0
            continue
        # 清单里轮次的时间字段是 source_*_seconds（该轮在母版原声时钟上的归属区间）。
        start = turn.get("source_start_seconds", turn.get("start_seconds"))
        end = turn.get("source_end_seconds", turn.get("end_seconds"))
        new = float(end or 0) - float(start or 0)
        scales[scene_id] = round(new / old, 4) if new > 0 else 1.0
    return scales


def main() -> int:
    parser = argparse.ArgumentParser(description="外站素材复刻 · 素材双筛")
    sub = parser.add_subparsers(dest="command", required=True)

    p_screen = sub.add_parser("screen", help="整目录人脸 + 烧入字幕双筛")
    p_screen.add_argument("--video-dir", required=True)
    p_screen.add_argument("--out")
    p_screen.add_argument("--json")
    p_screen.set_defaults(func=cmd_screen)

    p_pick = sub.add_parser("pick", help="带时间码的精细抽帧表（挑镜头用）")
    p_pick.add_argument("--video-dir", required=True)
    p_pick.add_argument("--id", required=True, help="文件名里的视频 id（支持部分匹配）")
    p_pick.add_argument("--times", required=True, help="逗号分隔的秒数")
    p_pick.add_argument("--cols", type=int, default=4)
    p_pick.add_argument("--out")
    p_pick.set_defaults(func=cmd_pick)

    p_verify = sub.add_parser("verify", help="把检测框画出来，人工判定真假命中")
    p_verify.add_argument("--video-dir", required=True)
    p_verify.add_argument("--id", required=True)
    p_verify.add_argument("--times", required=True)
    p_verify.add_argument("--out")
    p_verify.set_defaults(func=cmd_verify)

    p_audit = sub.add_parser("audit", help="母版 + 生效窗口的人脸/字幕复检（最终门禁）")
    p_audit.add_argument("--project", required=True, help="项目目录，如 projects/apple-fold-duo-remake-1")
    p_audit.add_argument("--out", help="输出目录，默认 <项目>/audit")
    p_audit.set_defaults(func=cmd_audit)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
