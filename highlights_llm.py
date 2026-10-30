#!/usr/bin/env python3
"""Transcript-driven viral highlight detection.

Adapted from Anil-matcha/AI-Youtube-Shorts-Generator (MIT), which itself ports
ViralVadoo's transcript_analysis/highlight_generator.py. Kept deliberately
faithful to the original: the content-type probe, the ranked virality criteria,
the same system prompt, 20-minute chunking with 60 s overlap for long videos,
asking for ~2x the target so dedupe has headroom, and the >50 % overlap dedupe
rule that keeps the higher score.

What is DIFFERENT from the original:
  * The target duration is configurable (the original hardcodes a 45-90 s sweet
    spot in the prompt). Default here is the 30 s this project renders.
  * The LLM backend is pluggable and can be an OpenAI-compatible endpoint
    (NVIDIA / OpenAI / any base_url), OR "emit" mode, which writes the exact
    prompts to disk so an agent can answer them when no API key exists on the
    box. The original requires OpenAI or Gemini keys and hard-fails without them.

Usage
    python highlights_llm.py --transcript transcript.json --num-clips 3
    python highlights_llm.py --transcript transcript.json --emit-prompt prompt.txt
    python highlights_llm.py --transcript transcript.json --answer answer.json
"""
import argparse
import json
import os
import re
import sys
from typing import Callable, Dict, List, Optional

LLMFn = Callable[[str], str]


CONTENT_TYPE_PROMPT = """Analyze this video transcript sample and classify the content type.
Choose one: podcast, interview, tutorial, lecture, commentary, debate, vlog, other.
Also estimate content density: low (mostly filler/chit-chat), medium, or high (dense info/stories).
Respond with JSON only: {"content_type": "...", "density": "..."}"""


VIRALITY_CRITERIA = """
Virality signals to prioritize (ranked by impact):
1. HOOK MOMENTS — statements that create immediate curiosity ("The secret is...", "Nobody talks about...", "I was completely wrong about...")
2. EMOTIONAL PEAKS — genuine surprise, laughter, anger, vulnerability, excitement; raw unscripted reactions
3. OPINION BOMBS — strong, polarizing or counter-intuitive statements that trigger agree/disagree
4. REVELATION MOMENTS — surprising facts, stats, or confessions that reframe how the viewer thinks
5. CONFLICT/TENSION — disagreement, pushback, or a problem being confronted head-on
6. QUOTABLE ONE-LINERS — a sentence that works as a standalone quote card
7. STORY PEAKS — the climax or twist of an anecdote; the payoff moment
8. PRACTICAL VALUE — a concrete tip, hack, or insight the viewer can immediately apply
"""


# TARGET seconds is substituted in; the original leaves 45-90 hardcoded.
HIGHLIGHT_SYSTEM_PROMPT = """You are an elite short-form video editor who has studied thousands of viral clips on TikTok, Instagram Reels, and YouTube Shorts. You know exactly what makes viewers stop scrolling, watch to the end, and share.

{virality_criteria}

Content type: {content_type} | Density: {density}

Your task: identify the most viral-worthy highlights from the transcript.

Rules:
- Every highlight must open with a strong HOOK — a line that grabs attention within the first 3 seconds
- Duration sweet spot: {target_seconds} seconds. Go shorter only for a perfect standalone one-liner. Go longer only when a story arc genuinely needs the context to land
- Never cut mid-sentence or mid-thought — each clip must feel complete and self-contained
- Clips must not overlap significantly with each other
- Score 0-100 on viral potential (not general quality)
- {num_clips_instruction}
- For each highlight, identify the single best "hook_sentence" — the opening line that would make someone stop scrolling
- Explain in one sentence why this clip is viral ("virality_reason")
- Use the exact timestamps printed in the transcript brackets; the clip must START at or just before the hook line

Respond ONLY with valid JSON (no markdown, no explanation):
{{"highlights":[{{"title":"string","start_time":float,"end_time":float,"score":int,"hook_sentence":"string","virality_reason":"string"}}]}}"""


