import { z } from "zod";

export const AssistantStatusSchema = z.object({
  configured: z.boolean(),
  provider: z.object({
    base_url: z.string().min(1),
    model: z.string().min(1),
    timeout_seconds: z.number().positive(),
  }).strict().nullable().optional(),
  project_id: z.string().min(1).nullable().optional(),
  wandb_external_project_id: z.string().min(1).nullable().optional(),
  trace_required: z.literal(true).optional(),
  cleared: z.literal(true).optional(),
}).strict();

export type AssistantStatus = z.infer<typeof AssistantStatusSchema>;

const NumericEvidenceReferenceSchema = z.object({
  evidence_id: z.string().min(1),
  value: z.number().finite(),
  units: z.string().min(1),
}).strict();

export const AssistantRunAssociationSchema = z.object({
  association_id: z.string().min(1),
  project_id: z.string().min(1),
  wandb_external_project_id: z.string().min(1),
  wandb_run_id: z.string().min(1),
  wandb_run_kind: z.string().min(1),
  run_subject_id: z.string().min(1),
  analysis_id: z.string().min(1).nullable().optional(),
}).strict();

const Sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);

export const AssistantEvidenceAuthorizationSchema = z.object({
  decision_id: z.string().regex(/^evidence_auth_[0-9a-f]{24}$/),
  project_id_sha256: Sha256Schema,
  analysis_id_sha256: Sha256Schema,
  model_revision_id_sha256: Sha256Schema,
  destination: z.enum(["WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"]),
  external_project_id_sha256: Sha256Schema,
  scope_evidence_sha256: Sha256Schema,
  snapshot_bundle_sha256: Sha256Schema,
  current_authorizations_sha256: Sha256Schema,
  selected_evidence_sha256: Sha256Schema,
  authorized_asset_count: z.number().int().nonnegative(),
  generalized_derivative_count: z.number().int().nonnegative(),
  context_schema_version: z.literal("ecocorridor-assistant-context/v3"),
}).strict();

export const AssistantAnswerSchema = z.object({
  answer: z.object({
    answer_id: z.string().min(1),
    call_id: z.string().min(1),
    project_id: z.string().min(1),
    text: z.string().min(1),
    evidence_ids: z.array(z.string().min(1)).min(1),
    numeric_claims: z.array(NumericEvidenceReferenceSchema),
    limitations: z.array(z.string().min(1)).min(1),
    provider_call_id: z.string().min(1),
    provider_model: z.string().min(1),
    trace_id: z.string().min(1),
    trace_url: z.string().url().nullable().optional(),
    association: AssistantRunAssociationSchema,
    evidence_authorization: AssistantEvidenceAuthorizationSchema,
    deterministic_validation: z.literal("PASSED"),
    validator_version: z.literal("ecocorridor-assistant-validator-v1"),
    trace_readback_verified: z.literal(true),
    read_only: z.literal(true),
  }).strict(),
}).strict();

export type AssistantAnswer = z.infer<typeof AssistantAnswerSchema>["answer"];

const SourceSpanSchema = z.object({
  document_id: z.string().min(1),
  page_number: z.number().int().positive(),
  start: z.number().int().nonnegative(),
  end: z.number().int().positive(),
  quote: z.string().min(1),
}).strict().refine((value) => value.end > value.start, "Source span must be ordered.");

const IntakeFieldSchema = z.object({
  field_id: z.string().min(1),
  field_name: z.enum(["purpose", "constraint", "safeguard", "cost_limit", "assumption"]),
  value: z.string().min(1),
  citations: z.array(SourceSpanSchema).min(1),
  confirmed: z.boolean(),
  confirmed_by: z.string().min(1).nullable().optional(),
  confirmed_at: z.string().min(1).nullable().optional(),
}).strict();

export const IntakeDraftSchema = z.object({
  draft_id: z.string().min(1),
  call_id: z.string().min(1),
  project_id: z.string().min(1),
  fields: z.array(IntakeFieldSchema).min(1),
  limitations: z.array(z.string().min(1)).min(1),
  provider_call_id: z.string().min(1),
  provider_model: z.string().min(1),
  trace_id: z.string().min(1),
  trace_url: z.string().url().nullable().optional(),
  association: AssistantRunAssociationSchema,
  deterministic_validation: z.literal("PASSED"),
  validator_version: z.literal("ecocorridor-intake-validator-v1"),
  trace_readback_verified: z.literal(true),
  requires_human_confirmation: z.literal(true),
  applied_to_model: z.literal(false),
}).strict();

export type IntakeDraft = z.infer<typeof IntakeDraftSchema>;

export const IntakeExtractResponseSchema = z.object({
  draft: IntakeDraftSchema,
  ready_for_model: z.literal(false),
  human_confirmation_required: z.literal(true),
}).strict();

export const IntakeConfirmResponseSchema = z.object({
  draft: IntakeDraftSchema,
  ready_for_model: z.boolean(),
  confirmed_fields: z.array(z.record(z.string(), z.unknown())).nullable(),
  applied_to_model: z.literal(false),
}).strict();

export const AssistantTraceResponseSchema = z.object({
  call_id: z.string().min(1),
  trace_id: z.string().min(1),
  association: AssistantRunAssociationSchema,
}).strict();

export const AssistantFeedbackResponseSchema = z.object({
  recorded: z.literal(true),
  receipt: z.record(z.string(), z.unknown()),
}).strict();

export function createAssistantCallId(kind: "evidence" | "intake"): string {
  const uuid = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${kind}-${uuid}`.replace(/[^A-Za-z0-9._:-]/g, "-").slice(0, 128);
}

export function validProviderConfiguration(baseUrl: string, model: string): boolean {
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$/.test(model)) return false;
  try {
    const parsed = new URL(baseUrl);
    const loopback = ["127.0.0.1", "localhost", "::1", "[::1]"].includes(parsed.hostname);
    return !parsed.username
      && !parsed.password
      && !parsed.search
      && !parsed.hash
      && (parsed.protocol === "https:" || (parsed.protocol === "http:" && loopback))
      && !parsed.pathname.replace(/\/$/, "").endsWith("/chat/completions");
  } catch {
    return false;
  }
}
