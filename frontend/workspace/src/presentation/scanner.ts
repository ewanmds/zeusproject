export interface ScannerSnapshot {
  ready?: boolean;
  connected?: boolean;
  warming_up?: boolean;
  flow_policy?: string;
  catalog_stale?: boolean;
  observed_sec?: number;
  markets_observed?: number;
  markets_total?: number;
  error?: unknown;
  reason?: unknown;
  thresholds?: Record<string, unknown> | null;
  candidates?: unknown[];
  flow_candidates?: number;
  diagnostics?: Record<string, unknown> | null;
  rejection_counts?: Record<string, unknown> | null;
  rejections?: Record<string, unknown> | null;
  max_spread_bps?: number;
  spread_bps_limit?: number;
  [key: string]: unknown;
}

const ERROR_LIMIT = 220;
const MESSAGE_LIMIT = 420;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function numeric(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value) && value > 0)
    return Math.floor(value);
  const row = asRecord(value);
  if (!row) return undefined;
  for (const key of ["count", "rejected", "value", "total"]) {
    const item = numeric(row[key]);
    if (item !== undefined) return item;
  }
  return undefined;
}

function clamp(value: string, limit: number) {
  const text = value.trim();
  return text.length > limit ? text.slice(0, limit - 1).trimEnd() + "…" : text;
}

/** Backend strings are untrusted display text: collapse control characters and bound length. */
export function sanitizeDiagnostic(value: unknown, limit = ERROR_LIMIT) {
  if (typeof value !== "string" && typeof value !== "number") return "";
  const text = String(value)
    .replace(/\b0x[a-fA-F0-9]{64,}\b/g, "[clé masquée]")
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/-]+/gi, "$1[masqué]")
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text ? clamp(text, limit) : "";
}

function diagnosticSources(scanner: ScannerSnapshot) {
  const base = [
    scanner.diagnostics,
    scanner.rejection_counts,
    scanner.rejections,
    scanner.thresholds,
  ];
  const sources = base.map(asRecord).filter(Boolean) as Record<string, unknown>[];
  for (const source of [...sources]) {
    for (const key of ["rejections", "counts", "rejected", "reasons"]) {
      const nested = asRecord(source[key]);
      if (nested && !sources.includes(nested)) sources.push(nested);
    }
  }
  return sources;
}

function count(source: Record<string, unknown>[], keys: string[]) {
  for (const row of source)
    for (const key of keys) {
      const value = numeric(row[key]);
      if (value !== undefined) return value;
    }
  return undefined;
}

function setting(
  scanner: ScannerSnapshot,
  sources: Record<string, unknown>[],
  keys: string[],
  fallback?: number,
) {
  for (const key of keys) {
    const value = numeric(scanner[key]);
    if (value !== undefined) return value;
  }
  return count(sources, keys) ?? (numeric(fallback) || undefined);
}

function marketCount(value: number) {
  return `${value} marché${value > 1 ? "s" : ""}`;
}

function spreadLimitText(limit: number | undefined) {
  return limit !== undefined ? ` (limite ${limit} bps)` : "";
}

/**
 * Returns one bounded, operator-readable scanner explanation, or null as soon
 * as fresh eligible data arrives. Fees stay informational and are never shown
 * as a launch blocker here.
 */
