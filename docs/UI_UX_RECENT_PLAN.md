# Plan UI/UX Zeus à partir de Recent

Statut : plan de conception, sans modification du code applicatif ni déploiement.
Les changements locaux du nettoyage précédent restent conservés.

## Objectif et limites

Permettre à un utilisateur de connecter son wallet, préparer une session et suivre
son exécution avec moins de défilement et une lecture plus directe. Le parcours
reste fondé sur les données réelles déjà fournies par le workspace.

Conserver le fond sombre, l'or, le violet, Exo 2, le monogramme Orbitron et les
chiffres JetBrains Mono. Aucune image décorative à réintroduire. Conserver les
réglages de trading, les confirmations, l'identité par wallet et les commandes
Start/Stop existantes. Ce travail ne change pas la stratégie ou le volume exécuté.

## Ressources examinées

Le [répertoire Skills de Recent](https://recent.design/skills) recense les
ressources suivantes, dont les principes ont été lus dans leurs sources :

- [Distill](https://github.com/pbakaus/impeccable/blob/main/skill/reference/distill.md) :
  retirer les répétitions, hiérarchiser l'action principale et révéler les options
  secondaires au bon moment.
- [Quieter](https://github.com/pbakaus/impeccable/blob/main/skill/reference/quieter.md) :
  réduire l'intensité des effets d'une interface d'outil tout en gardant son identité.
- [Polish](https://github.com/pbakaus/impeccable/blob/main/skill/reference/polish.md) :
  traiter les états, la cohérence et les parcours complets, pas seulement une capture.
- [Make Interfaces Feel Better](https://github.com/jakubkrehel/make-interfaces-feel-better) :
  alignement, surfaces, chiffres stables et interactions discrètes.
- [Web Interface Guidelines](https://github.com/vercel-labs/web-interface-guidelines/blob/main/command.md) :
  contraste, focus, formulaires, accessibilité et comportement des états asynchrones.

Références de composants observées dans la galerie Interface :

| Référence | Adaptation prévue pour Zeus |
| --- | --- |
| [Fold Toolbar](https://recent.design/i/6jgiv46-fold-toolbar-interaction) | Options avancées compactes, ouvertes dans le contexte de la configuration. |
| [Vael Fintech App](https://recent.design/i/vwi94zh-vael-fintech-app) | Hiérarchie du montant, du compte et de la confirmation. Zeus conserve un bouton explicite, sans geste de confirmation obligatoire. |
| [Stock Ticker Card](https://recent.design/i/1sr9bt9-stock-ticker-card-interaction) | Synthèse de session compacte, puis accès à ses détails réels. Pas de graphique de cours inventé. |

Ces références servent de principes et de modèles d'interaction. Aucun bundle
externe, hook d'agent ou nouvel outil payant n'a été installé pour cette étude.

## Constats sur Zeus

Observation de la page publique à 1280 × 720 et 390 × 844, puis lecture des vues
React, des styles et des contrats de données. Les états connectés ont été relus
dans le code ; aucun wallet réel n'a été connecté pour cet audit.

1. Le bouton principal du formulaire et son récapitulatif se trouvent sous le
   premier écran aux deux dimensions observées.
2. Les grandes cartes de stratégie et les répétitions du récapitulatif consomment
   une part importante de la hauteur avant l'action.
3. Plusieurs informations secondaires utilisent 8 à 11 px. Le gris `#66748c`
   sur `#0f172a` donne environ 3,78:1 : insuffisant pour du texte courant.
4. La couleur violette de base demande un ajustement de sa variante de fond pour
   garantir le contraste du texte des petits boutons.
5. Les cartes de session empilent plusieurs zones. Une lecture alignée faciliterait
   la comparaison de plusieurs bots et l'identification de celui qui demande une action.
6. Les erreurs sont regroupées dans un bandeau global ; elles doivent aussi
   apparaître près de l'action concernée, avec une prochaine étape claire.

## 1. Lancer : raccourcir le parcours visible

- Remplacer les grandes cartes desktop par un sélecteur de stratégie compact.
  Garder les lignes lisibles sur mobile, avec une seule description courte.
- Afficher d'abord la stratégie, le montant et les champs nécessaires au mode
  choisi. Conserver les autres réglages dans « Options avancées », avec un résumé
  de leurs valeurs, puis tous les paramètres effectifs dans la revue finale.
- Réduire le panneau de droite au compte, au montant, au marché/mode et à l'action.
  Enlever les intitulés et explications répétés ailleurs.
- Garder un seul bouton principal selon l'étape : connecter, autoriser, vérifier
  puis confirmer le lancement. Les contrôles wallet de l'en-tête restent secondaires.
- Sur téléphone, placer l'action et le montant dans une barre fixe au-dessus de
  la navigation. Réserver son espace et gérer le clavier pour ne masquer ni champ
  ni focus. Le récapitulatif complet reste accessible à la demande.

## 2. Scanner et sessions : mettre l'exécution au premier plan

- Scanner : liste alignée avec marché, débit observé en $/min, spread et file
  estimée. Distinguer le meilleur candidat du marché effectivement utilisé par
  une session. Une estimation n'est pas une promesse de fill.
- Sessions desktop : lignes/cartes plus basses, colonnes de chiffres alignées,
  état, marché et côté clairement visibles ; détails ouverts à la demande.
- Sessions mobile : résumé avec les mêmes informations essentielles, puis
  développement progressif du détail.
- Mettre en avant volume, fills et coût/1M ; conserver le net réalisé, les frais,
  la position et les paramètres effectifs dans la hiérarchie appropriée.
- À l'arrivée sans URL explicite, proposer les sessions actives lorsque leur
  présence est confirmée. Ne jamais déplacer automatiquement l'utilisateur au
  milieu d'une saisie ni changer sa sélection explicite de page.
- Conserver les identifiants et composants stables pendant les mises à jour.
  Aucun défilement forcé, remontage de carte ou animation des chiffres en continu.

## 3. Wallet et états : rendre chaque prochaine étape évidente

- Séparer clairement identité connectée, agent autorisé et session lancée.
- Un statut et une action utiles par étape, avec les erreurs près du contrôle
  concerné. Réserver le bandeau global aux problèmes qui touchent tout le workspace.
- Distinguer « en cours », « confirmé », « à vérifier » et « flux retardé ».
  Une commande non confirmée conserve son identifiant ; elle n'est pas renvoyée
  pour obtenir artificiellement une réponse de succès.
- Conserver « Vérifier l'arrêt » lorsque la fermeture n'est pas confirmée.
- Les valeurs absentes restent neutres et affichées comme inconnues ; ne pas
  utiliser le vert ou zéro pour donner l'impression d'un résultat connu.

## 4. Performances : hiérarchie cohérente et données vérifiables

- Utiliser les mêmes libellés et unités dans le résumé, les cartes et les détails.
- Donner la priorité au volume exécuté, au coût/1M, aux frais et au net réalisé.
- Conserver les filtres de période et le graphique par session, qui correspond
  aux données disponibles. Ne pas ajouter une courbe intrajournalière ou un flux
  de trades si les événements nécessaires ne sont pas fournis par l'API.
- Rendre les montants et résultats consultables dans une représentation textuelle
  ou tabulaire, en complément du graphique.

## 5. Finition commune

- Hiérarchie typographique courte : texte utile autour de 14–16 px, informations
  secondaires lisibles autour de 12–13 px ; microtexte réservé au décor.
- Contraste d'au moins 4,5:1 pour le texte courant, y compris les boutons et états
  sélectionnés. Garder la famille violette en adaptant les variantes sémantiques.
- Or pour l'identité et les repères ; violet pour l'action et la sélection ; rouge
  et vert pour les états qui le justifient réellement.
- Adoucir les bordures et halos secondaires. Utiliser l'espacement et l'alignement
  pour structurer, plutôt que multiplier les cartes imbriquées.
- Transitions courtes pour l'ouverture d'un détail ou la confirmation d'une action.
  Conserver les mises à jour financières stables et respecter la réduction des animations.
- Contrôles tactiles d'au moins 44 px, focus visible, labels explicites et aucune
  action masquée par les barres fixes.

## Ordre d'implémentation et fichiers

1. Revoir les tokens de contraste, l'échelle typographique et les composants
   communs (`styles.css`, `brand.css`, `ui.tsx`).
2. Réorganiser `RunView.tsx` et son action mobile, en gardant les mêmes champs et
   commandes de revue/lancement dans `App.tsx`.
3. Compacter `SessionsView.tsx`, préciser les états wallet dans `AccountView.tsx`
   et harmoniser `PerformanceView.tsx`.
4. Vérifier les états asynchrones, la navigation initiale, le focus et les transitions.
5. Exécuter le build strict, les tests existants et les parcours visuels avant
   toute publication. Le nettoyage local précédent reste une modification distincte
   à préserver dans le diff.

Pas de nouveau framework UI, de nouveau moteur de trading ou de réécriture de
l'authentification requis par ce plan. Les composants réutilisables restent dans
le système React/CSS existant.

## Critères d'acceptation

- Action principale accessible au premier écran à 1280 × 720 et 390 × 844.
- Vérifications complémentaires à 360, 768, 1440 et 1920 px ; aucun débordement
  horizontal et parcours clavier utilisable à 200 % de zoom.
- Connexion, autorisation, revue, lancement, détail et arrêt conservent leurs
  confirmations et le même compte de trading.
- Montants, limites et choix de côté conservés exactement ; modifier le formulaire
  ne retargete pas une session existante.
- Chargement, données absentes, marché au nom long, refus wallet, déconnexion,
  flux périmé et commande non confirmée disposent d'états lisibles.
- Les fenêtres ouvertes, le focus, les valeurs saisies et le défilement restent
  stables pendant les actualisations.
- Pas d'image décorative, de chiffre fictif ou de promesse de fill ajoutée.
- Les tests existants passent ; vérification des parcours avec exécution simulée,
  sans lancer de bot réel pour valider une amélioration visuelle.
