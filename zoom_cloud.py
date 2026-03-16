"""
zoom_cloud.py
Fetches rich meeting metadata from Zoom Cloud API post-meeting.

Setup:
1. Go to https://marketplace.zoom.us → Build App → Server-to-Server OAuth
2. Add scopes: meeting:read:admin, report:read:admin, user:read:admin
3. Activate app, copy Account ID, Client ID, Client Secret → .env
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ZOOM_ACCOUNT_ID  = os.getenv("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID   = os.getenv("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET")

TOKEN_URL = "https://zoom.us/oauth/token"
API_BASE  = "https://api.zoom.us/v2"

_token_cache: dict = {"token": None, "expires_at": 0}


def _get_access_token() -> str:
    """Fetch or return cached Server-to-Server OAuth token."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    resp = requests.post(
        TOKEN_URL,
        params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
        auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data["expires_in"]
    return _token_cache["token"]


def _get(path: str, params: dict = None) -> dict:
    token = _get_access_token()
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_meeting_details(meeting_id: str) -> dict:
    """
    Get rich meeting metadata. meeting_id is the numeric ID from local API
    (strip spaces first).
    
    Returns a merged dict with the most useful fields.
    """
    clean_id = meeting_id.replace(" ", "").replace("-", "")
    
    result = {
        "meeting_id": clean_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # ── 1. Past meeting details ──────────────────────────────────────────────
    # Contains: topic, start_time, end_time, duration, timezone, agenda,
    #           host email, type, total_participants (sometimes)
    try:
        details = _get(f"/past_meetings/{clean_id}")
        result.update({
            "topic":            details.get("topic"),
            "agenda":           details.get("agenda"),           # meeting description
            "start_time":       details.get("start_time"),
            "end_time":         details.get("end_time"),
            "duration_minutes": details.get("duration"),
            "timezone":         details.get("timezone"),
            "host_id":          details.get("host_id"),
            "host_email":       details.get("host_email"),
            "meeting_type":     details.get("type"),             # 1=instant,2=scheduled,3=recurring
            "total_minutes":    details.get("total_minutes"),
            "participants_count": details.get("participants_count"),
            "dept":             details.get("dept"),
            "uuid":             details.get("uuid"),
        })
    except Exception as e:
        logger.warning(f"Could not fetch past meeting details: {e}")

    # ── 2. Participant report ────────────────────────────────────────────────
    # Contains per-participant: name, email, join_time, leave_time,
    #                           duration, attentiveness_score (if enabled),
    #                           user_email, registrant_id
    try:
        participants = []
        page_token = None
        while True:
            params = {"page_size": 300}
            if page_token:
                params["next_page_token"] = page_token
            data = _get(f"/report/meetings/{clean_id}/participants", params)
            participants.extend(data.get("participants", []))
            page_token = data.get("next_page_token")
            if not page_token:
                break

        result["participants"] = [
            {
                "name":         p.get("name"),
                "email":        p.get("user_email") or p.get("email"),
                "join_time":    p.get("join_time"),
                "leave_time":   p.get("leave_time"),
                "duration_sec": p.get("duration"),
                "attentiveness_score": p.get("attentiveness_score"),
                "registrant_id": p.get("registrant_id"),
            }
            for p in participants
        ]
    except Exception as e:
        logger.warning(f"Could not fetch participant report: {e}")
        result["participants"] = []

    # ── 3. Host user details ─────────────────────────────────────────────────
    # Useful to get host's full name, department, job title
    try:
        host_id = result.get("host_id")
        if host_id:
            user = _get(f"/users/{host_id}")
            result["host"] = {
                "name":       f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
                "email":      user.get("email"),
                "dept":       user.get("dept"),
                "job_title":  user.get("job_title"),
                "timezone":   user.get("timezone"),
            }
    except Exception as e:
        logger.warning(f"Could not fetch host details: {e}")

    # ── 4. Meeting recordings (if cloud recording enabled) ───────────────────
    # Useful to cross-reference or grab Zoom's own transcript if available
    try:
        uuid = result.get("uuid")
        if uuid:
            import urllib.parse
            encoded_uuid = urllib.parse.quote(urllib.parse.quote(uuid, safe=""))
            recordings = _get(f"/meetings/{encoded_uuid}/recordings")
            result["cloud_recordings"] = [
                {
                    "type":         r.get("recording_type"),
                    "start_time":   r.get("recording_start"),
                    "end_time":     r.get("recording_end"),
                    "file_size":    r.get("file_size"),
                    "download_url": r.get("download_url"),
                    "play_url":     r.get("play_url"),
                }
                for r in recordings.get("recording_files", [])
            ]
    except Exception as e:
        logger.warning(f"Could not fetch cloud recordings: {e}")
        result["cloud_recordings"] = []

    return result


def wait_and_fetch(meeting_id: str, max_wait_seconds: int = 300, poll_interval: int = 30) -> Optional[dict]:
    """
    Poll for meeting data — Zoom's report API has a delay of ~2-5 minutes
    after a meeting ends before data is available.
    """
    logger.info(f"Waiting for Zoom Cloud API data (meeting {meeting_id})...")
    deadline = time.time() + max_wait_seconds
    
    while time.time() < deadline:
        try:
            data = fetch_meeting_details(meeting_id)
            if data.get("start_time"):  # data is ready
                logger.info("✅ Cloud meeting data fetched successfully")
                return data
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.debug(f"Meeting not yet in API, waiting {poll_interval}s...")
            else:
                logger.warning(f"HTTP error fetching meeting data: {e}")
        except Exception as e:
            logger.warning(f"Error fetching meeting data: {e}")
        
        time.sleep(poll_interval)
    
    logger.error("Timed out waiting for Zoom Cloud API data")
    return None
