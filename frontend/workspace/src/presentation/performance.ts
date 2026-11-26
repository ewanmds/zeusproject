import type { Session } from "../types";

export type Venue = "all" | "hip3" | "hip4";
export type ResolvedVenue = Exclude<Venue, "all">;
export type MarketVenue = ResolvedVenue | "mixed";
export type NumericField = "volume_usd" | "net_pnl" | "fees" | "fills";

const HIP4: ResolvedVenue = "hip4";
const HIP3: ResolvedVenue = "hip3";

function finite(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function known(session: Session, field: NumericField) {
  return session.accounting_complete && finite(session[field]);
}

export function venueOf(session: Pick<Session, "strategy">): ResolvedVenue {
  return session.strategy === "outcome" ? HIP4 : HIP3;
}

export function venueLabel(venue: Venue | MarketVenue) {
  if (venue === "all") return "Tous";
  if (venue === "mixed") return "HIP-3 + HIP-4";
  return venue === HIP4 ? "HIP-4" : "HIP-3";
}

export function formatPercent(value: number | null) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(value >= 10 ? 0 : 1).replace(".", ",")} %`;
}

export function formatFactor(value: number | null) {
  if (value == null) return "—";
  if (!Number.isFinite(value)) return "∞";
  return value.toFixed(2).replace(".", ",");
}

export function filterByVenue(sessions: Session[], venue: Venue) {
  return venue === "all"
    ? sessions
    : sessions.filter((session) => venueOf(session) === venue);
}

/** Sum a field only when every row proves a complete accounting value. */
export function sumKnown(rows: Session[], field: NumericField) {
  if (!rows.length) return 0;
  return rows.every((row) => known(row, field))
    ? rows.reduce((total, row) => total + (row[field] as number), 0)
    : null;
}

export interface PerformanceFacts {
  sessions: number;
  closed: number;
  running: number;
  volume: number | null;
  net: number | null;
  fees: number | null;
  fills: number | null;
  costPerMillion: number | null;
  winRate: number | null;
  wins: number;
  losses: number;
  flat: number;
  profitFactor: number | null;
  avgDuration: number | null;
  avgNet: number | null;
  bestMarket: { name: string; net: number } | null;
}

export function performanceFacts(sessions: Session[]): PerformanceFacts {
  const closed = sessions.filter(
    (session) => !session.running && known(session, "net_pnl"),
  );
  const wins = closed.filter((session) => (session.net_pnl as number) > 0);
  const losses = closed.filter((session) => (session.net_pnl as number) < 0);
  const flat = closed.filter((session) => session.net_pnl === 0);
  const volume = sumKnown(sessions, "volume_usd");
  const net = sumKnown(sessions, "net_pnl");
  const fees = sumKnown(sessions, "fees");
  const fills = sumKnown(sessions, "fills");
  const grossWin = wins.reduce((total, s) => total + (s.net_pnl as number), 0);
  const grossLoss = Math.abs(
    losses.reduce((total, s) => total + (s.net_pnl as number), 0),
  );
  const durations = closed
    .map((session) => session.duration_sec)
    .filter(finite);
  const markets = new Map<string, { name: string; net: number }>();
  for (const session of closed) {
    const key = session.market || session.name;
    const current = markets.get(key);
    const pnl = session.net_pnl as number;
    if (current) current.net += pnl;
    else markets.set(key, { name: session.name || key, net: pnl });
  }
  const bestMarket =
    [...markets.values()].sort((a, b) => b.net - a.net)[0] ?? null;
  return {
    sessions: sessions.length,
    closed: closed.length,
    running: sessions.filter((session) => session.running).length,
    volume,
    net,
    fees,
    fills,
    costPerMillion:
      volume != null && volume > 0 && net != null ? (-net / volume) * 1e6 : null,
    winRate: closed.length ? (wins.length / closed.length) * 100 : null,
    wins: wins.length,
    losses: losses.length,
    flat: flat.length,
    profitFactor:
      grossLoss > 0
        ? grossWin / grossLoss
        : grossWin > 0
          ? Number.POSITIVE_INFINITY
          : null,
    avgDuration: durations.length
      ? durations.reduce((total, value) => total + value, 0) / durations.length
      : null,
    avgNet: closed.length
      ? closed.reduce((total, s) => total + (s.net_pnl as number), 0) /
        closed.length
      : null,
    bestMarket,
  };
}

export interface EquityPoint {
  at: number;
  cumulative: number;
  net: number;
  market: string;
  name: string;
}

export function equityCurve(sessions: Session[]) {
  const ordered = sessions
    .slice()
    .sort((a, b) => a.started - b.started);
  let cumulative = 0;
  const points: EquityPoint[] = [];
  for (const session of ordered) {
    if (!known(session, "net_pnl")) continue;
    cumulative += session.net_pnl as number;
    points.push({
      at: session.started * 1000,
      cumulative,
      net: session.net_pnl as number,
      market: session.market,
      name: session.name,
    });
  }
  if (points.length) {
    // Start the curve at zero so the first session reads as a gain/loss, not
    // as an initial capital.
    points.unshift({
      at: points[0].at - 1000,
      cumulative: 0,
      net: 0,
      market: "",
      name: "Départ",
    });
  }
  return {
    points,
    incomplete: ordered.some((session) => !known(session, "net_pnl")),
  };
}

export interface BucketPoint {
  key: string;
  label: string;
  at: number;
  volume: number | null;
  net: number | null;
  fills: number | null;
  sessions: number;
}

const dayLabel = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit",
  month: "short",
});
const timeLabel = new Intl.DateTimeFormat("fr-FR", {
  hour: "2-digit",
  minute: "2-digit",
});

function dayKey(date: Date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

export function bucketPerformance(
  sessions: Session[],
  periodDays: number,
): {
  points: BucketPoint[];
  granularity: "session" | "day";
  incomplete: boolean;
} {
  const ordered = sessions.slice().sort((a, b) => a.started - b.started);
  const incomplete = ordered.some((session) => !session.accounting_complete);
  if (periodDays <= 1) {
    return {
      granularity: "session",
      incomplete,
      points: ordered.map((session) => ({
        key: session.id,
        label: timeLabel.format(new Date(session.started * 1000)),
        at: session.started * 1000,
        volume: known(session, "volume_usd") ? session.volume_usd : null,
        net: known(session, "net_pnl") ? session.net_pnl : null,
        fills: known(session, "fills") ? session.fills : null,
        sessions: 1,
      })),
    };
  }
  const buckets = new Map<string, Session[]>();
  for (const session of ordered) {
    const date = new Date(session.started * 1000);
    const key = dayKey(date);
    const rows = buckets.get(key);
    if (rows) rows.push(session);
    else buckets.set(key, [session]);
  }
  const points = [...buckets.entries()]
    .map(([key, rows]) => {
      const first = rows[0];
      const date = new Date(first.started * 1000);
      const midnight = new Date(
        date.getFullYear(),
        date.getMonth(),
        date.getDate(),
      );
      return {
        key,
        label: dayLabel.format(midnight),
        at: midnight.getTime(),
        volume: sumKnown(rows, "volume_usd"),
        net: sumKnown(rows, "net_pnl"),
        fills: sumKnown(rows, "fills"),
        sessions: rows.length,
      };
    })
    .sort((a, b) => a.at - b.at);
  return { points, granularity: "day", incomplete };
}

export interface MarketRow {
  key: string;
  name: string;
  venue: MarketVenue;
  sessions: number;
  volume: number | null;
  net: number | null;
  fills: number | null;
  winRate: number | null;
}

export function marketBreakdown(
  sessions: Session[],
  limit = 6,
): MarketRow[] {
  const groups = new Map<string, Session[]>();
  for (const session of sessions) {
    const key = session.market || session.name;
    const rows = groups.get(key);
    if (rows) rows.push(session);
    else groups.set(key, [session]);
  }
  const rows = [...groups.entries()].map(([key, members]) => {
    const latest = members.reduce((current, session) =>
      session.started > current.started ? session : current,
    );
    const venues = new Set(members.map(venueOf));
    const closed = members.filter(
      (session) => !session.running && known(session, "net_pnl"),
    );
    const wins = closed.filter((session) => (session.net_pnl as number) > 0);
    return {
      key,
      name: latest.name || key,
      venue:
        venues.size === 1
          ? ([...venues][0] as ResolvedVenue)
          : ("mixed" as const),
      sessions: members.length,
      volume: sumKnown(members, "volume_usd"),
      net: sumKnown(members, "net_pnl"),
      fills: sumKnown(members, "fills"),
      winRate: closed.length ? (wins.length / closed.length) * 100 : null,
    };
  });
  return rows
    .sort((a, b) => {
      const volume = (b.volume ?? -1) - (a.volume ?? -1);
      if (volume) return volume;
      return Math.abs(b.net ?? 0) - Math.abs(a.net ?? 0);
    })
    .slice(0, limit);
}

export function outcomeBreakdown(facts: PerformanceFacts) {
  return [
    { key: "wins" as const, label: "Gains", value: facts.wins },
    { key: "losses" as const, label: "Pertes", value: facts.losses },
    { key: "flat" as const, label: "Neutres", value: facts.flat },
    { key: "running" as const, label: "En cours", value: facts.running },
  ].filter((row) => row.value > 0);
}
