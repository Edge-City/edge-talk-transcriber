#!/usr/bin/env bash
#
# Deploy the Edge Talk Transcriber as a Cloud Run Job, and (optionally) schedule
# it to run nightly via Cloud Scheduler.
#
# All identifiers come from your .env (gitignored) or the environment — nothing
# org-specific is hard-coded here, so this script is safe to publish.
#
# Prerequisites:
#   - gcloud CLI installed + authenticated:  gcloud auth login
#   - A GCP project with billing enabled
#   - The runtime service account (RUNTIME_SA) added as a *Content manager* on
#     the Shared Drive that holds the talks + transcripts folder.
#
# Usage:
#   ./deploy.sh build        # build + push image, create/update the job
#   ./deploy.sh schedule     # create the nightly Cloud Scheduler trigger
#   ./deploy.sh run          # execute the job once, now
#   ./deploy.sh logs         # tail the most recent execution logs

set -euo pipefail

# Load .env if present (local convenience; CI can set env vars directly).
if [[ -f .env ]]; then set -a; source .env; set +a; fi

# ── Required / defaulted settings ───────────────────────────────────────────
: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID in .env}"
REGION="${REGION:-us-central1}"
JOB_NAME="${JOB_NAME:-edge-talk-transcriber}"
REPO="${AR_REPO:-edge-talk-transcriber}"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${REPO}/${JOB_NAME}"
RUNTIME_SA="${RUNTIME_SA:?Set RUNTIME_SA in .env (the SA the job runs as)}"
# Identity Cloud Scheduler uses to invoke the job. Defaults to the runtime SA
# (already exists, already has the needed access) so no extra SA is created.
SCHEDULER_SA="${SCHEDULER_SA:-${RUNTIME_SA}}"
SCHEDULE="${SCHEDULE:-0 3 * * *}"          # 3am, daily
TIMEZONE="${TIMEZONE:-America/New_York}"
MEMORY="${MEMORY:-16Gi}"                   # safe for videos up to ~14GB in /tmp (model+runtime add ~1.8GB); set WORKDIR to a mounted disk for larger
CPU="${CPU:-8}"
TASK_TIMEOUT="${TASK_TIMEOUT:-86400}"      # 24h. The GCS cache lets a re-run skip already-uploaded talks; a talk in progress when the timeout fires restarts from scratch.

cmd="${1:-build}"

