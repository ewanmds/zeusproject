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

Le montant est un plafond de position. **25 % de ce montant est coté par côté**
(ordre minimum Hyperliquid : 10 USD), donc le minimum utilisable est **40 USD**.
Le nombre de niveaux est déduit de cette taille : 1 niveau de 40 à 79 USD,
2 niveaux de 80 à 159 USD, 3 niveaux dès 160 USD. Les tailles sont arrondies vers
le bas et les sorties sont reduce-only. Les suspensions sur signaux sont conservées comme sur le
Bureau, et les protections de données, compte, budget API et pertes restent
actives. Le levier provient de `OBI_LEVERAGE` (30 par défaut, comme
`config.live.yaml`), plafonné au contrat.

### Économie et edge gate

Chaque session applique une formule de coût, pas un spread fixe :

```text
demi-spread requis = frais mesurés + sélection adverse mesurée + edge cible / 2
```

Les frais sont l'EWMA des frais réels par fill (repli sur `maker_fee_bps` avant le
premier fill), la sélection adverse est le markout EWMA du côté concerné (repli
sur `edge_adverse_floor_bps` pendant le warmup), et l'edge cible vaut 1 bps net
par aller-retour. Le moteur élargit le côté qui augmente l'inventaire jusqu'à ce
niveau ; au-delà de `edge_max_width_bps`, il ne cote pas ce côté. Le côté sortie
reste géré par le skew, l'âge de position et l'idle-breaker pour ne pas
emprisonner l'inventaire.

Au lancement, le mode lit le carnet public et signale un demi-spread venu
inférieur à l'edge requis. Le moteur applique ensuite son edge gate à chaque
cotation : il élargit le côté qui augmente l'inventaire ou ne le cote pas si
la largeur maximale serait dépassée. Un carnet momentanément étroit ne fait
donc plus échouer le démarrage.

**Arrêter** demande au Go d'annuler et fermer, puis de vérifier la mise à plat
par REST. En cas de panne réseau ou de position résiduelle, la session reste en
échec avec position inconnue ; l'interface n'annonce pas une clôture confirmée.

Le démarrage suit le lanceur du Bureau : un seul `-check-account`, puis le
processus live directement (aucun `-validate` supplémentaire). Une fois le carnet
validé, l'état reste **Live** même si une garde temporaire bloque un côté ; le
motif est affiché dans le détail de la session.

## Télémétrie et validation

Aucun nouveau log ni post-mortem sur disque en mode Zeus. Le flux JSON est
consommé en mémoire ; seul le snapshot courant du dashboard est remplacé dans
le répertoire temporaire. Les verrouillages Windows sont non fatals.

CPM = `(frais − PnL réalisé) / volume confirmé × 1 000 000`, hors funding.
Un CPM positif est un coût net ; un CPM négatif est un gain net après frais. Le
snapshot expose aussi `fee_bps`, `net_pnl`, le markout par côté et la comparaison
`edge requis / demi-spread coté`. Sans fills confirmés, le CPM reste nul au sens
JSON `null` et s'affiche `—`.
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
