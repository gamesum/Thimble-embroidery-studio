"""Read a picture with Claude and rebuild it as editable embroidery elements.

Claude only describes the picture (words, font style, colours, positions, simple shapes,
leftover artwork). The stitches themselves come from the engine: words are re-typeset with
the embroidery font library, shapes use the shape library, and artwork is traced.
"""
import base64, io, json, os, uuid

import anthropic
import numpy as np
from PIL import Image

from . import raster, embfont
from .design import IMAGE_DIR, rgb_to_hex

MODEL = "claude-opus-5"
CATEGORIES = ["Block", "Script", "Serif", "Varsity", "Blackletter", "Rounded", "Display", "Handwritten", "Western"]
SHAPES = ["heart", "star", "circle", "ring", "rect", "frame", "double_frame", "offset_frame", "line"]

SCHEMA = {
    "type": "object",
    "properties": {
        "background": {"type": "string", "description": "hex colour of the background / garment, e.g. #e8e4dc"},
        "notes": {"type": "string", "description": "one short sentence for the user about anything you could not reproduce"},
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["text", "shape", "drawing", "artwork"]},
                    "text": {"type": "string", "description": "exact words for text (keep capitalisation); empty otherwise"},
                    "font": {"type": "string", "description": "closest font from the provided list, for text; empty otherwise"},
                    "shape": {"type": "string", "enum": SHAPES + [""], "description": "for kind=shape"},
                    "color": {"type": "string", "description": "main thread colour as hex"},
                    "outline_color": {"type": "string", "description": "hex of a contrasting border around the letters/shape, or empty"},
                    "bbox": {"type": "array", "items": {"type": "number"},
                             "description": "[left, top, right, bottom] as fractions 0-1 of the image width/height"},
                    "cap_height": {"type": "number", "description": "for text: height of a capital letter as a fraction of image height"},
                    "arc": {"type": "number", "description": "for text on a curve: degrees of arc, positive = arches up, negative = smile; 0 if straight"},
                    "rotation": {"type": "number", "description": "degrees clockwise if the element is tilted, else 0"},
                    "geometry": {
                        "type": "array",
                        "description": "for kind=drawing: the artwork redrawn as flat parts, back to front; empty otherwise",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["polygon", "polyline", "circle"]},
                                "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                                           "description": "[x, y] fractions 0-1 of the image width/height (circle: one point = centre)"},
                                "radius": {"type": "number", "description": "circle radius as a fraction of image width, else 0"},
                                "stroke": {"type": "number", "description": "polyline line width as a fraction of image width, else 0"},
                                "color": {"type": "string", "description": "thread colour hex"},
                            },
                            "required": ["type", "points", "radius", "stroke", "color"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["kind", "text", "font", "shape", "color", "outline_color", "bbox", "cap_height", "arc", "rotation", "geometry"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["background", "notes", "elements"],
    "additionalProperties": False,
}


MODES = {
    "creative": """Style: you have a digitizer's creative license. Keep the original look, layout and colours, but
even things out so it embroiders cleanly: make curves smooth and even (a string of lights becomes a clean, even arc
or swag with evenly spaced bulbs of equal size), make repeated or mirrored things regular and symmetrical, centre
the lettering, give rows of text and dividers clear breathing room so nothing touches, and drop fussy details.""",
    "exact": """Style: exact replica. Reproduce the design as faithfully as possible - the same positions, sizes,
spacing, curves and irregularities as in the picture. Do not tidy, straighten or rearrange anything.""",
}


def _prompt(fonts):
    by_cat = {}
    for f in fonts:
        by_cat.setdefault(f["category"], []).append(f["name"])
    font_list = "\n".join("%s: %s" % (c, ", ".join(n)) for c, n in by_cat.items())
    return f"""You are helping a home embroiderer recreate this design on an embroidery machine.
List every separate piece of the design so it can be rebuilt:

- kind "text": each run of words that shares one font, size and colour (a separate element per line or per style).
  Give the exact words, the closest font from the list below, the thread colour, and the capital-letter height.
  If the letters have a contrasting border, give outline_color. If the words sit on a curve, give arc.
- kind "shape": simple shapes that match one of: {", ".join(SHAPES)} (e.g. a small heart, a rectangle border = frame,
  two overlapping rectangle borders = offset_frame, a divider = line). A divider that is interrupted by words
  (— WORDS —) is two separate line shapes, one each side of the words, never one line through them.
- kind "drawing": logo artwork built from simple forms (mountains, trees, sun, waves, a string of lights, stars,
  leaves, simple icons). Redraw it in `geometry` the way an embroidery digitizer would: flat parts only.
  polygon = a filled area (6-40 points, following its outline), polyline = a line or string (with stroke width),
  circle = a dot or bulb. Leave out glows, gradients, sparkles, haze, clouds of tiny dots and anything smaller than
  ~1.5% of the image width. Never draw the sky, a glow, light rays, cloud banks or any background area as a
  polygon - the fabric is the background; only draw the objects themselves. Keep each object's real proportions
  (trace mountain peaks and ridgelines where they are, not flattened). Use 2-4 thread colours. List parts back to
  front (later parts sew on top, e.g. snow caps after the mountain). bbox is the box around the whole drawing.
  Make each separate object its own drawing element (e.g. one for the mountain range with its snow caps, one for
  the string of lights with its wire), so they can be moved independently.
- kind "artwork": only for artwork too complex to redraw as parts (a detailed illustration); it will be traced.
Ignore the garment, fabric texture, shadows, wood, mockup backgrounds and photo lighting — describe the design only.
Colours are thread colours: pick clean flat colours, not shadows or highlights.
Bounding boxes are fractions of the whole image, tight around the ink.

Fonts available (choose the closest look; prefer bold/heavy block fonts for chunky letters, script for joined handwriting):
{font_list}"""


def _call(client, img_b64, media_type, prompt, on_chars=None):
    # (on_chars gets the running count of streamed text + thinking characters)
    kwargs = dict(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    def run(stream_fn, **extra):
        # streamed so the app can show progress while Claude writes its description
        with stream_fn(**kwargs, **extra) as st:
            n = 0
            for ev in st:
                if ev.type == "content_block_delta":
                    n += len(getattr(ev.delta, "text", "") or getattr(ev.delta, "thinking", "") or "")
                    if on_chars:
                        on_chars(n)
            return st.get_final_message()
    try:
        # server-side refusal fallback: if the model declines, the API retries on a fallback model
        resp = run(client.beta.messages.stream, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    except (anthropic.BadRequestError, TypeError):  # fallback param unavailable -> plain request
        resp = run(client.messages.stream)
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to read that picture.")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("That picture has too much going on to read in one go - try cropping it.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


def _prepare(path):
    """Downscale big photos (Claude sees ~1500px max anyway) and encode as PNG."""
    im = raster.load_image(path)
    im.thumbnail((1568, 1568))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return im, base64.standard_b64encode(buf.getvalue()).decode()


def _api_message(e):
    """The human-readable explanation inside an API error body."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message")
        if msg:
            return msg
    return getattr(e, "message", str(e))


def analyze(path, image_id, hoop, api_key, workspace_id=None, progress=None, mode="creative", refine="", previous=None):
    """-> (layout elements, notes, raw description). refine + previous: revise an earlier
    description following the user's instructions instead of starting from scratch."""
    import math, threading, time
    report = progress or (lambda f, msg: None)
    fonts = raster.fonts()
    names = {f["name"] for f in fonts}
    report(0.02, "Sending the picture to Claude")
    im, b64 = _prepare(path)
    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
    client = anthropic.Anthropic(api_key=api_key, max_retries=2, default_headers=headers)
    try:
        prompt = _prompt(fonts) + "\n\n" + MODES.get(mode, MODES["creative"])
        if refine and previous:
            prompt += ("\n\nYou already described this picture as:\n" + json.dumps(previous) +
                       "\n\nThe user wants these changes: " + refine.strip() +
                       "\nReturn the complete revised description (every element, same format), changing only what "
                       "the request asks for and keeping everything else as it was.")
        # how long Claude takes isn't known up front: a clock keeps the bar moving while it thinks,
        # and the streamed characters take over once the answer is being written; both ease towards 90%
        st = {"n": 0, "t0": time.time(), "done": False}
        def tick():
            while not st["done"]:
                ft = 1 - math.exp(-(time.time() - st["t0"]) / 45)
                fc = 1 - math.exp(-st["n"] / 3500)
                report(0.05 + 0.85 * max(0.7 * ft, fc), "Claude is writing it up" if st["n"] else "Claude is looking at the picture")
                time.sleep(0.4)
        threading.Thread(target=tick, daemon=True).start()
        try:
            data = _call(client, b64, "image/png", prompt, lambda n: st.__setitem__("n", n))
        finally:
            st["done"] = True
    except anthropic.AuthenticationError:
        raise RuntimeError("Your Anthropic API key was rejected - check it in Settings.")
    except anthropic.RateLimitError:
        raise RuntimeError("The Anthropic API is busy (rate limited). Try again in a minute.")
    except anthropic.APIConnectionError:
        raise RuntimeError("Couldn't reach the Anthropic API - check your internet connection.")
    except anthropic.APIStatusError as e:
        msg = _api_message(e)
        if "workspace" in msg.lower():
            msg += " (Fix: in Settings, paste your workspace ID, or create the API key inside a workspace in the Anthropic Console.)"
        elif "credit balance" in msg.lower():
            msg += " (Add credit at console.anthropic.com → Billing.)"
        raise RuntimeError("Anthropic API: " + msg)
    try:
        from .design import IMAGE_DIR as _d
        json.dump(dict(mode=mode, refine=refine, data=data), open(os.path.join(os.path.dirname(_d), "ai_last.json"), "w"), indent=1)
    except Exception:
        pass
    report(0.93, "Setting the type and shapes")
    res = to_layout(data, im, hoop, names, fonts, mode)
    report(1.0, "Done")
    return res, data.get("notes", ""), data


def _hex(s, default="#333333"):
    s = (s or "").strip()
    if len(s) == 7 and s.startswith("#"):
        try:
            int(s[1:], 16)
            return s.lower()
        except ValueError:
            pass
    return default


def to_layout(data, im, hoop, names, fonts, mode="creative"):
    """Claude's description -> layout elements sized and placed inside the hoop."""
    W, H = im.size
    els = [e for e in data.get("elements", []) if len(e.get("bbox") or []) == 4]
    if not els:
        return []
    for e in els:
        l, t, r, b = [min(1.0, max(0.0, float(v))) for v in e["bbox"]]
        e["_box"] = (min(l, r) * W, min(t, b) * H, max(l, r) * W, max(t, b) * H)
    ux0 = min(e["_box"][0] for e in els); uy0 = min(e["_box"][1] for e in els)
    ux1 = max(e["_box"][2] for e in els); uy1 = max(e["_box"][3] for e in els)
    s = min(0.9 * hoop[0] / max(1, ux1 - ux0), 0.9 * hoop[1] / max(1, uy1 - uy0))  # mm per image px
    cx, cy = (ux0 + ux1) / 2, (uy0 + uy1) / 2
    out = []
    def order(e):  # frames and artwork first, lettering over them, small accents (hearts, stars) last
        if e["kind"] == "shape":
            return 0 if e.get("shape") in ("frame", "double_frame", "offset_frame", "ring", "line", "rect") else 3
        return {"artwork": 1, "drawing": 1, "text": 2}.get(e["kind"], 4)
    for e in sorted(els, key=order):
        x0, y0, x1, y1 = e["_box"]
        x, y = round((x0 + x1) / 2 * s - cx * s, 1), round((y0 + y1) / 2 * s - cy * s, 1)
        w_mm, h_mm = (x1 - x0) * s, (y1 - y0) * s
        color = _hex(e.get("color"))
        outline = {"color": _hex(e.get("outline_color")), "width_mm": 1.0} if e.get("outline_color") else None
        rot = float(e.get("rotation") or 0)
        if e["kind"] == "text" and e.get("text", "").strip():
            font = e.get("font") if e.get("font") in names else next(
                (f["name"] for f in fonts if f["category"] in (e.get("font") or "")), "Montserrat Bold")
            cap = float(e.get("cap_height") or 0) * H * s or h_mm * 0.7
            cap = max(4.0, min(cap, h_mm * 1.05 if h_mm > 0 else cap))  # stitched letters under ~4 mm fill in
            el = dict(type="text", text=e["text"].strip(), font=font, height_mm=round(cap, 1), color=color,
                      letter_spacing=0.0, line_spacing=1.2, align="center", arc=float(e.get("arc") or 0),
                      style="auto", outline=outline, x=x, y=y, rotation=rot)
            # swap to the matching hand-digitized font when there is one for this size
            cat = next((f["category"] for f in fonts if f["name"] == font), "Block")
            el["_src"], el["_cat"] = font, cat
            pro = embfont.stand_in(font, cat, el["height_mm"])
            if pro:
                el["font"] = pro
            _fit_text_width(el, w_mm)
            out.append(el)
        elif e["kind"] == "drawing" and e.get("geometry"):
            el = _drawing(e, W, H, w_mm, x, y, rot)
            if el:
                out.append(el)
        elif e["kind"] == "shape" and e.get("shape") in SHAPES:
            stroke = max(0.8, min(3.0, min(w_mm, h_mm) * 0.03))
            out.append(dict(type="shape", kind=e["shape"], width_mm=round(max(2, w_mm), 1), height_mm=round(max(2, h_mm), 1),
                            stroke_mm=round(stroke, 1), color=color, style="auto", outline=outline, x=x, y=y, rotation=rot))
        else:
            out.append(_artwork(im, e["_box"], w_mm, x, y, rot))
    out = _join_words([o for o in out if o])
    if mode != "exact":
        out = _even_out(out, hoop)
    # drawings arrive as separate pieces (lights, mountains...) whenever their objects don't touch
    from .design import split_vector
    split = []
    for gi, e in enumerate(out):
        pieces = split_vector(e) if e["type"] == "vector" else [e]
        if len(pieces) > 1:
            for p in pieces:
                p["_group"] = gi  # pieces of one drawing keep their places relative to each other
        split += pieces
    out = _tidy(split, hoop)
    for e in out:  # private helper keys
        for k in [k for k in e if k.startswith("_")]:
            del e[k]
    return out


def _extent(el):
    """(width, height) in mm an element will actually take up once stitched."""
    t = el["type"]
    if t == "text":
        try:
            if embfont.family(el["font"]):
                w = embfont.text_width(el)
            else:
                _, (w, _) = raster.text_mask(el["text"], el["font"], el["height_mm"], el.get("letter_spacing", 0),
                                             el.get("line_spacing", 1.2), "center", el.get("arc", 0))
        except Exception:
            w = len(el["text"]) * el["height_mm"] * 0.7
        lines = el["text"].count("\n") + 1
        return w, el["height_mm"] * (1.25 + 1.2 * (lines - 1))
    if t == "shape":
        return el["width_mm"], el["height_mm"]
    if t == "vector" and el.get("parts"):
        # parts may reach past the box the model gave: measure them (symmetric about the centre)
        w, asp = el["width_mm"], el.get("aspect", 0.5)
        hu = hv = 0.0
        for p in el["parts"]:
            g = max(float(p.get("r") or 0), float(p.get("stroke") or 0) / 2)
            for u, v in p["points"]:
                hu = max(hu, abs(u - 0.5) + g)
                hv = max(hv, abs(v - asp / 2) + g)
        return 2 * hu * w, 2 * hv * w
    return el["width_mm"], el["width_mm"] * el.get("aspect", 0.5)


def _tidy(els, hoop):
    """Make room where re-typeset text came out bigger than in the picture (lettering has a
    4 mm minimum): trim divider lines that now run into words, push overlapping rows apart,
    and re-centre the whole design."""
    if not els:
        return els
    _fit_hoop(els, hoop)  # first, so the spacing fixes below are done at the final size
    box = lambda e: (lambda w, h: (e["x"] - w / 2, e["y"] - h / 2, e["x"] + w / 2, e["y"] + h / 2))(*_extent(e))
    texts = [e for e in els if e["type"] == "text"]
    # 1. divider lines beside words stop 1.5 mm short of them
    keep = []
    for e in els:
        if e["type"] == "shape" and e.get("kind") == "line":
            lx0, ly0, lx1, ly1 = box(e)
            for t in texts:
                tx0, ty0, tx1, ty1 = box(t)
                if ly1 < ty0 or ly0 > ty1 or lx1 < tx0 or lx0 > tx1:
                    continue
                # slide the divider out so it clears the (now bigger) words, keeping its length
                wl = lx1 - lx0
                if e["x"] <= t["x"]:
                    lx1 = min(lx1, tx0 - 1.5); lx0 = lx1 - wl
                else:
                    lx0 = max(lx0, tx1 + 1.5); lx1 = lx0 + wl
            # stay inside the hoop; only then give up length
            lx0, lx1 = max(lx0, -hoop[0] / 2 + 2), min(lx1, hoop[0] / 2 - 2)
            if lx1 - lx0 < 3:
                continue  # no room left for it at all
            e["width_mm"], e["x"] = round(lx1 - lx0, 1), round((lx0 + lx1) / 2, 1)
        keep.append(e)
    els = keep
    # 2. rows that now overlap: push the lower one (and everything under it) down
    def is_line(e):
        return e["type"] == "shape" and e.get("kind") == "line"
    order = sorted(els, key=lambda e: e["y"])
    for i, a in enumerate(order):
        if is_line(a):
            continue
        ax0, ay0, ax1, ay1 = box(a)
        for b in order[i + 1:]:
            if is_line(b):
                continue
            if a.get("_group") is not None and a.get("_group") == b.get("_group"):
                continue  # pieces of one drawing: their layout is the artwork's own
            bx0, by0, bx1, by1 = box(b)
            if bx1 <= ax0 or bx0 >= ax1:
                continue  # side by side, not stacked
            push = ay1 + 1.0 - by0
            if push > 0 and b["y"] > a["y"]:
                for c in order:
                    if c["y"] >= b["y"] - 0.01:
                        c["y"] = round(c["y"] + push, 1)
    # 3. centre the whole design in the hoop again, and shrink it if it no longer fits
    bs = [box(e) for e in els]
    cx = (min(b[0] for b in bs) + max(b[2] for b in bs)) / 2
    cy = (min(b[1] for b in bs) + max(b[3] for b in bs)) / 2
    for e in els:
        e["x"], e["y"] = round(e["x"] - cx, 1), round(e["y"] - cy, 1)
    return els


def _fit_hoop(els, hoop, margin=0.9):
    """Scale the whole design down (positions and sizes together) until it fits the hoop."""
    def box(e):
        w, h = _extent(e)
        return e["x"] - w / 2, e["y"] - h / 2, e["x"] + w / 2, e["y"] + h / 2
    bs = [box(e) for e in els]
    bw = max(b[2] for b in bs) - min(b[0] for b in bs)
    bh = max(b[3] for b in bs) - min(b[1] for b in bs)
    k = min(1.0, margin * hoop[0] / max(bw, 1e-6), margin * hoop[1] / max(bh, 1e-6))
    if k < 0.995:
        for e in els:
            e["x"], e["y"] = round(e["x"] * k, 1), round(e["y"] * k, 1)
            if e["type"] == "text":
                e["height_mm"] = round(max(4.0, e["height_mm"] * k), 1)
            elif e["type"] == "shape":
                e["width_mm"], e["height_mm"] = round(e["width_mm"] * k, 1), round(e["height_mm"] * k, 1)
            else:
                e["width_mm"] = round(e["width_mm"] * k, 1)
    return k


def _join_words(els):
    """Words the model split into pieces on one line (AFTER + GLO in two colours) become one text
    element with per-letter colours, so the spacing between them is the font's own."""
    texts = sorted([e for e in els if e["type"] == "text" and "\n" not in e["text"]], key=lambda e: e["x"])
    used, merged = set(), []
    for i, a in enumerate(texts):
        if id(a) in used:
            continue
        group = [a]
        for b in texts[i + 1:]:
            last = group[-1]
            same_type = b.get("_src", b["font"]) == last.get("_src", last["font"])
            if id(b) in used or not same_type or abs(b["y"] - last["y"]) > 0.35 * last["height_mm"]:
                continue
            if abs(b["height_mm"] - last["height_mm"]) > 0.2 * last["height_mm"] or b.get("arc") or last.get("arc"):
                continue
            wl, _ = _extent(last)
            gap = (b["x"] - _extent(b)[0] / 2) - (last["x"] + wl / 2)
            if gap > 1.2 * last["height_mm"]:
                continue
            group.append(b)
        if len(group) == 1:
            continue
        for g in group:
            used.add(id(g))
        base = group[0]
        text, colors = "", []
        for k, g in enumerate(group):
            if k:
                prev = group[k - 1]
                gap = (g["x"] - _extent(g)[0] / 2) - (prev["x"] + _extent(prev)[0] / 2)
                if gap > 0.45 * base["height_mm"]:  # a real word space in the picture
                    text += " "
                    colors.append(None)
            text += g["text"]
            colors += [None if g["color"] == base["color"] else g["color"]] * len(g["text"])
        x0 = min(g["x"] - _extent(g)[0] / 2 for g in group)
        x1 = max(g["x"] + _extent(g)[0] / 2 for g in group)
        el = dict(base, text=text, x=round((x0 + x1) / 2, 1), y=round(sum(g["y"] for g in group) / len(group), 1),
                  height_mm=max(g["height_mm"] for g in group), letter_spacing=0.0)
        if any(colors):
            el["letter_colors"] = colors
        if el.get("_src"):  # choose the stitch font once, for the joined word
            el["font"] = embfont.stand_in(el["_src"], el.get("_cat"), el["height_mm"]) or el["_src"]
        merged.append(el)
    return [e for e in els if id(e) not in used] + merged


def _even_out(els, hoop):
    """Creative mode: what a digitizer would tidy by hand - things near the middle are centred,
    divider pairs mirror each other, and strings of dots become an even, symmetric arc."""
    W = hoop[0]
    for e in els:
        if abs(e["x"]) < 0.12 * W and not (e["type"] == "shape" and e.get("kind") == "line"):
            e["x"] = 0.0
    lines = [e for e in els if e["type"] == "shape" and e.get("kind") == "line"]
    for a in lines:
        for b in lines:
            if a is b or a["x"] >= 0 or b["x"] <= 0 or abs(a["y"] - b["y"]) > 3:
                continue
            w = round((a["width_mm"] + b["width_mm"]) / 2, 1)
            d = round((abs(a["x"]) + abs(b["x"])) / 2, 1)
            y = round((a["y"] + b["y"]) / 2, 1)
            a.update(width_mm=w, x=-d, y=y)
            b.update(width_mm=w, x=d, y=y)
    for e in els:
        if e["type"] == "vector":
            e["parts"] = _even_strings(e["parts"])
    return els


def _even_strings(parts):
    """Four or more same-coloured dots = a string of lights: refit them on a parabola that is
    mirror-symmetric about the drawing's centre line, evenly spaced and equal in size, and lay any
    line running through them on the same curve."""
    def rgb(h):
        return np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], float)
    groups = []  # dots of similar colour (the model gives every bulb a slightly different yellow)
    for p in parts:
        if p["type"] == "circle" and p["points"]:
            for g in groups:
                if np.linalg.norm(rgb(g[0]["color"]) - rgb(p["color"])) < 130:
                    g.append(p)
                    break
            else:
                groups.append([p])
    for dots in groups:
        if len(dots) < 4:
            continue
        us = np.array([d["points"][0][0] for d in dots])
        vs = np.array([d["points"][0][1] for d in dots])
        # the wire nearest the dots (if any) defines the curve; otherwise the dots themselves
        wire, wd = None, None
        for p in parts:
            if p["type"] == "polyline" and len(p["points"]) >= 2:
                pts = np.array(p["points"], float)
                d = float(np.mean([np.min(np.abs(pts[:, 0] - u) + np.abs(pts[:, 1] - v)) for u, v in zip(us, vs)]))
                if d < 0.12 and (wd is None or d < wd):
                    wire, wd = p, d
        fu, fv = (np.array(wire["points"], float).T if wire is not None else (us, vs))
        A = np.column_stack([(fu - 0.5) ** 2, np.ones_like(fu)])
        (a, k), *_ = np.linalg.lstsq(A, fv, rcond=None)
        curve = lambda u: float(a * (u - 0.5) ** 2 + k)
        span = max(0.5 - us.min(), us.max() - 0.5)
        if wire is not None:
            wu = np.array(wire["points"], float)[:, 0]
            # the wire ends a little past the outermost bulbs (the model often runs it off the page)
            ext = min(max(0.5 - wu.min(), wu.max() - 0.5, span), span + 0.05)
            wire["points"] = [[round(float(u), 4), round(curve(u), 4)] for u in np.linspace(0.5 - ext, 0.5 + ext, 41)]
            span = min(span, ext - 0.02)  # bulbs stay on the wire, a little in from its ends
        color = max(set(d["color"] for d in dots), key=[d["color"] for d in dots].count)
        new_u = np.linspace(0.5 - span, 0.5 + span, len(dots))
        r = float(np.median([d["r"] for d in dots]))
        for d, u in zip(sorted(dots, key=lambda d: d["points"][0][0]), new_u):
            d["points"] = [[round(float(u), 4), round(curve(u), 4)]]
            d["r"] = round(r, 4)
            d["color"] = color  # one bulb colour
    return parts


def _drop_backdrop(parts, aspect):
    """Drop a big shape at the very back that everything else sits on - that's a glow or sky
    the model drew anyway, not an object (thread would cover the whole background)."""
    if len(parts) < 4 or parts[0]["type"] != "polygon":
        return parts
    pts = np.array(parts[0]["points"], float)
    x, y = pts[:, 0], pts[:, 1]
    area = 0.5 * abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))
    if area < 0.5 * max(aspect, 1e-6):   # covers less than half the drawing's box: a real object
        return parts
    from shapely.geometry import Polygon, Point
    back = Polygon(pts).buffer(0)
    inside = sum(back.contains(Point(p["points"][0])) for p in parts[1:] if p["points"])
    return parts[1:] if inside >= 0.7 * (len(parts) - 1) else parts


