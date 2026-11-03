#!/usr/bin/env python3
"""
shortsmith.py — local vertical-shorts generator
=======================================================================
Face-aware reframing + layout engine, modelled on the approach analysed
in github.com/mutonby/openshorts and applied to this machine (no Docker,
no cloud, no Gemini).

What it does, per clip
-----------------------
1. FACE DETECTION  — MediaPipe BlazeFace *full-range* (the same family
   OpenShorts uses at model_selection=1), run on a <=640px copy.
   Falls back to OpenCV YuNet if MediaPipe is unavailable.
2. SPEAKER TRACKING — distance-based face identity match with score
   decay, a sticky bonus for the current subject and a switch cooldown,
   so the camera does not ping-pong between two people.
3. SMOOTHED CAMERA  — "heavy tripod": a safe zone around the current
   centre, slow linear pans, and a jump-confirmation gate that rejects
   one-off detector outliers. Snaps (does not pan) at scene cuts.
4. LAYOUT ENGINE    — per sub-range, decides
     TRACK  : one face -> 9:16 crop that follows the speaker
     SPLIT  : two well-separated faces -> both stacked, one per half
   which is the OpenShorts behaviour: it stacks a two-shot instead of
   shrinking it into the middle of the picture.
5. CAPTIONS         — real word timings from faster-whisper, burned as
   karaoke-style ASS: white text, black outline, the spoken word gold.
   On SPLIT ranges the caption is anchored to the seam between halves
   (the one place it covers nobody) exactly as OpenShorts does.

Output: 1080x1920 H.264 + AAC, loudness-normalised.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import cv2

# ======================================================================
# CONFIG
# ======================================================================
# Defaults, all overridable — by the CLI flags in main() or by these env vars,
# which is how make_shorts.py drives this file for an arbitrary input video.
_HERE = os.path.dirname(os.path.abspath(__file__))


def _local_setting(key):
    """A machine-specific setting from local_config.json, or None.

    Personal paths stay out of the repository: a template carrying one
    developer's own video path is useless to anyone else and needless noise.
    Put yours in local_config.json (gitignored) — e.g. {"source": "D:/v.mp4"} —
    or just pass --input / set the environment variable.
    """
    try:
        with open(os.path.join(_HERE, "local_config.json"), encoding="utf-8") as f:
            return json.load(f).get(key)
    except (OSError, ValueError):
        return None


SRC = os.environ.get("SHORTSMITH_SRC") or _local_setting("source") or ""
OUT_DIR = os.environ.get("SHORTSMITH_OUT") or os.path.join(
    os.path.expanduser("~"), "Videos", "output")
WORK_ROOT = os.environ.get("SHORTSMITH_WORK") or os.path.join(_HERE, "shortsmith_work")
MODELS = os.environ.get("SHORTSMITH_MODELS") or os.path.join(_HERE, "models")

# Video encoder. libx264 is the default on purpose: it is in every ffmpeg build
# and gives the best quality per bit, which is exactly what matters when a 480p
# source is being blown up to 1080x1920. Hardware encoders are several times
# faster and slightly softer at the same size, so they are opt-in rather than
# chosen silently on your behalf -- set SHORTSMITH_ENCODER to one of these.
_HW_ENCODERS = {
    "nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19"],
    "qsv": ["-c:v", "h264_qsv", "-global_quality", "20"],
    "amf": ["-c:v", "h264_amf", "-quality", "balanced",
            "-rc", "cqp", "-qp_i", "19", "-qp_p", "21"],
    "videotoolbox": ["-c:v", "h264_videotoolbox", "-q:v", "60"],
}
ENCODER = (os.environ.get("SHORTSMITH_ENCODER") or "").lower()


def encoder_args():
    """The -c:v flags for this run -- software by default, hardware on request."""
    return _HW_ENCODERS.get(ENCODER,
                            ["-c:v", "libx264", "-preset", "medium", "-crf", "18"])

TARGET_W, TARGET_H = 1080, 1920
ASPECT_RATIO = TARGET_W / TARGET_H          # 0.5625 (9:16)
FPS_FALLBACK = 24.0

# Face detection ---------------------------------------------------------
DETECT_MAX_WIDTH = 640          # OpenShorts: run detectors on a <=640px copy
MIN_DETECTION_CONFIDENCE = 0.4

# Camera -----------------------------------------------------------------
JUMP_CONFIRM_FRAMES = 3         # big moves must repeat this many detections
DETECT_STRIDE = 4               # detect every Nth frame; camera interpolates
SAFE_ZONE_FRACTION = 0.25       # of crop width
PAN_SLOW = 3.0                  # px/frame when inside a normal move
PAN_FAST = 15.0                 # px/frame for a very large move
# Mean abs frame difference (on the downscaled analysis stream) that marks a
# scene cut. Measured on this source: normal motion peaks at ~2.7 (p95) while
# every real cut scores 41-69, so anything in that gap is safe. At a cut the
# camera must SNAP to the new subject and the tracker must forget the old one —
# without this the camera pans across the cut and the shot visibly slides.
CUT_THRESHOLD = 12.0

# Split (two-shot) layout ------------------------------------------------
MIN_COEXISTENCE = 0.5           # fraction of samples needing BOTH faces
MIN_SEPARATION = 0.20           # face-centre gap, fraction of frame width
MIN_FACE_WIDTH = 0.045          # smaller than this = background face
# Below this a stacked layout reads as a glitch rather than a choice: the viewer
# needs a beat to understand the split before it cuts away. The upstream repo
# uses 2.5s, but that assumes a hard cut on each side; this pipeline dips to
# black at both joins, so a short stack costs two transitions for a section the
# viewer barely registers (measured: a 3.5s stack put two dips inside the first
# five seconds of a 31s clip). 5.0s makes a stack worth the transitions.
MIN_RANGE_SECONDS = 5.0
# Fraction of source height per half-crop. The repo uses 0.8, which assumes a
# 1080p source where a 1215x864 half still reads as a medium shot. On this
# 854x480 source the two-shot faces are only ~0.07 of frame width, so 0.8 gave
# each half a wide table shot with the speaker small in the corner. 0.55 frames
# head-and-shoulders instead, which makes the stacked halves comparable in
# apparent size to the TRACK crop (and the upscale factor similar, ~3.6x vs 4x).
SPLIT_TIGHTNESS = 0.55

# Quality: the source is 854x480 and the canvas is 1080x1920, so every clip is a
# ~4x upscale and the default bicubic resampler leaves it soft. Lanczos plus a
# mild luma unsharp recovers perceived detail without ringing.
UPSCALE_FLAGS = "flags=lanczos"
SHARPEN = "unsharp=5:5:0.6:5:5:0.0"

# Motion: talking-head footage is static for tens of seconds at a time, which
# reads as dead on a phone. A very slow push-in adds life without being
# noticeable as a move. The rate is held roughly constant and clamped, so a 20s
# shot and a 40s shot both drift at a similar, unhurried pace; direction
# alternates per segment so consecutive shots do not all creep the same way.
MOTION_ENABLED = True
MOTION_RATE_PER_SEC = 0.008     # fraction of frame per second (0.8%/s)
MOTION_MIN = 0.06
MOTION_MAX = 0.16

# The push is EASED, not linear. A constant-rate zoom reads as robotic — the
# craft consensus for vertical video is "ease, never linear" for a slow drift —
# and magnitudes stay small ("big fast moves amplify sampling stutter and read
# as cheap"). We use a smoothstep, 3p^2 - 2p^3, in place of the old linear ramp.
MOTION_EASE = True

# Punch-ins on vocal stress, borrowed from the edit-decision-list planner in
# OpenShorts' editor.py: a short in-and-out pulse landing on the words the
# speaker leans on. Strength and length sit inside its stated ranges
# (0.04-0.10 and 0.4-1.2s) with a minimum gap, so it reads as emphasis rather
# than a twitch. Single-speaker stretches only — pulsing a stacked two-shot
# would zoom into both faces at once. Captions are burned in a later pass, so
# a pulse can never crop the text.
PUNCH_ENABLED = True
PUNCH_STRENGTH = 0.075          # peak extra zoom at the middle of the pulse
PUNCH_SECONDS = 0.55            # pulse length
PUNCH_MIN_GAP = 3.0             # never two pulses closer than this
PUNCH_MAX_PER_SEGMENT = 3       # matches the "<=3 big zooms/min" guidance
PUNCH_EDGE_MARGIN = 0.8         # keep pulses off the very start/end
PUNCH_MIN_SEGMENT = 4.0         # a very short segment gets no pulse at all

# Head/tail fades, applied AFTER captions are burned so the text fades with the
# picture. These are the transitions that actually belong on a short; the middle
# of the clip does not get one.
HEAD_FADE_SECONDS = 0.30
TAIL_FADE_SECONDS = 0.40

# Captions ---------------------------------------------------------------
FONT_NAME = "Arial"
CAPTION_FONTSIZE = 70
CAPTION_OUTLINE = 5
CAPTION_MARGIN_V = 320          # from the bottom, clear of platform UI
# Width is enforced in PIXELS, not characters — the old char budget was ~2x
# over. Measured on the face libass actually renders (Arial BOLD, because the
# style sets Bold=-1), uppercase averages ~44px per character at fontsize 70,
# so "WE'RE STRUGGLING" alone paints 706px and the 920px usable width holds
# only about 20 characters. A 32-char budget therefore ran past the frame edge
# and wrapped mid-phrase, which is what produced fragments like
# "ARK GLASSES. WE'RE GETTI". Measuring the painted width of each candidate
# phrase and breaking on that is exact, whatever the font does.
#
# The limits match the subtitle-readability research: 16-20 characters per
# line, 3-5 words per chunk, ~0.7s minimum on screen, and a ~17 characters per
# second ceiling (Netflix timed-text template; Szarkowska & Gerber-Moron,
# PLOS ONE 2018, on reading speed). Vertical frames are narrow, so the 42-char
# cinematic limit does not apply.
CAPTION_MAX_WORDS = 4
CAPTION_MAX_SECONDS = 2.2
CAPTION_MAX_PX = 880            # painted-width budget in the 1080 frame
CAPTION_MAX_CHARS = 22          # fallback only, if the font cannot be loaded
CAPTION_MIN_SECONDS = 0.70      # minimum time a phrase stays on screen
CAPTION_MAX_CPS = 17.0          # characters per second ceiling
CAPTION_FONT_FILE = "C:/Windows/Fonts/arialbd.ttf"
CAPTION_MIN_WORDS_FOR_PUNCT_BREAK = 3   # don't break on a 1-2 word stub
WHISPER_MODEL = "base"
WHISPER_LANG = "en"

# ======================================================================
# FACE DETECTION
# ======================================================================
_DETECTOR = None
_DETECTOR_KIND = None


def _load_detector():
    """BlazeFace (MediaPipe Tasks) first, YuNet as the fallback."""
    global _DETECTOR, _DETECTOR_KIND
    if _DETECTOR is not None:
        return

    blaze = os.path.join(MODELS, "blaze_face_full_range.tflite")
    if os.path.exists(blaze):
        try:
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
            opts = vision.FaceDetectorOptions(
                base_options=mp_python.BaseOptions(model_asset_path=blaze),
                min_detection_confidence=MIN_DETECTION_CONFIDENCE)
            _DETECTOR = vision.FaceDetector.create_from_options(opts)
            _DETECTOR_KIND = "blazeface-full-range"
            print("  detector: MediaPipe BlazeFace full-range")
            return
        except Exception as e:                       # noqa: BLE001
            print(f"  ! BlazeFace unavailable ({e}) — falling back to YuNet")

    yunet = os.path.join(MODELS, "yunet.onnx")
    if os.path.exists(yunet):
        _DETECTOR = cv2.FaceDetectorYN.create(yunet, "", (320, 320), 0.6, 0.3, 5000)
        _DETECTOR_KIND = "yunet"
        print("  detector: OpenCV YuNet")
        return

    raise RuntimeError("No face detector model found in " + MODELS)


def _detect_small(small_bgr):
    """Detect on an already-downscaled frame; returns boxes in SMALL coords."""
    out = []
    if _DETECTOR_KIND == "blazeface-full-range":
        import mediapipe as mp
        rgb = cv2.cvtColor(small_bgr, cv2.COLOR_BGR2RGB)
        res = _DETECTOR.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        h, w = small_bgr.shape[:2]
        for d in (res.detections or []):
            bb = d.bounding_box
            x0 = max(0, bb.origin_x); y0 = max(0, bb.origin_y)
            x1 = min(w, bb.origin_x + bb.width); y1 = min(h, bb.origin_y + bb.height)
            if x1 <= x0 or y1 <= y0:
                continue
            out.append({"box": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
                        "score": float(d.categories[0].score) if d.categories else 0.0})
    else:
        h, w = small_bgr.shape[:2]
        _DETECTOR.setInputSize((w, h))
        _, faces = _DETECTOR.detect(small_bgr)
        if faces is not None:
            for f in faces:
                x, y, fw, fh = f[:4]
                out.append({"box": [int(x), int(y), int(fw), int(fh)],
                            "score": float(f[-1])})
    return out


def detect_face_candidates(frame):
    """All faces in ORIGINAL frame coordinates (detection runs downscaled)."""
    h, w = frame.shape[:2]
    if w <= DETECT_MAX_WIDTH:
        small, scale = frame, 1.0
    else:
        scale = w / float(DETECT_MAX_WIDTH)
        small = cv2.resize(frame, (DETECT_MAX_WIDTH, max(int(h / scale), 2)),
                           interpolation=cv2.INTER_AREA)
    cands = []
    for c in _detect_small(small):
        x, y, cw, ch = c["box"]
        cands.append({"box": [int(x * scale), int(y * scale),
                              int(cw * scale), int(ch * scale)],
                      "score": c["score"]})
    cands.sort(key=lambda c: c["box"][2] * c["box"][3], reverse=True)
    return cands


# ======================================================================
# SPEAKER TRACKER  (ported from openshorts/main.py)
# ======================================================================
class SpeakerTracker:
    """Picks which face to follow, with hysteresis so it cannot flip-flop."""

    def __init__(self, stabilization_frames=15, cooldown_frames=30):
        self.active_speaker_id = None
        self.speaker_scores = {}
        self.known_faces = []
        self.locked_counter = 0
        self.switch_cooldown = cooldown_frames
        self.last_switch_frame = -1000
        self.next_id = 0

    def reset(self):
        self.active_speaker_id = None
        self.speaker_scores = {}
        self.known_faces = []
        self.locked_counter = 0
        self.last_switch_frame = -1000

    def get_target(self, face_candidates, frame_number, width, force_other=False):
        current = []
        for face in face_candidates:
            x, y, w, h = face["box"]
            center_x = x + w / 2.0
            best_id, best_dist = -1, width * 0.15
            for kf in self.known_faces:
                if frame_number - kf["last_frame"] > 30:
                    continue
                d = abs(center_x - kf["center"])
                if d < best_dist:
                    best_dist, best_id = d, kf["id"]
            if best_id == -1:
                best_id = self.next_id
                self.next_id += 1
            self.known_faces = [k for k in self.known_faces if k["id"] != best_id]
            self.known_faces.append({"id": best_id, "center": center_x,
                                     "last_frame": frame_number})
            current.append({"id": best_id, "box": face["box"], "score": face["score"]})

        for pid in list(self.speaker_scores):
            self.speaker_scores[pid] *= 0.85
            if self.speaker_scores[pid] < 0.1:
                del self.speaker_scores[pid]

        for cand in current:
            pid = cand["id"]
            raw = cand["score"] / (width * width * 0.05)
            self.speaker_scores[pid] = self.speaker_scores.get(pid, 0) + raw

        if not current:
            return None

        # Diarization says the speaker just changed. In a two-shot the OTHER
        # face is now the one talking, so switch to it deliberately rather than
        # waiting for the size/sticky scoring to notice — it may never, because
        # it keys off face SIZE, and the silent listener can easily be the
        # nearer or larger person in frame.
        #
        # This deliberately needs no face<->speaker identity mapping (which
        # diarization cannot give us: it numbers voices, not pixels). It only
        # relies on turns ALTERNATING, which is what a conversation does.
        if force_other and len(current) >= 2:
            other = next((c for c in current if c["id"] != self.active_speaker_id), None)
            if other is not None:
                self.active_speaker_id = other["id"]
                self.last_switch_frame = frame_number
                self.locked_counter = 0
                return other["box"]

        best, max_score = None, -1
        for cand in current:
            total = self.speaker_scores.get(cand["id"], 0)
            if cand["id"] == self.active_speaker_id:
                total *= 3.0                       # sticky
            if total > max_score:
                max_score, best = total, cand

        if not best:
            return None
        if best["id"] == self.active_speaker_id:
            self.locked_counter += 1
            return best["box"]

        if frame_number - self.last_switch_frame < self.switch_cooldown:
            old = next((c for c in current if c["id"] == self.active_speaker_id), None)
            return old["box"] if old else None

        self.active_speaker_id = best["id"]
        self.last_switch_frame = frame_number
        self.locked_counter = 0
        return best["box"]


# ======================================================================
# SMOOTHED CAMERAMAN  (ported from openshorts/main.py)
# ======================================================================
class SmoothedCameraman:
    """'Heavy tripod': hold still inside a safe zone, pan slowly outside it."""

    def __init__(self, out_w, out_h, video_w, video_h, aspect_ratio=ASPECT_RATIO):
        self.output_width = out_w
        self.output_height = out_h
        self.video_width = video_w
        self.video_height = video_h
        self.aspect_ratio = aspect_ratio

        self.current_center_x = video_w / 2.0
        self.target_center_x = video_w / 2.0

        self.crop_height = video_h
        self.crop_width = int(self.crop_height * aspect_ratio)
        if self.crop_width > video_w:
            self.crop_width = video_w
            self.crop_height = int(self.crop_width / aspect_ratio)

        self.safe_zone_radius = self.crop_width * SAFE_ZONE_FRACTION
        self.jump_confirm_frames = JUMP_CONFIRM_FRAMES
        self._pending_target = None
        self._pending_count = 0
        self._snap_pending = False

    def begin_scene(self):
        self._pending_target = None
        self._pending_count = 0
        self._snap_pending = True

    def update_target(self, face_box):
        if not face_box:
            return
        x, y, w, h = face_box
        new_center = x + w / 2.0

        if self._snap_pending:
            self._snap_pending = False
            self._pending_target = None
            self._pending_count = 0
            self.target_center_x = new_center
            self.current_center_x = new_center
            return

        if abs(new_center - self.target_center_x) > self.safe_zone_radius:
            if (self._pending_target is not None
                    and abs(new_center - self._pending_target) <= self.safe_zone_radius):
                self._pending_count += 1
            else:
                self._pending_target = new_center
                self._pending_count = 1
            if self._pending_count < self.jump_confirm_frames:
                return

        self._pending_target = None
        self._pending_count = 0
        self.target_center_x = new_center

    def get_crop_box(self, force_snap=False):
        if force_snap:
            self.current_center_x = self.target_center_x
        else:
            diff = self.target_center_x - self.current_center_x
            if abs(diff) > self.safe_zone_radius:
                direction = 1 if diff > 0 else -1
                speed = PAN_FAST if abs(diff) > self.crop_width * 0.5 else PAN_SLOW
                self.current_center_x += direction * speed
                new_diff = self.target_center_x - self.current_center_x
                if (direction == 1 and new_diff < 0) or (direction == -1 and new_diff > 0):
                    self.current_center_x = self.target_center_x

        half = self.crop_width / 2.0
        if self.current_center_x - half < 0:
            self.current_center_x = half
        if self.current_center_x + half > self.video_width:
            self.current_center_x = self.video_width - half

        x1 = max(0, int(self.current_center_x - half))
        x2 = min(self.video_width, int(self.current_center_x + half))
        return x1, 0, x2, self.video_height


# ======================================================================
# SOURCE PROBE / DECODE
# ======================================================================
def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    parts = r.stdout.strip().split(",")
    w, h = int(parts[0]), int(parts[1])
    if len(parts) > 2 and "/" in parts[2]:
        n, d = parts[2].split("/")
        fps = float(n) / float(d) if float(d) else FPS_FALLBACK
    else:
        fps = FPS_FALLBACK
    return w, h, fps


def decode_frames(path, start, duration, width, pix="bgr24"):
    """Yield raw frames of a (downscaled) segment via one ffmpeg pipe."""
    h_probe = probe(path)[1]
    w = min(width, probe(path)[0])
    w -= w % 2
    h = max(int(h_probe * w / probe(path)[0]), 2)
    h += h % 2
    nch = 3 if pix == "bgr24" else 1
    fb = w * h * nch
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.4f}", "-t", f"{duration:.4f}",
           "-i", path, "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", pix, "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         bufsize=fb * 4)
    try:
        while True:
            buf = p.stdout.read(fb)
            if len(buf) < fb:
                break
            if nch == 3:
                yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            else:
                yield np.frombuffer(buf, np.uint8).reshape(h, w)
    finally:
        p.stdout.close()
        p.wait()


# ======================================================================
# ANALYSIS PASS -> per-frame crop x
# ======================================================================
try:                                             # optional: works without it
    import diarize as _diarize
except Exception:                                # noqa: BLE001
    _diarize = None


def diarize_speaker_turns(path, start, duration, num_speakers=2):
    """Speaker turns for the clip window, or [] if unavailable.

    Never raises and never blocks a render: without turns the camera simply
    falls back to the face-size heuristic it used before.
    """
    if _diarize is None or os.environ.get("SHORTSMITH_NO_DIARIZE") == "1":
        return []
    return _diarize.speaker_turns(path, start, duration, num_speakers=num_speakers)


def speaker_change_frames(turns, fps, n_frames, min_gap=0.7):
    """Frame indices where the SPEAKING TURN changes.

    `turns` are window-relative seconds, matching this loop's frame clock, so no
    offset is needed. Overlapping turns happen (a diarizer can emit a short
    interjection inside a long turn), so the timeline is resolved by taking the
    turn that starts last, and switches closer together than `min_gap` are
    dropped — a real conversation does not change speaker twice a second, and
    flapping the camera would be worse than not switching at all.
    """
    if not turns:
        return set()
    ordered = sorted(turns, key=lambda x: (x["start"], -(x["end"] - x["start"])))
    marks, cur, last = set(), None, -1e9
    for x in ordered:
        if cur is not None and x["speaker"] != cur:
            f = int(round(x["start"] * fps))
            if 0 <= f < n_frames and (x["start"] - last) >= min_gap:
                marks.add(f)
                last = x["start"]
        cur = x["speaker"]
    return marks


def analyze_camera(path, start, duration, fps, orig_w, orig_h, turns=None):
    """Replay the tracker + cameraman over the segment.

    Returns (xs, cameraman, cut_frames). Mirrors openshorts' per-frame decision
    loop *including* its scene-cut reset (SCENE_CUT_RESET): a cut forgets the
    previous shot's speaker and SNAPS the camera to the new subject instead of
    panning to it. Without that the shot visibly slides for a second or two
    after every cut, which is what made the first render feel wrong.

    With `turns` (from diarize.py) the camera ALSO jumps to the other face when
    the speaking turn changes, so it follows whoever is actually talking instead
    of whoever the size heuristic prefers.

    Two passes: the first decodes once and remembers what was detected where;
    the second replays the decision loop. Detection dominates the cost, so it
    must happen once — the replay is pure arithmetic.
    """
    small_w = min(DETECT_MAX_WIDTH, orig_w)
    small_w -= small_w % 2
    scale = orig_w / float(small_w)

    # ---- pass 1: decode + detect, remembering detections and cut frames
    dets, cuts, n = {}, [], 0
    prev_gray = None
    for frame in decode_frames(path, start, duration, small_w):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.int16)
        is_cut = False
        if prev_gray is not None:
            is_cut = float(np.mean(np.abs(gray - prev_gray))) >= CUT_THRESHOLD
        prev_gray = gray
        if is_cut:
            cuts.append(n)
        # Detect on the stride AND on every cut frame (a new shot's subject
        # must be found immediately, not up to DETECT_STRIDE frames later).
        if n % DETECT_STRIDE == 0 or is_cut:
            cands = detect_face_candidates(frame)
            for c in cands:
                c["box"] = [int(v * scale) for v in c["box"]]
                c["score"] = c["box"][2] * c["box"][3]
            dets[n] = cands
        n += 1

    n_frames = n
    cut_set = set(cuts)
    changes = speaker_change_frames(turns, fps, n_frames)

    # ---- pass 2: replay the decision loop
    cameraman = SmoothedCameraman(TARGET_W, TARGET_H, orig_w, orig_h)
    tracker = SpeakerTracker(cooldown_frames=30)
    xs = []
    pending_switch = False
    for n in range(n_frames):
        is_cut = n in cut_set
        if is_cut:
            tracker.reset()
            cameraman.begin_scene()
            # Neutral until a face lands in the new shot, so we never snap to a
            # stale off-centre position from the previous shot.
            cameraman.target_center_x = orig_w / 2.0
            cameraman.current_center_x = orig_w / 2.0

        # A turn began. Detections only run every DETECT_STRIDE frames, and a
        # turn boundary rarely lands on one of them, so carry the request
        # forward and act on the next frame we actually have faces for —
        # otherwise the switch is silently dropped.
        if n in changes:
            pending_switch = True

        if n in dets:
            # The other face is talking now. A cut already reset the tracker, so
            # only force the switch within a shot.
            box = tracker.get_target(dets[n], n, orig_w,
                                     force_other=(pending_switch and not is_cut))
            if pending_switch:
                pending_switch = False
            if box:
                cameraman.update_target(box)

        x1, _, _, _ = cameraman.get_crop_box(force_snap=(n == 0 or is_cut))
        xs.append(x1)
    return xs, cameraman, cuts


# ======================================================================
# LAYOUT PASS -> split ranges (two-shots)
# ======================================================================
def _two_largest(cands, frame_w):
    kept = []
    for c in sorted(cands, key=lambda c: c["box"][2] * c["box"][3], reverse=True):
        ok = True
        for k in kept:                                # crude dedupe by IoU
            ax, ay, aw, ah = c["box"]; bx, by, bw, bh = k["box"]
            ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
            iy = max(0, min(ay + ah, by + bh) - max(ay, by))
            inter = ix * iy
            union = aw * ah + bw * bh - inter
            if union > 0 and inter / union > 0.30:
                ok = False
                break
        if ok:
            kept.append(c)
    big = [c for c in kept if c["box"][2] >= MIN_FACE_WIDTH * frame_w]
    if len(big) < 2:
        return None
    big.sort(key=lambda c: c["box"][2] * c["box"][3], reverse=True)
    a, b = big[0], big[1]
    return tuple(sorted((a, b), key=lambda c: c["box"][0]))


def detect_split_ranges(path, start, duration, orig_w, sample_fps=2.0):
    """Contiguous stretches that read as a stackable two-shot.

    Returns [(t0, t1, left_centre_norm, right_centre_norm)] in seconds relative
    to `start`; centres are (cx, cy) as fractions of the source frame.
    """
    step = 1.0 / sample_fps
    samples = []          # (t, left_norm, right_norm) or (t, None, None)
    t = 0.0
    # sample by decoding at sample_fps
    h_probe = probe(path)[1]
    sw = min(DETECT_MAX_WIDTH, orig_w); sw -= sw % 2
    sh = max(int(h_probe * sw / orig_w), 2); sh += sh % 2
    fb = sw * sh * 3
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.4f}", "-t", f"{duration:.4f}",
           "-i", path, "-vf", f"fps={sample_fps},scale={sw}:{sh}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         bufsize=fb * 4)
    try:
        while True:
            buf = p.stdout.read(fb)
            if len(buf) < fb:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(sh, sw, 3)
            pair = _two_largest(detect_face_candidates(frame), sw)
            if pair:
                lc = pair[0]["box"]; rc = pair[1]["box"]
                lcx = (lc[0] + lc[2] / 2) / sw; lcy = (lc[1] + lc[3] / 2) / sh
                rcx = (rc[0] + rc[2] / 2) / sw; rcy = (rc[1] + rc[3] / 2) / sh
                if (rcx - lcx) >= MIN_SEPARATION:
                    samples.append((t, (lcx, lcy), (rcx, rcy)))
                else:
                    samples.append((t, None, None))
            else:
                samples.append((t, None, None))
            t += step
    finally:
        p.stdout.close()
        p.wait()

    if not samples:
        return []

    # group runs of qualifying samples
    ranges, run = [], []
    for s in samples:
        if s[1] is not None:
            run.append(s)
        else:
            if run:
                ranges.append(run); run = []
    if run:
        ranges.append(run)

    out = []
    for run in ranges:
        t0, t1 = run[0][0], run[-1][0] + step
        if (t1 - t0) < MIN_RANGE_SECONDS:
            continue
        lx = float(np.median([r[1][0] for r in run]))
        ly = float(np.median([r[1][1] for r in run]))
        rx = float(np.median([r[2][0] for r in run]))
        ry = float(np.median([r[2][1] for r in run]))
        # also require both faces present for MIN_COEXISTENCE of the CLIP window
        out.append((round(t0, 2), round(t1, 2), (lx, ly), (rx, ry)))
    return out


# ======================================================================
# FILTERGRAPH BUILDERS
# ======================================================================
def stress_peaks(path, start, dur, max_peaks=PUNCH_MAX_PER_SEGMENT, hop=0.05):
    """Vocal-stress moments in a clip window -> punch-in times, in seconds.

    A speaker leans on a word by getting louder, so an RMS surge inside the
    window is a workable proxy for emphasis — the same signal OpenShorts' EDL
    planner keys its punch-ins off. Audio that never rises above its own mean
    yields nothing, so a flat, level close-up simply gets no pulses rather than
    arbitrary ones. Times are relative to the start of the window, matching the
    segment-relative `t` the motion expression is evaluated against.
    """
    if not PUNCH_ENABLED or dur < PUNCH_MIN_SEGMENT:
        return []
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{start:.4f}", "-t", f"{dur:.4f}",
             "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
            capture_output=True, timeout=300).stdout
        if not raw:
            return []
        a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
        n = max(1, int(hop * 16000))
        if len(a) < n * 8:
            return []
        frames = a[:len(a) // n * n].reshape(-1, n)
        rms = np.sqrt((frames ** 2).mean(axis=1))
        k = max(1, int(0.12 / hop))                  # ~120 ms smoothing
        env = np.convolve(rms, np.ones(k) / k, mode="same")
        thr = env.mean() + 0.9 * env.std()
        cand = []
        for i in range(1, len(env) - 1):
            if env[i] <= thr or env[i] < env[i - 1] or env[i] <= env[i + 1]:
                continue
            t = i * hop
            # Same guard motion_scale applies, so the times reported in the log
            # are exactly the pulses that get drawn — otherwise a peak near the
            # tail is announced and then silently dropped.
            if t < PUNCH_EDGE_MARGIN or t + PUNCH_SECONDS > dur - PUNCH_EDGE_MARGIN:
                continue
            cand.append((float(env[i]), t))
        if not cand:
            return []
        # Take the strongest peaks, not the earliest: they are not the same
        # thing, and the emphasis we want is by definition the loudest ones.
        # Spacing is then enforced greedily, strongest first.
        cand.sort(key=lambda c: -c[0])
        chosen = []
        for _, t in cand:
            if all(abs(t - c) >= PUNCH_MIN_GAP for c in chosen):
                chosen.append(t)
            if len(chosen) >= max_peaks:
                break
        return sorted(chosen)
    except Exception:                                # noqa: BLE001
        return []


def motion_scale(out_w, out_h, dur, seg_index, punches=None):
    """A slow animated zoom, as failable-over scale+crop filters.

    Two motions ride on one per-frame expression: a slow base push (a pull on
    alternate segments) eased with a smoothstep, because a constant-rate move
    reads as mechanical; and short punch-in pulses on the stressed words found
    by stress_peaks().

    Held as a per-frame expression so it keeps the source crop as the sampling
    base (we are upscaling from ~270x480 anyway, so the extra factor costs
    almost nothing in sharpness). Dimensions are forced even — yuv420 rejects
    odd ones — and the trailing crop recentres, which crop does by default.
    """
    if not MOTION_ENABLED or dur <= 0:
        return f"scale={out_w}:{out_h}:{UPSCALE_FLAGS}"
    z = min(MOTION_MAX, max(MOTION_MIN, MOTION_RATE_PER_SEC * dur))
    if seg_index % 2 == 0:
        z0, z1 = 1.0, 1.0 + z        # push in
    else:
        z0, z1 = 1.0 + z, 1.0        # pull out
    rate = z1 - z0

    p = f"(t/{dur:.4f})"
    if MOTION_EASE:
        # smoothstep — zero slope at both ends, so the move eases off the mark
        # and settles, instead of switching on at a constant speed
        ramp = f"(3*pow({p},2)-2*pow({p},3))"
    else:
        ramp = p
    expr = f"({z0:.4f}+{rate:.4f}*{ramp})"

    # Emphasis pulses: a parabolic bump 4u(1-u) that is 0 at both ends and 1 at
    # the middle, so it grows and releases rather than snapping on. The
    # between() factor zeroes the term outside the pulse, which is also what
    # keeps u from being evaluated meaningfully at a distance.
    for tk in (punches or []):
        if tk < PUNCH_EDGE_MARGIN or tk + PUNCH_SECONDS > dur - PUNCH_EDGE_MARGIN:
            continue
        u = f"((t-{tk:.3f})/{PUNCH_SECONDS:.3f})"
        expr += (f"+{PUNCH_STRENGTH:.4f}*4*{u}*(1-{u})"
                 f"*between(t,{tk:.3f},{tk + PUNCH_SECONDS:.3f})")

    # The extra parens matter: expr already carries the pulse terms, and
    # without wrapping the whole thing, `out_w*expr` would bind to the first
    # parenthesised group only — leaving the pulses added as 0.075 *pixels*
    # outside the multiplication, i.e. invisible.
    ew = f"2*trunc({out_w}*({expr})/2)"
    eh = f"2*trunc({out_h}*({expr})/2)"
    return (f"scale=w='{ew}':h='{eh}':eval=frame:{UPSCALE_FLAGS},"
            f"crop={out_w}:{out_h}")


def track_graph(xs, crop_w, crop_h, seg_start_offset, fps, out_w, out_h, workdir, idx, dur,
                punches=None):
    """sendcmd-driven dynamic crop that follows the speaker."""
    lines = []
    prev = None
    for i, x in enumerate(xs):
        if x != prev:
            lines.append(f"{i / fps:.4f} crop@c x {int(x)};")
            prev = x
    cmd_path = os.path.join(workdir, f"cmd_{idx:03d}.txt")
    with open(cmd_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    crop_y = max(0, (probe(SRC)[1] - crop_h) // 2)
    init = f"w={crop_w}:h={crop_h}:x={int(xs[0]) if xs else 0}:y={crop_y}"
    return (f"[0:v]sendcmd=f='{os.path.basename(cmd_path)}',crop@c={init},"
            f"{motion_scale(out_w, out_h, dur, idx, punches)},{SHARPEN},setsar=1[v]"), cmd_path


def split_graph(orig_w, orig_h, out_w, out_h, left_c, right_c, dur=0.0, seg_index=0):
    """vstack of two face-framed half crops; left-hand speaker on top."""
    half_h = out_h // 2
    half_h -= half_h % 2
    aspect = out_w / float(half_h)

    def geom(c):
        crop_h = int(round(orig_h * max(0.3, min(SPLIT_TIGHTNESS, 1.0))))
        crop_w = int(round(crop_h * aspect))
        if crop_w > orig_w:
            crop_w = orig_w
            crop_h = int(round(crop_w / aspect))
        crop_w -= crop_w % 2
        crop_h -= crop_h % 2
        cx = c[0] * orig_w
        cy = c[1] * orig_h
        x = int(round(cx - crop_w / 2.0))
        x = max(0, min(x, orig_w - crop_w))
        y = int(round(cy - crop_h * 0.42))
        y = max(0, min(y, orig_h - crop_h))
        return crop_w, crop_h, x - (x % 2), y - (y % 2)

    tw, th, tx, ty = geom(left_c)
    bw, bh, bx, by = geom(right_c)
    return (
        f"[0:v]split=2[ta][ba];"
        f"[ta]crop=w={tw}:h={th}:x={tx}:y={ty},scale={out_w}:{half_h}:{UPSCALE_FLAGS}[top];"
        f"[ba]crop=w={bw}:h={bh}:x={bx}:y={by},scale={out_w}:{half_h}:{UPSCALE_FLAGS}[bot];"
        f"[top][bot]vstack=inputs=2,pad={out_w}:{out_h}:0:0,"
        f"{motion_scale(out_w, out_h, dur, seg_index)},{SHARPEN},setsar=1[v]"
    )


# ======================================================================
# CAPTIONS
# ======================================================================
def _load_audio_16k(path):
    """Mono 16 kHz float32 samples via ffmpeg (bypasses faster-whisper's PyAV
    decoder, which calls an av.open() argument this PyAV build no longer has)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, timeout=900).stdout
    if not raw:
        return None
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def transcribe(clip_path):
    """Word-level timings for a rendered clip; [] on any failure."""
    try:
        from faster_whisper import WhisperModel
        audio = _load_audio_16k(clip_path)
        if audio is None or len(audio) < 16000:
            return []
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segs, _ = model.transcribe(audio, language=WHISPER_LANG,
                                   word_timestamps=True, vad_filter=True)
        words = []
        for s in segs:
            for w in (s.words or []):
                words.append({"start": float(w.start), "end": float(w.end),
                              "text": w.word.strip()})
        return [w for w in words if w["text"]]
    except Exception as e:                             # noqa: BLE001
        print(f"    ! transcription unavailable ({e}) — rendering without captions")
        return []


