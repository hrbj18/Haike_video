from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_material_vision_workbench_exposes_confirmation_progress_and_evidence() -> None:
    script = (ROOT / "backlot" / "ui" / "workbench.js").read_text(encoding="utf-8")
    styles = (ROOT / "backlot" / "ui" / "workbench.css").read_text(encoding="utf-8")

    assert "理解画面（Luna）" in script
    assert "不会上传整条视频" in script
    assert 'body: { stage: "vision", remote_vision_confirmed: true }' in script
    assert "/media-index/vision?limit=80" in script
    assert "镜头级画面理解" in script
    assert "currentIndexJob.progress" in script
    assert "mediaURL(projectId, frame.path)" in script
    assert "采用到当前片段" in script
    assert "candidate.vision_summary" in script
    assert "candidate.entities" in script
    assert "candidate.actions" in script
    assert "本地素材准备" in script
    assert "renderLocalMaterialPreparationCard()" in script
    assert "supportsLocalMaterialPreparation()" in script
    assert "批量高效率内容处理" in script
    assert "批量理解画面（确认）" in script
    assert 'api("/assets/media-index/vision-batch"' in script
    assert 'api("/assets/media-index/overview-batch"' in script
    assert "脚本可以继续编写，不会被打断" in script
    assert ".vision-shot-card" in styles
    assert ".media-recommendation-frame" in styles
    assert ".local-material-preparation-card" in styles


def test_material_overview_ui_uses_one_confirmed_full_flow() -> None:
    script = (ROOT / "backlot" / "ui" / "workbench.js").read_text(encoding="utf-8")
    styles = (ROOT / "backlot" / "ui" / "workbench.css").read_text(encoding="utf-8")

    assert "高效率内容处理" in script
    assert 'stage: "overview"' in script
    assert "高效率内容处理（确认）" in script
    assert "确认后任务会自动完成上述流程，不会要求第二次确认" in script
    assert "remote_vision_confirmed: true" in script
    assert "不会上传整条视频" in script
    assert "默认不会转写原声音频" in script
    assert "/media-index/overview" in script
    assert "查看联系表" in script
    assert "升级精细化（确认）" in script
    assert "AI 生成概览（确认）" not in script
    assert "openOverviewCell" in script
    assert "source_video_path" in script
    assert "稀疏采样画面地图" in script
    assert ".material-overview-sheet" in styles
    assert ".overview-cell-list" in styles
