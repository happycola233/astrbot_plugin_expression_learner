const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

const state = {
  group: "",
  q: "",
  sort: "learned",
  featured: false,
  page: 1,
  size: 30,
  total: 0,
  overview: null,
  editing: null,
};

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch],
  );
}

function formatTime(seconds) {
  if (!seconds) return "—";
  const diff = Date.now() / 1000 - seconds;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  const date = new Date(seconds * 1000);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

let toastTimer = null;
function toast(message, isError = false) {
  const box = $("toast");
  box.textContent = message;
  box.classList.toggle("error", isError);
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.hidden = true;
  }, 3200);
}

// The page runs in a sandboxed iframe where confirm() is not allowed, so
// destructive buttons need a second click within a few seconds instead.
function armed(button, label) {
  if (button.dataset.armed === "1") {
    delete button.dataset.armed;
    button.classList.remove("armed");
    button.textContent = button.dataset.label;
    return true;
  }
  button.dataset.label = button.textContent;
  button.dataset.armed = "1";
  button.classList.add("armed");
  button.textContent = label;
  setTimeout(() => {
    if (button.dataset.armed === "1") {
      delete button.dataset.armed;
      button.classList.remove("armed");
      button.textContent = button.dataset.label;
    }
  }, 4000);
  return false;
}

function currentGroup() {
  return state.overview?.groups.find((group) => group.group_id === state.group) || null;
}

function renderOverview() {
  const overview = state.overview;
  const select = $("group");
  select.innerHTML = overview.groups
    .map(
      (group) =>
        `<option value="${escapeHtml(group.group_id)}">${escapeHtml(group.group_id)}${
          group.learning_enabled ? "" : "（已不在学习名单）"
        }</option>`,
    )
    .join("");
  if (!overview.groups.some((group) => group.group_id === state.group)) {
    state.group = overview.groups[0]?.group_id || "";
  }
  select.value = state.group;

  const group = currentGroup();
  const stats = group || { total: 0, featured: 0, new_7d: 0, unlearned: 0, last_learn: 0 };
  const cards = [
    ["已学到", stats.total],
    ["精选", stats.featured],
    ["近 7 天新学", stats.new_7d],
    ["待学消息", stats.unlearned],
    ["上次学习", group?.learning ? "学习中…" : formatTime(stats.last_learn)],
  ];
  $("stats").innerHTML = cards
    .map(
      ([label, value]) =>
        `<div class="stat"><div class="label">${label}</div><div class="value">${escapeHtml(value)}</div></div>`,
    )
    .join("");

  const { embedding, settings } = overview;
  let embeddingLine;
  if (embedding.active) {
    embeddingLine = `嵌入模型：<strong>${escapeHtml(embedding.configured)}</strong>，按意思合并相似的说法、按意思挑选。`;
    if (group?.rebuilding) {
      embeddingLine += ` 正在重建向量，已完成 ${escapeHtml(group.rebuilding)} 条。`;
    } else if (group?.no_vector) {
      embeddingLine += ` 有 ${group.no_vector} 条还没有向量，挑选时用不上，点「重建向量」补上。`;
    }
  } else if (embedding.configured) {
    embeddingLine = `嵌入模型：找不到 <strong>${escapeHtml(embedding.configured)}</strong>，现在按文字相似度合并、随机挑选。`;
  } else {
    embeddingLine = "嵌入模型：没配，按文字相似度合并、随机挑选。";
  }
  if (!embedding.active && embedding.available.length) {
    embeddingLine += ` 能用的嵌入模型提供商 ID：${embedding.available.map((id) => `<strong>${escapeHtml(id)}</strong>`).join("、")}，填进插件配置的「嵌入模型提供商 ID」。`;
  }
  const settingsLine = `每次回复带 <strong>${settings.pick_count}</strong> 条 · 每攒 <strong>${settings.batch_size}</strong> 条新消息学一次 · 只用精选：<strong>${
    settings.featured_only ? "是" : "否"
  }</strong> · 私聊里也用：<strong>${settings.use_in_private ? "是" : "否"}</strong>（在插件配置里改）`;
  $("status").innerHTML = `${embeddingLine}<br />${settingsLine}`;

  $("learn").disabled = !group?.learning_enabled || group?.learning;
  $("learn").textContent = group?.learning ? "学习中…" : "立即学习";
  $("rebuild").hidden = !embedding.active || !group || (!group.no_vector && !group.rebuilding);
  $("rebuild").disabled = Boolean(group?.rebuilding);
  $("clear").disabled = !group || !group.total;
}

