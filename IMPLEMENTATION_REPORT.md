# Clipps implementation report · 2026-11-17

## Original application audit

Inspected the supplied `clipps-main.zip` (archive commit marker
`ebe884abc0648c79c7595144c6ce3c832ac2fe4e`). No repository instruction file,
git checkout, lockfile, existing unit-test directory or application build step
was present. The original ZIP remains the comparison baseline; original source
is preserved in the earlier uploaded archive version.

- `webapp.py`: stdlib ThreadingHTTPServer, local filesystem browser, raw streaming
  uploads, singleton in-memory render Job and subprocess log stream.
- `webui.html`: plain HTML/CSS/JS, browser-local settings, results preview/download.
  No timeline or durable editable project model was present.
- `make_shorts.py`: FFprobe → cached transcription → heuristic/optional LLM
  ranking → sentence/silence boundary refinement → renderer subprocess.
- `transcribe_source.py`: faster-whisper local ASR, existing CPU/GPU selection,
  English default, segment timestamps. Cache previously checked model only.
- `shortsmith.py`: native FFmpeg render/filtergraph, OpenCV/MediaPipe tracking,
  split layouts, captions, optional overlays, sound effects and diarization.
- `highlights_llm.py`: configurable OpenAI-compatible service, default NVIDIA
  endpoint/model, long transcript chunks, retries and deduplication. The original
  interval sanitizer clamped out-of-range output and admitted non-finite values.
- `requirements.txt`: numpy, opencv-python, mediapipe, pillow, faster-whisper,
  sherpa-onnx; no newly introduced runtime framework.

Confirmed reliability defects by inspection: repeated upload names overwrite;
truncated bodies could be reported successful; filename-only work directories
collide; transcript cache omits source/language identity; no durable job state;
invalid range requests clamp to misleading successful ranges; POSIX stop only
terminated the parent subprocess in the fallback path.

Baseline `python -m compileall -q .`: passed.
Baseline `python doctor.py`: exit 1, four required failures: cv2, mediapipe,
faster_whisper and BlazeFace model missing. FFmpeg/ffprobe/libass available.
These pre-existing environment blockers were not introduced by this update.

## Implemented

Added an actual local manual editing workspace at `/editor`, linked from the
existing generator. It covers path/upload import → optional analysis → review →
manual edit → preview → real single/batch export. Original automatic generator
and Windows launch flow remain available.

Persistent versioned JSON with atomic writes and revision-conflict checks;
autosave; bounded browser undo/redo; source relinking; ordered EDL segments;
numerical/slider trims; segment reordering, duplicate/delete; transcript search,
source seeking, editable text/timing and manual captions; fit/fill and crop
position; three output aspects; gain/mute/export normalization; caption size/color
and saved browser preset; source waveform with zoom and seeking; keyboard
controls; job progress/state/cancellation and interrupted-job recovery.

Exports remap captions through the same EDL used by playback, burn escaped ASS
captions, write SRT/VTT, validate expected video/audio/duration/dimensions, fully
decode before publishing, and atomically publish final MP4s. Manual exports need
no credentials or AI models. Source originals are never modified.

Hardened uploads with unique names, FFprobe validation, 20 GiB limits, partial
cleanup and atomic publish. Corrected suffix/unsatisfiable byte ranges. Added
local Host/Origin restrictions and workspace-scoped download access. Improved
POSIX process-tree cancellation. Added cache identity and language metadata,
strict LLM interval validation and a higher-priority transcript-data instruction.
No live API key or new external service was used.

## Important changed files

- New `workspace.py`: model, validation, persistence, analysis/export jobs, render.
- New `editor.html`: interactive editing UI.
- Updated `webapp.py`: workspace routes and HTTP/import/job fixes.
- Updated `transcribe_source.py`: cache identity and provider/language metadata.
- Updated `make_shorts.py`: source-identity cache directory names.
- Updated `highlights_llm.py`: finite/bounded intervals and request instructions.
- Updated `webui.html`: entry link to workspace.
- New `tests/test_workspace.py`, `WORKSPACE.md`, `.env.example`; updated README.

## Verification evidence

Run from the extracted project root:

```
python -m compileall -q .
python -m unittest discover -s tests -v
node -e "const fs=require('fs');new Function(fs.readFileSync('editor.html','utf8').split('<script>')[1].split('</script>')[0]);console.log('Editor JavaScript parses')"
ffprobe -v error -show_entries stream=codec_name,codec_type,width,height -show_entries format=duration,size -of json verification/smoke-edited.mp4
```

13 tests passed, covering invalid trims, repeated/reordered/removed segment
caption remapping, save/reopen/conflict/schema refusal, interrupted jobs,
changed sources, corrupt import, HTTP upload/content validation and byte ranges,
Host rejection, cancellation, malformed provider timestamps, duplicate suggestions,
actual waveform decoding, actual captioned crop/EDL export and no-audio portrait
export. All original/new Python files compile; editor JavaScript parses.

`verification/smoke-edited.mp4` is a real synthetic technical export:
H.264 720×720, AAC audio, duration 2.033008 seconds, 356760 bytes. It was built
from two reordered one-second intervals, burned captions and crop settings,
then validated by FFprobe and a full FFmpeg decode. The second export test
verified portrait fit output with no audio. These fixtures do not establish
real-world ASR, editorial quality, interview tracking or stream framing.

Browser verification attempted: Playwright package present, bundled Chromium
missing. Installed Chromium launch failed with `socket() failed: Operation not
permitted`. The remote browser could not reach the local server
(`ERR_CONNECTION_REFUSED`). Consequently there is no claimed visual review,
interaction pass or browser screenshot. HTTP integration and JS parsing passed.
Windows execution and representative real-footage cases remain unverified.

## Limits and remaining work

This is a working foundation increment, not completion of every requested
advanced feature. Real AI transcription, live NVIDIA calls and original face/
speaker renderer were not run because dependencies/models/credentials are
unavailable. No representative interview/stream footage was provided. No claim
of English/Tagalog/mixed-language accuracy is made. Browser interaction is blocked
in this environment, so the UI needs a local browser acceptance pass.

Not implemented: proxies/thumbnails, robust VFR preview, word alignment/highlights,
framing keyframes/zoom, blurred backgrounds, manual facecam composition, speaker
correction UI, fade/noise-reduction controls, automatic bounded disk retention,
remote transcription providers, old cache-to-project migration and dependency
locking. Waveform is source-time RMS, not an edited waveform. Legacy automatic
render completion is still less strict than the new manual export validation.
The old audio/ASR code still buffers long decoded recordings in memory.
Unknown project schema versions are rejected rather than guessed/migrated.

## Prioritized next steps

1. Run the interface on the target Windows PC; check actual import/edit/reopen/
   cancel/export behavior, browser codecs and visual preview/export consistency.
2. Install the existing AI dependencies and evaluate real English, Tagalog,
   mixed-language, interview, screen-recording and VFR fixtures. Verify provider
   models from the user's account before changing endpoint/model configuration.
3. Add cancellable preview proxies, bounded streaming ASR/audio feature extraction,
   storage retention and legacy export validation using this tested project model.
4. Add measured word timing, keyframes and content-specific composition only after
   the core workflow passes real-media acceptance testing.
