"""零付费并发基准台：证明粗剪提速收益，永不发网络请求。

用法（可复现命令，写进结论）：

    ./.venv/Scripts/python.exe scripts/benchmark_interaction_concurrency.py \
        --windows 34 --chunks 9 --sleep-ms 120 --concurrency 1,2,3,4 --json

本脚本注入**假分析器 / 假 ASR / 假抽帧**，只在本地 ``time.sleep`` 上量墙钟与在飞峰值，
因此可以在任意窗口数 / 分片数（含 S-001 的 34 窗口 / 9 分片）规模上复现调度开销，
既不联网也不产生任何付费调用（N1）。

**口径声明（务必随数字一起引用）**：本表量的是**并发内核的合成上限**（纯 ``sleep`` 假件），
**不是端到端收益**；默认路线甲（``serial_equivalent``）下付费调用仍串行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backlot.interaction_concurrency import (  # noqa: E402
    ConcurrencyStats,
    run_bounded,
)

# 必须与 3.77× / 2.99× 这类数字一起引用，禁止被读成「端到端提速」。
BENCHMARK_DISCLAIMER = (
    "本表是并发内核的合成上限，不是端到端收益；默认路线甲下付费调用仍串行。"
    "（Synthetic in-flight cap only, NOT end-to-end speedup: the default "
    "serial-equivalent route still issues paid calls one at a time.）"
)


def fake_analyze(kind: str, payload: dict, images: list) -> dict:
    """假视觉分析器：不发任何网络请求，返回合法骨架。"""
    return {
        "kind": kind,
        "window_id": str(payload.get("window_id") or ""),
        "events": [],
        "images": len(images),
    }


def fake_asr_submit(data: bytes) -> dict:
    """假 ASR 提交：产出假 task_id，不联网。"""
    return {"task_id": "fake-task", "bytes": len(data)}


def fake_extract(source: Any, ffmpeg: str, t: float, target: Any) -> tuple[float, str]:
    """假抽帧：不调用 ffmpeg。"""
    return (float(t), "fake-sha")


@dataclass
class BenchmarkHarness:
    """在注入的假件上量串行与各并发档的墙钟 / 在飞峰值 / 提交次数。"""

    windows: int = 34
    chunks: int = 9
    sleep_ms: float = 120.0
    concurrency: list[int] = field(default_factory=lambda: [1, 2, 3, 4])

    def _delay(self) -> None:
        time.sleep(max(0.0, float(self.sleep_ms)) / 1000.0)

    def _window_worker(self, window_id: int, _index: int) -> dict:
        # 一个窗口 = 本地抽帧 + 假分析，二者都只是等待。
        fake_extract(f"source-{window_id}", "fake-ffmpeg", float(window_id), None)
        self._delay()
        return fake_analyze("events", {"window_id": f"W{window_id:03d}"}, [1, 2, 3])

    def _chunk_worker(self, chunk_id: int, _index: int) -> dict:
        self._delay()
        return fake_asr_submit(b"\0" * 1024)

    def _measure_one(self, worker: Callable[[Any, int], Any], units: int, limit: int) -> dict:
        stats = ConcurrencyStats()
        started = time.perf_counter()
        outcomes = run_bounded(list(range(units)), worker, limit=limit, stats=stats)
        wall = time.perf_counter() - started
        failures = sum(1 for row in outcomes if not row.ok)
        return {
            "concurrency": limit,
            "wall_seconds": round(wall, 4),
            "in_flight_peak": stats.in_flight_peak,
            "submissions": stats.dispatched,
            "completed": stats.completed,
            "failures": failures,
        }

    def measure(self) -> dict:
        result: dict[str, Any] = {
            "windows": self.windows,
            "chunks": self.chunks,
            "sleep_ms": self.sleep_ms,
            "concurrency": list(self.concurrency),
            "window_benchmark": [],
            "chunk_benchmark": [],
        }
        for stage, worker, units in (
            ("window_benchmark", self._window_worker, self.windows),
            ("chunk_benchmark", self._chunk_worker, self.chunks),
        ):
            if units <= 0:
                continue
            rows = [self._measure_one(worker, units, limit) for limit in self.concurrency]
            baseline = next((row["wall_seconds"] for row in rows if row["concurrency"] == 1), None)
            for row in rows:
                row["speedup"] = (
                    round(baseline / row["wall_seconds"], 3) if baseline and row["wall_seconds"] else None
                )
            result[stage] = rows
        return result


def _summarise(harness: BenchmarkHarness) -> dict:
    report = harness.measure()
    lines = []
    for stage in ("window_benchmark", "chunk_benchmark"):
        rows = report[stage]
        if not rows:
            continue
        label = "视觉窗口" if stage == "window_benchmark" else "ASR 分片"
        lines.append(f"== {label} ==")
        for row in rows:
            lines.append(
                f"  并发 {row['concurrency']} 路：墙钟 {row['wall_seconds']:.3f}s · "
                f"在飞峰值 {row['in_flight_peak']} · 提交 {row['submissions']} · "
                f"加速比 {row['speedup'] if row['speedup'] is not None else '-'}"
            )
    report["summary_lines"] = lines
    report["disclaimer"] = BENCHMARK_DISCLAIMER
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="零付费并发基准台（不发网络请求）")
    parser.add_argument("--windows", type=int, default=34, help="视觉窗口数（默认 34）")
    parser.add_argument("--chunks", type=int, default=9, help="ASR 分片数（默认 9）")
    parser.add_argument("--sleep-ms", type=float, default=120.0, help="单个单元的假等待毫秒（默认 120）")
    parser.add_argument("--concurrency", type=str, default="1,2,3,4", help="逗号分隔的并发档位（默认 1,2,3,4）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    concurrency = [int(value) for value in str(args.concurrency).split(",") if str(value).strip()]
    harness = BenchmarkHarness(
        windows=max(0, int(args.windows)), chunks=max(0, int(args.chunks)),
        sleep_ms=float(args.sleep_ms), concurrency=concurrency,
    )
    report = _summarise(harness)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"基准台（零付费）· 口径声明：{BENCHMARK_DISCLAIMER}")
        print(f"基准台（零付费）：窗口 {harness.windows} / 分片 {harness.chunks} / 单步等待 {harness.sleep_ms:.0f}ms")
        for line in report["summary_lines"]:
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
