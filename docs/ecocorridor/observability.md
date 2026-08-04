# EcoCorridor observability boundary

SQLite and project-local content-addressed evidence remain authoritative. W&B
is a collaboration and lineage mirror; a failed upload cannot alter a direct
Circuitscape result, QA disposition, search order, or export content.

## Application profile

`ecocorridor_core.observability.ECOCORRIDOR_APP_PROFILE` is the sole naming
profile for Eco runs, Tables, Artifacts, Registry collections, local SDK
directories, and the native keychain service. It does not reuse GI namespaces.

The profile maps meaningful computations to W&B Runs. Individual direct jobs
belong in typed Tables and batched evidence Artifacts rather than thousands of
separate Runs.

## Durable delivery

Scientific producers commit their result and a `TelemetryProjection` outbox
row atomically. `DurableOutboxConsumer` processes committed rows in order:

1. Resolve the destination's exact project scope before claiming rows.
2. Verify the immutable outbox payload hash.
3. Reject credentials, local absolute paths, and unsupported profile names.
4. Verify each local artifact's byte count and SHA-256 before SDK startup.
5. Resume the deterministic W&B Run and project Tables and lineage. Before an
   output artifact is logged, query its deterministic outbox alias with W&B's
   documented [`artifact_exists()` Public API](https://docs.wandb.ai/models/ref/python/public-api/api#method-apiartifact_exists).
   A retry reuses an exact committed version only after
   its type, idempotency metadata, pinned `:vN` reference, bytes, and SHA-256
   all match; an ambiguous lookup or mismatch fails closed.
6. Download the exact logged or reused artifact version without using the SDK cache and
   verify its byte count and SHA-256.
7. Mark the local outbox row synced only after provider confirmation.

Transient provider failures become retryable outbox state. Integrity,
authorization, and policy failures become blocked state. Offline mode does not
claim rows; W&B's run, cache, artifact, data, and verification directories stay
under `.scenario-cloud/ecocorridor/` in that project.

Typed Tables are emitted only from retained source records. `search_batches`
describes the immutable initial run-plan batch; it does not reconstruct or
claim adaptive batches that were never persisted. `adverse_changes` contains
only failed rule dispositions with a matching direct evidence claim and
successful direct-run IDs. `sync_failures` contains only durable outbox rows
that still carry a stable failure code. If the corresponding local record does
not exist, the Table or row is omitted rather than synthesized.

## Restricted-scope gate

The W&B Public `Project` API does not currently document an authoritative field
for the four project visibility scopes. Access to a project, a legacy
`PRIVATE` value, team membership, successful upload, or a UI checkbox is not
proof that a project is `Restricted`.

`ProjectScopeResolver` therefore accepts only evidence from a reviewed provider
backed by a documented exact-scope field. Multi-tenant W&B Cloud is blocked in
this release. Dedicated or Self-Managed deployments are also blocked until the
specific deployed version offers a documented admin endpoint and its adapter is
implemented and reviewed.

Exact-scope evidence also records the normalized API base URL that produced it.
Entity, project, immutable provider ID, deployment, scope kind, and API endpoint
must all match the persisted binding on every online sync and assistant call;
scope proof from one deployment or endpoint cannot be replayed against another.

See W&B's [Restricted project policy](https://docs.wandb.ai/platform/hosting/iam/access-management/restricted-projects/)
and [Project Public API](https://docs.wandb.ai/models/ref/python/public-api/project).

## Weave answer-release gate

`WeaveNestedTraceSink` records `assistant.request` and each approved child step
in the same trace. The assistant returns no answer until:

- the root call is finished;
- the client flush completes;
- the root call is read back from Weave;
- its call ID, trace ID, completion marker, and ordered step list match locally;
- its provider `wb_run_id` matches the explicit W&B run context set through
  Weave's supported [`set_wandb_run_context`](https://docs.wandb.ai/weave/guides/tools/weave-in-workspaces#associate-traces-with-a-specific-wb-run)
  API;
- its immutable project, run-subject, analysis, run-kind, and association IDs
  match the association persisted with the returned answer or intake draft;
- deterministic evidence, unit, limitation, action-boundary, injection, and
  sensitive-location checks have already passed.

Missing credentials, Weave, provider access, flush acknowledgement, or exact
scope disables assistant answers. The deterministic local Evidence Rail remains
available.

### Provider and credential boundary

The desktop stores the W&B and model-provider credentials as separate keychain
items. The native host injects the model-provider key only into
`assistant.configure`; the strict RPC contract rejects it on explanation,
feedback, trace-readback, and intake-confirmation calls. The Python worker keeps
the key in a zeroable `ProviderCredentialSession`. SQLite, exports, trace
inputs, errors, subprocess arguments, and RPC responses never receive it.

Configuration requires an explicit model identifier and an HTTPS provider base
URL (loopback HTTP is allowed for local deployments). User information,
passwords, query strings, fragments, redirects, and a pre-appended
`/chat/completions` resource are rejected. A configured session is scoped to
one local project and one immutable W&B project identity.

Before every provider call, `BoundProjectAuthorizer` reopens the authoritative
project store and verifies all of the following:

1. the stored binding is online, verified, and names the EcoCorridor profile;
2. the current project ID matches the configured session;
3. the documented exact-scope resolver still returns the same `Restricted`
   project evidence and immutable external project ID; and
4. `WeaveNestedTraceSink` independently repeats the exact-scope check before it
   starts the root trace.

The Evidence Assistant resolves only persisted `ConnectivityEvidenceClaim`
records. Before a Weave trace or provider call can begin, the worker binds the
selection to one immutable analysis snapshot and rechecks every referenced
source against current asset bytes, current authorization, the exact W&B
destination, and any approved generalized derivative. Removed, changed,
revoked, prohibited, deployment-incompatible, or unresolved assets block the
call. A generalized-only scientific source blocks until a reviewed semantic
revision actually uses its derivative.

The provider and trace receive an opaque authorization receipt plus `E1`,
`E2`, … claim aliases, closed-enum claim kinds, exact numeric/Boolean values,
unit tokens, and fixed product limitations. They do not receive the raw user
question, claim prose, project-authored titles, asset names, portfolio names,
scenario names, focal-pair names, or direct-run IDs. The question is classified
locally into a finite intent vocabulary. The returned answer maps validated
aliases back to local evidence IDs only after trace readback; the same
authorization decision ID is retained in the trace and answer.

The Evidence Intake Assistant accepts
selected document pages, sends only this minimized text, rejects document-borne
prompt injection, and requires exact page/character-span citations. Drafts
are persisted as unconfirmed and cannot change project-model state. Explicit
`assistant.intake.confirm` calls create immutable, reviewer-attributed evidence;
they still do not automatically mutate resistance, focal-area, intervention,
or decision-rule records.

For evidence explanations, a renderer cannot set
`exact_location_authorized=true`. EcoCorridor v0.1 has no reviewer-bound,
destination-specific authorization for one assistant call, so the worker
rejects that request and always uses generalized location context. It also
adds sensitive asset IDs and titles from authoritative project records to the
redaction policy; renderer-supplied terms may only make redaction stricter.
Exact document context is considered only by the separate intake path after
the retained bytes, reviewed asset authorization, and selected deployment are
all verified.

Actual practitioner feedback is added to the completed Weave call as bounded
custom feedback, flushed, and read back by provider feedback ID before the RPC
reports success. See W&B's [Weave feedback guide](https://docs.wandb.ai/weave/guides/tracking/feedback).

## Two-stage protected integration gate

Provider evaluation and practitioner feedback are intentionally separate
manual-only workflows. Both use a protected GitHub Environment and neither can
run on ordinary pull requests or pushes. `${{ runner.temp }}` appears only in
step-level environment values. The protected environment supplies separate API
and browser base URLs so a Dedicated or Self-Managed deployment cannot silently
derive user-facing links from an API endpoint.

Stage one, `.github/workflows/ecocorridor-assistant-evaluation.yml`, receives the
W&B and model-provider credentials but no ratings. It performs the exact-scope
transport gate and provider-backed case evaluation, then uploads an immutable
pending report and raw case records. Before upload it embeds a first-party
producer identity containing the repository, run ID, run attempt, exact workflow
path and ref, workflow-file SHA, and checked-out head SHA. The pending report is
explicitly marked
`NOT_COLLECTED` and `release_eligible=false`; its artifact is not a release
candidate. W&B and provider secrets are attached only to the first-party steps
that need them; checkout, tool setup, dependency installation, local tests, and
artifact upload do not inherit those credentials.

Stage one currently exits with
`WANDB_RESTRICTED_SCOPE_UNVERIFIABLE` before importing W&B or Weave because no
supported exact-scope provider exists. This is an intentional external release
blocker, not a passing integration result. Once a documented resolver is added,
the same entry point performs an exact artifact upload/download hash roundtrip
and a Weave nested-trace flush/readback roundtrip with the invited service
account. After that transport gate succeeds, it runs the committed CC0
assistant case set through the configured provider and real Weave traces,
publishes the immutable Weave Dataset, records raw case-level outputs and
deterministic scores, and emits a cryptographic case-record aggregate. Prompt
injection, fabricated evidence, unauthorized action, numeric/unit fidelity,
required limitations, trace readback, and synthetic sensitive-token leakage
are recomputed locally by the release validator.

Only after reviewers have inspected those exact answers can stage two,
`.github/workflows/ecocorridor-observability-integration.yml`, be dispatched
with the successful stage-one Actions run ID and public payload/signature
hashes. The redacted rating envelope and detached signature are supplied through
the protected GitHub Environment as base64 secrets, not persistent dispatch URL
inputs. Stage two resolves the exact workflow path and ID, run attempt, `main`
branch, successful conclusion, exact commit, artifact ID, and server-reported
SHA-256 digest before downloading by immutable artifact ID. The downloaded
producer identity must match that independently resolved REST provenance,
including the run attempt. Stage two embeds its own equivalent producer record
before publishing the finalized artifact. It has no
model-provider credential and never reruns provider or Weave calls.

The protected Environment supplies `ECOCORRIDOR_ASSISTANT_RATINGS_B64` and
`ECOCORRIDOR_ASSISTANT_RATINGS_SIGNATURE_B64` as secrets, plus the approved
public key and key hash as non-secret variables. The signed payloads are small,
redacted release evidence; their public SHA-256 values remain dispatch inputs.
The two secret values are scoped only to the first-party materialization shell
step, so checkout, setup, download, upload, and unrelated validation steps never
inherit them.
The protected release uses the same pattern for
`ECOCORRIDOR_PRACTITIONER_ACCEPTANCE_B64` and its detached signature. This keeps
credential-bearing download locations out of durable Actions metadata.

Usefulness feedback is not accepted as an unsigned aggregate or editable
mapping. The finalizer requires redacted case-level ratings bound to the exact
source commit, dataset hash, provider model, evaluation/W&B identities, and each
case record, answer hash, trace ID, and provider call, plus a detached Ed25519
signature from the protected approved key. Ratings cannot be supplied to stage
one, and ratings for another evaluation cannot satisfy stage two. Rejected
injection and fabricated-evidence cases require a separately persisted,
flushed, and read-back aborted-trace record; exception handling alone never
counts as trace verification. The release job accepts only the successful
same-commit stage-two artifact, independently re-resolves and byte-compares the
stage-one artifact (including its embedded producer identity), verifies the
stage-two producer identity, and revalidates the report, raw cases, signed
ratings, detached signature, public verification key, and three exact Actions
provenance records. It also reruns the deterministic finalizer from the
independently downloaded stage-one pending report and case records, then requires
that output to be byte-identical to stage two's final report. Both copies ship in
the checksummed release evidence. Because the exact-scope resolver and
organizational credentials are external prerequisites, no passing evaluation
evidence is claimed in this source tree.
