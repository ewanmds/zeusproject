# Audit MID ? maintenance REST et execution

Mise a jour du 7 novembre 2026 : voir [execution WebSocket et automatique maker uniquement](../mm-quality/ws-maker-report.md).

Correction PnL du 6 novembre : voir [protection MID et bilan final](pnl-report.md).

Mise a jour du 6 novembre 2026 : voir [MID et BBO rapides](fast-report.md)
pour la correction du flux, des delais apres fill, les nouveaux p90 et les
replays fast-before/fast-after. Les mesures et empreintes ci-dessous concernent
la premiere passe MID, conservee comme reference historique.

Le mode MID Python est corrige et instrumente. Les lectures periodiques sont
isolees dans un thread en lecture seule par processus MID. Les precontroles
necessaires a une soumission restent synchrones et autoritaires.

Aucun ordre reel, demarrage de bot, deploiement ou redemarrage serveur n'a ete
execute pendant cette implementation. Les modifications deja presentes dans
le workspace ont ete conservees. Le moteur Go Freqtrade n'est pas remplace
par ce worker Python et aucun changement Go n'etait necessaire pour cette etape.

## Mesures observees et limites

Les fichiers [before.json](before.json), [after.json](after.json) et
[maintenance.json](maintenance.json) contiennent les percentiles p50/p95/p99,
les tailles d'echantillon et les compteurs bruts. Ils proviennent d'un exchange
factice sans matching, sans file d'attente exchange et sans fills.

Le premier replay effectue 1 000 cycles par scenario. La lecture user_state et
la reponse SDK dorment chacune 1 ms ; les autres lectures sont des mocks sans
delai. Les delais observes incluent l'ordonnanceur Windows et sa charge, non
controlee. Ils ne mesurent ni le reseau reel ni la latence Hyperliquid.
Les precontroles de signature et leur journal sur disque ne sont pas executes
par ce replay : leurs temps live restent indisponibles, pas estimes.

| Mesure du replay | Avant | Apres |
|---|---:|---:|
| Requetes executees / 1 000 recotations | 2000 | 1000 |
| Lectures compte / 1 000 recotations | 3000 | 2000 |
| Cycle recotation p50 (ms) | 4.9178 | 3.2498 |
| Cycle recotation p95 (ms) | 12.3799 | 4.1168 |
| Cycle recotation p99 (ms) | 20.0124 | 4.7892 |
| Evenement ? reponse SDK p50 (ms) | 4.8215 | 3.0855 |
| Evenement ? reponse SDK p95 (ms) | 12.1811 | 3.885 |
| Evenement ? reponse SDK p99 (ms) | 18.9421 | 4.5913 |
| Cycle sans ordre p50 (ms) | 0.0263 | 0.0476 |
| Cycle sans ordre p99 (ms) | 0.0584 | 0.1687 |
| Prix perimes : carnet change pendant REST / 1 000 | 1000 | 0 |

La reduction du nombre de requetes et la suppression du prix obsolete sont
reproduites par les tests. Les changements de percentiles sont des observations
locales : les queues de distribution varient avec la charge du poste.
L'instrumentation coute du CPU et des acquisitions de verrou, notamment dans
le scenario sans ordre ; elle n'est pas gratuite.

### Maintenance artificiellement lente

Les deux controles font les memes trois lectures factices de 10 ms chacune,
sans requete de trading. Chaque variante a 50 echantillons. Le controle inline
appelle l'ancienne sequence sync_position + reconcile_orders avant apply_intent.
Le controle worker utilise _maintenance_tick puis apply_intent, avant de
consommer la reponse du thread. La decision reste locale ; cette mesure ne
signifie pas qu'un ordre ouvrant peut contourner les controles de fraicheur.

| Mesure (ms) | Controle inline | Worker |
|---|---:|---:|
| Decision p50 | 31.7809 | 0.1512 |
| Decision p95 | 32.4744 | 0.3869 |
| Decision p99 | 33.0114 | 0.4861 |
| Fin des trois lectures p50 | 31.7827 | 31.7805 |
| Fin des trois lectures p95 | 32.4756 | 32.2792 |
| Fin des trois lectures p99 | 33.0131 | 32.3335 |

