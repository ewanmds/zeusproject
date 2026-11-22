import { describe, expect, it } from "vitest";
import {
  canResumeMakerExit,
  canResumeTrading,
  isMakerExitPending,
  resolveStopSnapshot,
  resumeExitMessage,
  resumeTradingMessage,
} from "./stop";
import type { Session, Snapshot } from "../types";

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    kind: "outcome",
    strategy: "outcome",
    name: "Test",
    phase: "running",
    running: true,
    cleanup_confirmed: false,
    stale: false,
    accounting_complete: true,
    started: 0,
    duration_sec: 0,
    volume_usd: 0,
    net_pnl: 0,
    fees: 0,
    fills: 0,
    cost_per_1m: null,
    position_usd: 0,
    state: "",
    reason: "",
    rotations: 0,
    config: {},
    ...overrides,
  } as Session;
}

function snapshot(sessions: Session[]): Snapshot {
  return { sessions } as Snapshot;
}

describe("stop evidence", () => {
  it("never confirms before cleanup is explicit", () => {
    const result = resolveStopSnapshot(
      snapshot([session({ running: true, cleanup_confirmed: false })]),
      "s1",
    );
    expect(result.status).toBe("pending");
    expect(result.message).toBe("Sortie maker en cours.");
    expect(result.confirmed).toBe(false);
  });

  it("confirms only when every matching row is stopped and verified", () => {
    const result = resolveStopSnapshot(
      snapshot([session({ running: false, cleanup_confirmed: true })]),
      "s1",
    );
    expect(result).toMatchObject({ status: "confirmed", confirmed: true });
    expect(result.message).toBe("Arrêt confirmé.");
  });

  it("never confirms a group while one leg is unconfirmed", () => {
    const confirmed = session({
      id: "leg-a",
      running: false,
      cleanup_confirmed: true,
      config: { auto_group_id: "group" } as Session["config"],
    });
    const pendingLeg = session({
      id: "leg-b",
      running: false,
      cleanup_confirmed: false,
      config: { auto_group_id: "group" } as Session["config"],
    });
    const result = resolveStopSnapshot(
      snapshot([confirmed, pendingLeg]),
      "group",
    );
    expect(result.status).toBe("pending");
    expect(result.session?.id).toBe("leg-b");
  });

  it("explains a forced stop that left a residual position", () => {
    const result = resolveStopSnapshot(
      snapshot([
        session({
          running: false,
          cleanup_confirmed: false,
          phase: "failed",
        }),
      ]),
      "s1",
    );
    expect(result.status).toBe("pending");
    expect(result.message).toContain("Arrêt non confirmé");
  });

  it("keeps a grouped unconfirmed leg visible", () => {
    const group = session({
      id: "group",
      running: false,
      cleanup_confirmed: false,
      config: { auto_group_id: "group" } as Session["config"],
    });
    const result = resolveStopSnapshot(snapshot([group]), "group");
    expect(result.status).toBe("pending");
    expect(result.session?.id).toBe("group");
  });

  it("treats a missing target as unverified, never confirmed", () => {
    const result = resolveStopSnapshot(snapshot([]), "s1");
    expect(result.status).toBe("unknown");
    expect(result.confirmed).toBe(false);
  });
});

describe("maker exit presentation", () => {
  it("recognises a draining session", () => {
    expect(
      isMakerExitPending(
        session({
          running: true,
          phase: "draining",
          exit_requested: true,
        }),
      ),
    ).toBe(true);
  });

  it("offers resume only to an unconfirmed stopped Outcome session", () => {
    expect(
      canResumeMakerExit(
        session({ running: false, cleanup_confirmed: false }),
      ),
    ).toBe(true);
    expect(
      canResumeMakerExit(session({ running: false, cleanup_confirmed: true })),
    ).toBe(false);
    expect(
      canResumeMakerExit(
        session({
          kind: "grid",
          strategy: "bbo",
          running: false,
          cleanup_confirmed: false,
        }),
      ),
    ).toBe(false);
  });

  it("resumes trading only when the stop was not a manual Stop", () => {
    expect(
      canResumeTrading(
        session({ running: false, cleanup_confirmed: false, stop_reason: "" }),
      ),
    ).toBe(true);
    expect(
      canResumeTrading(
        session({
          running: false,
          cleanup_confirmed: false,
          stop_reason: "Manual stop: maker exit",
        }),
      ),
    ).toBe(false);
  });

  it("describes a trading resume request without claiming closure", () => {
    const result = resumeTradingMessage(
      snapshot([session({ running: false, cleanup_confirmed: false })]),
      "s1",
    );
    expect(result.status).toBe("pending");
    expect(result.confirmed).toBe(false);
    expect(result.message).toContain("Reprise du trading");
  });

  it("describes a resume request without claiming closure", () => {
    const result = resumeExitMessage(
      snapshot([session({ running: false, cleanup_confirmed: false })]),
      "s1",
    );
    expect(result.status).toBe("pending");
    expect(result.confirmed).toBe(false);
  });
});
