"""Layout (JSON-able dict) -> stitch blocks -> pyembroidery pattern, preview and stats.

Layout:
{
  "hoop": [100, 100], "fabric": "knit", "group_colors": true,
  "elements": [
    {"type": "text", "text": "LIMITED", "font": "Montserrat Black", "height_mm": 14, "color": "#a0703c",
     "letter_spacing": 0.05, "arc": 0, "style": "auto", "outline": {"color": "#5a3a1a", "width_mm": 1.0},
     "x": 0, "y": -10, "rotation": 0},
    {"type": "shape", "kind": "heart", "width_mm": 8, "height_mm": 7, "color": "#d0202a", "x": 0, "y": -30},
    {"type": "image", "image_id": "abc", "width_mm": 90, "colors": [{"rgb": [..], "keep": true, "thread": "#..", "style": "auto"}], "x": 0, "y": 0}
  ]
}
x/y are the element centre in mm relative to the hoop centre (y down).
"""
import hashlib, io, json, math, os
import numpy as np
import cv2
from PIL import Image, ImageDraw
import pyembroidery as pe
from pyembroidery.EmbThreadShv import get_thread_set

from dataclasses import replace
from .params import RES, params_for
from . import raster, stitchgen as sg, embfont

IMAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "uploads")
_cache = {}



def num(v, default=0.0):
    """A number from a design piece; empty/None/garbage falls back to the default."""
    try:
        f = float(v)
        return f if f == f else float(default)  # NaN -> default
    except (TypeError, ValueError):
        return float(default)

# ----------------------------------------------------------------------------- threads

def hex_to_rgb(h):
    h = (h or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)   # #c33 -> #cc3333
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (51, 51, 51)               # a damaged color should never stop a build


def rgb_to_hex(c):
    return "#%02x%02x%02x" % tuple(int(v) for v in c[:3])


_shv = None


def shv_thread(hexcolor):
    """Nearest thread in the Designer I's built-in palette (what the machine will display)."""
    global _shv
    if _shv is None:
        _shv = [(t.description, ((t.color >> 16) & 255, (t.color >> 8) & 255, t.color & 255)) for t in get_thread_set()]
    rgb = hex_to_rgb(hexcolor)
    name, c = min(_shv, key=lambda t: sum((a - b) ** 2 for a, b in zip(rgb, t[1])))
    return name


# ----------------------------------------------------------------------------- shapes

