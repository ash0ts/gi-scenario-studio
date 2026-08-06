import { z } from "zod";
import {
  ReviewBriefDraftSchema as GeneratedReviewBriefDraftSchema,
  ReviewBriefPreflightSchema as GeneratedReviewBriefPreflightSchema,
} from "../generated/contracts";

export const REVIEW_BRIEF_ACKNOWLEDGEMENT =
  "I reviewed the cited evidence and limitations. This records review of the brief only; it does not select, approve, attest, or design a portfolio." as const;

function requiredField(
  context: z.RefinementCtx,
  value: unknown,
  path: Array<string | number>,
  message: string,
) {
  if (value === undefined) context.addIssue({ code: "custom", path, message });
}

export const ReviewBriefPreflightSchema = GeneratedReviewBriefPreflightSchema
  .superRefine((value, context) => {
    requiredField(context, value.agent_release_status, ["agent_release_status"], "agent release status must be explicit");
    requiredField(context, value.production_default, ["production_default"], "production default must be explicit");
    requiredField(context, value.experimental_acknowledgement_required, ["experimental_acknowledgement_required"], "experimental acknowledgement policy must be explicit");
    requiredField(context, value.deterministic_available, ["deterministic_available"], "deterministic checklist availability must be explicit");
    requiredField(context, value.deterministic_items, ["deterministic_items"], "deterministic checklist items must be explicit");
  })
  .transform((value) => ({
    ...value,
    provider_model: value.provider_model ?? null,
    blocked_reason: value.blocked_reason ?? null,
    agent_release_status: value.agent_release_status ?? "EXPERIMENTAL" as const,
    production_default: value.production_default ?? "DETERMINISTIC_CHECKLIST" as const,
    experimental_acknowledgement_required: value.experimental_acknowledgement_required ?? true,
    deterministic_available: value.deterministic_available ?? false,
    deterministic_items: (value.deterministic_items ?? []).map((item) => ({
      ...item,
      direct_run_ids: item.direct_run_ids ?? [],
      scenario_ids: item.scenario_ids ?? [],
    })),
  }))
  .superRefine((value, context) => {
    const boundaryAllowed =
      (value.source_mode === "RIDGEWAY_SYNTHETIC" && value.cloud_boundary === "CC0_SYNTHETIC")
      || (value.source_mode === "PROFESSIONAL" && value.cloud_boundary === "RESTRICTED_VERIFIED");
    if (value.eligible && (!boundaryAllowed || !value.provider_ready || !value.weave_ready)) {
      context.addIssue({
        code: "custom",
        path: ["eligible"],
        message: "an eligible review brief requires an allowed boundary, provider, and Weave",
      });
    }
    if (!value.eligible && !value.blocked_reason) {
      context.addIssue({
        code: "custom",
        path: ["blocked_reason"],
        message: "an ineligible review brief requires a remediation message",
      });
    }
    if (value.agent_release_status === "EXPERIMENTAL" && (value.production_default !== "DETERMINISTIC_CHECKLIST" || !value.experimental_acknowledgement_required)) {
      context.addIssue({
        code: "custom",
        path: ["agent_release_status"],
        message: "an experimental agent requires an explicit opt-in and deterministic production default",
      });
    }
    if (value.deterministic_available !== (value.deterministic_items.length > 0)) {
      context.addIssue({
        code: "custom",
        path: ["deterministic_available"],
        message: "deterministic checklist availability must match its authoritative items",
      });
    }
  });

export const ReviewBriefRunStageSchema = z.enum([
  "VERIFYING_PORTFOLIO",
  "RESOLVING_EVIDENCE",
  "PLANNING_REVIEW",
  "VALIDATING_OUTPUT",
  "READING_TRACE",
  "SAVING_DRAFT",
]);

export const ReviewBriefRunStateSchema = z.discriminatedUnion("state", [
  z.object({ state: z.literal("IDLE") }),
  z.object({
    state: z.literal("RUNNING"),
    stage: ReviewBriefRunStageSchema,
    started_at: z.string().datetime(),
  }),
  z.object({
    state: z.literal("FAILED"),
    code: z.string().trim().min(1),
    message: z.string().trim().min(1),
  }),
  z.object({
    state: z.literal("READY"),
    draft_id: z.string().trim().min(1),
  }),
]);

