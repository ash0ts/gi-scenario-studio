# Evidence Guide evaluation dataset v1

This directory contains 48 project-authored synthetic cases released under
CC0-1.0. The cases exercise exact-run explanations, Model Fitness Passport
boundaries, and safe refusals for unsupported engineering, ecological,
regulatory, and computational-impact claims.

The dataset contains no customer model, location, credential, or live trace
content. Its deterministic scorer checks evidence-ID binding, exact numeric and
unit fidelity, required limitations, prohibited claims, read-only authority,
and refusal behavior. It does not measure field validity or practitioner
usefulness.

Validate the corpus without making a cloud request:

```bash
PYTHONPATH=python uv run --no-sync python scripts/evaluate_evidence_guide.py \
  --validate-only
```

Evaluate saved answer objects with:

```bash
PYTHONPATH=python uv run --no-sync python scripts/evaluate_evidence_guide.py \
  --outputs path/to/outputs.json \
  --output dist/evidence-guide-evaluation/evaluation.json
```

The scorer-conformance projection is available with `--reference`. It proves
that the dataset and scorer agree; it is not a provider evaluation and does not
create or imply a live Weave trace.

## Candidate-model release gate

Preview the complete two-model campaign without credentials or network access:

```bash
PYTHONPATH=python uv run --no-sync python \
  scripts/run_evidence_guide_candidate_evaluation.py --dry-run
```

The plan covers all 48 cases for both pinned candidates, for 96 structured
inference calls. This is a **single-call, schema-constrained evidence-selection
experiment**, not an execution of the product's two-call tool-using agent. The
model selects evidence, limitation, and action IDs; repository code inserts
canonical numeric values and units. A separate three-turn integration probe
exercises the real runtime loop, but all three turns were rejected and no answer
was released. Together these are useful behavior and fail-closed integration
evidence; they are not a successful end-to-end agent evaluation. Execute mode
first verifies that the exact W&B destination is
`Restricted`, then writes separate outputs, deterministic evaluations, and
credential-free receipts for each model:

```bash
PYTHONPATH=python uv run --no-sync python \
  scripts/run_evidence_guide_candidate_evaluation.py \
  --execute \
  --entity "$WANDB_ENTITY" \
  --project "$WANDB_REPORT_PROJECT" \
  --practitioner-ratings path/to/anonymized-practitioner-ratings.json
```

The command never prints prompts, model text, provider diagnostics, or API
keys. It does not publish anything to Weave. Validate the resulting candidate
outputs, evaluations, receipts, and campaign gate without credentials or cloud
SDK imports:

```bash
PYTHONPATH=python uv run --no-sync python \
  scripts/evaluate_evidence_guide.py \
  --campaign-dir dist/evidence-guide-candidate-evaluation \
  --dry-run
```

Publication is a separate explicit action:

```bash
PYTHONPATH=python uv run --no-sync python \
  scripts/evaluate_evidence_guide.py \
  --campaign-dir dist/evidence-guide-candidate-evaluation \
  --publish \
  --entity "$WANDB_ENTITY" \
  --project "$WANDB_REPORT_PROJECT"
```

The publisher refuses the synthetic reference projection but accepts failed
real candidates as failure evidence. It logs all 48 cases per model, marks
missing model answers as `not_produced`, and publishes the exact campaign gate
and allowlisted candidate-receipt projections as a versioned Weave object. Raw
receipts remain local and are pinned by SHA-256. Aggregate inference usage is
preserved as count fields with a `tokens` unit while credential-like token
field names remain prohibited. This documents why a model is disabled without
turning a failed evaluation into a pass.

The five-reviewer/80% criterion remains required evidence, but reviewer hashes
alone are not release authority. The live guide remains disabled unless one
candidate passes every deterministic gate and a controlled release process
cryptographically verifies an approval receipt bound to the exact evaluation
and canonical ratings digest. Omitting ratings, the controlled receipt, or its
trusted verifier is valid for engineering regression work, but produces the
deliberate decision `DISABLE_LIVE_GUIDE`.

The checked-in `release-gate.json` is the byte-for-byte result of the current
real two-model campaign. It disables the live guide because neither candidate
passed the deterministic gates and practitioner usefulness has not been
measured. Its digest is pinned in both the dataset manifest and product code;
missing, changed, or malformed gate data disables only model inference while
the local Evidence Rail continues to work.

Ratings use this shape; `reviewer_id_sha256` must be a lowercase SHA-256
pseudonym created outside the repository rather than a name, email, or employee
ID. Treat the mapping used to create that pseudonym as sensitive:

```json
{
  "schema_version": "1.0",
  "dataset_id": "gi-evidence-guide-evaluation",
  "dataset_version": "v1",
  "ratings": [
    {
      "model_id": "ibm-granite/granite-4.1-8b",
      "case_id": "eg-exact-001",
      "reviewer_id_sha256": "<64 lowercase hexadecimal characters>",
      "helpful": true
    }
  ]
}
```
