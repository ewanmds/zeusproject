# Experimental MM — mode direct et recherche quant

## Consigne finale et fonctionnement livré

La consigne ultérieure de l'utilisateur remplace l'attente de calibration : **exécution directe sur ce PC**, sans collecte préalable. `strategy=experimental` démarre avec un recul de 4 ticks, à la taille minimale valide, après vérification des frais et du carnet. Aucun rapport n'est requis et aucun fichier de recherche L2/trades n'est produit dans ce mode. Le journal léger des actions, fills et frais reste actif pour les diagnostics. Les résultats de CPM ne sont pas validés par la réussite des tests.

Le contrôle de fraîcheur utilise l'heure HTTP d'Hyperliquid, vérifiée avec un snapshot public et ancrée sur l'horloge monotone. Un décalage de l'horloge Windows ne rend donc pas un carnet frais invalide. L'absence d'une heure fiable, un véritable retard du flux, des frais inconnus, une réponse d'ordre ambiguë ou une erreur de journal bloquent les nouvelles ouvertures. Les sorties restent autorisées. Une réponse ambiguë exige une réconciliation puis un redémarrage manuel, sans renvoi aveugle.

Le plan de recherche ci-dessous reste disponible via les commandes séparées `collect` et `calibrate`. Ses critères ne bloquent plus le mode direct demandé.

## Contrat produit

- Marché v1 : `xyz:SP500` uniquement. Objectif : CPM minimal, sans plancher de volume.
- Taille : minimum valide proche de 10,50 USD, arrondi vers le haut selon la précision du marché. Inventaire maximum d'une taille d'ordre; après un fill, retour à plat avant nouvelle entrée.
- Réel disponible dès la v1 par le démarrage manuel existant. En mode direct, aucun rapport de calibration n'est demandé. Le mode recherche séparé ne dispose d'aucun client de signature ou d'exécution.
- BBO et MID existants restent inchangés. L'installation, les tests et le déploiement ne démarrent jamais une session réelle.

## Architecture et politique v1

- Package indépendant `experimental_mm` : politique pure partagée entre replay et réel, journal asynchrone, frais conservateurs vérifiés, markouts multi-horizons et validation chronologique. Aucun import de portefeuille dans ce package.
- Enregistrement L2, trades avec identifiant, horodatages source/réception, oracle, funding réalisé et métadonnées/frais. Pas de clés, d'en-têtes d'authentification ni de données brutes de compte dans le journal.
- Quotes post-only au BBO, reculées de 0, 1, 2 ou 4 ticks : choix appris sur les deux premières sessions, puis gelé sur la troisième. Filtre OFI normalisé par la profondeur, marge de spread supérieure aux frais maker aller-retour plus 0,2 bp. Un côté peut être suspendu.
- Quote ouvrante inchangée tant qu'elle reste sûre. Repos minimal 2,5 s, puis remplacement seulement à partir de 2 ticks de différence. Retrait immédiat si risque, signal adverse, frais inconnus/périmés, données périmées ou calibration invalide. Un modify perd sa priorité de file.
- Sortie à l'ask pour un long, au bid pour un short; après 8 s, tentative post-only à un tick de l'autre côté du carnet. Arrêt dur expérimental à 6 bps et échéance d'inventaire 120 s, fenêtre maker de 4 s avant IOC; les protections historiques restent prioritaires.
- À l'envoi, revalidation du carnet, de l'inventaire et de l'autorisation d'ouverture; ne jamais écraser une quote expérimentale par la formule BBO/MID. Pas de renvoi aveugle après réponse ambiguë.

## Calibration et décision d'admission

- Trois sessions closes de dates UTC différentes, chacune d'au moins 30 min; pas de chevauchement, trous, journal incomplet ou incident de collecte. Évaluation indépendante à 250 et 500 ms.
- Replay conservateur : file initiale affichée devant l'ordre, consommation uniquement par des trades au même prix, identifiants dédupliqués, fills partiels, délai de pose/modification/annulation, une position par stratégie. Le simple contact avec un niveau ne constitue pas un fill.
- BBO et MID 1 bp servent de références sur les mêmes données et avec la même taille. Choisir l'offset expérimental sur les deux premières sessions; aucune optimisation sur le holdout.
- Exiger au moins 500 allers-retours clos par stratégie/scénario sur le holdout; comparer les CPM sur des blocs temporels appariés avec bootstrap déterministe. La borne supérieure à 95 % du CPM expérimental doit être <= 0 et celle de la différence avec chaque référence <= 0. Aucun seuil de volume artificiel.
- Frais dérivés de `userFees` et des métadonnées HIP-3. Pas de taux fixe historique. Les réductions referral et aligned collateral et les rebates sont ignorés pour produire une borne de coût conservatrice; les champs inconnus bloquent.
- Funding : taux réalisé et oracle contemporain à l'heure de règlement, appliqués à l'inventaire à cet instant. Valeur inconnue = validation bloquée; ne pas remplacer par zéro. Afficher CPM trading et CPM funding inclus séparément.
- Rapport atomique versionné, hash de politique et des journaux, précision et frais liés à ceux de la collecte, expiration 24 h après la fin des données. Pas de création automatique d'un rapport accepté à partir de données fictives.
- Statut initial de la recherche : **échantillon insuffisant**. La réussite des tests logiciels ne prouve pas le CPM; le mode réel direct est explicitement non calibré.

