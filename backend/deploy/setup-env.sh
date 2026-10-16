#!/usr/bin/env bash
# Creates .env from .env.example with a generated database password. Never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo ".env already exists; leaving it untouched."
  exit 0
fi

umask 077
cp .env.example .env
password="$(openssl rand -hex 24)"
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${password}/" .env
chmod 600 .env
echo "Created .env (chmod 600) with a generated POSTGRES_PASSWORD."
echo "Now edit it and fill PCA_BOT_TOKEN and PCA_ADMIN_TELEGRAM_IDS:  nano $(pwd)/.env"
