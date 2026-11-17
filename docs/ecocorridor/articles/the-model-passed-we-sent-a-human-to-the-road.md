# Why I Built EcoCorridor

_Building a wildlife-connectivity review tool, then testing whether an agent had a useful job in it._

I started EcoCorridor because I wanted to build environmental software that an engineer or ecologist could actually use. I was also looking for a useful role for AI. Those questions eventually led to a fairly specific job: help someone compare wildlife-crossing and habitat-restoration concepts, inspect the assumptions behind the results, and decide what needs further investigation.

The audience I have in mind is a transportation environmental team working with wildlife specialists and ecological consultants. They might be considering a crossing with guidance fencing, restoration around its approaches, or a combination of interventions at several sites. Before any of that becomes a design, someone has to compare the concepts, check their effects under different modeling assumptions, and work out which unanswered questions matter.

EcoCorridor Scenario Studio organizes that review around a selected bundle of interventions. It keeps the modeled results beside the costs, safeguard checks, assumptions, and source files. We built a fictional landscape called Ridgeway Crossing to exercise the workflow. That let us publish the inputs and calculations without exposing sensitive wildlife data. It also meant we had to be careful about what the example could demonstrate.

The result is a working synthetic review case and an experimental agent. We have not yet shown that either makes practitioners faster or more accurate. I want someone reading this to be able to follow the implementation and see where our first agent experiment fell short.

## Start with a model we can inspect

