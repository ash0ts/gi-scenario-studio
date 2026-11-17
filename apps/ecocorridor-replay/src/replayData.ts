import projection from "../../../benchmarks/ecocorridor/ridgeway/ui_projection.json";

export interface RidgewayScenario {
  id: string;
  guild: string;
  purpose: "target" | "safeguard";
  hypothesis: string;
  futureCondition: string;
  maturity: string;
  includedPairs: number;
}

export interface RidgewayPairResult {
  pairId: string;
  baselineEffectiveResistance: number;
  portfolioEffectiveResistance: number;
  baselineRelativeChangePct: number;
  passed: boolean;
  claimId: string;
  baselineRunId: string;
  portfolioRunId: string;
  baselineMatrixSha256: string;
  portfolioMatrixSha256: string;
}

export interface RidgewayScenarioResult {
  scenarioId: string;
  resistanceChangePct: number;
  ruleStatus: "pass" | "fail" | "review";
  reviewMaskChangePct: number;
  runId: string;
  baselineMapRunId: string;
  portfolioMapRunId: string;
  pairResults: RidgewayPairResult[];
}

export interface RidgewayCurrentRaster {
  runId: string;
  portfolioId: string;
  scenarioId: string;
  sha256: string;
  width: number;
  height: number;
  declaredNodata: number;
  nodataTopologySha256: string;
  minimumCurrent: number;
  maximumCurrent: number;
  cells: Array<number | null>;
}

export interface RidgewayReviewMaskProjection {
  maskId: string;
  sha256: string;
  width: number;
  height: number;
  cells: Array<0 | 1>;
  interpretation: string;
}

export interface RidgewayPortfolio {
  projectionKey: string;
  id: string | null;
  name: string;
  role: "caution" | "carry-forward" | "over-budget" | "brittle";
  costUsd: number | null;
  ruleStatus: "pass" | "fail" | "review" | "unavailable";
  passCount: number | null;
  requiredCount: number;
  worstResistanceChangePct: number | null;
  worstScenarioId: string | null;
  roadMaskFlag: boolean | null;
  nextAction: string;
  optionSummary: string;
  hypothesisRanks: Record<string, { rank: number; score: number }> | null;
  scenarioResults: RidgewayScenarioResult[];
}

export const RIDGEWAY_HYPOTHESIS_IDS = [
  "h1_low_barrier",
  "h2_moderate_barrier",
  "h3_high_barrier",
  "h4_future_pressure",
] as const;

export const RIDGEWAY_HYPOTHESIS_RANKING_METRIC = "Minimum required target-pair resistance reduction (%) within each resistance hypothesis; rank 1 is the highest score among the 128 directly evaluated portfolios.";

export interface RidgewayFixture {
  schemaVersion: "ecocorridor-replay/v1.1" | "ecocorridor-replay/v1.2";
  fixtureId: "ridgeway_crossing";
  baselinePortfolioId: string | null;
  title: string;
  cc0: true;
  manifestSha256: string | null;
  manifestVersion: string | null;
  evidenceState: "ENGINE_RUN_REQUIRED" | "VERIFIED_DIRECT_EVIDENCE";
  resourceObservability: {
    jobsWithObservations: number;
    jobsWithoutObservations: number;
  } | null;
  releaseReplayEligible: boolean;
  releaseBlocker: string;
  projectionGeneratedAt: string;
  disclaimer: string;
  decisionBrief: {
    question: string;
    budgetUsd: number;
    targetRule: string;
    safeguardRule: string;
    nonGoals: string[];
  };
  study: {
    portfolioCount: number;
    scenarioCount: number;
    matrixJobCount: number;
    mapJobCount: number;
    coreCount: number;
    interventionSiteCount: number;
    hypothesisRankingMetric: string | null;
  };
  currentRasters: RidgewayCurrentRaster[];
  reviewMask: RidgewayReviewMaskProjection | null;
  scenarios: RidgewayScenario[];
  portfolios: RidgewayPortfolio[];
}

