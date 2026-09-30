/* Thimble — Embroidery Studio front end */
"use strict";

// ------------------------------------------------------------------ state
const DEFAULT_LAYOUT = () => ({
  name: "My design", hoop: [100, 100], fabric: "knit", group_colors: true, fabric_color: "#d9d5cc", elements: [],
});
const S = {
  pan: { x: 0, y: 0 }, // screen px the view is slid by (middle-mouse drag, like a slicer)
  layout: DEFAULT_LAYOUT(), sel: -1, built: null, meta: null, fonts: [], zoom: 1, pan: { x: 0, y: 0 },
  undo: [], redo: [], images: {}, buildSeq: 0, dirty: true, sew: null,
};
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const uid = () => Math.random().toString(36).slice(2, 9);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// long requests carry a job id; while they run the busy badge polls the server for % done
let jobSeq = 0, jobPoll = null;
function watchJob(job) {
  clearInterval(jobPoll);
  const bar = $("#busyBar"), pct = $("#busyPct");
  bar.style.width = "0%"; pct.textContent = "";
  jobPoll = setInterval(async () => {
    try {
      const p = await (await fetch(`/api/progress/${job}`)).json();
      if (p.pct > 0) {
        bar.style.width = p.pct + "%"; pct.textContent = p.pct + "%";
        if (p.msg) $("#busySub").textContent = p.msg;
      }
    } catch (e) {}
  }, 350);
}
function stopJob() { clearInterval(jobPoll); jobPoll = null; $("#busySub").textContent = ""; $("#busyPct").textContent = ""; $("#busyBar").style.width = "0%"; }
async function api(path, body, opts = {}) {
  const long = ["/api/build", "/api/ai/analyze", "/api/export", "/api/disk/write"].includes(path);
  const job = long ? `j${Date.now().toString(36)}${jobSeq++}` : null;
  if (job) watchJob(job);
  let r;
  try {
    r = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, job ? { "X-Job": job } : {}), body: JSON.stringify(body),
    });
  } finally { if (job) stopJob(); }
  if (opts.blob) {
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r;
  }
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = `<span class="pin"></span><span>${msg}</span>`;
  $("#toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .4s"; }, kind === "bad" ? 6500 : 3800);
  setTimeout(() => t.remove(), kind === "bad" ? 7000 : 4300);
}

let busyTimer = null;
function busy(on, text = "Threading the needle…") {
  clearTimeout(busyTimer);
  // quick rebuilds: small badge in the corner; bigger jobs (saving, pictures, AI): centred on the hoop
  if (on) busyTimer = setTimeout(() => {
    $("#busyText").textContent = text; $("#busy").classList.toggle("big", on === "now"); $("#busy").hidden = false;
    const v = $("#busy video"); if (v) { v.currentTime = 0; v.play().catch(() => {}); }
  }, on === "now" ? 0 : 160);
  else $("#busy").hidden = true;
}

// ------------------------------------------------------------------ history
let histTimer = null;
function snapshot() { return JSON.stringify(S.layout); }
function commit(immediate = false) {
  endSew();
  // coalesce rapid edits (typing, sliders) into one undo step
  const snap = S._pending || snapshot();
  if (!S._pending) S._pending = snap;
  clearTimeout(histTimer);
  const push = () => { if (S._pending && S._pending !== snapshot()) { S.undo.push(S._pending); S.redo = []; if (S.undo.length > 100) S.undo.shift(); } S._pending = null; };
  if (immediate) push(); else histTimer = setTimeout(push, 600);
}
function beginEdit() { endSew(); if (!S._pending) S._pending = snapshot(); }
// like a slicer's preview: touching the design drops you straight back into editing
function endSew() {
  if (!S.sew) return;
  S.sew = null;
  $("#sewBar").hidden = true;
  draw();
}
function undo() {
  endSew();
  if (S._pending) { clearTimeout(histTimer); if (S._pending !== snapshot()) { S.undo.push(S._pending); } S._pending = null; }
  if (!S.undo.length) return;
  S.redo.push(snapshot()); S.layout = JSON.parse(S.undo.pop()); afterLoad();
}
function redo() {
  endSew();
  if (!S.redo.length) return;
  S.undo.push(snapshot()); S.layout = JSON.parse(S.redo.pop()); afterLoad();
}
function afterLoad() {
  if (S.sel >= S.layout.elements.length) S.sel = -1;
  $("#designName").value = S.layout.name || "My design";
  renderLayers(); renderProps(); scheduleBuild(0); save_local();
}

// ------------------------------------------------------------------ build
let buildTimer = null;
function scheduleBuild(delay = 280) {
  S.dirty = true;
  clearTimeout(buildTimer);
  buildTimer = setTimeout(build, delay);
}
async function build() {
  const seq = ++S.buildSeq;
  busy(true);
  try {
    const res = await api("/api/build", S.layout);
    if (seq !== S.buildSeq) return;
    S.built = res; S.dirty = false; S.vis = {};
    renderChart(); draw();
    if (S.autoSimplify) { S.autoSimplify = false; simplifyThreads(true); }
  } catch (e) {
    if (seq === S.buildSeq) toast("Couldn't stitch that: " + esc(e.message), "bad");
  } finally {
    if (seq === S.buildSeq) busy(false);
  }
}
function save_local() { try { localStorage.setItem("thimble.layout", snapshot()); } catch (e) { /* private mode */ } }

// ------------------------------------------------------------------ examples
const EXAMPLES = {
  limited: { name: "Limited edition", fabric: "fleece", fabric_color: "#d9d5cc", elements: [
    { type: "shape", kind: "offset_frame", width_mm: 86, height_mm: 40, stroke_mm: 1.0, color: "#9a6b3f", style: "auto", x: 0, y: 2, rotation: 0 },
    { type: "text", text: "LIMITED", font: "Alfa Slab One", height_mm: 12, letter_spacing: 0.04, color: "#b98a5a", outline: { color: "#6b4423", width_mm: 0.9 }, style: "auto", arc: 0, x: 0, y: -5, rotation: 0 },
    { type: "text", text: "edition", font: "Yellowtail", height_mm: 12, letter_spacing: 0, color: "#1d1d1d", style: "auto", arc: 0, x: 6, y: 13, rotation: 0 },
    { type: "shape", kind: "heart", width_mm: 6, height_mm: 5.5, color: "#d62839", style: "auto", x: 0, y: -24, rotation: 0 }] },
  varsity: { name: "Varsity", fabric: "fleece", fabric_color: "#1f2a44", elements: [
    { type: "text", text: "RIDGELINE", font: "Graduate", height_mm: 9, letter_spacing: 0.06, arc: 70, color: "#f2f0ea", outline: { color: "#b8322a", width_mm: 1.0 }, style: "auto", x: 0, y: -14, rotation: 0 },
    { type: "text", text: "Athletics", font: "Pacifico", height_mm: 11, letter_spacing: 0, arc: 0, color: "#c9a24a", style: "auto", x: 0, y: 6, rotation: -6 },
    { type: "text", text: "EST. 2026", font: "Oswald", height_mm: 5.5, letter_spacing: 0.25, arc: 0, color: "#f2f0ea", style: "auto", x: 0, y: 22, rotation: 0 }] },
  monogram: { name: "Monogram", fabric: "woven", fabric_color: "#f6f4ef", elements: [
    { type: "shape", kind: "ring", width_mm: 52, height_mm: 52, stroke_mm: 1.4, color: "#6f8c6a", style: "auto", x: 0, y: 0, rotation: 0 },
    { type: "text", text: "S", font: "Great Vibes", height_mm: 30, letter_spacing: 0, arc: 0, color: "#3a2c22", style: "auto", x: 0, y: 1, rotation: 0 },
    { type: "text", text: "SEW  WELL", font: "Cinzel Bold", height_mm: 4.5, letter_spacing: 0.3, arc: -150, color: "#6f8c6a", style: "auto", x: 0, y: 32, rotation: 0 }] },
};
function loadExample(key) {
  const ex = EXAMPLES[key];
  if (!ex) return;
  commit(true);
  S.layout = Object.assign(DEFAULT_LAYOUT(), JSON.parse(JSON.stringify(ex)), { hoop: S.layout.hoop });
  S.layout.elements.forEach((e) => (e.id = uid()));
  S.sel = -1;
  afterLoad();
  toast(`Loaded the “${esc(ex.name)}” example — click anything to change it.`);
}

// ------------------------------------------------------------------ elements
const SHAPES = [["heart", "Heart"], ["star", "Star"], ["circle", "Circle"], ["ring", "Ring"], ["rect", "Block"],
  ["frame", "Frame"], ["double_frame", "Double frame"], ["offset_frame", "Offset frame"], ["line", "Line"]];
const STYLES = [["auto", "Auto (recommended)"], ["satin", "Satin — follows the strokes"], ["satinfill", "Satin — one direction"],
  ["fill", "Fill — tatami"], ["run", "Outline — running stitch"]];

function freeSpotY(h) {
  // stack new things under existing ones so they don't land on top of each other
  const els = S.layout.elements;
  if (!els.length || !S.built) return 0;
  let bottom = -Infinity;
  els.forEach((el, i) => { const b = S.built.elements[i]; if (b) bottom = Math.max(bottom, el.y + b.h / 2); });
  const y = bottom + 4 + h / 2;
  return y + h / 2 < S.layout.hoop[1] / 2 ? y : 0;
}

function addElement(el) {
  commit(true); beginEdit();
  el.id = uid();
  S.layout.elements.push(el);
  S.sel = S.layout.elements.length - 1;
  commit(true);
  renderLayers(); renderProps(); scheduleBuild(0); save_local();
}

function addText() {
  addElement({ type: "text", text: "Your words", font: "✦ Geneva Simple Sans", height_mm: 10, color: "#2b4c7e",
    letter_spacing: 0.03, line_spacing: 1.25, arc: 0, align: "center", style: "auto", outline: null,
    x: 0, y: freeSpotY(10), rotation: 0 });
  setTimeout(() => { const t = $("#p-text"); if (t) { t.focus(); t.select(); } }, 50);
}
function addShape() {
  addElement({ type: "shape", kind: "heart", width_mm: 14, height_mm: 13, stroke_mm: 1.2, color: "#c0392b",
    style: "auto", outline: null, x: 0, y: freeSpotY(13), rotation: 0 });
}

async function uploadPicture(file) {
  const fd = new FormData(); fd.append("file", file);
  const r = await fetch("/api/upload", { method: "POST", body: fd });
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || "upload failed");
  return j;
}

function fitWidth(aspect) {
  const [W, H] = S.layout.hoop;
  return Math.round(Math.min(W * 0.9, (H * 0.9) / aspect));
}

async function addPicture(file) {
  busy("now", "Unpicking the colors…");
  try {
    const info = await uploadPicture(file);
    S.images[info.image_id] = info;
    addElement({ type: "image", image_id: info.image_id, name: file.name.replace(/\.[^.]+$/, ""),
      width_mm: fitWidth(info.aspect), aspect: info.aspect, colors: info.colors, x: 0, y: 0, rotation: 0 });
    toast("Picture added. Untick any color you don't want stitched.");
  } catch (e) { toast(esc(e.message), "bad"); } finally { busy(false); }
}

