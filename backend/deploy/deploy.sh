#!/usr/bin/env bash
# First deploy and every update. Data-preserving: backs up first, never removes volumes.
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null || fail "docker is not installed"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin is not installed"
[[ -f .env ]] || fail ".env missing. Run deploy/setup-env.sh first."

[[ "$(stat -c %a .env)" == "600" ]] || { chmod 600 .env; echo "Set .env permissions to 600."; }

value() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }
for key in PCA_BOT_TOKEN PCA_ADMIN_TELEGRAM_IDS POSTGRES_PASSWORD; do
  [[ -n "$(value "$key")" ]] || fail "$key is empty in .env"
done
[[ "$(value POSTGRES_PASSWORD)" =~ ^[A-Za-z0-9]+$ ]] || fail "POSTGRES_PASSWORD must be alphanumeric"

echo "==> Backing up database (if running)"
deploy/backup.sh

echo "==> Building image"
docker compose build

echo "==> Starting Postgres and Redis"
docker compose up -d --wait postgres redis

echo "==> Applying migrations"
docker compose run --rm migrate

echo "==> Starting bot and API"
docker compose up -d --wait bot api

docker compose ps
echo "Deployed. Follow bot logs with: docker compose logs -f bot"
