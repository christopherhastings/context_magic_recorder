#!/usr/bin/env python3
"""
Re-run transcription + diarization on one recording (e.g. after changing WHISPER_VAD_FILTER).

  python reprocess_recording.py
  python reprocess_recording.py ~/Recordings/foo.opus
  python reprocess_recording.py a.opus b.opus

Uses .env: HF_TOKEN, OUTPUT_DIR, WHISPER_MODEL, WHISPER_VAD_* .
If no paths given, picks the newest .wav or .opus in OUTPUT_DIR (excluding *_left/_right).
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

from processor import process_recording  # noqa: E402


def _newest_audio(output_dir: Path) -> Optional[Path]:
    paths = []
    for ext in ("*.wav", "*.opus"):
        for p in output_dir.glob(ext):
            if "_left" in p.stem or "_right" in p.stem:
                continue
            paths.append(p)
    if not paths:
        return None
    return max(paths, key=lambda x: x.stat().st_mtime)


def _meta_from_json(audio: Path) -> dict:
    j = audio.with_suffix(".json")
    if j.exists():
        data = json.loads(j.read_text(encoding="utf-8"))
        meeting = dict(data.get("meeting") or {})
        if meeting:
            return meeting
    return {
        "topic": audio.stem,
        "participants": [],
        "start_time": datetime.fromtimestamp(audio.stat().st_mtime).isoformat(),
    }


def _process_one(path: Path, hf: str, whisper_model: str) -> bool:
    print(f"Processing: {path}")
    print(f"  WHISPER_VAD_FILTER={os.getenv('WHISPER_VAD_FILTER', '(off)')}")
    meta = _meta_from_json(path)
    try:
        process_recording(
            audio_path=path,
            meeting_meta=meta,
            hf_token=hf,
            whisper_model=whisper_model,
            num_speakers=None,
        )
    except Exception as e:
        print(f"❌ {path.name}: {e}")
        return False
    print(f"✅ Wrote {path.with_suffix('.json')}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Reprocess one or more recordings")
    parser.add_argument(
        "audio",
        nargs="*",
        help="Path(s) to .wav or .opus (default: newest in OUTPUT_DIR)",
    )
    args = parser.parse_args()

    hf = os.getenv("HF_TOKEN")
    if not hf:
        print("❌ HF_TOKEN missing in .env")
        return 1

    output_dir = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))
    if args.audio:
        paths = [Path(p).expanduser().resolve() for p in args.audio]
    else:
        one = _newest_audio(output_dir)
        if not one:
            print(f"❌ No .wav/.opus in {output_dir}")
            return 1
        print(f"Using newest: {one.name}")
        paths = [one]

    for path in paths:
        if not path.exists():
            print(f"❌ Not found: {path}")
            return 1

    whisper_model = os.getenv("WHISPER_MODEL", "medium.en")
    ok_all = True
    for path in paths:
        if not _process_one(path, hf, whisper_model):
            ok_all = False
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
