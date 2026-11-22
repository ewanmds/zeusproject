import type { Session, Snapshot } from "../types";

export type StopStatus = "confirmed" | "pending" | "unknown";

export interface StopResolution {
  status: StopStatus;
  confirmed: boolean;
  message: string;
  session: Session | null;
}

interface GroupedSession extends Session {
  group_id?: string;
}

const PENDING_MAKER_EXIT = "Sortie maker en cours.";
const PENDING_STOP = "Arrêt en cours, nettoyage non confirmé.";
const UNKNOWN_TARGET =
  "Arrêt non vérifié : la session cible est introuvable dans l’état reçu.";

export function targetSessions(
  snapshot: Snapshot | null | undefined,
  target: string,
): Session[] {
  if (!snapshot || !Array.isArray(snapshot.sessions) || !target) return [];
  return snapshot.sessions.filter((session) => {
    const grouped = session as GroupedSession;
    const group =
      grouped.group_id ??
      (session.config as { auto_group_id?: string } | undefined)?.auto_group_id;
    return session.id === target || group === target;
  });
}

function isMakerOutcome(session: Session) {
  return session.kind === "outcome" || session.strategy === "outcome";
}

/**
 * Stop (and resume-exit) commands return the immediate workspace snapshot.
 * A command response only proves cleanup when every matching row is stopped
 * and explicitly confirmed. A missing row is never treated as confirmation.
 */
export function resolveStopSnapshot(
  snapshot: Snapshot | null | undefined,
  target: string,
): StopResolution {
  const matches = targetSessions(snapshot, target);
  if (!matches.length)
    return {
      status: "unknown",
      confirmed: false,
      message: UNKNOWN_TARGET,
      session: null,
    };

  const unresolved = matches.filter(
    (session) => session.running || session.cleanup_confirmed !== true,
  );
  if (unresolved.length) {
    const outcome = matches.some(isMakerOutcome);
    return {
      status: "pending",
      confirmed: false,
      message: outcome ? PENDING_MAKER_EXIT : PENDING_STOP,
      session: unresolved[0],
    };
  }

  return {
    status: "confirmed",
    confirmed: true,
    message: "Arrêt confirmé.",
    session: matches[0],
  };
}

export function isMakerExitPending(
  session: Pick<
    Session,
    "running" | "cleanup_confirmed" | "phase" | "exit_requested" | "stop_reason"
  >,
) {
  return (
    session.exit_requested === true ||
    session.phase === "draining" ||
    (!!session.stop_reason && session.cleanup_confirmed !== true)
  );
}

/**
 * Resume is deliberately limited to a stopped Outcome session whose cleanup
 * is still unconfirmed. Grid/perp sessions never get a maker-exit action.
 */
export function canResumeMakerExit(
  session: Pick<
    Session,
    "kind" | "strategy" | "running" | "cleanup_confirmed"
  >,
) {
  return (
    session.kind === "outcome" &&
    session.strategy === "outcome" &&
    !session.running &&
    session.cleanup_confirmed !== true
  );
}

export function resumeExitMessage(
  snapshot: Snapshot | null | undefined,
  target: string,
): StopResolution {
  const matches = targetSessions(snapshot, target);
  if (!matches.length)
    return {
      status: "unknown",
      confirmed: false,
      message:
        "Reprise non vérifiée : la session cible est introuvable dans l’état reçu.",
      session: null,
    };

  if (matches.every((session) => !session.running && session.cleanup_confirmed))
    return {
      status: "confirmed",
      confirmed: true,
      message:
        "Cette session est déjà arrêtée et vérifiée ; aucune sortie maker à reprendre.",
      session: matches[0],
    };

  const pending = matches.find(
    (session) => session.running || session.cleanup_confirmed !== true,
  )!;
  if (pending.running)
    return {
      status: "pending",
      confirmed: false,
      message: "Sortie maker en cours.",
      session: pending,
    };

  return {
    status: "pending",
    confirmed: false,
    message:
      "Reprise demandée. La sortie maker reste en attente de vérification.",
    session: pending,
  };
}
