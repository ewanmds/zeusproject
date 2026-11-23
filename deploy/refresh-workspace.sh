#!/usr/bin/env bash
# Update an EXISTING workspace. No Caddy/database migration and no bot launch.
set -euo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ "$ROOT" == /home/ec2-user/zeusproject && $(id -un) == ec2-user ]] || {
  echo 'Expected ec2-user in /home/ec2-user/zeusproject' >&2; exit 1;
}
cd "$ROOT"
git diff --quiet && git diff --cached --quiet || { echo 'Preserve local source edits before updating.' >&2; exit 1; }
[[ -x .venv-workspace/bin/python && -f .env.workspace ]] || { echo 'Existing workspace installation required.' >&2; exit 1; }
sudo -n systemctl is-active --quiet zeus-workspace
if sudo -n systemctl is-active --quiet zeus-bot; then
  echo 'Private zeus-bot is also active; reconcile the two services before updating.' >&2; exit 1
fi
WORKSPACE_STATE=$(.venv-workspace/bin/python - <<'PY'
import os
from pathlib import Path
from dotenv import dotenv_values
values = {**dotenv_values('.env'), **dotenv_values('.env.workspace'), **os.environ}
print(Path(values.get('ZEUS_WORKSPACE_STATE') or '.zeus-workspace').resolve())
PY
)
idle() { .venv-workspace/bin/python deploy/check_workspace_idle.py --workspace "$WORKSPACE_STATE" --legacy "$ROOT/.zeus-state"; }
idle
mkdir -p "$ROOT/.zeus-state"
MAINTENANCE="$ROOT/.zeus-state/maintenance"
command -v flock >/dev/null || { echo 'flock is required for the deployment lock.' >&2; exit 1; }
if ! .venv-workspace/bin/python -c 'from zeus.maintenance import maintenance_active; raise SystemExit(int(maintenance_active()))'; then
  echo 'An updater is still running. Wait for it to finish.' >&2; exit 1
fi
exec 9>>"$ROOT/.zeus-state/maintenance.lock"
flock -n 9 || {
  echo 'Another maintenance operation is recorded.' >&2; exit 1;
}
printf '%s\n' "Workspace update $$" > "$MAINTENANCE"
trap 'rm -f -- "$MAINTENANCE"' EXIT
STAMP=$(date -u +%Y%m%d-%H%M%S)
BACKUP="$ROOT/output/workspace-refresh-$STAMP"
mkdir -p "$BACKUP"
git rev-parse HEAD > "$BACKUP/revision"
cp -p .env.workspace "$BACKUP/env.workspace"
.venv-workspace/bin/python -m pip freeze > "$BACKUP/requirements.before.txt"
node_supported() {
  command -v node >/dev/null && command -v npm >/dev/null && node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit((a===20&&b>=19)||(a===22&&b>=12)||a>22?0:1)'
}
if ! node_supported && command -v node-22 >/dev/null && command -v npm-22 >/dev/null; then
  mkdir -p "$BACKUP/node-bin"
  ln -s "$(command -v node-22)" "$BACKUP/node-bin/node"
  ln -s "$(command -v npm-22)" "$BACKUP/node-bin/npm"
  export PATH="$BACKUP/node-bin:$PATH"
fi
node_supported || { echo 'Select Node 22.12+ and npm before updating.' >&2; exit 1; }
tar -czf "$BACKUP/trading-state.tar.gz" --exclude='*/network/*' -C "$ROOT" .zeus-state -C "$(dirname "$WORKSPACE_STATE")" "$(basename "$WORKSPACE_STATE")"
.venv-workspace/bin/python -m pip install -r requirements.txt
npm ci --prefix frontend/workspace --no-audit --no-fund
npm run build --prefix frontend/workspace
ZEUS_MAINTENANCE_ROOT="$BACKUP/test-maintenance" .venv-workspace/bin/python -m unittest discover -s tests -p 'test_*.py'
npm test --prefix frontend/workspace
idle
sudo -n systemctl restart zeus-workspace
EXPECTED=$(git rev-parse --short=12 HEAD)
for attempt in {1..30}; do
  if .venv-workspace/bin/python - "$EXPECTED" <<'PY'
import sys, requests
try:
    response = requests.get('http://127.0.0.1:8081/api/v2/config', timeout=2)
    response.raise_for_status()
    assert response.json()['build_revision'] == sys.argv[1]
except Exception:
    raise SystemExit(1)
PY
  then
    printf 'Workspace ready: %s\nBackup: %s\nNo trading session launched.\n' "$EXPECTED" "$BACKUP"
    exit 0
  fi
  sleep 1
done
echo 'Startup not verified. Inspect: sudo journalctl -u zeus-workspace -n 80 --no-pager' >&2
exit 1
