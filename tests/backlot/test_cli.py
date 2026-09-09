"""Tests for the local Backlot launcher CLI."""

from __future__ import annotations

from types import SimpleNamespace

from backlot import __main__ as cli


def test_open_without_browser_keeps_the_server_and_does_not_open_a_tab(monkeypatch, capsys):
    opened: list[str] = []
    monkeypatch.setattr(cli, "_server_alive", lambda port: True)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    result = cli.cmd_open(None, open_browser=False)

    assert result == 0
    assert opened == []
    assert "http://127.0.0.1:" in capsys.readouterr().out


def test_open_command_forwards_the_no_browser_flag(monkeypatch):
    calls: list[tuple[str | None, bool]] = []

    def fake_open(project_id: str | None, *, open_browser: bool = True) -> int:
        calls.append((project_id, open_browser))
        return 0

    monkeypatch.setattr(cli, "cmd_open", fake_open)

    assert cli.main(["open", "demo-project", "--no-browser"]) == 0
    assert calls == [("demo-project", False)]


def test_local_health_probe_bypasses_system_proxy(monkeypatch):
    captured: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, url, timeout):
            captured["url"] = url
            captured["timeout"] = timeout
            return Response()

    def fake_proxy_handler(value):
        captured["proxy"] = value
        return SimpleNamespace()

    monkeypatch.setattr(cli.urllib.request, "ProxyHandler", fake_proxy_handler)
    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda handler: Opener())

    assert cli._server_alive(4754) is True
    assert captured["proxy"] == {}
    assert captured["url"] == "http://127.0.0.1:4754/api/health"


def test_queue_submit_uses_registered_kind_and_codex_api(monkeypatch, capsys):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        calls.append((path, method, payload))
        return {"queue_job": {"job_id": "PQ-one", "status": "queued"}}

    monkeypatch.setattr(cli, "_queue_api_request", fake_request)
    result = cli.main(
        [
            "queue",
            "submit",
            "news-demo",
            "--kind",
            "avatar-review-preview",
            "--priority",
            "priority",
            "--idempotency-key",
            "daily-news-2026-09-08",
            "--request-json",
            '{"confirmed":true,"budget_limit_cny":5}',
        ]
    )

    assert result == 0
    assert calls == [
        (
            "/api/production-queue/jobs",
            "POST",
            {
                "project_id": "news-demo",
                "kind": "avatar-review-preview",
                "priority": "priority",
                "request": {"confirmed": True, "budget_limit_cny": 5},
                "idempotency_key": "daily-news-2026-09-08",
            },
        )
    ]
    assert "PQ-one" in capsys.readouterr().out


def test_queue_cli_rejects_invalid_json_before_network_call(monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(cli, "_queue_api_request", lambda *_args, **_kwargs: calls.append("called"))

    result = cli.main(
        ["queue", "submit", "demo", "--kind", "full-preview", "--request-json", "not-json"]
    )

    assert result == 1
    assert calls == []
    assert "不是有效 JSON" in capsys.readouterr().err
