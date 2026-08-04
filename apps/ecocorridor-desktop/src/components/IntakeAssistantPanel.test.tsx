import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HostBridge, ProfessionalAssetRecord, ProfessionalProjectSession } from "../types";
import { IntakeAssistantPanel } from "./IntakeAssistantPanel";

const projectSession: ProfessionalProjectSession = {
  projectHandle: "project_intake_opaque",
  projectId: "project-intake",
  title: "Reviewed intake project",
  mode: "IMPORTED",
  bindingState: "CONNECTED",
  bindingMessage: "Exact-scope Restricted destination connected.",
};

function asset(overrides: Partial<ProfessionalAssetRecord> = {}): ProfessionalAssetRecord {
  return {
    assetId: "document-reviewed",
    title: "Reviewed planning record",
    kind: "DOCUMENT",
    displayName: "planning-record.txt",
    relativePath: "data_assets/documents/planning-record.txt",
    sha256: "a".repeat(64),
    byteSize: 256,
    mediaType: "text/plain",
    source: "Agency planning record",
    vintage: "2026",
    license: "Internal reviewed source",
    sensitivity: "INTERNAL",
    cloudDisposition: "RAW_ALLOWED",
    exportAllowed: false,
    approvedDeployments: ["DEDICATED"],
    reviewer: "Agency reviewer",
    reviewedAt: "2026-08-04T12:00:00Z",
    intendedUse: "Evidence intake only",
    revision: 1,
    grid: null,
    authorization: {} as ProfessionalAssetRecord["authorization"],
    ...overrides,
  };
}

function payload(method: string, data: unknown) {
  return { api_version: "1.0", method, data, data_sha256: "b".repeat(64) };
}

function configuredStatus() {
  return {
    configured: true,
    provider: { base_url: "https://provider.example/v1", model: "reviewed-model", timeout_seconds: 60 },
    project_id: projectSession.projectId,
    wandb_external_project_id: "wandb-intake-project",
    trace_required: true as const,
  };
}

function intakeResponse(callId: string, documentId: string, text: string) {
  return {
    draft: {
      draft_id: "intake-draft-1",
      call_id: callId,
      project_id: projectSession.projectId,
      fields: [{
        field_id: "field-cost-limit",
        field_name: "cost_limit",
        value: "Screening cost must remain below 200000 USD.",
        citations: [{ document_id: documentId, page_number: 1, start: 0, end: text.length, quote: text }],
        confirmed: false,
      }],
      limitations: ["Draft evidence only; no ecological value or model object was created."],
      provider_call_id: "provider-call-1",
      provider_model: "reviewed-model",
      trace_id: "weave-trace-1",
      trace_url: "https://wandb.example/traces/weave-trace-1",
      association: {
        association_id: "association-1",
        project_id: projectSession.projectId,
        wandb_external_project_id: "wandb-intake-project",
        wandb_run_id: "wandb-run-1",
        wandb_run_kind: "assistant_evaluation",
        run_subject_id: callId,
      },
      deterministic_validation: "PASSED",
      validator_version: "ecocorridor-intake-validator-v1",
      trace_readback_verified: true,
      requires_human_confirmation: true,
      applied_to_model: false,
    },
    ready_for_model: false,
    human_confirmation_required: true,
  };
}

afterEach(() => {
  delete window.__ECOCORRIDOR_HOST__;
});

