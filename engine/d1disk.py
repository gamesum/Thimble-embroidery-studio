"""Husqvarna Viking Designer I disk writer (menu layout + SHV designs).

Layout reverse-engineered from a factory Designer I disk (297 SHV files, 9 MHV, 1 PHV) and
confirmed on a real machine:
  MENU_SEL.PHV              menu-selection screen (titles, group labels, button table, pixmap)
  MENU_01/MENU_01.MHV       6x6 design grid for menu 1 (labels, slot table, pixmap)
  MENU_01/DES01_nn.SHV      the designs
All multi-byte numbers are big-endian; distances are 0.1 mm; y grows downward.
The machine places the SHV origin at the hoop centre.
"""
import math, os, shutil, struct
import pyembroidery as pe
from pyembroidery.EmbThreadShv import get_thread_set

SIG = b"Embroidery disk created using software licensed from Viking Sewing Machines AB, Sweden"
assert len(SIG) == 86
MAX_STEP = 127  # signed 8-bit delta; -128 (0x80) is the escape byte
TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def pack_pixmap(grid, lines, pixels):
    """grid[line][pixel] -> 4-bit values, two pixels per byte, high nibble first."""
    out = bytearray()
    for ln in range(lines):
        row = grid[ln]
        for i in range(0, pixels, 2):
            hi = row[i]
            lo = row[i + 1] if i + 1 < pixels else 0
            out.append((hi << 4) | lo)
    return bytes(out)