def _ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _ends_phrase(text):
    """True when this word ends a natural phrase (punctuation)."""
    return bool(text) and text[-1] in ",.?!;:"


def _edge_fades(dur):
    """Fade in at the head, out at the tail.

    Applied in the FINAL pass, i.e. after captions are burned, so the text fades
    with the picture. Applying a fade in the segment pass left the caption at
    full brightness floating on a black frame, which reads as a broken frame.
    """
    outs = []
    if HEAD_FADE_SECONDS > 0:
        outs.append(f"fade=t=in:st=0:d={HEAD_FADE_SECONDS}")
    if TAIL_FADE_SECONDS > 0:
        st = max(0.0, dur - TAIL_FADE_SECONDS)
        outs.append(f"fade=t=out:st={st:.3f}:d={TAIL_FADE_SECONDS}")
    return ",".join(outs)


_FONT_CACHE = {}


def _text_px(s, size=None):
    """Painted width of a caption line, in frame pixels.

    Measures the real font rather than counting characters: uppercase Arial
    Bold at 70 runs about 44px per character, so a character budget under-
    records the true width by roughly half — which is how a 32-char line ended
    up running past the frame edge and wrapping mid-phrase. Falls back to a
    rough estimate only when the font cannot be loaded, so grouping still
    terminates either way.
    """
    size = size or CAPTION_FONTSIZE
    try:
        from PIL import ImageFont
        f = _FONT_CACHE.get(size)
        if f is None:
            f = ImageFont.truetype(CAPTION_FONT_FILE, size)
            _FONT_CACHE[size] = f
        return float(f.getlength(s))
    except Exception:                                # noqa: BLE001
        return len(s) * size * 0.62