def shape_mask(kind, w_mm, h_mm, stroke_mm=1.2):
    W, H = max(2, int(round(w_mm * RES))), max(2, int(round(h_mm * RES)))
    ss = 3
    im = Image.new("L", (W * ss + 8 * ss, H * ss + 8 * ss))
    d = ImageDraw.Draw(im)
    o = 4 * ss
    Ws, Hs = W * ss, H * ss
    sw = max(1, int(round(stroke_mm * RES * ss)))
    if kind == "circle":
        d.ellipse([o, o, o + Ws, o + Hs], fill=255)
    elif kind == "ring":
        d.ellipse([o, o, o + Ws, o + Hs], outline=255, width=sw)
    elif kind == "rect":
        d.rectangle([o, o, o + Ws, o + Hs], fill=255)
    elif kind == "frame":
        d.rectangle([o, o, o + Ws, o + Hs], outline=255, width=sw)
    elif kind == "double_frame":
        g = sw * 2
        d.rectangle([o, o, o + Ws, o + Hs], outline=255, width=sw)
        d.rectangle([o + g + sw, o + g + sw, o + Ws - g - sw, o + Hs - g - sw], outline=255, width=sw)
    elif kind == "offset_frame":  # two overlapping frames, like the "LIMITED edition" box
        g = int(min(Ws, Hs) * 0.08)
        d.rectangle([o, o, o + Ws - g, o + Hs - g], outline=255, width=sw)
        d.rectangle([o + g, o + g, o + Ws, o + Hs], outline=255, width=sw)
    elif kind == "line":
        d.rectangle([o, o + Hs // 2 - sw // 2, o + Ws, o + Hs // 2 + sw // 2], fill=255)
    elif kind == "star":
        cx, cy, R = o + Ws / 2, o + Hs / 2, min(Ws, Hs) / 2
        pts = [(cx + (R if i % 2 == 0 else R * 0.45) * math.sin(i * math.pi / 5),
                cy - (R if i % 2 == 0 else R * 0.45) * math.cos(i * math.pi / 5)) for i in range(10)]
        d.polygon(pts, fill=255)
    else:  # heart
        t = np.linspace(0, 2 * math.pi, 200)
        x = 16 * np.sin(t) ** 3
        y = -(13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t))
        x = (x - x.min()) / (x.max() - x.min()) * Ws + o
        y = (y - y.min()) / (y.max() - y.min()) * Hs + o
        d.polygon(list(zip(x, y)), fill=255)
    small = im.resize((im.width // ss, im.height // ss), Image.LANCZOS)
    m, _ = raster.crop(np.asarray(small) > 110)
    return m


# ----------------------------------------------------------------------------- element -> layers

def element_layers(el):
    """[(hex colour, mask, (ox_px, oy_px), style)] in a shared local pixel frame."""
    t = el.get("type")
    style = el.get("style", "auto")
    if t == "text":
        args = (el.get("text", "") or " ", el.get("font", "Montserrat Bold"), num(el.get("height_mm"), 10),
                num(el.get("letter_spacing"), 0.0), num(el.get("line_spacing"), 1.25),
                el.get("align", "center"), num(el.get("arc"), 0))
        if not _has_letter_colors(el):
            m, _ = raster.text_mask(*args)
            return _with_outline(el, m, style)
        m, _, lab = raster.text_mask(*args, labels=True)
        layers = []
        for color, idxs in _color_groups(el):
            sub = m & np.isin(lab, [i + 1 for i in idxs])
            if sub.any():
                layers.append((color, sub, (0, 0), style))
        return _with_outline(el, m, style, layers)
    if t == "shape":
        kind = el.get("kind", "heart")
        m = shape_mask(kind, num(el.get("width_mm"), 20), num(el.get("height_mm"), 20),
                       num(el.get("stroke_mm"), 1.2))
        if style in (None, "", "auto"):
            # long glossy satin passes, like big lettering: straight rows across solid shapes,
            # one diamond per point on a star, and satin following the line on rings/frames
            style = {"heart": "satinrows", "circle": "satinrows", "rect": "satinrows", "star": "star"}.get(kind, "satin")
        return _with_outline(el, m, style)
    if t == "vector":
        return vector_layers(el)
    if t == "image":
        im = raster.load_image(os.path.join(IMAGE_DIR, el["image_id"]))
        cols = el.get("colors") or []
        pal = [tuple(c["rgb"]) for c in cols] or None
        pal, masks, _, labels = raster.image_masks(im, num(el.get("width_mm"), 80), pal)
        bg = raster.background_index(labels)
        md = min(150.0, max(0.0, num(el.get("merge_colors"), 0)))
        if md > 0 and len(masks) > 1:
            # fold shading colours into the biggest nearby colour (the biggest of a group keeps its thread)
            area = [int(m.sum()) for m in masks]
            leader = list(range(len(masks)))
            for i in sorted(range(len(masks)), key=lambda k: -area[k]):
                if leader[i] != i or i == bg:
                    continue
                for j in range(len(masks)):
                    if j != i and j != bg and leader[j] == j and area[j] <= area[i] and                             sum((a - b) ** 2 for a, b in zip(pal[i], pal[j])) ** 0.5 <= md:
                        leader[j] = i
            for j, i in enumerate(leader):
                if i != j:
                    masks[i] = masks[i] | masks[j]
                    masks[j] = np.zeros_like(masks[j])
        sm = min(4.0, max(0.0, num(el.get("smooth"), 0)))
        if sm > 0 and masks:
            masks = smooth_masks(masks, sm)
        for i, m in enumerate(masks):  # grainy artwork: fill the pinholes in kept colours
            if i != bg:
                masks[i] = fill_pinholes(m)
        if el.get("trace"):
            return trace_layers(el, pal, masks, cols, bg)
        out = []
        for i, m in enumerate(masks):
            c = cols[i] if i < len(cols) else {"keep": i != bg}  # background is off by default
            if c.get("keep", True) and m.any():
                out.append((c.get("thread") or rgb_to_hex(pal[i]), m, (0, 0), c.get("style", "auto")))
        if el.get("eyes", True) and bg is not None and bg < len(pal):
            # holes show the picture's background colour (the eyes): sew them in that colour
            bcol = cols[bg] if bg < len(cols) else {}
            out += eye_layers(out, bcol.get("thread") or rgb_to_hex(pal[bg]))
        return out
    return []


def smooth_masks(masks, mm):
    """Round off lumpy, jagged outlines: blur every colour's area by `mm` and let the strongest
    colour win each pixel (areas stay a clean partition - no gaps or overlaps appear)."""
    sig = mm * RES
    stack = np.stack([cv2.GaussianBlur(m.astype(np.float32), (0, 0), sig) for m in masks])
    lab = np.argmax(stack, 0)
    empty = stack.max(0) < 0.3
    return [(lab == i) & ~empty for i in range(len(masks))]


TRACE_LINE_MAX = 2.0  # mm: parts of the picture this thin are drawn lines - sewn once down the middle
TRACE_SPECK = 1.5     # mm2: smaller blobs are shading noise, not something to outline


def trace_layers(el, pal, masks, cols, bg):
    """Traced picture: sew the edges between colours as lines instead of filling the areas
    (line art / redwork look). Each edge is sewn once, in the darker of the two colours beside
    it; thin parts of the picture (drawn lines) are sewn once along their middle.
    el["trace_line"]: "run" (running stitch) or "satin" (bold satin line)."""
    from scipy import ndimage
    bold = el.get("trace_line") == "satin"
    h, w = masks[0].shape if masks else (0, 0)
    keep = [bool((cols[i] if i < len(cols) else {"keep": i != bg}).get("keep", True)) and masks[i].any()
            for i in range(len(masks))]
    color = [(cols[i].get("thread") if i < len(cols) else None) or rgb_to_hex(pal[i]) for i in range(len(masks))]
    lum = [0.299 * pal[i][0] + 0.587 * pal[i][1] + 0.114 * pal[i][2] for i in range(len(masks))]
    # one clean label per pixel: blur each colour's area and let the strongest win, which rounds
    # off the pixel staircase of a small picture (the traced line follows these edges)
    sig = 0.35 * RES
    stack = np.stack([cv2.GaussianBlur(m.astype(np.float32), (0, 0), sig) for m in masks])
    lab = np.argmax(stack, 0).astype(np.int32)
    lab[stack.max(0) < 0.15] = -1
    # specks of shading (< TRACE_SPECK mm2) would each get their own little loop: let the
    # surrounding area take them over
    for i in range(len(masks)):
        n, cc, st, _ = cv2.connectedComponentsWithStats((lab == i).astype(np.uint8), connectivity=8)
        for j in range(1, n):
            if st[j, cv2.CC_STAT_AREA] < TRACE_SPECK * RES * RES:
                lab[cc == j] = -1
    masks = [lab == i for i in range(len(masks))]
    # drawn lines: thin pieces of a kept colour are sewn down the middle, not outlined
    lines = {}
    for i, m in enumerate(masks):
        if not keep[i]:
            continue
        n, cc = cv2.connectedComponents(m.astype(np.uint8), connectivity=8)
        dt = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 5)
        for j in range(1, n):
            part = cc == j
            if 2 * float(np.percentile(dt[part], 95)) / RES <= TRACE_LINE_MAX:
                lines.setdefault(i, np.zeros((h, w), bool))
                lines[i] |= part
                lab[part] = -1  # the line is not an edge between areas
    if (lab < 0).any() and (lab >= 0).any():
        _, (iy, ix) = ndimage.distance_transform_edt(lab < 0, return_indices=True)
        lab = lab[iy, ix]
    # edges between different labels (and the picture's own border around kept areas)
    L = np.pad(lab, 1, constant_values=-2)
    edge = {}

    def owner(a, b):
        ka = a >= 0 and keep[a]
        kb = b >= 0 and keep[b]
        if ka and kb:
            return a if lum[a] <= lum[b] else b
        return a if ka else (b if kb else None)
    H, W = L.shape
    for dy, dx in ((0, 1), (1, 0)):
        A = L[:H - dy, :W - dx]
        B = L[dy:, dx:]
        diff = A != B
        for a, b in set(zip(A[diff].tolist(), B[diff].tolist())):
            o = owner(a, b)
            if o is None:
                continue
            sel = diff & (A == a) & (B == b)
            full = np.zeros((H, W), bool)
            full[:H - dy, :W - dx] |= sel  # mark the pixel on the A side
            if o == b:                     # ...or on the B side, so the line sits on the owner's area
                full = np.zeros((H, W), bool)
                full[dy:, dx:] |= sel
            edge.setdefault(o, np.zeros((h, w), bool))
            edge[o] |= full[1:h + 1, 1:w + 1]
    from skimage.morphology import skeletonize
    width = min(4.0, max(0.5, num(el.get("trace_width"), 1.2))) if bold else 0.3  # mm
    min_len = max(0.0, num(el.get("trace_min"), 3.0))  # mm: shorter lines are left out
    r = max(1, int(round(width * RES)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    kh = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r | 1, r | 1))

    def long_enough(part):
        return np.count_nonzero(skeletonize(part)) / RES >= min_len
    out = []
    style = "satin" if bold else ("run3" if el.get("trace_repeat") == 3 else "run")
    for i in range(len(masks)):
        m = np.zeros((h, w), bool)
        if i in edge:
            n, cc = cv2.connectedComponents(edge[i].astype(np.uint8), connectivity=8)
            e = np.zeros((h, w), bool)
            for j in range(1, n):
                part = cc == j
                if np.count_nonzero(part) / RES >= max(min_len, 1.0):
                    e |= part
            if e.any():
                if bold:
                    # the line lies on the owner's side of the edge, so holes (eyes) keep their size
                    band = cv2.dilate(e.astype(np.uint8), k).astype(bool) & (lab == i)
                    m |= band | e
                else:
                    m |= e
        if i in lines:
            n, cc = cv2.connectedComponents(lines[i].astype(np.uint8), connectivity=8)
            for j in range(1, n):
                part = cc == j
                if long_enough(part):
                    # drawn lines keep their own weight (or the chosen width if that's bolder)
                    m |= cv2.dilate(part.astype(np.uint8), kh).astype(bool) if bold else part
        if m.any():
            out.append((color[i], m, (0, 0), style))
    # dark lines last, so they sit on top where they cross lighter ones
    out.sort(key=lambda t: -lum[[c for c in range(len(masks)) if color[c] == t[0]][0]])
    return out


def merge_vectors(els):
    """Several drawings -> one drawing (the opposite of split_vector): every part keeps its exact
    place on the hoop, so nothing moves. Turned / stretched pieces are folded into the points."""
    pts_mm = []  # every part in hoop millimetres
    for el in els:
        w, asp = num(el.get("width_mm"), 40), num(el.get("aspect"), 0.5)
        kx, ky = num(el.get("stretch_x"), 1.0), num(el.get("stretch_y"), 1.0)
        a = math.radians(num(el.get("rotation"), 0))
        c, s = math.cos(a), math.sin(a)
        x0, y0 = num(el.get("x"), 0), num(el.get("y"), 0)
        sc = w * math.sqrt(kx * ky)
        for p in el.get("parts", []):
            q = []
            for u, v in p.get("points", []):
                lx, ly = (u - 0.5) * w * kx, (v - asp / 2) * w * ky
                q.append((x0 + lx * c - ly * s, y0 + lx * s + ly * c))
            rings = []
            for r in p.get("rings", []) or []:
                rr = []
                for u, v in r:
                    lx, ly = (u - 0.5) * w * kx, (v - asp / 2) * w * ky
                    rr.append((x0 + lx * c - ly * s, y0 + lx * s + ly * c))
                rings.append(rr)
            pts_mm.append((p, q, rings, float(p.get("r") or 0) * sc, float(p.get("stroke") or 0) * sc))
    if not pts_mm:
        return None
    grow = max([max(r, st / 2) for _, _, _, r, st in pts_mm] + [0])
    xs = [x for _, q, _, _, _ in pts_mm for x, _ in q]
    ys = [y for _, q, _, _, _ in pts_mm for _, y in q]
    X0, X1, Y0, Y1 = min(xs) - grow, max(xs) + grow, min(ys) - grow, max(ys) + grow
    W = max(X1 - X0, 0.5)
    parts = []
    for p, q, rings, r, st in pts_mm:
        np_ = dict(p, points=[[round((x - X0) / W, 4), round((y - Y0) / W, 4)] for x, y in q])
        if rings:
            np_["rings"] = [[[round((x - X0) / W, 4), round((y - Y0) / W, 4)] for x, y in rr] for rr in rings]
        np_["r"] = round(r / W, 4)
        np_["stroke"] = round(st / W, 4)
        parts.append(np_)
    first = els[0]
    out = {k: v for k, v in first.items() if k in ("eyes", "group")}
    out.update(type="vector", width_mm=round(W, 2), aspect=round((Y1 - Y0) / W, 4), parts=parts,
               x=round((X0 + X1) / 2, 2), y=round((Y0 + Y1) / 2, 2), rotation=0,
               name=first.get("name") or "Drawing")
    return out


def trace_to_vector(el):
    """A traced picture -> an editable drawing: every traced line becomes a polyline with a handful
    of points (move / add / delete them), same place, size and threads as the trace."""
    el = dict(el, trace=True)
    layers = element_layers(el)
    W = num(el.get("width_mm"), 80)
    if not layers:
        return None
    H = layers[0][1].shape[0] / RES
    bold = el.get("trace_line") == "satin"
    stroke = (min(4.0, max(0.5, num(el.get("trace_width"), 1.2))) if bold else 0.3) / W
    parts = []
    for color, m, (ox, oy), style in layers:
        mm = m.copy()
        if not bold:  # hairline edges: thicken a touch so the skeleton is one clean line
            mm = cv2.dilate(mm.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        dt = cv2.distanceTransform(mm.astype(np.uint8), cv2.DIST_L2, 5)
        g = sg.StrokeGraph(mm, dt)
        for e in g.edges:
            pts = np.asarray(e["pts"], np.float32)
            if len(pts) < 2:
                continue
            closed = e["a"] == e["b"] and len(pts) > 6
            ap = cv2.approxPolyDP(pts.reshape(-1, 1, 2), 0.25 * RES, closed).reshape(-1, 2)
            if len(ap) < 2:
                continue
            q = [[round(float(x + ox) / RES / W, 4), round(float(y + oy) / RES / W, 4)] for x, y in ap]
            if closed:
                q.append(q[0])
            parts.append(dict(type="polyline", color=color, stroke=round(stroke, 4), points=q))
    if not parts:
        return None
    out = {k: v for k, v in el.items() if k in ("x", "y", "rotation", "stretch_x", "stretch_y", "group")}
    out.update(type="vector", width_mm=W, aspect=round(H / W, 4), parts=parts, eyes=False,
               name=(el.get("name") or "Picture") + " (lines)")
    return out


# border sewn under the letters: the letters' pull compensation spreads ~0.3-0.4 mm over it,
# so move it out by that much to keep the same visible width
FIRST_SHIFT = 0.4


def split_vector(el, by_color=False):
    """A drawing -> one drawing per object. Parts that touch belong together (bulbs on their wire,
    snow caps on their mountain); a line merely crossing a filled shape doesn't join them."""
    from shapely.geometry import Polygon, LineString, Point
    parts = el.get("parts", [])
    w = num(el.get("width_mm"), 40)
    asp = num(el.get("aspect"), 0.5)
    geoms = []
    for p in parts:
        pts = p.get("points", [])
        try:
            if p["type"] == "polygon" and len(pts) >= 3:
                g = Polygon(pts).buffer(0)
            elif p["type"] == "polyline" and len(pts) >= 2:
                g = LineString(pts).buffer(max(float(p.get("stroke") or 0), 0.004))
            elif p["type"] == "circle" and pts:
                g = Point(pts[0]).buffer(max(float(p.get("r") or 0), 0.004))
            else:
                g = None
        except Exception:
            g = None
        geoms.append(g)
    n = len(parts)
    root = list(range(n))
    def find(i):
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            gi, gj = geoms[i], geoms[j]
            if gi is None or gj is None:
                continue
            kinds = {parts[i]["type"], parts[j]["type"]}
            if kinds == {"polygon", "polyline"}:
                continue  # a wire running over a mountain is still two objects
            if gi.buffer(0.003).intersects(gj):
                root[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    if len(groups) < 2:
        if not by_color:
            return [el]
        # one connected object: split it by thread colour instead (mountain / snow caps)
        groups = {}
        for i, p in enumerate(parts):
            groups.setdefault((p.get("color") or "").lower(), []).append(i)
        if len(groups) < 2:
            # one colour too (a bunch of lines that touch): every line / shape on its own
            if n < 2:
                return [el]
            groups = {i: [i] for i in range(n)}
    rot = math.radians(num(el.get("rotation"), 0))
    out = []
    for idx in sorted(groups.values(), key=lambda g: g[0]):
        sub = [parts[i] for i in idx]
        us, vs = [], []
        for p in sub:
            grow = max(float(p.get("r") or 0), float(p.get("stroke") or 0) / 2)
            for u, v in p.get("points", []):
                us += [u - grow, u + grow]
                vs += [v - grow, v + grow]
        u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
        bw = max(u1 - u0, 1e-3)
        new = []
        for p in sub:
            q = dict(p, points=[[round((u - u0) / bw, 4), round((v - v0) / bw, 4)] for u, v in p["points"]])
            q["r"] = round(float(p.get("r") or 0) / bw, 4)
            q["stroke"] = round(float(p.get("stroke") or 0) / bw, 4)
            new.append(q)
        # new centre, in the (possibly turned) element's frame
        dx, dy = ((u0 + u1) / 2 - 0.5) * w, ((v0 + v1) / 2 - asp / 2) * w
        cx = num(el.get("x"), 0) + dx * math.cos(rot) - dy * math.sin(rot)
        cy = num(el.get("y"), 0) + dx * math.sin(rot) + dy * math.cos(rot)
        kinds = {p["type"] for p in sub}
        name = "Lights" if "circle" in kinds and len(sub) > 2 else "Line" if kinds == {"polyline"} else "Shapes"
        if len({(p.get("color") or "").lower() for p in sub}) == 1 and len(groups) > 1 and by_color:
            name = "%s (%s)" % (el.get("name") or "Drawing", shv_thread(sub[0].get("color") or "#333333"))
        out.append(dict(type="vector", name=name, parts=new, width_mm=round(bw * w, 1),
                        aspect=round((v1 - v0) / bw, 4), x=round(cx, 1), y=round(cy, 1), rotation=el.get("rotation", 0)))
    return out


EYE_MAX = 25.0  # mm2: enclosed holes up to this size are eyes, nostrils, buttons...
EYE_MIN = 1.5   # mm2 (smaller holes are grain in the picture - filled in, see fill_pinholes)
EYE_DEPTH = 0.5  # mm of stitching all round: a gap at the edge of a skirt isn't an eye


def fill_pinholes(m, max_mm2=EYE_MIN):
    """Grainy / textured artwork leaves tiny pinholes all over an area; the stitching would treat
    the area as a net of thin strokes. Fill holes smaller than max_mm2."""
    inv = (~m).astype(np.uint8)
    n, cc, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    h, w = m.shape
    out = m.copy()
    for j in range(1, n):
        x, y, bw, bh, area = st[j]
        if x == 0 or y == 0 or x + bw >= w or y + bh >= h:
            continue
        if area < max_mm2 * RES * RES:
            out[cc == j] = True
    return out


def eye_layers(layers, color):
    """Small holes completely surrounded by stitching (eyes, nostrils) come out ragged as holes in a
    fill. Digitizers sew them as a solid satin dot in a dark thread on top, overlapping the edge."""
    if not layers:
        return []
    h, w = layers[0][1].shape
    sewn = np.zeros((h, w), bool)
    for _, m, (ox, oy), _ in layers:
        sewn[oy:oy + m.shape[0], ox:ox + m.shape[1]] |= m[:h - oy, :w - ox]
    n, cc, st, _ = cv2.connectedComponentsWithStats((~sewn).astype(np.uint8), connectivity=4)
    dots = np.zeros((h, w), bool)
    for j in range(1, n):
        x, y, bw, bh, area = st[j]
        if x == 0 or y == 0 or x + bw >= w or y + bh >= h:
            continue  # open to the outside: background, not a hole
        if not (EYE_MIN * RES * RES <= area <= EYE_MAX * RES * RES):
            continue
        # eyes are compact (not slivers) and sit well inside the stitching
        if max(bw, bh) > 2.6 * max(1, min(bw, bh)) or area < 0.45 * bw * bh:
            continue
        hole = cc == j
        ring = cv2.dilate(hole.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * int(EYE_DEPTH * RES) + 1,) * 2)).astype(bool) & ~hole
        if (ring & ~sewn).sum() > 0.03 * ring.sum():
            continue
        dots |= hole
    if not dots.any():
        return []
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * int(0.3 * RES) + 1,) * 2)
    return [(color, cv2.dilate(dots.astype(np.uint8), k).astype(bool), (0, 0), "auto")]


