# Cedar Trace guided example

## Purpose

Cedar Trace teaches a practicing stormwater engineer how to screen green-infrastructure alternatives without obscuring the boundary between exact hydraulic results, search logic, assumptions, and professional judgment.

The user is answering one fictional planning question:

> Which Cedar Trace GI concept, if any, should advance to field and site-feasibility review?

Cedar Trace is a self-authored CC0 teaching watershed. It is not a real site. Its model, storms, opportunities, and costs are synthetic. Its `project_kind` is permanently `demo`, while its independent Model Fitness Passport disposition is `not_assessed`. It must display this notice:

> Synthetic teaching model and storms — not for design, permitting, flood-risk determination, or representation of an existing drainage system.

The end of the walkthrough is a candidate for further investigation, not a design recommendation.

## What the fixture contains

The source model has two subcatchments, two junctions, two conduits, one free outfall (`OUT1`), and one existing bioretention unit in `S1`. It uses EPA SWMM dynamic-wave routing and Green-Ampt infiltration in US customary units.

The exact evidence manifest is `benchmarks/guided_exact/manifest.json` (`cedar_trace_exact_v2`). It records:

- Four baseline runs.
- 220 nonbaseline integer design vectors evaluated under all four storms.
- 884 completed exact SWMM jobs in total.
- Accepted fixture QA for all 884 records.
- Input, report, output, and solver SHA-256 hashes for every record.
- Complete INP, RPT, and OUT files for the four baseline runs and the four teaching alternatives under every storm: 20 retained run sets.

The 220 designs are a deterministic hash-ranked subset of the finite design space. The fixture does not claim exhaustive search or a globally optimal design. No adaptive-ML performance claim is demonstrated by this example.

## The decision rules

The four rules must be visible before results are shown. Status labels are derived from these rules; they are not hidden UI thresholds.

| Rule | Threshold | Scope |
|---|---:|---|
| Four-storm `OUT1` volume reduction | At least 6.0% | Sum of `OUT1` event volumes across the four selected synthetic storms |
| Synthetic present-value screening cost | At most $200,000 | 30-year, 3% real discount rate, synthetic 2026 teaching values |
| Storm-specific `OUT1` peak change | No increase | Compare each alternative with the matching baseline storm |
| Exact-run QA | Accepted | Every design × storm record used by the alternative |

The 6% rule is evaluated from manifest values. Rounded values shown in the interface are explanatory and must not become a separate source of truth.

## Storm cases

These cases teach sensitivity to depth, duration, and temporal pattern. They are not observed events, adopted design storms, or recurrence intervals.

| Storm | Depth | Duration | Baseline `OUT1` peak | Baseline `OUT1` volume |
|---|---:|---:|---:|---:|
| Synthetic balanced event | 1.0 in | 1 hr | 4.14 cfs | 0.062 Mgal |
| Synthetic front-loaded event | 2.0 in | 1 hr | 10.11 cfs | 0.157 Mgal |
| Synthetic long-duration event | 2.5 in | 4 hr | 3.14 cfs | 0.167 Mgal |
| Synthetic stress event | 4.0 in | 2 hr | 13.33 cfs | 0.362 Mgal |

The aggregate baseline `OUT1` event volume is 0.748 Mgal.

## Allowed GI changes

The LID controls and their layer parameters are fixed teaching inputs. The search changes only the integer count of additional identical units at the approved slots.

| Vector position | Slot | Additional units | Unit footprint | Captured impervious area | Destination |
|---:|---|---:|---:|---:|---|
| 1 | `S1_BIO` bioretention | 0–4 | 200 ft² | 15% | `J1` |
| 2 | `S1_PP` permeable pavement | 0–3 | 500 ft² | 10% | `J1` |
| 3 | `S2_RG` rain garden | 0–4 | 225 ft² | 20% | Pervious routing, then `J2` |
| 4 | `S2_RB` rain barrel | 0–6 | 16 ft² | 5% | `J2` |

“Engineer approved” on these records means approved only for use in this synthetic demo. It is not a model-fitness or site-feasibility attestation.

## One versioned cost basis

All guided-example cost arithmetic comes from `cedar-trace-synthetic-cost-basis-v1`, defined in `python/gi_scenario_core/demo.py` and projected into the demo project and UI fixture. The older opportunity catalog is not an alternative cost source for this story.

| Slot | Unit CAPEX | Annual O&M | Replacement |
|---|---:|---:|---:|
| `S1_BIO` | $20,000 | $600/year | None |
| `S1_PP` | $28,000 | $450/year | None |
| `S2_RG` | $15,000 | $500/year | None |
| `S2_RB` | $2,500 | $80/year | $1,500 in year 15 |

These are stable synthetic inputs for teaching present-value arithmetic. They are not a quantity takeoff, market survey, vendor quote, bid, or location-specific estimate.

## The four teaching alternatives

All four are nondominated among the 220 exact evaluated alternatives for synthetic present-value cost and aggregate four-storm `OUT1` volume. Nondominance is descriptive; it is not an acceptance rule.

| Role | Alternative and vector | Cost | Volume reduction | Worst peak change | Rule result |
|---|---|---:|---:|---:|---|
| Low-cost caution | `ALT-017 [0,0,1,6]` | $54.99k | 4.14% | +0.07 cfs (+2.23%) in the long event | Does not meet |
| Carry-forward candidate | `ALT-207 [0,3,2,6]` | $190.25k | 6.42% | −0.08 cfs in the long event | Meets all four |
| Stronger, over budget | `ALT-109 [1,3,2,6]` | $222.01k | 6.95% | −0.08 cfs in the stress event | Does not meet cost cap |
| Observed performance extreme | `ALT-014 [4,3,4,5]` | $361.86k | 8.82% | −0.11 cfs in the long event | Does not meet cost cap |

