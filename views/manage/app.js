const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

// Which time each sort order shows in the last column.
const TIME_COLUMN = {
  learned: ["last_learned", "最近学到"],
  count: ["last_learned", "最近学到"],
  used: ["last_used", "最近带上"],
  created: ["created_at", "首次学到"],
};

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
  selected: new Set(),
  loaded: false,
};

let items = [];

// ---- helpers ----

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch],
  );
}

// Escape text and wrap the current search term in <mark>.
function highlight(text) {
  const raw = String(text ?? "");
  if (!state.q) return escapeHtml(raw);
  const needle = state.q.toLowerCase();
  const lower = raw.toLowerCase();
  let out = "";
  let from = 0;
  for (let at = lower.indexOf(needle); at !== -1; at = lower.indexOf(needle, from)) {
    out += `${escapeHtml(raw.slice(from, at))}<mark>${escapeHtml(raw.slice(at, at + needle.length))}</mark>`;
    from = at + needle.length;
  }
  return out + escapeHtml(raw.slice(from));
}

function icon(name, extra = "") {
  return `<svg class="ico ${extra}"><use href="#i-${name}" /></svg>`;
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

function fullTime(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
}

function errorText(error) {
  return error?.message || String(error);
}

let toastTimer = null;
function toast(message, isError = false) {
  const box = $("toast");
  box.innerHTML = `${icon(isError ? "alert" : "check")}<span>${escapeHtml(message)}</span>`;
  box.classList.toggle("error", isError);
  // Restart the entry animation when a toast replaces another.
  box.hidden = true;
  void box.offsetWidth;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.hidden = true;
  }, isError ? 5000 : 3000);
}

// The page runs in a sandboxed iframe where confirm() is blocked, so
// destructive actions ask through an in-page <dialog> instead.
function confirmDialog({ title, body, ok }) {
  const dialog = $("dialog");
  $("dialog-title").textContent = title;
  $("dialog-body").innerHTML = body;
  $("dialog-ok").textContent = ok;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}

$("dialog").addEventListener("click", (event) => {
  const dialog = $("dialog");
  const button = event.target.closest("button[data-value]");
  if (button) {
    dialog.close(button.dataset.value);
  } else if (event.target === dialog) {
    // A click on the backdrop lands on the <dialog> element itself.
    const box = dialog.getBoundingClientRect();
    const inside =
      event.clientX >= box.left && event.clientX <= box.right && event.clientY >= box.top && event.clientY <= box.bottom;
    if (!inside) dialog.close("cancel");
  }
});

function currentGroup() {
  return state.overview?.groups.find((group) => group.group_id === state.group) || null;
}

// ---- overview ----

function renderStats() {
  const group = currentGroup();
  const settings = state.overview.settings;
  const stats = group || { total: 0, featured: 0, new_7d: 0, unlearned: 0, last_learn: 0 };
  const batch = Math.max(1, settings.batch_size);
  const ratio = Math.min(1, stats.unlearned / batch);
  let note;
  if (!group?.learning_enabled) note = "不在学习的群里，不会再学";
  else if (group.learning) note = "正在学…";
  else if (stats.unlearned >= batch) note = "已攒够，下条消息后自动学";
  else note = `再攒 ${batch - stats.unlearned} 条自动学一次`;

  const card = (label, value, extra = "", title = "") =>
    `<div class="stat${extra ? " wide" : ""}"${title ? ` title="${escapeHtml(title)}"` : ""}>
      <div class="stat-label">${label}</div>
      <div class="stat-value">${value}</div>${extra}
    </div>`;

  $("stats").innerHTML = [
    card("已学到", stats.total),
    card(`${icon("star", "star-ico")}精选`, stats.featured),
    card("近 7 天新学", stats.new_7d),
    card(
      "待学消息",
      `${stats.unlearned}<small>/ ${batch}</small>`,
      `<div class="stat-meter${ratio >= 1 ? " full" : ""}"><span style="width:${(ratio * 100).toFixed(1)}%"></span></div>
       <div class="stat-note">${note}</div>`,
    ),
    card(
      "上次学习",
      group?.learning ? "学习中…" : stats.last_learn ? formatTime(stats.last_learn) : "还没学过",
      "",
      fullTime(stats.last_learn),
    ),
  ].join("");
}