export const ReviewBriefDraftSchema = GeneratedReviewBriefDraftSchema
  .superRefine((draft, context) => {
    requiredField(context, draft.schema_version, ["schema_version"], "review-brief schema version must be explicit");
    requiredField(context, draft.status, ["status"], "review-brief status must be explicit");
    if (draft.trace.flushed !== true) {
      context.addIssue({ code: "custom", path: ["trace", "flushed"], message: "trace flush confirmation must be explicit" });
    }
    if (draft.trace.readback_verified !== true) {
      context.addIssue({ code: "custom", path: ["trace", "readback_verified"], message: "trace read-back confirmation must be explicit" });
    }
    const validation = draft.validation;
    const exactValidation: Array<[keyof typeof validation, unknown]> = [
      ["validator_version", validation.validator_version],
      ["evidence_ids_valid", validation.evidence_ids_valid],
      ["numeric_and_unit_faithful", validation.numeric_and_unit_faithful],
      ["required_limitations_present", validation.required_limitations_present],
      ["unsupported_findings", validation.unsupported_findings],
      ["prohibited_claims", validation.prohibited_claims],
      ["sensitive_tokens", validation.sensitive_tokens],
      ["unauthorized_actions", validation.unauthorized_actions],
      ["source_snapshot_revalidated", validation.source_snapshot_revalidated],
    ];
    for (const [key, value] of exactValidation) {
      requiredField(context, value, ["validation", key], `${String(key)} must be explicit`);
    }
  })
  .transform((draft) => ({
  ...draft,
  schema_version: draft.schema_version ?? "review-brief-draft/v1" as const,
  status: draft.status ?? "UNCONFIRMED" as const,
  supersedes_draft_id: draft.supersedes_draft_id ?? null,
  disposition_snapshot: {
    ...draft.disposition_snapshot,
    controlling_scenario_id: draft.disposition_snapshot.controlling_scenario_id ?? null,
  },
  items: draft.items.map((item) => ({
    ...item,
    direct_run_ids: item.direct_run_ids ?? [],
    scenario_ids: item.scenario_ids ?? [],
    state: item.state ?? "OPEN" as const,
  })),
  trace: {
    ...draft.trace,
    flushed: draft.trace.flushed as true,
    readback_verified: draft.trace.readback_verified as true,
    trace_url: draft.trace.trace_url ?? null,
  },
  validation: {
    ...draft.validation,
    validator_version: draft.validation.validator_version as "review-brief-validator-v1",
    evidence_ids_valid: draft.validation.evidence_ids_valid as true,
    numeric_and_unit_faithful: draft.validation.numeric_and_unit_faithful as true,
    required_limitations_present: draft.validation.required_limitations_present as true,
    unsupported_findings: draft.validation.unsupported_findings as 0,
    prohibited_claims: draft.validation.prohibited_claims as 0,
    sensitive_tokens: draft.validation.sensitive_tokens as 0,
    unauthorized_actions: draft.validation.unauthorized_actions as 0,
    source_snapshot_revalidated: draft.validation.source_snapshot_revalidated as true,
  },
}));

export type ReviewBriefPreflight = z.infer<typeof ReviewBriefPreflightSchema>;
export type ReviewBriefRunStage = z.infer<typeof ReviewBriefRunStageSchema>;
export type ReviewBriefRunState = z.infer<typeof ReviewBriefRunStateSchema>;
export type ReviewBriefDraft = z.infer<typeof ReviewBriefDraftSchema>;
export type ReviewBriefItem = ReviewBriefDraft["items"][number];

export const ReviewBriefAcknowledgementSchema = z.object({
    acknowledgement_id: z.string().min(1),
    draft_id: z.string().min(1),
    reviewer: z.string().min(1),
    statement: z.literal(REVIEW_BRIEF_ACKNOWLEDGEMENT),
    acknowledged_at: z.string().datetime(),
    model_unchanged: z.literal(true),
    portfolio_selected: z.literal(false),
  }).strict();
