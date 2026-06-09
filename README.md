# Edge Talk Transcriber

Automatically transcribe every talk video in a Google Drive folder and write a
clean, timestamped `.txt` transcript for each one into an output subfolder.
Designed to run hands-off on a nightly schedule: it only transcribes talks that
don't already have a transcript, so re-runs are cheap and safe.

Built for the [Edge Esmeralda](https://www.edgeesmeralda.com) talk archive, but
fully generic: point it at any Drive folder of videos.

```
Google Drive (talk videos)
      │  inventory (recursive)
      ▼
  download each new video ──▶ ffmpeg → 16kHz mono audio
      ▼
  faster-whisper (large-v3-turbo, CPU) → timestamped segments
      ▼
  formatted transcript .txt ──▶ Google Drive "Transcripts" subfolder
```

## What a transcript looks like

```
# AI Keynote: Supercooperation

Date: 2026-06-08
Source video: AI Keynote_ Supercooperation.mp4
Drive: https://drive.google.com/file/d/<id>/view
Duration: 00:48:12
Transcribed: 2026-06-09 · whisper large-v3-turbo · language: en

---

[00:00] Welcome everyone. Today we're going to talk about how AI changes the
shape of collaboration at scale...

[00:34] The core idea behind supercooperation is...
```

## Why faster-whisper (not a hosted API)

- **Accurate timestamps at any length.** Whisper aligns timestamps from the
  audio itself, so there's no drift on multi-hour talks. (Hosted LLM
  transcription can drift badly and silently truncate long transcripts.)
- **No API keys.** MIT-licensed, runs CPU-only. Anyone can clone and run this
  in their own cloud with zero credentials beyond their own Google account.
- **Cheap.** Roughly $0.10–0.15 per audio hour of Cloud Run compute.

## Prerequisites

- Python 3.11+
- `ffmpeg` (`brew install ffmpeg` / `apt-get install ffmpeg`)
- A Google account with access to the Drive folder of videos
- For cloud deploy: a GCP project (billing enabled) + `gcloud` CLI

## Authentication

This tool **never uses a service-account key file.** It relies on Application
Default Credentials (ADC):

- **Local:** `gcloud auth application-default login`
- **Cloud Run:** attach a service account to the job (`--service-account`);
  credentials are provided automatically by the metadata server.

> Writing transcripts back to Drive requires the destination folder to live in
> a **Shared Drive** (service accounts have no My-Drive storage quota and cannot
> own files there). Add your runtime service account as a **Content manager** on
> that Shared Drive. If you'd rather write to a personal My Drive, run locally
> with your own user credentials instead of a service account.

## Configure

```bash
cp .env.example .env
# edit .env with your DRIVE_ROOT_FOLDER_ID, GCP_PROJECT_ID, GCS_BUCKET_NAME, etc.
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) See what would be transcribed (no work, no writes):
python main.py --dry-run

# 2) Transcribe just one talk to a LOCAL folder (no Drive writes):
python main.py --limit 1 --local-out ./out

# 3) Transcribe a single local video file (no Drive at all):
python main.py --local-file /path/to/talk.mp4 --local-out ./out

# 4) Full run — transcribe all new talks, upload to the Drive subfolder:
python main.py
```

## Deploy to Cloud Run (nightly, automatic)

```bash
gcloud auth login
./deploy.sh build       # build image + create/update the Cloud Run Job
./deploy.sh run         # one-off run now (also does the initial backfill)
./deploy.sh schedule    # create the nightly Cloud Scheduler trigger
./deploy.sh logs        # tail the latest run
```

The job is idempotent (state cached in GCS), so if a long backfill exceeds the
task timeout it simply resumes on the next execution.

## CLI reference

| Flag | Effect |
|------|--------|
| `--dry-run` | List talks and what would be transcribed; do nothing else. |
| `--limit N` | Process at most N new talks (good for a first test). |
| `--local-out DIR` | Write transcripts to a local folder instead of Drive. |
| `--local-file PATH` | Transcribe one local video file; no Drive at all. |

## Configuration (env vars)

See [`.env.example`](.env.example) for the full list. Key ones:
`DRIVE_ROOT_FOLDER_ID`, `TRANSCRIPTS_FOLDER_NAME`, `GCP_PROJECT_ID`,
`GCS_BUCKET_NAME`, `WHISPER_MODEL`, `WHISPER_LANGUAGE`, `SKIP_FOLDERS`, `WORKDIR`.

## Notes & limits

- **Large files / memory.** Each video is downloaded to `WORKDIR` (default
  `/tmp`, which on Cloud Run is RAM-backed), then deleted right after audio is
  extracted. The default deploy uses 16 GiB memory to handle videos up to ~7 GB.
  For larger videos, mount a disk or GCS bucket and set `WORKDIR` to it.
- **Engine swap.** faster-whisper is the only engine wired in. The code is
  structured so a hosted engine (e.g. Gemini) could be added behind a flag.
- **License.** MIT.
