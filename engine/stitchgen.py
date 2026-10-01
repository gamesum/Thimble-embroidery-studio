"""Turn a single-colour shape mask into stitches.

Satin: the shape's skeleton becomes a graph of strokes; each stroke gets a satin column whose
rails are found by casting rays perpendicular to the stroke, so stitches always run across the
stroke (the look of commercial lettering). Strokes are sewn "out with underlay, back with satin"
in a depth-first walk, so a connected letter needs no jumps and travel is hidden.

Fill: tatami rows at an angle with staggered needle points, edge-run and cross-hatched underlay,
boustrophedon routing and travel along the outline.

All public functions take/return millimetres; masks are RES px/mm with (0,0) at the top-left.
"""
import math
import numpy as np
import cv2
from scipy import ndimage
from shapely.geometry import Polygon, LineString, Point, MultiLineString
from shapely import affinity
from skimage.morphology import skeletonize

from .params import RES

try:
    from skan import Skeleton
except Exception:  # pragma: no cover
    Skeleton = None


# ----------------------------------------------------------------------------- helpers

def to_mm(xy_px):
    return (np.asarray(xy_px, float) + 0.5) / RES


def resample(pts, step):
    """Points every `step` along a polyline (keeps both ends)."""
    pts = np.asarray(pts, float)
    if len(pts) < 2:
        return pts
    seg = np.hypot(*np.diff(pts, axis=0).T)
    s = np.concatenate([[0], np.cumsum(seg)])
    if s[-1] == 0:
        return pts[:1]
    n = max(1, int(math.ceil(s[-1] / step)))
    t = np.linspace(0, s[-1], n + 1)
    return np.column_stack([np.interp(t, s, pts[:, 0]), np.interp(t, s, pts[:, 1])])


def run_points(pts, length, keep_corners=True):
    """Running stitch along a polyline; corners sharper than ~30 degrees keep a needle point."""
    pts = np.asarray(pts, float)
    if len(pts) < 2:
        return [tuple(p) for p in pts]
    if not keep_corners:
        return [tuple(p) for p in resample(pts, length)]
    # split at corners
    out, start = [], 0
    d = np.diff(pts, axis=0)
    ang = np.arctan2(d[:, 1], d[:, 0])
    for i in range(1, len(pts) - 1):
        turn = abs((ang[i] - ang[i - 1] + math.pi) % (2 * math.pi) - math.pi)
        if turn > math.radians(30):
            piece = resample(pts[start:i + 1], length)
            out.extend(map(tuple, piece[:-1]))
            start = i
    out.extend(map(tuple, resample(pts[start:], length)))
    return out


def clean(points, min_len):
    """Drop needle points closer than min_len to the previous one (keeps the last point)."""
    if not points:
        return points
    out = [points[0]]
    for p in points[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_len:
            out.append(p)
    if out[-1] != points[-1] and len(points) > 1:
        out[-1] = points[-1]
    return out


# ----------------------------------------------------------------------------- polygons

def mask_to_polygons(mask, smooth_px=1.0, crisp=False):
    """Mask -> list of shapely Polygons in mm (holes kept, pixel stairs smoothed).
    crisp=True keeps the geometry of logos: straight edges stay dead straight and corners stay
    sharp (the pixel stairs are fitted with line segments instead of blurred)."""
    m = mask.astype(np.uint8)
    contours, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hier is None:
        return []
    hier = hier[0]

    def ring(c):
        if crisp and len(c) >= 8:
            c = cv2.approxPolyDP(c, 0.9, True)  # 0.09 mm tolerance: stairs -> straight lines, curves stay smooth
            return to_mm(c[:, 0, :].astype(float))
        c = c[:, 0, :].astype(float)
        if len(c) >= 5 and smooth_px > 0:
            c = np.column_stack([ndimage.gaussian_filter1d(c[:, 0], smooth_px, mode="wrap"),
                                 ndimage.gaussian_filter1d(c[:, 1], smooth_px, mode="wrap")])
        return to_mm(c)

    polys = []
    for i, h in enumerate(hier):
        if h[3] != -1 or len(contours[i]) < 3:
            continue
        holes, ch = [], h[2]
        while ch != -1:
            if len(contours[ch]) >= 3:
                holes.append(ring(contours[ch]))
            ch = hier[ch][0]
        p = Polygon(ring(contours[i]), holes).buffer(0)
        if not p.is_empty:
            polys.extend(p.geoms if p.geom_type == "MultiPolygon" else [p])
    return [p.simplify(0.03) for p in polys if p.area > 0.05]


def travel(poly, a, b, length):
    """Hidden-ish travel from a to b inside poly: straight if possible, else along the nearest
    outline ring. Returns (points, ok) where ok=False means a jump is needed."""
    a, b = tuple(a), tuple(b)
    if math.hypot(a[0] - b[0], a[1] - b[1]) < 0.05:
        return [b], True
    line = LineString([a, b])
    if poly.buffer(0.15).contains(line):
        return run_points([a, b], length, keep_corners=False)[1:], True
    rings = [poly.exterior] + list(poly.interiors)
    pa, pb = Point(a), Point(b)
    best = None
    for r in rings:
        if r.distance(pa) < 0.8 and r.distance(pb) < 0.8:
            L = r.length
            da, db = r.project(pa), r.project(pb)
            fwd = (db - da) % L
            if fwd <= L / 2:
                ts = np.linspace(da, da + fwd, max(2, int(fwd / 0.5) + 1))
            else:
                ts = np.linspace(da, da - (L - fwd), max(2, int((L - fwd) / 0.5) + 1))
            pts = [r.interpolate(t % L).coords[0] for t in ts]
            cand = [a] + pts + [b]
            if best is None or len(cand) < len(best):
                best = cand
    if best:
        return run_points(best, length)[1:], True
    return [b], False


# ----------------------------------------------------------------------------- tatami fill

def _rows(rpoly, spacing, y_phase=0.0):
    minx, miny, maxx, maxy = rpoly.bounds
    rows = []
    y = miny + spacing / 2 + y_phase
    k = 0
    while y < maxy:
        inter = rpoly.intersection(LineString([(minx - 1, y), (maxx + 1, y)]))
        segs = []
        geoms = getattr(inter, "geoms", [inter])
        for g in geoms:
            if g.geom_type == "LineString" and g.length > 0.05:
                xs = [c[0] for c in g.coords]
                segs.append([min(xs), max(xs)])
        segs.sort()
        rows.append((k, y, segs))
        y += spacing
        k += 1
    return rows


def _row_points(x0, x1, y, k, length, staggers, min_len):
    """Needle points along one row, on a staggered global grid (brick pattern, no visible lines)."""
    sgn = 1 if x1 >= x0 else -1
    off = (k % staggers) / staggers * length
    lo, hi = min(x0, x1), max(x0, x1)
    m0 = math.ceil((lo - off) / length)
    grid = [off + m * length for m in range(m0, int(math.floor((hi - off) / length)) + 1)]
    grid = [g for g in grid if g - lo > min_len and hi - g > min_len]
    if sgn < 0:
        grid.reverse()
    return [(x0, y)] + [(g, y) for g in grid] + [(x1, y)]


def tatami(poly, angle, spacing, length, staggers, min_len, travel_len, start=None):
    """Fill a polygon. Returns list of continuous point lists (a new list means jump)."""
    if poly.is_empty or poly.area < 0.1:
        return []
    rpoly = affinity.rotate(poly, -angle, origin=(0, 0))
    rows = _rows(rpoly, spacing)
    todo = {(k, i) for k, _, segs in rows for i in range(len(segs))}
    if not todo:
        return []
    rowmap = {k: (y, segs) for k, y, segs in rows}

    def rot(p):  # rotated frame -> design frame
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)

    def unrot(p):
        c, s = math.cos(math.radians(-angle)), math.sin(math.radians(-angle))
        return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)

    cur = unrot(start) if start is not None else None
    objects, pts = [], []
    while todo:
        # nearest unsewn segment end
        best = None
        for k, i in todo:
            y, segs = rowmap[k]
            for end in (0, 1):
                x = segs[i][end]
                d = 0 if cur is None else (x - cur[0]) ** 2 + (y - cur[1]) ** 2
                if best is None or d < best[0]:
                    best = (d, k, i, end)
        _, k, i, end = best
        # travel there
        y, segs = rowmap[k]
        target = (segs[i][end], y)
        if cur is not None and pts:
            tpts, ok = travel(rpoly, cur, target, travel_len)
            if ok:
                pts.extend(tpts)
            else:
                objects.append(pts)
                pts = [target]
        else:
            pts.append(target)
        # boustrophedon run through consecutive rows
        direction = None
        while True:
            todo.discard((k, i))
            y, segs = rowmap[k]
            x0, x1 = segs[i] if end == 0 else segs[i][::-1]
            pts.extend(_row_points(x0, x1, y, k, length, staggers, min_len)[1:] if pts and pts[-1] == (x0, y)
                       else _row_points(x0, x1, y, k, length, staggers, min_len))
            cur = (x1, y)
            nxt = None
            for dk in ([direction] if direction else [1, -1]):
                if k + dk not in rowmap:
                    continue
                ny, nsegs = rowmap[k + dk]
                cands = [j for j, s in enumerate(nsegs) if (k + dk, j) in todo
                         and s[0] <= max(x0, x1) + spacing * 2 and s[1] >= min(x0, x1) - spacing * 2]
                if cands:
                    j = min(cands, key=lambda j: min(abs(nsegs[j][0] - x1), abs(nsegs[j][1] - x1)))
                    s = nsegs[j]
                    e = 0 if abs(s[0] - x1) <= abs(s[1] - x1) else 1
                    # only step if the connecting move stays short (else it's a separate section)
                    if abs(s[e] - x1) <= max(length, spacing * 6):
                        nxt = (dk, j, e)
                        break
            if not nxt:
                break
            direction, i, end = nxt[0], nxt[1], nxt[2]
            k += direction
    if pts:
        objects.append(pts)
    return [[rot(p) for p in clean(o, min_len)] for o in objects if o]


