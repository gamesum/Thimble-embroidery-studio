/* Thimble — Embroidery Studio front end */
"use strict";

// ------------------------------------------------------------------ state
const DEFAULT_LAYOUT = () => ({
  name: "My design", hoop: [100, 100], fabric: "knit", group_colors: true, fabric_color: "#d9d5cc", elements: [],
});
// display units: everything is stored in mm; inches are a view (per-viewer preference)
let UNITS = "mm";
try { UNITS = localStorage.getItem("thimble.units") === "in" ? "in" : "mm"; } catch (e) {}
const U = {
  get inch() { return UNITS === "in"; },
  get u() { return UNITS === "in" ? "in" : "mm"; },
  show(mm) { return UNITS === "in" ? Math.round((mm / 25.4) * 100) / 100 : Math.round(mm * 10) / 10; },
  toMm(v) { return UNITS === "in" ? v * 25.4 : v; },
  len(mm) { return `${U.show(mm)} ${U.u}`; },
  lab(s) { return UNITS === "in" ? s.replace(/\bmm\b/g, "in") : s; },
  thread(m) { return UNITS === "in" ? `${Math.round(m * 1.0936 * 10) / 10} yd` : `${m} m`; },
  step(stepMm) { return UNITS === "in" ? Math.max(0.01, Math.round((stepMm / 25.4) * 100) / 100) : stepMm; },
};
function setUnits(u) {
  UNITS = u === "in" ? "in" : "mm";
  try { localStorage.setItem("thimble.units", UNITS); } catch (e) {}
  const t = $("#unitsToggle"); if (t) t.textContent = UNITS;
  renderProps(); renderChart(); draw();
}

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
// website: a picture read runs in the background (web hosts cut long requests off) and the page
// collects the result through the same progress polling the busy badge uses
async function aiInBackground(body, retried = false) {
  const job = `j${Date.now().toString(36)}${jobSeq++}`;
  const r = await fetch("/api/ai/analyze", { method: "POST", body: JSON.stringify(body), headers: {
    "Content-Type": "application/json", "X-Job": job, "X-Async": "1", "X-Anthropic-Key": webKey.key, "X-Anthropic-Workspace": webKey.ws } });
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (r.status === 409 && j.missing && !retried) { await restorePictures(j.missing); return aiInBackground(body, true); }
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  const bar = $("#busyBar"), pct = $("#busyPct");
  for (;;) {
    await new Promise((ok) => setTimeout(ok, 700));
    if (activeAbort?.signal.aborted) { stopJob(); throw new Error("Cancelled."); }
    let p;
    try { p = await (await fetch(`/api/progress/${job}`)).json(); } catch (e) { continue; }
    if (p.pct) { bar.style.width = p.pct + "%"; pct.textContent = p.pct + "%"; if (p.msg) $("#busySub").textContent = p.msg; }
    if (p.done) { stopJob(); if (p.error) throw new Error(p.error); return p.result; }
  }
}
// website mode: the visitor's API key lives only in their own browser
const webKey = {
  get key() { try { return localStorage.getItem("thimble.akey") || ""; } catch (e) { return ""; } },
  get ws() { try { return localStorage.getItem("thimble.aws") || ""; } catch (e) { return ""; } },
  set(key, ws) {
    try {
      if (key) localStorage.setItem("thimble.akey", key);
      if (ws) localStorage.setItem("thimble.aws", ws); else localStorage.removeItem("thimble.aws");
    } catch (e) {}
  },
};
// ---- the browser keeps its own copy of every picture (IndexedDB, never leaves this computer
// except to this app's server). The website's server forgets uploads when it restarts; when it
// says a picture is missing, the copy is sent back under the same name and the request retried.
const picStore = (() => {
  let dbp = null;
  const db = () => dbp || (dbp = new Promise((ok, bad) => {
    const r = indexedDB.open("thimble", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("pics");
    r.onsuccess = () => ok(r.result); r.onerror = () => bad(r.error);
  }));
  const run = async (mode, fn) => {
    try {
      const d = await db();
      return await new Promise((ok, bad) => { const t = d.transaction("pics", mode); const q = fn(t.objectStore("pics")); t.oncomplete = () => ok(q && q.result); t.onerror = () => bad(t.error); });
    } catch (e) { return undefined; }
  };
  return { put: (id, blob) => run("readwrite", (s) => s.put(blob, id)), get: (id) => run("readonly", (s) => s.get(id)) };
})();
async function keepPicture(id) {
  // a picture the server made (split pieces, traced stitch files): keep a copy too
  try { const r = await fetch("/api/image/" + encodeURIComponent(id)); if (r.ok) await picStore.put(id, await r.blob()); } catch (e) { /* not critical */ }
}
async function restorePictures(ids) {
  for (const id of ids) {
    const blob = await picStore.get(id);
    if (!blob) throw new Error("A picture in this design is no longer on the website's server (it was added before Thimble kept a copy in your browser). Add it again with Picture / file - from now on this is automatic.");
    const fd = new FormData(); fd.append("file", new File([blob], id, { type: blob.type || "image/png" })); fd.append("restore_id", id);
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    if (!r.ok) throw new Error("Couldn't put a picture back on the server.");
    const info = S.images[id];
    if (info) delete info._img;  // redraw it from the restored file
  }
}

let activeAbort = null;
async function api(path, body, opts = {}) {
  const long = ["/api/build", "/api/ai/analyze", "/api/export", "/api/disk/write", "/api/disk/build"].includes(path);
  if (long && path !== "/api/build") activeAbort = new AbortController();  // cancellable (not the quiet rebuilds)
  if (S.meta?.hosted && path === "/api/ai/analyze") return aiInBackground(body);
  const job = long ? `j${Date.now().toString(36)}${jobSeq++}` : null;
  if (job) watchJob(job);
  let r;
  try {
    r = await fetch(path, body === undefined ? {} : {
      signal: path !== "/api/build" ? activeAbort?.signal : undefined,
      method: "POST", headers: Object.assign({ "Content-Type": "application/json" }, job ? { "X-Job": job } : {},
        S.meta?.hosted && path === "/api/ai/analyze" ? { "X-Anthropic-Key": webKey.key, "X-Anthropic-Workspace": webKey.ws } : {}), body: JSON.stringify(body),
    });
  } catch (e) {
    if (e.name === "AbortError") throw new Error("Cancelled.");
    throw e;
  } finally { if (job) stopJob(); }
  if (opts.blob && r.status === 409 && !opts.retried) {
    const j = await r.clone().json().catch(() => ({}));
    if (j.missing) { await restorePictures(j.missing); return api(path, body, Object.assign({}, opts, { retried: true })); }
  }
  if (opts.blob) {
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r;
  }
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (r.status === 409 && j.missing && !opts.retried) {
    await restorePictures(j.missing);
    return api(path, body, Object.assign({}, opts, { retried: true }));
  }
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}

function toast(msg, kind = "") {
  if (/^Cancelled\.?$/.test(msg)) { kind = ""; msg = "Cancelled."; }
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
  else { $("#busy").hidden = true; activeAbort = null; }
  $("#busyCancel").hidden = on !== "now";
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
    const sent = JSON.stringify(S.layout);
    const res = await api("/api/build", S.layout);
    if (seq !== S.buildSeq) return;
    S.built = res; S.dirty = false; S.fontPrev = null;
    // only drop the resize previews once the stitches really are for the design as it is now
    if (sent === JSON.stringify(S.layout)) S.vis = {};
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
    {"type": "shape", "kind": "offset_frame", "width_mm": 96, "height_mm": 52, "stroke_mm": 1.0, "color": "#9a6b3f", "style": "auto", "x": 0, "y": 3, "rotation": 0},
    {"type": "text", "text": "LIMITED", "font": "✦ Barstitch Bold", "height_mm": 13, "letter_spacing": 0.04, "color": "#b98a5a", "outline": {"color": "#6b4423", "width_mm": 0.9, "first": true}, "arc": 0, "x": 0, "y": -8, "rotation": 0, "style": "auto"},
    {"type": "text", "text": "edition", "font": "✦ Magnolia", "height_mm": 16, "letter_spacing": 0, "color": "#1d1d1d", "arc": 0, "x": 3, "y": 12, "rotation": 0, "style": "auto"},
    {"type": "shape", "kind": "heart", "width_mm": 6, "height_mm": 5.5, "color": "#d62839", "style": "auto", "x": 0, "y": -27, "rotation": 0}] },
  varsity: { name: "Varsity", fabric: "fleece", fabric_color: "#1f2a44", elements: [
    {"type": "text", "text": "RIDGELINE", "font": "✦ TT Directors", "height_mm": 15, "letter_spacing": 0.08, "arc": 50, "color": "#f2f0ea", "outline": {"color": "#b8322a", "width_mm": 1.0, "first": true}, "x": 0, "y": -18, "rotation": 0, "style": "auto"},
    {"type": "text", "text": "Athletics", "font": "✦ Magnolia", "height_mm": 17, "letter_spacing": 0, "arc": 0, "color": "#c9a24a", "x": 0, "y": 8, "rotation": -6, "style": "auto"},
    {"type": "text", "text": "EST. 2026", "font": "✦ Ink/Stitch Small Font", "height_mm": 5, "letter_spacing": 0.2, "arc": 0, "color": "#f2f0ea", "x": 0, "y": 27, "rotation": 0, "style": "auto"}] },
  monogram: { name: "Monogram", fabric: "woven", fabric_color: "#f6f4ef", elements: [
    {"type": "shape", "kind": "ring", "width_mm": 56, "height_mm": 56, "stroke_mm": 1.4, "color": "#6f8c6a", "style": "auto", "x": 0, "y": -4, "rotation": 0},
    {"type": "text", "text": "S", "font": "✦ Montecarlo", "height_mm": 32, "letter_spacing": 0, "arc": 0, "color": "#3a2c22", "x": 0, "y": -4, "rotation": 0, "style": "auto"},
    {"type": "text", "text": "SEW WELL", "font": "✦ Ink/Stitch Small Font", "height_mm": 5, "letter_spacing": 0.3, "arc": -120, "color": "#6f8c6a", "x": 0, "y": 32, "rotation": 0, "style": "auto"}] },
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
  ["frame", "Frame"], ["double_frame", "Double frame"], ["offset_frame", "Offset frame"], ["line", "Line"], ["tack", "Tack line"]];
const TEXT_STYLES = [["auto", "Auto - satin, or fill on big bold letters"], ["satin", "Satin sweeps - long glossy stitches across every stroke"], ["rows", "Straight across - every stitch side to side, one direction"], ["fill", "Fill - rows of short stitches"]];
const STYLES = [["auto", "Auto (recommended)"], ["satin", "Satin — follows the strokes"], ["satinfill", "Satin — one direction"],
  ["fill", "Fill — tatami"], ["rows", "Satin rows — straight across, all one direction"], ["contour", "Contour — rings follow the edge"], ["run", "Outline — running stitch"]];

function freeSpotY(h) {
  // stack new things under existing ones so they don't land on top of each other
  const els = S.layout.elements;
  if (!els.length || !S.built) return 0;
  let bottom = -Infinity;
  els.forEach((el, i) => { const b = S.built.elements[i]; if (b) bottom = Math.max(bottom, el.y + b.h / 2); });
  const y = bottom + 4 + h / 2;
  return y + h / 2 < S.layout.hoop[1] / 2 ? y : 0;
}

// ---- multi-select (Shift+click, or drag a box on empty fabric)
const multi = new Set(); // element ids
function selected() {
  // indices of everything selected (multi-selection, else the single selected item)
  const els = S.layout.elements;
  const ids = multi.size ? multi : new Set(S.sel >= 0 && els[S.sel] ? [els[S.sel].id] : []);
  return els.map((e, i) => (ids.has(e.id) ? i : -1)).filter((i) => i >= 0);
}
function clearMulti() { multi.clear(); }
function toggleInSelection(i) {
  const els = S.layout.elements;
  if (!multi.size && S.sel >= 0 && els[S.sel]) multi.add(els[S.sel].id);
  const id = els[i].id;
  if (multi.has(id)) {
    multi.delete(id);
    if (S.sel === i) S.sel = selected()[0] ?? -1;
  } else { multi.add(id); S.sel = i; }
  if (multi.size <= 1) { const only = selected()[0]; multi.clear(); S.sel = only ?? -1; }
  renderLayers(); renderProps(); draw();
}
function removeSelected() {
  const idx = selected();
  if (!idx.length) return;
  commit(true); beginEdit();
  const gone = new Set(idx.map((i) => S.layout.elements[i].id));
  S.layout.elements = S.layout.elements.filter((e) => !gone.has(e.id));
  multi.clear(); S.sel = -1;
  commit(true); renderLayers(); renderProps(); scheduleBuild(0); save_local();
}
function groupBox(idx) {
  // bounding box (mm) of several items, from their stitched sizes
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const i of idx) {
    const el = S.layout.elements[i], b = shownSize(el, i);
    if (!b) continue;
    const a = ((el.rotation || 0) * Math.PI) / 180, c = Math.abs(Math.cos(a)), s = Math.abs(Math.sin(a));
    const hw = (b.w * c + b.h * s) / 2, hh = (b.w * s + b.h * c) / 2;
    x0 = Math.min(x0, el.x - hw); x1 = Math.max(x1, el.x + hw); y0 = Math.min(y0, el.y - hh); y1 = Math.max(y1, el.y + hh);
  }
  return isFinite(x0) ? { x0, y0, x1, y1 } : null;
}
function moveSelected(dx, dy) {
  for (const i of selected()) {
    const el = S.layout.elements[i];
    el.x = Math.round((el.x + dx) * 10) / 10; el.y = Math.round((el.y + dy) * 10) / 10;
  }
}

function addElement(el) {
  clearMulti();
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
  await picStore.put(j.image_id, file);
  return j;
}

function fitWidth(aspect) {
  const [W, H] = S.layout.hoop;
  return Math.round(Math.min(W * 0.9, (H * 0.9) / aspect));
}

