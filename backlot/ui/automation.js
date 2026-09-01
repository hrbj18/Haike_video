import { el, getJSON } from "/ui/lib.js";

const STAGES = [
  ["research", "检索新闻"], ["script", "脚本质检"], ["project", "创建项目"],
  ["voice", "双角色配音"], ["avatar", "数字人生成"], ["align", "切割排序"],
  ["visuals", "补全画面"], ["compose", "全片预览"], ["review_ready", "等待审核"],
];
const STATUS_LABELS = { pending: "待执行", running: "进行中", succeeded: "已完成", skipped: "已跳过", failed: "失败", awaiting_human: "等待人工", awaiting_provider_authorization: "等待供应商授权", ambiguous: "提交结果待核对" };
const RUN_LABELS = { queued: "等待开始", running: "正在生产", awaiting_human: "等待脚本确认", awaiting_provider_authorization: "等待供应商授权", ambiguous: "提交结果待核对", blocked: "内容硬门阻断", failed: "需要处理", review_ready: "等待人工审核", cancelled: "已取消" };
const TOPIC_LABELS = { robotics: "机器人", ai_model: "大模型", chips_compute: "芯片算力", science_space: "科学航天", security_policy: "安全政策", consumer_tech: "消费科技", software_open_source: "软件开源", gaming: "游戏", internet_platform: "互联网平台", other: "其他" };
const FORM_LABELS = { visual_record: "现场名场面", risk_incident: "风险事件", breakthrough: "技术突破", price_change: "价格变化", financing: "融资投资", product_launch: "产品发布", product_update: "功能更新", trailer_announcement: "预告演示", rumor: "消息曝光" };
const RECOVERY_LABELS = { drafting: "正在写初稿", reviewing: "正在传播复验", repairing: "正在定点修稿", reselecting: "正在替换弱题", rescue_research: "正在补充候选", compact_fallback: "正在生成紧凑版", awaiting_human: "等待人工接管", fallback_review_candidate: "可靠稿继续生成待审片", passed: "文本双门通过", failed: "没有结构有效稿" };
const $ = (id) => document.getElementById(id);
let state = null;
let selectedRun = null;
let refreshTimer = null;

