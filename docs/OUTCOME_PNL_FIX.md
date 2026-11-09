# Diagnostic Outcome — 9 novembre 2026

Les 13 fills publics correspondant exactement à la capture ont été rapprochés
avec les champs `px`, `sz`, `closedPnl`, `fee` et `crossed` de Hyperliquid.
Le nom de la carte désigne le marché actif ; les chiffres couvrent aussi les
marchés précédents de cette même session Auto.

| Mesure | Valeur exacte |
| --- | ---: |
| Volume exécuté | 199,7838 USD |
| PnL réalisé brut | −0,0624 USD |
| Frais | 0,07970853 USD |
| PnL réalisé net | −0,14210853 USD |
| Coût / million exécuté | 711,31158 USD |

| Marché | Fills | PnL brut | Frais | Net |
| --- | ---: | ---: | ---: | ---: |
| `#294861` (Austria) | 8 | +0,0016 | 0,04702492 | −0,04542492 |
| `#290561` | 4 | −0,064 | 0,03268361 | −0,09668361 |
| `#270411` (Charlotte / Cleveland, No) | 1 achat | 0 | 0 | 0 |

Austria : quatre achats à 0,38268 et ventes à 0,38269. Le spread gagné
est de 0,0004 USD par aller-retour de 40 tokens, mais la vente maker coûte
0,01175623 USD. Ce flux peut remplir rapidement tout en perdant de l'argent.
Sur `#290561`, une sortie taker à 0,77236 après achat à 0,77591 réalise
−0,071 USD brut et 0,02076103 USD de frais. Le seul champ `crossed` ne
permet pas d'attribuer cette sortie à un arrêt manuel, une rotation ou un
contrôle de risque ; les journaux locaux ne couvrent pas cette exécution.

Une vente Charlotte ultérieure porte les totaux à 14 fills, 215,1016 USD de
volume et −0,16269565 USD net. Elle est exclue du rapprochement de la capture.
Les données détaillées de l'audit local sont dans
`output/outcome-diagnosis/screenshot-reconciliation.json` (ignoré par Git).

## Correction

- Le catalogue conserve `feeScale` et `deployerFeeScale`.
- L'Auto utilise le taux maker spot du compte (`userFees`), sa remise de
  parrainage et les multiplicateurs Outcome / déployeur. Le taux observé
  ici est de **7,68 bps sur la vente**, même en maker. Les frais réels des
  fills restent la référence pour la comptabilité.
- Le scanner exclut les candidats dont
  `(ask × (1 − taux de clôture) − bid) / mid × 10000 < 1 bp`.
- Le moteur revérifie ce critère avant les nouvelles entrées Auto : un
  marché sélectionné ne conserve pas son admissibilité si le carnet change.
  Les reliquats d'un bid ne contournent pas ce contrôle.
- Les taux sont isolés par wallet, rafraîchis en arrière-plan après 60 s et
  expirent après 5 minutes. Une donnée absente ou expirée bloque les achats,
  sans bloquer les ventes de l'inventaire existant.
- Les prix restent au touch, les montants et plafonds sont conservés.
  Les arrêts, sorties taker de sécurité et limites de risque sont préservés.
- Le scanner affiche le net théorique après frais et une raison d'attente
  concise lorsqu'aucun candidat ne couvre les coûts.

Ce filtre vérifie une condition économique à l'entrée. Il ne prédit ni
l'évolution du prix, ni les fills, ni le coût d'une future sortie forcée.
Il peut réduire le volume lorsque les spreads sont insuffisants ; aucun
élargissement automatique de la limite de spread n'est effectué.
Le volume affiché est le notionnel exécuté brut et ne représente pas
nécessairement le volume éligible aux paliers/récompenses Hyperliquid.

Sources : [frais Outcome](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees#outcome-tokens),
[multiplicateurs déployeur](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/hip-4-deployer-actions#deployer-fee-scale),
requêtes publiques `userFees`, `userFills` et `outcomeMeta` au moment de l'audit.

Validation : 9 tests de coûts / scanner / inventaire, 24 tests du workspace,
4 tests des objectifs de session et 23 tests frontend passent. Build
TypeScript / Vite réussi. Les parcours navigateur passent à 1440 et 390 px,
avec 31 composants Layro observés, signatures de wallets jetables,
lancement simulé, mises à jour, arrêt et isolation des comptes. Aucun ordre
réel envoyé pour les tests, aucun redémarrage du bot actif et aucun
déploiement EC2 / Vercel effectué dans ce correctif.
