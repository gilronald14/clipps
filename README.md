# Shortsmith — turn a long video into vertical shorts

Point it at any video and it picks the strong moments, crops them to 1080×1920
following whoever is on screen, adds an eased push plus emphasis punch-ins, and
burns word-timed captions.

Everything runs on this PC. No upload, no account, no API key.

---

## Use it

**Double-click `Start_Shortsmith.bat`.** A small console window opens and your
browser opens on `http://127.0.0.1:8765/` — that page is the whole program:

- **Import** — type or paste a path, walk the built-in folder browser (it shows
  your actual drives), or drag a video onto the page
- **Export** — pick any folder on this PC with the same browser
- **Options** — how many clips, the length range, and the speech model
- **START / Stop** — with progress streamed live as it renders
- **Your clips** — play or download each one straight from the page

Nothing leaves this PC. The server binds to `127.0.0.1` only, so it is not
reachable from the network, and it needs no internet at all once the models are
downloaded. Your choices are remembered in the browser between visits.

> A web page cannot open a native file dialog, so the folder browser asks the
> server what is on the disk and lets you walk it. That is why there is a
> built-in browser rather than your usual Windows file picker.

### Or the command line

```bat
python make_shorts.py "D:/videos/podcast.mp4"
```

That is the whole thing. It probes the video, transcribes it once, ranks the
strongest moments and renders each to 1080x1920 with face-aware framing and
burned captions.

| Flag | Meaning |
|---|---|
| `--clips 7` | how many shorts to make |
| `--min 20 --max 45` | the length range each clip is cut to |
| `--out DIR` | where the shorts are written (default `~/Videos/shorts`) |
| `--whisper base` | speech accuracy: `base` / `small` / `medium` |
| `--open` | open the output folder when it finishes |
| `--reuse` | reuse the cached highlights instead of re-ranking |
| `--llm` | try the LLM ranker first (needs an endpoint and a key) |

Each video gets its own subfolder under `--out`, so clips from different videos
never mix.

> A local **web UI** is planned, which will drive this same engine from a
> browser. Nothing about the pipeline changes; only the front end does.

---

## Install on a Windows PC

Three steps. That is the whole install.

1. **Download** — on the repository page click **Code → Download ZIP**, then
   extract it anywhere you like.
2. **`setup.bat`** — double-click it. Once. It finds a Python, installs the
   libraries, downloads the 36 MB of model files and checks the machine.
3. **`Start_Shortsmith.bat`** — double-click it whenever you want to use the
   app. Your browser opens on it.

Re-running `setup.bat` is safe: it skips whatever is already there, so it
doubles as a repair tool.

If you would rather use git, `git clone` the repository and carry on from step 2.

To check a machine at any time, or after something misbehaves:

```
python doctor.py
```

It prints `[ok]` / `[FAIL]` per item with the exact fix, and exits non-zero if
anything required is missing.

**Windows 10 and 11 are both fine.** Requirements:

| Need | Notes |
|---|---|
| Python 3.9+ | tick "Add python.exe to PATH" when installing |
| ffmpeg + ffprobe on PATH | `setup.bat` deliberately does **not** install this — it is a big third-party binary and a setup script should not fetch one silently. Any build with `libass` works; `doctor.py` verifies the filters it needs. |
| ~4 GB disk, 8 GB RAM is comfortable | no GPU needed — it runs on the CPU |

**A graphics card is optional, not required.** Nothing here needs one, and that
is deliberate: the tool should run on whatever machine someone has. But it is
not wasted either —

* **Transcription** uses CUDA automatically when a usable GPU is present
  (fp16 instead of int8 on the CPU, several times faster). If the driver or
  cuDNN is missing it falls back to the CPU rather than failing.
* **Encoding** stays on `libx264` by default, because it gives the best quality
  per bit and that matters when a 480p source is being scaled to 1080×1920.
  If you would rather trade a little softness for a lot of speed, set
  `SHORTSMITH_ENCODER` to `nvenc`, `qsv`, `amf` or `videotoolbox`.

`python doctor.py` reports what the machine it runs on can actually use.

The folder is self-contained apart from ffmpeg and Python, so it can live on a
USB stick or any local drive.

## Command line

```
python make_shorts.py "D:/videos/podcast.mp4"
python make_shorts.py "D:/videos/podcast.mp4" --clips 10 --min 15 --max 40
python make_shorts.py "D:/videos/podcast.mp4" --whisper small --open
```

| Flag | Meaning |
|---|---|
| `--clips N` | how many shorts to make (default 7) |
| `--min` / `--max` | clip length bounds in seconds (default 20 / 45) |
| `--whisper` | `base` (fast) · `small` · `medium` (slower, mis-hears less) |
| `--out DIR` | where shorts go (default `~/Videos/shorts/<video name>`) |
| `--llm` | try the LLM moment-ranker first (needs an endpoint + key) |
| `--reuse` | keep the existing `highlights.json` instead of re-ranking |
| `--open` | open the output folder when done |

