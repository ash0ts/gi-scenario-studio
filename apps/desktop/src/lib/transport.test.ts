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
