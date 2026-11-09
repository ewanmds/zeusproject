import { describe, expect, it } from "vitest";
import { sessionStatus } from "./StatusBadge";

const active = { running: true, stale: false, cleanup_confirmed: false, state: "Bid working", phase: "running" };
describe("session status evidence", () => {
  it("only marks a fresh running session online", () => {
    expect(sessionStatus(active, false)).toEqual({ tone: "online", label: "Bid en place" });
  });
  it("makes fee-blocked and risk-paused sessions amber", () => {
    expect(sessionStatus({ ...active, state: "Fees exceed spread" }, false))
      .toEqual({ tone: "degraded", label: "Frais non couverts" });
    expect(sessionStatus({ ...active, state: "Fees unavailable" }, false).tone).toBe("degraded");
    expect(sessionStatus({ ...active, state: "Risk paused" }, false).tone).toBe("degraded");
  });
  it("lets workspace and worker staleness override a working bid", () => {
    for (const [session, stale] of [[active, true], [{ ...active, stale: true }, false]] as const)
      expect(sessionStatus(session, stale)).toEqual({ tone: "degraded", label: "Données retardées" });
  });
  it("does not label an unconfirmed stop as stopped", () => {
    expect(sessionStatus({ ...active, running: false, phase: "stopped" }, false))
      .toEqual({ tone: "degraded", label: "Arrêt à vérifier" });
  });
  it("treats a verified stop as neutral, not an error", () => {
    expect(sessionStatus({ ...active, running: false, cleanup_confirmed: true, phase: "stopped", state: "" }, false))
      .toEqual({ tone: "neutral", label: "Arrêt confirmé" });
  });
  it("distinguishes recovery from a live quote", () => {
    expect(sessionStatus({ ...active, phase: "recovering", state: "" }, false).tone).toBe("maintenance");
  });
  it("never marks failed or unknown phases online", () => {
    expect(sessionStatus({ ...active, phase: "failed" }, false).tone).toBe("offline");
    expect(sessionStatus({ ...active, phase: "", state: "" }, false))
      .toEqual({ tone: "degraded", label: "État à vérifier" });
  });
});
