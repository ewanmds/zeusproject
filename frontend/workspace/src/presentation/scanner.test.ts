import { describe, expect, it } from "vitest";
import { describeScannerDiagnostic, sanitizeDiagnostic } from "./scanner";

describe("scanner diagnostics", () => {
  it("stays silent as soon as eligible candidates arrive", () => {
    expect(
      describeScannerDiagnostic({
        ready: true,
        candidates: [{ market: "#1" }],
        error: "old failure",
        diagnostics: { spread_rejections: 4 },
      }),
    ).toBeNull();
    expect(
      describeScannerDiagnostic({
        ready: false,
        candidates: [{ market: "#1" }],
        error: "old failure",
      }),
    ).toBeNull();
  });

  it("shows a sanitized, bounded backend cause", () => {
    const message = describeScannerDiagnostic({
      ready: false,
      error:
        "stream\nfailed 0x" +
        "a".repeat(64) +
        " " +
        "detail ".repeat(80),
    });
    expect(message).toContain("stream failed");
    expect(message).toContain("[clé masquée]");
    expect(message).not.toContain("\n");
    expect(message!.length).toBeLessThanOrEqual(420);
    expect(sanitizeDiagnostic(" a\n b ", 12)).toBe("a b");
  });

  it("prefers the backend threshold-aware reason when present", () => {
    const message = describeScannerDiagnostic({
      ready: false,
      reason:
        "Aucun candidat: 12 spread(s) > 8 bps (min observé 9.5 bps)",
      rejections: { spread: 12 },
    });
    expect(message).toContain("12 spread(s) > 8 bps");
  });

  it("adds a funds/approval cause the backend reason does not mention", () => {
    const message = describeScannerDiagnostic({
      ready: false,
      reason: "Aucun flux deux sens dans les 5 dernières minutes",
      rejection_counts: { insufficient_funds: 2 },
    });
    expect(message).toContain("Aucun flux deux sens");
    expect(message).toContain("fonds ou approbation");
  });

  it("explains warmup without pretending the scanner is broken", () => {
    const message = describeScannerDiagnostic({
      connected: true,
      warming_up: true,
      observed_sec: 12,
      markets_observed: 3,
      markets_total: 10,
    });
    expect(message).toContain("Observation du flux");
    expect(message).toContain("12/60 s");
    expect(message).toContain("3/10 marchés");
  });

  it("reports rejection counts with bps thresholds", () => {
    const spread = describeScannerDiagnostic({
      connected: true,
      observed_sec: 120,
      markets_observed: 10,
      markets_total: 10,
      diagnostics: { spread_rejections: 12, spread_bps_limit: 8 },
    });
    expect(spread).toContain("12 marchés écartés");
    expect(spread).toContain("8 bps");
    expect(spread).not.toContain("%");

    const funds = describeScannerDiagnostic({
      connected: true,
      observed_sec: 120,
      rejection_counts: { insufficient_funds: 2, approval_required: 1 },
    });
    expect(funds).toContain("fonds ou approbation");

    const stale = describeScannerDiagnostic({
      connected: true,
      observed_sec: 120,
      rejections: { stale_quotes: 4 },
    });
    expect(stale).toContain("carnet périmé");
  });

  it("describes no eligible flow with the current spread limit", () => {
    const message = describeScannerDiagnostic(
      {
        ready: false,
        connected: true,
        observed_sec: 120,
        markets_observed: 10,
        markets_total: 10,
        flow_candidates: 0,
        candidates: [],
      },
      10,
    );
    expect(message).toContain("Aucun marché éligible");
    expect(message).toContain("10 bps");
  });

  it("clears a previous diagnostic when fresh data succeeds", () => {
    const failed = describeScannerDiagnostic({
      ready: false,
      error: "temporary feed error",
    });
    expect(failed).toContain("temporary feed error");
    expect(
      describeScannerDiagnostic({
        ready: true,
        candidates: [{ market: "#1" }],
      }),
    ).toBeNull();
  });
});
