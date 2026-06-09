"""
Turn raw whisper segments into a clean, readable transcript file:

    # <Talk Title>

    Date: 2026-06-08
    Source video: <original filename>.mp4
    Drive: https://drive.google.com/file/d/<id>/view
    Duration: 00:48:12
    Transcribed: 2026-06-09 · whisper large-v3-turbo

    ---

    [00:00] First paragraph of speech, merged from short segments...
    [00:32] Next paragraph...

Short (~5s) whisper segments are merged into ~paragraph-sized blocks (a new
block starts roughly every PARA_GAP_SECONDS, or at sentence boundaries past
PARA_MIN_CHARS) so the result reads like prose, not a subtitle dump. Each block
is prefixed with the [mm:ss] (or [h:mm:ss]) of its first segment.
"""

import re

PARA_GAP_SECONDS = 30      # start a new paragraph at least this often
PARA_MIN_CHARS = 240       # ...or at a sentence end once a block is this long


def fmt_timestamp(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def transcript_filename(video, talk_date: str) -> str:
    """`YYYY-MM-DD — <Title>.txt`, sanitized for Drive."""
    title = re.sub(r"\.[^.]+$", "", video.name).strip()       # drop extension
    title = re.sub(r"\s+", " ", title)
    title = title.replace("/", "-")                            # '/' illegal in names
    prefix = f"{talk_date} — " if talk_date else ""
    return f"{prefix}{title}.txt"


def _merge_paragraphs(segments: list[dict]) -> list[tuple[float, str]]:
    """Merge short segments into (start_time, text) paragraph tuples."""
    paras: list[tuple[float, str]] = []
    cur_start = None
    cur_text = ""
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        if cur_start is None:
            cur_start = seg["start"]
            cur_text = text
            continue
        cur_text = f"{cur_text} {text}".strip()
        long_enough = len(cur_text) >= PARA_MIN_CHARS and re.search(r"[.!?]$", text)
        big_gap = (seg["end"] - cur_start) >= PARA_GAP_SECONDS
        if long_enough or big_gap:
            paras.append((cur_start, cur_text))
            cur_start, cur_text = None, ""
    if cur_text:
        paras.append((cur_start, cur_text))
    return paras


def build_transcript(video, result: dict, talk_date: str, model_name: str, today: str) -> str:
    """Assemble the full transcript text (header + timestamped body)."""
    title = re.sub(r"\.[^.]+$", "", video.name).strip()
    duration = result.get("duration", 0.0)
    drive_url = f"https://drive.google.com/file/d/{video.drive_id}/view"

    header = [f"# {title}", ""]
    if talk_date:
        header.append(f"Date: {talk_date}")
    header.append(f"Source video: {video.name}")
    header.append(f"Drive: {drive_url}")
    if duration:
        header.append(f"Duration: {fmt_duration(duration)}")
    lang = result.get("language", "")
    stamp = f"Transcribed: {today} · whisper {model_name}"
    if lang:
        stamp += f" · language: {lang}"
    header.append(stamp)
    header += ["", "---", ""]

    body = [
        f"[{fmt_timestamp(start)}] {text}"
        for start, text in _merge_paragraphs(result.get("segments", []))
    ]

    return "\n".join(header) + "\n" + "\n\n".join(body) + "\n"