describe("Intake Assistant retained-document eligibility", () => {
  it.each([
    {
      name: "raw sensitive-location document",
      document: asset({ sensitivity: "SENSITIVE_LOCATION" }),
      reason: "Generalized derivative required for assistant use.",
      remediation: /create, review, retain, and select an approved generalized derivative/i,
    },
    {
      name: "PDF without a verified text representation",
      document: asset({ assetId: "document-pdf", displayName: "planning-record.pdf", mediaType: "application/pdf" }),
      reason: "Verified text representation required.",
      remediation: /retain and authorize a UTF-8 text\/plain or text\/markdown derivative/i,
    },
  ])("blocks $name before any assistant intake request", async ({ document, reason, remediation }) => {
    const user = userEvent.setup();
    const request = vi.fn(async (method: string) => {
      if (method === "assistant.status") return payload(method, configuredStatus());
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

    render(<IntakeAssistantPanel projectSession={projectSession} deployment="DEDICATED" documents={[document]} defaultReviewer="Agency reviewer" />);
    expect(await screen.findByText("Traced provider ready")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Retained supporting document *"), document.assetId);

    expect(screen.getByRole("alert")).toHaveTextContent(reason);
    expect(screen.getByRole("alert")).toHaveTextContent(remediation);
    expect(screen.getByLabelText("Exact retained page text *")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Extract cited draft" })).toBeDisabled();
    expect(request.mock.calls.filter(([method]) => method === "assistant.intake.extract")).toHaveLength(0);
  });

  it("allows eligible retained text only through the traced worker request", async () => {
    const user = userEvent.setup();
    const document = asset();
    const pageText = "Screening cost must remain below 200000 USD.";
    const request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === "assistant.status") return payload(method, configuredStatus());
      if (method === "assistant.intake.extract") return payload(method, intakeResponse(String(params.call_id), document.assetId, pageText));
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

    render(<IntakeAssistantPanel projectSession={projectSession} deployment="DEDICATED" documents={[document]} defaultReviewer="Agency reviewer" />);
    expect(await screen.findByText("Traced provider ready")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Retained supporting document *"), document.assetId);

    expect(screen.getByRole("note")).toHaveTextContent("Eligible for exact worker verification on DEDICATED.");
    const pageTextInput = screen.getByLabelText("Exact retained page text *");
    expect(pageTextInput).toBeEnabled();
    await user.type(pageTextInput, pageText);
    const extract = screen.getByRole("button", { name: "Extract cited draft" });
    expect(extract).toBeEnabled();
    await user.click(extract);

    expect(await screen.findByText("Trace read back · deterministic citation validation passed")).toBeInTheDocument();
    await waitFor(() => expect(request.mock.calls.filter(([method]) => method === "assistant.intake.extract")).toHaveLength(1));
    const call = request.mock.calls.find(([method]) => method === "assistant.intake.extract");
    expect(call?.[1]).toMatchObject({
      project_handle: projectSession.projectHandle,
      pages: [{ document_id: document.assetId, page_number: 1, text: pageText }],
      sensitive_policy: { prohibited_terms: [], exact_location_authorized: false },
    });
  });

  it("keeps eligible document status distinct while a disconnected project blocks extraction", async () => {
    const user = userEvent.setup();
    const document = asset();
    const disconnectedSession: ProfessionalProjectSession = {
      ...projectSession,
      bindingState: "AUTHENTICATION_REQUIRED",
      bindingMessage: "Reconnect the exact-scope Restricted destination.",
    };
    const request = vi.fn(async (method: string) => {
      if (method === "assistant.status") return payload(method, configuredStatus());
      throw new Error(`Unexpected method ${method}`);
    });
    window.__ECOCORRIDOR_HOST__ = { request: request as unknown as HostBridge["request"] };

    render(<IntakeAssistantPanel projectSession={disconnectedSession} deployment="DEDICATED" documents={[document]} defaultReviewer="Agency reviewer" />);
    await user.selectOptions(screen.getByLabelText("Retained supporting document *"), document.assetId);

    expect(screen.getByRole("note")).toHaveTextContent("Eligible for exact worker verification on DEDICATED.");
    const pageTextInput = screen.getByLabelText("Exact retained page text *");
    expect(pageTextInput).toBeEnabled();
    await user.type(pageTextInput, "Screening cost must remain below 200000 USD.");
    expect(screen.getByRole("button", { name: "Extract cited draft" })).toBeDisabled();
    expect(request.mock.calls.filter(([method]) => method === "assistant.intake.extract")).toHaveLength(0);
  });
});
