import { invoke } from "@tauri-apps/api/core";

import type { ExportCopyReceipt, HostBridge, OpaqueFileHandle, OpaqueProjectHandle, RecentProjectHandle, RpcPayload } from "../types";

type CredentialKind = "wandb" | "model_provider";

function customBridge(): HostBridge | undefined {
  return window.__ECOCORRIDOR_HOST__;
}

export function hasNativeHost(): boolean {
  return Boolean(window.__TAURI_INTERNALS__ || customBridge());
}

/** Credentials are deliberately available only through the real Tauri host.
 * A browser/custom test bridge may exercise RPC, but it must never emulate a
 * renderer-visible API-key path.
 */
export function hasNativeCredentialHost(): boolean {
  return Boolean(window.__TAURI_INTERNALS__);
}

export function hasNativeExternalLinkHost(): boolean {
  return Boolean(window.__TAURI_INTERNALS__);
}

export async function hostRequest<T>(
  method: string,
  params: Record<string, unknown>,
  options: { timeoutMs?: number } = {},
): Promise<T> {
  const bridge = customBridge();
  if (bridge) return bridge.request<T>(method, params);
  if (!window.__TAURI_INTERNALS__) {
    throw new Error("The verified EcoCorridor desktop host is not available.");
  }
  return invoke<T>("worker_request", {
    request: { method, params, ...(options.timeoutMs ? { timeoutMs: options.timeoutMs } : {}) },
  });
}

export function rpcData<T>(result: RpcPayload<T> | T): T {
  if (
    result
    && typeof result === "object"
    && "data" in result
    && "data_sha256" in result
    && typeof (result as RpcPayload<T>).data_sha256 === "string"
  ) {
    return (result as RpcPayload<T>).data;
  }
  return result as T;
}

export function hostErrorMessage(reason: unknown, fallback: string): string {
  if (reason instanceof Error && reason.message.trim()) return reason.message;
  if (reason && typeof reason === "object") {
    const candidate = reason as { user_message?: unknown; message?: unknown };
    if (typeof candidate.user_message === "string" && candidate.user_message.trim()) return candidate.user_message;
    if (typeof candidate.message === "string" && candidate.message.trim()) return candidate.message;
  }
  return fallback;
}

export async function chooseConnectivityBundle(): Promise<OpaqueFileHandle | null> {
  const bridge = customBridge();
  if (bridge?.chooseConnectivityBundle) return bridge.chooseConnectivityBundle();
  if (!window.__TAURI_INTERNALS__) {
    throw new Error("Bundle selection is available only through the verified EcoCorridor desktop host.");
  }
  return invoke<OpaqueFileHandle | null>("choose_connectivity_bundle");
}

export async function chooseConnectivityAsset(): Promise<OpaqueFileHandle | null> {
  const bridge = customBridge();
  if (bridge?.chooseConnectivityAsset) return bridge.chooseConnectivityAsset();
  if (!window.__TAURI_INTERNALS__) {
    throw new Error("Asset selection is available only through the verified EcoCorridor desktop host.");
  }
  return invoke<OpaqueFileHandle | null>("choose_connectivity_asset");
}

export async function createProjectWorkspace(): Promise<OpaqueProjectHandle> {
  const bridge = customBridge();
  if (bridge?.createProjectWorkspace) return bridge.createProjectWorkspace();
  if (!window.__TAURI_INTERNALS__) {
    throw new Error("Project creation is available only through the verified EcoCorridor desktop host.");
  }
  return invoke<OpaqueProjectHandle>("create_project_workspace");
}

export async function listProjectWorkspaces(): Promise<RecentProjectHandle[]> {
  const bridge = customBridge();
  if (bridge) return bridge.listProjectWorkspaces ? bridge.listProjectWorkspaces() : [];
  if (!window.__TAURI_INTERNALS__) return [];
  return invoke<RecentProjectHandle[]>("list_project_workspaces");
}

export async function openProjectWorkspace(projectHandle: string): Promise<OpaqueProjectHandle> {
  const bridge = customBridge();
  if (bridge) {
    if (!bridge.openProjectWorkspace) throw new Error("The desktop test host does not support reopening saved projects.");
    return bridge.openProjectWorkspace(projectHandle);
  }
  if (!window.__TAURI_INTERNALS__) {
    throw new Error("Saved projects can be reopened only through the verified EcoCorridor desktop host.");
  }
  return invoke<OpaqueProjectHandle>("open_project_workspace", { projectHandle });
}

export async function saveExportCopy(projectHandle: string, exportId: string): Promise<ExportCopyReceipt | null> {
  const bridge = customBridge();
  if (bridge?.saveExportCopy) return bridge.saveExportCopy(projectHandle, exportId);
  if (!window.__TAURI_INTERNALS__) {
    throw new Error("Saving an authoritative evidence-package copy is available only through the signed desktop host.");
  }
  return invoke<ExportCopyReceipt | null>("save_export_copy", { projectHandle, exportId });
}

export async function openObservabilityReference(
  projectHandle: string,
  kind: "run" | "artifact" | "trace",
  referenceId: string,
): Promise<void> {
  if (!window.__TAURI_INTERNALS__) {
    throw new Error("Confirmed observability links open only through the signed desktop host.");
  }
  const method = `observability.open_${kind}`;
  const response = await hostRequest<RpcPayload<unknown>>(method, {
    project_handle: projectHandle,
    reference_id: referenceId,
  });
  const data = rpcData(response);
  if (!data || typeof data !== "object" || !("url" in data) || typeof data.url !== "string") {
    throw new Error("The worker did not return a confirmed provider URL.");
  }
  await invoke<void>("open_confirmed_provider_url", { url: data.url });
}

export async function credentialStatus(credentialId: string, kind: CredentialKind): Promise<boolean> {
  if (!window.__TAURI_INTERNALS__) return false;
  return invoke<boolean>("credential_status", { credentialId, kind });
}

export async function storeCredential(credentialId: string, kind: CredentialKind): Promise<void> {
  if (!window.__TAURI_INTERNALS__) {
    throw new Error("API-key entry is available only in the signed desktop application.");
  }
  await invoke<void>("credential_store", { credentialId, kind });
}

export function wandbCredentialId(baseUrl: string, entity: string, project: string): string {
  let host = "deployment";
  try {
    host = new URL(baseUrl).host;
  } catch {
    // Validation remains with the worker; this value is only an opaque keychain handle.
  }
  return `eco-wandb-${host}-${entity}-${project}`
    .replace(/[^A-Za-z0-9._-]/g, "_")
    .slice(0, 128);
}

export function modelProviderCredentialId(projectHandle: string, baseUrl: string, model: string): string {
  let host = "provider";
  try {
    host = new URL(baseUrl).host;
  } catch {
    // The worker remains authoritative for URL validation. This is only an
    // opaque operating-system keychain account identifier.
  }
  return `eco-model-${projectHandle}-${host}-${model || "unselected"}`
    .replace(/[^A-Za-z0-9._-]/g, "_")
    .slice(0, 128);
}
