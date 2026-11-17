# The Stormwater Plan That Looked Like a Bargain—Until We Asked One More Question

*How direct drainage-model runs, bounded search, and traceable evidence can help engineers narrow hundreds of green-infrastructure ideas without mistaking a clean calculation for field truth.*

The $55,000 plan looked terrific right up until we let the model rain for four hours.

Imagine you are responsible for a neighborhood where hard rain can overwhelm the drains. You have 220 possible improvement plans and nowhere near enough money to investigate—let alone build—all of them. So you make the sensible chart: cost along the bottom, reduction in water reaching the outlet up the side. One plan settles into a very flattering corner. It is cheap. It reduces modeled volume. This is the sort of dot that gets copied into a slide deck and begins acquiring adjectives.

Then somebody asks one more question.

During the long storm, that same plan raises the highest modeled flow from 3.14 to 3.21 cubic feet per second. The peak in a second storm rises too. The plan also misses the 6% volume-reduction target written into the decision brief.

The chart was honest. It was incomplete. It answered a smaller question and let us enjoy the larger conclusion.

> **A good decision is not the most attractive point on a plot. It is an alternative that survives every rule we decided mattered.**

That is the problem GI Scenario Studio is built to explore: how can an engineer screen many green-infrastructure alternatives without hiding adverse hydraulic behavior or losing the evidence behind a result?

Our recurring neighborhood is **Cedar Trace**, which is fictional in the way only a teaching example can be: the pipes are tidy, the costs are invented, and the storms arrive exactly when the demonstration expects them. Its rain, drainage model, and alternatives are synthetic. Cedar Trace is not a real municipality, a calibrated model, a flood-risk determination, or a recommendation to build anything. That limitation is evidence, not fine print.

<!-- ARTICLE_ANIMATION key="animation/incomplete_plot" -->

**What this establishes.** ALT-017 looks attractive in a cost-volume projection but fails both the aggregate-volume target and the selected-storm peak guardrail.

**What this does not establish.** This comparison does not rank real projects or establish that any Cedar Trace alternative is field-ready.

| ALT-017 evidence | Modeled result | Declared requirement | Disposition |
|---|---:|---:|---|
| Four-event outlet-volume reduction | 4.14% | at least 6.00% | Fails |
| Synthetic present-value screening cost | $54.99k | at most $200k | Passes |
| Long-event outlet peak | 3.14 → 3.21 cfs | no increase | Fails |
| Stress-event outlet peak | 13.33 → 13.34 cfs | no increase | Fails |
| Direct-run numerical checks | 4 of 4 accepted | all accepted | Passes |

## First, follow the water

Before we give a computer 884 jobs, it is worth remembering what the computer is trying to describe.

Green infrastructure changes how much rain becomes runoff and how quickly that runoff arrives. Rain gardens, planted basins, permeable pavement, and rain barrels can store water, slow it down, help it soak into the ground, or keep it out of the pipe system for a while. None of this requires a machine-learning vocabulary. It requires following the water.

Rain falls on an area that drains toward the same place. Engineers call that area a **catchment**. Some water soaks into soil. Some waits in a shallow depression or rain garden. The rest moves over the surface, into drains, and through pipes. Eventually it reaches an **outfall**: the point where water leaves the drainage model and, very politely, exits our little imaginary world.

Flow at that outfall rises and falls during a storm. Two measurements matter here:

- **Event volume** is all the water that passes during the event—the area under the flow-over-time curve.
- **Peak flow** is the highest modeled flow rate reached during the event.

A plan can reduce total volume without reducing the peak. It can even lower the peak in one storm and raise it in another. Storage that helps during a short burst may fill during a longer event. A plan therefore has to face more than one kind of rain, and one aggregate score cannot safely impersonate every hydraulic question.

Timing matters.

Cedar Trace uses four artificial patterns: one inch over one hour; two inches over one front-loaded hour; two and a half inches over four hours; and a four-inch, two-hour stress event. These are practice questions, not historical storms or official design events. The long four-hour event produces ALT-017's worst increase even though the stress event has a much larger overall peak.

The largest storm is not automatically the most revealing storm.

<!-- ARTICLE_VIDEO key="app/storm_scope" -->

## SWMM is an obedient imaginary town

SWMM is the US Environmental Protection Agency's **Storm Water Management Model**. A useful way to think about it is as a calculator for a simplified drainage world made of catchments, junctions, pipes, outlets, and rules about how water moves between them.

An engineer supplies that world, a rain pattern, and the green-infrastructure plan to test. SWMM applies the declared equations and produces modeled flows and water levels. GI Scenario Studio reads the result files, checks whether each run completed, checks whether its numerical water balance stayed inside the declared limit, and gives the files digital fingerprints so somebody can find the same calculation again.

This is valuable because SWMM is consistent. It is also uncritical.

It cannot notice the buried pipe we forgot to draw. It cannot walk outside, test the soil, find a utility conflict, ask who will maintain the rain garden, or decide whether our synthetic storm represents local risk. It will solve the world we described with admirable concentration, even when we described the wrong world.

