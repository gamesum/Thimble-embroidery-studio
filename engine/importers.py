"""Import files that aren't plain pictures.

- Embroidery files (PES, DST, JEF, VP3, SHV, ... anything pyembroidery reads) become a
  'stitches' element: the file's own stitches, moved/turned/recoloured as a whole.
- SVG vector art becomes a 'vector' drawing element (exact shapes and colours, stitched
  like the drawings the AI produces) instead of being traced from pixels.
"""
import math, os

import numpy as np
import pyembroidery as pe

EMB_EXTS = {"." + f["extension"] for f in pe.supported_formats()
            if f.get("reader") and f.get("category") == "embroidery"}
DEFAULT_THREADS = ["#1d1d1d", "#b8322a", "#2b4c7e", "#c9a24a", "#3f7d4e", "#7a4f9a", "#e07b24", "#f2f0ea"]


def read_embroidery(path, name="Embroidery file"):
    """-> 'stitches' element: colour blocks of stitch runs (mm, centred on 0,0)."""
    pat = pe.read(path)
    if pat is None or not pat.stitches:
        raise ValueError("That embroidery file couldn't be read, or it has no stitches.")
    threads = [t.hex_color() for t in pat.threadlist]
    blocks, objs, cur, ci = [], [], [], 0

    def end_run():
        nonlocal cur
        if len(cur) > 1:
            objs.append(cur)
        cur = []

    def end_block():
        nonlocal objs
        end_run()
        if objs:
            color = threads[ci] if ci < len(threads) else DEFAULT_THREADS[ci % len(DEFAULT_THREADS)]
            blocks.append(dict(color=color, objects=objs))
        objs = []

    for x, y, cmd in pat.stitches:
        c = cmd & pe.COMMAND_MASK
        if c == pe.STITCH:
            cur.append((x / 10.0, y / 10.0))
        elif c in (pe.JUMP, pe.TRIM):
            end_run()
        elif c in (pe.COLOR_CHANGE, pe.COLOR_BREAK, pe.NEEDLE_SET):
            if objs or len(cur) > 1:
                end_block()
                ci += 1
            else:
                cur = []
        elif c == pe.END:
            break
    end_block()
    if not blocks:
        raise ValueError("That embroidery file has no stitches.")
    pts = np.array([p for b in blocks for o in b["objects"] for p in o])
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for b in blocks:
        b["objects"] = [[[round(px - cx, 2), round(py - cy, 2)] for px, py in o] for o in b["objects"]]
    w, h = float(x1 - x0), float(y1 - y0)
    return dict(type="stitches", name=name, blocks=blocks, width_mm=round(w, 1), orig_w=round(w, 2),
                aspect=round(h / w, 4) if w else 1.0, x=0, y=0, rotation=0)


def read_svg(path, hoop, name="Vector art"):
    """-> 'vector' drawing element from an SVG's filled shapes and strokes."""
    import svgelements as se
    svg = se.SVG.parse(path, reify=True, ppi=96)
    raw = []  # (type, rings[[(x,y)]], color, stroke_px)
    for e in svg.elements():
        if not isinstance(e, se.Shape):
            continue
        try:
            path_ = se.Path(e)
        except Exception:
            continue
        rings = []
        for sp in path_.as_subpaths():
            sp = se.Path(sp)
            try:
                L = sp.length(error=1e-2)
            except Exception:
                L = 0
            if not L:
                continue
            n = max(8, min(400, int(L / 1.5)))
            pts = np.asarray(sp.npoint(np.linspace(0, 1, n)), float)
            if len(pts) >= 2 and np.isfinite(pts).all():
                rings.append(pts)
        if not rings:
            continue
        fill, stroke = e.fill, e.stroke
        if fill is not None and fill.value is not None and fill.alpha > 20:
            raw.append(("polygon", rings, fill.hexrgb, 0.0))
        sw = float(getattr(e, "stroke_width", 0) or 0)
        if stroke is not None and stroke.value is not None and stroke.alpha > 20 and sw > 0:
            for r in rings:
                raw.append(("polyline", [r], stroke.hexrgb, sw))
    if not raw:
        raise ValueError("That SVG has no filled shapes or lines I can stitch.")
    allp = np.vstack([r for _, rings, _, _ in raw for r in rings])
    x0, y0 = allp.min(0)
    x1, y1 = allp.max(0)
    W = max(x1 - x0, 1e-6)
    parts = []
    for kind, rings, color, sw in raw:
        norm = [[[round((x - x0) / W, 4), round((y - y0) / W, 4)] for x, y in r] for r in rings]
        part = dict(type=kind, points=norm[0], r=0, stroke=round(sw / W, 4) if kind == "polyline" else 0, color=color.lower())
        if kind == "polygon" and len(norm) > 1:
            part["rings"] = norm[1:]  # holes / extra islands of the same shape
        parts.append(part)
    # a full-size shape behind everything is a page background, not art
    from .ai import _drop_backdrop
    aspect = (y1 - y0) / W
    parts = _drop_backdrop(parts, aspect) if len(parts) >= 4 else parts
    width = min(0.8 * hoop[0], 0.8 * hoop[1] / max(aspect, 1e-6))
    return dict(type="vector", name=name, parts=parts, width_mm=round(width, 1), aspect=round(aspect, 4),
                x=0, y=0, rotation=0)
