"""
Media handling: download a talk video from Drive to local disk, then extract a
16 kHz mono WAV with ffmpeg (Whisper's native input). The large video file is
deleted immediately after audio extraction, so only the small WAV (~40-120 MB
per hour) persists during transcription.

We download to a real file (chunked) rather than buffering in memory. Where
that file lands is controlled by WORKDIR:
  - blank  -> system temp dir (on Cloud Run this is /tmp, a RAM-backed tmpfs,
              so the job must have enough memory for the largest video).
  - a path -> e.g. a mounted disk or GCS FUSE mount for very large files.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import google.auth
from google.auth.transport.requests import AuthorizedSession

from config import WORKDIR

CHUNK_SIZE = 8 * 1024 * 1024   # 8 MB write chunks
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

_session = None


def _workdir() -> Path:
    base = WORKDIR or tempfile.gettempdir()
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base)


def _authed_session() -> AuthorizedSession:
    global _session
    if _session is None:
        creds, _ = google.auth.default(scopes=[DRIVE_SCOPE])
        _session = AuthorizedSession(creds)
    return _session


def download_video(service, video, dest_dir: Path, attempts: int = 12) -> Path:
    """
    Download a Drive video to dest_dir with RESUME-on-failure.

    Multi-GB downloads from Drive routinely drop the connection mid-stream
    (broken pipe / SSL EOF), and a plain restart just drops again at a similar
    point — so the largest files never finish. Instead we stream to disk and,
    whenever the connection dies, resume from the byte we got to using an HTTP
    Range request. Each attempt makes forward progress, so any file completes
    given enough attempts.
    """
    ext = Path(video.name).suffix or ".mp4"
    out = dest_dir / f"{video.drive_id}{ext}"
    url = (f"https://www.googleapis.com/drive/v3/files/{video.drive_id}"
           f"?alt=media&supportsAllDrives=true")
    total = int(video.size_bytes or 0)
    session = _authed_session()

    last_err = None
    for attempt in range(1, attempts + 1):
        have = out.stat().st_size if out.exists() else 0
        if total and have >= total:
            print(" " * 40, end="\r")
            return out
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with session.get(url, headers=headers, stream=True, timeout=(30, 300)) as r:
                # 200 (fresh) or 206 (partial/resume) are both fine.
                if r.status_code not in (200, 206):
                    r.raise_for_status()
                mode = "ab" if have else "wb"
                with open(out, mode) as fh:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            fh.write(chunk)
                            if total:
                                pct = int(fh.tell() / total * 100)
                                print(f"    downloading {pct}%", end="\r")
            if total and out.stat().st_size >= total:
                print(" " * 40, end="\r")
                return out
            if not total:  # size unknown — one clean pass is all we can verify
                print(" " * 40, end="\r")
                return out
        except Exception as e:
            last_err = e
            got = out.stat().st_size if out.exists() else 0
            print(f"    ⚠️  download dropped at {got}/{total or '?'} bytes "
                  f"(attempt {attempt}/{attempts}, {type(e).__name__}); resuming...")
    raise RuntimeError(f"download failed after {attempts} attempts: {last_err}")


def extract_audio(video_path: Path, wav_path: Path) -> None:
    """Extract 16 kHz mono PCM WAV from a video/audio file via ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        str(wav_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-800:]}")


def prepare_audio(service, video) -> Path:
    """
    Download a Drive video and extract audio. Deletes the video afterward.
    Returns the path to the WAV file (caller deletes it when done).
    """
    work = _workdir()
    video_path = download_video(service, video, work)
    wav_path = work / f"{video.drive_id}.wav"
    try:
        extract_audio(video_path, wav_path)
    finally:
        # Free the large video file as soon as audio is extracted (or on error).
        try:
            video_path.unlink(missing_ok=True)
        except OSError:
            pass
    return wav_path


def cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
