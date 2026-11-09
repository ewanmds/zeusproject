import type { Session } from "../types";
export function sessionProgress(session: Session, extraSeconds = 0) {
  const duration = session.config.session_duration_sec ?? session.config.duration_sec;
  const volume = session.config.target_volume_usd;
  const fresh = !session.stale;
  if (duration != null && Number.isFinite(duration) && duration > 0) {
    const elapsed = session.duration_sec + (session.running && fresh && Number.isFinite(extraSeconds) ? Math.max(0, extraSeconds) : 0);
    return { label: "Objectif de durée", percent: fresh ? Math.min(100, Math.max(0, elapsed / duration * 100)) : null,
      target: duration, kind: "duration" as const };
  }
  if (volume != null && Number.isFinite(volume) && volume > 0)
    return { label: "Objectif de volume", percent: fresh && session.volume_usd != null ? Math.min(100, Math.max(0, session.volume_usd / volume * 100)) : null,
      target: volume, kind: "volume" as const };
  return null;
}
export function aggregate(rows: Session[], key: "volume_usd" | "net_pnl" | "fees" | "fills") {
  return rows.every((row) => row.accounting_complete && row[key] != null && Number.isFinite(row[key]))
    ? rows.reduce((total, row) => total + row[key]!, 0) : null;
}