// ---- refine / redo the last AI read: replaces only the pieces that read produced
function renderAiRefine() {
  const r = S.layout.ai_read, box = $("#aiRefine");
  box.hidden = !(r && r.raw && S.meta?.ai);
  if (!box.hidden) $("#aiRedo").textContent = r.mode === "exact" ? "Redo as creative" : "Redo as exact copy";
}
async function rereadPicture(opts) {
  const r = S.layout.ai_read;
  if (!r) return;
  busy("now", opts.refine ? "Refining your picture…" : "Reading your picture again…");
  try {
    const mode = opts.mode || r.mode;
    const res = await api("/api/ai/analyze", { image_id: r.image_id, hoop: S.layout.hoop, mode,
      refine: opts.refine || "", previous: opts.refine ? r.raw : null });
    beginEdit();
    const old = new Set(r.ids);
    const at = S.layout.elements.findIndex((e) => old.has(e.id));
    S.layout.elements = S.layout.elements.filter((e) => !old.has(e.id));
    res.elements.forEach((el) => (el.id = uid()));
    S.layout.elements.splice(at < 0 ? S.layout.elements.length : at, 0, ...res.elements);
    S.layout.ai_read = { image_id: r.image_id, mode, raw: res.raw, ids: res.elements.map((e) => e.id) };
    commit(true); S.autoSimplify = true;
    S.sel = -1; renderLayers(); renderProps(); renderAiRefine(); scheduleBuild(0); save_local();
    if (opts.refine) $("#aiRefineText").value = "";
    toast((opts.refine ? "Refined. " : "Read again. ") + "Undo goes back to the previous version." + (res.notes ? " " + esc(res.notes) : ""), "good");
  } catch (e) { toast(esc(e.message), "bad"); } finally { busy(false); }
}
$("#aiRefineGo").onclick = () => {
  const t = $("#aiRefineText").value.trim();
  if (!t) { toast("Type what you'd like changed first."); $("#aiRefineText").focus(); return; }
  rereadPicture({ refine: t });
};
$("#aiRedo").onclick = () => rereadPicture({ mode: S.layout.ai_read?.mode === "exact" ? "creative" : "exact" });
$("#aiRefineText").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#aiRefineGo").click(); });

async function readPicture(file) {
  if (!S.meta.ai) {
    toast("Add your Anthropic API key in Settings first — then I can read pictures.", "bad");
    openSettings(); return;
  }
  busy("now", "Reading your picture…");
  try {
    const info = await uploadPicture(file);
    S.images[info.image_id] = info;
    const mode = document.querySelector('input[name="aiMode"]:checked')?.value || "creative";
    const res = await api("/api/ai/analyze", { image_id: info.image_id, hoop: S.layout.hoop, mode });
    commit(true); beginEdit();
    for (const el of res.elements) { el.id = uid(); S.layout.elements.push(el); }
    // remember this read so it can be refined or redone later (kept with the project)
    S.layout.ai_read = { image_id: info.image_id, mode, raw: res.raw, ids: res.elements.map((e) => e.id) };
    commit(true); renderAiRefine();
    S.autoSimplify = true;
    S.sel = res.elements.length ? S.layout.elements.length - res.elements.length : S.sel;
    renderLayers(); renderProps(); scheduleBuild(0); save_local();
    toast(`Rebuilt ${res.elements.length} piece${res.elements.length === 1 ? "" : "s"} from your picture.` + (res.notes ? " " + esc(res.notes) : ""), "good");
  } catch (e) { toast(esc(e.message), "bad"); } finally { busy(false); }
}

function removeElement(i) {
  commit(true); beginEdit();
  S.layout.elements.splice(i, 1);
  S.sel = Math.min(S.sel, S.layout.elements.length - 1);
  if (S.sel === i) S.sel = -1;
  commit(true); renderLayers(); renderProps(); scheduleBuild(0); save_local();
}
function duplicate(i) {
  const c = JSON.parse(JSON.stringify(S.layout.elements[i]));
  c.x += 4; c.y += 4; delete c.id;
  addElement(c);
}
function moveLayer(i, d) {
  const j = i + d, els = S.layout.elements;
  if (j < 0 || j >= els.length) return;
  commit(true); beginEdit();
  [els[i], els[j]] = [els[j], els[i]];
  if (S.sel === i) S.sel = j; else if (S.sel === j) S.sel = i;
  commit(true); renderLayers(); scheduleBuild(0); save_local();
}
function elLabel(el) {
  if (el.type === "text") return el.text.replace(/\n/g, " ") || "(empty)";
  if (el.type === "shape") return (SHAPES.find((s) => s[0] === el.kind) || [0, "Shape"])[1];
  if (el.type === "vector") return el.name || "Drawing";
  return el.name || "Picture";
}
function elColor(el) {
  if (el.type === "image") { const k = (el.colors || []).find((c) => c.keep); return k ? k.thread : "#999"; }
  if (el.type === "vector") return (el.parts || [])[0]?.color || "#999";
  return el.color;
}

// ------------------------------------------------------------------ layers panel
const ICON = {
  up: '<svg viewBox="0 0 24 24"><path d="m6 15 6-6 6 6"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>',
  eye: '<svg viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  eyeoff: '<svg viewBox="0 0 24 24"><path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 3.9M6.6 6.6A17 17 0 0 0 2 12s3.6 7 10 7a9.7 9.7 0 0 0 4.4-1"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>',
};
function renderLayers() {
  if (S.meta) renderAiRefine();
  const ol = $("#layerList");
  ol.innerHTML = "";
  $("#layersEmpty").hidden = S.layout.elements.length > 0;
  S.layout.elements.forEach((el, i) => {
    const li = document.createElement("li");
    li.className = "layer" + (i === S.sel ? " sel" : "") + (el.hidden ? " hidden" : "");
    li.innerHTML = `<span class="dot" style="background:${esc(elColor(el))}"></span>
      <span class="name">${esc(elLabel(el))}</span><span class="kind">${el.type === "image" ? "pic" : el.type === "vector" ? "drawing" : el.type}</span>
      <span class="icons">
        <button data-a="up" title="Sew earlier" aria-label="Move up">${ICON.up}</button>
        <button data-a="down" title="Sew later" aria-label="Move down">${ICON.down}</button>
        <button data-a="hide" title="${el.hidden ? "Show" : "Hide"}" aria-label="Toggle visibility">${el.hidden ? ICON.eyeoff : ICON.eye}</button>
        <button data-a="dup" title="Duplicate (Ctrl+D)" aria-label="Duplicate">${ICON.copy}</button>
        <button data-a="del" title="Remove (Delete)" aria-label="Remove">${ICON.trash}</button>
      </span>`;
    li.onclick = (e) => {
      const a = e.target.closest("button")?.dataset.a;
      if (a === "up") moveLayer(i, -1);
      else if (a === "down") moveLayer(i, 1);
      else if (a === "del") removeElement(i);
      else if (a === "dup") duplicate(i);
      else if (a === "hide") { commit(true); beginEdit(); el.hidden = !el.hidden; commit(true); renderLayers(); scheduleBuild(0); save_local(); }
      else { endSew(); S.sel = i; renderLayers(); renderProps(); draw(); }
    };
    ol.appendChild(li);
  });
}

// ------------------------------------------------------------------ properties panel
function field(label, inner, value = "") {
  return `<div class="field"><div class="lbl"><span>${label}</span>${value !== "" ? `<output>${value}</output>` : ""}</div>${inner}</div>`;
}
function rangeField(id, label, min, max, step, val, unit = "") {
  return field(label, `<div class="row"><input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${val}">
    <input type="number" id="${id}-n" min="${min}" max="${max}" step="${step}" value="${val}" style="max-width:74px"></div>`) +
    (unit ? "" : "");
}
function threadField(id, label, color, open = false) {
  const sw = (S.meta?.threads || []).map((t) =>
    `<button type="button" class="thread-sw${t.hex.toLowerCase() === (color || "").toLowerCase() ? " on" : ""}" data-hex="${t.hex}" title="${esc(t.name)}" style="background:${t.hex}"></button>`).join("");
  return field(label, `<div class="color-row"><button type="button" class="thread-pick" data-toggle="${id}" aria-expanded="${open}" title="Choose a thread">
      <span class="dot" id="${id}-dot" style="background:${color}"></span><span class="cname" id="${id}-name">${esc(threadName(color))}</span><span class="caret">▾</span></button>
      <input type="color" id="${id}" value="${color}" title="Any color"></div>
    <div class="threads" data-for="${id}"${open ? "" : " hidden"}>${sw}</div>`);
}
// collapsible panel sections: all closed when you pick an item, then they stay as you leave them
let secOpen = {}, secFor = null;
function section(key, title, summary, inner) {
  const id = S.layout.elements[S.sel]?.id;
  if (id !== secFor) { secOpen = {}; secFor = id; }
  const open = !!secOpen[key];
  return `<details class="sec" data-sec="${key}"${open ? " open" : ""}><summary><span class="t">${title}</span><span class="s">${summary || ""}</span></summary><div class="sec-body">${inner}</div></details>`;
}
function threadName(hex) {
  const ts = S.meta?.threads || [];
  if (!ts.length || !hex) return "";
  const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const c = rgb(hex);
  let best = ts[0], bd = 1e9;
  for (const t of ts) { const d = rgb(t.hex).reduce((s, v, i) => s + (v - c[i]) ** 2, 0); if (d < bd) { bd = d; best = t; } }
  return (bd < 30 ? "" : "≈ ") + best.name;
}
function selectField(id, label, opts, val) {
  return field(label, `<select id="${id}">${opts.map(([v, t]) => `<option value="${v}"${v === val ? " selected" : ""}>${t}</option>`).join("")}</select>`);
}
function posFields(el) {
  return `<div class="two">${field("Across (mm)", `<input type="number" id="p-x" step="0.5" value="${el.x}">`)}${field("Down (mm)", `<input type="number" id="p-y" step="0.5" value="${el.y}">`)}</div>
    ${field("Place in the hoop", `<div class="align-grid">
      <button class="chip" data-place="left" title="Line up with the left edge of the sewing field">Left</button>
      <button class="chip" data-place="cx" title="Center across">Center</button>
      <button class="chip" data-place="right" title="Line up with the right edge">Right</button>
      <button class="chip" data-place="top" title="Line up with the top edge">Top</button>
      <button class="chip" data-place="cy" title="Center up and down">Middle</button>
      <button class="chip" data-place="bottom" title="Line up with the bottom edge">Bottom</button></div>`)}` +
    rangeField("p-rotation", "Turn (degrees)", -180, 180, 1, el.rotation || 0);
}
// ---- per-letter colors: click letters to pick them, then choose a thread
let letterSel = new Set(), letterSelFor = null;
function letterChips(el) {
  const lc = el.letter_colors || [];
  return [...el.text].map((ch, i) => ch === "\n" ? `<span class="letter-br"></span>` : !ch.trim() ? `<span class="letter-gap"></span>` :
    `<button type="button" class="letter-chip${letterSel.has(i) ? " on" : ""}" data-li="${i}" style="color:${lc[i] || el.color}" title="Letter ${i + 1}">${esc(ch)}</button>`).join("");
}
function letterColorFields(el) {
  if (letterSelFor !== el.id) { letterSel = new Set(); letterSelFor = el.id; }
  const any = (el.letter_colors || []).some(Boolean);
  return `<div class="field letters-card"><div class="lbl"><span>Color single letters</span></div>
    <div class="letters" id="p-letters">${letterChips(el)}</div>
    <p class="hint" id="p-lc-hint">${letterSel.size ? letterSel.size + " picked - choose a thread:" : "Click letters to pick them (click again to unpick)."}</p>
    <div id="p-lc-box"${letterSel.size ? "" : " hidden"}>${threadField("p-lc", "Thread for picked letters", el.color, true).replace('<div class="field">', '<div class="field" style="margin:0">')}</div>
    <div class="row" style="gap:6px"><button type="button" class="btn small" id="p-lc-all">Pick all</button>
    <button type="button" class="btn small" id="p-lc-reset"${any ? "" : " disabled"}>Back to one color</button></div></div>`;
}
function wireLetterColors(el) {
  const host = $("#p-letters");
  if (!host) return;
  const refresh = () => {
    host.innerHTML = letterChips(el);
    $("#p-lc-box").hidden = !letterSel.size;
    $("#p-lc-hint").textContent = letterSel.size ? letterSel.size + " picked - choose a thread:" : "Click letters to pick them (click again to unpick).";
    $("#p-lc-reset").disabled = !(el.letter_colors || []).some(Boolean);
  };
  host.onclick = (e) => {
    const b = e.target.closest("[data-li]");
    if (!b) return;
    const i = +b.dataset.li;
    letterSel.has(i) ? letterSel.delete(i) : letterSel.add(i);
    refresh();
  };
  $("#p-lc-all").onclick = () => { [...el.text].forEach((ch, i) => { if (ch.trim()) letterSel.add(i); }); refresh(); };
  $("#p-lc-reset").onclick = () => { setProp(el, "letter_colors", null); letterSel.clear(); refresh(); };
  wireThread("p-lc", () => el.color, (hex) => {
    if (!letterSel.size) return;
    const lc = [...el.text].map((_, i) => (el.letter_colors || [])[i] || null);
    letterSel.forEach((i) => { lc[i] = hex.toLowerCase() === el.color.toLowerCase() ? null : hex; });
    setProp(el, "letter_colors", lc.some(Boolean) ? lc : null);
    letterSel.clear(); // done with these letters: the next pick starts fresh
    refresh();
  });
  el._refreshLetters = refresh;
}

