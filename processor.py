"""
processor.py
Post-meeting pipeline.

Stereo recordings (Zoom):
  L channel = BlackHole = remote participants (may be multiple speakers)
  R channel = microphone = you (single known speaker)

  Pipeline:
    Right → resemblyzer voice fingerprint  →  "You" acoustic model
    Left  → WhisperX transcribe+align      →  words with phoneme-accurate timestamps
    Left  → pyannote diarize (no cap)      →  all speakers including echo
    Left  → acoustic echo filter           →  remote speakers only
    Right → WhisperX transcribe+align      →  "You" turns
    Merge → collapse micro-turns           →  final transcript

Mono recordings (Meet via Chrome tabCapture or Safari system audio):
  Single channel → WhisperX + pyannote diarization across all speakers

Output: .json + .md per recording
"""

import json
import logging
import subprocess
import warnings
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
        "ffmpeg", "-y", "-i", str(audio_path),
        "-af", "pan=mono|c0=c0",
        "-loglevel", "error", str(left),
    ], check=True, capture_output=True)

    subprocess.run([
        "ffmpeg", "-y", "-i", str(audio_path),
        "-af", "pan=mono|c0=c1",
        "-loglevel", "error", str(right),
    ], check=True, capture_output=True)

    logger.info(f"Split stereo: L={left.name} R={right.name}")
    return left, right


# ── Transcription (WhisperX with forced phoneme alignment) ────────────────────

def transcribe_aligned(audio_path: Path, model_size: str = "medium.en") -> list[Word]:
    """
    Transcribe with WhisperX: Whisper → wav2vec2 forced alignment.
    Returns words with phoneme-accurate timestamps (<50ms drift vs Whisper's ~1s).
    Falls back to faster-whisper if alignment model unavailable.
    """
    try:
        import whisperx

        device = "cpu"
        logger.info(f"Loading WhisperX model '{model_size}'...")
        model = whisperx.load_model(model_size, device=device, compute_type="int8")

        logger.info(f"Transcribing {audio_path.name}...")
        audio = whisperx.load_audio(str(audio_path))
        result = model.transcribe(audio, batch_size=8, language="en")

        logger.info("Force-aligning word timestamps (wav2vec2)...")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            align_model, metadata = whisperx.load_align_model(
                language_code="en", device=device
            )
        result = whisperx.align(
            result["segments"], align_model, metadata, audio, device=device,
            return_char_alignments=False,
        )

        words = []
        for seg in result.get("segments", []):
            for w in seg.get("words", []):
                if w.get("start") is not None and w.get("end") is not None:
                    words.append(Word(
                        start=float(w["start"]),
                        end=float(w["end"]),
                        text=w["word"],
                        probability=float(w.get("score", 0.0)),
                    ))

        duration = words[-1].end if words else 0
        logger.info(f"Aligned transcription: {len(words)} words, {duration:.1f}s")
        return words

    except Exception as e:
        logger.warning(f"WhisperX alignment failed ({e}), falling back to faster-whisper")
        return _transcribe_fallback(audio_path, model_size)


def _transcribe_fallback(audio_path: Path, model_size: str) -> list[Word]:
    """faster-whisper fallback (unaligned timestamps)."""
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        str(audio_path), language="en", word_timestamps=True,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
    )
    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append(Word(start=w.start, end=w.end,
                                  text=w.word, probability=w.probability))
    logger.info(f"Fallback transcription: {len(words)} words, {info.duration:.1f}s audio")
    return words


# ── Voice fingerprinting (resemblyzer) ────────────────────────────────────────

def extract_voice_fingerprint(audio_path: Path):
    """
    Extract a 256-dim speaker embedding for the audio using resemblyzer.
    Used to create a voice fingerprint of "You" from the right channel.
    Returns None if audio is too short or resemblyzer fails.
    """
    try:
        import numpy as np
        from resemblyzer import VoiceEncoder, preprocess_wav

        from resemblyzer.hparams import sampling_rate as _sr
        encoder = VoiceEncoder()
        wav = preprocess_wav(str(audio_path))
        if len(wav) < _sr * 2:  # need at least 2s
            logger.info("Right channel too short for voice fingerprint")
            return None

        embedding = encoder.embed_utterance(wav)
        logger.info(f"Voice fingerprint extracted ({len(wav)/_sr:.1f}s audio)")
        return embedding
    except Exception as e:
        logger.warning(f"Voice fingerprint extraction failed: {e}")
        return None


