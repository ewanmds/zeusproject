/**
 * Zeus workspace dictionaries.
 *
 * French is the source (base) dictionary. English must cover every key.
 * Market-making vocabulary that must never be translated is kept as-is in
 * both locales: Maker, Post-only, BBO, MID, Yes and No.
 */

const fr = {
  // Navigation
  "nav.ariaLabel": "Navigation principale",
  "nav.run": "Lancer",
  "nav.sessions": "Sessions",
  "nav.performance": "Performances",
  "nav.account": "Compte",

  // Header and global status
  "header.skipToContent": "Aller au contenu",
  "header.brandHome": "Zeus, accueil",
  "header.brandSub": "ESPACE DE TRADING",
  "header.live": "Live",
  "header.refreshing": "Actualisation",
  "header.refreshData": "Actualiser les données",
  "header.search": "Rechercher",
  "header.openSearch": "Ouvrir la recherche",
  "header.connect": "Connecter",
  "header.help": "Aide",
  "header.environment": "Hyperliquid Mainnet",

  // Common actions and reusable UI copy
  "common.loading": "Chargement…",
  "common.loadingSearch": "Chargement de la recherche",
  "common.loadingDetails": "Chargement du détail",
  "common.close": "Fermer",
  "common.cancel": "Annuler",
  "common.confirm": "Confirmer",
  "common.retry": "Réessayer",
  "common.refresh": "Actualiser",
  "common.back": "Retour",
  "common.continue": "Continuer",
  "common.copy": "Copier",
  "common.copied": "Copié",
  "common.openGuide": "Ouvrir le guide",
  "common.stop": "Arrêter",
  "common.installed": "Installé",
  "common.mobileQr": "Mobile & QR",
  "common.continueIn": "Continuez dans {wallet}",
  "common.walletConnected": "Wallet connecté. Votre espace est prêt.",
  "common.selectedCount": "{count} sélectionné(s)",
  "common.side.yes": "Yes",
  "common.side.no": "No",
  "common.side.none": "Non sélectionné",
  "common.searchTitle": "Rechercher dans Zeus",
  "common.sessionDetailsTitle": "Détail de la session",
  "common.walletTitle": "Votre wallet. Votre workspace.",
  "common.walletDescription":
    "Connectez votre wallet, puis signez le message de connexion.",
  "common.walletNoOrders":
    "La connexion ne lance aucun ordre et n’autorise pas le trading.",
  "common.walletInstallHint":
    "Installez un wallet EVM comme Rabby ou MetaMask, ou utilisez WalletConnect lorsqu’il est configuré.",
  "common.walletConnectMissing":
    "WalletConnect n’est pas encore configuré sur ce serveur.",
  "common.walletConnectServerMissing":
    "Le serveur doit configurer ZEUS_WALLETCONNECT_PROJECT_ID.",

  // Landing — hero
  "landing.hero.kicker": "Agent de market making",
  "landing.hero.kickerMeta": "Hyperliquid",
  "landing.hero.titleLead": "Le market making,",
  "landing.hero.titleAccent": " sans surveillance.",
  "landing.hero.lead":
    "Un agent qui cote dans le cadre que vous fixez, puis s’arrête proprement. Outcome et HIP-3, sans quitter Hyperliquid.",
  "landing.hero.connectWallet": "Connecter mon wallet",
  "landing.hero.howItWorks": "Comment ça marche",
  "landing.hero.proofAria": "Principes clés",
  "landing.hero.proofMaker": "Maker uniquement",
  "landing.hero.proofNonCustodial": "Non-custodial",
  "landing.hero.proofSessions": "Sessions attribuées",

  // Landing — session preview
  "landing.preview.aria": "Aperçu d’une session Zeus",
  "landing.preview.title": "Session Auto Outcome",
  "landing.preview.badge": "Aperçu",
  "landing.preview.market": "Marché",
  "landing.preview.autoMarket": "Sélection automatique",
  "landing.preview.execution": "Exécution",
  "landing.preview.rotation": "Rotation",
  "landing.preview.stop": "Arrêt",
  "landing.preview.journal": "Journal",
  "landing.preview.maker": "Maker",
  "landing.preview.flat": "À plat",
  "landing.preview.attributed": "Attribué",
  "landing.preview.durable": "Durable",
  "landing.preview.guardrails":
    "Garde-fous actifs pendant toute la session.",
  "landing.preview.caption": "Exemple de session — aucune donnée réelle.",
  "landing.preview.scope": "Périmètre fermé",

  // Landing — strategies and guardrails
  "landing.strategies.eyebrow": "Modes de cotation",
  "landing.strategies.title": "Trois stratégies. Trois garde-fous.",
  "landing.strategies.lead":
    "Un seul socle de contrôle. Vous choisissez la mécanique, Zeus applique les mêmes limites.",
  "landing.strategies.auto.tag": "Scan",
  "landing.strategies.auto.title": "Auto Outcome",
  "landing.strategies.auto.body":
    "Le scanner choisit le marché. La session entre en maker et tourne uniquement à plat.",
  "landing.strategies.bbo.tag": "Carnet",
  "landing.strategies.bbo.title": "BBO",
  "landing.strategies.bbo.body":
    "Deux prix au sommet du carnet, recalculés à chaque mouvement du bid et de l’ask.",
  "landing.strategies.mid.tag": "Distance",
  "landing.strategies.mid.title": "MID",
  "landing.strategies.mid.body":
    "Cotation à la distance choisie du prix médian, ajustée quand le carnet bouge.",
  "landing.guardrails.stop.title": "Arrêt instantané",
  "landing.guardrails.stop.body":
    "Un clic annule les ordres de la session et clôture seulement son inventaire attribué.",
  "landing.guardrails.risk.title": "Contrôles de risque",
  "landing.guardrails.risk.body":
    "Stop inventaire, spread maximal, carnet frais et soldes vérifiés avant chaque ordre.",
  "landing.guardrails.recovery.title": "Reprise backend",
  "landing.guardrails.recovery.body":
    "Une session interrompue reprend sans bouton. Un arrêt dur n’est jamais relancé.",

  // Landing — steps
  "landing.steps.eyebrow": "Parcours",
  "landing.steps.title": "Trois étapes.",
  "landing.steps.lead":
    "De la connexion au premier ordre, sans ligne de commande.",
  "landing.steps.wallet.title": "Connectez votre wallet",
  "landing.steps.wallet.body":
    "Signature de connexion. Aucun ordre n’est envoyé.",
  "landing.steps.agent.title": "Autorisez l’agent",
  "landing.steps.agent.body":
    "Une signature pour le trading. Vos fonds restent sur Hyperliquid.",
  "landing.steps.launch.title": "Lancez la session",
  "landing.steps.launch.body":
    "Le scanner choisit le marché. Vous suivez le volume et le PnL en direct.",

  // Landing — scope
  "landing.scope.eyebrow": "Périmètre",
  "landing.scope.title": "Ce qu’il fait. Ce qu’il ne touche pas.",
  "landing.scope.lead":
    "Une clé d’agent limitée au trading de la session. Le reste de votre compte reste hors de portée.",
  "landing.scope.allowed.title": "Autorisé",
  "landing.scope.allowed.badge": "Session",
  "landing.scope.allowed.orders.title": "Passer des ordres maker",
  "landing.scope.allowed.orders.body":
    "Dans la session active, au prix que vous avez défini.",
  "landing.scope.allowed.cancel.title": "Annuler les ordres",
  "landing.scope.allowed.cancel.body":
    "À tout moment, sans attendre une exécution.",
  "landing.scope.allowed.close.title": "Clôturer l’inventaire attribué",
  "landing.scope.allowed.close.body":
    "Uniquement les positions ouvertes par cette session.",
  "landing.scope.blocked.title": "Hors de portée",
  "landing.scope.blocked.withdraw.title":
    "Retirer ou transférer des fonds",
  "landing.scope.blocked.withdraw.body":
    "Aucun mouvement hors du compte Hyperliquid.",
  "landing.scope.blocked.taker.title": "Envoyer un ordre taker",
  "landing.scope.blocked.taker.body":
    "Le post-only refuse l’ordre avant qu’il ne traverse le spread.",
  "landing.scope.blocked.positions.title":
    "Toucher aux positions hors session",
  "landing.scope.blocked.positions.body":
    "Le reste de votre compte reste intact.",

  // Landing — trust
  "landing.trust.eyebrow": "Sécurité",
  "landing.trust.title": "Quatre limites explicites.",
  "landing.trust.lead":
    "Elles sont vérifiées à chaque ordre, sans dépendre de votre présence.",
  "landing.trust.postOnly.title": "Post-only strict",
  "landing.trust.postOnly.body":
    "Un ordre qui traverserait le spread est refusé, jamais converti en taker.",
  "landing.trust.inventory.title": "Inventaire cloisonné",
  "landing.trust.inventory.body":
    "Seules les positions ouvertes par la session sont clôturées à l’arrêt.",
  "landing.trust.recovery.title": "Reprise vérifiée",
  "landing.trust.recovery.body":
    "L’état de la session est contrôlé côté backend avant de reprendre.",
  "landing.trust.key.title": "Clé sans retrait",
  "landing.trust.key.body":
    "La clé de l’agent ne peut ni retirer ni transférer vos fonds.",

  // Landing — FAQ
  "landing.faq.eyebrow": "FAQ",
  "landing.faq.title": "Les questions qui restent.",
  "landing.faq.lead":
    "Quatre réponses courtes avant de connecter votre wallet.",
  "landing.faq.closeTab.question":
    "Que se passe-t-il si je ferme l’onglet ?",
  "landing.faq.closeTab.answer":
    "La session continue côté backend. À la réouverture, elle reprend sans bouton — sauf après un arrêt dur, qui n’est jamais relancé automatiquement.",
  "landing.faq.rejected.question":
    "Pourquoi un ordre peut-il être refusé ?",
  "landing.faq.rejected.answer":
    "Zeus vérifie le carnet, le spread, l’inventaire et les soldes avant chaque ordre. Un ordre qui ne respecte pas le cadre de la session est écarté, pas forcé.",
  "landing.faq.stop.question": "Comment arrêter une session ?",
  "landing.faq.stop.answer":
    "Un clic annule les ordres de la session et clôture uniquement l’inventaire attribué. Les positions qui ne viennent pas de la session ne sont pas touchées.",
  "landing.faq.market.question": "Qui choisit le marché ?",
  "landing.faq.market.answer":
    "En Auto Outcome, le scanner choisit le marché et la session tourne à plat. En BBO ou MID, vous désignez le marché ; les mêmes garde-fous s’appliquent.",

  // Landing — call to action
  "landing.cta.eyebrow": "Prêt ?",
  "landing.cta.title": "Votre prochaine session.",
  "landing.cta.lead":
    "Connectez votre wallet. Le cadre est prêt, l’arrêt aussi.",
  "landing.cta.connectWallet": "Connecter mon wallet",
  "landing.cta.openGuide": "Ouvrir le guide",

  // Launch — form
  "launch.page.title": "Lancer, simplement.",
  "launch.form.aria": "Préparer une session",
  "launch.form.strategyTitle": "Choisissez votre stratégie",
  "launch.form.configureTitle": "Configurez la session",
  "launch.form.amount": "Montant par ordre",
  "launch.form.minimum": "Minimum 10,50 $",
  "launch.form.quickAmountsAria": "Montants rapides",
  "launch.form.useMax": "Utiliser le montant maximal autorisé",
  "launch.form.percentOfMax": "{percent} du maximum autorisé",
  "launch.form.balanceLoading": "Solde en cours de chargement…",
  "launch.form.positionCap": "Plafond de position",
  "launch.form.capGaugeAria":
    "Montant par ordre par rapport au plafond de position",
  "launch.form.outcomeSide": "Côté Outcome",
  "launch.form.autoMarket": "Marché automatique",
  "launch.form.autoMarketDesc": "Flux et carnet observés en direct",
  "launch.form.marketHip3": "Marché HIP-3",
  "launch.form.searchMarket": "Rechercher un marché",
  "launch.form.searchMarketPlaceholder": "Rechercher un marché…",
  "launch.form.loadingMarkets": "Chargement de la sélection…",
  "launch.form.connectMarkets":
    "Connectez votre wallet pour charger les marchés.",
  "launch.form.sessionOptions": "Options de session",
  "launch.form.maxSuffix": "{amount} max.",
  "launch.form.positionCapField": "Plafond de position · USD",
  "launch.form.positionCapHelp":
    "Le plafond limite l’exposition cumulée de la session ; un ordre ne peut pas le dépasser.",
  "launch.form.maxSpread": "Spread maximal · bps",
  "launch.form.midDistance": "Distance au prix médian · bps",
  "launch.form.bboNote":
    "Le moteur cote au meilleur bid et au meilleur ask. Chaque ordre reste soumis aux contrôles de fonds et de risque.",
  "launch.form.sessionEnd": "Fin de session",
  "launch.form.manual": "Manuelle",
  "launch.form.duration": "Durée",
  "launch.form.volume": "Volume",
  "launch.form.durationMinutes": "Durée · minutes",
  "launch.form.targetVolume": "Volume exécuté cible · USD",
  "launch.form.stopLossField": "Seuil de perte réalisée · USD",
  "launch.form.endNote":
    "À l’objectif, les nouveaux achats cessent. La sortie reste en cours jusqu’à son exécution en maker.",
  "launch.form.frozen":
    "Les paramètres sont figés au lancement de la session.",

  // Launch — order summary
  "launch.summary.title": "Résumé de l’ordre.",
  "launch.summary.strategy": "Stratégie",
  "launch.summary.amount": "Montant par ordre",
  "launch.summary.market": "Marché",
  "launch.summary.autoSelection": "Sélection Auto",
  "launch.summary.toSelect": "À sélectionner",
  "launch.summary.execution": "Exécution",
  "launch.summary.makerPostOnly": "Maker · Post-only",
  "launch.summary.positionCap": "Plafond de position",
  "launch.summary.sessionEnd": "Fin de session",
  "launch.summary.stopLoss": "Seuil de perte",
  "launch.summary.hyperliquidAccount": "Compte Hyperliquid",
  "launch.summary.notConnected": "Non connecté",
  "launch.summary.connectWallet":
    "Connectez votre wallet pour continuer.",
  "launch.summary.authorizeTrading":
    "Autorisez le trading pour continuer.",
  "launch.summary.completeFields":
    "Complétez le montant, le plafond et le côté Outcome.",
  "launch.summary.ready": "Prêt à vérifier.",
  "launch.summary.verifying": "Vérification",
  "launch.summary.connectWalletCta": "Connecter mon wallet",
  "launch.summary.continueAuth": "Continuer vers l’autorisation",
  "launch.summary.reviewAndLaunch": "Vérifier et lancer",
  "launch.summary.footnoteOwner":
    "Vous vérifiez les paramètres avant le lancement.",
  "launch.summary.footnoteGuest":
    "Vos fonds restent sur votre compte Hyperliquid.",

  // Launch — review modal
  "launch.review.title": "Vérifiez votre session.",
  "launch.review.network": "Hyperliquid Mainnet · Ordres réels",
  "launch.review.account": "Compte",
  "launch.review.amount": "Montant par ordre",
  "launch.review.positionCap": "Plafond de position",
  "launch.review.engineLimits": "Limites du moteur",
  "launch.review.market": "Marché",
  "launch.review.autoScanner": "Scanner Auto · {side}",
  "launch.review.spotBalance": "Solde spot disponible",
  "launch.review.buyStop": "Arrêt des achats",
  "launch.review.volume": "{amount} de volume",
  "launch.review.manual": "Manuel",
  "launch.review.realizedLoss": "Seuil de perte réalisée",
  "launch.review.makerFees": "Frais maker estimés",
  "launch.review.makerFeeLine":
    "{bps} bps · {fee} par ordre de clôture.",
  "launch.review.outcomeFeeNote":
    "Ouverture Outcome gratuite · aucun rebate Outcome.",
  "launch.review.feeUnavailable":
    "Estimation indisponible ; les frais restent déduits du PnL.",
  "launch.review.feeNotEstimated":
    "Frais maker non estimés avant exécution ; aucun rebate n’est supposé.",
  "launch.review.maxLoss": "Perte maximale théorique",
  "launch.review.maxLossUnavailable":
    "Non estimable — limites du moteur.",
  "launch.review.guardsTitle": "Garde-fous actifs",
  "launch.review.guardPostOnly":
    "Post-only : aucun ordre taker à l’entrée.",
  "launch.review.guardMinimum": "Montant minimum : 10,50 $.",
  "launch.review.guardBalance":
    "Solde spot vérifié avant chaque ordre.",
  "launch.review.guardCap":
    "Un ordre ne dépasse pas le plafond de position.",
  "launch.review.guardSpread":
    "Spread maximal : {bps} bps · arrêt à {amount} de perte réalisée.",
  "launch.review.guardRotation": "Rotation à plat, sans vente forcée.",
  "launch.review.stopTitle": "Comment arrêter",
  "launch.review.stopBody":
    "Onglet Sessions → Arrêter. Les ordres de la session sont annulés et son inventaire attribué est clôturé au marché.",
  "launch.review.riskAck":
    "Je comprends que cette session passe des ordres réels sur Hyperliquid Mainnet et que je peux perdre le montant engagé.",
  "launch.review.roundingNote":
    "Les quantités Outcome sont arrondies à des tokens entiers. Le montant réel peut légèrement dépasser la taille demandée.",
  "launch.review.confirm": "Confirmer et lancer",
  "launch.review.confirming": "Lancement en cours",

  // Launch — steps and status
  "launch.steps.aria": "Étapes du lancement",
  "launch.steps.wallet": "Wallet",
  "launch.steps.authorization": "Autorisation",
  "launch.steps.session": "Session",
  "launch.status.launched": "Session lancée.",
  "launch.status.pending":
    "Lancement non confirmé. Vérifiez la session avant de réessayer.",
  "launch.status.awaiting":
    "Résultat en attente. Vérifiez la session avant de réessayer.",
  "launch.status.stopRequested":
    "Arrêt demandé : annulation des ordres et fermeture au marché.",
  "launch.status.stopConfirmed": "Arrêt et clôture confirmés.",
  "launch.status.stopping":
    "Arrêt demandé : clôture au marché en cours.",
  "launch.status.stopUnconfirmed": "Arrêt non confirmé : {message}",
  "launch.status.stopTimeout":
    "Arrêt non confirmé après 45 s : la session reste listée tant que la clôture n’est pas vérifiée.",

  // Errors and warnings
  "errors.signatureRejected":
    "Signature refusée. Vous pouvez réessayer.",
  "errors.connectionFailed": "La connexion a échoué.",
  "errors.authorizationRejected":
    "Autorisation refusée. Aucun bot lancé.",
  "errors.authorizationUnconfirmed":
    "Autorisation non confirmée. Vérifiez le compte.",
  "errors.agentNotReady":
    "Autorisez votre agent avant de lancer une session.",
  "errors.sideRequired": "Choisissez Yes ou No avant de lancer.",
  "errors.marketRequired": "Choisissez un marché avant de lancer.",
  "errors.amountRequired": "Saisissez un montant valide.",
  "errors.amountMinimum": "Minimum 10,50 $ par ordre.",
  "errors.amountMaximum": "Maximum 1 000 000 $ par ordre.",
  "errors.amountExceedsBalance":
    "Montant supérieur au solde spot disponible ({balance}).",
  "errors.amountBalanceWarning":
    "Ce montant utilise plus de 80 % de votre solde disponible ({balance}).",
  "errors.capMinimum":
    "Le plafond de position doit être d’au moins 10,50 $.",
  "errors.capMaximum": "Maximum 10 000 000 $ pour le plafond.",
  "errors.capBelowOrder":
    "Le plafond de position ({cap}) doit couvrir au moins un ordre.",
  "errors.dataDelayed":
    "Données retardées. Les valeurs affichées sont le dernier état reçu.",
  "errors.streamDelayed": "Flux retardé",
  "errors.walletUnavailable":
    "Installez un wallet EVM comme Rabby ou MetaMask, ou utilisez WalletConnect lorsqu’il est configuré.",
  "errors.walletConnectMissing":
    "WalletConnect n’est pas encore configuré sur ce serveur.",
  "errors.walletConnectProjectMissing":
    "Le serveur doit configurer ZEUS_WALLETCONNECT_PROJECT_ID.",
  "errors.unexpected": "Une erreur est survenue.",
} as const satisfies Record<string, string>;

