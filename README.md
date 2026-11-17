# GI Scenario Studio

Decision-first green-infrastructure alternatives screening with EPA SWMM,
W&B Models, and Weave.

GI Scenario Studio is a local-authority desktop workbench for environmental,
stormwater, and water-resources engineers. Its professional outcome is narrow
and useful:

> Turn a fit-for-purpose SWMM model into a defensible GI shortlist, with every
> hydraulic claim traceable to a direct SWMM run and, when the engineer enables
> cloud collaboration, computational and AI workflows mirrored to a Restricted
> W&B project.

The core engineering invariant is strict:

> Every reportable hydraulic value comes from a pinned SWMM execution. Search or
> ML may choose which exact candidate to simulate next; it never supplies a
> reportable hydraulic result.

> **Status: schema 2.0 source MVP.** The Cedar Trace verified replay, scientific
> contracts, evidence core, search benchmark harness, desktop UX, keychain
> boundary, and W&B/Weave integration seams are available from source. This
> repository does not yet claim a signed practitioner release: target-specific
> worker/SWMM packaging, release credentials, cross-platform integration gates,
> and installer signing remain release work.

The Evidence Guide release gate separates automated conformance from real-world
usefulness. The v1 provider campaign remains immutable historical failure
evidence. The architecture-first v2 campaign also stopped safely: its fresh
R2b trace canary reached W&B Inference and Weave, but Granite selected the wrong
finite explanation plan for a safety-critical unsupported question. The local
validator withheld the answer, the deterministic `D1` guide passed the same
case, and the recorded decision is `DISABLE_LIVE_GUIDE`. Practitioner
usefulness remains `not_measured`. No language-model explanation can be enabled
without a complete, unexpired, hash-pinned passing campaign receipt.

## What the application helps decide

The primary user already has a SWMM model suitable for alternatives screening.
The application leads with the decision—not a generic optimizer:

```text
Work locally or connect W&B -> Open bundle -> Decision brief -> Model fitness and baseline
-> Storms -> Opportunities and feasibility -> Goals and costs
-> Run plan -> Run -> Decision board -> Evidence package
```

The decision board puts pass/fail criteria, baseline deltas, controlling storms,
adverse changes, cost, limitations, and the next professional action ahead of
the Pareto plot. The Evidence Rail resolves any claim through its metric, storm,
direct solver job, semantic mutation, artifacts, hashes, QA, and assumptions.

Project kind (`demo`, `conceptual`, or `existing`) describes origin, not
maturity. Model fitness is recorded independently for one revision and intended
use in a Model Fitness Passport with one of four non-ordinal dispositions:
`not_assessed`, `fit_for_stated_use`, `fit_with_limitations`, or `not_fit`.
Numerical QA and cloud synchronization do not change that disposition.
Single-model bundle import atomically records an immutable model revision and a
truthful `not_assessed` passport. Learning evidence can therefore retain the
limitation immediately; only an identified engineer can replace that passport
through an explicit attestation for the active revision.

The target secondary conceptual-model route will assemble a planning model from
user-provided or approved open data, expose every default in an assumption
ledger, and join the same workflow after repair and review. The source MVP shows
that route's safety gates as a disabled, source-empty preview; GIS ingestion,
generation, repair, persisted provenance, and cloud projection are not complete.

## Optional W&B and Weave boundary

The application starts in `LOCAL_ONLY` mode and does not require a W&B account
for hydraulic work or local evidence. Engineers who want the cloud mirror bind
their GI project to a user-owned W&B project whose visibility is verified
exactly as `Restricted`. The binding wizard selects W&B Cloud, Dedicated Cloud,
or Self-Managed; verifies Models and Weave access; previews synchronized data
categories; and stores the W&B API key in the operating-system keychain.
Critical-infrastructure-restricted projects require Dedicated Cloud or
Self-Managed W&B.

