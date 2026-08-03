import { describe, expect, it } from "vitest";
import {
  bindOpenProject,
  chooseLocalWorkspace,
  formatCloudArtifactBytes,
  hasObservabilityDeepLinks,
  localObservabilityState,
  observabilityDetail,
  observabilityLabel,
  parseObservabilityAccountState,
  parseProjectCloudSyncState,
  projectCloudActionLabel,
  projectCloudPendingCount,
  projectCloudSyncDetail,
  projectCloudSyncLabel,
  projectCloudWandbUrl,
  wandbProjectSettingsUrl,
} from "./observability";

describe("observability presentation", () => {
  it("makes local engineering work the configured default", () => {
    const state = localObservabilityState(true);
    expect(state.state).toBe("LOCAL_ONLY");
    expect(observabilityLabel(state)).toBe("Local workspace");
    expect(observabilityDetail(state)).toBe("Cloud sync and assistant are off");
    expect(hasObservabilityDeepLinks(state)).toBe(false);
  });

  it("validates an unbound project as runnable local evidence", () => {
    const state = parseProjectCloudSyncState({
      project_id: "project_local",
      state: "LOCAL_ONLY",
      binding_required: false,
      binding: null,
      can_enter_home: true,
      can_open_project: true,
      can_run_local: true,
      can_sync: false,
      can_use_assistant: false,
      online: false,
      outbox_counts: {},
      pending_artifact_bytes: 0,
      local_authority: true,
    });

    expect(projectCloudSyncLabel(state)).toBe("Local evidence only");
    expect(projectCloudSyncDetail(state)).toContain("stay on this device");
    expect(projectCloudActionLabel(state)).toBe("Connect W&B");
    expect(projectCloudWandbUrl(state)).toBeNull();
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
      binding_required: false,
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
    expect(parseObservabilityAccountState(localObservabilityState()).state).toBe("LOCAL_ONLY");
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

  it("uses the worker to turn an open connected project into honest local-only mode", async () => {
    const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
    const request = async (method: string, params: Record<string, unknown>) => {
      calls.push({ method, params });
      if (method === "observability.disconnect") {
        return {
          project_id: "project_1",
          state: "LOCAL_ONLY",
          binding_required: false,
          binding: null,
          can_enter_home: true,
          can_open_project: true,
          can_run_local: true,
          can_sync: false,
          can_use_assistant: false,
          online: false,
          outbox_counts: { blocked: 2, synced: 1 },
          pending_artifact_bytes: 512,
          last_error_code: "LOCAL_MODE_ENABLED",
          local_authority: true,
        };
      }
      return localObservabilityState();
    };

    const result = await chooseLocalWorkspace(request, {
      id: "project_1",
      projectDirectory: "/project",
    });

    expect(result.project?.state).toBe("LOCAL_ONLY");
    expect(result.account.state).toBe("LOCAL_ONLY");
    expect(calls).toEqual([
      {
        method: "observability.disconnect",
        params: {
          scope: "project",
          project_directory: "/project",
          project_id: "project_1",
        },
      },
      { method: "observability.get_status", params: { scope: "account" } },
    ]);
  });

  it("binds the already-open project only when the worker confirms verified sync", async () => {
    const request = async (method: string, params: Record<string, unknown>) => {
      expect(method).toBe("observability.bind_project");
      expect(params).toEqual({ project_directory: "/project", project_id: "project_1" });
      return {
        project_id: "project_1",
        state: "CONNECTED",
        binding_required: false,
        binding: {
          binding_id: "binding_1",
          project_id: "project_1",
          data_policy_id: "policy_1",
          entity: "stormwater-team",
          external_project: "gi-project",
          external_project_id: "provider-project-1",
          api_base_url: "https://api.wandb.ai",
          web_base_url: "https://wandb.ai",
          access: "Restricted",
          deployment: "wandb_cloud",
          mode: "online",
          verified: true,
          immutable_after_first_upload: true,
          verified_at: "2026-08-03T12:00:00Z",
          updated_at: "2026-08-03T12:00:00Z",
        },
        can_enter_home: true,
        can_open_project: true,
        can_run_local: true,
        can_sync: true,
        can_use_assistant: true,
        online: true,
        outbox_counts: {},
        pending_artifact_bytes: 0,
        local_authority: true,
      };
    };

    const result = await bindOpenProject(request, {
      id: "project_1",
      projectDirectory: "/project",
    });

    expect(result.binding?.verified).toBe(true);
    expect(result.can_sync).toBe(true);
    expect(result.can_use_assistant).toBe(true);
  });
});
