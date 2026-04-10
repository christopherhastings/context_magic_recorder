"""
summarizer.py
LMStudio-powered meeting notes and living context document.

Lineage: transcript (.json) → notes ({stem}_notes.md) → context.md

LMStudio exposes an OpenAI-compatible API at http://localhost:1234/v1
No API key required for local models.

.env variables:
  LMSTUDIO_URL    Base URL of LMStudio server (default: http://localhost:1234/v1)
  LMSTUDIO_MODEL  Model name — LMStudio ignores this for the active model,
                  but some multi-model setups use it (default: local-model)
"""

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

LMSTUDIO_URL   = os.getenv("LMSTUDIO_URL",   "http://localhost:1234/v1")
VIEWER_URL     = "http://localhost:8766"

# lms CLI — installed by LMStudio
_LMS = Path.home() / ".lmstudio/bin/lms"

# Preferred model patterns, checked in order against model IDs (case-insensitive).
# First match among loaded or available models wins.
_PREFERRED_PATTERNS = [
    "gemma-4-26b",
    "gemma-4-31b",
    "gemma-3-27b",
    "gemma-3-12b",
    "mistral-small",
    "qwen3-30b",
    "gemma-3-4b",
]

_LMSTUDIO_BASE = LMSTUDIO_URL.rstrip("/").removesuffix("/v1")


# ── LMStudio server + model management ───────────────────────────────────────

def _v0_models() -> list[dict]:
    """Return all models from LMStudio v0 API (includes state, context info)."""
    try:
        resp = requests.get(f"{_LMSTUDIO_BASE}/api/v0/models", timeout=5)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception:
        return []


def _loaded_llm() -> dict | None:
    """Return the first loaded LLM model dict, or None."""
    for m in _v0_models():
        if m.get("state") == "loaded" and m.get("type") == "llm":
            return m
    return None


def _pick_model(models: list[dict]) -> str | None:
    """Pick the best model ID from a list using preference order."""
    for pattern in _PREFERRED_PATTERNS:
        for m in models:
            mid = (m.get("id") or m.get("modelKey") or "").lower()
            if pattern in mid:
                return m.get("id") or m.get("modelKey")
    # Fallback: first non-embedding model
    for m in models:
        if m.get("type") not in ("embedding", "vlm"):
            return m.get("id") or m.get("modelKey")
    return None


