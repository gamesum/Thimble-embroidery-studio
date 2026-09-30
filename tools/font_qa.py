"""Quality gate for the hand-digitized fonts: build a test phrase in every font and hide the
ones that fail (missing letters, broken metrics/scale, runaway stitch counts, too slow).
Writes fonts_emb/_meta/qa.json, which embfont.catalogue() uses to filter the picker."""
import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from engine import embfont
from engine.params import params_for

P = params_for("knit")
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
out = {}
for fam in embfont.catalogue(qa=False):
    d, why = fam["dir"], []
    try:
        f = embfont.EmbFont.load(d)
        miss = [c for c in LETTERS if f.glyph_for(c) is None]
        if miss:
            why.append("missing letters " + "".join(miss[:10]))
        else:
            lo, hi = embfont.size_range(fam)
            cap = min(max(12.0, lo), hi)
            t = time.time()
            objs, kinds, (w, h), _, _, _ = embfont.layout_text({"text": "Your words", "height_mm": cap}, fam, P)
            dt = time.time() - t
            n = sum(len(o) for o in objs)
            if not objs:
                why.append("no stitches")
            if not 3.0 <= w / cap <= 9.5:
                why.append("width %.1f x cap (bad spacing/scale)" % (w / cap))
            if not 0.75 <= h / cap <= 2.6:  # all-caps fonts have no descenders
                why.append("height %.1f x cap (bad scale)" % (h / cap))
            dens = n / max(1.0, w * h)
            if n > 25000 or dens > 12:
                why.append("%d stitches (runaway)" % n)
            if dt > 4:
                why.append("slow %.1fs" % dt)
    except Exception as e:
        why.append("error %r" % e)
    out[d] = dict(ok=not why, why=why)
    print("%-28s %s" % (d, "ok" if not why else "HIDE: " + "; ".join(why)), flush=True)
json.dump(out, open(os.path.join(embfont.ROOT, "_meta", "qa.json"), "w"), indent=1)
print(sum(v["ok"] for v in out.values()), "of", len(out), "fonts pass")
