import { el, fmtDuration } from "/ui/lib.js";

const app = document.getElementById("app");
const toastNode = document.getElementById("toast");
const returnPath = new URLSearchParams(location.search).get("return");
let center = null;
let tencent = null;
let avatarRoles = [];
let selectedProfileId = null;
let toastTimer = null;
let pollTimer = null;
let tencentBusy = null;

const CLOUD_PROVIDERS = ["doubao", "tencent"];
const isCloud = (profile) => !!profile && CLOUD_PROVIDERS.includes(profile.provider_id);

// Tencent Cloud only plays its discrete Speed anchors, so the UI has to show -
// and post - an achievable rate instead of a value the provider never renders.
const rateOptions = (profile) => (
  Array.isArray(profile && profile.speech_rate_options) && profile.speech_rate_options.length
    ? profile.speech_rate_options.map(Number)
    : null
);
const effectiveRate = (profile) => {
  const rate = Number((profile && profile.speech_rate) || 1.25);
  const options = rateOptions(profile);
  if (!options) return rate;
  return options.reduce((best, item) => (Math.abs(item - rate) < Math.abs(best - rate) ? item : best), options[0]);
};

// User-added voices exist for both cloud providers; the form switches labels,
// placeholder and rate control together with the chosen service.
const CUSTOM_CLOUD_PROVIDER_CHOICES = [
  {
    id: "doubao",
    label: "豆包云端配音",
    idLabel: "豆包音色 ID",
    placeholder: "粘贴豆包音色 ID",
    idAriaLabel: "新增豆包音色 ID",
    action: "添加豆包音色",
    hint: "普通 ID 默认使用 Speech 2.0；以 S_ 开头的复刻 ID 自动使用 ICL 资源。",
  },
  {
    id: "tencent",
    label: "腾讯云云端配音",
    idLabel: "腾讯云音色 ID",
    placeholder: "例如 502001 / 601010",
    idAriaLabel: "新增腾讯云音色 ID",
    action: "添加腾讯云音色",
    hint: "填腾讯云 VoiceType 纯数字 ID（如 502003、601010）；腾讯只支持固定语速档位。",
  },
];
// Fallback for the add form before any Tencent profile is on screen; the real
// list is delivered per profile as speech_rate_options.
const FALLBACK_TENCENT_RATE_OPTIONS = [0.6, 0.8, 1.0, 1.2, 1.5, 1.7, 2.0];

const tencentRateOptions = () => (
  (center && (center.profiles || []).map(rateOptions).find((options) => options))
  || FALLBACK_TENCENT_RATE_OPTIONS
);

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
  toastTimer = setTimeout(() => { toastNode.className = "toast"; }, 5200);
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

async function tencentRequest(path, options = {}) {
  const response = await fetch(`/api/tencent${path}`, {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "腾讯云请求未能完成，请稍后重试");
  }
  return response.json();
}

async function roleRequest(path, options = {}) {
  const response = await fetch(`/api/avatar-roles${path}`, {
    method: options.method || "GET",
    headers: options.headers || { "Content-Type": "application/json" },
    body: options.body instanceof Blob ? options.body : (options.body ? JSON.stringify(options.body) : undefined),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "角色档案请求未能完成，请稍后重试");
  }
  return response.json();
}

function roleForProfile(profile) {
  return avatarRoles.find((role) => ((role.voice_binding || {}).profile_id === profile.id)) || null;
}

function rolePortraitURL(role) {
  const reference = (role.references || []).find((item) => item.slot === "front");
  if (!reference || !reference.path) return "";
  return `/api/avatar-roles/${encodeURIComponent(role.role_id)}/media/${reference.path.split("/").map(encodeURIComponent).join("/")}`;
}

async function bindRole(profile, roleId) {
  try {
    await roleRequest(`/${encodeURIComponent(roleId)}/voice-binding`, { method: "PUT", body: { profile_id: profile.id } });
    showToast(`已将“${profile.name}”关联到角色档案。`);
    await refresh();
  } catch (error) { showToast(error.message || "关联角色失败", true); }
}

