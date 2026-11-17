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
and the Evidence Guide stay disabled until the project is bound to a W&B
project with visibility verified exactly as `Restricted`; the Guide also
requires its own inference and trace-content consent. Restricted access
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

Disabling W&B code and Git capture is not sufficient by itself. The pinned SDK
can otherwise create `wandb-metadata.json`, `requirements.txt`, system metrics,
and console files containing host or environment details. Every application and
publication adapter therefore sets `x_disable_meta`,
`x_disable_machine_info`, `x_disable_stats`, and `x_save_requirements=False`,
in addition to disabling code, Git, and console capture. Release publication
enumerates each run's current cloud file inventory and fails if
`wandb-metadata.json`, `requirements.txt`, or `output.log` is present.

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

The source MVP includes a provider-backed, read-only Evidence Guide. Each turn
uses the one- or two-call finite-plan architecture pinned by an unexpired v2
release receipt. A provider call may choose an allowlisted intent, bounded read
tool, or finite explanation-plan ID; it never authors the engineering answer.
The provider receives no write tool and cannot mutate a model, accept QA,
attest fitness, change search bounds, select a preferred alternative, or author
an engineering value. Values, units, citations, limitations, boundary language,
and proposed review actions are materialized and checked locally.

Conversations are multi-turn and evidence-packet-pinned. Each question creates
a new OpenTelemetry-backed Weave Agents turn under the same logical
conversation, with the selected architecture's actual LLM and tool spans plus
deterministic validation. The worker flushes the trace and reads the turn back
by trace ID before it stores or returns an answer. If the packet changes or any
policy, provider, tool, validator, flush, acknowledgement, campaign-expiry, or
circuit-breaker check fails, the turn fails closed. The Evidence Rail remains
available, and an untraced fallback answer is prohibited. See the
[Weave Agents tracing guide](https://docs.wandb.ai/weave/guides/tracking/trace-agents).

Inference and trace consent are explicit and separate from general W&B
synchronization. Engineering projects require trace-content consent; the
Evidence Guide is disabled for critical-infrastructure projects and unless its
online binding is verified as exactly `RESTRICTED`. The raw free-form question
and prior message text remain local. Before transport, the worker maps the
question to an allowlisted intent and safe canonical prompt, builds an immutable
structured-evidence packet, restricts tool arguments to its selected anchor,
recursively excludes prohibited keys and file/path data from tool results,
hashes or omits non-demo identifiers, and audits the complete provider and trace
payloads. Credentials are supplied only in the HTTPS authorization header and
are never part of those payloads or stored policy records.

A deterministic local trace filter removes prohibited-key fields and local
paths and redacts common credential, bearer, email, government-ID, and
labelled-phone patterns before the SDK sees content. Standard-project inference
is disabled unless both inference and trace-content consent are explicit; full
synthetic fixture traces still pass through the same filter. This release uses
the public W&B Serverless Inference origin only for W&B Cloud bindings and does
not forward Dedicated Cloud or Self-Managed credentials to that origin. Weave's sensitive-key
sanitizer remains enabled as defense in depth.

The optional Presidio-backed Weave PII setting is disabled because it can
attempt a runtime language-model download, which is incompatible with the
packaged application's offline and locked-supply-chain boundary. The local
filter is intentionally not described as removing all PII: it does not classify
every piece of domain-specific engineering data and does not sanitize the
separate W&B Inference request. Local normalization, allowlisting, and provider
payload audits are the mandatory pre-transport controls. See the
[Weave redaction guide](https://docs.wandb.ai/weave/guides/tracking/redact-pii)
for the separate SDK feature.

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
