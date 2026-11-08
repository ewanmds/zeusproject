# HIP-4 : volume au moindre coût — 7 novembre 2026

## Conclusion

Le market making sélectif, post-only, sur quelques marchés sportifs avant événement est la stratégie à tester en priorité. Conserver la priorité de file et plafonner l'inventaire. Ce choix est étayé par les spreads, sans rentabilité démontrée. Frais effectifs, volume crédité et fills réels restent à vérifier.

## Méthode

Lecture publique de api.hyperliquid.xyz/info : outcomeMeta, allMids, l2Book et recentTrades. Aucun compte interrogé, aucun ordre envoyé, aucun bot modifié ou lancé.

- Catalogue : 306 outcomes, 26 questions ; out 200, skew 66, txyz 25, fox 7, huit sans venue explicite, classés ici comme récurrents.
- 306 carnets côté 0, un par outcome : 278 avec bid et ask, aucune erreur au premier passage. Les côtés partagent leur liquidité : ne pas additionner leurs profondeurs.
- Premier balayage vers 20:27:06–20:28:15, Bangkok, UTC+7. Ces relevés par lots ne sont pas une photographie atomique.
- Transactions récentes sur 19 candidats choisis par spread ou profondeur : réponses de huit à dix transactions côté 0. Fenêtres tronquées, sans mesure complète à cinq minutes ou à 24 heures.
- Second relevé : six passages sur six marchés, espacés d'environ huit secondes plus latence. Une requête BTC a échoué. Cette durée ne valide ni volatilité robuste ni rentabilité.

## Carnets observés

| Venue | Outcomes | Carnets bid/ask | Spread médian, bps du midpoint | Spreads ≤ 1 bp |
|---|---:|---:|---:|---:|
| out | 200 | 176 | 40,05 | 57 |
| récurrents | 8 | 7 | 1 870,90 | 0 |
| txyz | 25 | 24 | 18 798,71 | 0 |
| skew | 66 | 66 | 19 492,00 | 0 |
| fox | 7 | 5 | 19 489,33 | 0 |

Ces médianes mélangent événements et prix parfois presque 0/1 ; elles ne mesurent pas la rentabilité des venues.

Profondeurs au meilleur prix côté 0, en USDC :

| Marché | Coin | Bid | Ask | Spread bps | Profondeur bid / ask |
|---|---|---:|---:|---:|---:|
| Seattle / Arizona | #249900 | 0,78717 | 0,78718 | 0,127 | 1 975 / 1 141 |
| Chicago / Tampa | #249930 | 0,71640 | 0,71641 | 0,140 | 305 / 224 |
| Oklahoma / Washington | #258540 | 0,82804 | 0,82805 | 0,121 | 477 / 978 |
| Detroit / Lakers | #258670 | 0,52999 | 0,53000 | 0,189 | 321 / 526 |
| BTC récurrent | #278250 | 0,41278 | 0,42015 | 176,966 | 22 / 13 |
| ETH récurrent | #278260 | 0,40300 | 0,44098 | 900,021 | 8 / 53 |

## Flux et comportement

Seattle/Arizona : dix transactions côté 0, 2 002,48 USDC, sur 14,57 minutes entre première et dernière ; dernière transaction vieille d'environ 180 secondes à l'interrogation. Meilleurs prix inchangés aux six relevés suivants. Candidat à mesurer, sans garantie de fills.

Oklahoma/Washington : dix transactions, 5 487,29 USDC ; dernière transaction vieille d'environ 14,5 minutes. Un spread serré peut coexister avec un flux discontinu.

Chicago/Tampa : six meilleurs bid/ask différents au second relevé. Spread de 0,00001 à 0,00079 ; amplitude du midpoint de 0,00089. Le premier spread ne décrit pas tout le comportement.

ETH récurrent : amplitude du midpoint de 0,0065 ; spread initial de 0,03798. BTC récurrent : environ 9 209 USDC sur cinq niveaux bid, mais 13 USDC au meilleur ask et 78 USDC sur cinq niveaux ask. La profondeur agrégée peut masquer une sortie immédiate faible.

## Mécanique importante

Deux tokens complémentaires par outcome ; règlement classique 1/0 pour un binaire. Les labels viennent de sideSpecs et peuvent être des noms d'équipes. Le prix est un prix de marché, pas une probabilité validée.

Acheter A à p correspond économiquement à vendre B à 1-p : carnet fusionné. Priorité prix, côté, temps ; au même prix fusionné, les ventes au repos précèdent les achats du côté dual. Détenir les tokens peut donner une priorité différente d'une ouverture duale ; mesurer cet avantage avec son coût de capital.

Split : X collatéral devient X tokens de chaque côté ; merge : opération inverse. Ces conversions ne constituent pas automatiquement du volume crédité. L'arbitrage entre issues distinctes exige exhaustivité, fallback, règles de résolution, prix exécutables, profondeur et frais. Un batch ne garantit pas deux fills simultanés complets.

Les binaires crypto dépendent de la distance au seuil, du temps restant et de la volatilité. Un hedge perp ne supprime pas le risque non linéaire près du règlement et ajoute des frais. En sport, nouvelles d'effectif et début de match peuvent créer des sauts.

## Frais : contradiction officielle

La page HIP-4 mentionne encore les frais nuls des premiers tests. La page Fees décrit des frais à la clôture ou au règlement, pas à l'ouverture, et aucun rebate Outcome. Cette étude publique ne confirme aucun taux applicable au compte. Contrôler barème effectif et frais des fills avant projection. Des builder codes peuvent ajouter un coût sur les ventes.

