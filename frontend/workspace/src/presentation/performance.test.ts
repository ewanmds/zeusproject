import { describe, expect, it } from "vitest";
import type { Session } from "../types";
import {
  bucketPerformance,
  equityCurve,
  filterByVenue,
  formatFactor,
  formatPercent,
  marketBreakdown,
  outcomeBreakdown,
  performanceFacts,
  sumKnown,
  venueOf,
} from "./performance";

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

describe("performance filters", () => {
  it("formats rates and factors for the dashboard", () => {
    expect(formatPercent(66.666)).toBe("67 %");
    expect(formatPercent(8.333)).toBe("8,3 %");
    expect(formatPercent(null)).toBe("—");
    expect(formatFactor(4)).toBe("4,00");
    expect(formatFactor(Number.POSITIVE_INFINITY)).toBe("∞");
    expect(formatFactor(null)).toBe("—");
  });

  it("maps Outcome to HIP-4 and BBO/MID to HIP-3", () => {
    expect(venueOf({ strategy: "outcome" })).toBe("hip4");
    expect(venueOf({ strategy: "bbo" })).toBe("hip3");
    expect(venueOf({ strategy: "mid" })).toBe("hip3");
  });

  it("filters by venue without mutating the input", () => {
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

describe("performance facts", () => {
  it("never turns an incomplete field into zero", () => {
    expect(sumKnown([], "net_pnl")).toBe(0);
    expect(sumKnown([session({ net_pnl: 3 })], "net_pnl")).toBe(3);
    expect(
      sumKnown(
        [session({ net_pnl: 3 }), session({ net_pnl: null })],
        "net_pnl",
      ),
    ).toBeNull();
    expect(
      sumKnown(
        [session({ net_pnl: 3, accounting_complete: false })],
        "net_pnl",
      ),
    ).toBeNull();
  });

  it("computes win rate, profit factor and best market from closed sessions", () => {
    const rows = [
      session({ id: "a", market: "#1", net_pnl: 10 }),
      session({ id: "b", market: "#1", net_pnl: 6 }),
      session({ id: "c", market: "#2", net_pnl: -4 }),
      session({
        id: "d",
        market: "#2",
        running: true,
        net_pnl: 5,
        accounting_complete: true,
      }),
    ];
    const facts = performanceFacts(rows);
    expect(facts.closed).toBe(3);
    expect(facts.running).toBe(1);
    expect(facts.net).toBe(17);
    expect(facts.winRate).toBeCloseTo(66.666, 2);
    expect(facts.wins).toBe(2);
    expect(facts.losses).toBe(1);
    expect(facts.profitFactor).toBe(4);
    expect(facts.avgNet).toBe(4);
    expect(facts.bestMarket).toEqual({ name: "Test market", net: 16 });
    expect(facts.costPerMillion).toBeCloseTo((-17 / 400) * 1e6, 2);
  });

  it("returns an infinite profit factor when there is no loss", () => {
    const facts = performanceFacts([session({ net_pnl: 5 })]);
    expect(facts.profitFactor).toBe(Number.POSITIVE_INFINITY);
    expect(facts.winRate).toBe(100);
  });
});

describe("performance series", () => {
  it("builds a sorted cumulative equity curve and skips unknown values", () => {
    const rows = [
      session({ id: "b", started: 200, net_pnl: 6 }),
      session({ id: "a", started: 100, net_pnl: 10 }),
      session({ id: "x", started: 150, net_pnl: null }),
    ];
    const curve = equityCurve(rows);
    expect(curve.points.map((point) => point.cumulative)).toEqual([0, 10, 16]);
    expect(curve.points[0].name).toBe("Départ");
    expect(curve.incomplete).toBe(true);
  });

  it("groups by session for 24 h and by day beyond that", () => {
    const rows = [
      session({
        id: "a",
        started: new Date(2026, 0, 10, 9).getTime() / 1000,
        volume_usd: 100,
        net_pnl: 3,
      }),
      session({
        id: "b",
        started: new Date(2026, 0, 10, 15).getTime() / 1000,
        volume_usd: 50,
        net_pnl: 2,
      }),
      session({
        id: "c",
        started: new Date(2026, 0, 11, 9).getTime() / 1000,
        volume_usd: 25,
        net_pnl: -1,
      }),
    ];
    const daily = bucketPerformance(rows, 7);
    expect(daily.granularity).toBe("day");
    expect(daily.points).toHaveLength(2);
    expect(daily.points[0].sessions).toBe(2);
    expect(daily.points[0].volume).toBe(150);
    expect(daily.points[0].net).toBe(5);
    const intraday = bucketPerformance(rows, 1);
    expect(intraday.granularity).toBe("session");
    expect(intraday.points).toHaveLength(3);
  });

  it("ranks markets by volume and keeps null accounting honest", () => {
    const rows = [
      session({ id: "a", market: "#1", name: "Alpha", volume_usd: 100, net_pnl: 4 }),
      session({ id: "b", market: "#2", name: "Beta", volume_usd: 250, net_pnl: 2 }),
      session({
        id: "c",
        market: "#2",
        name: "Beta",
        strategy: "bbo",
        kind: "grid",
        volume_usd: 20,
        net_pnl: null,
      }),
    ];
    const markets = marketBreakdown(rows);
    expect(markets.map((row) => row.name)).toEqual(["Beta", "Alpha"]);
    expect(markets[0].venue).toBe("mixed");
    expect(markets[0].volume).toBe(270);
    expect(markets[0].net).toBeNull();
    expect(markets[0].sessions).toBe(2);
  });

  it("counts wins, losses, flat and running sessions", () => {
    const facts = performanceFacts([
      session({ id: "a", net_pnl: 1 }),
      session({ id: "b", net_pnl: -1 }),
      session({ id: "c", net_pnl: 0 }),
      session({ id: "d", running: true, net_pnl: null }),
    ]);
    expect(outcomeBreakdown(facts)).toEqual([
      { key: "wins", label: "Gains", value: 1 },
      { key: "losses", label: "Pertes", value: 1 },
      { key: "flat", label: "Neutres", value: 1 },
      { key: "running", label: "En cours", value: 1 },
    ]);
  });
});
