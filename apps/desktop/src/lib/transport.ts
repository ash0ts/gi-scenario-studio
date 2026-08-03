import { invoke } from "@tauri-apps/api/core";
import { demoProject } from "../data/demo";
import type { ProjectKind, ProjectSummary, RpcTransport } from "../types";

const delay = (milliseconds: number) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

class BrowserDemoTransport implements RpcTransport {
  readonly mode = "browser-demo" as const;

  async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    await delay(method.includes("start") ? 500 : 180);

    if (method === "project.create") {
      const kind = (params.kind ?? "demo") as ProjectKind;
      if (kind === "demo") return { ...demoProject } as T;
      return {
        id: `local-${kind}`,
        name: kind === "conceptual" ? "New conceptual study" : "New SWMM import",
        kind,
        readiness: "CONCEPTUAL",
        location: "Draft · no source loaded",
        modelFile: kind === "conceptual" ? "concept-draft.inp" : "No bundle selected",
        updatedAt: new Date().toISOString(),
      } satisfies ProjectSummary as T;
    }

    return { ok: true, method, params } as T;
  }
}

class DesktopRpcTransport implements RpcTransport {
  readonly mode = "tauri" as const;

  async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    if (!window.__TAURI_INTERNALS__) {
      throw new Error("The desktop scientific worker is not available.");
    }
    const raw = await invoke<unknown>("worker_request", {
      request: { method, params },
    });
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

export const transport: RpcTransport = window.__TAURI_INTERNALS__
  ? new DesktopRpcTransport()
  : new BrowserDemoTransport();
