# Plan Zeus : 31 composants Layro et icônes unifiées

Date : 9 novembre 2026.
Statut : plan uniquement. Aucune modification applicative ni installation effectuée dans ce tour.
Les composants Layro déjà intégrés et les changements locaux précédents restent conservés.

## 1. Résultat attendu

Compléter les quatre pages Lancer, Sessions, Performances et Compte avec **31 références
Layro distinctes : 3 déjà adaptées, 28 nouvelles**, puis un jeu cohérent d’icônes.
Une référence compte lorsqu’un composant ou une adaptation de son code est réellement
utilisé dans un parcours visible. Plusieurs instances du même bouton ne comptent qu’une
fois. Une simple citation, un fichier inutilisé ou un changement de classe CSS ne suffit pas.

L’identité conservée est celle du workspace restauré : fond bleu nuit, or, violet,
Exo 2, signature Orbitron et chiffres JetBrains Mono. La navigation actuelle reste
reconnaissable, les cartes de stratégie restent identifiables et aucune image décorative
n’est réintroduite. Le résultat doit être plus complet et cohérent, sans changement des
réglages, des signatures ou de la stratégie de trading.

## 2. Recherche effectuée

Le MCP Layro a servi à rechercher les catégories, récupérer les sources et examiner les
dépendances déclarées. Des plans d’installation complets ont aussi été demandés pour
Wallet Card, Sheet, Select, Halo Field et Recharts Bar Chart. Les versions et licences de
la sélection ci-dessous ont été vérifiées dans les réponses du MCP ; toutes annoncent MIT.
La lecture approfondie du code et de ses dépendances transitives reste une étape
d’intégration, avant toute exécution de source tierce.

Les pages publiques ont complété les capacités du MCP, qui n’expose pas d’outil de
navigation Libraries/Collections :

