# Thimble — Embroidery Studio

Turn words, simple shapes and logos into clean machine-embroidery files, and write them
straight to a Husqvarna Viking **Designer I** floppy disk. Runs entirely on your own computer
in the browser; only the optional "Read a picture" feature talks to the internet (Anthropic API).

## Setup (Windows)

1. Install Python 3.12 or newer.
2. In this folder:
   ```
   python -m venv venv
   venv\Scripts\pip install -r requirements.txt
   ```
3. **Designer I disks only:** copy `MENU_SEL.PHV` and `MENU_01\MENU_01.MHV` from any Designer I
   floppy into the `templates` folder (as `templates\MENU_SEL.PHV` and `templates\MENU_01.MHV`).
   They are the machine's menu layout and are not included here because they come from
   Husqvarna's disks. Everything else works without them.
4. Double-click **`Start Thimble.bat`**. Your browser opens at http://127.0.0.1:5311.
   Keep the black window open while you work; close it to quit.

## What it does

- **Words** — hand-digitized embroidery fonts (marked ✦) with the stitch direction and
  sewing order set by hand for every letter, plus ~85 regular fonts that are auto-digitized.
  Curves, letter spacing, per-letter colors, a satin border (sewn first so there are no jump
  threads to snip inside letters, or last for the crispest edge), multi-line alignment.
- **Shapes** — heart, star, circle, ring, block, frames, line.
- **Pictures** — flat artwork is split into thread colors and traced; wide areas become
  tatami fill with crisp edges, narrow strokes become satin.
- **Read a picture (AI)** — Claude reads the words, fonts, colors and artwork in a picture and
  rebuilds them: text is re-set in embroidery fonts, and logo artwork is redrawn as clean
  shapes (fills, lines, dots) instead of traced pixels. *Creative* mode evens things out like a
  digitizer (symmetric arcs, evenly spaced lights, centered rows); *Exact copy* keeps the
  picture's layout. A refine box sends follow-up instructions. Needs an Anthropic API key
  (Settings); a few cents per picture.
- **Threads** — thread chart with stitch counts and thread length; click spools to see where
  they sew and merge them; *Simplify colors* merges look-alike threads.
- **Sew it out** — replay the stitch order at real machine speed or faster.
- **Save file** — VP3 (Husqvarna/Pfaff), PES (Brother), DST (Tajima), JEF (Janome), EXP, SHV,
  or a zipped Designer I floppy layout. Designs that go off the hoop are never exported.
- **Write to Designer I disk** — adds the design to the next free slot of Menu 1 (up to 36)
  and reads every file back to check it.

## Stitch settings

Based on Ink/Stitch and published digitizing guidance: satin at 0.4 mm spacing on columns up
to ~7 mm (wider areas become fill), center-walk / zigzag underlay, pull compensation and
density per fabric (Settings → Fabric: woven, knit, fleece, towel, cap). Lettering under
4–5 mm fills in — the ✦ *Ink/Stitch Small Font* is digitized for small text.

## Files

- `app.py` — local web server
- `engine/` — stitch engine (`stitchgen.py`), embroidery-font renderer (`embfont.py`),
  layout builder (`design.py`), Designer I disk writer (`d1disk.py`), AI picture reader (`ai.py`)
- `fonts/` — Google Fonts (SIL Open Font License)
- `fonts_emb/` — embroidery fonts from the Ink/Stitch project
  (https://github.com/inkstitch/embroidery-fonts); each font's license is in its `font.json`
  (SIL Open Font License or CC BY-SA)
- `static/` — the browser app
- `templates/` — Designer I menu templates (not included; see Setup)
- `tools/` — tests and helpers (`font_qa.py` checks every embroidery font)
- `config.json` (not in the repo) — your API key; `output/` (not in the repo) — your
  uploads, exports and saved projects
