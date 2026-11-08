"""Generate the brand assets from one drawing.

The mark: a crescent moon (dreaming) plus a three-node constellation joined by thin lines (the
wiki graph). No rays, nothing that reads as Claude's mark. See design/logo-candidates/ for the
three candidates this came from.

Writes:
  static/logo-mark.svg   icon only, currentColor with the terracotta accent
  static/logo-full.svg   icon + wordmark, the wordmark outlined from static/fonts/ so it renders
                         the same anywhere (SVG text would depend on the font being installed)
  static/favicon.svg     the same mark, transparent, light and dark inside the SVG
  static/js/logo.js      the shapes the app draws inline, so the mark follows the theme chosen in
                         Settings

Run: .venv/bin/python scripts/make_logo.py
"""
import math
import re
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

PROJECT = Path(__file__).resolve().parent.parent
STATIC = PROJECT / "static"
FONT = STATIC / "fonts" / "source-serif-4-600.woff2"

ACCENT = "#d97757"
INK_LIGHT = "#1f1e1d"
INK_DARK = "#ecebe4"
PAPER_LIGHT = "#faf9f5"
PAPER_DARK = "#262624"

WORD = "dreamscape"
WORD_HEIGHT = 12.8      # ink height of the wordmark on the 24 grid, matching the brand row
GAP = 9.0               # between the mark and the word
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def circle_intersections(c1, r1, c2, r2):
    d = dist(c1, c2)
    a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h = math.sqrt(max(0.0, r1 * r1 - a * a))
    ux, uy = (c2[0] - c1[0]) / d, (c2[1] - c1[1]) / d
    base = (c1[0] + a * ux, c1[1] + a * uy)
    return [(base[0] + h * uy, base[1] - h * ux), (base[0] - h * uy, base[1] + h * ux)]


def n(value):
    return f"{round(value, 2):g}"


def pt(p):
    return f"{n(p[0])} {n(p[1])}"


def arc_between(start, end, centre, radius, want):
    a1 = math.atan2(start[1] - centre[1], start[0] - centre[0])
    a2 = math.atan2(end[1] - centre[1], end[0] - centre[0])
    for sweep in (1, 0):
        span = (a2 - a1) % (2 * math.pi) if sweep else (a1 - a2) % (2 * math.pi)
        angle = a1 + (span / 2 if sweep else -span / 2)
        mid = (centre[0] + radius * math.cos(angle), centre[1] + radius * math.sin(angle))
        if want(mid):
            return ("1" if span > math.pi else "0"), sweep, mid
    raise ValueError("no arc found")


def crescent(big, r_big, cut, r_cut):
    p, q = circle_intersections(big, r_big, cut, r_cut)
    outer_flag, outer_sweep, _ = arc_between(p, q, big, r_big, lambda m: dist(m, cut) > r_cut)
    inner_flag, inner_sweep, inner_mid = arc_between(q, p, cut, r_cut, lambda m: dist(m, big) < r_big)
    path = (f"M{pt(p)}A{n(r_big)} {n(r_big)} 0 {outer_flag} {outer_sweep} {pt(q)}"
            f"A{n(r_cut)} {n(r_cut)} 0 {inner_flag} {inner_sweep} {pt(p)}Z")
    return path, inner_mid


def node(cx, cy, r, ring, fill=ACCENT):
    return f'<circle cx="{n(cx)}" cy="{n(cy)}" r="{n(r)}" fill="{fill}" stroke-width="{n(ring)}"/>'


def edge(a, b, width):
    return f'<path d="M{pt(a)}L{pt(b)}" stroke-width="{n(width)}"/>'


def mark_shapes(node_radius=1.9, node_ring=1.5, edge_width=1.3, moon_stroke=1.9, fill=ACCENT):
    path, _ = crescent((9.6, 14.2), 8.2, (13.0, 10.6), 8.2)
    nodes = [(16.4, 5.2), (20.6, 9.4), (15.8, 13.4)]
    lines = "".join(edge(a, b, edge_width)
                    for a, b in ((nodes[0], nodes[1]), (nodes[1], nodes[2]), (nodes[2], nodes[0])))
    dots = "".join(node(*position, node_radius, node_ring, fill) for position in nodes)
    return f'<path d="{path}" stroke-width="{n(moon_stroke)}"/>' + lines + dots