function outlineFields(el) {
  const ol = el.outline || null;
  return `<div class="field border-card"><label class="radio"><input type="checkbox" id="p-ol"${ol ? " checked" : ""}> <b>Satin border</b></label><span class="muted small">An outline in a second thread</span></div>
    <div id="p-ol-box"${ol ? "" : " hidden"}>${selectField("p-olfirst", "Sew it", [["first", "First, under the letters (nothing to snip)"], ["last", "Last, on top (snip jump threads)"]], ol && !ol.first ? "last" : "first")}${rangeField("p-olw", "Border width (mm)", 0.6, 3, 0.1, ol ? ol.width_mm : 1)}${threadField("p-olc", "Border thread", ol ? ol.color : "#3a2c22")}</div>`;
}

function renderProps() {
  const box = $("#props");
  const el = S.layout.elements[S.sel];
  if (!el) {
    const [W, H] = S.layout.hoop;
    box.innerHTML = `<h2>Your hoop</h2><div class="props-empty">
      <p class="hand">Start with some words, a shape, or a picture.</p>
      <p>Sewing field: <b>${W} × ${H} mm</b>. Fabric: <b>${esc(S.meta?.fabrics?.[S.layout.fabric] || "")}</b>.</p>
      <p class="muted small">Tips: drag things in the hoop to place them; drag the corner dot to resize and the top dot to turn.
      Letters look best at <b>6 mm or taller</b>. Click <b>Sew it out</b> to watch the stitch order.</p>
      <button class="btn" id="p-settings">Hoop &amp; fabric settings</button>
      <p class="hand" style="margin-top:18px">…or start from an example</p>
      <div class="chips">${Object.entries(EXAMPLES).map(([k, v]) => `<button class="chip" data-ex="${k}">${esc(v.name)}</button>`).join("")}</div></div>`;
    $("#p-settings").onclick = openSettings;
    $$("[data-ex]").forEach((b) => (b.onclick = () => loadExample(b.dataset.ex)));
    return;
  }
  let h = "";
  if (el.type === "text") {
    const multi = el.text.includes("\n");
    h += `<h2>Words</h2>` + field("Text", `<textarea id="p-text" rows="2" placeholder="Type here - Enter for a second line">${esc(el.text)}</textarea>`) +
      (String(el.font || "").startsWith("✦") ? "" : `<div class="swap-tip" id="p-swap" hidden></div>`) +
      section("size", "Font, size &amp; shape", `${esc(String(el.font || "").replace("✦ ", ""))} · ${el.height_mm} mm${el.arc ? " · curved" : ""}`,
        field("Font", `<div class="fontpick" id="fontpick"></div>`) +
        rangeField("p-height_mm", "Letter height (capitals, mm)", 3, 80, 0.5, el.height_mm) +
        rangeField("p-letter_spacing", "Letter spacing", -0.1, 0.5, 0.01, el.letter_spacing || 0) +
        rangeField("p-arc", "Curve (− smile / + arch)", -180, 180, 1, el.arc || 0) +
        (multi ? rangeField("p-line_spacing", "Line spacing", 0.8, 2, 0.05, el.line_spacing || 1.25) +
          field("Align lines", `<div class="seg3">${[["left", "Left"], ["center", "Center"], ["right", "Right"]].map(([v, t]) =>
            `<button class="chip${(el.align || "center") === v ? " on" : ""}" data-align="${v}">${t}</button>`).join("")}</div>`) : "")) +
      section("color", "Thread &amp; colors", `<span class="dot" style="background:${el.color}"></span>${esc(threadName(el.color))}${(el.letter_colors || []).some(Boolean) ? " + letters" : ""}`,
        threadField("p-color", "Thread", el.color) + letterColorFields(el)) +
      section("border", "Border", el.outline ? `${el.outline.width_mm} mm · sewn ${el.outline.first ? "first" : "last"}` : "none", outlineFields(el)) +
      section("pos", "Position", `${el.x}, ${el.y} mm${el.rotation ? ` · ${el.rotation}°` : ""}`, posFields(el));
  } else if (el.type === "shape") {
    h += `<h2>Shape</h2>` + field("Kind", `<div class="chips">${SHAPES.map(([k, t]) => `<button class="chip${k === el.kind ? " on" : ""}" data-kind="${k}">${t}</button>`).join("")}</div>`) +
      `<div class="two">${field("Width (mm)", `<input type="number" id="p-width_mm" min="2" max="360" step="0.5" value="${el.width_mm}">`)}${field("Height (mm)", `<input type="number" id="p-height_mm" min="2" max="360" step="0.5" value="${el.height_mm}">`)}</div>` +
      (["frame", "double_frame", "offset_frame", "ring", "line"].includes(el.kind) ? rangeField("p-stroke_mm", "Line thickness (mm)", 0.6, 8, 0.1, el.stroke_mm || 1.2) : "") +
      section("color", "Thread &amp; stitch", `<span class="dot" style="background:${el.color}"></span>${esc(threadName(el.color))}`,
        threadField("p-color", "Thread", el.color) + selectField("p-style", "Stitch", STYLES, el.style || "auto")) +
      section("border", "Border", el.outline ? `${el.outline.width_mm} mm` : "none", outlineFields(el)) +
      section("pos", "Position", `${el.x}, ${el.y} mm${el.rotation ? ` · ${el.rotation}°` : ""}`, posFields(el));
  } else if (el.type === "vector") {
    const cols = [...new Set((el.parts || []).map((p) => p.color.toLowerCase()))];
    h += `<h2>Drawing</h2>` + rangeField("p-width_mm", "Width (mm)", 5, Math.max(...S.layout.hoop), 0.5, el.width_mm) +
      `<p class="hint">Redrawn from your picture as clean shapes (${(el.parts || []).length} parts), so it stitches smoothly.</p>` +
      `<button type="button" class="btn small" id="p-split" title="Make each object (e.g. the lights, the mountains) its own layer">Split into pieces</button>` +
      field("Threads", `<div class="vcolors">${cols.map((c) => `<label class="vcolor"><input type="color" data-vc="${c}" value="${c}"><span>${esc(threadName(c))}</span></label>`).join("")}</div>`) +
      section("pos", "Position", `${el.x}, ${el.y} mm${el.rotation ? ` · ${el.rotation}°` : ""}`, posFields(el));
  } else {
    const cols = el.colors || [];
    h += `<h2>Picture</h2>` + rangeField("p-width_mm", "Width (mm)", 10, Math.max(...S.layout.hoop), 0.5, el.width_mm) +
      `<p class="hint">About ${Math.round(el.width_mm * (el.aspect || 1))} mm tall.</p>` +
      field("Colors <small>(untick to skip)</small>", `<div class="imgcolors">${cols.map((c, i) => `
        <div class="imgcolor"><span class="sw" style="background:${c.hex}" title="In the picture"></span>
          <label class="radio"><input type="checkbox" data-ci="${i}" data-k="keep"${c.keep ? " checked" : ""}> ${Math.round(c.share * 100)}%</label>
          <input type="color" data-ci="${i}" data-k="thread" value="${c.thread}" title="Thread color">
          <select data-ci="${i}" data-k="style">${STYLES.map(([v, t]) => `<option value="${v}"${v === (c.style || "auto") ? " selected" : ""}>${t.split(" —")[0].replace(" (recommended)", "")}</option>`).join("")}</select>
        </div>`).join("")}</div>`) +
      `<p class="hint">Flat artwork (logos, clip art, lettering) stitches best. Photos won't.</p>` +
      section("pos", "Position", `${el.x}, ${el.y} mm${el.rotation ? ` · ${el.rotation}°` : ""}`, posFields(el));
  }
  box.innerHTML = h;
  wireProps(el);
}

function setProp(el, key, val, rebuild = true) {
  beginEdit();
  el[key] = val;
  commit();
  if (rebuild) scheduleBuild(); else draw();
  save_local();
  if (["text", "color"].includes(key)) renderLayers();
}

function wireRange(el, key, rebuild = true, target = el) {
  const r = $("#p-" + key), n = $("#p-" + key + "-n");
  if (!r || !n) return; // plain number inputs (shape width/height) are wired separately
  const set = (v) => { v = parseFloat(v); if (isNaN(v)) return; r.value = v; n.value = v; setProp(target, key, v, rebuild); };
  r.oninput = () => set(r.value);
  n.onchange = () => set(n.value);
}

function wireThread(id, get, set) {
  const inp = $("#" + id);
  if (!inp) return;
  const apply = (hex) => { inp.value = hex; $("#" + id + "-name").textContent = threadName(hex); const d = $("#" + id + "-dot"); if (d) d.style.background = hex; set(hex);
    $$(`.threads[data-for="${id}"] .thread-sw`).forEach((b) => b.classList.toggle("on", b.dataset.hex.toLowerCase() === hex.toLowerCase())); };
  inp.oninput = () => apply(inp.value);
  $$(`.threads[data-for="${id}"] .thread-sw`).forEach((b) => (b.onclick = () => apply(b.dataset.hex)));
}

