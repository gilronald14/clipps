#!/usr/bin/env python3
"""Offline highlight ranking — no LLM, no API key, no network.

This exists so the pipeline can run fully unattended: the transcript-driven
ranker in highlights_llm.py needs a model, and on this box the only inference
credential is the user's Nous Portal token, which expires hourly and is Hermes's
to refresh. A "drop a video and wait" app cannot depend on that, so this module
scores candidate windows from signals that are already on disk.

It ranks by what a human picking clips would notice, in rough order:
  * speech DENSITY      — talking beats dead air
  * audio ENERGY        — a raised voice is usually a raised point
  * HOOK language       — questions, absolutes, numbers, second person
  * STANDALONE-ness     — opens at a sentence start, closes at a sentence end
  * quiet-in-the-middle — long pauses inside a window cost it

Quality is below the LLM ranker (it cannot tell a good point from a loud one),
so its output is marked `"ranker": "heuristic"` and the caller is expected to
say so. Boundaries still snap to sentence ends, which is what actually keeps
clips from starting mid-thought.

CLI:
    python highlights_heuristic.py --transcript T.json --source VIDEO \
        [--clips 7] [--min 20] [--max 45] --out highlights.json
"""
import argparse
import json
import os
import re
import subprocess

import numpy as np

# Landing in these ranges is what makes a clip feel like a clip.
TARGET_MIN_SEC = 20.0
TARGET_MAX_SEC  = 45.0
TARGET_NICE_SEC = 30.0        # windows are scored with a gentle pull toward this

SENTENCE_END = ".?!"
MAX_UNIT_SEC = 14.0           # flush a "sentence" that runs this long unpunctuated

