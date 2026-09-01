"""Command-line entry point used by Windows Task Scheduler."""

from __future__ import annotations

import argparse
import json
from datetime import date

from backlot.daily_automation import (
    DailyAutomationError,
    previous_target_date,
    read_status,
    release_run_lock,
    run_research_and_script,
    try_acquire_run_lock,
)
from backlot.daily_pipeline import run_daily_pipeline
from backlot.news_selection_v2 import run_news_selection_v2
from backlot.daily_script_v2 import run_script_v2_test


def main() -> int:
    parser = argparse.ArgumentParser(description="Haike Video 每日科技快报自动化")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    run = sub.add_parser("run")
    run.add_argument("--target-date")
    run.add_argument("--previous-day", action="store_true")
    run.add_argument("--trigger", default="cli")
    run.add_argument("--free-only", action="store_true", help="只执行检索、脚本和项目初始化")
    select_v2 = sub.add_parser("select-v2", help="只执行新闻素材选择V2，不生成脚本")
    select_v2.add_argument("--target-date")
    select_v2.add_argument("--previous-day", action="store_true")
    select_v2.add_argument("--trigger", default="cli")
    script_v2 = sub.add_parser("script-v2", help="基于真实V2选题生成独立完整测试脚本")
    script_v2.add_argument("--target-date", required=True)
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(read_status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "script-v2":
        print(json.dumps(run_script_v2_test(date.fromisoformat(args.target_date)), ensure_ascii=False, indent=2))
        return 0
    target = previous_target_date() if args.previous_day or not args.target_date else date.fromisoformat(args.target_date)
    if not try_acquire_run_lock(target, trigger=args.trigger):
        raise DailyAutomationError("每日科技快报已有生产进程在运行；本次重复触发已安全退出")
    try:
        if args.command == "select-v2":
            result = run_news_selection_v2(target, trigger=args.trigger)
        else:
            result = (
                run_research_and_script(target, trigger=args.trigger)
                if args.free_only
                else run_daily_pipeline(target, trigger=args.trigger)
            )
    finally:
        release_run_lock()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
