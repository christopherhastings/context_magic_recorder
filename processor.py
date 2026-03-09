"""
processor.py
Post-meeting pipeline.

Stereo recordings (Zoom):
  L channel = BlackHole = remote participants (may be multiple speakers)
  R channel = microphone = you (single known speaker)

  Pipeline:
    Left  → Whisper transcription + pyannote diarization → SPEAKER_00, SPEAKER_01...
    Right → Whisper transcription only                   → "You"
    Merge → interleave both channels by timestamp

Mono recordings (Meet via Chrome tabCapture or Safari system audio):
  Single channel → Whisper + pyannote diarization across all speakers

Output: .json + .md per recording
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Word:
    start: float
    end:   float
    text:  str
    probability: float = 0.0

@dataclass
class SpeakerTurn:
    speaker: str
    start:   float
    end:     float
    words:   list = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text.strip() for w in self.words)


# ── Audio utilities ───────────────────────────────────────────────────────────

def is_stereo(audio_path: Path) -> bool:
    """Return True if the file has 2 channels."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(audio_path)],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() == "2"
    except Exception:
        return False


def split_stereo(audio_path: Path) -> tuple[Path, Path]:
    """
    Split a stereo WAV into two mono WAVs.
    Returns (left_path, right_path).
    Left  = remote participants (BlackHole)
    Right = local microphone (you)
    """
    base  = audio_path.with_suffix("")
    left  = Path(f"{base}_left.wav")
    right = Path(f"{base}_right.wav")

    subprocess.run([
        "ffmpeg", "-i", str(audio_path),
        "-af", "pan=mono|c0=c0",
        str(left), "-y", "-loglevel", "error",
    ], check=True)

    subprocess.run([
        "ffmpeg", "-i", str(audio_path),
        "-af", "pan=mono|c0=c1",
        str(right), "-y", "-loglevel", "error",
    ], check=True)

    logger.info(f"Split stereo: L={left.name} R={right.name}")
    return left, right


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe(audio_path: Path, model_size: str = "medium.en") -> list[Word]:
    from faster_whisper import WhisperModel

    logger.info(f"Loading Whisper model '{model_size}'...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    logger.info(f"Transcribing {audio_path.name}...")
    segments, info = model.transcribe(
        str(audio_path),
        language="en",
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    words = []
    for segment in segments:
        if segment.words:
            for w in segment.words:
                words.append(Word(
                    start=w.start, end=w.end,
                    text=w.word, probability=w.probability,
                ))

    logger.info(f"Transcription complete: {len(words)} words, {info.duration:.1f}s audio")
    return words


# ── Diarization ───────────────────────────────────────────────────────────────

def diarize(audio_path: Path, hf_token: str,
            min_speakers: int = 1,
            max_speakers: Optional[int] = None) -> list[dict]:
    """
    Run pyannote speaker diarization with automatic speaker count detection.

    min_speakers: floor — never report fewer than this many speakers (default 1)
    max_speakers: ceiling — never report more than this (None = fully automatic)

    Passing min=1, max=None lets pyannote decide entirely. Passing max=3 on
    the remote channel of a stereo recording bounds the search without
    forcing a wrong answer on short or single-speaker segments.
    """
    from pyannote.audio import Pipeline
    import torch

    logger.info("Loading pyannote diarization pipeline...")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )

    if torch.backends.mps.is_available():
        pipeline.to(torch.device("mps"))
        logger.info("Using Apple MPS (GPU) for diarization")

    # Build kwargs — only pass bounds that are set
    kwargs = {}
    if min_speakers is not None: kwargs["min_speakers"] = min_speakers
    if max_speakers is not None: kwargs["max_speakers"] = max_speakers

    bounds = f"min={min_speakers}" + (f" max={max_speakers}" if max_speakers else " max=auto")
    logger.info(f"Diarizing {audio_path.name} ({bounds})...")
    diarization = pipeline(str(audio_path), **kwargs)

    segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segments.append({
            "speaker": speaker,
            "start":   turn.start,
            "end":     turn.end,
        })

    n_speakers = len(set(s["speaker"] for s in segments))
    logger.info(f"Diarization complete: {n_speakers} speakers detected, {len(segments)} segments")
    return segments


# ── Merge helpers ─────────────────────────────────────────────────────────────

def words_to_turns_with_diarization(words: list[Word],
                                    diarization: list[dict]) -> list[SpeakerTurn]:
    """Assign each word to a speaker via diarization, collapse into turns."""

    def speaker_at(t: float) -> str:
        best, best_overlap = None, 0.0
        for seg in diarization:
            overlap = min(t, seg["end"]) - max(t, seg["start"])
            if overlap > best_overlap:
                best_overlap = overlap
                best = seg["speaker"]
        return best or "UNKNOWN"

    labeled = [(speaker_at((w.start + w.end) / 2), w) for w in words]

    turns: list[SpeakerTurn] = []
    if not labeled:
        return turns

    cur_spk, first_word = labeled[0]
    cur_turn = SpeakerTurn(speaker=cur_spk, start=first_word.start,
                           end=first_word.end, words=[first_word])

    for spk, word in labeled[1:]:
        if spk == cur_spk:
            cur_turn.words.append(word)
            cur_turn.end = word.end
        else:
            turns.append(cur_turn)
            cur_turn = SpeakerTurn(speaker=spk, start=word.start,
                                   end=word.end, words=[word])
            cur_spk = spk

    turns.append(cur_turn)
    return turns


def words_to_turns_single_speaker(words: list[Word],
                                  speaker: str) -> list[SpeakerTurn]:
    """Collapse all words into turns for a single known speaker."""
    if not words:
        return []

    GAP = 1.5  # seconds — gap larger than this starts a new turn
    cur = SpeakerTurn(speaker=speaker, start=words[0].start,
                      end=words[0].end, words=[words[0]])
    turns = []

    for word in words[1:]:
        if word.start - cur.end > GAP:
            turns.append(cur)
            cur = SpeakerTurn(speaker=speaker, start=word.start,
                              end=word.end, words=[word])
        else:
            cur.words.append(word)
            cur.end = word.end

    turns.append(cur)
    return turns


def merge_turns(*turn_lists: list[SpeakerTurn]) -> list[SpeakerTurn]:
    """Interleave multiple turn lists sorted by start time."""
    all_turns = [t for turns in turn_lists for t in turns]
    return sorted(all_turns, key=lambda t: t.start)


def relabel_speakers(turns: list[SpeakerTurn],
                     participant_names: list[str]) -> list[SpeakerTurn]:
    """
    Map SPEAKER_00, SPEAKER_01... to real names by first appearance order.
    'You' is never remapped.
    """
    if not participant_names:
        return turns

    seen = {}
    for turn in turns:
        if turn.speaker != "You" and turn.speaker not in seen:
            seen[turn.speaker] = len(seen)

    speaker_map = {}
    for speaker, idx in sorted(seen.items(), key=lambda x: x[1]):
        speaker_map[speaker] = participant_names[idx] if idx < len(participant_names) else speaker

    for turn in turns:
        if turn.speaker != "You":
            turn.speaker = speaker_map.get(turn.speaker, turn.speaker)

    return turns


# ── Output formatters ─────────────────────────────────────────────────────────

def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def render_markdown(turns: list[SpeakerTurn], meeting_meta: dict) -> str:
    lines = []
    topic = meeting_meta.get("topic", "Meeting")
    start = meeting_meta.get("start_time", "")
    lines.append(f"# {topic}")
    if start:
        lines.append(f"**{start}**")
    lines.append("")

    participants = meeting_meta.get("participants", [])
    if participants:
        lines.append("## Participants")
        for p in participants:
            dur  = p.get("duration_sec", 0)
            line = f"- {p['name']}"
            if dur:          line += f" ({dur//60}m)"
            if p.get("email"): line += f" — {p['email']}"
            lines.append(line)
        lines.append("")

    agenda = meeting_meta.get("agenda")
    if agenda:
        lines.append("## Agenda")
        lines.append(agenda)
        lines.append("")

    lines.append("## Transcript")
    lines.append("")

    prev_speaker = None
    for turn in turns:
        if not turn.text.strip():
            continue
        if turn.speaker != prev_speaker:
            ts = format_timestamp(turn.start)
            lines.append(f"**{turn.speaker}** `[{ts}]`")
            prev_speaker = turn.speaker
        lines.append(f"> {turn.text.strip()}")
        lines.append("")

    return "\n".join(lines)


def build_output_json(turns: list[SpeakerTurn], meeting_meta: dict,
                      recording_path: Path,
                      diarization_segments: list[dict] = None) -> dict:
    return {
        "schema_version": "1.1",
        "recording": {
            "file":         str(recording_path),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        },
        "meeting":  meeting_meta,
        "speakers": sorted(set(t.speaker for t in turns)),
        "transcript": {
            "turns": [
                {
                    "speaker": t.speaker,
                    "start":   round(t.start, 3),
                    "end":     round(t.end, 3),
                    "text":    t.text,
                }
                for t in turns if t.text.strip()
            ],
        },
        "diarization_segments": diarization_segments or [],
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def process_recording(
    audio_path: Path,
    meeting_meta: dict,
    hf_token: str,
    whisper_model: str = "medium.en",
    num_speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
    participant_names: Optional[list[str]] = None,
) -> tuple[Path, Path]:
    """
    Full pipeline. Returns (json_path, markdown_path).

    Speaker count params (all optional, use whichever you know):
      num_speakers  — exact count, sets min=max=N
      min_speakers  — floor for auto-detection (default 1)
      max_speakers  — ceiling for auto-detection

    Stereo WAV (Zoom):
      Left  = remote audio → Whisper + pyannote diarization
      Right = mic (you)    → Whisper only, labelled "You"
      max for remote channel = max_speakers - 1 (you are already separated)

    Mono WAV (Meet):
      Single channel → Whisper + pyannote diarization across everyone
    """
    # Normalise: num_speakers is shorthand for min=max=N
    if num_speakers is not None:
        min_speakers = min_speakers or num_speakers
        max_speakers = max_speakers or num_speakers
    base = audio_path.with_suffix("")
    logger.info(f"=== Processing: {audio_path.name} ===")

    all_diarization = []

    if is_stereo(audio_path):
        logger.info("Stereo audio — channel-based speaker separation")
        left_path, right_path = split_stereo(audio_path)

        # Remote participants: transcribe + diarize with auto speaker detection
        logger.info("Transcribing remote channel (left)...")
        left_words = transcribe(left_path, model_size=whisper_model)

        # min=1 (could be one remote speaker), max=num_speakers-1 (you are separate)
        remote_min = max(1, (min_speakers or 1) - 1) if (min_speakers or 1) > 1 else 1
        remote_max = max(1, (max_speakers or 8) - 1)
        logger.info(f"Diarizing remote channel (min={remote_min} max={remote_max} remote speakers)...")
        left_diarization = diarize(
            left_path, hf_token=hf_token,
            min_speakers=remote_min,
            max_speakers=remote_max,
        )
        all_diarization = left_diarization
        remote_turns = words_to_turns_with_diarization(left_words, left_diarization)

        # You: transcribe only
        logger.info("Transcribing local channel (right — 'You')...")
        right_words  = transcribe(right_path, model_size=whisper_model)
        local_turns  = words_to_turns_single_speaker(right_words, speaker="You")

        # TODO: re-enable after diarization testing
        # left_path.unlink(missing_ok=True)
        # right_path.unlink(missing_ok=True)

        turns = merge_turns(remote_turns, local_turns)

    else:
        logger.info("Mono audio — full diarization across all speakers")
        words       = transcribe(audio_path, model_size=whisper_model)
        diarization = diarize(
            audio_path, hf_token=hf_token,
            min_speakers=min_speakers or 1,
            max_speakers=max_speakers,  # None = fully automatic
        )
        all_diarization = diarization
        turns = words_to_turns_with_diarization(words, diarization)

    # Relabel SPEAKER_XX with real names if available
    names = participant_names or [p["name"] for p in meeting_meta.get("participants", [])]
    if names:
        turns = relabel_speakers(turns, names)

    # Write outputs
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(
        build_output_json(turns, meeting_meta, audio_path, all_diarization),
        indent=2, ensure_ascii=False,
    ))
    logger.info(f"📄 JSON: {json_path}")

    md_path = base.with_suffix(".md")
    md_path.write_text(render_markdown(turns, meeting_meta))
    logger.info(f"📝 Markdown: {md_path}")

    return json_path, md_path
