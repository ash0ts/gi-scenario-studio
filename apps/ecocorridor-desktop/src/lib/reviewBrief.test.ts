import { describe, expect, it } from "vitest";

import {
  isReviewBriefInvocationCurrent,
  ReviewBriefDraftSchema,
  ReviewBriefPreflightSchema,
  reviewBriefCurrentDraft,
  reviewBriefScopeKey,
} from "./reviewBrief";

const sha = "a".repeat(64);

function item() {
  return {
    item_id: "review-item-one",
    finding_kind: "REVIEW_MASK" as const,
    review_topic: "CURRENT_REDISTRIBUTION_MASK" as const,
    priority: "BEFORE_FEASIBILITY" as const,
    action_kind: "FIELD_VERIFY" as const,
    reviewer_disciplines: ["FIELD" as const],
    evidence_claim_ids: ["claim-one"],
    rendered_text: "Inspect the configured review mask.",
  };
}

function draft(draftId = "draft-one", supersedesDraftId: string | null = null) {
  return {
    schema_version: "review-brief-draft/v1" as const,
    draft_id: draftId,
    status: "UNCONFIRMED" as const,
    source_mode: "RIDGEWAY_SYNTHETIC" as const,
    analysis_id: "analysis-one",
    portfolio_id: "portfolio-one",
    model_revision_id: "revision-one",
    decision_board_sha256: sha,
    created_at: "2026-08-06T12:00:00Z",
    supersedes_draft_id: supersedesDraftId,
    disposition_snapshot: {
      disposition: "PASSES" as const,
      next_action: "Continue qualified review.",
      controlling_scenario_id: "scenario-one",
      safeguard_status: "PASS" as const,
      review_mask_triggered: true,
    },
    items: [item()],
    limitations: ["Direct Circuitscape result under documented assumptions."],
    trace: {
      trace_id: `trace-${draftId}`,
      weave_call_id: `call-${draftId}`,
      association_id: `association-${draftId}`,
      wandb_run_id: `run-${draftId}`,
      flushed: true as const,
      readback_verified: true as const,
      trace_url: null,
    },
    validation: {
      validator_version: "review-brief-validator-v1" as const,
      evidence_ids_valid: true as const,
      numeric_and_unit_faithful: true as const,
      required_limitations_present: true as const,
      unsupported_findings: 0 as const,
      prohibited_claims: 0 as const,
      sensitive_tokens: 0 as const,
      unauthorized_actions: 0 as const,
      source_snapshot_revalidated: true as const,
    },
  };
}

describe("Review Brief frontend contracts", () => {
  it("normalizes optional deterministic item arrays without hiding checklist availability", () => {
    const parsed = ReviewBriefPreflightSchema.parse({
      source_mode: "RIDGEWAY_SYNTHETIC",
      project_title: "Ridgeway Crossing",
      analysis_id: "analysis-one",
      portfolio_id: "portfolio-one",
      model_revision_id: "revision-one",
      decision_board_sha256: sha,
      scenario_count: 12,
      evidence_claim_count: 1,
      deterministic_available: true,
      deterministic_items: [item()],
      disposition: "PASSES",
      next_action: "Continue qualified review.",
      cloud_boundary: "CC0_SYNTHETIC",
      provider_model: "provider-model",
      provider_ready: true,
      weave_ready: true,
      agent_release_status: "EXPERIMENTAL",
      production_default: "DETERMINISTIC_CHECKLIST",
      experimental_acknowledgement_required: true,
      eligible: true,
      blocked_reason: null,
    });

    expect(parsed.deterministic_items[0].direct_run_ids).toEqual([]);
    expect(parsed.deterministic_items[0].scenario_ids).toEqual([]);
  });

  it("rejects a preflight that omits its explicit production-default evidence", () => {
    const result = ReviewBriefPreflightSchema.safeParse({
      source_mode: "RIDGEWAY_SYNTHETIC",
      project_title: "Ridgeway Crossing",
      analysis_id: "analysis-one",
      portfolio_id: "portfolio-one",
      model_revision_id: "revision-one",
      decision_board_sha256: sha,
      scenario_count: 12,
      evidence_claim_count: 1,
      disposition: "PASSES",
      next_action: "Continue qualified review.",
      cloud_boundary: "CC0_SYNTHETIC",
      provider_model: "provider-model",
      provider_ready: true,
      weave_ready: true,
      eligible: true,
      blocked_reason: null,
    });
    expect(result.success).toBe(false);
  });

  it("never converts omitted trace or deterministic-validation receipts into a pass", () => {
    const missingTrace = draft();
    const missingTraceResult = ReviewBriefDraftSchema.safeParse({
      ...missingTrace,
      trace: { ...missingTrace.trace, readback_verified: undefined },
    });
    expect(missingTraceResult.success).toBe(false);

    const missingValidation = draft();
    const missingValidationResult = ReviewBriefDraftSchema.safeParse({
      ...missingValidation,
      validation: { ...missingValidation.validation, evidence_ids_valid: undefined },
    });
    expect(missingValidationResult.success).toBe(false);
  });

  it("derives one connected latest draft independently of response ordering", () => {
    const first = ReviewBriefDraftSchema.parse(draft("draft-one"));
    const second = ReviewBriefDraftSchema.parse(draft("draft-two", first.draft_id));
    expect(reviewBriefCurrentDraft([first, second])).toEqual(second);
    expect(reviewBriefCurrentDraft([second, first])).toEqual(second);

    const fork = ReviewBriefDraftSchema.parse(draft("draft-three", first.draft_id));
    expect(() => reviewBriefCurrentDraft([first, second, fork])).toThrow(/forked version chain/i);
  });

  it("accepts an invocation result only for the exact still-active scope", () => {
    const firstScope = reviewBriefScopeKey({
      project_handle: "project-one",
      analysis_id: "analysis-one",
      portfolio_id: "portfolio-one",
    });
    const secondScope = reviewBriefScopeKey({
      project_handle: "project-one",
      analysis_id: "analysis-one",
      portfolio_id: "portfolio-two",
    });
    const active = { invocation_id: "invocation-one", scope_key: firstScope };

    expect(isReviewBriefInvocationCurrent(active, "invocation-one", firstScope, firstScope)).toBe(true);
    expect(isReviewBriefInvocationCurrent(active, "invocation-one", firstScope, secondScope)).toBe(false);
    expect(isReviewBriefInvocationCurrent(active, "invocation-two", firstScope, firstScope)).toBe(false);
  });
});