function wireProps(el) {
  // TrueType text: offer the matching hand-digitized font (they stitch far cleaner)
  const tip = $("#p-swap");
  if (tip && el.type === "text") {
    api(`/api/stand-in?font=${encodeURIComponent(el.font)}&h=${el.height_mm}`).then((r) => {
      if (!r.font || S.layout.elements[S.sel] !== el || !$("#p-swap")) return;
      tip.innerHTML = `<span>This font is auto-traced and can look rough when sewn. <b>${esc(r.font.replace("✦ ", ""))}</b> is hand-digitized for this size.</span>
        <button type="button" class="btn small" id="p-swap-go">Use it</button>`;
      tip.hidden = false;
      $("#p-swap-go").onclick = () => { setProp(el, "font", r.font); renderProps(); };
    }).catch(() => {});
  }
  $$("details.sec").forEach((d) => (d.ontoggle = () => { secOpen[d.dataset.sec] = d.open; }));
  $$("[data-toggle]").forEach((b) => (b.onclick = () => {
    const g = $(`.threads[data-for="${b.dataset.toggle}"]`);
    g.hidden = !g.hidden; b.setAttribute("aria-expanded", String(!g.hidden));
  }));
  const t = $("#p-text");
  if (t) t.oninput = () => {
    const had = el.text.includes("\n"), old = el.text, nw = t.value;
    if (el.letter_colors) { // shift colors with the edit: keep the common start and end
      let a = 0; while (a < old.length && a < nw.length && old[a] === nw[a]) a++;
      let b = 0; while (b < old.length - a && b < nw.length - a && old[old.length - 1 - b] === nw[nw.length - 1 - b]) b++;
      const lc = el.letter_colors;
      el.letter_colors = [...lc.slice(0, a), ...Array(nw.length - a - b).fill(null), ...lc.slice(old.length - b, old.length)];
      letterSel.clear();
    }
    setProp(el, "text", nw);
    if ($("#p-letters")) $("#p-letters").innerHTML = letterChips(el); if (had !== t.value.includes("\n")) { renderProps(); $("#p-text").focus(); } };
  ["height_mm", "letter_spacing", "arc", "line_spacing", "stroke_mm"].forEach((k) => wireRange(el, k));
  wireRange(el, "rotation", false);
  if (el.type === "image" || el.type === "vector") wireRange(el, "width_mm");
  const split = $("#p-split");
  if (split) split.onclick = async () => {
    try {
      const res = await api("/api/split-drawing", el);
      if (res.elements.length < 2) { toast("This drawing is already a single piece in one color."); return; }
      beginEdit();
      const i = S.layout.elements.indexOf(el);
      res.elements.forEach((e) => (e.id = uid()));
      S.layout.elements.splice(i, 1, ...res.elements);
      // a split drawing that came from a picture read: keep refine working on its pieces
      const r = S.layout.ai_read;
      if (r && r.ids.includes(el.id)) r.ids = r.ids.filter((x) => x !== el.id).concat(res.elements.map((e) => e.id));
      commit(true);
      S.sel = i; renderLayers(); renderProps(); scheduleBuild(0); save_local();
      toast(`Split into ${res.elements.length} pieces - each is its own layer now.`, "good");
    } catch (e) { toast(esc(e.message), "bad"); }
  };
  $$("[data-vc]").forEach((inp) => (inp.oninput = () => {
    const old = inp.dataset.vc;
    beginEdit(); (el.parts || []).forEach((p) => { if (p.color.toLowerCase() === old) p.color = inp.value; }); commit();
    inp.dataset.vc = inp.value.toLowerCase(); scheduleBuild(); save_local(); renderLayers();
  }));
  ["width_mm", "height_mm"].forEach((k) => { const i = $("#p-" + k); if (i && i.type === "number" && el.type === "shape") i.onchange = () => setProp(el, k, clamp(parseFloat(i.value) || 10, 2, 360)); });
  const st = $("#p-style"); if (st) st.onchange = () => setProp(el, "style", st.value);
  const al = $("#p-align"); if (al) al.onchange = () => setProp(el, "align", al.value);
  wireThread("p-color", () => el.color, (hex) => { setProp(el, "color", hex); if ($("#p-letters")) $("#p-letters").innerHTML = letterChips(el); });
  wireLetterColors(el);
  const ol = $("#p-ol");
  if (ol) {
    ol.onchange = () => {
      beginEdit(); el.outline = ol.checked ? { color: $("#p-olc").value || "#3a2c22", width_mm: parseFloat($("#p-olw").value) || 1, first: $("#p-olfirst").value === "first" } : null; commit();
      $("#p-ol-box").hidden = !ol.checked; scheduleBuild(); save_local();
    };
    $("#p-olfirst").onchange = () => { if (el.outline) { beginEdit(); el.outline.first = $("#p-olfirst").value === "first"; commit(); scheduleBuild(); save_local(); } };
    const w = $("#p-olw"), wn = $("#p-olw-n");
    const setw = (v) => { w.value = v; wn.value = v; if (el.outline) { beginEdit(); el.outline.width_mm = parseFloat(v); commit(); scheduleBuild(); save_local(); } };
    w.oninput = () => setw(w.value); wn.onchange = () => setw(wn.value);
    wireThread("p-olc", () => el.outline?.color, (hex) => { if (el.outline) { beginEdit(); el.outline.color = hex; commit(); scheduleBuild(); save_local(); } });
  }
  $$(".chip[data-kind]").forEach((b) => (b.onclick = () => { setProp(el, "kind", b.dataset.kind); renderProps(); renderLayers(); }));
  $$("[data-ci]").forEach((inp) => {
    const i = +inp.dataset.ci, k = inp.dataset.k;
    const ev = inp.type === "checkbox" || inp.tagName === "SELECT" ? "onchange" : "oninput";
    inp[ev] = () => { beginEdit(); el.colors[i][k] = inp.type === "checkbox" ? inp.checked : inp.value; commit(); scheduleBuild(); save_local(); renderLayers(); };
  });
  const px = $("#p-x"), py = $("#p-y");
  if (px) { px.onchange = () => setProp(el, "x", parseFloat(px.value) || 0, false); py.onchange = () => setProp(el, "y", parseFloat(py.value) || 0, false); }
  $$("[data-align]").forEach((b) => (b.onclick = () => { setProp(el, "align", b.dataset.align); $$("[data-align]").forEach((c) => c.classList.toggle("on", c === b)); }));
  $$("[data-place]").forEach((b) => (b.onclick = () => {
    const [W, H] = S.layout.hoop, bx = S.built?.elements?.[S.sel] || { w: 0, h: 0 };
    // extent of the (possibly turned) box
    const a = ((el.rotation || 0) * Math.PI) / 180, c = Math.abs(Math.cos(a)), sn = Math.abs(Math.sin(a));
    const hw = (bx.w * c + bx.h * sn) / 2, hh = (bx.w * sn + bx.h * c) / 2, r1 = (v) => Math.round(v * 10) / 10;
    const k = b.dataset.place;
    if (k === "cx") setProp(el, "x", 0, false);
    if (k === "cy") setProp(el, "y", 0, false);
    if (k === "left") setProp(el, "x", r1(-W / 2 + hw), false);
    if (k === "right") setProp(el, "x", r1(W / 2 - hw), false);
    if (k === "top") setProp(el, "y", r1(-H / 2 + hh), false);
    if (k === "bottom") setProp(el, "y", r1(H / 2 - hh), false);
    px.value = el.x; py.value = el.y; scheduleBuild();
  }));
  if (el.type === "text") fontPicker($("#fontpick"), el.font, (f) => { setProp(el, "font", f); });
}

// ------------------------------------------------------------------ font picker
const CATS = ["All", "Pro digitized", "Block", "Script", "Serif", "Varsity", "Blackletter", "Rounded", "Display", "Handwritten", "Western"];
function fontPicker(host, current, onPick) {
  const prev = (n) => `/api/font-preview/${encodeURIComponent(n)}`;
  host.innerHTML = `<button type="button" class="btn fontpick-btn"><img alt="${esc(current)}" src="${prev(current)}"><span>▾</span></button>`;
  const btn = host.firstElementChild;
  let cat = "All";
  btn.onclick = (e) => {
    e.stopPropagation();
    if (host.querySelector(".fontpick-pop")) { host.querySelector(".fontpick-pop").remove(); return; }
    const pop = document.createElement("div");
    pop.className = "fontpick-pop";
    pop.innerHTML = `<input placeholder="Search ${S.fonts.length} fonts…" aria-label="Search fonts">
      <div class="fontpick-cats">${CATS.map((c) => `<button type="button" class="chip${c === cat ? " on" : ""}" data-c="${c}">${c}</button>`).join("")}</div>
      <div class="fontpick-list"></div>`;
    host.appendChild(pop);
    const list = pop.querySelector(".fontpick-list"), q = pop.querySelector("input");
    const fill = () => {
      const term = q.value.toLowerCase();
      list.innerHTML = S.fonts.filter((f) => (cat === "All" || f.category === cat) && f.name.toLowerCase().includes(term))
        .map((f) => `<div class="fontpick-item${f.name === current ? " on" : ""}" data-f="${esc(f.name)}"><img loading="lazy" alt="${esc(f.name)}" src="${prev(f.name)}"><span>${f.category}</span></div>`).join("")
        || `<p class="muted" style="padding:10px">No fonts match.</p>`;
    };
    fill(); q.focus();
    q.oninput = fill;
    pop.onclick = (ev) => {
      ev.stopPropagation();
      const c = ev.target.closest("[data-c]");
      if (c) { cat = c.dataset.c; $$(".fontpick-cats .chip", pop).forEach((b) => b.classList.toggle("on", b.dataset.c === cat)); fill(); return; }
      const it = ev.target.closest("[data-f]");
      if (it) { current = it.dataset.f; btn.querySelector("img").src = prev(current); pop.remove(); onPick(current); }
    };
    const close = (ev) => { if (!host.contains(ev.target)) { pop.remove(); document.removeEventListener("click", close); } };
    setTimeout(() => document.addEventListener("click", close), 0);
  };
}

// ------------------------------------------------------------------ thread chart
function spoolSVG(hex) {
  return `<svg viewBox="0 0 26 34" aria-hidden="true" style="flex:none"><rect x="2" y="1" width="22" height="4" rx="1.5" fill="#b08a5c"/>
    <rect x="2" y="29" width="22" height="4" rx="1.5" fill="#b08a5c"/><rect x="5" y="5" width="16" height="24" fill="${hex}"/>
    <g stroke="rgba(0,0,0,.18)" stroke-width="1">${[8, 11, 14, 17, 20, 23, 26].map((y) => `<path d="M5 ${y}h16"/>`).join("")}</g>
    <rect x="5" y="5" width="4" height="24" fill="rgba(255,255,255,.22)"/></svg>`;
}
// ---- thread merging: fewer spools = fewer thread changes on the machine
let threadSel = new Set();
function hexToLab(hex) {
  const lin = (v) => { v /= 255; return v > 0.04045 ? ((v + 0.055) / 1.055) ** 2.4 : v / 12.92; };
  const [r, g, b] = [1, 3, 5].map((i) => lin(parseInt(hex.slice(i, i + 2), 16)));
  const f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  const x = f((r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047), y = f(r * 0.2126 + g * 0.7152 + b * 0.0722), z = f((r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883);
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
}
const deltaE = (a, b) => { const p = hexToLab(a), q = hexToLab(b); return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]); };
function recolorAll(map) {
  // map: {from hex (lower case): to hex}. Every place a thread colour lives in the layout.
  const f = (h) => (h && map[h.toLowerCase()]) || h;
  for (const el of S.layout.elements) {
    if (el.color) el.color = f(el.color);
    if (el.outline) el.outline.color = f(el.outline.color);
    if (el.letter_colors) el.letter_colors = el.letter_colors.map((c) => (c ? f(c) : c));
    (el.parts || []).forEach((p) => (p.color = f(p.color)));
    (el.colors || []).forEach((c) => (c.thread = f(c.thread)));
  }
}
function applyMerge(map, note) {
  if (!Object.keys(map).length) { toast("Nothing to merge - the threads are already all different enough."); return; }
  const before = S.built?.stats?.colors?.length || 0;
  beginEdit(); recolorAll(map); commit(true);
  threadSel.clear(); save_local(); renderLayers(); renderProps(); scheduleBuild(0);
  toast(note || `Merged ${Object.keys(map).length} thread${Object.keys(map).length > 1 ? "s" : ""}. Undo puts them back.`);
}
function simplifyThreads(auto = false) {
  // most-used colour wins; anything within ~20 ΔE of a kept colour (same spool to the eye:
  // white vs cream, gold vs amber) joins it
  const cols = [...(S.built?.stats?.colors || [])].sort((a, b) => b.stitches - a.stitches);
  const kept = [], map = {};
  for (const c of cols) {
    const hex = c.hex.toLowerCase();
    if (kept.includes(hex) || map[hex]) continue;
    const near = kept.find((k) => deltaE(k, hex) < 20);
    if (near) map[hex] = near; else kept.push(hex);
  }
  const n = new Set(cols.map((c) => c.hex.toLowerCase())).size;
  if (auto && !Object.keys(map).length) return;
  applyMerge(map, `${auto ? "Merged look-alike threads" : "Simplified"}: ${n} → ${kept.length} threads. Undo puts them back.`);
}
function renderSpoolTools() {
  const host = $("#spoolTools");
  const n = S.built?.stats?.colors?.length || 0;
  if (threadSel.size >= 2) {
    host.innerHTML = `<span>Merge ${threadSel.size} into:</span>${[...threadSel].map((h) =>
      `<button type="button" class="merge-to" data-to="${h}" title="Use ${esc(threadName(h))} for all of them"><span class="dot" style="background:${h}"></span>${esc(threadName(h))}</button>`).join("")}
      <button type="button" class="btn small ghost" id="mergeCancel">Cancel</button>`;
    $$(".merge-to", host).forEach((b) => (b.onclick = () => {
      const to = b.dataset.to, map = {};
      threadSel.forEach((h) => { if (h !== to) map[h] = to; });
      applyMerge(map);
    }));
    $("#mergeCancel").onclick = () => { threadSel.clear(); renderChart(); draw(); };
  } else if (n >= 2) {
    host.innerHTML = `<button type="button" class="btn small" id="simplify" title="Merge threads that look the same">Simplify colors</button>
      <span class="muted">${threadSel.size ? "Showing where that thread sews · pick another spool to merge" : "or click spools to see and merge them"}</span>
      ${threadSel.size ? `<button type="button" class="btn small ghost" id="spoolClear">Show all</button>` : ""}`;
    $("#simplify").onclick = () => simplifyThreads();
    if ($("#spoolClear")) $("#spoolClear").onclick = () => { threadSel.clear(); renderChart(); draw(); };
  } else host.innerHTML = "";
}

