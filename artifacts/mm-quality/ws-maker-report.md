# MID/BBO Python : execution WebSocket et automatique maker uniquement

Date : 7 novembre 2026. Cette passe concerne `zeus/bots/order_book.py`, moteur
Python des modes MID et BBO. Le moteur Go Freqtrade reste distinct. Les autres
modifications preexistantes du workspace sont conservees.

## Comportement livre

- Toute cotation ou sortie automatique est une limite `Alo` post-only. Les
  branches perte, take-profit, age de position, runaway, flux coupe, erreur,
  perte du superviseur et arret du serveur n'autorisent plus une liquidation
  taker. Les prix d'ouverture MID gardent leur decalage configure ; BBO garde
  exactement le meilleur bid et le meilleur ask, sous ses limites de risque.
- Le Stop manuel peut annuler les ordres de la session puis envoyer une cloture
  IOC reduce-only apres lecture independante de l'inventaire. Cette autorisation
  est etroite : demande manuelle pour l'identifiant exact de la session, ou
  Ctrl+C dans son terminal interactif. SIGTERM, SIGINT headless, un ancien fichier
  texte `stop` et l'arret automatique du serveur ne donnent pas ce droit.
- Trois controles protegent cette politique : facade de securite avant journal
  et CLOID ; controle avant signature ; controle de la charge signee juste avant
  envoi. Le dernier controle ne modifie jamais ce qui a deja ete signe.
- Un ordre deja rempli/annule ou un rejet post-only attendu est reconcilie par
  les chemins existants. Les autres ordres acceptes d'une reponse mixte restent
  suivis. Une reponse malformee ou un resultat inconnu ne devient jamais une
  permission de renvoyer la meme exposition.

## Chemin critique et concurrence

`zeus/ws_execution.py` maintient une connexion d'execution distincte du flux de
marche. Le SDK construit et signe l'action ; seul son transport `/exchange`
est remplace. Les lectures restent HTTP. L'enveloppe et la reponse suivent les
[post requests officiels Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/post-requests),
verifies le 7 novembre 2026. Il n'y a aucun fallback HTTP d'une action WS.

Le socket est prepare avant le reglage du levier et ferme apres nettoyage et
comptabilite. Identifiants de requete, generation de connexion et ACK sont
correles. Une seule action est en vol. Un echec certain avant envoi est ignore
comme requete non soumise ; un timeout, envoi partiel ou ACK invalide apres debut
d'envoi conserve l'incertitude et requiert reconciliation. Aucun rejeu aveugle.
Reconnexion avec backoff, heartbeat et fermeture bornee sont testes hors ligne.

`zeus/action_worker.py` execute les appels SDK sur un thread serie : un callback
actif et un seul reveil en attente, qui recalcule la cible a son execution. Une
rafale de 1 000 reveils ne cree pas 1 000 requetes. La boucle principale continue
les controles de risque et Stop pendant l'attente d'un ACK. Les lectures REST
periodiques restent dans le worker de maintenance deja livre, avec revisions et
boite bornee. La recuperation du flux utilise aussi son propre client en lecture
seule et ne bloque plus la boucle principale.

Les controles frais avant soumission restent synchrones. Apres signature, le
carnet, sa generation, son age, la revision d'inventaire et le risque sont
reverifies, pour les sorties reduce-only comme pour les ouvertures. Les ouvertures
doivent aussi respecter les verifications periodiques et le plafond courant.

Lors de Stop, les travaux en attente sont retires. Le callback actif doit etre
termine avant de reutiliser son client HTTP pour le nettoyage. Son drainage
consomme le budget de nettoyage de 30 secondes. S'il reste bloque, aucune lecture
de nettoyage concurrente ni nouvelle cloture n'est envoyee : arret non confirme.

## Inventaire et resultat

`zeus/inventory.py` calcule l'entree moyenne a chaque ajout ; conserve l'entree
sur une reduction partielle ; la remet a zero a plat ; initialise l'entree du
reliquat sur un retournement. Les champs de position precedente (age, peak,
fenetre de sortie et raison de risque) sont reinitialises au bon moment.

Le risque valorise la quantite courante au meilleur bid pour un long et au
meilleur ask pour un short. Ce mark n'inclut ni frais, ni profondeur au-dela du
premier niveau, ni funding. Il ne reutilise plus un uPnL REST ancien avec une
quantite modifiee par un fill. Un `startPosition` incompatible avec l'etat local
exige une resynchronisation ; il n'applique pas le fill deux fois. Un prix
d'entree inconnu ou un carnet perime n'est jamais transforme en perte nulle.

