"use strict";
// ClipSift UI. Plain JS, no build step. Every server string goes through h() before it hits innerHTML.

const S = { view: "add", sources: [], jobs: [], layouts: [], games: [], settings: {}, cands: [],
  filter: { source: "", category: "", status: "" }, poll: null, editing: null, layoutEd: null, worker: "" };

const h = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function fmtTs(s) {
  if (s == null || isNaN(s)) return "";
  s = Math.max(0, s);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = Math.floor(s % 60);
  return (hh ? hh + ":" + String(mm).padStart(2, "0") : String(mm).padStart(2, "0")) + ":" + String(ss).padStart(2, "0");
}
function fmtTsMs(s) { return fmtTs(s) + "." + Math.floor((s % 1) * 10); }
function parseTs(t) {
  t = String(t).trim();
  if (!/^\d+(:\d{1,2}){0,2}(\.\d+)?$/.test(t)) throw new Error("Use h:mm:ss or seconds");
  return t.split(":").reduce((a, p) => a * 60 + parseFloat(p), 0);
}
function eta(s) { if (!s) return ""; return s < 90 ? `about ${Math.round(s)} s left (estimate)` : `about ${fmtTs(s)} left (estimate)`; }

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", credentials: "same-origin", headers: { "X-ClipSift": "1" } };
  if (opts.form) { init.body = opts.form; }
  else if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  const text = await r.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text.slice(0, 200) }; }
  if (r.status === 401 && !sessionStorage.getItem("cs_reloaded")) {
    try { sessionStorage.setItem("cs_reloaded", "1"); } catch {}
    location.reload();
  }
  if (r.ok) { try { sessionStorage.removeItem("cs_reloaded"); } catch {} }
  if (!r.ok) {
    const msg = data.error || (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || r.statusText));
    const e = new Error(msg); e.data = data; throw e;
  }
  return data;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add("hidden"), err ? 9000 : 3500);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }

// ---------- navigation and polling ----------

function go(view) {
  S.view = view;
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  closeModal();
  render();
}
$$("#nav a").forEach((a) => a.addEventListener("click", () => go(a.dataset.view)));

async function refreshState() {
  const st = await api("/api/state");
  S.sources = st.sources; S.jobs = st.jobs; S.worker = st.worker;
  const dot = $("#workerdot");
  dot.className = "dot " + (st.worker === "running" ? "ok" : st.worker === "unknown" ? "" : "bad");
  dot.title = "Worker: " + st.worker;
}

async function poll() {
  try {
    await refreshState();
    if (S.view === "process" || S.view === "add" || S.view === "export") renderLive();
    if (S.editing) updateEditJobs();
  } catch (e) { /* server restarting */ }
}

function activeJob(kind, opts = {}) {
  return S.jobs.find((j) => j.kind === kind && ["queued", "running"].includes(j.status)
    && (opts.source == null || j.source_id === opts.source) && (opts.cand == null || j.candidate_id === opts.cand));
}
function lastJob(kind, opts = {}) {
  return S.jobs.find((j) => j.kind === kind && (opts.source == null || j.source_id === opts.source)
    && (opts.cand == null || j.candidate_id === opts.cand));
}

function jobLine(j) {
  if (!j) return "";
  const pct = Math.round((j.progress || 0) * 100);
  const err = j.status === "failed" ? `<div class="errbox">${h(j.error)}</div>` : "";
  const btns = ["queued", "running"].includes(j.status)
    ? `<button class="sm" data-cancel="${j.id}">Cancel</button>`
    : (["cancelled", "failed"].includes(j.status) && j.kind !== "preview" ? `<button class="sm" data-resume="${j.id}">Resume</button>` : "");
  return `<div class="row"><span class="badge">${h(j.status)}</span>
    <div class="progress grow"><div style="width:${pct}%"></div></div><span class="small">${pct}%</span>${btns}</div>
    <div class="small muted">${h(j.stage || "")} ${h(j.message || "")} ${j.status === "running" ? h(eta(j.eta_s)) : ""}</div>${err}`;
}
document.addEventListener("click", (ev) => {
  const c = ev.target.closest("[data-cancel]"), r = ev.target.closest("[data-resume]");
  if (c) guard(async () => { await api(`/api/jobs/${c.dataset.cancel}/cancel`, { method: "POST" }); await poll(); });
  if (r) guard(async () => { await api(`/api/jobs/${r.dataset.resume}/resume`, { method: "POST" }); await poll(); });
});

function render() {
  const views = { add: viewAdd, layout: viewLayout, process: viewProcess, review: viewReview, export: viewExport,
    refs: viewRefs, settings: viewSettings, storage: viewStorage };
  guard(() => views[S.view]());
}
function renderLive() {
  if (S.view === "process") $$("[data-jobslot]").forEach((el) => {
    const sid = +el.dataset.jobslot;
    el.innerHTML = jobLine(activeJob("process", { source: sid }) || lastJob("process", { source: sid }) ||
      activeJob("download", { source: sid }) || lastJob("download", { source: sid }));
  });
  if (S.view === "add") $$("[data-dlslot]").forEach((el) => {
    el.innerHTML = jobLine(activeJob("download", { source: +el.dataset.dlslot }) || lastJob("download", { source: +el.dataset.dlslot }));
  });
  if (S.view === "export") $$("[data-exslot]").forEach((el) => {
    const j = activeJob("export", { cand: +el.dataset.exslot }) || lastJob("export", { cand: +el.dataset.exslot });
    el.innerHTML = j ? jobLine(j) : "";
  });
}

// ---------- 1. add ----------

async function viewAdd() {
  await refreshState();
  const games = S.games, layouts = S.layouts;
  const srcRows = S.sources.map((s) => {
    const tracks = (s.track_roles && Object.keys(s.track_roles).length) ? "set" : "";
    return `<tr>
      <td><b>${h(s.title || s.url || s.path)}</b><div class="small muted mono">${h(s.kind === "youtube" ? s.url : s.path)}</div>
        ${s.error ? `<div class="errbox">${h(s.error)}</div>` : ""}<div data-dlslot="${s.id}"></div></td>
      <td>${h(fmtTs(s.duration))}<div class="small muted">${h(s.orientation || "")} ${s.width ? h(s.width + "x" + s.height) : ""}</div></td>
      <td><select data-game="${s.id}">${games.map((g) => `<option value="${g.id}" ${g.id === s.game ? "selected" : ""}>${h(g.name)}</option>`).join("")}</select></td>
      <td><select data-layout="${s.id}" ${s.orientation === "vertical" ? "disabled title='Vertical clips keep their own composition'" : ""}>
        <option value="">Full frame (no layout)</option>
        ${layouts.map((l) => `<option value="${l.id}" ${l.id === s.layout_id ? "selected" : ""}>${h(l.name)}</option>`).join("")}</select></td>
      <td><button class="sm" data-tracks="${s.id}">Audio tracks${tracks ? " ✓" : ""}</button></td>
      <td><button class="sm bad" data-remove="${s.id}">Remove</button></td></tr>`;
  }).join("");
  $("#main").innerHTML = `
    <h2>Add recordings</h2>
    <div class="card"><h3 style="margin-top:0">YouTube stream or video</h3>
      <div class="row"><input id="yturl" class="grow" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off">
        <select id="ytq"><option value="best">Best available quality</option><option value="1080">1080p (smaller)</option><option value="720">720p (smallest)</option></select>
        <button id="ytcheck">Check</button></div>
      <div id="ytinfo" class="small" style="margin-top:8px"></div>
      <p class="small muted">Public and unlisted videos only. Live streams can be added after they end and YouTube has processed them.</p></div>
    <div class="card"><h3 style="margin-top:0">Files on this PC</h3>
      <div class="row"><button class="primary" id="pick">Choose video files…</button><span class="muted small">or paste full paths, one per line</span></div>
      <textarea id="paths" placeholder="D:\\Recordings\\2026-11-17 20-15-03.mp4" style="margin-top:8px"></textarea>
      <div class="row" style="margin-top:6px"><button id="addpaths">Add pasted paths</button></div></div>
    <h3>Recordings</h3>
    ${S.sources.length ? `<div class="card"><table><tr><th>Recording</th><th>Length</th><th>Game</th><th>Layout</th><th></th><th></th></tr>${srcRows}</table></div>`
      : `<div class="empty">No recordings yet.</div>`}`;
  renderLive();
  $("#ytcheck").onclick = () => guard(async () => {
    const box = $("#ytinfo"); box.textContent = "Checking…";
    try {
      const i = await api("/api/youtube/check", { method: "POST", body: { url: $("#yturl").value, quality: $("#ytq").value } });
      box.innerHTML = `<b>${h(i.title)}</b> · ${h(i.channel || "")} · ${h(i.duration_text)} · ${i.height ? h(i.height + "p" + (i.fps ? i.fps : "")) : ""}
        ${h(i.vcodec || "")} · about ${h(i.est_text)} to download ${i.was_live ? "· past livestream" : ""}
        <div class="row" style="margin-top:6px"><button class="primary" id="ytadd">Download and process</button></div>`;
      $("#ytadd").onclick = () => guard(async () => {
        const r = await api("/api/sources/youtube", { method: "POST", body: { url: i.url, title: i.title, quality: $("#ytq").value } });
        toast(r.message || "Download queued"); viewAdd();
      });
    } catch (e) { box.innerHTML = `<div class="errbox">${h(e.message)}</div>`; }
  });
  $("#pick").onclick = () => guard(async () => {
    const r = await api("/api/pick-files", { method: "POST", body: { kind: "video" } });
    if (r.paths.length) await addPaths(r.paths);
  });
  $("#addpaths").onclick = () => guard(() => addPaths($("#paths").value.split(/\r?\n/).filter((x) => x.trim())));
  $$("[data-game]").forEach((el) => el.onchange = () => guard(() => api(`/api/sources/${el.dataset.game}`, { method: "PATCH", body: { game: el.value } })));
  $$("[data-layout]").forEach((el) => el.onchange = () => guard(() => api(`/api/sources/${el.dataset.layout}`, { method: "PATCH", body: { layout_id: el.value ? +el.value : null } })));
  $$("[data-remove]").forEach((el) => el.onclick = () => guard(async () => {
    const s = S.sources.find((x) => x.id === +el.dataset.remove);
    const dl = s.kind === "youtube" && s.path ? confirm("Also delete the downloaded video file? (Cancel keeps the file.)") : false;
    if (!confirm("Remove this recording and its candidates from ClipSift? The original file is not touched.")) return;
    await api(`/api/sources/${s.id}?delete_download=${dl}`, { method: "DELETE" }); viewAdd();
  }));
  $$("[data-tracks]").forEach((el) => el.onclick = () => openTracks(+el.dataset.tracks));
}

