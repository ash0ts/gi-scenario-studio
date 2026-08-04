import { z } from "zod";

import type { EvidenceSelection, RidgewayFixture, RidgewayHypothesisId, RidgewayPairResult, RidgewayPortfolio, RidgewayScenarioResult } from "../types";

const sha256Schema = z.string().regex(/^[a-f0-9]{64}$/);
const portfolioIdSchema = z.string().regex(/^portfolio_[a-f0-9]{20}$/);
export const RIDGEWAY_HYPOTHESIS_IDS = [
  "h1_low_barrier",
  "h2_moderate_barrier",
  "h3_high_barrier",
  "h4_future_pressure",
] as const satisfies readonly RidgewayHypothesisId[];
export const RIDGEWAY_HYPOTHESIS_RANKING_METRIC = "Minimum required target-pair resistance reduction (%) within each resistance hypothesis; rank 1 is the highest score among the 128 directly evaluated portfolios.";
const scenarioSchema = z.object({
  id: z.string().min(1),
  guild: z.string().min(1),
  purpose: z.enum(["target", "safeguard"]),
  hypothesis: z.string().min(1),
  futureCondition: z.string().min(1),
  maturity: z.string().min(1),
  includedPairs: z.number().int().positive(),
}).strict();
const pairResultSchema = z.object({
  pairId: z.string().min(1),
  baselineEffectiveResistance: z.number().finite().positive(),
  portfolioEffectiveResistance: z.number().finite().positive(),
  baselineRelativeChangePct: z.number().finite(),
  passed: z.boolean(),
  claimId: z.string().min(1),
  baselineRunId: z.string().min(1),
  portfolioRunId: z.string().min(1),
  baselineMatrixSha256: sha256Schema,
  portfolioMatrixSha256: sha256Schema,
}).strict();
const scenarioResultSchema = z.object({
  scenarioId: z.string().min(1),
  resistanceChangePct: z.number().finite(),
  ruleStatus: z.enum(["pass", "fail", "review"]),
  reviewMaskChangePct: z.number().finite(),
  runId: z.string().min(1),
  baselineMapRunId: z.string().min(1),
  portfolioMapRunId: z.string().min(1),
  pairResults: z.array(pairResultSchema).min(1),
}).strict();
const currentRasterSchema = z.object({
  runId: z.string().min(1),
  portfolioId: portfolioIdSchema,
  scenarioId: z.string().min(1),
  sha256: sha256Schema,
  width: z.number().int().positive(),
  height: z.number().int().positive(),
  declaredNodata: z.number().finite(),
  nodataTopologySha256: sha256Schema,
  minimumCurrent: z.number().finite().nonnegative(),
  maximumCurrent: z.number().finite().nonnegative(),
  cells: z.array(z.number().finite().nonnegative().nullable()).min(1),
}).strict();
const reviewMaskSchema = z.object({
  maskId: z.string().min(1),
  sha256: sha256Schema,
  width: z.number().int().positive(),
  height: z.number().int().positive(),
  cells: z.array(z.union([z.literal(0), z.literal(1)])).min(1),
  interpretation: z.string().min(1),
}).strict();
const hypothesisRankObservationSchema = z.object({
  rank: z.number().int().min(1).max(128),
  score: z.number().finite(),
}).strict();
const hypothesisRanksSchema = z.object({
  h1_low_barrier: hypothesisRankObservationSchema,
  h2_moderate_barrier: hypothesisRankObservationSchema,
  h3_high_barrier: hypothesisRankObservationSchema,
  h4_future_pressure: hypothesisRankObservationSchema,
}).strict();
const observationStatusSchema = z.enum([
  "MEASURED",
  "ESTIMATED",
  "NOT_MEASURED",
  "MIXED",
  "NOT_RECORDED",
]);
const resourceObservabilitySchema = z.object({
  jobsWithObservations: z.number().int().nonnegative(),
  jobsWithoutObservations: z.number().int().nonnegative(),
  directSolverWallSeconds: z.number().finite().nonnegative(),
  estimatedEnergyKwh: z.number().finite().nonnegative().nullable(),
  estimatedEnergyLowerKwh: z.number().finite().nonnegative().nullable(),
  estimatedEnergyUpperKwh: z.number().finite().nonnegative().nullable(),
  maximumObservedSingleProcessMemoryBytes: z.number().int().nonnegative().nullable(),
  peakMemoryIsSampledLowerBound: z.boolean(),
  peakMemoryStatus: observationStatusSchema,
  energyStatus: observationStatusSchema,
  proposerWallTimeStatus: z.literal("NOT_APPLICABLE_FIXED_ENUMERATION"),
  limitation: z.string().min(1),
}).strict();
const portfolioSchema = z.object({
  projectionKey: z.string().min(1),
  id: z.string().min(1).nullable(),
  name: z.string().min(1),
  role: z.enum(["caution", "carry-forward", "over-budget", "brittle"]),
  costUsd: z.number().finite().nonnegative().nullable(),
  ruleStatus: z.enum(["pass", "fail", "review", "unavailable"]),
  passCount: z.number().int().nonnegative().nullable(),
  requiredCount: z.number().int().positive(),
  worstResistanceChangePct: z.number().finite().nullable(),
  worstScenarioId: z.string().min(1).nullable(),
  roadMaskFlag: z.boolean().nullable(),
  nextAction: z.string().min(1),
  optionSummary: z.string().min(1),
  hypothesisRanks: hypothesisRanksSchema.nullable().optional(),
  scenarioResults: z.array(scenarioResultSchema),
}).strict();

