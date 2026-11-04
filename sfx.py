#!/usr/bin/env python3
"""Procedural sound effects, so a visible action is not silent.

The rule this serves, from the Lemo-Opuscar director's notes:

    "Every visible action has a sound: landings, page turns, cuts, drips,
     clicks, cloth. Match the material."

A punch-in that arrives in silence, or a graphic that pops with no click, is
half the moment. This makes the other half.

Source: `core/audio/sfx.py` from github.com/lemomo-ai/lemo-opuscar (MIT, by
LemoLab). The sound designs are theirs -- the filter sweeps, the resonance
stacks, the envelopes. What is different here is the plumbing: their version
depends on scipy for its Butterworth filters and on soundfile to write, and
neither is installed on a stock Windows box. Rather than add ~40 MB of scipy to
what a user has to download, the biquad is implemented directly and the WAV is
written with the standard library. Same shapes, no new dependencies.

Everything is mono float32 at 48 kHz until the mix, which is stereo.
"""
import math
import struct
import wave

import numpy as np

SR = 48000
_rng = np.random.default_rng(7)


# ------------------------------------------------------------------ helpers
def t_(d):
    return np.arange(int(round(d * SR))) / SR


def env_exp(d, tau):
    """Exponential decay. The shape of anything that rings out."""
    return np.exp(-t_(d) / tau)


def noise(d):
    return _rng.standard_normal(int(round(d * SR)))


def norm(x, peak=1.0):
    m = np.abs(x).max()
    return x * (peak / m) if m > 0 else x


# ------------------------------------------------------- filters (no scipy)
def _biquad(x, b0, b1, b2, a0, a1, a2):
    """Direct-form II biquad, in a plain loop.

    Not vectorised, and that is fine: every sound here is under two seconds, so
    the worst case is about 80k iterations and it costs a few tens of
    milliseconds. A faster path would mean a dependency, which is the trade
    this file deliberately refuses.
    """
    b0, b1, b2 = b0 / a0, b1 / a0, b2 / a0
    a1, a2 = a1 / a0, a2 / a0
    y = np.empty_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for i in range(len(x)):
        x0 = x[i]
        v = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        y[i] = v
        x2, x1 = x1, x0
        y2, y1 = y1, v
    return y


