# Release resources

This directory is intentionally source-only. A release job stages the packaged
`ecocorridor-worker`, PackageCompiler Circuitscape application, Ridgeway
fixture, notices, and `release-manifest.json` here before invoking Tauri with
`tauri.release.conf.json`.

`release-manifest.json` is signed with the release Ed25519 key and accompanied
by `release-manifest.sig`. The corresponding raw public key is injected at
compile time through `ECOCORRIDOR_RELEASE_PUBLIC_KEY_HEX`; it is never inferred
from the bundle being verified. Worker startup fails closed if the signature,
resource hashes, executable declarations, or application identifier differ.

Development and unit-test builds do not create an inert sidecar. The native
host reports `RESOURCE_MANIFEST_UNAVAILABLE` until real, signed, hash-listed
resources exist.
