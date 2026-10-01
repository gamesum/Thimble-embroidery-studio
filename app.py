"""Thimble - Embroidery Studio. Local web app: python app.py, then open http://127.0.0.1:5311"""
import re, base64, ctypes, io, json, os, string, time, traceback, uuid, webbrowser, threading

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
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # pictures and disk files; nothing legitimate is bigger

# THIMBLE_HOSTED=1: running as a public website. Then nothing personal lives on the server:
# each visitor's API key stays in their browser (sent only with their own AI requests), projects
# are saved in their browser, and floppy disks are written by the browser (see /api/disk/build).
HOSTED = os.environ.get("THIMBLE_HOSTED") == "1"


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


PIC_ID = re.compile(r"^[0-9a-f]{12}\.(png|jpe?g|bmp|gif|webp|tiff?|heic|heif)$")


def _pictures_in(body):
    """Picture files a request needs: pictures on the hoop, or the one picture it works on."""
    ids = set()
    if not isinstance(body, dict):
        return ids
    lay = body.get("layout") if isinstance(body.get("layout"), dict) else body
    for el in lay.get("elements") or []:
        if isinstance(el, dict) and el.get("type") == "image" and el.get("image_id"):
            ids.add(os.path.basename(str(el["image_id"])))
    if body.get("type") == "image" or request.path == "/api/ai/analyze":
        if body.get("image_id"):
            ids.add(os.path.basename(str(body["image_id"])))
    return ids


@app.before_request
def _pictures_present():
    """The website's server forgets uploads when it restarts. Say which pictures are gone (409)
    so the browser can send its own copies back and try again, instead of failing deep inside."""
    if request.method != "POST" or not request.path.startswith("/api/") or not request.is_json:
        return None
    body = request.get_json(silent=True)
    gone = sorted(i for i in _pictures_in(body) if not os.path.exists(os.path.join(UPLOADS, i)))
    if gone:
        return jsonify(error="A picture in this design needs to be added again.", missing=gone), 409
    return None


