"""
processor.py
Post-meeting pipeline:
  1. Transcribe with faster-whisper (word-level timestamps)
  2. Diarize with pyannote.audio (speaker segments)
  3. Merge transcript words into speaker-labeled turns
  4. Enrich with Zoom Cloud API metadata
  5. Output: rich JSON + readable Markdown transcript

Requirements:
  pip install faster-whisper pyannote.audio torch torchaudio
  
  pyannote requires accepting terms at:
  https://huggingface.co/pyannote/speaker-diarization-3.1
  Then: huggingface-cli login
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class Word:
    start: float
    end: float
    text: str
    probability: float = 0.0

@dataclass
class SpeakerTurn:
    speaker: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text.strip() for w in self.words)

    @property
    def duration(self) -> float:
        return self.end - self.start


# ── Transcription ────────────────────────────────────────────────────────────

def transcribe(audio_path: Path, model_size: str = "medium.en", vocabulary: str = "") -> list[Word]:
    """
    Run faster-whisper with word-level timestamps.
    M4 Pro can handle medium.en in ~0.15x realtime (much faster than realtime).
    Use 'large-v3' for best accuracy at cost of ~3x slower.
    """
    from faster_whisper import WhisperModel

    logger.info(f"Loading Whisper model '{model_size}'...")
    # device="cpu" works fine on M4 Pro via MLX; use "cuda" if you have a GPU
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    logger.info(f"Transcribing {audio_path.name}...")
    segments, info = model.transcribe(
        str(audio_path),
        initial_prompt=vocabulary or None,
        language="en",
        word_timestamps=True,       # critical — gives us per-word timing
        vad_filter=True,            # skip silence (speeds up processing, improves quality)
        vad_parameters={
            "min_silence_duration_ms": 500,
        },
    )

    words = []
    for segment in segments:
        if segment.words:
            for word in segment.words:
                words.append(Word(
                    start=word.start,
                    end=word.end,
                    text=word.word,
                    probability=word.probability,
                ))

    logger.info(f"Transcription complete: {len(words)} words, {info.duration:.1f}s audio")
    return words


# ── Diarization ──────────────────────────────────────────────────────────────

def diarize(audio_path: Path, hf_token: str, num_speakers: Optional[int] = None) -> list[dict]:
    """
    Run pyannote speaker diarization.
    Returns list of {speaker, start, end} segments.
    
    num_speakers: if you know how many speakers, pass it — improves accuracy significantly.
    """
    from pyannote.audio import Pipeline
    import torch

    logger.info("Loading pyannote diarization pipeline...")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )

    # Use MPS (Apple Silicon GPU) if available
    if torch.backends.mps.is_available():
        pipeline.to(torch.device("mps"))
        logger.info("Using Apple MPS (GPU) for diarization")
    else:
        logger.info("Using CPU for diarization")

    logger.info(f"Diarizing {audio_path.name}...")
    
    diarize_kwargs = {}
    if num_speakers:
        diarize_kwargs["num_speakers"] = num_speakers

    diarization = pipeline(str(audio_path), **diarize_kwargs)

    segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segments.append({
            "speaker": speaker,   # e.g. "SPEAKER_00", "SPEAKER_01"
            "start": turn.start,
            "end": turn.end,
        })

    logger.info(f"Diarization complete: {len(set(s['speaker'] for s in segments))} speakers, {len(segments)} segments")
    return segments


# ── Merge ────────────────────────────────────────────────────────────────────

def merge_transcript_with_speakers(words: list[Word], diarization: list[dict]) -> list[SpeakerTurn]:
    """
    Assign each word to a speaker by finding the diarization segment that
    best overlaps with the word's midpoint timestamp.
    
    Then collapse consecutive words from the same speaker into turns.
    """

    def speaker_at(t: float) -> str:
            """Find which speaker is active at time t."""
            for seg in diarization:
                if seg["start"] <= t <= seg["end"]:
                    return seg["speaker"]
            # No exact match — find nearest segment
            best = None
            best_dist = float("inf")
            for seg in diarization:
                dist = min(abs(t - seg["start"]), abs(t - seg["end"]))
                if dist < best_dist:
                    best_dist = dist
                    best = seg["speaker"]
            return best or "UNKNOWN"

    # Assign speaker to each word by midpoint
    labeled_words = []
    for word in words:
        midpoint = (word.start + word.end) / 2
        labeled_words.append((speaker_at(midpoint), word))

    # Collapse into turns (consecutive words from same speaker)
    turns: list[SpeakerTurn] = []
    if not labeled_words:
        return turns

    current_speaker, first_word = labeled_words[0]
    current_turn = SpeakerTurn(
        speaker=current_speaker,
        start=first_word.start,
        end=first_word.end,
        words=[first_word],
    )

    for speaker, word in labeled_words[1:]:
        if speaker == current_speaker:
            current_turn.words.append(word)
            current_turn.end = word.end
        else:
            turns.append(current_turn)
            current_turn = SpeakerTurn(
                speaker=speaker,
                start=word.start,
                end=word.end,
                words=[word],
            )
            current_speaker = speaker

    turns.append(current_turn)
    return turns


def relabel_speakers(turns: list[SpeakerTurn], participant_names: list[str]) -> list[SpeakerTurn]:
    """
    Attempt to map pyannote's generic labels (SPEAKER_00, SPEAKER_01) to 
    real names from the participant list.
    
    Simple heuristic: sort speakers by first appearance, map to participant list
    in order. Not perfect, but better than generic labels. The JSON output
    preserves original labels so you can re-map manually if needed.
    
    For better accuracy, consider voice enrollment with pyannote.
    """
    if not participant_names:
        return turns

    # Find speakers in order of first appearance
    seen = {}
    for turn in turns:
        if turn.speaker not in seen:
            seen[turn.speaker] = len(seen)

    speaker_map = {}
    for speaker, idx in sorted(seen.items(), key=lambda x: x[1]):
        if idx < len(participant_names):
            speaker_map[speaker] = participant_names[idx]
        else:
            speaker_map[speaker] = speaker  # fallback to generic label

    for turn in turns:
        turn.speaker = speaker_map.get(turn.speaker, turn.speaker)

    return turns


# ── Output formatters ────────────────────────────────────────────────────────

def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def render_markdown(turns: list[SpeakerTurn], meeting_meta: dict) -> str:
    lines = []

    # Header
    topic = meeting_meta.get("topic", "Zoom Meeting")
    start = meeting_meta.get("start_time", "")
    lines.append(f"# {topic}")
    if start:
        lines.append(f"**{start}**")
    lines.append("")

    # Participants
    participants = meeting_meta.get("participants", [])
    if participants:
        lines.append("## Participants")
        for p in participants:
            dur = p.get("duration_sec", 0)
            dur_str = f" ({dur//60}m)" if dur else ""
            lines.append(f"- {p['name']}{dur_str}" + (f" — {p['email']}" if p.get('email') else ""))
        lines.append("")

    # Agenda
    agenda = meeting_meta.get("agenda")
    if agenda:
        lines.append("## Agenda")
        lines.append(agenda)
        lines.append("")

    # Transcript
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


def build_output_json(turns: list[SpeakerTurn], words: list[Word],
                      diarization: list[dict], meeting_meta: dict,
                      recording_path: Path) -> dict:
    return {
        "schema_version": "1.0",
        "recording": {
            "file": str(recording_path),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        },
        "meeting": meeting_meta,
        "speakers": list(set(t.speaker for t in turns)),
        "transcript": {
            "turns": [
                {
                    "speaker": t.speaker,
                    "start": round(t.start, 3),
                    "end": round(t.end, 3),
                    "text": t.text,
                }
                for t in turns
            ],
            "words": [
                {
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "text": w.text,
                    "confidence": round(w.probability, 3),
                }
                for w in words
            ],
        },
        "diarization_segments": diarization,
    }


# ── Vocabulary ────────────────────────────────────────────────────────────────

VOCABULARY_FILE = Path(__file__).parent / "vocabulary.txt"

def load_vocabulary() -> str:
    """Load custom vocabulary from vocabulary.txt if it exists."""
    if VOCABULARY_FILE.exists():
        text = VOCABULARY_FILE.read_text().strip()
        logger.info(f"Loaded vocabulary ({len(text.split(','))} terms)")
        return text
    return ""


# ── Stereo helpers ────────────────────────────────────────────────────────────

def is_stereo(audio_path: Path) -> bool:
    """Check if audio file has 2 channels."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels",
             "-of", "csv=p=0", str(audio_path)],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() == "2"
    except Exception:
        return False


