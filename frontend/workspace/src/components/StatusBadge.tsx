// Adapted from @shadcnblocks/status on Layro (MIT).
import type { ReactNode } from "react";
import type { Session } from "../types";
import { phaseText, stateText } from "../config";

export type StatusTone = "online" | "offline" | "maintenance" | "degraded" | "neutral";
export function sessionStatus(
  session: Pick<Session, "running" | "stale" | "cleanup_confirmed" | "state" | "phase">,
  workspaceStale: boolean,
): { tone: StatusTone; label: string } {
  if (workspaceStale || session.stale) return { tone: "degraded", label: "Données retardées" };
  if (!session.running && !session.cleanup_confirmed) return { tone: "degraded", label: "Arrêt à vérifier" };
  const phaseLabel = phaseText[session.phase] || session.phase || "État à vérifier";
  if (["error", "failed"].includes(session.phase))
    return { tone: "offline", label: session.phase === "error" ? "Erreur" : phaseLabel };
  if (session.running && ["connecting", "starting", "stopping", "rotating", "reconciling", "recovering"].includes(session.phase))
    return { tone: "maintenance", label: phaseLabel };
  const label = stateText[session.state] || phaseLabel;
  if (session.running && !["running", "active"].includes(session.phase))
    return { tone: "degraded", label };
  return { tone: session.running ? "online" : "neutral", label };
}

export function StatusBadge({ tone, children }: { tone: StatusTone; children: ReactNode }) {
  return (
    <span className={`state-badge zeus-status ${tone} ${tone === "online" ? "on" : ""}`} data-layro="shadcnblocks/status">
      <span className="status-indicator" aria-hidden="true"><span /></span>
      <span>{children}</span>
    </span>
  );
}