def vector_layers(el):
    """Flat geometry (polygons, lines, dots) -> one mask per thread colour, in sewing order.
    Later colours sit on top; the parts underneath are knocked out there (with 0.4 mm overlap) so
    thread doesn't pile up - the way digitizers layer logo artwork."""
    w = num(el.get("width_mm"), 40)
    asp = num(el.get("aspect"), 0.5)
    # frame: the element box, grown evenly on each side to hold any part that pokes out
    # (kept symmetric so the element's centre stays the box centre)
    ext_u, ext_v = 0.0, 0.0
    for part in el.get("parts", []):
        grow = max(float(part.get("r") or 0), float(part.get("stroke") or 0))
        for u, v in part.get("points", []):
            ext_u = max(ext_u, -(u - grow), u + grow - 1)
            ext_v = max(ext_v, -(v - grow), v + grow - asp)
    pu, pv = ext_u * w + 2.0, ext_v * w + 2.0
    Wp, Hp = int(math.ceil((w + 2 * pu) * RES)), int(math.ceil((w * asp + 2 * pv) * RES))
    to_px = lambda u, v: (int(round((u * w + pu) * RES)), int(round((v * w + pv) * RES)))
    lines = {}  # thin strings/lines: sewn as a triple running stitch, not a satin cord
    order, masks = [], {}
    for part in el.get("parts", []):
        c = (part.get("color") or "#333333").lower()
        if c not in masks:
            masks[c] = np.zeros((Hp, Wp), np.uint8)
            order.append(c)
        m = masks[c]
        pts = np.array([to_px(u, v) for u, v in part.get("points", [])], np.int32)
        if not len(pts):
            continue
        if part.get("type") == "polygon" and len(pts) >= 3:
            rings = [pts] + [np.array([to_px(u, v) for u, v in r], np.int32) for r in part.get("rings", []) if len(r) >= 3]
            cv2.fillPoly(m, rings, 1)  # several rings in one call: inner ones become holes
        elif part.get("type") == "polyline" and len(pts) >= 2:
            sw = float(part.get("stroke") or 0) * w
            if sw < 0.9:
                lines.setdefault(c, []).append(pts)
                continue
            cv2.polylines(m, [pts], False, 1, int(round(sw * RES)), lineType=cv2.LINE_8)
        elif part.get("type") == "circle":
            r = max(int(round(0.6 * RES)), int(round(float(part.get("r") or 0) * w * RES)))
            cv2.circle(m, tuple(int(v) for v in pts[0]), r, 1, -1)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * int(0.4 * RES) + 1,) * 2)
    layers = []
    for i, c in enumerate(order):
        m = masks[c].astype(bool)
        for c2 in order[i + 1:]:  # knock out what a later colour covers, keeping a small overlap
            m &= ~cv2.erode(masks[c2], k).astype(bool)
        if m.any():
            layers.append((c, m, (0, 0), "auto"))
        if c in lines:
            lm = np.zeros((Hp, Wp), np.uint8)
            for pts in lines[c]:
                cv2.polylines(lm, [pts], False, 1, max(1, int(0.3 * RES)), lineType=cv2.LINE_8)
            layers.append((c, lm.astype(bool), (0, 0), "run"))
    if el.get("eyes", True):
        # the darkest thread already in the drawing (or near-black) for eyes
        lum = lambda hx: sum(int(hx[i:i + 2], 16) * f for i, f in ((1, 0.299), (3, 0.587), (5, 0.114)))
        dark = min(order, key=lum) if order else "#1e1e1e"
        layers += eye_layers(layers, dark if lum(dark) < 90 else "#1e1e1e")
    return layers


