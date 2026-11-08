"""Generate the favicon files from static/favicon.svg (and the plain mark).

Writes, all committed:
  static/favicon.ico            16, 32, 48 in one file
  static/apple-touch-icon.png   180x180 on a solid warm tile
  static/icon-192.png, static/icon-512.png
  static/site.webmanifest

The SVG is currentColor, so each render sets the ink and the paper for the theme the file is used
in. apple-touch-icon and the manifest icons sit on a solid warm background so they never become
transparent.

Run: .venv/bin/python scripts/make_icons.py
"""
import io
import json
from pathlib import Path

from PIL import Image
from cairosvg import svg2png

PROJECT = Path(__file__).resolve().parent.parent
STATIC = PROJECT / "static"

INK = "#1f1e1d"
PAPER = "#faf9f5"          # warm cream, the app's light background
ACCENT = "#d97757"
SIZES = [16, 32, 48, 180, 192, 512]


def render(svg_path, size, ink, paper):
    text = svg_path.read_text(encoding="utf-8").replace("<svg ", f'<svg style="color:{ink}" ', 1)
    png = svg2png(bytestring=text.encode(), output_width=size, output_height=size,
                  background_color=paper)
    return Image.open(io.BytesIO(png)).convert("RGB")


def main():
    mark = STATIC / "logo-mark.svg"
    tile = STATIC / "favicon.svg"
    if not tile.exists():
        raise SystemExit("run scripts/make_logo.py first")

    # The favicon file itself is the SVG, which the browser can render in both themes.
    for size in SIZES:
        source = tile if size < 180 else tile
        image = render(source, size, INK, PAPER)
        if size == 180:
            image.save(STATIC / "apple-touch-icon.png")
        elif size in (192, 512):
            image.save(STATIC / f"icon-{size}.png")

    sizes = [(16, 16), (32, 32), (48, 48)]
    base = render(tile, 48, INK, PAPER)
    base.save(STATIC / "favicon.ico", format="ICO", sizes=sizes)

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
