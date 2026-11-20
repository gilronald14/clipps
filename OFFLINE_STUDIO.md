# Clipps offline studio

## Beginner workflow in the editing studio

The main editor at `http://127.0.0.1:8765/editor` now starts with a guided
sequence. **You do not have to use the advanced waveform, trim, framing and
caption controls to generate clips.** Expand the Advanced sections only if you
want to change an automatic decision.

1. **Import a recording** or open a previously saved project. The video/audio
   source stays local.
2. **Choose a content profile and analysis mode.** Use **Pure offline** to get
   started without AI dependencies. For speech captions, **Offline smart** needs
   compatible local dependencies and downloaded model weights.
3. Pick one of two actions:
   - **Find highlights to review:** analyzes the source and adds editable
     suggestions. Select a suggested highlight (not the initial **New clip**
     placeholder), render the exact edit preview, then export your chosen clip.
   - **Find & export clips automatically:** analyzes the source and queues a
     batch export of up to the requested number of suggested clips. This
     bypasses the review step; **watch outputs before publishing**. Existing
     suggestions from the selected profile can be reused when rescanning
     produces no additional unique results.
4. **Preview** from the center panel. **Completed clips & downloads** lists
   exported MP4 files and accompanying subtitle/metadata sidecars.
5. Select an **export platform and resolution**. Advanced editing settings,
   audio gain, layout, manual captions and trim handles remain available by
   expanding the corresponding Advanced section.

A successful export proves technical validity, **not** that a clip has a good
hook, understandable context or payoff. In Pure offline mode there is no
automatic speech transcription and highlights are based on measurable signals.
Professional editorial evaluation remains an important separate step.

### Updating a Windows ZIP installation

Close the old `Start_Shortsmith.bat` process before updating. If you downloaded
a ZIP rather than cloning with Git, back up the existing `projects` and
`uploads` folders **before replacing anything**. Either copy updated code
into the existing installation without deleting these folders, or restore both
folders to the same location afterwards. Saved project source paths are absolute,
so moving the installation or uploaded media may require using **Relink source
path**. Do not delete the original media.

### Benign browser disconnects on Windows

An occasional `ConnectionAbortedError: [WinError 10053]` may occur if a
browser cancels an idle keep-alive request while seeking or reloading. The server
ignores only expected transport disconnect exceptions; other errors are still
reported. If the editor becomes unreachable or jobs fail, inspect the actual
`Jobs` error and server logs instead of treating every disconnect as harmless.

## Windows quick start

Install Python 3.9+ and put a native FFmpeg build with ffprobe, libx264, AAC,
libass, showwaves and loudnorm on PATH. Pure offline uses Python's standard
library. This build was exercised with Python 3.14.9 and FFmpeg 9.0.1 on Windows.
From the repository folder in PowerShell:

```powershell
ffmpeg -version
ffprobe -version
python -m venv .venv
.\.venv\Scripts\python.exe webapp.py
```

Open http://127.0.0.1:8765/editor. The launcher opens this page too.
The original automatic renderer is available at the root URL.

The model-free workflow also runs unattended from PowerShell:

```powershell
python make_shorts.py "D:\Videos\stream.mp4" --pure-offline --profile gameplay --clips 3 --min 20 --max 60 --out "D:\Videos\Clips"
```

It creates an editable project and copies validated clips and sidecars into a
unique folder under the requested output directory.

1. Import a local media path or drop a file. A native path avoids a second copy.
2. Choose **Pure offline** for model-free analysis. For an interview you can
   append source-timed SRT or WebVTT captions or use **Offline smart**. The
   latter transcribes audio locally if the project has no captions.
3. Choose a profile, count and length range. Gameplay favors visual change.
   Interview and podcast favor available speech evidence. Auto uses podcast
   weights for audio-only sources, gameplay weights for silent video without
   captions, and balanced weights for other inputs. Click **Find highlights**.
   A flat silent source can yield zero.
4. Review the suggestions, their evidence, and the seconds before and after each
   cut. Adjust the ordered segments, correct the transcript, and choose fit to
   preserve screen content or fill to crop. Blur fills empty space behind the
   full frame. Safe-area guides are preview-only. If native playback is slow
   or unsupported, build a lightweight preview. Scan again after editing. Repeated
   suggestions retain the original edited clip in the project.
5. Choose square, vertical or landscape and 720p or 1080p. Export one or all.
   Vertical H.264/AAC MP4 is the shared delivery format for Shorts, TikTok
   and Reels. The Jobs section links MP4, SRT, VTT and JSON export details.
   The files live under `projects/<project-id>/<job-id>/`. Projects reopen
   from the editor. Cancel an active job in Jobs. Restarting marks abandoned
   jobs interrupted. Retry from the saved edit.

