import { el, fmtDuration } from "/ui/lib.js";

const app = document.getElementById("app");
const toastNode = document.getElementById("toast");
const returnPath = new URLSearchParams(location.search).get("return");
let center = null;
let selectedProfileId = null;
let toastTimer = null;
let pollTimer = null;

const statusText = {
  available: "服务可用",
  unavailable: "服务不可用",
  idle: "尚未开始",
  generating: "正在生成",
  completed: "已完成",
  failed: "需要处理",
};

function showToast(message, failed = false) {
  clearTimeout(toastTimer);
  toastNode.textContent = message;
  toastNode.className = `toast show${failed ? " failed" : ""}`;
  toastTimer = setTimeout(() => { toastNode.className = "toast"; }, 4200);
}

function button(label, style, handler, disabled = false) {
  return el("button", { class: `button ${style || ""}`, type: "button", disabled: disabled ? "" : null, onclick: handler }, label);
}

async function request(path, options = {}) {
  const response = await fetch(`/api/audio-center${path}`, {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "请求未能完成，请稍后重试");
  }
  return response.json();
}

function profileCard(profile) {
  const isSelected = profile.id === selectedProfileId;
  const isDefault = center.default_voice && profile.id === center.default_voice.id;
  const card = el("article", { class: `voice-card${isSelected ? " selected" : ""}` },
    el("div", { class: "voice-card-top" },
      el("div", {}, el("h3", {}, profile.name), el("p", {}, profile.description || "Haike Video 本地音色")),
      isDefault ? el("span", { class: "badge default" }, "通用默认") : el("span", { class: "badge" }, profile.voice_type === "cloned" ? "克隆音色" : "预设音色"),
    ),
    el("dl", { class: "voice-facts" },
      el("div", {}, el("dt", {}, "引擎"), el("dd", {}, profile.default_engine || "自动")),
      el("div", {}, el("dt", {}, "语言"), el("dd", {}, profile.language || "中文")),
    ),
    el("p", { class: "profile-id" }, `音色编号：${profile.id}`),
    el("div", { class: "card-actions" },
      button(isSelected ? "已选作试听" : "选择试听", isSelected ? "quiet" : "", () => { selectedProfileId = profile.id; render(); }),
      !isDefault ? button("设为通用默认", "quiet", () => setDefault(profile.id)) : null,
    ),
  );
  return card;
}

async function setDefault(profileId) {
  try {
    center = await request("/default-voice", { method: "PUT", body: { profile_id: profileId } });
    selectedProfileId = profileId;
    render();
    showToast("已设为通用默认音色；之后新项目会引用这一选择。");
  } catch (error) { showToast(error.message || "设置默认音色失败", true); }
}

async function generatePreview() {
  const text = document.getElementById("previewText").value.trim();
  try {
    center = await request("/previews/jobs", { method: "POST", body: { text, profile_id: selectedProfileId } });
    render();
    showToast("试听已进入本机队列，生成期间会自动刷新状态。");
    schedulePoll();
  } catch (error) { showToast(error.message || "试听任务未能启动", true); }
}

function previewRow(preview) {
  return el("article", { class: "preview-row" },
    el("div", { class: "preview-copy" },
      el("div", { class: "preview-title" }, el("strong", {}, preview.profile_name), el("span", {}, fmtDuration(preview.duration_seconds))),
      el("p", {}, preview.text),
    ),
    el("audio", { controls: "", preload: "metadata", src: `/audio-preview/${encodeURIComponent(preview.id)}` }),
  );
}

