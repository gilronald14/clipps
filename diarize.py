#!/usr/bin/env python3
"""Who is talking, and when — CPU speaker diarization via sherpa-onnx.

This is the piece that tells a SPEAKER from a LISTENER. Without it the framing
logic can only ask "who is on screen?", so a silent listener gets tracked and
stacked alongside the person actually talking.

Why diarization rather than watching mouths: it works on the audio, so it is
completely unaffected by how small the faces are. That matters here — measured
on this source, faces in a two-shot are only ~61 px wide (a mouth region of
~31 px), which makes any visual lip-reading signal marginal exactly where it is
needed. Diarization has no such limit and needs no GPU.

Model choice and the clustering/threshold defaults come from WanGP
(github.com/deepbeepmeep/WanGP), which uses this same sherpa-onnx + pyannote
stack for its two-speaker input. Its tuning transfers; its model choice does not
— it ships a Chinese embedder, and this material is English, so we use
wespeaker_en_voxceleb_CAM++ instead.

API:
    from diarize import speaker_turns, turns_at
    turns = speaker_turns("video.mp4", start=2405.0, duration=11.0)
    # -> [{"start": 0.0, "end": 3.4, "speaker": 0}, ...]  times relative to the window

CLI:
    python diarize.py VIDEO [--start S] [--duration D] [--speakers 2]
"""
import argparse
import os
import subprocess

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(_HERE, "models", "diarization")
SEG_MODEL = os.path.join(MODEL_DIR, "seg_pyannote3.onnx")
EMB_MODEL = os.path.join(MODEL_DIR, "embed_en_campplus.onnx")

SAMPLE_RATE = 16000

_DIARIZER = None


def models_available():
    return os.path.exists(SEG_MODEL) and os.path.exists(EMB_MODEL)


def load_wave(path, start=None, duration=None, sr=SAMPLE_RATE):
    """Mono float32 in [-1, 1] via ffmpeg.

    ffmpeg rather than PyAV/soundfile: this build of PyAV calls an av.open()
    argument faster-whisper's stack no longer passes, and going through one
    decoder for the whole pipeline keeps the audio identical to what Whisper saw.
    """
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{float(start):.4f}"]
    if duration is not None:
        cmd += ["-t", f"{float(duration):.4f}"]
    cmd += ["-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=1800).stdout
    if not raw:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def _load_diarizer(num_speakers=2, threshold=0.5, min_on=0.3, min_off=0.5):
    """Build the diarizer once and reuse it (model load is the slow part)."""
    global _DIARIZER
    if _DIARIZER is not None:
        return _DIARIZER

    import sherpa_onnx
    if not models_available():
        raise FileNotFoundError(
            f"diarization models missing in {MODEL_DIR} — expected "
            f"{os.path.basename(SEG_MODEL)} and {os.path.basename(EMB_MODEL)}")

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=SEG_MODEL),
            num_threads=max(1, (os.cpu_count() or 2) // 2),
            provider="cpu",
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=EMB_MODEL,
            num_threads=max(1, (os.cpu_count() or 2) // 2),
            provider="cpu",
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers, threshold=threshold),
        min_duration_on=min_on,
        min_duration_off=min_off,
    )
    _DIARIZER = sherpa_onnx.OfflineSpeakerDiarization(config)
    return _DIARIZER


def speaker_turns(path, start=None, duration=None, num_speakers=2,
                  threshold=0.5, min_on=0.3, min_off=0.5):
    """Speaker turns for a window of `path`, times relative to the window start.

    Returns [] when the models are missing or anything fails — callers treat an
    empty result as "no information" and fall back to on-screen framing, so a
    diarization problem can never break a render.
    """
    if not models_available():
        return []
    try:
        audio = load_wave(path, start, duration)
        if audio.size < SAMPLE_RATE // 2:          # under half a second: nothing to say
            return []
        sd = _load_diarizer(num_speakers=num_speakers, threshold=threshold,
                            min_on=min_on, min_off=min_off)
        result = sd.process(audio)
        turns = []
        for seg in result.sort_by_start_time():
            turns.append({"start": float(seg.start), "end": float(seg.end),
                          "speaker": int(seg.speaker)})
        return turns
    except Exception as e:                                   # noqa: BLE001
        print(f"    ! diarization unavailable ({type(e).__name__}: {e})")
        return []


def turns_at(turns, t, pad=0.0):
    """Which speakers are talking at time `t` (window-relative seconds)."""
    return sorted({x["speaker"] for x in turns
                   if x["start"] - pad <= t < x["end"] + pad})


def speaking_time(turns):
    """Total seconds per speaker — tells the two apart from the intro/outro one."""
    out = {}
    for x in turns:
        out[x["speaker"]] = out.get(x["speaker"], 0.0) + (x["end"] - x["start"])
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--start", type=float, default=None)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--speakers", type=int, default=2)
    a = ap.parse_args()
    if not models_available():
        raise SystemExit(f"models missing in {MODEL_DIR}")
    turns = speaker_turns(a.input, a.start, a.duration, num_speakers=a.speakers)
    if not turns:
        raise SystemExit("no turns returned")
    print(f"{len(turns)} turn(s):")
    for x in turns:
        print(f"  {x['start']:7.2f} -> {x['end']:7.2f}  ({x['end']-x['start']:5.2f}s)  "
              f"speaker {x['speaker']}")
    print("\ntalking time:", {k: round(v, 2) for k, v in speaking_time(turns).items()})


if __name__ == "__main__":
    main()