build() {
  echo "=== Enabling APIs ==="
  gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com cloudscheduler.googleapis.com \
    drive.googleapis.com storage.googleapis.com --project="${GCP_PROJECT_ID}"

  echo "=== Ensuring Artifact Registry repo exists ==="
  gcloud artifacts repositories describe "${REPO}" --location="${REGION}" \
    --project="${GCP_PROJECT_ID}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${REPO}" --repository-format=docker \
    --location="${REGION}" --project="${GCP_PROJECT_ID}"

  echo "=== Building + pushing image via Cloud Build ==="
  gcloud builds submit --tag "${IMAGE}" --project="${GCP_PROJECT_ID}" .

  echo "=== Creating/updating the Cloud Run Job ==="
  # Pass non-secret config to the job as env vars. (Secrets are never used:
  # the job authenticates via its attached service account.) The leading
  # "^@@^" tells gcloud the pair delimiter is "@@", so values may contain
  # commas (e.g. a multi-folder SKIP_FOLDERS list).
  ENV_VARS="^@@^DRIVE_ROOT_FOLDER_ID=${DRIVE_ROOT_FOLDER_ID}@@GCP_PROJECT_ID=${GCP_PROJECT_ID}@@GCS_BUCKET_NAME=${GCS_BUCKET_NAME}@@GCS_CACHE_KEY=${GCS_CACHE_KEY:-pipeline-state/transcriber-cache.json}@@TRANSCRIPTS_FOLDER_NAME=${TRANSCRIPTS_FOLDER_NAME:-Transcripts}@@WHISPER_MODEL=${WHISPER_MODEL:-large-v3-turbo}@@WHISPER_COMPUTE_TYPE=${WHISPER_COMPUTE_TYPE:-int8}@@WHISPER_LANGUAGE=${WHISPER_LANGUAGE:-}@@WHISPER_BEAM_SIZE=${WHISPER_BEAM_SIZE:-5}@@SKIP_FOLDERS=${SKIP_FOLDERS:-transcripts}@@MIN_FILE_SIZE_BYTES=${MIN_FILE_SIZE_BYTES:-512000}"
  [[ -n "${TRANSCRIPTS_FOLDER_ID:-}" ]] && ENV_VARS="${ENV_VARS}@@TRANSCRIPTS_FOLDER_ID=${TRANSCRIPTS_FOLDER_ID}"

  if gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    gcloud run jobs update "${JOB_NAME}" --image "${IMAGE}" \
      --service-account "${RUNTIME_SA}" --region "${REGION}" --project "${GCP_PROJECT_ID}" \
      --memory "${MEMORY}" --cpu "${CPU}" --task-timeout "${TASK_TIMEOUT}" \
      --max-retries 1 --execution-environment gen2 \
      --set-env-vars "${ENV_VARS}"
  else
    gcloud run jobs create "${JOB_NAME}" --image "${IMAGE}" \
      --service-account "${RUNTIME_SA}" --region "${REGION}" --project "${GCP_PROJECT_ID}" \
      --memory "${MEMORY}" --cpu "${CPU}" --task-timeout "${TASK_TIMEOUT}" \
      --max-retries 1 --execution-environment gen2 \
      --set-env-vars "${ENV_VARS}"
  fi
  echo "✅ Job ready. Test it:  ./deploy.sh run"
}

schedule() {
  echo "=== Ensuring scheduler service account + invoker role ==="
  local sa_id="${SCHEDULER_SA%%@*}"
  gcloud iam service-accounts describe "${SCHEDULER_SA}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "${sa_id}" \
      --display-name="Cloud Scheduler -> ${JOB_NAME}" --project="${GCP_PROJECT_ID}"
  # Scope run.invoker to THIS job only (least privilege), not the whole project.
  gcloud run jobs add-iam-policy-binding "${JOB_NAME}" --region="${REGION}" \
    --member="serviceAccount:${SCHEDULER_SA}" --role="roles/run.invoker" \
    --project="${GCP_PROJECT_ID}" >/dev/null

  local uri="https://run.googleapis.com/v2/projects/${GCP_PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run"
  echo "=== Creating/updating Cloud Scheduler job (${SCHEDULE} ${TIMEZONE}) ==="
  if gcloud scheduler jobs describe "${JOB_NAME}-nightly" --location="${REGION}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "${JOB_NAME}-nightly" --location="${REGION}" \
      --schedule="${SCHEDULE}" --time-zone="${TIMEZONE}" --uri="${uri}" --http-method=POST \
      --oauth-service-account-email="${SCHEDULER_SA}" --project="${GCP_PROJECT_ID}"
  else
    gcloud scheduler jobs create http "${JOB_NAME}-nightly" --location="${REGION}" \
      --schedule="${SCHEDULE}" --time-zone="${TIMEZONE}" --uri="${uri}" --http-method=POST \
      --oauth-service-account-email="${SCHEDULER_SA}" --project="${GCP_PROJECT_ID}"
  fi
  echo "✅ Nightly trigger set."
}

run() {
  gcloud run jobs execute "${JOB_NAME}" --region "${REGION}" --project "${GCP_PROJECT_ID}"
}

logs() {
  gcloud logging read "resource.labels.job_name=${JOB_NAME}" \
    --project "${GCP_PROJECT_ID}" --format='value(textPayload)' --limit=80
}

case "${cmd}" in
  build) build ;;
  schedule) schedule ;;
  run) run ;;
  logs) logs ;;
  *) echo "Usage: ./deploy.sh [build|schedule|run|logs]"; exit 1 ;;
esac