function renderChart() {
  const st = S.built?.stats;
  if (!st) return;
  const fmt = (n) => n.toLocaleString();
  $("#stats").innerHTML = `<span>Stitches</span><b>${fmt(st.stitches)}</b><span>Size</span><b>${st.size[0]} × ${st.size[1]} mm</b>
    <span>Sewing time</span><b>≈ ${Math.max(1, Math.round(st.minutes))} min</b>
    <span>Thread</span><b title="Top thread; bobbin ≈ ${st.bobbin_m} m">≈ ${st.thread_m} m <small class="muted">+ ${st.bobbin_m} m bobbin</small></b><span>Thread changes</span><b>${Math.max(0, st.colors.length - 1)}</b>`;
  $("#spools").innerHTML = st.colors.map((c, i) => `<div class="spool${threadSel.has(c.hex.toLowerCase()) ? " on" : ""}" data-hex="${c.hex.toLowerCase()}" title="Click to pick for merging · ${esc(c.kinds.join(", "))}">${spoolSVG(c.hex)}
    <div class="t"><b>${i + 1}. ${esc(c.thread)}</b><span class="n">${fmt(c.stitches)} stitches · ≈ ${c.thread_m} m</span></div></div>`).join("") ||
    `<span class="muted" style="font-family:Hand,cursive;font-size:18px">Thread chart appears here.</span>`;
  const live = new Set(st.colors.map((c) => c.hex.toLowerCase()));
  threadSel = new Set([...threadSel].filter((h) => live.has(h)));
  $$("#spools .spool").forEach((sp) => (sp.onclick = () => {
    const h = sp.dataset.hex;
    threadSel.has(h) ? threadSel.delete(h) : threadSel.add(h);
    sp.classList.toggle("on", threadSel.has(h));
    renderSpoolTools(); draw();
  }));
  renderSpoolTools();
  $("#notes").innerHTML = (st.warnings || []).slice(0, 3).map((w) => `<div class="note">${esc(w)}</div>`).join("");
}

// ------------------------------------------------------------------ canvas
const cv = $("#canvas"), ctx = cv.getContext("2d");
let view = { s: 5, ox: 0, oy: 0 }; // px per mm, hoop-centre in px
let weave = null;

function makeWeave(color) {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const g = c.getContext("2d");
  g.fillStyle = color; g.fillRect(0, 0, 64, 64);
  const lum = parseInt(color.slice(1, 3), 16) + parseInt(color.slice(3, 5), 16) + parseInt(color.slice(5, 7), 16);
  const ink = lum > 380 ? "0,0,0" : "255,255,255";
  for (let y = 0; y < 64; y += 2) { g.fillStyle = `rgba(${ink},${0.035 + ((y * 7) % 5) * 0.006})`; g.fillRect(0, y, 64, 1); }
  for (let x = 0; x < 64; x += 3) { g.fillStyle = `rgba(${ink},0.03)`; g.fillRect(x, 0, 1, 64); }
  for (let i = 0; i < 260; i++) { g.fillStyle = `rgba(${ink},${Math.random() * 0.05})`; g.fillRect(Math.random() * 64, Math.random() * 64, 1, 1); }
  return ctx.createPattern(c, "repeat");
}

function fitView() {
  const r = cv.getBoundingClientRect();
  const [W, H] = S.layout.hoop;
  const frame = 22; // mm of hoop around the field
  view.s = Math.min(r.width / (W + frame * 2), r.height / (H + frame * 2)) * S.zoom;
  view.ox = r.width / 2 + S.pan.x;
  view.oy = r.height / 2 + S.pan.y;
  $("#zoomLabel").textContent = Math.round(S.zoom * 100) + "%";
}
const mm2px = (x, y) => [view.ox + x * view.s, view.oy + y * view.s];
const px2mm = (x, y) => [(x - view.ox) / view.s, (y - view.oy) / view.s];

function resize() {
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

function roundRect(g, x, y, w, h, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}

function drawHoop() {
  const r = cv.getBoundingClientRect();
  ctx.clearRect(0, 0, r.width, r.height);
  const [W, H] = S.layout.hoop, s = view.s;
  const pad = 9, ring = 7; // mm: fabric beyond the field, wooden ring thickness
  const [x0, y0] = mm2px(-W / 2 - pad, -H / 2 - pad);
  const fw = (W + pad * 2) * s, fh = (H + pad * 2) * s, rr = 16 * s;
  // shadow + wooden ring
  ctx.save();
  ctx.shadowColor = "rgba(60,35,15,.35)"; ctx.shadowBlur = 22; ctx.shadowOffsetY = 8;
  roundRect(ctx, x0 - ring * s, y0 - ring * s, fw + ring * 2 * s, fh + ring * 2 * s, rr + ring * s);
  const wood = ctx.createLinearGradient(x0, y0 - ring * s, x0, y0 + fh + ring * s);
  wood.addColorStop(0, "#b98452"); wood.addColorStop(0.5, "#9a6437"); wood.addColorStop(1, "#7a4b28");
  ctx.fillStyle = wood; ctx.fill();
  ctx.restore();
  // wood grain
  ctx.save();
  roundRect(ctx, x0 - ring * s, y0 - ring * s, fw + ring * 2 * s, fh + ring * 2 * s, rr + ring * s); ctx.clip();
  ctx.strokeStyle = "rgba(60,30,10,.18)"; ctx.lineWidth = 1;
  for (let i = 0; i < 9; i++) {
    ctx.beginPath(); const yy = y0 - ring * s + (i + 0.5) * (fh + ring * 2 * s) / 9;
    ctx.moveTo(x0 - ring * s, yy); ctx.bezierCurveTo(x0 + fw * 0.3, yy - 6, x0 + fw * 0.7, yy + 6, x0 + fw + ring * s, yy); ctx.stroke();
  }
  ctx.restore();
  // fabric
  if (!weave || weave._c !== S.layout.fabric_color) { weave = makeWeave(S.layout.fabric_color || "#d9d5cc"); weave._c = S.layout.fabric_color; }
  roundRect(ctx, x0, y0, fw, fh, rr);
  ctx.fillStyle = weave; ctx.fill();
  ctx.save(); ctx.clip();
  const inner = ctx.createRadialGradient(view.ox, view.oy, Math.min(fw, fh) * 0.3, view.ox, view.oy, Math.max(fw, fh) * 0.75);
  inner.addColorStop(0, "rgba(0,0,0,0)"); inner.addColorStop(1, "rgba(40,20,5,.16)");
  ctx.fillStyle = inner; ctx.fillRect(x0, y0, fw, fh);
  ctx.restore();
  // brass clamp
  const [cx, cy] = mm2px(0, -H / 2 - pad - ring);
  const bw = 16 * s, bh = 5 * s;
  const brass = ctx.createLinearGradient(cx, cy - bh, cx, cy + bh);
  brass.addColorStop(0, "#e2c275"); brass.addColorStop(1, "#9c7a2c");
  ctx.fillStyle = brass; roundRect(ctx, cx - bw / 2, cy - bh * 0.8, bw, bh * 1.6, 2 * s); ctx.fill();
  ctx.fillStyle = "#7d6122"; ctx.beginPath(); ctx.arc(cx, cy, 1.6 * s, 0, 7); ctx.fill();
  // measuring grid (like a slicer's build plate)
  {
    const stepMm = view.s < 2.2 ? 20 : 10, g0 = px2mm(x0, y0), g1 = px2mm(x0 + fw, y0 + fh);
    const gx0 = g0[0], gy0 = g0[1], gx1 = g1[0], gy1 = g1[1];
    ctx.save();
    roundRect(ctx, x0, y0, fw, fh, rr); ctx.clip(); // stay on the fabric
    ctx.lineWidth = 1;
    const fc = S.layout.fabric_color || "#d9d5cc";
    const lum = parseInt(fc.slice(1, 3), 16) + parseInt(fc.slice(3, 5), 16) + parseInt(fc.slice(5, 7), 16);
    const ink = lum > 380 ? "60,40,25" : "255,255,255", al = lum > 380 ? [0.13, 0.22, 0.32] : [0.14, 0.25, 0.36];
    for (let m = Math.ceil(gx0 / stepMm) * stepMm; m <= gx1; m += stepMm) {
      const major = m === 0 ? 2 : m % 50 === 0 ? 1 : 0;
      ctx.strokeStyle = `rgba(${ink},${al[major]})`;
      const [px, py0] = mm2px(m, gy0), [, py1] = mm2px(m, gy1);
      ctx.beginPath(); ctx.moveTo(Math.round(px) + 0.5, py0); ctx.lineTo(Math.round(px) + 0.5, py1); ctx.stroke();
    }
    for (let m = Math.ceil(gy0 / stepMm) * stepMm; m <= gy1; m += stepMm) {
      const major = m === 0 ? 2 : m % 50 === 0 ? 1 : 0;
      ctx.strokeStyle = `rgba(${ink},${al[major]})`;
      const [px0, py] = mm2px(gx0, m), [px1] = mm2px(gx1, m);
      ctx.beginPath(); ctx.moveTo(px0, Math.round(py) + 0.5); ctx.lineTo(px1, Math.round(py) + 0.5); ctx.stroke();
    }
    ctx.restore();
  }
  // sewing field + tape ticks
  const [fx, fy] = mm2px(-W / 2, -H / 2);
  ctx.setLineDash([4, 4]); ctx.strokeStyle = "rgba(122,82,52,.55)"; ctx.lineWidth = 1;
  ctx.strokeRect(fx, fy, W * s, H * s); ctx.setLineDash([]);
  ctx.strokeStyle = "rgba(122,82,52,.45)";
  for (let m = 0; m <= W; m += 5) { const [tx] = mm2px(-W / 2 + m, 0); ctx.beginPath(); ctx.moveTo(tx, fy); ctx.lineTo(tx, fy - (m % 10 ? 3 : 7)); ctx.stroke(); }
  for (let m = 0; m <= H; m += 5) { const [, ty] = mm2px(0, -H / 2 + m); ctx.beginPath(); ctx.moveTo(fx, ty); ctx.lineTo(fx - (m % 10 ? 3 : 7), ty); ctx.stroke(); }
  // centre mark
  ctx.strokeStyle = "rgba(122,82,52,.35)";
  ctx.beginPath(); ctx.moveTo(view.ox - 6, view.oy); ctx.lineTo(view.ox + 6, view.oy); ctx.moveTo(view.ox, view.oy - 6); ctx.lineTo(view.ox, view.oy + 6); ctx.stroke();
}

function shade(hex, f) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return `rgb(${c.map((v) => Math.round(f < 1 ? v * f : v + (255 - v) * (f - 1))).join(",")})`;
}

