# What this clipper is good at, and what it will get wrong

Shortsmith was built and tuned on a **two-person interview**: locked-off cameras,
constant speech, faces roughly the same size all the way through. Everything below
follows from that. Read this before pointing it at something else, because two of
the failure modes below are silent — you get a bad clip, or no clip at all, with
no error.

## The short version

| Content | Verdict | The reason |
|---|---|---|
| **Podcast / interview** | **Good — this is the target** | face tracking, stacked two-shot, diarization and transcript ranking were all designed for it |
| **Talking head to camera** | **Good** | one face, steady framing |
| **Vlog — handheld, talking** | **Fair** | the face tracks, but handheld shake is not modelled; the crop is smoothed, not stabilised |
| **Vlog — scenery and B-roll** | **Poor** | no face in frame, and there is no fallback layout (see below) |
| **Vlog — music-led, little speech** | **Fails** | the moment ranker needs a transcript; with none it returns zero clips |
| **Stream — commentary / just chatting** | **Fair** | it is speech, so it works; but see the transcription cost |
| **Stream — gameplay with a corner webcam** | **Poor** | the gameplay is face-free, and the webcam face is likely rejected as a "background face" |
| **Screen recording / tutorial** | **Fails** | no faces anywhere, and usually little continuous speech |

## Why each one behaves that way

### The moment ranker needs speech

It scores a window on, in order of weight: **speech density** (0.34), **audio
energy** (0.26), **hook language** (0.24), length fit (0.10), minus a **silence**
penalty (0.16). Three of the four positive terms need a transcript.

With no transcript it does not degrade — it stops:

```python
segs = transcript.get("segments") or []
if not segs:
    return {"highlights": [], "ranker": "heuristic"}
```

**Zero clips, no error.** A music-led vlog or a quiet gameplay stream will look
like the program did nothing. If you are clipping that kind of material, the
ranker has to be replaced before anything else is worth doing.

There is also **no chunking**: the whole source is transcribed and scored in one
pass. At roughly 8x realtime, a 4-hour stream is about **30 minutes** of
transcription before the first clip appears.

### There is no layout for a frame with no face

The camera follows a detected face. When a frame has no face, `update_target` is
never called, so the camera **holds its last position** — and on a scene cut it
resets to the frame centre and **snaps** there. On face-free footage with frequent
cuts (a product-demo montage, scenery B-roll, gameplay) that reads as a crop that
keeps jumping for no reason. Measured on one such section: **71 repositionings in
34 seconds, 60 of them inside 1.5s.**

A "blurred background bed" fallback layout was designed and written up in the
development notes but **was never implemented**. If scenery footage matters to
you, that layout is the first thing to build.

### A small face is discarded on purpose

```python
MIN_FACE_WIDTH = 0.045     # smaller than this = background face
DETECT_MAX_WIDTH = 640     # detectors run on a <=640px copy
```

On a 1920-wide frame that minimum is **86px**, and detection happens on a 640px
copy, so the decision is made on a face about **29px wide**. This is deliberate —
it stops the camera locking onto a face on a poster in the background — but it
also means a **small corner webcam is treated as background** and ignored. A
stream layout puts its face exactly there.

### The reframing assumes landscape

The crop is `crop_height = source_height`, `crop_width = crop_height x 0.5625`.
It clamps if that would exceed the frame width, so an already-vertical source
works — but by accident, not by design, and there is no logic that recognises a
source which is already 9:16 and simply passes it through.

## What generalises across all of it

Worth knowing, because it means some improvements pay off whatever you clip:

| Skill | Applies to | Status |
|---|---|---|
| Pacing / visual-change density | everything | works, but tuned low |
| Captions and word emphasis | everything with speech | strong |
| Sound effects on visible actions | everything | works |
| Safe-zone placement | everything | correct |
| Motion graphics / animated text | everything | available, unused |
| Face-crop reframing | **needs a face** | works when there is one |
| Transcript moment ranking | **needs speech** | works when there is speech |

## If you want to clip vlogs and streams

In rough order of what unblocks the most:

1. **A no-face layout.** Blurred or centre-weighted bed instead of an arbitrary
   vertical slice of scenery. Until this exists, scenery footage is unusable.
2. **Moment scoring that does not need words.** Audio peaks, motion between
   frames, scene changes, sustained loudness. A gameplay highlight is an *event*,
   not a sentence.
3. **Chunked transcription** with overlap, so a multi-hour source does not cost
   half an hour before the first clip and does not lose a highlight that straddles
   a chunk boundary.
4. **A smaller minimum face size** — or a per-layout one — so a webcam inset is
   not thrown away as a background face.

The motion, graphics, caption and sound work is worth doing regardless; none of it
depends on the subject being a talking head.
