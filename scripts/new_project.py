"""Create a portable Haike Video content project from the checked-in template."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.workspace_paths import WorkspacePaths


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a new Haike Video content project")
    parser.add_argument("--id", required=True, help="Lowercase ASCII project id, e.g. 004-ai-news")
    parser.add_argument("--title", required=True, help="Human-readable project title")
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--aspect-ratio", default="9:16")
    args = parser.parse_args()

    workspace = WorkspacePaths.from_repo_root(REPO_ROOT)
    target = workspace.project_source(args.id)
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"Project already exists and is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    for name in ("docs", "script", "timeline", "composition", "media", "qa"):
        (target / name).mkdir(parents=True, exist_ok=True)

    template_path = workspace.templates_root / "project.yaml"
    data = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    data["id"] = args.id
    data["title"] = args.title
    data["format"]["fps"] = args.fps
    data["format"]["aspect_ratio"] = args.aspect_ratio
    (target / "project.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
