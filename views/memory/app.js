const bridge = window.AstrBotPluginView;
const state = {
  bots: [],
  selected: "",
  detail: null,
  rebuild: null,
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

function renderAmbient(ambient) {
  if (!ambient || !ambient.text) {
    return `<div class="detail-block"><h3>常驻背景</h3><p class="muted">（空）</p></div>`;
  }
  const recent = (ambient.recent || [])
    .map((row) => String(row.summary || row.fragment || "").trim())
    .filter(Boolean);
  const people = (ambient.people || []).map((row) => {
    const topic = String(row.last_topic || "").trim();
    return topic ? `${row.name}（${topic}）` : String(row.name || "");
  }).filter(Boolean);
  const important = (ambient.important || [])
    .map((row) => String(row.text || "").trim())
    .filter(Boolean);
  const blocks = [];
  if (recent.length) blocks.push(`<p><b>最近</b>：${recent.map(esc).join("；")}</p>`);
  if (people.length) blocks.push(`<p><b>认识的人</b>：${people.map(esc).join("；")}</p>`);
  if (important.length) blocks.push(`<p><b>重要</b>：${important.map(esc).join("；")}</p>`);
  return `<div class="detail-block"><h3>常驻背景</h3>${blocks.join("")}</div>`;
}

function renderDetail(data) {
  state.detail = data;
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
    ${renderAmbient(data.ambient)}
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
        const origin = row.source === "story" ? '<span class="chip">故事</span> ' : "";
        return `<li>${origin}${esc(row.fragment || "（未生成）")} <span class="muted">salience ${row.salience ?? "-"}</span>${button}</li>`;
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
}

function openRebuildDialog(data, eventId) {
  if (!data) return;
  const row = (data.events || []).find((item) => String(item.id) === String(eventId));
  if (!row) return;
  state.rebuild = { botId: data.bot_id, eventId: Number(eventId) };
  const oldFragment = row.fragment || "（空）";
  const oldSummary = (row.summary || "（空）").slice(0, 240);
  $("rebuild-old").textContent = `旧碎片：${oldFragment}\n旧摘要：${oldSummary}`;
  $("rebuild-dialog").showModal();
}

async function runRebuild() {
  const pending = state.rebuild;
  const dialog = $("rebuild-dialog");
  if (!pending) return;
  const button = $("rebuild-run");
  button.disabled = true;
  try {
    await apiPost("event/rebuild", {
      bot: pending.botId,
      event_id: pending.eventId,
    });
    dialog.close();
    state.rebuild = null;
    await refreshAll();
    toast("重建完成");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function refreshAll() {
  try {
    await loadOverview();
    if (state.selected) {
      const data = await apiGet("detail", { bot: state.selected });
      renderDetail(data);
    }
    if (!$("panel-settings").classList.contains("hidden")) {
      await loadSettings();
    }
    if (!$("panel-story").classList.contains("hidden")) {
      await loadStory();
    }
    toast("已刷新");
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
    if (!$("panel-story").classList.contains("hidden")) await loadStory();
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

const GATE_SOURCE_LABELS = {
  webui: "WebUI 覆盖",
  bot_override: "配置 bot_overrides",
  config: "全局配置",
  builtin: "内置默认",
};

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
        <label class="gate-field">门控问题（来源：${esc(GATE_SOURCE_LABELS[row.gate_question_source] || row.gate_question_source || "")}）<textarea class="gate-input" rows="4" spellcheck="false">${esc(row.gate_question ?? "")}</textarea></label>
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
  body.gate_question = node.querySelector(".gate-input").value;
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

function clock(ms) {
  if (!ms) return "—";
  const date = new Date(Number(ms));
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getMonth() + 1}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function storyState(row, masterEnabled) {
  if (!masterEnabled) return ['<span class="chip off">总开关未开</span>', false];
  if (!row.enabled) return ['<span class="chip off">未开启</span>', false];
  if (row.paused) return [`<span class="chip warn">已暂停：${esc(row.pause_reason || "未说明")}</span>`, true];
  if (row.running) return ['<span class="chip">正在演一场</span>', true];
  return ['<span class="chip">运行中</span>', true];
}

function renderStory(data) {
  $("story-master").textContent = data.master_enabled
    ? "总开关已开。每个机器人还需要各自开启。"
    : "总开关未开：先在插件配置里打开 story.enabled，下面的开关才会生效。";
  const list = $("story-list");
  const bots = data.bots || [];
  if (!bots.length) {
    list.innerHTML = '<p class="muted">还没有机器人。</p>';
    return;
  }
  list.innerHTML = bots
    .map((row) => {
      const [chip, active] = storyState(row, data.master_enabled);
      const card = (row.card_lines || []).map((line) => String(line).replace(/^-\s*/, "")).join(" ");
      const canon = row.canon || {};
      const scenes = (row.scenes || [])
        .map((scene) => {
          const body =
            scene.status === "ok"
              ? esc(scene.fragment || "（没有记下什么）")
              : `<span class="warn">没演成：${esc(scene.error || "未知原因")}</span>`;
          const meta = `${scene.cause === "manual" ? "手动 · " : ""}${scene.rounds} 轮 · ${scene.calls} 次调用${scene.salience ? " · salience " + scene.salience : ""}`;
          return `<li>${clock(scene.started_at)} ${body} <span class="muted">${meta}</span></li>`;
        })
        .join("");
      const problems = (row.problems || []).length
        ? `<p class="warn">还缺：${(row.problems || []).map(esc).join("；")}</p>`
        : "";
      return `
      <div class="story-card" data-bot="${esc(row.bot_id)}">
        <div class="story-head">
          <div class="story-name">${esc(row.bot_id)} ${chip}</div>
          <div class="detail-actions">
            <button class="ghost" data-story="${row.enabled ? "off" : "on"}">${row.enabled ? "关闭" : "开启"}</button>
            <button class="ghost" data-story="${row.paused ? "resume" : "pause"}" ${row.enabled ? "" : "disabled"}>${row.paused ? "继续" : "暂停"}</button>
            <button data-story="wake" ${row.enabled && !row.running ? "" : "disabled"} title="不消耗唤醒额度">立即演一场</button>
            <button class="ghost" data-story-import>导入世界种子</button>
            <button class="ghost" data-story-export title="含完整转录、设定集和主持人笔记">导出故事数据</button>
          </div>
        </div>
        <div class="story-facts">
          <div><b>世界</b>${row.has_seed ? esc(row.world_title || "（未命名）") : '<span class="warn">还没有导入世界种子</span>'}</div>
          <div><b>人设来源</b>${row.base_card_source ? `${esc(row.base_card_source)}（${row.base_card_chars} 字）` : '<span class="warn">没有取到</span>'}</div>
          <div><b>唤醒额度</b>${row.tokens} / ${row.capacity}（每天回补 ${row.refill_per_day}）</div>
          <div><b>下次唤醒</b>${active ? clock(row.next_wake_at) : "—"}</div>
          <div><b>状态卡</b>${card ? esc(card) : '<span class="muted">没有或已过期</span>'}</div>
          <div><b>累计</b>${row.scenes_total} 场 · 设定 ${canon.seed ?? 0}+${canon.play ?? 0} · 笔记 ${row.notes}</div>
        </div>
        ${problems}
        <h3>最近的场景（她记下的）</h3>
        ${scenes ? `<ul>${scenes}</ul>` : '<p class="muted">（还没有）</p>'}
      </div>`;
    })
    .join("");
}

async function loadStory() {
  try {
    renderStory(await apiGet("story"));
  } catch (error) {
    toast(error.message, true);
  }
}

const STORY_ACTION_DONE = {
  on: "故事已开启",
  off: "故事已关闭，已有的记忆和设定都还在",
  pause: "故事已暂停",
  resume: "故事继续",
  wake: "开始演了，通常要一两分钟，稍后点刷新看结果",
};

async function storyAction(bot, action) {
  try {
    await apiPost("story/action", { bot, action });
    toast(STORY_ACTION_DONE[action] || "好了");
  } catch (error) {
    toast(error.message, true);
  }
  await loadStory();
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
  $("panel-story").classList.toggle("hidden", name !== "story");
  if (name === "settings") loadSettings();
  if (name === "story") loadStory();
}

document.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  if (target.closest("#refresh")) {
    void refreshAll();
    return;
  }
  const storyCard = target.closest(".story-card");
  if (storyCard) {
    const bot = storyCard.dataset.bot;
    const action = target.closest("[data-story]");
    if (action && !action.disabled) {
      void storyAction(bot, action.dataset.story);
      return;
    }
    if (target.closest("[data-story-import]")) {
      openImport(bot);
      return;
    }
    if (target.closest("[data-story-export]")) {
      if (window.confirm("导出的文件包含完整转录、设定集和主持人笔记，会剧透。继续吗？")) {
        void exportBot(bot, "story");
      }
      return;
    }
  }
  const rebuild = target.closest("[data-rebuild]");
  if (rebuild && !rebuild.disabled) {
    openRebuildDialog(state.detail, rebuild.dataset.rebuild);
    return;
  }
  if (target.closest("#rebuild-cancel")) {
    $("rebuild-dialog").close();
    return;
  }
  if (target.closest("#rebuild-run")) {
    void runRebuild();
  }
});

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