Le calcul du risque et les mises a jour des fills partagent leur verrou d'etat.
La maintenance ne peut ecraser une preverification en cours ou un fill plus
recent. Les callbacks sans requete ne font plus avancer artificiellement la
revision, pour ne pas affamer la maintenance sur un carnet actif.

## Mesures et preuves

Les chiffres des [replays MID precedents](../mid-audit/report.md) et de la
[passe de latence precedente](../mid-audit/fast-report.md) sont historiques,
associes a leurs empreintes de source. Ils ne mesurent pas ce nouveau transport
WS et ne sont pas presentes comme des gains live de cette passe.

| Verification de cette passe | Resultat |
|---|---|
| Suite Python complete | 413 tests OK en 95.864 s ; voir `validation.json` et `test-python.log` |
| Compatibilite adaptateur Freqtrade + sorties maker | 19 tests OK |
| Navigateur, toutes requetes et sockets mocks | Clavier, payload MID, carte immuable, badge MAKER / WS, BBO, largeurs 1440/390 : OK |
| Compilation frontend | `npm.cmd run build` : OK |
| Syntaxe Python 3.9 + bytecode local | 7 fichiers coeur : OK |
| Diff whitespace | `git diff --check` : OK (avertissements CRLF preexistants) |
| Connexion WS publique, sans action signee | Ready puis fermeture OK ; 0 action signee |

Les tests couvrent : fills partiels, ouverture/fermeture/retournement, REST deja
en avance sur un fill, inconnus conserves, ACK retardes, reponses mixtes,
deconnexions, payload signe immuable, course fill/Stop/carnet avant envoi,
requetes de maintenance regroupees, ordres personnels exclus, nettoyage manuel
et automatique, preverification bloquee et drainage avant nettoyage.

Le champ `latency` reste compatible et contient p50/p90/p95/p99 des echantillons
bornes : decision, file d'evenements, evenement/fill vers decision, evenement vers
envoi/ACK, age du carnet, verrous API/signature, preparation SDK, WS et maintenance.
Il expose aussi l'etat des workers et du transport. Les lectures de securite et
l'ordonnanceur Windows restent inclus dans les chemins concernes ; une connexion
WS ne supprime ni la latence reseau ni le temps de traitement de l'exchange.

**Indisponibles pour la nouvelle version :** percentiles d'execution live,
proportion maker/taker live, frais live et comparaison de PnL reel. La seule
connexion publique verifie le handshake, pas un placement. Aucun chiffre de
rentabilite ou de latence live n'a ete invente. Les tests avec ACK bloque prouvent
la reactivite des controles ; ils ne mesurent pas une file d'attente exchange.

## Limites operationnelles

Maker uniquement signifie qu'une sortie peut rester non executee et que la
position peut continuer a perdre. Sur une erreur fatale ou un flux inconnu,
le nettoyage automatique annule les ordres propres mais ne liquide pas la
position au marche ; un reliquat reste explicitement non confirme. Le mode MID
protege facultativement ses sorties pendant sa duree configuree ; il n'offre
aucune garantie de gain et ne change pas les ouvertures en BBO.

Si un worker est deja termine avec un reliquat, il ne peut plus consommer un
nouveau fichier Stop. La carte actuelle n'offre pas de cloture de secours pour
ce processus mort : fermeture manuelle sur l'exchange et reconciliation peuvent
etre necessaires avant un nouveau depart sur le meme marche.

Le verrou interprocessus Python du meme agent couvre encore toute l'attente
d'ACK ; plusieurs marches du meme agent peuvent attendre ce verrou. Les signers
Go ou externes ne partagent pas ce verrou Python. Aucune garantie de coordination
universelle des nonces n'est annoncee.

Les montants, plafonds, anti-spam et noms qualifies par DEX sont preserves. Aucun
ordre reel, lancement de strategie, redemarrage serveur/VPS, deploiement ou push
n'a ete effectue. Les processus deja lances gardent leur ancien code ; le nouveau
contrat de Stop JSON et la politique doivent etre charges par un serveur mis a
jour et une nouvelle session, sans redemarrage automatique par cet audit.

## Reproduction hors ligne

Depuis la racine du depot :

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s integrations/freqtrade/tests -p "test_*.py"
python tests/check_mid_exit_ui.py
python -m py_compile zeus/bots/order_book.py zeus/server.py zeus/safety.py zeus/signing.py zeus/ws_execution.py zeus/inventory.py zeus/action_worker.py
git diff --check
cd frontend
npm.cmd run build
```

Les tests de navigateur interceptent tous les appels reseau. Aucun compte reel
ni secret n'est necessaire. `validation.json` conserve les empreintes de source
verifiees ; `test-python.log` conserve la sortie complete de la suite.