const SHA256 = /^[a-f0-9]{64}$/;
const PORTFOLIO_ID = /^portfolio_[a-f0-9]{20}$/;

function record(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function string(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`${label} must be a non-empty string.`);
  return value;
}

function finite(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${label} must be a finite number.`);
  return value;
}

function integer(value: unknown, label: string, minimum = 0): number {
  const parsed = finite(value, label);
  if (!Number.isInteger(parsed) || parsed < minimum) throw new Error(`${label} must be an integer of at least ${minimum}.`);
  return parsed;
}

function array(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${label} must be an array.`);
  return value;
}

function nullableString(value: unknown, label: string): string | null {
  return value === null ? null : string(value, label);
}

function nullableFinite(value: unknown, label: string): number | null {
  return value === null ? null : finite(value, label);
}

function parseScenario(value: unknown, index: number): RidgewayScenario {
  const item = record(value, `scenario ${index + 1}`);
  const purpose = string(item.purpose, `scenario ${index + 1} purpose`);
  if (purpose !== "target" && purpose !== "safeguard") throw new Error(`scenario ${index + 1} has an unsupported purpose.`);
  return {
    id: string(item.id, `scenario ${index + 1} id`),
    guild: string(item.guild, `scenario ${index + 1} guild`),
    purpose,
    hypothesis: string(item.hypothesis, `scenario ${index + 1} hypothesis`),
    futureCondition: string(item.futureCondition, `scenario ${index + 1} future condition`),
    maturity: string(item.maturity, `scenario ${index + 1} maturity`),
    includedPairs: integer(item.includedPairs, `scenario ${index + 1} included pairs`, 1),
  };
}

function parsePair(value: unknown, label: string): RidgewayPairResult {
  const item = record(value, label);
  if (typeof item.passed !== "boolean") throw new Error(`${label} must declare its rule result.`);
  const baselineMatrixSha256 = string(item.baselineMatrixSha256, `${label} baseline matrix SHA-256`);
  const portfolioMatrixSha256 = string(item.portfolioMatrixSha256, `${label} portfolio matrix SHA-256`);
  if (!SHA256.test(baselineMatrixSha256) || !SHA256.test(portfolioMatrixSha256)) throw new Error(`${label} has an invalid matrix digest.`);
  return {
    pairId: string(item.pairId, `${label} pair id`),
    baselineEffectiveResistance: finite(item.baselineEffectiveResistance, `${label} baseline resistance`),
    portfolioEffectiveResistance: finite(item.portfolioEffectiveResistance, `${label} portfolio resistance`),
    baselineRelativeChangePct: finite(item.baselineRelativeChangePct, `${label} relative change`),
    passed: item.passed,
    claimId: string(item.claimId, `${label} claim id`),
    baselineRunId: string(item.baselineRunId, `${label} baseline run id`),
    portfolioRunId: string(item.portfolioRunId, `${label} portfolio run id`),
    baselineMatrixSha256,
    portfolioMatrixSha256,
  };
}

function parseResult(value: unknown, label: string): RidgewayScenarioResult {
  const item = record(value, label);
  const ruleStatus = string(item.ruleStatus, `${label} rule status`);
  if (ruleStatus !== "pass" && ruleStatus !== "fail" && ruleStatus !== "review") throw new Error(`${label} has an unsupported rule status.`);
  return {
    scenarioId: string(item.scenarioId, `${label} scenario id`),
    resistanceChangePct: finite(item.resistanceChangePct, `${label} resistance change`),
    ruleStatus,
    reviewMaskChangePct: finite(item.reviewMaskChangePct, `${label} review-mask change`),
    runId: string(item.runId, `${label} run id`),
    baselineMapRunId: string(item.baselineMapRunId, `${label} baseline map run id`),
    portfolioMapRunId: string(item.portfolioMapRunId, `${label} portfolio map run id`),
    pairResults: array(item.pairResults, `${label} pair results`).map((pair, index) => parsePair(pair, `${label} pair ${index + 1}`)),
  };
}

