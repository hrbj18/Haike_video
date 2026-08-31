"""Browser regressions from the Backlot UI bug bash."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from lib.checkpoint import CANONICAL_STAGE_ARTIFACTS, init_project, write_checkpoint
from lib.pipeline_loader import get_stage_order, load_pipeline
from scripts import backlot_screenshot_stage
from tests.contracts.test_phase0_contracts import sample_artifact


pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402


APPROVAL_CASES = [
    ("gate-research", "framework-smoke", "research", "research_brief", "Test Topic"),
    ("gate-idea", "hybrid", "idea", "brief", "Did you know?"),
    ("gate-proposal", "cinematic", "proposal", "proposal_packet", "The Surprising Truth About X"),
    ("gate-script", "cinematic", "script", "script", "Hello world"),
    ("gate-scene-plan", "cinematic", "scene_plan", "scene_plan", "Host on camera"),
    ("gate-assets", "cinematic", "assets", "asset_manifest", "asset-1"),
    ("gate-edit", "documentary-montage", "edit", "edit_decisions", "cut-1"),
    ("gate-compose", "cinematic", "compose", "render_report", "renders/output.mp4"),
    ("gate-publish", "cinematic", "publish", "publish_log", "youtube"),
]


def _complete_predecessors(root, project_id: str, pipeline_type: str, stage: str) -> None:
    order = get_stage_order(load_pipeline(pipeline_type))
    for predecessor in order[: order.index(stage)]:
        artifact_name = CANONICAL_STAGE_ARTIFACTS.get(predecessor)
        if artifact_name:
            artifact = sample_artifact(artifact_name)
            if artifact_name == "edit_decisions":
                artifact["render_runtime"] = "ffmpeg"
            artifacts = {artifact_name: artifact}
        else:
            artifacts = {}
        write_checkpoint(
            root,
            project_id,
            predecessor,
            "completed",
            artifacts,
            pipeline_type=pipeline_type,
            human_approved=True,
        )


def _build_approval_projects() -> None:
    root = backlot_screenshot_stage.STAGE_DIR
    for project_id, pipeline_type, stage, artifact_name, _visible_text in APPROVAL_CASES:
        artifact = sample_artifact(artifact_name)
        if artifact_name == "edit_decisions":
            artifact["render_runtime"] = "ffmpeg"
        review_summary = (
            {
                "critical": 0,
                "suggestions": 1,
                "nitpicks": 0,
                "review_focus_met": "9/9",
                "schema_validation": "proposal_packet PASS",
            }
            if stage == "proposal"
            else "Artifact is ready for human review."
        )
        init_project(
            project_id,
            title=f"Approval fixture: {stage}",
            pipeline_type=pipeline_type,
            pipeline_dir=root,
        )
        _complete_predecessors(root, project_id, pipeline_type, stage)
        write_checkpoint(
            root,
            project_id,
            stage,
            "awaiting_human",
            {artifact_name: artifact},
            pipeline_type=pipeline_type,
            review={
                "round": 1,
                "decision": "pass",
                "critical": 0,
                "suggestions": 1,
                "nitpicks": 0,
                "summary": review_summary,
            },
        )

    # A manifest-declared custom stage/artifact proves the fallback is driven
    # by the stage contract rather than a hardcoded canonical-stage list.
    init_project(
        "gate-character-design",
        title="Approval fixture: character design",
        pipeline_type="character-animation",
        pipeline_dir=root,
    )
    _complete_predecessors(
        root,
        "gate-character-design",
        "character-animation",
        "character_design",
    )
    write_checkpoint(
        root,
        "gate-character-design",
        "character_design",
        "awaiting_human",
        {"character_design": {
            "version": "1.0",
            "characters": [{
                "id": "ada",
                "display_name": "Ada",
                "role": "explorer",
                "body_type": "round",
                "style": "flat graphic",
                "silhouette_notes": "Round explorer with a bright orange field jacket",
                "required_emotions": ["curious"],
                "required_actions": ["wave"],
            }],
        }},
        pipeline_type="character-animation",
    )

    avatar_project = init_project(
        "avatar-cloud-ready",
        title="双角色数字人页面回归",
        pipeline_type="avatar-spokesperson",
        pipeline_dir=root,
    )
    (avatar_project / "artifacts" / "script.json").write_text(json.dumps({
        "sections": [
            {"turn_id": "T001", "speaker_id": "yaya", "speaker_name": "雅雅", "text": "欢迎收看今天的科技快报。"},
            {"turn_id": "T002", "speaker_id": "mengmeng", "speaker_name": "檬檬", "text": "下面进入第二条消息。"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    from backlot.avatar_import import initialize_avatar_package
    from backlot.avatar_cloud import _refresh_readiness
    package = initialize_avatar_package(avatar_project, {"generation_mode": "dashscope_wan_s2v"})
    for binding in package["speaker_bindings"]:
        binding["presenter_shot"] = {
            "path": f"assets/incoming/avatar/presenter/{binding['speaker_id']}/presenter.png",
            "original_filename": f"{binding['name']}录音间.png", "sha256": ("a" if binding["speaker_id"] == "yaya" else "b") * 64,
            "size_bytes": 100, "uploaded_at": "2026-08-12T00:00:00Z",
            "media": {"width": 941, "height": 1672, "format": "PNG"},
        }
    for index, turn in enumerate(package["turns"]):
        turn["driving_audio"] = {
            "id": f"AVAC-{index:016d}", "path": f"assets/audio/avatar_driving/{turn['turn_id']}.wav",
            "provider_path": f"assets/audio/avatar_driving/{turn['turn_id']}_provider.wav",
            "original_filename": f"{turn['turn_id']}.wav", "sha256": str(index + 1) * 64,
            "size_bytes": 100, "duration_seconds": 4.5, "codec": "pcm_s16le", "state": "current",
            "source_type": "uploaded", "created_at": "2026-08-12T00:00:00Z",
        }
        turn["status"] = "audio_ready"
    _refresh_readiness(package)
    from backlot.avatar_import import _save_package
    _save_package(avatar_project, package)


@pytest.fixture(scope="module")
def staged_backlot_server():
    backlot_screenshot_stage.build_stage()
    _build_approval_projects()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    env = dict(os.environ)
    env["OPENMONTAGE_PROJECTS_DIR"] = str(backlot_screenshot_stage.STAGE_DIR)
    server = subprocess.Popen(
        [sys.executable, "-m", "backlot", "serve", "--port", str(port)],
        cwd=backlot_screenshot_stage.REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                break
        except Exception:
            time.sleep(0.2)
    else:
        server.terminate()
        raise RuntimeError("Backlot server did not become healthy")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def test_project_pages_fit_mobile_and_tablet_widths(staged_backlot_server):
    project_paths = [
        "/p/signal-in-the-static?static=1",
        "/p/the-slow-orchard?static=1",
        "/p/the-last-lighthouse?static=1",
        "/p/paper-boats?static=1",
        "/p/gate-proposal?static=1",
        "/p/gate-character-design?static=1",
    ]
    viewports = [
        {"width": 390, "height": 844},
        {"width": 768, "height": 1024},
    ]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for viewport in viewports:
                page.set_viewport_size(viewport)
                for path in project_paths:
                    page.goto(staged_backlot_server + path, wait_until="networkidle")
                    page.wait_for_timeout(300)
                    sizes = page.evaluate(
                        """() => ({
                            scrollWidth: document.documentElement.scrollWidth,
                            clientWidth: document.documentElement.clientWidth
                        })"""
                    )
                    assert sizes["scrollWidth"] <= sizes["clientWidth"], (
                        path,
                        viewport,
                        sizes,
                    )
        finally:
            browser.close()


def test_daily_automation_center_fits_desktop_and_narrow_widths(staged_backlot_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for viewport in ({"width": 390, "height": 844}, {"width": 1440, "height": 1000}):
                page.set_viewport_size(viewport)
                page.goto(staged_backlot_server + "/automation", wait_until="networkidle")
                page.wait_for_timeout(300)
                sizes = page.evaluate("""() => ({
                    scrollWidth: document.documentElement.scrollWidth,
                    clientWidth: document.documentElement.clientWidth
                })""")
                assert sizes["scrollWidth"] <= sizes["clientWidth"], (viewport, sizes)
                assert page.get_by_text("每日科技快报生产中心").is_visible()
        finally:
            browser.close()


def test_static_navigation_invalid_route_and_active_takes(staged_backlot_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 1000})
        try:
            page.goto(staged_backlot_server + "/?static=1", wait_until="networkidle")
            href = page.locator("a.lib-card-link").first.get_attribute("href")
            assert href and "static=1" in href

            response = page.goto(
                staged_backlot_server + "/p/..%2FAGENT_GUIDE.md?static=1",
                wait_until="networkidle",
            )
            assert response and response.status == 200
            assert "PROJECT NOT FOUND" in page.locator("body").inner_text()

            page.goto(staged_backlot_server + "/p/the-last-lighthouse?static=1", wait_until="networkidle")
            page.wait_for_timeout(300)
            assert page.locator(".takes .tk.active").count() >= 1
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("project_id", "_pipeline_type", "stage", "artifact_name", "visible_text"),
    APPROVAL_CASES,
)
def test_every_canonical_gate_promotes_its_artifact_before_approval(
    staged_backlot_server,
    project_id,
    _pipeline_type,
    stage,
    artifact_name,
    visible_text,
):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(
                staged_backlot_server + f"/p/{project_id}?static=1",
                wait_until="networkidle",
            )
            review = page.locator(f'.approval-review[data-stage="{stage}"]')
            assert review.is_visible()
            assert review.get_by_text("PENDING APPROVAL", exact=True).is_visible()
            assert "[object Object]" not in review.inner_text()
            artifact = review.locator(f'[data-artifact="{artifact_name}"]')
            assert artifact.is_visible()
            assert visible_text in artifact.inner_text()

            review.get_by_role("button", name="OPEN FULL ARTIFACT").click()
            assert page.locator(".drawer").is_visible()
            assert visible_text in page.locator(".drawer").inner_text()
        finally:
            browser.close()


def test_script_gate_keeps_script_visible_and_marks_pending_approval(staged_backlot_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(staged_backlot_server + "/p/gate-script?static=1", wait_until="networkidle")
            assert page.locator(".script-card").is_visible()
            assert page.locator(".script-pending").inner_text() == "PENDING APPROVAL"
        finally:
            browser.close()


def test_manifest_declared_custom_gate_uses_generic_review_fallback(staged_backlot_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(
                staged_backlot_server + "/p/gate-character-design?static=1",
                wait_until="networkidle",
            )
            review = page.locator('.approval-review[data-stage="character_design"]')
            assert review.is_visible()
            artifact = review.locator('[data-artifact="character_design"]')
            assert artifact.is_visible()
            assert "Ada" in artifact.inner_text()
            assert "Round explorer" in artifact.inner_text()
        finally:
            browser.close()


def test_avatar_role_library_form_fields_are_visible_when_optional_panel_is_open(staged_backlot_server):
    """Regression: a broad CSS selector previously hid all role text inputs."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto(staged_backlot_server + "/p/avatar-cloud-ready", wait_until="networkidle")
            page.get_by_text("数字人素材", exact=True).click()
            page.get_by_text("可选：通用角色库（跨项目复用）", exact=True).click()
            field = page.get_by_label("角色名称")
            assert field.is_visible()
            assert field.evaluate("element => getComputedStyle(element).pointerEvents") == "auto"
            assert page.get_by_text("出镜图").first.is_visible()
            assert page.get_by_role("button", name="生成每位角色的试片").is_visible()
        finally:
            browser.close()


def test_workbench_theme_switch_is_visible_and_persists_after_reload(staged_backlot_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            page.goto(staged_backlot_server + "/p/avatar-cloud-ready", wait_until="networkidle")
            page.evaluate("localStorage.removeItem('backlot.theme')")
            page.reload(wait_until="networkidle")

            toggle = page.get_by_role("button", name="切换至浅色主题")
            assert toggle.is_visible()
            toggle.click()
            assert page.locator("html").get_attribute("data-theme") == "light"
            assert page.evaluate("localStorage.getItem('backlot.theme')") == "light"
            assert page.locator(".sidebar").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            ) == "rgba(255, 249, 236, 0.88)"

            page.reload(wait_until="networkidle")
            assert page.locator("html").get_attribute("data-theme") == "light"
            assert page.get_by_role("button", name="切换至深色主题").is_visible()
        finally:
            browser.close()
