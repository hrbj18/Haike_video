"""Portable workspace path resolution for OpenMontage.

The repository is the only canonical project root.  Machine-specific media
locations can be overridden through .env.local without changing committed
project files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml
except ModuleNotFoundError:  # Keep lightweight document/build tools usable with the bundled runtime.
    yaml = None


class WorkspaceConfigError(ValueError):
    """Raised when the workspace configuration is invalid."""


DEFAULT_CONFIG = {
    "content_root": "content",
    "project_source_root": "content/episodes",
    "library_root": "content/library",
    "templates_root": "content/templates",
    "runtime_root": "projects",
    "render_root": "renders",
    "cache_root": "cache",
    "temp_root": "temp",
    "migration_root": "content/migration",
}

ENV_OVERRIDES = {
    "content_root": "OPENMONTAGE_CONTENT_ROOT",
    "project_source_root": "OPENMONTAGE_PROJECT_SOURCE_ROOT",
    "library_root": "OPENMONTAGE_LIBRARY_ROOT",
    "templates_root": "OPENMONTAGE_TEMPLATES_ROOT",
    "runtime_root": "OPENMONTAGE_RUNTIME_ROOT",
    "render_root": "OPENMONTAGE_RENDER_ROOT",
    "cache_root": "OPENMONTAGE_CACHE_ROOT",
    "temp_root": "OPENMONTAGE_TEMP_ROOT",
    "migration_root": "OPENMONTAGE_MIGRATION_ROOT",
}


def find_repo_root(start: Path | None = None) -> Path:
    """Find the nearest OpenMontage repository root."""

    candidate = (start or Path(__file__)).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for path in (candidate, *candidate.parents):
        if (path / "AGENT_GUIDE.md").exists() and (path / ".git").exists():
            return path
    raise WorkspaceConfigError(f"Could not locate an OpenMontage repository root from {candidate}")


def _resolve_path(repo_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _load_config(repo_root: Path, config_path: Path | None = None) -> Mapping[str, Any]:
    path = config_path or repo_root / "config" / "workspace.yaml"
    if not path.exists():
        return {"paths": dict(DEFAULT_CONFIG), "defaults": {}}
    if yaml is None:
        # The checked-in workspace.yaml mirrors DEFAULT_CONFIG.  Falling back
        # here lets small local tools run before the full Python environment is
        # installed; the setup/preflight flow still installs PyYAML for all
        # manifest-aware commands.
        return {"paths": dict(DEFAULT_CONFIG), "defaults": {}}
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise WorkspaceConfigError(f"Workspace config must be a mapping: {path}")
    return raw


@dataclass(frozen=True)
class WorkspacePaths:
    """Canonical filesystem locations for one OpenMontage checkout."""

    repo_root: Path
    content_root: Path
    project_source_root: Path
    library_root: Path
    templates_root: Path
    runtime_root: Path
    render_root: Path
    cache_root: Path
    temp_root: Path
    migration_root: Path
    defaults: Mapping[str, Any]

    @classmethod
    def from_repo_root(
        cls,
        repo_root: Path | None = None,
        config_path: Path | None = None,
    ) -> "WorkspacePaths":
        root = find_repo_root(repo_root or Path(__file__)) if repo_root is None else repo_root.resolve()
        raw = _load_config(root, config_path)
        configured = dict(DEFAULT_CONFIG)
        configured.update(raw.get("paths", {}) or {})
        for key, env_name in ENV_OVERRIDES.items():
            value = os.environ.get(env_name)
            if value:
                configured[key] = value
        resolved = {key: _resolve_path(root, str(value)) for key, value in configured.items()}
        return cls(repo_root=root, defaults=raw.get("defaults", {}) or {}, **resolved)

    def project_source(self, project_id: str) -> Path:
        self._validate_project_id(project_id)
        return self.project_source_root / project_id

    def project_runtime(self, project_id: str) -> Path:
        self._validate_project_id(project_id)
        return self.runtime_root / project_id

    def project_docs(self, project_id: str) -> Path:
        return self.project_source(project_id) / "docs"

    def project_script(self, project_id: str) -> Path:
        return self.project_source(project_id) / "script"

    def project_timeline(self, project_id: str) -> Path:
        return self.project_source(project_id) / "timeline"

    def project_media(self, project_id: str) -> Path:
        return self.project_source(project_id) / "media"

    def project_composition(self, project_id: str) -> Path:
        return self.project_source(project_id) / "composition"

    def project_qa(self, project_id: str) -> Path:
        return self.project_source(project_id) / "qa"

    def ensure_runtime_dirs(self) -> None:
        for path in (self.content_root, self.library_root, self.runtime_root, self.render_root, self.cache_root, self.temp_root):
            path.mkdir(parents=True, exist_ok=True)

    def as_dict(self) -> dict[str, str]:
        return {
            "repo_root": str(self.repo_root),
            "content_root": str(self.content_root),
            "project_source_root": str(self.project_source_root),
            "library_root": str(self.library_root),
            "templates_root": str(self.templates_root),
            "runtime_root": str(self.runtime_root),
            "render_root": str(self.render_root),
            "cache_root": str(self.cache_root),
            "temp_root": str(self.temp_root),
            "migration_root": str(self.migration_root),
        }

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not project_id or project_id in {".", ".."} or Path(project_id).name != project_id:
            raise WorkspaceConfigError(f"Invalid project id: {project_id!r}")


def get_workspace_paths(repo_root: Path | None = None) -> WorkspacePaths:
    """Convenience accessor used by tools and scripts."""

    return WorkspacePaths.from_repo_root(repo_root)
