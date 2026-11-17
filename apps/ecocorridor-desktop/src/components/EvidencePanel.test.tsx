import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { EvidenceSelection, HostBridge } from "../types";
import { EvidencePanel } from "./EvidencePanel";

const sha = (character: string) => character.repeat(64);

const selection: EvidenceSelection = {
  claimId: "claim-reference-pair-1-2",
  title: "Reference tolerance disposition · reviewed scenario · pair-1-2",
  value: true,
  units: "within_tolerance",
  status: "verified",
  portfolioId: "portfolio-no-action",
  scenarioId: "scenario-reviewed",
  runId: "direct-run-reviewed",
  baselineRunId: "direct-run-reviewed",
  reducer: "Boolean result of the reviewed tolerance formula.",
  assumption: "Numerical reproduction does not establish ecological validity.",
  source: "PROFESSIONAL_CLAIM",
};

function payload(data: unknown) {
  return {
    api_version: "1.0",
    method: "connectivity.evidence.resolve",
    data,
    data_sha256: sha("a"),
  };
}

function resolvedEvidence(value: boolean | number, units = "within_tolerance") {
  return {
    claim: {
      claim_id: selection.claimId,
      claim_kind: "DIRECT_REFERENCE_COMPARISON",
      portfolio_id: selection.portfolioId,
      scenario_id: selection.scenarioId,
      pair_id: "pair-1-2",
      value,
      units,
      reducer: selection.reducer,
      direct_run_ids: [selection.runId],
      nodes: [
        { node_id: `claim:${selection.claimId}`, node_type: "CLAIM", label: "Reviewed tolerance disposition" },
        { node_id: `run:${selection.runId}`, node_type: "DIRECT_RUN", label: "Direct Circuitscape baseline run" },
      ],
      edges: [{ from_node_id: `claim:${selection.claimId}`, to_node_id: `run:${selection.runId}`, relation: "SUPPORTED_BY" }],
      limitations: ["Reference reproduction does not validate resistance, focal-area, or action-effect assumptions."],
    },
    limitations: ["Reference reproduction does not establish ecological validity."],
  };
}

afterEach(() => {
  delete window.__ECOCORRIDOR_HOST__;
});

test("formats a selection-side tolerance Boolean without exposing raw true or false", () => {
  render(<EvidencePanel open selection={{ ...selection, value: false }} fixture={null} verified publicMode projectHandle={null} onClose={vi.fn()} />);

  expect(screen.getByText("Outside tolerance")).toBeInTheDocument();
  expect(screen.getByText("reviewed Boolean tolerance disposition")).toBeInTheDocument();
  expect(screen.queryByText("false", { exact: true })).not.toBeInTheDocument();
});

test("renders the authoritative Boolean reference disposition instead of a table-only numeric value", async () => {
  const request = vi.fn(async (method: string) => payload(method === "assistant.status" ? { configured: false } : resolvedEvidence(true)));
  window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

  render(<EvidencePanel open selection={selection} fixture={null} verified publicMode={false} projectHandle="opaque-project" onClose={vi.fn()} />);

  expect(await screen.findByText("Within tolerance")).toBeInTheDocument();
  expect(screen.getByText("reviewed Boolean tolerance disposition")).toBeInTheDocument();
  expect(screen.getAllByText(selection.runId!).length).toBeGreaterThanOrEqual(1);
  expect(request).toHaveBeenCalledWith("connectivity.evidence.resolve", {
    project_handle: "opaque-project",
    claim_id: selection.claimId,
  });
});

test("withholds a reference claim when the resolved value or units do not match the selection", async () => {
  window.__ECOCORRIDOR_HOST__ = {
    request: vi.fn(async (method: string) => payload(method === "assistant.status" ? { configured: false } : resolvedEvidence(100.5, "effective_resistance"))) as unknown as HostBridge["request"],
  };

  render(<EvidencePanel open selection={selection} fixture={null} verified publicMode={false} projectHandle="opaque-project" onClose={vi.fn()} />);

  expect(await screen.findByText("Withheld")).toBeInTheDocument();
  expect(screen.getByText(/does not match the selected projection or retained evidence identity/)).toBeInTheDocument();
  expect(screen.queryByText("Within tolerance")).not.toBeInTheDocument();
});

