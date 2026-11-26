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
  const phaseLabel =
    phaseText[session.phase] || session.phase || "État à vérifier";
  // A terminal session keeps its terminal label even when the live stream is
  // briefly delayed; only a running quote can be "Données retardées".
  if (!session.running) {
    if (!session.cleanup_confirmed)
      return { tone: "degraded", label: "Arrêt non confirmé" };
    if (["error", "failed"].includes(session.phase))
      return {
        tone: "offline",
        label: session.phase === "error" ? "Erreur" : phaseLabel,
      };
    return { tone: "neutral", label: "Arrêt confirmé" };
  }
  // Only a real publication gap is "delayed"; accounting still settling is not.
  if (workspaceStale || session.stale)
    return { tone: "degraded", label: "Données retardées" };
  if (["error", "failed"].includes(session.phase))
    return {
      tone: "offline",
      label: session.phase === "error" ? "Erreur" : phaseLabel,
    };
  // A live worker can be stuck in exchange reconciliation while its monitor
  // keeps the stats fresh. Do not label that as a healthy "En cours".
  if (session.running && (session.phase === "recovering" || session.state === "Reconciling"))
    return { tone: "degraded", label: "Vérification" };
  const makerExit =
    session.phase === "draining" || session.exit_requested === true;
  if (makerExit && session.running)
    return {
      tone: "maintenance",
      label: session.state === "Ask working" ? "Ask" : "Vente",
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
