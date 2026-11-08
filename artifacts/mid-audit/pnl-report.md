# Correction PnL MID et fiabilite du bilan, 6 novembre 2026

Cette passe corrige des causes observees de pertes et de bilan incomplet. Elle
ne prouve pas la rentabilite future du market maker. BBO conserve ses prix
stricts au meilleur bid/ask ; montants, plafonds et symboles DEX sont conserves.

## Constat verifie en lecture seule

La derniere session io:ANTH avait echoue sur le rejet exact :
`Attempted to modify to invalid new order`.
Le nettoyage avait ensuite cloture le short par un ordre IOC, en deux fills.
Les fills de cet ordre n'etaient pas inclus dans le dernier bilan du dashboard.

Verification par userFills, historicalOrders et les CLOIDs de la session,
sans publier l'adresse du compte ni les identifiants d'ordres dans ce rapport :

| Derniere session seulement | Valeur |
|---|---:|
| Fills | 8 |
| Fills maker / taker | 6 / 2 |
| Volume execute | 401.3593 USD |
| Frais | 0.017334 USD |
| PnL realise brut | -0.0295 USD |
| PnL realise apres frais | **-0.046834 USD** |

Le dashboard affichait environ -0.026 USD et 5 fills : le resultat etait
incomplet, pas meilleur. Les deux fills taker appartiennent a un seul ordre
de fermeture. Ce controle n'est pas un calcul du PnL de toutes les sessions.
Les fichiers d'une ancienne session arretee n'ont pas ete reecrits.

Autre cause : une sortie MID suivait uniquement le nouveau mid. Un short a
1700.7 pouvait donc etre rachete a 1700.9. La latence reduite de la passe
precedente ne suffit pas a corriger cette selection ou ce prix de sortie.

## Execution corrigee

Seul le rejet exact ci-dessus, sur une modification ALO avec une reponse
complete, est ajoute aux cas reconciliables. Le moteur relit position et ordres
reels avant de continuer. Il conserve les autres ordres acceptes d'un batch.

Si l'ancien OID reste ouvert, la prochaine decision peut l'annuler puis placer
un nouvel ordre maker recalcule, apres confirmation d'annulation et controle
frais. Aucun nouvel envoi n'a lieu dans le callback de reconciliation et la
requete de modification refusee n'est pas renvoyee aveuglement. Si l'ordre
a deja disparu, la decision repart de l'inventaire et des ordres observes.

Une reponse ambigue, une liste de statuts incomplete, un autre rejet ou une
verification indisponible conservent l'interlock de securite. Cette correction
ne generalise pas l'ignorance des erreurs et ne retire pas le Stop manuel.

## Protection MID limitee

L'option **Protect MID exits for 30 seconds** est active par defaut pour les
nouveaux lancements MID. Elle est visible et desactivable dans le formulaire,
transmise au worker et conservee dans la carte de la session. Modifier le
formulaire ne change jamais les parametres d'une session deja lancee.

Pendant les 30 premieres secondes de la position, la sortie reduce-only prend
le plus protecteur du prix MID normal et du seuil suivant, arrondi au tick :

- Long : prix de vente couvrant l'entree, les frais d'entree estimes, les frais
  maker de sortie estimes et un tick de marge.
- Short : prix de rachat plafonne pour couvrir les memes couts et un tick.

Les estimations utilisent les maxima de frais positifs observes dans les fills
du meme marche pendant la session. Les rebates ne sont pas supposes. Si les
frais sont inconnus, le moteur conserve le prix MID normal et ne pretend pas
avoir etabli un seuil net. Les parametres HIP-3 peuvent modifier les frais :
la grille generique userFees seule n'a donc pas ete appliquee comme taux exact.
[Regles officielles de frais](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees).

Apres 30 secondes, le moteur revient au prix MID normal. Une sortie de risque
existante est prioritaire sur le seuil de profit. Les cotations ordinaires
restent ALO ; cette option n'autorise jamais un IOC. Les exceptions manuelles,
de nettoyage et d'urgence deja explicites sont conservees.