test("renders reviewed cost evidence without mislabeling it as a Circuitscape result", async () => {
  const costSelection: EvidenceSelection = {
    ...selection,
    claimId: "claim-cost-basis",
    title: "Reviewed portfolio cost basis",
    value: "Resolved by worker",
    units: "retained evidence",
    scenarioId: "",
    runId: undefined,
    baselineRunId: undefined,
    reducer: "Reviewed cost-basis reducer",
  };
  const request = vi.fn(async (method: string) => payload(method === "assistant.status" ? { configured: false } : {
    claim: {
      claim_id: costSelection.claimId,
      claim_kind: "COST",
      portfolio_id: costSelection.portfolioId,
      scenario_id: null,
      pair_id: null,
      value: 275000,
      units: "USD_2026_PRESENT_VALUE",
      text: "Reviewed present-value screening cost.",
      direct_run_ids: [],
      nodes: [
        { node_id: `claim:${costSelection.claimId}`, node_type: "CLAIM", label: "Reviewed cost evidence" },
        { node_id: "cost:reviewed", node_type: "COST_BASIS", label: "Reviewed cost basis" },
      ],
      edges: [{ from_node_id: `claim:${costSelection.claimId}`, to_node_id: "cost:reviewed", relation: "DERIVED_FROM" }],
      limitations: ["Cost evidence is decision evidence, not a Circuitscape output."],
    },
    limitations: ["Cost evidence is decision evidence, not a Circuitscape output."],
  }));
  window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

  render(<EvidencePanel open selection={costSelection} fixture={null} verified publicMode={false} projectHandle="opaque-project" onClose={vi.fn()} />);

  expect(await screen.findByText("Worker decision evidence resolved")).toBeInTheDocument();
  expect(screen.getByText(/cost · not a solver output/i)).toBeInTheDocument();
  expect(screen.getByText("Reviewed non-solver basis")).toBeInTheDocument();
  expect(screen.queryByText(/Direct-run reference/)).not.toBeInTheDocument();
  expect(screen.getByText(/It is not a Circuitscape output/i)).toBeInTheDocument();
});

test("resolves a manifest-bound Ridgeway decision record without requiring solver runs", async () => {
  const guidedSelection: EvidenceSelection = {
    ...selection,
    claimId: "ECO-COST-portfolio-over-budget",
    title: "Synthetic screening cost record",
    value: true,
    units: "retained_review_basis",
    scenarioId: "",
    runId: undefined,
    baselineRunId: undefined,
    source: "GUIDED_PROJECTION",
  };
  const request = vi.fn(async () => payload({
    claim: {
      claim_id: guidedSelection.claimId,
      claim_kind: "GUIDED_DECISION_RECORD",
      portfolio_id: guidedSelection.portfolioId,
      scenario_id: null,
      pair_id: null,
      value: 275000,
      units: "USD",
      reducer: "Synthetic screening cost compared with the retained budget rule.",
      text: "Manifest-bound synthetic decision record used by the Review Brief.",
      direct_run_ids: [],
      nodes: [
        { node_id: "record:cost", node_type: "GUIDED_DECISION_RECORD", label: "Synthetic cost basis" },
        { node_id: "manifest:ridgeway", node_type: "EVIDENCE_MANIFEST", label: "Pinned Ridgeway evidence manifest" },
      ],
      edges: [{ from_node_id: "record:cost", to_node_id: "manifest:ridgeway", relation: "BOUND_TO_PINNED_FIXTURE" }],
      limitations: ["Synthetic screening cost is not a bid or approval."],
    },
    limitations: ["Synthetic screening cost is not a bid or approval."],
  }));
  window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

  render(<EvidencePanel open selection={guidedSelection} fixture={null} verified publicMode={false} projectHandle="opaque-project" showAssistant={false} onClose={vi.fn()} />);

  expect(await screen.findByText("275000.0")).toBeInTheDocument();
  expect(screen.getByText(/guided decision record · not a solver output/i)).toBeInTheDocument();
  expect(screen.queryByText(/Direct-run reference/)).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Evidence Assistant" })).not.toBeInTheDocument();
  expect(request).toHaveBeenCalledTimes(1);
});

test("does not resolve a professional map identity that has not been verified", async () => {
  const request = vi.fn(async (method: string) => {
    if (method === "assistant.status") return payload({ configured: false });
    return payload(resolvedEvidence(12.5, "% diagnostic review-mask current redistribution"));
  });
  window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

  render(<EvidencePanel
    open
    selection={{
      ...selection,
      claimId: "claim-map-unverified",
      title: "Direct current-map diagnostic · reviewed scenario",
      value: "Resolved from retained map claim",
      units: "diagnostic current redistribution",
      status: "review",
      reducer: "Configured review-mask reducer over same-scenario retained current rasters",
      assumption: "Current density is a model diagnostic, not observed or predicted animal use.",
    }}
    fixture={null}
    verified
    publicMode={false}
    projectHandle="opaque-project"
    onClose={vi.fn()}
  />);

  expect(await screen.findByText("Withheld")).toBeInTheDocument();
  expect(screen.getByText(/verified claim identity is unavailable/i)).toBeInTheDocument();
  expect(request).not.toHaveBeenCalledWith("connectivity.evidence.resolve", expect.anything());
});
