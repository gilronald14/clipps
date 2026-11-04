#!/usr/bin/env python3
"""Rasterise a simple SVG path to a PNG, using only Pillow.

Why this exists: the two free icon sets worth having (Simple Icons, CC0, and
Lucide, ISC) are published as SVG. FFmpeg on a normal Windows build has no SVG
decoder and nothing on this machine can rasterise one either -- no librsvg, no
cairosvg, no Inkscape, and the `convert` on PATH is Windows' own FAT-to-NTFS
tool rather than ImageMagick. Rather than take on a dependency for a single
glyph, this fills the path itself.

It is deliberately small: it handles the subset those icon sets actually use --
M L H V C S A Z in both cases -- and nothing else. It is not a general SVG
renderer and should not be treated as one.

The fill is even-odd by scanline rather than Pillow's polygon(), because these
glyphs have counters: the hole in a "o" or the bowl of a "g" has to stay empty.
polygon() would fill them solid.

    python svg_raster.py in.svg out.png --size 256
"""
import math
import re
import sys

from PIL import Image, ImageDraw

# ---------------------------------------------------------------- path data
_NUM_RE = re.compile(r"[+-]?(?:\d*\.\d+|\d+\.?)(?:[eE][+-]?\d+)?")


def parse_path(d):
    """SVG path data -> a list of subpaths, each a list of (x, y) points.

    Scanned character by character rather than by splitting the string into
    tokens with one number regex. The reason is arc flags: `a4 4 0 01-3-4` is
    rotation 0, large-arc-flag 0, sweep-flag 1, then dx -3, dy -4. That `01` is
    two single-character flags with no separator, which is legal and is how
    minifiers emit it. A generic number regex reads it as the number 1, the arc
    is then short of an argument and gets dropped -- silently deleting part of
    the artwork rather than failing loudly.
    """
    i, n = 0, len(d or "")
    out, cur = [], []
    x = y = sx = sy = 0.0
    prev_c2 = None

    def skip():
        nonlocal i
        while i < n and d[i] in " ,\t\r\n":
            i += 1

    def num():
        nonlocal i
        skip()
        m = _NUM_RE.match(d, i)
        if not m:
            return None
        i = m.end()
        return float(m.group(0))

    def flag():
        nonlocal i
        skip()
        if i < n and d[i] in "01":
            v = int(d[i])
            i += 1
            return v
        return None

    def flush():
        # >= 2, not > 2: a two-point subpath is a straight line, and these icon
        # sets are full of them -- the stem and dot of an exclamation mark.
        if len(cur) >= 2:
            out.append(list(cur))
        cur.clear()

    def rollback(to):
        nonlocal i
        i = to

    while i < n:
        skip()
        if i >= n:
            break
        c = d[i]
        if c not in "MmLlHhVvCcSsQqTtAaZz":
            i += 1
            continue
        i += 1
        rel = c.islower()
        C = c.upper()

        if C == "M":
            a, b = num(), num()
            if a is None or b is None:
                continue
            flush()
            x, y = a + (x if rel else 0), b + (y if rel else 0)
            sx, sy = x, y
            cur.append((x, y))
            prev_c2 = None
            while True:                    # extra pairs are implicit lineto
                mark = i
                a, b = num(), num()
                if a is None or b is None:
                    rollback(mark)
                    break
                x = a + (x if rel else 0)
                y = b + (y if rel else 0)
                cur.append((x, y))
        elif C == "L":
            while True:
                mark = i
                a, b = num(), num()
                if a is None or b is None:
                    rollback(mark)
                    break
                x = a + (x if rel else 0)
                y = b + (y if rel else 0)
                cur.append((x, y))
            prev_c2 = None
        elif C == "H":
            while True:
                mark = i
                a = num()
                if a is None:
                    rollback(mark)
                    break
                x = a + (x if rel else 0)
                cur.append((x, y))
            prev_c2 = None
        elif C == "V":
            while True:
                mark = i
                a = num()
                if a is None:
                    rollback(mark)
                    break
                y = a + (y if rel else 0)
                cur.append((x, y))
            prev_c2 = None
        elif C == "C":
            while True:
                mark = i
                a = [num() for _ in range(6)]
                if any(v is None for v in a):
                    rollback(mark)
                    break
                ox, oy = x, y
                x1 = a[0] + (x if rel else 0); y1 = a[1] + (y if rel else 0)
                x2 = a[2] + (x if rel else 0); y2 = a[3] + (y if rel else 0)
                x = a[4] + (x if rel else 0);  y = a[5] + (y if rel else 0)
                cur.extend(_cubic(ox, oy, x1, y1, x2, y2, x, y))
                prev_c2 = (x2, y2)
        elif C == "S":
            while True:
                mark = i
                a = [num() for _ in range(4)]
                if any(v is None for v in a):
                    rollback(mark)
                    break
                ox, oy = x, y
                x1, y1 = ((2 * x - prev_c2[0], 2 * y - prev_c2[1])
                          if prev_c2 else (x, y))
                x2 = a[0] + (x if rel else 0); y2 = a[1] + (y if rel else 0)
                x = a[2] + (x if rel else 0);  y = a[3] + (y if rel else 0)
                cur.extend(_cubic(ox, oy, x1, y1, x2, y2, x, y))
                prev_c2 = (x2, y2)
        elif C == "A":
            while True:
                mark = i
                rx, ry = num(), num()
                rot = num()
                large, sweep = flag(), flag()
                nx, ny = num(), num()
                if None in (rx, ry, rot, large, sweep, nx, ny):
                    rollback(mark)
                    break
                ox, oy = x, y
                x = nx + (x if rel else 0)
                y = ny + (y if rel else 0)
                cur.extend(_arc(ox, oy, abs(rx), abs(ry), rot, large, sweep,
                                x, y))
                prev_c2 = None
        elif C == "Z":
            if cur:
                cur.append((sx, sy))
                x, y = sx, sy
            flush()
            prev_c2 = None
        else:
            # Q/T are not used by these icon sets; drop their arguments
            while num() is not None:
                pass
    flush()
    return out


