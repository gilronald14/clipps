> Current changes to analysis, project format, and export settings are documented in [Offline Studio](OFFLINE_STUDIO.md).

# Clipps workspace

## Setup and run

Manual editing/export: install Python 3.9+ and a native FFmpeg build containing
libx264 and libass; ensure `ffmpeg` and `ffprobe` are on PATH. No pip dependencies
are added for the manual editor.

```
python webapp.py
```

Open http://127.0.0.1:8765/editor. The server is local and single-user. Keep the
terminal running. Existing `Start_Shortsmith.bat` and the automatic generator at
http://127.0.0.1:8765/ remain available.

For the original automatic pipeline and optional analysis:

```
python -m pip install -r requirements.txt
python download_models.py
python doctor.py
```

Review the original setup instructions and model licenses before downloading
optional face/speaker models. Prepare Whisper weights before offline use. See [the current studio guide](OFFLINE_STUDIO.md). CPU int8
is supported by the existing provider. Automatic detection and English/Tagalog
language overrides are exposed; mixed-language accuracy is not established.
Whisper is the sole transcription provider in this increment.

## Editing workflow

1. Paste a full source path and click Import path, or choose/drop a file.
   Native paths avoid copying large recordings. Uploads stream to disk in 1 MiB
   chunks, use unique filenames, and have a 20 GiB limit. FFprobe validates
   container/streams; it cannot prove an entire source is uncorrupted.
2. Reopen saved projects from the selector. Sources remain unchanged.
3. Optionally Transcribe & suggest, selecting model, language, count and duration
   bounds. Analysis is local, segment-timed and uses existing audio/transcript
   heuristics. It does not assess visual streaming highlights or prove virality.
   Corrections cannot be overwritten by re-analysis. Create a fresh project to
   re-transcribe. A job error retains manual editing and export.
4. Select a clip. Set title, trim its first segment using sliders, or enter exact
   numeric segment times. Add, remove or reorder segments to remove pauses or
   change order. No pauses are removed automatically. Duplicate/delete are undoable.
5. Edit caption text and source timing; search filters a bounded 150-row view.
   Add captions at the source playhead. Transcript seeking intentionally seeks
   source footage, which may be outside the current edit.
6. Choose fit/full-frame or fill/crop, 9:16 / 16:9 / 1:1, crop position, audio
   gain/mute/normalization and caption size/color. Save/apply one browser-local
   caption style. Reset framing restores full-frame fit.
7. Build the optional audio waveform. Zoom and scroll, then click to seek source
   audio. It displays source RMS, not an edited-time waveform.
8. Preview, loop, seek or step by 1/30 second. Space toggles playback when no
   input/control has focus; Ctrl/Cmd+Z and Shift+Ctrl/Cmd+Z undo/redo. Browser
   frame stepping is approximate and browser codec support can prevent preview.
9. Export one/all. Each job keeps the edit revision that started it, so editing
   during export never changes the running render. Results include MP4/SRT/VTT.

## Persistence, timeline and rendering

`workspace.py` owns schema v1 project JSON, validation, atomic saves, optimistic
revision checks, caption remapping, jobs and FFmpeg rendering. `editor.html` is
plain JavaScript and CSS. `webapp.py` routes `/editor` and `/api/workspace/*`.
The old generator is preserved rather than replaced.

Projects live in `projects/<uuid>/project.json`. Source identity consists of the
absolute path, byte size and mtime in nanoseconds (not a full content hash).
Identical size/mtime is not cryptographic proof of identity. Relinking requires
matching duration, rotated dimensions and audio presence; manually verify the
replacement is the same recording. Upload copies live in `uploads/`.

Each clip stores ordered `{start,end}` intervals in source seconds, title,
aspect, fit, crop position, audio and caption style. Edited time is the cumulative
sum of earlier interval durations plus the current source offset. Repeated or
reordered intervals repeat/reorder captions through interval intersection.
Captions remain source-timed. Trimming inside a caption preserves its full text;
review and correct boundary wording. Segment timing is not fabricated word timing.
Output is CFR 30 fps, 720×1280, 1280×720 or 720×720. Cut times are seconds; the
encoder quantizes duration to output frames, with at most about one frame per
segment plus container/audio padding. Arbitrary stream-copy cut accuracy is not
claimed. Temporary parts are accurately re-encoded before concatenation.