def filter_echo_acoustic(
    diarization: list[dict],
    you_fingerprint,
    pyannote_embeddings: Optional[dict] = None,
    audio_path: Optional[Path] = None,
    similarity_threshold: float = 0.82,
) -> list[dict]:
    """
    Remove diarization segments whose speaker sounds like "You".

    Strategy (in priority order):
    1. pyannote speaker embeddings (free from diarization, no extra model load)
    2. resemblyzer embeddings from raw audio (fallback)

    Speakers with cosine similarity ≥ threshold to the "You" fingerprint
    are removed from the diarization before turn building.
    """
    if you_fingerprint is None:
        return diarization

    import numpy as np

    def cosine(a, b) -> float:
        a, b = a.flatten(), b.flatten()
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))

    # ── Strategy 1: use pyannote embeddings (already computed) ────────────────
    if pyannote_embeddings:
        echo_speakers: set[str] = set()
        for spk, emb in pyannote_embeddings.items():
            try:
                sim = cosine(you_fingerprint, emb)
                logger.info(f"  {spk}: pyannote cosine similarity to You = {sim:.3f}")
                if sim >= similarity_threshold:
                    echo_speakers.add(spk)
                    logger.info(f"    → echo identified via pyannote embeddings, removing {spk}")
            except Exception as e:
                logger.debug(f"pyannote embedding compare failed for {spk}: {e}")

        if echo_speakers:
            filtered = [s for s in diarization if s["speaker"] not in echo_speakers]
            logger.info(
                f"Acoustic echo filter (pyannote): removed {len(echo_speakers)} speaker(s), "
                f"{len(diarization)-len(filtered)} segments dropped"
            )
            return filtered
        logger.info("Acoustic echo filter (pyannote): no echo speakers detected")
        return diarization

    # ── Strategy 2: resemblyzer fallback ─────────────────────────────────────
    if audio_path is None:
        return diarization
    try:
        from resemblyzer import VoiceEncoder, preprocess_wav
        from resemblyzer.hparams import sampling_rate as _sr

        encoder  = VoiceEncoder()
        wav      = preprocess_wav(str(audio_path))
        duration = len(wav) / _sr

        speaker_best: dict[str, dict] = {}
        for seg in diarization:
            spk, dur = seg["speaker"], seg["end"] - seg["start"]
            if spk not in speaker_best or dur > (speaker_best[spk]["end"] - speaker_best[spk]["start"]):
                speaker_best[spk] = seg

        echo_speakers = set()
        for spk, seg in speaker_best.items():
            if seg["end"] - seg["start"] < 1.5:
                continue
            s, e = max(0, seg["start"]), min(duration, seg["end"])
            try:
                emb = encoder.embed_utterance(wav[int(s * _sr): int(e * _sr)])
                sim = cosine(you_fingerprint, emb)
                logger.info(f"  {spk}: resemblyzer cosine similarity to You = {sim:.3f}")
                if sim >= similarity_threshold:
                    echo_speakers.add(spk)
                    logger.info(f"    → echo identified via resemblyzer, removing {spk}")
            except Exception as ex:
                logger.debug(f"resemblyzer failed for {spk}: {ex}")

        filtered = [s for s in diarization if s["speaker"] not in echo_speakers]
        if echo_speakers:
            logger.info(f"Acoustic echo filter (resemblyzer): removed {len(echo_speakers)} speaker(s)")
        else:
            logger.info("Acoustic echo filter (resemblyzer): no echo speakers detected")
        return filtered

    except Exception as e:
        logger.warning(f"Acoustic echo filter failed ({e}), keeping all segments")
        return diarization


# ── Diarization ───────────────────────────────────────────────────────────────

def diarize(audio_path: Path, hf_token: str,
            min_speakers: int = 1,
            max_speakers: Optional[int] = None
            ) -> tuple[list[dict], Optional[dict]]:
    """
    Run pyannote speaker diarization.
    Returns (segments, speaker_embeddings_dict) where:
      segments = [{"speaker": str, "start": float, "end": float}, ...]
      speaker_embeddings_dict = {"SPEAKER_00": np.ndarray, ...} or None

    max_speakers=None lets pyannote fully auto-detect.
    """
    from pyannote.audio import Pipeline
    import torch

    logger.info("Loading pyannote diarization pipeline...")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=hf_token,
        )

    if torch.backends.mps.is_available():
        pipeline.to(torch.device("mps"))
        logger.info("Using Apple MPS (GPU) for diarization")

    kwargs = {}
    if min_speakers is not None: kwargs["min_speakers"] = min_speakers
    if max_speakers is not None: kwargs["max_speakers"] = max_speakers

    bounds = f"min={min_speakers}" + (f" max={max_speakers}" if max_speakers else " max=auto")
    logger.info(f"Diarizing {audio_path.name} ({bounds})...")

    # pyannote 4.x works best with pre-loaded waveforms (avoids torchcodec issues)
    import torchaudio
    waveform, sample_rate = torchaudio.load(str(audio_path))
    audio_input = {"waveform": waveform, "sample_rate": sample_rate}
    result = pipeline(audio_input, **kwargs)

    # pyannote 4.x returns DiarizeOutput; 3.x returns Annotation directly
    if hasattr(result, "speaker_diarization"):
        annotation       = result.speaker_diarization
        raw_embeddings   = result.speaker_embeddings  # (n_speakers, dim) ndarray or None
        speaker_labels   = annotation.labels()
        embeddings_dict  = (
            {spk: raw_embeddings[i] for i, spk in enumerate(speaker_labels)}
            if raw_embeddings is not None else None
        )
    else:
        annotation      = result
        embeddings_dict = None

    segments = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        segments.append({"speaker": speaker, "start": turn.start, "end": turn.end})

    n_speakers = len(set(s["speaker"] for s in segments))
    logger.info(f"Diarization complete: {n_speakers} speakers, {len(segments)} segments")
    return segments, embeddings_dict


