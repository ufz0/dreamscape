"""Generate the favicon files from static/favicon.svg (and the plain mark).

Writes, all committed:
  static/favicon.ico            16, 32, 48 in one file
  static/apple-touch-icon.png   180x180
  static/icon-192.png, static/icon-512.png
  static/site.webmanifest

The SVG is currentColor, so a browser can render it in either theme. A raster cannot, so the
rasters are transparent and drawn in a middle tone that reads on both a light and a dark tab bar:
the neutral parts of the mark use NEUTRAL, the terracotta accent stays. The one exception is the
apple-touch-icon, which a phone puts on a home screen tile of its own choosing, so it keeps a flat
warm background.

Run: .venv/bin/python scripts/make_icons.py
"""
import io
import json
from pathlib import Path

from PIL import Image
from cairosvg import svg2png

PROJECT = Path(__file__).resolve().parent.parent
STATIC = PROJECT / "static"

# The app's theme inks, used by the SVG itself through prefers-color-scheme.
INK_LIGHT = "#1f1e1d"
INK_DARK = "#ecebe4"
# A middle tone between the two: for the rasters, which are one colour and have to work either way.
NEUTRAL = "#7d7b73"
PAPER = "#faf9f5"          # warm cream, the app's light background
ACCENT = "#d97757"
SIZES = [16, 32, 48, 180, 192, 512]
# Only the apple-touch-icon keeps a background; everything else is transparent.
BACKGROUND_SIZES = {180: PAPER}


def render(svg_path, size, ink, paper=None):
    """One size. `paper=None` leaves the image transparent."""
    text = svg_path.read_text(encoding="utf-8").replace("<svg ", f'<svg style="color:{ink}" ', 1)
    png = svg2png(bytestring=text.encode(), output_width=size, output_height=size,
                  background_color=paper)
    image = Image.open(io.BytesIO(png))
    return image.convert("RGBA") if paper is None else image.convert("RGB")


def main():
    mark = STATIC / "logo-mark.svg"
    tile = STATIC / "favicon.svg"
    if not tile.exists():
        raise SystemExit("run scripts/make_logo.py first")

    for size in SIZES:
        image = render(tile, size, NEUTRAL, BACKGROUND_SIZES.get(size))
        if size == 180:
            image.save(STATIC / "apple-touch-icon.png")
        elif size in (192, 512):
            image.save(STATIC / f"icon-{size}.png")

    # An ICO carries the sizes in one file; transparency needs the alpha channel.
    base = render(tile, 48, NEUTRAL)
    base.save(STATIC / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])

    manifest = {
        "name": "dreamscape",
        "short_name": "dreamscape",
        "description": "A chat app whose memory is a wiki it rebuilds while dreaming.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": PAPER,
        "theme_color": PAPER,
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    (STATIC / "site.webmanifest").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print("wrote:", ", ".join(sorted(p.name for p in STATIC.iterdir()
                                     if p.suffix in (".ico", ".png") or p.name == "site.webmanifest")))


main()
