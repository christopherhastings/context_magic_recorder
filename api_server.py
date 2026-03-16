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

# Load .env from same directory as this file
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # dotenv optional — fall back to environment variables

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))

app = FastAPI(title="Recorder API", version="1.0.0")


def resolve_recording(recording_id: str) -> tuple[Path, Path]:
    """
    Given a recording_id (which may have had special chars stripped by the URL),
    return (json_path, opus_path) for the best matching recording.
    Raises HTTPException 404 if nothing found.
    """
    safe_id = re.sub(r"[^\w\-]", "", recording_id)

    # Exact match first
    json_path = OUTPUT_DIR / f"{safe_id}.json"
    if json_path.exists():
        return json_path, json_path.with_suffix(".opus")

    # Prefix fallback — match on timestamp + source (first 3 _ segments)
    # e.g. "2026-02-24_09-05-06_zoom" covers any topic variation
    parts = safe_id.split("_")
    if len(parts) >= 3:
        prefix = "_".join(parts[:3])
        candidates = sorted(OUTPUT_DIR.glob(f"{prefix}*.json"))
        if candidates:
            # Pick closest stem length to what was requested
            best = min(candidates, key=lambda p: abs(len(p.stem) - len(safe_id)))
            return best, best.with_suffix(".opus")

    raise HTTPException(status_code=404, detail=f"Recording not found: {recording_id}")

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

VIEWER_HTML = Path(__file__).parent / "viewer.html"


@app.get("/", response_class=FileResponse)
def viewer():
    """Serve the transcript viewer."""
    if not VIEWER_HTML.exists():
        raise HTTPException(status_code=404, detail="viewer.html not found alongside api_server.py")
    return FileResponse(VIEWER_HTML, media_type="text/html")



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


@app.get("/api/recordings")
def list_recordings():
    """All recordings as summaries, newest first."""
    summaries = []
    for path in get_all_json_files():
        try:
            data = load_recording(path)
            summaries.append(make_summary(path.stem, data))
        except Exception as e:
            summaries.append({"id": path.stem, "topic": path.stem, "error": str(e)})
    return {"recordings": summaries, "total": len(summaries)}


@app.get("/api/recordings/{recording_id}")
def get_recording(recording_id: str):
    """Full transcript data for a single recording."""
    json_path, _ = resolve_recording(recording_id)
    try:
        return load_recording(json_path)
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



@app.patch("/api/recordings/{recording_id}")
def rename_recording(recording_id: str, body: dict):
    """
    Update mutable fields on a recording.
    Currently supports: {"topic": "New name"}
    """
    json_path, _ = resolve_recording(recording_id)
    try:
        data = json.loads(json_path.read_text())
        if "topic" in body:
            new_topic = str(body["topic"]).strip()[:120]
            if not new_topic:
                raise HTTPException(status_code=422, detail="Topic cannot be empty")
            data.setdefault("meeting", {})["topic"] = new_topic
            # Also store original if not already saved
            if "original_topic" not in data["meeting"] and data["meeting"].get("topic") != new_topic:
                data["meeting"]["original_topic"] = data["meeting"].get("topic", "")
            data["meeting"]["topic"] = new_topic
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return {"ok": True, "topic": data["meeting"]["topic"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/recordings/{recording_id}/rediarize")
async def rediarize(recording_id: str, body: dict):
    """
    Re-run diarization on an existing recording with a new speaker count.
    Accepts: {"num_speakers": N}  — N must be 1–10.
    Returns the updated transcript JSON when complete.
    This can take 30–120 seconds for a long meeting.
    """
    import asyncio

    json_path, opus_path = resolve_recording(recording_id)

    def _parse_int(key, lo, hi):
        val = body.get(key)
        if val is None:
            return None
        try:
            val = int(val)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail=f"{key} must be an integer")
        if not lo <= val <= hi:
            raise HTTPException(status_code=422, detail=f"{key} must be {lo}–{hi}")
        return val

    num_speakers = _parse_int("num_speakers", 1, 10)
    min_speakers = _parse_int("min_speakers", 1, 10)
    max_speakers = _parse_int("max_speakers", 1, 10)

    # num_speakers is a shorthand for min=max=N
    if num_speakers is not None:
        min_speakers = min_speakers or num_speakers
        max_speakers = max_speakers or num_speakers

    if min_speakers and max_speakers and min_speakers > max_speakers:
        raise HTTPException(status_code=422, detail="min_speakers must be ≤ max_speakers")

    # We need the original audio — Opus archive exists, WAV was deleted.
    # Decode Opus → WAV for reprocessing, then re-archive.
    if not opus_path.exists():
        raise HTTPException(
            status_code=409,
            detail="Audio archive not found — cannot re-diarize without the original audio."
        )

    hf_token   = os.getenv("HF_TOKEN")
    if not hf_token:
        raise HTTPException(status_code=503, detail="HF_TOKEN not set — diarization unavailable")

    whisper_model = os.getenv("WHISPER_MODEL", "medium.en")

    def run_rediarize():
        import sys, subprocess, tempfile
        from pathlib import Path as P

        # Decode Opus → temporary WAV
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_wav = P(tmp.name)

        subprocess.run([
            "ffmpeg", "-i", str(opus_path),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(tmp_wav), "-y", "-loglevel", "error",
        ], check=True)

        # Load existing JSON for metadata
        data = json.loads(json_path.read_text())
        meeting_meta = data.get("meeting", {})

        # Re-run processor
        sys.path.insert(0, str(Path(__file__).parent))
        from processor import process_recording

        process_recording(
            audio_path=tmp_wav,
            meeting_meta=meeting_meta,
            hf_token=hf_token,
            whisper_model=whisper_model,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )

        # processor writes to tmp_wav's stem — move outputs to correct names
        tmp_base = tmp_wav.with_suffix("")
        out_base = json_path.with_suffix("")
        for ext in [".json", ".md"]:
            src = tmp_base.with_suffix(ext)
            dst = out_base.with_suffix(ext)
            if src.exists():
                src.rename(dst)

        tmp_wav.unlink(missing_ok=True)
        return json.loads(json_path.read_text())

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_rediarize)
    return result