async function unbindRole(role) {
  try {
    await roleRequest(`/${encodeURIComponent(role.role_id)}/voice-binding`, { method: "PUT", body: { profile_id: "" } });
    showToast("已解除角色与音色的关联。");
    await refresh();
  } catch (error) { showToast(error.message || "解除关联失败", true); }
}

async function createRoleForProfile(profile, name) {
  try {
    const role = await roleRequest("", { method: "POST", body: { name, license: "仅限本人项目使用" } });
    await roleRequest(`/${encodeURIComponent(role.role_id)}/voice-binding`, { method: "PUT", body: { profile_id: profile.id } });
    showToast(`已新建角色“${name}”并关联音色；现在上传正面出镜图。`);
    await refresh();
  } catch (error) { showToast(error.message || "新建角色失败", true); }
}

async function uploadRolePortrait(role, file) {
  try {
    await roleRequest(`/${encodeURIComponent(role.role_id)}/references/front/file?filename=${encodeURIComponent(file.name)}`, {
      method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file,
    });
    showToast("正面出镜图已保存；下一次项目预检会复制并冻结到项目内。");
    await refresh();
  } catch (error) { showToast(error.message || "上传正面出镜图失败", true); }
}

function profileCard(profile) {
  const isSelected = profile.id === selectedProfileId;
  const isDefault = center.default_voice && profile.id === center.default_voice.id;
  // One preview runs at a time; disabling the others avoids a refusal that
  // reads like "this voice cannot be generated".
  const previewBusy = Boolean(center.preview_job && center.preview_job.status === "generating");
  const card = el("article", { class: `voice-card${isSelected ? " selected" : ""}` },
    el("div", { class: "voice-card-top" },
      el("div", {}, el("h3", {}, profile.name), el("p", {}, profile.description || profile.provider_name || "Haike Video 音色")),
      isDefault ? el("span", { class: "badge default" }, "通用默认") : el("span", { class: "badge" }, profile.voice_type.startsWith("cloud") ? "云端音色" : (profile.voice_type === "cloned" ? "克隆音色" : "预设音色")),
    ),
    el("dl", { class: "voice-facts" },
      el("div", {}, el("dt", {}, "引擎"), el("dd", {}, profile.default_engine || "自动")),
      el("div", {}, el("dt", {}, "语言"), el("dd", {}, profile.language || "中文")),
      el("div", {}, el("dt", {}, "来源"), el("dd", {}, profile.provider_name || "本地")),
      profile.scene ? el("div", {}, el("dt", {}, "场景"), el("dd", {}, profile.scene)) : null,
      el("div", {}, el("dt", {}, "状态"), el("dd", {}, profile.available === false ? "尚未配置" : "可用")),
      isCloud(profile) ? el("div", {}, el("dt", {}, "语速"), el("dd", {}, `${effectiveRate(profile).toFixed(2)}×`)) : null,
    ),
    el("p", { class: "profile-id" }, `音色编号：${profile.id}`),
    cloudRateControl(profile),
    el("div", { class: "card-actions" },
      button(isSelected ? "已选作试听" : "选择试听", isSelected ? "quiet" : "", () => { selectedProfileId = profile.id; render(); }, profile.available === false),
      isCloud(profile) ? button(previewBusy ? "试听生成中…" : "立即试听（按字数计费）", "quiet", () => generatePreview(profile.id), profile.available === false || previewBusy) : null,
      !isDefault ? button("设为通用默认", "quiet", () => setDefault(profile.id), profile.available === false) : null,
      profile.is_custom_cloud_voice ? button("移除此音色", "quiet", () => removeCustomCloudVoice(profile.id)) : null,
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

async function generatePreview(profileId = selectedProfileId) {
  const text = document.getElementById("previewText").value.trim();
  try {
    const selected = (center.profiles || []).find((profile) => profile.id === profileId);
    const playbackRate = isCloud(selected) ? effectiveRate(selected) : 1;
    center = await request("/previews/jobs", { method: "POST", body: { text, profile_id: profileId, playback_rate: playbackRate } });
    selectedProfileId = profileId;
    render();
    showToast("试听已进入配音队列，生成期间会自动刷新状态。");
    schedulePoll();
  } catch (error) { showToast(error.message || "试听任务未能启动", true); }
}

function previewRow(preview) {
  return el("article", { class: "preview-row" },
    el("div", { class: "preview-copy" },
      el("div", { class: "preview-title" }, el("strong", {}, preview.profile_name), el("span", {}, `${fmtDuration(preview.duration_seconds)} · ${Number(preview.playback_rate || 1).toFixed(2)}×`)),
      el("p", {}, preview.text),
    ),
    el("audio", { controls: "", preload: "metadata", src: `/audio-preview/${encodeURIComponent(preview.id)}` }),
  );
}

async function setCloudVoicePlaybackRate(profileId, rate) {
  try {
    center = await request(`/cloud-voices/${encodeURIComponent(profileId)}/playback-rate`, { method: "PUT", body: { playback_rate: rate } });
    render();
    showToast(`该云端音色的语速已设为 ${Number(rate).toFixed(2)}×；已开始的任务不会变化。`);
  } catch (error) { showToast(error.message || "保存云端语速失败", true); }
}

function cloudRateControl(profile) {
  if (!isCloud(profile)) return null;
  const rate = effectiveRate(profile);
  const options = rateOptions(profile);
  const value = el("strong", { class: "rate-value" }, `${rate.toFixed(2)}×`);
  if (options) {
    // Tencent Cloud only exposes a discrete Speed scale; a free slider would
    // let the user pick 1.10x and then silently hear 1.00x.
    const select = el("select", { "aria-label": `${profile.name}的云端配音语速` },
      ...options.map((item) => el("option", {
        value: String(item),
        selected: Math.abs(item - rate) < 0.001 ? "selected" : null,
      }, `${item.toFixed(2)}×`)),
    );
    select.value = String(rate);
    select.addEventListener("change", () => setCloudVoicePlaybackRate(profile.id, Number(select.value)));
    return el("div", { class: "cloud-rate-control" },
      el("div", {}, el("span", {}, "此音色语速"), value),
      select,
      el("small", {}, `腾讯云只支持这几个固定档位：${options.map((item) => `${item.toFixed(2)}×`).join(" / ")}。1.10× 等中间值无法精确合成，会取最近档位。只影响此音色之后的新任务；音频真实时长仍决定字幕与数字人切割。`),
    );
  }
  const slider = el("input", { type: "range", min: "0.50", max: "2.00", step: "0.05", value: String(rate), "aria-label": `${profile.name}的云端配音语速` });
  slider.addEventListener("input", () => { value.textContent = `${Number(slider.value).toFixed(2)}×`; });
  slider.addEventListener("change", () => setCloudVoicePlaybackRate(profile.id, Number(slider.value)));
  return el("div", { class: "cloud-rate-control" },
    el("div", {}, el("span", {}, "此音色语速"), value),
    slider,
    el("small", {}, "默认 1.25×。只影响此音色之后的新任务；音频真实时长仍决定字幕与数字人切割。"),
  );
}

async function addCustomCloudVoice(provider, name, voiceId, playbackRate) {
  const serviceName = provider === "tencent" ? "腾讯云" : "豆包";
  try {
    center = await request("/cloud-voices", { method: "POST", body: { provider, name, voice_id: voiceId, playback_rate: playbackRate } });
    const created = (center.profiles || []).find((profile) => profile.is_custom_cloud_voice && profile.name === name);
    selectedProfileId = created ? created.id : selectedProfileId;
    render();
    showToast(`${serviceName}音色已添加并选中。点击“立即试听（按字数计费）”即可生成短试听。`);
  } catch (error) { showToast(error.message || `添加${serviceName}音色失败`, true); }
}

async function removeCustomCloudVoice(profileId) {
  try {
    center = await request(`/cloud-voices/${encodeURIComponent(profileId)}`, { method: "DELETE" });
    if (selectedProfileId === profileId) selectedProfileId = center.default_voice && center.default_voice.id;
    render();
    showToast("自定义云端音色已从本机配置移除；既有试听文件未删除。");
  } catch (error) { showToast(error.message || "移除音色失败", true); }
}

function customCloudVoicePanel() {
  const providerSelect = el("select", { "aria-label": "新增音色的配音服务" },
    ...CUSTOM_CLOUD_PROVIDER_CHOICES.map((item) => el("option", { value: item.id }, item.label)),
  );
  const name = el("input", { type: "text", maxlength: "48", placeholder: "例如：新闻女声", "aria-label": "新增音色显示名称" });
  const voiceId = el("input", { type: "text", maxlength: "256", "aria-label": "新增音色 ID" });
  const idLabel = el("span", {}, "");
  const hint = el("p", {}, "");

  const rate = el("input", { type: "range", min: "0.50", max: "2.00", step: "0.05", value: "1.25", "aria-label": "新增豆包音色语速" });
  const rateValue = el("strong", { class: "rate-value" }, "1.25×");
  rate.addEventListener("input", () => { rateValue.textContent = `${Number(rate.value).toFixed(2)}×`; });
  const doubaoRateField = el("label", { class: "cloud-voice-form-rate" }, el("span", {}, "默认语速"), rate, rateValue);

  // Tencent's Speed parameter is discrete, so the add form offers the same
  // anchor list the profile cards use instead of a free slider.
  const tencentRate = el("select", { "aria-label": "新增腾讯云音色语速" },
    ...tencentRateOptions().map((item) => el("option", { value: String(item) }, `${item.toFixed(2)}×`)),
  );
  tencentRate.value = "1.2";
  const tencentRateField = el("label", { class: "cloud-voice-form-rate" }, el("span", {}, "默认语速"), tencentRate);

  const submit = button("添加豆包音色", "primary", () => {
    const provider = providerSelect.value;
    addCustomCloudVoice(
      provider,
      name.value.trim(),
      voiceId.value.trim(),
      provider === "tencent" ? Number(tencentRate.value) : Number(rate.value),
    );
  });

  const applyProvider = () => {
    const choice = CUSTOM_CLOUD_PROVIDER_CHOICES.find((item) => item.id === providerSelect.value)
      || CUSTOM_CLOUD_PROVIDER_CHOICES[0];
    idLabel.textContent = choice.idLabel;
    voiceId.placeholder = choice.placeholder;
    voiceId.setAttribute("aria-label", choice.idAriaLabel);
    hint.textContent = `${choice.hint}添加只写本机配置，不会发起收费调用。`;
    submit.textContent = choice.action;
    doubaoRateField.hidden = choice.id !== "doubao";
    tencentRateField.hidden = choice.id !== "tencent";
  };
  providerSelect.addEventListener("change", applyProvider);
  applyProvider();

  return el("section", { class: "panel custom-cloud-voice-panel" },
    el("div", { class: "panel-head" }, el("div", {}, el("p", { class: "eyebrow" }, "云端音色管理"), el("h2", {}, "粘贴音色 ID，添加后直接试听"), hint)),
    el("div", { class: "cloud-voice-form" },
      el("label", {}, el("span", {}, "配音服务"), providerSelect),
      el("label", {}, el("span", {}, "显示名称"), name),
      el("label", {}, idLabel, voiceId),
      doubaoRateField,
      tencentRateField,
      submit,
    ),
  );
}

/* ------------------------- 腾讯云 API 管理 ------------------------- */

function regionSelect(ariaLabel, value) {
  const select = el("select", { "aria-label": ariaLabel });
  const regions = (tencent && tencent.regions) || ["ap-guangzhou"];
  const list = regions.includes(value) || !value ? regions : [value, ...regions];
  list.forEach((region) => {
    const option = el("option", { value: region }, region);
    if (region === (value || "ap-guangzhou")) option.selected = "";
    select.appendChild(option);
  });
  return select;
}

async function refreshTencent() {
  try { tencent = await tencentRequest("/config"); } catch (_) { tencent = null; }
}

async function saveTencentConfig(form) {
  tencentBusy = "save";
  render();
  try {
    tencent = await tencentRequest("/config", {
      method: "PUT",
      body: {
        secret_id: form.secretId.value.trim(),
        secret_key: form.secretKey.value.trim(),
        tts_region: form.ttsRegion.value,
        asr_region: form.asrRegion.value,
      },
    });
    showToast("腾讯云密钥已保存到本机 .env.secrets.local（不会提交到代码库）。");
  } catch (error) {
    showToast(error.message || "保存腾讯云配置失败", true);
  } finally {
    tencentBusy = null;
    render();
  }
}

async function testTencent(service) {
  tencentBusy = service;
  render();
  try {
    const result = await tencentRequest("/test", { method: "POST", body: { service } });
    if (service === "asr") {
      showToast(`语音识别连通成功，识别结果：“${result.recognized_text || ""}”`);
    } else {
      showToast(`语音合成连通成功（${result.voice_id}，${result.audio_duration_seconds ?? "?"} 秒）。`);
    }
  } catch (error) {
    showToast(`${service.toUpperCase()} 测试失败：${error.message}`, true);
  } finally {
    tencentBusy = null;
    await refreshTencent();
    render();
  }
}

async function clearTencentConfig() {
  if (!window.confirm("确定要清除本机保存的腾讯云 SecretId / SecretKey 吗？")) return;
  tencentBusy = "clear";
  render();
  try {
    tencent = await tencentRequest("/config", { method: "DELETE" });
    showToast("已清除本机腾讯云密钥。");
  } catch (error) {
    showToast(error.message || "清除腾讯云配置失败", true);
  } finally {
    tencentBusy = null;
    await refresh();
  }
}

function tencentApiPanel() {
  if (!tencent) return null;
  const configured = tencent.configured;
  const secretId = el("input", { type: "text", autocomplete: "off", placeholder: tencent.secret_id_masked || "AKID…", "aria-label": "腾讯云 SecretId" });
  const secretKey = el("input", { type: "password", autocomplete: "new-password", placeholder: configured ? "已保存（留空则不修改）" : "腾讯云 SecretKey", "aria-label": "腾讯云 SecretKey" });
  const ttsRegion = regionSelect("语音合成地域", tencent.tts_region);
  const asrRegion = regionSelect("语音识别地域", tencent.asr_region);
  const form = { secretId, secretKey, ttsRegion, asrRegion };

  const presetNames = (tencent.preset_voices || []).map((voice) => voice.name).join("、");
  const busy = (key) => tencentBusy === key;

  return el("section", { class: "panel tencent-api-panel", id: "tencent-api" },
    el("div", { class: "panel-head" },
      el("div", {},
        el("p", { class: "eyebrow" }, "API 管理 · 腾讯云语音"),
        el("h2", {}, "腾讯云 SecretId / SecretKey 与测试"),
        el("p", {}, "密钥只写入本机 .env.secrets.local，页面永远只回显掩码。语音合成（TTS）与语音识别（ASR）共用同一对密钥。"),
      ),
      el("span", { class: `status ${configured ? "available" : "unavailable"}` }, configured ? "已配置" : "未配置"),
    ),
    el("div", { class: "cloud-voice-form tencent-form" },
      el("label", {}, el("span", {}, "SecretId"), secretId),
      el("label", {}, el("span", {}, "SecretKey"), secretKey),
      el("label", {}, el("span", {}, "语音合成地域"), ttsRegion),
      el("label", {}, el("span", {}, "语音识别地域"), asrRegion),
      button(busy("save") ? "正在保存…" : "保存密钥", "primary", () => saveTencentConfig(form), !!tencentBusy),
    ),
    el("div", { class: "tencent-service-grid" },
      tencentServiceCard("tts", "语音合成 (TTS)", tencent.tts, "合成一段短句验证密钥与资源包是否可用。"),
      tencentServiceCard("asr", "语音识别 (ASR)", tencent.asr, "先用腾讯云 TTS 合成一句，再回读识别，验证 ASR 是否已开通。"),
    ),
    el("div", { class: "tencent-preset-strip" },
      el("span", {}, `已导入 ${(tencent.preset_voices || []).length} 个腾讯云预设音色：`),
      el("strong", {}, presetNames || "—"),
    ),
    el("div", { class: "tencent-panel-actions" },
      button(busy("clear") ? "正在清除…" : "清除本机密钥", "quiet", clearTencentConfig, !!tencentBusy),
      el("span", { class: "hint" }, "提示：ASR 需在腾讯云控制台单独开通语音识别服务；未开通时测试会返回可读的提示。"),
    ),
  );
}

function tencentServiceCard(service, title, info, description) {
  const configured = info && info.configured;
  return el("div", { class: "tencent-service-card" },
    el("div", { class: "tencent-service-head" },
      el("strong", {}, title),
      el("span", { class: `status ${configured ? "available" : "unavailable"}` }, configured ? "密钥已就绪" : "等待密钥"),
    ),
    el("p", {}, description),
    el("p", { class: "tencent-service-meta" }, `${info ? info.endpoint : "—"} · ${info ? info.version : ""} · ${info ? info.region : ""}`),
    el("div", { class: "tencent-service-actions" },
      button(tencentBusy === service ? "正在测试…" : "测试连通", "quiet", () => testTencent(service), !!tencentBusy || !configured),
      info && info.console_url ? el("a", { class: "button quiet", href: info.console_url, target: "_blank", rel: "noreferrer" }, "打开控制台") : null,
    ),
  );
}

function avatarRoleBindingRow(profile) {
  const role = roleForProfile(profile);
  const name = el("input", { maxlength: "80", placeholder: "例如：雅雅", "aria-label": `${profile.name}的新角色名称` });
  const create = button("新建并关联", "quiet", () => {
    const value = name.value.trim();
    if (!value) { showToast("请先填写角色名称，例如“雅雅”", true); return; }
    createRoleForProfile(profile, value);
  }, profile.available === false);
  const portrait = role && rolePortraitURL(role);
  const upload = el("input", { type: "file", accept: ".png,.jpg,.jpeg,.webp", "aria-label": `${profile.name}对应角色的正面出镜图` });
  upload.addEventListener("change", () => { if (role && upload.files[0]) uploadRolePortrait(role, upload.files[0]); });
  return el("article", { class: "avatar-role-binding-row" },
    portrait ? el("img", { src: portrait, alt: `${role.name}正面出镜图` }) : el("div", { class: "avatar-role-thumb-empty" }, "未上传正面图"),
    el("div", { class: "avatar-role-binding-copy" },
      el("strong", {}, profile.name),
      el("span", {}, role ? `角色：${role.name}` : "尚未关联数字人角色"),
      el("small", {}, role ? (portrait ? "正面图已就绪；项目启动时将复制并冻结。" : "请上传一张清晰的单人正面或半身出镜图。") : "先新建并关联角色，避免只靠文件名猜测人物。"),
    ),
    role ? el("div", { class: "avatar-role-binding-actions" },
      el("label", { class: "button quiet" }, portrait ? "更换正面图" : "上传正面图", upload),
      button("解除关联", "quiet", () => unbindRole(role)),
    ) : el("div", { class: "avatar-role-binding-actions" }, name, create),
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

  const providerSummary = (center.providers || []).map((item) => `${item.name}：${statusText[item.status] || "未知"}`).join("；");
  const service = el("section", { class: "service-strip" },
    el("div", {}, el("p", { class: "eyebrow" }, "可切换配音服务"), el("h2", {}, provider.name || "Haike Video 配音服务"), el("p", {}, provider.detail || "")),
    el("div", { class: "service-state" }, el("span", { class: `status ${provider.status || "unavailable"}` }, statusText[provider.status] || "未知"), el("span", {}, providerSummary || "尚未发现可用服务")),
  );
  const defaultCard = el("section", { class: "default-card" },
    el("div", {}, el("p", { class: "eyebrow" }, "软件通用默认"), el("h2", {}, center.default_voice ? center.default_voice.name : "尚未选择音色"), el("p", {}, center.default_voice ? `${center.default_voice.description || "可用于所有新项目"}` : "请从下方选择一个可用音色。")),
    center.default_voice ? el("div", { class: "default-meta" }, el("span", {}, `编号 ${center.default_voice.id}`), el("span", {}, `引擎 ${center.default_voice.default_engine || "自动"}`)) : null,
  );
  const audition = el("section", { class: "panel audition" },
    el("div", { class: "panel-head" }, el("div", {}, el("p", { class: "eyebrow" }, "独立试听"), el("h2", {}, "先听音色，再用到视频"), el("p", {}, selected ? `当前试听：${selected.name}。此音频属于软件级试听，不写入任何项目。` : "请先选择一个音色。")), isGenerating ? el("span", { class: "status generating" }, "正在生成") : null),
    el("div", { class: "panel-body" }, textarea, el("div", { class: "audition-actions" }, button(isGenerating ? "正在生成试听…" : "生成试听", "primary", generatePreview, isGenerating || !selected || selected.available === false), selected && center.default_voice && selected.id !== center.default_voice.id ? button("将当前试听音色设为默认", "quiet", () => setDefault(selected.id), isGenerating || selected.available === false) : null)),
    job.status === "failed" ? el("p", { class: "error" }, job.error || "试听生成失败") : null,
    job.status === "generating" ? el("p", { class: "hint" }, `Haike Video 正在通过${job.provider_name || "所选服务"}生成音频，页面会自动刷新。`) : null,
  );
  const voiceGrid = el("section", { class: "panel" },
    el("div", { class: "panel-head" },
      el("div", {},
        el("p", { class: "eyebrow" }, "音色目录"),
        el("h2", {}, "已确认音色、腾讯云预设与自定义云端音色"),
        el("p", {}, "本地雅雅、檬檬及其强情感版、豆包雅雅/檬檬，以及 6 个腾讯云预设音色（3 女声 3 男声）；你自行添加的豆包 / 腾讯云音色只保存在本机，历史音色不删除。"),
      ),
    ),
    el("div", { class: "voice-grid" }, (center.profiles || []).map(profileCard)),
  );
  const avatarBindings = el("section", { class: "panel avatar-role-bindings" },
    el("div", { class: "panel-head" }, el("div", {}, el("p", { class: "eyebrow" }, "数字人角色绑定"), el("h2", {}, "音色、角色与出镜图"), el("p", {}, "给实际用于数字人口播的音色关联一个角色，并上传该角色的正面出镜图。项目预检会复制图片到项目目录、展示台词→音色→图片关系，再冻结本次任务输入。"))),
    el("div", { class: "avatar-role-binding-list" }, (center.profiles || []).map(avatarRoleBindingRow)),
  );
  const previews = el("section", { class: "panel" }, el("div", { class: "panel-head" }, el("div", {}, el("p", { class: "eyebrow" }, "近期试听"), el("h2", {}, "可重复试听的通用音频"))), (center.previews || []).length ? el("div", { class: "preview-list" }, center.previews.map(previewRow)) : el("div", { class: "empty" }, "还没有试听记录。先选一个音色，输入一句文案即可。"));

  app.replaceChildren(
    el("header", { class: "topbar" }, returnPath ? el("a", { class: "back-link", href: backHref }, "返回当前项目") : null, el("div", { class: "title" }, el("p", { class: "eyebrow" }, "HAIKE VIDEO / 软件级功能"), el("h1", {}, "通用配音中心"), el("p", {}, "管理全局默认音色与独立试听；项目只生成、审核并引用自己的旁白文件。"))),
    el("div", { class: "layout" }, el("div", { class: "intro-column" }, service, defaultCard), audition),
    tencentApiPanel(),
    customCloudVoicePanel(),
    voiceGrid,
    avatarBindings,
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
    const [nextCenter, roles, nextTencent] = await Promise.all([
      request(""),
      roleRequest(""),
      tencentRequest("/config").catch(() => null),
    ]);
    center = nextCenter;
    avatarRoles = roles.roles || [];
    tencent = nextTencent;
    if (!(center.profiles || []).some((profile) => profile.id === selectedProfileId)) selectedProfileId = center.default_voice && center.default_voice.id;
    render();
    schedulePoll();
  } catch (error) {
    if (showErrors) showToast(error.message || "配音中心暂时无法加载", true);
  }
}

refresh();
