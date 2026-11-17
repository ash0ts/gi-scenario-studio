# GI Scenario Studio — editor script

**Master:** 1920×1080, 30 fps, 27:50.000  
**Narration authority:** `story-manifest.json`; performer reads `performer-script.md` verbatim.  
**Captions:** burn-in optional; sidecar master is `combined.srt`. Never place captions over the application’s primary action or Evidence Rail fields.  
**Audio:** narration centered; quiet room tone under application holds; no triumphant sting on ALT-207. If music is used, keep it textural and at least 20 LU below narration.

## Editorial rules

1. Animation explains one relationship; the application proves that relationship exists in the product.
2. Use match cuts to preserve identity. Do not cover a discontinuity with a decorative whip, glitch, or particle transition.
3. Hold application pages long enough to read. Cursor motion is evidence-directed: one movement, one spoken idea.
4. Never animate an application control to imply functionality the capture did not execute.
5. Never show a fake solver progress bar. The guided state is a pinned replay.
6. Keep the synthetic-model notice in frame whenever a conclusion first appears.
7. All values, units, IDs, and claim language come from the manifest and claim ledger. Do not round again in editorial graphics.
8. Any cloud synchronization or provider-backed assistant workflow requires a policy-verified exact `Restricted` W&B binding, while local evidence remains authoritative. This recording is local-only because that scope cannot be verified. Do not add W&B or Weave success graphics.

## Canonical assembly

| Global in–out | Clip | Source | Edit objective |
|---|---|---|---|
| 00:00–00:55 | `01_pareto_trap` | `01_pareto_trap.mp4` | Cold-open on the attractive failure |
| 00:55–01:50 | `app_home` | App capture | Meet the engineer, define green infrastructure, and open the fictional teaching example |
| 01:50–02:50 | `02_rainfall_to_runoff` | `02_rainfall_to_runoff.mp4` | Explain forcing-to-response physically |
| 02:50–03:50 | `03_swmm_represents` | `03_swmm_represents.mp4` | Bound what SWMM represents and what exact means |
| 03:50–05:20 | `app_overview` | App capture | State all four rules before results |
| 05:20–06:00 | `04_decision_intersection` | `04_decision_intersection.mp4` | Show acceptance as an AND intersection |
| 06:00–07:35 | `app_model` | App capture | Separate preflight, QA, and model fitness |
| 07:35–08:15 | `05_qa_vs_fitness` | `05_qa_vs_fitness.mp4` | Make QA and fitness orthogonal |
| 08:15–09:50 | `app_storms` | App capture | Preserve depth, duration, pattern, and authority |
| 09:50–10:40 | `06_four_responses` | `06_four_responses.mp4` | Show response vector versus aggregate |
| 10:40–12:15 | `app_opportunities` | App capture | Show engineer-bounded integer choices |
| 12:15–13:30 | `07_bounded_search_space` | `07_bounded_search_space.mp4` | Bound the finite universe and ML authority |
| 13:30–14:50 | `app_goals` | App capture | Tie thresholds to metrics, units, cost, and QA |
| 14:50–15:40 | `08_job_matrix` | `08_job_matrix.mp4` | Reconcile 4 + 220×4 = 884 |
| 15:40–17:10 | `app_run` | App capture | Distinguish verified replay from local rerun |
| 17:10–19:20 | `app_acceptance_matrix` | App capture | Apply all four rules to the retained alternatives |
| 19:20–20:20 | `09_hidden_dimension` | `09_hidden_dimension.mp4` | Explain why 2D nondominance is incomplete |
| 20:20–21:50 | `app_tradeoff` | App capture | Use plot and support tabs after acceptance |
| 21:50–22:50 | `10_evidence_chain` | `10_evidence_chain.mp4` | Unroll one value into its evidence chain |
| 22:50–24:40 | `app_evidence_rail` | App capture | Inspect ALT-017, then ALT-207 |
| 24:40–26:15 | `app_export_observability` | App capture | Show computed gates, local download, failed-closed cloud policy |
| 26:15–26:55 | `11_engineer_decides` | `11_engineer_decides.mp4` | Convert shortlist into field questions |
| 26:55–27:50 | `app_closing` | App capture | State practitioner value and present limitations |

