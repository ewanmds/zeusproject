# Vercel frontend and EC2 workspace backend

Public browser origin: `https://zeusproject.vercel.app`.
Backend/Caddy origin: `https://3-113-242-142.sslip.io`.

The repository now contains `vercel.json` configurations at both the repository
root and `frontend/`, supporting either of the existing project's possible Root
Directory settings. Both build the React workspace rather than the legacy
`frontend/build.mjs`, serve its `dist`, and proxy `/api/*` to the HTTPS EC2
backend. API response caching is disabled. The API administrator token used
by the old frontend is not part of the workspace build.

The workspace's SIWE origin must be the Vercel browser origin. On EC2:

```bash
cd /home/ec2-user/zeusproject
git pull --ff-only origin main
bash deploy/update-workspace.sh https://3-113-242-142.sslip.io https://zeusproject.vercel.app
```

The first argument selects the existing Caddy site and its upstream. The second
sets the allowed browser/SIWE origin in `.env.workspace`. Thus the script does
not search for a Vercel hostname in the EC2 Caddyfile. If `.env.workspace` was
already initialized for another browser origin, review and change only its
`ZEUS_WORKSPACE_ORIGIN` value; preserve the existing encryption key and database
URL. The deploy script deliberately refuses to silently change an existing
origin.

Vercel's Git integration builds the new configuration after a push to main.
The frontend needs the EC2 deployment to finish before wallet login or sessions
can work. Production domain validation requires both deployments to be ready.
If Vercel skipped a deployment, redeploy the latest main commit in its dashboard.

Injected wallets need no additional configuration. WalletConnect still requires
the Reown project ID and this Vercel origin allowed in the Reown project.

Routing references: [Vercel rewrites](https://vercel.com/docs/routing/rewrites)
and [project build configuration](https://vercel.com/docs/builds/configure-a-build).
