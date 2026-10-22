# Mise à jour sur EC2

Ces commandes correspondent au service `deploy/zeus-bot.service` : utilisateur
`ec2-user`, dossier `/home/ec2-user/zeusproject`, environnement Python `.venv`.

Arrêter d'abord les stratégies depuis le tableau de bord et vérifier les
positions. Le bouton **Stop** annule les ordres du bot et ferme sa position.
Le redémarrage du service interrompt aussi les processus enfants.

Dans le terminal SSH de l'instance EC2 :

```bash
(
  set -e
  cd /home/ec2-user/zeusproject
  sudo systemctl stop zeus-bot
  git pull --ff-only origin main
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m unittest discover -s tests -q
  sudo systemctl start zeus-bot
  sudo systemctl status zeus-bot --no-pager
)
```

Si une commande échoue, la séquence s'arrête. Corriger l'erreur avant de relancer
le service. `git pull --ff-only` conserve les changements locaux et refuse une
divergence ; ne pas remplacer cette commande par un reset forcé.

Le fichier `.env` reste sur le serveur. Le backend sert directement les fichiers
de `frontend/` : aucun build npm n'est nécessaire pour ce service EC2.
Recharger la page, puis relancer manuellement les stratégies voulues.

Vérifier la version et consulter les derniers journaux :

```bash
git rev-parse --short HEAD
sudo journalctl -u zeus-bot -n 80 --no-pager
```

Pour suivre les journaux en direct :

```bash
sudo journalctl -u zeus-bot -f
```