def _drawing(e, W, H, w_mm, x, y, rot):
    """Claude's flat geometry -> a 'vector' element. Coordinates are stored as fractions of the
    element's width (u right, v down from its top-left) so resizing scales everything together."""
    x0, y0, x1, y1 = e["_box"]
    bw = max(1.0, x1 - x0)
    parts = []
    for g in e.get("geometry") or []:
        pts = [(float(p[0]) * W, float(p[1]) * H) for p in (g.get("points") or []) if len(p) >= 2]
        t = g.get("type")
        if not pts or (t == "polygon" and len(pts) < 3) or (t == "polyline" and len(pts) < 2):
            continue
        parts.append(dict(type=t, points=[[round((px - x0) / bw, 4), round((py - y0) / bw, 4)] for px, py in pts],
                          r=round(float(g.get("radius") or 0) * W / bw, 4), stroke=round(float(g.get("stroke") or 0) * W / bw, 4),
                          color=_hex(g.get("color"), _hex(e.get("color")))))
    parts = _drop_backdrop(parts, (y1 - y0) / bw)
    if not parts:
        return None
    return dict(type="vector", name="Drawing", parts=parts, width_mm=round(max(5.0, w_mm), 1),
                aspect=round((y1 - y0) / bw, 4), x=x, y=y, rotation=rot)


