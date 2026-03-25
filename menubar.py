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

try:
    import rumps
except ImportError:
    sys.stderr.write(
        "menubar.py: missing dependency `rumps`.\n"
        "  cd ~/recorder && source venv/bin/activate && pip install -r requirements.txt\n"
        "Then: launchctl unload ~/Library/LaunchAgents/com.recorder.menubar.plist\n"
        "      launchctl load  ~/Library/LaunchAgents/com.recorder.menubar.plist\n"
    )
    raise SystemExit(1)


def _accessory_mode_for_menu_bar_only():
    """
    Without this, a process started by launchd may register no visible status item.
    NSApplicationActivationPolicyAccessory = menu bar + no Dock icon.
    """
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception as exc:
        sys.stderr.write(f"[menubar] accessory mode (non-fatal): {exc}\n")

STATUS_FILE = Path("/tmp/recorder_status.json")
POLL_SECS   = 1

# ── Icons (using Unicode — no image assets needed) ────────────────────────────
ICON_IDLE     = "⬤"   # filled circle — green via title colour hack
ICON_RECORD   = "⏺"   # record symbol — orange
ICON_ERROR    = "⚠"   # warning

# macOS menu bar text colours via attributed strings aren't possible in rumps
# so we use distinct symbols + the title prefix to communicate state
STATE_IDLE    = {"title": "⬤",  "color": "idle"}
STATE_RECORD  = {"title": "⏺",  "color": "recording"}
STATE_ERROR   = {"title": "⚠",  "color": "error"}


import requests

API_BASE_URL = "http://localhost:8766/api"