def split_stereo(audio_path: Path) -> tuple[Path, Path]:
    """Split stereo WAV into left (remote) and right (local) mono files."""
    left_path = audio_path.with_stem(audio_path.stem + "_left")
    right_path = audio_path.with_stem(audio_path.stem + "_right")

    subprocess.run([
        "ffmpeg", "-i", str(audio_path),
        "-filter_complex", "channelsplit=channel_layout=stereo[L][R]",
        "-map", "[L]", "-ar", "16000", "-ac", "1", str(left_path),
        "-map", "[R]", "-ar", "16000", "-ac", "1", str(right_path),
        "-y", "-loglevel", "error",
    ], check=True)

    logger.info(f"Split stereo: L={left_path.name} R={right_path.name}")
    return left_path, right_path


def process_stereo(
    audio_path: Path,
    meeting_meta: dict,
    hf_token: str,
    whisper_model: str = "medium.en",
    vocabulary: str = "",
    num_speakers: Optional[int] = None,
    local_speaker_name: str = "You",
) -> tuple[list[SpeakerTurn], list[Word], list[dict]]:
    """
    Stereo pipeline: left=remote participants, right=local mic.
    Transcribes each channel separately, diarizes only the remote channel,
    then merges timelines.
    """
    left_path, right_path = split_stereo(audio_path)

    try:
        # Transcribe both channels
        logger.info("Transcribing remote channel (left)...")
        remote_words = transcribe(left_path, model_size=whisper_model, vocabulary=vocabulary)

        logger.info("Transcribing local channel (right)...")
        local_words = transcribe(right_path, model_size=whisper_model, vocabulary=vocabulary)

        # Diarize only the remote channel (multiple speakers possible)
        remote_diarization = []
        if remote_words:
            remote_speaker_count = num_speakers - 1 if num_speakers and num_speakers > 1 else None
            if remote_speaker_count and remote_speaker_count <= 1:
                remote_speaker_count = None  # let pyannote auto-detect
            remote_diarization = diarize(
                left_path, hf_token=hf_token,
                num_speakers=remote_speaker_count,
            )

        # Build turns for local speaker (all words = you)
        local_turns = []
        if local_words:
            current_turn = SpeakerTurn(
                speaker=local_speaker_name,
                start=local_words[0].start,
                end=local_words[0].end,
                words=[local_words[0]],
            )
            for w in local_words[1:]:
                if w.start - current_turn.end > 1.5:
                    local_turns.append(current_turn)
                    current_turn = SpeakerTurn(
                        speaker=local_speaker_name,
                        start=w.start, end=w.end, words=[w],
                    )
                else:
                    current_turn.words.append(w)
                    current_turn.end = w.end
            local_turns.append(current_turn)

        # Build turns for remote speakers (using diarization)
        if remote_diarization:
            remote_turns = merge_transcript_with_speakers(remote_words, remote_diarization)
        else:
            remote_turns = []
            if remote_words:
                current_turn = SpeakerTurn(
                    speaker="Remote Speaker",
                    start=remote_words[0].start,
                    end=remote_words[0].end,
                    words=[remote_words[0]],
                )
                for w in remote_words[1:]:
                    if w.start - current_turn.end > 1.5:
                        remote_turns.append(current_turn)
                        current_turn = SpeakerTurn(
                            speaker="Remote Speaker",
                            start=w.start, end=w.end, words=[w],
                        )
                    else:
                        current_turn.words.append(w)
                        current_turn.end = w.end
                remote_turns.append(current_turn)

        # Merge timelines chronologically
        all_turns = sorted(local_turns + remote_turns, key=lambda t: t.start)
        all_words = sorted(local_words + remote_words, key=lambda w: w.start)

        logger.info(
            f"Stereo merge: {len(local_turns)} local turns, "
            f"{len(remote_turns)} remote turns"
        )

        return all_turns, all_words, remote_diarization

    finally:
        left_path.unlink(missing_ok=True)
        right_path.unlink(missing_ok=True)


