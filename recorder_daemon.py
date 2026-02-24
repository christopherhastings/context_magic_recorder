"""
recorder_daemon.py
Unified recording daemon for Zoom, Chrome Meet, and Safari Meet.

Run:   python recorder_daemon.py
Logs:  tail -f /tmp/recorder_daemon.log
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
import websockets
from dotenv import load_dotenv

from zoom_detector import ZoomDetector
from archiver import archive_audio
from permissions import run_checks
from processor import process_recording
from zoom_cloud import wait_and_fetch
from audio_router import get_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/tmp/recorder_daemon.log"),
    ],
)
logger = logging.getLogger("daemon")

# ── Config ─────────────────────────────────────────────────────────────────────
BLACKHOLE_DEVICE  = os.getenv("BLACKHOLE_DEVICE",  "BlackHole 2ch")
OUTPUT_DIR        = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))
WHISPER_MODEL     = os.getenv("WHISPER_MODEL", "medium.en")
HF_TOKEN          = os.getenv("HF_TOKEN")
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL", "3"))
WS_PORT           = int(os.getenv("WS_PORT", "8765"))
NUM_SPEAKERS      = int(os.getenv("NUM_SPEAKERS", "4"))
CLOUD_API_ENABLED = bool(os.getenv("ZOOM_ACCOUNT_ID"))
STATUS_FILE       = Path("/tmp/recorder_status.json")


# ── Helpers ────────────────────────────────────────────────────────────────────

def write_status(state: str, **kwargs):
    try:
        STATUS_FILE.write_text(json.dumps({
            "state":   state,
            "updated": datetime.now().isoformat(),
            **kwargs,
        }))
    except Exception:
        pass


def notify(title: str, message: str = ""):
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            check=True, timeout=3,
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# RecordingSession
# ══════════════════════════════════════════════════════════════════════════════

class RecordingSession:
    def __init__(self, source: str, meta: dict):
        self.source       = source
        self.meta         = dict(meta)
        self.started_at   = datetime.now(timezone.utc)
        self.ended_at     = None
        self.capture_mode = "ffmpeg" if source in ("zoom", "safari_meet") else "stream"

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts      = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        raw     = meta.get("topic", source)
        # Strip filesystem-unsafe chars — / is critical, it creates phantom subdirectories
        topic   = re.sub(r'[/\\\\:*?"<>|]', '-', raw).replace(" ", "_").strip("-_")[:40]
        src_tag = {"zoom": "zoom", "chrome_meet": "meet-chrome", "safari_meet": "meet-safari"}[source]
        ext     = ".wav" if self.capture_mode == "ffmpeg" else ".webm"
        self.audio_path = OUTPUT_DIR / f"{ts}_{src_tag}_{topic}{ext}"

        self._ffmpeg_proc = None
        self._stream_file = None
        self._bytes_recv  = 0

        # Only Safari Meet needs system-level audio routing
        self._router = get_router() if source == "safari_meet" else None

    def start_ffmpeg(self):
        # Activate audio routing BEFORE ffmpeg starts (don't miss opening audio)
        if self._router:
            if not self._router.activate():
                logger.warning(
                    "[safari_meet] Audio routing unavailable — "
                    "other apps playing audio may appear in this recording."
                )

        cmd = [
            "ffmpeg", "-f", "avfoundation",
            "-i", f":{BLACKHOLE_DEVICE}",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(self.audio_path), "-y", "-loglevel", "error",
        ]
        self._ffmpeg_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        logger.info(f"[{self.source}] ffmpeg → {self.audio_path.name}")

    def stop_ffmpeg(self):
        if not self._ffmpeg_proc:
            return
        try:
            self._ffmpeg_proc.stdin.write(b"q")
            self._ffmpeg_proc.stdin.flush()
            self._ffmpeg_proc.wait(timeout=10)
        except Exception:
            self._ffmpeg_proc.terminate()
            self._ffmpeg_proc.wait()
        self._ffmpeg_proc = None

        # Restore audio AFTER ffmpeg stops (capture right to the end)
        if self._router:
            self._router.deactivate()

    def open_stream_file(self):
        self._stream_file = open(self.audio_path, "wb")

    def write_chunk(self, data: bytes):
        if self._stream_file:
            self._stream_file.write(data)
            self._bytes_recv += len(data)

    def close_stream_file(self) -> Path:
        if self._stream_file:
            self._stream_file.close()
            self._stream_file = None
        wav_path = self.audio_path.with_suffix(".wav")
        try:
            subprocess.run([
                "ffmpeg", "-i", str(self.audio_path),
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                str(wav_path), "-y", "-loglevel", "error",
            ], check=True)
            self.audio_path.unlink()
            self.audio_path = wav_path
        except Exception as e:
            logger.error(f"WebM→WAV failed: {e}")
        return self.audio_path

    def stop(self):
        self.ended_at = datetime.now(timezone.utc)
        self.meta.update({
            "local_joined": self.started_at.isoformat(),
            "local_left":   self.ended_at.isoformat(),
        })
        mins = (self.ended_at - self.started_at).total_seconds() / 60
        logger.info(f"[{self.source}] stopped after {mins:.1f} min")

        if self.capture_mode == "ffmpeg":
            self.stop_ffmpeg()
        else:
            self.close_stream_file()

    def post_process_async(self):
        threading.Thread(
            target=self._post_process, daemon=True,
            name=f"processor-{self.source}",
        ).start()

    def _post_process(self):
        meta  = dict(self.meta)
        topic = meta.get("topic", "meeting")
        write_status("processing", meeting_topic=topic, source=self.source)
        notify(f"Processing: {topic}", "Transcription + diarization running...")
        logger.info(f"=== post-processing [{self.source}] ===")

        # Zoom Cloud API enrichment (optional — only if credentials configured)
        if self.source == "zoom" and CLOUD_API_ENABLED:
            meeting_id = meta.get("meetingId") or meta.get("meeting_id")
            if meeting_id:
                cloud = wait_and_fetch(meeting_id)
                if cloud:
                    lj, ll = meta.get("local_joined"), meta.get("local_left")
                    meta.update(cloud)
                    meta["local_joined"], meta["local_left"] = lj, ll

        if not self.audio_path.exists():
            logger.error(f"Audio file missing: {self.audio_path}")
            write_status("error", error=f"Audio file missing: {self.audio_path.name}")
            return

        try:
            if HF_TOKEN:
                # Full pipeline: faster-whisper transcription + pyannote diarization
                json_p, md_p = process_recording(
                    audio_path=self.audio_path,
                    meeting_meta=meta,
                    hf_token=HF_TOKEN,
                    whisper_model=WHISPER_MODEL,
                    num_speakers=NUM_SPEAKERS,
                )
            else:
                # No diarization — transcription only via faster-whisper directly
                logger.warning("HF_TOKEN not set — transcription only, no speaker labels")
                from faster_whisper import WhisperModel
                from processor import render_markdown, build_output_json

                model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
                segments, _ = model.transcribe(
                    str(self.audio_path),
                    language="en",
                    word_timestamps=True,
                    vad_filter=True,
                )

                # Flatten to a single speaker turn per segment (no diarization)
                turns = []
                for seg in segments:
                    turns.append({
                        "speaker": "Speaker",
                        "start":   round(seg.start, 3),
                        "end":     round(seg.end, 3),
                        "text":    seg.text.strip(),
                    })

                # Write JSON
                json_data = {
                    "schema_version": "1.0",
                    "recording": {
                        "file": str(self.audio_path),
                        "processed_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "meeting": meta,
                    "speakers": ["Speaker"],
                    "transcript": {"turns": turns, "words": []},
                    "diarization_segments": [],
                }
                json_path = self.audio_path.with_suffix(".json")
                json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False))

                # Write Markdown
                md_lines = [f"# {meta.get('topic', 'Meeting')}\n"]
                for t in turns:
                    m, s = divmod(int(t["start"]), 60)
                    md_lines.append(f"**{t['speaker']}** `[{m:02d}:{s:02d}]`")
                    md_lines.append(f"> {t['text']}\n")
                self.audio_path.with_suffix(".md").write_text("\n".join(md_lines))

            archive_audio(self.audio_path)
            logger.info(f"=== done: {topic} ===")
            write_status("idle")
            notify(f"Transcript ready: {topic}", "Open viewer at localhost:8766")

        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)
            write_status("error", error=str(e))
            notify("Recording processing failed", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Zoom Watcher
# ══════════════════════════════════════════════════════════════════════════════

class ZoomWatcher:
    # How many consecutive misses before we declare the call over.
    # At POLL_INTERVAL=3s, MISS_THRESHOLD=4 means 12 seconds of silence
    # before stopping — enough to survive Zoom's brief audio routing gaps.
    MISS_THRESHOLD = 4

    def __init__(self):
        self._detector    = ZoomDetector(blackhole_device=BLACKHOLE_DEVICE)
        self._session: RecordingSession | None = None
        self._in_call     = False
        self._miss_count  = 0
        self._recording_since: datetime | None = None

    def _tick(self):
        info = self._detector.poll()

        if info.in_call:
            self._miss_count = 0   # reset debounce on any positive detection

            if not self._in_call:
                # Call just started
                self._in_call = True
                self._recording_since = datetime.now()
                logger.info(f"[zoom] 🔴 {info.topic}")
                self._session = RecordingSession("zoom", {"topic": info.topic})
                self._session.start_ffmpeg()
                write_status("recording",
                    meeting_topic=info.topic,
                    source="zoom",
                    recording_since=self._recording_since.isoformat(),
                )
                notify(f"Recording: {info.topic}", "Zoom call detected")
            else:
                # Still in call — refresh status so menu bar stays current
                elapsed = (datetime.now() - self._recording_since).total_seconds()
                write_status("recording",
                    meeting_topic=info.topic,
                    source="zoom",
                    recording_since=self._recording_since.isoformat(),
                    elapsed_seconds=int(elapsed),
                )

        else:
            if self._in_call:
                self._miss_count += 1
                logger.debug(f"[zoom] miss {self._miss_count}/{self.MISS_THRESHOLD}")

                if self._miss_count >= self.MISS_THRESHOLD:
                    # Confirmed call ended — stop and process
                    logger.info(f"[zoom] ⏹ call ended (confirmed after {self._miss_count} misses)")
                    self._in_call    = False
                    self._miss_count = 0
                    if self._session:
                        self._session.stop()
                        self._session.post_process_async()
                        self._session = None

    def run(self):
        logger.info("[zoom] watcher started")
        while True:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"[zoom] tick error: {e}")
            time.sleep(POLL_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# Meet WebSocket Server (Chrome + Safari extensions)
# ══════════════════════════════════════════════════════════════════════════════

class MeetServer:
    def __init__(self):
        self._sessions: dict[str, RecordingSession] = {}

    async def handle(self, websocket):
        client_id = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
        logger.info(f"[meet-ws] connected: {client_id}")
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    s = self._sessions.get(client_id)
                    if s and s.capture_mode == "stream":
                        s.write_chunk(message)
                else:
                    try:
                        msg = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_event(client_id, msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._end_session(client_id)
            logger.info(f"[meet-ws] disconnected: {client_id}")

    async def _handle_event(self, client_id: str, msg: dict):
        t      = msg.get("type")
        source = msg.get("source", "chrome_meet")
        meta   = msg.get("meta", {})

        if t == "meeting_start":
            topic = meta.get("topic", "Google Meet")
            logger.info(f"[{source}] 🔴 {topic}")
            session = RecordingSession(source, meta)
            self._sessions[client_id] = session
            if session.capture_mode == "stream":
                session.open_stream_file()
            else:
                session.start_ffmpeg()
            write_status("recording",
                meeting_topic=topic,
                source=source,
                recording_since=datetime.now().isoformat(),
            )
            browser = "Chrome" if "chrome" in source else "Safari"
            notify(f"Recording: {topic}", f"{browser} Meet call detected")

        elif t == "meeting_end":
            await self._end_session(client_id)

        elif t == "meta_update":
            s = self._sessions.get(client_id)
            if s:
                s.meta.update(meta)

        elif t == "selector_broken":
            logger.warning("[meet] DOM selector failure reported by extension")
            write_status("selector_broken")

    async def _end_session(self, client_id: str):
        session = self._sessions.pop(client_id, None)
        if not session:
            return
        session.stop()
        session.post_process_async()

    async def serve(self):
        logger.info(f"[meet-ws] listening on ws://localhost:{WS_PORT}")
        async with websockets.serve(self.handle, "localhost", WS_PORT):
            await asyncio.Future()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    logger.info("══════════════════════════════════════")
    logger.info("  Recorder Daemon")
    logger.info(f"  Output:      {OUTPUT_DIR}")
    logger.info(f"  BlackHole:   {BLACKHOLE_DEVICE}")
    logger.info(f"  Whisper:     {WHISPER_MODEL}")
    logger.info(f"  Diarization: {'✓' if HF_TOKEN else '✗ — set HF_TOKEN in .env'}")
    logger.info(f"  Zoom Cloud:  {'✓' if CLOUD_API_ENABLED else '✗ — optional'}")
    logger.info(f"  WS port:     {WS_PORT}")
    logger.info("══════════════════════════════════════")

    # Check permissions first — opens System Settings if anything is missing
    run_checks(notify_fn=notify)

    write_status("idle")

    threading.Thread(target=ZoomWatcher().run, daemon=True, name="zoom").start()

    try:
        asyncio.run(MeetServer().serve())
    except KeyboardInterrupt:
        write_status("idle")
        logger.info("Shutdown.")
        sys.exit(0)


if __name__ == "__main__":
    main()
