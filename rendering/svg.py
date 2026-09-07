from __future__ import annotations

import math
import os
import re
import xml.etree.ElementTree as ET

from core.model import SceneObject, Transform

# Adaptive flattening: for a path whose control points span `extent` units,
# use `max(MIN_TOL, extent * ADAPTIVE_RATIO)` as the flatness criterion so big
# drawings don't explode into millions of vertices.
ADAPTIVE_RATIO = 0.004
MIN_TOL = 0.08


def _local(tag: str) -> str:
    """Strip the XML namespace prefix from an element tag."""
    return tag.split("}", 1)[1] if "}" in tag else tag


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

_NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "orange": (255, 165, 0),
    "purple": (128, 0, 128), "magenta": (255, 0, 255), "cyan": (0, 255, 255),
    "pink": (255, 192, 203), "brown": (165, 42, 42), "navy": (0, 0, 128),
    "teal": (0, 128, 128), "lime": (0, 255, 0), "silver": (192, 192, 192),
    "maroon": (128, 0, 0), "olive": (128, 128, 0), "gold": (255, 215, 0),
    "lightgray": (211, 211, 211), "lightgrey": (211, 211, 211),
    "darkgray": (169, 169, 169), "darkgrey": (169, 169, 169),
    "transparent": None,
}

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_color(value: str | None) -> str | None:
    """Parse a CSS/SVG color into a '#rrggbb' hex string (or None)."""
    if value is None:
        return None
    value = value.strip()
    low = value.lower()
    if not value or low in ("none", "transparent"):
        return None

    m = _HEX_RE.match(low)
    if m:
        hx = m.group(1)
        if len(hx) == 3:
            return "#%s%s%s" % tuple(c * 2 for c in hx)
        return "#" + hx

    if low in _NAMED_COLORS:
        rgb = _NAMED_COLORS[low]
        if rgb is None:
            return None
        return "#%02x%02x%02x" % rgb

    m = re.match(r"rgba?\(([^)]*)\)", value, re.IGNORECASE)
    if m:
        parts = [p.strip() for p in m.group(1).replace("/", ",").split(",")]
        if len(parts) < 3:
            return None
        nums = []
        for p in parts[:3]:
            if p.endswith("%"):
                nums.append(int(max(0.0, min(100.0, float(p[:-1]))) * 255 / 100))
            else:
                try:
                    nums.append(int(max(0.0, min(255.0, float(p)))))
                except ValueError:
                    nums.append(0)
        return "#%02x%02x%02x" % tuple(nums)
    return None


def _css_props(style: str | None) -> dict[str, str]:
    """Parse a `style="prop: val; ..."` attribute into a dict."""
    props: dict[str, str] = {}
    if not style:
        return props
    for chunk in style.split(";"):
        if ":" in chunk:
            key, _, val = chunk.partition(":")
            props[key.strip().lower()] = val.strip()
    return props


# ---------------------------------------------------------------------------
# Affine transform helpers
# ---------------------------------------------------------------------------

def parse_transform(transform_str: str | None) -> list | None:
    """Parse an SVG `transform` attribute into an ordered list of ops:
    ("translate", tx, ty) / ("rotate", deg, cx, cy) / ("scale", sx, sy) /
    ("matrix", a, b, c, d, e, f) / ("skewX"|"skewY", deg).
    Returns None when empty / unimplementable.
    """
    if not transform_str:
        return None
    ops: list = []
    pattern = re.compile(r"([a-zA-Z]+)\s*\(([^)]*)\)")
    for m in pattern.finditer(transform_str):
        op = m.group(1)
        args = [float(v) for v in re.split(r"[,\s]+", m.group(2).strip()) if v]
        if op == "translate" and args:
            ops.append(("translate", args[0], args[1] if len(args) > 1 else 0.0))
        elif op == "rotate" and args:
            if len(args) >= 3:
                ops.append(("rotate", args[0], args[1], args[2]))
            else:
                ops.append(("rotate", args[0], 0.0, 0.0))
        elif op == "scale" and args:
            sx = args[0]
            sy = args[1] if len(args) > 1 else sx
            ops.append(("scale", sx, sy))
        elif op == "matrix" and len(args) >= 6:
            ops.append(("matrix", *args[:6]))
        elif op == "skewX" and args:
            ops.append(("skewX", args[0]))
        elif op == "skewY" and args:
            ops.append(("skewY", args[0]))
    return ops or None