def unpack_pixmap(data, lines, pixels):
    bpl = math.ceil(pixels / 2)
    return [[(data[ln * bpl + p // 2] >> 4) if p % 2 == 0 else (data[ln * bpl + p // 2] & 15)
             for p in range(pixels)] for ln in range(lines)]


def draw_line(grid, a, b, value):
    (l0, p0), (l1, p1) = a, b
    n = max(abs(l1 - l0), abs(p1 - p0), 1)
    for i in range(n + 1):
        grid[round(l0 + (l1 - l0) * i / n)][round(p0 + (p1 - p0) * i / n)] = value


def build_segments(pattern, origin=None):
    """Return (origin, colors) where colors = [[('stitch'|'jump', x, y), ...], ...] in absolute 0.1 mm.
    origin None = centre of the design; otherwise the given point (hoop centre = (0, 0))."""
    colors, cur, pending_jump = [], [], False
    for x, y, cmd in pattern.stitches:
        x, y = round(x), round(y)
        cmd &= pe.COMMAND_MASK
        if cmd == pe.STITCH:
            cur.append(("jump" if pending_jump else "stitch", x, y))
            pending_jump = False
        elif cmd in (pe.JUMP, pe.TRIM):
            pending_jump = True
        elif cmd == pe.COLOR_CHANGE:
            if cur:
                colors.append(cur)
            cur, pending_jump = [], True
        elif cmd == pe.END:
            break
    if cur:
        colors.append(cur)
    if not colors:
        raise ValueError("the design has no stitches")
    if origin is None:
        xs = [x for seg in colors for _, x, _ in seg]
        ys = [y for seg in colors for _, _, y in seg]
        origin = ((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2)
    colors[0][0] = ("jump",) + colors[0][0][1:]
    return origin, colors


def encode_stitches(origin, colors):
    """Encode to SHV records. Returns (bytes, per-color record counts, color start positions, path)."""
    out, counts, starts, path = bytearray(), [], [], []
    px, py = origin
    for seg in colors:
        starts.append((px - origin[0], py - origin[1]))
        n = 0
        for kind, x, y in seg:
            dx, dy = x - px, y - py
            if kind == "jump" and (dx or dy):
                out += b"\x80\x01" + struct.pack(">hh", dx, dy) + b"\x80\x02"
                n += 4  # 0x80 0x01 counts 3, 0x80 0x02 counts 1 (matches factory headers)
                path.append(("jump", x - origin[0], y - origin[1]))
                px, py = x, y
                continue
            steps = max(1, math.ceil(max(abs(dx), abs(dy)) / MAX_STEP))
            for i in range(1, steps + 1):
                nx = px + round(dx * i / steps) - round(dx * (i - 1) / steps)
                ny = py + round(dy * i / steps) - round(dy * (i - 1) / steps)
                out += struct.pack(">bb", nx - px, ny - py)
                n += 1
                px, py = nx, ny
                path.append(("stitch", px - origin[0], py - origin[1]))
        counts.append(n)
    return bytes(out), counts, starts, path


def thumbnail(path, bounds):
    """SHV preview: one line per mm of x, one pixel per mm of (reversed) y, value 3 = stitched."""
    x0, x1, y0, y1 = bounds
    lines, pixels = (x1 - x0) // 10 + 2, (y1 - y0) // 10 + 2
    to = lambda x, y: (round((x - x0) / 10), round((y1 - y) / 10))
    grid = [[0] * pixels for _ in range(lines)]
    prev = to(0, 0)
    for kind, x, y in path:
        p = to(x, y)
        if kind == "stitch":
            draw_line(grid, prev, p, 3)
        prev = p
    return grid, lines, pixels, to


def size_class_for(span_mm):
    """Factory files use 0x10 / 0x30 / 0x50 for small / medium / large designs."""
    return 0x10 if span_mm < 30 else 0x30 if span_mm < 55 else 0x50


def write_shv(pattern, name, origin=None):
    """Encode a pattern as SHV bytes. Returns (bytes, thumbnail grid, info)."""
    origin_units = None if origin is None else (round(origin[0]), round(origin[1]))
    origin, colors = build_segments(pattern, origin_units)
    data, counts, starts, path = encode_stitches(origin, colors)
    xs = [0] + [x for _, x, _ in path]
    ys = [0] + [y for _, _, y in path]
    bounds = (min(xs), max(xs), min(ys), max(ys))
    grid, lines, pixels, to = thumbnail(path, bounds)
    if lines >= 256 or pixels >= 256:
        raise ValueError("design is larger than the Designer I preview allows (about 250 mm)")
    start_mark, end_mark = to(0, 0), to(path[-1][1], path[-1][2])

    threads = get_thread_set()

    def nearest(t):
        c = t.color
        rgb = ((c >> 16) & 255, (c >> 8) & 255, c & 255)
        return min(range(len(threads)), key=lambda i: sum(
            (a - b) ** 2 for a, b in zip(rgb, ((threads[i].color >> 16) & 255, (threads[i].color >> 8) & 255, threads[i].color & 255))))

    nm = name.upper().encode("ascii", "replace")[:12]
    out = bytearray(SIG) + bytes([len(nm)]) + nm
    out += bytes([lines, pixels, start_mark[0], start_mark[1], end_mark[0], end_mark[1]])
    out += pack_pixmap(grid, lines, pixels)
    x0, x1, y0, y1 = bounds
    out += bytes([len(colors), 0xC4, 0x28, 0x00, size_class_for(max(x1 - x0, y1 - y0) / 10), 0x00, 0x00])
    out += struct.pack(">hhhhI", x1, -y0, x0, -y1, sum(counts))
    for i, (n, (sx, sy)) in enumerate(zip(counts, starts)):
        idx = nearest(pattern.threadlist[i]) if i < len(pattern.threadlist) else 1
        out += struct.pack(">IB", n, idx) + b"\x02\x00\x00" + b"\x00\x00" + struct.pack(">hh", sx, sy)
    out += data
    info = dict(bounds=bounds, counts=counts, lines=lines, pixels=pixels, colors=len(colors))
    return bytes(out), grid, info


# ---- MHV: 6x6 design grid. The screen is stored rotated 90 degrees: pixmap "lines" run along the
# screen's x axis. Slot k (0-based) lives in pixmap cell row k % 6, column 5 - k // 6.

def _cell_bands(grid, lines, pixels):
    rows = [l for l in range(lines) if sum(1 for v in grid[l] if v == 11) > pixels * 0.8]
    cols = [p for p in range(pixels) if sum(1 for l in range(lines) if grid[l][p] == 11) > lines * 0.8]

    def bands(idx, n):
        cuts = [-1] + idx + [n]
        return [(a + 1, b) for a, b in zip(cuts, cuts[1:]) if b - a > 4]
    return bands(rows, lines), bands(cols, pixels)


def mhv_blank(template, label):
    d = bytearray(template)
    lab = label.upper().encode("ascii", "replace")[:11].ljust(12, b"\x00")
    for i in range(16):
        d[0x56 + 12 * i: 0x56 + 12 * (i + 1)] = lab
    d[0x119:0x119 + 36] = bytes([0x80 + i for i in range(36)])
    lines, pixels = d[0x13D], d[0x13E]
    grid = unpack_pixmap(d[0x13F:], lines, pixels)
    grid = [[v if v == 11 else 0 for v in row] for row in grid]  # keep only the grid lines
    d[0x13F:] = pack_pixmap(grid, lines, pixels)
    return bytes(d)


def mhv_used_slots(mhv):
    return sum(1 for b in mhv[0x119:0x119 + 36] if b < 0x80)


def mhv_put(mhv, slot, thumb):
    """Draw a design thumbnail into slot (0-based) and mark slots up to it as used."""
    d = bytearray(mhv)
    lines, pixels = d[0x13D], d[0x13E]
    grid = unpack_pixmap(d[0x13F:], lines, pixels)
    row_bands, col_bands = _cell_bands(grid, lines, pixels)
    (l0, l1), (p0, p1) = row_bands[slot % 6], col_bands[5 - slot // 6]
    l0, l1, p0, p1 = l0 + 2, l1 - 2, p0 + 2, p1 - 2
    tg, tl, tp = thumb
    s = max(tl / max(1, l1 - l0), tp / max(1, p1 - p0))
    offl, offp = (l1 - l0 - tl / s) / 2, (p1 - p0 - tp / s) / 2
    k = max(1, math.ceil(s))
    for ln in range(l0, l1):
        for p in range(p0, p1):
            grid[ln][p] = 0
            sl, sp = int((ln - l0 - offl) * s), int((p - p0 - offp) * s)
            if 0 <= sl < tl and 0 <= sp < tp and any(
                    tg[min(tl - 1, sl + a)][min(tp - 1, sp + b)] for a in range(k) for b in range(k)):
                grid[ln][p] = 3
    d[0x13F:] = pack_pixmap(grid, lines, pixels)
    used = max(mhv_used_slots(mhv), slot + 1)
    d[0x119:0x119 + 36] = bytes(list(range(1, used + 1)) + [0x80 + i for i in range(36 - used)])
    return bytes(d)


def write_phv(template, title, group_label):
    d = bytearray(template)
    t = title.upper().encode("ascii", "replace")[:23].ljust(24, b"\x00")
    for i in range(16):
        d[0x5A + 24 * i: 0x5A + 24 * (i + 1)] = t
    for g, start in enumerate([0x1DE, 0x2A0, 0x362, 0x424]):  # 16 x 12-byte labels per group button
        lab = (group_label if g == 0 else "").upper().encode("ascii", "replace")[:11].ljust(12, b"\x00")
        for i in range(16):
            d[start + 12 * i: start + 12 * (i + 1)] = lab
    d[0x4E4] = 1  # number of menus on the disk
    for e in range(4):  # button table: x, y, w, h, first menu of group
        d[0x4E5 + 5 * e + 4] = 1 if e == 0 else 0
    assert len(d) == len(template)
    return bytes(d)


def disk_status(target):
    """What is on a Designer I disk/folder now."""
    mhv_path = os.path.join(target, "MENU_01", "MENU_01.MHV")
    if not os.path.exists(os.path.join(target, "MENU_SEL.PHV")) or not os.path.exists(mhv_path):
        return dict(layout=False, used=0, free=36)
    used = mhv_used_slots(open(mhv_path, "rb").read())
    return dict(layout=True, used=used, free=36 - used)


def _templates(target):
    """(MENU_01.MHV, MENU_SEL.PHV) bytes to start a fresh menu from: the templates folder, or else
    the menu files already on this disk (any Designer I disk with a menu will do)."""
    for base, mhv_rel in ((TEMPLATES, "MENU_01.MHV"), (target, os.path.join("MENU_01", "MENU_01.MHV"))):
        m, p = os.path.join(base, mhv_rel), os.path.join(base, "MENU_SEL.PHV")
        if os.path.exists(m) and os.path.exists(p):
            return open(m, "rb").read(), open(p, "rb").read()
    raise ValueError("To start a new menu, Thimble needs the Designer I menu files. Use a disk that already has a "
                     "Designer I menu (any factory disk works), or copy MENU_SEL.PHV and MENU_01.MHV from one into "
                     "the templates folder.")


def write_disk(pattern, target, name, disk_label="MY DESIGNS", mode="add", origin=(0, 0)):
    """Write a design to a Designer I disk (or folder). mode 'add' uses the next free slot of
    Menu 1 (starting a menu if the disk has none); 'replace' starts a fresh menu.
    Every file written is read back and compared. Returns dict(slot=1-based, files, info)."""
    shv, grid, info = write_shv(pattern, name, origin)
    thumb = (grid, info["lines"], info["pixels"])
    st = disk_status(target)
    menu_dir = os.path.join(target, "MENU_01")
    if mode == "add" and st["layout"]:
        if st["free"] <= 0:
            raise ValueError("Menu 1 on this disk is full (36 designs). Use 'Start fresh'.")
        slot = st["used"]
        mhv = open(os.path.join(menu_dir, "MENU_01.MHV"), "rb").read()
        phv = None
    else:
        slot = 0
        tmhv, tphv = _templates(target)
        mhv = mhv_blank(tmhv, disk_label)
        phv = write_phv(tphv, disk_label, disk_label)
    mhv = mhv_put(mhv, slot, thumb)
    files = {}
    if phv is not None:
        files[os.path.join(target, "MENU_SEL.PHV")] = phv
    files[os.path.join(menu_dir, "MENU_01.MHV")] = mhv
    files[os.path.join(menu_dir, "DES01_%02d.SHV" % (slot + 1))] = shv
    if os.path.isdir(target):
        existing = sum(os.path.getsize(p) for p in files if os.path.exists(p))
        if mode == "replace" and os.path.isdir(menu_dir):
            existing += sum(os.path.getsize(os.path.join(menu_dir, f)) for f in os.listdir(menu_dir)
                            if f.upper().startswith("DES01_") and f.upper().endswith(".SHV"))
        need = sum(len(b) for b in files.values()) - existing
        free = shutil.disk_usage(target).free
        if need > free:
            raise ValueError("Not enough space on the disk (%d KB free, %d KB needed)." % (free // 1024, need // 1024))
    os.makedirs(menu_dir, exist_ok=True)
    if mode == "replace":
        for f in os.listdir(menu_dir):
            if f.upper().startswith("DES01_") and f.upper().endswith(".SHV"):
                os.remove(os.path.join(menu_dir, f))
    for p, data in files.items():
        with open(p, "wb") as fh:
            fh.write(data)
    for p, data in files.items():  # verify by reading back
        with open(p, "rb") as fh:
            if fh.read() != data:
                raise IOError("Verification failed for " + p)
    return dict(slot=slot + 1, files=[os.path.relpath(p, target) for p in files], info=info)
