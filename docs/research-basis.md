# Research basis and claim boundaries

GI Scenario Studio is deliberately narrower than a general stormwater design
platform. The category already includes public and commercial planning tools;
the product differentiates through a user-owned Restricted W&B evidence mirror,
local engineering authority, transparent exact execution, open schemas, and
evidence that can be independently rerun.

## Primary references

- [EPA SWMM](https://www.epa.gov/water-research/storm-water-management-model-swmm)
  defines the supported public-domain simulator and eight native LID types.
- [SWMM 5.2 user manual](https://www.epa.gov/system/files/documents/2022-04/swmm-users-manual-version-5.2.pdf)
  is normative for input, LID deployment, numerical diagnostics, and reports.
- [EPA GI modeling toolkit](https://www.epa.gov/water-research/green-infrastructure-modeling-toolkit)
  shows that optimization and life-cycle comparison are established categories.
- [EPA environmental benefits of green infrastructure](https://www.epa.gov/green-infrastructure/environmental-benefits-green-infrastructure)
  identifies potential benefits such as runoff capture, water-quality
  improvement, groundwater recharge where site conditions allow, heat-island
  reduction, and habitat connectivity. It does not establish that a particular
  project delivers those outcomes.
- [EPA groundwater-protection guidance](https://www.epa.gov/green-infrastructure/green-infrastructure-and-groundwater-protection)
  makes recharge and groundwater quality conditional on soils, water-table
  separation, pollutants, siting, pretreatment, design, and monitoring.
- [EPA CSO LTCP review](https://www.epa.gov/system/files/documents/2022-06/LTCP%20Review%20Fact%20Sheet.pdf)
  supports alternatives and cost/performance analysis in that specific context;
  the application does not generalize this into a universal MS4 mandate.
- [NOAA Atlas 15](https://water.noaa.gov/about/atlas15) is tracked as a data
  source, not treated as an adopted standard before final publication.
- [SWMManywhere](https://github.com/ImperialCollegeLondon/SWMManywhere) is the
  optional BSD-licensed concept-model synthesis adapter. Its output retains
  `project_kind: conceptual`; a separate Model Fitness Passport remains
  `not_assessed` until an engineer reviews the stated-use evidence.

## Ecological and life-cycle boundary

Cedar Trace is a synthetic hydraulic teaching study, not an ecological impact
assessment or life-cycle assessment. Its exact SWMM records can establish what
the model calculated under the checked-in assumptions. They cannot establish
an outcome that the study did not observe or inventory:

- **Water quality:** no pollutant buildup, washoff, treatment performance, or
  receiving-water samples are modeled or measured.
- **Groundwater recharge:** SWMM `infiltration loss` is a runoff-accounting
  quantity, not a field measurement of recharge. Cedar Trace has no monitored
  subsurface flow, water table, soil chemistry, or groundwater quality.
- **Habitat and heat:** there is no habitat survey, vegetation-performance
  record, biodiversity observation, or air/surface-temperature measurement.
- **Real flood risk:** the storms are synthetic and have no authoritative
  recurrence interval. The model is not calibrated to observed events and does
  not estimate property damage or avoided flood probability.
- **Embodied and operational impact:** there is no bill of materials,
  construction method, transport distance, maintenance schedule, replacement
  cycle, end-of-life scenario, or embodied-carbon inventory.
- **Computational impact:** exact-job equivalents count simulation requests;
  they are not electricity measurements. No power telemetry, facility
  efficiency, grid carbon intensity, or hardware-life-cycle inventory was
  collected.

The distinction matters because a hydraulically attractive alternative can
still have material, maintenance, water-quality, siting, or ecological burdens.
[Flynn and Traver](https://doi.org/10.1016/j.ecoleng.2013.01.004) use a
cradle-to-grave LCA for one bio-infiltration rain garden and account for
construction, operation, and decommissioning. A
[Cincinnati rain-garden study](https://doi.org/10.1111/1752-1688.12320)
compares green and grey systems with explicit environmental and cost
boundaries. [O'Sullivan et al.](https://doi.org/10.1016/j.jenvman.2014.10.025)
show how materials, construction, transport, operation, maintenance, and the
chosen functional unit shape stormwater-treatment LCA results. These primary
studies support maintaining separate hydraulic, water-quality, ecological,
implementation, and computational ledgers. Their site-specific results do not
transfer to Cedar Trace and do not turn modeled runoff changes into measured
ecological benefit or carbon savings.

## Why this workflow is scientifically defensible

The product does not depend on a paper saying that one algorithm or one green
infrastructure practice is universally best. The literature supports the
structure of the question:

- SWMM provides a reproducible rainfall-runoff and hydraulic calculation under
  declared model assumptions. Its manual also makes calibration data and
  continuity diagnostics distinct concerns; successful execution is therefore
  not treated as proof of field fitness.
- Stormwater alternatives have more than one objective. Studies by
  [Eckart, McPhee, and Bolisetti](https://doi.org/10.1016/j.jhydrol.2018.04.068)
  and [Giacomoni and Joseph](https://doi.org/10.1061/%28ASCE%29WR.1943-5452.0000812)
  couple SWMM with multiobjective search to examine cost, volume, peak flow,
  and placement tradeoffs. Their site-specific results do not transfer to
  Cedar Trace or to a practitioner's model.
- [Fadhel, Rico-Ramirez, and Han](https://doi.org/10.1016/j.jhydrol.2018.03.041)
  show why rainfall timing can change modeled sewer peak response. This
  supports retaining storm-specific guardrails instead of collapsing all
  storms into one score; it does not make the synthetic Cedar Trace storms
  authoritative design events.
- [Rosa, Clausen, and Dietz](https://doi.org/10.1111/jawr.12272) demonstrate
  calibration and verification work for SWMM LID simulations. This supports
  the application's separation of execution QA from a Model Fitness Passport.
- [Rodriguez, Cavadini, and Cook](https://doi.org/10.1016/j.watres.2024.121284)
  test five Fehraltorf model structures over thirty years and show that modeled
  CSO and green-infrastructure conclusions depend materially on whether
  evapotranspiration, boundary inflow, groundwater, and their interactions are
  represented. The associated [CC0 data package](https://doi.org/10.25678/0009PE)
  supports the article's hash-pinned published-output reanalysis. Missing
  licensed meteorological forcing prevents us from calling that reanalysis a
  fresh SWMM reproduction, and the retained report QA remains visible.
- [Deb et al.](https://doi.org/10.1109/4235.996017) define NSGA-II, and
  [Zitzler et al.](https://doi.org/10.1109/TEVC.2003.810758) show why one scalar
  quality indicator is not enough to characterize multiobjective search. The
  benchmark therefore reports hypervolume, epsilon, feasible recall, adverse
  recall, and seed variation rather than declaring a winner from one curve.
- [Geurts, Ernst, and Wehenkel](https://doi.org/10.1007/s10994-006-6226-1)
  establish ExtraTrees as a randomized tree ensemble. A
  [surrogate-assisted LID study](https://doi.org/10.1016/j.jhydrol.2022.128848)
  motivates testing such proposers, but neither source licenses predicted
  hydraulics as engineering evidence. Every displayed hydraulic result still
  comes from an exact stored or newly executed SWMM run.

## Cedar Trace benchmark result

The completed benchmark uses one immutable synthetic universe: 700 vectors,
four storms, and 2,800 direct SWMM result records. Four search methods were
replayed at three budgets and ten seeds, producing 120 deterministic search
traces. An unseen vector's stored outcome was revealed only after the method
requested it, and each request was charged as four exact-job equivalents.

The complete universe contains 12 alternatives that satisfy all four teaching
rules, 421 alternatives with a positive outlet-peak change in at least one
selected storm, and four alternatives on the exact feasible cost-volume
frontier. At a 256-design budget, Sobol + ExtraTrees reached median hypervolume
1.000, feasible recall 0.833, and adverse recall 0.462. It still failed the
predeclared production gate, so the evidence supports only this conclusion:
adaptive ML remains experimental and scrambled Sobol remains the production
default.

The declared 95% adverse-recall gate cannot be met at that budget. Recovering
95% of 421 adverse alternatives requires at least 400 distinct adverse
requests, while the budget permits 256 requests total. This is a defect in the
gate, not evidence for or against ExtraTrees. A revised gate must be
budget-feasible, fixed before outcomes are inspected, and evaluated across at
least three public networks.

The replay performed no new hydraulic executions and recorded no measured
energy. Exact-job equivalents are a fair simulation-budget accounting unit,
not an energy or carbon measurement. [Green Algorithms](https://doi.org/10.1002/advs.202100707)
and [Henderson et al.](https://jmlr.org/papers/v21/20-312.html) support reporting
runtime, hardware, memory, location, and measurement method before making a
computational-footprint claim. Until those observations exist, the product
makes no “carbon saved” statement.

## Evidence Guide release-gate result

The current decision is based on an architecture-first v2 protocol, while the
earlier v1 studies remain immutable historical development evidence. None of
these experiments measures hydraulic accuracy, ecological outcomes, or whether
practitioners find the explanations useful.

### Historical v1 evidence

The v1 candidate-selection campaign exercised 48 schema-constrained cases per
model, or 96 logical case invocations in total. It tested whether a model could
select only repository-owned evidence and limitation IDs for a deterministic
renderer; it was not the multi-turn, tool-using product runtime. Weave recorded
the already-produced outputs and deterministic scores; it did not execute the
models or scorers. [Weave Evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations)
and the [EvaluationLogger](https://docs.wandb.ai/weave/guides/evaluation/evaluation_logger)
provide the evaluation record, not a guarantee that an answer is correct.

- `ibm-granite/granite-4.1-8b` passed 3 of 48 complete cases. Forty-five cases
  produced scorable output: all 45 used valid evidence IDs, while numeric/unit
  fidelity was 93.75%, evidence coverage 93.75%, required-limitation coverage
  10.42%, and read-only/action compliance 39.58%. It entered refusal mode for
  all eight refusal cases but completed safe abstention in only two.
- `openai/gpt-oss-20b` passed 0 of 48 complete cases. Eighteen cases produced
  scorable output and 30 ended in provider failure: all 18 produced outputs
  used valid evidence IDs, while numeric/unit fidelity was 37.50%, evidence
  coverage 35.42%, required-limitation coverage 2.08%, and read-only/action
  compliance 20.83%. It entered refusal mode for six of eight refusal cases
  and completed safe abstention in none.

Valid IDs on produced outputs are therefore not equivalent to complete evidence
coverage. The campaign reported 81,720 provider tokens, but no practitioner
usefulness study or controlled practitioner approval was performed. It also
recorded no power, energy, facility-efficiency, or carbon-intensity data, so the
token count supports neither an energy claim nor a carbon claim.

The separate v1 runtime probe exercised the actual multi-turn agent path: three
turns in two conversations, using the read-only evidence tools and the
[Weave Agents trace structure](https://docs.wandb.ai/weave/guides/tracking/trace-agents).
All three turns were acknowledged and readable in Weave, all three showed tool
activity, all three failed deterministic validation, and no generated answer
was released. The `ALT-017` follow-up remained in its original conversation;
the `ALT-207` question began a new one. The rejected turns exposed three
different controls: invalid next-check selection, a generated number that was
not repository-owned, and an invalid supporting claim.

The exact cloud parent relationship between each tool call and its routing LLM
span remains unverified. Weave SDK 0.53.4 exposed a non-root parent for each tool
but omitted the routing-only chat parent from the Agents span/turn projection.
The local regression proves that tools execute while the router context is
active; it does not substitute for cloud-parentage evidence. This limitation is
preserved in the probe receipt rather than inferred away.

### Architecture-first v2 result

The v2 study tightened the comparison: the deterministic `D1` guide is allowed
to win, production-eligible model arms may select only a finite explanation
plan, and repository code owns all facts, units, limitations, and actions. Its
public corpus includes 120 development and 120 project-family-separated
validation scenarios. A 300-scenario hidden test was specified, but no
access-controlled hidden corpus is currently available or sealed. The public
hidden receipt is explicitly unsealed and release-ineligible; it is not
evidence that hidden rows exist elsewhere.

The completed R1 capability screen contributed 160 inherited protocol attempts.
That screen established schema and tool compatibility and retained
`ibm-granite/granite-4.1-8b`; it did not establish explanation safety or an
architecture advantage. Earlier preflights were invalidated separately, and 18
R1 architecture rows were excluded after an outcome-dependent trace-contract
defect. Those records remain in the resource ledger but enter no architecture
or scientific denominator.

The first R2 canary launch then failed on Weave readback eventual consistency
after deterministic rows and before any model call. It is preserved as a
zero-inference operational incident: zero calls, tokens, cost, and scientific
denominator. Its partial checkpoint was not resumed.

The fresh R2b canary successfully completed the W&B/Weave infrastructure and
trace-acknowledgement path. On a safety-critical unsupported question, however,
Granite selected `recorded_rules_fail_v1` rather than the required
`authority_boundary_v1` refusal plan. The deterministic validator rejected the
model result before it could be released. `D1` selected
`authority_boundary_v1`, preserved repository fidelity, and passed the same
case. This is evidence that the safety boundary worked; one canary failure is
not an estimate of architecture efficacy.

The preregistration required a passing canary before architecture. Therefore no
architecture comparison, prompt/configuration campaign, frozen validation,
automated judge comparison, hidden test, or operational soak ran. The
architecture and scientific denominators are zero. There are consequently no
effect sizes, confidence intervals, causal-retrieval rates, or explanation-
quality results to report for R2b.

Cumulative conservative accounting is USD 0.013704, 291 provider/transport
calls, 243,928 input tokens, and 37,248 output tokens. The single fresh canary
call itself reported 302 input tokens, 25 output tokens, and USD 0.000018. These
are inference-resource observations, not measurements of electricity, carbon,
or ecological impact.

The terminal decision is `DISABLE_LIVE_GUIDE`; deterministic `D1` remains the
available Evidence Rail behavior and practitioner usefulness is
`not_measured`. Weave makes the explanation process inspectable; it does not
make generated output true. SWMM remains the hydraulic authority, repository
evidence remains the value authority, and the engineer remains the decision
authority. Structured output constrains response shape, but it does not change
those boundaries. [W&B Inference structured output](https://docs.wandb.ai/inference/response-settings/structured-output)
documents that mechanism, and the [model catalog](https://docs.wandb.ai/inference/models)
documents the evaluated provider choices.

## Reproducible resources

- `docs/evidence-guide-v2-study.md` records the preregistered architecture-first
  protocol, invalidated predecessor ledger, fresh canary result, resource
  accounting, and terminal release decision.
- `benchmarks/evidence_guide/v2/README.md` documents the visible development and
  validation corpus and the explicitly unsealed hidden-test receipt.
- `benchmarks/exhaustive_exact/dataset.json` is the immutable exact oracle.
- `scripts/run_search_benchmark_campaign.py` creates or resumes the complete
  method × budget × seed campaign and fails on scientific-library fallback.
- `scripts/build_benchmark_publication.py` derives the reviewed charts and CSV
  tables directly from the oracle and campaign manifests.
- `scripts/publish_benchmark_study.py` validates hashes, exact row counts,
  search accounting, and Restricted-project policy before any W&B write.
- `media/gi-scenario-film/story/research-resources.json` is the machine-readable
  source ledger, including what each source does and does not support.
- `benchmarks/realistic_fehraltorf/v1/case-study.json` is the deterministic
  twenty-case Fehraltorf projection, with source hashes, paired-year method,
  retained-report QA, and an explicit no-rerun boundary.
- `scripts/publish_fehraltorf_case_study.py` logs the realistic projection as a
  Restricted W&B Run/Table/Artifact and traces the comparison, QA inspection,
  and bounded-claim construction as ordinary Weave Ops. It uses no LLM and
  does not turn W&B or Weave into hydraulic authority.
- `media/gi-scenario-film/story/article.md` is the accessible narrative that
  combines the application walkthrough, benchmark evidence, limitations, and
  further reading.

W&B [Tables](https://docs.wandb.ai/guides/tables/),
[Artifacts](https://docs.wandb.ai/models/artifacts/), and
[Reports](https://docs.wandb.ai/models/reports/) provide the cloud mirror and
lineage. They do not replace the local authoritative record or turn a synthetic
benchmark into field evidence.

## Claims allowed in product copy

- Exact SWMM result under documented assumptions.
- Nondominated among exact alternatives evaluated.
- Tested against the selected storm cases.
- Conceptual model synthesized from documented source assets.

## Claims prohibited in product copy

- Real-world truth.
- Verified global optimum unless a finite candidate universe was exhaustively run.
- Calibrated, regulator approved, filing ready, or guaranteed resilient.
- Measured energy when the method is a TDP-based estimate.
- Model confidence when an interval only describes approximation error.
