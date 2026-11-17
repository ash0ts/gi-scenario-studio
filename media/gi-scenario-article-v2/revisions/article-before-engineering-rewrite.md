# We Built an AI to Explain the Evidence. Then We Checked Whether It Was Listening.

*A stormwater Evidence Guide, 672 acknowledged Weave traces, and the awkward gap between seeing a fact and changing an answer.*

We had built the part of the demo that usually gets the applause.

We were building the gated explanation path for a simple interaction: an engineer opens a stormwater result, asks why it failed, and gets a plain-English explanation linked to the underlying evidence. The Guide was read-only. It had bounded tools. The facts came from our repository. We had assembled enough responsible-sounding nouns to feel pretty good about ourselves.

Then we changed one number.

In a synthetic test we had not used to tune the prompt, the deciding fact was how close pipe `C-19` was to its declared capacity. The rule required the ratio to stay at or below `0.85`. It began at `0.79`. We changed it to `0.91`, so the disposition should have flipped from within the rule to a violation.

The model chose the same response structure both times.

Both calls produced acknowledged Weave traces.

That was the useful failure. The traces preserved the evidence, provider response, selected structure, and validator result. They did not make the selection correct.

<figure>
  <picture>
    <source media="(max-width: 680px)" type="image/svg+xml" srcset="figures/01-counterfactual-mobile.svg">
    <source type="image/svg+xml" srcset="figures/01-counterfactual.svg">
    <img src="figures/01-counterfactual.png" alt="Two held-out evidence cards show conduit capacity ratio C-19 changing from 0.79, which meets a less-than-or-equal-to 0.85 rule, to 0.91, which fails it. Both calls produced acknowledged traces and selected the same explanation plan. No answer was released.">
  </picture>
  <figcaption><strong>One representative held-out failure.</strong> The deciding evidence crossed the rule, while the selected plan stayed fixed. This is one trace-bound synthetic case, not an estimate of universal model behavior.</figcaption>
</figure>

> **A trace can prove that an answer happened. It cannot, by itself, prove that the answer followed the evidence.**

## Why we wanted the Guide in the first place

GI Scenario Studio is a research prototype for screening green-infrastructure alternatives. Green infrastructure here means things such as rain gardens, permeable pavement, planted storage, and rain barrels that can store, slow, redirect, or infiltrate stormwater.