CHUNK_SIZE_SECONDS = 1200       # 20-min chunks for long videos
LONG_VIDEO_THRESHOLD = 1800     # chunk videos longer than 30 min
CHUNK_OVERLAP_SECONDS = 60
MAX_HIGHLIGHT_API_ATTEMPTS = 3
TARGET_CLIP_SECONDS = int(os.environ.get("TARGET_CLIP_SECONDS", "30"))


# ----------------------------------------------------------------------
# LLM backends
# ----------------------------------------------------------------------
def call_openai_compatible_llm(prompt: str) -> str:
    """Any OpenAI-compatible /chat/completions endpoint.

    Reads a key from (in order) HL_LLM_API_KEY, NVIDIA_API_KEY, OPENAI_API_KEY,
    and a base_url from HL_LLM_BASE_URL or OPENAI_BASE_URL.
    """
    import urllib.request

    key = (os.getenv("HL_LLM_API_KEY") or os.getenv("NVIDIA_API_KEY")
           or os.getenv("OPENAI_API_KEY") or "").strip()
    base = (os.getenv("HL_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL")
            or "https://integrate.api.nvidia.com/v1").rstrip("/")
    model = os.getenv("HL_LLM_MODEL", "deepseek-ai/deepseek-v4.1-flash").strip()
    if not key:
        raise RuntimeError("no LLM key set (HL_LLM_API_KEY / NVIDIA_API_KEY / OPENAI_API_KEY)")

    body = json.dumps({
        "model": model,
        "temperature": 0.4,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode())
    return data["choices"][0]["message"]["content"] or ""


def default_llm_fn() -> LLMFn:
    return call_openai_compatible_llm


# ----------------------------------------------------------------------
# Prompt assembly / parsing  (faithful to the original)
# ----------------------------------------------------------------------
def _parse_json_loose(raw: str) -> Dict:
    """Models sometimes wrap JSON in markdown fences — strip and parse."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end + 1])
        raise


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _sanitize_highlights(raw_highlights: object, duration: float) -> List[Dict]:
    if not isinstance(raw_highlights, list):
        return []
    max_end = duration if duration > 0 else float("inf")
    cleaned: List[Dict] = []
    for item in raw_highlights:
        if not isinstance(item, dict):
            continue
        start = _coerce_float(item.get("start_time"), default=-1.0)
        end = _coerce_float(item.get("end_time"), default=-1.0)
        if start < 0 or end <= start:
            continue
        if max_end != float("inf"):
            start = min(start, max_end)
            end = min(end, max_end)
            if end <= start:
                continue
        cleaned.append({
            "title": str(item.get("title") or "Untitled Highlight").strip(),
            "start_time": start,
            "end_time": end,
            "score": max(0, min(100, _coerce_int(item.get("score"), default=0))),
            "hook_sentence": str(item.get("hook_sentence") or "").strip(),
            "virality_reason": str(item.get("virality_reason") or "").strip(),
        })
    return cleaned


def detect_content_type(transcript: Dict, llm_fn: LLMFn) -> Dict[str, str]:
    segments = transcript.get("segments", [])
    sample = " ".join(s["text"] for s in segments[:25])[:3000]
    prompt = f"{CONTENT_TYPE_PROMPT}\n\nTranscript sample:\n{sample}"
    try:
        return _parse_json_loose(llm_fn(prompt))
    except Exception:
        return {"content_type": "other", "density": "medium"}


def build_transcript_text(transcript: Dict) -> str:
    segments = transcript.get("segments", [])
    return "\n".join(f"[{s['start']:.1f}s] {s['text'].strip()}" for s in segments)


def chunk_transcript(transcript: Dict) -> List[Dict]:
    segments = transcript.get("segments", [])
    duration = transcript.get("duration") or (segments[-1]["end"] if segments else 0)
    chunks, start = [], 0
    while start < duration:
        end = min(start + CHUNK_SIZE_SECONDS, duration)
        chunk_segs = [s for s in segments
                      if s["start"] >= start and s["end"] <= end + CHUNK_OVERLAP_SECONDS]
        if chunk_segs:
            chunk = dict(transcript)
            chunk["segments"] = chunk_segs
            chunk["duration"] = end - start
            chunk["_offset"] = start
            chunks.append(chunk)
        start += CHUNK_SIZE_SECONDS - CHUNK_OVERLAP_SECONDS
    return chunks


def build_prompt(transcript_text: str, content_info: Dict, duration: float,
                 num_clips: int, is_chunk: bool = False) -> str:
    """The exact prompt sent to the model (also what --emit-prompt writes)."""
    target = max(num_clips * 2, 5)
    natural_max = max(2 if is_chunk else 3, int(duration / (TARGET_CLIP_SECONDS * 3)))
    min_clips = min(target, natural_max, 8)
    system = HIGHLIGHT_SYSTEM_PROMPT.format(
        virality_criteria=VIRALITY_CRITERIA,
        content_type=content_info.get("content_type", "other"),
        density=content_info.get("density", "medium"),
        target_seconds=TARGET_CLIP_SECONDS,
        num_clips_instruction=f"Generate at least {min_clips} highlights",
    )
    return f"{system}\n\nTranscript:\n{transcript_text}"


def call_highlight_api(prompt: str, duration: float, llm_fn: LLMFn) -> Dict:
    last_error = "unknown"
    for attempt in range(1, MAX_HIGHLIGHT_API_ATTEMPTS + 1):
        raw = llm_fn(prompt)
        try:
            parsed = _parse_json_loose(raw)
            highlights = _sanitize_highlights(parsed.get("highlights"), duration=duration)
            if highlights:
                return {"highlights": highlights}
            last_error = "no valid highlights in response"
        except Exception as e:                                   # noqa: BLE001
            last_error = str(e)
        if attempt < MAX_HIGHLIGHT_API_ATTEMPTS:
            print(f"[highlights] invalid model output on attempt {attempt}/"
                  f"{MAX_HIGHLIGHT_API_ATTEMPTS}; retrying", flush=True)
            prompt = (prompt
                      + "\n\nIMPORTANT: Return ONLY valid JSON with a top-level 'highlights'"
                        " array. Each item must include: title, start_time, end_time, score,"
                        " hook_sentence, virality_reason. No markdown fences, no commentary.")
    raise RuntimeError(f"highlight generator produced invalid output after "
                       f"{MAX_HIGHLIGHT_API_ATTEMPTS} attempts: {last_error}")


def dedupe_highlights(highlights: List[Dict]) -> List[Dict]:
    """Drop a highlight if it overlaps >50% with a higher-scoring one kept."""
    highlights = sorted(highlights, key=lambda x: int(x.get("score", 0)), reverse=True)
    kept: List[Dict] = []
    for h in highlights:
        h_start, h_end = float(h["start_time"]), float(h["end_time"])
        h_dur = h_end - h_start
        overlapping = False
        for k in kept:
            latest_start = max(h_start, float(k["start_time"]))
            earliest_end = min(h_end, float(k["end_time"]))
            overlap = earliest_end - latest_start
            if overlap > 0 and overlap > 0.5 * h_dur:
                overlapping = True
                break
        if not overlapping:
            kept.append(h)
    return kept


def get_highlights(transcript: Dict, num_clips: int = 3,
                   llm_fn: Optional[LLMFn] = None) -> Dict:
    """Main entry point — {highlights: [...]} sorted by score."""
    llm_fn = llm_fn or default_llm_fn()
    duration = transcript.get("duration", 0)
    content_info = detect_content_type(transcript, llm_fn=llm_fn)
    print(f"[highlights] content={content_info.get('content_type')} "
          f"density={content_info.get('density')} duration={duration:.0f}s", flush=True)

    if duration >= LONG_VIDEO_THRESHOLD:
        chunks = chunk_transcript(transcript)
        print(f"[highlights] long video — splitting into {len(chunks)} chunks", flush=True)
        all_h: List[Dict] = []
        for i, chunk in enumerate(chunks):
            offset = chunk.get("_offset", 0)
            print(f"[highlights] chunk {i+1}/{len(chunks)} (offset {offset:.0f}s)", flush=True)
            p = build_prompt(build_transcript_text(chunk), content_info,
                             chunk["duration"], num_clips, is_chunk=True)
            for h in call_highlight_api(p, chunk["duration"], llm_fn).get("highlights", []):
                h["start_time"] += offset
                h["end_time"] += offset
                all_h.append(h)
        highlights = dedupe_highlights(all_h)
    else:
        p = build_prompt(build_transcript_text(transcript), content_info,
                         duration, num_clips)
        highlights = dedupe_highlights(
            call_highlight_api(p, duration, llm_fn).get("highlights", []))

    highlights.sort(key=lambda h: int(h.get("score", 0)), reverse=True)
    return {"highlights": highlights, "content_info": content_info}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _emit_prompts(transcript: Dict, num_clips: int) -> List[str]:
    """Every prompt an agent must answer, in order (content-type, then chunks)."""
    duration = transcript.get("duration", 0)
    segments = transcript.get("segments", [])
    sample = " ".join(s["text"] for s in segments[:25])[:3000]
    prompts = [f"{CONTENT_TYPE_PROMPT}\n\nTranscript sample:\n{sample}"]
    if duration >= LONG_VIDEO_THRESHOLD:
        for chunk in chunk_transcript(transcript):
            prompts.append(build_prompt(build_transcript_text(chunk),
                                        {"content_type": "podcast", "density": "medium"},
                                        chunk["duration"], num_clips, is_chunk=True))
    else:
        prompts.append(build_prompt(build_transcript_text(transcript),
                                    {"content_type": "podcast", "density": "medium"},
                                    duration, num_clips))
    return prompts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--num-clips", type=int, default=3)
    ap.add_argument("--out", default=None, help="where to write highlights JSON")
    ap.add_argument("--emit-prompt", default=None,
                    help="write the prompts for an agent to answer (no API key needed)")
    ap.add_argument("--answer", default=None,
                    help="JSON file with the agent's {highlights:[...]} answer(s)")
    args = ap.parse_args()

    with open(args.transcript, encoding="utf-8") as f:
        transcript = json.load(f)

    if args.emit_prompt:
        prompts = _emit_prompts(transcript, args.num_clips)
        with open(args.emit_prompt, "w", encoding="utf-8") as f:
            for i, p in enumerate(prompts):
                f.write(f"===== PROMPT {i} =====\n{p}\n\n")
        print(f"wrote {len(prompts)} prompt(s) -> {args.emit_prompt}")
        return

    if args.answer:
        raw = json.load(open(args.answer, encoding="utf-8"))
        items = raw.get("highlights") if isinstance(raw, dict) else raw
        clean = _sanitize_highlights(items, transcript.get("duration", 0))
        # answers may carry absolute times already; dedupe and rank
        final = dedupe_highlights(clean)[: args.num_clips]
        out = args.out or "highlights.json"
        json.dump({"highlights": final}, open(out, "w", encoding="utf-8"), indent=2)
        print(f"{len(final)} highlight(s) -> {out}")
        for h in final:
            print(f"  {h['score']:3d}  {h['start_time']:7.1f}-{h['end_time']:7.1f}s  "
                  f"({h['end_time']-h['start_time']:.0f}s)  {h['title']}")
        return

    result = get_highlights(transcript, args.num_clips)
    out = args.out or "highlights.json"
    json.dump(result, open(out, "w", encoding="utf-8"), indent=2)
    for h in result["highlights"]:
        print(f"  {h['score']:3d}  {h['start_time']:7.1f}-{h['end_time']:7.1f}s  {h['title']}")


if __name__ == "__main__":
    main()
