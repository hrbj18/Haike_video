import { el, fmtAgo, getJSON, subscribe } from "/ui/lib.js";

const grid = document.getElementById("grid");
const form = document.getElementById("projectForm");
const dialog = document.getElementById("projectDialog");
const createButton = document.getElementById("createProject");
const closeButton = document.getElementById("closeProjectDialog");
const cancelButton = document.getElementById("cancelProject");
const titleInput = document.getElementById("projectTitle");
const idInput = document.getElementById("projectId");
const formMessage = document.getElementById("projectFormMessage");
const submitButton = document.getElementById("submitProject");
const libraryMessage = document.getElementById("libraryMessage");
const deleteDialog = document.getElementById("deleteProjectDialog");
const deleteForm = document.getElementById("deleteProjectForm");
const deleteSummary = document.getElementById("deleteProjectSummary");
const deleteMessage = document.getElementById("deleteProjectMessage");
const deleteButton = document.getElementById("confirmDeleteProject");
const finalDeleteDialog = document.getElementById("finalDeleteProjectDialog");
const finalDeleteForm = document.getElementById("finalDeleteProjectForm");
const finalDeleteName = document.getElementById("finalDeleteProjectName");
const finalDeleteMessage = document.getElementById("finalDeleteProjectMessage");
const executeDeleteButton = document.getElementById("executeDeleteProject");
const dailyPanelToggle = document.getElementById("toggleDailyAutomationPanel");
const dailyPanelBody = document.getElementById("dailyAutomationBody");
const dailyDisclosure = document.getElementById("dailyAutomationDisclosure");
const dailyEnabled = document.getElementById("dailyAutomationEnabled");
const dailyRunBadge = document.getElementById("dailyRunBadge");
const dailySummary = document.getElementById("dailyAutomationSummary");
const dailyTargetDate = document.getElementById("dailyTargetDate");
const dailyStartButton = document.getElementById("startDailyRun");
const dailyRefreshButton = document.getElementById("refreshDailyStatus");
const dailyPresenterShape = document.getElementById("dailyPresenterShape");
const THEME_KEY = "backlot.theme";
let currentTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
let deleteTarget = null;
let deletePreview = null;
let dailyStatusTimer = null;

const DAILY_STAGE_LABELS = {
  research: "检索新闻", script: "脚本质检", project: "创建项目", voice: "双角色配音",
  avatar: "数字人生成", align: "切割排序", visuals: "补全画面", compose: "全片预览", review_ready: "等待审核",
};

function localYesterday() {
  const value = new Date();
  value.setDate(value.getDate() - 1);
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
}

function setDailyExpanded(expanded) {
  dailyPanelBody.hidden = !expanded;
  dailyPanelToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  dailyDisclosure.textContent = expanded ? "收起" : "展开";
}

function runStatusLabel(status) {
  return {
    queued: "等待开始", running: "正在运行", failed: "需要处理", review_ready: "等待审核",
    cancelled: "已取消",
  }[status] || "尚无任务";
}

function renderDailyBillingAudit(run) {
  const container = document.getElementById("dailyBillingAudit");
  const audit = run?.billing_audit || {};
  const rows = Object.entries(audit);
  if (!rows.length) {
    container.replaceChildren(el("strong", {}, "RunningHub 计费核验"), el("p", {}, "尚无已完成的数字人任务。"));
    return;
  }
  const labels = { yaya: "雅雅", mengmeng: "檬檬" };
  container.replaceChildren(
    el("strong", {}, "RunningHub 计费核验"),
    ...rows.map(([role, item]) => {
      const usage = item?.billing?.provider_usage || {};
      const instance = item?.observed_instance || "等待平台账单";
      const seconds = Number(usage.task_cost_seconds || 0);
      const rate = item?.billing?.observed_hourly_rate_cny;
      const warning = item?.billing_blocker || (["standard_24gb", "plus_48gb"].includes(instance)
        ? "检测到非 Lite 费率：自动化不会继续提交未完成角色。"
        : "");
      return el("div", { class: `daily-billing-row${warning ? " warning" : ""}` },
        el("b", {}, labels[role] || role),
        el("span", {}, `请求：${item?.requested_instance === "auto_lite" ? "自动 Lite" : (item?.requested_instance || "—")}`),
        el("span", {}, `账单推断：${instance}`),
        el("span", {}, seconds ? `运行：${Math.round(seconds)} 秒` : "运行时长未回传"),
        el("span", {}, rate ? `费率：¥${Number(rate).toFixed(2)}/小时` : "费率待核验"),
        warning ? el("em", {}, warning) : null,
      );
    }),
  );
}

