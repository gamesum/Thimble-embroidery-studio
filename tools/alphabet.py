"""Render the alphabet large through the full pipeline for close inspection of every letter."""
import io, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PIL import Image
from engine import design

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "tests")
font = sys.argv[1] if len(sys.argv) > 1 else "Montserrat Bold"
lines = sys.argv[2:] or ["ABCDEFG", "HIJKLMN", "OPQRSTU", "VWXYZ&", "abcdefgh", "ijklmnop", "qrstuvwxyz"]
tag = font.replace(" ", "")
t = time.time()
for n, text in enumerate(lines):
    L = {"hoop": [130, 22], "fabric": "knit",
         "elements": [{"type": "text", "text": text, "font": font, "height_mm": 14, "letter_spacing": 0.12, "color": "#f4f1ea", "x": 0, "y": 0}]}
    blocks, _, P = design.build(L)
    im = Image.open(io.BytesIO(design.render_preview(blocks, L["hoop"], scale=12)))
    bg = Image.new("RGBA", im.size, (48, 48, 54, 255))
    bg.alpha_composite(im)
    bg.convert("RGB").save(os.path.join(OUT, "abc_%s_%d.png" % (tag, n)))
print("done %.1fs" % (time.time() - t))
