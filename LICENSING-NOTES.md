# Licensing notes (read before selling Thimble)

Thimble is released under the **GNU GPL v3** (see `LICENSE`), because it uses material from the
open-source [Ink/Stitch](https://inkstitch.org) project, which is GPL v3.

What that allows: anyone may use, change and share Thimble, and you may charge for it (for example
run the website as a paid service or sell copies). What it doesn't allow while the Ink/Stitch parts are
in: making Thimble **closed source**, or selling it under a private license. Every copy you hand out
must come with the source code under the GPL.

## What came from Ink/Stitch

| Where | What | If you ever go closed source |
|---|---|---|
| `fonts_emb/` | The ✦ hand-digitized embroidery fonts, from [inkstitch/embroidery-fonts](https://github.com/inkstitch/embroidery-fonts). Each font's own license is in its `font.json` (`font_license`): 49 SIL Open Font License, 13 Creative Commons BY-SA (commercial use OK with credit; changes must be shared the same way), 2 public domain - and all are listed in the app under Settings → About & licenses. The digitized files are part of the Ink/Stitch project. | Remove the folder (or check each font's license and ask the Ink/Stitch font authors). The TrueType fonts in `fonts/` (Google Fonts, OFL) stay. |
| `engine/embfont.py` | Reads those fonts; its satin routine is a port of Ink/Stitch's satin-column code (marked "port of Ink/Stitch" in the file). | Rewrite the satin part from scratch, or drop the ✦ fonts. |
| `engine/data/threads.json` | The 75 thread-brand colour charts (Madeira, Sulky, Isacord...), converted from Ink/Stitch's `palettes/`. | Replace with charts you compile yourself (thread makers publish their colour cards). |

Written from scratch (not copied, only the general idea is shared): contour fill, satin rows / star
satin, wide-satin lettering, tracing, eyes, everything else in `engine/` and `static/`.

**Keep this table up to date** whenever something else is taken from Ink/Stitch (or any GPL project).

## Other open-source parts (all allow closed-source / commercial use)

Flask (BSD), pyembroidery (MIT), OpenCV (Apache 2.0), NumPy / SciPy / scikit-image / Shapely /
skan (BSD), Pillow (MIT-CMU), pillow-heif (BSD; its libheif is LGPL - fine to use as a library),
svgelements (MIT), anthropic SDK (MIT), Google Fonts in `fonts/` (SIL OFL 1.1).

Not legal advice - if you get serious about selling, have a lawyer look at this list.
