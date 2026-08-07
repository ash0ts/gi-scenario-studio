# Human-readable W&B article

The historical W&B Report is an experiment record. It is deliberately left
unchanged. A separate article tells the result in plain language:

> Passing the model is not the same as being ready to build.

The article begins with the decision and the Ridgeway example. Technical hashes
and provenance links appear at the end, where they can support the explanation
instead of replacing it.

## What the article says

Ridgeway is a synthetic audit drill. For its carry-forward candidate:

- all 12 synthetic scenarios completed and no required connectivity check
  failed;
- screening cost is USD 400,000 under a USD 500,000 cap;
- modeled difficulty connecting habitat areas improved 1.60% in the least
  favorable safeguard case, so the check designed to avoid worsening the
  safeguard guild passed; and
- the model's road-area warning changed by 47.35% to 65.65%, above the authored
  15% review trigger.

Circuitscape uses an electrical-circuit analogy to show where modeled
connectivity may concentrate. The road-area number describes a shift in that
analogy. It does not predict animal movement or crossing use. It identifies a
place where the model and site assumptions need field review. The resulting
status is “carry forward for field and feasibility review,” not approval.

The article also reports the first agent experiment without overselling it. The
agent workflow used 10,542 provider input tokens versus 10,798 for the
full-context comparator—a 2.37% reduction, below the 25% promotion gate. The
deterministic checklist therefore remains the default. The old experiment also
used fixed traversal and an answer-oriented comparator, so it cannot establish
adaptive-agent usefulness.

## Reader-first safety boundary

Publication has two separate phases.

1. `verify` performs only reads. It verifies the historical W&B Run, all bytes
   in its versioned Artifact, all retained Weave trace receipts, the exact
   historical Report blocks, the pushed source commit, and the committed
   Ridgeway projection. It writes a canonical plan outside the repository.
2. `publish` reads every source again and requires an exact match with that
   plan. It creates a new W&B Report in the same project. It never assigns blocks
   to the historical Report, never accepts the historical Report as a
   destination, and never creates a W&B Run, Artifact, or result media.

Before the only `Report.save()` call, the publisher performs the full source
readback twice. After creation, it verifies the new Report body and checks that
the historical Report still has the same ID, title, block count, and block hash.

Retries discover and reuse only an exact report bearing the same publication
key. A title collision, changed source, duplicate publication key, or changed
body fails closed. The W&B Reports API has no compare-and-swap primitive, so
this design avoids in-place mutation entirely.

## Operator sequence

Both outputs must be outside the repository. Credentials stay in process memory
and are never written to the plan or receipt.

```bash
python scripts/publish_ecocorridor_human_readable_report.py verify \
  --source-report-url 'https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/…' \
  --authorize-source-readback \
  --output /private/tmp/ecocorridor-readable-report-plan.json
```

Review the exact plan and article copy before allowing the only write:

```bash
python scripts/publish_ecocorridor_human_readable_report.py publish \
  --source-report-url 'https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/…' \
  --plan /private/tmp/ecocorridor-readable-report-plan.json \
  --authorize-source-readback \
  --authorize-new-report \
  --output /private/tmp/ecocorridor-readable-report-receipt.json
```

This implementation has unit tests and a no-write verification path. Creating
a live derivative Report is a separately authorized operation; local tests
never claim that it happened. The exact remote readback receipt is the authority
for any published Report.
