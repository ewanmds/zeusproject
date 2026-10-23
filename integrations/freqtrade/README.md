# Freqtrade dans Zeus

« Freqtrade » désigne ici le bot Go Hyperliquid du dossier fourni, pas le
framework Python homonyme. Aucun composant n'est démarré à l'import.

## Organisation

- `engine/` : sources Go, SDK local, lanceurs, documentation et tests conservés.
- `profile.json` : profil dérivé de `config.live.yaml`, sans identifiants.
- `tests/` : tests hors ligne de l'adaptateur Python et de son API.
- `bin/` : nouveaux exécutables compilés, ignorés par Git.
- `local/original/` : copie intégrale du dossier initial, avec ses binaires,
  configurations locales, anciens journaux et `.git` ; entièrement ignorée par Git.
- `local/source-manifest.json` : inventaire SHA-256 vérifié de la copie initiale.

Le dossier original sur le Bureau reste intact. Les anciens lanceurs sont
conservés ; l'entrée du mode Zeus est l'adaptateur `zeus.bots.freqtrade`.

## Compilation et EC2

Depuis la racine Zeus, avec Go 1.25+ disponible :

```text
python -m zeus.bots.freqtrade --build
```

Cette commande compile Windows et Linux amd64 ; elle ne lance pas le bot.
Les anciens exécutables ne sont jamais choisis comme solution de secours.
Sur EC2, après `git pull --ff-only`, exécuter cette commande avec le Python
du venv avant d'utiliser Freqtrade. Le backend BBO/MID reste utilisable si Go
ou le binaire Freqtrade manque. Le lancement du serveur reste `python bot_server.py`.

## Utilisation

Dans Zeus, choisir **Freqtrade**, le marché et le montant, puis **Lancer**.
Ce mode est réel uniquement ; cette action autorise la validation du compte
puis les ordres. Aucune stratégie n'est lancée par l'installation ou les tests.

Le montant est un plafond, sans augmentation automatique au minimum d'échange.
Une seule taille d'ordre d'inventaire est autorisée ; les tailles sont arrondies
vers le bas, et les sorties sont reduce-only. Les suspensions sur signaux sont
retirées, mais les protections de données, compte, budget API et pertes restent
actives. Le levier provient de `OBI_LEVERAGE` (20 par défaut), plafonné au contrat.

**Arrêter** demande au Go d'annuler et fermer, puis de vérifier la mise à plat
par REST. En cas de panne réseau ou de position résiduelle, la session reste en
échec avec position inconnue ; l'interface n'annonce pas une clôture confirmée.

## Télémétrie et validation

Aucun nouveau log ni post-mortem sur disque en mode Zeus. Le flux JSON est
consommé en mémoire ; seul le snapshot courant du dashboard est remplacé dans
le répertoire temporaire. Les verrouillages Windows sont non fatals.

CPM = `(frais − PnL réalisé) / volume confirmé × 1 000 000`, hors funding.
Sans fills confirmés, le CPM reste nul au sens JSON `null` et s'affiche `—`.
Les tentatives d'ordres sont comptées séparément ; elles incluent les échecs.
Les réponses de placement/modification ambiguës ne sont pas renvoyées en boucle :
elles suspendent les nouvelles quotes jusqu'à réconciliation. Une confirmation
de comptabilité finale manquante laisse le CPM inconnu, indépendamment de la
confirmation de mise à plat.

```text
python -B -m unittest discover -s integrations/freqtrade/tests -p "test_*.py" -v
```

Depuis `engine/`, exécuter `go test ./...` et `go vet ./...`. Les tests SDK
s'exécutent séparément dans `engine/third_party/hyperliquid-sdk/`, sans variables
`ENDPOINT`/`QUICKNODE_ENDPOINT` pour exclure les tests réseau optionnels.
Ces vérifications ne prouvent pas une performance de trading ni un CPM futur.