`ALT-017` is the critical teaching case. It is inexpensive and nondominated on the two plotted objectives, yet it misses the aggregate-volume target and increases the modeled long-event peak from 3.14 to 3.21 cfs. A Pareto position is therefore not an engineering acceptance decision.

`ALT-207` meets the declared teaching rules. The responsible conclusion is:

> Candidate to carry forward for site and feasibility review.

It is not “the best design,” “approved,” “validated,” or “guaranteed resilient.”

## Ten learning checkpoints

Each screen should answer one professional question and offer one primary action.

| # | Checkpoint | Question the screen must answer | Learning outcome |
|---:|---|---|---|
| 1 | Name the decision | What is this analysis allowed to support? | Restate the target, cost cap, guardrail, and next action |
| 2 | Read the Model Fitness Passport | Why is its disposition `not_assessed`? | Separate project origin, solver QA, calibration evidence, and intended-use fitness |
| 3 | Inspect the baseline | Did every untouched storm produce an accepted exact run? | Check provenance and continuity before comparison |
| 4 | Confirm storm scope | Which cases are tested, and which regulatory claims are excluded? | Treat the cases as synthetic sensitivities |
| 5 | Review allowed changes | Which inputs are fixed and which unit counts vary? | Confirm that search does not invent LID designs |
| 6 | Read the run plan | How do four baselines and 220 designs become 884 jobs? | Understand that search chooses runs and SWMM supplies results |
| 7 | Apply every rule | Which alternatives pass volume, cost, peak, and QA together? | Use a rule matrix before the Pareto chart |
| 8 | Find the adverse change | Why does `ALT-017` fail? | Inspect storm-specific deltas, not just aggregates |
| 9 | Resolve the evidence | Which runs, hashes, assumptions, and limitations support a value? | Trace a claim to exact evidence and retained files |
| 10 | Choose the next action | What must happen before design use? | Plan field, geotechnical, utility, O&M, and constructability review |

## Replay and rerun are different actions

### Verified replay

Replay is the default offline experience. It reads the committed exact-run manifest and retained artifacts; it does not execute SWMM. “Verified” here means the fixture passed structural checks and its retained bytes match the recorded hashes. It does not mean the synthetic drainage model is calibrated, reference matched, or representative of field conditions.

The Evidence Rail can open the retained INP, RPT, and OUT records for every baseline and teaching-alternative storm run.

### Optional rerun

A rerun is conditional on the host having a configured SWMM 5.2.4 executable whose SHA-256 matches the manifest. It creates new isolated run artifacts. New results must be compared with the replay record using the documented metric-specific tolerances.

The static fixture cannot claim that a suitable solver is installed. A disabled rerun action must state the missing requirement rather than failing silently or falling back to fabricated values.

## Optional W&B and Weave record

The guided replay works entirely in `LOCAL_ONLY` mode. If the user deliberately
binds the project to a user-owned W&B project verified exactly as `Restricted`,
the source MVP queues one `guided_session` snapshot when `guided.open`
succeeds. Its `guided_checkpoints` Table records the fixture's
initial checkpoint definitions and status; it does not yet record later
completion, duration, backtracking, Evidence Rail opens, comparisons, rerun
status, or browser-download completion. The browser learning summary is local
and is not logged as an output artifact.

After a verified binding, an offline walkthrough may continue and queue the
implemented open-time snapshot for synchronization. The source MVP provides
deterministic guided copy and Evidence Rail navigation. Its optional,
provider-backed Evidence Guide remains online-only: each question makes two
pinned W&B Inference calls, may select one to four of six capability-gated read
tools, and must finish as a flushed, read-back Weave Agents turn. No untraced or
unvalidated answer is substituted.

## Evidence Rail requirements

Selecting a teaching alternative should reveal:

- Its four exact run IDs and storm IDs.
- Matching baseline and alternative peak values for every storm.
- Input, RPT, OUT, and solver hashes.
- The exact integer LID changes by slot.
- The versioned cost basis and present-value derivation.
- QA status and continuity findings.
- The four-rule pass/fail matrix.
- Project kind, Model Fitness Passport, storm, metric, and artifact limitations.

The “controlling” result for a rule is the worst rule margin or adverse delta. It is not automatically the storm with the largest absolute volume.

## Precise claim boundaries

Acceptable language includes:

- “Direct SWMM 5.2.4 result under the documented synthetic assumptions.”
- “Nondominated among 220 exact evaluated alternatives for the stated objectives.”
- “Tested against the four selected synthetic storm cases.”
- “A small modeled `OUT1` peak increase occurred in the long synthetic event.”

Do not claim:

- Calibration, validation, or reference reproduction.
- A regulatory or return-period storm.
- Real-world flood-risk reduction.
- No adverse impacts.
- A global optimum or universally best design.
- A construction-ready or accurate cost estimate.

The current extractor supports runoff and flow continuity, reported system flooding loss, and `OUT1` event peak and volume. It does not support conclusions about node HGL, surcharge duration, link-capacity duration, complete hydrographs, or full-network adverse impacts. Zero reported system flooding loss in this fixture is not evidence that a real project has no flooding or adverse effects.

## What happens after the walkthrough

The appropriate next step for `ALT-207` is a feasibility work plan: confirm drainage topology and available area, inspect utilities and ownership, obtain soils and infiltration information, evaluate maintenance access and responsibility, review constructability, replace synthetic costs, select authoritative local storms, and use a fit-for-purpose reviewed or calibrated model.

Only after that work can a professional decide whether any concept should advance beyond alternatives screening.
