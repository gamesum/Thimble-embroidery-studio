"""Render sample text in the hand-digitized embroidery fonts (visual QA of engine/embfont.py)."""
import io, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PIL import Image, ImageDraw
from engine import embfont, design
from engine.params import params_for

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "tests")
P = params_for("knit")
cat = {f["dir"]: f for f in embfont.catalogue()}
names = sys.argv[1:] or list(cat)[:8]
text = os.environ.get("SAMPLE", "Rake & Mow")
height = float(os.environ.get("HEIGHT", "11"))
rows = []
for d in names:
    fam = cat.get(d)
    if not fam:
        print("no font", d)
        continue
    t = time.time()
    try:
        objs, kinds, size, f, missing, shapes = embfont.layout_text({"text": text, "height_mm": height}, fam, P)
    except Exception as e:
        import traceback; traceback.print_exc()
        print("FAIL", d, e)
        continue
    blocks = [dict(color="#2b4c7e", objects=objs, kinds=kinds)]
    W = max(40, size[0] + 8)
    im = Image.open(io.BytesIO(design.render_preview(blocks, [W, height * 2.4], scale=9)))
    bg = Image.new("RGBA", im.size, (238, 234, 226, 255))
    bg.alpha_composite(im)
    ImageDraw.Draw(bg).text((6, 4), "%s [%s] cap=%.1fmm  %.1fs %s" % (fam["name"], f.dir, f.cap_mm, time.time() - t,
                                                                   ("missing " + "".join(sorted(missing))) if missing else ""), fill=(90, 80, 70, 255))
    rows.append(bg)
    print(d, "ok", "%.1fs" % (time.time() - t), len(objs), "objects", sum(len(o) for o in objs), "pts")
if rows:
    Wm = max(r.width for r in rows)
    sheet = Image.new("RGB", (Wm, sum(r.height for r in rows)), "white")
    y = 0
    for r in rows:
        sheet.paste(r.convert("RGB"), (0, y))
        y += r.height
    sheet.save(os.path.join(OUT, os.environ.get("OUTNAME", "embfont_sheet.png")))