function renderDailyBillingSafety(safety) {
  const container = document.getElementById("dailyBillingSafety");
  if (!container) return;
  const eligible = safety?.auto_schedule_eligible === true;
  container.className = `daily-billing-safety ${eligible ? "ok" : "warning"}`;
  container.replaceChildren(
    el("strong", {}, eligible ? "无人值守：Lite 请求合同已启用" : "无人值守：暂不允许开启"),
    el("span", {}, safety?.message || "企业 Lite 必须先以实际账单核验，自动调度不等于保证 Lite。"),
    safety?.state === "lite_verification_required"
      ? el("small", {}, "请使用独立的最小探测任务核验费率；正式角色不会用于算力试错。")
      : null,
  );
}

function renderDailyStatus(state) {
  const config = state?.config || {};
  const effective = state?.effective_state || {};
  const run = state?.active_run || state?.latest_run;
  dailyEnabled.checked = config.enabled === true;
  dailyEnabled.disabled = false;
  dailyEnabled.title = "只有实际账单已核验为企业 Lite 时，凌晨自动生产才允许提交数字人。";
  dailyPresenterShape.value = config.avatar?.shape || "circle";
  const actuallyEnabled = effective.effective_enabled === true;
  const conflicted = effective.conflict === true;
  dailyRunBadge.textContent = conflicted ? "调度未生效" : (actuallyEnabled ? "已启用" : "未启用");
  dailyRunBadge.classList.toggle("idle", !actuallyEnabled);
  dailyRunBadge.classList.toggle("warning", conflicted);
  dailySummary.textContent = conflicted
    ? "配置与 Windows 计划任务不一致；请进入自动生产中心修复。"
    : actuallyEnabled
    ? `下次 ${state.next_run?.starts_at?.replace("T", " ") || "03:00"}，生成 ${state.next_run?.target_date || "上一自然日"}`
    : "当前关闭；可立即试跑，确认后再启用每天 03:00 自动生成。";
  document.getElementById("dailyNextRun").textContent = state.next_run?.starts_at?.replace("T", " ") || "—";
  document.getElementById("dailyNextTarget").textContent = state.next_run?.target_date || "—";
  const latestProject = document.getElementById("dailyLatestProject");
  if (run?.project_id) {
    latestProject.textContent = run.project_id;
    latestProject.href = `/p/${encodeURIComponent(run.project_id)}`;
  } else {
    latestProject.textContent = "尚无";
    latestProject.removeAttribute("href");
  }
  const spent = Number(run?.budget?.spent || 0);
  const reserved = Number(run?.budget?.reserved || 0);
  const limit = Number(run?.budget?.limit ?? config.max_budget_cny ?? 5);
  document.getElementById("dailyBudget").textContent = `¥${(spent + reserved).toFixed(2)} / ¥${limit.toFixed(2)}`;
  const stages = run?.stages || {};
  const succeeded = Object.values(stages).filter((item) => ["succeeded", "skipped"].includes(item?.status)).length;
  document.getElementById("dailyProgress").value = succeeded;
  document.getElementById("dailyProgressCount").textContent = `${succeeded} / 9`;
  document.getElementById("dailyProgressTitle").textContent = run
    ? `${run.target_date} · ${runStatusLabel(run.status)}`
    : "尚无运行任务";
  const current = run?.current_stage ? stages[run.current_stage] : null;
  const live = run?.live_progress;
  const liveMessage = live?.kind === "visual_batch" && Number(live.total || 0) > 0
    ? `正在逐格补全主体画面：${Number(live.completed || 0)} / ${Number(live.total || 0)}，失败 ${Number(live.failed || 0)} 格。已完成结果会即时保存。`
    : "";
  document.getElementById("dailyProgressMessage").textContent = current?.error
    ? `失败：${current.error}`
    : (liveMessage || current?.message || (run?.status === "review_ready" ? "全片预览已就绪，请进入项目审核。" : "开启自动化，或选择已结束的日期立即试跑。"));
  const list = document.getElementById("dailyStageList");
  const labels = { pending: "待执行", running: "进行中", succeeded: "完成", failed: "失败", skipped: "跳过" };
  list.replaceChildren(...Object.entries(stages).map(([name, item]) => el("span", {
    class: `daily-stage ${item.status || "pending"}`,
    title: item.error || item.message || "",
  }, `${DAILY_STAGE_LABELS[name] || name} · ${labels[item.status] || item.status}`)));
  const busy = run?.status === "queued" || run?.status === "running";
  dailyStartButton.disabled = busy;
  dailyStartButton.textContent = busy ? "正在执行一条龙任务…" : "立即生成这一天";
  renderDailyBillingAudit(run);
  renderDailyBillingSafety(state?.billing_safety);
  if (busy) scheduleDailyRefresh(3000);
}