def build_ass(words, out_path, split_ranges):
    """Karaoke-style ASS: full phrase shown, spoken word in gold.

    On a SPLIT stretch the line is anchored mid-frame (\\an5) so it sits on the
    seam between the two stacked speakers and covers neither.
    """
    def in_split(t):
        return any(a <= t < b for a, b in split_ranges)

    # Group words into phrases that read naturally AND fit the frame width.
    # Prefer breaking where the speaker already paused (punctuation) — that is
    # where a reader expects a break — and only fall back to the measured width,
    # word count, time and reading-speed budgets when a phrase runs long. The
    # width test uses the real painted width, so a phrase can never reach the
    # frame edge and wrap.
    phrases, cur = [], []
    for w in words:
        if cur:
            cand = " ".join(x["text"] for x in cur + [w]).upper()
            span = w["end"] - cur[0]["start"]
            too_many = len(cur) >= CAPTION_MAX_WORDS
            too_long = span > CAPTION_MAX_SECONDS
            too_wide = _text_px(cand) > CAPTION_MAX_PX
            # reading speed: a chunk that would flash past faster than ~17 cps
            # cannot be read, so break before adding the word
            too_fast = (span > 0.05 and len(cur) >= 2
                        and len(cand) / span > CAPTION_MAX_CPS)
            punct = (_ends_phrase(cur[-1]["text"])
                     and len(cur) >= CAPTION_MIN_WORDS_FOR_PUNCT_BREAK)
            if too_many or too_long or too_wide or too_fast or punct:
                phrases.append(cur)
                cur = []
        cur.append(w)
    if cur:
        phrases.append(cur)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {TARGET_W}\n"
        f"PlayResY: {TARGET_H}\n"
        "WrapStyle: 0\n"          # smart wrap — safety net so a long line never
                                  # runs past the frame edge and gets clipped
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{FONT_NAME},{CAPTION_FONTSIZE},&H00FFFFFF,&H0000D7FF,"
        f"&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{CAPTION_OUTLINE},0,2,"
        f"80,80,{CAPTION_MARGIN_V},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for pi, ph in enumerate(phrases):
        ph_start = ph[0]["start"]
        nxt = phrases[pi + 1][0]["start"] if pi + 1 < len(phrases) else None
        for i, w in enumerate(ph):
            hi = " ".join(
                (f"{{\\c&H0000D7FF&}}{x['text'].upper()}{{\\c&H00FFFFFF&}}" if j == i
                 else x["text"].upper())
                for j, x in enumerate(ph))
            start = w["start"]
            if i + 1 < len(ph):
                end = ph[i + 1]["start"]
            else:
                # Hold the final line long enough for the whole phrase to be
                # read (>= ~0.7s on screen) — a fast talker would otherwise
                # flash it past. Never into the next phrase, or two chunks would
                # share the frame at once.
                end = max(w["end"], w["start"] + 0.3,
                          ph_start + CAPTION_MIN_SECONDS)
                if nxt is not None and end > nxt:
                    end = max(nxt, w["end"])
            prefix = "{\\an5}" if in_split((start + end) / 2) else ""
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,"
                         f"0,0,0,,{prefix}{hi}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    return len(lines)


