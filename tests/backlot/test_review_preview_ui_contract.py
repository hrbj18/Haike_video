"""Static UI contract for the no-avatar one-click review preview.

These tests deliberately read source text only. They do not start a browser,
server, model, TTS runtime, network client, or media process.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKBENCH_JS = (ROOT / "backlot" / "ui" / "workbench.js").read_text(encoding="utf-8")
WORKBENCH_HTML = (ROOT / "backlot" / "ui" / "workbench.html").read_text(encoding="utf-8")
WORKBENCH_CSS = (ROOT / "backlot" / "ui" / "workbench.css").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    """Return one top-level JS function body without relying on marker comments."""
    pattern = re.compile(
        rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{(?P<body>.*?)"
        r"(?=\n(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(|\n// REVIEW_PREVIEW_END)",
        flags=re.DOTALL,
    )
    match = pattern.search(WORKBENCH_JS)
    assert match, f"missing top-level JavaScript function: {name}"
    return match.group("body")


def test_frozen_review_preview_http_contract_is_used_exactly() -> None:
    assert 'api(reviewPreviewPreflightPath(planningMode), { method: "GET" })' in WORKBENCH_JS
    assert 'return isAvatarProject(projectState) ? "/automation/avatar-review-preview" : "/automation/review-preview";' in WORKBENCH_JS
    assert 'api(`${reviewPreviewApiRoot()}/jobs`, { method: "POST"' in WORKBENCH_JS
    assert 'api(`${reviewPreviewApiRoot()}/jobs/current`, { method: "GET" })' in WORKBENCH_JS
    assert 'api(`${reviewPreviewApiRoot()}/jobs/${encodeURIComponent(id)}/resume`' in WORKBENCH_JS
    assert '{ method: "POST", body: resumeBody }' in _function_body("resumeReviewPreviewJob")
    start = _function_body("startReviewPreviewJob")
    assert "reviewPreviewStartPayload(selectedPlanningMode, preflight)" in start
    assert "fetchReviewPreviewPreflight({ planningMode: selectedPlanningMode })" in start


def test_rejected_draft_preserves_input_and_exposes_regeneration() -> None:
    assert 'draft.status === "revision_requested"' in WORKBENCH_JS
    assert 'draft.status !== "approved" && draft.status !== "revision_requested"' in WORKBENCH_JS
    assert "renderScriptGeneratorForm(intake, draft)" in WORKBENCH_JS
    assert 'intake.video_title || state.project.title' in WORKBENCH_JS
    assert 'intake.source_text || intake.script_text || intake.idea || intake.brief' in WORKBENCH_JS
    assert 'revisionDraft.review_note || "无"' in WORKBENCH_JS
    assert "已保留视频标题、原输入和审核意见" in WORKBENCH_JS
    assert "按意见重新生成草案" in WORKBENCH_JS


def test_script_draft_generation_has_an_honest_in_flight_feedback_contract() -> None:
    generator = _function_body("renderScriptGeneratorForm")
    feedback = _function_body("updateScriptDraftGenerationFeedback")
    state_transition = _function_body("setScriptDraftGenerationInFlight")
    assert "let scriptDraftGenerationInFlight = false;" in WORKBENCH_JS
    assert 'class: "script-generation-status"' in generator
    assert 'role: "status"' in generator
    assert '"aria-live": "polite"' in generator
    assert '"data-script-generation-status": ""' in generator
    assert '"data-script-generation-control": ""' in generator
    assert "正在请求已配置的文本模型，请勿重复点击" in WORKBENCH_JS
    assert 'control.textContent = scriptDraftGenerationInFlight ? "正在生成草案…"' in feedback
    assert "control.disabled = scriptDraftGenerationInFlight" in feedback
    assert "scriptDraftGenerationStartedAt = Date.now()" in state_transition
    assert "window.setInterval(updateScriptDraftGenerationFeedback, 1000)" in state_transition
    assert "try {" in generator and "finally {" in generator
    assert "setScriptDraftGenerationInFlight(false);" in generator
    assert ".script-generation-status" in WORKBENCH_CSS


def test_script_generation_feedback_survives_sse_driven_form_rerenders() -> None:
    generator = _function_body("renderScriptGeneratorForm")
    assert "let generationInFlight" not in generator
    assert "scriptDraftGenerationInFlight ? null" in generator
    assert 'scriptDraftGenerationInFlight ? "正在生成草案…" : idleSubmitLabel' in generator
    assert "scriptDraftGenerationInFlight ? scriptDraftGenerationStatusText()" in generator


def test_primary_action_is_limited_to_approved_supported_projects() -> None:
    assert 'pipeline_type === "animated-explainer"' in WORKBENCH_JS
    assert 'pipeline_type === "avatar-spokesperson"' in WORKBENCH_JS
    assert '(state.project.script_draft || {}).status !== "approved"' in WORKBENCH_JS
    assert 'if (!supportsReviewPreview() || !approved) return null;' in WORKBENCH_JS
    assert "无数字人口播 · 唯一主操作" in WORKBENCH_JS
    assert "有数字人口播 · 唯一主操作" in WORKBENCH_JS
    assert "生成审核预览" in WORKBENCH_JS
    assert "进入数字人素材（高级）" in WORKBENCH_JS
    assert "RunningHub Standard 24GB" in WORKBENCH_JS
    assert "Whisper 只记录诊断，不覆盖精确帧切点" in WORKBENCH_JS
    assert "不会因低置信度打断流程" in WORKBENCH_JS


def test_preflight_is_truthful_and_covers_all_frozen_fields() -> None:
    for label in (
        "项目类型",
        "脚本审核",
        "脚本哈希",
        "预计句数",
        "预计补画面",
        "冻结音色",
        "本地 TTS",
        "FFmpeg",
        "ffprobe",
        "Pexels",
        "文本 AI / 视觉导演",
        "HyperFrames",
        "视觉策略",
        "声音与确认",
        "阻断项",
        "提醒",
    ):
        assert label in WORKBENCH_JS
    assert "零数字人调用：不调用 RunningHub、DashScope 数字人或其他付费数字人服务。" in WORKBENCH_JS
    assert "预检尚未通过" in WORKBENCH_JS
    assert "不会启动任何媒体任务" in WORKBENCH_JS


def test_visual_planning_selector_defaults_to_ai_and_offers_explicit_rule_mix() -> None:
    assert 'let reviewPreviewPlanningMode = "ai_director";' in WORKBENCH_JS
    selector = _function_body("renderReviewPreviewPlanningModeSelector")
    assert 'value: "ai_director"' in selector
    assert "AI 智能导演（精细，使用配好的文本模型）" in selector
    assert 'value: "rule_mix"' in selector
    assert "规则混合（不调用文本模型）" in selector
    assert "setReviewPreviewPlanningMode(event.target.value)" in selector
    assert "disabled: active" in selector
    assert "reviewPreviewPlanningMode" not in _function_body("resetReviewPreviewForScriptChange")


def test_planning_mode_controls_preflight_url_and_start_payload_behavior() -> None:
    normalize = "function reviewPreviewNormalizePlanningMode(value) {" + _function_body("reviewPreviewNormalizePlanningMode")
    preflight_path = "function reviewPreviewPreflightPath(value = reviewPreviewPlanningMode) {" + _function_body("reviewPreviewPreflightPath")
    start_payload = "function reviewPreviewStartPayload(value = reviewPreviewPlanningMode, preflight = null) {" + _function_body("reviewPreviewStartPayload")
    script = "\n".join(
        (
            normalize,
            preflight_path,
            start_payload,
            "const cases = {",
            "  ai: {path:reviewPreviewPreflightPath('ai_director'), body:reviewPreviewStartPayload('ai_director')},",
            "  rules: {path:reviewPreviewPreflightPath('rule_mix'), body:reviewPreviewStartPayload('rule_mix')},",
            "};",
            "process.stdout.write(JSON.stringify(cases));",
        )
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    cases = json.loads(completed.stdout)
    assert cases["ai"] == {
        "path": "/automation/review-preview/preflight?planning_mode=ai_director",
        "body": {
            "confirmed": True,
            "network_confirmed": True,
            "text_ai_confirmed": True,
            "visual": {"planning_mode": "ai_director"},
        },
    }
    assert cases["rules"] == {
        "path": "/automation/review-preview/preflight?planning_mode=rule_mix",
        "body": {
            "confirmed": True,
            "network_confirmed": True,
            "text_ai_confirmed": False,
            "visual": {"planning_mode": "rule_mix"},
        },
    }


def test_avatar_oom_plus_authorization_is_explicit_in_preflight_url_and_start_payload() -> None:
    normalize = "function reviewPreviewNormalizePlanningMode(value) {" + _function_body("reviewPreviewNormalizePlanningMode")
    preflight_path = "function reviewPreviewPreflightPath(value = reviewPreviewPlanningMode) {" + _function_body("reviewPreviewPreflightPath")
    start_payload = "function reviewPreviewStartPayload(value = reviewPreviewPlanningMode, preflight = null) {" + _function_body("reviewPreviewStartPayload")
    script = "\n".join(
        (
            "let reviewPreviewPlanningMode = 'ai_director';",
            "let avatarMode = true;",
            "function isAvatarProject() { return avatarMode; }",
            normalize,
            preflight_path,
            start_payload,
            "const avatar = {",
            "  path:reviewPreviewPreflightPath('ai_director'),",
            "  body:reviewPreviewStartPayload('ai_director',{budget:{limit_cny:5}}),",
            "};",
            "avatarMode = false;",
            "const plain = {",
            "  path:reviewPreviewPreflightPath('ai_director'),",
            "  body:reviewPreviewStartPayload('ai_director',{budget:{limit_cny:5}}),",
            "};",
            "process.stdout.write(JSON.stringify({avatar,plain}));",
        )
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    cases = json.loads(completed.stdout)
    assert cases["avatar"] == {
        "path": (
            "/automation/avatar-review-preview/preflight?planning_mode=ai_director"
            "&budget_limit_cny=5&allow_plus_on_oom=true"
        ),
        "body": {
            "confirmed": True,
            "budget_limit_cny": 5,
            "allow_plus_on_oom": True,
            "visual": {"planning_mode": "ai_director"},
        },
    }
    assert cases["plain"]["path"] == "/automation/review-preview/preflight?planning_mode=ai_director"
    assert "allow_plus_on_oom" not in cases["plain"]["body"]


def test_ai_blocker_stays_blocked_and_never_auto_downgrades() -> None:
    preflight = _function_body("renderReviewPreviewPreflight")
    assert "reviewPreviewTextAiBlocked(preflight)" in preflight
    assert "preflight.visual_generation_required !== false" in preflight
    assert "当前 AI 模式保持阻断" in preflight
    assert "手动改选“规则混合（不调用文本模型）”" in preflight
    assert "系统不会自动切换" in preflight
    setter = _function_body("setReviewPreviewPlanningMode")
    assert "reviewPreviewPlanningMode = nextMode" in setter
    assert "reviewPreviewIsActive()" in setter
    assert 'reviewPreviewPlanningMode = "rule_mix"' not in WORKBENCH_JS


def test_avatar_start_confirmation_is_single_upfront_paid_and_audio_confirmation() -> None:
    start = _function_body("startReviewPreviewJob")
    assert start.count("这是本次唯一一次启动确认") == 1
    assert "配音完成后不会再次要求试听确认" in start
    assert "Whisper 仅作诊断，精确帧清单会自动连续切割" in start
    assert "只有清单漂移或外部结果异常才会安全暂停" in start
    assert "纳入本次确认，配音后不再暂停" in WORKBENCH_JS
    assert "avatarContract.workflow_id" in start
    assert "avatarContract.workflow_profile" in start
    assert "avatarContract.resolution" in start
    assert "avatarContract.fps" in start
    assert "数字人预检缺少完整的工作流" in start
    preflight = _function_body("renderReviewPreviewPreflight")
    assert "工作流未提供" in preflight
    assert "输出规格未提供" in preflight
    assert "InfiniteTalk 精确帧工作流、448×560、Standard 24GB" in preflight


def test_avatar_single_confirmation_discloses_bounded_oom_recovery_and_budget() -> None:
    start = _function_body("startReviewPreviewJob")
    for required_copy in (
        "每位主持最多 3 次",
        "Standard 24GB 最多 2 次",
        "Plus 48GB 最多 1 次",
        "只有前两次都明确 OOM",
        "结果不明绝不重提",
        "本轮费用硬上限 ¥${Number(((preflight.budget || {}).limit_cny) || 5).toFixed(2)}",
    ):
        assert required_copy in start
    assert "Plus 禁用" not in start


def test_runninghub_ui_freezes_exact_workflow_and_keeps_legacy_route_key_internal() -> None:
    assert 'const RUNNINGHUB_EXACT_WORKFLOW_ID = "2094449979141218305"' in WORKBENCH_JS
    assert 'const RUNNINGHUB_EXACT_WORKFLOW_PROFILE = "infinitetalk_448x560_exact_clock_v2"' in WORKBENCH_JS
    assert 'value: "runninghub_longcat"' in WORKBENCH_JS
    settings = _function_body("saveRunningHubSettings")
    assert "workflow_profile: RUNNINGHUB_EXACT_WORKFLOW_PROFILE" in settings
    assert "workflow_template: RUNNINGHUB_EXACT_WORKFLOW_TEMPLATE" in settings
    cloud_actions = _function_body("renderCloudMultiSpeakerActions")
    assert "readonly" in cloud_actions
    assert "不允许逐段工具覆盖一键生产合同" in cloud_actions


def test_planning_confirmation_and_frozen_task_strategy_are_truthful() -> None:
    start = _function_body("startReviewPreviewJob")
    assert "visual_target_scene_count" in start
    assert "预计补全 ${visualTargetCount} 个场景" in start
    assert "先建分镜，再按冻结脚本映射确认" in start
    assert 'selectedPlanningMode === "ai_director"' in start
    assert "会调用预检中显示的已配置文本模型" in start
    assert "不调用文本模型" in start
    assert "复用现有本地画面，不调用文本模型" in start
    assert "任务不会调用 Pexels、文本模型或 HyperFrames" in start
    assert "reviewPreviewStartPayload(selectedPlanningMode, preflight)" in start
    assert "预检返回的视觉规划方式与当前选择不一致" in start
    render_job = _function_body("renderReviewPreviewJob")
    assert "reviewPreviewFrozenPlanningMode(job)" in render_job
    assert "冻结视觉策略" in render_job
    selector = _function_body("renderReviewPreviewPlanningModeSelector")
    assert "active && frozenMode ? frozenMode : reviewPreviewPlanningMode" in selector
    assert "当前预览继续使用任务卡显示的冻结策略" in selector


def test_capability_copy_understands_available_and_not_required_contracts() -> None:
    capability_copy = "function reviewPreviewCapabilityCopy(value) {" + _function_body("reviewPreviewCapabilityCopy")
    script = "\n".join(
        (
            "function reviewPreviewIssueCopy(value) { return JSON.stringify(value); }",
            capability_copy,
            "process.stdout.write(JSON.stringify({",
            "ffmpeg:reviewPreviewCapabilityCopy({available:true,path:'ffmpeg'}),",
            "ffprobe:reviewPreviewCapabilityCopy({available:false}),",
            "skipped:reviewPreviewCapabilityCopy({available:false,status:'skipped_not_required'}),",
            "}));",
        )
    )
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, encoding="utf-8", timeout=10)
    values = json.loads(completed.stdout)
    assert values["ffmpeg"]["label"] == "可用"
    assert values["ffprobe"]["label"] == "不可用"
    assert values["skipped"] == {"state": "ready", "label": "本次不需要", "detail": "已复用合格本地画面"}


def test_prepared_local_visual_start_payload_does_not_authorize_visual_network() -> None:
    normalize = "function reviewPreviewNormalizePlanningMode(value) {" + _function_body("reviewPreviewNormalizePlanningMode")
    start_payload = "function reviewPreviewStartPayload(value = reviewPreviewPlanningMode, preflight = null) {" + _function_body("reviewPreviewStartPayload")
    script = "\n".join(
        (
            "let reviewPreviewPlanningMode = 'ai_director';",
            normalize,
            start_payload,
            "process.stdout.write(JSON.stringify(reviewPreviewStartPayload('ai_director',{visual_generation_required:false})));",
        )
    )
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, encoding="utf-8", timeout=10)
    payload = json.loads(completed.stdout)
    assert payload == {
        "confirmed": True,
        "network_confirmed": False,
        "text_ai_confirmed": False,
        "visual": {"planning_mode": "ai_director"},
    }


def test_planning_selector_updates_dynamic_state_without_interrupting_player() -> None:
    setter = _function_body("setReviewPreviewPlanningMode")
    assert "updateReviewPreviewPanel()" in setter
    assert "render()" not in setter
    update = _function_body("updateReviewPreviewPanel")
    assert "previousDynamic.replaceWith(nextDynamic)" in update
    assert "syncReviewPreviewPlayer(panel)" in update
    sync = _function_body("syncReviewPreviewPlayer")
    assert "existingMedia && existingMedia.dataset.mediaSource === mediaSource" in sync
    assert "return existingMedia" in sync


def test_default_and_frozen_voice_relationship_is_explicit() -> None:
    assert "preflight.frozen_voice || preflight.voice" in WORKBENCH_JS
    assert "voiceCatalog.default_voice" in WORKBENCH_JS
    assert "当前预览使用 ${frozenName}；新默认 ${defaultName} 将在重新生成后生效。" in WORKBENCH_JS
    assert "当前任务已冻结音色 ${frozenName}" in WORKBENCH_JS
    assert "重新生成审核预览" in WORKBENCH_JS


def test_parent_job_card_covers_progress_gates_failure_resume_and_ready_player() -> None:
    for value in ("queued", "running", "awaiting_human", "failed", "completed"):
        assert value in REVIEW_PREVIEW_STATUSES
    for stage in (
        "preflight",
        "scene_plan",
        "line_plan",
        "narration",
        "audio_timeline",
        "subtitles",
        "visual_plan",
        "visual_generation",
        "audio_sample",
        "full_preview",
        "review_ready",
    ):
        assert f'{stage}:' in WORKBENCH_JS
    for contract_field in ("job.job_id", "job.counts", "job.current", "job.gate", "job.error", "job.safe_resume_point", "job.result"):
        assert contract_field in WORKBENCH_JS
    assert "人工门只会暂停任务，不会自动批准" in WORKBENCH_JS
    assert "从安全点继续" in WORKBENCH_JS
    assert '"data-review-preview-media": ""' in WORKBENCH_JS
    assert "终点只到 preview_ready" in WORKBENCH_JS


def test_runtime_and_failed_slot_messages_are_actionable() -> None:
    for code in (
        "hyperframes_cli_missing",
        "hyperframes_cli_timeout",
        "hyperframes_cli_launch_failed",
        "hyperframes_ffmpeg_missing",
        "hyperframes_node_missing",
        "hyperframes_node_too_old",
        "visual_generation_incomplete",
    ):
        assert f"{code}:" in WORKBENCH_JS
    render_job = _function_body("renderReviewPreviewJob")
    assert "error.preserved_completed_slots" in render_job
    assert "error.retry_failed_slots" in render_job
    assert "已保留 ${preservedVisualSlots} 个成功画面" in render_job
    assert "只重试 ${retryVisualSlots} 个失败画面" in render_job


def test_poll_failure_keeps_old_state_and_schedules_bounded_retry() -> None:
    poll = _function_body("pollReviewPreviewJob")
    catch = poll.split("} catch (error) {", maxsplit=1)[1]
    assert "reviewPreviewCurrentLoaded = true" not in catch
    assert "reviewPreviewJob = null" not in catch
    assert "scheduleReviewPreviewPoll()" in catch
    assert "reviewPreviewPollFailures += 1" in catch
    scheduler = _function_body("scheduleReviewPreviewPoll")
    assert "Math.min(reviewPreviewPollFailures, REVIEW_PREVIEW_POLL_BACKOFF_MS.length - 1)" in scheduler
    assert "REVIEW_PREVIEW_POLL_BACKOFF_MS[fallbackIndex]" in scheduler
    assert "[1200, 2400, 5000, 10000, 15000]" in WORKBENCH_JS


def test_refresh_recovers_current_job_and_active_clicks_are_idempotent() -> None:
    assert "(initialLoad || reviewPreviewScriptChanged) && supportsReviewPreview(nextState) && !reviewPreviewCurrentLoaded" in WORKBENCH_JS
    assert "void pollReviewPreviewJob({ quiet: true })" in WORKBENCH_JS
    assert "REVIEW_PREVIEW_ACTIVE_STATUSES" in WORKBENCH_JS
    assert "if (reviewPreviewIsActive())" in WORKBENCH_JS
    assert "不会重复创建" in WORKBENCH_JS


def test_progress_update_never_replaces_same_source_media_node() -> None:
    sync = _function_body("syncReviewPreviewPlayer")
    assert "existingMedia && existingMedia.dataset.mediaSource === mediaSource" in sync
    assert "return existingMedia" in sync
    update = _function_body("updateReviewPreviewPanel")
    assert "previousDynamic.replaceWith(nextDynamic)" in update
    assert "syncReviewPreviewPlayer(panel)" in update
    assert "panel.replaceWith" not in update
    assert "data-review-preview-dynamic" in WORKBENCH_JS
    assert "data-review-preview-player" in WORKBENCH_JS


def test_failed_resume_requires_retryable_and_safe_point() -> None:
    can_resume = _function_body("reviewPreviewCanResume")
    assert "retryable !== false" in can_resume
    assert "Boolean(safeResumePoint)" in can_resume
    assert "!resultUnknown" in can_resume
    assert '"ambiguous_submission"' in can_resume
    render_job = _function_body("renderReviewPreviewJob")
    assert "reviewPreviewCanResume(job)" in render_job
    assert 'jobStatus === "failed" && !reviewPreviewCanResume(job)' in render_job
    assert "不能自动续跑" in render_job
    poll = _function_body("pollReviewPreviewJob")
    assert "reviewPreviewCanResume(reviewPreviewJob)" in poll
    assert "当前需要人工处理，不能自动续跑" in poll


def test_human_gate_uses_backend_action_and_supports_non_audio_copy() -> None:
    gate_label = _function_body("reviewPreviewGateActionLabel")
    assert "gate.action_label || gate.required_action || gate.reason" in gate_label
    assert "确认后继续" in gate_label
    assert 'confirm_visual_plan: "确认视觉方案后继续"' in WORKBENCH_JS
    assert 'confirm_network_submission: "确认网络任务后继续"' in WORKBENCH_JS


def test_resume_confirmation_payloads_follow_gate_semantics() -> None:
    issue_key = "function reviewPreviewIssueKey(value) {" + _function_body("reviewPreviewIssueKey")
    needs_external = "function reviewPreviewNeedsExternalConfirmation(job) {" + _function_body("reviewPreviewNeedsExternalConfirmation")
    resume_payload = "function reviewPreviewResumePayload(jobId, job = null, externalStateConfirmed = false) {" + _function_body("reviewPreviewResumePayload")
    script = "\n".join(
        (
            issue_key,
            needs_external,
            resume_payload,
            "const cases = {",
            "  audio: reviewPreviewResumePayload('audio-1', {status:'awaiting_human', gate:{stage:'audio_sample'}}),",
            "  ambiguousUnconfirmed: reviewPreviewResumePayload('external-1', {status:'failed', error:{code:'ambiguous_external_operation'}}, false),",
            "  ambiguousConfirmed: reviewPreviewResumePayload('external-1', {status:'failed', error:{code:'ambiguous_external_operation'}}, true),",
            "  ordinaryFailed: reviewPreviewResumePayload('failed-1', {status:'failed', safe_resume_point:'visual_plan'}, true),",
            "};",
            "process.stdout.write(JSON.stringify(cases));",
        )
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    cases = json.loads(completed.stdout)
    assert cases["audio"] == {"job_id": "audio-1", "confirmed": True}
    assert cases["ambiguousUnconfirmed"] == {"job_id": "external-1"}
    assert cases["ambiguousConfirmed"] == {
        "job_id": "external-1",
        "external_state_confirmed": True,
        "safe_to_retry": True,
    }
    assert cases["ordinaryFailed"] == {"job_id": "failed-1"}


def test_resume_ui_requires_explicit_external_confirmation_before_request() -> None:
    render_job = _function_body("renderReviewPreviewJob")
    assert "resumeReviewPreviewJob(job.job_id, job)" in render_job
    assert "核对外部状态并确认安全重试" in render_job
    resume = _function_body("resumeReviewPreviewJob")
    assert "reviewPreviewNeedsExternalConfirmation(job)" in resume
    assert "externalStateConfirmed = window.confirm" in resume
    assert "if (!externalStateConfirmed) return" in resume
    assert "reviewPreviewResumePayload(id, job, externalStateConfirmed)" in resume


def test_task_center_reports_waiting_human_separately_without_full_render() -> None:
    running = _function_body("taskCenterRunning")
    waiting = _function_body("taskCenterWaiting")
    assert "active_count" in running
    assert "waiting_count" not in running
    assert "waiting_count" in waiting
    summary = _function_body("taskCenterSummary")
    button_label = _function_body("taskCenterButtonLabel")
    assert "正在运行" in summary
    assert "项等待人工确认" in summary
    assert "项等待人工确认" in button_label
    poll = _function_body("pollTaskCenter")
    assert "updateTaskCenterButton()" in poll
    assert "updateTaskCenterIsland()" in poll
    assert "render()" not in poll
    update_button = _function_body("updateTaskCenterButton")
    assert "control.textContent = taskCenterButtonLabel()" in update_button
    assert "replaceWith" not in update_button


def test_task_center_waiting_count_behavior() -> None:
    sources = (
        "function taskCenterRunning() {" + _function_body("taskCenterRunning"),
        "function taskCenterWaiting() {" + _function_body("taskCenterWaiting"),
        "function taskCenterSummary() {" + _function_body("taskCenterSummary"),
        "function taskCenterButtonLabel() {" + _function_body("taskCenterButtonLabel"),
    )
    script = "\n".join(
        sources
        + (
            "function snapshot(value) { taskCenter = value; return {running:taskCenterRunning(), summary:taskCenterSummary(), button:taskCenterButtonLabel()}; }",
            "const cases = {",
            "  waitingOnly: snapshot({active_count:0, waiting_count:1, failure_count:0}),",
            "  mixed: snapshot({active_count:2, waiting_count:1, failure_count:0}),",
            "  empty: snapshot({active_count:0, waiting_count:0, failure_count:0}),",
            "};",
            "process.stdout.write(JSON.stringify(cases));",
        )
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    cases = json.loads(completed.stdout)
    assert cases["waitingOnly"] == {
        "running": False,
        "summary": "1 项等待人工确认",
        "button": "任务中心 · 1 项等待人工确认",
    }
    assert cases["mixed"] == {
        "running": True,
        "summary": "正在运行 2 项 · 1 项等待人工确认",
        "button": "任务中心 · 2 项运行 · 1 项等待人工确认",
    }
    assert cases["empty"] == {
        "running": False,
        "summary": "当前没有运行中或等待人工确认的任务",
        "button": "任务中心",
    }


def test_script_changes_reset_state_and_completed_hash_mismatch_is_stale() -> None:
    reset = _function_body("resetReviewPreviewForScriptChange")
    assert "reviewPreviewPreflight = null" in reset
    assert "reviewPreviewJob = null" in reset
    assert "reviewPreviewCurrentLoaded = false" in reset
    mutate = _function_body("mutate")
    assert 'path === "/script-draft" || path === "/script-draft/review"' in mutate
    assert 'path === "/script-draft/content" || path === "/script-draft/reopen"' in mutate
    assert "resetReviewPreviewForScriptChange()" in mutate
    freshness = _function_body("reviewPreviewFreshness")
    assert 'return jobHash === currentHash ? "current" : "stale"' in freshness
    ready = _function_body("reviewPreviewIsReady")
    assert 'reviewPreviewFreshness(job) === "current"' in ready
    assert "历史预览已过期：脚本已变化" in WORKBENCH_JS


def test_script_draft_exposes_sentence_editor_and_versioned_review_actions() -> None:
    editor = _function_body("renderScriptDraftEditor")
    studio = _function_body("renderScriptStudio")
    assert "逐句编辑" in editor
    assert "保存草案修改" in editor
    assert "恢复 AI 原稿" in editor
    assert "expected_revision" in editor
    assert 'mutate("/script-draft/content"' in editor
    assert "重新编辑脚本" in studio
    assert 'mutate("/script-draft/reopen"' in studio
    assert 'expected_revision: draft.revision' in studio
    assert ".script-sentence-row" in WORKBENCH_CSS


def test_organize_script_exposes_faithful_and_light_polish_choices() -> None:
    form = _function_body("renderScriptGeneratorForm")
    assert "忠实整理（推荐）" in form
    assert "轻度润色" in form
    assert "organize_strength" in form
    assert "intake.organize_strength" in form
    assert "intake.script_mode" in WORKBENCH_JS


def test_raw_preflight_and_job_errors_are_mapped_to_actionable_chinese() -> None:
    issue_copy = _function_body("reviewPreviewIssueCopy")
    assert "issue.user_message, issue.message" in issue_copy
    assert "issue.code, issue.type, issue.detail, issue.reason" in issue_copy
    assert "REVIEW_PREVIEW_ISSUE_MESSAGES" in issue_copy
    assert 'ffmpeg_not_found: "未找到 FFmpeg' in WORKBENCH_JS
    assert 'pexels_not_configured: "Pexels 尚未配置' in WORKBENCH_JS
    assert "return item.message || item.detail" not in WORKBENCH_JS


def test_review_preview_block_contains_no_avatar_or_publication_actions() -> None:
    block = "\n".join(
        _function_body(name)
        for name in (
            "startReviewPreviewJob",
            "resumeReviewPreviewJob",
            "pollReviewPreviewJob",
            "renderReviewPreviewJob",
        )
    )
    assert "/avatar" not in block
    assert "/approve" not in block
    assert "/publish" not in block
    assert "/finalize" not in block
    assert "confirm_paid" not in block
    assert "ASR" not in block


def test_background_music_supports_project_upload_progress_and_source_range() -> None:
    assert "/workbench/music/uploads?filename=" in WORKBENCH_JS
    assert 'request.upload.addEventListener("progress"' in WORKBENCH_JS
    assert "data-music-upload-progress" in WORKBENCH_JS
    assert "source_start_seconds: Number(sourceStart.value)" in WORKBENCH_JS
    assert "source_end_seconds: Number(sourceEnd.value)" in WORKBENCH_JS
    for label in (
        "上传本地音乐",
        "当前播放位置设为起点",
        "当前播放位置设为终点",
        "使用整首",
        "试听选区",
    ):
        assert label in WORKBENCH_JS
    for selector in (
        ".music-upload-box",
        ".music-range-grid",
        ".music-range-actions",
        ".music-range-status",
    ):
        assert selector in WORKBENCH_CSS


def test_full_preview_failure_shows_reason_and_retry_action() -> None:
    body = _function_body("renderFullPreviewPanel")
    assert 'preview.status === "failed"' in body
    assert "重试生成全片预览" in body
    assert "合成失败" in body
    assert "preview.error" in body
    assert 'status(isGenerating ? "generating" : isReady ? "completed" : isFailed ? "failed"' in body


def test_desktop_and_narrow_layout_contracts_are_static_only() -> None:
    assert ".review-preview-preflight-grid" in WORKBENCH_CSS
    assert "grid-template-columns: repeat(3, minmax(0,1fr))" in WORKBENCH_CSS
    assert "@media (max-width: 760px)" in WORKBENCH_CSS
    assert ".review-preview-preflight-grid, .review-preview-capabilities { grid-template-columns: 1fr; }" in WORKBENCH_CSS
    assert '<script type="module" src="/ui/workbench.js"></script>' in WORKBENCH_HTML


REVIEW_PREVIEW_STATUSES = {
    match
    for match in re.findall(
        r"\b(idle|queued|running|awaiting_human|failed|cancelled|completed)\b",
        WORKBENCH_JS,
    )
}
