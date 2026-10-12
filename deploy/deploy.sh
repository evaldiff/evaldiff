#!/usr/bin/env bash
# Run on the deployment host from a clean reviewed checkout.
set -euo pipefail

cd "$(dirname "$0")"
DEPLOY_STAGE=configuration
_EVALDIFF_TEMP_CONTEXT=''
cleanup() {
  if [ -n "$_EVALDIFF_TEMP_CONTEXT" ]; then
    rm -rf -- "$_EVALDIFF_TEMP_CONTEXT"
  fi
}
trap cleanup EXIT
trap 'echo "Deployment failed during ${DEPLOY_STAGE}; inspect service state privately before retrying." >&2' ERR

if [ -f .env ]; then
  set -a
  # Deployment-local, untracked secrets file.
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

for required_secret in POSTGRES_PASSWORD SEAWEEFS_SECRET EVALDIFF_SECRET_KEY; do
  if [ -z "${!required_secret:-}" ]; then
    echo "$required_secret is required; see deploy/UPGRADING.md" >&2
    exit 1
  fi
done
export POSTGRES_PASSWORD SEAWEEFS_SECRET EVALDIFF_SECRET_KEY

# Fail on missing prerequisites; host provisioning and public DNS are separate.
DEPLOY_STAGE=prerequisites
for dependency in docker git python3 timeout tar mktemp; do
  if ! command -v "$dependency" >/dev/null; then
    echo "$dependency is required on the deployment host" >&2
    exit 1
  fi
done
python3 -c 'import tomllib' >/dev/null 2>&1 || {
  echo "Python 3.11+ is required for deployment checks" >&2
  exit 1
}
timeout --kill-after=2s 10s docker compose version >/dev/null
timeout --kill-after=2s 10s docker info >/dev/null 2>&1
WAIT_HELP=$(timeout --kill-after=2s 10s docker compose up --help)
if [[ "$WAIT_HELP" != *"--wait-timeout"* || "$WAIT_HELP" != *"--wait"* ]]; then
  echo "Docker Compose must support up --wait and --wait-timeout" >&2
  exit 1
fi

CHECKER=./check_deployment.py
PUBLIC_URL=${EVALDIFF_DEPLOY_PUBLIC_URL:-https://api.evaldiff.io}
python3 "$CHECKER" origin "$PUBLIC_URL"
DEPLOY_STAGE=candidate
CANDIDATE=$(python3 "$CHECKER" candidate)
read -r EVALDIFF_BUILD_VERSION EVALDIFF_BUILD_REVISION <<< "$CANDIDATE"
export EVALDIFF_BUILD_VERSION EVALDIFF_BUILD_REVISION
IMAGE_REFERENCE="evaldiff-api:${EVALDIFF_BUILD_REVISION}"

# Validate interpolation without printing rendered secrets, then build identity.
DEPLOY_STAGE=build
# Build only committed inputs, excluding ignored files and concurrent local edits.
_EVALDIFF_TEMP_CONTEXT=$(mktemp -d "${TMPDIR:-/tmp}/evaldiff-candidate.XXXXXXXX")
git -C .. archive "$EVALDIFF_BUILD_REVISION" | tar -x -C "$_EVALDIFF_TEMP_CONTEXT"
EVALDIFF_BUILD_CONTEXT=$_EVALDIFF_TEMP_CONTEXT
export EVALDIFF_BUILD_CONTEXT
timeout --kill-after=2s 15s docker compose config --quiet
timeout --kill-after=10s 900s docker compose build api
IMAGE_ID=$(python3 "$CHECKER" image "$IMAGE_REFERENCE" "$EVALDIFF_BUILD_VERSION" "$EVALDIFF_BUILD_REVISION")

# Invalid keys must fail before stopping the old API or changing services.
DEPLOY_STAGE=encryption-preflight
timeout --kill-after=2s 30s docker compose run --rm --no-deps api python -m api.migrate_secrets --validate-config
if [ "$(python3 "$CHECKER" candidate)" != "$CANDIDATE" ]; then
  echo "Candidate changed during build; deployment stopped" >&2
  exit 1
fi

# Old processes outside Compose must already be stopped per UPGRADING.md.
DEPLOY_STAGE=stop-old-api
timeout --kill-after=5s 45s docker compose stop --timeout 30 api
DEPLOY_STAGE=dependencies
timeout --kill-after=5s 150s docker compose up -d --no-build --wait --wait-timeout 120 db seaweed
DEPLOY_STAGE=api-readiness
timeout --kill-after=5s 150s docker compose up -d --no-build --wait --wait-timeout 120 api
DEPLOY_STAGE=caddy
timeout --kill-after=5s 150s docker compose up -d --no-build --wait --wait-timeout 120 caddy

DEPLOY_STAGE=service-health
python3 "$CHECKER" services "$IMAGE_ID"
DEPLOY_STAGE=container-smoke
# Send only trusted checker code on stdin; no key/URL credentials in arguments.
timeout --kill-after=2s 10s docker compose exec -T api python - \
  http http://localhost:8000 "$EVALDIFF_BUILD_VERSION" "$EVALDIFF_BUILD_REVISION" < "$CHECKER"

DEPLOY_STAGE=https-smoke
# Give Caddy a bounded initial certificate/readiness window. Keep raw responses
# and transport diagnostics out of logs; the final check prints fixed reasons.
HTTPS_READY=false
for _ in $(seq 1 15); do
  if timeout --kill-after=2s 10s python3 "$CHECKER" http "$PUBLIC_URL" \
    "$EVALDIFF_BUILD_VERSION" "$EVALDIFF_BUILD_REVISION" --public >/dev/null 2>&1; then
    HTTPS_READY=true
    break
  fi
  sleep 2
done
if [ "$HTTPS_READY" != true ]; then
  timeout --kill-after=2s 10s python3 "$CHECKER" http "$PUBLIC_URL" \
    "$EVALDIFF_BUILD_VERSION" "$EVALDIFF_BUILD_REVISION" --public
  echo "HTTPS qualification did not pass within the smoke window" >&2
  exit 1
fi
# Recheck services after public smoke so a dependency failure cannot go unnoticed.
python3 "$CHECKER" services "$IMAGE_ID"
echo "Deployment verified: version=$EVALDIFF_BUILD_VERSION revision=$EVALDIFF_BUILD_REVISION image=$IMAGE_ID"