function renderOverview() {
  const overview = state.overview;
  const select = $("group");
  select.innerHTML = overview.groups.length
    ? overview.groups
        .map(
          (group) =>
            `<option value="${escapeHtml(group.group_id)}">${escapeHtml(group.group_id)}${
              group.learning_enabled ? "" : "（已不在学习名单）"
            }</option>`,
        )
        .join("")
    : `<option value="">还没有群</option>`;
  if (!overview.groups.some((group) => group.group_id === state.group)) {
    state.group = overview.groups[0]?.group_id || "";
  }
  select.value = state.group;
  select.disabled = !overview.groups.length;

  renderStats();

  const group = currentGroup();
  const { embedding, settings } = overview;
  if (embedding.active) {
    $("embedding").innerHTML = `<span class="dot ok"></span><span class="k">嵌入模型</span><strong>${escapeHtml(
      embedding.configured,
    )}</strong><span class="desc">按意思合并相似的说法、按意思挑选</span>`;
  } else if (embedding.configured) {
    $("embedding").innerHTML = `<span class="dot warn"></span><span class="k">嵌入模型</span><strong>${escapeHtml(
      embedding.configured,
    )}</strong><span class="tag">找不到</span><span class="desc">现在按文字相似度合并、随机挑选</span>`;
  } else {
    $("embedding").innerHTML = `<span class="dot"></span><span class="k">嵌入模型</span><span class="desc">没配，按文字相似度合并、随机挑选</span>`;
  }

  const yesNo = (value) => (value ? "是" : "否");
  $("settings").innerHTML = `
    <span class="chip">每次回复带 <b>${settings.pick_count}</b> 条</span>
    <span class="chip">每攒 <b>${settings.batch_size}</b> 条新消息学一次</span>
    <span class="chip">只用精选 <b>${yesNo(settings.featured_only)}</b></span>
    <span class="chip">私聊里也用 <b>${yesNo(settings.use_in_private)}</b></span>
    <span class="hint">在插件配置里改</span>`;

  let notice = "";
  let info = false;
  if (group?.rebuilding) {
    info = true;
    notice = `正在重建向量，已完成 <strong>${escapeHtml(group.rebuilding)}</strong> 条…`;
  } else if (embedding.active && group?.no_vector) {
    notice = `有 <strong>${group.no_vector}</strong> 条还没有向量，挑选时用不上。点「重建向量」补上。`;
  } else if (!embedding.active && embedding.available.length) {
    notice = `能用的嵌入模型提供商 ID：${embedding.available
      .map((id) => `<code>${escapeHtml(id)}</code>`)
      .join("、")}，填进插件配置的「嵌入模型提供商 ID」。`;
  } else if (group && !group.learning_enabled) {
    notice = "这个群已经不在「学习的群」里：不会再学，回复时也不会用到这里的说法。";
  } else if (!overview.groups.length) {
    notice = "还没有要学习的群。先在插件配置的「学习的群」里填上群号。";
  }
  $("notice").hidden = !notice;
  $("notice").classList.toggle("info", info);
  $("notice-text").innerHTML = notice;
  $("notice").querySelector("use").setAttribute("href", info ? "#i-refresh" : "#i-alert");
  $("notice").querySelector(".ico").classList.toggle("spin", info);

  const learn = $("learn");
  learn.disabled = !group?.learning_enabled || group.learning;
  learn.classList.toggle("busy", Boolean(group?.learning));
  learn.querySelector("span").textContent = group?.learning ? "学习中…" : "立即学习";
  learn.title = group && !group.learning_enabled ? "这个群不在「学习的群」里" : "把攒下的新消息马上学一次";

  const rebuild = $("rebuild");
  rebuild.hidden = !embedding.active || !group || (!group.no_vector && !group.rebuilding);
  rebuild.disabled = Boolean(group?.rebuilding);
  rebuild.textContent = group?.rebuilding ? "重建中…" : "重建向量";

  $("clear").disabled = !group?.total;
}

// ---- list ----