function parseHypothesisRanks(value: unknown, label: string): RidgewayPortfolio["hypothesisRanks"] {
  if (value === null) return null;
  const ranks = record(value, label);
  const observedIds = Object.keys(ranks).sort();
  const expectedIds = [...RIDGEWAY_HYPOTHESIS_IDS].sort();
  if (observedIds.length !== expectedIds.length || observedIds.some((id, index) => id !== expectedIds[index])) {
    throw new Error(`${label} must contain the four pinned Ridgeway resistance hypotheses.`);
  }
  return Object.fromEntries(RIDGEWAY_HYPOTHESIS_IDS.map((hypothesisId) => {
    const observation = record(ranks[hypothesisId], `${label} ${hypothesisId}`);
    return [hypothesisId, {
      rank: integer(observation.rank, `${label} ${hypothesisId} rank`, 1),
      score: finite(observation.score, `${label} ${hypothesisId} score`),
    }];
  }));
}

function parsePortfolio(value: unknown, index: number): RidgewayPortfolio {
  const item = record(value, `portfolio ${index + 1}`);
  const role = string(item.role, `portfolio ${index + 1} role`);
  if (!(["caution", "carry-forward", "over-budget", "brittle"] as const).includes(role as RidgewayPortfolio["role"])) {
    throw new Error(`portfolio ${index + 1} has an unsupported role.`);
  }
  const ruleStatus = string(item.ruleStatus, `portfolio ${index + 1} rule status`);
  if (!(["pass", "fail", "review", "unavailable"] as const).includes(ruleStatus as RidgewayPortfolio["ruleStatus"])) {
    throw new Error(`portfolio ${index + 1} has an unsupported rule status.`);
  }
  if (item.roadMaskFlag !== null && typeof item.roadMaskFlag !== "boolean") throw new Error(`portfolio ${index + 1} has an invalid road-mask flag.`);
  const hypothesisRanks = parseHypothesisRanks(item.hypothesisRanks, `portfolio ${index + 1} hypothesis ranks`);
  return {
    projectionKey: string(item.projectionKey, `portfolio ${index + 1} projection key`),
    id: nullableString(item.id, `portfolio ${index + 1} id`),
    name: string(item.name, `portfolio ${index + 1} name`),
    role: role as RidgewayPortfolio["role"],
    costUsd: nullableFinite(item.costUsd, `portfolio ${index + 1} cost`),
    ruleStatus: ruleStatus as RidgewayPortfolio["ruleStatus"],
    passCount: item.passCount === null ? null : integer(item.passCount, `portfolio ${index + 1} pass count`),
    requiredCount: integer(item.requiredCount, `portfolio ${index + 1} required count`, 1),
    worstResistanceChangePct: nullableFinite(item.worstResistanceChangePct, `portfolio ${index + 1} weakest change`),
    worstScenarioId: nullableString(item.worstScenarioId, `portfolio ${index + 1} weakest scenario`),
    roadMaskFlag: item.roadMaskFlag as boolean | null,
    nextAction: string(item.nextAction, `portfolio ${index + 1} next action`),
    optionSummary: string(item.optionSummary, `portfolio ${index + 1} option summary`),
    hypothesisRanks,
    scenarioResults: array(item.scenarioResults, `portfolio ${index + 1} scenario results`).map((result, resultIndex) => parseResult(result, `portfolio ${index + 1} result ${resultIndex + 1}`)),
  };
}

