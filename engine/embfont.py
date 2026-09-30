"""Hand-digitized embroidery fonts (Ink/Stitch font library format).

Each glyph in these fonts was digitized by a person: satin columns are drawn as two rails
(+ optional rungs) in stitching order, with running-stitch travel paths between them and
per-column settings (pull compensation, underlay, short stitches, split length). We render
them with a port of Ink/Stitch's satin algorithm, so lettering comes out the way the font's
digitizer intended - clean joints, correct stitch angles, tucked crossbars - at any size.
"""
import json, math, os, re
import xml.etree.ElementTree as ET

import numpy as np
from shapely.geometry import Point as ShPoint, Polygon as ShPolygon
from shapely.geometry import LineString, Point as SPoint, MultiLineString, Polygon
from shapely.ops import nearest_points
import svgelements

from . import stitchgen as sg

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts_emb")
NS = {"svg": "http://www.w3.org/2000/svg", "inkscape": "http://www.inkscape.org/namespaces/inkscape",
      "sodipodi": "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd", "inkstitch": "http://inkstitch.org/namespace"}
INK = "{%s}" % NS["inkscape"]
IST = "{%s}" % NS["inkstitch"]
SOD = "{%s}" % NS["sodipodi"]


def num(v, default=0.0):
    """A number from a design piece; empty/None/garbage falls back to the default."""
    try:
        f = float(v)
        return f if f == f else float(default)  # NaN -> default
    except (TypeError, ValueError):
        return float(default)

# ----------------------------------------------------------------------------- catalogue

CATEGORY = [("handwriting", "Script"), ("italic", "Script"), ("serif", "Serif"), ("sans_serif", "Block"), ("display", "Display")]


_qa = None


def catalogue(qa=True):
    """[{name, dir, category, variants:[dir...], license}] for the embroidery fonts on disk.
    qa=True leaves out fonts that failed tools/font_qa.py."""
    global _qa
    fams = {}
    if not os.path.isdir(ROOT):
        return []
    if qa and _qa is None:
        p = os.path.join(ROOT, "_meta", "qa.json")
        _qa = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    for d in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, d, "font.json")
        if not os.path.exists(p):
            continue
        meta = json.load(open(p, encoding="utf-8"))
        kws = meta.get("keywords", [])
        base = re.sub(r"(_small|_Small|_tiny|_small_AGS)$", "", d)
        if base.endswith("_small"):
            base = base[:-6]
        if d == "apex_simple_small_AGS":
            base = "apex_simple_AGS"
        fam = fams.setdefault(base, dict(dir=base, variants=[], name=None, category="Block", license=""))
        fam["variants"].append(d)
        if d == base or fam["name"] is None:
            fam["name"] = meta.get("name", d).strip()
            fam["license"] = meta.get("font_license", "")
            cat = "Block"
            for k, c in CATEGORY:
                if k in kws:
                    cat = c
                    break
            if "running_stitch" in kws:
                cat = "Script" if "handwriting" in kws else "Display"
            fam["category"] = cat
    out = []
    for base, f in fams.items():
        if base not in f["variants"]:
            continue  # a small variant without its main font
        if qa and _qa and not _qa.get(base, {}).get("ok", True):
            continue  # failed the quality check
        f["name"] = "✦ " + f["name"].replace(" KOR", "").replace(" AGS", "").replace(" FI", "")
        out.append(f)
    return sorted(out, key=lambda f: f["name"].lower())


# ----------------------------------------------------------------------------- SVG helpers

def _parse_transform(s):
    m = np.eye(3)
    if not s:
        return m
    for name, args in re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", s):
        v = [float(x) for x in re.split(r"[\s,]+", args.strip()) if x]
        t = np.eye(3)
        if name == "matrix":
            t = np.array([[v[0], v[2], v[4]], [v[1], v[3], v[5]], [0, 0, 1]])
        elif name == "translate":
            t = np.array([[1, 0, v[0]], [0, 1, v[1] if len(v) > 1 else 0], [0, 0, 1]])
        elif name == "scale":
            t = np.array([[v[0], 0, 0], [0, v[1] if len(v) > 1 else v[0], 0], [0, 0, 1]])
        elif name == "rotate":
            a = math.radians(v[0])
            r = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
            if len(v) == 3:
                t = np.array([[1, 0, v[1]], [0, 1, v[2]], [0, 0, 1]]) @ r @ np.array([[1, 0, -v[1]], [0, 1, -v[2]], [0, 0, 1]])
            else:
                t = r
        elif name == "skewX":
            t = np.array([[1, math.tan(math.radians(v[0])), 0], [0, 1, 0], [0, 0, 1]])
        elif name == "skewY":
            t = np.array([[1, 0, 0], [math.tan(math.radians(v[0])), 1, 0], [0, 0, 1]])
        m = m @ t
    return m


def _to_mm_len(val):
    """SVG length -> mm (plain numbers are CSS px)."""
    m = re.match(r"([\d.eE+-]+)\s*([a-z%]*)", val or "")
    if not m:
        return None
    n, u = float(m.group(1)), m.group(2)
    return n * {"mm": 1, "cm": 10, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6, "px": 25.4 / 96, "": 25.4 / 96}.get(u, 25.4 / 96)


def _subpaths(d, M):
    """Path data -> [(points Nx2 finely sampled, nodes Kx2 bezier end points)] in user units, transformed."""
    out = []
    try:
        path = svgelements.Path(d)
    except Exception:
        return out
    for sp in path.as_subpaths():
        pts, nodes = [], []
        for seg in sp:
            if isinstance(seg, svgelements.Move):
                pts.append((seg.end.x, seg.end.y))
                nodes.append((seg.end.x, seg.end.y))
                continue
            if seg.start is None or seg.end is None:
                continue
            if isinstance(seg, (svgelements.Line, svgelements.Close)):
                pts.append((seg.end.x, seg.end.y))
            else:
                # rough length from 9 samples, then sample the whole curve in one vector call
                probe = np.asarray(seg.npoint(np.linspace(0, 1, 9)), float)
                n = max(4, int(np.hypot(*np.diff(probe, axis=0).T).sum() / 0.15) + 1)
                pts.extend(map(tuple, np.asarray(seg.npoint(np.linspace(0, 1, n + 1)[1:]), float)))
            nodes.append((seg.end.x, seg.end.y))
        if len(pts) >= 2:
            P = np.array(pts, float)
            N = np.array(nodes, float)
            P = (np.c_[P, np.ones(len(P))] @ M.T)[:, :2]
            N = (np.c_[N, np.ones(len(N))] @ M.T)[:, :2]
            keep = np.r_[True, np.hypot(*np.diff(P, axis=0).T) > 1e-6]
            out.append((P[keep], N))
    return out