The hydraulic calculations come from the US Environmental Protection Agency’s [Storm Water Management Model, or SWMM](https://www.epa.gov/water-research/storm-water-management-model-swmm). An engineer gives SWMM a simplified drainage world—catchments, pipes, junctions, outlets, rain—and SWMM calculates how water moves through that world. The output can be reproducible and numerically precise. It is still a model result under declared assumptions, not a field measurement or an approval to build something.

Our synthetic teaching project, Cedar Trace, contains a deliberately tempting alternative called `ALT-017`. On a cost-versus-volume chart, it looks like the kind of point that gets invited to the executive summary. Its fictional present-value screening cost is `$54,990`, and it reduces modeled four-event outlet volume by `4.14%`.

Unfortunately, the decision brief requires at least `6%`. The same alternative also raises the modeled outlet peak in two of the four selected storms: `3.14 → 3.21 cfs` in the long event and `13.33 → 13.34 cfs` in the stress event. All four runs passed the fixture’s declared numerical checks; the alternative still failed the decision screen.

This is exactly the sort of evidence that is easy to read badly. The cheap point is real inside the synthetic study. So are the missed target and the two adverse peak changes. We hoped a conversational layer could help an engineer ask:

- Why does this inexpensive alternative fail?
- Which storm exposes the largest problem?
- How can a point look efficient on a two-axis plot and still fail the full brief?
- What does the model result establish, and what still needs to be checked in the field?

The existing Evidence Rail can answer those questions deterministically. It exposes the rule, storm, run, files, hashes, assumptions, and limitations. But it asks the reader to assemble the story. We wanted the Evidence Guide to reduce that reading work without quietly acquiring engineering authority.

That last clause did a lot of work.

## We made the model’s job almost embarrassingly small

The first version of the idea was a normal tool-using assistant: inspect bounded evidence, write an explanation, cite the evidence, and suggest sensible next checks. The more we looked at the authority problem, the less normal that felt.

If the model wrote free prose, it could copy a value incorrectly, omit the limitation that made the value interpretable, or add a recommendation the evidence did not support. A very eloquent drainage hallucination is still a drainage hallucination. It is just harder to interrupt.

So we shrank the job.

In the intended product path, SWMM owned the modeled hydraulic values. The repository owned the facts, units, rules, limitations, and the fixed set of next checks the Guide was allowed to suggest. Read-only tools could retrieve a small evidence packet. The model could select one explanation plan from a finite registry. Repository code then inserted the canonical values into that plan, and a deterministic validator checked whether the selection matched the evidence. Weave recorded the provider calls, latency, token use, and validator result.

Our fourth controlled run, which the artifacts call R4c, started one step later, with prepared synthetic evidence packets. It tested interpretation after retrieval; it did not test whether a production retrieval layer found the right evidence.

The model could not change the drainage model, accept a quality finding, approve an alternative, invent a hydraulic value, or decide what should be built. It could choose how to explain a bounded record. We thought reducing its freedom might reduce the ways it could be wrong.

That was reasonable. It was also not enough.

<figure>
  <picture>
    <source media="(max-width: 680px)" type="image/svg+xml" srcset="figures/04-authority-map-mobile.svg">
    <source type="image/svg+xml" srcset="figures/04-authority-map.svg">
    <img src="figures/04-authority-map.png" alt="An intended product architecture shows SWMM results, repository-owned facts and rules, read-only evidence tools, a model choosing one explanation plan, and a combined repository renderer and validator with exclusive release and stop branches. A study marker shows that R4c began with a prepared packet after retrieval. Weave is a separate observation layer.">
  </picture>
  <figcaption><strong>The intended authority boundary.</strong> R4c began with a prepared packet, so retrieval quality was outside the experiment. Weave recorded the provider-and-validation path; it did not supply hydraulics, certify the explanation, or make the engineering decision. The study took the stop branch.</figcaption>
</figure>

## Fluency was the wrong exam

The obvious test would have been to ask whether the explanations sounded good. That would also have been the easiest way to congratulate ourselves.

Instead, we tested whether the selected plan changed for the right reason.

Each synthetic scenario gave the model a question, a simple decision rule, a target fact, several unrelated facts, and a small set of allowed plan IDs. We then made three controlled changes:

1. Change the target value so it crosses the rule. A grounded explanation should change its plan.
2. Change only an unrelated distractor. A grounded explanation should stay put.
3. Remove the target evidence. A grounded explanation should abstain.

We also presented each scenario twice with the evidence order shuffled and the IDs remapped. A scenario passed only if both presentations behaved correctly. This matters because succeeding when the right fact happens to be first in the list is not quite the scientific breakthrough we were hoping for.

<figure>
  <picture>
    <source media="(max-width: 680px)" type="image/svg+xml" srcset="figures/02-listening-tests-mobile.svg">
    <source type="image/svg+xml" srcset="figures/02-listening-tests.svg">
    <img src="figures/02-listening-tests.png" alt="Three controlled evidence tests show that crossing a target rule should change the plan, changing only an unrelated fact should keep the plan, and removing the target should cause abstention. Each case must pass two shuffled presentations.">
  </picture>
  <figcaption><strong>The listening test.</strong> A polished answer cannot pass by merely remaining polished. It has to respond to the deciding fact, ignore irrelevant movement, and stop when the needed evidence disappears.</figcaption>
</figure>

This was a controlled behavior test: change one relevant part of the input and check whether the selected plan changes with it. It does not prove the model “understands” drainage. It asks a narrower and more useful question: did this fixed configuration respond to this bounded evidence?

Repository-owned logic supplied the expected behavior. We did not use another language model to grade the safety-critical result. An LLM judge can be useful for comparative style or readability, but “the number crossed the declared threshold” is a rule we can evaluate exactly. Inviting a second model to debate it would have been adding a committee where an `if` statement was available.

## Every call returned. The behavior still failed.

The completed R4c study made `672` provider calls across two stages: `384` evidence-following attempts and `288` response-form diagnostics. Every call returned. Every call received a Weave trace acknowledgement. There were no runtime retries.

Operationally, this was lovely.

On the held-out scenarios—the ones kept out of prompt tuning—it was not enough. The two tested model configurations, Granite and Qwen, passed all three required behaviors in `0 of 12` and `3 of 12` scenarios respectively. The pieces failed differently:

- When the target crossed its rule, Granite changed correctly in `1 of 12` held-out scenarios and Qwen in `5 of 12`.
- When only a distractor changed, the plan stayed correctly stable in `6 of 12` for Granite and `7 of 12` for Qwen.
- When the target disappeared, both models abstained correctly in `10 of 12`.

A scenario counted as a pass only when all three behaviors worked: change for the deciding fact, ignore the distractor, and abstain when the fact is missing. Both shuffled presentations had to work too. We did not average a strong abstention score into a weak threshold score and call the result “mostly grounded.”

A separate deterministic positive control passed `192 of 192` scheduled protocol cells. That tells us the repository-owned rule machinery could express the expected behavior for those cells. It does **not** prove that deterministic copy is more useful to practitioners, because we did not run that human comparison.

<figure>
  <picture>
    <source media="(max-width: 680px)" type="image/svg+xml" srcset="figures/03-two-ledgers-mobile.svg">
    <source type="image/svg+xml" srcset="figures/03-two-ledgers.svg">
    <img src="figures/03-two-ledgers.png" alt="The operational ledger shows 672 of 672 provider responses and 672 of 672 trace acknowledgements. The held-out behavior ledger shows Granite passing all three grounding mechanisms in zero of twelve scenarios and Qwen in three of twelve. A separately labeled deterministic control passes 192 of 192 cells.">
  </picture>
  <figcaption><strong>Two ledgers, two results.</strong> The complete call and trace record made the negative result clean and inspectable. It did not give us an explanation feature to ship.</figcaption>
</figure>

The provider-and-tracing path worked. Our first W&B Table publication did not. A mapping-schema mismatch raised a `TypeError` after all `672` calls had finished. The attempt records and traces survived, and we rebuilt the publication with `0` additional provider calls. Apparently the observability pipeline wanted its own failure column.

We found one boring model-interface issue too. A `128`-token cap completed the strict response form in only `1 of 36` Granite cells and `11 of 36` Qwen cells. At `256` tokens, both completed `36 of 36`. The larger box fixed form completion. It did not fix evidence-following. We repaired the envelope; the letter inside still had to pass its own test.

This distinction—between form completion, operational reliability, and evidence-following—became the whole point of the study. A single “agent quality” score would have hidden it.

## What the trace actually gave us

The Weave traces preserved the question, evidence packet, selected plan, provider response, usage, latency, and validator outcome. In the `0.79 → 0.91` pair, the [original trace](https://wandb.ai/wandb/gi-scenario-studio-agent-evals/weave/agents/conversations/eval-b947d0f5f6cc-b4b4f0b55e0be263f769ab671df06e51) and [changed trace](https://wandb.ai/wandb/gi-scenario-studio-agent-evals/weave/agents/conversations/eval-b947d0f5f6cc-d08af8ce08a97ab002c5a27177fde786) let us inspect the exact failure. The deciding value was present. The threshold was present. The plan stayed the same.

Without that record, we might know only that an aggregate score was low. With it, we could see what kind of failure occurred, reproduce it, and connect it to a product decision. This is what observability is good at: turning “the agent did something weird” into a bounded claim about one recorded execution.

What the trace did not do was certify the relationship between input and output. A trace is closer to a flight recorder than an airworthiness certificate. It can tell us which controls moved, which warnings appeared, and what the system produced. Somebody still has to decide whether that behavior was acceptable.

We attached deterministic scores to the recorded executions and published the result through W&B. The scores meant something only because the experiment supplied fixed rules, controlled changes, correct denominators, held-out project families, and a stop condition written before we saw the result. Observability made the evidence inspectable. Experimental design made it meaningful.

## So we kept the boring version

We did not release a generated answer.

We kept the deterministic Evidence Rail available and disabled the live Guide. The product decision was `DISABLE_LIVE_GUIDE`, and the study recorded `0` released answers.

This is less cinematic than watching an agent narrate a hydrograph. It is also the feature working as designed.

In the Cedar Trace product path, the repository remains the value authority because it owns the facts, rules, units, and limitations. SWMM remains the authority for the fixture's modeled flow and volume. The engineer remains the decision authority because neither the simulator nor the assistant can inspect the real site, locate utilities, test soils, negotiate maintenance ownership, or accept professional responsibility.

Weave remains in the system because the next failure should be easier to find than the last one. It audits the explanation process. It does not promote the explanation into engineering evidence.

We built the shiny interface and ended up keeping the stop condition. I trust the product more because the failed gate changed what a user could see.

## What this experiment does not tell us

The study measured synthetic, post-retrieval evidence behavior for two fixed model configurations under one bounded protocol. It did not measure:

- whether practicing engineers find the Guide useful;
- whether the evidence retrieval layer finds the right record in a real project;
- prompt-injection resistance across the full application;
- hydraulic accuracy or model fitness;
- ecological benefit, flood-risk reduction, or field performance;
- regulatory suitability or design readiness;
- general model intelligence, safety, or quality.

Those are not legal disclaimers stapled onto an otherwise universal conclusion. They are the edges of the experiment.

The result supports one narrower claim: every scheduled call returned and received a trace acknowledgement, but the tested configurations did not reliably change plans when the supplied evidence changed. They did not meet our release rule. That was enough to change what we shipped.

This matters especially in environmental engineering because modeled quantities sit inside longer evidence chains. EPA notes that green infrastructure can support runoff capture, water quality, groundwater, habitat, and heat mitigation. Those are separate claims with separate evidence needs. A runoff-accounting number cannot establish all of them by changing fonts.

The assistant therefore has to preserve what is not established, not merely summarize what is.

## Reproduce or challenge the result

- The source and experiment code live in the [GI Scenario Studio repository](https://github.com/ash0ts/gi-scenario-studio).
- The article’s machine-readable evidence projection is `media/gi-scenario-film/dist/evidence-guide-r4c/article-summary.json`.
- The complete attempt records are under `dist/evidence-guide-r4c-grounding-final-r1/live/publication/`.
- Cedar Trace is bound to `apps/desktop/src/data/guidedFixture.json` and the retained exact-run fixture under `benchmarks/guided_exact/`.
- The limitations and later architecture-first release policy are in `docs/evidence-guide-v2-study.md`.
- W&B documents the scope of [Weave trace trees](https://docs.wandb.ai/weave/guides/tracking/trace-tree). EPA documents the site questions behind [green infrastructure and groundwater protection](https://www.epa.gov/green-infrastructure/green-infrastructure-and-groundwater-protection).

The figures here are generated from the checked-in evidence projection and fail their build if the source values drift. The editable SVGs, mobile artboards, raster fallbacks, alt text, source hashes, and verification scripts sit beside this draft.

## The next version has to earn speech

The next experiment should not begin with a majestic prompt rewrite. It should begin with the deterministic guide as a baseline and a sealed set of cases the model has not helped us debug.

Any model-assisted version should have to demonstrate:

- the correct plan change when deciding evidence crosses a rule;
- stability when irrelevant evidence changes;
- abstention when required evidence is missing or contradictory;
- exact binding to the current project, revision, subject, and evidence IDs;
- reliable failure when the provider or trace acknowledgement is unavailable;
- usefulness to practicing engineers, measured with practitioners rather than inferred from our enthusiasm.

If a finite plan selector cannot clear those gates, free prose is not the next architecture. A smaller model is not automatically the answer either. The deterministic rail is allowed to win.

That last sentence is easy to say and surprisingly clarifying to build around. Once the existing product is allowed to remain in place, an AI feature has to create measurable value rather than merely create AI.

We kept the evidence. We kept the traces. We turned off the generated answer.

The model can try again when it shows that changing the evidence changes what it says.