Le worker retire l'attente reseau de la decision ; il n'accelere pas les
lectures. Leur temps total peut augmenter avec l'ordonnancement et le transfert
du resultat. Les fills, le taux maker/taker, les frais, le PnL net et le cout par
million restent **indisponibles** dans ces replays. Aucun gain de rentabilite
n'est deduit de ces chiffres.

## Chemins critiques examines et corrections

| Chemin | Constat / traitement |
|---|---|
| order_book._on_l2 | Validation finite et temporelle, horodatage monotone local, derniere version du carnet conservee. Les evenements sont regroupes par la boucle, sans file illimitee de decisions. |
| compute_mid_intent / _execution_price | Mid +/- distance configuree, arrondis au tick. Les sorties normales ne sont plus discretement ramenees au BBO pendant une fenetre bank. Les exceptions de liquidation d'urgence restent separees. |
| apply_intent | Un ordre inchang? conserve son age et sa file. Les sorties reduce-only MID utilisent modify lorsque prix/taille changent, au lieu d'annuler puis recreer. BBO conserve son chemin de recotation. |
| _position_checked_requests | Position perp et ordres ouverts encore verifies pour les sorties, quantite plafonnee au restant reel, ancien OID exclu lors d'un modify. Le solde spot d'affichage ne bloque plus ce precontrole MID. |
| _prepare_mid_submission | Dans le verrou de signature : Stop/superviseur, revision de position, fraicheur et dernier prix MID rev?rifies apres REST et l'attente du signataire. Une deferral locale prouvee sans envoi retire seulement ses CLOIDs de l'incertitude. |
| modify_quotes / tracking ACK | Le tracking utilise le prix effectivement envoye apres recalcul final. Rejets post-only et ordres deja termines sont reconcilies ; les autres quotes acceptees restent suivies. |
| _on_user_fills | Champs valides avant application/deduplication. Un fill malforme ne modifie pas partiellement la position. Roles maker/taker/inconnu distingues ; comptabilite incomplete signalee. |
| sync_position / reconcile_orders | Revision d'execution et fill verifiees. Les ordres personnels ne sont pas adoptes dans le tracking MID. Une ancienne lecture ne peut pas ressusciter un ordre. |
| safety / signing | Intention durable avant envoi, generation au debut et a la fin d'une requete signee, nonce interprocessus Python conserve. Un resultat inconnu n'est pas rejoue. |
| write_stats | Writer dedie, un snapshot en attente rempla?able, ecriture atomique et publication finale bornee. Les journaux de securite restent synchrones. |
| _trading_loop / run | Maintenance periodique en arriere-plan uniquement pour MID, consommation des resultats sur le thread de trading, arret du worker borne avant nettoyage exchange. |

## Contrat du worker

`zeus/mid_maintenance.py` ne recoit aucun client Exchange. Il cree sa propre
session HTTP dans son thread, ne fait que trois types de lectures /info fixes,
puis ferme la session dans ce meme thread. Il ne modifie jamais la position,
les ordres ou le latch de securite du bot.

- Une lecture active, une demande en attente fusionnee, un resultat immuable.
- Cadences conservees : position/equity 5 s, ordres 10 s, verification des ordres
  suspects et grace apres quote conservees. Le travail n'est pas accumule.
- Le resultat porte session, revision et etat in-flight captures a la demande.
  Un fill, message d'ordre, debut/fin de requete ou lecture autoritaire peut
  l'invalider. Seule la boucle principale applique un resultat encore valide.
- Fraicheur mesuree depuis le debut des lectures, pas depuis leur livraison.
  Donnees position >5 s ou ordres >10 s : pas de nouvel ordre ouvrant ni de
  modification ouvrante. Les ordres deja au repos ne sont pas annules pour
  cette seule raison ; les retraits de protection restent possibles.
- Une sortie reduce-only peut continuer avec son propre precontrole frais.
  Une reponse de maintenance n'est jamais une autorisation suffisante d'envoi.
- Erreur critique valide : interlock existant conserve ; jamais zero/[] par
  defaut. Erreur spot optionnelle : ancienne valeur affichee conservee et
  equity_stale signale, sans rendre la position arbitrairement inconnue.
