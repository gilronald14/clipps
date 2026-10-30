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

import settings                                              # noqa: E402
from transcribe_source import probe_duration, transcribe    # noqa: E402


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
    # Default to wherever shorts were last written, so a folder chosen once in
    # the window (or here) is remembered -- including for drag-and-drop, which
    # has no way to ask. settings.default_out() falls back to ~/Videos/shorts.
    ap.add_argument("--out", default=settings.default_out(),
                    help="where to put the shorts (remembered between runs)")
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
    # Remember both choices so the next run -- and the window, and a file
    # dropped on the launcher, none of which can ask -- starts where this one
    # left off. A failure here is ignored; it is only a convenience.
    settings.save(out=os.path.abspath(a.out), source=src)
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