# `you`/`we` keep it personal; the rest are the usual markers of a claim worth
# clipping. Deliberately shallow — this is a tie-breaker, not comprehension.
_HOOKS = [
    (r"\?", 3.0),
    (r"\b(never|nobody|noth?ing|everyone|always|every|impossible)\b", 1.6),
    (r"\b(the (thing|problem|point|reason|hard part) is)\b", 2.2),
    (r"\b(what if|imagine|think about)\b", 2.0),
    (r"\b\d+(\.\d+)?\s*(%|percent|x|years?|minutes?|hours?|people|billion|million|thousand)\b", 1.4),
    (r"\b(you|your|we|our|I)\b", 0.7),
    (r"\b(best|worst|biggest|hardest|most|least|only)\b", 1.3),
    (r"\b(because|so that|which means|that's why)\b", 1.0),
    (r"\b(actually|in fact|the truth is|honestly)\b", 1.2),
]
_HOOK_RE = [(re.compile(p, re.I), w) for p, w in _HOOKS]


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def rms_envelope(path, hop=0.25, keep=None):
    """Per-`hop` RMS of the mono 16 kHz signal, for the whole file.

    Decoded through ffmpeg rather than PyAV: this build of PyAV calls an
    av.open() argument faster-whisper 1.2.1 no longer passes.
    """
    if keep is None:
        keep = {2} if False else None
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
         "-f", "s16le", "-"], capture_output=True, timeout=3600).stdout
    if not raw:
        return np.zeros(1, dtype=np.float32), hop
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    n = max(1, int(hop * 16000))
    a = a[: len(a) // n * n].reshape(-1, n)
    return np.sqrt((a ** 2).mean(axis=1)).astype(np.float32), hop


def sentence_units(segments):
    """Group transcript segments into sentences, capped at MAX_UNIT_SEC."""
    units, cur = [], None
    for s in segments:
        txt = (s.get("text") or "").strip()
        if not txt:
            continue
        if cur is None:
            cur = {"start": float(s["start"]), "end": float(s["end"]), "text": txt}
        else:
            cur["end"] = float(s["end"])
            cur["text"] = cur["text"] + " " + txt
        long_enough = (cur["end"] - cur["start"]) >= MAX_UNIT_SEC
        if txt[-1] in SENTENCE_END or long_enough:
            units.append(cur)
            cur = None
    if cur:
        units.append(cur)
    return units


def _norm(vals):
    """Rank-normalise to 0..1 so no single component dominates by units alone."""
    a = np.asarray(vals, dtype=np.float32)
    if a.size == 0:
        return a
    lo, hi = float(a.min()), float(a.max())
    if hi - lo < 1e-9:
        return np.zeros_like(a)
    return (a - lo) / (hi - lo)


def rank(transcript, source, num_clips=7, min_sec=TARGET_MIN_SEC,
         max_sec=TARGET_MAX_SEC):
    """Return {'highlights': [...]} in the schema shortsmith.load_clips() reads."""
    segs = transcript.get("segments") or []
    if not segs:
        return {"highlights": [], "ranker": "heuristic"}

    units = sentence_units(segs)
    if len(units) < 2:
        return {"highlights": [], "ranker": "heuristic"}

    env, hop = rms_envelope(source)
    voiced = float(np.percentile(env, 60)) if env.size else 0.0

    def window_stats(a, b):
        i0, i1 = int(a / hop), max(int(b / hop), int(a / hop) + 1)
        i1 = min(i1, env.size)
        if i1 <= i0:
            return 0.0, 0.0
        seg = env[i0:i1]
        energy = float(seg.mean())
        silence = float((seg < voiced * 0.35).mean()) if voiced > 0 else 0.0
        return energy, silence

    cands = []
    for i in range(len(units)):
        for j in range(i, len(units)):
            a, b = units[i]["start"], units[j]["end"]
            dur = b - a
            if dur < min_sec:
                continue
            if dur > max_sec:
                break
            text = " ".join(u["text"] for u in units[i:j + 1])
            words = len(re.findall(r"[A-Za-z']+", text))
            if words < 25:                      # too little said to clip
                continue
            energy, silence = window_stats(a, b)
            hooks = sum(w * len(rx.findall(text)) for rx, w in _HOOK_RE)
            cands.append({
                "start": a, "end": b, "dur": dur, "text": text,
                "words": words, "energy": energy, "silence": silence,
                "hook_rate": hooks / max(dur, 1.0),
                "opener": units[i]["text"], "first_sentence": text,
            })
    if not cands:
        return {"highlights": [], "ranker": "heuristic"}

    density = _norm([c["words"] / max(c["dur"], 1.0) for c in cands])
    energy = _norm([c["energy"] for c in cands])
    hooks = _norm([c["hook_rate"] for c in cands])
    silence = _norm([c["silence"] for c in cands])
    for k, c in enumerate(cands):
        # a gentle pull toward a natural clip length, so two windows that score
        # the same break in favour of the one closer to TARGET_NICE_SEC
        length_fit = 1.0 - min(1.0, abs(c["dur"] - TARGET_NICE_SEC) / TARGET_NICE_SEC)
        c["score"] = (0.34 * float(density[k]) + 0.26 * float(energy[k])
                      + 0.24 * float(hooks[k]) + 0.10 * length_fit
                      - 0.16 * float(silence[k]))

    cands.sort(key=lambda c: -c["score"])

    # Greedy non-max suppression: at most 20% overlap with anything already
    # chosen, which is what stops five near-copies of the same moment.
    chosen = []
    for c in cands:
        if len(chosen) >= num_clips:
            break
        clash = False
        for d in chosen:
            ov = min(c["end"], d["end"]) - max(c["start"], d["start"])
            if ov > 0 and ov > 0.20 * min(c["dur"], d["dur"]):
                clash = True
                break
        if not clash:
            chosen.append(c)
    chosen.sort(key=lambda c: c["start"])

    hi = []
    for c in chosen:
        first = c["first_sentence"].strip()
        title = " ".join(first.split()[:9])
        if len(first.split()) > 9:
            title += "…"
        hi.append({
            "start_time": round(c["start"], 2),
            "end_time": round(c["end"], 2),
            "title": title,
            "score": int(round(max(0, min(100, 50 + 45 * c["score"])))),
            "hook_sentence": first[:220],
        })
    return {"highlights": hi, "ranker": "heuristic",
            "note": "scored offline from speech density, audio energy, hook "
                    "language and pause pattern — not an LLM's judgement"}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--clips", type=int, default=7)
    ap.add_argument("--min", type=float, default=TARGET_MIN_SEC)
    ap.add_argument("--max", type=float, default=TARGET_MAX_SEC)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    with open(a.transcript, encoding="utf-8") as f:
        tr = json.load(f)
    res = rank(tr, a.source, num_clips=a.clips, min_sec=a.min, max_sec=a.max)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    print(f"ranked {len(res['highlights'])} clip(s) -> {a.out}")
    for h in res["highlights"]:
        m, s = divmod(int(h["start_time"]), 60)
        print(f"  {m:02d}:{s:02d}  {h['end_time']-h['start_time']:5.1f}s  "
              f"score {h['score']:3d}  {h['title']}")


if __name__ == "__main__":
    main()
