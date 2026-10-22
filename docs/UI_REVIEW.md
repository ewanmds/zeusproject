# Zeus — revue UI/UX du 22 octobre 2026

## Périmètre

Application des instructions lues dans les quatre skills demandés :

- [emil-design-eng](https://www.ui-skills.com/skills/emilkowalski/emil-design-eng)
- [make-interfaces-feel-better](https://www.ui-skills.com/skills/jakubkrehel/make-interfaces-feel-better)
- [12-principles-of-animation](https://www.ui-skills.com/skills/raphaelsalaja/12-principles-of-animation)
- [fixing-accessibility](https://www.ui-skills.com/skills/ibelick/fixing-accessibility)

Les quatre skills sont installés localement. Interface existante en HTML/CSS/JS conservée. À la demande de l'utilisateur, les couleurs, polices, grille de fond et identité d'origine restent présentes : bleu `#090d16`, or `#f59e0b`, violet `#8b5cf6`, Exo 2 et Orbitron. Aucun changement du moteur de trading.

## Corrections

| Severity | Location | Before | After | Why |
| --- | --- | --- | --- | --- |
| MEDIUM | `frontend/index.html`, `frontend/app.js` — onglets | Deuxième panneau toujours `hidden`, onglets sans flèches clavier | Visibilité synchronisée, sélection et tabindex synchronisés, flèches/Home/End | Rendre les deux stratégies accessibles au clavier et à la souris |
| MEDIUM | `frontend/app.js` — pickers | Navigation clavier partielle, focus non rendu au déclencheur | Recherche, flèches, Home/End, Entrée, Échap, fermeture à la sortie du focus, attributs ARIA cohérents | Parcourir et sélectionner les marchés sans souris |
| MEDIUM | `frontend/index.html`, `frontend/app.js` — accès | Superposition sans isolation native du focus | Dialog natif nommé, arrière-plan inerte, boucle Tab, label visible, erreur annoncée | Maintenir le focus dans la connexion |
| MEDIUM | `frontend/app.js` — formulaires | Valeurs absentes remplacées silencieusement, erreurs distantes uniquement en toast | Valeurs invalides bloquées avant envoi, focus sur le champ, aria-invalid et erreurs persistantes; spread nul transmis tel quel | Rendre les erreurs compréhensibles et corrigeables |
| MEDIUM | `frontend/app.js` — boutons | Mises à jour de statut pouvant réactiver un bouton occupé | Désactivation respectant aria-busy et état réel de la stratégie | Éviter les doubles soumissions pendant une requête |
| MEDIUM | `frontend/app.js` — statistiques | Valeur inconnue affichée comme zéro, absence de synthèse | Tiret pour donnée absente, synthèse des sessions actives, coût/M calculé sur le volume total, état vide explicite | Distinguer absence de données et résultat mesuré |
| MEDIUM | `frontend/app.js` — cartes | Reconstruction lors d'un changement de phase ou de distance | Mise à jour des textes sans remplacer la carte quand la liste de tokens reste identique; focus déplacé vers la carte si son bouton disparaît | Préserver le focus pendant les mises à jour |
| MEDIUM | `frontend/index.css` — responsive | Largeurs minimales rigides, texte d'erreur tronqué, carte arrêtée très atténuée | Colonnes flexibles à 320 px, en-tête repliable, erreurs complètes et lisibles, commandes compactes sans chevauchement | Accéder aux actions et aux diagnostics sur mobile |
| LOW | `frontend/index.css` — typography | Chiffres et retours à la ligne inégaux | Chiffres tabulaires, titres équilibrés, texte courant en pretty, hiérarchie et aides de formulaire | Stabiliser les chiffres et guider la lecture en gardant les polices existantes |
| LOW | `frontend/index.css` — surfaces | Cibles petites et hiérarchie des sessions peu explicite | Cibles de formulaire 44 px, boutons mobiles 44 px, cartes de synthèse et état vide dans les surfaces existantes | Améliorer le toucher et la lecture sans nouvelle palette |
| LOW | `frontend/index.css` — animations | transition: all, pulsations répétées, notifications sans sortie | Propriétés explicites, indicateurs statiques, sortie opacity/transform 180 ms ease-out, pression scale(.96), réduction du mouvement et interactions clavier respectées | Réduire la distraction sur un terminal fréquent |
| LOW | `frontend/index.html`, `frontend/app.js` — icons | Notification non fermable et durée fixe | Fermeture nommée avec icône SVG currentColor, minuterie suspendue au survol/focus et onglet masqué, maximum quatre notifications | Laisser le temps de lire et permettre de dégager l'écran |
| LOW | `frontend/index.css` — performance | Police appelée par le HTML et par @import CSS | Un seul chargement des mêmes polices, aucune nouvelle bibliothèque | Éviter une déclaration réseau redondante et conserver le système existant |

## Couverture des cinq catégories

| Catégorie | Vérification |
| --- | --- |
| Typography | Polices d'origine, chiffres tabulaires, texte long et montants, rendu avec les Google Fonts chargées |
| Surfaces | Rayons et bordures existants conservés, cibles tactiles, cartes vides/actives/arrêtées/en erreur |
| Animations | Entrée/sortie des notifications, focus/pression/chargement, lecture ralentie à 10 %, reduced motion |
| Icons | SVG existants conservés, nouveaux symboles locaux, icônes décoratives masquées aux lecteurs d'écran, fermeture nommée |
| Performance | Pas de dépendance frontend ajoutée, pas de transition all, pas d'animation des statistiques, stabilité DOM sur changement de statut |

### Principes d'animation

| Rule | Count | Severity |
| --- | --- | --- |
| `timing-under-300ms` — sortie de notification | 1 correction | LOW |
| `physics-active-state` — scale(.96) | 1 correction commune aux boutons | LOW |
| Mouvement réduit et absence de pulsation des données | 1 correction commune | LOW |

Emplacements corrigés :

- `frontend/index.css:1272` — `[timing-under-300ms]` notification de 180 ms avec sortie ease-out.
- `frontend/index.css:1276` — `[easing-exit-ease-out]` retrait par opacity/transform, sans variation de hauteur.
- `frontend/index.css:1277` — `[staging-restraint]` animations de changement d'onglet désactivées au clavier.
- `frontend/index.css:1316` — `[reduced-motion]` animations et transitions désactivées selon la préférence système.

## Pistes examinées puis écartées

| Location | Candidate | Rejected because |
| --- | --- | --- |
| `frontend/index.css` | Refonte graphite et or | L'utilisateur demande explicitement le style et les couleurs d'origine |
| Statistiques et cartes | Compteurs animés, stagger des mises à jour et pulsations live | Données fréquentes : distraction et instabilité visuelle sans bénéfice pour la lecture |
| Frontend | Ajouter React ou une bibliothèque d'animation | Les corrections sont réalisables avec le système HTML/CSS/JS existant |
| Boutons Start/Stop | Ajouter une confirmation systématique | Introduirait une étape supplémentaire dans le parcours existant; l'effet réel des commandes est expliqué à proximité |

## Vérification

- `python scripts/verify_ui.py --screenshots "$env:TEMP/zeus-ui-polish"` : PASS. HTTP et WebSocket de trading interceptés, données simulées; seules les polices externes peuvent être chargées.
- Captures et vérifications d'absence de débordement à 320, 390, 768 et 1440 px, états vide et session en erreur. Inspection visuelle des captures desktop, mobile et connexion.
- Parcours clavier des deux pickers et des onglets; validation sans requête pour champ vide; requête MID simulée avec distance 7,5 bps; erreur serveur persistante; focus conservé lors d'une mise à jour de phase; coût/M de 40 USD pour 10 000 USD de volume, 1,20 USD de frais et 0,80 USD de PnL réalisé.
- Connexion : isolation du focus, Shift+Tab/Tab, Échap, soumission d'un code fictif sur API simulée.
- Réduction du mouvement vérifiée par styles calculés; lecture des animations de notification à 10 % dans Chromium.
- `npm.cmd run build --prefix frontend` : PASS.
- `node --check frontend/app.js` : PASS.
- `python -m unittest discover -s tests -v` : 29 tests PASS; échanges et processus simulés par la suite existante.
- `git diff --check` : PASS.

**Verdict : Approve pour le périmètre vérifié.** Non vérifiés : lecteur d'écran réel, Safari/Firefox, audit WCAG complet des contrastes historiques, mesure de performance sur EC2. La palette historique a été conservée; ce document ne certifie pas une conformité WCAG globale. Aucun ordre réel ni redémarrage du bot effectué.