def _cubic(x0, y0, x1, y1, x2, y2, x3, y3, steps=16):
    pts = []
    for k in range(1, steps + 1):
        t = k / steps
        u = 1 - t
        pts.append((u*u*u*x0 + 3*u*u*t*x1 + 3*u*t*t*x2 + t*t*t*x3,
                    u*u*u*y0 + 3*u*u*t*y1 + 3*u*t*t*y2 + t*t*t*y3))
    return pts


def _arc(x0, y0, rx, ry, rot_deg, large, sweep, x1, y1, steps=24):
    """Endpoint arc -> points, via the centre parameterisation in the spec."""
    import math
    if rx == 0 or ry == 0:
        return [(x1, y1)]
    phi = math.radians(rot_deg)
    cp, sp = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    x1p, y1p = cp * dx2 + sp * dy2, -sp * dx2 + cp * dy2
    rx, ry = abs(rx), abs(ry)
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam); rx *= s; ry *= s
    num = rx*rx*ry*ry - rx*rx*y1p*y1p - ry*ry*x1p*x1p
    den = rx*rx*y1p*y1p + ry*ry*x1p*x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x0 + x1) / 2.0
    cy = sp * cxp + cp * cyp + (y0 + y1) / 2.0
    def ang(ux, uy, vx, vy):
        import math
        d = math.hypot(ux, uy) * math.hypot(vx, vy)
        if d == 0:
            return 0.0
        a = math.acos(max(-1.0, min(1.0, (ux*vx + uy*vy) / d)))
        return -a if (ux*vy - uy*vx) < 0 else a
    th0 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry,
              (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dth > 0:
        dth -= 2 * math.pi
    elif sweep and dth < 0:
        dth += 2 * math.pi
    pts = []
    for k in range(1, steps + 1):
        th = th0 + dth * k / steps
        px, py = rx * math.cos(th), ry * math.sin(th)
        pts.append((cp * px - sp * py + cx, sp * px + cp * py + cy))
    return pts


# ------------------------------------------------------------------- fill
def fill_path(subpaths, size, pad_ratio=0.0):
    """Even-odd scanline fill of the subpaths into an 8-bit mask."""
    xs = [p[0] for sp in subpaths for p in sp]
    ys = [p[1] for sp in subpaths for p in sp]
    if not xs:
        return Image.new("L", (size, size), 0)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    pad = pad_ratio * size
    scale = (size - 2 * pad) / max(w, h)
    ox = pad + (size - 2 * pad - w * scale) / 2 - minx * scale
    oy = pad + (size - 2 * pad - h * scale) / 2 - miny * scale

    scaled = [[(p[0] * scale + ox, p[1] * scale + oy) for p in sp] for sp in subpaths]

    mask = Image.new("1", (size, size), 0)
    px = mask.load()
    edges = []
    for sp in scaled:
        for k in range(len(sp)):
            x0, y0 = sp[k]
            x1, y1 = sp[(k + 1) % len(sp)]
            if y0 != y1:
                edges.append((x0, y0, x1, y1))
    for row in range(size):
        yc = row + 0.5
        xs_hit = []
        for x0, y0, x1, y1 in edges:
            if (y0 <= yc < y1) or (y1 <= yc < y0):
                t = (yc - y0) / (y1 - y0)
                xs_hit.append(x0 + t * (x1 - x0))
        xs_hit.sort()
        for k in range(0, len(xs_hit) - 1, 2):       # even-odd
            a, b = int(xs_hit[k] + 0.5), int(xs_hit[k + 1] + 0.5)
            for col in range(max(0, a), min(size, b)):
                px[col, row] = 1
    return mask.convert("L").point(lambda v: 255 if v else 0)


# ------------------------------------------------- the other shape elements
# Lucide draws plenty of its icons from these rather than from a path -- its
# `monitor` is a <rect> plus a <line>, with no <path d=...> at all.
def _rect_subpath(x, y, w, h, rx):
    if rx <= 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    pts = []
    for cx, cy, a0 in ((x + w - rx, y + rx, -90), (x + w - rx, y + h - rx, 0),
                       (x + rx, y + h - rx, 90), (x + rx, y + rx, 180)):
        end = math.radians(a0 + 90)
        pts.extend(_arc(cx, cy, rx, rx, 0, 0, 1,
                        cx + rx * math.cos(end), cy + rx * math.sin(end),
                        steps=5))
    return pts


def _circle_subpath(cx, cy, rx, ry=None):
    return _arc(cx + rx, cy, rx, ry or rx, 0, 0, 1, cx - rx, cy, steps=36) + \
           _arc(cx - rx, cy, rx, ry or rx, 0, 0, 1, cx + rx, cy, steps=36)


def _points_subpath(raw, close):
    nums = [float(n) for n in re.findall(r"-?\d*\.?\d+", raw or "")]
    pts = list(zip(nums[0::2], nums[1::2]))
    return pts + ([pts[0]] if close and pts else [])


def _attr(tag, name, default=None):
    """A raw attribute string, or `default` when the attribute is absent."""
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag)
    if not m:
        m = re.search(rf"\b{name}\s*=\s*'([^']*)'", tag)
    return m.group(1).strip() if m else default


