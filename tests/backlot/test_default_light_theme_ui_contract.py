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