function parseRaster(value: unknown, index: number): RidgewayCurrentRaster {
  const item = record(value, `current raster ${index + 1}`);
  const cells = array(item.cells, `current raster ${index + 1} cells`).map((cell) => cell === null ? null : finite(cell, `current raster ${index + 1} cell`));
  const width = integer(item.width, `current raster ${index + 1} width`, 1);
  const height = integer(item.height, `current raster ${index + 1} height`, 1);
  const sha256 = string(item.sha256, `current raster ${index + 1} SHA-256`);
  const nodataTopologySha256 = string(item.nodataTopologySha256, `current raster ${index + 1} NODATA SHA-256`);
  if (cells.length !== width * height || !SHA256.test(sha256) || !SHA256.test(nodataTopologySha256)) throw new Error(`current raster ${index + 1} has invalid dimensions or digests.`);
  return {
    runId: string(item.runId, `current raster ${index + 1} run id`),
    portfolioId: string(item.portfolioId, `current raster ${index + 1} portfolio id`),
    scenarioId: string(item.scenarioId, `current raster ${index + 1} scenario id`),
    sha256,
    width,
    height,
    declaredNodata: finite(item.declaredNodata, `current raster ${index + 1} NODATA`),
    nodataTopologySha256,
    minimumCurrent: finite(item.minimumCurrent, `current raster ${index + 1} minimum current`),
    maximumCurrent: finite(item.maximumCurrent, `current raster ${index + 1} maximum current`),
    cells,
  };
}

function parseReviewMask(value: unknown): RidgewayReviewMaskProjection | null {
  if (value === null || value === undefined) return null;
  const item = record(value, "review mask");
  const width = integer(item.width, "review mask width", 1);
  const height = integer(item.height, "review mask height", 1);
  const cells = array(item.cells, "review mask cells").map((cell) => {
    if (cell !== 0 && cell !== 1) throw new Error("review mask cells must be binary.");
    return cell;
  });
  const sha256 = string(item.sha256, "review mask SHA-256");
  if (cells.length !== width * height || !SHA256.test(sha256)) throw new Error("review mask has invalid dimensions or digest.");
  return {
    maskId: string(item.maskId, "review mask id"),
    sha256,
    width,
    height,
    cells,
    interpretation: string(item.interpretation, "review mask interpretation"),
  };
}

export function parsePublicRidgewayFixture(value: unknown): RidgewayFixture {
  const item = record(value, "Ridgeway public projection");
  const schemaVersion = string(item.schemaVersion, "Ridgeway schema version");
  if (schemaVersion !== "ecocorridor-replay/v1.1" && schemaVersion !== "ecocorridor-replay/v1.2") throw new Error("Ridgeway public projection has an unsupported schema version.");
  if (item.fixtureId !== "ridgeway_crossing" || item.cc0 !== true) throw new Error("Ridgeway public projection must be the CC0 synthetic fixture.");
  if (typeof item.releaseReplayEligible !== "boolean") throw new Error("Ridgeway public projection must declare release eligibility.");
  if (typeof item.releaseBlocker !== "string") throw new Error("Ridgeway release blocker must be text.");

  const brief = record(item.decisionBrief, "Ridgeway decision brief");
  const study = record(item.study, "Ridgeway study");
  const scenarios = array(item.scenarios, "Ridgeway scenarios").map(parseScenario);
  const portfolios = array(item.portfolios, "Ridgeway portfolios").map(parsePortfolio);
  if (scenarios.length !== 12 || new Set(scenarios.map(({ id }) => id)).size !== 12 || portfolios.length !== 4) {
    throw new Error("Ridgeway public projection requires 12 unique scenarios and four teaching roles.");
  }
  const currentRasters = array(item.currentRasters ?? [], "Ridgeway current rasters").map(parseRaster);
  const reviewMask = parseReviewMask(item.reviewMask);
  const fixture: RidgewayFixture = {
    schemaVersion,
    fixtureId: "ridgeway_crossing",
    baselinePortfolioId: nullableString(item.baselinePortfolioId, "Ridgeway baseline portfolio id"),
    title: string(item.title, "Ridgeway title"),
    cc0: true,
    manifestSha256: nullableString(item.manifestSha256, "Ridgeway manifest SHA-256"),
    manifestVersion: nullableString(item.manifestVersion, "Ridgeway manifest version"),
    evidenceState: string(item.evidenceState, "Ridgeway evidence state") as RidgewayFixture["evidenceState"],
    resourceObservability: item.resourceObservability === null || item.resourceObservability === undefined ? null : record(item.resourceObservability, "Ridgeway resource observations") as RidgewayFixture["resourceObservability"],
    releaseReplayEligible: item.releaseReplayEligible,
    releaseBlocker: item.releaseBlocker,
    projectionGeneratedAt: string(item.projectionGeneratedAt, "Ridgeway projection timestamp"),
    disclaimer: string(item.disclaimer, "Ridgeway disclaimer"),
    decisionBrief: {
      question: string(brief.question, "Ridgeway decision question"),
      budgetUsd: finite(brief.budgetUsd, "Ridgeway budget"),
      targetRule: string(brief.targetRule, "Ridgeway target rule"),
      safeguardRule: string(brief.safeguardRule, "Ridgeway safeguard rule"),
      nonGoals: array(brief.nonGoals, "Ridgeway non-goals").map((nonGoal, index) => string(nonGoal, `Ridgeway non-goal ${index + 1}`)),
    },
    study: {
      portfolioCount: integer(study.portfolioCount, "Ridgeway portfolio count", 1),
      scenarioCount: integer(study.scenarioCount, "Ridgeway scenario count", 1),
      matrixJobCount: integer(study.matrixJobCount, "Ridgeway matrix-job count", 1),
      mapJobCount: integer(study.mapJobCount, "Ridgeway map-job count"),
      coreCount: integer(study.coreCount, "Ridgeway core count", 1),
      interventionSiteCount: integer(study.interventionSiteCount, "Ridgeway intervention-site count", 1),
      hypothesisRankingMetric: nullableString(study.hypothesisRankingMetric, "Ridgeway hypothesis ranking metric"),
    },
    currentRasters,
    reviewMask,
    scenarios,
    portfolios,
  };

  if (fixture.releaseReplayEligible) validateVerifiedFixture(fixture);
  else validateUnavailableFixture(fixture);
  return fixture;
}

