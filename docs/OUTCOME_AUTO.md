# Outcome Auto : sélection par flux taker

Dans **Outcome → Selection → Auto**, choisir le côté,
la taille d'ordre et le spread maximal du carnet en bps. L'exécution passe en
**Maker / ALO**, au meilleur bid ou ask, sans amélioration automatique du spread.

Le scanner est public et ne signe aucun ordre. Il commence à l'ouverture du mode
Auto. Attendre au moins 60 secondes et la confirmation de tous les abonnements
(environ 80 secondes pour 200 marchés, hors connexion). **Start Auto** peut être
cliqué pendant cette attente : il arme un seul lancement, avec les paramètres
figés, dès qu'un marché satisfait les critères. **Cancel Auto** ou **Stop all**
annule cette attente. Fermer ou recharger la page l'annule également ; la demande
n'est pas persistée dans le navigateur. Aucun ordre n'est envoyé pendant la
recherche. Le marché est revérifié au lancement. Une fois le bot lancé, la
rotation est pilotée par le serveur et continue sans dépendre de l'onglet.

## Bid + Ask et rotation

Le mode Maker lancé depuis l'interface maintient un bid et un ask simultanément,
si les USDC et les tokens disponibles le permettent. La taille est **par ordre**,
le plafond de position reste commun et ne compte pas une vente attendue comme
une réduction déjà exécutée. Aucun token détenu n'est vendu deux fois. À zéro
inventaire, seul le bid est possible au départ ; l'ask apparaît après acquisition
de tokens. Les arrondis de quantité respectent le minimum de la venue.

Auto mesure le volume réellement exécuté pendant trois minutes. Par défaut,
le seuil est un montant d'ordre exécuté par minute : avec un ordre de 10 USD,
moins de 10 USD/min sur cette fenêtre rend le marché candidat à une rotation.
Ce seuil est distinct du volume public du scanner.

Quand le débit est insuffisant, le bot choisit le premier autre marché admissible
du classement en dollars/minute. Le marché décevant est écarté dix minutes pour
éviter de revenir immédiatement sur la même file, même si son volume public
reste élevé. En l'absence d'alternative admissible, il conserve le marché courant.

La transition annule les achats, maintient des ventes post-only pour l'inventaire,
puis vérifie l'absence d'ordres et de tokens. Le serveur attend ensuite la fin du
worker et la confirmation du nettoyage avant de sélectionner à nouveau et de
lancer le remplaçant. Il ne liquide pas au marché pour forcer une rotation.
Une position qui ne se revend pas retarde donc le changement de marché.

Le côté, le montant d'ordre et le plafond restent ceux de la session démarrée.
Les pertes réalisées sont reportées dans le budget de risque du remplaçant ;
un arrêt de risque ou un nettoyage non confirmé bloque la rotation. **Stop**
annule toute la chaîne, même depuis une ancienne carte, et empêche un remplaçant
de démarrer après la demande. Aucune chaîne n'est reprise automatiquement après
un redémarrage du serveur.

## Classement

- Tous les marchés du catalogue `out`, deux côtés observés séparément.
- Historique borné à cinq minutes ; volume et quantités directionnels sur une minute.
- Déduplication des transactions et regroupement des passages à la seconde :
  dix fills au même instant ne sont pas dix passages récurrents indépendants.
- Au moins six secondes distinctes de transactions sur cinq minutes, une plage
  d'au moins 30 secondes, trois tranches actives de dix secondes sur la dernière
  minute, et deux secondes distinctes d'activité dans chaque sens.
- Dernière transaction datant de 20 secondes au maximum ; intervalle médian
  entre passages de 30 secondes au maximum.
- Classement principal par volume exécuté public sur une minute, en dollars.
  Le flux équilibré, sa couverture, sa fraîcheur, le spread et la file servent
  de filtres ou de départage. Jusqu'à 24 carnets candidats sont vérifiés par
  lecture ; le résultat indique combien ont été examinés.
- Carnet récent de cinq secondes au maximum, profondeur au meilleur prix de
  chaque côté au moins égale à la taille d'ordre ; spread maximal 10 bps par défaut.
  Cette limite continue à bloquer les nouvelles entrées si le spread s'élargit
  après lancement, sans bloquer la revente de l'inventaire.
- Exclusion des événements commencés ou à moins de 30 minutes de leur date connue.
  Les enfants sportifs nécessitent la date du parent. Ce filtre ne détecte pas
  toutes les annonces imprévues.
- Les marchés ayant déjà une session active ou non réconciliée sont écartés.

La « queue proxy » est la quantité affichée au meilleur prix plus notre taille,
divisée par le débit opposé observé. Elle ne prédit pas le temps de fill : les
annulations, nouveaux ordres, changements de prix et doubles représentations
du carnet peuvent modifier la file. Les deux tokens complémentaires partagent
leur liquidité ; les profondeurs et flux des deux représentations ne sont pas
additionnés pour gonfler le score.

Le classement optimise une **opportunité de rotation**, sans promettre de fills,
de profit, de frais nuls ou de volume officiellement crédité. Les périodes sans
marché admissible sont normales. Un déséquilibre du flux ou une disparition de
liquidité après lancement reste un risque de la session. La rotation réévalue
les possibilités ; elle ne garantit pas une exécution ni un coût inférieur.

## Précision

Les prix Outcome suivent désormais cinq chiffres significatifs, avec huit
décimales au maximum, au lieu d'une grille fixe de quatre décimales. Exemples
préservés : `0.34833`, `0.78717`, `0.013457`. Les achats passifs sont arrondis vers
le bas et les ventes passives vers le haut ; les prix restent strictement entre
zéro et un. Les conversions destinées aux IOC conservent leur arrondi agressif.

## Validation

```powershell
python -m unittest discover -s tests -p 'test_outcome*.py' -q
python tests/check_outcome_ui.py
node --check frontend/app.js
```

Les tests utilisent des flux, carnets, comptes et workers factices. Le contrôle
public séparé de connectivité ne lance aucun bot et n'envoie aucun ordre.