def _coeffs(kind, f, q):
    f = max(10.0, min(f, SR * 0.45))
    w0 = 2 * math.pi * f / SR
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    if kind == "lp":
        return (1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha
    if kind == "hp":
        return (1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha
    # band-pass, constant peak gain
    return alpha, 0.0, -alpha, 1 + alpha, -2 * cw, 1 - alpha


def lp(x, f, q=0.707):
    return _biquad(x, *_coeffs("lp", f, q))


def hp(x, f, q=0.707):
    return _biquad(x, *_coeffs("hp", f, q))


def bp(x, lo, hi, q=0.707):
    """A band, as two passes: high-pass at the bottom, low-pass at the top."""
    return lp(hp(x, lo, q), hi, q)


def brown(d):
    w = np.cumsum(noise(d))
    if len(w) > 1:
        w -= np.linspace(w[0], w[-1], len(w))
    return norm(hp(w, 20))


# ------------------------------------------------------------------ sounds
def whoosh(d=0.35, v=1.0):
    """Air moving past: noise through a band that sweeps up and back.

    For a graphic arriving, or a cut you want felt rather than heard.
    """
    n = noise(d)
    t = t_(d)
    out = np.zeros_like(n)
    step = 480                                    # 10 ms blocks
    for i in range(0, len(n), step):
        hi = min(len(n), i + step)
        f = 600 + 2600 * math.sin(math.pi * i / len(n))
        tail = bp(n[max(0, i - 2000):hi], f * 0.7, f * 1.3)[-(hi - i):]
        tail = np.resize(tail, hi - i)
        out[i:hi] = tail
    return norm(out * np.sin(np.pi * t / d) ** 2) * v


def thump(v=1.0, f=70.0):
    """Weight landing: a sine dropping in pitch, plus a little low noise.

    For a punch-in. The pitch drop is what stops it sounding like a beep.
    """
    d = 0.35
    t = t_(d)
    body = np.sin(2 * math.pi * f * t * (1 - 0.3 * t)) * env_exp(d, 0.07)
    knock = lp(noise(d), 300) * env_exp(d, 0.02) * 0.5
    return norm(body + knock) * v


def pop(v=1.0):
    """A small thing appearing: a sine rising 500 -> 1900 Hz, short decay."""
    d = 0.18
    t = t_(d)
    f = 500 + 1400 * t / d
    return norm(np.sin(2 * math.pi * np.cumsum(f) / SR) * env_exp(d, 0.05)) * v


def click(pitch=1.0, v=1.0):
    """A hard small snap: very short transient plus two resonances."""
    d = 0.06
    t = t_(d)
    tr = hp(noise(d), 2500) * env_exp(d, 0.0015)
    res = sum(a * np.sin(2 * math.pi * f * pitch * t + _rng.random() * 6)
              * env_exp(d, tau)
              for f, a, tau in ((2900, 0.6, 0.012), (4600, 0.4, 0.008),
                                (1500, 0.35, 0.018)))
    body = np.sin(2 * math.pi * 190 * pitch * t) * env_exp(d, 0.01) * 0.5
    return norm(tr * 0.8 + res + body) * v


def ding(v=1.0):
    """A soft bell: five inharmonic partials decaying at different rates."""
    d = 1.6
    t = t_(d)
    x = sum(a * np.sin(2 * math.pi * f * t) * env_exp(d, tau)
            for f, a, tau in ((1318, 1, 0.6), (2637, 0.5, 0.35),
                              (3951, 0.3, 0.2), (1976, 0.25, 0.5),
                              (5274, 0.15, 0.1)))
    return norm(x) * v


def step(v=1.0):
    """A footfall on a hard floor: a tick with a bright ring."""
    d = 0.05
    t = t_(d)
    return norm(hp(noise(d), 1800) * env_exp(d, 0.004)
                + np.sin(2 * math.pi * 2200 * t) * env_exp(d, 0.006) * 0.5) * v


def rumble(d=1.5, v=1.0):
    """Low sustained bed, faded in and out."""
    return norm(lp(brown(d), 200)) * np.sin(np.pi * t_(d) / d / 2) ** 2 * v


# -------------------------------------------------------------------- mix
def compress(x, thr=0.25, ratio=3.5, att=0.004, rel=0.08):
    """A simple compressor, so one loud hit does not swamp the bed."""
    a = math.exp(-1 / (att * SR))
    r = math.exp(-1 / (rel * SR))
    out = np.empty_like(x)
    env = 0.0
    ax = np.abs(x)
    for i in range(len(x)):
        c = ax[i]
        env = a * env + (1 - a) * c if c > env else r * env + (1 - r) * c
        g = (thr + (env - thr) / ratio) / env if env > thr and env > 1e-9 else 1.0
        out[i] = x[i] * g
    return out


def limit(x, ceil=0.9, look_ms=2.0):
    """Peak ceiling so the mix cannot clip. No look-ahead, by design: a real
    look-ahead needs scipy's maximum_filter, and the difference is inaudible on
    short effects."""
    peak = np.abs(x).max()
    return x * (ceil / peak) if peak > ceil else x


def bed(seconds, channels=2):
    """An empty stereo buffer to add sounds into."""
    return np.zeros((int(round(seconds * SR)), channels), dtype=np.float32)


def add(buf, x, at, gain=1.0, pan=0.0):
    """Place a mono sound into a stereo bed at `at` seconds.

    `pan` -1 is hard left, +1 hard right. Equal-power, so a centred sound does
    not dip in level as it moves.
    """
    s = int(at * SR)
    if s >= len(buf) or s + len(x) <= 0:
        return
    if s < 0:
        x = x[-s:]
        s = 0
    e = min(len(buf), s + len(x))
    l = math.cos((pan + 1) * math.pi / 4)
    r = math.sin((pan + 1) * math.pi / 4)
    buf[s:e, 0] += x[:e - s] * gain * l
    buf[s:e, 1] += x[:e - s] * gain * r


def write_wav(path, buf, sr=SR):
    """Write a float array as 16-bit PCM WAV, with the standard library.

    soundfile would do it in one line; this avoids the dependency. Accepts mono
    (n,) or stereo (n, 2).
    """
    a = np.asarray(buf)
    if a.ndim == 1:
        a = a[:, None]
    a = np.clip(a, -1.0, 1.0)
    pcm = (a * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(a.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def read_wav(path):
    """Read a 16-bit PCM WAV back as float32. For the mixer and for tests."""
    with wave.open(path, "rb") as w:
        n, ch, sr = w.getnframes(), w.getnchannels(), w.getframerate()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    if ch > 1:
        a = a.reshape(-1, ch)
    return a, sr
