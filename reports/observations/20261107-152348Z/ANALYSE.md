# Observation live Outcome ? 7 novembre 2026

Fen?tre : 22:23:48?22:34:53 (Bangkok), soit 11.08 minutes. Lecture seule pendant la mesure ; aucun r?glage modifi? dans les workers observ?s.

## R?sultat de la fen?tre

- Volume ex?cut? : 107.76836 $, soit 9.72 $/min.
- 12 nouveaux fills, tous signal?s maker (`crossed=false`) par l'exchange.
- Variation du PnL r?alis? net d?clar? par les workers : -0.02309004 $.
- Frais des fills de la fen?tre : 0.04139004 $.
- Co?t net observ? : 214.26 $ par million de volume ex?cut?. Ce ratio sur un petit ?chantillon ne pr?dit pas une performance future.
- Statuts WebSocket observ?s : {'filled': 11, 'open': 54, 'canceled': 44}. Un fill peut ?tre partiel : le nombre de fills ne se compare pas directement au nombre d'ordres enti?rement ex?cut?s.
- 44 annulations, dur?e de vie m?diane 3,35 s ; 29 avant 5 s. La connexion a commenc? apr?s le lancement : ces compteurs ne couvrent pas les premi?res minutes.

## March?s travers?s

Les chiffres ci-dessous couvrent les workers entiers, dont Napoli commenc? avant la fen?tre ; ils ne doivent pas ?tre additionn?s comme r?sultat de la seule fen?tre.

| March? | Dur?e worker | Volume ex?cut? | Fills | PnL net d?clar? |
|---|---:|---:|---:|---:|
| Napoli | 00:05:02 | 53.50206 $ | 10 | -0.02106456 $ |
| NBA Regular Season: Atlanta Hawks v Los Angeles Lakers | 00:03:04 | 0.00000 $ | 0 | 0.00000000 $ |
| NBA Regular Season: Portland Trail Blazers v San Antonio Spurs | 00:05:31 | 80.42460 $ | 8 | -0.01259005 $ |

## Conclusions et am?liorations

1. **Conserver les ordres partiellement remplis.** Le code pr?c?dent pouvait retirer le reliquat d'un bid parce que la capacit? restante ne suffisait plus pour un nouvel ordre de 10 $. Ce reliquat est pourtant un ordre existant, d?j? financ?. Ne pas ouvrir de nouvelle petite vente normale apr?s un achat partiel ; attendre le montant complet. Les sorties de s?curit? et drains restent possibles.
2. **Deux ordres de 10 $ demandent de l'inventaire et du capital.** ? 11 $ de plafond, une position de 5 $ laisse seulement 6 $ de capacit? : un nouveau bid de 10 $ est impossible. Le plafond autoris? de 21 $ permet g?n?ralement un ask et un bid complets, apr?s constitution d'inventaire. Le minimum en tokens entiers peut emp?cher cette simultan?it? sur certains prix ; ne jamais d?passer le plafond pour la forcer.
3. **Le plus gros $/min n'est pas toujours le plus accessible.** Hawks?Lakers a conserv? un m?me bid au meilleur prix pendant plus de deux minutes sans fill. Portland?Spurs a ex?cut? 80,42 $ en 5 min 31. Prochaine am?lioration ? tester : classer aussi les flux agressifs utiles, le temps de file estim? et le rendement ex?cut? de nos propres ordres. Un gain de volume apr?s ce changement reste ? mesurer.
4. **Tester une temporisation avant les petits repricings.** Beaucoup d'annulations surviennent vers 3 s. Comparer, sur des fen?tres distinctes, le temps r?ellement cot?, la position dans la file, les fills et le co?t par million. Une quote ancienne loin du meilleur prix peut aussi r?duire les fills : augmenter aveugl?ment la dur?e de repos n'est pas valid?.
5. **S?parer volume ex?cut? et volume cr?dit?.** La documentation officielle indique que le comptage Outcome d?pend des transactions donnant lieu ? frais. La somme prix ? quantit? du bot n'est donc pas une preuve du volume cr?dit? par la plateforme : https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees#outcome-tokens

## Application apr?s la mesure

Ancienne cha?ne Auto arr?t?e avec position v?rifi?e ? z?ro et arr?t confirm?. Nouvelle session `bea9e1a691c447218bdc366a0c1efd09`, Auto Maker, 10 $ par nouvel ordre, plafond 21 $. La politique `full_size_quotes` conserve les reliquats passifs financ?s ; aucune nouvelle vente normale sous le montant cible, aucun short de tokens non poss?d?s. Les sorties de s?curit? peuvent rester inf?rieures ? 10 $.

127 tests Outcome passent dans une copie isol?e avant publication des fichiers, dont six nouveaux tests couvrant les fills partiels, les deux c?t?s, le plafond et le drain. Les fichiers publi?s sont identiques aux fichiers test?s. La nouvelle r?gle est activ?e dans les workers Auto sans red?marrer le serveur du dashboard.

Sources d?taill?es : `samples.jsonl`, `summary.json`. Aucun secret ni adresse du compte n'est enregistr? dans ces rapports.
