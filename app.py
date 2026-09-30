"""Thimble - Embroidery Studio. Local web app: python app.py, then open http://127.0.0.1:5311"""
import base64, ctypes, io, json, os, string, time, traceback, uuid, webbrowser, threading

from flask import Flask, jsonify, request, send_file, send_from_directory, abort
from PIL import Image, ImageDraw, ImageFont

from engine import design, raster, d1disk
from engine.params import FABRICS, params_for

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
UPLOADS = design.IMAGE_DIR
PROJECTS = os.path.join(OUT, "projects")
EXPORTS = os.path.join(OUT, "exports")
CONFIG = os.path.join(ROOT, "config.json")
for d in (UPLOADS, PROJECTS, EXPORTS):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__, static_folder=os.path.join(ROOT, "static"), static_url_path="/static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def _no_stale_ui(resp):
    # the page's own code must never come from an old browser cache after an update
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


def config():
    try:
        return json.load(open(CONFIG))
    except Exception:
        return {}


def save_config(c):
    json.dump(c, open(CONFIG, "w"), indent=1)


def err(msg, code=400):
    return jsonify(error=msg), code


@app.errorhandler(ValueError)
def on_value_error(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(Exception)
def on_error(e):
    traceback.print_exc()
    return jsonify(error=str(e)), 500


# ----------------------------------------------------------------------------- pages & assets

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/fonts/<path:name>")
def font_file(name):
    return send_from_directory(raster.FONT_DIR, name)


@app.get("/api/fonts")
def api_fonts():
    from engine import embfont
    pro = [dict(name=f["name"], category="Pro digitized", file=None, license=f["license"]) for f in embfont.catalogue()]
    return jsonify(pro + raster.fonts())


_font_previews = {}


@app.get("/api/font-preview/<path:name>")
def font_preview(name):
    """Small PNG of a font's name written in that font (for the picker)."""
    from engine import embfont
    fam = embfont.family(name)
    if fam and name not in _font_previews:
        _font_previews[name] = embfont.preview_png(fam, name.lstrip("✦ "))
    if name not in _font_previews:
        f = next((f for f in raster.fonts() if f["name"] == name), None)
        if not f:
            abort(404)
        font = ImageFont.truetype(os.path.join(raster.FONT_DIR, f["file"]), 30)
        label = f["name"].replace(" Bold", "").replace(" SemiBold", "").replace(" Black", "")
        b = font.getbbox(label)
        im = Image.new("RGBA", (b[2] + 10, 46), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((4, 40 - b[3] + 0), label, font=font, fill=(58, 44, 34, 255))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        _font_previews[name] = buf.getvalue()
    return send_file(io.BytesIO(_font_previews[name]), mimetype="image/png", max_age=86400)


@app.get("/api/stand-in")
def stand_in():
    """The hand-digitized font to suggest for a TrueType font at a given letter height."""
    from engine import embfont
    name = request.args.get("font", "")
    cat = next((f["category"] for f in raster.fonts() if f["name"] == name), "Block")
    try:
        h = float(request.args.get("h", 10))
    except ValueError:
        h = 10.0
    return jsonify(font=embfont.stand_in(name, cat, h))


@app.post("/api/split-drawing")
def split_drawing():
    """One drawing element -> separate drawing elements, one per object."""
    el = request.get_json(force=True)
    # objects first; a drawing that is already one object splits by thread colour
    return jsonify(elements=design.split_vector(el, by_color=True))


@app.get("/api/meta")
def meta():
    from pyembroidery.EmbThreadShv import get_thread_set
    threads = [dict(name=t.description, hex="#%06x" % (t.color & 0xFFFFFF)) for t in get_thread_set()]
    seen, uniq = set(), []
    for t in threads:
        if t["hex"] not in seen:
            seen.add(t["hex"])
            uniq.append(t)
    return jsonify(fabrics={k: v[0] for k, v in FABRICS.items()}, threads=uniq,
                   formats={k: v for k, v in design.FORMATS.items() if v},
                   ai=bool(config().get("anthropic_key") or os.environ.get("ANTHROPIC_API_KEY")),
                   workspace=config().get("anthropic_workspace", ""))


# ----------------------------------------------------------------------------- images

@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f:
        return err("No file received.")
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"):
        return err("Please use a PNG, JPG, BMP, GIF or WEBP picture.")
    image_id = uuid.uuid4().hex[:12] + ext
    path = os.path.join(UPLOADS, image_id)
    f.save(path)
    try:
        im = raster.load_image(path)
    except Exception:
        os.remove(path)
        return err("That file doesn't look like a picture I can read.")
    return jsonify(image_info(image_id, im))


def image_info(image_id, im):
    pal = raster.palette(im)
    small = im.copy()
    small.thumbnail((300, 300))
    import numpy as np
    labels = raster.snap(np.asarray(small), pal)
    bg = raster.background_index(labels)
    counts = np.bincount(labels.ravel(), minlength=len(pal))
    colors = [dict(rgb=list(c), hex=design.rgb_to_hex(c), thread=design.rgb_to_hex(c),
                   keep=i != bg, share=round(float(counts[i]) / labels.size, 3), style="auto")
              for i, c in enumerate(pal)]
    return dict(image_id=image_id, width_px=im.width, height_px=im.height, colors=colors,
                aspect=im.height / im.width)


@app.get("/api/image/<path:image_id>")
def get_image(image_id):
    return send_from_directory(UPLOADS, image_id)


# ----------------------------------------------------------------------------- build

PROGRESS = {}  # job id -> {pct, msg}: long requests report here, the page polls /api/progress


def progress_for(job, lo=0.0, hi=1.0):
    """Callback(fraction, message) that records progress for `job` mapped into [lo, hi]."""
    if not job:
        return None
    if len(PROGRESS) > 200:
        PROGRESS.clear()
    def cb(f, msg=""):
        PROGRESS[job] = dict(pct=int(round(100 * (lo + (hi - lo) * max(0.0, min(1.0, f))))), msg=msg)
    return cb


@app.get("/api/progress/<job>")
def get_progress(job):
    return jsonify(PROGRESS.get(job) or dict(pct=0, msg=""))


def element_payload(layout, progress=None):
    blocks, boxes, P = design.build(layout, progress)
    pattern = design.to_pattern(blocks, P)
    st = design.stats(blocks, layout, pattern)
    # per-element stitches (element-local, un-rotated) so the browser can drag without a rebuild
    per = []
    for idx, el in enumerate(layout.get("elements", [])):
        if el.get("hidden"):
            per.append(None)
            continue
        eb, (w, h) = design.element_blocks(el, P, design.stitch_key(layout))
        per.append(dict(w=w, h=h, blocks=[dict(color=b["color"], objects=[[round(v, 2) for p in o for v in p] for o in b["objects"]])
                                          for b in eb]))
    seq = [dict(color=b["color"], element=b.get("element"), objects=[[round(v, 2) for p in o for v in p] for o in b["objects"]])
           for b in blocks]
    return dict(elements=per, sequence=seq, stats=st)


@app.post("/api/build")
def build():
    layout = request.get_json(force=True)
    t = time.time()
    res = element_payload(layout, progress_for(request.headers.get("X-Job"), 0, 0.97))
    res["ms"] = int((time.time() - t) * 1000)
    return jsonify(res)


class NotSewable(ValueError):
    pass


def pattern_for(layout):
    blocks, boxes, P = design.build(layout, progress_for(request.headers.get("X-Job"), 0, 0.9))
    if not blocks:
        raise NotSewable("The design is empty.")
    hoop = layout.get("hoop", [100, 100])
    if design.off_hoop(blocks, hoop):
        # the machine would hit the hoop frame (or refuse the file) - never hand one out
        raise NotSewable("Part of the design is outside the %gx%g mm hoop. Move or shrink it first." % tuple(hoop))
    return design.to_pattern(blocks, P)


def safe_name(s, default="design"):
    s = "".join(ch for ch in (s or "") if ch.isalnum() or ch in " -_").strip()
    return s or default


@app.post("/api/export")
def export():
    body = request.get_json(force=True)
    layout, fmt = body["layout"], body.get("format", "vp3")
    if fmt not in design.FORMATS or not design.FORMATS[fmt]:
        return err("Unknown format.")
    pat = pattern_for(layout)
    name = safe_name(layout.get("name"))
    path = os.path.join(EXPORTS, "%s.%s" % (name, fmt))
    if fmt == "shv":
        shv, _, _ = d1disk.write_shv(pat, name[:12], origin=(0, 0))
        open(path, "wb").write(shv)
    elif fmt == "zip":
        # a complete Designer I disk (MENU_SEL.PHV + MENU_01) zipped - unzip onto a blank floppy
        import tempfile, zipfile
        tmp = tempfile.mkdtemp(prefix="d1disk_")
        d1disk.write_disk(pat, tmp, name[:12], "MY DESIGNS", mode="replace")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(tmp):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.relpath(full, tmp))
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        design.export(pat, fmt, path)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


# ----------------------------------------------------------------------------- Designer I disk

def removable_drives():
    out = []
    if os.name != "nt":
        return out
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = letter + ":\\"
        if ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) != 2:  # DRIVE_REMOVABLE
            continue
        info = dict(path=root, letter=letter, ready=False, floppy=letter in "AB")
        try:
            total = ctypes.c_ulonglong(0)
            free = ctypes.c_ulonglong(0)
            ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(root), None, ctypes.byref(total), ctypes.byref(free))
            if ok:
                info.update(ready=True, total=total.value, free=free.value, floppy=total.value < 3_000_000)
        except Exception:
            pass
        out.append(info)
    return out


