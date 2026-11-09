#!/usr/bin/env python3
"""Any video in -> ranked vertical shorts out. One command, unattended.

    python make_shorts.py "D:/videos/podcast.mp4"

Steps, each cached so a re-run is cheap:
    1. probe the source
    2. transcribe it once          <work>/<slug>/transcript.json
    3. rank the highlight moments  <work>/<slug>/highlights.json
    4. render each moment to 1080x1920 — face-aware reframing, eased push plus
       stress punch-ins, burned word-timed captions — via shortsmith.py

Ranking is OFFLINE by default (highlights_heuristic.py): no API key, no network,
so it runs unattended. Pass --llm to try the transcript ranker in
highlights_llm.py first, which needs a reachable OpenAI-compatible endpoint; if
that fails it falls back offline rather than stopping.

Everything is namespaced by the source filename, so two different videos never
overwrite each other's transcript, highlights or output.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from transcribe_source import probe_duration, transcribe    # noqa: E402


def detect_silences(source, noise="-35dB", minimum=0.20):
    """Silent spans in the source, as (start, end) pairs.

    The waveform is the authority on where a cut is safe. Transcript
    timestamps drift, and -- more importantly -- an ASR word onset lands on the
    consonant, so a cut placed on one chops the attack off the first word and
    the edit sounds clipped even though every word is present.
    """
    r = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", source, "-af",
         f"silencedetect=noise={noise}:d={minimum}", "-f", "null", "-"],
        capture_output=True, text=True, timeout=3600)
    text = (r.stderr or "")
    spans, open_at = [], None
    for line in text.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            open_at = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and open_at is not None:
            end = float(m.group(1))
            if end > open_at:
                spans.append((open_at, end))
            open_at = None
    return spans


def snap_edge(t, spans, pad=0.06, reach=2.5):
    """Move a cut edge into the nearest real silence.

    `t` is where the transcript says a sentence ends. If that already falls
    inside silence, it is safe and stays -- only the padding is applied. If it
    does not, the edge moves to the closest boundary of the nearest silent span
    and then `pad` further in, so the cut lands in silence rather than on the
    consonant of the next word.

    Leaving `reach` seconds finds nothing and the time is returned untouched:
    a clip is better slightly clipped than silently moved somewhere unrelated.
    """
    if not spans:
        return t
    best, best_gap = None, None
    for a, b in spans:
        if a <= t <= b:                     # already inside silence
            best, best_gap = (a, b), 0.0
            break
        gap = a - t if t < a else t - b
        if gap < reach and (best_gap is None or gap < best_gap):
            best, best_gap = (a, b), gap
    if best is None:
        return t
    a, b = best
    if a <= t <= b:
        # Inside, but hug the nearer side so we do not lose speech on a long
        # pause: keep the time, it is already somewhere safely quiet.
        return t
    return min(b, max(a, a + pad)) if t < a else max(a, min(b, b - pad))


# How far either side of a proposed edge we will look for a sentence boundary.
# A cut is better left where the ranker put it than dragged somewhere unrelated.
SENTENCE_REACH_S = 6.0


def interpolate_words(segs):
    """(token, start, end) for every word, from segment-level timings.

    The source transcript carries no per-word times, and using the SEGMENT
    boundaries as sentence boundaries does not work: measured on the real
    transcript only 45% of segments end with punctuation, and a sentence
    routinely ends in the middle of one. Two clips in seven opened on a line
    like "in the American Time Use Survey", which is plainly mid-thought.

    So distribute each segment's duration across its own words in proportion to
    character count. That assumes a roughly steady rate inside a segment, which
    puts a boundary within a few tenths of a second -- close enough, because the
    silence pass afterwards snaps to the exact sample.
    """
    out = []
    for s in segs:
        toks = (s.get("text") or "").split()
        if not toks:
            continue
        weights = [len(t) + 1 for t in toks]
        total = float(sum(weights))
        t0, t1 = float(s["start"]), float(s["end"])
        span = max(t1 - t0, 0.0)
        acc = 0.0
        for tok, weight in zip(toks, weights):
            a = t0 + span * acc / total
            acc += weight
            out.append((tok, a, t0 + span * acc / total))
    return out


def sentence_edges(t_path):
    """Times where a sentence begins and where one ends, to the nearest word.

    A sentence ends at the last word carrying . ? !; the sentence after it
    starts on the next word. Working word by word is what catches the boundaries
    that fall inside a segment.
    """
    with open(t_path, encoding="utf-8") as f:
        segs = (json.load(f).get("segments") or [])
    words = interpolate_words(segs)
    starts, ends = [], []
    if words:
        starts.append(words[0][1])
    for i, (tok, _a, b) in enumerate(words):
        if re.search(r"[.?!][\"')\]]*$", tok):
            ends.append(b)
            if i + 1 < len(words):
                starts.append(words[i + 1][1])
    return sorted(starts), sorted(ends)


def snap_to_sentence(t, edges, prefer_earlier=True, reach=SENTENCE_REACH_S):
    """Move `t` to the nearest sentence edge within `reach`, else leave it.

    Ties go earlier for a clip's start (opening slightly wide is recoverable,
    clipping the hook is not) and later for its end (finish the thought). This
    is the same asymmetry a human editor applies without thinking about it.
    """
    best, best_gap = None, None
    for e in edges:
        gap = abs(e - t)
        if gap > reach:
            continue
        if best is None or gap < best_gap:
            best, best_gap = e, gap
        elif gap == best_gap and ((prefer_earlier and e < best)
                                  or (not prefer_earlier and e > best)):
            best = e
    return t if best is None else best


def slugify(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    s = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").lower()
    return (s or "video")[:60]


def human(sec):
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def probe_video(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    parts = (r.stdout or "").strip().split(",")
    if len(parts) < 3:
        raise SystemExit(f"could not find a video stream in {path}")
    w, h = int(parts[0]), int(parts[1])
    try:
        n, d = parts[2].split("/")
        fps = float(n) / float(d) if float(d) else 25.0
    except (ValueError, ZeroDivisionError):
        fps = 25.0
    return w, h, fps


def rank_highlights(transcript, source, num_clips, min_sec, max_sec, use_llm, log):
    """LLM ranker when asked and reachable, else offline. Never raises."""
    if use_llm:
        try:
            log("  trying the transcript LLM ranker ...")
            from highlights_llm import default_llm_fn, get_highlights
            res = get_highlights(transcript, num_clips=num_clips,
                                 llm_fn=default_llm_fn())
            hi = res.get("highlights") if isinstance(res, dict) else None
            if hi:
                log(f"  LLM ranked {len(hi)} moment(s)")
                res["ranker"] = "llm"
                return res
            log("  LLM returned nothing usable — falling back offline")
        except Exception as e:                       # noqa: BLE001
            log(f"  LLM ranker unavailable ({type(e).__name__}: {e})")
            log("  falling back to the offline ranker")

    from highlights_heuristic import rank
    log("  ranking offline: speech density, audio energy, hook language, pauses")
    return rank(transcript, source, num_clips=num_clips,
                min_sec=min_sec, max_sec=max_sec)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="source video (mp4 / mkv / mov / ...)")
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"),
                                                  "Videos", "shorts"),
                    help="where to put the shorts (default ~/Videos/shorts)")
    ap.add_argument("--work", default=os.path.join(_HERE, "work"))
    ap.add_argument("--clips", type=int, default=7, help="how many shorts (7)")
    ap.add_argument("--min", type=float, default=20.0, dest="min_sec")
    ap.add_argument("--max", type=float, default=45.0, dest="max_sec")
    ap.add_argument("--whisper", default=os.environ.get("WHISPER_MODEL", "base"),
                    help="base | small | medium (small is slower, mishears less)")
    ap.add_argument("--llm", action="store_true",
                    help="try the LLM ranker first (needs endpoint + key)")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse an existing highlights.json instead of re-ranking")
    ap.add_argument("--keep", action="store_true", help="keep work dirs")
    ap.add_argument("--open", action="store_true",
                    help="open the output folder when finished")
    a = ap.parse_args()

    src = os.path.abspath(a.input)
    if not os.path.exists(src):
        raise SystemExit(f"input not found: {src}")

    slug = slugify(src)
    work = os.path.join(a.work, slug)
    out = os.path.join(a.out, slug)
    os.makedirs(work, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    t_path = os.path.join(work, "transcript.json")
    h_path = os.path.join(work, "highlights.json")

    def log(m):
        print(m, flush=True)

    log("=" * 70)
    log("  make_shorts — any video in, vertical shorts out")
    log("=" * 70)
    w, h, fps = probe_video(src)
    dur = probe_duration(src)
    log(f"  input : {os.path.basename(src)}")
    log(f"          {w}x{h} @ {fps:.2f}fps, {human(dur)}")
    log(f"  output: {out}")
    # Measured on this box: transcription ~8x realtime with 'base' on a long
    # file (worse on short ones, where the model load dominates), and rendering
    # ~3 min per clip — each clip costs a Whisper load on top of face tracking,
    # the segment renders, the concat and the caption burn. Stated up front so a
    # long wait reads as expected rather than hung.
    log(f"  rough time: ~{human(dur / 8.0 + a.clips * 180)} "
        f"(transcribe + {a.clips} clips)\n")

    t0 = time.time()
    log("[1/3] transcription")
    transcribe(src, t_path, model=a.whisper, log=log)

    log("\n[2/3] highlight selection")
    if a.reuse and os.path.exists(h_path):
        log(f"  reusing {os.path.relpath(h_path, _HERE)}")
        with open(h_path, encoding="utf-8") as f:
            res = json.load(f)
    else:
        with open(t_path, encoding="utf-8") as f:
            tr = json.load(f)
        res = rank_highlights(tr, src, a.clips, a.min_sec, a.max_sec, a.llm, log)
        with open(h_path, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)

    hi = res.get("highlights") or []
    if not hi:
        raise SystemExit("no highlight moments found — is there speech in this file?")
    log(f"  {(res.get('ranker') or 'unknown').upper()} ranker chose {len(hi)} moment(s):")
    for k, x in enumerate(hi, 1):
        s, e = float(x["start_time"]), float(x["end_time"])
        m, sec = divmod(int(s), 60)
        log(f"    {k}. {m:02d}:{sec:02d}  {e - s:5.1f}s  "
            f"score {x.get('score', 0):>3}  {x.get('title', '')}")

    # ---- anchor every edge on real silence
    #
    # The transcript decides WHICH moments are worth cutting; the waveform
    # decides exactly WHERE. ASR word onsets land on the consonant, so an edge
    # placed on one chops the attack off the first word -- the edit sounds
    # clipped even though the transcript says every word is present. Padding
    # does not rescue it because the error is systematic toward the consonant,
    # not random drift. So find the silence either side and cut in that.
    # Three passes, in this order, because each one is a coarser question than
    # the next: WHERE in the argument is the clip (sentence), how LONG may it be
    # (duration), then exactly WHICH sample to cut on (silence).
    log("\n  anchoring cut edges")
    sent_starts, sent_ends = sentence_edges(t_path)
    log(f"    {len(sent_starts)} sentence start(s), {len(sent_ends)} sentence end(s)")
    spans = detect_silences(src)
    log(f"    {len(spans)} silent gap(s) in the source")

    moved, snapped = 0, 0
    for x in hi:
        s0, e0 = float(x["start_time"]), float(x["end_time"])
        # 1. Sentence snap -- a clip must not open or close mid-thought.
        ss = snap_to_sentence(s0, sent_starts, prefer_earlier=True)
        ee = snap_to_sentence(e0, sent_ends, prefer_earlier=False)
        if abs(ss - s0) > 0.01 or abs(ee - e0) > 0.01:
            snapped += 1
        # 2. Silence align -- ASR onsets sit on the consonant, so an edge placed
        #    on one chops the attack off the first word. Padding cannot rescue
        #    that, because the error is systematic toward the consonant rather
        #    than random drift. Cut inside the quiet instead.
        s1, e1 = snap_edge(ss, spans), snap_edge(ee, spans)
        # 3. Duration guard -- neither pass may leave a clip the wrong length.
        #    Widest-to-narrowest, so the most refinement that still fits wins.
        for cs, ce in ((s1, e1), (s1, ee), (ss, e1), (ss, ee), (s0, e0)):
            if a.min_sec <= (ce - cs) <= a.max_sec:
                break
        else:
            cs, ce = s0, e0
        if abs(cs - s0) > 0.01 or abs(ce - e0) > 0.01:
            moved += 1
            log(f"    {s0:7.2f}s -> {cs:7.2f}s    {e0:7.2f}s -> {ce:7.2f}s")
        x["start_time"], x["end_time"] = round(cs, 3), round(ce, 3)
    log(f"    {snapped} of {len(hi)} snapped to a sentence edge")
    log(f"    {moved} of {len(hi)} moved off a word onset onto silence")
    # Written back so the renderer sees the adjusted times, and so a later
    # --reuse is stable: an edge already inside silence snaps to itself.
    with open(h_path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)

    log(f"\n[3/3] rendering {len(hi)} clip(s) — this is the slow part")
    env = dict(os.environ)
    render_work = os.path.join(work, "render")
    env.update({"SHORTSMITH_SRC": src, "SHORTSMITH_OUT": out,
                "SHORTSMITH_WORK": render_work, "HIGHLIGHTS_JSON": h_path})
    if a.keep:
        env["SHORTSMITH_KEEP"] = "1"
    os.makedirs(render_work, exist_ok=True)
    rc = subprocess.run([sys.executable, os.path.join(_HERE, "shortsmith.py")],
                        cwd=_HERE, env=env).returncode
    if rc != 0:
        raise SystemExit(f"renderer exited with code {rc}")

    made = sorted(f for f in os.listdir(out)
                  if f.startswith("short_") and f.endswith(".mp4"))
    log("\n" + "=" * 70)
    log(f"  DONE — {len(made)} clip(s) in {human(time.time() - t0)}")
    for f in made:
        fp = os.path.join(out, f)
        log(f"    {fp}  ({os.path.getsize(fp):,} bytes)")
    if a.open:
        try:
            os.startfile(out)                        # Windows
        except AttributeError:
            subprocess.run(["xdg-open", out], check=False)


if __name__ == "__main__":
    main()
