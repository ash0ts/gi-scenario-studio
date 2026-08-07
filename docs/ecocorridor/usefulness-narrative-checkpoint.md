# W&B usefulness narrative checkpoint

The append-only narrative publisher updates the existing EcoCorridor W&B
Report without creating a W&B Run, Artifact, experiment result, or media item.
It exists to make the article tell the current story honestly:

1. the historical 42-record campaign demonstrated trace integrity and
   fail-closed execution under its recorded conditions;
2. its 2.37% input-token reduction missed the 25% promotion gate, so
   `KEEP_DETERMINISTIC` remains the decision;
3. fixed traversal and an answer-contract-bearing comparator made that study
   unsuitable as proof of adaptive-agent usefulness;
4. the corrected evidence program is implemented; and
5. corrected provider, practitioner, handoff, and shadow-project results have
   not yet been observed.

The section describes the 160-job direct-study plan but publishes no local
execution result. Development executions remain `TEST_ONLY_UNPUBLISHABLE` and
cannot become W&B study evidence without the protected, signed and attested
release-engine provenance required by the direct-evidence boundary.

## Mutation boundary

`scripts/append_ecocorridor_usefulness_narrative.py` fails closed unless:

- the active branch is `codex/ecocorridor-scenario-studio`;
- `HEAD` is clean and `git ls-remote` confirms that it exactly matches the
  authoritative pushed branch;
- the approved implementation checkpoint is an ancestor;
- the origin is `ash0ts/gi-scenario-studio`;
- the retained historical W&B Run and immutable downloaded Artifact read back
  with the exact campaign, dataset, source, model, artifact manifest, canonical
  campaign bytes, all 22 trace receipts, counts, token totals, decision, and
  non-promotion status;
- the destination Report has the exact entity, project, ID, title, and retained
  historical marker fields and semantic body hash; and
- the user explicitly authorizes both the update and an operator-held exclusive
  editing window.

The W&B Reports SDK does not expose a compare-and-swap save. The publisher
therefore requires the exclusive window, checks the server `updated_at`
revision immediately before saving, appends one content-hashed owned section,
and reads the Report back. It verifies that the complete historical prefix is
unchanged and that exactly one matching section body exists. A retry is
idempotent. Conflicting v1 content is never rewritten.

The local receipt records zero corrected-campaign results and `false` for new
result media. It must be written outside the repository so a successful update
does not dirty the source tree.

```bash
set -a
source /approved/path/to/.env
set +a

python scripts/append_ecocorridor_usefulness_narrative.py \
  --entity wandb \
  --project ecocorridor-scenario-studio \
  --report-url 'https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/…' \
  --authorize-report-update \
  --authorize-exclusive-report-update \
  --output /private/tmp/ecocorridor-usefulness-narrative-receipt.json
```

No section from this path may claim animal behavior, crossing use, collision
reduction, population response, biodiversity benefit, corridor approval,
regulatory acceptance, or a design-ready intervention. No animation is added
until a completed, hash-bound corrected campaign exists.
