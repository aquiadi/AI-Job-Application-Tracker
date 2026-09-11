#!/usr/bin/env bash
# Delete everything this project created in the sandbox project.
#
# The way an audition build costs money is being forgotten about. This is the command
# that stops that, and it names what it will delete before deleting it.
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
[[ -n "${PROJECT}" && "${PROJECT}" != CHANGEME-* ]] || { echo "GOOGLE_CLOUD_PROJECT is not set" >&2; exit 1; }

echo "This will delete, in project ${PROJECT}:"
echo "  - Cloud Run services jobtrack-api, jobtrack-web"
echo "  - Cloud Run job jobtrack-migrate"
echo "  - the AlloyDB cluster named by DB_ALLOYDB_INSTANCE_URI, and its data"
echo "  - buckets ${GCS_UPLOADS_BUCKET:-?}, ${GCS_RAW_BUCKET:-?}, ${GCS_ARTIFACTS_BUCKET:-?}"
echo
read -r -p "Type the project id to confirm: " CONFIRM
[[ "${CONFIRM}" == "${PROJECT}" ]] || { echo "Not confirmed. Nothing deleted."; exit 1; }

for service in jobtrack-api jobtrack-web; do
  gcloud run services delete "${service}" --project "${PROJECT}" --region "${REGION}" --quiet || true
done
gcloud run jobs delete jobtrack-migrate --project "${PROJECT}" --region "${REGION}" --quiet || true

if [[ -n "${DB_ALLOYDB_INSTANCE_URI:-}" ]]; then
  CLUSTER="$(echo "${DB_ALLOYDB_INSTANCE_URI}" | sed -n 's|.*/clusters/\([^/]*\)/.*|\1|p')"
  LOCATION="$(echo "${DB_ALLOYDB_INSTANCE_URI}" | sed -n 's|.*/locations/\([^/]*\)/.*|\1|p')"
  # The cluster is the expensive thing. AlloyDB bills hourly for as long as it exists,
  # whether or not anything queries it.
  gcloud alloydb clusters delete "${CLUSTER}" --project "${PROJECT}" \
    --region "${LOCATION}" --force --quiet || true
fi

for bucket in "${GCS_UPLOADS_BUCKET:-}" "${GCS_RAW_BUCKET:-}" "${GCS_ARTIFACTS_BUCKET:-}"; do
  [[ -n "${bucket}" ]] && gcloud storage rm -r "gs://${bucket}" --project "${PROJECT}" || true
done

echo
echo "Done. Check the billing page tomorrow: deletion is not always instant, and this"
echo "script does not touch anything it did not create."
