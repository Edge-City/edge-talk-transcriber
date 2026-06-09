"""
Cache I/O: a local JSON file when running locally, Google Cloud Storage when
running inside a Cloud Run Job. Cloud Run sets CLOUD_RUN_JOB automatically, so
the mode switches with zero code changes elsewhere.

The cache is a flat dict. The transcriber uses one key:
    "done": { drive_id: {"transcript_id": ..., "name": ..., "at": ...} }
plus "transcripts_folder_id" once resolved/created.
"""

import json
import os
from pathlib import Path

IS_CLOUD = bool(os.environ.get("CLOUD_RUN_JOB") or os.environ.get("K_SERVICE"))

_gcs_client = None


def _get_gcs_client():
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage
        import google.auth
        from config import GCP_PROJECT_ID
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        _gcs_client = storage.Client(project=GCP_PROJECT_ID, credentials=creds)
    return _gcs_client


def load_cache(cache_file: str) -> dict:
    if IS_CLOUD:
        return _load_gcs()
    path = Path(cache_file)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict, cache_file: str) -> None:
    if IS_CLOUD:
        _save_gcs(cache)
    else:
        with open(cache_file, "w") as f:
            json.dump(cache, f, indent=2)


def _load_gcs() -> dict:
    from config import GCS_BUCKET_NAME, GCS_CACHE_KEY
    try:
        bucket = _get_gcs_client().bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(GCS_CACHE_KEY)
        if blob.exists():
            print(f"📥 Loaded cache from gs://{GCS_BUCKET_NAME}/{GCS_CACHE_KEY}")
            return json.loads(blob.download_as_text())
        print("ℹ️  No cache in GCS — starting fresh.")
    except Exception as e:
        print(f"⚠️  Could not load cache from GCS: {e}")
    return {}


def _save_gcs(cache: dict) -> None:
    from config import GCS_BUCKET_NAME, GCS_CACHE_KEY
    if not GCS_BUCKET_NAME:
        raise RuntimeError(
            "GCS_BUCKET_NAME is not set — cannot persist the cache on Cloud Run. "
            "Without it, every run would re-transcribe all talks."
        )
    bucket = _get_gcs_client().bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(GCS_CACHE_KEY)
    blob.upload_from_string(json.dumps(cache, indent=2), content_type="application/json")
    print(f"💾 Saved cache to gs://{GCS_BUCKET_NAME}/{GCS_CACHE_KEY}")
