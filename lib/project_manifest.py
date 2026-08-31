"""Validation for reusable content project manifests."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
REQUIRED_KEYS = ("id", "title", "version", "format", "speakers", "providers", "quality_gates")


class ProjectManifestError(ValueError):
    """Raised when a project manifest is missing required production metadata."""


def load_project_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ProjectManifestError(f"Project manifest does not exist: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ProjectManifestError(f"Project manifest must be a mapping: {path}")
    validate_project_manifest(data, path)
    return data


def validate_project_manifest(data: dict[str, Any], path: Path | None = None) -> None:
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ProjectManifestError(f"Missing required keys {missing} in {path or 'manifest'}")
    project_id = data.get("id")
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise ProjectManifestError(
            f"Project id must be 3-64 lowercase ASCII characters with hyphens: {project_id!r}"
        )
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ProjectManifestError("Project title must be a non-empty string")
    if not isinstance(data.get("version"), int) or data["version"] < 1:
        raise ProjectManifestError("Project version must be a positive integer")
    format_data = data.get("format")
    if not isinstance(format_data, dict):
        raise ProjectManifestError("format must be a mapping")
    if format_data.get("fps") not in {24, 25, 30, 50, 60}:
        raise ProjectManifestError("format.fps must be one of 24, 25, 30, 50 or 60")
    if format_data.get("aspect_ratio") not in {"9:16", "16:9", "1:1", "4:5"}:
        raise ProjectManifestError("format.aspect_ratio must be one of 9:16, 16:9, 1:1 or 4:5")
    speakers = data.get("speakers")
    if not isinstance(speakers, list) or not speakers or not all(isinstance(item, dict) for item in speakers):
        raise ProjectManifestError("speakers must be a non-empty list of mappings")
    if not all(item.get("id") and item.get("display_name") for item in speakers):
        raise ProjectManifestError("each speaker requires id and display_name")
    for section in ("providers", "quality_gates"):
        if not isinstance(data.get(section), dict):
            raise ProjectManifestError(f"{section} must be a mapping")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an OpenMontage project manifest")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        manifest = load_project_manifest(args.manifest)
    except ProjectManifestError as exc:
        print(f"INVALID: {exc}")
        return 1
    print(f"VALID: {manifest['id']} / {manifest['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

