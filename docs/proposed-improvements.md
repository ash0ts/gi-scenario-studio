# GI Scenario Studio — Proposed Product, Simulation, Benchmarking, and Responsible-AI Improvements

> **Historical design input — superseded by the schema 2.0 public MVP.** This
> document is preserved as the research review that informed the current
> implementation. Its readiness ladder, optional W&B/Weave architecture,
> pre-v2 repository inventory, and related recommendations are not current
> product requirements. The current sources of truth are the
> [README](../README.md), [product principles](product-principles.md),
> [platform architecture](platform-architecture.md),
> [data governance](data-governance.md),
> [observability contract](observability.md), and
> [guided example](guided-example.md). Schema 2.0 uses an independent Model
> Fitness Passport, requires every production project to bind to a user-owned
> W&B project verified as `Restricted`, mirrors full engineering evidence, and
> prohibits untraced Evidence Assistant responses.

## Executive answer

GI Scenario Studio has the right foundation for a trustworthy engineering product, but it is still under development and is not yet an end-to-end alternatives-analysis application.

The repository already has important pieces: typed contracts, a line-preserving SWMM parser, readiness levels, an isolated SWMM subprocess runner, durable SQLite state, candidate generation, an Evidence Rail interface concept, and an intentionally limited conceptual-model path. The missing piece is the connection between them.

The most important improvement is one complete, honest workflow:

```text
Import a real SWMM project
  → reproduce its baseline
  → define one engineer-approved GI alternative
  → run every required storm in pinned EPA SWMM
  → extract and QA the hydraulic metrics
  → persist immutable evidence
  → compare real alternatives in the desktop
  → resolve every number through the Evidence Rail
  → export a reproducible package
```

Weights & Biases and Weave can help, but they should have narrow and different roles:

- **W&B should measure whether a search method finds useful exact alternatives with fewer SWMM jobs.** It is development and benchmark observability, not the hydraulic runtime.
- **Weave should trace and evaluate an optional, read-only Evidence Assistant.** It helps test whether the assistant is accurate, sourced, bounded, and useful.
- **EPA SWMM remains the source of hydraulic results. The local project database remains the evidence authority. The engineer remains the decision-maker.**

The best first LLM feature is not an autonomous design agent. It is a project guide that explains validation findings, locates assumptions, compares exact results, and drafts evidence-linked text without generating engineering values or changing the model.

## 1. Repository truth: what exists and what does not

This distinction should remain visible in product copy, demos, release notes, and internal planning.

| Capability | Current state | What remains |
|---|---|---|
| SWMM execution | A real isolated subprocess runner can invoke a supplied executable and preserve `.inp`, `.rpt`, and `.out` artifacts | Package and hash the permitted EPA SWMM engine; connect execution to the complete desktop workflow |
| Baseline run | The worker can launch baseline models when an executable is supplied | Add complete metric extraction, reference comparison, desktop progress, and actionable QA |
| Alternatives analysis | Analysis records and durable states exist | Materialize alternatives, create design × storm jobs, execute them, extract metrics, and compute the exact frontier |
| SWMM parsing and mutation | Line-preserving parsing and typed mutations exist | Broaden golden fixtures, semantic diffs, validation coverage, and full workflow use |
| Hydraulic metrics | Report-level continuity parsing exists | Add a versioned `.out`/API extraction layer for node, link, outfall, flooding, surcharge, and event metrics |
| Search | Enumeration, scrambled Sobol sampling, and an optional ExtraTrees proposer exist | Implement integer NSGA-II, stronger constraint handling, benchmark harnesses, and evidence-based enablement gates |
| Desktop UX | The product direction and Evidence Rail are well developed | Replace guided-example progress and static comparisons with authoritative worker data |
| Concept model | A deterministic local generator and a SWMManywhere adapter boundary exist | Pin and integrate a real SWMManywhere bridge, provider data, provenance, and map repair |
| Exports | Repository-bound JSON, CSV, Markdown, HTML, and optional verified run artifacts are available | Implement DOCX/XLSX packages and always include selected `.inp`, `.rpt`, `.out`, hashes, and limitations |
| Benchmarking | A CC0 teaching fixture exists with four candidate slots and 700 possible design vectors | Generate golden exact results, the exhaustive frontier, tolerances, a repeatable runner, and cross-platform comparisons |
| Reproducibility | Hashes, seeds, state, and manifests are designed into the core | Remove remaining nondeterminism, including random export identifiers from deterministic comparisons |
| W&B and Weave | Not part of the authoritative engineering path | Add optional adapters only after the local benchmark and assistant contracts are stable |
| LLM assistant | Not required by the current scientific core | Add a read-only Evidence Assistant only after exact results and Evidence Rail retrieval are operational |

Key repository evidence for this snapshot:

- [`solver.py`](../python/gi_scenario_core/solver.py) contains the real isolated subprocess runner and report-level QA parsing.
- [`rpc_worker.py`](../python/gi_scenario_core/rpc_worker.py) exposes baseline execution, while the current alternatives command primarily advances durable analysis state.
- [`search.py`](../python/gi_scenario_core/search.py) contains enumeration, Sobol sampling, and the gated ExtraTrees proposer; integer NSGA-II is not yet implemented there.
- [`concept.py`](../python/gi_scenario_core/concept.py) contains the local concept generator and injected SWMManywhere adapter boundary.
- [`demo.ts`](../apps/desktop/src/data/demo.ts) supplies the current guided-example result records used by the desktop.
- [`export.py`](../python/gi_scenario_core/export.py) shows the currently implemented export formats.
- [`cc0_reference`](../benchmarks/cc0_reference/) is the finite teaching fixture from which the first exhaustive benchmark can be built.
- [`test_repository_export_solver.py`](../tests/python/test_repository_export_solver.py) includes the deterministic-export comparison that currently exposes random export-ID metadata.

The product should be described as **in development** until the complete exact-simulation workflow is operational. A polished interface must not imply that static guided-example results came from the user’s model.

## 2. Non-negotiable product boundaries

### 2.1 Exact means simulator-exact

An exact result is produced by a pinned and hashed SWMM execution from documented inputs. It is not automatically a calibrated result, a field observation, a regulatory determination, or real-world truth.

Use:

> Exact SWMM result under documented assumptions.

Do not use:

> AI-predicted hydraulic result.

