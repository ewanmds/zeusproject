import type { Draft } from "./types";
export const strategies = [
  {
    id: "outcome",
    icon: "auto",
    name: "Auto Outcome",
    label: "HIP-4",
    description: "Le scanner choisit le marché admissible et suit le flux en direct.",
    detail: "Sélection automatique",
  },
  {
    id: "bbo",
    icon: "book",
    name: "BBO",
    label: "HIP-3",
    description: "Zeus cote au meilleur bid et au meilleur ask du carnet.",
    detail: "Prix du carnet",
  },
  {
    id: "mid",
    icon: "target",
    name: "MID",
    label: "HIP-3",
    description: "Zeus cote à la distance choisie du prix médian.",
    detail: "Distance réglable",
  },
] as const;
export const nav = [
  { id: "run", name: "Lancer", icon: "run" },
  { id: "sessions", name: "Sessions", icon: "sessions" },
  { id: "performance", name: "Performances", icon: "chart" },
  { id: "account", name: "Compte", icon: "wallet" },
] as const;
export const initial: Draft = {
  strategy: "outcome",
  order_usd: 25,
  position_cap: 200,
  market: "",
  side: null,
  spread_bps: 10,
  offset_bps: 1,
  duration_sec: null,
  target_volume_usd: null,
  stop_loss_usd: 50,
};
export const phaseText: Record<string, string> = {
  connecting: "Démarrage",
  running: "En cours",
  rotating: "Changement de marché",
  recovering: "Vérification",
  stopping: "Arrêt en cours",
  draining: "Vente",
  stopped: "Arrêt confirmé",
  failed: "À vérifier",
};
export const stateText: Record<string, string> = {
  "Bid working": "Bid",
  "Ask working": "Ask",
  "Bid + Ask": "Bid + Ask",
  Reconciling: "Vérification",
  "Waiting for book": "En cours",
  "Waiting to quote": "En cours",
  "Spread too wide": "En cours",
  "Fees exceed spread": "En cours",
  "Fees unavailable": "En cours",
  "Risk paused": "En cours",
  "Inventory stop": "En cours",
  "Draining inventory": "Vente",
  "Exit pending": "Vente",
  "Maker exit pending": "Vente",
  "Building inventory": "En cours",
  Rotating: "En cours",
};
