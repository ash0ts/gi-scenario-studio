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
also does not expose an exact project-visibility scope. The worker checks a
supported exact field when present and otherwise uses an isolated narrow W&B
GraphQL policy query. It must return exact `RESTRICTED` identity; `PRIVATE`,
missing fields, identity mismatch, and schema drift all fail closed before
probes or uploads.

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

## Scientific operations in Weave

Weave records three bounded scientific operations as ordinary calls, not Agent
turns: adaptive-search batch proposal, exact-result ingestion, and
adverse-effect evaluation. The exact-ingestion call summarizes a completed
analysis barrier rather than creating one trace per SWMM subprocess. The
adverse-effect call records only aggregate finding counts. Search-proposal
calls record batch configuration and selection counts; they never contain a
predicted hydraulic result.

When a corresponding W&B analysis or benchmark Run is known, the worker sets
the Weave client's W&B run context so the call carries the native Run
association. Exact-analysis run IDs are deterministic, allowing a call made
before asynchronous artifact upload finishes to link to the first exact-batch
Run when it appears. Opaque hashes replace local project and analysis IDs in
trace inputs.

These hooks are best effort. Weave errors are caught after the local scientific
operation has committed and cannot change candidate order, exact SWMM output,
QA, adverse findings, or the decision board.

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
enable the Evidence Guide. It only preserves already-authorized engineering
work until the mirror can catch up.

## Evidence explanation traces

The source MVP includes an opt-in, provider-backed Evidence Guide. One active
conversation is pinned to one evidence packet, its SHA-256, selected anchor,
analysis, model revision, W&B destination, provider policy, model, prompt,
response schema, tool contract, and validator version. The same conversation
can contain multiple ordered turns; each question is normalized independently,
and the provider receives only the prior allowlisted intent and canonical safe
question—not raw earlier messages or generated prose.

