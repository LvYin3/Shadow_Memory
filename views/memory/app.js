const bridge = window.AstrBotPluginView;
const state = {
  bots: [],
  selected: "",
  detail: null,
  rebuild: null,
  tts: [],
  defaults: {},
  config: null,
  overrideEdits: {},
  configBot: "",
  configView: { level: "cards", groupId: "", sectionKey: "" },
  secretEdits: {},
  spoilerConfirmed: false,
  image: null,
  imageBot: "",
  imageView: "manage",
  imageTestDraft: {
    scene: "",
    recentChat: "",
    providerId: "",
    includeStory: null,
    runReview: false,
  },
  imageTestResult: null,
  imageTestBusy: false,
};

const CONFIG_BLURBS = {
  model: "判断、写作和看图用的模型。",
  memory: "她记得什么、怎么想起来。",
  interaction: "什么时候开口、图片和备忘。",
  story: "聊天之外的那一段生活。",
  image: "拍照、参考图与视觉审核。",
  logging: "日志留多久。",
  bots: "只对某一个机器人生效的覆盖。",
  other: "还没归进上面几块的项。",
};

const CATEGORY_LABELS = {
  persona: "人格",
  facts: "事实",
  events: "事件",
  insights: "洞察",
  notes: "笔记",
  media: "媒体",
  image_refs: "参考图",
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
    if (!$("panel-image").classList.contains("hidden")) {
      await loadImage(state.imageBot);
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
    const refs = data.image_refs || {};
    $("preview-result").textContent = `将写入：人格 ${data.counts.persona}，事实 ${data.counts.facts}，事件 ${data.counts.events}，洞察 ${data.counts.insights}，笔记 ${data.counts.notes}；媒体 ${media.files || 0} 个 / ${((media.bytes || 0) / 1048576).toFixed(1)} MB（跳过 ${media.missing || 0}）；参考图 ${refs.files || 0} 张`;
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
    const refs = data.image_refs || {};
    toast(`转移完成：人格 ${data.counts.persona}，事实 ${data.counts.facts}，事件 ${data.counts.events}，洞察 ${data.counts.insights}，笔记 ${data.counts.notes}，媒体 ${media.copied || 0}，参考图 ${refs.copied || 0}${data.backup ? "；备份 " + data.backup : ""}`);
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
      const platformWarn = row.platform_warning
        ? `<p class="warn">暂缓唤醒：${esc(row.platform_warning)}</p>`
        : "";
      const canRefill =
        row.enabled &&
        !row.running &&
        Number(row.refill_per_day) > 0 &&
        Number(row.tokens) < Number(row.capacity);
      const refillTitle = !row.enabled
        ? "故事还没开启"
        : row.running
          ? "正在演这一场，等演完再补"
          : !(Number(row.refill_per_day) > 0)
            ? "当前每天回补额度为 0，无法补充"
            : Number(row.tokens) >= Number(row.capacity)
              ? "唤醒额度已满"
              : `立刻增加 ${row.refill_per_day} 次，最多攒 ${row.capacity} 次`;
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
          <div><b>唤醒额度</b>${row.tokens} / ${row.capacity}（每天回补 ${row.refill_per_day}）<button class="ghost story-inline-btn" data-story="refill" ${canRefill ? "" : "disabled"} title="${esc(refillTitle)}">补充一天额度</button></div>
          <div><b>下次唤醒</b>${active ? clock(row.next_wake_at) : "—"}</div>
          <div><b>状态卡</b>${card ? esc(card) : '<span class="muted">没有或已过期</span>'}</div>
          <div><b>累计</b>${row.scenes_total} 场 · 设定 ${canon.seed ?? 0}+${canon.play ?? 0} · 笔记 ${row.notes}</div>
          <div><b>活跃配角</b>${activeNpcs(row)}</div>
        </div>
        ${problems}
        ${platformWarn}
        <h3>最近的场景（她记下的）</h3>
        ${scenes ? `<ul>${scenes}</ul>` : '<p class="muted">（还没有）</p>'}
        <p><button class="ghost" data-story-spoiler>剧透与种子</button></p>
        <div class="story-spoiler"></div>
      </div>`;
    })
    .join("");
}

function activeNpcs(row) {
  const names = (row.npcs || []).map((npc) => {
    const when = npc.last_seen_at ? clock(npc.last_seen_at) : "还没出场";
    return `${esc(npc.name)}（${when}）`;
  });
  return names.length ? names.join("、") : '<span class="muted">没有</span>';
}

function spoilerHtml(data) {
  const scenes = (data.scenes || [])
    .map((scene) => {
      const lines = (scene.transcript || [])
        .map((turn) => {
          const who = turn.role === "npc" ? esc(turn.name || "配角") : turn.role === "me" ? "她" : "叙述";
          const extra = turn.role === "me" && turn.inner ? `（心里：${esc(turn.inner)}）` : "";
          return `<li>${who}：${extra}${esc(turn.text || "")}</li>`;
        })
        .join("");
      return `<section><h4>${clock(scene.started_at)} · ${esc(scene.status || "")}</h4><ul>${lines || "<li class='muted'>（空）</li>"}</ul></section>`;
    })
    .join("");
  const tree = data.tree || { threads: [], daily: { recent: [], older: [] } };
  const threads = (tree.threads || [])
    .map((thread) => {
      const people = (thread.people || []).map(esc).join("、");
      const linked = [...(thread.canon || []), ...(thread.seed || [])].map(esc).join("、");
      const scenes = (thread.scenes || []).map((id) => `#${id}`).join(" ");
      return `<li>${esc(thread.title || "")}${people ? ` · ${people}` : ""}${linked ? ` · ${linked}` : ""}${scenes ? ` · ${scenes}` : ""}</li>`;
    })
    .join("");
  const daily = tree.daily || {};
  const recent = (daily.recent || []).map((item) => `<li>#${item.id} ${esc(item.line || "")}</li>`).join("");
  const older = (daily.older || []).map((id) => `#${id}`).join(" ");
  const canon = (data.canon || [])
    .map((item) => {
      const known = item.visibility === "known";
      const badge = known ? "角色已知" : "仅 GM";
      const next = known ? "gm_only" : "known";
      const action = known ? "藏为仅 GM" : "设为角色已知";
      return `<li>${esc(item.origin)} · <b>[${badge}]</b> ${esc(item.name)}：${esc(item.body)} `
        + `<button class="ghost" type="button" data-canon-vis data-canon-id="${item.id}" data-next-vis="${next}">${action}</button></li>`;
    })
    .join("");
  const seed = data.seed || {};
  const options = (data.categories || [])
    .map((item) => `<option value="${esc(item.id)}">${esc(item.label)}</option>`)
    .join("");
  const entries = (seed.entries || [])
    .map((entry) => entryRow(options, entry))
    .join("");
  const hooks = (seed.hooks || []).map((hook) => hookRow(hook)).join("");
  const opening = seed.opening || {};
  let heavy = "";
  if (data.spoiler_heavy) {
    const notes = (data.notes || [])
      .map(
        (note) => `<div class="note-row" data-note-id="${note.id}">
          <input class="note-title" value="${esc(note.title)}" />
          <textarea class="note-body">${esc(note.body)}</textarea>
          <button class="ghost" data-note-save>保存</button>
          <button class="ghost" data-note-delete>删除</button>
        </div>`
      )
      .join("");
    const npcs = (data.npcs || [])
      .map((npc) => {
        const persona = (npc.persona || []).map((item) => `<li>${esc(item.layer)}：${esc(item.text)}</li>`).join("");
        const facts = (npc.facts || []).map((text) => `<li>${esc(text)}</li>`).join("");
        return `<section><h4>${esc(npc.name)} · ${npc.status === "active" ? "活跃" : "休眠"}</h4>
          <p>${esc(npc.relationship || "")}</p>
          <ul>${persona}</ul><ul>${facts}</ul></section>`;
      })
      .join("");
    heavy = `<h3>主持人笔记</h3>${notes || '<p class="muted">（没有）</p>'}
      <div class="note-row">
        <input class="note-title" placeholder="新笔记标题" />
        <textarea class="note-body" placeholder="正文"></textarea>
        <button class="ghost" data-note-save>添加</button>
      </div>
      <h3>配角记忆</h3>${npcs || '<p class="muted">（没有）</p>'}`;
  }
  return `
    <h3>完整转录</h3>
    ${scenes || '<p class="muted">（还没有）</p>'}
    <h3>故事树（只读）</h3>
    ${threads ? `<ul>${threads}</ul>` : '<p class="muted">（还没有未了结的线）</p>'}
    <p>日常</p>
    ${recent ? `<ul>${recent}</ul>` : ""}
    ${older ? `<p class="muted">更早：${older}</p>` : ""}
    <h3>设定集（可切换角色可见性）</h3>
    ${canon ? `<ul>${canon}</ul>` : '<p class="muted">（没有）</p>'}
    <h3>世界种子</h3>
    <label>标题 <input class="seed-title" value="${esc(seed.title || "")}" /></label>
    <label>大纲（背景模型可见） <textarea class="seed-outline">${esc(seed.outline || "")}</textarea></label>
    <label>角色已知底稿（聊天与演出里的她可见，留空保存时自动提炼） <textarea class="seed-known-outline">${esc(seed.known_outline || "")}</textarea></label>
    <div class="seed-entries">${entries}</div>
    <button class="ghost" type="button" data-entry-add>加一条设定</button>
    <div class="seed-hooks">${hooks}</div>
    <button class="ghost" type="button" data-hook-add>加一个钩子</button>
    <label>开场地点 <input class="seed-where" value="${esc(opening.where || "")}" /></label>
    <label>开场在做 <input class="seed-doing" value="${esc(opening.doing || "")}" /></label>
    <p><button data-seed-save>保存世界种子</button> <button class="ghost" type="button" data-world-classify>自动提炼底稿与分类</button></p>
    ${heavy}`;
}