> **Exact calculation under stated assumptions is not the same as field truth.**

<!-- ARTICLE_ANIMATION key="animation/water_ledger" -->

The long-event ledger below is the receipt. It holds rainfall fixed and shows where SWMM's accounting says the water went. Across all four selected events, OUT1 volume changes from 0.748 to 0.700 million gallons—about 48,000 fewer modeled gallons.

**What this establishes.** Under the declared synthetic model, ALT-207 changes the modeled partition among infiltration loss, surface runoff, final storage, low-impact-development (LID) drainage, and outfall volume.

Here, **infiltration loss** is SWMM runoff-accounting language. It is not a measurement of groundwater recharge.

**What this does not establish.** The calculation does not measure groundwater recharge, water quality, habitat, avoided flooding, annual performance, or realized ecological benefit.

| Long-event SWMM accounting quantity | Unchanged model | ALT-207 | Units |
|---|---:|---:|---|
| Total precipitation | 0.937 | 0.937 | acre-ft |
| Infiltration loss | 0.412 | 0.436 | acre-ft |
| Surface runoff | 0.514 | 0.447 | acre-ft |
| Final storage | 0.012 | 0.043 | acre-ft |
| LID drainage | 0.000* | 0.014 | acre-ft |
| External outflow | 0.167 | 0.150 | million gallons |

\* The unchanged-model report omits the optional LID-drainage row, so the comparison projects that absence as zero. The table otherwise reproduces values rounded in the retained SWMM reports. Small rounded mass-balance differences are not substituted for SWMM's own continuity calculation.

## Write the rules before the plot starts flirting

If the acceptance rules appear only after our favorite dot does, we are no longer screening alternatives. We are writing fan fiction about a scatterplot.

Cedar Trace therefore starts with a decision brief, written before the results appear.

An alternative advances through this teaching screen only if it satisfies all four rules:

1. Across four selected synthetic storms, reduce aggregate outlet volume by at least 6% relative to the unchanged model.
2. Keep the fictional 30-year present-value screening cost—future costs expressed in today's dollars—at or below $200,000.
3. Do not increase peak flow at the outlet in any selected storm.
4. Pass the numerical checks for every plan-by-storm simulation used in the decision.

The unchanged model is the **baseline**. It gives every alternative the same point of comparison.

These four rules are wired like four lights in series. A plan advances only if all four are green. Three passes cannot average away one required failure.

ALT-207 clears all four teaching rules. ALT-017 is cheaper, but it misses the volume target and raises the peak in two storms. Under this brief, those are not small deductions from an overall score. They are stopping conditions.

## A clean calculation can still have the wrong map

Before comparing alternatives, the application examines the unchanged model and the saved study. It identifies the software version, confirms the required files are present, opens the model, and checks the four stored baseline runs. Its water-accounting check compares what entered, left, and remained in the model. The largest mismatch is 0.079%, inside the ±2% teaching limit.

So the stored calculations completed cleanly. The engine ran. The files reconciled. The imaginary town behaved consistently.

But Cedar Trace includes no observed storm and no trusted reference result for comparison. Its **model fitness is therefore not assessed**. That label is neither a failure nor permission to ignore the gap. It tells us two separate things:

- We can inspect how the calculation was executed.
- We do not know whether this simplified model represents a real place well enough for a real decision.

A navigation app can follow its map perfectly while confidently steering us toward a road that closed last Tuesday. Drainage models have the same distinction. Execution evidence answers, *Did the computation run as declared?* Site evidence answers, *Does this model represent the decision we actually face?*

Clean execution earns confidence in the calculation. It does not, by itself, earn confidence in the world the calculation describes.

<!-- ARTICLE_VIDEO key="app/model_fitness" -->

## Rain refuses to be one number

Now put the four synthetic storms to work. One plan goes in; four responses come out. Total event volumes can be combined because the teaching rule explicitly asks about aggregate reduction. Peaks cannot safely be blended into a soothing average: the guardrail asks whether **any individual storm** gets worse.

ALT-017 lowers two peaks and raises two. Its worst increase is 0.07 cubic feet per second in the long event. Averaging the peaks into one score could make that warning disappear. The spreadsheet would become calmer. The pipe would not.

> **Every summary is a choice about what to forget.**

## Give automation four dials, not the city

The software is not allowed to redesign Cedar Trace freely. It cannot change pipe sizes, invent soil properties, quietly move an outfall, or create a new rain-garden specification. Those variables remain outside the approved search question.

It can vary only four whole-number counts of additional units at four teaching locations:

- Planted bioretention units.
- Areas of permeable pavement.
- Rain-garden units.
- Groups of rain barrels.

Each location has a maximum count and a declared drainage destination. The engineering definition of each practice remains fixed. The map is a schematic, not surveyed geometry.

The four unit counts work like four dials with five, four, five, and seven settings. Multiplying those ranges produces 700 possible four-number lists, called **vectors**, including the unchanged baseline:

`5 × 4 × 5 × 7 = 700`

Testing all 700 vectors under all four storms requires 2,800 direct SWMM runs. That exhaustive study exists as a separate benchmark.