Captions use plain-text ASS with override escaping, margins and libass wrapping.
Preview consumes the same source EDL/style, but browser fonts/wrapping, exact
frame seeking and output color can differ. Browser audio gain above 0 dB is
capped at full volume; export applies the requested gain. Normalization applies
only to export. No word highlighting, keyframes, blurred background or manual
facecam composition is implemented in the new workspace.

Saves debounce at 600 ms. Undo/redo keeps 60 edits in browser memory and does not
survive refresh; project content does. An unsaved-change unload prompt protects
pending saves. Failed saves remain unsaved and can be retried by editing again
or initiating an export. Concurrent tab edits return a conflict; reload to use
the current saved revision rather than overwriting it. Server-owned jobs/source
cannot be changed through the editing save endpoint.

Jobs persist queued/running/completed/failed/cancelled state. One workspace heavy
job runs at a time; jobs are not automatically resumed after restart. Recovery
marks interrupted jobs failed with an actionable message. Analysis checks the
starting revision before writing, so newer manual edits are not overwritten.
Worker subprocess trees are cancelled, including FFmpeg and local transcription.
Export files publish atomically after FFprobe checks, stream/duration/dimension
checks and a full FFmpeg decode. Cancelled batch exports may retain already
validated clips in their job directory, but only completed batches publish a
complete download list. Job process diagnostics are retained in `process.log`.

Intermediate parts are deleted at job completion; final exports, transcript
analysis, uploads and projects are retained. Job metadata retains 30 entries.
Disk retention is **not automatically bounded**: close the app, back up projects,
and remove only unneeded project job folders / upload copies manually. Never
remove originals unless you separately intend to. Check free disk before long
exports. Long analysis still uses the original whole-source audio decoding and
can consume substantial RAM; streaming ASR and proxies are future work.

No older durable project schema existed in the original archive. Legacy cached
transcripts/highlights are preserved in `work/`; they are not silently converted
into editable projects. Unknown workspace schema versions are refused safely.

## Privacy and provider configuration

Manual workspace processing stays local. No remote URL fetching is added.
Optional LLM ranking remains confined to the legacy automatic generator and sends
transcripts to its configured endpoint only when selected. Configure server
process environment variables shown in `.env.example`; this app does not load a
`.env` file automatically. Never place keys in HTML or commit actual values.
NVIDIA model availability and live authenticated calls were not verified here.
The existing default model string remains unchanged rather than guessing a new
available model. Invalid/non-finite/out-of-range provider intervals are rejected;
scores are editorial estimates. Retries are already bounded in the ranker.

The server rejects nonlocal Host headers and cross-origin requests. Workspace
file URLs are restricted to the project's known source/completed exports.
The legacy browse/file API intentionally retains broad local filesystem access;
this application is not a multiuser hosted service. Do not expose its port.

## Tool choices and licenses

No new runtime libraries were added. Native FFmpeg fits the existing Python
pipeline and long recordings; FFmpeg build license terms depend on its codecs.
The repository remains MIT; existing model/license credits remain in LICENSE
and CREDITS.md. Existing dependencies are unpinned; a tested lock is still needed.

Primary sources reviewed 2026-11-17:

- https://ffmpeg.org/ffmpeg.html and https://ffmpeg.org/ffmpeg-filters.html:
  native trimming, filtergraphs, captions and audio pipeline retained.
- https://github.com/SYSTRAN/faster-whisper: retained existing local transcription,
  language/VAD support; word timing not added without accuracy validation.
- https://github.com/m-bain/whisperX: alignment/diarization may help later, but
  additional model/dependency requirements do not justify adding it here.
- https://www.scenedetect.com/: useful shot boundary tool; deferred until
  evaluation against existing scene-change handling and real footage.
- https://github.com/remotion-dev/skills: targets Remotion projects; no React or
  Remotion runtime exists here. Adding a second render system is not justified.
- https://ffmpegwasm.netlify.app/docs/faq/: describes lower performance than
  native processing; no browser WASM renderer was added.

## Verification and troubleshooting

```
python -m compileall -q .
python -m unittest discover -s tests -v
python doctor.py
```

Doctor checks the original full AI pipeline; its missing-model failures do not
block the manual workspace. If imports fail, verify the path and `ffprobe` on
PATH. If burned captions fail, verify the `subtitles` filter/libass. If playback
fails but export succeeds, use a browser-compatible MP4/H.264 source for preview.
No automatic proxy transcode is currently supplied. For missing AI libraries,
install requirements in a virtual environment. For a provider outage, use manual
editing or offline analysis; never invent completed transcript results.
