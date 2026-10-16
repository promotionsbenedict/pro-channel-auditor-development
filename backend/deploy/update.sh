#!/usr/bin/env bash
# Pulls the latest code (fast-forward only) and redeploys without touching data.
set -euo pipefail
cd "$(dirname "$0")/.."

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git pull --ff-only
fi
exec deploy/deploy.sh
