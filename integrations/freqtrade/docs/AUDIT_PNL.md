# Audit PnL du mode Freqtrade

Date : 23 octobre 2026. Périmètre : le bot Go Hyperliquid lancé par
`zeus.bots.freqtrade` (mode « Freqtrade » du dashboard). Le mode `bbo`/`mid`
(`zeus.bots.order_book`) n'est pas modifié, mais son diagnostic est identique.

## 1. Mesure

Sources : snapshots `/tmp/hl_obi_stats_*.json` des sessions réelles du 23/10.
CPM = `(frais − PnL réalisé) / volume × 1e6` ; un CPM positif est un coût.

| Session | Durée | Volume | Fills | Frais | PnL réalisé | CPM | Spread venu |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `io:ANTH` | 25 min | 6 120,89 $ | 126 | 0,187 $ | −0,450 $ | 104 $/M | 1,55 bps |
| `io:SNDK` | 5,5 min | 1 725,81 $ | 26 | 0,055 $ | −0,247 $ | 175 $/M | 0,78 bps |
| `xyz:CL` | 21 s | 149,78 $ | 2 | 0,009 $ | −0,015 $ | 156 $/M | 1,98 bps |
| **Total** | — | **7 996 $** | **154** | **0,251 $** | **−0,712 $** | **120 $/M** | — |

Net après frais : **−0,96 $**, soit **−1,2 bps du volume**. Les frais réels sont
~0,3 bps par fill (HIP-3 growth mode), pas les 3 bps du profil d'origine.

## 2. Causes

1. **Spread sous le coût.** `quote_spread_bps: 0.7` (demi-spread) alors que le
   coût par aller-retour est ~2 × 0,3 bps de frais + ~1 bps de sélection adverse.
   Il faut au moins ~1,3 bps de demi-spread pour un edge net positif.
2. **Taille = tout le budget.** Un seul fill saturait la position, le côté entrée
   tombait à zéro (`allowedBuy = maxPosition − exposition`) et le bot devenait
   exit-only. La décote d'inventaire rendait une partie du spread.
3. **Protections qui liquident.** Le risk manager déclenchait `RiskHalt` dès que
   la position atteignait `max_position_notional` et comparait la position à
   `max_spread_exposure` (même montant). `RiskHalt` annule tout et ferme au
   marché : frais taker + slippage à chaque saturation.
4. **Stops désactivés.** `max_drawdown_pct: 100`, pas de session stop-loss,
   `absolute_equity_floor: 0`.
5. **Marché par défaut inadapté.** SP500 a un demi-spread venu de ~0,07 bps :
   impossible de couvrir les frais.
6. **Cécité.** Markout désactivé, aucun champ d'edge dans le snapshot, markout
   masqué dans la carte Freqtrade.
7. **Pièges moteur.** Troncature `orders[:perCoinCap]` sans distinction de côté
   (les ventes disparaissaient dès 2 niveaux) et plancher de frais centré sur
   le midpoint des quotes au lieu de la fair value.

## 3. Modifications livrées

### Moteur Go

- **Edge gate** (`internal/quoting/manager.go`, `internal/config/config.go`) :
  `demi-spread requis = frais mesurés + adverse mesurée + edge cible / 2`,
  appliqué au côté qui augmente l'inventaire. Frais EWMA depuis les fills réels,
  adverse depuis le markout EWMA par côté, repli sur les planchers de config.
  Au-delà de `edge_max_width_bps`, le côté n'est pas coté. Le côté sortie garde
  le pipeline skew / âge / idle-breaker.
- **Frais mesurés** : `RecordFill` met à jour un EWMA de `fee / notional`.
- **Plancher de frais** : centré sur la fair value, plus sur le mid.
- **Troncature d'ordres** : plafond **par côté** (`capOrdersPerSide`), les deux
  côtés survivent.
- **Mode fixed-sizing** : quand `signal_pauses_enabled` est faux, les tailles
  sont remises au notional configuré après les gates de signal, donc les deux
  côtés restent cotés ; l'edge gate s'applique ensuite et peut encore écarter
  un côté trop cher. Zeus force `signal_pauses_enabled: true`, donc ce mode
  n'est pas utilisé par le preset.