def _affine(op):
    """Convert a single transform op into a flat (a, b, c, d, e, f) matrix."""
    name = op[0]
    if name == "translate":
        return (1.0, 0.0, 0.0, 1.0, op[1], op[2])
    if name == "scale":
        return (op[1], 0.0, 0.0, op[2], 0.0, 0.0)
    if name == "rotate":
        ang = math.radians(op[1])
        ca, sa = math.cos(ang), math.sin(ang)
        if op[2] != 0.0 or op[3] != 0.0:
            cx, cy = op[2], op[3]
            t = (1.0, 0.0, 0.0, 1.0, cx, cy)
            r = (ca, sa, -sa, ca, 0.0, 0.0)
            ti = (1.0, 0.0, 0.0, 1.0, -cx, -cy)
            return _mult_affine(_mult_affine(t, r), ti)
        return (ca, sa, -sa, ca, 0.0, 0.0)
    if name == "matrix":
        return (op[1], op[2], op[3], op[4], op[5], op[6])
    if name == "skewX":
        return (1.0, 0.0, math.tan(math.radians(op[1])), 1.0, 0.0, 0.0)
    if name == "skewY":
        return (1.0, math.tan(math.radians(op[1])), 0.0, 1.0, 0.0, 0.0)
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mult_affine(m1, m2):
    """Compose two affines: result maps x' = m1(m2(x)) (m2 applied first)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1,
    )


def _trace_affine(ops: list | None) -> tuple | None:
    """Compose an ops list into a single (a, b, c, d, e, f) affine."""
    if not ops:
        return None
    m = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for op in ops:
        m = _mult_affine(m, _affine(op))
    return m


def _ops_to_transform(ops: list | None) -> Transform:
    """Decompose a TRS affine into the editor's Transform (x, y, rotation,
    scale_x, scale_y). Exact for translate/rotate/scale (no skew)."""
    m = _trace_affine(ops)
    if m is None:
        return Transform()
    a, b, c, d, e, f = m
    angle = math.degrees(math.atan2(b, a))
    cr = math.cos(math.radians(angle))
    sr = math.sin(math.radians(angle))
    sx = a * cr + b * sr
    sy = d * cr - c * sr
    if sx == 0.0:
        sx = 1.0
    if sy == 0.0:
        sy = 1.0
    return Transform(x=e, y=f, rotation=angle, scale_x=sx, scale_y=sy)


# ---------------------------------------------------------------------------
# SVG path parsing + flattening
# ---------------------------------------------------------------------------

def _flatten_cubic(p0, p1, p2, p3, out, tolerance: float):
    ax, ay = p0
    bx, by = p1
    cx, cy = p2
    dx, dy = p3
    x = (3 * bx - 2 * ax - dx) ** 2 + (3 * cx - 2 * dx - ax) ** 2
    y = (3 * by - 2 * ay - dy) ** 2 + (3 * cy - 2 * dy - by) ** 2
    if x + y <= tolerance * tolerance:
        out.append((dx, dy))
        return
    abx, aby = (ax + bx) / 2, (ay + by) / 2
    bcx, bcy = (bx + cx) / 2, (by + cy) / 2
    cdx, cdy = (cx + dx) / 2, (cy + dy) / 2
    abbcx, abbcy = (abx + bcx) / 2, (aby + bcy) / 2
    bccdx, bccdy = (bcx + cdx) / 2, (bcy + cdy) / 2
    midx, midy = (abbcx + bccdx) / 2, (abbcy + bccdy) / 2
    _flatten_cubic((ax, ay), (abx, aby), (abbcx, abbcy), (midx, midy), out, tolerance)
    _flatten_cubic((midx, midy), (bccdx, bccdy), (cdx, cdy), (dx, dy), out, tolerance)


def _flatten_quad(p0, p1, p2, out, tolerance: float):
    ax, ay = p0
    bx, by = p1
    cx, cy = p2
    x = (2 * bx - ax - cx) ** 2 + (2 * by - ay - cy) ** 2
    if x <= tolerance * tolerance:
        out.append((cx, cy))
        return
    abx, aby = (ax + bx) / 2, (ay + by) / 2
    bcx, bcy = (bx + cx) / 2, (by + cy) / 2
    mx, my = (abx + bcx) / 2, (aby + bcy) / 2
    _flatten_quad((ax, ay), (abx, aby), (mx, my), out, tolerance)
    _flatten_quad((mx, my), (bcx, bcy), (cx, cy), out, tolerance)


def _arc_to_cubics(cx, cy, rx, ry, phi, theta1, delta_theta):
    """Arc (center parametrization) -> list of cubic bezier 4-tuples."""
    if rx == 0 or ry == 0 or delta_theta == 0:
        return []
    cos_phi = math.cos(phi)
    sin_phi = math.sin(phi)
    segments = int(math.ceil(abs(delta_theta) / (math.pi / 2)))
    delta = delta_theta / segments
    cubics: list = []
    t = theta1
    for _ in range(segments):
        t1, t2 = t, t + delta
        alpha = 4.0 / 3.0 * math.tan(delta / 4.0)
        cos1, sin1 = math.cos(t1), math.sin(t1)
        cos2, sin2 = math.cos(t2), math.sin(t2)
        p0 = (cx + rx * cos1 * cos_phi - ry * sin1 * sin_phi,
              cy + rx * cos1 * sin_phi + ry * sin1 * cos_phi)
        p3 = (cx + rx * cos2 * cos_phi - ry * sin2 * sin_phi,
              cy + rx * cos2 * sin_phi + ry * sin2 * cos_phi)
        dx1, dy1 = -sin1, cos1
        dx2, dy2 = -sin2, cos2
        c1 = (p0[0] + alpha * (rx * dx1 * cos_phi - ry * dy1 * sin_phi),
              p0[1] + alpha * (rx * dx1 * sin_phi + ry * dy1 * cos_phi))
        c2 = (p3[0] - alpha * (rx * dx2 * cos_phi - ry * dy2 * sin_phi),
              p3[1] - alpha * (rx * dx2 * sin_phi + ry * dy2 * cos_phi))
        cubics.append((p0, c1, c2, p3))
        t = t2
    return cubics


def _endpoint_to_center_arc(x1, y1, x2, y2, rx, ry, x_rot_deg, large_arc, sweep):
    """SVG endpoint→center arc parametrization (F.6.5 in the SVG spec)."""
    phi = math.radians(x_rot_deg)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx = (x1 - x2) / 2
    dy = (y1 - y2) / 2
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    if den == 0:
        raise ValueError("degenerate arc")
    radicand = max(0.0, num / den)
    sign = -1.0 if large_arc != sweep else 1.0
    coef = sign * math.sqrt(radicand)
    cxp = coef * rx * y1p / ry
    cyp = coef * -ry * x1p / rx

    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        if norm == 0:
            return 0.0
        ang = math.acos(max(-1.0, min(1.0, dot / norm)))
        if ux * vy - uy * vx < 0:
            ang = -ang
        return ang

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta_theta = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
                        (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    delta_theta %= 2 * math.pi
    if sweep == 0 and delta_theta > 0:
        delta_theta -= 2 * math.pi
    elif sweep == 1 and delta_theta < 0:
        delta_theta += 2 * math.pi
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2
    return cx, cy, rx, ry, phi, theta1, delta_theta


_PATH_TOKEN_RE = re.compile(
    r"([MLHVCSQTAZmlhvcsqtaz])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)"
)


def _tokenize_path(d: str):
    for m in _PATH_TOKEN_RE.finditer(d):
        if m.group(1) is not None:
            yield ("cmd", m.group(1))
        else:
            yield ("num", float(m.group(2)))


def _path_extent(d: str) -> float:
    """Rough axis-aligned size of a path's points, from its raw numbers."""
    vals = [float(x) for m in re.finditer(r"-?\d+\.?\d*(?:[eE][+-]?\d+)?", d) for x in (m.group(),)]
    if not vals:
        return 1.0
    xs = vals[0::2]
    ys = vals[1::2]
    return max(max(xs) - min(xs), max(ys) - min(ys), 1.0)


