#!/usr/bin/env bash
# Regenerate the web app's API types from the running API's OpenAPI document.
#
# Generated from the live document rather than a checked-in copy of the schema,
# because the document is what the API actually serves. A hand-maintained type file
# drifts silently, and the first symptom is a runtime shape error in the browser.
#
# The output is committed: `npm run typecheck` has to work in a clean checkout with no
# database and no API running.
set -euo pipefail

API_URL="${API_URL:-http://127.0.0.1:8080}"
OUT="apps/web/src/lib/api-types.ts"

if ! curl -fsS "${API_URL}/healthz" >/dev/null 2>&1; then
  echo "The API is not answering at ${API_URL}." >&2
  echo "Start it with 'make dev', or set API_URL." >&2
  exit 1
fi

curl -fsS "${API_URL}/openapi.json" -o /tmp/jobtrack-openapi.json
npx --prefix apps/web openapi-typescript /tmp/jobtrack-openapi.json -o "${OUT}"
echo "Wrote ${OUT}"
