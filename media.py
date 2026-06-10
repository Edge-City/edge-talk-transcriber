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

from googleapiclient.http import MediaIoBaseDownload

from config import WORKDIR

CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB download chunks


def _workdir() -> Path:
    base = WORKDIR or tempfile.gettempdir()
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base)


def download_video(service, video, dest_dir: Path, attempts: int = 3) -> Path:
    """
    Download a Drive video to dest_dir, streaming to disk in chunks.

    Large multi-GB downloads from Drive occasionally drop the connection
    (broken pipe / SSL EOF). next_chunk(num_retries=...) retries transient
    HTTP errors per chunk, and the outer loop restarts the whole download if
    the stream dies mid-way.
    """
    ext = Path(video.name).suffix or ".mp4"
    out = dest_dir / f"{video.drive_id}{ext}"
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            request = service.files().get_media(fileId=video.drive_id, supportsAllDrives=True)
            with open(out, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=CHUNK_SIZE)
                done = False
                while not done:
                    status, done = downloader.next_chunk(num_retries=5)
                    if status:
                        print(f"    downloading {int(status.progress() * 100)}%", end="\r")
            print(" " * 30, end="\r")
            return out
        except Exception as e:
            last_err = e
            print(f"    ⚠️  download attempt {attempt}/{attempts} failed ({type(e).__name__}); retrying...")
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
