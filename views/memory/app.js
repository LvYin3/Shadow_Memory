const bridge = window.AstrBotPluginView;
const state = {
  bots: [],
  selected: "",
  tts: [],
  defaults: {},
};

const CATEGORY_LABELS = {
  persona: "人格",
  facts: "事实",
  events: "事件",
  insights: "洞察",
  notes: "笔记",
  media: "媒体",
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function toast(message, isError = false) {
  const box = $("toast");
  box.textContent = message;
  box.classList.toggle("error", isError);
  box.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.add("hidden"), 4200);
}

async function apiGet(path, params) {
  return bridge.apiGet(path, params || {});
}

async function apiPost(path, body) {
  return bridge.apiPost(path, body || {});
}

function botLabel(row) {
  return `${row.bot_id} · 人格 ${row.persona?.counts?.core ?? 0}/${row.persona?.counts?.edge ?? 0}/${row.persona?.counts?.tension ?? 0}`;
}

function renderBotList() {
  const list = $("bot-list");
  if (!state.bots.length) {
    list.innerHTML = '<p class="muted">还没有任何机器人记忆。</p>';
    return;
  }
  list.innerHTML = state.bots
    .map((row) => {
      const media = row.media || {};
      const over = media.files > 100 || (media.bytes || 0) > 200 * 1024 * 1024;
      return `
      <button class="bot-card ${row.bot_id === state.selected ? "selected" : ""}" data-bot="${esc(row.bot_id)}">
        <div class="bot-name">${esc(row.bot_id)}</div>
        <div class="bot-meta">
          <span>人格 ${row.persona?.counts?.core ?? 0}/${row.persona?.counts?.edge ?? 0}/${row.persona?.counts?.tension ?? 0}</span>
          <span>事实 ${row.facts?.active ?? 0}</span>
          <span>事件 ${row.events?.total ?? 0}</span>
        </div>
        <div class="bot-meta">
          <span>洞察 ${row.insights?.total ?? 0}</span>
          <span>笔记 ${row.notes?.total ?? 0}</span>
          <span class="${over ? "warn" : ""}">媒体 ${media.files ?? 0}（${media.bytes ? (media.bytes / 1048576).toFixed(1) : 0} MB）</span>
        </div>
      </button>`;
    })
    .join("");
  list.querySelectorAll(".bot-card").forEach((node) => {
    node.addEventListener("click", () => selectBot(node.dataset.bot));
  });
}

function section(title, rows, render) {
  if (!rows || !rows.length) {
    return `<div class="detail-block"><h3>${title}</h3><p class="muted">（空）</p></div>`;
  }
  return `<div class="detail-block"><h3>${title}</h3><ul>${rows.map(render).join("")}</ul></div>`;
}

function renderDetail(data) {
  const panel = $("detail");
  panel.innerHTML = `
    <div class="detail-head">
      <div>
        <h2>${esc(data.bot_id)}</h2>
        <p class="muted">只读明细，每类最多 20 条。</p>
      </div>
      <div class="detail-actions">
        <button id="export-full" class="ghost">导出 full</button>
        <button id="export-seed" class="ghost">导出 seed</button>
        <button id="open-import" class="ghost">导入</button>
        <button id="open-transfer">转移</button>
      </div>
    </div>
    ${section("人格", data.persona, (row) => `<li><b>${esc(row.layer)}</b> ${esc(row.text)}</li>`)}
    ${section("事实", data.facts, (row) => `<li>${row.key_fact ? "★ " : ""}${esc(row.text)} <span class="muted">${esc(row.kind)}</span></li>`)}
    ${section(
      "事件",
      data.events,
      (row) => {
        const canRebuild = row.status === "closed" && Boolean(row.has_turns);
        const title = row.status !== "closed" ? "未关闭的事件不能重建" : "没有原始对话，无法重建";
        const button = canRebuild
          ? `<button class="ghost rebuild-btn" data-rebuild="${row.id}">重建</button>`
          : `<button class="ghost rebuild-btn" disabled title="${title}">重建</button>`;
        return `<li>${esc(row.fragment || "（未生成）")} <span class="muted">salience ${row.salience ?? "-"}</span>${button}</li>`;
      },
    )}
    ${section("洞察", data.insights, (row) => `<li>${esc(row.text)}</li>`)}
    ${section("笔记", data.notes, (row) => `<li><b>${esc(row.title || row.kind)}</b> ${esc(row.body)}</li>`)}
    ${section("媒体", data.media, (row) => `<li>${esc(row.filename)} <span class="muted">${row.description || ""} · ${row.size} B</span></li>`)}
  `;
  $("export-full").onclick = () => exportBot(data.bot_id, "full");
  $("export-seed").onclick = () => exportBot(data.bot_id, "seed");
  $("open-import").onclick = () => openImport(data.bot_id);
  $("open-transfer").onclick = () => openTransfer(data.bot_id);
  panel.querySelectorAll("[data-rebuild]").forEach((node) => {
    node.addEventListener("click", () => rebuildEvent(data, node.dataset.rebuild));
  });
}

