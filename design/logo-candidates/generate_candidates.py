"""Three logo candidates for dreamscape, on the same 24x24 grid as the icon set.

Concept: a chat app whose memory is a wiki graph that the model rebuilds at night by dreaming.
Crescent moon (dreaming) + 3-4 nodes joined by thin lines (the graph). No rays, no starburst,
nothing that reads as Claude's mark.

Every candidate is currentColor with the terracotta accent on the nodes or the moon, so it works
in light and dark. Stroke 1.7, round caps, 24x24 viewbox: same language as static/js/icons.js.

Run: .venv/bin/python design/logo-candidates/generate_candidates.py
The picked candidate is drawn by draw_chosen() below and written to static/logo-mark.svg.
"""
import math
from pathlib import Path

ACCENT = "#d97757"
OUT = Path(__file__).resolve().parent
PROJECT = OUT.parent.parent


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def circle_intersections(c1, r1, c2, r2):
    """The two points where two circles meet."""
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
    """One arc between two points, choosing the side by testing the arc midpoint.

    `want` is a predicate on the midpoint: True when this is the arc we want.
    Returns (large_arc_flag, sweep_flag, midpoint).
    """
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
    """Lune of the big circle minus the cut circle: outer arc of the big circle, inner arc of the
    cut circle. Returns the path plus the two horns and the middle of the inner arc."""
    p, q = circle_intersections(big, r_big, cut, r_cut)
    outer_flag, outer_sweep, outer_mid = arc_between(p, q, big, r_big, lambda m: dist(m, cut) > r_cut)
    inner_flag, inner_sweep, inner_mid = arc_between(q, p, cut, r_cut, lambda m: dist(m, big) < r_big)
    path = (f"M{pt(p)}A{n(r_big)} {n(r_big)} 0 {outer_flag} {outer_sweep} {pt(q)}"
            f"A{n(r_cut)} {n(r_cut)} 0 {inner_flag} {inner_sweep} {pt(p)}Z")
    return path, p, q, inner_mid


def node(cx, cy, r=1.9, accent=True, ring=1.5):
    return (f'<circle cx="{n(cx)}" cy="{n(cy)}" r="{n(r)}" fill="{ACCENT if accent else "none"}" '
            f'stroke-width="{n(ring)}"/>')


def edge(a, b, width=1.1):
    return f'<path d="M{pt(a)}L{pt(b)}" stroke-width="{n(width)}"/>'


def svg(body, label):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" '
            f'fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
            f'stroke-linejoin="round">\n  <title>{label}</title>\n  {body}\n</svg>\n')


def draw_candidate_1():
    """Crescent moon plus a small constellation of three nodes."""
    path, _, _, _ = crescent((10.0, 14.2), 8.4, (13.6, 10.6), 8.4)
    nodes = [(17.4, 6.2), (20.4, 10.8), (16.4, 12.2)]
    return svg(f'<path d="{path}" stroke-width="1.6"/>'
               + edge(nodes[0], nodes[2]) + edge(nodes[2], nodes[1])
               + "".join(node(*position, 1.7) for position in nodes),
               "crescent moon and a small constellation")


def draw_candidate_2():
    """A rounded chat bubble with the crescent inside it, and nodes on the bubble."""
    bubble = ("M3.6 6.2a2.6 2.6 0 0 1 2.6-2.6h11.2a2.6 2.6 0 0 1 2.6 2.6v8.6a2.6 2.6 0 0 1-2.6 2.6H10"
              "l-3.4 3.2V17.4H6.2a2.6 2.6 0 0 1-2.6-2.6z")
    inner, _, _, _ = crescent((12.2, 9.6), 4.8, (15.0, 6.8), 4.6)
    nodes = [(8.2, 13.2), (11.6, 14.4), (15.6, 12.6)]
    return svg(f'<path d="{bubble}"/>'
               + f'<path d="{inner}" stroke-width="1.5"/>'
               + edge(nodes[0], nodes[1]) + edge(nodes[1], nodes[2])
               + "".join(node(*position, 1.5) for position in nodes),
               "chat bubble with the crescent inside")


def draw_candidate_3():
    """The crescent as one node of a three-node graph."""
    path, _, _, middle = crescent((9.6, 12.2), 7.8, (12.6, 9.2), 7.8)
    nodes = [(17.8, 7.4), (18.4, 16.2)]
    return svg(f'<path d="{path}"/>'
               + edge(middle, nodes[0]) + edge(middle, nodes[1]) + edge(nodes[0], nodes[1])
               + "".join(node(*position, 1.8) for position in nodes),
               "crescent as a node of the graph")


def draw_chosen():
    """The picked candidate: candidate 1, tuned so it still reads at 16px.

    A crescent that keeps the icon-set stroke weight, three nodes with the terracotta accent, and
    thin edges between them with enough gap for the lines to show at favicon size. The moon follows
    currentColor; only the nodes carry the accent, as in the graph view.
    """
    path, _, _, _ = crescent((9.6, 14.2), 8.2, (13.0, 10.6), 8.2)
    nodes = [(16.4, 5.2), (20.6, 9.4), (15.8, 13.4)]
    return svg(f'<path d="{path}" stroke-width="1.9"/>'
               + "".join(edge(a, b, 1.3) for a, b in
                        ((nodes[0], nodes[1]), (nodes[1], nodes[2]), (nodes[2], nodes[0])))
               + "".join(node(*position, 1.9) for position in nodes),
               "dreamscape: a crescent moon and the wiki graph")


if __name__ == "__main__":
    for index, draw in enumerate((draw_candidate_1, draw_candidate_2, draw_candidate_3), start=1):
        (OUT / f"candidate-{index}.svg").write_text(draw(), encoding="utf-8")
        print(f"candidate-{index}.svg")
    (PROJECT / "static" / "logo-mark.svg").write_text(draw_chosen(), encoding="utf-8")
    print("static/logo-mark.svg")