const ridgewayFixtureSchema = z.object({
  schemaVersion: z.enum(["ecocorridor-replay/v1.0", "ecocorridor-replay/v1.1", "ecocorridor-replay/v1.2"]),
  fixtureId: z.literal("ridgeway_crossing"),
  baselinePortfolioId: portfolioIdSchema.nullable().optional(),
  title: z.string().min(1),
  cc0: z.literal(true),
  manifestSha256: sha256Schema.nullable(),
  manifestVersion: z.string().min(1).nullable(),
  evidenceState: z.enum(["ENGINE_RUN_REQUIRED", "VERIFIED_DIRECT_EVIDENCE"]),
  resourceObservability: resourceObservabilitySchema.nullable().optional(),
  releaseReplayEligible: z.boolean(),
  releaseBlocker: z.string(),
  projectionGeneratedAt: z.string().min(1),
  disclaimer: z.string().min(1),
  decisionBrief: z.object({
    question: z.string().min(1),
    budgetUsd: z.number().finite().nonnegative(),
    targetRule: z.string().min(1),
    safeguardRule: z.string().min(1),
    nonGoals: z.array(z.string().min(1)).min(1),
  }).strict(),
  study: z.object({
    portfolioCount: z.number().int().positive(),
    scenarioCount: z.number().int().positive(),
    matrixJobCount: z.number().int().positive(),
    mapJobCount: z.number().int().nonnegative(),
    coreCount: z.number().int().positive(),
    interventionSiteCount: z.number().int().positive(),
    hypothesisRankingMetric: z.literal(RIDGEWAY_HYPOTHESIS_RANKING_METRIC).nullable().optional(),
  }).strict(),
  currentRasters: z.array(currentRasterSchema).optional(),
  reviewMask: reviewMaskSchema.nullable().optional(),
  scenarios: z.array(scenarioSchema),
  portfolios: z.array(portfolioSchema),
}).strict();

