"""
archiver.py
Post-processing audio archival.

After transcription + diarization, we no longer need the large WAV file.
Convert to Opus in an OGG container at 32kbps — excellent voice quality,
tiny footprint.

Size comparison for a 1-hour call at 16kHz mono:
  WAV  (pcm_s16le):  ~115 MB   ← what we record
  MP3  (64kbps):     ~28 MB
  Opus (32kbps):     ~14 MB    ← what we archive  ✓
  Opus (24kbps):     ~10 MB    (still fine for voice, slightly less clear)

Opus at 32kbps is indistinguishable from the source for speech. It's the
codec used by WhatsApp, Discord, and Zoom themselves for voice.

The .opus file is the canonical long-term record. WAV is deleted after
archival succeeds. JSON + MD transcripts are kept indefinitely.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("archiver")


def archive_audio(wav_path: Path, bitrate_kbps: int = 32) -> Path | None:
    """
    Convert WAV → Opus OGG. Deletes WAV on success.
    Returns the .opus path, or None if conversion failed (WAV kept).
    """
    if not wav_path.exists():
        logger.warning(f"archive_audio: file not found: {wav_path}")
        return None

    opus_path = wav_path.with_suffix(".opus")

    logger.info(f"Archiving {wav_path.name} → {opus_path.name} ({bitrate_kbps}kbps Opus)...")

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-i", str(wav_path),
                "-c:a", "libopus",
                "-b:a", f"{bitrate_kbps}k",
                "-vbr", "on",              # variable bitrate — better quality at same size
                "-compression_level", "10",  # max compression (CPU cheap, ~same quality)
                "-application", "voip",    # optimised for voice
                str(opus_path),
                "-y",
                "-loglevel", "error",
            ],
            check=True,
            timeout=300,  # 5min max for a very long recording
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Opus conversion failed: {e}. WAV file kept.")
        return None
    except subprocess.TimeoutExpired:
        logger.error("Opus conversion timed out. WAV file kept.")
        return None

    # Verify the opus file looks reasonable
    if not opus_path.exists() or opus_path.stat().st_size < 1024:
        logger.error(f"Opus file missing or suspiciously small. WAV file kept.")
        opus_path.unlink(missing_ok=True)
        return None

    wav_mb  = wav_path.stat().st_size  / 1024 / 1024
    opus_mb = opus_path.stat().st_size / 1024 / 1024
    ratio   = (1 - opus_mb / wav_mb) * 100

    logger.info(f"  {wav_mb:.1f} MB → {opus_mb:.1f} MB ({ratio:.0f}% reduction)")

    # Delete WAV only after confirming Opus file is good
    wav_path.unlink()
    logger.info(f"  WAV deleted. Archive: {opus_path.name}")

    return opus_path
