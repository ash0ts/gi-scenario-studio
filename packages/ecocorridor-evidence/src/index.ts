import directEvidenceManifest from "../../../benchmarks/ecocorridor/ridgeway/direct-evidence-manifest.json";

const PINNED_MANIFEST_SHA256 = "abf53dd7fa03956e8d076286e30d1ef3e5b35abf6a06952390b5366fdfef3c1e";
const PINNED_PROJECTION_SHA256 = "57091a4776e255d597cf9561ad245c121786a5d2562e3227021c5ba38091f08e";
const PINNED_PROJECTION_VALUE_SHA256 = "e2123b012c97fcfa2c4558e5f3c5c23c618d3a5c9dfee532897e862c8902ed05";
const SHA256 = /^[a-f0-9]{64}$/;

const PERMANENT_LIMITATIONS = [
  "Synthetic connectivity screening only; Ridgeway Crossing is not a real place or species assessment.",
  "Direct Circuitscape values describe modeled effective resistance under documented assumptions, not observed or predicted animal movement.",
  "Current redistribution is a diagnostic review trigger, not crossing use or collision evidence.",
  "No collision reduction is established. No population benefit is established. No biodiversity benefit is established. No optimal corridor is established. No regulatory approval is established. No design-ready crossing is established.",
  "Costs are synthetic screening values bound to the pinned cost-basis hash; they are not Circuitscape outputs or construction estimates.",
] as const;

interface StoryPair {
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

interface StoryResult {
  scenarioId: string;
  resistanceChangePct: number;
  ruleStatus: "pass" | "fail" | "review";
  reviewMaskChangePct: number;
  runId: string;
  baselineMapRunId: string;
  portfolioMapRunId: string;
  pairResults: StoryPair[];
}

interface StoryRaster {
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

export interface RidgewayDecisionStoryFixture {
  schemaVersion: string;
  fixtureId: string;
  baselinePortfolioId: string | null;
  title: string;
  cc0: boolean;
  manifestSha256: string | null;
  manifestVersion: string | null;
  evidenceState: string;
  releaseReplayEligible: boolean;
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
  resourceObservability: unknown;
  reviewMask: null | {
    maskId: string;
    sha256: string;
    width: number;
    height: number;
    interpretation: string;
    cells: Array<0 | 1>;
  };
  currentRasters: StoryRaster[];
  scenarios: Array<{
    id: string;
    guild: string;
    purpose: "target" | "safeguard";
    hypothesis: string;
    futureCondition: string;
    maturity: string;
    includedPairs: number;
  }>;
  portfolios: Array<{
    id: string | null;
    name: string;
    role: string;
    costUsd: number | null;
    ruleStatus: string;
    passCount: number | null;
    requiredCount: number;
    worstResistanceChangePct: number | null;
    worstScenarioId: string | null;
    roadMaskFlag: boolean | null;
    nextAction: string;
    optionSummary: string;
    hypothesisRanks: Record<string, { rank: number; score: number }> | null;
    scenarioResults: StoryResult[];
  }>;
}

function fail(message: string): never {
  throw new Error(`Ridgeway redacted decision story is unavailable: ${message}`);
}

function requireSha256(value: unknown, label: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) fail(`${label} is not a SHA-256 digest`);
  return value;
}

function exactNumber(left: number, right: number): boolean {
  return Number.isFinite(left) && Number.isFinite(right) && Math.abs(left - right) <= 1e-10;
}

function stableJson(value: unknown): string {
  const normalize = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(normalize);
    if (item !== null && typeof item === "object") {
      return Object.fromEntries(
        Object.entries(item as Record<string, unknown>)
          .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
          .map(([key, child]) => [key, normalize(child)]),
      );
    }
    return item;
  };
  return JSON.stringify(normalize(value));
}