export function parseRidgewayFixture(value: unknown): RidgewayFixture {
  const parsed = ridgewayFixtureSchema.parse(value);
  const fixture = {
    ...parsed,
    baselinePortfolioId: parsed.baselinePortfolioId ?? null,
    resourceObservability: parsed.resourceObservability ?? null,
    currentRasters: parsed.currentRasters ?? [],
    reviewMask: parsed.reviewMask ?? null,
    study: {
      ...parsed.study,
      hypothesisRankingMetric: parsed.study.hypothesisRankingMetric ?? null,
    },
    portfolios: parsed.portfolios.map((portfolio) => ({
      ...portfolio,
      hypothesisRanks: portfolio.hypothesisRanks ?? null,
    })),
  } as RidgewayFixture;
  if (fixture.scenarios.length !== 12 || fixture.study.scenarioCount !== fixture.scenarios.length) {
    throw new Error("Ridgeway requires exactly 12 ecological scenarios.");
  }
  if (fixture.portfolios.length !== 4) {
    throw new Error("The teaching projection requires four evidence roles.");
  }
  const scenarioIds = new Set(fixture.scenarios.map((item) => item.id));
  const teachingPortfolioIds = new Set(
    fixture.portfolios.flatMap((item) => item.id ? [item.id] : []),
  );
  if (fixture.releaseReplayEligible) {
    if (fixture.schemaVersion !== "ecocorridor-replay/v1.2") {
      throw new Error("Verified Ridgeway evidence requires the v1.2 replay contract.");
    }
    if (!fixture.manifestSha256 || !fixture.manifestVersion) {
      throw new Error("Verified Ridgeway evidence requires its exact manifest identity.");
    }
    if (!fixture.baselinePortfolioId) {
      throw new Error("Verified Ridgeway evidence requires its packaged baseline portfolio identity.");
    }
    if (
      !fixture.resourceObservability
      || fixture.resourceObservability.jobsWithObservations
        !== fixture.study.matrixJobCount + fixture.study.mapJobCount
      || fixture.resourceObservability.jobsWithoutObservations !== 0
    ) {
      throw new Error("Verified Ridgeway evidence requires one valid resource observation per direct job.");
    }
    if (teachingPortfolioIds.has(fixture.baselinePortfolioId)) {
      throw new Error("The Ridgeway baseline portfolio must be distinct from every teaching portfolio.");
    }
    if (!fixture.reviewMask || fixture.currentRasters.length !== fixture.study.mapJobCount) {
      throw new Error("Verified Ridgeway evidence requires the review mask and all 60 retained current rasters.");
    }
  }
  if (scenarioIds.size !== fixture.scenarios.length) throw new Error("Ridgeway scenario IDs must be unique.");
  for (const portfolio of fixture.portfolios) {
    if (fixture.releaseReplayEligible) {
      if (fixture.study.portfolioCount !== 128 || fixture.study.hypothesisRankingMetric !== RIDGEWAY_HYPOTHESIS_RANKING_METRIC) {
        throw new Error("Verified Ridgeway evidence requires its exact 128-portfolio hypothesis ranking basis.");
      }
      if (!portfolio.hypothesisRanks) {
        throw new Error(`${portfolio.projectionKey} has no direct hypothesis-rank evidence.`);
      }
      if (Object.keys(portfolio.hypothesisRanks).length !== RIDGEWAY_HYPOTHESIS_IDS.length) {
        throw new Error(`${portfolio.projectionKey} does not cover all four resistance hypotheses.`);
      }
      if (!portfolio.id || portfolio.scenarioResults.length !== fixture.scenarios.length) {
        throw new Error(`${portfolio.projectionKey} does not include a complete direct scenario ensemble.`);
      }
      const resultIds = new Set(portfolio.scenarioResults.map((item) => item.scenarioId));
      if (resultIds.size !== scenarioIds.size || [...scenarioIds].some((id) => !resultIds.has(id))) {
        throw new Error(`${portfolio.id} does not cover the exact required scenario set.`);
      }
      for (const result of portfolio.scenarioResults) {
        if (!result.runId.startsWith(`matrix_${portfolio.id}_`)) {
          throw new Error(`${portfolio.id} contains a non-authoritative direct-run identity.`);
        }
        const scenario = fixture.scenarios.find((item) => item.id === result.scenarioId)!;
        if (result.pairResults.length !== scenario.includedPairs) {
          throw new Error(`${portfolio.id} does not retain every required focal-pair result.`);
        }
        const pairIds = new Set(result.pairResults.map((item) => item.pairId));
        if (pairIds.size !== result.pairResults.length) {
          throw new Error(`${portfolio.id} contains duplicate focal-pair results.`);
        }
        const expectedBaselineRunId = `matrix_${fixture.baselinePortfolioId}_${result.scenarioId}`;
        const expectedPortfolioRunId = `matrix_${portfolio.id}_${result.scenarioId}`;
        const expectedBaselineMapRunId = `map_${fixture.baselinePortfolioId}_${result.scenarioId}`;
        const expectedPortfolioMapRunId = `map_${portfolio.id}_${result.scenarioId}`;
        if (
          result.runId !== expectedPortfolioRunId
          || result.baselineMapRunId !== expectedBaselineMapRunId
          || result.portfolioMapRunId !== expectedPortfolioMapRunId
        ) {
          throw new Error(`${portfolio.id} contains a non-authoritative matrix or current-raster identity.`);
        }
        for (const pair of result.pairResults) {
          const expectedChange = (pair.portfolioEffectiveResistance - pair.baselineEffectiveResistance) / pair.baselineEffectiveResistance * 100;
          const expectedPass = expectedChange <= (scenario.purpose === "safeguard" ? 0 : -3);
          if (
            pair.claimId !== `ECO-PAIR-${portfolio.id}-${result.scenarioId}-${pair.pairId}`
            || pair.baselineRunId !== expectedBaselineRunId
            || pair.portfolioRunId !== expectedPortfolioRunId
            || Math.abs(pair.baselineRelativeChangePct - expectedChange) > 1e-9
            || pair.passed !== expectedPass
          ) {
            throw new Error(`${portfolio.id} contains inconsistent focal-pair evidence.`);
          }
        }
        const controllingChange = Math.max(...result.pairResults.map((item) => item.baselineRelativeChangePct));
        if (Math.abs(controllingChange - result.resistanceChangePct) > 1e-9) {
          throw new Error(`${portfolio.id} scenario reducer differs from its focal-pair evidence.`);
        }
      }
    }
  }
  if (fixture.releaseReplayEligible && fixture.reviewMask) {
    const rasterIds = new Set(fixture.currentRasters.map((item) => item.runId));
    const expectedRasterIds = new Set([
      ...fixture.scenarios.map((scenario) => `map_${fixture.baselinePortfolioId}_${scenario.id}`),
      ...fixture.portfolios.flatMap((portfolio) => fixture.scenarios.map((scenario) => `map_${portfolio.id}_${scenario.id}`)),
    ]);
    if (rasterIds.size !== fixture.currentRasters.length || rasterIds.size !== expectedRasterIds.size || [...expectedRasterIds].some((id) => !rasterIds.has(id))) {
      throw new Error("Verified Ridgeway current-raster identities do not match the 60-job teaching ensemble.");
    }
    for (const raster of fixture.currentRasters) {
      if (
        raster.cells.length !== raster.width * raster.height
        || raster.width !== fixture.reviewMask.width
        || raster.height !== fixture.reviewMask.height
        || raster.minimumCurrent > raster.maximumCurrent
      ) {
        throw new Error(`Current raster ${raster.runId} has an invalid compact grid projection.`);
      }
    }
    if (fixture.reviewMask.cells.length !== fixture.reviewMask.width * fixture.reviewMask.height) {
      throw new Error("The Ridgeway review-mask projection has an invalid compact grid.");
    }
  }
  return fixture;
}