function localYesterday() {
  const value = new Date();
  value.setDate(value.getDate() - 1);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

function fmtDate(value) {
  if (!value) return "—";
  return String(value).replace("T", " ").replace("+08:00", "");
}

function money(run) {
  const budget = run?.budget || {};
  return Number(budget.spent || 0) + Number(budget.reserved || 0);
}

function setMessage(message = "", error = false) {
  const node = $("automationMessage");
  node.textContent = message;
  node.classList.toggle("error", error);
}

function renderScheduler() {
  const config = state?.config || {};
  const scheduler = state?.scheduler || {};
  const effective = state?.effective_state || {};
  const enabled = effective.effective_enabled === true;
  const conflict = effective.conflict === true;
  const lastRunFailed = enabled && effective.last_run_succeeded === false;
  $("automationEnabled").checked = config.enabled === true;
  $("automationSwitchLabel").textContent = enabled ? "每日自动生产已生效" : (conflict ? "配置开启但调度未生效" : "每日自动生产已关闭");
  const badge = $("automationHealth");
  badge.className = `automation-health ${(conflict || lastRunFailed) ? "warning" : (enabled ? "ok" : "off")}`;
  badge.textContent = conflict ? "凌晨调度异常" : (lastRunFailed ? "上次生产失败" : (enabled ? "凌晨调度正常" : "自动生产已关闭"));
  const alert = $("schedulerAlert");
  alert.className = `scheduler-alert${conflict ? " visible danger" : (lastRunFailed ? " visible" : "")}`;
  alert.textContent = (conflict || lastRunFailed) ? (effective.message || "项目配置与 Windows 计划任务不一致。") : "";
  $("repairScheduler").hidden = !conflict;
  const facts = [
    ["项目配置", config.enabled ? "已开启" : "已关闭"],
    ["Windows 任务", scheduler.installed ? (scheduler.runtime_enabled ? "已启用" : "已禁用") : "未安装"],
    ["下一次运行", enabled ? (scheduler.next_run_time || state.next_run?.starts_at || "等待系统计算") : "不会自动运行"],
    ["上次运行", scheduler.last_run_time || "尚无"],
    ["上次退出码", scheduler.last_result === 0 ? "0（正常）" : (scheduler.last_result ?? "尚无")],
  ];
  $("schedulerFacts").replaceChildren(...facts.map(([label, value]) => el("span", { class: "automation-fact" }, el("small", {}, label), el("strong", {}, String(value)))));
}

function renderPreflight() {
  const config = state?.config || {};
  const ai = state?.text_ai || {};
  const providers = state?.text_providers || {};
  const selector = providers[providers.selection_provider] || ai;
  const writer = providers[providers.writer_provider] || {};
  const reviewer = providers[providers.reviewer_provider] || ai;
  const fallback = providers[providers.technical_fallback_provider] || ai;
  const safety = state?.billing_safety || {};
  const golden = state?.golden_scripts || {};
  const douyin = state?.douyin_signals || {};
  const scheduler = state?.effective_state || {};
  const douyinDetail = douyin.state === "ok"
    ? `${douyin.latest_modes?.join(" + ") || "已采集"} · ${Number(douyin.latest_ok_count || 0)} 条热度信号`
    : (douyin.state === "ready" ? "接口或快照已就绪，等待下一轮采集" : "未配置；本轮会非致命跳过");
  const rows = [
    ["中国市场选题主编", selector.configured ? `${selector.model || "已配置"} · ${providers.selection_provider === "doubao" ? "豆包" : "Luna 降级"}` : "未配置，无法完成市场选题", !selector.configured],
    ["中文对话写稿", writer.configured ? `${writer.model || "已配置"} · ${providers.writer_provider === "doubao" ? "豆包" : "Luna 降级"}` : "未配置写稿模型", !writer.configured],
    ["独立传播冷审", reviewer.configured ? `${reviewer.model || "已配置"} · ${providers.reviewer_provider === "doubao" ? "豆包新请求" : "Luna 降级"}` : "未配置传播复验模型", !reviewer.configured],
    ["事实与技术兜底", fallback.configured ? `${fallback.model || "已配置"} · 仅服务不可用时接管` : "未配置 Luna 技术兜底", !fallback.configured],
    ["写稿黄金样板", `${Number(golden.loaded_count || 0)} 份用户批准样板已加载`, Number(golden.loaded_count || 0) < 1],
    ["抖音热度信号", douyinDetail, douyin.state !== "ok"],
    ["双主持语音", "Haike Video 本地同名音色 · 两条长音频后切割", false],
    ["数字人算力", "精确帧工作流 2094449979141218305 · Standard 24GB · Plus禁用", false],
    ["画面合同", `${config.aspect === "portrait" ? "1080×1920" : config.aspect} · 4:5 源 · ${config.avatar?.shape === "circle" ? "圆形" : "可调外框"}`, false],
    ["成片默认项", `${config.background_music?.enabled ? "新闻背景音乐" : "无背景音乐"} · 默认字幕 · 不自动发布`, !scheduler.healthy && config.enabled],
  ];
  $("preflightGrid").replaceChildren(...rows.map(([title, detail, warning]) => el("div", { class: `preflight-item${warning ? " warning" : ""}` }, el("b", {}, title), el("span", {}, detail))));
}

function renderTextRecovery(run) {
  const node = $("textRecoveryDetail");
  const recovery = run?.text_resilience || {};
  const combinations = recovery.combinations || [];
  const attempts = recovery.attempts || [];
  if (!run || (!combinations.length && !recovery.state)) {
    node.replaceChildren(
      el("h3", {}, "文本韧性与候选组合"),
      el("p", {}, "完成新闻筛选后，这里会显示候选组合、换题原因和剩余恢复额度。"),
    );
    return;
  }
  const policy = recovery.policy || {};
  const stateLabel = RECOVERY_LABELS[recovery.state] || recovery.state || "等待文本阶段";
  const summary = el("div", { class: "recovery-summary" },
    el("span", { class: `recovery-state ${recovery.state || "idle"}` }, stateLabel),
    el("span", {}, `传播复验 ${Number(recovery.editorial_reviews_used || 0)} / ${Number(policy.max_editorial_reviews || 2)}`),
    el("span", {}, `已尝试 ${Number(recovery.attempt_count || 0)} 次`),
    ...(recovery.best_score ? [el("span", {}, `当前最佳 ${recovery.best_score} 分`)] : []),
  );
  const combinationCards = combinations.map((combo) => {
    const selected = combo.combination_id === recovery.best_combination_id;
    const stories = (combo.stories || []).map((story) => el("div", { class: "recovery-story" },
      el("span", { class: `heat-chip ${String(story.heat_level || "").toLowerCase()}` }, story.heat_level || "H?"),
      el("div", {},
        el("b", {}, story.headline || story.event_id || "未命名新闻"),
        el("small", {}, `${TOPIC_LABELS[story.topic_family] || story.topic_family || "未分类"} · ${FORM_LABELS[story.event_form] || story.event_form || "未分类"}`),
      ),
      ...(story.replacement_priority === 1 ? [el("span", { class: "replace-hint" }, "优先替换")] : []),
    ));
    return el("section", { class: `combination-card${selected ? " selected" : ""}` },
      el("header", {},
        el("b", {}, `组合 ${combo.rank || "—"}${selected ? " · 当前最佳" : ""}`),
        el("span", {}, `${Number(combo.episode_score || 0).toFixed(1)} 分 · ${combo.story_count || 0} 条${combo.duration_profile === "compact_high_value" ? " · 紧凑版" : ""}`),
      ),
      ...stories,
    );
  });
  const attemptRows = attempts.map((attempt) => el("li", {},
    el("b", {}, `${attempt.attempt_id || "尝试"} · ${attempt.editorial_score || "未评分"}分`),
    el("span", {}, attempt.status === "passed" ? "已通过" : (attempt.recovery?.reason || "已保留问题证据")),
  ));
  node.replaceChildren(
    el("div", { class: "recovery-head" },
      el("div", {}, el("h3", {}, "文本韧性与候选组合"), el("p", {}, recovery.terminal_reason || "系统会先修句子；发现整期同质化时才换题或缩编。")),
      summary,
    ),
    combinations.length ? el("div", { class: "combination-grid" }, ...combinationCards) : el("p", {}, "候选组合正在生成。"),
    ...(attempts.length ? [el("details", { class: "attempt-ledger" }, el("summary", {}, "查看文本尝试记录"), el("ol", {}, ...attemptRows))] : []),
  );
}

function renderRun(run) {
  selectedRun = run || null;
  const overview = $("runOverview");
  if (!run) {
    overview.replaceChildren(el("h3", {}, "尚无自动生产记录"), el("span", {}, "选择一个已结束的自然日立即生成。"));
    $("stageTimeline").replaceChildren();
    $("scriptDetail").replaceChildren(el("h3", {}, "脚本摘要"), el("p", {}, "任务开始后显示。"));
    $("costDetail").replaceChildren(el("h3", {}, "成本与算力"), el("p", {}, "尚无费用。"));
    renderTextRecovery(null);
    $("failurePanel").hidden = true;
    return;
  }
  overview.replaceChildren(
    el("h3", {}, `${run.target_date} · ${RUN_LABELS[run.status] || run.status}`),
    el("span", {}, `触发：${run.trigger === "schedule" ? "凌晨计划任务" : "前端/手工"}`),
    el("span", {}, `更新：${fmtDate(run.updated_at)}`),
    ...(run.project_id ? [el("a", { href: `/p/${encodeURIComponent(run.project_id)}` }, "进入项目审核 →")] : []),
  );
  $("stageTimeline").replaceChildren(...STAGES.map(([name, label], index) => {
    const item = run.stages?.[name] || {};
    return el("div", { class: `stage-node ${item.status || "pending"}`, title: item.error || item.message || "" },
      el("span", { class: "stage-index" }, String(index + 1).padStart(2, "0")),
      el("b", {}, label),
      el("small", {}, STATUS_LABELS[item.status] || "待执行"),
      el("small", {}, item.message || (item.attempts ? `已尝试 ${item.attempts} 次` : "")),
    );
  }));
  const script = run.script_summary || {};
  const release = run.media_release_decision || {};
  $("scriptDetail").replaceChildren(
    el("h3", {}, "脚本与新闻选择"),
    el("p", {}, script.story_count ? `${script.story_count} 条新闻 · ${script.line_count} 句台词 · 传播复验 ${script.editorial_score || "—"} 分 · ${release.decision === "fallback_review_candidate" ? "可靠可用稿，继续生成待审片" : "优质线通过"}` : "尚未生成脚本。"),
    ...(release.decision ? [el("p", {}, `媒体放行：${release.decision} · ${(release.reasons || []).join("；")}`)] : []),
    ...(script.headlines?.length ? [el("ol", { class: "script-headlines" }, ...script.headlines.map((headline) => el("li", {}, headline)))] : []),
  );
  const spent = money(run);
  const limit = Number(run.budget?.limit || 5);
  const auditRows = Object.entries(run.billing_audit || {});
  $("costDetail").replaceChildren(
    el("h3", {}, "成本与算力证据"),
    el("div", { class: "cost-number" }, `¥${spent.toFixed(2)} / ¥${limit.toFixed(2)}`),
    el("p", {}, auditRows.length ? auditRows.map(([role, item]) => `${role === "yaya" ? "雅雅" : "檬檬"}：${item.observed_instance || "未产生账单"}`).join("；") : "本轮尚未产生 RunningHub 账单。"),
    ...(run.provider_eligibility ? [el("p", {}, `供应商资格：${run.provider_eligibility.reason || run.provider_eligibility.state}`)] : []),
    ...((run.paid_operations || []).length ? [el("ul", { class: "script-headlines" }, ...(run.paid_operations || []).map((item) => el("li", {}, `${item.role || "任务"} · ${item.state} · ${item.task_id || "暂无任务编号"}`)))] : []),
    el("p", {}, "网络、排队、限流和超时只恢复当前阶段；不会升级到 Plus 48GB，也不会重复提交结果未知的付费任务。"),
  );
  renderTextRecovery(run);
  const panel = $("failurePanel");
  const failedStage = run.failure?.stage ? run.stages?.[run.failure.stage] : null;
  panel.hidden = !["failed", "awaiting_human", "awaiting_provider_authorization", "ambiguous", "blocked"].includes(run.status);
  if (!panel.hidden) {
    if (run.status === "awaiting_provider_authorization") {
      panel.replaceChildren(el("h3", {}, "脚本已放行，等待供应商资格"), el("p", {}, run.provider_eligibility?.reason || "Lite 账单尚未核验。"), el("p", {}, "当前不会提交 RunningHub，也不会产生数字人费用。"));
      return;
    }
    if (run.status === "ambiguous") {
      panel.replaceChildren(el("h3", {}, "供应商提交结果未知"), el("p", {}, "系统不会自动重提，避免重复扣费。请先按任务时间在 RunningHub 核对任务列表。"));
      return;
    }
    if (run.status === "awaiting_human") {
      const editorialRecovery = run.approval_policy?.editorial_recovery_reason;
      panel.replaceChildren(
        el("h3", {}, editorialRecovery ? "自动恢复已用完，最佳稿已保留" : "脚本已生成，付费媒体暂未启动"),
        el("p", {}, editorialRecovery || run.approval_policy?.fallback_script_reason || "本轮没有 H3 爆点，脚本达到可靠可用线后仍需人工确认。"),
        el("p", {}, editorialRecovery ? "你可以保留当前头条，换掉最低贡献选题后重新进行文本双门；该动作本身不会产生媒体费用。" : "确认只代表允许下一次启动进入配音；不会自动发布视频。"),
        el("div", { class: "failure-actions" }, editorialRecovery
          ? el("button", { class: "primary-action", type: "button", onclick: () => replaceWeakStory(run) }, "保留头条并替换弱题")
          : el("button", { class: "primary-action", type: "button", onclick: () => approveFallbackScript(run) }, "确认脚本可进入媒体制作"),
        ),
      );
      return;
    }
    const succeeded = STAGES.filter(([name]) => ["succeeded", "skipped"].includes(run.stages?.[name]?.status)).map(([, label]) => label);
    panel.replaceChildren(
      el("h3", {}, `任务停在：${STAGES.find(([name]) => name === run.failure?.stage)?.[1] || run.current_stage}`),
      el("p", {}, run.failure?.summary || "本阶段未完成，已保存前序产物。"),
      el("p", {}, `继续时会复用：${succeeded.join("、") || "暂无"}。`),
      el("details", {}, el("summary", {}, "查看技术错误"), el("pre", {}, failedStage?.error || "未记录错误")),
      el("div", { class: "failure-actions" },
        el("button", { class: "primary-action", type: "button", onclick: () => resumeRun(run) }, `从${STAGES.find(([name]) => name === run.current_stage)?.[1] || "失败阶段"}继续`),
        el("span", { class: "muted" }, spent ? "继续前会核对剩余预算与已有任务。" : "当前未产生 RunningHub 费用；恢复后可能提交企业 Lite 任务。"),
      ),
    );
  }
}

function renderHistory() {
  const runs = state?.history || [];
  $("historyList").replaceChildren(...runs.map((run) => el("button", {
    class: `history-row${selectedRun?.target_date === run.target_date ? " selected" : ""}`,
    type: "button",
    onclick: () => { renderRun(run); renderHistory(); },
  },
  el("strong", {}, run.target_date),
  el("b", { class: run.status === "review_ready" ? "good" : (run.status === "failed" ? "bad" : "") }, RUN_LABELS[run.status] || run.status),
  el("span", {}, STAGES.find(([name]) => name === run.current_stage)?.[1] || run.current_stage || "—"),
  el("span", {}, `¥${money(run).toFixed(2)}`),
  el("span", {}, fmtDate(run.updated_at)),
  )));
}

function renderAll() {
  renderScheduler();
  renderPreflight();
  const active = state?.active_run;
  const preferred = active || (selectedRun && state.history?.find((item) => item.target_date === selectedRun.target_date)) || state?.latest_run;
  renderRun(preferred);
  renderHistory();
  const busy = active && ["queued", "running"].includes(active.status);
  $("startRun").disabled = Boolean(busy);
  $("startRun").textContent = busy ? "正在后台生产…" : "立即生成";
  if (busy) scheduleRefresh(3000);
}

function scheduleRefresh(delay = 10000) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refresh().catch(() => {}), delay);
}

