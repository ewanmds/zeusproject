import type { Session } from "../types";

export type Venue = "all" | "hip3" | "hip4";
export type NumericField = "volume_usd" | "net_pnl" | "fees" | "fills";

function finite(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Sum what is available; a missing field never hides the other rows. */
export function sumAvailable(rows: Session[], field: NumericField) {
  const knownField = {
    volume_usd: "known_volume_usd",
    net_pnl: "known_net_pnl",
    fees: "known_fees",
    fills: "known_fills",
  } as const;
  return rows.reduce((total, session) => {
    const value = session[knownField[field]] ?? session[field];
    return total + (finite(value) ? value : 0);
  }, 0);
}

export function venueOf(session: Pick<Session, "strategy">): Exclude<Venue, "all"> {
  return session.strategy === "outcome" ? "hip4" : "hip3";
}

export function filterByVenue(sessions: Session[], venue: Venue) {
  return venue === "all"
    ? sessions
    : sessions.filter((session) => venueOf(session) === venue);
}

export interface VolumeBucket {
  key: string;
  label: string;
  at: number;
  hip4: number;
  hip3: number;
  total: number;
  sessions: number;
}

const dayLabel = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit",
  month: "short",
});

function bucketKey(date: Date, hourly: boolean) {
  const day = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  return hourly ? `${day}-${date.getHours()}` : day;
}

function venueVolume(rows: Session[], venue: Exclude<Venue, "all">) {
  return sumAvailable(
    rows.filter((session) => venueOf(session) === venue),
    "volume_usd",
  );
}

/**
 * Volume per hour for 24 h and per day beyond that.
 * A bucket exposes HIP-4 and HIP-3 separately so the chart can stack them.
 */
export function volumeByPeriod(sessions: Session[], periodDays: number) {
  const hourly = periodDays <= 1;
  const groups = new Map<string, Session[]>();
  for (const session of sessions) {
    const date = new Date(session.started * 1000);
    const key = bucketKey(date, hourly);
    const rows = groups.get(key);
    if (rows) rows.push(session);
    else groups.set(key, [session]);
  }
  const points = [...groups.entries()]
    .map(([key, rows]) => {
      const date = new Date(rows[0].started * 1000);
      const start = hourly
        ? new Date(
            date.getFullYear(),
            date.getMonth(),
            date.getDate(),
            date.getHours(),
          )
        : new Date(date.getFullYear(), date.getMonth(), date.getDate());
      const hip4 = venueVolume(rows, "hip4");
      const hip3 = venueVolume(rows, "hip3");
      return {
        key,
        label: hourly
          ? `${String(date.getHours()).padStart(2, "0")}:00`
          : dayLabel.format(start),
        at: start.getTime(),
        hip4,
        hip3,
        total: hip4 + hip3,
        sessions: rows.length,
      };
    })
    .sort((a, b) => a.at - b.at);
  return {
    points,
    granularity: hourly ? ("hour" as const) : ("day" as const),
  };
}
