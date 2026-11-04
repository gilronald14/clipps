#!/usr/bin/env python3
"""Find or build the picture that goes on screen for a spoken name.

Resolution order, and it matters:

  1. `assets/overlays/<name>.png` — the user's own file. Always wins, so
     anything dropped in there is used verbatim, at its own colours.
  2. the local cache, if this name was fetched before.
  3. Simple Icons (CC0, public domain) — brand marks, each carrying the brand's
     official colour, so a badge can theme itself to whatever it names.
     Lucide (ISC) is the fallback for concepts with no brand: a clock for
     urgency, a moon for overnight.

Nothing is bundled. Icons are fetched on demand and cached, and every one is
either public domain or ISC, so the licence stays traceable and the repository
stays small.

Pillow draws two things that need no download at all — a number badge, and a
colour emoji taken from the Windows emoji font — so the feature works with no
network the first time it runs.

    python overlay_assets.py fetch meta openai clock --set lucide
    python overlay_assets.py list
"""
import argparse
import json
import os
import re
import sys
import urllib.request

from PIL import Image, ImageDraw, ImageFont

import svg_raster

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(_HERE, "assets", "icons")
USER = os.path.join(_HERE, "assets", "overlays")

SIMPLE_ICONS = "https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/{}.svg"
SIMPLE_INDEX = ("https://cdn.jsdelivr.net/npm/simple-icons@latest/data/"
                "simple-icons.json")
LUCIDE = "https://cdn.jsdelivr.net/npm/lucide-static@latest/icons/{}.svg"

# Names people actually say, mapped to the icon that means them. Without this
# "Facebook" finds nothing while "meta" works, which reads as a bug.
ALIASES = {
    "facebook": "meta", "fb": "meta", "instagram": "instagram",
    # spoken forms whose own name is not an icon slug anywhere: these five are
    # said constantly in an interview but resolve to nothing without a mapping
    "chatgpt": "openai", "gpt": "openai", "open ai": "openai",
    "alphabet": "google", "iphone": "apple", "aws": "amazonwebservices",
    "google": "google", "youtube": "youtube",
    "tiktok": "tiktok", "twitter": "x", "x": "x", "apple": "apple",
    "amazon": "amazon", "aws": "amazonwebservices", "microsoft": "microsoft",
    "windows": "windows", "nvidia": "nvidia", "netflix": "netflix",
    "spotify": "spotify", "github": "github", "tesla": "tesla", "meta": "meta",
    "android": "android", "linux": "linux", "python": "python",
    "whatsapp": "whatsapp", "linkedin": "linkedin", "reddit": "reddit",
    "discord": "discord", "slack": "slack", "zoom": "zoom",
    "roblox": "roblox", "minecraft": "minecraft", "steam": "steam",
    "paypal": "paypal", "stripe": "stripe", "shopify": "shopify",
    "adobe": "adobe", "figma": "figma", "notion": "notion",
}