We used [Circuitscape](https://docs.circuitscape.org/Circuitscape.jl/latest/usage/) for the connectivity calculations. It treats a landscape as an electrical network: cells have resistance values, and selected habitat areas serve as endpoints. Those resistance values encode assumptions about how difficult movement is through the landscape. An ecologist has to supply and justify those assumptions; the solver cannot discover them from a map.

For each pair of habitat areas, Circuitscape calculates **effective resistance** and stores the results in a matrix—a table of values for the habitat pairs. In our screening rules, a decrease relative to the no-action baseline is favorable under that particular model. Circuitscape can also produce a **current map**, a grid of values called a raster, showing how modeled electrical current is distributed. That is useful for inspecting where a proposed change affects the calculation, but it is not a record of animals moving through the landscape.

Keeping those two outputs separate became an important design choice. A pairwise result answers a question about the connection between endpoints. A current map lets us inspect particular cells along the way. We wanted a reviewer to see both, without treating either as evidence of crossing use or collisions avoided.

Ridgeway has four habitat cores separated by a highway, eight intervention sites, and three anonymous animal groups. Two groups are the intended targets; the third is a safeguard group whose modeled connectivity must not worsen. We authored four resistance hypotheses for each group, giving us 12 required scenarios. These are synthetic test assumptions, not independently validated ecological models.

We then defined 128 portfolios, including no action. A **portfolio** is one bundle of site options, such as a crossing concept at one site and approach restoration at another. Each portfolio receives the entire set of scenarios. That avoids comparing an option tested under all the assumptions with one that happens to have results only for the easier cases.

One portfolio evaluated under one scenario is one direct Circuitscape job. The study therefore contains `128 × 12 = 1,536` matrix jobs. We ran the baseline and four teaching portfolios again with current-map output enabled, adding `5 × 12 = 60` jobs. The retained package contains 1,596 direct jobs in total; a job can produce multiple output files.

That is enough evidence to test whether the software can preserve a complete comparison and make its calculations inspectable. It does not make Ridgeway a field study. The synthetic inputs, rules, and numerical results all remain available for someone who wants to question how we constructed it.

## Following one candidate through the review

The candidate used here combines four authored site actions: approach restoration, a crossing-and-approach concept, a crossing with guidance fencing and end treatments, and a composite package that combines effects within one site option. Its synthetic screening cost is $400,000, against a $500,000 cap.

Our target rule requires at least a 3% reduction in effective resistance for every required target pair. The safeguard rule permits no increase for any required safeguard pair. The candidate passed those pairwise checks across its complete 12-scenario ensemble. Its least favorable safeguard result was still a 1.6031% reduction relative to no action.

We also configured a separate review rule around the highway. Before the study, we designated a fixed 48-cell road-review area and set a trigger for investigating an increase of more than 15% in the summed current inside it. This is an authored software-test threshold, not an established biological threshold.

Here is what the selected candidate actually shows:

| Check | Observed result | What the reviewer can conclude |
| --- | --- | --- |
| Scenario coverage and pairwise rules | All 12 scenarios complete; no required pairwise check failed | The candidate meets the defined connectivity screens under these assumptions |
| Screening cost | $400,000 against a $500,000 cap | It fits this synthetic cost basis; constructability remains separate |
| Least favorable safeguard pair | 1.6031% lower effective resistance | This pair did not worsen in the model |
| Road-area diagnostic | Summed current changed 47.35%–65.65% across the four safeguard hypotheses | Every case exceeds the authored 15% investigation trigger |
| Overall disposition | `review` | Investigate the flagged evidence before deciding whether to advance the concept |

The road-area finding does not contradict the pairwise result. They measure different things. The modeled connection between habitat areas became less resistant while the summed current inside a particular area increased. Our interface leaves both findings visible because a reviewer needs to explain that combination, not choose which number to believe.

The maps below show the low-barrier safeguard scenario—the same scenario that supplies the least favorable resistance result. Both rasters use the same scale and the same road-review mask.

<!-- ECOVIS:road-mask-current-maps -->
![Two stacked 20 by 12 synthetic Circuitscape current grids show the no-action baseline above the selected candidate on one color scale. Amber outlines the same 48 road-review cells. Their summed current is 1.0638 at baseline and 1.5675 for the candidate, a 47.35 percent increase.](../article-assets/the-model-passed/road-mask-current-maps.png)

_The same 48-cell road-review mask is used in both direct current rasters, on a shared scale. Its summed current rises from 1.0638 to 1.5675 in this safeguard scenario, a 47.35% change. This is a circuit diagnostic, not animal-use evidence._
<!-- /ECOVIS -->

The change is calculated by summing the raster values inside the mask for the baseline and candidate, then comparing those sums. The rounded sums are 1.0638 and 1.5675; using the retained precision gives a 47.3470% increase. The comparison belongs to this model setup; we do not pool these values across unrelated animal groups.

We checked the other safeguard hypotheses too:

| Safeguard hypothesis | Change in summed current inside the same road-review area |
| --- | ---: |
| Low barrier | +47.35% |
| Moderate barrier | +59.59% |
| High barrier | +65.65% |
| Future pressure | +64.77% |

The flag therefore persists across all four safeguard hypotheses in this fixture. It identifies a modeling result to investigate; it does not establish harm. A qualified reviewer would need to inspect the resistance assumptions and intervention footprint, consider the road and approach context, and determine what field evidence is needed before advancing the concept.

That is the handoff we wanted the application to preserve. There was no field visit in this study. The demonstrated behavior is that the software retained the favorable screens and the separate review requirement together.

## A reviewer should be able to check the number

Once a result affects the next step, the reviewer needs more than our description of it. For the controlling safeguard pair, the saved effective resistance is about 1.840286 at baseline and 1.810784 for the candidate. The application computes the percentage as `(candidate − baseline) / baseline × 100`, using full precision. That produces the displayed −1.6031% change.

We built the Evidence Rail so the reader can work backward from that percentage. Opening the result shows the exact scenario, the candidate and baseline direct-run identities, the saved matrix artifact, its checksum, and the numerical quality-assurance status.

<!-- ECOVIS:evidence-rail-receipt -->
![The actual EcoCorridor Evidence Rail displays a negative 1.60 percent baseline-relative resistance change and its candidate and baseline jobs, safeguard scenario, matrix SHA-256, and accepted fixture QA.](../article-assets/the-model-passed/evidence-rail-receipt.png)

_Opening the controlling result shows the calculation, baseline and candidate jobs, a saved matrix hash, and numerical QA. These records let a reviewer check the calculation; the ecological assumptions still require separate review._
<!-- /ECOVIS -->

The [source record for this pair](https://github.com/ash0ts/gi-scenario-studio/blob/f0a26e730f421b3ff95b665df832192caeecfdda/benchmarks/ecocorridor/ridgeway/ui_projection.json#L16035) also contains the values, full run identifiers, and hashes as text. The checksum lets us check that this is the same saved file that produced the result. The run references identify the comparison, and the numerical checks show whether those jobs met the fixture's requirements. None can establish that the resistance values describe a real animal's behavior. A reviewer can agree with the arithmetic and still reject the assumptions.

W&B gives us a place to keep the shared experiment record: which study ran, what it produced, and which version of the evidence the article uses. The local project remains the engineering record; cloud records do not decide whether a result is ecologically credible. For this article, the published material is synthetic. Professional data needs separate authorization, particularly when it includes sensitive locations or licensed layers.

This is the capability I can show today: someone can follow a finding into its source calculation. Whether that saves a practitioner time compared with a well-prepared conventional evidence package still needs to be measured.

## The evidence gave the agent a narrower job

Organizing the results left us with another task. A reviewer who has selected a portfolio needs a field and feasibility brief: which findings require attention, which assumptions remain unresolved, and which source records support each item. As the evidence grows, finding the relevant branches could become substantial work.

That seemed like a plausible place for an agent. We already had the calculations and the findings. The question was whether a model could retrieve the relevant ones without repeatedly reading the whole package.

We built a bounded Review Brief Agent around read-only tools. The worker supplies an authorized evidence snapshot for the selected portfolio. Tools expose named findings and status checks; they do not provide general database, filesystem, solver, or model-editing access. The provider returns selections from known finding identifiers and closed action categories. Local code resolves those selections into the visible sentences, numbers, units, and evidence links.

That last choice limits the writing freedom of the model. It also lets us check whether the brief contains only supported material. We did not need an LLM to rewrite −1.6031% in a more persuasive voice.

For the calls themselves, we used the OpenAI Agents SDK integration with **W&B Weave** and **CoreWeave Agent Lens**, which records the model turns and tool calls as grouped conversations. The worker requires a successful readback of the exact trace before saving a draft. An invalid selection, changed source, missing authorization, or failed trace check leaves a rejection record and no saved draft.

<!-- ECOVIS:evidence-handoff -->
![A downward flow follows authorized evidence into bounded provider and read-only tool work. Required release checks combine exact Weave trace readback, current authorization, local validation, and deterministic rendering before an unconfirmed draft is saved. Missing evidence, provider or tool failure, and failed release checks each exit to no draft.](../article-assets/the-model-passed/evidence-handoff.png)

_Only a validated output with an exact Weave trace readback can become an unconfirmed draft. The historical agent used a fixed tool sequence; this diagram does not claim selective retrieval or autonomous review._
<!-- /ECOVIS -->

A successful result is an **unconfirmed draft**. A person still checks it and decides what to investigate. The model cannot use the brief to approve a portfolio or dismiss a safeguard finding. The no-login public replay remains separate and makes no agent or cloud calls.

Those boundaries gave us something specific to test. They did not tell us whether adding the agent was worth the extra calls and review machinery.

## Our first comparison left the default unchanged

The historical campaign compared a deterministic checklist, a single model call given the full evidence context, and the read-only tool workflow. The provider methods used `openai/gpt-oss-120b` against the same 14-case synthetic dataset. Four cases came from the Ridgeway teaching portfolios; the others exercised conditions such as incomplete evidence, rejected numerical checks, stale state, and invalid or unauthorized output.

We wanted to know whether tool-based retrieval could reduce provider input while preserving the required findings. The campaign's promotion rule required at least a 25% reduction. That calculation uses the summed provider-reported input tokens across the four teaching-portfolio cases, not the whole 14-case dataset:

| Method | Input tokens across four teaching-portfolio cases |
| --- | ---: |
| Full-context single call | 10,798 |
| Read-only tool workflow | 10,542 |

The difference is 256 tokens, or 2.37%. The deterministic checklist stayed the default.

There are two problems with treating this as a verdict on agents. We forced the tool workflow through a fixed sequence, so it could not demonstrate useful choices about what to read or when to stop. We also supplied the full-context comparator with an expected-answer contract describing what a valid answer should contain. That gave it hints it should not have received in a usefulness comparison. We wrote both sides of that test.

<!-- ECOVIS:agent-promotion-gate -->
![Across four teaching-portfolio cases, horizontal bars show 10,798 full-context input tokens and 10,542 agent input tokens. A reduction chart places the observed 2.37 percent beside the 25 percent promotion gate. Notes identify fixed agent traversal and comparator answer hints; the deterministic checklist remains default.](../article-assets/the-model-passed/agent-promotion-gate.png)

_Across the four teaching-portfolio cases, the historical agent used 256 fewer input tokens, a 2.37% reduction against a 25% gate. Fixed traversal and answer-contract hints in the full-context comparator limit the comparison. The deterministic checklist stayed default._
<!-- /ECOVIS -->

The campaign still established something narrower. Its 42 scored records comprise 14 local deterministic controls, 22 provider-backed conversations with retained Weave traces, and six cases blocked before a provider call. The recorded calls could be read back and reconciled, and the tested rejection paths produced no saved draft. Some safety cases used explicit local guards or injected downstream failures; those results are evidence about those controls, not proof that the model resisted the attacks by itself.

That is why I want the [historical run](https://forge.coreweave.com/wandb/wandb/ecocorridor-scenario-studio/runs/review-brief-realistic-120b-5dad47c-v5) to remain available with its limitations. The traces let us inspect what executed, while the campaign artifact keeps the dataset, scoring, and result together. Correcting the next comparison should not make the old one disappear.

The source now separates adaptive traversal from an explicitly exhaustive control and removes the expected-answer hints from the full-context method. Those are repairs to the experiment setup. They are not new evidence that the agent helps, and this article does not report a completed fair usefulness campaign.

## What would make me use it

For the next agent comparison, I want the methods to receive the same evidence and output requirements, with the expected findings kept away from the provider. The adaptive agent needs to choose its reads and stop when it has enough. An agent required to open everything belongs alongside it as a control. If selective retrieval helps, we should see less irrelevant evidence accessed and lower context use without missing critical findings.

The more important product test involves people. Ecological and transportation practitioners should review matched cases using EcoCorridor and a conventional package containing the same matrices, maps, costs, assumptions, and source records. We need to measure missed safeguards, correct screening dispositions, evidence reconstruction, false alarms, and verification time. The agent would have to improve that workflow further, not merely produce a brief that looks finished.

Those studies remain planned. The current implementation is also a technical preview, not a practitioner release: professional cloud authorization, independent release verification, signed installers, and practitioner acceptance still have unresolved gates. No result here establishes animal movement, collision reduction, population or biodiversity benefit, or an approved crossing.

I still think the review task is worth building for. A person evaluating a concept should be able to see which assumption controls it, open the calculation, and carry an unresolved finding into the next review. Ridgeway gives practitioners a concrete version of that workflow to question.

For now, I would start with the deterministic review and ask someone who does this work to try reconstructing the candidate's safeguard result. If they have to repeat the entire investigation, we have more product work to do. If they can check it, challenge the assumption, and identify the next useful investigation more easily, we have something worth measuring properly.

---

## Inspect the work

- [Historical Review Brief campaign](https://forge.coreweave.com/wandb/wandb/ecocorridor-scenario-studio/runs/review-brief-realistic-120b-5dad47c-v5) and its [versioned evidence artifact](https://forge.coreweave.com/wandb/wandb/ecocorridor-scenario-studio/artifacts/evaluation-bundle/ecocorridor-review-brief-33f5f7be9aee/v0).
- [Original technical Report](https://forge.coreweave.com/wandb/wandb/ecocorridor-scenario-studio/reports/EcoCorridor-historical-search-benchmark--VmlldzoxNzY1OTU0MQ==), preserving the earlier experiment record.
- [EcoCorridor implementation and review history](https://github.com/ash0ts/gi-scenario-studio/pull/13).
- [Circuitscape documentation](https://docs.circuitscape.org/Circuitscape.jl/latest/usage/) and the [CoreWeave Agent Lens OpenAI Agents SDK integration](https://docs.coreweave.com/products/agent-lens/integrations/openai-agents-sdk).

_Ridgeway Crossing is synthetic CC0 teaching material. This article interprets retained direct Circuitscape evidence and the historical Review Brief campaign; the rewrite adds no experiment result._
