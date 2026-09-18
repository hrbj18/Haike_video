"""外站素材复刻 · 把规格书建成本机项目（可复刻工作流的核心步骤）。

输入：一份 remake-spec.json（素材清单 + 分镜 + 配音/音乐设置）。
产出：projects/<project_id>/ 下的完整可渲染状态：
  1. 素材清洗母版（机械裁掉底部烧入字幕带、剥离原声）
  2. 已登记的资产记录（含出处与被剔除内容的说明）
  3. 已通过的正式脚本（script.json + workbench 的 script_draft）
  4. 分镜场景，每段视觉时间线由本地切片完整覆盖（full_bleed）
  5. 项目级 BGM 与旁白增益策略

设计取舍：
  * 不做「先切成一堆小片段」——直接登记清洗母版 + 用 source_in/out 引用区间。
    好处是时间轴不变、出处可追溯、也不会因为切点四舍五入而变速。
  * 裁字幕只裁底部一条带，时间轴不受影响，所以 source_in/out 依然按原片秒数写。
  * 母版一律 `-an`：复刻片必须用自己的配音，原博主声音不能进成片。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backlot import music_library as ml  # noqa: E402
from backlot import workbench as wb  # noqa: E402
from backlot.music_preferences import DEFAULT_PLAYBACK_GAIN_DB  # noqa: E402
from backlot.narration_preferences import (  # noqa: E402
    DEFAULT_NARRATION_GAIN_DB,
    clamp_narration_gain_db,
)
from backlot.state import PROJECTS_DIR  # noqa: E402
from lib.checkpoint import init_project  # noqa: E402

FFMPEG = REPO / ".venv/Lib/site-packages/static_ffmpeg/bin/win32/ffmpeg.exe"
FFPROBE = FFMPEG.parent / "ffprobe.exe"
CLEAN_DIR = Path("assets/video/clean-master")


def probe(path: Path) -> dict:
    cmd = [str(FFPROBE), "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,r_frame_rate",
           "-show_entries", "format=duration", "-of", "json", str(path)]
    out = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout or "{}")
    stream = (out.get("streams") or [{}])[0]
    num, _, den = str(stream.get("r_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1)
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(fps, 4),
        "duration": round(float((out.get("format") or {}).get("duration") or 0.0), 3),
    }


def find_source(spec: dict, source: dict) -> Path:
    root = Path(spec["material_root"])
    for f in root.iterdir():
        if f.name.endswith(source["aweme_id"] + ".mp4"):
            return f
    raise SystemExit(f"未找到素材文件：{source['key']} {source['aweme_id']}")


def build_clean_master(project_dir: Path, spec: dict, source: dict) -> tuple[Path, dict]:
    """裁掉底部字幕带 + 剥离原声，产出清洗母版。"""
    origin = find_source(spec, source)
    target_dir = project_dir / CLEAN_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source['key']}-{source['aweme_id']}.mp4"
    facts = probe(origin)
    crop = float(source.get("crop_bottom_ratio") or 0.0)
    vf = "null"
    if crop > 0:
        keep = max(0.5, 1.0 - crop)
        # 高度取偶数，避免 yuv420p 编码报错
        vf = f"crop=iw:trunc(ih*{keep:.4f}/2)*2:0:0"
    cmd = [str(FFMPEG), "-y", "-v", "error", "-i", str(origin),
           "-vf", vf, "-an",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(target)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not target.is_file():
        raise SystemExit(f"清洗母版生成失败 {source['key']}: {r.stderr[:300]}")
    cleaned = probe(target)
    if crop > 0 and cleaned["height"] >= facts["height"]:
        raise SystemExit(f"{source['key']} 字幕带裁切未生效：{cleaned['height']} vs {facts['height']}")
    if abs(cleaned["duration"] - facts["duration"]) > 1.0:
        raise SystemExit(f"{source['key']} 清洗后时长漂移：{cleaned['duration']} vs {facts['duration']}")
    return target, cleaned


def build_script(spec: dict) -> dict:
    sections = []
    cursor = 0.0
    for section in spec["sections"]:
        shots = section["shots"]
        duration = round(sum(round(float(s["out"]) - float(s["in"]), 3) for s in shots), 3)
        start = round(cursor, 3)
        end = round(cursor + duration, 3)
        cues = []
        beat = start
        for shot in shots:
            span = round(float(shot["out"]) - float(shot["in"]), 3)
            cues.append({"timestamp_seconds": round(beat + span / 2, 3),
                         "description": str(shot["intent"])})
            beat += span
        sections.append({
            "id": section["id"],
            "turn_id": section["id"],
            "label": f"{section['label']}",
            "text": section["text"],
            "speaker_id": spec["voice"]["role"],
            "start_seconds": start,
            "end_seconds": end,
            "enhancement_cues": cues,
        })
        cursor = end
    return {
        "version": "1.0",
        "title": spec["title"],
        "total_duration_seconds": round(cursor, 3),
        "sections": sections,
        "metadata": {
            "audio_mode": "narration",
            "remake_of": "外站同题材短视频（仅借鉴选题与信息结构，文案为重写）",
            "speaker": spec["voice"]["profile_name"],
        },
    }


def main(spec_path: Path) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    project_id = spec["project_id"]
    project_dir = PROJECTS_DIR / project_id

    # ---- 1. 项目骨架 -------------------------------------------------------
    init_project(project_id, title=spec["title"], pipeline_type=spec["pipeline_type"],
                 pipeline_dir=PROJECTS_DIR, style_playbook=spec["style_playbook"])
    marker_path = project_dir / "project.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["render_profile"] = {"aspect_ratio": spec["aspect"], "width": 1080, "height": 1920,
                                "fps": 30, "audio_sample_rate": 48000}
    marker["intake"] = {
        "aspect": spec["aspect"], "aspect_label": "竖版 9:16",
        "created_from": "remake_workflow", "duration_source": "audio_driven",
        "brief": spec["brief"],
    }
    # ★ 2026-09-17：**无数字人期不得写 `avatar` 键**。
    #   原实现无条件写 `avatar: {..., "default_treatment": "custom"}`，副作用是：
    #   `_normalize_intake` 一见到 avatar 就把字段补全，而 "custom" 属于
    #   `PRESENTER_TREATMENTS` 的合法值 ⇒ 后续 `_scene_presenter()` 不会把它回退成
    #   "hidden"，于是 animated-explainer 期每次重建都被推回"有数字人"上下文。
    #   判据与源码保持一致（非自创）：
    #     `_is_avatar_project(state)` == `pipeline_type == AVATAR_PIPELINE`
    #     （`backlot/workbench.py:4611` 用它决定"是否强制要求数字人素材"）
    #   ⇒ 非 avatar 期直接不写该键，让 `_presenter_default()`（treatment="hidden"）生效。
    if spec["pipeline_type"] == "avatar-spokesperson":
        marker["intake"]["avatar"] = {
            "source_status": "ready", "generation_mode": "runninghub_longcat",
            "import_mode": "per_turn", "default_treatment": "custom",
            "background_mode": "opaque",
        }
    else:
        print(f"[1] pipeline_type={spec['pipeline_type']} → 无数字人（intake 不写 avatar）",
              flush=True)
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[1] 项目骨架就绪 {project_dir}", flush=True)

    # ---- 2. 素材清洗母版 + 登记 -------------------------------------------
    state = wb._load_for_write(project_dir)
    existing_paths = {str(a.get("path")) for a in state.get("assets") or []}
    master_info: dict[str, dict] = {}
    for source in spec["sources"]:
        target, facts = build_clean_master(project_dir, spec, source)
        rel = target.relative_to(project_dir).as_posix()
        master_info[source["key"]] = {"path": rel, "facts": facts, "rel": rel}
        if rel not in existing_paths:
            wb.add_asset(project_dir, {
                "name": f"{source['key']} {source['title']}",
                "type": "video",
                "source_type": "web_download",
                "path": rel,
                "duration_seconds": facts["duration"],
                "resolution": f"{facts['width']}x{facts['height']}",
                "provider": f"外站下载（{source['author']}）",
                "source_tool": "clean_master_crop_no_audio",
                "license": "复刻对标素材：仅用于本项目画面重剪，发布前请自行确认授权",
                "source_url": source["url"],
            })
        print(f"    母版 {source['key']}: {facts['width']}x{facts['height']} "
              f"{facts['duration']}s crop={source.get('crop_bottom_ratio')}", flush=True)

    state = wb._load_for_write(project_dir)
    assets = {a["id"]: a for a in state.get("assets") or []}
    key_to_asset = {}
    for key, info in master_info.items():
        for asset in assets.values():
            if str(asset.get("path")) == info["rel"]:
                key_to_asset[key] = asset
                break
        if key not in key_to_asset:
            raise SystemExit(f"资产登记失败：{key}")

    # ---- 2b. 清理上一版残留的素材资产（★ 必须做，否则下游会绑错素材）-------
    # 2026-09-15 事故：本脚本原来**只增不删**资产。整期换素材后（aweme_id 变了
    # ⇒ clean-master 文件名变了），旧资产仍留在表里，且与新资产**共用 "S1 " 名字前缀**；
    # `pp8.step_retime` 当时用 `name.startswith(f"{k} ")` 反查映射，`next()` 取先出现者
    # ⇒ 画面块被悄悄绑回旧素材（powerbank 开头 4 块仍是央视画面、ram 的猫meme、
    # deepseek 的静态录屏）。现在 retime 已改成按 path 精确匹配，这里再把残留扫干净，
    # 让资产表本身也不含歧义（UI 里也不会看到两条同名素材）。
    expect = {(project_dir / CLEAN_DIR / f"{s['key']}-{s['aweme_id']}.mp4").relative_to(project_dir).as_posix()
              for s in spec["sources"]}
    stale = [a for a in state.get("assets") or []
             if isinstance(a, dict)
             and str(a.get("path") or "").startswith(CLEAN_DIR.as_posix() + "/")
             and str(a.get("path")) not in expect]

    # 2b-1. 保留资产的**名字也要跟着 spec 刷新**：aweme_id 没变（如 recrop 再构图版）
    #       时文件名相同、不会新增登记，但 title 变了；不刷新就会出现
    #       「名字写着旧标题、文件其实是新内容」的误导（ram S1 实测）。
    title_by_rel = {(project_dir / CLEAN_DIR / f"{s['key']}-{s['aweme_id']}.mp4")
                    .relative_to(project_dir).as_posix(): str(s.get("title") or "")
                    for s in spec["sources"]}
    renamed = 0
    for a in state.get("assets") or []:
        if not isinstance(a, dict):
            continue
        rel = str(a.get("path") or "")
        want_name = title_by_rel.get(rel)
        if not want_name:
            continue
        key_of = rel.split("/")[-1].split("-", 1)[0]
        full = f"{key_of} {want_name}"
        if str(a.get("name") or "") != full:
            a["name"] = full
            renamed += 1
    if renamed:
        wb._save(project_dir, state)
        print(f"[2b] 刷新 {renamed} 个资产显示名（aweme_id 未变但标题变了）", flush=True)
    if stale:
        # 移入项目内回收目录（不是删除），保留可回溯
        recycle = project_dir / "assets" / "_recycle" / "remake-stale"
        for a in stale:
            src = project_dir / str(a.get("path"))
            if src.is_file():
                dst = recycle / src.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    dst = dst.with_name(f"{dst.stem}-{a.get('id')}{dst.suffix}")
                shutil.move(str(src), str(dst))
                print(f"[2b] 残留素材资产移入回收：{src.name} ← {a.get('id')} "
                      f"{str(a.get('name'))[:36]}", flush=True)
            else:
                print(f"[2b] 残留素材资产登记已移除（文件不存在）：{a.get('id')} "
                      f"{str(a.get('name'))[:36]}", flush=True)
        removed = {str(a.get("id")) for a in stale}
        state["assets"] = [a for a in state.get("assets") or []
                           if str(a.get("id")) not in removed]
        wb._save(project_dir, state)
        print(f"[2b] 共清理 {len(stale)} 个残留素材资产（新的 {len(expect)} 个保留）", flush=True)
    else:
        print(f"[2b] 无残留素材资产（{len(expect)} 个）", flush=True)

    # ---- 3. 正式脚本 -------------------------------------------------------
    script = build_script(spec)
    draft = state["project"].setdefault("script_draft", {})
    draft.update({
        "status": "approved", "mode": "remake_rewrite", "revision": 1, "approved_revision": 1,
        "created_at": draft.get("created_at") or wb._now(),
        "approved_at": wb._now(), "updated_at": wb._now(),
        "script": script,
    })
    state["project"]["pipeline_type"] = spec["pipeline_type"]
    state["project"]["title"] = spec["title"]
    intake = wb._normalize_intake(state["project"].get("intake"))
    intake.update({"brief": spec["brief"], "aspect": spec["aspect"], "aspect_label": "竖版 9:16",
                   "video_title": spec["title"], "script_status": "draft_approved"})
    state["project"]["intake"] = intake
    wb._save(project_dir, state)
    # generate_scene_plan_from_script 在已有分镜时会提前返回，不会再写 script.json；
    # 这里显式落盘，保证 「正式脚本」始终是磁盘上的唯一事实来源。
    wb._atomic_write(project_dir / "artifacts" / "script.json", script)
    print(f"[3] 正式脚本已通过：{len(script['sections'])} 段 "
          f"{script['total_duration_seconds']} 秒", flush=True)

    # ---- 4. 分镜 ----------------------------------------------------------
    wb.generate_scene_plan_from_script(project_dir)
    state = wb._load_for_write(project_dir)
    scenes = state.get("scenes") or []
    if len(scenes) != len(spec["sections"]):
        raise SystemExit(f"分镜数量不符：{len(scenes)} vs {len(spec['sections'])}")
    print(f"[4] 分镜草案：{len(scenes)} 段", flush=True)

    # ---- 5. 逐段绑定本地画面（full_bleed）---------------------------------
    for section, scene in zip(spec["sections"], scenes):
        duration = round(float(scene["end_seconds"]) - float(scene["start_seconds"]), 3)
        blocks = []
        cursor = 0.0
        for index, shot in enumerate(section["shots"], 1):
            span = round(float(shot["out"]) - float(shot["in"]), 3)
            end = round(duration, 3) if index == len(section["shots"]) else round(cursor + span, 3)
            asset = key_to_asset[shot["source"]]
            facts = master_info[shot["source"]]["facts"]
            if float(shot["out"]) > facts["duration"] + 0.02:
                raise SystemExit(f"{scene['id']} 第 {index} 镜超出素材时长")
            blocks.append({
                "id": f"VB-{index:03d}",
                "start_seconds": round(cursor, 3),
                "end_seconds": end,
                "source_mode": "web_download",
                "asset_id": asset["id"],
                "label": f"{shot['source']} {shot['intent']}"[:160],
                "source_in_seconds": round(float(shot["in"]), 3),
                "source_out_seconds": round(float(shot["in"]) + (end - cursor), 3),
                "locked": False,
            })
            cursor = end
        wb.update_scene_visual_timeline(project_dir, scene["id"], {"blocks": blocks})
        scene_now = next(s for s in wb.read_workbench(project_dir)["scenes"]
                         if s["id"] == scene["id"])
        composition = scene_now.get("visual_composition") or {}
        if (str(composition.get("layout_recipe")) != "full_bleed"
                or composition.get("overlays")):
            wb.update_scene_visual_composition(project_dir, scene["id"], {
                "version": 1,
                "layout_recipe": "full_bleed",
                "background": {"source": "visual_timeline", "treatment": "normal"},
                "overlays": [],
                "expected_revision": int(composition.get("revision") or 1),
            })
        print(f"    {scene['id']} {duration}s ← {len(blocks)} 个本地区间", flush=True)

    # ---- 6. 配音（数字人母版会按该音色生成）------------------------------
    # ★ 2026-09-16：与第 7 步 BGM 对称——spec 不写 `narration_gain_db` 时**必须兜底
    #   软件级默认 +8.0**，不能是 0.0。原实现 `float(spec.get(...) or 0.0)` 让**每次
    #   重建都把口播增益打回 0 dB**（实测 matext2-remake-1 12:53 重建后
    #   `narration_policy.playback_gain_db == 0.0`，而同期几期都是 +8.0）
    #   ⇒ 与「重建后 BGM 又变小」是同一类漏洞的姊妹，只是方向相反、更隐蔽：
    #     0 dB 不会报错、预览也能过，只有上手机听才会发现口播发闷。
    narration_gain = spec.get("narration_gain_db")
    # ★ 2026-09-16 修：此处原写 `np_[...]`，但 `np_` **从未定义** ⇒ 每次重建
    #   都在这一步 `NameError: name 'np_' is not defined`（第 4 步之后、第 6 步崩），
    #   而且是在场景已经改完、母版已经重转之后崩 ⇒ 留下"半重建"的项目。
    #   正确写法：先取 state、再取 narration_policy 的引用，改完一并保存。
    state = wb._load_for_write(project_dir)
    np_ = wb._ensure_narration_policy(state)
    np_["playback_gain_db"] = clamp_narration_gain_db(
        narration_gain if narration_gain is not None else DEFAULT_NARRATION_GAIN_DB
    )
    np_["updated_at"] = wb._now()
    wb._save(project_dir, state)

    # ---- 7. BGM -----------------------------------------------------------
    music = spec.get("music") or {}
    if music.get("source_path"):
        temp = ml.prepare_project_music_upload(project_dir, music["display_name"])
        shutil.copyfile(music["source_path"], temp)
        final, metadata = ml.complete_project_music_upload(
            project_dir, temp, music["display_name"])
        state = wb._load_for_write(project_dir)
        policy = wb._ensure_music_policy(state)
        policy.update({
            "enabled": True,
            "category": "project_upload",
            "track_id": metadata["id"],
            "playback_gain_db": float(music.get("playback_gain_db", DEFAULT_PLAYBACK_GAIN_DB)),
            "loop": True,
            "source_start_seconds": 0.0,
            "source_end_seconds": None,
            "fade_in_seconds": float(music.get("fade_in_seconds", 0.6)),
            "fade_out_seconds": float(music.get("fade_out_seconds", 1.5)),
            "updated_at": wb._now(),
        })
        wb._save(project_dir, state)
        print(f"[7] BGM 已入项目曲库：{metadata['id'][:34]}… 时长 {metadata.get('duration_seconds')}s "
              f"增益 {policy['playback_gain_db']}dB", flush=True)

    # ---- 7b. 重建必然改变音频混音签名 → 旧样板置 stale ---------------------
    # ★ 为什么必须在这里清（2026-09-15 实测踩到）：
    #   服务端在 `full_preview` 入口用 `sample.policy_signature == _audio_mix_signature(state)`
    #   校验（workbench.py:3204），而签名里含**人声增益 + BGM 设置 + 输出响度目标**。
    #   rebuild 改写了 narration_gain（第 6 步）与 music gain（第 7 步），签名一定变；
    #   但 rebuild 是**直接写 state.json、没走 API**，所以服务端不会自动置 stale。
    #   后果：`pp8.step_music` 看到 `sample.status == "approved"` 就跳过 → 紧接着
    #   preview 撞 `HTTP 422 声音设置已修改：请先生成并确认第一段音量样板，再生成全片`。
    state = wb._load_for_write(project_dir)
    policy = wb._ensure_music_policy(state)
    if (policy.get("sample") or {}).get("status") == "approved":
        wb._stale_music_sample(policy, "项目重建：音频混音设置已变更，第一段样板需重新生成并确认")
        wb._save(project_dir, state)
        print("[7b] 音频混音签名已变更 → 旧声音样板置 stale（下游会重新生成样板）", flush=True)

    # ---- 8. 摘要 ----------------------------------------------------------
    state = wb.read_workbench(project_dir)
    summary = {
        "project_dir": str(project_dir),
        "scenes": len(state.get("scenes") or []),
        "assets": len(state.get("assets") or []),
        "duration": state["project"].get("duration_seconds"),
        "music_track": (state.get("music_policy") or {}).get("track_id"),
        "all_visuals_complete": all(
            wb._scene_has_complete_visual(state, s) for s in (state.get("scenes") or [])
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("BUILD OK")


def _cli() -> int:
    parser = argparse.ArgumentParser(description="把复刻规格书建成可渲染的本机项目")
    parser.add_argument("--spec", required=True, help="remake-spec.json 路径")
    args = parser.parse_args()
    main(Path(args.spec).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