async function sha256(value: string): Promise<string> {
  if (!globalThis.crypto?.subtle) fail("the secure projection-integrity check is unavailable");
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function rasterDescriptor(raster: StoryRaster) {
  requireSha256(raster.sha256, `current raster ${raster.runId}`);
  requireSha256(raster.nodataTopologySha256, `current raster ${raster.runId} NODATA topology`);
  if (raster.cells.length !== raster.width * raster.height) fail(`current raster ${raster.runId} has inconsistent dimensions`);
  return {
    run_id: raster.runId,
    portfolio_id: raster.portfolioId,
    scenario_id: raster.scenarioId,
    artifact_sha256: raster.sha256,
    width: raster.width,
    height: raster.height,
    declared_nodata: raster.declaredNodata,
    nodata_topology_sha256: raster.nodataTopologySha256,
    minimum_current: raster.minimumCurrent,
    maximum_current: raster.maximumCurrent,
    interpretation: "Diagnostic modeled current only; not animal movement or crossing use.",
  };
}

function manifestIdentity() {
  const manifest = directEvidenceManifest;
  if (manifest.schema_version !== "ridgeway-direct-evidence/v1.2" || manifest.fixture_version !== "1.2.0" || manifest.release_replay_eligible !== true) {
    fail("the bundled direct-evidence manifest is not the reviewed v1.2 release-replay candidate");
  }
  const bundle = manifest.evidence_bundle;
  return {
    manifest_schema_version: manifest.schema_version,
    manifest_sha256: PINNED_MANIFEST_SHA256,
    projection_sha256: PINNED_PROJECTION_SHA256,
    projection_value_sha256: PINNED_PROJECTION_VALUE_SHA256,
    model_sha256: requireSha256(manifest.model_sha256, "model"),
    physics_model_sha256: requireSha256(manifest.physics_model_sha256, "physics model"),
    engine_manifest_sha256: requireSha256(manifest.engine_manifest_sha256, "engine manifest"),
    engine_environment_sha256: requireSha256(manifest.engine_environment_sha256, "engine environment"),
    workflow_manifest_sha256: requireSha256(manifest.workflow_manifest_sha256, "workflow manifest"),
    assumption_ledger_sha256: requireSha256(manifest.assumption_ledger_sha256, "assumption ledger"),
    cost_basis_sha256: requireSha256(manifest.cost_basis_sha256, "cost basis"),
    decision_snapshot_sha256: requireSha256(manifest.decision_snapshot_sha256, "decision snapshot"),
    evidence_index_sha256: requireSha256(manifest.evidence_index_sha256, "evidence index"),
    claim_safety_sha256: requireSha256(manifest.claim_safety_sha256, "claim-safety snapshot"),
    evidence_archive: {
      artifact_sha256: requireSha256(bundle.sha256, "direct-evidence archive"),
      byte_size: bundle.byte_size,
      file_count: bundle.file_count,
      internal_index_sha256: requireSha256(bundle.internal_index_sha256, "archive index"),
      packager_sha256: requireSha256(bundle.packager_sha256, "evidence packager"),
    },
  };
}

export async function buildRedactedRidgewayDecisionStory(fixture: RidgewayDecisionStoryFixture) {
  if (await sha256(stableJson(fixture)) !== PINNED_PROJECTION_VALUE_SHA256) {
    fail("the active fixture values differ from the exact reviewed CC0 projection");
  }
  const evidenceIdentity = manifestIdentity();
  if (
    fixture.fixtureId !== "ridgeway_crossing"
    || fixture.cc0 !== true
    || fixture.schemaVersion !== "ecocorridor-replay/v1.2"
    || fixture.evidenceState !== "VERIFIED_DIRECT_EVIDENCE"
    || fixture.releaseReplayEligible !== true
    || fixture.manifestSha256 !== PINNED_MANIFEST_SHA256
    || fixture.manifestVersion !== directEvidenceManifest.fixture_version
    || !fixture.baselinePortfolioId
  ) {
    fail("the active fixture is not the exact verified CC0 projection");
  }
  if (
    fixture.scenarios.length !== fixture.study.scenarioCount
    || fixture.study.scenarioCount !== 12
    || fixture.study.matrixJobCount !== 1_536
    || fixture.study.mapJobCount !== 60
    || fixture.currentRasters.length !== fixture.study.mapJobCount
    || fixture.portfolios.length !== 4
    || !fixture.reviewMask
  ) {
    fail("the pinned study, scenario, map, or teaching-portfolio ensemble is incomplete");
  }

  const scenarioById = new Map(fixture.scenarios.map((item) => [item.id, item]));
  if (scenarioById.size !== fixture.scenarios.length) fail("scenario IDs are ambiguous");
  const rasterByRunId = new Map(fixture.currentRasters.map((item) => [item.runId, item]));
  if (rasterByRunId.size !== fixture.currentRasters.length) fail("current-raster run IDs are ambiguous");
  const reviewMask = fixture.reviewMask;
  requireSha256(reviewMask.sha256, "review mask");
  if (reviewMask.cells.length !== reviewMask.width * reviewMask.height) fail("review-mask dimensions are inconsistent");

  const portfolioIds = new Set<string>();
  const portfolios = fixture.portfolios.map((portfolio) => {
    if (
      !portfolio.id
      || portfolio.id === fixture.baselinePortfolioId
      || portfolioIds.has(portfolio.id)
      || portfolio.costUsd === null
      || portfolio.worstResistanceChangePct === null
      || !portfolio.worstScenarioId
      || !portfolio.hypothesisRanks
      || portfolio.scenarioResults.length !== fixture.scenarios.length
    ) {
      fail(`teaching portfolio ${portfolio.name} is incomplete or ambiguous`);
    }
    portfolioIds.add(portfolio.id);
    const resultScenarioIds = new Set(portfolio.scenarioResults.map((item) => item.scenarioId));
    if (resultScenarioIds.size !== scenarioById.size || [...scenarioById].some(([id]) => !resultScenarioIds.has(id))) {
      fail(`portfolio ${portfolio.id} does not cover the exact scenario ensemble`);
    }

    const scenarioResults = portfolio.scenarioResults.map((result) => {
      const scenario = scenarioById.get(result.scenarioId);
      if (!scenario || result.pairResults.length !== scenario.includedPairs) fail(`scenario ${result.scenarioId} has incomplete focal-pair evidence`);
      const baselineMap = rasterByRunId.get(result.baselineMapRunId);
      const portfolioMap = rasterByRunId.get(result.portfolioMapRunId);
      if (
        !baselineMap
        || !portfolioMap
        || baselineMap.portfolioId !== fixture.baselinePortfolioId
        || portfolioMap.portfolioId !== portfolio.id
        || baselineMap.scenarioId !== result.scenarioId
        || portfolioMap.scenarioId !== result.scenarioId
      ) {
        fail(`scenario ${result.scenarioId} is detached from its retained current rasters`);
      }
      const pairIds = new Set<string>();
      const pairEvidence = result.pairResults.map((pair) => {
        if (
          pairIds.has(pair.pairId)
          || pair.portfolioRunId !== result.runId
          || !pair.claimId
          || !pair.baselineRunId
          || !pair.portfolioRunId
        ) {
          fail(`scenario ${result.scenarioId} has ambiguous pair evidence`);
        }
        pairIds.add(pair.pairId);
        requireSha256(pair.baselineMatrixSha256, `pair ${pair.pairId} baseline matrix`);
        requireSha256(pair.portfolioMatrixSha256, `pair ${pair.pairId} portfolio matrix`);
        return {
          pair_id: pair.pairId,
          claim_id: pair.claimId,
          baseline_effective_resistance_model_units: pair.baselineEffectiveResistance,
          portfolio_effective_resistance_model_units: pair.portfolioEffectiveResistance,
          baseline_relative_change_percent: pair.baselineRelativeChangePct,
          rule_passed: pair.passed,
          baseline_direct_run_id: pair.baselineRunId,
          baseline_matrix_sha256: pair.baselineMatrixSha256,
          portfolio_direct_run_id: pair.portfolioRunId,
          portfolio_matrix_sha256: pair.portfolioMatrixSha256,
          qa_status: "ACCEPTED_IN_VERIFIED_FIXTURE_MANIFEST",
        };
      });
      const controllingPair = result.pairResults.find((pair) => exactNumber(pair.baselineRelativeChangePct, result.resistanceChangePct));
      if (!controllingPair) fail(`scenario ${result.scenarioId} reducer is detached from its pair evidence`);
      return {
        scenario_id: result.scenarioId,
        rule_status: result.ruleStatus,
        reducer: {
          method: "WEAKEST_REQUIRED_FOCAL_PAIR_WITHIN_SCENARIO",
          baseline_relative_change_percent: result.resistanceChangePct,
          controlling_pair_claim_id: controllingPair.claimId,
        },
        pair_evidence: pairEvidence,
        review_mask_diagnostic: {
          current_redistribution_percent: result.reviewMaskChangePct,
          baseline_map_run_id: result.baselineMapRunId,
          baseline_current_raster_sha256: baselineMap.sha256,
          portfolio_map_run_id: result.portfolioMapRunId,
          portfolio_current_raster_sha256: portfolioMap.sha256,
          review_mask_id: reviewMask.maskId,
          review_mask_sha256: reviewMask.sha256,
          interpretation: reviewMask.interpretation,
        },
        qa_status: "ACCEPTED_IN_VERIFIED_FIXTURE_MANIFEST",
      };
    });
    const worst = scenarioResults.find((item) => item.scenario_id === portfolio.worstScenarioId);
    if (!worst || !exactNumber(worst.reducer.baseline_relative_change_percent, portfolio.worstResistanceChangePct)) {
      fail(`portfolio ${portfolio.id} weakest result is detached from its direct scenario reducer`);
    }
    return {
      portfolio_id: portfolio.id,
      evidence_role: portfolio.role,
      display_name: portfolio.name,
      option_summary: portfolio.optionSummary,
      rule_status: portfolio.ruleStatus,
      passed_scenario_count: portfolio.passCount,
      required_scenario_count: portfolio.requiredCount,
      worst_scenario: {
        scenario_id: portfolio.worstScenarioId,
        baseline_relative_change_percent: portfolio.worstResistanceChangePct,
        reducer_claim_id: worst.reducer.controlling_pair_claim_id,
      },
      hypothesis_ranks: portfolio.hypothesisRanks,
      hypothesis_rank_basis: {
        metric: fixture.study.hypothesisRankingMetric,
        directly_evaluated_portfolios: fixture.study.portfolioCount,
        evidence_index_sha256: evidenceIdentity.evidence_index_sha256,
      },
      review_mask_flag: portfolio.roadMaskFlag,
      screening_cost: {
        amount_usd: portfolio.costUsd,
        cost_basis_sha256: evidenceIdentity.cost_basis_sha256,
        engine_output: false,
      },
      disposition: portfolio.nextAction,
      scenario_results: scenarioResults,
    };
  });

  return {
    schema_version: "ecocorridor-redacted-decision-story/v1.1",
    public_scope: "CC0_SYNTHETIC_RIDGEWAY_ONLY",
    fixture: {
      fixture_id: fixture.fixtureId,
      title: fixture.title,
      baseline_portfolio_id: fixture.baselinePortfolioId,
      projection_generated_at: fixture.projectionGeneratedAt,
      evidence_state: fixture.evidenceState,
    },
    evidence_identity: evidenceIdentity,
    decision_brief: fixture.decisionBrief,
    study: fixture.study,
    scenarios: fixture.scenarios.map((item) => ({
      scenario_id: item.id,
      anonymous_guild: item.guild,
      purpose: item.purpose,
      resistance_hypothesis: item.hypothesis,
      future_condition: item.futureCondition,
      maturity_horizon: item.maturity,
      included_pair_count: item.includedPairs,
    })),
    portfolios,
    current_raster_artifacts: fixture.currentRasters.map(rasterDescriptor),
    review_mask_artifact: {
      mask_id: reviewMask.maskId,
      artifact_sha256: reviewMask.sha256,
      width: reviewMask.width,
      height: reviewMask.height,
      interpretation: reviewMask.interpretation,
    },
    resource_observability: fixture.resourceObservability,
    limitations: [fixture.disclaimer, ...PERMANENT_LIMITATIONS],
  } as const;
}

export function serializeRedactedRidgewayDecisionStory(
  fixture: RidgewayDecisionStoryFixture,
): Promise<string> {
  return buildRedactedRidgewayDecisionStory(fixture).then(
    (story) => `${JSON.stringify(story, null, 2)}\n`,
  );
}
