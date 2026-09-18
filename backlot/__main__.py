"""Backlot CLI.

    python -m backlot open [project-id]   # start server if needed, open browser
    python -m backlot open --no-browser   # start server only; print the local URL
    python -m backlot serve [--port N]    # run the server in the foreground

    python -m backlot research-pack <path>            # intake one episode research pack
    python -m backlot research-pack --batch <root>    # reconcile every episode below root

``research-pack`` runs in-process on purpose: it needs neither the server nor the
heavy production queue, because intake only writes a ledger and an editorial
snapshot (never a project or a final video).

``open`` is idempotent and non-fatal by design: agents call it at pipeline
initialization and must continue the production even if it fails.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from backlot import DEFAULT_PORT

SERVER_STARTUP_TIMEOUT_SECONDS = 60


def _port() -> int:
    try:
        return int(os.environ.get("BACKLOT_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def _server_alive(port: int) -> bool:
    try:
        # A desktop may have HTTP(S)_PROXY configured globally.  Local health
        # probes must never travel through that proxy, otherwise a running
        # Backlot process can be mistaken for a failed launch.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _spawn_server(port: int) -> None:
    """Start the server as a detached background process."""
    cmd = [sys.executable, "-m", "backlot", "serve", "--port", str(port)]
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def cmd_open(project_id: str | None, *, open_browser: bool = True) -> int:
    port = _port()
    if not _server_alive(port):
        try:
            _spawn_server(port)
        except Exception as exc:
            print(f"backlot: could not start server ({exc}) — continuing without the board")
            return 1
        deadline = time.time() + SERVER_STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if _server_alive(port):
                break
            time.sleep(0.4)
        else:
            print("backlot: server did not come up in time — continuing without the board")
            return 1
    url = f"http://127.0.0.1:{port}/"
    if project_id:
        url = f"http://127.0.0.1:{port}/p/{project_id}"
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print(f"backlot: {url}")
    return 0


def _attach_server_logging() -> None:
    """Mirror server logs into .backlot/logs/backlot.log.

    The launcher starts this process detached and windowless, so stdout and
    stderr never reach anything a human can read.  Writing our own log file
    guarantees that a crashed or wedged server can still be explained later.
    """
    import logging
    from pathlib import Path

    log_dir = Path(__file__).resolve().parent.parent / ".backlot" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "backlot.log", encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)


def _detach_console() -> None:
    """Drop the inherited console when we are meant to run as a service.

    A Windows virtual environment's ``python.exe`` is a trampoline: it spawns
    the real interpreter as a child process and waits for it.  The child ends up
    owning a freshly created console of its own, even when the trampoline itself
    was started detached.  That console is a window somebody can close, and
    closing it kills the server -- which is exactly the failure this command
    keeps hitting.  ``FreeConsole`` detaches us from it, so no console event can
    ever take the server down, and the stray window disappears with it.

    Only done for background launches (``BACKLOT_DETACHED`` is set by
    scripts/launch_backlot.py): a developer running ``python -m backlot serve``
    in a terminal keeps their console and their Ctrl+C.
    """
    if os.name != "nt" or not os.environ.get("BACKLOT_DETACHED"):
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        kernel32.SetConsoleCtrlHandler.restype = ctypes.c_bool
        kernel32.FreeConsole.restype = ctypes.c_bool
        kernel32.SetConsoleCtrlHandler(None, True)
        kernel32.FreeConsole()
    except Exception:
        pass


def cmd_serve(port: int) -> int:
    import logging

    import uvicorn

    _detach_console()
    # The server is launched through ``pythonw.exe`` (see
    # scripts/launch_backlot.py) and then drops even that console, so it owns no
    # console at all.  Every console child it spawns -- ffmpeg, ffprobe, npx --
    # is therefore handed a brand new console *window* by Windows, and a render
    # spawns hundreds of them.  Installing the no-window default here covers the
    # whole process tree in one place, grandchildren included.
    from lib.subprocess_window import install_default

    install_default()
    _attach_server_logging()
    try:
        uvicorn.run("backlot.server:app", host="127.0.0.1", port=port, log_level="warning")
    except BaseException:
        # SystemExit from a failing lifespan handler used to kill this process
        # with nothing written anywhere; record it before it propagates.
        logging.getLogger("backlot.serve").exception(
            "Backlot server stopped unexpectedly on port %s", port
        )
        raise
    return 0


def _queue_api_request(path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    port = _port()
    if not _server_alive(port):
        raise RuntimeError("Backlot 服务未运行；请先执行 python -m backlot open --no-browser")
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = None
        raise RuntimeError(str(detail or f"队列接口返回 HTTP {exc.code}")) from exc


def cmd_queue(args: argparse.Namespace) -> int:
    try:
        if args.queue_command == "list":
            path = "/api/production-queue"
            if args.project_id:
                path += "?project_id=" + urllib.parse.quote(args.project_id)
            result = _queue_api_request(path)
        elif args.queue_command == "submit":
            try:
                request_payload = json.loads(args.request_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"--request-json 不是有效 JSON：{exc.msg}") from exc
            if not isinstance(request_payload, dict):
                raise RuntimeError("--request-json 必须是 JSON 对象")
            body = {
                "project_id": args.project_id,
                "kind": args.kind,
                "priority": args.priority,
                "request": request_payload,
            }
            if args.idempotency_key:
                body["idempotency_key"] = args.idempotency_key
            result = _queue_api_request("/api/production-queue/jobs", method="POST", payload=body)
        elif args.queue_command == "priority":
            result = _queue_api_request(
                f"/api/production-queue/jobs/{urllib.parse.quote(args.job_id)}/priority",
                method="POST",
                payload={"priority": args.priority},
            )
        elif args.queue_command in {"pause", "resume", "cancel", "retry"}:
            result = _queue_api_request(
                f"/api/production-queue/jobs/{urllib.parse.quote(args.job_id)}/{args.queue_command}",
                method="POST",
                payload={},
            )
        else:
            raise RuntimeError("请指定 queue 子命令")
    except RuntimeError as exc:
        print(f"backlot queue: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_research_pack(args: argparse.Namespace) -> int:
    """研究包 intake：只读消费 CopySkill 已发布的包，走本地账本 + 快照。

    刻意**不经**统一生产队列：队列的 kind 全部是 ``projects_root/<project_id>``
    下的媒体生产任务，而 intake 不产出项目也不产出成片。因此这里直接进程内调用
    （不需要 Backlot 服务、不触网、不花钱）。
    """
    from pathlib import Path

    from backlot.research_pack_intake import intake_from_current_json, reconcile

    if not Path(args.path).exists():
        print(f"backlot research-pack: 路径不存在：{args.path}", file=sys.stderr)
        return 1
    try:
        if args.batch:
            result = reconcile(
                args.path, ledger_path=args.ledger, snapshot_root=args.snapshot_root
            )
        else:
            result = intake_from_current_json(
                args.path, ledger_path=args.ledger, snapshot_root=args.snapshot_root
            )
    except Exception as exc:
        print(f"backlot research-pack: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backlot", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    p_open = sub.add_parser("open", help="open the board in the browser (starts server if needed)")
    p_open.add_argument("project_id", nargs="?", default=None)
    p_open.add_argument("--no-browser", action="store_true", help="start the local server but do not open a browser")

    p_serve = sub.add_parser("serve", help="run the Backlot server in the foreground")
    p_serve.add_argument("--port", type=int, default=_port())

    p_queue = sub.add_parser("queue", help="inspect or submit registered production jobs")
    queue_sub = p_queue.add_subparsers(dest="queue_command")
    p_queue_list = queue_sub.add_parser("list", help="list the global production queue")
    p_queue_list.add_argument("--project-id", default=None)
    p_queue_submit = queue_sub.add_parser("submit", help="submit a registered production job")
    p_queue_submit.add_argument("project_id")
    p_queue_submit.add_argument(
        "--kind",
        required=True,
        choices=("review-preview", "avatar-review-preview", "full-preview"),
    )
    p_queue_submit.add_argument(
        "--priority", choices=("priority", "normal", "background"), default="normal"
    )
    p_queue_submit.add_argument("--idempotency-key", default=None)
    p_queue_submit.add_argument("--request-json", required=True)
    for action in ("pause", "resume", "cancel", "retry"):
        parser_action = queue_sub.add_parser(action, help=f"{action} a production job")
        parser_action.add_argument("job_id")
    p_queue_priority = queue_sub.add_parser("priority", help="change queued job priority")
    p_queue_priority.add_argument("job_id")
    p_queue_priority.add_argument("priority", choices=("priority", "normal", "background"))

    p_research = sub.add_parser(
        "research-pack",
        help="read-only intake of a published episode research pack (no server, no network)",
    )
    p_research.add_argument(
        "path",
        help="episode root, its current.json, or (with --batch) a research-pack root",
    )
    p_research.add_argument(
        "--batch",
        action="store_true",
        help="treat path as a research-pack root and reconcile every episode below it",
    )
    p_research.add_argument("--ledger", default=None, help="override the intake ledger json path")
    p_research.add_argument(
        "--snapshot-root", default=None, help="override where editorial snapshots are written"
    )

    args = parser.parse_args(argv)
    if args.command == "open":
        return cmd_open(args.project_id, open_browser=not args.no_browser)
    if args.command == "serve":
        return cmd_serve(args.port)
    if args.command == "queue":
        return cmd_queue(args)
    if args.command == "research-pack":
        return cmd_research_pack(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