async function rebuildEvent(data, eventId) {
  const row = (data.events || []).find((item) => String(item.id) === String(eventId));
  if (!row) return;
  const oldFragment = row.fragment || "（空）";
  const oldSummary = (row.summary || "（空）").slice(0, 240);
  const ok = confirm(
    `重建这条事件？\n\n旧碎片：${oldFragment}\n旧摘要：${oldSummary}\n\n` +
      "会按原始对话重新生成文本，保留 salience / S / R / 状态。",
  );
  if (!ok) return;
  try {
    await apiPost("event/rebuild", { bot: data.bot_id, event_id: Number(eventId) });
    toast("重建完成");
    await selectBot(data.bot_id);
  } catch (error) {
    toast(error.message, true);
  }
}

async function selectBot(botId) {
  state.selected = botId;
  renderBotList();
  try {
    const data = await apiGet("detail", { bot: botId });
    renderDetail(data);
  } catch (error) {
    toast(error.message, true);
  }
}

async function exportBot(botId, kind) {
  try {
    await bridge.download("export", { bot: botId, kind }, `${botId}_${kind}.json`);
    toast(`已导出 ${kind}`);
  } catch (error) {
    toast(error.message, true);
  }
}

function fillTargets(selectId, exclude) {
  const select = $(selectId);
  select.innerHTML = state.bots
    .filter((row) => row.bot_id !== exclude)
    .map((row) => `<option value="${esc(row.bot_id)}">${esc(row.bot_id)}</option>`)
    .join("");
}

function openTransfer(botId) {
  fillTargets("transfer-target", botId);
  $("preview-result").textContent = "";
  $("confirm-wrap").classList.add("hidden");
  $("confirm-target").value = "";
  const boxes = $("category-boxes");
  boxes.innerHTML = Object.entries(CATEGORY_LABELS)
    .map(
      ([key, label]) =>
        `<label class="check"><input type="checkbox" value="${key}" checked /> ${label}</label>`,
    )
    .join("");
  $("transfer-dialog").dataset.source = botId;
  $("transfer-dialog").showModal();
}

function selectedCategories() {
  return [...$("category-boxes").querySelectorAll("input:checked")].map((node) => node.value);
}

function transferMode() {
  return document.querySelector('input[name="mode"]:checked')?.value || "merge";
}

function toggleConfirm() {
  const replace = transferMode() === "replace";
  $("confirm-wrap").classList.toggle("hidden", !replace);
}

async function previewTransfer() {
  const source = $("transfer-dialog").dataset.source;
  const body = {
    source,
    target: $("transfer-target").value,
    categories: selectedCategories(),
    mode: transferMode(),
  };
  if (!body.target) return toast("没有可用的目标机器人", true);
  try {
    const data = await apiPost("transfer/preview", body);
    const media = data.media || {};
    $("preview-result").textContent = `将写入：人格 ${data.counts.persona}，事实 ${data.counts.facts}，事件 ${data.counts.events}，洞察 ${data.counts.insights}，笔记 ${data.counts.notes}；媒体 ${media.files || 0} 个 / ${((media.bytes || 0) / 1048576).toFixed(1)} MB（跳过 ${media.missing || 0}）`;
    if (media.over_limit) toast("媒体超过 100MB / 500 文件，执行会被拒绝", true);
  } catch (error) {
    toast(error.message, true);
  }
}

async function runTransfer() {
  const source = $("transfer-dialog").dataset.source;
  const mode = transferMode();
  const target = $("transfer-target").value;
  const body = {
    source,
    target,
    categories: selectedCategories(),
    mode,
  };
  if (mode === "replace") {
    body.confirm_target = $("confirm-target").value.trim();
    if (body.confirm_target !== target) {
      return toast("覆盖确认必须与目标 bot_id 完全一致", true);
    }
  }
  try {
    $("transfer-run").disabled = true;
    const data = await apiPost("transfer", body);
    const media = data.media || {};
    toast(`转移完成：人格 ${data.counts.persona}，事实 ${data.counts.facts}，事件 ${data.counts.events}，洞察 ${data.counts.insights}，笔记 ${data.counts.notes}，媒体 ${media.copied || 0}${data.backup ? "；备份 " + data.backup : ""}`);
    $("transfer-dialog").close();
    await loadOverview();
  } catch (error) {
    toast(error.message, true);
  } finally {
    $("transfer-run").disabled = false;
  }
}

function openImport(botId) {
  fillTargets("import-target", "");
  if (botId) $("import-target").value = botId;
  $("import-force").checked = false;
  $("import-file").value = "";
  $("import-dialog").showModal();
}

