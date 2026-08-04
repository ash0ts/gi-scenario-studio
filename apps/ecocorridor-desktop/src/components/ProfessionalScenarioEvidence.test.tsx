import axe from "axe-core";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import type { HostBridge } from "../types";
import { ProfessionalScenarioEvidence } from "./ProfessionalScenarioEvidence";

const sha = (character: string) => character.repeat(64);

function response(scenarioId: string, offset = 0, mapAvailable = true) {
  const scenario = {
    scenario_id: scenarioId,
    title: scenarioId === "scenario-forest" ? "Forest guild · reviewed hypothesis" : "Safeguard guild · alternate hypothesis",
    guild_id: scenarioId === "scenario-forest" ? "guild-forest" : "guild-safeguard",
    life_stage: "adult",
    resistance_hypothesis_id: scenarioId === "scenario-forest" ? "hypothesis-reviewed-a" : "hypothesis-reviewed-b",
    focal_set_id: scenarioId === "scenario-forest" ? "focal-forest" : "focal-safeguard",
    future_condition: "present",
    maturity_horizon: "screening",
    included_pair_ids: [scenarioId === "scenario-forest" ? "pair-forest-1-2" : "pair-safeguard-3-4"],
    safeguard: scenarioId !== "scenario-forest",
    grid_id: "grid-reviewed",
    engine_mode: "RASTER_PAIRWISE" as const,
    raster_semantics: "RESISTANCE" as const,
    neighbor_rule: "EIGHT" as const,
    resistance_average: "ARITHMETIC" as const,
    precision: "double" as const,
    julia_threads: 1 as const,
    blas_threads: 1 as const,
  };
  const pairId = scenario.included_pair_ids[0];
  const baselineRunId = `run-baseline-${scenarioId}`;
  const portfolioRunId = `run-selected-${scenarioId}`;
  const run = (portfolioId: string, directRunId: string, matrixSha: string, currentSha: string | null) => ({
    direct_run_id: directRunId,
    portfolio_id: portfolioId,
    scenario_id: scenarioId,
    exact: true as const,
    engine_name: "Circuitscape" as const,
    engine_manifest_sha256: sha("a"),
    model_run_cache_key: portfolioId === "portfolio-baseline" ? sha("b") : sha("c"),
    result_sha256: portfolioId === "portfolio-baseline" ? sha("d") : sha("e"),
    matrix_sha256: matrixSha,
    current_map_sha256: currentSha,
    qa_status: "ACCEPTED" as const,
    qa_findings: [],
  });
  return {
    analysis_id: "analysis-reviewed",
    portfolio_id: "portfolio-selected",
    baseline_portfolio_id: "portfolio-baseline",
    model_revision_id: "revision-reviewed",
    scenario,
    baseline_run: run("portfolio-baseline", baselineRunId, sha("1"), mapAvailable ? sha("3") : null),
    portfolio_run: run("portfolio-selected", portfolioRunId, sha("2"), mapAvailable ? sha("4") : null),
    pair_evidence: [{
      pair_id: pairId,
      required: true,
      baseline_effective_resistance: scenarioId === "scenario-forest" ? 10 : 900,
      portfolio_effective_resistance: scenarioId === "scenario-forest" ? 9 : 990,
      baseline_relative_reduction_percent: scenarioId === "scenario-forest" ? 10 : -10,
      baseline_direct_run_id: baselineRunId,
      portfolio_direct_run_id: portfolioRunId,
      baseline_matrix_sha256: sha("1"),
      portfolio_matrix_sha256: sha("2"),
      evidence_claim_ids: [`claim-${pairId}`],
      primary_evidence_claim_id: `claim-${pairId}`,
    }],
    map_evidence: mapAvailable ? {
      status: "AVAILABLE" as const,
      grid: { grid_id: "grid-reviewed", crs: "EPSG:5070", width: 20, height: 10, transform: [30, 0, 1000, 0, -30, 2000], nodata: -9999, value_kind: "CONTINUOUS" as const, cell_area_m2: 900 },
      baseline_artifact: { direct_run_id: baselineRunId, artifact_id: `artifact:${baselineRunId}:current_map`, sha256: sha("3"), byte_size: 900, media_type: "text/plain" },
      portfolio_artifact: { direct_run_id: portfolioRunId, artifact_id: `artifact:${portfolioRunId}:current_map`, sha256: sha("4"), byte_size: 900, media_type: "text/plain" },
      evidence_claim_ids: ["claim-map-reviewed"],
      preview: {
        width: 2,
        height: 2,
        selection_method: "SYSTEMATIC_EXACT_CELL_SAMPLE" as const,
        baseline_minimum: 1,
        baseline_maximum: 4,
        portfolio_minimum: 1.1,
        portfolio_maximum: 4.4,
        cells: [
          { preview_row: 0, preview_column: 0, source_row: 2, source_column: 4, baseline_current: 1, portfolio_current: 1.1 },
          { preview_row: 0, preview_column: 1, source_row: 2, source_column: 14, baseline_current: 2, portfolio_current: 2.2 },
          { preview_row: 1, preview_column: 0, source_row: 7, source_column: 4, baseline_current: 3, portfolio_current: 3.3 },
          { preview_row: 1, preview_column: 1, source_row: 7, source_column: 14, baseline_current: 4, portfolio_current: 4.4 },
        ],
      },
      exact_cell_page: {
        offset,
        limit: 100,
        total_cell_count: 200,
        has_previous: offset > 0,
        has_next: offset + 100 < 200,
        cells: [
          { linear_index: offset, row: Math.floor(offset / 20), column: offset % 20, x_center: 1015, y_center: 1985, baseline_current: 1, portfolio_current: 1.1, portfolio_minus_baseline: 0.1 },
          { linear_index: offset + 1, row: Math.floor((offset + 1) / 20), column: (offset + 1) % 20, x_center: 1045, y_center: 1985, baseline_current: null, portfolio_current: null, portfolio_minus_baseline: null },
        ],
      },
      interpretation: "Diagnostic current redistribution; not observed or predicted animal use." as const,
    } : {
      status: "ABSENT" as const,
      absence_code: "CURRENT_MAPS_NOT_RETAINED",
      absence_reason: "Neither run retained a current raster; rerun with current-map output enabled.",
      evidence_claim_ids: [],
      interpretation: "Diagnostic current redistribution; not observed or predicted animal use." as const,
    },
    same_scenario_only: true as const,
    claim_language: "Direct Circuitscape result under documented assumptions." as const,
    limitations: ["Connectivity values are direct model outputs under documented assumptions.", "Current density is diagnostic only."],
  };
}

