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
  expect(screen.getAllByText(selection.runId).length).toBeGreaterThanOrEqual(1);
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
  expect(screen.getByText(/does not match the selected projection value and direct-run identity/)).toBeInTheDocument();
  expect(screen.queryByText("Within tolerance")).not.toBeInTheDocument();
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
