# MID et BBO : flux rapide et latence, 6 novembre 2026

Passe suivante : [protection MID et bilan final](pnl-report.md). Les empreintes
et replays de cette page identifient la version de la passe latence conservee
dans .zeus-state/pnl-before-20261106/order_book.py, avant cette nouvelle correction.

Cette passe concerne les deux moteurs Python. Freqtrade Go est conserve.
Je n'ai arrete ou recharge aucune session et aucun ordre de test n'a ete
envoye. Les corrections prendront effet au prochain lancement
manuel d'une session. Les autres modifications du workspace sont conservees.

## Diagnostic observe avant correction

Instantane en lecture seule du MID io:ANTH, environ 309 secondes de session :

| Mesure | Echantillons | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|
| Decision | 1024 | 0.0747 | 0.1130 | 0.1745 |
| SDK jusqu'a reponse, signature incluse | 44 | 749.3834 | 933.7529 | 1122.1892 |
| Precontrole position | 38 | 121.9050 | 275.7979 | 330.5941 |
| Attente verrou signature | 45 | 0.8597 | 1.2869 | 2.0051 |

L'ancien event_queue_delay repetait l'age d'un meme carnet a chaque reveil.
Son p95 de 5110.828 ms n'est donc pas une mesure valide de file d'attente.
Le p90 n'etait pas expose : il ne peut pas etre reconstruit depuis ces agregats.
Ces chiffres live viennent de l'ancienne version, pas de la correction.

Observation publique distincte : trois sockets, meme marche io:ANTH, 24 secondes,
aucune souscription privee et aucune action signee. Les intervalles concernent
les messages, pas la latence evenement exchange -> application :

| Flux | Messages | N intervalles | p50 ms | p90 ms | p95 ms | p99 ms |
|---|---:|---:|---:|---:|---:|---:|
| l2Book sans option | 6 | 5 | 5373.037 | 5439.844 | 5439.844 | 5439.844 |
| l2Book fast=true | 47 | 46 | 526.324 | 682.999 | 721.726 | 956.216 |
| bbo | 78 | 77 | 179.715 | 558.898 | 666.109 | 1067.860 |

Fenetre courte, arrivees conditionnees par l'activite du marche : ces chiffres
ne sont ni une cadence garantie ni une distribution generale. Aucune latence
reseau aller simple n'est deduite de l'horloge exchange et de l'horloge Windows.
L'option fast livre cinq niveaux, suffisants pour les cinq niveaux deja utilises
par SignalEngine. Source officielle verifiee pendant le diagnostic :
[subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions).

## Corrections

- Les deux modes souscrivent bbo et l2Book fast=true. Les prix sont ordonnes par
  timestamp exchange ; BBO gagne une egalite. La profondeur a sa propre version.
  Un L2 plus ancien ne remplace jamais un prix BBO plus recent.
- Les callbacks des anciennes connexions sont ignores. Une reconnexion invalide
  la certitude de position et la generation d'execution jusqu'au controle frais.
  Un L2 frais confirme la sante du flux lorsque le BBO ne change pas.
- MID conserve mid +/- son decalage ; BBO conserve le meilleur bid/ask exact.
  Le prix est revalide juste avant signature dans les deux modes.
- La transition d'un ordre d'ouverture appartenant a la session vers une sortie
  reduce-only utilise batchModify. Le precontrole REST verifie encore position,
  direction, tailles reservees et ancien OID remplace. Un flag RO inconnu n'est
  jamais devine ; RO -> ouverture conserve le chemin annulation/placement.
- Les sorties RO deja presentes peuvent etre modifiees dans les deux modes.
  Une deferral connue avant envoi n'autorise plus un fallback cancel/place.
  Les reponses inconnues ne sont pas rejouees.
- Le regroupement ordinaire de 80 ms est conserve ; la boucle se reveille a
  son echeance plutot que d'attendre 250 ms de plus. Les fills et retraits de
  protection peuvent agir immediatement. Une rafale conserve la derniere cible.
- Le worker REST borne et le writer de statistiques sont etendus a BBO : session
  HTTP separee, une lecture active, une demande fusionnee, un resultat immuable,
  application uniquement par le thread de trading avec ses gardes de revision.
  Cadences de verification : position 5 s et ordres 10 s. La grace post-quote
  reste pour les suspicions, mais ne repousse plus indefiniment la verification
  periodique pendant les recotations. Les memes gardes rendent les reponses
  depassees inapplicables. Une verification perimee bloque les ouvertures.
- Montants, limites, symboles DEX, nonce partage, post-only, backoffs de rejet,
  journaux durables et exceptions de fermeture manuelle/urgence sont conserves.

## Metriques compatibles

latency.metric_version=2 distingue la nouvelle semantique. Tous les champs
existants restent disponibles et chaque timing ajoute p90 a p50/p95/p99.
event_queue_delay et event_to_decision echantillonnent la premiere decision
pour chaque version effectivement consommee, sans recompter les timers.
coalesced_book_events compte les versions depassees ; les versions eliminees
ne sont pas presentees comme des decisions executees.