# Concepts with no brand take an interface icon instead of a word.
CONCEPT_ICONS = {
    "urgency": "clock", "time": "clock", "clock": "clock",
    "overnight": "moon", "night": "moon", "moon": "moon",
    "idea": "lightbulb", "lightbulb": "lightbulb", "growth": "trending-up",
    "money": "circle-dollar-sign", "warning": "triangle-alert",
    "search": "search", "warning2": "alert-triangle", "lock": "lock",
    "chart": "chart-line", "target": "target", "rocket": "rocket",
}


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "shortsmith"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def slug(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def cached(name):
    p = os.path.join(CACHE, slug(name) + ".png")
    return p if os.path.exists(p) else None


def user_file(name):
    """The user's own image, if they supplied one under that name."""
    if not os.path.isdir(USER):
        return None
    want = slug(name)
    for f in os.listdir(USER):
        stem, ext = os.path.splitext(f)
        if ext.lower() in (".png", ".webp", ".jpg", ".jpeg") and slug(stem) == want:
            return os.path.join(USER, f)
    return None


def fetch(name, kind="brand", size=256):
    """Download one icon and rasterise it into the cache. Returns the path.

    The local filename is the slug, but the remote name keeps its hyphens:
    Lucide's files really are called `circle-dollar-sign.svg` and
    `trending-up.svg`, so slugging the name before building the URL 404s on
    every multi-word icon.
    """
    s = slug(name)
    remote = (name or "").lower().strip()
    os.makedirs(CACHE, exist_ok=True)
    dest = os.path.join(CACHE, s + ".png")
    if os.path.exists(dest):
        return dest
    urls = ([SIMPLE_ICONS.format(remote)] if kind == "brand" else []) + \
           [LUCIDE.format(remote)]
    for url in urls:
        try:
            svg = _get(url)
        except Exception:                              # noqa: BLE001
            continue
        img = svg_raster.rasterise(svg, size)
        if img is None:
            continue
        img.save(dest)
        return dest
    return None


def brand_colour(name):
    """The brand's official hex, from the Simple Icons index (cached)."""
    p = os.path.join(CACHE, "_index.json")
    try:
        if not os.path.exists(p):
            os.makedirs(CACHE, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(_get(SIMPLE_INDEX, timeout=40))
        with open(p, encoding="utf-8") as f:
            idx = json.load(f)
    except Exception:                                  # noqa: BLE001
        return None
    s = slug(name)
    for e in idx:
        if slug(e.get("title", "")) == s or slug(e.get("slug", "")) == s:
            hx = (e.get("hex") or "").lstrip("#")
            if len(hx) == 6:
                return tuple(int(hx[i:i+2], 16) for i in (0, 2, 4))
    return None


def emoji_png(char, size=220, font="C:/Windows/Fonts/seguiemj.ttf",
              cache_dir=None):
    """A colour emoji rendered from the system emoji font. No download."""
    key = "_emoji_" + str(ord(char[0])) if char else "_emoji"
    dest = os.path.join(cache_dir or CACHE, key + ".png")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        return dest
    try:
        f = ImageFont.truetype(font, int(size * 0.8))
    except OSError:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((size * 0.06, size * 0.04), char, font=f, embedded_color=True)
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
        side = max(img.size)
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
        img = sq
    img.save(dest)
    return dest


def badge_png(text, accent=(255, 215, 0), size=88, cache_dir=None,
              font="C:/Windows/Fonts/arialbd.ttf"):
    """A rounded pill carrying a number or short phrase. No download.

    This is the restrained shape the rules ask for: a small badge in a corner
    for a figure the speaker said, rather than a full-frame card.
    """
    text = (text or "").upper().strip()
    if not text:
        return None
    key = "_badge_" + slug(text)[:40] + "_" + "%02x%02x%02x" % accent
    dest = os.path.join(cache_dir or CACHE, key + ".png")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        return dest
    try:
        f = ImageFont.truetype(font, int(size * 0.72))
    except OSError:
        f = ImageFont.load_default()
    probe = Image.new("RGBA", (8, 8))
    pad_x, pad_y = int(size * 0.42), int(size * 0.22)
    box = ImageDraw.Draw(probe).textbbox((0, 0), text, font=f)
    w = (box[2] - box[0]) + pad_x * 2
    h = size + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = h // 2
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r,
                        fill=(14, 16, 20, 232), outline=accent + (255,), width=5)
    d.text((pad_x - box[0], pad_y - box[1]), text, font=f, fill=accent + (255,))
    img.save(dest)
    return dest


# Words that carry no meaning for a lookup. Speech arrives as phrases -- "a
# moon", "the clock", "like Meta" -- and looking the whole phrase up finds
# nothing, which reads as the trigger being broken rather than the phrase being
# wordy.
_STOP = {"a", "an", "the", "like", "and", "or", "to", "of", "in", "on", "at",
         "for", "with", "is", "are", "was", "it", "this", "that", "my", "your",
         "his", "her", "their", "our", "some", "any", "so", "very", "really"}


def candidates(name):
    """Lookup keys to try for a spoken phrase, most specific first."""
    raw = (name or "").lower().strip()
    out = []
    for cand in (raw,
                 " ".join(w for w in raw.split() if w not in _STOP),
                 *[w for w in raw.split() if w not in _STOP and len(w) > 2]):
        cand = cand.strip()
        if cand and cand not in out:
            out.append(ALIASES.get(cand, cand))
    return out


def resolve(name, kind="auto"):
    """Best available picture for `name`: user file, cache, then download."""
    if not name:
        return None
    for key in candidates(name):
        u = user_file(key)
        if u:
            return u
    for key in candidates(name):
        c = cached(key)
        if c:
            return c
    for key in candidates(name):
        if kind in ("auto", "brand"):
            p = fetch(key, "brand")
            if p:
                return p
    for key in candidates(name):
        p = fetch(CONCEPT_ICONS.get(key, key), "concept")
        if p:
            return p
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch", help="download and rasterise icons")
    f.add_argument("names", nargs="+")
    f.add_argument("--set", default="brand", choices=["brand", "concept"])
    sub.add_parser("list", help="show what is cached")
    a = ap.parse_args()
    if a.cmd == "fetch":
        for n in a.names:
            p = fetch(n, a.set)
            print(f"  {n:<20} {'-> ' + p if p else 'NOT FOUND'}")
    else:
        os.makedirs(CACHE, exist_ok=True)
        got = sorted(x for x in os.listdir(CACHE) if x.endswith(".png"))
        print(f"  cache: {CACHE}  ({len(got)} file(s))")
        for g in got[:40]:
            print("   ", g)


if __name__ == "__main__":
    sys.exit(main())
