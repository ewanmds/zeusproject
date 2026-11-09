# Audit des pertes de prix Outcome — 9 novembre 2026

## Périmètre et méthode

Lecture seule des endpoints publics `userFills`, `userFillsByTime`, `orderStatus`
et `candleSnapshot` du compte concerné. Les données ont été sauvegardées dans
`output/outcome-diagnosis/` ; aucun ordre n'a été envoyé.

L'échantillon des 2 000 derniers fills contient 1 943 fills Outcome. Une
requête indépendante depuis le 8 novembre à 00:00 UTC retourne 816 fills,
dont 759 Outcome, et retrouve tous les fills de la période étudiée ci-dessous.
Les données concernent l'activité du compte : elles ne prouvent pas à elles
seules quel processus, version ou session Zeus a soumis chaque ordre.
Les journaux locaux n'identifient pas les ordres des principaux cas récents.

Les heures ci-dessous sont celles de Bangkok (UTC+7).

## Résultat du 9 novembre, 00:00 à la dernière exécution à 20:37:43

| Mesure | Valeur |
| --- | ---: |
| Fills Outcome | 674 |
| Volume exécuté brut | 10 192,0222 USD |
| PnL réalisé sur les prix | −4,06547996 USD |
| Frais | 4,02341715 USD |
| PnL réalisé net | −8,08889711 USD |

Sur cette journée locale, mouvements de prix et frais contribuent chacun à
environ la moitié de la perte nette. Sur la fenêtre distincte du 9 novembre
00:00–13:37:43 **UTC**, les prix contribuent à −4,40006996 USD et les frais à
2,75634896 USD, soit environ 61,5 % de la perte nette dus aux prix. Le choix
du fuseau et de la période change donc les proportions.

L'audit reconstruit 248 cycles complets de position nulle à position nulle
clôturés ce jour-là. Leur comptabilité est cohérente avec les cash-flows :
aucun écart supérieur à 0,00001 USD. Parmi eux :

- 214 cycles ont un PnL brut positif ; seulement 12 un net positif.
- Gain brut médian des cycles gagnants sur les prix : **0,00059 USD**.
- Perte brute médiane des cycles perdants sur les prix : **0,01994 USD**.

Les cycles avec inventaire initial non observé, rupture de position ou
position encore ouverte sont exclus des statistiques de cycles complets,
mais leurs fills restent dans les totaux réalisés. Ces cycles ne sont pas
assimilés aux sessions logiques du dashboard.

## Cas 1 : Houston / Golden State, Yes — exposition longue et trou de liquidité

- Achat de 35 tokens à **0,64364**, à **10:32:57,924** (`#279860`).
- Ventes de 4 tokens à **0,56473**, puis 31 à **0,56329**.
- Fin à **11:15:19,804** : **42 min 21,88 s** après le premier fill.
- PnL brut **−2,80649 USD** ; frais **0,01514565 USD**.
- Les fills de vente sont maker (`crossed=false`) : une vente maker peut
  réaliser une grosse perte si le prix a baissé depuis l'achat.

Les bougies affichent zéro volume de 10:33 à 10:41 inclus. Après quelques
échanges à 10:42–10:43, la bougie de 10:45 descend à **0,56316**. Les bougies
sans volume ne permettent pas de reconstruire les bids, la profondeur ou
la liquidité réellement disponible pour sortir. Un stop n'aurait donc pas
nécessairement exécuté au prix de son seuil pendant ce saut.

Le garde d'inventaire actuel a été introduit dans le commit `a8aa6dc`, à
**12:06:04**, après ces exécutions. Cette perte ne peut pas servir de preuve
que ce garde déjà déployé aurait échoué. L'ancien maintien de l'exposition
et l'absence de liquidité constituent les faits observables.

## Cas 2 : Atlanta / Lakers, No — choc rapide puis réentrée pendant la baisse

Marché `#270391` :

1. Bid maker placé à **15:05:49,466**, rempli à **15:05:53,748** pour 28
   tokens à **0,54469**.