- **Tolérance de halt** : `max_position_halt_tolerance_pct` (0 par défaut,
  10 % en Zeus) évite de liquider une position au plafond normal.
- **Diagnostics** : `EdgeRequiredBid/AskBps`, `QuoteHalfBid/AskBps`,
  `EdgeSkipBid/Ask`, `MeasuredFeeBps` dans `DiagnosticSnapshot`.

### Mode Zeus

- `profile.json` : spread de base 2,0 bps, fee floor actif (1,5 bps), edge gate
  (cible 1,0 / adverse 0,75 / largeur max 10), markout et per-side winrate
  actifs, toxicité renforcée, réductions d'entrée, âge de position, idle-breaker,
  funding urgency, price-shock + shock-chain, trend/flow/regime gates, VPIN.
  Volume-burst, wall-fronting, optimizer v2, adaptive notional et schedule
  restent désactivés (preset agressif séparé).
- Risque : drawdown 2,5 %, cooldown 120 s, reprise 97 %, session stop-loss
  −150 bps (warn −75, pause 120 s, recovery 50 % / 300 s), tolérance de halt 10 %.
- `session_config` : **100 % du montant par côté**, un seul ordre par côté,
  `max_open_orders = 4`, exposition 1,1 × montant, minimum 40 USD.
- **Filtre de marché** : lecture `l2Book` au lancement ; refus si le demi-spread
  venu est inférieur au quart de l'edge requis. Vérifié sur les données live :
  SP500 et SNDK refusés, ANTH et CL acceptés.
- Snapshot enrichi : `markout_bid/ask_bps`, `quote_half_spread_bps`,
  `edge_required_bps`, `edge_margin_bps`, `measured_fee_bps`, `fee_bps`,
  `net_pnl`. La carte affiche le markout et « Edge requis / coté ».

## 4. Vérifications

- `go test ./...` : toutes les packages OK.
- `go vet ./...` : OK.
- `python -B -m unittest discover -s integrations/freqtrade/tests` : 18 tests OK.
- Configuration générée (100 USD, `xyz:CL`) : sizing 100 USD/côté, un niveau,
  4 ordres max (quotes et ordres de sortie compris).
- Filtre live : `xyz:SP500` et `io:SNDK` refusés, `io:ANTH` et `xyz:CL` acceptés.

## 5. Effet attendu

Avec 2 bps de demi-spread, un aller-retour capture ~4 bps ; coûts ~0,6 bps de
frais + ~1 bps d'adverse laissent
environ **+1,5 à +2 bps nets par aller-retour** si l'adverse ne s'aggrave pas.
Le CPM viserait −150 à −200 $/M au lieu de +120. En dollars, cela reste petit :
~0,02–0,05 $/h sur 40 $, ~0,15–0,40 $/h sur 1 000 $. L'objectif réaliste est un
taux positif et l'arrêt de l'hémorragie, pas un revenu significatif sur petit
capital.

## 6. Canari live (40 USD minimum)

1. Choisir `xyz:CL` ou `io:ANTH` (jamais SP500), montant 40 USD.
2. Laisser tourner 10 minutes ou 20 fills, puis vérifier : CPM ≤ 0, net ≥ 0,
   aucun `risk_halt` de flatten, inventaire < 90 % du budget, markout ≥ −1 bps,
   demi-spread coté ≥ edge requis.
3. Si le CPM est positif : arrêter, ne pas augmenter la taille, revenir au
   diagnostic (edge gate ou adverse mal calibrés).
4. Si le CPM est négatif : 3 sessions à 100–200 USD avant toute montée.

## 7. Limites

- Aucune garantie de gain : l'edge gate rend le **prix** conforme au coût mesuré,
  pas la réalisation future.
- Les fills papier restent optimistes ; le canari se fait en réel, à petite taille.
- Les nouveaux champs sont désactivés par défaut dans le moteur ; seuls les
  presets Zeus les activent.
- Pas de backtester ni de mode multi-actifs dans ce lot.