def _has_letter_colors(el):
    lc = el.get("letter_colors") or []
    return any(c and c.lower() != el.get("color", "#222222").lower() for c in lc)


def _color_groups(el):
    """[(hex, [char indices])] for per-letter colours, in order of first appearance
    (the main thread first, so each colour is sewn once)."""
    text, lc, base = el.get("text", ""), el.get("letter_colors") or [], el.get("color", "#222222")
    groups = {base.lower(): (base, [])}
    for i, ch in enumerate(text):
        c = (lc[i] if i < len(lc) else None) or base
        groups.setdefault(c.lower(), (c, []))[1].append(i)
    return [g for g in groups.values() if g[1]]


def _with_outline(el, m, style, layers=None):
    """Main layer(s) plus an optional satin border (sewn after, overlapping the edge by 0.3 mm)."""
    layers = layers or [(el.get("color", "#222222"), m, (0, 0), style)]
    ol = el.get("outline") or {}
    w = float(ol.get("width_mm") or 0)
    if w <= 0:
        return layers
    pad = int(round((w + max(0.0, num(ol.get("gap_mm"), 0)) + 1.5) * RES)) + 2
    main = [(c, lm, (pad, pad), st) for c, lm, _, st in layers]
    only = bool(ol.get("only"))  # just the edge, sewn over stitching that's already on the fabric
    first = bool(ol.get("first")) and not only
    # edge-only straddles the old edge (half on the letter, half off) so it covers the fill's ragged rim
    gap = (-w / 2 if only else num(ol.get("gap_mm"), -0.3)) + (FIRST_SHIFT if first else 0.0)
    st = "border:%g:%g%s" % (w, gap, ":first" if first else "")
    if ol.get("match"):  # each letter edged in its own thread
        border = [(c, np.pad(lm, pad), (0, 0), st) for c, lm, _, _ in layers]
    else:
        border = [(ol.get("color", "#000000"), np.pad(m, pad), (0, 0), st)]
    if only:
        return border
    # sewn first: the letters go on top and hide the border's hops between rings
    return border + main if first else main + border