def trace_path(d: str) -> list[list[tuple[float, float]]]:
    """Parse an SVG `d` attribute and flatten it to subpath point lists.

    Supports all SVG path commands, relative+absolute, repeated parameters,
    implicit lineto after M, and smooth-cubic/quadratic reflection. The
    flattening tolerance scales with the path's own extent so giant
    drawings don't explode into millions of vertices.
    """
    tolerance = max(MIN_TOL, ADAPTIVE_RATIO * _path_extent(d))
    tokens = list(_tokenize_path(d))
    subpaths: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] | None = None

    cx = cy = 0.0
    start_x = start_y = 0.0
    prev_cubic_ctl: tuple | None = None
    prev_quad_ctl: tuple | None = None
    prev_upper = ""

    def num(i):
        return tokens[i][1] if i < n and tokens[i][0] == "num" else None

    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i][0] == "cmd":
            cmd = tokens[i][1]
            i += 1
        else:
            # Implicit repetition of the previous command.
            cmd = prev_upper
            if not cmd:
                i += 1
                continue
        upper = cmd.upper()
        rel = cmd.islower()

        if upper == "M":
            x = num(i)
            y = num(i + 1)
            if x is None or y is None:
                break
            if rel:
                x += cx
                y += cy
            cx, cy = x, y
            start_x, start_y = x, y
            cur = [(x, y)]
            subpaths.append(cur)
            i += 2
            while True:
                nx = num(i)
                ny = num(i + 1)
                if nx is None or ny is None:
                    break
                if rel:
                    nx += cx
                    ny += cy
                cx, cy = nx, ny
                cur.append((cx, cy))
                i += 2
            prev_upper = "L"
            prev_cubic_ctl = None
            prev_quad_ctl = None
            continue

        if upper == "Z":
            if cur is not None and cur[0] != cur[-1]:
                cur.append(cur[0])
            cx, cy = start_x, start_y
            prev_cubic_ctl = None
            prev_quad_ctl = None
            prev_upper = "Z"
            i += 0
            continue

        prev_upper = upper

        if cur is None:
            continue

        if upper == "L":
            while True:
                x, y = num(i), num(i + 1)
                if x is None or y is None:
                    break
                if rel:
                    x += cx
                    y += cy
                cx, cy = x, y
                cur.append((cx, cy))
                i += 2
            prev_cubic_ctl = None
            prev_quad_ctl = None
        elif upper == "H":
            while num(i) is not None:
                x = num(i)
                if rel:
                    x += cx
                cx = x
                cur.append((cx, cy))
                i += 1
            prev_cubic_ctl = None
            prev_quad_ctl = None
        elif upper == "V":
            while num(i) is not None:
                y = num(i)
                if rel:
                    y += cy
                cy = y
                cur.append((cx, cy))
                i += 1
            prev_cubic_ctl = None
            prev_quad_ctl = None
        elif upper == "C":
            while num(i) is not None and num(i + 1) is not None and num(i + 2) is not None \
                    and num(i + 3) is not None and num(i + 4) is not None and num(i + 5) is not None:
                x1, y1 = num(i), num(i + 1)
                x2, y2 = num(i + 2), num(i + 3)
                x3, y3 = num(i + 4), num(i + 5)
                if rel:
                    x1 += cx; y1 += cy
                    x2 += cx; y2 += cy
                    x3 += cx; y3 += cy
                _flatten_cubic((cx, cy), (x1, y1), (x2, y2), (x3, y3), cur, tolerance)
                prev_cubic_ctl = (x2, y2)
                cx, cy = x3, y3
                i += 6
            prev_quad_ctl = None
        elif upper == "S":
            while num(i) is not None and num(i + 1) is not None and num(i + 2) is not None \
                    and num(i + 3) is not None:
                x2, y2 = num(i), num(i + 1)
                x3, y3 = num(i + 2), num(i + 3)
                if prev_cubic_ctl is not None and prev_upper in ("C", "S"):
                    x1 = 2 * cx - prev_cubic_ctl[0]
                    y1 = 2 * cy - prev_cubic_ctl[1]
                else:
                    x1, y1 = cx, cy
                if rel:
                    x2 += cx; y2 += cy
                    x3 += cx; y3 += cy
                _flatten_cubic((cx, cy), (x1, y1), (x2, y2), (x3, y3), cur, tolerance)
                prev_cubic_ctl = (x2, y2)
                cx, cy = x3, y3
                i += 4
            prev_quad_ctl = None
        elif upper == "Q":
            while num(i) is not None and num(i + 1) is not None and num(i + 2) is not None \
                    and num(i + 3) is not None:
                x1, y1 = num(i), num(i + 1)
                x2, y2 = num(i + 2), num(i + 3)
                if rel:
                    x1 += cx; y1 += cy
                    x2 += cx; y2 += cy
                _flatten_quad((cx, cy), (x1, y1), (x2, y2), cur, tolerance)
                prev_quad_ctl = (x1, y1)
                cx, cy = x2, y2
                i += 4
            prev_cubic_ctl = None
        elif upper == "T":
            while num(i) is not None and num(i + 1) is not None:
                x2 = num(i); y2 = num(i + 1)
                if prev_quad_ctl is not None and prev_upper in ("Q", "T"):
                    x1 = 2 * cx - prev_quad_ctl[0]
                    y1 = 2 * cy - prev_quad_ctl[1]
                else:
                    x1, y1 = cx, cy
                if rel:
                    x2 += cx; y2 += cy
                _flatten_quad((cx, cy), (x1, y1), (x2, y2), cur, tolerance)
                prev_quad_ctl = (x1, y1)
                cx, cy = x2, y2
                i += 2
            prev_cubic_ctl = None
        elif upper == "A":
            while num(i) is not None and num(i + 6) is not None:
                rx, ry = num(i), num(i + 1)
                x_rot, laf, sf = num(i + 2), int(num(i + 3)), int(num(i + 4))
                x2, y2 = num(i + 5), num(i + 6)
                if rel:
                    x2 += cx
                    y2 += cy
                rx = abs(rx)
                ry = abs(ry)
                if rx == 0 or ry == 0 or (cx == x2 and cy == y2):
                    cur.append((x2, y2))
                    cx, cy = x2, y2
                    i += 7
                    continue
                try:
                    ccx, ccy, arx, ary, phi, t1, dt = _endpoint_to_center_arc(
                        cx, cy, x2, y2, rx, ry, x_rot, laf, sf
                    )
                except (ValueError, ZeroDivisionError):
                    cur.append((x2, y2))
                    cx, cy = x2, y2
                    i += 7
                    continue
                for p0, c1, c2, p3 in _arc_to_cubics(ccx, ccy, arx, ary, phi, t1, dt):
                    _flatten_cubic(p0, c1, c2, p3, cur, tolerance)
                cx, cy = x2, y2
                prev_cubic_ctl = None
                prev_quad_ctl = None
                i += 7
        else:
            i += 1

    return subpaths


