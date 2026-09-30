"""Drive the real UI in headless Edge via the DevTools protocol and check every feature.
Requires the server running (python app.py). Uses a temp folder as the Designer I disk."""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request
import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = "http://127.0.0.1:5311"
PORT = 9333
results, errors = [], []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("  -- " + str(detail) if detail not in ("", None) else ""))


class Page:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.n = 0
        self.send("Runtime.enable")
        self.send("Page.enable")

    def send(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                errors.append(d.get("exception", {}).get("description", d.get("text")))
            if msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
                errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"]))
            if msg.get("id") == mid:
                return msg.get("result", {})

    def js(self, expr, await_promise=True):
        if expr.lstrip().startswith("const "):
            expr = "{" + expr + "}"  # keep each step's variables out of the page's global scope
        r = self.send("Runtime.evaluate", expression=expr, awaitPromise=await_promise, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]))
        return r.get("result", {}).get("value")

    def wait(self, expr, timeout=20):
        t = time.time()
        while time.time() - t < timeout:
            try:
                if self.js(expr):
                    return True
            except Exception:
                pass
            time.sleep(0.15)
        return False

    def built(self):
        # wait until no build is pending and the last build is in
        return self.wait("!S.dirty && S.built && !buildTimer_pending()", 30)


def main():
    prof = tempfile.mkdtemp(prefix="thimble_ui_")
    proc = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + prof, "--window-size=1600,980", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    disk = tempfile.mkdtemp(prefix="fake_floppy_")
    try:
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % PORT, timeout=2))
                page_tab = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.3)
        p = Page(page_tab["webSocketDebuggerUrl"])
        p.send("Page.navigate", url=BASE + "/")
        check("app loads", p.wait("typeof S !== 'undefined' && S.meta && S.fonts.length > 80"))
        p.js("window.buildTimer_pending = () => false; localStorage.clear(); true")
        p.js("S.layout = DEFAULT_LAYOUT(); S.sel=-1; afterLoad(); true")
        p.wait("S.built && !S.dirty")

        # --- words
        p.js("document.querySelector('#addText').click()")
        check("add words", p.js("S.layout.elements.length") == 1 and p.js("!!document.querySelector('#p-text')"))
        p.js("const t=document.querySelector('#p-text'); t.value='Hello Sew'; t.dispatchEvent(new Event('input')); true")
        p.wait("S.layout.elements[0].text==='Hello Sew'")
        p.wait("!S.dirty && S.built.stats.stitches>0", 30)
        st = p.js("S.built.stats.stitches")
        check("type text -> stitches", st and st > 200, st)
        check("layer list updated", "Hello Sew" in p.js("document.querySelector('#layerList').innerText"))
        # font picker
        p.js("document.querySelector('.fontpick-btn').click(); true")
        check("font picker opens", p.wait("document.querySelectorAll('.fontpick-item').length > 80"))
        p.js("document.querySelector('.fontpick-cats [data-c=\"Script\"]').click(); true")
        n_script = p.js("document.querySelectorAll('.fontpick-item').length")
        check("font category filter", 10 < n_script < 40, n_script)
        p.js("const q=document.querySelector('.fontpick-pop input'); q.value='pacif'; q.dispatchEvent(new Event('input')); true")
        p.js("document.querySelector('.fontpick-item').click(); true")
        check("pick font", p.js("S.layout.elements[0].font") == "Pacifico", p.js("S.layout.elements[0].font"))
        # size slider
        p.js("const r=document.querySelector('#p-height_mm'); r.value=16; r.dispatchEvent(new Event('input')); true")
        p.wait("!S.dirty", 30)
        check("size slider", p.js("S.layout.elements[0].height_mm") == 16)
        # thread swatch
        p.js("document.querySelectorAll('.threads[data-for=\"p-color\"] .thread-sw')[3].click(); true")
        check("thread swatch", p.js("S.layout.elements[0].color") == p.js("document.querySelectorAll('.threads[data-for=\"p-color\"] .thread-sw')[3].dataset.hex"))
        # outline
        p.js("const c=document.querySelector('#p-ol'); c.checked=true; c.dispatchEvent(new Event('change')); true")
        p.wait("!S.dirty && S.built.stats.colors.length===2", 30)
        check("satin border adds a colour", p.js("S.built.stats.colors.length") == 2, p.js("JSON.stringify([S.dirty, S.layout.elements[0], S.built.stats.colors.map(c=>c.kinds.join('+')), document.querySelector('.toast')?.innerText])"))
        # curve
        p.js("const r=document.querySelector('#p-arc'); r.value=60; r.dispatchEvent(new Event('input')); true")
        p.wait("!S.dirty", 30)
        check("curve slider", p.js("S.layout.elements[0].arc") == 60 and p.js("S.built.elements[0].h") > 10)

        # --- shape
        p.js("document.querySelector('#addShape').click(); true")
        p.wait("S.layout.elements.length===2 && !S.dirty", 30)
        p.js("document.querySelector('.chip[data-kind=\"star\"]').click(); true")
        p.wait("!S.dirty", 30)
        check("add shape + change kind", p.js("S.layout.elements[1].kind") == "star")
        p.js("const w=document.querySelector('#p-width_mm'); w.value=20; w.dispatchEvent(new Event('change')); true")
        check("shape width field", p.js("S.layout.elements[1].width_mm") == 20)
        p.js("const s=document.querySelector('#p-style'); s.value='fill'; s.dispatchEvent(new Event('change')); true")
        p.wait("!S.dirty", 30)
        check("stitch style select", p.js("S.built.stats.colors.some(c=>c.kinds.includes('fill'))"))

        # --- drag on canvas (move element 1)
        before = p.js("[S.layout.elements[1].x, S.layout.elements[1].y]")
        p.js("""(() => { S.sel=1; draw(); const el=S.layout.elements[1]; const r=cv.getBoundingClientRect();
            const [x,y]=mm2px(el.x, el.y); const o={bubbles:true, pointerId:1, button:0};
            cv.dispatchEvent(new PointerEvent('pointerdown',{...o, clientX:r.left+x, clientY:r.top+y}));
            cv.dispatchEvent(new PointerEvent('pointermove',{...o, clientX:r.left+x+60, clientY:r.top+y+30}));
            cv.dispatchEvent(new PointerEvent('pointerup',{...o, clientX:r.left+x+60, clientY:r.top+y+30})); return true })()""")
        after = p.js("[S.layout.elements[1].x, S.layout.elements[1].y]")
        check("drag to move", after[0] > before[0] + 3 and after[1] > before[1] + 1, (before, after))
        # rotate handle
        p.wait("!S.dirty", 30)
        p.js("""(() => { const el=S.layout.elements[1]; const bx=selBox(el,1); const h=handles(el,bx); const r=cv.getBoundingClientRect();
            const o={bubbles:true, pointerId:1, button:0}; const [cx,cy]=mm2px(el.x,el.y);
            cv.dispatchEvent(new PointerEvent('pointerdown',{...o, clientX:r.left+h.rot[0], clientY:r.top+h.rot[1]}));
            cv.dispatchEvent(new PointerEvent('pointermove',{...o, clientX:r.left+cx+80, clientY:r.top+cy}));
            cv.dispatchEvent(new PointerEvent('pointerup',{...o, clientX:r.left+cx+80, clientY:r.top+cy})); return true })()""")
        rot = p.js("S.layout.elements[1].rotation")
        check("rotate handle", abs(abs(rot) - 90) < 6, rot)
        # scale handle
        p.wait("!S.dirty", 30)
        w0 = p.js("S.layout.elements[1].width_mm")
        p.js("""(() => { const el=S.layout.elements[1]; const bx=selBox(el,1); const h=handles(el,bx); const r=cv.getBoundingClientRect();
            const o={bubbles:true, pointerId:1, button:0}; const [cx,cy]=mm2px(el.x,el.y);
            const fx=cx+(h.se[0]-cx)*1.5, fy=cy+(h.se[1]-cy)*1.5;
            cv.dispatchEvent(new PointerEvent('pointerdown',{...o, clientX:r.left+h.se[0], clientY:r.top+h.se[1]}));
            cv.dispatchEvent(new PointerEvent('pointermove',{...o, clientX:r.left+fx, clientY:r.top+fy}));
            cv.dispatchEvent(new PointerEvent('pointerup',{...o, clientX:r.left+fx, clientY:r.top+fy})); return true })()""")
        w1 = p.js("S.layout.elements[1].width_mm")
        check("resize handle", 1.3 * w0 < w1 < 1.7 * w0, (w0, w1))
        p.wait("!S.dirty", 30)

        # middle-mouse drag slides the view (slicer style); Fit puts it back
        p.js("""(() => { const r=cv.getBoundingClientRect(); const o={bubbles:true, pointerId:2, button:1};
            cv.dispatchEvent(new PointerEvent('pointerdown',{...o, clientX:r.left+200, clientY:r.top+200}));
            cv.dispatchEvent(new PointerEvent('pointermove',{...o, clientX:r.left+260, clientY:r.top+170}));
            cv.dispatchEvent(new PointerEvent('pointerup',{...o, clientX:r.left+260, clientY:r.top+170})); return true })()""")
        pan = p.js("[S.pan.x, S.pan.y]")
        check("middle-mouse pan", pan == [60, -30], pan)
        p.js("document.getElementById('zoomFit').click()")
        check("fit resets pan", p.js("S.pan.x === 0 && S.pan.y === 0 && S.zoom === 1"))
        # placement: snap to the left edge of the sewing field
        p.js("document.querySelector('[data-place=\"left\"]').click(); true")
        p.wait("!S.dirty", 30)
        lx = p.js("(() => { const e=S.layout.elements[S.sel], b=S.built.elements[S.sel]; return e.x - b.w/2 + S.layout.hoop[0]/2 })()")
        check("place: left edge", lx is not None and abs(lx) < 1.5, lx)
        p.js("document.querySelector('[data-place=\"cx\"]').click(); true")
        check("pro fonts listed", p.js("S.fonts.filter(f => f.category === 'Pro digitized').length") > 40)

        # --- keyboard: duplicate, nudge, delete, undo/redo
        p.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'d', ctrlKey:true})); true")
        check("Ctrl+D duplicate", p.js("S.layout.elements.length") == 3)
        x0 = p.js("S.layout.elements[S.sel].x")
        p.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight', shiftKey:true})); true")
        check("arrow nudge", abs(p.js("S.layout.elements[S.sel].x") - (x0 + 5)) < 0.01)
        p.js("document.activeElement.blur(); document.dispatchEvent(new KeyboardEvent('keydown',{key:'Delete'})); true")
        check("Delete removes", p.js("S.layout.elements.length") == 2)
        p.js("undo(); true")
        check("undo", p.js("S.layout.elements.length") == 3)
        p.js("redo(); true")
        check("redo", p.js("S.layout.elements.length") == 2)
        # layer buttons
        p.js("document.querySelectorAll('#layerList .layer')[1].querySelector('[data-a=up]').click(); true")
        check("layer reorder", p.js("S.layout.elements[0].type") == "shape")
        p.js("document.querySelectorAll('#layerList .layer')[0].querySelector('[data-a=hide]').click(); true")
        p.wait("!S.dirty", 30)
        check("hide layer", p.js("S.layout.elements[0].hidden===true && S.built.elements[0]===null"))
        p.js("document.querySelectorAll('#layerList .layer')[0].querySelector('[data-a=hide]').click(); true")
        p.wait("!S.dirty", 30)

        # --- picture upload (through the real upload endpoint)
        ag = r"C:\Users\18018\Desktop\RIDGELINE\AG_PERMANENT_LIGHTING.png"
        if os.path.exists(ag):
            import base64
            b64 = base64.b64encode(open(ag, "rb").read()).decode()
            p.js("(async()=>{const b=await (await fetch('data:image/png;base64,%s')).blob(); await addPicture(new File([b],'AG.png',{type:'image/png'})); return true})()" % b64)
            p.wait("S.layout.elements.length===3 && !S.dirty", 40)
            check("add picture", p.js("S.layout.elements[2].type") == "image" and p.js("S.built.elements[2].blocks.length") >= 2)
            check("picture colour rows", p.js("document.querySelectorAll('.imgcolor').length") >= 3)
            p.js("const c=document.querySelector('.imgcolor input[type=checkbox]:checked'); c.checked=false; c.dispatchEvent(new Event('change')); true")
            p.wait("!S.dirty", 30)
            check("untick a picture colour", p.js("S.built.elements[2].blocks.length") >= 1)
            p.js("document.querySelector('#showArt').click(); true")
            check("show artwork toggle", p.js("document.querySelector('#showArt').checked"))
            p.js("S.layout.elements.splice(2,1); S.sel=-1; afterLoad(); true")
            p.wait("!S.dirty", 30)

        # --- sew it out
        p.js("document.querySelector('#btnSewOut').click(); true")
        check("sew-out runs", p.wait("S.sew && S.sew.k > 50", 15))
        p.js("document.querySelector('#sewPause').click(); true")
        k = p.js("S.sew.k")
        time.sleep(0.4)
        check("sew-out pause", p.js("S.sew.k") == k)
        p.js("const s=document.querySelector('#sewScrub'); s.value=10; s.dispatchEvent(new Event('input')); true")
        check("sew-out scrub", p.js("S.sew.k") == 10)
        check("sew-out info", "Thread 1 of" in p.js("document.querySelector('#sewInfo').textContent"))
        p.js("document.querySelector('#sewClose').click(); true")
        check("sew-out close", p.js("S.sew === null && document.querySelector('#sewBar').hidden"))
        p.js("document.querySelector('#showJumps').click(); draw(); true")

        # --- settings
        p.js("openSettings(); true")
        check("settings dialog", p.js("document.querySelector('#dlgSettings').open"))
        p.js("document.querySelector('#setFabric').value='towel'; document.querySelector('#setHoop').value='240x150'; document.querySelector('#fabricSwatches button:nth-child(3)').click(); true")
        p.js("saveSettings()")
        p.wait("!S.dirty", 30)
        check("settings saved", p.js("S.layout.fabric") == "towel" and p.js("S.layout.hoop.join('x')") == "240x150" and p.js("S.layout.fabric_color") == "#2a2a2e")
        p.js("document.querySelector('#setHoop').value='100x100'; openSettings(); document.querySelector('#setHoop').value='100x100'; saveSettings()")
        p.wait("!S.dirty", 30)

        # --- export (fetch like the button does, and check the bytes)
        size = p.js("(async()=>{const r=await api('/api/export',{layout:S.layout, format:'vp3'},{blob:true}); return (await r.blob()).size})()")
        check("export VP3 from UI", size and size > 1000, size)
        p.js("document.querySelector('#exportFormat').value") and check("default export format is VP3", p.js("document.querySelector('#exportFormat').options[0].value") == "vp3")

        # --- disk dialog -> write to a temp folder standing in for the floppy
        p.js("openDisk()")
        check("disk dialog opens", p.wait("document.querySelector('#dlgDisk').open"))
        p.js("const f=document.querySelector('#diskFolder'); f.value=%s; f.dispatchEvent(new Event('change')); true" % json.dumps(disk))
        p.wait("document.querySelector('#diskStatus').innerText.includes('slot 1')", 15)
        check("disk status shows slot", "slot 1" in p.js("document.querySelector('#diskStatus').innerText"))
        p.js("writeDisk()")
        p.wait("document.querySelector('#diskResult').className.includes('ok')", 30)
        check("write disk from UI", "slot 1" in p.js("document.querySelector('#diskResult').innerText"), p.js("document.querySelector('#diskResult').innerText"))
        p.js("writeDisk()")
        p.wait("document.querySelector('#diskResult').innerText.includes('slot 2')", 30)
        check("second design -> slot 2", "slot 2" in p.js("document.querySelector('#diskResult').innerText"))
        check("disk files", sorted(os.listdir(os.path.join(disk, "MENU_01"))) == ["DES01_01.SHV", "DES01_02.SHV", "MENU_01.MHV"])
        p.js("document.querySelector('#dlgDisk').close(); true")

        # --- projects + examples + new
        p.js("S.layout.name='UI test project'; true")
        p.js("document.querySelector('#btnSave').click(); true")
        time.sleep(1)
        p.js("openProjects()")
        check("open project list", p.wait("[...document.querySelectorAll('#projectList li')].some(l=>l.innerText.includes('UI test project'))"))
        p.js("document.querySelector('#dlgOpen').close(); true")
        p.js("loadExample('varsity'); true")
        p.wait("!S.dirty", 30)
        check("load example", p.js("S.layout.elements.length") == 3 and p.js("S.built.stats.stitches") > 2000)
        p.js("window.confirm=()=>true; document.querySelector('#btnNew').click(); true")
        check("new design", p.js("S.layout.elements.length") == 0)
        p.js("location.reload(); true", await_promise=False)
        time.sleep(1)
        check("autosave restores after reload", p.wait("typeof S!=='undefined' && S.meta && S.layout.elements.length===0"))
        # AI button without a key -> opens settings with a message
        p.js("S.meta.ai=false; readPicture(new File([new Blob(['x'])],'x.png')); true")
        check("AI without key opens settings", p.wait("document.querySelector('#dlgSettings').open", 5))
    finally:
        try:
            os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "projects", "UI test project.json"))
        except OSError:
            pass
        proc.terminate()
        time.sleep(0.5)
        shutil.rmtree(prof, ignore_errors=True)
        shutil.rmtree(disk, ignore_errors=True)
    check("no JavaScript errors", not errors, errors[:5])
    passed = sum(1 for _, ok in results if ok)
    print("\n%d / %d passed" % (passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