# ── Merge helpers ─────────────────────────────────────────────────────────────

def words_to_turns_with_diarization(words: list[Word],
                                    diarization: list[dict]) -> list[SpeakerTurn]:
    """Assign each word to a speaker via diarization, collapse into turns."""

    def speaker_at(t: float) -> str:
        for seg in diarization:
            if seg["start"] <= t <= seg["end"]:
                return seg["speaker"]
        best = min(diarization, key=lambda s: min(abs(t - s["start"]), abs(t - s["end"])), default=None)
        return best["speaker"] if best else "UNKNOWN"

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

    GAP = 1.5
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


def collapse_turns(turns: list[SpeakerTurn],
                   gap_threshold: float = 0.8) -> list[SpeakerTurn]:
    """
    Merge consecutive same-speaker turns separated by a short gap.
    Reduces fragmentation from pyannote micro-segments and Whisper pauses.
    """
    if not turns:
        return turns

    merged = [turns[0]]
    for turn in turns[1:]:
        prev = merged[-1]
        if (turn.speaker == prev.speaker and
                turn.start - prev.end <= gap_threshold):
            prev.words.extend(turn.words)
            prev.end = turn.end
        else:
            merged.append(turn)

    removed = len(turns) - len(merged)
    if removed:
        logger.info(f"Collapsed {removed} micro-turns into neighbouring same-speaker turns")
    return merged


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
        "schema_version": "1.2",
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

    Stereo WAV (Zoom):
      1. Extract "You" voice fingerprint from right channel (resemblyzer)
      2. Transcribe left channel with WhisperX (phoneme-aligned timestamps)
      3. Diarize left channel with pyannote (no speaker cap — auto-detect)
      4. Remove echo speakers acoustically using voice fingerprint
      5. Transcribe right channel with WhisperX
      6. Merge, collapse micro-turns, relabel

    Mono WAV (Meet):
      WhisperX transcription + pyannote diarization across all speakers
    """
    if num_speakers is not None:
        min_speakers = min_speakers or num_speakers
        max_speakers = max_speakers or num_speakers

    base = audio_path.with_suffix("")
    logger.info(f"=== Processing: {audio_path.name} ===")

    all_diarization = []

    if is_stereo(audio_path):
        logger.info("Stereo — channel-based separation with acoustic echo cancellation")
        left_path, right_path = split_stereo(audio_path)

        # Step 1: Extract "You" voice fingerprint from mic channel
        logger.info("Extracting voice fingerprint from mic channel...")
        you_fingerprint = extract_voice_fingerprint(right_path)

        # Step 2: Transcribe remote channel (aligned timestamps)
        logger.info("Transcribing remote channel (left)...")
        left_words = transcribe_aligned(left_path, model_size=whisper_model)

        # Step 3: Diarize remote channel — no speaker cap, echo removed acoustically
        logger.info("Diarizing remote channel (auto speaker count)...")
        left_diarization, left_embeddings = diarize(
            left_path, hf_token=hf_token,
            min_speakers=1,
            max_speakers=max_speakers,  # None = fully auto
        )
        all_diarization = left_diarization

        # Step 4: Remove echo speakers using pyannote embeddings (free) or resemblyzer
        logger.info("Running acoustic echo cancellation...")
        left_diarization = filter_echo_acoustic(
            left_diarization,
            you_fingerprint,
            pyannote_embeddings=left_embeddings,
            audio_path=left_path,
        )

        remote_turns = words_to_turns_with_diarization(left_words, left_diarization)

        # Step 5: Transcribe mic channel
        logger.info("Transcribing mic channel (right — 'You')...")
        right_words = transcribe_aligned(right_path, model_size=whisper_model)
        local_turns  = words_to_turns_single_speaker(right_words, speaker="You")

        turns = merge_turns(remote_turns, local_turns)

    else:
        logger.info("Mono audio — full diarization across all speakers")
        words = transcribe_aligned(audio_path, model_size=whisper_model)
        diarization, _ = diarize(
            audio_path, hf_token=hf_token,
            min_speakers=min_speakers or 1,
            max_speakers=max_speakers,
        )
        all_diarization = diarization
        turns = words_to_turns_with_diarization(words, diarization)

    # Collapse micro-turns (same speaker, short gap)
    turns = collapse_turns(turns)

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
    logger.info(f"JSON: {json_path}")

    md_path = base.with_suffix(".md")
    md_path.write_text(render_markdown(turns, meeting_meta))
    logger.info(f"Markdown: {md_path}")

    return json_path, md_path