def layer_objects(mask, style, P, entry=None):
    """Stitch every connected piece of a single-colour mask; nearest-neighbour order.
    Returns (objects [[(x,y) mm, ...], ...], kinds)."""
    if style and style.startswith("border"):
        _, w, gap, *flags = style.split(":")
        objs = sg.border_shape(mask, float(w), float(gap), P, entry)
        if "first" in flags:
            objs = sg.join_hidden([o for o in objs if o], mask, P)
        return [o for o in objs if o], ["border"] * len([o for o in objs if o])
    n, lab, stats, cents = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    comps = []
    for j in range(1, n):
        if stats[j, cv2.CC_STAT_AREA] < 4:
            continue
        x, y, w, h = stats[j, :4]
        sub = lab[y:y + h, x:x + w] == j
        sub = np.pad(sub, 2)
        comps.append((sub, (x - 2, y - 2), cents[j] / RES))
    objects, kinds = [], []
    cur = entry
    while comps:
        k = 0 if cur is None else min(range(len(comps)), key=lambda i: np.hypot(*(comps[i][2] - cur)))
        sub, (ox, oy), c = comps.pop(k)
        kind = sg.classify(sub, P) if style in (None, "", "auto") else style
        local_entry = None if cur is None else (cur[0] - ox / RES, cur[1] - oy / RES)
        if kind == "rows":
            objs = []
            for poly in sg.mask_to_polygons(sub, crisp=True):
                objs += sg.rows_blocks(poly, P, angle=0.0, start=local_entry)
        elif kind == "contour":
            objs = []
            for poly in sg.mask_to_polygons(sub, crisp=True):
                objs += sg.contour_fill(poly, P, start=local_entry)
        elif kind in ("satinrows", "star"):
            objs = []
            for poly in sg.mask_to_polygons(sub, crisp=True):
                objs += (sg.star_satin if kind == "star" else sg.satin_rows)(poly, P, start=local_entry)
        elif kind == "satin" and style == "satin":
            # asked for satin on purpose: let the sweeps run up to 12 mm (the machine's longest
            # single stitch is 12.7) before splitting them
            objs = sg.satin_shape(sub, replace(P, satin_max_width=max(P.satin_max_width, 12.0)), local_entry)
        elif kind == "satin":
            objs = sg.satin_shape(sub, P, local_entry)
        elif kind == "run":
            objs = sg.run_shape(sub, P, local_entry)
        elif kind == "run3":
            objs = sg.run_shape(sub, P, local_entry, repeats=3)
        elif kind == "satinfill":
            objs = []
            for poly in sg.mask_to_polygons(sub):
                objs += sg.satin_fill(poly, P, start=local_entry)
        else:
            objs = []
            for poly in sg.mask_to_polygons(sub, crisp=True):
                objs += sg.fill_shape(poly, P, start=local_entry)
        for o in objs:
            if o:
                objects.append([(p[0] + ox / RES, p[1] + oy / RES) for p in o])
                kinds.append(kind)
        if objects:
            cur = np.array(objects[-1][-1])
    return objects, kinds


