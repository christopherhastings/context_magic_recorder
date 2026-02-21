"""
api_server.py
FastAPI server that serves transcript data to the React viewer.
Runs on http://localhost:8766

Endpoints:
  GET /api/recordings          → list of all recordings (summary)
  GET /api/recordings/{id}     → full transcript JSON for one recording
  GET /api/search?q=...        → search across all transcripts

The recording "id" is the filename stem:
  2026-02-17_09-02-15_zoom_Weekly_Sync
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))

app = FastAPI(title="Recorder API", version="1.0.0")

# Allow the React viewer (Vite dev server or file://) to call this
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_all_json_files() -> list[Path]:
    if not OUTPUT_DIR.exists():
        return []
    # Exclude .meta.json files (those are fallback metadata, not full transcripts)
    return sorted(
        [p for p in OUTPUT_DIR.glob("*.json") if not p.name.endswith(".meta.json")],
        reverse=True,  # newest first
    )


def load_recording(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def make_summary(recording_id: str, data: dict) -> dict:
    """Lightweight summary for the list view — no transcript turns."""
    meeting = data.get("meeting", {})
    turns   = data.get("transcript", {}).get("turns", [])
    return {
        "id":           recording_id,
        "topic":        meeting.get("topic", recording_id),
        "source":       _infer_source(recording_id),
        "start_time":   meeting.get("start_time") or meeting.get("local_joined"),
        "duration_minutes": meeting.get("duration_minutes"),
        "participant_count": len(meeting.get("participants", [])),
        "participants": meeting.get("participants", []),
        "speaker_count": len(data.get("speakers", [])),
        "turn_count":   len(turns),
        "has_agenda":   bool(meeting.get("agenda")),
        "processed_at": data.get("recording", {}).get("processed_at"),
        "audio_file":   data.get("recording", {}).get("file"),
    }


def _infer_source(recording_id: str) -> str:
    if "meet-chrome" in recording_id:  return "google_meet_chrome"
    if "meet-safari" in recording_id:  return "google_meet_safari"
    if "zoom" in recording_id:         return "zoom"
    return "unknown"


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/api/recordings")
def list_recordings():
    """All recordings as summaries, newest first."""
    summaries = []
    for path in get_all_json_files():
        try:
            data = load_recording(path)
            summaries.append(make_summary(path.stem, data))
        except Exception as e:
            # Don't let one corrupt file break the whole list
            summaries.append({
                "id":    path.stem,
                "topic": path.stem,
                "error": str(e),
            })
    return {"recordings": summaries, "total": len(summaries)}


@app.get("/api/recordings/{recording_id}")
def get_recording(recording_id: str):
    """Full transcript data for a single recording."""
    # Sanitise ID — no path traversal
    safe_id = re.sub(r"[^\w\-]", "", recording_id)
    path = OUTPUT_DIR / f"{safe_id}.json"

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Recording not found: {recording_id}")

    try:
        return load_recording(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search")
def search(q: str = Query(..., min_length=1)):
    """
    Full-text search across all transcript turns.
    Returns list of {recording_id, topic, matches: [{speaker, start, text}]}
    """
    q_lower = q.lower()
    results = []

    for path in get_all_json_files():
        try:
            data   = load_recording(path)
            turns  = data.get("transcript", {}).get("turns", [])
            meeting = data.get("meeting", {})

            matches = [
                {
                    "speaker": t["speaker"],
                    "start":   t["start"],
                    "text":    t["text"],
                }
                for t in turns
                if q_lower in t.get("text", "").lower()
            ]

            # Also match on topic / agenda / participant names
            topic_match = q_lower in meeting.get("topic", "").lower()
            agenda_match = q_lower in (meeting.get("agenda") or "").lower()
            participant_match = any(
                q_lower in (p.get("name") or "").lower()
                for p in meeting.get("participants", [])
            )

            if matches or topic_match or agenda_match or participant_match:
                results.append({
                    "id":           path.stem,
                    "topic":        meeting.get("topic", path.stem),
                    "start_time":   meeting.get("start_time"),
                    "source":       _infer_source(path.stem),
                    "match_count":  len(matches),
                    "matches":      matches[:5],  # preview first 5, client loads full if needed
                    "topic_match":  topic_match,
                })
        except Exception:
            continue

    return {"query": q, "results": results, "total": len(results)}


@app.get("/api/status")
def status():
    """Current daemon recording state (from status file)."""
    status_file = Path("/tmp/recorder_status.json")
    if status_file.exists():
        try:
            return json.loads(status_file.read_text())
        except Exception:
            pass
    return {"state": "unknown"}


@app.get("/api/health")
def health():
    return {"ok": True, "output_dir": str(OUTPUT_DIR), "exists": OUTPUT_DIR.exists()}


# ── Dev entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="info")
