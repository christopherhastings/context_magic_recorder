"""
permissions.py
Checks required macOS permissions at daemon startup and surfaces
clear, actionable warnings when they're missing.

Checks:
  1. Accessibility — needed for osascript to read Zoom window titles
  2. Notifications  — needed for osascript display notification calls

Both are optional in the sense that recording still works without them,
but the user should know what's degraded.
"""

import logging
import subprocess
import sys

logger = logging.getLogger("permissions")


# ── Accessibility ──────────────────────────────────────────────────────────────

def check_accessibility() -> bool:
    """
    Returns True if the current process has Accessibility permission.

    Uses AXIsProcessTrusted() via a tiny Python+ctypes call — more
    reliable than osascript self-test because it directly queries the
    macOS API rather than trying an action that might fail silently.
    """
    try:
        import ctypes
        import ctypes.util

        appkit = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("ApplicationServices")
        )
        appkit.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(appkit.AXIsProcessTrusted())
    except Exception:
        # Fall back to osascript probe if ctypes approach fails
        return _check_accessibility_osascript()


def _check_accessibility_osascript() -> bool:
    """Probe by attempting to read System Events — returns False if blocked."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to return name of first process'],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def request_accessibility():
    """
    Prompt macOS to show the Accessibility permission dialog for
    the current application. The user still has to click Allow —
    this just opens the right pane.
    """
    try:
        import ctypes
        import ctypes.util
        appkit = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("ApplicationServices")
        )
        # Passing True triggers the system prompt
        appkit.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        options = ctypes.c_void_p(
            # kAXTrustedCheckOptionPrompt = True
            # We pass a CFDictionary — easiest via osascript
            None
        )
        appkit.AXIsProcessTrustedWithOptions(options)
    except Exception:
        pass

    # Regardless, open the Privacy pane directly
    subprocess.run([
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    ])


# ── Notifications ──────────────────────────────────────────────────────────────

def check_notifications() -> bool:
    """
    Test whether osascript can deliver a notification.
    Sends a silent test — if it errors, notifications are blocked.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'display notification "Recorder daemon started" '
             'with title "Recorder" subtitle "Ready to record meetings"'],
            capture_output=True, text=True, timeout=5,
        )
        # osascript exits non-zero if notifications are blocked
        return result.returncode == 0
    except Exception:
        return False


def open_notification_settings():
    subprocess.run([
        "open",
        "x-apple.systempreferences:com.apple.preference.notifications"
    ])


# ── Combined startup check ────────────────────────────────────────────────────

def run_checks(notify_fn=None) -> dict[str, bool]:
    """
    Run all permission checks. Logs clear warnings for failures.
    Optionally calls notify_fn(title, message) for menu bar notifications.

    Returns dict of {check_name: passed}
    """
    results = {}

    # ── Accessibility ──────────────────────────────────────────────────────
    logger.info("Checking Accessibility permission...")
    has_accessibility = check_accessibility()
    results["accessibility"] = has_accessibility

    if has_accessibility:
        logger.info("  ✓ Accessibility — Zoom window titles will be read")
    else:
        logger.warning(
            "  ✗ Accessibility permission not granted.\n"
            "\n"
            "  Impact: Zoom meeting titles will show as 'Zoom Meeting'\n"
            "  instead of the actual meeting name. Recording still works.\n"
            "\n"
            "  To fix:\n"
            "  1. System Settings → Privacy & Security → Accessibility\n"
            "  2. Click + and add Terminal (or whichever app runs this daemon)\n"
            "  3. Restart the daemon\n"
            "\n"
            "  Opening Privacy & Security now..."
        )
        request_accessibility()

        if notify_fn:
            notify_fn(
                "Accessibility permission needed",
                "Zoom meeting titles won't be captured. "
                "Grant access in Privacy & Security → Accessibility.",
            )

    # ── Notifications ──────────────────────────────────────────────────────
    logger.info("Checking Notifications permission...")
    has_notifications = check_notifications()
    results["notifications"] = has_notifications

    if has_notifications:
        logger.info("  ✓ Notifications — startup notification sent")
    else:
        logger.warning(
            "  ✗ Notifications are blocked.\n"
            "\n"
            "  Impact: No alerts when recordings start, finish, or error.\n"
            "  The menu bar icon and log file still reflect all state changes.\n"
            "\n"
            "  To fix:\n"
            "  1. System Settings → Notifications\n"
            "  2. Find Terminal (or Script Editor) in the list\n"
            "  3. Enable 'Allow notifications'\n"
            "\n"
            "  Opening Notification settings now..."
        )
        open_notification_settings()

    return results
