import { describe, expect, it } from "vitest";

import type { RidgewayFixture } from "../types";
import { fixtureIsManifestVerified } from "./ridgeway";
import { publicRidgewayFixture } from "./publicRidgeway";
import { PUBLIC_RIDGEWAY_EVIDENCE_MANIFEST_SHA256, PUBLIC_RIDGEWAY_RELEASE_TRUSTED } from "./trustedManifests";

describe("independent Ridgeway release trust", () => {
  it("never accepts a projection merely because it declares its own digest", () => {
    const independentlyPinnedDigest = "a".repeat(64);
    const releaseCandidate: RidgewayFixture = {
      ...publicRidgewayFixture,
      releaseReplayEligible: true,
      evidenceState: "VERIFIED_DIRECT_EVIDENCE",
      manifestSha256: independentlyPinnedDigest,
    };
    expect(fixtureIsManifestVerified(releaseCandidate, independentlyPinnedDigest)).toBe(true);

    const selfDeclaredMutation = { ...releaseCandidate, manifestSha256: "f".repeat(64) };
    expect(fixtureIsManifestVerified(selfDeclaredMutation, selfDeclaredMutation.manifestSha256)).toBe(true);
    expect(fixtureIsManifestVerified(selfDeclaredMutation, independentlyPinnedDigest)).toBe(false);
    expect(PUBLIC_RIDGEWAY_RELEASE_TRUSTED).toBe(true);
    expect(PUBLIC_RIDGEWAY_EVIDENCE_MANIFEST_SHA256).toBe(publicRidgewayFixture.manifestSha256);
    expect(fixtureIsManifestVerified(publicRidgewayFixture, PUBLIC_RIDGEWAY_EVIDENCE_MANIFEST_SHA256)).toBe(true);
  });
});
