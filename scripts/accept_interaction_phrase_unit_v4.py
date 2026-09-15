"""End-to-end acceptance for the phrase-unit second pass and the weighted ranking.

This is the harness the V4 guide's C-class criteria are written against.  It is
resumable and idempotent at every step, because the expensive parts (cloud ASR and
cloud vision on a 90-minute local video) must never be repeated just because a
later step failed:

1. create the project (title ``长视频测试2``) if it does not exist;
2. stream the source video into project storage — never buffered in RAM;
3. run the interaction analysis once, through the documented job endpoint;
4. read the ranking with the shipped default order (``duration_desc`` = 优先时间长);
5. for the top ``--top`` ranked events: generate the first-pass complete cut
   (local, free) and then one second-pass edit (at most one text-model call each);
6. write a machine-readable report next to the run and print a summary.

Usage::

    ./.venv/Scripts/python.exe scripts/accept_interaction_phrase_unit_v4.py --stage all
    ./.venv/Scripts/python.exe scripts/accept_interaction_phrase_unit_v4.py --stage cut

Nothing is published: every produced plan stops at ``pending_review``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:4754"
DEFAULT_PROJECT = "long-video-test-2"
DEFAULT_TITLE = "长视频测试2"
DEFAULT_SOURCE = r"C:\Users\Administrator\Downloads\直播回放-09月08日\直播回放-09月08日.mp4"
DEFAULT_REPORT = ".backlot/accept_phrase_unit_v4.json"
MAX_CANDIDATE_SECONDS = 180.0  # the first-pass candidate contract


def _opener() -> urllib.request.OpenerDirector:
    # Never let a system proxy intercept localhost traffic.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


OPENER = _opener()


def call(base: str, method: str, path: str, payload: dict | None = None, *, timeout: float = 300.0):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    if not body:
        return 200, {}
    try:
        return 200, json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 200, body[:400]


def stream_upload(base: str, project: str, source: Path, name: str, *, timeout: float = 3600.0) -> tuple[int, object]:
    """PUT the file with a chunked body so a multi-gigabyte source never enters RAM."""
    try:
        import requests
    except ImportError:  # pragma: no cover - the workbench venv ships requests
        raise SystemExit("需要 requests 才能流式上传长素材")

    def chunks(handle, size=8 * 1024 * 1024):
        while True:
            block = handle.read(size)
            if not block:
                break
            yield block

    query = urllib.parse.urlencode({"filename": source.name, "name": name, "license": "own"})
    url = f"{base}/api/project/{urllib.parse.quote(project)}/workbench/assets/uploads?{query}"
    with source.open("rb") as handle:
        response = requests.put(url, data=chunks(handle), timeout=timeout,
                                proxies={"http": None, "https": None})
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text[:400]


def poll_workbench(base: str, project: str, key: str, *, timeout: float, interval: float, label: str,
                   expected_job_id: str = "", verbose: bool = True) -> dict:
    """Wait for one named job, identified by its own id.

    Matching on the id matters: a previous event's job is still sitting in the
    same slot as ``completed``, so "wait for status == completed" would return
    instantly with the *old* result.
    """
    started = time.perf_counter()
    last = ""
    while True:
        status, state = call(base, "GET", f"/api/project/{project}/workbench", timeout=180)
        if status != 200 or not isinstance(state, dict):
            time.sleep(interval)
            continue
        job = ((state.get("automation") or {}).get(key) or {})
        if expected_job_id and str(job.get("job_id") or "") != expected_job_id:
            if time.perf_counter() - started > timeout:
                return {"status": "timeout", "error": f"未看到任务 {expected_job_id} 出现"}
            time.sleep(interval)
            continue
        line = f"  [{time.perf_counter() - started:7.0f}s] {label} status={job.get('status')} stage={job.get('stage')}"
        if verbose and line != last:
            print(line, flush=True)
            last = line
        if job.get("status") in {"completed", "failed", "ambiguous"}:
            return job
        if time.perf_counter() - started > timeout:
            job = dict(job)
            job["status"] = "timeout"
            job["error"] = job.get("error") or f"等待超过 {timeout:.0f} 秒"
            return job
        time.sleep(interval)


def read_interactions(base: str, project: str, asset: str, *, order_mode: str = "",
                      weights: str = "", timeout: float = 300.0) -> dict:
    query = {}
    if order_mode:
        query["order_mode"] = order_mode
    if weights:
        query["w"] = weights
    suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
    status, payload = call(base, "GET",
                          f"/api/project/{project}/workbench/assets/{asset}/media-index/interactions{suffix}",
                          timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(f"读取互动目录失败：{status} {payload}")
    return payload


def find_asset_id(base: str, project: str) -> str:
    status, state = call(base, "GET", f"/api/project/{project}/workbench", timeout=180)
    assets = (state or {}).get("assets") if isinstance(state, dict) else None
    if not assets:
        raise SystemExit("项目里没有素材，上传可能没有成功")
    return str(assets[-1].get("id") or "")


def ensure_project(base: str, project: str, title: str, source: Path) -> None:
    status, created = call(base, "POST", "/api/projects", {
        "project_id": project,
        "title": title,
        "pipeline_type": "animated-explainer",
        "style_playbook": "clean-professional",
        "brief": f"用 {source} 做长视频粗剪 + 二次精剪的端到端验收（短语级语音单元 + 权重重排序）。",
        "aspect": "portrait",
    })
    if status in (200, 201):
        # The route declares 201 but the running build answers 200; both mean the
        # project is now there, so trust the body rather than the code.
        print(f"1) 项目 {project}（{title}）已就绪")
        return
    if status == 409 or "已存在" in str(created) or "exists" in str(created).lower():
        print(f"1) 项目 {project} 已存在，继续使用")
        return
    raise SystemExit(f"新建项目失败：{status} {created}")


def ensure_asset(base: str, project: str, source: Path, asset: str, name: str, *, upload: bool) -> str:
    status, state = call(base, "GET", f"/api/project/{project}/workbench", timeout=180)
    existing = [row for row in ((state or {}).get("assets") or []) if row.get("media_index")]
    if not upload and not existing:
        raise SystemExit("项目里没有已导入素材，去掉 --skip-upload 再试")
    if not upload:
        print(f"2) 复用已导入素材 {existing[-1].get('id')}")
        return str(existing[-1].get("id"))
    print(f"2) 流式上传 {source.name}（{source.stat().st_size / 1024 / 1024:.0f} MB）")
    started = time.perf_counter()
    status, uploaded = stream_upload(base, project, source, name)
    if status != 200:
        raise SystemExit(f"上传失败：{status} {uploaded}")
    print(f"   上传完成，用时 {time.perf_counter() - started:.1f}s -> {str(uploaded)[:200]}")
    return find_asset_id(base, project)


def run_analysis(base: str, project: str, asset: str, *, timeout: float) -> dict:
    status, preflight = call(base, "GET",
                            f"/api/project/{project}/workbench/assets/{asset}/media-index/interaction-preflight",
                            timeout=600)
    if status != 200 or not isinstance(preflight, dict):
        raise SystemExit(f"互动预检失败：{status} {preflight}")
    budget = preflight.get("budget") or {}
    parallel = preflight.get("parallelism") or {}
    print(f"3) 预检：窗口 {budget.get('windows')} 个 · 视觉调用上限 {budget.get('model_calls_max')} · "
          f"并发 视觉={parallel.get('vision_windows')} ASR={parallel.get('asr_chunks')}")
    status, started = call(base, "POST", f"/api/project/{project}/workbench/assets/{asset}/media-index/jobs", {
        "stage": "interaction",
        "profile": "efficient",
        "recognize_audio": True,
        # Candidates are generated explicitly for the ranking's top N below; the
        # auto path would follow the vision model's own order instead.
        "generate_candidates": False,
        "remote_vision_confirmed": True,
        "remote_asr_confirmed": True,
        "preflight_signature": preflight.get("signature"),
    }, timeout=600)
    if status != 200:
        raise SystemExit(f"提交互动分析失败：{status} {started}")
    job_id = str(((started or {}).get("automation") or {}).get("media_index", {}).get("job_id") or "") \
        if isinstance(started, dict) else ""
    print("   已排队（付费：云端 ASR + 视觉窗口），等待完成……")
    job = poll_workbench(base, project, "media_index", timeout=timeout, interval=20, label="互动分析",
                         expected_job_id=job_id)
    print(f"   终态：{job.get('status')} error={str(job.get('error') or '')[:200]}")
    if job.get("status") != "completed":
        raise SystemExit("互动分析没有完成，停止后续步骤")
    return job


def candidate_for_event(base: str, project: str, asset: str, event_id: str, revision: int, *,
                        timeout: float) -> dict:
    status, queued = call(base, "POST",
                         f"/api/project/{project}/workbench/assets/{asset}/media-index/interactions/candidates",
                         {"event_id": event_id, "expected_review_revision": revision}, timeout=300)
    if status != 200:
        return {"status": "failed", "error": f"{status} {str(queued)[:200]}"}
    job_id = str(((queued or {}).get("candidate_job") or {}).get("job_id") or "") \
        if isinstance(queued, dict) else ""
    return poll_workbench(base, project, "interaction_candidate", timeout=timeout, interval=5,
                          label=f"首次完整切片 {event_id}", expected_job_id=job_id)


def second_pass_for_parent(base: str, project: str, asset: str, parent: dict, *, timeout: float,
                           options: dict) -> dict:
    status, queued = call(base, "POST",
                         f"/api/project/{project}/workbench/assets/{asset}/media-index/interactions/second-pass",
                         {
                             "parent_plan_id": parent["plan_id"],
                             "expected_parent_revision": parent.get("revision"),
                             "confirmed": True,
                             "options": options,
                         }, timeout=300)
    if status != 200:
        return {"status": "failed", "error": f"{status} {str(queued)[:300]}"}
    job_id = str(((queued or {}).get("second_pass_job") or {}).get("job_id") or "") \
        if isinstance(queued, dict) else ""
    return poll_workbench(base, project, "interaction_second_pass", timeout=timeout, interval=8,
                          label=f"二次精剪 {parent['plan_id']}", expected_job_id=job_id)


def summarise(second: dict) -> dict:
    compression = second.get("compression") if isinstance(second.get("compression"), dict) else {}
    removed_by_pause = second.get("removed_by_pause_seconds")
    if removed_by_pause is None:
        removed_by_pause = compression.get("removed_seconds")
    return {
        "plan_id": second.get("plan_id"), "version": second.get("version"),
        "parent_plan_id": (second.get("parent") or {}).get("plan_id"),
        "status": second.get("status"),
        "source_duration": second.get("source_duration"),
        "body_source_duration": second.get("body_source_duration"),
        "removed_source_seconds": second.get("removed_source_seconds"),
        "removed_by_pause_seconds": removed_by_pause,
        "pause_trim_count": len(second.get("pause_trims") or []),
        "output_duration": second.get("output_duration"),
        "removed_ratio": (round(float(second.get("removed_source_seconds") or 0)
                                / max(1e-6, float(second.get("source_duration") or 1)), 4)),
        "target_duration_status": second.get("target_duration_status"),
        "target_window": [(second.get("options") or {}).get("target_min_seconds"),
                          (second.get("options") or {}).get("target_max_seconds")],
        "duration_policy": (second.get("options") or {}).get("duration_policy"),
        "pause_preset": (second.get("options") or {}).get("pause_preset"),
        "unit_source": second.get("unit_source"), "unit_count": second.get("unit_count"),
        "content_qa": (second.get("content_qa") or {}).get("status"),
        "qa": (second.get("qa") or {}).get("status"),
        "preview": (second.get("preview") or {}).get("path"),
        "subtitle_cues": len(second.get("subtitle_cues") or []),
        "usage": second.get("usage"),
        "degradations": second.get("degradations"),
        "warnings": second.get("warnings"),
    }


def first_pass_summary(candidate: dict) -> dict:
    return {
        "plan_id": candidate.get("plan_id"), "event_id": candidate.get("event_id"),
        "source_duration": candidate.get("source_duration"),
        "output_duration": candidate.get("output_duration"),
        "qa": (candidate.get("qa") or {}).get("status"),
        "preview": (candidate.get("preview") or {}).get("path"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--asset", default="", help="留空则自动取项目里最后一个素材")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--order-mode", default="duration_desc")
    parser.add_argument("--weights", default="")
    parser.add_argument("--stage", default="all",
                        choices=["all", "create", "analyse", "cut", "report"])
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--analysis-timeout", type=float, default=6 * 3600)
    parser.add_argument("--cut-timeout", type=float, default=1800)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--pause-preset", default="tight")
    parser.add_argument("--speed", type=float, default=1.1)
    args = parser.parse_args()

    source = Path(args.source)
    report_path = Path(args.report)
    report: dict = {"project": args.project, "source": str(source), "base": args.base,
                    "order_mode": args.order_mode, "top": args.top}

    status, health = call(args.base, "GET", "/api/projects", timeout=30)
    if status != 200:
        raise SystemExit(f"工作台 {args.base} 不可用（{status}）；先启动 启动工作台.bat")

    if args.stage in {"all", "create"}:
        ensure_project(args.base, args.project, args.title, source)
        if not source.is_file():
            raise SystemExit(f"素材不存在：{source}")
        asset = ensure_asset(args.base, args.project, source, args.asset, args.title,
                             upload=not args.skip_upload)
    else:
        asset = args.asset or find_asset_id(args.base, args.project)
    args.asset = asset
    report["asset"] = asset
    print(f"   素材编号 {asset}")

    job = {}
    if args.stage in {"all", "analyse"}:
        job = run_analysis(args.base, args.project, asset, timeout=args.analysis_timeout)
        report["analysis_job"] = {key: job.get(key) for key in ("status", "stage", "error", "finished_at")}

    if args.stage in {"all", "analyse", "cut", "report"}:
        data = read_interactions(args.base, args.project, asset,
                                 order_mode=args.order_mode, weights=args.weights)
        recommendations = data.get("recommendations") or {}
        events = recommendations.get("events") or []
        review = data.get("review") or {}
        revision = int(review.get("revision") or 0)
        report["ranking"] = {
            "version": recommendations.get("version"),
            "order_mode": recommendations.get("order_mode"),
            "order_mode_label": recommendations.get("order_mode_label"),
            "weights": recommendations.get("weights"),
            "requirement_threshold": recommendations.get("requirement_threshold"),
            "event_count": len(events),
            "review_event_count": len(review.get("events") or []),
            "review_revision": revision,
            "notes": recommendations.get("notes"),
            "top": [{key: row.get(key) for key in
                     ("rank", "event_id", "start", "end", "duration_seconds", "recommendation_score",
                      "factors", "requirement")} for row in events[:args.top]],
        }
        print(f"4) 排序 {recommendations.get('order_mode')}（{recommendations.get('order_mode_label')}）"
              f" · 事件 {len(events)} 条 · 权重 {recommendations.get('weights')}")
        print("   排名前 %d：" % args.top)
        for row in events[:args.top]:
            print(f"     #{row['rank']} {row['event_id']} {row['start']:.1f}—{row['end']:.1f}s "
                  f"({row['duration_seconds']:.1f}s) 综合 {round(row['recommendation_score'] * 100)}"
                  f" 选材要求 {'✓' if (row.get('requirement') or {}).get('ok') else '✗'}")

        if not events:
            print("   没有任何互动事件，无法继续")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return 2

        cuts = []
        if args.stage in {"all", "cut"}:
            options = {"duration_policy": "proportional", "pause_preset": args.pause_preset,
                       "speed": args.speed, "burn_subtitles": True}
            report["second_pass_options"] = options
            print("5) 按排序逐条生成首次完整切片 + 二次精剪（目标 %d 条）" % args.top)
            produced = 0
            review_events = review.get("events") or []
            for row in events:
                if produced >= args.top:
                    break
                # The ranking is keyed by the *index* event id (``W007-E01``) while
                # the candidate API takes the human review id (``R0007``); the two
                # spaces are bridged through ``source_event_ids`` rather than by
                # assuming the numbering matches (it does not, in general).
                review_event = next(
                    (item for item in review_events
                     if str(row["event_id"]) in [str(value) for value in item.get("source_event_ids") or []]
                     or str(item.get("review_event_id")) == str(row["event_id"])),
                    None,
                )
                entry = {"rank": row["rank"], "event_id": row["event_id"],
                         "duration_seconds": row["duration_seconds"],
                         "requirement_ok": (row.get("requirement") or {}).get("ok")}
                if not review_event:
                    entry["skipped"] = "互动目录里没有对应的人工审核条目"
                    print(f"   - #{row['rank']} {row['event_id']}：{entry['skipped']}")
                    cuts.append(entry)
                    continue
                entry["review_event_id"] = review_event.get("review_event_id")
                entry["review_window"] = [review_event.get("start"), review_event.get("end")]
                review_seconds = float(review_event.get("end") or 0) - float(review_event.get("start") or 0)
                if review_seconds > MAX_CANDIDATE_SECONDS:
                    # The first-pass candidate contract refuses an interaction longer
                    # than 180s; the ranking is not allowed to hide that behind a
                    # different clip, so the skip is recorded with its own reason and
                    # the next ranked material takes the slot.
                    entry["skipped"] = (f"互动范围 {review_seconds:.1f}s 超过首次完整切片 "
                                        f"{MAX_CANDIDATE_SECONDS:.0f}s 合同，已跳过并记录")
                    print(f"   - #{row['rank']} {row['event_id']}（{entry['review_event_id']}）：{entry['skipped']}")
                    cuts.append(entry)
                    continue
                produced += 1
                print(f"   - #{row['rank']} {row['event_id']}（{entry['review_event_id']}）首次完整切片……")
                job_state = candidate_for_event(args.base, args.project, asset,
                                               str(review_event["review_event_id"]), revision,
                                               timeout=args.cut_timeout)
                entry["candidate_job"] = {key: job_state.get(key) for key in ("status", "error")}
                if job_state.get("status") != "completed":
                    print(f"     首次切片失败：{entry['candidate_job']}")
                    produced -= 1
                    cuts.append(entry)
                    continue
                refreshed = read_interactions(args.base, args.project, asset, order_mode=args.order_mode)
                parent = next((item for item in (refreshed.get("candidates") or [])
                               if str(item.get("event_id")) == str(review_event["review_event_id"])
                               and item.get("is_active") is True), None)
                if not parent:
                    entry["error"] = "首次切片已生成但读不到当前候选"
                    cuts.append(entry)
                    continue
                entry["parent"] = first_pass_summary(parent)
                print(f"     首次切片 {parent['plan_id']}：{parent.get('source_duration')}s → "
                      f"{parent.get('output_duration')}s，开始二次精剪……")
                second_job = second_pass_for_parent(args.base, args.project, asset, parent,
                                                   timeout=args.cut_timeout, options=options)
                entry["second_pass_job"] = {key: second_job.get(key) for key in ("status", "error")}
                if second_job.get("status") != "completed":
                    print(f"     二次精剪失败：{entry['second_pass_job']}")
                    cuts.append(entry)
                    continue
                final = read_interactions(args.base, args.project, asset, order_mode=args.order_mode)
                # A parent can have more than one second-pass plan once the
                # derivation itself has been revised; the newest is the one this
                # run produced, and it is the one the report must quote.
                matching = [item for item in (final.get("second_pass_candidates") or [])
                            if str((item.get("parent") or {}).get("plan_id")) == str(parent["plan_id"])]
                matching.sort(key=lambda item: str(item.get("updated_at") or ""))
                plan = matching[-1] if matching else None
                entry["second_pass"] = summarise(plan) if plan else None
                if plan:
                    limited = entry["second_pass"] or {}
                    print(f"     二次精剪 {plan['plan_id']}：父 {limited.get('source_duration')}s → "
                          f"{limited.get('output_duration')}s（停顿压缩 "
                          f"{limited.get('removed_by_pause_seconds')}s/"
                          f"{limited.get('pause_trim_count')} 处，目标 {limited.get('target_duration_status')}，"
                          f"QA {limited.get('qa')}）")
                cuts.append(entry)
            report["cuts"] = cuts

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