def stretch_of(el):
    """Free (non-proportional) resize on top of the element's own size: (kx, ky)."""
    return (min(5.0, max(0.2, num(el.get("stretch_x"), 1.0))), min(5.0, max(0.2, num(el.get("stretch_y"), 1.0))))


def _stretched(res, el):
    """Stitch-level stretch, for things that come as stitches (embroidery fonts, stitch files)."""
    kx, ky = stretch_of(el)
    if kx == 1 and ky == 1:
        return res
    blocks, (w, h) = res
    return ([dict(b, objects=[[(x * kx, y * ky) for x, y in o] for o in b["objects"]]) for b in blocks], (w * kx, h * ky))


def element_blocks(el, P, fabric, progress=None):
    """Cached per element (position/rotation excluded). -> (blocks, (w_mm, h_mm))
    blocks: [{color, objects, kinds}] in element-local mm centred on (0,0)."""
    key_el = {k: v for k, v in el.items() if k not in ("x", "y", "rotation", "id", "name")}
    key = hashlib.sha1(json.dumps([key_el, fabric], sort_keys=True).encode()).hexdigest()
    if key in _cache:
        return _cache[key]
    if el.get("type") == "stitches":
        k = float(el.get("width_mm") or 1) / float(el.get("orig_w") or el.get("width_mm") or 1)
        blocks = [dict(color=b["color"], objects=[[(x * k, y * k) for x, y in o] for o in b["objects"]],
                       kinds=["stitches"] * len(b["objects"])) for b in el.get("blocks", [])]
        pts = [p for b in blocks for o in b["objects"] for p in o]
        size = ((max(p[0] for p in pts) - min(p[0] for p in pts), max(p[1] for p in pts) - min(p[1] for p in pts))
                if pts else (0, 0))
        res = _stretched((blocks, size), el)
        _cache[key] = res
        return res
    fam = embfont.family(el.get("font")) if el.get("type") == "text" else None
    if fam:
        res = _stretched(_emb_text_blocks(el, fam, P), el)
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = res
        return res
    layers = element_layers(el)
    if not layers:
        return [], (0, 0)
    kx, ky = stretch_of(el)
    if kx != 1 or ky != 1:
        # stretch the artwork itself (not the stitches), so the stitch spacing stays right
        def grow(m):
            nw, nh = max(1, int(round(m.shape[1] * kx))), max(1, int(round(m.shape[0] * ky)))
            return cv2.resize(m.astype(np.uint8) * 255, (nw, nh), interpolation=cv2.INTER_LINEAR) > 127
        layers = [(c, grow(m), (int(round(ox * kx)), int(round(oy * ky))), st) for c, m, (ox, oy), st in layers]
    W = max(m.shape[1] + ox for _, m, (ox, oy), _ in layers)
    H = max(m.shape[0] + oy for _, m, (ox, oy), _ in layers)
    cx, cy = W / 2 / RES, H / 2 / RES
    blocks = []
    entry = None
    for li, (color, m, (ox, oy), style) in enumerate(layers):
        if progress:
            progress(li / len(layers), "Stitching color %d of %d" % (li + 1, len(layers)))
        objs, kinds = layer_objects(m, style, P, entry)
        objs = [[(x + ox / RES - cx, y + oy / RES - cy) for x, y in o] for o in objs]
        if objs:
            blocks.append(dict(color=color, objects=objs, kinds=kinds))
            entry = np.array(objs[-1][-1]) + (cx - ox / RES, cy - oy / RES)
    res = (blocks, (W / RES, H / RES))
    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = res
    return res


