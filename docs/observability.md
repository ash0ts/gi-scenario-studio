# W&B Models and Weave observability

This document is the operational contract for the cloud mirror. It does not
change the engineering authority described in
[the platform architecture](platform-architecture.md): local SQLite state and
content-addressed bytes remain authoritative.

Implementation status: imported bundles, an open-time guided snapshot, completed exact
analyses, and evidence exports have automatic W&B projections. Conceptual GIS
assets, generated models, Registry promotion, Weave Dataset evaluation, and
same-transaction producer/outbox coupling remain contract requirements rather
than source-MVP claims.

The guided projection is currently an open-time checkpoint snapshot; later UI
interaction and browser-download events are not projected. W&B SDK `0.28.1`'s
[documented Project API](https://docs.wandb.ai/models/ref/python/public-api/project)
also does not expose an exact project-visibility scope. Because
`PRIVATE` cannot distinguish Team from Restricted, this implementation fails
closed before probes and uploads until a supported resolver returns the exact
`RESTRICTED` scope.

## Optional project binding

The production desktop opens in `LOCAL_ONLY` mode. A user may open the
connection wizard from Home or an active project when collaboration and cloud
lineage are useful:

1. Select W&B Cloud, Dedicated Cloud, or Self-Managed and the API base URL.
2. Enter a personal W&B API key in the host-owned masked prompt; the host stores
   it directly in the operating-system keychain.
3. Select the W&B entity or team.
4. Select or create one W&B project for this GI project.
5. Verify project access is exactly `Restricted`.
6. Review the full engineering-evidence categories and permanent exclusions.
7. Test W&B Models and Weave access.
8. Confirm and persist an `ObservabilityBinding` without the credential.

Choosing **Work locally** ends the active cloud session and prevents new
uploads and assistant calls. A local project can be bound later; the binding is
recorded against that project and its current model revision rather than only
against an account-level UI state.

The default external project name is:

```text
gi-scenario-<project-slug>-<short-project-hash>
```

The first successful artifact upload freezes the binding. A different
destination requires a new local project revision and a lineage reference to
the old binding.

W&B recommends Restricted projects for sensitive collaboration because access
is limited to explicitly invited members. See the current
[access-management guidance](https://docs.wandb.ai/guides/hosting/iam/access-management/restricted-projects/).

## Connection states

The application exposes one concise state everywhere:

| State | Meaning | Engineering work |
|---|---|---|
| `LOCAL_ONLY` | No active cloud binding or session; evidence remains on this device | Enabled; sync and assistant disabled |
| `CONNECTED` | Binding and credentials are valid; outbox is current | Enabled |
| `SYNCING` | A deterministic outbox batch is uploading | Enabled |
| `SYNC_QUEUED_OFFLINE` | A previously bound project has local work waiting for a connection | Enabled |
| `FAILED_RETRYING` | Upload failed and a durable retry is scheduled | Enabled; cloud links may be stale |
| `AUTHENTICATION_REQUIRED` | Key is missing or expired | Existing local work remains readable; binding actions blocked |
| `BLOCKED_PROJECT_POLICY` | Destination is not Restricted or violates classification policy | Local work enabled; sync and assistant blocked |

A green indicator means only that cloud synchronization is current. It does not
mean the model is fit, QA passed, or an alternative is acceptable.

## Defensive SDK configuration

The worker forces these defaults before initializing either SDK:

```text
WANDB_CONSOLE=off
WANDB_DISABLE_CODE=true
WANDB_DISABLE_GIT=true
WANDB_ERROR_REPORTING=false
WANDB_SHOW_RUN=false
```

`WANDB_DIR`, `WANDB_CACHE_DIR`, and `WANDB_ARTIFACT_DIR` are scoped under
application-managed storage for the current local project. The application
does not capture source code, Git metadata, console output, or arbitrary process
environment. Environment controls and offline behavior are documented by W&B
at [W&B environment variables](https://docs.wandb.ai/models/track/environment-variables).

## Computation-to-Run mapping

One W&B Run represents a meaningful computation, not one SWMM subprocess:

| Job type | Inputs | History | Summary |
|---|---|---|---|
| `bundle_import` | Source bundle and dependency artifacts | Validation stages | Import disposition and issue counts |
| `concept_generation` | Recipe and data assets | Generation/repair stages | Model hash and fitness blockers |
| `baseline` | Model, storm set, QA policy | Ordered solver-batch totals | Baseline QA and exact-job totals |
| `alternatives_analysis` | Baseline, candidate space, goals, costs | Batch metrics and cache use | Frontier size, feasible count, adverse count, disposition |
| `guided_session` | Pinned Cedar Trace fixture | Open-time checkpoint snapshot (current source MVP) | Initial replay disposition |
| `search_benchmark` | Immutable exhaustive dataset | Budget checkpoints | Decision-quality and exact-job-equivalent metrics; resource observations remain a release gate |
| `assistant_evaluation` | Versioned question dataset and prompt | Evaluation cases | Deterministic scorer results |
| `evidence_export` | Selected exact evidence | Export stages | Eligibility, output artifacts, limitations |
| `release_gate` | Release inputs and benchmark versions | Gate execution | Pass/fail with report links |

Runs record input hashes, app/schema/solver/extractor versions, storm and
candidate dimensions, metric definitions, cost basis, tolerances, seed, worker
count, search method, platform, data policy, and local IDs. See
[W&B Runs](https://docs.wandb.ai/models/runs).

## Typed Tables

Individual computations are rows in typed W&B Tables:

```text
solver_jobs
evaluated_alternatives
qa_findings
adverse_changes
evidence_claims
search_batches
energy_observations
guided_checkpoints
sync_failures
```

This avoids 884 or 2,800 tiny Runs while preserving design × storm inspection.
Each row includes stable local IDs and artifact references; no row uses a local
path as identity. See [W&B Tables](https://docs.wandb.ai/models/tables/log_tables).

## Artifact graph

The cloud lineage graph mirrors repository dependencies:

```text
source bundle + GIS/data assets
    -> normalized model
    -> storm set + candidate space + cost basis
    -> baseline-runs
    -> exact-run-batch + search-model + search-trace
    -> decision-evidence
    -> evidence-package
```

Artifact types are:

```text
swmm-project       data-asset          storm-set
candidate-space    cost-basis          baseline-runs
exact-run-batch    search-model        search-trace
decision-evidence  evidence-package    guided-fixture
benchmark-dataset
```

An exact-run batch closes at 128 jobs or 512 MiB, whichever comes first. The
`CloudArtifactManifest` retains the logical name, media type, size, local source
object, and SHA-256 for every member. `use_artifact()` and `log_artifact()`
create the W&B lineage edges; see the
[artifact lineage guide](https://docs.wandb.ai/models/artifacts/explore-and-traverse-an-artifact-graph).

After upload, the worker verifies provider metadata and performs a download/hash
roundtrip when required by the operation. It stores the external URL and version
only after the expected digest is confirmed.

## Registry policy

The intended collections are:

```text
Datasets / gi-guided-fixture
Datasets / gi-search-benchmarks
Models / gi-search-proposer
Release Evidence / gi-scenario-studio
Prompts / gi-evidence-assistant
```

Per-analysis ExtraTrees models are versioned `search-model` artifacts but are
not promoted. Only a proposer that passes the public-network and multi-seed
benchmark gate may be linked into `Models / gi-search-proposer`. Registry is a
reviewed promotion mechanism, not an automatic deployment system. See
[W&B Registry](https://docs.wandb.ai/models/registry).

## Durable outbox and idempotency

Every `TelemetryEnvelope` has a deterministic idempotency key based on the local
operation, project revision, inputs, and artifact digests. The outbox insert is
transactional and retry-safe. In the source MVP, automatic bundle, guided,
completed-analysis, and export projections are inserted immediately after their
authoritative engineering commit; they are not yet part of that same producer
transaction. Atomic coupling or startup reconciliation is required before a
production release can claim there is no crash window.

The synchronizer:

1. claims one pending item transactionally;
2. verifies the local manifest and bytes;
3. resumes a known W&B Run ID or creates one deterministic run;
4. logs the ordered Tables and output Artifacts;
5. verifies returned versions and required hashes; and
6. persists the confirmed external references.

A crash before step 6 leaves a retryable item. The retry queries or resumes the
same W&B identity; it never creates a second engineering event. Completed local
physics cache entries remain immutable regardless of cloud state.

Uploads happen at solver-batch barriers rather than worker completion order.
That makes a search trace reproducible even when exact jobs finish in a
different parallel order.

## Offline operation

Initial binding requires network access. After verification, local SWMM work may
continue without a network. The outbox records pending evidence and W&B offline
storage can stage a Run. Synchronization resumes later through the worker or
[`wandb sync`](https://docs.wandb.ai/models/ref/cli/wandb-sync/).

Offline mode does not authorize a new destination, bypass Restricted access, or
enable the Evidence Assistant. It only preserves already-authorized engineering
work until the mirror can catch up.

## Evidence explanation traces

The current source MVP builds a deterministic, read-only explanation from local
evidence, validates it, records one Weave call, flushes the client, and reads
that call back before returning the answer. It does **not** invoke a language
model provider, materialize Weave Datasets, or emit the nested operation tree
below. The desktop UI therefore exposes deterministic guided explanations, not
a provider-backed Evidence Assistant.

The intended provider-backed implementation will initialize Weave with the same
`entity/project` used by the analysis. W&B documents automatic trace/run
association for traced operations executed in a W&B Run context; see
[link W&B runs and Weave traces](https://docs.wandb.ai/weave/guides/tracking/trace-to-run).

That later implementation must nest:

```text
assistant.request
  authorize_project
  resolve_local_evidence
  construct_context
  call_provider
  extract_numeric_claims
  validate_evidence_references
  validate_units_and_limitations
  format_response
  record_feedback
```

Before upload, the worker applies PII and sensitive-key redaction for secrets,
authorization material, tokens, credential IDs, and local paths, using the
controls described in the
[Weave redaction guide](https://docs.wandb.ai/weave/guides/tracking/redact-pii).

The present deterministic endpoint returns an answer only when:

- a Weave trace ID is confirmed;
- every numeric claim matches an exact local value, unit, and evidence ID;
- required limitations are present;
- no unauthorized action is proposed; and
- deterministic local validation passes.

If Weave is unavailable, the call fails closed. No untraced or unvalidated
answer is displayed. A future model-provider call must use the same rule.

## Evaluations and release gate

Versioned Weave Datasets for synthetic guided and benchmark questions remain a
release requirement; they are not materialized by this source MVP. The local
deterministic scorer already covers:

- evidence-ID validity;
- numeric and unit faithfulness;
- required limitations;
- scope-boundary and unauthorized-action violations;
- appropriate abstention; and
- prompt-injection or fabricated-evidence rejection.

LLM judges may help diagnose style or practitioner usefulness but cannot
override deterministic failures. Weave evaluations provide repeatable dataset,
function, and scorer comparisons; see
[Weave evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations).

Before a provider-backed assistant can ship, its release gate requires 100%
numeric/evidence/limitation fidelity, zero unauthorized actions, safe handling
of all injection/fabrication tests, and at least 80% positive practitioner
usefulness on the curated set.

## Deep links in the desktop

The worker can return verified links for:

- the analysis or guided-session Run;
- solver-jobs and evaluated-alternatives Tables;
- an Artifact version and its lineage graph;
- a search-model artifact or promoted Registry version;
- the project observability dashboard;
- a Weave assistant trace; and
- pending/failed outbox items.

Each link is shown beside the local object ID and expected artifact digest. It
supplements the Evidence Rail; it does not replace local evidence resolution.

## Local setup

Copy the empty template and provide values without committing the resulting
file:

```bash
cp .env.example .env
```

Required command-line variable names are:

```text
WANDB_API_KEY
WANDB_ENTITY
WANDB_PROJECT
WANDB_BASE_URL
```

Production desktop authentication uses the operating-system keychain instead.
Do not put an API key in source, application configuration, a shell command
argument, or a GitHub workflow file.

## Protected integration workflow

Pull-request CI uses disabled or in-memory adapters and never receives a W&B
credential. `.github/workflows/wandb-integration.yml` is manual and uses a
protected GitHub Environment named `wandb-integration`. Configure these secrets
and variable in that Environment:

```text
secret: WANDB_SERVICE_ACCOUNT_API_KEY
secret: WANDB_SERVICE_ACCOUNT_ENTITY
variable: WANDB_INTEGRATION_PROJECT
optional variable: WANDB_BASE_URL
optional variable: WANDB_DEPLOYMENT
```

The workflow uses a W&B service account, validates that the destination is
Restricted, performs the source-supported connection probe, uploads a tiny
synthetic probe Artifact, downloads it, and compares SHA-256. It prints neither
the credential nor environment contents and uploads no SWMM or client data.

## Operational checklist

Before enabling a production analysis:

- binding status is verified and destination access is `Restricted`;
- critical-infrastructure classification matches the deployment;
- the user has reviewed full-sync categories and exclusions;
- Models and Weave probes pass;
- local W&B directories are project scoped;
- the outbox is durable and idempotent;
- artifact manifest hashes reconcile;
- assistant calls fail closed without Weave; and
- the Evidence Rail remains complete when cloud access is unavailable.