def edge_runs(poly, inset, length, start):
    """Running stitch around the (inset) outline(s); returns list of point lists."""
    ip = poly.buffer(-inset) if inset else poly
    if ip.is_empty:
        return []
    parts = list(ip.geoms) if ip.geom_type == "MultiPolygon" else [ip]
    out = []
    for part in parts:
        for ring in [part.exterior] + list(part.interiors):
            c = np.asarray(ring.coords)[:-1]
            if len(c) < 3:
                continue
            j = int(np.argmin(np.hypot(c[:, 0] - start[0], c[:, 1] - start[1]))) if start is not None else 0
            c = np.vstack([c[j:], c[:j + 1]])
            out.append(run_points(c, length))
    return out


def fill_shape(poly, P, angle=None, start=None):
    """Complete professional fill: edge run + underlay + staggered tatami. -> list of point lists."""
    angle = P.fill_angle if angle is None else angle
    poly = poly.buffer(P.fill_pull_comp, join_style=2)
    start = start if start is not None else tuple(poly.exterior.coords[0])
    pieces = []
    if poly.area > 12:  # underlay only where it matters (> ~3.5 x 3.5 mm)
        pieces += edge_runs(poly, P.edge_run_inset, P.run_length, start)
        up = poly.buffer(-P.fill_underlay_inset)
        if not up.is_empty:
            for part in (up.geoms if up.geom_type == "MultiPolygon" else [up]):
                pieces += tatami(part, angle + 90, P.fill_underlay_spacing, 3.0, 1, P.min_stitch,
                                 P.travel_length, pieces[-1][-1] if pieces else start)
    top = tatami(poly, angle, P.fill_spacing, P.fill_length, P.fill_staggers, P.min_stitch,
                 P.travel_length, pieces[-1][-1] if pieces else start)
    return _chain(pieces + top, poly, P.travel_length)


def contour_fill(poly, P, start=None):
    """Contour fill: rows that follow the shape's own edge, stepping inward ring by ring (the idea
    behind Ink/Stitch's contour fill). Each ring is joined to the next with a tiny step, so the whole
    shape sews as one continuous line and the texture echoes the outline instead of straight rows."""
    spacing = P.fill_spacing
    poly = poly.buffer(P.fill_pull_comp, join_style=1)
    rings = []
    g = poly
    while not g.is_empty:
        for part in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
            if part.area < spacing * spacing:
                continue
            for ring in [part.exterior] + list(part.interiors):
                c = np.asarray(ring.coords)
                if len(c) >= 4 and ring.length > 3 * spacing:
                    rings.append(c)
        g = g.buffer(-spacing, join_style=1, resolution=6)
    if not rings:
        return []
    out, cur = [], None if start is None else np.asarray(start, float)
    todo = list(range(len(rings)))
    seq = []
    L = min(P.fill_length, 2.5)
    while todo:
        if cur is None:
            k = todo[0]
        else:
            k = min(todo, key=lambda i: float(np.min(np.hypot(*(rings[i][:, :2] - cur).T))))
        todo.remove(k)
        c = rings[k][:-1, :2]
        j = 0 if cur is None else int(np.argmin(np.hypot(*(c - cur).T)))
        c = np.vstack([c[j:], c[:j], c[j:j + 1]])
        pts = run_points(c, L)
        if seq and cur is not None and np.hypot(*(np.asarray(pts[0]) - cur)) > 2.5 * spacing + 0.5:
            out.append(seq)  # a different branch: travel / trim between them
            seq = []
        seq += [tuple(p) for p in pts]
        cur = np.asarray(seq[-1], float)
    if seq:
        out.append(seq)
    pieces = []
    if poly.area > 12:  # light underlay so the rings sit on something
        up = poly.buffer(-P.fill_underlay_inset)
        if not up.is_empty:
            for part in (up.geoms if up.geom_type == "MultiPolygon" else [up]):
                pieces += tatami(part, P.fill_angle + 90, P.fill_underlay_spacing, 3.0, 1, P.min_stitch,
                                 P.travel_length, pieces[-1][-1] if pieces else start)
    return _chain(pieces + [clean(o, P.min_stitch) for o in out], poly, P.travel_length)


def _chain(pieces, poly, travel_len):
    """Join consecutive pieces with travel stitches when possible."""
    out = []
    for pc in pieces:
        if not pc:
            continue
        if out:
            tpts, ok = travel(poly, out[-1][-1], pc[0], travel_len)
            if ok:
                out[-1].extend(tpts[:-1] + list(pc))
                continue
        out.append(list(pc))
    return out


# ----------------------------------------------------------------------------- skeleton graph