# ---------------------------------------------------------------------------
# SVG importer (converts to simple native primitives)
# ---------------------------------------------------------------------------

class _Importer:
    """Recursive SVG -> SceneObject tree converter.

    Groups become container Symbols; shapes become the editor's native
    rect / circle / polygon primitives. Paths are flattened to polygons once
    at import time (beziers/arcs become point lists). Everything imported is
    plain primitives with a flat fill color - no gradients, strokes, curves,
    or path data stored in the scene.
    """

    _RE_BOILERPLATE_GROUP = re.compile(
        r"^(layer|page|costume|artboard|art|canvas|group)[-__. ]?[\d.]*$", re.IGNORECASE
    )

    def __init__(self, root: ET.Element, filename: str = "SVG"):
        self.root = root
        self.filename = filename
        self.defs: dict[str, ET.Element] = {}
        self._collect_defs(root)

    def _collect_defs(self, elem: ET.Element):
        for child in elem.iter():
            tag = _local(child.tag)
            if tag in ("linearGradient", "radialGradient", "symbol", "clipPath",
                       "pattern", "marker", "filter", "mask"):
                gid = child.get("id")
                if gid:
                    self.defs[gid] = child

    def _resolve_fill(self, elem: ET.Element, inherited: str | None) -> str | None:
        """Effective fill color for an element (may be "none"), resolved from
        inline style -> fill attribute -> inherited group fill."""
        style = _css_props(elem.get("style"))
        raw = style.get("fill") if style else None
        if raw is None:
            raw = elem.get("fill")
        if raw is None:
            return inherited
        raw = raw.strip()
        if raw.lower() == "none":
            return "none"
        if raw.lower().startswith("url("):
            # Approximate gradients with their first stop colour.
            m = re.match(r"url\(#([^)]+)\)", raw)
            if m:
                grad = self.defs.get(m.group(1))
                if grad is not None:
                    base = self._gradient_base_color(grad)
                    if base is not None:
                        return base
            return None
        return parse_color(raw)

    def _gradient_base_color(self, grad: ET.Element) -> str | None:
        for stop in grad:
            if _local(stop.tag) != "stop":
                continue
            style = _css_props(stop.get("style"))
            raw = style.get("stop-color") or stop.get("stop-color") or "#000000"
            return parse_color(raw) or "#000000"
        return None

    def _process_element(self, elem: ET.Element, inherited_fill: str | None) -> SceneObject | None:
        tag = _local(elem.tag)
        fill = self._resolve_fill(elem, inherited_fill)
        if tag in ("g", "svg", "Symbol", "symbol"):
            return self._process_group(elem, fill)
        if tag == "defs":
            return None
        if tag == "use":
            return self._process_use(elem, fill)
        if tag == "path":
            return self._process_path(elem, fill)
        if tag == "rect":
            return self._process_rect(elem, fill)
        if tag == "circle":
            return self._process_circle(elem, fill)
        if tag == "ellipse":
            return self._process_ellipse(elem, fill)
        if tag == "line":
            return self._process_line(elem, fill)
        if tag == "polyline":
            return self._process_poly(elem, fill, closed=False)
        if tag == "polygon":
            return self._process_poly(elem, fill, closed=True)
        if tag == "text":
            return self._process_text(elem, fill)
        # Everything else (style/title/desc/gradients/clipPaths/markers...)
        # carries no geometry we can draw natively.
        return None

    def _name(self, elem: ET.Element, default: str) -> str:
        return elem.get("id") or elem.get("inkscape:label") or default

    def _container_for(self, elem: ET.Element, fill: str | None) -> SceneObject:
        return SceneObject(
            name=self._name(elem, "Group"),
            shape_type="container",
            transform=_ops_to_transform(parse_transform(elem.get("transform"))),
        )

    def _process_group(self, elem: ET.Element, fill: str | None) -> SceneObject:
        container = self._container_for(elem, fill)
        if elem.get("opacity"):
            try:
                if float(elem.get("opacity")) <= 0.0:
                    container.visible = False
            except ValueError:
                pass
        for child in elem:
            obj = self._process_element(child, fill)
            if obj is not None:
                container.children.append(obj)
        return container

    def _process_path(self, elem: ET.Element, fill: str | None) -> SceneObject | None:
        d = elem.get("d")
        if not d:
            return None
        subpaths = [sp for sp in trace_path(d) if len(sp) >= 2]
        if not subpaths:
            return None
        points = self._best_subpath(subpaths)
        if len(points) < 3:
            return None
        obj = SceneObject(
            name=self._name(elem, "Path"),
            shape_type="polygon",
            shape_data={"points": points},
            transform=_ops_to_transform(parse_transform(elem.get("transform"))),
            color=fill or "#cccccc",
        )
        return obj

    def _best_subpath(self, subpaths: list) -> list:
        """Keep the longest subpath as the polygon (a path with compound
        subpaths/holes keeps just its main outline - simple and predictable)."""
        return max(subpaths, key=len)

    def _process_rect(self, elem: ET.Element, fill: str | None) -> SceneObject:
        x = float(elem.get("x", "0"))
        y = float(elem.get("y", "0"))
        w = float(elem.get("width", "0"))
        h = float(elem.get("height", "0"))
        rx = float(elem.get("rx", "0") or "0")
        ry = float(elem.get("ry", "0") or "0")
        tr = _ops_to_transform(parse_transform(elem.get("transform")))
        if rx == 0 and ry == 0:
            obj = SceneObject(
                name=self._name(elem, "Rect"),
                shape_type="rect",
                shape_data={"width": w, "height": h},
                transform=tr,
                color=fill or "#cccccc",
            )
            obj.transform.x += x + w / 2
            obj.transform.y += y + h / 2
            return obj
        if rx == 0 and ry > 0:
            rx = ry
        elif ry == 0 and rx > 0:
            ry = rx
        rx = min(abs(rx), w / 2)
        ry = min(abs(ry), h / 2)
        points = _rounded_rect_points(x, y, w, h, rx, ry)
        obj = SceneObject(
            name=self._name(elem, "Rect"),
            shape_type="polygon",
            shape_data={"points": points},
            transform=tr,
            color=fill or "#cccccc",
        )
        return obj

    def _process_circle(self, elem: ET.Element, fill: str | None) -> SceneObject:
        cx = float(elem.get("cx", "0"))
        cy = float(elem.get("cy", "0"))
        r = abs(float(elem.get("r", "0") or "0"))
        tr = _ops_to_transform(parse_transform(elem.get("transform")))
        obj = SceneObject(
            name=self._name(elem, "Circle"),
            shape_type="circle",
            shape_data={"radius": r},
            transform=tr,
            color=fill or "#cccccc",
        )
        obj.transform.x += cx
        obj.transform.y += cy
        return obj

    def _process_ellipse(self, elem: ET.Element, fill: str | None) -> SceneObject:
        cx = float(elem.get("cx", "0"))
        cy = float(elem.get("cy", "0"))
        rx = abs(float(elem.get("rx", "0") or "0"))
        ry = abs(float(elem.get("ry", "0") or "0"))
        steps = max(12, int(2 * math.pi * max(rx, ry, 1)))
        pts = []
        for k in range(steps):
            a = 2 * math.pi * k / steps
            pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a)))
        return SceneObject(
            name=self._name(elem, "Ellipse"),
            shape_type="polygon",
            shape_data={"points": pts},
            transform=_ops_to_transform(parse_transform(elem.get("transform"))),
            color=fill or "#cccccc",
        )

    def _process_line(self, elem: ET.Element, fill: str | None) -> SceneObject:
        x1 = float(elem.get("x1", "0"))
        y1 = float(elem.get("y1", "0"))
        x2 = float(elem.get("x2", "0"))
        y2 = float(elem.get("y2", "0"))
        return SceneObject(
            name=self._name(elem, "Line"),
            shape_type="polygon",
            shape_data={"points": [(x1, y1), (x2, y2)]},
            transform=_ops_to_transform(parse_transform(elem.get("transform"))),
            color=fill or "#cccccc",
        )

    def _process_poly(self, elem: ET.Element, fill: str | None, closed: bool) -> SceneObject | None:
        pts_str = elem.get("points", "")
        if not pts_str.strip():
            return None
        coords = [float(v) for v in re.split(r"[,\s]+", pts_str.strip()) if v]
        pts = [(coords[k], coords[k + 1]) for k in range(0, len(coords) - 1, 2)]
        if len(pts) < 2:
            return None
        return SceneObject(
            name=self._name(elem, "Polygon" if closed else "Polyline"),
            shape_type="polygon",
            shape_data={"points": pts},
            transform=_ops_to_transform(parse_transform(elem.get("transform"))),
            color=fill or "#cccccc",
        )

    def _process_text(self, elem: ET.Element, fill: str | None) -> SceneObject | None:
        text = (elem.text or "").strip()
        x = float(elem.get("x", "0") or "0")
        y = float(elem.get("y", "0") or "0")
        style = _css_props(elem.get("style"))
        try:
            size = float((style.get("font-size") or elem.get("font-size") or "16").rstrip("px"))
        except ValueError:
            size = 16.0
        width = size * max(len(text), 1) * 0.6
        pts = [(x, y - size), (x + width, y - size), (x + width, y), (x, y)]
        return SceneObject(
            name=text[:24] or "Text",
            shape_type="polygon",
            shape_data={"points": pts, "text": text},
            transform=_ops_to_transform(parse_transform(elem.get("transform"))),
            color=fill or "#000000",
        )

    def _process_use(self, elem: ET.Element, fill: str | None) -> SceneObject | None:
        href = elem.get("href") or elem.get("{http://www.w3.org/1999/xlink}href") or ""
        if not href.startswith("#"):
            return None
        ref = self.defs.get(href[1:])
        if ref is None:
            return None
        x = float(elem.get("x", "0") or "0")
        y = float(elem.get("y", "0") or "0")
        obj = self._process_element(ref, fill)
        if obj is None:
            return None
        if x or y or parse_transform(elem.get("transform")):
            use_ops = list(parse_transform(elem.get("transform")) or []) + [("translate", x, y)]
            container = SceneObject(name="Use", shape_type="container")
            container.transform = _ops_to_transform(use_ops)
            container.children.append(obj)
            return container
        return obj

    def _is_degenerate_group(self, obj: SceneObject) -> bool:
        """True when a container carries no state of its own: no transform,
        no mask, fully visible."""
        t = obj.transform
        return (
            obj.shape_type == "container"
            and not obj.is_mask
            and obj.visible
            and t.x == 0.0 and t.y == 0.0 and t.rotation == 0.0
            and t.scale_x == 1.0 and t.scale_y == 1.0
        )

    def _flatten_boilerplate_groups(self, children: list):
        """Drop empty decoration <g> wrappers and lift the children of
        transformer-less, anonymous export-wrapper groups one level up,
        recursively.

        SVG export tools (Illustrator/Inkscape/Figma) usually wrap artwork
        in several layers of boilerplate <g> ("Layer 1", "Page-1", a bare
        <g> with no id). Those turn into useless empty folders here;
        flattening removes the nesting. Meaningful part names ("tail",
        "head", ...) survive as selectable containers.
        """
        out: list = []
        for obj in children:
            if obj.shape_type == "container":
                self._flatten_boilerplate_groups(obj.children)
            if obj.shape_type == "container" and not obj.is_mask:
                if not obj.children and self._is_degenerate_group(obj):
                    continue
                if (
                    obj.children
                    and self._is_degenerate_group(obj)
                    and self._RE_BOILERPLATE_GROUP.match(obj.name)
                ):
                    out.extend(obj.children)
                    continue
            out.append(obj)
        children[:] = out


