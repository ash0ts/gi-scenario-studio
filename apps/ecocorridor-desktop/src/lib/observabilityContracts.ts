import { z } from "zod";

export const Sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);

const CloudDeploymentSchema = z.enum(["WANDB_CLOUD", "DEDICATED", "SELF_MANAGED", "LOCAL_ONLY"]);
const ProfessionalCloudDeploymentSchema = z.enum(["WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"]);
const ApprovalStatusSchema = z.enum(["NOT_REQUIRED", "REQUIRED", "APPROVED", "STALE", "UNAVAILABLE"]);

const DetailedCloudUploadPreviewFields = {
  destination: CloudDeploymentSchema,
  entity: z.string().min(1),
  project: z.string().min(1),
  external_project_id: z.string().min(1),
  projections: z.array(z.object({
    source_asset_id: z.string().min(1),
    uploaded_asset_id: z.string().min(1),
    representation: z.string().min(1),
    bytes: z.number().int().nonnegative(),
  }).strict()),
  input_bytes: z.number().int().nonnegative(),
  estimated_current_raster_bytes: z.number().int().nonnegative().nullable(),
  telemetry_projected_bytes: z.number().int().nonnegative(),
  telemetry_artifact_count: z.number().int().nonnegative(),
  approval_audit_reservation_bytes: z.number().int().nonnegative(),
  approval_audit_reservation_count: z.number().int().nonnegative(),
  operational_metadata_reservation_bytes: z.number().int().nonnegative(),
  operational_metadata_reservation_count: z.number().int().nonnegative(),
  estimated_total_bytes: z.number().int().nonnegative().nullable(),
  current_raster_estimate_status: z.enum(["NOT_REQUIRED", "PRE_RUN_ESTIMATE", "ACTUAL_RECONCILED", "PARTIALLY_RECONCILED", "UNAVAILABLE"]),
  actual_retained_current_raster_bytes: z.number().int().nonnegative(),
  pending_estimated_current_raster_bytes: z.number().int().nonnegative().nullable(),
  expected_current_raster_count: z.number().int().nonnegative().nullable(),
  retained_current_raster_count: z.number().int().nonnegative(),
  current_raster_estimate_basis: z.string().min(1),
  model_revision_id: z.string().min(1),
  model_sha256: Sha256Schema,
  telemetry_inventory_sha256: Sha256Schema,
  sync_plan_sha256: Sha256Schema,
  approval_status: ApprovalStatusSchema,
  approval_id: z.string().min(1).nullable(),
  blockers: z.array(z.string()),
  warnings: z.array(z.string()),
  allowed: z.boolean(),
  message: z.string().min(1).nullable().optional(),
  technical_type: z.string().min(1).nullable().optional(),
};

export const DetailedCloudUploadPreviewSchema = z.object(DetailedCloudUploadPreviewFields).strict().superRefine((preview, context) => {
  if (preview.approval_status === "UNAVAILABLE") {
    if (preview.allowed || preview.blockers.length === 0) {
      context.addIssue({ code: "custom", message: "An unavailable upload plan must be blocked with a finding." });
    }
    if (preview.estimated_current_raster_bytes !== null || preview.estimated_total_bytes !== null
      || preview.current_raster_estimate_status !== "UNAVAILABLE"
      || preview.pending_estimated_current_raster_bytes !== null || preview.approval_id !== null) {
      context.addIssue({ code: "custom", message: "A detailed unavailable plan must preserve its unknown estimate." });
    }
  } else if (preview.estimated_current_raster_bytes === null || preview.estimated_total_bytes === null
    || preview.pending_estimated_current_raster_bytes === null) {
    context.addIssue({ code: "custom", message: "A detailed actionable upload plan requires complete exact byte estimates." });
  }
  if (preview.approval_status === "APPROVED" && !preview.approval_id) {
    context.addIssue({ code: "custom", message: "An approved upload plan requires approval identity." });
  }
  if (preview.allowed && !["APPROVED", "NOT_REQUIRED"].includes(preview.approval_status)) {
    context.addIssue({ code: "custom", message: "Only an approved or approval-free exact upload plan can be allowed." });
  }
  if (preview.allowed && preview.blockers.length > 0) {
    context.addIssue({ code: "custom", message: "An allowed upload plan cannot retain blocking findings." });
  }
  if (preview.approval_audit_reservation_bytes !== preview.approval_audit_reservation_count * 16_384) {
    context.addIssue({ code: "custom", message: "Approval-audit reservation does not match its versioned bound." });
  }
  if (preview.operational_metadata_reservation_bytes !== preview.operational_metadata_reservation_count * 65_536) {
    context.addIssue({ code: "custom", message: "Operational-metadata reservation does not match its versioned bound." });
  }
  if (preview.approval_audit_reservation_bytes + preview.operational_metadata_reservation_bytes > preview.telemetry_projected_bytes) {
    context.addIssue({ code: "custom", message: "Telemetry reservations exceed projected telemetry bytes." });
  }
  if (preview.estimated_total_bytes !== null && preview.pending_estimated_current_raster_bytes !== null
    && preview.estimated_total_bytes !== preview.input_bytes + preview.telemetry_projected_bytes + preview.pending_estimated_current_raster_bytes) {
    context.addIssue({ code: "custom", message: "The deduplicated upload total does not match its exact byte inventory." });
  }
});