def _style(el):
    st = {}
    for kv in (el.get("style") or "").split(";"):
        if ":" in kv:
            k, v = kv.split(":", 1)
            st[k.strip()] = v.strip()
    for k in ("fill", "stroke", "display"):
        if el.get(k) is not None:
            st.setdefault(k, el.get(k))
    return st


# ----------------------------------------------------------------------------- font loading

class _LazyGlyphs(dict):
    """dict of glyph -> elements that parses a glyph the first time it is looked up."""

    def __init__(self, raw, parse):
        super().__init__()
        self._rawkeys, self._parse = raw, parse

    def __contains__(self, k):
        return k in self._rawkeys

    def __missing__(self, k):
        if k not in self._rawkeys:
            raise KeyError(k)
        v = self._parse(k)
        self[k] = v
        return v


class EmbFont:
    _cache = {}

    @classmethod
    def load(cls, d):
        if d not in cls._cache:
            cls._cache[d] = cls(d)
        return cls._cache[d]

    def __init__(self, d):
        self.dir = d
        base = os.path.join(ROOT, d)
        self.meta = json.load(open(os.path.join(base, "font.json"), encoding="utf-8"))
        tree = ET.parse(os.path.join(base, "glyphs.svg"))
        root = tree.getroot()
        vb = [float(x) for x in re.split(r"[\s,]+", root.get("viewBox", "").strip()) if x] if root.get("viewBox") else None
        w_mm, h_mm = _to_mm_len(root.get("width")), _to_mm_len(root.get("height"))
        if vb and w_mm:
            self.mm = w_mm / vb[2]
            view_h = vb[3]
        else:
            self.mm = 25.4 / 96
            view_h = (h_mm / self.mm) if h_mm else 0
        # guides (Inkscape stores them y-up from the bottom of the page)
        guides = {}
        for g in root.iter(SOD + "guide"):
            lab = g.get(INK + "label") or g.get("inkscape:label")
            pos = g.get("position")
            if lab and pos:
                x, y = [float(v) for v in pos.split(",")]
                guides[lab] = y
        self._raw = {}   # glyph -> [(kind, d, M, params)]; curves are only flattened when used
        self._uu = {}
        self._order = {}
        self.metrics = {}
        self._walk(root, np.eye(3))
        uu = self._uu_glyph
        # baseline: choose the reading of the guide that lines up with the bottom of flat letters
        probe = [c for c in "HEILTNMxnmuzvw" if c in self._raw and uu(c)]
        bottoms = [max(p[:, 1].max() for e in uu(c) for p, _ in e["sub"]) for c in probe[:4]]
        guess = float(np.median(bottoms)) if bottoms else 0.0
        if "baseline" in guides:
            cands = [view_h - guides["baseline"], guides["baseline"]]
            self.baseline = min(cands, key=lambda v: abs(v - guess)) if bottoms else cands[0]
        else:
            self.baseline = guess
        tops = [min(p[:, 1].min() for e in uu(c) for p, _ in e["sub"]) for c in "HEIT" if c in self._raw and uu(c)]
        if "caps" in guides and "baseline" in guides:
            self.cap_uu = abs(guides["caps"] - guides["baseline"])
        elif tops:
            self.cap_uu = self.baseline - float(np.median(tops))
        else:
            asc = [min(p[:, 1].min() for e in uu(c) for p, _ in e["sub"]) for c in "bdhkl" if c in self._raw and uu(c)]
            self.cap_uu = (self.baseline - float(np.median(asc))) if asc else 30.0
        self.cap_mm = self.cap_uu * self.mm
        self.glyphs = _LazyGlyphs(self._raw, self._norm_glyph)

    def _uu_glyph(self, ch):
        """Glyph elements in SVG user units (parsed on first use)."""
        if ch not in self._uu:
            els = []
            for kind, d, M, params in self._raw.get(ch, []):
                sub = _subpaths(d, M)
                if sub:
                    els.append(dict(kind=kind, sub=sub, params=params))
            self._uu[ch] = els
        return self._uu[ch]

    def _norm_glyph(self, ch):
        """Move a glyph so its baseline is y=0 and its ink starts at x=0 (like Ink/Stitch), in mm."""
        els = [dict(e) for e in self._uu_glyph(ch)]
        xs = [p[:, 0].min() for e in els for p, _ in e["sub"]] or [0]
        xe = [p[:, 0].max() for e in els for p, _ in e["sub"]] or [0]
        min_x = min(xs)
        for e in els:
            e["sub"] = [((p - [min_x, self.baseline]) * self.mm, (n - [min_x, self.baseline]) * self.mm) for p, n in e["sub"]]
        self.metrics[ch] = dict(min_x=min_x * self.mm, width=(max(xe) - min_x) * self.mm)
        return els

    def _walk(self, el, M, glyph=None):
        tag = el.tag.split("}")[-1]
        M = M @ _parse_transform(el.get("transform"))
        if tag == "g":
            lab = el.get(INK + "label") or ""
            if lab.startswith("GlyphLayer-"):
                glyph = lab[11:]
                self._raw.setdefault(glyph, [])
            for ch in el:
                self._walk(ch, M, glyph)
            return
        if tag == "svg":
            for ch in el:
                self._walk(ch, M, glyph)
            return
        if glyph is None or tag not in ("path", "rect", "line", "polyline", "polygon", "circle", "ellipse"):
            return
        if el.get(INK + "connection-start") or el.get(INK + "connection-end"):
            return  # command connectors, not stitches
        d = el.get("d")
        if tag != "path" or not d:
            return
        st = _style(el)
        params = {k[len(IST):]: v for k, v in el.attrib.items() if k.startswith(IST)}
        fill = st.get("fill", "none")
        if params.get("satin_column", "").lower() == "true":
            kind = "satin"
        elif fill not in ("none", "") and not fill.startswith("url"):
            kind = "fill"
        else:
            kind = "run"
        self._raw[glyph].append((kind, d, M, params))

    # -- metrics in mm at native size
    def _fix_accents(self, g, els):
        """Accented letters: many fonts sit the accent almost on the letter. Pros make it a
        touch smaller and leave a clear gap so it reads cleanly and doesn't merge when sewn."""
        import unicodedata
        dec = [c for c in unicodedata.decomposition(g).split() if not c.startswith("<")]
        if not any(0x300 <= int(c, 16) <= 0x36F for c in dec):
            return els
        def box(e):
            a = np.vstack([p for p, _ in e["sub"]])
            return a[:, 0].min(), a[:, 1].min(), a[:, 0].max(), a[:, 1].max()
        cap = self.cap_mm or 1.0
        solid = [(e, box(e)) for e in els if e["kind"] != "run"]
        body = [b for e, b in solid if b[3] > -0.3 * cap]
        if not body:
            return els
        body_top = min(b[1] for b in body)
        acc = [(e, b) for e, b in solid if b[3] < body_top - 0.02 * cap]
        if not acc:
            return els
        cx = (min(b[0] for _, b in acc) + max(b[2] for _, b in acc)) / 2
        by = max(b[3] for _, b in acc)
        k = 0.8
        shift = max(0.0, (0.16 * cap) - (body_top - by))  # gap under the accent >= 16% of cap height
        ids = {id(e) for e, _ in acc}
        out = []
        for e in els:
            if id(e) in ids:
                mv = lambda a: (np.asarray(a, float) - [cx, by]) * k + [cx, by - shift]
                out.append(dict(e, sub=[(mv(p), mv(n)) for p, n in e["sub"]]))
            elif e["kind"] == "run":
                a = np.vstack([p for p, _ in e["sub"]])
                if a[:, 1].min() < body_top - 0.02 * cap and a[:, 1].max() > body_top:
                    continue  # walk from the accent down to the letter would show in the gap: jump instead
                out.append(e)
            else:
                out.append(e)
        return out

    def ordered(self, g):
        """Glyph elements in sewing order. The rule pros use on every letter: a stroke whose
        end is tucked into another stroke sews first, so the covering stroke (the spine of
        B/D/E/F/P/R, the bar of T, the stem of d) goes over the seam and hides it. Travel
        runs stay with the stroke they lead into; otherwise the digitizer's order is kept."""
        if g in self._order:
            return self._order[g]
        els = self._fix_accents(g, self.glyphs[g])
        # group each satin with the runs just before it (its travel / underlay walk)
        groups, pend = [], []
        for e in els:
            pend.append(e)
            if e["kind"] != "run":
                groups.append(pend)
                pend = []
        if pend:
            if groups:
                groups[-1] += pend
            else:
                groups.append(pend)
        info = []
        tol = 0.12  # mm (glyph data is already in mm at native size)
        for grp in groups:
            head = grp[-1] if grp[-1]["kind"] != "run" else None
            poly, ends = None, []
            if head is not None and head["kind"] == "satin":
                rails, _ = _rails_rungs(head["sub"], head["params"])
                if rails is not None:
                    try:
                        poly = ShPolygon(np.vstack([rails[0], rails[1][::-1]])).buffer(0)
                        ends = [[ShPoint(np.asarray(rails[0][i]) + (np.asarray(rails[1][i]) - np.asarray(rails[0][i])) * t)
                                 for t in np.linspace(0.1, 0.9, 9)] for i in (0, -1)]
                    except Exception:
                        poly = None
            elif head is not None and head["kind"] == "fill":
                try:
                    poly = ShPolygon(head["sub"][0][0]).buffer(0)
                except Exception:
                    poly = None
            info.append((poly, ends))
        n = len(groups)
        # bridge hairline gaps: a stroke that stops just short of another (the bar of an e, the
        # arm of an r) would leave a sliver of fabric showing; run it on so it tucks under
        bridge = [[0.0, 0.0] for _ in range(n)]
        for k in range(n):
            poly, ends = info[k]
            head = groups[k][-1]
            if poly is None or head["kind"] != "satin" or not ends:
                continue
            others = [info[j][0] for j in range(n) if j != k and info[j][0] is not None]
            if not others:
                continue
            rails, _ = _rails_rungs(head["sub"], head["params"])
            for side, i in enumerate((0, -1)):
                if any(_tucked(q.buffer(tol), ends[side], 0.5) for q in others):
                    continue
                a, b = np.asarray(rails[0][i], float), np.asarray(rails[1][i], float)
                width = math.hypot(*(b - a))
                t = 0.1 if side == 0 else 0.9
                inner = (np.asarray(LineString(rails[0]).interpolate(t, normalized=True).coords[0]) +
                         np.asarray(LineString(rails[1]).interpolate(t, normalized=True).coords[0])) / 2
                out = (a + b) / 2 - inner
                L = math.hypot(*out)
                if L < 1e-9 or width < 1e-9:
                    continue
                out /= L
                pts = [np.array([p.x, p.y]) for p in ends[side]]
                step, d = width * 0.04, 0.0
                while d < width * 0.6:
                    d += step
                    moved = [ShPoint(*(q + out * d)) for q in pts]
                    if any(_cover(q, moved) >= 0.5 for q in others):
                        d += width * 0.12       # reach in far enough to tuck under
                        bridge[k][side] = d
                        info[k][1][side] = [ShPoint(*(q + out * d)) for q in pts]
                        break
        for k in range(n):
            if any(bridge[k]):
                groups[k][-1] = dict(groups[k][-1], ext=tuple(bridge[k]))
        before = [set() for _ in range(n)]  # before[j] = groups that must sew before j
        for i in range(n):
            for j in range(n):
                if i == j or info[j][0] is None or not info[i][1]:
                    continue
                # i tucks into j more than j tucks into i -> i sews first (diagonals of M/N
                # under the outer stems, arms under the spine, stem under the T bar)
                c_ij = max(_cover(info[j][0].buffer(tol), e) for e in info[i][1])
                c_ji = max((_cover(info[i][0].buffer(tol), e) for e in info[j][1]), default=0.0) if info[i][0] is not None else 0.0
                if c_ij >= 0.3 and c_ij > c_ji + 0.1:
                    before[j].add(i)
        for i in range(n):
            for j in list(before[i]):
                if i in before[j]:          # mutual (V point, X crossing): keep font order
                    before[i].discard(j)
                    before[j].discard(i)
        done, order = set(), []
        while len(order) < n:
            ready = [k for k in range(n) if k not in done and before[k] <= done]
            k = ready[0] if ready else min(k for k in range(n) if k not in done)  # cycle: fall back
            done.add(k)
            order.append(k)
        # square the corners: where a neighbouring stroke sticks out past a column's free end
        # (arm tops beyond the spine of B/D/E), run the column on to meet its edge
        for k in range(n):
            poly, ends = info[k]
            head = groups[k][-1]
            if poly is None or head["kind"] != "satin" or not ends:
                continue
            others = [info[j][0] for j in range(n) if j != k and info[j][0] is not None]
            if not others:
                continue
            from shapely.ops import unary_union
            uni = unary_union(others)
            rails, _ = _rails_rungs(head["sub"], head["params"])
            ext = [0.0, 0.0]
            for side, (i, ii) in enumerate(((0, 1), (-1, -2))):
                a, b = np.asarray(rails[0][i], float), np.asarray(rails[1][i], float)
                if any(_tucked(q.buffer(tol), ends[side]) for q in others):
                    continue  # tucked end: the covering stroke hides it
                width = math.hypot(*(b - a))
                inner = (LineString(rails[0]).interpolate(0.1 if side == 0 else 0.9, normalized=True).coords[0],
                         LineString(rails[1]).interpolate(0.1 if side == 0 else 0.9, normalized=True).coords[0])
                out = (a + b) / 2 - (np.asarray(inner[0]) + np.asarray(inner[1])) / 2
                L = math.hypot(*out)
                if L < 1e-9 or width < 1e-9:
                    continue
                out /= L
                samples = [a + (b - a) * t for t in np.linspace(0.05, 0.95, 9)]
                step, d = width * 0.04, 0.0
                while d + step <= width * 0.45:
                    cov = sum(uni.contains(ShPoint(*(q + out * (d + step)))) for q in samples)
                    if cov < 3:
                        break
                    d += step
                ext[side] = d
            prev = head.get("ext") or (0.0, 0.0)
            ext = [max(ext[0], prev[0]), max(ext[1], prev[1])]
            if any(ext):
                groups[k][-1] = dict(head, ext=tuple(ext))
        res = [e for k in order for e in groups[k]]
        if order != sorted(order):
            # layers were re-stacked: the digitizer's walks no longer lead into the stroke
            # after them, and a walk sewn after a satin would sit on top of it. Sew every
            # walk first (as underlay, hidden), then the strokes in the new order.
            res = [e for e in res if e["kind"] == "run"] + [e for e in res if e["kind"] != "run"]
        self._order[g] = res
        return res

    def advance(self, ch):
        adv = self.meta.get("horiz_adv_x", {}) or {}
        if ch in adv:
            return adv[ch] * self.mm
        d = self.meta.get("horiz_adv_x_default")
        m = self.metrics.get(ch)
        if d is not None:
            return d * self.mm
        return (m["width"] + m["min_x"]) if m else 0

    def kerning(self, a, b):
        kp = self.meta.get("kerning_pairs", {}) or {}
        v = kp.get("%s %s" % (a, b), kp.get(a + b, 0))
        return v * self.mm

    def word_space(self):
        return (self.meta.get("horiz_adv_x_space") or 20) * self.mm

    def glyph_for(self, ch):
        if ch in self.glyphs and self.glyphs[ch]:
            return ch
        alt = ch.swapcase()
        if alt in self.glyphs and self.glyphs[alt]:
            return alt
        return None


