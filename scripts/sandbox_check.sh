#!/usr/bin/env bash
#
# Verify that the Google Cloud sandbox project can actually run this system.
#
# Enabling an API is not the same as being able to use it. A sandbox project can
# have an API enabled and still refuse the calls we depend on, because of org
# policy, missing IAM, a region restriction, or an un-initialised service. So
# every API here gets two checks: it is enabled, and one real call against it
# succeeds.
#
# Exit codes:
#   0  everything required is usable
#   1  a non-blocking check failed (the build can continue, with caveats)
#   2  AlloyDB or Vertex AI is unusable (hard stop, nothing else matters)
#   3  bad invocation or missing prerequisites
#
# Usage:
#   scripts/sandbox_check.sh                 enable missing APIs, then verify
#   scripts/sandbox_check.sh --verify-only   verify without enabling anything

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load .env if present so a developer does not have to export everything by hand.
# shellcheck disable=SC1091
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  source "${REPO_ROOT}/.env"
  set +a
fi

VERIFY_ONLY=0
for arg in "$@"; do
  case "${arg}" in
    --verify-only) VERIFY_ONLY=1 ;;
    -h | --help)
      sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: ${arg}" >&2
      exit 3
      ;;
  esac
done

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
EXTRACT_MODEL="${GEMINI_EXTRACT_MODEL:-gemini-3.5-flash-lite}"
GENERATE_MODEL="${GEMINI_GENERATE_MODEL:-gemini-3.5-flash}"
EMBEDDING_MODEL="${EMBEDDING_MODEL:-gemini-embedding-001}"
EMBEDDING_REGION="${EMBEDDING_REGION:-${REGION}}"

# Required APIs. Order matters only for readability.
readonly REQUIRED_APIS=(
  aiplatform.googleapis.com
  alloydb.googleapis.com
  run.googleapis.com
  pubsub.googleapis.com
  cloudtasks.googleapis.com
  cloudscheduler.googleapis.com
  identitytoolkit.googleapis.com
  secretmanager.googleapis.com
  storage.googleapis.com
)

# APIs the above depend on in practice. AlloyDB needs private services access,
# which lives behind servicenetworking; Cloud Run source deploys need Cloud Build
# and Artifact Registry; Direct VPC egress needs compute.
readonly SUPPORTING_APIS=(
  compute.googleapis.com
  servicenetworking.googleapis.com
  artifactregistry.googleapis.com
  cloudbuild.googleapis.com
  iam.googleapis.com
  cloudresourcemanager.googleapis.com
)

# name -> outcome ("ok" | "warn: ..." | "fail: ...")
declare -a RESULT_NAMES=()
declare -a RESULT_STATES=()
declare -a RESULT_NOTES=()

HARD_FAIL=0
SOFT_FAIL=0

record() {
  local name="$1" state="$2" note="${3:-}"
  RESULT_NAMES+=("${name}")
  RESULT_STATES+=("${state}")
  RESULT_NOTES+=("${note}")
  case "${state}" in
    FAIL) HARD_FAIL=1 ;;
    WARN) SOFT_FAIL=1 ;;
  esac
  printf '  %-22s %s%s\n' "${name}" "${state}" "${note:+ — ${note}}"
}

# Run a command, discarding stdout, capturing stderr's first useful line.
probe() {
  local out
  if out="$("$@" 2>&1 >/dev/null)"; then
    PROBE_ERR=""
    return 0
  fi
  PROBE_ERR="$(printf '%s' "${out}" | grep -v '^$' | head -1 | cut -c1-140)"
  return 1
}

step() { printf '\n%s\n' "$1"; }

# --------------------------------------------------------------------------
# Prerequisites
# --------------------------------------------------------------------------

step "Prerequisites"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "  gcloud not found. Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install" >&2
  exit 3
fi
printf '  %-22s %s\n' "gcloud" "$(gcloud version 2>/dev/null | head -1)"

if ! ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)" || [[ -z "${ACCOUNT}" ]]; then
  echo "  No active gcloud credentials. Run: gcloud auth login && gcloud auth application-default login" >&2
  exit 3
fi
printf '  %-22s %s\n' "account" "${ACCOUNT}"

if [[ -z "${PROJECT_ID}" ]]; then
  cat >&2 <<'EOF'
  No project set. Either:
    export GOOGLE_CLOUD_PROJECT=your-sandbox-project-id
  or:
    gcloud config set project your-sandbox-project-id
EOF
  exit 3
fi
printf '  %-22s %s\n' "project" "${PROJECT_ID}"
printf '  %-22s %s\n' "region" "${REGION}"
printf '  %-22s %s\n' "vertex location" "${VERTEX_LOCATION}"

if ! gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "  Cannot read project ${PROJECT_ID}. Wrong id, or this account lacks access." >&2
  exit 3
fi

BILLING="$(gcloud beta billing projects describe "${PROJECT_ID}" \
  --format='value(billingEnabled)' 2>/dev/null || echo "unknown")"
case "${BILLING}" in
  True) printf '  %-22s %s\n' "billing" "enabled" ;;
  False)
    echo "  Billing is NOT enabled on ${PROJECT_ID}. Vertex AI and AlloyDB will both refuse." >&2
    exit 2
    ;;
  *) printf '  %-22s %s\n' "billing" "could not determine (missing billing.viewer; continuing)" ;;
esac

# --------------------------------------------------------------------------
# API enablement
# --------------------------------------------------------------------------

step "API enablement"

ENABLED="$(gcloud services list --enabled --project="${PROJECT_ID}" --format='value(config.name)' 2>/dev/null || true)"

missing=()
for api in "${REQUIRED_APIS[@]}" "${SUPPORTING_APIS[@]}"; do
  grep -qx "${api}" <<<"${ENABLED}" || missing+=("${api}")
done

if [[ ${#missing[@]} -eq 0 ]]; then
  echo "  all ${#REQUIRED_APIS[@]} required and ${#SUPPORTING_APIS[@]} supporting APIs already enabled"
elif [[ ${VERIFY_ONLY} -eq 1 ]]; then
  echo "  not enabled (--verify-only, so not enabling): ${missing[*]}"
else
  echo "  enabling: ${missing[*]}"
  if ! gcloud services enable "${missing[@]}" --project="${PROJECT_ID}" 2>&1 | sed 's/^/    /'; then
    echo "    enable call failed; re-checking individually" >&2
  fi
  ENABLED="$(gcloud services list --enabled --project="${PROJECT_ID}" --format='value(config.name)' 2>/dev/null || true)"
fi

for api in "${REQUIRED_APIS[@]}"; do
  if grep -qx "${api}" <<<"${ENABLED}"; then
    record "${api%%.*} api" "OK" "enabled"
  else
    record "${api%%.*} api" "FAIL" "not enabled"
  fi
done

# --------------------------------------------------------------------------
# Real calls. Enabled != usable.
# --------------------------------------------------------------------------

step "Usability probes"

TOKEN="$(gcloud auth print-access-token 2>/dev/null || true)"
if [[ -z "${TOKEN}" ]]; then
  echo "  could not mint an access token; REST probes will be skipped" >&2
fi

# Returns the HTTP status of a JSON POST and writes the body to $2.
post_json() {
  local url="$1" body_out="$2" payload="$3"
  curl -sS -o "${body_out}" -w '%{http_code}' \
    -X POST "${url}" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    -d "${payload}" 2>/dev/null || echo "000"
}

api_error() {
  python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit()
e = d.get("error", d)
print(str(e.get("message", e))[:160])' "$1" 2>/dev/null || true
}

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# --- Vertex AI: generative model access (hard requirement) ---------------
# countTokens is free and still proves project + model + endpoint access.
if [[ -n "${TOKEN}" ]]; then
  vertex_host="https://aiplatform.googleapis.com"
  [[ "${VERTEX_LOCATION}" == "global" ]] || vertex_host="https://${VERTEX_LOCATION}-aiplatform.googleapis.com"

  for model in "${EXTRACT_MODEL}" "${GENERATE_MODEL}"; do
    code="$(post_json \
      "${vertex_host}/v1/projects/${PROJECT_ID}/locations/${VERTEX_LOCATION}/publishers/google/models/${model}:countTokens" \
      "${TMP}/vertex.json" \
      '{"contents":[{"role":"user","parts":[{"text":"ping"}]}]}')"
    if [[ "${code}" == "200" ]]; then
      record "vertex ${model}" "OK" "reachable at location=${VERTEX_LOCATION}"
    else
      record "vertex ${model}" "FAIL" "HTTP ${code}: $(api_error "${TMP}/vertex.json")"
    fi
  done

  # --- Vertex AI: embeddings (hard requirement) --------------------------
  # gemini-embedding-001 is a regional model, so it is probed at EMBEDDING_REGION,
  # not at the global generative endpoint.
  code="$(post_json \
    "https://${EMBEDDING_REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${EMBEDDING_REGION}/publishers/google/models/${EMBEDDING_MODEL}:predict" \
    "${TMP}/embed.json" \
    '{"instances":[{"task_type":"SEMANTIC_SIMILARITY","content":"ping"}],"parameters":{"outputDimensionality":768}}')"
  if [[ "${code}" == "200" ]]; then
    dims="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["predictions"][0]["embeddings"]["values"]))' "${TMP}/embed.json" 2>/dev/null || echo "?")"
    record "vertex embeddings" "OK" "${EMBEDDING_MODEL} @ ${EMBEDDING_REGION}, ${dims} dims"
  else
    record "vertex embeddings" "FAIL" "HTTP ${code}: $(api_error "${TMP}/embed.json")"
  fi

  # --- Identity Platform -------------------------------------------------
  # A 404 here means the API is on but Identity Platform has never been
  # initialised, which is a one-click fix in the console rather than a blocker.
  code="$(curl -sS -o "${TMP}/idp.json" -w '%{http_code}' \
    "https://identitytoolkit.googleapis.com/admin/v2/projects/${PROJECT_ID}/config" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "x-goog-user-project: ${PROJECT_ID}" 2>/dev/null || echo "000")"
  case "${code}" in
    200) record "identity platform" "OK" "initialised" ;;
    404) record "identity platform" "WARN" "API on but not initialised: enable it once at console.cloud.google.com/customer-identity" ;;
    *) record "identity platform" "WARN" "HTTP ${code}: $(api_error "${TMP}/idp.json")" ;;
  esac
