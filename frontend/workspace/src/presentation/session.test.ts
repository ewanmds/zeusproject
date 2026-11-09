import { describe, expect, it } from "vitest";
import type { Session } from "../types";
import { aggregate, sessionProgress } from "./session";
const session = { running: true, stale: false, duration_sec: 60, volume_usd: 5, net_pnl: -.1,
  fees: .02, fills: 1, accounting_complete: true, config: {} } as Session;
describe("real session projections", () => {
  it("does not invent a target for a manual session", () => expect(sessionProgress(session)).toBeNull());
  it("uses the launched duration projection before a draft alias", () => {
    expect(sessionProgress({ ...session, config: { session_duration_sec: 200, duration_sec: 100 } }, 40)?.percent).toBe(50);
  });
  it("stops extrapolating a stopped timer and rejects unavailable progress", () => {
    expect(sessionProgress({ ...session, running: false, config: { session_duration_sec: 100 } }, 40)?.percent).toBe(60);
    expect(sessionProgress({ ...session, stale: true, config: { session_duration_sec: 100 } }, 40)?.percent).toBeNull();
  });
  it("clamps actual volume, and never turns unknown volume into zero", () => {
    expect(sessionProgress({ ...session, config: { target_volume_usd: 4 } })?.percent).toBe(100);
    expect(sessionProgress({ ...session, volume_usd: null, config: { target_volume_usd: 10 } })?.percent).toBeNull();
  });
  it("does not display invalid or infinite targets", () => {
    expect(sessionProgress({ ...session, config: { target_volume_usd: Infinity } })).toBeNull();
  });
  it("propagates incomplete accounting instead of publishing partial totals", () => {
    expect(aggregate([session, { ...session, accounting_complete: false }], "volume_usd")).toBeNull();
    expect(aggregate([session, { ...session, volume_usd: null }], "volume_usd")).toBeNull();
    expect(aggregate([session, session], "volume_usd")).toBe(10);
  });
});
