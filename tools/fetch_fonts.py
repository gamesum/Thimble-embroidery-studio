"""Download the curated Google Fonts library (all SIL Open Font License) as static TTFs.

Uses the Google Fonts CSS API with a legacy user agent, which returns plain TrueType URLs.
"""
import json, os, re, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "..", "fonts")

# (display name, Google family, weight, category)
FONTS = [
    # Block & sans â€” the everyday workhorses of apparel embroidery
    ("Montserrat Bold", "Montserrat", 700, "Block"),
    ("Montserrat Black", "Montserrat", 900, "Block"),
    ("Poppins SemiBold", "Poppins", 600, "Block"),
    ("Oswald", "Oswald", 600, "Block"),
    ("Bebas Neue", "Bebas Neue", 400, "Block"),
    ("Anton", "Anton", 400, "Block"),
    ("Archivo Black", "Archivo Black", 400, "Block"),
    ("League Spartan Bold", "League Spartan", 700, "Block"),
    ("Raleway Bold", "Raleway", 700, "Block"),
    ("Roboto Bold", "Roboto", 700, "Block"),
    ("Open Sans Bold", "Open Sans", 700, "Block"),
    ("Lato Bold", "Lato", 700, "Block"),
    ("Work Sans Bold", "Work Sans", 700, "Block"),
    ("Nunito Bold", "Nunito", 700, "Rounded"),
    ("Quicksand Bold", "Quicksand", 700, "Rounded"),
    ("Fredoka SemiBold", "Fredoka", 600, "Rounded"),
    ("Varela Round", "Varela Round", 400, "Rounded"),
    ("Rubik Bold", "Rubik", 700, "Rounded"),
    ("Righteous", "Righteous", 400, "Display"),
    ("Russo One", "Russo One", 400, "Display"),
    ("Teko SemiBold", "Teko", 600, "Block"),
    ("Barlow Condensed Bold", "Barlow Condensed", 700, "Block"),
    ("Josefin Sans Bold", "Josefin Sans", 700, "Block"),
    ("Kanit Bold", "Kanit", 700, "Block"),
    ("Bungee", "Bungee", 400, "Display"),
    # Varsity / sport
    ("Graduate", "Graduate", 400, "Varsity"),
    ("Black Ops One", "Black Ops One", 400, "Varsity"),
    ("Alfa Slab One", "Alfa Slab One", 400, "Varsity"),
    ("Bowlby One SC", "Bowlby One SC", 400, "Varsity"),
    ("Staatliches", "Staatliches", 400, "Varsity"),
    # Serif & slab
    ("Playfair Display Bold", "Playfair Display", 700, "Serif"),
    ("Merriweather Bold", "Merriweather", 700, "Serif"),
    ("Lora Bold", "Lora", 700, "Serif"),
    ("Cinzel Bold", "Cinzel", 700, "Serif"),
    ("Abril Fatface", "Abril Fatface", 400, "Serif"),
    ("DM Serif Display", "DM Serif Display", 400, "Serif"),
    ("Roboto Slab Bold", "Roboto Slab", 700, "Serif"),
    ("Arvo Bold", "Arvo", 700, "Serif"),
    ("Bitter Bold", "Bitter", 700, "Serif"),
    ("Zilla Slab Bold", "Zilla Slab", 700, "Serif"),
    ("Cormorant Garamond Bold", "Cormorant Garamond", 700, "Serif"),
    ("Libre Baskerville Bold", "Libre Baskerville", 700, "Serif"),
    ("Rye", "Rye", 400, "Western"),
    ("Ultra", "Ultra", 400, "Serif"),
    ("Old Standard TT Bold", "Old Standard TT", 700, "Serif"),
    # Script â€” monograms, "edition"-style swashes
    ("Great Vibes", "Great Vibes", 400, "Script"),
    ("Dancing Script Bold", "Dancing Script", 700, "Script"),
    ("Pacifico", "Pacifico", 400, "Script"),
    ("Satisfy", "Satisfy", 400, "Script"),
    ("Sacramento", "Sacramento", 400, "Script"),
    ("Allura", "Allura", 400, "Script"),
    ("Alex Brush", "Alex Brush", 400, "Script"),
    ("Parisienne", "Parisienne", 400, "Script"),
    ("Pinyon Script", "Pinyon Script", 400, "Script"),
    ("Kaushan Script", "Kaushan Script", 400, "Script"),
    ("Yellowtail", "Yellowtail", 400, "Script"),
    ("Lobster", "Lobster", 400, "Script"),
    ("Cookie", "Cookie", 400, "Script"),
    ("Courgette", "Courgette", 400, "Script"),
    ("Tangerine Bold", "Tangerine", 700, "Script"),
    ("Italianno", "Italianno", 400, "Script"),
    ("Marck Script", "Marck Script", 400, "Script"),
    ("Mr Dafoe", "Mr Dafoe", 400, "Script"),
    ("Arizonia", "Arizonia", 400, "Script"),
    ("Petit Formal Script", "Petit Formal Script", 400, "Script"),
    ("Clicker Script", "Clicker Script", 400, "Script"),
    ("Norican", "Norican", 400, "Script"),
    ("Grand Hotel", "Grand Hotel", 400, "Script"),
    ("Damion", "Damion", 400, "Script"),
    ("Rochester", "Rochester", 400, "Script"),
    # Blackletter â€” streetwear staple
    ("UnifrakturCook", "UnifrakturCook", 700, "Blackletter"),
    ("UnifrakturMaguntia", "UnifrakturMaguntia", 400, "Blackletter"),
    ("Pirata One", "Pirata One", 400, "Blackletter"),
    ("Grenze Gotisch Bold", "Grenze Gotisch", 700, "Blackletter"),
    ("Jacquard 24", "Jacquard 24", 400, "Blackletter"),
    # Handwritten & fun
    ("Permanent Marker", "Permanent Marker", 400, "Handwritten"),
    ("Caveat Bold", "Caveat", 700, "Handwritten"),
    ("Amatic SC Bold", "Amatic SC", 700, "Handwritten"),
    ("Patrick Hand", "Patrick Hand", 400, "Handwritten"),
    ("Indie Flower", "Indie Flower", 400, "Handwritten"),
    ("Shadows Into Light", "Shadows Into Light", 400, "Handwritten"),
    ("Gloria Hallelujah", "Gloria Hallelujah", 400, "Handwritten"),
    ("Bangers", "Bangers", 400, "Display"),
    ("Luckiest Guy", "Luckiest Guy", 400, "Display"),
    ("Chewy", "Chewy", 400, "Display"),
    ("Special Elite", "Special Elite", 400, "Display"),
]

UA = "Wget/1.21"  # a plain UA makes the CSS API return .ttf URLs


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main():
    os.makedirs(FONT_DIR, exist_ok=True)
    manifest, failed = [], []
    for name, family, weight, cat in FONTS:
        fname = re.sub(r"[^A-Za-z0-9]+", "", name) + ".ttf"
        path = os.path.join(FONT_DIR, fname)
        try:
            if not os.path.exists(path):
                css = fetch("https://fonts.googleapis.com/css2?family=%s:wght@%d" % (family.replace(" ", "+"), weight)).decode()
                url = re.search(r"url\((https://[^)]+\.ttf)\)", css).group(1)
                open(path, "wb").write(fetch(url))
                time.sleep(0.2)
            manifest.append({"name": name, "family": family, "weight": weight, "category": cat, "file": fname})
        except Exception as e:  # keep going; report at the end
            failed.append((name, str(e)[:80]))
    json.dump(manifest, open(os.path.join(FONT_DIR, "fonts.json"), "w"), indent=1)
    print("fonts ok:", len(manifest), "failed:", failed)


if __name__ == "__main__":
    main()