// files that aren't pictures: embroidery files keep their own stitches, SVG becomes a drawing
const EMB_EXT = new Set("100,10o,bro,dat,dsb,dst,dsz,emd,exp,exy,fxy,gt,hus,inb,jef,jpx,ksm,max,mit,new,pcd,pcm,pcq,pcs,pec,pes,phb,phc,sew,shv,spx,stc,stx,tap,tbf,u01,vp3,xxx,zhs,zxy".split(",").map((e) => e.replace(".", "")));
// a stitch file can't be resized/edited freely - redraw it as a picture, then trace it or have
// the AI rebuild it, and put the result in its place at the same size
async function recreateStitches(el, useAI) {
  if (useAI && !S.meta.ai) { toast("Add your Anthropic API key in Settings first.", "bad"); openSettings(); return; }
  busy("now", useAI ? "Recreating with AI…" : "Tracing the stitches…");
  try {
    const info = await api("/api/stitches-to-image", el);
    S.images[info.image_id] = info; keepPicture(info.image_id);
    const at = S.layout.elements.indexOf(el);
    let fresh;
    if (useAI) {
      // read it as if it were a hoop the size of the stitch file, then move it to where the file was
      const res = await api("/api/ai/analyze", { image_id: info.image_id, hoop: [info.width_mm / 0.9, info.height_mm / 0.9], mode: "exact" });
      fresh = res.elements.map((e) => Object.assign(e, { id: uid(), x: Math.round((e.x + el.x) * 10) / 10, y: Math.round((e.y + el.y) * 10) / 10 }));
      if (!fresh.length) throw new Error("The AI couldn't find anything to rebuild in that file.");
      S.layout.ai_read = { image_id: info.image_id, mode: "exact", raw: res.raw, ids: fresh.map((e) => e.id) };
    } else {
      fresh = [{ id: uid(), type: "image", image_id: info.image_id, name: (el.name || "Stitch file") + " (traced)", width_mm: info.width_mm,
        aspect: info.aspect, colors: info.colors, x: el.x, y: el.y, rotation: el.rotation || 0 }];
    }
    commit(true); beginEdit();
    S.layout.elements.splice(at, 1, ...fresh);
    commit(true);
    S.sel = at; S.autoSimplify = useAI;
    renderLayers(); renderProps(); scheduleBuild(0); save_local();
    toast(useAI ? `Recreated as ${fresh.length} editable piece${fresh.length === 1 ? "" : "s"}. Undo brings the stitch file back.`
      : "Traced into an editable picture. Undo brings the stitch file back.", "good");
  } catch (e) { toast(esc(e.message), "bad"); } finally { busy(false); }
}
async function importFile(file) {
  busy("now", "Opening " + file.name + "…");
  try {
    const fd = new FormData(); fd.append("file", file); fd.append("hoop", S.layout.hoop.join(","));
    const r = await fetch("/api/import", { method: "POST", body: fd });
    const j = await r.json().catch(() => ({ error: r.statusText }));
    if (!r.ok || j.error) throw new Error(j.error || "import failed");
    addElement(j.element);
    toast(j.element.type === "stitches"
      ? `Opened ${esc(file.name)} - its stitches are used as they are. You can move, turn and recolor it.`
      : `Imported ${esc(file.name)} as a drawing - clean shapes, ready to stitch.`, "good");
  } catch (e) { toast(esc(e.message), "bad"); } finally { busy(false); }
}
async function addPicture(file) {
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (ext === "thimble") return openFile(file);  // a project, whichever button it came through
  if (ext === "svg" || EMB_EXT.has(ext)) return importFile(file);
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
    const mode = document.querySelector('input[name="aiMode"]:checked')?.value || "exact";
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
  clearMulti();
  commit(true); beginEdit();
  S.layout.elements.splice(i, 1);
  S.sel = Math.min(S.sel, S.layout.elements.length - 1);
  if (S.sel === i) S.sel = -1;
  commit(true); renderLayers(); renderProps(); scheduleBuild(0); save_local();
}
function duplicate(i) {
  const c = JSON.parse(JSON.stringify(S.layout.elements[i]));
  c.x += 4; c.y += 4; delete c.id; delete c.group;
  addElement(c);
}
// ---- groups: pieces that select, move, resize, turn and delete together
function groupMates(el) {
  return el && el.group ? S.layout.elements.filter((x) => x.group === el.group && !x.hidden) : el ? [el] : [];
}
function selectGroupOf(i) {
  const el = S.layout.elements[i];
  clearMulti();
  for (const x of groupMates(el)) multi.add(x.id);
  if (multi.size <= 1) multi.clear();
  S.sel = i;
}
function groupSelected() {
  const idx = selected();
  if (idx.length < 2) return;
  beginEdit();
  const g = "g" + uid();
  for (const i of idx) S.layout.elements[i].group = g;
  commit(true); save_local(); renderLayers(); renderProps();
  toast(`Grouped ${idx.length} pieces. Click any of them to pick up the whole group; Alt+click picks just one.`, "good");
}
function ungroupSelected() {
  const idx = multi.size > 1 ? selected() : S.sel >= 0 ? S.layout.elements.filter((x) => x.group && x.group === S.layout.elements[S.sel].group).map((x) => S.layout.elements.indexOf(x)) : [];
  if (!idx.some((i) => S.layout.elements[i].group)) return;
  beginEdit();
  for (const i of idx) delete S.layout.elements[i].group;
  commit(true); save_local(); renderLayers(); renderProps();
  toast("Ungrouped - each piece moves on its own again.", "good");
}
// ---- copy / paste (works across Thimble tabs on this computer too)
const clip = {
  get() { try { return JSON.parse(localStorage.getItem("thimble.clip") || "null") || clip.mem; } catch (e) { return clip.mem; } },
  set(v) { clip.mem = v; try { localStorage.setItem("thimble.clip", JSON.stringify(v)); } catch (e) {} },
  mem: null, pastes: 0,
};
function copySelected(cut = false) {
  const idx = selected();
  if (!idx.length) return;
  clip.set({ elements: idx.map((i) => JSON.parse(JSON.stringify(S.layout.elements[i]))), images: Object.fromEntries(idx.map((i) => S.layout.elements[i]).filter((e) => e.type === "image").map((e) => [e.image_id, S.images[e.image_id] ? { aspect: S.images[e.image_id].aspect, colors: S.images[e.image_id].colors } : {}])) });
  clip.pastes = 0;
  if (cut) { if (multi.size > 1) removeSelected(); else removeElement(S.sel); toast(`Cut ${idx.length === 1 ? "1 piece" : idx.length + " pieces"} - Ctrl+V to paste.`); }
  else toast(`Copied ${idx.length === 1 ? "1 piece" : idx.length + " pieces"} - Ctrl+V to paste.`);
}
function pasteClip() {
  const c = clip.get();
  if (!c || !c.elements?.length) { toast("Nothing copied yet - select something and press Ctrl+C."); return; }
  clip.pastes++;
  const off = 5 * clip.pastes, groups = {};
  commit(true); beginEdit();
  const fresh = c.elements.map((e0) => {
    const e = JSON.parse(JSON.stringify(e0));
    e.id = uid(); e.x = Math.round((e.x + off) * 10) / 10; e.y = Math.round((e.y + off) * 10) / 10;
    if (e.group) e.group = groups[e.group] || (groups[e.group] = "g" + uid());
    if (e.type === "image" && !S.images[e.image_id]) S.images[e.image_id] = Object.assign({ image_id: e.image_id }, c.images?.[e.image_id] || {});
    return e;
  });
  S.layout.elements.push(...fresh);
  commit(true);
  clearMulti();
  if (fresh.length > 1) fresh.forEach((e) => multi.add(e.id));
  S.sel = S.layout.elements.length - 1;
  renderLayers(); renderProps(); scheduleBuild(0); save_local();
}
async function mergeSelected() {
  const idx = selected();
  const els = idx.map((i) => S.layout.elements[i]);
  if (els.length < 2) { toast("Select two or more pieces to merge them."); return; }
  if (els.some((e) => e.type !== "vector" && e.type !== "image")) { toast("Words and shapes can't be merged into one - use Group (Ctrl+G) to keep them together."); return; }
  try {
    const r = await api("/api/merge-drawings", { elements: els });
    if (r.element.type === "image") { S.images[r.element.image_id] = { image_id: r.element.image_id, aspect: r.element.aspect, colors: r.element.colors }; keepPicture(r.element.image_id); }
    commit(true); beginEdit();
    const at = Math.min(...idx);
    r.element.id = uid();
    const gone = new Set(els.map((e) => e.id));
    S.layout.elements = S.layout.elements.filter((e) => !gone.has(e.id));
    S.layout.elements.splice(at, 0, r.element);
    commit(true);
    clearMulti(); S.sel = at;
    renderLayers(); renderProps(); scheduleBuild(0); save_local();
    toast(`Merged ${els.length} pieces into one ${r.element.type === "image" ? "picture" : "drawing"}.${r.note ? " " + r.note : ""}`, "good");
  } catch (e) { toast(esc(e.message), "bad"); }
}
function duplicateSelected() {
  // copy the whole selection; copies of a group become a new group of their own
  const idx = selected();
  if (!idx.length) return;
  commit(true); beginEdit();
  const newGroups = {}, copies = [];
  for (const i of idx) {
    const c = JSON.parse(JSON.stringify(S.layout.elements[i]));
    c.x += 4; c.y += 4; c.id = uid();
    if (c.group) c.group = newGroups[c.group] || (newGroups[c.group] = "g" + uid());
    copies.push(c);
  }
  S.layout.elements.push(...copies);
  commit(true);
  clearMulti(); copies.forEach((c) => multi.add(c.id)); S.sel = S.layout.elements.length - 1;
  renderLayers(); renderProps(); scheduleBuild(0); save_local();
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
  if (el.type === "stitches") return (el.blocks || [])[0]?.color || "#999";
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
  // top of the list = in front (sewn last, on top); bottom = at the back (sewn first)
  for (let i = S.layout.elements.length - 1; i >= 0; i--) {
    const el = S.layout.elements[i];
    const li = document.createElement("li");
    li.className = "layer" + (i === S.sel || multi.has(el.id) ? " sel" : "") + (el.hidden ? " hidden" : "");
    li.innerHTML = `<span class="dot" style="background:${esc(elColor(el))}"></span>
      <span class="name">${el.group ? `<span class="grp" title="In a group">⛓</span> ` : ""}${esc(elLabel(el))}</span><span class="kind">${el.type === "image" ? "pic" : el.type === "vector" ? "drawing" : el.type === "stitches" ? "stitch file" : el.type}</span>
      <span class="icons">
        <button data-a="up" title="Bring forward (sewn later, on top)" aria-label="Move up">${ICON.up}</button>
        <button data-a="down" title="Send backward (sewn earlier, underneath)" aria-label="Move down">${ICON.down}</button>
        <button data-a="hide" title="${el.hidden ? "Show" : "Hide"}" aria-label="Toggle visibility">${el.hidden ? ICON.eyeoff : ICON.eye}</button>
        <button data-a="dup" title="Duplicate (Ctrl+D)" aria-label="Duplicate">${ICON.copy}</button>
        <button data-a="del" title="Remove (Delete)" aria-label="Remove">${ICON.trash}</button>
      </span>`;
    li.onclick = (e) => {
      const a = e.target.closest("button")?.dataset.a;
      if (a === "up") moveLayer(i, 1);
      else if (a === "down") moveLayer(i, -1);
      else if (a === "del") removeElement(i);
      else if (a === "dup") duplicate(i);
      else if (a === "hide") { commit(true); beginEdit(); el.hidden = !el.hidden; commit(true); renderLayers(); scheduleBuild(0); save_local(); }
      else if (e.shiftKey) { endSew(); toggleInSelection(i); }
      else { endSew(); clearMulti(); S.sel = i; renderLayers(); renderProps(); draw(); }
    };
    li.draggable = true;
    li.ondragstart = (e) => { dragLayer = i; e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(i)); li.classList.add("dragging"); };
    li.ondragend = () => { dragLayer = null; $$("#layerList li").forEach((n) => n.classList.remove("dragging", "drop-above", "drop-below")); };
    li.ondragover = (e) => {
      if (dragLayer === null) return;
      e.preventDefault();
      const above = e.offsetY < li.offsetHeight / 2;
      li.classList.toggle("drop-above", above); li.classList.toggle("drop-below", !above);
    };
    li.ondragleave = () => li.classList.remove("drop-above", "drop-below");
    li.ondrop = (e) => {
      e.preventDefault();
      const from = dragLayer, above = e.offsetY < li.offsetHeight / 2;
      dragLayer = null;
      if (from !== null) reorderLayer(from, above ? i + 1 : i);   // shown front-to-back, so "above" = a later index
    };
    ol.appendChild(li);
  }
}
let dragLayer = null;
// move element `from` to array slot `slot` (0..length, before the removal)
function reorderLayer(from, slot) {
  const els = S.layout.elements;
  const to = slot > from ? slot - 1 : slot;
  if (to === from) return;
  commit(true); beginEdit();
  const selId = els[S.sel]?.id;
  const [el] = els.splice(from, 1);
  els.splice(to, 0, el);
  if (selId) S.sel = els.findIndex((e) => e.id === selId);
  commit(true); renderLayers(); scheduleBuild(0); save_local();
}

