"""Exact caption-clipping check for one clip.

Diffs the captionless stitch (kept via SHORTSMITH_KEEP=1) against the finished
render. The difference isolates the burned caption pixels precisely, so the
bounding box is real text and nothing else. Usage:
    python verify_cap_diff.py short_4
"""
import glob
import os
import subprocess
import sys

import numpy as np

name = sys.argv[1] if len(sys.argv) > 1 else "short_1"
W, H, FPS = 1080, 1920, 6

cands = sorted(glob.glob(f"shortsmith_work/{name}_*"))
if not cands:
    raise SystemExit(f"no kept workdir for {name} - rerun with SHORTSMITH_KEEP=1")
wd = cands[-1]
OUTDIR = os.environ.get("SHORTSMITH_OUT") or os.path.join(
    os.path.expanduser("~"), "Videos", "output")
A = os.path.join(wd, "stitched.mp4")            # captions NOT burned
B = os.path.join(OUTDIR, f"{name}.mp4")


def frames(p):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", p, "-vf", f"fps={FPS},scale={W}:{H}",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, timeout=900).stdout
    n = len(raw) // (W * H)
    return np.frombuffer(raw[: n * W * H], np.uint8).reshape(n, H, W)


a, b = frames(A), frames(B)
n = min(len(a), len(b))
skip = int(0.9 * FPS)
diff = np.abs(a[:n].astype(np.int16) - b[:n].astype(np.int16)) > 45

mins, maxs, tops, bots, seen = [], [], [], [], 0
for i in range(skip, n - skip):
    d = diff[i]
    if d.sum() < 300:
        continue
    seen += 1
    cols = np.where(d.any(axis=0))[0]
    rows = np.where(d.any(axis=1))[0]
    mins.append(int(cols.min()))
    maxs.append(int(cols.max()))
    tops.append(int(rows.min()))
    bots.append(int(rows.max()))

print(f"{name}: {seen} frames with caption text (of {n - 2 * skip} compared)")
if not seen:
    raise SystemExit("no caption frames - check the diff threshold")

heights = [bt - tp for tp, bt in zip(tops, bots)]
print(f"  bbox L{min(mins)} R{max(maxs)}   (margins 80 / 1000)")
print(f"  tops {min(tops)}  bottoms {max(bots)}   on a {H} frame")
print(f"  text height: min {min(heights)}  median {int(np.median(heights))}"
      f"  max {max(heights)}  (single 70px line ~80-95; >=150 = wrapped)")
l = sum(1 for x in mins if x < 78)
r = sum(1 for x in maxs if x > 1002)
print(f"  crossing LEFT margin: {l}   crossing RIGHT margin: {r}")
print(f"  {'PASS' if l == 0 and r == 0 else 'FAIL'}")