def size_range(family):
    """(min, max) capital height in mm the family's variants were digitized for."""
    lo, hi = [], []
    for v in family["variants"]:
        f = EmbFont.load(v)
        lo.append(f.meta.get("min_scale", 1.0) * f.cap_mm)
        hi.append(f.meta.get("max_scale", 1.0) * f.cap_mm)
    return min(lo), max(hi)


def pick_variant(family, cap_mm):
    """Choose the digitized variant (regular / small / tiny) best suited to the requested size."""
    best, bd = None, None
    for v in family["variants"]:
        f = EmbFont.load(v)
        lo = f.meta.get("min_scale", 1.0) * f.cap_mm
        hi = f.meta.get("max_scale", 1.0) * f.cap_mm
        dist = 0 if lo <= cap_mm <= hi else min(abs(cap_mm - lo) / lo, abs(cap_mm - hi) / hi)
        if best is None or dist < bd:
            best, bd = f, dist
    return best


# ----------------------------------------------------------------------------- satin (port of Ink/Stitch)

def _f(params, k, default):
    try:
        v = params.get(k)
        return default if v in (None, "") else float(str(v).split()[0])
    except ValueError:
        return default


def _b(params, k, default=False):
    v = params.get(k)
    return default if v is None else str(v).lower() == "true"


