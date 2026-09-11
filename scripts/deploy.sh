#!/usr/bin/env bash
# Deploy to Cloud Run with gcloud. Terraform is deferred; docs/ROADMAP.md says why.
#
# Everything this needs that is not in .env is a placeholder you fill once:
#   GOOGLE_CLOUD_PROJECT     the sandbox project id
#   DB_ALLOYDB_INSTANCE_URI  projects/P/locations/R/clusters/C/instances/I
#   GCS_*_BUCKET             three bucket names
#   WEB_ORIGINS              the deployed web app's origin
#
# It refuses to run rather than deploying something half-configured, because a Cloud
# Run service that starts and then fails on its first request costs the same as one
# that works and is harder to diagnose.
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
REPO="${ARTIFACT_REPO:-jobtrack}"
TAG="$(git rev-parse --short HEAD)"

fail() { echo "error: $*" >&2; exit 1; }

[[ -n "${PROJECT}" && "${PROJECT}" != CHANGEME-* ]] \
  || fail "GOOGLE_CLOUD_PROJECT is not set. Put your project id in .env."
gcloud auth print-access-token >/dev/null 2>&1 \
  || fail "gcloud has no credentials. Run: gcloud auth login"

for name in DB_ALLOYDB_INSTANCE_URI GCS_UPLOADS_BUCKET GCS_RAW_BUCKET GCS_ARTIFACTS_BUCKET; do
  [[ -n "${!name:-}" ]] || fail "${name} is not set. See .env.example."
done

IMAGE_HOST="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}"

echo "==> project ${PROJECT}, region ${REGION}, tag ${TAG}"

# One repository for both images. Created here rather than assumed, so a fresh
# project needs no manual step before the first deploy.
gcloud artifacts repositories describe "${REPO}" \
  --location "${REGION}" --project "${PROJECT}" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "${REPO}" \
       --repository-format=docker --location "${REGION}" --project "${PROJECT}" \
       --description="Job Application Tracker images"

echo "==> building images"
gcloud builds submit --project "${PROJECT}" --tag "${IMAGE_HOST}/api:${TAG}" \
  --file services/api/Dockerfile .
gcloud builds submit --project "${PROJECT}" --tag "${IMAGE_HOST}/web:${TAG}" \
  --file apps/web/Dockerfile .

# Migrations run as a job, not on service start. A service that migrates at boot
# races itself the moment there is more than one instance, and Cloud Run starts
# more than one instance whenever it feels like it.
echo "==> running migrations"
gcloud run jobs deploy jobtrack-migrate \
  --project "${PROJECT}" --region "${REGION}" \
  --image "${IMAGE_HOST}/api:${TAG}" \
  --command uv --args "run,alembic,-c,packages/core/alembic.ini,upgrade,head" \
  --set-env-vars "ENVIRONMENT=cloud,GOOGLE_CLOUD_PROJECT=${PROJECT},DB_ALLOYDB_INSTANCE_URI=${DB_ALLOYDB_INSTANCE_URI}" \
  --service-account "jobtrack-migrate@${PROJECT}.iam.gserviceaccount.com" \
  --vpc-egress all-traffic --network default --subnet default \
  --max-retries 1 --execute-now --wait

echo "==> deploying api"
gcloud run deploy jobtrack-api \
  --project "${PROJECT}" --region "${REGION}" \
  --image "${IMAGE_HOST}/api:${TAG}" \
  --service-account "jobtrack-api@${PROJECT}.iam.gserviceaccount.com" \
  --set-env-vars "ENVIRONMENT=cloud,SERVICE_NAME=api,LLM_BACKEND=vertex,GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_REGION=${REGION},DB_ALLOYDB_INSTANCE_URI=${DB_ALLOYDB_INSTANCE_URI},GCS_UPLOADS_BUCKET=${GCS_UPLOADS_BUCKET},GCS_RAW_BUCKET=${GCS_RAW_BUCKET},GCS_ARTIFACTS_BUCKET=${GCS_ARTIFACTS_BUCKET},WEB_ORIGINS=${WEB_ORIGINS:-}" \
  --vpc-egress all-traffic --network default --subnet default \
  --min-instances 0 --max-instances 4 --cpu 1 --memory 1Gi \
  --allow-unauthenticated

API_URL="$(gcloud run services describe jobtrack-api --project "${PROJECT}" \
  --region "${REGION}" --format 'value(status.url)')"

echo "==> deploying web against ${API_URL}"
gcloud run deploy jobtrack-web \
  --project "${PROJECT}" --region "${REGION}" \
  --image "${IMAGE_HOST}/web:${TAG}" \
  --service-account "jobtrack-web@${PROJECT}.iam.gserviceaccount.com" \
  --set-env-vars "NEXT_PUBLIC_API_URL=${API_URL},NEXT_PUBLIC_FIREBASE_PROJECT_ID=${PROJECT},NEXT_PUBLIC_FIREBASE_API_KEY=${NEXT_PUBLIC_FIREBASE_API_KEY:-}" \
  --min-instances 0 --max-instances 4 --cpu 1 --memory 512Mi \
  --allow-unauthenticated

WEB_URL="$(gcloud run services describe jobtrack-web --project "${PROJECT}" \
  --region "${REGION}" --format 'value(status.url)')"

echo
echo "api  ${API_URL}"
echo "web  ${WEB_URL}"
echo
echo "Set WEB_ORIGINS=${WEB_URL} in .env and redeploy the api, or the browser will be"
echo "refused by CORS. It is an explicit allowlist on purpose."