export function hypothesisRankRange(portfolio: RidgewayPortfolio) {
  const ranks = portfolio.hypothesisRanks;
  if (!ranks) return null;
  const entries = RIDGEWAY_HYPOTHESIS_IDS.map((hypothesisId) => ({
    hypothesisId,
    ...ranks[hypothesisId],
  }));
  const best = [...entries].sort((left, right) => left.rank - right.rank || left.hypothesisId.localeCompare(right.hypothesisId))[0];
  const worst = [...entries].sort((left, right) => right.rank - left.rank || left.hypothesisId.localeCompare(right.hypothesisId))[0];
  return {
    best,
    worst,
    highestScore: Math.max(...entries.map((item) => item.score)),
    lowestScore: Math.min(...entries.map((item) => item.score)),
  };
}

export function fixtureIsManifestVerified(fixture: RidgewayFixture, expectedManifestSha256: string | null): boolean {
  return Boolean(
    fixture.releaseReplayEligible
    && fixture.evidenceState === "VERIFIED_DIRECT_EVIDENCE"
    && fixture.manifestSha256
    && expectedManifestSha256
    && fixture.manifestSha256 === expectedManifestSha256
    && sha256Schema.safeParse(expectedManifestSha256).success,
  );
}

export function scenarioFor(fixture: RidgewayFixture, result: RidgewayScenarioResult) {
  const scenario = fixture.scenarios.find((item) => item.id === result.scenarioId);
  if (!scenario) throw new Error(`Unknown fixture scenario ${result.scenarioId}.`);
  return scenario;
}

