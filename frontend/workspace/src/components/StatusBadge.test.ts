import { describe, expect, it } from "vitest";
import { sessionStatus } from "./StatusBadge";

const active = {
  running: true,
  stale: false,
  cleanup_confirmed: false,
  state: "Bid working",
  phase: "running",
};
describe("session status evidence", () => {
  it("marks a fresh running quote online", () => {
    expect(sessionStatus(active, false)).toEqual({
      tone: "online",
      label: "Bid",
    });
  });
  it("keeps informational fee states quiet instead of amber", () => {
    for (const state of ["Fees exceed spread", "Fees unavailable", "Risk paused"])
      expect(sessionStatus({ ...active, state }, false).tone).toBe("online");
  });
  it("shows a maker exit as pending maintenance, not a false stop", () => {
    expect(
      sessionStatus(
        { ...active, phase: "draining", exit_requested: true },
        false,
      ),
    ).toEqual({ tone: "maintenance", label: "Sortie maker en cours" });
    expect(
      sessionStatus(
        { ...active, running: false, phase: "failed", stop_reason: "Manual stop" },
        false,
      ),
    ).toEqual({ tone: "degraded", label: "Sortie maker en cours" });
  });
  it("lets workspace and worker staleness override a working bid", () => {
    for (const [session, stale] of [
      [active, true],
      [{ ...active, stale: true }, false],
    ] as const)
      expect(sessionStatus(session, stale)).toEqual({
        tone: "degraded",
        label: "Données retardées",
      });
  });
  it("does not label an unconfirmed stop as stopped", () => {
    expect(
      sessionStatus({ ...active, running: false, phase: "stopped" }, false),
    ).toEqual({ tone: "degraded", label: "Arrêt non confirmé" });
  });
  it("treats a verified stop as neutral, not an error", () => {
    expect(
      sessionStatus(
        {
          ...active,
          running: false,
          cleanup_confirmed: true,
          phase: "stopped",
          state: "",
        },
        false,
      ),
    ).toEqual({ tone: "neutral", label: "Arrêt confirmé" });
  });
  it("never marks a failed session online", () => {
    expect(sessionStatus({ ...active, phase: "failed" }, false).tone).toBe(
      "offline",
    );
  });
  it("distinguishes an unknown phase from a working quote", () => {
    expect(sessionStatus({ ...active, phase: "mystery" }, false)).toEqual({
      tone: "degraded",
      label: "État à vérifier",
    });
  });
});
