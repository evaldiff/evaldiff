#!/usr/bin/env bash
# evaldiff - one-shot deploy script (run ON the Hetzner box)
# Secrets come from .env (KEY=VALUE lines) or the environment.
set -euo pipefail

cd "$(dirname "$0")"

# --- secrets: from .env (preferred) or the environment ---
if [ -f .env ]; then
  set -a
  # Deployment-local, untracked secrets file.
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
  echo "POSTGRES_PASSWORD is required (put it in .env or export it)" >&2
  exit 1
fi
if [ -z "${SEAWEEFS_SECRET:-}" ]; then
  echo "SEAWEEFS_SECRET is required (put it in .env or export it)" >&2
  exit 1
fi

if [ -z "${EVALDIFF_SECRET_KEY:-}" ]; then
  echo "EVALDIFF_SECRET_KEY is required; see deploy/UPGRADING.md" >&2
  exit 1
fi

export POSTGRES_PASSWORD SEAWEEFS_SECRET EVALDIFF_SECRET_KEY

# --- system deps ---
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin missing" >&2
  exit 1
fi

# --- DNS: Caddy ACME needs api.evaldiff.io to resolve to this box ---
IP=$(hostname -I | awk '{print $1}')
if ! grep -q "api.evaldiff.io" /etc/hosts; then
  echo "$IP api.evaldiff.io" | sudo tee -a /etc/hosts >/dev/null
fi

# --- build & start ---
# --quiet validates interpolation without printing rendered secrets.
docker compose config --quiet
docker compose build api
# Validate key syntax in the candidate image before changing running services.
docker compose run --rm --no-deps api python -m api.migrate_secrets --validate-config
docker compose up -d
docker compose ps

# --- smoke test (once the API is up) ---
for _ in $(seq 1 30); do
  if docker compose exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)" \
    >/dev/null 2>&1 && curl --max-time 3 -fsS https://api.evaldiff.io/health >/dev/null 2>&1; then
    echo
    echo "API is up:"
    curl --max-time 3 -fsS https://api.evaldiff.io/health
    echo
    echo "Next: verify https://api.evaldiff.io/health from outside"
    exit 0
  fi
  sleep 2
done
echo "API did not become healthy after 30 checks - check: docker compose logs api" >&2
exit 1