@app.errorhandler(ValueError)
def on_value_error(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(Exception)
def on_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):  # "not found" etc. are answers, not crashes
        if request.path.startswith("/api/"):
            return jsonify(error=e.description or e.name), e.code
        return e
    traceback.print_exc()
    try:  # keep the details for troubleshooting (the console window is easy to lose)
        with open(os.path.join(OUT, "error.log"), "a", encoding="utf-8") as fh:
            fh.write("\n=== %s %s %s\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), request.method, request.path, traceback.format_exc()))
    except Exception:
        pass
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


@app.post("/api/split-picture")
def split_picture():
    """One picture -> one picture per object (things that don't touch: the ghost, each star).
    Each piece is cropped from the original, everything else in its box painted the background
    colour, and placed exactly where it was. Colour settings and stitch mode carry over."""
    import numpy as np, cv2, math
    el = request.get_json(force=True)
    if el.get("type") != "image":
        return err("Only pictures can be split here.")
    path = os.path.join(UPLOADS, os.path.basename(el.get("image_id", "")))
    if not os.path.exists(path):
        return err("That picture isn't on the server any more - add it again.")
    im = raster.load_image(path)
    scale = min(1.0, 1600.0 / max(im.size))  # work at up to 1600 px
    if scale < 1:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
    arr = np.asarray(im)
    cols = el.get("colors") or []
    pal = [tuple(c["rgb"]) for c in cols] or raster.palette(im)
    labels = raster.snap(arr, pal)
    bg = raster.background_index(labels)
    keep = [bool(c.get("keep", i != bg)) for i, c in enumerate(cols)] or [i != bg for i in range(len(pal))]
    fg = np.isin(labels, [i for i, k in enumerate(keep) if k]).astype(np.uint8)
    W, H = im.size
    width_mm = float(el.get("width_mm") or 80)
    px_mm = W / width_mm
    # parts closer than ~1 mm belong together; specks under ~2 mm2 are dropped
    gap = max(1, int(round(1.0 * px_mm)))
    joined = cv2.dilate(fg, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * gap + 1, 2 * gap + 1)))
    n, cc, st, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    min_px = 2.0 * px_mm * px_mm
    pieces = [cc == j for j in range(1, n) if (fg[cc == j]).sum() >= min_px]
    how = "objects"
    if len(pieces) < 2:
        # one object: split it by thread colour instead (press again on a colour to split its objects)
        pieces = [labels == i for i, k in enumerate(keep) if k and (labels == i).sum() >= min_px]
        how = "colors"
        if len(pieces) < 2:
            return jsonify(elements=[])
    bg_rgb = tuple(int(v) for v in pal[bg]) if bg is not None and bg < len(pal) else (255, 255, 255)
    rot = math.radians(float(el.get("rotation") or 0))
    c, s_ = math.cos(rot), math.sin(rot)
    out = []
    for n_i, pm in enumerate(sorted(pieces, key=lambda m: -int(m.sum()))):
        ys, xs = np.nonzero(pm)
        x, y, w, h = int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)
        pad = gap + 2
        x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(H, y + h + pad)
        crop = arr[y0:y1, x0:x1].copy()
        crop[~pm[y0:y1, x0:x1]] = bg_rgb
        image_id = uuid.uuid4().hex[:12] + ".png"
        Image.fromarray(crop).save(os.path.join(UPLOADS, image_id))
        # centre offset in mm (picture-local), then turned with the picture
        dx = ((x0 + x1) / 2 - W / 2) / px_mm
        dy = ((y0 + y1) / 2 - H / 2) / px_mm
        piece = {k: v for k, v in el.items() if k not in ("id", "image_id", "width_mm", "aspect", "x", "y", "name")}
        piece.update(image_id=image_id, width_mm=round((x1 - x0) / px_mm, 2), aspect=(y1 - y0) / (x1 - x0),
                     x=round(float(el.get("x") or 0) + dx * c - dy * s_, 2), y=round(float(el.get("y") or 0) + dx * s_ + dy * c, 2),
                     name="%s %d" % (el.get("name") or "Picture", n_i + 1))
        out.append(piece)
    return jsonify(elements=out, how=how)


@app.post("/api/merge-drawings")
def merge_drawings():
    """Several pieces -> one. Drawings join into one drawing; pictures are laid onto one picture
    (each keeps its place); pictures mixed with drawings are traced to lines and joined."""
    els = (request.get_json(force=True) or {}).get("elements") or []
    if len(els) < 2:
        return err("Pick two or more pieces to merge.")
    if any(e.get("type") not in ("vector", "image") for e in els):
        return err("Words and shapes can't be merged into a drawing - group them instead (Ctrl+G) to keep them together.")
    if all(e.get("type") == "vector" for e in els):
        return jsonify(element=design.merge_vectors(els))
    if all(e.get("type") == "image" for e in els):
        return jsonify(element=merge_pictures(els))
    lines = [e if e.get("type") == "vector" else design.trace_to_vector(e) for e in els]
    lines = [v for v in lines if v]
    return jsonify(element=design.merge_vectors(lines), note="Pictures were turned into lines so they could join the drawings.")