## Insert edit plans

### 01 · Pareto trap · 00:00–00:55

- **00:00–00:06:** Ask `WHAT DID THE PLOT FORGET?`; identify the dots only as 220 possible neighborhood plans under four rain tests.
- **00:06–00:16:** Draw the cost-versus-aggregate-volume plane and populate all 220 alternatives. Use the diagonal arrow only to explain the two visible directions of improvement.
- **00:16–00:25:** Draw the 6% “less total outlet water” target. Highlight `ALT-017`, `$54.99k`, and `4.14%` without implying acceptance.
- **00:25–00:38:** Compress the plot left. Open the long-event comparison on the right: `3.14 → 3.21 cfs`, or `+0.07 cfs`.
- **00:38–00:46:** Reveal that the stress-event peak also rises by `+0.01 cfs`; preserve the volume threshold so both omitted failures remain visible.
- **00:46–00:53:** Resolve `ATTRACTIVE ON THIS CHART ≠ ACCEPTABLE UNDER EVERY RULE`. Save “nondominated” for Scene 09, where it is defined.
- **00:53–00:55:** Silent hold.
- **Match cut:** preserve ALT-017’s apparent coordinate for eight frames, then dissolve to Home. Do not imply the Home screen contains this plot.

### 02 · Rainfall to runoff · 01:50–02:50

- **00:00–00:08:** Show a conceptual rainfall pattern over one catchment. Keep `CONCEPTUAL · NOT MEASURED` visible.
- **00:08–00:25:** Track the same water markers into three physical fates: infiltration, temporary storage, and runoff to outfall `OUT1`.
- **00:25–00:37:** Let runoff become an outfall flow-versus-time curve. Mark peak flow as a point and event volume as the area under the curve.
- **00:37–00:50:** Change only the rainfall pattern. Retain the earlier response in gray while the changed response appears in teal, so causality remains legible.
- **00:50–00:58:** Resolve `RAINFALL IS THE INPUT. RUNOFF IS THE MODELED RESPONSE.` Keep peak and volume visibly distinct.
- **00:58–01:00:** Silent hold on the catchment, outfall, and hydrograph.
- **Match cut:** preserve the physical drainage identity as the next insert asks which declared objects SWMM actually receives.

### 03 · What SWMM represents · 02:50–03:50

- **00:00–00:12:** Begin with the neighborhood we want to understand: land, buildings, and a drainage path.
- **00:12–00:25:** Cut—not morph—to the engineer’s declared runoff areas, junctions, pipes, and outfall. Then add rainfall, approved GI rules, and candidate counts.
- **00:25–00:40:** Draw a `DIRECT MODEL RUN` boundary: `MODEL + RAIN + PLAN → EPA SWMM 5.2.4 → RESULT FILES`. Explain the last card as the readable report and detailed output.
- **00:40–00:50:** Add a separate `READ + CHECK` boundary that finds volume and peak, checks the run, and creates digital fingerprints. Do not attribute these steps to SWMM.
- **00:50–00:57:** Place survey, field soils, calibration, and utilities outside both computational boundaries.
- **00:57–01:00:** Hold `EXACT CALCULATION ≠ FIELD TRUTH`.
- **Match cut:** dissolve the boundary into the real Overview and its model-limit language.

### 04 · Decision intersection · 05:20–06:00

- **00:00–00:09:** Present the four required conditions as equal parallel cards: volume, cost, every-storm peak, and direct-run QA.
- **00:09–00:16:** Join all four cards with one rail labelled `AND · NO AVERAGING`; do not imply an order of importance.
- **00:16–00:26:** Place ALT-207’s four observed values beneath the cards and show that all four terms are true.
- **00:26–00:35:** Replace that band with ALT-017. Mark volume and peak false while cost and QA remain true.
- **00:35–00:40:** Hold `A REQUIRED FAILURE CANNOT BE AVERAGED AWAY` and `ONE FALSE MAKES THE WHOLE AND STATEMENT FALSE`.
- **Match cut:** cut cleanly to the Model findings; do not imply the animated cards are interface rows.