@app.post("/api/recordings/{recording_id}/split")
async def split_recording(recording_id: str, body: dict):
    """
    Split a recording into two at a given timestamp.

    Body: {"split_at": 1234.5}  — seconds from start

    Creates two new recordings:
      {stem}_part1.{ext}  — 0 to split_at
      {stem}_part2.{ext}  — split_at to end

    The original is renamed to {stem}_original.{ext}.
    Returns {"part1_id": "...", "part2_id": "..."}
    """
    import asyncio

    json_path, opus_path = resolve_recording(recording_id)

    split_at = body.get("split_at")
    if split_at is None:
        raise HTTPException(status_code=422, detail="split_at (seconds) required")
    try:
        split_at = float(split_at)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="split_at must be a number")
    if split_at <= 0:
        raise HTTPException(status_code=422, detail="split_at must be > 0")

    if not opus_path.exists():
        raise HTTPException(status_code=409, detail="Audio archive not found")

    def run_split():
        import subprocess, json as _json
        from pathlib import Path as P

        base   = json_path.with_suffix("")
        p1_base = P(f"{base}_part1")
        p2_base = P(f"{base}_part2")
        orig_base = P(f"{base}_original")

        # ── Split audio ────────────────────────────────────────────────
        p1_opus = p1_base.with_suffix(".opus")
        p2_opus = p2_base.with_suffix(".opus")

        # Part 1: 0 → split_at
        subprocess.run([
            "ffmpeg", "-i", str(opus_path),
            "-t", str(split_at),
            "-c", "copy", str(p1_opus), "-y", "-loglevel", "error",
        ], check=True)

        # Part 2: split_at → end
        subprocess.run([
            "ffmpeg", "-i", str(opus_path),
            "-ss", str(split_at),
            "-c", "copy", str(p2_opus), "-y", "-loglevel", "error",
        ], check=True)

        # ── Split transcript ───────────────────────────────────────────
        data     = _json.loads(json_path.read_text())
        meeting  = data.get("meeting", {})
        speakers = data.get("speakers", [])
        turns    = data.get("transcript", {}).get("turns", [])

        turns1 = [t for t in turns if t["start"] < split_at]
        turns2 = [t for t in turns if t["start"] >= split_at]

        # Re-zero part2 timestamps
        turns2_zeroed = [
            {**t, "start": round(t["start"] - split_at, 3),
                  "end":   round(t["end"]   - split_at, 3)}
            for t in turns2
        ]

        def make_part(part_turns, part_num, audio_file):
            return {
                "schema_version": "1.1",
                "recording": {
                    "file": str(audio_file),
                    "processed_at": data.get("recording", {}).get("processed_at"),
                    "split_from": str(json_path),
                    "split_part": part_num,
                },
                "meeting": {
                    **meeting,
                    "topic": f"{meeting.get('topic', 'Meeting')} (part {part_num})",
                    "original_topic": meeting.get("topic", ""),
                },
                "speakers": speakers,
                "transcript": {"turns": part_turns},
                "diarization_segments": [],
            }

        p1_json = p1_base.with_suffix(".json")
        p2_json = p2_base.with_suffix(".json")
        p1_json.write_text(_json.dumps(make_part(turns1, 1, p1_opus), indent=2))
        p2_json.write_text(_json.dumps(make_part(turns2_zeroed, 2, p2_opus), indent=2))

        # ── Rename original ────────────────────────────────────────────
        orig_json = orig_base.with_suffix(".json")
        orig_opus = orig_base.with_suffix(".opus")
        json_path.rename(orig_json)
        opus_path.rename(orig_opus)

        return {
            "part1_id": p1_base.name,
            "part2_id": p2_base.name,
            "original_id": orig_base.name,
            "split_at": split_at,
        }

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_split)
    return result


# ── Dev entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="info")