> Guaranteed flood reduction.

> Verified global optimum.

The only exception to the last statement is a finite candidate universe that was completely evaluated.

### 2.2 Machine learning allocates compute

The ExtraTrees proposer, or any future surrogate, may rank unevaluated candidate vectors. It may not supply a reportable flow, depth, HGL, flood volume, duration, or cost.

Every alternative shown in results must have been run through SWMM for every required storm. Predicted values belong only inside the internal acquisition process.

### 2.3 Readiness remains human-controlled

Software may identify evidence and blockers, but it may not promote a project to `ENGINEER_REVIEWED` or `REFERENCE_MATCHED`. An LLM may not attest, approve assumptions, accept QA exceptions, or declare a model calibrated.

### 2.4 Local evidence remains authoritative

The authoritative record is:

```text
Project SQLite
  + content-addressed local artifacts
  + versioned schemas
  + deterministic export manifests
```

W&B and Weave may receive an optional, redacted projection of that record. Their availability, state, or URLs must never be required to run SWMM, open a project, resolve an Evidence Rail item, or create a professional export.

### 2.5 The core product remains useful offline

Existing-model import, validation, simulation, comparison, Evidence Rail, and export must not need the internet. Network access is limited to explicitly approved open-data acquisition and optional cloud services.

## 3. Target technical flow

```text
Engineering truth path

Desktop UI
  → Tauri host
  → Python scientific worker
  → authoritative SQLite transaction
  → typed model mutation
  → durable design × storm scheduler
  → pinned SWMM subprocess
  → immutable run artifacts
  → versioned metric extraction and QA
  → exact comparison and Evidence Rail
  → local exports

Optional development-observability path

Local benchmark results
  → redacted BenchmarkTelemetrySink
  → W&B Runs, Tables, Artifacts, Registry, and Reports

Optional language-assistance path

User question
  → authorization check
  → read-only typed project tools
  → bounded LLM explanation
  → deterministic claim and citation validator
  → Evidence Rail-linked answer
  → optional redacted Weave trace and evaluation
```

This architecture keeps observability outside the scientific truth path. A W&B or Weave outage must not change search order, project state, hydraulic results, readiness, or export eligibility.

## 4. Complete the exact-simulation vertical slice

### 4.1 Add an `AnalysisOrchestrator`

The orchestrator should own the entire alternatives lifecycle:

1. Validate the project, baseline, storms, candidates, objectives, constraints, cost basis, QA policy, and simulation budget.
2. Create a deterministic initial search plan.
3. Persist every candidate vector and search event before execution.
4. Apply typed mutation commands to create each alternative model.
5. Reparse and validate every materialized model.
6. Compute the complete physics cache key.
7. Create one durable solver job for each design × required storm.
8. Execute jobs with bounded concurrency.
9. Preserve `.inp`, `.rpt`, `.out`, stdout metadata, and hashes as immutable artifacts.
10. Extract versioned hydraulic metrics.
11. Store QA findings and accepted metrics transactionally.
12. Scan the full network for adverse changes.
13. Compute costs, constraints, storm aggregations, and the exact nondominated set.
14. Emit ordered progress events to the desktop.
15. Complete, pause, cancel, fail, or recover the analysis without losing valid completed work.

`analysis.start` should acknowledge a durable state transition quickly. It should not remain an in-memory task or a long-lived request.

### 4.2 Build a versioned SWMM metric extractor

The initial extractor should support:

- Node peak depth and peak HGL.
- Node minimum freeboard where rim elevation is available.
- Flood volume, maximum flood rate, and flooding duration.
- Outfall peak flow and event volume.
- Surcharge duration.
- Link peak flow.
- Full-flow or capacity duration.
- Runoff and flow-routing continuity.
- Simulation start, end, routing step, and output interval.

Every stored value should carry:

```text
project ID
analysis ID
design ID
storm ID
solver job ID
object type and ID
metric definition and aggregation
value and units
input, dependency, output, and solver hashes
extractor and metric-schema versions
QA disposition
```

The extractor should read the official binary output or a pinned, verified API. Human-readable report parsing is useful for diagnostics but should not be the sole source for every time-series metric.

### 4.3 Enforce exact-result invariants

- No `ExactRunResult` without a succeeded exact solver job.
- No accepted metric before immutable run artifacts exist.
- No design-level result until every required storm has an accepted disposition.
- No result table or export value without a resolvable Evidence Rail record.
- No adaptive proposal may replace missing exact storms with predictions.
- Cost changes may rerank completed hydraulics without invalidating the physics cache.

### 4.4 Finish durable scheduling

One job is one design × one storm. The scheduler should provide:

- Transactional planning and queueing.
- Bounded worker concurrency.
- Idempotency keys for repeated commands.
- Design deduplication before queueing.
- Per-job isolated work directories.
- Process termination and timeout policy.
- Startup recovery for abandoned `RUNNING` jobs.
- Pause that stops new jobs while allowing active jobs to finish.
- Cancel that terminates active processes and preserves completed evidence.
- Ordered events with monotonic sequence numbers.
- Immutable completed physics-cache entries.

The plan screen should calculate the real budget:

```text
baseline jobs = required storms
candidate capacity = floor((job cap - baseline jobs) / required storms)
```

The UI should show both candidate count and total exact SWMM jobs so a user cannot confuse the two.

### 4.5 Package and verify the solver

- Bundle the allowed EPA SWMM 5.2.4 engine for each supported platform, or provide a signed installer-time acquisition process with exact checksum verification.
- Record the binary hash in every run and export.
- Test the packaged binary against EPA reference cases and repository fixtures.
- Invoke it without a shell and with a minimal environment.
- Document platform-specific floating-point tolerances instead of promising universal byte-for-byte metric equality.