function scheduleDailyRefresh(delay = 10000) {
  clearTimeout(dailyStatusTimer);
  dailyStatusTimer = setTimeout(() => refreshDailyStatus().catch(console.error), delay);
}

async function refreshDailyStatus() {
  dailyRefreshButton.disabled = true;
  try {
    renderDailyStatus(await getJSON("/api/daily-automation/status"));
  } finally {
    dailyRefreshButton.disabled = false;
  }
}

async function updateDailyEnabled() {
  const requested = dailyEnabled.checked;
  dailyEnabled.disabled = true;
  dailySummary.textContent = requested ? "正在创建并启用 Windows 每日计划任务…" : "正在停用每日计划任务…";
  try {
    const response = await fetch("/api/daily-automation/config", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: requested }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "自动化开关保存失败");
    renderDailyStatus(body);
    setLibraryMessage(requested ? "每日科技快报已启用：每天 03:00 自动生成上一自然日内容。" : "每日科技快报已停用，历史运行与项目不会删除。", false);
  } catch (error) {
    dailyEnabled.checked = !requested;
    setLibraryMessage(error.message || "自动化开关保存失败", true);
    await refreshDailyStatus().catch(console.error);
  }
}

async function updateDailyPresenterShape() {
  dailyPresenterShape.disabled = true;
  try {
    const response = await fetch("/api/daily-automation/config", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ avatar: { shape: dailyPresenterShape.value } }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "数字人外框保存失败");
    renderDailyStatus(body);
    setLibraryMessage("每日自动化的数字人外框已保存；下一条新视频会使用该设置。", false);
  } catch (error) {
    setLibraryMessage(error.message || "数字人外框保存失败", true);
    await refreshDailyStatus().catch(console.error);
  } finally {
    dailyPresenterShape.disabled = false;
  }
}

async function startDailyRun() {
  const target = dailyTargetDate.value || localYesterday();
  dailyStartButton.disabled = true;
  dailyStartButton.textContent = "正在启动…";
  document.getElementById("dailyProgressTitle").textContent = `${target} · 正在启动`;
  document.getElementById("dailyProgressMessage").textContent = "正在建立日期运行合同，随后检索并核验新闻。";
  try {
    const response = await fetch("/api/daily-automation/runs", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target_date: target }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "每日任务启动失败");
    setLibraryMessage(`${target} 科技快报已开始。可关闭此面板，后台任务会继续运行。`);
    await refreshDailyStatus();
  } catch (error) {
    setLibraryMessage(error.message || "每日任务启动失败", true);
    dailyStartButton.disabled = false;
    dailyStartButton.textContent = "立即生成这一天";
  }
}

function applyTheme(theme) {
  currentTheme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = currentTheme;
  localStorage.setItem(THEME_KEY, currentTheme);
}

function renderThemeToggle() {
  const next = currentTheme === "light" ? "dark" : "light";
  return el("button", {
    class: "theme-toggle",
    type: "button",
    title: `切换至${next === "light" ? "浅色" : "深色"}主题`,
    "aria-label": `切换至${next === "light" ? "浅色" : "深色"}主题`,
    "aria-pressed": currentTheme === "light" ? "true" : "false",
    onclick: () => {
      applyTheme(next);
      document.querySelector(".theme-toggle").replaceWith(renderThemeToggle());
    },
  }, el("span", { class: "theme-toggle-icon", "aria-hidden": "true" }, currentTheme === "light" ? "☀" : "☾"));
}

