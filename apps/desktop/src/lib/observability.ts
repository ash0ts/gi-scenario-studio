import { CloudSyncStateSchema, ObservabilityAccountStateSchema } from "../generated/contracts";
import type { CloudConnectionState, ObservabilityState } from "../types";

const unreachableCloudState = (state: never): never => {
  throw new Error(`Unsupported cloud connection state: ${String(state)}`);
};

export interface ProjectCloudSyncState {
  project_id: string;
  state: CloudConnectionState;
  binding_required: boolean;
  binding: {
    entity: string;
    external_project: string;
    verified: boolean;
    web_base_url: string;
  } | null;
  can_enter_home: boolean;
  can_open_project: boolean;
  can_run_local: boolean;
  can_sync: boolean;
  can_use_assistant: boolean;
  online: boolean;
  outbox_counts: Record<string, number>;
  pending_artifact_bytes: number;
  last_synced_at: string | null;
  last_error_code: string | null;
  local_authority: true;
}

export interface ActiveObservabilityProject {
  id: string;
  projectDirectory: string;
}

export type ObservabilityRpcRequest = (
  method: string,
  params: Record<string, unknown>,
  options?: { timeoutMs?: number },
) => Promise<unknown>;

export interface LocalWorkspaceTransition {
  account: ObservabilityState;
  project: ProjectCloudSyncState | null;
}

export const localObservabilityState = (configured = true): ObservabilityState => ({
  schemaVersion: "2.0",
  scope: "account",
  state: "LOCAL_ONLY",
  mode: "local",
  configured,
  provider: "none",
  disclosureAccepted: false,
  message: "Engineering work stays on this device. W&B sync and the Weave assistant are off.",
});

export function observabilityLabel(state: ObservabilityState): string {
  if (state.mode === "connected") return "W&B connected";
  if (state.mode === "demo") return "Interface preview";
  if (state.mode === "error") return "Observability needs attention";
  return "Local workspace";
}

export function observabilityDetail(state: ObservabilityState): string {
  if (state.mode === "connected") return `${state.entity}/${state.project}`;
  if (state.mode === "demo") return state.entity && state.project
    ? `${state.entity}/${state.project} · link preview`
    : "No cloud project is bound";
  if (state.mode === "error") return state.message ?? "Connection check failed";
  return "Cloud sync and assistant are off";
}

export function hasObservabilityDeepLinks(state: ObservabilityState): boolean {
  return Boolean(state.wandbUrl || state.weaveUrl);
}

export function wandbProjectSettingsUrl(
  webBaseUrl: string,
  entity: string,
  project: string,
): string | null {
  try {
    const base = new URL(webBaseUrl);
    if (base.protocol !== "https:" || base.username || base.password) return null;
    const path = entity.trim() && project.trim()
      ? `/${encodeURIComponent(entity.trim())}/${encodeURIComponent(project.trim())}/settings`
      : "/home";
    return new URL(path, base.origin).toString();
  } catch {
    return null;
  }
}

export function parseProjectCloudSyncState(value: unknown): ProjectCloudSyncState {
  return CloudSyncStateSchema.parse(value) as ProjectCloudSyncState;
}

export function parseObservabilityAccountState(value: unknown): ObservabilityState {
  return ObservabilityAccountStateSchema.parse(value) as ObservabilityState;
}

export async function chooseLocalWorkspace(
  request: ObservabilityRpcRequest,
  project: ActiveObservabilityProject | null,
): Promise<LocalWorkspaceTransition> {
  let projectState: ProjectCloudSyncState | null = null;
  if (project) {
    projectState = parseProjectCloudSyncState(await request("observability.disconnect", {
      scope: "project",
      project_directory: project.projectDirectory,
      project_id: project.id,
    }));
    if (
      projectState.project_id !== project.id
      || projectState.state !== "LOCAL_ONLY"
      || projectState.binding !== null
      || projectState.can_sync
      || projectState.can_use_assistant
    ) {
      throw new Error("The worker did not confirm local-only mode for this project.");
    }
    const account = parseObservabilityAccountState(await request(
      "observability.get_status",
      { scope: "account" },
    ));
    if (account.state !== "LOCAL_ONLY" || account.mode !== "local") {
      throw new Error("The worker did not end the active cloud account session.");
    }
    return { account, project: projectState };
  }
  const account = parseObservabilityAccountState(await request("observability.disconnect", {
    scope: "account",
  }));
  if (account.state !== "LOCAL_ONLY" || account.mode !== "local") {
    throw new Error("The worker did not confirm local-only mode.");
  }
  return { account, project: null };
}