export const ReviewBriefDraftResponseSchema = z.object({
  draft: ReviewBriefDraftSchema,
  acknowledgement: ReviewBriefAcknowledgementSchema.nullable(),
}).strict();
export const ReviewBriefListResponseSchema = z.object({
  drafts: z.array(ReviewBriefDraftSchema),
  acknowledgements: z.array(ReviewBriefAcknowledgementSchema),
}).strict().superRefine((value, context) => {
  const draftIds = new Set(value.drafts.map((draft) => draft.draft_id));
  const acknowledged = new Set<string>();
  for (const [index, acknowledgement] of value.acknowledgements.entries()) {
    if (!draftIds.has(acknowledgement.draft_id)) {
      context.addIssue({ code: "custom", path: ["acknowledgements", index, "draft_id"], message: "acknowledgement must reference a returned draft" });
    }
    if (acknowledged.has(acknowledgement.draft_id)) {
      context.addIssue({ code: "custom", path: ["acknowledgements", index, "draft_id"], message: "only one acknowledgement may be returned per immutable draft" });
    }
    acknowledged.add(acknowledgement.draft_id);
  }
});
export const ReviewBriefAcknowledgementResponseSchema = z.object({
  recorded: z.literal(true),
  acknowledgement: ReviewBriefAcknowledgementSchema,
}).strict();
export const ReviewBriefFeedbackResponseSchema = z.object({
  recorded: z.literal(true),
  feedback: z.object({
    feedback_id: z.string().min(1),
    draft_id: z.string().min(1),
    rating: z.enum(["HELPFUL", "NOT_HELPFUL"]),
    comment: z.string(),
    recorded_at: z.string().datetime(),
    trace_receipt: z.object({
      weave_feedback_id: z.string().min(1),
      trace_id: z.string().min(1),
      weave_call_id: z.string().min(1),
      wandb_run_id: z.string().min(1),
      verified: z.literal(true),
    }).strict(),
  }).strict(),
}).strict();

export const RIDGEWAY_REVIEW_BRIEF_ANALYSIS_ID = "ridgeway-verified-replay-v1.2" as const;

export type ReviewBriefAcknowledgement = z.infer<typeof ReviewBriefAcknowledgementSchema>;

export type ReviewBriefScope = Record<string, unknown> & {
  project_handle: string;
  analysis_id: string;
  portfolio_id: string;
};

export interface ReviewBriefInvocationIdentity {
  invocation_id: string;
  scope_key: string;
}

export function reviewBriefScopeKey(scope: ReviewBriefScope): string {
  return JSON.stringify([scope.project_handle, scope.analysis_id, scope.portfolio_id]);
}

export function reviewBriefCurrentDraft(drafts: ReviewBriefDraft[]): ReviewBriefDraft | null {
  if (!drafts.length) return null;
  const identities = new Set(drafts.map((draft) => draft.draft_id));
  if (identities.size !== drafts.length) throw new Error("Review Brief history contains duplicate draft IDs.");
  const analysisIds = new Set(drafts.map((draft) => draft.analysis_id));
  const portfolioIds = new Set(drafts.map((draft) => draft.portfolio_id));
  if (analysisIds.size !== 1 || portfolioIds.size !== 1) {
    throw new Error("Review Brief history spans more than one decision scope.");
  }
  const parentIds = new Set<string>();
  for (const draft of drafts) {
    if (draft.supersedes_draft_id === null) continue;
    if (!identities.has(draft.supersedes_draft_id)) {
      throw new Error("Review Brief history references a missing superseded draft.");
    }
    if (parentIds.has(draft.supersedes_draft_id)) {
      throw new Error("Review Brief history contains a forked version chain.");
    }
    parentIds.add(draft.supersedes_draft_id);
  }
  const tips = drafts.filter((draft) => !parentIds.has(draft.draft_id));
  if (tips.length !== 1) throw new Error("Review Brief history has no unique current draft.");
  let cursor: ReviewBriefDraft | undefined = tips[0];
  const visited = new Set<string>();
  while (cursor) {
    if (visited.has(cursor.draft_id)) throw new Error("Review Brief history contains a cycle.");
    visited.add(cursor.draft_id);
    cursor = cursor.supersedes_draft_id === null
      ? undefined
      : drafts.find((draft) => draft.draft_id === cursor?.supersedes_draft_id);
  }
  if (visited.size !== drafts.length) throw new Error("Review Brief history is not one connected version chain.");
  return tips[0];
}

export function isReviewBriefInvocationCurrent(
  active: ReviewBriefInvocationIdentity | null,
  invocationId: string,
  invocationScopeKey: string,
  currentScopeKey: string,
): boolean {
  return Boolean(
    active
    && active.invocation_id === invocationId
    && active.scope_key === invocationScopeKey
    && invocationScopeKey === currentScopeKey,
  );
}
