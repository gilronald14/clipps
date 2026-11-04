#!/usr/bin/env python3
"""Put a picture on screen when the speaker says something you can point at.

The rule this follows, from rules/proof.md:

    "If the piece names a real thing, show it." ... "Never fabricate."

So the only things that earn an overlay are things that are actually there:
a brand the speaker named, or a figure they said out loud. Nothing is invented
and nothing is inferred. A clip where nothing nameable is said gets no overlay
at all, which is the correct outcome rather than a failure.

Two deliberate limits:

  * It is restrained. One small picture in a corner, not a full-frame card.
    rules/motion.md is blunt that "an edit where every beat is a card is a
    slideshow with a face attached".
  * It lands ON the word. Arriving late is the most common way these read as
    sloppy, so the entrance starts just before the word and settles as it is
    spoken.

Compositing order matters and is enforced by the caller: this must be applied
BEFORE captions are burned, because anything applied after the caption pass
covers the captions.
"""
import json
import os
import re

import overlay_assets

# The entrance shape comes from the shared easing set. Optional: without it the
# old hand-rolled ramp is used and the graphic still appears.
try:
    import easing as _easing
except Exception:                                       # noqa: BLE001
    _easing = None

_HERE = os.path.dirname(os.path.abspath(__file__))
USER_DIR = os.path.join(_HERE, "assets", "overlays")
KEYWORD_FILE = os.path.join(USER_DIR, "keywords.json")

# Brands worth listening for. Kept as an explicit list rather than "everything
# in the icon index", because matching all ~3000 titles against ordinary speech
# hits words like "x", "steam" and "notion" constantly. These are the ones an
# interview about technology actually says.
BRANDS = [
    "meta", "facebook", "instagram", "whatsapp", "openai", "chatgpt", "gpt",
    "google", "alphabet", "youtube", "android", "microsoft", "windows",
    "apple", "iphone", "amazon", "aws", "nvidia", "netflix", "spotify",
    "tiktok", "twitter", "github", "tesla", "spacex", "paypal", "stripe",
    "shopify", "adobe", "figma", "notion", "slack", "zoom", "discord",
    "reddit", "linkedin", "snapchat", "roblox", "minecraft", "steam",
    "python", "linux", "bitcoin", "ethereum", "samsung", "sony", "ibm",
    "intel", "amd", "qualcomm", "uber", "airbnb", "coinbase", "dropbox",
]

# A spoken figure worth badging. Digits plus a magnifier, or a worded number
# with a unit -- both are things the speaker actually said.
_NUM_WORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
             r"twelve|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
             r"hundred)")
_NUM_PAT = re.compile(
    rf"(\d[\d,\.]*\s*(?:k|m|bn|billion|million|trillion|thousand|percent|%)"
    rf"|{_NUM_WORD}[\s-]{_NUM_WORD}*\s*"
    rf"(?:billion|million|thousand|percent|years?|months?|days?|hours?|weeks?|"
    rf"seconds?|minutes?|times)|\d+\s*(?:years?|months?|days?|hours?|percent|%))",
    re.I)


