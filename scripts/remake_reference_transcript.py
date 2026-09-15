"""外站素材复刻 · 参考视频付费转写（腾讯云录音文件识别）

用途：复刻的第一步是「拿别人的脚本」。这类视频没有字幕文件，只能用付费 ASR
把口播转成带句级时间戳的文本，再据此重写自己的文案。

★ 计费安全契约（这是本脚本存在的主要理由）：
  腾讯云录音文件识别的提交接口和结果接口是分开的。如果提交请求发出后进程被杀，
  我们无法判断云端到底受没受理——重提就会重复计费。所以每个任务写一个
  ``<tag>.task.json`` 三态状态机：
    submitting → accepted → completed
  其中 ``submitting`` / ``ambiguous`` 是「受理不明」，**禁止自动重提**，
  必须人工确认后才能清掉状态文件重跑。已经完成的转写永远复用。

用法：
  python scripts/remake_reference_transcript.py \
      --video-dir <素材目录> --out <输出目录> --id <aweme_id> [--id ...] [--tag-prefix ref]

  # 也可以直接把要转写的视频文件路径列出来
  python scripts/remake_reference_transcript.py --file <path.mp4> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backlot.tencent_asr import (  # noqa: E402
    TencentASRAmbiguous,
    _to_cloud_audio,
    assert_tencent_asr_ready,
    transcribe_file,
)
from lib.ffmpeg_locator import resolve_ffmpeg_with_option  # noqa: E402


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def ffmpeg_bin() -> str:
    pair = resolve_ffmpeg_with_option("-filter_complex_script")
    if not pair:
        raise SystemExit("未找到可用的 ffmpeg")
    return pair[0]


class Runner:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        with (self.out / "run.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def transcribe(self, tag: str, source: Path) -> dict:
        result_path = self.out / f"{tag}.json"
        task_path = self.out / f"{tag}.task.json"
        if result_path.is_file():
            self.log(f"{tag}: 复用既有转写 {result_path.name}")
            return {"tag": tag, "status": "reused", "result": str(result_path)}
        if task_path.is_file():
            prior = json.loads(task_path.read_text(encoding="utf-8"))
            if prior.get("status") in {"submitting", "ambiguous"}:
                raise SystemExit(
                    f"{tag}: 既有提交受理状态不明（{prior.get('reference')}）；"
                    "按契约禁止自动重提，请先人工确认云端是否已受理"
                )
        state = {"version": 1, "tag": tag, "file": source.name, "status": "submitting",
                 "started_at": now(), "source": str(source)}
        write_json(task_path, state)
        self.log(f"{tag}: 提取 16k 单声道音轨…")
        audio = _to_cloud_audio(source, self.out / "audio", ffmpeg_bin())
        self.log(f"{tag}: 音轨 {audio.name} "
                 f"{round(audio.stat().st_size / 1048576, 2)} MB，提交腾讯云录音文件识别")

        def on_accepted(reference: str) -> None:
            state.update({"status": "accepted", "reference": reference, "accepted_at": now()})
            write_json(task_path, state)

        try:
            text, segments, metadata = transcribe_file(
                audio, timeout_seconds=900, ffmpeg=ffmpeg_bin(),
                on_progress=lambda m: self.log(f"{tag}: {m}"),
                on_accepted=on_accepted,
            )
        except TencentASRAmbiguous as exc:
            state.update({"status": "ambiguous", "error": str(exc), "finished_at": now()})
            write_json(task_path, state)
            self.log(f"{tag}: 受理不明，已冻结等待人工核对：{exc}")
            return {"tag": tag, "status": "ambiguous", "error": str(exc)}
        write_json(result_path, {
            "version": 1,
            "tag": tag,
            "file": source.name,
            "source": str(source),
            "audio": str(audio),
            "provider": "腾讯云录音文件识别（16k_zh）",
            "transcribed_at": now(),
            "segment_count": len(segments),
            "chars": len(text),
            "segments": segments,
            "metadata": metadata,
            "text": text,
        })
        state.update({"status": "completed", "finished_at": now(),
                      "segment_count": len(segments), "chars": len(text),
                      "result": result_path.name})
        write_json(task_path, state)
        self.log(f"{tag}: 完成，{len(segments)} 句 / {len(text)} 字")
        return {"tag": tag, "status": "completed", "segments": len(segments),
                "chars": len(text), "result": str(result_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="参考视频付费转写（腾讯云 ASR）")
    parser.add_argument("--out", required=True, help="输出目录（含三态状态文件）")
    parser.add_argument("--video-dir", help="素材目录；配合 --id 按文件名匹配")
    parser.add_argument("--id", action="append", default=[], help="aweme id，可重复")
    parser.add_argument("--file", action="append", default=[], help="直接指定视频文件，可重复")
    args = parser.parse_args()

    targets: list[tuple[str, Path]] = []
    if args.video_dir:
        root = Path(args.video_dir)
        for id_ in args.id:
            match = next((p for p in root.iterdir() if id_ in p.name), None)
            if match is None:
                raise SystemExit(f"未在 {root} 找到 id={id_} 的视频")
            targets.append((id_, match))
    for raw in args.file:
        path = Path(raw)
        targets.append((path.stem.split("_")[-1] or path.stem, path))
    if not targets:
        raise SystemExit("请至少给一个 --id 或 --file")

    assert_tencent_asr_ready()
    runner = Runner(Path(args.out))
    results = [runner.transcribe(tag, path) for tag, path in targets]
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
