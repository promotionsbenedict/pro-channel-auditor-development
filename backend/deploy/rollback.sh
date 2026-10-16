#!/usr/bin/env bash
# Restores the image that was running before the last deploy. Never touches volumes or the schema:
# migrations in this project are additive, so the previous code runs against the newer schema.
# To also restore data, see OPERATIONS.md "Restoring a backup" (manual, deliberate step).
set -euo pipefail
cd "$(dirname "$0")/.."

value() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }
stack="$(value PCA_STACK_NAME || true)"
image="pca-app:${stack:-pca}"

docker image inspect "${image}-previous" >/dev/null 2>&1 \
  || { echo "No ${image}-previous image found; nothing to roll back to." >&2; exit 1; }

echo "==> Backing up database before rollback"
deploy/backup.sh

echo "==> Restoring ${image}-previous"
docker tag "${image}-previous" "$image"
docker compose up -d --no-build --wait bot worker api
docker compose ps
echo "Rolled back. Revert the code with 'git checkout <commit>' before the next deploy."