# ======================================================================
# RENDER ONE CLIP
# ======================================================================
def run(cmd, cwd=None, timeout=1800):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-800:]}")
    return r


def render_clip(clip, workdir, orig_w, orig_h, fps):
    name = clip["name"]
    start, duration = clip["start"], clip["duration"]
    label = f'{clip.get("title","")}' + (f'  [score {clip["score"]}]' if clip.get("score") else "")
    mm, ss = divmod(int(start), 60)
    print(f"\n=== {name}  ({mm:02d}:{ss:02d}, {duration:.1f}s) ===")
    if label.strip():
        print(f"    {label}")
    if clip.get("hook"):
        print(f'    hook: "{clip["hook"]}"')

    final = os.path.join(OUT_DIR, f"{name}.mp4")

    # ---- 1. camera trajectory + split planning
    print("  [1/5] diarization + tracking faces -> camera path")
    turns = diarize_speaker_turns(SRC, start, duration)
    if turns:
        from diarize import speaking_time
        spk = speaking_time(turns)
        print(f"        {len(turns)} turn(s) | talking time "
              + ", ".join(f"spk{k}: {v:.1f}s" for k, v in sorted(spk.items())))
    else:
        print("        no diarization — following the face-size heuristic only")
    xs, cameraman, cuts = analyze_camera(SRC, start, duration, fps, orig_w, orig_h,
                                         turns=turns)
    if not xs:
        raise RuntimeError("no frames decoded")
    cut_secs = ", ".join(f"{c / fps:.1f}s" for c in cuts)
    print(f"        {len(xs)} frames, crop {cameraman.crop_width}x{cameraman.crop_height}, "
          f"{len(cuts)} cut(s){': ' + cut_secs if cuts else ''}")

    print("  [2/5] detecting two-shot (split) ranges")
    splits = detect_split_ranges(SRC, start, duration, orig_w)
    for t0, t1, lc, rc in splits:
        print(f"        SPLIT {t0:5.1f}s-{t1:5.1f}s  "
              f"left cx={lc[0]:.2f}  right cx={rc[0]:.2f}")
    if not splits:
        print("        none — single-speaker framing throughout")

    # ---- 2. build ranges: split ranges override the track path
    ranges = []                                   # (t0, t1, kind, payload)
    for t0, t1, lc, rc in splits:
        ranges.append((t0, t1, "SPLIT", (lc, rc)))
    # subtract split ranges from the tracking timeline
    def covered(t):
        return any(a <= t < b for a, b, _, _ in ranges)

    t = 0.0
    seg_start = None
    step = 1.0 / fps
    while t < duration:
        if covered(t):
            if seg_start is not None:
                ranges.append((seg_start, t, "TRACK", None))
                seg_start = None
        else:
            if seg_start is None:
                seg_start = t
        t += step
    if seg_start is not None:
        ranges.append((seg_start, duration, "TRACK", None))
    ranges.sort(key=lambda r: r[0])

    # merge adjacent TRACK ranges
    merged = []
    for r in ranges:
        if merged and merged[-1][2] == "TRACK" == r[2] and abs(merged[-1][1] - r[0]) < 0.06:
            merged[-1] = (merged[-1][0], r[1], "TRACK", None)
        else:
            merged.append(tuple(r))
    ranges = merged
    print("  [3/5] ranges: " + ", ".join(
        f"{k} {a:.1f}-{b:.1f}s" for a, b, k, _ in ranges))

    # ---- 3. render each range as its own segment
    segments = []
    ass_path = None
    burn_captions = clip.get("captions", True)
    split_windows = [(a, b) for a, b, k, _ in ranges if k == "SPLIT"]

    # Drop degenerate ranges up front so the neighbour lookups below (which
    # decide where the fades go) line up with the segments actually rendered.
    render_list = [r for r in ranges if (r[1] - r[0]) > 0.05]

    for i, (t0, t1, kind, payload) in enumerate(render_list):
        dur = t1 - t0
        seg = os.path.join(workdir, f"seg_{i:03d}.mp4")
        if kind == "SPLIT":
            g = split_graph(orig_w, orig_h, TARGET_W, TARGET_H, payload[0], payload[1],
                            dur=dur, seg_index=i)
            cmd_path = None
        else:
            f0 = int(round(t0 * fps)); f1 = int(round(t1 * fps))
            seg_xs = xs[f0:f1] or xs[-1:]
            # Punch in on the words the speaker leans on. Single-speaker
            # stretches only: a pulse on a stacked two-shot would zoom into
            # both faces at once.
            punches = stress_peaks(SRC, start + t0, dur)
            if punches:
                print("        punch-in at " + ", ".join(f"{p:.1f}s" for p in punches)
                      + f"  ({PUNCH_SECONDS}s, +{PUNCH_STRENGTH*100:.0f}%)")
            g, cmd_path = track_graph(seg_xs, cameraman.crop_width,
                                      cameraman.crop_height, t0, fps,
                                      TARGET_W, TARGET_H, workdir, i, dur,
                                      punches=punches)

        # No per-segment fade here. A mid-clip dip is the wrong place for a
        # transition — and because captions are burned in a LATER pass, a fade
        # at this stage darkened the video while the caption stayed bright
        # white, which reads as a broken frame rather than an edit. The layout
        # changes already coincide with real cuts in the source, so a clean cut
        # is the correct transition. Head/tail fades are applied after the
        # captions, in the final pass, so text fades with the picture.
        run(["ffmpeg", "-y", "-loglevel", "error",
             "-ss", f"{start + t0:.4f}", "-t", f"{dur:.4f}", "-i", SRC,
             "-filter_complex", g, "-map", "[v]",
             *encoder_args(),
             "-pix_fmt", "yuv420p", "-r", f"{fps}", "-an", seg], cwd=workdir)
        segments.append(seg)

    if not segments:
        raise RuntimeError("no segments rendered")

    # ---- 4. concat video, attach normalised audio
    concat_txt = os.path.join(workdir, "concat.txt")
    with open(concat_txt, "w") as f:
        for s in segments:
            f.write(f"file '{os.path.basename(s)}'\n")

    stitched = os.path.join(workdir, "stitched.mp4")
    # -ss/-t must sit BEFORE "-i SRC" so they seek the long source as INPUT
    # options. As output options they would also apply to the concatenated
    # video (whose timestamps only run 0..duration) and strip every frame.
    run(["ffmpeg", "-y", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", "concat.txt",
         "-ss", f"{start:.4f}", "-t", f"{duration:.4f}", "-i", SRC,
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-af", "loudnorm=I=-14:TP=-1.0:LRA=7",
         "-shortest", "-movflags", "+faststart", "stitched.mp4"], cwd=workdir)

    # ---- 5. captions, then head/tail fades (fades last so the text fades too)
    vf_parts = []
    if burn_captions:
        print("  [4/5] transcribing...")
        words = transcribe(os.path.join(workdir, "stitched.mp4"))
        if words:
            print(f"        {len(words)} words")
            ass_path = os.path.join(workdir, f"cap_{name}.ass")
            n = build_ass(words, ass_path, split_windows)
            print(f"        {n} caption events")
            # libass resolves fonts through fontconfig, which is unreliable on
            # this box; drop the TTF next to the script and point fontsdir at it
            # so the face resolves without a fontconfig lookup.
            font_src = "C:/Windows/Fonts/arial.ttf"
            if os.path.exists(font_src):
                shutil.copy(font_src, os.path.join(workdir, "arial.ttf"))
            vf_parts.append(f"subtitles={os.path.basename(ass_path)}:fontsdir=.")
        else:
            print("        no words — rendering without captions")
    fades = _edge_fades(duration)
    if fades:
        vf_parts.append(fades)

    if vf_parts:
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", "stitched.mp4",
             "-vf", ",".join(vf_parts),
             *encoder_args(),
             "-pix_fmt", "yuv420p", "-c:a", "copy",
             "-movflags", "+faststart", os.path.basename(final)], cwd=workdir)
        shutil.move(os.path.join(workdir, os.path.basename(final)), final)
    else:
        shutil.move(os.path.join(workdir, "stitched.mp4"), final)
    print(f"  [5/5] done -> {final}")
    return final


