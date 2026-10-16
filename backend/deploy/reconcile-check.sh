#!/usr/bin/env bash
# Read-only. Shows how the live VPS files differ from the fetched repository version, so manual
# fixes can be reviewed before 'git pull' replaces them. Run from the backend directory.
set -euo pipefail
cd "$(dirname "$0")/.."

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "Not a git checkout." >&2; exit 1; }
git fetch --quiet origin
branch="${1:-$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo origin/main)}"

echo "Local edits not committed on this server:"
git status --short -- . || true
echo
for f in Dockerfile src/pca/bot/keyboards.py src/pca/bot/handlers/customer.py pyproject.toml docker-compose.yml; do
  echo "===== $f: live server vs $branch ====="
  git diff --stat "$branch" -- "$f" || true
done
echo
echo "Full diff of one file:  git diff $branch -- <file>"
echo "Save live changes first: git stash push -m vps-manual-fixes"