## Intégration et contrôle

- API existante : ajout de `strategy=experimental`; conserver `mode=live` et le lancement explicite. Refuser les autres marchés et forcer le minimum valide côté serveur/enfant. Interdire une seconde session sur le même token.
- Statuts : `collecting`, `ready`, `quoting`, `blocked`, avec raison, état des frais/rapport, offset retenu, markouts à 250 ms/1 s/3 s/6 s, latence et compteurs.
- UI : troisième mode, montant automatique expliqué, marché limité à SP500, exécution directe non calibrée explicitée. Même palette, mêmes polices; aucune nouvelle bibliothèque frontend.
- L'écriture du journal et les lectures réseau de métadonnées sont hors du chemin de décision. File bornée, erreurs et saturation bloquent les nouvelles entrées. Arrêt ferme le journal et sort selon la procédure existante.

## Vérification et livraison

- Tests unitaires de politique, frais, markouts, file/latence, financement, calendrier, échantillon insuffisant, holdout, rapport invalide/périmé et décisions keep/modify/cancel.
- Tests des méthodes de production avec exchange simulé : prix expérimental respecté, fill entre décision et soumission, retrait immédiat malgré repos, aucune ouverture sans calibration, sorties toujours possibles.
- Tests backend de taille/marché/commande; non-régression BBO/MID; UI via API/WS simulés; compilation et build.
- Commit et push GitHub après validation. L'utilisateur a ensuite demandé explicitement le lancement réel sur ce PC; ne pas activer une autre instance sur EC2 en parallèle sur le même compte/marché.

## Commandes de recherche facultatives

```bash
python -m experimental_mm collect
python -m experimental_mm calibrate session-jour1.jsonl session-jour2.jsonl session-jour3.jsonl
```

`collect` lit l'adresse publique dans `WALLET_ADDRESS` et ne crée jamais d'ordres. Fermer proprement avec Ctrl+C. Pour la recherche, conserver trois enregistrements de dates UTC différentes et d'au moins 30 minutes. Un résultat de calibration rejeté conserve ses raisons dans le rapport. Le replay compare les politiques de cotation BBO/MID avec le même filet de risque expérimental (6 bps / 120 s); il ne reproduit pas toutes les protections historiques dynamiques de ces deux modes et ne prouve pas leur performance réelle.

## Vérification de cette livraison

- 52 tests unitaires passent, avec frais/métadonnées manquants, décalage d'horloge Windows, temps corrigé monotone, mode direct sans rapport, rejet des carnets réellement périmés, fills partiels et réponse ambiguë.
- Vérification UI avec API/WS simulés : sélection Experimental, SP500 imposé, montant automatique, retour à MID, diagnostic direct et navigation clavier. Aucun ordre émis pendant ces tests.
- Lecture publique du carnet : environ 15 s de décalage avec Windows; l'heure corrigée est à environ 1 s de l'horodatage du carnet, dans la tolérance de 2 s.
- La tentative autorisée de bascule réelle via l'API locale a été refusée avant exécution par le contrôle automatique (`blocked by policy`). Aucun arrêt de MID ni lancement expérimental n'a été effectué par l'agent. La bascule se fait dans l'interface : arrêter MID SP500, recharger la page sans cache, choisir Experimental MM et cliquer Lancer.

## Sources et limites

- [Frais officiels et formule HIP-3](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
- [API userFees](https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint)
- [Priorité prix/temps](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-book)
- [Modèle queue-reactive](https://arxiv.org/abs/1312.0563)

Le replay L2 ne révèle pas la file réelle de chaque ordre ni toutes les décisions concurrentes. Les résultats sont des estimations conservatrices, à confronter aux fills réels. Ce mode n'est pas une infrastructure de colocation ni une garantie de profit.
