def patch(p, pairs):
    s = open(p, encoding='utf8').read()
    for a, b in pairs:
        assert a in s, (p, a[:70])
        s = s.replace(a, b, 1)
    open(p, 'w', encoding='utf8').write(s)


patch('static/index.html', [
('''      <label class="toggle"><input type="checkbox" id="showJumps"> <span>Show jumps</span></label>''',
 '''      <label class="toggle"><input type="checkbox" id="showJumps"> <span>Show jumps</span></label>
      <label class="toggle"><input type="checkbox" id="showProblems" checked> <span>Show problems</span></label>'''),
])
patch('static/app.js', [
('''  $("#notes").innerHTML = (st.warnings || []).map(inUnits).slice(0, 3).map((w) => `<div class="note">${esc(w)}</div>`).join("");''',
 '''  const probs = st.problems || [];
  $("#notes").innerHTML = (st.warnings || []).map(inUnits).slice(0, 3).map((w) => `<div class="note">${esc(w)}</div>`).join("") +
    probs.slice(0, 4).map((p, i) => `<div class="note problem ${p.level}" data-prob="${i}" title="Click to see where"><b>${p.level === "warn" ? "⚠" : "ℹ"}</b> ${esc(inUnits(p.msg))}</div>`).join("");
  $$("#notes [data-prob]").forEach((n) => (n.onclick = () => { S.probFocus = probs[+n.dataset.prob]; draw(); setTimeout(() => { S.probFocus = null; draw(); }, 2500); }));'''),
('''  if ($("#showJumps").checked && S.built) drawJumps();''', '''  if ($("#showJumps").checked && S.built) drawJumps();
  if ($("#showProblems").checked && S.built?.stats?.problems) drawProblems(S.built.stats.problems);'''),
('''function drawJumps() {''', '''function drawProblems(list) {
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
function drawJumps() {'''),
('''const hv = S.layout.hoop.join("x");''', '''const hv = S.layout.hoop.join("x");'''),
])
patch('static/style.css', [
('''.legal-link {''', '''.note.problem { cursor: pointer; }
.note.problem.warn { background: #fdebd9; border-color: #e8702a; }
.note.problem.info { background: #eef1f5; border-color: #9aa8bb; }
.legal-link {'''),
])
print("ok")