def load_keywords():
    """User triggers from assets/overlays/keywords.json.

    Either a list of words, or a list of {"match": ..., "asset": ...} so a
    phrase can point at a picture that is not named after it.
    """
    if not os.path.exists(KEYWORD_FILE):
        return []
    try:
        with open(KEYWORD_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:                                  # noqa: BLE001
        return []
    out = []
    for item in data if isinstance(data, list) else data.get("triggers", []):
        if isinstance(item, str):
            out.append({"match": item, "asset": item})
        elif isinstance(item, dict) and item.get("match"):
            out.append({"match": str(item["match"]),
                        "asset": str(item.get("asset") or item["match"])})
    return out


def _gap_ok(hits, t, min_gap):
    return all(abs(t - h["t"]) >= min_gap for h in hits)


def detect(words, min_gap=6.0, limit=6, brands=True, keywords=None,
           numbers=True, assets=True):
    """Pick the moments worth a picture. Returns a time-ordered hit list.

    Each hit is {"t", "name", "kind", "label"}. `t` is the moment the word is
    spoken, which is when the entrance should settle.
    """
    kw = keywords if keywords is not None else load_keywords()
    hits = []

    # --- 1. the user's own triggers win, and are allowed to be phrases
    for entry in kw:
        phrase = entry["match"].lower().strip()
        if not phrase:
            continue
        parts = phrase.split()
        for i, w in enumerate(words):
            if w.get("text", "").lower().strip(".,!?\"'") != parts[0]:
                continue
            # a multi-word trigger has to actually appear in order
            if len(parts) > 1:
                seq = [x.get("text", "").lower().strip(".,!?\"'")
                       for x in words[i:i + len(parts)]]
                if seq != parts:
                    continue
            if _gap_ok(hits, w["start"], min_gap):
                hits.append({"t": float(w["start"]), "name": entry["asset"],
                             "kind": "keyword", "label": entry["match"]})

    # --- 2. brands the speaker named
    if brands:
        for w in words:
            raw = w.get("text", "").strip(".,!?\"'").lower()
            if len(raw) < 3 or raw not in BRANDS:
                continue
            if _gap_ok(hits, w["start"], min_gap):
                hits.append({"t": float(w["start"]), "name": raw,
                             "kind": "brand", "label": w["text"].strip()})

    # --- 3. a figure they said out loud
    if numbers:
        text = " ".join(x.get("text", "") for x in words)
        for m in _NUM_PAT.finditer(text):
            phrase = m.group(1).strip()
            # find the word whose position matches this character offset
            consumed, t = 0, None
            for w in words:
                nxt = consumed + len(w.get("text", "")) + 1
                if consumed <= m.start() < nxt:
                    t = float(w["start"])
                    break
                consumed = nxt
            if t is None or not _gap_ok(hits, t, min_gap):
                continue
            label = re.sub(r"\s+", " ", phrase).upper()
            if len(label) > 18:
                continue
            hits.append({"t": t, "name": label, "kind": "number",
                         "label": label})

    hits.sort(key=lambda h: h["t"])
    if limit and len(hits) > limit:
        # keep them evenly spread rather than all from the opening
        step = len(hits) / float(limit)
        hits = [hits[int(i * step)] for i in range(limit)]

    if assets:
        kept = []
        for h in hits:
            p = (overlay_assets.badge_png(h["name"]) if h["kind"] == "number"
                 else overlay_assets.resolve(h["name"]))
            if p:
                h["png"] = p
                if h["kind"] == "brand":
                    c = overlay_assets.brand_colour(h["name"])
                    if c:
                        h["accent"] = c
                kept.append(h)
        return kept
    return hits


# --------------------------------------------------------------- ffmpeg side
# Fallback shape, used only if easing.py is unavailable. This was the original
# hand-rolled entrance: a smoothstep with a single sine hump standing in for an
# overshoot. It peaks at 1.037, which is barely a pop at all -- measured.
_OLD_SHAPE = "clip(t/{dur},0,1)"
_OLD_FULL = "0.62+0.38*({p}*{p}*(3-2*{p}))+0.13*sin(PI*{p})"


def scale_expr(dur=0.42):
    """A scale ramp that settles like a spring, in the input's own timeline.

    Uses the damped spring from the Lemo-Opuscar toolkit. A spring is what makes
    an entrance read as having weight: it overshoots, comes back, and rings
    down, rather than rising at a constant rate and stopping dead.

    The damping is 0.7, not the source's 0.35, because an entrance runs for a
    fixed time and then holds: at their damping the spring only reaches 1.0417
    by t=1, so the graphic would sit 4% oversized for the rest of its life.
    """
    if _easing is not None:
        return _easing.ffmpeg_spring(dur=dur)
    p = _OLD_SHAPE.format(dur=dur)
    return _OLD_FULL.format(p=p)


def build_filters(hits, target_h=112, x=64, bottom=240,
                  hold=2.5, pop=0.42, first_input=1, start_label="0:v"):
    """Filter-complex fragments that composite each hit under the captions.

    Returns (pre, chain, out_label) where `pre` transforms each extra input and
    `chain` overlays them one after another. Position is bottom-left, clear of
    the caption band and clear of the platform's own button column.
    """
    pre, chain = [], []
    cur = start_label
    for i, h in enumerate(hits):
        idx = first_input + i
        lbl = f"ov{i}"
        # The entrance settles AT the word, so it starts a touch early.
        t0 = max(0.0, float(h["t"]) - pop * 0.45)
        t1 = t0 + hold
        # The expressions MUST be single-quoted. They contain commas --
        # `clip(t/0.42,0,1)` -- and ffmpeg's filter parser takes an unquoted
        # comma as the start of the next filter or option, so it reports
        # "No option name near ..." and the whole graph fails to build.
        _sx = scale_expr(pop)
        pre.append(
            f"[{idx}:v]format=rgba,"
            f"scale=w='iw*({_sx})':h='ih*({_sx})':"
            f"eval=frame,"
            f"fade=t=in:st=0:d=0.10:alpha=1,"
            f"fade=t=out:st={max(0.0, hold - 0.16):.3f}:d=0.16:alpha=1,"
            f"setpts=PTS+{t0:.3f}/TB[{lbl}]")
        y = f"H-h-{bottom}"
        chain.append(
            f"[{cur}][{lbl}]overlay=x={x}:y={y}:eof_action=pass:"
            f"enable='between(t,{t0:.3f},{t1:.3f})'[v{i}]")
        cur = f"v{i}"
    return pre, chain, cur


def prepare_inputs(hits, target_h=112):
    """Extra ffmpeg inputs for the hits, scaled to a consistent height.

    Each is looped for its own life so the file is a still that behaves like a
    clip. `-t` bounds it; the overlay's enable window does the real timing.
    """
    args = []
    for h in hits:
        args += ["-loop", "1", "-t", "4", "-i", h["png"]]
    return args