function rowHtml(item) {
  const editing = state.editing === item.id;
  const selected = state.selected.has(item.id);
  const [timeKey] = TIME_COLUMN[state.sort];
  const time = item[timeKey];
  const examples = item.examples.length
    ? `<ul class="examples">${item.examples
        .map((text) => `<li title="${escapeHtml(text)}">${highlight(text)}</li>`)
        .join("")}</ul>`
    : `<span class="none">—</span>`;
  const situation = editing
    ? `<input class="cell-input" data-field="situation" maxlength="40" value="${escapeHtml(item.situation)}" aria-label="情境" />`
    : highlight(item.situation);
  const style = editing
    ? `<input class="cell-input" data-field="style" maxlength="40" value="${escapeHtml(item.style)}" aria-label="表达" />`
    : highlight(item.style);
  const ops = editing
    ? `<button class="op save" data-op="save" type="button" title="保存（Enter）" aria-label="保存">${icon("check")}</button>
       <button class="op" data-op="cancel" type="button" title="取消（Esc）" aria-label="取消">${icon("x")}</button>`
    : `<button class="op" data-op="edit" type="button" title="编辑" aria-label="编辑">${icon("edit")}</button>
       <button class="op del" data-op="delete" type="button" title="删除" aria-label="删除">${icon("trash")}</button>`;
  const classes = [editing && "editing", selected && "selected"].filter(Boolean).join(" ");
  return `<tr data-id="${item.id}"${classes ? ` class="${classes}"` : ""}>
    <td class="c-check"><label class="hit"><input type="checkbox" data-op="select" aria-label="选择这一条"${
      selected ? " checked" : ""
    } /></label></td>
    <td class="c-star"><button class="star${item.featured ? " on" : ""}" data-op="star" type="button" aria-pressed="${Boolean(
      item.featured,
    )}" title="${item.featured ? "取消精选" : "设为精选"}">${icon("star")}</button></td>
    <td class="c-situation">${situation}</td>
    <td class="c-style">${style}</td>
    <td class="c-example">${examples}</td>
    <td class="c-num c-count${item.count ? "" : " zero"}">${item.count}</td>
    <td class="c-num c-used${item.use_count ? "" : " zero"}">${item.use_count}</td>
    <td class="c-time" title="${time ? fullTime(time) : "还没带上过"}">${time ? formatTime(time) : "—"}</td>
    <td class="c-ops"><div class="ops">${ops}</div></td>
  </tr>`;
}

function emptyHtml() {
  const batch = state.overview?.settings.batch_size ?? 40;
  let title;
  let hint;
  if (!state.loaded) {
    title = "加载中…";
    hint = "";
  } else if (!state.group) {
    title = "还没有要学习的群";
    hint = "在插件配置的「学习的群」里填上群号，群里聊起来就会开始学。";
  } else if (state.q) {
    title = `没有找到「${state.q}」`;
    hint = "换个词试试，搜索会匹配情境、表达和例句。";
  } else if (state.featured) {
    title = "还没有精选";
    hint = "点每一条前面的星标设为精选。打开「只用精选的」后，回复时只从精选里挑。";
  } else {
    title = "还没学到东西";
    hint = `群里每攒够 ${batch} 条新消息会自动学一次，也可以点「立即学习」。`;
  }
  return `<div class="empty-icon">${icon(state.q ? "search" : "sparkle", state.loaded ? "" : "spin")}</div><strong>${escapeHtml(
    title,
  )}</strong>${hint ? `<span>${escapeHtml(hint)}</span>` : ""}`;
}

function renderSelection() {
  const count = state.selected.size;
  $("selection").hidden = count === 0;
  $("selected-count").textContent = count;
  const all = $("check-all");
  all.checked = items.length > 0 && count === items.length;
  all.indeterminate = count > 0 && count < items.length;
  all.disabled = items.length === 0;
}

function renderRows() {
  $("time-head").textContent = TIME_COLUMN[state.sort][1];
  $("rows").innerHTML = items.map(rowHtml).join("");
  const empty = items.length === 0;
  document.querySelector(".grid").hidden = empty;
  $("empty").hidden = !empty;
  if (empty) $("empty").innerHTML = emptyHtml();

  const pages = Math.max(1, Math.ceil(state.total / state.size));
  $("page-info").textContent = pages > 1 ? `第 ${state.page} / ${pages} 页 · 共 ${state.total} 条` : `共 ${state.total} 条`;
  $("prev").hidden = $("next").hidden = pages <= 1;
  $("prev").disabled = state.page <= 1;
  $("next").disabled = state.page >= pages;
  renderSelection();
}

// ---- loading ----

async function loadOverview() {
  state.overview = await bridge.apiGet("overview");
  renderOverview();
  schedulePoll();
}

