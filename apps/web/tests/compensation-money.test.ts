import { describe, expect, it } from "vitest";
import {
  compMoney,
  moneyInput,
  parseMoneyInput,
  percent,
} from "@/lib/compensation";

describe("exact compensation input boundaries", () => {
  it("preserves 20-digit original minor amounts through display and parsing", () => {
    for (const exponent of [0, 2, 3]) {
      const minor = "99999999999999999999";
      expect(parseMoneyInput(moneyInput(minor, exponent), exponent)).toBe(
        minor,
      );
    }
    expect(parseMoneyInput("000012.30", 2)).toBe("1230");
    expect(parseMoneyInput("1.001", 3)).toBe("1001");
    expect(parseMoneyInput("12", 0)).toBe("12");
    expect(compMoney("1520000", "USD")).toBe("USD 15,200.00");
  });
  it("rejects fractional minor units, exponents, negative and overflow inputs", () => {
    for (const value of [
      "1.001",
      "-2",
      "1e5",
      "1,000",
      "",
      ".5",
      "NaN",
      "1000000000000000000.00",
    ])
      expect(() => parseMoneyInput(value, 2)).toThrow();
    expect(() => parseMoneyInput("1.0", 0)).toThrow();
    expect(() => parseMoneyInput("1", 1)).toThrow();
    expect(() => compMoney("100", "XXX")).toThrow();
  });
  it("renders the pinned decimal guideline without float arithmetic", () => {
    expect(percent("0.04")).toBe("4%");
    expect(percent("0.0015")).toBe("0.15%");
    expect(percent("0.20")).toBe("20%");
    expect(percent("0.00")).toBe("0%");
  });
});