function strokeObjects(g, objects, color, tf, s, alpha = 1) {
  // objects: flat [x0,y0,x1,y1,...] in element-local mm; tf maps to screen
  // real 40wt thread spreads to ~0.45 mm on fabric; a soft edge shadow + sheen line reads as satin
  const passes = [[shade(color, 0.72), 0.52], [color, 0.44], [shade(color, 1.35), 0.12]];
  g.globalAlpha = alpha;
  g.lineCap = "round"; g.lineJoin = "round";
  for (let p = 0; p < 3; p++) {
    const [col, w] = passes[p];
    g.strokeStyle = col; g.lineWidth = Math.max(p === 2 ? 0.5 : 0.8, w * s);
    const off = p === 2 ? -0.07 * s : 0;
    g.beginPath();
    for (const o of objects) {
      for (let i = 0; i < o.length; i += 2) {
        const [x, y] = tf(o[i], o[i + 1]);
        if (i === 0) g.moveTo(x + off, y + off); else g.lineTo(x + off, y + off);
      }
    }
    g.stroke();
  }
  g.globalAlpha = 1;
}

function elTransform(el) {
  const a = ((el.rotation || 0) * Math.PI) / 180, c = Math.cos(a), sn = Math.sin(a);
  return (x, y) => mm2px(el.x + x * c - y * sn, el.y + x * sn + y * c);
}

function drawArtwork(el, b) {
  const info = S.images[el.image_id];
  if (!info) return;
  if (!info._img) { info._img = new Image(); info._img.src = "/api/image/" + el.image_id; info._img.onload = draw; return; }
  if (!info._img.complete) return;
  const w = el.width_mm, h = el.width_mm * (el.aspect || info.aspect);
  ctx.save();
  const [cx, cy] = mm2px(el.x, el.y);
  ctx.translate(cx, cy); ctx.rotate(((el.rotation || 0) * Math.PI) / 180);
  ctx.globalAlpha = 0.3;
  ctx.drawImage(info._img, (-w / 2) * view.s, (-h / 2) * view.s, w * view.s, h * view.s);
  ctx.restore();
}

// transient visual scale per element while a resize is in flight (until the rebuild lands)
S.vis = {};
function visOf(el) { return S.vis[el.id] || { fx: 1, fy: 1 }; }

function selBox(el, i) {
  const b = S.built?.elements?.[i];
  if (!b) return null;
  const v = visOf(el);
  return { w: b.w * v.fx + 1.5, h: b.h * v.fy + 1.5 };
}

function elBlocksTransform(el) {
  // element-local stitches -> screen, including any in-flight visual scale
  const v = visOf(el), base = elTransform(el);
  return (x, y) => base(x * v.fx, y * v.fy);
}

const HANDLE_KEYS = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
const HANDLE_SIGN = { nw: [-1, -1], n: [0, -1], ne: [1, -1], e: [1, 0], se: [1, 1], s: [0, 1], sw: [-1, 1], w: [-1, 0] };
const HANDLE_CURSOR = { nw: "nwse-resize", se: "nwse-resize", ne: "nesw-resize", sw: "nesw-resize", n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize", rot: "grab" };

function handles(el, bx) {
  const tf = elTransform(el), out = {};
  for (const k of HANDLE_KEYS) { const [sx, sy] = HANDLE_SIGN[k]; out[k] = tf((sx * bx.w) / 2, (sy * bx.h) / 2); }
  out.rot = tf(0, -bx.h / 2 - 22 / view.s);
  return out;
}

function boxOutline(el, bx, color, dash, width) {
  const tf = elTransform(el);
  const pts = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([x, y]) => tf((x * bx.w) / 2, (y * bx.h) / 2));
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
  ctx.beginPath(); pts.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.closePath(); ctx.stroke();
  ctx.setLineDash([]);
}

let hoverIdx = -1;
function draw() {
  if (!cv.width) return;
  fitView();
  drawHoop();
  if (S.sew) { drawSew(); return; }
  const els = S.layout.elements, built = S.built?.elements || [];
  els.forEach((el, i) => {
    if (el.hidden) return;
    if (el.type === "image" && $("#showArt").checked) drawArtwork(el, built[i]);
    const b = built[i];
    if (!b) return;
    const tf = elBlocksTransform(el);
    for (const blk of b.blocks) strokeObjects(ctx, blk.objects, blk.color, tf, view.s,
      threadSel.size && !threadSel.has(blk.color.toLowerCase()) ? 0.16 : 1);
  });
  if ($("#showJumps").checked && S.built) drawJumps();
  if (!els.length) {
    ctx.fillStyle = "rgba(122,82,52,.55)"; ctx.font = `${Math.max(18, view.s * 5)}px Hand, cursive`; ctx.textAlign = "center";
    ctx.fillText("Your hoop is empty — add some words or a picture", view.ox, view.oy);
  }
  // hover outline
  if (hoverIdx >= 0 && hoverIdx !== S.sel && els[hoverIdx] && !els[hoverIdx].hidden) {
    const hb = selBox(els[hoverIdx], hoverIdx);
    if (hb) boxOutline(els[hoverIdx], hb, "rgba(184,50,42,.55)", [4, 4], 1);
  }
  // alignment guides while dragging (red: hoop centre, brass: lined up with another item)
  if (drag && drag.kind === "move" && drag.guides) {
    const [W, H] = S.layout.hoop, g = drag.guides;
    ctx.save(); ctx.lineWidth = 1.2; ctx.setLineDash([6, 4]);
    if (g.x !== null) {
      ctx.strokeStyle = g.x === 0 ? "#d6336c" : "#b08a5c";
      const [x1, y1] = mm2px(g.x, -H / 2), [, y2] = mm2px(g.x, H / 2);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x1, y2); ctx.stroke();
    }
    if (g.y !== null) {
      ctx.strokeStyle = g.y === 0 ? "#d6336c" : "#b08a5c";
      const [x1, y1] = mm2px(-W / 2, g.y), [x2] = mm2px(W / 2, g.y);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y1); ctx.stroke();
    }
    ctx.restore();
  }
  // selection: PowerPoint-style box with 8 square handles + rotate knob
  const el = els[S.sel];
  const bx = el && !el.hidden && selBox(el, S.sel);
  if (bx) {
    boxOutline(el, bx, "#b8322a", [], 1.4);
    const hs = handles(el, bx), tf = elTransform(el);
    const [tx, ty] = tf(0, -bx.h / 2);
    ctx.strokeStyle = "#b8322a"; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(hs.rot[0], hs.rot[1]); ctx.stroke();
    const a = ((el.rotation || 0) * Math.PI) / 180;
    for (const [k, [x, y]] of Object.entries(hs)) {
      ctx.fillStyle = "#fff"; ctx.strokeStyle = "#b8322a"; ctx.lineWidth = 1.6;
      if (k === "rot") {
        ctx.beginPath(); ctx.arc(x, y, 6.5, 0, 7); ctx.fill(); ctx.stroke();
        ctx.beginPath(); ctx.arc(x, y, 3, 0.4, 5.2); ctx.stroke();
      } else {
        ctx.save(); ctx.translate(x, y); ctx.rotate(a);
        ctx.fillRect(-5, -5, 10, 10); ctx.strokeRect(-5, -5, 10, 10);
        ctx.restore();
      }
    }
  }
}

function drawJumps() {
  ctx.strokeStyle = "rgba(184,50,42,.7)"; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
  let last = null;
  for (const b of S.built.sequence) for (const o of b.objects) {
    if (last) { const [a, c] = mm2px(last[0], last[1]), [d, e] = mm2px(o[0], o[1]); ctx.beginPath(); ctx.moveTo(a, c); ctx.lineTo(d, e); ctx.stroke(); }
    last = [o[o.length - 2], o[o.length - 1]];
  }
  ctx.setLineDash([]);
}

// ------------------------------------------------------------------ pointer interaction
let drag = null;
function toLocal(el, mx, my) {
  const a = (-(el.rotation || 0) * Math.PI) / 180, dx = mx - el.x, dy = my - el.y;
  return [dx * Math.cos(a) - dy * Math.sin(a), dx * Math.sin(a) + dy * Math.cos(a)];
}
function hitElement(mx, my) {
  const els = S.layout.elements;
  for (let i = els.length - 1; i >= 0; i--) {
    const el = els[i]; if (el.hidden) continue;
    const bx = selBox(el, i); if (!bx) continue;
    const [lx, ly] = toLocal(el, mx, my);
    if (Math.abs(lx) <= bx.w / 2 && Math.abs(ly) <= bx.h / 2) return i;
  }
  return -1;
}
function hitHandle(px, py) {
  const el = S.layout.elements[S.sel], bx = el && !el.hidden && selBox(el, S.sel);
  if (!bx) return null;
  const hs = handles(el, bx);
  let best = null, bd = 13; // generous grab radius (px)
  for (const [k, [hx, hy]] of Object.entries(hs)) {
    const d = Math.hypot(px - hx, py - hy);
    if (d < bd) { bd = d; best = k; }
  }
  return best;
}

cv.addEventListener("pointerdown", (e) => {
  if (e.button === 1) { // middle button: slide the whole view around
    e.preventDefault();
    drag = { kind: "pan", x0: e.clientX, y0: e.clientY, pan0: { ...S.pan } };
    cv.setPointerCapture(e.pointerId);
    cv.style.cursor = "grabbing";
    return;
  }
  if (e.button !== 0) return;
  endSew(); // clicking the design ends the sew-out preview and carries on as a normal click
  const r = cv.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
  const [mx, my] = px2mm(px, py);
  const h = hitHandle(px, py);
  if (h) {
    const el = S.layout.elements[S.sel], b = S.built.elements[S.sel];
    beginEdit();
    drag = { kind: h === "rot" ? "rot" : "resize", handle: h, el, start: JSON.parse(JSON.stringify(el)),
      w0: b.w, h0: b.h, a0: Math.atan2(my - el.y, mx - el.x) };
    cv.setPointerCapture(e.pointerId);
    return;
  }
  const i = hitElement(mx, my);
  if (i !== S.sel) { S.sel = i; renderLayers(); renderProps(); }
  if (i >= 0) {
    beginEdit();
    const el = S.layout.elements[i];
    drag = { kind: "move", el, dx: mx - el.x, dy: my - el.y };
    cv.setPointerCapture(e.pointerId);
  }
  draw();
});

