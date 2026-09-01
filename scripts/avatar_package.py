"""Operate an Haike Video avatar source package without the browser workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backlot.avatar_import import (
    assemble_avatar_package,
    initialize_avatar_package,
    read_avatar_package,
    run_avatar_asr,
    start_avatar_assembly,
    start_avatar_asr,
    validate_avatar_package,
)
from backlot.state import PROJECTS_DIR


def project_directory(project_id: str) -> Path:
    project = (PROJECTS_DIR / project_id).resolve()
    try:
        project.relative_to(PROJECTS_DIR.resolve())
    except ValueError as exc:
        raise SystemExit("project_id may not escape the projects directory") from exc
    if not (project / "project.json").is_file():
        raise SystemExit(f"unknown project: {project_id}")
    return project


def summary(package: dict | None) -> dict:
    if not package:
        return {"exists": False}
    return {
        "exists": True,
        "project_id": package["project_id"],
        "import_mode": package["import_mode"],
        "audio_mode": package["audio_mode"],
        "turns": len(package["turns"]),
        "uploaded_turns": sum(1 for turn in package["turns"] if turn.get("source")),
        "validation": package["validation"]["status"],
        "asr": package["asr"]["status"],
        "assembly": package["assembly"]["status"],
        "outputs": {
            key: package["assembly"].get(key)
            for key in ("output_path", "timeline_path", "subtitle_path", "qa_path")
            if package["assembly"].get(key)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and assemble native-audio avatar clips")
    parser.add_argument("project_id")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="initialize the package from artifacts/script.json")
    init_parser.add_argument("--mode", choices=("per_turn", "longform"), default="per_turn")
    init_parser.add_argument("--replace", action="store_true")
    subparsers.add_parser("status", help="show a compact package status")
    subparsers.add_parser("validate", help="probe all uploaded source media")
    asr_parser = subparsers.add_parser("asr", help="run local ASR and script alignment")
    asr_parser.add_argument("--model")
    assembly_parser = subparsers.add_parser("assemble", help="create the native-audio dialogue master")
    assembly_parser.add_argument("--model")
    args = parser.parse_args()
    project = project_directory(args.project_id)

    if args.command == "init":
        package = initialize_avatar_package(project, {"import_mode": args.mode, "replace": args.replace})
    elif args.command == "status":
        package = read_avatar_package(project)
    elif args.command == "validate":
        package = validate_avatar_package(project)
    elif args.command == "asr":
        payload = {"model": args.model} if args.model else {}
        start_avatar_asr(project, payload)
        package = run_avatar_asr(project, payload)
    else:
        payload = {"model": args.model} if args.model else {}
        start_avatar_assembly(project, payload)
        package = assemble_avatar_package(project, payload)

    print(json.dumps(summary(package), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