The guided Cedar Trace story is smaller. It contains 220 deterministically chosen alternatives, four storms per alternative, and four baseline runs. It is nonexhaustive, and it is not a demonstration that machine learning found a superior answer.

Imagine the 699 nonbaseline alternatives as face-down cards. A search method—including, in an appropriate workflow, a small ML proposer—may choose which card to turn over next. That choice is a question, not a hydraulic answer. The selected candidate still goes through SWMM. Only the direct model run supplies the values shown to the engineer.

> **Search decides where to look. SWMM supplies what we see.**

<!-- ARTICLE_VIDEO key="app/bounded_opportunities" -->

## Eight hundred eighty-four jobs and zero fake progress bars

Put the four storms across the top of a table. The unchanged model fills the first four cells—one baseline run for each storm. Each of the 220 alternatives then receives a row with four more cells.

`4 baseline runs + 220 alternatives × 4 storms = 884 direct SWMM runs`

Each cell records the result, its numerical check, and digital fingerprints for its files. The project also retains complete input, report, and detailed output files for the four baseline runs and for four teaching alternatives under four storms: 20 complete evidence sets.

The application can open this finished record immediately as a **verified fixture replay**. That means the saved files still match the recorded study. It does not mean simulations are running now.

Running all 884 jobs again would create a new experiment. A progress bar is a claim about the present tense. The application should show genuine progress only when it actually launches the expected SWMM executable.

> **A replay is a receipt, not a progress bar.**

<!-- ARTICLE_VIDEO key="app/verified_replay" -->

## Then the storms start turning over the cards

The guided study teaches the workflow without making a person wait for every possible combination. Useful. Pleasant, even. But the full 699-alternative fixture lets us ask the awkward follow-up: what did the guided hand leave face down?

For this fixture, every positive reported peak difference counted as adverse; a professional project needs its own explicit numerical tolerance. Across all 699 nonbaseline alternatives, 421 increase the outlet peak in at least one selected storm. The balanced one-inch event exposes one such alternative. The front-loaded two-inch event exposes none. The long 2.5-inch event exposes 420, while the deeper four-inch stress event exposes 47. Because 47 alternatives fail in two events, those storm counts overlap.

<!-- ARTICLE_ANIMATION key="animation/storm_reveals" -->

**What this establishes.** Inside this finite Cedar Trace model and candidate space, the selected storms expose materially different adverse peak-flow behavior.

**What this does not establish.** Depth, duration, and temporal shape changed together. The result does not prove that long storms are generally more important than deeper storms or that temporal pattern alone caused the difference.

<!-- ARTICLE_BENCHMARK key="benchmark/storm_guardrail_atlas" -->

