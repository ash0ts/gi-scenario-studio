# GI Scenario Studio film claim ledger

## Analysis contract

- **Audience:** Environmental and stormwater practitioners, students, and technically curious viewers who may know neither SWMM nor multi-objective search.
- **Single question:** How can computation help an engineer screen many green-infrastructure alternatives without allowing AI to invent hydraulic evidence or make the decision?
- **Intended takeaway:** Search may allocate a limited simulation budget, but every displayed hydraulic value remains a direct, traceable SWMM result and the engineer retains decision authority.
- **Source boundary:** Numerical film claims come only from `apps/desktop/src/data/guidedFixture.json` and `benchmarks/exhaustive_exact/dataset.json`, normalized by `src/gi_story/claims.py`.
- **Out of scope:** Calibration, field validity, regulatory compliance, real-world flood-risk reduction, global optimality, construction feasibility, accurate cost, search-method superiority, energy/carbon savings, and a production W&B/Weave connection.

## Fixed, varied, and measured

- **Fixed:** Cedar Trace teaching model, four synthetic storms, pinned EPA SWMM 5.2.4 binary, LID-control definitions, candidate-slot bounds, metric definitions, QA policy, and synthetic cost basis.
- **Varied:** Four integer counts of additional bioretention, permeable-pavement, rain-garden, and rain-barrel units.
- **Measured:** OUT1 event volume and peak by storm, aggregate four-storm volume reduction, fixture QA, and synthetic present-value cost.
- **Observed:** The 884-job guided record and 2,800-job exhaustive benchmark table contain direct SWMM results under the documented synthetic assumptions.
- **Interpreted:** A Pareto position is not an engineering acceptance decision; `ALT-017` is the counterexample.
- **Limitation:** Exact means simulator-exact for these inputs, not real-world truth.
- **Next question:** After a candidate survives screening, what field, utility, soil, maintenance, constructability, and model-fitness evidence is required?

## Displayed claims

