"""Engine smoke test: build sample layouts, time them, save previews on a fabric background."""
import io, os, shutil, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PIL import Image
from engine import design

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "tests")
os.makedirs(OUT, exist_ok=True)
os.makedirs(design.IMAGE_DIR, exist_ok=True)

LIMITED = {"hoop": [100, 100], "fabric": "fleece", "elements": [
    {"type": "shape", "kind": "offset_frame", "width_mm": 86, "height_mm": 40, "stroke_mm": 1.0, "color": "#9a6b3f", "x": 0, "y": 2},
    {"type": "text", "text": "LIMITED", "font": "Alfa Slab One", "height_mm": 12, "letter_spacing": 0.04,
     "color": "#b98a5a", "outline": {"color": "#6b4423", "width_mm": 0.9}, "x": 0, "y": -5},
    {"type": "text", "text": "edition", "font": "Yellowtail", "height_mm": 12, "color": "#1d1d1d", "x": 6, "y": 13},
    {"type": "shape", "kind": "heart", "width_mm": 6, "height_mm": 5.5, "color": "#d62839", "x": 0, "y": -24},
]}

AG = {"hoop": [100, 100], "fabric": "knit", "elements": [
    {"type": "image", "image_id": "AG_PERMANENT_LIGHTING.png", "width_mm": 94, "x": 0, "y": 0}]}


def fabric_bg(size, rgb=(222, 220, 214)):
    import numpy as np
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 6, (size[1], size[0], 1))
    arr = np.clip(np.array(rgb, float)[None, None, :] + noise, 0, 255).astype("uint8")
    return Image.fromarray(arr, "RGB").convert("RGBA")


def run(name, layout, fabric_rgb=(222, 220, 214)):
    t = time.time()
    blocks, boxes, P = design.build(layout)
    pat = design.to_pattern(blocks, P)
    st = design.stats(blocks, layout, pat)
    png = design.render_preview(blocks, layout["hoop"], scale=10)
    dt = time.time() - t
    im = Image.open(io.BytesIO(png))
    bg = fabric_bg(im.size, fabric_rgb)
    bg.alpha_composite(im)
    bg.convert("RGB").save(os.path.join(OUT, name + ".png"))
    print(name, "%.1fs" % dt, st)
    return blocks, pat, P


if __name__ == "__main__":
    src = r"C:\Users\18018\Desktop\RIDGELINE\AG_PERMANENT_LIGHTING.png"
    if os.path.exists(src):
        shutil.copy(src, os.path.join(design.IMAGE_DIR, "AG_PERMANENT_LIGHTING.png"))
    which = sys.argv[1:] or ["limited", "ag"]
    if "limited" in which:
        run("limited", LIMITED)
    if "ag" in which:
        run("ag", AG, (40, 40, 44))
