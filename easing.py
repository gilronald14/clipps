#!/usr/bin/env python3
"""Easing and interpolation, the same set used by the Lemo-Opuscar films.

Why this file exists: the motion code here had been hand-rolling its own
shapes. A punch-in used `4u(1-u)` and an overlay entrance used
`0.62 + 0.38*smoothstep(p) + 0.13*sin(PI*p)` -- a single sine hump standing in
for an overshoot. Those are guesses at an ease. This is the real thing.

Source: `core/lib.js` from github.com/lemomo-ai/lemo-opuscar (MIT, by LemoLab).
The maths is reimplemented here in Python and the comments are ours; the shapes
are theirs.

The two that matter most:

  * `spring(t)` -- a damped spring settling to 1, with one or two real rebounds.
    This is what makes an entrance feel like it has weight. A sine hump does
    not: it rises, dips and stops, all at a constant rate.
  * `monotone(keys)` -- Fritsch-Carlson monotone cubic interpolation. Smooth
    through every keyframe but guaranteed not to overshoot between them. That
    property is the whole point for a camera path: a plain cubic spline through
    the same keys dips below a low key and rises above a high one, so the
    camera drifts where you never asked it to go.

None of these take state: every value is a pure function of `t`, which is what
lets any frame be computed in any order.
"""
import math

# ---------------------------------------------------------------- basics
def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def lerp(a, b, t):
    return a + (b - a) * t


def seg(t, a, b):
    """Where t falls inside [a, b], as 0..1. The building block of everything."""
    if b == a:
        return 0.0
    return clamp((t - a) / (b - a))


# ------------------------------------------------------------- easings
def ss(t):
    """Smoothstep: 3t^2 - 2t^3. Eases both ends."""
    t = clamp(t)
    return t * t * (3 - 2 * t)


def eio(t):
    """Ease in-out, cubic."""
    t = clamp(t)
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def eo(t):
    """Ease out, cubic. Fast start, gentle stop."""
    return 1 - pow(1 - clamp(t), 3)


def ei(t):
    """Ease in, cubic. Gentle start, fast finish."""
    return pow(clamp(t), 3)


def back(t, s=1.8):
    """Overshoot: goes past 1, then settles back onto it.

    A heavier `s` overshoots further. Use for an entrance that should feel
    springy rather than smooth.
    """
    t = clamp(t) - 1
    return 1 + t * t * ((s + 1) * t + s)


def spring(t, k=13.0, z=0.7):
    """Damped spring settling to 1, with one or two rebounds.

    `k` is stiffness (higher = quicker), `z` is damping (lower = more bounce).
    Unlike `back`, which overshoots once and returns, this rings down the way a
    real object does.

    The defaults are NOT the source's (k=7, z=0.35) and the reason is worth
    knowing. A spring is asymptotic: it approaches 1 and never arrives. Their
    values are fine for a character that keeps moving, but an entrance runs for
    a fixed fraction of a second and then holds, and at t=1 their defaults have
    only reached **1.0417** -- measured. So the graphic would sit 4% too large
    for the rest of its life. Measured residuals at t=1:

        z=0.35 (source)  4.17% too big       z=0.70  0.11%
        z=0.50           2.91% too big       z=0.90  0.03%

    (k=13, z=0.7) peaks at 1.174 -- a visible pop -- and has settled by t=1.
    """
    t = max(0.0, t)
    return 1 - math.exp(-z * k * t) * math.cos(k * math.sqrt(1 - z * z) * t * 1.6)


def env(t, a, b, fi=0.3, fo=0.3):
    """A 0 -> 1 -> 0 envelope over [a, b], fading in and out at the edges.

    This is the shape of anything that appears, holds, and leaves -- an
    entrance, a pulse, a badge.
    """
    return min(seg(t, a, a + fi), 1 - seg(t, b - fo, b))


def pulse(t, centre, half_width):
    """A single smooth hump peaking at 1 in the middle of the window."""
    u = seg(t, centre - half_width, centre + half_width)
    return math.sin(math.pi * u) ** 2


# -------------------------------------------------------- deterministic noise
def mulberry(seed):
    """A seeded random source, so a frame is reproducible.

    `random.random()` is the wrong tool in a renderer: the same frame must come
    out identical every time, on any worker, in any order.
    """
    state = [seed & 0xFFFFFFFF]

    def rnd():
        state[0] = (state[0] + 0x6D2B79F5) & 0xFFFFFFFF
        t = state[0]
        t = (t ^ (t >> 15)) * (t | 1) & 0xFFFFFFFF
        t = (t ^ (t + ((t ^ (t >> 7)) * (t | 61) & 0xFFFFFFFF))) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0
    return rnd


