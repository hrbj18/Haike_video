from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_daily_automation.py"
    spec = importlib.util.spec_from_file_location("openmontage_daily_wrapper_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_scheduler_wrapper_retries_exact_run_for_transient_failures(monkeypatch):
    module = _module()
    attempts = {"count": 0}
    sleeps = []
    monkeypatch.setattr(sys, "argv", ["wrapper", "run", "--previous-day", "--trigger", "schedule"])

    def runner():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("HTTP 503 temporary")
        return 0

    code = module._run_with_transient_recovery(runner=runner, sleeper=sleeps.append)

    assert code == 0
    assert attempts["count"] == 3
    assert sleeps == [30, 90]


def test_scheduler_wrapper_does_not_retry_configuration_failure(monkeypatch):
    module = _module()
    attempts = {"count": 0}
    monkeypatch.setattr(sys, "argv", ["wrapper", "run", "--previous-day", "--trigger", "schedule"])

    def runner():
        attempts["count"] += 1
        raise RuntimeError("RunningHub workflow id is missing")

    assert module._run_with_transient_recovery(runner=runner, sleeper=lambda _delay: None) == 1
    assert attempts["count"] == 1