def _emb_text_blocks(el, fam, P):
    """Text in a hand-digitized embroidery font: the font's own satin columns, stitch
    directions and sewing order, sewn with our fabric settings; optional satin border."""
    density = P.satin_spacing / 0.40  # fabric + density choice relative to the 0.4 mm standard
    objs, kinds, (w, h), f, missing, shapes, owners = embfont.layout_text(el, fam, P, density, with_owner=True)
    if not objs:
        return [], (0, 0)
    if _has_letter_colors(el):
        blocks = []
        for color, idxs in _color_groups(el):
            keep = set(idxs)
            sel = [k for k, ow in enumerate(owners) if ow in keep]
            if sel:
                blocks.append(dict(color=color, objects=[objs[k] for k in sel], kinds=[kinds[k] for k in sel]))
    else:
        blocks = [dict(color=el.get("color", "#222222"), objects=objs, kinds=kinds)]
    ol = el.get("outline") or {}
    bw = float(ol.get("width_mm") or 0)
    if bw > 0:
        only = bool(ol.get("only"))
        gap = -bw / 2 if only else num(ol.get("gap_mm"), -0.3)
        if only:
            blocks = []
        m, (x0, y0) = embfont.outline_mask(shapes, bw + max(0.0, num(ol.get("gap_mm"), 0)) + 1.5, RES)
        # close the counters between letters so the border hugs the word, like the TTF path does
        first = bool(ol.get("first")) and not only
        if first:
            gap += FIRST_SHIFT
        border = sg.border_shape(m, bw, gap, P, None if first or only else np.array(objs[-1][-1]) - (x0, y0))
        if first:
            border = sg.join_hidden([o for o in border if o], m, P)
        border = [[(x + x0, y + y0) for x, y in o] for o in border if o]
        if border:
            bb = dict(color=ol.get("color", "#000000"), objects=border, kinds=["border"] * len(border))
            if first:
                blocks.insert(0, bb)
            else:
                blocks.append(bb)
            pts = np.array([p for o in border for p in o])
            w = max(w, float(np.ptp(pts[:, 0])))
            h = max(h, float(np.ptp(pts[:, 1])))
    return blocks, (w, h)


# ----------------------------------------------------------------------------- whole design

def stitch_key(layout):
    """Everything besides the element itself that changes its stitches (cache key part)."""
    return "%s|%s" % (layout.get("fabric", "knit"), layout.get("density", "standard"))


def _bbox(b):
    p = np.array([q for o in b["objects"] for q in o], float)
    return (p.min(0) - 0.5, p.max(0) + 0.5) if len(p) else None


def _group_by_colour(blocks):
    """Fewer colour changes without breaking the layer order: a block may hop back next to an earlier block
    of its colour only if nothing sewn in between (another colour) overlaps it - otherwise it would end up under it."""
    out, boxes = [], []
    for b in blocks:
        bb = _bbox(b)
        col = b["color"].lower()
        pos = len(out)
        for j in range(len(out) - 1, -1, -1):
            o = out[j]
            if o["color"].lower() == col:
                pos = j + 1
                break
            ob = boxes[j]
            if bb is not None and ob is not None and not (bb[1][0] < ob[0][0] or ob[1][0] < bb[0][0] or bb[1][1] < ob[0][1] or ob[1][1] < bb[0][1]):
                break
        out.insert(pos, b)
        boxes.insert(pos, bb)
    return out


def build(layout, progress=None):
    """progress(fraction 0-1, message) is called as elements are stitched (for the busy badge)."""
    P = params_for(layout.get("fabric", "knit"), layout.get("density", "standard"))
    all_blocks, boxes = [], []
    els = layout.get("elements", [])
    for idx, el in enumerate(els):
        if el.get("hidden"):
            continue
        sub = None
        if progress:
            label = (el.get("text") or el.get("name") or el.get("kind") or el.get("type") or "").replace("\n", " ")[:24]
            sub = lambda f, msg, idx=idx, label=label: progress((idx + f) / max(1, len(els)), "%s: %s" % (label, msg) if label else msg)
            sub(0, "starting")
        blocks, (w, h) = element_blocks(el, P, stitch_key(layout), sub)
        x0, y0 = num(el.get("x"), 0), num(el.get("y"), 0)
        rot = math.radians(num(el.get("rotation"), 0))
        c, s = math.cos(rot), math.sin(rot)
        for b in blocks:
            objs = [[(x0 + px * c - py * s, y0 + px * s + py * c) for px, py in o] for o in b["objects"]]
            all_blocks.append(dict(color=b["color"], objects=objs, kinds=b["kinds"], element=idx))
        boxes.append(dict(index=idx, id=el.get("id"), x=x0, y=y0, w=w, h=h, rotation=num(el.get("rotation"), 0)))
    if layout.get("group_colors", True):
        all_blocks = _group_by_colour(all_blocks)
    merged = []
    for b in all_blocks:
        if merged and merged[-1]["color"].lower() == b["color"].lower():
            merged[-1]["objects"] += b["objects"]
            merged[-1]["kinds"] += b["kinds"]
        else:
            merged.append(dict(b))
    return merged, boxes, P


def _tie(o, P):
    """Lock stitches at both ends so nothing unravels after trims."""
    if len(o) < 2:
        return o
    def toward(a, b, L):
        d = math.hypot(b[0] - a[0], b[1] - a[1]) or 1
        return (a[0] + (b[0] - a[0]) / d * L, a[1] + (b[1] - a[1]) / d * L)
    a, b = o[0], o[1]
    y, z = o[-1], o[-2]
    t = P.tie_length
    return [a, toward(a, b, t), a] + o[1:-1] + [y, toward(y, z, t), y]


JOIN_GAP = 1.5  # mm: objects of one colour closer than this are joined with a stitch instead of a jump


def to_pattern(blocks, P):
    pat = pe.EmbPattern()
    first_block = True
    for b in blocks:
        th = pe.EmbThread()
        th.set_hex_color(b["color"])
        th.description = shv_thread(b["color"])
        pat.add_thread(th)
        if not first_block:
            pat.add_command(pe.COLOR_CHANGE)
        first_block = False
        last = None  # needle position after the previous object in this colour
        for i, o in enumerate(b["objects"]):
            if last is not None and math.hypot(o[0][0] - last[0], o[0][1] - last[1]) <= JOIN_GAP:
                # a gap this small isn't worth a thread to snip: just keep stitching across it
                for x, y in o:
                    pat.add_stitch_absolute(pe.STITCH, x * 10, y * 10)
                last = o[-1]
                continue
            o = _tie(list(o), P)
            if pat.stitches:
                pat.add_command(pe.TRIM)
            pat.add_stitch_absolute(pe.JUMP, o[0][0] * 10, o[0][1] * 10)
            for x, y in o:
                pat.add_stitch_absolute(pe.STITCH, x * 10, y * 10)
            last = o[-1]
    pat.add_command(pe.END)
    return pat