def _fit_text_width(el, target_w):
    """Make the typeset words span the width they had in the picture (size first, then tracking)."""
    if target_w <= 0:
        return
    try:
        if embfont.family(el["font"]):
            w = embfont.text_width(el)
        else:
            _, (w, _) = raster.text_mask(el["text"], el["font"], el["height_mm"], 0, el["line_spacing"], "center", el["arc"])
    except Exception:
        return
    if w <= 0:
        return
    ratio = target_w / w
    if ratio < 0.97:
        el["height_mm"] = round(max(4.0, el["height_mm"] * ratio), 1)
    elif ratio > 1.3:  # only clearly spaced-out lettering (L I G H T I N G) gets tracking; the model's
        # boxes are rough, so small mismatches keep the font's own spacing
        chars = max(1, len(el["text"].replace(" ", "")) - 1)
        extra = (target_w - w) / chars / el["height_mm"]
        is_script = any(f["category"] == "Script" and f["name"] == el["font"] for f in raster.fonts()) or \
            (embfont.family(el["font"]) or {}).get("category") == "Script"
        if is_script:  # scripts must stay joined: grow the size instead of spacing letters apart
            el["height_mm"] = round(el["height_mm"] * min(ratio, 1.35), 1)
        else:
            el["letter_spacing"] = round(min(0.35, extra), 3)


def _artwork(im, box, w_mm, x, y, rot):
    """Crop leftover artwork into its own traced picture element."""
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    crop = im.crop((x0, y0, x1, y1))
    image_id = uuid.uuid4().hex[:12] + ".png"
    crop.save(os.path.join(IMAGE_DIR, image_id))
    pal = raster.palette(crop)
    small = crop.copy()
    small.thumbnail((300, 300))
    labels = raster.snap(np.asarray(small), pal)
    bg = raster.background_index(labels)
    counts = np.bincount(labels.ravel(), minlength=len(pal))
    colors = [dict(rgb=list(c), hex=rgb_to_hex(c), thread=rgb_to_hex(c), keep=i != bg,
                   share=round(float(counts[i]) / labels.size, 3), style="auto") for i, c in enumerate(pal)]
    return dict(type="image", image_id=image_id, name="Artwork", width_mm=round(max(5, w_mm), 1),
                aspect=crop.height / crop.width, colors=colors, x=x, y=y, rotation=rot)