- Stop interdit les demandes suivantes et les lectures suivantes lorsque le
  latch est ferme. Les resultats tardifs sont abandonnes. Attente join <=1 s ;
  un appel HTTP deja parti peut finir ensuite dans ce thread daemon, sans
  publication ni effet sur le trading. Timeout HTTP conserve a 10 s.

## Metriques et compatibilite

Les statistiques JSON et /api/grid/status exposent le champ additionnel
`latency`, avec des fenetres bornees de 1 024 observations :

- l2_callback, event_queue_delay, decision, event_to_decision ;
- api_lock_wait/hold, position_preflight, signing_lock_wait, durable_journal ;
- book_age_at_submit, event_to_submit, sdk_to_response, event_to_ack ;
- temps des lectures maintenance, stats_write et compteurs d'evenements,
  rejets, reponses, coalescence et resultats ecartes/appliques.

`event_to_ack` termine a la reponse SDK, y compris un rejet explicite ; il ne
signifie pas un fill ou une acceptation. Les erreurs transport sont mesurees
separement. `latency.maintenance` expose active/pending/alive/stopping,
position_age_ms, orders_age_ms, opening_blocked, last_error et equity_stale.
Les champs existants sont conserves ; aucune modification du frontend n'est
requise. maker_fills, taker_fills et unknown_role_fills restent distincts.

## Cout et risques residuels

- Les precontroles REST avant soumission peuvent encore retarder un envoi.
  Ils n'ont pas ete remplaces par une hypothese sur l'inventaire.
- Le verrou de signature Python couvre l'action complete par agent. Son attente
  et les ecritures du journal sont mesurees, pas supprimees. La croissance du
  journal et les clients externes/Go utilisant le meme agent restent a surveiller.
- Sur un carnet tres actif, davantage de resultats de maintenance peuvent etre
  ecartes. Les ouvertures restent suspendues lorsque leur verification expire ;
  c'est un choix conservateur, pas une relance automatique.
- Une sortie maker au decalage MID peut attendre davantage ou etre moins souvent
  remplie qu'une sortie au touch. Les tests ne prouvent pas une baisse du cout/M.
- L'historique exchange, les fills perdus pendant une coupure et le funding ne
  sont pas reconstruits par ce worker. Les metriques ne sont pas un audit
  financier complet. Les mesures live avant/apres n'ont pas ete collectees.
- Le bloc de protection reseau de risque (feed mort) reste explicite et peut
  encore faire du REST synchronement ; le worker ne modifie pas cette urgence.

## Reproduction hors ligne

```powershell
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s integrations/freqtrade/tests -p 'test_*.py'
python tests/benchmark_mid.py --source .zeus-state/mid-audit-before-20261105/order_book.before.py --cycles 1000 --output artifacts/mid-audit/before.json
python tests/benchmark_mid.py --cycles 1000 --output artifacts/mid-audit/after.json
python tests/benchmark_mid_maintenance.py --output artifacts/mid-audit/maintenance.json
```

Le premier snapshot source est une sauvegarde locale ignoree par Git ; son
empreinte ci-dessous identifie exactement le controle avant. Ne pas substituer
une autre version puis attribuer les differences a cette seule correction.

- Python : 3.12.2
- Source avant SHA256 : `2dbb949d1c7a80aabe6f6010a7e95f5eaa96210602537440c781a823a9227bf8`
- Source apres SHA256 : `c02e3f915176f905f26ef968f1707415d4561e79ca3a19cde76c01fa52c6069a`

La validation comprend fills partiels, reponse mixte post-only, Stop avant
signature, timeouts, resultats retardes apres fill/modify/cancel, session changee,
requete en cours, exclusion des ordres personnels, coalescence, worker bloque,
writer lent, nonce partage et contrat de /api/grid/status. Aucun test ne lance
un moteur reel ni un ordre sur l'exchange.

## Validation executee

- Suite principale : 202 tests reussis.
- Compatibilite adaptateur Freqtrade : 19 tests reussis.
- Les 22 tests de maintenance/isolation/API ont ete reexecutes apres le dernier
  durcissement : une lecture critique echouee rend aussi la position inconnue,
  sans remplacer sa derniere quantite par zero. Ils passent tous.
- Compilation syntaxique Python et git diff --check reussis.
- Aucun serveur ou bot actif n'a ete redemarre pour charger ces modifications.
  Le worker sera utilise au prochain lancement manuel d'une session MID.