| Claim ID | Approved visible claim | Authoritative evidence | Required caveat |
|---|---|---|---|
| `C-GUIDED-SYNTHETIC` | Cedar Trace is a synthetic teaching watershed with model fitness `not_assessed`. | `guidedFixture.json#/project_kind`, `#/fitness_disposition`, `#/notice` | Not a real site, adopted storm, calibrated model, design recommendation, or flood-risk determination. |
| `C-UNIVERSE-700` | Candidate bounds provide `5 × 4 × 5 × 7 = 700` integer vectors: one baseline vector and 699 nonbaseline vectors. | `dataset.json#/candidate_slots`, `#/universe_size`, `#/job_accounting/vectors` | The vector entries are counts of **additional** fixed units; they are not AI-authored LID designs. |
| `C-STORMS-4` | Every design is tested against four synthetic storms: 1.0 in/1 hr, 2.0 in/1 hr, 2.5 in/4 hr, and 4.0 in/2 hr. | `guidedFixture.json#/storms`, `#/baseline/runs` | User-defined sensitivities, not observed events or recurrence intervals. |
| `C-BASELINE-0748` | The four-storm baseline OUT1 event volume is 0.748 Mgal. | `guidedFixture.json#/baseline/four_storm_outfall_volume_million_gallons` | Aggregate of this teaching fixture's four selected events, not an annual or real-site volume. |
| `C-QA-0079` | The largest absolute baseline continuity error is 0.079%, within the fixture's ±2% warning policy. | `guidedFixture.json#/baseline/runs/*/metrics`, `#/baseline/runs/*/qa/policy` | Numerical QA is not calibration, reference matching, or model fitness. |
| `C-COST-BASIS` | One synthetic cost basis uses synthetic 2026 USD, a 30-year horizon, and a 3% real discount rate. | `guidedFixture.json#/cost_basis` | Not a bid, quantity takeoff, vendor quote, market source, or location-specific estimate. |
| `C-JOBS-884` | Guided replay arithmetic is `4 baselines + 220 alternatives × 4 storms = 884` completed direct SWMM jobs. | `guidedFixture.json#/job_accounting` | Replay reads prior exact records; it does not execute 884 jobs during the film. The 220 alternatives are a deterministic subset, not an ML-selected or exhaustive study. |
| `C-RETAINED-20` | The fixture retains 20 complete INP/RPT/OUT run sets: four baselines plus four teaching alternatives under four storms. | `guidedFixture.json#/job_accounting/retained_jobs`, `#/baseline/artifact_availability`, teaching alternatives' `artifact_availability` | All 884 records have metrics and hashes, but only 20 have complete retained solver files. |
| `C-BENCHMARK-2800` | Exhaustive benchmark arithmetic is `700 vectors × 4 storms = 2,800` direct SWMM jobs, equivalently `4 + 699 × 4`. | `dataset.json#/job_accounting`, `#/oracle_policy` | Search replays reveal stored outcomes only after requesting a vector; one request costs four exact-job equivalents. They do not rerun all hydraulics for every method and seed. |
| `C-ALT017` | `ALT-017 [0,0,1,6]`: 0.717 Mgal; 4.14% reduction; $54.99k. It fails the volume rule and raises two peaks: long event 3.14→3.21 cfs (+0.07), stress event 13.33→13.34 cfs (+0.01). | `guidedFixture.json` alternative `ALT-017`, especially `storm_runs` for `synthetic_long_2_5in` and `synthetic_stress_4in` | Say the long event is the **worst** increase, not the only increase. Nondominated does not mean acceptable. |
| `C-ALT207` | `ALT-207 [0,3,2,6]`: 0.700 Mgal; 6.42% reduction; $190.25k; passes all four teaching rules. | `guidedFixture.json` alternative `ALT-207` | Use only “Candidate to carry forward for site and feasibility review.” It is not best, optimal, validated, approved, or design-ready. |
| `C-ALT109` | `ALT-109 [1,3,2,6]`: 0.696 Mgal; 6.95% reduction; $222.01k; exceeds the cost cap. | `guidedFixture.json` alternative `ALT-109` | Stronger observed volume reduction does not make the option acceptable under the brief. |
| `C-ALT014` | `ALT-014 [4,3,4,5]`: 0.682 Mgal; 8.82% reduction; $361.86k; strongest observed volume result among the 220 guided alternatives. | `guidedFixture.json` alternative `ALT-014` | Do not call it the strongest possible vector in the complete 700-vector universe. |
| `C-ML-BOUNDARY` | Search may rank which candidate to simulate or reveal next; SWMM supplies every reportable hydraulic value. | `README.md` core invariant; `python/gi_scenario_core/benchmark.py` module contract | No film claim that ExtraTrees, NSGA-II, or any ML method beats Sobol. No predicted hydraulic values, confidence, energy, or carbon claims. |
| `C-WANDB-BOUNDARY` | Any cloud synchronization or provider-backed assistant workflow requires a W&B binding whose exact `Restricted` policy status is verified; local SQLite and content-addressed artifacts remain the engineering authority. | `docs/observability.md`, `docs/product-principles.md` | The current SDK cannot authoritatively prove exact `Restricted` scope, so this source-build recording fails closed before upload. The visible built-in explanation is deterministic local copy, not a provider-backed AI assistant call. |
| `C-EVIDENCE-CHAIN` | For a selected retained run: integer change → materialized INP → recorded SWMM execution → retained RPT/OUT and hashes → QA-classified metric → decision rule. | `apps/desktop/src/data/demo.ts`, `apps/desktop/src/components/EvidenceRail.tsx` | Full files exist for 20 retained run sets; all 884 records carry exact metrics, QA, and retained file hashes. Traceability establishes provenance, not calibration or regulatory authority. |

## Exact display values

### Baseline storms

| Storm | Depth / duration | Baseline OUT1 peak | Baseline OUT1 volume |
|---|---:|---:|---:|
| Balanced | 1.0 in / 1 hr | 4.14 cfs | 0.062 Mgal |
| Front-loaded | 2.0 in / 1 hr | 10.11 cfs | 0.157 Mgal |
| Long-duration | 2.5 in / 4 hr | 3.14 cfs | 0.167 Mgal |
| Stress | 4.0 in / 2 hr | 13.33 cfs | 0.362 Mgal |

Aggregate baseline OUT1 volume is `0.748 Mgal`.

### Teaching rules

1. At least 6% aggregate OUT1 event-volume reduction across the four storms.
2. At most $200,000 synthetic 30-year present-value screening cost.
3. No OUT1 peak increase in any selected storm.
4. Accepted fixture QA for every design × storm record.

Displayed costs and percentages are rounded for explanation. Rule results come from stored manifest precision.

## Safe conclusion language

- “Direct SWMM 5.2.4 result under the documented synthetic assumptions.”
- “Nondominated among the 220 directly evaluated alternatives for these objectives.”
- “Tested against four selected synthetic storm cases.”
- “Candidate to carry forward for site and feasibility review.”
- “Numerical QA passed; model fitness remains not assessed.”
- “Verified replay of prior exact-run records.”

## Forbidden overclaims

- AI designed or hydraulically predicted the alternative.
- Best, globally optimal, regulator-approved, validated, calibrated, design-ready, or guaranteed resilient.
- Real-world flood-risk reduction or no adverse impacts.
- Accurate or location-specific cost estimate.
- A successful live W&B upload or provider-backed Weave assistant in this source build.
- All 884 records retain complete files.
- The 884 jobs are executing during replay.
- `ALT-207` is the only passing design among all 220 or all 700; it is the only passing member of the four-option teaching shortlist.