def merge_pictures(els):
    import numpy as np, math
    placed = []
    for e in els:
        path = os.path.join(UPLOADS, os.path.basename(e.get("image_id", "")))
        im = raster.load_image(path)
        w = float(e.get("width_mm") or 40)
        h = w * im.height / im.width
        kx, ky = float(e.get("stretch_x") or 1), float(e.get("stretch_y") or 1)
        placed.append((e, im, w * kx, h * ky))
    xs, ys = [], []
    for e, im, w, h in placed:
        a = math.radians(float(e.get("rotation") or 0))
        hw = (abs(w * math.cos(a)) + abs(h * math.sin(a))) / 2
        hh = (abs(w * math.sin(a)) + abs(h * math.cos(a))) / 2
        xs += [float(e.get("x") or 0) - hw, float(e.get("x") or 0) + hw]
        ys += [float(e.get("y") or 0) - hh, float(e.get("y") or 0) + hh]
    X0, X1, Y0, Y1 = min(xs), max(xs), min(ys), max(ys)
    s = min(1600.0 / max(X1 - X0, Y1 - Y0), max(im.width / w for _, im, w, _ in placed))  # px per mm
    canvas = None
    for e, im, w, h in placed:
        cols = e.get("colors") or []
        pal = [tuple(c["rgb"]) for c in cols] or raster.palette(im)
        labels = raster.snap(np.asarray(im), pal)
        bg = raster.background_index(labels)
        if canvas is None:
            bgc = tuple(int(v) for v in pal[bg]) if bg is not None and bg < len(pal) else (255, 255, 255)
            canvas = Image.new("RGB", (max(8, int((X1 - X0) * s)), max(8, int((Y1 - Y0) * s))), bgc)
        fg = Image.fromarray(((labels != bg) * 255).astype("uint8")) if bg is not None else Image.new("L", im.size, 255)
        size = (max(1, int(w * s)), max(1, int(h * s)))
        im2, fg2 = im.resize(size, Image.LANCZOS), fg.resize(size, Image.LANCZOS)
        rot = float(e.get("rotation") or 0)
        if rot:
            im2 = im2.rotate(-rot, expand=True, resample=Image.BICUBIC)
            fg2 = fg2.rotate(-rot, expand=True, resample=Image.BICUBIC)
        cx, cy = (float(e.get("x") or 0) - X0) * s, (float(e.get("y") or 0) - Y0) * s
        canvas.paste(im2, (int(cx - im2.width / 2), int(cy - im2.height / 2)), fg2)
    image_id = uuid.uuid4().hex[:12] + ".png"
    canvas.save(os.path.join(UPLOADS, image_id))
    info = image_info(image_id, canvas)
    first = els[0]
    out = {k: v for k, v in first.items() if k in ("trace", "trace_line", "trace_width", "trace_min", "smooth", "eyes")}
    out.update(type="image", image_id=image_id, width_mm=round(X1 - X0, 2), aspect=info["aspect"], colors=info["colors"],
               x=round((X0 + X1) / 2, 2), y=round((Y0 + Y1) / 2, 2), rotation=0, name=first.get("name") or "Picture")
    return out


@app.post("/api/trace-to-lines")
def trace_to_lines():
    """A traced picture -> an editable drawing of polylines."""
    el = request.get_json(force=True)
    if el.get("type") != "image":
        return err("Only traced pictures can be turned into lines.")
    v = design.trace_to_vector(el)
    if not v:
        return err("There were no lines to turn into a drawing.")
    return jsonify(element=v)


_THREADS = None


def thread_brands():
    global _THREADS
    if _THREADS is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine", "data", "threads.json")
        _THREADS = json.load(open(p, encoding="utf8"))["brands"]
    return _THREADS


@app.get("/api/threads/<path:brand>")
def threads_for(brand):
    """One thread brand's colour chart (from Ink/Stitch): [{hex, name, num}]."""
    rows = thread_brands().get(brand)
    if rows is None:
        return err("Unknown thread brand.", 404)
    resp = jsonify(threads=[dict(hex=h, name=("%s %s" % (num, n)).strip() if num else n, num=num, label=n) for h, n, num in rows])
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.post("/api/feedback")
def feedback():
    """Bug reports and feature requests: appended to output/feedback.jsonl and printed (the website's log keeps it)."""
    j = request.get_json(force=True, silent=True) or {}
    msg = str(j.get("message") or "").strip()[:4000]
    if not msg:
        return jsonify(error="Please write a few words first."), 400
    rec = dict(time=time.strftime("%Y-%m-%d %H:%M:%S"), kind="bug" if j.get("kind") == "bug" else "idea", message=msg,
               contact=str(j.get("contact") or "")[:200], page=str(j.get("page") or "")[:300],
               agent=str(j.get("agent") or "")[:300], hosted=HOSTED)
    d = j.get("design")
    if d is not None:
        txt = json.dumps(d)
        rec["design"] = d if len(txt) < 200000 else "(too large to attach)"
    line = json.dumps(rec, ensure_ascii=False)
    try:
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, "feedback.jsonl"), "a", encoding="utf8") as f:
            f.write(line + chr(10))
    except Exception:
        pass
    print("FEEDBACK " + line, flush=True)
    threading.Thread(target=_mail_feedback, args=(rec,), daemon=True).start()
    return jsonify(ok=True)