- [Newest](https://layro.dev/components/all?sort=newest) et
  [Most installed](https://layro.dev/components/all?sort=installs) : comparaison des nouveautés
  et des composants les plus utilisés, sans confondre popularité et pertinence pour Zeus.
- [Libraries](https://layro.dev/libraries) et [Authors](https://layro.dev/authors) :
  identification des familles, auteurs et licences.
- [Dashboards and data](https://layro.dev/lists/dashboards) :
  choix de graphiques adaptés à des données existantes ; la collection contient notamment
  Recharts Pie Chart.
- [Small details that feel good](https://layro.dev/lists/small-details) :
  retenue sur les effets et sélection du Dock de Magic UI.
- [Build an AI chat](https://layro.dev/lists/ai-chat) :
  étude des compositions de recherche et de retours asynchrones ; le champ de recherche
  prévu ci-dessous porte sur les marchés et sessions réellement disponibles.

Les collections servent de références et ne sont pas des dépendances installées en bloc.

## 3. Bibliothèques et cohérence

| Famille examinée | Rôle retenu |
| --- | --- |
| [HextaUI](https://layro.dev/libraries/hextaui), preetsuthar17 | Contrôles, sélection, dialogue d’alerte, sheet, skeleton, empty et table. |
| [Cult UI](https://layro.dev/libraries/cult-ui), nolly-studio | Champs, sélection coulissante, notifications et progression adaptés à Zeus. |
| [SmoothUI](https://layro.dev/libraries/smoothui), educlopez | Wallet, avatar, étapes, dialogues, alertes, statistiques et aides contextuelles. |
| [KokonutUI](https://layro.dev/libraries/kokonutui), kokonut-labs | Menu du wallet et composition de recherche globale. |
| [Magic UI](https://layro.dev/libraries/magicui) | Dock mobile, avec cibles fixes au toucher. |
| [Spell](https://layro.dev/libraries/spell) | Présentation des raccourcis clavier. |
| [Kibo UI](https://layro.dev/libraries/kibo-ui) | Composition des indicateurs de statut. |
| [Loading UI](https://layro.dev/libraries/loading-ui) | Indicateur d’attente indéterminée. |
| [Ncdai](https://layro.dev/libraries/ncdai) | Divulgation progressive des options par accordéon. |
| Recharts via legions-developer | Un seul moteur graphique pour les deux visualisations de Performances. |

Ces familles fournissent des sources et des comportements, pas des thèmes à superposer.
Chaque adaptation reçoit les mêmes couleurs, rayons, tailles, focus et règles de mouvement.

## 4. Inventaire des 31 références

| Nº | Source Layro | Emplacement | Utilisation prévue | Lot / état |
| --- | --- | --- | --- | --- |
| 1 | [Dock](https://layro.dev/@magicuidesign/dock) — `magicuidesign/dock` | Navigation mobile | Dock des quatre pages, labels persistants et zones tactiles stables. | 1 · nouveau |
| 2 | [Tabs](https://layro.dev/@preetsuthar17/tabs) — `preetsuthar17/tabs` | Sessions / Performances | Onglets de détail : exécution, position et paramètres ; filtres de période cohérents. | 1 · nouveau |
| 3 | [User Account Avatar](https://layro.dev/@educlopez/user-account-avatar) — `educlopez/user-account-avatar` | En-tête / Compte | Avatar dérivé du wallet et adresse courte ; identification du compte actif. | 1 · nouveau |
| 4 | [Wallet Card](https://layro.dev/@educlopez/wallet-card) — `educlopez/wallet-card` | Compte | Solde réel, compte Hyperliquid, réseau et autorisation de l’agent dans une carte unique. | 2 · nouveau |
| 5 | [Profile Dropdown](https://layro.dev/@kokonut-labs/profile-dropdown) — `kokonut-labs/profile-dropdown` | En-tête | Menu du wallet : compte, copie de l’adresse, lien Hyperliquid, déconnexion. | 1 · nouveau |
| 6 | [Button](https://layro.dev/@preetsuthar17/button) — `preetsuthar17/button` | Actions | Étendre le bouton contrôlé existant aux actions asynchrones, avec libellé et largeur stables. | 2 · existant à enrichir |
| 7 | [Halo Input](https://layro.dev/@nolly-studio/halo-input) — `nolly-studio/halo-input` | Lancer | Champs précis du montant, du plafond, du spread et des objectifs ; état focus Zeus. | 2 · nouveau |
| 8 | [Select](https://layro.dev/@preetsuthar17/select) — `preetsuthar17/select` | Lancer | Sélection HIP-3 au clavier, avec recherche associée et marché explicitement sélectionné. | 2 · nouveau |
| 9 | [Halo Toggle Group](https://layro.dev/@nolly-studio/halo-toggle-group) — `nolly-studio/halo-toggle-group` | Lancer / Filtres | Consolider Yes/No, fin de session et filtres ; un seul état contrôlé par groupe. | 2 · existant à enrichir |
| 10 | [Switch](https://layro.dev/@preetsuthar17/switch) — `preetsuthar17/switch` | Sessions | Préférence locale Vue compacte : cartes ou tableau, sans modifier les bots. | 3 · nouveau |
| 11 | [Animated Stepper](https://layro.dev/@educlopez/animated-stepper) — `educlopez/animated-stepper` | Connexion / Lancer | Étapes réelles wallet, agent et revue de session ; progression fondée sur leur état vérifié. | 2 · nouveau |
| 12 | [Alert dialog](https://layro.dev/@preetsuthar17/alert-dialog) — `preetsuthar17/alert-dialog` | Sessions | Confirmation d’arrêt avec marché, position connue et état de vérification. | 2 · nouveau |
| 13 | [Dialog](https://layro.dev/@educlopez/dialog) — `educlopez/dialog` | Connexion / Revue | Modales wallet et revue finale : focus, Escape et paramètres complets. | 2 · nouveau |
| 14 | [Sheet](https://layro.dev/@preetsuthar17/sheet) — `preetsuthar17/sheet` | Mobile | Panneau mobile des réglages et détails, partageant les mêmes valeurs que la vue desktop. | 2 · nouveau |
| 15 | [Apple Accordion](https://layro.dev/@ncdai/apple-accordion) — `ncdai/apple-accordion` | Lancer / Détails | Options avancées et paramètres effectifs ouverts à la demande, sans perdre la saisie. | 2 · nouveau |
| 16 | [Status](https://layro.dev/@shadcnblocks/status) — `shadcnblocks/status` | Tout le workspace | États frais, retardés, en vérification et arrêtés ; priorité aux preuves disponibles. | 3 · existant à enrichir |
| 17 | [Alert](https://layro.dev/@educlopez/alert) — `educlopez/alert` | Formulaires / Commandes | Erreur annoncée près de l’action ; bandeau global réservé à la connexion générale. | 3 · nouveau |
| 18 | [Halo Notification](https://layro.dev/@nolly-studio/halo-notification) — `nolly-studio/halo-notification` | Commandes | Retours discrets pour copie, lancement confirmé et arrêt vérifié ; attente clairement distincte. | 3 · nouveau |
| 19 | [Skeleton](https://layro.dev/@preetsuthar17/skeleton) — `preetsuthar17/skeleton` | Sessions / Compte | Chargement initial à dimensions réservées ; les données retardées gardent leur état explicite. | 3 · nouveau |
| 20 | [Empty](https://layro.dev/@preetsuthar17/empty) — `preetsuthar17/empty` | Toutes les pages | États wallet absent, aucun résultat et aucune session, chacun avec une prochaine action. | 3 · nouveau |
| 21 | [Stats 2](https://layro.dev/@educlopez/stats-2) — `educlopez/stats-2` | Sessions / Performances | Cartes volume, fills, coût par million et net réalisé, branchées aux agrégats réels. | 3 · nouveau |
| 22 | [Table](https://layro.dev/@preetsuthar17/table) — `preetsuthar17/table` | Scanner / Sessions | Colonnes alignées, tri des données reçues, en-tête lisible et accès aux détails. | 3 · nouveau |
| 23 | [Recharts Bar Chart](https://layro.dev/@legions-developer/recharts-bar-chart) — `legions-developer/recharts-bar-chart` | Performances | Volume exécuté par session, axes et infobulles précis, équivalent accessible en tableau. | 4 · nouveau |
| 24 | [Recharts Pie Chart](https://layro.dev/@legions-developer/recharts-pie-chart) — `legions-developer/recharts-pie-chart` | Performances | Répartition du volume par stratégie, calculée sur les mêmes sessions et la même période. | 4 · nouveau |
| 25 | [Rich Popover](https://layro.dev/@educlopez/rich-popover) — `educlopez/rich-popover` | Champs / Statistiques | Aide contextuelle pour bps, file estimée, frais et coût par million, accessible au toucher. | 3 · nouveau |
| 26 | [Keyboard Shortcuts](https://layro.dev/@xxtomm/keyboard-shortcuts) — `xxtomm/keyboard-shortcuts` | Navigation / Aide | Touches de recherche et de navigation affichées ; Escape ferme les couches ouvertes. | 1 · nouveau |
| 27 | [Scroll area](https://layro.dev/@preetsuthar17/scroll-area) — `preetsuthar17/scroll-area` | Scanner / Listes | Listes longues et tableaux défilants sans masquer le focus ni déplacer la page à chaque tick. | 3 · nouveau |
| 28 | [Halo Progress](https://layro.dev/@nolly-studio/halo-progress) — `nolly-studio/halo-progress` | Sessions | Progression d’un objectif durée ou volume réellement configuré ; état inconnu explicite. | 3 · nouveau |
| 29 | [Fade Arc](https://layro.dev/@turbostarter/fade-arc) — `turbostarter/fade-arc` | Attentes | Chargement indéterminé lors de signature, vérification et réconciliation, accompagné d’un texte. | 2 · nouveau |
| 30 | [Halo Field](https://layro.dev/@nolly-studio/halo-field) — `nolly-studio/halo-field` | Lancer / Compte | Label, description, erreur et champ reliés ; unités toujours explicites. | 2 · nouveau |
| 31 | [Ai Input Search](https://layro.dev/@kokonut-labs/ai-input-search) — `kokonut-labs/ai-input-search` | Recherche globale | Palette pour rechercher les marchés et sessions chargés, puis naviguer vers un résultat. | 1 · nouveau |

### Versions et provenance

- `magicuidesign/dock` : version 1.0.0, licence MIT.
- `preetsuthar17/tabs` : version 1.0.0, licence MIT.
- `educlopez/user-account-avatar` : version 1.0.1, licence MIT.
- `educlopez/wallet-card` : version 1.0.1, licence MIT.
- `kokonut-labs/profile-dropdown` : version 1.0.0, licence MIT.
- `preetsuthar17/button` : version 1.0.0, licence MIT.
- `nolly-studio/halo-input` : version 1.0.0, licence MIT.
- `preetsuthar17/select` : version 1.0.0, licence MIT.
- `nolly-studio/halo-toggle-group` : version 1.0.0, licence MIT.
- `preetsuthar17/switch` : version 1.0.0, licence MIT.
- `educlopez/animated-stepper` : version 1.0.1, licence MIT.
- `preetsuthar17/alert-dialog` : version 1.0.0, licence MIT.
- `educlopez/dialog` : version 1.0.1, licence MIT.
- `preetsuthar17/sheet` : version 1.0.0, licence MIT.
- `ncdai/apple-accordion` : version 1.0.0, licence MIT.
- `shadcnblocks/status` : version 1.0.0, licence MIT.
- `educlopez/alert` : version 1.0.1, licence MIT.
- `nolly-studio/halo-notification` : version 1.0.0, licence MIT.
- `preetsuthar17/skeleton` : version 1.0.0, licence MIT.
- `preetsuthar17/empty` : version 1.0.0, licence MIT.
- `educlopez/stats-2` : version 1.0.1, licence MIT.
- `preetsuthar17/table` : version 1.0.0, licence MIT.
- `legions-developer/recharts-bar-chart` : version 1.0.0, licence MIT.
- `legions-developer/recharts-pie-chart` : version 1.0.0, licence MIT.
- `educlopez/rich-popover` : version 1.0.0, licence MIT.
- `xxtomm/keyboard-shortcuts` : version 1.0.0, licence MIT.
- `preetsuthar17/scroll-area` : version 1.0.0, licence MIT.
- `nolly-studio/halo-progress` : version 1.0.0, licence MIT.
- `turbostarter/fade-arc` : version 1.0.0, licence MIT.
- `nolly-studio/halo-field` : version 1.0.0, licence MIT.
- `kokonut-labs/ai-input-search` : version 1.0.0, licence MIT.

Les trois références existantes correspondent à FeedbackButton, SegmentedControl et
StatusBadge. Elles seront enrichies, pas comptées comme trois nouvelles intégrations.

## 5. Icônes

Adopter **Lucide React** comme famille unique d’icônes d’interface. Les sources Layro
utilisent actuellement Lucide et Tabler ; leurs imports seront harmonisés dans un adaptateur
commun qui conserve l’API interne `Icon name=...` et les attributs accessibles.
Le monogramme Zeus reste sa signature actuelle.

La [documentation officielle Lucide](https://lucide.dev/guide/react) confirme les SVG
inline, le réglage de la taille et du trait et l’inclusion des seules icônes importées.
Importer explicitement les icônes utiles, sans catalogue dynamique complet.

Prévoir environ 30 rôles : wallet, compte, agent vérifié, déconnexion, lancer, arrêter,
actualiser, rechercher, fermer, valider, chevron, flèche, bid/ask, graphique de volume,
répartition, carnet, cible, réglages, copie, lien externe, alerte, chargement, durée,
filtrer, liste, grille, menu, clavier, notification et aide. Les noms d’exports exacts
seront alignés sur la version installée et vérifiés par TypeScript.

Les boutons d’icône auront un libellé accessible ; les icônes accompagnées de texte
seront décoratives pour le lecteur d’écran. Les états importants auront toujours un texte.
La licence ISC de Lucide sera conservée dans les notices, distincte des licences MIT
des composants Layro.

## 6. Parcours et données

### Lancer

Conserver les trois stratégies et les valeurs actuelles : 25 USD par ordre, plafond
200 USD, côté No, spread 10 bps et seuil de perte réalisée 50 USD. Les options de durée
et de volume restent celles de la session préparée. Ces valeurs ne sont pas modifiées
par les composants visuels.

Le formulaire utilise Halo Field, Halo Input, Select et Halo Toggle Group. Les options
avancées s’ouvrent dans l’accordéon sur desktop et dans le Sheet sur mobile, en partageant
un seul état de brouillon. La revue finale reste obligatoire et affiche les paramètres
effectifs. L’état contrôlé du bouton reste relié aux vraies réponses de commande.

### Compte et wallet

Le menu de compte, l’avatar et la carte wallet montrent le même propriétaire. Le Stepper
distingue l’identité connectée, l’autorisation de l’agent et la préparation d’une session.
Un rafraîchissement ou un refus de signature ne devient pas un succès visuel.
Les résultats de recherche, préférences privées et états affichés suivent le propriétaire
du workspace ; un changement de wallet invalide les données de l’ancien compte.

### Scanner et sessions

Les candidats du scanner restent distincts du marché actif de chaque session. Le
classement montre les données reçues : débit en USD/min, spread et file estimée.
La vue compacte est une préférence de présentation, indépendante du moteur.

Les cartes ou lignes conservent leur identifiant pendant les mises à jour. Les badges
font primer les données retardées et les arrêts non confirmés. Les valeurs inconnues
restent inconnues et ne deviennent pas zéro. Un bouton Vérifier reste accessible
lorsqu’une commande ou un arrêt n’est pas confirmé.

### Performances

Le graphique en barres utilise le volume exécuté par session. Le graphique circulaire
utilise la répartition du volume des mêmes sessions par stratégie, sur la même période.
Un tableau accessible donne les valeurs exactes. Avec un total nul, afficher un état vide ;
avec des données incomplètes, afficher clairement cet état plutôt qu’une répartition fausse.

La progression durée/volume n’existe que si un objectif a réellement été enregistré.
Les jeux de données de démonstration des composants seront retirés des adaptations.
Les graphiques et indicateurs consommeront les mêmes projections que les tableaux.
Le frontend doit typer explicitement la projection du serveur pour
`session_duration_sec`, `session_expires_at` et `target_volume_usd`, au lieu de supposer
que les noms du brouillon et ceux du snapshot sont identiques. Les timers utilisent le
temps du snapshot et cessent d’extrapoler lorsque les données sont retardées.

## 7. Architecture et dépendances

Conserver React, TypeScript, Vite et le système CSS actuel. Adapter les classes Tailwind
des références en styles de composants Zeus. Le projet garde ainsi un seul système de styles.

Organisation cible, en conservant les points d’entrée actuels :

- `src/components/` : primitives Layro adaptées, dont les trois composants existants.
- `src/components/workspace/` : compositions métier WalletPanel, ScannerPanel,
  SessionRow, SessionReview, MetricsOverview et GlobalSearch.
- `src/presentation/` : sélecteurs purs pour statuts, regroupements, formats et progression.
- `src/views/` : assemblage des quatre pages et branchement aux propriétés réelles.
- Adaptateur d’icônes commun derrière `ui.tsx`.
- Tokens Zeus centralisés et règles de mouvement partagées, sans changer les valeurs de marque.
- `THIRD_PARTY_NOTICES.md` : source, auteur, version et licence de chaque adaptation utilisée.

`useWorkspace` reste la source des snapshots, de leur fraîcheur et de leur propriétaire.
Les composants visuels ne démarrent pas leurs propres boucles de polling concurrentes.

Dépendances retenues : Lucide pour les icônes ; Base UI pour les contrôles complexes
nécessitant gestion du focus et du clavier ; Recharts pour les graphiques.
Les transitions courantes restent en CSS. Les parties graphiques sont chargées à l’ouverture
de Performances. Les imports Motion ou Radix des exemples sont adaptés ou mutualisés ;
ils ne justifient pas l’ajout automatique d’une deuxième bibliothèque pour la même fonction.

L’import automatique global de 31 composants n’est pas la méthode retenue :
les plans Layro peuvent embarquer des helpers et composants partagés. Par exemple,
Sheet référence Button et ses hooks ; Halo Field référence les primitives field/label/separator.
Ces dépendances seront fusionnées et contrôlées sans écraser FeedbackButton ni ses états
financiers contrôlés. Le helper `cn`, les icônes et les tokens auront une seule implémentation.

Un candidat écarté à ce stade est `bklit/bar-chart` : son plan d’installation a retourné
« Conflicting registry files at src/lib/utils.ts. Nothing written. ».
Les graphiques retenus partagent donc Recharts. Halo Search, qui déclare un runtime de shaders,
a également été remplacé dans la sélection par la composition de recherche KokonutUI.

## 8. Mouvement, lisibilité et rapidité

- Réaction aux clics, focus et sélection : 100–180 ms ; ouverture des couches : 180–240 ms.
- Respect de `prefers-reduced-motion`, y compris pour les animations des sources tierces.
- Chiffres tabulaires et mise à jour immédiate des données financières.
- Dimensions réservées pendant le chargement ; aucune animation de chiffre à chaque tick.
- Cibles tactiles de 44 px et focus visible, y compris dans les sheets et popovers.
- Graphiques sans animation à chaque révision du snapshot ; interaction et changement de
  période peuvent avoir une transition courte.
- Icônes importées explicitement ; un seul moteur graphique ; rendu stable des lignes.
- Aucun pourcentage de gain en rapidité annoncé sans mesure avant/après.

## 9. Réalisation en cinq lots

1. **Socle et navigation** : état initial sauvegardé, tokens inchangés, icônes, Dock,
   onglets, avatar, menu du wallet, raccourcis et recherche. Captures comparées à l’existant.
2. **Préparation et connexion** : champs, sélection, étapes, wallet, accordéon,
   revue, confirmation et panneau mobile. Vérifier d’abord les états refusés et en attente.
3. **Suivi de l’exécution** : scanner, cartes/tableau, préférences visuelles, statistiques,
   chargements, statuts, alertes, notifications et progression réelle.
4. **Performances** : regroupements exacts, barres et répartition, table équivalente,
   chargement différé et gestion des données absentes.
5. **Finition** : cohérence des 31 intégrations, navigation clavier, mobile, états réseau,
   mesure du bundle, nettoyage des seuls éléments devenus inutilisés, documentation de provenance.

Chaque lot doit fonctionner avant le suivant et rester réversible. Les captures et la
vérification des valeurs servent à contrôler l’identité visuelle et la continuité des parcours.

## 10. Conditions de livraison

- Au moins 25 références distinctes réellement utilisées, avec cible de 31 ; registre
  de provenance relié aux imports et aux parcours visibles.
- Palette, polices et repères de la version restaurée conservés.
- Navigation, recherche, saisie, signature, revue, commande et arrêt testés au clavier.
- Revue finale, propriétaire du wallet et paramètres lancés inchangés après édition du brouillon.
- Aucun renvoi automatique d’une commande à résultat inconnu ; arrêt non confirmé toujours visible.
- Équivalence desktop/mobile d’un seul brouillon et gestion du clavier virtuel et des safe areas.
- Vérifications à 360, 390, 768, 1280, 1440 et 1920 px ; reflow à l’équivalent d’un zoom 200 %.
- États testés : wallet absent, signature refusée, réseau indisponible, données retardées,
  état comptable incomplet, session vide, nom de marché long, objectifs absents et stop incertain.
- Build TypeScript strict, tests frontend existants et nouveaux tests de comportement utiles,
  parcours Playwright avec wallets jetables et exécution simulée.
- Comparaison des chargements initiaux et des imports différés avant/après ; aucun
  chargement de la bibliothèque graphique sur Lancer sans nécessité.
- Tests sans fonds réels. Le travail UI n’autorise pas de nouvelle session live.
- Livrable local et contrôlable ; publication GitHub/Vercel/EC2 traitée séparément.

