import { describe, expect, it } from "vitest";
import type { Session } from "../types";
import { aggregate, sessionProgress, sessionVisible, activeSessionCount } from "./session";
const session = {
  running: true,
  stale: false,
  duration_sec: 60,
  volume_usd: 5,
  net_pnl: -0.1,
  fees: 0.02,
  fills: 1,
  accounting_complete: true,
  config: {},
} as Session;
describe("real session projections", () => {
  it("never shows a stopped session in the active tab", () => {
    expect(sessionVisible({ ...session, running: true }, "active")).toBe(true);
    expect(sessionVisible({ ...session, running: false }, "active")).toBe(false);
    expect(sessionVisible({ ...session, running: false }, "closed")).toBe(true);
    expect(sessionVisible({ ...session, running: true }, "closed")).toBe(false);
    expect(sessionVisible({ ...session, running: false }, "all")).toBe(true);
  });
  it("drops a pending stop from the live badge immediately", () => {
    const rows = [
      { id: "a", running: true },
      { id: "b", running: true },
      { id: "c", running: false },
    ];
    expect(activeSessionCount(rows, {})).toBe(2);
    expect(activeSessionCount(rows, { a: 1 })).toBe(1);
    expect(activeSessionCount(rows, { a: 1, b: 1 })).toBe(0);
  });
  it("does not invent a target for a manual session", () =>
    expect(sessionProgress(session)).toBeNull());
  it("uses the launched duration projection before a draft alias", () => {
    expect(
      sessionProgress(
        {
          ...session,
          config: { session_duration_sec: 200, duration_sec: 100 },
        },
        40,
      )?.percent,
    ).toBe(50);
  });
  it("stops extrapolating a stopped timer and rejects unavailable progress", () => {
    expect(
      sessionProgress(
        { ...session, running: false, config: { session_duration_sec: 100 } },
        40,
      )?.percent,
    ).toBe(60);
    expect(
      sessionProgress(
        { ...session, stale: true, config: { session_duration_sec: 100 } },
        40,
      )?.percent,
    ).toBeNull();
  });
  it("clamps actual volume, and never turns unknown volume into zero", () => {
    expect(
      sessionProgress({ ...session, config: { target_volume_usd: 4 } })
        ?.percent,
    ).toBe(100);
    expect(
      sessionProgress({
        ...session,
        volume_usd: null,
        config: { target_volume_usd: 10 },
      })?.percent,
    ).toBeNull();
  });
  it("does not display invalid or infinite targets", () => {
    expect(
      sessionProgress({ ...session, config: { target_volume_usd: Infinity } }),
    ).toBeNull();
  });
  it("propagates incomplete accounting instead of publishing partial totals", () => {
    expect(
      aggregate(
        [session, { ...session, accounting_complete: false }],
        "volume_usd",
      ),
    ).toBeNull();
    expect(
      aggregate([session, { ...session, volume_usd: null }], "volume_usd"),
    ).toBeNull();
    expect(aggregate([session, session], "volume_usd")).toBe(10);
  });
});