# ======================================================================
# CLIPS  (chosen from the whole-video face + audio scan)
# ======================================================================
# Fallback list, used only when no LLM-ranked highlights.json is present.
CLIPS = [
    {"name": "short_1", "start": 665,  "duration": 30},   # 11:05 loud close-up + two-shot
    {"name": "short_2", "start": 1685, "duration": 30},   # 28:05 two-shot heavy
    {"name": "short_3", "start": 850,  "duration": 30},   # 14:10 pure close-up
]

# Transcript-ranked highlights produced by highlights_llm.py (the approach from
# AI-Youtube-Shorts-Generator). When this exists it DRIVES clip selection: the
# moments are chosen by what is said, not only by audio level and face size.
DEFAULT_HIGHLIGHTS_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "transcript_work", "highlights.json")


def load_clips():
    """Clips from the ranked highlights file, else the built-in fallback."""
    path = os.environ.get("HIGHLIGHTS_JSON", DEFAULT_HIGHLIGHTS_JSON)
    if not os.path.exists(path):
        print(f"  no highlights file at {path} — using the built-in clip list")
        return list(CLIPS)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    clips = []
    for i, h in enumerate(data.get("highlights", []), 1):
        start = float(h["start_time"])
        end = float(h["end_time"])
        if end <= start:
            continue
        clips.append({"name": f"short_{i}", "start": start,
                      "duration": round(end - start, 2),
                      "title": h.get("title", ""), "score": h.get("score", 0),
                      "hook": h.get("hook_sentence", "")})
    print(f"  loaded {len(clips)} ranked highlight(s) from {os.path.basename(path)}")
    return clips


