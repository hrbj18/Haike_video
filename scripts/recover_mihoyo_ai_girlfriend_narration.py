"""Recover the interrupted narration job for one bounded production project.

The first five takes are already selected in the workbench.  Scene 6 reached
the provider and failed only while downloading its completed result, so this
script recovers that exact result URL instead of submitting the text again.
Scene 7 is generated once with the voice profile frozen in project state.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backlot.audio_center import get_voice_profile
from backlot.tts_runtime import _convert_to_wav, generate_voice_audio
from backlot.workbench import (
    AUTOMATION_ASSET_MANIFEST,
    _activity,
    _append_asset,
    _atomic_write,
    _automation,
    _automation_asset_manifest,
    _commit_narration_timeline,
    _concat_audio,
    _decision,
    _ffmpeg_available,
    _load_for_write,
    _narration_version_id,
    _now,
    _probe_duration_seconds,
    _promote_scene_narration_version,
    _refresh_visual_timing_status,
    _safe_relpath,
    _save,
    _scene_narration_default,
    _script_sections,
    _write_subtitles,
)


PROJECT = ROOT / "projects" / "mihoyo-ai-girlfriend-remake-1"
SIGNED_URL = re.compile(r"https://[^\s]+")
FAILED_HOST = re.compile(r"host='([^']+)'")
FAILED_PATH = re.compile(r"with url:\s*(/[^\s]+)")


def _recover_completed_download(error: str, scene_id: str) -> tuple[Path, Path]:
    """Download a provider-completed result without logging its signed URL."""
    match = SIGNED_URL.search(error or "")
    if match:
        url = match.group(0).rstrip(").,，。")
    else:
        host = FAILED_HOST.search(error or "")
        path = FAILED_PATH.search(error or "")
        url = f"https://{host.group(1)}{path.group(1)}" if host and path else ""
    if not url:
        raise RuntimeError(f"{scene_id} 没有可恢复的已完成结果地址；禁止自动重新提交")
    output_dir = PROJECT / "assets" / "audio" / "voicebox"
    output_dir.mkdir(parents=True, exist_ok=True)
    encoded = output_dir / f".{scene_id}.doubao.mp3"
    target = output_dir / f"{scene_id}.wav"
    completed = subprocess.run(
        [
            "curl.exe",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--retry",
            "5",
            "--retry-all-errors",
            "--connect-timeout",
            "20",
            "--max-time",
            "180",
            "--output",
            str(encoded),
            url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=False,
    )
    if completed.returncode != 0 or not encoded.is_file() or encoded.stat().st_size <= 0:
        raise RuntimeError(f"{scene_id} 已完成音频下载恢复失败：{completed.stderr[-400:]}")
    conversion_error = _convert_to_wav(encoded, target)
    if conversion_error:
        raise RuntimeError(conversion_error)
    encoded.unlink(missing_ok=True)
    metadata = target.with_suffix(target.suffix + ".doubao.json")
    metadata.write_text(
        json.dumps(
            {
                "provider": "doubao",
                "recovery": "completed_provider_result_download",
                "signed_url_persisted": False,
                "recovered_at": _now(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target, metadata


def _attach_take(state: dict, scene: dict, source_audio: Path, metadata_path: Path | None) -> None:
    narration = scene.get("narration") if isinstance(scene.get("narration"), dict) else _scene_narration_default()
    scene["narration"] = narration
    current = next(
        (item for item in narration.get("versions", []) if item.get("id") == narration.get("current_version_id")),
        None,
    )
    relative = _safe_relpath(PROJECT, str(source_audio))
    if current and current.get("audio_path") == relative:
        return

    ffmpeg = _ffmpeg_available()
    duration = _probe_duration_seconds(source_audio, ffmpeg, 0)
    if duration <= 0:
        raise RuntimeError(f"无法读取 {scene['id']} 的恢复音频时长")
    automation = _automation(state)
    voice = automation["voice"]
    existing_asset = next((asset for asset in state.get("assets", []) if asset.get("path") == relative), None)
    audio_asset = existing_asset or _append_asset(
        PROJECT,
        state,
        {
            "name": f"{scene.get('title') or scene['id']} · {voice['label']}旁白",
            "type": "audio",
            "source_type": "local_generated",
            "path": str(source_audio),
            "duration_seconds": duration,
            "provider": voice.get("provider_name") or "豆包云端配音",
            "source_tool": voice.get("provider") or "doubao",
            "license": "由所选配音供应商生成；请按项目发布规范复核",
            "generation": {
                "provider_id": voice.get("provider") or "doubao",
                "profile_id": voice.get("profile_id"),
                "profile_name": voice.get("profile_name"),
                "voice_label": voice.get("label"),
                "scene_id": scene["id"],
                "generated_at": _now(),
                "timing_mode": "natural",
                "metadata_path": str(metadata_path) if metadata_path else None,
                "recovered": scene["id"] == "sec_06",
            },
        },
    )
    version_id = _narration_version_id(scene)
    narration.setdefault("versions", []).append(
        {
            "id": version_id,
            "status": "candidate",
            "text": narration.get("text") or scene.get("description") or "",
            "asset_id": audio_asset["id"],
            "audio_path": audio_asset["path"],
            "profile_id": voice.get("profile_id"),
            "profile_name": voice.get("profile_name"),
            "duration_seconds": duration,
            "raw_duration_seconds": duration,
            "timing_mode": "natural",
            "created_at": _now(),
            "source": "project_narration_recovery" if scene["id"] == "sec_06" else "project_narration",
        }
    )
    _promote_scene_narration_version(state, scene, version_id)
    _activity(
        state,
        "voice_generation_recovered" if scene["id"] == "sec_06" else "voice_generation",
        f"{scene['id']} 的 {voice['label']} 自然旁白已就绪（{duration:.2f} 秒）",
        scene_id=scene["id"],
        asset_id=audio_asset["id"],
        duration_seconds=duration,
    )


def main() -> None:
    state = _load_for_write(PROJECT)
    automation = _automation(state)
    job = automation["narration_generation"]
    if job.get("status") not in {"failed", "generating"}:
        raise RuntimeError("项目旁白不处于可恢复状态")
    scenes = {str(scene.get("id")): scene for scene in state.get("scenes", [])}
    profile_id = str(automation["voice"].get("profile_id") or "")
    profile = get_voice_profile(profile_id)
    if not profile:
        raise RuntimeError("项目冻结的默认音色已经不可用")

    sec6 = PROJECT / "assets" / "audio" / "voicebox" / "sec_06.wav"
    sec6_meta = sec6.with_suffix(sec6.suffix + ".doubao.json")
    if not sec6.is_file():
        sec6, sec6_meta = _recover_completed_download(str(job.get("error") or ""), "sec_06")
    _attach_take(state, scenes["sec_06"], sec6, sec6_meta if sec6_meta.is_file() else None)
    job["completed_scenes"] = 6
    job["status"] = "generating"
    job["error"] = ""
    _save(PROJECT, state)

    state = _load_for_write(PROJECT)
    automation = _automation(state)
    job = automation["narration_generation"]
    scenes = {str(scene.get("id")): scene for scene in state.get("scenes", [])}
    sec7 = PROJECT / "assets" / "audio" / "voicebox" / "sec_07.wav"
    sec7_meta = sec7.with_suffix(sec7.suffix + ".doubao.json")
    if not sec7.is_file():
        result = generate_voice_audio(
            text=str(scenes["sec_07"].get("narration", {}).get("text") or scenes["sec_07"].get("description") or ""),
            profile=profile,
            output_path=sec7,
            language="zh",
        )
        if not result.success or not sec7.is_file():
            error = str(result.error or "")
            if "https://" not in error:
                raise RuntimeError(f"sec_07 配音未生成，且没有安全恢复点：{error[:400]}")
            sec7, sec7_meta = _recover_completed_download(error, "sec_07")
    _attach_take(state, scenes["sec_07"], sec7, sec7_meta if sec7_meta.is_file() else None)
    job["completed_scenes"] = 7
    _save(PROJECT, state)

    state = _load_for_write(PROJECT)
    automation = _automation(state)
    job = automation["narration_generation"]
    sections = _script_sections(PROJECT, state)
    timeline_update = _commit_narration_timeline(state, reason="project_narration_recovered")
    visual_timing = _refresh_visual_timing_status(PROJECT, state)
    scenes = list(state.get("scenes", []))
    parts = [PROJECT / scene["narration"]["versions"][-1]["audio_path"] for scene in scenes]
    narration = _concat_audio(PROJECT, parts)
    subtitle_path = _write_subtitles(PROJECT, scenes, sections)
    _atomic_write(PROJECT / AUTOMATION_ASSET_MANIFEST, _automation_asset_manifest(PROJECT, state))
    job.update(
        {
            "status": "completed",
            "stage": "ready_to_render",
            "finished_at": _now(),
            "completed_scenes": len(scenes),
            "total_scenes": len(scenes),
            "audio_path": _safe_relpath(PROJECT, str(narration)),
            "subtitle_path": _safe_relpath(PROJECT, str(subtitle_path)),
            "timeline_update": timeline_update,
            "error": "",
        }
    )
    automation["render"] = {"status": "awaiting_assets", "runtime": "ffmpeg", "output_path": None, "error": ""}
    automation["status"] = "narration_ready"
    _decision(
        state,
        "timeline_authority",
        "旁白类项目时间轴",
        "自然配音主时间轴",
        "已复用前五段成功音频、恢复第六段已完成结果，并按全部实测时长重排画面和字幕。",
    )
    _activity(
        state,
        "narration_generation_finished",
        f"项目旁白与字幕已从安全点完成；正式时长为 {timeline_update['new_total_duration_seconds']:.2f} 秒",
        visual_timing=visual_timing,
    )
    _save(PROJECT, state)
    print(json.dumps({"status": "completed", "duration_seconds": timeline_update["new_total_duration_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
