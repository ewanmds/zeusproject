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
  it("labels a requested sale without the maker-exit badge", () => {
    expect(
      sessionStatus(
        { ...active, phase: "draining", exit_requested: true },
        false,
      ),
    ).toEqual({ tone: "maintenance", label: "Vente" });
    expect(sessionStatus({ ...active, phase: "draining", state: "Ask working" }, false))
      .toEqual({ tone: "maintenance", label: "Ask" });
    expect(sessionStatus({ ...active, stop_reason: "old low executed $/min" }, false))
      .toEqual({ tone: "online", label: "Bid" });
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
    expect(sessionStatus({ ...active, phase: "failed", exit_requested: true }, false).tone)
      .toBe("offline");
  });
});