# ── Main entry point ─────────────────────────────────────────────────────────

def process_recording(
    audio_path: Path,
    meeting_meta: dict,
    hf_token: str,
    whisper_model: str = "medium.en",
    num_speakers: Optional[int] = None,
    participant_names: Optional[list[str]] = None,
) -> tuple[Path, Path]:
    """
    Full pipeline. Returns (json_path, markdown_path).
    Automatically uses stereo pipeline if audio has 2 channels.
    """
    base = audio_path.with_suffix("")
    vocabulary = load_vocabulary()

    logger.info(f"=== Processing: {audio_path.name} ===")

    stereo = is_stereo(audio_path)
    if stereo:
        logger.info("Stereo audio detected — using channel-based speaker separation")

    if stereo:
        local_name = "You"
        if participant_names:
            local_name = participant_names[0]

        turns, words, diarization = process_stereo(
            audio_path, meeting_meta,
            hf_token=hf_token,
            whisper_model=whisper_model,
            vocabulary=vocabulary,
            num_speakers=num_speakers,
            local_speaker_name=local_name,
        )

        if participant_names and len(participant_names) > 1:
            remote_names = participant_names[1:]
            turns = relabel_speakers(turns, remote_names)

    else:
        # Mono path: original pipeline
        words = transcribe(audio_path, model_size=whisper_model, vocabulary=vocabulary)
        diarization = diarize(audio_path, hf_token=hf_token, num_speakers=num_speakers)
        turns = merge_transcript_with_speakers(words, diarization)

        names = participant_names or [p["name"] for p in meeting_meta.get("participants", [])]
        if names:
            turns = relabel_speakers(turns, names)

    # Write outputs
    json_data = build_output_json(turns, words, diarization, meeting_meta, audio_path)
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False))
    logger.info(f"📄 JSON: {json_path}")

    md = render_markdown(turns, meeting_meta)
    md_path = base.with_suffix(".md")
    md_path.write_text(md)
    logger.info(f"📝 Markdown: {md_path}")

    return json_path, md_path
