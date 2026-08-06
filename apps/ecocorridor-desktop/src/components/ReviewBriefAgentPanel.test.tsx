import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  REVIEW_BRIEF_ACKNOWLEDGEMENT,
  ReviewBriefDraftSchema,
  ReviewBriefPreflightSchema,
  type ReviewBriefDraft,
  type ReviewBriefPreflight,
} from "../lib/reviewBrief";
import { ReviewBriefAgentPanel, type ReviewBriefAgentPanelProps } from "./ReviewBriefAgentPanel";

const digest = (value: string) => value.repeat(64).slice(0, 64);

function preflight(overrides: Partial<ReviewBriefPreflight> = {}) {
  return ReviewBriefPreflightSchema.parse({
    source_mode: "RIDGEWAY_SYNTHETIC",
    project_title: "Ridgeway Crossing",
    analysis_id: "analysis-ridgeway",
    portfolio_id: "portfolio_d3aa8b1e170ffd76b322",
    model_revision_id: "ridgeway-v1.2",
    decision_board_sha256: digest("a"),
    scenario_count: 12,
    evidence_claim_count: 19,
    deterministic_available: true,
    deterministic_items: [{
      item_id: "deterministic-road-mask",
      finding_kind: "REVIEW_MASK",
      review_topic: "CURRENT_REDISTRIBUTION_MASK",
      priority: "BEFORE_FEASIBILITY",
      action_kind: "FIELD_VERIFY",
      reviewer_disciplines: ["FIELD"],
      evidence_claim_ids: ["claim-road-mask"],
      direct_run_ids: [],
      scenario_ids: ["scenario-h4"],
      rendered_text: "Inspect the configured current-redistribution review-mask diagnostic and its cited limitations before feasibility review.",
    }],
    disposition: "PASSES",
    next_action: "Candidate to carry forward for ecological, field, and engineering feasibility review.",
    cloud_boundary: "CC0_SYNTHETIC",
    provider_model: "review-brief-model",
    provider_ready: true,
    weave_ready: true,
    agent_release_status: "EXPERIMENTAL",
    production_default: "DETERMINISTIC_CHECKLIST",
    experimental_acknowledgement_required: true,
    eligible: true,
    blocked_reason: null,
    ...overrides,
  });
}

function draft(overrides: Partial<ReviewBriefDraft> = {}) {
  return ReviewBriefDraftSchema.parse({
    schema_version: "review-brief-draft/v1",
    draft_id: "review-brief-1",
    status: "UNCONFIRMED",
    source_mode: "RIDGEWAY_SYNTHETIC",
    analysis_id: "analysis-ridgeway",
    portfolio_id: "portfolio_d3aa8b1e170ffd76b322",
    model_revision_id: "ridgeway-v1.2",
    decision_board_sha256: digest("a"),
    created_at: "2026-08-06T14:30:00Z",
    supersedes_draft_id: null,
    disposition_snapshot: {
      disposition: "PASSES",
      next_action: "Continue to qualified feasibility review.",
      controlling_scenario_id: "scenario-h4",
      safeguard_status: "PASS",
      review_mask_triggered: true,
    },
    items: [{
      item_id: "review-item-road-mask",
      finding_kind: "REVIEW_MASK",
      review_topic: "CURRENT_REDISTRIBUTION_MASK",
      priority: "BEFORE_FEASIBILITY",
      action_kind: "FIELD_VERIFY",
      reviewer_disciplines: ["ECOLOGY", "FIELD"],
      evidence_claim_ids: ["claim-road-mask", "claim-controlling-pair"],
      direct_run_ids: ["matrix-portfolio-candidate-scenario-h4"],
      scenario_ids: ["scenario-h4"],
      rendered_text: "Inspect the configured current-redistribution review-mask diagnostic and its cited limitations before feasibility review.",
      state: "OPEN",
    }],
    limitations: ["Direct Circuitscape result under documented assumptions."],
    trace: {
      trace_id: "weave-trace-review-1",
      weave_call_id: "weave-call-review-1",
      association_id: "association-review-1",
      wandb_run_id: "wandb-run-review-1",
      flushed: true,
      readback_verified: true,
      trace_url: "https://wandb.example/traces/weave-trace-review-1",
    },
    validation: {
      validator_version: "review-brief-validator-v1",
      evidence_ids_valid: true,
      numeric_and_unit_faithful: true,
      required_limitations_present: true,
      unsupported_findings: 0,
      prohibited_claims: 0,
      sensitive_tokens: 0,
      unauthorized_actions: 0,
      source_snapshot_revalidated: true,
    },
    ...overrides,
  });
}

