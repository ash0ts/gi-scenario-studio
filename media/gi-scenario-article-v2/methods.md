# Methods and evidence

These notes accompany [Why We Added an AI Guide to a Stormwater Tool](article.md). They describe the completed experiment behind the article, including the failed checks and publication recovery. This is not a report of the larger architecture-optimization campaign proposed earlier.

## Two different kinds of evidence

The opening example comes from the [Cedar Trace guided fixture](../../apps/desktop/src/data/guidedFixture.json). It uses a synthetic SWMM model, four synthetic storms, and teaching rules. `ALT-017` has a synthetic 30-year present-value screening cost of $54,990, compared with a $200,000 cap. Its aggregate outlet-volume reduction is 4.14%, below the 6% target. It increases the long-event peak from 3.14 to 3.21 cfs and the stress-event peak from 13.33 to 13.34 cfs. All four runs have accepted numerical QA.

Those facts explain the interpretation problem. They are not the agent's test corpus. The experiment used separately authored CC0 synthetic evidence records, including the pipe example in the article. The value change from 0.79 to 0.91 was an intervention on a prepared agent-test record, not a new SWMM simulation.

The [article evidence manifest](dist/evidence-manifest.json) binds the displayed fixture values, result tables, and figures to source-file SHA-256 hashes. It is generated from the fixture and the recorded experiment, not from prose in the article.

## Tested configuration

The experiment is identified in the repository as `R4c`. Its [protocol and scoring implementation](../../python/gi_scenario_core/evidence_guide_r4_grounding.py) and [runner](../../scripts/run_evidence_guide_r4_grounding.py) specify the schedule.

The two fixed W&B Inference model IDs were:

- `ibm-granite/granite-4.1-8b`
- `Qwen/Qwen3-30B-A3B-Instruct-2507`

Temperature was 0 and seed was 17. Calls were scheduled across three time blocks. Each scheduled attempt used one provider invocation and one transport attempt; there was no successful-response retry pool to choose from. Each scenario was presented with two different opaque evidence-ID mappings and claim orders.

In the plan-selection test, the provider received a question and a prepared evidence packet, then selected a finite explanation plan and references. Repository code owned the factual answer content, limitations, and actions. The selected plan and bindings were checked against the declared rule. This is a post-retrieval test: it does not establish that an agent can find the relevant evidence through production tools.

The plan-selection stage used a 128-output-token limit. A separate diagnostic used the full structured prose response contract at 128 and 256 output tokens. Its results should not be substituted for the plan-selection behavior scores.

## Cases, conditions, and denominators

The plan-selection corpus contained 24 unique scenarios from four authored project families:

<table id="corpus-split">
  <caption>Family-separated synthetic test corpus</caption>
  <thead><tr><th scope="col">Split</th><th scope="col">Project families</th><th scope="col">Unique scenarios</th></tr></thead>
  <tbody>
    <tr data-split="development"><th scope="row">Development</th><td>Cedar Trace CC0; Brookside CC0</td><td>12</td></tr>
    <tr data-split="validation"><th scope="row">Validation</th><td>Pine Ward CC0; Harbor Annex CC0</td><td>12</td></tr>
  </tbody>
</table>

These are authored test families, not four calibrated public drainage networks. The split separates project families; the records do not establish that the validation cases stayed untouched throughout prompt development. This is not the sealed 300-scenario hidden test from the earlier proposed study.

Each scenario had four conditions: the original target, a relevant value moved across its rule, the required fact removed, and a change to unrelated evidence. This produces three paired behavior checks against the original.

The validation tables count **unique scenarios**. Both shuffled presentations must pass a check for that scenario to pass it. The two presentations are repeated measurements, not independent additional cases.

- **Threshold sensitivity:** the original and changed selections must both be correct, with the selection changing when the target crosses the rule.
- **Distractor specificity:** changing only unrelated evidence must preserve the correct selection.
- **Withheld-target abstention:** removing the required fact must produce the insufficient-evidence response with the expected evidence handling.
- **Complete mechanism:** the scenario must satisfy all three checks.

For Granite, the validation counts were 1/12, 6/12, 10/12, and 0/12 respectively. For Qwen, they were 5/12, 7/12, 10/12, and 3/12. These are descriptive results on a small fixed corpus. We do not treat their difference as a general model ranking.

## Full call accounting

The completed R4c package contains **672 = 384 + 288** provider invocations.

<table id="call-accounting">
  <caption>Provider calls in the completed experiment</caption>
  <thead><tr><th scope="col">Stage</th><th scope="col">Scheduled combination</th><th scope="col">Calls</th></tr></thead>
  <tbody>
    <tr data-stage="grounding"><th scope="row">Plan selection</th><td>24 scenarios × 4 conditions × 2 models × 2 presentations</td><td>384</td></tr>
    <tr data-stage="prose_diagnosis"><th scope="row">Response-contract diagnostic</th><td>12 scenarios × 3 conditions × 2 models × 2 presentations × 2 token caps</td><td>288</td></tr>
    <tr data-stage="total"><th scope="row">Total</th><td>Both stages</td><td>672</td></tr>
  </tbody>
</table>

All 672 provider invocations returned a response; all 672 attempts recorded trace acknowledgement. There were 672 transport attempts. Provider-reported usage totaled 1,140,656 input tokens and 56,302 output tokens. These are operational measurements, not 672 correct explanations.

