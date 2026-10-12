import { expect, it } from "vitest";
import { formatMoney } from "@/lib/workforce";
it.each([
  [
    { currency: "USD" as const, minor_units: "123456", exponent: 2 },
    "USD 1,234.56",
  ],
  [
    { currency: "JPY" as const, minor_units: "123456", exponent: 0 },
    "JPY 123,456",
  ],
  [
    { currency: "KWD" as const, minor_units: "123456", exponent: 3 },
    "KWD 123.456",
  ],
  [{ currency: "KWD" as const, minor_units: "1", exponent: 3 }, "KWD 0.001"],
  [
    {
      currency: "USD" as const,
      minor_units: "99999999999999999999",
      exponent: 2,
    },
    "USD 999,999,999,999,999,999.99",
  ],
  [{ currency: "USD" as const, minor_units: "-1", exponent: 2 }, "USD −0.01"],
])(
  "formats exact minor units without floating point conversion %j",
  (money, expected) => {
    expect(formatMoney(money)).toBe(expected);
  },
);
