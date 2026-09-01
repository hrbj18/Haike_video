import { el, fmtDuration, mediaURL, subscribe } from "/ui/lib.js";

const RUNNINGHUB_EXACT_WORKFLOW_ID = "2094449979141218305";
const RUNNINGHUB_EXACT_WORKFLOW_PROFILE = "infinitetalk_448x560_exact_clock_v2";
const RUNNINGHUB_EXACT_WORKFLOW_TEMPLATE = "config/runninghub/workflow-2094449979141218305.api.json";

const rawProjectPath = location.pathname.split("/p/")[1] || "";
const projectId = decodeURIComponent(rawProjectPath.split("/")[0]);
const encodedProjectId = encodeURIComponent(projectId);
const app = document.getElementById("app");
const toastNode = document.getElementById("toast");
const assetDialog = document.getElementById("assetDialog");
const assetForm = document.getElementById("assetForm");
const imageDialog = document.getElementById("imageDialog");
const imageForm = document.getElementById("imageForm");
const aiConfigDialog = document.getElementById("aiConfigDialog");
const aiConfigForm = document.getElementById("aiConfigForm");
const aiConfigStatus = document.getElementById("aiConfigStatus");
const aiConfigTest = document.getElementById("aiConfigTest");
const doubaoConfigStatus = document.getElementById("doubaoConfigStatus");
const doubaoConfigTest = document.getElementById("doubaoConfigTest");
const THEME_KEY = "backlot.theme";
let currentTheme = document.documentElement.dataset.theme === "light" ? "light" : "dark";

function applyTheme(theme) {
  currentTheme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = currentTheme;
  try { localStorage.setItem(THEME_KEY, currentTheme); } catch (_) { /* 浏览器禁用本地存储时仍允许本次切换。 */ }
  const themeColor = document.querySelector('meta[name="theme-color"]');
  if (themeColor) themeColor.content = currentTheme === "light" ? "#efe4c9" : "#0b1220";
}

function renderThemeToggle() {
  const nextTheme = currentTheme === "light" ? "dark" : "light";
  const nextLabel = nextTheme === "light" ? "浅色" : "深色";
  return el("button", {
    class: "theme-toggle",
    type: "button",
    title: `切换至${nextLabel}主题`,
    "aria-label": `切换至${nextLabel}主题`,
    "aria-pressed": currentTheme === "light" ? "true" : "false",
    onclick: () => { applyTheme(nextTheme); render(); },
  }, el("span", { class: "theme-toggle-icon", "aria-hidden": "true" }, currentTheme === "light" ? "☀" : "☾"));
}

applyTheme(currentTheme);

// Closing a dialog is never a submission. In particular, a user must be able
// to abandon an empty material-registration form without its required fields
// invoking the browser's validation UI.
document.querySelectorAll("[data-dialog-close]").forEach((control) => {
  control.addEventListener("click", () => {
    const dialog = document.getElementById(control.dataset.dialogClose);
    if (!dialog) return;
    const form = dialog.querySelector("form");
    dialog.close("cancel");
    if (form) form.reset();
  });
});

let state = null;
let voiceCatalog = { provider: { status: "unknown" }, profiles: [], default_voice: null };
let musicCatalog = { version: 1, category: "news", tracks: [], errors: [], policy: null, narration_policy: null, narration_defaults: null };
let musicUploadState = { status: "idle", progress: 0, filename: "", error: "" };
let uploadedMusicTrackId = "";
let avatarRoles = null;
let avatarRolesLoading = false;
let avatarScriptTemplates = null;
let avatarScriptTemplatesLoading = false;
let avatarScriptTemplatePreview = null;
let selectedAvatarScriptTemplateId = "";
let avatarUserScriptPreview = null;
let avatarUserScriptLoading = false;
let avatarUserScriptSubmitting = false;
let avatarUserScriptPasteText = "";
let avatarUserScriptTitle = "";
let avatarUserScriptSpeakerOverrides = {};
let avatarAspectFitChoices = {};
let localWhisperModels = null;
let localWhisperModelsLoading = false;
// A new project starts at the production intake gate. Existing projects can
// still jump into scene review from the overview once a scene plan exists.
let activeView = "overview";
let selectedSceneId = null;
let selectedSegmentId = null;
let toastTimer = null;
let keyframeJob = null;
let keyframeJobSceneId = null;
let keyframeJobTimer = null;
let keyframeJobPollInFlight = false;
let keyframeJobStartedAt = 0;
let keyframeJobElapsed = 0;
let keyframeResultsReady = false;
let visualBatchJob = null;
let visualBatchTimer = null;
let visualBatchPollInFlight = false;
let visualBatchResultsReady = false;
let visualBatchPlan = null;
let visualBatchPlanning = false;
let visualBatchPlanningError = "";
let visualBatchSelection = new Set();
let visualBatchSelectionInitialized = false;
// The asset audit is intentionally an on-demand snapshot.  It may hash media
// files, so normal page refreshes and SSE updates never trigger it implicitly.
let assetLibraryAudit = null;
let assetLibraryAuditLoading = false;
let assetLibraryFilter = "all";
let assetLibrarySearch = "";
let assetLibrarySelection = new Set();
// Keep the workbench compact on a fresh page load. The reviewer can expand
// batch production explicitly when needed.
let visualBatchPanelOpen = false;
// Whole-film audio can include a long sample player.  Keep it folded until a
// reviewer deliberately needs to tune or audition the mix.
let musicPanelOpen = false;
let previewSyncJob = null;
let previewSyncTimer = null;
let previewSyncPollInFlight = false;
// REVIEW_PREVIEW_BEGIN
// The one-click review preview is a durable server-owned parent job. Keep its
// small status card isolated from the rest of the workbench so polling never
// rebuilds an unrelated scene editor or changes avatar workflow state.
let reviewPreviewPreflight = null;
let reviewPreviewJob = null;
let reviewPreviewTimer = null;
let reviewPreviewPollInFlight = false;
let reviewPreviewCurrentLoaded = false;
let reviewPreviewActionInFlight = false;
let reviewPreviewPollFailures = 0;
let reviewPreviewPollWarning = "";
let reviewPreviewScriptIdentityValue = "";
let reviewPreviewPlanningMode = "ai_director";
// REVIEW_PREVIEW_END
// The task centre is intentionally a small polling island. It must never
// force a full review-screen redraw while a user is playing or editing media.
let taskCenter = { active_count: 0, waiting_count: 0, failure_count: 0, completed_count: 0, tasks: [] };
let taskCenterOpen = false;
let taskCenterTimer = null;
let taskCenterPollInFlight = false;
let taskCenterResultsReady = false;
const visualBatchDraft = {
  planningMode: "ai_director",
  // The batch is deliberately explicit: either fill only blank slots, or
  // replace the selected scenes' editable slots.  The recommendation and
  // execution steps must always use this same operation.
  operationMode: "fill_missing",
  profile: "auto",
  mixStrategy: "balanced",
  imageSource: "web_download",
  personPolicy: "balanced",
  candidateLimit: 6,
  contentRules: ["no_presenter_studio", "no_large_text_watermark"],
  searchTheme: "",
  preferredKeywords: "",
  cautiousTopics: "主播、演播室、正面人物肖像、商务会议、人物采访、网红自拍",
  queryOverrides: {},
  copyLayout: true,
  layoutSourceId: "",
};
// Kept in the client only as a resilient display fallback. The server owns
// validation and normalisation against the style-pack manifest, so a stale
// browser can never invent a render branch that the renderer does not know.
const HYPERFRAMES_LAYOUT_VARIANTS = {
  headline_statement: [
    { id: "editorial_headline", name: "编辑标题", motion_variant: "stamp_in" },
    { id: "signal_stack", name: "信号堆叠", motion_variant: "tags_lock" },
  ],
  relationship_map: [
    { id: "radial_map", name: "关系图", motion_variant: "node_bloom" },
    { id: "causal_chain", name: "因果链", motion_variant: "step_through" },
    { id: "convergence", name: "汇聚图", motion_variant: "converge_in" },
  ],
  single_metric: [
    { id: "hero_metric", name: "主数据", motion_variant: "metric_pop" },
    { id: "metric_ledger", name: "数据清单", motion_variant: "ledger_reveal" },
  ],
  comparison: [
    { id: "split_columns", name: "左右对照", motion_variant: "opposing_slide" },
    { id: "stacked_duel", name: "上下对照", motion_variant: "top_bottom_lock" },
    { id: "balance_axis", name: "平衡轴", motion_variant: "axis_balance" },
  ],
  process: [
    { id: "vertical_rail", name: "纵向流程", motion_variant: "rail_build" },
    { id: "zigzag_steps", name: "折线步骤", motion_variant: "zigzag_step" },
  ],
  quote_evidence: [{ id: "claim_evidence", name: "判断与证据", motion_variant: "evidence_stack" }],
  closing_question: [{ id: "question_hold", name: "问题停留", motion_variant: "question_land" }],
};

function hyperframesLayoutChoices(stylePack, recipe) {
  const recipes = Array.isArray(stylePack && stylePack.recipes) ? stylePack.recipes : [];
  const matching = recipes.find((item) => item && item.id === recipe);
  if (matching && Array.isArray(matching.variants) && matching.variants.length) return matching.variants;
  return HYPERFRAMES_LAYOUT_VARIANTS[recipe] || HYPERFRAMES_LAYOUT_VARIANTS.relationship_map;
}

function hyperframesLayoutChoice(stylePack, recipe, requested) {
  const choices = hyperframesLayoutChoices(stylePack, recipe);
  return choices.find((item) => item.id === requested) || choices[0];
}
let reviewPlaybackPositions = {};
// Caption edits are a separate overlay draft. They intentionally update the
// existing player in place instead of rebuilding the review screen and
// interrupting the editor's current playback position.
const subtitleDrafts = new Map();
const reviewCaptionControllers = new Map();
// Caption font sizes use the rendered video canvas as their ruler.  Keep the
// observers separate from playback controllers so they can be released on a
// normal workbench refresh.
const captionSizeObservers = new Map();
let stateFingerprint = "";
let voiceCatalogFingerprint = "";
let musicCatalogFingerprint = "";
let refreshInFlight = null;
let refreshQueued = false;
// Script generation is a synchronous request, but saving its input emits an
// SSE refresh before the model returns. Keep this state above the rendered
// form so that a full workbench redraw cannot make an active request look idle.
let scriptDraftGenerationInFlight = false;
let scriptDraftGenerationStartedAt = 0;
let scriptDraftGenerationTimer = null;
const reviewUiMemory = {
  sideScrollTop: null,
  audioByKey: new Map(),
};
// A fresh page load opens the normal scene-review layout. Focus mode is an
// explicit reviewer choice and never changes the project or render.
let reviewFocusMode = false;

const sourceLabels = {
  human_provided: "人工提供", web_download: "网络下载", project_library: "项目素材库",
  ai_generated: "AI 生成", local_generated: "本地生成", mixed: "混合来源", undecided: "待决定",
  avatar_only: "数字人全屏",
};
const statusLabels = {
  pending: "待审核", approved: "已通过", needs_adjustment: "需调整", blocked: "已阻塞",
  generated: "待逐帧审核", scaffolded: "时间线已生成",
  editable: "可编辑", frozen: "已冻结", planned: "待补素材", ready_to_render: "可预检",
  rendered: "候选已渲染", promoted: "已并入", rolled_back: "已回滚", failed: "失败",
  rendering: "正在局部合成", generating_candidate: "正在生成候选", candidate_ready: "候选可试听",
  current: "已采用", candidate: "候选可试听", candidate_rendered: "片段候选已合成", candidate_failed: "候选失败", applying_candidate: "正在局部合成",
  unknown: "状态未知",
};
const anchorLabels = { first_frame: "首帧", climax_frame: "高潮帧", exit_frame: "出场帧" };

Object.assign(statusLabels, {
  generating: "生成中", completed: "已完成", completed_with_warnings: "完成（有提示）",
  queued: "已排队", running: "进行中", completed_with_failures: "完成（存在失败）", failed: "失败",
  assets_ready: "素材已就绪", assets_ready_with_warnings: "素材就绪（有提示）",
  review_ready: "等待审核", queued: "等待合成", idle: "尚未开始",
  awaiting_narration: "等待旁白", narration_ready: "旁白已就绪", generating_narration: "正在生成旁白",
  awaiting_assets: "等待时长匹配素材", needs_duration_refresh: "素材需按新时长更新",
  ready: "可开始合成", rendering_video: "正在合成视频", needs_refresh: "成片需更新", superseded: "已被新素材替换",
  not_started: "尚未开始", running: "处理中", passed: "已通过", passed_with_warnings: "通过（有提醒）",
  missing: "缺少文件", uploaded: "已上传", media_valid: "媒体有效", media_invalid: "媒体无效",
  asr_passed: "台词通过", asr_failed: "台词不符", assembled: "已进入母版",
  cut_pending_review: "切点待审核", cut_approved: "切点已通过",
  audio_ready: "驱动音频已就绪", cloud_queued: "云端任务已排队", cloud_generating: "云端生成中",
  cloud_failed: "云端生成失败", cloud_generated: "云端视频已就绪",
  uploading: "正在上传", detecting: "正在检查出镜图", submitted: "已提交", downloading: "正在下载",
  succeeded: "已完成", cancelled: "已取消", awaiting_sample_approval: "等待确认试片",
  generating_sample: "正在生成试片", generating_batch: "正在批量生成", not_ready: "尚未准备好",
  sample_generating: "试片生成中",
  timeline_applied: "真实时间线已应用", native_avatar_audio: "数字人原声",
  awaiting_human: "等待人工确认", preview_ready: "审核预览已就绪",
});

function showToast(message, error = false) {
  clearTimeout(toastTimer);
  toastNode.textContent = message;
  toastNode.className = `toast show${error ? " error" : ""}`;
  toastTimer = setTimeout(() => { toastNode.className = "toast"; }, 4200);
}

async function globalApi(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    method: options.method || "GET",
    headers: Object.assign({ "Content-Type": "application/json" }, options.headers || {}),
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "请求失败");
  return payload;
}

function paintAIConfigStatus(config, message = "") {
  aiConfigStatus.className = `ai-config-status${config && config.configured ? " configured" : ""}`;
  aiConfigStatus.textContent = message || (config && config.configured
    ? `已配置 · ${config.api_key_masked || "密钥已保存"} · ${config.model}`
    : "尚未配置文本 AI");
}

function paintDoubaoConfigStatus(config, message = "") {
  doubaoConfigStatus.className = `ai-config-status${config && config.configured ? " configured" : ""}`;
  doubaoConfigStatus.textContent = message || (config && config.configured
    ? `已配置快报主编 · ${config.api_key_masked || "密钥已保存"} · ${config.model}`
    : "豆包未配置，选题、写稿与冷审将使用主模型降级");
}

async function openAIConfig() {
  aiConfigForm.reset();
  paintAIConfigStatus(null, "正在读取配置…");
  paintDoubaoConfigStatus(null, "正在读取配置…");
  aiConfigDialog.showModal();
  try {
    const [config, doubao] = await Promise.all([
      globalApi("/ai-text/config"), globalApi("/ai-text/doubao/config"),
    ]);
    aiConfigForm.elements.base_url.value = config.base_url || "";
    aiConfigForm.elements.model.value = config.model || "gpt-5.6-luna";
    aiConfigForm.elements.api_key.placeholder = config.configured
      ? `已保存 ${config.api_key_masked || "密钥"}，留空不修改`
      : "请输入 API 密钥";
    paintAIConfigStatus(config);
    aiConfigForm.elements.doubao_base_url.value = doubao.base_url || "https://ark.cn-beijing.volces.com/api/v3";
    aiConfigForm.elements.doubao_model.value = doubao.model || "doubao-seed-2-1-pro-260628";
    aiConfigForm.elements.doubao_api_key.placeholder = doubao.configured
      ? `已保存 ${doubao.api_key_masked || "密钥"}，留空不修改`
      : "请输入豆包 API 密钥；留空则由主模型降级完成选题、写稿与冷审";
    paintDoubaoConfigStatus(doubao);
  } catch (error) {
    paintAIConfigStatus(null, error.message || "读取配置失败");
    paintDoubaoConfigStatus(null, error.message || "读取配置失败");
  }
}

function status(value) {
  const name = statusLabels[value] || value || "未知";
  return el("span", { class: `status ${value || ""}` }, name);
}

function clock(seconds) {
  const n = Number(seconds) || 0;
  const m = Math.floor(n / 60);
  const s = Math.floor(n % 60);
  const f = Math.round((n - Math.floor(n)) * ((state && state.settings && state.settings.frame_rate) || 30));
  return `${m}:${String(s).padStart(2, "0")}:${String(f).padStart(2, "0")}`;
}

function sceneName(scene) { return `场景 ${String(scene.order).padStart(2, "0")} · ${scene.title}`; }
function pipelineLabel(pipeline) {
  return {
    "animated-explainer": "无数字人口播",
    "avatar-spokesperson": "有数字人口播",
    hybrid: "混合素材",
    "screen-demo": "屏幕演示",
    cinematic: "电影感叙事",
    animation: "动态图形",
  }[pipeline] || (pipeline === "unknown" ? "未指定流程" : pipeline || "未指定流程");
}
function selectedScene() { return (state && state.scenes.find((scene) => scene.id === selectedSceneId)) || (state && state.scenes[0]) || null; }
function selectedSegment() { return (state && state.segments.find((segment) => segment.id === selectedSegmentId)) || (state && state.segments[0]) || null; }
function assetsById() { return new Map(((state && state.assets) || []).map((asset) => [asset.id, asset])); }
function isLiveAsset(asset) { return Boolean(asset) && (!asset.lifecycle || asset.lifecycle.status !== "trashed"); }
function usagesFor(sceneId) { return ((state && state.usages) || []).filter((usage) => usage.scene_id === sceneId); }
function activeUsage(sceneId) { return usagesFor(sceneId).find((usage) => usage.selected && usage.role === "visual") || usagesFor(sceneId).find((usage) => usage.selected) || null; }
function currentAsset(sceneId) { const usage = activeUsage(sceneId); return usage ? assetsById().get(usage.asset_id) : null; }
function aiVisualCandidate(scene) {
  if (!scene) return null;
  const assets = (state && state.assets) || [];
  const storedId = scene.ai_visual_candidate && scene.ai_visual_candidate.asset_id;
  const stored = storedId ? assets.find((asset) => asset.id === storedId) : null;
  if (stored && stored.source_type === "ai_generated") return stored;
  const reviewTimeline = scene.keyframe_review && Array.isArray(scene.keyframe_review.timeline) ? scene.keyframe_review.timeline : [];
  const reviewed = reviewTimeline.find((item) => item.anchor_kind === "first_frame");
  const reviewedAsset = reviewed ? assets.find((asset) => asset.id === reviewed.asset_id) : null;
  if (reviewedAsset && reviewedAsset.source_type === "ai_generated") return reviewedAsset;
  return [...assets].reverse().find((asset) => asset.source_type === "ai_generated"
    && asset.generation && asset.generation.scene_id === scene.id && asset.generation.anchor_kind === "first_frame") || null;
}
function sceneNarration(scene) { return (scene && scene.narration) || { status: "idle", versions: [], job: { status: "idle" } }; }
function narrationVersion(scene, versionId) { return sceneNarration(scene).versions.find((item) => item.id === versionId) || null; }
function currentNarration(scene) { return narrationVersion(scene, sceneNarration(scene).current_version_id); }
function candidateNarration(scene) { return narrationVersion(scene, sceneNarration(scene).candidate_version_id); }
function segmentForScene(sceneId) { return (state.segments || []).find((segment) => (segment.scene_ids || []).includes(sceneId)) || null; }
function isAvatarProject(projectState = state) { return Boolean(projectState && projectState.project && projectState.project.pipeline_type === "avatar-spokesperson"); }
function isNoAvatarProject(projectState = state) {
  return Boolean(projectState && projectState.project && projectState.project.pipeline_type === "animated-explainer");
}
function supportsReviewPreview(projectState = state) {
  return isNoAvatarProject(projectState) || isAvatarProject(projectState);
}

// REVIEW_PREVIEW_BEGIN
const REVIEW_PREVIEW_ACTIVE_STATUSES = new Set(["queued", "running", "awaiting_human"]);
const REVIEW_PREVIEW_POLL_BACKOFF_MS = [1200, 2400, 5000, 10000, 15000];
const REVIEW_PREVIEW_STATUS_LABELS = {
  idle: "尚未开始", queued: "已排队", running: "正在生成", awaiting_human: "等待人工确认",
  ambiguous: "外部结果待核对", failed: "生成失败", cancelled: "已取消", completed: "审核预览已就绪",
};
const REVIEW_PREVIEW_STAGE_LABELS = {
  preflight: "可信预检", scene_plan: "建立分镜草案", line_plan: "拆分逐句台账",
  narration: "逐句生成配音", audio_timeline: "建立真实音频时间线", subtitles: "生成字幕",
  visual_plan: "规划主体画面", visual_generation: "生成主体画面", audio_sample: "生成声音样板",
  voice: "生成双主持长配音", avatar_generation: "RunningHub 数字人生成",
  avatar_alignment: "Whisper 台词与切点对齐", avatar_assembly: "切割并合成数字人母版",
  preview_render: "合成全片审核预览", full_preview: "合成全片审核预览", review_ready: "等待人工观看",
};
const REVIEW_PREVIEW_ISSUE_MESSAGES = Object.freeze({
  ffmpeg_not_found: "未找到 FFmpeg。请在安装设置中修复受控媒体运行时，然后重新预检。",
  ffprobe_not_found: "未找到 ffprobe。请在安装设置中修复受控媒体运行时，然后重新预检。",
  tts_unavailable: "本地配音当前不可用。请先在通用配音中心确认服务与音色。",
  voice_not_found: "冻结音色已不存在。请在通用配音中心重新选择可用音色。",
  yaya_missing: "未找到内置音色“雅雅”。请先修复音色安装，系统不会静默改用其他音色。",
  pexels_not_configured: "Pexels 尚未配置。请先完成素材服务配置，再重新预检。",
  text_ai_not_configured: "文本 AI 尚未配置。请选择规则混合策略，或先完成文本模型配置。",
  hyperframes_unavailable: "HyperFrames 当前不可用。请检查本地运行时后重新预检。",
  hyperframes_cli_missing: "本机尚未完成 HyperFrames 本地初始化。请先完成一次初始化，再重新预检。",
  hyperframes_cli_timeout: "HyperFrames 本地启动超时。请关闭残留的 Node 或渲染进程后重新预检。",
  hyperframes_cli_launch_failed: "HyperFrames 本地程序无法启动。请检查本地缓存或安装后重新预检。",
  hyperframes_ffmpeg_missing: "HyperFrames 未找到项目受控的 FFmpeg/ffprobe，请先修复媒体运行时。",
  hyperframes_node_missing: "HyperFrames 未找到 Node.js，请先修复项目本地 Node 运行时。",
  hyperframes_node_too_old: "Node.js 版本过低；HyperFrames 需要 Node.js 22 或更高版本。",
  visual_generation_incomplete: "部分主体画面生成失败；已完成成果会保留，从安全点继续时只重试失败画面。",
  script_not_approved: "脚本尚未通过人工审核。请先完成脚本审核。",
  script_hash_mismatch: "脚本已发生变化。请重新预检并为当前脚本生成审核预览。",
  unsupported_project_type: "当前项目不是无数字人口播项目，不能启动本流程。",
  ambiguous_submission: "上次网络提交结果不明确。为避免重复任务，请人工核对后处理。",
  submission_unknown: "上次网络提交结果不明确。为避免重复任务，请人工核对后处理。",
  result_unknown: "任务结果不明确。为避免重复执行，请人工核对已产生的记录和文件。",
  non_retryable: "当前失败不可自动续跑。请按错误说明人工处理后重新预检。",
  approve_audio_sample: "确认声音样板后继续",
  confirm_audio_sample: "确认声音样板后继续",
  confirm_visual_plan: "确认视觉方案后继续",
  confirm_network_submission: "确认网络任务后继续",
});

function reviewPreviewJobFromPayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  const candidate = payload.job
    || payload.review_preview_pipeline
    || ((payload.automation || {}).review_preview_pipeline)
    || ((payload.automation || {}).review_preview);
  if (candidate && typeof candidate === "object") return candidate;
  return payload.job_id || payload.status ? payload : null;
}

function reviewPreviewPreflightFromPayload(payload) {
  if (!payload || typeof payload !== "object") return null;
  return payload.preflight || payload.review_preview_preflight || payload;
}

function reviewPreviewIsActive(job = reviewPreviewJob) {
  return Boolean(job && REVIEW_PREVIEW_ACTIVE_STATUSES.has(String(job.status || "")));
}

function reviewPreviewIssueKey(value) {
  return String(value || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
}

function reviewPreviewNormalizePlanningMode(value) {
  return String(value || "").trim().toLowerCase() === "rule_mix" ? "rule_mix" : "ai_director";
}

function reviewPreviewPlanningModeCopy(value, detailed = false) {
  const mode = reviewPreviewNormalizePlanningMode(value);
  if (mode === "rule_mix") return detailed ? "规则混合（不调用文本模型）" : "规则混合";
  return detailed ? "AI 智能导演（精细，使用配好的文本模型）" : "AI 智能导演（精细）";
}

function reviewPreviewApiRoot(projectState = state) {
  return isAvatarProject(projectState) ? "/automation/avatar-review-preview" : "/automation/review-preview";
}

function reviewPreviewPreflightPath(value = reviewPreviewPlanningMode) {
  const avatarMode = typeof isAvatarProject === "function" && isAvatarProject();
  const root = avatarMode ? "/automation/avatar-review-preview" : "/automation/review-preview";
  const budget = avatarMode ? "&budget_limit_cny=5&allow_plus_on_oom=true" : "";
  return `${root}/preflight?planning_mode=${encodeURIComponent(reviewPreviewNormalizePlanningMode(value))}${budget}`;
}

function reviewPreviewStartPayload(value = reviewPreviewPlanningMode, preflight = null) {
  const planningMode = reviewPreviewNormalizePlanningMode(value);
  const avatarMode = typeof isAvatarProject === "function" && isAvatarProject();
  if (avatarMode) {
    return {
      confirmed: true,
      budget_limit_cny: Number((((preflight || {}).budget || {}).limit_cny) || 5),
      allow_plus_on_oom: true,
      visual: { planning_mode: planningMode },
    };
  }
  const visualGenerationRequired = !preflight || preflight.visual_generation_required !== false;
  return {
    confirmed: true,
    network_confirmed: visualGenerationRequired,
    text_ai_confirmed: visualGenerationRequired && planningMode === "ai_director",
    visual: { planning_mode: planningMode },
  };
}

function reviewPreviewFrozenPlanningMode(source) {
  if (!source || typeof source !== "object") return "";
  const visualStrategy = source.visual_strategy || source.visual_policy || {};
  const inputs = source.inputs || {};
  const result = source.result || {};
  const candidates = [
    source.planning_mode,
    typeof visualStrategy === "string" ? visualStrategy : visualStrategy.planning_mode || visualStrategy.mode,
    (source.visual || {}).planning_mode,
    (inputs.visual || {}).planning_mode,
    typeof inputs.visual_strategy === "string" ? inputs.visual_strategy : (inputs.visual_strategy || {}).planning_mode,
    (result.visual || {}).planning_mode,
    typeof result.visual_strategy === "string" ? result.visual_strategy : (result.visual_strategy || {}).planning_mode,
  ];
  const value = candidates.map((item) => reviewPreviewIssueKey(item)).find((item) => ["ai_director", "rule_mix"].includes(item));
  return value || "";
}

function reviewPreviewIssueCopy(issue, fallback = "发现未识别问题。请刷新后重试；仍失败请联系管理员并提供任务编号。") {
  if (!issue) return fallback;
  if (typeof issue === "string") {
    if (/[\u3400-\u9fff]/.test(issue)) return issue;
    return REVIEW_PREVIEW_ISSUE_MESSAGES[reviewPreviewIssueKey(issue)] || fallback;
  }
  if (typeof issue !== "object") return fallback;
  for (const value of [issue.user_message, issue.message, issue.label]) {
    if (!value) continue;
    if (/[\u3400-\u9fff]/.test(String(value))) return String(value);
    const mapped = REVIEW_PREVIEW_ISSUE_MESSAGES[reviewPreviewIssueKey(value)];
    if (mapped) return mapped;
  }
  for (const value of [issue.code, issue.type, issue.detail, issue.reason]) {
    if (!value) continue;
    const mapped = REVIEW_PREVIEW_ISSUE_MESSAGES[reviewPreviewIssueKey(value)];
    if (mapped) return mapped;
    if (/[\u3400-\u9fff]/.test(String(value))) return String(value);
  }
  return fallback;
}

function reviewPreviewScriptIdentity(projectState = state) {
  const project = (projectState && projectState.project) || {};
  const draft = project.script_draft || {};
  return JSON.stringify({
    status: draft.status || "",
    created_at: draft.created_at || "",
    approved_at: draft.approved_at || "",
    script_hash: draft.script_hash || project.script_hash || "",
    script: draft.script || null,
  });
}

function reviewPreviewJobScriptHash(job = reviewPreviewJob) {
  if (!job || typeof job !== "object") return "";
  return String(job.script_hash || ((job.inputs || {}).script_hash) || ((job.result || {}).script_hash) || "");
}

function reviewPreviewCurrentScriptHash() {
  const draft = ((state || {}).project || {}).script_draft || {};
  return String((reviewPreviewPreflight || {}).script_hash || draft.script_hash || ((state || {}).project || {}).script_hash || "");
}

function reviewPreviewFreshness(job = reviewPreviewJob) {
  if (!job || job.status !== "completed") return "current";
  const jobHash = reviewPreviewJobScriptHash(job);
  const currentHash = reviewPreviewCurrentScriptHash();
  if (!jobHash || !currentHash) return "unknown";
  return jobHash === currentHash ? "current" : "stale";
}

function resetReviewPreviewForScriptChange() {
  clearTimeout(reviewPreviewTimer);
  reviewPreviewTimer = null;
  reviewPreviewPreflight = null;
  reviewPreviewJob = null;
  reviewPreviewCurrentLoaded = false;
  reviewPreviewPollFailures = 0;
  reviewPreviewPollWarning = "";
}

function reviewPreviewVoice(source) {
  if (!source || typeof source !== "object") return null;
  return source.frozen_voice
    || source.voice
    || ((source.inputs || {}).voice)
    || ((source.result || {}).voice)
    || null;
}

function reviewPreviewVoiceName(voice) {
  if (!voice) return "尚未冻结";
  if (typeof voice === "string") return voice;
  return voice.display_name || voice.name || voice.profile_name || voice.label || voice.profile_id || voice.id || "尚未冻结";
}

function reviewPreviewVoiceCopy(voice) {
  const name = reviewPreviewVoiceName(voice);
  if (!voice || typeof voice === "string") return name;
  const engine = voice.engine || voice.default_engine || voice.provider || "";
  const profileId = voice.profile_id || voice.id || "";
  return [name, engine ? `引擎 ${engine}` : "", profileId ? `编号 ${profileId}` : ""].filter(Boolean).join(" · ");
}

function reviewPreviewList(value) {
  if (!value) return [];
  const items = Array.isArray(value) ? value : [value];
  return items.map((item) => reviewPreviewIssueCopy(item)).filter(Boolean);
}

function reviewPreviewCapability(preflight, keys) {
  const capabilities = (preflight && preflight.capabilities) || {};
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(capabilities, key)) return capabilities[key];
    if (preflight && Object.prototype.hasOwnProperty.call(preflight, key)) return preflight[key];
  }
  return null;
}

function reviewPreviewCapabilityCopy(value) {
  if (value === true) return { state: "ready", label: "可用", detail: "" };
  if (value === false || value == null) return { state: "blocked", label: "不可用", detail: "未通过预检" };
  if (typeof value === "string") {
    const ready = ["ready", "available", "configured", "passed", "ok"].includes(value.toLowerCase());
    return { state: ready ? "ready" : "blocked", label: ready ? "可用" : "不可用", detail: ready ? "" : reviewPreviewIssueCopy(value) };
  }
  const raw = String(value.status || value.state || (value.ready === true ? "ready" : value.ready === false ? "blocked" : "unknown"));
  const normalized = raw.toLowerCase();
  if (["skipped_not_required", "not_required"].includes(normalized)) {
    return { state: "ready", label: "本次不需要", detail: "已复用合格本地画面" };
  }
  const ready = value.ready === true || value.available === true || ["ready", "available", "configured", "passed", "ok"].includes(normalized);
  const detail = ready
    ? (value.user_message || value.message || value.model || value.provider || "")
    : reviewPreviewIssueCopy(value);
  const explicitlyUnavailable = value.ready === false || value.available === false;
  return { state: ready ? "ready" : "blocked", label: ready ? "可用" : (!explicitlyUnavailable && normalized === "unknown" ? "待确认" : "不可用"), detail };
}

function reviewPreviewTextAiBlocked(preflight) {
  if (!preflight || typeof preflight !== "object") return false;
  const capability = reviewPreviewCapabilityCopy(reviewPreviewCapability(preflight, ["text_ai", "visual_model", "ai_visual_director"]));
  if (capability.state !== "ready") return true;
  return (Array.isArray(preflight.blockers) ? preflight.blockers : [preflight.blockers]).filter(Boolean).some((item) => {
    if (typeof item === "string") return ["text_ai_not_configured", "text_model_not_configured"].includes(reviewPreviewIssueKey(item));
    return [item.code, item.type, item.detail, item.reason].some((value) => ["text_ai_not_configured", "text_model_not_configured"].includes(reviewPreviewIssueKey(value)));
  });
}

function reviewPreviewStrategyCopy(value, fallback) {
  if (!value) return fallback;
  const raw = typeof value === "string" ? value : (value.planning_mode || value.mode || value.strategy || "");
  const localized = { ai_director: "AI 视觉导演", rule_mix: "规则混合", disabled: "关闭", off: "关闭" }[String(raw).toLowerCase()];
  if (localized) return localized;
  if (typeof value === "string") return /[\u3400-\u9fff]/.test(value) ? value : fallback;
  return value.label || value.name || value.detail || fallback;
}

function setReviewPreviewPlanningMode(value) {
  if (reviewPreviewIsActive()) return;
  const nextMode = reviewPreviewNormalizePlanningMode(value);
  if (nextMode === reviewPreviewPlanningMode && reviewPreviewPreflight) return;
  reviewPreviewPlanningMode = nextMode;
  reviewPreviewPreflight = null;
  reviewPreviewPollWarning = "";
  updateReviewPreviewPanel();
  void fetchReviewPreviewPreflight({ planningMode: nextMode }).catch(() => {
    reviewPreviewPollWarning = "视觉规划预检暂时无法更新，请稍后重试；系统不会自动切换规划方式。";
    updateReviewPreviewPanel();
  });
}

function renderReviewPreviewPlanningModeSelector(active, completed) {
  const frozenMode = reviewPreviewFrozenPlanningMode(reviewPreviewJob);
  const displayedMode = active && frozenMode ? frozenMode : reviewPreviewPlanningMode;
  const mode = el("select", {
    disabled: active ? "" : null,
    onchange: (event) => setReviewPreviewPlanningMode(event.target.value),
    "aria-label": "视觉规划方式",
  },
  el("option", { value: "ai_director" }, "AI 智能导演（精细，使用配好的文本模型）"),
  el("option", { value: "rule_mix" }, "规则混合（不调用文本模型）"));
  mode.value = displayedMode;
  const label = active ? "当前任务冻结的视觉规划方式" : completed ? "下一次重新生成的视觉规划方式" : "视觉规划方式";
  const note = active
    ? "任务启动后策略已冻结；选择器不会改写当前任务。"
    : completed
      ? "当前预览继续使用任务卡显示的冻结策略；这里仅设置下一次重新生成。"
      : displayedMode === "ai_director"
        ? "默认精细路线，会调用预检中显示的已配置文本模型。"
        : "确定性规则路线，不调用文本模型。";
  return el("label", { class: "review-preview-planning-selector" }, el("strong", {}, label), mode, el("span", {}, note));
}

function reviewPreviewStatusBadge(value, label) {
  return el("span", { class: `status ${value || "unknown"}` }, label || REVIEW_PREVIEW_STATUS_LABELS[value] || value || "状态未知");
}

function reviewPreviewMediaSource(path) {
  const value = String(path || "").trim();
  if (!value) return "";
  if (value.startsWith("/")) return value;
  return mediaURL(projectId, value);
}

function reviewPreviewAvatarVoiceCopy(source) {
  const roles = (source && source.roles) || (((source || {}).frozen_input || {}).roles) || {};
  const values = Object.values(roles).filter((item) => item && typeof item === "object");
  if (!values.length) return "尚未冻结";
  return values.map((item) => `${item.label || item.role || "主持人"}：${item.profile_name || item.profile_id || "未识别音色"}`).join("；");
}

function renderReviewPreviewPreflight(preflight) {
  if (!preflight) return null;
  const blockers = reviewPreviewList(preflight.blockers);
  const warnings = reviewPreviewList(preflight.warnings);
  const ready = preflight.ready === true && blockers.length === 0;
  const frozenVoice = preflight.frozen_voice || preflight.voice || {};
  const avatarMode = isAvatarProject();
  const avatarContract = (preflight.avatar_contract && typeof preflight.avatar_contract === "object") ? preflight.avatar_contract : {};
  const avatarContractCopy = [
    avatarContract.provider,
    avatarContract.workflow_id ? `工作流 ${avatarContract.workflow_id}` : "工作流未提供",
    avatarContract.workflow_profile || "配置档未提供",
    avatarContract.resolution && avatarContract.fps ? `${avatarContract.resolution} · ${avatarContract.fps}FPS` : "输出规格未提供",
    avatarContract.instance_label || "实例规格未提供",
    "两位主持串行",
  ].filter(Boolean).join(" · ");
  const capabilityRows = [
    ["本地 TTS", ["tts", "local_tts"]],
    ["FFmpeg", ["ffmpeg"]],
    ["ffprobe", ["ffprobe"]],
    ["Pexels", ["pexels"]],
    ["文本 AI / 视觉导演", ["text_ai", "visual_model", "ai_visual_director"]],
    ["HyperFrames", ["hyperframes"]],
    ...(avatarMode ? [["本地 Whisper", ["asr"]], ["RunningHub 数字人", ["avatar"]]] : []),
  ].map(([label, keys]) => {
    const copy = reviewPreviewCapabilityCopy(reviewPreviewCapability(preflight, keys));
    return el("div", { class: `review-preview-capability ${copy.state}` },
      el("strong", {}, label), el("span", {}, copy.label), copy.detail ? el("small", {}, copy.detail) : null,
    );
  });
  const visualStrategy = reviewPreviewStrategyCopy(preflight.visual_strategy || preflight.visual_policy, "等待后端给出视觉策略");
  const preflightPlanningMode = reviewPreviewFrozenPlanningMode(preflight) || reviewPreviewPlanningMode;
  const textAiBlocked = preflight.visual_generation_required !== false
    && preflightPlanningMode === "ai_director"
    && reviewPreviewTextAiBlocked(preflight);
  const musicContract = preflight.music_gate || preflight.music_contract || preflight.music_strategy || preflight.music_policy || {};
  const musicStrategy = musicContract.reason || reviewPreviewStrategyCopy(musicContract, "背景音乐关闭");
  const pausesForSample = preflight.will_pause_for_audio_sample === true
    || (typeof musicContract === "object" && (musicContract.will_pause_for_audio_sample === true || musicContract.requires_human_gate === true));
  const visualTargetCount = Number(preflight.visual_target_scene_count || 0);
  const visualTargetCopy = preflight.visual_generation_required === false
    ? "0 个场景；复用现有合格画面"
    : preflight.visual_scope_pending_scene_plan === true
      ? `${visualTargetCount} 个场景；先建分镜，再按冻结脚本映射确认`
      : `${visualTargetCount} 个场景；只补缺失主体画面`;
  return el("section", { class: `review-preview-preflight ${ready ? "is-ready" : "is-blocked"}`, "aria-label": "审核预览可信预检" },
    el("div", { class: "review-preview-preflight-head" },
      el("div", {}, el("strong", {}, ready ? "预检通过" : "预检尚未通过"), el("span", {}, ready ? "启动后会冻结本次输入和音色" : "不会启动任何媒体任务")),
      status(ready ? "passed" : "blocked"),
    ),
    el("dl", { class: "review-preview-preflight-grid" },
      el("div", {}, el("dt", {}, "项目类型"), el("dd", {}, pipelineLabel(preflight.project_type || (state.project || {}).pipeline_type))),
      el("div", {}, el("dt", {}, "脚本审核"), el("dd", {}, preflight.script_review_status || preflight.script_status || (state.project.script_draft || {}).status || "未知")),
      el("div", {}, el("dt", {}, "脚本哈希"), el("dd", { class: "review-preview-hash" }, preflight.script_hash || "尚未提供")),
      el("div", {}, el("dt", {}, "预计句数"), el("dd", {}, `${Number(preflight.line_count || 0)} 句`)),
      el("div", {}, el("dt", {}, "预计补画面"), el("dd", {}, visualTargetCopy)),
      el("div", {}, el("dt", {}, avatarMode ? "冻结双主持音色" : "冻结音色"), el("dd", {}, avatarMode ? reviewPreviewAvatarVoiceCopy(preflight) : reviewPreviewVoiceCopy(frozenVoice))),
      el("div", {}, el("dt", {}, "视觉策略"), el("dd", {}, visualStrategy)),
      el("div", {}, el("dt", {}, "声音与确认"), el("dd", {}, `${musicStrategy}${pausesForSample ? "；会暂停等待声音样板试听" : "；纳入本次确认，配音后不再暂停"}`)),
      avatarMode ? el("div", {}, el("dt", {}, "数字人合同"), el("dd", {}, avatarContractCopy)) : null,
      avatarMode ? el("div", {}, el("dt", {}, "费用与自动恢复"), el("dd", {}, `本次最多 ¥${Number(((preflight.budget || {}).limit_cny) || 5).toFixed(2)}；每位主持最多 3 次：Standard 24GB 最多 2 次，只有前两次都明确 OOM 才使用 1 次 Plus 48GB`)) : null,
    ),
    el("div", { class: "review-preview-capabilities" }, capabilityRows),
    el("p", { class: "review-preview-zero-avatar" }, avatarMode
      ? "付费边界：只允许预检中冻结的 InfiniteTalk 精确帧工作流、448×560、Standard 24GB；两位主持严格串行，结果不明绝不重提。Standard 明确 OOM 才有限自动恢复，第三次才可能使用 Plus 48GB；本地 Whisper 只记录诊断，不覆盖精确帧切点，也不会因低置信度打断流程。"
      : "零数字人调用：不调用 RunningHub、DashScope 数字人或其他付费数字人服务。"),
    blockers.length ? el("div", { class: "review-preview-message-list bad" }, el("strong", {}, "阻断项"), el("ul", {}, blockers.map((item) => el("li", {}, item)))) : null,
    textAiBlocked ? el("div", { class: "review-preview-message-list warn" }, el("strong", {}, "AI 智能导演需要文本模型"), el("p", {}, "当前 AI 模式保持阻断。你可以先配置文本模型，或手动改选“规则混合（不调用文本模型）”；系统不会自动切换。")) : null,
    warnings.length ? el("div", { class: "review-preview-message-list warn" }, el("strong", {}, "提醒"), el("ul", {}, warnings.map((item) => el("li", {}, item)))) : null,
  );
}

function reviewPreviewCounts(job) {
  const counts = (job && job.counts) || {};
  return {
    completed: Number(counts.completed ?? counts.done ?? job?.completed_count ?? 0),
    failed: Number(counts.failed ?? job?.failed_count ?? 0),
    total: Number(counts.total ?? job?.total_count ?? 0),
  };
}

function reviewPreviewCurrentCopy(current) {
  if (!current) return "准备下一项";
  if (typeof current === "string") return current;
  const identity = current.label || current.text || current.line_id || current.scene_id || current.item_id || "当前项";
  const index = Number(current.index || current.ordinal || 0);
  const total = Number(current.total || 0);
  return index && total ? `${identity}（${index}/${total}）` : identity;
}

function reviewPreviewElapsed(job) {
  if (Number.isFinite(Number(job && job.elapsed_seconds))) return fmtDuration(Number(job.elapsed_seconds));
  const started = Date.parse(String((job && (job.started_at || job.created_at)) || ""));
  if (!Number.isFinite(started)) return "—";
  return fmtDuration(Math.max(0, (Date.now() - started) / 1000));
}

function reviewPreviewResultMedia(job) {
  const result = (job && job.result) || {};
  const gate = (job && job.gate) || {};
  const preview = result.preview || {};
  const gatePreview = gate.preview || {};
  return result.preview_url || result.preview_path || result.output_path || preview.url || preview.path
    || gate.preview_url || gate.preview_path || gate.output_path || gate.media_path || gatePreview.url || gatePreview.path || "";
}

function renderReviewPreviewVoiceRelation(job) {
  if (isAvatarProject()) {
    const voices = reviewPreviewAvatarVoiceCopy(job || reviewPreviewPreflight);
    return voices === "尚未冻结" ? null : el("p", { class: "review-preview-voice-relation" }, `当前任务已冻结 ${voices}；后续修改角色或音色不会改写本次付费任务。`);
  }
  const frozenName = reviewPreviewVoiceName(reviewPreviewVoice(job) || reviewPreviewVoice(reviewPreviewPreflight));
  const defaultName = reviewPreviewVoiceName(voiceCatalog.default_voice);
  if (frozenName === "尚未冻结") return null;
  if (defaultName !== "尚未冻结" && frozenName !== defaultName) {
    return el("p", { class: "review-preview-voice-relation changed" }, `当前预览使用 ${frozenName}；新默认 ${defaultName} 将在重新生成后生效。`);
  }
  return el("p", { class: "review-preview-voice-relation" }, `当前任务已冻结音色 ${frozenName}；配音中心修改默认不会影响本次预览。`);
}

function reviewPreviewGateActionLabel(gate) {
  if (!gate || typeof gate !== "object") return "确认后继续";
  if (reviewPreviewIssueKey(gate.stage) === "ambiguous_external_operation") return "核对外部状态并确认安全重试";
  if (!gate.action_label && !gate.required_action && !gate.reason && reviewPreviewIssueKey(gate.stage) === "audio_sample") return "确认声音样板并继续";
  return reviewPreviewIssueCopy(gate.action_label || gate.required_action || gate.reason, "确认后继续");
}

function reviewPreviewSafePointCopy(value) {
  const key = String(value || "");
  if (!key) return "";
  if (REVIEW_PREVIEW_STAGE_LABELS[key]) return REVIEW_PREVIEW_STAGE_LABELS[key];
  if (/[\u3400-\u9fff]/.test(key)) return key;
  return "已记录的安全恢复点";
}

function reviewPreviewCanResume(job) {
  if (!job || job.status !== "failed" || !job.job_id) return false;
  const error = job.error && typeof job.error === "object" ? job.error : {};
  const retryable = job.retryable ?? error.retryable;
  const safeResumePoint = job.safe_resume_point || error.safe_resume_point || "";
  const outcomeKey = reviewPreviewIssueKey(job.outcome || error.code || error.type || "");
  const resultUnknown = ["ambiguous", "ambiguous_submission", "submission_unknown", "result_unknown"].includes(outcomeKey);
  return retryable !== false && !resultUnknown && Boolean(safeResumePoint);
}

function reviewPreviewNeedsExternalConfirmation(job) {
  if (!job || typeof job !== "object") return false;
  const gate = job.gate && typeof job.gate === "object" ? job.gate : {};
  const error = job.error && typeof job.error === "object" ? job.error : {};
  return [gate.stage, gate.code, gate.type, job.outcome, error.code, error.type]
    .some((value) => reviewPreviewIssueKey(value) === "ambiguous_external_operation");
}

function reviewPreviewResumePayload(jobId, job = null, externalStateConfirmed = false) {
  const body = { job_id: String(jobId || "").trim() };
  const context = job && typeof job === "object" ? job : {};
  const gate = context.gate && typeof context.gate === "object" ? context.gate : {};
  if (context.status === "awaiting_human" && reviewPreviewIssueKey(gate.stage) === "audio_sample") {
    body.confirmed = true;
  }
  if (reviewPreviewNeedsExternalConfirmation(context) && externalStateConfirmed === true) {
    body.external_state_confirmed = true;
    body.safe_to_retry = true;
  }
  return body;
}

function reviewPreviewIsReady(job) {
  if (!job || job.status !== "completed") return false;
  const readiness = ((job.result || {}).readiness || "");
  return (readiness === "preview_ready" || job.stage === "review_ready") && reviewPreviewFreshness(job) === "current";
}

function renderReviewPreviewJob(job) {
  if (!job) return el("div", { class: "review-preview-empty" }, "尚未启动父任务。点击上方主操作后，系统会先执行可信预检。");
  const jobStatus = String(job.status || "idle");
  const counts = reviewPreviewCounts(job);
  const stage = String(job.stage || "preflight");
  const gate = job.gate || {};
  const error = job.error || {};
  const hasError = typeof error === "string" ? Boolean(error.trim()) : Boolean(Object.keys(error).length);
  const errorCopy = hasError ? reviewPreviewIssueCopy(error, "任务失败，但未提供可显示的原因。请人工检查任务记录后处理。") : "";
  const safeResumePoint = job.safe_resume_point || (typeof error === "object" && error.safe_resume_point) || "";
  const preservedVisualSlots = Number((typeof error === "object" && error.preserved_completed_slots) || 0);
  const retryVisualSlots = Number((typeof error === "object" && error.retry_failed_slots) || 0);
  const frozenPlanningMode = reviewPreviewFrozenPlanningMode(job);
  const freshness = reviewPreviewFreshness(job);
  const previewReady = reviewPreviewIsReady(job);
  const jobStatusCopy = freshness === "stale"
    ? "历史预览已过期：脚本已变化"
    : freshness === "unknown" && jobStatus === "completed"
      ? "任务已完成，正在核对脚本版本"
      : jobStatus === "completed" && !previewReady
        ? "任务已完成，等待预览校验"
        : (REVIEW_PREVIEW_STATUS_LABELS[jobStatus] || jobStatus);
  const controls = el("div", { class: "inline-actions review-preview-actions" });
  if (jobStatus === "awaiting_human" && job.job_id) {
    controls.append(button(reviewPreviewGateActionLabel(gate), "primary", () => resumeReviewPreviewJob(job.job_id, job), reviewPreviewActionInFlight));
  } else if (reviewPreviewCanResume(job)) {
    const resumeLabel = reviewPreviewNeedsExternalConfirmation(job) ? "核对外部状态并确认安全重试" : "从安全点继续";
    controls.append(button(resumeLabel, "primary", () => resumeReviewPreviewJob(job.job_id, job), reviewPreviewActionInFlight));
  }
  return el("div", { class: `review-preview-job status-${jobStatus}${freshness === "stale" ? " is-stale" : ""}` },
    el("div", { class: "review-preview-job-head" },
      el("div", {}, el("strong", {}, jobStatusCopy), el("span", {}, `当前阶段：${REVIEW_PREVIEW_STAGE_LABELS[stage] || stage}`)),
      reviewPreviewStatusBadge(previewReady ? "preview_ready" : (freshness === "stale" ? "needs_refresh" : jobStatus), jobStatusCopy),
    ),
    el("div", { class: "review-preview-job-facts" },
      el("span", {}, `任务 ${job.job_id || "待分配"}`),
      el("span", {}, `完成 ${counts.completed}/${counts.total || "—"}`),
      el("span", {}, `失败 ${counts.failed}`),
      el("span", {}, `已用时 ${reviewPreviewElapsed(job)}`),
      frozenPlanningMode ? el("span", {}, `冻结视觉策略：${reviewPreviewPlanningModeCopy(frozenPlanningMode)}`) : null,
    ),
    el("div", { class: "review-preview-progress", role: "progressbar", "aria-label": "审核预览父任务进度", "aria-valuemin": "0", "aria-valuemax": String(counts.total || 1), "aria-valuenow": String(Math.min(counts.total || 1, counts.completed + counts.failed)) },
      el("span", { style: `width:${counts.total ? Math.min(100, Math.round(((counts.completed + counts.failed) / counts.total) * 100)) : 0}%` }),
    ),
    el("p", { class: "review-preview-current" }, `当前：${reviewPreviewCurrentCopy(job.current)}`),
    renderReviewPreviewVoiceRelation(job),
    freshness === "stale" ? el("div", { class: "review-preview-error" }, el("strong", {}, "当前正式脚本与该预览不一致"), el("p", {}, "这是可追溯的历史预览，不会标记为当前 preview_ready。请为当前脚本重新预检并生成。")) : null,
    jobStatus === "awaiting_human" ? el("div", { class: "review-preview-gate" },
      el("strong", {}, "人工门只会暂停任务，不会自动批准"),
      el("p", {}, reviewPreviewIssueCopy(gate.reason || gate.message, "请先检查当前结果，再由你决定是否继续。")),
      gate.required_action ? el("p", { class: "minor" }, `需要操作：${reviewPreviewIssueCopy(gate.required_action, "请按人工门说明完成所需操作")}`) : null,
    ) : null,
    errorCopy ? el("div", { class: "review-preview-error" },
      el("strong", {}, "失败原因"),
      el("p", {}, errorCopy),
      preservedVisualSlots || retryVisualSlots
        ? el("p", {}, `已保留 ${preservedVisualSlots} 个成功画面；继续后只重试 ${retryVisualSlots} 个失败画面。`)
        : null,
      safeResumePoint ? el("p", {}, `已完成成果会保留；安全恢复点：${reviewPreviewSafePointCopy(safeResumePoint)}。`) : el("p", {}, "已完成成果会保留。"),
    ) : null,
    jobStatus === "failed" && !reviewPreviewCanResume(job) ? el("div", { class: "review-preview-manual" }, "当前结果不明、不可重试或没有安全恢复点，不能自动续跑。请人工处理后重新预检。") : null,
    controls.childNodes.length ? controls : null,
  );
}

function reviewPreviewPlayerCopy(job) {
  const freshness = reviewPreviewFreshness(job);
  if (freshness === "stale") return { title: "历史预览播放器", note: "脚本已变化；此媒体仅供追溯，不是当前审核预览。" };
  if (reviewPreviewIsReady(job)) return { title: "审核预览播放器", note: "预览已就绪，等待人工观看；这里不会自动批准片段或进入正式发布。" };
  return { title: "人工检查播放器", note: "请完成当前人工检查后，再使用任务卡中的继续操作。" };
}

function createReviewPreviewMedia(mediaSource) {
  return el(/\.(wav|mp3|m4a|aac|flac)(?:\?|$)/i.test(mediaSource) ? "audio" : "video", {
    controls: "", preload: "metadata", src: mediaSource, "data-review-preview-media": "", "data-media-source": mediaSource,
  });
}

function renderReviewPreviewPlayer(job) {
  const mediaSource = reviewPreviewMediaSource(reviewPreviewResultMedia(job));
  const holder = el("div", { class: "review-preview-player", "data-review-preview-player": "" });
  if (!mediaSource) return holder;
  const copy = reviewPreviewPlayerCopy(job);
  holder.append(el("strong", { "data-review-preview-player-title": "" }, copy.title), createReviewPreviewMedia(mediaSource), el("p", { "data-review-preview-player-note": "" }, copy.note));
  return holder;
}

function syncReviewPreviewPlayer(panel) {
  if (!panel) return null;
  const holder = panel.querySelector("[data-review-preview-player]");
  if (!holder) return null;
  const mediaSource = reviewPreviewMediaSource(reviewPreviewResultMedia(reviewPreviewJob));
  const existingMedia = holder.querySelector("[data-review-preview-media]");
  if (!mediaSource) {
    if (existingMedia) holder.replaceChildren();
    return null;
  }
  const copy = reviewPreviewPlayerCopy(reviewPreviewJob);
  if (existingMedia && existingMedia.dataset.mediaSource === mediaSource) {
    const title = holder.querySelector("[data-review-preview-player-title]");
    const note = holder.querySelector("[data-review-preview-player-note]");
    if (title) title.textContent = copy.title;
    if (note) note.textContent = copy.note;
    return existingMedia;
  }
  const media = createReviewPreviewMedia(mediaSource);
  holder.replaceChildren(el("strong", { "data-review-preview-player-title": "" }, copy.title), media, el("p", { "data-review-preview-player-note": "" }, copy.note));
  return media;
}

function renderReviewPreviewDynamicState() {
  const approved = Boolean((state.project.script_draft || {}).status === "approved");
  if (!supportsReviewPreview() || !approved) return null;
  const avatarMode = isAvatarProject();
  const active = reviewPreviewIsActive();
  const externallyBlocked = reviewPreviewJob && reviewPreviewJob.status === "ambiguous";
  const completed = reviewPreviewJob && reviewPreviewJob.status === "completed";
  const stale = reviewPreviewFreshness(reviewPreviewJob) === "stale";
  const actionLabel = reviewPreviewActionInFlight
    ? "正在检查并启动…"
    : active
      ? "审核预览任务进行中"
      : completed
        ? (stale ? "为当前脚本重新生成审核预览" : "重新生成审核预览")
        : externallyBlocked
          ? "请先核对 RunningHub 外部任务"
          : reviewPreviewPreflight && reviewPreviewPreflight.ready !== true
          ? "重新检查并生成"
          : "生成审核预览";
  return el("div", { "data-review-preview-dynamic": "", "aria-live": "polite" },
    el("div", { class: "panel-head review-preview-panel-head" },
      el("div", {},
        el("p", { class: "eyebrow" }, avatarMode ? "有数字人口播 · 唯一主操作" : "无数字人口播 · 唯一主操作"),
        el("h4", {}, avatarMode ? "一键生成有数字人审核预览" : "一键生成审核预览"),
        el("p", {}, avatarMode
          ? "从已通过双主持脚本生成本地配音、RunningHub Standard 24GB 数字人、精确帧切点、主体画面、字幕和可人工观看的全片预览。"
          : "从已通过脚本建立逐句配音、真实时间线、主体画面、字幕和可人工观看的全片预览。"),
      ),
      button(actionLabel, "primary review-preview-primary", startReviewPreviewJob, active || externallyBlocked || reviewPreviewActionInFlight),
    ),
    el("div", { class: "panel-body review-preview-body" },
      reviewPreviewPollWarning ? el("div", { class: "review-preview-poll-warning" }, reviewPreviewPollWarning) : null,
      renderReviewPreviewPlanningModeSelector(active, Boolean(completed)),
      renderReviewPreviewPreflight(reviewPreviewPreflight),
      renderReviewPreviewJob(reviewPreviewJob),
      el("p", { class: "review-preview-stop-note" }, "终点只到 preview_ready：不自动批准片段、不生成正式发布版、不发布视频。"),
    ),
  );
}

function renderReviewPreviewPanel() {
  const dynamic = renderReviewPreviewDynamicState();
  if (!dynamic) return null;
  return el("section", { class: "panel review-preview-panel", "data-review-preview-panel": "" }, dynamic, renderReviewPreviewPlayer(reviewPreviewJob));
}

function updateReviewPreviewPanel() {
  const panel = document.querySelector("[data-review-preview-panel]");
  if (!panel || !state || !supportsReviewPreview()) return;
  const previousDynamic = panel.querySelector("[data-review-preview-dynamic]");
  const nextDynamic = renderReviewPreviewDynamicState();
  if (previousDynamic && nextDynamic) previousDynamic.replaceWith(nextDynamic);
  syncReviewPreviewPlayer(panel);
}

async function fetchReviewPreviewPreflight({ update = true, planningMode = reviewPreviewPlanningMode } = {}) {
  const payload = await api(reviewPreviewPreflightPath(planningMode), { method: "GET" });
  reviewPreviewPreflight = reviewPreviewPreflightFromPayload(payload);
  if (update) updateReviewPreviewPanel();
  return reviewPreviewPreflight;
}

async function startReviewPreviewJob() {
  if (!supportsReviewPreview() || (state.project.script_draft || {}).status !== "approved") {
    showToast("只有受支持的口播项目且脚本已通过时，才能生成审核预览", true);
    return;
  }
  if (reviewPreviewIsActive()) {
    showToast(`已有任务 ${reviewPreviewJob.job_id || ""} 正在处理，不会重复创建`);
    return;
  }
  reviewPreviewActionInFlight = true;
  updateReviewPreviewPanel();
  try {
    const selectedPlanningMode = reviewPreviewPlanningMode;
    const preflight = await fetchReviewPreviewPreflight({ planningMode: selectedPlanningMode });
    const blockers = reviewPreviewList(preflight && preflight.blockers);
    if (!preflight || preflight.ready !== true || blockers.length) {
      showToast(blockers[0] || "预检未通过；没有启动媒体任务", true);
      return;
    }
    const lineCount = Number(preflight.line_count || 0);
    const avatarContract = (preflight.avatar_contract && typeof preflight.avatar_contract === "object") ? preflight.avatar_contract : {};
    if (isAvatarProject() && (!avatarContract.workflow_id || !avatarContract.workflow_profile || !avatarContract.resolution || !avatarContract.fps || !avatarContract.instance_label)) {
      showToast("数字人预检缺少完整的工作流、配置档、输出尺寸或实例合同；任务没有启动。", true);
      return;
    }
    const frozenPlanningMode = reviewPreviewFrozenPlanningMode(preflight);
    if (frozenPlanningMode && frozenPlanningMode !== selectedPlanningMode) {
      showToast("预检返回的视觉规划方式与当前选择不一致；任务没有启动，请重新预检。", true);
      return;
    }
    const visualStrategy = reviewPreviewPlanningModeCopy(selectedPlanningMode, true);
    const musicContract = preflight.music_gate || preflight.music_contract || preflight.music_strategy || preflight.music_policy || {};
    const musicStrategy = reviewPreviewStrategyCopy(musicContract, "背景音乐关闭");
    const audioGateNature = musicContract.will_pause_for_audio_sample === true || musicContract.required === true
      ? "会先暂停等待声音样板试听"
      : musicContract.trusted_default === true
        ? "内置雅雅默认设置已受信任，不会暂停试听"
        : "无需声音样板暂停";
    const visualGenerationRequired = preflight.visual_generation_required !== false;
    const visualTargetCount = Number(preflight.visual_target_scene_count || 0);
    const visualTargetNature = visualGenerationRequired
      ? `预计补全 ${visualTargetCount} 个场景${preflight.visual_scope_pending_scene_plan === true ? "（先建分镜，再按冻结脚本映射确认）" : ""}`
      : "现有主体画面均已就绪";
    const textModelNature = !visualGenerationRequired
      ? "复用现有本地画面，不调用文本模型"
      : selectedPlanningMode === "ai_director"
        ? "会调用预检中显示的已配置文本模型"
        : "不调用文本模型";
    const visualRuntimeNature = visualGenerationRequired
      ? "任务可能调用 Pexels 和 HyperFrames"
      : "任务不会调用 Pexels、文本模型或 HyperFrames";
    const confirmed = isAvatarProject()
      ? window.confirm(`这是本次唯一一次启动确认。预检已通过：共 ${lineCount} 轮，冻结双主持音色“${reviewPreviewAvatarVoiceCopy(preflight)}”，本地 Whisper“${(preflight.asr || {}).model_id || "已安装模型"}”，数字人合同 RunningHub 工作流 ${avatarContract.workflow_id} / ${avatarContract.workflow_profile} / ${avatarContract.resolution} / ${avatarContract.fps}FPS / ${avatarContract.instance_label}。雅雅、檬檬严格串行；每位主持最多 3 次：Standard 24GB 最多 2 次，Plus 48GB 最多 1 次，只有前两次都明确 OOM 才会升级，结果不明绝不重提。本轮费用硬上限 ¥${Number(((preflight.budget || {}).limit_cny) || 5).toFixed(2)}。声音设置“${musicStrategy}”也会一并冻结，配音完成后不会再次要求试听确认。${visualTargetNature}，视觉规划“${visualStrategy}”（${textModelNature}）。Whisper 仅作诊断，精确帧清单会自动连续切割；只有清单漂移或外部结果异常才会安全暂停。终点仅为待人工观看的审核预览。确认连续执行完整流程吗？`)
      : window.confirm(`预检已通过：预计 ${lineCount} 句，${visualTargetNature}，冻结音色“${reviewPreviewVoiceName(preflight.frozen_voice || preflight.voice)}”，视觉规划“${visualStrategy}”（${textModelNature}），声音策略“${musicStrategy}”（${audioGateNature}）。${visualRuntimeNature}，且绝不调用任何数字人服务；终点仅为待人工观看的审核预览。确认开始吗？`);
    if (!confirmed) return;
    const payload = await api(`${reviewPreviewApiRoot()}/jobs`, { method: "POST", body: reviewPreviewStartPayload(selectedPlanningMode, preflight) });
    reviewPreviewJob = reviewPreviewJobFromPayload(payload);
    if (!reviewPreviewJob) throw new Error("服务器未返回审核预览父任务");
    updateReviewPreviewPanel();
    trackReviewPreviewJob(reviewPreviewJob);
    showToast(reviewPreviewIsActive() ? "审核预览父任务已启动" : "已加载审核预览任务结果");
  } catch (error) {
    showToast(reviewPreviewIssueCopy(error, "审核预览任务启动失败，请检查预检提示后重试。"), true);
  } finally {
    reviewPreviewActionInFlight = false;
    updateReviewPreviewPanel();
  }
}

async function resumeReviewPreviewJob(jobId, job = null) {
  const id = String(jobId || "").trim();
  if (!id || reviewPreviewActionInFlight) return;
  let externalStateConfirmed = false;
  if (reviewPreviewNeedsExternalConfirmation(job)) {
    externalStateConfirmed = window.confirm("外部任务状态不明确。请先在对应服务核对是否已经提交或完成；只有确认不会造成重复执行时，才继续安全重试。确认已完成核对并继续吗？");
    if (!externalStateConfirmed) return;
  }
  const resumeBody = reviewPreviewResumePayload(id, job, externalStateConfirmed);
  reviewPreviewActionInFlight = true;
  updateReviewPreviewPanel();
  try {
    const payload = await api(`${reviewPreviewApiRoot()}/jobs/${encodeURIComponent(id)}/resume`, { method: "POST", body: resumeBody });
    reviewPreviewJob = reviewPreviewJobFromPayload(payload);
    if (!reviewPreviewJob) throw new Error("服务器未返回续跑后的父任务状态");
    updateReviewPreviewPanel();
    trackReviewPreviewJob(reviewPreviewJob);
    showToast("任务已从安全点继续；已完成成果不会重做");
  } catch (error) {
    showToast(reviewPreviewIssueCopy(error, "任务继续失败，请人工检查当前状态后重试。"), true);
  } finally {
    reviewPreviewActionInFlight = false;
    updateReviewPreviewPanel();
  }
}

function scheduleReviewPreviewPoll(delayMs = null) {
  clearTimeout(reviewPreviewTimer);
  const fallbackIndex = Math.min(reviewPreviewPollFailures, REVIEW_PREVIEW_POLL_BACKOFF_MS.length - 1);
  const delay = delayMs == null ? REVIEW_PREVIEW_POLL_BACKOFF_MS[fallbackIndex] : delayMs;
  reviewPreviewTimer = setTimeout(() => { void pollReviewPreviewJob(); }, delay);
}

async function pollReviewPreviewJob({ quiet = false } = {}) {
  if (reviewPreviewPollInFlight || !supportsReviewPreview()) return;
  reviewPreviewPollInFlight = true;
  const previousStatus = reviewPreviewJob && reviewPreviewJob.status;
  try {
    const payload = await api(`${reviewPreviewApiRoot()}/jobs/current`, { method: "GET" });
    reviewPreviewCurrentLoaded = true;
    reviewPreviewJob = reviewPreviewJobFromPayload(payload);
    reviewPreviewPollFailures = 0;
    reviewPreviewPollWarning = "";
    if (reviewPreviewJob?.status === "completed" && !reviewPreviewPreflight) {
      try {
        await fetchReviewPreviewPreflight({ update: false });
      } catch (error) {
        reviewPreviewPollFailures += 1;
        reviewPreviewPollWarning = "任务已恢复，但脚本版本暂时无法核对；系统会自动重试，不会把旧预览标为当前结果。";
      }
    }
    updateReviewPreviewPanel();
    clearTimeout(reviewPreviewTimer);
    reviewPreviewTimer = null;
    if (reviewPreviewIsActive() && reviewPreviewJob.status !== "awaiting_human") scheduleReviewPreviewPoll(1200);
    else if (reviewPreviewJob?.status === "completed" && reviewPreviewFreshness(reviewPreviewJob) === "unknown") scheduleReviewPreviewPoll();
    if (!quiet && previousStatus && previousStatus !== reviewPreviewJob?.status) {
      if (reviewPreviewJob?.status === "awaiting_human") showToast("任务已暂停，等待你完成试听或检查");
      if (reviewPreviewJob?.status === "failed") {
        showToast(reviewPreviewCanResume(reviewPreviewJob)
          ? "审核预览生成失败；已保留成果，可从安全点继续"
          : "审核预览生成失败；当前需要人工处理，不能自动续跑", true);
      }
      if (reviewPreviewJob?.status === "completed") showToast("审核预览已就绪，等待人工观看");
    }
  } catch (error) {
    reviewPreviewPollFailures += 1;
    reviewPreviewPollWarning = "任务状态连接暂时中断，已保留上次进度，正在自动重试。";
    updateReviewPreviewPanel();
    if (!quiet) showToast("审核预览状态暂时无法更新，系统会自动重试", true);
    scheduleReviewPreviewPoll();
  } finally {
    reviewPreviewPollInFlight = false;
  }
}

function trackReviewPreviewJob(job) {
  reviewPreviewJob = job || reviewPreviewJob;
  reviewPreviewPollFailures = 0;
  reviewPreviewPollWarning = "";
  clearTimeout(reviewPreviewTimer);
  if (reviewPreviewIsActive() && reviewPreviewJob.status !== "awaiting_human") {
    scheduleReviewPreviewPoll(350);
  }
}
// REVIEW_PREVIEW_END
function presenterFor(scene) { return (scene && scene.presenter) || { treatment: "hidden" }; }
function presenterTreatmentLabel(treatment) {
  return { fullscreen: "全屏主体", pip_top_left: "左上角解说员", custom: "自定义画中画", hidden: "暂时隐藏" }[treatment] || "暂未设置";
}
function avatarTimelineApplied() { return Boolean(state && state.avatar && state.avatar.status === "timeline_applied"); }
function presenterLayouts() {
  return (state && state.presenter_layouts) || { default_template_id: "pip_top_left", templates: [] };
}
function presenterLayout(scene) {
  const presenter = presenterFor(scene);
  const layouts = presenterLayouts();
  const template = (layouts.templates || []).find((item) => item.id === presenter.layout_template_id)
    || (layouts.templates || []).find((item) => item.id === layouts.default_template_id)
    || { id: "pip_top_left", name: "左上角解说员", geometry: { x: .035, y: .04, width: .29 } };
  return {
    template,
    geometry: presenter.layout_override || template.geometry,
    cropBottom: Number(presenter.crop_bottom ?? template.crop_bottom ?? 0),
    shape: presenter.shape || template.shape || "rounded",
    faceCrop: presenter.face_crop || template.face_crop || { x: .5, y: 0, zoom: 1 },
    customized: Boolean(presenter.layout_override),
  };
}

function reviewPreview(scene) {
  return (scene && scene.review_preview) || { status: "idle", output_path: null, caption_cues: [] };
}

function subtitleStyles() {
  return (state && state.subtitle_styles) || { default_template_id: "subtitle-default", templates: [] };
}

function subtitleState(scene) {
  return (scene && scene.subtitles) || { template_id: "subtitle-default", style_override: {}, cue_overrides: {} };
}

function subtitleStyleFor(scene) {
  const styles = subtitleStyles();
  const subtitle = subtitleState(scene);
  const template = (styles.templates || []).find((item) => item.id === subtitle.template_id)
    || (styles.templates || []).find((item) => item.id === styles.default_template_id)
    || { id: "subtitle-default", name: "标准中文短句字幕", style: {} };
  const base = {
    enabled: true, font: "Microsoft YaHei", font_size: 42, bold: true,
    text_color: "#FFFFFF", outline_color: "#07111F", outline_width: 3,
    background_enabled: false, background_color: "#07111F", background_opacity: 68,
    position: { x: .5, y: .89, width: .84, anchor: "bottom-center" }, max_lines: 2,
  };
  const style = Object.assign({}, base, template.style || {}, subtitle.style_override || {});
  style.position = Object.assign({}, base.position, (template.style || {}).position || {}, (subtitle.style_override || {}).position || {});
  style.template_id = template.id;
  style.template_name = template.name;
  return style;
}

function subtitleDraftFor(scene) {
  const known = subtitleDrafts.get(scene.id);
  if (known) return known;
  const draft = {
    template_id: subtitleState(scene).template_id || "subtitle-default",
    style: structuredClone(subtitleStyleFor(scene)),
    cue_overrides: Object.assign({}, subtitleState(scene).cue_overrides || {}),
  };
  subtitleDrafts.set(scene.id, draft);
  return draft;
}

function captionCueId(index) { return `cue-${String(index + 1).padStart(3, "0")}`; }

function effectiveCaptionCues(scene) {
  const draft = subtitleDrafts.get(scene.id);
  const overrides = (draft && draft.cue_overrides) || subtitleState(scene).cue_overrides || {};
  return (reviewPreview(scene).caption_cues || []).map((cue, index) => {
    const id = cue.id || captionCueId(index);
    return Object.assign({}, cue, { id, text: Object.prototype.hasOwnProperty.call(overrides, id) ? overrides[id] : cue.text });
  });
}

function captionCssColor(color, opacity = 100) {
  const value = /^#[0-9a-f]{6}$/i.test(String(color || "")) ? String(color) : "#07111F";
  if (opacity >= 100) return value;
  const alpha = Math.max(0, Math.min(1, Number(opacity || 0) / 100));
  const red = parseInt(value.slice(1, 3), 16);
  const green = parseInt(value.slice(3, 5), 16);
  const blue = parseInt(value.slice(5, 7), 16);
  return `rgba(${red}, ${green}, ${blue}, ${alpha.toFixed(2)})`;
}

function scaleCaptionFontToCanvas(node) {
  if (!node || !node.parentElement) return;
  const nativeFontSize = Math.max(24, Math.min(80, Number(node.dataset.captionFontSize || 42)));
  const canvas = node.parentElement;
  const width = Number(canvas.clientWidth || 0);
  const height = Number(canvas.clientHeight || 0);
  const shortEdge = Math.min(width, height);
  // Before an editor/player has been attached, it has no measurable canvas.
  // Use the native 1080px reference for this one paint; the scheduled pass
  // below replaces it as soon as layout has a real size.
  const resolved = shortEdge > 0 ? (nativeFontSize * shortEdge) / 1080 : nativeFontSize;
  node.style.fontSize = `${Math.max(1, resolved).toFixed(2)}px`;
}

function observeCaptionCanvas(node) {
  const canvas = node && node.parentElement;
  if (!canvas) return;
  const known = captionSizeObservers.get(node);
  if (!known || known.canvas !== canvas) {
    if (known) known.observer.disconnect();
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(() => scaleCaptionFontToCanvas(node));
      observer.observe(canvas);
      captionSizeObservers.set(node, { canvas, observer });
    }
  }
  requestAnimationFrame(() => scaleCaptionFontToCanvas(node));
}

function releaseCaptionCanvasObservers() {
  for (const { observer } of captionSizeObservers.values()) observer.disconnect();
  captionSizeObservers.clear();
}

function applyCaptionStyle(node, style) {
  const position = style.position || {};
  const anchor = position.anchor || "bottom-center";
  node.hidden = !style.enabled;
  node.style.left = `${Number(position.x ?? .5) * 100}%`;
  node.style.top = `${Number(position.y ?? .89) * 100}%`;
  node.style.bottom = "auto";
  node.style.width = `${Number(position.width ?? .84) * 100}%`;
  // Match ASS \an2 / \an5 / \an8: a bottom anchor places the baseline at
  // the guide, a centre anchor centres it, and a top anchor starts there.
  const verticalShift = anchor === "bottom-center" ? "-100%" : anchor === "top-center" ? "0" : "-50%";
  node.style.transform = `translate(-50%, ${verticalShift})`;
  node.style.fontFamily = style.font || "Microsoft YaHei";
  // ASS uses the output canvas short edge (1080px at 1080x1920) as its font
  // ruler.  Measure that same canvas in JavaScript.  CSS size containment
  // would make this grid item lose its intrinsic video height, so do not use
  // container units here.
  node.dataset.captionFontSize = String(Math.max(24, Math.min(80, Number(style.font_size || 42))));
  scaleCaptionFontToCanvas(node);
  observeCaptionCanvas(node);
  node.style.fontWeight = style.bold ? "700" : "400";
  node.style.color = style.text_color || "#FFFFFF";
  node.style.textShadow = Number(style.outline_width || 0) > 0
    ? `0 0 ${Math.max(1, Number(style.outline_width))}px ${style.outline_color || "#07111F"}, 0 1px ${Math.max(1, Number(style.outline_width))}px ${style.outline_color || "#07111F"}`
    : "none";
  node.style.background = style.background_enabled ? captionCssColor(style.background_color, style.background_opacity) : "transparent";
  node.style.padding = style.background_enabled ? "0.22em 0.48em" : "0";
  node.style.borderRadius = style.background_enabled ? "0.28em" : "0";
  node.style.maxHeight = `${Math.max(1, Number(style.max_lines || 2)) * 1.5}em`;
  node.style.overflow = "hidden";
}

function refreshLiveCaption(scene) {
  const controller = reviewCaptionControllers.get(scene.id);
  if (controller) controller.refresh();
}

function visualPlan(scene) {
  return (scene && scene.visual_plan) || { engine: "openai_image", prompt: "", structured_spec: {}, constraints: [], status: "draft", revision: 0 };
}

function visualTimeline(scene) {
  return (scene && scene.visual_timeline) || { blocks: [], revision: 0 };
}

function sceneHasPresenterMedia(scene) {
  return Boolean(isAvatarProject() && presenterFor(scene).source_path);
}

function sceneHasSupportingVisual(scene) {
  const knownAssets = assetsById();
  const blocks = (visualTimeline(scene).blocks || []).filter((item) => {
    const asset = item && item.asset_id ? knownAssets.get(item.asset_id) : null;
    return item && asset && asset.path && isLiveAsset(asset)
      && item.status !== "failed" && item.status !== "planned" && item.status !== "generating";
  });
  if (!blocks.length) {
    const usage = usagesFor(scene.id).find((item) => item.selected && ["visual", "image", "video"].includes(item.role));
    const asset = usage ? knownAssets.get(usage.asset_id) : null;
    return Boolean(asset && asset.path && isLiveAsset(asset));
  }
  const sorted = [...blocks].sort((a, b) => Number(a.start_seconds || 0) - Number(b.start_seconds || 0));
  const duration = Math.max(.04, Number(scene.end_seconds || 0) - Number(scene.start_seconds || 0));
  return Math.abs(Number(sorted[0].start_seconds || 0)) < .02
    && Math.abs(Number(sorted[sorted.length - 1].end_seconds || 0) - duration) < .02
    && sorted.every((item, index) => !index || Math.abs(Number(sorted[index - 1].end_seconds || 0) - Number(item.start_seconds || 0)) < .02);
}

function sceneIsRenderable(scene) {
  if (!isAvatarProject()) return sceneHasSupportingVisual(scene);
  const treatment = presenterFor(scene).treatment || "hidden";
  if (treatment === "fullscreen") return sceneHasPresenterMedia(scene);
  if (["pip_top_left", "custom"].includes(treatment)) return sceneHasPresenterMedia(scene) && sceneHasSupportingVisual(scene);
  return sceneHasSupportingVisual(scene);
}

function sceneHasFailedVisual(scene) {
  return (visualTimeline(scene).blocks || []).some((item) => item && item.status === "failed");
}

function currentVisualBatch() {
  return visualBatchJob || (((state || {}).automation || {}).visual_batch) || { status: "idle", items: [] };
}

function visualDirectorSummary(item) {
  const ledger = (item || {}).director_ledger || {};
  const attempts = Array.isArray(ledger.attempts) ? ledger.attempts : [];
  const latest = attempts.length ? attempts[attempts.length - 1] : {};
  const decision = (latest || {}).decision || {};
  const count = Array.isArray((latest || {}).candidates) ? latest.candidates.length : 0;
  if (!attempts.length) return "";
  const score = Number(decision.weighted_score || 0);
  const source = decision.decision_source === "project_text_model" ? "项目内模型" : "固定规则回退";
  return `自动导演第 ${attempts.length} 轮预览 ${count} 条候选，${source}${score ? `，综合分 ${score.toFixed(0)}` : ""}${decision.reason ? `：${decision.reason}` : ""}`;
}

function visualBatchFinished(job = currentVisualBatch()) {
  return ["completed", "completed_with_failures", "failed", "cancelled"].includes(String((job || {}).status || ""));
}

function visualBatchRunning() {
  return ["queued", "generating"].includes(currentVisualBatch().status);
}

function currentPreviewSync() {
  return previewSyncJob || (((state || {}).automation || {}).preview_sync) || { status: "idle", items: [] };
}

function previewSyncRunning() {
  return ["queued", "generating"].includes(String(currentPreviewSync().status || ""));
}

function previewSynchronized(scene) {
  const preview = reviewPreview(scene);
  return preview.status === "ready" && Boolean(preview.output_path);
}

async function pollPreviewSync() {
  if (previewSyncPollInFlight) return;
  previewSyncPollInFlight = true;
  try {
    const result = await api("/review-previews/jobs/current", { method: "GET" });
    previewSyncJob = result.generation || { status: "idle", items: [] };
    updatePreviewSyncIsland();
    if (previewSyncRunning()) {
      clearTimeout(previewSyncTimer);
      previewSyncTimer = setTimeout(pollPreviewSync, 1000);
    } else {
      clearTimeout(previewSyncTimer);
      previewSyncTimer = null;
      const completed = Number(previewSyncJob.completed_scenes || 0);
      const failed = Number(previewSyncJob.failed_scenes || 0);
      await refresh({ force: true });
      previewSyncJob = ((state.automation || {}).preview_sync) || previewSyncJob;
      updatePreviewSyncIsland();
      showToast(failed ? `审核预览已同步 ${completed} 个，${failed} 个失败` : `已同步 ${completed} 个审核预览` , Boolean(failed));
    }
  } catch (error) {
    clearTimeout(previewSyncTimer);
    previewSyncTimer = null;
    showToast(error.message || "审核预览同步进度读取失败", true);
  } finally { previewSyncPollInFlight = false; }
}

async function startPreviewSync() {
  if (visualBatchRunning()) {
    const batch = currentVisualBatch();
    const done = Number(batch.completed_slots || 0);
    const total = Number(batch.total_slots || 0);
    return showToast(`主体画面仍在生成（${done}/${total}）。请等待完成后再同步审核预览，避免生成中的片段被判为缺少素材。`, true);
  }
  const missing = (state.scenes || []).filter((scene) => !previewSynchronized(scene)).length;
  if (!missing) return showToast("当前页面中的审核预览均已同步");
  if (!window.confirm(`将按顺序在本机生成 ${missing} 个未同步的审核预览。素材和时间线不会重做，是否继续？`)) return;
  try {
    state = await api("/review-previews/jobs", { method: "POST", body: { confirmed: true, selection_mode: "missing" } });
    previewSyncJob = ((state.automation || {}).preview_sync) || null;
    updatePreviewSyncIsland();
    clearTimeout(previewSyncTimer);
    previewSyncTimer = setTimeout(pollPreviewSync, 350);
    showToast("审核预览正在后台串行同步；当前播放和操作不会被打断");
  } catch (error) { showToast(error.message || "审核预览同步启动失败", true); }
}

async function applySelectedPresenterLayout(layoutSource) {
  const targets = [...visualBatchSelection].filter((sceneId) => sceneId !== layoutSource);
  if (!targets.length) return showToast("请先选择至少一个需要同步的目标片段", true);
  if (previewSyncRunning()) return showToast("审核预览正在同步，请等待完成后再调整数字人样式", true);
  try {
    state = await api("/presenter-layouts/apply-selected", {
      method: "POST",
      body: { source_scene_id: layoutSource, target_scene_ids: targets },
    });
    previewSyncJob = ((state.automation || {}).preview_sync) || null;
    ensureSelection();
    render();
    clearTimeout(previewSyncTimer);
    previewSyncTimer = setTimeout(pollPreviewSync, 350);
    showToast(`已同步 ${targets.length} 个片段的数字人样式；审核预览正在后台刷新`);
  } catch (error) { showToast(error.message || "数字人样式同步启动失败", true); }
}

function updatePreviewSyncIsland() {
  const node = document.querySelector("[data-preview-sync-island]");
  if (!node) return;
  const job = currentPreviewSync();
  const done = Number(job.completed_scenes || 0);
  const failed = Number(job.failed_scenes || 0);
  const total = Number(job.total_scenes || 0);
  const missing = (state.scenes || []).filter((scene) => !previewSynchronized(scene)).length;
  const visualBatchActive = visualBatchRunning();
  const visualBatch = currentVisualBatch();
  node.className = `preview-sync-live ${previewSyncRunning() ? "is-running" : ""}`;
  node.replaceChildren(
    el("div", {},
      el("strong", {}, previewSyncRunning() ? `正在同步审核预览 ${done + failed}/${total}` : missing ? `${missing} 个片段画面未同步到播放器` : "所有审核预览已同步"),
      el("span", {}, previewSyncRunning() ? `当前：${(job.current || {}).scene_id || "准备下一段"}。只更新进度卡，完成后统一加载。` : visualBatchActive ? `主体画面仍在生成 ${Number(visualBatch.completed_slots || 0)}/${Number(visualBatch.total_slots || 0)}；请完成后再同步，避免未落盘的素材导致同步失败。` : failed ? `上轮有 ${failed} 个失败，可重新同步未完成项。` : missing ? "素材已下载不等于播放器已更新；只补本地预览，不会重新下载素材。" : "可直接逐片段播放审核。"),
    ),
    button(previewSyncRunning() ? "正在同步…" : visualBatchActive ? "等待主体画面完成" : missing ? `同步 ${missing} 个未更新预览` : "已全部同步", "primary small", startPreviewSync, previewSyncRunning() || visualBatchActive || !missing),
  );
}

function visualBatchPayload(profile, copyLayout, layoutSource, operationMode = "fill_missing") {
  return {
    selection_mode: "custom",
    scene_ids: [...visualBatchSelection],
    source_mode: "mixed",
    profile,
    operation_mode: operationMode,
    planning_mode: visualBatchDraft.planningMode,
    mix_strategy: visualBatchDraft.mixStrategy,
    image_source: visualBatchDraft.imageSource,
    person_policy: visualBatchDraft.personPolicy,
    candidate_limit: visualBatchDraft.candidateLimit,
    content_rules: [...visualBatchDraft.contentRules],
    search_theme: visualBatchDraft.searchTheme,
    preferred_keywords: visualBatchDraft.preferredKeywords,
    cautious_topics: visualBatchDraft.cautiousTopics,
    query_overrides: visualBatchDraft.queryOverrides,
    copy_presenter_layout: copyLayout,
    layout_source_scene_id: layoutSource,
  };
}

function applyVisualPlanRoute(block, route) {
  const mapping = {
    stock_video: ["video", "web_download"],
    stock_image: ["image", "web_download"],
    ai_image: ["image", "openai_image"],
    hyperframes: ["video", "hyperframes"],
  };
  const selected = mapping[route] || mapping.stock_video;
  block.route = route;
  block.media_kind = selected[0];
  block.source_mode = selected[1];
  block.decision_source = "human_override";
  recalculateVisualBatchPlanMetrics();
}

function recalculateVisualBatchPlanMetrics() {
  if (!visualBatchPlan) return;
  const counts = { stock_video: 0, stock_image: 0, ai_image: 0, hyperframes: 0 };
  const durations = { stock_video: 0, stock_image: 0, ai_image: 0, hyperframes: 0 };
  let previousRoute = "";
  let currentStreak = 0;
  let maxStreak = 0;
  for (const item of visualBatchPlan.items || []) for (const candidate of item.blocks || []) {
    if (candidate.status !== "planned" || !Object.hasOwn(counts, candidate.route)) continue;
    counts[candidate.route] += 1;
    durations[candidate.route] += Math.max(0, Number(candidate.end_seconds || 0) - Number(candidate.start_seconds || 0));
    if (candidate.route === previousRoute) currentStreak += 1;
    else { previousRoute = candidate.route; currentStreak = 1; }
    maxStreak = Math.max(maxStreak, currentStreak);
  }
  const total = Object.values(durations).reduce((sum, value) => sum + value, 0);
  const shares = Object.fromEntries(Object.entries(durations).map(([key, value]) => [key, total ? value / total : 0]));
  visualBatchPlan.route_counts = counts;
  visualBatchPlan.route_duration_seconds = durations;
  visualBatchPlan.duration_shares = shares;
  visualBatchPlan.total_planned_duration_seconds = total;
  visualBatchPlan.stock_video_duration_seconds = durations.stock_video;
  visualBatchPlan.hyperframes_duration_seconds = durations.hyperframes;
  visualBatchPlan.primary_image_duration_seconds = durations.stock_image + durations.ai_image;
  visualBatchPlan.max_route_streak = maxStreak;
  visualBatchPlan.video_slots = counts.stock_video + counts.hyperframes;
  visualBatchPlan.image_slots = counts.stock_image + counts.ai_image;
  visualBatchPlan.ai_image_slots = counts.ai_image;
  visualBatchPlan.hyperframes_slots = counts.hyperframes;
  const envelope = (visualBatchPlan.policy || {}).duration_balance || { stock_video_min: .60, stock_video_max: .70 };
  const stockShare = shares.stock_video || 0;
  const warning = [];
  if ((counts.stock_image + counts.ai_image) > 0) warning.push("当前包含人工指定的静态图片主体画面");
  if ((counts.stock_video + counts.hyperframes) >= 3 && (stockShare < Number(envelope.stock_video_min) || stockShare > Number(envelope.stock_video_max))) warning.push("视频与动态页面的时长占比超出当前预设区间");
  if (maxStreak > 3) warning.push(`同类来源连续 ${maxStreak} 格`);
  visualBatchPlan.balance_status = warning.length ? "warning" : "balanced";
  visualBatchPlan.balance_warning = warning.join("；");
}

function visualBatchOperationLabel(operationMode) {
  return operationMode === "replace_selected" ? "替换所选主体画面" : "补全缺失主体画面";
}

function visualBatchPlanMatches(operationMode) {
  return Boolean(
    visualBatchPlan
    && visualBatchPlan.request
    && visualBatchPlan.request.operationMode === operationMode,
  );
}

function visualBatchPlanHasWork(operationMode) {
  return visualBatchPlanMatches(operationMode) && Number(visualBatchPlan.total_slots || 0) > 0;
}

function visualRecommendationLabel(plan = visualBatchPlan) {
  return plan && plan.planner && plan.planner.mode === "ai_director" ? "AI 推荐" : "规则推荐";
}

async function previewVisualBatch(profile, copyLayout, layoutSource, operationMode = "fill_missing") {
  if (!visualBatchSelection.size) return showToast("请先选择至少一个片段", true);
  if (visualBatchPlanning) return showToast("AI 正在识别画面需求，请等待当前识别完成");
  visualBatchPlanning = true;
  visualBatchPlanningError = "";
  visualBatchPlan = null;
  render();
  showToast(visualBatchDraft.planningMode === "ai_director"
    ? `AI 已开始分析 ${visualBatchSelection.size} 个片段；通常需要 20–90 秒，请勿重复点击`
    : "正在生成规则推荐，请稍候");
  try {
    visualBatchDraft.profile = profile;
    visualBatchDraft.copyLayout = copyLayout;
    visualBatchDraft.layoutSourceId = layoutSource;
    const request = { ...visualBatchPayload(profile, copyLayout, layoutSource, operationMode), ai_planning_confirmed: visualBatchDraft.planningMode === "ai_director" };
    visualBatchPlan = await api("/visual-batch/preview", { method: "POST", body: request });
    visualBatchPlan.request = { profile, copyLayout, layoutSource, operationMode, planningMode: visualBatchDraft.planningMode, mixStrategy: visualBatchDraft.mixStrategy, imageSource: visualBatchDraft.imageSource, personPolicy: visualBatchDraft.personPolicy, candidateLimit: visualBatchDraft.candidateLimit, contentRules: [...visualBatchDraft.contentRules], searchTheme: visualBatchDraft.searchTheme, preferredKeywords: visualBatchDraft.preferredKeywords, cautiousTopics: visualBatchDraft.cautiousTopics };
    const action = visualBatchOperationLabel(operationMode);
    if (!Number(visualBatchPlan.total_slots || 0)) {
      showToast(`没有可${action}的画面格。请在下方切换操作方式后重新识别。`, true);
      return true;
    }
    showToast(`${visualRecommendationLabel()}已准备好：${visualBatchPlan.scene_count} 个片段、${visualBatchPlan.total_slots} 个画面格。确认后点击第二步开始${action}。`);
    return true;
  } catch (error) {
    visualBatchPlanningError = error.message || "批量画面计划生成失败";
    showToast(visualBatchPlanningError, true);
    return false;
  } finally {
    visualBatchPlanning = false;
    render();
  }
}

async function pollVisualBatch() {
  if (visualBatchPollInFlight) return;
  visualBatchPollInFlight = true;
  try {
    const result = await api("/visual-batch/jobs/current", { method: "GET" });
    visualBatchJob = result.generation || { status: "idle", items: [] };
    updateVisualBatchIsland();
    updateVisualBlockJobIslands();
    if (visualBatchRunning()) {
      clearTimeout(visualBatchTimer);
      visualBatchTimer = setTimeout(pollVisualBatch, 1000);
    } else {
      clearTimeout(visualBatchTimer);
      visualBatchTimer = null;
      visualBatchResultsReady = visualBatchFinished(visualBatchJob);
      updateVisualBatchIsland();
      updateVisualBlockJobIslands();
      const done = Number(visualBatchJob.completed_slots || 0);
      const failed = Number(visualBatchJob.failed_slots || 0);
      showToast(failed
        ? `批量画面已完成 ${done}/${Number(visualBatchJob.total_slots || 0)}，${failed} 个失败；请加载结果检查`
        : `批量画面已全部完成：${done}/${Number(visualBatchJob.total_slots || 0)}，请加载结果检查`,
      Boolean(failed));
    }
  } catch (error) {
    clearTimeout(visualBatchTimer);
    visualBatchTimer = null;
    showToast(error.message || "批量画面进度读取失败", true);
  } finally { visualBatchPollInFlight = false; }
}

function trackVisualBatch(job) {
  visualBatchJob = job || currentVisualBatch();
  visualBatchResultsReady = false;
  clearTimeout(visualBatchTimer);
  visualBatchTimer = setTimeout(pollVisualBatch, 350);
  updateVisualBlockJobIslands();
}

async function startVisualBatch(profile, copyLayout, layoutSource, operationMode) {
  if (!visualBatchPlanMatches(operationMode)) {
    showToast("请先完成第一步：AI 识别并给出推荐。", true);
    return;
  }
  if (!visualBatchPlanHasWork(operationMode)) {
    showToast(`当前没有可${visualBatchOperationLabel(operationMode)}的画面格。请切换操作方式后重新识别。`, true);
    return;
  }
  const replacing = operationMode === "replace_selected";
  const aiCount = Number(visualBatchPlan.ai_image_slots || 0);
  const hyperframesCount = Number(visualBatchPlan.hyperframes_slots || 0);
  const personLabel = {relaxed:"宽松",balanced:"平衡",strict:"严格"}[visualBatchPlan.policy.person_policy] || "平衡";
  const totalDuration = Number(visualBatchPlan.total_planned_duration_seconds || 0);
  const stockDuration = Number(visualBatchPlan.stock_video_duration_seconds || 0);
  const motionDuration = Number(visualBatchPlan.hyperframes_duration_seconds || 0);
  const summary = `${replacing ? "将替换所选片段的未锁定主体画面" : "将只补全缺失主体画面"}：共 ${visualBatchPlan.total_slots} 个槽位。网络视频 ${visualBatchPlan.route_counts.stock_video || 0} 格 / ${stockDuration.toFixed(1)} 秒，HyperFrames ${hyperframesCount} 格 / ${motionDuration.toFixed(1)} 秒${aiCount ? `，另有 OpenAI 生图 ${aiCount} 张（可能产生费用）` : ""}，待生成总时长 ${totalDuration.toFixed(1)} 秒。数字人素材和位置不会改变；人物策略为“${personLabel}”；锁定槽位不会覆盖，失败时会暂停该格而不会静默换引擎。是否执行这份已审核计划？`;
  if (!window.confirm(summary)) return;
  try {
    state = await api("/visual-batch/jobs", { method: "POST", body: { ...visualBatchPayload(profile, copyLayout, layoutSource, operationMode), confirmed: true, ai_generation_confirmed: aiCount > 0, reviewed_plan: visualBatchPlan } });
    visualBatchPlan = null;
    trackVisualBatch((state.automation || {}).visual_batch);
    render();
    updateVisualBatchIsland();
    showToast(`${replacing ? "替换" : "补全"}任务已开始；播放器和当前滚动位置不会被刷新`);
  } catch (error) { showToast(error.message || "批量画面任务启动失败", true); }
}

function updateVisualBatchIsland() {
  const node = document.querySelector("[data-visual-batch-island]");
  if (!node) return;
  const job = currentVisualBatch();
  const done = Number(job.completed_slots || 0);
  const failed = Number(job.failed_slots || 0);
  const total = Number(job.total_slots || 0);
  const current = job.current ? `${job.current.scene_id} · ${job.current.block_id}` : "";
  const currentItem = job.current ? (job.items || []).find((item) => item.scene_id === job.current.scene_id && item.block_id === job.current.block_id) : null;
  const candidateProgress = currentItem && currentItem.candidate_limit ? `；候选 ${Number(currentItem.candidate_attempt || 0)}/${Number(currentItem.candidate_limit)}` : "";
  const directorProgress = currentItem ? visualDirectorSummary(currentItem) : "";
  node.className = `visual-batch-live ${visualBatchRunning() ? "is-running" : ""}`;
  const children = [
    el("strong", {}, visualBatchRunning()
      ? `正在串行补全 ${done + failed}/${total}`
      : visualBatchResultsReady
        ? `本轮完成 ${done}/${total}`
        : visualBatchFinished(job)
          ? `本轮结果已加载 ${done}/${total}`
          : "尚未开始批量生成"),
    el("span", {}, visualBatchRunning() ? `当前：${current || "准备下一槽位"}${candidateProgress}。${(currentItem && currentItem.stage) || "准备处理"}；${directorProgress || "任务只更新这张卡，不会打断视频播放。"}` : job.error || (failed ? `${failed} 个槽位在多候选筛选后仍失败，可加载结果查看具体原因。` : total ? "全部槽位已处理，可加载结果检查画面。" : "先选择片段并预览计划。")),
  ];
  if (visualBatchResultsReady) children.push(button("加载批量结果", "primary small", async () => {
      visualBatchResultsReady = false;
      visualBatchJob = null;
      await refresh({ force: true });
      showToast(`已加载本轮 ${done}/${total} 个画面槽位，可逐片段审核`);
    }));
  node.replaceChildren(...children);
}

function updateVisualBlockJobIslands() {
  const job = currentVisualBatch();
  for (const node of document.querySelectorAll("[data-visual-block-job]")) {
    const sceneId = node.dataset.sceneId;
    const blockId = node.dataset.blockId;
    const item = (job.items || []).find((entry) => entry.scene_id === sceneId && entry.block_id === blockId);
    const buttonNode = document.querySelector(`[data-visual-block-refresh-button="${sceneId}|${blockId}"]`);
    if (!item) {
      node.replaceChildren();
      node.hidden = true;
      continue;
    }
    node.hidden = false;
    const itemStatus = String(item.status || job.status || "queued");
    const running = ["queued", "generating"].includes(itemStatus) || (["queued", "generating"].includes(job.status) && itemStatus !== "completed" && itemStatus !== "failed");
    node.className = `visual-block-job-live ${running ? "is-running" : itemStatus === "failed" ? "is-failed" : "is-completed"}`;
    if (buttonNode) {
      buttonNode.disabled = running;
      buttonNode.textContent = running ? (itemStatus === "queued" ? "等待搜索…" : `筛选 ${Number(item.candidate_attempt || 0)}/${Number(item.candidate_limit || 1)}…`) : "只换这格";
    }
    if (running) {
      node.replaceChildren(el("strong", {}, itemStatus === "queued" ? "已进入队列" : (item.stage || "正在自动寻找新素材")), el("span", {}, visualDirectorSummary(item) || `已检查 ${Number(item.candidate_attempt || 0)}/${Number(item.candidate_limit || 1)} 个候选，淘汰 ${(item.rejected_candidates || []).length} 个。完成前不会改动其他画面。`));
    } else if (itemStatus === "completed") {
      const liveScene = (state.scenes || []).find((scene) => scene.id === sceneId);
      const liveBlock = ((liveScene && liveScene.visual_timeline) || {}).blocks?.find((block) => block.id === blockId);
      const alreadyApplied = Boolean(item.asset_id && liveBlock && liveBlock.asset_id === item.asset_id);
      if (alreadyApplied) {
        node.replaceChildren(
          el("strong", {}, `替换完成 · ${item.asset_id}`),
          el("span", {}, visualDirectorSummary(item) || "这格时间线已经采用新素材；左侧仍显示旧画面时，请同步或刷新本段审核预览。"),
        );
      } else {
        node.replaceChildren(
          el("strong", {}, `新素材已就绪 · ${item.asset_id || "已登记"}`),
          el("span", {}, visualDirectorSummary(item) || "原区间、切点和其他画面保持不变。加载后可在左侧播放器审核。"),
          button("加载这格新素材", "primary small", async () => {
            visualBatchResultsReady = false;
            visualBatchJob = null;
            await refresh({ force: true });
            showToast(`${blockId} 的新素材已加载；请同步或刷新本段审核预览`);
          }),
        );
      }
    } else {
      node.replaceChildren(el("strong", {}, "换素材失败"), el("span", {}, item.error || job.error || "未返回具体原因，请重试。"));
    }
  }
}

function visualEngineLabel(engine) {
  return {
    openai_image: "OpenAI 静态图",
    hyperframes: "HyperFrames 动态画面",
    remotion: "Remotion 动态画面",
    ppt_card: "历史 PPT 信息卡",
  }[engine] || "画面生成方式";
}

function renderPptCardBriefEditor(scene, plan) {
  const brief = scene.ppt_card_brief || {};
  const engine = el("select", {},
    el("option", { value: "openai_image" }, "OpenAI 静态图"),
    el("option", { value: "hyperframes" }, "HyperFrames 动态画面"),
    el("option", { value: "remotion" }, "Remotion 动态画面"),
    el("option", { value: "ppt_card", selected: "" }, "PPT 信息卡（本地可编辑素材）"),
  );
  engine.value = "ppt_card";
  const title = el("input", { value: brief.title || "", placeholder: "例如：数字人正在成为商业角色" });
  title.value = brief.title || "";
  const takeaway = el("textarea", { class: "ppt-card-takeaway", placeholder: "用一句话写清本段结论，不要复制画面提示词。" });
  takeaway.value = brief.takeaway || "";
  const source = el("textarea", { class: "ppt-card-source", readOnly: "", value: brief.source_text || "", placeholder: "本段暂无可用于提炼的台词，请先补充台词或手动填写要点。" });
  source.value = brief.source_text || "";
  const type = el("select", {},
    el("option", { value: "headline_metrics" }, "重点标题与要点"),
    el("option", { value: "comparison" }, "双项对比"),
    el("option", { value: "timeline" }, "流程与时间线"),
  );
  type.value = brief.card_type || "headline_metrics";
  const theme = el("select", {},
    el("option", { value: "tech_neon" }, "科技霓虹"),
    el("option", { value: "editorial" }, "编辑简报"),
    el("option", { value: "signal_amber" }, "信号琥珀"),
  );
  theme.value = brief.theme || "tech_neon";
  const items = (Array.isArray(brief.items) && brief.items.length ? brief.items : ["", ""]).slice(0, 4).map((value) => {
    const input = el("input", { value, placeholder: "一条不超过 26 字的关键信息" });
    input.value = value;
    return input;
  });
  while (items.length < 2) items.push(el("input", { placeholder: "一条不超过 26 字的关键信息" }));
  const itemList = el("div", { class: "ppt-card-item-list" });
  const previewTitle = el("strong", {}, title.value || "信息卡标题");
  const previewTakeaway = el("p", {}, takeaway.value || "一句话结论会显示在这里");
  const previewItems = el("ol", {});
  let dirty = false;
  const preview = () => {
    previewTitle.textContent = title.value.trim() || "信息卡标题";
    previewTakeaway.textContent = takeaway.value.trim() || "一句话结论会显示在这里";
    previewItems.replaceChildren(...items.map((input) => el("li", {}, input.value.trim() || "待补充要点")));
  };
  const markDirty = () => { dirty = true; preview(); };
  const rebuildItems = () => {
    itemList.replaceChildren(...items.map((input, index) => {
      input.addEventListener("input", markDirty);
      return el("div", { class: "ppt-card-item-row" }, el("span", {}, String(index + 1)), input,
        items.length > 2 ? button("删除", "secondary small", () => { items.splice(index, 1); rebuildItems(); markDirty(); }) : null,
      );
    }));
    preview();
  };
  rebuildItems();
  [title, takeaway, type, theme].forEach((control) => control.addEventListener("input", markDirty));
  [type, theme].forEach((control) => control.addEventListener("change", markDirty));

  const saveBrief = () => mutate(`/scenes/${encodeURIComponent(scene.id)}/ppt-card-brief`, {
    method: "PUT",
    body: {
      title: title.value,
      takeaway: takeaway.value,
      items: items.map((input) => input.value),
      card_type: type.value,
      theme: theme.value,
    },
  }, "信息卡内容草案已保存；现在可以生成素材");
  const switchEngine = () => mutate(`/scenes/${encodeURIComponent(scene.id)}/visual-plan`, {
    method: "PUT",
    body: {
      engine: engine.value,
      prompt: plan.prompt || "根据本段台词生成主体画面，避免第二主播、文字和水印。",
      structured_spec: plan.structured_spec || {},
      constraints: plan.constraints || [],
    },
  }, `已切换为${visualEngineLabel(engine.value)}`);
  const job = scene.ppt_card_generation || { status: "idle" };
  const generate = button(
    ["queued", "generating", "running"].includes(job.status) ? "正在生成 PPT 信息卡…" : "确认内容并生成 PPT 信息卡",
    "primary",
    async () => {
      if (dirty || brief.status !== "saved") return showToast("请先保存信息卡草案；生成只会使用已保存的标题、结论和要点", true);
      try {
        state = await api(`/scenes/${encodeURIComponent(scene.id)}/ppt-cards/jobs`, { method: "POST", body: { confirmed: true } });
        ensureSelection(); render(); trackTaskCenter();
        showToast("信息卡任务已进入任务中心；生成完成后会登记为新的 S-xxx 素材");
      } catch (error) { showToast(error.message || "信息卡任务提交失败", true); }
    },
    ["queued", "generating", "running"].includes(job.status),
  );
  return el("section", { class: "visual-design ppt-card-editor" },
    el("div", { class: "visual-design-head" }, el("div", {}, el("strong", {}, "PPT 信息卡内容与版式"), el("span", {}, "只使用已确认的短文本；不会把画面提示词、导演指令或第二主播写进画面。")), status(brief.status === "saved" ? "approved" : "pending")),
    el("label", {}, "生成方式", engine),
    el("div", { class: "ppt-card-switch" }, button("切换生成方式", "secondary", switchEngine), el("span", { class: "form-note" }, "切换后不会删除当前信息卡草案。")),
    el("details", { open: "" }, el("summary", {}, "本段台词来源（只读）"), source),
    el("div", { class: "ppt-card-brief-grid" }, el("label", {}, "标题（最多 28 字）", title), el("label", {}, "卡片结构", type), el("label", {}, "视觉主题", theme)),
    el("label", {}, "一句话结论（最多 48 字）", takeaway),
    el("div", { class: "ppt-card-items-head" }, el("strong", {}, "2–4 条关键信息"), items.length < 4 ? button("新增要点", "secondary small", () => { items.push(el("input", { placeholder: "一条不超过 26 字的关键信息" })); rebuildItems(); markDirty(); }) : null),
    itemList,
    el("aside", { class: "ppt-card-live-preview" }, el("span", {}, "版式内容预览"), previewTitle, previewTakeaway, previewItems),
    el("div", { class: "inline-actions" }, button("保存信息卡草案", "primary", saveBrief), generate),
    job.status === "failed" ? el("div", { class: "inline-error" }, `信息卡生成失败：${job.error || "未返回具体原因"}`) : null,
    scene.ppt_card_candidate ? el("div", { class: "keyframe-adoption" },
      el("strong", {}, `PPT 信息卡候选 ${scene.ppt_card_candidate.asset_id}`),
      el("span", {}, "已登记为普通图片素材；采用后仍可在下方时间线中独立拆分、锁定或换图。"),
      button("采用为当前画面", "primary small", () => mutate("/usages", { method: "POST", body: { scene_id: scene.id, asset_id: scene.ppt_card_candidate.asset_id, role: "visual" } }, "已采用信息卡；可继续在时间线调整具体区间")),
    ) : null,
  );
}

function renderVisualDesignPanel(scene) {
  const plan = visualPlan(scene);
  if (plan.engine === "ppt_card") return renderPptCardBriefEditor(scene, plan);
  const engine = el("select", {},
    el("option", { value: "openai_image", selected: plan.engine === "openai_image" ? "" : null }, "OpenAI 静态图"),
    el("option", { value: "hyperframes", selected: plan.engine === "hyperframes" ? "" : null }, "HyperFrames 动态画面"),
    el("option", { value: "remotion", selected: plan.engine === "remotion" ? "" : null }, "Remotion 动态画面"),
  );
  const prompt = el("textarea", { class: "visual-prompt", value: plan.prompt || "", placeholder: "描述本段应该出现的对象、环境、构图和科技感；系统会自动禁止第二主播、文字和水印。" });
  prompt.value = plan.prompt || "";
  const headline = el("input", { value: (plan.structured_spec || {}).headline || scene.title || "", placeholder: "动态画面主题" });
  const centerLabel = el("input", { value: (plan.structured_spec || {}).center_label || "", placeholder: "例如：商业价值" });
  const components = el("input", { value: ((plan.structured_spec || {}).components || []).join("、"), placeholder: "例如：数据卡片、产品轮廓、时间轴" });
  const motion = el("input", { value: (plan.structured_spec || {}).motion || "", placeholder: "例如：卡片依次进入，重点数字轻微放大" });
  const palette = el("input", { value: (plan.structured_spec || {}).palette || "", placeholder: "例如：深蓝、青色高光" });
  const stylePack = (plan.style_pack || {});
  const copyPlan = (plan.structured_spec || {}).copy_plan || {};
  const recipeLabels = {
    headline_statement: "标题判断", relationship_map: "关系图", single_metric: "关键数字",
    comparison: "双项对比", process: "流程", quote_evidence: "证据短句", closing_question: "结尾提问",
  };
  const recipe = el("select", {},
    el("option", { value: "headline_statement" }, "标题判断"),
    el("option", { value: "relationship_map" }, "关系图"),
    el("option", { value: "single_metric" }, "关键数字"),
    el("option", { value: "comparison" }, "双项对比"),
    el("option", { value: "process" }, "流程"),
    el("option", { value: "quote_evidence" }, "证据短句"),
    el("option", { value: "closing_question" }, "结尾提问"),
  );
  recipe.value = (plan.structured_spec || {}).scene_recipe || "relationship_map";
  const layoutVariant = el("select", { "aria-label": "动态画面版式" });
  const refreshLayoutChoices = (requested) => {
    const choices = hyperframesLayoutChoices(stylePack, recipe.value);
    const preserved = requested || layoutVariant.value || ((plan.structured_spec || {}).layout_variant || "");
    layoutVariant.replaceChildren(...choices.map((item) => el("option", { value: item.id }, `${item.name} · ${item.motion_variant}`)));
    layoutVariant.value = (choices.find((item) => item.id === preserved) || choices[0]).id;
  };
  refreshLayoutChoices((plan.structured_spec || {}).layout_variant || "");
  let recipeManuallyChanged = false;
  const savedRecipeMode = copyPlan.selection_mode || (copyPlan.status === "ready" && copyPlan.source === "text_ai" ? "ai" : "default");
  const recipeDecisionNode = el("span", { class: "recipe-decision" });
  const paintRecipeDecision = () => {
    const label = recipeLabels[recipe.value] || "当前结构";
    const mode = recipeManuallyChanged ? "manual" : savedRecipeMode;
    recipeDecisionNode.className = `recipe-decision ${mode}`;
    recipeDecisionNode.textContent = mode === "ai"
      ? `AI 推荐：${label} · 根据当前片段及前后文自动选择，可手动覆盖`
      : mode === "manual"
        ? `人工选择：${label} · 保存后将按你的选择生成`
        : `系统默认：${label} · 点击“AI 提炼画面文案”后会自动推荐`;
  };
  paintRecipeDecision();
  const subtitleMode = el("select", {},
    el("option", { value: "inherit" }, "沿用当前字幕方案（默认）"),
    el("option", { value: "apply_recommended" }, "应用科技快报 V1 推荐字幕"),
  );
  subtitleMode.value = stylePack.subtitle_mode || "inherit";
  const subtitleScope = el("select", {},
    el("option", { value: "scene" }, "只应用到当前片段"),
    el("option", { value: "all" }, "应用到全部片段"),
  );
  subtitleScope.value = stylePack.subtitle_apply_scope || "scene";
  const constraints = el("div", { class: "constraint-chips" },
    el("span", {}, "禁止第二主播"), el("span", {}, "禁止 AI 烘焙文字与水印"),
    el("span", {}, "保留字幕安全区"),
    ...((plan.constraints || []).includes("reserve_presenter_safe_area") ? [el("span", {}, "避开数字人区域")] : []),
  );
  let planDirty = false;
  let planStateNode = null;
  let motionButton = null;
  const markPlanDirty = () => {
    planDirty = true;
    if (planStateNode) planStateNode.textContent = `未保存：将改用 ${visualEngineLabel(engine.value)}；请先保存方案`;
    if (motionButton) {
      motionButton.disabled = true;
      motionButton.textContent = `请先保存为 ${engine.value === "hyperframes" ? "HyperFrames" : "Remotion"}`;
    }
  };
  [engine, prompt, headline, centerLabel, components, motion, palette, subtitleMode, subtitleScope, layoutVariant].forEach((control) => {
    control.addEventListener("input", markPlanDirty);
    control.addEventListener("change", markPlanDirty);
  });
  recipe.addEventListener("change", () => {
    recipeManuallyChanged = true;
    refreshLayoutChoices();
    paintRecipeDecision();
    markPlanDirty();
  });
  layoutVariant.addEventListener("change", markPlanDirty);
  const save = () => mutate(`/scenes/${encodeURIComponent(scene.id)}/visual-plan`, {
    method: "PUT",
    body: {
      engine: engine.value,
      prompt: prompt.value,
      structured_spec: {
        headline: headline.value,
        center_label: centerLabel.value,
        components: components.value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean),
        motion: motion.value,
        palette: palette.value,
        scene_recipe: recipe.value,
        layout_variant: layoutVariant.value,
        motion_variant: (hyperframesLayoutChoice(stylePack, recipe.value, layoutVariant.value) || {}).motion_variant || "",
        copy_plan: {
          ...copyPlan,
          ...(copyPlan.status === "ready" ? { selection_mode: recipeManuallyChanged ? "manual" : savedRecipeMode } : {}),
        },
      },
      style_pack_id: "tech-brief-v1",
      subtitle_mode: subtitleMode.value,
      subtitle_apply_scope: subtitleScope.value,
      constraints: plan.constraints || [],
    },
  }, "画面方案已保存；生成时将严格使用这份方案");

  const planState = plan.status === "saved" ? `已保存 · 版本 ${plan.revision}` : "草稿尚未保存";
  const refineCopy = button(
    copyPlan.status === "ready" ? "重新提炼画面文案" : "AI 提炼画面文案",
    "quiet",
    async () => {
      if (planDirty) return showToast("请先保存当前画面方案，再进行 AI 语境提炼", true);
      if (!window.confirm("将调用已配置的文本模型，结合当前片段及前后文提炼画面标题、中心结论和短要点。该操作可能产生少量 API 费用，是否继续？")) return;
      await mutate(`/scenes/${encodeURIComponent(scene.id)}/visual-copy/refine`, { method: "POST", body: {} }, "画面文案已根据前后语境提炼；请检查并可继续人工修改");
    },
    plan.engine !== "hyperframes",
  );
  const motionJob = scene.motion_generation || { status: "idle" };
  const isMotionEngine = ["hyperframes", "remotion"].includes(plan.engine);
  motionButton = isMotionEngine ? button(
    motionJob.status === "generating" ? `${plan.engine === "hyperframes" ? "HyperFrames" : "Remotion"} 正在生成…` : `用 ${plan.engine === "hyperframes" ? "HyperFrames" : "Remotion"} 生成动态素材`,
    "primary",
    () => mutate(`/scenes/${encodeURIComponent(scene.id)}/motion-visual/jobs`, { method: "POST", body: {} }, "动态素材任务已开始；完成后会登记新的 S-xxx 素材"),
    plan.status !== "saved" || motionJob.status === "generating",
  ) : null;
  const cardType = el("select", {},
    el("option", { value: "headline_metrics" }, "重点标题与数据"),
    el("option", { value: "comparison" }, "双项对比"),
    el("option", { value: "timeline" }, "流程与时间线"),
  );
  const cardTheme = el("select", {},
    el("option", { value: "tech_neon" }, "科技霓虹"),
    el("option", { value: "editorial" }, "编辑简报"),
    el("option", { value: "signal_amber" }, "信号琥珀"),
  );
  const pptJob = scene.ppt_card_generation || { status: "idle" };
  const pptButton = plan.engine === "ppt_card" ? button(
    pptJob.status === "generating" || pptJob.status === "queued" ? "正在生成 PPT 信息卡…" : "生成 PPT 信息卡素材",
    "primary",
    async () => {
      if (planDirty || plan.status !== "saved") return showToast("请先保存画面方案，再生成信息卡", true);
      try {
        state = await api(`/scenes/${encodeURIComponent(scene.id)}/ppt-cards/jobs`, {
          method: "POST",
          body: {
            confirmed: true,
            card_type: cardType.value,
            theme: cardTheme.value,
            title: headline.value,
            summary: prompt.value,
            items: components.value.split(/[、，,；;\n]/).map((item) => item.trim()).filter(Boolean),
          },
        });
        ensureSelection();
        render();
        trackTaskCenter();
        showToast("信息卡任务已进入任务中心；完成后会登记为新的 S-xxx 素材");
      } catch (error) { showToast(error.message || "信息卡任务提交失败", true); }
    },
    pptJob.status === "generating" || pptJob.status === "queued",
  ) : null;
  planStateNode = el("span", { class: "form-note visual-plan-state" }, planState);
  return el("section", { class: "visual-design" },
    el("div", { class: "visual-design-head" }, el("div", {}, el("strong", {}, "画面生成方案"), el("span", {}, "先看提示词，再决定是否生成；不会再自动生成第二主播。")), status(plan.status === "saved" ? "approved" : "pending")),
    el("label", {}, "生成方式", engine),
    el("label", {}, "可编辑画面提示词", prompt),
    plan.engine === "hyperframes" ? el("div", { class: "style-pack-contract" },
      el("div", { class: "style-pack-contract-head" }, el("strong", {}, "科技快报风格包 V1"), status("approved")),
      el("p", { class: "form-note" }, "9:16 已完成生产路径验证。背景只生成图形文字与动态结构；数字人和逐句字幕仍由独立图层合成。"),
      el("div", { class: "ai-copy-summary" },
        el("div", {}, el("strong", {}, "AI 画面文案"), el("span", {}, copyPlan.status === "ready" ? `已提炼 · ${copyPlan.model || "已配置模型"}` : "尚未提炼；当前可能仍是规则草稿")),
        copyPlan.scene_goal ? el("p", {}, `画面目标：${copyPlan.scene_goal}`) : null,
        refineCopy,
      ),
      el("div", { class: "visual-spec-grid" },
        el("label", {}, "当前画幅", el("output", {}, "继承项目设置")),
        el("label", { class: "recipe-field" }, "场景结构", recipe, recipeDecisionNode),
        el("label", {}, "画面版式", layoutVariant, el("small", { class: "form-note" }, "同一结构可换版式；不会改变数字人或模块化字幕。")),
        el("label", {}, "字幕处理", subtitleMode),
        el("label", {}, "推荐字幕应用范围", subtitleScope),
      ),
      el("p", { class: "form-note" }, "选择推荐字幕并保存后才会覆盖选定范围的字幕样式；不会修改逐句文字、配音或片段时长。"),
    ) : null,
    constraints,
    el("details", {}, el("summary", {}, "动态画面结构设置"), el("div", { class: "visual-spec-grid" }, el("label", {}, "归纳标题", headline), el("label", {}, "中心结论", centerLabel), el("label", {}, "短要点（使用顿号分隔）", components), el("label", {}, "运动", motion), el("label", {}, "配色", palette))),
    plan.engine === "ppt_card" ? el("div", { class: "visual-spec-grid ppt-card-options" }, el("label", {}, "信息卡结构", cardType), el("label", {}, "信息卡主题", cardTheme), el("p", { class: "form-note" }, "内容取自上方主题、提示词与组件；生成后会成为可在时间线拆分、锁定与替换的图片素材。")) : null,
    el("div", { class: "inline-actions" }, button("保存画面方案", "primary", save), motionButton, pptButton, planStateNode),
    motionJob.status === "failed" ? el("div", { class: "inline-error" }, `动态素材生成失败：${motionJob.error || "未返回具体原因"}`) : null,
    scene.motion_visual_candidate ? el("div", { class: "keyframe-adoption" }, el("strong", {}, `动态候选 ${scene.motion_visual_candidate.asset_id}`), el("span", {}, "已进入素材台账，可在下方画面时间线的任意区间选择使用。")) : null,
    scene.ppt_card_candidate ? el("div", { class: "keyframe-adoption" },
      el("strong", {}, `PPT 信息卡候选 ${scene.ppt_card_candidate.asset_id}`),
      el("span", {}, "已登记为普通图片素材；采用后仍可在下方时间线中单独拆分、锁定或换图。"),
      button("采用为当前画面", "primary small", () => mutate("/usages", { method: "POST", body: { scene_id: scene.id, asset_id: scene.ppt_card_candidate.asset_id, role: "visual" } }, "已采用信息卡；可继续在时间线调整具体区间")),
    ) : null,
  );
}

function visualAssetIdentity(asset) {
  const generation = (asset && asset.generation) || {};
  if (generation.video_id !== undefined && generation.video_id !== null) return `Pexels 视频 ${generation.video_id}`;
  if (generation.photo_id !== undefined && generation.photo_id !== null) return `Pexels 图片 ${generation.photo_id}`;
  return "";
}

function renderVisualTimelineEditor(scene) {
  const duration = Math.max(.04, Number(scene.end_seconds || 0) - Number(scene.start_seconds || 0));
  const presenterAssetIds = new Set((state.scenes || []).map((item) => presenterFor(item).asset_id).filter(Boolean));
  const usedBlockIds = new Set((state.scenes || []).flatMap((item) => (visualTimeline(item).blocks || []).map((block) => block.asset_id)).filter(Boolean));
  const assets = (state.assets || []).filter((asset) => {
    if (!isLiveAsset(asset) || !asset.path || !["image", "video"].includes(String(asset.type || "").toLowerCase())) return false;
    return !presenterAssetIds.has(asset.id) || usedBlockIds.has(asset.id);
  });
  const assetLookup = new Map(assets.map((asset) => [asset.id, asset]));
  const identityScenes = new Map();
  for (const owner of state.scenes || []) {
    for (const block of visualTimeline(owner).blocks || []) {
      const identity = visualAssetIdentity(assetLookup.get(block.asset_id) || (state.assets || []).find((asset) => asset.id === block.asset_id));
      if (!identity) continue;
      if (!identityScenes.has(identity)) identityScenes.set(identity, new Set());
      identityScenes.get(identity).add(owner.id);
    }
  }
  let blocks = (visualTimeline(scene).blocks || []).map((item) => ({ ...item }));
  const fallback = currentAsset(scene.id);
  if (!blocks.length && fallback && fallback.path && ["image", "video"].includes(String(fallback.type || "").toLowerCase())) {
    blocks = [{ id: "VB-001", start_seconds: 0, end_seconds: duration, asset_id: fallback.id, source_mode: fallback.source_type || "project_library", label: fallback.name }];
  }
  const trackBody = el("div", { class: "visual-track-body" });
  const splitAt = el("input", { type: "number", min: ".4", max: String(Math.max(.4, duration - .4)), step: ".01", value: String(Math.min(duration - .4, Math.max(.4, duration / 2)).toFixed(2)) });
  const rebuild = () => {
    trackBody.replaceChildren();
    if (!blocks.length) {
      trackBody.append(el("div", { class: "empty compact" }, el("strong", {}, "本段还没有画面区间"), el("span", {}, "先分配素材，或使用上方批量补全画面。")));
      return;
    }
    const rail = el("div", { class: "visual-track-rail" });
    for (const [index, block] of blocks.entries()) {
      const locked = Boolean(block.locked);
      const asset = assetLookup.get(block.asset_id);
      const span = Math.max(.04, Number(block.end_seconds) - Number(block.start_seconds));
      rail.append(el("button", { type: "button", class: "visual-track-block", style: `width:${Math.max(4, span / duration * 100)}%`, title: `${Number(block.start_seconds).toFixed(2)}–${Number(block.end_seconds).toFixed(2)} 秒` }, `${index + 1} · ${block.asset_id || "待选"}`));
      if (index > 0) {
        const previous = blocks[index - 1];
        const boundary = el("input", {
          type: "number", min: String((Number(previous.start_seconds) + .4).toFixed(2)), max: String((Number(block.end_seconds) - .4).toFixed(2)),
          step: ".01", value: Number(block.start_seconds).toFixed(2), disabled: locked || Boolean(previous.locked) ? "" : null,
        });
        boundary.addEventListener("change", () => {
          const point = Number(boundary.value);
          if (!(point >= Number(boundary.min) && point <= Number(boundary.max))) return showToast("切点必须让前后画面都至少保留 0.4 秒", true);
          previous.end_seconds = Number(point.toFixed(3));
          block.start_seconds = Number(point.toFixed(3));
          rebuild();
        });
        trackBody.append(el("div", { class: "visual-cut-boundary" },
          el("span", {}, `切点 ${index}`), boundary,
          el("span", { class: "visual-block-time" }, `成片 ${clock(Number(scene.start_seconds || 0) + Number(block.start_seconds || 0))}`),
        ));
      }
      const selector = el("select", { disabled: locked ? "" : null });
      selector.append(el("option", { value: "" }, "选择 S-xxx 图片或视频"));
      for (const item of assets) selector.append(el("option", { value: item.id, selected: item.id === block.asset_id ? "" : null }, `${item.id} · ${item.name}`));
      selector.addEventListener("change", () => {
        const selected = assetLookup.get(selector.value);
        block.asset_id = selector.value;
        block.label = selected ? selected.name : "";
        block.source_mode = selected && (selected.provenance || {}).source_tool === "ppt_card_provider" ? "ppt_card" : selected && selected.source_type === "web_download" ? "web_download" : selected && selected.source_type === "ai_generated" ? "openai_image" : "project_library";
        rebuild();
      });
      const identity = visualAssetIdentity(asset);
      const repeatedScenes = identity ? [...(identityScenes.get(identity) || [])].filter((sceneId) => sceneId !== scene.id) : [];
      const repeatedLabels = repeatedScenes.map((sceneId) => {
        const owner = (state.scenes || []).find((item) => item.id === sceneId);
        return owner ? `场景${String(owner.order).padStart(2, "0")}` : sceneId;
      });
      const media = asset ? (String(asset.type).toLowerCase() === "image"
        ? el("img", { src: mediaURL(projectId, asset.path), alt: asset.name || asset.id, loading: "lazy" })
        : el("video", { src: mediaURL(projectId, asset.path), muted: "", preload: "metadata" })) : el("div", { class: "visual-block-placeholder" }, "待选择画面");
      let refreshBlockButton = null;
      if (scene.source_strategy === "web_download") {
        refreshBlockButton = button(block.status === "generating" ? "正在换素材…" : "只换这格", "primary small", async () => {
          if (locked) return showToast("请先解锁这个画面区间", true);
          if (!window.confirm(`只替换 ${block.id}，其他画面和切点保持不变。是否继续？`)) return;
          try {
            state = await api(`/scenes/${encodeURIComponent(scene.id)}/visual-blocks/${encodeURIComponent(block.id)}/refresh/jobs`, { method: "POST", body: { confirmed: true } });
            trackVisualBatch((state.automation || {}).visual_batch);
            updateVisualBatchIsland();
            updateVisualBlockJobIslands();
            showToast(`${block.id} 已进入换素材队列`);
          } catch (error) { showToast(error.message || "画面区间换素材失败", true); }
        }, locked || visualBatchRunning());
        refreshBlockButton.dataset.visualBlockRefreshButton = `${scene.id}|${block.id}`;
      }
      const actions = el("div", { class: "inline-actions visual-block-actions" },
        button(locked ? "解锁" : "锁定", locked ? "quiet small" : "small", () => mutate(`/scenes/${encodeURIComponent(scene.id)}/visual-blocks/${encodeURIComponent(block.id)}`, { method: "PATCH", body: { locked: !locked } }, locked ? "画面区间已解锁" : "画面区间已锁定；批量操作不会覆盖它"), !block.asset_id),
        refreshBlockButton,
        blocks.length > 1 ? button(index === 0 ? "与后一格合并" : "与前一格合并", "quiet small", () => {
          const neighbor = index === 0 ? blocks[1] : blocks[index - 1];
          if (locked || Boolean(neighbor.locked)) return showToast("请先解锁相邻画面区间", true);
          if (index === 0) {
            block.end_seconds = blocks[1].end_seconds;
            blocks.splice(1, 1);
          } else {
            neighbor.end_seconds = block.end_seconds;
            blocks.splice(index, 1);
          }
          rebuild();
        }, locked || Boolean((index === 0 ? blocks[1] : blocks[index - 1])?.locked)) : null,
      );
      trackBody.append(el("article", { class: `visual-block-card ${locked ? "is-locked" : ""}` },
        el("div", { class: "visual-block-thumb" }, media),
        el("div", { class: "visual-block-detail" },
          el("div", { class: "visual-block-title" }, el("strong", {}, `区间 ${index + 1} · ${block.id || "新槽位"}`), status(locked ? "frozen" : "editable")),
          el("span", { class: "visual-block-time" }, `本段 ${Number(block.start_seconds).toFixed(2)}–${Number(block.end_seconds).toFixed(2)} 秒（${span.toFixed(2)} 秒）`),
          el("span", { class: "visual-block-time" }, `成片 ${clock(Number(scene.start_seconds || 0) + Number(block.start_seconds || 0))}–${clock(Number(scene.start_seconds || 0) + Number(block.end_seconds || 0))}`),
          selector,
          asset ? el("span", { class: "visual-asset-meta" }, `${asset.id} · ${String(asset.type || "").toUpperCase()}${identity ? ` · ${identity}` : ""}`) : null,
          repeatedLabels.length ? el("span", { class: "visual-duplicate-warning" }, `重复提醒：同一来源素材还用于 ${repeatedLabels.join("、")}`) : null,
          block.status === "failed" ? el("span", { class: "inline-error" }, block.error || "该槽位生成失败") : null,
          el("div", { class: "visual-block-job-live", hidden: "", "data-visual-block-job": "", "data-scene-id": scene.id, "data-block-id": block.id }),
          actions,
        ),
      ));
    }
    trackBody.prepend(rail);
  };
  const split = () => {
    const point = Number(splitAt.value);
    const index = blocks.findIndex((item) => point >= Number(item.start_seconds) + .4 && point <= Number(item.end_seconds) - .4);
    if (index < 0) return showToast("分割点必须位于画面内部，并让前后各保留至少 0.4 秒", true);
    if (blocks[index].locked) return showToast("请先解锁要分割的画面区间", true);
    const source = blocks[index];
    blocks.splice(index, 1, { ...source, end_seconds: Number(point.toFixed(3)) }, { ...source, id: "", start_seconds: Number(point.toFixed(3)), locked: false });
    rebuild();
  };
  const saveTimeline = () => {
    if (!blocks.length) return showToast("请先建立至少一个画面区间", true);
    mutate(`/scenes/${encodeURIComponent(scene.id)}/visual-timeline`, { method: "PUT", body: { blocks } }, "画面时间线已保存；播放器预览已标记为待同步");
  };
  rebuild();
  queueMicrotask(updateVisualBlockJobIslands);
  return el("section", { class: "visual-track visual-track-editor" },
    el("div", { class: "visual-track-head" },
      el("div", {}, el("strong", {}, `本段画面时间线 · ${blocks.length} 格`), el("span", {}, `智能切点、素材归属和重复来源都在这里；修改一格不会影响其他格。`)),
      el("span", { class: "status editable" }, `${duration.toFixed(2)} 秒`),
    ),
    trackBody,
    el("div", { class: "inline-actions split-controls" }, el("label", {}, "在本段第", splitAt, "秒新增切点"), button("分割当前区间", "quiet", split), button("保存画面时间线", "primary", saveTimeline)),
  );
}

function reviewCanvasProfile() {
  const profile = (state && state.project && state.project.render_profile) || {};
  const width = Number(profile.width || 0);
  const height = Number(profile.height || 0);
  if (width > 0 && height > 0) return { width, height, ratio: `${width} / ${height}` };
  const aspect = (state && state.project && state.project.intake && state.project.intake.aspect) || "landscape";
  if (aspect === "portrait" || aspect === "vertical") return { width: 9, height: 16, ratio: "9 / 16" };
  if (aspect === "square") return { width: 1, height: 1, ratio: "1 / 1" };
  return { width: 16, height: 9, ratio: "16 / 9" };
}

function sceneAspectRatio() {
  return reviewCanvasProfile().ratio;
}

function reviewLayoutKind() {
  const profile = reviewCanvasProfile();
  if (Math.abs(profile.width - profile.height) < .01) return "square";
  return profile.width < profile.height ? "portrait" : "landscape";
}

function reviewAspectLabel() {
  const profile = reviewCanvasProfile();
  return `${Math.round(profile.width)}×${Math.round(profile.height)}`;
}

function reviewFocusActive() {
  return reviewFocusMode === null ? reviewLayoutKind() === "portrait" : reviewFocusMode;
}

function sceneRelativeTime(scene, absolute) {
  return Math.min(Math.max(0, Number(absolute || 0) - Number(scene.start_seconds || 0)), Math.max(.04, Number(scene.end_seconds || 0) - Number(scene.start_seconds || 0)));
}

function reviewTimeLabel(scene, relative) {
  return `${clock(Number(scene.start_seconds || 0) + Number(relative || 0))} · 本段 ${fmtDuration(Math.max(0, Number(relative || 0)))}`;
}

async function api(path, options = {}) {
  const response = await fetch(`/api/project/${encodedProjectId}/workbench${path}`, {
    headers: Object.assign({ "Content-Type": "application/json" }, options.headers || {}),
    method: options.method,
    body: options.body && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body,
  });
  if (!response.ok) {
    let detail = "请求失败";
    try { detail = (await response.json()).detail || detail; } catch (error) { /* text fallback */ }
    throw new Error(detail);
  }
  return response.json();
}

async function mutate(path, options, success) {
  try {
    const nextState = await api(path, options);
    const scriptMutation = path === "/script-draft" || path === "/script-draft/review"
      || path === "/script-draft/content" || path === "/script-draft/reopen";
    if (scriptMutation) resetReviewPreviewForScriptChange();
    state = nextState;
    reviewPreviewScriptIdentityValue = reviewPreviewScriptIdentity(state);
    ensureSelection();
    render();
    if (scriptMutation && supportsReviewPreview() && (state.project.script_draft || {}).status === "approved") {
      void pollReviewPreviewJob({ quiet: true });
    }
    if (success) showToast(success);
  } catch (error) { showToast(error.message || "操作失败", true); }
}

async function uploadAvatarFile(path, file) {
  const separator = path.includes("?") ? "&" : "?";
  const response = await fetch(
    `/api/project/${encodedProjectId}/workbench${path}${separator}filename=${encodeURIComponent(file.name)}`,
    { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file },
  );
  if (!response.ok) {
    let detail = "数字人视频上传失败";
    try { detail = (await response.json()).detail || detail; } catch (error) { /* text fallback */ }
    throw new Error(detail);
  }
  state = await response.json();
  ensureSelection();
  render();
  return state;
}

async function loadAvatarScriptTemplatePreview(templateId) {
  const id = String(templateId || "");
  if (!id) {
    avatarScriptTemplatePreview = null;
    render();
    return;
  }
  try {
    const response = await fetch(`/api/script-templates/avatar/preview?template_id=${encodeURIComponent(id)}`);
    if (!response.ok) {
      let detail = "模板脚本预览读取失败";
      try { detail = (await response.json()).detail || detail; } catch (error) { /* text fallback */ }
      throw new Error(detail);
    }
    avatarScriptTemplatePreview = await response.json();
  } catch (error) {
    avatarScriptTemplatePreview = null;
    showToast(error.message || "模板脚本预览读取失败", true);
  }
  if (state) render();
}

async function loadAvatarScriptTemplates(force = false) {
  if (avatarScriptTemplatesLoading || (avatarScriptTemplates && !force)) return;
  avatarScriptTemplatesLoading = true;
  try {
    const response = await fetch("/api/script-templates/avatar");
    if (!response.ok) throw new Error("模板脚本列表读取失败");
    avatarScriptTemplates = await response.json();
    const templates = avatarScriptTemplates.templates || [];
    if (!templates.length) throw new Error("当前内容库没有可导入的数字人口播模板");
    if (!templates.some((item) => item.template_id === selectedAvatarScriptTemplateId)) {
      const preferred = templates.find((item) => item.episode_id === "004-tech-brief" && /v1\.1\.md$/i.test(item.filename)) || templates[0];
      selectedAvatarScriptTemplateId = preferred.template_id;
      await loadAvatarScriptTemplatePreview(selectedAvatarScriptTemplateId);
      return;
    }
  } catch (error) {
    avatarScriptTemplates = { templates: [] };
    showToast(error.message || "模板脚本列表读取失败", true);
  } finally {
    avatarScriptTemplatesLoading = false;
    if (state) render();
  }
}

async function importAvatarScriptTemplate(payload) {
  try {
    state = await api("/avatar-script/template", { method: "POST", body: payload });
    ensureSelection();
    avatarScriptTemplatePreview = null;
    render();
    showToast("模板脚本、分镜草案与数字人轮次素材包已初始化；尚未调用云端生成");
  } catch (error) { showToast(error.message || "模板脚本初始化失败", true); }
}

async function previewAvatarUserDocx(file) {
  if (!file) return;
  avatarUserScriptLoading = true;
  avatarUserScriptPreview = null;
  avatarUserScriptSpeakerOverrides = {};
  render();
  try {
    const response = await fetch(
      `/api/project/${encodedProjectId}/workbench/avatar-script/imports/preview?filename=${encodeURIComponent(file.name)}`,
      { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file },
    );
    if (!response.ok) {
      let detail = "Word 脚本解析失败";
      try { detail = (await response.json()).detail || detail; } catch (error) { /* text fallback */ }
      throw new Error(detail);
    }
    avatarUserScriptPreview = await response.json();
    avatarUserScriptSpeakerOverrides = Object.fromEntries(
      (avatarUserScriptPreview.speakers || []).map((speaker) => [speaker.name, speaker.speaker_id]),
    );
    showToast(`已识别 ${avatarUserScriptPreview.turn_count} 个轮次，请核对后确认导入`);
  } catch (error) {
    showToast(error.message || "Word 脚本解析失败", true);
  } finally {
    avatarUserScriptLoading = false;
    if (state) render();
  }
}

async function previewAvatarUserText() {
  if (!avatarUserScriptPasteText.trim()) return showToast("请先粘贴带有角色和台词的脚本", true);
  avatarUserScriptLoading = true;
  avatarUserScriptPreview = null;
  avatarUserScriptSpeakerOverrides = {};
  render();
  try {
    avatarUserScriptPreview = await api("/avatar-script/imports/preview", {
      method: "POST",
      body: { text: avatarUserScriptPasteText, title: avatarUserScriptTitle },
    });
    avatarUserScriptSpeakerOverrides = Object.fromEntries(
      (avatarUserScriptPreview.speakers || []).map((speaker) => [speaker.name, speaker.speaker_id]),
    );
    showToast(`已识别 ${avatarUserScriptPreview.turn_count} 个轮次，请核对后确认导入`);
  } catch (error) {
    showToast(error.message || "粘贴脚本解析失败", true);
  } finally {
    avatarUserScriptLoading = false;
    if (state) render();
  }
}

async function importAvatarUserScript(payload) {
  if (avatarUserScriptSubmitting) return;
  avatarUserScriptSubmitting = true;
  render();
  try {
    state = await api("/avatar-script/imports/commit", { method: "POST", body: payload });
    ensureSelection();
    avatarUserScriptPreview = null;
    avatarUserScriptSpeakerOverrides = {};
    render();
    showToast("脚本、分镜草案与数字人轮次已初始化；台词保持原文，未调用 AI");
  } catch (error) {
    showToast(error.message || "脚本初始化失败", true);
  } finally {
    avatarUserScriptSubmitting = false;
    if (state) render();
  }
}

async function loadAvatarRoles(force = false) {
  if (avatarRolesLoading || (avatarRoles && !force)) return;
  avatarRolesLoading = true;
  try {
    const response = await fetch("/api/avatar-roles");
    if (!response.ok) throw new Error("角色库读取失败");
    avatarRoles = await response.json();
  } catch (error) {
    showToast(error.message || "角色库读取失败", true);
    avatarRoles = { roles: [] };
  } finally {
    avatarRolesLoading = false;
    if (state) render();
  }
}

async function createAvatarRole(name, description, license) {
  try {
    const response = await fetch("/api/avatar-roles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description, license }),
    });
    if (!response.ok) {
      let detail = "角色创建失败";
      try { detail = (await response.json()).detail || detail; } catch (error) { /* fallback */ }
      throw new Error(detail);
    }
    await loadAvatarRoles(true);
    showToast("角色已建立；请上传透明三视图或至少一张正面参考图");
  } catch (error) { showToast(error.message || "角色创建失败", true); }
}

async function uploadAvatarRoleReference(roleId, slot, file) {
  try {
    const response = await fetch(`/api/avatar-roles/${encodeURIComponent(roleId)}/references/${encodeURIComponent(slot)}/file?filename=${encodeURIComponent(file.name)}`, {
      method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file,
    });
    if (!response.ok) {
      let detail = "角色参考图上传失败";
      try { detail = (await response.json()).detail || detail; } catch (error) { /* fallback */ }
      throw new Error(detail);
    }
    await loadAvatarRoles(true);
    showToast("角色参考图已保存到通用角色库");
  } catch (error) { showToast(error.message || "角色参考图上传失败", true); }
}

async function selectAvatarCloudRole(speakerId, roleId) {
  await mutate(
    `/avatar-package/cloud/speakers/${encodeURIComponent(speakerId)}/role`,
    { method: "POST", body: { role_id: roleId || "" } },
    roleId ? "通用角色档案已关联；它仅用于身份留档，不影响当前出镜图生成" : "已取消通用角色档案关联；当前出镜图仍可直接生成",
  );
}

async function uploadAvatarCloudPresenter(speakerId, file) {
  try {
    await uploadAvatarFile(`/avatar-package/cloud/speakers/${encodeURIComponent(speakerId)}/presenter/file`, file);
    showToast("项目出镜图已上传；该图片将作为阿里云实际生成输入");
  } catch (error) { showToast(error.message || "项目出镜图上传失败", true); }
}

async function saveAvatarCloudRenderSpec(payload) {
  await mutate("/avatar-package/cloud/render-spec", { method: "POST", body: payload }, "输出画幅已保存，云端输入图已完成本地检查");
}

async function uploadAvatarDrivingAudio(turnId, file) {
  try {
    await uploadAvatarFile(`/avatar-package/turns/${encodeURIComponent(turnId)}/driving-audio/file`, file);
    showToast(`${turnId} 的驱动音频已校验；它将决定该片段的最终时长`);
  } catch (error) { showToast(error.message || `${turnId} 的驱动音频上传失败`, true); }
}

async function generateAvatarVoiceboxDrivingAudio(turnId, profileId) {
  await mutate(
    `/avatar-package/turns/${encodeURIComponent(turnId)}/driving-audio/voicebox/candidates/jobs`,
    { method: "POST", body: profileId ? { profile_id: profileId } : {} },
    `${turnId} 正在用 Haike Video 本地配音生成候选音频；完成后可直接试听与采用`,
  );
}

async function applyAvatarVoiceboxDrivingAudio(turnId, candidateId) {
  await mutate(
    `/avatar-package/turns/${encodeURIComponent(turnId)}/driving-audio/voicebox/candidates/${encodeURIComponent(candidateId)}/apply`,
    { method: "POST", body: {} },
    `${turnId} 已采用该本地配音；真实音频时长将作为云端数字人片段时长`,
  );
}

async function refreshAvatarVoiceboxMappings() {
  await mutate(
    "/avatar-package/voicebox/mappings/refresh",
    { method: "POST", body: {} },
    "已按 Haike Video 本地音色库重新识别同名说话人；重复同名音色会要求你手动指定",
  );
}

async function setAvatarVoiceboxSpeakerMapping(speakerId, profileId) {
  await mutate(
    `/avatar-package/voicebox/speakers/${encodeURIComponent(speakerId)}/mapping`,
    { method: "POST", body: { profile_id: profileId || "" } },
    profileId ? "已保存本项目的说话人音色指定" : "已恢复同名优先的自动音色识别",
  );
}

async function startAvatarVoiceboxBatch(mode) {
  const copy = {
    missing_and_apply: "正在按脚本顺序为缺少音频的轮次配音；每段生成后会自动采用，页面会持续更新进度",
    all_candidates: "正在按脚本顺序生成全部候选音频；不会覆盖当前已采用的驱动音频",
    failed_only_and_apply: "正在按脚本顺序重试此前失败的轮次；成功后会自动采用",
  };
  await mutate(
    "/avatar-package/voicebox/batch/jobs",
    { method: "POST", body: { mode } },
    copy[mode] || "批量配音任务已排队",
  );
}

async function uploadAvatarTurn(turnId, file) {
  try {
    await uploadAvatarFile(`/avatar-package/turns/${encodeURIComponent(turnId)}/file`, file);
    showToast(`${turnId} 已上传并完成媒体探测`);
  } catch (error) { showToast(error.message || `${turnId} 上传失败`, true); }
}

async function uploadAvatarSpeaker(speakerId, file) {
  try {
    await uploadAvatarFile(`/avatar-package/speakers/${encodeURIComponent(speakerId)}/file`, file);
    showToast(`${speakerId} 的长视频已上传`);
  } catch (error) { showToast(error.message || `${speakerId} 上传失败`, true); }
}

async function switchToLocalLongformPlan() {
  if (!confirm("将创建一个独立的“本地整段口播切割”方案。现有云端方案会完整归档，不会删除。确认切换吗？")) return;
  await mutate(
    "/avatar-package/plans/local-longform",
    { method: "POST", body: { frame_fit_mode: "blur_background" } },
    "已切换到本地整段口播方案；原云端方案已完整归档",
  );
}

async function saveLongformPresentation(frameFitMode) {
  await mutate(
    "/avatar-package/presentation",
    { method: "POST", body: { frame_fit_mode: frameFitMode } },
    "画幅适配已保存；它会在最终合成时生效",
  );
}

async function saveLongformCut(turnId, startSeconds, endSeconds, reviewNote = "") {
  await mutate(
    `/avatar-package/cut-plan/items/${encodeURIComponent(turnId)}`,
    { method: "PATCH", body: { start_seconds: Number(startSeconds), end_seconds: Number(endSeconds), review_note: reviewNote } },
    `${turnId} 的切点已保存，等待你确认`,
  );
}

async function approveLongformCut(turnId) {
  await mutate(
    `/avatar-package/cut-plan/items/${encodeURIComponent(turnId)}/approve`,
    { method: "POST", body: {} },
    `${turnId} 已通过切割审核`,
  );
}

async function approveHighConfidenceLongformCuts() {
  await mutate(
    "/avatar-package/cut-plan/approve-high-confidence",
    { method: "POST", body: {} },
    "全部高置信片段已批量通过；其余片段仍需逐条审核",
  );
}

async function loadLocalWhisperModels(force = false) {
  if (localWhisperModelsLoading || (localWhisperModels && !force)) return;
  localWhisperModelsLoading = true;
  try {
    const response = await fetch(`/api/project/${encodedProjectId}/workbench/avatar-package/asr/local-models`);
    if (!response.ok) throw new Error("无法读取本机可用的 ASR 模型");
    localWhisperModels = (await response.json()).models || [];
  } catch (error) {
    localWhisperModels = [];
    showToast(error.message || "无法读取本机可用的 ASR 模型", true);
  } finally {
    localWhisperModelsLoading = false;
    if (state) render();
  }
}

async function startLongformSpeakerDiagnosis(speakerId, model) {
  await mutate(
    `/avatar-package/asr/speakers/${encodeURIComponent(speakerId)}/candidates/jobs`,
    { method: "POST", body: model ? { model } : {} },
    "已开始仅分析该说话人的台词；当前切点和其他角色均不会改变",
  );
}

async function applyLongformSpeakerCandidate(speakerId, candidateId) {
  if (!confirm("采用后只会替换该说话人的待审核切点；其他角色已通过的片段保持不变。是否继续？")) return;
  await mutate(
    `/avatar-package/asr/speakers/${encodeURIComponent(speakerId)}/candidates/${encodeURIComponent(candidateId)}/apply`,
    { method: "POST", body: {} },
    "候选切点已采用；请在下方逐段试听并审核",
  );
}

async function realignLongformSpeakerCandidate(speakerId, candidateId) {
  await mutate(
    `/avatar-package/asr/speakers/${encodeURIComponent(speakerId)}/candidates/${encodeURIComponent(candidateId)}/realign/jobs`,
    { method: "POST", body: {} },
    "正在复用已有识别文本重新对齐：不会再次运行 ASR，也不会改动当前切点",
  );
}

async function uploadAvatarBatch(files) {
  const packageState = state && state.avatar_package;
  if (!packageState || packageState.import_mode !== "per_turn") return;
  const turns = new Map(packageState.turns.map((turn) => [turn.turn_id, turn]));
  let completed = 0;
  const rejected = [];
  for (const file of Array.from(files || [])) {
    const match = file.name.toUpperCase().match(/T\d{3,}/);
    if (!match || !turns.has(match[0])) {
      rejected.push(file.name);
      continue;
    }
    try {
      await uploadAvatarFile(`/avatar-package/turns/${encodeURIComponent(match[0])}/file`, file);
      completed += 1;
    } catch (error) {
      rejected.push(`${file.name}（${error.message}）`);
    }
  }
  if (rejected.length) showToast(`已上传 ${completed} 个；未匹配：${rejected.join("、")}`, true);
  else showToast(`已按轮次编号上传 ${completed} 个数字人视频`);
}

function keyframeAnchorLabel(kind) { return kind === "first_frame" ? "首帧" : "高潮帧"; }

function keyframeJobForScene(scene) {
  if (keyframeJobSceneId === scene.id && keyframeJob) return keyframeJob;
  return scene.keyframe_generation || {};
}

function updateKeyframeJobIslands() {
  if (!keyframeJobSceneId) return;
  const job = keyframeJob || {};
  const anchors = job.anchors || {};
  const elapsed = Math.max(0, Math.floor((Date.now() - keyframeJobStartedAt) / 1000));
  document.querySelectorAll(`[data-keyframe-job-scene="${CSS.escape(String(keyframeJobSceneId))}"]`).forEach((panel) => {
    const done = Number(job.completed_count || 0);
    const total = Number(job.expected_count || 2);
    const active = job.status === "completed"
      ? "关键帧已生成，等待你加载结果"
      : ["failed", "completed_with_failures"].includes(job.status)
        ? "任务未完全完成，可继续失败关键帧"
        : job.active_anchor_kind ? `正在生成${keyframeAnchorLabel(job.active_anchor_kind)}` : "正在准备下一张关键帧";
    const summary = panel.querySelector("[data-keyframe-job-summary]");
    if (summary) summary.textContent = `${active} · 已完成 ${done}/${total} · 已运行 ${elapsed} 秒。你可以继续播放、审片和填写批注。`;
    for (const kind of ["first_frame", "climax_frame"]) {
      const node = panel.querySelector(`[data-keyframe-anchor="${kind}"]`);
      if (!node) continue;
      const item = anchors[kind] || {};
      node.textContent = `${keyframeAnchorLabel(kind)}：${statusLabels[item.status] || "等待中"}${item.asset_id ? `（${item.asset_id}）` : ""}`;
      node.className = `status ${item.status || "queued"}`;
    }
    const actionBox = panel.querySelector("[data-keyframe-job-actions]");
    if (actionBox) {
      actionBox.replaceChildren();
      if (keyframeResultsReady && job.status === "completed") {
        actionBox.append(button("加载新结果", "quiet small", async () => {
          keyframeResultsReady = false;
          keyframeJobSceneId = null;
          await refresh();
        }));
      } else if (["failed", "completed_with_failures"].includes(job.status)) {
        actionBox.append(button("继续生成失败关键帧", "quiet small", () => resumeFailedKeyframeJob(), false));
      }
    }
  });
}

function renderKeyframeJobIslands(sceneId) {
  if (sceneId !== keyframeJobSceneId) return;
  updateKeyframeJobIslands();
}

function stopKeyframeJobTracking({ completed = false } = {}) {
  clearInterval(keyframeJobTimer);
  keyframeJobTimer = null;
  const job = keyframeJob || {};
  const sceneId = keyframeJobSceneId;
  keyframeJobStartedAt = 0;
  keyframeJobElapsed = 0;
  if (completed && sceneId) {
    keyframeResultsReady = true;
    showToast(job.status === "completed" ? "关键帧已就绪；当前预览未被替换，点击“加载新结果”后再审核。" : "部分关键帧未完成；成功图片已登记，可继续失败的那一张。", job.status !== "completed");
  }
  updateKeyframeJobIslands();
}

async function pollKeyframeJob() {
  if (!keyframeJobSceneId || keyframeJobPollInFlight) return;
  keyframeJobPollInFlight = true;
  try {
    const response = await fetch(`/api/project/${encodedProjectId}/workbench/scenes/${encodeURIComponent(keyframeJobSceneId)}/keyframes/jobs/current`);
    if (!response.ok) throw new Error("无法读取关键帧任务状态");
    const task = await response.json();
    keyframeJob = task.generation || {};
    if (keyframeJob.status !== "generating") {
      stopKeyframeJobTracking({ completed: true });
      return;
    }
    updateKeyframeJobIslands();
  } catch (error) {
    const panel = document.querySelector(`[data-keyframe-job-scene="${CSS.escape(String(keyframeJobSceneId || ""))}"] [data-keyframe-job-summary]`);
    if (panel) panel.textContent = "后台任务仍在执行；暂时无法读取最新进度。审核操作不受影响。";
  } finally {
    keyframeJobPollInFlight = false;
  }
}

function trackKeyframeJob(sceneId, job) {
  keyframeJobSceneId = sceneId;
  keyframeJob = job || {};
  keyframeResultsReady = false;
  keyframeJobStartedAt = Date.parse(keyframeJob.started_at || "") || Date.now();
  clearInterval(keyframeJobTimer);
  keyframeJobTimer = setInterval(() => {
    keyframeJobElapsed = Math.max(0, Math.floor((Date.now() - keyframeJobStartedAt) / 1000));
    updateKeyframeJobIslands();
    if (keyframeJobElapsed > 0 && keyframeJobElapsed % 3 === 0) void pollKeyframeJob();
  }, 1000);
  updateKeyframeJobIslands();
  void pollKeyframeJob();
}

async function resumeFailedKeyframeJob() {
  const sceneId = keyframeJobSceneId;
  const job = keyframeJob || {};
  if (!sceneId) return;
  if (!window.confirm("将仅继续失败的关键帧。已成功图片不会再次调用生图服务，是否继续？")) return;
  try {
    const nextState = await api(`/scenes/${encodeURIComponent(sceneId)}/keyframes/jobs/current/retry`, {
      method: "POST",
      body: { model: job.model || "gpt-image-2", size: job.size || "1536x1024", quality: job.quality || "low" },
    });
    const nextScene = (nextState.scenes || []).find((scene) => scene.id === sceneId);
    if (state && nextScene) {
      const localScene = state.scenes.find((scene) => scene.id === sceneId);
      if (localScene) localScene.keyframe_generation = nextScene.keyframe_generation;
    }
    trackKeyframeJob(sceneId, (nextScene || {}).keyframe_generation);
    showToast("已继续失败的关键帧；已成功素材不会重复生成。");
  } catch (error) {
    showToast(error.message || "继续生成失败关键帧未能启动", true);
  }
}

async function startKeyframeGeneration(scene, quality, resumeFailed = false) {
  if (keyframeJobSceneId) return;
  try {
    const nextState = await api(`/scenes/${encodeURIComponent(scene.id)}/keyframes/jobs${resumeFailed ? "/current/retry" : ""}`, {
      method: "POST", body: { confirmed: true, model: "gpt-image-2", size: "1536x1024", quality, resume_failed: resumeFailed },
    });
    const startedScene = (nextState.scenes || []).find((item) => item.id === scene.id) || scene;
    if (state) {
      const localScene = state.scenes.find((item) => item.id === scene.id);
      if (localScene) localScene.keyframe_generation = startedScene.keyframe_generation;
    }
    trackKeyframeJob(scene.id, startedScene.keyframe_generation);
    renderKeyframeJobIslands(scene.id);
    showToast(resumeFailed ? "已继续失败的关键帧；已成功素材不会重复生成。" : "已开始后台生成；左侧预览和其他审核操作不会刷新。");
  } catch (error) {
    showToast(error.message || "关键帧任务未能启动", true);
  }
}

async function adoptAiVisualAndRefreshPreview(scene) {
  try {
    state = await api(`/scenes/${encodeURIComponent(scene.id)}/ai-visual/adopt`, { method: "POST" });
    ensureSelection();
    render();
    showToast("AI 主体画面已采用，正在生成本段可播放预览");
    state = await api(`/scenes/${encodeURIComponent(scene.id)}/review-preview`, { method: "POST" });
    ensureSelection();
    render();
    showToast("AI 主体画面已进入本段预览；旧网络素材已保留，可随时回退");
  } catch (error) {
    showToast(error.message || "采用 AI 主体画面失败", true);
  }
}

function automationState() {
  return (state && state.automation) || { asset_generation: {}, narration_generation: {}, render: {}, voice: {} };
}

function assetAutomationRunning() {
  return automationState().asset_generation && automationState().asset_generation.status === "generating";
}

function narrationAutomationRunning() {
  return automationState().narration_generation && automationState().narration_generation.status === "generating";
}

function videoRenderRunning() {
  return automationState().render && automationState().render.status === "generating";
}

async function startNetworkAssetGeneration() {
  if (assetAutomationRunning() || narrationAutomationRunning() || videoRenderRunning()) return;
  try {
    state = await api("/automation/network-assets/jobs", { method: "POST", body: { confirmed: true, fill_undecided: true } });
    ensureSelection();
    render();
    showToast("已开始按真实旁白时长搜集 Pexels 素材，页面会持续显示每个场景的进度。");
  } catch (error) { showToast(error.message || "Pexels 自动素材任务未能启动", true); }
}

async function startAvatarHandoff(defaultTreatment) {
  if (assetAutomationRunning() || narrationAutomationRunning() || videoRenderRunning()) return;
  try {
    state = await api("/avatar-package/handoff/jobs", { method: "POST", body: { default_treatment: defaultTreatment } });
    ensureSelection();
    render();
    showToast("已开始逐段合成数字人原声母版；页面可继续操作或刷新，已完成片段会自动复用。");
  } catch (error) { showToast(error.message || "数字人一键交接未能启动", true); }
}

async function savePresenterLayout(scene, payload, success) {
  try {
    state = await api("/presenter-layouts", { method: "POST", body: Object.assign({ scene_id: scene.id }, payload) });
    ensureSelection();
    render();
    showToast(success || "数字人版式已保存");
  } catch (error) { showToast(error.message || "数字人版式保存失败", true); }
}

function updateStateWithoutReviewReload(nextState) {
  state = nextState;
  stateFingerprint = JSON.stringify(nextState);
  ensureSelection();
}

function keepSavedSubtitleDraft(sceneId, nextState, { invalidateAll = false } = {}) {
  updateStateWithoutReviewReload(nextState);
  // Saving a reusable style changes the shared template.  Other scenes may
  // already have an in-memory editor draft created before that update; such a
  // draft would otherwise mask the new template after switching scenes or
  // using the app-level refresh.  A per-scene save intentionally preserves
  // other drafts, while a global template save must discard them.
  if (invalidateAll) subtitleDrafts.clear();
  const persistedScene = (state.scenes || []).find((item) => item.id === sceneId);
  if (!persistedScene) return;
  // The player controller deliberately keeps the existing video element alive.
  // Retain a draft that mirrors the just-saved data so that controller can
  // repaint its overlay immediately rather than waiting for a full UI render.
  subtitleDrafts.set(sceneId, {
    template_id: subtitleState(persistedScene).template_id || "subtitle-default",
    style: structuredClone(subtitleStyleFor(persistedScene)),
    cue_overrides: Object.assign({}, subtitleState(persistedScene).cue_overrides || {}),
  });
}

async function saveSceneSubtitles(scene, draft) {
  try {
    const nextState = await api(`/scenes/${encodeURIComponent(scene.id)}/subtitles`, {
      method: "PUT",
      body: { template_id: draft.template_id, style: draft.style, cue_overrides: draft.cue_overrides },
    });
    keepSavedSubtitleDraft(scene.id, nextState);
    refreshLiveCaption(scene);
    showToast("字幕已保存；左侧审核视频未重载，成片预览会提示需要更新");
  } catch (error) { showToast(error.message || "字幕保存失败", true); }
}

async function saveSubtitleStyleForAll(scene, draft, name) {
  try {
    const nextState = await api("/subtitle-styles", {
      method: "POST",
      body: {
        scene_id: scene.id,
        template_id: draft.template_id || "subtitle-default",
        name: String(name || "标准中文短句字幕").trim(),
        style: draft.style,
        apply_scope: "all",
        set_default: true,
      },
    });
    keepSavedSubtitleDraft(scene.id, nextState, { invalidateAll: true });
    refreshLiveCaption(scene);
    showToast("字幕方案已应用到全部片段；文字内容和配音保持各自独立");
  } catch (error) { showToast(error.message || "批量应用字幕方案失败", true); }
}

async function saveSubtitleStyleAsDefault(draft) {
  try {
    const response = await fetch("/api/workbench/subtitle-defaults", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style: draft.style }),
    });
    if (!response.ok) {
      let detail = "默认字幕样式保存失败";
      try { detail = (await response.json()).detail || detail; } catch (error) { /* ignored */ }
      throw new Error(detail);
    }
    await response.json();
    showToast("已设为以后新视频的默认字幕样式；当前项目不会被自动覆盖。");
  } catch (error) { showToast(error.message || "默认字幕样式保存失败", true); }
}

async function refreshCurrentSceneNetworkAsset(scene, instruction = "") {
  if (assetAutomationRunning() || narrationAutomationRunning() || videoRenderRunning()) {
    return showToast("当前有生产任务在运行，请等待完成后再更换素材", true);
  }
  try {
    state = await api(`/scenes/${encodeURIComponent(scene.id)}/network-assets/jobs`, {
      method: "POST",
      body: { confirmed: true, instruction: instruction.trim() },
    });
    ensureSelection();
    render();
    showToast(`已开始仅替换${sceneName(scene)}的 Pexels 素材；其他场景和旧素材记录不会改变。`);
  } catch (error) { showToast(error.message || "当前场景素材未能开始更换", true); }
}

function openAudioCenter() {
  location.assign(`/audio?return=${encodeURIComponent(`/p/${projectId}`)}`);
}

async function startProjectNarration() {
  try {
    state = await api("/automation/narration/jobs", {
      method: "POST",
      body: { confirmed: true },
    });
    ensureSelection();
    render();
    showToast("已开始生成自然语速的项目旁白；完成后会先建立真实时间线，再按时长匹配画面。");
  } catch (error) { showToast(error.message || "项目旁白任务未能启动", true); }
}

async function startProjectVideoRender() {
  try {
    state = await api("/automation/video-render/jobs", { method: "POST", body: { confirmed: true } });
    ensureSelection();
    render();
    showToast("已开始合成视频；页面会持续显示进度，完成后进入关键帧与片段审核。");
  } catch (error) { showToast(error.message || "视频合成任务未能启动", true); }
}

function fullPreviewState() {
  // Mutation responses contain the model as it stood immediately before the
  // operation's derived summary was attached.  Re-derive the small counters
  // from the returned scenes/job so the controls never briefly show stale
  // "idle / 0 approved" state while SSE is catching up.
  const scenes = state.scenes || [];
  const approved = scenes.filter((scene) => scene.review_status === "approved");
  const missing = scenes.filter((scene) => !sceneIsRenderable(scene)).map((scene) => scene.id);
  return {
    ...(state.full_preview || {}),
    technical_ready: scenes.length > 0 && missing.length === 0,
    missing_scene_ids: missing,
    total_scenes: scenes.length,
    approved_count: approved.length,
    all_scenes_approved: scenes.length > 0 && approved.length === scenes.length,
    preview: automationState().preview_render || ((state.full_preview || {}).preview) || {},
  };
}

async function startFullPreviewRender() {
  const preview = fullPreviewState().preview || {};
  if (preview.status === "generating") return;
  try {
    state = await api("/automation/full-preview/jobs", { method: "POST", body: { confirmed: true } });
    ensureSelection();
    render();
    showToast("已开始生成全片预览：不会改变任何片段的审核状态，也不会覆盖正式成片。");
  } catch (error) { showToast(error.message || "全片预览任务未能启动", true); }
}

async function approveFullPreviewScenes() {
  const summary = fullPreviewState();
  if (!window.confirm(`确认已看完全片预览，并一次通过全部 ${summary.total_scenes || 0} 个技术就绪片段吗？之后仍可回到任意片段修改，修改会让本预览过期。`)) return;
  try {
    state = await api("/review/full-preview/approve", { method: "POST", body: { confirmed: true } });
    ensureSelection();
    render();
    showToast("已批量通过可发布片段。现在可以生成正式成片。", false);
  } catch (error) { showToast(error.message || "批量确认失败", true); }
}

function openSceneFromFullPreview(sceneId) {
  selectedSceneId = sceneId;
  const segment = segmentForScene(sceneId);
  if (segment) selectedSegmentId = segment.id;
  activeView = "review";
  reviewFocusMode = false;
  render();
}

function renderFullPreviewPanel(compact = false) {
  const summary = fullPreviewState();
  const preview = summary.preview || {};
  const total = Number(summary.total_scenes || state.scenes.length || 0);
  const approved = Number(summary.approved_count || 0);
  const missing = summary.missing_scene_ids || [];
  const isGenerating = preview.status === "generating";
  const isReady = preview.status === "completed" && preview.output_path;
  const isStale = preview.status === "needs_refresh";
  const isFailed = preview.status === "failed";
  const canPreview = Boolean(summary.technical_ready) && !isGenerating;
  const canPublish = Boolean(summary.technical_ready) && Boolean(summary.all_scenes_approved) && !isGenerating;
  const actions = el("div", { class: "inline-actions" });
  if (isReady) actions.append(el("a", { class: "button primary", href: mediaURL(projectId, preview.output_path), target: "_blank", rel: "noreferrer" }, "打开全片预览"));
  actions.append(button(
    isGenerating ? "正在合成全片预览…" : isFailed ? "重试生成全片预览" : isStale ? "重新生成全片预览" : isReady ? "生成新的全片预览" : "生成全片预览",
    "primary", startFullPreviewRender, !canPreview,
  ));
  if (isReady) actions.append(button(`一键确认全部 ${total} 段`, "", approveFullPreviewScenes, approved >= total));
  actions.append(button("发布正式成片", "", startProjectVideoRender, !canPublish));
  const sceneLinks = el("div", { class: "inline-actions" });
  for (const scene of state.scenes) {
    sceneLinks.append(button(`${String(scene.order || 0).padStart(2, "0")} · ${scene.title || scene.id}`, "quiet small", () => openSceneFromFullPreview(scene.id)));
  }
  const copy = isGenerating
    ? `正在用现有时间线合成预览 v${preview.version || ""}；片段工作台仍可继续操作。`
    : isFailed ? `预览 v${preview.version || ""} 合成失败：${preview.error || "后台未返回具体原因，请重试或查看任务中心。"}`
    : isStale ? `有片段已修改：${preview.stale_reason || "请重新生成全片预览"}`
    : isReady ? `预览 v${preview.version || 1} 已就绪。已人工确认 ${approved}/${total} 段；先看全片，再一键确认或跳回指定片段修改。`
    : summary.technical_ready ? `所有 ${total} 段画面已技术就绪。可以先生成全片预览；此操作不会自动通过任何片段。`
    : `还缺少 ${missing.length} 段可合成画面：${missing.join("、") || "请补全画面时间线"}`;
  return el("section", { class: `panel automation-panel full-preview-panel${compact ? " compact" : ""}` },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "全片预览与正式发布"), el("p", {}, "先看完整节奏，再确认；正式成片只在人工确认后生成，并建立热插拔基线。"))),
    el("div", { class: "panel-body automation-body" },
      el("div", { class: `automation-progress${isGenerating ? " is-running" : ""}` }, status(isGenerating ? "generating" : isReady ? "completed" : isFailed ? "failed" : isStale ? "needs_refresh" : "pending"), el("span", {}, copy)),
      actions,
      isReady || isStale ? el("div", { class: "minor" }, "定位修改：") : null,
      isReady || isStale ? sceneLinks : null,
    ),
  );
}

async function startSceneNarrationCandidate(scene, text, profileId) {
  const narration = sceneNarration(scene);
  if (narration.job && narration.job.status === "generating") return;
  try {
    state = await api(`/scenes/${encodeURIComponent(scene.id)}/narration/candidates/jobs`, {
      method: "POST", body: { text: text.trim(), profile_id: profileId || null },
    });
    ensureSelection();
    render();
    showToast("正在生成该片段的候选配音；完成后可直接试听，不会修改当前成片");
  } catch (error) { showToast(error.message || "候选配音任务未能启动", true); }
}

async function applySceneNarrationCandidate(scene, versionId) {
  try {
    state = await api(`/scenes/${encodeURIComponent(scene.id)}/narration/candidates/${encodeURIComponent(versionId)}/apply/jobs`, { method: "POST" });
    ensureSelection();
    render();
    showToast("正在按候选配音的自然时长重合成当前片段；前序内容不变，后续内容只会顺延或前移。");
  } catch (error) { showToast(error.message || "局部合成任务未能启动", true); }
}

function avatarAssemblyProgressText(assembly) {
  const summary = assembly.summary || {};
  const total = Number(summary.total || 0);
  const completed = Number(summary.completed || 0);
  const reused = Number(summary.reused || 0);
  if (assembly.status === "running") {
    if (summary.phase === "normalizing") {
      const current = summary.current_turn_id ? `正在处理 ${summary.current_turn_id}` : "正在准备下一段";
      return `${current}（${completed}/${total || "?"}）；已复用 ${reused} 段。可离开或刷新页面，任务会继续运行。`;
    }
    if (summary.phase === "concatenating") return `已完成 ${completed}/${total || "?"} 段规范化，正在顺序拼接母版；不会重新拉伸数字人原声。`;
    return "正在准备数字人原声母版；任务采用低内存逐段处理，页面可继续操作。";
  }
  if (assembly.status === "failed" && summary.resumable) {
    return `上次合成未完成，已保留 ${completed}/${total || "?"} 段有效切片。重新点击“合成母版并进入片段工作台”会从可复用片段继续，不会改动脚本、原片或已审核切点。`;
  }
  return "本项目采用数字人视频自带声音。请按轮次上传、核对台词，再由原声生成时间线和字幕。";
}

function avatarAssemblyIssueText(assembly) {
  if (assembly.status !== "failed") return null;
  const raw = String(assembly.error || ((assembly.issues || [])[0] || {}).message || "");
  if (/cannot allocate memory|malloc of size|内存不足/i.test(raw)) {
    return "上次合成因内存不足中断。系统已切换为低内存逐段合成；重新启动即可从已完成片段继续。";
  }
  return raw ? `上次合成未完成：${raw.slice(-360)}` : "上次合成未完成，可重新启动并从已完成片段继续。";
}

function renderNativeAvatarAutomation(compact) {
  const packageState = state.avatar_package;
  const validation = packageState.validation || {};
  const asr = packageState.asr || {};
  const assembly = packageState.assembly || {};
  const complete = assembly.status === "passed";
  const applied = avatarTimelineApplied();
  const running = asr.status === "running" || assembly.status === "running";
  const steps = el("div", { class: "automation-steps" });
  const uploaded = packageState.import_mode === "per_turn"
    ? packageState.turns.filter((turn) => turn.source).length
    : packageState.speakers.filter((speaker) => speaker.source).length;
  const expected = packageState.import_mode === "per_turn" ? packageState.turns.length : packageState.speakers.length;
  const stepData = [
    [uploaded === expected ? "done" : "active", "1", `上传数字人原片 ${uploaded}/${expected}`],
    [validation.status === "passed" || validation.status === "passed_with_warnings" ? "done" : validation.status === "failed" ? "" : "active", "2", "媒体与轮次检查"],
    [asr.status === "passed" || packageState.settings.require_asr === false ? "done" : asr.status === "running" ? "active" : "", "3", "原声台词核验"],
    [complete ? "done" : assembly.status === "running" ? "active" : "", "4", "原声母版合成"],
    [applied ? "done" : complete ? "active" : "", "5", "应用真实时间线与场景版式"],
  ];
  for (const [stepState, number, label] of stepData) {
    steps.append(el("div", { class: `automation-step ${stepState}` }, el("strong", {}, number), el("span", {}, label)));
  }
  const actions = el("div", { class: "inline-actions" },
    button("进入数字人导入", "primary", () => { activeView = "avatar"; render(); }),
  );
  if (complete && assembly.output_path) {
    actions.append(el("a", { class: "button quiet", href: mediaURL(projectId, assembly.output_path), target: "_blank", rel: "noreferrer" }, "打开数字人母版"));
  }
  const issue = [...(validation.issues || []), ...(asr.issues || []), ...(assembly.issues || [])].find((item) => item.severity === "error");
  const body = el("div", { class: "panel-body automation-body" },
    steps,
    el("div", { class: `automation-progress${running ? " is-running" : ""}` },
      status(assembly.status === "running" ? "running" : applied ? "timeline_applied" : complete ? "passed" : asr.status || validation.status),
      el("span", {}, applied
        ? "数字人原声已经成为唯一主时间轴；系统不会再生成或叠加 TTS。现在可逐场选择全屏或画中画并审核实际关键帧。"
        : complete
          ? "原声母版已完成。请进入数字人素材页点击“应用为真实时间线”，再开始场景审核。"
        : avatarAssemblyProgressText(assembly)),
    ),
    assembly.status === "failed" ? el("div", { class: "report bad" }, avatarAssemblyIssueText(assembly)) : issue ? el("div", { class: "report bad" }, issue.message) : null,
    actions,
  );
  return el("section", { class: `panel automation-panel${compact ? " compact" : ""}` },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "数字人原声生产路径"), el("p", {}, "上传原片 → 校验轮次 → ASR 核词 → 合成母版"))),
    body,
  );
}

function renderAvatarImportRequired(compact) {
  return el("section", { class: `panel automation-panel${compact ? " compact" : ""}` },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "数字人原声生产路径"), el("p", {}, "先导入已生成的数字人视频，再由原声建立真实时间线。"))),
    el("div", { class: "panel-body automation-body" },
      el("div", { class: "automation-steps" },
        el("div", { class: "automation-step active" }, el("strong", {}, "1"), el("span", {}, "建立数字人导入合同")),
        el("div", { class: "automation-step" }, el("strong", {}, "2"), el("span", {}, "上传并核验原片")),
        el("div", { class: "automation-step" }, el("strong", {}, "3"), el("span", {}, "应用真实时间线和版式")),
      ),
      el("div", { class: "automation-progress" }, status("not_started"), el("span", {}, "此工作流不会自动生成通用旁白。请先导入带原生声音的数字人视频。")),
      el("div", { class: "inline-actions" }, button("进入数字人素材", "primary", () => { activeView = "avatar"; render(); })),
    ),
  );
}

function renderAutomationStatus(compact) {
  if (isAvatarProject()) return state.avatar_package ? renderNativeAvatarAutomation(compact) : renderAvatarImportRequired(compact);
  const automation = automationState();
  const assets = automation.asset_generation || {};
  const narration = automation.narration_generation || {};
  const renderJob = automation.render || {};
  const assetStatus = assets.status || "idle";
  const narrationStatus = narration.status || "idle";
  const renderStatus = renderJob.status || "idle";
  const hasAssets = assetStatus === "completed" || assetStatus === "completed_with_warnings";
  const needsDurationRefresh = assetStatus === "needs_duration_refresh";
  const timingIssues = Array.isArray(assets.timing_issues) ? assets.timing_issues : [];
  const hasNarration = narrationStatus === "completed" && narration.audio_path;
  const isDone = renderStatus === "completed" && renderJob.output_path;
  const isRunning = assetStatus === "generating" || narrationStatus === "generating" || renderStatus === "generating";
  const failure = (assets.error || narration.error || renderJob.error || "").trim();
  const actions = el("div", { class: "inline-actions" });
  let progress = "先生成自然语速的项目旁白；系统据此建立时间线，再按真实时长搜集和裁切画面。";

  if (assetStatus === "generating") {
    progress = "Pexels 素材：" + (assets.completed_scenes || 0) + "/" + (assets.total_scenes || 0) + " 个场景已完成";
  } else if (narrationStatus === "generating") {
    progress = "正在用通用默认音色生成项目旁白：" + (narration.completed_scenes || 0) + "/" + (narration.total_scenes || 0) + " 段已完成";
  } else if (renderStatus === "generating") {
    progress = "正在将已确认的旁白、字幕和已登记素材合成为视频。";
  } else if (hasNarration && needsDurationRefresh) {
    progress = `旁白已按自然时长更新；${timingIssues.length ? timingIssues.join("、") : "部分"}场景的已选画面不够长。请重新搜集匹配时长的素材后再合成。`;
  } else if (hasNarration && !hasAssets) {
    progress = "项目旁白、字幕和真实时间线已生成。请试听；满意后开始按每段实际时长搜集画面素材。";
  } else if (hasNarration && !isDone) {
    progress = "旁白、字幕和时长匹配素材均已就绪。请先试听；满意后再开始视频合成。";
  } else if (isDone) {
    progress = "成片、字幕、片段基线均已生成；现在可以只审核或替换某一个片段。";
  }

  if (!hasNarration) {
    actions.append(button(narrationStatus === "generating" ? "正在生成项目旁白…" : "生成项目旁白", "primary", startProjectNarration, isRunning));
    actions.append(button("打开通用配音中心", "quiet", openAudioCenter, isRunning));
  } else if (!hasAssets) {
    actions.append(el("a", { class: "button quiet", href: mediaURL(projectId, narration.audio_path), target: "_blank", rel: "noreferrer" }, "试听项目旁白"));
    actions.append(button(assetStatus === "generating" ? "正在按时长搜集素材…" : needsDurationRefresh ? "按新旁白时长更新素材" : "按旁白时长搜集素材", "primary", startNetworkAssetGeneration, isRunning));
    actions.append(button("打开通用配音中心", "quiet", openAudioCenter, isRunning));
  } else if (!isDone) {
    actions.append(el("a", { class: "button quiet", href: mediaURL(projectId, narration.audio_path), target: "_blank", rel: "noreferrer" }, "试听项目旁白"));
    actions.append(button(renderStatus === "generating" ? "正在合成视频…" : "开始合成视频", "primary", startProjectVideoRender, isRunning));
    actions.append(button("重新生成旁白并更新时间线", "quiet", startProjectNarration, isRunning));
    actions.append(button("打开通用配音中心", "quiet", openAudioCenter, isRunning));
  } else {
    actions.append(el("a", { class: "button primary", href: mediaURL(projectId, renderJob.output_path), target: "_blank", rel: "noreferrer" }, "打开完整成片"));
    actions.append(button("进入片段工作台", "", function () { activeView = "review"; render(); }));
    actions.append(button("重新生成旁白并更新时间线", "quiet", startProjectNarration, isRunning));
  }

  const firstState = hasNarration ? "done" : (narrationStatus === "generating" ? "active" : "");
  const secondState = hasAssets ? "done" : (assetStatus === "generating" ? "active" : "");
  const thirdState = isDone ? "done" : (renderStatus === "generating" ? "active" : "");
  const fourthState = isDone ? "active" : "";
  const steps = el("div", { class: "automation-steps" });
  steps.append(el("div", { class: "automation-step " + firstState }, el("strong", {}, "1"), el("span", {}, "项目旁白与真实时间线")));
  steps.append(el("div", { class: "automation-step " + secondState }, el("strong", {}, "2"), el("span", {}, "按旁白时长匹配 Pexels 素材")));
  steps.append(el("div", { class: "automation-step " + thirdState }, el("strong", {}, "3"), el("span", {}, "本地视频合成")));
  steps.append(el("div", { class: "automation-step " + fourthState }, el("strong", {}, "4"), el("span", {}, "关键帧与片段审核")));

  const header = el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "自动生产路径"), el("p", {}, "自然旁白 → 真实时间线 → 时长匹配素材 → 视频合成 → 片段审核")));
  const body = el("div", { class: "panel-body automation-body" });
  body.append(steps);
  body.append(el("div", { class: "automation-progress" + (isRunning ? " is-running" : "") }, status(narrationStatus === "generating" ? narrationStatus : (renderStatus === "generating" ? renderStatus : assetStatus)), el("span", {}, progress)));
  if (failure) body.append(el("div", { class: "report bad" }, failure));
  body.append(actions);
  return el("section", { class: "panel automation-panel" + (compact ? " compact" : "") }, header, body);
}

function taskCenterRunning() {
  return Number(taskCenter.active_count || 0) > 0;
}

function taskCenterWaiting() {
  return Number(taskCenter.waiting_count || 0) > 0;
}

function taskCenterSummary() {
  const parts = [];
  if (taskCenterRunning()) parts.push(`正在运行 ${Number(taskCenter.active_count || 0)} 项`);
  if (taskCenterWaiting()) parts.push(`${Number(taskCenter.waiting_count || 0)} 项等待人工确认`);
  if (Number(taskCenter.failure_count || 0) > 0) parts.push(`有 ${Number(taskCenter.failure_count || 0)} 项需处理`);
  return parts.length ? parts.join(" · ") : "当前没有运行中或等待人工确认的任务";
}

function taskCenterButtonLabel() {
  const parts = [];
  if (taskCenterRunning()) parts.push(`${Number(taskCenter.active_count || 0)} 项运行`);
  if (taskCenterWaiting()) parts.push(`${Number(taskCenter.waiting_count || 0)} 项等待人工确认`);
  return parts.length ? `任务中心 · ${parts.join(" · ")}` : "任务中心";
}

function taskProgressLabel(task) {
  const progress = task.progress || {};
  if (Number(progress.total || 0) > 0) return `${progress.completed || 0}/${progress.total}${progress.failed ? `，失败 ${progress.failed}` : ""}`;
  return task.status === "completed" ? "已完成" : "等待状态更新";
}

function renderTaskCenterIsland() {
  const tasks = Array.isArray(taskCenter.tasks) ? taskCenter.tasks : [];
  const summary = taskCenterSummary();
  const list = el("div", { class: "task-center-list" });
  if (!tasks.length) list.append(el("p", { class: "form-note" }, "尚无可追踪的项目任务。后续生成、下载、同步和合成都会显示在这里。"));
  for (const task of tasks.slice(0, 12)) {
    const actions = el("div", { class: "task-center-item-actions" });
    if (task.scene_id || task.target_view) actions.append(button("查看", "quiet small", () => {
      if (task.scene_id) selectedSceneId = task.scene_id;
      activeView = task.target_view || "review";
      taskCenterOpen = false;
      render();
    }));
    if (task.retry && task.retry.action === "retry_ppt_card") actions.append(button("重试", "primary small", async () => {
      try {
        state = await api(`/scenes/${encodeURIComponent(task.retry.scene_id)}/ppt-cards/jobs/current/retry`, { method: "POST", body: { job_id: task.retry.job_id } });
        ensureSelection(); render(); trackTaskCenter(); showToast("已重新提交 PPT 信息卡任务");
      } catch (error) { showToast(error.message || "重试失败", true); }
    }));
    list.append(el("article", { class: `task-center-item task-${task.status}` },
      el("div", { class: "task-center-item-main" },
        el("strong", {}, task.title || "未命名任务"),
        el("span", { class: "minor" }, task.stage || "处理中"),
        el("div", { class: "task-center-progress" }, el("span", { style: `width:${Math.round(Number((task.progress || {}).ratio || 0) * 100)}%` })),
        el("small", {}, `${statusLabels[task.status] || task.status} · ${taskProgressLabel(task)}`),
        task.error ? el("p", { class: "inline-error compact" }, task.error) : null,
      ),
      actions,
    ));
  }
  return el("section", { class: "task-center-popover", "data-task-center-island": "" },
    el("div", { class: "task-center-head" }, el("div", {}, el("strong", {}, "全局任务中心"), el("span", {}, summary)), button("关闭", "quiet small", () => { taskCenterOpen = false; render(); })),
    list,
    taskCenterResultsReady ? el("div", { class: "task-center-footer" }, button("加载已完成结果", "primary small", async () => {
      taskCenterResultsReady = false;
      await refresh({ force: true });
    })) : null,
  );
}

function updateTaskCenterIsland() {
  const previous = document.querySelector("[data-task-center-island]");
  if (previous && taskCenterOpen) previous.replaceWith(renderTaskCenterIsland());
}

function updateTaskCenterButton() {
  const control = document.querySelector("[data-task-center-button]");
  if (control) control.textContent = taskCenterButtonLabel();
}

async function pollTaskCenter() {
  if (taskCenterPollInFlight) return;
  taskCenterPollInFlight = true;
  try {
    const response = await fetch(`/api/project/${encodedProjectId}/workbench/tasks`);
    if (!response.ok) throw new Error("任务中心状态读取失败");
    const next = await response.json();
    const wasRunning = taskCenterRunning();
    taskCenter = next;
    if (wasRunning && !taskCenterRunning() && !taskCenterWaiting()) taskCenterResultsReady = true;
    updateTaskCenterButton();
    updateTaskCenterIsland();
    clearTimeout(taskCenterTimer);
    taskCenterTimer = taskCenterRunning() ? setTimeout(pollTaskCenter, 850) : null;
  } catch (error) {
    clearTimeout(taskCenterTimer);
    taskCenterTimer = taskCenterOpen ? setTimeout(pollTaskCenter, 2500) : null;
  } finally {
    taskCenterPollInFlight = false;
  }
}

function trackTaskCenter() {
  clearTimeout(taskCenterTimer);
  void pollTaskCenter();
}

async function refresh({ force = false } = {}) {
  if (refreshInFlight) {
    refreshQueued = true;
    return refreshInFlight;
  }
  refreshInFlight = (async () => {
  try {
    const initialLoad = !state;
    const [response, voicesResponse, musicResponse] = await Promise.all([
      fetch(`/api/project/${encodedProjectId}/workbench`),
      fetch(`/api/project/${encodedProjectId}/workbench/voices`).catch(() => null),
      fetch(`/api/project/${encodedProjectId}/workbench/music`).catch(() => null),
    ]);
    if (!response.ok) throw new Error("无法读取工作台数据");
    const nextState = await response.json();
    const nextVoiceCatalog = voicesResponse && voicesResponse.ok ? await voicesResponse.json() : voiceCatalog;
    const nextMusicCatalog = musicResponse && musicResponse.ok ? await musicResponse.json() : musicCatalog;
    const nextStateFingerprint = JSON.stringify(nextState);
    const nextVoiceCatalogFingerprint = JSON.stringify(nextVoiceCatalog);
    const nextMusicCatalogFingerprint = JSON.stringify(nextMusicCatalog);
    const shouldRender = !state || nextStateFingerprint !== stateFingerprint || nextVoiceCatalogFingerprint !== voiceCatalogFingerprint || nextMusicCatalogFingerprint !== musicCatalogFingerprint;
    const nextScriptIdentity = reviewPreviewScriptIdentity(nextState);
    const reviewPreviewScriptChanged = Boolean(reviewPreviewScriptIdentityValue && reviewPreviewScriptIdentityValue !== nextScriptIdentity);
    if (reviewPreviewScriptChanged) resetReviewPreviewForScriptChange();
    reviewPreviewScriptIdentityValue = nextScriptIdentity;
    const persistedRunningJob = (nextState.scenes || []).find((scene) => scene.keyframe_generation?.status === "generating");
    if (persistedRunningJob && (!keyframeJob || keyframeJobSceneId !== persistedRunningJob.id || keyframeJob.status !== "generating")) {
      trackKeyframeJob(persistedRunningJob.id, persistedRunningJob.keyframe_generation);
    }
    const persistedVisualBatch = ((nextState.automation || {}).visual_batch) || {};
    // SSE can arrive before the throttled status poll sees the final update.
    // Reconcile this small snapshot first so a stale local “0/N 生成中” job
    // never keeps deferring the completed project state indefinitely.
    if (persistedVisualBatch.job_id) {
      visualBatchJob = persistedVisualBatch;
      if (visualBatchFinished(persistedVisualBatch)) {
        clearTimeout(visualBatchTimer);
        visualBatchTimer = null;
      }
      updateVisualBatchIsland();
    }
    if (["queued", "generating"].includes(persistedVisualBatch.status) && !visualBatchTimer && !visualBatchPollInFlight) {
      trackVisualBatch(persistedVisualBatch);
    }
    const persistedPreviewSync = ((nextState.automation || {}).preview_sync) || {};
    if (persistedPreviewSync.job_id) {
      previewSyncJob = persistedPreviewSync;
      if (!["queued", "generating"].includes(persistedPreviewSync.status)) {
        clearTimeout(previewSyncTimer);
        previewSyncTimer = null;
      }
      updatePreviewSyncIsland();
    }
    if (["queued", "generating"].includes(persistedPreviewSync.status) && !previewSyncTimer && !previewSyncPollInFlight) {
      previewSyncTimer = setTimeout(pollPreviewSync, 350);
    }
    // A keyframe job deliberately updates through its small status endpoint.
    // The general project SSE also fires after each durable anchor write, but
    // rebuilding the review screen here would reset the video element and any
    // in-progress annotation. Defer the full state load until the reviewer
    // explicitly chooses “加载新结果”.
    const keyframeJobRunning = keyframeJobSceneId && keyframeJob && keyframeJob.status === "generating";
    const keyframeResultDeferred = keyframeJobSceneId && keyframeResultsReady;
    const visualBatchDeferred = visualBatchRunning() || visualBatchResultsReady;
    const previewSyncDeferred = previewSyncRunning();
    const shouldDeferState = !force && !initialLoad && (keyframeJobRunning || keyframeResultDeferred || visualBatchDeferred || previewSyncDeferred);
    if (!shouldDeferState) {
      state = nextState;
      voiceCatalog = nextVoiceCatalog;
      musicCatalog = nextMusicCatalog;
      stateFingerprint = nextStateFingerprint;
      voiceCatalogFingerprint = nextVoiceCatalogFingerprint;
      musicCatalogFingerprint = nextMusicCatalogFingerprint;
      ensureSelection();
      if (shouldRender) render();
    }
    if ((initialLoad || reviewPreviewScriptChanged) && supportsReviewPreview(nextState) && !reviewPreviewCurrentLoaded) {
      void pollReviewPreviewJob({ quiet: true });
    }
    if (initialLoad || taskCenterOpen || taskCenterRunning()) trackTaskCenter();
  } catch (error) {
    app.replaceChildren(el("main", { class: "workspace" }, el("div", { class: "empty" }, el("strong", {}, "工作台暂时不可用"), error.message)));
  }
  })();
  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
    if (refreshQueued) {
      refreshQueued = false;
      void refresh();
    }
  }
}

function ensureSelection() {
  if (!state) return;
  if (!state.scenes.some((scene) => scene.id === selectedSceneId)) selectedSceneId = state.scenes[0] ? state.scenes[0].id : null;
  if (!state.segments.some((segment) => segment.id === selectedSegmentId)) selectedSegmentId = state.segments[0] ? state.segments[0].id : null;
}

function button(label, className, handler, disabled = false) {
  return el("button", { class: `button ${className || ""}`, type: "button", disabled: disabled ? "" : null, onclick: handler }, label);
}

function scriptDraftGenerationStatusText() {
  const elapsedSeconds = Math.max(1, Math.floor((Date.now() - scriptDraftGenerationStartedAt) / 1000));
  return `正在请求已配置的文本模型，请勿重复点击（已等待 ${elapsedSeconds} 秒）`;
}

function updateScriptDraftGenerationFeedback() {
  document.querySelectorAll("[data-script-generation-submit]").forEach((control) => {
    control.disabled = scriptDraftGenerationInFlight;
    control.textContent = scriptDraftGenerationInFlight ? "正在生成草案…" : control.dataset.idleLabel;
  });
  document.querySelectorAll("[data-script-generation-control]").forEach((control) => {
    control.disabled = scriptDraftGenerationInFlight;
  });
  document.querySelectorAll("[data-script-generation-status]").forEach((node) => {
    node.hidden = !scriptDraftGenerationInFlight;
    node.textContent = scriptDraftGenerationInFlight ? scriptDraftGenerationStatusText() : "";
  });
}

function setScriptDraftGenerationInFlight(active) {
  scriptDraftGenerationInFlight = active;
  if (scriptDraftGenerationTimer !== null) window.clearInterval(scriptDraftGenerationTimer);
  scriptDraftGenerationTimer = null;
  if (active) {
    scriptDraftGenerationStartedAt = Date.now();
    scriptDraftGenerationTimer = window.setInterval(updateScriptDraftGenerationFeedback, 1000);
  }
  updateScriptDraftGenerationFeedback();
}

function pageHeader(eyebrow, title, description, action = null) {
  const heading = el("div", {}, el("p", { class: "eyebrow" }, eyebrow), el("h3", {}, title), description ? el("p", {}, description) : null);
  return el("header", { class: "page-header" }, heading, action);
}

function renderSidebar() {
  const navItems = [
    ["overview", "总览"], ["review", "片段工作台"], ["assets", "素材库"], ["quality", "成片与版本"],
  ];
  // 数字人口播项目在导入前也需要能够进入素材包页；不能等素材包
  // 已创建后才暴露入口，否则用户会被困在通用流程中。
  if (isAvatarProject()) navItems.splice(1, 0, ["avatar", "数字人素材"]);
  const nav = el("nav", { class: "nav", "aria-label": "工作台分区" });
  for (const [id, label] of navItems) {
    nav.append(el("button", { class: activeView === id ? "active" : "", type: "button", onclick: () => { activeView = id; render(); } }, label));
  }
  return el("aside", { class: "sidebar" },
    el("div", { class: "brand" }, el("div", { class: "brand-mark" }, "Haike Video / 海客视频工厂"), el("h1", {}, "海客视频工厂"), el("p", {}, "人审决策，而非手工剪辑")),
    nav,
    el("div", { class: "sidebar-foot" }, el("strong", {}, "当前原则"), el("br"), "按片段审核画面、字幕和配音；采用后只合成目标片段。"),
  );
}

function renderTopbar() {
  const project = state.project;
  const stateText = state.persisted ? "已保存到项目" : "预览草稿，尚未保存";
  const taskCenterButton = button(taskCenterButtonLabel(), "quiet", () => { taskCenterOpen = !taskCenterOpen; if (taskCenterOpen) trackTaskCenter(); render(); });
  taskCenterButton.dataset.taskCenterButton = "";
  return el("div", { class: "topbar-wrap" }, el("header", { class: "topbar" },
    el("a", { class: "back-link", href: "/" }, "项目库"),
    el("div", { class: "project-heading" }, el("p", { class: "eyebrow" }, "中文 AI 视频导演审核台"), el("h2", {}, project.title)),
    el("div", { class: "top-actions" },
      el("span", { class: "status editable" }, stateText),
      renderThemeToggle(),
      !state.persisted ? button("初始化并保存", "primary", () => mutate("/bootstrap", { method: "POST" }, "工作台已初始化")) : null,
      taskCenterButton,
      el("a", { class: "button quiet", href: `/audio?return=${encodeURIComponent(`/p/${projectId}`)}` }, "通用配音中心"),
      button("AI 配置", "quiet", openAIConfig),
      button("刷新", "quiet", refresh),
      el("a", { class: "button quiet", href: `/p/${encodedProjectId}/board` }, "旧版看板"),
    ),
  ), taskCenterOpen ? renderTaskCenterIsland() : null);
}

function renderMetric(label, value, detail) {
  return el("article", { class: "metric" }, el("span", { class: "label" }, label), el("strong", {}, value), el("div", { class: "detail" }, detail));
}

function scriptModeFromIntake(intake) {
  if (["organize_script", "expand_idea", "from_scratch"].includes(intake.script_mode)) return intake.script_mode;
  if (intake.script_status === "complete" || intake.script_status === "partial") return "organize_script";
  if (intake.script_status === "idea") return "expand_idea";
  return "from_scratch";
}

function scriptModeCopy(mode) {
  if (mode === "organize_script") return {
    hint: "保留原意和事实，把你粘贴的内容整理成可审核的分段脚本。",
    placeholder: "粘贴完整脚本或零散台词……",
    action: "整理成脚本草案",
    requiresText: true,
  };
  if (mode === "expand_idea") return {
    hint: "把一句主题、几个要点或一段粗略想法扩写成完整脚本。",
    placeholder: "例如：做一期普通人也能听懂的人形机器人热点杂谈……",
    action: "扩写成脚本草案",
    requiresText: true,
  };
  return {
    hint: "系统会围绕视频标题从零生成；你也可以补一句方向，让结果更贴近预期。",
    placeholder: "可选：补充主题、语气或必须出现的内容……",
    action: "生成脚本草案",
    requiresText: false,
  };
}

function renderScriptSection(section) {
  return el("article", { class: "script-draft-section" },
    el("div", { class: "script-draft-section-head" }, el("strong", {}, section.label || section.id), el("span", {}, `${clock(section.start_seconds)} — ${clock(section.end_seconds)}`)),
    el("p", {}, section.text),
    section.speaker_directions ? el("p", { class: "minor" }, `表达：${section.speaker_directions}`) : null,
    (section.enhancement_cues || []).length ? el("p", { class: "minor" }, `画面：${section.enhancement_cues.map((cue) => cue.description).join("；")}`) : null,
  );
}

function splitScriptSentences(text) {
  const normalized = String(text || "").replace(/\r/g, "").trim();
  if (!normalized) return [""];
  const matches = normalized.match(/[^。！？!?；;\n]+[。！？!?；;]?/g) || [normalized];
  const sentences = matches.map((item) => item.trim()).filter(Boolean);
  return sentences.length ? sentences : [""];
}

function scriptDraftEditableSections(script) {
  return (script.sections || []).map((section) => ({
    id: section.id || "",
    label: section.label || section.id || "正文",
    sentences: splitScriptSentences(section.text),
  }));
}

function renderScriptDraftEditor(draft) {
  const script = draft.script || {};
  const original = draft.original_script || null;
  let sections = scriptDraftEditableSections(script);
  let dirty = false;
  const titleInput = el("input", {
    value: script.title || state.project.title || "",
    maxlength: "200",
    "aria-label": "脚本标题",
  });
  const rows = el("div", { class: "script-editor-sections" });
  const saveButton = button("保存草案修改", "primary", async () => {
    const title = titleInput.value.trim();
    const payloadSections = sections.map((section) => ({
      id: section.id || undefined,
      label: String(section.label || "").trim(),
      sentences: section.sentences.map((sentence) => String(sentence || "").trim()),
    }));
    if (!title) {
      titleInput.focus();
      showToast("脚本标题不能为空", true);
      return;
    }
    if (payloadSections.some((section) => !section.sentences.length || section.sentences.some((sentence) => !sentence))) {
      showToast("请先补全空白句子，或删除不需要的句子", true);
      return;
    }
    await mutate("/script-draft/content", {
      method: "PATCH",
      body: { expected_revision: draft.revision, title, sections: payloadSections },
    }, "脚本草案修改已保存");
  });
  const dirtyNote = el("span", { class: "script-editor-dirty", "aria-live": "polite" }, "修改只在点击保存后生效");
  const markDirty = () => {
    dirty = true;
    dirtyNote.textContent = "有未保存修改";
    saveButton.disabled = false;
  };
  titleInput.addEventListener("input", markDirty);

  const renderRows = () => {
    rows.replaceChildren();
    sections.forEach((section, sectionIndex) => {
      const labelInput = el("input", {
        value: section.label,
        maxlength: "120",
        "aria-label": `第 ${sectionIndex + 1} 段标题`,
        oninput: () => { section.label = labelInput.value; markDirty(); },
      });
      const sentenceList = el("div", { class: "script-sentence-list" });
      section.sentences.forEach((sentence, sentenceIndex) => {
        const sentenceInput = el("textarea", {
          rows: "2",
          maxlength: "4000",
          "aria-label": `第 ${sectionIndex + 1} 段第 ${sentenceIndex + 1} 句`,
          oninput: () => { section.sentences[sentenceIndex] = sentenceInput.value; markDirty(); },
        }, sentence);
        sentenceList.append(el("div", { class: "script-sentence-row" },
          el("span", { class: "script-sentence-number" }, `${sentenceIndex + 1}`),
          sentenceInput,
          el("div", { class: "script-sentence-actions" },
            button("上移", "quiet compact", () => {
              [section.sentences[sentenceIndex - 1], section.sentences[sentenceIndex]] = [section.sentences[sentenceIndex], section.sentences[sentenceIndex - 1]];
              markDirty(); renderRows();
            }, sentenceIndex === 0),
            button("下移", "quiet compact", () => {
              [section.sentences[sentenceIndex + 1], section.sentences[sentenceIndex]] = [section.sentences[sentenceIndex], section.sentences[sentenceIndex + 1]];
              markDirty(); renderRows();
            }, sentenceIndex === section.sentences.length - 1),
            button("在下方添加", "quiet compact", () => {
              section.sentences.splice(sentenceIndex + 1, 0, "");
              markDirty(); renderRows();
            }),
            button("删除", "danger compact", () => {
              if (section.sentences.length <= 1) return;
              section.sentences.splice(sentenceIndex, 1);
              markDirty(); renderRows();
            }, section.sentences.length <= 1),
          ),
        ));
      });
      rows.append(el("article", { class: "script-edit-section" },
        el("div", { class: "script-edit-section-head" },
          el("label", {}, el("span", {}, `第 ${sectionIndex + 1} 段`), labelInput),
          el("div", { class: "inline-actions" },
            button("段落上移", "quiet compact", () => {
              [sections[sectionIndex - 1], sections[sectionIndex]] = [sections[sectionIndex], sections[sectionIndex - 1]];
              markDirty(); renderRows();
            }, sectionIndex === 0),
            button("段落下移", "quiet compact", () => {
              [sections[sectionIndex + 1], sections[sectionIndex]] = [sections[sectionIndex], sections[sectionIndex + 1]];
              markDirty(); renderRows();
            }, sectionIndex === sections.length - 1),
            button("删除段落", "danger compact", () => {
              if (sections.length <= 1) return;
              sections.splice(sectionIndex, 1);
              markDirty(); renderRows();
            }, sections.length <= 1),
          ),
        ),
        sentenceList,
        button("添加一句", "quiet compact", () => {
          section.sentences.push("");
          markDirty(); renderRows();
        }),
      ));
    });
  };
  renderRows();

  const restore = original ? button("恢复 AI 原稿", "quiet", () => {
    if (!window.confirm("恢复后会替换当前尚未保存的编辑内容，但仍需点击“保存草案修改”才会写入项目。是否继续？")) return;
    titleInput.value = original.title || script.title || "";
    sections = scriptDraftEditableSections(original);
    markDirty();
    renderRows();
  }) : null;
  const addSection = button("添加段落", "quiet", () => {
    sections.push({ id: "", label: `新段落 ${sections.length + 1}`, sentences: [""] });
    markDirty();
    renderRows();
  });
  saveButton.disabled = !dirty;
  return el("section", { class: "script-draft-editor", "aria-label": "逐句编辑脚本草案" },
    el("div", { class: "script-editor-head" },
      el("div", {}, el("strong", {}, "逐句编辑"), el("p", {}, "可逐句增删、改写和排序；保存不会再次调用大模型。")),
      el("span", { class: "fact" }, `草案版本 ${draft.revision || 1}`),
    ),
    el("label", { class: "script-editor-title" }, el("span", {}, "脚本标题"), titleInput),
    rows,
    el("div", { class: "script-editor-footer" },
      el("div", { class: "inline-actions" }, addSection, restore),
      el("div", { class: "script-editor-save" }, dirtyNote, saveButton),
    ),
  );
}

function renderScriptGeneratorForm(intake, revisionDraft = null) {
  const mode = (revisionDraft && revisionDraft.mode) || scriptModeFromIntake(intake);
  const sourceValue = intake.source_text || intake.script_text || intake.idea || intake.brief || "";
  const titleInput = el("input", {
    name: "video_title",
    value: intake.video_title || state.project.title || "",
    maxlength: "200",
    placeholder: "输入视频标题",
    autocomplete: "off",
    "data-script-generation-control": "",
    disabled: scriptDraftGenerationInFlight ? "" : null,
  });
  const sourceInput = el("textarea", {
    name: "source_text",
    rows: "10",
    maxlength: "20000",
    "data-script-generation-control": "",
    disabled: scriptDraftGenerationInFlight ? "" : null,
  }, sourceValue);
  const modeSelect = el("select", {
    name: "mode",
    "aria-label": "本次脚本处理方式",
    "data-script-generation-control": "",
    disabled: scriptDraftGenerationInFlight ? "" : null,
  });
  for (const [value, label] of [
    ["organize_script", "整理已有脚本"],
    ["expand_idea", "扩写简单想法"],
    ["from_scratch", "从零生成脚本"],
  ]) modeSelect.append(el("option", { value }, label));
  modeSelect.value = mode;
  const organizeStrength = el("select", {
    name: "organize_strength",
    "aria-label": "整理强度",
    "data-script-generation-control": "",
    disabled: scriptDraftGenerationInFlight ? "" : null,
  },
    el("option", { value: "faithful" }, "忠实整理（推荐）"),
    el("option", { value: "light_polish" }, "轻度润色"),
  );
  organizeStrength.value = (revisionDraft && revisionDraft.organize_strength) || intake.organize_strength || "faithful";
  const organizeStrengthField = el("label", { class: "script-mode-field script-organize-strength" },
    el("span", {}, "整理强度"), organizeStrength,
    el("small", {}, "忠实整理只修断句、错字和明显语病；轻度润色允许补少量衔接，但不会新增事实。"),
  );
  const modeHint = el("p", { class: "script-mode-hint" });
  const generationStatus = el("p", {
    class: "script-generation-status",
    role: "status",
    "aria-live": "polite",
    "data-script-generation-status": "",
    hidden: scriptDraftGenerationInFlight ? null : "",
  }, scriptDraftGenerationInFlight ? scriptDraftGenerationStatusText() : "");
  const idleSubmitLabel = revisionDraft ? "按意见重新生成草案" : "生成脚本草案";
  const submit = button(scriptDraftGenerationInFlight ? "正在生成草案…" : idleSubmitLabel, "primary script-generate-button", async () => {
    if (scriptDraftGenerationInFlight) return;
    const selectedMode = modeSelect.value;
    const copy = scriptModeCopy(selectedMode);
    const videoTitle = titleInput.value.trim();
    const sourceText = sourceInput.value.trim();
    if (!videoTitle) {
      titleInput.focus();
      showToast("请填写视频标题", true);
      return;
    }
    if (copy.requiresText && !sourceText) {
      sourceInput.focus();
      showToast(selectedMode === "organize_script" ? "请粘贴需要整理的脚本" : "请先输入一个简单想法", true);
      return;
    }
    if (!window.confirm("即将调用项目内置大模型生成脚本草案，可能产生 API 费用；生成后仍需人工审核。是否继续？")) return;
    setScriptDraftGenerationInFlight(true);
    try {
      await mutate("/script-draft", {
        method: "POST",
        body: { mode: selectedMode, organize_strength: organizeStrength.value, video_title: videoTitle, source_text: sourceText, confirmed: true },
      }, revisionDraft ? "已保留原输入并重新生成草案，请再次审核" : "脚本草案已生成，请逐段审核");
    } finally {
      // On success this form has already been replaced by the draft editor;
      // on failure, restore its controls without discarding the user's input.
      setScriptDraftGenerationInFlight(false);
    }
  }, scriptDraftGenerationInFlight);
  submit.dataset.scriptGenerationSubmit = "";
  submit.dataset.idleLabel = idleSubmitLabel;
  const syncModeCopy = () => {
    const copy = scriptModeCopy(modeSelect.value);
    modeHint.textContent = copy.hint;
    sourceInput.placeholder = copy.placeholder;
    if (!scriptDraftGenerationInFlight) {
      const label = revisionDraft ? `按意见${copy.action}` : copy.action;
      submit.textContent = label;
      submit.dataset.idleLabel = label;
    }
    organizeStrengthField.hidden = modeSelect.value !== "organize_script";
  };
  modeSelect.addEventListener("change", syncModeCopy);
  syncModeCopy();
  return el("form", { class: `script-generator-form${revisionDraft ? " revision-regenerator" : ""}`, onsubmit: (event) => event.preventDefault() },
    revisionDraft ? el("div", { class: "revision-preserved-note" },
      el("strong", {}, "已保留视频标题、原输入和审核意见"),
      el("p", {}, "可直接修改下面的原输入，或保持不变并按审核意见重新生成；旧草案仍保留在上方供对照。"),
    ) : null,
    el("label", { class: "script-primary-field" }, el("span", {}, "视频标题"), titleInput),
    el("label", { class: "script-primary-field" }, el("span", {}, "输入脚本/简单想法"), sourceInput),
    el("div", { class: "script-mode-block" },
      el("label", { class: "script-mode-field" }, el("span", {}, "本次脚本处理方式"), modeSelect),
      organizeStrengthField,
      modeHint,
    ),
    submit,
    generationStatus,
    el("p", { class: "script-review-note" }, revisionDraft ? `本轮审核意见：${revisionDraft.review_note || "无"}` : "大模型只生成草案，正式使用前仍需人工审核。"),
  );
}

function renderScriptStudio(intake) {
  const draft = state.project.script_draft;
  const panelBody = el("div", { class: "panel-body" });
  if (draft && draft.script) {
    const script = draft.script;
    panelBody.append(
      el("div", { class: "draft-meta" },
        status(draft.status === "approved" ? "approved" : draft.status === "revision_requested" ? "needs_adjustment" : "pending"),
        el("span", { class: "fact" }, `总时长：${script.total_duration_seconds || "—"} 秒`),
        el("span", { class: "fact" }, `分段：${(script.sections || []).length} 段`),
        draft.model ? el("span", { class: "fact" }, `模型：${draft.model}`) : null,
      ),
      draft.review_note ? el("p", { class: "report bad" }, `修改意见：${draft.review_note}`) : null,
      draft.status === "approved"
        ? el("div", { class: "script-draft-list" }, (script.sections || []).map(renderScriptSection))
        : renderScriptDraftEditor(draft),
    );
    if (draft.status === "approved") {
      const parentJob = ((state.automation || {}).review_preview_pipeline || {});
      const canReopen = !state.scenes.length && !parentJob.job_id
        && !["queued", "running", "awaiting_human", "completed"].includes(String(parentJob.status || ""));
      if (canReopen) panelBody.append(el("div", { class: "script-approved-edit" },
        el("div", {}, el("strong", {}, "还需要调整文字？"), el("p", {}, "下游制作尚未开始，可以安全地重新打开当前脚本。")),
        button("重新编辑脚本", "quiet", () => mutate("/script-draft/reopen", {
          method: "POST", body: { expected_revision: draft.revision },
        }, "脚本已重新打开，请编辑并再次通过")),
      ));
      if (supportsReviewPreview()) {
        panelBody.append(renderReviewPreviewPanel());
        if (isAvatarProject()) {
          panelBody.append(el("div", { class: "script-next-step" },
            el("div", {}, el("strong", {}, "需要查看数字人诊断？"), el("p", {}, "一键父任务会按精确帧清单自动切割；Whisper 只保留诊断。旧素材或清单异常时再进入高级素材页处理。")),
            button("进入数字人素材（高级）", "quiet", () => { activeView = "avatar"; render(); }),
          ));
        }
      } else {
        // Preserve the existing avatar preparation path. The no-avatar parent
        // job above must never alter, initialize, or call this branch.
        panelBody.append(
          el("div", { class: "script-next-step" },
            el("div", {}, el("strong", {}, state.scenes.length ? "分镜草案已就绪" : "下一步：生成分镜草案"), el("p", {}, state.scenes.length ? (isAvatarProject() ? "先导入数字人原片并应用真实时间线，再逐场审核版式、关键帧和主体素材。" : "现在可以逐场审核首帧、高潮帧和素材来源。") : "系统会按脚本分段生成场景，不会直接生成或消耗素材。")),
            state.scenes.length
              ? button(isAvatarProject() ? "进入数字人素材" : "进入场景审核", "primary", () => { activeView = isAvatarProject() ? "avatar" : "review"; render(); })
              : button("生成分镜草案", "primary", async () => {
                try {
                  state = await api("/scene-plan", { method: "POST" });
                  ensureSelection();
                  activeView = isAvatarProject() ? "avatar" : "review";
                  render();
                  showToast(isAvatarProject() ? "分镜草案已生成。下一步请导入逐段数字人原片并建立真实时间线。" : "分镜草案已生成，请先审核首帧和高潮帧");
                } catch (error) { showToast(error.message || "分镜草案生成失败", true); }
              }),
          ),
        );
      }
    }
    if (draft.status !== "approved" && draft.status !== "revision_requested") {
      const note = el("textarea", { class: "revision-note", rows: "3", placeholder: "如果需要修改，请写清楚哪一段、哪里不满意、希望如何表达。" });
      panelBody.append(el("div", { class: "script-review-actions" },
        button("通过脚本草案", "primary", () => mutate("/script-draft/review", { method: "POST", body: { action: "approve", expected_revision: draft.revision } }, "脚本草案已通过")),
        el("div", { class: "revision-action" }, note, button("退回并记录意见", "", () => mutate("/script-draft/review", { method: "POST", body: { action: "request_revision", note: note.value, expected_revision: draft.revision } }, "已记录脚本修改意见"))),
      ));
    }
    if (draft.status === "revision_requested") panelBody.append(renderScriptGeneratorForm(intake, draft));
  } else {
    panelBody.append(renderScriptGeneratorForm(intake));
  }
  return el("section", { class: "panel script-studio" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, draft ? "脚本草案审核" : "脚本内容"), el("p", {}, draft ? "先看表达，再决定通过还是退回修改。" : "只需要标题和内容，其余细节在后续工作台处理。"))),
    panelBody,
  );
}

function renderProjectLaunchpad() {
  const intake = state.project.intake || {};
  const action = state.persisted
    ? el("a", { class: "button quiet", href: "/" }, "返回项目库")
    : button("确认并保存工作台", "primary", () => mutate("/bootstrap", { method: "POST" }, "项目工作台已保存"));
  return el("section", { class: "page script-launchpad" },
    pageHeader("脚本工作台", "从一句想法，到可审核脚本", "输入标题和内容，再选择内置大模型的处理方式。", action),
    el("div", { class: "script-launchpad-column" }, renderScriptStudio(intake)),
  );
}

function renderOverview() {
  if (!state.scenes.length) return renderProjectLaunchpad();
  const reviewed = state.scenes.filter((scene) => scene.review_status === "approved").length;
  const frozen = state.segments.filter((segment) => segment.state === "frozen").length;
  const sourceKnown = state.scenes.filter((scene) => scene.source_strategy !== "undecided").length;
  const activePatches = state.patches.filter((patch) => !["promoted", "rolled_back"].includes(patch.status)).length;
  const avatarNeedsImport = isAvatarProject() && !avatarTimelineApplied();
  const metrics = el("section", { class: "metric-grid" },
    renderMetric("场景审核", `${reviewed}/${state.scenes.length}`, "已通过 / 全部场景"),
    renderMetric("素材台账", `${state.assets.length}`, `${state.usages.length} 次已登记使用`),
    renderMetric("冻结片段", `${frozen}/${state.segments.length}`, "A/C 版本边界可追溯"),
    renderMetric("片段候选", `${activePatches}`, "待试听或待合并的局部版本"),
  );
  const next = state.scenes.find((scene) => scene.review_status !== "approved");
  const actions = el("div", { class: "inline-actions" },
    button(avatarNeedsImport ? "进入数字人素材" : "进入片段工作台", isNoAvatarProject() ? "quiet" : "primary", () => { activeView = avatarNeedsImport ? "avatar" : "review"; if (!avatarNeedsImport && next) selectedSceneId = next.id; render(); }),
    button("登记素材", "", () => assetDialog.showModal()),
    button("AI 生图", "", () => imageDialog.showModal()),
  );
  const process = el("div", { class: "policy-list" },
    policy("1. 选来源", "每个场景先决定人工提供、网络下载、项目库或生成。"),
    policy("2. 核对锚点", "以首帧、高潮帧、出场帧为最小审核单位，留下教师式批注。"),
    policy("3. 锁定合格段", "冻结完成片段的版本与边界；后续修改不牵动 A/C。"),
    policy("4. 局部收口", "试听、采用并仅合成目标 B；确认后才原子合并到成片。"),
  );
  return el("section", { class: "page" },
    pageHeader("导演总览", "从脚本意图到可交付成片", "这里管理判断与交接，不提供逐帧手工剪辑。", actions),
    metrics,
    renderReviewPreviewPanel(),
    renderFullPreviewPanel(),
    renderBackgroundMusicPanel(),
    renderAutomationStatus(),
    el("div", { class: "grid-2" },
      el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "本轮工作路径"), el("p", {}, "每一步都能回查到素材、使用与版本。"))), el("div", { class: "panel-body" }, process)),
      el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "下一项待处理"), el("p", {}, next ? sceneName(next) : "所有场景均已通过"))), el("div", { class: "panel-body" }, next ? el("div", {}, el("p", {}, next.description || "此场景尚未补充画面说明。"), status(next.review_status)) : el("div", { class: "empty" }, "可以开始冻结已确认片段。"))),
    ),
    renderTimelinePanel(),
  );
}

function renderGainControl({ label, value, min, max, step, ariaLabel, help }) {
  const input = el("input", { type: "range", min: String(min), max: String(max), step: String(step), value: String(value), "aria-label": ariaLabel });
  const output = el("strong", { class: "music-gain-value" }, `${Number(value) > 0 ? "+" : ""}${value} dB`);
  input.addEventListener("input", () => { output.textContent = `${Number(input.value) > 0 ? "+" : ""}${input.value} dB`; });
  return {
    input,
    node: el("label", { class: "field music-gain-control" }, el("span", {}, label), el("div", { class: "music-gain-row" }, input, output), el("small", {}, help)),
  };
}

function updateMusicUploadIsland() {
  const progress = document.querySelector("[data-music-upload-progress]");
  const label = document.querySelector("[data-music-upload-label]");
  if (progress) progress.value = Math.max(0, Math.min(100, Number(musicUploadState.progress || 0)));
  if (label) {
    label.textContent = musicUploadState.status === "uploading"
      ? `正在上传 ${musicUploadState.filename}：${Math.round(musicUploadState.progress || 0)}%`
      : musicUploadState.status === "failed"
        ? `上传失败：${musicUploadState.error || "请检查音频文件"}`
        : musicUploadState.status === "completed"
          ? `${musicUploadState.filename} 已加入本项目曲库`
          : "支持 WAV、MP3、M4A、AAC、FLAC、OGG、OPUS、AIFF，最大 100MB";
  }
}

function uploadProjectMusic(file) {
  if (!file || musicUploadState.status === "uploading") return;
  if (file.size > 100 * 1024 * 1024) {
    showToast("背景音乐文件不能超过 100MB", true);
    return;
  }
  musicUploadState = { status: "uploading", progress: 0, filename: file.name, error: "" };
  render();
  const request = new XMLHttpRequest();
  request.open("POST", `/api/project/${encodedProjectId}/workbench/music/uploads?filename=${encodeURIComponent(file.name)}`);
  request.setRequestHeader("Content-Type", "application/octet-stream");
  request.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    musicUploadState.progress = Math.round((event.loaded / event.total) * 100);
    updateMusicUploadIsland();
  });
  request.addEventListener("load", () => {
    let payload = {};
    try { payload = JSON.parse(request.responseText || "{}"); } catch (error) { /* handled below */ }
    if (request.status < 200 || request.status >= 300) {
      musicUploadState = { status: "failed", progress: 0, filename: file.name, error: payload.detail || "背景音乐上传失败" };
      render();
      showToast(musicUploadState.error, true);
      return;
    }
    if (payload.catalog) musicCatalog = payload.catalog;
    uploadedMusicTrackId = (payload.track || {}).id || "";
    musicUploadState = { status: "completed", progress: 100, filename: file.name, error: "" };
    render();
    showToast("本地音乐已安全加入当前项目，请选择区间并保存声音设置。", false);
  });
  request.addEventListener("error", () => {
    musicUploadState = { status: "failed", progress: 0, filename: file.name, error: "网络连接中断，请重试" };
    render();
    showToast(musicUploadState.error, true);
  });
  request.send(file);
}

function renderBackgroundMusicPanel() {
  const tracks = Array.isArray(musicCatalog.tracks) ? musicCatalog.tracks : [];
  const defaults = musicCatalog.defaults || { playback_gain_db: -8 };
  const narrationDefaults = musicCatalog.narration_defaults || { playback_gain_db: 0 };
  const policy = Object.assign({ enabled: false, track_id: null, playback_gain_db: defaults.playback_gain_db, source_start_seconds: 0, source_end_seconds: null, sample: {} }, state.music_policy || musicCatalog.policy || {});
  const narrationPolicy = Object.assign({ playback_gain_db: narrationDefaults.playback_gain_db }, state.narration_policy || musicCatalog.narration_policy || {});
  const sample = Object.assign({ status: "idle", output_path: null, scene_id: null, error: "", stale_reason: "" }, policy.sample || {});
  const selectedId = uploadedMusicTrackId || policy.track_id || (tracks[0] && tracks[0].id) || "";
  const enabled = el("input", { type: "checkbox", checked: policy.enabled ? "" : null, "aria-label": "为全片添加背景音乐" });
  const select = el("select", { "aria-label": "新闻背景音乐" });
  const sourceStart = el("input", { type: "number", min: "0", step: "0.1", value: String(policy.source_start_seconds || 0), "aria-label": "背景音乐选区起点" });
  const sourceEnd = el("input", { type: "number", min: "0", step: "0.1", value: String(policy.source_end_seconds ?? 0), "aria-label": "背景音乐选区终点" });
  const rangeStatus = el("span", { class: "music-range-status", "data-music-range-status": "" });
  const uploadInput = el("input", {
    type: "file",
    accept: ".wav,.mp3,.m4a,.aac,.flac,.ogg,.opus,.aiff,.aif,audio/*",
    disabled: musicUploadState.status === "uploading" ? "" : null,
    "aria-label": "上传本地背景音乐",
  });
  uploadInput.addEventListener("change", () => {
    if (uploadInput.files && uploadInput.files[0]) uploadProjectMusic(uploadInput.files[0]);
  });
  const uploadBox = el("div", { class: "music-upload-box" },
    el("div", { class: "music-upload-actions" },
      el("label", { class: `button quiet music-upload-button${musicUploadState.status === "uploading" ? " disabled" : ""}` },
        musicUploadState.status === "uploading" ? "正在上传…" : "上传本地音乐",
        uploadInput,
      ),
      el("span", { class: "muted", "data-music-upload-label": "" },
        musicUploadState.status === "uploading"
          ? `正在上传 ${musicUploadState.filename}：${Math.round(musicUploadState.progress || 0)}%`
          : musicUploadState.status === "failed"
            ? `上传失败：${musicUploadState.error || "请检查音频文件"}`
            : musicUploadState.status === "completed"
              ? `${musicUploadState.filename} 已加入本项目曲库`
              : "支持常见音频格式，最大 100MB",
      ),
    ),
    el("progress", { max: "100", value: String(musicUploadState.progress || 0), "data-music-upload-progress": "" }),
  );
  const gainValue = Math.max(-24, Math.min(0, Number(policy.playback_gain_db ?? defaults.playback_gain_db ?? -8)));
  const narrationGainValue = Math.max(-12, Math.min(12, Number(narrationPolicy.playback_gain_db ?? narrationDefaults.playback_gain_db ?? 0)));
  const narrationGain = renderGainControl({ label: "人物台词音量", value: narrationGainValue, min: -12, max: 12, step: .5, ariaLabel: "人物台词音量", help: "只调整人物相对背景音乐的强弱；不会覆盖本地配音音频或数字人原片。" });
  const musicGain = renderGainControl({ label: "背景音乐相对人声音量", value: gainValue, min: -24, max: 0, step: 1, ariaLabel: "背景音乐混音音量", help: "建议从 -8 dB 开始。最终响度归一化会保留人物与音乐的相对比例。" });
  if (!tracks.length) select.append(el("option", { value: "" }, "暂无可用新闻背景音乐"));
  for (const track of tracks) select.append(el("option", { value: track.id }, `${track.scope === "project" ? "本项目 · " : "内置 · "}${track.title} · ${fmtDuration(track.duration_seconds || 0)}`));
  select.value = selectedId;
  const detail = el("div", { class: "music-track-detail" });
  let currentTrack = null;
  let previewingRange = false;
  const updateRangeStatus = () => {
    const start = Number(sourceStart.value);
    const end = Number(sourceEnd.value);
    const duration = Number((currentTrack || {}).duration_seconds || 0);
    const valid = Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end > start && end <= duration + .01 && end - start >= 1;
    rangeStatus.textContent = valid
      ? `将循环使用 ${fmtDuration(start)} — ${fmtDuration(end)}（${fmtDuration(end - start)}）`
      : "选区至少 1 秒，且不能超过音轨时长";
    rangeStatus.classList.toggle("invalid", !valid);
    return valid;
  };
  const updateDetail = ({ resetRange = false } = {}) => {
    const track = tracks.find((item) => item.id === select.value);
    currentTrack = track || null;
    previewingRange = false;
    detail.replaceChildren();
    if (!track) {
      detail.append(el("div", { class: "empty" }, (musicCatalog.errors || [])[0] || "请将可解码的新闻音乐放入 song 文件夹。"));
      return;
    }
    const duration = Math.max(0, Number(track.duration_seconds || 0));
    const useSavedRange = !resetRange && track.id === policy.track_id && uploadedMusicTrackId !== track.id;
    if (!useSavedRange) {
      sourceStart.value = "0";
      sourceEnd.value = String(duration);
    } else if (!Number(policy.source_end_seconds)) {
      sourceEnd.value = String(duration);
    }
    sourceStart.max = String(duration);
    sourceEnd.max = String(duration);
    const mediaPath = `/api/project/${encodedProjectId}/workbench/${track.media_url}`;
    const player = el("audio", { controls: "", preload: "metadata", src: mediaPath });
    player.addEventListener("timeupdate", () => {
      if (previewingRange && player.currentTime >= Number(sourceEnd.value)) {
        player.pause();
        player.currentTime = Number(sourceStart.value) || 0;
        previewingRange = false;
      }
    });
    const setStart = button("当前播放位置设为起点", "quiet small", () => {
      sourceStart.value = Math.min(duration, Math.max(0, player.currentTime || 0)).toFixed(1);
      updateRangeStatus();
    });
    const setEnd = button("当前播放位置设为终点", "quiet small", () => {
      sourceEnd.value = Math.min(duration, Math.max(0, player.currentTime || 0)).toFixed(1);
      updateRangeStatus();
    });
    const wholeTrack = button("使用整首", "quiet small", () => {
      sourceStart.value = "0";
      sourceEnd.value = String(duration);
      updateRangeStatus();
    });
    const previewRange = button("试听选区", "quiet small", () => {
      if (!updateRangeStatus()) return showToast("请先设置一个有效的音乐选区", true);
      player.currentTime = Number(sourceStart.value) || 0;
      previewingRange = true;
      const playback = player.play();
      if (playback && playback.catch) playback.catch(() => showToast("浏览器未能开始播放，请点击播放器后重试", true));
    });
    detail.append(
      player,
      el("p", { class: "muted" }, `${track.sample_rate ? `${Math.round(track.sample_rate / 1000)} kHz` : "未知采样率"} · ${track.channels === 2 ? "双声道" : `${track.channels || 0} 声道`} · ${track.codec || "未知编码"}`),
      el("div", { class: "music-range-grid" },
        el("label", { class: "field" }, el("span", {}, "选区起点（秒）"), sourceStart),
        el("label", { class: "field" }, el("span", {}, "选区终点（秒）"), sourceEnd),
      ),
      el("div", { class: "music-range-actions" }, setStart, setEnd, wholeTrack, previewRange),
      rangeStatus,
      el("p", { class: "music-calibration" }, track.calibration_note || "合成时保持当前文件响度"),
      el("p", { class: "muted" }, track.license_notice || "发布前请确认音乐授权。"),
    );
    updateRangeStatus();
  };
  sourceStart.addEventListener("input", updateRangeStatus);
  sourceEnd.addEventListener("input", updateRangeStatus);
  select.addEventListener("change", () => {
    uploadedMusicTrackId = "";
    updateDetail({ resetRange: true });
  });
  updateDetail({ resetRange: selectedId !== policy.track_id || uploadedMusicTrackId === selectedId });
  const savePolicy = async ({ sampleAfterSave = false } = {}) => {
    try {
      state = await api("/narration-policy", { method: "PUT", body: { playback_gain_db: Number(narrationGain.input.value) } });
      state = await api("/music-policy", { method: "PUT", body: {
        enabled: enabled.checked,
        track_id: select.value,
        playback_gain_db: Number(musicGain.input.value),
        source_start_seconds: Number(sourceStart.value),
        source_end_seconds: Number(sourceEnd.value),
      } });
      if (sampleAfterSave) {
        state = await api("/music-sample/jobs", { method: "POST", body: {} });
      }
      musicCatalog.policy = state.music_policy;
      musicCatalog.narration_policy = state.narration_policy;
      uploadedMusicTrackId = "";
      stateFingerprint = JSON.stringify(state);
      render();
      showToast(sampleAfterSave ? "第一段声音样板已开始生成；完成后请试听人物与音乐比例。" : "全片声音设置已保存；变更后需要重新生成第一段样板。");
    } catch (error) {
      showToast(error.message || "全片声音设置保存失败", true);
    }
  };
  const unavailableTrack = enabled.checked && !tracks.length;
  const save = button("保存本项目声音设置", "quiet", () => savePolicy(), unavailableTrack);
  const generateSample = button(sample.status === "generating" ? "正在生成声音样板…" : "保存并生成第 1 段声音样板", "primary", () => savePolicy({ sampleAfterSave: true }), unavailableTrack || sample.status === "generating");
  const saveMusicAsDefault = button(`设为以后默认音乐音量（${gainValue} dB）`, "quiet", async () => {
    try {
      const response = await fetch("/api/workbench/music-defaults", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ playback_gain_db: Number(musicGain.input.value) }),
      });
      if (!response.ok) {
        let detail = "默认音量保存失败";
        try { detail = (await response.json()).detail || detail; } catch (error) { /* ignored */ }
        throw new Error(detail);
      }
      musicCatalog.defaults = await response.json();
      showToast(`已将 ${Number(musicGain.input.value)} dB 保存为以后新项目的默认背景音乐音量。`);
      render();
    } catch (error) { showToast(error.message || "默认音量保存失败", true); }
  }, !tracks.length);
  const saveNarrationAsDefault = button(`设为以后默认人物音量（${narrationGainValue >= 0 ? "+" : ""}${narrationGainValue} dB）`, "quiet", async () => {
    try {
      const response = await fetch("/api/workbench/narration-defaults", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ playback_gain_db: Number(narrationGain.input.value) }),
      });
      if (!response.ok) {
        let detail = "默认人物音量保存失败";
        try { detail = (await response.json()).detail || detail; } catch (error) { /* ignored */ }
        throw new Error(detail);
      }
      musicCatalog.narration_defaults = await response.json();
      showToast(`已将 ${Number(narrationGain.input.value) > 0 ? "+" : ""}${Number(narrationGain.input.value)} dB 保存为以后新项目的默认人物音量。`);
      render();
    } catch (error) { showToast(error.message || "默认人物音量保存失败", true); }
  });
  const sampleBody = el("div", { class: "music-sample-status" });
  if (sample.status === "generating") {
    sampleBody.append(el("p", { class: "music-sample-pending" }, "正在生成第 1 段实际混音样板。可继续编辑其他内容；完成后自动刷新。"));
  } else if (sample.status === "ready" || sample.status === "approved") {
    sampleBody.append(el("p", { class: sample.status === "approved" ? "music-sample-approved" : "music-sample-ready" }, sample.status === "approved" ? "声音样板已确认：当前人物与音乐比例会应用到全片。" : "请试听该片段的人物台词和背景音乐；满意后确认应用到全片。"));
    if (sample.output_path) sampleBody.append(el("video", { class: "music-sample-player", controls: "", preload: "metadata", playsinline: "", src: mediaURL(projectId, sample.output_path) }));
    if (sample.status === "ready") sampleBody.append(button("样板音量合格，应用到全片", "primary", async () => {
        try {
          state = await api("/music-sample/approve", { method: "POST", body: { confirmed: true } });
          musicCatalog.policy = state.music_policy;
          stateFingerprint = JSON.stringify(state);
          render();
          showToast("声音样板已确认；全片预览和正式成片都会使用当前人物与音乐比例。");
        } catch (error) { showToast(error.message || "样板确认失败", true); }
      }));
  } else if (sample.status === "failed") {
    sampleBody.append(el("p", { class: "form-error" }, `试听样板生成失败：${sample.error || "请检查第一段是否有可播放的审核预览"}`));
  } else if (sample.status === "stale") {
    sampleBody.append(el("p", { class: "music-sample-pending" }, sample.stale_reason || "人物或音乐设置已变更，请重新生成试听样板。"));
  } else {
    sampleBody.append(el("p", { class: "muted" }, "请保存设置并生成第 1 段声音样板。整体响度合格不等于人物与音乐比例合适。"));
  }
  const selectedTrack = tracks.find((item) => item.id === selectedId);
  const savedRangeEnd = Number(policy.source_end_seconds ?? (selectedTrack || {}).duration_seconds ?? 0);
  const savedRange = policy.enabled && selectedTrack
    ? ` · ${fmtDuration(Number(policy.source_start_seconds || 0))}—${fmtDuration(savedRangeEnd)}`
    : "";
  const summary = `人物 ${narrationGainValue >= 0 ? "+" : ""}${narrationGainValue} dB · ${policy.enabled ? `${selectedTrack ? selectedTrack.title : "未选择曲目"} ${gainValue} dB${savedRange}` : "无背景音乐"} · ${sample.status === "approved" ? "样板已确认" : sample.status === "generating" ? "样板生成中" : "待试听确认"}`;
  const body = el("div", { class: "panel-body music-panel-body" },
      narrationGain.node,
      el("label", { class: "music-toggle" }, enabled, el("span", {}, "为全片添加背景音乐")),
      uploadBox,
      el("label", { class: "field" }, el("span", {}, "选择新闻音乐"), select),
      musicGain.node,
      detail,
      el("div", { class: "inline-actions" }, save, generateSample, saveNarrationAsDefault, saveMusicAsDefault),
      sampleBody,
    );
  return el("section", { class: `panel music-panel ${musicPanelOpen ? "is-open" : ""}` },
    el("button", { type: "button", class: "music-panel-head", onclick: () => { musicPanelOpen = !musicPanelOpen; render(); } },
      el("div", {}, el("p", { class: "eyebrow" }, "全片声音"), el("h4", {}, "人物台词与背景音乐：先试听，再应用全片"), el("span", {}, summary)),
      el("div", { class: "music-panel-status" }, status(sample.status === "approved" ? "completed" : "pending"), el("span", {}, musicPanelOpen ? "收起" : "展开")),
    ),
    musicPanelOpen ? body : null,
  );
}

function policy(title, description) { return el("div", { class: "policy" }, el("strong", {}, title), el("span", {}, description)); }

function renderTimelinePanel() {
  const timeline = el("div", { class: "timeline" }, el("div", { class: "timeline-track" }));
  const track = timeline.firstChild;
  for (const segment of state.segments) {
    const scene = state.scenes.find((item) => segment.scene_ids.includes(item.id));
    const width = Math.max(108, Math.min(260, (segment.end_seconds - segment.start_seconds) * 8 + 92));
    track.append(el("button", { class: `segment ${segment.id === selectedSegmentId ? "active" : ""}`, style: `width:${width}px`, type: "button", onclick: () => { selectedSegmentId = segment.id; if (scene) selectedSceneId = scene.id; activeView = "review"; render(); } },
      el("span", { class: "seg-id" }, segment.id), el("span", { class: "seg-name" }, (scene && scene.title) || "未命名片段"), el("span", { class: "seg-meta" }, `${fmtDuration(segment.end_seconds - segment.start_seconds)} · ${statusLabels[segment.state] || segment.state}`),
    ));
  }
  return el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "语义时间线 / 渲染片段"), el("p", {}, "点击片段即可审核画面、字幕和配音；不需要手工拖拽切点。"))), timeline);
}

function renderReview() {
  const scene = selectedScene();
  if (!scene) return el("section", { class: "page" }, pageHeader("场景审核", "暂无可审核场景", "请先导入脚本或分镜。"));
  const layoutKind = reviewLayoutKind();
  const focusActive = reviewFocusActive();
  const list = el("div", { class: "scene-list" });
  for (const item of state.scenes) {
    list.append(el("button", { class: `scene-item ${item.id === scene.id ? "active" : ""}`, type: "button", onclick: () => { selectedSceneId = item.id; const segment = segmentForScene(item.id); if (segment) selectedSegmentId = segment.id; render(); } },
      el("div", { class: "scene-line" }, el("span", { class: "scene-name" }, sceneName(item)), status(item.review_status)),
      el("div", { class: "scene-time" }, `${clock(item.start_seconds)} — ${clock(item.end_seconds)}`),
    ));
  }
  return el("section", { class: "page" },
    pageHeader("片段工作台", "按一个片段完成画面、字幕与配音审核", "选择片段后先核对关键画面，再试听当前配音；不满意就生成候选，并且只合成当前片段。", button(focusActive ? "退出专注审核" : "专注审核", "quiet", () => { reviewFocusMode = !focusActive; render(); })),
    renderFullPreviewPanel(true),
    renderBackgroundMusicPanel(),
    renderVisualBatchPanel(scene),
    el("div", { class: `review-layout review-layout--${layoutKind}${focusActive ? " is-focus" : ""}` },
      el("section", { class: "panel review-scene-panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "场景清单"), el("p", {}, "按脚本顺序审核"))), list),
      renderFrameStage(scene, focusActive),
      el("div", { class: "review-side" }, renderNarrationReview(scene), renderSubtitleEditor(scene), renderReviewControls(scene)),
    ),
  );
}

function renderVisualBatchPanel(scene) {
  const scenes = state.scenes || [];
  if (!visualBatchSelectionInitialized) {
    visualBatchSelection = new Set(scenes.filter((item) => !sceneHasSupportingVisual(item)).map((item) => item.id));
    visualBatchSelectionInitialized = true;
  }
  const profile = el("select", { "aria-label": "批量画面节奏" },
    el("option", { value: "auto" }, "智能语义节奏：约 6 秒/画面（推荐）"),
    el("option", { value: "video" }, "视频为主：约 5 秒/画面"),
    el("option", { value: "image" }, "静态图片节奏：仅限手动方案"),
  );
  profile.value = (visualBatchPlan && visualBatchPlan.request && visualBatchPlan.request.profile) || visualBatchDraft.profile;
  const operationMode = el("select", { "aria-label": "本次画面操作" },
    el("option", { value: "fill_missing" }, "只补空白主体画面（不覆盖已有内容）"),
    el("option", { value: "replace_selected" }, "替换所选主体画面（保留锁定格）"),
  );
  operationMode.value = visualBatchDraft.operationMode;
  const planningMode = el("select", { "aria-label": "画面规划方式" },
    el("option", { value: "ai_director" }, "AI 智能导演：逐格理解语境并推荐来源（推荐）"),
    el("option", { value: "rule_mix" }, "规则混合：不调用 AI，按固定比例规划"),
  );
  planningMode.value = visualBatchDraft.planningMode;
  const mixStrategy = el("select", { "aria-label": "智能混合比例" },
    el("option", { value: "balanced" }, visualBatchDraft.planningMode === "ai_director" ? "平衡叙事：实拍 60%–70%，HY 30%–40%（推荐）" : "均衡交替：视频 / 图片（推荐）"),
    el("option", { value: "video_first" }, visualBatchDraft.planningMode === "ai_director" ? "实拍优先：实拍 70%–80%" : "视频优先：约 2 个视频配 1 张图片"),
    el("option", { value: "motion_first" }, visualBatchDraft.planningMode === "ai_director" ? "动态图优先：动态图 55%–70%" : "图片优先：约 2 张图片配 1 个视频"),
  );
  mixStrategy.value = visualBatchDraft.mixStrategy;
  const imageSource = el("select", { "aria-label": "批量图片来源" },
    el("option", { value: "web_download" }, "网络图片（Pexels，不产生 AI 生图费）"),
    el("option", { value: "openai_image" }, "OpenAI 生图（执行前确认数量与费用）"),
  );
  imageSource.value = visualBatchDraft.imageSource;
  const personPolicy = el("select", { "aria-label": "人物出镜策略" },
    el("option", { value: "relaxed" }, "宽松：只拦截第二主播和超大正脸"),
    el("option", { value: "balanced" }, "平衡：允许手部、背影和远景人物（推荐）"),
    el("option", { value: "strict" }, "严格：画面中完全不出现人物"),
  );
  personPolicy.value = visualBatchDraft.personPolicy;
  const candidateLimit = el("select", { "aria-label": "每格最多筛选候选数量" },
    el("option", { value: "4" }, "最多检查 4 个候选（较快）"),
    el("option", { value: "6" }, "最多检查 6 个候选（推荐）"),
    el("option", { value: "8" }, "最多检查 8 个候选（命中率更高）"),
  );
  candidateLimit.value = String(visualBatchDraft.candidateLimit);
  const searchTheme = el("input", { type: "text", value: visualBatchDraft.searchTheme, placeholder: "留空则根据本期脚本自动识别，例如：AI 与高新科技", "aria-label": "本期素材主题" });
  const preferredKeywords = el("textarea", { rows: "3", placeholder: "例如：芯片、半导体晶圆、机器人、机械臂、自动化生产线、智能手机", "aria-label": "推荐搜索对象" }, visualBatchDraft.preferredKeywords);
  const cautiousTopics = el("textarea", { rows: "2", "aria-label": "谨慎使用主题" }, visualBatchDraft.cautiousTopics);
  const layoutSource = el("select", { "aria-label": "数字人版式来源片段" });
  const requestedLayoutSource = (visualBatchPlan && visualBatchPlan.request && visualBatchPlan.request.layoutSource) || visualBatchDraft.layoutSourceId || scene.id;
  for (const item of scenes) layoutSource.append(el("option", { value: item.id, selected: item.id === requestedLayoutSource ? "" : null }, `${String(item.order).padStart(2, "0")} · ${item.title}`));
  layoutSource.value = requestedLayoutSource;
  const copyLayoutValue = isAvatarProject() && ((visualBatchPlan && visualBatchPlan.request) ? visualBatchPlan.request.copyLayout : visualBatchDraft.copyLayout);
  const copyLayout = el("input", { type: "checkbox", checked: copyLayoutValue ? "" : null });
  planningMode.addEventListener("change", () => { visualBatchDraft.planningMode = planningMode.value; visualBatchPlan = null; render(); });
  operationMode.addEventListener("change", () => { visualBatchDraft.operationMode = operationMode.value; visualBatchPlan = null; render(); });
  profile.addEventListener("change", () => { visualBatchDraft.profile = profile.value; visualBatchPlan = null; render(); });
  mixStrategy.addEventListener("change", () => { visualBatchDraft.mixStrategy = mixStrategy.value; visualBatchPlan = null; render(); });
  imageSource.addEventListener("change", () => { visualBatchDraft.imageSource = imageSource.value; visualBatchPlan = null; render(); });
  personPolicy.addEventListener("change", () => { visualBatchDraft.personPolicy = personPolicy.value; visualBatchPlan = null; render(); });
  candidateLimit.addEventListener("change", () => { visualBatchDraft.candidateLimit = Number(candidateLimit.value); visualBatchPlan = null; render(); });
  searchTheme.addEventListener("input", () => { visualBatchDraft.searchTheme = searchTheme.value; visualBatchPlan = null; });
  preferredKeywords.addEventListener("input", () => { visualBatchDraft.preferredKeywords = preferredKeywords.value; visualBatchPlan = null; });
  cautiousTopics.addEventListener("input", () => { visualBatchDraft.cautiousTopics = cautiousTopics.value; visualBatchPlan = null; });
  copyLayout.addEventListener("change", () => { visualBatchDraft.copyLayout = copyLayout.checked; visualBatchPlan = null; });
  layoutSource.addEventListener("change", () => { visualBatchDraft.layoutSourceId = layoutSource.value; visualBatchPlan = null; });
  const selectionList = el("div", { class: "visual-batch-scenes" });
  const selectIds = (ids) => {
    visualBatchSelection = new Set(ids);
    visualBatchPlan = null;
    render();
  };
  for (const item of scenes) {
    const checkbox = el("input", { type: "checkbox", checked: visualBatchSelection.has(item.id) ? "" : null, "aria-label": `选择 ${item.title}` });
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) visualBatchSelection.add(item.id); else visualBatchSelection.delete(item.id);
      visualBatchPlan = null;
      const count = document.querySelector("[data-visual-batch-count]");
      if (count) count.textContent = `已选 ${visualBatchSelection.size}/${scenes.length}`;
    });
    const hasSupportingVisual = sceneHasSupportingVisual(item);
    const hasPresenterMedia = sceneHasPresenterMedia(item);
    selectionList.append(el("label", { class: `visual-batch-scene ${hasSupportingVisual ? "has-visual" : "missing-visual"}` },
      checkbox,
      el("span", {}, `${String(item.order).padStart(2, "0")} · ${item.title}`),
      el("span", { class: "visual-batch-readiness" },
        el("small", {}, hasSupportingVisual ? "主体画面：已就绪" : "主体画面：待补"),
        isAvatarProject() ? el("small", {}, hasPresenterMedia ? "数字人：已就绪" : "数字人：未就绪") : null,
      ),
    ));
  }
  const ruleOptions = [
    ["no_presenter_studio", "禁止主播或演播室", "写入检索词与 AI 提示词，结果仍需人工复核"],
    ["no_large_text_watermark", "禁止大段文字与水印", "写入检索词与 AI 提示词，结果仍需人工复核"],
  ];
  const searchPanel = el("details", { class: "visual-search-strategy" },
    el("summary", {}, el("strong", {}, "检索策略（可选）"), el("span", {}, "先约束搜索，再轻量检查")),
    el("div", { class: "visual-search-fields" },
      el("label", {}, el("strong", {}, "本期素材主题"), searchTheme, el("small", {}, "留空时系统会读取全部台词自动识别；主题只用于确定素材方向，不会机械地塞进每条查询。")),
      el("label", {}, el("strong", {}, "优先搜索的具体对象"), preferredKeywords, el("small", {}, "用中文逗号分隔。系统优先选择能直接形成画面的实体，而不是只搜索“AI、科技、未来”。")),
      el("label", {}, el("strong", {}, "谨慎使用的主题"), cautiousTopics, el("small", {}, "这些词不会作为负面词提交给 Pexels；系统会改用机器、产品、界面、环境等正向对象规避。")),
    ),
    el("p", {}, "每个画面槽会获得不同镜头职责，并生成“精确检索 → 行业检索 → 兜底检索”三组英文关键词。请先点击“预览补全计划”核对后再执行。"),
  );
  const rulesPanel = el("details", { class: "visual-batch-rules" },
    el("summary", {}, el("strong", {}, "下载前轻量保险（可选）"), el("span", {}, `${{relaxed:"宽松",balanced:"平衡",strict:"严格"}[visualBatchDraft.personPolicy]}模式 · 最多 ${visualBatchDraft.candidateLimit} 个候选`)),
    el("div", { class: "visual-policy-controls" },
      el("label", {}, el("strong", {}, "人物出镜策略"), personPolicy, el("small", {}, "平衡模式允许手部、背影、远景人群和产品使用者，只拦截抢镜人物与正面大脸。")),
      el("label", {}, el("strong", {}, "失败前自动换素材"), candidateLimit, el("small", {}, "候选不合格会自动丢弃并继续搜索，不会立即让整格失败。")),
    ),
    el("p", {}, "人物限制使用面积、位置和连续帧综合判断；不是检测到人就淘汰。最终仍需逐槽审核。"),
    ...ruleOptions.map(([value, label, help]) => {
      const input = el("input", { type: "checkbox", checked: visualBatchDraft.contentRules.includes(value) ? "" : null });
      input.addEventListener("change", () => {
        const next = new Set(visualBatchDraft.contentRules);
        if (input.checked) next.add(value); else next.delete(value);
        visualBatchDraft.contentRules = [...next];
        visualBatchPlan = null;
        render();
      });
      return el("label", { class: "visual-rule-option" }, input, el("span", {}, el("strong", {}, label), el("small", {}, help)));
    }),
  );
  const operationLabel = visualBatchOperationLabel(visualBatchDraft.operationMode);
  const selectedExistingCount = scenes.filter((item) => visualBatchSelection.has(item.id) && sceneHasSupportingVisual(item)).length;
  const planHasWork = visualBatchPlanHasWork(visualBatchDraft.operationMode);
  const plannedDuration = Number((visualBatchPlan || {}).total_planned_duration_seconds || 0);
  const stockDuration = Number((visualBatchPlan || {}).stock_video_duration_seconds || 0);
  const motionDuration = Number((visualBatchPlan || {}).hyperframes_duration_seconds || 0);
  const stockPercent = plannedDuration ? Math.round(stockDuration / plannedDuration * 100) : 0;
  const motionPercent = plannedDuration ? Math.round(motionDuration / plannedDuration * 100) : 0;
  const hyperPlans = visualBatchPlan ? visualBatchPlan.items.flatMap((item) => item.blocks.filter((block) => block.status === "planned" && block.route === "hyperframes")) : [];
  const recipeCounts = hyperPlans.reduce((counts, block) => { const key = block.scene_recipe || "relationship_map"; counts[key] = (counts[key] || 0) + 1; return counts; }, {});
  const layoutCounts = hyperPlans.reduce((counts, block) => {
    const recipe = block.scene_recipe || "relationship_map";
    const layout = hyperframesLayoutChoice(null, recipe, block.layout_variant);
    const key = `${recipe}:${layout.id}`;
    counts[key] = { recipe, layout, count: (counts[key] ? counts[key].count : 0) + 1 };
    return counts;
  }, {});
  const copyFingerprints = new Map();
  for (const block of hyperPlans) {
    const copy = block.graphic_copy || {};
    const fingerprint = JSON.stringify([copy.headline || "", copy.center_label || "", copy.nodes || []]);
    if (fingerprint !== '["","",[]]') copyFingerprints.set(fingerprint, (copyFingerprints.get(fingerprint) || 0) + 1);
  }
  const duplicateCopyCount = [...copyFingerprints.values()].filter((count) => count > 1).reduce((total, count) => total + count, 0);
  const planRows = visualBatchPlan ? el("div", { class: `visual-batch-plan ${planHasWork ? "is-ready" : "is-empty"}` },
    el("div", { class: "visual-batch-plan-summary" },
      el("strong", {}, planHasWork ? `${visualRecommendationLabel()}已准备好 · ${visualBatchPlan.scene_count} 个片段 · ${visualBatchPlan.total_slots} 个画面格` : "本次没有可执行的画面推荐"),
      planHasWork
        ? [
          el("span", {}, `${visualBatchPlan.planner && visualBatchPlan.planner.mode === "ai_director" ? "AI 已逐格理解台词并给出来源建议" : "已按规则生成来源建议"}；你可以在下方逐格改写，再执行第二步。`),
          Number((visualBatchPlan.planner || {}).repaired_slots || 0) || Number((visualBatchPlan.planner || {}).fallback_slots || 0)
            ? el("span", { class: "visual-plan-repair-note" }, `AI 本轮漏项已自动补齐：重试成功 ${Number((visualBatchPlan.planner || {}).repaired_slots || 0)} 格、规则兜底 ${Number((visualBatchPlan.planner || {}).fallback_slots || 0)} 格；整批任务未中断。`)
            : null,
          el("span", {}, `按时长推荐：网络视频 ${(visualBatchPlan.route_counts || {}).stock_video || 0} 格 / ${stockDuration.toFixed(1)} 秒（${stockPercent}%），HyperFrames ${(visualBatchPlan.route_counts || {}).hyperframes || 0} 格 / ${motionDuration.toFixed(1)} 秒（${motionPercent}%）${visualBatchPlan.primary_image_duration_seconds ? `，人工图片 ${Number(visualBatchPlan.primary_image_duration_seconds).toFixed(1)} 秒` : ""}。`),
          Number((visualBatchPlan.planner || {}).balance_adjusted_slots || 0)
            ? el("span", { class: "visual-plan-repair-note" }, `全片平衡器调整了 ${Number(visualBatchPlan.planner.balance_adjusted_slots)} 格，其中默认图片路线转回视频或动态页面 ${Number(visualBatchPlan.planner.normalized_image_slots || 0)} 格。`)
            : null,
          visualBatchPlan.balance_warning
            ? el("span", { class: "visual-plan-balance-warning" }, `节奏提醒：${visualBatchPlan.balance_warning}。你仍可逐格修改。`)
            : el("span", {}, "当前视频与动态页面的时长占比处于所选预设区间。"),
          hyperPlans.length ? el("span", {}, `动态图结构：${Object.entries(recipeCounts).map(([key, count]) => `${{headline_statement:"标题判断",relationship_map:"关系图",single_metric:"关键数字",comparison:"双项对比",process:"流程",quote_evidence:"证据短句",closing_question:"结尾提问"}[key] || key} ${count} 格`).join("、")}。版式分布：${Object.values(layoutCounts).map((item) => `${item.layout.name} ${item.count} 格`).join("、")}。${Number((visualBatchPlan.planner || {}).layout_adjusted_slots || 0) ? `为避免相邻重复，已自动调整 ${Number((visualBatchPlan.planner || {}).layout_adjusted_slots)} 格版式。` : ""}${duplicateCopyCount ? `检测到 ${duplicateCopyCount} 格文案高度重复，请在生成前核对。` : "未发现完全重复的画面文案。"}`) : null,
          el("span", {}, `本次会${visualBatchPlan.policy.operation_mode === "replace_selected" ? "替换所选的未锁定主体画面" : "只补全缺失主体画面"}；数字人素材不参与空白判断，锁定格不会被覆盖。开始执行后，这份推荐会冻结为项目内可追溯合同。`),
        ]
        : el("div", { class: "visual-batch-empty-plan" },
          el("span", {}, selectedExistingCount ? `你选中的 ${selectedExistingCount} 个片段已有主体画面，而“只补空白主体画面”不会覆盖已有内容，所以没有可执行的画面格。数字人素材不计入这个判断。` : "所选片段目前没有可处理的空白主体画面格。数字人原片与主体画面分开判断。"),
          visualBatchDraft.operationMode === "fill_missing" && selectedExistingCount
            ? button("改为“替换所选主体画面”并重新识别", "primary small", () => {
              visualBatchDraft.operationMode = "replace_selected";
              previewVisualBatch(profile.value, copyLayout.checked, layoutSource.value, "replace_selected");
            })
            : null,
        ),
    ),
    ...visualBatchPlan.items.map((item) => {
      const plannedBlocks = item.blocks.filter((block) => block.status === "planned");
      return el("div", { class: "visual-batch-plan-row" },
        el("div", { class: "visual-plan-scene-head" },
          el("strong", {}, `${String(item.order).padStart(2, "0")} · ${item.title}`),
          el("span", {}, `${Number(item.duration_seconds).toFixed(2)} 秒 · ${plannedBlocks.length || 0} 个${visualBatchPlan.policy.operation_mode === "replace_selected" ? "待替换" : "待补"}画面格`),
          item.locked_slots ? el("span", { class: "status approved" }, `${item.locked_slots} 个已锁`) : null,
          item.preserved_slots ? el("span", { class: "status" }, `${item.preserved_slots} 个保留`) : null,
        ),
        ...plannedBlocks.map((block) => {
          const route = el("select", { "aria-label": `${item.title} ${block.id} 画面生产方式` },
            el("option", { value: "stock_video" }, "网络视频"),
            el("option", { value: "hyperframes" }, "HyperFrames 动态画面"),
            el("option", { value: "stock_image" }, "网络图片（手动覆盖）"),
            el("option", { value: "ai_image" }, "OpenAI 生图（手动覆盖）"),
          );
          route.value = block.route || (block.source_mode === "hyperframes" ? "hyperframes" : block.source_mode === "openai_image" ? "ai_image" : block.media_kind === "image" ? "stock_image" : "stock_video");
          route.addEventListener("change", () => { applyVisualPlanRoute(block, route.value); render(); });
          const intent = el("input", { type: "text", value: block.visual_intent || "", placeholder: "这格画面要让观众看到什么", "aria-label": `${item.title} ${block.id} 画面意图` });
          intent.addEventListener("input", () => { block.visual_intent = intent.value; block.decision_source = "human_override"; });
          const queryInput = el("input", { type: "text", value: block.query || "", placeholder: "英文检索词，例如 industrial robot assembly", "aria-label": `${item.title} ${block.id} 首选检索词` });
          queryInput.addEventListener("input", () => {
            if (!visualBatchDraft.queryOverrides[item.scene_id]) visualBatchDraft.queryOverrides[item.scene_id] = {};
            visualBatchDraft.queryOverrides[item.scene_id][block.id] = queryInput.value;
            block.query = queryInput.value;
            if (block.query_ladder && block.query_ladder[0]) block.query_ladder[0].query = queryInput.value;
            block.decision_source = "human_override";
          });
          const recipe = el("select", { "aria-label": `${item.title} ${block.id} 动态图形结构` },
            el("option", { value: "headline_statement" }, "标题判断"),
            el("option", { value: "relationship_map" }, "关系图"),
            el("option", { value: "single_metric" }, "单一数据"),
            el("option", { value: "comparison" }, "双项对比"),
            el("option", { value: "process" }, "流程"),
            el("option", { value: "quote_evidence" }, "证据短句"),
            el("option", { value: "closing_question" }, "结尾提问"),
          );
          recipe.value = block.scene_recipe || "relationship_map";
          const layoutVariant = el("select", { "aria-label": `${item.title} ${block.id} 动态画面版式` });
          const refreshBlockLayoutChoices = (requested) => {
            const choices = hyperframesLayoutChoices(null, recipe.value);
            const active = requested || layoutVariant.value || block.layout_variant || "";
            layoutVariant.replaceChildren(...choices.map((choice) => el("option", { value: choice.id }, `${choice.name} · ${choice.motion_variant}`)));
            const selected = choices.find((choice) => choice.id === active) || choices[0];
            layoutVariant.value = selected.id;
            block.layout_variant = selected.id;
            block.motion_variant = selected.motion_variant;
          };
          refreshBlockLayoutChoices(block.layout_variant || "");
          recipe.addEventListener("change", () => {
            block.scene_recipe = recipe.value;
            refreshBlockLayoutChoices();
            block.decision_source = "human_override";
          });
          layoutVariant.addEventListener("change", () => {
            const selected = hyperframesLayoutChoice(null, recipe.value, layoutVariant.value);
            block.layout_variant = selected.id;
            block.motion_variant = selected.motion_variant;
            block.layout_variant_locked = true;
            block.decision_source = "human_override";
          });
          const graphicCopy = block.graphic_copy && typeof block.graphic_copy === "object" ? block.graphic_copy : (block.graphic_copy = {});
          const copyInput = (key, placeholder, label) => {
            const input = el("input", { type: "text", value: graphicCopy[key] || "", placeholder, "aria-label": `${item.title} ${block.id} ${label}` });
            input.addEventListener("input", () => { graphicCopy[key] = input.value; block.decision_source = "human_override"; });
            return el("label", { class: "visual-copy-field" }, el("small", {}, label), input);
          };
          const nodeInput = el("input", { type: "text", value: (graphicCopy.nodes || []).join("｜"), placeholder: "要点一｜要点二｜要点三", "aria-label": `${item.title} ${block.id} 画面要点` });
          nodeInput.addEventListener("input", () => { graphicCopy.nodes = nodeInput.value.split(/[｜|]/).map((value) => value.trim()).filter(Boolean).slice(0, 4); block.decision_source = "human_override"; });
          const details = [
            el("span", {}, `${block.id} · ${Number(block.start_seconds || 0).toFixed(2)}–${Number(block.end_seconds || 0).toFixed(2)} 秒`),
            block.slot_text ? el("small", { class: "visual-slot-text" }, `本格台词：${block.slot_text}`) : null,
            route,
            intent,
            el("small", {}, `AI 理由：${block.reason || "按当前台词语境规划"} · 置信度 ${Math.round(Number(block.confidence || 0) * 100)}%${block.fallback_route ? ` · 建议失败后人工改为：${{stock_video:"网络视频",stock_image:"网络图片",ai_image:"OpenAI 图片",hyperframes:"HyperFrames"}[block.fallback_route] || block.fallback_route}` : ""}`),
          ];
          if (["stock_video", "stock_image"].includes(route.value)) details.push(queryInput, el("small", {}, (block.query_ladder || []).map((entry) => `${entry.level}：${entry.query}`).join(" → ")));
          else if (route.value === "hyperframes") details.push(
            recipe,
            el("label", { class: "visual-layout-field" }, el("small", {}, "动态图版式"), layoutVariant),
            el("div", { class: "visual-copy-editor" },
              copyInput("headline", "例如：数字角色正在影响现实", "画面标题"),
              copyInput("scene_goal", "这一格只需要让观众理解什么", "表达目标"),
              copyInput("center_label", "关系图核心或本格概念", "核心概念"),
              copyInput("supporting_statement", "补充判断或证据", "补充说明"),
              el("label", { class: "visual-copy-field" }, el("small", {}, "画面要点（用｜分隔）"), nodeInput),
            ),
            el("small", {}, "以上是生成前可审核的画面文案。动态图形只生成主体画面；数字人与模块化字幕仍由 Haike Video 叠加。"),
          );
          else details.push(el("small", {}, "OpenAI 生图会在开始执行前再次提示费用；图片会转成该槽位长度的视频，便于无缝合成。"));
          return el("label", { class: "visual-query-plan" },
            ...details,
          );
        }),
      );
    }),
  ) : null;
  const planningFeedback = visualBatchPlanning
    ? el("div", { class: "visual-batch-planning", role: "status", "aria-live": "polite" },
      el("span", { class: "visual-batch-planning-spinner", "aria-hidden": "true" }),
      el("div", {},
        el("strong", {}, visualBatchDraft.planningMode === "ai_director" ? "AI 正在识别并规划画面" : "正在生成规则推荐"),
        el("span", {}, visualBatchDraft.planningMode === "ai_director"
          ? `正在理解 ${visualBatchSelection.size} 个片段的台词与前后文，并逐格推荐网络视频或 HyperFrames 动态画面。通常需要 20–90 秒，完成后会自动显示结果。`
          : "正在按当前节奏与来源偏好建立画面槽位，完成后会自动显示结果。"),
      ),
    )
    : visualBatchPlanningError
      ? el("div", { class: "visual-batch-planning is-error", role: "alert" },
        el("div", {}, el("strong", {}, "AI 识别未完成"), el("span", {}, visualBatchPlanningError)),
      )
      : null;
  const body = el("div", { class: "visual-batch-body" },
    el("div", { class: "visual-batch-toolbar" },
      el("div", {}, el("strong", {}, "1. 选择片段"), el("span", { "data-visual-batch-count": "" }, `已选 ${visualBatchSelection.size}/${scenes.length}`)),
      el("div", { class: "inline-actions" },
        button("仅选缺少主体画面", "primary small", () => selectIds(scenes.filter((item) => !sceneHasSupportingVisual(item)).map((item) => item.id))),
        button("仅选筛选失败", "quiet small", () => selectIds(scenes.filter(sceneHasFailedVisual).map((item) => item.id))),
        button("选择全部", "quiet small", () => selectIds(scenes.map((item) => item.id))),
        button("清空", "quiet small", () => selectIds([])),
      ),
    ),
    selectionList,
    el("div", { class: "visual-batch-settings" },
      el("label", {}, el("strong", {}, "本次生成目标"), operationMode, el("small", {}, visualBatchDraft.operationMode === "replace_selected" ? "会替换所选片段中未锁定的主体画面格；数字人原片和位置不会改变。" : "只处理尚未有主体内容的画面格；数字人素材不会被当作主体画面，也不会被覆盖。")),
      el("label", {}, el("strong", {}, "智能切画面"), profile, el("small", {}, "系统会均分片段，避免末尾出现一闪而过的小尾巴。")),
      el("label", {}, el("strong", {}, "AI 推荐偏好"), planningMode, mixStrategy, visualBatchDraft.planningMode === "rule_mix" ? imageSource : null, el("small", {}, visualBatchDraft.planningMode === "ai_director" ? "第一步默认只在网络视频与 HyperFrames 之间推荐，并按整片时长自动重平衡；图片仍可在结果中逐格手动指定。" : "不调用 AI，按你设定的节奏和偏好给出可编辑推荐。")),
      isAvatarProject() ? el("label", {},
        el("strong", {}, "数字人样式同步（可选）"),
        el("span", { class: "check-row" }, copyLayout, "后续补画面任务中，同时复制来源片段的数字人样式"),
        layoutSource,
        el("div", { class: "inline-actions" }, button(previewSyncRunning() ? "正在刷新审核预览…" : "一键同步所选片段的数字人样式", "primary small", () => applySelectedPresenterLayout(layoutSource.value), previewSyncRunning() || !visualBatchSelection.size)),
        el("small", {}, "只复制出镜方式、位置大小与原片底部裁切；不会下载、替换或重排主体画面。随后会自动刷新所选片段的审核预览。"),
      ) : null,
    ),
    searchPanel,
    rulesPanel,
    el("div", { class: "visual-batch-actions" },
      button(visualBatchPlanning ? "① AI 正在识别…" : visualBatchDraft.planningMode === "ai_director" ? "① AI 识别并给出推荐" : "① 生成规则推荐", "quiet", () => previewVisualBatch(profile.value, copyLayout.checked, layoutSource.value, visualBatchDraft.operationMode), visualBatchRunning() || visualBatchPlanning),
      button(visualBatchRunning() ? "正在批量处理…" : `② 开始${operationLabel}`, "primary", () => startVisualBatch(profile.value, copyLayout.checked, layoutSource.value, visualBatchDraft.operationMode), visualBatchRunning() || visualBatchPlanning || !planHasWork),
      !visualBatchPlan && !visualBatchPlanning && !visualBatchRunning() && !visualBatchResultsReady ? el("span", { class: "visual-batch-action-hint" }, "先完成第一步，查看并可改写 AI 推荐；第二步才会下载或生成画面。") : null,
    ),
    el("div", { class: `visual-batch-live ${visualBatchRunning() ? "is-running" : ""}`, "data-visual-batch-island": "" }),
    planningFeedback,
    planRows,
    el("div", { class: `preview-sync-live ${previewSyncRunning() ? "is-running" : ""}`, "data-preview-sync-island": "" }),
  );
  queueMicrotask(() => { updateVisualBatchIsland(); updatePreviewSyncIsland(); });
  return el("section", { class: `panel visual-batch-panel ${visualBatchPanelOpen ? "is-open" : ""}` },
    el("button", { type: "button", class: "visual-batch-head", onclick: () => { visualBatchPanelOpen = !visualBatchPanelOpen; render(); } },
      el("div", {}, el("p", { class: "eyebrow" }, "批量生产"), el("h4", {}, "批量补全画面"), el("span", {}, "选择范围 → 预览槽位 → 串行下载 → 逐槽审核")),
      el("span", {}, visualBatchPanelOpen ? "收起" : "展开"),
    ),
    visualBatchPanelOpen ? body : null,
  );
}

function renderReviewFocusToolbar(scene) {
  const scenes = state.scenes || [];
  const index = Math.max(0, scenes.findIndex((item) => item.id === scene.id));
  const selector = el("select", { class: "review-scene-switcher", "aria-label": "切换审核场景" });
  for (const item of scenes) selector.append(el("option", { value: item.id }, `${String(item.order).padStart(2, "0")} · ${item.title}`));
  selector.value = scene.id;
  selector.addEventListener("change", () => {
    selectedSceneId = selector.value;
    const segment = segmentForScene(selectedSceneId);
    if (segment) selectedSegmentId = segment.id;
    render();
  });
  const switchScene = (next) => {
    const target = scenes[next];
    if (!target) return;
    selectedSceneId = target.id;
    const segment = segmentForScene(target.id);
    if (segment) selectedSegmentId = segment.id;
    render();
  };
  return el("div", { class: "review-focus-toolbar" },
    el("div", { class: "review-focus-copy" }, el("strong", {}, "专注审核"), el("span", {}, `${reviewAspectLabel()} · 画面按实际成片比例展示`)),
    el("div", { class: "review-focus-actions" },
      button("上一段", "quiet small", () => switchScene(index - 1), index === 0),
      selector,
      button("下一段", "quiet small", () => switchScene(index + 1), index >= scenes.length - 1),
      button("显示场景清单", "quiet small", () => { reviewFocusMode = false; render(); }),
    ),
  );
}

function renderFrameStage(scene, focusActive = false) {
  const preview = reviewPreview(scene);
  const presenter = presenterFor(scene);
  const timing = scene.timing || {};
  const fit = scene.visual_fit || null;
  const duration = Math.max(.04, Number(scene.end_seconds || 0) - Number(scene.start_seconds || 0));
  const current = Math.min(duration, Math.max(0, Number(reviewPlaybackPositions[scene.id] || 0)));
  const player = el("div", { class: "light-review-player", style: `--review-aspect:${sceneAspectRatio()}` });
  const stage = el("div", { class: "light-review-canvas" });
  const timeReadout = el("span", { class: "review-time-readout" }, reviewTimeLabel(scene, current));
  const scrub = el("input", { class: "review-scrubber", type: "range", min: "0", max: String(duration), step: "0.01", value: String(current), "aria-label": "审核片段时间定位" });
  const caption = el("div", { class: "review-live-caption", "aria-live": "polite", "data-scene-caption": scene.id });
  let video = null;
  const refreshCaption = () => {
    const draft = subtitleDrafts.get(scene.id);
    const style = draft ? draft.style : subtitleStyleFor(scene);
    applyCaptionStyle(caption, style);
    const currentTime = Number(reviewPlaybackPositions[scene.id] || 0);
    const cue = effectiveCaptionCues(scene).find((item) => currentTime >= Number(item.start_seconds || 0) && currentTime <= Number(item.end_seconds || 0));
    caption.textContent = cue ? cue.text : "";
  };
  const updateTime = (relative) => {
    const next = Math.min(duration, Math.max(0, Number(relative || 0)));
    reviewPlaybackPositions[scene.id] = next;
    scrub.value = String(next);
    timeReadout.textContent = reviewTimeLabel(scene, next);
    refreshCaption();
  };
  const seek = (relative, play = false) => {
    updateTime(relative);
    if (video) {
      video.currentTime = Math.min(Math.max(0, Number(relative || 0)), Math.max(0, video.duration - .03));
      if (play) video.play().catch(() => {});
    }
  };
  reviewCaptionControllers.set(scene.id, { refresh: refreshCaption, seek });
  if (preview.status === "ready" && preview.output_path) {
    video = el("video", { src: mediaURL(projectId, preview.output_path), controls: "", preload: "metadata", playsinline: "" });
    video.addEventListener("loadedmetadata", () => seek(current));
    video.addEventListener("timeupdate", () => {
      if (video.currentTime >= duration - .03) { video.currentTime = 0; updateTime(0); }
      else updateTime(video.currentTime);
    });
    video.addEventListener("ended", () => { video.currentTime = 0; updateTime(0); });
    stage.append(video, caption);
  } else {
    const reason = preview.status === "failed" ? (preview.error || "本段审核预览生成失败") : preview.status === "stale" ? (preview.stale_reason || "画面已变化，需要刷新预览") : "请先生成本段审核预览";
    stage.append(el("div", { class: "frame-placeholder" }, el("strong", {}, preview.status === "stale" ? "审核预览已过期" : "尚未生成可播放的审核片段"), el("br"), reason));
  }
  player.append(stage, el("div", { class: "frame-overlay" }, status(scene.review_status), el("span", { class: "status editable" }, preview.resolution || "按项目画幅预览"), isAvatarProject() ? el("span", { class: "status editable" }, `数字人：${presenterTreatmentLabel(presenter.treatment)}`) : null));
  const refreshLabel = preview.status === "ready" ? "刷新审核预览" : preview.status === "stale" ? "刷新已变更预览" : "生成本段审核预览";
  const actions = el("div", { class: "review-player-actions" },
    button(refreshLabel, "primary", () => mutate(`/scenes/${encodeURIComponent(scene.id)}/review-preview`, { method: "POST" }, "本段可播放审核预览已生成")),
    preview.status === "ready" ? button("从当前时刻播放", "quiet", () => seek(Number(scrub.value), true)) : null,
    timeReadout,
  );
  scrub.addEventListener("input", () => seek(Number(scrub.value)));
  refreshCaption();
  const anchors = el("div", { class: "review-anchor-rail" });
  const keyframeTimeline = (scene.keyframe_review && scene.keyframe_review.timeline) || [];
  const keyframeByKind = new Map(keyframeTimeline.map((item) => [item.anchor_kind, item]));
  for (const anchor of scene.anchors || []) {
    const item = keyframeByKind.get(anchor.kind);
    const relative = sceneRelativeTime(scene, anchor.time_seconds);
    anchors.append(el("button", { class: `review-anchor ${anchor.status === "approved" ? "approved" : ""}`, type: "button", style: `left:${Math.min(98, Math.max(1, relative / duration * 100))}%`, onclick: () => seek(relative, true) },
      el("span", {}, item ? (item.label || anchorLabels[anchor.kind]) : (anchorLabels[anchor.kind] || anchor.label)),
      el("small", {}, fmtDuration(relative)),
    ));
  }
  for (const directive of scene.surgical_directives || []) {
    const relative = sceneRelativeTime(scene, directive.start_seconds);
    anchors.append(el("button", { class: "review-anchor directive", type: "button", style: `left:${Math.min(98, Math.max(1, relative / duration * 100))}%`, onclick: () => seek(relative, true) }, el("span", {}, directive.id), el("small", {}, "组件")));
  }
  const keyframeSummary = el("div", { class: "keyframe-summary" });
  const keyframeReview = scene.keyframe_review || {};
  const candidate = aiVisualCandidate(scene);
  const aiVisualAlreadyActive = Boolean(candidate && currentAsset(scene.id) && currentAsset(scene.id).id === candidate.id);
  if (scene.source_strategy === "ai_generated" && candidate) {
    keyframeSummary.append(el("div", { class: "keyframe-adoption" },
      el("div", {},
        el("strong", {}, aiVisualAlreadyActive ? "当前预览已采用 AI 主体画面" : "AI 主体画面尚未进入播放器"),
        el("span", {}, aiVisualAlreadyActive ? `左侧播放器已使用 ${candidate.id} 作为本段主体画面。` : `已找到 AI 首帧 ${candidate.id}；点击后会替换旧网络素材并自动刷新本段预览。`),
      ),
      button(aiVisualAlreadyActive ? "已采用 AI 主体画面" : "采用 AI 主体画面并刷新预览", "primary", () => adoptAiVisualAndRefreshPreview(scene), aiVisualAlreadyActive),
    ));
  }
  for (const item of keyframeTimeline) {
    const anchor = (scene.anchors || []).find((entry) => entry.kind === item.anchor_kind);
    const mark = (itemStatus) => mutate(`/scenes/${encodeURIComponent(scene.id)}/keyframes/review`, { method: "POST", body: { action: "update", items: [{ anchor_kind: item.anchor_kind, status: itemStatus }] } }, `${item.label || "关键帧"}已标记为${statusLabels[itemStatus]}`);
    keyframeSummary.append(el("div", { class: "keyframe-chip" },
      button(item.label || anchorLabels[item.anchor_kind] || "关键帧", "small", () => seek(sceneRelativeTime(scene, item.time_seconds), true)),
      status(item.status),
      button("通过", "quiet small", () => mark("approved"), item.status === "approved"),
      button("调整", "quiet small", () => mark("needs_adjustment"), item.status === "needs_adjustment"),
      anchor ? el("span", { class: "minor" }, item.visual_note || "") : null,
    ));
  }
  const genericAnchors = (scene.anchors || []).filter((anchor) => !keyframeByKind.has(anchor.kind));
  for (const anchor of genericAnchors) keyframeSummary.append(el("div", { class: "keyframe-chip" }, button(anchorLabels[anchor.kind] || anchor.label, "small", () => seek(sceneRelativeTime(scene, anchor.time_seconds), true)), status(anchor.status), button("通过", "quiet small", () => updateAnchor(scene, anchor, "approved")), button("调整", "quiet small", () => updateAnchor(scene, anchor, "needs_adjustment"))));
  return el("section", { class: `frame-stage${focusActive ? " is-focus" : ""}` },
    focusActive ? renderReviewFocusToolbar(scene) : null,
    el("div", { class: "panel light-review-panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, sceneName(scene)), el("p", {}, `${clock(scene.start_seconds)} — ${clock(scene.end_seconds)} · 以实际片段播放为主，锚点仅用于快速定位`))), el("div", { class: "panel-body" }, player, actions, scrub, anchors, el("div", { class: "frame-caption" }, scene.description || "请补充此场景的表达目标。"), isAvatarProject() ? el("div", { class: "report" }, `数字人版式：${presenterTreatmentLabel(presenter.treatment)}。审核预览使用本段原声，不拉伸语速或嘴型。`) : null, timing.voice_duration_seconds ? el("div", { class: "minor" }, `自然配音 ${Number(timing.voice_duration_seconds).toFixed(2)} 秒 · 当前片段 ${fmtDuration(duration)}`) : null, fit ? el("div", { class: `report ${fit.strategy === "needs_replacement" ? "bad" : ""}` }, `画面时长策略：${fit.strategy === "trim" ? "裁切现有素材" : fit.strategy === "brief_hold" ? "补足极短尾帧" : fit.strategy === "generated_motion" ? "图片动态片段" : "需要更换更长素材"}${fit.source_duration_seconds ? ` · 素材 ${Number(fit.source_duration_seconds).toFixed(2)} 秒` : ""}`) : null)),
    el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "审核锚点"), el("p", {}, "关键帧保留为判断依据；点击即可回到对应时刻，不再挤占播放画面。"))), el("div", { class: "panel-body" }, keyframeSummary.children.length ? keyframeSummary : el("div", { class: "empty" }, "还没有关键帧锚点。先生成审核预览，再按需要生成或刷新关键帧。"))),
    renderSurgicalDirectivePanel(scene, () => Number(scrub.value), seek),
  );
}

function renderSurgicalDirectivePanel(scene, currentTime, seek) {
  const type = el("select", { "aria-label": "定点组件类型" }, el("option", { value: "text_callout" }, "文字提示卡"), el("option", { value: "info_label" }, "信息标签"), el("option", { value: "focus_box" }, "聚焦框"));
  const position = el("select", { "aria-label": "定点组件位置" }, el("option", { value: "lower_third" }, "下方信息区"), el("option", { value: "top_left" }, "左上角"), el("option", { value: "top_right" }, "右上角"), el("option", { value: "center" }, "画面中央"));
  const text = el("input", { maxlength: "80", placeholder: "例如：关键数据：3 倍增长", "aria-label": "组件文字" });
  const seconds = el("input", { type: "number", min: "0.5", max: "12", step: "0.5", value: "2.5", "aria-label": "组件持续秒数" });
  const focusX = el("input", { type: "range", min: "0.02", max: "0.88", step: "0.01", value: ".20", "aria-label": "聚焦框横向位置" });
  const focusY = el("input", { type: "range", min: "0.02", max: "0.78", step: "0.01", value: ".20", "aria-label": "聚焦框纵向位置" });
  const focusWidth = el("input", { type: "range", min: "0.08", max: "0.90", step: "0.01", value: ".58", "aria-label": "聚焦框宽度" });
  const focusHeight = el("input", { type: "range", min: "0.08", max: "0.62", step: "0.01", value: ".36", "aria-label": "聚焦框高度" });
  const focusReadout = el("span", { class: "minor" });
  const focusControls = el("div", { class: "surgical-focus-controls", hidden: "hidden" },
    el("label", {}, "左右", focusX), el("label", {}, "上下", focusY), el("label", {}, "宽度", focusWidth), el("label", {}, "高度", focusHeight), focusReadout,
  );
  const focusBox = () => ({ x: Number(focusX.value), y: Number(focusY.value), width: Number(focusWidth.value), height: Number(focusHeight.value) });
  const repaintFocus = () => { const box = focusBox(); focusReadout.textContent = `聚焦区域：${Math.round(box.x * 100)}% / ${Math.round(box.y * 100)}% · ${Math.round(box.width * 100)}% × ${Math.round(box.height * 100)}%`; };
  [focusX, focusY, focusWidth, focusHeight].forEach((input) => input.addEventListener("input", repaintFocus));
  repaintFocus();
  const targetTime = el("strong", {}, reviewTimeLabel(scene, currentTime()));
  const updateTargetTime = () => { targetTime.textContent = reviewTimeLabel(scene, currentTime()); };
  const updateComponentForm = () => {
    const isFocus = type.value === "focus_box";
    text.disabled = isFocus;
    text.placeholder = isFocus ? "聚焦框不需要文字" : "例如：关键数据：3 倍增长";
    position.disabled = isFocus;
    focusControls.hidden = !isFocus;
  };
  type.addEventListener("change", updateComponentForm);
  updateComponentForm();
  const directives = el("div", { class: "directive-list" });
  for (const item of scene.surgical_directives || []) directives.append(el("div", { class: "directive-item" },
    el("div", {}, el("strong", {}, item.id), el("span", { class: "minor" }, ` ${item.component_type === "text_callout" ? "文字提示卡" : item.component_type === "info_label" ? "信息标签" : "聚焦框"} · ${clock(item.start_seconds)}`), item.text ? el("p", {}, item.text) : null),
    el("div", { class: "inline-actions" }, button("定位", "quiet small", () => seek(sceneRelativeTime(scene, item.start_seconds), true)), button("删除", "quiet small danger", () => mutate(`/scenes/${encodeURIComponent(scene.id)}/surgical-directives/${encodeURIComponent(item.id)}`, { method: "DELETE" }, "组件已删除，请刷新审核预览"))),
  ));
  return el("section", { class: "panel surgical-panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "定点组件调整"), el("p", {}, "在当前播放时刻下达一个小而明确的画面指令；它会写入审核预览和最终成片，不进入手工剪辑器。"))),
    el("div", { class: "panel-body surgical-form" },
      el("div", { class: "review-current-target" }, "当前定位：", targetTime, button("读取当前播放位置", "quiet small", updateTargetTime)),
      el("div", { class: "surgical-form-grid" }, type, position, seconds), text, focusControls,
      el("div", { class: "inline-actions" }, button("在当前时刻添加组件", "primary", () => mutate(`/scenes/${encodeURIComponent(scene.id)}/surgical-directives`, { method: "POST", body: { component_type: type.value, position: position.value, text: text.value, box: type.value === "focus_box" ? focusBox() : undefined, duration_seconds: Number(seconds.value), start_seconds: Number(scene.start_seconds || 0) + currentTime() } }, "定点组件已添加；请刷新本段审核预览确认效果"))),
      directives.children.length ? directives : el("p", { class: "form-note" }, "尚未添加定点组件。需要更换主体素材或改变整体镜头时，仍使用右侧“导演指令”。"),
    ),
  );
}

function renderPresenterLayoutEditor(scene) {
  const layout = presenterLayout(scene);
  const geometry = layout.geometry || { x: .035, y: .04, width: .29 };
  const template = el("select", { "aria-label": "数字人版式模板" });
  for (const item of presenterLayouts().templates || []) {
    template.append(el("option", { value: item.id }, item.name));
  }
  template.value = layout.template.id;
  const x = el("input", { type: "range", min: "0", max: "0.7", step: "0.01", value: String(geometry.x), "aria-label": "数字人横向位置" });
  const y = el("input", { type: "range", min: "0", max: "0.72", step: "0.01", value: String(geometry.y), "aria-label": "数字人纵向位置" });
  const width = el("input", { type: "range", min: "0.12", max: "0.7", step: "0.01", value: String(geometry.width), "aria-label": "数字人大小" });
  const cropBottom = el("input", { type: "range", min: "0", max: "0.30", step: "0.01", value: String(layout.cropBottom), "aria-label": "裁掉数字人原片底部无效区" });
  const initialFaceCrop = layout.faceCrop || { x: .5, y: 0, zoom: 1 };
  const faceX = el("input", { type: "range", min: "0", max: "1", step: "0.01", value: String(initialFaceCrop.x ?? .5), "aria-label": "圆形头像取景横向位置" });
  const faceY = el("input", { type: "range", min: "0", max: "1", step: "0.01", value: String(initialFaceCrop.y ?? 0), "aria-label": "圆形头像取景纵向位置" });
  const faceZoom = el("input", { type: "range", min: "1", max: "2.4", step: "0.05", value: String(initialFaceCrop.zoom ?? 1), "aria-label": "圆形头像取景放大倍率" });
  const shape = el("select", { "aria-label": "数字人画中画外框" },
    el("option", { value: "rounded" }, "圆角矩形（推荐）"),
    el("option", { value: "circle" }, "圆形头像"),
    el("option", { value: "rectangle" }, "直角矩形"),
  );
  shape.value = layout.shape || "rounded";
  const readout = el("span", { class: "minor" });
  const aspect = (state.project.intake || {}).aspect;
  const previewBox = el("div", { class: `presenter-layout-preview-box ${aspect === "landscape" ? "landscape" : "portrait"}` });
  const sourcePath = presenterFor(scene).source_path;
  const facePreview = sourcePath ? el("video", { class: "presenter-layout-face-preview", muted: "", playsinline: "", preload: "auto", src: mediaURL(projectId, sourcePath), "aria-label": "数字人静态取景预览" }) : null;
  if (facePreview) {
    // This surface edits crop and placement; it is not a media player. Keep
    // the source explicitly muted and paused so opening the workbench can
    // never produce hidden avatar audio or an unexplained moving thumbnail.
    facePreview.defaultMuted = true;
    facePreview.muted = true;
    facePreview.pause();
  }
  const preview = el("div", { class: `presenter-layout-preview${facePreview ? " has-source" : ""}` }, facePreview, el("span", { class: "presenter-layout-preview-label" }, "数字人"));
  previewBox.append(preview, el("div", { class: "presenter-layout-caption-safe" }, "字幕安全区"));
  let cropControls = null;
  function draftGeometry() {
    const w = Number(width.value), px = Math.min(1 - w, Number(x.value));
    return { x: Number(px.toFixed(4)), y: Number(y.value), width: Number(w.toFixed(4)) };
  }
  function draftCropBottom() { return Number(Number(cropBottom.value).toFixed(4)); }
  function draftFaceCrop() {
    return { x: Number(Number(faceX.value).toFixed(4)), y: Number(Number(faceY.value).toFixed(4)), zoom: Number(Number(faceZoom.value).toFixed(4)) };
  }
  function repaintFacePreview() {
    const isCircle = shape.value === "circle";
    if (cropControls) cropControls.hidden = !isCircle;
    if (!facePreview) return;
    if (!isCircle || !facePreview.videoWidth || !facePreview.videoHeight) {
      facePreview.style.cssText = "";
      return;
    }
    const sourceWidth = facePreview.videoWidth;
    const sourceHeight = facePreview.videoHeight;
    const cropHeight = sourceHeight * (1 - draftCropBottom());
    const baseSide = Math.min(sourceWidth, cropHeight);
    const side = baseSide / draftFaceCrop().zoom;
    const cropX = (sourceWidth - side) * draftFaceCrop().x;
    const cropY = (cropHeight - side) * draftFaceCrop().y;
    facePreview.style.width = `${(sourceWidth / side) * 100}%`;
    facePreview.style.height = `${(sourceHeight / side) * 100}%`;
    facePreview.style.left = `${-(cropX / side) * 100}%`;
    facePreview.style.top = `${-(cropY / side) * 100}%`;
  }
  function repaint() {
    const value = draftGeometry();
    preview.style.left = `${value.x * 100}%`;
    preview.style.top = `${value.y * 100}%`;
    preview.style.width = `${value.width * 100}%`;
    preview.classList.toggle("circle", shape.value === "circle");
    preview.classList.toggle("rectangle", shape.value === "rectangle");
    repaintFacePreview();
    const face = draftFaceCrop();
    const faceText = shape.value === "circle" ? ` · 头像取景 ${Math.round(face.zoom * 100)}% / 横向 ${Math.round(face.x * 100)}% / 纵向 ${Math.round(face.y * 100)}%` : "";
    readout.textContent = `位置 ${Math.round(value.x * 100)}% / ${Math.round(value.y * 100)}% · 宽度 ${Math.round(value.width * 100)}% · ${shape.options[shape.selectedIndex].text} · 裁掉底部 ${Math.round(draftCropBottom() * 100)}%${faceText}`;
  }
  [x, y, width, cropBottom, shape, faceX, faceY, faceZoom].forEach((input) => input.addEventListener("input", repaint));
  if (facePreview) facePreview.addEventListener("loadedmetadata", () => {
    facePreview.pause();
    repaint();
  });
  const name = el("input", { value: layout.customized ? "本段自定义版式" : layout.template.name, maxlength: "80", "aria-label": "版式名称" });
  cropControls = el("div", { class: "presenter-face-crop-controls" },
    el("div", { class: "presenter-layout-sliders" },
      el("label", {}, "头像左右", faceX), el("label", {}, "头像上下", faceY), el("label", {}, "脸部放大", faceZoom),
    ),
    el("p", { class: "form-note" }, "圆形框内直接调整取景位置和放大倍率。先让脸部居中、眼睛略高于中线，再保存并批量应用给同一角色。"),
    button("重置圆形取景", "quiet small", () => { faceX.value = ".5"; faceY.value = "0"; faceZoom.value = "1"; repaint(); }),
  );
  repaint();
  return el("div", { class: "presenter-layout-editor" },
    el("strong", {}, "数字人画中画位置与大小"),
    el("p", { class: "form-note" }, "宽度控制整体大小，高度随原视频比例自动计算；圆形头像可单独调整脸部取景。不会拉伸人物，也不会改变原声时长。"),
    previewBox,
    el("label", { class: "control-label" }, "复用模板", template),
    el("label", { class: "control-label" }, "数字人外框", shape),
    el("div", { class: "presenter-layout-sliders" },
      el("label", {}, "左右", x), el("label", {}, "上下", y), el("label", {}, "大小", width),
    ),
    el("label", { class: "control-label" }, "裁掉原片底部无效区", cropBottom),
    cropControls,
    readout,
    el("div", { class: "inline-actions" },
      button("应用模板到本段", "small", () => mutate(`/scenes/${encodeURIComponent(scene.id)}`, { method: "PATCH", body: { presenter_layout_template_id: template.value } }, "已应用模板到本段")),
      button("保存本段微调", "small", () => mutate(`/scenes/${encodeURIComponent(scene.id)}`, { method: "PATCH", body: { presenter_layout_template_id: template.value, presenter_layout: draftGeometry(), presenter_crop_bottom: draftCropBottom(), presenter_shape: shape.value, presenter_face_crop: draftFaceCrop() } }, "本段数字人画面已更新，请刷新实际关键帧")),
    ),
    el("label", { class: "control-label" }, "把当前布局保存为模板", name),
    el("div", { class: "inline-actions" },
      button("保存并应用当前角色", "quiet small", () => savePresenterLayout(scene, { name: name.value, geometry: draftGeometry(), crop_bottom: draftCropBottom(), shape: shape.value, face_crop: draftFaceCrop(), apply_scope: "speaker" }, "已保存并应用到当前角色的全部片段；请同步审核预览")),
      button("保存并应用全部片段", "quiet small", () => savePresenterLayout(scene, { name: name.value, geometry: draftGeometry(), crop_bottom: draftCropBottom(), shape: shape.value, face_crop: draftFaceCrop(), apply_scope: "all", set_default: true }, "已保存并应用到全部数字人片段；请同步审核预览")),
    ),
  );
}

function renderSubtitleEditor(scene) {
  const draft = subtitleDraftFor(scene);
  let style = draft.style;
  const templates = subtitleStyles().templates || [];
  const template = el("select", { "aria-label": "字幕方案" });
  for (const item of templates) template.append(el("option", { value: item.id }, item.name));
  template.value = draft.template_id || style.template_id || "subtitle-default";
  const planName = el("input", { value: style.template_name || "标准中文短句字幕", maxlength: "80", "aria-label": "字幕方案名称" });
  const enabled = el("input", { type: "checkbox", checked: style.enabled ? "" : null, "aria-label": "显示字幕" });
  const font = el("select", { "aria-label": "字幕字体" });
  ["Microsoft YaHei", "Source Han Sans SC", "PingFang SC", "Noto Sans CJK SC", "SimSun"].forEach((name) => font.append(el("option", { value: name }, name)));
  font.value = style.font || "Microsoft YaHei";
  const fontSize = el("input", { type: "range", min: "24", max: "80", step: "1", value: String(style.font_size || 42), "aria-label": "字幕大小" });
  const bold = el("input", { type: "checkbox", checked: style.bold ? "" : null, "aria-label": "加粗字幕" });
  const textColor = el("input", { type: "color", value: style.text_color || "#FFFFFF", "aria-label": "字幕颜色" });
  const outlineColor = el("input", { type: "color", value: style.outline_color || "#07111F", "aria-label": "描边颜色" });
  const outlineWidth = el("input", { type: "range", min: "0", max: "8", step: "0.5", value: String(style.outline_width ?? 3), "aria-label": "描边宽度" });
  const backgroundEnabled = el("input", { type: "checkbox", checked: style.background_enabled ? "" : null, "aria-label": "显示字幕底板" });
  const backgroundColor = el("input", { type: "color", value: style.background_color || "#07111F", "aria-label": "底板颜色" });
  const backgroundOpacity = el("input", { type: "range", min: "0", max: "100", step: "1", value: String(style.background_opacity ?? 68), "aria-label": "底板透明度" });
  const position = style.position || { x: .5, y: .89, width: .84, anchor: "bottom-center" };
  const anchor = el("select", { "aria-label": "字幕锚点" },
    el("option", { value: "bottom-center" }, "下方居中"),
    el("option", { value: "center" }, "画面居中"),
    el("option", { value: "top-center" }, "上方居中"),
  );
  anchor.value = position.anchor || "bottom-center";
  const x = el("input", { type: "range", min: "0.22", max: "0.78", step: "0.01", value: String(position.x ?? .5), "aria-label": "字幕左右位置" });
  const y = el("input", { type: "range", min: "0.07", max: "0.94", step: "0.01", value: String(position.y ?? .89), "aria-label": "字幕上下位置" });
  const width = el("input", { type: "range", min: "0.42", max: "0.94", step: "0.01", value: String(position.width ?? .84), "aria-label": "字幕最大宽度" });
  const maxLines = el("select", { "aria-label": "字幕最大行数" }, el("option", { value: "1" }, "最多 1 行"), el("option", { value: "2" }, "最多 2 行（推荐）"), el("option", { value: "3" }, "最多 3 行"));
  maxLines.value = String(style.max_lines || 2);
  const previewBox = el("div", { class: `subtitle-layout-preview-box ${reviewLayoutKind()}` });
  const previewCaption = el("div", { class: "subtitle-layout-preview-caption" }, "这是当前字幕的实时预览");
  const safeArea = el("div", { class: "subtitle-layout-safe-area" }, "真实视频左侧会即时看到相同字幕层");
  previewBox.append(previewCaption, safeArea);
  const readout = el("span", { class: "minor" });
  const repaint = () => {
    style.enabled = enabled.checked;
    style.font = font.value;
    style.font_size = Number(fontSize.value);
    style.bold = bold.checked;
    style.text_color = textColor.value;
    style.outline_color = outlineColor.value;
    style.outline_width = Number(outlineWidth.value);
    style.background_enabled = backgroundEnabled.checked;
    style.background_color = backgroundColor.value;
    style.background_opacity = Number(backgroundOpacity.value);
    style.max_lines = Number(maxLines.value);
    const nextWidth = Number(width.value);
    const safeX = Math.min(1 - nextWidth / 2, Math.max(nextWidth / 2, Number(x.value)));
    if (Number(x.value) !== safeX) x.value = String(safeX);
    style.position = { x: safeX, y: Number(y.value), width: nextWidth, anchor: anchor.value };
    applyCaptionStyle(previewCaption, style);
    readout.textContent = `位置 ${Math.round(style.position.x * 100)}% / ${Math.round(style.position.y * 100)}% · 宽度 ${Math.round(style.position.width * 100)}% · 字号 ${style.font_size}`;
    refreshLiveCaption(scene);
  };
  [enabled, font, fontSize, bold, textColor, outlineColor, outlineWidth, backgroundEnabled, backgroundColor, backgroundOpacity, anchor, x, y, width, maxLines].forEach((input) => input.addEventListener("input", repaint));
  template.addEventListener("change", () => {
    const selected = templates.find((item) => item.id === template.value);
    if (!selected) return;
    draft.template_id = selected.id;
    style = structuredClone(Object.assign({}, selected.style, { position: Object.assign({}, selected.style.position || {}) }));
    draft.style = style;
    subtitleDrafts.set(scene.id, draft);
    planName.value = selected.name || "标准中文短句字幕";
    enabled.checked = Boolean(style.enabled);
    font.value = style.font || "Microsoft YaHei";
    fontSize.value = String(style.font_size || 42);
    bold.checked = Boolean(style.bold);
    textColor.value = style.text_color || "#FFFFFF";
    outlineColor.value = style.outline_color || "#07111F";
    outlineWidth.value = String(style.outline_width ?? 3);
    backgroundEnabled.checked = Boolean(style.background_enabled);
    backgroundColor.value = style.background_color || "#07111F";
    backgroundOpacity.value = String(style.background_opacity ?? 68);
    anchor.value = (style.position || {}).anchor || "bottom-center";
    x.value = String((style.position || {}).x ?? .5);
    y.value = String((style.position || {}).y ?? .89);
    width.value = String((style.position || {}).width ?? .84);
    maxLines.value = String(style.max_lines || 2);
    repaint();
  });
  repaint();

  const cueRows = el("div", { class: "subtitle-edit-cues" });
  for (const [index, cue] of effectiveCaptionCues(scene).entries()) {
    const cueId = cue.id || captionCueId(index);
    const text = el("textarea", { rows: "2", maxlength: "240", "aria-label": `编辑 ${cueId} 字幕` });
    text.value = cue.text || "";
    text.addEventListener("input", () => {
      const value = text.value.trim();
      if (value) draft.cue_overrides[cueId] = value;
      else delete draft.cue_overrides[cueId];
      refreshLiveCaption(scene);
    });
    cueRows.append(el("div", { class: "subtitle-edit-cue" },
      el("time", {}, `${clock(cue.start_seconds)} — ${clock(cue.end_seconds)}`),
      text,
      button("定位", "quiet small", () => (reviewCaptionControllers.get(scene.id) || {}).seek?.(Number(cue.start_seconds || 0))),
    ));
  }

  return el("section", { class: "panel subtitle-editor" },
    el("div", { class: "panel-head" }, el("div", {},
      el("h4", {}, "字幕编辑器"),
      el("p", {}, "文字、位置和样式是独立图层。拖动控件会实时作用于左侧播放器；保存不会重新生成画面、配音或数字人。"),
    )),
    el("div", { class: "panel-body subtitle-editor-body" },
      el("label", { class: "control-label" }, "复用字幕方案", template),
      previewBox,
      el("div", { class: "subtitle-style-grid" },
        el("label", {}, "字体", font), el("label", {}, "字号", fontSize),
        el("label", { class: "check-row" }, bold, "加粗"), el("label", {}, "文字颜色", textColor),
        el("label", {}, "描边颜色", outlineColor), el("label", {}, "描边宽度", outlineWidth),
        el("label", { class: "check-row" }, backgroundEnabled, "字幕底板"), el("label", {}, "底板颜色", backgroundColor),
        el("label", {}, "底板不透明度", backgroundOpacity),
      ),
      el("div", { class: "subtitle-position-grid" },
        el("label", {}, "对齐锚点", anchor), el("label", {}, "左右", x), el("label", {}, "上下", y),
        el("label", {}, "字幕宽度", width), el("label", {}, "最大行数", maxLines),
      ),
      readout,
      el("div", { class: "inline-actions" },
        button("保存当前片段字幕", "primary", () => saveSceneSubtitles(scene, draft)),
        button("保存方案并应用到全部片段", "quiet", () => saveSubtitleStyleForAll(scene, draft, planName.value)),
        button("设为默认字幕样式", "quiet", () => saveSubtitleStyleAsDefault(draft)),
      ),
      el("p", { class: "minor subtitle-default-note" }, "默认值会在以后新建视频时作为起始字幕样式，不会覆盖本项目或其他已存在项目。"),
      el("label", { class: "control-label" }, "字幕方案名称", planName),
      el("div", { class: "subtitle-cue-editor-head" }, el("strong", {}, "逐句字幕文字"), el("span", { class: "minor" }, "改字不会修改原始配音，也不会改变片段时长。")),
      cueRows.children.length ? cueRows : el("div", { class: "narration-empty" }, "本段还没有可编辑的短句字幕。请先生成审核预览或确认本段旁白。"),
      el("div", { class: "inline-actions" }, button("还原未保存修改", "quiet small", () => { subtitleDrafts.delete(scene.id); refreshLiveCaption(scene); render(); })),
    ),
  );
}

function renderReviewControls(scene) {
  const presenter = presenterFor(scene);
  const avatarReady = isAvatarProject() && avatarTimelineApplied();
  const mainVisualNeeded = !isAvatarProject() || presenter.treatment !== "fullscreen";
  const sources = ["human_provided", "web_download", "project_library", "ai_generated"];
  const choices = el("div", { class: "choice-grid" });
  for (const source of sources) choices.append(el("button", { class: `choice ${scene.source_strategy === source ? "selected" : ""}`, type: "button", onclick: () => mutate(`/scenes/${encodeURIComponent(scene.id)}`, { method: "PATCH", body: { source_strategy: source } }, "已记录素材来源策略") }, sourceLabels[source]));
  const usage = activeUsage(scene.id);
  const selector = el("select", {});
  selector.append(el("option", { value: "" }, "选择已登记的素材"));
  for (const asset of state.assets.filter(isLiveAsset)) selector.append(el("option", { value: asset.id, selected: usage && usage.asset_id === asset.id ? "" : null }, `${asset.id} · ${asset.name}`));
  const note = el("textarea", { placeholder: "例如：首帧需保留产品轮廓；高潮帧换成 S-008，镜头由远推近，不能改动口播节奏。" });
  const keyframeReview = scene.keyframe_review;
  const candidate = aiVisualCandidate(scene);
  const keyframeGeneration = keyframeJobForScene(scene);
  const isGenerating = keyframeGeneration.status === "generating";
  const generationPanel = el("div", { class: "keyframe-generate-panel" });
  if (avatarReady && presenter.treatment !== "hidden") {
    const pipNeedsBackground = ["pip_top_left", "custom"].includes(presenter.treatment) && !currentAsset(scene.id);
    generationPanel.append(
      el("div", { class: "keyframe-generate-copy" },
        el("strong", {}, presenter.treatment === "fullscreen" ? "数字人原片审核帧" : "数字人画中画合成审核帧"),
        el("span", {}, presenter.treatment === "fullscreen"
          ? "从本段绑定的数字人母版提取首帧和高潮帧；声音和时长不会重算。"
          : pipNeedsBackground
            ? "请先为主体画面选择或生成一条实际素材，然后才能合成左上角数字人关键帧。"
            : "将用已选主体画面和数字人原片生成真实合成的首帧、高潮帧，供你审核字幕与版式；不会替换主体素材。"),
      ),
      button("生成数字人合成审核帧", "quiet", () => mutate(`/scenes/${encodeURIComponent(scene.id)}/avatar-keyframes`, { method: "POST" }, "数字人实际合成审核帧已生成，请逐张审核"), pipNeedsBackground),
    );
  }
  if (scene.source_strategy === "ai_generated") {
    const quality = el("select", {});
    quality.append(el("option", { value: "low" }, "低质量试样（推荐）"), el("option", { value: "medium" }, "中质量"), el("option", { value: "high" }, "高质量精修"));
    const currentVisualPlan = visualPlan(scene);
    const planSaved = currentVisualPlan.status === "saved" && currentVisualPlan.engine === "openai_image";
    const hasRetryableFrame = ["failed", "completed_with_failures"].includes(keyframeGeneration.status)
      && Object.values(keyframeGeneration.anchors || {}).some((item) => item && item.status === "failed");
    const startLabel = isGenerating ? "正在后台生成" : hasRetryableFrame ? "继续生成失败关键帧" : currentVisualPlan.engine !== "openai_image" ? "请使用上方动态素材按钮" : !planSaved ? "请先保存画面方案" : keyframeReview && keyframeReview.status === "approved" ? "已通过关键帧" : keyframeReview ? "重新生成 AI 主体画面" : "生成 AI 主体画面";
    const startButton = button(startLabel, "primary", () => {
      if (isGenerating || (keyframeReview && keyframeReview.status === "approved")) return;
      const confirmText = hasRetryableFrame
        ? `将仅继续失败的关键帧。已成功图片不会再次调用生图服务，是否继续？`
        : `本次将通过 OpenAI 兼容服务调用 gpt-image-2，生成首帧和高潮帧共 2 张图片，质量为${quality.options[quality.selectedIndex].text}。图片会产生接口费用，是否继续？`;
      if (!window.confirm(confirmText)) return;
      startKeyframeGeneration(scene, quality.value, hasRetryableFrame);
    }, Boolean(!planSaved || isGenerating || (keyframeReview && keyframeReview.status === "approved")));
    const jobPanel = el("div", { class: `keyframe-job-live${isGenerating ? " is-running" : ""}`, "data-keyframe-job-scene": scene.id });
    const anchors = keyframeGeneration.anchors || {};
    jobPanel.append(
      el("strong", {}, isGenerating ? "后台关键帧任务" : "关键帧任务状态"),
      el("span", { "data-keyframe-job-summary": "" }, isGenerating ? "后台任务已经启动；不会刷新左侧视频或清空你的审核操作。" : keyframeGeneration.error || "尚未提交关键帧任务"),
      el("div", { class: "keyframe-job-anchors" },
        el("span", { class: `status ${(anchors.first_frame || {}).status || "queued"}`, "data-keyframe-anchor": "first_frame" }, `首帧：${statusLabels[(anchors.first_frame || {}).status] || "等待中"}${(anchors.first_frame || {}).asset_id ? `（${anchors.first_frame.asset_id}）` : ""}`),
        el("span", { class: `status ${(anchors.climax_frame || {}).status || "queued"}`, "data-keyframe-anchor": "climax_frame" }, `高潮帧：${statusLabels[(anchors.climax_frame || {}).status] || "等待中"}${(anchors.climax_frame || {}).asset_id ? `（${anchors.climax_frame.asset_id}）` : ""}`),
      ),
      el("div", { class: "inline-actions", "data-keyframe-job-actions": "" }),
    );
    queueMicrotask(() => updateKeyframeJobIslands());
    generationPanel.append(
      el("div", { class: `keyframe-generate-copy ${isGenerating ? "is-generating" : ""}` }, el("strong", {}, isGenerating ? "正在后台生成 AI 主体画面" : "本场景 AI 主体画面"), el("span", {}, isGenerating ? "进度只会更新此任务卡。当前播放、滚动位置和未保存批注不会被重置。" : hasRetryableFrame ? `上次仅部分完成：${keyframeGeneration.error || "可继续失败关键帧"}` : candidate ? `已生成可采用的 AI 首帧：${candidate.id}` : "尚未生成实际 AI 画面"), el("span", {}, "将创建 AI 首帧 + 高潮帧。生成后，请在左侧审核区点击“采用 AI 主体画面并刷新预览”，才会替换旧网络素材。")),
      jobPanel,
      el("label", { class: "control-label" }, "生成质量", quality),
      startButton,
    );
  }
  if (scene.source_strategy === "web_download") {
    const assetJob = automationState().asset_generation || {};
    const isCurrentSceneRefresh = assetJob.status === "generating"
      && assetJob.mode === "scene_refresh"
      && Array.isArray(assetJob.scene_ids)
      && assetJob.scene_ids[0] === scene.id;
    const productionBusy = assetAutomationRunning() || narrationAutomationRunning() || videoRenderRunning();
    generationPanel.append(
      el("div", { class: `keyframe-generate-copy ${isCurrentSceneRefresh ? "is-generating" : ""}` },
        el("strong", {}, isCurrentSceneRefresh ? "正在更换当前场景素材" : "当前场景一键换素材"),
        el("span", {}, isCurrentSceneRefresh
          ? "正在从 Pexels 下载新的候选画面，并重建本场景的首帧与高潮帧。请不要重复点击。"
          : "只重找当前场景；其他场景不会重新下载，旧素材和原使用编号会保留在素材台账中。"),
        el("span", {}, "可先在下方“审核批注”写明想换成什么画面；留空时系统会自动切换检索角度，避开当前画面。"),
      ),
      button(isCurrentSceneRefresh ? "正在更换素材…" : "一键换当前场景素材", "primary", () => {
        refreshCurrentSceneNetworkAsset(scene, note.value);
      }, productionBusy),
    );
    if (isCurrentSceneRefresh) generationPanel.append(renderAutomationStatus(true));
  }
  const keyframeApprovalReady = keyframeReview && keyframeReview.status === "approved";
  const keyframesRequired = scene.source_strategy === "ai_generated" || (isAvatarProject() && presenter.treatment !== "hidden");
  if (scene.source_strategy === "ai_generated" && keyframeReview && !keyframeApprovalReady) {
    generationPanel.append(el("div", { class: "inline-actions keyframe-review-actions" },
      button("通过这组关键帧", "primary", () => {
        if (keyframeReview.timeline.some((item) => item.status !== "approved")) return showToast("请先在中间逐张通过首帧和高潮帧", true);
        mutate(`/scenes/${encodeURIComponent(scene.id)}/keyframes/review`, { method: "POST", body: { action: "approve", note: note.value } }, "关键帧审核已通过，现在可以通过场景");
      }),
      button("要求重新调整", "danger", () => {
        if (!note.value.trim()) return showToast("请先在下方填写关键帧调整意见", true);
        mutate(`/scenes/${encodeURIComponent(scene.id)}/keyframes/review`, { method: "POST", body: { action: "request_revision", note: note.value } }, "已记录关键帧调整意见");
      }),
    ));
  }
  const panel = el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "导演指令"), el("p", {}, "只描述目标与判断，不手调帧。"))),
    isAvatarProject() ? el("div", { class: "panel-body control-group" },
      el("span", { class: "control-label" }, "数字人出镜方式"),
      el("div", { class: "choice-grid" },
        ...[["fullscreen", "全屏主体"], ["pip_top_left", "左上角解说员"], ["custom", "自定义画中画"], ["hidden", "暂时隐藏"]].map(([treatment, label]) => el("button", { class: `choice ${presenter.treatment === treatment ? "selected" : ""}`, type: "button", disabled: avatarReady ? null : "", onclick: () => mutate(`/scenes/${encodeURIComponent(scene.id)}`, { method: "PATCH", body: { presenter_treatment: treatment } }, `已切换为${label}`) }, label)),
      ),
      el("p", { class: "form-note" }, avatarReady ? "全屏数字人不需要另选主体素材；画中画和隐藏模式可继续使用网络、AI 或人工素材。" : "请先在“数字人素材”页完成原声母版，并应用为真实时间线。"),
      avatarReady ? button(`替换 ${presenter.turn_id || "本段"} 数字人原片`, "quiet", () => { activeView = "avatar"; render(); }) : null,
      avatarReady && ["pip_top_left", "custom"].includes(presenter.treatment) ? renderPresenterLayoutEditor(scene) : null,
    ) : null,
    mainVisualNeeded ? el("div", { class: "panel-body control-group" },
      el("span", { class: "control-label" }, "这个场景的主体画面来自哪里？"),
      choices,
      scene.source_strategy === "ai_generated"
        ? renderVisualDesignPanel(scene)
        : scene.source_strategy === "web_download"
          ? el("div", { class: "source-branch-note" }, el("strong", {}, "网络下载模式"), el("span", {}, "将依据台词和审核批注从 Pexels 搜索并替换当前场景素材；AI 动态画面方案已收起，不会参与本次操作。"))
          : scene.source_strategy === "human_provided"
            ? el("div", { class: "source-branch-note" }, el("strong", {}, "人工提供模式"), el("span", {}, "请在下方画面时间线中，为每个时间区间选择已登记的图片或视频。"))
            : scene.source_strategy === "project_library"
              ? el("div", { class: "source-branch-note" }, el("strong", {}, "项目素材库模式"), el("span", {}, "请在下方画面时间线中选择项目素材库中已登记的素材。"))
              : null,
      renderVisualTimelineEditor(scene),
      generationPanel,
    ) : el("div", { class: "panel-body keyframe-gate" }, el("strong", {}, "数字人全屏主体"), el("span", {}, "此场景直接使用已锁定的数字人母版和原声音频，不需要再选网络或 AI 主体素材。"), generationPanel),
    keyframesRequired && keyframeReview
      ? el("div", { class: "panel-body keyframe-gate" }, el("strong", {}, keyframeApprovalReady ? "关键帧审核已通过" : "请先逐帧审核"), el("span", {}, keyframeApprovalReady ? "通过后，首帧和高潮帧图片已生成 U-xxx 使用编号。" : "在中间画面逐张检查图片、字幕和画面调整说明，全部通过后才能通过场景。"))
      : null,
    el("div", { class: "panel-body control-group" }, el("span", { class: "control-label" }, "审核批注 / 生成教案"), note, el("div", { class: "inline-actions" },
      button("记录批注", "", () => { if (!note.value.trim()) return showToast("请先填写批注", true); mutate("/annotations", { method: "POST", body: { scene_id: scene.id, anchor_kind: "climax_frame", text: note.value } }, "批注已写入决策记录"); }),
      button(keyframeApprovalReady || !keyframesRequired ? "场景通过" : "先通过关键帧", "primary", () => mutate(`/scenes/${encodeURIComponent(scene.id)}`, { method: "PATCH", body: { review_status: "approved" } }, "场景已标记为通过"), keyframesRequired && !keyframeApprovalReady),
      button("标记需要调整", "danger", () => mutate(`/scenes/${encodeURIComponent(scene.id)}`, { method: "PATCH", body: { review_status: "needs_adjustment" } }, "已标记为需要调整；如需换画面，请点击上方“一键换当前场景素材”")),
    )),
    scene.notes && scene.notes.length ? el("div", { class: "panel-body" }, el("span", { class: "control-label" }, "最近批注"), el("div", { class: "activity" }, scene.notes.slice(-3).reverse().map((item) => el("div", { class: "activity-item" }, el("time", {}, (item.at || "").slice(11, 16)), el("span", {}, item.text))))) : null,
  );
  return panel;
}

function renderAvatarNarrationReview(scene) {
  const presenter = presenterFor(scene);
  const sourcePath = presenter.source_path;
  const sourceStart = Number(presenter.source_start_seconds || 0);
  const sourceEnd = Number(presenter.source_end_seconds || 0);
  const duration = Math.max(0, sourceEnd - sourceStart);
  const audioKey = sourcePath ? `${scene.id}|${sourcePath}|${sourceStart}|${sourceEnd}` : "";
  const audio = sourcePath ? el("audio", { controls: "", preload: "metadata", src: mediaURL(projectId, sourcePath), "data-review-audio-key": audioKey }) : null;
  if (audio) {
    audio.addEventListener("loadedmetadata", () => {
      const saved = reviewUiMemory.audioByKey.get(audioKey);
      const target = saved ? saved.currentTime : sourceStart;
      audio.currentTime = Math.min(Math.max(0, target), Math.max(0, audio.duration - .05));
    });
    audio.addEventListener("timeupdate", () => { if (sourceEnd > sourceStart && audio.currentTime >= sourceEnd) audio.pause(); });
  }
  const sceneText = (scene.narration && scene.narration.text) || scene.description || "";
  return el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "数字人原声与字幕"), el("p", {}, "试听的是本段数字人原片的原生声音；不提供本地重配，避免嘴型与语速失真。"))),
    el("div", { class: "panel-body narration-panel" },
      sourcePath
        ? el("div", { class: "narration-take current" },
          el("div", { class: "take-head" }, el("strong", {}, "当前采用数字人原声"), status("native_avatar_audio")),
          el("span", { class: "minor" }, `${presenter.turn_id || "本段"} · 自然时长 ${duration.toFixed(2)} 秒 · ${presenter.asset_id || "数字人母版"}`),
          audio,
        )
        : el("div", { class: "report bad" }, "尚未绑定数字人母版。请先在“数字人素材”完成原声母版并应用真实时间线。"),
      el("label", { class: "control-label" }, "本段原声文本", el("textarea", { class: "narration-text", rows: "4", readonly: "" }, sceneText)),
      el("div", { class: "report" }, "如果台词、语气或嘴型不满意，请回到“数字人素材”替换对应 Txxx 原片，重新核验并合成母版；系统会明确要求重新应用时间线，而不会偷偷把音频拉长。"),
    ),
  );
}

function captureReviewInteractionState() {
  const side = app.querySelector(".review-side");
  if (side) reviewUiMemory.sideScrollTop = side.scrollTop;
  app.querySelectorAll(".review-side audio[data-review-audio-key]").forEach((audio) => {
    reviewUiMemory.audioByKey.set(audio.dataset.reviewAudioKey, {
      currentTime: Number(audio.currentTime || 0),
      volume: audio.volume,
      muted: audio.muted,
    });
    // A refresh, scene switch, or navigation must never leave an audio-only
    // element playing after its controls have moved off screen.
    audio.pause();
  });
}

function restoreReviewInteractionState() {
  const sideScrollTop = reviewUiMemory.sideScrollTop;
  const restore = () => {
    const side = app.querySelector(".review-side");
    if (side && sideScrollTop !== null) side.scrollTop = Math.min(sideScrollTop, Math.max(0, side.scrollHeight - side.clientHeight));
    app.querySelectorAll(".review-side audio[data-review-audio-key]").forEach((audio) => {
      const saved = reviewUiMemory.audioByKey.get(audio.dataset.reviewAudioKey);
      if (!saved) return;
      audio.volume = saved.volume;
      audio.muted = saved.muted;
      const restorePosition = () => {
        if (Number.isFinite(audio.duration) && audio.duration > 0) {
          const target = Math.min(Math.max(0, saved.currentTime), Math.max(0, audio.duration - .05));
          if (Math.abs(audio.currentTime - target) > .05) audio.currentTime = target;
        }
        audio.pause();
      };
      if (audio.readyState >= 1) restorePosition();
      else audio.addEventListener("loadedmetadata", restorePosition, { once: true });
    });
  };
  requestAnimationFrame(restore);
}

function renderNarrationReview(scene) {
  if (isAvatarProject()) return renderAvatarNarrationReview(scene);
  const narration = sceneNarration(scene);
  const current = currentNarration(scene);
  const candidate = candidateNarration(scene);
  const profiles = Array.isArray(voiceCatalog.profiles) ? voiceCatalog.profiles : [];
  const voiceReady = voiceCatalog.provider && voiceCatalog.provider.status === "available";
  const text = el("textarea", { class: "narration-text", maxlength: "5000", placeholder: "填写这个片段需要说的话；默认继承已确认脚本。" });
  text.value = narration.text || scene.description || "";
  const profile = el("select", { "aria-label": "选择本地音色" });
  profile.append(el("option", { value: "" }, voiceCatalog.default_voice ? `通用默认：${voiceCatalog.default_voice.name}` : "选择通用默认音色"));
  for (const item of profiles) {
    const selected = (candidate && candidate.profile_id === item.id) || (!candidate && current && current.profile_id === item.id) || (!current && voiceCatalog.default_voice && voiceCatalog.default_voice.id === item.id);
    profile.append(el("option", { value: item.id, selected: selected ? "" : null }, item.name));
  }
  const job = narration.job || {};
  const generating = job.status === "generating";
  const segment = segmentForScene(scene.id);
  const hasBaseline = Boolean(segment && (segment.versions || []).some((version) => version.id === segment.current_version_id && version.artifact_path));
  const currentBlock = current && current.audio_path
    ? el("div", { class: "narration-take current" },
      el("div", { class: "take-head" }, el("strong", {}, "当前采用配音"), status("current")),
      el("span", { class: "minor" }, `${current.id} · ${current.profile_name || "已确认音色"}${current.duration_seconds ? ` · 自然时长 ${Number(current.duration_seconds).toFixed(2)} 秒` : ""}`),
      el("audio", { controls: "", preload: "metadata", src: mediaURL(projectId, current.audio_path) }),
    )
    : el("div", { class: "narration-empty" }, "当前片段尚无已采用的独立配音。可先生成候选试听；首次完整成片仍会使用项目旁白。");

  const children = [
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "配音与字幕"), el("p", {}, "音色是通用设置；每个片段只保存自己的试听候选和采用记录。"))),
    el("div", { class: "panel-body narration-panel" },
      currentBlock,
      el("label", { class: "control-label" }, "本片段脚本", text),
      el("label", { class: "control-label" }, "候选音色", profile),
      el("div", { class: "inline-actions" },
        button(generating ? "正在生成候选配音…" : "生成候选配音", "primary", () => startSceneNarrationCandidate(scene, text.value, profile.value), generating || !voiceReady),
        button("管理通用音色", "quiet", openAudioCenter),
      ),
      !voiceReady ? el("div", { class: "report bad" }, "Haike Video 本地配音当前不可用。请完成安装并在通用配音中心确认音色。") : null,
      generating ? el("div", { class: "narration-job is-running" }, status("generating"), el("span", {}, `正在生成 ${job.version_id || "候选版本"}，页面完成后会自动刷新；不会覆盖当前成片。`)) : null,
      job.status === "failed" ? el("div", { class: "report bad" }, job.error || "候选配音生成失败，请检查本地配音服务后重试。") : null,
    ),
  ];

  if (candidate && candidate.audio_path) {
    const localPatch = (state.patches || []).find((patch) => patch.source === "scene_narration" && patch.narration_version_id === candidate.id && !["rolled_back"].includes(patch.status));
    const composing = localPatch && localPatch.status === "rendering";
    const rendered = localPatch && localPatch.status === "rendered";
    const impact = candidate.timeline_impact || (localPatch && localPatch.timeline_impact) || null;
    const naturalDuration = Number(candidate.duration_seconds || candidate.raw_duration_seconds || 0);
    const currentDuration = segment ? Number(segment.end_seconds || 0) - Number(segment.start_seconds || 0) : 0;
    const impactDelta = impact ? Number(impact.delta_seconds || 0) : naturalDuration - currentDuration;
    const downstream = impact && Array.isArray(impact.changes)
      ? impact.changes.filter((item) => item.scene_id !== scene.id && Math.abs(Number(item.shift_seconds || 0)) > 0.001)
      : [];
    const cueList = el("div", { class: "subtitle-cues" });
    for (const cue of (candidate.subtitle_cues || [])) {
      cueList.append(el("div", { class: "subtitle-cue" }, el("time", {}, `${clock(cue.start_seconds)} — ${clock(cue.end_seconds)}`), el("span", {}, cue.text)));
    }
    children.push(el("div", { class: "panel-body narration-candidate" },
      el("div", { class: "take-head" }, el("strong", {}, "候选配音（先试听，再采用）"), status(rendered ? "candidate_rendered" : candidate.status || "candidate_ready")),
      el("span", { class: "minor" }, `${candidate.id} · ${candidate.profile_name || "本地音色"} · ${naturalDuration ? "自然时长 " + naturalDuration.toFixed(2) + " 秒" : "采用时自动测量真实时长（不会沿用旧片段时长）"}`),
      el("audio", { controls: "", preload: "metadata", src: mediaURL(projectId, candidate.audio_path) }),
      impact ? el("div", { class: "report" },
        el("strong", {}, "采用影响预览"),
        el("div", {}, `当前时间槽 ${currentDuration.toFixed(2)} 秒；候选配音 ${naturalDuration.toFixed(2)} 秒；本段将${impactDelta >= 0 ? "延长" : "缩短"} ${Math.abs(impactDelta).toFixed(2)} 秒。`),
        el("div", { class: "minor" }, downstream.length
          ? `后续 ${downstream.map((item) => item.scene_id).join("、")} 的内容不会重新生成，只会整体${impactDelta >= 0 ? "后移" : "前移"}。`
          : "后续场景内容和绝对位置均无需变化。"),
      ) : null,
      (candidate.subtitle_cues || []).length ? el("details", { class: "subtitle-details" }, el("summary", {}, "查看候选字幕分句"), cueList) : null,
      localPatch && localPatch.status === "blocked" ? el("div", { class: "report bad" }, ((localPatch.render_report || {}).checks || []).filter((check) => !check.ok).map((check) => check.detail).join("；") || "局部合成未通过，请查看片段基线状态。") : null,
      rendered && localPatch.composition_candidate_path ? el("a", { class: "button quiet", href: mediaURL(projectId, localPatch.composition_candidate_path), target: "_blank", rel: "noreferrer" }, "打开局部成片预览") : null,
      el("div", { class: "inline-actions" },
        button(composing ? "正在按自然时长合成…" : rendered ? "局部预览已就绪" : "采用自然时长并局部合成", "primary", () => applySceneNarrationCandidate(scene, candidate.id), composing || rendered || !hasBaseline),
        rendered ? button("确认合并到完整成片", "", () => mutate(`/patches/${localPatch.id}/promote`, { method: "POST" }, "该片段的新配音已合并到完整成片，项目旁白也已同步更新")) : null,
      ),
      !hasBaseline ? el("p", { class: "form-note" }, "先完成一次完整成片，系统才会建立可冻结的片段基线；候选仍可先试听。") : el("p", { class: "form-note" }, "系统不会拉伸语速。局部合成只重做当前片段；前序内容冻结，后续内容仅随时间线移动。"),
    ));
  }
  return el("section", { class: "panel" }, ...children);
}

function updateAnchor(scene, anchor, anchorStatus) {
  mutate(`/scenes/${encodeURIComponent(scene.id)}`, { method: "PATCH", body: { anchor_kind: anchor.kind, anchor_status: anchorStatus } }, `${anchorLabels[anchor.kind]}已标记为${statusLabels[anchorStatus]}`);
}

function formatAssetBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

const assetAuditLabels = {
  active: "正在使用",
  unused: "可清理",
  missing: "文件缺失",
  record_only: "仅登记",
  trashed: "回收站",
};

async function scanAssetLibrary() {
  if (assetLibraryAuditLoading) return;
  assetLibraryAuditLoading = true;
  render();
  try {
    assetLibraryAudit = await api("/asset-library/audit");
    const summary = assetLibraryAudit.summary || {};
    showToast(`素材库扫描完成：${summary.active_count || 0} 个正在使用，${summary.unused_count || 0} 个未使用`);
  } catch (error) {
    showToast(error.message || "素材库扫描失败", true);
  } finally {
    assetLibraryAuditLoading = false;
    render();
  }
}

async function cleanupSelectedAssets() {
  const rows = (assetLibraryAudit && assetLibraryAudit.assets) || [];
  const selected = rows.filter((row) => assetLibrarySelection.has(row.id) && row.cleanup_eligible);
  if (!selected.length) {
    showToast("请先选择可清理素材", true);
    return;
  }
  const bytes = selected.reduce((total, row) => total + (Number(row.size_bytes) || 0), 0);
  if (!window.confirm(`将 ${selected.length} 个未使用素材（约 ${formatAssetBytes(bytes)}）移入本项目回收站？\n\n不会永久删除，之后可在素材库中一键恢复。`)) return;
  try {
    state = await api("/asset-library/cleanup", { method: "POST", body: { asset_ids: selected.map((row) => row.id), confirmed: true } });
    ensureSelection();
    assetLibrarySelection.clear();
    assetLibraryAudit = await api("/asset-library/audit");
    showToast(`已移入项目回收站：${selected.length} 个素材`);
  } catch (error) {
    showToast(error.message || "移入回收站失败", true);
  }
  render();
}

async function restoreAssetFromRecycleBin(assetId) {
  try {
    state = await api(`/asset-library/assets/${encodeURIComponent(assetId)}/restore`, { method: "POST" });
    ensureSelection();
    assetLibraryAudit = await api("/asset-library/audit");
    showToast("素材已恢复到原始项目路径");
  } catch (error) {
    showToast(error.message || "恢复素材失败", true);
  }
  render();
}

function renderAssets() {
  const audit = assetLibraryAudit;
  const summary = (audit && audit.summary) || {};
  const auditRows = (audit && audit.assets) || [];
  const query = assetLibrarySearch.trim().toLowerCase();
  const rows = auditRows.filter((row) => {
    if (assetLibraryFilter === "cleanable" && !row.cleanup_eligible) return false;
    if (assetLibraryFilter === "trashed" && row.status !== "trashed") return false;
    if (assetLibraryFilter === "issues" && !["missing", "record_only"].includes(row.status)) return false;
    if (!query) return true;
    return [row.id, row.name, row.path, row.source_type, row.status].join(" ").toLowerCase().includes(query);
  });
  const cleanableRows = auditRows.filter((row) => row.cleanup_eligible);
  const selectedCleanableCount = cleanableRows.filter((row) => assetLibrarySelection.has(row.id)).length;
  const selectedCleanableBytes = cleanableRows
    .filter((row) => assetLibrarySelection.has(row.id))
    .reduce((total, row) => total + (Number(row.size_bytes) || 0), 0);

  const filterSelect = el("select", { "aria-label": "素材库筛选" },
    el("option", { value: "all", selected: assetLibraryFilter === "all" ? "" : null }, "全部素材"),
    el("option", { value: "cleanable", selected: assetLibraryFilter === "cleanable" ? "" : null }, "仅看可清理"),
    el("option", { value: "trashed", selected: assetLibraryFilter === "trashed" ? "" : null }, "项目回收站"),
    el("option", { value: "issues", selected: assetLibraryFilter === "issues" ? "" : null }, "缺失 / 仅登记"),
  );
  filterSelect.addEventListener("change", () => { assetLibraryFilter = filterSelect.value; render(); });
  const search = el("input", { type: "search", value: assetLibrarySearch, placeholder: "按编号、名称或路径查找" });
  search.addEventListener("input", () => { assetLibrarySearch = search.value; render(); });
  const body = el("tbody", {});
  for (const row of rows) {
    const asset = (state.assets || []).find((item) => item.id === row.id) || {};
    const checkbox = el("input", { type: "checkbox", checked: assetLibrarySelection.has(row.id) ? "" : null, disabled: row.cleanup_eligible ? null : "", "aria-label": `选择 ${row.name}` });
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) assetLibrarySelection.add(row.id); else assetLibrarySelection.delete(row.id);
      render();
    });
    const refs = row.references && row.references.length
      ? row.references.slice(0, 3).map((reference) => el("span", { class: "usage-chip", title: reference.kind }, reference.label))
      : el("span", { class: "minor" }, row.history_usage_count ? `历史使用 ${row.history_usage_count} 次，当前未引用` : "当前未引用");
    body.append(el("tr", { class: `asset-audit-row is-${row.status}` },
      el("td", { class: "asset-select-cell" }, checkbox),
      el("td", {}, el("span", { class: "asset-code" }, row.id), el("span", { class: "asset-name" }, row.name), row.duplicate_count > 1 ? el("span", { class: "asset-duplicate" }, `重复文件组 · ${row.duplicate_count} 项`) : null),
      el("td", {}, el("div", {}, asset.type || row.type), el("div", { class: "minor" }, sourceLabels[asset.source_type] || row.source_type)),
      el("td", {}, el("span", { class: `asset-audit-status ${row.status}` }, assetAuditLabels[row.status] || row.status), el("div", { class: "minor" }, row.cleanup_reason)),
      el("td", { class: "asset-reference-cell" }, refs),
      el("td", {}, el("div", { class: "minor" }, formatAssetBytes(row.size_bytes)), row.path ? el("div", { class: "minor path" }, row.path) : null, row.missing_paths && row.missing_paths.length ? el("div", { class: "minor warning" }, `缺失：${row.missing_paths.join("、")}`) : null),
      el("td", {}, row.status === "trashed" ? button("恢复", "quiet small", () => restoreAssetFromRecycleBin(row.id)) : null),
    ));
  }
  const table = el("table", { class: "asset-table asset-audit-table" }, el("thead", {}, el("tr", {}, el("th", {}, "选择"), el("th", {}, "稳定素材"), el("th", {}, "类型与来源"), el("th", {}, "健康状态"), el("th", {}, "当前引用"), el("th", {}, "空间与路径"), el("th", {}, "操作"))), body);
  const metrics = audit ? el("div", { class: "asset-health-grid" },
    factBlock("正在使用", `${summary.active_count || 0}`, "当前时间线、配音、审核和局部任务"),
    factBlock("可安全清理", `${summary.unused_count || 0}`, `可回收 ${formatAssetBytes(summary.reclaimable_bytes)}`),
    factBlock("重复文件", `${summary.duplicate_group_count || 0} 组`, "只提示，不会自动删除"),
    factBlock("需处理", `${(summary.missing_count || 0) + (summary.record_only_count || 0)}`, `缺失 ${summary.missing_count || 0} · 仅登记 ${summary.record_only_count || 0}`),
  ) : el("div", { class: "asset-audit-empty" }, el("strong", {}, "还未扫描素材库"), el("span", {}, "扫描会识别当前引用、可回收空间、重复文件与缺失路径；不会删除或移动任何文件。"));
  return el("section", { class: "page" },
    pageHeader("素材库", "让每个素材有去向，也让每次清理可恢复", "扫描只读取项目资产；批量清理只会移动当前未引用、自动生成或下载的素材到项目回收站，绝不直接永久删除。", el("div", { class: "inline-actions" }, button("扫描素材库", "primary", scanAssetLibrary, assetLibraryAuditLoading), button("AI 生图", "", () => imageDialog.showModal()), button("登记素材", "", () => assetDialog.showModal()))),
    el("section", { class: "panel asset-governance" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "素材健康与回收"), el("p", {}, audit ? `最近扫描：${new Date(audit.generated_at).toLocaleString()}` : "建议在批量生成、替换或合成全片前扫描一次。"))), el("div", { class: "panel-body" },
      metrics,
      audit ? el("div", { class: "asset-governance-actions" },
        el("div", { class: "asset-filter-controls" }, filterSelect, search),
        el("div", { class: "inline-actions" },
          button("全选可清理", "quiet", () => { assetLibrarySelection = new Set(cleanableRows.map((row) => row.id)); render(); }, !cleanableRows.length),
          button("清空选择", "quiet", () => { assetLibrarySelection.clear(); render(); }, !assetLibrarySelection.size),
          button(`移入回收站（${selectedCleanableCount}）`, "danger", cleanupSelectedAssets, !selectedCleanableCount),
        ),
      ) : null,
      audit ? el("p", { class: "form-note" }, selectedCleanableCount ? `已选择 ${selectedCleanableCount} 个可清理素材，预计释放 ${formatAssetBytes(selectedCleanableBytes)}。` : "人工提供与项目素材库内容默认受保护；重复文件仅作提示，由你决定是否保留。") : null,
    )),
    el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "可追溯素材台账"), el("p", {}, audit ? `显示 ${rows.length}/${auditRows.length} 项；回收站内素材可直接恢复。` : `${state.assets.length} 个稳定素材，${state.usages.length} 次使用记录`))), el("div", { class: "panel-body" }, audit ? (rows.length ? table : el("div", { class: "empty" }, "当前筛选没有匹配素材。")) : el("div", { class: "empty" }, "请先扫描素材库，生成可治理的引用与空间报告。"))),
    el("div", { class: "grid-2" },
      el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "来源决策规则"))), el("div", { class: "panel-body policy-list" }, policy("人工提供", "保留原文件路径、提供人和授权信息。"), policy("网络下载", "需补来源链接、许可与下载时间；不能只写“网上找的”。"), policy("AI / 本地生成", "版本、模型或工具信息应记录到素材版本。"))),
      el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "素材治理规则"))), el("div", { class: "panel-body policy-list" }, policy("当前引用", "时间线、当前配音、数字人、关键帧审核和未结束的局部任务都会锁定素材。"), policy("回收站", "清理是移动而不是删除；保留编号、使用历史和原始路径，可一键恢复。"), policy("重复文件", "按内容哈希识别，仅提示重复，不会擅自删除仍在使用的素材。"))),
    ),
  );
}

function renderPatch() {
  const segment = selectedSegment();
  if (!segment) return el("section", { class: "page" }, pageHeader("局部热插拔", "暂无可操作片段", "请先导入场景。"));
  const scene = state.scenes.find((item) => segment.scene_ids.includes(item.id));
  const candidates = state.assets.filter((asset) => isLiveAsset(asset) && asset.type === "video");
  const hasBaselineCache = state.segments.every((item) => item.versions.some((version) => version.id === item.current_version_id && version.artifact_path));
  const candidateSelect = el("select", {});
  candidateSelect.append(el("option", { value: "" }, "暂不指定（先生成任务）"));
  for (const asset of candidates) candidateSelect.append(el("option", { value: asset.id }, `${asset.id} · ${asset.name}`));
  const instruction = el("textarea", { placeholder: "例如：仅替换这一段的结果展示，保留前后口播节奏和出入场边界；首帧要是产品正面，高潮帧为 3 张结果墙。" });
  const strict = el("input", { type: "radio", name: "patch-mode", value: "strict_freeze", checked: "" });
  const seam = el("input", { type: "radio", name: "patch-mode", value: "seam_transition" });
  const prepare = () => mutate("/patches", { method: "POST", body: { segment_id: segment.id, candidate_asset_id: candidateSelect.value || null, instruction: instruction.value, mode: seam.checked ? "seam_transition" : "strict_freeze" } }, "局部任务已建立；A/C 已按冻结合同保护");
  const scope = el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "目标片段与冻结合同"), el("p", {}, "画面热插拔时 B 可重做，A/C 内容只读冻结；配音热插拔会在场景审核中自动采用自然时长波纹模式。")), status(segment.state)),
    el("div", { class: "panel-body" },
      el("div", { class: "grid-3" }, factBlock("目标 B", segment.id, (scene && scene.title) || ""), factBlock("帧范围", `${segment.start_frame} — ${segment.end_frame}`, `${clock(segment.start_seconds)} — ${clock(segment.end_seconds)}`), factBlock("音频样本", `${segment.audio_start_sample} — ${segment.audio_end_sample}`, "用于样本级边界核对")),
      el("div", { class: "inline-actions", style: "margin-top:12px" },
        segment.state === "frozen" ? button("解除冻结", "quiet", () => mutate(`/segments/${segment.id}/freeze`, { method: "POST", body: { frozen: false } }, "片段已解除冻结")) : button("冻结此片段", "primary", () => mutate(`/segments/${segment.id}/freeze`, { method: "POST", body: { frozen: true } }, "已快照当前版本与边界")),
        hasBaselineCache ? el("span", { class: "status approved" }, "片段缓存已就绪") : button("建立片段缓存", "", () => mutate("/baseline-cache", { method: "POST" }, "已建立片段缓存；后续仅重编码 B")),
      ),
    ),
  );
  const form = el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "建立局部调整任务"), el("p", {}, "先决定范围与合同，再进入候选和渲染。"))),
    el("div", { class: "panel-body control-group" },
      el("label", { class: "control-label" }, "候选视频素材（可稍后指定）"), candidateSelect,
      el("label", { class: "control-label" }, "导演调整指令"), instruction,
      el("span", { class: "control-label" }, "边界模式"),
      el("label", { class: "minor" }, strict, " 严格冻结：A/C 不能重编码，未通过关键帧预检不能合并"),
      el("label", { class: "minor" }, seam, " 缝合转场：允许边界转场重编码，报告会明确标注"),
      el("div", { class: "inline-actions" }, button("建立仅 B 的渲染计划", "primary", prepare)),
    ),
  );
  const timeline = renderTimelinePanel();
  return el("section", { class: "page" },
    pageHeader("局部热插拔", "局部满意，丝滑并入；其他片段内容不被牵动", "画面替换按固定片段处理；配音替换由场景审核发起，按自然时长让后续内容顺延或前移。系统不接受重叠的未收口任务。"),
    timeline,
    el("div", { class: "grid-2" }, scope, form),
    renderPatchList(),
  );
}

function factBlock(label, value, detail) { return el("div", { class: "policy" }, el("strong", {}, label), el("span", {}, value), detail ? el("span", {}, detail) : null); }

function renderPatchList() {
  const list = el("div", { class: "patch-list" });
  const patches = state.patches.slice().reverse();
  for (const patch of patches) {
    const report = patch.render_report;
    const checks = (report && report.checks) || [];
    const card = el("article", { class: "patch-card" },
      el("div", { class: "patch-card-head" }, el("h5", {}, `${patch.id} · ${patch.segment_id}`), status(patch.status)),
      el("p", {}, patch.instruction),
      el("div", { class: "patch-facts" }, el("span", { class: "fact" }, patch.mode === "ripple_timeline" ? "自然配音波纹" : patch.mode === "strict_freeze" ? "严格冻结" : "缝合转场"), el("span", { class: "fact" }, `${patch.start_frame} — ${patch.end_frame} 帧`), el("span", { class: "fact" }, patch.candidate_asset_id || patch.candidate_audio_asset_id ? `候选 ${patch.candidate_asset_id || patch.candidate_audio_asset_id}` : "待指定候选"), el("span", { class: "fact" }, `缓存 ${patch.cache_key.slice(0, 8)}`)),
      report ? el("div", { class: `report ${report.status === "ready_for_verified_splice" ? "ok" : "bad"}` }, checks.map((check) => `${check.ok ? "通过" : "待处理"} · ${check.name}：${check.detail}`).join("\n")) : null,
      el("div", { class: "patch-actions" },
        button("执行预检", "", () => mutate(`/patches/${patch.id}/render`, { method: "POST" }, "局部渲染预检已完成"), !["planned", "ready_to_render", "blocked"].includes(patch.status)),
        button("并入成片", "primary", () => mutate(`/patches/${patch.id}/promote`, { method: "POST" }, "候选版本已并入合成清单"), patch.status !== "rendered"),
        button("回滚候选", "danger", () => mutate(`/patches/${patch.id}/rollback`, { method: "POST" }, "候选任务已回滚，冻结版本未改变"), ["promoted", "rolled_back"].includes(patch.status)),
      ),
    );
    list.append(card);
  }
  return el("section", { class: "panel", style: "margin-top:14px" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "热插拔任务队列"), el("p", {}, "所有计划、预检、候选与回滚均保留在项目内。"))), el("div", { class: "panel-body" }, patches.length ? list : el("div", { class: "empty" }, el("strong", {}, "还没有局部任务"), "选择一个渲染片段后，写下希望如何调整。")));
}

function renderQuality() {
  const frozen = state.segments.filter((segment) => segment.state === "frozen");
  const overlapRisk = state.patches.filter((patch) => patch.status !== "rolled_back" && patch.status !== "promoted").length > 1;
  const rows = [
    ["素材可追溯", state.assets.every((asset) => asset.id && asset.source_type && asset.provenance && asset.provenance.license), "每个使用中的素材都应有 S-xxx 与来源许可。"],
    ["审核锚点", state.scenes.every((scene) => scene.anchors && scene.anchors.some((anchor) => anchor.kind === "first_frame") && scene.anchors.some((anchor) => anchor.kind === "climax_frame")), "每场景至少首帧、高潮帧可审。"],
    ["冻结合同", frozen.length > 0 || state.segments.length === 0, "冻结后记录帧级与音频样本级边界。"],
    ["任务隔离", !overlapRisk, "未收口任务不能覆盖相同渲染范围。"],
  ];
  return el("section", { class: "page" },
    pageHeader("交付护栏", "把无法接受的隐性风险变成可见检查", "这里是审核台的底层约束；它们优先于“先跑出一个结果”。"),
    el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "实时工作台检查"), el("p", {}, "检查结果来自当前持久化状态。"))), el("div", { class: "panel-body policy-list" }, rows.map(([name, ok, detail]) => el("div", { class: "policy", style: `border-color:${ok ? "var(--lime)" : "var(--rose)"}` }, el("strong", {}, `${ok ? "通过" : "需处理"} · ${name}`), el("span", {}, detail))))),
    el("div", { class: "grid-2" },
      el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "严格冻结的硬条件"))), el("div", { class: "panel-body policy-list" }, policy("固定边界", "不能因 B 的时长变化推动 C；必须在 B 内裁切、延展或显式解冻。"), policy("不伪造渲染", "未通过编码、关键帧与时间基预检时，系统只给出阻塞报告，不会标为完成。"), policy("原子提升", "候选版本通过缝合审核后才可提升；历史版本保留，可回查。"))),
      el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "当前冻结状态"))), el("div", { class: "panel-body" }, frozen.length ? frozen.map((segment) => el("div", { class: "activity-item" }, el("time", {}, segment.id), el("span", {}, `版本 ${segment.current_version_id} · ${segment.start_frame}—${segment.end_frame} 帧`))) : el("div", { class: "empty" }, "尚未冻结片段。"))),
    ),
  );
}

function avatarIssuePanel(packageState) {
  const issues = [
    ...(packageState.validation.issues || []),
    ...(packageState.asr.issues || []),
    ...(packageState.assembly.issues || []),
  ];
  if (!issues.length) return null;
  const list = el("div", { class: "avatar-issues" });
  for (const issue of issues) {
    list.append(el("div", { class: `report ${issue.severity === "error" ? "bad" : ""}` },
      el("strong", {}, issue.turn_id ? `${issue.turn_id} · ${issue.code}` : issue.code),
      el("span", {}, issue.message),
    ));
  }
  return el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "检查结果"), el("p", {}, "错误必须处理；提醒不会阻塞后续步骤。"))),
    el("div", { class: "panel-body" }, list),
  );
}

function avatarOutputs(packageState) {
  const assembly = packageState.assembly || {};
  if (!assembly.output_path) return null;
  const links = el("div", { class: "inline-actions" });
  for (const [path, label, primary] of [
    [assembly.output_path, "播放数字人母版", true],
    [assembly.timeline_path, "查看实际时间线", false],
    [assembly.subtitle_path, "查看字幕", false],
    [assembly.qa_path, "查看 QA 报告", false],
  ]) {
    if (path) links.append(el("a", { class: `button ${primary ? "primary" : "quiet"}`, href: mediaURL(projectId, path), target: "_blank", rel: "noreferrer" }, label));
  }
  return el("section", { class: "panel avatar-output" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "交付产物"), el("p", {}, "全部时间都来自数字人视频原声，不采用脚本估算。"))),
    el("div", { class: "panel-body" }, links),
  );
}

function avatarTurnCard(turn, speakerNames) {
  const input = el("input", { type: "file", accept: ".mp4,.mov,.mkv,.webm,.m4v", "aria-label": `上传 ${turn.turn_id}` });
  input.addEventListener("change", () => { if (input.files[0]) uploadAvatarTurn(turn.turn_id, input.files[0]); });
  const media = turn.source && turn.source.media && !turn.source.media.error ? turn.source.media : null;
  const facts = el("div", { class: "avatar-turn-facts" },
    el("span", { class: "asset-code" }, turn.turn_id),
    el("strong", {}, speakerNames.get(turn.speaker_id) || turn.speaker_id),
    status(turn.status),
    media ? el("span", { class: "minor" }, `${fmtDuration(media.duration_seconds)} · ${media.video.width}×${media.video.height} · ${media.audio.sample_rate || "?"}Hz`) : null,
  );
  return el("article", { class: "avatar-turn" },
    facts,
    el("p", { class: "avatar-copy" }, turn.text),
    el("div", { class: "avatar-turn-upload" },
      el("span", { class: "minor" }, turn.source ? turn.source.original_filename : `建议文件名：${turn.expected_filename}`),
      el("label", { class: "button small" }, turn.source ? "替换视频" : "选择视频", input),
    ),
    turn.transcript ? el("p", { class: "avatar-transcript" }, `识别：${turn.transcript}（覆盖率 ${Math.round((turn.asr_coverage || 0) * 100)}%）`) : null,
  );
}

function renderAvatarPerTurn(packageState) {
  const speakerNames = new Map(packageState.speakers.map((speaker) => [speaker.speaker_id, speaker.name]));
  const drop = el("div", {
    class: "avatar-dropzone",
    ondragover: (event) => { event.preventDefault(); event.currentTarget.classList.add("active"); },
    ondragleave: (event) => event.currentTarget.classList.remove("active"),
    ondrop: (event) => {
      event.preventDefault();
      event.currentTarget.classList.remove("active");
      uploadAvatarBatch(event.dataTransfer.files);
    },
  }, el("strong", {}, "把全部数字人视频拖到这里"), el("span", {}, "系统从文件名识别 T001、T002……并自动归位；也可以逐条上传。"));
  const batchInput = el("input", { type: "file", multiple: "", accept: ".mp4,.mov,.mkv,.webm,.m4v" });
  batchInput.addEventListener("change", () => uploadAvatarBatch(batchInput.files));
  drop.append(el("label", { class: "button quiet" }, "批量选择文件", batchInput));
  const turnList = el("div", { class: "avatar-turn-list" });
  for (const turn of packageState.turns) turnList.append(avatarTurnCard(turn, speakerNames));
  return el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "按轮次导入（推荐）"), el("p", {}, "一段台词对应一个视频，错一条只重做一条，剪辑边界最可靠。"))),
    el("div", { class: "panel-body" }, drop, turnList),
  );
}

function renderAvatarLongform(packageState) {
  if (!localWhisperModels && !localWhisperModelsLoading) loadLocalWhisperModels();
  const list = el("div", { class: "avatar-speaker-list" });
  const frameFit = el("select", { "aria-label": "本地口播画幅适配方式" });
  for (const [value, label] of [["blur_background", "推荐：模糊背景完整显示人物"], ["contain_black", "保留完整画面并使用黑边"], ["cover_crop", "裁切铺满项目画幅"]]) {
    frameFit.append(el("option", { value }, label));
  }
  frameFit.value = ((packageState.presentation || {}).frame_fit_mode) || "blur_background";
  frameFit.addEventListener("change", () => saveLongformPresentation(frameFit.value));
  for (const speaker of packageState.speakers) {
    const input = el("input", { type: "file", accept: ".mp4,.mov,.mkv,.webm,.m4v" });
    input.addEventListener("change", () => { if (input.files[0]) uploadAvatarSpeaker(speaker.speaker_id, input.files[0]); });
    list.append(el("article", { class: "avatar-speaker" },
      el("div", {}, el("strong", {}, speaker.name), el("p", { class: "minor" }, speaker.source ? `${speaker.source.original_filename} · ${((speaker.source.media || {}).duration_seconds || 0).toFixed ? ((speaker.source.media || {}).duration_seconds || 0).toFixed(2) : ""} 秒` : `上传 ${speaker.name} 的完整连续视频`)),
      status(speaker.source ? "uploaded" : "missing"),
      el("label", { class: "button small" }, speaker.source ? "替换长视频" : "选择长视频", input),
    ));
  }
  return el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "本地整段口播 · 按台词自动切割"), el("p", {}, "每位角色只上传一条完整口播视频；系统先给出切点建议，你审核通过后才会进入最终合成。原片始终保留，不会被改写。"))),
    el("div", { class: "panel-body" },
      el("label", { class: "control-label" }, "成片画幅适配", frameFit),
      el("p", { class: "form-note" }, "你的口播原片不必与项目画幅完全一致。推荐“模糊背景完整显示人物”，避免拉伸脸部；最终只在母版合成阶段转码为项目的 9:16 或 16:9。"),
      list,
    ),
  );
}

function percent(value) { return `${Math.round((Number(value) || 0) * 100)}%`; }

function renderLongformSpeakerDiagnostics(packageState) {
  const planItems = (packageState.cut_plan || {}).items || [];
  const diagnostics = ((packageState.asr || {}).speaker_diagnostics) || {};
  const cards = el("div", { class: "asr-diagnostic-list" });
  for (const speaker of packageState.speakers) {
    const record = diagnostics[speaker.speaker_id] || {};
    const candidates = record.candidates || [];
    const activeId = record.active_candidate_id;
    const latestId = record.latest_candidate_id;
    const active = candidates.find((item) => item.candidate_id === activeId) || null;
    const latest = candidates.find((item) => item.candidate_id === latestId) || null;
    const fallbackItems = planItems.filter((item) => item.speaker_id === speaker.speaker_id);
    const baseline = active || {
      overall_metrics: {
        similarity: fallbackItems.reduce((sum, item) => sum + Number(item.asr_similarity || 0), 0) / Math.max(1, fallbackItems.length),
        coverage: fallbackItems.reduce((sum, item) => sum + Number(item.asr_coverage || 0), 0) / Math.max(1, fallbackItems.length),
      },
      summary: { total: fallbackItems.length, pending_review: fallbackItems.filter((item) => item.status === "pending_review").length, needs_manual: fallbackItems.filter((item) => item.status === "needs_manual").length },
      turns: fallbackItems.map((item) => ({ turn_id: item.turn_id, transcript: item.transcript || "", asr_similarity: item.asr_similarity, asr_coverage: item.asr_coverage, status: item.status, reason: item.review_note || "初始核对结果" })),
    };
    const isRunning = ((record.job || {}).status) === "running";
    const modelSelect = el("select", { "aria-label": `${speaker.name} 诊断模型` });
    const models = localWhisperModels || [];
    if (!models.length) modelSelect.append(el("option", { value: "" }, localWhisperModelsLoading ? "正在读取本机模型…" : "使用当前本机默认模型"));
    for (const model of models) modelSelect.append(el("option", { value: model.id }, model.label));
    const latestIsNew = latest && latest.candidate_id !== activeId && ["enhanced_diagnosis", "normalized_realign"].includes(latest.kind);
    const canNormalizeRealign = latest && latest.full_transcript && latest.kind === "enhanced_diagnosis";
    const turns = (latestIsNew ? latest.turns : baseline.turns) || [];
    const transcript = latestIsNew ? latest.full_transcript : baseline.full_transcript;
    const metrics = latestIsNew ? latest.overall_metrics : baseline.overall_metrics;
    cards.append(el("article", { class: `asr-diagnostic-card ${isRunning ? "running" : ""}` },
      el("div", { class: "asr-diagnostic-head" },
        el("div", {}, el("strong", {}, `${speaker.name} · 台词诊断`), el("p", { class: "minor" }, speaker.source ? `原片：${speaker.source.original_filename}` : "尚未上传原片")),
        status(isRunning ? "running" : (record.status || "not_started")),
      ),
      el("div", { class: "asr-diagnostic-metrics" },
        el("span", {}, `整段相似度 ${percent(metrics && metrics.similarity)}`),
        el("span", {}, `整段覆盖率 ${percent(metrics && metrics.coverage)}`),
        el("span", {}, `可审核 ${((latestIsNew ? latest.summary : baseline.summary) || {}).pending_review || 0} 句`),
        el("span", {}, `需人工定位 ${((latestIsNew ? latest.summary : baseline.summary) || {}).needs_manual || 0} 句`),
      ),
      el("p", { class: "form-note" }, "重新分析只生成对比候选，不会重跑其他角色，也不会覆盖当前切点。确认采用后，才替换此角色的待审核切点。"),
      el("div", { class: "asr-diagnostic-actions" },
        modelSelect,
        button(isRunning ? `正在分析 ${speaker.name}…` : `仅重新分析 ${speaker.name}`, "small", () => startLongformSpeakerDiagnosis(speaker.speaker_id, modelSelect.value), isRunning || !speaker.source),
        canNormalizeRealign ? button("复用本次文本重新对齐", "quiet small", () => realignLongformSpeakerCandidate(speaker.speaker_id, latest.candidate_id), isRunning) : null,
        latestIsNew ? button("采用这份候选切点", "primary small", () => applyLongformSpeakerCandidate(speaker.speaker_id, latest.candidate_id)) : null,
      ),
      transcript ? el("details", { class: "asr-transcript-details" },
        el("summary", {}, "查看本次整段 ASR 文本与逐句差异"),
        el("p", { class: "asr-full-transcript" }, transcript),
        el("div", { class: "asr-turn-diagnostics" }, ...turns.map((turn) => el("div", { class: `asr-turn-diagnostic ${turn.status || ""}` },
          el("strong", {}, turn.turn_id),
          el("span", {}, `覆盖 ${percent(turn.asr_coverage)} · 相似 ${percent(turn.asr_similarity)}`),
          turn.transcript ? el("p", {}, `识别：${turn.transcript}`) : null,
          turn.reason ? el("p", { class: "minor" }, turn.reason) : null,
        ))),
      ) : el("p", { class: "form-note" }, "初次核对未保留整段逐词文本。点击“仅重新分析”可生成可比较的完整诊断，不会改变现有切点。"),
      latestIsNew ? el("p", { class: "report warn" }, `候选 ${latest.candidate_id} 尚未采用：请先比较覆盖率与逐句文本，再决定是否替换。`) : null,
      (record.job || {}).error ? el("p", { class: "report error" }, `诊断失败：${record.job.error}`) : null,
    ));
  }
  return el("section", { class: "panel asr-diagnostics" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "3. 说话人级 ASR 诊断与无损返工"), el("p", {}, "当某位角色识别异常时，只重分析这一位。檬檬已稳定的切点、通过状态和原片不会被重新计算或覆盖。"))),
    el("div", { class: "panel-body" }, cards),
  );
}

function renderLongformCutReview(packageState) {
  const plan = packageState.cut_plan || {};
  const items = plan.items || [];
  if (!items.length) return null;
  const turns = new Map(packageState.turns.map((turn) => [turn.turn_id, turn]));
  const speakers = new Map(packageState.speakers.map((speaker) => [speaker.speaker_id, speaker]));
  const summary = plan.summary || {};
  const list = el("div", { class: "avatar-cut-review-list" });
  for (const item of items) {
    const turn = turns.get(item.turn_id) || {};
    const speaker = speakers.get(item.speaker_id) || {};
    const sourcePath = ((speaker.source || {}).path);
    const start = el("input", { type: "number", step: "0.01", min: "0", value: item.start_seconds ?? "", "aria-label": `${item.turn_id} 起始秒数` });
    const end = el("input", { type: "number", step: "0.01", min: "0", value: item.end_seconds ?? "", "aria-label": `${item.turn_id} 结束秒数` });
    const note = el("input", { value: item.review_note || "", placeholder: "审核备注（可选）", "aria-label": `${item.turn_id} 审核备注` });
    const preview = sourcePath ? el("video", { controls: "", preload: "metadata", src: mediaURL(projectId, sourcePath), class: "avatar-cut-preview" }) : null;
    if (preview && Number.isFinite(Number(item.start_seconds))) {
      preview.addEventListener("loadedmetadata", () => { preview.currentTime = Math.max(0, Number(item.start_seconds)); });
      preview.addEventListener("timeupdate", () => { if (Number.isFinite(Number(end.value)) && preview.currentTime >= Number(end.value)) preview.pause(); });
    }
    const confidenceLabel = { high: "高置信", medium: "中置信", low: "低置信", manual: "人工调整" }[item.confidence] || item.confidence;
    const statusLabel = { approved: "已通过", pending_review: "待审核", needs_manual: "需手动定位", invalid: "无效" }[item.status] || item.status;
    list.append(el("article", { class: `avatar-cut-card ${item.status}` },
      el("div", { class: "avatar-cut-card-head" },
        el("strong", {}, `${item.turn_id} · ${speaker.name || item.speaker_id}`),
        status(item.status), el("span", { class: `cut-confidence ${item.confidence}` }, confidenceLabel),
      ),
      el("p", { class: "avatar-cut-script" }, turn.text || "未找到脚本文本"),
      item.transcript ? el("p", { class: "minor" }, `识别：${item.transcript}`) : null,
      el("p", { class: "minor" }, `覆盖率 ${Math.round((item.asr_coverage || 0) * 100)}% · 相似度 ${Math.round((item.asr_similarity || 0) * 100)}%`),
      preview,
      el("div", { class: "avatar-cut-controls" },
        el("label", {}, "起点（秒）", start),
        el("label", {}, "终点（秒）", end),
        button("前移 0.12 秒", "quiet small", () => { start.value = Math.max(0, Number(start.value || 0) - 0.12).toFixed(2); }),
        button("后移 0.12 秒", "quiet small", () => { end.value = (Number(end.value || 0) + 0.12).toFixed(2); }),
      ),
      note,
      el("div", { class: "inline-actions" },
        button("保存切点", "small", () => saveLongformCut(item.turn_id, start.value, end.value, note.value)),
        button(item.status === "approved" ? "已通过" : "确认通过", item.status === "approved" ? "quiet small" : "primary small", () => approveLongformCut(item.turn_id), item.status === "approved"),
      ),
      item.review_note && item.status === "needs_manual" ? el("p", { class: "report warn" }, item.review_note) : null,
    ));
  }
  return el("section", { class: "panel avatar-cut-review" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "3. 切割审核：每句台词对应原片片段"), el("p", {}, "系统建议切点不是最终结果。逐段试听、微调起止秒数并确认；只有全部通过后才允许合成。"))),
    el("div", { class: "panel-body" },
      el("div", { class: "cut-review-summary" }, `已通过 ${summary.approved || 0}/${summary.total || items.length} · 待审核 ${summary.pending_review || 0} · 需手动定位 ${summary.needs_manual || 0}`),
      button("批量通过全部高置信片段", "quiet", () => approveHighConfidenceLongformCuts(), !items.some((item) => item.status === "pending_review" && item.confidence === "high")),
      list,
    ),
  );
}

function renderAvatarPackageActions(packageState) {
  const validation = packageState.validation || {};
  const asr = packageState.asr || {};
  const assembly = packageState.assembly || {};
  const mediaPassed = validation.status === "passed" || validation.status === "passed_with_warnings";
  const cutsApproved = packageState.import_mode !== "longform" || (packageState.cut_plan || {}).status === "approved";
  const asrPassed = (asr.status === "passed" || packageState.settings.require_asr === false) && cutsApproved;
  const running = asr.status === "running" || assembly.status === "running";
  const assembled = assembly.status === "passed";
  const treatment = el("select", { "aria-label": "数字人默认出镜方式" });
  for (const [value, label] of [["fullscreen", "全屏数字人主体"], ["pip_top_left", "左上角数字人解说"], ["hidden", "暂时只使用主体画面"]]) {
    treatment.append(el("option", { value }, label));
  }
  treatment.value = (packageState.presentation && packageState.presentation.default_treatment) || (state.avatar && state.avatar.default_treatment) || "fullscreen";
  const actions = el("div", { class: "inline-actions" },
    button("1. 检查全部原片", "", () => mutate("/avatar-package/validate", { method: "POST" }, "媒体与轮次检查已完成"), running),
    button(asr.status === "running" ? "正在核对台词…" : "2. ASR 核对台词", "", () => mutate("/avatar-package/asr/jobs", { method: "POST", body: {} }, "台词核验已开始"), running || !mediaPassed || packageState.settings.require_asr === false),
    button(assembly.status === "running" ? "正在合成母版…" : packageState.import_mode === "longform" && !cutsApproved ? "请先完成切割审核" : "4. 合成原声母版", "primary", () => mutate("/avatar-package/assembly/jobs", { method: "POST", body: {} }, "数字人母版合成已开始"), running || !mediaPassed || !asrPassed),
  );
  const handoffReady = mediaPassed && asrPassed && !assembled && !running;
  const assemblySummary = assembly.summary || {};
  const assemblyProgress = avatarAssemblyProgressText(assembly);
  const assemblyFailure = avatarAssemblyIssueText(assembly);
  const handoffPanel = handoffReady ? el("div", { class: "avatar-apply-timeline avatar-handoff" },
    el("strong", {}, "一键进入片段工作台"),
    el("p", { class: "form-note" }, assembly.status === "failed" && assemblySummary.resumable
      ? "上次合成已中断，但已完成切片会被安全复用。点击后将从断点继续，不会重新上传原片、改写脚本或改变已审核切点。"
      : "系统会依次合成原声母版、以真实音频时长建立场景边界，并按下方默认版式自动标记需要补齐主体画面的片段。不会拉伸音频或嘴型。"),
    el("label", { class: "control-label" }, "进入工作台时的默认出镜方式", treatment),
    button(assembly.status === "failed" && assemblySummary.resumable ? "从已完成片段继续合成并进入工作台" : "合成母版并进入片段工作台", "primary", () => startAvatarHandoff(treatment.value)),
  ) : null;
  const applyPanel = assembled ? el("div", { class: "avatar-apply-timeline" },
    el("label", { class: "control-label" }, "首次应用时的默认出镜方式", treatment),
    el("p", { class: "form-note" }, avatarTimelineApplied()
      ? "真实时间线已应用。再次应用会以最新原声母版刷新片段时长和出镜绑定，请在未开始场景审核时使用。"
      : "此操作会以数字人原声的真实时长创建场景时间线、字幕和不可变母版版本；不会生成 TTS。"),
    button(avatarTimelineApplied() ? "重新应用真实时间线" : "4. 应用为真实时间线", "primary", () => mutate("/avatar-package/apply", { method: "POST", body: { default_treatment: treatment.value } }, "数字人原声已应用为真实时间线，请进入片段工作台审核画面")),
  ) : null;
  return el("section", { class: "panel avatar-actions" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "生产闸门"), el("p", {}, "先验媒体，再核台词，最后合成；任何一步失败都不会悄悄跳过。"))),
    el("div", { class: "panel-body" },
      actions,
      assembly.status === "running" || (assembly.status === "failed" && assemblySummary.resumable)
        ? el("div", { class: assembly.status === "failed" ? "report bad" : "report" }, assembly.status === "failed" ? assemblyFailure : assemblyProgress)
        : null,
      handoffPanel,
      applyPanel,
      el("p", { class: "form-note" }, "合成输出固定为 H.264/AAC、25fps、48kHz；成片超过两分钟会在 QA 阶段失败。"),
    ),
  );
}

function cloudRoleImage(role) {
  const reference = (role.references || [])[0];
  if (!reference) return null;
  return el("img", {
    class: "avatar-role-thumb", alt: `${role.name} 角色参考图`,
    src: `/api/avatar-roles/${encodeURIComponent(role.role_id)}/media/${reference.path.split("/").map(encodeURIComponent).join("/")}`,
  });
}

function renderCloudRoleLibraryLegacy(packageState) {
  if (!avatarRoles && !avatarRolesLoading) loadAvatarRoles();
  const roles = (avatarRoles && avatarRoles.roles) || [];
  const selectedRoleId = (packageState.role || {}).role_id;
  const creationName = el("input", { placeholder: "例如：雅雅（录音间）", "aria-label": "角色名称" });
  const creationDescription = el("input", { placeholder: "可选：角色外观、服饰与使用说明", "aria-label": "角色说明" });
  const creationLicense = el("input", { placeholder: "例如：本人已获授权", "aria-label": "角色授权说明" });
  const createPanel = el("div", { class: "avatar-role-create" },
    el("strong", {}, "没有角色？先建立一次通用角色档案"),
    creationName, creationDescription, creationLicense,
    button("新建角色档案", "small", () => createAvatarRole(creationName.value.trim(), creationDescription.value.trim(), creationLicense.value.trim())),
  );
  const roleList = el("div", { class: "avatar-role-list" });
  for (const role of roles) {
    const selected = role.role_id === selectedRoleId;
    const item = el("article", { class: `avatar-role ${selected ? "selected" : ""}` },
      cloudRoleImage(role),
      el("div", { class: "avatar-role-copy" },
        el("strong", {}, role.name),
        el("span", { class: "minor" }, `版本 ${role.version} · ${role.references.length} 张参考图`),
        role.description ? el("span", { class: "minor" }, role.description) : null,
      ),
      button(selected ? "当前角色" : "使用此角色", selected ? "quiet small" : "small", () => selectAvatarCloudRole(role.role_id), selected),
    );
    if (selected) {
      const uploads = el("div", { class: "avatar-role-reference-inputs" });
      for (const [slot, label] of [["front", "正面"], ["left", "左侧"], ["right", "右侧"], ["reference", "其他参考"]]) {
        const file = el("input", { type: "file", accept: ".png,.jpg,.jpeg,.webp", "aria-label": `上传${label}参考图` });
        file.addEventListener("change", () => { if (file.files[0]) uploadAvatarRoleReference(role.role_id, slot, file.files[0]); });
        uploads.append(el("label", { class: "button quiet small" }, `上传${label}图`, file));
      }
      item.append(el("div", { class: "avatar-role-reference-note" }, "透明三视图可分别上传到正面、左侧、右侧；它们用于身份记录，不会被当作云端实际出镜图。"), uploads);
    }
    roleList.append(item);
  }
  return el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "1. 通用角色库（身份参考）"), el("p", {}, "角色三视图只保存一次，可跨项目复用；实际生成仍由项目出镜图决定。"))),
    el("div", { class: "panel-body" },
      roles.length ? roleList : el("div", { class: "report" }, avatarRolesLoading ? "正在读取角色库…" : "还没有角色档案。建议先建立“雅雅”，再上传透明三视图。"),
      createPanel,
    ),
  );
}

function renderCloudPresenterShotLegacy(packageState) {
  const input = el("input", { type: "file", accept: ".png,.jpg,.jpeg,.webp", "aria-label": "上传项目出镜图" });
  input.addEventListener("change", () => { if (input.files[0]) uploadAvatarCloudPresenter(input.files[0]); });
  const shot = packageState.presenter_shot;
  return el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "2. 本项目实际出镜图"), el("p", {}, "请选择一张录音间里的单人角色图。只有这张图会提交给阿里云，切勿上传三视图拼图。"))),
    el("div", { class: "panel-body avatar-presenter-shot" },
      shot ? el("img", { class: "avatar-presenter-preview", alt: "项目出镜图预览", src: mediaURL(projectId, shot.path) }) : el("div", { class: "avatar-shot-empty" }, "尚未上传项目出镜图"),
      el("div", {},
        el("strong", {}, shot ? shot.original_filename : "请上传一张单人场景图"),
        el("p", { class: "minor" }, shot ? `${shot.media.width}×${shot.media.height} · 已作为云端生成输入` : "建议正面或半身、人物清晰、背景稳定，最小 400×400 像素。"),
        el("label", { class: "button small" }, shot ? "替换项目出镜图" : "选择项目出镜图", input),
      ),
    ),
  );
}

function cloudJobLabel(job) {
  if (!job) return "尚未生成";
  return `${statusLabels[job.status] || job.status} · ${job.stage || ""}`;
}

function normaliseVoiceboxName(value) {
  return String(value || "").normalize("NFKC").trim().toLocaleLowerCase();
}

function voiceboxMappingForSpeaker(packageState, speakerId) {
  const stored = ((packageState.voicebox || {}).speaker_mappings || []).find((item) => item && item.speaker_id === speakerId);
  if (stored) return stored;
  const speaker = (packageState.speakers || []).find((item) => item.speaker_id === speakerId) || {};
  const profiles = Array.isArray(voiceCatalog.profiles) ? voiceCatalog.profiles : [];
  const matches = profiles.filter((item) => normaliseVoiceboxName(item.name) === normaliseVoiceboxName(speaker.name));
  if (matches.length === 1) return {
    speaker_id: speakerId, speaker_name: speaker.name || speakerId, profile_id: matches[0].id, profile_name: matches[0].name,
    selection_source: "same_name", status: "ready", detail: "同名精确匹配（将在开始配音时保存）",
  };
  if (matches.length > 1) return {
    speaker_id: speakerId, speaker_name: speaker.name || speakerId, profile_id: null, profile_name: null,
    selection_source: "ambiguous", status: "needs_attention", detail: `发现 ${matches.length} 个同名音色，请手动指定`,
  };
  const fallback = voiceCatalog.default_voice;
  return fallback ? {
    speaker_id: speakerId, speaker_name: speaker.name || speakerId, profile_id: fallback.id, profile_name: fallback.name,
    selection_source: "default", status: "ready", detail: "未找到同名音色，使用通用默认音色兜底",
  } : {
    speaker_id: speakerId, speaker_name: speaker.name || speakerId, profile_id: null, profile_name: null,
    selection_source: "unavailable", status: "unavailable", detail: "通用配音中心没有可用音色",
  };
}

function voiceboxSelectionLabel(mapping) {
  const labels = { manual: "手动指定", same_name: "同名精确匹配", default: "通用默认兜底", ambiguous: "同名重复", unavailable: "不可用" };
  return labels[mapping.selection_source] || "待识别";
}

function renderCloudTurnCard(turn, speakerNames, packageState) {
  const providerLabel = packageState.generation_mode === "runninghub_longcat" ? "RunningHub" : "阿里云";
  const file = el("input", { type: "file", accept: ".wav,.mp3,.m4a,.aac,.flac,.ogg", "aria-label": `上传 ${turn.turn_id} 驱动音频` });
  file.addEventListener("change", () => { if (file.files[0]) uploadAvatarDrivingAudio(turn.turn_id, file.files[0]); });
  const audio = turn.driving_audio;
  const voiceJob = turn.driving_audio_job || {};
  const mapping = voiceboxMappingForSpeaker(packageState, turn.speaker_id);
  const candidateTakes = (turn.driving_audio_candidates || []).filter((item) => item && item.state === "candidate" && item.source_type === "voicebox_generated");
  const profiles = Array.isArray(voiceCatalog.profiles) ? voiceCatalog.profiles : [];
  const voiceReady = voiceCatalog.provider && voiceCatalog.provider.status === "available";
  const profile = el("select", { "aria-label": `${turn.turn_id} 的本地音色` });
  profile.append(el("option", { value: "" }, mapping.profile_name ? `自动：${mapping.profile_name}（${voiceboxSelectionLabel(mapping)}）` : "请先指定该角色的音色"));
  for (const item of profiles) {
    const selected = (voiceJob.profile_id === item.id) || (audio && audio.profile_id === item.id) || (!voiceJob.profile_id && !audio?.profile_id && mapping.profile_id === item.id);
    profile.append(el("option", { value: item.id, selected: selected ? "" : null }, item.name));
  }
  const automaticProfileId = String(mapping.profile_id || "");
  const selectedProfileId = String(voiceJob.profile_id || (audio && audio.profile_id) || automaticProfileId || "");
  profile.value = selectedProfileId;
  const job = turn.cloud_job;
  const jobIsRunning = job && ["queued", "uploading", "detecting", "submitted", "running", "downloading"].includes(job.status);
  const actions = el("div", { class: "inline-actions" },
    el("label", { class: "button quiet small" }, audio ? "替换驱动音频" : "上传驱动音频", file),
    button(
      voiceJob.status === "generating" ? "正在生成本地候选…" : "用本地配音生成",
      "small",
      () => generateAvatarVoiceboxDrivingAudio(turn.turn_id, profile.value === automaticProfileId ? "" : profile.value),
      voiceJob.status === "generating" || !voiceReady || jobIsRunning || mapping.status !== "ready",
    ),
  );
  if (jobIsRunning && job.provider_task_id) {
    actions.append(button("继续跟踪此任务", "quiet small", () => mutate(`/avatar-package/turns/${encodeURIComponent(turn.turn_id)}/cloud/resume/jobs`, { method: "POST", body: {} }, `${turn.turn_id} 正在恢复 ${providerLabel} 任务跟踪`)));
  }
  if (job && (job.status === "failed" || job.status === "succeeded")) {
    actions.append(button("重新生成此段", "small", () => {
      if (confirm(`将重新调用 ${providerLabel} 生成 ${turn.turn_id}，可能产生费用。是否确认消耗额度并继续？`)) mutate(`/avatar-package/turns/${encodeURIComponent(turn.turn_id)}/cloud/retry/jobs`, { method: "POST", body: { confirm_paid: true } }, `${turn.turn_id} 已重新进入生成队列`);
    }, !audio));
  }
  const audioOrigin = audio && audio.source_type === "voicebox_generated"
    ? `Haike Video 本地配音 · ${audio.profile_name || "未标注音色"}`
    : "已上传音频";
  const candidateBlocks = candidateTakes.map((candidate) => el("div", { class: "cloud-audio-preview narration-take" },
    el("div", { class: "take-head" }, el("strong", {}, `候选音频 · ${candidate.profile_name || "本地音色"}`), status("candidate")),
    el("span", { class: "minor" }, `${fmtDuration(candidate.duration_seconds)} · 先试听，采用后才会替换当前驱动音频`),
    el("audio", { controls: "", preload: "metadata", src: mediaURL(projectId, candidate.path) }),
    button("采用为驱动音频", "primary small", () => applyAvatarVoiceboxDrivingAudio(turn.turn_id, candidate.id), jobIsRunning),
  ));
  return el("article", { class: "avatar-turn cloud-turn" },
    el("div", { class: "avatar-turn-facts" }, el("span", { class: "asset-code" }, turn.turn_id), el("strong", {}, speakerNames.get(turn.speaker_id) || turn.speaker_id), status(turn.status)),
    el("p", { class: "avatar-copy" }, turn.text),
    audio ? el("div", { class: "cloud-audio-preview" },
      el("span", { class: "minor" }, `${audioOrigin} · ${audio.original_filename} · ${fmtDuration(audio.duration_seconds)} · 该时长将成为片段时长`),
      el("audio", { controls: "", src: mediaURL(projectId, audio.path) }),
    ) : el("p", { class: "minor" }, "尚未采用驱动音频。可上传已有音频，或用 Haike Video 本地配音按本轮台词生成候选；单段最多 20 秒。"),
    el("div", { class: "voicebox-turn-control" },
      el("label", { class: "control-label" }, "本轮本地音色（生成内容固定为上方台词）", profile),
      el("span", { class: `minor ${mapping.status !== "ready" ? "voicebox-mapping-warning" : ""}` }, `${voiceboxSelectionLabel(mapping)}：${mapping.detail || "将在开始生成时确认"}`),
      !voiceReady ? el("div", { class: "report bad" }, "Haike Video 本地配音当前不可用。你仍可上传现成驱动音频；如需生成，请先安装服务并到通用配音中心检查音色。") : null,
      voiceJob.status === "generating" ? el("div", { class: "narration-job is-running" }, status("generating"), el("span", {}, `正在生成 ${voiceJob.profile_name || "本地音色"} 候选音频；不会覆盖当前已采用音频。`)) : null,
      voiceJob.status === "failed" ? el("div", { class: "report bad" }, voiceJob.error || "本地候选音频生成失败，请检查服务后重试。") : null,
    ),
    candidateBlocks.length ? el("div", { class: "voicebox-candidate-list" }, candidateBlocks) : null,
    job ? el("div", { class: `cloud-job ${jobIsRunning ? "running" : ""}` },
      el("strong", {}, cloudJobLabel(job)), job.error ? el("span", {}, job.error) : null,
      job.result_path ? el("a", { href: mediaURL(projectId, job.result_path), target: "_blank", rel: "noreferrer" }, "播放生成片段") : null,
    ) : null,
    actions,
  );
}

function renderCloudActionsLegacy(packageState) {
  const cloud = packageState.cloud || {};
  const sampleTurn = (packageState.turns || []).find((turn) => turn.turn_id === cloud.sample_turn_id) || packageState.turns[0];
  const sampleReady = sampleTurn && sampleTurn.cloud_job && sampleTurn.cloud_job.status === "succeeded";
  const allDone = packageState.turns.length > 0 && packageState.turns.every((turn) => (turn.cloud_job || {}).status === "succeeded");
  const hasRunning = packageState.turns.some((turn) => ["queued", "uploading", "detecting", "submitted", "running", "downloading"].includes((turn.cloud_job || {}).status));
  const canStart = cloud.status === "ready" && sampleTurn && Boolean(sampleTurn.driving_audio);
  const actions = el("div", { class: "inline-actions" },
    button("生成首段试片", "primary", () => {
      if (confirm("将调用阿里云生成首段数字人试片，可能产生费用。确认开始吗？")) mutate("/avatar-package/cloud/sample/jobs", { method: "POST", body: { turn_id: sampleTurn.turn_id } }, "试片任务已提交，生成进度会自动刷新");
    }, !canStart || hasRunning),
    button("确认试片，生成其余片段", "primary", () => mutate("/avatar-package/cloud/sample/approve", { method: "POST", body: {} }, "试片已确认，现在可以生成其余片段"), !sampleReady || Boolean(cloud.sample_approved)),
    button("开始生成其余片段", "primary", () => {
      if (confirm("将依次调用阿里云生成剩余数字人片段，可能产生费用。确认开始吗？")) mutate("/avatar-package/cloud/batch/jobs", { method: "POST", body: {} }, "其余片段已排队，将按顺序生成");
    }, !cloud.sample_approved || hasRunning || allDone),
  );
  return el("section", { class: "panel avatar-actions" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "4. 先试片，再批量生成"), el("p", {}, "阿里云任务为异步任务。进度、任务编号与本地下载结果都会被保存；刷新页面不会中断跟踪。"))),
    el("div", { class: "panel-body" },
      el("div", { class: `report ${cloud.status === "failed" ? "bad" : cloud.status === "completed" ? "ok" : ""}` }, cloud.message || "请完成上方输入。"),
      actions,
      allDone ? el("div", { class: "report ok" }, "全部片段已生成。下一步：检查全部原片 → 合成原声母版 → 应用为真实时间线。") : null,
    ),
  );
}

function renderCloudAvatarLegacy(packageState) {
  const speakerNames = new Map(packageState.speakers.map((speaker) => [speaker.speaker_id, speaker.name]));
  const readyAudio = packageState.turns.filter((turn) => turn.driving_audio).length;
  const generated = packageState.turns.filter((turn) => (turn.cloud_job || {}).status === "succeeded").length;
  return el("section", { class: "page" },
    pageHeader("阿里云数字人口播", "角色身份、项目出镜、驱动音频，三者各司其职", "音频时长是唯一时间轴。云端生成的视频会立即落盘，然后进入原片检查、字幕审核和成片流程。"),
    el("div", { class: "metric-grid" },
      renderMetric("角色身份", packageState.role ? packageState.role.name : "未选择", "全局角色库，可跨项目复用"),
      renderMetric("驱动音频", `${readyAudio}/${packageState.turns.length}`, "每段不超过 20 秒"),
      renderMetric("生成片段", `${generated}/${packageState.turns.length}`, "已下载到本项目素材目录"),
      renderMetric("云端状态", statusLabels[(packageState.cloud || {}).status] || (packageState.cloud || {}).status || "未知", "先试片，后批量"),
    ),
    renderCloudRoleLibrary(packageState),
    renderCloudPresenterShot(packageState),
    el("section", { class: "panel" },
      el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "3. 逐段驱动音频"), el("p", {}, "每轮可上传已有音频，或直接从通用配音中心选择本地音色生成候选、试听后采用。画面会匹配真实音频时长，不会拉伸配音。"))),
      el("div", { class: "panel-body" }, el("div", { class: "avatar-turn-list" }, packageState.turns.map((turn) => renderCloudTurnCard(turn, speakerNames, packageState)))),
    ),
    renderCloudActions(packageState),
    generated === packageState.turns.length ? renderAvatarPackageActions(packageState) : null,
    avatarIssuePanel(packageState),
    avatarOutputs(packageState),
  );
}

function cloudBinding(packageState, speakerId) {
  return (packageState.speaker_bindings || []).find((item) => item.speaker_id === speakerId) || null;
}

function renderCloudRoleLibraryForBindings() {
  if (!avatarRoles && !avatarRolesLoading) loadAvatarRoles();
  const roles = (avatarRoles && avatarRoles.roles) || [];
  const name = el("input", { placeholder: "例如：雅雅（录音间）", "aria-label": "角色名称" });
  const description = el("input", { placeholder: "可选：外观、服饰与使用说明", "aria-label": "角色说明" });
  const license = el("input", { placeholder: "例如：本人已授权", "aria-label": "角色授权说明" });
  const list = el("div", { class: "avatar-role-list" });
  for (const role of roles) {
    const item = el("article", { class: "avatar-role" },
      cloudRoleImage(role),
      el("div", { class: "avatar-role-copy" },
        el("strong", {}, role.name),
        el("span", { class: "minor" }, `版本 ${role.version} · ${role.references.length} 张身份参考图`),
        role.description ? el("span", { class: "minor" }, role.description) : null,
      ),
    );
    const uploads = el("div", { class: "avatar-role-reference-inputs" });
    for (const [slot, label] of [["front", "正面"], ["left", "左侧"], ["right", "右侧"], ["reference", "其他参考"]]) {
      const file = el("input", { type: "file", accept: ".png,.jpg,.jpeg,.webp", "aria-label": `上传${label}参考图` });
      file.addEventListener("change", () => { if (file.files[0]) uploadAvatarRoleReference(role.role_id, slot, file.files[0]); });
      uploads.append(el("label", { class: "button quiet small" }, `上传${label}图`, file));
    }
    item.append(el("div", { class: "avatar-role-reference-note" }, "三视图用于身份留档和跨项目复用；阿里云实际只会收到下方为该角色上传的单人出镜图。"), uploads);
    list.append(item);
  }
  return el("details", { class: "panel avatar-role-library-optional" },
    el("summary", { class: "panel-head" }, el("div", {}, el("h4", {}, "可选：通用角色库（跨项目复用）"), el("p", {}, "不建立角色档案也可以生成。本区域只用于保存三视图、授权和身份说明，供以后项目复用。"))),
    el("div", { class: "panel-body" },
      roles.length ? list : el("div", { class: "report" }, avatarRolesLoading ? "正在读取角色库…" : "目前没有通用角色档案；这不会阻止本期生成。"),
      el("div", { class: "avatar-role-create" },
        el("strong", {}, "新建通用角色档案"), name, description, license,
        button("新建角色", "small", () => createAvatarRole(name.value.trim(), description.value.trim(), license.value.trim())),
      ),
    ),
  );
}

function renderCloudSpeakerBinding(packageState, binding) {
  const roles = (avatarRoles && avatarRoles.roles) || [];
  const selected = (binding || {}).role || {};
  const shot = (binding || {}).presenter_shot;
  const aspectFit = (binding || {}).aspect_fit || null;
  const sample = (binding || {}).sample || {};
  const roleSelect = el("select", { "aria-label": `${binding.name} 的角色身份` },
    el("option", { value: "" }, "不关联通用档案（可直接生成）"),
    roles.map((role) => el("option", { value: role.role_id }, `${role.name}（${role.references.length} 张参考图）`)),
  );
  roleSelect.value = selected.role_id || "";
  roleSelect.addEventListener("change", () => selectAvatarCloudRole(binding.speaker_id, roleSelect.value));
  const file = el("input", { type: "file", accept: ".png,.jpg,.jpeg,.webp", "aria-label": `上传${binding.name}的实际出镜图` });
  file.addEventListener("change", () => { if (file.files[0]) uploadAvatarCloudPresenter(binding.speaker_id, file.files[0]); });
  const sampleLabel = sample.approved ? "试片已确认" : sample.status === "awaiting_approval" ? "试片待确认" : sample.status === "generating" || sample.status === "queued" ? "试片生成中" : "尚未试片";
  return el("article", { class: `avatar-speaker-binding ${binding.status || "not_ready"}` },
    el("div", { class: "avatar-speaker-binding-head" },
      el("div", {}, el("span", { class: "asset-code" }, binding.speaker_id), el("strong", {}, binding.name), status(binding.status)),
      el("span", { class: "minor" }, sampleLabel),
    ),
    el("div", { class: "avatar-presenter-shot compact" },
      shot ? el("img", { class: "avatar-presenter-preview", alt: `${binding.name} 项目出镜图预览`, src: mediaURL(projectId, shot.path) }) : el("div", { class: "avatar-shot-empty" }, "尚未上传项目出镜图"),
      el("div", {},
        el("strong", {}, shot ? shot.original_filename : "上传一张该角色的实际单人出镜图"),
        el("p", { class: "minor" }, shot ? `${shot.media.width}×${shot.media.height} · 已冻结为后续任务输入` : "建议正面或半身、人物清晰、背景稳定；不要上传三视图拼图。"),
        el("label", { class: "button small" }, shot ? "更换该角色出镜图" : "选择出镜图", file),
        aspectFit ? el("div", { class: `avatar-aspect-status ${aspectFit.status}` },
          el("strong", {}, aspectFit.status === "matched" ? "画幅已匹配" : aspectFit.status === "prepared" ? "适配预览已就绪" : "需要处理画幅"),
          el("span", {}, `${aspectFit.target_label} · 差异 ${Number(aspectFit.difference_percent || 0).toFixed(1)}%`),
          el("span", { class: "minor" }, aspectFit.message),
          aspectFit.provider_input ? el("img", { class: "avatar-provider-input-preview", alt: `${binding.name} 的云端实际输入图预览`, src: mediaURL(projectId, aspectFit.provider_input.path) }) : null,
        ) : null,
      ),
    ),
    el("label", { class: "intake-field optional-role-field" }, el("span", {}, "通用角色档案（可选，不影响生成）"), roleSelect),
  );
}

function renderCloudRenderSpec(packageState) {
  const cloud = packageState.cloud || {};
  const isRunningHub = packageState.generation_mode === "runninghub_longcat";
  const providerLabel = isRunningHub ? "RunningHub InfiniteTalk" : "阿里云";
  const bindings = packageState.speaker_bindings || [];
  const aspect = el("select", { "aria-label": "数字人输出画幅" },
    el("option", { value: "portrait" }, "竖版 9:16"),
    el("option", { value: "landscape" }, "横版 16:9"),
    el("option", { value: "square" }, "方形 1:1"),
  );
  aspect.value = cloud.aspect_ratio || "portrait";
  const resolution = el("select", { "aria-label": `${providerLabel} 清晰度` },
    isRunningHub ? el("option", { value: "448x560" }, "448×560（4:5人物源，当前工作流固定）") : null,
    !isRunningHub ? el("option", { value: "480P" }, "480P（试片推荐，节省额度）") : null,
    !isRunningHub ? el("option", { value: "720P" }, "720P（正式片段，更高成本）") : null,
  );
  resolution.value = cloud.resolution || (isRunningHub ? "448x560" : "480P");
  if (isRunningHub) {
    aspect.value = "portrait";
    aspect.disabled = true;
    resolution.disabled = true;
  }
  const defaultFit = el("select", { "aria-label": "出镜图默认适配方式" },
    el("option", { value: "cover_crop" }, "居中裁切填满画幅（推荐口播）"),
    el("option", { value: "contain_blur" }, "保留全图，用模糊背景补边"),
  );
  defaultFit.value = cloud.input_fit_mode || "cover_crop";
  const mismatched = bindings.filter((binding) => (binding.aspect_fit || {}).status === "needs_choice");
  const perSpeakerChoices = mismatched.map((binding) => {
    const select = el("select", { "aria-label": `${binding.name} 的出镜图适配方式` },
      el("option", { value: "cover_crop" }, "居中裁切填满画幅"),
      el("option", { value: "contain_blur" }, "保留全图，模糊背景补边"),
    );
    select.value = avatarAspectFitChoices[binding.speaker_id] || defaultFit.value;
    select.addEventListener("change", () => { avatarAspectFitChoices[binding.speaker_id] = select.value; });
    return el("div", { class: "avatar-aspect-choice" },
      el("strong", {}, binding.name),
      el("span", { class: "minor" }, `${binding.aspect_fit.target_label} · 当前差异 ${Number(binding.aspect_fit.difference_percent || 0).toFixed(1)}%`),
      select,
    );
  });
  return el("section", { class: "panel avatar-render-spec" },
    el("div", { class: "panel-head" }, el("div", {},
      el("h4", {}, "2. 输出画幅与清晰度（提交前必检）"),
      el("p", {}, `画幅会同步为本项目最终成片画布；${providerLabel} 依据实际上传的图片生成口播。保存后系统会在本地生成每位角色的云端输入图，不消耗额度。`),
    )),
    el("div", { class: "panel-body" },
      el("div", { class: "avatar-render-spec-controls" },
        el("label", { class: "intake-field" }, el("span", {}, "项目与数字人画幅"), aspect),
        el("label", { class: "intake-field" }, el("span", {}, `${providerLabel} 清晰度`), resolution),
        el("label", { class: "intake-field" }, el("span", {}, "不匹配出镜图的默认处理"), defaultFit),
      ),
      el("div", { class: "report" }, isRunningHub ? "当前 InfiniteTalk 精确帧工作流使用 448×560 的4:5人物源，最终仍合成到项目竖版画布。系统会先冻结精确尺寸输入图供核对；此步骤不会消耗积分。" : "480P / 720P 是阿里云的清晰度档位，不是横竖比例。选择画幅后，系统以适配后的输入图约束比例；实际像素由阿里云按档位输出。"),
      mismatched.length ? el("div", { class: "report bad" }, `待处理：${mismatched.map((binding) => binding.name).join("、")} 的出镜图与目标画幅不一致。保存后会按所选方式生成可预览输入图。`) : null,
      perSpeakerChoices.length ? el("div", { class: "avatar-aspect-choice-list" }, perSpeakerChoices) : null,
      button("保存并生成云端输入图预览", "primary", () => {
        const changedCanvas = aspect.value !== (cloud.aspect_ratio || "portrait");
        const message = changedCanvas
          ? "这会同步修改本项目的最终画幅，并使未提交的数字人试片失效。不会调用云端。确认继续吗？"
          : "将按当前画幅生成或更新云端实际输入图预览，不会调用云端。确认继续吗？";
        if (confirm(message)) saveAvatarCloudRenderSpec({ aspect_ratio: aspect.value, resolution: resolution.value, default_fit_mode: defaultFit.value, fit_modes: avatarAspectFitChoices });
      }),
    ),
  );
}

async function saveRunningHubSettings(workflowId, apiKey, buttonNode) {
  const original = buttonNode.textContent;
  buttonNode.disabled = true;
  buttonNode.textContent = "正在保存…";
  try {
    await globalApi("/runninghub/config", {
      method: "PUT",
      body: {
        workflow_id: String(workflowId || "").trim(),
        workflow_profile: RUNNINGHUB_EXACT_WORKFLOW_PROFILE,
        workflow_template: RUNNINGHUB_EXACT_WORKFLOW_TEMPLATE,
        api_key: String(apiKey || "").trim(),
        base_url: "https://www.runninghub.cn",
      },
    });
    showToast("RunningHub 配置已安全保存，无需重启");
    await refresh();
  } catch (error) {
    showToast(error.message || "RunningHub 配置保存失败", true);
  } finally {
    buttonNode.disabled = false;
    buttonNode.textContent = original;
  }
}

function renderCloudMultiSpeakerActions(packageState) {
  const bindings = packageState.speaker_bindings || [];
  const cloud = packageState.cloud || {};
  const isRunningHub = packageState.generation_mode === "runninghub_longcat";
  const providerLabel = isRunningHub ? "RunningHub" : "阿里云";
  const providerReady = !isRunningHub || Boolean((cloud.configuration || {}).configured);
  const runningHubWorkflowId = el("input", { type: "text", inputmode: "numeric", value: RUNNINGHUB_EXACT_WORKFLOW_ID, readonly: "", "aria-label": "RunningHub 工作流 ID" });
  const runningHubApiKey = el("input", { type: "password", placeholder: (cloud.configuration || {}).api_key_configured ? "API 密钥已保存，留空不修改" : "RunningHub API 密钥", autocomplete: "new-password", "aria-label": "RunningHub API 密钥" });
  const saveRunningHubButton = button("保存 RunningHub 配置", "quiet", () => void saveRunningHubSettings(runningHubWorkflowId.value, runningHubApiKey.value, saveRunningHubButton));
  const allBindingsReady = bindings.length > 0 && bindings.every((binding) => binding.presenter_shot && ["matched", "prepared"].includes(((binding || {}).aspect_fit || {}).status));
  const allSamplesReady = bindings.length > 0 && bindings.every((binding) => (binding.sample || {}).status === "awaiting_approval" || (binding.sample || {}).approved);
  const allSamplesApproved = bindings.length > 0 && bindings.every((binding) => (binding.sample || {}).approved);
  const allDone = packageState.turns.length > 0 && packageState.turns.every((turn) => (turn.cloud_job || {}).status === "succeeded");
  const hasRunning = packageState.turns.some((turn) => ["queued", "uploading", "detecting", "submitted", "running", "downloading"].includes((turn.cloud_job || {}).status));
  const readyAudio = packageState.turns.filter((turn) => turn.driving_audio).length === packageState.turns.length;
  const approvalButtons = bindings.map((binding) => {
    const sample = binding.sample || {};
    const turn = packageState.turns.find((item) => item.turn_id === sample.turn_id);
    if (sample.status !== "awaiting_approval" || !turn) return null;
    return el("div", { class: "cloud-sample-approval" },
      el("strong", {}, `${binding.name} · ${turn.turn_id} 试片`),
      turn.source ? el("video", { controls: "", preload: "metadata", src: mediaURL(projectId, turn.source.path) }) : null,
      button(`确认 ${binding.name} 试片`, "small", () => mutate("/avatar-package/cloud/sample/approve", { method: "POST", body: { speaker_id: binding.speaker_id } }, `${binding.name} 的试片已确认`)),
    );
  }).filter(Boolean);
  return el("section", { class: "panel avatar-actions" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "3. 每位角色分别试片，再一键批量生成"), el("p", {}, `试片会各取该角色在脚本中的首段台词；确认都自然后，才允许提交其余轮次。${providerLabel} 调用会要求确认，并按脚本顺序串行执行。`))),
    el("div", { class: "panel-body" },
      isRunningHub ? el("details", { class: "runninghub-provider-config", open: providerReady ? null : "" },
        el("summary", {}, providerReady ? "RunningHub 接口已配置" : "完成 RunningHub 接口配置"),
        el("div", { class: "stack" },
          el("p", { class: "form-note" }, `生产工作流已冻结为 ${RUNNINGHUB_EXACT_WORKFLOW_ID} / ${RUNNINGHUB_EXACT_WORKFLOW_PROFILE}。这里只更新本机 API 密钥，不允许逐段工具覆盖一键生产合同。`),
          runningHubWorkflowId,
          runningHubApiKey,
          saveRunningHubButton,
        ),
      ) : null,
      isRunningHub && !providerReady ? el("div", { class: "report bad" }, `RunningHub 接口尚未就绪：${((cloud.configuration || {}).issues || []).join("；")}。API 密钥已保存在本机时，只需补充已发布工作流 ID。`) : null,
      isRunningHub && providerReady ? el("div", { class: "report ok" }, `RunningHub 已就绪 · 工作流 ${(cloud.configuration || {}).workflow_id || RUNNINGHUB_EXACT_WORKFLOW_ID} · InfiniteTalk精确帧 · 448×560`) : null,
      el("div", { class: `report ${cloud.status === "failed" ? "bad" : cloud.status === "completed" ? "ok" : ""}` }, cloud.message || "请完成上方输入。"),
      el("div", { class: "inline-actions" },
        button("生成每位角色的试片", "primary", () => {
          if (confirm(`将为 ${bindings.length} 位说话人各调用一次 ${providerLabel} 数字人试片，共 ${bindings.length} 个付费任务。确认消耗额度并开始吗？`)) mutate("/avatar-package/cloud/sample/jobs", { method: "POST", body: { confirm_paid: true } }, "试片任务已提交，生成进度会自动刷新");
        }, !providerReady || !allBindingsReady || !readyAudio || hasRunning || allSamplesReady || allSamplesApproved),
        button("开始生成其余片段", "primary", () => {
          const remaining = packageState.turns.filter((turn) => (turn.cloud_job || {}).status !== "succeeded").length;
          if (confirm(`将按脚本顺序生成剩余 ${remaining} 个片段，最多提交 ${remaining} 个 ${providerLabel} 付费任务；相同输入的已完成片段不会重复提交。确认消耗额度并开始吗？`)) mutate("/avatar-package/cloud/batch/jobs", { method: "POST", body: { confirm_paid: true } }, "其余片段已排队，将按顺序生成");
        }, !providerReady || !allSamplesApproved || hasRunning || allDone),
      ),
      approvalButtons.length ? el("div", { class: "cloud-sample-approvals" }, approvalButtons) : null,
      !allSamplesReady && allBindingsReady && readyAudio ? el("p", { class: "form-note" }, "试片生成完成后，会在这里分别出现雅雅、檬檬等角色的试听与确认按钮。") : null,
      !allBindingsReady ? el("p", { class: "form-note" }, `先完成每位角色的出镜图上传，并在上方保存输出画幅、生成可预览的云端输入图；未通过该检查不会提交 ${providerLabel} 任务。`) : null,
      allDone ? el("div", { class: "report ok" }, "全部片段已生成。下一步：检查全部原片 → 合成原声母版 → 应用为真实时间线。") : null,
    ),
  );
}

function renderVoiceboxBatchPanel(packageState) {
  const configuration = packageState.voicebox || {};
  const mappings = (packageState.speakers || []).map((speaker) => voiceboxMappingForSpeaker(packageState, speaker.speaker_id));
  const profiles = Array.isArray(voiceCatalog.profiles) ? voiceCatalog.profiles : [];
  const voiceReady = voiceCatalog.provider && voiceCatalog.provider.status === "available";
  const batch = configuration.batch || null;
  const running = batch && ["queued", "running"].includes(batch.status);
  const unresolved = mappings.filter((item) => item.status !== "ready");
  const batchItems = (batch && batch.items) || [];
  const complete = batchItems.filter((item) => item.status === "completed").length;
  const totalQueued = batchItems.filter((item) => item.status !== "skipped").length;
  const failed = batchItems.filter((item) => item.status === "failed");
  const mode = el("select", { "aria-label": "批量配音模式" },
    el("option", { value: "missing_and_apply" }, "补齐缺音频并自动采用（推荐）"),
    el("option", { value: "all_candidates" }, "全部生成候选，不覆盖当前音频"),
    el("option", { value: "failed_only_and_apply" }, "仅重试此前失败的轮次并自动采用"),
  );
  const mappingCards = mappings.map((mapping) => {
    const select = el("select", { "aria-label": `${mapping.speaker_name} 的本地音色` },
      el("option", { value: "" }, `自动：${mapping.profile_name || "待选择"}（${voiceboxSelectionLabel(mapping)}）`),
      profiles.map((profile) => el("option", { value: profile.id }, profile.name)),
    );
    select.value = mapping.selection_source === "manual" ? String(mapping.profile_id || "") : "";
    select.addEventListener("change", () => setAvatarVoiceboxSpeakerMapping(mapping.speaker_id, select.value));
    return el("article", { class: `voicebox-speaker-route ${mapping.status}` },
      el("div", {}, el("strong", {}, mapping.speaker_name), el("span", { class: "minor" }, `${voiceboxSelectionLabel(mapping)} · ${mapping.detail}`)),
      select,
    );
  });
  const itemSummary = batch ? el("div", { class: `voicebox-batch-progress ${running ? "is-running" : ""}` },
    status(batch.status),
    el("strong", {}, `批次 ${batch.batch_id}：${complete}/${totalQueued} 已完成`),
    el("span", {}, batch.mode === "all_candidates" ? "本批次只保留候选音频，不会替换已采用音频。" : "本批次按脚本顺序单线程执行，成功后自动采用为该轮驱动音频。"),
    failed.length ? el("span", { class: "voicebox-mapping-warning" }, `失败 ${failed.length} 段：${failed.map((item) => item.turn_id).join("、")}；可选择“仅重试此前失败的轮次”。`) : null,
  ) : el("p", { class: "minor" }, "尚未创建批量任务。批量任务会在开始时冻结台词与音色映射，执行中不会并发调用本地配音引擎。" );
  return el("section", { class: "panel voicebox-batch-panel" },
    el("div", { class: "panel-head" }, el("div", {},
      el("h4", {}, "1. 本地角色音色与一键配音"),
      el("p", {}, "优先按“说话人名称 = 本地音色名称”精确匹配；找不到才回退到通用默认音色。多个同名音色绝不猜测，必须人工指定。"),
    ), button("重新识别同名音色", "quiet small", refreshAvatarVoiceboxMappings, running || !voiceReady)),
    el("div", { class: "panel-body" },
      el("div", { class: "voicebox-speaker-routes" }, mappingCards),
      !voiceReady ? el("div", { class: "report bad" }, "Haike Video 本地配音当前不可用。请先在通用配音中心确认服务与音色可用；仍可上传已有音频。") : null,
      unresolved.length ? el("div", { class: "report bad" }, `请先为 ${unresolved.map((item) => item.speaker_name).join("、")} 指定音色，再开始批量配音。`) : null,
      el("div", { class: "voicebox-batch-actions" },
        el("label", { class: "intake-field" }, el("span", {}, "批量模式"), mode),
        button(running ? "批量配音进行中…" : "开始一键配音", "primary", () => startAvatarVoiceboxBatch(mode.value), running || !voiceReady || unresolved.length > 0),
      ),
      itemSummary,
    ),
  );
}

function renderCloudAvatar(packageState) {
  if (!avatarRoles && !avatarRolesLoading) loadAvatarRoles();
  const speakerNames = new Map(packageState.speakers.map((speaker) => [speaker.speaker_id, speaker.name]));
  const bindings = packageState.speaker_bindings || packageState.speakers.map((speaker) => ({ ...speaker, status: "not_ready", sample: { status: "not_started", approved: false } }));
  const readyAudio = packageState.turns.filter((turn) => turn.driving_audio).length;
  const generated = packageState.turns.filter((turn) => (turn.cloud_job || {}).status === "succeeded").length;
  const prepared = bindings.filter((binding) => binding.presenter_shot).length;
  const allAudioReady = readyAudio === packageState.turns.length;
  const allShotsReady = prepared === bindings.length && bindings.length > 0;
  const allInputsPrepared = allShotsReady && bindings.every((binding) => ["matched", "prepared"].includes(((binding || {}).aspect_fit || {}).status));
  const awaitingApproval = bindings.filter((binding) => (binding.sample || {}).status === "awaiting_approval");
  const allSamplesApproved = bindings.length > 0 && bindings.every((binding) => (binding.sample || {}).approved);
  const allDone = packageState.turns.length > 0 && generated === packageState.turns.length;
  const providerLabel = packageState.generation_mode === "runninghub_longcat" ? "RunningHub InfiniteTalk" : "阿里云";
  const nextStep = !allAudioReady
    ? `先补齐 ${packageState.turns.length - readyAudio} 段驱动音频。可使用下方“一键配音”，也可逐段上传。`
    : !allShotsReady
      ? `音频已齐。请为 ${bindings.filter((binding) => !binding.presenter_shot).map((binding) => binding.name).join("、")} 上传实际出镜图。`
      : !allInputsPrepared
        ? "出镜图已上传，但仍需先在“输出画幅与清晰度”中生成并核对云端实际输入图。"
      : awaitingApproval.length
        ? `试片已经生成，请播放并确认：${awaitingApproval.map((binding) => binding.name).join("、")}。`
        : !allSamplesApproved
          ? `音频与出镜图均已就绪。下一步是为每位说话人生成一条 ${providerLabel} 试片。`
          : !allDone
            ? `所有角色试片已确认。下一步批量生成剩余 ${packageState.turns.length - generated} 个片段。`
            : "全部数字人片段已完成。下一步检查原片并应用真实时间线。";
  return el("section", { class: "page" },
    pageHeader(`${providerLabel} 多角色数字人口播`, "脚本决定轮次顺序；每位说话人只需准备实际出镜图与对应音频。", "音频时长是唯一时间轴。通用角色档案是可选的身份留档，不再阻塞本期生成。"),
    el("section", { class: "panel avatar-local-switch" },
      el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "已有本地整段口播视频？"), el("p", {}, "雅雅、檬檬各一条完整口播即可。系统会按本期脚本自动切割，并要求你审核每句台词的边界后再合成。"))),
      el("div", { class: "panel-body" },
        el("p", { class: "form-note" }, `切换会新建独立的本地方案；当前 ${providerLabel} 配置、任务记录和已生成素材会被完整归档，不会混入本地流程。运行中的云端任务必须先结束或取消。`),
        button("改用本地整段口播视频", "quiet", switchToLocalLongformPlan),
      ),
    ),
    el("div", { class: `next-step-banner ${allDone ? "done" : ""}` }, el("strong", {}, allDone ? "本阶段已完成" : "当前下一步"), el("span", {}, nextStep)),
    el("div", { class: "metric-grid" },
      renderMetric("出镜图", `${prepared}/${bindings.length}`, "每位说话人一张实际场景图"),
      renderMetric("驱动音频", `${readyAudio}/${packageState.turns.length}`, "每段不超过 20 秒"),
      renderMetric("生成片段", `${generated}/${packageState.turns.length}`, "已下载到本项目素材目录"),
      renderMetric("云端状态", statusLabels[(packageState.cloud || {}).status] || (packageState.cloud || {}).status || "未知", "先分别试片，后批量"),
    ),
    renderVoiceboxBatchPanel(packageState),
    renderCloudRenderSpec(packageState),
    el("section", { class: "panel" },
      el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "2. 本期说话人与实际出镜图"), el("p", {}, "雅雅、檬檬等说话人已由脚本自动建立；直接上传各自的单人场景图即可生成，不需要再次新建角色。"))),
      el("div", { class: "panel-body" }, el("div", { class: "avatar-speaker-binding-list" }, bindings.map((binding) => renderCloudSpeakerBinding(packageState, binding)))),
    ),
    renderCloudMultiSpeakerActions(packageState),
    el("section", { class: "panel" },
      el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "4. 逐轮次驱动音频与试听"), el("p", {}, "导入脚本后已按说话人顺序生成 T001、T002…；一键配音后可在这里试听每段。单段也可手动换音色生成候选；画面会跟随音频真实时长，不会拉伸配音。"))),
      el("div", { class: "panel-body" }, el("div", { class: "avatar-turn-list" }, packageState.turns.map((turn) => renderCloudTurnCard(turn, speakerNames, packageState)))),
    ),
    renderCloudRoleLibraryForBindings(),
    generated === packageState.turns.length ? renderAvatarPackageActions(packageState) : null,
    avatarIssuePanel(packageState),
    avatarOutputs(packageState),
  );
}

function renderAvatarUserScriptStarter({ source, mode, background, treatment }) {
  const existingPreparation = Boolean((state.scenes || []).length || state.avatar_package || ((state.project || {}).script_draft || {}).script);
  const preview = avatarUserScriptPreview;
  const fileInput = el("input", { type: "file", accept: ".docx", "aria-label": "上传 Word 脚本" });
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) void previewAvatarUserDocx(fileInput.files[0]);
  });
  const pasteTitle = el("input", { type: "text", value: avatarUserScriptTitle, placeholder: "脚本标题（可选）" });
  pasteTitle.addEventListener("input", () => { avatarUserScriptTitle = pasteTitle.value; });
  const paste = el("textarea", {
    rows: "6",
    placeholder: "例如：\nT1 雅雅：欢迎收听今天的科技快报。\nT2 檬檬：先看第一条消息。",
  }, avatarUserScriptPasteText);
  paste.addEventListener("input", () => {
    avatarUserScriptPasteText = paste.value;
    if (avatarUserScriptPreview && avatarUserScriptPreview.source_kind === "pasted_text") {
      avatarUserScriptPreview = null;
      avatarUserScriptSpeakerOverrides = {};
    }
  });

  const adoptTitle = el("input", { type: "checkbox" });
  const replaceConfirmed = el("input", { type: "checkbox" });
  const previewPanel = preview ? el("div", { class: "avatar-template-preview" },
    el("div", { class: "avatar-template-metrics" },
      el("span", { class: "fact" }, `识别到 ${preview.turn_count} 个轮次`),
      el("span", { class: "fact" }, `预计 ${fmtDuration(preview.estimated_total_duration_seconds)}`),
      el("span", { class: "fact" }, `说话人：${(preview.speakers || []).map((speaker) => speaker.name).join("、")}`),
      el("span", { class: "fact" }, preview.source_kind === "docx" ? `来源：${preview.filename}` : "来源：粘贴文本"),
    ),
    (preview.warnings || []).length
      ? el("div", { class: "report" }, (preview.warnings || []).join("\n"))
      : el("div", { class: "report ok" }, "格式检查通过。系统不会调用 AI，也不会改写台词。"),
    el("div", { class: "speaker-mapping-grid" }, (preview.speakers || []).map((speaker) => {
      const input = el("input", {
        type: "text",
        value: avatarUserScriptSpeakerOverrides[speaker.name] || speaker.speaker_id,
        pattern: "[a-z][a-z0-9_-]{1,31}",
        "aria-label": `${speaker.name} 的说话人编号`,
      });
      input.addEventListener("input", () => { avatarUserScriptSpeakerOverrides[speaker.name] = input.value.trim().toLowerCase(); });
      return el("label", { class: "intake-field" }, el("span", {}, `${speaker.name} 的稳定编号`), input);
    })),
    el("div", { class: "template-turn-list" }, (preview.turns || []).map((turn) => el("article", { class: "template-turn" },
      el("div", { class: "template-turn-head" },
        el("strong", {}, `${turn.turn_id} · ${turn.speaker_name}`),
        el("span", {}, `${turn.source_location} · 预计 ${fmtDuration(turn.estimated_seconds)}`),
      ),
      el("p", {}, turn.text),
    ))),
  ) : el("div", { class: "empty" }, "上传 Word 或粘贴脚本后，系统会先展示全部轮次和角色映射；确认前不会写入正式脚本。");
  const initialize = () => {
    if (!preview) return showToast("请先解析脚本并核对预览", true);
    if (existingPreparation && !replaceConfirmed.checked) {
      return showToast("当前项目已有准备内容；请勾选覆盖确认后继续", true);
    }
    const ids = Object.values(avatarUserScriptSpeakerOverrides).map((value) => String(value || "").trim());
    if (ids.some((value) => !/^[a-z][a-z0-9_-]{1,31}$/.test(value))) {
      return showToast("说话人编号必须以小写字母开头，只能包含小写字母、数字、横线和下划线", true);
    }
    if (new Set(ids).size !== ids.length) return showToast("不同角色不能使用相同的说话人编号", true);
    void importAvatarUserScript({
      import_token: preview.import_token,
      speaker_overrides: avatarUserScriptSpeakerOverrides,
      generation_mode: source.value,
      import_mode: ["dashscope_wan_s2v", "runninghub_longcat"].includes(source.value) ? "per_turn" : mode.value,
      background_mode: background.value,
      default_treatment: treatment.value,
      adopt_source_title: adoptTitle.checked,
      replace_confirmed: existingPreparation ? replaceConfirmed.checked : false,
    });
  };
  return el("section", { class: "panel avatar-user-script-import" },
    el("div", { class: "panel-head" }, el("div", {},
      el("h4", {}, "1. 导入自己的成品脚本（推荐）"),
      el("p", {}, "上传 Word 或粘贴台词，先逐轮核对，再一次性建立分镜与数字人素材包。原文不会交给 AI 改写。"),
    )),
    el("div", { class: "panel-body" },
      el("div", { class: "inline-actions" },
        el("label", { class: `button primary${avatarUserScriptLoading ? " disabled" : ""}` }, avatarUserScriptLoading ? "正在解析 Word…" : "上传 Word 脚本", fileInput),
        el("span", { class: "form-note" }, "支持 .docx，最大 10 MB"),
      ),
      el("details", { class: "script-paste-box" },
        el("summary", {}, "也可以直接粘贴脚本"),
        el("div", { class: "stack" }, pasteTitle, paste, button(avatarUserScriptLoading ? "正在识别…" : "识别粘贴脚本", "quiet", () => void previewAvatarUserText(), avatarUserScriptLoading)),
      ),
      previewPanel,
      preview ? el("label", { class: "minor template-check" }, adoptTitle, " 使用脚本标题更新项目名称") : null,
      preview && existingPreparation ? el("label", { class: "minor template-check replace" }, replaceConfirmed, " 我已了解：系统会先备份旧脚本、分镜与数字人合同，再建立新的准备内容") : null,
      preview ? el("div", { class: "inline-actions" }, button(
        avatarUserScriptSubmitting ? "正在初始化…" : "确认导入并一键初始化",
        "primary",
        initialize,
        avatarUserScriptSubmitting || avatarUserScriptLoading,
      )) : null,
      el("p", { class: "form-note" }, "导入时长只是估算；数字人原声到位后，画面、字幕和剪辑点会改用真实音频时长。"),
    ),
  );
}

function renderAvatarTemplateStarter({ source, mode, background, treatment }) {
  if (!avatarScriptTemplates && !avatarScriptTemplatesLoading) void loadAvatarScriptTemplates();
  const templates = (avatarScriptTemplates && avatarScriptTemplates.templates) || [];
  const existingPreparation = Boolean((state.scenes || []).length || state.avatar_package || ((state.project || {}).script_draft || {}).script);
  if (avatarScriptTemplatesLoading && !templates.length) {
    return el("section", { class: "panel avatar-template-import" },
      el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "2. 选择项目内置模板（可选）"), el("p", {}, "正在读取本地内容库中的模板脚本……"))),
      el("div", { class: "panel-body" }, el("div", { class: "empty" }, "正在解析模板、说话人与台词。")),
    );
  }
  if (!templates.length) {
    return el("section", { class: "panel avatar-template-import" },
      el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "2. 选择项目内置模板（可选）"))),
      el("div", { class: "panel-body" }, el("div", { class: "report" }, "没有发现可导入模板。请将包含“说话人：台词”的 Markdown 文档放入 content/episodes/<期数>/docs 后刷新。")),
    );
  }
  const templateSelect = el("select", { "aria-label": "模板脚本" });
  for (const template of templates) {
    templateSelect.append(el("option", { value: template.template_id }, `${template.episode_id} · ${template.title}（${template.turn_count} 段）`));
  }
  templateSelect.value = selectedAvatarScriptTemplateId || templates[0].template_id;
  templateSelect.addEventListener("change", () => {
    selectedAvatarScriptTemplateId = templateSelect.value;
    avatarScriptTemplatePreview = null;
    void loadAvatarScriptTemplatePreview(selectedAvatarScriptTemplateId);
    render();
  });
  const preview = avatarScriptTemplatePreview && avatarScriptTemplatePreview.template_id === templateSelect.value ? avatarScriptTemplatePreview : null;
  const adoptTitle = el("input", { type: "checkbox", checked: "" });
  const replaceConfirmed = el("input", { type: "checkbox" });
  const initialize = () => {
    if (!preview) return showToast("请等待模板解析完成，再确认初始化", true);
    if (existingPreparation && !replaceConfirmed.checked) {
      return showToast("当前项目已有准备内容；请勾选“已了解覆盖会创建备份”后继续", true);
    }
    void importAvatarScriptTemplate({
      template_id: preview.template_id,
      generation_mode: source.value,
      import_mode: ["dashscope_wan_s2v", "runninghub_longcat"].includes(source.value) ? "per_turn" : mode.value,
      background_mode: background.value,
      default_treatment: treatment.value,
      adopt_template_title: adoptTitle.checked,
      replace_confirmed: existingPreparation ? replaceConfirmed.checked : false,
    });
  };
  const previewPanel = preview ? el("div", { class: "avatar-template-preview" },
    el("div", { class: "avatar-template-metrics" },
      el("span", { class: "fact" }, `识别到 ${preview.turn_count} 个片段轮次`),
      el("span", { class: "fact" }, `预估 ${fmtDuration(preview.estimated_total_duration_seconds)}`),
      el("span", { class: "fact" }, `说话人：${(preview.speakers || []).map((speaker) => speaker.name).join("、")}`),
    ),
    preview.warnings && preview.warnings.length ? el("div", { class: "report" }, preview.warnings.join("\n")) : null,
    el("div", { class: "template-turn-list" }, (preview.turns || []).map((turn) => el("article", { class: "template-turn" },
      el("div", { class: "template-turn-head" }, el("strong", {}, `${turn.turn_id} · ${turn.speaker_name}`), el("span", {}, `脚本预估 ${fmtDuration(turn.estimated_seconds)}`)),
      el("p", {}, turn.text),
    ))),
  ) : el("div", { class: "empty" }, "选择模板后，将在这里展示识别出的轮次顺序、说话人与全部台词。");
  return el("section", { class: "panel avatar-template-import" },
    el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "2. 选择项目内置模板（可选）"), el("p", {}, "适合复用内容库中的标准脚本；先审核台词顺序、角色归属和预估时长，确认后才写入项目。"))),
    el("div", { class: "panel-body avatar-template-body" },
      el("label", { class: "intake-field" }, el("span", {}, "本地模板脚本"), templateSelect),
      previewPanel,
      el("label", { class: "minor template-check" }, adoptTitle, " 使用模板标题更新项目名称"),
      existingPreparation ? el("label", { class: "minor template-check replace" }, replaceConfirmed, " 我已了解：系统会先备份旧的脚本、分镜与素材包合同，再创建新的准备工作") : null,
      el("div", { class: "inline-actions" }, button("确认导入并一键初始化", "primary", initialize, !preview)),
      el("p", { class: "form-note" }, "初始化会一次性建立 artifacts/script.json、分镜草案、T001…轮次清单及多角色数字人素材包。旧媒体文件不会被删除，只会从新的时间线脱离。"),
    ),
  );
}

function renderAvatar() {
  const packageState = state.avatar_package;
  if (!packageState) {
    const savedAvatarIntake = ((state.project && state.project.intake) || {}).avatar || {};
    const source = el("select", { "aria-label": "数字人来源" },
      el("option", { value: "runninghub_longcat" }, "RunningHub InfiniteTalk 精确帧生成（积分计费）"),
      el("option", { value: "dashscope_wan_s2v" }, "阿里云生成（项目出镜图 + 驱动音频）"),
      el("option", { value: "manual_import" }, "导入已完成的数字人视频"),
    );
    const mode = el("select", { "aria-label": "数字人导入模式" },
      el("option", { value: "per_turn" }, "按轮次逐条导入（推荐）"),
      el("option", { value: "longform" }, "每位数字人一条长视频（兼容）"),
    );
    const background = el("select", { "aria-label": "数字人导出背景" },
      el("option", { value: "opaque" }, "普通不透明背景（当前稳定支持）"),
      el("option", { value: "green_screen" }, "绿幕背景（保留给后续抠像）"),
      el("option", { value: "transparent" }, "透明背景（保留 Alpha 信息）"),
    );
    const treatment = el("select", { "aria-label": "默认数字人出镜方式" },
      el("option", { value: "fullscreen" }, "全屏数字人主体"),
      el("option", { value: "pip_top_left" }, "左上角数字人解说"),
      el("option", { value: "hidden" }, "暂时只使用主体画面"),
    );
    source.value = savedAvatarIntake.generation_mode || "runninghub_longcat";
    mode.value = savedAvatarIntake.import_mode || "per_turn";
    background.value = savedAvatarIntake.background_mode || "opaque";
    treatment.value = savedAvatarIntake.default_treatment || "fullscreen";
    return el("section", { class: "page" },
      pageHeader("数字人素材包", "让数字人原声成为剪辑的唯一时间轴", "可导入已完成视频，也可使用 RunningHub 或阿里云把项目出镜图与逐段音频生成数字人片段。"),
      renderAvatarUserScriptStarter({ source, mode, background, treatment }),
      renderAvatarTemplateStarter({ source, mode, background, treatment }),
      el("details", { class: "panel avatar-onboarding" },
        el("summary", { class: "panel-head" }, "高级恢复：从已有正式脚本建立数字人素材包"),
        el("div", { class: "panel-body avatar-init" },
          el("label", { class: "intake-field" }, el("span", {}, "数字人来源"), source),
          el("label", { class: "intake-field" }, el("span", {}, "素材导出方式"), mode),
          el("label", { class: "intake-field" }, el("span", {}, "数字人导出背景"), background),
          el("label", { class: "intake-field" }, el("span", {}, "默认出镜方式"), treatment),
          el("div", { class: "report" }, "仅用于项目已经存在合法 artifacts/script.json、但数字人素材包缺失的恢复场景。普通新项目请使用上方 Word、粘贴或内置模板入口。"),
          button("从现有正式脚本恢复", "quiet", () => mutate("/avatar-package/initialize", { method: "POST", body: { generation_mode: source.value, import_mode: ["dashscope_wan_s2v", "runninghub_longcat"].includes(source.value) ? "per_turn" : mode.value, background_mode: background.value, default_treatment: treatment.value } }, "数字人素材包已恢复")),
        ),
      ),
    );
  }
  if (["dashscope_wan_s2v", "runninghub_longcat"].includes(packageState.generation_mode)) return renderCloudAvatar(packageState);
  const uploaded = packageState.import_mode === "per_turn"
    ? packageState.turns.filter((turn) => turn.source).length
    : packageState.speakers.filter((speaker) => speaker.source).length;
  const expected = packageState.import_mode === "per_turn" ? packageState.turns.length : packageState.speakers.length;
  return el("section", { class: "page" },
    pageHeader("数字人素材包", "上传、核词、合成，一条时间轴到底", "数字人视频自带声音是主音轨；脚本时长只作预估，最终字幕和剪辑点都从实际原声生成。"),
    el("div", { class: "metric-grid" },
      renderMetric("导入模式", packageState.import_mode === "per_turn" ? "逐轮次" : "长视频", packageState.provider.name),
      renderMetric("上传进度", `${uploaded}/${expected}`, "只接受项目内的真实媒体文件"),
      renderMetric("台词核验", statusLabels[packageState.asr.status] || packageState.asr.status, packageState.settings.require_asr ? "本地 ASR，不后台下载模型" : "测试模式已关闭"),
      renderMetric("母版状态", statusLabels[packageState.assembly.status] || packageState.assembly.status, "原声时间线 / 25fps / 48kHz"),
    ),
    packageState.import_mode === "per_turn" ? renderAvatarPerTurn(packageState) : renderAvatarLongform(packageState),
    renderAvatarPackageActions(packageState),
    packageState.import_mode === "longform" ? renderLongformSpeakerDiagnostics(packageState) : null,
    packageState.import_mode === "longform" ? renderLongformCutReview(packageState) : null,
    avatarIssuePanel(packageState),
    avatarOutputs(packageState),
  );
}

function renderHistory() {
  const decisions = state.decisions.slice().reverse();
  const activities = state.activities.slice().reverse().slice(0, 40);
  const historyList = el("div", { class: "activity" }, activities.map((item) => el("div", { class: "activity-item" }, el("time", {}, (item.at || "").replace("T", " ").slice(5, 16)), el("span", {}, item.message))));
  const decisionList = el("div", { class: "activity" }, decisions.map((item) => el("div", { class: "activity-item" },
    el("time", {}, item.id),
    el("span", {}, `${item.subject}：${item.selected}${item.note ? `；${item.note}` : ""}`),
  )));
  return el("section", { class: "page" },
    pageHeader("决策记录", "每个选择都有原因，不会只剩最终结果", "素材来源、具体用法与热插拔合同会作为项目内审计记录保留。"),
    el("div", { class: "grid-2" },
      el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "最近活动"), el("p", {}, "按发生时间倒序"))), el("div", { class: "panel-body" }, activities.length ? historyList : el("div", { class: "empty" }, "尚无活动记录。"))),
      el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("h4", {}, "人工决策"), el("p", {}, "来源、素材与热插拔选择"))), el("div", { class: "panel-body" }, decisions.length ? decisionList : el("div", { class: "empty" }, "还没有需要记录的决策。"))),
    ),
  );
}

function content() {
  if (activeView === "overview") return renderOverview();
  if (activeView === "avatar") return renderAvatar();
  if (activeView === "assets") return renderAssets();
  if (activeView === "patch") return renderReview();
  if (activeView === "quality") return renderQuality();
  if (activeView === "history") return renderHistory();
  return renderReview();
}

function render() {
  if (!state) return;
  reviewCaptionControllers.clear();
  releaseCaptionCanvasObservers();
  captureReviewInteractionState();
  app.replaceChildren(renderSidebar(), el("main", { class: "workspace", id: "main-content", tabindex: "-1" }, renderTopbar(), content()));
  restoreReviewInteractionState();
}

assetForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(assetForm).entries());
  try {
    state = await api("/assets", { method: "POST", body: values });
    assetDialog.close();
    assetForm.reset();
    ensureSelection();
    render();
    showToast("素材已登记，并获得稳定编号");
  } catch (error) { showToast(error.message || "素材登记失败", true); }
});

imageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(imageForm).entries());
  const summary = `将使用 OpenAI 兼容服务的 gpt-image-2 生成 ${values.n} 张${values.quality === "high" ? "高质量" : values.quality === "low" ? "低质量试样" : "中质量"}图片。该操作可能产生费用，是否继续？`;
  if (!window.confirm(summary)) return;
  try {
    state = await api("/openai-images", { method: "POST", body: Object.assign(values, { confirmed: true }) });
    imageDialog.close();
    imageForm.reset();
    activeView = "assets";
    ensureSelection();
    render();
    showToast("AI 图片已生成并登记为稳定素材；请审核后再分配到场景");
  } catch (error) { showToast(error.message || "AI 生图失败", true); }
});

aiConfigForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(aiConfigForm).entries());
  try {
    // Both providers share one local secret file. Save sequentially so two
    // concurrent atomic replacements can never race and drop one provider.
    const config = await globalApi("/ai-text/config", { method: "PUT", body: {
      base_url: values.base_url, model: values.model, api_key: values.api_key,
    } });
    const doubao = await globalApi("/ai-text/doubao/config", { method: "PUT", body: {
      base_url: values.doubao_base_url, model: values.doubao_model, api_key: values.doubao_api_key,
    } });
    aiConfigForm.elements.api_key.value = "";
    aiConfigForm.elements.api_key.placeholder = `已保存 ${config.api_key_masked || "密钥"}，留空不修改`;
    paintAIConfigStatus(config, "配置已安全保存；无需重启工作台");
    aiConfigForm.elements.doubao_api_key.value = "";
    aiConfigForm.elements.doubao_api_key.placeholder = doubao.configured
      ? `已保存 ${doubao.api_key_masked || "密钥"}，留空不修改`
      : "请输入豆包 API 密钥；留空则由主模型降级完成选题、写稿与冷审";
    paintDoubaoConfigStatus(doubao, doubao.configured ? "豆包快报主编配置已安全保存" : "豆包未配置，将使用主模型降级");
    showToast("主模型与豆包配置已保存");
  } catch (error) {
    paintAIConfigStatus(null, error.message || "保存配置失败");
    paintDoubaoConfigStatus(null, error.message || "保存配置失败");
    showToast(error.message || "保存配置失败", true);
  }
});

aiConfigTest.addEventListener("click", async () => {
  aiConfigTest.disabled = true;
  aiConfigTest.textContent = "正在测试…";
  try {
    const result = await globalApi("/ai-text/test", { method: "POST", body: {} });
    paintAIConfigStatus({ configured: true }, `${result.message} · ${result.model}`);
    showToast("AI 连接测试通过");
  } catch (error) {
    paintAIConfigStatus(null, error.message || "连接测试失败");
    showToast(error.message || "连接测试失败", true);
  } finally {
    aiConfigTest.disabled = false;
    aiConfigTest.textContent = "测试主模型";
  }
});

doubaoConfigTest.addEventListener("click", async () => {
  doubaoConfigTest.disabled = true;
  doubaoConfigTest.textContent = "正在测试…";
  try {
    const result = await globalApi("/ai-text/doubao/test", { method: "POST", body: {} });
    paintDoubaoConfigStatus({ configured: true }, `${result.message} · ${result.model}`);
    showToast("豆包连接测试通过");
  } catch (error) {
    paintDoubaoConfigStatus(null, error.message || "豆包连接测试失败");
    showToast(error.message || "豆包连接测试失败", true);
  } finally {
    doubaoConfigTest.disabled = false;
    doubaoConfigTest.textContent = "测试豆包";
  }
});

subscribe(`/api/project/${encodedProjectId}/events`, () => {
  if (reviewPreviewIsActive()) {
    void pollReviewPreviewJob();
    return;
  }
  // Keyframe workers emit an event after every durable anchor write. Their
  // isolated task card already polls the small status endpoint; fetching and
  // rendering the entire workbench here would recreate the active video.
  if (keyframeJobSceneId && (keyframeJob?.status === "generating" || keyframeResultsReady)) {
    void pollKeyframeJob();
    return;
  }
  if (visualBatchRunning() || visualBatchResultsReady) {
    void pollVisualBatch();
    return;
  }
  if (previewSyncRunning()) {
    void pollPreviewSync();
    return;
  }
  void refresh();
});
refresh();
