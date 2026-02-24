"""
menubar.py
macOS menu bar app providing visual recording status.

States:
  ●  (green)   — daemon running, no active recording
  ⏺  (amber)   — recording in progress  [title updates with meeting name]
  ✕  (red)     — error condition

Uses rumps — a clean Python library for macOS menu bar apps.
Install: pip install rumps

Run this as a separate process. It communicates with the daemon
via a simple status file written to /tmp/recorder_status.json.
The daemon writes state; menubar.py reads it on a timer.

Why a status file rather than a socket?
- rumps runs its own runloop, which doesn't play nicely with asyncio
- A file is trivial to write from any thread in the daemon
- No coupling between the two processes
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import rumps

STATUS_FILE = Path("/tmp/recorder_status.json")
POLL_SECS   = 2

# ── Icons (using Unicode — no image assets needed) ────────────────────────────
ICON_IDLE     = "⬤"   # filled circle — green via title colour hack
ICON_RECORD   = "⏺"   # record symbol — orange
ICON_ERROR    = "⚠"   # warning

# macOS menu bar text colours via attributed strings aren't possible in rumps
# so we use distinct symbols + the title prefix to communicate state
STATE_IDLE    = {"title": "⬤",  "color": "idle"}
STATE_RECORD  = {"title": "⏺",  "color": "recording"}
STATE_ERROR   = {"title": "⚠",  "color": "error"}


class RecorderMenuBar(rumps.App):
    def __init__(self):
        super().__init__(
            name="Recorder",
            title=STATE_IDLE["title"],
            quit_button=None,
        )

        self._last_state  = None
        self._recording_since = None

        # Menu items
        self._status_item    = rumps.MenuItem("Status: Idle")
        self._meeting_item   = rumps.MenuItem("")
        self._duration_item  = rumps.MenuItem("")
        self._sep1           = rumps.separator
        self._open_folder    = rumps.MenuItem("Open Recordings Folder", callback=self._open_recordings)
        self._open_viewer    = rumps.MenuItem("Open Transcript Viewer", callback=self._open_viewer)
        self._sep2           = rumps.separator
        self._quit_item      = rumps.MenuItem("Quit Recorder", callback=self._quit)

        self.menu = [
            self._status_item,
            self._meeting_item,
            self._duration_item,
            rumps.separator,
            self._open_folder,
            self._open_viewer,
            rumps.separator,
            self._quit_item,
        ]

        # Hide meeting + duration items initially
        self._meeting_item.hide()
        self._duration_item.hide()

    @rumps.timer(POLL_SECS)
    def _poll_status(self, _):
        state = self._read_status()
        self._update_ui(state)

    def _read_status(self) -> dict:
        try:
            if STATUS_FILE.exists():
                data  = json.loads(STATUS_FILE.read_text())
                mtime = STATUS_FILE.stat().st_mtime
                data["_file_age"] = datetime.now().timestamp() - mtime
                return data
        except Exception:
            pass
        return {"state": "unknown"}

    def _update_ui(self, status: dict):
        state     = status.get("state", "unknown")
        meeting   = status.get("meeting_topic", "")
        source    = status.get("source", "")
        error     = status.get("error", "")
        since     = status.get("recording_since")
        file_age  = status.get("_file_age", 0)

        # If status file is stale (>30s old) and claims to be recording,
        # the daemon has likely crashed — show a warning
        if state == "recording" and file_age > 30:
            self.title = "⚠"
            self._status_item.title = "Daemon may have crashed"
            self._meeting_item.hide()
            self._duration_item.hide()
            return

        if state == "idle":
            self.title = "⬤"
            self._status_item.title = "Status: Ready"
            self._meeting_item.hide()
            self._duration_item.hide()
            self._last_state = state

        elif state == "recording":
            # Always compute elapsed so title stays live every 2 seconds
            elapsed_str = ""
            if since:
                try:
                    started = datetime.fromisoformat(since)
                    elapsed = datetime.now().astimezone() - started.astimezone()
                    mins    = int(elapsed.total_seconds() // 60)
                    secs    = int(elapsed.total_seconds() % 60)
                    elapsed_str = f" {mins}:{secs:02d}"
                    self._duration_item.title = f"  Duration: {mins}:{secs:02d}"
                    self._duration_item.show()
                except Exception:
                    self._duration_item.hide()

            # Title shows symbol + elapsed so it's always clear something is happening
            self.title = f"⏺{elapsed_str}"
            source_label = {"zoom": "Zoom", "chrome_meet": "Meet (Chrome)",
                            "safari_meet": "Meet (Safari)"}.get(source, source)
            self._status_item.title = f"Recording — {source_label}"
            if meeting:
                self._meeting_item.title = f"  {meeting}"
                self._meeting_item.show()
            self._last_state = state

        elif state == "processing":
            self.title = "◌"
            self._status_item.title = f"Processing: {meeting}"
            self._meeting_item.hide()
            self._duration_item.hide()
            self._last_state = state

        elif state == "error":
            if self._last_state != "error":
                self._notify_error(error or "Unknown error")
            self.title = "⚠"
            self._status_item.title = f"Error: {error or 'Unknown error'}"
            self._meeting_item.hide()
            self._duration_item.hide()
            self._last_state = "error"

        elif state == "selector_broken":
            self.title = "⚠"
            self._status_item.title = "Meet: Detection lost"
            if self._last_state != "selector_broken":
                self._notify(
                    "Google Meet detector needs attention",
                    "The Meet DOM selectors may have changed.",
                )
            self._last_state = "selector_broken"

        else:
            # State file missing or unknown — daemon not running
            if file_age > 60 or state == "unknown":
                self.title = "⬤"
                self._status_item.title = "Daemon not running"
            self._meeting_item.hide()
            self._duration_item.hide()

    def _notify(self, title: str, message: str):
        rumps.notification(
            title=title,
            subtitle="",
            message=message,
            sound=False,
        )

    def _notify_error(self, message: str):
        rumps.notification(
            title="Recorder Error",
            subtitle="",
            message=message,
            sound=True,
        )

    def _open_recordings(self, _):
        recordings_dir = os.path.expanduser(
            os.getenv("OUTPUT_DIR", "~/Recordings")
        )
        subprocess.run(["open", recordings_dir])

    def _open_viewer(self, _):
        # Opens the transcript viewer in the default browser
        subprocess.run(["open", "http://localhost:8766"])

    def _quit(self, _):
        rumps.quit_application()


def run():
    RecorderMenuBar().run()


if __name__ == "__main__":
    run()
