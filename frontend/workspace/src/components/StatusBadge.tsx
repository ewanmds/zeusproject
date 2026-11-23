// Adapted from @shadcnblocks/status on Layro (MIT).
import type { ReactNode } from "react";
import type { Session } from "../types";
import { phaseText, stateText } from "../config";
import { isMakerExitPending } from "../presentation/stop";

export type StatusTone =
  | "online"
  | "offline"
  | "maintenance"
  | "degraded"
  | "neutral";

type StatusSession = Pick<
  Session,
  "running" | "stale" | "cleanup_confirmed" | "state" | "phase"
> &
  Partial<Pick<Session, "exit_requested" | "stop_reason">>;

const MAINTENANCE = [
  "connecting",
  "starting",
  "stopping",
  "draining",
  "rotating",
  "reconciling",
  "recovering",
];

export function sessionStatus(
  session: StatusSession,
  workspaceStale: boolean,
): { tone: StatusTone; label: string } {
  // Only a real publication gap is "delayed"; accounting still settling is not.
  if (workspaceStale || session.stale)
    return { tone: "degraded", label: "Données retardées" };

  const knownPhase = Object.prototype.hasOwnProperty.call(
    phaseText,
    session.phase,
  );
  const phaseLabel = knownPhase ? phaseText[session.phase] : "État à vérifier";
  const makerExit = isMakerExitPending(session);

  if (makerExit)
    return session.running
      ? { tone: "maintenance", label: "Sortie maker en cours" }
      : { tone: "degraded", label: "Sortie maker en cours" };

  if (!session.running && session.cleanup_confirmed !== true)
    return { tone: "degraded", label: "Arrêt non confirmé" };

  if (["error", "failed"].includes(session.phase))
    return {
      tone: "offline",
      label: session.phase === "error" ? "Erreur" : phaseLabel,
    };

  if (session.running) {
    if (!knownPhase && !["running", "active"].includes(session.phase))
      return { tone: "degraded", label: "État à vérifier" };
    if (MAINTENANCE.includes(session.phase))
      return { tone: "maintenance", label: phaseLabel };
    return {
      tone: "online",
      label: stateText[session.state] || phaseLabel,
    };
  }

  return {
    tone: "neutral",
    label: stateText[session.state] || phaseLabel,
  };
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
