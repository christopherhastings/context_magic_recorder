"""
zoom_detector.py
Detects active Zoom calls via window title monitoring.

Method:
  Check if zoom.us is running, then read all its window titles via
  AppleScript. During an active call Zoom has a window titled with
  the meeting topic (e.g. "Weekly Standup"). When idle it only has
  windows like "Zoom Workplace" or none visible at all.

  A debounce of 2 consecutive polls prevents false positives from
  transient windows (settings dialogs, chat, etc.).

Previous approach (lsof checking for BlackHole in open file handles)
stopped working on macOS Sonoma/Sequoia — audio device handles are
no longer visible in process file descriptors.

Requires Accessibility permission for osascript to read window titles.
"""

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger("zoom_detector")

# Window titles that mean Zoom is open but NOT in a call
_NON_CALL_TITLES = {
    "", "zoom", "zoom.us", "zoom workplace",
    "zoom workplace - free plan", "zoom workplace - licensed",
    "zoom - free account", "zoom - licensed",
}

# Substrings in a title that indicate a non-meeting window
_NON_CALL_SUBSTRINGS = [
    "settings",
    "preferences",
    "choose ",
    "select a ",
    "update ",
    "sign in",
    "sign up",
    "waiting room",
]


@dataclass
class ZoomCallInfo:
    in_call: bool = False
    topic: str = "Zoom Meeting"


class ZoomDetector:
    def __init__(self, blackhole_device: str = "BlackHole 2ch"):
        self._blackhole_device = blackhole_device   # kept for compat, not used for detection
        self._consecutive_hits = 0
        self._debounce = 2       # require N consecutive polls to confirm a call
        self._last_topic = "Zoom Meeting"

    def poll(self) -> ZoomCallInfo:
        detected, topic = self._check_zoom_windows()

        if detected:
            self._consecutive_hits += 1
            self._last_topic = topic
        else:
            self._consecutive_hits = 0

        in_call = self._consecutive_hits >= self._debounce
        return ZoomCallInfo(
            in_call=in_call,
            topic=self._last_topic if in_call else "Zoom Meeting",
        )

    def _check_zoom_windows(self) -> tuple[bool, str]:
        """Returns (likely_in_call, meeting_topic)."""
        # Is zoom.us even running?
        try:
            result = subprocess.run(
                ["pgrep", "-x", "zoom.us"],
                capture_output=True, text=True, timeout=3,
            )
            if not result.stdout.strip():
                return False, ""
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False, ""

        # Read all window titles
        titles = self._get_all_window_titles()
        if not titles:
            return False, ""

        # Find a title that looks like an active meeting
        has_missing_value = False
        for title in titles:
            normalized = title.lower().strip()

            if normalized == "missing value":
                # AppleScript nil — window exists but title unreadable
                has_missing_value = True
                continue

            if normalized in _NON_CALL_TITLES:
                continue

            if any(sub in normalized for sub in _NON_CALL_SUBSTRINGS):
                continue

            # This looks like a meeting window
            clean = title.replace(" - Zoom", "").strip()
            return True, clean or "Zoom Meeting"

        # If we only saw "missing value" windows (not idle ones), likely in a call
        if has_missing_value:
            return True, "Zoom Meeting"

        return False, ""

    def _get_all_window_titles(self) -> list[str]:
        """Get all zoom.us window titles via AppleScript."""
        try:
            # Return titles as newline-separated (comma in titles would break CSV)
            script = '''
                set output to ""
                tell application "System Events"
                    tell process "zoom.us"
                        repeat with w in windows
                            set output to output & (name of w as text) & linefeed
                        end repeat
                    end tell
                end tell
                return output
            '''
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []

            return [t.strip() for t in result.stdout.strip().splitlines() if t.strip()]

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