def _cover(poly, edge_pts):
    """Fraction of a column's end edge (sample points) that lies inside another stroke."""
    return sum(poly.contains(q) for q in edge_pts) / max(1, len(edge_pts))


def _tucked(poly, edge_pts, frac=0.7):
    """Is a column's end edge buried inside another stroke?"""
    return _cover(poly, edge_pts) >= frac


def _rails_rungs(sub, params):
    paths = [p for p, _ in sub if len(p) > 1]
    nodes = [n for p, n in sub if len(p) > 1]
    lines = [LineString(p) for p in paths]
    n = len(lines)
    if n < 2:
        return None, None
    if n == 2:
        rail_idx = [0, 1]
    else:
        counts = [sum(lines[i].intersects(lines[j]) for j in range(n) if i != j) for i in range(n)]
        if n == 3:
            possible = [i for i in range(n) if counts[i] == 1 and lines[i].length > 0.03]
        else:
            possible = [i for i in range(n) if counts[i] > 2 and lines[i].length > 0.03]
        rail_idx = possible if len(possible) == 2 else sorted(range(n), key=lambda i: -lines[i].length)[:2]
    rails = [paths[i] for i in rail_idx]
    rail_nodes = [nodes[i] for i in rail_idx]
    if _b(params, "swap_satin_rails"):
        rails, rail_nodes = rails[::-1], rail_nodes[::-1]
    # rail direction (Ink/Stitch "automatic")
    rev = params.get("reverse_rails", "automatic")
    r0, r1 = LineString(rails[0]), LineString(rails[1])
    flip = [False, False]
    if rev == "automatic":
        a = sum(r0.interpolate(i / 10, normalized=True).distance(r1.interpolate(i / 10, normalized=True)) for i in range(10))
        b = sum(r0.interpolate(i / 10, normalized=True).distance(r1.interpolate(1 - i / 10, normalized=True)) for i in range(10))
        if a > b:
            flip[1] = True
    elif rev in ("first", "both"):
        flip[0] = True
    if rev in ("second", "both"):
        flip[1] = True
    for i in (0, 1):
        if flip[i]:
            rails[i], rail_nodes[i] = rails[i][::-1], rail_nodes[i][::-1]
    if n == 2:  # old-style: the bezier nodes of the rails, paired up, act as rungs
        pts = []
        eq = len(rail_nodes[0]) == len(rail_nodes[1])
        for i in (0, 1):
            nd = rail_nodes[i]
            if len(nd) > 2 or not eq:
                pts.append(list(nd[1:-1]))
            else:
                pts.append([np.array(LineString(nd).interpolate(0.2).coords[0])])
        rungs = []
        for s, e in zip(*pts):
            s, e = np.asarray(s), np.asarray(e)
            c = (s + e) / 2
            rungs.append(np.array([c + (s - c) * 1.1, c + (e - c) * 1.1]))
    else:
        rungs = [paths[i] for i in range(n) if i not in rail_idx]
    return rails, rungs