def _parse_args(argv):
    """Flags for a reusable run; bare words still select clip names.

    Kept as a hand-rolled parser rather than argparse so that a bare positional
    list (`shortsmith.py short_2 short_5`) keeps working exactly as before.
    """
    opts = {"input": None, "out": None, "work": None, "highlights": None, "clips": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--input", "-i"):
            i += 1; opts["input"] = argv[i]
        elif a in ("--out", "-o"):
            i += 1; opts["out"] = argv[i]
        elif a in ("--work", "-w"):
            i += 1; opts["work"] = argv[i]
        elif a == "--highlights":
            i += 1; opts["highlights"] = argv[i]
        elif a in ("--help", "-h"):
            print("usage: shortsmith.py [clipname ...] [--input VIDEO] "
                  "[--out DIR] [--work DIR] [--highlights JSON]")
            sys.exit(0)
        elif a.startswith("-"):
            raise SystemExit(f"unknown option: {a}")
        else:
            opts["clips"].append(a)
        i += 1
    return opts


def main():
    global SRC, OUT_DIR, WORK_ROOT
    opts = _parse_args(sys.argv[1:])
    if opts["input"]:
        SRC = opts["input"]
    if opts["out"]:
        OUT_DIR = opts["out"]
    if opts["work"]:
        WORK_ROOT = opts["work"]
    if opts["highlights"]:
        os.environ["HIGHLIGHTS_JSON"] = opts["highlights"]

    if not SRC:
        raise SystemExit(
            "no input video given.\n"
            "  easiest:            python make_shorts.py \"C:/path/video.mp4\"\n"
            "  or here:            --input \"C:/path/video.mp4\"\n"
            "  or environment:     SHORTSMITH_SRC=C:/path/video.mp4\n"
            '  or local_config.json:  {"source": "C:/path/video.mp4"}')
    if not os.path.exists(SRC):
        raise SystemExit(f"source video not found: {SRC}")

    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(WORK_ROOT, exist_ok=True)

    print("=" * 66)
    print("  shortsmith — face-aware vertical shorts")
    print("=" * 66)
    _load_detector()

    orig_w, orig_h, fps = probe(SRC)
    print(f"  source: {os.path.basename(SRC)}  {orig_w}x{orig_h} @ {fps:.2f}fps")
    print(f"  target: {TARGET_W}x{TARGET_H} (9:16)")
    print(f"  out:    {OUT_DIR}\n")

    clips = load_clips()
    if opts["clips"]:                          # optional: only these clip names
        wanted = set(opts["clips"])
        clips = [c for c in clips if c["name"] in wanted]

    if not clips:
        raise SystemExit("no clips to render — nothing in the highlight list "
                         "matched, and no built-in fallback applied")

    made = []
    for clip in clips:
        wd = tempfile.mkdtemp(prefix=clip["name"] + "_", dir=WORK_ROOT)
        try:
            made.append(render_clip(clip, wd, orig_w, orig_h, fps))
        except Exception as e:                        # noqa: BLE001
            print(f"  !! {clip['name']} FAILED: {e}")
        finally:
            if os.environ.get("SHORTSMITH_KEEP") == "1":
                print(f"  (workdir kept for inspection: {wd})")
            else:
                shutil.rmtree(wd, ignore_errors=True)

    print("\n" + "=" * 66)
    print("  outputs")
    for p in made:
        print(f"    {p}  ({os.path.getsize(p):,} bytes)")


if __name__ == "__main__":
    main()
