# Deployer Zeus

Architecture cible:

```text
Vercel (frontend) -> HTTPS Caddy -> FastAPI localhost:8080 -> Hyperliquid
```

Le port `8080` reste local au VPS. Caddy gere le certificat HTTPS et le
WebSocket `/ws`.

## 1. Creer le VPS AWS EC2

Dans la console EC2, lancer une instance Linux Free Tier eligible. Amazon Linux
2023 est le choix le plus simple ici; son utilisateur SSH par defaut est
`ec2-user`. Activer une IP publique et, si le domaine doit rester stable,
associer une Elastic IP.

Creer un Security Group avec seulement:

- TCP 22 depuis ton IP pour SSH;
- TCP 80 et 443 depuis Internet pour Caddy;
- ne pas ouvrir TCP 8080.

Attention: la duree et les types d'instances gratuits AWS dependent de la date
de creation du compte et des credits Free Tier. Verifier l'eligibilite dans la
console avant de lancer l'instance.

## 2. Installer le bot

```bash
sudo dnf update -y
sudo dnf install -y git python3 python3-pip
git clone https://github.com/ewanmds/zeusproject.git /home/ec2-user/zeusproject
cd /home/ec2-user/zeusproject
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Si tu choisis Ubuntu au lieu d'Amazon Linux 2023:

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip
```

Dans ce cas, remplace `ec2-user` par `ubuntu` dans les chemins et dans
`deploy/zeus-bot.service`.

Creer ensuite `/home/ec2-user/zeusproject/.env` directement sur le VPS. Ne jamais
committer ce fichier:

```dotenv
PRIVATE_KEY=ta_cle_privee_hyperliquid
WALLET_ADDRESS=ton_adresse_hyperliquid

# Necessaires seulement si WorldCup utilise un wallet distinct.
PRIVATE_KEY_OUTCOME=
WALLET_ADDRESS_OUTCOME=

# Long token aleatoire, par exemple: openssl rand -hex 32
BOT_API_TOKEN=remplace_par_un_token_aleatoire
BOT_CORS_ORIGINS=https://ton-frontend.vercel.app
BOT_HOST=127.0.0.1
BOT_PORT=8080
```

```bash
chmod 600 /home/ec2-user/zeusproject/.env
```

## 3. Demarrer automatiquement le backend

Le fichier `deploy/zeus-bot.service` suppose Amazon Linux 2023 et
l'utilisateur `ec2-user`.

```bash
sudo cp deploy/zeus-bot.service /etc/systemd/system/zeus-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now zeus-bot
sudo systemctl status zeus-bot
curl http://127.0.0.1:8080/health
```

Logs en temps reel:

```bash
sudo journalctl -u zeus-bot -f
```

## 4. Activer HTTPS avec Caddy

Faire pointer un sous-domaine, par exemple `api.ton-domaine.com`, vers l'IP
publique du VPS. Caddy demande automatiquement le certificat si les ports 80
et 443 sont accessibles.

Installer Caddy depuis le depot officiel Fedora/RedHat, puis:

```bash
sudo dnf install -y dnf-plugins-core
sudo dnf copr enable @caddy/caddy -y
sudo dnf install -y caddy
```

Puis:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/api.example.com/api.ton-domaine.com/' /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

Verifier:

```bash
curl https://api.ton-domaine.com/health
```

## 5. Deployer le frontend sur Vercel

Importer `ewanmds/zeusproject` dans Vercel avec ces reglages:

- Root Directory: `frontend`;
- Framework Preset: `Other`;
- Build Command: `npm run build`;
- Output Directory: `dist`.

Ajouter les variables Vercel de production:

```text
ZEUS_API_BASE=https://api.ton-domaine.com
ZEUS_API_TOKEN=la_meme_valeur_que_dans_le_.env_du_VPS
```

Chaque redeploiement regenere `dist/config.js`. Le token est utilise par le
navigateur pour controler l'API; il ne doit donc pas etre considere comme un
secret absolu. Pour un dashboard prive, ajouter ensuite Cloudflare Access, un
VPN ou une authentification devant le frontend.

## 6. Mise a jour

```bash
cd /home/ec2-user/zeusproject
git pull --ff-only origin main
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo systemctl restart zeus-bot
```

Vercel redeploie automatiquement le frontend a chaque push sur `main`.