def _rounded_rect_points(x, y, w, h, rx, ry) -> list:
    steps = 5
    corners = [
        (180, 270, x + rx, y + ry),
        (270, 360, x + w - rx, y + ry),
        (360, 450, x + w - rx, y + h - ry),
        (90, 180, x + rx, y + h - ry),
    ]
    pts = []
    for start, end, ccx, ccy in corners:
        for k in range(steps):
            a = math.radians(start + (end - start) * k / steps)
            pts.append((ccx + rx * math.cos(a), ccy + ry * math.sin(a)))
    pts.append(pts[0])
    return pts


def _shape_points(obj: SceneObject) -> list:
    """Yield the geometry's local-space points for a primitive shape
    (used to measure bounding boxes - rects/circles for every sample point)."""
    if obj.shape_type == "rect":
        w = obj.shape_data.get("width", 100) / 2
        h = obj.shape_data.get("height", 80) / 2
        return [(-w, -h), (w, -h), (w, h), (-w, h)]
    if obj.shape_type == "circle":
        r = obj.shape_data.get("radius", 50)
        return [(-r, -r), (r, -r), (r, r), (-r, r)]
    return list(obj.shape_data.get("points", []))


def _transform_affine(tr: Transform) -> tuple:
    """Affine matrix (a, b, c, d, e, f) for a stored TRS Transform."""
    ang = math.radians(tr.rotation)
    ca, sa = math.cos(ang), math.sin(ang)
    a = ca * tr.scale_x
    b = sa * tr.scale_x
    c = -sa * tr.scale_y
    d = ca * tr.scale_y
    return (a, b, c, d, tr.x, tr.y)


