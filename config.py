"""
Central configuration for the Edge Talk Transcriber.

Every value is read from an environment variable so the repo is safe to
open-source: there are NO hard-coded org-specific identifiers here. For local
development, put your values in a `.env` file (gitignored) — see `.env.example`.
On Cloud Run, the same variables are set on the job at deploy time.
"""

import os
from dotenv import load_dotenv

# Loads a local .env if present. A no-op on Cloud Run (vars set on the job).
load_dotenv()


def _clean_list(raw: str) -> list[str]:
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


# ── Google Cloud ──────────────────────────────────────────────────────────
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
# Bucket is only needed for the cloud cache (and optionally a scratch workdir).
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "")
GCS_CACHE_KEY = os.environ.get("GCS_CACHE_KEY", "pipeline-state/transcriber-cache.json")

# ── Google Drive ──────────────────────────────────────────────────────────
# The folder whose subfolders hold the talk videos (the "talks" root).
DRIVE_ROOT_FOLDER_ID = os.environ.get("DRIVE_ROOT_FOLDER_ID", "")
# Where transcripts are written. If blank, a subfolder named
# TRANSCRIPTS_FOLDER_NAME is created inside DRIVE_ROOT_FOLDER_ID and reused.
TRANSCRIPTS_FOLDER_ID = os.environ.get("TRANSCRIPTS_FOLDER_ID", "")
TRANSCRIPTS_FOLDER_NAME = os.environ.get("TRANSCRIPTS_FOLDER_NAME", "Transcripts")

# ── Transcription engine (faster-whisper) ─────────────────────────────────
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")          # cpu | cuda
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
# Blank = autodetect language per talk. Set e.g. "en" to force English.
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "").strip()
WHISPER_BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "5"))

# ── Inventory filtering ───────────────────────────────────────────────────
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg", ".mts",
}
# Folder names (case-insensitive substring) to NOT descend into. The output
# transcripts folder is skipped by default so we never re-scan our own output.
SKIP_FOLDERS = _clean_list(
    os.environ.get("SKIP_FOLDERS", "transcripts")
)
# Ignore tiny files (thumbnails, corrupt clips). Default 500 KB.
MIN_FILE_SIZE_BYTES = int(os.environ.get("MIN_FILE_SIZE_BYTES", str(500 * 1024)))

# ── Local / runtime ───────────────────────────────────────────────────────
# Local cache file (used when NOT on Cloud Run). Cloud Run uses GCS_CACHE_KEY.
CACHE_FILE = os.environ.get("CACHE_FILE", ".transcriber_cache.json")
# Scratch directory for downloads + extracted audio. Blank = system temp dir.
# On Cloud Run, point this at a mounted disk/bucket if processing very large
# files; otherwise /tmp (RAM) is used and the job needs enough memory.
WORKDIR = os.environ.get("WORKDIR", "")


def require(*names: str) -> None:
    """Fail fast with a clear message if required config is missing."""
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise SystemExit(
            "Missing required config: "
            + ", ".join(missing)
            + "\nSet them in a .env file (see .env.example) or as environment variables."
        )