Cette protection peut retarder un fill et augmenter la perte d'inventaire avant
son expiration. Elle ne filtre pas les entrees toxiques et ne garantit ni fill
ni PnL positif. Les frais estimes peuvent changer et le funding n'est pas inclus
dans ce seuil. Aucun gain financier contrefactuel n'a ete fabrique depuis la
capture ou un modele de matching sans historique de queue.

Interfaces : mid_protect_exits (bool, defaut true) et mid_exit_protect_sec
(1 a 120 s, defaut 30) sur la requete MID. CLI : --protect-mid-exits,
--strict-mid-exits et --mid-exit-protect-sec. BBO ignore cette protection.
Les ouvertures MID gardent exactement leur decalage configure.

## Bilan final corrige

- Les callbacks du flux courant continuent de recevoir les fills pendant le
  nettoyage, tout en maintenant le blocage des nouvelles cotations.
- Les OIDs des ACK filled sont attribues a la session, comme ceux des ACK resting.
- Une verification finale en lecture seule reconstitue volume, frais, PnL
  realise et roles maker/taker depuis les seuls ordres de la session. Elle est
  limitee a trois secondes et au temps restant du budget de nettoyage de 30 s.
- Les quantites de fermeture accusees par l'exchange et celles deja recues par
  WebSocket doivent apparaitre dans l'historique. Une reponse tronquee, invalide
  ou en retard rend la comptabilite explicitement non confirmee.
- Les identites des fills reconcilies sont dedupliquees avant d'accepter leurs
  eventuelles arrivees WebSocket tardives. Pas de double comptage.
- Seule une verification independante de nettoyage autorise le bilan de
  position finale nulle. Une erreur comptable ne fabrique pas un etat flat.
- L'API masque le PnL net et le cout/M si le bilan est incomplet, affiche la
  raison, fige la duree finale et ne presente plus une ancienne position comme
  encore ouverte apres un nettoyage confirme.

Le PnL net ici est le PnL realise moins les frais d'execution. Le funding reste
exclu, comme dans l'interface existante.

## Verification

Tests specifiques : refus de modification avec ordre encore ouvert, ordre
accepte dans une reponse mixte, verification indisponible, autre erreur non
masquee, nouveau placement uniquement apres annulation confirmee ; calcul des
prix long/short apres frais, prix MID deja meilleur, expiration, priorite du
risque, BBO et ouvertures inchanges ; fills IOC de nettoyage, historique
incomplet, timeout, budget expire, doublons tardifs, OID rempli, bilan flat,
duree figee, option transmise au processus et persistee.

Le navigateur est teste avec toutes les requetes reseau et WebSockets simules,
a 1440 et 390 px : checkbox native au clavier, payload, carte independante du
formulaire, absence de debordement horizontal. Captures pnl-ui-1440.png et
pnl-ui-390.png. Le controle Freqtrade/BBO/MID existant passe aussi.

```powershell
python -m unittest discover -s tests -p 'test_*.py'
python -m unittest discover -s integrations/freqtrade/tests -p 'test_*.py'
python tests/check_mid_exit_ui.py
python integrations/freqtrade/tests/check_ui.py
npm.cmd --prefix frontend run build
git diff --check
```

Les modifications sont locales. Aucun ordre de test, bot ou redemarrage serveur
n'a ete lance pour les appliquer. Le serveur deja charge doit etre recharge
pour prendre en compte la nouvelle configuration API et toutes les statistiques.
Les sessions deja lancees ne changent pas automatiquement de politique.

Validation finale : **278 tests Python et 19 tests adaptateur Freqtrade passent**.
Les **24 tests PnL/protection/reconciliation** font partie de la suite Python.
Controles navigateur desktop/mobile et clavier, compilation frontend,
node --check, py_compile, syntaxe Python 3.9 et git diff --check passes.
Les tests s'executent sous Python 3.12 ; aucun test d'execution Python 3.9
n'est revendique. Le snapshot avant correction est conserve dans
.zeus-state/pnl-before-20261106 ; son order_book.py correspond au SHA256
de la passe fast-after.json precedente.
