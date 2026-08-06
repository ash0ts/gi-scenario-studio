import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RIDGEWAY_SYNTHETIC_TRACE_CONSENT, WandbGate } from "./WandbGate";

const host = vi.hoisted(() => ({ request: vi.fn() }));

vi.mock("../lib/host", () => ({
  credentialStatus: vi.fn(async () => true),
  hasNativeHost: vi.fn(() => true),
  hostErrorMessage: (reason: unknown, fallback: string) => reason instanceof Error ? reason.message : fallback,
  hostRequest: host.request,
  rpcData: <T,>(value: { data?: T } | T): T => value && typeof value === "object" && "data" in value ? (value as { data: T }).data : value as T,
  storeCredential: vi.fn(async () => undefined),
  wandbCredentialId: (_baseUrl: string, entity: string, project: string) => `credential-${entity}-${project}`,
}));

describe("WandbGate capability routing", () => {
  beforeEach(() => { host.request.mockReset(); });

  it("uses the exact opened Ridgeway handle and consent for W&B Cloud trace-only preflight", async () => {
    const user = userEvent.setup();
    const onVerified = vi.fn();
    host.request.mockResolvedValue({
      data: {
        preflight_id: "synthetic_trace_preflight_fixture123",
        scope: "CC0_SYNTHETIC_TRACE_ONLY",
        allowed: true,
        visibility_claim: "NONE",
        restricted_verified: false,
        weave_capability: "PENDING_NATIVE_AGENT_PREFLIGHT",
        binding_preview: { entity: "wandb", project: "ecocorridor-scenario-studio", deployment: "WANDB_CLOUD" },
      },
    });

    render(<WandbGate path="guided" projectHandle="project-guided-exact" onClose={vi.fn()} onVerified={onVerified} />);

    const cloud = screen.getByRole("radio", { name: /W&B Cloud/i });
    expect(cloud).toBeEnabled();
    expect(cloud).toBeChecked();
    await user.type(screen.getByLabelText("Entity or team"), "wandb");
    await user.type(screen.getByLabelText("Project"), "ecocorridor-scenario-studio");
    await user.click(screen.getByRole("checkbox", { name: /I authorize alias-only agent interaction/i }));
    const verify = screen.getByRole("button", { name: /Verify trace-only target/i });
    await waitFor(() => expect(verify).toBeEnabled());
    await user.click(verify);

    expect(host.request).toHaveBeenCalledWith("observability.synthetic_trace.preflight", expect.objectContaining({
      project_handle: "project-guided-exact",
      consent: RIDGEWAY_SYNTHETIC_TRACE_CONSENT,
      deployment: "WANDB_CLOUD",
      api_base_url: "https://api.wandb.ai",
      web_base_url: "https://wandb.ai",
      entity: "wandb",
      project: "ecocorridor-scenario-studio",
    }));
    expect(onVerified).toHaveBeenCalledWith(expect.objectContaining({
      purpose: "RIDGEWAY_SYNTHETIC_TRACE_ONLY",
      projectHandle: "project-guided-exact",
      deployment: "WANDB_CLOUD",
    }));
    expect(screen.getByRole("status")).toHaveTextContent(/visibility was not asserted/i);
    expect(JSON.stringify(host.request.mock.calls)).not.toContain("full_sync");
  });

  it("keeps professional onboarding on the exact-Restricted method with W&B Cloud disabled", async () => {
    const user = userEvent.setup();
    const onVerified = vi.fn();
    host.request.mockResolvedValue({
      data: { preflight_id: "preflight_professional", scope: "RESTRICTED", allowed: true },
    });

    render(<WandbGate path="planning" projectHandle={null} onClose={vi.fn()} onVerified={onVerified} />);

    expect(screen.getByRole("radio", { name: /W&B Cloud/i })).toBeDisabled();
    await user.type(screen.getByLabelText("API base URL"), "https://api.wandb.example.org");
    await user.type(screen.getByLabelText("Entity or team"), "wildlife-program");
    await user.type(screen.getByLabelText("Project"), "reviewed-connectivity");
    await user.click(screen.getByRole("checkbox", { name: /classify each asset/i }));
    const verify = screen.getByRole("button", { name: /Verify Restricted project/i });
    await waitFor(() => expect(verify).toBeEnabled());
    await user.click(verify);

    expect(host.request).toHaveBeenCalledWith("observability.preflight", expect.not.objectContaining({
      project_handle: expect.anything(),
      consent: expect.anything(),
    }));
    expect(onVerified).toHaveBeenCalledWith(expect.objectContaining({
      purpose: "PROFESSIONAL_FULL_SYNC",
      projectHandle: null,
      deployment: "DEDICATED",
    }));
  });
});
