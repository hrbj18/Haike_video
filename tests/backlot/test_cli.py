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