EPA currently publishes SWMM 5.2.4 software, source, and an API guide, and describes SWMM as a planning and analysis tool for drainage and green-infrastructure strategies. See the [official EPA SWMM page](https://www.epa.gov/water-research/storm-water-management-model-swmm) and [EPA source repository](https://github.com/USEPA/Stormwater-Management-Model).

## 5. Connect the desktop to real project state

### 5.1 Remove simulated progress from professional paths

The Run screen should use durable worker events, not timers. It should show:

- Planned, queued, running, succeeded, QA-rejected, failed, and cached jobs.
- Designs complete across all storms.
- Exact jobs consumed and remaining.
- Search batch and method.
- Cache hits.
- Estimated remaining work based on observed local job durations, clearly labelled as an estimate.
- Pause, resume, and cancel behavior that matches the durable state machine.

Guided-example data can remain, but it must be visibly labelled `DEMO` and use a separate fixture mode.

### 5.2 Make frontend state disposable

The worker and SQLite database should remain authoritative. After a restart, the renderer should rebuild its view from:

```text
project.get_state
analysis.get_state
results.query
project event sequence
```

The UI must not infer scientific success from a completed animation, a Tauri acknowledgement, or a locally cached React object.

### 5.3 Generate and enforce contracts

- Generate JSON Schema from Pydantic contracts.
- Generate TypeScript types and Zod validators from the same schema versions.
- Commit or reproducibly build the generated artifacts used by the desktop.
- Validate requests, responses, events, saved files, and exported manifests.
- Add compatibility fixtures for migrations and older projects.

## 6. Improve the engineer’s workflow

### 6.1 Import and preflight

Group findings into plain-language actions:

- **Fix before running.**
- **Review before relying on results.**
- **Information.**

Each finding should identify:

- File and dependency.
- SWMM section and source line.
- Object ID and map location when available.
- Why the issue matters.
- The corrective action.
- Whether the application can prepare a reversible repair.
- The evidence required to dismiss or accept the issue.

Examples of useful messages:

> Conduit C-14 has a non-positive slope. Review its inlet and outlet inverts before running dynamic-wave hydraulics.

> Rainfall file `design_10yr.dat` was referenced but was not included in the project bundle. Select the file or remove the reference.

### 6.2 Run planning

Before execution, show:

- Baseline jobs.
- Candidate designs.
- Storms per design.
- Total exact jobs.
- Cache hits.
- Worker count.
- Estimated local storage.
- Search method and the reason it was chosen.
- Results that will be reportable at the current readiness level.
- Sensitivity cases, if any, as a separate budget.

The primary action should be explicit:

> Run 184 alternatives across 4 storms — 736 exact SWMM jobs

### 6.3 Candidate configuration

Add:

- Bulk candidate creation from selected subcatchments or GIS polygons.
- LID footprint and displaced-surface feasibility checks.
- A side-by-side view of immutable `[LID_CONTROLS]` and searchable `[LID_USAGE]` quantities.
- Organization-approved LID templates.
- Unit-aware validation.
- Semantic change previews.
- Undo for setup changes.
- Clear provenance for why each location was considered feasible.

The search variable remains the integer count of additional identical approved units. The application must not invent LID media, drains, soils, infiltration rates, or construction details.

### 6.4 Comparison workspace

Provide:

- Cost–performance trade-off plots with accessible tables.
- Baseline and alternative hydrographs.
- Storm-by-alternative matrices.
- Network maps of improvements and adverse effects.
- Full-network scans even when the engineer selected only a few reporting locations.
- A clear explanation of which objectives and constraints placed an option on the displayed frontier.
- A distinction between “nondominated among evaluated alternatives” and “best.”
- Direct opening of a selected materialized `.inp` in official EPA SWMM.
- Evidence Rail links on every number, QA badge, assumption, cost, and model change.

### 6.5 Exports and remediation

Export readiness should be computed from repository evidence. A user checkbox cannot override missing evidence.

Blocked exports should explain the exact reason and navigate to it:

> The professional evidence package is unavailable because three assumed pipe diameters remain unreviewed.

The complete export set should include:

- DOCX narrative report.
- XLSX comparison workbook.
- CSV and JSON machine-readable results.
- Selected alternative and baseline `.inp` files.
- Corresponding `.rpt` and `.out` files.
- Search trace.
- QA findings and accepted exceptions.
- Assumption and cost ledgers.
- Dependency, solver, schema, extractor, and template hashes.
- Readiness notices and limitations.

## 7. Complete the conceptual-model path without overstating it

### 7.1 Integrate SWMManywhere as an adapter

Use this flow:

```text
ConceptModelRecipe
  → approved user or provider data
  → normalized GeoParquet layers
  → pinned SWMManywhere execution
  → generated SWMM bundle
  → standard secure bundle import
  → standard validation and hashing
  → focused topology repair
  → assumption review
```

The concept builder must not bypass project import, validation, QA, provenance, or readiness gates.

[SWMManywhere](https://github.com/ImperialCollegeLondon/SWMManywhere) is an appropriate open adapter for synthetic urban drainage networks, but its own [extended example](https://imperialcollegelondon.github.io/SWMManywhere/notebooks/extended_demo/) shows implausible outlet choices, stranded pipes, and disconnected areas caused by source data and synthesis decisions. That supports the planned review workflow; it does not support automatic promotion to an engineer-reviewed model.

### 7.2 Add focused map repair

Support only the setup repairs needed to review a generated network:

- Confirm or move outlets.
- Connect stranded components.
- Remove implausible links.
- Add or delete a simple node or conduit.
- Edit diameter, invert, slope, length, and roughness.
- Reassign subcatchment outlets.
- Mark known versus synthetic infrastructure.
- Flag uncertain objects.
- Undo every edit.

Do not attempt to become a complete replacement for the official SWMM editor.

### 7.3 Guarantee provenance coverage

Every generated parameter should identify:

```text
value and units
source asset
source vintage, resolution, license, and checksum
derivation or formula
observed, imported, derived, defaulted, or manual origin
confirmed, inferred, or assumed confidence
reviewer and review status
```

Unknown pipes, inverts, infiltration properties, or hydrologic values must never be silently guessed. A selected conceptual default remains visible and blocks promotion until it is reviewed or explicitly accepted by an engineer.

### 7.4 Add conceptual-model benchmarks

Test:

- Repeat generation from an identical recipe.
- Offline generation from cached provider assets.
- Missing, inconsistent, and low-resolution layers.
- Outlet-selection errors.
- Stranded and disconnected networks.
- Manual repair followed by regeneration.
- Provenance coverage for every derived object.
- Permanent conceptual warnings in every surface and export.
- Structural comparison with known networks without claiming that synthesis recovered the real buried system.

## 8. Build a benchmark program before making optimization claims

### 8.1 Start with the closed 700-design universe

The CC0 fixture contains four candidate slots with cardinalities:

```text
5 × 4 × 5 × 7 = 700 designs
```

Run all 700 against the pinned fixture storm set. The resulting exact set gives the project a known frontier and makes search-quality claims falsifiable.

Compare:

- Exhaustive enumeration as ground truth.
- Uniform random sampling.
- Scrambled Sobol quasi-Monte Carlo.
- Integer NSGA-II.
- QMC plus ExtraTrees-assisted proposal.

Use the same model, storm set, solver, metric definitions, QA policy, job budgets, and declared seeds. Compare methods by exact SWMM jobs consumed, not optimizer iterations.

SciPy documents scrambled [Sobol sequences](https://docs.scipy.org/doc/scipy/reference/stats.qmc.html), including their power-of-two balance properties. Pymoo documents [NSGA-II](https://pymoo.org/algorithms/moo/nsga2.html). Recent research also continues to couple SWMM with multi-objective methods such as [MOEA/D](https://www.sciencedirect.com/science/article/pii/S0022169425003828). These establish that the techniques are modern and relevant; they do not prove that a particular implementation helps this product. The benchmark must do that.

### 8.2 Measure decision quality

Primary benchmark measures should be:

- Feasible alternatives discovered.
- Hypervolume relative to the exhaustive frontier.
- IGD+ or another declared frontier-distance measure.
- Jobs to the first feasible design.
- Jobs to 90% and 95% of exhaustive-frontier hypervolume.
- Recovery of low-cost and high-performance extremes.
- Stability across seeds.
- Solver failures and QA rejections.
- Cache-hit rate.
- Search overhead versus solver execution.
- CPU and elapsed runtime.
- Measured or explicitly estimated energy.

Prediction error is a diagnostic, not the product outcome. The important question is whether the method finds better exact engineering choices under the same simulation budget.

### 8.3 Publish benchmark assets

Each benchmark release should contain:

- Fixture manifest, license, and checksum.
- SWMM binary hash.
- Parser, metric, and objective-schema versions.
- Exact design table.
- Golden `.rpt` and `.out` files when redistribution is allowed.
- Exact nondominated frontier.
- Metric-specific tolerance policy.
- Search configurations, candidate order, batch boundaries, and seeds.
- Machine-readable results.
- Static charts and a human-readable report.
- Cross-platform differential report.

### 8.4 Gate adaptive ML

Adaptive search should remain off by default until it:

- Matches or exceeds QMC decision quality across multiple declared budgets and seeds.
- Shows a meaningful predeclared improvement on more than one network.
- Retains the required exploration reserve.
- Adds no hydraulic-result error because every reported result remains exact.
- Keeps proposer overhead small relative to simulation work.
- Reproduces the same barriered candidate trace from the same inputs, seed, and environment.

A negative benchmark is a valid result. If ExtraTrees does not beat QMC or integer NSGA-II reliably, the simpler method should remain the default.

## 9. Modernize search carefully

### 9.1 Keep simple methods first-class

Exhaustive enumeration and scrambled Sobol sampling are understandable, reproducible, and strong at small budgets. They should not be treated as temporary placeholders.

### 9.2 Add budget-terminated integer NSGA-II

Requirements:

- Integer-safe sampling, crossover, and mutation.
- The same canonical candidate vector used by other methods.
- Deduplication before solver planning.
- Exact evaluation against every storm.
- Deterministic population sorting at batch barriers.
- Recorded population, operator, seed, and termination configuration.
- The same exact job budget used by competing methods.

MOEA/D may be added later as a research comparator, not because it is newer, but only if a declared benchmark question justifies it.

### 9.3 Improve the adaptive proposer

The current ExtraTrees approach is reasonable for a small tabular, integer design space. Improve it with:

- Separate feasibility and solver-failure classifiers.
- Explicit objective and constraint treatment.
- Pareto-aware acquisition rather than a simple normalized weighted sum.
- Decision-space diversity so proposals do not cluster around nearly identical layouts.
- Barriered batches so parallel completion order cannot alter the next model fit.
- A fixed QMC exploration reserve.
- Recorded model, feature, acquisition, and training-data fingerprints.
- No use before the declared minimum number of unique, QA-usable exact designs.

Tree-to-tree dispersion may help rank proposals, but it must not be labelled “confidence,” “hydraulic uncertainty,” or “design certainty.”

### 9.4 Add assumption sensitivity as a separate workflow

For conceptual or assumption-heavy projects, allow the engineer to declare ranges or cases for parameters such as:

- Pipe diameter.
- Roughness.
- Invert elevation.
- Impervious percentage.
- Infiltration properties.
- LID maintenance performance.

Then answer:

> Does the preferred alternative remain useful across the input cases the engineer supplied?

Keep sensitivity jobs, objectives, and reports separate from the base alternatives run. Do not claim probabilistic risk unless the distributions and dependencies were professionally justified.

## 10. Use W&B to prove the search helps

### 10.1 Correct role

The strongest W&B use is scientific development and release benchmarking:

```text
Local benchmark database and artifacts = authority
W&B = optional comparison, lineage, collaboration, and reporting view
```

The term **W&B Sweep Agent** can be confusing. It is a worker that executes W&B hyperparameter-sweep jobs. It is not an LLM engineering agent, and it should not replace GI Scenario Studio’s storm-aware, cache-aware, durable solver scheduler.

### 10.2 W&B object mapping

| W&B object | GI Scenario Studio meaning |
|---|---|
| Project | A search-benchmark program, not a customer engineering project |
| Group | Fixture version + SWMM version + metric schema |
| Run | One fixture × search method × seed × job budget × software version |
| Run history step | Completed exact designs or exact SWMM jobs consumed |
| Table row | One exact evaluated design summary |
| Artifact | Public fixture, reference frontier, search trace, normalized results, proposer snapshot, or release report |
| Registry collection | An approved public benchmark, extractor test set, or proposer version that passed declared gates |
| Sweep | A developer study of proposer/search settings across public fixtures |
| Report | A reviewable benchmark narrative, accompanied by repository-owned static results |

Do not create one W&B Run for every SWMM subprocess. That would produce noisy tracking and blur the distinction between a scientific campaign and its individual exact jobs.

### 10.3 What each benchmark run should record

Configuration:

- Application commit and version.
- Benchmark and storm-suite hashes.
- SWMM binary hash.
- Parser, extractor, objective, and QA-policy versions.
- Search method, seed, and exact job budget.
- QMC, NSGA-II, or ExtraTrees configuration.
- Batch size and exploration allocation.
- Worker count, OS, architecture, and cache policy.

Progress and summary metrics:

- Hypervolume and IGD+ versus the exact frontier.
- Feasible alternatives discovered.
- First-feasible and target-hypervolume job counts.
- Extreme solutions recovered.
- Solver failures and QA rejections.
- Cache hits.
- Solver, search, CPU, and elapsed runtime.
- `EnergyObservation` value, boundary, and measured/estimated method.

W&B supports experiment tracking, [Tables](https://docs.wandb.ai/models/tables), versioned [Artifacts](https://docs.wandb.ai/models/artifacts), artifact [lineage](https://docs.wandb.ai/models/artifacts/explore-and-traverse-an-artifact-graph), [Registry](https://docs.wandb.ai/guides/core/registry/model_registry/link-model-version/), [Sweeps](https://docs.wandb.ai/models/sweeps), and [Reports](https://docs.wandb.ai/models/reports). These features fit benchmark development well.

### 10.4 Safe integration boundary

Add a provider-neutral interface:

```text
BenchmarkTelemetrySink
  ├── DisabledTelemetrySink
  ├── LocalTelemetrySink
  └── WandBTelemetrySink
```

The W&B adapter should:

- Receive only a deliberately minimized benchmark projection.
- Run outside every SWMM subprocess.
- Log only after authoritative local commits succeed.
- Never block, fail, or alter hydraulic execution.
- Use deterministic campaign IDs for resumable benchmarks.
- Retain portable local JSON/Parquet results.
- Treat synchronization as a separate, explicit operation.
- Use explicit artifact versions or digests, not a floating `latest` alias, for release evidence.

W&B’s programmatic Reports/Workspaces interface is documented as preview functionality, so every release should still create repository-owned Markdown, JSON, CSV/Parquet, and static figures.

### 10.5 Appropriate W&B uses

- Compare random, QMC, NSGA-II, and adaptive proposal under equal exact budgets.
- Diagnose whether the proposer generalizes across held-out public networks.
- Track resource and energy observations.
- Detect benchmark regressions between releases.
- Version redistributable fixtures and exact reference results.
- Publish a frozen, reviewable experiment report alongside static evidence.
- Tune development parameters such as tree count, leaf size, exploration fraction, activation threshold, and batch size.

### 10.6 Inappropriate W&B uses

Do not use W&B to:

- Calculate or hold the only copy of a hydraulic result.
- Schedule production SWMM alternatives.
- Replace project SQLite, cache keys, manifests, or the Evidence Rail.
- Upload private `.inp`, `.out`, GIS, rainfall, coordinates, asset IDs, paths, or logs by default.
- Promote readiness or accept QA.
- Train a global surrogate on unrelated customer projects without a separate, explicit data agreement.
- Present a surrogate prediction as a SWMM result.
- Infer energy or carbon from CPU utilization alone.

### 10.7 Offline and privacy behavior

W&B documents `online`, `offline`, and `disabled` modes. [`offline`](https://docs.wandb.ai/models/track/environment-variables) stores run metadata locally for possible later synchronization; it is not a replacement for the permanent local Evidence Rail.

For professional projects:

- Default W&B to disabled.
- Require informed consent and a field preview before enabling synchronization.
- Disable console, code, git, and error-report capture where sensitive content might leak.
- Prefer public/CC0 fixtures for normal benchmark tracking.
- Treat apparently harmless network metrics as potentially sensitive infrastructure information.

W&B documents [reference artifacts and artifact privacy](https://docs.wandb.ai/models/artifacts/data-privacy-and-compliance), Dedicated Cloud, and [Self-Managed deployment](https://docs.wandb.ai/platform/hosting/hosting-options/self-managed). These may support an enterprise review, but they are not MVP desktop dependencies. A bring-your-own bucket does not automatically mean that every item of metadata stays local.

## 11. Add one bounded LLM Evidence Assistant

### 11.1 Product role

A useful name is **Evidence Assistant** or **Project Guide**, not “AI engineer.”

Its purpose is:

> Make the existing engineering evidence easier to find, understand, compare, and document.

It does not calculate hydraulics, choose physical parameters, approve designs, or replace professional judgment.

### 11.2 High-value workflows

#### Explain import and validation findings

Examples:

- “Why can’t this model run?”
- “What does this continuity warning mean in this project?”
- “Show me the conduits with non-positive slope.”
- “Which missing dependency is blocking the baseline?”

The assistant should retrieve structured findings, cite the affected file/section/object, and link to curated, versioned EPA documentation where relevant.

#### Review the assumption ledger

Examples:

- “Which parameters are still assumed?”
- “What blocks engineer-reviewed readiness?”
- “Which generated objects have no confirmed source?”

It may summarize or prioritize. It may not accept an assumption, fill a missing value, or promote readiness.

#### Guide analysis setup

Examples:

- Explain storm aggregation and job-budget consequences.
- Translate an engineer’s stated requirement into a draft `MetricSpec`.
- Prepare a run-plan preview.
- Navigate to missing cost-basis fields.

The deterministic application validates object IDs, units, thresholds, and schemas. The engineer confirms the plan and starts the analysis.

#### Explain exact alternatives

Examples:

- “Why is ALT-018 on the displayed frontier?”
- “Which storm controls this option?”
- “Where did conditions become worse?”
- “What changed from the baseline model?”

The assistant reads exact metrics, exact costs, QA findings, and semantic diffs. It cannot infer missing hydraulic behavior.

#### Draft evidence-linked language

It can draft:

- Executive summaries.
- Methods descriptions.
- Alternatives-screening narratives.
- Assumption and limitation summaries.
- QA-resolution notes.
- Plain-language explanations for meetings.

Every numerical claim should be inserted from typed tool results and carry an internal evidence reference. Drafts remain subject to engineer review.

#### Explain redacted diagnostics

With explicit user selection, the assistant may help interpret worker, parser, or solver errors from a redacted diagnostic bundle. Raw client models and GIS data remain excluded by default.

### 11.3 Start with one assistant, not a multi-agent system

One bounded assistant with typed tools is easier to understand, evaluate, secure, and maintain than a group of agents debating engineering decisions.

If an independent checker is needed, make the first checker deterministic application code. A second LLM critic may be useful in offline evaluations, but it should not become the authority for numbers, readiness, permissions, or QA.

### 11.4 Read-only tool surface

Initial tools should be narrow and read-only:

```text
project.get_summary
validation.list_findings
model.get_object
provenance.list_assumptions
readiness.get_checklist
storm.list_cases
analysis.get_configuration
analysis.get_status
results.get_exact_metric
results.compare_exact
evidence.resolve
cost.get_basis
docs.search_curated
ui.prepare_navigation
```

The assistant receives no arbitrary SQL, filesystem, network, shell, Python, or raw SWMM-editing capability.

If the user requests a change, the assistant returns a proposal:

```json
{
  "action": "review_assumed_pipe_diameter",
  "object_ids": ["C-14"],
  "reason": "Diameter is marked assumed and blocks engineer review.",
  "evidence_ids": ["finding:81", "provenance:C-14:diameter"]
}
```

The normal application presents the structured diff or review form. Only a confirmed user action calls the deterministic domain service.

### 11.5 Structured response contract

Require:

```text
answer
evidence references
numeric claims
assumptions used
limitations
proposed actions
requires engineer confirmation
```

Each numeric engineering claim should carry:

```text
value
units
evidence ID
run ID or source ID
```

A deterministic postprocessor compares the claim with the retrieved tool result. An unsupported number blocks the answer instead of merely adding a warning.

### 11.6 Actions the assistant must never perform

- Generate or predict reportable hydraulic values.
- Guess pipe, soil, infiltration, LID, or cost parameters.
- Directly edit a SWMM model.
- Promote readiness or sign an attestation.
- Accept a QA exception.
- Mark a source authoritative.
- Approve a cost basis.
- Suppress an adverse effect.
- Start a costly analysis without explicit confirmation.
- Finalize or sign a professional export.
- Claim calibration, regulatory acceptance, design adequacy, global optimality, or real-world flood protection.
- Treat model comments, uploaded documents, or retrieved text as instructions to change its permissions.

### 11.7 Provider and offline model

Add a provider-neutral interface:

```text
AssistantProvider
  ├── DisabledProvider
  ├── LocalModelProvider
  └── ApprovedCloudProvider
```

The core application must work without an assistant. A local model may support offline or sensitive work. A cloud model requires a clear disclosure of the exact data leaving the machine.

Use a small model and minimal retrieved context for routine explanations. A larger model may be offered for longer drafting only when the user approves the provider and data transfer. Cache answers by prompt, evidence, policy, and model-version hash. Do not run autonomous background loops.

## 12. Use Weave to test and observe the assistant

### 12.1 Correct role

[W&B Weave](https://docs.wandb.ai/weave) is an observability and evaluation platform for LLM applications. It can trace custom functions and integrations with agent frameworks, but it is not itself the engineering agent or hydraulic runtime.

Weave can help answer:

- Which evidence did the assistant retrieve?
- Which tools did it call?
- Did it copy numbers and units correctly?
- Did it attempt a forbidden action?
- How did a prompt, model, or retrieval change affect quality?
- What were latency, token use, and cost?

Do not depend on Weave’s experimental Serve or Deploy utilities for the product. The official [Tools and Utilities page](https://weave-docs.wandb.ai/guides/tools/) describes them as early alpha.

### 12.2 Trace structure

Wrap the assistant and tool calls as Weave operations:

```text
answer_engineering_question
  ├── authorize_intent
  ├── retrieve_project_context
  ├── evidence.resolve
  ├── results.compare_exact
  ├── llm.generate_draft
  └── validate_claims_and_permissions
```

Record:

- Prompt, policy, model, and provider versions.
- Tool-schema versions.
- Tool calls, arguments, errors, and latency.
- Evidence IDs retrieved.
- Claim-validator and permission-validator outcomes.
- Token use and cost when supported.
- User or engineer feedback when explicitly supplied.

Weave documents nested [traces and operations](https://docs.wandb.ai/weave/guides/tracking/tracing), trace plots for [latency, cost, and token use](https://weave-docs.wandb.ai/guides/tracking/trace-plots/), and integrations that can trace tool-using agents. This makes it useful for evaluation, not authority.

### 12.3 Build a versioned evaluation dataset

Use redistributable fixtures and engineer-reviewed examples covering:

- Common onboarding questions.
- Import and dependency errors.
- Ambiguous SWMM warnings.
- Readiness questions.
- Assumption-ledger gaps.
- Exact-result comparisons.
- Unit-conversion traps.
- Cost-basis omissions.
- Conceptual-model limitations.
- Requests for guaranteed or regulator-approved claims.
- Requests to guess missing parameters.
- Requests to hide adverse effects.
- Fabricated object IDs or storm cases.
- Prompt injection in model comments and uploaded text.
- Questions whose answer is genuinely unavailable.

Weave supports versioned [Datasets](https://docs.wandb.ai/weave/guides/core-types/datasets), [Evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations), and custom [Scorers](https://docs.wandb.ai/weave/guides/evaluation/scorers).

### 12.4 Use deterministic scorers as release gates

Primary scorers:

- Every numerical claim exactly matches retrieved evidence.
- Units match the underlying contract.
- Every evidence ID resolves locally.
- Required readiness and conceptual-model notices are present.
- No prohibited product claim appears.
- No unauthorized tool was called.
- No model object, storm, source, or result was invented.
- Insufficient evidence causes an explicit abstention.
- Tool arguments validate against their JSON Schemas.
- Required limitations are not omitted.
- The requested deep link points to the correct object or finding.
- Retrieved project content was treated as untrusted data, not instructions.

Use practicing-engineer annotation for:

- Helpfulness.
- Clarity.
- Appropriate detail.
- Whether the next action is practical.
- Whether the explanation could mislead a non-specialist.

An LLM judge may supplement style and broad-helpfulness assessment. It must never be the sole arbiter of hydraulic accuracy, authorization, readiness, or evidence validity.

### 12.5 Assistant release gate

Do not enable the assistant in a professional workflow until the declared evaluation set shows:

- No invented hydraulic metrics.
- Every numeric engineering claim linked to valid evidence.
- Correct refusals for certification, parameter guessing, QA approval, and readiness promotion.
- No unauthorized mutation or analysis execution.
- Resistance to instructions embedded in project content.
- Correct conceptual-model limitations.
- Graceful abstention when information is unavailable.
- No effect on simulation, comparison, Evidence Rail, or export when disabled or unavailable.

### 12.6 Guardrails and monitoring

Hard controls belong in local application code:

- Reject a claimed hydraulic value without an exact result.
- Reject nonexistent evidence IDs.
- Reject assistant-originated readiness changes.
- Reject omitted conceptual warnings.
- Reject unsupported units or objects.
- Reject assistant-generated direct mutations.

Weave [guardrails](https://docs.wandb.ai/weave/guides/evaluation/guardrails) and monitors may add observability, but no cloud scorer should be part of the safety-critical path. Log the local validator’s outcome to Weave rather than delegating the decision to Weave.

### 12.7 Privacy and offline nuance

A trace can contain prompts, outputs, retrieved documents, tool inputs, code, and paths. Treat Weave tracing as an explicit data disclosure.

- Keep LLM assistance and Weave tracing off by default.
- Obtain per-project consent before transmitting a trace.
- Send allowlisted evidence fields rather than raw `.inp`, `.out`, GIS, rainfall, or client files.
- Remove client names, usernames, absolute paths, coordinates, asset descriptions, and sensitive internal IDs.
- Use stable pseudonyms or hashes where correlation is needed.
- Do not rely only on PII detection; infrastructure data can be confidential without containing PII.
- Disable source-code or input/output capture where appropriate.

Weave provides [input/output postprocessing and PII redaction](https://docs.wandb.ai/weave/guides/tracking/redact-pii), but redaction is a defense in depth, not permission to upload a project.

Weave documents multi-tenant, dedicated, and [self-managed deployment](https://docs.wandb.ai/weave/guides/platform/weave-self-managed). Its write-ahead log improves delivery resilience, but the documentation does not present it as a complete local/offline trace application. When Weave is unavailable, disable assistant calls and keep deterministic Evidence Rail navigation available; do not generate an untraced local answer.

## 13. Security, privacy, and data governance improvements

### 13.1 Classify project data

Add per-project classifications such as:

```text
PUBLIC_FIXTURE
INTERNAL
CLIENT_CONFIDENTIAL
CRITICAL_INFRASTRUCTURE_RESTRICTED
```

Use classification to set safe defaults for diagnostics, open-data access, W&B, Weave, cloud LLMs, and support bundles.

### 13.2 Preview every external disclosure

Before a cloud integration, show:

- Provider and deployment.
- Exact categories of data being sent.
- Whether prompts, outputs, files, metrics, code, or paths are included.
- Retention and deletion policy links.
- The project’s classification and any conflict.
- A local-only alternative.

Consent should be scoped and revocable. Enabling open-data downloads should not silently enable W&B, Weave, an LLM provider, or diagnostic upload.

### 13.3 Defend the assistant from project-content injection

- Treat comments, object names, reports, uploaded text, and retrieved documents as untrusted data.
- Never derive tool permissions from retrieved text.
- Keep system policy and tool allowlists outside the model context that project content can modify.
- Validate all tool arguments and evidence IDs.
- Include injection cases in every evaluation release.

### 13.4 Preserve existing platform security

Continue to enforce:

- Archive path normalization and size/file-count limits.
- Rejection of symlinks, devices, executables, and paths outside project roots.
- No shell invocation for SWMM.
- Minimal process environments.
- Tauri capability allowlists.
- Local redacted diagnostics by default.
- Signed updates, dependency locks, SBOMs, and license checks.

## 14. Environmentally conscious computing improvements

The environmentally responsible feature is not a green-themed interface. It is avoiding unnecessary exact runs and unnecessary model calls while reporting resource evidence honestly.

### 14.1 Simulation efficiency

- Use immutable physics caching.
- Deduplicate design vectors before queueing.
- Reuse hydraulics when only cost assumptions change.
- Apply explicit solver-job, worker, storage, and resource caps.
- Use bounded concurrency rather than maximum parallelism by default.
- Stop ineffective searches at the declared budget.
- Compare adaptive search with simple baselines before claiming saved simulations.

### 14.2 LLM efficiency

- Make assistance user-triggered, not always-on.
- Use a small model for routine explanation.
- Retrieve only the evidence needed for the question.
- Cache answers by prompt + evidence + policy + model hash.
- Avoid autonomous multi-agent loops.
- Cap tokens, tool calls, and retries.
- Sample expensive judge-based monitoring instead of evaluating every request with another LLM.

### 14.3 Honest energy records

Keep the `EnergyObservation` contract and distinguish:

- Measured wall-plug or hardware-counter energy.
- Provider-reported energy.
- Estimated energy from a documented method.
- Resource proxies such as CPU time, utilization, and model tokens.

W&B can visualize system metrics and logged energy observations. It does not transform CPU utilization into a measured carbon footprint. Report the boundary, hardware, method, and uncertainty.

## 15. Verification and acceptance improvements

### 15.1 Exact-simulation acceptance

- A real project runs from the desktop without developer intervention.
- Every alternative is evaluated against every required storm.
- Every displayed value resolves to a succeeded exact job and immutable evidence.
- A selected alternative reopens and runs in official EPA SWMM.
- Restart, pause, resume, cancel, cache, and recovery behave as documented.
- Changing cost-only data does not trigger hydraulic reruns.
- Full-network adverse effects are scanned and visible.

### 15.2 Benchmark acceptance

- The 700-design fixture has a complete exact result table and frontier.
- A public command reproduces each benchmark without W&B.
- Algorithms use equal exact-job budgets and fixed declared seeds.
- Release results include static machine- and human-readable evidence.
- Adaptive ML remains disabled when it does not beat simpler methods under the declared gates.

### 15.3 Conceptual-model acceptance

- A user can create, repair, review, simulate, and export a conceptual screening model.
- The user understands that it is not a representation of the real buried system.
- Every derived object and parameter has provenance.
- Permanent conceptual warnings appear in the app, charts, workbooks, reports, and model description.
- Structural checks do not automatically promote readiness.

### 15.4 UX acceptance

- A new user completes the guided example without coaching.
- A practicing engineer imports a real model, fixes preflight findings, runs alternatives, compares them, and exports evidence without developer intervention.
- Every blocking message states a corrective action.
- All quantities show units.
- Charts have accessible table equivalents.
- Keyboard-only and WCAG 2.2 AA workflows pass.
- The previous project and screen restore after restart.

### 15.5 Assistant acceptance

- The core product is fully functional with the assistant and tracing disabled.
- The assistant cannot alter readiness, QA, models, costs, or execution.
- Numeric-claim and evidence validators block unsupported output.
- Professional engineers review the evaluation set and failure cases.
- Cloud disclosure and consent are clear and testable.

## 16. Recommended implementation order

This is an order of dependency, not a calendar estimate.

1. Complete design × storm execution and versioned metric extraction.
2. Connect authoritative results, progress, QA, and Evidence Rail data to the desktop.
3. Complete professional exports and deterministic manifests.
4. Build the exhaustive 700-design benchmark and public benchmark runner.
5. Add optional W&B tracking for public benchmark campaigns.
6. Implement and compare integer NSGA-II and improved barriered adaptive proposal.
7. Improve preflight, run planning, candidate editing, comparison, and export remediation.
8. Integrate pinned SWMManywhere and focused map repair.
9. Add assumption sensitivity as a separate exact workflow.
10. Build one read-only “Explain this finding” Evidence Assistant interaction.
11. Add Weave traces and deterministic evaluations on minimized public test data.
12. Expand the assistant to exact-result explanation and evidence-linked drafting only after it passes its gates.
13. Consider proposal-producing tools only after the read-only assistant has strong longitudinal evidence.

The sequencing principle is:

> First make every simulation real and traceable. Then prove the search helps. Only then add an LLM that makes the evidence easier to understand.

## 17. What not to add to this MVP

- A generic chatbot with the whole project dropped into its context.
- LLM-generated hydraulic values or missing physical parameters.
- Direct agent access to SQL, filesystem, shell, Python, or SWMM text mutation.
- Multi-agent debates over engineering approval.
- A global surrogate trained on private customer projects.
- W&B as the production scheduler or only result store.
- Weave Serve/Deploy as a production dependency while documented as experimental.
- Automatic calibration without a separate scientific and professional specification.
- A full SWMM desktop editor.
- 2D flood modeling before the 1D exact workflow is complete.
- Regulatory, filing, resilience, calibration, or global-optimum claims that the evidence does not support.
- Carbon claims inferred from weak resource proxies.

## 18. Recommended public language

For the product:

> GI Scenario Studio is an alternatives-analysis workspace for EPA SWMM. It uses exact simulations to compare green-infrastructure strategies and may use machine learning to prioritize which alternatives to simulate next.

For the current development state:

> GI Scenario Studio is under development. Its goal is to connect reproducible EPA SWMM runs, model-readiness checks, alternatives search, and evidence-linked reporting in one desktop workflow.

For the assistant:

> The Evidence Assistant explains project records and exact simulation results. It does not calculate hydraulics, choose engineering parameters, or approve a design.

For W&B and Weave:

> W&B helps the development team test whether the search method finds better exact alternatives under the same simulation budget. Weave helps test whether the optional assistant remains accurate, sourced, and within its allowed role. Neither system produces the engineering results.

## 19. Primary research and documentation

### Hydraulic engine and conceptual models

- [EPA Storm Water Management Model](https://www.epa.gov/water-research/storm-water-management-model-swmm)
- [EPA SWMM source repository](https://github.com/USEPA/Stormwater-Management-Model)
- [SWMManywhere repository](https://github.com/ImperialCollegeLondon/SWMManywhere)
- [SWMManywhere extended example and documented synthesis defects](https://imperialcollegelondon.github.io/SWMManywhere/notebooks/extended_demo/)

### Search and optimization

- [SciPy quasi-Monte Carlo documentation](https://docs.scipy.org/doc/scipy/reference/stats.qmc.html)
- [Pymoo NSGA-II documentation](https://pymoo.org/algorithms/moo/nsga2.html)
- [Coupled SWMM–MOEA/D research](https://www.sciencedirect.com/science/article/pii/S0022169425003828)

### Weights & Biases

- [W&B experiment tracking](https://docs.wandb.ai/models/track)
- [W&B Tables](https://docs.wandb.ai/models/tables)
- [W&B Artifacts](https://docs.wandb.ai/models/artifacts)
- [W&B artifact lineage](https://docs.wandb.ai/models/artifacts/explore-and-traverse-an-artifact-graph)
- [W&B Registry](https://docs.wandb.ai/guides/core/registry/model_registry/link-model-version/)
- [W&B Sweeps](https://docs.wandb.ai/models/sweeps)
- [W&B Reports](https://docs.wandb.ai/models/reports)
- [W&B environment and offline settings](https://docs.wandb.ai/models/track/environment-variables)
- [W&B artifact privacy and reference artifacts](https://docs.wandb.ai/models/artifacts/data-privacy-and-compliance)
- [W&B deployment options](https://docs.wandb.ai/platform/hosting/hosting-options)

### Weave and LLM evaluation

- [W&B Weave overview](https://docs.wandb.ai/weave)
- [Weave tracing](https://docs.wandb.ai/weave/guides/tracking/tracing)
- [Weave evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations)
- [Weave datasets](https://docs.wandb.ai/weave/guides/core-types/datasets)
- [Weave scorers](https://docs.wandb.ai/weave/guides/evaluation/scorers)
- [Weave trace plots](https://weave-docs.wandb.ai/guides/tracking/trace-plots/)
- [Weave PII redaction](https://docs.wandb.ai/weave/guides/tracking/redact-pii)
- [Weave deployment and security](https://docs.wandb.ai/weave/guides/platform)
- [Self-managed Weave](https://docs.wandb.ai/weave/guides/platform/weave-self-managed)
- [Weave experimental tools](https://weave-docs.wandb.ai/guides/tools/)

## Final recommendation

W&B and Weave are worth adding only if they strengthen evidence rather than complicate the product story.

The defensible architecture is:

> W&B measures whether the optimization strategy actually finds better exact alternatives under the same SWMM budget. Weave measures whether the optional language assistant explains those alternatives accurately, safely, and usefully. SWMM remains the source of hydraulic results, the local database remains the evidence authority, and the engineer remains the decision-maker.