export function describeScannerDiagnostic(
  scanner: ScannerSnapshot | null | undefined,
  maxSpreadBps?: number,
): string | null {
  if (!scanner) return null;
  const candidates = Array.isArray(scanner.candidates)
    ? scanner.candidates
    : [];
  if (candidates.length || scanner.ready === true) return null;

  const sources = diagnosticSources(scanner);
  const parts: string[] = [];
  const reason = sanitizeDiagnostic(scanner.reason);
  if (reason) parts.push(reason);
  const error = sanitizeDiagnostic(scanner.error);
  if (error && !reason.includes(error)) parts.push(`Cause signalée : ${error}`);
  const detail = sanitizeDiagnostic(
    scanner.diagnostics?.message ??
      scanner.diagnostics?.reason ??
      scanner.diagnostics?.error,
  );
  if (detail && detail !== error && !reason.includes(detail)) parts.push(detail);

  const spread = count(sources, [
    "spread",
    "spread_bps",
    "spread_rejected",
    "spread_rejections",
    "too_wide",
    "over_spread",
  ]);
  const depth = count(sources, [
    "depth",
    "depth_rejected",
    "insufficient_depth",
    "shallow",
    "shallow_book",
    "book_depth",
  ]);
  const stale = count(sources, [
    "stale",
    "stale_book",
    "book_stale",
    "stale_rejections",
    "stale_quotes",
  ]);
  const funds = count(sources, [
    "funds",
    "funds_rejected",
    "insufficient_funds",
    "approval",
    "approval_required",
    "approval_rejected",
    "agent",
    "agent_required",
  ]);
  const flow = count(sources, [
    "flow",
    "no_flow",
    "ineligible_flow",
    "no_eligible_flow",
    "flow_rejected",
  ]);
  const spreadLimit = setting(
    scanner,
    sources,
    ["spread_bps_limit", "max_spread_bps", "spread_limit_bps"],
    maxSpreadBps,
  );
  const explained = (...words: string[]) =>
    !!reason && words.some((word) => reason.toLowerCase().includes(word));

  if (spread !== undefined && !explained("spread", "bps"))
    parts.push(
      `${marketCount(spread)} écarté${spread > 1 ? "s" : ""} : spread au-delà de la limite${spreadLimitText(spreadLimit)}.`,
    );
  if (depth !== undefined && !explained("profondeur", "depth", "carnet"))
    parts.push(
      `${marketCount(depth)} écarté${depth > 1 ? "s" : ""} : profondeur insuffisante.`,
    );
  if (
    stale !== undefined &&
    !explained("périmé", "perime", "stale", "catalogue")
  )
    parts.push(
      `${marketCount(stale)} écarté${stale > 1 ? "s" : ""} : carnet périmé.`,
    );
  if (
    funds !== undefined &&
    !explained("fonds", "fund", "approbation", "approval")
  )
    parts.push(
      `${marketCount(funds)} écarté${funds > 1 ? "s" : ""} : fonds ou approbation insuffisants.`,
    );
  if (scanner.flow_policy !== "informational" && flow !== undefined && !explained("flux", "flow"))
    parts.push(
      `${marketCount(flow)} écarté${flow > 1 ? "s" : ""} : flux insuffisant.`,
    );

  if (!parts.length) {
    const observed = numeric(scanner.observed_sec);
    const observedMarkets = numeric(scanner.markets_observed);
    const totalMarkets = numeric(scanner.markets_total);
    if (scanner.connected === false) parts.push("Scanner non connecté au flux public.");
    else if (
      scanner.warming_up ||
      (scanner.flow_policy !== "informational" && observed !== undefined && observed < 60) ||
      (scanner.flow_policy !== "informational" && observedMarkets !== undefined &&
        totalMarkets !== undefined &&
        observedMarkets < totalMarkets)
    ) {
      const duration =
        observed !== undefined ? `${Math.min(60, observed)}/60 s` : "en cours";
      const markets =
        observedMarkets !== undefined && totalMarkets !== undefined
          ? `, ${observedMarkets}/${totalMarkets} marchés`
          : "";
      parts.push(`Observation du flux ${duration}${markets}.`);
    } else if (scanner.catalog_stale)
      parts.push("Catalogue des marchés expiré ; actualisation en cours.");
    else {
      const flowCandidates = numeric(scanner.flow_candidates);
      const prefix =
        flowCandidates !== undefined
          ? `${marketCount(flowCandidates)} avec flux observé, aucun éligible`
          : "Aucun marché éligible";
      parts.push(`${prefix} avec ces paramètres${spreadLimitText(spreadLimit)}.`);
    }
  }

  if (!parts.length)
    parts.push(
      `Aucun marché éligible avec ces paramètres${spreadLimitText(spreadLimit)}.`,
    );
  return clamp(parts.join(" · "), MESSAGE_LIMIT);
}