function applyResize(e, mx, my) {
  const d = drag, el = d.el, s0 = d.start;
  const [sx, sy] = HANDLE_SIGN[d.handle];
  // work in the element's frame at the moment the drag started
  const a = (-(s0.rotation || 0) * Math.PI) / 180, dx = mx - s0.x, dy = my - s0.y;
  const lx = dx * Math.cos(a) - dy * Math.sin(a), ly = dx * Math.sin(a) + dy * Math.cos(a);
  const W = d.w0, H = d.h0;
  const ax = (-sx * W) / 2, ay = (-sy * H) / 2; // opposite corner / edge stays put
  let fx = 1, fy = 1;
  const corner = sx !== 0 && sy !== 0;
  if (corner || el.type === "image" || el.type === "vector" || (el.type === "text" && sy !== 0)) {
    // proportional: project the pointer onto the handle's diagonal (or axis)
    const vx = sx ? sx * W : 0, vy = sy ? sy * H : 0;
    const t = ((lx - ax) * vx + (ly - ay) * vy) / (vx * vx + vy * vy);
    fx = fy = clamp(t, 0.08, 12);
  } else if (sx !== 0) {
    fx = clamp((sx * (lx - ax)) / W, 0.08, 12);
  } else {
    fy = clamp((sy * (ly - ay)) / H, 0.08, 12);
  }
  // text side handles stretch the letter spacing, which scales only across
  if (el.type === "text" && sx !== 0 && sy === 0) {
    const n = Math.max(1, (s0.text || "").replace(/\s/g, "").length - 1);
    const grow = W * (fx - 1);
    el.letter_spacing = Math.round(clamp((s0.letter_spacing || 0) + grow / n / s0.height_mm, -0.1, 1.5) * 1000) / 1000;
    fy = 1;
  } else if (el.type === "text") {
    el.height_mm = Math.round(clamp(s0.height_mm * fx, 3, 150) * 10) / 10;
    fx = fy = el.height_mm / s0.height_mm;
  } else if (el.type === "shape") {
    el.width_mm = Math.round(clamp(s0.width_mm * fx, 2, 400) * 10) / 10;
    el.height_mm = Math.round(clamp(s0.height_mm * fy, 2, 400) * 10) / 10;
    fx = el.width_mm / s0.width_mm; fy = el.height_mm / s0.height_mm;
  } else {
    el.width_mm = Math.round(clamp(s0.width_mm * fx, 5, 400) * 10) / 10;
    fx = fy = el.width_mm / s0.width_mm;
  }
  // new centre: anchor + half the new extent, back in design coordinates
  const cxL = sx ? ax + (sx * W * fx) / 2 : 0, cyL = sy ? ay + (sy * H * fy) / 2 : 0;
  const ra = ((s0.rotation || 0) * Math.PI) / 180;
  el.x = Math.round((s0.x + cxL * Math.cos(ra) - cyL * Math.sin(ra)) * 10) / 10;
  el.y = Math.round((s0.y + cxL * Math.sin(ra) + cyL * Math.cos(ra)) * 10) / 10;
  S.vis[el.id] = { fx, fy };
}

cv.addEventListener("pointermove", (e) => {
  const r = cv.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
  const [mx, my] = px2mm(px, py);
  if (!drag) {
    const h = hitHandle(px, py), hi = hitElement(mx, my);
    cv.style.cursor = h ? HANDLE_CURSOR[h] : hi >= 0 ? "move" : "default";
    if (hi !== hoverIdx) { hoverIdx = hi; draw(); }
    return;
  }
  if (drag.kind === "pan") {
    S.pan = { x: drag.pan0.x + e.clientX - drag.x0, y: drag.pan0.y + e.clientY - drag.y0 };
    draw();
    return;
  }
  const el = drag.el;
  if (drag.kind === "move") {
    const snap = e.shiftKey ? 0.1 : 0.5;
    el.x = Math.round((mx - drag.dx) / snap) * snap; el.y = Math.round((my - drag.dy) / snap) * snap;
    // smart guides: snap to the hoop's centre lines and to other items' centres (hold Shift to place freely)
    drag.guides = { x: null, y: null };
    if (!e.shiftKey) {
      const tol = 7 / view.s; // ~7 screen px
      const xs = [0], ys = [0];
      S.layout.elements.forEach((o) => { if (o !== el && !o.hidden) { xs.push(o.x); ys.push(o.y); } });
      const near = (v, list) => list.reduce((b, c) => (Math.abs(c - v) < tol && (b === null || Math.abs(c - v) < Math.abs(b - v)) ? c : b), null);
      const gx = near(el.x, xs), gy = near(el.y, ys);
      if (gx !== null) { el.x = gx; drag.guides.x = gx; }
      if (gy !== null) { el.y = gy; drag.guides.y = gy; }
    }
  } else if (drag.kind === "resize") {
    applyResize(e, mx, my);
  } else if (drag.kind === "rot") {
    let a = drag.start.rotation + ((Math.atan2(my - el.y, mx - el.x) - drag.a0) * 180) / Math.PI;
    a = ((a + 540) % 360) - 180;
    if (!e.shiftKey) a = Math.round(a / 5) * 5;
    el.rotation = a;
  }
  draw();
});
cv.addEventListener("pointerleave", () => { if (!drag && hoverIdx !== -1) { hoverIdx = -1; draw(); } });
cv.addEventListener("pointerup", () => {
  if (!drag) return;
  const k = drag.kind;
  drag = null;
  if (k === "pan") { cv.style.cursor = "default"; return; }
  commit(true); save_local(); renderProps(); renderLayers();
  scheduleBuild(k === "resize" ? 0 : 350);
});
cv.addEventListener("wheel", (e) => {
  // zoom toward the cursor, like a slicer: the point under the mouse stays put
  e.preventDefault();
  const r = cv.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
  const [mx, my] = px2mm(px, py);
  S.zoom = clamp(S.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12), 0.5, 8);
  fitView();
  S.pan.x += px - (view.ox + mx * view.s);
  S.pan.y += py - (view.oy + my * view.s);
  draw();
}, { passive: false });

