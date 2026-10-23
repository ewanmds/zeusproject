# MM Grid Adaptive dans Zeus

Le mode `grid_adaptive` utilise **le moteur original complet**, copié depuis
`Favorites/crypto/market-making-experiments/MM_GRID_ADAPTIVE_version_originale.py`.
`grid_adaptive/original.py` ne change que l'import relatif de `cost_control`.
La comptabilité `grid_adaptive/cost_control.py` est une copie identique du fichier source.

Empreintes SHA-256 des fichiers source au moment de la copie :

- Moteur : `3eef6e5a3c690ca795cdded03c3b97be3cb974fc6d5a4a14318cbaa67f5067e2`
- Comptabilité : `8f05b8eee95e7ab83c715c9fb3232c0a16c2310b2f40339d7fdc49e1325cd1b4`

## Logique conservée

- Une couche par côté, inventaire maximal d'une taille d'ordre.
- Score de régime : Kaufman ER sur 20 bougies et profil de volume sur 90
  bougies d'une minute. Minimum 30 bougies ; historique chargé au démarrage.
- Le score de tendance continue de moduler le spread. À la demande de
  l'utilisateur, `TREND_FILTER_ENABLED=False` dans le profil Zeus : aucune
  coupure de quotes pour tendance, même si le score dépasse 0,45.
  L'attente de l'historique initial (warmup) reste inchangée.
- Spread : maximum du tick, des frais maker estimés + 0,35 bp, et de l'ATR × 0,25,
  puis élargissement selon le score de régime. Le BBO est rejoint quand le
  spread du carnet couvre le spread protégé (`safe_bbo`).
- Déplacement du centre selon l'inventaire, sorties reduce-only, cible POC
  pendant une suspension de régime.
- Conservation en file avant un déplacement de 8 ticks de l'ancre ; annulations
  ordinaires espacées de 2 s, placements espacés de 0,5 s. Ce n'est pas un
  repos garanti contre un changement de risque.
- Réconciliation des réponses ambiguës, fills partiels, événements désordonnés
  et rejets ; aucune nouvelle soumission tant que l'état reste incertain.
- SL 10 % / TP 15 % de la marge, pause de risque 20 s, fenêtre de mise à plat
  lun–ven 15 h 25–16 h 45 **heure de Paris** (DST géré par le moteur).
- Arrêt : annulation des ordres et tentative de clôture reduce-only IOC.

Le fichier de documentation voisin du source mentionne une pause de 90 s
après breakout. **Le code actuel n'implémente pas cette règle** : il utilise
l'hystérésis du score de régime. Le profil Zeus désactive uniquement le blocage
en tendance ; il conserve l'ATR et l'élargissement selon le score.

## Adaptateur Zeus

`grid_adaptive/runtime.py` ajoute le fichier d'arrêt, les statistiques JSON
atomiques et les compteurs d'actions réellement soumises, y compris les tentatives
échouées. Le moteur original fait des annulations/placements, sans modifications.
Sous Windows, chaque snapshot utilise un fichier temporaire unique, fermé avant
le remplacement. Un verrou de lecture déclenche au maximum trois tentatives
(30 ms d'attente cumulée). Si l'écriture reste impossible, le snapshot précédent
est conservé et le prochain cycle de télémétrie réessaie : cette erreur de
statistiques ne déclenche ni arrêt ni clôture de position. L'avertissement de
journal est limité à un toutes les 30 secondes par fichier.
Le verrou par symbole est partagé avec BBO/MID ; le verrou du bot original
est également pris. Les deux modes existants ne sont pas modifiés.

Le marché, la précision, le dimensionnement au lot supérieur et la contrainte
isolated proviennent des métadonnées du contrat. La quantité est fixée au
lancement, comme dans le source ; le notionnel varie ensuite avec le prix.
Le levier utilise `OBI_LEVERAGE` (20 par défaut), plafonné au maximum du contrat.
Les frais maker sont une **estimation réglable**, 0,288 bp par défaut, pas un
tarif vérifié. Les journaux sont dans `logs/mm_grid/` et exclus de Git.

Le démarrage se fait depuis le sélecteur **MM Grid Adaptive** de Zeus, puis
le bouton de lancement existant. La commande dédiée exige aussi `--live` :

```text
python ADAPTIVE_GRID_BOT.py --token SP500 --notional 15 --grid-maker-fee-bps 0.288 --live
```

Cette commande place des ordres réels. Les tests n'exécutent pas cette commande.
Le mode n'est pas démarré par l'installation ni par la vérification du code.

## Validation

```text
python -m compileall -q grid_adaptive ADAPTIVE_GRID_BOT.py bot_server.py
python -m unittest discover -s ./tests -p test_stats_io.py -v
node --check frontend/app.js
```

CPM réalisé = `(frais - PnL réalisé) / volume exécuté × 1 000 000`, hors funding.
Les vérifications hors ligne (dont la suite du fichier source, avec les clients
simulés) vérifient le code et la comptabilité ; elles ne prouvent
ni une hausse des fills, ni une baisse du CPM, ni un objectif de rendement.
