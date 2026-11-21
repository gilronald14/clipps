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
    from workspace import identity, atomic
    source_identity = identity(source)
    if reuse and os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                old = json.load(f)
            if (old.get("segments") and old.get("model") == model
                    and old.get("source_identity") == source_identity
                    and old.get("timing") == "word" and old.get("requested_language") == language):
                log(f"  transcript cached: {len(old['segments'])} segments, "
                    f"model '{old.get('model')}'")
                return False
            log(f"  cached transcript used model '{old.get('model')}', "
                f"want '{model}' — re-transcribing")
        except (ValueError, OSError):
            log("  cached transcript unreadable — re-transcribing")

    dur = probe_duration(source)
    log(f"  source duration {dur:.1f}s ({dur/60:.1f} min)")
    from offline_scan import blocks
    os.environ['HF_HUB_OFFLINE']='1'
    from faster_whisper import WhisperModel
    device, compute = pick_device(log)
    t0 = time.time()

    def decode(device, compute):
        try:
            model_instance = WhisperModel(model, device=device, compute_type=compute,
                                          local_files_only=True)
        except Exception as e:                        # noqa: BLE001
            # The app deliberately never downloads models at runtime (offline
            # by default). Explain that instead of surfacing a huggingface
            # traceback when the requested size was never fetched.
            if type(e).__name__ in ('LocalEntryNotFoundError', 'OfflineModeIsEnabled'):
                raise SystemExit(
                    f"The '{model}' Whisper model is not on this disk yet, and Clipps "
                    "never downloads models silently. Fetch it once — internet is "
                    "needed only for this step, after that everything runs offline:\n"
                    f"    python download_models.py --whisper {model}\n"
                    "Or pick a size that is already downloaded (tiny/base/small).")
            raise
        out_segments = []
        sample_offset = 0
        detected = language
        from speech_chunks import windows, owned_segment
        pcm = blocks(source, ['-vn', '-ac', '1', '-ar', '16000', '-f', 's16le'], 16000*2*120)
        for raw, offset, left, right in windows(pcm):
            audio = np.frombuffer(raw, '<i2').astype(np.float32) / 32768
            sample_offset = round(right * 16000)
            segs, info = model_instance.transcribe(audio, language=language, word_timestamps=True,
                                                  vad_filter=True, beam_size=1)
            for seg in segs:
                cue = owned_segment(seg,offset,left,right,dur)
                if cue and cue['end']>cue['start']:
                    detected = detected or info.language
                    out_segments.append(cue)
            log(f"    decoded {right:.0f}s / {dur:.0f}s")
        if not sample_offset:
            raise ValueError("No decodable audio")
        return out_segments, detected

    try:
        out_segments, detected = decode(device, compute)
    except RuntimeError:
        if device == "cpu":
            raise
        log("  GPU inference failed. Retrying on CPU.")
        out_segments, detected = decode("cpu","int8")

    data = {"duration": dur, "model": model, "segments": out_segments,
            "source_identity": source_identity, "requested_language": language,
            "language": detected, "provider": "faster-whisper", "timing": "word"}
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    atomic(out_path, data)
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
