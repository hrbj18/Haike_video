"""Tests for the non-mutating GitHub release-boundary audit."""

from __future__ import annotations

import subprocess

from scripts import audit_github_release as release


def test_classification_allows_product_source_but_not_local_agent_or_media_trees():
    assert release.classify_path("backlot/audio_center.py") == "eligible"
    assert release.classify_path("backlot/ui/workbench.js") == "eligible"
    assert release.classify_path("tools/audio/doubao_tts.py") == "eligible"
    assert release.classify_path("docs/handoff/DEPLOYMENT.md") == "eligible"
    assert release.classify_path(".agents/private-state.json") == "excluded"
    assert release.classify_path("assets/avatar_sources/yaya.png") == "excluded"
    assert release.classify_path("prototypes/haike-video-ui-demo-v1/src/App.jsx") == "excluded"
    assert release.classify_path("docs/SINGLE_DEVELOPMENT_GUIDE_FORMAL_UI_PHASE4_REVIEW_WORKBENCH_ZH-CN.md") == "excluded"
    assert release.classify_path("docs/images/backlot/board-live.png") == "excluded"


def test_unknown_paths_require_explicit_human_release_decision():
    assert release.classify_path("pipeline_defs/legacy.yaml") == "excluded"
    assert release.classify_path("vendor/new-runtime.bin") == "review"


def test_audit_redacts_secret_values_and_blocks_named_staging(tmp_path):
    (tmp_path / "backlot").mkdir()
    candidate = tmp_path / "backlot" / "provider.py"
    candidate.write_text('API_KEY = "' + "sk-" + 'abcdefghijklmnopqrstuvwxyz123456"', encoding="utf-8")

    report = release.audit(tmp_path, paths=("backlot/provider.py", "vendor/new-runtime.bin"))

    assert report.secret_paths == ("backlot/provider.py",)
    assert report.review == ("vendor/new-runtime.bin",)
    assert report.ready_for_named_staging is False


def test_git_candidate_paths_excludes_unchanged_tracked_files(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Release audit test"], cwd=tmp_path, check=True)
    (tmp_path / "kept.md").write_text("unchanged", encoding="utf-8")
    (tmp_path / "changed.py").write_text("before", encoding="utf-8")
    subprocess.run(["git", "add", "kept.md", "changed.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    (tmp_path / "changed.py").write_text("after", encoding="utf-8")
    (tmp_path / "new.py").write_text("new", encoding="utf-8")

    assert release.git_candidate_paths(tmp_path) == ("changed.py", "new.py")