Re-running is cheap: the transcript and the chosen moments are cached per video
under `work/<video name>/`. Delete that folder to start clean.

---

## How it picks the moments

By default, **offline** — no model, no network, so it always works:

* **speech density** — talking beats dead air
* **audio energy** — a raised voice is usually a raised point
* **hook language** — questions, absolutes, numbers, second person
* **standalone-ness** — opens at a sentence start, closes at a sentence end
* **pauses inside** the window cost it

Boundaries snap to sentence ends, so a clip never starts mid-thought, and
overlapping candidates are suppressed so you don't get five near-copies.

This is a *proxy*. It cannot tell a good point from a loud one. If you ever give
it an OpenAI-compatible endpoint (set `HL_LLM_API_KEY` and optionally
`HL_LLM_BASE_URL` / `HL_LLM_MODEL`, then pass `--llm`), it uses the
transcript-reading ranker in `highlights_llm.py`, which is better and falls back
to the offline ranking automatically if the call fails.

---

## Following the right person

The framing normally follows whoever is largest and most central in frame. That
is wrong when two people are on screen and only one is talking, so the pipeline
also runs **speaker diarization** (sherpa-onnx + pyannote, on the CPU) and
switches the crop to the *other* face whenever the speaking turn changes.

It deliberately does not try to work out *which* face belongs to which voice — a
diarizer numbers voices, not pixels, and matching the two reliably needs a model
this machine cannot run. Instead it relies on turns **alternating**, which is
what a conversation does.

Two honest notes:

* It only acts when **two faces are on screen**. In a close-up there is one face
  and nothing to disambiguate, so it does nothing.
* On the seven clips here it changed **nothing at all** — because the source is
  already well edited and cuts to whoever is speaking, so turn changes never
  coincide with a two-shot. It is insurance for sources where the camera *does
  not* cut for you: a locked-off or webcam two-person podcast, which is exactly
  where "follow the loudest face" goes wrong. Checked on a stretch of this same
  source where a turn change does land inside a two-shot, it moved the crop by
  up to 366 px.

Cost is one CPU diarization pass per clip (a second or two). Disable with
`SHORTSMITH_NO_DIARIZE=1` if you ever want to.

## Things that appear on screen when they're named

When the speaker says something you can point at, a small picture appears in the
lower-left: the brand's mark, or a badge carrying a figure they said out loud.
It fades in, holds about 2.5 seconds, and leaves.

It only ever shows things that are **actually there**. A brand is shown because
the speaker named it, and a badge shows the words they used. Nothing is invented,
nothing is inferred, and a clip where nothing nameable is said gets no overlay at
all — that is the correct result, not a failure.

**Where the pictures come from**, in order:

1. `assets/overlays/<name>.png` — **your own picture wins.** Drop a file in
   named after the trigger and it is used exactly as-is, colours and all.
2. Simple Icons (**CC0, public domain**) — brand marks, each fetched with the
   brand's official colour so the badge themes itself.
3. Lucide (**ISC**) — outline icons for ideas with no brand: a clock for
   urgency, a moon for overnight.
4. Drawn on the spot with Pillow — a number badge, or a colour emoji from the
   Windows emoji font. These need no download, so the feature works offline.

Nothing is bundled with the program: icons are fetched once and cached in
`assets/icons/`, which is not committed.

**To add your own triggers**, edit `assets/overlays/keywords.json`:

```json
[ { "match": "robot",    "asset": "bot" },
  { "match": "a deadline", "asset": "clock" } ]
```

`match` is the word or phrase to listen for; `asset` is the picture — a brand
name, a Lucide icon name, or the name of a file you dropped in the folder.

Turn it off completely with `SHORTSMITH_OVERLAYS=0`, or adjust
`SHORTSMITH_OVERLAY_SEC` (hold time, default `2.5`), `SHORTSMITH_OVERLAY_GAP`
(least seconds between two, default `6`), `SHORTSMITH_OVERLAY_MAX` (most in one
clip, default `6`).

The pictures are composited **before** the captions are burned, so the captions
always sit on top and are never hidden.

---

## Movement that settles, and a sound on every action