function installHost(mapAvailable = true) {
  const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
    if (method !== "connectivity.results.scenario_detail") throw new Error(`Unexpected method ${method}`);
    const detail = response(String(params.scenario_id), Number(params.cell_offset ?? 0), mapAvailable);
    return { api_version: "1.0", method, data: detail, data_sha256: sha("f") };
  });
  window.__ECOCORRIDOR_HOST__ = {
    request: request as unknown as HostBridge["request"],
  };
  return request;
}

afterEach(() => {
  delete window.__ECOCORRIDOR_HOST__;
});

test("keeps matrix and scrubber synchronized to one authoritative ecological scenario", async () => {
  const request = installHost();
  const onEvidence = vi.fn();
  const user = userEvent.setup();
  const { container } = render(<ProfessionalScenarioEvidence projectHandle="project-handle" analysisId="analysis-reviewed" portfolioId="portfolio-selected" scenarioIds={["scenario-forest", "scenario-safeguard"]} initialScenarioId="scenario-forest" onEvidence={onEvidence} />);

  expect(await screen.findByText("guild-forest · hypothesis-reviewed-a")).toBeInTheDocument();
  expect(screen.getByText("pair-forest-1-2")).toBeInTheDocument();
  expect(screen.queryByText("pair-safeguard-3-4")).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Focal-pair direct evidence for scenario-forest" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Exact current-raster cells for scenario-forest" })).toBeInTheDocument();
  expect(screen.getByText(`sha256:${sha("3")}`)).toBeInTheDocument();

  const slider = screen.getByRole("slider", { name: "Move the professional baseline / selected divider" });
  slider.focus();
  await user.keyboard("{ArrowRight}");
  expect(slider).toHaveFocus();

  await user.click(screen.getByRole("button", { name: "Trace focal pair" }));
  expect(onEvidence).toHaveBeenCalledWith(expect.objectContaining({ claimId: "claim-pair-forest-1-2", scenarioId: "scenario-forest", source: "PROFESSIONAL_CLAIM" }));

  await user.selectOptions(screen.getByLabelText("Scenario shown in matrix and map"), "scenario-safeguard");
  expect(await screen.findByText("guild-safeguard · hypothesis-reviewed-b")).toBeInTheDocument();
  expect(screen.getByText("pair-safeguard-3-4")).toBeInTheDocument();
  expect(screen.queryByText("pair-forest-1-2")).not.toBeInTheDocument();
  expect(request).toHaveBeenLastCalledWith("connectivity.results.scenario_detail", expect.objectContaining({ scenario_id: "scenario-safeguard", portfolio_id: "portfolio-selected", cell_offset: 0 }));

  const results = await axe.run(container);
  expect(results.violations.filter((item) => item.impact === "serious" || item.impact === "critical")).toEqual([]);
});

test("pages exact cells through the worker and leaves pair evidence visible when maps are absent", async () => {
  const request = installHost();
  const user = userEvent.setup();
  const { unmount } = render(<ProfessionalScenarioEvidence projectHandle="project-handle" analysisId="analysis-reviewed" portfolioId="portfolio-selected" scenarioIds={["scenario-forest"]} initialScenarioId="scenario-forest" onEvidence={vi.fn()} />);
  await screen.findByText("guild-forest · hypothesis-reviewed-a");
  await user.click(screen.getByRole("button", { name: "Next 100" }));
  await waitFor(() => expect(request).toHaveBeenLastCalledWith("connectivity.results.scenario_detail", expect.objectContaining({ cell_offset: 100 })));
  expect(await screen.findByText("Offset 100")).toBeInTheDocument();
  unmount();

  installHost(false);
  render(<ProfessionalScenarioEvidence projectHandle="project-handle" analysisId="analysis-reviewed" portfolioId="portfolio-selected" scenarioIds={["scenario-forest"]} initialScenarioId="scenario-forest" onEvidence={vi.fn()} />);
  expect(await screen.findByText("Map comparison was not retained")).toBeInTheDocument();
  expect(screen.getByText(/CURRENT_MAPS_NOT_RETAINED/)).toBeInTheDocument();
  expect(screen.getByText("pair-forest-1-2")).toBeInTheDocument();
  expect(screen.queryByRole("slider", { name: "Move the professional baseline / selected divider" })).not.toBeInTheDocument();
});
