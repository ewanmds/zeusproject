import { describe, expect, it } from "vitest";
import {
  defaultLocale,
  interpolate,
  isLocale,
  translate,
  type TranslationKey,
} from "./dictionaries";

describe("i18n dictionaries", () => {
  it("defaults to French and resolves the English dictionary", () => {
    expect(defaultLocale).toBe("fr");
    expect(translate("fr", "nav.run")).toBe("Lancer");
    expect(translate("en", "nav.run")).toBe("Launch");
  });

  it("falls back to the French base and then to the key itself", () => {
    expect(translate("de", "nav.sessions")).toBe("Sessions");
    const missing = "errors.doesNotExist" as TranslationKey;
    expect(translate("fr", missing)).toBe(missing);
    expect(translate("en", missing)).toBe(missing);
  });

  it("interpolates named parameters and preserves unknown placeholders", () => {
    expect(translate("fr", "common.selectedCount", { count: 3 })).toBe(
      "3 sélectionné(s)",
    );
    expect(translate("en", "common.selectedCount", { count: 2 })).toBe(
      "2 selected",
    );
    expect(
      interpolate("{left} / {right}", { left: "BID", right: "ASK" }),
    ).toBe("BID / ASK");
    expect(interpolate("{left} / {right}", { left: "BID" })).toBe(
      "BID / {right}",
    );
    expect(interpolate("No placeholder")).toBe("No placeholder");
  });

  it("keeps trading vocabulary in English in both locales", () => {
    expect(translate("fr", "common.side.yes")).toBe("Yes");
    expect(translate("fr", "common.side.no")).toBe("No");
    expect(translate("fr", "landing.strategies.bbo.title")).toBe("BBO");
    expect(translate("fr", "landing.strategies.mid.title")).toBe("MID");
    expect(translate("fr", "launch.summary.makerPostOnly")).toBe(
      "Maker · Post-only",
    );
  });

  it("accepts only supported locale values", () => {
    expect(isLocale("fr")).toBe(true);
    expect(isLocale("en")).toBe(true);
    expect(isLocale("de")).toBe(false);
    expect(isLocale(null)).toBe(false);
    expect(isLocale(undefined)).toBe(false);
  });
});
