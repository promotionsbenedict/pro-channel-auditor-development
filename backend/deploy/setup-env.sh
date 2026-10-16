#!/usr/bin/env bash
# Creates .env from .env.example with a generated database password. Never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."

umask 077

random_base64() { head -c "$1" /dev/urandom | base64 | tr -d '\n'; }
random_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

add_session_key() {
  local key_id="k$(date +%Y%m%d)"
  local key
  key="$(random_base64 32)"
  if grep -q '^PCA_SESSION_KEYS=' .env; then
    sed -i "s|^PCA_SESSION_KEYS=.*|PCA_SESSION_KEYS=${key_id}:${key}|" .env
  else
    printf '\nPCA_SESSION_KEYS=%s:%s\n' "$key_id" "$key" >> .env
  fi
  if grep -q '^PCA_SESSION_ACTIVE_KEY_ID=' .env; then
    sed -i "s|^PCA_SESSION_ACTIVE_KEY_ID=.*|PCA_SESSION_ACTIVE_KEY_ID=${key_id}|" .env
  else
    printf 'PCA_SESSION_ACTIVE_KEY_ID=%s\n' "$key_id" >> .env
  fi
  echo "Generated session encryption key ${key_id}. Back up .env somewhere safe: without it stored sessions can't be read."
}

if [[ -f .env ]]; then
  # Existing .env: only fill in a session key if none has been set yet.
  if grep -qE '^PCA_SESSION_KEYS=.+' .env; then
    echo ".env already exists and has a session key; leaving it untouched."
  else
    add_session_key
  fi
  chmod 600 .env
  exit 0
fi

cp .env.example .env
password="$(random_hex 24)"
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${password}/" .env
add_session_key
chmod 600 .env
echo "Created .env (chmod 600) with a generated POSTGRES_PASSWORD."
echo "Now edit it and fill PCA_BOT_TOKEN and PCA_ADMIN_TELEGRAM_IDS:  nano $(pwd)/.env"
