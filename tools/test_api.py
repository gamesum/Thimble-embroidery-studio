"""End-to-end API tests against a running server (python app.py). Uses a temp folder as a
stand-in Designer I disk - never touches a real floppy."""
import io, json, os, shutil, sys, tempfile, time, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
BASE = "http://127.0.0.1:%s" % os.environ.get("THIMBLE_PORT", "5311")
results = []


def req(path, body=None, raw=False, files=None):
    if files:
        boundary = "----thimbletest"
        name, data = files
        payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                   f"Content-Type: image/png\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        r = urllib.request.Request(BASE + path, data=payload, headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    elif body is not None:
        r = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    else:
        r = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            content = resp.read()
            return resp.status, (content if raw else json.loads(content))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("  -- " + str(detail) if detail else ""))


LIMITED = {"name": "Limited Edition", "hoop": [100, 100], "fabric": "fleece", "group_colors": True, "elements": [
    {"type": "shape", "kind": "offset_frame", "width_mm": 86, "height_mm": 40, "stroke_mm": 1.0, "color": "#9a6b3f", "x": 0, "y": 2},
    {"type": "text", "text": "LIMITED", "font": "Alfa Slab One", "height_mm": 12, "letter_spacing": 0.04,
     "color": "#b98a5a", "outline": {"color": "#6b4423", "width_mm": 0.9}, "x": 0, "y": -5},
    {"type": "text", "text": "edition", "font": "Yellowtail", "height_mm": 12, "color": "#1d1d1d", "x": 6, "y": 13},
    {"type": "shape", "kind": "heart", "width_mm": 6, "height_mm": 5.5, "color": "#d62839", "x": 0, "y": -24}]}

