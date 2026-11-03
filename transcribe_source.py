#!/usr/bin/env python3
"""Transcribe a whole source once and cache it to JSON.

This is the one-time cost that unlocks transcript-driven highlight detection
(the approach from Anil-matcha/AI-Youtube-Shorts-Generator): the transcript is
what lets highlights be chosen by WHAT IS SAID rather than only by audio level
and face size.

Output schema: {"duration": float, "model": str,
                "segments": [{"start": float, "end": float, "text": str}]}

Reusable API:
    from transcribe_source import transcribe
    transcribe(source, out_path, model="base", log=print)

CLI:
    python transcribe_source.py [--input VIDEO] [--out T.json] [--model base]
"""
import argparse
import json
import os
import subprocess
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
# Default for the standalone CLI only — make_shorts.py always passes a path
# explicitly. Deliberately empty so no personal path ships in the repository.
DEFAULT_SRC = os.environ.get("SHORTSMITH_SRC") or ""


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(r.stdout.strip())


def load_audio_16k(path):
    """Mono 16 kHz float32 via ffmpeg (skips faster-whisper's PyAV decoder)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, timeout=3600).stdout
    if not raw:
        raise RuntimeError("ffmpeg produced no audio")
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def pick_device(log=print):
    """The fastest transcription this machine can actually run.

    A GPU is never required, but when one is present Whisper runs several times
    faster in fp16 than int8 on the CPU. So detect rather than assume: this
    program is meant to run on other people's machines, and hardcoding the CPU
    would throw away most of the speed on any of them that has a graphics card.

    Override with SHORTSMITH_DEVICE=cpu|cuda and SHORTSMITH_COMPUTE=<type>.
    """
    want = (os.environ.get("SHORTSMITH_DEVICE") or "auto").lower()
    forced = os.environ.get("SHORTSMITH_COMPUTE")
    if want == "cpu":
        return "cpu", forced or "int8"

    # Ask what the GPU can actually RUN, not merely whether one exists.
    # get_cuda_device_count() answers the second question and is misleading: it
    # returns 1 on any machine with an NVIDIA driver, including one with no
    # cuDNN. get_supported_compute_types() answers the first.
    try:
        import ctranslate2                       # ships with faster-whisper
        types = set(ctranslate2.get_supported_compute_types("cuda"))
        # Only the reduced-precision types are worth moving for. float32 alone
        # means an old card: it would run, but slower than int8 on the CPU.
        for t in ("float16", "int8_float16", "bfloat16"):
            if t in types:
                return "cuda", forced or t
        if want == "cuda":
            log(f"  CUDA offers only {sorted(types)} — the CPU is faster here")
    except Exception:                            # noqa: BLE001
        if want == "cuda":
            log("  asked for CUDA but it is not usable here — using the CPU")
    return "cpu", forced or "int8"


def transcribe(source, out_path, model="base", language="en", log=print,
               reuse=True):
    """Transcribe `source` into `out_path`. Returns True if work was done.

    Cached, but keyed on the model name: an existing transcript is reused only
    when the same model produced it, so switching to a bigger model does not
    silently keep the older, less accurate text.
    """
    if reuse and os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("segments") and old.get("model") == model:
                log(f"  transcript cached: {len(old['segments'])} segments, "
                    f"model '{old.get('model')}'")
                return False
            log(f"  cached transcript used model '{old.get('model')}', "
                f"want '{model}' — re-transcribing")
        except (ValueError, OSError):
            log("  cached transcript unreadable — re-transcribing")

    dur = probe_duration(source)
    log(f"  source duration {dur:.1f}s ({dur/60:.1f} min)")
    log("  decoding audio -> 16 kHz mono ...")
    audio = load_audio_16k(source)
    log(f"    {len(audio)/16000:.1f}s of audio")

    from faster_whisper import WhisperModel
    device, compute = pick_device(log)
    log(f"  loading whisper '{model}' ({compute}, {device}) ...")
    t0 = time.time()
    try:
        m = WhisperModel(model, device=device, compute_type=compute)
    except Exception as e:                       # noqa: BLE001
        if device == "cpu":
            raise
        # A graphics card that is present but not usable -- an older driver, a
        # missing cuDNN -- must not end the run. This has to work on machines
        # nobody here can see, so fall back rather than fail.
        log(f"  {device} is not usable here ({type(e).__name__}) — using the CPU")
        device, compute = "cpu", "int8"
        m = WhisperModel(model, device=device, compute_type=compute)
    segs, _ = m.transcribe(audio, language=language, word_timestamps=False,
                           vad_filter=True, beam_size=1)
    out_segments = []
    for s in segs:
        txt = (s.text or "").strip()
        if txt:
            out_segments.append({"start": round(float(s.start), 3),
                                 "end": round(float(s.end), 3),
                                 "text": txt})
        if out_segments and len(out_segments) % 200 == 0:
            el = max(time.time() - t0, 1e-6)
            log(f"    {len(out_segments)} segments, {s.end:.0f}s/{dur:.0f}s "
                f"({s.end/el:.1f}x realtime)")

    data = {"duration": dur, "model": model, "segments": out_segments}
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    log(f"  transcribed {len(out_segments)} segments in {time.time()-t0:.0f}s")
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=DEFAULT_SRC)
    ap.add_argument("--out", default=os.path.join(_HERE, "transcript_work",
                                                  "transcript.json"))
    ap.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "base"))
    ap.add_argument("--force", action="store_true", help="ignore the cache")
    a = ap.parse_args()
    transcribe(a.input, a.out, model=a.model, reuse=not a.force)
    print(f"-> {a.out}")


if __name__ == "__main__":
    main()
