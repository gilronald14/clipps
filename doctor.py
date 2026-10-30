#!/usr/bin/env python3
"""Check whether this machine can run the pipeline, and say exactly what to fix.

Run this first on a new PC, and again if anything behaves oddly:

    python doctor.py

Exit code 0 = ready to render. 1 = something required is missing.
"""
import importlib.util
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(_HERE, "models")

# module name -> what it is for, and the pip name if it differs
REQUIRED = [
    ("numpy", "numbers", "numpy"),
    ("cv2", "face detection / video frames", "opencv-python"),
    ("mediapipe", "the BlazeFace face detector", "mediapipe"),
    ("PIL", "measuring caption text width", "pillow"),
    ("faster_whisper", "transcription for captions", "faster-whisper"),
]
CHECKS = []


def ok(msg):
    CHECKS.append((True, msg))
    print(f"  [ok]   {msg}")


def bad(msg, fix=None):
    CHECKS.append((False, msg + (f"  ->  {fix}" if fix else "")))
    print(f"  [FAIL] {msg}" + (f"\n         fix: {fix}" if fix else ""))


def warn(msg):
    print(f"  [note] {msg}")


print("=" * 70)
print("  Shortsmith health check")
print("=" * 70)

# ---- python
print("\npython")
v = sys.version_info
print(f"  version: {v.major}.{v.minor}.{v.micro}")
print(f"  exe    : {sys.executable}")
if v < (3, 9):
    bad("Python 3.9 or newer is required", "install a current Python 3")
else:
    ok(f"Python {v.major}.{v.minor} is new enough")

# ---- libraries
print("\nlibraries")
missing_pkgs = []
for mod, why, pkg in REQUIRED:
    if importlib.util.find_spec(mod) is None:
        missing_pkgs.append(pkg)
        bad(f"{mod} missing ({why})", f"python -m pip install {pkg}")
    else:
        ok(f"{mod}  ({why})")
if importlib.util.find_spec("sherpa_onnx") is None:
    warn("sherpa_onnx missing — speaker diarization will be skipped; "
         "the rest still works")
    warn("  add it with:  python -m pip install sherpa-onnx")
else:
    ok("sherpa_onnx  (speaker diarization)")

# ---- ffmpeg
print("\nffmpeg")
for exe, why in (("ffmpeg", "all rendering"), ("ffprobe", "reading durations")):
    p = shutil.which(exe)
    if p:
        ok(f"{exe} found ({why})")
    else:
        bad(f"{exe} not on PATH ({why})",
            "install ffmpeg and add its bin folder to PATH")
if shutil.which("ffmpeg"):
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=60).stdout
        need = ["scale", "crop", "vstack", "subtitles", "sendcmd", "loudnorm",
                "unsharp", "fade"]
        have = [f for f in need if f" {f} " in out]
        miss = [f for f in need if f not in have]
        # libass is what actually draws captions
        if "subtitles" in miss:
            bad("ffmpeg is missing the 'subtitles' filter",
                "install a build with libass (the 'full' builds have it)")
        else:
            ok(f"ffmpeg filters present: {', '.join(need)}")
        if miss and "subtitles" not in miss:
            warn(f"some optional filters missing: {', '.join(miss)}")
    except Exception as e:                            # noqa: BLE001
        warn(f"could not list ffmpeg filters ({e})")

# ---- models
print("\nmodels")
need_models = [
    ("blaze_face_full_range.tflite", "face detection", True),
    ("yunet.onnx", "face detection fallback", False),
    ("diarization/seg_pyannote3.onnx", "speaker diarization", False),
    ("diarization/embed_en_campplus.onnx", "speaker diarization", False),
]
for rel, why, required in need_models:
    p = os.path.join(MODELS, rel)
    if os.path.exists(p):
        ok(f"{rel}  ({os.path.getsize(p)/1048576:.1f} MB, {why})")
    elif required:
        bad(f"{rel} missing ({why})", "python download_models.py")
    else:
        warn(f"{rel} missing ({why}) — that feature will be skipped")
        warn("  add it with:  python download_models.py")

# ---- writable output
print("\npaths")
outprobe = os.path.join(_HERE, ".write_test")
try:
    os.makedirs(outprobe, exist_ok=True)
    open(os.path.join(outprobe, "x"), "w").close()
    os.remove(os.path.join(outprobe, "x"))
    os.rmdir(outprobe)
    ok(f"pipeline folder is writable: {_HERE}")
except OSError as e:
    bad(f"cannot write in {_HERE} ({e})",
        "move the folder somewhere you own, e.g. under your user profile")

# ---- optional hardware note
print("\nhardware (informational — no GPU is required)")
try:
    r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                        "--format=csv,noheader"], capture_output=True,
                       text=True, timeout=30)
    warn(f"GPU: {r.stdout.strip()}" if r.stdout.strip() else "no NVIDIA GPU seen")
    warn("  rendering is CPU-based; a GPU only helps if you add the optional "
         "upscaling tools")
except Exception:                                     # noqa: BLE001
    warn("no NVIDIA GPU seen — fine, everything here runs on the CPU")

# ---- record the interpreter that passed, for the launchers
#
# The .bat files start the window and drag-and-drop jobs. Rather than making
# them re-search (and risk picking a different Python, or the Windows Store
# stub), they read this one line. Only written when everything passed, so a
# stale or broken path is never recorded.
PYNOTE = os.path.join(_HERE, "python_path.txt")
failed = [m for good, m in CHECKS if not good]
if not failed:
    try:
        with open(PYNOTE, "w", encoding="utf-8") as f:
            f.write(sys.executable + "\n")
    except OSError as e:
        warn(f"could not write {os.path.basename(PYNOTE)} ({e})")

print("\n" + "=" * 70)
if failed:
    print(f"  NOT READY — {len(failed)} problem(s):")
    for m in failed:
        print(f"    - {m}")
    print("\n  Fix the [FAIL] lines above, then run: python doctor.py")
else:
    print("  READY. Render something with:")
    print('    python make_shorts.py "C:/path/to/video.mp4" --open')
    print("  or double-click Shortsmith_App.bat")
print("=" * 70)
sys.exit(1 if failed else 0)
