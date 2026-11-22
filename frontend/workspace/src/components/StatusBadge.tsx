// Adapted from @shadcnblocks/status on Layro (MIT).
import type { ReactNode } from "react";
import type { Session } from "../types";
import { phaseText, stateText } from "../config";

export type StatusTone =
  | "online"
  | "offline"
  | "maintenance"
  | "degraded"
  | "neutral";
export function sessionStatus(
  session: Pick<
    Session,
    "running" | "stale" | "cleanup_confirmed" | "state" | "phase"
  > & { exit_requested?: boolean; stop_reason?: string },
  workspaceStale: boolean,
): { tone: StatusTone; label: string } {
  // Only a real publication gap is "delayed"; accounting still settling is not.
  if (workspaceStale || session.stale)
    return { tone: "degraded", label: "Données retardées" };
  const phaseLabel =
    phaseText[session.phase] || session.phase || "État à vérifier";
  const makerExit =
    session.phase === "draining" ||
    session.exit_requested === true ||
    (!!session.stop_reason && session.cleanup_confirmed !== true);
  if (makerExit && session.running)
    return { tone: "online", label: "Sortie maker en cours" };
  if (!session.running && !session.cleanup_confirmed)
    return { tone: "degraded", label: "Arrêt non confirmé" };
  if (["error", "failed"].includes(session.phase))
    return {
      tone: "offline",
      label: session.phase === "error" ? "Erreur" : phaseLabel,
    };
  if (session.running)
    return {
      tone: "online",
      label: stateText[session.state] || phaseLabel,
    };
  return { tone: "neutral", label: stateText[session.state] || phaseLabel };
}

export function StatusBadge({
  tone,
  children,
}: {
  tone: StatusTone;
  children: ReactNode;
}) {
  return (
    <span
      className={`state-badge zeus-status ${tone} ${tone === "online" ? "on" : ""}`}
      data-layro="shadcnblocks/status"
    >
      <span className="status-indicator" aria-hidden="true">
        <span />
      </span>
      <span>{children}</span>
    </span>
  );
}
