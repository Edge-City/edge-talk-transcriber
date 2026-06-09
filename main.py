#!/usr/bin/env python3
"""
Edge Talk Transcriber — orchestrator.

Finds talk videos in a Google Drive folder, transcribes the ones that don't yet
have a transcript, and writes each transcript as a .txt into a flat output
folder (date-prefixed filenames). Idempotent: re-running only processes new
talks, so it's safe to run on a schedule.

Usage:
  python main.py                      # full run (inventory -> transcribe new -> upload)
  python main.py --dry-run            # list talks + what would be transcribed; no work
  python main.py --limit 1            # process at most 1 new talk (good for a first test)
  python main.py --local-out ./out    # write transcripts to a local folder, not Drive
  python main.py --local-file talk.mp4 [--local-out ./out]
                                      # transcribe a single local video; no Drive at all
"""

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import config
from formatter import build_transcript, transcript_filename


def _today() -> str:
    return date.today().isoformat()


def _make_local_video(path: Path):
    """A TalkVideo-like stand-in for a local file (no Drive)."""
    from drive_client import TalkVideo
    return TalkVideo(
        drive_id=f"local-{path.stem}",
        name=path.name,
        size_bytes=path.stat().st_size,
        drive_path=path.name,
        day_folder="",
        talk_date="",
        created_time="",
    )


def run_local_file(video_path: Path, out_dir: Path) -> None:
    """Transcribe one local video file end-to-end (engine + formatter only)."""
    import media
    import transcribe

    out_dir.mkdir(parents=True, exist_ok=True)
    video = _make_local_video(video_path)

    wav = out_dir / f"{video_path.stem}.wav"
    print(f"🎬 {video.name}")
    media.extract_audio(video_path, wav)
    result = transcribe.transcribe(wav)
    media.cleanup(wav)

    text = build_transcript(video, result, video.talk_date, config.WHISPER_MODEL, _today())
    fname = transcript_filename(video, video.talk_date)
    (out_dir / fname).write_text(text, encoding="utf-8")
    print(f"✅ Wrote {out_dir / fname}  ({len(result['segments'])} segments)")


def run_pipeline(limit: int | None, dry_run: bool, local_out: Path | None) -> None:
    import drive_client
    from cache import load_cache, save_cache, IS_CLOUD

    config.require("DRIVE_ROOT_FOLDER_ID")
    # On Cloud Run the cache lives in GCS; without a bucket the "done" state can't
    # persist and every run would re-transcribe everything. Fail fast instead.
    if IS_CLOUD:
        config.require("GCS_BUCKET_NAME")
    service = drive_client.build_drive_service(readonly=dry_run)

    print("🔍 Scanning Drive for talk videos...")
    videos = drive_client.run_inventory(service)
    total_gb = sum(v.size_bytes for v in videos) / 1024**3
    print(f"   Found {len(videos)} videos ({total_gb:.1f} GB)\n")

    cache = load_cache(config.CACHE_FILE)
    done = cache.get("done", {})

    # Resolve output target + which transcripts already exist (so we skip them).
    existing_names: set[str] = set()
    folder_id = None
    if local_out is None:
        if dry_run:
            # Read-only: find the folder if it exists, but never create it.
            folder_id = drive_client.find_transcripts_folder(service)
            if folder_id:
                existing_names = drive_client.list_existing_transcript_names(service, folder_id)
        else:
            folder_id = drive_client.get_or_create_transcripts_folder(service)
            cache["transcripts_folder_id"] = folder_id
            existing_names = drive_client.list_existing_transcript_names(service, folder_id)

    todo = []
    for v in videos:
        fname = transcript_filename(v, v.talk_date)
        if v.drive_id in done or fname in existing_names:
            continue
        todo.append((v, fname))

    print(f"📋 {len(todo)} new / {len(videos) - len(todo)} already done")
    if dry_run:
        for v, fname in todo:
            print(f"   • {fname}  ({v.size_bytes/1024**2:.0f} MB)")
        print("\n(dry run — nothing transcribed)")
        return

    # Parallel backfill: when the job runs with multiple Cloud Run tasks, each
    # task takes a disjoint slice of the todo list (Cloud Run sets these vars).
    # Slices are disjoint, so no two tasks ever process the same talk.
    task_index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    if task_count > 1:
        todo = todo[task_index::task_count]
        print(f"🔀 Task {task_index + 1}/{task_count} handles {len(todo)} of them")
    for v, fname in todo:
        print(f"   • {fname}  ({v.size_bytes/1024**2:.0f} MB)")
    if not todo:
        print("\nNothing to do. ✅")
        return

    import media
    import transcribe

    if local_out is not None:
        local_out.mkdir(parents=True, exist_ok=True)

    processed = 0
    for v, fname in todo:
        if limit is not None and processed >= limit:
            print(f"\n⏹  Reached --limit {limit}; stopping.")
            break
        print(f"\n🎬 {v.drive_path}")
        wav = None
        try:
            wav = media.prepare_audio(service, v)
            result = transcribe.transcribe(wav)
            text = build_transcript(v, result, v.talk_date, config.WHISPER_MODEL, _today())

            if local_out is not None:
                (local_out / fname).write_text(text, encoding="utf-8")
                print(f"   ✅ {local_out / fname}  ({len(result['segments'])} segments)")
            else:
                file_id = drive_client.upload_transcript(service, folder_id, fname, text)
                done[v.drive_id] = {"transcript_id": file_id, "name": fname, "at": _today()}
                # Skip cache writes when sharded (parallel tasks would clobber the
                # single cache file); the Drive filename check handles idempotency.
                if task_count == 1:
                    cache["done"] = done
                    save_cache(cache, config.CACHE_FILE)
                print(f"   ✅ Uploaded {fname}  ({len(result['segments'])} segments)")
            processed += 1
        except Exception as e:
            print(f"   ❌ Failed: {e}")
        finally:
            if wav is not None:
                media.cleanup(wav)

    print(f"\n✅ Done. Transcribed {processed} talk(s).")


def main() -> None:
    p = argparse.ArgumentParser(description="Transcribe talk videos from Google Drive.")
    p.add_argument("--dry-run", action="store_true", help="List talks; transcribe nothing.")
    p.add_argument("--limit", type=int, default=None, help="Process at most N new talks.")
    p.add_argument("--local-out", type=str, default=None,
                   help="Write transcripts to this local folder instead of Drive.")
    p.add_argument("--local-file", type=str, default=None,
                   help="Transcribe a single local video file (no Drive at all).")
    args = p.parse_args()

    local_out = Path(args.local_out) if args.local_out else None

    if args.local_file:
        run_local_file(Path(args.local_file), local_out or Path("./out"))
        return

    run_pipeline(limit=args.limit, dry_run=args.dry_run, local_out=local_out)


if __name__ == "__main__":
    sys.exit(main())
