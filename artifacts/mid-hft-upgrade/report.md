# MID HFT MM — correction complète, latence et économie

Date : 7 novembre 2026. Périmètre : `zeus/bots/order_book.py` (modes MID et BBO),
`zeus/mid_maintenance.py`, `zeus/safety.py`, `zeus/session_accounting.py`, tests.
Le moteur Go Freqtrade est distinct et n'a pas été modifié.

Aucun bot, aucun ordre réel, aucun redémarrage de serveur n'a été lancé pour
cette passe. Les benchmarks sont hors ligne (REST/signature simulés, sans
matching ni réseau). Les corrections prennent effet au prochain lancement
manuel d'une session.

## Méthode

Audit indépendant à cinq dimensions (PnL/sorties/risque, latence/hot path,
exécution/réconciliation, flux/signaux, microstructure) puis vérification
adversariale par cinq agents séparés. Les findings ont été confrontés au code
courant avant correction. Les agents ont signalé que le fichier évoluait
pendant l'audit ; les constats ci-dessous ont été revérifiés dans la version
finale.

## Constat principal

Le mode MID avait perdu son moteur de cotation : `compute_mid_intent` faisait
un simple `mid ± offset`, les sorties suivaient le mid à la baisse, `bank_until`
ne changeait pas le prix, et `fast_mm()` désactivait OFI/TFI, markout, skew,
EV gate, sweep breaker et pause de côté. La microstructure restante était
donc un MM symétrique à distance fixe, sans protection contre la sélection
adverse, avec deux lectures REST synchrones par re-quote de sortie et une
reconciliation à quatre lectures après chaque fill.

## Corrections apportées

### 1. Latence / chemin critique

- **Préflight de sortie sans REST quand l'état est frais.** Le worker de
  maintenance fournit déjà position (5 s) et ordres (10 s). Une sortie
  reduce-only ne peut pas ouvrir d'exposition : si la base de fill locale est
  connue et les lectures vérifiées fraîches, le préflight utilise le suivi
  local sans `user_state` + `open_orders`. Preuve : `benchmark_mid.json`
  passe de **2000 à 2 lectures REST** pour 1000 re-quotes de sortie.
