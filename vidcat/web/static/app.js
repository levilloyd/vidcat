"use strict";

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

const state = {
  q: "", sort: "date", order: "desc", tags: new Set(), exts: new Set(), folder: "",
  dateFrom: "", dateTo: "", minDur: "", maxDur: "", bad: false, dup: false,
  page: 1, pageSize: 60, items: [], total: 0, current: -1,
};
let requestId = 0;

// ---------- formatting
const fmtSize = (n) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
};
const fmtDur = (s) => {
  if (!s) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
};
const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
const fmtDateTime = (ts) => new Date(ts * 1000).toLocaleString();
const stem = (name) => name.replace(/\.[^.]+$/, "");

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { "Content-Type": "application/json" };
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

// ---------- listing
function queryString(page) {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  p.set("sort", state.sort);
  p.set("order", state.order);
  p.set("page", page);
  p.set("page_size", state.pageSize);
  state.tags.forEach((t) => p.append("tag", t));
  state.exts.forEach((e) => p.append("ext", e));
  if (state.folder) p.set("folder", state.folder);
  if (state.dateFrom) p.set("date_from", state.dateFrom);
  if (state.dateTo) p.set("date_to", state.dateTo);
  if (state.minDur !== "") p.set("min_dur", String(parseFloat(state.minDur) * 60));
  if (state.maxDur !== "") p.set("max_dur", String(parseFloat(state.maxDur) * 60));
  if (state.bad) p.set("bad_name", "true");
  if (state.dup) p.set("duplicates", "true");
  return p.toString();
}

async function load(reset) {
  const id = ++requestId;
  const page = reset ? 1 : state.page + 1;
  let data;
  try {
    data = await api("/api/videos?" + queryString(page));
  } catch (e) {
    $("summary").textContent = "Error: " + e.message;
    return;
  }
  if (id !== requestId) return; // a newer request superseded this one
  state.page = data.page;
  state.total = data.total;
  state.items = reset ? data.items : state.items.concat(data.items);
  renderGrid(reset ? 0 : state.items.length - data.items.length);
}

function renderGrid(fromIndex) {
  const grid = $("grid");
  if (fromIndex === 0) grid.replaceChildren();
  if (state.items.length === 0) {
    grid.append(el("div", { class: "empty" }, "No videos match. Try clearing filters, or run `vidcat scan` on a folder."));
  }
  for (let i = fromIndex; i < state.items.length; i++) grid.append(makeCard(state.items[i], i));
  $("summary").textContent = `${state.total.toLocaleString()} video${state.total === 1 ? "" : "s"}`;
  $("more").hidden = state.items.length >= state.total;
  $("saveAll").hidden = state.total === 0;
  $("saveAll").textContent = state.total === 1 ? "Save…" : `Save all ${state.total.toLocaleString()}…`;
}

function makeCard(v, index) {
  const thumb = el("div", { class: "thumb" });
  const img = el("img", { class: "rot", "data-rot": String(v.rotation || 0), src: `/thumb/${v.id}?v=${v.rev}`, alt: "", loading: "lazy" });
  img.addEventListener("error", () => { img.remove(); thumb.prepend(el("div", { class: "noimg" }, "No preview")); });
  thumb.append(img);
  if (v.duration) thumb.append(el("span", { class: "badge" }, fmtDur(v.duration)));

  const info = el("div", { class: "info" }, el("div", { class: "name" }, v.name),
    el("div", { class: "sub" }, `${fmtDate(v.created_at)} · ${fmtSize(v.size)}`));
  const flags = el("div", { class: "flags" });
  if (v.bad_name) flags.append(el("span", { class: "flag" }, "Needs better name"));
  if (v.dup_count) flags.append(el("span", { class: "flag" }, `Possible duplicate (${v.dup_count})`));
  if (flags.children.length) info.append(flags);
  if (v.tags.length) info.append(el("div", { class: "chips" }, ...v.tags.map((t) => el("span", { class: "chip" }, t))));

  const card = el("button", { class: "card", type: "button", "data-id": v.id }, thumb, info);
  card.addEventListener("click", () => openModal(index));
  return card;
}

