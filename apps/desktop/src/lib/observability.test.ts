import { describe, expect, it } from "vitest";
import {
  formatCloudArtifactBytes,
  hasObservabilityDeepLinks,
  localObservabilityState,
  observabilityDetail,
  observabilityLabel,
  parseObservabilityAccountState,
  parseProjectCloudSyncState,
  projectCloudPendingCount,
  projectCloudSyncDetail,
  projectCloudSyncLabel,
  projectCloudWandbUrl,
  wandbProjectSettingsUrl,
} from "./observability";

describe("observability presentation", () => {
  it("keeps the required binding state explicit", () => {
    const state = localObservabilityState(true);
    expect(observabilityLabel(state)).toBe("W&B required");
    expect(observabilityDetail(state)).toBe("Authentication required");
    expect(hasObservabilityDeepLinks(state)).toBe(false);
  });

  it("labels browser-safe link previews without claiming a cloud connection", () => {
    const state = {
      ...localObservabilityState(true),
      mode: "demo" as const,
      provider: "wandb-weave" as const,
      entity: "example",
      project: "fixture",
      wandbUrl: "https://wandb.ai/example/fixture",
    };
    expect(observabilityLabel(state)).toBe("Interface preview");
    expect(observabilityDetail(state)).toContain("link preview");
    expect(hasObservabilityDeepLinks(state)).toBe(true);
  });

  it("validates and explains the project-level cloud ledger", () => {
    const state = parseProjectCloudSyncState({
      project_id: "project_1",
      state: "FAILED_RETRYING",
      binding_required: true,
      binding: {
        binding_id: "binding_1",
        project_id: "project_1",
        data_policy_id: "policy_1",
        entity: "stormwater-team",
        external_project: "gi-cedar-trace",
        external_project_id: "wb-project-1",
        deployment: "wandb_cloud",
        verified: true,
        updated_at: "2026-08-03T12:00:00Z",
      },
      can_enter_home: true,
      can_open_project: true,
      can_run_local: true,
      can_sync: true,
      can_use_assistant: true,
      online: true,
      outbox_counts: { failed: 2, synced: 4 },
      pending_artifact_bytes: 1_572_864,
      last_error_code: "WANDB_QUOTA",
      local_authority: true,
    });

    expect(projectCloudPendingCount(state)).toBe(2);
    expect(projectCloudSyncLabel(state)).toBe("Evidence sync failed");
    expect(projectCloudSyncDetail(state)).toContain("WANDB_QUOTA");
    expect(formatCloudArtifactBytes(state.pending_artifact_bytes)).toBe("1.5 MB");
    expect(projectCloudWandbUrl(state)).toBe("https://wandb.ai/stormwater-team/gi-cedar-trace");
  });

  it("rejects project status values outside the generated v2 contract", () => {
    expect(() => parseProjectCloudSyncState({ project_id: "project_1", state: "MAYBE_CONNECTED" })).toThrow();
  });

  it("validates the account onboarding response through the generated contract", () => {
    const state = parseObservabilityAccountState({
      schemaVersion: "2.0",
      scope: "account",
      state: "CONNECTED",
      mode: "connected",
      configured: true,
      provider: "wandb-weave",
      disclosureAccepted: true,
      sessionId: "session_1",
      entity: "stormwater-team",
      project: "gi-project",
      weaveProject: "gi-project",
      wandbUrl: "https://wandb.ai/stormwater-team/gi-project",
      weaveUrl: "https://wandb.ai/stormwater-team/gi-project/weave",
      deployment: "wandb_cloud",
      apiBaseUrl: "https://api.wandb.ai",
      webBaseUrl: "https://wandb.ai",
      externalProjectId: "provider-project-1",
      includedCategories: ["model"],
      excludedCategories: ["credentials"],
      probes: {},
      lastChecked: "2026-08-03T12:00:00Z",
      message: "Restricted destination verified.",
    });

    expect(state.state).toBe("CONNECTED");
    expect(() => parseObservabilityAccountState({ ...state, mode: "maybe" })).toThrow();
  });

  it("creates only credential-free HTTPS project-settings links", () => {
    expect(wandbProjectSettingsUrl("https://wandb.ai", "storm team", "cedar/trace")).toBe(
      "https://wandb.ai/storm%20team/cedar%2Ftrace/settings",
    );
    expect(wandbProjectSettingsUrl("javascript:alert(1)", "team", "project")).toBeNull();
    expect(wandbProjectSettingsUrl("https://user:secret@wandb.example", "team", "project")).toBeNull();
  });
});