def _mult(m1, m2):
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1,
    )


def _apply(m, x, y):
    if m is None:
        return (x, y)
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _world_bbox(root: SceneObject) -> tuple:
    """Bounding box of a subtree in world space, composing every ancestor
    transform. Used to centre an imported SVG at the origin."""
    xs: list = []
    ys: list = []

    def walk(node: SceneObject, parent_affine: tuple | None):
        node_affine = _transform_affine(node.transform)
        m = node_affine if parent_affine is None else _mult(parent_affine, node_affine)
        for p in _shape_points(node):
            wx, wy = _apply(m, p[0], p[1])
            xs.append(wx)
            ys.append(wy)
        for c in node.children:
            walk(c, m)

    walk(root, None)
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


def import_svg_simple(path: str) -> SceneObject:
    """Import an SVG file into a single root container SceneObject.

    The file is converted to the editor's own simple primitives (rect,
    circle, polygon with flat fills); groups become containers. The root
    container's transform is set so the artwork's bounding box is centred on
    the origin, which places it at the centre of the editor's canvas.
    """
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        raise ValueError(f"Invalid SVG: {e}")
    root = tree.getroot()
    if _local(root.tag) != "svg":
        raise ValueError("Not an SVG file")

    importer = _Importer(root, filename=os.path.splitext(os.path.basename(path))[0])
    container = SceneObject(name=importer.filename, shape_type="container")
    for child in root:
        obj = importer._process_element(child, None)
        if obj is not None:
            container.children.append(obj)

    importer._flatten_boilerplate_groups(container.children)

    # Centre the artwork's bounding box on the world origin (camera center).
    min_x, min_y, max_x, max_y = _world_bbox(container)
    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2
    container.transform = Transform(x=-cx, y=-cy)

    return container