let listRequest = 0;
async function loadList() {
  const ticket = ++listRequest;
  if (!state.group) {
    items = [];
    state.total = 0;
    state.loaded = true;
    renderRows();
    return;
  }
  $("list").classList.add("loading");
  try {
    const result = await bridge.apiGet("list", {
      group: state.group,
      q: state.q,
      sort: state.sort,
      featured: state.featured ? 1 : 0,
      page: state.page,
      size: state.size,
    });
    if (ticket !== listRequest) return;
    if (!result.items.length && state.page > 1) {
      state.page -= 1;
      return loadList();
    }
    items = result.items;
    state.total = result.total;
    state.loaded = true;
    // Keep only selections that are still on this page.
    const ids = new Set(items.map((item) => item.id));
    state.selected = new Set([...state.selected].filter((id) => ids.has(id)));
    renderRows();
  } finally {
    if (ticket === listRequest) $("list").classList.remove("loading");
  }
}

async function refresh() {
  const button = $("refresh");
  button.classList.add("busy");
  try {
    await loadOverview();
    await loadList();
  } catch (error) {
    toast(errorText(error), true);
  } finally {
    button.classList.remove("busy");
  }
}

function reloadList() {
  loadList().catch((error) => toast(errorText(error), true));
}

// While a group is learning or rebuilding vectors, poll the overview so the
// progress shows without pressing refresh, then reload the list when done.
let pollTimer = null;
let wasBusy = false;
function schedulePoll() {
  clearTimeout(pollTimer);
  const busy = state.overview.groups.some((group) => group.learning || group.rebuilding);
  if (wasBusy && !busy) reloadList();
  wasBusy = busy;
  if (busy) {
    pollTimer = setTimeout(() => loadOverview().catch(() => schedulePoll()), 2500);
  }
}

function resetView() {
  state.page = 1;
  state.editing = null;
  state.selected.clear();
}

// ---- toolbar ----

$("group").addEventListener("change", (event) => {
  state.group = event.target.value;
  resetView();
  refresh();
});

$("refresh").addEventListener("click", refresh);

let searchTimer = null;
$("search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = event.target.value.trim();
    resetView();
    reloadList();
  }, 250);
});

$("search").addEventListener("keydown", (event) => {
  if (event.key === "Escape" && event.target.value) {
    event.target.value = "";
    event.target.dispatchEvent(new Event("input"));
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !event.target.closest("input, select, textarea, dialog")) {
    event.preventDefault();
    $("search").focus();
  }
});

$("sort").addEventListener("change", (event) => {
  state.sort = event.target.value;
  resetView();
  reloadList();
});

document.querySelector(".segmented").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  const featured = button.dataset.filter === "featured";
  if (featured === state.featured) return;
  state.featured = featured;
  for (const other of document.querySelectorAll(".segmented button")) {
    other.setAttribute("aria-pressed", String(other === button));
  }
  resetView();
  reloadList();
});

function goToPage(page) {
  state.page = page;
  state.editing = null;
  state.selected.clear();
  reloadList();
  const top = $("list").getBoundingClientRect().top;
  if (top < 0) $("list").scrollIntoView({ block: "start" });
}

$("prev").addEventListener("click", () => goToPage(state.page - 1));
$("next").addEventListener("click", () => goToPage(state.page + 1));

// ---- actions ----

$("learn").addEventListener("click", async () => {
  const learn = $("learn");
  learn.disabled = true;
  learn.classList.add("busy");
  learn.querySelector("span").textContent = "学习中…";
  try {
    const result = await bridge.apiPost("learn", { group: state.group });
    toast(
      result.messages
        ? `读了 ${result.messages} 条消息，新学 ${result.added} 条，合并 ${result.merged} 条`
        : "没有待学的消息",
    );
  } catch (error) {
    toast(errorText(error), true);
  }
  await refresh();
});

$("rebuild").addEventListener("click", async () => {
  $("rebuild").disabled = true;
  try {
    await bridge.apiPost("rebuild", { group: state.group });
    toast("开始重建向量");
  } catch (error) {
    toast(errorText(error), true);
  }
  await refresh();
});

$("clear").addEventListener("click", async () => {
  const group = currentGroup();
  if (!group) return;
  const ok = await confirmDialog({
    title: `清空群 ${group.group_id} 学到的全部表达？`,
    body: `<p>会删掉 <strong>${group.total}</strong> 条${
      group.featured ? `（其中 <strong>${group.featured}</strong> 条精选）` : ""
    }，不能恢复。攒着待学的消息不受影响。</p>`,
    ok: "清空",
  });
  if (!ok) return;
  try {
    const result = await bridge.apiPost("clear", { group: group.group_id });
    toast(`已清空 ${result.deleted} 条`);
  } catch (error) {
    toast(errorText(error), true);
  }
  resetView();
  await refresh();
});

