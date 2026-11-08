# Replace the public EC2 dashboard with the wallet workspace

Target verified from this checkout's service/Caddy definitions:
`ec2-user`, `/home/ec2-user/zeusproject`, existing `zeus-bot`, public hostname
`3-113-242-142.sslip.io`. The remote machine has not been inspected or changed
from this coding session; the script verifies its prerequisites when run there.

First stop live bots from their UI and verify their cleanup. During deployment,
stopping the old service invokes its normal cancellation/inventory cleanup and
can close positions. Journals, `.env`, database contents and agent keys are
preserved. The deployment does not start a trading session.

```bash
cd /home/ec2-user/zeusproject
git status --short
git pull --ff-only origin main
bash deploy/update-workspace.sh https://3-113-242-142.sslip.io
```

The script builds the frontend, installs Python dependencies in an independent
`.venv-workspace` (preserving the old service's `.venv`), checks Caddy,
prepares production configuration, starts `zeus-workspace` on loopback 8081,
then changes only the target site's simple `reverse_proxy` from 8080 to 8081.
Other Caddy site blocks remain unchanged; unsupported layouts stop before the
service switch. Caddy is reloaded only after local readiness succeeds. Public
HTTPS readiness must pass before the old service is disabled.

On Amazon Linux 2023 it can install Node.js 22 and PostgreSQL 15 when missing.
Other distributions need these prerequisites installed by their operator.
A running Caddy service is required. Python 3.9 in the old `.venv` is accepted:
the script selects a newer interpreter, or installs `python3.11` and
`python3.11-pip` on Amazon Linux 2023. It creates `.venv-workspace` from that
interpreter without replacing `.venv` or the system Python. An incompatible
stopped workspace environment is moved to the deployment backup before
recreation. See [AWS's Python guide](https://docs.aws.amazon.com/linux/al2023/ug/python.html).
Package names follow [AWS's Node guide](https://docs.aws.amazon.com/linux/al2023/ug/nodejs.html)
and its [AL2023 package list](https://docs.aws.amazon.com/linux/al2023/release-notes/vercmp-AL2-AL2023.12.html).

For a fresh installation it creates a dedicated local PostgreSQL database and
role with a random password, plus an independent Fernet encryption key. Both
are stored only in `.env.workspace` with mode 0600. The only authentication
rule added to PostgreSQL is scoped to that database/role on 127.0.0.1 using
SCRAM. The previous authentication file is backed up. Existing generated
credentials and keys are reused after an interrupted run; pre-existing role
passwords are never overwritten.

For an existing/external database, create `.env.workspace` first with the
production fields shown in `.env.workspace.example`; omit
`ZEUS_WORKSPACE_LOCAL_DATABASE`. This skips local database provisioning.
Never replace an existing encryption key. A public origin change must be
reviewed explicitly; the script refuses to silently change one.

Deployment backups live under `output/deploy-<UTC timestamp>/`, including any
pre-existing workspace environment file. Protect/back up this folder with the
database and execution state. All these files are ignored by Git.

```bash
sudo systemctl status zeus-workspace --no-pager
sudo journalctl -u zeus-workspace -n 80 --no-pager
```

WalletConnect QR remains optional: set `ZEUS_WALLETCONNECT_PROJECT_ID` in
`.env.workspace`, allow the public domain in Reown, and restart the workspace
when sessions are stopped. Injected wallets need no Reown ID.

If startup fails before proxy replacement, Caddy is left unchanged. Inspect the
workspace logs; `sudo systemctl start zeus-bot` restores the old backend without
automatically launching bots. For a later rollback, stop/reconcile workspace
sessions first, restore the saved Caddyfile, validate/reload Caddy, and enable
the old service. Do not run two independent supervisors for the same account.