def _square_ends(rails, rungs, min_skew=0.3):
    """Slanted end cuts (accents, V/W/K diagonals): add rungs so the body of the column is
    stitched straight across and only the tip fans out - the way pros digitize a bevelled end.
    Without them, equal-fraction pairing lays every stitch parallel to the cut, i.e. obliquely."""
    l0, l1 = LineString(rails[0]), LineString(rails[1])
    if l0.length < 1e-6 or l1.length < 1e-6:
        return rungs
    width = l0.interpolate(0.5, normalized=True).distance(l1)
    cuts0 = [l0.project(nearest_points(LineString(r), l0)[1]) for r in rungs]
    cuts1 = [l1.project(nearest_points(LineString(r), l1)[1]) for r in rungs]
    add = []
    for end in (0, 1):
        a = np.asarray(rails[0][0 if end == 0 else -1], float)
        b = np.asarray(rails[1][0 if end == 0 else -1], float)
        for src, dst, own, other in ((a, l1, cuts0, cuts1), (b, l0, cuts1, cuts0)):
            d = dst.project(ShPoint(src))
            reach = d if end == 0 else dst.length - d   # how far the other rail runs past this corner
            if reach < max(min_skew, 0.35 * width) or reach > 0.45 * dst.length:
                continue
            # don't cross the digitizer's own rungs
            if any((c < d) if end == 0 else (c > d) for c in other if 1e-6 < c < dst.length - 1e-6):
                continue
            q = np.asarray(dst.interpolate(d).coords[0])
            src_line = l0 if dst is l1 else l1
            ds = 1e-3 if end == 0 else src_line.length - 1e-3
            p0 = np.asarray(src_line.interpolate(ds).coords[0])
            add.append(np.array([p0 + (p0 - q) * 0.05, q + (q - p0) * 0.05]))
            break
    return list(rungs) + add


def _sections(rails, rungs):
    lines = [LineString(r) for r in rails]
    cuts = [[], []]
    for rg in rungs:
        rl = LineString(rg)
        inter = rl.intersection(MultiLineString(lines))
        if inter.geom_type == "MultiPoint" and len(inter.geoms) > 2:
            continue
        for i, ln in enumerate(lines):
            _, on_rail = nearest_points(rl, ln)
            cuts[i].append(ln.project(on_rail))
    secs = []
    for i, ln in enumerate(lines):
        ds = sorted(set([0.0] + [c for c in cuts[i] if 0 < c < ln.length] + [ln.length]))
        pieces = []
        for a, b in zip(ds, ds[1:]):
            if b - a < 1e-6:
                pieces.append(None)
                continue
            pts = [ln.interpolate(a).coords[0]]
            coords = np.asarray(ln.coords)
            dist = np.r_[0, np.cumsum(np.hypot(*np.diff(coords, axis=0).T))]
            pts += [tuple(c) for c, dd in zip(coords, dist) if a < dd < b]
            pts.append(ln.interpolate(b).coords[0])
            pieces.append(np.array(pts))
        secs.append(pieces)
    m = min(len(secs[0]), len(secs[1]))
    return [(secs[0][k], secs[1][k]) for k in range(m) if secs[0][k] is not None and secs[1][k] is not None]


class _Poly:
    """Fast polyline with normalized interpolation (replaces shapely in the hot loop)."""
    __slots__ = ("c", "cum", "length")

    def __init__(self, coords):
        self.c = np.asarray(coords, float)
        seg = np.hypot(*np.diff(self.c, axis=0).T) if len(self.c) > 1 else np.zeros(0)
        self.cum = np.r_[0.0, np.cumsum(seg)]
        self.length = float(self.cum[-1])

    def at(self, t):
        d = min(max(t, 0.0), 1.0) * self.length
        i = int(np.searchsorted(self.cum, d, side="right") - 1)
        i = min(max(i, 0), len(self.c) - 2)
        span = self.cum[i + 1] - self.cum[i]
        u = 0.0 if span <= 0 else (d - self.cum[i]) / span
        return self.c[i] + (self.c[i + 1] - self.c[i]) * u


def _seg_dist(p, a, b):
    ab = b - a
    L2 = float(ab @ ab)
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, float((p - a) @ ab) / L2))
    return math.hypot(*(a + ab * t - p))


def _stitch_distance(p0, p1, q0, q1):
    prev = q1 - q0
    L = math.hypot(*prev)
    if L < 1e-4:
        return _seg_dist(q0, p0, p1)
    nrm = np.array([-prev[1], prev[0]]) / L
    return max(abs(np.dot(p0 - q0, nrm)), abs(np.dot(p1 - q1, nrm)))