### 05 · QA versus fitness · 07:35–08:15

- **00:00–00:07:** Draw vertical `COMPUTATIONAL TRACEABILITY` axis.
- **00:07–00:16:** Stack parser, execution, continuity, artifacts, and hashes.
- **00:16–00:22:** Draw horizontal `SITE EVIDENCE` axis.
- **00:22–00:31:** Place survey, observed events, calibration, and reference match.
- **00:31–00:37:** Place Cedar Trace at `TRACEABLE REPLAY`; add independent badge `FITNESS: NOT ASSESSED`.
- **00:37–00:40:** Hold `WITHIN POLICY` and `NOT ASSESSED` as independent evidence cards.
- **Match cut:** cut to Storms as the next evidence scope; do not imply a card-to-table morph.

### 06 · Four responses · 09:50–10:40

- **00:00–00:08:** Define ALT-017 as `[BIO, PP, RG, RB] = [0, 0, 1, 6]`—one deployment question.
- **00:08–00:22:** Branch that same question into four storm-specific response rows. Show storm depth and duration alongside each volume and peak delta.
- **00:22–00:31:** Copy the four volumes into one aggregate sum and compare the resulting reduction with the 6% target.
- **00:31–00:38:** Test each storm peak independently. Outline the long event as the worst response and identify the smaller stress-event increase.
- **00:38–00:45:** Dim the storm rows to demonstrate what disappears when only aggregate volume is retained; state that the peak guardrail has vanished from the projection.
- **00:45–00:50:** Restore all four rows and hold `SUM VOLUME. TEST PEAKS ONE BY ONE.`
- **Match cut:** retain the `[BIO, PP, RG, RB]` identity as the app’s four opportunity slots appear.

### 07 · Bounded search space · 12:15–13:30

- **00:00–00:12:** Build the four bounded integer choices and multiply `5 × 4 × 5 × 7 = 700`.
- **00:12–00:22:** Replace the arithmetic with 700 face-down cards: 700 known deployment questions whose hydraulic answers are still unknown.
- **00:22–00:37:** Separate the complete benchmark dataset, `700 × 4 = 2,800` direct jobs, from the deterministic guided replay, `4 + 220 × 4 = 884` direct jobs.
- **00:37–00:44:** State `NONEXHAUSTIVE · NOT ADAPTIVE ML` and that a finite study record is not a global real-world optimum claim.
- **00:44–00:57:** In a clearly labelled possible future workflow, let search select one face-down vector `[0, 3, 2, 6]` as the next question.
- **00:57–01:05:** Pass the vector and four storms through a direct SWMM execution. Reveal only modeled hydraulic quantities—peak, volume, and time series—after the run.
- **01:05–01:12:** Show budget, deduplication, and cache as ways to avoid repeated direct work. Resolve `SEARCH CHOOSES THE CARD. SWMM COMPUTES THE ANSWER.`
- **01:12–01:15:** Silent hold.
- **Match cut:** dissolve from the authority boundary to the pinned rules on Goals.

### 08 · Job matrix · 14:50–15:40

- **00:00–00:09:** Put the four named storm columns across the top and fill one untouched-model baseline row: four baseline jobs.
- **00:09–00:19:** Add representative alternative rows, preserve all four storm columns, and expand the brace to 220 vectors.
- **00:19–00:28:** Compress the alternatives into 44 bands × 5 rows, then write `4 + 220 × 4 = 884`.
- **00:28–00:36:** Sweep once through the matrix and label the complete record `884 JOB RECORDS · METRICS · QA · HASHES`; do not mimic live progress.
- **00:36–00:44:** Replace the matrix with the truthful retained-evidence decomposition: five named rows by four storms—one baseline plus four teaching alternatives—for 20 INP/RPT/OUT run sets.
- **00:44–00:48:** Contrast `OPEN FINISHED RECORD · VERIFIED REPLAY` with `EXECUTE AGAIN · NEW LOCAL RERUN`.
- **00:48–00:50:** Silent hold.
- **Match cut:** four equation terms become Run statistic cards.

