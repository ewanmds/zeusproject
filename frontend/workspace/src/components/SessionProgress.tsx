import type { Session } from "../types";
import { sessionProgress } from "../presentation/session";
import { clock, usd } from "../data";
// Layro: nolly-studio/halo-progress — clamped determinate values, explicit unknown state.
export function SessionProgress({
  session,
  extraSeconds = 0,
  stale = false,
}: {
  session: Session;
  extraSeconds?: number;
  stale?: boolean;
}) {
  const progress = sessionProgress(
    stale ? { ...session, stale: true } : session,
    extraSeconds,
  );
  if (!progress) return null;
  return (
    <div className="session-progress" data-layro="nolly-studio/halo-progress">
      <label>
        <span>
          {progress.label} ·{" "}
          {progress.kind === "duration"
            ? clock(progress.target)
            : usd(progress.target)}
        </span>
        <span>
          {progress.percent == null
            ? "Données retardées"
            : Math.floor(progress.percent) + " %"}
        </span>
        <progress
          max={100}
          value={progress.percent ?? undefined}
          aria-valuetext={
            progress.percent == null ? "Progression inconnue" : undefined
          }
        />
      </label>
    </div>
  );
}
