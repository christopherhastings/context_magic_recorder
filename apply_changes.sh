#!/bin/bash
# Apply code changes to recorder project
# Run from project root: bash apply_changes.sh

set -e

# recorder_daemon.py changes
python3 << 'PYEOF'
import re

with open("recorder_daemon.py", "r") as f:
    content = f.read()

# 1. Add MIC_DEVICE after BLACKHOLE_DEVICE
content = content.replace(
    'BLACKHOLE_DEVICE  = os.getenv("BLACKHOLE_DEVICE",  "BlackHole 2ch")\nOUTPUT_DIR        = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))',
    'BLACKHOLE_DEVICE  = os.getenv("BLACKHOLE_DEVICE",  "BlackHole 2ch")\nMIC_DEVICE        = os.getenv("MIC_DEVICE", "")  # auto-detect if blank\nOUTPUT_DIR        = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))'
)

# 2. Add detect_mic_device before write_status
detect_fn = '''def detect_mic_device() -> str:
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


'''
content = content.replace(
    '# ── Helpers ────────────────────────────────────────────────────────────────────\n\ndef write_status',
    '# ── Helpers ────────────────────────────────────────────────────────────────────\n\n' + detect_fn + 'def write_status'
)

# 3. Replace start_ffmpeg cmd block
old_ffmpeg = '''    def start_ffmpeg(self):
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
        logger.info(f"[{self.source}] ffmpeg → {self.audio_path.name}")'''

new_ffmpeg = '''    def start_ffmpeg(self):
        # Activate audio routing BEFORE ffmpeg starts (don't miss opening audio)
        if self._router:
            if not self._router.activate():
                logger.warning(
                    "[safari_meet] Audio routing unavailable — "
                    "other apps playing audio may appear in this recording."
                )

        if self.source == "zoom":
            mic = detect_mic_device()
            cmd = [
                "ffmpeg",
                "-f", "avfoundation", "-i", f":{BLACKHOLE_DEVICE}",
                "-f", "avfoundation", "-i", f":{mic}",
                "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[aout]",
                "-map", "[aout]", "-ar", "16000", "-ac", "2", "-c:a", "pcm_s16le",
                str(self.audio_path), "-y", "-loglevel", "error",
            ]
            logger.info(f"[{self.source}] stereo capture: {BLACKHOLE_DEVICE} (L) + {mic} (R)")
        else:
            cmd = [
                "ffmpeg", "-f", "avfoundation",
                "-i", f":{BLACKHOLE_DEVICE}",
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                str(self.audio_path), "-y", "-loglevel", "error",
            ]
        self._ffmpeg_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        logger.info(f"[{self.source}] ffmpeg → {self.audio_path.name}")'''

content = content.replace(old_ffmpeg, new_ffmpeg)

# 4. Add _current_topic to ZoomWatcher.__init__
content = content.replace(
    'self._recording_since: datetime | None = None\n\n    def _tick(self):',
    'self._recording_since: datetime | None = None\n        self._current_topic: str = ""\n\n    def _tick(self):'
)

# 4b. Replace _tick
old_tick = '''    def _tick(self):
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
                        self._session = None'''

new_tick = '''    def _tick(self):
        info = self._detector.poll()

        if info.in_call:
            self._miss_count = 0

            if not self._in_call:
                # CASE A: New meeting
                self._in_call = True
                self._recording_since = datetime.now()
                self._current_topic = info.topic
                logger.info(f"[zoom] 🔴 {info.topic}")
                self._session = RecordingSession("zoom", {"topic": info.topic})
                self._session.start_ffmpeg()
                write_status("recording",
                    meeting_topic=info.topic,
                    source="zoom",
                    recording_since=self._recording_since.isoformat(),
                )
                notify(f"Recording: {info.topic}", "Zoom call detected")

            elif info.topic != self._current_topic:
                # CASE B: Topic change — restart session
                logger.info(f"[zoom] topic change: {self._current_topic} → {info.topic}")
                if self._session:
                    self._session.stop()
                    self._session.post_process_async()
                self._current_topic = info.topic
                self._recording_since = datetime.now()
                self._session = RecordingSession("zoom", {"topic": info.topic})
                self._session.start_ffmpeg()
                write_status("recording",
                    meeting_topic=info.topic,
                    source="zoom",
                    recording_since=self._recording_since.isoformat(),
                )
                notify(f"Recording: {info.topic}", "Topic changed — new session")

            else:
                # Still in call — refresh status
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
                    logger.info(f"[zoom] ⏹ call ended (confirmed after {self._miss_count} misses)")
                    self._in_call = False
                    self._miss_count = 0
                    if self._session:
                        self._session.stop()
                        self._session.post_process_async()
                        self._session = None'''

content = content.replace(old_tick, new_tick)

with open("recorder_daemon.py", "w") as f:
    f.write(content)
print("recorder_daemon.py: changes applied")
PYEOF

# processor.py changes
python3 << 'PYEOF'
with open("processor.py", "r") as f:
    content = f.read()

old_block = '''        # min=1 (could be one remote speaker), max=num_speakers-1 (you are separate)
        remote_min = max(1, (min_speakers or 1) - 1) if (min_speakers or 1) > 1 else 1
        remote_max = max(1, (max_speakers or 8) - 1)
        logger.info(f"Diarizing remote channel (min={remote_min} max={remote_max} remote speakers)...")
        left_diarization = diarize(
            left_path, hf_token=hf_token,
            min_speakers=remote_min,
            max_speakers=remote_max,
        )'''

new_block = '''        p_count = len(meeting_meta.get("participants", []))
        remote_max = max(1, (max_speakers or p_count or 8) - 1)
        logger.info(f"Diarizing remote channel (max={remote_max} remote speakers)...")
        left_diarization = diarize(
            left_path, hf_token=hf_token,
            min_speakers=1,
            max_speakers=remote_max,
        )'''

content = content.replace(old_block, new_block)

with open("processor.py", "w") as f:
    f.write(content)
print("processor.py: changes applied")
PYEOF

echo "All changes applied successfully."
