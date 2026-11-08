"""Render the logo candidates to PNG at 256px and 16px, on light and dark, to look at them.

The SVGs are currentColor, so the render sets the ink colour for each theme. The 16px output is
what the sidebar actually shows; an 8x nearest-neighbour copy of it is written too so the small
version can be read on screen.

Run: .venv/bin/python design/logo-candidates/render_candidates.py
"""
import io
from pathlib import Path

from PIL import Image
from cairosvg import svg2png

OUT = Path(__file__).resolve().parent

THEMES = {"light": ("#faf9f5", "#1f1e1d"), "dark": ("#262624", "#ecebe4")}
PAPER = {"light": "#ffffff", "dark": "#2f2f2d"}


def render(svg_text, colour, size, background):
    text = svg_text.replace('<svg ', f'<svg style="color:{colour}" ', 1)
    png = svg2png(bytestring=text.encode(), output_width=size, output_height=size,
                  background_color=background)
    return Image.open(io.BytesIO(png)).convert("RGB")


def main():
    names = sorted(p.name for p in OUT.glob("candidate-*.svg"))
    for name in names:
        stem = name.removesuffix(".svg")
        text = (OUT / name).read_text(encoding="utf-8")
        for theme, (background, ink) in THEMES.items():
            big = render(text, ink, 256, PAPER[theme])
            big.save(OUT / f"{stem}-256-{theme}.png")
            small = render(text, ink, 16, PAPER[theme])
            small.save(OUT / f"{stem}-16-{theme}.png")
            zoom = small.resize((128, 128), Image.NEAREST)
            canvas = Image.new("RGB", (148, 148), PAPER[theme])
            canvas.paste(zoom, (10, 10))
            canvas.save(OUT / f"{stem}-16-zoom-{theme}.png")
    print("rendered:", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))


main()
