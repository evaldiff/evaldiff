# evaldiff - one-shot deploy script (run ON the Hetzner box)
# Secrets come from .env (KEY=VALUE lines) or the environment.
set -euo pipefail

cd "$(dirname "$0")"

# --- secrets: from .env (preferred) or the environment ---
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if [ -z "${POSTGRES_PASSWORD}" ]; then
  echo "POSTGRES_PASSWORD is required (put it in .env or export it)" >&2
  exit 1
fi
if [ -z "${SEAWEEFS_SECRET}" ]; then
  echo "SEAWEEFS_SECRET is required (put it in .env or export it)" >&2
  exit 1
fi

export POSTGRES_PASSWORD SEAWEEFS_SECRET

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
docker compose up -d --build
docker compose ps

# --- smoke test (once the API is up) ---
for i in $(seq 1 30); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo
    echo "API is up:"
    curl -s http://localhost:8000/health
    echo
    echo "Next: verify https://api.evaldiff.io/health from outside"
    exit 0
  fi
  sleep 2
done
echo "API did not become healthy in 60s - check: docker compose logs api" >&2
exit 1