# The favicon is shown at 16px, where thin edges disappear: heavier lines, larger dots.
APP_SHAPES = mark_shapes()
FAVICON_SHAPES = mark_shapes(node_radius=2.2, node_ring=1.7, edge_width=1.6, moon_stroke=2.2)


def svg_open(width, theme_css=False):
    style = ""
    if theme_css:
        style = ("  <style>svg { color: " + INK_LIGHT + "; }"
                 "@media (prefers-color-scheme: dark) { svg { color: " + INK_DARK + "; } }</style>\n")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n(width)} 24" '
            f'width="{n(width)}" height="24" fill="none" stroke="currentColor" '
            f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">\n'
            f'{style}  <title>dreamscape</title>\n')


def wordmark_paths(text, ink_height):
    """Outline the word from the shipped font.

    Returns (list of path data, width). The paths are drawn in a local space where the ink top is
    y=0, so the caller can centre the word against the mark using the measured ink height rather
    than font metrics.
    """
    font = TTFont(FONT)
    upm = font["head"].unitsPerEm
    glyphset = font.getGlyphSet()
    cmap = font.getBestCmap()
    metrics = font["hmtx"]

    glyphs = []
    for char in text:
        name = cmap[ord(char)]
        bounds = BoundsPen(glyphset)
        glyphset[name].draw(bounds)
        glyphs.append((name, bounds.bounds))

    drawn = [g for g in glyphs if g[1]]
    y_min = min(b[1] for _, b in drawn)      # the lowest ink point
    y_max = max(b[3] for _, b in drawn)       # the highest ink point
    scale = ink_height / (y_max - y_min)

    cursor = 0.0
    paths = []
    for name, bounds in glyphs:
        pen = SVGPathPen(glyphset)
        transformed = TransformPen(pen, (scale, 0, 0, -scale, cursor * scale, y_max * scale))
        glyphset[name].draw(transformed)
        commands = pen.getCommands()
        if commands:
            paths.append(NUMBER_RE.sub(lambda m: n(float(m.group())), commands))
        cursor += metrics[name][0]

    return paths, cursor * scale


def logo_full(shapes):
    paths, width = wordmark_paths(WORD, WORD_HEIGHT)
    total = 24 + GAP + width
    word_x = 24 + GAP
    word_y = 12.0 - WORD_HEIGHT / 2      # centre the word against the mark
    out = [svg_open(total)]
    out.append(f"  {shapes}\n")
    for command in paths:
        out.append(f'  <g transform="translate({n(word_x)},{n(word_y)})">'
                   f'<path d="{command}" fill="currentColor" stroke="none"/></g>\n')
    out.append("</svg>\n")
    return "".join(out)


def favicon(shapes):
    """The mark on a transparent background, so it works on a light or a dark browser tab bar.

    A raster cannot switch themes, but an SVG can: the neutral parts of the mark change with
    prefers-color-scheme, the terracotta accent stays.
    """
    return svg_open(24, theme_css=True) + f"  {shapes}\n</svg>\n"


def main():
    STATIC.mkdir(exist_ok=True)
    (STATIC / "logo-mark.svg").write_text(svg_open(24) + f"  {APP_SHAPES}\n</svg>\n", encoding="utf-8")
    (STATIC / "logo-full.svg").write_text(logo_full(APP_SHAPES), encoding="utf-8")
    (STATIC / "favicon.svg").write_text(favicon(FAVICON_SHAPES), encoding="utf-8")

    js = ("// Brand mark, generated by scripts/make_logo.py - do not edit by hand.\n"
          "// The app draws it inline so the mark follows the theme chosen in Settings; the same\n"
          "// shapes are in static/logo-mark.svg for use outside the app.\n"
          f"export const LOGO = `{mark_shapes(fill='var(--accent)')}`;\n")
    (STATIC / "js" / "logo.js").write_text(js, encoding="utf-8")
    print("wrote static/logo-mark.svg, static/logo-full.svg, static/favicon.svg, static/js/logo.js")


main()
