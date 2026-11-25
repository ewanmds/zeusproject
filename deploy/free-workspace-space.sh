#!/usr/bin/env bash
# Reclaim rebuildable space on a small EC2 root volume before a deploy.
# Never touches .env*, .zeus-state, .zeus-workspace, output/ or journals.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

echo "Before:"
df -h "$ROOT" | tail -1

# A partial npm install is rebuildable and is usually the largest leftover.
# dist/ is kept on purpose: the running workspace keeps serving the old
# bundle until the next successful build replaces it.
rm -rf frontend/workspace/node_modules

# npm caches are rebuildable and often hold several hundred MB.
npm cache clean --force 2>/dev/null || true
rm -rf "$HOME/.npm/_cacache" "$HOME/.npm/_logs"

# Python tool caches are rebuildable too. The venv and .env files are kept.
rm -rf "$HOME/.cache/pip" "$HOME/.cache/uv"

# System caches, old journal data and temporary files.
sudo dnf clean all || true
sudo journalctl --vacuum-size=100M || true
sudo find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + 2>/dev/null || true

echo "After:"
df -h "$ROOT" | tail -1

echo
echo "Largest directories (review before deleting anything else):"
sudo du -xhd1 "$ROOT" "$HOME" /var /usr /opt 2>/dev/null | sort -h | tail -20
echo
echo "Old deploy backups (safe to remove the ones you no longer need):"
sudo du -sh "$ROOT"/output/deploy-* 2>/dev/null | sort -h | tail -10 || true
