# Data and model governance

GI Scenario Studio handles engineering models that may contain sensitive asset,
drainage, rainfall, cost, and location information. Cloud observability is an
optional collaboration capability; it does not transfer engineering authority
to W&B.

> The local project SQLite database and content-addressed artifact store are the
> authoritative engineering record. W&B Models and Weave are a user-controlled
> collaboration, lineage, and observability mirror.

Source-MVP status: automatic cloud projection currently covers imported
bundles, an open-time guided snapshot, completed analyses, and worker-created
exports. The broader policy
below is the release contract; conceptual GIS/data-asset persistence and cloud
projection are not complete yet.

## Optional W&B project binding

A production project may remain local and enter Home without W&B. Cloud sync
and the Evidence Assistant stay disabled until the project is bound to a W&B
project with visibility verified exactly as `Restricted`. Restricted access
limits a project to explicitly invited team members; a public or merely
discoverable project is not an acceptable destination. See W&B's
[restricted-project guidance](https://docs.wandb.ai/guides/hosting/iam/access-management/restricted-projects/).

W&B SDK `0.28.1` does not expose that exact scope through its documented Public
API. The source MVP therefore fails closed before probes or uploads; it never
maps the legacy `PRIVATE` access value to `Restricted`.

The binding flow must establish:

- the deployment: W&B Cloud, Dedicated Cloud, or Self-Managed;
- the API base URL, entity or team, W&B project, and corresponding Weave
  project;
- that the signed-in principal can use both W&B Models and Weave;
- that project access is exactly `Restricted`;
- the project data classification and permitted deployment; and
- explicit acknowledgement of the categories synchronized to W&B.

Each GI project binds to a separate W&B project by default. The suggested name
is `gi-scenario-<project-slug>-<short-project-hash>`. After the first successful
artifact upload, the binding is immutable. Changing the destination creates a
new local `ModelRevision` with a lineage link to the prior binding; it never
silently moves or aliases evidence.

Projects classified as critical-infrastructure restricted may use only W&B
Dedicated Cloud or Self-Managed W&B. W&B Cloud is blocked for that
classification even if the target project is Restricted.

## What is synchronized

The binding flow requires acknowledgement of full engineering-evidence
synchronization; it is not a reduced-telemetry option. Subject to project
policy, the mirror includes:

- source SWMM bundles and referenced dependencies;
- user-provided and approved open GIS or data assets;
- normalized and materialized SWMM models;
- storm inputs and boundary assumptions;
- candidate spaces, immutable LID controls, feasibility records, and the
  versioned cost basis;
- INP, RPT, and OUT files from exact solver jobs;
- extracted metrics, QA findings, adverse-change scans, and search traces;
- decision briefs, Evidence Rail claim records, and evidence exports;
- guided and benchmark fixtures; and
- selected assistant evidence context, Weave traces, evaluations, and user
  feedback.

Before binding, the interface previews these categories and the deployment's
storage and deletion behavior. W&B notes that cloud artifacts are encrypted and
that deleted artifacts may remain recoverable for a retention period; review
the current [artifact privacy and compliance guidance](https://docs.wandb.ai/models/artifacts/data-privacy-and-compliance/)
before accepting the policy.

Choosing local-only mode or disconnecting prevents new W&B and Weave activity.
It does not delete evidence that the user previously uploaded; retention and
deletion remain governed by the selected W&B deployment.

## What is never project evidence

The following are prohibited from W&B Runs, Tables, Artifacts, Weave traces,
exports, diagnostics, and application logs:

- API keys, model-provider keys, passwords, authorization headers, cookies, or
  session tokens;
- environment-variable names paired with values or full environment dumps;
- local absolute paths, operating-system usernames, home-directory names, or
  internal credential identifiers;
- raw application console logs;
- crash dumps or diagnostic content that has not passed redaction; and
- executable plugins or downloaded code.

Per-file cloud manifests use logical names, content hashes, byte counts, media
types, and local object IDs. They do not use a local source path as identity.
Credentials must be treated as passwords and must never be committed or
hard-coded, consistent with the
[W&B API-key guidance](https://docs.wandb.ai/models/track/public-api-guide).

## Credential boundary

The desktop host owns credential entry and persistence. The renderer can ask
the host to open a masked native prompt, but cannot submit or read a plaintext
key.
The host stores the W&B key in macOS Keychain or Windows Credential Manager,
resolves it only for an authorized connection request, and transfers it to the
Python worker in memory. It is never a process argument, inherited worker
environment variable, SQLite column, artifact, diagnostic field, or export
value. Temporary secret buffers are cleared after use. The prompt command
returns only whether the operating-system keychain accepted the credential.

Provider-model credentials use a separate keychain entry and are never reused
as W&B credentials. Browser development previews cannot authenticate and must
not imply that a Restricted binding was verified.

Repository setup documents variable names only. `.env.example` contains empty
placeholders; a real `.env` file is ignored. Local and CI checks must report
only connection state and non-secret project identifiers.

## Local authority and the durable outbox

The production contract commits an engineering state change and its
telemetry-outbox record in one SQLite transaction. The source MVP's outbox is
transactional and idempotent, but its bundle, guided, completed-analysis, and
export producers insert the envelope immediately after the authoritative state
commit. Atomic coupling or startup reconciliation remains a release gate. The
target sequence is:

1. Persist project, job, QA, metric, decision, or export state locally.
2. Persist a deterministic `TelemetryEnvelope` and idempotency key in the same
   transaction.
3. Resolve and hash every referenced local artifact.
4. Upload at a deterministic batch barrier.
5. Verify the cloud artifact digest or downloaded bytes.
6. Store the W&B run, artifact version, and Weave trace references locally only
   after confirmation.

W&B failure cannot alter solver inputs, candidate order, exact output, QA,
costs, frontier membership, the decision board, or an export. Retries reuse the
same idempotency key and W&B run identity. The outbox records pending, uploading,
failed, blocked, and synchronized states without duplicating immutable evidence.

After one successful online binding, hydraulic work may continue offline. The
application queues evidence locally and can stage W&B runs in offline mode;
synchronization resumes when credentials and network access return. Initial
authentication, a new binding, and assistant calls still require connectivity.
See W&B's [offline-mode environment controls](https://docs.wandb.ai/models/track/environment-variables)
and [`wandb sync`](https://docs.wandb.ai/models/ref/cli/wandb-sync/).

## W&B lineage and retention

The expected artifact graph is:

```text
source bundle + GIS/data assets
  -> normalized model
  -> storm set + candidate space + cost basis
  -> baseline artifacts
  -> exact run batches + search-model artifacts
  -> decision evidence
  -> evidence package
```

`use_artifact()` establishes input lineage and `log_artifact()` establishes
output lineage. Exact-run artifacts are chunked at 128 jobs or 512 MiB,
whichever comes first, while the cloud manifest retains a digest for each file.
See W&B's [artifact-lineage documentation](https://docs.wandb.ai/models/artifacts/explore-and-traverse-an-artifact-graph).

Deleting a local project must not silently delete its cloud mirror. Deleting or
retaining either copy is a separate, explicit operation governed by the
organization's record-retention policy. Rebinding creates lineage; it does not
rewrite prior W&B history.

## Weave explanation governance

The current source MVP creates a deterministic, read-only evidence explanation
and acknowledges it with one Weave call. It cannot mutate a model, accept QA,
attest fitness, change search bounds, select a preferred alternative, or invent
an engineering value. It does not call a language-model provider.

Every explanation request must create a confirmed Weave call in the same W&B
project. The planned provider-backed assistant must additionally nest
authorization, local evidence resolution, context construction, provider call,
numeric-claim extraction, evidence validation, limitation validation, response
formatting, and feedback. Until that exists, the UI offers only deterministic
guided copy. If Weave is unavailable, the endpoint is disabled; the Evidence
Rail remains available. An untraced fallback answer is prohibited.

Selected evidence context may contain project data because full synchronization
was explicitly accepted. Before any trace leaves the worker, Python-side PII and
sensitive-key redaction removes credentials, authorization material, internal
credential IDs, and local paths. W&B documents this pre-send behavior in the
[Weave redaction guide](https://docs.wandb.ai/weave/guides/tracking/redact-pii).

## Source data and licensing

The production contract requires every model and data asset to record its
source, license, checksum, vintage, modification notice, and model influence.
Imported-bundle files satisfy the implemented subset; conceptual GIS assets do
not yet have this complete persisted record. Private status never waives
provenance, and public visibility is not permission to redistribute an asset.

The target concept workflow requires users to approve each provider request
after seeing its dataset, vintage, resolution, license, attribution, extent,
and intended parameter use. Provider responses must become passive,
content-addressed assets reusable offline. This persistence and approval flow is
not complete in the source MVP; downloaded code or executable plugins remain
unsupported.

Only explicitly redistributable assets can enter public guided or benchmark
collections. A paper's access license does not automatically license associated
model files.

## Diagnostics and support bundles

Diagnostics are local and redacted by default. The user separately chooses
whether a support bundle includes model files, coordinates, project names, or
source data. API keys, authorization material, environment dumps, absolute
paths, and unredacted console logs are never selectable.

## Model Fitness Passport and claim governance

Project kind, model fitness, numerical QA, and cloud synchronization are
independent facts:

- `demo`, `conceptual`, or `existing` records the project's origin and does not
  confer fitness;
- accepted continuity QA says a solver run met a declared numerical policy;
- a Model Fitness Passport records one revision's intended use, disposition,
  criterion evidence, permitted outputs, prohibited uses, and unresolved
  limitations; and
- W&B synchronization says the local evidence has a verified cloud mirror.

None implies calibration, real-world truth, regulatory acceptance, site
feasibility, or construction suitability. Passport dispositions are not a
maturity ladder. Engineer review and professional acceptance remain explicit
human decisions backed by repository evidence.