def _mail_feedback(rec):
    """Email the report to the owner. Needs a Gmail address + app password: THIMBLE_MAIL_USER / THIMBLE_MAIL_PASS /
    THIMBLE_MAIL_TO environment variables (the website) or mail_user / mail_pass / mail_to in config.json (this computer)."""
    c = config()
    user = os.environ.get("THIMBLE_MAIL_USER") or c.get("mail_user")
    pw = os.environ.get("THIMBLE_MAIL_PASS") or c.get("mail_pass")
    to = os.environ.get("THIMBLE_MAIL_TO") or c.get("mail_to") or user
    if not (user and pw and to):
        return
    try:
        import smtplib
        from email.message import EmailMessage
        nl = chr(10)
        m = EmailMessage()
        m["Subject"] = "Thimble %s: %s" % ("bug report" if rec["kind"] == "bug" else "feature request", rec["message"][:60].replace(nl, " "))
        m["From"], m["To"] = user, to
        if rec.get("contact"):
            m["Reply-To"] = rec["contact"]
        body = [rec["message"], "", "---", "From: %s" % (rec.get("contact") or "(no email given)"), "When: %s" % rec["time"],
                "Where: %s" % rec.get("page"), "Browser: %s" % rec.get("agent"), "On the website: %s" % rec.get("hosted")]
        m.set_content(nl.join(body))
        if isinstance(rec.get("design"), dict):
            m.add_attachment(json.dumps(rec["design"], indent=1).encode("utf8"), maintype="application", subtype="json", filename="design.json")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as srv:
            srv.login(user, pw)
            srv.send_message(m)
    except Exception as e:
        print("FEEDBACK mail failed: %s" % e, flush=True)


@app.get("/api/legal")
def legal():
    """Every embroidery font with its author's license (for the About & licenses page)."""
    out = []
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts_emb")
    for d in sorted(os.listdir(base)):
        p = os.path.join(base, d, "font.json")
        if not os.path.exists(p):
            continue
        try:
            j = json.load(open(p, encoding="utf8"))
        except Exception:
            continue
        lic = (j.get("font_license") or "see Ink/Stitch").replace("SCC-BY-SA", "CC BY-SA").replace("Mublic Domain", "Public Domain")
        out.append(dict(name=j.get("name") or d, license=lic,
                        original=j.get("original_font") or "", url=j.get("original_font_url") or ""))
    return jsonify(fonts=out)


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
                   ai=False if HOSTED else bool(config().get("anthropic_key") or os.environ.get("ANTHROPIC_API_KEY")),
                   workspace="" if HOSTED else config().get("anthropic_workspace", ""),
                   hosted=HOSTED, brands=sorted(thread_brands()))


# ----------------------------------------------------------------------------- images

@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f:
        return err("No file received.")
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".heic", ".heif"):
        return err("Please use a PNG, JPG, HEIC, WEBP, BMP, GIF or TIFF picture - or an SVG or embroidery file.")
    image_id = uuid.uuid4().hex[:12] + ext
    restore = request.form.get("restore_id", "")
    if restore and PIC_ID.match(restore):
        image_id = restore  # the browser putting back a picture the server forgot
    path = os.path.join(UPLOADS, image_id)
    f.save(path)
    try:
        im = raster.load_image(path)
    except Exception:
        os.remove(path)
        return err("That file doesn't look like a picture I can read.")
    return jsonify(image_info(image_id, im))