async function runImport() {
  const file = $("import-file").files?.[0];
  if (!file) return toast("先选一个 JSON 文件", true);
  if (file.size > 10 * 1024 * 1024) return toast("文件超过 10MB", true);
  let payload;
  try {
    payload = JSON.parse(await file.text());
  } catch {
    return toast("不是合法的 JSON 文件", true);
  }
  try {
    $("import-run").disabled = true;
    const data = await apiPost("import", {
      bot: $("import-target").value,
      force: $("import-force").checked,
      payload,
    });
    toast(`导入完成：${JSON.stringify(data.counts)}${data.backup ? "；备份 " + data.backup : ""}`);
    $("import-dialog").close();
    await loadOverview();
  } catch (error) {
    toast(error.message, true);
  } finally {
    $("import-run").disabled = false;
  }
}

function ttsOptions(row) {
  const options = [
    `<option value="__follow__" ${row.tts_override == null ? "selected" : ""}>跟随配置${row.tts_source === "config" ? "（当前 " + esc(row.tts_provider_id) + "）" : "（默认静音）"}</option>`,
    `<option value="__mute__" ${row.tts_override === "" ? "selected" : ""}>静音</option>`,
    ...state.tts.map(
      (provider) =>
        `<option value="${esc(provider.id)}" ${row.tts_override === provider.id ? "selected" : ""}>${esc(provider.label)}</option>`,
    ),
  ];
  return options.join("");
}

function renderSettings(data) {
  state.tts = data.tts_providers || [];
  state.defaults = data.defaults || {};
  const list = $("settings-list");
  if (!data.bots.length) {
    list.innerHTML = '<p class="muted">没有机器人可配置。</p>';
    return;
  }
  list.innerHTML = data.bots
    .map(
      (row) => `
      <div class="setting-row" data-bot="${esc(row.bot_id)}">
        <div class="setting-name">${esc(row.bot_id)}<div class="muted">TTS 来源：${esc(row.tts_source)}</div></div>
        <label>TTS<select class="tts-select">${ttsOptions(row)}</select></label>
        <label>上下文 N<input class="ctx-input" type="number" min="0" max="50" value="${row.context_recent_messages ?? ""}" placeholder="全局 ${data.defaults.context_recent_messages}" /></label>
        <button class="save-btn ghost">保存</button>
      </div>`,
    )
    .join("");
  list.querySelectorAll(".setting-row").forEach((node) => {
    node.querySelector(".save-btn").addEventListener("click", () => saveSetting(node));
  });
}

async function saveSetting(node) {
  const bot = node.dataset.bot;
  const ttsValue = node.querySelector(".tts-select").value;
  const ctxRaw = node.querySelector(".ctx-input").value.trim();
  const body = { bot };
  if (ttsValue === "__follow__") {
    body.clear_tts = true;
  } else if (ttsValue === "__mute__") {
    body.tts_provider_id = "";
  } else {
    body.tts_provider_id = ttsValue;
  }
  if (ctxRaw === "") {
    body.clear_context = true;
  } else {
    body.context_recent_messages = Number(ctxRaw);
  }
  try {
    await apiPost("settings", body);
    toast("设置已保存");
    await loadSettings();
    await loadOverview();
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadSettings() {
  try {
    renderSettings(await apiGet("settings"));
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadOverview() {
  try {
    const data = await apiGet("overview");
    state.bots = data.bots || [];
    if (state.selected && !state.bots.some((row) => row.bot_id === state.selected)) {
      state.selected = "";
    }
    renderBotList();
  } catch (error) {
    toast(error.message, true);
  }
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((node) => {
    node.classList.toggle("active", node.dataset.tab === name);
  });
  $("panel-overview").classList.toggle("hidden", name !== "overview");
  $("panel-settings").classList.toggle("hidden", name !== "settings");
  if (name === "settings") loadSettings();
}

async function boot() {
  if (!bridge) {
    document.body.innerHTML = '<p class="muted">这个页面需要在 AstrBot WebUI 中打开。</p>';
    return;
  }
  await bridge.ready();
  const context = bridge.getContext?.() || {};
  console.info("[shadow-memory] view", context.pluginName, context.pageName);
  document.querySelectorAll(".tab").forEach((node) => {
    node.addEventListener("click", () => switchTab(node.dataset.tab));
  });
  $("refresh").addEventListener("click", () => {
    loadOverview();
    if (!$("panel-settings").classList.contains("hidden")) loadSettings();
  });
  $("preview-btn").addEventListener("click", previewTransfer);
  $("transfer-run").addEventListener("click", runTransfer);
  $("transfer-cancel").addEventListener("click", () => $("transfer-dialog").close());
  $("import-run").addEventListener("click", runImport);
  $("import-cancel").addEventListener("click", () => $("import-dialog").close());
  $("transfer-target").addEventListener("change", toggleConfirm);
  document.querySelectorAll('input[name="mode"]').forEach((node) => {
    node.addEventListener("change", toggleConfirm);
  });
  toggleConfirm();
  await loadOverview();
}

boot();