function validateUnavailableFixture(fixture: RidgewayFixture): void {
  if (
    fixture.schemaVersion !== "ecocorridor-replay/v1.1"
    || fixture.evidenceState !== "ENGINE_RUN_REQUIRED"
    || fixture.manifestSha256 !== null
    || fixture.baselinePortfolioId !== null
    || !fixture.releaseBlocker
    || fixture.portfolios.some((portfolio) => portfolio.id !== null || portfolio.costUsd !== null || portfolio.hypothesisRanks !== null || portfolio.scenarioResults.length !== 0)
  ) {
    throw new Error("Unavailable Ridgeway evidence must remain fail closed.");
  }
}

function validateVerifiedFixture(fixture: RidgewayFixture): void {
  if (
    fixture.schemaVersion !== "ecocorridor-replay/v1.2"
    || fixture.evidenceState !== "VERIFIED_DIRECT_EVIDENCE"
    || !fixture.manifestSha256
    || !SHA256.test(fixture.manifestSha256)
    || !fixture.manifestVersion
    || !fixture.baselinePortfolioId
    || !PORTFOLIO_ID.test(fixture.baselinePortfolioId)
    || fixture.study.portfolioCount !== 128
    || fixture.study.scenarioCount !== 12
    || fixture.study.matrixJobCount !== 1_536
    || fixture.study.mapJobCount !== 60
    || fixture.study.hypothesisRankingMetric !== RIDGEWAY_HYPOTHESIS_RANKING_METRIC
    || !fixture.reviewMask
    || fixture.currentRasters.length !== 60
    || fixture.resourceObservability?.jobsWithObservations !== 1_596
    || fixture.resourceObservability.jobsWithoutObservations !== 0
  ) {
    throw new Error("Verified Ridgeway evidence is incomplete or inconsistent.");
  }
  const scenarioById = new Map(fixture.scenarios.map((scenario) => [scenario.id, scenario]));
  const portfolioIds = new Set<string>();
  for (const portfolio of fixture.portfolios) {
    if (!portfolio.id || !PORTFOLIO_ID.test(portfolio.id) || portfolio.id === fixture.baselinePortfolioId || portfolioIds.has(portfolio.id)) throw new Error("Verified Ridgeway teaching portfolio identities must be unique and non-baseline.");
    portfolioIds.add(portfolio.id);
    if (portfolio.costUsd === null || portfolio.worstResistanceChangePct === null || !portfolio.worstScenarioId || !portfolio.hypothesisRanks || portfolio.scenarioResults.length !== 12) throw new Error(`${portfolio.id} does not retain a complete teaching result.`);
    if (Object.values(portfolio.hypothesisRanks).some((observation) => observation.rank > fixture.study.portfolioCount)) throw new Error(`${portfolio.id} has a hypothesis rank outside the pinned direct-study universe.`);
    const resultScenarioIds = new Set<string>();
    for (const result of portfolio.scenarioResults) {
      const scenario = scenarioById.get(result.scenarioId);
      if (!scenario || resultScenarioIds.has(result.scenarioId)) throw new Error(`${portfolio.id} has an invalid scenario ensemble.`);
      resultScenarioIds.add(result.scenarioId);
      const expectedBaselineRunId = `matrix_${fixture.baselinePortfolioId}_${result.scenarioId}`;
      const expectedPortfolioRunId = `matrix_${portfolio.id}_${result.scenarioId}`;
      if (
        result.runId !== expectedPortfolioRunId
        || result.baselineMapRunId !== `map_${fixture.baselinePortfolioId}_${result.scenarioId}`
        || result.portfolioMapRunId !== `map_${portfolio.id}_${result.scenarioId}`
        || result.pairResults.length !== scenario.includedPairs
      ) throw new Error(`${portfolio.id} has incomplete direct scenario evidence.`);
      const pairIds = new Set<string>();
      for (const pair of result.pairResults) {
        const recalculated = (pair.portfolioEffectiveResistance - pair.baselineEffectiveResistance) / pair.baselineEffectiveResistance * 100;
        const expectedPass = recalculated <= (scenario.purpose === "safeguard" ? 0 : -3);
        if (
          pairIds.has(pair.pairId)
          || pair.baselineEffectiveResistance <= 0
          || pair.portfolioEffectiveResistance <= 0
          || pair.claimId !== `ECO-PAIR-${portfolio.id}-${result.scenarioId}-${pair.pairId}`
          || pair.baselineRunId !== expectedBaselineRunId
          || pair.portfolioRunId !== expectedPortfolioRunId
          || Math.abs(pair.baselineRelativeChangePct - recalculated) > 1e-9
          || pair.passed !== expectedPass
        ) throw new Error(`${portfolio.id} has inconsistent focal-pair evidence.`);
        pairIds.add(pair.pairId);
      }
      const controlling = Math.max(...result.pairResults.map((pair) => pair.baselineRelativeChangePct));
      if (Math.abs(controlling - result.resistanceChangePct) > 1e-9) throw new Error(`${portfolio.id} has an inconsistent scenario reducer.`);
    }
  }
  const expectedRasterIds = new Set([
    ...fixture.scenarios.map((scenario) => `map_${fixture.baselinePortfolioId}_${scenario.id}`),
    ...fixture.portfolios.flatMap((portfolio) => fixture.scenarios.map((scenario) => `map_${portfolio.id}_${scenario.id}`)),
  ]);
  const actualRasterIds = new Set(fixture.currentRasters.map((raster) => raster.runId));
  if (actualRasterIds.size !== expectedRasterIds.size || [...expectedRasterIds].some((id) => !actualRasterIds.has(id))) throw new Error("Verified Ridgeway current-raster identities are incomplete.");
  for (const raster of fixture.currentRasters) {
    if (raster.width !== fixture.reviewMask.width || raster.height !== fixture.reviewMask.height || raster.minimumCurrent < 0 || raster.maximumCurrent < raster.minimumCurrent) throw new Error(`Current raster ${raster.runId} is incompatible with the review mask.`);
  }
}

export function loadPublicRidgewayFixture(): RidgewayFixture {
  return parsePublicRidgewayFixture(projection as unknown);
}