function renderPanel(overrides: Partial<ReviewBriefAgentPanelProps> = {}) {
  const props: ReviewBriefAgentPanelProps = {
    open: true,
    preflight: preflight(),
    runState: { state: "IDLE" },
    drafts: [],
    activeDraftId: null,
    projectHandle: null,
    observabilityConnected: false,
    observabilityMessage: "Optional tracing is not connected.",
    onClose: vi.fn(),
    onGenerate: vi.fn(),
    onSelectDraft: vi.fn(),
    onInspectEvidence: vi.fn(),
    onOpenTrace: vi.fn(),
    onProviderStatusChange: vi.fn(),
    ...overrides,
  };
  return { ...render(<ReviewBriefAgentPanel {...props} />), props };
}

describe("ReviewBriefAgentPanel", () => {
  it("shows a decision-complete Ridgeway preflight and invokes only the real generate callback", async () => {
    const user = userEvent.setup();
    const onGenerate = vi.fn();
    renderPanel({ onGenerate });

    expect(screen.getByRole("dialog", { name: "Review Brief" })).toBeInTheDocument();
    expect(screen.getByText("CC0 synthetic")).toBeInTheDocument();
    expect(screen.getByText("12 scenarios · 19 claims")).toBeInTheDocument();
    expect(screen.getByText(/No approval or model change/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Deterministic field-review checklist" })).toBeInTheDocument();
    expect(screen.getByText(/Production default · local only/i)).toBeInTheDocument();
    const generate = screen.getByRole("button", { name: /Run experimental traced agent/i });
    expect(generate).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /I understand this runs the experimental agent/i }));
    expect(generate).toBeEnabled();
    await user.click(generate);
    expect(onGenerate).toHaveBeenCalledWith(true);
  });

  it("fails closed for a professional project without exact Restricted verification", async () => {
    const user = userEvent.setup();
    const onGenerate = vi.fn();
    const blockedReason = "Verify the exact Restricted W&B destination before sending professional evidence.";
    renderPanel({
      preflight: preflight({
        source_mode: "PROFESSIONAL",
        cloud_boundary: "BLOCKED",
        provider_ready: false,
        weave_ready: false,
        provider_model: null,
        eligible: false,
        blocked_reason: blockedReason,
      }),
      onGenerate,
    });

    expect(screen.getByRole("alert")).toHaveTextContent(blockedReason);
    const generate = screen.getByRole("button", { name: /Run experimental traced agent/i });
    expect(generate).toBeDisabled();
    await user.click(generate);
    expect(onGenerate).not.toHaveBeenCalled();
  });

  it("does not fabricate per-gate progress while the worker reports only a terminal result", () => {
    renderPanel({
      runState: {
        state: "RUNNING",
        stage: "VALIDATING_OUTPUT",
        started_at: "2026-08-06T14:31:00Z",
      },
    });

    expect(screen.getByRole("heading", { name: "Six gates must pass before save" })).toBeInTheDocument();
    expect(screen.getByText("Validate the brief").closest("li")).toHaveClass("stage-pending");
    expect(screen.getByText("Assemble review actions").closest("li")).toHaveClass("stage-pending");
    expect(screen.getByText("Read back the Weave trace").closest("li")).toHaveClass("stage-pending");
    expect(screen.getByText(/reports only terminal success or failure/i)).toBeInTheDocument();
    expect(document.querySelector("[aria-current='step']")).toBeNull();
    expect(screen.queryByText(/\d+% complete/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Generating traced draft/i })).toBeDisabled();
  });

  it("links every checklist citation to the Evidence Rail and records only the non-approval acknowledgement", async () => {
    const user = userEvent.setup();
    const onInspectEvidence = vi.fn();
    const onOpenTrace = vi.fn();
    const onRecordReview = vi.fn();
    const onClose = vi.fn();
    const currentDraft = draft();
    renderPanel({
      drafts: [currentDraft],
      activeDraftId: currentDraft.draft_id,
      runState: { state: "READY", draft_id: currentDraft.draft_id },
      onInspectEvidence,
      onOpenTrace,
      onRecordReview,
      onClose,
    });

    expect(screen.getAllByText("UNCONFIRMED").length).toBeGreaterThan(0);
    expect(screen.getByText(/Trace read back and validated/i)).toBeInTheDocument();
    const draftArticle = screen.getByRole("article", { name: "Human review checklist" });
    const topicHeading = within(draftArticle).getByRole("heading", { name: "Review topic: Current redistribution review mask" });
    const item = topicHeading.closest("li");
    expect(item).not.toBeNull();
    expect(item).toHaveAccessibleName("Review topic: Current redistribution review mask");
    expect(within(item as HTMLElement).getByText(/current-redistribution review-mask diagnostic/i)).toBeInTheDocument();
    expect(within(item as HTMLElement).getByText("Verify in the field")).toBeInTheDocument();
    const evidenceButtons = within(item as HTMLElement).getAllByRole("button", { name: /Evidence/ });
    await user.click(evidenceButtons[0]);
    await user.click(evidenceButtons[1]);
    expect(onInspectEvidence.mock.calls).toEqual([["claim-road-mask"], ["claim-controlling-pair"]]);
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(onClose.mock.invocationCallOrder[0]).toBeLessThan(onInspectEvidence.mock.invocationCallOrder[0]);

    await user.click(screen.getByRole("button", { name: /Open in Weave Agents/i }));
    expect(onOpenTrace).toHaveBeenCalledWith("weave-trace-review-1");

    const recordReview = screen.getByRole("button", { name: "Record human review" });
    expect(recordReview).toBeDisabled();
    await user.type(screen.getByLabelText("Reviewer name or accountable role"), "Agency ecology reviewer");
    await user.click(screen.getByRole("checkbox", { name: REVIEW_BRIEF_ACKNOWLEDGEMENT }));
    expect(recordReview).toBeEnabled();
    await user.click(recordReview);
    expect(onRecordReview).toHaveBeenCalledWith(currentDraft.draft_id, REVIEW_BRIEF_ACKNOWLEDGEMENT, "Agency ecology reviewer");
    expect(screen.getByText(/not model attestation or portfolio approval/i)).toBeInTheDocument();
  });

  it("keeps stale history inspectable while preventing a human-review record", () => {
    const staleDraft = draft({ draft_id: "review-brief-stale", status: "STALE" });
    renderPanel({
      drafts: [staleDraft],
      activeDraftId: staleDraft.draft_id,
      onRecordReview: vi.fn(),
    });

    expect(screen.getByRole("alert")).toHaveTextContent("The source evidence changed.");
    expect(screen.getByRole("checkbox", { name: REVIEW_BRIEF_ACKNOWLEDGEMENT })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Record human review" })).toBeDisabled();
  });

  it("switches immutable history through the integration callback", async () => {
    const user = userEvent.setup();
    const first = draft();
    const second = draft({
      draft_id: "review-brief-2",
      created_at: "2026-08-06T15:00:00Z",
      supersedes_draft_id: first.draft_id,
    });
    const onSelectDraft = vi.fn();
    renderPanel({ drafts: [second, first], activeDraftId: first.draft_id, onSelectDraft });

    await user.selectOptions(screen.getByLabelText("Open retained draft"), second.draft_id);
    expect(onSelectDraft).toHaveBeenCalledWith(second.draft_id);
  });

  it("shows immutable supersession and acknowledgement readback details", () => {
    const first = draft();
    const second = draft({
      draft_id: "review-brief-2",
      created_at: "2026-08-06T15:00:00Z",
      supersedes_draft_id: first.draft_id,
    });
    renderPanel({
      drafts: [second, first],
      activeDraftId: second.draft_id,
      acknowledgements: [{
        acknowledgement_id: "ack-review-2",
        draft_id: second.draft_id,
        reviewer: "Agency ecology reviewer",
        statement: REVIEW_BRIEF_ACKNOWLEDGEMENT,
        acknowledged_at: "2026-08-06T15:05:00Z",
        model_unchanged: true,
        portfolio_selected: false,
      }],
      onRecordReview: vi.fn(),
    });

    expect(screen.getByText(first.draft_id)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Factual-completeness review recorded" })).toBeInTheDocument();
    expect(screen.getByText("Agency ecology reviewer")).toBeInTheDocument();
    expect(screen.getByText(REVIEW_BRIEF_ACKNOWLEDGEMENT)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record human review" })).not.toBeInTheDocument();
  });

  it("announces operation errors as errors even after an agent run failed", () => {
    renderPanel({
      runState: { state: "FAILED", code: "REVIEW_BRIEF_TRACE_FAILED", message: "No draft was saved." },
      operationMessage: { tone: "ERROR", text: "The retained acknowledgement could not be read back." },
    });

    expect(screen.getAllByRole("alert").some((item) => item.textContent?.includes("acknowledgement could not be read back"))).toBe(true);
    expect(screen.getByText("Review Brief action failed")).toBeInTheDocument();
  });

  it("traps focus, closes on Escape, and restores focus to the opener", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(false);
      return <>
        <button type="button" onClick={() => setOpen(true)}>Open review brief</button>
        <ReviewBriefAgentPanel
          open={open}
          preflight={preflight()}
          runState={{ state: "IDLE" }}
          drafts={[]}
          activeDraftId={null}
          projectHandle={null}
          observabilityConnected={false}
          observabilityMessage="Optional tracing is not connected."
          onClose={() => setOpen(false)}
          onGenerate={() => undefined}
          onSelectDraft={() => undefined}
          onInspectEvidence={() => undefined}
          onOpenTrace={() => undefined}
          onProviderStatusChange={() => undefined}
        />
      </>;
    }
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open review brief" });
    await user.click(opener);
    const close = screen.getByRole("button", { name: "Close review brief" });
    await waitFor(() => expect(close).toHaveFocus());
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Review Brief" })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("does not recapture focus when a parent operation message rerenders the open modal", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(false);
      const [message, setMessage] = useState<ReviewBriefAgentPanelProps["operationMessage"]>(null);
      const currentDraft = draft();
      return <>
        <button type="button" onClick={() => setOpen(true)}>Open focus test</button>
        <ReviewBriefAgentPanel
          open={open}
          preflight={preflight()}
          runState={{ state: "READY", draft_id: currentDraft.draft_id }}
          drafts={[currentDraft]}
          activeDraftId={currentDraft.draft_id}
          projectHandle={null}
          observabilityConnected={false}
          observabilityMessage="Optional tracing is not connected."
          operationMessage={message}
          onClose={() => setOpen(false)}
          onGenerate={() => undefined}
          onSelectDraft={() => undefined}
          onInspectEvidence={() => undefined}
          onOpenTrace={() => undefined}
          onProviderStatusChange={() => undefined}
          onFeedback={() => setMessage({ tone: "SUCCESS", text: "Feedback recorded." })}
        />
      </>;
    }
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open focus test" });
    await user.click(opener);
    const feedback = screen.getByRole("button", { name: "Yes" });
    await user.click(feedback);
    expect(await screen.findByText("Feedback recorded.")).toBeInTheDocument();
    expect(feedback).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(opener).toHaveFocus();
  });

  it("has no serious or critical automated accessibility findings", async () => {
    const currentDraft = draft();
    renderPanel({ drafts: [currentDraft], activeDraftId: currentDraft.draft_id, onRecordReview: vi.fn() });
    const result = await axe.run(document.body, {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.violations.filter((item) => ["serious", "critical"].includes(item.impact ?? ""))).toEqual([]);
  });
});