function pinnedBaselinePortfolioId(fixture: RidgewayFixture, verified: boolean): string | undefined {
  const baselinePortfolioId = fixture.baselinePortfolioId ?? undefined;
  if (verified && !baselinePortfolioId) {
    throw new Error("The verified fixture manifest has no pinned baseline portfolio identity.");
  }
  return baselinePortfolioId;
}

export function evidenceFor(
  fixture: RidgewayFixture,
  portfolio: RidgewayPortfolio,
  verified: boolean,
  result?: RidgewayScenarioResult,
): EvidenceSelection {
  const controlling = result ?? portfolio.scenarioResults.find((item) => item.scenarioId === portfolio.worstScenarioId) ?? portfolio.scenarioResults[0];
  const scenario = controlling ? scenarioFor(fixture, controlling) : fixture.scenarios[0];
  const baselinePortfolioId = pinnedBaselinePortfolioId(fixture, verified);
  return {
    claimId: controlling && portfolio.id ? `ECO-CLM-${portfolio.id}-${controlling.scenarioId}` : "ECO-CLAIM-NOT-PUBLISHED",
    title: `${portfolio.name} · ${scenario.hypothesis}`,
    value: controlling ? controlling.resistanceChangePct.toFixed(1) : "Not published",
    units: controlling ? "% baseline-relative effective resistance" : "direct engine evidence required",
    status: verified ? "verified" : "projection",
    portfolioId: portfolio.id ?? `unassigned:${portfolio.projectionKey}`,
    scenarioId: scenario.id,
    runId: controlling?.runId ?? "not-published",
    baselineRunId: controlling && baselinePortfolioId
      ? `matrix_${baselinePortfolioId}_${controlling.scenarioId}`
      : undefined,
    reducer: "(portfolio effective resistance − baseline effective resistance) ÷ baseline × 100",
    assumption: "Resistance surfaces, focal areas, and intervention effects are synthetic reviewed inputs for teaching only.",
  };
}

export function pairEvidenceFor(
  fixture: RidgewayFixture,
  portfolio: RidgewayPortfolio,
  verified: boolean,
  result: RidgewayScenarioResult,
  pair: RidgewayPairResult,
): EvidenceSelection {
  if (!portfolio.id) throw new Error("A focal-pair claim requires a published portfolio.");
  const scenario = scenarioFor(fixture, result);
  return {
    claimId: pair.claimId,
    title: `${portfolio.name} · ${scenario.hypothesis} · ${pair.pairId}`,
    value: pair.baselineRelativeChangePct.toFixed(2),
    units: "% baseline-relative effective resistance",
    status: verified ? "verified" : "projection",
    portfolioId: portfolio.id,
    scenarioId: scenario.id,
    runId: pair.portfolioRunId,
    baselineRunId: pair.baselineRunId,
    reducer: "(portfolio effective resistance − baseline effective resistance) ÷ baseline effective resistance × 100",
    assumption: "Resistance surfaces, focal areas, and intervention effects are synthetic reviewed inputs for teaching only.",
  };
}

export function mapEvidenceFor(
  fixture: RidgewayFixture,
  portfolio: RidgewayPortfolio,
  verified: boolean,
  result: RidgewayScenarioResult,
): EvidenceSelection {
  if (!portfolio.id) throw new Error("A diagnostic map claim requires a published portfolio.");
  const scenario = scenarioFor(fixture, result);
  const baselinePortfolioId = pinnedBaselinePortfolioId(fixture, verified);
  return {
    claimId: `ECO-MAP-${portfolio.id}-${result.scenarioId}`,
    title: `${portfolio.name} · ${scenario.hypothesis} · road-mask diagnostic`,
    value: result.reviewMaskChangePct.toFixed(1),
    units: "% diagnostic review-mask current redistribution",
    status: verified ? "verified" : "projection",
    portfolioId: portfolio.id,
    scenarioId: scenario.id,
    runId: `map_${portfolio.id}_${result.scenarioId}`,
    baselineRunId: baselinePortfolioId
      ? `map_${baselinePortfolioId}_${result.scenarioId}`
      : undefined,
    reducer: "(portfolio mask-summed current − baseline mask-summed current) ÷ baseline mask-summed current × 100",
    assumption: "Current-density rasters are diagnostic model outputs, not observed or predicted animal use; NODATA cells are excluded on the common valid grid.",
  };
}

export function formatUsd(value: number | null) {
  if (value === null) return "Not published";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(value);
}
