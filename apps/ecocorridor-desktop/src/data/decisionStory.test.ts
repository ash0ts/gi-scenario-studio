import {
  buildRedactedRidgewayDecisionStory,
  serializeRedactedRidgewayDecisionStory,
} from "@ecocorridor-scenario-studio/evidence";

import { publicRidgewayFixture } from "./publicRidgeway";

describe("redacted Ridgeway decision story", () => {
  it("keeps each reported value attached to direct-run evidence without exporting raster cells", async () => {
    const story = await buildRedactedRidgewayDecisionStory(publicRidgewayFixture);

    expect(story.schema_version).toBe("ecocorridor-redacted-decision-story/v1.1");
    expect(story.evidence_identity.manifest_sha256).toBe(publicRidgewayFixture.manifestSha256);
    expect(story.evidence_identity.projection_value_sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(story.portfolios).toHaveLength(4);
    expect(story.scenarios).toHaveLength(12);
    expect(story.current_raster_artifacts).toHaveLength(60);

    for (const portfolio of story.portfolios) {
      expect(portfolio.scenario_results).toHaveLength(12);
      const weakest = portfolio.scenario_results.find(
        (result) => result.scenario_id === portfolio.worst_scenario.scenario_id,
      );
      expect(weakest?.reducer.controlling_pair_claim_id).toBe(
        portfolio.worst_scenario.reducer_claim_id,
      );
      for (const result of portfolio.scenario_results) {
        expect(result.pair_evidence).not.toHaveLength(0);
        expect(result.review_mask_diagnostic.baseline_map_run_id).toContain(
          publicRidgewayFixture.baselinePortfolioId,
        );
        expect(result.review_mask_diagnostic.portfolio_map_run_id).toContain(
          portfolio.portfolio_id,
        );
        expect(result.review_mask_diagnostic.baseline_map_run_id).not.toBe(
          result.review_mask_diagnostic.portfolio_map_run_id,
        );
        expect(result.review_mask_diagnostic.baseline_current_raster_sha256).toMatch(
          /^[a-f0-9]{64}$/,
        );
        expect(result.review_mask_diagnostic.portfolio_current_raster_sha256).toMatch(
          /^[a-f0-9]{64}$/,
        );
        for (const pair of result.pair_evidence) {
          expect(pair.claim_id).not.toBe("");
          expect(pair.baseline_direct_run_id).not.toBe("");
          expect(pair.portfolio_direct_run_id).not.toBe("");
          expect(pair.baseline_matrix_sha256).toMatch(/^[a-f0-9]{64}$/);
          expect(pair.portfolio_matrix_sha256).toMatch(/^[a-f0-9]{64}$/);
          expect(pair.qa_status).toBe("ACCEPTED_IN_VERIFIED_FIXTURE_MANIFEST");
        }
      }
    }

    const serialized = await serializeRedactedRidgewayDecisionStory(publicRidgewayFixture);
    expect(serialized).not.toContain('"cells"');
    expect(serialized).not.toMatch(/\/Users\/|\/private\/|[A-Za-z]:\\Users\\/);
    expect(serialized).not.toMatch(/api[_-]?key|credential|authorization[_-]?header/i);
  });

  it("fails closed when the fixture identity or direct evidence is altered", async () => {
    const alteredIdentity = structuredClone(publicRidgewayFixture);
    alteredIdentity.manifestSha256 = "0".repeat(64);
    await expect(buildRedactedRidgewayDecisionStory(alteredIdentity)).rejects.toThrow(
      /exact reviewed CC0 projection/i,
    );

    const alteredRun = structuredClone(publicRidgewayFixture);
    alteredRun.portfolios[0].scenarioResults[0].pairResults[0].portfolioRunId =
      "detached-run";
    await expect(buildRedactedRidgewayDecisionStory(alteredRun)).rejects.toThrow(
      /exact reviewed CC0 projection/i,
    );

    const alteredCost = structuredClone(publicRidgewayFixture);
    alteredCost.portfolios[0].costUsd = (alteredCost.portfolios[0].costUsd ?? 0) + 1;
    await expect(buildRedactedRidgewayDecisionStory(alteredCost)).rejects.toThrow(
      /exact reviewed CC0 projection/i,
    );
  });
});
