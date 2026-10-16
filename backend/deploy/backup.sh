#!/usr/bin/env bash
# Writes a compressed pg_dump to ./backups and keeps the newest 14.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -z "$(docker compose ps -q postgres 2>/dev/null)" ]]; then
  echo "Postgres container is not running; nothing to back up."
  exit 0
fi

mkdir -p backups
chmod 700 backups
file="backups/pca-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$file"
if [[ ! -s "$file" ]]; then
  rm -f "$file"
  echo "Backup failed: empty dump." >&2
  exit 1
fi
chmod 600 "$file"
echo "Backup written: $file"
ls -1t backups/pca-*.dump | tail -n +15 | xargs -r rm -f