const UnavailableCloudUploadPreviewSchema = z.object({
  allowed: z.literal(false),
  blockers: z.array(z.string()).min(1),
  warnings: z.array(z.string()),
  estimated_current_raster_bytes: z.null(),
  estimated_total_bytes: z.null(),
  current_raster_estimate_status: z.literal("UNAVAILABLE"),
  actual_retained_current_raster_bytes: z.number().int().nonnegative(),
  pending_estimated_current_raster_bytes: z.null(),
  expected_current_raster_count: z.null(),
  retained_current_raster_count: z.number().int().nonnegative(),
  current_raster_estimate_basis: z.string().min(1),
  sync_plan_sha256: z.null(),
  approval_status: z.literal("UNAVAILABLE"),
  approval_id: z.null(),
  message: z.string().min(1),
  technical_type: z.string().min(1),
}).strict();

export const CloudUploadPreviewSchema = z.union([
  DetailedCloudUploadPreviewSchema,
  UnavailableCloudUploadPreviewSchema,
]);

const ProjectScopeEvidenceSchema = z.object({
  entity: z.string().min(1),
  project: z.string().min(1),
  external_project_id: z.string().min(1),
  deployment: ProfessionalCloudDeploymentSchema,
  api_base_url: z.string().url(),
  scope: z.literal("RESTRICTED"),
  evidence_kind: z.enum(["PUBLIC_API_DOCUMENTED_EXACT", "ADMIN_API_DOCUMENTED_EXACT"]),
  provider_field: z.string().regex(/^\S+$/),
}).strict();

export const ObservabilityBindingSchema = z.object({
  state: z.enum(["CONNECTED", "SYNCING", "SYNC_QUEUED_OFFLINE", "FAILED_RETRYING", "AUTHENTICATION_REQUIRED", "BLOCKED", "BLOCKED_PROJECT_POLICY"]),
  binding_id: z.string().min(1),
  entity: z.string().min(1),
  project: z.string().min(1),
  deployment: ProfessionalCloudDeploymentSchema,
  api_base_url: z.string().url(),
  web_base_url: z.string().url(),
  profile_namespace: z.literal("ecocorridor"),
  scope_evidence: ProjectScopeEvidenceSchema,
  verified: z.literal(true),
  external_project_id: z.string().min(1),
  estimated_current_raster_bytes: z.number().int().nonnegative().nullable().optional(),
  last_sync: z.record(z.string(), z.unknown()).nullable().optional(),
}).strict().superRefine((binding, context) => {
  const scope = binding.scope_evidence;
  if (binding.entity !== scope.entity || binding.project !== scope.project
    || binding.deployment !== scope.deployment || binding.api_base_url !== scope.api_base_url
    || binding.external_project_id !== scope.external_project_id) {
    context.addIssue({ code: "custom", message: "The binding identity differs from exact Restricted-scope evidence." });
  }
});

