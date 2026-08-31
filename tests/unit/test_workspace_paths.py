from pathlib import Path

from lib.workspace_paths import WorkspaceConfigError, WorkspacePaths


def test_workspace_paths_are_inside_repository(tmp_path: Path):
    paths = WorkspacePaths.from_repo_root(Path(__file__).resolve().parents[2])
    assert paths.project_source("003-tech-chat").is_relative_to(paths.repo_root)
    assert paths.project_docs("003-tech-chat") == paths.project_source("003-tech-chat") / "docs"


def test_project_id_rejects_path_traversal(tmp_path: Path):
    paths = WorkspacePaths.from_repo_root(Path(__file__).resolve().parents[2])
    for value in ("..", "../escape", "nested/project", ""):
        try:
            paths.project_source(value)
        except WorkspaceConfigError:
            pass
        else:
            raise AssertionError(f"project id should be rejected: {value!r}")