The deterministic positive control passed **192/192** cells: 24 scenarios × 4 conditions × 2 presentations. It used no model provider calls and is not part of the 672-call total. It checks that the rule logic and expected bindings can produce the scheduled behavior. It is not a measured comparison of engineer effort or explanation preference.

The source records are the [article summary](../gi-scenario-film/dist/evidence-guide-r4c/article-summary.json), [saved analysis](../../dist/evidence-guide-r4c-grounding-final-r1/live/publication/analysis.json), and [attempt records](../../dist/evidence-guide-r4c-grounding-final-r1/live/publication/attempts.json).

## Output-token diagnostic

The separate diagnostic asked for the full structured prose response. It used 12 scenarios under three conditions—original, threshold crossing, and required evidence withheld—at two token caps. Each scenario-condition pair had to pass both presentations.

<table id="token-diagnostic">
  <caption>Strict response-contract completion; 36 scenario-condition pairs per model and cap</caption>
  <thead><tr><th scope="col">Model</th><th scope="col">128 output tokens</th><th scope="col">256 output tokens</th></tr></thead>
  <tbody>
    <tr data-model="ibm-granite/granite-4.1-8b"><th scope="row">Granite</th><td>1/36</td><td>36/36</td></tr>
    <tr data-model="Qwen/Qwen3-30B-A3B-Instruct-2507"><th scope="row">Qwen</th><td>11/36</td><td>36/36</td></tr>
  </tbody>
</table>

The larger cap improved completion of this strict response contract in this corpus. That does not establish explanation quality or engineer usefulness, and the paired result alone does not identify the cause of every failed low-cap response. In particular, it does not repair the plan-selection failures reported in the main article.

## The recorded counterfactual

The figure uses case `r4-ha-21` from `harbor_annex_cc0`, presentation 1, with the pinned Qwen configuration. The metric is `conduit_capacity_ratio` for synthetic object `C-19`, in ratio units. The rule is `<= 0.85`.

The original value was 0.79 and satisfied the rule. The changed value was 0.91 and did not. The recorded plan was unchanged; both attempts had trace acknowledgements and no answer was released.

The corresponding records are the [original Weave conversation](https://wandb.ai/wandb/gi-scenario-studio-agent-evals/weave/agents/conversations/eval-b947d0f5f6cc-b4b4f0b55e0be263f769ab671df06e51) and [changed Weave conversation](https://wandb.ai/wandb/gi-scenario-studio-agent-evals/weave/agents/conversations/eval-b947d0f5f6cc-d08af8ce08a97ab002c5a27177fde786). These links require access to the existing restricted project. Acknowledgement is recorded separately from correctness; it does not mean a model interpretation passed validation.

## Publication failure and recovery

The initial R4c publication failed after all 672 provider attempts and analyses had been saved. A call to `wandb.Table.add_data` raised a `TypeError`: the SDK had inferred a mapping schema for an `error_codes` column from an earlier row, then encountered additional keys in a later row.

This was a Table projection failure before the publication receipt was written. It was not a provider or trace failure, and it did not change the recorded experiment results.

Recovery validated the 672 saved attempts, responses, and trace acknowledgements, then rebuilt the publication with **0 new provider calls**. The [recovered W&B run](https://wandb.ai/wandb/gi-scenario-studio-agent-evals/runs/a0da507bad0a74f35b75a42d) records the result. Its immutable artifact reference is:

`wandb/gi-scenario-studio-agent-evals/evidence-guide-r4c-publication-734df3c736956c87:v0`

The [summary](../gi-scenario-film/dist/evidence-guide-r4c/article-summary.json) retains the failure boundary, offending mapping, recovery hashes, artifact identity, and readback receipt. Earlier R4 and R4b development incidents are listed separately in that record; their calls are not included in the completed R4c scientific denominator.

## Recorded decision and scope

The recorded decision was `DISABLE_LIVE_GUIDE`, with `production_release_eligible: false`, zero released generated answers, and `practitioner_usefulness_status: not_measured`. The deterministic Evidence Rail remained available.

This completed study supports a narrow finding: the tested configurations did not reliably choose the expected plan in response to controlled changes in supplied synthetic evidence. It was not a production safety evaluation, a live retrieval benchmark, a prompt-injection test, or a practitioner study. It also did not measure hydraulic accuracy, ecological benefit, or real-world engineering outcomes.

## Source and build notes

The [protocol](../../python/gi_scenario_core/evidence_guide_r4_grounding.py) defines conditions, payload construction, and scoring. The [summary](../gi-scenario-film/dist/evidence-guide-r4c/article-summary.json) projects the completed records into the article's claims. The [visual contract](visual-contracts.json) records each figure's job and evidence boundary. The [package README](README.md) gives the local build and verification commands.

The figures and HTML pages can be rebuilt without provider calls or cloud publication. Their verifier checks the native tables, source hashes, referenced assets, and rendered layout. Reproducing model calls is a different operation and may depend on provider availability; it is not part of building this article.

For background, see the EPA's [SWMM overview](https://www.epa.gov/water-research/storm-water-management-model-swmm) and [GI environmental-benefit categories](https://www.epa.gov/green-infrastructure/environmental-benefits-green-infrastructure), plus W&B's documentation on [Weave agent tracing](https://docs.wandb.ai/weave/guides/tracking/trace-agents) and [evaluations](https://docs.wandb.ai/weave/guides/core-types/evaluations). These explain the tools and potential environmental outcomes; they are not evidence that this Guide improved a decision.

[Return to the article](article.md).