class StrokeGraph:
    """Skeleton of a mask as nodes/edges (pixel coordinates, x=col, y=row)."""

    def __init__(self, mask, dt):
        self.dt = dt
        self.edges = []  # dict(a, b, pts[N,2] (x,y px))
        sk = skeletonize(mask)
        n = int(sk.sum())
        if n == 0:
            return
        if n < 3 or Skeleton is None:
            ys, xs = np.nonzero(sk)
            self.edges.append(dict(a=0, b=0, pts=np.column_stack([xs, ys]).astype(float)))
            return
        S = Skeleton(sk)
        coords = S.coordinates
        nodes = {}

        def node(pix):
            if pix not in nodes:
                nodes[pix] = pix
            return pix

        for i in range(S.n_paths):
            ids = S.path(i)
            pc = coords[ids][:, ::-1].astype(float)  # (row,col) -> (x,y)
            self.edges.append(dict(a=node(int(ids[0])), b=node(int(ids[-1])), pts=pc))
        self._merge_close_nodes(coords)
        for _ in range(3):
            if not self._prune():
                break
        self._merge_degree2()
        self.corners = set()
        self._split_corners()

    def _split_corners(self, max_turn_deg=55):
        """Split strokes at sharp turns so each satin column runs straight-ish (no fans).
        New nodes are recorded in self.corners; columns get extended through them."""
        new_edges = []
        for e in self.edges:
            pts = e["pts"]
            if len(pts) < 12 or e["a"] == e["b"] and len(pts) < 20:
                new_edges.append(e)
                continue
            h = float(np.median([self._dt_at(p) for p in pts])) or 1.0
            sig = max(1.5, 0.35 * h)
            sm = np.column_stack([ndimage.gaussian_filter1d(pts[:, 0], sig, mode="nearest"),
                                  ndimage.gaussian_filter1d(pts[:, 1], sig, mode="nearest")])
            d = np.gradient(sm, axis=0)
            ang = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
            W = max(2, int(round(0.9 * h)))
            n = len(pts)
            turn = np.zeros(n)
            wide = np.zeros(n)
            for i in range(W, n - W):
                turn[i] = abs(ang[i + W] - ang[i - W])
                a, b = max(0, i - 2 * W), min(n - 1, i + 2 * W)
                wide[i] = abs(ang[b] - ang[a])
            # a real corner turns all at once (the wide window adds little); a curve like an
            # "o" or "s" keeps turning, so its wide-window turn is ~2x the narrow one -> no split
            sharp = (turn > math.radians(max_turn_deg)) & (turn > 0.72 * wide)
            # wide strokes round their corners off over about a stroke width, so a real corner
            # (V point, top of M, G bar) shows a big turn that the wide window only partly adds to
            sharp |= (turn > math.radians(80)) & (turn > 0.58 * wide)
            cuts, i = [], int(1.2 * h) + W
            while i < n - int(1.2 * h) - W:
                if sharp[i]:
                    j = i + int(np.argmax(turn[i:min(n, i + 2 * W + 1)]))
                    cuts.append(j)
                    i = j + max(int(2 * h), 2 * W)
                else:
                    i += 1
            if not cuts:
                new_edges.append(e)
                continue
            prev_node, prev_i = e["a"], 0
            for c in cuts:
                nid = ("corner", id(e), c)
                self.corners.add(nid)
                new_edges.append(dict(a=prev_node, b=nid, pts=pts[prev_i:c + 1]))
                prev_node, prev_i = nid, c
            new_edges.append(dict(a=prev_node, b=e["b"], pts=pts[prev_i:]))
        self.edges = new_edges

    # -- graph maintenance
    corners = set()

    def degree(self):
        d = {}
        for e in self.edges:
            d[e["a"]] = d.get(e["a"], 0) + 1
            d[e["b"]] = d.get(e["b"], 0) + 1
        return d

    def _merge_close_nodes(self, coords):
        parent = {}

        def find(x):
            while parent.get(x, x) != x:
                x = parent[x]
            return x
        keep = []
        for e in self.edges:
            L = np.hypot(*np.diff(e["pts"], axis=0).T).sum() if len(e["pts"]) > 1 else 0
            if e["a"] != e["b"] and L <= 2.5:
                parent[find(e["a"])] = find(e["b"])
            else:
                keep.append(e)
        for e in keep:
            e["a"], e["b"] = find(e["a"]), find(e["b"])
        self.edges = keep

    def _length(self, e):
        p = e["pts"]
        return float(np.hypot(*np.diff(p, axis=0).T).sum()) if len(p) > 1 else 0.0

    def _dt_at(self, p):
        x, y = int(round(p[0])), int(round(p[1]))
        y = min(max(y, 0), self.dt.shape[0] - 1)
        x = min(max(x, 0), self.dt.shape[1] - 1)
        return float(self.dt[y, x])

    def _prune(self):
        deg = self.degree()
        changed = False
        keep = []
        for e in self.edges:
            leaf_a, leaf_b = deg[e["a"]] == 1, deg[e["b"]] == 1
            if (leaf_a ^ leaf_b) and len(self.edges) > 1:
                junction_end = e["pts"][-1] if leaf_a else e["pts"][0]
                h = self._dt_at(junction_end)
                L = self._length(e)
                # wide letters: a branch running into a corner tapers to nothing (a spur), while
                # half of a real stroke (the stem of a K) keeps its width
                tapers = float(np.median([self._dt_at(q) for q in e["pts"]])) < 0.6 * h
                if L < 0.95 * h + 2 or (h > 35 and L < 1.7 * h + 2 and tapers):
                    changed = True
                    continue
            keep.append(e)
        self.edges = keep
        return changed

    def _merge_degree2(self):
        while True:
            deg = self.degree()
            target = next((n for n, d in deg.items() if d == 2 and
                           sum(1 for e in self.edges if e["a"] == n and e["b"] == n) == 0), None)
            if target is None:
                return
            es = [e for e in self.edges if e["a"] == target or e["b"] == target]
            if len(es) != 2 or es[0] is es[1]:
                return
            e1, e2 = es
            p1 = e1["pts"] if e1["b"] == target else e1["pts"][::-1]
            a = e1["a"] if e1["b"] == target else e1["b"]
            p2 = e2["pts"] if e2["a"] == target else e2["pts"][::-1]
            b = e2["b"] if e2["a"] == target else e2["a"]
            self.edges = [e for e in self.edges if e is not e1 and e is not e2]
            self.edges.append(dict(a=a, b=b, pts=np.vstack([p1, p2[1:]])))


# ----------------------------------------------------------------------------- satin

def _raycast(mask, p, n, cap):
    """Distance (px) from p along unit vector n until leaving the mask (0.25 px steps)."""
    h, w = mask.shape
    t = 0.0
    while t < cap:
        x, y = p[0] + n[0] * (t + 0.25), p[1] + n[1] * (t + 0.25)
        xi, yi = int(round(x)), int(round(y))
        if xi < 0 or yi < 0 or xi >= w or yi >= h or not mask[yi, xi]:
            return t + 0.5
        t += 0.25
    return cap


def _extend_to_tip(mask, pts, dt):
    """Skeleton ends stop about one half-width short of a stroke end; extend along the tangent."""
    if len(pts) < 3:
        return pts
    at = lambda p: dt[min(max(int(round(p[1])), 0), dt.shape[0] - 1), min(max(int(round(p[0])), 0), dt.shape[1] - 1)]
    # the last ~half-width of a skeleton curls toward a corner of a square stroke end;
    # drop it and take the direction from the straight part before it
    hm = float(np.median([at(p) for p in pts[-min(len(pts), 20):]]))
    cut = int(0.9 * hm)
    if len(pts) - cut >= 4:
        pts = pts[:len(pts) - cut]
    k = min(len(pts) - 1, max(6, int(1.5 * hm)))
    d = pts[-1] - pts[-1 - k]
    nrm = np.hypot(*d)
    if nrm == 0:
        return pts
    d = d / nrm
    h = at(pts[-1])
    L = _raycast(mask, pts[-1], d, 3 * h + 3)
    ext = [pts[-1] + d * s for s in np.arange(1.0, max(L - 0.5, 1.0), 1.0)]
    return np.vstack([pts] + ([np.array(ext)] if ext else []))