class RecorderMenuBar(rumps.App):
    def __init__(self):
        super().__init__(
            name="Recorder",
            title=STATE_IDLE["title"],
            quit_button=None,
        )

        self._last_state  = None
        self._last_source = None

        # Menu items
        self._status_item    = rumps.MenuItem("Status: Idle")
        self._meeting_item   = rumps.MenuItem("")
        self._duration_item  = rumps.MenuItem("")
        self._sep_manual     = rumps.separator
        self._start_rec_item = rumps.MenuItem("Start Manual Recording", callback=self._start_recording)
        self._split_rec_item = rumps.MenuItem("Split Recording Segment", callback=self._split_recording)
        self._stop_rec_item  = rumps.MenuItem("Stop Manual Recording", callback=self._stop_recording)
        self._sep_actions    = rumps.separator
        self._open_folder    = rumps.MenuItem("Open Recordings Folder", callback=self._open_recordings)
        self._open_viewer    = rumps.MenuItem("Open Transcript Viewer", callback=self._open_viewer)
        self._sep_quit       = rumps.separator
        self._quit_item      = rumps.MenuItem("Quit Recorder", callback=self._quit)

        self.menu = [
            self._status_item,
            self._meeting_item,
            self._duration_item,
            self._sep_manual,
            self._start_rec_item,
            self._split_rec_item,
            self._stop_rec_item,
            self._sep_actions,
            self._open_folder,
            self._open_viewer,
            self._sep_quit,
            self._quit_item,
        ]

        # Hide items initially
        self._meeting_item.hide()
        self._duration_item.hide()
        self._split_rec_item.hide()
        self._stop_rec_item.hide()

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

        # Daemon updates status file every 5s during recording.
        # If file is older than 15s and claims recording, daemon is likely dead.
        if state == "recording" and file_age > 15:
            self.title = "⚠"
            self._status_item.title = "Daemon not responding"
            self._meeting_item.hide()
            self._duration_item.hide()
            self._show_manual_controls(is_recording=False)
            return

        # Show/hide manual controls based on state
        is_manual = source == "zoom_manual"
        self._show_manual_controls(is_recording=(state == "recording"), is_manual_source=is_manual)

        if state == "idle":
            self.title = "⬤"
            self._status_item.title = "Status: Ready"
            self._meeting_item.hide()
            self._duration_item.hide()

        elif state == "recording":
            # Keep menubar title compact — just the icon, no timer text
            # Timer goes in the dropdown menu instead
            self.title = "⏺"
            if since:
                try:
                    started = datetime.fromisoformat(since)
                    elapsed = datetime.now().astimezone() - started.astimezone()
                    mins, secs = divmod(int(elapsed.total_seconds()), 60)
                    self._duration_item.title = f"  Duration: {mins}:{secs:02d}"
                    self._duration_item.show()
                except Exception:
                    self._duration_item.hide()
            else:
                self._duration_item.hide()

            source_labels = {
                "zoom": "Zoom (auto)",
                "zoom_manual": "Zoom (manual)",
                "chrome_meet": "Meet (Chrome)",
                "safari_meet": "Meet (Safari)",
            }
            self._status_item.title = f"Recording: {source_labels.get(source, source)}"
            if meeting:
                self._meeting_item.title = f"  {meeting}"
                self._meeting_item.show()
            else:
                self._meeting_item.hide()

        elif state == "processing":
            self.title = "◌"
            self._status_item.title = f"Processing: {meeting}"
            self._meeting_item.hide()
            self._duration_item.hide()

        elif state == "error":
            if self._last_state != "error":
                self._notify_error(error or "Unknown error")
            self.title = "⚠"
            self._status_item.title = f"Error: {error or 'Unknown error'}"
            self._meeting_item.hide()
            self._duration_item.hide()

        elif state == "selector_broken":
            self.title = "⚠"
            self._status_item.title = "Meet: Detection lost"
            if self._last_state != "selector_broken":
                self._notify(
                    "Google Meet detector needs attention",
                    "The Meet DOM selectors may have changed.",
                )

        else:
            if file_age > 60 or state == "unknown":
                self.title = "⬤"
                self._status_item.title = "Daemon not running"
            self._meeting_item.hide()
            self._duration_item.hide()

        self._last_state = state
        self._last_source = source

    def _show_manual_controls(self, is_recording: bool, is_manual_source: bool):
        if is_recording:
            # When any recording is happening, hide "Start"
            self._start_rec_item.hide()
            # Only show Split/Stop for *manual* recordings
            if is_manual_source:
                self._split_rec_item.show()
                self._stop_rec_item.show()
            else:
                self._split_rec_item.hide()
                self._stop_rec_item.hide()
        else:
            # When idle, can always start a new manual recording
            self._start_rec_item.show()
            self._split_rec_item.hide()
            self._stop_rec_item.hide()

    def _notify(self, title: str, message: str):
        rumps.notification(title=title, subtitle="", message=message, sound=False)

    def _notify_error(self, message: str):
        rumps.notification(title="Recorder Error", subtitle="", message=message, sound=True)

    def _start_recording(self, _):
        try:
            # Ask for a topic
            resp = rumps.Window(
                "Start Recording",
                "Enter a topic for this recording:",
                default_text="My Meeting",
                ok="Start",
                cancel=True,
            ).run()
            if resp.clicked:
                requests.post(f"{API_BASE_URL}/actions/start", json={"topic": resp.text}, timeout=5)
                self._notify("Recording starting...", resp.text)
        except requests.RequestException as e:
            self._notify_error(f"Failed to start: {e}")

    def _stop_recording(self, _):
        try:
            requests.post(f"{API_BASE_URL}/actions/stop", timeout=5)
            self._notify("Recording stopping...", "")
        except requests.RequestException as e:
            self._notify_error(f"Failed to stop: {e}")

    def _split_recording(self, _):
        try:
            resp = rumps.Window(
                "Split Recording",
                "Enter a topic for the NEW segment:",
                default_text="New Segment",
                ok="Split",
                cancel=True,
            ).run()
            if resp.clicked:
                requests.post(f"{API_BASE_URL}/actions/split", json={"topic": resp.text}, timeout=5)
                self._notify("Recording splitting...", f"New segment: {resp.text}")
        except requests.RequestException as e:
            self._notify_error(f"Failed to split: {e}")

    def _open_recordings(self, _):
        recordings_dir = os.path.expanduser(os.getenv("OUTPUT_DIR", "~/Recordings"))
        subprocess.run(["open", recordings_dir])

    def _open_viewer(self, _):
        subprocess.run(["open", "http://localhost:8766"])

    def _quit(self, _):
        rumps.quit_application()


def _append_boot_log(line: str) -> None:
    try:
        with open("/tmp/recorder_menubar.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {line}\n")
    except OSError:
        pass


def run():
    _accessory_mode_for_menu_bar_only()
    _append_boot_log("Recorder menubar starting (rumps)")
    RecorderMenuBar().run()


if __name__ == "__main__":
    run()