// ---------- filters
function toggleSet(set, value) { set.has(value) ? set.delete(value) : set.add(value); }

function chip(label, count, on, onClick) {
  const c = el("button", { class: "chip" + (on ? " on" : ""), type: "button", "aria-pressed": String(on) }, label);
  if (count != null) c.append(el("small", {}, String(count)));
  c.addEventListener("click", onClick);
  return c;
}

async function loadFacets() {
  const [tags, exts, folders] = await Promise.all([api("/api/tags"), api("/api/exts"), api("/api/folders")]);
  $("tags").replaceChildren(...(tags.length ? tags.map((t) => chip(t.name, t.count, state.tags.has(t.name), () => {
    toggleSet(state.tags, t.name); loadFacets(); load(true);
  })) : [el("span", { class: "sub" }, "No tags yet")]));
  $("exts").replaceChildren(...exts.map((e) => chip("." + e.ext, e.count, state.exts.has(e.ext), () => {
    toggleSet(state.exts, e.ext); loadFacets(); load(true);
  })));
  const sel = $("folder");
  sel.replaceChildren(el("option", { value: "" }, "All folders"));
  for (const f of folders) {
    const short = f.path.split("/").slice(-2).join("/");
    const opt = el("option", { value: f.path, title: f.path }, `${short} (${f.count})`);
    if (f.path === state.folder) opt.selected = true;
    sel.append(opt);
  }
  $("tagList").replaceChildren(...tags.map((t) => el("option", { value: t.name })));
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function bindFilters() {
  const refresh = () => load(true);
  $("q").addEventListener("input", debounce((e) => { state.q = e.target.value.trim(); refresh(); }, 250));
  $("sort").addEventListener("change", (e) => { state.sort = e.target.value; refresh(); });
  $("order").addEventListener("click", () => {
    state.order = state.order === "desc" ? "asc" : "desc";
    $("order").textContent = state.order === "desc" ? "↓" : "↑";
    refresh();
  });
  $("bad").addEventListener("change", (e) => { state.bad = e.target.checked; refresh(); });
  $("dup").addEventListener("change", (e) => { state.dup = e.target.checked; refresh(); });
  for (const [id, key] of [["dateFrom", "dateFrom"], ["dateTo", "dateTo"], ["minDur", "minDur"], ["maxDur", "maxDur"]]) {
    $(id).addEventListener("change", (e) => { state[key] = e.target.value; refresh(); });
  }
  $("folder").addEventListener("change", (e) => { state.folder = e.target.value; refresh(); });
  $("more").addEventListener("click", () => load(false));
  $("clear").addEventListener("click", () => {
    Object.assign(state, { q: "", tags: new Set(), exts: new Set(), folder: "", dateFrom: "", dateTo: "", minDur: "", maxDur: "", bad: false, dup: false });
    $("q").value = ""; $("bad").checked = false; $("dup").checked = false;
    for (const id of ["dateFrom", "dateTo", "minDur", "maxDur"]) $(id).value = "";
    loadFacets(); load(true);
  });
}

// ---------- detail modal
const modal = $("modal");

function setStatus(msg, isError = false) {
  const s = $("status");
  s.textContent = msg;
  s.classList.toggle("error", isError);
}

function openModal(index) {
  state.current = index;
  const v = state.items[index];
  setStatus("");
  const player = $("player");
  $("playerError").hidden = true;
  player.onerror = () => {
    // MediaError codes: 1 aborted, 2 network, 3 decode, 4 format/source not supported (the spec's names, not ours).
    const err = player.error;
    const label = { 1: "playback was aborted", 2: "a network error", 3: "a decoding error", 4: "an unsupported format" }[err && err.code]
      || "an unknown error";
    const detail = err && err.message ? ` — ${err.message}` : "";
    const cause = err && err.code === 4
      ? `Your browser can't play .${v.ext} files.`
      : `Playback failed: ${label}${detail}. Large files on a slow connection can do this intermittently — try again.`;
    $("playerError").textContent = `${cause} Use "Show in Finder" or Download to open it in another app.`;
    $("playerError").hidden = false;
  };
  // ?v= keeps the browser from playing a cached copy of another video that once had this id.
  // The fragment makes browsers show the first frame instead of black.
  player.src = `/media/${v.id}?v=${v.rev}#t=0.001`;
  applyPlayerRotation(v);
  $("nameInput").value = stem(v.name);
  $("extLabel").textContent = "." + v.ext;
  $("download").href = `/media/${v.id}?v=${v.rev}`;
  $("download").setAttribute("download", v.name);
  renderModalTags(v);
  const meta = $("meta");
  meta.replaceChildren();
  const captured = v.date_source === "filename" ? `${fmtDate(v.created_at)} (from file name)`
    : v.date_source === "mtime" ? `${fmtDateTime(v.created_at)} (file date — may just be when it was copied)`
    : fmtDateTime(v.created_at);
  const rows = [
    ["Captured", captured],
    ["Length", fmtDur(v.duration) || "unknown"],
    ["Size", fmtSize(v.size)],
    ["Resolution", v.width ? `${v.width}×${v.height}` : "unknown"],
    ["Codec", v.codec || "unknown"],
    ["Location", v.dir],
  ];
  if (v.caption) rows.push(["Description", v.caption]);
  if (v.dup_count) rows.push(["Duplicates", `${v.dup_count} other file(s) with the same size — run \`vidcat dupes\` to review`]);
  for (const [k, val] of rows) meta.append(el("dt", {}, k), el("dd", {}, val));
  $("prev").disabled = index <= 0;
  $("next").disabled = index >= state.items.length - 1;
  if (!modal.open) modal.showModal();
}

function renderModalTags(v) {
  $("videoTags").replaceChildren(...v.tags.map((t) => {
    const c = el("span", { class: "chip" }, t);
    const x = el("button", { class: "x", type: "button", "aria-label": `Remove tag ${t}`, style: "background:none;border:0;color:inherit;padding:0 0 0 6px;cursor:pointer" }, "×");
    x.addEventListener("click", () => mutateTags("DELETE", `?tag=${encodeURIComponent(t)}`));
    c.append(x);
    return c;
  }));
}

// Replace a video everywhere it's shown. Looked up by id, not by "current", because the user may have
// moved to another video while the request was in flight.
function replaceItem(updated) {
  const index = state.items.findIndex((x) => x.id === updated.id);
  if (index < 0) return false;
  state.items[index] = updated;
  const card = document.querySelector(`.card[data-id="${updated.id}"]`);
  if (card) card.replaceWith(makeCard(updated, index));
  return index === state.current;
}

async function mutateTags(method, suffix = "", body) {
  const v = state.items[state.current];
  try {
    const updated = await api(`/api/videos/${v.id}/tags${suffix}`, { method, body });
    if (replaceItem(updated)) renderModalTags(updated);
    loadFacets();
  } catch (e) { setStatus(e.message, true); }
}

// ---------- rotation (a viewing preference stored in the catalog; the file itself is never changed)
function applyPlayerRotation(v) {
  const player = $("player");
  const rot = v.rotation || 0;
  player.dataset.rot = String(rot);
  // Prefer the browser's own dimensions (they include any rotation flag inside the file) over the catalog's.
  const w = player.videoWidth || v.width || 16, h = player.videoHeight || v.height || 9;
  const turned = rot === 90 || rot === 270;
  $("stage").style.setProperty("--ar", String(turned ? h / w : w / h));
  player.controls = rot === 0;          // native controls would be rotated along with the picture
  $("customControls").hidden = rot === 0;
}

async function rotate(delta) {
  const v = state.items[state.current];
  if (!v) return;
  const previous = v.rotation || 0;
  v.rotation = (previous + delta + 360) % 360;
  applyPlayerRotation(v);
  replaceItem(v);
  try {
    await api(`/api/videos/${v.id}`, { method: "PATCH", body: { rotation: v.rotation } });
  } catch (e) {
    v.rotation = previous;
    if (replaceItem(v)) applyPlayerRotation(v);
    setStatus("Couldn't save rotation: " + e.message, true);
  }
}

function bindCustomControls() {
  const p = $("player");
  const clock = (s) => fmtDur(s) || "0:00";
  const sync = () => {
    $("cPlay").textContent = p.paused ? "▶" : "⏸";
    $("cMute").textContent = p.muted || p.volume === 0 ? "🔇" : "🔊";
    if (p.duration) $("cSeek").value = String((p.currentTime / p.duration) * 1000);
    $("cTime").textContent = `${clock(p.currentTime)} / ${clock(p.duration)}`;
  };
  ["play", "pause", "timeupdate", "loadedmetadata", "volumechange", "emptied"].forEach((ev) => p.addEventListener(ev, sync));
  $("cPlay").addEventListener("click", () => (p.paused ? p.play() : p.pause()));
  p.addEventListener("click", () => { if (!p.controls) $("cPlay").click(); });
  $("cSeek").addEventListener("input", () => { if (p.duration) p.currentTime = (p.duration * $("cSeek").value) / 1000; });
  $("cMute").addEventListener("click", () => { p.muted = !p.muted; });
  $("cFull").addEventListener("click", () => (document.fullscreenElement ? document.exitFullscreen() : $("playerBox").requestFullscreen()));
  p.addEventListener("loadedmetadata", () => { const v = state.items[state.current]; if (v) applyPlayerRotation(v); });
}

function bindModal() {
  $("close").addEventListener("click", () => modal.close());
  modal.addEventListener("close", () => { $("player").pause(); $("player").removeAttribute("src"); $("player").load(); });
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.close(); });
  $("prev").addEventListener("click", () => state.current > 0 && openModal(state.current - 1));
  $("next").addEventListener("click", () => state.current < state.items.length - 1 && openModal(state.current + 1));
  modal.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "ArrowLeft") $("prev").click();
    if (e.key === "ArrowRight") $("next").click();
    if (e.key === "r") rotate(90);
    if (e.key === "R") rotate(-90);
  });
  $("rotL").addEventListener("click", () => rotate(-90));
  $("rotR").addEventListener("click", () => rotate(90));

  $("tagInput").addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const tags = e.target.value.split(",").map((t) => t.trim()).filter(Boolean);
    if (!tags.length) return;
    e.target.value = "";
    await mutateTags("POST", "", { tags });
  });

  const rename = async () => {
    const v = state.items[state.current];
    const name = $("nameInput").value.trim();
    if (!name || name === stem(v.name)) return;
    try {
      const updated = await api(`/api/videos/${v.id}`, { method: "PATCH", body: { name } });
      if (replaceItem(updated)) {
        $("nameInput").value = stem(updated.name);
        setStatus(`Renamed to ${updated.name}`);
      }
    } catch (e) { setStatus(e.message, true); }
  };
  $("saveName").addEventListener("click", rename);
  $("nameInput").addEventListener("keydown", (e) => { if (e.key === "Enter") rename(); });

  const suggest = (ai) => async () => {
    const v = state.items[state.current];
    const buttons = [$("suggest"), $("suggestAi")];
    buttons.forEach((b) => (b.disabled = true));
    setStatus(ai ? "Asking the vision model… (can take a few seconds)" : "");
    try {
      const r = await api(`/api/videos/${v.id}/suggest-name?ai=${ai}`, { method: "POST" });
      $("nameInput").value = r.suggestion;
      if (r.caption && ai) { v.caption = r.caption; }
      setStatus(r.warning || (ai ? "Suggestion ready — edit it, then Rename." : ""), Boolean(r.warning));
    } catch (e) { setStatus(e.message, true); }
    buttons.forEach((b) => (b.disabled = false));
  };
  $("suggest").addEventListener("click", suggest(false));
  $("suggestAi").addEventListener("click", suggest(true));

  $("reveal").addEventListener("click", () => api(`/api/videos/${state.items[state.current].id}/reveal`, { method: "POST" }).catch((e) => setStatus(e.message, true)));
}

