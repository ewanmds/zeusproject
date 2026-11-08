#!/usr/bin/env bash
# Run on EC2 after git pull. Does not launch a trading session.
set -euo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
ORIGIN=${1:-https://3-113-242-142.sslip.io}
cd "$ROOT"
if [[ "$ROOT" != /home/ec2-user/zeusproject || $(id -un) != ec2-user ]]; then
  echo 'This deployment targets ec2-user and /home/ec2-user/zeusproject.' >&2; exit 1
fi
git diff --quiet && git diff --cached --quiet || { echo 'Commit or preserve local source changes first.' >&2; exit 1; }
[[ -x .venv/bin/python ]] || { echo 'The existing .venv/bin/python is required.' >&2; exit 1; }
.venv/bin/python -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
sudo -v
sudo systemctl is-active --quiet caddy || { echo 'Caddy must already serve the public HTTPS site.' >&2; exit 1; }
STAMP=$(date -u +%Y%m%d-%H%M%S)
BACKUP="$ROOT/output/deploy-$STAMP"
mkdir -p "$BACKUP"
sudo cat /etc/caddy/Caddyfile > "$BACKUP/Caddyfile"
[[ ! -f .env.workspace ]] || cp -p .env.workspace "$BACKUP/env.workspace"
if sudo test -f /etc/systemd/system/zeus-workspace.service; then
  sudo cat /etc/systemd/system/zeus-workspace.service > "$BACKUP/zeus-workspace.service"
fi
.venv/bin/python deploy/workspace_config.py caddy --origin "$ORIGIN" --input "$BACKUP/Caddyfile" --output "$BACKUP/Caddyfile.next"
sudo caddy validate --adapter caddyfile --config "$BACKUP/Caddyfile.next"

node_supported() {
  command -v node >/dev/null && command -v npm >/dev/null && node -e 'const [a,b]=process.versions.node.split(".").map(Number);process.exit((a===20&&b>=19)||(a===22&&b>=12)||a>22?0:1)'
}
if ! node_supported; then
  . /etc/os-release
  [[ "$ID" == amzn && "$VERSION_ID" == 2023 ]] || { echo 'Install Node 22.12+ and npm, then rerun.' >&2; exit 1; }
  sudo dnf install -y nodejs22 nodejs22-npm
  # AL2023 also exposes versioned binary names; alternatives may retain older defaults.
  if command -v node-22 >/dev/null && command -v npm-22 >/dev/null; then
    mkdir -p "$BACKUP/node-bin"
    ln -s "$(command -v node-22)" "$BACKUP/node-bin/node"
    ln -s "$(command -v npm-22)" "$BACKUP/node-bin/npm"
    export PATH="$BACKUP/node-bin:$PATH"
  fi
  node_supported || { echo 'Select Node 22.12+ and npm in PATH, then rerun.' >&2; exit 1; }
fi
[[ -x .venv-workspace/bin/python ]] || .venv/bin/python -m venv .venv-workspace
.venv-workspace/bin/python -m pip install -r requirements.txt
npm ci --prefix frontend/workspace --no-audit --no-fund
npm run build --prefix frontend/workspace

LOCAL_DB=$(.venv-workspace/bin/python -c 'from pathlib import Path; from dotenv import dotenv_values; p=Path(".env.workspace"); print("yes" if not p.exists() or dotenv_values(p).get("ZEUS_WORKSPACE_LOCAL_DATABASE")=="1" else "no")')
if [[ "$LOCAL_DB" == yes ]]; then
  if ! command -v psql >/dev/null; then
    . /etc/os-release
    [[ "$ID" == amzn && "$VERSION_ID" == 2023 ]] || { echo 'Install and start PostgreSQL, then rerun.' >&2; exit 1; }
    sudo dnf install -y postgresql15 postgresql15-server
  fi
  if ! sudo test -s /var/lib/pgsql/data/PG_VERSION && command -v postgresql-setup >/dev/null; then
    sudo postgresql-setup --initdb
  fi
  sudo systemctl enable --now postgresql
fi
.venv-workspace/bin/python deploy/workspace_config.py configure --origin "$ORIGIN"

echo 'Build and configuration ready. Stopping the old service invokes its existing order/inventory cleanup.'
sudo systemctl stop zeus-bot
if ! .venv-workspace/bin/python - <<'PY'
from zeus.server import BotManager
manager = BotManager()
try:
    result = manager.reconcile_sessions()
    if not result.get('can_start'):
        raise SystemExit('Old sessions are not confirmed clean. Resolve/reconcile them before migration.')
finally:
    manager.outcome.rotation.close()
PY
then
  echo 'Migration stopped before switching Caddy. To inspect the old dashboard: sudo systemctl start zeus-bot' >&2
  exit 1
fi
sudo install -m 644 deploy/zeus-workspace.service /etc/systemd/system/zeus-workspace.service
sudo systemctl daemon-reload
sudo systemctl enable zeus-workspace
sudo systemctl restart zeus-workspace
READY=no
for attempt in {1..30}; do
  if .venv-workspace/bin/python -c 'import requests; r=requests.get("http://127.0.0.1:8081/api/v2/config",timeout=2);r.raise_for_status();assert r.json()["version"]=="workspace-1"' 2>/dev/null; then
    READY=yes; break
  fi
  sleep 1
done
if [[ "$READY" != yes ]]; then
  echo "Workspace startup failed. Caddy unchanged. Backup: $BACKUP" >&2
  sudo systemctl status zeus-workspace --no-pager || true
  echo 'Inspect: sudo journalctl -u zeus-workspace -n 80 --no-pager' >&2
  echo 'Restore the private dashboard if needed: sudo systemctl start zeus-bot' >&2
  exit 1
fi
sudo cp -p /etc/caddy/Caddyfile "$BACKUP/Caddyfile.before-switch"
sudo install -m 644 "$BACKUP/Caddyfile.next" /etc/caddy/Caddyfile
if ! sudo systemctl reload caddy; then
  sudo cp -p "$BACKUP/Caddyfile.before-switch" /etc/caddy/Caddyfile
  sudo systemctl reload caddy
  echo 'Caddy reload failed; previous proxy restored.' >&2
  exit 1
fi
.venv-workspace/bin/python -c 'import sys,requests; r=requests.get(sys.argv[1]+"/api/v2/config",timeout=15);r.raise_for_status();assert r.json()["version"]=="workspace-1";print("Public HTTPS workspace verified")' "$ORIGIN"
sudo systemctl disable zeus-bot
printf 'Deployed %s at %s\nBackup: %s\n' "$(git rev-parse --short HEAD)" "$ORIGIN" "$BACKUP"
echo 'No bot launched. Installed wallets are ready; WalletConnect QR requires a Reown project ID.'
