import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AssistantProviderSetup } from "./AssistantProviderSetup";

const host = vi.hoisted(() => ({ request: vi.fn() }));

vi.mock("../lib/host", () => ({
  credentialStatus: vi.fn(async () => true),
  hasNativeCredentialHost: vi.fn(() => true),
  hostErrorMessage: (reason: unknown, fallback: string) => reason instanceof Error ? reason.message : fallback,
  hostRequest: host.request,
  modelProviderCredentialId: (projectHandle: string, _baseUrl: string, model: string) => `model-${projectHandle}-${model}`,
  rpcData: <T,>(value: { data?: T } | T): T => value && typeof value === "object" && "data" in value ? (value as { data: T }).data : value as T,
  storeCredential: vi.fn(async () => undefined),
}));

const unavailable = "A verified destination is required.";

describe("AssistantProviderSetup capability isolation", () => {
  beforeEach(() => { host.request.mockReset(); });

  it("routes Ridgeway only through synthetic Review Brief status and configuration", async () => {
    const user = userEvent.setup();
    host.request.mockImplementation(async (method: string) => {
      if (method === "assistant.review_brief.status_synthetic") {
        return { data: { configured: false, provider: null, project_id: "ridgeway", wandb_external_project_id: "wandb-ridgeway", trace_required: true } };
      }
      if (method === "assistant.review_brief.configure_synthetic") {
        return { data: { configured: true, provider: { base_url: "https://api.openai.com/v1", model: "gpt-test", timeout_seconds: 60 }, project_id: "ridgeway", wandb_external_project_id: "wandb-ridgeway", trace_required: true } };
      }
      throw new Error(`Unexpected method ${method}`);
    });

    render(<AssistantProviderSetup projectHandle="project-guided-exact" allowConfiguration unavailableReason={unavailable} capability="RIDGEWAY_REVIEW_BRIEF_ONLY" onStatus={vi.fn()} />);

    expect(await screen.findByText("Review Brief Agent provider is off")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Explicit model ID *"), "gpt-test");
    const configure = screen.getByRole("button", { name: "Configure Review Brief Agent" });
    await waitFor(() => expect(configure).toBeEnabled());
    await user.click(configure);

    expect(await screen.findByText("Review Brief provider ready")).toBeInTheDocument();
    expect(host.request.mock.calls.map(([method]) => method)).toEqual([
      "assistant.review_brief.status_synthetic",
      "assistant.review_brief.configure_synthetic",
    ]);
    expect(JSON.stringify(host.request.mock.calls)).not.toContain('"assistant.status"');
    expect(JSON.stringify(host.request.mock.calls)).not.toContain('"assistant.configure"');
  });

  it("keeps professional Evidence and Intake provider setup on the professional methods", async () => {
    host.request.mockResolvedValue({ data: { configured: false, provider: null, project_id: "professional", wandb_external_project_id: "wandb-professional", trace_required: true } });

    render(<AssistantProviderSetup projectHandle="project-professional" allowConfiguration={false} unavailableReason={unavailable} onStatus={vi.fn()} />);

    expect(await screen.findByText("Traced assistant is off")).toBeInTheDocument();
    expect(host.request).toHaveBeenCalledWith("assistant.status", { project_handle: "project-professional" });
    expect(host.request).not.toHaveBeenCalledWith("assistant.review_brief.status_synthetic", expect.anything());
  });
});