def _fattr(tag, name, default=0.0):
    """An attribute as a float. Separate from _attr on purpose: a path's `d`
    starts with digits and would otherwise be coerced, silently turning a whole
    outline into one enormous number."""
    v = _attr(tag, name)
    if v is None:
        return default
    try:
        return float(re.sub(r"[^0-9.\-]", "", v) or default)
    except ValueError:
        return default


def parse_svg(svg_text):
    """A whole SVG -> (subpaths, stroke_width or None).

    `stroke_width` is None when the artwork is solid (Simple Icons): those are
    filled. When it is a number the artwork is drawn as an outline (Lucide),
    and filling it instead produces a solid blob where the icon should be a
    ring -- which is exactly what a clock looks like if you get this wrong.
    """
    subpaths = []

    def add(tag, pts):
        if len(pts) > 1:
            subpaths.append(pts)

    for m in re.finditer(r"<path[^>]*>", svg_text):
        d = _attr(m.group(0), "d", "")
        if d:
            subpaths.extend(parse_path(d))
    for m in re.finditer(r"<rect[^>]*>", svg_text):
        t = m.group(0)
        add(t, _rect_subpath(_fattr(t, "x"), _fattr(t, "y"),
                             _fattr(t, "width"), _fattr(t, "height"),
                             _fattr(t, "rx") or _fattr(t, "ry")))
    for m in re.finditer(r"<circle[^>]*>", svg_text):
        t = m.group(0)
        add(t, _circle_subpath(_fattr(t, "cx"), _fattr(t, "cy"),
                               _fattr(t, "r")))
    for m in re.finditer(r"<ellipse[^>]*>", svg_text):
        t = m.group(0)
        add(t, _circle_subpath(_fattr(t, "cx"), _fattr(t, "cy"),
                               _fattr(t, "rx"), _fattr(t, "ry")))
    for m in re.finditer(r"<line[^>]*>", svg_text):
        t = m.group(0)
        add(t, [(_fattr(t, "x1"), _fattr(t, "y1")),
                (_fattr(t, "x2"), _fattr(t, "y2"))])
    for m in re.finditer(r"<polyline[^>]*>", svg_text):
        add(m.group(0), _points_subpath(_attr(m.group(0), "points", ""), False))
    for m in re.finditer(r"<polygon[^>]*>", svg_text):
        add(m.group(0), _points_subpath(_attr(m.group(0), "points", ""), True))

    if not subpaths:
        return [], None

    # stroke mode: the root carries fill="none" and a stroke, or any element
    # declares a stroke-width
    stroked = bool(re.search(r'fill\s*=\s*"(none|transparent)"', svg_text)) or \
        bool(re.search(r'stroke\s*=\s*"(?!none)[^"]+"', svg_text))
    if not stroked:
        return subpaths, None
    sw = 2.0
    for m in re.finditer(r'stroke-width\s*=\s*"?([0-9.]+)', svg_text):
        sw = float(m.group(1))
        break
    vb = re.search(r'viewBox\s*=\s*"([^"]+)"', svg_text)
    span = 24.0
    if vb:
        nums = [float(n) for n in re.findall(r"-?\d*\.?\d+", vb.group(1))]
        if len(nums) == 4 and nums[2] > 0:
            span = nums[2]
    return subpaths, sw * (256.0 / span) / 256.0    # width as a fraction of size