def _cap_direction(mask, contour, tip, med):
    """Direction of the cut at a stroke end (PCA of the outline near the tip), or None."""
    if contour is None or len(contour) == 0:
        return None
    r = max(2.5, 0.95 * med)
    near = contour[np.hypot(contour[:, 0] - tip[0], contour[:, 1] - tip[1]) < r]
    if len(near) < 5:
        return None
    near = near - near.mean(0)
    _, sv, vt = np.linalg.svd(near, full_matrices=False)
    if sv[0] < 1e-6 or sv[1] / sv[0] > 0.6:  # not a clear straight cut (e.g. a rounded end)
        return None
    v = vt[0]
    return v / (np.hypot(*v) or 1)


def _rung_span(mask, p, n, lim_l, lim_r):
    """Along the rung through p (unit n): the inside run of the mask that contains p, or the
    nearest one if p is just outside (a column running on past a slanted end). Returns
    (dl, dr) with left rail p + n*dl and right rail p - n*dr (either may be negative), or None."""
    h, w = mask.shape
    us = np.arange(-lim_r, lim_l + 0.01, 0.5)
    xs = np.round(p[0] + n[0] * us).astype(int)
    ys = np.round(p[1] + n[1] * us).astype(int)
    ok = (xs >= 0) & (ys >= 0) & (xs < w) & (ys < h)
    inside = np.zeros(len(us), bool)
    inside[ok] = mask[ys[ok], xs[ok]]
    if not inside.any():
        return None
    edges = np.flatnonzero(np.diff(np.concatenate([[0], inside.astype(np.int8), [0]])))
    runs = list(zip(edges[::2], edges[1::2] - 1))
    lo, hi = min(runs, key=lambda r: 0 if us[r[0]] <= 0 <= us[r[1]] else min(abs(us[r[0]]), abs(us[r[1]])))
    if us[hi] - us[lo] < 2:
        return None
    return float(us[hi]) + 0.25, -float(us[lo]) + 0.25


