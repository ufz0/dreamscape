"""Render the three candidates and the picked mark at 16, 20, 64 and 256 px, light and dark.

The 16px column is what the sidebar and favicon actually show.

Run: .venv/bin/python design/logo-candidates/check_sizes.py
"""
import io
from pathlib import Path

from PIL import Image
from cairosvg import svg2png

import generate_candidates as gen

OUT = Path(__file__).resolve().parent
THEMES = {"light": ("#ffffff", "#1f1e1d"), "dark": ("#2f2f2d", "#ecebe4")}
SIZES = (16, 20, 64, 256)


def render(svg_text, ink, size, paper):
    text = svg_text.replace('<svg ', f'<svg style="color:{ink}" ', 1)
    png = svg2png(bytestring=text.encode(), output_width=size, output_height=size,
                  background_color=paper)
    return Image.open(io.BytesIO(png)).convert("RGB")


def strip(name, svg_text):
    rows = []
    for theme, (paper, ink) in THEMES.items():
        row = Image.new("RGB", (sum(SIZES) + 8 * len(SIZES), max(SIZES)), paper)
        x = 0
        for size in SIZES:
            image = render(svg_text, ink, size, paper)
            row.paste(image, (x, (max(SIZES) - size) // 2))
            x += size + 8
        rows.append(row)
    canvas = Image.new("RGB", (rows[0].width, rows[0].height * 2 + 8), "#ffffff")
    canvas.paste(rows[0], (0, 0))
    canvas.paste(rows[1], (0, rows[0].height + 8))
    canvas.save(OUT / f"{name}-sizes.png")
    print(f"{name}-sizes.png")


for index, draw in enumerate((gen.draw_candidate_1, gen.draw_candidate_2, gen.draw_candidate_3),
                             start=1):
    strip(f"candidate-{index}", draw())
strip("chosen", gen.draw_chosen())