### 09 · Hidden dimension · 19:20–20:20

- **00:00–00:12:** Four gauges appear: volume, cost, peak, QA.
- **00:12–00:27:** ALT-207 passes all; ALT-017 fails volume and peak; ALT-109 and ALT-014 fail cost.
- **00:27–00:39:** Project only cost and volume into 2D. All four retained points sit on the evaluated frontier for that view.
- **00:39–00:48:** Rotate peak dimension back; lift ALT-017 out of acceptable region with `+0.07 cfs`.
- **00:48–00:57:** Headline `PARETO FRONTIER ≠ ACCEPTANCE BOUNDARY` with limitation `AMONG 220 EVALUATED DESIGNS`.
- **00:57–01:00:** Silent hold.
- **Match cut:** cost-volume plane aligns pixel-for-pixel in orientation, not necessarily scale, with the app plot.

### 10 · Evidence chain · 21:50–22:50

- **00:00–00:07:** Display `ALT-017 · LONG-EVENT PEAK Δ = +0.07 cfs`.
- **00:07–00:15:** Ask `COMPARED WITH WHAT?`; reveal `3.14 → 3.21 cfs`.
- **00:15–00:34:** Keep the claim anchored while eight numbered cards form a causal snake: added practices → input model → SWMM run → report and detailed output → matching fingerprints → outlet peak → run check → limitations.
- **00:34–00:42:** Traverse the visible arrows in numerical order and enclose the complete path as `RETRACEABLE`.
- **00:42–00:49:** Replace the dense path cleanly with two separate statements: what provenance establishes and what it does not establish.
- **00:49–00:57:** Rotate the same evidence into the application’s narrow right-side Evidence Rail. No cloud icon.
- **00:57–01:00:** Hold `PROVENANCE KEEPS THE CLAIM INSIDE ITS ACTUAL BOUNDARY`.
- **Match cut:** rail becomes the real Evidence Rail already open on ALT-017.

### 11 · Engineer decides · 26:15–26:55

- **00:00–00:08:** ALT-207 enters a funnel labelled `SCREENING`; explicitly cross out `DESIGN APPROVAL`.
- **00:08–00:24:** Expand into seven cards: drainage, field reconnaissance, utilities, soils, maintenance, constructability, model fitness.
- **00:24–00:31:** Place the evidence path beside the unresolved field questions, never inside a trophy.
- **00:31–00:37:** Resolve the cards into `SWMM computes`, `SEARCH allocates attention`, `EVIDENCE preserves the path`, and `THE ENGINEER decides`.
- **00:37–00:40:** Hold the four-line authority summary with assumptions and synthetic limitations visible.
- **Match cut:** cut from `THE ENGINEER decides` to the Closing screen and its professional next actions.

## Application capture plan

### Home · 00:55–01:50

- Start on a settled page for four seconds.
- Point to the existing-model card during “practicing engineer.”
- Point to the guided card during “Cedar Trace.”
- Keep the disabled conceptual roadmap visible during that limitation.
- Point once to `LOCAL_ONLY`; do not open observability yet.
- Click guided example on the last spoken clause. Allow page transition to finish before the cut.

### Overview · 03:50–05:20

- Keep heading, purpose, and all four rules in the first frame.
- Cursor tracks rule order 1→4. Do not circle or wiggle.
- Open model limits only after all rules have been read.
- Rail remains open three seconds, then closes with focus restored.
- Click Start on the last line; audio may continue one second across the transition.

### Model · 06:00–07:35