@app.post("/api/import")
def import_file():
    """Embroidery files (PES, DST, JEF, VP3, SHV, ...) and SVG vector art -> a ready element."""
    from engine import importers
    import tempfile
    f = request.files.get("file")
    if not f:
        return err("No file received.")
    name, ext = os.path.splitext(os.path.basename(f.filename or "file"))
    ext = ext.lower()
    if ext != ".svg" and ext not in importers.EMB_EXTS:
        return err("Thimble can't open %s files." % (ext or "those"))
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    try:
        f.save(tmp.name)
        tmp.close()
        hoop = [float(v) for v in (request.form.get("hoop") or "100,100").split(",")[:2]]
        el = importers.read_svg(tmp.name, hoop, name) if ext == ".svg" else importers.read_embroidery(tmp.name, name)
    except ValueError as e:
        return err(str(e))
    except Exception:
        return err("That file couldn't be read - it may be damaged or a variant Thimble doesn't know.")
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    return jsonify(element=el)


@app.post("/api/stitches-to-image")
def stitches_to_image():
    """A stitch file drawn as a flat-colour picture (each thread one solid colour, on a background
    unlike any of them), saved as an upload so it can be traced or read by the AI like any picture."""
    el = request.get_json(force=True)
    if el.get("type") != "stitches":
        return err("Only stitch files can be turned into a picture.")
    el0 = dict(el, x=0, y=0, rotation=0)
    blocks, (w, h) = design.element_blocks(el0, params_for("knit"), "to-image")
    if not blocks or w <= 0 or h <= 0:
        return err("That stitch file is empty.")
    pad = 3.0
    scale = max(6.0, min(16.0, 1500.0 / max(w, h)))  # px per mm: ~1500 px on the long side
    W, H = int((w + 2 * pad) * scale), int((h + 2 * pad) * scale)
    cols = [design.hex_to_rgb(b["color"]) for b in blocks]
    cands = [(255, 255, 255), (0, 0, 0), (128, 128, 128), (0, 170, 255), (255, 0, 200), (0, 200, 90)]
    bg = max(cands, key=lambda c: min((sum((a - b) ** 2 for a, b in zip(c, t)) for t in cols), default=1e9))
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    tw = max(2, int(round(0.5 * scale)))
    for b, rgb in zip(blocks, cols):
        for o in b["objects"]:
            pts = [(W / 2 + x * scale, H / 2 + y * scale) for x, y in o]
            if len(pts) > 1:
                d.line(pts, fill=rgb, width=tw, joint="curve")
    image_id = uuid.uuid4().hex[:12] + ".png"
    im.save(os.path.join(UPLOADS, image_id))
    info = image_info(image_id, im)
    info.update(width_mm=round(w + 2 * pad, 1), height_mm=round(h + 2 * pad, 1))
    return jsonify(info)


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
    if len(PROGRESS) > 200:  # forget finished jobs first
        for k in [k for k, v in PROGRESS.items() if v.get("done")][:100]:
            PROGRESS.pop(k, None)
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
    try:
        from engine import checks
        st["problems"] = checks.check(blocks, layout.get("hoop", [100, 100]))
    except Exception:  # a checker bug must never block a build
        st["problems"] = []
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
    if HOSTED:
        return err("Not available on the website.", 404)
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
    if HOSTED:
        return err("Not available on the website.", 404)
    target = request.get_json(force=True).get("target", "")
    if not target or not os.path.isdir(target):
        return err("That drive or folder isn't available. Is the disk in?")
    return jsonify(d1disk.disk_status(target))


@app.post("/api/disk/write")
def disk_write():
    if HOSTED:
        return err("Not available on the website.", 404)
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