def plot_points_on_rails(sections, spacing, offset=(0.0, 0.0)):
    """Ink/Stitch's rail walker: equal counts of points on both rails, spaced by the longer
    (outer) rail and corrected so the gap measured across the previous stitch is `spacing`."""
    pairs = []

    def emit(p0, p1):
        d = p1 - p0
        L = math.hypot(*d) or 1e-9
        u = d / L
        pairs.append((p0 - u * offset[0], p1 + u * offset[1]))

    old0 = old1 = None
    s0 = s1 = None
    for i, (s0, s1) in enumerate(sections):
        l0, l1 = _Poly(s0), _Poly(s1)
        if i == 0:
            old0, old1 = np.asarray(s0[0], float), np.asarray(s1[0], float)
            emit(old0, old1)
        n_pts = max(l0.length, l1.length, 0.01) / spacing
        sec_sp = 1.0 / n_pts
        dist = _stitch_distance(np.asarray(s0[0], float), np.asarray(s1[0], float), old0, old1)
        to_travel = (1 - min(dist / spacing, 1.0)) * sec_sp
        cursor, it = 0.0, 0
        while cursor + to_travel <= 1:
            it += 1
            p0 = l0.at(cursor + to_travel)
            p1 = l1.at(cursor + to_travel)
            if it <= 2:
                dd = _stitch_distance(p0, p1, old0, old1)
                if dd > 1e-3 and abs((spacing - dd) / spacing) > 0.05:
                    to_travel = (spacing / dd) * to_travel
                    if it == 1:
                        to_travel = min(to_travel, 1 - cursor)
                    continue
            cursor += to_travel
            to_travel = sec_sp
            old0, old1 = p0, p1
            emit(p0, p1)
            it = 0
    if pairs and s0 is not None:
        e0, e1 = np.asarray(s0[-1]), np.asarray(s1[-1])
        if _stitch_distance(e0, e1, old0, old1) > 0.1:
            emit(e0, e1)
    return pairs


def _short_stitches(pairs, inset=0.15, distance=0.25):
    out, last = [], [None, None]
    idx = [0, 0]
    for a, b in pairs:
        res = []
        for side, (p, q) in enumerate(((a, b), (b, a))):
            L = math.hypot(*(q - p))
            if last[side] is None or math.hypot(*(p - last[side])) >= distance:
                last[side] = p
                idx[side] = 0
                res.append(p)
            else:
                idx[side] += 1
                res.append(p + (q - p) / (L or 1) * (inset * L) if idx[side] % 2 == 1 else p)
        out.append((res[0], res[1]))
    return out


def _even_run(points, length):
    return [tuple(p) for p in sg.run_points(np.asarray(points), length)]


def satin_stitches(sub, params, P, density=1.0, ext=None):
    """One satin column -> needle points (mm), underlay first, like Ink/Stitch.
    ext: (start, end) mm to lengthen the column along its axis (corner squaring)."""
    rails, rungs = _rails_rungs(sub, params)
    if rails is None:
        return []
    if ext and any(ext):
        rails = [np.array(r, float) for r in rails]
        for side, i in ((0, 0), (1, -1)):
            if not ext[side]:
                continue
            a, b = rails[0][i], rails[1][i]
            j = 1 if i == 0 else -2
            inner = (rails[0][j] + rails[1][j]) / 2
            out = (a + b) / 2 - inner
            L = math.hypot(*out)
            if L > 1e-9:
                shift = out / L * ext[side]
                rails[0][i] = a + shift
                rails[1][i] = b + shift
    secs = _sections(rails, _square_ends(rails, rungs))
    if not secs:
        return []
    spacing = _f(params, "zigzag_spacing_mm", 0.4) * density
    pc = _f(params, "pull_compensation_mm", 0.0) + P.font_pull_extra
    pcp = _f(params, "pull_compensation_percent", 0.0) / 100.0
    pairs = plot_points_on_rails(secs, spacing, (pc, pc))
    if pcp:
        pairs = [(a - (b - a) * pcp / 2, b + (b - a) * pcp / 2) for a, b in pairs]
    if not pairs:
        return []
    if _b(params, "e_stitch"):
        top = []
        for a, b in pairs:
            top += [tuple(a), tuple(b), tuple(a)]
    else:
        sp = pairs
        short = _short_stitches(sp, _f(params, "short_stitch_inset", 15) / 100.0 if params.get("short_stitch_inset") else 0.15,
                                _f(params, "short_stitch_distance_mm", 0.25))
        top = []
        for a, b in short:
            top += [tuple(a), tuple(b)]
    max_len = _f(params, "max_stitch_length_mm", 0) or P.satin_max_width * 1.15
    top = sg._split_long(top, max_len)

    # underlay (centre walk -> contour -> zigzag), each pass ends where the next begins
    layers = []
    centre = [((a + b) / 2) for a, b in plot_points_on_rails(secs, 0.5)]
    if _b(params, "center_walk_underlay"):
        reps = int(_f(params, "center_walk_underlay_repeats", 2))
        walk = _even_run(centre, _f(params, "center_walk_underlay_stitch_length_mm", 1.5))
        for r in range(max(1, reps)):
            layers.append(walk if r % 2 == 0 else walk[::-1])
    if _b(params, "contour_underlay"):
        ins = _f(params, "contour_underlay_inset_mm", 0.4)
        cp = plot_points_on_rails(secs, 0.3, (-ins, -ins))
        side0 = _even_run([a for a, _ in cp], _f(params, "contour_underlay_stitch_length_mm", 1.5))
        side1 = _even_run([b for _, b in cp], _f(params, "contour_underlay_stitch_length_mm", 1.5))
        layers.append(side0 + side1[::-1])
    if _b(params, "zigzag_underlay"):
        ins = _f(params, "zigzag_underlay_inset_mm", _f(params, "contour_underlay_inset_mm", 0.4) / 2)
        zp = plot_points_on_rails(secs, _f(params, "zigzag_underlay_spacing_mm", 3.0) / 2, (-ins, -ins))
        fwd = [tuple(p[i % 2]) for i, p in enumerate(zp)]
        back = [tuple(p[(i + 1) % 2]) for i, p in enumerate(zp)][::-1]
        mz = _f(params, "zigzag_underlay_max_stitch_length_mm", 0)
        if mz:
            fwd, back = sg._split_long(fwd, mz), sg._split_long(back, mz)
        layers.append(fwd + back)
    # small lettering the digitizer left bare still gets a centre walk on soft fabric
    if not layers and P.font_min_underlay:
        walk = _even_run(centre, 1.5)
        layers += [walk, walk[::-1]]
    seq = []
    for lay in layers + [top]:
        if not lay:
            continue
        if seq:
            # orient each layer to start near where the previous one ended
            if math.hypot(lay[-1][0] - seq[-1][0], lay[-1][1] - seq[-1][1]) < math.hypot(lay[0][0] - seq[-1][0], lay[0][1] - seq[-1][1]):
                lay = lay[::-1]
        seq += [tuple(map(float, p)) for p in lay]
    return seq


