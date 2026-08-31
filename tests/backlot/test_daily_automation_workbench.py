from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_daily_automation_workbench_has_dedicated_entry_and_recovery_controls():
    library = (ROOT / "backlot" / "ui" / "index.html").read_text(encoding="utf-8")
    page = (ROOT / "backlot" / "ui" / "automation.html").read_text(encoding="utf-8")
    script = (ROOT / "backlot" / "ui" / "automation.js").read_text(encoding="utf-8")
    assert 'href="/automation"' in library
    assert "每日科技快报生产中心" in page
    assert "凌晨任务是否真的会运行" in page
    assert "从${STAGES.find" in script
    assert "中国市场选题主编" in script
    assert "独立传播冷审" in script
    assert "事实与技术兜底" in script
    assert "抖音热度信号" in script
    assert "网络、排队、限流和超时只恢复当前阶段" in script
    assert "不会升级到 Plus 48GB" in script
    assert 'id="textRecoveryDetail"' in page
    assert "文本韧性与候选组合" in script
    assert "保留头条并替换弱题" in script
    assert "/replace-weak-story" in script
    assert "等待供应商授权" in script
    assert "媒体放行" in script
    assert "系统不会自动重提" in script
    assert "paid_operations" in script


def test_automation_page_uses_local_assets_only():
    page = (ROOT / "backlot" / "ui" / "automation.html").read_text(encoding="utf-8")
    assert "https://" not in page
    assert "/ui/automation.css" in page
    assert "/ui/automation.js" in page