This does not prove that a four-hour storm is generally more important than a deeper storm. Inside this declared model and candidate space, the four selected event definitions exposed different alternatives; depth, duration, and temporal shape changed together. Wider sewer-network research has found that temporal pattern can affect modeled peak flow, not only total depth ([Fadhel, Rico-Ramirez, and Han, 2018](https://doi.org/10.1016/j.jhydrol.2018.03.041)). Rainfall has choreography, and drainage networks can notice.

The guided 220 cover 31.5% of the nonbaseline universe, but only two of the twelve alternatives that pass every teaching rule. They contain 127 of the 421 adverse alternatives and none of the four alternatives on the exact feasible frontier.

<!-- ARTICLE_BENCHMARK key="benchmark/guided_coverage" -->

That makes the guided replay useful for learning how to inspect a result. It does not make its shortlist exhaustive. ALT-207 passed the class exercise. It did not win even this finite modeled universe.

## Four plans walk into a decision matrix

The most useful comparison does not begin with another scatterplot. It begins with an acceptance matrix that has the social disadvantage of remembering every declared rule.

### ALT-017: the charming bargain

ALT-017 costs about $55,000 and reduces aggregate outlet volume by just over 4%. It misses the 6% target. During the long storm, its modeled peak rises from 3.14 to 3.21 cubic feet per second, and the peak in a second storm rises too.

Cheap is not the same as acceptable. The dot remains charming. The rules remain unmoved.

### ALT-207: the one that clears the teaching screen

ALT-207 reduces aggregate volume by about 6.4% for roughly $190,000. None of its four modeled peaks rises, and every run passes its numerical checks.

That makes it a **candidate to carry forward for site and feasibility review**. It does not make ALT-207 approved, validated, recommended, design-ready, or ready to build.

### ALT-109: more modeled volume reduction, wrong budget

ALT-109 offers about 7% aggregate volume reduction. Its roughly $222,000 screening cost crosses the stated cap, so it does not clear the teaching brief.

### ALT-014: the volume maximalist

ALT-014 produces the strongest aggregate volume reduction observed among the 220 guided alternatives, about 8.8%. Its screening cost is roughly $362,000.

More modeled hydraulic benefit can still be the wrong fit for the decision. “More” is not a complete procurement strategy.

The application evaluates the unrounded stored values even when the article or screen uses rounded figures for readability. Among these four teaching examples, one clears every rule. That has earned us a smaller set of questions—not a design verdict.

## Pareto is a description, not a permission slip

Now the cost-versus-volume plot has a useful role: exploring tradeoffs.

On this plot, lower cost is farther left and greater volume reduction is higher. A tested plan is **nondominated** when no other tested plan is at least as cheap and at least as effective while improving at least one of those two measures. The visible edge of those points is a **Pareto frontier**.

The terms sound like verdicts. They are not. Nondominated describes the dimensions we chose to plot.

ALT-017 can sit on that visible edge because it offers an attractive combination of cost and aggregate volume reduction. Restore the 6% target and it falls short. Restore each storm's peak and two red increases appear. Restore the missing real-world fitness evidence and another uncertainty remains.

A flat plot is a shadow of a larger decision. Pareto status describes the dimensions on the axes; it does not erase rules that the plot omits.

<!-- ARTICLE_VIDEO key="app/frontier_guardrail" -->

## Six hundred ninety-nine alternatives enter; twelve survive the rules

The guided replay gave us a handful of instructive characters. The exhaustive benchmark gives every allowed combination the same unpleasant afternoon.

Cedar Trace has 700 possible vectors, including the unchanged baseline. Running each vector through four storms produced 2,800 direct SWMM jobs. Among the 699 alternatives, 297 meet the modeled 6% volume target. Only 46 also stay within the synthetic $200,000 cap. Then the peak-flow guardrail removes 34 of those 46. Just 12 satisfy the complete teaching contract, and four are nondominated within that exact feasible cost-volume set.

<!-- ARTICLE_ANIMATION key="animation/decision_funnel" -->

**What this establishes.** Restoring the cost, storm-specific peak, and QA requirements narrows the synthetic modeled shortlist from hundreds of alternatives to twelve complete-rule passes. Numerical execution QA removes none.

**What this does not establish.** Passing this screen does not prove site feasibility, field performance, construction value, or global optimality beyond the finite modeled universe.

<!-- ARTICLE_BENCHMARK key="benchmark/exact_decision_landscape" -->

This puts a number behind the opening lesson. Looking only at cost and volume produces a shortlist almost four times larger than the one obtained after restoring the storm-specific peak rule. All 699 alternatives passed the fixture's numerical execution checks, so this narrowing came from the decision rules—not broken solver jobs. The machinery worked. The criteria did the rejecting.

<!-- ARTICLE_BENCHMARK key="benchmark/rule_filter" -->

## So we gave four search methods the same exam

The practical question is simple: can a search expose acceptable plans and hidden adverse responses with fewer candidate requests?

Because we already know all 699 answers, we can turn the exhaustive table face down. A method picks one unseen vector; only then may it see that vector's stored four-storm result. The hidden table acts as the answer key, but search cannot inspect an unrequested outcome. Each candidate request therefore counts as four exact SWMM-job equivalents.

| Method | How it chooses the next candidate | Learns from revealed results? |
|---|---|---|
| Random | Shuffles the unseen candidates | No |
| Scrambled Sobol | Spreads requests across the bounded space | No |
| Integer NSGA-II | Evolves integer candidates using revealed tradeoffs, crossover, and mutation | Yes |
| Sobol + ExtraTrees | Uses a randomized-tree model to rank unseen candidates while reserving 25% of later batches for exploration | Yes |

The integer NSGA-II method follows [Deb and colleagues](https://doi.org/10.1109/4235.996017). ExtraTrees is described by [Geurts, Ernst, and Wehenkel](https://doi.org/10.1007/s10994-006-6226-1). All three Sobol-initialized methods begin with the same 64 designs.

The controls were deliberately strict: the same immutable dataset, the same three budgets, the same ten seeds, unique candidate requests, and the same exact scoring rules. Search could not inspect an unseen outcome. W&B recorded the replay; it did not influence candidate selection.

`4 methods × 3 budgets × 10 seeds = 120 replays`

No new hydraulic runs were performed. Reusing the immutable answer table avoids recomputing the same 2,800 SWMM outcomes for every method and seed. “Exact-job equivalent” is an accounting unit, not a measurement of energy or carbon savings.

The chart reports medians across ten seeds at budgets of 256, 512, and 1,024 exact-job equivalents. Because one candidate faces four storms, those budgets allow 64, 128, and 256 candidate requests. The shaded regions show the 10th–90th percentile rather than hiding seed-to-seed variation.

- **Hypervolume ratio** asks, *How much of the known feasible cost-volume tradeoff region did you recover?* A value of 1.0 matches that exhaustive region.
- **Feasible recall** asks, *How many of the twelve complete-rule alternatives did you find?*
- **Adverse recall** asks, *How many of the 421 alternatives with at least one peak increase did you expose?*

At the smallest budget, NSGA-II and ExtraTrees have not begun adapting: all three Sobol-initialized methods have seen the same 64 designs, so their identical results are expected. At 512 exact-job equivalents, NSGA-II has the strongest median hypervolume, while ExtraTrees exposes more adverse alternatives. At 1,024, both adaptive methods reach a median hypervolume ratio of 1.0. ExtraTrees finds a median 83.3% of the feasible alternatives and 46.2% of the adverse alternatives.

Those results are encouraging, but they are not a universal ranking. Random sampling even has a slightly higher median hypervolume than plain Sobol at the largest budget in this one fixture. The middle-budget ExtraTrees results have a wide lower tail. Hypervolume also describes only the feasible cost-volume projection; feasible and adverse recall answer different questions. Multiobjective-optimization research cautions against judging an approximation with one indicator alone ([Zitzler et al., 2003](https://doi.org/10.1109/TEVC.2003.810758)).

<!-- ARTICLE_BENCHMARK key="benchmark/search_efficiency" -->

## Great. Turn on adaptive search?

No.

The adaptive proposer was judged against release conditions fixed before we read the campaign result. It failed four of five:

- Its median hypervolume exceeded Sobol by at least 5% at only the largest, 256-design budget—not at two budgets.
- Its worst 10th-percentile hypervolume gap was 0.052 below Sobol, exceeding the permitted 0.02.
- It matched or exceeded Sobol's median feasible recall.
- It did not reach 95% adverse recall.
- Evidence came from one synthetic network, not at least three public networks.

One failure also reveals a problem in our gate. We had written a requirement that no method could possibly pass. At the maximum budget, a method may request only 256 designs, while the exhaustive table contains 421 adverse alternatives. Even a perfect selector could recover no more than:

`256 ÷ 421 = 60.8%`

Reaching 95% would require finding at least 400 adverse alternatives—already 1,600 exact-job equivalents before allowing a single nonadverse request. Asking a 256-request search to recover 95% of 421 adverse alternatives is not ambitious. It is arithmetic with a grudge.

The gate is mathematically impossible at the declared budget. We have to replace it with a budget-feasible target and freeze that correction before looking at another campaign.

<!-- ARTICLE_ANIMATION key="animation/search_release_gate" -->

**What this establishes.** ExtraTrees reaches a median hypervolume ratio of 1.000, 83.3% feasible recall, and 46.2% adverse recall at the largest declared budget, but passes only one of five release gates. A selector limited to 256 requests cannot recover 95% of 421 adverse alternatives; even a perfect selector can request at most 60.8% as many designs as that adverse set contains.

**What this does not establish.** This one-network replay does not establish ML superiority, live solver savings, carbon savings, or production readiness. Search chooses which candidate to inspect; direct SWMM evidence supplies every displayed hydraulic value. Energy use, kWh, and CO₂e were not measured.

<!-- ARTICLE_BENCHMARK key="benchmark/release_gate_audit" -->

The correct conclusion is not that ExtraTrees failed forever or that NSGA-II won. It is that adaptive ML has not earned production use. Scrambled Sobol remains the application's nonadaptive production search while the gate is corrected and the benchmark expands across public networks.

Prior work supports the shape of the problem, not the Cedar Trace answer:

- SWMM-linked multiobjective studies show why cost, peak flow, volume, and placement belong in the same decision problem ([Eckart, McPhee, and Bolisetti, 2018](https://doi.org/10.1016/j.jhydrol.2018.04.068); [Giacomoni and Joseph, 2017](https://doi.org/10.1061/%28ASCE%29WR.1943-5452.0000812)).
- Surrogate-assisted LID sizing can reduce optimization effort, but one catchment's result does not make predicted hydraulics universally reliable ([Yang et al., 2023](https://doi.org/10.1016/j.jhydrol.2022.128848)).
- Calibration studies show why clean SWMM execution cannot substitute for comparison with observations ([Rosa, Clausen, and Dietz, 2015](https://doi.org/10.1111/jawr.12272)).

These papers do not validate Cedar Trace, its synthetic storms, costs, or field realism.

### The small box this benchmark fits inside

- Cedar Trace is one small synthetic network with four synthetic storms and one fictional cost basis.
- The search space contains only four bounded integer decisions.
- Ten seeds measure variation within this experiment; they are not external validation.
- The replay uses a frozen exact-results table, so it does not measure live SWMM runtime, queue behavior, energy, or failure recovery.
- Any positive reported peak difference was counted as adverse. A professional project needs an explicit numerical precision and tolerance policy.
- The exact feasible frontier is exact only for this finite modeled universe and these declared objectives—not for field conditions or unmodeled alternatives.

## Turn `+0.07 cfs` into a receipt

Consider one red result: `+0.07 cfs`.

Hand that number to another engineer and the first question should be, *Compared with what?* This is a healthy response to most numbers wearing no identification.

The answer is the long storm: 3.14 cfs in the unchanged model and 3.21 cfs in ALT-017. From there, the evidence chain continues:

- ALT-017 adds one rain-garden unit and six rain-barrel groups, stored as the vector `[0, 0, 1, 6]`.
- The application writes that declared change into a SWMM input file.
- The pinned SWMM version executes the storm case.
- The project keeps the input, report, and detailed output.
- Digital fingerprints identify those files.
- The numerical check records whether the execution was accepted.

That chain is **provenance**: a path from a claim back to what created it. In GI Scenario Studio, the Evidence Rail exposes that path for each selected result—the plan counts, cost basis, before-and-after storm values, run identity, model changes, retained files, hashes, checks, assumptions, and limitations.

A digital fingerprint is a receipt, not a halo.

Provenance tells another person how a number was made. It does not prove the underlying model matches the real world.

The interface replay opens deterministic evidence for ALT-017's two peak increases—`3.14→3.21 cfs` in the long event and `13.33→13.34 cfs` in the stress event. It also shows that ALT-207's synthetic screen does not establish water quality, groundwater recharge, habitat or heat benefit, real-world flood reduction, maintenance performance, or lifecycle impact. The generated Guide remains disabled because its release checks failed. No model call is made, and no canned AI answer is substituted.

<!-- ARTICLE_VIDEO key="app/evidence_rail" -->

## Then we tried to make the receipt talk

A receipt can tell us exactly what happened. It cannot always tell a busy engineer where to look first. So we tried adding a conversational Evidence Guide inside the rail: *Why did this alternative fail? Which storm controls it? What should I inspect next?*

This is the point in the story where “add an AI” becomes very tempting. The evidence is already structured. The questions are bounded. Weave—W&B's tracing system—will preserve the calls and tool activity. The traces will be beautiful. What could possibly go wrong?

The answer needed a better test than *Did the model produce valid JSON while looking thoughtful?* We asked three behavioral questions:

- If the deciding fact crosses a declared rule, does the explanation change?
- If only an unrelated fact changes, does the explanation stay put?
- If the needed evidence disappears, does the Guide admit that it cannot decide?

We gave two language models exactly that exam. Each case appeared twice with the evidence labels and order shuffled, so memorizing a position or label could not count as success. A case counted as correct only when both presentations behaved correctly. Repository-owned rules without a language model supplied the expected comparison.

This is the essential distinction:

> **A trace can prove that an explanation ran. It cannot prove that the explanation listened to the evidence.**

<!-- ARTICLE_EVALUATION key="evidence-guide/r4c-grounding" -->

## What stays useful after the release gate says no

All 672 model calls returned. All 672 received Weave trace acknowledgements. The held-out evidence-following result still failed the release gate.

This is not a story about observability failing. It did its recording job as designed: preserving calls, tools, latency, outputs, and failure paths. The mistake would be asking that record to certify something it does not measure.

The negative result does not leave the engineer with nothing. It tells us which part of the product can be trusted today.

- The deterministic Evidence Rail remains available.
- Repository-owned rules, values, units, limitations, and next actions remain authoritative.
- Weave remains useful for inspecting calls, tools, latency, and failure paths—but not as a correctness certificate.
- Generated explanations remain disabled until a future configuration passes a new, sealed evaluation.

No assistant answer was released during this study. Practitioner usefulness, retrieval quality, prompt-injection resistance, hydraulic validity, ecological benefit, and field decision quality were not measured.

Method note: the controlled changes follow the behavioral-testing idea behind [CheckList](https://aclanthology.org/2020.acl-main.442/). We limit the conclusion to this fixed benchmark rather than presenting it as generalized model accuracy, following [NIST AI 800-3](https://www.nist.gov/publications/expanding-ai-evaluation-toolbox-statistical-models).

Even a perfect explanation could only be as sound as the evidence underneath it. Later, the Fehraltorf case study shows how much the hydraulic conclusion itself can move when the model boundary changes.

## The failed attempts stay in the record

The completed experiment is easier to interpret because the earlier failures remain visible. Broken experimental plumbing must not be misreported as model behavior.

The first 48-case screen found that two candidate models missed required limitations and actions. A separate three-turn Weave Agents probe showed that the application rejected invalid generated answers rather than displaying them—what **fail closed** means here. Those results established containment behavior, not explanation quality.

<!-- ARTICLE_EVALUATION key="evidence-guide/v1" -->

Audit records:

- [Granite evaluation](https://wandb.ai/wandb/gi-scenario-studio-article/r/call/019fd964-7ed6-7581-95ea-6ebc75a13be1) and [GPT-OSS evaluation](https://wandb.ai/wandb/gi-scenario-studio-article/r/call/019fd964-809f-7bc0-b147-d6ca0b8ca67a).
- [Two-turn ALT-017 investigation](https://wandb.ai/wandb/gi-scenario-studio-article/weave/agents/conversations/assistantconversation_58cfd7fc090a352803e27990) and [ALT-207 ecological-boundary investigation](https://wandb.ai/wandb/gi-scenario-studio-article/weave/agents/conversations/assistantconversation_d103df23fa3aed51516cce15).

They are failure evidence, not approval evidence.

The first campaign reported provider tokens and client-observed latency, but not electricity or carbon. Provider hardware, power, and grid intensity were not measured, so it supports no kWh or CO₂e claim.

### The four-case gate stopped the next study

We tried again. The next study was supposed to compare deterministic guidance with model-assisted architectures, but its preregistered four-case admission gate stopped the work before comparison. Granite selected a hydraulic-failure explanation for a question that required an authority boundary. The repository validator caught the wrong plan, wrong-subject evidence, missing limitations, and missing actions. No generated answer was released.

Because the campaign stopped before the comparison stage, the architecture denominator is zero. It would be incorrect to say that the deterministic guide scientifically beat the model-assisted arm. There was no completed comparison to win. The result establishes fail-closed rejection, not comparative architecture efficacy.

Audit records: [deterministic run](https://wandb.ai/wandb/gi-scenario-studio-agent-evals/runs/1d6de8815e2da8b3f11dfd55), [Granite run](https://wandb.ai/wandb/gi-scenario-studio-agent-evals/runs/53d3bcaf9ce116e903e74c9e), and [Weave trace](https://wandb.ai/wandb/gi-scenario-studio-agent-evals/r/call/019fda8d-2cda-75e6-b2b1-e1ff38143e8c).

<!-- ARTICLE_EVALUATION key="evidence-guide/v2" -->

## Then a real catchment changed the size of the warning label

Cedar Trace taught us that an explanation can ignore the evidence. Fehraltorf, Switzerland, supplies the next uncomfortable question: what if the explanation follows the evidence perfectly, but the answer moves when the model boundary moves? Fehraltorf's studied area uses a **combined sewer**, where stormwater and sanitary sewage share pipes.

In published SWMM outputs for this real combined-sewer catchment, the modeled bioretention reduction changes from **56.8% to 92.2%** depending on which processes the model represents. That is a spread of **35.4 percentage points**. Same green-infrastructure scenario. Different accounting of evapotranspiration, boundary inflow, groundwater, or all three together. The rain garden did not change between rows. The modeled world around it did.

Now the essential boundary: This was **not a fresh SWMM rerun**. The Eawag research package covers thirty years, but the required MeteoSwiss rainfall and evapotranspiration inputs are not redistributed with the open package.

We verified the public archive hashes, read the authors' retained result tables and SWMM reports, and recomputed median annual combined-sewer-overflow volume with the published method. This is an independent check of published outputs—not an independent reproduction of the hydraulic simulation.

The paired-year calculation first subtracts each GI year's overflow from the matching no-GI year, takes the median of those paired differences, and divides by the median no-GI volume. It is not the ratio of the two displayed medians.

| Model structure | No-GI median annual overflow | Bioretention median annual overflow | Paired-year modeled reduction |
|---|---:|---:|---:|
| Base case | 142,180 m³/year | 24,505 m³/year | 82.8% |
| Adds evapotranspiration | 121,130 m³/year | 21,991 m³/year | 83.2% |
| Adds boundary inflow | 209,185 m³/year | 43,494 m³/year | 76.9% |
| Adds groundwater | 124,568 m³/year | 5,869 m³/year | 92.2% |
| Combines all three processes | 183,032 m³/year | 69,957 m³/year | 56.8% |

That range does not mean bioretention physically changed between rows. The accounting boundary changed. Water can enter, leave, or return to the modeled system through processes that a simpler structure omits.

There is another reason not to promote the largest percentage into a victory banner. The combined-process bioretention report records a **−54.273% groundwater continuity error**. Continuity is a mass-balance check: the water entering, leaving, and remaining in that part of the model should reconcile under the project's declared QA policy. The error does **not** mean the overflow result is exactly 54.273% too high or too low. It means the groundwater ledger did not close, so the result needs engineering scrutiny before it supports a decision.

<!-- ARTICLE_CASE_STUDY key="realistic-fehraltorf/v1" -->

The report also exposes the computation itself. A linked W&B run holds the twenty-case table, chart, and immutable evidence artifact; a nested Weave trace records the comparison, retained-report QA inspection, and bounded-claim step. No language model is involved in that trace. Weave makes the reasoning path inspectable; it does not make the hydraulic result more true.

The source [dataset](https://doi.org/10.25678/0009PE) is published under CC0, and the associated [Water Research paper](https://doi.org/10.1016/j.watres.2024.121284) explains the study design. The report table is generated from the hash-pinned case record rather than copied by hand.

**What this establishes.** In the published Fehraltorf outputs, estimated overflow reduction varies by 35.4 percentage points across the five model structures. A defensible screening tool should show the model boundary and execution-quality findings beside the benefit metric, not several screens later.

**What this does not establish.** This reanalysis does not establish field performance, calibration fitness for a new decision, regulatory suitability, ecological benefit, water-quality improvement, groundwater recharge, avoided flooding, or construction readiness. It also does not replace the fresh solver reproduction that the unavailable meteorological inputs currently prevent.

## The deliverable is a smaller set of expensive questions

The final product is not a winning dot. It is a handoff another engineer can challenge.

An evidence package carries the original question, the rules fixed in advance, the alternatives compared, the modeled results, the direct runs behind those claims, and the limitations that still matter. It records which cases retain complete input, report, and detailed output files. A good export does not make the conclusion larger. It makes the work easier for the next person to inspect. Bounded humility, now available as a ZIP file.

The local SQLite database and content-addressed files remain the authoritative engineering record. When its access policy has been verified, an exact `Restricted` W&B project can mirror that evidence for observability and collaboration. If that policy cannot be verified, synchronization should remain blocked while the local evidence continues to work. The cloud mirror does not become the hydraulic authority.

For Cedar Trace, ALT-207 passes the synthetic teaching screen. The next questions live outside the computer:

- Is there enough physical space?
- Where are the utilities?
- Will the soil accept water at the assumed rate?
- Who will maintain each practice?
- Is the alternative constructible?
- Are the storms, costs, model topology, and assumptions appropriate for the real decision?

Good screening does not manufacture certainty. Its intended value is to focus field investigation on a smaller, better-explained set of candidates while preserving why those candidates survived the screen.

ALT-207 is only a candidate to carry forward for site and feasibility review.

<!-- ARTICLE_VIDEO key="app/engineer_handoff" -->

## The engineer gets the last word

We began with a dot that looked like a bargain. The useful outcome was not replacing it with a more impressive dot. It was preserving the questions the plot could not answer.

GI Scenario Studio is not valuable because it names an automatic winner. Its value is the ability to say:

- Here are the alternatives we tested.
- Here are the rules we chose before seeing the results.
- Here is why each alternative passed or failed.
- Here are the direct model runs behind every hydraulic claim.
- Here are the assumptions and unresolved questions that limit what those claims mean.

A practitioner can repeat this workflow with a fit-for-purpose SWMM project, locally appropriate storm definitions, engineer-defined opportunity locations, and a defensible local cost basis. The conclusions will still depend on that model and its assumptions. But the result is no longer a mysterious point on a chart. It is a claim another engineer can open, question, and trace.

The useful time to expose a hidden assumption is before detailed design, permitting, and construction make it expensive.

**SWMM computes. Search allocates attention. Evidence preserves the path. The engineer decides.**

## If you want to reproduce, challenge, or extend this

The links below do different jobs: some document the model and data, some provide scientific context, and some expose the observability machinery. None is a substitute for reading the evidence attached to the claim you care about.

### Stormwater modeling and data

- [EPA SWMM software, source, and documentation](https://www.epa.gov/water-research/storm-water-management-model-swmm)
- [SWMM 5.2 User's Manual](https://www.epa.gov/system/files/documents/2022-04/swmm-users-manual-version-5.2.pdf)
- [EPA Green Infrastructure Modeling Toolkit](https://www.epa.gov/water-research/green-infrastructure-modeling-toolkit)
- [NOAA Precipitation Frequency Data Server](https://hdsc.nws.noaa.gov/pfds/)

### Green infrastructure and optimization

- [EPA: potential environmental benefits of green infrastructure](https://www.epa.gov/green-infrastructure/environmental-benefits-green-infrastructure)
- [EPA: green infrastructure and groundwater protection](https://www.epa.gov/green-infrastructure/green-infrastructure-and-groundwater-protection)
- [Multiobjective optimization of low-impact-development stormwater controls](https://doi.org/10.1016/j.jhydrol.2018.04.068)
- [Multi-objective placement of low-impact development](https://doi.org/10.1061/%28ASCE%29WR.1943-5452.0000812)
- [Calibration and verification of SWMM for LID](https://doi.org/10.1111/jawr.12272)
- [Rainfall temporal patterns and modeled peak flow](https://doi.org/10.1016/j.jhydrol.2018.03.041)
- [Surrogate-based multiobjective LID sizing](https://doi.org/10.1016/j.jhydrol.2022.128848)

### Life-cycle boundaries for stormwater infrastructure

- [Life-cycle assessment of a bio-infiltration rain garden](https://doi.org/10.1016/j.ecoleng.2013.01.004)
- [Life-cycle comparison of green and grey stormwater systems](https://doi.org/10.1111/1752-1688.12320)
- [Life-cycle assessment of stormwater treatment systems](https://doi.org/10.1016/j.jenvman.2014.10.025)

### Search evaluation and responsible computation

- [NSGA-II](https://doi.org/10.1109/4235.996017)
- [Why multiobjective algorithms require multiple quality measures](https://doi.org/10.1109/TEVC.2003.810758)
- [Extremely randomized trees](https://doi.org/10.1007/s10994-006-6226-1)
- [Green Algorithms: measuring computational footprints](https://doi.org/10.1002/advs.202100707)
- [Systematic reporting of ML energy and carbon footprints](https://jmlr.org/papers/v21/20-312.html)

### Reproducing and inspecting the campaign

- [W&B Tables](https://docs.wandb.ai/guides/tables/)
- [W&B Artifacts and lineage](https://docs.wandb.ai/models/artifacts/)
- [W&B Reports](https://docs.wandb.ai/models/reports/)
- [Weave Datasets](https://docs.wandb.ai/weave/guides/core-types/datasets)
- [Weave Evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations)
- [Weave EvaluationLogger](https://docs.wandb.ai/weave/guides/evaluation/evaluation_logger)
- [Weave Agents tracing](https://docs.wandb.ai/weave/guides/tracking/trace-agents)
- [W&B Inference structured output](https://docs.wandb.ai/inference/response-settings/structured-output)
- [W&B Serverless Inference model catalog](https://docs.wandb.ai/inference/models)
