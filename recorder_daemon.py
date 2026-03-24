"""
recorder_daemon.py
Unified recording daemon for Zoom, Chrome Meet, and Safari Meet.

Run:   python recorder_daemon.py
Logs:  tail -f /tmp/recorder_daemon.log
"""

import asyncio
import json
import logging
import logging.handlers
import os
import shutil
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

from archiver import archive_audio
from permissions import run_checks
from processor import process_recording
from zoom_cloud import wait_and_fetch
from audio_router import get_router

# Absolute path resolution for background processes
BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"

if env_path.exists():
    load_dotenv(dotenv_path=env_path)


def configure_logging() -> Path | None:
    """
    Console + file logging. Env:
      LOG_LEVEL — DEBUG, INFO (default), WARNING, ERROR
      LOG_FILE — default /tmp/recorder_daemon.log
      LOG_MAX_BYTES — if > 0, use RotatingFileHandler (e.g. 5242880 for 5 MiB)
      LOG_BACKUP_COUNT — rotated files to keep (default 5)
      ZOOM_DETECT_DEBUG — 1/true → zoom_detector logger at DEBUG
    """
    root = logging.getLogger()
    root.handlers.clear()

    level_name = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
        sys.stderr.write(f"[recorder] Invalid LOG_LEVEL={level_name!r}, using INFO\n")

    root.setLevel(level)

    date_fmt = "%Y-%m-%d %H:%M:%S"
    stream_fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt=date_fmt
    )

    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(stream_fmt)
    root.addHandler(stderr_h)

    log_path = Path(os.getenv("LOG_FILE", "/tmp/recorder_daemon.log")).expanduser()
    file_path: Path | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = int(os.getenv("LOG_MAX_BYTES", "0") or "0")
        backup_ct = max(1, int(os.getenv("LOG_BACKUP_COUNT", "5") or "5"))
        if max_bytes > 0:
            fh: logging.Handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=max_bytes,
                backupCount=backup_ct,
                encoding="utf-8",
            )
        else:
            fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(stream_fmt)
        root.addHandler(fh)
        file_path = log_path
    except OSError as e:
        sys.stderr.write(f"[recorder] File logging disabled ({log_path}): {e}\n")

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if os.getenv("ZOOM_DETECT_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
        logging.getLogger("zoom_detector").setLevel(logging.DEBUG)

    return file_path


LOG_FILE_PATH = configure_logging()
logger = logging.getLogger("daemon")

if not env_path.exists():
    logger.warning("`.env` file not found at %s — set HF_TOKEN and paths there", env_path)

# Double check the token is actually in memory now
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    logger.warning("HF_TOKEN is missing or empty — diarization will be skipped")

from zoom_detector import ZoomDetector

# ── Config ─────────────────────────────────────────────────────────────────────
BLACKHOLE_DEVICE  = os.getenv("BLACKHOLE_DEVICE",  "BlackHole 2ch")
MIC_DEVICE        = os.getenv("MIC_DEVICE", "")  # auto-detect if blank
OUTPUT_DIR        = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))
WHISPER_MODEL     = os.getenv("WHISPER_MODEL", "medium.en")
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL", "3"))
WS_PORT           = int(os.getenv("WS_PORT", "8765"))
NUM_SPEAKERS      = int(os.getenv("NUM_SPEAKERS")) if os.getenv("NUM_SPEAKERS") else None
CLOUD_API_ENABLED = bool(os.getenv("ZOOM_ACCOUNT_ID"))
STATUS_FILE       = Path("/tmp/recorder_status.json")


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_hf_token():
    """Fetch HF_TOKEN at runtime so it's fresh after load_dotenv."""
    return os.getenv("HF_TOKEN")


def detect_mic_device() -> str:
    """Auto-detect the default system input device for local voice capture."""
    if MIC_DEVICE:
        return MIC_DEVICE
    try:
        import sounddevice as sd
        info = sd.query_devices(sd.default.device[0])
        name = info["name"]
        logger.info(f"[mic] auto-detected: {name}")
        return name
    except Exception:
        return "MacBook Pro Microphone"


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


