#!/usr/bin/env python3
"""Fetch the model files the pipeline needs. Safe to re-run.

Everything lands under models/ next to this script, so the whole folder can be
copied to another PC as-is. Downloads are verified by size, and a file that is
already the right size is skipped — so re-running after an interruption only
fetches what is missing.

    python download_models.py            # all of them
    python download_models.py --check    # report what is present, download nothing
"""
import argparse
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(_HERE, "models")

MP = "https://storage.googleapis.com/mediapipe-models/face_detector"

# (dest path relative to models/, url, expected size in bytes)
FILES = [
    ("blaze_face_full_range.tflite",
     f"{MP}/blaze_face_full_range/float16/latest/blaze_face_full_range.tflite",
     None),
    ("yunet.onnx",
     "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
     "face_detection_yunet_2023mar.onnx",
     None),
    ("diarization/seg_pyannote3.onnx",
     "https://huggingface.co/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/"
     "resolve/main/model.onnx",
     5992913),
    ("diarization/embed_en_campplus.onnx",
     "https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/main/"
     "wespeaker_en_voxceleb_CAM++.onnx",
     29292684),
]

# A download smaller than this is certainly an error page, not a model.
MIN_PLAUSIBLE = 100 * 1024


def human(n):
    return f"{n/1048576:.1f} MB" if n >= 1048576 else f"{n/1024:.0f} KB"


def fetch(dest, url, expect, quiet=False):
    if os.path.exists(dest):
        have = os.path.getsize(dest)
        if expect is None or have == expect:
            print(f"  ok      {os.path.relpath(dest, _HERE)}  ({human(have)})")
            return True, have
        print(f"  re-fetch {os.path.relpath(dest, _HERE)}  "
              f"(have {human(have)}, want {human(expect)})")
    else:
        print(f"  get     {os.path.relpath(dest, _HERE)}")

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if not quiet and total:
                    pct = 100 * got / total
                    sys.stdout.write(f"\r          {pct:5.1f}%  {human(got)}")
                    sys.stdout.flush()
        if not quiet:
            sys.stdout.write("\r" + " " * 40 + "\r")
    except Exception as e:                            # noqa: BLE001
        print(f"  FAILED  {type(e).__name__}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False, 0

    size = os.path.getsize(tmp)
    if size < MIN_PLAUSIBLE:
        print(f"  FAILED  {human(size)} is too small to be a model — "
              f"wrong URL or an error page")
        os.remove(tmp)
        return False, 0
    if expect is not None and size != expect:
        print(f"  WARNING size {human(size)} != expected {human(expect)}; keeping it")
    os.replace(tmp, dest)
    print(f"  done    {os.path.relpath(dest, _HERE)}  ({human(size)})")
    return True, size


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="report only, download nothing")
    a = ap.parse_args()

    print(f"models -> {MODELS}\n")
    missing = []
    for rel, url, expect in FILES:
        dest = os.path.join(MODELS, rel)
        if a.check:
            have = os.path.exists(dest)
            size = os.path.getsize(dest) if have else 0
            good = have and (expect is None or size == expect)
            print(f"  {'ok    ' if good else 'MISSING'} {rel}"
                  + (f"  ({human(size)})" if have else ""))
            if not good:
                missing.append(rel)
            continue
        ok, _ = fetch(dest, url, expect)
        if not ok:
            missing.append(rel)

    print()
    if a.check:
        print("everything present" if not missing
              else f"missing {len(missing)}: run  python download_models.py")
    else:
        print("all models ready" if not missing
              else f"{len(missing)} file(s) failed — re-run to retry")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