function pipelineLabel(pipeline) {
  return {
    "animated-explainer": "无数字人口播",
    "avatar-spokesperson": "有数字人口播",
    hybrid: "混合素材",
    "screen-demo": "屏幕演示",
    cinematic: "电影感叙事",
  }[pipeline] || (pipeline === "unknown" ? "未指定流程" : pipeline || "未指定流程");
}

function stageLabel(stage) {
  return {
    research: "调研", proposal: "方案", idea: "创意", script: "脚本",
    scene_plan: "分镜", assets: "素材", edit: "剪辑决策", compose: "合成", publish: "交付",
  }[stage] || stage;
}

function projectRowState(project) {
  const states = project.stage_states || [];
  const active = states.find((state) => state.status === "in_progress");
  const failed = states.find((state) => state.status === "failed");
  const completed = states.filter((state) => state.status === "completed").length;

  if (project.awaiting_human) return { tone: "awaiting", label: "等待审核" };
  if (failed) return { tone: "needs-attention", label: `需处理 · ${stageLabel(failed.name)}` };
  if (active) return { tone: "in-progress", label: `进行中 · ${stageLabel(active.name)}` };
  if (states.length && completed === states.length) return { tone: "completed", label: "已完成" };
  if (completed) return { tone: "in-progress", label: `制作中 · ${completed}/${states.length}` };
  if (project.live) return { tone: "in-progress", label: "制作中" };
  return { tone: "draft", label: "草案" };
}

function projectRowFacts(project) {
  return [
    pipelineLabel(project.pipeline_type),
    project.scene_count ? `${project.scene_count} 个场景` : null,
    project.render_count ? `${project.render_count} 个成片` : null,
  ].filter(Boolean).join(" · ");
}

