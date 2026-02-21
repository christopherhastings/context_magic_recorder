"""
audio_router.py
Manages system audio routing for Safari Meet recordings.

The problem:
  Safari has no per-tab audio capture API. To record Meet in Safari,
  we need the system to route its audio through BlackHole so ffmpeg
  can capture it. But we don't want to capture all system audio — only
  the active meeting.

The solution:
  - Keep system audio output on your normal device (speakers/AirPods/Bose)
    at all times EXCEPT during an active Safari Meet call.
  - When a Safari meeting starts: switch system output to a Multi-Output
    Device that includes both your normal output AND BlackHole.
  - When the meeting ends: switch back to your normal output.
  - BlackHole only receives audio during the meeting window.

Zoom is NOT affected by this:
  Zoom's speaker output is set to BlackHole directly in Zoom's audio
  settings (app-level routing). System output is irrelevant for Zoom.

Chrome Meet is NOT affected by this:
  tabCapture captures the specific tab's audio stream directly.
  No system audio routing needed.

Requirements:
  brew install switchaudio-osx blackhole-2ch

.env variables:
  NORMAL_OUTPUT      Your usual output (e.g. "MacBook Pro Speakers")
                     Leave blank to auto-detect current output at startup
  MULTI_OUTPUT_NAME  Name of the Multi-Output Device you created in
                     Audio MIDI Setup (default: "Recorder Output")

Multi-Output Device setup (one-time, in Audio MIDI Setup):
  1. Click + → Create Multi-Output Device
  2. Tick: BlackHole 2ch + MacBook Pro Speakers + any headphones
  3. Rename it to: "Recorder Output"
  (AirPods/Bluetooth devices: connect them, they appear automatically)
"""

import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("audio_router")

MULTI_OUTPUT_NAME = os.getenv("MULTI_OUTPUT_NAME", "Recorder Output")
NORMAL_OUTPUT     = os.getenv("NORMAL_OUTPUT", "")  # auto-detect if blank

_current_normal_output: str | None = None   # captured at first use


def _run(cmd: list[str], check=False) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if check:
            r.check_returncode()
        return r.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.debug(f"Command failed {cmd}: {e}")
        return ""


def _switchaudiosource_available() -> bool:
    return bool(_run(["which", "switchaudiosource"]))


def get_current_output() -> str:
    """Get the current system audio output device name."""
    return _run(["switchaudiosource", "-c"])


def set_output(device_name: str) -> bool:
    """Switch system audio output to named device. Returns True on success."""
    result = _run(["switchaudiosource", "-s", device_name])
    # switchaudiosource exits 0 on success with no stdout
    # Verify it actually switched
    time.sleep(0.3)
    actual = get_current_output()
    success = device_name.lower() in actual.lower()
    if success:
        logger.info(f"Audio output → {device_name}")
    else:
        logger.warning(f"Failed to switch to '{device_name}' (got '{actual}')")
    return success


def list_outputs() -> list[str]:
    """List all available audio output devices."""
    out = _run(["switchaudiosource", "-a", "-t", "output"])
    return [line.strip() for line in out.splitlines() if line.strip()]


class AudioRouter:
    """
    Context-aware audio router.
    Call activate() before starting a Safari Meet recording.
    Call deactivate() when the recording stops.
    Thread-safe for single Safari session at a time.
    """

    def __init__(self):
        self._available   = _switchaudiosource_available()
        self._active       = False
        self._saved_output: str | None = None

        if not self._available:
            logger.warning(
                "switchaudiosource not found. Safari Meet will capture all system audio.\n"
                "Install with: brew install switchaudio-osx"
            )

    def activate(self) -> bool:
        """
        Switch system output to Multi-Output Device (includes BlackHole).
        Called when Safari Meet session starts.
        Returns True if routing was changed.
        """
        if not self._available or self._active:
            return False

        # Remember current output so we can restore it
        self._saved_output = NORMAL_OUTPUT or get_current_output()

        if not self._saved_output:
            logger.warning("Could not detect current audio output — routing not changed")
            return False

        # Verify the Multi-Output Device exists
        available = list_outputs()
        if not any(MULTI_OUTPUT_NAME.lower() in d.lower() for d in available):
            logger.warning(
                f"Multi-Output Device '{MULTI_OUTPUT_NAME}' not found.\n"
                f"Available outputs: {available}\n"
                f"Create it in Audio MIDI Setup (see SETUP.md).\n"
                f"Safari Meet will capture all system audio as fallback."
            )
            return False

        success = set_output(MULTI_OUTPUT_NAME)
        if success:
            self._active = True
            logger.info(f"Safari Meet routing active (saved: {self._saved_output})")
        return success

    def deactivate(self):
        """
        Restore original audio output.
        Called when Safari Meet session ends.
        """
        if not self._available or not self._active:
            return

        self._active = False
        if self._saved_output:
            set_output(self._saved_output)
            logger.info(f"Audio routing restored → {self._saved_output}")
            self._saved_output = None

    def __enter__(self):
        self.activate()
        return self

    def __exit__(self, *_):
        self.deactivate()

    @property
    def is_active(self) -> bool:
        return self._active


# Singleton — shared across the daemon
_router: AudioRouter | None = None

def get_router() -> AudioRouter:
    global _router
    if _router is None:
        _router = AudioRouter()
    return _router
