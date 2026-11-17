import rawFixture from "./ridgewayFixture.json";

import {
  parseRidgewayFixture,
  RIDGEWAY_HYPOTHESIS_IDS,
  RIDGEWAY_HYPOTHESIS_RANKING_METRIC,
} from "./ridgeway";


function verifiedFixture(): any {
  const testSha = (value: number) => value.toString(16).padStart(64, "0").slice(-64);
  const fixture = structuredClone(rawFixture) as any;
  fixture.schemaVersion = "ecocorridor-replay/v1.2";
  fixture.evidenceState = "VERIFIED_DIRECT_EVIDENCE";
  fixture.releaseReplayEligible = true;
  fixture.manifestSha256 = "a".repeat(64);
  fixture.manifestVersion = "ridgeway-v1-test";
  fixture.baselinePortfolioId = "portfolio_00000000000000000000";
  fixture.study.hypothesisRankingMetric = RIDGEWAY_HYPOTHESIS_RANKING_METRIC;
  fixture.resourceObservability = {
    jobsWithObservations: fixture.study.matrixJobCount + fixture.study.mapJobCount,
    jobsWithoutObservations: 0,
    directSolverWallSeconds: 120,
    estimatedEnergyKwh: 0.001,
    estimatedEnergyLowerKwh: 0.0005,
    estimatedEnergyUpperKwh: 0.002,
    maximumObservedSingleProcessMemoryBytes: 1024,
    peakMemoryIsSampledLowerBound: true,
    peakMemoryStatus: "MEASURED",
    energyStatus: "ESTIMATED",
    proposerWallTimeStatus: "NOT_APPLICABLE_FIXED_ENUMERATION",
    limitation: "Synthetic contract test; an energy proxy is not a carbon value.",
  };
  fixture.reviewMask = {
    maskId: "test-review-mask",
    sha256: testSha(1),
    width: 2,
    height: 2,
    cells: [0, 1, 1, 0],
    interpretation: "Diagnostic review mask; not observed or predicted animal use.",
  };
  fixture.currentRasters = fixture.scenarios.map((scenario: any, scenarioIndex: number) => ({
    runId: `map_${fixture.baselinePortfolioId}_${scenario.id}`,
    portfolioId: fixture.baselinePortfolioId,
    scenarioId: scenario.id,
    sha256: testSha(100 + scenarioIndex),
    width: 2,
    height: 2,
    declaredNodata: -9999,
    nodataTopologySha256: testSha(200 + scenarioIndex),
    minimumCurrent: 0,
    maximumCurrent: 3,
    cells: [0, 1, 2, 3],
  }));
  fixture.portfolios.forEach((portfolio: any, portfolioIndex: number) => {
    portfolio.id = `portfolio_${String(portfolioIndex + 1).padStart(20, "0")}`;
    portfolio.scenarioResults = fixture.scenarios.map((scenario: any, scenarioIndex: number) => {
      const baselineRunId = `matrix_${fixture.baselinePortfolioId}_${scenario.id}`;
      const portfolioRunId = `matrix_${portfolio.id}_${scenario.id}`;
      const baselineMapRunId = `map_${fixture.baselinePortfolioId}_${scenario.id}`;
      const portfolioMapRunId = `map_${portfolio.id}_${scenario.id}`;
      const pairResults = Array.from({ length: scenario.includedPairs }, (_, pairIndex) => {
        const pairId = `${scenario.id}_pair_${pairIndex + 1}`;
        const baselineEffectiveResistance = 10 + pairIndex;
        return {
          pairId,
          baselineEffectiveResistance,
          portfolioEffectiveResistance: baselineEffectiveResistance * 0.96,
          baselineRelativeChangePct: -4,
          passed: true,
          claimId: `ECO-PAIR-${portfolio.id}-${scenario.id}-${pairId}`,
          baselineRunId,
          portfolioRunId,
          baselineMatrixSha256: testSha(1_000 + scenarioIndex),
          portfolioMatrixSha256: testSha(2_000 + portfolioIndex * 100 + scenarioIndex),
        };
      });
      fixture.currentRasters.push({
        runId: portfolioMapRunId,
        portfolioId: portfolio.id,
        scenarioId: scenario.id,
        sha256: testSha(3_000 + portfolioIndex * 100 + scenarioIndex),
        width: 2,
        height: 2,
        declaredNodata: -9999,
        nodataTopologySha256: testSha(200 + scenarioIndex),
        minimumCurrent: 0,
        maximumCurrent: 4,
        cells: [0, 1, 2, 4],
      });
      return {
        scenarioId: scenario.id,
        resistanceChangePct: -4,
        ruleStatus: "pass",
        reviewMaskChangePct: 0,
        runId: portfolioRunId,
        baselineMapRunId,
        portfolioMapRunId,
        pairResults,
      };
    });
    portfolio.hypothesisRanks = Object.fromEntries(
      RIDGEWAY_HYPOTHESIS_IDS.map((hypothesisId, hypothesisIndex) => [
        hypothesisId,
        { rank: portfolioIndex * 4 + hypothesisIndex + 1, score: 12 - hypothesisIndex },
      ]),
    );
  });
  return fixture;
}