# The browser reads the menu files on the visitor's floppy (Chrome/Edge folder access), sends them
# here, and gets back exactly which files to write and delete. The disk logic is the same as the
# local app's; it just runs on a scratch copy.
_DISK_FILE = re.compile(r"^(MENU_SEL\.PHV|MENU_01/MENU_01\.MHV|MENU_01/DES01_\d\d\.SHV)$", re.I)


def _disk_copy(files):
    """Scratch folder holding the disk files the browser sent ({relative path: base64})."""
    import base64, tempfile
    tmp = tempfile.mkdtemp(prefix="d1web_")
    before = {}
    for rel, b64 in (files or {}).items():
        rel = rel.replace("\\", "/")
        if not _DISK_FILE.match(rel) or len(b64 or "") > 400_000:
            continue
        rel = rel.upper()
        data = base64.b64decode(b64 or "")
        path = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(data)
        before[rel] = data
    return tmp, before


@app.post("/api/disk/peek")
def disk_peek():
    import shutil
    tmp, _ = _disk_copy(request.get_json(force=True).get("files"))
    try:
        return jsonify(d1disk.disk_status(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/disk/build")
def disk_build():
    import base64, shutil
    body = request.get_json(force=True)
    tmp, before = _disk_copy(body.get("files"))
    try:
        pat = pattern_for(body["layout"])
        name = safe_name(body["layout"].get("name"), "DESIGN")[:12]
        res = d1disk.write_disk(pat, tmp, name, safe_name(body.get("label"), "MY DESIGNS")[:11], mode=body.get("mode", "add"))
        after = {}
        for root, _, names in os.walk(tmp):
            for n in names:
                p = os.path.join(root, n)
                after[os.path.relpath(p, tmp).replace("\\", "/").upper()] = open(p, "rb").read()
        write = {rel: base64.b64encode(d).decode() for rel, d in after.items() if before.get(rel) != d}
        # write the design first and the menu last, so a pulled disk never lists a missing design
        order = sorted(write, key=lambda r: (r.endswith(".MHV"), r.endswith(".PHV")))
        return jsonify(slot=res["slot"], write=[[r, write[r]] for r in order],
                       delete=[r for r in before if r not in after], status=d1disk.disk_status(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------- projects

@app.get("/api/projects")
def projects():
    if HOSTED:
        return err("Not available on the website.", 404)
    items = []
    for f in sorted(os.listdir(PROJECTS), key=lambda f: -os.path.getmtime(os.path.join(PROJECTS, f))):
        if f.endswith(".json"):
            items.append(dict(file=f, name=f[:-5], modified=os.path.getmtime(os.path.join(PROJECTS, f))))
    return jsonify(items)


@app.post("/api/projects")
def save_project():
    if HOSTED:
        return err("Not available on the website.", 404)
    layout = request.get_json(force=True)
    name = safe_name(layout.get("name"))
    json.dump(layout, open(os.path.join(PROJECTS, name + ".json"), "w"), indent=1)
    return jsonify(ok=True, file=name + ".json")


@app.get("/api/projects/<path:f>")
def load_project(f):
    if HOSTED:
        return err("Not available on the website.", 404)
    p = os.path.join(PROJECTS, os.path.basename(f))
    if not os.path.exists(p):
        return err("Not found", 404)
    return jsonify(json.load(open(p)))


# ----------------------------------------------------------------------------- AI

@app.post("/api/ai/key")
def ai_key():
    if HOSTED:
        return err("Not available on the website.", 404)
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


@app.post("/api/ai/test")
def ai_test():
    """Check a key without spending anything: list the models it can use (free)."""
    import anthropic
    body = request.get_json(force=True)
    if HOSTED:
        key = (request.headers.get("X-Anthropic-Key") or "").strip()
        ws = (request.headers.get("X-Anthropic-Workspace") or "").strip()
    else:
        key = (body.get("key") or "").strip() or config().get("anthropic_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        ws = (body.get("workspace") or "").strip() or config().get("anthropic_workspace", "")
    if not key:
        return jsonify(ok=False, msg="There's no key to test yet - paste one in the box first.")
    if not key.startswith("sk-ant-"):
        return jsonify(ok=False, msg="That doesn't look like an Anthropic key - it should start with sk-ant-. Copy it again from the API keys page.")
    try:
        client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=20,
                                     default_headers={"anthropic-workspace-id": ws} if ws else None)
        client.models.list(limit=1)
        return jsonify(ok=True, msg="It works! Press Save and you're all set.")
    except anthropic.AuthenticationError:
        return jsonify(ok=False, msg="Anthropic didn't accept that key. Make sure you copied all of it, or make a new key and try again.")
    except anthropic.PermissionDeniedError as e:
        m = str(e)
        if "workspace" in m.lower():
            return jsonify(ok=False, msg="This key was made outside a workspace. Easiest fix: make a new key and choose the Default workspace.")
        return jsonify(ok=False, msg="Anthropic says this key isn't allowed to do that. Check your account on the Anthropic website.")
    except anthropic.APIConnectionError:
        return jsonify(ok=False, msg="Couldn't reach Anthropic - check the internet connection and try again.")
    except Exception as e:
        return jsonify(ok=False, msg="Anthropic said: %s" % str(e)[:200])


@app.post("/api/ai/forget")
def ai_forget():
    if HOSTED:
        return err("Not available on the website.", 404)
    c = config()
    c.pop("anthropic_key", None)
    c.pop("anthropic_workspace", None)
    save_config(c)
    return jsonify(ai=False, workspace="")


@app.post("/api/ai/analyze")
def ai_analyze():
    from engine import ai
    body = request.get_json(force=True)
    if HOSTED:  # the visitor's own key, sent with this request only; never stored or logged here
        key = (request.headers.get("X-Anthropic-Key") or "").strip()
        workspace = (request.headers.get("X-Anthropic-Workspace") or "").strip() or None
    else:
        key = config().get("anthropic_key") or os.environ.get("ANTHROPIC_API_KEY")
        workspace = config().get("anthropic_workspace")
    if not key:
        return err("Add your Anthropic API key in Settings to use picture reading.")
    image_id = os.path.basename(body.get("image_id", ""))
    if not os.path.exists(os.path.join(UPLOADS, image_id)):
        return err("That picture isn't loaded any more - add it again.")
    hoop = body.get("hoop", [100, 100])
    job = request.headers.get("X-Job")

    def work():
        return ai.analyze(os.path.join(UPLOADS, image_id), image_id, hoop, key, workspace, progress_for(job),
                          mode=body.get("mode", "exact"), refine=body.get("refine", ""), previous=body.get("previous"))

    if request.headers.get("X-Async") and job:
        # website: web hosts cut requests off after ~60 s and a read can take longer, so it runs in the
        # background and the page collects the answer from /api/progress/<job>
        def run():
            try:
                els, notes, raw = work()
                PROGRESS[job] = dict(pct=100, msg="Done", done=True, result=dict(elements=els, notes=notes, raw=raw))
            except Exception as e:  # noqa: BLE001 - reported to the page
                PROGRESS[job] = dict(pct=100, msg="", done=True, error=str(e) or "The picture couldn't be read.")
        PROGRESS[job] = dict(pct=1, msg="Starting")
        threading.Thread(target=run, daemon=True).start()
        return jsonify(job=job, started=True)
    try:
        layout_elements, notes, raw = work()
    except RuntimeError as e:
        return err(str(e))
    return jsonify(elements=layout_elements, notes=notes, raw=raw)


if __name__ == "__main__":
    port = int(os.environ.get("THIMBLE_PORT", 5311))
    if not os.environ.get("THIMBLE_NO_BROWSER"):
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:%d" % port)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
