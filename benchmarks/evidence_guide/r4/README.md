# Evidence Guide R4 grounding corpus

This directory contains a deterministic, self-authored corpus for one narrow
scientific question:

> When a repository value changes, does an explanation system change its
> decision for the right reason—and ignore a similarly sized change to an
> irrelevant record?

It does **not** evaluate SWMM, green-infrastructure performance, ecological
benefit, calibration, regulatory compliance, or practitioner usefulness. All
project names and numerical records are fictional. The values are plausible
stormwater-screening quantities, but they are not solver outputs or field
observations.

## Experimental design

The corpus has exactly 24 scenarios across four fictional project families:

- Cedar Trace
- Brookside
- Pine Ward
- Harbor Annex

Each family contributes six scenarios, three `≤` rules and three `≥` rules,
with three passing and three failing original records. The quantities cover
peak flow, event volume, volume reduction, cost, freeboard, flood volume and
duration, surcharge duration, conduit-capacity behavior, surface storage,
hydraulic grade, and other bounded screening signals.

Every scenario contains four matched conditions:

1. `original_target` — the original target and four to eight distractors.
2. `target_threshold_crossing` — only the target value and its record hash
   change; the value crosses the pinned, nonzero threshold.
3. `target_withheld` — only the target's availability changes; all distractors
   remain.
4. `distractor_only_perturbation` — the target remains identical while one
   distractor value and its record hash change.

Together, the first two cells measure target sensitivity, the fourth measures
distractor specificity, and the third measures missing-evidence abstention.
The design supports reporting each property independently instead of hiding
them inside one accuracy score.

## Leakage boundary

`provider_payload` is the only object intended for a model. It contains the
question, anchor, target identity, rule, retrieved evidence, response contract,
and scope boundary. It does not contain condition labels, gold answers, or an
expected plan.

Gold plan IDs, expected evidence citations, and intervention identities live
under `evaluator_only`. Experiment code must never copy that object into a
provider request. Opaque plan IDs are assigned deterministically per scenario;
the provider packet exposes only the rule-to-plan mapping required to perform
the task.

## Reproducibility

Regenerate the checked-in corpus:

```bash
python3 scripts/build_evidence_guide_r4_corpus.py
```

Verify that it matches the builder without writing:

```bash
python3 scripts/build_evidence_guide_r4_corpus.py --check
```

The manifest embedded in `corpus.json` contains SHA-256 digests for the corpus
body, every scenario, and the complete set of provider packets. Canonical JSON
uses UTF-8, sorted keys, compact separators, and rejects NaN. The initial corpus
body digest is:

```text
5f71147c8fe20e7be3606dcef3c74fb9e4dc105dc0ab1d562d381ec99eea825a
```

Tests fail on dataset drift, target leakage, unbalanced rules or outcomes,
missing provenance, a zero threshold, an invalid distractor count, or any
intervention that changes more than its declared evidence field.

## License

The corpus, its fictional project families, and its synthetic numerical
records are dedicated to the public domain under **CC0 1.0**. To the extent
possible under law, the authors waive all copyright and related rights in
these materials.

Third-party model outputs produced by future experiments are not part of this
CC0 corpus and must retain their own provider and publication terms.

## R4 provider-protocol amendment

The original R4 live execution stopped after its first 224-cell time block.
All 224 calls failed before either model returned a response. A separate
six-call diagnostic isolated the failure to W&B Inference rejecting the JSON
Schema keyword `uniqueItems`: plain chat and the documented minimal structured
schema succeeded for both models, while the otherwise matched R4 schema failed
for both with `Grammar error: Unimplemented keys: ["uniqueItems"]`.

R4 therefore has no scientific model-behavior denominator. Its output remains
preserved under `dist/evidence-guide-r4-grounding-final-r1`; it is not resumed,
rewritten, or pooled with the replacement study.

R4b removes only that unsupported provider-grammar keyword. Duplicate evidence,
limitation, and action identifiers are still rejected by the repository after
parsing. A four-call corrected-schema preflight returned HTTP 200 for both R4b
schemas and both models. Those diagnostics test protocol compatibility only;
they are not substantive grounding observations.

The complete incident counts, receipt identifiers, diagnostic matrix, token
counts, claim boundary, and clean-start rules are recorded in
[`protocol-amendment-r4b.json`](protocol-amendment-r4b.json).

R4b's budget ledger also carries the failed work forward. Cap enforcement starts
from `$0.1087813`: the preserved `$0.066883` verified cumulative cost, the
failed R4 block's `$0.041520` at-risk envelope, and the diagnostics' `$0.0003783`
conservative envelope. The separately reported observed total is `$0.0669285`;
it is not used to weaken the cap reservation.