def _ensure_lmstudio() -> bool:
    """
    Ensure LMStudio server is running and an LLM is loaded.
    Uses lms CLI to start the server or load a model if needed.
    Returns True when ready, False if setup failed.
    """
    # 1. Is server up?
    server_up = False
    try:
        requests.get(f"{LMSTUDIO_URL}/models", timeout=3).raise_for_status()
        server_up = True
    except Exception:
        pass

    if not server_up:
        if not _LMS.exists():
            logger.error("LMStudio server not running and lms CLI not found at %s", _LMS)
            return False
        logger.info("LMStudio server not running — starting via lms server start...")
        subprocess.run([str(_LMS), "server", "start"], capture_output=True)
        for _ in range(20):
            time.sleep(1)
            try:
                requests.get(f"{LMSTUDIO_URL}/models", timeout=2).raise_for_status()
                server_up = True
                break
            except Exception:
                pass
        if not server_up:
            logger.error("LMStudio server failed to start")
            return False
        logger.info("LMStudio server started")

    # 2. Is an LLM loaded?
    if _loaded_llm():
        return True

    # 3. Load a preferred model
    if not _LMS.exists():
        logger.error("No model loaded and lms CLI not found — cannot auto-load")
        return False

    all_models = _v0_models()
    model_id = _pick_model([m for m in all_models if m.get("type") == "llm"])
    if not model_id:
        logger.error("No suitable LLM models found in LMStudio library")
        return False

    logger.info("No model loaded — loading %s...", model_id)
    result = subprocess.run(
        [str(_LMS), "load", model_id, "-y"],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        logger.error("lms load failed: %s", result.stderr.strip())
        return False

    logger.info("Model loaded: %s", model_id)
    return True


def _get_context_limit() -> int:
    """Return the loaded model's context window size."""
    m = _loaded_llm()
    if m:
        ctx = m.get("loaded_context_length") or m.get("max_context_length")
        if ctx:
            return int(ctx)
    return 4096


def _truncate_transcript(text: str, max_chars: int) -> str:
    """
    Keep beginning and end of transcript when it exceeds budget.
    Meetings have important context at the start (agenda, intros) and
    end (decisions, action items), so we preserve both and drop the middle.
    """
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    head = text[:half]
    tail = text[-half:]
    dropped_lines = text[half:-half].count("\n")
    return head + f"\n\n[... {dropped_lines} lines omitted for context limit ...]\n\n" + tail


def _call_lm(prompt: str) -> str:
    if not _ensure_lmstudio():
        raise RuntimeError("LMStudio is not available and could not be started")

    # Use whatever model is loaded — detect its ID dynamically
    loaded = _loaded_llm()
    model_id = loaded["id"] if loaded else "local-model"

    # Estimate tokens (rough: 4 chars per token). Reserve 800 tokens for output.
    ctx_limit = _get_context_limit()
    max_prompt_chars = max(1000, (ctx_limit - 800) * 4)

    if len(prompt) > max_prompt_chars:
        logger.warning(
            f"Prompt too long ({len(prompt)} chars) for context {ctx_limit}. Truncating transcript."
        )
        # The transcript block starts after "TRANSCRIPT:\n" — truncate only that part
        marker = "\nTRANSCRIPT:\n"
        idx = prompt.find(marker)
        if idx != -1:
            pre = prompt[:idx + len(marker)]
            post_marker = "\n\nProduce notes"
            post_idx = prompt.find(post_marker)
            body = prompt[idx + len(marker): post_idx if post_idx != -1 else None]
            post = prompt[post_idx:] if post_idx != -1 else ""
            budget = max_prompt_chars - len(pre) - len(post)
            prompt = pre + _truncate_transcript(body, budget) + post

    resp = requests.post(
        f"{LMSTUDIO_URL}/chat/completions",
        json={
            "model":       model_id,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens":  min(4096, ctx_limit // 2),
        },
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_transcript(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        m, s = divmod(int(t.get("start", 0)), 60)
        lines.append(f"[{m:02d}:{s:02d}] {t['speaker']}: {t['text'].strip()}")
    return "\n".join(lines)


def _fmt_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%B %d, %Y")
    except Exception:
        return iso


# ── Speaker name resolution ───────────────────────────────────────────────────

def resolve_speaker_names(turns: list[dict]) -> dict[str, str]:
    """
    Use LMStudio/Gemma 3 to infer real names for SPEAKER_XX labels.
    Samples the first few turns per speaker and asks the model to identify
    names from context clues ("Hi I'm Robbie", "Thanks Kayla", etc.).
    Returns {SPEAKER_00: "Robbie", SPEAKER_01: "Kayla"} — omits unknowns.
    """
    unknown_speakers = [
        spk for spk in sorted(set(t["speaker"] for t in turns))
        if spk.startswith("SPEAKER_")
    ]
    if not unknown_speakers:
        return {}

    # Collect up to 3 turns per speaker; cap at 8 speakers to stay within context
    unknown_speakers = unknown_speakers[:8]
    samples: dict[str, list[str]] = {spk: [] for spk in unknown_speakers}
    for t in turns:
        spk = t["speaker"]
        if spk in samples and len(samples[spk]) < 3:
            m, s = divmod(int(t.get("start", 0)), 60)
            samples[spk].append(f'[{m:02d}:{s:02d}] "{t["text"].strip()[:120]}"')

    sample_text = "\n\n".join(
        f"{spk}:\n" + "\n".join(lines)
        for spk, lines in samples.items() if lines
    )

    prompt = f"""You are analyzing a meeting transcript to identify speakers by name.

Below are sample quotes from each unknown speaker label. A speaker's name may appear when someone addresses them ("Thanks Robbie"), when they introduce themselves ("I'm Sarah"), or from other context clues.

{sample_text}

Respond with ONLY a JSON object mapping speaker labels to real first names. Omit any speaker whose name cannot be determined with confidence.
Example: {{"SPEAKER_00": "Robbie", "SPEAKER_01": "Sarah"}}
If no names can be determined: {{}}"""

    try:
        content = _call_lm(prompt)
        match = re.search(r'\{[^}]*\}', content, re.DOTALL)
        if match:
            mapping = json.loads(match.group())
            # Validate: only keep entries that map SPEAKER_XX to non-empty strings
            valid = {k: v for k, v in mapping.items()
                     if k.startswith("SPEAKER_") and isinstance(v, str) and v.strip()}
            if valid:
                logger.info(f"Speaker names resolved: {valid}")
            return valid
    except Exception as e:
        logger.debug(f"Speaker name resolution failed: {e}")
    return {}


def apply_speaker_names(turns: list[dict], name_map: dict[str, str]) -> list[dict]:
    """Apply SPEAKER_XX → real name mapping to turns in place."""
    if not name_map:
        return turns
    for t in turns:
        if t["speaker"] in name_map:
            t["speaker"] = name_map[t["speaker"]]
    return turns


# ── Per-recording notes ───────────────────────────────────────────────────────

def summarize_recording(json_path: Path) -> Path:
    """
    Generate structured notes for one recording.
    Writes {stem}_notes.md alongside the transcript JSON.
    Returns the notes path.
    """
    data        = json.loads(json_path.read_text(encoding="utf-8"))
    meeting     = data.get("meeting", {})
    turns       = data.get("transcript", {}).get("turns", [])
    speakers    = data.get("speakers", [])

    topic    = meeting.get("topic", json_path.stem)
    date_str = _fmt_date(meeting.get("start_time") or meeting.get("local_joined", ""))

    # Resolve SPEAKER_XX → real names before summarizing
    if any(t.get("speaker", "").startswith("SPEAKER_") for t in turns):
        logger.info("Resolving speaker names via LLM...")
        name_map = resolve_speaker_names(turns)
        if name_map:
            turns = apply_speaker_names([dict(t) for t in turns], name_map)
            # Also update speakers list
            speakers = sorted(set(t["speaker"] for t in turns))

    participants = ", ".join(
        p.get("name", "") for p in meeting.get("participants", [])
    ) or ", ".join(speakers) or "unknown"

    transcript_text = _fmt_transcript(turns)
    if not transcript_text.strip():
        raise ValueError("Transcript is empty — cannot summarize")

    prompt = f"""You are a meticulous meeting analyst. Analyze this transcript and produce structured notes.

Meeting: {topic}
Date: {date_str}
Participants: {participants}

TRANSCRIPT:
{transcript_text}

Produce notes in exactly this markdown format. Be specific and concrete — pull real names, numbers, and terms from the transcript:

## Summary
2–4 sentences: what this meeting was about and what was accomplished.

## Decisions Made
Bullet list of concrete decisions reached. If none, write "- None recorded."

## Action Items
- [ ] Person (if known): specific task described

## Technical Details
List every technical specific mentioned: field names, parameter names, table names, file paths, API endpoints, system names, configuration values, processes. Format each as:
- **name**: brief explanation of what it is or why it matters

## Key Points
3–6 bullets — the most important things to remember from this meeting.
"""

    logger.info(f"Summarizing {json_path.name} via LMStudio...")
    content = _call_lm(prompt)

    notes_path = json_path.with_name(json_path.stem + "_notes.md")
    header = f"# Notes: {topic}\n"
    if date_str:
        header += f"_{date_str}_\n"
    header += f"\n[← View transcript]({VIEWER_URL}/?id={json_path.stem})\n\n"

    notes_path.write_text(header + content + "\n", encoding="utf-8")
    logger.info(f"Notes written: {notes_path.name}")
    return notes_path


# ── Global context document ───────────────────────────────────────────────────

def update_context(output_dir: Path) -> Path:
    """
    Regenerate context.md from all existing _notes.md files (newest first).
    If context.md already exists, passes it to the model for incremental update.
    Returns the context.md path.
    """
    notes_files = sorted(output_dir.glob("*_notes.md"), reverse=True)
    if not notes_files:
        raise ValueError("No notes files found — generate notes for at least one recording first")

    context_path = output_dir / "context.md"
    existing_context = context_path.read_text(encoding="utf-8") if context_path.exists() else ""

    # Build notes text (cap at 20 most recent to stay within context window)
    notes_blocks = []
    for nf in notes_files[:20]:
        notes_blocks.append(nf.read_text(encoding="utf-8"))
    all_notes = "\n\n---\n\n".join(notes_blocks)

    if existing_context:
        prompt = f"""You maintain a living project context document. Update it with the latest meeting notes.

CURRENT CONTEXT DOCUMENT:
{existing_context}

ALL MEETING NOTES (newest first):
{all_notes}

Update the context document. Rules:
- Keep a "Recent Meetings" section: date, topic, one-line outcome, link to notes file
- Maintain a "Decisions Log" with dates — append new decisions, don't remove old ones
- Maintain an "Open Action Items" checklist — add new items, mark done if clearly completed
- Maintain a "Technical Reference" section with all field names, paths, table names, processes, APIs, system names mentioned across all meetings
- Update any sections where new information supersedes old
- Keep it concise — this is a reference document, not a narrative

Output the complete updated document starting with: # Context
"""
    else:
        prompt = f"""You are creating a living project context document from meeting notes.

MEETING NOTES (newest first):
{all_notes}

Create a context.md with these sections:
- **Recent Meetings**: date, topic, one-line outcome, link to notes file
- **Decisions Log**: all decisions made, each with a date
- **Open Action Items**: checklist of all outstanding tasks across all meetings
- **Technical Reference**: all field names, file paths, table names, processes, APIs, system names mentioned — with brief explanations
- **Key Context**: the most important background needed to understand this project

Output the complete document starting with: # Context
"""

    logger.info(f"Updating context.md from {len(notes_files)} notes files via LMStudio...")
    content = _call_lm(prompt)

    # Ensure it starts with the right heading
    if not content.startswith("# Context"):
        content = "# Context\n\n" + content

    # Build the index header (always regenerated, not LM-generated — deterministic)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    index_lines = [f"\n_Last updated: {timestamp}_\n", "\n## Meeting Notes Index\n"]
    for nf in notes_files:
        stem  = nf.stem.replace("_notes", "")
        parts = stem.split("_")
        date_prefix = parts[0] if parts else ""
        topic = " ".join(parts[3:]).replace("-", " ").title() if len(parts) > 3 else stem
        index_lines.append(f"- [{topic}]({nf.name}) `{date_prefix}`")
    index_lines.append("\n---\n")
    index_block = "\n".join(index_lines)

    # Insert index after the # Context heading line
    lines = content.split("\n")
    # Find first ## heading to insert before it
    insert_at = len(lines)
    for i, line in enumerate(lines[1:], 1):
        if line.startswith("## "):
            insert_at = i
            break
    lines.insert(insert_at, index_block)
    content = "\n".join(lines)

    context_path.write_text(content, encoding="utf-8")
    logger.info(f"context.md updated ({len(notes_files)} meetings, {len(content)} chars)")
    return context_path
