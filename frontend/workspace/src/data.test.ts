import { describe, it, expect, vi, afterEach } from "vitest";
import {
  SnapshotStore,
  usd,
  clock,
  api,
  setCSRF,
  invalidateIdentity,
} from "./data";
import type { Snapshot } from "./types";
const frame = (
  revision: number,
  owner = "a",
  source_id = "server-a",
  published_at = 100,
) =>
  ({
    type: "workspace",
    owner,
    source_id,
    revision,
    published_at,
    sessions: [],
    metrics: {},
  }) as unknown as Snapshot;
afterEach(() => vi.unstubAllGlobals());
describe("tenant snapshots", () => {
  it("rejects other wallets and out-of-order frames", () => {
    const s = new SnapshotStore("a");
    expect(s.accept(frame(3))).toBe(true);
    expect(s.accept(frame(4, "b"))).toBe(false);
    expect(s.accept(frame(2))).toBe(false);
    expect(s.current?.revision).toBe(3);
  });
  it("retires old server streams after restart", () => {
    const s = new SnapshotStore("a");
    s.accept(frame(5));
    expect(s.accept(frame(1, "a", "server-b", 101))).toBe(true);
    expect(s.accept(frame(9, "a", "server-a", 102))).toBe(false);
  });
  it("never accepts an older server timestamp", () => {
    const s = new SnapshotStore("a");
    s.accept(frame(5));
    expect(s.accept(frame(1, "a", "server-b", 90))).toBe(false);
  });
  it("rejects nonfinite stream metadata", () => {
    const s = new SnapshotStore("a");
    expect(s.accept(frame(NaN))).toBe(false);
  });
});
describe("identity boundary", () => {
  it("discards a late API response after wallet change", async () => {
    let finish: (v: unknown) => void = () => {};
    vi.stubGlobal("fetch", () => new Promise((r) => (finish = r)));
    setCSRF("a");
    const request = api("/account");
    invalidateIdentity();
    setCSRF("b");
    finish({ ok: true, json: async () => ({ privateAccount: "a" }) });
    await expect(request).rejects.toMatchObject({ name: "AbortError" });
  });
  it("does not retry a failed financial command", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("timeout"));
    vi.stubGlobal("fetch", fetch);
    await expect(api("/sessions", {})).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
describe("financial display", () => {
  it("distinguishes unknown, zero and small losses", () => {
    expect(usd(null)).toBe("—");
    expect(usd(0)).toBe("$0.00");
    expect(usd(-0.0048)).toContain("0.0048");
    expect(clock(125)).toBe("00:02:05");
  });
});
