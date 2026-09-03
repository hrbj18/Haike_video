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
    assert "一键理解全部本地视频" in script
    assert 'api("/assets/media-index/vision-batch"' in script
    assert "脚本可以继续编写，不会被打断" in script
    assert ".vision-shot-card" in styles
    assert ".media-recommendation-frame" in styles
    assert ".local-material-preparation-card" in styles