describe("Ridgeway verified projection contract", () => {
  it("requires an explicit baseline identity and complete direct rank evidence", () => {
    const parsed = parseRidgewayFixture(verifiedFixture());
    expect(parsed.baselinePortfolioId).toBe("portfolio_00000000000000000000");
    expect(parsed.portfolios.every((portfolio) => portfolio.hypothesisRanks !== null)).toBe(true);
  });

  it("rejects a baseline identity aliased to a teaching portfolio", () => {
    const fixture = verifiedFixture();
    fixture.baselinePortfolioId = fixture.portfolios[0].id;
    expect(() => parseRidgewayFixture(fixture)).toThrow(/baseline portfolio must be distinct/i);
  });

  it("rejects missing, malformed, or incomplete hypothesis ranks", () => {
    const missing = verifiedFixture();
    delete missing.portfolios[0].hypothesisRanks.h4_future_pressure;
    expect(() => parseRidgewayFixture(missing)).toThrow();

    const outOfRange = verifiedFixture();
    outOfRange.portfolios[0].hypothesisRanks.h1_low_barrier.rank = 129;
    expect(() => parseRidgewayFixture(outOfRange)).toThrow();

    const nonFinite = verifiedFixture();
    nonFinite.portfolios[0].hypothesisRanks.h1_low_barrier.score = Number.NaN;
    expect(() => parseRidgewayFixture(nonFinite)).toThrow();
  });

  it("rejects missing or incomplete per-job resource observations", () => {
    const missing = verifiedFixture();
    delete missing.resourceObservability;
    expect(() => parseRidgewayFixture(missing)).toThrow(/resource observation per direct job/i);

    const incomplete = verifiedFixture();
    incomplete.resourceObservability.jobsWithObservations -= 1;
    incomplete.resourceObservability.jobsWithoutObservations = 1;
    expect(() => parseRidgewayFixture(incomplete)).toThrow(/resource observation per direct job/i);
  });

  it("rejects a missing focal-pair row or a reducer that differs from direct pair evidence", () => {
    const missingPair = verifiedFixture();
    missingPair.portfolios[0].scenarioResults[0].pairResults.pop();
    expect(() => parseRidgewayFixture(missingPair)).toThrow(/every required focal-pair result/i);

    const alteredReducer = verifiedFixture();
    alteredReducer.portfolios[0].scenarioResults[0].resistanceChangePct = -9;
    expect(() => parseRidgewayFixture(alteredReducer)).toThrow(/reducer differs/i);
  });

  it("rejects altered focal-pair lineage and incomplete retained current rasters", () => {
    const alteredHash = verifiedFixture();
    alteredHash.portfolios[0].scenarioResults[0].pairResults[0].portfolioMatrixSha256 = "f".repeat(64);
    alteredHash.portfolios[0].scenarioResults[0].pairResults[0].portfolioRunId = "matrix_wrong";
    expect(() => parseRidgewayFixture(alteredHash)).toThrow(/inconsistent focal-pair evidence/i);

    const missingRaster = verifiedFixture();
    missingRaster.currentRasters.pop();
    expect(() => parseRidgewayFixture(missingRaster)).toThrow(/all 60 retained current rasters/i);
  });
});