// ---------- save all results to a folder (the server copies them; the page shows progress)
function exportMessage(text, isError = false) {
  $("exportBox").hidden = false;
  $("exportText").textContent = text;
  $("exportText").classList.toggle("error", isError);
  for (const id of ["exportCancel", "exportReveal", "exportProgress", "exportSkippedBox"]) $(id).hidden = true;
  $("exportDismiss").hidden = false;
}

function renderExport(job) {
  if (!job || job.state === "none" || job.state === "not started") { $("exportBox").hidden = true; return; }
  const running = job.state === "running";
  const n = (count) => `${count.toLocaleString()} video${count === 1 ? "" : "s"}`;
  const text = {
    running: `Saving ${n(job.total)} to ${job.dest}: ${job.copied.toLocaleString()} done, ` +
      `${fmtSize(job.done_bytes)} of ${fmtSize(job.total_bytes)}${job.current ? ` — ${job.current}` : ""}`,
    done: `Saved ${n(job.copied)} to ${job.dest}.`,
    cancelled: `Cancelled. ${n(job.copied)} saved to ${job.dest}; the one being copied was discarded.`,
    failed: `${job.error} ${n(job.copied)} saved to ${job.dest}.`,
  }[job.state];
  exportMessage(text, job.state === "failed");
  $("exportProgress").hidden = !running;
  $("exportProgress").value = job.total_bytes ? job.done_bytes / job.total_bytes : 0;
  $("exportCancel").hidden = !running;
  $("exportDismiss").hidden = running;
  $("exportReveal").hidden = running || job.copied === 0;
  $("saveAll").disabled = running;
  $("exportSkippedBox").hidden = job.skipped.length === 0;
  $("exportSkippedSummary").textContent = `${n(job.skipped.length)} skipped`;
  $("exportSkipped").replaceChildren(...job.skipped.map((s) => el("li", {}, `${s.name}: ${s.reason}`)));
}

