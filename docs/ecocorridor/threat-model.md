# Threat model

## Protected assets

- W&B and model-provider credentials;
- raw ecological locations and licensed layers;
- model revisions, resistance and focal rasters, intervention effects;
- direct-run artifacts and evidence identities;
- signed packaged worker and Circuitscape resources.

## Trust boundaries and controls

- **Renderer → native host:** strict RPC method allowlist and byte limits; the
  renderer never receives credentials.
- **Native host → worker:** JSONL over standard streams, redacted initialization,
  verified executable hash, environment allowlist, no shell.
- **Worker → Circuitscape:** isolated directory, normalized paths, fixed resource
  limits, pinned engine manifest, no network. Restart recovery is fail-closed:
  only a one-use, attempt-bound receipt written after confirmed whole-tree exit
  may requeue a `RUNNING` job. On POSIX, confirmation means the isolated process
  group is absent after natural exit or bounded termination; child exit, PID
  disappearance alone, or a sent signal is not proof that descendants stopped.
  Hard-crash or mismatched state requires a local orphan-process audit and
  performs no automatic mutation. Windows release evidence remains blocked
  until the launcher owns a native Job Object from process creation and confirms
  active-process zero; `taskkill` and direct-child wait are not substitutes.
- **Archives and spatial assets:** path containment, symlink/device rejection,
  decompressed limits, explicit grid transforms, provenance and authorization.
  A portable bundle selection is opened once as a regular file and streamed
  into an anonymous, size-bounded snapshot while its raw SHA-256 is computed.
  Member inspection, manifest parsing, and secure extraction all consume that
  same staged inode, so replacing or editing the selected path cannot combine
  identities from different archives. A separate compressed-byte cap bounds
  temporary staging before decompressed member and file-count limits apply. A
  no-follow materialized-tree walk then requires exactly the indexed regular
  files and their necessary parent directories; any extra file, directory,
  symbolic link, or special node blocks database and outbox publication.
  User-selected source assets are opened as one regular-file identity and
  streamed into a same-directory staging inode while their exact byte count and
  SHA-256 are recomputed. Modeled rasters are inspected from those staged bytes,
  not by reopening the mutable selection. Publication uses a no-clobber link;
  an existing destination is reusable only when its opened bytes exactly match.
  The semantic revision and telemetry outbox remain one SQLite transaction. If
  that transaction fails, the exact verified file may remain as an idempotent
  retry target, but no asset, revision, evidence claim, or outbox row is
  published. The final local-file link and total retained-input check execute
  under that transaction's `BEGIN IMMEDIATE` writer lock; imported assets and
  provider-CAS orphans therefore cannot be omitted by a concurrent local add,
  and a stable asset ID cannot be rebound to changed evidence. Explicit-consent
  provider downloads likewise use shared
  provider-and-SHA paths. A failed publisher never unlinks those exact bytes,
  because another concurrent asset may already have reused and published the
  same content; reference-aware garbage collection is a separate operation.
  Network bytes remain in a private staging file until a `BEGIN IMMEDIATE`
  publication guard accounts for every physical provider-CAS blob, including
  unpublished orphans, and every other retained input. The guard serializes
  the final no-clobber CAS link with the asset, provider receipt, semantic
  revision, and outbox transaction. Changed bytes cannot reuse an immutable
  asset ID, and a concurrent second blob cannot cross the reviewed input cap.
  A cross-process project lock permits only one network staging owner. The lock
  identity and staging directory must be regular, current-user-owned, and
  private; symlinks and special entries fail closed. OS lock release provides
  crash recovery, after which the next owner removes only a bounded set of
  recognized staging files, never provider CAS paths.
- **Worker → W&B/Weave:** producer-atomic outbox, deterministic idempotency,
  sensitive-key and local-path redaction, fail-closed destination policy.
- **Assistant:** read-only evidence authorization, minimized context, mandatory
  nested Weave trace, deterministic numeric/unit/evidence/limitation validator.
  Renderer call IDs remain bounded local keys; cloud traces receive only a
  deterministic opaque alias. Length-preserving page redaction keeps exact
  intake citations anchored to the retained source text.
- **Protected producer → Pages/release:** consumers resolve the exact successful
  run attempt, workflow and head SHA, immutable artifact ID, embedded identity,
  and server-reported digest before accepting evidence. A local or copied
  package cannot self-attest into either publication path.

Prompt injection, an unavailable trace, fabricated evidence ID, sensitive
location disclosure, or a request to alter/approve a model returns no assistant
answer. Deterministic Evidence Rail navigation remains available.