function card(project) {
  const state = projectRowState(project);

  const staticSuffix = new URLSearchParams(location.search).has("static") ? "?static=1" : "";
  const openProject = el("a", { class: "lib-card-link lib-project-row-link", href: `/p/${project.project_id}${staticSuffix}` },
    el("span", { class: "project-row-key" }, `#${project.project_id}`),
    el("div", { class: "project-row-copy" },
      el("h3", {}, project.title || project.project_id),
      el("p", {}, projectRowFacts(project)),
    ),
    el("span", { class: `project-row-state ${state.tone}` }, state.label),
    el("time", { class: "project-row-time", dateTime: new Date(Number(project.last_activity || 0) * 1000).toISOString() }, fmtAgo(project.last_activity)),
    el("span", { class: "project-row-enter" }, "进入工作区"),
  );
  const manage = el("button", {
    class: "project-manage",
    type: "button",
    "aria-label": `管理项目：${project.title || project.project_id}`,
    title: "管理项目",
    onclick: () => showDeleteDialog(project),
  }, "管理");
  return el("article", { class: `lib-card lib-project-row${project.live ? " live-card" : ""}` }, openProject, manage);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[index]}`;
}

function setLibraryMessage(message, isError = false) {
  libraryMessage.textContent = message;
  libraryMessage.classList.toggle("visible", Boolean(message));
  libraryMessage.classList.toggle("error", isError);
}

function setDeleteMessage(message, isError = false) {
  deleteMessage.textContent = message;
  deleteMessage.classList.toggle("visible", Boolean(message));
  deleteMessage.classList.toggle("error", isError);
}

function resetDeleteFlow() {
  deleteTarget = null;
  deletePreview = null;
  deleteButton.disabled = true;
  deleteButton.textContent = "永久删除项目";
  executeDeleteButton.disabled = false;
  executeDeleteButton.textContent = "确认永久删除";
  setDeleteMessage("");
  finalDeleteMessage.textContent = "";
  finalDeleteMessage.classList.remove("visible", "error");
}

function closeDeleteFlow() {
  if (deleteDialog.open) deleteDialog.close();
  if (finalDeleteDialog.open) finalDeleteDialog.close();
  resetDeleteFlow();
}

function showFinalDeleteDialog() {
  if (!deleteTarget || !deletePreview?.can_delete) return;
  deleteDialog.close();
  finalDeleteName.textContent = `“${deletePreview.title}”`;
  finalDeleteMessage.textContent = "";
  finalDeleteMessage.classList.remove("visible", "error");
  finalDeleteDialog.showModal();
  executeDeleteButton.focus();
}

function returnToDeletePreview() {
  if (finalDeleteDialog.open) finalDeleteDialog.close();
  if (deleteTarget && deletePreview) deleteDialog.showModal();
}

function renderDeletePreview(preview) {
  const storage = preview.storage || {};
  const categories = storage.categories || {};
  const included = el("ul", { class: "delete-scope-list" }, ...(preview.scope || []).map((item) => el("li", {}, item)));
  const preserved = el("p", { class: "delete-preserved" }, `不会删除：${(preview.preserved || []).join("、")}`);
  const taskWarning = preview.active_tasks?.length
    ? el("div", { class: "delete-task-warning" },
      el("strong", {}, "当前不能删除"),
      el("span", {}, `仍有 ${preview.active_tasks.length} 个任务在运行，请等待任务结束后重试。`),
    ) : null;
  const summaryChildren = [
    el("div", { class: "delete-project-identity" },
      el("strong", {}, preview.title),
      el("code", {}, preview.project_id),
    ),
    el("div", { class: "delete-storage-facts" },
      el("span", {}, `${storage.file_count || 0} 个文件`),
      el("span", {}, formatBytes(storage.total_bytes)),
      el("span", {}, `素材 ${formatBytes(categories.assets)}`),
      el("span", {}, `成片 ${formatBytes(categories.renders)}`),
    ),
    el("strong", { class: "delete-scope-title" }, "将永久删除"),
    included,
    preserved,
  ];
  if (taskWarning) summaryChildren.push(taskWarning);
  deleteSummary.replaceChildren(...summaryChildren);
  deleteButton.disabled = !preview.can_delete;
}

async function showDeleteDialog(project) {
  deleteTarget = project;
  deletePreview = null;
  deleteSummary.textContent = "正在检查项目数据…";
  deleteButton.disabled = true;
  setDeleteMessage("");
  deleteDialog.showModal();
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(project.project_id)}/deletion-preview`);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "无法检查项目数据，请稍后重试。");
    if (!deleteTarget || deleteTarget.project_id !== body.project_id) return;
    deletePreview = body;
    renderDeletePreview(body);
    if (body.can_delete) deleteButton.focus();
  } catch (error) {
    deleteSummary.textContent = "项目数据检查失败。";
    setDeleteMessage(error.message || "无法检查项目数据，请稍后重试。", true);
  }
}

function fallbackId() {
  const timestamp = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  const entropy = Date.now().toString(36).slice(-5);
  return `video-${timestamp}-${entropy}`;
}

function showDialog() {
  form.reset();
  formMessage.textContent = "";
  formMessage.classList.remove("visible", "error");
  idInput.value = fallbackId();
  dialog.showModal();
  titleInput.focus();
}

function closeDialog() {
  dialog.close();
}

function setFormMessage(message, isError = false) {
  formMessage.textContent = message;
  formMessage.classList.toggle("visible", Boolean(message));
  formMessage.classList.toggle("error", isError);
}

function setFieldValidation(field) {
  let message = "";
  if (field.validity.valueMissing) message = "请输入项目名称";
  field.setCustomValidity(message);
}

form.addEventListener("input", (event) => {
  if (event.target instanceof HTMLInputElement) event.target.setCustomValidity("");
});
form.addEventListener("invalid", (event) => {
  if (event.target instanceof HTMLInputElement) setFieldValidation(event.target);
}, true);

async function render() {
  const projects = await getJSON("/api/projects");
  document.getElementById("count").textContent = `${projects.length} 个项目`;
  const liveCount = projects.filter((project) => project.live).length;
  const badge = document.getElementById("liveBadge");
  badge.classList.toggle("idle", liveCount === 0);
  document.getElementById("liveText").textContent = liveCount ? `${liveCount} 个进行中` : "暂无任务";
  grid.innerHTML = "";
  document.getElementById("empty").style.display = projects.length ? "none" : "block";
  for (const project of projects) grid.append(card(project));
}