// ------------------------------------------------------------------ sew-out simulator
function startSew() {
  if (!S.built || !S.built.sequence.length) { toast("Add something to the hoop first."); return; }
  const segs = [];
  let colorIdx = 0;
  for (const b of S.built.sequence) {
    for (const o of b.objects) for (let i = 2; i < o.length; i += 2) segs.push([o[i - 2], o[i - 1], o[i], o[i + 1], b.color, colorIdx]);
    colorIdx++;
  }
  const off = document.createElement("canvas");
  S.sew = { segs, k: 0, playing: true, off, drawn: 0, colors: S.built.stats.colors };
  $("#sewBar").hidden = false; $("#sewScrub").max = segs.length; $("#sewPause").textContent = "Pause";
  sewFrame();
}
function sewFrame() {
  const sw = S.sew; if (!sw) return;
  if (sw.playing) {
    // time-based: the option value is stitches per second (a home machine sews ~700/min = ~12/s)
    const now = performance.now(), dt = Math.min(0.25, (now - (sw.t || now)) / 1000);
    sw.t = now;
    sw.acc = (sw.acc ?? sw.k) + dt * +$("#sewSpeed").value;
    if (sw.acc < sw.k) sw.acc = sw.k; // scrubbed back
    sw.k = Math.min(sw.segs.length, Math.floor(sw.acc));
    $("#sewScrub").value = sw.k;
    if (sw.k >= sw.segs.length) { sw.playing = false; $("#sewPause").textContent = "Replay"; }
  }
  draw();
  if (sw.playing) requestAnimationFrame(sewFrame);
}
function drawSew() {
  const sw = S.sew, s = view.s;
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  if (sw.off.width !== cv.width || sw.off.height !== cv.height || sw.viewKey !== `${view.s}|${view.ox}|${view.oy}` || sw.k < sw.drawn) {
    sw.off.width = cv.width; sw.off.height = cv.height; sw.drawn = 0; sw.viewKey = `${view.s}|${view.ox}|${view.oy}`;
    sw.octx = sw.off.getContext("2d"); sw.octx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  // ghost of the whole design
  for (const b of S.built.sequence) strokeObjects(ctx, b.objects, b.color, (x, y) => mm2px(x, y), s, 0.12);
  // newly sewn stitches onto the offscreen layer
  const g = sw.octx; g.lineCap = "round";
  for (let i = sw.drawn; i < sw.k; i++) {
    const [x0, y0, x1, y1, col] = sw.segs[i];
    const [a, b] = mm2px(x0, y0), [c, d] = mm2px(x1, y1);
    g.strokeStyle = shade(col, 0.55); g.lineWidth = Math.max(0.8, 0.5 * s); g.beginPath(); g.moveTo(a, b); g.lineTo(c, d); g.stroke();
    g.strokeStyle = col; g.lineWidth = Math.max(0.8, 0.36 * s); g.beginPath(); g.moveTo(a, b); g.lineTo(c, d); g.stroke();
    g.strokeStyle = shade(col, 1.45); g.lineWidth = Math.max(0.5, 0.12 * s); g.beginPath(); g.moveTo(a - 0.07 * s, b - 0.07 * s); g.lineTo(c - 0.07 * s, d - 0.07 * s); g.stroke();
  }
  sw.drawn = sw.k;
  ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.drawImage(sw.off, 0, 0); ctx.restore();
  // needle
  const cur = sw.segs[Math.max(0, sw.k - 1)];
  if (cur) {
    const [nx, ny] = mm2px(cur[2], cur[3]);
    ctx.strokeStyle = "#5c6670"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(nx, ny - 26); ctx.lineTo(nx, ny - 3); ctx.stroke();
    ctx.fillStyle = cur[4]; ctx.beginPath(); ctx.arc(nx, ny, 3, 0, 7); ctx.fill();
    const c = sw.colors[cur[5]];
    $("#sewInfo").textContent = `Thread ${cur[5] + 1} of ${sw.colors.length}: ${c ? c.thread : ""} · ${Math.round((sw.k / sw.segs.length) * 100)}%`;
  }
}
$("#btnSewOut").onclick = async () => { if (S.dirty) await build(); startSew(); };
$("#sewPause").onclick = () => {
  const sw = S.sew; if (!sw) return;
  if (!sw.playing && sw.k >= sw.segs.length) sw.k = 0;
  sw.playing = !sw.playing; $("#sewPause").textContent = sw.playing ? "Pause" : "Play";
  sw.acc = sw.k; sw.t = null; // resume from where the needle is, without a time jump
  if (sw.playing) sewFrame();
};
$("#sewScrub").oninput = () => { if (!S.sew) return; S.sew.k = +$("#sewScrub").value; S.sew.acc = S.sew.k; S.sew.playing = false; $("#sewPause").textContent = "Play"; draw(); };
$("#sewClose").onclick = endSew;

// ------------------------------------------------------------------ export & disk
function offHoopMessage() {
  const o = S.built?.stats?.over || {};
  const parts = Object.entries(o).map(([k, v]) => `${v} mm past the ${k} edge`);
  return `Can't save yet: the design is ${parts.join(" and ") || "partly off the hoop"}. Move or shrink it so everything is inside the hoop.`;
}
function blockedOffHoop(inDialog) {
  if (!S.built?.stats?.outside) return false;
  if (inDialog) { const r = $("#diskResult"); r.className = "result bad"; r.textContent = offHoopMessage(); }
  else toast(offHoopMessage(), "bad");
  return true;
}
async function doExport() {
  if (blockedOffHoop()) return;
  const fmt = $("#exportFormat").value;
  busy("now", "Winding the bobbin…");
  try {
    const r = await api("/api/export", { layout: S.layout, format: fmt }, { blob: true });
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (S.layout.name || "design").replace(/[^\w\- ]+/g, "") + "." + fmt;
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    toast(`Saved ${esc(a.download)} to your Downloads.`, "good");
  } catch (e) { toast(esc(e.message), "bad"); } finally { busy(false); }
}

async function openDisk() {
  if (!S.layout.elements.length) { toast("Add something to the hoop first."); return; }
  if (blockedOffHoop()) return; // say so up front, before the disk window covers the page
  $("#diskResult").className = "result"; $("#diskResult").textContent = "";
  $("#dlgDisk").showModal();
  await refreshDrives();
}
async function refreshDrives() {
  const sel = $("#diskDrive");
  sel.innerHTML = `<option>Looking for drives…</option>`;
  try {
    const { drives } = await api("/api/disk/drives");
    const opts = drives.map((d) => `<option value="${esc(d.path)}"${d.ready ? "" : " disabled"}>${esc(d.path)} ${d.floppy ? "floppy" : "removable"}${d.ready ? "" : " — no disk"}</option>`);
    sel.innerHTML = opts.join("") || `<option value="">No removable drives found</option>`;
    const ready = drives.find((d) => d.ready && d.floppy) || drives.find((d) => d.ready);
    if (ready) sel.value = ready.path;
    await diskStatus();
  } catch (e) { sel.innerHTML = `<option value="">${esc(e.message)}</option>`; }
}
function diskTarget() { return $("#diskFolder").value.trim() || $("#diskDrive").value; }
async function diskStatus() {
  const t = diskTarget(), box = $("#diskStatus");
  if (!t) { box.textContent = "Put a disk in the drive, then press Look again."; return; }
  try {
    const st = await api("/api/disk/status", { target: t });
    const mode = $$('input[name=diskMode]').find((r) => r.checked).value;
    const next = mode === "replace" || !st.layout ? 0 : st.used;
    box.innerHTML = (st.layout ? `This disk has a Designer menu with <b>${st.used}</b> of 36 designs.` : `No Designer menu on this disk yet — I'll create one.`) +
      `<div class="disk-slots">${Array.from({ length: 36 }, (_, i) => `<i class="${i === next ? "next" : i < st.used && mode === "add" ? "used" : ""}" title="Slot ${i + 1}"></i>`).join("")}</div>` +
      `<div class="muted small" style="margin-top:6px">Your design will be <b>Menu 1, slot ${next + 1}</b>.</div>`;
  } catch (e) { box.textContent = e.message; }
}
async function writeDisk() {
  const target = diskTarget();
  const mode = $$('input[name=diskMode]').find((r) => r.checked).value;
  if (!target) { const r = $("#diskResult"); r.className = "result bad"; r.textContent = "Choose a drive first."; return; }
  if (blockedOffHoop(true)) return;
  if (mode === "replace" && !confirm("Start fresh? The designs already in Menu 1 on this disk will be replaced.")) return;
  const res = $("#diskResult");
  res.className = "result"; res.textContent = "Writing… (floppies are slow — about 10 seconds)";
  $("#diskWrite").disabled = true;
  try {
    const r = await api("/api/disk/write", { layout: S.layout, target, mode, label: $("#diskLabel").value });
    res.className = "result ok";
    res.innerHTML = `✓ Written and checked. On the machine: <b>Menu 1 → slot ${r.slot}</b>. (${r.files.map(esc).join(", ")})`;
    await diskStatus();
  } catch (e) { res.className = "result bad"; res.textContent = e.message; } finally { $("#diskWrite").disabled = false; }
}

// ------------------------------------------------------------------ settings / projects
const FABRIC_COLORS = ["#d9d5cc", "#f6f4ef", "#2a2a2e", "#1f2a44", "#8a1c1c", "#4a5a3a", "#c7a98a", "#9fb5c9", "#e8c7c8"];
function openSettings() {
  const hv = S.layout.hoop.join("x");
  const preset = [...$("#setHoop").options].some((o) => o.value === hv);
  $("#setHoop").value = preset ? hv : "custom";
  $("#setHoopW").value = S.layout.hoop[0]; $("#setHoopH").value = S.layout.hoop[1];
  $("#customHoop").hidden = preset;
  $("#setHoop").onchange = () => { $("#customHoop").hidden = $("#setHoop").value !== "custom"; };
  $("#setFabric").innerHTML = Object.entries(S.meta.fabrics).map(([k, v]) => `<option value="${k}"${k === S.layout.fabric ? " selected" : ""}>${esc(v)}</option>`).join("");
  $("#setGroup").checked = S.layout.group_colors !== false;
  $("#setDensity").value = S.layout.density || "standard";
  $("#setKey").value = ""; $("#setKey").placeholder = S.meta.ai ? "Key saved — paste a new one to replace" : "sk-ant-…";
  $("#setWorkspace").value = S.meta.workspace || "";
  $("#fabricSwatches").innerHTML = FABRIC_COLORS.map((c) => `<button type="button" data-c="${c}" style="background:${c}" class="${c === S.layout.fabric_color ? "on" : ""}" aria-label="Fabric ${c}"></button>`).join("");
  $$("#fabricSwatches button").forEach((b) => (b.onclick = () => { $$("#fabricSwatches button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); }));
  $("#dlgSettings").showModal();
}
async function saveSettings() {
  commit(true); beginEdit();
  S.layout.hoop = $("#setHoop").value === "custom"
    ? [clamp(Math.round(+$("#setHoopW").value || 100), 20, 500), clamp(Math.round(+$("#setHoopH").value || 100), 20, 500)]
    : $("#setHoop").value.split("x").map(Number);
  S.layout.fabric = $("#setFabric").value;
  S.layout.density = $("#setDensity").value;
  S.layout.group_colors = $("#setGroup").checked;
  const fc = $("#fabricSwatches button.on"); if (fc) S.layout.fabric_color = fc.dataset.c;
  commit(true);
  const key = $("#setKey").value.trim(), ws = $("#setWorkspace").value.trim();
  if (key || ws !== (S.meta.workspace || "")) {
    const r = await api("/api/ai/key", { key, workspace: ws });
    S.meta.ai = r.ai; S.meta.workspace = r.workspace;
    toast("AI settings saved on this computer.", "good");
  }
  $("#dlgSettings").close();
  renderProps(); scheduleBuild(0); save_local();
}
async function openProjects() {
  const items = await api("/api/projects");
  $("#projectList").innerHTML = items.map((p) => `<li data-f="${esc(p.file)}">${esc(p.name)}<span>${new Date(p.modified * 1000).toLocaleString()}</span></li>`).join("") ||
    `<li class="muted">No saved projects yet.</li>`;
  $$("#projectList li[data-f]").forEach((li) => (li.onclick = async () => {
    const lay = await api("/api/projects/" + encodeURIComponent(li.dataset.f));
    commit(true); S.layout = Object.assign(DEFAULT_LAYOUT(), lay); S.sel = -1; await loadImageInfo(); afterLoad();
    $("#dlgOpen").close(); toast(`Opened “${esc(S.layout.name)}”.`);
  }));
  $("#dlgOpen").showModal();
}
async function loadImageInfo() {
  for (const el of S.layout.elements) if (el.type === "image" && !S.images[el.image_id]) S.images[el.image_id] = { aspect: el.aspect || 1 };
}

// ------------------------------------------------------------------ wiring
function pickFile(then) {
  const f = $("#fileInput");
  f.value = ""; f.onchange = () => f.files[0] && then(f.files[0]); f.click();
}
$("#addText").onclick = addText;
$("#addShape").onclick = addShape;
$("#addImage").onclick = () => pickFile(addPicture);
$("#addAI").onclick = () => pickFile(readPicture);
$("#btnExport").onclick = doExport;
$("#btnDisk").onclick = openDisk;
$("#diskRefresh").onclick = refreshDrives;
$("#diskDrive").onchange = diskStatus;
$("#diskFolder").onchange = diskStatus;
$$('input[name=diskMode]').forEach((r) => (r.onchange = diskStatus));
$("#diskWrite").onclick = writeDisk;
$("#btnSettings").onclick = openSettings;
$("#setSave").onclick = saveSettings;
$("#btnOpen").onclick = openProjects;
$("#btnSave").onclick = async () => { try { const r = await api("/api/projects", S.layout); toast(`Saved project “${esc(S.layout.name)}”.`, "good"); } catch (e) { toast(esc(e.message), "bad"); } };
$("#btnNew").onclick = () => {
  if (S.layout.elements.length && !confirm("Start a new design? (Save first if you want to keep this one.)")) return;
  commit(true); const keep = { hoop: S.layout.hoop, fabric: S.layout.fabric, fabric_color: S.layout.fabric_color };
  S.layout = Object.assign(DEFAULT_LAYOUT(), keep); S.sel = -1; afterLoad();
};
$("#btnUndo").onclick = undo;
$("#btnRedo").onclick = redo;
$("#designName").oninput = () => { beginEdit(); S.layout.name = $("#designName").value; commit(); save_local(); };
$("#zoomIn").onclick = () => { S.zoom = clamp(S.zoom * 1.25, 0.4, 8); draw(); };
$("#zoomOut").onclick = () => { S.zoom = clamp(S.zoom / 1.25, 0.4, 8); draw(); };
$("#zoomFit").onclick = () => { S.zoom = 1; S.pan = { x: 0, y: 0 }; draw(); };
cv.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); }); // no browser autoscroll
new ResizeObserver(() => resize()).observe($("#hoopWrap")); // redraw whenever the hoop area changes size
$("#showArt").onchange = draw;
$("#showJumps").onchange = draw;
window.addEventListener("resize", resize);
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y" && !typing) { e.preventDefault(); redo(); return; }
  if (typing) return;
  const el = S.layout.elements[S.sel];
  if (!el) return;
  if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeElement(S.sel); }
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "d") { e.preventDefault(); duplicate(S.sel); }
  else if (e.key === "Escape") { S.sel = -1; renderLayers(); renderProps(); draw(); }
  else if (e.key.startsWith("Arrow")) {
    e.preventDefault(); beginEdit();
    const st = e.shiftKey ? 5 : 0.5;
    if (e.key === "ArrowLeft") el.x -= st; if (e.key === "ArrowRight") el.x += st;
    if (e.key === "ArrowUp") el.y -= st; if (e.key === "ArrowDown") el.y += st;
    el.x = Math.round(el.x * 10) / 10; el.y = Math.round(el.y * 10) / 10;
    commit(); draw(); save_local(); scheduleBuild(500);
    const px = $("#p-x"), py = $("#p-y"); if (px) { px.value = el.x; py.value = el.y; }
  }
});
// drop a picture anywhere onto the hoop
$("#hoopWrap").addEventListener("dragover", (e) => e.preventDefault());
$("#hoopWrap").addEventListener("drop", (e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f && f.type.startsWith("image/")) addPicture(f); });

// ------------------------------------------------------------------ start
(async function start() {
  const [meta, fonts] = await Promise.all([api("/api/meta"), api("/api/fonts")]);
  S.meta = meta; S.fonts = fonts;
  const order = ["vp3", "zip", "shv", "pes", "dst", "jef", "exp"];
  $("#exportFormat").innerHTML = order.filter((k) => meta.formats[k]).map((k) => `<option value="${k}">${esc(meta.formats[k])}</option>`).join("");
  try {
    const saved = JSON.parse(localStorage.getItem("thimble.layout") || "null");
    if (saved && saved.elements) { S.layout = Object.assign(DEFAULT_LAYOUT(), saved); await loadImageInfo(); }
  } catch (e) { /* ignore */ }
  const q = new URLSearchParams(location.search);
  if (q.get("example") && EXAMPLES[q.get("example")]) {
    S.layout = Object.assign(DEFAULT_LAYOUT(), JSON.parse(JSON.stringify(EXAMPLES[q.get("example")])));
    if (q.get("select")) S.sel = +q.get("select");
  }
  $("#designName").value = S.layout.name;
  renderLayers(); renderProps();
  resize();
  scheduleBuild(0);
  if (q.get("sew")) setTimeout(async () => { await build(); startSew(); if (+q.get("sew") > 1) { S.sew.k = Math.floor(S.sew.segs.length * +q.get("sew") / 100); } }, 600);
})();