export type Locale = "fr" | "en";
export type TranslationKey = keyof typeof fr;
export type TranslationParams = Record<string, string | number>;

export const defaultLocale: Locale = "fr";

const en: Record<TranslationKey, string> = {
  // Navigation
  "nav.ariaLabel": "Main navigation",
  "nav.run": "Launch",
  "nav.sessions": "Sessions",
  "nav.performance": "Performance",
  "nav.account": "Account",

  // Header and global status
  "header.skipToContent": "Skip to content",
  "header.brandHome": "Zeus, home",
  "header.brandSub": "TRADING WORKSPACE",
  "header.live": "Live",
  "header.refreshing": "Refreshing",
  "header.refreshData": "Refresh data",
  "header.search": "Search",
  "header.openSearch": "Open search",
  "header.connect": "Connect",
  "header.help": "Help",
  "header.environment": "Hyperliquid Mainnet",

  // Common actions and reusable UI copy
  "common.loading": "Loading…",
  "common.loadingSearch": "Loading search",
  "common.loadingDetails": "Loading details",
  "common.close": "Close",
  "common.cancel": "Cancel",
  "common.confirm": "Confirm",
  "common.retry": "Retry",
  "common.refresh": "Refresh",
  "common.back": "Back",
  "common.continue": "Continue",
  "common.copy": "Copy",
  "common.copied": "Copied",
  "common.openGuide": "Open guide",
  "common.stop": "Stop",
  "common.installed": "Installed",
  "common.mobileQr": "Mobile & QR",
  "common.continueIn": "Continue in {wallet}",
  "common.walletConnected": "Wallet connected. Your workspace is ready.",
  "common.selectedCount": "{count} selected",
  "common.side.yes": "Yes",
  "common.side.no": "No",
  "common.side.none": "Not selected",
  "common.searchTitle": "Search Zeus",
  "common.sessionDetailsTitle": "Session details",
  "common.walletTitle": "Your wallet. Your workspace.",
  "common.walletDescription":
    "Connect your wallet, then sign the login message.",
  "common.walletNoOrders":
    "Connecting places no order and does not authorize trading.",
  "common.walletInstallHint":
    "Install an EVM wallet such as Rabby or MetaMask, or use WalletConnect when it is configured.",
  "common.walletConnectMissing":
    "WalletConnect is not configured on this server yet.",
  "common.walletConnectServerMissing":
    "The server must configure ZEUS_WALLETCONNECT_PROJECT_ID.",

  // Landing — hero
  "landing.hero.kicker": "Market-making agent",
  "landing.hero.kickerMeta": "Hyperliquid",
  "landing.hero.titleLead": "Market making,",
  "landing.hero.titleAccent": " unattended.",
  "landing.hero.lead":
    "An agent that quotes within the limits you set, then stops cleanly. Outcome and HIP-3, without leaving Hyperliquid.",
  "landing.hero.connectWallet": "Connect wallet",
  "landing.hero.howItWorks": "How it works",
  "landing.hero.proofAria": "Key principles",
  "landing.hero.proofMaker": "Maker only",
  "landing.hero.proofNonCustodial": "Non-custodial",
  "landing.hero.proofSessions": "Scoped sessions",

  // Landing — session preview
  "landing.preview.aria": "Zeus session preview",
  "landing.preview.title": "Auto Outcome session",
  "landing.preview.badge": "Preview",
  "landing.preview.market": "Market",
  "landing.preview.autoMarket": "Automatic selection",
  "landing.preview.execution": "Execution",
  "landing.preview.rotation": "Rotation",
  "landing.preview.stop": "Stop",
  "landing.preview.journal": "Journal",
  "landing.preview.maker": "Maker",
  "landing.preview.flat": "Flat",
  "landing.preview.attributed": "Attributed",
  "landing.preview.durable": "Durable",
  "landing.preview.guardrails":
    "Guardrails active throughout the session.",
  "landing.preview.caption": "Sample session — no real data.",
  "landing.preview.scope": "Closed scope",

  // Landing — strategies and guardrails
  "landing.strategies.eyebrow": "Quoting modes",
  "landing.strategies.title": "Three strategies. Three guardrails.",
  "landing.strategies.lead":
    "One control framework. You choose the mechanics; Zeus applies the same limits.",
  "landing.strategies.auto.tag": "Scan",
  "landing.strategies.auto.title": "Auto Outcome",
  "landing.strategies.auto.body":
    "The scanner picks the market. The session enters as Maker and only rotates flat.",
  "landing.strategies.bbo.tag": "Book",
  "landing.strategies.bbo.title": "BBO",
  "landing.strategies.bbo.body":
    "Two prices at the top of the book, recalculated whenever the bid or ask moves.",
  "landing.strategies.mid.tag": "Distance",
  "landing.strategies.mid.title": "MID",
  "landing.strategies.mid.body":
    "Quoting at your chosen distance from the mid price, adjusted as the book moves.",
  "landing.guardrails.stop.title": "Instant stop",
  "landing.guardrails.stop.body":
    "One click cancels the session’s orders and closes only its scoped inventory.",
  "landing.guardrails.risk.title": "Risk controls",
  "landing.guardrails.risk.body":
    "Inventory stop, maximum spread, fresh book and balances checked before every order.",
  "landing.guardrails.recovery.title": "Backend recovery",
  "landing.guardrails.recovery.body":
    "An interrupted session resumes without a button. A hard stop is never restarted.",

  // Landing — steps
  "landing.steps.eyebrow": "Journey",
  "landing.steps.title": "Three steps.",
  "landing.steps.lead":
    "From the connection to the first order, without a command line.",
  "landing.steps.wallet.title": "Connect your wallet",
  "landing.steps.wallet.body":
    "Connection signature. No order is sent.",
  "landing.steps.agent.title": "Authorize the agent",
  "landing.steps.agent.body":
    "One signature for trading. Your funds stay on Hyperliquid.",
  "landing.steps.launch.title": "Launch the session",
  "landing.steps.launch.body":
    "The scanner picks the market. You follow volume and PnL live.",

  // Landing — scope
  "landing.scope.eyebrow": "Scope",
  "landing.scope.title": "What it does. What it leaves alone.",
  "landing.scope.lead":
    "An agent key limited to the session’s trading. The rest of your account stays out of reach.",
  "landing.scope.allowed.title": "Allowed",
  "landing.scope.allowed.badge": "Session",
  "landing.scope.allowed.orders.title": "Place Maker orders",
  "landing.scope.allowed.orders.body":
    "Within the active session, at the price you set.",
  "landing.scope.allowed.cancel.title": "Cancel orders",
  "landing.scope.allowed.cancel.body":
    "At any time, without waiting for a fill.",
  "landing.scope.allowed.close.title": "Close scoped inventory",
  "landing.scope.allowed.close.body":
    "Only positions opened by this session.",
  "landing.scope.blocked.title": "Out of scope",
  "landing.scope.blocked.withdraw.title":
    "Withdraw or transfer funds",
  "landing.scope.blocked.withdraw.body":
    "No movement out of the Hyperliquid account.",
  "landing.scope.blocked.taker.title": "Send a taker order",
  "landing.scope.blocked.taker.body":
    "Post-only rejects the order before it crosses the spread.",
  "landing.scope.blocked.positions.title":
    "Touch positions outside the session",
  "landing.scope.blocked.positions.body":
    "The rest of your account stays untouched.",

  // Landing — trust
  "landing.trust.eyebrow": "Security",
  "landing.trust.title": "Four explicit limits.",
  "landing.trust.lead":
    "They are checked on every order, whether you are present or not.",
  "landing.trust.postOnly.title": "Strict Post-only",
  "landing.trust.postOnly.body":
    "An order that would cross the spread is rejected, never converted to taker.",
  "landing.trust.inventory.title": "Scoped inventory",
  "landing.trust.inventory.body":
    "Only positions opened by the session are closed on stop.",
  "landing.trust.recovery.title": "Verified recovery",
  "landing.trust.recovery.body":
    "Session state is checked on the backend before resuming.",
  "landing.trust.key.title": "No-withdrawal key",
  "landing.trust.key.body":
    "The agent key cannot withdraw or transfer your funds.",

  // Landing — FAQ
  "landing.faq.eyebrow": "FAQ",
  "landing.faq.title": "The remaining questions.",
  "landing.faq.lead":
    "Four short answers before you connect your wallet.",
  "landing.faq.closeTab.question": "What happens if I close the tab?",
  "landing.faq.closeTab.answer":
    "The session keeps running on the backend. When you reopen it, it resumes without a button — except after a hard stop, which is never restarted automatically.",
  "landing.faq.rejected.question": "Why can an order be rejected?",
  "landing.faq.rejected.answer":
    "Zeus checks the book, spread, inventory and balances before every order. An order outside the session’s limits is skipped, not forced.",
  "landing.faq.stop.question": "How do I stop a session?",
  "landing.faq.stop.answer":
    "One click cancels the session’s orders and closes only the scoped inventory. Positions that did not come from the session are left untouched.",
  "landing.faq.market.question": "Who picks the market?",
  "landing.faq.market.answer":
    "In Auto Outcome, the scanner picks the market and the session rotates flat. In BBO or MID, you choose the market; the same guardrails apply.",

  // Landing — call to action
  "landing.cta.eyebrow": "Ready?",
  "landing.cta.title": "Your next session.",
  "landing.cta.lead":
    "Connect your wallet. The framework is ready, and so is the stop.",
  "landing.cta.connectWallet": "Connect wallet",
  "landing.cta.openGuide": "Open the guide",

  // Launch — form
  "launch.page.title": "Launch, simply.",
  "launch.form.aria": "Prepare a session",
  "launch.form.strategyTitle": "Choose your strategy",
  "launch.form.configureTitle": "Configure the session",
  "launch.form.amount": "Amount per order",
  "launch.form.minimum": "Minimum $10.50",
  "launch.form.quickAmountsAria": "Quick amounts",
  "launch.form.useMax": "Use the maximum allowed amount",
  "launch.form.percentOfMax": "{percent} of the maximum allowed",
  "launch.form.balanceLoading": "Loading balance…",
  "launch.form.positionCap": "Position cap",
  "launch.form.capGaugeAria":
    "Order amount relative to the position cap",
  "launch.form.outcomeSide": "Outcome side",
  "launch.form.autoMarket": "Automatic market",
  "launch.form.autoMarketDesc": "Live order flow and order book",
  "launch.form.marketHip3": "HIP-3 market",
  "launch.form.searchMarket": "Search for a market",
  "launch.form.searchMarketPlaceholder": "Search for a market…",
  "launch.form.loadingMarkets": "Loading selection…",
  "launch.form.connectMarkets":
    "Connect your wallet to load markets.",
  "launch.form.sessionOptions": "Session options",
  "launch.form.maxSuffix": "{amount} max.",
  "launch.form.positionCapField": "Position cap · USD",
  "launch.form.positionCapHelp":
    "The cap limits the session’s cumulative exposure; no single order can exceed it.",
  "launch.form.maxSpread": "Maximum spread · bps",
  "launch.form.midDistance": "Distance from mid price · bps",
  "launch.form.bboNote":
    "The engine quotes at the best bid and best ask. Every order remains subject to balance and risk checks.",
  "launch.form.sessionEnd": "Session end",
  "launch.form.manual": "Manual",
  "launch.form.duration": "Duration",
  "launch.form.volume": "Volume",
  "launch.form.durationMinutes": "Duration · minutes",
  "launch.form.targetVolume": "Target executed volume · USD",
  "launch.form.stopLossField": "Realized loss limit · USD",
  "launch.form.endNote":
    "At the target, new buys stop. The exit remains active until it fills as Maker.",
  "launch.form.frozen":
    "Parameters are fixed when the session launches.",

  // Launch — order summary
  "launch.summary.title": "Order summary.",
  "launch.summary.strategy": "Strategy",
  "launch.summary.amount": "Amount per order",
  "launch.summary.market": "Market",
  "launch.summary.autoSelection": "Auto selection",
  "launch.summary.toSelect": "To be selected",
  "launch.summary.execution": "Execution",
  "launch.summary.makerPostOnly": "Maker · Post-only",
  "launch.summary.positionCap": "Position cap",
  "launch.summary.sessionEnd": "Session end",
  "launch.summary.stopLoss": "Loss limit",
  "launch.summary.hyperliquidAccount": "Hyperliquid account",
  "launch.summary.notConnected": "Not connected",
  "launch.summary.connectWallet":
    "Connect your wallet to continue.",
  "launch.summary.authorizeTrading":
    "Authorize trading to continue.",
  "launch.summary.completeFields":
    "Complete the amount, cap and Outcome side.",
  "launch.summary.ready": "Ready to review.",
  "launch.summary.verifying": "Checking",
  "launch.summary.connectWalletCta": "Connect wallet",
  "launch.summary.continueAuth": "Continue to authorization",
  "launch.summary.reviewAndLaunch": "Review and launch",
  "launch.summary.footnoteOwner":
    "You review the parameters before launch.",
  "launch.summary.footnoteGuest":
    "Your funds stay in your Hyperliquid account.",

  // Launch — review modal
  "launch.review.title": "Review your session.",
  "launch.review.network": "Hyperliquid Mainnet · Real orders",
  "launch.review.account": "Account",
  "launch.review.amount": "Amount per order",
  "launch.review.positionCap": "Position cap",
  "launch.review.engineLimits": "Engine limits",
  "launch.review.market": "Market",
  "launch.review.autoScanner": "Auto Scanner · {side}",
  "launch.review.spotBalance": "Available spot balance",
  "launch.review.buyStop": "Buy stop",
  "launch.review.volume": "{amount} in volume",
  "launch.review.manual": "Manual",
  "launch.review.realizedLoss": "Realized loss limit",
  "launch.review.makerFees": "Estimated Maker fees",
  "launch.review.makerFeeLine":
    "{bps} bps · {fee} per closing order.",
  "launch.review.outcomeFeeNote":
    "Free Outcome entry · no Outcome rebate.",
  "launch.review.feeUnavailable":
    "Estimate unavailable; fees are still deducted from PnL.",
  "launch.review.feeNotEstimated":
    "Maker fees are not estimated before execution; no rebate is assumed.",
  "launch.review.maxLoss": "Theoretical maximum loss",
  "launch.review.maxLossUnavailable":
    "Not estimable — engine limits.",
  "launch.review.guardsTitle": "Active guardrails",
  "launch.review.guardPostOnly":
    "Post-only: no taker order on entry.",
  "launch.review.guardMinimum": "Minimum amount: $10.50.",
  "launch.review.guardBalance":
    "Spot balance checked before every order.",
  "launch.review.guardCap":
    "An order never exceeds the position cap.",
  "launch.review.guardSpread":
    "Maximum spread: {bps} bps · stop at {amount} realized loss.",
  "launch.review.guardRotation": "Flat rotation, no forced selling.",
  "launch.review.stopTitle": "How to stop",
  "launch.review.stopBody":
    "Sessions tab → Stop. The session’s orders are cancelled and its scoped inventory is closed at market.",
  "launch.review.riskAck":
    "I understand this session places real orders on Hyperliquid Mainnet and that I can lose the amount committed.",
  "launch.review.roundingNote":
    "Outcome quantities are rounded to whole tokens. The actual amount may slightly exceed the requested size.",
  "launch.review.confirm": "Confirm and launch",
  "launch.review.confirming": "Launching",

  // Launch — steps and status
  "launch.steps.aria": "Launch steps",
  "launch.steps.wallet": "Wallet",
  "launch.steps.authorization": "Authorization",
  "launch.steps.session": "Session",
  "launch.status.launched": "Session launched.",
  "launch.status.pending":
    "Launch not confirmed. Check the session before trying again.",
  "launch.status.awaiting":
    "Result pending. Check the session before trying again.",
  "launch.status.stopRequested":
    "Stop requested: cancelling orders and closing at market.",
  "launch.status.stopConfirmed": "Stop and close confirmed.",
  "launch.status.stopping": "Stop requested: closing at market.",
  "launch.status.stopUnconfirmed": "Stop not confirmed: {message}",
  "launch.status.stopTimeout":
    "Stop not confirmed after 45 s: the session stays listed until the close is verified.",

  // Errors and warnings
  "errors.signatureRejected":
    "Signature rejected. You can try again.",
  "errors.connectionFailed": "The connection failed.",
  "errors.authorizationRejected":
    "Authorization rejected. No bot launched.",
  "errors.authorizationUnconfirmed":
    "Authorization not confirmed. Check the account.",
  "errors.agentNotReady":
    "Authorize your agent before launching a session.",
  "errors.sideRequired": "Choose Yes or No before launching.",
  "errors.marketRequired": "Choose a market before launching.",
  "errors.amountRequired": "Enter a valid amount.",
  "errors.amountMinimum": "Minimum $10.50 per order.",
  "errors.amountMaximum": "Maximum $1,000,000 per order.",
  "errors.amountExceedsBalance":
    "Amount exceeds the available spot balance ({balance}).",
  "errors.amountBalanceWarning":
    "This amount uses more than 80% of your available balance ({balance}).",
  "errors.capMinimum":
    "The position cap must be at least $10.50.",
  "errors.capMaximum": "Maximum $10,000,000 for the cap.",
  "errors.capBelowOrder":
    "The position cap ({cap}) must cover at least one order.",
  "errors.dataDelayed":
    "Delayed data. Displayed values are the last state received.",
  "errors.streamDelayed": "Delayed feed",
  "errors.walletUnavailable":
    "Install an EVM wallet such as Rabby or MetaMask, or use WalletConnect when it is configured.",
  "errors.walletConnectMissing":
    "WalletConnect is not configured on this server yet.",
  "errors.walletConnectProjectMissing":
    "The server must configure ZEUS_WALLETCONNECT_PROJECT_ID.",
  "errors.unexpected": "Something went wrong.",
};

const dictionaries: Record<
  Locale,
  Partial<Record<TranslationKey, string>>
> = { fr, en };

/** True when a stored or external value is a supported locale. */
export function isLocale(value: unknown): value is Locale {
  return value === "fr" || value === "en";
}

/** Replace `{name}` placeholders, leaving unknown placeholders untouched. */
export function interpolate(
  template: string,
  params?: TranslationParams,
): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) => {
    const value = params[name];
    return value === undefined ? match : String(value);
  });
}

/**
 * Resolve a key in this order: requested locale, French base, English
 * fallback, then the key itself. Interpolates `{name}` parameters last.
 */
export function translate(
  locale: string,
  key: TranslationKey,
  params?: TranslationParams,
): string {
  const template =
    (isLocale(locale) ? dictionaries[locale][key] : undefined) ??
    dictionaries[defaultLocale][key] ??
    dictionaries.en[key] ??
    key;
  return interpolate(template, params);
}