2. Vente IOC à **15:05:57,164**, exécutée à **0,52861** :
   **−0,45024 USD brut en 3,416 s**, environ −295 bps.
3. Nouveau bid maker placé à **15:06:02,177**, seulement **5,013 s** après
   la vente. Premier fill à **15:06:09,026**, soit 11,862 s après la vente.
4. Achat de 29 tokens à **0,52862**, puis ventes à **0,52050 / 0,51967** :
   **−0,25208 USD brut** supplémentaires.

La bougie de 15:05 clôture à 0,52861 ; celle de 15:06 descend jusqu'à
0,51863. La seconde entrée survient donc après un choc déjà observable.
En revanche, les données disponibles ne prouvent pas qu'un filtre fondé
sur les seules bougies précédant la première entrée aurait prévu ce choc.

Le statut public de la première vente confirme un IOC, et ceux des ordres
suivants confirment des limites post-only. Ils ne donnent pas le motif
interne : stop d'inventaire, arrêt manuel et rotation restent à distinguer
avec le journal d'exécution. La fenêtre de 5 secondes correspond à la
temporisation actuelle du code, mais ne suffit pas à attribuer sa cause.

## Défaillances reproduites dans le code actuel et corrections locales

1. **Prix d'entrée inconnu après reprise d'inventaire.** Une position
   synchronisée par REST ne récupérait pas `entryNtl / total`. Le stop
   relatif exige `avg_entry_px > 0` et pouvait rester inactif. Le coût
   d'entrée positif fourni par l'exchange est désormais restauré. Si le
   champ est absent ou nul, aucun prix d'entrée n'est inventé.
2. **Désaccord REST / fills.** Si REST avait déjà intégré un achat ou une
   vente, le callback pouvait additionner ou soustraire sa quantité une
   seconde fois. Il utilise désormais `startPosition` pour le solde après
   fill et conserve le coût REST plus récent au lieu de le pondérer deux
   fois. La réconciliation reste obligatoire avant les nouveaux ordres.
3. **Pause après stop incomplète.** `inventory_stop_until` pouvait afficher
   une pause sans interdire un nouveau bid. Le contrôle des achats respecte
   maintenant cette pause existante de 5 secondes ; les ventes restent
   possibles. **Cela ne suffit pas à empêcher la réentrée observée après
   5,013 s de délai de placement** : choisir une pause plus longue reste
   une décision de stratégie à tester, pas un résultat démontré ici.
4. **Motif perdu après un fill.** Le dernier motif de sortie forcée, son
   prix d'entrée, sa source, le carnet observé, le prix limite et le statut
   de soumission sont conservés dans `inventory_risk_context` des snapshots
   du worker. Une réponse reçue ne prétend pas que la position est clôturée.

Ces corrections n'attribuent pas rétroactivement les grosses pertes à ces
bugs. Elles réparent des cas reproduits hors ligne et rendent l'analyse
des prochaines sorties vérifiable. Aucun seuil de stop, montant, plafond,
temporisation configurée ou filtre de frais n'a été retouché dans cet audit.

## Priorité pour l'optimisation suivante

Le critère « beaucoup de dollars/minute et une petite file » peut attirer
le bot pendant une correction violente des probabilités. Les fills restent
rapides alors que chaque petit gain de spread s'accompagne d'un risque de
perte de prix beaucoup plus important.

La priorité à évaluer est une suspension des réentrées sur le marché après
un choc ou une sortie en perte, puis une sélection d'un autre marché
uniquement après inventaire nul confirmé. Il faut mesurer le coût par
million, les pertes de prix, les occasions manquées et les délais sur
plusieurs épisodes avant de retenir un délai ou un seuil de mouvement.
Un filtre de frais de 1 bp ne traite pas à lui seul cette asymétrie.

Les derniers fills récupérés précèdent le redémarrage EC2 confirmé à
20:40:36. Ils ne constituent pas une validation en live du correctif de
frais, ni des nouvelles corrections locales de cet audit.

Sources : [API publique Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint),
réponses sauvegardées localement, historique Git et tests de régression.
