# Review Brief agent evaluation and Report append

The Review Brief campaign compares three fixed methods over the same fourteen
CC0 synthetic cases:

1. `deterministic_exhaustive` — a complete local control with no provider;
2. `existing_single_shot_full_context` — one no-tool, one-turn OpenAI Agents
   SDK conversation with strict structured output;
3. `native_read_only_agent` — the Review Brief Agents SDK subprocess with only
   alias-scoped read-only tools.

The first four cases are derived from the verified Ridgeway teaching roles.
The remaining ten cover safeguard failure, incomplete ensembles, QA rejection,
stale evidence, unknown aliases, prompt injection, sensitive locations,
unsupported structured output, trace-readback failure, and unauthorized action.
Every method-by-case record is scored by the same deterministic scorer set.

## Historical campaign interpretation

The already-published 42-record campaign is retained byte-for-byte as
historical trace-integrity evidence, not usefulness evidence. Its native path
used a forced tool order, and its single-shot context included the deterministic
expected-answer contract. The observed 2.37% provider-input-token reduction
also missed the preregistered 25% operational gate. Those facts make the result
a useful negative control: it verified fail-closed plumbing and exact Weave
readback, but it cannot show selective discovery, fair comparative quality, or
practitioner benefit.

Current source removes the expected-answer contract from the single-shot
context. It also separates `ADAPTIVE` traversal—which sees only a neutral
finding-universe count, chooses coarse branches, inspects selected aliases, and
may stop without opening or returning every finding—from the labelled
`FORCED_EXHAUSTIVE` all-branch/all-finding ablation. Discovery does not reveal
priorities, expected aliases, required tools, or a deterministic action recipe. Every tool
read now produces an alias-only retrieval receipt recording requested, returned,
new, and duplicate aliases plus the exact output hash, output bytes, and cumulative
model-context bytes. Adaptive preflight budgets only fixed instructions/input and
then enforces the unchanged 64 KiB ceiling after every actual read; the forced
ablation still preflights its complete deterministic traversal. These changes do
not rewrite or relabel the historical artifact. A new usefulness campaign must use its own
versioned schema and the method IDs `fair_full_context`,
`adaptive_read_only_agent`, and `forced_exhaustive_agent`.

## Conversations and pre-provider blocks

A 42-record matrix is not described as 42 model calls. The deterministic
method has no provider calls. The two provider methods retain an explicit
`execution_path` on each record:

- `PROVIDER_CONVERSATION` requires a real provider call plus exact Weave Agents
  trace flush and readback;
- `PRE_PROVIDER_BLOCK` proves the fixed local boundary stopped before provider
  invocation and contains no provider or trace receipt;
- `DETERMINISTIC_LOCAL` is reserved for the exhaustive local control.

The fixed matrix requires 22 recorded provider case conversations and six
pre-provider blocks. Provider capability preflights are additional traces and
are not miscounted as case records. Both provider methods block
prompt-injection and sensitive-location cases locally so raw
adversarial/location text never crosses the external boundary. The native
method additionally exercises its local stale-identity and unknown-alias
authorization guards before subprocess invocation. Other synthetic safety
cases first exercise the provider method and retain the verified control
trace, then exercise an explicit downstream state, schema, trace-readback, or
unauthorized-action rejection seam. Every safety execution records
`fault_injection_exercised=true`. Each of those executions also queries a shared
isolated application `EcoProjectStore`, writes and reads back a checksum-verified
rejection audit, confirms the application's rejection-excludes-draft trigger is
installed, and queries the real `eco_review_brief_drafts` table for the exact
call identity. It records `persistence_backend=ECO_PROJECT_STORE`,
`rejection_audit_verified=true`, and `saved_draft_count=0`, and fails closed if
any application Review Brief draft exists. None of the evaluation refusal
envelopes is saved as an application draft.

The corrected single-shot provider receives authorized numeric evidence, semantic source
facts, required safety limitations, and the case question. It does not receive
the expected answer or scorer-only required findings. The corrected native method uses
the complete production `ridgeway_review_source` snapshot and its actual agent
selections; it does not rebuild the provider snapshot from scorer ground truth.
Selected actions, reviewer disciplines, production finding IDs, finding text,
and finding-level production evidence IDs are preserved in the scored result.
Required-finding recall uses maximum one-to-one semantic matching: the same
observed finding must have the expected kind and contain that expected finding's
required evidence, and one observed finding cannot satisfy multiple benchmark
findings. Unmatched or invalid-evidence observed findings are unsupported; the
scorer does not match benchmark finding IDs or expected prose. After the
benchmark was found to combine cost, direct-pair, and map evidence into one
finding, the role ground truth was replaced with an independently adjudicated
semantic ledger derived from the public Ridgeway projection. It expects the
same real-world units as the production source—diagnostic review-mask, cost,
and ecological-assumption findings—with only each finding's own evidence IDs.
A network-free contract test renders all four real production sources and
requires 1.0 recall with zero unsupported findings before any provider result
is considered. After the
production draft is validated to cover the exact authorized source evidence,
the local evaluation renderer projects only the case's verified numeric evidence
into the scored answer. Numeric faithfulness therefore tests that bounded local
projection, not provider numerical reasoning. A real native run can still fail
required-finding, selection, authorization, or copy-safety gates when its actual
production selections do not support the benchmark target.

