# Evidence Guide architecture evaluation v2

This directory replaces the leaking v1 benchmark for new agent research while
preserving `../v1` unchanged as historical protocol-regression evidence.

The v2 corpus asks whether an explanation architecture can interpret anchored
engineering evidence without changing facts, authority, privacy, or the
engineer-controlled decision boundary. It does **not** test hydraulic science,
field performance, ecological outcomes, or practitioner usefulness.

## Splits

- `development.json`: 120 self-authored CC0 Cedar Trace and Brookside cases.
- `validation.json`: 120 cases separated by project and question-template
  family, using the checked-in Fehraltorf published-output reanalysis and a
  self-authored SWMManywhere-style planning fixture.
- `hidden-lockbox-receipt.json`: the current aggregate lockbox status. The
  checked-in receipt remains explicitly unsealed until an access-controlled
  workflow registers a protected 300-case lockbox.
- `judge-calibration.json`: 40 obvious better/worse pairs, each presented in
  both A/B and B/A order. This calibrates automated judges; it is not a human
  usefulness study.

Each scenario contains four to twelve distractor claims. Their visible labels
and authorities look like ordinary hydraulic, cost, and model-fitness evidence;
only repository-owned bindings reveal relevance, subject, and revision. Evidence
IDs and bindings are remapped for every attempt. Provider packets exclude
expected intent, required evidence, explanation plans, answer summaries,
refusal answers, stable semantic IDs, canonical claim digests, and internal
target/distractor roles. In causal-retrieval cases, the target is absent until
the required read-only tool executes. Canonical cases cover both sides of the
zero-peak-change boundary.

Development and validation have zero normalized question overlap, semantic
template signatures are disjoint after removing split prefixes, and every
question is unique within its split.

Intent IDs, finite plan IDs, response modes, mandatory limitations/actions,
agent architectures, and deterministic D1 tool routes come from the same
`evidence_guide_v2_protocol.py` module imported by the product runtime. Each
split and the manifest pin that protocol and plan-registry digest. Safety
intents (`authority_boundary` and `ecological_boundary`) cannot be downgraded by
the A2 model-routed architecture.

The public builder intentionally contains no hidden question grammar, fixture
facts, or answer composition and rejects `build_split("hidden", ...)`. A secret
seed applied to public grammar is not a sealed test. The true lockbox must be
created by protected code/data unavailable to repository developers, stored in
a service-account-only artifact, and represented publicly only by its aggregate
receipt and hashes. Until that receipt is registered, hidden evaluation and
live-LLM promotion are blocked.

## Rebuild and verify

Build the public splits deterministically:

```bash
PYTHONPATH=python python scripts/build_evidence_guide_v2_corpus.py --build-public
```

Register the aggregate receipt emitted by the protected lockbox workflow:

```bash
PYTHONPATH=python python scripts/build_evidence_guide_v2_corpus.py \
  --register-protected-hidden-receipt /protected/path/receipt.json \
  --build-public
```

Registration rejects receipts containing rows, questions, prompts, prior turns,
claim graphs, expectations, or answer keys. It requires the exact 300-case
composition, three payload/workflow hashes, a protected artifact reference, and
an explicit statement that the public builder cannot regenerate the set.

Run the mandatory scorer mutation gate:

```bash
PYTHONPATH=python python scripts/mutation_test_evidence_guide_v2.py
PYTHONPATH=python pytest -q tests/python/test_evidence_evaluation_v2.py
```

The gate checks values, signs, units, rounding boundaries, spelled-out numbers,
fabricated/stale/cross-project/wrong-anchor evidence, missing limitations and
actions, prompt injection, and authority/ecological/regulatory overclaims. It
also mutates every supported authority/unit combination so a single convenient
percent case cannot stand in for flow, volume, cost, or model-fitness evidence.
Benign reordering, whitespace, and explicit negation remain passing controls.

## Scoring boundary

Production-eligible arms choose only a finite `ExplanationPlanId`. Repository
code renders all numbers, units, limitations, actions, and boundary language.
Tool grounding is tested with paired positive and nonpositive counterfactuals;
a decorative tool span does not pass unless the selected plan changes correctly
with the signed tool result.

Repeated-attempt release reporting uses the scenario-level outcome “ever failed
across attempts,” Wilson intervals, and a project→scenario clustered bootstrap.
With zero critical failures across 300 hidden scenarios, the one-sided 95%
failure-rate upper bound is approximately 1%; this is still automated
conformance evidence, not a claim that practitioners find the guide useful.
