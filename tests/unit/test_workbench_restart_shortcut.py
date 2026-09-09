from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_update_and_restart_shortcut_uses_safe_project_launcher() -> None:
    shortcut = REPO_ROOT / "更新并重启工作台.bat"
    content = shortcut.read_text(encoding="utf-8")

    assert 'cd /d "%~dp0"' in content
    assert 'scripts\\start_backlot.ps1' in content
    assert 'set "START_OPTIONS=-Restart"' in content
    assert 'BACKLOT_NO_BROWSER%' in content
    assert '-NoBrowser' in content
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass' in content
    assert 'if errorlevel 1' in content
    assert 'pause' in content


def test_safe_launcher_verifies_owner_and_health_before_restart() -> None:
    launcher = (REPO_ROOT / "scripts" / "start_backlot.ps1").read_text(encoding="utf-8")

    assert "function Stop-VerifiedBacklotServer" in launcher
    assert "/api/health" in launcher
    assert "health.app -ne 'backlot'" in launcher
    assert "-m\\s+backlot\\s+serve" in launcher
    assert "if ($Restart)" in launcher
    assert "Stop-VerifiedBacklotServer -TargetPort $Port" in launcher
