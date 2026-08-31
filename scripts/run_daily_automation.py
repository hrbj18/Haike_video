"""Stable absolute-path wrapper for Windows Task Scheduler."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import os
import re
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backlot.daily_cli import main


TRANSIENT_RETRY_DELAYS_SECONDS = (30, 90, 180)


def _is_scheduled_run() -> bool:
    return "run" in sys.argv[1:] and "--trigger" in sys.argv[1:] and "schedule" in sys.argv[1:]


def _is_transient_failure(error: object) -> bool:
    return bool(re.search(
        r"SSL|EOF|连接(?:中断|重置|失败)|timeout|timed out|HTTP\s*(?:429|50[234])|\b(?:429|502|503|504)\b",
        str(error),
        re.IGNORECASE,
    ))


def _run_with_transient_recovery(runner=main, sleeper=time.sleep) -> int:
    """Retry the exact scheduled target after bounded transport failures."""
    attempt = 0
    while True:
        try:
            return int(runner())
        except Exception as exc:  # noqa: BLE001 - traceback is persisted by caller.
            traceback.print_exc()
            if (
                not _is_scheduled_run()
                or not _is_transient_failure(exc)
                or attempt >= len(TRANSIENT_RETRY_DELAYS_SECONDS)
            ):
                return 1
            delay = TRANSIENT_RETRY_DELAYS_SECONDS[attempt]
            attempt += 1
            print(
                f"检测到临时连接故障，{delay}秒后恢复同一目标日期；"
                f"只从安全阶段继续（第{attempt}次）。",
                flush=True,
            )
            sleeper(delay)


def _run_with_scheduler_log() -> int:
    log_path = REPO_ROOT / ".backlot" / "daily-runs" / "scheduler.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.is_file() and log_path.stat().st_size > 2_000_000:
        tail = log_path.read_bytes()[-1_000_000:]
        log_path.write_bytes(tail)
    with log_path.open("a", encoding="utf-8", newline="\n") as stream:
        with redirect_stdout(stream), redirect_stderr(stream):
            print(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] Windows 计划任务启动")
            code = _run_with_transient_recovery()
            print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] Windows 计划任务结束，退出码 {code}")
            stream.flush()
            return code


if __name__ == "__main__":
    raise SystemExit(_run_with_scheduler_log())
