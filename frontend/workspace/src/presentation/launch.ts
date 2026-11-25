import type { Draft } from "../types";

export const MIN_ORDER_USD = 10.5;
export const MAX_ORDER_USD = 1_000_000;
export const MAX_POSITION_USD = 10_000_000;
export const BALANCE_WARN_RATIO = 0.8;

const money = (value: number) =>
  new Intl.NumberFormat("fr-FR", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);

/** The server requires order_usd <= position_cap for every strategy. */
export function launchPayload(draft: Draft): Draft {
  const side = draft.side ?? "no";
  if (draft.strategy === "outcome") return { ...draft, side };
  const cap = Number.isFinite(draft.position_cap)
    ? draft.position_cap
    : MIN_ORDER_USD;
  return {
    ...draft,
    side,
    position_cap: Math.max(cap, draft.order_usd, MIN_ORDER_USD),
  };
}

export function orderMax(
  draft: Draft,
  available: number | null | undefined,
): number | null {
  if (available == null || !Number.isFinite(available) || available <= 0) {
    return null;
  }
  const basis =
    draft.strategy === "outcome"
      ? Math.min(available, draft.position_cap)
      : available;
  return Math.floor(basis * 100) / 100;
}

export function quickAmounts(
  draft: Draft,
  available: number | null | undefined,
): { label: string; value: number }[] {
  const max = orderMax(draft, available);
  if (max == null) return [];
  return [
    { label: "25 %", value: Math.floor(max * 0.25 * 100) / 100 },
    { label: "50 %", value: Math.floor(max * 0.5 * 100) / 100 },
    { label: "MAX", value: max },
  ].filter((item) => item.value >= MIN_ORDER_USD);
}

export function amountError(
  draft: Draft,
  available: number | null | undefined,
): string | null {
  const value = draft.order_usd;
  if (!Number.isFinite(value) || value <= 0) return "Saisissez un montant valide.";
  if (value < MIN_ORDER_USD) return "Minimum 10,50 $ par ordre.";
  if (value > MAX_ORDER_USD) return "Maximum 1 000 000 $ par ordre.";
  if (available != null && Number.isFinite(available) && value > available) {
    return `Montant supérieur au solde spot disponible (${money(available)}).`;
  }
  return null;
}

export function amountWarning(
  draft: Draft,
  available: number | null | undefined,
): string | null {
  if (
    available != null &&
    Number.isFinite(available) &&
    available > 0 &&
    draft.order_usd <= available &&
    draft.order_usd > available * BALANCE_WARN_RATIO
  ) {
    return `Ce montant utilise plus de 80 % de votre solde disponible (${money(available)}).`;
  }
  return null;
}

export function capError(draft: Draft): string | null {
  if (draft.strategy !== "outcome") return null;
  const cap = draft.position_cap;
  if (!Number.isFinite(cap) || cap < MIN_ORDER_USD) {
    return "Le plafond de position doit être d'au moins 10,50 $.";
  }
  if (cap > MAX_POSITION_USD) {
    return "Maximum 10 000 000 $ pour le plafond.";
  }
  if (draft.order_usd > cap) {
    return `Le plafond de position (${money(cap)}) doit couvrir au moins un ordre.`;
  }
  return null;
}

export function capGauge(draft: Draft): { percent: number; text: string } {
  const cap = draft.position_cap;
  const percent =
    Number.isFinite(cap) && cap > 0
      ? Math.min(100, Math.max(0, (draft.order_usd / cap) * 100))
      : 0;
  return {
    percent,
    text:
      draft.order_usd > cap
        ? `Plafond dépassé · ${money(draft.order_usd)} sur ${money(cap)}`
        : `${money(draft.order_usd)} sur ${money(cap)} · ${percent.toFixed(1)} % du plafond`,
  };
}

export function formBlockReason(
  draft: Draft,
  available: number | null | undefined,
): "amount" | "cap" | "side" | "market" | null {
  if (amountError(draft, available)) return "amount";
  if (capError(draft)) return "cap";
  if (draft.strategy === "outcome" && !draft.side) return "side";
  if (draft.strategy !== "outcome" && !draft.market.trim()) return "market";
  return null;
}

export function sideLabel(side: Draft["side"]): string {
  return side === "yes" ? "Yes" : side === "no" ? "No" : "Non sélectionné";
}
