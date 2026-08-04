import type { AssetDeployment, ProfessionalAssetRecord } from "../types";

const RAW_SENSITIVE_CLASSES: ReadonlySet<ProfessionalAssetRecord["sensitivity"]> = new Set([
  "SENSITIVE_LOCATION",
  "CRITICAL_INFRASTRUCTURE",
  "TRIBAL_OR_CULTURAL",
]);

const VERIFIED_TEXT_MEDIA_TYPES = new Set(["text/plain", "text/markdown"]);

export type IntakeAssetEligibility = {
  eligible: boolean;
  code:
    | "ELIGIBLE_FOR_WORKER_VERIFICATION"
    | "SELECT_DOCUMENT"
    | "DOCUMENT_REQUIRED"
    | "DESTINATION_REQUIRED"
    | "DEPLOYMENT_UNAUTHORIZED"
    | "CLOUD_PROHIBITED"
    | "GENERALIZED_DERIVATIVE_REQUIRED"
    | "DEDICATED_DESTINATION_REQUIRED"
    | "RAW_SENSITIVE_DOCUMENT_BLOCKED"
    | "VERIFIED_TEXT_REQUIRED";
  title: string;
  detail: string;
};

function blocked(code: IntakeAssetEligibility["code"], title: string, detail: string): IntakeAssetEligibility {
  return { eligible: false, code, title, detail };
}

/**
 * Mirrors the worker's pre-provider document checks using authoritative asset
 * metadata already retained in project state. The worker still rechecks bytes,
 * UTF-8 decoding, page boundaries, current authorization, and destination.
 */
export function evaluateIntakeAssetEligibility(
  asset: ProfessionalAssetRecord | null,
  deployment: AssetDeployment | null,
): IntakeAssetEligibility {
  if (!asset) {
    return blocked("SELECT_DOCUMENT", "Choose a retained supporting document.", "Only an explicitly selected retained document can supply assistant context.");
  }
  if (asset.kind !== "DOCUMENT") {
    return blocked("DOCUMENT_REQUIRED", "This retained asset is not a document.", "Select a retained DOCUMENT asset; maps, rasters, matrices, and vectors cannot supply assistant page text.");
  }
  if (!deployment || deployment === "LOCAL_ONLY") {
    return blocked("DESTINATION_REQUIRED", "A verified provider destination is required.", "Bind this project to an exact-scope Restricted W&B destination before preparing assistant context.");
  }
  if (!asset.approvedDeployments.includes(deployment)) {
    return blocked("DEPLOYMENT_UNAUTHORIZED", `Not authorized for ${deployment}.`, "Have an accountable reviewer authorize an allowed representation for this exact deployment, or select another retained document.");
  }
  if (asset.cloudDisposition === "PROHIBITED") {
    return blocked("CLOUD_PROHIBITED", "Provider and Weave replication are prohibited.", "This document cannot be used by the assistant. Keep it local and select a separately authorized retained representation.");
  }
  if (asset.cloudDisposition === "GENERALIZED_DERIVATIVE_ONLY") {
    return blocked("GENERALIZED_DERIVATIVE_REQUIRED", "Select the approved generalized derivative.", "Raw page context is blocked. Retain and select the reviewed generalized derivative identified by the asset authorization.");
  }
  if (asset.cloudDisposition === "DEDICATED_OR_SELF_MANAGED_ONLY" && deployment !== "DEDICATED" && deployment !== "SELF_MANAGED") {
    return blocked("DEDICATED_DESTINATION_REQUIRED", "A Dedicated or Self-Managed destination is required.", "Move this project to an approved Dedicated or Self-Managed deployment, or select another retained document.");
  }
  if (RAW_SENSITIVE_CLASSES.has(asset.sensitivity)) {
    return blocked("RAW_SENSITIVE_DOCUMENT_BLOCKED", "Generalized derivative required for assistant use.", `${asset.sensitivity} page text cannot enter provider or Weave context in this release. Create, review, retain, and select an approved generalized derivative.`);
  }
  const mediaType = asset.mediaType.trim().toLowerCase();
  if (!VERIFIED_TEXT_MEDIA_TYPES.has(mediaType)) {
    return blocked("VERIFIED_TEXT_REQUIRED", "Verified text representation required.", `${asset.mediaType || "Unknown media type"} cannot supply verifiable page text. Retain and authorize a UTF-8 text/plain or text/markdown derivative with explicit page boundaries.`);
  }
  return {
    eligible: true,
    code: "ELIGIBLE_FOR_WORKER_VERIFICATION",
    title: `Eligible for exact worker verification on ${deployment}.`,
    detail: `Retained ${mediaType} metadata and destination authorization are compatible. Before any provider call, the worker rechecks checksum, UTF-8 decoding, exact page boundaries, and current policy.`,
  };
}
