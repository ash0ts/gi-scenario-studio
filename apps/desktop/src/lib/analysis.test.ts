import { describe, expect, it } from "vitest";
import { attestationsComplete, calculateDesignCapacity, nondominatedIds } from "./analysis";

describe("exact analysis budget", () => {
  it("reserves one baseline job per storm", () => {
    expect(calculateDesignCapacity(884, 4)).toBe(220);
  });

  it("never returns negative capacity", () => {
    expect(calculateDesignCapacity(2, 4)).toBe(0);
    expect(calculateDesignCapacity(100, 0)).toBe(0);
  });
});

describe("readiness attestation", () => {
  it("requires every explicit attestation", () => {
    expect(attestationsComplete([true, true, true, true, true])).toBe(true);
    expect(attestationsComplete([true, true, false, true, true])).toBe(false);
    expect(attestationsComplete([])).toBe(false);
  });
});

describe("metric-specific exact frontier", () => {
  const alternatives = [
    { id: "low-cost", cost: 1, volume: 8, peak: 9 },
    { id: "balanced", cost: 2, volume: 5, peak: 8 },
    { id: "low-volume", cost: 3, volume: 3, peak: 10 },
    { id: "low-peak", cost: 3, volume: 7, peak: 4 },
  ];

  it("recomputes nondomination for the selected hydraulic metric", () => {
    const volume = nondominatedIds(alternatives, (item) => [item.cost, item.volume]);
    const peak = nondominatedIds(alternatives, (item) => [item.cost, item.peak]);

    expect([...volume]).toEqual(["low-cost", "balanced", "low-volume"]);
    expect([...peak]).toEqual(["low-cost", "balanced", "low-peak"]);
  });
});