- **Fences séparées position / ordres.** Une action de cotation ne jette plus
  une lecture de position fraîche. La position est fencée par
  `position_revision` + `position_generation` ; les ordres par une empreinte
  des ordres suivis (et non par la révision d'exécution globale). Une lecture
  de position plus récente (foreground) invalide la lecture de fond.
- **Recheck immédiat après fill**, ré-armé si le résultat est écarté, au lieu
  d'attendre la cadence de 5 s. Les ouvertures reprennent en ~un aller-retour
  de lecture au lieu de plusieurs secondes.
- **Fill → ordre mort supprimé du suivi.** Le modify d'urgence ne vise plus un
  OID déjà rempli ; plus de rafale de reconciliation multi-REST après chaque
  fill. Les fills partiels décrémentent la taille suivie.
- **Final gate sur la révision du top-of-book uniquement.** Une mise à jour
  de profondeur L2 sans changement de touche n'annule plus une action signée.
- **Rejet ALO définitif : plus de `position_dirty`.** Le côté rejeté recule de
  0,15 à 1,5 s (au lieu de 1 à 8 s) sans noircir l'autre côté ni l'inventaire.
- **ACK d'exécution borné à 2 s** (au lieu de 10 s) : une perte d'ACK ne bloque
  plus le worker d'action pendant dix secondes.
- **Signal stack limité à 50 ms de cadence** (hors fill urgent), au lieu de
  O(mids) + O(trades) à chaque événement de flux.
- **Sonde de supervision mise en cache 50 ms** : moins d'appels Win32 dans le
  gate final.
- **Emergency sweep non bloquant** : le thread WebSocket arme une pause et
  réveille la boucle ; il ne fait plus de cancel réseau synchrone qui pouvait
  retarder tout le feed.

### 2. Profitabilité / microstructure (MID)

- **Moteur de réservation réactivé** : fair = mid ajusté par le tilt
  micro-price (relatif, avec TTL 0,5 s) + alpha OFI/TFI auto-calibré + biais
  macro borné. Demi-spread = max(offset utilisateur, frais + marge, capture
  d'une fraction du spread live). Les quotes ne percent jamais le BBO.
- **EV gate au round-trip** : deux frais maker (0,576 bps), cible d'edge
  (1,0 bps par défaut) et coût adverse plancher (0,75 bps) sont exigés ; le
  côté est reculé tick par tick, ou retiré si la largeur dépasse 5 bps.
- **Toxicité des quotes profondes** : une quote derrière la touche est remplie
  principalement par des sweeps ; elle est taxée à 100 % du coût adverse
  mesuré, au lieu de 20 %.
- **Markout par côté, uniquement sur les fills d'ouverture** : les sorties ne
  polluent plus l'EWMA. Les seuils de probation sont indexés sur le plancher
  adverse (-0,75 / -1,5 bps), pas sur un zéro sans échantillon.
- **Sorties réellement exécutables** : sortie normale à la touche ; fenêtre
  bank/stop = prix post-only le plus agressif (un tick dans le spread) ;
  plancher break-even tenant compte des frais pendant 30 s ; ancre de profit
  tenant compte des deux frais + un tick ensuite ; après 60 s, touche. Une
  sortie ne suit plus le mid vers une perte réalisée pendant une fenêtre de
  risque.
- **Hystérésis de sortie directionnelle** : une sortie qui devient le meilleur
  prix reste en file ; elle n'est replacée que si le marché la rend non
  compétitive au-delà de la tolérance. Urgence fill/TP/stop = reprice immédiat.
- **Anti-adverse rebranché** : pull par z-score, momentum court/long, régime
  directionnel (on ne vend pas un pump, on n'achète pas un dump ; panique à
  l'intensité 3), early-warning, pause après sweep, cooldown après fill,
  markout soft/hard, EV gate, widening de volatilité.
- **Exclusion de nos propres ordres du signal** : les niveaux L2 sont nettoyés
  de la taille de nos quotes avant OFI/micro/OBI/queue. Le fair utilise la
  touche ajustée quand elle est fraîche. Fini le flux auto-généré.
- **Flux plus propre** : trades horodatés à la réception locale, buckets OFI/TFI
  en taux (par seconde), reset complet de l'état signal à la reconnexion,
  refus des bougies macro plus anciennes et déduplication.
- **Audit/santé** : un mark récent reste utilisable pour armer un stop/TP/limite
  de session quand le carnet est momentanément indisponible ; un mark inconnu
  n'est jamais transformé en zéro.

### 3. Sécurité / inventaire / comptabilité

- **Sorties reduce-only jamais annulées sur carnet mort ou spread cassé** :
  l'ordre passif reste au repos ; seule l'ouverture est retirée.
- **Arrêt automatique (limite de perte session)** : au lieu de couper le
  processus en laissant la position nue, le bot passe en mode « sorties
  seules » : plus aucune ouverture, fenêtre bank renouvelée, arrêt seulement
  une fois flat (ou après 120 s). Le cleanup automatique annule les ouvertures
  mais laisse l'ordre de sortie reduce-only au repos sur l'exchange.
- **Erreur de lecture de maintenance transitoire** : plus d'arrêt immédiat.
  Les ouvertures sont bloquées, les sorties restent possibles, la lecture est
  retentée à la cadence ; l'interlock ne se ferme qu'après 3 échecs consécutifs.
- **Snapshot userFills** : les fills de la session courante sont appliqués
  (les anciens restent dédupliqués), au lieu d'être tous marqués traités.
- **Bilan final** : pagination `userFillsByTime` au-delà de 2000 lignes,
  résolution des CLOIDs en attente via `orderStatus`, et
  `accounting_complete=False` si un CLOID ou l'historique reste non résolu.
- **Flag reduce-only appris** depuis orderUpdates/openOrders : moins de
  cancel/replace inutiles après une reconnexion.
- **Reconciliation d'exécution** : le chemin d'urgence ne fait plus la lecture
  spot d'affichage (3 lectures au lieu de 4) ; la seconde lecture `user_state`
  est conservée comme contrôle de fill entre deux lectures.

## Benchmarks hors ligne

Les scénarios simulent des lectures REST et des ACK ; ils ne mesurent ni le
réseau réel, ni Hyperliquid, ni le matching, ni les frais/PnL. Les queues
incluent l'ordonnanceur Windows.

| Mesure (1000 cycles sauf mention) | Avant | Après |
|---|---:|---:|
| `ro_reprice` p50 / p95 / p99 (ms) | 3,2498 / 4,1168 / 4,7892 | **1,4805 / 2,0637 / 3,0487** |
| `ro_reprice` lectures compte | 2000 | **2** |
| `moving_preflight` p50 / p95 / p99 (ms) | 3,4058 / 4,1244 / 4,5694 | **1,4894 / 2,0767 / 2,7994** |
| `moving_preflight` lectures compte | 2000 | **2** |
| MID conversion p50 / p90 / p95 / p99 (ms, 500 cycles) | 4,8606 / 5,2386 / 5,4786 / 6,2352 | **1,5508 / 1,8295 / 1,8807 / 2,7893** |
| BBO conversion p50 / p90 / p95 / p99 (ms, 500 cycles) | 4,8440 / 5,2962 / 5,5966 / 6,3216 | **1,7217 / 1,8079 / 1,9433 / 3,2204** |
| Lectures compte MID/BBO conversion | 1000 | **2** |
| Décision maintenance inline / worker p50 (ms) | 31,89 | **0,10** (worker) |
| Prix périmés soumis | 0 | 0 |

Fichiers : `benchmark_mid.json`, `benchmark_fast_mm.json`,
`benchmark_maintenance.json` dans ce dossier.

## Économie attendue (modèle, pas un PnL live)

Le gate exige, par aller-retour : `2 × 0,288 bps de frais + 1,0 bps d'edge +
0,75 bps de coût adverse`, soit ~2,33 bps avant capture de sortie. Avec un
offset utilisateur de 1 bps, une quote de 2 bps de spread est placée ~1 tick
derrière la touche ; sur un marché à 10 bps de spread, elle rejoint la touche ;
sur un carnet à 1 tick (type SP500), elle se place à ~2,4 bps du mid (16 ticks)
ou le côté est retiré si la largeur maximale est dépassée. Ces distances sont
le résultat du gate, pas des constantes codées en dur.

Hypothèse de sortie : la sortie normale rejoint la touche, le bank/stop un
tick dans le spread. Le PnL net réel dépend du markout adverse, du taux de
complétion et des frais réels du marché ; il n'est pas mesuré par ces
benchmarks.

## Réglages utiles

- `OBI_MID_ADAPTIVE=0` : retour au MID fixe `mid ± offset` (repli).
- `OBI_MID_EDGE_TARGET_BPS` (défaut 1,0) : edge visé par aller-retour.
- `OBI_MID_ADVERSE_FLOOR_BPS` (défaut 0,75) : coût adverse a priori.
- `OBI_MID_MAX_QUOTE_WIDTH_BPS` (défaut 5,0) : largeur maximale derrière la
  touche ; au-delà, le côté est retiré.
- `OBI_MID_EXIT_REPRICE_TOL_BPS` / `OBI_MID_EXIT_MIN_REST_SEC`.
- `OBI_WS_ACK_TIMEOUT` (défaut 2,0 s).
- `OBI_MID_MAINTENANCE_FAIL_LIMIT` (défaut 3 lectures consécutives en échec).
- `OBI_MID_LOSS_CUTS` (défaut 0) : 0 = mode normal, aucune sortie de perte
  ordinaire ; 1 = rétablit les fenêtres bank/scratch/early/stop/max-hold.
- `OBI_MID_CATASTROPHIC_STOP_BPS` (défaut 50) : seuil du seul stop conservé
  en mode normal (fenêtre bank passive, jamais de taker). 0 = seuil calculé
  par la volatilité (6–20 bps), pas une désactivation.
- `OBI_SPREAD_CAPTURE_FRAC`, `OBI_SKEW_BPS`, `OBI_HALF_SPREAD_BPS` restent
  disponibles mais le skew d'inventaire est inerte sous la règle actuelle
  « une seule position, pas d'ajout tant que non flat ».

## Mise à jour du 7 novembre — sorties MID « mode normal »

Le MID ne réalise plus une perte sur un signal alpha/régime/âge. Après un
fill, la sortie reduce-only reste au **plancher de profit** (entrée ± deux
frais maker ± un tick) et attend le retour du marché. Les fenêtres bank ALO
de scratch toxique, de cut pump/dump, d'early warning, de stop ordinaire et de
max-hold sont désactivées par défaut. Le take-profit (retracement du peak)
reste actif tant que la position est encore en profit, et le régime aligné en
fin de mouvement aussi. Seul un mouvement catastrophique (`50 bps` par défaut)
ouvre encore une fenêtre bank passive ; la limite de perte de session et le
Stop manuel restent inchangés.

Motif : sur un carnet ANT à 0,62 bps de spread, le bot ouvrait un short à
1608,1 puis le rachetait 4 à 7 secondes plus tard à 1609,7–1610,1 (−0,116 $
réalisés, 5 fills maker). Ce n'étaient pas les frais (0,0057 $) mais le
mouvement adverse réalisé par la sortie bank. Le tableau de bord affiche
désormais `MID · … · HOLD PROFIT` pour ce mode.

## Validation

- Suite Python : **445 tests OK** (`python -m unittest discover -s tests -p 'test_*.py'`).
- Adaptateur Freqtrade : **19 tests OK**.
- `tests/check_mid_exit_ui.py` : OK (clavier, payload MID, libellé de session
  immuable, BBO inchangé, 1440/390 px, requêtes mockées).
- `py_compile` sur les modules cœur ; `git diff --check` OK (avertissements
  CRLF préexistants).

## Risques résiduels (explicites)

- **Politique maker-only conservée.** Un stop autonome ne croise jamais le
  spread. Une tendance à sens unique peut laisser la position ouverte malgré
  la fenêtre bank ; le mode « sorties seules » et le cleanup qui laisse
  l'ordre reduce-only au repos réduisent la queue de perte, mais ne garantissent
  pas un fill. Seul un Stop manuel explicite autorise une fermeture marketable
  reduce-only.
- **Verrou de signature inter-processus** : il couvre toujours la signature,
  l'envoi WS et l'attente d'ACK. Plusieurs marchés du même agent restent
  sérialisés ; ce n'est pas corrigé ici.
- **Journal durable** : `publish()` réécrit encore les ensembles `owned/cloids`
  croissants avant chaque action signée. Non corrigé.
- **Comptabilité live** : les fills live sont comptés par coin ; un fill manuel
  étranger sur le même symbole peut contaminer le PnL live. Le bilan final est
  filtré par OID/CLOID de la session et reste l'autorité.
- **Le skew d'inventaire** reste inerte tant qu'une seule position est permise
  et que les ouvertures exigent un inventaire flat.
- **SP500 / carnet à 1 tick** : structurellement serré ; le gate élargit ou
  retire les côtés. Un marché à spread plus large ou un edge target plus bas
  est préférable pour le volume.
- **Mesures live** : aucun percentile d'exécution live, aucun taux maker/taker
  live, aucun PnL réel n'est revendiqué par cette passe.

## Reproduction

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s integrations/freqtrade/tests -p "test_*.py"
python tests/check_mid_exit_ui.py
python tests/benchmark_mid.py --cycles 1000 --output artifacts/mid-hft-upgrade/benchmark_mid.json
python tests/benchmark_fast_mm.py --cycles 500 --output artifacts/mid-hft-upgrade/benchmark_fast_mm.json
python tests/benchmark_mid_maintenance.py --output artifacts/mid-hft-upgrade/benchmark_maintenance.json
python -m py_compile zeus/bots/order_book.py zeus/mid_maintenance.py zeus/safety.py zeus/session_accounting.py
git diff --check
```
