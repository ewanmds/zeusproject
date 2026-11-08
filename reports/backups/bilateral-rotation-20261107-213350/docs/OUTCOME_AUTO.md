# Outcome Auto : sélection par flux taker

Dans **Outcome → Market selection → Auto · recent taker flow**, choisir le côté,
la taille d'ordre et le spread maximal du carnet en bps. L'exécution passe en
**Maker / ALO**, au meilleur bid ou ask, sans amélioration automatique du spread.

Le scanner est public et ne signe aucun ordre. Il commence à l'ouverture du mode
Auto. Attendre au moins 60 secondes et la confirmation de tous les abonnements
(environ 80 secondes pour 200 marchés, hors connexion). **Start Auto** peut être
cliqué pendant cette attente : il arme un seul lancement, avec les paramètres
figés, dès qu'un marché satisfait les critères. **Cancel Auto** ou **Stop all**
annule cette attente. Fermer ou recharger la page l'annule également ; la demande
n'est pas persistée dans le navigateur. Aucun ordre n'est envoyé pendant la
recherche. Le marché choisi est revérifié au lancement et fixé pour cette
session ; il n'y a pas de migration d'une position ouverte vers un autre marché.

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
- Score fondé sur le flux équilibré achat/vente, la couverture temporelle et la
  fraîcheur, puis pénalisé par le spread et une estimation de la file à consommer.
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
liquidité après lancement reste un risque de la session : ce mode sélectionne le
marché au démarrage, sans modifier les protections d'exécution existantes.

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
