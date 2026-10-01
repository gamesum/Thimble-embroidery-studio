"""Pre-sew checks: look through the stitches the way a digitizer proof-reads a design before sewing,
and report anything likely to cause trouble, with where it is (mm, hoop centre = 0,0).

Each problem is {kind, level ("warn" | "info"), msg, x, y, n}. Nothing here changes the stitches."""
import math
import numpy as np

MIN_STITCH = 0.25      # mm: shorter stitches are fused by the machine / can't be sewn
PILE = 18.0            # mm of thread per mm2 (averaged over 2x2 mm): a normal fill is ~4, satin ~8-12
PILE_AREA = 25         # mm2 of it in one patch before it's worth a warning
JUMP_FAR = 25.0        # mm: a jump this long is a visible thread to snip
# (the floppy writer already splits any stitch over 12.7 mm into equal steps, so long stitches are not a problem)


def thread_grid(blocks, W, H):
    """mm of thread laid down per 1 mm2 cell (each stitch's length spread along its path)."""
    g = np.zeros((int(math.ceil(W)) + 3, int(math.ceil(H)) + 3))
    for b in blocks:
        for o in b["objects"]:
            p = np.asarray(o, float)
            if len(p) < 2:
                continue
            seg = np.hypot(*np.diff(p, axis=0).T)
            n = np.maximum(1, np.ceil(seg / 0.5).astype(int))
            a, d = np.repeat(p[:-1], n, axis=0), np.repeat(np.diff(p, axis=0), n, axis=0)
            t = np.concatenate([(np.arange(k) + 0.5) / k for k in n])
            pt = a + d * t[:, None]
            ix = np.clip((pt[:, 0] + W / 2).astype(int) + 1, 0, g.shape[0] - 1)
            iy = np.clip((pt[:, 1] + H / 2).astype(int) + 1, 0, g.shape[1] - 1)
            np.add.at(g, (ix, iy), np.repeat(seg / n, n))
    return g


def check(blocks, hoop):
    from scipy import ndimage
    probs = []
    W, H = hoop
    n_short, short_at, jumps, tiny = 0, [], [], []
    last = None
    for b in blocks:
        for o, kind in zip(b["objects"], b["kinds"]):
            pts = np.asarray(o, float)
            if len(pts) < 2:
                continue
            if last is not None:
                d = float(np.hypot(*(pts[0] - last)))
                if d > JUMP_FAR:
                    jumps.append((last, pts[0], d))
            last = pts[-1]
            if kind != "stitches":
                seg = np.hypot(*np.diff(pts, axis=0).T)
                sh = np.nonzero(seg < MIN_STITCH)[0]
                n_short += len(sh)
                if len(sh) and len(short_at) < 3:
                    short_at.append(pts[sh[0]])
            ext = pts.max(0) - pts.min(0)
            if kind in ("fill", "satin", "satinfill", "satinrows", "rows") and ext.max() < 0.8 and len(pts) < 12:
                tiny.append(pts.mean(0))
    g = thread_grid(blocks, W, H)
    sm = (g[:-1, :-1] + g[1:, :-1] + g[:-1, 1:] + g[1:, 1:]) / 4
    lab, n = ndimage.label(sm > PILE)
    patches = []
    for k in range(1, n + 1):
        cells = np.argwhere(lab == k)
        if len(cells) >= PILE_AREA:
            c = cells.mean(0)
            patches.append((len(cells), float(sm[lab == k].max()), c))
    patches.sort(key=lambda t: -t[0])
    for area, peak, c in patches[:3]:
        probs.append(dict(kind="pile", level="warn", n=int(area), x=round(float(c[0] + 0.5 - 1) - W / 2, 1), y=round(float(c[1] + 0.5 - 1) - H / 2, 1),
                          msg="Thread builds up in about %d mm2 here (%.0f mm of thread per mm2; a normal fill is ~4). It can get stiff, "
                              "break needles or pucker the fabric - use fewer overlapping layers, merge colors, or lighten the density." % (area, peak)))
    if n_short > 12 and short_at:
        p = short_at[0]
        probs.append(dict(kind="short", level="info", n=n_short, x=round(float(p[0]), 1), y=round(float(p[1]), 1),
                          msg="%d very short stitches (under 0.3 mm). The machine merges them, but many can fray the thread - "
                              "try the Smooth slider or a lighter density." % n_short))
    if len(tiny) >= 3:
        p = tiny[0]
        probs.append(dict(kind="tiny", level="info", n=len(tiny), x=round(float(p[0]), 1), y=round(float(p[1]), 1),
                          msg="%d details are smaller than 1 mm - too small to sew cleanly. Raise \"leave out shorter than\" / Smooth, or delete them." % len(tiny)))
    if jumps:
        a, b, d = max(jumps, key=lambda j: j[2])
        probs.append(dict(kind="jump", level="info", n=len(jumps), x=round(float((a[0] + b[0]) / 2), 1), y=round(float((a[1] + b[1]) / 2), 1),
                          msg="%d long jump%s (up to %.0f mm) - each leaves a thread to snip. Moving pieces closer or reordering them helps."
                              % (len(jumps), "" if len(jumps) == 1 else "s", d)))
    return probs