def hashf(n):
    """A stable pseudo-random value per input. Same input, same output."""
    n = math.sin(n * 127.1 + 311.7) * 43758.5453
    return n - math.floor(n)


def vnoise(x):
    """Value noise: smooth, continuous, reproducible. For organic drift."""
    i = math.floor(x)
    f = x - i
    u = f * f * (3 - 2 * f)
    return lerp(hashf(i), hashf(i + 1), u)


# ------------------------------------------------------------ keyframes
def monotone(keys):
    """Fritsch-Carlson monotone cubic through (x, y) keys.

    Smooth through every key, and it never overshoots past one. Pass keys as
    [(t0, v0), (t1, v1), ...] and get back f(t).

    This is the right interpolator for a camera path or a zoom ramp. A normal
    cubic (Catmull-Rom, what you would reach for first) is smooth too, but it
    swings past the keys, and on a camera that means moving somewhere you never
    asked it to go. Measured against Catmull-Rom on the same keys, monotone
    stays inside the key range every time:

        keys                    monotone        Catmull-Rom
        0.0 0.0 1.0 0.0         0.000 .. 0.9999  -0.074 .. 0.9999   <- negative
        0.9 0.9 0.9 0.1         0.100 .. 0.9000   0.100 .. 0.9593
        0.3 0.31 0.32 0.9       0.300 .. 0.9000   0.274 .. 0.9000   <- below the
                                                                     lowest key
    """
    pts = sorted(keys, key=lambda k: k[0])
    n = len(pts)
    if n == 0:
        return lambda t: 0.0
    if n == 1:
        return lambda t: pts[0][1]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]

    d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) if xs[i + 1] != xs[i] else 0.0
         for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[n - 1] = d[0], d[n - 2]
    for i in range(1, n - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2
    for i in range(n - 1):
        if d[i] == 0.0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        s = a * a + b * b
        if s > 9:
            k = 3 / math.sqrt(s)
            m[i], m[i + 1] = k * a * d[i], k * b * d[i]

    def f(t):
        if t <= xs[0]:
            return ys[0]
        if t >= xs[-1]:
            return ys[-1]
        i = 0
        while i < n - 2 and t > xs[i + 1]:
            i += 1
        h = xs[i + 1] - xs[i]
        if h == 0:
            return ys[i]
        u = (t - xs[i]) / h
        u2, u3 = u * u, u * u * u
        return ((2 * u3 - 3 * u2 + 1) * ys[i]
                + (u3 - 2 * u2 + u) * h * m[i]
                + (-2 * u3 + 3 * u2) * ys[i + 1]
                + (u3 - u2) * h * m[i + 1])
    return f


def track(keys):
    """Multi-dimensional keyframes: [(t, [a, b, c]), ...] -> f(t) -> [a, b, c].

    Every dimension gets its own monotone curve, so a camera that moves in x, y
    and zoom at once stays smooth in all three.
    """
    if not keys:
        return lambda t: []
    dim = len(keys[0][1])
    fns = [monotone([(k[0], k[1][j]) for k in keys]) for j in range(dim)]
    return lambda t: [f(t) for f in fns]


# ------------------------------------------------------------ ffmpeg bridge
def ffmpeg_spring(dur=0.42, k=13.0, z=0.7, start=0.62, end=1.0):
    """`spring` as an ffmpeg expression, for animating `scale ... eval=frame`.

    Everything is written in terms of `t` so ffmpeg can evaluate it per frame.
    `exp` and `cos` are both available in ffmpeg's expression parser.
    """
    p = f"clip(t/{dur},0,1)"
    s = (f"(1-exp(-{z}*{k}*{p})*cos({k}*sqrt(1-{z}*{z})*{p}*1.6))")
    return f"({start}+({end}-{start})*{s})"


def ffmpeg_back(dur=0.34, s=1.8, start=0.62, end=1.0):
    """`back` as an ffmpeg expression (single overshoot, then settle)."""
    p = f"clip(t/{dur},0,1)"
    b = f"(1+({p}-1)*({p}-1)*(({s}+1)*({p}-1)+{s}))"
    return f"({start}+({end}-{start})*{b})"


def ffmpeg_pulse(centre, half_width=0.275, amount=0.10):
    """A smooth hump on the main timeline, as an ffmpeg expression.

    Written against absolute `t` so it lands where it was asked to, rather than
    measured from the start of whatever segment it is composed into.
    """
    a, b = centre - half_width, centre + half_width
    u = f"clip((t-{a:.6f})/{b - a:.6f},0,1)"
    return f"{amount}*pow(sin(PI*{u}),2)"