dailyPanelToggle.addEventListener("click", () => setDailyExpanded(dailyPanelBody.hidden));
dailyEnabled.addEventListener("change", updateDailyEnabled);
dailyStartButton.addEventListener("click", startDailyRun);
dailyRefreshButton.addEventListener("click", () => refreshDailyStatus().catch((error) => setLibraryMessage(error.message || "自动化状态刷新失败", true)));
dailyPresenterShape.addEventListener("change", updateDailyPresenterShape);
createButton.addEventListener("click", showDialog);
closeButton.addEventListener("click", closeDialog);
cancelButton.addEventListener("click", closeDialog);
document.getElementById("closeDeleteProjectDialog").addEventListener("click", closeDeleteFlow);
document.getElementById("cancelDeleteProject").addEventListener("click", closeDeleteFlow);
document.getElementById("closeFinalDeleteProjectDialog").addEventListener("click", returnToDeletePreview);
document.getElementById("backFromFinalDeleteProject").addEventListener("click", returnToDeletePreview);
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) closeDialog();
});
deleteDialog.addEventListener("click", (event) => {
  if (event.target === deleteDialog) closeDeleteFlow();
});
finalDeleteDialog.addEventListener("click", (event) => {
  if (event.target === finalDeleteDialog) returnToDeletePreview();
});

deleteForm.addEventListener("submit", (event) => {
  event.preventDefault();
  showFinalDeleteDialog();
});

finalDeleteForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!deleteTarget || !deletePreview?.can_delete) return;
  executeDeleteButton.disabled = true;
  executeDeleteButton.textContent = "正在删除…";
  finalDeleteMessage.textContent = "正在删除项目目录及相关数据，请不要关闭页面。";
  finalDeleteMessage.classList.add("visible");
  finalDeleteMessage.classList.remove("error");
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(deleteTarget.project_id)}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        confirm_project_id: deleteTarget.project_id,
        confirmation: "DELETE_PROJECT",
        permanent: true,
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "项目删除失败，请稍后重试。");
    const deletedTitle = deleteTarget.title || deleteTarget.project_id;
    const deletedBytes = formatBytes(body.deleted_storage?.total_bytes || 0);
    closeDeleteFlow();
    setLibraryMessage(`已永久删除“${deletedTitle}”及 ${deletedBytes} 项目数据。`);
    await render();
  } catch (error) {
    finalDeleteMessage.textContent = error.message || "项目删除失败，请稍后重试。";
    finalDeleteMessage.classList.add("visible", "error");
    executeDeleteButton.textContent = "确认永久删除";
    executeDeleteButton.disabled = false;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setFieldValidation(titleInput);
  if (!form.reportValidity()) return;

  const data = Object.fromEntries(new FormData(form));
  data.project_id = data.project_id.trim().toLowerCase();
  data.title = data.title.trim();
  setFormMessage("正在创建项目…");
  submitButton.disabled = true;
  try {
    const response = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = response.status === 405
        ? "当前页面连接的是旧版服务，请刷新页面后重试。"
        : (body.detail || "创建项目失败，请稍后再试。");
      throw new Error(typeof detail === "string" ? detail : "创建项目失败，请稍后再试。");
    }
    location.assign(`/p/${encodeURIComponent(body.project_id)}`);
  } catch (error) {
    setFormMessage(error.message || "创建项目失败，请稍后再试。", true);
    submitButton.disabled = false;
  }
});

applyTheme(currentTheme);
dailyTargetDate.value = localYesterday();
document.getElementById("liveBadge").before(renderThemeToggle());
refreshDailyStatus().catch((error) => {
  console.error(error);
  dailySummary.textContent = "自动化状态暂时无法读取，请点击展开后重试。";
});
render().catch((error) => {
  console.error(error);
  setFormMessage("项目库暂时无法加载，请刷新后重试。", true);
});
if (!new URLSearchParams(location.search).has("static")) {
  subscribe("/api/library/events", () => render().catch(console.error));
}