## Provider capability boundary

The producer requires an explicit provider base URL, exact model identifier,
provider name, and mode. It has no provider/model defaults. `RESPONSES` is
accepted only with the explicit official `https://api.openai.com/v1` base URL;
other OpenAI-compatible providers use `CHAT_COMPLETIONS`.

Before campaign records are generated, the producer requires a Review Brief
agent capability preflight that performs a real guarded tool call, strict
structured output, Weave flush, and exact trace readback. That verified
capability receipt gates the no-tool single-turn Agents executor as well. Each
native production snapshot is preflighted before its prepare call by the
runtime.

Provider and W&B credentials are accepted only from
`ECOCORRIDOR_PROVIDER_API_KEY` and `WANDB_API_KEY` in process memory. They are
not accepted as arguments, placed in context, logged, or written to the
campaign.

## Run the protected campaign

Install the locked optional dependencies:

```bash
uv sync --locked \
  --extra ecocorridor \
  --extra ecocorridor-agents \
  --extra observability \
  --extra ecocorridor-report \
  --extra dev
```

Then run the producer with an explicit protected configuration. The example
uses placeholders deliberately; the repository does not hard-code a model,
provider, W&B target, or historical Report URL.

```bash
PYTHONPATH=python .venv/bin/python \
  scripts/run_ecocorridor_review_brief_evaluation.py \
  --source-repository OWNER/REPOSITORY \
  --source-commit FULL_40_CHARACTER_COMMIT \
  --provider-name PROVIDER \
  --provider-base-url https://provider.example/v1 \
  --provider-model EXACT_MODEL_ID \
  --provider-mode CHAT_COMPLETIONS \
  --provider-cost-basis-ref provider-pricing-YYYY-MM-DD \
  --provider-input-usd-per-million INPUT_RATE \
  --provider-output-usd-per-million OUTPUT_RATE \
  --wandb-entity ENTITY \
  --wandb-project PROJECT \
  --wandb-external-project-id EXACT_EXTERNAL_PROJECT_ID \
  --wandb-api-base-url https://api.wandb.ai \
  --wandb-web-base-url https://wandb.ai \
  --wandb-run-id UNIQUE_PROTECTED_RUN_ID \
  --authorize-synthetic-public-provider-evaluation \
  --output /tmp/ecocorridor-review-brief-campaign.json
```

The producer requires a clean tree at the exact source commit. It writes the
campaign only after the complete matrix and deterministic recomputation pass.
The signed campaign includes the exact W&B run ID and credential-free run URL
that own its provider and Weave conversations. Publication resumes that same
run; it does not invent a second evaluation run.
It does not publish an artifact, edit a Report, render a graphic, or render a
video.

## Historical default-method decision

`deterministic_exhaustive` remains the production default unless every fixed
promotion gate passes. `native_read_only_agent` is promoted only when:

- every one of its fourteen deterministic role and safety gates passes;
- its role and safety pass counts are each non-inferior to the existing
  single-shot comparator; and
- its summed provider-reported input tokens over the four role cases are at
  least 25% lower than the comparator;
- cost is available for both provider methods on an explicit, versioned basis;
  and
- action and reviewer-discipline appropriateness can be scored against an
  independent semantic facet rather than merely checked against allowed values.

The 25% materiality threshold is a project-defined operational choice made
before the campaign. It is not a user-specified requirement and is not an
ecological-science threshold. The signed decision records
`context_measurement_method=PROVIDER_REPORTED_INPUT_TOKENS_V1`; local IPC or
snapshot byte counts are never compared across methods. The campaign also
records estimated provider cost from exact provider-reported token counts and
an operator-supplied, versioned pricing basis. The signed campaign retains both
rates, currency, estimation method, reference, and a basis digest so the cost
column can be recomputed from the artifact without trusting mutable run config.
Missing token usage, rates, or a cost-basis reference fails the online campaign
instead of inventing a cost.
The native production snapshot retains a closed `review_topic` facet, and local
validation requires the chosen action and reviewer disciplines—including a
primary discipline—to be appropriate for that topic.