async function pollExport() {
  try {
    const job = await api("/api/export");
    renderExport(job);
    if (job.state === "running") setTimeout(pollExport, 1000);
  } catch (e) { exportMessage("Lost track of the save: " + e.message, true); }
}

function bindExport() {
  $("saveAll").addEventListener("click", async () => {
    $("saveAll").disabled = true;
    exportMessage(`Choose where to save ${state.total.toLocaleString()} video${state.total === 1 ? "" : "s"} ` +
      "in the folder window that just opened (it may be behind this one)…");
    $("exportDismiss").hidden = true;
    try {
      const job = await api("/api/export?" + queryString(1), { method: "POST" });
      renderExport(job);
      if (job.state === "running") pollExport();
    } catch (e) { exportMessage(e.message, true); }
    if ($("exportCancel").hidden) $("saveAll").disabled = false;
  });
  $("exportCancel").addEventListener("click", () => api("/api/export/cancel", { method: "POST" }).then(renderExport)
    .catch((e) => exportMessage(e.message, true)));
  $("exportReveal").addEventListener("click", () => api("/api/export/reveal", { method: "POST" })
    .catch((e) => exportMessage(e.message, true)));
  $("exportDismiss").addEventListener("click", () => { $("exportBox").hidden = true; $("saveAll").disabled = false; });
  // A save keeps going if the page is reloaded; pick its progress back up.
  api("/api/export").then((job) => { if (job.state === "running") { renderExport(job); pollExport(); } }).catch(() => {});
}

bindFilters();
bindModal();
bindCustomControls();
bindExport();
loadFacets();
load(true).then(() => {
  // Deep link: /#v12 opens video 12 (if it's on the first page of results).
  const m = location.hash.match(/^#v(\d+)$/);
  const index = m ? state.items.findIndex((v) => v.id === Number(m[1])) : -1;
  if (index >= 0) openModal(index);
});
