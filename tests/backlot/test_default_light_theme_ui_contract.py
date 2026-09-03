"""Contract checks for the shared warm-paper default theme."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = ROOT / "backlot" / "ui"
THEMED_PAGES = (
    "index.html",
    "audio_center.html",
    "automation.html",
    "workbench.html",
    "board.html",
)


def test_all_primary_pages_default_to_light_without_overriding_saved_dark_choice():
    for name in THEMED_PAGES:
        source = (UI_ROOT / name).read_text(encoding="utf-8")
        assert 'const storedTheme = localStorage.getItem(themeKey);' in source, name
        assert 'const theme = storedTheme === "dark" ? "dark" : "light";' in source, name
        assert 'if (storedTheme !== theme) localStorage.setItem(themeKey, theme);' in source, name
        assert 'document.documentElement.dataset.theme = theme;' in source, name
        assert "editorial-theme-v1" not in source, name


def test_library_and_legacy_board_follow_the_bootstrapped_theme_value():
    for name in ("library.js", "board.js"):
        source = (UI_ROOT / name).read_text(encoding="utf-8")
        assert 'document.documentElement.dataset.theme === "dark" ? "dark" : "light"' in source


def test_script_draft_editor_uses_warm_paper_tokens_in_light_theme():
    source = (UI_ROOT / "workbench.css").read_text(encoding="utf-8")
    assert ':root[data-theme="light"] .script-draft-editor' in source
    assert 'background: #f7eddd;' in source
    assert ':root[data-theme="light"] .script-edit-section' in source
    assert 'background: #fffaf0;' in source
    assert ':root[data-theme="light"] .script-sentence-number' in source


def test_review_preview_and_avatar_import_chrome_finish_in_warm_paper_theme():
    source = (UI_ROOT / "workbench.css").read_text(encoding="utf-8")
    completion = source[source.index("/* Warm-paper completion:") :]

    for selector in (
        ".review-preview-panel",
        ".review-preview-preflight",
        ".review-preview-preflight-grid > div",
        ".review-preview-capability",
        ".review-preview-avatar-binding",
        ".review-preview-job",
        ".avatar-template-import",
        ".avatar-user-script-import",
        ".template-turn",
        ".script-paste-box",
        ".avatar-turn",
        ".avatar-cut-card",
        ".asr-diagnostic-card",
        ".cloud-job",
        ".voicebox-batch-panel",
    ):
        assert selector in completion

    for token in ("background: #fbf6eb;", "background: #f7eddd;", "background: #f8e8d7;"):
        assert token in completion

    # A real preview video is intentionally not repainted: only UI chrome moves
    # to the warm-paper theme, so source pixels retain their expected contrast.
    assert ".review-preview-player video" not in completion
