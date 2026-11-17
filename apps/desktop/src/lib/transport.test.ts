import { describe, expect, it } from "vitest";
import type { ProjectSummary } from "../types";
import type { GuidedFixture } from "../data/demo";
import { BrowserDemoTransport, hostWorkerRequest, withRequestTimeout } from "./transport";

describe("guided transport boundary", () => {
  it("loads the bundled fixture only through the labeled browser preview adapter", async () => {
    const transport = new BrowserDemoTransport();
    const project = await transport.request<ProjectSummary>("project.create", { kind: "demo" });
    const opened = await transport.request<{ fixture: GuidedFixture; preview_mode: string }>("guided.open", {
      project_directory: project.location,
      project_id: project.id,
    });

    expect(opened.preview_mode).toBe("browser-demo");
    expect(opened.fixture.fixture_id).toBe("cedar_trace_exact_v2");
    expect(opened.fixture.job_accounting.completed_jobs).toBe(884);
  });

  it("projects the checked Evidence Guide release gate without making a model call", async () => {
    const transport = new BrowserDemoTransport();
    const capabilities = await transport.request<{
      available: boolean;
      state: string;
      reason: string;
      model: string | null;
      provider: string;
      local_evidence_available: boolean;
    }>("assistant.get_capabilities", { project_id: "cedar-trace" });

    expect(capabilities.available).toBe(false);
    expect(capabilities.state).toBe("MODEL_NOT_RELEASE_GATED");
    expect(capabilities.reason).toContain("DISABLE_LIVE_GUIDE");
    expect(capabilities.reason).toContain("no candidate passed every deterministic and practitioner gate");
    expect(capabilities.model).toBeNull();
    expect(capabilities.provider).toBe("W&B Inference");
    expect(capabilities.local_evidence_available).toBe(true);
  });

  it("uses an explicit timeout without changing successful responses", async () => {
    await expect(withRequestTimeout(Promise.resolve("connected"), 120_000)).resolves.toBe("connected");
    expect(() => withRequestTimeout(Promise.resolve("nope"), 0)).toThrow(/positive/);
    expect(hostWorkerRequest("observability.connect", { scope: "account" }, 120_000)).toEqual({
      method: "observability.connect",
      params: { scope: "account" },
      timeoutMs: 120_000,
    });
  });
});