// ------------------------------------------------------------------ properties panel
function field(label, inner, value = "") {
  return `<div class="field"><div class="lbl"><span>${label}</span>${value !== "" ? `<output>${value}</output>` : ""}</div>${inner}</div>`;
}
function rangeField(id, label, min, max, step, val, unit = "") {
  const len = unit === "len", d = len ? ' data-len="1"' : "";
  if (len) { label = U.lab(label); min = U.show(min); max = U.show(max); step = U.step(step); val = U.show(val); }
  return field(label, `<div class="row"><input type="range" id="${id}"${d} min="${min}" max="${max}" step="${step}" value="${val}">
    <input type="number" id="${id}-n"${d} min="${min}" max="${max}" step="${step}" value="${val}" style="max-width:74px"></div>`);
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
// ---- thread brand: the user's own spools (charts from Ink/Stitch); remembered in this browser
const MACHINE_BRAND = "Husqvarna Viking (machine colors)";
let threadBrand = (() => { try { return localStorage.getItem("thimble.brand") || MACHINE_BRAND; } catch (e) { return MACHINE_BRAND; } })();
async function setThreadBrand(brand) {
  threadBrand = brand || MACHINE_BRAND;
  try { localStorage.setItem("thimble.brand", threadBrand); } catch (e) {}
  if (!S.meta) return;
  if (!S.meta.machineThreads) S.meta.machineThreads = S.meta.threads;
  if (threadBrand === MACHINE_BRAND) S.meta.threads = S.meta.machineThreads;
  else {
    try { S.meta.threads = (await api("/api/threads/" + encodeURIComponent(threadBrand))).threads; }
    catch (e) { S.meta.threads = S.meta.machineThreads; threadBrand = MACHINE_BRAND; }
  }
  renderProps(); if (S.built) renderChart();
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
  const st = el.stretch_x || el.stretch_y;
  return (st ? `<div class="field stretch-note"><span>Stretched${el.stretch_x ? ` ${Math.round(el.stretch_x * 100)}% wide` : ""}${el.stretch_y ? ` ${Math.round(el.stretch_y * 100)}% tall` : ""}</span>
      <button type="button" class="btn small" id="p-unstretch">Undo stretch</button></div>` : "") +
    `<p class="hint">Drag a corner or side to stretch; hold Shift to keep the proportions.</p>` +
    `<div class="two">${field(U.lab("Across (mm)"), `<input type="number" id="p-x" step="${U.step(0.5)}" value="${U.show(el.x)}">`)}${field(U.lab("Down (mm)"), `<input type="number" id="p-y" step="${U.step(0.5)}" value="${U.show(el.y)}">`)}</div>
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
    <div id="p-ol-box"${ol ? "" : " hidden"}>${selectField("p-olfirst", "Sew it", [["first", "First, under the letters (nothing to snip)"], ["last", "Last, on top (snip jump threads)"], ["patch", "Patch look - border floats off the letters, fabric showing between"], ["only", "Edge only - sew over letters already stitched"]], ol?.only ? "only" : ol && (ol.gap_mm || 0) > 0.2 ? "patch" : ol && !ol.first ? "last" : "first")}${(ol?.gap_mm || 0) > 0.2 ? rangeField("p-olgap", "Gap to the letters (mm)", 0.3, 4, 0.1, ol.gap_mm, "len") : ""}${rangeField("p-olw", "Border width (mm)", 0.6, 5, 0.1, ol ? ol.width_mm : 1, "len")}
    <label class="radio"><input type="checkbox" id="p-olmatch"${ol?.match ? " checked" : ""}> Same thread as each letter</label>
    <div id="p-olc-box"${ol?.match ? " hidden" : ""}>${threadField("p-olc", "Border thread", ol ? ol.color : "#3a2c22")}</div>
    ${ol?.only ? `<p class="hint">Only the satin edge is sewn. Leave the fabric in the hoop (or re-hoop it exactly) and keep the piece where it was - the machine puts the design at the hoop center, so it lands on the old stitching.</p>` : ""}</div>`;
}

function renderProps() {
  const box = $("#props");
  // prune ids that no longer exist (undo, deletes)
  for (const id of [...multi]) if (!S.layout.elements.some((e) => e.id === id)) multi.delete(id);
  if (multi.size > 1) {
    const idx = selected();
    box.innerHTML = `<h2>${idx.length} pieces selected</h2>
      <p class="hint">Drag any of them to move them together; drag the box's corners to resize, the top knob to turn. Arrow keys nudge. Shift+click adds or removes a piece. Group them to keep them together.</p>
      <div class="align-grid" style="margin:10px 0">
        <button class="chip" data-gplace="cx">Center across</button><button class="chip" data-gplace="cy">Center down</button>
        <button class="chip" data-gplace="both">Center in hoop</button></div>
      <div class="row" style="gap:6px;margin-bottom:8px">${(() => {
        const gs = new Set(idx.map((i) => S.layout.elements[i].group || ""));
        const one = gs.size === 1 && !gs.has("");
        return (one ? "" : `<button class="btn small primary" id="g-group" title="Ctrl+G">Group</button>`) +
          ([...gs].some(Boolean) ? `<button class="btn small" id="g-ungroup" title="Ctrl+Shift+G">Ungroup</button>` : "");
      })()}<button class="btn small" id="g-dup" title="Ctrl+D">Duplicate</button>
        <button class="btn small" id="g-merge" title="Join drawings into one drawing, or pictures into one picture (Ctrl+M)">Merge into one</button></div>
      <div class="row" style="gap:6px"><button class="btn small" id="g-del">Delete ${idx.length} pieces</button>
        <button class="btn small ghost" id="g-clear">Clear selection</button></div>
      <ul class="multi-list">${idx.map((i) => `<li><span class="dot" style="background:${elColor(S.layout.elements[i])}"></span>${esc(elLabel(S.layout.elements[i]))}</li>`).join("")}</ul>`;
    $$("[data-gplace]").forEach((b) => (b.onclick = () => {
      const g = groupBox(idx); if (!g) return;
      const k = b.dataset.gplace, cx = (g.x0 + g.x1) / 2, cy = (g.y0 + g.y1) / 2;
      beginEdit(); moveSelected(k === "cy" ? 0 : -cx, k === "cx" ? 0 : -cy); commit(true);
      draw(); save_local(); scheduleBuild(300);
    }));
    $("#g-del").onclick = removeSelected;
    if ($("#g-group")) $("#g-group").onclick = groupSelected;
    if ($("#g-ungroup")) $("#g-ungroup").onclick = ungroupSelected;
    $("#g-dup").onclick = duplicateSelected;
    if ($("#g-merge")) $("#g-merge").onclick = mergeSelected;
    $("#g-clear").onclick = () => { clearMulti(); S.sel = -1; renderLayers(); renderProps(); draw(); };
    return;
  }
  const el = S.layout.elements[S.sel];
  if (!el) {
    const [W, H] = S.layout.hoop;
    box.innerHTML = `<h2>Your hoop</h2><div class="props-empty">
      <p class="hand">Start with some words, a shape, or a picture.</p>
      <p>Sewing field: <b>${U.show(W)} × ${U.show(H)} ${U.u}</b>. Fabric: <b>${esc(S.meta?.fabrics?.[S.layout.fabric] || "")}</b>.</p>
      <p class="muted small">Tips: drag things in the hoop to place them; drag the corner dot to resize and the top dot to turn.
      Letters look best at <b>${U.len(6)} or taller</b>. Click <b>Sew it out</b> to watch the stitch order.</p>
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
      section("size", "Font, size &amp; shape", `${esc(String(el.font || "").replace("✦ ", ""))} · ${U.len(el.height_mm)}${el.arc ? " · curved" : ""}`,
        field("Font", `<div class="fontpick" id="fontpick"></div>`) +
        rangeField("p-height_mm", "Letter height (capitals, mm)", 3, 80, 0.5, el.height_mm, "len") +
        rangeField("p-letter_spacing", "Letter spacing", -0.1, 0.5, 0.01, el.letter_spacing || 0) +
        rangeField("p-arc", "Curve (− smile / + arch)", -180, 180, 1, el.arc || 0) +
        (multi ? rangeField("p-line_spacing", "Line spacing", 0.8, 2, 0.05, el.line_spacing || 1.25) +
          field("Align lines", `<div class="seg3">${[["left", "Left"], ["center", "Center"], ["right", "Right"]].map(([v, t]) =>
            `<button class="chip${(el.align || "center") === v ? " on" : ""}" data-align="${v}">${t}</button>`).join("")}</div>`) : "")) +
      section("color", "Thread &amp; colors", `<span class="dot" style="background:${el.color}"></span>${esc(threadName(el.color))}${(el.letter_colors || []).some(Boolean) ? " + letters" : ""}`,
        threadField("p-color", "Thread", el.color) + letterColorFields(el) +
        (String(el.font || "").startsWith("✦") ? "" : selectField("p-style", "Stitch", TEXT_STYLES, el.style || "auto") +
          (el.style === "satin" && el.height_mm >= 25 ? `<p class="hint">Long satin sweeps (up to 12 mm) on big letters: use firm stabilizer. Sewing them over letters already filled works great - the old stitching holds them flat.</p>` : ""))) +
      section("border", "Border", el.outline ? `${U.len(el.outline.width_mm)} · ${el.outline.only ? "edge only" : "sewn " + (el.outline.first ? "first" : "last")}` : "none", outlineFields(el)) +
      section("pos", "Position", `${U.show(el.x)}, ${U.show(el.y)} ${U.u}${el.rotation ? ` · ${el.rotation}°` : ""}${el.stretch_x || el.stretch_y ? " · stretched" : ""}`, posFields(el));
  } else if (el.type === "shape") {
    if (el.kind === "tack") {
      h += `<h2>Tack line</h2><p class="hint">One plain line of running stitch - no embroidery. Good for tacking fabric, batting or a patch down before the real stitching. Drag it, turn it with the top knob, stretch it with the side handles.</p>` + field("Kind", `<div class="chips">${SHAPES.map(([k, t]) => `<button class="chip${k === el.kind ? " on" : ""}" data-kind="${k}">${t}</button>`).join("")}</div>`) +
        field(U.lab("Length (mm)"), `<input type="number" id="p-width_mm" step="${U.step(1)}" value="${U.show(el.width_mm)}">`) +
        rangeField("p-stitch_mm", "Stitch length (mm)", 1, 8, 0.5, el.stitch_mm || 3, "len") +
        threadField("p-color", "Thread", el.color) +
        section("pos", "Position", `${U.show(el.x)}, ${U.show(el.y)} ${U.u}${el.rotation ? ` · ${el.rotation}°` : ""}`, posFields(el));
    } else
    h += `<h2>Shape</h2>` + field("Kind", `<div class="chips">${SHAPES.map(([k, t]) => `<button class="chip${k === el.kind ? " on" : ""}" data-kind="${k}">${t}</button>`).join("")}</div>`) +
      `<div class="two">${field(U.lab("Width (mm)"), `<input type="number" id="p-width_mm" step="${U.step(0.5)}" value="${U.show(el.width_mm)}">`)}${field(U.lab("Height (mm)"), `<input type="number" id="p-height_mm" step="${U.step(0.5)}" value="${U.show(el.height_mm)}">`)}</div>` +
      (["frame", "double_frame", "offset_frame", "ring", "line"].includes(el.kind) ? rangeField("p-stroke_mm", "Line thickness (mm)", 0.6, 8, 0.1, el.stroke_mm || 1.2, "len") : "") +
      section("color", "Thread &amp; stitch", `<span class="dot" style="background:${el.color}"></span>${esc(threadName(el.color))}`,
        threadField("p-color", "Thread", el.color) + selectField("p-style", "Stitch", STYLES, el.style || "auto")) +
      section("border", "Border", el.outline ? U.len(el.outline.width_mm) : "none", outlineFields(el)) +
      section("pos", "Position", `${U.show(el.x)}, ${U.show(el.y)} ${U.u}${el.rotation ? ` · ${el.rotation}°` : ""}${el.stretch_x || el.stretch_y ? " · stretched" : ""}`, posFields(el));
  } else if (el.type === "stitches") {
    const k = el.width_mm / (el.orig_w || el.width_mm);
    h += `<h2>Stitch file</h2>` + rangeField("p-width_mm", "Width (mm)", Math.max(3, el.orig_w * 0.5), el.orig_w * 1.5, 0.5, el.width_mm, "len") +
      `<p class="hint">${Math.abs(k - 1) > 0.1 ? `<b>Resized to ${Math.round(k * 100)}%.</b> Stitch files look best within about 10% of their own size. ` : ""}These are the file's own stitches, so they sew exactly as they were digitized.</p>` +
      (Math.abs(k - 1) > 0.001 ? `<button type="button" class="btn small" id="p-resetsize">Back to original size</button>` : "") +
      `<div class="field recreate-card"><div class="lbl"><span>Want to resize or edit it freely?</span></div>
        <button type="button" class="btn small primary" id="p-recreate-ai">Recreate with AI (editable)</button>
        <button type="button" class="btn small" id="p-recreate-trace">Trace into an editable picture</button>
        <p class="hint">The AI reads it like a picture and rebuilds real text, shapes and drawings in its place.
        Tracing is free and needs no AI - fine for simple shapes; lettering comes out better with the AI.</p></div>` +
      field("Threads", `<div class="vcolors">${(el.blocks || []).map((b, i) => `<label class="vcolor"><input type="color" data-sc="${i}" value="${b.color}"><span>${i + 1}. ${esc(threadName(b.color))}</span></label>`).join("")}</div>`) +
      section("pos", "Position", `${U.show(el.x)}, ${U.show(el.y)} ${U.u}${el.rotation ? ` · ${el.rotation}°` : ""}${el.stretch_x || el.stretch_y ? " · stretched" : ""}`, posFields(el));
  } else if (el.type === "vector") {
    const cols = [...new Set((el.parts || []).map((p) => p.color.toLowerCase()))];
    h += `<h2>Drawing</h2>` + rangeField("p-width_mm", "Width (mm)", 5, Math.max(...S.layout.hoop), 0.5, el.width_mm, "len") +
      `<p class="hint">Clean shapes and lines (${(el.parts || []).length} parts, ${(el.parts || []).reduce((n, p) => n + (p.points || []).length, 0)} points), so it stitches smoothly.</p>` +
      `<button type="button" class="btn small ${S.nodeEdit === el.id ? "primary" : ""}" id="p-nodes">${S.nodeEdit === el.id ? "Done editing points" : "Edit points"}</button>` +
      (S.nodeEdit === el.id ? `<p class="hint"><b>Drag</b> a dot to move it. <b>Click</b> a dot and press <b>Delete</b> to remove it. <b>Double-click</b> a line to add a dot. <b>Esc</b> when you're done.</p>` : "") +
      `<button type="button" class="btn small" id="p-split" title="Each object (the lights, the mountains) becomes its own layer; press again to split by color, then into single lines and shapes">Split into pieces</button>` +
      `<label class="radio"><input type="checkbox" id="p-eyes"${el.eyes === false ? "" : " checked"}> Sew small holes (eyes) as solid dots</label>` +
      field("Threads", `<div class="vcolors">${cols.map((c) => `<label class="vcolor"><input type="color" data-vc="${c}" value="${c}"><span>${esc(threadName(c))}</span></label>`).join("")}</div>`) +
      section("pos", "Position", `${U.show(el.x)}, ${U.show(el.y)} ${U.u}${el.rotation ? ` · ${el.rotation}°` : ""}${el.stretch_x || el.stretch_y ? " · stretched" : ""}`, posFields(el));
  } else {
    const cols = el.colors || [];
    h += `<h2>Picture</h2>` + rangeField("p-width_mm", "Width (mm)", 10, Math.max(...S.layout.hoop), 0.5, el.width_mm, "len") +
      `<p class="hint">About ${Math.round(el.width_mm * (el.aspect || 1))} mm tall.</p>` +
      rangeField("p-merge_colors", "Merge similar colors", 0, 150, 5, el.merge_colors || 0) +
      `<p class="hint">Folds shading and near-identical colors into the biggest one nearby. 0 = keep every color.</p>` +
      rangeField("p-smooth", "Smooth the shapes", 0, 4, 0.1, el.smooth || 0) +
      `<p class="hint">Rounds off lumpy, jagged edges before stitching. 0 = exactly as in the picture.</p>` +
      field("Stitch as", `<div class="seg3">${[["", "Filled"], ["trace", "Traced lines"]].map(([v, t]) =>
        `<button class="chip${(el.trace ? "trace" : "") === v ? " on" : ""}" data-trace="${v}">${t}</button>`).join("")}</div>`) +
      (el.trace ? selectField("p-trace_line", "Line", [["run", "Running stitch - fine, hand-drawn look"], ["satin", "Satin line - bold and shiny"]], el.trace_line || "run") +
        (el.trace_line === "satin"
          ? rangeField("p-trace_width", "Line thickness (mm)", 0.6, 4, 0.1, el.trace_width || 1.2, "len")
          : selectField("p-trace_repeat", "Line weight", [["1", "Single - light"], ["3", "Triple (bean stitch) - bolder"]], String(el.trace_repeat || 1))) +
        rangeField("p-trace_min", "Leave out lines shorter than (mm)", 0, 20, 0.5, el.trace_min ?? 3, "len") +
        `<button type="button" class="btn small primary" id="p-tolines" title="Turn the trace into lines with dots you can drag, add and delete">Make lines editable</button>` +
        `<p class="hint">Only the edges between colors are sewn - each edge once, in the darker thread - and thin lines are sewn down their middle. Raise "leave out" to drop fur, hatching and other small strokes; untick a color to leave its edges out.</p>` : "") +
      (el.trace ? "" : `<label class="radio"><input type="checkbox" id="p-eyes"${el.eyes === false ? "" : " checked"}> Sew small holes (eyes) as solid dots</label>`) +
      `<button type="button" class="btn small" id="p-split" title="Each separate object (each star, the ghost...) becomes its own picture; press again on a one-object picture to split it by color">Split into pieces</button>` +
      field("Colors <small>(untick to skip)</small>", `<div class="imgcolors">${cols.map((c, i) => `
        <div class="imgcolor"><span class="sw" style="background:${c.hex}" title="In the picture"></span>
          <label class="radio"><input type="checkbox" data-ci="${i}" data-k="keep"${c.keep ? " checked" : ""}> ${Math.round(c.share * 100)}%</label>
          <input type="color" data-ci="${i}" data-k="thread" value="${c.thread}" title="Thread color">
          ${el.trace ? "" : `<select data-ci="${i}" data-k="style">${STYLES.map(([v, t]) => `<option value="${v}"${v === (c.style || "auto") ? " selected" : ""}>${t.split(" —")[0].replace(" (recommended)", "")}</option>`).join("")}</select>`}
        </div>`).join("")}</div>`) +
      `<p class="hint">Flat artwork (logos, clip art, lettering) stitches best. Photos won't.</p>` +
      section("pos", "Position", `${U.show(el.x)}, ${U.show(el.y)} ${U.u}${el.rotation ? ` · ${el.rotation}°` : ""}${el.stretch_x || el.stretch_y ? " · stretched" : ""}`, posFields(el));
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
  const set = (v) => {
    v = parseFloat(v); if (isNaN(v)) return; r.value = v; n.value = v;
    setProp(target, key, r.dataset.len ? Math.round(U.toMm(v) * 100) / 100 : v, rebuild);
  };
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
  ["height_mm", "letter_spacing", "arc", "line_spacing", "stroke_mm", "stitch_mm"].forEach((k) => wireRange(el, k));
  wireRange(el, "rotation", false);
  if (el.type === "image" || el.type === "vector" || el.type === "stitches") wireRange(el, "width_mm");
  $$("[data-sc]").forEach((inp) => (inp.oninput = () => {
    beginEdit(); el.blocks[+inp.dataset.sc].color = inp.value; commit(); scheduleBuild(); save_local(); renderLayers();
  }));
  const rs = $("#p-resetsize"); if (rs) rs.onclick = () => { setProp(el, "width_mm", el.orig_w); renderProps(); };
  const ra = $("#p-recreate-ai"); if (ra) ra.onclick = () => recreateStitches(el, true);
  const rt = $("#p-recreate-trace"); if (rt) rt.onclick = () => recreateStitches(el, false);
  const split = $("#p-split");
  if (split) split.onclick = async () => {
    try {
      const pic = el.type === "image";
      const res = await api(pic ? "/api/split-picture" : "/api/split-drawing", el);
      if (res.elements.length < 2) { toast(pic ? "Everything in this picture touches - it's already one piece." : "This drawing is already a single piece in one color."); return; }
      if (pic) res.elements.forEach((e) => { S.images[e.image_id] = { image_id: e.image_id, aspect: e.aspect, colors: e.colors }; keepPicture(e.image_id); });
      beginEdit();
      const i = S.layout.elements.indexOf(el);
      res.elements.forEach((e) => (e.id = uid()));
      S.layout.elements.splice(i, 1, ...res.elements);
      // a split drawing that came from a picture read: keep refine working on its pieces
      const r = S.layout.ai_read;
      if (r && r.ids.includes(el.id)) r.ids = r.ids.filter((x) => x !== el.id).concat(res.elements.map((e) => e.id));
      commit(true);
      S.sel = i; renderLayers(); renderProps(); scheduleBuild(0); save_local();
      toast(res.how === "colors" ? `Split by color into ${res.elements.length} pieces. Press Split again on one to separate its parts.`
        : `Split into ${res.elements.length} pieces - each is its own layer now.`, "good");
    } catch (e) { toast(esc(e.message), "bad"); }
  };
  $$("[data-vc]").forEach((inp) => (inp.oninput = () => {
    const old = inp.dataset.vc;
    beginEdit(); (el.parts || []).forEach((p) => { if (p.color.toLowerCase() === old) p.color = inp.value; }); commit();
    inp.dataset.vc = inp.value.toLowerCase(); scheduleBuild(); save_local(); renderLayers();
  }));
  ["width_mm", "height_mm"].forEach((k) => { const i = $("#p-" + k); if (i && i.type === "number" && el.type === "shape") i.onchange = () => setProp(el, k, clamp(Math.round(U.toMm(parseFloat(i.value) || 0) * 10) / 10 || 10, 2, 360)); });
  const st = $("#p-style"); if (st) st.onchange = () => setProp(el, "style", st.value);
  $$("[data-trace]").forEach((b) => (b.onclick = () => {
    beginEdit();
    if (b.dataset.trace) { el.trace = true; el.trace_line = el.trace_line || "run"; } else { delete el.trace; }
    commit(); scheduleBuild(); save_local(); renderProps(); renderLayers();
  }));
  const us = $("#p-unstretch");
  if (us) us.onclick = () => { beginEdit(); delete el.stretch_x; delete el.stretch_y; commit(); scheduleBuild(0); save_local(); renderProps(); };
  const nb = $("#p-nodes");
  if (nb) nb.onclick = () => { S.nodeEdit = S.nodeEdit === el.id ? null : el.id; S.nodeSel = null; renderProps(); draw(); };
  const tlb = $("#p-tolines");
  if (tlb) tlb.onclick = async () => {
    busy("now", "Turning the trace into lines…");
    try {
      const r = await api("/api/trace-to-lines", el);
      const i = S.layout.elements.indexOf(el);
      commit(true); beginEdit();
      r.element.id = uid();
      S.layout.elements.splice(i, 1, r.element);
      commit(true);
      S.sel = i; S.nodeEdit = r.element.id; S.nodeSel = null;
      renderLayers(); renderProps(); scheduleBuild(0); save_local();
      toast(`Now ${r.element.parts.length} editable lines - drag the dots. Undo brings the picture back.`, "good");
    } catch (e) { toast(esc(e.message), "bad"); } finally { busy(false); }
  };
  const tl = $("#p-trace_line"); if (tl) tl.onchange = () => { setProp(el, "trace_line", tl.value); renderProps(); };
  const tr = $("#p-trace_repeat"); if (tr) tr.onchange = () => setProp(el, "trace_repeat", +tr.value);
  if ($("#p-trace_width")) wireRange(el, "trace_width");
  if ($("#p-trace_min")) wireRange(el, "trace_min");
  if ($("#p-smooth")) wireRange(el, "smooth");
  if ($("#p-merge_colors")) wireRange(el, "merge_colors");
  const ey = $("#p-eyes"); if (ey) ey.onchange = () => setProp(el, "eyes", ey.checked);
  const al = $("#p-align"); if (al) al.onchange = () => setProp(el, "align", al.value);
  wireThread("p-color", () => el.color, (hex) => { setProp(el, "color", hex); if ($("#p-letters")) $("#p-letters").innerHTML = letterChips(el); });
  wireLetterColors(el);
  const ol = $("#p-ol");
  if (ol) {
    ol.onchange = () => {
      const how = $("#p-olfirst").value;
      beginEdit(); el.outline = ol.checked ? { color: $("#p-olc").value || "#3a2c22", width_mm: Math.round(U.toMm(parseFloat($("#p-olw").value)) * 100) / 100 || 1, first: how === "first", only: how === "only", match: $("#p-olmatch").checked, ...(how === "patch" ? { gap_mm: 0.8 } : {}) } : null; commit();
      $("#p-ol-box").hidden = !ol.checked; scheduleBuild(); save_local();
    };
    $("#p-olfirst").onchange = () => {
      if (!el.outline) return;
      const how = $("#p-olfirst").value;
      beginEdit(); el.outline.first = how === "first"; el.outline.only = how === "only";
      if (how === "patch") el.outline.gap_mm = el.outline.gap_mm > 0.2 ? el.outline.gap_mm : 0.8; else delete el.outline.gap_mm;
      if (how === "only" && el.outline.width_mm < 2.5) el.outline.width_mm = 3;  // wide enough to hide the fill's edge
      commit(); scheduleBuild(); save_local(); renderProps();
    };
    $("#p-olmatch").onchange = () => {
      if (!el.outline) return;
      beginEdit(); el.outline.match = $("#p-olmatch").checked; commit();
      $("#p-olc-box").hidden = el.outline.match; scheduleBuild(); save_local();
    };
    const w = $("#p-olw"), wn = $("#p-olw-n");
    const setw = (v) => { w.value = v; wn.value = v; if (el.outline) { beginEdit(); el.outline.width_mm = Math.round(U.toMm(parseFloat(v)) * 100) / 100; commit(); scheduleBuild(); save_local(); } };
    w.oninput = () => setw(w.value); wn.onchange = () => setw(wn.value);
    const gp = $("#p-olgap"), gpn = $("#p-olgap-n");
    if (gp) {
      const setg = (v) => { gp.value = v; gpn.value = v; if (el.outline) { beginEdit(); el.outline.gap_mm = Math.round(U.toMm(parseFloat(v)) * 100) / 100; commit(); scheduleBuild(); save_local(); } };
      gp.oninput = () => setg(gp.value); gpn.onchange = () => setg(gpn.value);
    }
    wireThread("p-olc", () => el.outline?.color, (hex) => { if (el.outline) { beginEdit(); el.outline.color = hex; commit(); scheduleBuild(); save_local(); } });
  }
  $$(".chip[data-kind]").forEach((b) => (b.onclick = () => { setProp(el, "kind", b.dataset.kind); renderProps(); renderLayers(); }));
  $$("[data-ci]").forEach((inp) => {
    const i = +inp.dataset.ci, k = inp.dataset.k;
    const ev = inp.type === "checkbox" || inp.tagName === "SELECT" ? "onchange" : "oninput";
    inp[ev] = () => { beginEdit(); el.colors[i][k] = inp.type === "checkbox" ? inp.checked : inp.value; commit(); scheduleBuild(); save_local(); renderLayers(); };
  });
  const px = $("#p-x"), py = $("#p-y");
  if (px) {
    px.onchange = () => setProp(el, "x", Math.round(U.toMm(parseFloat(px.value) || 0) * 10) / 10, false);
    py.onchange = () => setProp(el, "y", Math.round(U.toMm(parseFloat(py.value) || 0) * 10) / 10, false);
  }
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
    px.value = U.show(el.x); py.value = U.show(el.y); scheduleBuild();
  }));
  if (el.type === "text") fontPicker($("#fontpick"), el.font,
    (f) => { clearTimeout(fontPrevTimer); fontPrevSeq++; S.fontPrev = null; setProp(el, "font", f); },
    (f) => previewFont(el, f));
}

