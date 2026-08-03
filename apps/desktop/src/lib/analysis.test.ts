import { describe, expect, it } from "vitest";
import { acceptanceStatus, calculateDesignCapacity, controllingStormIndex, nondominatedIds, reviewChecksComplete, selectDecisionReference } from "./analysis";

describe("exact analysis budget", () => {
  it("reserves one baseline job per storm", () => {
    expect(calculateDesignCapacity(884, 4)).toBe(220);
  });

  it("never returns negative capacity", () => {
    expect(calculateDesignCapacity(2, 4)).toBe(0);
    expect(calculateDesignCapacity(100, 0)).toBe(0);
  });
});

describe("model-fitness review checks", () => {
  it("requires every explicit review check", () => {
    expect(reviewChecksComplete([true, true, true, true, true])).toBe(true);
    expect(reviewChecksComplete([true, true, false, true, true])).toBe(false);
    expect(reviewChecksComplete([])).toBe(false);
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

describe("teaching decision lens", () => {
  it("makes the disclosed volume bands deterministic", () => {
    expect(acceptanceStatus(0.699)).toBe("meets");
    expect(acceptanceStatus(0.71)).toBe("near");
    expect(acceptanceStatus(0.721)).toBe("does-not-meet");
  });

  it("identifies the controlling storm and lowest-cost passing selection", () => {
    expect(controllingStormIndex([0.1, 0.25, 0.2, 0.18])).toBe(1);
    expect(controllingStormIndex([])).toBe(-1);
    const reference = selectDecisionReference([
      { id: "stronger", cost: 12, volume: 0.65 },
      { id: "least-cost-pass", cost: 9, volume: 0.69 },
      { id: "fail", cost: 4, volume: 0.73 },
    ]);
    expect(reference?.id).toBe("least-cost-pass");
  });
});
