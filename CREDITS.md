# Credits

## Code adapted from other projects

### lemo-opuscar — https://github.com/lemomo-ai/lemo-opuscar

MIT License, Copyright (c) 2026 LemoLab.

Two files here are adapted from it:

- **`easing.py`** from `core/lib.js`. The easing set and, most importantly, the
  damped spring and the Fritsch-Carlson monotone interpolator. Reimplemented in
  Python; the shapes are theirs.
- **`sfx.py`** from `core/audio/sfx.py`. The sound designs — the swept band for
  the whoosh, the pitch-dropping sine for the thump, the resonance stacks, the
  compressor and the equal-power mixer. Their version needs scipy for its
  Butterworth filters and soundfile to write; this one implements a biquad
  directly and writes with the standard library, so nothing extra has to be
  installed. The sounds themselves are theirs.

Their demo films are under CC BY 4.0. **No demo film, still, or style asset from
that project is included here** — only the two code ideas above, which is what
the MIT licence on the code covers.

### i-hate-editing — https://github.com/ranahaani/i-hate-editing

MIT License. Several editing rules came from its `rules/` documents and are
credited where they are applied in the source:

- cut edges anchored on **silence** rather than on ASR word onsets (HARD-RULES 18)
- captions held clear of the platform's own caption band
- "show the real thing, never fabricate", and "when there is no B-roll, change
  the framing of the face instead" — the rules behind the on-screen graphics

## Assets fetched at run time

Not bundled, and each is either public domain or permissively licensed:

| Source | Licence | Used for |
|---|---|---|
| [Simple Icons](https://simpleicons.org) | CC0 1.0 (public domain) | brand marks, and each brand's official colour |
| [Lucide](https://lucide.dev) | ISC | outline icons for ideas with no brand |

Icons are downloaded once and cached in `assets/icons/`, which is not committed.

## Model weights

Not bundled. `setup.bat` fetches them from their own distribution points:

| Model | Used for |
|---|---|
| MediaPipe BlazeFace (full-range) | face detection |
| OpenCV YuNet | face detection fallback |
| sherpa-onnx pyannote segmentation + CAM++ embedder | speaker diarization |

Each keeps its own licence; see the project each is downloaded from.