function entryRow(options, entry) {
  const selected = entry && entry.category ? entry.category : "other";
  const opts = options.replace(
    `value="${selected}"`,
    `value="${selected}" selected`
  );
  const vis = entry && entry.visibility ? entry.visibility : "";
  const visOpts = [
    ["", "自动判定"],
    ["known", "角色已知"],
    ["gm_only", "仅 GM"],
  ]
    .map(([val, label]) => `<option value="${val}"${vis === val ? " selected" : ""}>${label}</option>`)
    .join("");
  return `<div class="entry-row">
    <select class="entry-category">${opts}</select>
    <select class="entry-visibility">${visOpts}</select>
    <input class="entry-name" value="${esc((entry && entry.name) || "")}" />
    <textarea class="entry-body">${esc((entry && entry.body) || "")}</textarea>
    <button class="ghost" type="button" data-row-remove>去掉</button>
  </div>`;
}

function hookRow(text) {
  return `<div class="hook-row"><input class="hook-text" value="${esc(text || "")}" />
    <button class="ghost" type="button" data-row-remove>去掉</button></div>`;
}

async function openSpoiler(card, bot) {
  const box = card.querySelector(".story-spoiler");
  if (!box) return;
  if (!state.spoilerConfirmed) {
    box.innerHTML = `<p>展开后会看到她还没经历的转录、设定和世界种子。这次确认只在本页有效，刷新后要再确认。</p>
      <button type="button" data-spoiler-confirm>我知道，展开</button>`;
    return;
  }
  try {
    box.dataset.categories = JSON.stringify((await loadSpoiler(card, bot)).categories || []);
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadSpoiler(card, bot) {
  const data = await apiGet("story/spoiler", { bot });
  card.querySelector(".story-spoiler").innerHTML = spoilerHtml(data);
  card.dataset.categories = JSON.stringify(data.categories || []);
  return data;
}

function collectSeed(card) {
  const box = card.querySelector(".story-spoiler");
  const entries = [...box.querySelectorAll(".entry-row")]
    .map((row) => {
      const item = {
        category: row.querySelector(".entry-category").value,
        name: row.querySelector(".entry-name").value.trim(),
        body: row.querySelector(".entry-body").value.trim(),
      };
      const vis = row.querySelector(".entry-visibility")?.value || "";
      if (vis) item.visibility = vis;
      return item;
    })
    .filter((item) => item.name || item.body);
  const hooks = [...box.querySelectorAll(".hook-text")]
    .map((node) => node.value.trim())
    .filter(Boolean);
  const knownOutline = box.querySelector(".seed-known-outline")?.value.trim() || "";
  const seed = {
    version: "world_seed/v1",
    title: box.querySelector(".seed-title").value.trim(),
    outline: box.querySelector(".seed-outline").value.trim(),
    entries,
    hooks,
    opening: {
      where: box.querySelector(".seed-where").value.trim(),
      doing: box.querySelector(".seed-doing").value.trim(),
    },
  };
  if (knownOutline) seed.known_outline = knownOutline;
  return seed;
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
  refill: "已补充一天唤醒额度",
};

async function saveStorySeed(card, bot) {
  try {
    const res = await apiPost("story/seed", { bot, ...collectSeed(card) });
    const extra = res && res.known != null ? `（角色已知 ${res.known} 条，仅 GM ${res.gm_only} 条）` : "";
    toast(`世界种子已换上${extra}，下一场才会用`);
    await loadSpoiler(card, bot);
  } catch (error) {
    toast(error.message, true);
  }
}

async function classifyStoryWorld(card, bot) {
  try {
    const res = await apiPost("story/classify", { bot });
    toast(`已提炼角色底稿并分类：角色已知 ${res.known} 条，仅 GM ${res.gm_only} 条`);
    await loadSpoiler(card, bot);
  } catch (error) {
    toast(error.message, true);
  }
}

async function toggleCanonVisibility(card, bot, btn) {
  try {
    const id = Number(btn.dataset.canonId || 0);
    const visibility = btn.dataset.nextVis || "gm_only";
    await apiPost("story/canon/visibility", { bot, id, visibility });
    toast(visibility === "known" ? "已设为角色已知" : "已藏为仅 GM 可见");
    await loadSpoiler(card, bot);
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveStoryNote(card, bot, row) {
  if (!row) return;
  try {
    await apiPost("story/note", {
      bot,
      id: Number(row.dataset.noteId || 0),
      title: row.querySelector(".note-title").value,
      body: row.querySelector(".note-body").value,
    });
    toast("笔记已保存");
    await loadSpoiler(card, bot);
  } catch (error) {
    toast(error.message, true);
  }
}

async function deleteStoryNote(card, bot, row) {
  if (!row || !row.dataset.noteId) {
    row?.remove();
    return;
  }
  try {
    await apiPost("story/note/delete", { bot, id: Number(row.dataset.noteId) });
    toast("笔记已删除");
    await loadSpoiler(card, bot);
  } catch (error) {
    toast(error.message, true);
  }
}

async function storyAction(bot, action) {
  try {
    const data = await apiPost("story/action", { bot, action });
    if (
      action === "refill" &&
      data &&
      data.before_tokens != null &&
      data.after_tokens != null
    ) {
      toast(`已补充唤醒额度：${data.before_tokens} → ${data.after_tokens}`);
    } else {
      toast(STORY_ACTION_DONE[action] || "好了");
    }
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

function providerChoices(list, selected, emptyLabel) {
  const items = list || [];
  const known = new Set(items.map((item) => item.id));
  let html = `<option value="">${esc(emptyLabel)}</option>`;
  if (selected && !known.has(selected)) {
    html += `<option value="${esc(selected)}" selected>${esc(selected)}（当前值）</option>`;
  }
  html += items
    .map(
      (item) =>
        `<option value="${esc(item.id)}" ${item.id === selected ? "selected" : ""}>${esc(item.label || item.id)}</option>`,
    )
    .join("");
  return html;
}

function fieldControl(field, value) {
  const path = esc(field.path);
  const slider = field.slider || {};
  const min = slider.min != null ? ` min="${esc(slider.min)}"` : "";
  const max = slider.max != null ? ` max="${esc(slider.max)}"` : "";
  const step = slider.step != null ? ` step="${esc(slider.step)}"` : field.number_type === "float" ? ` step="any"` : "";
  if (field.widget === "bool") {
    return `<label class="check"><input type="checkbox" data-config-path="${path}" data-widget="bool" ${value ? "checked" : ""} /><span>${esc(field.label)}</span></label>`;
  }
  if (field.widget === "textarea") {
    return `<label class="wide">${esc(field.label)}<textarea data-config-path="${path}" data-widget="textarea" rows="4">${esc(value)}</textarea></label>`;
  }
  if (field.widget === "select" || field.widget === "ugc") {
    const current = value == null ? "" : String(value);
    const options = (field.options || [])
      .map(
        (item) =>
          `<option value="${esc(item.value)}" ${String(item.value) === current ? "selected" : ""}>${esc(item.label)}</option>`,
      )
      .join("");
    return `<label>${esc(field.label)}<select data-config-path="${path}" data-widget="${esc(field.widget)}">${options}</select></label>`;
  }
  if (field.widget === "provider") {
    return `<label>${esc(field.label)}<select data-config-path="${path}" data-widget="provider">${providerChoices(state.config.providers, value || "", "留空用默认")}</select></label>`;
  }
  if (field.widget === "secret") {
    const set = state.config.values.secrets?.[field.path]?.set;
    const pending = state.secretEdits[field.path] || {};
    const clear = pending.clear ? " checked" : "";
    return `<div class="wide"><label>${esc(field.label)}<input type="password" data-secret="${path}" autocomplete="new-password" placeholder="${set ? "已填写，留空不改" : "未填写"}" value="${esc(pending.value || "")}" /></label><label class="check"><input type="checkbox" data-secret-clear="${path}"${clear} />清除</label></div>`;
  }
  if (field.widget === "number") {
    return `<label>${esc(field.label)}<input type="number" data-config-path="${path}" data-widget="number"${min}${max}${step} value="${esc(value)}" /></label>`;
  }
  if (field.widget === "thinking") {
    const modes = state.config.values.thinking_by_task || {};
    const rows = (field.options ? ["fragment", "tension", "facts", "distill", "consolidate"] : [])
      .map((task) => {
        const current = modes[task] || "";
        const options = (field.options || [])
          .map(
            (item) =>
              `<option value="${esc(item.value)}" ${item.value === current ? "selected" : ""}>${esc(item.label)}</option>`,
          )
          .join("");
        return `<label>${esc(task)}<select data-thinking="${esc(task)}">${options}</select></label>`;
      })
      .join("");
    return `<div class="wide"><div>${esc(field.label)}</div><div class="config-fields">${rows}</div></div>`;
  }
  if (field.widget === "tts") {
    const rows = Object.entries(state.config.values.tts_provider_ids || {});
    const body = (rows.length ? rows : [["", ""]])
      .map(
        ([bot, provider]) => `
        <div class="tts-row">
          <input data-tts-bot placeholder="bot_id" value="${esc(bot)}" />
          <select data-tts-provider>${providerChoices(state.config.tts_providers, provider, "选择提供商")}</select>
          <button type="button" class="ghost" data-tts-remove>删除</button>
        </div>`,
      )
      .join("");
    return `<div class="wide" data-tts><div>${esc(field.label)}</div>${body}<button type="button" class="ghost" data-tts-add>加一行</button></div>`;
  }
  if (field.widget === "dict_rows") {
    const rows = Object.entries(value || {});
    const body = (rows.length ? rows : [["", ""]])
      .map(
        ([key, item]) => `
        <div class="tts-row">
          <input data-dict-key="${path}" value="${esc(key)}" />
          <input data-dict-value="${path}" value="${esc(item)}" />
          <button type="button" class="ghost" data-dict-remove>删除</button>
        </div>`,
      )
      .join("");
    return `<div class="wide" data-dict="${path}"><div>${esc(field.label)}</div>${body}<button type="button" class="ghost" data-dict-add="${path}">加一行</button></div>`;
  }
  return `<label>${esc(field.label)}<input type="text" data-config-path="${path}" data-widget="text" value="${esc(value)}" /></label>`;
}

function fieldBlock(field, values) {
  const value = values[field.path];
  const hint = field.hint ? `<p class="hint">${esc(field.hint)}</p>` : "";
  const note = field.note ? `<p class="note">${esc(field.note)}</p>` : "";
  return `<div>${fieldControl(field, value)}${hint}${note}</div>`;
}

function renderFields(fields, values) {
  if (!fields.length) return "";
  return `<div class="config-fields">${fields.map((field) => fieldBlock(field, values)).join("")}</div>`;
}

function configBotChoices() {
  const ids = new Set(state.config.bots || []);
  (state.bots || []).forEach((row) => ids.add(row.bot_id));
  Object.keys(state.overrideEdits).forEach((id) => ids.add(id));
  return [...ids].filter(Boolean).sort();
}

function overrideControl(field, value) {
  const key = esc(field.key);
  if (field.widget === "tri") {
    const current = value === true ? "true" : value === false ? "false" : "";
    const options = (field.options || [])
      .map(
        (item) =>
          `<option value="${esc(item.value)}" ${item.value === current ? "selected" : ""}>${esc(item.label)}</option>`,
      )
      .join("");
    return `<label>${esc(field.label)}<select data-override="${key}" data-widget="tri">${options}</select></label>`;
  }
  if (field.widget === "select" || field.widget === "ugc") {
    const current = value == null ? "" : String(value);
    const options = (field.options || [])
      .map(
        (item) =>
          `<option value="${esc(item.value)}" ${String(item.value) === current ? "selected" : ""}>${esc(item.label)}</option>`,
      )
      .join("");
    return `<label>${esc(field.label)}<select data-override="${key}" data-widget="${esc(field.widget)}">${options}</select></label>`;
  }
  if (field.widget === "provider") {
    return `<label>${esc(field.label)}<select data-override="${key}" data-widget="provider">${providerChoices(state.config.tts_providers, value || "", "跟随全局")}</select></label>`;
  }
  if (field.widget === "textarea") {
    return `<label>${esc(field.label)}<textarea data-override="${key}" data-widget="textarea" rows="3">${esc(value || "")}</textarea></label>`;
  }
  if (field.widget === "number") {
    const slider = field.slider || {};
    const min = slider.min != null ? ` min="${esc(slider.min)}"` : "";
    const max = slider.max != null ? ` max="${esc(slider.max)}"` : "";
    const step = slider.step != null ? ` step="${esc(slider.step)}"` : field.number_type === "float" ? ` step="any"` : "";
    return `<label>${esc(field.label)}<input data-override="${key}" data-widget="number" type="number"${min}${max}${step} value="${value == null ? "" : esc(value)}" placeholder="留空跟随全局" /></label>`;
  }
  return `<label>${esc(field.label)}<input data-override="${key}" data-widget="text" type="text" value="${esc(value || "")}" placeholder="留空跟随全局" /></label>`;
}

function renderBotFields() {
  const group = (state.config.groups || []).find((item) => item.kind === "overrides");
  const box = $("config-bot-fields");
  if (!group || !box) return;
  const values = state.overrideEdits[state.configBot] || {};
  if (!state.configBot) {
    box.innerHTML = `<p class="muted">选择或填写一个机器人。</p>`;
    return;
  }
  box.innerHTML = (group.fields || [])
    .map((field) => {
      const hint = field.hint ? `<p class="hint">${esc(field.hint)}</p>` : "";
      return `<div>${overrideControl(field, values[field.key])}${hint}</div>`;
    })
    .join("");
}

function renderBotPicker() {
  const select = $("config-bot-select");
  if (!select) return;
  const ids = configBotChoices();
  select.innerHTML = [`<option value="">选择机器人</option>`]
    .concat(
      ids.map(
        (id) => `<option value="${esc(id)}" ${id === state.configBot ? "selected" : ""}>${esc(id)}</option>`,
      ),
    )
    .join("");
}

function configGroups() {
  return state.config?.groups || [];
}

function configGroup(id) {
  return configGroups().find((item) => item.id === id) || null;
}

function configBackButton() {
  return `<button type="button" class="ghost config-back" data-config-back>返回</button>`;
}

function renderSectionBlocks(section, values) {
  return (section.blocks || [])
    .map((block) => {
      const title = block.title ? `<h4>${esc(block.title)}</h4>` : "";
      const advanced = block.advanced?.length
        ? `<details class="advanced"><summary>进阶</summary>${renderFields(block.advanced, values)}</details>`
        : "";
      return `${title}${renderFields(block.common || [], values)}${advanced}`;
    })
    .join("");
}

function renderConfigCards() {
  const cards = configGroups()
    .map(
      (group) => `
      <button type="button" class="config-card" data-config-group="${esc(group.id)}">
        <span class="config-card-title">${esc(group.title)}</span>
        <span class="muted">${esc(CONFIG_BLURBS[group.id] || "")}</span>
      </button>`,
    )
    .join("");
  return `<div class="config-cards">${cards}</div>`;
}

function renderConfigSections(groupId) {
  const group = configGroup(groupId);
  if (!group || group.kind === "overrides") return renderConfigCards();
  const rows = (group.sections || [])
    .map(
      (section) => `
      <button type="button" class="config-card" data-config-section="${esc(section.key)}">
        <span class="config-card-title">${esc(section.title)}</span>
      </button>`,
    )
    .join("");
  return `${configBackButton()}<h2>${esc(group.title)}</h2><div class="config-cards">${rows}</div>`;
}

function renderConfigFields(view) {
  const group = configGroup(view.groupId);
  if (!group) return renderConfigCards();
  if (group.kind === "overrides") {
    return `${configBackButton()}
      <section class="config-block">
        <h2>${esc(group.title)}</h2>
        <p class="muted">留空就是删掉这项覆盖，跟随全局。不认识的键会原样留着，这里不显示。</p>
        <div class="bot-pick">
          <select id="config-bot-select"></select>
          <input id="config-bot-new" type="text" placeholder="或填写新的 bot_id" />
          <button type="button" class="ghost" id="config-bot-use">编辑</button>
        </div>
        <div id="config-bot-fields" class="config-fields"></div>
      </section>`;
  }
  const sections = group.sections || [];
  const section = sections.find((item) => item.key === view.sectionKey) || sections[0];
  if (!section) {
    return `${configBackButton()}<p class="muted">这一节没有可改的项。</p>`;
  }
  const values = state.config.values?.fields || {};
  return `${configBackButton()}
    <section class="config-block">
      <p class="muted">${esc(group.title)}</p>
      <h2>${esc(section.title)}</h2>
      ${renderSectionBlocks(section, values)}
    </section>`;
}

function renderConfig() {
  const root = $("config-root");
  if (!root || !state.config) return;
  const view = state.configView || { level: "cards", groupId: "", sectionKey: "" };
  if (view.level === "sections") root.innerHTML = renderConfigSections(view.groupId);
  else if (view.level === "fields") root.innerHTML = renderConfigFields(view);
  else root.innerHTML = renderConfigCards();
  renderBotPicker();
  renderBotFields();
}

function openConfigGroup(id) {
  flushDraft();
  const group = configGroup(id);
  if (!group) return;
  const sections = group.sections || [];
  if (group.kind === "overrides" || sections.length <= 1) {
    state.configView = {
      level: "fields",
      groupId: id,
      sectionKey: group.kind === "overrides" ? "" : sections[0]?.key || "",
    };
  } else {
    state.configView = { level: "sections", groupId: id, sectionKey: "" };
  }
  renderConfig();
}

function openConfigSection(key) {
  flushDraft();
  state.configView = { level: "fields", groupId: state.configView.groupId, sectionKey: key };
  renderConfig();
}

function backConfig() {
  flushDraft();
  const view = state.configView;
  const group = configGroup(view.groupId);
  const many = group && group.kind !== "overrides" && (group.sections || []).length > 1;
  if (view.level === "fields" && many) {
    state.configView = { level: "sections", groupId: view.groupId, sectionKey: "" };
  } else {
    state.configView = { level: "cards", groupId: "", sectionKey: "" };
  }
  renderConfig();
}

function flushBotForm() {
  const box = $("config-bot-fields");
  if (!box || !state.configBot) return;
  const data = {};
  box.querySelectorAll("[data-override]").forEach((node) => {
    if (node.dataset.widget === "tri") {
      data[node.dataset.override] = node.value === "true" ? true : node.value === "false" ? false : "";
      return;
    }
    data[node.dataset.override] = node.value;
  });
  state.overrideEdits[state.configBot] = data;
}

function useConfigBot(botId) {
  const next = String(botId || "").trim();
  if (!next) {
    toast("先填写机器人", true);
    return;
  }
  flushBotForm();
  if (!state.overrideEdits[next]) state.overrideEdits[next] = {};
  state.configBot = next;
  renderBotPicker();
  renderBotFields();
}

function onConfigChange(event) {
  const target = event.target;
  if (target && target.id === "config-bot-select") {
    flushBotForm();
    state.configBot = target.value || "";
    renderBotFields();
  }
}

function onConfigClick(event) {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  if (target.closest("[data-config-back]")) {
    backConfig();
    return;
  }
  const card = target.closest("[data-config-group]");
  if (card) {
    openConfigGroup(card.dataset.configGroup);
    return;
  }
  const section = target.closest("[data-config-section]");
  if (section) {
    openConfigSection(section.dataset.configSection);
    return;
  }
  if (target.id === "config-bot-use") {
    useConfigBot($("config-bot-new").value);
    return;
  }
  if (target.dataset.ttsAdd != null || target.hasAttribute("data-tts-add")) {
    const host = target.closest("[data-tts]");
    const row = document.createElement("div");
    row.className = "tts-row";
    row.innerHTML = `<input data-tts-bot placeholder="bot_id" /><select data-tts-provider>${providerChoices(state.config.tts_providers, "", "选择提供商")}</select><button type="button" class="ghost" data-tts-remove>删除</button>`;
    host.insertBefore(row, target);
    return;
  }
  if (target.hasAttribute("data-tts-remove")) {
    target.closest(".tts-row")?.remove();
    return;
  }
  if (target.dataset.dictAdd) {
    const host = target.closest("[data-dict]");
    const path = target.dataset.dictAdd;
    const row = document.createElement("div");
    row.className = "tts-row";
    row.innerHTML = `<input data-dict-key="${esc(path)}" /><input data-dict-value="${esc(path)}" /><button type="button" class="ghost" data-dict-remove>删除</button>`;
    host.insertBefore(row, target);
    return;
  }
  if (target.hasAttribute("data-dict-remove")) {
    target.closest(".tts-row")?.remove();
  }
}

function flushDraft() {
  if (!state.config?.values) return;
  flushBotForm();
  const root = $("config-root");
  if (!root) return;
  const values = state.config.values;
  values.fields = values.fields || {};
  root.querySelectorAll("[data-config-path]").forEach((node) => {
    const path = node.dataset.configPath;
    if (node.dataset.widget === "bool") values.fields[path] = node.checked;
    else if (node.dataset.widget === "ugc" || node.dataset.widget === "number") values.fields[path] = node.value;
    else values.fields[path] = node.value;
  });
  root.querySelectorAll("[data-secret]").forEach((node) => {
    const path = node.dataset.secret;
    const clear = root.querySelector(`[data-secret-clear="${CSS.escape(path)}"]`);
    state.secretEdits[path] = { value: node.value, clear: Boolean(clear && clear.checked) };
  });
  const thinkingNodes = root.querySelectorAll("[data-thinking]");
  if (thinkingNodes.length) {
    values.thinking_by_task = { ...(values.thinking_by_task || {}) };
    thinkingNodes.forEach((node) => {
      values.thinking_by_task[node.dataset.thinking] = node.value;
    });
  }
  const ttsHost = root.querySelector("[data-tts]");
  if (ttsHost) {
    const tts = {};
    ttsHost.querySelectorAll(".tts-row").forEach((row) => {
      const bot = row.querySelector("[data-tts-bot]");
      const provider = row.querySelector("[data-tts-provider]");
      if (!bot || !provider) return;
      const botId = bot.value.trim();
      const providerId = provider.value.trim();
      if (botId && providerId) tts[botId] = providerId;
    });
    values.tts_provider_ids = tts;
  }
  root.querySelectorAll("[data-dict]").forEach((host) => {
    const path = host.dataset.dict;
    const rows = {};
    host.querySelectorAll(".tts-row").forEach((row) => {
      const key = row.querySelector("[data-dict-key]")?.value.trim();
      const value = row.querySelector("[data-dict-value]")?.value ?? "";
      if (key) rows[key] = value;
    });
    values.fields[path] = rows;
  });
}

function collectConfig() {
  flushDraft();
  const values = state.config?.values || {};
  const secrets = {};
  const known = new Set([
    ...Object.keys(values.secrets || {}),
    ...Object.keys(state.secretEdits || {}),
  ]);
  known.forEach((path) => {
    secrets[path] = state.secretEdits[path] || { value: "", clear: false };
  });
  return {
    fields: { ...(values.fields || {}) },
    secrets,
    thinking_by_task: { ...(values.thinking_by_task || {}) },
    tts_provider_ids: { ...(values.tts_provider_ids || {}) },
    bot_overrides: state.overrideEdits,
  };
}

async function loadConfig() {
  try {
    state.config = await apiGet("config");
    state.overrideEdits = { ...(state.config.values?.bot_overrides || {}) };
    state.secretEdits = {};
    state.configView = { level: "cards", groupId: "", sectionKey: "" };
    const choices = configBotChoices();
    state.configBot = choices.includes(state.configBot) ? state.configBot : choices[0] || "";
    renderConfig();
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveConfig() {
  const button = $("config-save");
  button.disabled = true;
  try {
    const result = await apiPost("config", collectConfig());
    if (result?.values && state.config) state.config.values = result.values;
    state.secretEdits = {};
    state.overrideEdits = { ...(result?.values?.bot_overrides || state.overrideEdits) };
    renderConfig();
    toast("已保存，下一轮就生效");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((node) => {
    node.classList.toggle("active", node.dataset.tab === name);
  });
  $("panel-overview").classList.toggle("hidden", name !== "overview");
  $("panel-settings").classList.toggle("hidden", name !== "settings");
  $("panel-story").classList.toggle("hidden", name !== "story");
  $("panel-image").classList.toggle("hidden", name !== "image");
  $("panel-config").classList.toggle("hidden", name !== "config");
  if (name === "settings") loadSettings();
  if (name === "story") loadStory();
  if (name === "image") loadImage(state.imageBot);
  if (name === "config") loadConfig();
}

function providerCardHtml(item, protocols) {
  const opts = protocols
    .map(
      (p) =>
        `<option value="${esc(p.id)}" ${p.id === item.protocol ? "selected" : ""}>${esc(p.label)}</option>`,
    )
    .join("");
  return `
    <div class="provider-card" data-provider-row>
      <label>ID<input type="text" data-pf="id" value="${esc(item.id || "")}" placeholder="如 openai-main" /></label>
      <label>显示名<input type="text" data-pf="name" value="${esc(item.name || "")}" placeholder="如 GPT Image" /></label>
      <label>协议<select data-pf="protocol">${opts}</select></label>
      <label>Base URL<input type="text" data-pf="base_url" value="${esc(item.base_url || "")}" placeholder="https://api.openai.com/v1" /></label>
      <label>模型名<input type="text" data-pf="model" value="${esc(item.model || "")}" placeholder="gpt-image-1 / gemini-2.5-flash-image" /></label>
      <label>API Key${item.api_key_set ? "（已设置，留空不改）" : ""}
        <input type="password" data-pf="api_key" value="" placeholder="${item.api_key_set ? "••••••（留空不改）" : "sk-..."}" autocomplete="off" />
      </label>
      <label>尺寸（OpenAI）<input type="text" data-pf="size" value="${esc(item.size || "")}" placeholder="如 1024x1024（可空）" /></label>
      <label>比例（Gemini）<input type="text" data-pf="aspect_ratio" value="${esc(item.aspect_ratio || "")}" placeholder="如 3:4 / 1:1（可空）" /></label>
      <label>超时（秒）<input type="number" data-pf="timeout_s" value="${esc(item.timeout_s ?? 60)}" min="5" max="300" /></label>
      <div class="provider-actions">
        ${item.api_key_set ? '<label class="check"><input type="checkbox" data-pf="clear_api_key" /> 清除密钥</label>' : ""}
        <button type="button" class="ghost" data-provider-test>测试连通性</button>
        <button type="button" class="danger ghost" data-provider-remove>删除</button>
      </div>
    </div>`;
}

function collectProviderRows() {
  return [...document.querySelectorAll("#image-providers-list [data-provider-row]")].map((row) => ({
    id: row.querySelector('[data-pf="id"]')?.value.trim() || "",
    name: row.querySelector('[data-pf="name"]')?.value.trim() || "",
    protocol: row.querySelector('[data-pf="protocol"]')?.value || "openai_images",
    base_url: row.querySelector('[data-pf="base_url"]')?.value.trim() || "",
    model: row.querySelector('[data-pf="model"]')?.value.trim() || "",
    api_key: row.querySelector('[data-pf="api_key"]')?.value.trim() || "",
    size: row.querySelector('[data-pf="size"]')?.value.trim() || "",
    aspect_ratio: row.querySelector('[data-pf="aspect_ratio"]')?.value.trim() || "",
    timeout_s: Number(row.querySelector('[data-pf="timeout_s"]')?.value || 60),
    clear_api_key: Boolean(row.querySelector('[data-pf="clear_api_key"]')?.checked),
  }));
}

async function hydrateImageThumbs(bot) {
  const nodes = [...document.querySelectorAll("#image-root img[data-ref-thumb]")];
  await Promise.all(
    nodes.map(async (img) => {
      const id = img.dataset.refThumb;
      if (!bot || !id) return;
      try {
        const res = await apiGet("image/file", { bot, id, inline: "1" });
        if (res && res.data_url) img.src = res.data_url;
      } catch {
        img.alt = "加载失败";
      }
    }),
  );
}

function downloadDataUrl(dataUrl, filename) {
  const comma = String(dataUrl || "").indexOf(",");
  if (comma < 0) return toast("图片数据无效", true);
  const header = dataUrl.slice(0, comma);
  const mimeMatch = header.match(/data:([^;]+);base64/i);
  const mime = mimeMatch ? mimeMatch[1] : "image/png";
  const binary = atob(dataUrl.slice(comma + 1));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  const blob = new Blob([bytes], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename || "shadow-photo-test.png";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function captureImageTestDraft() {
  const sceneEl = $("img-test-scene");
  if (!sceneEl) return;
  state.imageTestDraft = {
    scene: sceneEl.value,
    recentChat: $("img-test-chat")?.value || "",
    providerId: $("img-test-provider")?.value || "",
    includeStory: Boolean($("img-test-story")?.checked),
    runReview: Boolean($("img-test-review")?.checked),
  };
}

function renderImageTestPage(data) {
  const providers = data.providers || [];
  const draft = state.imageTestDraft;
  const includeStory =
    draft.includeStory === null ? Boolean(data.story_enabled) : Boolean(draft.includeStory);
  const providerOpts = [
    `<option value="">跟随当前机器人默认（${esc(data.bot_provider_id || data.default_provider_id || "自动第一条")}）</option>`,
    ...providers.map(
      (p) =>
        `<option value="${esc(p.id)}" ${p.id === draft.providerId ? "selected" : ""}>${esc(p.name || p.id)} (${esc(p.protocol)})</option>`,
    ),
  ].join("");

  const res = state.imageTestResult;
  let resultHtml = '<p class="muted">填写画面描述并点击「开始测试生图」后，将在此预览成图并提供下载与转译详情。</p>';
  if (res) {
    const refsHtml = (res.refs || []).length
      ? `<div class="test-ref-tags">${res.refs
          .map(
            (r) =>
              `<span class="chip">${r.role === "style" ? "固定画风" : esc(r.category_label)}：${esc(r.name)}</span>`,
          )
          .join("")}</div>`
      : '<span class="muted">未使用参考图</span>';

    let reviewHtml = "";
    if (res.review) {
      const statusMap = { passed: "通过", rejected: "驳回（仅参考，未阻断）", unavailable: "审核模型不可用" };
      const label = statusMap[res.review.status] || res.review.status;
      reviewHtml = `
        <div class="test-review-box">
          <div><b>视觉审核试跑：</b><span class="chip">${esc(label)}</span></div>
          ${res.review.reason ? `<div><b>原因：</b>${esc(res.review.reason)}</div>` : ""}
          ${res.review.fix_hint ? `<div><b>修正建议：</b>${esc(res.review.fix_hint)}</div>` : ""}
          ${res.review.description ? `<div><b>画面描述：</b>${esc(res.review.description)}</div>` : ""}
        </div>`;
    }

    const tmeta = res.translate_meta || {};
    const translateLabel = tmeta.used_vision
      ? `带图转译（附带 ${tmeta.ref_count || 0} 张参考图 · 模式 ${tmeta.mode || "-"}）`
      : tmeta.fallback_reason
        ? `已降级纯文本（${tmeta.fallback_reason}）`
        : `纯文本转译（模式 ${tmeta.mode || "off"}）`;

    resultHtml = `
      <div class="image-test-result">
        <div class="image-test-preview">
          <img class="test-preview-img" src="${res.data_url}" alt="测试生图结果" />
          <div class="row">
            <button id="img-test-download" type="button">下载图片到电脑</button>
            <span class="muted">机器人 ${esc(res.bot_id)} ｜ ${esc(res.provider_name || res.provider_id)}（${esc(res.protocol)}）｜ ${res.latency_ms} ms ｜ ${(res.size / 1024).toFixed(1)} KB${res.aspect_ratio ? " ｜ 比例 " + esc(res.aspect_ratio) : ""}</span>
          </div>
          <div><b>转译方式：</b><span class="chip">${esc(translateLabel)}</span></div>
        </div>
        <div>
          <b>实际附带参考图</b>
          ${refsHtml}
        </div>
        <div>
          <b>转译模型生成的生图提示词</b>
          <pre class="test-code-box">${esc(res.prompt || "")}</pre>
        </div>
        ${reviewHtml}
        <details class="advanced">
          <summary>查看注入给转译模型的原始上下文（可能含故事设定与主持人笔记，展开可能剧透）</summary>
          <pre class="test-code-box">${esc(res.context_text || "（空）")}</pre>
        </details>
      </div>`;
  }

  return `
    <div class="image-section">
      <div class="image-section-head">
        <h3>生图测试（调试模式）</h3>
        <span class="muted">不检查总开关、不扣每日配额、不落盘也不入审核区</span>
      </div>
      ${
        !state.imageBot
          ? '<p class="muted">请先在顶部选择一个机器人环境。</p>'
          : `
        <div class="image-test-form">
          <label>原始画面描述（模拟主对话模型传给拍照工具的描述）
            <textarea id="img-test-scene" placeholder="例如：在窗边木桌上拍一杯冒热气的手冲咖啡，窗外是傍晚的海面">${esc(draft.scene)}</textarea>
          </label>
          <div class="image-test-options">
            <label class="inline-field">本次提供商
              <select id="img-test-provider">${providerOpts}</select>
            </label>
            <label class="check"><input id="img-test-story" type="checkbox" ${includeStory ? "checked" : ""} /> 带入故事上下文</label>
            <label class="check"><input id="img-test-review" type="checkbox" ${draft.runReview ? "checked" : ""} /> 附带试跑一次视觉审核（仅看判定，不阻断不重试）</label>
          </div>
          <label>模拟近期聊天内容（可选，用于测试聊天语境指代）
            <textarea id="img-test-chat" placeholder="留空则不注入模拟聊天；可填如：小明：你现在在喝什么呀">${esc(draft.recentChat)}</textarea>
          </label>
          <div class="row">
            <button id="img-test-run" type="button" ${state.imageTestBusy ? "disabled" : ""}>${state.imageTestBusy ? "转译并生图中..." : "开始测试生图"}</button>
            ${res ? '<button id="img-test-clear" type="button" class="ghost">清空结果</button>' : ""}
          </div>
        </div>`
      }
    </div>
    <div class="image-section">
      <div class="image-section-head">
        <h3>测试结果</h3>
      </div>
      ${resultHtml}
    </div>
  `;
}

async function runImageTest() {
  if (!state.imageBot) return toast("请先选择机器人环境", true);
  captureImageTestDraft();
  const scene = (state.imageTestDraft.scene || "").trim();
  if (!scene) return toast("请输入原始画面描述", true);
  state.imageTestBusy = true;
  if (state.image) renderImage(state.image);
  try {
    const res = await apiPost("image/test", {
      bot: state.imageBot,
      scene,
      provider_id: state.imageTestDraft.providerId || "",
      include_story: Boolean(state.imageTestDraft.includeStory),
      recent_chat: state.imageTestDraft.recentChat || "",
      run_review: Boolean(state.imageTestDraft.runReview),
    });
    state.imageTestResult = res;
    toast(`生图测试完成（${res.latency_ms} ms）`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.imageTestBusy = false;
    if (state.image) renderImage(state.image);
  }
}

function renderImage(data) {
  state.image = data;
  state.imageBot = data.bot || "";
  const botSelect = $("image-bot");
  const bots = data.bots || [];
  botSelect.innerHTML = bots.length
    ? bots
        .map(
          (id) =>
            `<option value="${esc(id)}" ${id === state.imageBot ? "selected" : ""}>${esc(id)}</option>`,
        )
        .join("")
    : '<option value="">（暂无机器人）</option>';
  const toggleBtn = $("image-view-toggle");
  if (toggleBtn) {
    toggleBtn.textContent = state.imageView === "test" ? "← 返回生图管理" : "进入生图测试";
  }
  const usage = data.usage || { used: 0, limit: 20 };
  const pending = data.pending || { total: 0, cap_total: 15, groups: [], full: false };
  $("image-status").textContent =
    `全局：${data.enabled ? "已开启" : "已关闭"} ｜ 今日生图：${usage.used} / ${usage.limit || "不限"} ｜ 待审区：${pending.total} / ${pending.cap_total}${pending.full ? "（已满，暂停自动生成）" : ""}`;

  if (state.imageView === "test") {
    $("image-root").innerHTML = renderImageTestPage(data);
    return;
  }

  const protocols = data.protocols || [];
  const categories = data.categories || [];
  const providers = data.providers || [];
  const defaultSelectOpts = [
    `<option value="">自动（取第一条可用）</option>`,
    ...providers.map(
      (p) =>
        `<option value="${esc(p.id)}" ${p.id === data.default_provider_id ? "selected" : ""}>${esc(p.name || p.id)} (${esc(p.id)})</option>`,
    ),
  ].join("");

  const catOptions = categories
    .map((c) => `<option value="${esc(c.id)}">${esc(c.label)}</option>`)
    .join("");

  const pendingHtml = !state.imageBot
    ? '<p class="muted">请先选择机器人。</p>'
    : !(pending.groups || []).length
      ? '<p class="muted">待审区是空的。故事收场出现新事物时，自动生成的候选参考图会出现在这里。</p>'
      : pending.groups
          .map(
            (group) => `
          <div class="pending-group" data-pending-cat="${esc(group.category)}" data-pending-name="${esc(group.name)}" data-pending-desc="${esc(group.description || "")}">
            <div class="pending-group-head">
              <div>
                <b>[${esc(group.category_label)}] ${esc(group.name)}</b>
                <span class="muted">（候选 ${group.items.length} / ${pending.cap_per_subject || 3}）</span>
                ${group.description ? `<div class="muted">${esc(group.description)}</div>` : ""}
              </div>
              <button type="button" class="ghost" data-pending-regen ${group.items.length >= (pending.cap_per_subject || 3) || pending.full ? "disabled" : ""}>再生成一张</button>
            </div>
            <div class="ref-grid">
              ${group.items
                .map(
                  (item) => `
                <div class="ref-card" data-ref-id="${item.id}">
                  <img class="ref-thumb" data-ref-thumb="${item.id}" alt="${esc(item.name)}" />
                  <div class="muted" title="${esc(item.prompt || "")}">${esc((item.prompt || "").slice(0, 80))}${(item.prompt || "").length > 80 ? "..." : ""}</div>
                  <div class="row">
                    <button type="button" data-pending-approve="${item.id}">采用此图</button>
                    <button type="button" class="danger ghost" data-pending-reject="${item.id}">驳回</button>
                  </div>
                </div>`,
                )
                .join("")}
            </div>
          </div>`,
          )
          .join("");

  const activeRefs = data.active_refs || [];
  const activeHtml = !state.imageBot
    ? '<p class="muted">请先选择机器人。</p>'
    : !activeRefs.length
      ? '<p class="muted">暂无正式参考图。可在上方上传画风、角色本人、人物、场景或物件参考图。</p>'
      : `<div class="ref-grid">${activeRefs
          .map(
            (item) => `
          <div class="ref-card" data-active-ref="${item.id}">
            <img class="ref-thumb" data-ref-thumb="${item.id}" alt="${esc(item.name)}" />
            <div><span class="chip">${esc(item.category_label)}</span> <b>${esc(item.name)}</b></div>
            <input type="text" data-ref-edit-name value="${esc(item.name)}" placeholder="名称" />
            <input type="text" data-ref-edit-desc value="${esc(item.description || "")}" placeholder="外观与特征说明（供转译模型参考）" />
            <div class="row">
              <button type="button" class="ghost" data-ref-save="${item.id}">保存说明</button>
              <button type="button" class="danger ghost" data-ref-delete="${item.id}">删除</button>
            </div>
          </div>`,
          )
          .join("")}</div>`;

  $("image-root").innerHTML = `
    <div class="image-section">
      <div class="image-section-head">
        <h3>1. 待审区（${pending.total} / ${pending.cap_total}）</h3>
        ${pending.full ? '<span class="warn">审核区已满，已暂停自动生成，请先处理</span>' : ""}
      </div>
      ${pendingHtml}
    </div>

    <div class="image-section">
      <div class="image-section-head">
        <h3>2. 正式参考图库（${activeRefs.length} 张）</h3>
      </div>
      <div class="upload-form">
        <label>分类<select id="ref-upload-cat">${catOptions}</select></label>
        <label>名称<input id="ref-upload-name" type="text" placeholder="如：画风1 / 本人常服 / 咖啡馆" /></label>
        <label>外观特征说明<input id="ref-upload-desc" type="text" placeholder="给转译模型看的视觉要点（可选）" /></label>
        <label>选择图片<input id="ref-upload-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif" /></label>
        <button id="ref-upload-btn" type="button" ${!state.imageBot ? "disabled" : ""}>上传参考图</button>
      </div>
      ${activeHtml}
    </div>

    <div class="image-section">
      <div class="image-section-head">
        <h3>3. 生图提供商档案（密钥仅存插件配置）</h3>
        <div class="row">
          <label class="check"><input id="image-master-enabled" type="checkbox" ${data.enabled ? "checked" : ""} /> 启用生图</label>
          <button id="image-provider-add" type="button" class="ghost">新增提供商</button>
          <button id="image-provider-save" type="button">保存提供商配置</button>
        </div>
      </div>
      <div class="row" style="margin-bottom:10px;">
        <label class="inline-field">默认提供商
          <select id="image-default-provider">${defaultSelectOpts}</select>
        </label>
      </div>
      <div id="image-providers-list">
        ${providers.length ? providers.map((p) => providerCardHtml(p, protocols)).join("") : '<p class="muted">还没有配置生图提供商，点「新增提供商」添加 OpenAI / Gemini 生图接口。</p>'}
      </div>
    </div>
  `;
  void hydrateImageThumbs(state.imageBot);
}

async function loadImage(bot) {
  try {
    const data = await apiGet("image", bot ? { bot } : {});
    renderImage(data);
  } catch (error) {
    toast(error.message, true);
  }
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("读取图片文件失败"));
    reader.readAsDataURL(file);
  });
}

async function uploadImageRef() {
  if (!state.imageBot) return toast("请先选择机器人", true);
  const file = $("ref-upload-file")?.files?.[0];
  if (!file) return toast("请先选择一张图片", true);
  try {
    $("ref-upload-btn").disabled = true;
    const dataUrl = await readFileAsDataUrl(file);
    await apiPost("image/ref/upload", {
      bot: state.imageBot,
      category: $("ref-upload-cat").value,
      name: $("ref-upload-name").value.trim(),
      description: $("ref-upload-desc").value.trim(),
      data_base64: dataUrl,
    });
    toast("参考图已上传");
    await loadImage(state.imageBot);
  } catch (error) {
    toast(error.message, true);
  } finally {
    const btn = $("ref-upload-btn");
    if (btn) btn.disabled = false;
  }
}

async function saveImageProviders() {
  try {
    const providers = collectProviderRows();
    await apiPost("image/providers/save", {
      enabled: Boolean($("image-master-enabled")?.checked),
      default_provider_id: $("image-default-provider")?.value || "",
      providers,
    });
    toast("生图提供商配置已保存");
    await loadImage(state.imageBot);
  } catch (error) {
    toast(error.message, true);
  }
}

async function testImageProvider(row, btn) {
  const payload = {
    id: row.querySelector('[data-pf="id"]')?.value.trim() || "",
    name: row.querySelector('[data-pf="name"]')?.value.trim() || "",
    protocol: row.querySelector('[data-pf="protocol"]')?.value || "openai_images",
    base_url: row.querySelector('[data-pf="base_url"]')?.value.trim() || "",
    model: row.querySelector('[data-pf="model"]')?.value.trim() || "",
    api_key: row.querySelector('[data-pf="api_key"]')?.value.trim() || "",
    size: row.querySelector('[data-pf="size"]')?.value.trim() || "",
    aspect_ratio: row.querySelector('[data-pf="aspect_ratio"]')?.value.trim() || "",
    timeout_s: Number(row.querySelector('[data-pf="timeout_s"]')?.value || 60),
  };
  try {
    btn.disabled = true;
    btn.textContent = "测试中...";
    const res = await apiPost("image/providers/test", { provider: payload });
    if (res.ok) {
      toast(`测试成功（${res.latency_ms} ms，${res.mime}，${(res.size / 1024).toFixed(1)} KB）`);
    } else {
      toast(`测试失败：${res.error || "未知错误"}`, true);
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "测试连通性";
  }
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
    if (target.closest("[data-story-spoiler]")) {
      void openSpoiler(storyCard, bot);
      return;
    }
    if (target.closest("[data-spoiler-confirm]")) {
      state.spoilerConfirmed = true;
      void openSpoiler(storyCard, bot);
      return;
    }
    if (target.closest("[data-row-remove]")) {
      const row = target.closest(".entry-row, .hook-row");
      if (row) row.remove();
      return;
    }
    if (target.closest("[data-entry-add]")) {
      const categories = JSON.parse(storyCard.dataset.categories || "[]");
      const options = categories
        .map((item) => `<option value="${esc(item.id)}">${esc(item.label)}</option>`)
        .join("");
      storyCard.querySelector(".seed-entries").insertAdjacentHTML("beforeend", entryRow(options, null));
      return;
    }
    if (target.closest("[data-hook-add]")) {
      storyCard.querySelector(".seed-hooks").insertAdjacentHTML("beforeend", hookRow(""));
      return;
    }
    if (target.closest("[data-seed-save]")) {
      void saveStorySeed(storyCard, bot);
      return;
    }
    if (target.closest("[data-world-classify]")) {
      void classifyStoryWorld(storyCard, bot);
      return;
    }
    const visBtn = target.closest("[data-canon-vis]");
    if (visBtn) {
      void toggleCanonVisibility(storyCard, bot, visBtn);
      return;
    }
    if (target.closest("[data-note-save]")) {
      void saveStoryNote(storyCard, bot, target.closest(".note-row"));
      return;
    }
    if (target.closest("[data-note-delete]")) {
      void deleteStoryNote(storyCard, bot, target.closest(".note-row"));
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
    return;
  }
  if (target.closest("#image-view-toggle")) {
    if (state.imageView === "test") {
      captureImageTestDraft();
      state.imageView = "manage";
    } else {
      state.imageView = "test";
    }
    if (state.image) renderImage(state.image);
    return;
  }
  if (target.closest("#img-test-run")) {
    void runImageTest();
    return;
  }
  if (target.closest("#img-test-clear")) {
    captureImageTestDraft();
    state.imageTestResult = null;
    if (state.image) renderImage(state.image);
    return;
  }
  if (target.closest("#img-test-download")) {
    const res = state.imageTestResult;
    if (res && res.data_url) {
      downloadDataUrl(res.data_url, res.filename);
    }
    return;
  }
  if (target.closest("#ref-upload-btn")) {
    void uploadImageRef();
    return;
  }
  if (target.closest("#image-provider-add")) {
    const list = $("image-providers-list");
    if (list) {
      const empty = list.querySelector("p.muted");
      if (empty) empty.remove();
      list.insertAdjacentHTML(
        "beforeend",
        providerCardHtml(
          { protocol: "openai_images", timeout_s: 60 },
          state.image?.protocols || [],
        ),
      );
    }
    return;
  }
  if (target.closest("#image-provider-save")) {
    void saveImageProviders();
    return;
  }
  const testBtn = target.closest("[data-provider-test]");
  if (testBtn) {
    const row = testBtn.closest("[data-provider-row]");
    if (row) void testImageProvider(row, testBtn);
    return;
  }
  if (target.closest("[data-provider-remove]")) {
    target.closest("[data-provider-row]")?.remove();
    return;
  }
  const approveBtn = target.closest("[data-pending-approve]");
  if (approveBtn) {
    const id = Number(approveBtn.dataset.pendingApprove || 0);
    if (id && state.imageBot) {
      apiPost("image/pending/approve", { bot: state.imageBot, id })
        .then(() => {
          toast("已采用该参考图并清理其余候选");
          return loadImage(state.imageBot);
        })
        .catch((err) => toast(err.message, true));
    }
    return;
  }
  const rejectBtn = target.closest("[data-pending-reject]");
  if (rejectBtn) {
    const id = Number(rejectBtn.dataset.pendingReject || 0);
    if (id && state.imageBot) {
      apiPost("image/pending/reject", { bot: state.imageBot, id })
        .then(() => {
          toast("已驳回该候选图");
          return loadImage(state.imageBot);
        })
        .catch((err) => toast(err.message, true));
    }
    return;
  }
  const regenBtn = target.closest("[data-pending-regen]");
  if (regenBtn && !regenBtn.disabled) {
    const group = regenBtn.closest(".pending-group");
    if (group && state.imageBot) {
      regenBtn.disabled = true;
      apiPost("image/pending/regenerate", {
        bot: state.imageBot,
        category: group.dataset.pendingCat,
        name: group.dataset.pendingName,
        description: group.dataset.pendingDesc,
      })
        .then(() => {
          toast("已生成一张新候选参考图");
          return loadImage(state.imageBot);
        })
        .catch((err) => toast(err.message, true))
        .finally(() => {
          regenBtn.disabled = false;
        });
    }
    return;
  }
  const refSaveBtn = target.closest("[data-ref-save]");
  if (refSaveBtn) {
    const card = refSaveBtn.closest("[data-active-ref]");
    const id = Number(refSaveBtn.dataset.refSave || 0);
    if (card && id && state.imageBot) {
      apiPost("image/ref/update", {
        bot: state.imageBot,
        id,
        name: card.querySelector("[data-ref-edit-name]")?.value.trim() || "",
        description: card.querySelector("[data-ref-edit-desc]")?.value.trim() || "",
      })
        .then(() => {
          toast("参考图信息已更新");
          return loadImage(state.imageBot);
        })
        .catch((err) => toast(err.message, true));
    }
    return;
  }
  const refDelBtn = target.closest("[data-ref-delete]");
  if (refDelBtn) {
    const id = Number(refDelBtn.dataset.refDelete || 0);
    if (id && state.imageBot && window.confirm("确定删除这张参考图吗？")) {
      apiPost("image/ref/delete", { bot: state.imageBot, id })
        .then(() => {
          toast("参考图已删除");
          return loadImage(state.imageBot);
        })
        .catch((err) => toast(err.message, true));
    }
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
  $("config-save").addEventListener("click", () => saveConfig());
  $("config-root").addEventListener("click", onConfigClick);
  $("config-root").addEventListener("change", onConfigChange);
  $("preview-btn").addEventListener("click", previewTransfer);
  $("transfer-run").addEventListener("click", runTransfer);
  $("transfer-cancel").addEventListener("click", () => $("transfer-dialog").close());
  $("import-run").addEventListener("click", runImport);
  $("import-cancel").addEventListener("click", () => $("import-dialog").close());
  $("transfer-target").addEventListener("change", toggleConfirm);
  $("image-bot").addEventListener("change", (event) => {
    if (state.imageView === "test") {
      captureImageTestDraft();
      state.imageTestDraft.includeStory = null;
    }
    void loadImage(event.target.value);
  });
  document.querySelectorAll('input[name="mode"]').forEach((node) => {
    node.addEventListener("change", toggleConfirm);
  });
  toggleConfirm();
  await loadOverview();
}

boot();