// ------------------------------------------------------------------ font picker
const CATS = ["All", "Pro digitized", "Block", "Script", "Serif", "Varsity", "Blackletter", "Rounded", "Display", "Handwritten", "Western"];
// hover a font in the picker: stitch a throw-away copy of the design with that font and show it;
// leaving puts the real stitches back (nothing is changed until a font is clicked)
let fontPrevSeq = 0, fontPrevTimer = null;
function previewFont(el, font) {
  clearTimeout(fontPrevTimer);
  const seq = ++fontPrevSeq;
  if (!font) {
    if (S.fontPrev) { S.built = S.fontPrev.built; S.fontPrev = null; draw(); }
    return;
  }
  fontPrevTimer = setTimeout(async () => {
    const idx = S.layout.elements.indexOf(el);
    if (idx < 0) return;
    const lay = JSON.parse(snapshot());
    lay.elements[idx].font = font;
    try {
      const r = await fetch("/api/build", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(lay) });
      const res = await r.json();
      if (seq !== fontPrevSeq || !r.ok || res.error) return; // moved on to another font, or it failed
      if (!S.fontPrev) S.fontPrev = { built: S.built };
      S.built = res; draw();
    } catch (e) { /* a preview that fails just doesn't show */ }
  }, 140);
}

function fontPicker(host, current, onPick, onHover = () => {}) {
  const prev = (n) => `/api/font-preview/${encodeURIComponent(n)}`;
  host.innerHTML = `<button type="button" class="btn fontpick-btn"><img alt="${esc(current)}" src="${prev(current)}"><span>▾</span></button>`;
  const btn = host.firstElementChild;
  let cat = "All";
  btn.onclick = (e) => {
    e.stopPropagation();
    if (host.querySelector(".fontpick-pop")) { host.querySelector(".fontpick-pop").remove(); onHover(null); return; }
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
    let hovered = null;
    list.onmouseover = (ev) => {
      const it = ev.target.closest("[data-f]");
      if (it && it.dataset.f !== hovered) { hovered = it.dataset.f; onHover(hovered); }
    };
    list.onmouseleave = () => { hovered = null; onHover(null); };
    pop.onclick = (ev) => {
      ev.stopPropagation();
      const c = ev.target.closest("[data-c]");
      if (c) { cat = c.dataset.c; $$(".fontpick-cats .chip", pop).forEach((b) => b.classList.toggle("on", b.dataset.c === cat)); fill(); return; }
      const it = ev.target.closest("[data-f]");
      if (it) { current = it.dataset.f; btn.querySelector("img").src = prev(current); pop.remove(); onPick(current); }
    };
    const close = (ev) => { if (!host.contains(ev.target)) { pop.remove(); onHover(null); document.removeEventListener("click", close); } };
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
    (el.blocks || []).forEach((b) => (b.color = f(b.color)));
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
  $("#stats").innerHTML = `<span>Stitches</span><b>${fmt(st.stitches)}</b><span>Size</span><b>${U.show(st.size[0])} × ${U.show(st.size[1])} ${U.u}</b>
    <span>Sewing time</span><b>≈ ${Math.max(1, Math.round(st.minutes))} min</b>
    <span>Thread</span><b title="Top thread; bobbin ≈ ${U.thread(st.bobbin_m || 0)}">≈ ${U.thread(st.thread_m || 0)} <small class="muted">+ ${U.thread(st.bobbin_m || 0)} bobbin</small></b><span>Thread changes</span><b>${Math.max(0, st.colors.length - 1)}</b>`;
  const seenAt = {};
  $("#spools").innerHTML = st.colors.map((c, i) => { const hx = c.hex.toLowerCase(), prior = seenAt[hx]; seenAt[hx] = prior || i + 1; return `<div class="spool${threadSel.has(hx) ? " on" : ""}" data-hex="${hx}" title="${prior ? `Same thread as spool ${prior}, sewn again because a piece sewn in between overlaps it. To sew it in one go, put these pieces next to each other in the Layers list.` : "Click to pick for merging"} · ${esc(c.kinds.join(", "))}">${spoolSVG(c.hex)}
    <div class="t"><b>${i + 1}. ${esc(threadBrand === MACHINE_BRAND ? c.thread : threadBrand.replace(/ (Rayon|Polyester|Embroidery)$/, "") + " " + threadName(c.hex).replace(/^≈ /, "≈ "))}</b><span class="n">${prior ? `again (same as ${prior}) · ` : ""}${fmt(c.stitches)} stitches · ≈ ${U.thread(c.thread_m)}</span></div></div>`; }).join("") ||
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
  // server messages are written in mm; show them in the chosen units
  const inUnits = (w) => (U.inch ? w.replace(/(\d+(?:\.\d+)?)(?:\s?[x×]\s?(\d+(?:\.\d+)?))?\s?mm\b/g,
    (m, a, b) => (b ? `${U.show(+a)} × ${U.show(+b)} in` : U.len(+a))) : w);
  const probs = st.problems || [];
  $("#notes").innerHTML = (st.warnings || []).map(inUnits).slice(0, 3).map((w) => `<div class="note">${esc(w)}</div>`).join("") +
    probs.slice(0, 4).map((p, i) => `<div class="note problem ${p.level}" data-prob="${i}" title="Click to see where"><b>${p.level === "warn" ? "⚠" : "ℹ"}</b> ${esc(inUnits(p.msg))}</div>`).join("");
  $$("#notes .note").forEach((n) => (n.title = n.title || n.textContent));
  $$("#notes [data-prob]").forEach((n) => (n.onclick = () => { n.classList.toggle("open"); S.probFocus = probs[+n.dataset.prob]; draw(); setTimeout(() => { S.probFocus = null; draw(); }, 2500); }));
}

// ------------------------------------------------------------------ canvas
const cv = $("#canvas"), ctx = cv.getContext("2d");
let view = { s: 5, ox: 0, oy: 0 }; // px per mm, hoop-centre in px
let weave = null;

function makeWeave(color) {
  // fabric texture: soft irregular fibres and slubs, no straight lines (straight lines read as graph paper)
  const N = 160, c = document.createElement("canvas"); c.width = c.height = N;
  const g = c.getContext("2d");
  g.fillStyle = color; g.fillRect(0, 0, N, N);
  const lum = parseInt(color.slice(1, 3), 16) + parseInt(color.slice(3, 5), 16) + parseInt(color.slice(5, 7), 16);
  const ink = lum > 380 ? "0,0,0" : "255,255,255", hi = lum > 380 ? "255,255,255" : "0,0,0";
  let seed = 3; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  g.lineCap = "round";
  for (let i = 0; i < 900; i++) {
    const x = rnd() * N, y = rnd() * N, len = 3 + rnd() * 9, horiz = rnd() < 0.55;
    g.strokeStyle = `rgba(${rnd() < 0.6 ? ink : hi},${0.025 + rnd() * 0.04})`;
    g.lineWidth = 0.8 + rnd() * 0.9;
    g.beginPath(); g.moveTo(x, y);
    if (horiz) g.lineTo(x + len, y + (rnd() - 0.5) * 1.2); else g.lineTo(x + (rnd() - 0.5) * 1.2, y + len);
    g.stroke();
    // wrap the edges so the tile repeats without seams
    if (x + len > N || y + len > N) { g.save(); g.translate(x + len > N ? -N : 0, y + len > N ? -N : 0); g.stroke(); g.restore(); }
  }
  return ctx.createPattern(c, "repeat");
}

function fitView() {
  const r = cv.getBoundingClientRect();
  const [W, H] = S.layout.hoop;
  const frame = 22; // mm of hoop around the field
  const top = 52;   // px kept clear for the zoom/sew buttons floating over the top
  view.s = Math.min(r.width / (W + frame * 2), (r.height - top) / (H + frame * 2)) * S.zoom;
  view.ox = r.width / 2 + S.pan.x;
  view.oy = top + (r.height - top) / 2 + S.pan.y;
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

function roundRect(g, x, y, w, h, r, add = false) {
  // add: append to the current path (for cut-outs) instead of starting a new one
  if (!add) g.beginPath();
  g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}

function drawHoop() {
  const r = cv.getBoundingClientRect();
  ctx.clearRect(0, 0, r.width, r.height);
  const [W, H] = S.layout.hoop, s = view.s;
  const pad = 9, ring = 7, inner = 2.2; // mm: fabric beyond the field, wooden ring, inner ring showing
  const [x0, y0] = mm2px(-W / 2 - pad, -H / 2 - pad);
  const fw = (W + pad * 2) * s, fh = (H + pad * 2) * s, rr = 16 * s;
  const ox0 = x0 - ring * s, oy0 = y0 - ring * s, ow = fw + ring * 2 * s, oh = fh + ring * 2 * s, orr = rr + ring * s;
  let seed = 7; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647); // same grain every redraw
  // shadow on the table + the wooden outer ring
  ctx.save();
  ctx.shadowColor = "rgba(60,35,15,.38)"; ctx.shadowBlur = 26; ctx.shadowOffsetY = 10;
  roundRect(ctx, ox0, oy0, ow, oh, orr);
  const wood = ctx.createLinearGradient(ox0, oy0, ox0 + ow * 0.25, oy0 + oh);
  wood.addColorStop(0, "#c08a56"); wood.addColorStop(0.45, "#a46c3d"); wood.addColorStop(1, "#7c4b27");
  ctx.fillStyle = wood; ctx.fill();
  ctx.restore();
  // wood grain: long wavy fibres with a few darker growth lines and knots
  ctx.save();
  roundRect(ctx, ox0, oy0, ow, oh, orr); ctx.clip();
  for (let i = 0; i < 70; i++) {
    const yy = oy0 + rnd() * oh, amp = (2 + rnd() * 6) * Math.max(1, s / 4), dark = rnd() < 0.18;
    ctx.strokeStyle = dark ? `rgba(70,35,12,${0.14 + rnd() * 0.12})` : `rgba(${rnd() < 0.5 ? "60,30,10" : "255,225,180"},${0.05 + rnd() * 0.07})`;
    ctx.lineWidth = dark ? 1.2 : 0.8;
    ctx.beginPath(); ctx.moveTo(ox0, yy);
    ctx.bezierCurveTo(ox0 + ow * 0.3, yy - amp, ox0 + ow * 0.65, yy + amp, ox0 + ow, yy + (rnd() - 0.5) * amp);
    ctx.stroke();
  }
  ctx.restore();
  // bevel: light catching the outer top edge, darker underside
  ctx.save();
  roundRect(ctx, ox0 + 1, oy0 + 1, ow - 2, oh - 2, orr - 1);
  const bev = ctx.createLinearGradient(0, oy0, 0, oy0 + oh);
  bev.addColorStop(0, "rgba(255,236,205,.55)"); bev.addColorStop(0.5, "rgba(255,236,205,.08)"); bev.addColorStop(1, "rgba(40,18,5,.35)");
  ctx.strokeStyle = bev; ctx.lineWidth = 1.6; ctx.stroke();
  ctx.restore();
  // the inner ring peeking out inside the outer one (a real hoop is two rings)
  const ix0 = x0 - inner * s, iy0 = y0 - inner * s, iw = fw + inner * 2 * s, ih = fh + inner * 2 * s, irr = rr + inner * s;
  ctx.save();
  roundRect(ctx, ix0, iy0, iw, ih, irr);
  const ir = ctx.createLinearGradient(0, iy0, 0, iy0 + ih);
  ir.addColorStop(0, "#8d5a31"); ir.addColorStop(1, "#b07a47");
  ctx.fillStyle = ir; ctx.fill();
  ctx.strokeStyle = "rgba(45,20,6,.45)"; ctx.lineWidth = 1; ctx.stroke();
  ctx.restore();
  // fabric, pulled taut
  if (!weave || weave._c !== S.layout.fabric_color) { weave = makeWeave(S.layout.fabric_color || "#d9d5cc"); weave._c = S.layout.fabric_color; }
  roundRect(ctx, x0, y0, fw, fh, rr);
  ctx.fillStyle = weave; ctx.fill();
  ctx.save(); ctx.clip();
  const sheen = ctx.createRadialGradient(view.ox - fw * 0.15, view.oy - fh * 0.2, Math.min(fw, fh) * 0.1, view.ox, view.oy, Math.max(fw, fh) * 0.78);
  sheen.addColorStop(0, "rgba(255,255,255,.05)"); sheen.addColorStop(0.6, "rgba(0,0,0,0)"); sheen.addColorStop(1, "rgba(40,20,5,.2)");
  ctx.fillStyle = sheen; ctx.fillRect(x0, y0, fw, fh);
  // the ring's shadow falling onto the fabric along its inside edge
  ctx.shadowColor = "rgba(25,12,3,.55)"; ctx.shadowBlur = Math.max(6, 2.2 * s); ctx.shadowOffsetY = Math.max(1.5, 0.5 * s);
  ctx.beginPath(); ctx.rect(x0 - 60, y0 - 60, fw + 120, fh + 120); roundRect(ctx, x0, y0, fw, fh, rr, true);
  ctx.fillStyle = "rgba(0,0,0,1)"; ctx.fill("evenodd");
  ctx.restore();
  // brass clamp with its tightening screw
  const [cx, cy] = mm2px(0, -H / 2 - pad - ring);
  const bw = 17 * s, bh = 5.4 * s;
  ctx.save();
  ctx.shadowColor = "rgba(40,20,5,.45)"; ctx.shadowBlur = 5; ctx.shadowOffsetY = 2;
  const brass = ctx.createLinearGradient(cx, cy - bh, cx, cy + bh);
  brass.addColorStop(0, "#f3dc98"); brass.addColorStop(0.45, "#cfa94e"); brass.addColorStop(1, "#8a6a22");
  ctx.fillStyle = brass; roundRect(ctx, cx - bw / 2, cy - bh * 0.8, bw, bh * 1.6, 2 * s); ctx.fill();
  ctx.restore();
  ctx.strokeStyle = "rgba(90,65,15,.6)"; ctx.lineWidth = 1; roundRect(ctx, cx - bw / 2, cy - bh * 0.8, bw, bh * 1.6, 2 * s); ctx.stroke();
  ctx.strokeStyle = "rgba(255,245,210,.6)"; ctx.beginPath(); ctx.moveTo(cx - bw / 2 + 2 * s, cy - bh * 0.8 + 1); ctx.lineTo(cx + bw / 2 - 2 * s, cy - bh * 0.8 + 1); ctx.stroke();
  const sr = 2.1 * s, screw = ctx.createRadialGradient(cx - sr * 0.4, cy - sr * 0.4, sr * 0.1, cx, cy, sr);
  screw.addColorStop(0, "#fff2c4"); screw.addColorStop(1, "#7d6122");
  ctx.fillStyle = screw; ctx.beginPath(); ctx.arc(cx, cy, sr, 0, 7); ctx.fill();
  ctx.strokeStyle = "rgba(60,40,8,.8)"; ctx.lineWidth = Math.max(1, 0.35 * s);
  ctx.beginPath(); ctx.moveTo(cx - sr * 0.7, cy + sr * 0.25); ctx.lineTo(cx + sr * 0.7, cy - sr * 0.25); ctx.stroke();
  // measuring grid (like a slicer's build plate)
  {
    const stepMm = U.inch ? (view.s < 2.2 ? 50.8 : 25.4) : (view.s < 2.2 ? 50 : 20), g0 = px2mm(x0, y0), g1 = px2mm(x0 + fw, y0 + fh);
    const majEvery = U.inch ? 2 : (view.s < 2.2 ? 2 : 5); // strong line every 2 in / 100 mm (plus the centre lines)
    const gx0 = g0[0], gy0 = g0[1], gx1 = g1[0], gy1 = g1[1];
    ctx.save();
    roundRect(ctx, x0, y0, fw, fh, rr); ctx.clip(); // stay on the fabric
    ctx.lineWidth = 1;
    const fc = S.layout.fabric_color || "#d9d5cc";
    const lum = parseInt(fc.slice(1, 3), 16) + parseInt(fc.slice(3, 5), 16) + parseInt(fc.slice(5, 7), 16);
    const ink = lum > 380 ? "60,40,25" : "255,255,255", al = lum > 380 ? [0.13, 0.22, 0.32] : [0.14, 0.25, 0.36];
    for (let m = Math.ceil(gx0 / stepMm) * stepMm; m <= gx1; m += stepMm) {
      const gk = Math.round(m / stepMm), major = gk === 0 ? 2 : gk % majEvery === 0 ? 1 : 0;
      ctx.strokeStyle = `rgba(${ink},${al[major]})`;
      const [px, py0] = mm2px(m, gy0), [, py1] = mm2px(m, gy1);
      ctx.beginPath(); ctx.moveTo(Math.round(px) + 0.5, py0); ctx.lineTo(Math.round(px) + 0.5, py1); ctx.stroke();
    }
    for (let m = Math.ceil(gy0 / stepMm) * stepMm; m <= gy1; m += stepMm) {
      const gk = Math.round(m / stepMm), major = gk === 0 ? 2 : gk % majEvery === 0 ? 1 : 0;
      ctx.strokeStyle = `rgba(${ink},${al[major]})`;
      const [px0, py] = mm2px(gx0, m), [px1] = mm2px(gx1, m);
      ctx.beginPath(); ctx.moveTo(px0, Math.round(py) + 0.5); ctx.lineTo(px1, Math.round(py) + 0.5); ctx.stroke();
    }
    ctx.restore();
  }
  // outside the sewing area: shaded and hatched, so the cut-off is obvious
  const [fx, fy] = mm2px(-W / 2, -H / 2);
  {
    const fc = S.layout.fabric_color || "#d9d5cc";
    const lum = parseInt(fc.slice(1, 3), 16) + parseInt(fc.slice(3, 5), 16) + parseInt(fc.slice(5, 7), 16);
    ctx.save();
    roundRect(ctx, x0, y0, fw, fh, rr); ctx.rect(fx, fy, W * s, H * s);
    ctx.clip("evenodd");
    ctx.fillStyle = lum > 380 ? "rgba(60,30,10,.13)" : "rgba(0,0,0,.32)"; ctx.fillRect(x0, y0, fw, fh);
    ctx.strokeStyle = lum > 380 ? "rgba(90,50,20,.16)" : "rgba(255,255,255,.07)"; ctx.lineWidth = 1;
    const step = 9;
    ctx.beginPath();
    for (let d = -fh; d < fw + fh; d += step) { ctx.moveTo(x0 + d, y0); ctx.lineTo(x0 + d - fh, y0 + fh); }
    ctx.stroke();
    ctx.restore();
  }
  // the sewing area's edge: a clear red dashed line with a label
  ctx.save();
  ctx.setLineDash([8, 5]); ctx.strokeStyle = "rgba(196,60,45,.85)"; ctx.lineWidth = 1.6;
  ctx.strokeRect(fx, fy, W * s, H * s); ctx.setLineDash([]);
  const fsz = Math.max(10, Math.min(13, s * 3));
  ctx.font = `600 ${fsz}px system-ui, "Segoe UI", sans-serif`; ctx.textBaseline = "bottom"; ctx.textAlign = "left";
  const label = `Sewing area ${U.inch ? `${U.show(W)} × ${U.show(H)} in` : `${W} × ${H} mm`}`;
  const lw = ctx.measureText(label).width;
  ctx.fillStyle = "rgba(196,60,45,.9)"; roundRect(ctx, fx, fy - fsz - 6, lw + 10, fsz + 6, 3); ctx.fill();
  ctx.fillStyle = "#fff"; ctx.fillText(label, fx + 5, fy - 2);
  ctx.restore();
  ctx.strokeStyle = "rgba(122,82,52,.45)"; ctx.lineWidth = 1;
  const tick = U.inch ? 25.4 / 8 : 5, longEvery = U.inch ? 4 : 2; // eighths of an inch, long every half / 5 mm, long every 10
  for (let i = 0, m = 0; m <= W + 1e-6; m = ++i * tick) { const [tx] = mm2px(-W / 2 + m, 0); ctx.beginPath(); ctx.moveTo(tx, fy + H * s); ctx.lineTo(tx, fy + H * s + (i % longEvery ? 3 : 7)); ctx.stroke(); }
  for (let i = 0, m = 0; m <= H + 1e-6; m = ++i * tick) { const [, ty] = mm2px(0, -H / 2 + m); ctx.beginPath(); ctx.moveTo(fx, ty); ctx.lineTo(fx - (i % longEvery ? 3 : 7), ty); ctx.stroke(); }
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
  if (!info._img) {
    info._img = new Image(); info._img.src = "/api/image/" + el.image_id; info._img.onload = draw;
    // the website's server may have forgotten it: put our copy back once, then draw again
    info._img.onerror = () => { if (info._restoring) return; info._restoring = true; restorePictures([el.image_id]).then(draw).catch(() => {}); };
    return;
  }
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
function visOf(el) {
  // while a resize waits for its stitches: draw the piece at the size it's going to be
  const v = S.vis[el.id], b = v && S.built?.elements?.[S.layout.elements.indexOf(el)];
  return b && b.w && b.h ? { fx: v.w / b.w, fy: v.h / b.h } : { fx: 1, fy: 1 };
}
function shownSize(el, i) {
  // the size a piece is drawn at right now (mm): its stitches, or a resize still being stitched
  const b = S.built?.elements?.[i];
  if (!b) return null;
  const v = S.vis[el.id];
  return v ? { w: v.w, h: v.h } : { w: b.w, h: b.h };
}

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
  if ($("#showProblems").checked && S.built?.stats?.problems) drawProblems(S.built.stats.problems);
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
  // multi-selection: a dashed box round each item and one round the whole group
  if (multi.size > 1) {
    const idx = selected();
    for (const i of idx) { const b = selBox(els[i], i); if (b && !els[i].hidden) boxOutline(els[i], b, "#b8322a", [5, 3], 1.4); }
    const g = groupBox(idx);
    if (g) {
      const [ax, ay] = mm2px(g.x0 - 1.5, g.y0 - 1.5), [bx2, by2] = mm2px(g.x1 + 1.5, g.y1 + 1.5);
      ctx.strokeStyle = "rgba(184,50,42,.55)"; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
      ctx.strokeRect(ax, ay, bx2 - ax, by2 - ay); ctx.setLineDash([]);
    }
    const gh = groupHandles();
    if (gh) {
      const [tx, ty] = mm2px((gh.box.x0 + gh.box.x1) / 2, gh.box.y0);
      ctx.strokeStyle = "#b8322a"; ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(gh.rot[0], gh.rot[1]); ctx.stroke();
      for (const [k, v] of Object.entries(gh)) {
        if (k === "box") continue;
        const [x, y] = v;
        ctx.fillStyle = "#fff"; ctx.strokeStyle = "#b8322a"; ctx.lineWidth = 1.6;
        if (k === "rot") { ctx.beginPath(); ctx.arc(x, y, 6.5, 0, 7); ctx.fill(); ctx.stroke(); ctx.beginPath(); ctx.arc(x, y, 3, 0.4, 5.2); ctx.stroke(); }
        else { ctx.fillRect(x - 5, y - 5, 10, 10); ctx.strokeRect(x - 5, y - 5, 10, 10); }
      }
    }
  }
  if (drag && drag.kind === "marquee") {
    const [ax, ay] = mm2px(drag.x0, drag.y0), [bx2, by2] = mm2px(drag.x1, drag.y1);
    ctx.fillStyle = "rgba(184,50,42,.08)"; ctx.fillRect(ax, ay, bx2 - ax, by2 - ay);
    ctx.strokeStyle = "rgba(184,50,42,.8)"; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
    ctx.strokeRect(ax, ay, bx2 - ax, by2 - ay); ctx.setLineDash([]);
  }
  if (nodeEl()) { drawNodes(nodeEl()); return; }
  // selection: PowerPoint-style box with 8 square handles + rotate knob
  const el = multi.size > 1 ? null : els[S.sel];
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

function drawProblems(list) {
  ctx.save();
  for (const p of list) {
    const [x, y] = mm2px(p.x, p.y), warn = p.level === "warn", focus = S.probFocus === p;
    if (!warn && !focus) continue;   // quiet notes only show up when you click them
    if (focus) { ctx.strokeStyle = "rgba(196,60,45,.9)"; ctx.lineWidth = 2.5; ctx.beginPath(); ctx.arc(x, y, 26, 0, 7); ctx.stroke(); }
    ctx.fillStyle = warn ? "#e8702a" : "#6b7a8f"; ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x, y - 10); ctx.lineTo(x + 9, y + 7); ctx.lineTo(x - 9, y + 7); ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#fff"; ctx.font = "bold 10px system-ui"; ctx.textAlign = "center"; ctx.fillText("!", x, y + 5.5);
  }
  ctx.restore();
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
function groupHandles() {
  // the group box's 8 handles + rotate knob, in screen px (the box is square to the hoop)
  const g = groupBox(selected());
  if (!g) return null;
  const x0 = g.x0 - 1.5, y0 = g.y0 - 1.5, x1 = g.x1 + 1.5, y1 = g.y1 + 1.5, cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const out = { box: { x0, y0, x1, y1 } };
  for (const k of HANDLE_KEYS) { const [sx, sy] = HANDLE_SIGN[k]; out[k] = mm2px(cx + (sx * (x1 - x0)) / 2, cy + (sy * (y1 - y0)) / 2); }
  const [tx, ty] = mm2px(cx, y0);
  out.rot = [tx, ty - 22];
  return out;
}
// ---- point editing for drawings: drag points, click + Delete removes, double-click a line adds one
S.nodeEdit = null; S.nodeSel = null;
function nodeEl() { const el = S.layout.elements[S.sel]; return el && el.type === "vector" && S.nodeEdit === el.id && multi.size <= 1 ? el : null; }
function vecToScreen(el, u, v) {
  const w = el.width_mm, asp = el.aspect || 0.5, kx = el.stretch_x || 1, ky = el.stretch_y || 1;
  return elTransform(el)((u - 0.5) * w * kx, (v - asp / 2) * w * ky);
}
function screenToVec(el, px, py) {
  const [mx, my] = px2mm(px, py), a = (-(el.rotation || 0) * Math.PI) / 180;
  const dx = mx - el.x, dy = my - el.y, lx = dx * Math.cos(a) - dy * Math.sin(a), ly = dx * Math.sin(a) + dy * Math.cos(a);
  const w = el.width_mm, asp = el.aspect || 0.5, kx = el.stretch_x || 1, ky = el.stretch_y || 1;
  return [Math.round((lx / (w * kx) + 0.5) * 10000) / 10000, Math.round((ly / (w * ky) + asp / 2) * 10000) / 10000];
}
function hitNode(el, px, py) {
  let best = null, bd = 8;
  (el.parts || []).forEach((p, pi) => (p.points || []).forEach(([u, v], i) => {
    const [x, y] = vecToScreen(el, u, v), d = Math.hypot(px - x, py - y);
    if (d < bd) { bd = d; best = { p: pi, i }; }
  }));
  return best;
}
function drawNodes(el) {
  ctx.save();
  (el.parts || []).forEach((p, pi) => {
    const pts = (p.points || []).map(([u, v]) => vecToScreen(el, u, v));
    if (!pts.length) return;
    ctx.strokeStyle = "rgba(232,112,42,.9)"; ctx.lineWidth = 1.2; ctx.setLineDash([]);
    ctx.beginPath(); pts.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (p.type === "polygon") ctx.closePath();
    ctx.stroke();
    pts.forEach(([x, y], i) => {
      const on = S.nodeSel && S.nodeSel.p === pi && S.nodeSel.i === i;
      ctx.fillStyle = on ? "#b8322a" : "#fff"; ctx.strokeStyle = "#b8322a"; ctx.lineWidth = 1.4;
      ctx.fillRect(x - 4, y - 4, 8, 8); ctx.strokeRect(x - 4, y - 4, 8, 8);
    });
  });
  ctx.restore();
}
function deleteNode() {
  const el = nodeEl(), s = S.nodeSel;
  if (!el || !s) return false;
  const part = el.parts[s.p];
  if (!part) return false;
  beginEdit();
  part.points.splice(s.i, 1);
  const closed = part.points.length > 2 && part.points[0][0] === part.points[part.points.length - 1][0] && part.points[0][1] === part.points[part.points.length - 1][1];
  if (part.points.length < (part.type === "polygon" ? 3 : 2) || (closed && part.points.length < 3)) el.parts.splice(s.p, 1);
  S.nodeSel = null;
  if (!el.parts.length) { commit(true); removeElement(S.sel); S.nodeEdit = null; return true; }
  commit(true); save_local(); scheduleBuild(0); draw();
  return true;
}
cv.addEventListener("dblclick", (e) => {
  const el = nodeEl();
  if (!el) return;
  const r = cv.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
  // nearest segment within a few pixels: put a new point there
  let best = null, bd = 7;
  el.parts.forEach((p, pi) => {
    if (p.type === "circle") return;
    const pts = p.points.map(([u, v]) => vecToScreen(el, u, v));
    for (let i = 0; i + 1 < pts.length; i++) {
      const [ax, ay] = pts[i], [bx, by] = pts[i + 1], L2 = (bx - ax) ** 2 + (by - ay) ** 2 || 1;
      const t = clamp(((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / L2, 0, 1);
      const d = Math.hypot(px - (ax + t * (bx - ax)), py - (ay + t * (by - ay)));
      if (d < bd) { bd = d; best = { p: pi, i: i + 1 }; }
    }
  });
  if (!best) return;
  beginEdit();
  el.parts[best.p].points.splice(best.i, 0, screenToVec(el, px, py));
  S.nodeSel = best;
  commit(true); save_local(); scheduleBuild(0); draw();
});

function hitHandle(px, py) {
  if (nodeEl()) return null; // editing points: no resize handles
  let hs;
  if (multi.size > 1) {
    hs = groupHandles();
    if (!hs) return null;
    hs = Object.fromEntries(Object.entries(hs).filter(([k]) => k !== "box"));
  } else {
    const el = S.layout.elements[S.sel], bx = el && !el.hidden && selBox(el, S.sel);
    if (!bx) return null;
    hs = handles(el, bx);
  }
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
  const ne = nodeEl();
  if (ne) {
    const hit = hitNode(ne, px, py);
    if (hit) {
      S.nodeSel = hit; beginEdit();
      drag = { kind: "node", el: ne, hit };
      cv.setPointerCapture(e.pointerId); draw();
      return;
    }
    if (hitElement(mx, my) !== S.sel) { S.nodeEdit = null; S.nodeSel = null; renderProps(); }
    else { S.nodeSel = null; draw(); return; }
  }
  const h = hitHandle(px, py);
  if (h && multi.size > 1) {
    const gh = groupHandles(), idx = selected();
    beginEdit();
    drag = { kind: h === "rot" ? "grot" : "gresize", handle: h, box: gh.box,
      items: idx.map((i) => ({ el: S.layout.elements[i], start: JSON.parse(JSON.stringify(S.layout.elements[i])), size: shownSize(S.layout.elements[i], i) })),
      cx: (gh.box.x0 + gh.box.x1) / 2, cy: (gh.box.y0 + gh.box.y1) / 2 };
    drag.a0 = Math.atan2(my - drag.cy, mx - drag.cx);
    cv.setPointerCapture(e.pointerId);
    return;
  }
  if (h) {
    const el = S.layout.elements[S.sel], b = shownSize(S.layout.elements[S.sel], S.sel);
    beginEdit();
    drag = { kind: h === "rot" ? "rot" : "resize", handle: h, el, start: JSON.parse(JSON.stringify(el)),
      w0: b.w, h0: b.h, a0: Math.atan2(my - el.y, mx - el.x) };
    cv.setPointerCapture(e.pointerId);
    return;
  }
  const i = hitElement(mx, my);
  if (i >= 0 && e.shiftKey) {
    const mates = groupMates(S.layout.elements[i]);
    if (mates.length > 1) {
      // shift+click on a group adds / removes the whole group
      const on = multi.has(mates[0].id);
      if (!multi.size && S.sel >= 0) multi.add(S.layout.elements[S.sel].id);
      for (const x of mates) on ? multi.delete(x.id) : multi.add(x.id);
      if (multi.size === 1) { S.sel = S.layout.elements.findIndex((x) => multi.has(x.id)); multi.clear(); }
      else if (!on) S.sel = i;
      renderLayers(); renderProps(); draw();
      return;
    }
    toggleInSelection(i); return;
  }
  if (i >= 0 && !e.altKey && S.layout.elements[i].group && !multi.has(S.layout.elements[i].id)) {
    selectGroupOf(i); renderLayers(); renderProps();
  }
  if (i >= 0 && multi.size > 1 && multi.has(S.layout.elements[i].id)) {
    // drag any of the selected items: they all move together
    beginEdit();
    drag = { kind: "moveMany", mx0: mx, my0: my, done: { x: 0, y: 0 } };
    cv.setPointerCapture(e.pointerId);
    return;
  }
  if (i < 0) {
    // empty fabric: drag a box to select what it touches (Shift keeps the current selection)
    if (!e.shiftKey) { clearMulti(); if (S.sel !== -1) { S.sel = -1; renderLayers(); renderProps(); } }
    drag = { kind: "marquee", x0: mx, y0: my, x1: mx, y1: my, add: e.shiftKey };
    cv.setPointerCapture(e.pointerId);
    draw();
    return;
  }
  if (i !== S.sel || multi.size) { clearMulti(); S.sel = i; renderLayers(); renderProps(); }
  beginEdit();
  const el = S.layout.elements[i];
  drag = { kind: "move", el, dx: mx - el.x, dy: my - el.y };
  cv.setPointerCapture(e.pointerId);
  draw();
});

function setStretch(el, key, v) {
  v = Math.round(clamp(v, 0.2, 5) * 1000) / 1000;
  if (Math.abs(v - 1) < 0.01) delete el[key]; else el[key] = v;
}
// resize one element by fx (across) and fy (down), in its own frame, from its state s0 at the
// start of the drag. Its natural size takes one factor; a stretch takes the difference, so
// pictures/drawings/text can be squashed or widened. Returns the factors actually applied.
function scaleElement(el, s0, fx, fy) {
  const kx0 = s0.stretch_x || 1, ky0 = s0.stretch_y || 1;
  if (el.type === "text") {
    el.height_mm = Math.round(clamp(s0.height_mm * fy, 3, 150) * 10) / 10;
    const a = el.height_mm / s0.height_mm;
    setStretch(el, "stretch_x", (kx0 * fx) / a);
    return { fx: ((el.stretch_x || 1) / kx0) * a, fy: a };
  }
  if (el.type === "shape") {
    el.width_mm = Math.round(clamp(s0.width_mm * fx, 2, 400) * 10) / 10;
    el.height_mm = Math.round(clamp(s0.height_mm * fy, 2, 400) * 10) / 10;
    return { fx: el.width_mm / s0.width_mm, fy: el.height_mm / s0.height_mm };
  }
  el.width_mm = Math.round(clamp(s0.width_mm * fx, 3, 400) * 10) / 10;
  const a = el.width_mm / s0.width_mm;
  setStretch(el, "stretch_y", (ky0 * fy) / a);
  return { fx: a, fy: ((el.stretch_y || 1) / ky0) * a };
}

function applyResize(e, mx, my) {
  // corners and sides stretch freely; hold Shift to keep the proportions
  const d = drag, el = d.el, s0 = d.start;
  const [sx, sy] = HANDLE_SIGN[d.handle];
  // work in the element's frame at the moment the drag started
  const a = (-(s0.rotation || 0) * Math.PI) / 180, dx = mx - s0.x, dy = my - s0.y;
  const lx = dx * Math.cos(a) - dy * Math.sin(a), ly = dx * Math.sin(a) + dy * Math.cos(a);
  const W = d.w0, H = d.h0;
  const ax = (-sx * W) / 2, ay = (-sy * H) / 2; // opposite corner / edge stays put
  let fx = sx ? clamp((sx * (lx - ax)) / W, 0.08, 12) : 1;
  let fy = sy ? clamp((sy * (ly - ay)) / H, 0.08, 12) : 1;
  if (e.shiftKey) {
    if (sx && sy) {
      const vx = sx * W, vy = sy * H;
      fx = fy = clamp(((lx - ax) * vx + (ly - ay) * vy) / (vx * vx + vy * vy), 0.08, 12);
    } else {
      fx = fy = sx ? fx : fy;
    }
  }
  ({ fx, fy } = scaleElement(el, s0, fx, fy));
  // new centre: anchor + half the new extent, back in design coordinates
  const cxL = sx ? ax + (sx * W * fx) / 2 : 0, cyL = sy ? ay + (sy * H * fy) / 2 : 0;
  const ra = ((s0.rotation || 0) * Math.PI) / 180;
  el.x = Math.round((s0.x + cxL * Math.cos(ra) - cyL * Math.sin(ra)) * 10) / 10;
  el.y = Math.round((s0.y + cxL * Math.sin(ra) + cyL * Math.cos(ra)) * 10) / 10;
  S.vis[el.id] = { w: W * fx, h: H * fy };
}

function applyGroupResize(e, mx, my) {
  // several pieces: scale the whole group from the opposite side of its box
  const d = drag, b = d.box;
  const [sx, sy] = HANDLE_SIGN[d.handle];
  const W = b.x1 - b.x0, H = b.y1 - b.y0;
  const ax = sx > 0 ? b.x0 : sx < 0 ? b.x1 : (b.x0 + b.x1) / 2;
  const ay = sy > 0 ? b.y0 : sy < 0 ? b.y1 : (b.y0 + b.y1) / 2;
  let fx = sx ? clamp((sx * (mx - ax)) / W, 0.08, 12) : 1;
  let fy = sy ? clamp((sy * (my - ay)) / H, 0.08, 12) : 1;
  if (e.shiftKey) {
    if (sx && sy) {
      const vx = sx * W, vy = sy * H;
      fx = fy = clamp(((mx - ax) * vx + (my - ay) * vy) / (vx * vx + vy * vy), 0.08, 12);
    } else fx = fy = sx ? fx : fy;
  }
  for (const { el, start, size } of d.items) {
    // a piece turned on its side swaps the factors; one at an odd angle scales evenly
    const r = (((start.rotation || 0) % 180) + 180) % 180;
    let ex = fx, ey = fy;
    if (Math.abs(r - 90) < 10) [ex, ey] = [fy, fx];
    else if (r > 10 && r < 170) ex = ey = Math.sqrt(fx * fy);
    const got = scaleElement(el, start, ex, ey);
    el.x = Math.round((ax + (start.x - ax) * fx) * 10) / 10;
    el.y = Math.round((ay + (start.y - ay) * fy) * 10) / 10;
    if (size) S.vis[el.id] = { w: size.w * got.fx, h: size.h * got.fy };
  }
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
  if (drag.kind === "marquee") { drag.x1 = mx; drag.y1 = my; draw(); return; }
  if (drag.kind === "moveMany") {
    const snap = e.shiftKey ? 0.1 : 0.5;
    const tx = Math.round((mx - drag.mx0) / snap) * snap, ty = Math.round((my - drag.my0) / snap) * snap;
    moveSelected(tx - drag.done.x, ty - drag.done.y);
    drag.done = { x: tx, y: ty };
    draw();
    return;
  }
  const el = drag.el || null;
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
  } else if (drag.kind === "node") {
    drag.el.parts[drag.hit.p].points[drag.hit.i] = screenToVec(drag.el, px, py);
  } else if (drag.kind === "resize") {
    applyResize(e, mx, my);
  } else if (drag.kind === "gresize") {
    applyGroupResize(e, mx, my);
  } else if (drag.kind === "grot") {
    let da = ((Math.atan2(my - drag.cy, mx - drag.cx) - drag.a0) * 180) / Math.PI;
    if (!e.shiftKey) da = Math.round(da / 5) * 5;
    const r = (da * Math.PI) / 180, c = Math.cos(r), s = Math.sin(r);
    for (const { el: it, start } of drag.items) {
      const dx = start.x - drag.cx, dy = start.y - drag.cy;
      it.x = Math.round((drag.cx + dx * c - dy * s) * 10) / 10;
      it.y = Math.round((drag.cy + dx * s + dy * c) * 10) / 10;
      it.rotation = (((start.rotation || 0) + da + 540) % 360) - 180;
    }
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
  const k = drag.kind, d = drag;
  drag = null;
  if (k === "pan") { cv.style.cursor = "default"; return; }
  if (k === "marquee") {
    const x0 = Math.min(d.x0, d.x1), x1 = Math.max(d.x0, d.x1), y0 = Math.min(d.y0, d.y1), y1 = Math.max(d.y0, d.y1);
    if (x1 - x0 > 0.5 || y1 - y0 > 0.5) {
      if (d.add && !multi.size && S.sel >= 0) multi.add(S.layout.elements[S.sel].id);
      S.layout.elements.forEach((el, i) => {
        if (el.hidden) return;
        const b = groupBox([i]);
        if (b && b.x1 >= x0 && b.x0 <= x1 && b.y1 >= y0 && b.y0 <= y1) { multi.add(el.id); S.sel = i; }
      });
      for (const el of [...S.layout.elements]) if (multi.has(el.id)) groupMates(el).forEach((x) => multi.add(x.id));
      if (multi.size === 1) { const only = selected()[0]; multi.clear(); S.sel = only; }
    }
    renderLayers(); renderProps(); draw();
    return;
  }
  commit(true); save_local(); renderProps(); renderLayers();
  scheduleBuild(k === "resize" || k === "gresize" || k === "node" ? 0 : 350);
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
  const parts = Object.entries(o).map(([k, v]) => `${U.len(v)} past the ${k} edge`);
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
  const web = !!S.meta.hosted;
  $("#diskLocal").hidden = web; $("#diskWeb").hidden = !web;
  $("#dlgDisk").showModal();
  if (!web) { await refreshDrives(); return; }
  if (!window.showDirectoryPicker) {
    $("#diskStatus").innerHTML = "Writing straight to a floppy needs <b>Chrome or Edge</b> on a computer. In other browsers, use <b>Save file</b> and copy the files onto the disk yourself.";
    $("#diskPick").disabled = true; $("#diskWrite").disabled = true; return;
  }
  $("#diskPick").disabled = false; $("#diskWrite").disabled = false;
  if (webDisk.dir) await diskStatus(); else $("#diskStatus").textContent = "Put the floppy in the drive, then choose it above.";
}
// website: the browser reads/writes the floppy itself (File System Access API, Chrome/Edge)
const webDisk = {
  dir: null,
  b64(buf) { const u = new Uint8Array(buf); let s = ""; for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode(...u.subarray(i, i + 0x8000)); return btoa(s); },
  bytes(b64) { const s = atob(b64), u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return u; },
  async sub(dir, path, create = false) { let d = dir; for (const p of path) d = await d.getDirectoryHandle(p, { create }); return d; },
  async read() {
    const files = {}, dir = webDisk.dir;
    const get = async (d, n) => { try { return await d.getFileHandle(n); } catch (e) { return null; } };
    const phv = await get(dir, "MENU_SEL.PHV");
    if (phv) files["MENU_SEL.PHV"] = webDisk.b64(await (await phv.getFile()).arrayBuffer());
    let m = null; try { m = await dir.getDirectoryHandle("MENU_01"); } catch (e) {}
    if (m) {
      const mhv = await get(m, "MENU_01.MHV");
      if (mhv) files["MENU_01/MENU_01.MHV"] = webDisk.b64(await (await mhv.getFile()).arrayBuffer());
      for await (const [name, h] of m.entries()) if (h.kind === "file" && /^DES01_\d\d\.SHV$/i.test(name)) files["MENU_01/" + name.toUpperCase()] = "";
    }
    return files;
  },
  async apply(write, del) {
    for (const [rel, b64] of write) {
      const parts = rel.split("/"), d = await webDisk.sub(webDisk.dir, parts.slice(0, -1), true);
      const fh = await d.getFileHandle(parts[parts.length - 1], { create: true });
      const data = webDisk.bytes(b64), w = await fh.createWritable();
      await w.write(data); await w.close();
      const back = new Uint8Array(await (await fh.getFile()).arrayBuffer());  // read back to check
      if (back.length !== data.length || back.some((v, i) => v !== data[i])) throw new Error("Checking " + rel + " failed - try another disk.");
    }
    for (const rel of del) {
      const parts = rel.split("/"), d = await webDisk.sub(webDisk.dir, parts.slice(0, -1));
      await d.removeEntry(parts[parts.length - 1]).catch(() => {});
    }
  },
};
$("#diskPick").onclick = async () => {
  try {
    webDisk.dir = await window.showDirectoryPicker({ id: "thimble-floppy", mode: "readwrite", startIn: "desktop" });
    $("#diskPicked").textContent = webDisk.dir.name;
    await diskStatus();
  } catch (e) { if (e.name !== "AbortError") $("#diskStatus").textContent = e.message; }
};
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
  const t = S.meta.hosted ? webDisk.dir : diskTarget(), box = $("#diskStatus");
  if (!t) { box.textContent = S.meta.hosted ? "Put the floppy in the drive, then choose it above." : "Put a disk in the drive, then press Look again."; return; }
  try {
    const st = S.meta.hosted ? await api("/api/disk/peek", { files: await webDisk.read() }) : await api("/api/disk/status", { target: t });
    const mode = $$('input[name=diskMode]').find((r) => r.checked).value;
    const next = mode === "replace" || !st.layout ? 0 : st.used;
    box.innerHTML = (st.layout ? `This disk has a Designer menu with <b>${st.used}</b> of 36 designs.` : `No Designer menu on this disk yet — I'll create one.`) +
      `<div class="disk-slots">${Array.from({ length: 36 }, (_, i) => `<i class="${i === next ? "next" : i < st.used && mode === "add" ? "used" : ""}" title="Slot ${i + 1}"></i>`).join("")}</div>` +
      `<div class="muted small" style="margin-top:6px">Your design will be <b>Menu 1, slot ${next + 1}</b>.</div>`;
  } catch (e) { box.textContent = e.message; }
}
async function writeDisk() {
  const target = S.meta.hosted ? webDisk.dir : diskTarget();
  const mode = $$('input[name=diskMode]').find((r) => r.checked).value;
  if (!target) { const r = $("#diskResult"); r.className = "result bad"; r.textContent = "Choose a drive first."; return; }
  if (blockedOffHoop(true)) return;
  if (mode === "replace" && !confirm("Start fresh? The designs already in Menu 1 on this disk will be replaced.")) return;
  const res = $("#diskResult");
  res.className = "result"; res.textContent = "Writing… (floppies are slow — about 10 seconds)";
  $("#diskWrite").disabled = true;
  try {
    let r;
    if (S.meta.hosted) {
      const b = await api("/api/disk/build", { layout: S.layout, files: await webDisk.read(), mode, label: $("#diskLabel").value });
      await webDisk.apply(b.write, b.delete);
      r = { slot: b.slot, files: b.write.map((w) => w[0]) };
    } else {
      r = await api("/api/disk/write", { layout: S.layout, target, mode, label: $("#diskLabel").value });
    }
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
  $("#setUnits").value = UNITS;
  $("#setHoopW").value = U.show(S.layout.hoop[0]); $("#setHoopH").value = U.show(S.layout.hoop[1]);
  $("#setHoopW").step = $("#setHoopH").step = U.inch ? 0.1 : 1;
  $("#setHoopW").placeholder = `Width (${U.u})`; $("#setHoopH").placeholder = `Height (${U.u})`;
  [...$("#setHoop").options].forEach((o) => {
    if (!o.dataset.label) o.dataset.label = o.textContent;
    const [w, h] = o.value.split("x").map(Number);
    o.textContent = o.dataset.label + (U.inch && w ? ` (${U.show(w)} × ${U.show(h)} in)` : "");
  });
  $("#customHoop").hidden = preset;
  $("#setHoop").onchange = () => { $("#customHoop").hidden = $("#setHoop").value !== "custom"; };
  $("#setFabric").innerHTML = Object.entries(S.meta.fabrics).map(([k, v]) => `<option value="${k}"${k === S.layout.fabric ? " selected" : ""}>${esc(v)}</option>`).join("");
  $("#setGroup").checked = S.layout.group_colors !== false;
  $("#setDensity").value = S.layout.density || "standard";
  $("#setBrand").innerHTML = [MACHINE_BRAND, ...(S.meta.brands || [])].map((b) => `<option${b === threadBrand ? " selected" : ""}>${esc(b)}</option>`).join("");
  $("#setKey").value = ""; $("#setKey").type = "password"; $("#keyShow").textContent = "Show";
  $("#setKey").placeholder = S.meta.ai ? "Your key is saved - paste a new one only to replace it" : "Paste your key here (sk-ant-…)";
  const st = $("#aiStatus"); st.textContent = S.meta.ai ? "✓ Set up and ready" : "Not set up yet"; st.className = "ai-status " + (S.meta.ai ? "ok" : "no");
  $("#keyTestMsg").textContent = "";
  if (S.meta.hosted) $("#keyNote").textContent = "Saved only in this web browser on this computer - Thimble's server never keeps it. It's only sent along with the pictures you choose to read, straight on to Anthropic. Use Forget my key on a shared computer.";
  $("#keyShow").onclick = () => { const k = $("#setKey"); k.type = k.type === "password" ? "text" : "password"; $("#keyShow").textContent = k.type === "password" ? "Show" : "Hide"; };
  $("#keyTest").onclick = async () => {
    const msg = $("#keyTestMsg"); msg.textContent = "Checking…"; msg.style.color = "";
    const key = $("#setKey").value.trim(), ws = $("#setWorkspace").value.trim();
    try {
      const r = await fetch("/api/ai/test", { method: "POST", headers: Object.assign({ "Content-Type": "application/json" },
        S.meta.hosted ? { "X-Anthropic-Key": key || webKey.key, "X-Anthropic-Workspace": ws || webKey.ws } : {}), body: JSON.stringify(S.meta.hosted ? {} : { key, workspace: ws }) });
      const j = await r.json();
      msg.textContent = j.msg || j.error || "Something went wrong."; msg.style.color = j.ok ? "#2f6b2a" : "#a33";
    } catch (e) { msg.textContent = "Couldn't check right now - try again in a moment."; msg.style.color = "#a33"; }
  };
  $("#keyForget").onclick = async () => {
    if (!confirm("Remove your AI key from this computer? You can paste it again any time.")) return;
    if (S.meta.hosted) { try { localStorage.removeItem("thimble.akey"); localStorage.removeItem("thimble.aws"); } catch (e) {} S.meta.ai = false; S.meta.workspace = ""; }
    else { const r = await api("/api/ai/forget", {}); S.meta.ai = r.ai; S.meta.workspace = r.workspace; }
    $("#setWorkspace").value = ""; $("#aiStatus").textContent = "Not set up yet"; $("#aiStatus").className = "ai-status no";
    $("#setKey").placeholder = "Paste your key here (sk-ant-…)"; $("#keyTestMsg").textContent = "Key removed.";
  };
  $("#setWorkspace").value = S.meta.workspace || "";
  $("#fabricSwatches").innerHTML = FABRIC_COLORS.map((c) => `<button type="button" data-c="${c}" style="background:${c}" class="${c === S.layout.fabric_color ? "on" : ""}" aria-label="Fabric ${c}"></button>`).join("");
  $$("#fabricSwatches button").forEach((b) => (b.onclick = () => { $$("#fabricSwatches button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); }));
  $("#dlgSettings").showModal();
}
async function saveSettings() {
  commit(true); beginEdit();
  S.layout.hoop = $("#setHoop").value === "custom"
    ? [clamp(Math.round(U.toMm(+$("#setHoopW").value) || 100), 20, 500), clamp(Math.round(U.toMm(+$("#setHoopH").value) || 100), 20, 500)]
    : $("#setHoop").value.split("x").map(Number);
  if ($("#setUnits").value !== UNITS) setUnits($("#setUnits").value);
  S.layout.fabric = $("#setFabric").value;
  S.layout.density = $("#setDensity").value;
  S.layout.group_colors = $("#setGroup").checked;
  if ($("#setBrand").value !== threadBrand) await setThreadBrand($("#setBrand").value);
  const fc = $("#fabricSwatches button.on"); if (fc) S.layout.fabric_color = fc.dataset.c;
  commit(true);
  const key = $("#setKey").value.trim(), ws = $("#setWorkspace").value.trim();
  if (key || ws !== (S.meta.workspace || "")) {
    if (S.meta.hosted) {
      webKey.set(key, ws); S.meta.ai = !!webKey.key; S.meta.workspace = ws;
      toast("AI key saved in this browser only.", "good");
    } else {
      const r = await api("/api/ai/key", { key, workspace: ws });
      S.meta.ai = r.ai; S.meta.workspace = r.workspace;
      toast("AI settings saved on this computer.", "good");
    }
  }
  $("#dlgSettings").close();
  renderProps(); scheduleBuild(0); save_local();
}
// ---- feedback: bug reports and feature requests
let fbKind = "bug";
$("#btnFeedback").onclick = () => { $("#dlgFeedback").showModal(); $("#fbMsg").focus(); };
$$("#fbKind .chip").forEach((b) => (b.onclick = () => {
  fbKind = b.dataset.k;
  $$("#fbKind .chip").forEach((c) => c.classList.toggle("on", c === b));
  $("#fbMsgLbl").textContent = fbKind === "bug" ? "What went wrong? What did you expect?" : "What would you like Thimble to do?";
}));
$("#fbSend").onclick = async () => {
  const message = $("#fbMsg").value.trim();
  if (!message) { toast("Write a few words first."); return; }
  const body = { kind: fbKind, message, contact: $("#fbContact").value.trim(), page: location.href, agent: navigator.userAgent };
  if ($("#fbDesign").checked) {
    const l = JSON.parse(JSON.stringify(S.layout));
    (l.elements || []).forEach((e) => { if (e.blocks) e.blocks = "(stitch data left out)"; });
    body.design = l;
  }
  try { await api("/api/feedback", body); $("#dlgFeedback").close(); $("#fbMsg").value = ""; toast("Thank you! Your note was sent.", "good"); }
  catch (e) { toast("Couldn't send that - " + (e.message || "try again"), "bad"); }
};
$("#openLegal").onclick = async () => {
  $("#dlgLegal").showModal();
  try {
    const r = await api("/api/legal");
    $("#legalFonts").innerHTML = r.fonts.map((f) => `<div><b>${esc(f.name)}</b> - ${esc(f.license)}${f.original ? ` (from ${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.original)}</a>` : esc(f.original)})` : ""}</div>`).join("");
  } catch (e) { $("#legalFonts").textContent = "Each font's license is listed in its font.json file in the source code."; }
};
$("#busyCancel").onclick = () => { if (activeAbort) activeAbort.abort(); $("#busyCancel").hidden = true; busy(false); };

// ---- the "How to" tour (opens by itself the first time)
const HOWTO = [
  { icon: "🧵", title: "Welcome to Thimble", body: `<p>Turn words, shapes and pictures into stitches for your embroidery machine. It takes four steps:</p>
    <ul><li><b>Add</b> something to the hoop</li><li><b>Arrange</b> it</li><li><b>Preview</b> the stitching</li><li><b>Save</b> it for your machine</li></ul>` },
  { icon: "✚", title: "1. Add to the hoop", body: `<p>On the left: <b>Words</b> to type some text, <b>Shape</b> for hearts and frames, <b>Picture / file</b> to stitch a logo or drawing, and <b>Read a picture</b> to let the AI rebuild a picture as clean stitchable shapes (optional - set it up in Settings).</p>` },
  { icon: "✥", title: "2. Arrange it", body: `<ul><li><b>Drag</b> to move. Drag a <b>corner or side</b> to stretch; hold <b>Shift</b> to keep the shape.</li>
    <li>The round knob on top <b>turns</b> it.</li><li><b>Shift+click</b> or drag a box round things to pick several, then <b>Ctrl+G</b> to group them.</li>
    <li>The panel on the right changes colors, fonts, stitch style and more. <b>Ctrl+Z</b> undoes anything.</li></ul>` },
  { icon: "▶", title: "3. Preview the stitching", body: `<p>The hoop already shows the real stitches. Press <b>Sew it out</b> to watch them being sewn in order. Orange <b>!</b> marks on the hoop warn about trouble spots - click the notes under the stats to see where.</p>` },
  { icon: "💾", title: "4. Save for your machine", body: `<p><b>Write to Designer I disk</b> puts the design straight onto your floppy. Other machines? Choose a file type under <b>Or save a file</b> and press <b>Save file</b>. Nothing leaves the hoop area - if part of the design is outside, Thimble won't let you save it.</p>
    <p class="muted small">You can open this tour again any time from <b>How to</b> at the top.</p>` },
];
let howAt = 0;
function showHow(i = 0) {
  howAt = Math.max(0, Math.min(HOWTO.length - 1, i));
  const s = HOWTO[howAt];
  $("#howStep").innerHTML = `<div class="big">${s.icon}</div><h2>${s.title}</h2>${s.body}`;
  $("#howDots").innerHTML = HOWTO.map((_, k) => `<i class="${k === howAt ? "on" : ""}"></i>`).join("");
  $("#howBack").style.visibility = howAt ? "visible" : "hidden";
  $("#howNext").textContent = howAt === HOWTO.length - 1 ? "Start stitching" : "Next";
  if (!$("#dlgHow").open) $("#dlgHow").showModal();
}
function endHow() { try { localStorage.setItem("thimble.intro", "1"); } catch (e) {} $("#dlgHow").close(); }
$("#btnHowTo").onclick = () => showHow(0);
$("#howBack").onclick = () => showHow(howAt - 1);
$("#howNext").onclick = () => (howAt >= HOWTO.length - 1 ? endHow() : showHow(howAt + 1));
$("#howSkip").onclick = endHow;
$("#dlgHow").addEventListener("cancel", () => { try { localStorage.setItem("thimble.intro", "1"); } catch (e) {} });
const webProjects = {
  all() { try { return JSON.parse(localStorage.getItem("thimble.projects") || "{}"); } catch (e) { return {}; } },
  save(layout) {
    const all = webProjects.all();
    all[layout.name || "My design"] = { layout, modified: Date.now() / 1000 };
    localStorage.setItem("thimble.projects", JSON.stringify(all));
  },
};
async function openProjects() {
  const items = S.meta.hosted
    ? Object.entries(webProjects.all()).sort((a, b) => b[1].modified - a[1].modified).map(([name, p]) => ({ file: name, name, modified: p.modified }))
    : await api("/api/projects");
  $("#projectList").innerHTML = items.map((p) => `<li data-f="${esc(p.file)}">${esc(p.name)}<span>${new Date(p.modified * 1000).toLocaleString()}</span></li>`).join("") ||
    `<li class="muted">No saved projects yet.</li>`;
  $$("#projectList li[data-f]").forEach((li) => (li.onclick = async () => {
    const lay = S.meta.hosted ? webProjects.all()[li.dataset.f].layout : await api("/api/projects/" + encodeURIComponent(li.dataset.f));
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
$("#btnOpen").onclick = () => { const f = $("#openInput"); f.value = ""; f.onchange = () => f.files[0] && openFile(f.files[0]); f.click(); };
$("#btnRecent").onclick = openProjects;
async function saveProject() {
  try {
    if (S.meta.hosted) webProjects.save(JSON.parse(snapshot()));
    else await api("/api/projects", S.layout);
    toast(`Saved project “${esc(S.layout.name)}”${S.meta.hosted ? " in this browser" : ""}.`, "good");
  } catch (e) { toast(esc(e.message), "bad"); }
}
// ---- project files (.thimble): the layout plus every picture it uses, so it opens anywhere
async function projectFileBlob() {
  const lay = JSON.parse(snapshot()), images = {};
  const ids = new Set(lay.elements.filter((e) => e.type === "image").map((e) => e.image_id));
  if (lay.ai_read?.image_id) ids.add(lay.ai_read.image_id);
  for (const id of ids) {
    try {
      const r = await fetch(`/api/image/${encodeURIComponent(id)}`);
      const b = await r.blob();
      if (!r.ok || !b.type.startsWith("image/")) continue;  // picture no longer on the server: don't pack the error
      images[id] = await new Promise((ok) => { const r = new FileReader(); r.onload = () => ok(r.result); r.readAsDataURL(b); });
    } catch (e) { /* picture no longer on the server: the layout still saves */ }
  }
  return new Blob([JSON.stringify({ thimble: 1, saved: new Date().toISOString(), layout: lay, images })], { type: "application/json" });
}
async function saveProjectFile() {
  const name = ((S.layout.name || "My design").replace(/[\\/:*?"<>|]+/g, "").trim() || "My design") + ".thimble";
  busy("now", "Packing up your project…");
  try {
    const blob = await projectFileBlob();
    if (window.showSaveFilePicker) {
      // Chrome/Edge: a real "Save as" window to pick the folder and name
      const h = await window.showSaveFilePicker({ suggestedName: name, types: [{ description: "Thimble project", accept: { "application/json": [".thimble"] } }] });
      const w = await h.createWritable(); await w.write(blob); await w.close();
      const nm = h.name.replace(/\.thimble$/i, "");
      if (nm && nm !== S.layout.name) { beginEdit(); S.layout.name = nm; commit(true); $("#designName").value = nm; save_local(); }
      toast(`Saved ${esc(h.name)}.`, "good");
    } else {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = name; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      toast(`Saved ${esc(name)} to your Downloads.`, "good");
    }
  } catch (e) { if (e.name !== "AbortError") toast(esc(e.message), "bad"); } finally { busy(false); }
}
async function openProjectFile(file) {
  const data = JSON.parse(await file.text());
  const lay = data.layout || data;  // older saves are the bare layout
  if (!lay || !Array.isArray(lay.elements)) throw new Error("That file isn't a Thimble project.");
  // pictures travel inside the file: put them back on the server and point the design at them
  const remap = {};
  let lost = 0;
  for (const [id, url] of Object.entries(data.images || {})) {
    // a missing or damaged picture shouldn't stop the rest of the project from opening
    if (!String(url).startsWith("data:image/")) { lost++; continue; }
    try {
      const b = await (await fetch(url)).blob();
      const ext = (id.split(".").pop() || "png").toLowerCase();
      const info = await uploadPicture(new File([b], "picture." + ext, { type: b.type || "image/png" }));
      remap[id] = info.image_id; S.images[info.image_id] = info;
    } catch (e) { lost++; }
  }
  for (const el of lay.elements) if (el.type === "image" && remap[el.image_id]) el.image_id = remap[el.image_id];
  if (lay.ai_read?.image_id && remap[lay.ai_read.image_id]) lay.ai_read.image_id = remap[lay.ai_read.image_id];
  commit(true); clearMulti();
  S.layout = Object.assign(DEFAULT_LAYOUT(), lay); S.sel = -1;
  await loadImageInfo(); afterLoad();
  toast(`Opened “${esc(S.layout.name || file.name)}”.` + (lost ? ` ${lost} picture${lost === 1 ? " was" : "s were"} missing from the file - the rest is all there.` : ""), "good");
}
async function openFile(file) {
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (ext === "thimble" || ext === "json") {
    busy("now", "Opening " + file.name + "…");
    try { await openProjectFile(file); } catch (e) { toast(esc(e.message), "bad"); } finally { busy(false); }
    return;
  }
  addPicture(file);  // pictures, SVG and embroidery files join the current design
}
$("#btnSaveAs").onclick = saveProjectFile;
$("#btnSave").onclick = async () => {
  try {
    if (S.meta.hosted) webProjects.save(JSON.parse(snapshot()));
    else await api("/api/projects", S.layout);
    toast(`Saved project “${esc(S.layout.name)}”${S.meta.hosted ? " in this browser" : ""}.`, "good");
  } catch (e) { toast(esc(e.message), "bad"); }
};
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
$("#unitsToggle").textContent = UNITS;
$("#unitsToggle").onclick = () => setUnits(UNITS === "mm" ? "in" : "mm");
$("#zoomFit").onclick = () => { S.zoom = 1; S.pan = { x: 0, y: 0 }; draw(); };
cv.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); }); // no browser autoscroll
new ResizeObserver(() => resize()).observe($("#hoopWrap")); // redraw whenever the hoop area changes size
$("#showArt").onchange = draw;
$("#showJumps").onchange = draw;
window.addEventListener("resize", resize);
// left-hand sections fold away: click the heading (remembered for next time)
(() => {
  let folded = {};
  try { folded = JSON.parse(localStorage.getItem("thimble.fold") || "{}"); } catch (e) {}
  $$(".panel.left > section.swatch").forEach((sec, k) => {
    const h = sec.querySelector(":scope > h2");
    if (!h) return;
    const key = h.textContent.trim().split(/\s/)[0] + k;
    h.classList.add("fold-head"); h.tabIndex = 0; h.setAttribute("role", "button");
    const apply = () => { sec.classList.toggle("folded", !!folded[key]); h.setAttribute("aria-expanded", String(!folded[key])); };
    const flip = () => { folded[key] = !folded[key]; apply(); try { localStorage.setItem("thimble.fold", JSON.stringify(folded)); } catch (e) {} resize(); };
    h.onclick = flip;
    h.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } };
    apply();
  });
})();
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y" && !typing) { e.preventDefault(); redo(); return; }
  if (typing) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    e.preventDefault(); clearMulti();
    S.layout.elements.forEach((x) => { if (!x.hidden) multi.add(x.id); });
    S.sel = S.layout.elements.length - 1;
    if (multi.size <= 1) clearMulti();
    renderLayers(); renderProps(); draw(); return;
  }
  if ((e.ctrlKey || e.metaKey) && ["c", "x"].includes(e.key.toLowerCase()) && !window.getSelection()?.toString()) {
    if (selected().length) { e.preventDefault(); copySelected(e.key.toLowerCase() === "x"); }
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") { e.preventDefault(); pasteClip(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "g") {
    e.preventDefault(); e.shiftKey ? ungroupSelected() : groupSelected(); return;
  }
  if (multi.size > 1 && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "d") { e.preventDefault(); duplicateSelected(); return; }
  if (multi.size > 1 && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "m") { e.preventDefault(); mergeSelected(); return; }
  if (multi.size > 1) {
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSelected(); }
    else if (e.key === "Escape") { clearMulti(); S.sel = -1; renderLayers(); renderProps(); draw(); }
    else if (e.key.startsWith("Arrow")) {
      e.preventDefault(); beginEdit();
      const st = e.shiftKey ? 5 : 0.5;
      moveSelected(e.key === "ArrowLeft" ? -st : e.key === "ArrowRight" ? st : 0, e.key === "ArrowUp" ? -st : e.key === "ArrowDown" ? st : 0);
      commit(); draw(); save_local(); scheduleBuild(500);
    }
    return;
  }
  const el = S.layout.elements[S.sel];
  if (!el) return;
  if (nodeEl()) {
    if ((e.key === "Delete" || e.key === "Backspace")) { e.preventDefault(); deleteNode(); return; }
    if (e.key === "Escape") { S.nodeEdit = null; S.nodeSel = null; renderProps(); draw(); return; }
  }
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
    const px = $("#p-x"), py = $("#p-y"); if (px) { px.value = U.show(el.x); py.value = U.show(el.y); }
  }
});
// drop a picture anywhere onto the hoop
$("#hoopWrap").addEventListener("dragover", (e) => e.preventDefault());
$("#hoopWrap").addEventListener("drop", (e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) openFile(f); }); // projects, pictures, SVG or embroidery files

// ------------------------------------------------------------------ start
(async function start() {
  const [meta, fonts] = await Promise.all([api("/api/meta"), api("/api/fonts")]);
  S.meta = meta; S.fonts = fonts;
  if (meta.hosted) { S.meta.ai = !!webKey.key; S.meta.workspace = webKey.ws; }
  if (threadBrand !== MACHINE_BRAND) setThreadBrand(threadBrand);
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
  try { if (!localStorage.getItem("thimble.intro") && !S.layout.elements.length) setTimeout(() => showHow(0), 700); } catch (e) {}
  renderLayers(); renderProps();
  resize();
  scheduleBuild(0);
  if (q.get("sew")) setTimeout(async () => { await build(); startSew(); if (+q.get("sew") > 1) { S.sew.k = Math.floor(S.sew.segs.length * +q.get("sew") / 100); } }, 600);
})();