def _fit(subpaths, size, pad_ratio=0.0):
    """Subpaths -> pixel points, scaled to fill the canvas."""
    xs = [p[0] for sp in subpaths for p in sp]
    ys = [p[1] for sp in subpaths for p in sp]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    pad = pad_ratio * size
    scale = (size - 2 * pad) / max(w, h)
    ox = pad + (size - 2 * pad - w * scale) / 2 - minx * scale
    oy = pad + (size - 2 * pad - h * scale) / 2 - miny * scale
    return [[(p[0] * scale + ox, p[1] * scale + oy) for p in sp]
            for sp in subpaths]


def stroke_paths(subpaths, size, width_px, pad_ratio=0.06):
    """Draw the subpaths as outlines -- the correct treatment for Lucide."""
    scaled = _fit(subpaths, size, pad_ratio)
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    w = max(2, int(round(width_px)))
    for sp in scaled:
        if len(sp) < 2:
            continue
        d.line([tuple(p) for p in sp], fill=255, width=w, joint="curve")
        for p in (sp[0], sp[-1]):                 # round caps
            d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2],
                      fill=255)
    return mask


def rasterise(svg_text, size=256, colour=(255, 255, 255)):
    """An SVG string -> an RGBA Image of the artwork in `colour`.

    Solid artwork is filled with an even-odd rule; outlined artwork is stroked.
    Getting this wrong is not a subtle rendering difference: a stroked clock
    filled instead of stroked comes out as a solid wedge.
    """
    subpaths, stroke_w = parse_svg(svg_text)
    if not subpaths:
        return None
    if stroke_w is None:
        mask = fill_path(subpaths, size)
    else:
        pad = 0.06
        mask = stroke_paths(subpaths, size, stroke_w * size, pad)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    solid = Image.new("RGBA", (size, size), colour + (255,))
    img.paste(solid, (0, 0), mask)
    return img


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("svg")
    ap.add_argument("out")
    ap.add_argument("--size", type=int, default=256)
    ap.add_argument("--hex", default="ffffff")
    a = ap.parse_args()
    r, g, b = (int(a.hex[i:i+2], 16) for i in (0, 2, 4))
    img = rasterise(open(a.svg, encoding="utf-8").read(), a.size, (r, g, b))
    if img is None:
        raise SystemExit("no <path d=...> found")
    img.save(a.out)
    print(f"-> {a.out}  {img.size}")


if __name__ == "__main__":
    main()
