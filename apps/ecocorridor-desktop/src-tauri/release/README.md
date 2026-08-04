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

The signing script accepts only a raw 32-byte Ed25519 seed encoded as exactly
64 lowercase hexadecimal characters followed by one line feed. It rejects PEM,
base64, uppercase hex, expanded 64-byte private keys, CRLF, and missing or extra
whitespace. The protected GitHub secret
`ECOCORRIDOR_RESOURCE_SIGNING_SEED_HEX` contains the 64 lowercase hexadecimal
characters without a trailing newline; the release workflow validates it,
materializes the canonical newline-terminated file under `RUNNER_TEMP` with a
restrictive umask, and unsets the environment value before invoking the signer.
On POSIX, the signer also rejects any seed file with group or world permission
bits. The seed is never printed, copied into resources, or persisted as release
evidence.

Signing and Python-side verification use pinned PyNaCl 1.6.2. Compatibility at
the native boundary is unchanged: `release-manifest.sig` remains a raw 64-byte
Ed25519 detached signature, and the compile-time public key remains the raw
32-byte Ed25519 public key encoded as 64 lowercase hexadecimal characters. The
Rust host continues to verify those bytes with `ed25519-dalek`.

This is a trust-anchor migration, not a change in how an old PEM secret is
stored. The former PEM secret is intentionally rejected and must not be copied,
converted in CI, or reused. Rotation requires generating a new 32-byte seed
through the approved offline secret process, replacing the protected hex
secret, rebuilding every target from one commit, and reviewing the newly
derived public-key trust anchor before publication. The Tauri updater signing
key remains a separate credential and follows its own rotation procedure.

Development and unit-test builds do not create an inert sidecar. The native
host reports `RESOURCE_MANIFEST_UNAVAILABLE` until real, signed, hash-listed
resources exist.
