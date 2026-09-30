"""Text and image -> clean single-colour masks at RES px/mm."""
import json, math, os
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

from .params import RES

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")
_fonts = None


def fonts():
    global _fonts
    if _fonts is None:
        _fonts = json.load(open(os.path.join(FONT_DIR, "fonts.json")))
    return _fonts


def font_path(name):
    for f in fonts():
        if f["name"] == name:
            return os.path.join(FONT_DIR, f["file"])
    return os.path.join(FONT_DIR, fonts()[0]["file"])


# ----------------------------------------------------------------------------- text

def _cap_scale(path):
    """Font px size that gives a cap height of 100 px."""
    f = ImageFont.truetype(path, 200)
    b = f.getbbox("H")
    return 200 * 100 / max(1, b[3] - b[1])


def text_mask(text, font, height_mm, letter_spacing=0.0, line_spacing=1.25, align="center", arc=0.0, labels=False):
    """Render text so capital letters are height_mm tall. Returns (mask, (w_mm, h_mm)).
    letter_spacing is a fraction of the cap height (0 keeps the font's own kerning and
    script connections). arc (degrees, +up/-down) bends each line along a circle.
    labels=True also returns an int image (same crop) giving, per pixel, 1 + the index in
    `text` of the character that drew it (0 = none) - used for per-letter colours."""
    path = font_path(font)
    ss = 2  # supersample for smooth edges
    cap_px = height_mm * RES * ss
    size = max(4, int(round(_cap_scale(path) * cap_px / 100)))
    f = ImageFont.truetype(path, size)
    lines = text.split("\n") or [""]
    asc, desc = f.getmetrics()
    line_h = int((asc + desc) * line_spacing)
    track = letter_spacing * cap_px

    def line_img(s, base=0, lab=False):
        # lab=True: draw each character with its own value 1+index (index into `text`)
        if not s:
            return Image.new("L", (1, asc + desc))
        if lab:
            b = f.getbbox(s)
            if track == 0:
                im = Image.new("I", (b[2] - min(0, b[0]) + 4, asc + desc + 4))
                x0, xs = 2 - min(0, b[0]), [f.getlength(s[:i]) for i in range(len(s))]
            else:
                widths = [f.getlength(ch) for ch in s]
                im = Image.new("I", (int(sum(widths) + track * (len(s) - 1) + size), asc + desc + 4))
                x0, xs = 2, list(np.cumsum([0] + [w + track for w in widths[:-1]]))
            d = ImageDraw.Draw(im)
            for i, ch in enumerate(s):
                if ch.strip():
                    d.text((x0 + xs[i], 2), ch, font=f, fill=base + i + 1)
            return im
        if track == 0:
            b = f.getbbox(s)
            im = Image.new("L", (b[2] - min(0, b[0]) + 4, asc + desc + 4))
            ImageDraw.Draw(im).text((2 - min(0, b[0]), 2), s, font=f, fill=255)
            return im
        widths = [f.getlength(ch) for ch in s]
        W = int(sum(widths) + track * (len(s) - 1) + size)
        im = Image.new("L", (W, asc + desc + 4))
        d = ImageDraw.Draw(im)
        x = 2
        for ch, w in zip(s, widths):
            d.text((x, 2), ch, font=f, fill=255)
            x += w + track
        return im

    imgs = [line_img(s) for s in lines]
    starts = np.cumsum([0] + [len(s) + 1 for s in lines[:-1]])
    limgs = [line_img(s, int(b), True) for s, b in zip(lines, starts)] if labels else None
    if arc:
        imgs = [_bend(im, arc) for im in imgs]
        if labels:
            limgs = [_bend_labels(im, arc) for im in limgs]
    W = max(im.width for im in imgs)
    H = line_h * (len(imgs) - 1) + imgs[-1].height
    canvas = Image.new("L", (W, max(H, 1)))
    for n, im in enumerate(imgs):
        x = {"left": 0, "right": W - im.width}.get(align, (W - im.width) // 2)
        canvas.paste(im, (x, n * line_h), im)
    small = canvas.resize((max(1, W // ss), max(1, canvas.height // ss)), Image.LANCZOS)
    m = np.asarray(small) > 110
    if not labels:
        return crop(m)
    lab = np.zeros((canvas.height, W), np.int32)
    for n, (im, li) in enumerate(zip(imgs, limgs)):
        x = {"left": 0, "right": W - im.width}.get(align, (W - im.width) // 2)
        a = np.asarray(li, np.int32)
        h, w = min(a.shape[0], lab.shape[0] - n * line_h), min(a.shape[1], W - x)
        region = lab[n * line_h:n * line_h + h, x:x + w]
        np.copyto(region, a[:h, :w], where=a[:h, :w] > 0)
    lab = cv2.resize(lab.astype(np.float32), (m.shape[1], m.shape[0]), interpolation=cv2.INTER_NEAREST).astype(np.int32)
    # stitched pixels the label render missed (anti-aliased rims): take the nearest letter
    if (m & (lab == 0)).any() and (lab > 0).any():
        _, idx = cv2.distanceTransformWithLabels((lab == 0).astype(np.uint8), cv2.DIST_L2, 3, labelType=cv2.DIST_LABEL_PIXEL)
        ys, xs = np.nonzero(lab > 0)
        table = np.zeros(idx.max() + 1, np.int32)
        table[idx[ys, xs]] = lab[ys, xs]
        lab = np.where(lab > 0, lab, table[idx])
    ys, xs = np.nonzero(m)
    if len(xs) == 0:
        return crop(m) + (np.zeros((1, 1), np.int32),)
    y0, y1, x0, x1 = max(0, ys.min() - 2), ys.max() + 3, max(0, xs.min() - 2), xs.max() + 3
    mc, size_mm = crop(m)
    return mc, size_mm, lab[y0:y1, x0:x1][:mc.shape[0], :mc.shape[1]]


def _bend_labels(im, arc_deg):
    """_bend for a label image (nearest-neighbour so letter ids are never blended)."""
    a = np.asarray(im, np.int32).astype(np.float32)
    return Image.fromarray(np.asarray(_bend(Image.fromarray(a, "F"), arc_deg, cv2.INTER_NEAREST), np.float32).astype(np.int32), "I")


def _bend(im, arc_deg, interp=cv2.INTER_LINEAR):
    """Bend a text line image along a circular arc (positive = arch up)."""
    a = np.asarray(im)
    h, w = a.shape
    theta = math.radians(min(abs(arc_deg), 300))
    R = w / theta  # radius at the baseline (arch) or cap line (smile)
    sag = R * (1 - math.cos(min(theta, math.pi) / 2))
    out_h = int(math.ceil(sag + h)) + 4
    cx = w / 2
    ys, xs = np.mgrid[0:out_h, 0:w].astype(np.float32)
    if arc_deg > 0:  # arch: centre below, baseline on radius R
        cy = R + h + 2
        dx, dy = xs - cx, cy - ys
        src_y = h - (np.hypot(dx, dy) - R)
    else:  # smile: centre above, cap line on radius R
        cy = -R * math.cos(min(theta, math.pi) / 2) + 2
        dx, dy = xs - cx, ys - cy
        src_y = np.hypot(dx, dy) - R
    src_x = cx + np.arctan2(dx, dy) * R
    res = cv2.remap(a, src_x.astype(np.float32), src_y.astype(np.float32), interp,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return Image.fromarray(res)


def crop(m, pad=2):
    ys, xs = np.nonzero(m)
    if len(xs) == 0:
        return np.zeros((1, 1), bool), (0.1, 0.1)
    m = m[max(0, ys.min() - pad):ys.max() + pad + 1, max(0, xs.min() - pad):xs.max() + pad + 1]
    return m, (m.shape[1] / RES, m.shape[0] / RES)


def outline_mask(mask, width_mm, gap_mm=0.0):
    """Ring around a shape (for outlined lettering). Returns mask padded to fit."""
    r_out = int(round((width_mm + gap_mm) * RES))
    r_gap = int(round(gap_mm * RES))
    pad = r_out + 2
    m = np.pad(mask, pad)
    k = lambda r: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    outer = cv2.dilate(m.astype(np.uint8), k(r_out)).astype(bool)
    inner = cv2.dilate(m.astype(np.uint8), k(r_gap)).astype(bool) if r_gap else m
    # the ring overlaps the letter by 0.3 mm so no fabric shows between them
    overlap = cv2.erode(m.astype(np.uint8), k(max(1, int(0.3 * RES)))).astype(bool) if not r_gap else inner
    return (outer & ~overlap) if not r_gap else (outer & ~inner), pad


# ----------------------------------------------------------------------------- images

def load_image(path_or_file):
    im = Image.open(path_or_file)
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg
    return im.convert("RGB")


def palette(im, max_colors=8, min_share=0.004):
    """Dominant flat colours of artwork (anti-aliasing blends are ignored)."""
    small = im.copy()
    small.thumbnail((400, 400))
    q = small.quantize(colors=max_colors * 3, method=Image.Quantize.MEDIANCUT)
    pal = np.array(q.getpalette()[:max_colors * 9]).reshape(-1, 3)
    counts = np.bincount(np.asarray(q).ravel(), minlength=len(pal))
    order = np.argsort(-counts)
    total = counts.sum()
    out = []
    for i in order:
        if counts[i] / total < min_share:
            break
        c = pal[i].astype(float)
        if all(np.linalg.norm(c - o) > 42 for o in out):
            out.append(c)
        if len(out) >= max_colors:
            break
    # a colour that is just a 50/50 blend of two others is anti-aliasing, not ink
    keep = []
    for i, c in enumerate(out):
        blend = any(np.linalg.norm(c - (out[a] + out[b]) / 2) < 22 for a in range(len(out)) for b in range(a + 1, len(out))
                    if a != i and b != i)
        if not blend or i < 2:
            keep.append(c)
    return [tuple(int(v) for v in c) for c in keep]


def snap(im_arr, pal):
    """Label each pixel with a palette index; edge pixels (blends of two colours) go to the
    nearer end of that pair so no false outline colours appear."""
    px = im_arr.reshape(-1, 3).astype(np.float32)
    P = np.array(pal, np.float32)
    best_d = np.full(len(px), np.inf, np.float32)
    best_i = np.zeros(len(px), np.int32)
    for a in range(len(P)):
        for b in range(a, len(P)):
            v = P[b] - P[a]
            L2 = float(v @ v)
            t = np.zeros(len(px), np.float32) if L2 == 0 else np.clip(((px - P[a]) @ v) / L2, 0, 1)
            d = ((px - (P[a] + t[:, None] * v)) ** 2).sum(1)
            better = d < best_d
            best_d[better] = d[better]
            best_i[better] = np.where(t[better] < 0.5, a, b)
    return best_i.reshape(im_arr.shape[:2])


def background_index(labels):
    border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    return int(np.bincount(border).argmax())


def image_masks(im, width_mm, pal=None, min_area_mm2=0.8):
    """Artwork -> (palette, [mask per colour], size_mm). Masks are cleaned of specks."""
    w = max(8, int(round(width_mm * RES)))
    h = max(8, int(round(im.height * w / im.width)))
    arr = np.asarray(im.resize((w, h), Image.LANCZOS))
    pal = pal or palette(im)
    labels = snap(arr, pal)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    masks = []
    for i in range(len(pal)):
        m = (labels == i).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        for j in range(1, n):
            if stats[j, cv2.CC_STAT_AREA] < min_area_mm2 * RES * RES:
                m[lab == j] = 0
        masks.append(m.astype(bool))
    return pal, masks, (w / RES, h / RES), labels