@app.get("/api/disk/drives")
def drives():
    ds = removable_drives()
    for d in ds:
        if d["ready"]:
            try:
                d["status"] = d1disk.disk_status(d["path"])
            except Exception as e:
                d["status"] = dict(error=str(e))
    return jsonify(drives=ds)


@app.post("/api/disk/status")
def disk_status():
    target = request.get_json(force=True).get("target", "")
    if not target or not os.path.isdir(target):
        return err("That drive or folder isn't available. Is the disk in?")
    return jsonify(d1disk.disk_status(target))


@app.post("/api/disk/write")
def disk_write():
    body = request.get_json(force=True)
    target = body.get("target", "")
    if not target or not os.path.isdir(target):
        return err("That drive or folder isn't available. Is the disk in?")
    pat = pattern_for(body["layout"])
    name = safe_name(body["layout"].get("name"), "DESIGN")[:12]
    res = d1disk.write_disk(pat, target, name, safe_name(body.get("label"), "MY DESIGNS")[:11],
                            mode=body.get("mode", "add"))
    res["status"] = d1disk.disk_status(target)
    return jsonify(res)


# ----------------------------------------------------------------------------- projects

@app.get("/api/projects")
def projects():
    items = []
    for f in sorted(os.listdir(PROJECTS), key=lambda f: -os.path.getmtime(os.path.join(PROJECTS, f))):
        if f.endswith(".json"):
            items.append(dict(file=f, name=f[:-5], modified=os.path.getmtime(os.path.join(PROJECTS, f))))
    return jsonify(items)