export const ObservabilityConnectResponseSchema = z.object({
  binding: ObservabilityBindingSchema,
  upload_preview: CloudUploadPreviewSchema,
}).strict().superRefine((response, context) => {
  const preview = response.upload_preview;
  if (!("destination" in preview)) return;
  if (preview.entity !== response.binding.entity || preview.project !== response.binding.project
    || preview.external_project_id !== response.binding.external_project_id
    || preview.destination !== response.binding.deployment) {
    context.addIssue({ code: "custom", message: "The destination-bearing upload plan belongs to another W&B destination." });
  }
});

const OutboxCountSchema = z.number().int().nonnegative();
const OutboxStateCountsSchema = z.object({
  PENDING: OutboxCountSchema.optional(),
  UPLOADING: OutboxCountSchema.optional(),
  SYNCED: OutboxCountSchema.optional(),
  FAILED: OutboxCountSchema.optional(),
  BLOCKED: OutboxCountSchema.optional(),
}).strict();

const ObservabilitySyncReceiptSchema = z.object({
  online: z.boolean(),
  claimed: z.number().int().nonnegative(),
  synced: z.number().int().nonnegative(),
  failed: z.number().int().nonnegative(),
  blocked: z.number().int().nonnegative(),
  queued_offline: z.boolean(),
  remaining: z.number().int().nonnegative(),
  state_counts: OutboxStateCountsSchema,
  sync_state: z.enum(["CONNECTED", "SYNCING", "SYNC_QUEUED_OFFLINE", "FAILED_RETRYING", "AUTHENTICATION_REQUIRED", "BLOCKED"]),
}).strict().superRefine((receipt, context) => {
  if (receipt.synced + receipt.failed + receipt.blocked > receipt.claimed) {
    context.addIssue({ code: "custom", message: "Synchronization terminal results cannot exceed claimed outbox rows." });
  }
  if (receipt.queued_offline !== !receipt.online) {
    context.addIssue({ code: "custom", message: "Queued-offline disposition must be the inverse of online execution." });
  }
  if (!receipt.online && (receipt.claimed !== 0 || receipt.synced !== 0 || receipt.failed !== 0 || receipt.blocked !== 0)) {
    context.addIssue({ code: "custom", message: "Offline synchronization cannot claim or finish outbox rows." });
  }
  const counts = {
    PENDING: receipt.state_counts.PENDING ?? 0,
    UPLOADING: receipt.state_counts.UPLOADING ?? 0,
    SYNCED: receipt.state_counts.SYNCED ?? 0,
    FAILED: receipt.state_counts.FAILED ?? 0,
    BLOCKED: receipt.state_counts.BLOCKED ?? 0,
  };
  const remaining = Object.entries(receipt.state_counts)
    .filter(([state]) => state !== "SYNCED")
    .reduce((total, [, count]) => total + count, 0);
  if (receipt.remaining !== remaining) {
    context.addIssue({ code: "custom", message: "The synchronization receipt remaining count does not match its committed outbox-state counts." });
  }
  if (receipt.synced > counts.SYNCED) {
    context.addIssue({ code: "custom", message: "The synchronization receipt synced count exceeds authoritative synced rows." });
  }
  if (receipt.failed > counts.FAILED || receipt.blocked > counts.BLOCKED) {
    context.addIssue({ code: "custom", message: "The synchronization receipt failures exceed authoritative terminal state counts." });
  }
  const expectedState = counts.BLOCKED > 0
    ? "BLOCKED"
    : counts.FAILED > 0
      ? "FAILED_RETRYING"
      : counts.UPLOADING > 0
        ? "SYNCING"
        : counts.PENDING > 0
          ? "SYNC_QUEUED_OFFLINE"
          : "CONNECTED";
  if (receipt.sync_state !== expectedState) {
    context.addIssue({ code: "custom", message: "The synchronization receipt state differs from authoritative outbox-state counts." });
  }
});

export const ObservabilitySyncResponseSchema = z.object({
  binding: ObservabilityBindingSchema,
  receipt: ObservabilitySyncReceiptSchema,
}).strict().superRefine((response, context) => {
  if (response.binding.state !== response.receipt.sync_state) {
    context.addIssue({ code: "custom", message: "The synchronization binding state does not match the derived receipt state." });
  }
});
