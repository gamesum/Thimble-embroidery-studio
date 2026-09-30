"""Render sample words in several fonts through the full pipeline (visual QA)."""
import io, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PIL import Image, ImageDraw
from engine import design

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "tests")
FONTS = sys.argv[1:] or ["Montserrat Bold", "Alfa Slab One", "Yellowtail", "Dancing Script Bold",
                         "Pacifico", "Graduate", "UnifrakturCook", "Playfair Display Bold"]

rows = []
for f in FONTS:
    L = {"hoop": [100, 26], "fabric": "knit",
         "elements": [{"type": "text", "text": "Rake & Mow" if "Script" not in f and f not in ("Yellowtail", "Pacifico") else "edition",
                       "font": f, "height_mm": 11, "color": "#2b4c7e", "x": 0, "y": 0}]}
    t = time.time()
    blocks, _, P = design.build(L)
    im = Image.open(io.BytesIO(design.render_preview(blocks, L["hoop"], scale=9)))
    bg = Image.new("RGBA", im.size, (238, 234, 226, 255))
    bg.alpha_composite(im)
    ImageDraw.Draw(bg).text((6, 4), "%s  (%.1fs)" % (f, time.time() - t), fill=(90, 80, 70, 255))
    rows.append(bg)
W = max(r.width for r in rows)
sheet = Image.new("RGB", (W, sum(r.height for r in rows)), "white")
y = 0
for r in rows:
    sheet.paste(r.convert("RGB"), (0, y))
    y += r.height
sheet.save(os.path.join(OUT, "font_sheet.png"))
print("saved", len(rows))
