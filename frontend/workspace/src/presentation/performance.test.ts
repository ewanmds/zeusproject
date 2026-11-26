import { describe, expect, it } from "vitest";
import type { Session } from "../types";
import { filterByVenue, venueOf, volumeByPeriod } from "./performance";

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    kind: "outcome",
    strategy: "outcome",
    name: "Test market",
    market: "#101",
    phase: "stopped",
    running: false,
    cleanup_confirmed: true,
    started: 1000,
    duration_sec: 120,
    volume_usd: 100,
    net_pnl: 0,
    fees: 1,
    fills: 2,
    cost_per_1m: 0,
    position_usd: 0,
    stale: false,
    accounting_complete: true,
    state: "stopped",
    reason: "",
    rotations: 0,
    config: {},
    quotes: [],
    ...overrides,
  };
}

describe("performance venue filters", () => {
  it("maps Outcome to HIP-4 and BBO/MID to HIP-3", () => {
    expect(venueOf({ strategy: "outcome" })).toBe("hip4");
    expect(venueOf({ strategy: "bbo" })).toBe("hip3");
    expect(venueOf({ strategy: "mid" })).toBe("hip3");
  });

  it("filters without mutating the input", () => {
    const rows = [
      session({ id: "a", strategy: "outcome" }),
      session({ id: "b", strategy: "bbo" }),
      session({ id: "c", strategy: "mid" }),
    ];
    expect(filterByVenue(rows, "all")).toHaveLength(3);
    expect(filterByVenue(rows, "hip4").map((row) => row.id)).toEqual(["a"]);
    expect(filterByVenue(rows, "hip3").map((row) => row.id)).toEqual(["b", "c"]);
    expect(rows).toHaveLength(3);
  });
});

describe("volume by period", () => {
  it("groups by hour for 24 h and by day beyond", () => {
    const rows = [
      session({
        id: "a",
        started: new Date(2026, 0, 10, 9).getTime() / 1000,
        volume_usd: 100,
      }),
      session({
        id: "b",
        strategy: "bbo",
        kind: "grid",
        started: new Date(2026, 0, 10, 9, 30).getTime() / 1000,
        volume_usd: 50,
      }),
      session({
        id: "c",
        started: new Date(2026, 0, 10, 15).getTime() / 1000,
        volume_usd: 25,
      }),
    ];
    const daily = volumeByPeriod(rows, 7);
    expect(daily.granularity).toBe("day");
    expect(daily.points).toHaveLength(1);
    expect(daily.points[0].hip4).toBe(125);
    expect(daily.points[0].hip3).toBe(50);
    expect(daily.points[0].total).toBe(175);
    expect(daily.points[0].sessions).toBe(3);

    const hourly = volumeByPeriod(rows, 1);
    expect(hourly.granularity).toBe("hour");
    expect(hourly.points).toHaveLength(2);
    expect(hourly.points[0].hip4).toBe(100);
    expect(hourly.points[0].hip3).toBe(50);
    expect(hourly.points[1].hip4).toBe(25);
    expect(hourly.points[1].hip3).toBe(0);
  });

  it("does not turn an incomplete venue volume into zero", () => {
    const rows = [
      session({ id: "a", volume_usd: 100 }),
      session({ id: "b", strategy: "bbo", kind: "grid", volume_usd: null }),
    ];
    const result = volumeByPeriod(rows, 7);
    expect(result.incomplete).toBe(true);
    expect(result.points[0].hip4).toBe(100);
    expect(result.points[0].hip3).toBeNull();
    expect(result.points[0].total).toBeNull();
  });

  it("returns no points without sessions", () => {
    const result = volumeByPeriod([], 7);
    expect(result.points).toEqual([]);
    expect(result.incomplete).toBe(false);
  });
});
