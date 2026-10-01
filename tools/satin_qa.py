"""Satin quality check for big lettering: stitch every letter of some heavy fonts as wide satin
and score it, so changes to the satin code can be measured instead of eyeballed.

  coverage  share of the letter covered by thread (holes and bare corners lower it)
  hole      biggest bare patch inside the letter, mm^2
  spill     share of thread landing more than 0.6 mm outside the letter
  chaos     how much the stitch angle jumps between neighbouring satin stitches (fans, tangles)
  pile      thread stacked in the worst 1 mm spot vs a typical spot (fans and knots pile up:
            hard lumps, broken needles); a clean satin letter stays under ~2.5

  python tools/satin_qa.py                      # default fonts, A-Z a-z 0-9, 40 mm
  python tools/satin_qa.py --fonts "Anton" --letters AKMW --height 30 --sheet out.png

Prints the worst letters and a summary per font; --sheet saves a contact sheet of the worst."""
import argparse, math, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cv2
import numpy as np
from dataclasses import replace
from PIL import Image, ImageDraw
from engine import raster, design
from engine.params import params_for, RES

DEFAULT_FONTS = ["Archivo Black", "Montserrat Black", "Anton", "League Spartan Bold", "Luckiest Guy", "Bowlby One SC"]
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def score(mask, objects):
    """Metrics for one letter; objects are needle points in mm in the mask's frame."""
    h, w = mask.shape
    pad = 20
    drawn = np.zeros((h + 2 * pad, w + 2 * pad), np.uint8)
    angles, jumps = [], []
    for o in objects:
        pts = np.round((np.asarray(o) * RES) + pad).astype(np.int32)
        if len(pts) > 1:
            cv2.polylines(drawn, [pts], False, 1, thickness=4)  # ~0.4 mm thread
        a_prev = None
        for p, q in zip(o, o[1:]):
            d = np.subtract(q, p)
            L = math.hypot(*d)
            if L < 2.0:  # underlay, travel and short stitches don't count for the angle
                a_prev = None
                continue
            a = math.atan2(d[1], d[0]) % math.pi  # a satin zig and zag share a line
            if a_prev is not None:
                da = abs(a - a_prev)
                jumps.append(min(da, math.pi - da))
            a_prev = a
    drawn = drawn[pad:pad + h, pad:pad + w] if drawn.shape else drawn
    m = mask.astype(bool)
    cov = float((drawn.astype(bool) & m).sum()) / max(1, m.sum())
    inner = cv2.erode(m.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    bare = (inner & ~drawn.astype(bool)).astype(np.uint8)
    n, _, st, _ = cv2.connectedComponentsWithStats(bare, 8)
    hole = float(st[1:, cv2.CC_STAT_AREA].max()) / RES / RES if n > 1 else 0.0
    big = np.zeros((h + 2 * pad, w + 2 * pad), np.uint8)
    for o in objects:
        pts = np.round((np.asarray(o) * RES) + pad).astype(np.int32)
        if len(pts) > 1:
            cv2.polylines(big, [pts], False, 1, thickness=4)
    halo = np.zeros_like(big)
    halo[pad:pad + h, pad:pad + w] = m
    halo = cv2.dilate(halo, np.ones((13, 13), np.uint8))
    spill = float((big.astype(bool) & ~halo.astype(bool)).sum()) / max(1, big.sum())
    # 'chaos': the typical jump in stitch angle between neighbours, in degrees (a clean
    # column turns a degree or two per stitch; fans and tangles jump tens of degrees)
    chaos = float(np.percentile(np.degrees(jumps), 90)) if jumps else 0.0
    # thread pile-up: draw every stitch 1 px wide additively, smooth over 1 mm
    acc = np.zeros((h + 2 * pad, w + 2 * pad), np.float32)
    for o in objects:
        pts = np.round((np.asarray(o) * RES) + pad).astype(np.int32)
        for a, b in zip(pts, pts[1:]):
            one = np.zeros_like(acc, np.uint8)
            cv2.line(one, tuple(int(v) for v in a), tuple(int(v) for v in b), 1, 1)
            acc += one
    acc = cv2.blur(acc, (RES, RES))[pad:pad + h, pad:pad + w]
    vals = acc[inner]
    pile = float(np.percentile(vals, 99.7) / max(np.median(vals), 1e-3)) if vals.size else 0.0
    return dict(coverage=cov, hole=hole, spill=spill, chaos=chaos, pile=pile)


def run(fonts, letters, height, fabric="knit"):
    P = replace(params_for(fabric, "dense"), satin_max_width=12.0)
    rows = []
    for font in fonts:
        for ch in letters:
            m, _ = raster.text_mask(ch, font, height, 0, 1.2, "center", 0)
            t = time.time()
            objs, kinds = design.layer_objects(m, "satin", P)
            dt = time.time() - t
            s = score(m, objs)
            s.update(font=font, ch=ch, secs=dt, mask=m, objects=objs)
            rows.append(s)
    return rows


def badness(r):
    return (1 - r["coverage"]) * 100 + r["hole"] * 2 + r["spill"] * 100 + max(0.0, r["chaos"] - 15) * 0.5 + max(0.0, r["pile"] - 2.5) * 6


def sheet(rows, path, n=24, cell=330):
    rows = sorted(rows, key=badness, reverse=True)[:n]
    cols = 4
    im = Image.new("RGB", (cols * cell, ((len(rows) + cols - 1) // cols) * (cell + 18)), "white")
    d = ImageDraw.Draw(im)
    for i, r in enumerate(rows):
        m = r["mask"]
        k = (cell - 16) / max(m.shape)
        ox, oy = (i % cols) * cell + 8, (i // cols) * (cell + 18) + 18
        hw, hh = m.shape[1] / RES, m.shape[0] / RES
        objs = [[(x - hw / 2, y - hh / 2) for x, y in o] for o in r["objects"]]
        png = design.render_preview([dict(color="#203a6a", objects=objs)], (hw, hh), scale=RES * k * 1.0)
        import io
        tile = Image.open(io.BytesIO(png)).convert("RGBA")
        bg = Image.new("RGBA", tile.size, (236, 236, 236, 255))
        bg.alpha_composite(tile)
        im.paste(bg.convert("RGB"), (ox, oy))
        d.text((ox, oy - 15), "%s %s hole %.1f spill %.1f%% chaos %.0f pile %.1f" % (
            r["font"].split()[0], r["ch"], r["hole"], r["spill"] * 100, r["chaos"], r["pile"]), fill=(150, 30, 30))
    im.save(path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fonts", default=",".join(DEFAULT_FONTS))
    ap.add_argument("--letters", default=LETTERS)
    ap.add_argument("--height", type=float, default=40.0)
    ap.add_argument("--sheet", default="")
    ap.add_argument("--worst", type=int, default=15)
    a = ap.parse_args()
    rows = run([f.strip() for f in a.fonts.split(",") if f.strip()], a.letters, a.height)
    print("%-22s %6s %6s %6s %6s %6s %6s" % ("font", "cover", "hole", "spill", "chaos", "pile", "secs"))
    for font in dict.fromkeys(r["font"] for r in rows):
        rs = [r for r in rows if r["font"] == font]
        print("%-22s %5.1f%% %6.1f %5.2f%% %6.1f %6.1f %6.1f" % (font, np.mean([r["coverage"] for r in rs]) * 100,
              np.max([r["hole"] for r in rs]), np.mean([r["spill"] for r in rs]) * 100,
              np.median([r["chaos"] for r in rs]), np.median([r["pile"] for r in rs]), sum(r["secs"] for r in rs)))
    print("\nworst letters:")
    for r in sorted(rows, key=badness, reverse=True)[:a.worst]:
        print("  %-20s %s  cover %5.1f%%  hole %5.1f mm2  spill %4.1f%%  chaos %4.0f  pile %4.1f" % (
            r["font"], r["ch"], r["coverage"] * 100, r["hole"], r["spill"] * 100, r["chaos"], r["pile"]))
    allr = rows
    print("\nOVERALL cover %.1f%%  worst hole %.1f mm2  spill %.2f%%  chaos(med) %.1f  bad letters %d/%d" % (
        np.mean([r["coverage"] for r in allr]) * 100, max(r["hole"] for r in allr),
        np.mean([r["spill"] for r in allr]) * 100, np.median([r["chaos"] for r in allr]),
        sum(badness(r) > 10 for r in allr), len(allr)))
    if a.sheet:
        sheet(rows, a.sheet)
