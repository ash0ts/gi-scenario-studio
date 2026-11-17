import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HostBridge, PlanningState, ProfessionalAssetRecord, ProfessionalModelRecords, ProfessionalProjectSession } from "../types";
import { FocalAreasScreen } from "./Screens";
import { WorkspaceDraftProvider } from "./WorkspaceDrafts";

const planning: PlanningState = {
  projectName: "Reviewed ridge screen",
  decisionQuestion: "Which concept should advance to feasibility review?",
  intendedUse: "Planning comparison only.",
  studyArea: "Generalized study area",
  reviewer: "Agency ecologist",
  targetGuild: "guild_target",
  assetNames: [],
  alignmentMethod: "nearest",
  alignmentApproved: true,
  hypothesisRationale: "Reviewed resistance rationale.",
  focalRationale: "Reviewed habitat-core selection and intended pair comparison.",
  interventionBasis: "",
  safeguardNotes: "",
  budgetUsd: "",
};

const projectSession: ProfessionalProjectSession = {
  projectHandle: "project_focal_input",
  projectId: "focal-input-project",
  title: "Reviewed ridge screen",
  mode: "PLANNING_DRAFT",
  bindingState: "CONNECTED",
  bindingMessage: "Restricted destination verified.",
};

const focalAsset: ProfessionalAssetRecord = {
  assetId: "asset_focal_reviewed",
  title: "Reviewed focal identifiers",
  kind: "FOCAL_RASTER",
  displayName: "reviewed-focal.asc",
  relativePath: "data_assets/reviewed-focal.asc",
  sha256: "a".repeat(64),
  byteSize: 2048,
  mediaType: "text/plain",
  source: "Agency habitat-core review",
  vintage: "2026",
  license: "Agency internal use terms",
  sensitivity: "INTERNAL",
  cloudDisposition: "DEDICATED_OR_SELF_MANAGED_ONLY",
  exportAllowed: false,
  approvedDeployments: ["DEDICATED"],
  reviewer: "Agency ecologist",
  reviewedAt: "2026-08-03T12:00:00Z",
  intendedUse: "Reviewed focal identifiers for this planning comparison.",
  revision: 1,
  grid: {
    grid_id: "grid_focal_reviewed",
    crs: "EPSG:5070",
    width: 10,
    height: 8,
    transform: [30, 0, 500000, 0, -30, 4200000],
    nodata: -9999,
    value_kind: "IDENTIFIER",
    cell_area_m2: 900,
  },
  authorization: {
    asset_id: "asset_focal_reviewed",
    ownership: "State wildlife agency",
    license_spdx_or_name: "Agency internal use terms",
    sensitivity: "INTERNAL",
    cloud_disposition: "DEDICATED_OR_SELF_MANAGED_ONLY",
    export_allowed: false,
    retention_policy: "Retain through project close.",
    spatial_redaction: "Generalized project extent only.",
    approved_deployments: ["DEDICATED"],
    reviewer: "Agency ecologist",
    reviewed_at: "2026-08-03T12:00:00Z",
    generalized_asset_id: null,
    notes: "Reviewed focal asset authorization.",
  },
};

const records: ProfessionalModelRecords = {
  hypotheses: [],
  focalAreaSets: [],
  scenarios: [],
  interventionSites: [],
  interventionOptions: [],
  safeguards: [],
  reviewMasks: [],
  decisionRules: [],
  decisionBrief: null,
  costBasis: null,
  resourcePolicy: null,
};

afterEach(() => {
  delete window.__ECOCORRIDOR_HOST__;
});

describe("FocalAreasScreen", () => {
  it("blocks a malformed token and persists the complete corrected ID list", async () => {
    const user = userEvent.setup();
    const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method !== "connectivity.focal_areas.save") throw new Error(`Unexpected method ${method}`);
      const focalAreaSet = params.focal_area_set as { focal_set_id: string };
      return {
        api_version: "1.0",
        method,
        data: { record_id: focalAreaSet.focal_set_id, revision: 2 },
        data_sha256: "b".repeat(64),
      };
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

    render(
      <WorkspaceDraftProvider>
        <FocalAreasScreen
          planning={planning}
          onPlanning={vi.fn()}
          projectSession={projectSession}
          professionalAssets={[focalAsset]}
          professionalRecords={records}
          onProfessionalRecords={vi.fn()}
        />
      </WorkspaceDraftProvider>,
    );

    await user.type(screen.getByLabelText("Title *"), "North, central, and south habitat cores");
    await user.type(screen.getByLabelText("Guild ID *"), "guild_target");
    await user.type(screen.getByLabelText(/Positive focal IDs/i), "1, 2x, 3");
    await user.type(screen.getByLabelText(/Required pairs/i), "1-2, 2-3");
    fireEvent.change(screen.getByLabelText("Reviewed at *"), { target: { value: "2026-08-03T13:30" } });

    const saveButton = screen.getByRole("button", { name: "Save reviewed focal set" });
    expect(screen.getByLabelText(/Positive focal IDs/i)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent('Focal ID "2x" must be a positive whole number.');
    expect(saveButton).toBeDisabled();
    expect(request).not.toHaveBeenCalled();

    await user.clear(screen.getByLabelText(/Positive focal IDs/i));
    await user.type(screen.getByLabelText(/Positive focal IDs/i), "1, 2, 3");
    expect(screen.queryByText(/2x/)).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText(/Required pairs/i));
    await user.type(screen.getByLabelText(/Required pairs/i), "3-1");
    expect(screen.getByLabelText(/Required pairs/i)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent(/must list the lower focal ID first/);
    expect(saveButton).toBeDisabled();
    expect(request).not.toHaveBeenCalled();

    await user.clear(screen.getByLabelText(/Required pairs/i));
    await user.type(screen.getByLabelText(/Required pairs/i), "1-2, 2-3");
    expect(saveButton).toBeEnabled();
    await user.click(saveButton);

    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
    const saveParams = request.mock.calls[0][1] as {
      project_handle: string;
      focal_area_set: {
        focal_set_id: string;
        focal_ids: number[];
        pairs: Array<{ pair_id: string; from_focal_id: number; to_focal_id: number; required: boolean }>;
      };
    };
    expect(saveParams.project_handle).toBe("project_focal_input");
    expect(saveParams.focal_area_set.focal_ids).toEqual([1, 2, 3]);
    const focalSetId = saveParams.focal_area_set.focal_set_id;
    expect(saveParams.focal_area_set.pairs).toEqual([
      { pair_id: `${focalSetId}_pair_1_2`, from_focal_id: 1, to_focal_id: 2, required: true },
      { pair_id: `${focalSetId}_pair_2_3`, from_focal_id: 2, to_focal_id: 3, required: true },
    ]);
  });
});