function rowHtml(item) {
  const editing = state.editing === item.id;
  const examples = item.examples.map((text) => `<div>${escapeHtml(text)}</div>`).join("");
  const situation = editing
    ? `<input data-field="situation" maxlength="40" value="${escapeHtml(item.situation)}" />`
    : escapeHtml(item.situation);
  const style = editing
    ? `<input data-field="style" maxlength="40" value="${escapeHtml(item.style)}" />`
    : escapeHtml(item.style);
  const ops = editing
    ? `<button class="link" data-op="save">保存</button><button class="link" data-op="cancel">取消</button>`
    : `<button class="link" data-op="edit">编辑</button><button class="link danger" data-op="delete">删除</button>`;
  return `<tr data-id="${item.id}">
    <td class="col-star"><button class="star ${item.featured ? "on" : ""}" data-op="star" title="${
      item.featured ? "取消精选" : "设为精选"
    }">${item.featured ? "★" : "☆"}</button></td>
    <td>${situation}</td>
    <td>${style}</td>
    <td class="example">${examples || "—"}</td>
    <td class="num">${item.count}</td>
    <td class="num">${item.use_count}</td>
    <td class="time" title="${new Date(item.last_learned * 1000).toLocaleString()}">${formatTime(item.last_learned)}</td>
    <td>${ops}</td>
  </tr>`;
}

let items = [];
function renderRows() {
  $("rows").innerHTML = items.map(rowHtml).join("");
  $("empty").hidden = items.length > 0;
  const pages = Math.max(1, Math.ceil(state.total / state.size));
  $("page-info").textContent = `第 ${state.page} / ${pages} 页，共 ${state.total} 条`;
  $("prev").disabled = state.page <= 1;
  $("next").disabled = state.page >= pages;
}

async function loadOverview() {
  state.overview = await bridge.apiGet("overview");
  renderOverview();
}

async function loadList() {
  if (!state.group) {
    items = [];
    state.total = 0;
    renderRows();
    return;
  }
  const result = await bridge.apiGet("list", {
    group: state.group,
    q: state.q,
    sort: state.sort,
    featured: state.featured ? 1 : 0,
    page: state.page,
    size: state.size,
  });
  items = result.items;
  state.total = result.total;
  if (!items.length && state.page > 1) {
    state.page -= 1;
    return loadList();
  }
  renderRows();
}

async function refresh() {
  try {
    await loadOverview();
    await loadList();
  } catch (error) {
    toast(error.message || String(error), true);
  }
}

async function run(task, success) {
  try {
    const result = await task();
    if (success) toast(typeof success === "function" ? success(result) : success);
  } catch (error) {
    toast(error.message || String(error), true);
  }
  await refresh();
}

$("group").addEventListener("change", (event) => {
  state.group = event.target.value;
  state.page = 1;
  state.editing = null;
  refresh();
});

$("refresh").addEventListener("click", refresh);

let searchTimer = null;
$("search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = event.target.value.trim();
    state.page = 1;
    loadList().catch((error) => toast(error.message, true));
  }, 300);
});

$("sort").addEventListener("change", (event) => {
  state.sort = event.target.value;
  state.page = 1;
  loadList().catch((error) => toast(error.message, true));
});

$("featured").addEventListener("change", (event) => {
  state.featured = event.target.checked;
  state.page = 1;
  loadList().catch((error) => toast(error.message, true));
});

$("prev").addEventListener("click", () => {
  state.page -= 1;
  loadList().catch((error) => toast(error.message, true));
});

$("next").addEventListener("click", () => {
  state.page += 1;
  loadList().catch((error) => toast(error.message, true));
});

$("learn").addEventListener("click", () => {
  $("learn").disabled = true;
  $("learn").textContent = "学习中…";
  run(
    () => bridge.apiPost("learn", { group: state.group }),
    (result) =>
      result.messages
        ? `读了 ${result.messages} 条消息，新学 ${result.added} 条，合并 ${result.merged} 条`
        : "没有待学的消息",
  );
});

$("rebuild").addEventListener("click", () => {
  run(() => bridge.apiPost("rebuild", { group: state.group }), "开始重建向量，过一会儿点「刷新」看进度");
});

$("clear").addEventListener("click", (event) => {
  if (!armed(event.currentTarget, "再点一次，清空本群全部表达")) return;
  run(
    () => bridge.apiPost("clear", { group: state.group }),
    (result) => `已清空 ${result.deleted} 条`,
  );
});

$("rows").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-op]");
  if (!button) return;
  const row = button.closest("tr");
  const id = Number(row.dataset.id);
  const item = items.find((entry) => entry.id === id);
  const op = button.dataset.op;
  if (op === "star") {
    run(() => bridge.apiPost("update", { id, featured: !item.featured }));
  } else if (op === "edit") {
    state.editing = id;
    renderRows();
    row.parentElement.querySelector(`tr[data-id="${id}"] input`)?.focus();
  } else if (op === "cancel") {
    state.editing = null;
    renderRows();
  } else if (op === "save") {
    const situation = row.querySelector('input[data-field="situation"]').value.trim();
    const style = row.querySelector('input[data-field="style"]').value.trim();
    state.editing = null;
    run(() => bridge.apiPost("update", { id, situation, style }), "已保存");
  } else if (op === "delete") {
    if (!armed(button, "确定？")) return;
    run(() => bridge.apiPost("delete", { ids: [id] }), "已删除");
  }
});

await bridge.ready();
await refresh();