YouTube supports eligible square or vertical Shorts up to three minutes. A
Short over one minute with an active copyright claim, including a manual claim,
is blocked worldwide while the claim is active. Duration and codec validation
cannot establish rights. See [YouTube Help](https://support.google.com/youtube/answer/15424877).

### Preparing optional transcription

Use a Python environment supported by faster-whisper and CTranslate2.
On a connected machine, explicitly prepare the desired model in the same user
cache used at runtime. For example:

```powershell
python -m pip install faster-whisper numpy
python -c "from faster_whisper.utils import download_model; download_model('base')"
```

Choose that size when offline. If the model is missing, use Pure offline or
prepare the weights. Set `SHORTSMITH_DEVICE=cpu` in the environment to force
CPU. No model or dependency download was performed for the verification below.

## Processing and compatibility

FFprobe records source identity, stream presence, display rotation and dimensions.
Both workspace modes measure mono audio in one-second blocks and grayscale
32×18 frames at 1 fps. Evidence is cached per project and source identity.
Motion, audio changes and optional subtitle cues produce source-time suggestions
with a component breakdown. Bounded motion bursts get setup and recovery time.
Both paths append to the persistent EDL. A revision conflict stops the write.
Schema 1 projects read as schema 2 and persist on the next write. Future schemas
are rejected. Existing manual clips, edits, and job history survive migration.

The exporter renders from the original media. Audio-only input becomes a
waveform composition. Optional loudness processing runs on the assembled
program with a loudness measurement before the final encode. Duration, stream presence, dimensions and full decode are checked
before publishing the MP4. Older clip settings default to 720p.
Transcription processes 120-second PCM blocks and stores word offsets.
The legacy camera renderer can use these words instead of transcribing each
rendered clip again. Overlay resolution uses user artwork and cached files.
The legacy opt-in external ranker remains separate.

## Verification and remaining work

| Capability | Evidence and status |
|---|---|
| Model-free highlights and saved edits | Tested with actual generated silent moving video and a repeat scan after a manual edit. |
| Audio-only import and export | Tested with a WAV input, lightweight preview, normalized 1080×1920 waveform export, retained audio, duration check and full decode. |
| Migration, caption trims and job recovery | Regression tests cover old projects, corrected captions, reordered segments, cancellation and recovery. |
| Shorts controls, subtitle import, resolution settings | Implemented. JavaScript syntax checked. The 1080p encode passed. Full browser journey remains unverified. |
| Offline smart and legacy camera renderer | Source integrated. Actual inference, GPU fallback and speaker framing need cached models and real recordings. |
| Professional editing | Partial. Workspace supports full frame, blurred background, crop, ordered cuts, burned captions and whole-program loudness processing. Tracked layouts and effects still live in the legacy renderer. |
| Long-source performance | PCM and sampled video stream in bounded blocks. No multi-hour benchmark or proxy playback benchmark yet. |
| Release evaluation | No verified optional dependency lock, full clean-install test or human-rated interview, tutorial or gameplay benchmark. |

A controlled fixture used 30 seconds of silent 320×180 H.264 at 15 fps with
an eight-second moving segment beginning at second 12. The prior transcript
selector returns no candidates for empty transcripts by design. The first
motion-based window ended at 16.275 seconds. With burst-aware boundaries
the selected interval was 11–22 seconds. Signal extraction plus ranking took
0.282 seconds in one Windows CPU run. Sampled peak working set for Python and
its FFmpeg child was 45.3 MiB (20 ms sampling, excluding fixture generation).
This checks coverage of a known event. Human editorial preference and
speaker comprehension require evaluation on representative recordings.

Run `python -m unittest discover -s tests` with FFmpeg on PATH. The HTTP range
test requires permission to connect to a local listening socket.

On this Windows run, 19 of 20 tests passed in 7.995 seconds. The remaining
test could not connect to its loopback HTTP server (WinError 10013). The
baseline at 131519d had the same restriction, with 12 of 13 passing when
Python used UTF-8. The new CLI was also exercised against the silent event
fixture and produced an editable project and a fully decoded MP4 with sidecars.

Editorial references: [YouTube creator discussion](https://blog.youtube/creator-and-artist-stories/youtube-shorts-deep-dive/),
[TikTok Creative Codes](https://ads.tiktok.com/business/en/creative-codes).
Processing references: [FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper).
