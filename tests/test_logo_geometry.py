import math
import xml.etree.ElementTree as ET
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
NS = "{http://www.w3.org/2000/svg}"


def _local(tag):
    return tag.split("}", 1)[-1]


def _parse(name):
    return ET.parse(STATIC / name).getroot()


def _find_by_id(root, elem_id):
    for el in root.iter():
        if el.get("id") == elem_id:
            return el
    raise AssertionError(f"missing element id={elem_id}")


def _num(el, attr):
    return float(el.get(attr))


def _glyph_group(root):
    for el in root.iter():
        if _local(el.tag) == "g" and el.get("filter"):
            return el
    raise AssertionError("glyph group not found")


def _ring(root, elem_id):
    el = _find_by_id(root, elem_id)
    assert _local(el.tag) == "circle"
    return el


def _line(root, elem_id):
    el = _find_by_id(root, elem_id)
    assert _local(el.tag) == "line"
    return el


def _canon(el):
    attrs = tuple(sorted((k, v) for k, v in el.attrib.items() if k != "transform"))
    return (_local(el.tag), attrs, tuple(_canon(c) for c in el))


def _intersections(b, q):
    cx1, cy1, r = _num(b, "cx"), _num(b, "cy"), _num(b, "r")
    cx2, cy2 = _num(q, "cx"), _num(q, "cy")
    d = math.hypot(cx2 - cx1, cy2 - cy1)
    mx, my = (cx1 + cx2) / 2, (cy1 + cy2) / 2
    h = math.sqrt(r * r - (d / 2) ** 2)
    px, py = -(cy2 - cy1) / d, (cx2 - cx1) / d
    pts = [(mx + px * h, my + py * h), (mx - px * h, my - py * h)]
    lower = max(pts, key=lambda p: p[0] + p[1])
    upper = min(pts, key=lambda p: p[0] + p[1])
    return lower, upper


def test_rings_equal_and_point_symmetric():
    root = _parse("favicon.svg")
    b = _ring(root, "b-ring")
    q = _ring(root, "q-ring")
    assert _num(b, "r") == _num(q, "r") == 5.6
    assert math.isclose(_num(b, "cx") + _num(q, "cx"), 32, abs_tol=1e-9)
    assert math.isclose(_num(b, "cy") + _num(q, "cy"), 32, abs_tol=1e-9)
    assert _num(b, "stroke-width") == _num(q, "stroke-width") == 2.68


def test_stems_rotated_correspond_and_match_caps():
    root = _parse("favicon.svg")
    g = _glyph_group(root)
    b = _line(root, "b-stem")
    q = _line(root, "q-stem")
    b_pts = [(_num(b, "x1"), _num(b, "y1")), (_num(b, "x2"), _num(b, "y2"))]
    q_pts = [(_num(q, "x1"), _num(q, "y1")), (_num(q, "x2"), _num(q, "y2"))]
    rot = {(32 - x, 32 - y) for x, y in b_pts}
    for px, py in rot:
        assert any(
            math.isclose(px, qx, abs_tol=1e-9) and math.isclose(py, qy, abs_tol=1e-9)
            for qx, qy in q_pts
        ), f"rotated b stem endpoint {(px, py)} missing from q stem"
    assert _num(b, "stroke-width") == _num(q, "stroke-width") == 2.9
    assert g.get("stroke-linecap") == "round"


def test_paint_order_and_weave_mask():
    root = _parse("favicon.svg")
    g = _glyph_group(root)
    children = list(g)
    kinds = [
        c.get("id") if _local(c.tag) in ("circle", "line") else _local(c.tag)
        for c in children
    ]
    assert kinds == ["q-ring", "q-stem", "b-ring", "b-stem", "g"]
    weave = children[4]
    assert weave.get("mask") == "url(#weaveMask)"
    assert weave.get("clip-path") is None
    redrawn = [c for c in weave if _local(c.tag) == "circle"]
    assert len(redrawn) == 1
    q = _ring(root, "q-ring")
    for attr in ("cx", "cy", "r", "stroke", "stroke-width"):
        assert redrawn[0].get(attr) == q.get(attr)
    assert not any(el.get("clip-path") for el in root.iter())
    assert not any(el.get("id") == "weaveClip" for el in root.iter())


def test_weave_fade_covers_lower_excludes_upper():
    root = _parse("favicon.svg")
    fade = _find_by_id(root, "weaveFade")
    assert _local(fade.tag) == "linearGradient"
    assert fade.get("gradientUnits") == "userSpaceOnUse"
    x1, y1, x2, y2 = _num(fade, "x1"), _num(fade, "y1"), _num(fade, "x2"), _num(fade, "y2")
    stops = {float(s.get("offset")): s.get("stop-color") for s in fade if _local(s.tag) == "stop"}
    assert stops.get(0.0) in ("#000", "#000000", "black")
    assert stops.get(1.0) in ("#fff", "#ffffff", "white")
    mask = _find_by_id(root, "weaveMask")
    assert _local(mask.tag) == "mask"
    assert mask.get("maskUnits") == "userSpaceOnUse"
    assert (_num(mask, "x"), _num(mask, "y"), _num(mask, "width"), _num(mask, "height")) == (0, 0, 32, 32)
    rects = [c for c in mask if _local(c.tag) == "rect"]
    assert len(rects) == 1
    rect = rects[0]
    assert (_num(rect, "x"), _num(rect, "y"), _num(rect, "width"), _num(rect, "height")) == (0, 0, 32, 32)
    assert rect.get("fill") == "url(#weaveFade)"
    lower, upper = _intersections(_ring(root, "b-ring"), _ring(root, "q-ring"))
    dx, dy = x2 - x1, y2 - y1
    denom = dx * dx + dy * dy

    def proj(p):
        return ((p[0] - x1) * dx + (p[1] - y1) * dy) / denom

    assert proj(upper) <= 0
    assert proj(lower) >= 1


def test_logo_matches_favicon_defs_and_glyphs():
    fav = _parse("favicon.svg")
    logo = _parse("logo.svg")
    fav_defs = fav.find(NS + "defs")
    logo_defs = logo.find(NS + "defs")
    assert fav_defs is not None and logo_defs is not None
    assert _canon(fav_defs) == _canon(logo_defs)
    assert _canon(_glyph_group(fav)) == _canon(_glyph_group(logo))
