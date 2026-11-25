import { describe, expect, it } from "vitest";
import type { Draft } from "../types";
import {
  amountError,
  amountWarning,
  capError,
  capGauge,
  formBlockReason,
  launchPayload,
  orderMax,
  quickAmounts,
} from "./launch";

const draft = (overrides: Partial<Draft> = {}): Draft => ({
  strategy: "outcome",
  order_usd: 25,
  position_cap: 200,
  market: "",
  side: "no",
  spread_bps: 10,
  offset_bps: 1,
  duration_sec: null,
  target_volume_usd: null,
  stop_loss_usd: 50,
  ...overrides,
});

describe("launch rules", () => {
  it("enforces minimum, maximum and available balance", () => {
    expect(amountError(draft({ order_usd: 5 }), 100)).toContain("Minimum");
    expect(amountError(draft({ order_usd: 101 }), 100)).toContain("solde");
    expect(amountError(draft({ order_usd: 25 }), 100)).toBeNull();
  });

  it("warns above 80% without blocking", () => {
    expect(amountWarning(draft({ order_usd: 81 }), 100)).toContain("80 %");
    expect(amountWarning(draft({ order_usd: 80 }), 100)).toBeNull();
  });

  it("requires the cap to cover one order", () => {
    expect(capError(draft({ order_usd: 30, position_cap: 20 }))).toContain(
      "couvrir",
    );
    expect(capError(draft({ order_usd: 20, position_cap: 20 }))).toBeNull();
  });

  it("derives quick amounts from the cap and balance", () => {
    const values = quickAmounts(draft({ position_cap: 100 }), 80);
    expect(values.map((v) => v.label)).toEqual(["25 %", "50 %", "MAX"]);
    expect(values.map((v) => v.value)).toEqual([20, 40, 80]);
    expect(orderMax(draft({ position_cap: 100 }), 80)).toBe(80);
  });

  it("normalizes the hidden BBO/MID cap for the server", () => {
    const payload = launchPayload(
      draft({ strategy: "bbo", order_usd: 250, position_cap: 200 }),
    );
    expect(payload.position_cap).toBe(250);
    expect(launchPayload(draft()).position_cap).toBe(200);
  });

  it("blocks an unselected Outcome side", () => {
    expect(formBlockReason(draft({ side: null }), 100)).toBe("side");
    expect(formBlockReason(draft({ side: "yes" }), 100)).toBeNull();
    expect(capGauge(draft({ order_usd: 25, position_cap: 200 })).percent).toBe(
      12.5,
    );
  });
});
