import ridgewayReleaseTrust from "./ridgewayReleaseTrust.json";

/**
 * Release-reviewed digests live outside the data they authenticate. Keep this
 * module free of projection imports so the professional desktop does not bundle
 * a static fallback outcome.
 */
export const PUBLIC_RIDGEWAY_RELEASE_TRUSTED = ridgewayReleaseTrust.trusted === true;
export const PUBLIC_RIDGEWAY_EVIDENCE_MANIFEST_SHA256: string | null = PUBLIC_RIDGEWAY_RELEASE_TRUSTED
  && typeof ridgewayReleaseTrust.evidenceManifestSha256 === "string"
  ? ridgewayReleaseTrust.evidenceManifestSha256
  : null;