@app.post("/api/projects")
def save_project():
    layout = request.get_json(force=True)
    name = safe_name(layout.get("name"))
    json.dump(layout, open(os.path.join(PROJECTS, name + ".json"), "w"), indent=1)
    return jsonify(ok=True, file=name + ".json")


@app.get("/api/projects/<path:f>")
def load_project(f):
    p = os.path.join(PROJECTS, os.path.basename(f))
    if not os.path.exists(p):
        return err("Not found", 404)
    return jsonify(json.load(open(p)))


# ----------------------------------------------------------------------------- AI

@app.post("/api/ai/key")
def ai_key():
    body = request.get_json(force=True)
    key = (body.get("key") or "").strip()
    c = config()
    if key:
        c["anthropic_key"] = key
    if "workspace" in body:
        ws = (body.get("workspace") or "").strip()
        if ws:
            c["anthropic_workspace"] = ws
        else:
            c.pop("anthropic_workspace", None)
    save_config(c)
    return jsonify(ai=bool(c.get("anthropic_key")), workspace=c.get("anthropic_workspace", ""))


@app.post("/api/ai/analyze")
def ai_analyze():
    from engine import ai
    body = request.get_json(force=True)
    key = config().get("anthropic_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return err("Add your Anthropic API key in Settings to use picture reading.")
    image_id = os.path.basename(body.get("image_id", ""))
    if not os.path.exists(os.path.join(UPLOADS, image_id)):
        return err("That picture isn't loaded any more - add it again.")
    hoop = body.get("hoop", [100, 100])
    try:
        layout_elements, notes, raw = ai.analyze(os.path.join(UPLOADS, image_id), image_id, hoop, key,
                                                 config().get("anthropic_workspace"),
                                                 progress_for(request.headers.get("X-Job")),
                                                 mode=body.get("mode", "creative"), refine=body.get("refine", ""),
                                                 previous=body.get("previous"))
    except RuntimeError as e:
        return err(str(e))
    return jsonify(elements=layout_elements, notes=notes, raw=raw)


if __name__ == "__main__":
    port = int(os.environ.get("THIMBLE_PORT", 5311))
    if not os.environ.get("THIMBLE_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:%d" % port)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
