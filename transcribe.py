"""
Transcription engine — faster-whisper (CTranslate2).

faster-whisper is MIT-licensed, runs CPU-only, needs no API keys, and produces
deterministic, drift-free timestamps at any audio length. The model is loaded
once and reused across talks.

Returns a list of segments: [{"start": float, "end": float, "text": str}, ...]
with times in seconds — a simple shape the formatter turns into [mm:ss] lines.
"""

from config import (
    WHISPER_MODEL,
    WHISPER_DEVICE,
    WHISPER_COMPUTE_TYPE,
    WHISPER_LANGUAGE,
    WHISPER_BEAM_SIZE,
)

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        print(f"🧠 Loading whisper model '{WHISPER_MODEL}' "
              f"({WHISPER_DEVICE}/{WHISPER_COMPUTE_TYPE})...")
        _model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
    return _model


def transcribe(wav_path) -> dict:
    """
    Transcribe an audio file. Returns:
        {"language": str, "duration": float, "segments": [ {start,end,text} ]}
    """
    model = _get_model()
    segments_iter, info = model.transcribe(
        str(wav_path),
        language=WHISPER_LANGUAGE or None,
        beam_size=WHISPER_BEAM_SIZE,
        vad_filter=True,  # drop long silences -> tighter, cleaner segments
    )
    segments = [
        {"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
        for s in segments_iter
        if s.text and s.text.strip()
    ]
    return {
        "language": getattr(info, "language", "") or "",
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
        "segments": segments,
    }