def run_stitches(sub, params):
    L = _f(params, "running_stitch_length_mm", 2.5)
    reps = int(_f(params, "repeats", 1))
    bean = int(_f(params, "bean_stitch_repeats", 0))
    out = []
    for p, _ in sub:
        pts = _even_run(p, L)
        if bean:
            bb = [pts[0]]
            for q0, q1 in zip(pts, pts[1:]):
                bb += [q1, q0, q1] * bean + [q1]
            pts = bb
        seq = []
        for r in range(max(1, reps)):
            seq += pts if r % 2 == 0 else pts[::-1]
        out.append(seq)
    return out


def fill_stitches(sub, params, P):
    rings = [p for p, _ in sub if len(p) >= 3]
    if not rings:
        return []
    polys = sorted([Polygon(r).buffer(0) for r in rings], key=lambda g: -g.area)
    shape = polys[0]
    for h in polys[1:]:
        shape = shape.symmetric_difference(h)
    out = []
    for part in (shape.geoms if shape.geom_type == "MultiPolygon" else [shape]):
        angle = _f(params, "angle", P.fill_angle)
        out += sg.fill_shape(part, P, angle=angle)
    return out


# ----------------------------------------------------------------------------- text layout

def _warp_arc(pts, width, arc_deg):
    """Bend points laid out on a straight baseline (y down, baseline y=0, x in [0,width])
    around a circle. Positive arc arches upward."""
    if not arc_deg or width <= 0:
        return pts
    th = math.radians(abs(arc_deg))
    R = width / th
    out = np.empty_like(pts)
    phi = (pts[:, 0] - width / 2) / R
    if arc_deg > 0:  # centre below the text
        r = R - pts[:, 1]
        out[:, 0] = width / 2 + r * np.sin(phi)
        out[:, 1] = R - r * np.cos(phi)
    else:  # centre above
        r = R + pts[:, 1]
        out[:, 0] = width / 2 + r * np.sin(phi)
        out[:, 1] = -R + r * np.cos(phi)
    return out


_glyph_cache = {}


def _stitch_element(e, sub, P, density, scale=1.0):
    """One glyph element -> [(kind, needle points)]."""
    if e["kind"] == "satin":
        ext = e.get("ext")
        o = satin_stitches(sub, e["params"], P, density, (ext[0] * scale, ext[1] * scale) if ext else None)
        return [("satin", o)] if o else []
    if e["kind"] == "fill":
        return [("fill", o) for o in fill_stitches(sub, e["params"], P) if o]
    return [("run", o) for o in run_stitches(sub, e["params"]) if len(o) > 1]


def layout_text(el, family, P, density=1.0, stitch=True, with_owner=False):
    """Text element -> (objects [[(x,y) mm]], (w, h), font, warnings). Objects are centred on (0,0)."""
    text = el.get("text", "") or " "
    cap = num(el.get("height_mm"), 10)
    f = pick_variant(family, cap)
    s = cap / f.cap_mm if f.cap_mm > 0 else 1
    ls = num(el.get("letter_spacing"), 0) * cap
    lines = text.split("\n")
    line_h = (f.meta.get("leading", 100) or 100) * f.mm * s
    placed = []  # (line_idx, element dict with scaled/translated subpaths)
    widths, missing = [], set()
    base_idx = 0
    for li, line in enumerate(lines):
        x, last = 0.0, None
        items = []
        for ci, ch in enumerate(line, base_idx):
            if ch == " ":
                x += min(f.word_space() * s, 0.36 * cap) + ls  # some fonts ship a full-em space
                last = None
                continue
            g = f.glyph_for(ch)
            if g is None:
                missing.add(ch)
                continue
            m = f.metrics[g]
            if last is not None:
                x -= f.kerning(last, g) * s
            x += ls if last is not None else 0
            for e in f.ordered(g):
                items.append((e, x + m["min_x"] * s, ci))  # glyph ink starts at its left bearing
            x += f.advance(g) * s
            last = g
        widths.append(x)
        placed.append(items)
        base_idx += len(line) + 1
    W = max(widths) if widths else 0
    align = el.get("align", "center")
    objs, shapes_only = [], []
    arc = num(el.get("arc"), 0)
    pkey = (P.font_pull_extra, P.font_min_underlay, P.satin_max_width, P.fill_angle, P.fill_spacing, round(density, 3)) if P else None
    for li, items in enumerate(placed):
        off = {"left": 0, "right": W - widths[li]}.get(align, (W - widths[li]) / 2)
        y0 = li * line_h
        for e, x, ci in items:
            dx, dy = x + off, y0
            if not stitch:
                sub = [((p * s) + [dx, dy], (n * s) + [dx, dy]) for p, n in e["sub"]]
                if arc:
                    sub = [(_warp_arc(p, W, arc), _warp_arc(n, W, arc)) for p, n in sub]
                shapes_only.append((e["kind"], sub))
                continue
            if arc:
                sub = [(_warp_arc((p * s) + [dx, dy], W, arc), _warp_arc((n * s) + [dx, dy], W, arc)) for p, n in e["sub"]]
                pieces = _stitch_element(e, sub, P, density, s)
            else:
                # stitches are translation-invariant: compute once per glyph element & size, then move
                key = (f.dir, id(e), round(s, 4), pkey)
                if key not in _glyph_cache:
                    if len(_glyph_cache) > 4000:
                        _glyph_cache.clear()
                    sub0 = [(p * s, n * s) for p, n in e["sub"]]
                    _glyph_cache[key] = (_stitch_element(e, sub0, P, density, s), sub0)
                pieces0, sub0 = _glyph_cache[key]
                pieces = [(k, [(px + dx, py + dy) for px, py in o]) for k, o in pieces0]
                sub = [(p + [dx, dy], n + [dx, dy]) for p, n in sub0]
            for k, o in pieces:
                objs.append((k, o, sub, ci))
    if not stitch:
        return shapes_only, f
    # join consecutive pieces the digitizer meant to connect (they share an end point)
    merged, kinds, owners = [], [], []
    for k, o, _, ci in objs:
        if merged and owners[-1] == ci and math.hypot(o[0][0] - merged[-1][-1][0], o[0][1] - merged[-1][-1][1]) < 0.6:
            merged[-1].extend(o[1:])
            if k != "run":
                kinds[-1] = k  # underlay walk + its satin sew as one piece; label it by the top stitch
        else:
            merged.append(list(o))
            kinds.append(k)
            owners.append(ci)
    if not merged:
        return ([], [], (0, 0), f, missing, [], []) if with_owner else ([], [], (0, 0), f, missing, [])
    allp = np.array([p for o in merged for p in o])
    cx, cy = (allp[:, 0].min() + allp[:, 0].max()) / 2, (allp[:, 1].min() + allp[:, 1].max()) / 2
    merged = [[(px - cx, py - cy) for px, py in o] for o in merged]
    size = (float(allp[:, 0].max() - allp[:, 0].min()), float(allp[:, 1].max() - allp[:, 1].min()))
    shapes = [(k, [(p - [cx, cy], n - [cx, cy]) for p, n in sub]) for k, _, sub, _ in objs]
    if with_owner:
        return merged, kinds, size, f, missing, shapes, owners
    return merged, kinds, size, f, missing, shapes