Each released turn follows the finite-plan architecture pinned by the unexpired
v2 campaign receipt. `A1` makes one provider call after deterministic routing;
`A2` makes an intent call and a finite-plan call; `A3` makes a bounded tool-route
call while target evidence is hidden, then a finite-plan call. The available
subset comes from six deterministic, read-only tools:
`decision.get_rule_matrix`, `results.compare_exact`,
`results.get_runoff_ledger`, `evidence.resolve`, `model.get_fitness`, and
`project.list_limitations`. These tools expose no generic SQL, filesystem,
solver, attestation, or mutation capability. Provider calls return only bounded
intent/tool/plan identifiers under
[strict structured output](https://docs.wandb.ai/inference/response-settings/structured-output).
Numeric values, units, evidence references, required limitations, boundary
language, and action text are materialized and validated from the repository;
they are never trusted to generated prose.

The actual [Weave Agents trace](https://docs.wandb.ai/weave/guides/tracking/trace-agents)
shape is:

```text
Conversation (logical grouping by conversation_id)
  Turn (one trace for one question)
    LLM: optional intent or bounded tool routing
    Tool: selected read-only calls
    LLM: finite explanation-plan selection
    Local: deterministic render and validation
```

Binding authorization and evidence-packet construction happen locally before
that traced turn. The raw user question is retained locally; it is mapped to an
allowlisted intent and canonical safe question before either W&B Inference or
Weave receives it. Provider and trace payloads are canonicalized and audited;
prohibited keys and file/path data are excluded, project and repository IDs are
hashed or omitted for non-demo projects, tool arguments are rebound to the
selected anchor, and tool results pass a recursive local allowlist before
transport.

Before any trace call, a repository-owned deterministic filter removes
prohibited-key fields and local paths and redacts common credential, bearer,
email, government-ID, and labelled-phone patterns. Standard engineering
projects cannot invoke the provider unless the user explicitly consents to both
inference and content logging; the synthetic fixture may log full allowlisted
content, but it passes through the same filter. Weave's sensitive-key sanitizer
remains enabled as defense in depth.

Weave's optional Presidio-backed `redact_pii` setting is deliberately disabled.
Its language model can require a runtime download, which is incompatible with
an offline-capable packaged engineering application and its locked supply
chain. The deterministic filter is not a general PII classifier and does not
establish that domain-specific engineering data is non-sensitive. Local
allowlisting and provider-payload audits remain the mandatory transport
boundary; see the
[Weave redaction guide](https://docs.wandb.ai/weave/guides/tracking/redact-pii)
for the separate SDK capability.

The endpoint returns an answer only when:

- the project is online, verified, exactly `RESTRICTED`, and not classified as
  critical infrastructure;
- explicit inference consent exists and a non-demo project has explicitly
  consented to trace content;
- the evidence packet still matches the conversation-pinned digest;
- the configured model, tool names, tool count, tool subjects, provider response
  IDs, and structured output satisfy their pinned limits;
- every numeric claim matches an exact local value, unit, and evidence ID;
- required limitations are present;
- no unauthorized action is proposed;
- deterministic local validation passes; and
- the OpenTelemetry-backed turn has real trace/span IDs, flush succeeds, and
  `get_agent_turn(trace_id=...)` reads the turn back from Weave.

Failure at any provider, policy, tool, validation, flush, or acknowledgement
step marks the local turn failed and returns no generated answer. The
deterministic Evidence Rail remains available; no untraced or unvalidated
fallback is substituted.

## Evaluations and release gate

The repository preserves
`benchmarks/evidence_guide/v1/dataset.json` and its failed real campaign as a
48-row historical protocol-regression record. It cannot enable the guide.

The v2 study materializes 120 development scenarios and 120 frozen validation
scenarios, plus a checksum receipt for a 300-scenario service-account lockbox.
Splits are separated by project and question-template families. Per-attempt
provider packets remap IDs, shuffle distractors, remove trusted intent/answer/
plan fields, and keep causal targets hidden until tool execution. The local
scorer covers:

- evidence-ID validity, freshness, subject, and project binding;
- digit and spelled-out numeric, unit, sign, and rounding faithfulness;
- required limitations;
- required allowed actions;
- scope-boundary and unauthorized-action violations;
- appropriate abstention; and
- prompt-injection or fabricated-evidence rejection; and
- paired counterfactual proof that the tool result changed the selected plan.

Two separately calibrated LLM judges may compare automated explanation quality
but cannot override any deterministic safety failure. Their result is never
called practitioner usefulness. Weave evaluations provide repeatable dataset,
function, and scorer comparisons; see
[Weave evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations).

`scripts/evaluate_evidence_guide.py` validates the corpus, scores saved outputs,
and publishes non-reference evidence with `weave.Dataset`, `weave.publish`, and
Python `EvaluationLogger`. A failed or incomplete candidate is legitimate
evaluation evidence: every versioned case is logged, while a provider or local
validation failure is represented as `not_produced` with zero deterministic
scores rather than fabricated model output. Failure evidence never enables the
Guide.

The same command can integrity-check a complete two-model campaign, publish both
candidate evaluations, and publish the exact `release-gate.json` plus pinned
candidate-receipt projections as one versioned Weave campaign object. Raw
receipts remain local and are represented by their SHA-256. Aggregate inference
usage is retained as `input_count`, `output_count`, and `total_count` with an
explicit `tokens` unit; credential-like `*token*` field names remain prohibited
at the trace boundary. Publication is
explicit, fails unless the destination policy is exactly `RESTRICTED`, and
records SDK-returned evaluation URLs and object references in a local receipt.
`--dry-run` performs all local hash, scorer, candidate-receipt, and campaign-gate
checks without credentials, cloud SDK imports, or network calls. The synthetic
reference projection remains useful for scorer conformance but is rejected as
model evidence.

The current v1 campaign receipt records two published failed evaluations in the
exact Restricted `wandb/gi-scenario-studio-article` project; the checked-in gate
is `DISABLE_LIVE_GUIDE` and selects no model. A separate hash-pinned synthetic
integration-probe receipt records three real Granite turns in two Weave Agents
conversations. The gate was overridden only inside the injected test harness.
The second ALT-017 question remained in its original conversation; ALT-207 began
a clean conversation. Independent SDK readback confirmed trace and root-span
identity, message types, tool activity, and root `ERROR` status. Local validation
rejected every turn with `EVIDENCE_NEXT_CHECK_SELECTION_INVALID` or
`EVIDENCE_GENERATED_NUMBER_BLOCKED` before any generated answer was released.
Those evaluation and conversation links are failure-audit evidence, not release
approval.

The deterministic gate measures explanation fidelity, not field truth or
practitioner usefulness. Repository-owned redaction and Weave's sensitive-key
sanitizer are defense in depth for the trace record; local allowlisting is
applied before every provider and trace request.

The v2 gate is lexicographic. It requires zero accepted unsafe answers; zero
fabricated, stale, cross-project, wrong-subject, or unresolved evidence; perfect
repository fidelity and authority-boundary handling; perfect causal behavior;
the preregistered provider-reliability, intent, false-refusal, latency, trace,
and automated-judge thresholds; a complete one-time hidden test; and a
fail-closed operational soak. Later quality metrics cannot compensate for an
earlier safety failure.

The release receipt pins the model, architecture, prompt, schema, tools, plan
registry, renderer, validator, datasets, judges, cloud receipts, budget ledger,
and exact production scope for ninety days. The deterministic `D1` control is
allowed to win or tie; in that event W&B Inference stays disabled. Missing,
incomplete, over-budget, expired, unavailable, mismatched, or failed receipts
all return `DISABLE_LIVE_GUIDE`. Practitioner usefulness is deliberately
`not_measured` and is never inferred from automated judge preference.

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
- W&B automatic metadata, requirements, system metrics, and console capture are
  disabled, and the cloud run-file audit reports zero prohibited files;
- assistant calls fail closed without Weave; and
- the Evidence Rail remains complete when cloud access is unavailable.
