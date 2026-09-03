"""Static contracts for the Phase 4 segment-workbench migration.

These checks intentionally do not start a server, render media, call a model,
or issue any mutation request.  They protect the read-only full-preview
fallback and the editor structure used by the real workbench.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "backlot" / "ui" / "workbench.js").read_text(encoding="utf-8")
CSS = (ROOT / "backlot" / "ui" / "workbench.css").read_text(encoding="utf-8")
NAVIGATION_CSS = (ROOT / "backlot" / "ui" / "navigation.css").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    pattern = re.compile(
        rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{(?P<body>.*?)"
        r"(?=\n(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(|\nconst\s+[A-Z_]+\s*=)",
        flags=re.DOTALL,
    )
    match = pattern.search(JS)
    assert match, f"missing top-level JavaScript function: {name}"
    return match.group("body")


def test_completed_full_preview_is_a_read_only_segment_playback_fallback() -> None:
    source = _function_body("reviewPlaybackSource")
    assert 'kind: "scene"' in source
    assert 'kind: "full_preview"' in source
    assert 'const full = fullPreviewState().preview || {};' in source
    assert 'full.status === "completed" && full.output_path' in source
    assert 'offset_seconds: Math.max(0, Number((scene || {}).start_seconds || 0))' in source
    assert 'method: "POST"' not in source


def test_frame_player_applies_segment_offset_without_generating_media() -> None:
    stage = _function_body("renderFrameStage")
    assert "const preview = reviewPlaybackSource(scene);" in stage
    assert '"data-review-playback-source": preview.kind' in stage
    assert "Number(preview.offset_seconds || 0) + Number(relative || 0)" in stage
    assert 'preview.kind === "full_preview" ? "生成独立片段预览"' in stage
    assert "正在复用已完成的全片审核预览" in stage
    assert "mutate(`/scenes/${encodeURIComponent(scene.id)}/review-preview`" in stage


def test_demo_editor_sections_map_to_real_workbench_components() -> None:
    editor = _function_body("renderReviewEditor")
    controls = _function_body("renderReviewControls")
    for label in ("画面", "素材", "字幕", "配音", "数字人", "检查"):
        assert label in JS
    assert "renderSubtitleEditor(scene)" in editor
    assert "renderNarrationReview(scene)" in editor
    assert "renderReviewControls(scene, reviewEditorTab)" in editor
    assert "renderVisualTimelineEditor(scene)" in controls
    assert "renderVisualCompositionEditor(scene)" in controls
    assert "renderPresenterLayoutEditor(scene)" in controls
    assert "renderStoryHeadlineLayoutEditor(scene)" in controls
    assert 'showChecks ? el("div", { class: "panel-body control-group" }' in controls


def test_warm_paper_editor_and_fixed_desktop_director_rail_are_scoped() -> None:
    assert ".review-editor-tabs" in CSS
    assert ":root[data-theme=\"light\"] .review-anchor" in CSS
    assert ":root[data-theme=\"light\"] .light-review-player" in CSS
    assert ":root[data-theme=\"light\"] .light-review-canvas > video { background: transparent; }" in CSS
    assert "@media (min-width: 861px)" in NAVIGATION_CSS
    assert ".shell .global-sidebar" in NAVIGATION_CSS
    assert "position: fixed;" in NAVIGATION_CSS
    assert "@media (max-width: 860px)" in NAVIGATION_CSS