export async function bindOpenProject(
  request: ObservabilityRpcRequest,
  project: ActiveObservabilityProject,
): Promise<ProjectCloudSyncState> {
  const state = parseProjectCloudSyncState(await request("observability.bind_project", {
    project_directory: project.projectDirectory,
    project_id: project.id,
  }, { timeoutMs: 120_000 }));
  if (
    state.project_id !== project.id
    || state.binding === null
    || !state.binding.verified
    || !state.can_sync
    || !state.can_use_assistant
  ) {
    throw new Error("The worker did not bind this project to the verified Restricted destination.");
  }
  return state;
}

export function projectCloudSyncLabel(state: ProjectCloudSyncState): string {
  switch (state.state) {
    case "LOCAL_ONLY": return "Local evidence only";
    case "CONNECTED": return projectCloudPendingCount(state) > 0 ? "Evidence upload pending" : "Evidence mirror current";
    case "SYNCING": return "Syncing project evidence";
    case "SYNC_QUEUED_OFFLINE": return "Working offline";
    case "FAILED_RETRYING": return "Evidence sync failed";
    case "AUTHENTICATION_REQUIRED": return "W&B sign-in needed";
    case "BLOCKED_PROJECT_POLICY": return "Project policy blocks sync";
    default: return unreachableCloudState(state.state);
  }
}

export function projectCloudSyncDestination(state: ProjectCloudSyncState): string {
  if (!state.binding) return "No cloud project bound";
  return `${state.binding.entity}/${state.binding.external_project}`;
}

export function projectCloudWandbUrl(state: ProjectCloudSyncState): string | null {
  if (!state.binding) return null;
  return `${state.binding.web_base_url.replace(/\/+$/, "")}/${encodeURIComponent(state.binding.entity)}/${encodeURIComponent(state.binding.external_project)}`;
}

export function projectCloudWeaveUrl(state: ProjectCloudSyncState): string | null {
  const projectUrl = projectCloudWandbUrl(state);
  return projectUrl ? `${projectUrl}/weave` : null;
}

export function projectCloudPendingCount(state: ProjectCloudSyncState): number {
  return Object.entries(state.outbox_counts).reduce(
    (total, [status, count]) => status === "synced" || status === "cancelled" ? total : total + count,
    0,
  );
}

export function formatCloudArtifactBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIndex]}`;
}

export function projectCloudSyncDetail(state: ProjectCloudSyncState): string {
  const pending = projectCloudPendingCount(state);
  const destination = projectCloudSyncDestination(state);
  if (state.state === "LOCAL_ONLY") return "Exact runs and evidence stay on this device";
  if (state.state === "AUTHENTICATION_REQUIRED") return `${destination} · reconnect to resume uploads`;
  if (state.state === "BLOCKED_PROJECT_POLICY") return `${destination} · review the project data policy`;
  if (state.state === "FAILED_RETRYING") {
    const failure = state.last_error_code ? ` · ${state.last_error_code}` : "";
    const failed = pending || state.outbox_counts.failed || 0;
    return `${failed} item${failed === 1 ? "" : "s"} awaiting retry${failure}`;
  }
  if (pending > 0 || state.pending_artifact_bytes > 0) {
    return `${pending} item${pending === 1 ? "" : "s"} · ${formatCloudArtifactBytes(state.pending_artifact_bytes)} waiting`;
  }
  return destination;
}

export function projectCloudSyncTone(state: ProjectCloudSyncState): "local" | "connected" | "syncing" | "queued" | "failure" | "authentication" | "blocked" {
  switch (state.state) {
    case "LOCAL_ONLY": return "local";
    case "CONNECTED": return "connected";
    case "SYNCING": return "syncing";
    case "SYNC_QUEUED_OFFLINE": return "queued";
    case "FAILED_RETRYING": return "failure";
    case "AUTHENTICATION_REQUIRED": return "authentication";
    case "BLOCKED_PROJECT_POLICY": return "blocked";
    default: return unreachableCloudState(state.state);
  }
}

export function projectCloudActionLabel(state: ProjectCloudSyncState): string {
  switch (state.state) {
    case "LOCAL_ONLY": return "Connect W&B";
    case "SYNCING": return "Refresh sync progress";
    case "SYNC_QUEUED_OFFLINE": return "Check connection";
    case "FAILED_RETRYING": return "Retry failed uploads";
    case "AUTHENTICATION_REQUIRED": return "Reconnect W&B";
    case "BLOCKED_PROJECT_POLICY": return "Review connection policy";
    case "CONNECTED": return "Refresh sync status";
    default: return unreachableCloudState(state.state);
  }
}
