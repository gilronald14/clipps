"""How often does diarization actually change the camera, per clip?

The switch can only act when TWO faces are on screen at a turn boundary. On a
close-up there is one face and nothing to disambiguate, so if the source is
mostly close-ups the honest answer is "rarely" — this measures that instead of
assuming the feature helped.
"""
import importlib.util
import json
import os
import sys

spec = importlib.util.spec_from_file_location("ss", "shortsmith.py")
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)

ss._load_detector()
SRC = ss.SRC
w, h, fps = ss.probe(SRC)

hl = json.load(open("transcript_work/highlights.json"))["highlights"]
only = sys.argv[1:] or None

print(f"{'clip':8} {'len':>6} {'turns':>6} {'chg':>4} {'2-face%':>8} "
      f"{'chg@2face':>10} {'moved%':>7} {'maxpx':>6}")
tot_moved = tot_frames = tot_changes = tot_at2 = 0
for i, x in enumerate(hl, 1):
    name = f"short_{i}"
    if only and name not in only:
        continue
    start = float(x["start_time"])
    dur = float(x["end_time"]) - start
    turns = ss.diarize_speaker_turns(SRC, start, dur)
    changes = ss.speaker_change_frames(turns, fps, int(dur * fps))

    xs_off, cam, _ = ss.analyze_camera(SRC, start, dur, fps, w, h, turns=None)
    xs_on, _, _ = ss.analyze_camera(SRC, start, dur, fps, w, h, turns=turns)
    n = min(len(xs_off), len(xs_on))
    moved = sum(1 for k in range(n) if abs(xs_off[k] - xs_on[k]) > 8)
    mx = max((abs(xs_off[k] - xs_on[k]) for k in range(n)), default=0)

    # how many frames had 2+ faces, and how many turn changes landed there
    two = 0
    det_frames = 0
    for f in range(n):
        pass
    # re-derive detections cheaply: only need the count at change frames, so
    # approximate "2-face share" from the camera path being ambiguous is not
    # possible — do a real detection pass once here.
    dets = {}
    frame_i = 0
    prev = None
    import cv2
    import numpy as np
    for frame in ss.decode_frames(SRC, start, dur, min(ss.DETECT_MAX_WIDTH, w)):
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.int16)
        cut = prev is not None and float(np.mean(np.abs(g - prev))) >= ss.CUT_THRESHOLD
        prev = g
        if frame_i % ss.DETECT_STRIDE == 0 or cut:
            c = ss.detect_face_candidates(frame)
            dets[frame_i] = len(c)
        frame_i += 1
    det_frames = len(dets)
    two = sum(1 for v in dets.values() if v >= 2)
    at2 = sum(1 for f in changes
              if any(k in dets and dets[k] >= 2 for k in range(f, min(f + ss.DETECT_STRIDE + 1, frame_i))))

    tot_moved += moved
    tot_frames += n
    tot_changes += len(changes)
    tot_at2 += at2
    print(f"{name:8} {dur:6.1f} {len(turns):6d} {len(changes):4d} "
          f"{100*two/max(det_frames,1):7.1f}% {at2:5d}/{len(changes):<4d} "
          f"{100*moved/max(n,1):6.1f}% {mx:6.0f}")

print()
print(f"TOTAL frames {tot_frames} | frames whose crop diarization changed: "
      f"{tot_moved} ({100*tot_moved/max(tot_frames,1):.1f}%)")
print(f"TOTAL turn changes {tot_changes} | of those landing where 2 faces were "
      f"detected: {tot_at2}")