Two things were added after studying
[lemo-opuscar](https://github.com/lemomo-ai/lemo-opuscar) (MIT), a library of
films built entirely in code. Its rule is worth the whole exercise: **every
visible action has a sound**, and a move should *settle* rather than stop.

**A spring, not a ramp.** A graphic arriving used to grow at a constant rate and
then stop dead. It now uses a damped spring: it overshoots to about 1.06, comes
back under 1, and rings down onto its final size. Measured through a real
entrance, the graphic goes 158 px → 272 px (overshoot) → 253 px (rebound) → 256
px (settled).

The damping is deliberately *not* the source's. A spring approaches its target
and never arrives, and an entrance holds still once it is over: at the original
damping the graphic was still 4% oversized when the animation ended, so it
would sit wrong for the rest of its life.

**A sound on every action.** A thump where a punch-in lands, a pop where a
graphic arrives. Both are synthesised from scratch in numpy — no sound files, no
downloads, nothing new to install. They are quiet on purpose: the speech is the
film. Measured against the same clip rendered without them, the finished audio
is **−14.7 LUFS either way**, with no clipping, and every effect lands within a
frame of the action it belongs to.

`easing.py` also carries the interpolation the camera would want if it ever gets
a keyframed path: monotone cubic (Fritsch–Carlson), which is smooth through
every keyframe but cannot overshoot between them. A plain cubic spline can, and
on a camera that means drifting somewhere you never asked it to go — measured on
a single-spike key set, Catmull-Rom dips to **−0.074**, i.e. a negative zoom.

Turn the sound off with `SHORTSMITH_SFX=0`; adjust it with
`SHORTSMITH_SFX_PUNCH` and `SHORTSMITH_SFX_POP` (both are linear gains).

---

## What's inside

| File | Job |
|---|---|
| `make_shorts.py` | front door: probe → transcribe → rank → render |
| `shortsmith.py` | the renderer: face tracking, framing, motion, captions |
| `transcribe_source.py` | one-pass Whisper transcription, cached per video |
| `highlights_heuristic.py` | offline moment ranking |
| `highlights_llm.py` | LLM moment ranking (optional) |
| `diarize.py` | who is talking, when — CPU speaker diarization |
| `overlays.py` | puts a picture on screen for a name the speaker says |
| `overlay_assets.py` | finds or draws that picture (user file → cache → free icon sets) |
| `svg_raster.py` | turns a fetched SVG icon into a PNG, with no SVG library |
| `easing.py` | the motion shapes: spring settles, overshoot, smooth keyframes |
| `sfx.py` | the sound on each visible action, synthesised in numpy |
| `assets/overlays/keywords.json` | your own trigger words — edit this |
| `webapp.py` | the local web server (stdlib only — nothing to install) |
| `webui.html` | the page it serves: import, export, options, live progress |
| `Start_Shortsmith.bat` | starts the server and opens your browser |
| `setup.bat` / `setup_env.py` | one-time install on a new PC |
| `doctor.py` | checks the machine and says exactly what to fix |
| `download_models.py` | fetches the model files, re-runnable |
| `tools/` | maintainer diagnostics, not needed to run |

Everything the program needs is in this folder. `setup.bat` is the only file you
have to run.

---

## Requirements

Already satisfied on this machine; listed for reference.

* **ffmpeg + ffprobe** on PATH
* **Python 3** with `mediapipe`, `faster-whisper`, `opencv-python`, `pillow`,
  `numpy`

The launchers *test* for those libraries rather than trusting the first
`python` on PATH — a Windows Store `python.exe` stub shadows real installs in a
normal shell. If none is found they say so and print the `pip install` line.

---

## Speed, honestly

Roughly **3 minutes per short** on this machine, plus transcription. A 47-minute
source with 7 shorts took about 22 minutes end to end. Most of the time is not
transcription — each clip re-loads Whisper to time its own captions, on top of
face tracking, the segment renders and the concat.

Expect roughly: transcribe ≈ duration ÷ 8, render ≈ 3 min × number of shorts.

---

## Things that will look odd, and why

* **Soft faces.** A 480p source filling 1080×1920 is a ~4× upscale. That is the
  source, not the pipeline — no amount of re-encoding adds detail that was never
  recorded.
* **A wrong word in a caption.** That is Whisper mis-hearing, not a rendering
  fault. `--whisper small` reduces it and costs transcription time.
* **A silent person stacked in frame.** The two-speaker (stacked) layout is
  chosen by who is *on screen*, not who is talking; telling a listener from a
  speaker needs an active-speaker model, which this does not have yet.
* **A drifting crop over footage with no face in it.** During cutaway or
  immersive B-roll the tracker has nothing to lock onto, so it repositions
  constantly. Measured on a product-demo section: 71 repositionings, 60 of them
  inside 1.5 s. The movement is small, but it is the one place the framing is
  not deliberate.
* **One brand shows no mark.** Five names people say constantly — ChatGPT, GPT,
  Alphabet, iPhone, AWS — are not icon names anywhere, so they are mapped to the
  company that owns them (OpenAI, Google, Apple, Amazon). If a name is neither a
  brand nor in your keyword list, it simply gets no picture.

---

## Credits

`easing.py` and `sfx.py` are adapted from [lemo-opuscar](https://github.com/lemomo-ai/lemo-opuscar) (MIT, LemoLab), and several editing rules came from [i-hate-editing](https://github.com/ranahaani/i-hate-editing) (MIT). Fetched icons are CC0 (Simple Icons) and ISC (Lucide). Full list, with licences: [CREDITS.md](CREDITS.md).
