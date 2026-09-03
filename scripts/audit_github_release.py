"""Audit the GitHub source-release boundary without changing Git state.

The OpenMontage workspace intentionally contains local agent tooling, private
media and generated production evidence alongside source code.  This tool
classifies files before a human prepares a named Git staging allowlist.  It
never stages, commits, pushes, deletes or prints any suspected secret value.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

ELIGIBLE_PREFIXES = (
    ".github/",
    "backlot/",
    "config/",
    "content/templates/",
    "lib/",
    "remotion-composer/src/",
    "schemas/",
    "scripts/",
    "styles/",
    "tests/",
    "tools/",
)
ELIGIBLE_DOC_PREFIXES = (
    "docs/ASSET_LIBRARY_GOVERNANCE_",
    "docs/AVATAR_",
    "docs/DEPLOYMENT_",
    "docs/handoff/",
    "docs/LOCAL_TTS_",
    "docs/MIGRATION_PLAN_",
    "docs/OPENMONTAGE_WORKSPACE_",
    "docs/PRD_",
    "docs/RELEASE_",
    "docs/RUNNINGHUB_",
)
ELIGIBLE_EXACT = {
    ".env.example",
    ".gitattributes",
    ".gitignore",
    ".python-version",
    "AGENT_GUIDE.md",
    "AGENTS.md",
    "CHANGELOG.md",
    "CODEX.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "Makefile",
    "PROJECT_CONTEXT.md",
    "README.md",
    "README_zh-CN.md",
    "THIRD_PARTY_NOTICES.md",
    "UPSTREAM.md",
    "config.yaml",
    "remotion-composer/package-lock.json",
    "remotion-composer/package.json",
    "remotion-composer/SCENE_TYPES.md",
    "remotion-composer/titled_video_props.json",
    "remotion-composer/tsconfig.json",
    "setup.py",
    "启动工作台.bat",
    "更新并重启工作台.bat",
}
ELIGIBLE_ROOT_GLOBS = ("requirements*.txt",)
EXCLUDED_PREFIXES = (
    ".agents/",
    ".claude/",
    ".codex/",
    ".cursor/",
    "assets/",
    "ink-theater/",
    "pipeline_defs/",
    "prototypes/",
    "skills/",
)
EXCLUDED_DOC_PREFIXES = (
    "docs/images/",
    "docs/stage-gates/",
    "docs/examples/",
    "docs/FORMAL_UI_",
    "docs/ISOLATED_TEST_REPORT_",
    "docs/LANDSCAPE_HERO_",
    "docs/MONEYPRINTERTURBO_",
    "docs/SINGLE_DEVELOPMENT_GUIDE_",
    "docs/SINGLE_PRODUCTION_PLAN_",
)
EXCLUDED_EXACT = {
    "debug.log",
    "design-qa.md",
    "diagram.png",
    "render-demo.sh",
    "render_demo.py",
    ".windsurfrules",
    "CLAUDE.md",
    "COPILOT.md",
    "CURSOR.md",
    "PROMPT_GALLERY.md",
    "docs/ARCHITECTURE.md",
    "docs/PROVIDERS.md",
    "docs/PR_REVIEW_GUIDE.md",
    "docs/SPONSORS.md",
    "docs/apple-silicon-mps.md",
    "docs/comfyui-adapter-plan.md",
}
SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9_-])ark-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


@dataclass(frozen=True)
class ReleaseAudit:
    eligible: tuple[str, ...]
    excluded: tuple[str, ...]
    review: tuple[str, ...]
    secret_paths: tuple[str, ...]

    @property
    def ready_for_named_staging(self) -> bool:
        return not self.review and not self.secret_paths


def normalize_path(value: str) -> str:
    path = value.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def classify_path(relative: str) -> str:
    """Return eligible, excluded, or review; never infer that unknown files ship."""
    path = normalize_path(relative)
    if path in EXCLUDED_EXACT or path.startswith(EXCLUDED_PREFIXES) or path.startswith(EXCLUDED_DOC_PREFIXES):
        return "excluded"
    if path.startswith("remotion-composer/public/demo-props/") and path.endswith(".json"):
        return "eligible"
    if path in ELIGIBLE_EXACT or path.startswith(ELIGIBLE_PREFIXES) or path.startswith(ELIGIBLE_DOC_PREFIXES):
        return "eligible"
    candidate = Path(path)
    if candidate.parent == Path(".") and any(candidate.match(pattern) for pattern in ELIGIBLE_ROOT_GLOBS):
        return "eligible"
    return "review"


def git_candidate_paths(root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Return only paths that would enter the next commit.

    ``git ls-files -co`` also lists clean tracked files.  Auditing that set
    made a release branch fail merely because it already contained an older
    handoff file outside the current allowlist.  The release boundary is about
    the next commit, so inspect tracked changes relative to ``HEAD`` plus
    untracked, non-ignored files instead.
    """
    commands = (
        ["git", "diff", "--name-only", "-z", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    )
    paths: set[str] = set()
    for command in commands:
        completed = subprocess.run(command, cwd=root, check=True, capture_output=True)
        paths.update(
            normalize_path(item)
            for item in completed.stdout.decode("utf-8", "surrogateescape").split("\0")
            if item
        )
    return tuple(sorted(paths))


def has_high_confidence_secret(path: Path) -> bool:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return any(pattern.search(content) for pattern in SECRET_PATTERNS)


def audit(root: Path = REPO_ROOT, paths: tuple[str, ...] | None = None) -> ReleaseAudit:
    buckets: dict[str, list[str]] = {"eligible": [], "excluded": [], "review": []}
    candidates = paths if paths is not None else git_candidate_paths(root)
    for relative in candidates:
        buckets[classify_path(relative)].append(normalize_path(relative))
    secret_paths = tuple(
        relative for relative in buckets["eligible"]
        if has_high_confidence_secret(root / relative)
    )
    return ReleaseAudit(
        eligible=tuple(sorted(buckets["eligible"])),
        excluded=tuple(sorted(buckets["excluded"])),
        review=tuple(sorted(buckets["review"])),
        secret_paths=secret_paths,
    )


def _summary(report: ReleaseAudit) -> dict[str, object]:
    excluded_top_level = Counter(path.split("/", 1)[0] for path in report.excluded)
    return {
        "eligible_count": len(report.eligible),
        "excluded_count": len(report.excluded),
        "review_count": len(report.review),
        "secret_match_count": len(report.secret_paths),
        "ready_for_named_staging": report.ready_for_named_staging,
        "excluded_top_level": dict(sorted(excluded_top_level.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--show-paths", action="store_true")
    args = parser.parse_args(argv)
    report = audit(args.root.resolve())
    summary = _summary(report)
    if args.as_json:
        print(json.dumps(summary | {"review": report.review, "secret_paths": report.secret_paths}, ensure_ascii=False, indent=2))
    else:
        print("[OK] Git state was not changed.")
        for key in ("eligible_count", "excluded_count", "review_count", "secret_match_count"):
            print(f"{key}={summary[key]}")
        print(f"ready_for_named_staging={summary['ready_for_named_staging']}")
        print("excluded_top_level=" + json.dumps(summary["excluded_top_level"], ensure_ascii=False, sort_keys=True))
        if args.show_paths:
            for label, paths in (("REVIEW", report.review), ("SUSPECTED_SECRET", report.secret_paths)):
                for path in paths:
                    print(f"{label} {path}")
    if report.secret_paths:
        return 2
    return 1 if report.review else 0


if __name__ == "__main__":
    raise SystemExit(main())