The historical campaign records `PROMOTE_NATIVE` or `KEEP_DETERMINISTIC`; it does not
silently change desktop behavior. Until a new fair usefulness campaign passes every gate,
`native_read_only_agent` remains `EXPERIMENTAL` and
`deterministic_exhaustive` remains the production default. The producer emits
no campaign artifact when the 42-record matrix is incomplete or any required
execution cannot be verified, and the publisher re-verifies that complete
artifact. A blocked or partial campaign therefore cannot produce a
native-method production conclusion.

## Network-free publication plan

The publisher can verify a completed campaign and emit a deterministic plan
without importing W&B SDKs:

```bash
PYTHONPATH=python .venv/bin/python \
  scripts/publish_ecocorridor_review_brief_report.py \
  --campaign /tmp/ecocorridor-review-brief-campaign.json \
  --dry-run \
  --output /tmp/ecocorridor-review-brief-publication-plan.json
```

The plan contains the dataset, exact 42 records, scorecards, claim ledger, and
a claim-bound animation story/transcript contract. It explicitly records
`video_rendered=false` and `video_logged=false`; no video or graphic is
fabricated. Any media production begins only after this real campaign exists
and uses its claim ledger.

## Fail-closed animation render

The renderer accepts only the complete campaign at the checked-out clean source
commit and requires explicit authorization. It rebuilds the story and every
claim-ledger source hash, requires the exact 42 records, 22 verified provider
conversations, and six pre-provider blocks, and binds the media to the original
campaign run. It then creates a local-only seekable Canvas film and an
accessible transcript:

```bash
PYTHONPATH=python .venv/bin/python \
  scripts/render_ecocorridor_review_brief_animation.py \
  --campaign /tmp/ecocorridor-review-brief-campaign.json \
  --output-directory /tmp/ecocorridor-review-brief-media \
  --authorize-verified-provider-media-render
```

The output is a 1280×720, 15 fps, silent H.264 High/yuv420p/TV-range MP4 with
an exact duration and frame count, plus interactive HTML, a poster, one named
checkpoint per scene, a contact sheet, transcript, render contract, validation
receipt, and byte manifest. The validator checks deterministic repeated frames,
keyboard playback and scrubbing, responsive aspect ratio, reduced motion, codec
and decode integrity, and absence of network assets. A failed or partial render
leaves no output directory or manifest, so it cannot reach publication.

## Append to the existing historical Report

Online publication requires the exact existing W&B Report URL. The publisher
never creates or discovers a replacement Report. It logs a new immutable
campaign artifact version, downloads and byte-verifies it, then appends one
marker-bounded owned section to the existing Report. Existing historical
blocks and earlier campaign sections are never deleted or rewritten. An exact
retry is idempotent.

```bash
PYTHONPATH=python .venv/bin/python \
  scripts/publish_ecocorridor_review_brief_report.py \
  --campaign /tmp/ecocorridor-review-brief-campaign.json \
  --entity ENTITY \
  --project PROJECT \
  --wandb-api-base-url https://api.wandb.ai \
  --wandb-web-base-url https://wandb.ai \
  --report-url "EXISTING_CREDENTIAL_FREE_HTTPS_REPORT_URL" \
  --media-manifest \
    /tmp/ecocorridor-review-brief-media/review-brief-animation-media-manifest.json \
  --authorize-synthetic-public-upload \
  --output /tmp/ecocorridor-review-brief-publication-receipt.json
```

The manual protected workflow
`.github/workflows/ecocorridor-review-brief-evaluation.yml` performs the real
campaign, then rendering and validation, before publication. The publisher
re-hashes the exact matching media before logging its W&B Video/Image keys or
adding the Report media browser. The Report also includes a table with exact
credential-free Weave trace URLs/IDs and ordered concrete tool attempts for
every provider conversation. Its environment must supply the existing Report
URL through `ECOCORRIDOR_REVIEW_BRIEF_REPORT_URL`; the historical URL is not
stored in source.
The publisher pins both the resumed Run SDK and artifact readback client to the
same declared API base and rejects Run, artifact, or Report URLs from another
web origin. This keeps Cloud, Dedicated, and Self-Managed publication targets
from silently crossing deployments.

## Interpretation limits

This is one synthetic, provider-specific campaign. It is not a general model
ranking or practitioner acceptance result. It establishes no observed animal
movement, crossing use, collision reduction, population effect, approved
corridor, or design-ready intervention. Connectivity values in the claim
ledger are copied only from the verified direct Ridgeway resource. The local
numeric projection does not measure provider numerical reasoning, the explicit
token-price estimate is not an invoice, and the closed topic check is a bounded
contract test rather than practitioner evidence that a selected action or
discipline is appropriate in the field.
