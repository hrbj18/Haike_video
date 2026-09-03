"""Run contract-level QA for a reusable OpenMontage content project."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.project_manifest import ProjectManifestError, load_project_manifest
from lib.workspace_paths import WorkspacePaths


def _paragraphs(path: Path) -> list[str]:
    return [part.strip() for part in path.read_text(encoding="utf-8").split("\n\n") if part.strip()]


def qa_project(project_id: str, repo_root: Path) -> dict[str, Any]:
    workspace = WorkspacePaths.from_repo_root(repo_root)
    project_root = workspace.project_source(project_id)
    manifest_path = project_root / "project.yaml"
    report: dict[str, Any] = {
        "project_id": project_id,
        "project_root": str(project_root),
        "passed": False,
        "errors": [],
        "warnings": [],
        "checks": {},
    }

    try:
        manifest = load_project_manifest(manifest_path)
        report["checks"]["manifest"] = "passed"
    except ProjectManifestError as exc:
        report["errors"].append(str(exc))
        report["checks"]["manifest"] = "failed"
        return report

    required_dirs = ("docs", "script", "timeline", "composition", "media", "qa")
    missing_dirs = [name for name in required_dirs if not (project_root / name).is_dir()]
    if missing_dirs:
        report["errors"].append(f"Missing project directories: {missing_dirs}")
    report["checks"]["directories"] = "passed" if not missing_dirs else "failed"

    speaker_ids = [item["id"] for item in manifest["speakers"]]
    script_counts: dict[str, int] = {}
    for speaker_id in speaker_ids:
        script_path = project_root / "script" / f"{speaker_id}-clean.txt"
        if not script_path.exists():
            report["errors"].append(f"Missing clean script: {script_path}")
            continue
        paragraphs = _paragraphs(script_path)
        script_counts[speaker_id] = len(paragraphs)
        if not paragraphs:
            report["errors"].append(f"Clean script is empty: {script_path}")
    report["checks"]["speaker_scripts"] = "passed" if len(script_counts) == len(speaker_ids) and not report["errors"] else "failed"

    timeline_path = project_root / "timeline" / "dialogue.json"
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        segments = timeline.get("segments", [])
        expected_indices = list(range(1, len(segments) + 1))
        actual_indices = [segment.get("index") for segment in segments]
        if actual_indices != expected_indices:
            report["errors"].append("Timeline indices are not contiguous starting at 1")
        for segment in segments:
            speaker_id = segment.get("speaker_id")
            if speaker_id not in speaker_ids:
                report["errors"].append(f"Timeline references unknown speaker: {speaker_id}")
                continue
            ref = Path(str(segment.get("text_ref", "")))
            if ref.is_absolute() or ".." in ref.parts:
                report["errors"].append(f"Timeline text_ref must be project-relative: {ref}")
                continue
            referenced = project_root / ref
            if not referenced.exists():
                report["errors"].append(f"Timeline text_ref does not exist: {referenced}")
                continue
            paragraph = segment.get("paragraph")
            if not isinstance(paragraph, int) or paragraph < 1 or paragraph > script_counts.get(speaker_id, 0):
                report["errors"].append(f"Invalid paragraph for timeline segment {segment.get('index')}")
        report["checks"]["timeline"] = "passed" if not report["errors"] else "failed"
        report["timeline_segments"] = len(segments)
    except (OSError, json.JSONDecodeError) as exc:
        report["errors"].append(f"Unable to read timeline: {exc}")
        report["checks"]["timeline"] = "failed"

    if manifest.get("quality_gates", {}).get("source_citation"):
        source_url = manifest.get("source", {}).get("official_url")
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            report["errors"].append("source_citation is enabled but source.official_url is missing")
        report["checks"]["source_citation"] = "passed" if source_url else "failed"

    report["passed"] = not report["errors"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reusable project contract QA")
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo_root = REPO_ROOT
    report = qa_project(args.project, repo_root)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