# closest hand-digitized family for a TrueType look, best first; the first whose digitized size
# range covers the requested letter height wins (small text lands on fonts made for small text)
_STAND_INS = {
    "heavy":   ["✦ Barstitch Bold", "✦ Geneva Simple Sans", "✦ Ink/Stitch Medium Font", "✦ Ink/Stitch Small Font"],
    "block":   ["✦ Geneva Simple Sans", "✦ Barstitch regular", "✦ Ink/Stitch Medium Font", "✦ Ink/Stitch Small Font"],
    "rounded": ["✦ Geneva Simple Sans Rounded", "✦ TT Masters", "✦ Ink/Stitch Small Font"],
    "serif":   ["✦ Apex Simple", "✦ Venezia", "✦ Chicken Little"],
    "script":  ["✦ Magnolia", "✦ Allegria 20", "✦ Aventurina", "✦ MAM Script", "✦ Pacificlo"],
    "hand":    ["✦ Chicken Scratch", "✦ Digory Doodles Bean", "✦ Pacificlo"],
    "display": ["✦ TT Directors", "✦ Roaring Twenties", "✦ Caesarus SC", "✦ Ink/Stitch Small Font"],
}
_HEAVY = ("black", "heavy", "bold", "anton", "bebas", "alfa slab", "impact")


def stand_in(ttf_name, category, cap_mm):
    """Hand-digitized font to use instead of a TrueType font (auto-tracing a TTF never stitches
    as cleanly). Returns a '✦ ...' name, or None to keep the TrueType font."""
    n = (ttf_name or "").lower()
    cat = (category or "Block").lower()
    style = {"script": "script", "handwritten": "hand", "serif": "serif", "rounded": "rounded",
             "display": "display", "varsity": "display", "western": "display", "blackletter": "display"}.get(cat)
    if style is None:
        style = "heavy" if any(k in n for k in _HEAVY) else "block"
    best, bd = None, None
    for name in _STAND_INS[style]:
        fam = family(name)
        if not fam:
            continue
        lo, hi = size_range(fam)
        if lo * 0.95 <= cap_mm <= hi * 1.05:
            return name
        d = min(abs(cap_mm - lo) / lo, abs(cap_mm - hi) / hi)
        if bd is None or d < bd:
            best, bd = name, d
    return best if bd is not None and bd < 0.08 else None


def text_width(el):
    """Width in mm of a text element set in an embroidery font (no stitching)."""
    fam = family(el.get("font"))
    if not fam:
        return 0.0
    shapes, _ = layout_text(el, fam, None, stitch=False)
    xs = [q[0] for _, sub in shapes for p, _n in sub for q in p]
    return (max(xs) - min(xs)) if xs else 0.0


_fam_by_name = None


def family(name):
    """Catalogue entry for a font name as shown in the picker ('✦ ...'), or None."""
    global _fam_by_name
    if not name or not name.startswith("✦"):
        return None
    if _fam_by_name is None:
        _fam_by_name = {f["name"]: f for f in catalogue()}
    return _fam_by_name.get(name)


def preview_png(fam, text, height_px=34):
    """Picker preview: the glyph outlines filled (fast - no stitching)."""
    import io
    from PIL import Image
    shapes, f = layout_text({"text": text, "height_mm": 10}, fam, None, stitch=False)
    m, _ = outline_mask(shapes, 0.3, height_px / 13.0)
    ys, xs = np.nonzero(m)
    if len(xs):
        m = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    rgba = np.zeros(m.shape + (4,), np.uint8)
    rgba[m] = (58, 44, 34, 255)
    im = Image.fromarray(rgba, "RGBA")
    im = im.crop((0, 0, im.width, im.height)).resize((max(1, im.width), im.height))
    out = Image.new("RGBA", (im.width + 8, 46), (0, 0, 0, 0))
    out.paste(im, (4, max(0, 40 - im.height)), im)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def outline_mask(shapes, pad_mm, res):
    """Raster mask of the lettering (for satin borders around embroidery-font text)."""
    import cv2
    pts = np.array([q for _, sub in shapes for p, _ in sub for q in p]) if shapes else np.zeros((1, 2))
    x0, y0 = pts.min(0) - pad_mm
    x1, y1 = pts.max(0) + pad_mm
    W, H = int(math.ceil((x1 - x0) * res)) + 1, int(math.ceil((y1 - y0) * res)) + 1
    m = np.zeros((H, W), np.uint8)
    for kind, sub in shapes:
        if kind == "satin":
            rails, rungs = _rails_rungs([(p, n) for p, n in sub], {})
            if rails is None:
                continue
            poly = np.vstack([rails[0], rails[1][::-1]])
            cv2.fillPoly(m, [np.round((poly - [x0, y0]) * res).astype(np.int32)], 1)
        elif kind == "fill":
            for p, _ in sub:
                cv2.fillPoly(m, [np.round((p - [x0, y0]) * res).astype(np.int32)], 1)
        else:
            for p, _ in sub:
                cv2.polylines(m, [np.round((p - [x0, y0]) * res).astype(np.int32)], False, 1, max(1, int(0.8 * res)))
    # hairline gaps between strokes close up when sewn; don't let the border dive into them
    r = max(1, int(round(0.4 * res)))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    return m.astype(bool), (x0, y0)
