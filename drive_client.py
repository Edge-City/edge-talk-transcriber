"""
Google Drive access: authentication, recursive inventory of talk videos,
resolving/creating the output transcripts folder, and uploading transcripts.

Auth uses Application Default Credentials (google.auth.default):
  - Locally: the credentials from `gcloud auth application-default login`.
  - On Cloud Run: the attached runtime service account (no key file needed).

Every Drive call sets supportsAllDrives / includeItemsFromAllDrives so this
works for files living in a Shared Drive (where a service account CAN create
files — unlike My Drive, where service accounts have no storage quota).
"""

import io
import re
from dataclasses import dataclass, asdict

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from config import (
    DRIVE_ROOT_FOLDER_ID,
    TRANSCRIPTS_FOLDER_ID,
    TRANSCRIPTS_FOLDER_NAME,
    VIDEO_EXTENSIONS,
    SKIP_FOLDERS,
    MIN_FILE_SIZE_BYTES,
)

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}


@dataclass
class TalkVideo:
    drive_id: str
    name: str               # original Drive filename, incl. extension
    size_bytes: int
    drive_path: str         # e.g. "Mon. June 8, 2026/AI Keynote ....mp4"
    day_folder: str         # immediate parent folder name
    talk_date: str          # ISO "YYYY-MM-DD" (best effort) or ""
    created_time: str       # Drive createdTime (RFC3339) or ""


# ── Auth ──────────────────────────────────────────────────────────────────
def build_drive_service(readonly: bool = False):
    scope = (
        "https://www.googleapis.com/auth/drive.readonly"
        if readonly
        else "https://www.googleapis.com/auth/drive"
    )
    creds, _ = google.auth.default(scopes=[scope])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ── Date parsing ────────────────────────────────────────────────────────────
def parse_talk_date(day_folder: str, created_time: str = "") -> str:
    """
    Parse a day-folder name like 'Mon. June 8, 2026', 'Sun May 31, 2026',
    'Thur June 4, 2026' into ISO 'YYYY-MM-DD'. Falls back to the Drive
    createdTime date if the folder name can't be parsed.
    """
    if day_folder:
        m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", day_folder)
        if m:
            month = _MONTHS.get(m.group(1).lower())
            if month:
                try:
                    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"
                except ValueError:
                    pass
    if created_time and len(created_time) >= 10:
        return created_time[:10]
    return ""


# ── Inventory ───────────────────────────────────────────────────────────────
def _is_video(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS)


def _list_children(service, folder_id: str):
    """Yield all non-trashed children of a folder, handling pagination."""
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, size, createdTime, shortcutDetails)",
            pageSize=1000,
            orderBy="name",
            corpora="allDrives",          # required so a service account sees Shared Drive items
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()
        for item in resp.get("files", []):
            yield item
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def run_inventory(service, root_id: str = "") -> list[TalkVideo]:
    """Recursively collect every talk video under the root folder."""
    root_id = root_id or DRIVE_ROOT_FOLDER_ID
    videos: list[TalkVideo] = []

    def walk(folder_id: str, path: str, day_folder: str, depth: int):
        if depth > 8:
            return
        for item in _list_children(service, folder_id):
            name = item["name"]
            mime = item.get("mimeType", "")
            item_id = item["id"]

            if mime == SHORTCUT_MIME:
                tgt = item.get("shortcutDetails", {})
                if tgt.get("targetMimeType") == FOLDER_MIME:
                    mime = FOLDER_MIME
                    item_id = tgt.get("targetId", item_id)

            if mime == FOLDER_MIME:
                if any(s in name.lower() for s in SKIP_FOLDERS):
                    continue
                child_path = f"{path}/{name}" if path else name
                # The day-folder is the first level under the root.
                child_day = name if depth == 0 else day_folder
                walk(item_id, child_path, child_day, depth + 1)
            elif _is_video(name):
                size = int(item.get("size", 0) or 0)
                if size < MIN_FILE_SIZE_BYTES:
                    continue
                created = item.get("createdTime", "")
                videos.append(TalkVideo(
                    drive_id=item_id,
                    name=name,
                    size_bytes=size,
                    drive_path=f"{path}/{name}" if path else name,
                    day_folder=day_folder,
                    talk_date=parse_talk_date(day_folder, created),
                    created_time=created,
                ))

    walk(root_id, "", "", 0)
    return videos


# ── Output folder ─────────────────────────────────────────────────────────
def _escape_q(value: str) -> str:
    """Escape a literal for use inside a Drive API query string (single-quoted)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def find_transcripts_folder(service, root_id: str = "") -> str | None:
    """Return the transcripts folder id if it exists; never creates (read-only)."""
    if TRANSCRIPTS_FOLDER_ID:
        return TRANSCRIPTS_FOLDER_ID
    root_id = root_id or DRIVE_ROOT_FOLDER_ID
    resp = service.files().list(
        q=(f"'{root_id}' in parents and trashed=false "
           f"and mimeType='{FOLDER_MIME}' and name='{_escape_q(TRANSCRIPTS_FOLDER_NAME)}'"),
        fields="files(id, name)",
        corpora="allDrives",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    found = resp.get("files", [])
    return found[0]["id"] if found else None


def get_or_create_transcripts_folder(service, root_id: str = "") -> str:
    """Return the transcripts folder id, creating it under the root if needed."""
    existing = find_transcripts_folder(service, root_id)
    if existing:
        return existing

    root_id = root_id or DRIVE_ROOT_FOLDER_ID
    created = service.files().create(
        body={
            "name": TRANSCRIPTS_FOLDER_NAME,
            "mimeType": FOLDER_MIME,
            "parents": [root_id],
        },
        fields="id",
        supportsAllDrives=True,
    ).execute()
    print(f"📂 Created transcripts folder '{TRANSCRIPTS_FOLDER_NAME}' ({created['id']})")
    return created["id"]


def list_existing_transcript_names(service, folder_id: str) -> set[str]:
    """Names of .txt files already present in the transcripts folder."""
    names = set()
    for item in _list_children(service, folder_id):
        if item.get("mimeType") != FOLDER_MIME and item["name"].lower().endswith(".txt"):
            names.add(item["name"])
    return names


def upload_transcript(service, folder_id: str, filename: str, text: str) -> str:
    """Create a UTF-8 .txt file in the transcripts folder. Returns the file id."""
    media = MediaIoBaseUpload(
        io.BytesIO(text.encode("utf-8")),
        mimetype="text/plain; charset=utf-8",
        resumable=False,
    )
    created = service.files().create(
        body={"name": filename, "parents": [folder_id], "mimeType": "text/plain"},
        media_body=media,
        fields="id, name, webViewLink, driveId",
        supportsAllDrives=True,
    ).execute()
    return created["id"]