book_age_at_decision et book_age_at_submit restent des mesures de fraicheur.
event_to_submit et event_to_ack utilisent le declencheur de la decision et
incluent l'attente de coalescence lorsqu'aucun nouvel evenement ne le remplace.
Un ACK termine a la reponse SDK, sans signifier acceptation ou fill.
fill_to_decision mesure la premiere reaction locale au fill.

submission_prepare mesure la revalidation finale ; sdk_prepare_sign mesure
la preparation du SDK et sa signature jusqu'a l'appel HTTP ; exchange_http
mesure requete HTTP + attente serveur + decodage. Les exceptions HTTP sont
separees. sdk_to_response reste l'agregat compatible. Le wrapper est limite
a l'instance SDK du processus et ne modifie ni payload ni signature.
latency.feed ajoute source, age local du carnet et generation. L'API
/api/grid/status transmet ces champs sans changement obligatoire du frontend.

## Replays avant/apres

Donnees : [fast-before.json](fast-before.json) et [fast-after.json](fast-after.json).
Le meme scenario est rejoue 500 fois par mode : une position short connue et
l'ordre buy d'ouverture oppose doit devenir sa sortie RO, avec controles frais.
Chaque lecture REST et chaque reponse d'execution dorment artificiellement 1 ms.
Pas de constructeur, HTTP, signature, carnet exchange, matching, frais ou PnL.
Les queues mesurees incluent la charge et l'ordonnanceur Windows non controles.

| Scenario | Requetes avant/apres | Lectures compte avant/apres | p90 cycle avant/apres ms | p95 cycle avant/apres ms | p99 cycle avant/apres ms |
|---|---|---|---|---|---|
| MID conversion | 1000 / 500 | 1000 / 1000 | 7.0751 / 5.2386 | 7.3890 / 5.4786 | 8.2622 / 6.2352 |
| BBO conversion | 1000 / 500 | 1500 / 1000 | 8.6451 / 5.2962 | 8.9110 / 5.5966 | 11.3848 / 6.3216 |

Les ordres inchanges envoient toujours zero requete. Le controle 100 reveils / un
evenement passe de 100 echantillons repetes a un seul. La reduction de requetes
est structurelle ; les differences de percentiles sont des observations locales.

Couts negatifs mesures : sans ordre, p95 MID 0.0873 -> 0.0942 ms et p95 BBO
0.0604 -> 0.0717 ms. L'instrumentation et la decision avant le gate de coalescence
ajoutent du travail. Les flux rapides augmentent aussi les callbacks et peuvent
augmenter les modifications utiles ; aucune baisse globale d'actions ou aucun
gain de file/queue n'est promis pour un marche different.

Le verrou de signature couvre toujours la requete complete par agent : plusieurs
bots peuvent attendre la reponse du precedent. Les precontroles REST restent
synchrones. Le delai reseau/exchange n'a pas ete accelere ou contourne ; son
p95 devra etre mesure apres un prochain lancement manuel. Aucune promesse HFT
ou de rentabilite n'est deduite des replays.

## Verification et reproduction

Tests nouveaux : ordre des sources, tie BBO/L2, autre DEX, donnees invalides,
ancienne connexion, quiet feed, REST depasse, echantillons sans doublons,
echeance 80 ms, conservation de cette attente dans event_to_submit, conversion
RO bornee au fill partiel, Stop pendant une requete, rafale de 500 messages
pendant un ACK lent, absence de fallback destructeur et timing SDK/HTTP.
Les memes tests sont executes pour MID et BBO ; les tests de maintenance
fill/modify/cancel/timeout/Stop sont aussi rejoues pour BBO.

```powershell
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s integrations/freqtrade/tests -p 'test_*.py'
python tests/benchmark_fast_mm.py --source .zeus-state/mm-latency-before-20261106/order_book.py --cycles 500 --output artifacts/mid-audit/fast-before.json
python tests/benchmark_fast_mm.py --cycles 500 --output artifacts/mid-audit/fast-after.json
git diff --check
```

Le snapshot avant est une sauvegarde locale ignoree par Git, avec manifeste des
empreintes. Les JSON avant/apres identifient leur source order_book.py par SHA256.
La syntaxe Python 3.9 est verifiee ; l'execution des tests utilise Python 3.12.
Les modifications restent locales ; aucun processus deja lance n'est recharge.

Validation finale : **254 tests Python reussis**, dont **52 tests du nouveau
fichier fast_mm_latency**, et **19 tests adaptateur Freqtrade reussis**.
py_compile, verification syntaxique 3.9 et git diff --check sont passes.
L'empreinte du code final correspond a fast-after.json :
`f4961ba6bf94401457d1f5ddb7e04962b4995a156bdbad6cc8e9d1bf2a23dd7b`.

Au dernier controle API, io:ANTH etait `stopped`, sans erreur, avec les
anciennes metriques (metric_version absent). Aucune relance n'a ete effectuee.
