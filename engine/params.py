"""Stitch settings. Values follow Ink/Stitch defaults and published digitizing guidance
(satin 0.4 mm spacing, 1-7 mm column widths, center-walk underlay on small lettering,
more pull compensation and coverage on knits/fleece)."""
from dataclasses import dataclass, replace

RES = 10  # raster pixels per mm; 1 px == 0.1 mm == one embroidery unit


@dataclass(frozen=True)
class Params:
    # satin
    satin_spacing: float = 0.40        # peak-to-peak zigzag spacing, mm
    satin_min_width: float = 1.0       # narrower columns are widened to this
    satin_max_width: float = 7.0       # wider shapes become tatami fill
    run_max_width: float = 0.55        # shapes thinner than this become a run stitch
    pull_comp: float = 0.20            # added to each satin edge, mm
    center_walk_below: float = 3.0     # columns narrower than this get center-walk underlay
    zigzag_underlay_spacing: float = 2.5
    underlay_inset: float = 0.4
    # tatami fill
    fill_spacing: float = 0.40
    fill_length: float = 3.5
    fill_staggers: int = 4
    fill_angle: float = 45.0
    fill_pull_comp: float = 0.15
    fill_underlay_spacing: float = 2.0
    fill_underlay_inset: float = 0.6
    edge_run_inset: float = 0.5
    # hand-digitized embroidery fonts: extra pull compensation on top of the font's own
    # (fonts are digitized for stable fabric), and a centre walk if a column has no underlay
    font_pull_extra: float = 0.05
    font_min_underlay: bool = True
    # running
    run_length: float = 2.5
    travel_length: float = 2.5
    min_stitch: float = 0.3
    tie_length: float = 0.7


FABRICS = {
    "woven": ("Woven / twill / denim", Params(pull_comp=0.12, fill_pull_comp=0.10, fill_spacing=0.42, fill_underlay_spacing=2.5,
                                              font_pull_extra=0.0, font_min_underlay=False)),
    "knit": ("T-shirt / knit", Params()),
    "fleece": ("Sweatshirt / fleece / hoodie", Params(pull_comp=0.30, fill_pull_comp=0.25, fill_spacing=0.36,
                                                     satin_spacing=0.36, fill_underlay_spacing=1.6, center_walk_below=2.2,
                                                     font_pull_extra=0.10)),
    "towel": ("Towel / terry (use topping)", Params(pull_comp=0.35, fill_pull_comp=0.30, fill_spacing=0.34,
                                                   satin_spacing=0.34, fill_underlay_spacing=1.5, font_pull_extra=0.15)),
    "cap": ("Cap / structured", Params(pull_comp=0.25, fill_pull_comp=0.2, fill_spacing=0.40, font_pull_extra=0.08)),
}


DENSITY = {"light": 1.15, "standard": 1.0, "dense": 0.85}  # multiplies stitch spacing


def params_for(fabric="knit", density="standard", **overrides):
    p = FABRICS.get(fabric, FABRICS["knit"])[1]
    f = DENSITY.get(density, 1.0)
    p = replace(p, satin_spacing=round(p.satin_spacing * f, 3), fill_spacing=round(p.fill_spacing * f, 3))
    return replace(p, **{k: v for k, v in overrides.items() if v is not None})