def satin_column(mask, dt, pts_px, P, ext_start, ext_end, cap_start=False, cap_end=False, contour=None, closed=False):
    """Rails + satin/underlay needle points for one stroke (px), in the direction of pts.
    ext_*: extend the column to the outline (stroke ends and split corners).
    cap_*: that end is a real stroke end - angle the last stitches to match the cut."""
    pts = np.asarray(pts_px, float)
    straight = None
    if ext_end and not closed:
        pts = _extend_to_tip(mask, pts, dt)
    if ext_start and not closed:
        pts = _extend_to_tip(mask, pts[::-1], dt)[::-1]
    if len(pts) >= 5:
        mode = "wrap" if closed else "nearest"
        # the medial axis of a wide stroke wobbles (and bends toward joints); smooth it in
        # proportion to the stroke width so the stitches sweep evenly across
        hd = np.median([dt[min(max(int(round(y)), 0), dt.shape[0] - 1), min(max(int(round(x)), 0), dt.shape[1] - 1)] for x, y in pts])
        sig = 2.0 if hd < 30 else min(0.6 * hd, len(pts) / 6)
        if hd >= 30 and not closed:
            # a wide stroke that is basically straight (legs of A, V, W...) gets one straight
            # centre line, so every stitch runs at the same angle from end to end
            # (fitted to the middle of the stroke: the medial axis swerves into corners at the ends)
            mid = pts[len(pts) // 5: len(pts) - len(pts) // 5]
            if len(mid) >= 5:
                cen = mid.mean(0)
                _, _, vt = np.linalg.svd(mid - cen)
                ax = vt[0]
                dev = np.abs((mid - cen) @ np.array([-ax[1], ax[0]]))
                proj = (pts - cen) @ ax
                ln = float(proj.max() - proj.min())
                if ln > 2 * hd and float(np.percentile(dev, 90)) < 0.25 * hd:
                    t0, t1 = (pts[0] - cen) @ ax, (pts[-1] - cen) @ ax
                    sgn = 1.0 if t1 > t0 else -1.0
                    nrm = np.array([-ax[1], ax[0]]) * sgn  # the column's left normal
                    # each side's width from the middle, so rungs can't wander into a crossbar
                    mpts = cen + np.linspace(t0, t1, 15)[3:12, None] * ax
                    limL = 1.1 * float(np.median([_raycast(mask, q, nrm, 3 * hd) for q in mpts])) + 1.0
                    limR = 1.1 * float(np.median([_raycast(mask, q, -nrm, 3 * hd) for q in mpts])) + 1.0
                    # run the ends on while the rungs still find the letter (to the far corner
                    # of a slanted foot, not just where the centre line leaves it)
                    # (each new rung has to overlap the last one - never jump a counter to
                    # another part of the letter)
                    def run_on(t, step):
                        prev = _rung_span(mask, cen + t * ax, nrm, limL, limR)
                        while prev and abs(t + step - (t0 if step * sgn > 0 else t1)) < 20 * hd:
                            sp = _rung_span(mask, cen + (t + step) * ax, nrm, limL, limR)
                            if not sp or min(sp[0], prev[0]) + min(sp[1], prev[1]) < 0.5 * min(sp[0] + sp[1], prev[0] + prev[1]):
                                break
                            t, prev = t + step, sp
                        return t
                    if ext_end:
                        t1 = run_on(t1, sgn)
                    if ext_start:
                        t0 = run_on(t0, -sgn)
                    pts = cen + np.linspace(t0, t1, max(5, int(abs(t1 - t0)) + 1))[:, None] * ax
                    straight = (limL, limR)
        pts = np.column_stack([ndimage.gaussian_filter1d(pts[:, 0], sig, mode=mode),
                               ndimage.gaussian_filter1d(pts[:, 1], sig, mode=mode)])
    step = P.satin_spacing / 2 * RES
    # sample finely; the final spacing is chosen below from the outer rail
    c = resample(np.vstack([pts, pts[:1]]) if closed else pts, step / 3)
    if closed and len(c) > 2:
        c = c[:-1]
    if len(c) < 2:
        c = np.vstack([pts[0], pts[-1]]) if len(pts) > 1 else np.vstack([pts[0], pts[0] + 0.01])
    if closed:
        tan = (np.roll(c, -1, axis=0) - np.roll(c, 1, axis=0)) / 2
    else:
        tan = np.gradient(c, axis=0)
    tn = np.hypot(tan[:, 0], tan[:, 1])
    tn[tn == 0] = 1
    tan = tan / tn[:, None]
    nor = np.column_stack([-tan[:, 1], tan[:, 0]])
    at = lambda p: dt[min(max(int(round(p[1])), 0), dt.shape[0] - 1), min(max(int(round(p[0])), 0), dt.shape[1] - 1)]
    hdt = np.array([at(p) for p in c])
    med = float(np.median(hdt)) if len(hdt) else 1.0

    # rung directions: across the stroke, turning to follow a slanted cut near real stroke ends
    rdir = nor.copy()
    s = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(c, axis=0).T))])
    D = 1.8 * med + 2
    for flag, idx, sgn in ((cap_start, 0, -1), (cap_end, len(c) - 1, 1)):
        if not flag or closed:
            continue
        tip_dir = tan[idx] * sgn
        L = _raycast(mask, c[idx], tip_dir, 4 * med + 4)
        v = _cap_direction(mask, contour, c[idx] + tip_dir * L, med)
        if v is None or abs(np.dot(v, nor[idx])) < 0.45:
            continue
        dist = s if sgn < 0 else s[-1] - s
        for i in np.nonzero(dist < D)[0]:
            vv = v if np.dot(v, nor[i]) > 0 else -v
            t = 1 - dist[i] / D
            d = (1 - t) * nor[i] + t * vv
            rdir[i] = d / (np.hypot(*d) or 1)
    # wide strokes keep their full width right to a flat cut (local thickness drops to nothing
    # at the outline, which would taper the column to a point and leave the corners bare)
    local = np.maximum(hdt, med) if med >= 30 else hdt
    caps = np.minimum(1.45 * local + 1.0, 1.7 * med + 1.5) / np.maximum(np.abs(np.sum(rdir * nor, 1)), 0.5)
    if straight:
        # straight wide stroke: every stitch parallel, each trimmed to the letter's outline
        spans = [_rung_span(mask, c[i], nor[i], *straight) for i in range(len(c))]
        keep = np.array([sp is not None for sp in spans])
        c, nor, tan, hdt = c[keep], nor[keep], tan[keep], hdt[keep]
        rdir = nor.copy()
        dl = np.array([sp[0] for sp in spans if sp is not None])
        dr = np.array([sp[1] for sp in spans if sp is not None])
    else:
        dl = np.array([_raycast(mask, c[i], rdir[i], caps[i]) for i in range(len(c))])
        dr = np.array([_raycast(mask, c[i], -rdir[i], caps[i]) for i in range(len(c))])
    if len(c) >= 5 and not straight:
        mode = "wrap" if closed else "nearest"
        dl = ndimage.gaussian_filter1d(ndimage.median_filter(dl, 5, mode=mode), 1.0, mode=mode)
        dr = ndimage.gaussian_filter1d(ndimage.median_filter(dr, 5, mode=mode), 1.0, mode=mode)
    # near joints a ray can run down the neighbouring stroke; cap each edge at ~1.3x the
    # stroke's typical half-width (measured over the middle of the column)
    if len(c) >= 8 and not straight:
        mid = slice(len(c) // 5, len(c) - len(c) // 5)
        for side in (dl, dr):
            lim = (1.1 if med >= 30 else 1.3) * float(np.median(side[mid])) + 1.0
            np.minimum(side, lim / np.maximum(np.abs(np.sum(rdir * nor, 1)), 0.5), out=side)
    w = dl + dr
    minw = P.satin_min_width * RES
    grow = np.where(w < minw, (minw - w) / 2, 0)
    dl, dr = dl + grow + P.pull_comp * RES, dr + grow + P.pull_comp * RES
    L = c + rdir * dl[:, None]
    R = c - rdir * dr[:, None]
    for rail in (L, R):  # tight inside curves: a rail must not run backwards
        for i in range(1, len(rail)):
            if np.dot(rail[i] - rail[i - 1], tan[i]) < -0.3:
                rail[i] = rail[i - 1]
    width_mm = float(np.median(w)) / RES

    # spacing is set by the OUTSIDE of a curve: keep a sample only once either rail has moved
    # a full step, so the outer edge stays packed (no fringe) and the inner edge gets denser
    if len(c) > 4:
        keep, lastL, lastR = [0], L[0], R[0]
        for i in range(1, len(c) - 1):
            if max(np.hypot(*(L[i] - lastL)), np.hypot(*(R[i] - lastR))) >= step * 0.98:
                keep.append(i)
                lastL, lastR = L[i], R[i]
        keep.append(len(c) - 1)
        keep = np.array(sorted(set(keep)))
        c, L, R, rdir, tan = c[keep], L[keep].copy(), R[keep].copy(), rdir[keep], tan[keep]
        dl, dr = dl[keep], dr[keep]
        # short stitches: where the inside rail is crowded, every other stitch stops short
        for rail in (L, R):
            for i in range(2, len(rail)):
                if np.hypot(*(rail[i] - rail[i - 2])) < 0.5 * P.satin_spacing * RES and (i // 2) % 2 == 1:
                    rail[i] = c[i] + (rail[i] - c[i]) * 0.62
    if width_mm < P.center_walk_below:
        # small lettering: centre walk three times (out, back, out - Ink/Stitch-style repeats),
        # a firm spine that keeps the satin raised without an edge walk poking out the sides
        walk = run_points(np.vstack([c, c[:1]]) if closed else c, P.run_length * RES)
        under = walk + walk[::-1][1:] + walk[1:]
    else:
        ins = P.underlay_inset * RES
        Li = c + rdir * np.maximum(dl - P.pull_comp * RES - ins, 0.2)[:, None]
        Ri = c - rdir * np.maximum(dr - P.pull_comp * RES - ins, 0.2)[:, None]
        if straight:
            # the centre line can run just past a slanted end: inset from the stitch's own
            # middle so the underlay never pokes outside the letter
            a = (dl - dr) / 2
            half = np.maximum((dl + dr) / 2 - P.pull_comp * RES - ins, 0.2)
            Li = c + rdir * (a + half)[:, None]
            Ri = c + rdir * (a - half)[:, None]
        zz =max(1, int(round(P.zigzag_underlay_spacing * RES / 2 / step)))
        idx = list(range(0, len(c), zz))
        if idx[-1] != len(c) - 1:
            idx.append(len(c) - 1)
        zig = [tuple(Li[j]) if n % 2 == 0 else tuple(Ri[j]) for n, j in enumerate(idx)]
        if width_mm >= 5.0 and not closed:
            # wide columns get built up in layers: edge walk out, zigzag back, other edge out
            # (ends at the far end, ready for the satin to come back over the top)
            walk = P.run_length * RES
            under = run_points(Li, walk) + zig[::-1] + run_points(Ri, walk)
        else:
            under = zig
    return dict(center=c, underlay=under, width=width_mm, rails=(L, R), closed=closed)


def _split_long(points, max_len, phase=0):
    """Split satin: a stitch longer than max_len gets extra needle points at staggered
    positions so wide letters keep the satin look but lie flat (no loose floats)."""
    out = [points[0]] if points else []
    k = phase
    for p in points[1:]:
        a = out[-1]
        d = math.hypot(p[0] - a[0], p[1] - a[1])
        if d > max_len:
            n = int(math.ceil(d / max_len))
            off = (k % 3) / 3.0  # three-way stagger so the splits don't line up into a groove
            for j in range(n - 1):
                t = (j + 0.5 + off * 0.5) / n
                out.append((a[0] + (p[0] - a[0]) * t, a[1] + (p[1] - a[1]) * t))
            k += 1
        out.append(p)
    return out


def _strokes(g, dt):
    """Assemble skeleton edges into pen strokes the way a digitizer would.
    At each joint the two edges that continue most nearly straight are merged into one
    continuous stroke (it will sew on top); the other edges end there ("tuck under").
    Returns strokes [{pts, a, b, closed, tucks:set(nodes), through:set(nodes)}]."""
    edges = g.edges
    deg = g.degree()

    def node_pt(idx, end):
        return edges[idx]["pts"][0] if end == "a" else edges[idx]["pts"][-1]

    def leave_dir(idx, end):
        p = edges[idx]["pts"] if end == "a" else edges[idx]["pts"][::-1]
        h = float(dt[min(max(int(round(p[0][1])), 0), dt.shape[0] - 1), min(max(int(round(p[0][0])), 0), dt.shape[1] - 1)])
        k = min(len(p) - 1, max(3, int(round(1.5 * h + 2))))
        d = p[k] - p[0]
        return d / (np.hypot(*d) or 1)

    ends_at = {}
    for idx, e in enumerate(edges):
        ends_at.setdefault(e["a"], []).append((idx, "a"))
        ends_at.setdefault(e["b"], []).append((idx, "b"))
    link = {}
    for n, incident in ends_at.items():
        if deg[n] < 3 or n in g.corners:
            continue
        dirs = {ie: leave_dir(*ie) for ie in incident}
        pairs = sorted(((float(np.dot(dirs[x], dirs[y])), x, y) for i, x in enumerate(incident)
                        for y in incident[i + 1:] if x[0] != y[0]), key=lambda t: t[0])
        used = set()
        for dot, x, y in pairs:
            if dot > -0.6:  # must continue within ~53 degrees of straight
                break
            if x in used or y in used:
                continue
            link[x], link[y] = y, x
            used.update((x, y))

    other = lambda end: "b" if end == "a" else "a"
    done, strokes = set(), []
    for e0 in range(len(edges)):
        if e0 in done:
            continue
        # walk backwards to the start of this stroke
        cur, ent = e0, "a"
        seen = {e0}
        while (cur, ent) in link:
            nxt, nend = link[(cur, ent)]
            if nxt in seen:
                break
            seen.add(nxt)
            cur, ent = nxt, other(nend)
        start_node = edges[cur][ent]
        pts, through, closed = [], set(), False
        while True:
            done.add(cur)
            p = edges[cur]["pts"] if ent == "a" else edges[cur]["pts"][::-1]
            pts.append(p if not pts else p[1:])
            ex = other(ent)
            if (cur, ex) in link:
                nxt, nend = link[(cur, ex)]
                through.add(edges[cur][ex])
                if nxt in done:
                    closed = True  # came back round to where the stroke started
                    break
                cur, ent = nxt, nend
            else:
                break
        end_node = edges[cur][other(ent)]
        allpts = np.vstack(pts)
        if not closed and start_node == end_node and edges[cur]["a"] == edges[cur]["b"] and len(pts) == 1:
            closed = True  # a lone loop edge, e.g. the ring of an "o"
        tucks = set()
        for n in ((start_node, end_node) if not closed else ()):
            if deg.get(n, 0) >= 3 and n not in g.corners:
                tucks.add(n)
        strokes.append(dict(pts=allpts, a=start_node, b=end_node, closed=closed, tucks=tucks, through=through))
    return strokes


def satin_shape(mask, P, entry=None):
    """Satin-stitch a connected shape (one component) like a digitizer builds a letter:
    branches that end at a joint (crossbars, arms, bowls) sew first and tuck under;
    strokes that run straight through a joint sew afterwards as one unbroken column on top.
    Returns list of point lists in mm."""
    m8 = mask.astype(np.uint8)
    dt = cv2.distanceTransform(m8, cv2.DIST_L2, 5)
    g = StrokeGraph(mask, dt)
    if not g.edges:
        return []
    deg = g.degree()
    cnts, _ = cv2.findContours(m8, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    contour = np.vstack([c[:, 0, :] for c in cnts]).astype(float) if cnts else None
    strokes = _strokes(g, dt)
    for st in strokes:
        leaf = lambda n: deg.get(n, 0) == 1
        st["col"] = satin_column(mask, dt, st["pts"], P,
                                 ext_start=leaf(st["a"]) or st["a"] in g.corners,
                                 ext_end=leaf(st["b"]) or st["b"] in g.corners,
                                 cap_start=leaf(st["a"]), cap_end=leaf(st["b"]),
                                 contour=contour, closed=st["closed"])

    # a stroke that tucks under a joint stops just inside the edge of the stroke(s) running
    # through it (0.4 mm overlap) instead of fanning out in the middle of the joint
    overlap = max(2, int(round(0.4 * RES)))
    for st in strokes:
        if not st["tucks"] or st["col"]["closed"]:
            continue
        for n in st["tucks"]:
            over = [o for o in strokes if o is not st and n in o["through"]]
            if not over:
                continue
            cov_n = np.zeros(mask.shape, np.uint8)
            for o in over:
                oL, oR = o["col"]["rails"]
                for k in range(len(oL) - 1):
                    cv2.fillPoly(cov_n, [np.round(np.array([oL[k], oL[k + 1], oR[k + 1], oR[k]])).astype(np.int32)], 1)
            cov_n = cv2.erode(cov_n, np.ones((2 * overlap + 1, 2 * overlap + 1), np.uint8))
            col = st["col"]
            c, (L, R) = col["center"], col["rails"]
            inside = lambda p: 0 <= int(round(p[1])) < mask.shape[0] and 0 <= int(round(p[0])) < mask.shape[1] \
                and cov_n[int(round(p[1])), int(round(p[0]))] > 0
            lo, hi = 0, len(c)
            if st["a"] == n:
                while lo < hi - 2 and inside(c[lo]):
                    lo += 1
            if st["b"] == n:
                while hi > lo + 2 and inside(c[hi - 1]):
                    hi -= 1
            col["center"], col["rails"] = c[lo:hi], (L[lo:hi], R[lo:hi])

    # order: a stroke that tucks under a joint sews before the strokes running through it
    must_before = {i: set() for i in range(len(strokes))}
    for i, st in enumerate(strokes):
        for n in st["tucks"]:
            for j, other_st in enumerate(strokes):
                if j != i and n in other_st["through"]:
                    must_before[j].add(i)
    todo = set(range(len(strokes)))
    cur = None if entry is None else np.asarray(entry) * RES - 0.5
    seq_objs, seq = [], []
    cov = np.zeros(mask.shape, np.uint8)

    def inside(a, b):
        n = max(2, int(np.hypot(*(np.asarray(b) - a)) / 1.5))
        for t in np.linspace(0, 1, n):
            x, y = np.asarray(a) + (np.asarray(b) - np.asarray(a)) * t
            xi, yi = int(round(x)), int(round(y))
            if not (0 <= yi < mask.shape[0] and 0 <= xi < mask.shape[1] and mask[yi, xi]):
                return False
        return True

    while todo:
        ready = [i for i in todo if not (must_before[i] & todo)] or list(todo)  # cycles: just go

        def dist(i):
            col = strokes[i]["col"]
            if cur is None:
                return 0.0
            ends = [col["center"][0]] if col["closed"] else [col["center"][0], col["center"][-1]]
            return min(float(np.hypot(*(e - cur))) for e in ends)
        i = min(ready, key=dist)
        todo.discard(i)
        col = strokes[i]["col"]
        c, (L, R) = col["center"], col["rails"]
        rev = (not col["closed"] and cur is not None and
               np.hypot(*(c[-1] - cur)) < np.hypot(*(c[0] - cur)))
        order = list(range(len(c)))[::-1] if rev else list(range(len(c)))
        under = col["underlay"][::-1] if rev else col["underlay"]
        start_pt = np.asarray(under[0]) if under else c[order[0]]
        if seq and cur is not None and not inside(cur, start_pt):
            seq_objs.append(seq)
            seq = []
        seq.extend(under)  # out along the stroke with underlay...
        back = order[::-1] if not col["closed"] else order  # ...and back over it with satin
        satin_pts = [tuple(L[k]) if j % 2 == 0 else tuple(R[k]) for j, k in enumerate(back)]
        seq.extend(_split_long(satin_pts, P.satin_max_width * RES))
        for k in range(len(c) - 1 + (1 if col["closed"] else 0)):
            k2 = (k + 1) % len(c)
            cv2.fillPoly(cov, [np.round(np.array([L[k], L[k2], R[k2], R[k]])).astype(np.int32)], 1)
        cur = np.asarray(seq[-1])
    if seq:
        seq_objs.append(seq)

    # coverage safety net: anything the columns missed gets a small fill, sewn first (underneath)
    cov = cv2.dilate(cov, np.ones((3, 3), np.uint8))
    missed = mask & (cov == 0)
    patches = []
    if missed.any():
        missed = cv2.morphologyEx(missed.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)).astype(bool)
        for poly in mask_to_polygons(missed, smooth_px=0.5):
            if poly.area >= 0.35:
                patches += tatami(poly, P.fill_angle, P.fill_spacing, P.fill_length, P.fill_staggers,
                                  P.min_stitch, P.travel_length, None)
    main = [clean([tuple(p) for p in to_mm(np.asarray(o))], P.min_stitch * 0.6) for o in seq_objs if len(o) > 1]
    shape_poly = mask_to_polygons(mask)
    region = shape_poly[0] if shape_poly else None
    pieces = [pt for pt in patches if pt] + [m for m in main if m]
    return _chain(pieces, region, P.travel_length) if region is not None else pieces


def run_shape(mask, P, entry=None, repeats=2):
    """Thin shapes: double (there-and-back) running stitch along the skeleton."""
    dt = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 5)
    g = StrokeGraph(mask, dt)
    if not g.edges:
        return []
    adj = {}
    for idx, e in enumerate(g.edges):
        adj.setdefault(e["a"], []).append(idx)
        adj.setdefault(e["b"], []).append(idx)
    deg = g.degree()
    start = next((n for n, d in deg.items() if d == 1), next(iter(deg)))
    seq, done = [], set()
    L = P.run_length * RES * 0.8

    def walk(n):
        for idx in adj.get(n, []):
            if idx in done:
                continue
            done.add(idx)
            e = g.edges[idx]
            fwd = e["a"] == n
            pts = e["pts"] if fwd else e["pts"][::-1]
            out = run_points(pts, L)
            seq.extend(out)
            if e["a"] == e["b"]:
                continue
            walk(e["b"] if fwd else e["a"])
            seq.extend(out[::-1])
    walk(start)
    if seq and repeats >= 3:
        # bean stitch: every stitch sewn forward, back and forward again - a bold hand-drawn line
        bean = [seq[0]]
        for q in seq[1:]:
            p0 = bean[-1]
            bean += [q, p0, q]
        seq = bean
    return [clean([tuple(p) for p in to_mm(np.asarray(seq))], P.min_stitch)] if seq else []


# ----------------------------------------------------------------------------- satin fill & border

def satin_fill(poly, P, angle=None, start=None):
    """Single-direction satin across a small solid shape (hearts, dots, badges):
    one long stitch per row, zig-zagging. Rows wider than satin_max get split stitches."""
    if angle is None:
        # stitches run along the shape's short axis so they stay short and shiny
        rect = poly.minimum_rotated_rectangle
        c = list(rect.exterior.coords)
        e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
        e2 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
        short = e1 if math.hypot(*e1) < math.hypot(*e2) else e2
        angle = math.degrees(math.atan2(short[1], short[0]))
        angle = angle + 15 if abs(((angle % 180) - 90)) < 5 or abs(angle % 180) < 5 else angle  # avoid dead-straight
    poly = poly.buffer(P.pull_comp * 0.8, join_style=1)
    pieces = []
    inner = poly.buffer(-max(0.4, P.underlay_inset))
    if not inner.is_empty and poly.area > 6:
        for part in (inner.geoms if inner.geom_type == "MultiPolygon" else [inner]):
            pieces += tatami(part, angle + 90, 1.6, 2.5, 1, P.min_stitch, P.travel_length, start)
    top = tatami(poly, angle, P.satin_spacing / 2, P.satin_max_width, 2, P.min_stitch * 1.2, P.travel_length,
                 pieces[-1][-1] if pieces else start)
    return _chain(pieces + top, poly, P.travel_length)


def satin_rows(poly, P, angle=None, start=None, max_len=12.0):
    """Side-to-side satin across a solid shape: one straight stitch per row from edge to edge, the
    rows packed at satin spacing - the long glossy pass. Rows longer than the machine can take in one
    stitch (max_len) are split at scattered points so no groove lines up. Falls back to satin_fill
    if the rows would have to jump across a gap (very concave shapes)."""
    from shapely import affinity
    from shapely.geometry import LineString
    if angle is None:
        rect = poly.minimum_rotated_rectangle
        c = list(rect.exterior.coords)
        e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
        e2 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
        short = e1 if math.hypot(*e1) < math.hypot(*e2) else e2
        angle = math.degrees(math.atan2(short[1], short[0]))
    poly = poly.buffer(P.pull_comp * 0.8, join_style=1)
    # rotate so the stitches lie along x; rows step along y
    cx, cy = poly.centroid.x, poly.centroid.y
    rp = affinity.rotate(poly, -angle, origin=(cx, cy))
    x0, y0, x1, y1 = rp.bounds
    step = P.satin_spacing / 2
    rows = []
    y = y0 + step / 2
    while y < y1:
        seg = rp.intersection(LineString([(x0 - 1, y), (x1 + 1, y)]))
        parts = [g for g in getattr(seg, "geoms", [seg]) if not g.is_empty and g.length > 0.05]
        if len(parts) > 1:
            return satin_fill(poly.buffer(-P.pull_comp * 0.8), P, angle=angle, start=start)
        if parts:
            xs = [p[0] for p in parts[0].coords]
            rows.append((min(xs), max(xs), y))
        y += step
    if not rows:
        return []
    pts = []
    for k, (a, b, yy) in enumerate(rows):
        pts.append((a, yy) if k % 2 == 0 else (b, yy))
        pts.append((b, yy) if k % 2 == 0 else (a, yy))
    # each row is one pass; consecutive rows join at the edge (zig-zag)
    zig = [pts[0]]
    for k in range(1, len(rows)):
        zig.append(pts[2 * k - 1])
        zig.append(pts[2 * k])
    zig.append(pts[-1])
    out = []
    golden = 0.0
    for p in zig:
        if out:
            a = out[-1]
            d = math.hypot(p[0] - a[0], p[1] - a[1])
            if d > max_len:
                n = int(math.ceil(d / max_len))
                golden = (golden + 0.618) % 1.0  # scattered split points, never a straight groove
                for j in range(n - 1):
                    t = (j + 0.3 + 0.4 * golden) / n
                    out.append((a[0] + (p[0] - a[0]) * t, a[1] + (p[1] - a[1]) * t))
        out.append(p)
    back = lambda q: affinity.rotate(LineString([q, (q[0] + 1e-6, q[1])]), angle, origin=(cx, cy)).coords[0]
    top = [tuple(back(q)) for q in out]
    # underlay: an edge walk just inside the outline plus a light zig-zag across the stitches
    pieces = []
    inner = poly.buffer(-max(0.4, P.underlay_inset))
    if not inner.is_empty and poly.area > 6:
        for part in (inner.geoms if inner.geom_type == "MultiPolygon" else [inner]):
            pieces += tatami(part, angle + 90, 1.6, 2.5, 1, P.min_stitch, P.travel_length, start)
    return _chain(pieces + [clean(top, P.min_stitch * 0.6)], poly, P.travel_length)


def star_satin(poly, P, start=None, max_len=12.0):
    """A star sewn the way digitizers do it: one diamond per point (centre, inner corner, tip,
    inner corner), each with satin running across the arm, all meeting cleanly in the middle."""
    from shapely.geometry import Polygon
    c = poly.centroid
    pts = list(poly.exterior.coords)[:-1]
    if len(pts) < 8:
        return satin_rows(poly, P, start=start, max_len=max_len)
    r = [math.hypot(x - c.x, y - c.y) for x, y in pts]
    n = len(pts)
    # tips: the star's points are the corners of its convex hull; a rounded tip gives a little
    # cluster of hull corners - keep the farthest one of each cluster
    hull = set(poly.convex_hull.exterior.coords)
    cand = [i for i in range(n) if pts[i] in hull and r[i] > 0.7 * max(r)]
    ang = lambda i: math.atan2(pts[i][1] - c.y, pts[i][0] - c.x)
    cand.sort(key=ang)
    groups = []
    for i in cand:
        if groups and abs(ang(i) - ang(groups[-1][-1])) < math.radians(20):
            groups[-1].append(i)
        else:
            groups.append([i])
    if len(groups) > 1 and (ang(groups[0][0]) + 2 * math.pi - ang(groups[-1][-1])) < math.radians(20):
        groups[0] = groups.pop() + groups[0]
    tips = sorted(max(g, key=lambda i: r[i]) for g in groups)
    if len(tips) < 3:
        return satin_rows(poly, P, start=start, max_len=max_len)
    out = []
    cur = start
    for t_i, t in enumerate(tips):
        prev_t, next_t = tips[t_i - 1], tips[(t_i + 1) % len(tips)]
        # inner corners: the closest-to-centre points between this tip and its neighbours
        span_a = [(i % n) for i in range(prev_t + 1, t + (n if t <= prev_t else 0))]
        span_b = [(i % n) for i in range(t + 1, next_t + (n if next_t <= t else 0))]
        if not span_a or not span_b:
            continue
        ia = min(span_a, key=lambda i: r[i])
        ib = min(span_b, key=lambda i: r[i])
        kite = Polygon([(c.x, c.y), pts[ia], pts[t], pts[ib]]).buffer(0.25, join_style=2)  # overlap neighbours
        tx, ty = pts[t][0] - c.x, pts[t][1] - c.y
        ang = math.degrees(math.atan2(ty, tx)) + 90  # stitches across the arm
        part = satin_rows(kite, P, angle=ang, start=cur, max_len=max_len)
        if part:
            out += part
            cur = part[-1][-1]
    return out


def border_satin(poly, width, P, start=None):
    """Satin border following an outline ring (for outlined lettering / badges).
    poly is the ring's centre-line polygon; returns list of point lists (mm)."""
    out = []
    rings = [poly.exterior] + list(poly.interiors)
    for ring in rings:
        c = np.asarray(ring.coords)
        if len(c) < 4:
            continue
        step = P.satin_spacing / 2
        c = resample(c, step)[:-1]
        if len(c) < 6:
            continue
        # smooth the centre line a little so normals don't jitter at pixel stairs
        c = np.column_stack([ndimage.gaussian_filter1d(c[:, 0], 2, mode="wrap"),
                             ndimage.gaussian_filter1d(c[:, 1], 2, mode="wrap")])
        if start is not None:
            j = int(np.argmin(np.hypot(c[:, 0] - start[0], c[:, 1] - start[1])))
            c = np.vstack([c[j:], c[:j]])
        c = np.vstack([c, c[:1]])
        tan = np.gradient(c, axis=0)
        tn = np.hypot(tan[:, 0], tan[:, 1])
        tn[tn == 0] = 1
        tan /= tn[:, None]
        nor = np.column_stack([-tan[:, 1], tan[:, 0]])
        hw = width / 2 + P.pull_comp * 0.5
        L, R = c + nor * hw, c - nor * hw
        for rail in (L, R):  # tight inside curves: don't let a rail run backwards
            for i in range(1, len(rail)):
                if np.dot(rail[i] - rail[i - 1], tan[i]) < -0.02:
                    rail[i] = rail[i - 1]
        under = run_points(c, P.run_length)
        satin = [tuple(L[i]) if i % 2 == 0 else tuple(R[i]) for i in range(len(c))]
        out.append(clean(list(under) + satin, P.min_stitch * 0.6))
        start = out[-1][-1]  # next ring (a letter's hole) starts nearest where this one ended
    return out


def join_hidden(objs, cover, P, edge_mm=0.9, max_len=25.0):
    """Link consecutive objects with a running stitch where the straight hop between them lies
    inside `cover` (a mask of what will be sewn on top later, e.g. the letters for a border
    sewn first). Replaces jump threads the machine would drag across the letters - a Designer I
    has no thread cutter. The first/last `edge_mm` of a hop may sit under the border itself."""
    if not objs:
        return objs
    H, W = cover.shape
    out = [list(objs[0])]
    for o in objs[1:]:
        a, b = np.asarray(out[-1][-1], float), np.asarray(o[0], float)
        L = float(np.hypot(*(b - a)))
        ok = 0 < L <= max_len
        if ok:
            for t in np.linspace(0, 1, max(3, int(L / 0.15))):
                d = t * L
                if d < edge_mm or L - d < edge_mm:
                    continue
                x, y = (a + (b - a) * t) * RES
                xi, yi = int(round(x)), int(round(y))
                if not (0 <= xi < W and 0 <= yi < H and cover[yi, xi]):
                    ok = False
                    break
        if ok:
            out[-1] += [tuple(p) for p in run_points(np.array([a, b]), 2.0)[1:]] + list(o[1:])
        else:
            out.append(list(o))
    return out


def border_shape(mask, width, gap, P, entry=None):
    """Outline around a shape mask: satin ring of `width` mm whose inner edge sits `gap` mm
    outside the shape (negative gap = overlap into the shape)."""
    polys = mask_to_polygons(mask, smooth_px=1.2)
    out = []
    for poly in polys:
        centre = poly.buffer(gap + width / 2, join_style=1, resolution=8)
        for part in (centre.geoms if centre.geom_type == "MultiPolygon" else [centre]):
            out += border_satin(part, width, P, entry)
            if out and out[-1]:
                entry = out[-1][-1]
    return out


# ----------------------------------------------------------------------------- classification

def classify(mask, P):
    """Pick satin / fill / run for a connected component from its stroke widths."""
    dt = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 5)
    sk = skeletonize(mask)
    if not sk.any():
        return "run"
    widths = 2 * dt[sk] / RES
    p80, p50, p95 = np.percentile(widths, 80), np.percentile(widths, 50), np.percentile(widths, 95)
    if p80 <= P.run_max_width:
        return "run"
    # blobs (heart, dot, badge) have a short skeleton relative to their width: stroke-following
    # satin would radiate, so use straight satin (or fill if too big for satin)
    sk_len = sk.sum() / RES
    if sk_len < 2.2 * max(p95, 0.1):
        return "satinfill" if mask.sum() / RES / RES < 250 else "fill"
    # satin only where the stroke is narrow enough to lie flat (pros: ~7 mm, a little more at
    # the widest spots); anything bigger is tatami fill - split satin on 15-20 mm letters snags
    # and looks messy
    if p50 <= P.satin_max_width and p80 <= P.satin_max_width * 1.25:
        return "satin"
    return "fill"