async function refresh() {
  $("refreshAutomation").disabled = true;
  try {
    state = await getJSON("/api/daily-automation/status");
    renderAll();
  } catch (error) {
    setMessage(error.message || "自动生产状态读取失败", true);
  } finally {
    $("refreshAutomation").disabled = false;
  }
}

async function saveEnabled(enabled, actionLabel) {
  const checkbox = $("automationEnabled");
  checkbox.disabled = true;
  setMessage(`${actionLabel}…`);
  try {
    const response = await fetch("/api/daily-automation/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `${actionLabel}失败`);
    state = body;
    setMessage(enabled ? "凌晨 03:00 自动生产已启用，项目配置与 Windows 任务已同步。" : "每日自动生产已关闭，历史任务和项目均保留。" );
    renderAll();
  } catch (error) {
    setMessage(error.message || `${actionLabel}失败`, true);
    await refresh();
  } finally {
    checkbox.disabled = false;
  }
}

async function startTarget(target, label) {
  $("startRun").disabled = true;
  setMessage(`${label}：正在建立幂等运行清单…`);
  try {
    const response = await fetch("/api/daily-automation/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target_date: target }) });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `${label}失败`);
    setMessage(`${target} 已由 Haike Video 后台接管；关闭浏览器不会中断任务。`);
    await refresh();
  } catch (error) {
    setMessage(error.message || `${label}失败`, true);
    $("startRun").disabled = false;
  }
}

async function resumeRun(run) {
  await startTarget(run.target_date, "恢复任务");
}

async function approveFallbackScript(run) {
  setMessage("正在记录人工脚本确认…");
  try {
    const response = await fetch(`/api/daily-automation/runs/${encodeURIComponent(run.target_date)}/approve-fallback-script`, { method: "POST" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "脚本确认失败");
    setMessage(body.message || "脚本已确认；可再次启动任务继续媒体制作。");
    await refresh();
  } catch (error) {
    setMessage(error.message || "脚本确认失败", true);
  }
}

async function replaceWeakStory(run) {
  setMessage("正在锁定头条并切换差异化候选…");
  try {
    const response = await fetch(`/api/daily-automation/runs/${encodeURIComponent(run.target_date)}/replace-weak-story`, { method: "POST" });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "没有可替换的差异化候选");
    setMessage(body.message || "候选已切换，正在重新进入文本双门。" );
    await startTarget(run.target_date, "重新审核脚本");
  } catch (error) {
    setMessage(error.message || "替换弱题失败", true);
  }
}

$("automationEnabled").addEventListener("change", (event) => saveEnabled(event.target.checked, event.target.checked ? "正在启用每日调度" : "正在关闭每日调度"));
$("repairScheduler").addEventListener("click", () => saveEnabled(true, "正在重新同步 Windows 计划任务"));
$("refreshAutomation").addEventListener("click", () => refresh());
$("startRun").addEventListener("click", () => startTarget($("targetDate").value || localYesterday(), "启动任务"));
$("targetDate").value = localYesterday();
refresh();
