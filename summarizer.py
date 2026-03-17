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
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

LMSTUDIO_URL   = os.getenv("LMSTUDIO_URL",   "http://localhost:1234/v1")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "local-model")
VIEWER_URL     = "http://localhost:8766"


# ── LMStudio client ───────────────────────────────────────────────────────────

def _call_lm(prompt: str) -> str:
    resp = requests.post(
        f"{LMSTUDIO_URL}/chat/completions",
        json={
            "model":       LMSTUDIO_MODEL,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens":  4096,
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

    topic       = meeting.get("topic", json_path.stem)
    date_str    = _fmt_date(meeting.get("start_time") or meeting.get("local_joined", ""))
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