$("check-all").addEventListener("change", (event) => {
  state.selected = event.target.checked ? new Set(items.map((item) => item.id)) : new Set();
  renderRows();
});

$("select-none").addEventListener("click", () => {
  state.selected.clear();
  renderRows();
});

$("delete-selected").addEventListener("click", async () => {
  const ids = [...state.selected];
  if (!ids.length) return;
  const ok = await confirmDialog({
    title: `删除选中的 ${ids.length} 条？`,
    body: "<p>删除后不能恢复。群里以后再出现同样的说法，还会重新学到。</p>",
    ok: "删除",
  });
  if (!ok) return;
  try {
    const result = await bridge.apiPost("delete", { ids });
    toast(`已删除 ${result.deleted} 条`);
    state.selected.clear();
  } catch (error) {
    toast(errorText(error), true);
  }
  await refresh();
});

// ---- rows ----

async function toggleStar(item) {
  const featured = !item.featured;
  const group = currentGroup();
  const apply = (value) => {
    item.featured = value ? 1 : 0;
    if (group) group.featured += value ? 1 : -1;
    renderRows();
    renderStats();
  };
  apply(featured);
  try {
    await bridge.apiPost("update", { id: item.id, featured });
    if (state.featured && !featured) reloadList();
  } catch (error) {
    apply(!featured);
    toast(errorText(error), true);
  }
}

function startEdit(id) {
  state.editing = id;
  renderRows();
  const input = $("rows").querySelector(`tr[data-id="${id}"] input[data-field="situation"]`);
  input?.focus();
  input?.select();
}

function cancelEdit() {
  state.editing = null;
  renderRows();
}

async function saveEdit(row, item) {
  const situation = row.querySelector('input[data-field="situation"]').value.trim();
  const style = row.querySelector('input[data-field="style"]').value.trim();
  if (!situation || !style) {
    toast("情境和表达都不能为空", true);
    return;
  }
  if (situation === item.situation && style === item.style) {
    cancelEdit();
    return;
  }
  for (const control of row.querySelectorAll("input, button")) control.disabled = true;
  try {
    await bridge.apiPost("update", { id: item.id, situation, style });
    item.situation = situation;
    item.style = style;
    state.editing = null;
    renderRows();
    toast("已保存");
  } catch (error) {
    for (const control of row.querySelectorAll("input, button")) control.disabled = false;
    toast(errorText(error), true);
  }
}

async function deleteOne(item) {
  const ok = await confirmDialog({
    title: "删除这一条？",
    body: `<div class="preview"><span class="sit">${escapeHtml(item.situation)}</span><span class="sty">${escapeHtml(
      item.style,
    )}</span></div><p>删除后不能恢复。群里以后再出现同样的说法，还会重新学到。</p>`,
    ok: "删除",
  });
  if (!ok) return;
  try {
    await bridge.apiPost("delete", { ids: [item.id] });
    state.selected.delete(item.id);
    toast("已删除");
  } catch (error) {
    toast(errorText(error), true);
  }
  await refresh();
}

$("rows").addEventListener("click", (event) => {
  const control = event.target.closest("[data-op]");
  if (!control || control.dataset.op === "select") return;
  const row = control.closest("tr");
  const item = items.find((entry) => entry.id === Number(row.dataset.id));
  if (!item) return;
  const op = control.dataset.op;
  if (op === "star") toggleStar(item);
  else if (op === "edit") startEdit(item.id);
  else if (op === "cancel") cancelEdit();
  else if (op === "save") saveEdit(row, item);
  else if (op === "delete") deleteOne(item);
});

$("rows").addEventListener("change", (event) => {
  if (event.target.dataset.op !== "select") return;
  const id = Number(event.target.closest("tr").dataset.id);
  if (event.target.checked) state.selected.add(id);
  else state.selected.delete(id);
  event.target.closest("tr").classList.toggle("selected", event.target.checked);
  renderSelection();
});

$("rows").addEventListener("keydown", (event) => {
  if (!event.target.matches(".cell-input")) return;
  const row = event.target.closest("tr");
  const item = items.find((entry) => entry.id === Number(row.dataset.id));
  if (event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    saveEdit(row, item);
  } else if (event.key === "Escape") {
    event.preventDefault();
    cancelEdit();
  }
});

await bridge.ready();
renderRows();
await refresh();