- Show model name, version, units, fingerprint before expanding bundle.
- Pointer settles on missing reference comparison exactly when narration says it.
- Show `0.079% max` and ±2% together; never isolate the number from policy.
- Open deterministic explanation long enough to read its “no model or cloud call” label.
- Check acknowledgment only after the limitation is spoken.

### Storms · 08:15–09:50

- Keep authority column visible while reading every depth and duration.
- Long-event evidence opens once, then closes. Do not preview ALT-017 yet.
- Hold stress hyetograph without claiming it controls the decision.
- Cross-model checks stay visible during comparability language.

### Opportunities · 10:40–12:15

- Select each slot once; no rapid toggling.
- Preserve candidate inspector and map together.
- Show `Schematic`, `Not site geometry`, `Demo approval only`, and `Read only` in legible frames.
- Do not click synthetic cost assumptions; Goals owns that explanation.

### Goals · 13:30–14:50

- Track four rules in numerical order.
- Keep economic assumptions on screen as a set.
- Zero-flooding-loss caution must remain visible for the entire caveat.
- Point to ALT-017 adverse strip but save its trace for later.

### Run · 15:40–17:10

- First frame includes “No solver jobs are being simulated now.”
- Do not mouse over a hidden or disabled rerun in a way that suggests it will work.
- Reveal the solver hash with version in the same shot.
- Preview scatterplot receives no interpretive zoom.
- Navigate to Compare at the end.

### Acceptance matrix · 17:10–19:20

- Use a slow vertical crop or gentle page scroll, never a digital zoom that blurs text.
- ALT-017 row first; hold both red cells in one frame so the bargain-looking failure becomes concrete.
- ALT-207 row second; hold its cautious disposition and never call it a recommendation.
- ALT-109 and ALT-014 cost failures each receive a readable pause.
- Keep “stored full precision” note visible before concluding 1/4 passes.
- Do not click Trace yet.

### Tradeoff · 20:20–21:50

- Legend and axes remain visible during point-color explanation.
- Select only ALT-017 and ALT-207 for the main comparison.
- Change metric once and return; do not imply the maximum-peak plot is a new simulation.
- Each support tab gets one clean settled view.
- The zero-flooding-loss caveat stays on screen while spoken.

### Evidence Rail · 22:50–24:40

- Rail opens at top with keyboard focus restored to heading.
- ALT-017 long-event run is the only expanded run during failure explanation.
- Show all three hashes without reading their strings aloud.
- Scroll to typed changes, local deterministic explanation, policy-blocked local-only mirror, and retained artifacts.
- Close; open ALT-207; return to the top automatically.
- End on cautious handoff language, not the cost.

### Export and observability · 24:40–26:15

- Show computed gate copy before individual rows.
- Inspect `guided-manifest`, close rail, then show evidence-set files and `20 run sets`.
- Download exactly once; success banner must say local and not synchronized.
- Open observability without any credential entry.
- Keep the local-only recording state and exact `Restricted` cloud/assistant binding requirement legible.
- Do not describe W&B or Weave as optional; the exception is this source recording’s failed-closed policy state.
- No W&B or Weave browser tab, success toast, run URL, artifact URL, or trace URL appears.

### Closing · 26:55–27:50

- Begin on “Not design approval” plus field actions.
- Return to either decision summary or Home in one clean cut.
- Existing-model path receives the final cursor point.
- End with an evidence affordance visible.
- Four-second final hold with room tone; no celebratory end card.

## Caption and finishing checks

- `combined.srt` must end no later than 27:46, leaving the four-second final hold clean.
- Per-clip SRTs use local time beginning near 00:00:00,500 and never cross their clip duration.
- Preserve units with values: percent, dollars, Mgal, cfs, years, and storm count.
- Preserve negative scope language in captions at the moment the associated conclusion appears.
- Verify no caption obscures: four Overview rules, Model Fitness Passport, storm authority, opportunity ranges, Run replay notice, acceptance cells, plot axes, Evidence Rail hashes, or observability policy.
- Final loudness, color, font, and safe-title checks occur after captions are burned or attached.
