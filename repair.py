import os
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from processor import process_recording

# 1. Setup paths and env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

HF_TOKEN = os.getenv("HF_TOKEN")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Recordings")))

def repair_recordings():
    if not HF_TOKEN:
        print("❌ ERROR: HF_TOKEN not found in .env. Cannot diarize.")
        return

    # Find all .wav files in your recordings folder
    wav_files = list(OUTPUT_DIR.glob("*.wav"))
    
    if not wav_files:
        print(f"ℹ️ No .wav files found in {OUTPUT_DIR}")
        return

    print(f"--- Starting Repair on {len(wav_files)} files ---")

    for wav_path in wav_files:
        # Skip if we already have a .json for it (optional: remove this to force re-process)
        json_path = wav_path.with_suffix(".json")
        
        print(f"\n🎬 Processing: {wav_path.name}")
        
        # Create dummy metadata (since the daemon isn't providing it)
        meta = {
            "topic": wav_path.stem.split("_")[-1].replace("-", " "),
            "participants": [],
            "start_time": datetime.fromtimestamp(wav_path.stat().st_mtime).isoformat(),
        }

        try:
            # RUN THE PIPELINE
            process_recording(
                audio_path=wav_path,
                meeting_meta=meta,
                hf_token=HF_TOKEN,
                num_speakers=None,  # Let it auto-detect
            )
            print(f"✅ Success: {wav_path.name}")
        except Exception as e:
            print(f"❌ Failed: {wav_path.name}")
            print(f"   Error: {e}")

if __name__ == "__main__":
    repair_recordings()
