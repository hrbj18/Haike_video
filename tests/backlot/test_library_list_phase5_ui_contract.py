from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIBRARY_JS = ROOT / "backlot" / "ui" / "library.js"
BOARD_CSS = ROOT / "backlot" / "ui" / "board.css"
WORKBENCH_CSS = ROOT / "backlot" / "ui" / "workbench.css"


def test_project_library_uses_rows_without_replacing_real_project_links():
    source = LIBRARY_JS.read_text(encoding="utf-8")

    assert "function projectRowState(project)" in source
    assert "function projectRowFacts(project)" in source
    assert 'class: "lib-card-link lib-project-row-link"' in source
    assert 'class: "project-row-enter" }, "进入工作区"' in source
    assert 'class: "project-manage"' in source
    assert "href: `/p/${project.project_id}${staticSuffix}`" in source
    assert "thumbURL" not in source


def test_warm_library_rows_and_selected_scene_have_explicit_light_theme_contracts():
    library_css = BOARD_CSS.read_text(encoding="utf-8")
    workbench_css = WORKBENCH_CSS.read_text(encoding="utf-8")

    for selector in (
        ".lib-project-row",
        ".lib-project-row-link",
        ".project-row-key",
        ".project-row-state.awaiting",
        ":root[data-theme=\"light\"] .lib-project-row",
        ":root[data-theme=\"light\"] .project-row-enter",
    ):
        assert selector in library_css

    assert ':root[data-theme="light"] .scene-item.active' in workbench_css
    assert "background: #f6e2d4;" in workbench_css
    assert "box-shadow: inset 3px 0 0 #c14a2b;" in workbench_css