else
  record "vertex ai" "FAIL" "no access token"
  record "identity platform" "WARN" "no access token"
fi

# --- AlloyDB (hard requirement) -----------------------------------------
if probe gcloud alloydb clusters list --project="${PROJECT_ID}" --region="${REGION}"; then
  clusters="$(gcloud alloydb clusters list --project="${PROJECT_ID}" --region="${REGION}" --format='value(name)' 2>/dev/null | wc -l | tr -d ' ')"
  record "alloydb" "OK" "${clusters} cluster(s) in ${REGION}"
else
  record "alloydb" "FAIL" "${PROBE_ERR}"
fi

# --- The rest ------------------------------------------------------------
if probe gcloud run services list --project="${PROJECT_ID}" --region="${REGION}"; then
  record "cloud run" "OK" "${REGION}"
else
  record "cloud run" "FAIL" "${PROBE_ERR}"
fi

if probe gcloud pubsub topics list --project="${PROJECT_ID}" --limit=1; then
  record "pub/sub" "OK" ""
else
  record "pub/sub" "FAIL" "${PROBE_ERR}"
fi

if probe gcloud tasks queues list --project="${PROJECT_ID}" --location="${REGION}"; then
  record "cloud tasks" "OK" "${REGION}"
else
  record "cloud tasks" "FAIL" "${PROBE_ERR}"
fi

if probe gcloud scheduler jobs list --project="${PROJECT_ID}" --location="${REGION}"; then
  record "cloud scheduler" "OK" "${REGION}"
else
  record "cloud scheduler" "FAIL" "${PROBE_ERR}"
fi

if probe gcloud secrets list --project="${PROJECT_ID}" --limit=1; then
  record "secret manager" "OK" ""
else
  record "secret manager" "FAIL" "${PROBE_ERR}"
fi

if probe gcloud storage buckets list --project="${PROJECT_ID}" --limit=1; then
  record "cloud storage" "OK" ""
else
  record "cloud storage" "FAIL" "${PROBE_ERR}"
fi

# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

blocking=()
for i in "${!RESULT_NAMES[@]}"; do
  [[ "${RESULT_STATES[$i]}" == "FAIL" ]] || continue
  case "${RESULT_NAMES[$i]}" in
    alloydb | vertex*) blocking+=("${RESULT_NAMES[$i]}: ${RESULT_NOTES[$i]}") ;;
  esac
done

step "Verdict"

if [[ ${#blocking[@]} -gt 0 ]]; then
  cat >&2 <<EOF
  STOP. AlloyDB or Vertex AI is unusable in ${PROJECT_ID}:

$(printf '    - %s\n' "${blocking[@]}")

  This system stores its data and its vectors in AlloyDB and calls Gemini on
  Vertex AI. Neither is substitutable inside the Google Cloud-only constraint,
  so there is no point continuing until this is resolved. Likely causes, in the
  order worth checking:

    1. The sandbox project restricts these services. Ask for them to be added.
    2. An org policy blocks the region. Try a different GOOGLE_CLOUD_REGION.
    3. The model id is wrong or retired. Check the ids in .env against
       https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models
    4. Your account lacks roles/aiplatform.user or roles/alloydb.admin.
EOF
  exit 2
fi

if [[ ${HARD_FAIL} -eq 1 || ${SOFT_FAIL} -eq 1 ]]; then
  echo "  Usable, with caveats. Review the FAIL/WARN lines above before deploying."
  exit 1
fi

echo "  All required services are enabled and answering in ${PROJECT_ID} (${REGION})."
exit 0