if __name__ == "__main__":
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE + "/api/meta", timeout=2); break
        except Exception:
            time.sleep(0.5)

    st, meta = req("/api/meta")
    check("meta", st == 200 and meta["threads"] and meta["formats"] and meta["fabrics"], "%d threads" % len(meta.get("threads", [])))
    st, fonts = req("/api/fonts")
    check("font list", st == 200 and len(fonts) >= 80, "%d fonts" % len(fonts))
    st, png = req("/api/font-preview/" + urllib.request.quote("Great Vibes"), raw=True)
    check("font preview png", st == 200 and png[:4] == b"\x89PNG")
    st, html = req("/", raw=True)
    check("index page", st == 200 and b"Thimble" in html)
    for asset in ("/static/app.js", "/static/style.css", "/fonts/DMSerifDisplay.ttf"):
        st, _ = req(asset, raw=True)
        check("asset " + asset, st == 200)

    # build: text, shapes, outline, script, arcs, rotation, every stitch style
    t = time.time()
    st, b = req("/api/build", LIMITED)
    check("build LIMITED edition", st == 200 and b["stats"]["stitches"] > 3000, "%s stitches in %.1fs" % (b.get("stats", {}).get("stitches"), time.time() - t))
    check("thread chart", st == 200 and len(b["stats"]["colors"]) == 5, [c["thread"] for c in b["stats"]["colors"]])
    check("no warnings", st == 200 and not b["stats"]["warnings"], b["stats"].get("warnings"))
    styles = {}
    for style in ("auto", "satin", "satinfill", "fill", "run"):
        lay = {"hoop": [100, 100], "fabric": "knit", "elements": [
            {"type": "text", "text": "Stitch", "font": "Montserrat Bold", "height_mm": 14, "color": "#224488", "style": style, "x": 0, "y": 0}]}
        st, r = req("/api/build", lay)
        styles[style] = r.get("stats", {}).get("stitches", 0) if st == 200 else -1
    check("all stitch styles build", all(v > 50 for v in styles.values()), styles)
    arcs = {"hoop": [100, 100], "elements": [
        {"type": "text", "text": "EST. 2026", "font": "Graduate", "height_mm": 7, "arc": 120, "color": "#333333", "x": 0, "y": -20},
        {"type": "text", "text": "Hello\nthere", "font": "Pacifico", "height_mm": 9, "arc": -40, "color": "#aa3355", "x": 0, "y": 15, "rotation": 20}]}
    st, r = req("/api/build", arcs)
    check("arc + multi-line + rotation", st == 200 and r["stats"]["stitches"] > 500, r.get("stats", {}).get("size"))
    shapes = {"hoop": [100, 100], "elements": [
        {"type": "shape", "kind": k, "width_mm": 18, "height_mm": 14, "stroke_mm": 1.4, "color": "#557755", "x": -40 + 20 * (i % 5), "y": -20 + 25 * (i // 5)}
        for i, k in enumerate(["heart", "star", "circle", "ring", "rect", "frame", "double_frame", "offset_frame", "line"])]}
    st, r = req("/api/build", shapes)
    check("all 9 shapes", st == 200 and all(e and e["blocks"] for e in r["elements"]), r.get("stats", {}).get("stitches"))
    small = {"hoop": [100, 100], "elements": [{"type": "text", "text": "tiny", "font": "Lato Bold", "height_mm": 3.5, "color": "#000000", "x": 0, "y": 0}]}
    st, r = req("/api/build", small)
    check("small-text warning", st == 200 and any("5 mm" in w for w in r["stats"]["warnings"]), r["stats"]["warnings"])
    out = {"hoop": [100, 100], "elements": [{"type": "text", "text": "OUT OF BOUNDS", "font": "Anton", "height_mm": 20, "color": "#000000", "x": 30, "y": 0}]}
    st, r = req("/api/build", out)
    check("outside-hoop warning", st == 200 and any("outside" in w for w in r["stats"]["warnings"]), r["stats"]["warnings"])
    st, r = req("/api/build", {"hoop": [100, 100], "elements": []})
    check("empty design", st == 200 and r["stats"]["stitches"] == 0)

    # picture upload + trace
    ag = r"C:\Users\18018\Desktop\RIDGELINE\AG_PERMANENT_LIGHTING.png"
    if os.path.exists(ag):
        st, info = req("/api/upload", files=("AG.png", open(ag, "rb").read()))
        check("upload picture", st == 200 and info["colors"], [(c["hex"], c["keep"]) for c in info.get("colors", [])])
        bgs = [c for c in info["colors"] if not c["keep"]]
        check("background auto-skipped", len(bgs) == 1 and bgs[0]["hex"] in ("#1a1a1e", "#1b1b1f", "#191919"), bgs)
        lay = {"hoop": [100, 100], "elements": [dict(type="image", image_id=info["image_id"], width_mm=90, aspect=info["aspect"], colors=info["colors"], x=0, y=0)]}
        st, r = req("/api/build", lay)
        check("trace picture", st == 200 and len(r["stats"]["colors"]) == 2, [(c["thread"], c["stitches"], c["kinds"]) for c in r["stats"]["colors"]])
    st, bad = req("/api/upload", files=("notes.txt", b"hello"))
    check("rejects non-images", st == 400, bad.get("error"))

    # exports: every format, re-read with pyembroidery
    import pyembroidery as pe
    for fmt in ("vp3", "pes", "dst", "jef", "exp"):
        st, data = req("/api/export", {"layout": LIMITED, "format": fmt}, raw=True)
        ok = st == 200 and len(data) > 500
        if ok:
            p = os.path.join(tempfile.gettempdir(), "thimble_test." + fmt)
            open(p, "wb").write(data)
            pat = pe.read(p)
            n = sum(1 for s in pat.stitches if s[2] & pe.COMMAND_MASK == pe.STITCH)
            ok = n > 3000
        check("export " + fmt, ok, "%d bytes, %s stitches" % (len(data) if st == 200 else 0, n if st == 200 else "-"))

    # Designer I disk: into a temp folder standing in for the floppy
    disk = tempfile.mkdtemp(prefix="fake_floppy_")
    st, s0 = req("/api/disk/status", {"target": disk})
    check("disk status (blank)", st == 200 and not s0["layout"])
    st, w1 = req("/api/disk/write", {"layout": LIMITED, "target": disk, "mode": "add", "label": "TEST DISK"})
    check("disk write slot 1", st == 200 and w1["slot"] == 1, w1.get("files"))
    st, w2 = req("/api/disk/write", {"layout": dict(LIMITED, name="Second"), "target": disk, "mode": "add"})
    check("disk add slot 2", st == 200 and w2["slot"] == 2 and w2["status"]["used"] == 2)
    st, w3 = req("/api/disk/write", {"layout": dict(LIMITED, name="Fresh"), "target": disk, "mode": "replace"})
    left = sorted(os.listdir(os.path.join(disk, "MENU_01")))
    check("disk replace", st == 200 and w3["slot"] == 1 and left == ["DES01_01.SHV", "MENU_01.MHV"], left)
    # verify the SHV with the parser built from the factory disk + the stitch round trip
    from engine import d1disk
    shv = open(os.path.join(disk, "MENU_01", "DES01_01.SHV"), "rb").read()
    check("SHV signature", shv.startswith(d1disk.SIG))
    p2 = pe.read(os.path.join(disk, "MENU_01", "DES01_01.SHV"))
    n2 = sum(1 for s in p2.stitches if s[2] & pe.COMMAND_MASK == pe.STITCH)
    check("SHV readable by pyembroidery", n2 > 3000, "%d stitches" % n2)
    st, bad = req("/api/disk/write", {"layout": LIMITED, "target": "Z:\\nope", "mode": "add"})
    check("disk write missing drive -> friendly error", st == 400, bad.get("error"))
    st, dr = req("/api/disk/drives")
    check("drive detection", st == 200 and isinstance(dr["drives"], list), [(d["path"], d.get("floppy"), d.get("ready")) for d in dr["drives"]])
    shutil.rmtree(disk)

    # projects
    st, _ = req("/api/projects", dict(LIMITED, name="API test project"))
    st2, items = req("/api/projects")
    st3, back = req("/api/projects/" + urllib.request.quote("API test project.json"))
    check("project save/list/load", st == 200 and any(i["name"] == "API test project" for i in items) and back.get("elements") == LIMITED["elements"])
    os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "projects", "API test project.json"))

    # AI without a key -> clear message
    st, r = req("/api/ai/analyze", {"image_id": "x.png", "hoop": [100, 100]})
    check("AI bad request -> friendly error", st == 400 and ("API key" in r.get("error", "") or "picture" in r.get("error", "")), r.get("error"))

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n%d / %d passed" % (passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)

