#!/usr/bin/env python3
"""
merge_recordings.py
Concat Zoom fragments in time order → one WAV per meeting → transcribe + diarize.

Auto: group by max gap (--max-gap, default 3 min). Explicit: --segment HH:MM-HH:MM per meeting (needs --date).

Cleans up by moving fragment .opus/.wav/.json/.md and *_left/right.wav to Recordings/_fragmented/
(unless --no-archive). New *_merged* outputs stay in Recordings.

Examples:
  python merge_recordings.py --date 2026-03-19 --segment 14:00-15:02 --segment 15:02-17:00 --dry-run
  python merge_recordings.py --date 2026-03-19 --max-gap 10   # looser auto grouping
"""

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))
HF_TOKEN = os.getenv("HF_TOKEN")
DEFAULT_MAX_GAP_MINUTES = 3  # Auto mode: gap between clips to stay in same group
MIN_GROUP_SIZE = 2   # Auto mode: only merge groups with 2+ recordings


def get_audio_duration_seconds(path: Path) -> float:
    """Get duration via ffprobe."""
    try:
        result = subprocess.run(
            ["/usr/local/bin/ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return 0.0


def parse_recording_path(path: Path) -> Optional[dict]:
    """
    Parse filename: 2026-03-18_08-59-48_zoom_Project_Nova_Standup.opus
    Returns {date, time, datetime, source, topic, path, duration} or None.
    """
    stem = path.stem
    # Remove _left/_right suffix from processor split; skip prior merge outputs
    if stem.endswith("_left") or stem.endswith("_right") or stem.endswith("_merged"):
        return None
    parts = stem.split("_")
    if len(parts) < 4:
        return None
    try:
        date_str = parts[0]   # 2026-03-18
        time_str = parts[1]   # 08-59-48
        source = parts[2]     # zoom
        topic = "_".join(parts[3:]) if len(parts) > 3 else "Meeting"
        dt = datetime.strptime(f"{date_str}_{time_str}", "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None

    # Prefer .opus (canonical), fall back to .wav
    base = path.with_suffix("")
    audio_path = base.with_suffix(".opus") if base.with_suffix(".opus").exists() else base.with_suffix(".wav")
    if not audio_path.exists():
        return None

    duration = get_audio_duration_seconds(audio_path)
    return {
        "date": date_str,
        "time": time_str,
        "datetime": dt,
        "source": source,
        "topic": topic,
        "path": audio_path,
        "duration": duration,
        "stem": stem,
    }


def collect_recordings(date_filter: Optional[str]) -> List[dict]:
    """Gather all zoom recordings, optionally filtered by date."""
    records = []
    seen_stems = set()
    for ext in ("*.opus", "*.wav"):
        for path in OUTPUT_DIR.glob(ext):
            if "_left" in path.stem or "_right" in path.stem:
                continue
            if date_filter and not path.name.startswith(date_filter):
                continue
            parsed = parse_recording_path(path)
            if parsed and parsed["stem"] not in seen_stems:
                seen_stems.add(parsed["stem"])
                records.append(parsed)
    return sorted(records, key=lambda r: r["datetime"])


def group_by_proximity(records: List[dict], max_gap_minutes: float) -> List[List[dict]]:
    """
    Group recordings that are within max_gap_minutes of each other.
    Uses end time of prev vs start time of next.
    """
    if not records:
        return []
    groups = []
    current = [records[0]]
    for rec in records[1:]:
        prev_end = current[-1]["datetime"].timestamp() + current[-1]["duration"]
        next_start = rec["datetime"].timestamp()
        gap_min = (next_start - prev_end) / 60
        if gap_min <= max_gap_minutes and rec["date"] == current[-1]["date"]:
            current.append(rec)
        else:
            if len(current) >= MIN_GROUP_SIZE:
                groups.append(current)
            current = [rec]
    if len(current) >= MIN_GROUP_SIZE:
        groups.append(current)
    return groups


def _parse_hhmm(s: str):
    parts = s.strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"Bad time (need HH:MM): {s!r}")
    return int(parts[0]), int(parts[1])


def groups_from_segments(
    records: List[dict], date_str: str, segment_specs: List[str]
) -> List[List[dict]]:
    """
    One merge group per --segment. segment_specs like '14:00-15:02' using the same
    calendar day as date_str (from filenames).
    """
    groups_out: List[List[dict]] = []
    for spec in segment_specs:
        spec = spec.strip()
        if "-" not in spec:
            raise ValueError(f"Segment must be HH:MM-HH:MM, got {spec!r}")
        a, b = spec.split("-", 1)
        h0, m0 = _parse_hhmm(a)
        h1, m1 = _parse_hhmm(b)
        start = datetime.strptime(
            f"{date_str} {h0:02d}:{m0:02d}:00", "%Y-%m-%d %H:%M:%S"
        )
        end = datetime.strptime(
            f"{date_str} {h1:02d}:{m1:02d}:00", "%Y-%m-%d %H:%M:%S"
        )
        if end <= start:
            raise ValueError(f"Segment end must be after start: {spec}")
        bucket = [r for r in records if start <= r["datetime"] < end]
        bucket.sort(key=lambda r: r["datetime"])
        if bucket:
            groups_out.append(bucket)
    return groups_out


def archive_stem_extras(stem: str, archive_dir: Path, skip_paths=None) -> None:
    """Move *stem*_left.wav / *stem*_right.wav from an old process_recording run."""
    skip_paths = skip_paths or set()
    for side in ("_left", "_right"):
        p = OUTPUT_DIR / f"{stem}{side}.wav"
        if p.exists() and p.resolve() not in skip_paths:
            dest = archive_dir / p.name
            if dest.exists():
                dest.unlink()
            p.rename(dest)


def merge_audio_files(paths: List[Path], out_path: Path) -> bool:
    """Concatenate audio files with ffmpeg. Normalizes to WAV 16kHz stereo for consistency."""
    if not paths:
        return False
    # Concat demuxer requires same codec; inputs may be opus or wav. Use filter_complex
    # to decode each, normalize to 16k stereo, then concat.
    inputs = []
    filter_parts = []
    for i, p in enumerate(paths):
        inputs.extend(["-i", str(p)])
        # Normalize: 16kHz stereo (aresample + aformat for mono→stereo if needed)
        filter_parts.append(f"[{i}:a]aresample=16000,aformat=channel_layouts=stereo[a{i}]")
    n = len(paths)
    concat_inputs = "".join(f"[a{i}]" for i in range(n))
    filter_complex = ";".join(filter_parts) + f";{concat_inputs}concat=n={n}:v=0:a=1[out]"

    try:
        subprocess.run(
            [
                "/usr/local/bin/ffmpeg", "-y",
                *inputs,
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-ar", "16000", "-ac", "2", "-c:a", "pcm_s16le",
                str(out_path), "-loglevel", "error",
            ],
            check=True, timeout=600,
        )
        return out_path.exists()
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Merge fragmented Zoom recordings")
    parser.add_argument("--date", help="Only process this date (YYYY-MM-DD)")
    parser.add_argument(
        "--segment",
        action="append",
        dest="segments",
        metavar="HH:MM-HH:MM",
        help="Meeting time window (repeat per meeting). Requires --date. Uses half-open [start,end).",
    )
    parser.add_argument(
        "--max-gap",
        type=float,
        default=float(os.getenv("MERGE_MAX_GAP_MINUTES", str(DEFAULT_MAX_GAP_MINUTES))),
        help=f"Auto mode: max minutes between clip end and next start (default {DEFAULT_MAX_GAP_MINUTES})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show plan only")
    parser.add_argument("--no-archive", action="store_true", help="Don't move originals")
    args = parser.parse_args()

    if not HF_TOKEN:
        print("❌ HF_TOKEN not set in .env. Diarization requires it.")
        return 1

    if args.segments and not args.date:
        print("❌ --segment requires --date YYYY-MM-DD")
        return 1

    records = collect_recordings(args.date)
    # Zoom-ish filenames only (skip meet-chrome etc.)
    records = [r for r in records if r.get("source") == "zoom"]

    if not records:
        print(f"No recordings found in {OUTPUT_DIR}" + (f" for {args.date}" if args.date else ""))
        return 0

    if args.segments:
        groups = groups_from_segments(records, args.date, args.segments)
        if not groups:
            print("No clips fell into the given --segment windows.")
            return 0
    else:
        groups = group_by_proximity(records, args.max_gap)
        if not groups:
            print(
                f"No fragmented groups found (all standalone or gaps > {args.max_gap} min). "
                "Try explicit --segment HH:MM-HH:MM per meeting."
            )
            return 0

    print(f"Found {len(groups)} group(s) to merge:\n")
    for i, group in enumerate(groups):
        total_dur = sum(r["duration"] for r in group)
        print(f"  Group {i+1}: {len(group)} recordings, ~{total_dur/60:.1f} min total")
        for r in group:
            print(f"    {r['datetime'].strftime('%H:%M:%S')}  {r['path'].name}  ({r['duration']:.0f}s)")
        # Pick best topic (prefer non-generic)
        topics = [r["topic"] for r in group]
        best_topic = next((t for t in topics if "zoom meeting" not in t.lower() and "meeting chat" not in t.lower()), topics[0])
        print(f"    → merge as: {group[0]['date']}_{group[0]['time']}_zoom_{best_topic}\n")

    if args.dry_run:
        print("Dry run — no changes made.")
        return 0

    from processor import process_recording

    archive_dir = OUTPUT_DIR / "_fragmented"
    if not args.no_archive:
        archive_dir.mkdir(exist_ok=True)

    for i, group in enumerate(groups):
        first = group[0]
        best_topic = next(
            (r["topic"] for r in group if "zoom meeting" not in r["topic"].lower() and "meeting chat" not in r["topic"].lower()),
            first["topic"],
        )
        safe_topic = "".join(c if c.isalnum() or c in " -_" else "-" for c in best_topic)[:50]
        merged_stem = f"{first['date']}_{first['time']}_zoom_{safe_topic}_merged"
        merged_wav = OUTPUT_DIR / f"{merged_stem}.wav"
        paths = [r["path"] for r in group]

        print(f"Merging group {i+1} → {merged_wav.name}...")
        if not merge_audio_files(paths, merged_wav):
            print(f"  ❌ ffmpeg merge failed")
            continue

        meta = {
            "topic": best_topic.replace("_", " "),
            "participants": [],
            "start_time": first["datetime"].isoformat(),
            "local_joined": first["datetime"].isoformat(),
            "local_left": None,  # Will be set from duration
        }
        last = group[-1]
        end_dt = last["datetime"].timestamp() + last["duration"]
        meta["local_left"] = datetime.fromtimestamp(end_dt).isoformat()

        try:
            process_recording(
                audio_path=merged_wav,
                meeting_meta=meta,
                hf_token=HF_TOKEN,
                num_speakers=None,
            )
            print(f"  ✅ Processed: {merged_stem}.json")
        except Exception as e:
            print(f"  ❌ Processing failed: {e}")
            continue

        if not args.no_archive:
            skip_merged = {merged_wav.resolve()}
            for r in group:
                for ext in (".opus", ".wav", ".json", ".md"):
                    p = r["path"].with_suffix(ext)
                    if p.exists() and p.resolve() not in skip_merged:
                        dest = archive_dir / p.name
                        if dest.exists():
                            dest.unlink()
                        p.rename(dest)
                        print(f"  Archived: {p.name}")
                archive_stem_extras(r["stem"], archive_dir, skip_paths=skip_merged)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    exit(main())