The intended mirror covers models, GIS/data assets, storms, candidate and cost
records, exact INP/RPT/OUT artifacts, metrics, QA, search traces, decision
evidence, and exports. The source MVP automatically projects imported bundles,
an open-time guided-session snapshot, completed exact analyses, and worker-
created evidence exports. Conceptual data-asset provenance and generated-model
projection are not complete and must not be represented as synchronized yet.
Credentials, environment dumps, local absolute paths, and raw console logs are
always excluded.

The pinned W&B `0.28.1` [documented Project API](https://docs.wandb.ai/models/ref/python/public-api/project)
can confirm project identity and caller access, but does not currently expose
an exact project-visibility scope. The worker therefore checks supported exact
visibility fields first, then uses one isolated, narrow W&B GraphQL policy query
as an interim resolver. Any missing field, schema drift, or identity mismatch
fails closed. `PRIVATE` is never treated as `Restricted`, and a successful
access or upload probe is not used as a privacy substitute.

The local SQLite database and content-addressed artifact store remain
authoritative, and the application can be used entirely in `LOCAL_ONLY` mode.
When a project is deliberately bound, W&B [Runs](https://docs.wandb.ai/models/runs),
[Tables](https://docs.wandb.ai/models/tables/log_tables),
[Artifacts](https://docs.wandb.ai/models/artifacts/explore-and-traverse-an-artifact-graph),
and [Registry](https://docs.wandb.ai/models/registry) provide the collaboration
mirror. The outbox is durable and idempotent, and tests confirm cloud failure
does not alter candidate order, SWMM output, QA, costs, or exports. Coupling
every producer's engineering write and outbox insert in one SQLite transaction
is still a release gate; current automatic projections occur immediately after
the authoritative commit. After an initial verified binding, work may continue
offline and synchronize later using W&B's
[offline support](https://docs.wandb.ai/models/track/environment-variables).

The source MVP contains an opt-in, provider-backed Evidence Guide, but no
provider-backed architecture is currently released: v2 stopped at its mandatory
canary and retained deterministic `D1`. Any production-eligible arm would never
ask a model to write engineering facts. Depending on a future selected arm, the
provider would make one or two bounded calls to choose an intent, a read-only
tool route, or one finite explanation-plan ID. Repository code then supplies
every value, unit, citation, limitation, boundary statement, and allowed next
action.
Multiple questions can share one evidence-packet-pinned conversation, while
each question is a new
[Weave Agents turn](https://docs.wandb.ai/weave/guides/tracking/trace-agents)
with the actual LLM and tool spans. The worker flushes and reads the turn back
before returning an answer. Provider and trace inputs are normalized and
allowlisted locally. A deterministic local filter redacts sensitive fields,
paths, and common credential/PII patterns before tracing; standard projects use
no provider or trace content without explicit inference and content-logging
consent. Serverless Inference additionally requires a verified W&B Cloud
binding. The deterministic Evidence Rail works without the Guide, and an
untraced, expired, or unvalidated answer is never substituted as engineering
evidence.

The original
[`gi-evidence-guide-evaluation:v1`](benchmarks/evidence_guide/v1/README.md)
dataset and its failed campaign are retained unchanged as protocol-regression
evidence. They are not a release benchmark: v1 exposed trusted answer hints,
did not causally test tool use, and did not reliably detect spelled-out numeric
hallucinations.

The architecture-first v2 protocol provides 120 development scenarios and 120
project-family-separated validation scenarios. It reserves a 300-scenario
hidden-test design, but no access-controlled hidden corpus is currently
available or sealed: the checked-in hidden receipt is explicitly unsealed and
release-ineligible. Evidence IDs and claim order are remapped per attempt;
causal targets remain unavailable until the required tool executes. The
deterministic scorer is mutation-tested before provider calls. Run the
credential-free controls with:

```bash
PYTHONPATH=python .venv/bin/python scripts/build_evidence_guide_v2_corpus.py \
  --build-public
PYTHONPATH=python .venv/bin/python scripts/mutation_test_evidence_guide_v2.py \
  --split development
PYTHONPATH=python .venv/bin/python scripts/mutation_test_evidence_guide_v2.py \
  --split validation
```

The checked-in hidden file is a public status receipt, not evidence that hidden
rows exist elsewhere. Paid execution is stage-at-a-time, preregistered before
the first call, capped at $300 and the declared token/call ceilings, and spread
across three time blocks. W&B stores one Run per variant/model/replicate block;
Weave records component evaluations and model, tool, and validation spans.
Failed and incomplete arms are retained.

The completed R2b launch inherited a hash-pinned 160-attempt capability screen
from R1. Earlier preflights and 18 R1 architecture rows were invalidated and do
not enter any scientific denominator. A separate R2 Weave-readback incident
made zero provider calls and is retained only as operational evidence. The
fresh R2b canary then completed its observability path, including trace
acknowledgement, but `ibm-granite/granite-4.1-8b` chose
`recorded_rules_fail_v1` instead of the required `authority_boundary_v1` plan.
The deterministic validator blocked release; `D1` passed the same unsupported
case. Because the preregistration required a passing canary before architecture,
no architecture, prompt/configuration, validation, judge, hidden, or soak stage
ran, and the architecture/scientific denominator is zero. Cumulative
conservative accounting is 291 provider calls, 243,928 input tokens, 37,248
output tokens, and $0.013704. The one fresh canary call reported 302 input
tokens, 25 output tokens, and $0.000018. The result is
`DISABLE_LIVE_GUIDE`; deterministic `D1` remains active. See the
[v2 study protocol and result](docs/evidence-guide-v2-study.md) for the exact
architectures, stopping rules, receipts, and claim boundaries.

See [data governance](docs/data-governance.md) for the complete policy.

## Cedar Trace guided experience

Cedar Trace is one worker-backed, hash-verified synthetic story:

> A municipal stormwater engineer is deciding which Cedar Trace GI concept, if
> any, should advance to field and site-feasibility review.

It opens at Overview as **Verified fixture replay**. Replay reads a pinned
manifest; it does not pretend to run SWMM. The manifest records:

- 4 direct baseline jobs, one under each selected synthetic storm;
- 220 exact nonbaseline integer LID vectors × 4 storms = 880 jobs;
- 884 accepted direct EPA SWMM 5.2.4 jobs in total;
- hashes and extracted metrics for every job; and
- 20 complete retained INP/RPT/OUT sets: four baselines plus four teaching
  alternatives under four storms.

The teaching rules are visible before results:

- reduce aggregate `OUT1` event volume across the four storms by at least 6%;
- keep synthetic 30-year present-value screening cost at or below $200,000;
- do not increase `OUT1` peak flow in any selected storm; and
- require accepted fixture QA for every design × storm job.

The four retained alternatives teach why a Pareto point is not automatically an
acceptable engineering option:

| Role | Alternative | Outcome |
|---|---|---|
| Low-cost caution | `ALT-017 [0,0,1,6]` | $54.99k; 4.14% reduction; misses the target and raises two selected-storm peaks, with the worst response in the long event at 3.14→3.21 cfs |
| Carry-forward candidate | `ALT-207 [0,3,2,6]` | $190.25k; 6.42% reduction; meets all four teaching rules |
| Stronger, over budget | `ALT-109 [1,3,2,6]` | $222.01k; 6.95% reduction; exceeds the cost cap |
| Observed performance extreme | `ALT-014 [4,3,4,5]` | $361.86k; 8.82% reduction; strongest observed volume result but poor cost fit |

`ALT-207` is described only as:

> Candidate to carry forward for site and feasibility review.

It is not best, optimal, validated, approved, or design-ready.

An optional **Rerun all 884 jobs locally** action requires a packaged, hash-
approved SWMM 5.2.4 executable. It uses real progress and durable
pause/resume/cancel, then compares the new output with the pinned fixture using
metric-specific tolerances.

Read [the guided example specification](docs/guided-example.md) or inspect the
[exact manifest](benchmarks/guided_exact/manifest.json).

### Evidence-grounded report media

The companion article,
[The Stormwater Plan That Looked Like a Bargain](media/gi-scenario-film/story/article.md),
pairs seven recordings of the real guided React interface with five short
[deterministic Canvas films](media/gi-scenario-report-films/README.md). The
application clips demonstrate Storms, Model, Opportunities, Run, Compare,
Evidence Rail, and Export. The Canvas films explain the decision logic using
values projected from the checked-in guided fixture, exact 700-vector dataset,
search campaign, publication tables, and retained SWMM reports.

The active analytical films are silent-first, locally rendered, and published
to the existing Restricted W&B report with source hashes and artifact lineage.
They show modeled runoff-accounting and hydraulic signals, not measured field
or ecological outcomes. In particular, SWMM `Infiltration Loss` is not treated
as groundwater recharge, and the films make no claim about avoided flooding,
water quality, habitat, annual benefit, energy, or carbon.

Build and inspect the active report-film package from the repository root:

```bash
uv sync --locked --extra dev --extra science
pnpm install --frozen-lockfile
pnpm build:report-films:campaign
pnpm build:report-films:tables
pnpm build:report-films:data
pnpm test:report-films:node
pnpm render:report-films:stills
pnpm check:report-films:stills
pnpm render:report-films
pnpm check:report-films
pnpm publish:report-films:dry-run
```

The three build commands deterministically replay the frozen search benchmark,
rebuild its publication tables, and then project the film data; they do not run
new hydraulics. Rendering requires local Chrome, FFmpeg, and FFprobe. The final
command performs hash and lineage validation for the pinned
`wandb/gi-scenario-studio-article` destination but makes no W&B write. Actual
publication remains a separate explicit `--publish` operation after the target
is independently verified as exactly `RESTRICTED`.

The earlier [Manim project](media/gi-scenario-film/README.md) is preserved as
legacy source and historical review material. It is not part of the active W&B
report, rendering commands, artifacts, or CI pipeline.

An automated Playwright walkthrough records the seven interface views from the
deterministic browser preview, and the W&B publisher places application and
Canvas media beside the relevant article copy.

The seven article clips are truthful **interface-only verified replays**. They
inspect the checked-in exact fixture; they do not launch SWMM, show fake live
progress, create an export, or exercise native desktop/keychain behavior. A new
884-job execution remains a separate, optional desktop action that requires the
approved packaged SWMM executable. See the
[legacy film README](media/gi-scenario-film/README.md) for the exact preview,
recording, extraction, and validation commands; use the
[active report-film README](media/gi-scenario-report-films/README.md) for the
Canvas render and Restricted W&B publication workflow.

## 2,800-job hidden-oracle benchmark

The teaching replay is deliberately separate from adaptive-search evaluation.
The benchmark generator evaluates the complete 700-vector Cedar Trace universe:

```text
4 baseline jobs + 699 alternatives × 4 storms = 2,800 direct SWMM jobs
```

It produces an immutable schema `2.0` exact-result dataset. Random, scrambled
Sobol, integer NSGA-II, and Sobol + ExtraTrees replays can see a design's stored
outcome only after requesting that vector; each request costs four exact-job
equivalents. The harness reports hypervolume ratio, epsilon indicator, feasible
and adverse-result recall, selection counts, and exact-job-equivalent budget
without re-running identical hydraulics for every method and seed. Campaign
timing, peak memory, and measured-or-estimated energy observations remain a
release gate and are not synthesized from the hidden table.

The NSGA-II comparator is a deterministic, batch-barriered `(mu + lambda)`
implementation (`mu = 64`, `lambda <= 32`). It uses binary feasibility-first
nondominated sorting, normalized crowding distance, rank/crowding binary
tournaments, bounded uniform integer crossover, bounded random-reset mutation,
and front/crowding environmental survival. Exact vectors are deduplicated before
oracle requests; deterministic QMC fills only operator-exhaustion gaps and is
reported separately in the replay trace.

The benchmark does not enable ML by itself. Production remains on Sobol unless
the declared public-network, multi-seed release gates are met. Predicted
hydraulic values and unsupported “carbon saved” claims are excluded.

### Completed Cedar Trace campaign

The checked-in campaign runner replayed four methods at 64, 128, and 256
requested designs over the same ten seeds: 120 controlled runs in total. Each
design request was charged as four exact-SWMM-job equivalents because every
candidate has four storm responses in the frozen oracle. The campaign performed
no new hydraulic executions and did not measure energy.

| Design budget | Job equivalents | Random median HV | Sobol median HV | NSGA-II median HV | Sobol + ExtraTrees median HV |
|---:|---:|---:|---:|---:|---:|
| 64 | 256 | 0.100 | 0.190 | 0.190 | 0.190 |
| 128 | 512 | 0.357 | 0.501 | 0.679 | 0.399 |
| 256 | 1,024 | 0.616 | 0.570 | 1.000 | 1.000 |

At the largest budget, ExtraTrees recovered a median 83.3% of the twelve
complete-rule alternatives and 46.2% of the 421 alternatives with a modeled
peak increase. It nevertheless failed four of five declared release checks:
the median-hypervolume improvement occurred at only one budget, its worst
10th-percentile hypervolume gap to Sobol was -0.052, adverse recall was below
the declared threshold, and only one synthetic network was tested. The 95%
adverse-recall threshold is itself impossible at this budget: 256 requests can
recover at most 60.8% of 421 adverse alternatives. That gate must be replaced
before a new multi-network campaign is examined. Sobol therefore remains the
production search and adaptive ML remains experimental.

Build the local campaign, reviewed figures, W&B publication plan, and companion
article with:

```bash
uv sync --extra science --extra dev --extra observability
PYTHONPATH=python .venv/bin/python scripts/run_search_benchmark_campaign.py --workers 1
PYTHONPATH=python .venv/bin/python scripts/build_benchmark_publication.py
PYTHONPATH=python .venv/bin/python scripts/publish_benchmark_study.py \
  --entity "$WANDB_REPORT_ENTITY" \
  --project "$WANDB_REPORT_PROJECT"
```

The last command is a hash-validating dry run. Add `--publish` only after the
destination is independently verified as exactly `RESTRICTED`. Publication
uses deterministic W&B run IDs, a pinned dataset artifact, typed Tables,
per-run search traces, and a hash-verified campaign artifact. Read the
[research basis](docs/research-basis.md) for scientific context and explicit
claim boundaries.

After publication, verify the actual cloud file inventories and record the
result in the local receipt:

```bash
PYTHONPATH=python .venv/bin/python scripts/publish_benchmark_study.py \
  --entity "$WANDB_REPORT_ENTITY" \
  --project "$WANDB_REPORT_PROJECT" \
  --audit-only
```

The audit fails if W&B automatic metadata, requirements, or console files are
present. This matters because disabling code and Git capture alone does not
remove host paths and machine details from every SDK-generated file.

## Source MVP capability map

| Area | Present in source | Release claim not made yet |
|---|---|---|
| Guided workflow | Cedar Trace overview, decision rules, acceptance matrix, adverse-result lesson, Evidence Rail, 884-job verified replay, and 20 retained run sets | A calibrated or real municipal model |
| Scientific core | Pydantic schema 2.0 contracts, secure bundle/parser/mutation services, SQLite state, revision-pinned analyses, exact solver runner, search, metric/QA records bound to retained RPT hashes and extractor versions, repository-recorded learning exports, and guided/benchmark generators | Universal compatibility with every SWMM or PCSWMM extension |
| W&B/Weave boundary | Restricted-project policy contracts, fail-closed exact-scope gate, content-hash manifests, durable outbox, exact-version artifact verification, typed Tables, SDK adapters, host-owned masked credential prompt and keychain storage, one- or two-call finite-plan W&B Inference arms recorded through Weave Agents, ordinary Run-linked scientific operation calls, deterministic local trace redaction, six read-only evidence tools, immutable v1 failure evidence, separated v2 development/validation corpora with an explicitly unsealed hidden-test receipt, mutation-tested scorers, causal tool ablations, automated judge gates, budgeted campaign orchestration, safe browser preview, and protected integration workflows | An authoritative Restricted-scope resolver for the pinned Public API, an available sealed hidden lockbox, a completed passing v2 architecture/validation/judge/hidden/soak receipt, measured practitioner usefulness, or cloud authority over engineering results |
| Search evaluation | Finite-universe generator and hidden-oracle Random/Sobol/NSGA-II/ExtraTrees replay | A globally optimal design or approved ML proposer |
| Desktop distribution | React/Tauri/Python source and target packaging contract | Signed/notarized macOS or signed Windows installer in this repository |
| Professional workflow | Secure portable-bundle import, parsed preflight, versioned unassessed passport and constrained engineer attestation, explicit storm and LID-slot setup, direct run arithmetic, durable exact execution controls, complete-ensemble comparison, adverse-peak scan, run-level Evidence Rail, and repository-gated learning export | Cost/criterion authoring, accepted Decision Brief authoring, reference reproduction UI, or a completed calibration, survey, geotechnical, utility, permitting, and construction workflow |

An unavailable production path must be disabled with an explanation. The source
UI must not fabricate cloud connectivity, progress, solver output, or
professional evidence.

### Source-MVP release gates

The following boundaries are intentionally visible rather than papered over:

- Bundle, guided-session, completed-analysis, and export evidence are projected
  automatically, but their producer writes are not yet atomically coupled to
  outbox insertion. Crash-safe producer reconciliation remains release work.
- User/open GIS assets and generated conceptual models do not yet have the full
  persisted provenance and W&B projection required by the product contract.
- Imported projects can execute an explicit engineer-selected exact test set,
  but practitioner cost contracts, threshold evaluation, and accepted Decision
  Brief authoring are still withheld; the interface does not name a preferred
  alternative. Cost-bearing, screening, and verified-alternatives exports fail
  closed until repository-backed CostBasis, full-network review, and reference-
  comparison services can prove those claims. Learning evidence remains
  available and revision-pinned.
- Bundles containing multiple INP models require an explicit persisted base-
  model selection. Other INP files remain unclassified companions and are not
  treated as storm variants until a future semantic equivalence service proves
  storm-only differences. Restart recovery still needs a dedicated analysis-
  status read RPC; the current UI derives live job progress from the durable
  result ledger.
- W&B `0.28.1` does not expose an authoritative exact project-visibility scope
  through its documented Project object. The current interim resolver is one
  isolated, narrow GraphQL policy query that must return exact `RESTRICTED`
  identity; `PRIVATE`, missing fields, and schema drift all fail closed.
- The provider-backed Evidence Guide and its Weave Agents turns are implemented.
  Both v1 real candidate evaluations failed and remain immutable historical
  evidence. The v2 runtime limits production-eligible models to selecting finite
  interpretation plans. R2b inherited the valid 160-attempt capability screen,
  then stopped at its preregistered trace canary when Granite selected
  `recorded_rules_fail_v1` instead of `authority_boundary_v1` for a
  safety-critical unsupported case. The validator released no model answer;
  deterministic `D1` passed the same case and remains active. No architecture,
  validation, judge, hidden, or soak denominator was opened, and the hidden
  lockbox remains unavailable and unsealed. The checked decision is
  `DISABLE_LIVE_GUIDE` and practitioner usefulness is `not_measured`.
- Search replays report decision quality and exact-job-equivalent budgets, but
  campaign timing, peak memory, and measured-or-estimated energy collection are
  not yet implemented.
- CI covers contracts, Python, React, Rust, secret boundaries, a complete
  lockfile vulnerability scan, and dependency-license policy. Automated
  axe/WCAG browser checks, packaged-installer/SBOM audits, real packaged-sidecar
  builds, signing, and notarization remain release gates.

## Quick start: UI and guided replay

Requirements:

- Node.js 22
- pnpm 11.9.0, pinned in `package.json`

```bash
corepack enable
pnpm install --frozen-lockfile
pnpm dev
```

Open <http://localhost:1420>. Browser mode is an explicitly labelled development
preview: it can inspect the synthetic guided evidence and cloud UX but cannot
authenticate, verify a Restricted project, use a keychain, or create a
practitioner cloud binding.

Frontend checks:

```bash
pnpm test:frontend
pnpm build
```

## Scientific worker setup

Python 3.11 or newer is supported; CI uses Python 3.12. With `uv`:

```bash
uv sync --locked --extra dev --extra science --extra export --extra observability
uv run --no-sync pytest tests/python
uv run --no-sync ruff check python tests scripts
uv run --no-sync ruff format --check python tests scripts
```

Or install editable extras in an activated virtual environment:

```bash
python -m pip install -e '.[dev,science,export,observability]'
python -m pytest tests/python
```

The worker accepts JSON-RPC 2.0 over JSONL standard input. This read-only smoke
test uses the same API major version as the desktop host:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"system.ping","params":{}}' \
  | gi-scenario-worker --transport jsonl-stdio --api-version 2.0
```

## W&B development configuration

Never commit or hard-code an API key. Start from the empty variable-name
template:

```bash
cp .env.example .env
```

Set only the values needed for local command-line integration:

```text
WANDB_API_KEY
WANDB_ENTITY
WANDB_PROJECT
WANDB_BASE_URL
WANDB_DEPLOYMENT
WANDB_MODE
```

The worker applies defensive defaults:

```text
WANDB_CONSOLE=off
WANDB_DISABLE_CODE=true
WANDB_DISABLE_GIT=true
WANDB_ERROR_REPORTING=false
WANDB_SHOW_RUN=false
```

Production desktop users enter their key once through the binding wizard. The
native host stores it in the operating-system keychain; it is not stored in
`.env`, SQLite, logs, diagnostics, artifacts, exports, or process arguments.
Treat API keys as passwords, as required by the
[W&B API guidance](https://docs.wandb.ai/models/track/public-api-guide).

The browser preview never accepts or tests a real credential. CI on pull
requests uses mock or disabled adapters and receives no W&B secret. Only the
manually approved `W&B integration` workflow uses service-account secrets.

## Regenerate exact guided evidence

Build the official EPA SWMM console executable from the pinned tag and commit
recorded in [the engine manifest](engine/swmm-5.2.4.json). Use a new or empty
output directory:

```bash
python scripts/generate_guided_fixture.py \
  --solver /path/to/runswmm \
  --output /new/empty/guided-output

python scripts/generate_guided_ui_fixture.py \
  --manifest /new/empty/guided-output/manifest.json \
  --output /new/empty/guidedFixture.json
```

The generator refuses a non-5.2.4 executable. It launches each job without a
shell, records the executable hash, and writes the manifest only after job
accounting and QA reconcile.

## Generate and replay the exhaustive benchmark

Generate the immutable 2,800-job table with the same pinned solver:

```bash
python scripts/generate_exhaustive_benchmark.py \
  --solver /path/to/runswmm \
  --output /new/empty/exhaustive-output
```

Run an offline hidden-oracle replay:

```bash
gi-studio-benchmark \
  --dataset /new/empty/exhaustive-output/dataset.json \
  --method sobol \
  --budget 128 \
  --seed 424242 \
  --output /new/empty/sobol-128.json
```

Release-gate benchmark campaigns use `--wandb` and refuse to log unless a
Restricted integration has already been established and the W&B variables point
to the intended test project. A replay without that flag is only a local
reproducibility check, not a GI project or accepted release-gate record.
Practitioner analysis is never coordinated by W&B Sweeps.

## Public contracts

Pydantic is the schema authority. Generate schema `2.0` JSON Schema and matching
TypeScript/Zod declarations with:

```bash
uv run --no-sync python scripts/generate_schemas.py
```

Outputs live in [`schemas/v2.0/`](schemas/v2.0/) and
[`apps/desktop/src/generated/contracts.ts`](apps/desktop/src/generated/contracts.ts).
CI fails on schema drift. Schema `2.0` is a breaking public release; the worker
does not include a legacy `1.0` payload adapter.

## Desktop development

The Tauri host additionally requires Rust 1.88 or newer and a target-specific
worker sidecar at:

```text
apps/desktop/src-tauri/binaries/gi-scenario-worker-$TARGET_TRIPLE[.exe]
```

With the worker and approved SWMM executable packaged:

```bash
pnpm desktop
```

A missing worker or solver is a blocking setup error, not a reason to substitute
browser data. A production release must build the target worker and SWMM from
pinned sources, run the integration and differential suites, generate a
CycloneDX SBOM and checksums, include notices, and sign/notarize the installer.
See [the desktop packaging contract](docs/platform-packaging.md).

## Verification and CI

The required workflow runs Python tests and formatting, secret-boundary checks,
schema and guided-projection drift checks, frontend tests/build, Rust formatting,
Clippy and tests, plus an OSV v2.3.8 scan of `uv.lock`, `pnpm-lock.yaml`, and
`Cargo.lock`. OSV findings fail closed unless they have an advisory-specific,
expiring entry in [`osv-scanner.toml`](osv-scanner.toml). A deterministic policy
check rejects AGPL and SSPL dependency licenses and reports unresolved license
metadata for packaging review. GitHub Dependency Review can be enabled as an
additional check with `ENABLE_GHAS_DEPENDENCY_REVIEW=true` when the repository
has GitHub Advanced Security. CI never receives production credentials.

The separate manually dispatched
[`wandb-integration.yml`](.github/workflows/wandb-integration.yml) is bound to a
protected GitHub Environment. It uses a W&B service-account key from Actions
secrets, verifies the Restricted target, performs the supported mock-free
connection and tiny artifact hash roundtrip, and never echoes the key.

Run the local source checks before proposing a change:

```bash
uv run --no-sync pytest tests/python
uv run --no-sync ruff check python tests scripts
uv run --no-sync ruff format --check python tests scripts
pnpm test:frontend
pnpm build
cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --check
cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
osv-scanner --config=osv-scanner.toml --recursive --licenses --all-packages \
  --format=json --output-file=/tmp/gi-osv-results.json ./
python3 scripts/check_osv_policy.py /tmp/gi-osv-results.json
```

## Architecture and governance

- [Platform architecture](docs/platform-architecture.md) — processes, durable
  state, JSON-RPC, evidence, outbox, W&B/Weave, credentials, and security.
- [Observability operations](docs/observability.md) — binding, cloud mapping,
  retry semantics, deep links, assistant tracing, and integration gates.
- [Data governance](docs/data-governance.md) — full-sync categories, exclusions,
  retention, data classification, licensing, and trace redaction.
- [Guided example](docs/guided-example.md) — Cedar Trace story, rules, alternatives,
  claims, replay, and rerun.
- [Product principles](docs/product-principles.md) — exact-result and AI boundaries.
- [Research basis](docs/research-basis.md) — technical choices and source basis.

## Scope and claim boundaries

GI Scenario Studio is an alternatives-screening application. It is not a
replacement for calibration, survey, geotechnical investigation, utility
locating, maintenance planning, constructability review, official SWMM editing,
or professional judgment. It does not automatically make regulatory,
flood-risk, compliance, or construction decisions.

Acceptable result language is precise:

- “Exact SWMM result under documented assumptions.”
- “Nondominated among exact alternatives evaluated.”
- “Tested against the selected storm cases.”

The application does not claim real-world truth, a verified global optimum,
regulator approval, a filing-ready package, or guaranteed resilience.

## Repository map

```text
apps/desktop/              React interface and Tauri host
python/gi_scenario_core/   Scientific worker and domain services
schemas/v2.0/              Generated public JSON Schemas
tests/python/              Core, evidence, observability, solver, and export tests
benchmarks/guided_exact/   Cedar Trace 884-job manifest and 20 retained run sets
engine/                    SWMM source pin and platform build hashes
scripts/                   Schema, fixture, benchmark, and security checks
docs/                      Architecture, governance, UX, packaging, and research
.github/workflows/         Source CI and protected W&B integration gate
```

## License and notices

Application code is [Apache-2.0](LICENSE). EPA SWMM is United States public
domain subject to EPA disclaimer and no-endorsement terms. SWMManywhere is
BSD-3-Clause and optional. Dataset and benchmark terms remain attached to their
manifests. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [NOTICE](NOTICE).