async function addPaths(paths) {
  const r = await api("/api/sources/local", { method: "POST", body: { paths } });
  if (r.errors.length) toast(r.errors.map((e) => `${e.path}: ${e.error}`).join("\n"), true);
  else toast(`Added ${r.added.length} recording(s)`);
  viewAdd();
}

async function openTracks(sid) {
  const s = await api(`/api/sources/${sid}`);
  const full = s;
  const audio = (full.info && full.info.audio) || [];
  const roles = full.track_roles || {};
  const opts = ["mixed", "mic", "discord", "game", "ignore"];
  openModal(`<h2>Audio tracks: ${h(s.title)}</h2>
    ${audio.length < 2 ? `<p class="muted">This file has ${audio.length} audio track(s), so it is treated as one mixed track.</p>` : `
    <p class="muted small">If OBS recorded separate tracks, say what each one is. Speech is read from mic + Discord, swearing is muted only there, and game sound stays.</p>
    <table><tr><th>#</th><th>Track</th><th>Role</th></tr>${audio.map((a) => `<tr><td>${a.index + 1}</td>
      <td>${h(a.title || "track " + (a.index + 1))} <span class="small muted">${h(a.codec)} ${a.channels}ch</span></td>
      <td><select data-role="${a.index}"><option value="">(not set)</option>${opts.map((o) => `<option ${roles[a.index] === o ? "selected" : ""}>${o}</option>`).join("")}</select></td></tr>`).join("")}</table>`}
    <div class="row" style="margin-top:12px"><button class="primary" id="savetr">Save</button><button onclick="closeModal()">Close</button></div>`);
  $("#savetr").onclick = () => guard(async () => {
    const tr = {};
    $$("[data-role]").forEach((el) => { if (el.value) tr[el.dataset.role] = el.value; });
    await api(`/api/sources/${sid}`, { method: "PATCH", body: { track_roles: tr } });
    toast("Saved. Re-process the recording to use the new track roles."); closeModal(); viewAdd();
  });
}

// ---------- 2. layout ----------

const RECT_COLORS = { facecam: "#4fb3ff", gameplay: "#3ecf8e", killfeed: "#ffb347", banner: "#ff8c69", reward: "#ffd84d" };
const defaultLayout = () => ({ facecam: [0.0, 0.0, 0.25, 0.25], gameplay: [0.25, 0.0, 0.5, 1.0], gameplay_mode: "crop",
  facecam_h: 0.30, brand_h: 0.085, brand_on: true, brand: { text: S.settings.creator_name || "", color: "#111318", text_color: "#FFFFFF" },
  hud_insets: [], hud_regions: {} });

async function viewLayout() {
  await refreshState();
  const horiz = S.sources.filter((s) => s.orientation === "horizontal" && s.path);
  const ed = S.layoutEd || (S.layoutEd = { id: S.layouts[0]?.id || null, data: S.layouts[0] ? structuredClone(S.layouts[0].data) : defaultLayout(),
    name: S.layouts[0]?.name || "My layout", src: horiz[0]?.id || null, t: 60, game: horiz[0]?.game || "cs2" });
  const g = S.games.find((x) => x.id === ed.game);
  $("#main").innerHTML = `<h2>Layout</h2>
    <div class="row" style="margin-bottom:10px">
      <select id="lsel"><option value="">+ New layout</option>${S.layouts.map((l) => `<option value="${l.id}" ${l.id === ed.id ? "selected" : ""}>${h(l.name)}</option>`).join("")}</select>
      <input id="lname" value="${h(ed.name)}" placeholder="Layout name"><button class="primary" id="lsave">Save layout</button>
      ${ed.id ? `<button class="bad" id="ldel">Delete</button>` : ""}<span class="grow"></span>
      <select id="lsrc">${horiz.map((s) => `<option value="${s.id}" ${s.id === ed.src ? "selected" : ""}>${h(s.title)}</option>`).join("") || "<option>Add a horizontal recording first</option>"}</select></div>
    ${horiz.length ? "" : `<div class="warnbox">Add a horizontal recording to draw regions over a real frame.</div>`}
    <div class="layout-wrap">
      <div>
        <div class="stage" id="stage"><img id="lframe" alt="" draggable="false"></div>
        <div class="row" style="margin-top:8px"><span class="small muted">Frame at</span>
          <input type="range" id="lt" min="0" max="${Math.floor(horiz.find((s) => s.id === ed.src)?.duration || 100)}" value="${ed.t}" class="grow">
          <span id="ltlabel" class="small mono">${fmtTs(ed.t)}</span></div>
        <p class="small muted">Drag boxes to move, drag the white corner to resize. Regions are saved as fractions of the frame, so they work at any resolution.</p>
        <h3>HUD regions for <select id="lgame">${S.games.filter((x) => x.hud_ocr).map((x) => `<option value="${x.id}" ${x.id === ed.game ? "selected" : ""}>${h(x.name)}</option>`).join("")}</select></h3>
        <p class="small muted">Where the kill feed, round banners or money popups appear. These are read with OCR to find quiet skill plays. Game defaults are rough guesses; move them onto the real HUD.</p>
        <div class="row" id="hudbtns"></div>
      </div>
      <div class="card">
        <canvas id="vprev" class="vprev" width="270" height="480"></canvas>
        <div class="kv" style="margin-top:12px">
          <label>Facecam panel</label><div class="row"><input type="checkbox" id="lfc" ${ed.data.facecam ? "checked" : ""}><input type="range" id="lfch" min="0.15" max="0.45" step="0.01" value="${ed.data.facecam_h}" class="grow"></div>
          <label>Gameplay</label><select id="lmode"><option value="crop">Crop to my gameplay box</option><option value="fit">Fit my gameplay box (no crop)</option><option value="full">Full frame (nothing cropped)</option></select>
          <label>Branding panel</label><div class="row"><input type="checkbox" id="lbon" ${ed.data.brand_on ? "checked" : ""}><input type="range" id="lbh" min="0.04" max="0.15" step="0.005" value="${ed.data.brand_h}" class="grow"></div>
          <label>Brand text</label><input id="lbt" value="${h(ed.data.brand?.text || "")}">
          <label>Panel colour</label><input id="lbc" type="color" value="${h(ed.data.brand?.color || "#111318")}">
          <label>Text colour</label><input id="lbtc" type="color" value="${h(ed.data.brand?.text_color || "#ffffff")}">
        </div>
        <h3>Always-on insets</h3>
        <p class="small muted">Optional HUD pieces (minimap, score) copied into the gameplay panel on every clip. Kill feed insets are added automatically when a clip's evidence would be cropped out.</p>
        <div id="insets"></div><button class="sm" id="addinset">+ Add inset</button>
      </div>
    </div>`;
  $("#lmode").value = ed.data.gameplay_mode || "crop";
  const img = $("#lframe");
  const loadFrame = () => { if (ed.src) img.src = `/api/sources/${ed.src}/frame?t=${ed.t}&w=1280&_=${Date.now()}`; };
  img.onload = () => { drawRects(); drawVPrev(); };
  loadFrame();
  $("#lt").oninput = (e) => { ed.t = +e.target.value; $("#ltlabel").textContent = fmtTs(ed.t); };
  $("#lt").onchange = loadFrame;
  $("#lsrc").onchange = (e) => { ed.src = +e.target.value; viewLayout(); };
  $("#lgame").onchange = (e) => { ed.game = e.target.value; viewLayout(); };
  $("#lsel").onchange = (e) => {
    const l = S.layouts.find((x) => x.id === +e.target.value);
    Object.assign(ed, l ? { id: l.id, data: structuredClone(l.data), name: l.name } : { id: null, data: defaultLayout(), name: "New layout" });
    viewLayout();
  };
  const bind = (id, fn) => { $(id).oninput = (e) => { fn(e.target); drawRects(); drawVPrev(); }; };
  bind("#lfc", (el) => { ed.data.facecam = el.checked ? (ed._fc || [0, 0, 0.25, 0.25]) : (ed._fc = ed.data.facecam, null); });
  bind("#lfch", (el) => ed.data.facecam_h = +el.value);
  bind("#lmode", (el) => ed.data.gameplay_mode = el.value);
  bind("#lbon", (el) => ed.data.brand_on = el.checked);
  bind("#lbh", (el) => ed.data.brand_h = +el.value);
  bind("#lbt", (el) => (ed.data.brand ||= {}).text = el.value);
  bind("#lbc", (el) => (ed.data.brand ||= {}).color = el.value);
  bind("#lbtc", (el) => (ed.data.brand ||= {}).text_color = el.value);
  const hudDefaults = { cs2: { killfeed: [0.70, 0.05, 0.29, 0.25], banner: [0.25, 0.08, 0.50, 0.26] },
    valorant: { killfeed: [0.70, 0.07, 0.29, 0.28], banner: [0.25, 0.10, 0.50, 0.28] },
    rocket_league: { banner: [0.20, 0.12, 0.60, 0.40] }, wardogs: { killfeed: [0.70, 0.05, 0.29, 0.25], reward: [0.33, 0.30, 0.34, 0.30] } };
  const regs = (ed.data.hud_regions ||= {});
  regs[ed.game] ||= structuredClone(hudDefaults[ed.game] || {});
  $("#hudbtns").innerHTML = Object.keys(hudDefaults[ed.game] || {}).map((k) =>
    `<label class="small"><input type="checkbox" data-hud="${k}" ${regs[ed.game][k] ? "checked" : ""}> ${k}</label>`).join(" ") +
    ` <button class="sm" id="hudreset">Reset to game defaults</button>`;
  $$("[data-hud]").forEach((el) => el.onchange = () => {
    regs[ed.game][el.dataset.hud] = el.checked ? structuredClone(hudDefaults[ed.game][el.dataset.hud]) : null; drawRects();
  });
  $("#hudreset").onclick = () => { regs[ed.game] = structuredClone(hudDefaults[ed.game] || {}); viewLayout(); };
  const renderInsets = () => {
    $("#insets").innerHTML = (ed.data.hud_insets || []).map((ins, i) => `<div class="row small" style="margin-bottom:4px">
      inset ${i + 1} <select data-ipos="${i}">${["tr", "tl", "br", "bl", "tc"].map((p) => `<option ${ins.pos === p ? "selected" : ""}>${p}</option>`).join("")}</select>
      size <input type="range" min="0.15" max="0.6" step="0.01" value="${ins.scale || 0.32}" data-iscale="${i}">
      <button class="sm bad" data-idel="${i}">✕</button></div>`).join("");
    $$("[data-ipos]").forEach((el) => el.onchange = () => { ed.data.hud_insets[+el.dataset.ipos].pos = el.value; drawVPrev(); });
    $$("[data-iscale]").forEach((el) => el.oninput = () => { ed.data.hud_insets[+el.dataset.iscale].scale = +el.value; drawVPrev(); });
    $$("[data-idel]").forEach((el) => el.onclick = () => { ed.data.hud_insets.splice(+el.dataset.idel, 1); renderInsets(); drawRects(); drawVPrev(); });
  };
  renderInsets();
  $("#addinset").onclick = () => { (ed.data.hud_insets ||= []).push({ src: [0.02, 0.02, 0.15, 0.25], pos: "tl", scale: 0.3 }); renderInsets(); drawRects(); drawVPrev(); };
  $("#lsave").onclick = () => guard(async () => {
    const body = { name: $("#lname").value.trim(), data: ed.data };
    const saved = ed.id ? await api(`/api/layouts/${ed.id}`, { method: "PUT", body }) : await api("/api/layouts", { method: "POST", body });
    ed.id = saved.id; ed.name = saved.name;
    S.layouts = await api("/api/layouts");
    if (ed.src) await api(`/api/sources/${ed.src}`, { method: "PATCH", body: { layout_id: saved.id } });
    toast("Layout saved"); viewLayout();
  });
  if ($("#ldel")) $("#ldel").onclick = () => guard(async () => {
    if (!confirm("Delete this layout?")) return;
    await api(`/api/layouts/${ed.id}`, { method: "DELETE" });
    S.layouts = await api("/api/layouts"); S.layoutEd = null; viewLayout();
  });
}

function editableRects() {
  const ed = S.layoutEd, d = ed.data, out = [];
  if (d.facecam) out.push({ key: "facecam", get: () => d.facecam, set: (r) => d.facecam = r });
  if (d.gameplay_mode !== "full") out.push({ key: "gameplay", get: () => d.gameplay || [0.25, 0, 0.5, 1], set: (r) => d.gameplay = r });
  const regs = (d.hud_regions || {})[ed.game] || {};
  for (const k of ["killfeed", "banner", "reward"]) if (regs[k]) out.push({ key: k, get: () => regs[k], set: (r) => regs[k] = r });
  (d.hud_insets || []).forEach((ins, i) => out.push({ key: "inset " + (i + 1), color: "#c792ea", get: () => ins.src, set: (r) => ins.src = r }));
  return out;
}

function drawRects() {
  const stage = $("#stage"); if (!stage) return;
  $$(".rect", stage).forEach((x) => x.remove());
  for (const R of editableRects()) {
    const [x, y, w, hgt] = R.get();
    const el = document.createElement("div");
    el.className = "rect"; const col = R.color || RECT_COLORS[R.key] || "#fff";
    el.style.cssText = `left:${x * 100}%;top:${y * 100}%;width:${w * 100}%;height:${hgt * 100}%;border-color:${col}`;
    el.innerHTML = `<span style="background:${col}">${h(R.key)}</span><div class="h"></div>`;
    stage.appendChild(el);
    el.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      const resize = ev.target.classList.contains("h");
      const box = stage.getBoundingClientRect();
      const start = [ev.clientX, ev.clientY], r0 = R.get().slice();
      try { el.setPointerCapture(ev.pointerId); } catch { /* synthetic or lost pointer */ }
      const move = (e) => {
        const dx = (e.clientX - start[0]) / box.width, dy = (e.clientY - start[1]) / box.height;
        let [nx, ny, nw, nh] = r0;
        if (resize) { nw = Math.min(1 - nx, Math.max(0.03, nw + dx)); nh = Math.min(1 - ny, Math.max(0.03, nh + dy)); }
        else { nx = Math.min(1 - nw, Math.max(0, nx + dx)); ny = Math.min(1 - nh, Math.max(0, ny + dy)); }
        R.set([nx, ny, nw, nh].map((v) => Math.round(v * 10000) / 10000));
        el.style.left = nx * 100 + "%"; el.style.top = ny * 100 + "%"; el.style.width = nw * 100 + "%"; el.style.height = nh * 100 + "%";
        drawVPrev();
      };
      const up = () => { el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); };
      el.addEventListener("pointermove", move); el.addEventListener("pointerup", up);
    });
  }
}

function drawVPrev() {
  // client-side mock of the export composition, for instant feedback only
  const c = $("#vprev"), img = $("#lframe"); if (!c || !img || !img.naturalWidth) return;
  const ctx = c.getContext("2d"), W = c.width, H = c.height, d = S.layoutEd.data, iw = img.naturalWidth, ih = img.naturalHeight;
  ctx.fillStyle = "#0B0C0F"; ctx.fillRect(0, 0, W, H);
  const fcH = d.facecam ? Math.round(H * d.facecam_h) : 0, brH = d.brand_on ? Math.round(H * d.brand_h) : 0, gpH = H - fcH - brH;
  const cover = (r, dx, dy, dw, dh, pan = 0) => {
    let [sx, sy, sw, sh] = [r[0] * iw, r[1] * ih, r[2] * iw, r[3] * ih];
    const a = dw / dh;
    if (sw / sh > a) { const nw = sh * a; sx += (sw - nw) / 2 * (1 + pan); sw = nw; } else { const nh = sw / a; sy += (sh - nh) / 2; sh = nh; }
    ctx.drawImage(img, sx, sy, sw, sh, dx, dy, dw, dh);
  };
  const contain = (r, dx, dy, dw, dh) => {
    const [sx, sy, sw, sh] = [r[0] * iw, r[1] * ih, r[2] * iw, r[3] * ih];
    ctx.save(); ctx.filter = "blur(8px) brightness(0.8)"; cover(r, dx, dy, dw, dh); ctx.restore();
    const s = Math.min(dw / sw, dh / sh), w = sw * s, hh = sh * s;
    ctx.drawImage(img, sx, sy, sw, sh, dx + (dw - w) / 2, dy + (dh - hh) / 2, w, hh);
  };
  if (d.facecam) cover(d.facecam, 0, 0, W, fcH);
  const g = d.gameplay_mode === "full" ? [0, 0, 1, 1] : (d.gameplay || [0, 0, 1, 1]);
  if (d.gameplay_mode === "crop") cover(g, 0, fcH, W, gpH, d.pan || 0); else contain(g, 0, fcH, W, gpH);
  for (const ins of d.hud_insets || []) {
    const [sx, sy, sw, sh] = [ins.src[0] * iw, ins.src[1] * ih, ins.src[2] * iw, ins.src[3] * ih];
    const w = W * (ins.scale || 0.32), hh = w * sh / sw, m = W * 0.02, pos = ins.pos || "tr";
    const x = pos[1] === "l" ? m : pos[1] === "c" ? (W - w) / 2 : W - w - m, y = pos[0] === "t" ? fcH + m : fcH + gpH - hh - m;
    ctx.drawImage(img, sx, sy, sw, sh, x, y, w, hh);
  }
  if (brH) {
    ctx.fillStyle = d.brand?.color || "#111318"; ctx.fillRect(0, H - brH, W, brH);
    ctx.fillStyle = d.brand?.text_color || "#fff"; ctx.font = `800 ${Math.round(brH * 0.38)}px Poppins, sans-serif`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(d.brand?.text || "", W / 2, H - brH / 2);
  }
}

// ---------- 3. process ----------

async function viewProcess() {
  await refreshState();
  const st = S.settings;
  const ready = S.sources.filter((s) => s.path || s.kind === "youtube");
  $("#main").innerHTML = `<h2>Process</h2>
    <div class="card"><div class="kv">
      <label>Speech language</label><select id="plang"><option value="mixed">Hindi + English mixed (Roman Hinglish)</option><option value="en">English only</option><option value="hi">Hindi only</option><option value="auto">Auto-detect</option></select>
      <label>Candidates per recording</label><input type="number" id="pmax" min="5" max="200" value="${st.max_candidates}">
      <label>Clip length (s)</label><div class="row"><input type="number" id="pmin" min="5" max="120" value="${st.min_clip_s}" style="width:80px"> to
        <input type="number" id="pmaxlen" min="10" max="180" value="${st.max_clip_s}" style="width:80px"></div>
      <label>Quiet stretches checked / hour</label><input type="number" id="pquiet" min="0" max="60" value="${st.quiet_samples_per_hour}">
      <label>Video scan</label><select id="pvis"><option value="auto">Auto (keyframes if dense, else 1 fps)</option><option value="1fps">1 frame per second (slower, misses less)</option><option value="keyframes">Keyframes only (fastest)</option><option value="off">Off</option></select>
    </div><p class="small muted">Long streams are processed in chunks and checkpointed. You can close the app; it resumes where it stopped. Time estimates are estimates.</p></div>
    ${ready.length ? `<div class="card"><table><tr><th>Recording</th><th>Game</th><th style="width:45%">Status</th><th></th></tr>
    ${ready.map((s) => `<tr><td><b>${h(s.title)}</b><div class="small muted">${h(fmtTs(s.duration))} · ${s.counts.n} candidates</div></td>
      <td class="small">${h((S.games.find((g) => g.id === s.game) || {}).name || s.game)}</td>
      <td data-jobslot="${s.id}"></td>
      <td><button class="primary sm" data-proc="${s.id}" ${s.path ? "" : "disabled"}>${s.counts.n ? "Re-process" : "Process"}</button></td></tr>`).join("")}</table></div>`
      : `<div class="empty">Add a recording first.</div>`}`;
  $("#plang").value = st.language_mode; $("#pvis").value = st.visual_scan;
  renderLive();
  $$("[data-proc]").forEach((el) => el.onclick = () => guard(async () => {
    const opts = { language_mode: $("#plang").value, max_candidates: +$("#pmax").value, min_clip_s: +$("#pmin").value,
      max_clip_s: +$("#pmaxlen").value, quiet_samples_per_hour: +$("#pquiet").value, visual_scan: $("#pvis").value };
    if (opts.min_clip_s >= opts.max_clip_s) throw new Error("Minimum length must be shorter than maximum length");
    await api(`/api/sources/${el.dataset.proc}/process`, { method: "POST", body: { options: opts } });
    await poll(); toast("Processing queued");
  }));
}

// ---------- 4. review ----------

const CAT_LABEL = { skill: "Skill play", funny: "Funny squad moment", scare: "Reaction / scare", other: "Other" };

function sourceLink(c, t) {
  const s = c.source || {};
  if (s.kind === "youtube" && s.video_id) return `<a href="https://www.youtube.com/watch?v=${h(s.video_id)}&t=${Math.floor(t)}s" target="_blank" rel="noopener">${fmtTs(t)} ↗</a>`;
  return `<span class="mono">${fmtTs(t)}</span>`;
}

async function viewReview() {
  await refreshState();
  const f = S.filter;
  const q = new URLSearchParams();
  if (f.source) q.set("source_id", f.source); if (f.category) q.set("category", f.category); if (f.status) q.set("status", f.status);
  S.cands = await api("/api/candidates?" + q);
  const tab = (key, val, label) => `<a class="${f[key] === val ? "active" : ""}" data-f="${key}" data-v="${val}">${label}</a>`;
  $("#main").innerHTML = `<h2>Review candidates</h2>
    <div class="row" style="margin-bottom:8px"><select id="fsrc"><option value="">All recordings</option>${S.sources.map((s) => `<option value="${s.id}" ${String(s.id) === f.source ? "selected" : ""}>${h(s.title)}</option>`).join("")}</select>
      <span class="small muted">Scores rank moments inside one recording. They are not predictions of views.</span></div>
    <div class="tabs">${tab("category", "", "All")}${tab("category", "skill", "Skill plays")}${tab("category", "funny", "Funny squad")}${tab("category", "scare", "Reactions / scares")}${tab("category", "other", "Other")}
      <span style="width:16px"></span>${tab("status", "", "Any status")}${tab("status", "new", "New")}${tab("status", "accepted", "Accepted")}${tab("status", "rejected", "Rejected")}${tab("status", "exported", "Exported")}</div>
    ${S.cands.length ? `<div class="grid">${S.cands.map(candCard).join("")}</div>` : `<div class="empty">No candidates here yet. Process a recording first.</div>`}`;
  $("#fsrc").onchange = (e) => { f.source = e.target.value; viewReview(); };
  $$("[data-f]").forEach((el) => el.onclick = () => { f[el.dataset.f] = el.dataset.v; viewReview(); });
  $$("[data-acc]").forEach((el) => el.onclick = () => setStatus(+el.dataset.acc, "accepted"));
  $$("[data-rej]").forEach((el) => el.onclick = () => setStatus(+el.dataset.rej, "rejected"));
  $$("[data-edit]").forEach((el) => el.onclick = () => openEditor(+el.dataset.edit));
}

function candCard(c) {
  const e = c.edit || {}, s0 = e.start ?? c.start, s1 = e.end ?? c.end;
  const off = Math.max(0, s0 - (c.proxy_start || 0)), off1 = Math.max(off, s1 - (c.proxy_start || 0));
  const w = c.warnings || {};
  const warn = (w.flagged ? `<span class="badge warn" title="caption lines to check">${w.flagged} caption line(s) to check</span>` : "")
    + (w.hidden_unclear ? `<span class="badge warn" title="held back until reviewed">${w.hidden_unclear} unclear line(s) hidden</span>` : "");
  const tiers = [...new Set((c.evidence || []).map((x) => x.tier))].map((t) => `<span class="tier ${t}">${t}</span>`).join("");
  return `<div class="cand ${c.status}">
    ${c.proxy_path ? `<video preload="none" controls src="/media/proxy/${c.id}#t=${off.toFixed(1)},${off1.toFixed(1)}" class="${c.source?.orientation === "vertical" ? "vertical" : ""}"></video>` : `<div class="empty">no preview</div>`}
    <div class="body">
      <div class="row"><span class="badge ${c.category}">${h(CAT_LABEL[c.category])}</span>${(c.features?.also || []).map((a) => `<span class="small muted">+${h(a)}</span>`).join(" ")}
        <span class="grow"></span><span class="small muted" title="ranking estimate within this recording">rank ${c.rank_score}</span></div>
      <div class="small">${sourceLink(c, s0)} · ${(s1 - s0).toFixed(0)} s · <span class="muted">${h(c.source?.title || "")}</span></div>
      <div class="reason">${h(c.reason)}</div>
      <div>${tiers} ${warn}</div>
      ${e.hook ? `<div class="small">Hook: <b>${h(e.hook)}</b></div>` : ""}
      <div class="actions"><button class="good sm" data-acc="${c.id}">Accept</button><button class="bad sm" data-rej="${c.id}">Reject</button>
        <button class="sm" data-edit="${c.id}">Edit &amp; preview</button><span class="grow"></span><span class="badge ${c.status}">${h(c.status)}</span></div>
    </div></div>`;
}

async function setStatus(id, status) {
  await guard(async () => {
    await api(`/api/candidates/${id}`, { method: "PATCH", body: { status } });
    if (S.editing && S.editing.id === id) { S.editing = await api(`/api/candidates/${id}`); renderEditor(); }
    else viewReview();
  });
}

async function openEditor(id) {
  S.editing = await api(`/api/candidates/${id}`);
  S.editing._dirty = {};
  renderEditor();
}

function renderEditor() {
  const c = S.editing, e = c.edit || {}, vertical = c.source.orientation === "vertical";
  const [b0, b1] = c.bounds;
  const hooks = c.hooks || [];
  const curHook = e.hook ?? "";
  openModal(`<div class="row"><h2 class="grow" style="margin:0">${h(CAT_LABEL[c.category])} · ${sourceLink(c, b0)}</h2>
      <span class="badge ${c.status}">${h(c.status)}</span><button onclick="closeModal()">Close</button></div>
    <div class="small muted" style="margin:4px 0 12px">${h(c.reason)}</div>
    <div class="edit-grid">
      <div>
        <div class="tabs"><a class="active" data-ptab="src">Source</a><a data-ptab="vert">Vertical preview</a></div>
        <video id="evid" controls preload="metadata" src="/media/proxy/${c.id}"></video>
        <div id="pstatus" class="small muted" style="margin-top:6px"></div>
        <div class="row" style="margin-top:8px"><button class="primary" id="eprev">Render vertical preview</button>
          <span class="small muted">Low-res preview with your layout, captions and hook.</span></div>
        <h3>Evidence</h3>
        <div class="small">${(c.evidence || []).map((x) => `<div><span class="tier ${x.tier}">${x.tier}</span>${h(x.label)} <span class="muted">${fmtTs(x.t)}</span></div>`).join("") || "none"}</div>
        <p class="small muted">observed = measured or read from the screen · model = a model's output with its own uncertainty · inferred = our interpretation</p>
      </div>
      <div>
        <h3 style="margin-top:0">Start and end</h3>
        <div class="row"><input id="estart" value="${fmtTsMs(b0)}" style="width:110px">
          ${[-5, -1, 1, 5].map((d) => `<button class="sm" data-nudge="start" data-d="${d}">${d > 0 ? "+" : ""}${d}s</button>`).join("")}
          <button class="sm" data-playhead="start">= playhead</button></div>
        <div class="row" style="margin-top:6px"><input id="eend" value="${fmtTsMs(b1)}" style="width:110px">
          ${[-5, -1, 1, 5].map((d) => `<button class="sm" data-nudge="end" data-d="${d}">${d > 0 ? "+" : ""}${d}s</button>`).join("")}
          <button class="sm" data-playhead="end">= playhead</button></div>
        <div class="small muted" id="edur">${(b1 - b0).toFixed(1)} s</div>
        <h3>Hook text</h3>
        ${hooks.map((o, i) => `<label class="row small"><input type="radio" name="hook" value="${i}" ${o.text === curHook ? "checked" : ""}> ${h(o.text)} <span class="muted">(${h(o.grounding)})</span></label>`).join("")}
        <label class="row small"><input type="radio" name="hook" value="custom" ${curHook && !hooks.some((o) => o.text === curHook) ? "checked" : ""}> <input id="ehook" class="grow" maxlength="120" value="${h(curHook && !hooks.some((o) => o.text === curHook) ? curHook : "")}" placeholder="Write your own"></label>
        <label class="row small"><input type="radio" name="hook" value="none" ${!curHook ? "checked" : ""}> No hook</label>
        <h3>Captions</h3>
        <div class="row small"><label><input type="checkbox" id="ecap" ${e.captions_on !== false ? "checked" : ""}> Show captions</label>
          <label><input type="checkbox" id="ecen" ${e.censor_on !== false ? "checked" : ""}> Light censor (mask + mute swears)</label></div>
        ${c.warnings.estimated_mute ? `<div class="warnbox">A swear in an edited line is muted at an estimated time. Check the preview.</div>` : ""}
        <div class="lines" id="elines">${c.lines.map((ln, i) => `<div class="line ${ln.hidden ? "hiddenline" : ""} ${ln.status}" title="${h(ln.alt ? "Other reading: " + ln.alt : "")}">
          <span class="small mono muted">${fmtTs(ln.start)}</span><input data-line="${i}" value="${h(ln.text)}">
          ${ln.status !== "ok" ? `<button class="sm good" data-approve="${i}" title="Mark as checked">✓</button>` : ""}
          <button class="sm" data-hide="${i}" title="${ln.hidden ? "Show this line" : "Hide this line"}">${ln.hidden ? "show" : "hide"}</button></div>
          ${ln.alt ? `<div class="small muted" style="padding:0 8px 4px 52px">other reading: ${h(ln.alt)}</div>` : ""}`).join("") || `<div class="small muted" style="padding:8px">No speech found in this clip.</div>`}</div>
        <p class="small muted">Orange = low confidence words or a disputed line. Red = two readings disagreed and both were weak, so the line is hidden until you check it.</p>
        ${vertical ? `<p class="small muted">Vertical source: kept as recorded, only captions and hook are added.</p>` : `
        <h3>Framing</h3>
        <div class="kv"><label>Gameplay</label><select id="emode"><option value="">Layout default</option><option value="crop">Crop</option><option value="fit">Fit (no crop)</option><option value="full">Full frame</option></select>
          <label>Pan crop</label><input type="range" id="epan" min="-1" max="1" step="0.05" value="${e.pan ?? 0}">
          <label>Facecam size</label><div class="row"><input type="range" id="efch" min="0.15" max="0.45" step="0.01" value="${e.facecam_h ?? 0.3}" class="grow"><label class="small"><input type="checkbox" id="efcoff" ${e.facecam_off ? "checked" : ""}> off</label></div>
          <label>Auto HUD insets</label><label class="small"><input type="checkbox" id="eins" ${e.auto_insets !== false ? "checked" : ""}> ${c.auto_insets.length ? h(c.auto_insets.map((i) => i.region).join(", ") + " would be cropped out, so it is added as an inset") : "nothing needed"}</label></div>`}
        <div class="row" style="margin-top:16px"><button class="primary" id="esave">Save changes</button>
          <button class="good" id="eacc">Accept</button><button class="bad" id="erej">Reject</button></div>
      </div></div>`);
  if (!vertical) $("#emode").value = e.gameplay_mode || "";
  const vid = $("#evid");
  vid.addEventListener("loadedmetadata", () => { vid.currentTime = Math.max(0, b0 - (c.proxy_start || 0)); }, { once: true });
  $$("[data-ptab]").forEach((el) => el.onclick = () => {
    $$("[data-ptab]").forEach((x) => x.classList.toggle("active", x === el));
    if (el.dataset.ptab === "vert") { vid.src = `/media/preview/${c.id}?_=${Date.now()}`; vid.onerror = () => { $("#pstatus").textContent = "No preview yet. Render one first."; }; }
    else vid.src = `/media/proxy/${c.id}`;
  });
  const setTime = (which, t) => {
    $(which === "start" ? "#estart" : "#eend").value = fmtTsMs(Math.max(0, t));
    $("#edur").textContent = (parseTs($("#eend").value) - parseTs($("#estart").value)).toFixed(1) + " s";
  };
  $$("[data-nudge]").forEach((el) => el.onclick = () => guard(() => {
    const id = el.dataset.nudge === "start" ? "#estart" : "#eend";
    setTime(el.dataset.nudge, parseTs($(id).value) + +el.dataset.d);
  }));
  $$("[data-playhead]").forEach((el) => el.onclick = () => {
    if (!vid.src.includes("/proxy/")) return toast("Switch to the Source tab to pick a time", true);
    setTime(el.dataset.playhead, (c.proxy_start || 0) + vid.currentTime);
  });
  $$("[data-approve]").forEach((el) => el.onclick = () => { S.editing._dirty[+el.dataset.approve] = { ...(S.editing._dirty[+el.dataset.approve] || {}), approved: true }; el.closest(".line").classList.remove("flagged", "unclear"); el.remove(); });
  $$("[data-hide]").forEach((el) => el.onclick = () => {
    const i = +el.dataset.hide, ln = c.lines[i], now = !(S.editing._dirty[i]?.hidden ?? ln.hidden);
    S.editing._dirty[i] = { ...(S.editing._dirty[i] || {}), hidden: now };
    el.textContent = now ? "show" : "hide"; el.closest(".line").classList.toggle("hiddenline", now);
  });
  $$("[data-line]").forEach((el) => el.oninput = () => { S.editing._dirty[+el.dataset.line] = { ...(S.editing._dirty[+el.dataset.line] || {}), text: el.value }; });
  $("#esave").onclick = () => guard(() => saveEdits());
  $("#eacc").onclick = () => guard(async () => { await saveEdits(true); await setStatus(c.id, "accepted"); });
  $("#erej").onclick = () => setStatus(c.id, "rejected");
  $("#eprev").onclick = () => guard(async () => {
    await saveEdits(true);
    const r = await api(`/api/candidates/${c.id}/preview`, { method: "POST" });
    S.editing._previewJob = r.job_id; $("#pstatus").textContent = "Rendering preview…";
  });
  updateEditJobs();
}

async function saveEdits(quiet = false) {
  const c = S.editing, edit = {};
  const s = parseTs($("#estart").value), en = parseTs($("#eend").value);
  if (en - s < 1) throw new Error("End must be after start");
  if (Math.abs(s - c.bounds[0]) > 0.05) edit.start = s;
  if (Math.abs(en - c.bounds[1]) > 0.05) edit.end = en;
  const pick = $("input[name=hook]:checked")?.value;
  edit.hook = pick === "none" ? "" : pick === "custom" ? $("#ehook").value.trim() : (c.hooks[+pick]?.text || "");
  edit.captions_on = $("#ecap").checked; edit.censor_on = $("#ecen").checked;
  if ($("#emode")) {
    edit.gameplay_mode = $("#emode").value || null; edit.pan = +$("#epan").value;
    edit.facecam_h = +$("#efch").value; edit.facecam_off = $("#efcoff").checked; edit.auto_insets = $("#eins").checked;
  }
  const ov = [...(c.edit.caption_overrides || [])];
  for (const [i, d] of Object.entries(S.editing._dirty)) {
    const ln = c.lines[+i];
    const k = ov.findIndex((o) => Math.abs(o.abs_start - ln.abs_start) < 0.05 && Math.abs(o.abs_end - ln.abs_end) < 0.05);
    const entry = { ...(k >= 0 ? ov[k] : {}), abs_start: ln.abs_start, abs_end: ln.abs_end, ...d };
    if (d.text !== undefined) entry.approved = true;  // a line you typed is a line you checked
    if (k >= 0) ov[k] = entry; else ov.push(entry);
  }
  edit.caption_overrides = ov;
  const prevJob = S.editing._previewJob;
  S.editing = await api(`/api/candidates/${c.id}`, { method: "PATCH", body: { edit } });
  S.editing._dirty = {}; S.editing._previewJob = prevJob;
  if (!quiet) { toast("Saved"); renderEditor(); }
}

function updateEditJobs() {
  const c = S.editing; if (!c || !$("#pstatus")) return;
  const j = S.jobs.find((x) => x.id === c._previewJob);
  if (!j) return;
  if (j.status === "done") {
    $("#pstatus").textContent = "Preview ready.";
    c._previewJob = null;
    $$("[data-ptab]").forEach((x) => x.classList.toggle("active", x.dataset.ptab === "vert"));
    $("#evid").src = `/media/preview/${c.id}?_=${Date.now()}`;
  } else if (j.status === "failed") { $("#pstatus").innerHTML = `<span class="errbox">${h(j.error)}</span>`; c._previewJob = null; }
  else $("#pstatus").textContent = `Rendering preview… ${Math.round((j.progress || 0) * 100)}%`;
}

// ---------- 5. export ----------

async function viewExport() {
  await refreshState();
  const cands = (await api("/api/candidates")).filter((c) => c.status === "accepted" || c.status === "exported");
  $("#main").innerHTML = `<h2>Export</h2>
    <div class="card"><div class="row"><span class="grow small muted">1080x1920 MP4, highest quality settings. Exports go to the export folder (Settings); recordings are never changed.</span>
      <button id="opendir">Open export folder</button><button class="primary" id="doexp">Export selected</button></div></div>
    ${cands.length ? `<div class="card"><table><tr><th><input type="checkbox" id="selall"></th><th>Clip</th><th>Length</th><th style="width:40%">Status</th><th></th></tr>
      ${cands.map((c) => { const e = c.edit || {}, s0 = e.start ?? c.start, s1 = e.end ?? c.end; return `<tr>
        <td><input type="checkbox" data-sel="${c.id}" ${c.status === "accepted" ? "checked" : ""}></td>
        <td><span class="badge ${c.category}">${h(CAT_LABEL[c.category])}</span> ${sourceLink(c, s0)} <span class="small muted">${h(c.source?.title)}</span>
          ${e.hook ? `<div class="small">“${h(e.hook)}”</div>` : ""}${c.warnings?.flagged ? `<div class="small" style="color:var(--warn)">${c.warnings.flagged} caption line(s) not checked</div>` : ""}</td>
        <td>${(s1 - s0).toFixed(0)} s</td><td data-exslot="${c.id}"></td>
        <td>${c.status === "exported" ? `<a href="/media/export/${c.id}" target="_blank">Play</a>` : ""}</td></tr>`; }).join("")}</table></div>`
      : `<div class="empty">Accept some candidates in Review first.</div>`}`;
  renderLive();
  if ($("#selall")) $("#selall").onchange = (e) => $$("[data-sel]").forEach((x) => x.checked = e.target.checked);
  $("#opendir").onclick = () => guard(() => api("/api/open", { method: "POST", body: { what: "exports" } }));
  $("#doexp").onclick = () => guard(async () => {
    const ids = $$("[data-sel]").filter((x) => x.checked).map((x) => +x.dataset.sel);
    if (!ids.length) throw new Error("Select at least one clip");
    await api("/api/export", { method: "POST", body: { candidate_ids: ids } });
    await poll(); toast(`Exporting ${ids.length} clip(s)`);
  });
}

// ---------- references ----------

async function viewRefs() {
  const [refs, prefs] = await Promise.all([api("/api/references"), api("/api/preferences")]);
  const sg = prefs.suggestions;
  const fmtDate = (t) => t ? new Date(t * 1000).toISOString().slice(0, 10) : "";
  $("#main").innerHTML = `<h2>Reference library</h2>
    <p class="muted small">Your past clips guide ranking and style. Everything here is optional; processing works without it.</p>
    <div class="card"><div class="row"><button class="primary" id="rpick">Import reference MP4s…</button>
      <label class="small">Metrics CSV <input type="file" id="rcsv" accept=".csv,text/csv"></label>
      <button id="ranal">Analyse new references</button><button id="rgraph">Fetch from Instagram API</button></div>
      <details style="margin-top:8px"><summary class="small">Public post links (best effort, no login)</summary>
        <textarea id="rurls" placeholder="https://www.instagram.com/reel/..."></textarea><button class="sm" id="rpub">Read public numbers</button>
        <div id="rpubres" class="small"></div>
        <p class="small muted">Instagram often hides these without a login. ClipSift does not log in or work around that. The reliable routes are the CSV export from Meta Business Suite (Insights > Content > Export) or the official API with your own token (Settings).</p></details></div>
    <div class="card"><h3 style="margin-top:0">Suggestions</h3>
      <div class="small">${h(sg.n_refs)} references, ${h(sg.n_with_metrics)} with views/likes, ${h(sg.n_analysed)} analysed, ${h(sg.n_decisions)} accept/reject choices.</div>
      ${sg.duration ? `<div>Clip length around <b>${sg.duration.target} s</b> (range ${sg.duration.range.join(" to ")} s, ${sg.duration.n} clips, ${h(sg.duration.basis)})</div>` : ""}
      <div>Moment type weights: ${Object.entries(sg.category_weights).map(([k, v]) => `${h(CAT_LABEL[k])} ×${v}`).join(" · ")}</div>
      ${sg.captions ? `<div>Captions in your clips: about ${sg.captions.words_per_line} words per line, ${sg.captions.upper ? "mostly UPPERCASE" : "mixed case"}, at ${Math.round(sg.captions.y * 100)}% height (${sg.captions.n} clips, ${h(sg.captions.basis)})</div>` : ""}
      ${sg.pacing ? `<div>Pacing: better clips ${sg.pacing.top_words_per_s} words/s and ${sg.pacing.top_cuts_per_min} cuts/min vs ${sg.pacing.rest_words_per_s} and ${sg.pacing.rest_cuts_per_min} for the rest</div>` : ""}
      ${sg.notes.map((n) => `<div class="small muted">${h(n)}</div>`).join("")}
      <div class="row" style="margin-top:8px"><button class="sm" data-apply="category_weights">Use type weights</button><button class="sm" data-apply="duration" ${sg.duration ? "" : "disabled"}>Use clip length</button>
        <button class="sm" data-apply="captions" ${sg.captions ? "" : "disabled"}>Use caption style</button>
        <label class="small"><input type="checkbox" id="ruse" ${prefs.current.use_preferences ? "checked" : ""}> use preferences in ranking</label></div></div>
    ${refs.length ? `<div class="card"><table><tr><th>Clip</th><th>Posted</th><th>Views</th><th>Likes</th><th>Comments</th><th title="relative to your other clips, age-adjusted when dates are known">Perf.</th><th>Type</th><th>★ / ✕</th><th></th></tr>
      ${refs.map((r) => `<tr><td><b>${h(r.title || r.permalink || r.path)}</b><div class="small muted mono">${h(r.path || r.permalink || "")}</div></td>
        <td><input data-rf="${r.id}:posted_at" value="${h(fmtDate(r.posted_at))}" style="width:105px" placeholder="yyyy-mm-dd"></td>
        ${["views", "likes", "comments"].map((k) => `<td><input data-rf="${r.id}:${k}" value="${r[k] ?? ""}" style="width:80px" placeholder="unknown"></td>`).join("")}
        <td class="small">${r.perf ? r.perf.z.toFixed(2) : "-"}</td><td class="small">${h(r.features?.category || (r.analyzed_at ? "?" : "not analysed"))}</td>
        <td><label><input type="checkbox" data-rfav="${r.id}" ${r.favourite ? "checked" : ""}>★</label> <label><input type="checkbox" data-rrej="${r.id}" ${r.rejected ? "checked" : ""}>✕</label></td>
        <td><button class="sm bad" data-rdel="${r.id}">✕</button></td></tr>`).join("")}</table>
      <p class="small muted">Blank means unknown, which is different from 0. Views and likes depend on lots outside the clip; these are hints, not causes.</p></div>` : `<div class="empty">No references yet.</div>`}`;
  $("#rpick").onclick = () => guard(async () => {
    const p = await api("/api/pick-files", { method: "POST", body: { kind: "video" } });
    if (!p.paths.length) return;
    const r = await api("/api/references/import", { method: "POST", body: { paths: p.paths } });
    toast(`Imported ${r.added.length}; analysing in the background`); viewRefs();
  });
  $("#rcsv").onchange = (e) => guard(async () => {
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    const r = await api("/api/references/csv", { method: "POST", form: fd });
    toast(`CSV: ${r.created} new, ${r.updated} updated`); viewRefs();
  });
  $("#ranal").onclick = () => guard(async () => { await api("/api/references/analyze", { method: "POST", body: {} }); toast("Analysis queued"); });
  $("#rgraph").onclick = () => guard(async () => { const r = await api("/api/instagram/graph-fetch", { method: "POST" }); toast(`Instagram API: ${r.rows} posts`); viewRefs(); });
  $("#rpub").onclick = () => guard(async () => {
    const r = await api("/api/instagram/public", { method: "POST", body: { urls: $("#rurls").value.split(/\s+/).filter(Boolean) } });
    $("#rpubres").innerHTML = r.results.map((x) => `<div>${h(x.url)}: ${x.ok ? `views ${h(x.views ?? "unknown")}, likes ${h(x.likes ?? "unknown")}` : `<span style="color:var(--bad)">${h(x.error)}</span>`}</div>`).join("");
  });
  $$("[data-rf]").forEach((el) => el.onchange = () => guard(async () => {
    const [id, k] = el.dataset.rf.split(":");
    await api(`/api/references/${id}`, { method: "PATCH", body: { [k]: el.value.trim() } }); toast("Saved");
  }));
  $$("[data-rfav]").forEach((el) => el.onchange = () => guard(() => api(`/api/references/${el.dataset.rfav}`, { method: "PATCH", body: { favourite: el.checked } })));
  $$("[data-rrej]").forEach((el) => el.onchange = () => guard(() => api(`/api/references/${el.dataset.rrej}`, { method: "PATCH", body: { rejected: el.checked } })));
  $$("[data-rdel]").forEach((el) => el.onclick = () => guard(async () => { if (confirm("Remove from the library? The file stays on disk.")) { await api(`/api/references/${el.dataset.rdel}`, { method: "DELETE" }); viewRefs(); } }));
  $$("[data-apply]").forEach((el) => el.onclick = () => guard(async () => { await api("/api/preferences/apply", { method: "POST", body: { parts: [el.dataset.apply] } }); S.settings = await api("/api/settings"); toast("Applied. You can change these in Settings."); }));
  $("#ruse").onchange = (e) => guard(async () => { S.settings = await api("/api/settings", { method: "PUT", body: { use_preferences: e.target.checked } }); });
}

// ---------- settings ----------

async function viewSettings() {
  const [st, sys, gl, gsi] = await Promise.all([api("/api/settings"), api("/api/system"), api("/api/glossary"), api("/api/gsi/status")]);
  S.settings = st;
  const modelRows = sys.models.map((m) => `<tr><td>${h(m.name)}</td><td class="small">${h(m.note)}</td><td>${m.mb} MB</td><td class="small">${h(m.license)}</td>
    <td>${m.ready ? `<span class="badge accepted">ready</span>` : m.convert ? `<span class="small muted">run setup\\convert_models.ps1</span>` : `<button class="sm" data-dl="${m.name}">Download</button>`}</td></tr>`).join("");
  $("#main").innerHTML = `<h2>Settings</h2>
    <div class="card"><h3 style="margin-top:0">This PC</h3><div class="small">
      GPU: ${sys.gpus.length ? h(sys.gpus.map((g) => `${g.name} (${g.free_mb} of ${g.total_mb} MB free)`).join(", ")) : "no NVIDIA GPU found"} · CUDA devices for speech: ${sys.cuda_devices}
      · RAM free ${sys.ram_free_mb} MB · NVENC: ${sys.nvenc ? "yes" : "no"} · ffmpeg: ${sys.ffmpeg ? "found" : `<b style="color:var(--bad)">missing</b>`} · Deno (YouTube): ${sys.deno ? "found" : `<b style="color:var(--warn)">missing</b>`} · free disk ${h(sys.disk_free)}</div></div>
    <div class="card"><div class="kv">
      <label>Creator name</label><input data-s="creator_name" value="${h(st.creator_name)}">
      <label>In-game names</label><input data-s="ingame_names" data-list="1" value="${h((st.ingame_names || []).join(", "))}" title="comma separated; used to read your kills in the kill feed">
      <label>Export folder</label><input data-s="export_dir" value="${h(st.export_dir)}" placeholder="${h(sys.home)}\\exports">
      <label>Speech device</label><select data-s="asr_device"><option value="auto">Auto (GPU if it has room)</option><option value="cuda">GPU</option><option value="cpu">CPU (slow)</option></select>
      <label>GPU speech model</label><select data-s="asr_model_gpu">${sys.models.map((m) => `<option value="${m.name}">${h(m.name)}</option>`).join("")}</select>
      <label>CPU speech model</label><select data-s="asr_model_cpu">${sys.models.map((m) => `<option value="${m.name}">${h(m.name)}</option>`).join("")}</select>
      <label>Download quality</label><select data-s="download_quality"><option value="best">Best available</option><option value="1080">1080p</option><option value="720">720p</option></select>
      <label>Export quality</label><select data-s="export_quality"><option value="max">Maximum (slower)</option><option value="fast">Fast</option></select>
      <label>Encoder</label><select data-s="encoder"><option value="auto">Auto (NVENC if it works)</option><option value="nvenc">NVENC</option><option value="x264">x264 (CPU)</option></select>
      <label>Captions on by default</label><input type="checkbox" data-s="captions_on" ${st.captions_on ? "checked" : ""}>
      <label>Words per caption</label><input type="number" min="1" max="8" data-s="caption_words_max" value="${st.caption_words_max}">
      <label>UPPERCASE captions</label><input type="checkbox" data-s="caption_upper" ${st.caption_upper ? "checked" : ""}>
      <label>Highlight spoken word</label><input type="checkbox" data-s="caption_highlight" ${st.caption_highlight ? "checked" : ""}>
      <label>Caption height (layout)</label><input type="number" step="0.01" min="0.3" max="0.9" data-s="caption_y" value="${st.caption_y}">
      <label>Caption height (vertical)</label><input type="number" step="0.01" min="0.3" max="0.9" data-s="vertical_caption_y" value="${st.vertical_caption_y}">
      <label>Hook seconds (0 = whole clip)</label><input type="number" min="0" max="60" data-s="hook_seconds" value="${st.hook_seconds}">
      <label>Light censor by default</label><input type="checkbox" data-s="censor_on" ${st.censor_on ? "checked" : ""}>
      <label>Extra words to censor</label><input data-s="profanity_extra" data-list="1" value="${h((st.profanity_extra || []).join(", "))}">
      <label>Never censor</label><input data-s="profanity_allow" data-list="1" value="${h((st.profanity_allow || []).join(", "))}">
      <label>Loudness normalize</label><input type="checkbox" data-s="loudnorm" ${st.loudnorm ? "checked" : ""}>
      <label>Laughter/scream model</label><input type="checkbox" data-s="use_audio_tagger" ${st.use_audio_tagger ? "checked" : ""}>
      <label>Instagram account id</label><input data-s="instagram_graph_user_id" value="${h(st.instagram_graph_user_id)}" placeholder="optional, for the official API">
      <label>Instagram API token</label><input type="password" data-s="instagram_graph_token" value="${h(st.instagram_graph_token)}" placeholder="optional; stored only on this PC">
    </div><div class="row" style="margin-top:12px"><button class="primary" id="ssave">Save settings</button></div></div>
    <div class="card"><h3 style="margin-top:0">Glossary</h3><p class="small muted">Names and terms the speech model gets wrong. One per line: <span class="mono">Correct term = how it gets heard, another way</span></p>
      <textarea id="gloss" style="min-height:140px">${h(gl.map((g) => g.term + " = " + (g.aliases || []).join(", ")).join("\n"))}</textarea>
      <button id="gsave">Save glossary</button></div>
    <div class="card"><h3 style="margin-top:0">Models</h3><table><tr><th>Speech model</th><th>Use</th><th>Size</th><th>License</th><th></th></tr>${modelRows}
      <tr><td>ced-mini (laughter/screams)</td><td class="small">AudioSet event tagger</td><td>${sys.tagger.mb} MB</td><td class="small">${h(sys.tagger.license)}</td>
      <td>${sys.tagger.ready ? `<span class="badge accepted">ready</span>` : `<button class="sm" data-dl="ced-mini">Download</button>`}</td></tr></table></div>
    <div class="card"><h3 style="margin-top:0">Live game events (CS2, Dota 2)</h3>
      <p class="small">While ClipSift is open, CS2 and Dota 2 can send your kills, defuses and round results to it (Valve's Game State Integration, the same feed SteelSeries Moments uses). Processing lines them up with the recording.</p>
      <div class="small">CS2: ${gsi.cs2.events_24h} events in the last day · Dota 2: ${gsi.dota2.events_24h}</div>
      <div class="row" style="margin-top:8px"><button data-gsi="cs2">Set up CS2</button><button data-gsi="dota2">Set up Dota 2</button></div><div id="gsiout"></div></div>`;
  $$("select[data-s]").forEach((el) => el.value = st[el.dataset.s]);
  $("#ssave").onclick = () => guard(async () => {
    const vals = {};
    $$("[data-s]").forEach((el) => {
      const k = el.dataset.s;
      if (el.type === "checkbox") vals[k] = el.checked;
      else if (el.dataset.list) vals[k] = el.value.split(",").map((x) => x.trim()).filter(Boolean);
      else if (el.type === "number") vals[k] = +el.value;
      else vals[k] = el.value;
    });
    S.settings = await api("/api/settings", { method: "PUT", body: vals }); toast("Settings saved");
  });
  $("#gsave").onclick = () => guard(async () => {
    const entries = $("#gloss").value.split(/\r?\n/).filter((l) => l.trim()).map((l) => {
      const [term, rest = ""] = l.split("="); return { term: term.trim(), aliases: rest.split(",").map((x) => x.trim()).filter(Boolean) };
    });
    await api("/api/glossary", { method: "PUT", body: entries }); toast("Glossary saved. It applies the next time a recording is processed.");
  });
  $$("[data-dl]").forEach((el) => el.onclick = () => guard(async () => { await api("/api/models/download", { method: "POST", body: { name: el.dataset.dl } }); toast("Download started"); }));
  $$("[data-gsi]").forEach((el) => el.onclick = () => guard(async () => {
    const g = el.dataset.gsi, cfg = await api(`/api/gsi/config/${g}`);
    $("#gsiout").innerHTML = `<p class="small">${cfg.path ? `Will be saved to <span class="mono">${h(cfg.path)}</span>` : h(cfg.hint)}</p>
      <pre class="log">${h(cfg.text)}</pre>${cfg.path ? `<button class="primary" id="gsiinst">Install for ${h(g)}</button>` : ""}`;
    if ($("#gsiinst")) $("#gsiinst").onclick = () => guard(async () => { const r = await api(`/api/gsi/install/${g}`, { method: "POST" }); toast("Installed. " + (r.note || "")); });
  }));
}

// ---------- storage ----------

async function viewStorage() {
  const [stg, logs] = await Promise.all([api("/api/storage"), api("/api/logs?name=worker&lines=200")]);
  const hu = stg.human;
  $("#main").innerHTML = `<h2>Storage &amp; logs</h2>
    <div class="card"><div class="kv"><label>Free disk</label><b>${h(hu.free)}</b><label>Work files</label><span>${h(hu.work)}</span>
      <label>Previews</label><span>${h(hu.previews)}</span><label>Downloads</label><span>${h(hu.downloads)}</span>
      <label>Exports</label><span>${h(hu.exports)}</span><label>Models</label><span>${h(hu.models)}</span></div>
      <p class="small muted">Cleaning removes ClipSift's temporary files only. Your recordings, downloads and exports are not touched. Re-processing re-creates work files.</p>
      <div class="row"><button data-clean="previews">Clear previews</button><button data-clean="work">Clear work files</button><button data-clean="all_temp">Clear all temporary files</button>
        <button id="openhome">Open data folder</button></div></div>
    <div class="card"><div class="row"><h3 class="grow" style="margin:0">Log</h3><select id="logsel"><option>worker</option><option>server</option></select><button id="openlogs">Open log folder</button></div>
      <pre class="log" id="logbox">${h(logs.lines.join("\n"))}</pre></div>`;
  $$("[data-clean]").forEach((el) => el.onclick = () => guard(async () => {
    if (!confirm("Delete these temporary files?")) return;
    await api("/api/storage/clean", { method: "POST", body: { what: el.dataset.clean } }); toast("Cleaned"); viewStorage();
  }));
  $("#logsel").onchange = (e) => guard(async () => { $("#logbox").textContent = (await api(`/api/logs?name=${e.target.value}&lines=300`)).lines.join("\n"); });
  $("#openhome").onclick = () => guard(() => api("/api/open", { method: "POST", body: { what: "home" } }));
  $("#openlogs").onclick = () => guard(() => api("/api/open", { method: "POST", body: { what: "logs" } }));
}

// ---------- modal ----------

function openModal(html) { $("#modalbox").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() {
  $("#modal").classList.add("hidden"); $("#modalbox").innerHTML = "";
  if (S.editing) { S.editing = null; if (S.view === "review") viewReview(); }
}
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#modal").classList.contains("hidden")) closeModal(); });
window.closeModal = closeModal;

// ---------- boot ----------

(async function boot() {
  try {
    [S.games, S.layouts, S.settings] = await Promise.all([api("/api/games"), api("/api/layouts"), api("/api/settings")]);
    api("/api/system").then((s) => {
      $("#sysline").textContent = (s.gpus.length ? s.gpus[0].name + " · " : "No NVIDIA GPU · ") + (s.nvenc ? "NVENC · " : "") + "free disk " + s.disk_free
        + (s.ffmpeg ? "" : " · ffmpeg missing!");
    }).catch(() => {});
  } catch (e) { toast("Could not reach ClipSift: " + e.message, true); }
  go("add");
  S.poll = setInterval(poll, 1500);
})();