def thread_length_m(objects, per_stitch_mm=0.5):
    """Estimated top thread (metres) for sewn objects: the needle path plus ~0.5 mm per stitch
    for the loop down into the fabric; jumps between objects are trimmed and not counted.
    Bobbin use is roughly a third of this (the usual rule of thumb for satin and fill)."""
    total = 0.0
    for o in objects:
        if len(o) > 1:
            a = np.asarray(o, float)
            total += float(np.hypot(*np.diff(a, axis=0).T).sum()) + per_stitch_mm * len(o)
    return total / 1000.0


def off_hoop(blocks, hoop, tol=0.05):
    """True if any needle point lands outside the hoop's sewing field (origin = hoop centre)."""
    hw, hh = hoop[0] / 2 + tol, hoop[1] / 2 + tol
    return any(abs(x) > hw or abs(y) > hh for b in blocks for o in b["objects"] for x, y in o)


def stats(blocks, layout, pattern):
    hoop = layout.get("hoop", [100, 100])
    pts = [p for b in blocks for o in b["objects"] for p in o]
    warnings = []
    if not pts:
        return dict(stitches=0, colors=[], size=[0, 0], minutes=0, jumps=0, warnings=["Add some text, a shape or an image."])
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    size = [max(xs) - min(xs), max(ys) - min(ys)]
    outside = off_hoop(blocks, hoop)
    over = {}
    if outside:
        # how far past each edge, so the message says what to do
        for side, v in (("left", -min(xs) - hoop[0] / 2), ("right", max(xs) - hoop[0] / 2),
                        ("top", -min(ys) - hoop[1] / 2), ("bottom", max(ys) - hoop[1] / 2)):
            if v > 0.05:
                over[side] = round(float(v), 1)
        warnings.append("Off the %gx%g mm hoop by %s. Move or shrink it before saving." % (
            hoop[0], hoop[1], ", ".join("%.1f mm on the %s" % (v, k) for k, v in over.items())))
    for el in layout.get("elements", []):
        if el.get("type") == "text" and num(el.get("height_mm"), 10) < 5 and not embfont.family(el.get("font")):
            warnings.append('"%s" is %.1f mm tall - lettering under 5 mm may not stitch cleanly.'
                            % (el.get("text", "")[:20], float(el.get("height_mm"))))
        fam = embfont.family(el.get("font")) if el.get("type") == "text" else None
        if fam:
            lo, hi = embfont.size_range(fam)
            hmm = num(el.get("height_mm"), 10)
            if hmm < lo * 0.95 or hmm > hi * 1.05:
                warnings.append("%s is digitized for %.0f-%.0f mm letters; at %.1f mm it may sew poorly. Pick another font or resize."
                                % (fam["name"].lstrip("✦ "), lo, hi, hmm))
    for el in layout.get("elements", []):
        if el.get("type") == "stitches" and el.get("orig_w"):
            k = float(el.get("width_mm") or 1) / float(el["orig_w"])
            if abs(k - 1) > 0.1:
                warnings.append('"%s" is resized to %d%% - stitch files get too dense or too sparse past about 10%%.'
                                % (el.get("name", "Stitch file")[:20], round(k * 100)))
    counts = [sum(1 for _ in o) + 6 for b in blocks for o in [sum(b["objects"], [])]]
    total = sum(1 for s in pattern.stitches if s[2] == pe.STITCH)
    jumps = sum(len(b["objects"]) for b in blocks) - len(blocks)
    colors = [dict(hex=b["color"], thread=shv_thread(b["color"]), stitches=sum(len(o) + 4 for o in b["objects"]),
                   kinds=sorted(set(b["kinds"])), thread_m=round(thread_length_m(b["objects"]), 1)) for b in blocks]
    top_m = sum(c["thread_m"] for c in colors)
    minutes = total / 450 + 0.75 * max(0, len(blocks) - 1) + 0.15 * jumps
    return dict(stitches=int(total), colors=colors, size=[round(float(size[0]), 1), round(float(size[1]), 1)],
                minutes=round(minutes, 1), jumps=jumps, warnings=warnings, outside=outside, over=over,
                thread_m=round(top_m, 1), bobbin_m=round(top_m / 3, 1))


# ----------------------------------------------------------------------------- preview

def render_preview(blocks, hoop, scale=8, ss=2):
    """Transparent PNG of the stitches (thread-like shading), hoop field centred."""
    W, H = int(hoop[0] * scale), int(hoop[1] * scale)
    k = scale * ss
    im = Image.new("RGBA", (W * ss, H * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ox, oy = W * ss / 2, H * ss / 2
    tw = max(1, int(0.44 * k))
    for b in blocks:
        r, g, bl = hex_to_rgb(b["color"])
        dark = (int(r * 0.72), int(g * 0.72), int(bl * 0.72), 255)
        mid = (r, g, bl, 255)
        lite = (min(255, int(r + (255 - r) * 0.45)), min(255, int(g + (255 - g) * 0.45)), min(255, int(bl + (255 - bl) * 0.45)), 255)
        hl = max(1, tw // 3)
        off = 0.1 * k
        for o in b["objects"]:
            pts = [(ox + x * k, oy + y * k) for x, y in o]
            # draw stitch by stitch in sewing order so later stitches cover earlier ones
            for p, q in zip(pts, pts[1:]):
                d.line([p, q], fill=dark, width=tw + max(1, tw // 6))
                d.line([p, q], fill=mid, width=tw)
                d.line([(p[0] - off, p[1] - off), (q[0] - off, q[1] - off)], fill=lite, width=hl)
    im = im.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ----------------------------------------------------------------------------- export

FORMATS = {"vp3": "Husqvarna Viking / Pfaff (.vp3)", "pes": "Brother / Baby Lock (.pes)", "dst": "Tajima (.dst)",
           "jef": "Janome (.jef)", "exp": "Melco / Bernina (.exp)",
           "zip": "Designer I floppy on another PC (.zip, unzip onto the floppy)",
           "shv": "Single .shv (for other software; the machine won't find it alone)"}


def export(pattern, fmt, path):
    pe.write(pattern, path) if fmt != "dst" else pe.write_dst(pattern, path)
    return path