Le volume exécuté diffère du volume crédité : minting et transactions sans payeur de frais peuvent ne rien créditer. Les autres cas suivent des règles spécifiques au prix payant, au burn et au règlement. Ne pas appliquer automatiquement somme(prix × quantité) au compteur officiel. Aucun gain en points ou airdrop supposé.

## Coût réel

```text
V_exécuté = somme abs(prix × quantité), fills uniques achats et ventes
PnL_net = variation d'equity − dépôts + retraits
Coût/M exécuté = −PnL_net / V_exécuté × 1 000 000
Coût/M crédité = −PnL_net / V_crédité × 1 000 000
```

Valoriser l'inventaire à sa liquidation réalisable avec profondeur et frais. Le PnL net inclut les coûts : ne pas soustraire les frais deux fois. Le second ratio exige un volume crédité confirmé et non nul.

Au carnet figé, un aller-retour taker de q tokens perd q × (ask − bid), pour q × (ask + bid) de volume. Coût/M hors frais : 1 000 000 × (ask − bid)/(ask + bid).

Un cycle maker rempli aux deux prix donne le gain opposé seulement si les deux fills arrivent sans dérive. Seattle/Arizona : 6,35 USDC/M de gain théorique avant frais. Un frais hypothétique de 0,04 % sur la vente seule représenterait environ 200 USDC/M ; illustration mathématique, pas taux Outcome confirmé.

BTC et ETH récurrents : environ 8 848 et 45 001 USDC/M de coût taker au carnet initial figé, hors frais. Aucune prévision de fills.

## Stratégie à tester

1. Trois à cinq marchés sportifs avant événement, sélectionnés selon flux réalisable, coût net, temps d'inventaire et profondeur de sortie.
2. ALO au meilleur bid/ask lorsque fonds et inventaire le permettent. Sans token disponible à vendre : achat puis vente. Une cotation bilatérale exige un inventaire préfinancé ; étudier le split séparément.
3. Conserver une quote pertinente ; modifier sur changement réel du prix équitable, du risque ou de l'inventaire. Retrait immédiat si danger.
4. Diminuer les achats après accumulation, favoriser les ventes ; plafonner l'exposition par événement/question.
5. Écarter matchs en cours, annonces déterminantes et proximité du règlement sans modèle adapté. Calibrer la fenêtre de retrait.
6. IOC pour réduction de risque ou occasion dont le calcul net couvre spread, impact et frais. Ne pas forcer les cycles pour un quota.
7. Arbitrage entre issues distinctes en complément opportuniste, avec gestion des fills partiels.

Première simulation, paramètres non calibrés : trois marchés ; comparer 10, 25 et 50 USDC si les minimums l'autorisent. Limiter la taille par la profondeur dans une borne de slippage fixée. Comparer notamment la plage de prix 0,15–0,85, sans la présenter comme optimale. Fixer budget de perte et coût/M maximal avant un éventuel test réel ; aucun budget choisi ici pour le compte.

Mesurer le markout à 1, 5 et 30 secondes : achat q × (mid futur − prix payé), vente q × (prix reçu − mid futur). Un résultat négatif signale une évolution défavorable après le fill ; ce n'est pas le PnL final du cycle.

Acheter puis revendre environ 500 USDC produit environ 1 000 USDC exécutés. Cent cycles remplis donnent environ 100 000 USDC ; ni la file ni le marché ne garantissent ces cycles.

## Bot actuel : chemins lus, aucun correctif

Dans zeus/bots/worldcup.py :

- Fonctions round_outcome_buy_px / round_outcome_sell_px : quatre décimales, contre cinq dans certains carnets. La grille 0,0001 peut faire perdre le touch et la priorité de prix.
- desired_spot_no_order : achat si inventaire vide, puis vente des tokens détenus ; cycle sur un côté, pas moteur bilatéral équilibré.
- Dry : achat/vente attribué directement au prix demandé, volume augmenté sans simulation de file ni transactions agressives. Ses fills ne valident pas le débit maker.
- net_pnl utilisé par le stop dépend du PnL clôturé et des frais ; cette mesure seule ne couvre pas la perte latente Outcome.
- Des chemins de stop liquident en IOC : inclure cet impact dans le coût du cycle.

Ces observations ne constituent pas un audit exhaustif des superviseurs, interfaces et modes.

## Validation restante

Enregistrer au moins une journée complète avec nouvelles et événements, puis comparer des fenêtres similaires. Un jour est un premier filtre, pas preuve de stabilité. Simuler les transactions agressives et la quantité devant l'ordre ; toucher le prix ne suffit pas à attribuer un fill.

Comparer maker/maker, maker/taker et taker/taker avec mêmes limites et frais effectifs. Publier volumes exécuté/crédité, PnL inventaire inclus, coût/M, temps de sortie, fills, annulations par fill et pire inventaire. Choisir le débit maximal respectant les budgets de coût et de risque.

La prévention des self-trades annule l'ordre au repos pour une même adresse. L'analyse vise les exécutions avec d'autres participants, sans volume artificiel.

## Sources et archives

- [HIP-4](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-4-outcome-markets)
- [Frais et volume Outcome](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees#outcome-tokens)
- [Spécifications](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications)
- [Ordres, split et merge](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)
- [Encodage](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)
- [Précision](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)
- [Limites API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
- [Self-trade prevention](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/self-trade-prevention)

Archives publiques voisines : meta.json, books.json, trades.json et resample.json, avec noms complets, quantités et horodatages.
