import { invoke } from "@tauri-apps/api/core";
import { demoProject } from "../data/guidedSummary";
import type { ObservabilityState, ProjectKind, ProjectSummary, RpcTransport } from "../types";

const delay = (milliseconds: number) => new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));

export function withRequestTimeout<T>(request: Promise<T>, timeoutMs?: number): Promise<T> {
  if (timeoutMs === undefined) return request;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new Error("RPC timeout must be a positive number of milliseconds.");
  return new Promise<T>((resolve, reject) => {
    const timer = globalThis.setTimeout(() => reject(new Error(`The worker request exceeded ${Math.round(timeoutMs / 1000)} seconds.`)), timeoutMs);
    request.then(
      (value) => { globalThis.clearTimeout(timer); resolve(value); },
      (reason) => { globalThis.clearTimeout(timer); reject(reason); },
    );
  });
}

export function hostWorkerRequest(method: string, params: Record<string, unknown>, timeoutMs?: number): {
  method: string;
  params: Record<string, unknown>;
  timeoutMs?: number;
} {
  return timeoutMs === undefined ? { method, params } : { method, params, timeoutMs };
}

export class BrowserDemoTransport implements RpcTransport {
  readonly mode = "browser-demo" as const;

  private observability: ObservabilityState = {
    mode: "local",
    configured: false,
    provider: "none",
    disclosureAccepted: false,
    message: "Interface-only preview; no engineering project is bound.",
  };

  async request<T>(method: string, params: Record<string, unknown> = {}, options: { timeoutMs?: number } = {}): Promise<T> {
    await withRequestTimeout(delay(method.includes("start") ? 500 : 180), options.timeoutMs);

    if (method === "project.create") {
      const kind = (params.kind ?? "demo") as ProjectKind;
      if (kind === "demo") return { ...demoProject } as T;
      return {
        id: `local-${kind}`,
        name: kind === "conceptual" ? "New conceptual study" : "New SWMM import",
        kind,
        fitnessDisposition: "not_assessed",
        location: "Draft · no source loaded",
        modelFile: kind === "conceptual" ? "concept-draft.inp" : "No bundle selected",
        updatedAt: new Date().toISOString(),
      } satisfies ProjectSummary as T;
    }

    if (method === "guided.open") {
      const { default: fixture } = await import("../data/guidedFixture.json");
      const projectId = String(params.project_id ?? demoProject.id);
      return {
        project_id: projectId,
        current_step_id: "overview",
        checkpoints: [],
        outcome: "A transparent GI shortlist whose hydraulic values resolve to exact pinned SWMM runs.",
        local_authority: true,
        fixture,
        preview_mode: "browser-demo",
      } as T;
    }

    if (method === "observability.get_status") {
      return { ...this.observability } as T;
    }

    if (method === "observability.preview_interface") {
      this.observability = {
        mode: "demo",
        configured: true,
        provider: "wandb-weave",
        disclosureAccepted: true,
        lastChecked: new Date().toISOString(),
        message: "Interface-only Cedar Trace preview. No network request or credential was used.",
      };
      return { ...this.observability } as T;
    }

    if (method === "observability.test_connection") {
      const entity = String(params.entity ?? "").trim();
      const project = String(params.project ?? "").trim();
      const weaveProject = String(params.weaveProject ?? project).trim();
      const validSlug = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/;
      if (!validSlug.test(entity) || !validSlug.test(project) || !validSlug.test(weaveProject)) {
        throw new Error("Use W&B entity and project slugs containing only letters, numbers, dots, underscores, or hyphens.");
      }
      this.observability = {
        mode: "demo",
        configured: true,
        provider: "wandb-weave",
        entity,
        project,
        weaveProject,
        wandbUrl: `https://wandb.ai/${encodeURIComponent(entity)}/${encodeURIComponent(project)}`,
        weaveUrl: `https://wandb.ai/${encodeURIComponent(entity)}/${encodeURIComponent(weaveProject)}/weave`,
        lastChecked: new Date().toISOString(),
        disclosureAccepted: params.disclosureAccepted === true,
        message: "Browser preview only: links were validated locally, but no authenticated cloud connection was attempted.",
      };
      return { ...this.observability } as T;
    }

    return { ok: true, method, params } as T;
  }
}

class DesktopRpcTransport implements RpcTransport {
  readonly mode = "tauri" as const;

  async request<T>(method: string, params: Record<string, unknown> = {}, options: { timeoutMs?: number } = {}): Promise<T> {
    if (!window.__TAURI_INTERNALS__) {
      throw new Error("The desktop scientific worker is not available.");
    }
    const raw = await withRequestTimeout(invoke<unknown>("worker_request", {
      request: hostWorkerRequest(method, params, options.timeoutMs),
    }), options.timeoutMs);
    const response = (typeof raw === "string" ? JSON.parse(raw) : raw) as {
      result?: T;
      error?: { code?: number; message?: string; user_message?: string };
    };
    if (response.error) {
      throw new Error(response.error.user_message ?? response.error.message ?? `Worker request failed (${response.error.code ?? "unknown"}).`);
    }
    return (Object.prototype.hasOwnProperty.call(response, "result") ? response.result : response) as T;
  }
}

export const transport: RpcTransport = typeof window !== "undefined" && window.__TAURI_INTERNALS__
  ? new DesktopRpcTransport()
  : new BrowserDemoTransport();
