import type { Draft } from "./types";
export const strategies = [
  {
    id: "outcome",
    icon: "auto",
    name: "Auto Outcome",
    label: "HIP-4",
    description: "Le scanner choisit le marché.",
    detail: "Sélection automatique",
  },
  {
    id: "bbo",
    icon: "book",
    name: "BBO",
    label: "HIP-3",
    description: "Au meilleur bid et ask.",
    detail: "Prix du carnet",
  },
  {
    id: "mid",
    icon: "target",
    name: "MID",
    label: "HIP-3",
    description: "Votre distance au prix médian.",
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
  side: "no",
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
  stopped: "Arrêt confirmé",
  failed: "À vérifier",
};
export const stateText: Record<string, string> = {
  "Bid working": "Bid en place",
  "Ask working": "Ask en place",
  "Bid + Ask": "Bid + Ask",
  Reconciling: "Réconciliation",
  "Waiting for book": "Attente du carnet",
  "Waiting to quote": "Attente de cotation",
  "Spread too wide": "Spread trop large",
  "Inventory stop": "Stop inventaire",
  "Draining inventory": "Sortie de position",
  "Building inventory": "Inventaire en cours",
  Rotating: "Changement de marché",
};