function render() {
  if (!center) return;
  const provider = center.provider || {};
  const selected = (center.profiles || []).find((profile) => profile.id === selectedProfileId) || center.default_voice;
  if (selected && !selectedProfileId) selectedProfileId = selected.id;
  const job = center.preview_job || { status: "idle" };
  const isGenerating = job.status === "generating";
  const backHref = returnPath && returnPath.startsWith("/p/") ? returnPath : "/";
  const textarea = el("textarea", { id: "previewText", maxlength: "500", placeholder: "例如：今天的内容，用三个步骤帮你建立一套高效阅读的方法。" });
  textarea.value = job.status === "generating" ? job.text || "" : "这是一段通用配音试听。语速自然、咬字清晰，适合中文知识类短视频旁白。";

  const service = el("section", { class: "service-strip" },
    el("div", {}, el("p", { class: "eyebrow" }, "本机服务"), el("h2", {}, provider.name || "Haike Video 本地配音"), el("p", {}, provider.detail || "")),
    el("div", { class: "service-state" }, el("span", { class: `status ${provider.status || "unavailable"}` }, statusText[provider.status] || "未知"), el("span", {}, "不会把音色写死到某一个视频里")),
  );
  const defaultCard = el("section", { class: "default-card" },
    el("div", {}, el("p", { class: "eyebrow" }, "软件通用默认"), el("h2", {}, center.default_voice ? center.default_voice.name : "尚未选择音色"), el("p", {}, center.default_voice ? `${center.default_voice.description || "可用于所有新项目"}` : "请从下方选择一个可用音色。")),
    center.default_voice ? el("div", { class: "default-meta" }, el("span", {}, `编号 ${center.default_voice.id}`), el("span", {}, `引擎 ${center.default_voice.default_engine || "自动"}`)) : null,
  );
  const audition = el("section", { class: "panel audition" },
    el("div", { class: "panel-head" }, el("div", {}, el("p", { class: "eyebrow" }, "独立试听"), el("h2", {}, "先听音色，再用到视频"), el("p", {}, selected ? `当前试听：${selected.name}。此音频属于软件级试听，不写入任何项目。` : "请先选择一个音色。")), isGenerating ? el("span", { class: "status generating" }, "正在生成") : null),
    el("div", { class: "panel-body" }, textarea, el("div", { class: "audition-actions" }, button(isGenerating ? "正在生成试听…" : "生成试听", "primary", generatePreview, isGenerating || provider.status !== "available"), selected && center.default_voice && selected.id !== center.default_voice.id ? button("将当前试听音色设为默认", "quiet", () => setDefault(selected.id), isGenerating) : null)),
    job.status === "failed" ? el("p", { class: "error" }, job.error || "试听生成失败") : null,
    job.status === "generating" ? el("p", { class: "hint" }, "Haike Video 正在本机生成音频，页面会自动刷新。") : null,
  );
  const voiceGrid = el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("p", { class: "eyebrow" }, "音色目录"), el("h2", {}, "选择一个真实可用的本地音色"), el("p", {}, "预设与克隆音色都由 Haike Video 私有目录管理；项目只保存音色编号。"))), el("div", { class: "voice-grid" }, (center.profiles || []).map(profileCard)));
  const previews = el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("p", { class: "eyebrow" }, "近期试听"), el("h2", {}, "可重复试听的通用音频"))), (center.previews || []).length ? el("div", { class: "preview-list" }, center.previews.map(previewRow)) : el("div", { class: "empty" }, "还没有试听记录。先选一个音色，输入一句文案即可。"));

  app.replaceChildren(
    el("header", { class: "topbar" }, el("a", { class: "back-link", href: backHref }, returnPath ? "返回当前项目" : "返回项目库"), el("div", { class: "title" }, el("p", { class: "eyebrow" }, "Haike Video / 软件级功能"), el("h1", {}, "通用配音中心"), el("p", {}, "管理全局默认音色与独立试听；项目只生成、审核并引用自己的旁白文件。"))),
    el("div", { class: "layout" }, el("div", { class: "intro-column" }, service, defaultCard), audition),
    voiceGrid,
    previews,
  );
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (center && center.preview_job && center.preview_job.status === "generating") {
    pollTimer = setTimeout(() => refresh(false), 1800);
  }
}

async function refresh(showErrors = true) {
  try {
    center = await request("");
    if (!(center.profiles || []).some((profile) => profile.id === selectedProfileId)) selectedProfileId = center.default_voice && center.default_voice.id;
    render();
    schedulePoll();
  } catch (error) {
    if (showErrors) showToast(error.message || "配音中心暂时无法加载", true);
  }
}

refresh();