def get_ffmpeg_binary() -> str:
    """
    launchd jobs get a minimal PATH — bare 'ffmpeg' often fails.
    Prefer FFMPEG_BIN, then PATH, then Homebrew locations (Intel vs Apple Silicon).
    """
    env = (os.getenv("FFMPEG_BIN") or "").strip()
    if env:
        return env
    which = shutil.which("ffmpeg")
    if which:
        return which
    for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(p).is_file():
            return p
    return "ffmpeg"


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
        # Strip filesystem-unsafe and shell-unsafe chars from topic for the filename.
        # Apostrophes and backticks are especially dangerous — they break ffmpeg args.
        topic   = re.sub(r'''[/\\:*?"<>|'`\[\](){}#&;!$~]''', '', raw).replace(" ", "_").strip("-_")[:40]
        src_tag = {"zoom": "zoom", "chrome_meet": "meet-chrome", "safari_meet": "meet-safari"}[source]
        ext     = ".wav" if self.capture_mode == "ffmpeg" else ".webm"
        self.audio_path = OUTPUT_DIR / f"{ts}_{src_tag}_{topic}{ext}"

        self._ffmpeg_proc = None
        self._stream_file = None
        self._bytes_recv  = 0

        # Only Safari Meet needs system-level audio routing.
        # Zoom/manual recordings rely on the system already being set to
        # a Multi-Output Device (e.g. Magic_Context) that includes BlackHole.
        # Activating the router for Zoom disrupts the audio path and kills ffmpeg.
        self._router = get_router() if source == "safari_meet" else None

    def start_ffmpeg(self):
        # Activate audio routing BEFORE ffmpeg starts (don't miss opening audio)
        if self._router:
            if not self._router.activate():
                logger.warning(
                    "[safari_meet] Audio routing unavailable — "
                    "other apps playing audio may appear in this recording."
                )

        ff = get_ffmpeg_binary()

        if self.source == "zoom":
            mic_device = detect_mic_device()
            # Stereo: L=BlackHole (remote participants), R=mic (your voice)
            # aformat forces each input to mono before merge; pan assigns channels explicitly
            cmd = [
                ff,
                "-f", "avfoundation",
                "-i", f":{BLACKHOLE_DEVICE}",   # Input 0: remote audio
                "-f", "avfoundation",
                "-i", f":{mic_device}",          # Input 1: local mic
                "-filter_complex",
                "[0:a]aformat=channel_layouts=mono[left];"
                "[1:a]aformat=channel_layouts=mono[right];"
                "[left][right]amerge=inputs=2,pan=stereo|c0<c0|c1<c1[out]",
                "-map", "[out]",
                "-ar", "16000", "-ac", "2", "-c:a", "pcm_s16le",
                str(self.audio_path), "-y", "-loglevel", "error",
            ]
            logger.info(f"[{self.source}] stereo capture: L={BLACKHOLE_DEVICE} R={mic_device}")
        else:
            # Safari Meet / Chrome Meet fallback: mono from BlackHole only
            cmd = [
                ff,
                "-f", "avfoundation",
                "-i", f":{BLACKHOLE_DEVICE}",
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                str(self.audio_path), "-y", "-loglevel", "error",
            ]

        # CRITICAL: Do NOT use stdout=PIPE or stderr=PIPE here.
        # ffmpeg runs for the entire meeting — piped buffers fill up (~64KB)
        # and ffmpeg blocks, deadlocking the recording. Send stderr to the
        # log file instead so we can still debug failures after the fact.
        ffmpeg_log = Path("/tmp/recorder_ffmpeg.log")
        self._ffmpeg_log_fh = open(ffmpeg_log, "a")
        self._ffmpeg_log_fh.write(f"\n--- {datetime.now().isoformat()} | {self.audio_path.name} ---\n")
        self._ffmpeg_log_fh.write(f"cmd: {' '.join(cmd)}\n")
        self._ffmpeg_log_fh.flush()
        self._ffmpeg_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=self._ffmpeg_log_fh,
        )
        logger.info(f"[{self.source}] ffmpeg ({ff}) → {self.audio_path.name}  (stderr → {ffmpeg_log})")

        # Give ffmpeg a moment to start, then verify it's still alive.
        # If it died on launch (bad device, permission error, etc.) we catch it
        # here instead of discovering a missing WAV file 30 minutes later.
        time.sleep(0.5)
        if self._ffmpeg_proc.poll() is not None:
            self._ffmpeg_log_fh.flush()
            logger.error(
                f"[{self.source}] ffmpeg died on startup! exit code: {self._ffmpeg_proc.returncode} "
                f"— check /tmp/recorder_ffmpeg.log"
            )
            self._ffmpeg_proc = None

    def stop_ffmpeg(self):
        if not self._ffmpeg_proc:
            return
        try:
            self._ffmpeg_proc.stdin.write(b"q")
            self._ffmpeg_proc.stdin.flush()
            return_code = self._ffmpeg_proc.wait(timeout=10)
            if return_code != 0:
                logger.error(f"[{self.source}] ffmpeg exited with code {return_code} — check /tmp/recorder_ffmpeg.log")
        except Exception as e:
            logger.error(f"[{self.source}] Error stopping ffmpeg: {e}", exc_info=True)
            self._ffmpeg_proc.terminate()
            self._ffmpeg_proc.wait()
        finally:
            self._ffmpeg_proc = None
            if hasattr(self, "_ffmpeg_log_fh") and self._ffmpeg_log_fh:
                self._ffmpeg_log_fh.close()
                self._ffmpeg_log_fh = None

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
                get_ffmpeg_binary(), "-i", str(self.audio_path),
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
            if get_hf_token():
                # Full pipeline: faster-whisper transcription + pyannote diarization
                json_p, md_p = process_recording(
                    audio_path=self.audio_path,
                    meeting_meta=meta,
                    hf_token=get_hf_token(),
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
    """
    Manages a long-running, continuous recording session that can be
    manually started, stopped, and split into multiple files.
    This replaces the automatic, window-title-based detection.
    """
    def __init__(self):
        self._session: RecordingSession | None = None
        self._lock = threading.Lock()

    def start_recording(self, topic="Recording"):
        with self._lock:
            if self._session:
                logger.warning("[zoom] start_recording ignored, session already active")
                return
            self._session = RecordingSession("zoom", {"topic": topic})
            self._session.start_ffmpeg()
            write_status("recording", topic=topic, meeting_topic=topic, source="zoom_manual")
            logger.info(f"[zoom] 🔴 Manual recording started: {topic}")
            notify("Recording Started", topic)

    def stop_recording(self):
        with self._lock:
            if not self._session:
                logger.warning("[zoom] stop_recording ignored, no session active")
                return
            logger.info("[zoom] Manual recording stopped.")
            self._session.stop()
            self._session.post_process_async()
            self._session = None
            write_status("idle")
            notify("Recording Stopped", "")

    def split_recording(self, new_topic="New Segment"):
        with self._lock:
            if not self._session:
                logger.warning("[zoom] split_recording ignored, no session active")
                return

            old_topic = self._session.meta.get("topic", "Segment")
            logger.info(f"[zoom] Splitting recording. New segment: {new_topic}")

            self._session.stop()
            self._session.post_process_async()

            self._session = RecordingSession("zoom", {"topic": new_topic, "prev_topic": old_topic})
            self._session.start_ffmpeg()
            write_status("recording", topic=new_topic, meeting_topic=new_topic, source="zoom_manual")
            notify("Recording Split", f"New segment: {new_topic}")

    def run(self):
        """This thread no longer polls, just keeps the watcher alive."""
        logger.info("[zoom] watcher running in manual command mode")
        while True:
            time.sleep(3600)  # Keep thread alive, do nothing


# ══════════════════════════════════════════════════════════════════════════════
# Meet WebSocket Server (Chrome + Safari extensions + Manual Control)
# ══════════════════════════════════════════════════════════════════════════════

class MeetServer:
    def __init__(self, zoom_watcher: ZoomWatcher):
        self._sessions: dict[str, RecordingSession] = {}
        self._zoom_watcher = zoom_watcher

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
                        logger.warning(f"[meet-ws] invalid JSON from {client_id}")
                        continue

                    # Route command to the appropriate handler
                    if "command" in msg:
                        await self._handle_command(client_id, msg)
                    else:
                        await self._handle_event(client_id, msg)

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._end_session(client_id)
            logger.info(f"[meet-ws] disconnected: {client_id}")

    async def _handle_command(self, client_id: str, msg: dict):
        cmd = msg.get("command")
        logger.info(f"[meet-ws] command from {client_id}: {cmd}")

        if cmd == "start_manual_recording":
            self._zoom_watcher.start_recording(msg.get("topic", "Manual Recording"))
        elif cmd == "stop_manual_recording":
            self._zoom_watcher.stop_recording()
        elif cmd == "split_manual_recording":
            self._zoom_watcher.split_recording(msg.get("topic", "New Segment"))
        else:
            logger.warning(f"[meet-ws] unknown command: {cmd}")


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
    logger.info(f"  Diarization: {'✓' if os.getenv('HF_TOKEN') else '✗ — set HF_TOKEN in .env'}")
    logger.info(f"  Zoom Cloud:  {'✓' if CLOUD_API_ENABLED else '✗ — optional'}")
    logger.info(f"  WS port:     {WS_PORT}")
    logger.info(f"  ffmpeg:      {get_ffmpeg_binary()}")
    logger.info(
        f"  Log level:   {logging.getLevelName(logging.getLogger().getEffectiveLevel())}"
    )
    if LOG_FILE_PATH:
        logger.info("  Log file:    %s", LOG_FILE_PATH)
    logger.info("══════════════════════════════════════")

    # Check permissions first — opens System Settings if anything is missing
    run_checks(notify_fn=notify)

    write_status("idle")

    # The new manual watcher runs in the background but doesn't poll.
    # It waits for commands from the WebSocket server.
    zoom_watcher = ZoomWatcher()
    threading.Thread(target=zoom_watcher.run, daemon=True, name="zoom").start()

    # The WebSocket server now handles Meet extension events AND manual commands.
    meet_server = MeetServer(zoom_watcher=zoom_watcher)
    try:
        asyncio.run(meet_server.serve())
    except KeyboardInterrupt:
        write_status("idle")
        logger.info("Shutdown.")
        sys.exit(0)


if __name__ == "__main__":
    main()