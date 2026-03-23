#!/usr/bin/env python3
"""
merge_transcripts.py
Merge existing .json transcripts for a time window — no audio concat.

Each fragment's turns are time-shifted by the cumulative duration of prior fragments
(based on max(turn.end) or audio file length if empty). Real-world gaps between
clips are *not* preserved in timestamps; the merged timeline is contiguous.

Example (Mar 19 afternoon, local filename times ≈ 2pm–3:01pm):
  python merge_transcripts.py --date 2026-03-19 --start 14:00 --end 15:02 \\
    --topic "Mar 19 early afternoon (merged)"

Writes:  {OUTPUT_DIR}/{date}_{first_clip_time}_zoom_{slug}_transcript_merged.json + .md
"""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))


def _ffprobe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            [
                "/usr/local/bin/ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return 0.0


def _parse_json_stem_datetime(stem: str) -> Optional[datetime]:
    if stem.endswith("_left") or stem.endswith("_right"):
        return None
    parts = stem.split("_")
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def _segment_duration_from_data(json_path: Path, data: dict) -> float:
    turns = data.get("transcript", {}).get("turns") or []
    if turns:
        return max(float(t["end"]) for t in turns)
    base = json_path.with_suffix("")
    for ext in (".opus", ".wav"):
        p = base.with_suffix(ext)
        if p.exists():
            return _ffprobe_duration(p)
    return 0.0


def _collect_json_paths(date_str: str) -> List[Path]:
    paths = []
    for p in sorted(OUTPUT_DIR.glob(f"{date_str}_*.json")):
        if p.name.endswith(".meta.json"):
            continue
        if "transcript_merged" in p.stem:
            continue
        dt = _parse_json_stem_datetime(p.stem)
        if dt is None:
            continue
        parts = p.stem.split("_")
        if len(parts) < 3 or parts[2] != "zoom":
            continue
        paths.append(p)
    return sorted(paths, key=lambda x: _parse_json_stem_datetime(x.stem) or datetime.min)


def _in_window(dt: datetime, start: datetime, end: datetime) -> bool:
    return start <= dt < end


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge transcript JSONs in a time window")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (filename prefix)")
    ap.add_argument("--start", default="14:00", help="HH:MM start (inclusive)")
    ap.add_argument("--end", default="15:02", help="HH:MM end (exclusive), e.g. 15:02 ≈ before 3:02pm")
    ap.add_argument("--topic", default="", help="Meeting title in output (optional)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    h0, m0 = [int(x) for x in args.start.split(":")[:2]]
    h1, m1 = [int(x) for x in args.end.split(":")[:2]]
    win_start = datetime.strptime(f"{args.date} {h0:02d}:{m0:02d}:00", "%Y-%m-%d %H:%M:%S")
    win_end = datetime.strptime(f"{args.date} {h1:02d}:{m1:02d}:00", "%Y-%m-%d %H:%M:%S")
    if win_end <= win_start:
        print("❌ --end must be after --start")
        return 1

    all_json = _collect_json_paths(args.date)
    selected = []
    for jp in all_json:
        dt = _parse_json_stem_datetime(jp.stem)
        if dt and _in_window(dt, win_start, win_end):
            selected.append(jp)

    if not selected:
        print(f"No JSON transcripts in [{args.start}, {args.end}) for {args.date}")
        return 0

    print(f"Selected {len(selected)} transcript(s):")
    for jp in selected:
        print(f"  {jp.name}")

    merged_turns = []  # dicts
    sources = []
    offset = 0.0

    for jp in selected:
        data = json.loads(jp.read_text(encoding="utf-8"))
        turns = data.get("transcript", {}).get("turns") or []
        dur = _segment_duration_from_data(jp, data)
        sources.append({"file": jp.name, "offset_seconds": round(offset, 3), "segment_duration": round(dur, 3)})
        for t in turns:
            merged_turns.append({
                "speaker": t.get("speaker", "Speaker"),
                "start": round(float(t["start"]) + offset, 3),
                "end": round(float(t["end"]) + offset, 3),
                "text": (t.get("text") or "").strip(),
            })
        offset += dur

    merged_turns = [t for t in merged_turns if t["text"]]

    topic = (args.topic or "").strip()
    if not topic:
        topic = f"Merged transcript {args.date} {args.start}-{args.end}"

    first_stem = selected[0].stem
    parts = first_stem.split("_")
    first_time = parts[1] if len(parts) > 1 else "00-00-00"
    slug = "".join(c if c.isalnum() else "_" for c in topic).strip("_")[:50] or "merged"
    out_stem = f"{args.date}_{first_time}_zoom_{slug}_transcript_merged"
    out_json = OUTPUT_DIR / f"{out_stem}.json"
    out_md = OUTPUT_DIR / f"{out_stem}.md"

    meeting_meta = {
        "topic": topic,
        "start_time": win_start.isoformat(),
        "local_joined": win_start.isoformat(),
        "local_left": win_end.isoformat(),
        "merged_transcript_sources": sources,
        "merged_transcript_note": "Timestamps are contiguous; real-world gaps between recorder fragments are not shown.",
    }

    out_data = {
        "schema_version": "1.1",
        "recording": {
            "file": str(out_json),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        },
        "meeting": meeting_meta,
        "speakers": sorted({t["speaker"] for t in merged_turns}),
        "transcript": {"turns": merged_turns},
        "diarization_segments": [],
    }

    if args.dry_run:
        print(f"\nDry run → would write {out_json.name} ({len(merged_turns)} turns)")
        return 0

    out_json.write_text(json.dumps(out_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Markdown (simple; avoid importing SpeakerTurn)
    lines = [f"# {topic}", "", f"**{meeting_meta['start_time']}** — *merged from {len(selected)} fragment(s)*", "", "## Transcript", ""]
    prev_spk = None
    for t in merged_turns:
        m, s = divmod(int(t["start"]), 60)
        h = m // 60
        m = m % 60
        ts = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        if t["speaker"] != prev_spk:
            lines.append(f"**{t['speaker']}** `[{ts}]`")
            prev_spk = t["speaker"]
        lines.append(f"> {t['text']}")
        lines.append("")
    out_md.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n✅ Wrote {out_json.name} ({len(merged_turns)} turns)")
    print(f"✅ Wrote {out_md.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
