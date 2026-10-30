# Shortsmith — turn a long video into vertical shorts

Point it at any video and it picks the strong moments, crops them to 1080×1920
following whoever is on screen, adds an eased push plus emphasis punch-ins, and
burns word-timed captions.

Everything runs on this PC. No upload, no account, no API key.

---

## Use it

**Drop a video onto an icon.** Drag an `.mp4` / `.mkv` / `.mov` onto
`Shortsmith_Drop.bat`. A window opens, progress prints, and when it finishes the
shorts open automatically in `%USERPROFILE%\Videos\shorts\<video name>\`.

**Or use the window.** Double-click `Shortsmith_App.bat` — or the desktop icon
the setup creates — to pick:

- **Import** · `Choose…` takes any video on this PC
- **Export** · `Change…` sets where the shorts are written
- how many shorts, the length range, and the speech-accuracy model

Then press START and watch the log.

**Your choices are remembered.** The next run opens where the last one finished,
and so does drag-and-drop — which cannot ask, so it inherits the export folder
you set in the window. Each video gets its own subfolder under that folder, so
clips from different videos never mix.

All three routes are the same thing: **drag a file onto the window**, drop it on
`Shortsmith_Drop.bat`, or press `Choose…`. Dropping onto the window needs no
extra package — Tk has no built-in file drop, so the window hooks the Windows
facility directly instead of depending on the `tkinterdnd2` Tcl extension. If
that hook cannot be installed on some locked-down machine the window still opens
and works, and says so once in its log.

---

## Install on a Windows PC

`setup.bat` installs the **dependencies** of a copy you already have — it does
not download the program. To get the program, fetch the code first:

**With git (recommended, and it can update itself later):**

```bat
git clone https://github.com/YOUR-USER/YOUR-REPO.git
cd YOUR-REPO
setup.bat
```

**Without git:** on the repository page click **Code → Download ZIP**, extract
it, then double-click `setup.bat` inside the extracted folder.

**Or use the helper**, which does both halves in one go:

```bat
install.bat https://github.com/YOUR-USER/YOUR-REPO.git
```

It clones into `%USERPROFILE%\shortsmith` and hands over to `setup.bat`. Edit the
`REPO=` line near the top of `install.bat` to set your own URL permanently.

Whichever route you take, `setup.bat` finds a Python, installs the libraries,
downloads the model files, verifies the machine, and finally puts a **Shortsmith
icon on the desktop and Start menu** so it launches like any other program.
**Re-running is safe** — it skips whatever is already there, so it doubles as a
repair tool. To take the icons away again:

```
python install_shortcuts.py --remove
```

To check a machine at any time, or after something misbehaves:

```
python doctor.py
```

It prints `[ok]` / `[FAIL]` per item with the exact fix, and exits non-zero if
anything required is missing. It finishes by writing `python_path.txt` — the
interpreter that passed — which the launchers then reuse instead of guessing.

**Windows 10 and 11 are both fine.** Requirements:

| Need | Notes |
|---|---|
| Python 3.9+ | tick "Add python.exe to PATH" when installing |
| ffmpeg + ffprobe on PATH | `setup.bat` deliberately does **not** install this — it is a big third-party binary and a setup script should not fetch one silently. Any build with `libass` works; `doctor.py` verifies the filters it needs. |
| ~4 GB disk, 8 GB RAM is comfortable | no GPU required — everything runs on the CPU |

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

## What's inside

| File | Job |
|---|---|
| `make_shorts.py` | front door: probe → transcribe → rank → render |
| `shortsmith.py` | the renderer: face tracking, framing, motion, captions |
| `transcribe_source.py` | one-pass Whisper transcription, cached per video |
| `highlights_heuristic.py` | offline moment ranking |
| `highlights_llm.py` | LLM moment ranking (optional) |
| `diarize.py` | who is talking, when — CPU speaker diarization |
| `shortsmith_app.py` | the window |
| `settings.py` | remembers your import/export folders and options between runs |
| `install.bat` | fetch the program from GitHub, then set it up |
| `setup.bat` / `setup_env.py` | one-time install on a new PC |
| `doctor.py` | checks the machine and says exactly what to fix |
| `download_models.py` | fetches the model files, re-runnable |
| `install_shortcuts.py` | desktop + Start menu icon |
| `Shortsmith_Drop.bat` | drag-and-drop launcher |
| `Shortsmith_App.bat` | opens the window |
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
