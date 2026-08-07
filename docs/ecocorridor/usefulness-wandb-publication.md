# Usefulness protocol publication in W&B

The usefulness publisher appends a new, marker-bounded section to an existing
EcoCorridor W&B Report. It does not create or replace the article, reinterpret
its historical campaign, execute a model, or publish a study result.

The frozen plan logs these typed W&B Tables:

- experiment-stage status;
- five independent landscape definitions;
- fair method arms and the 300-cell base task matrix;
- safety attack classes, all 1,000 frozen attack-recipe identities, and planned
  sample counts;
- empty, typed result surfaces for model replicates, retrievals, turn context,
  failures, practitioner outcomes, paired effects, handoff, and shadow records;
- the bounded claim ledger.

The accompanying `usefulness-protocol` artifact contains the exact protocol,
landscape manifest, cost basis, historical-evidence projection, program status,
frozen safety recipes, and claim ledger. Every file is regenerated from
structured data, hashed before upload, downloaded again, and compared byte for
byte. The publisher then reads back the exact finished W&B run before it can
touch the Report.

## Fail-closed publication sequence

1. `prepare_ecocorridor_usefulness_program.py` freezes three distinct model IDs,
   the cost basis, source commit, fixtures, prompts, tools, validators,
   randomization, and statistical analysis.
2. `publish_ecocorridor_usefulness_report.py` requires that exact Git commit and
   a clean current tree, plus explicit authorization for the CC0 synthetic
   upload.
3. It logs the typed Tables and an immutable versioned artifact.
4. It downloads every artifact file and reads back the exact finished run.
5. It appends one bounded section to the supplied existing Report URL.
6. It reads the Report back and verifies both the original block prefix and the
   new marker/body digest before writing a success receipt.

Any ambiguous run identity, mismatched artifact byte, changed historical block,
wrong W&B deployment, missing consent, dirty source tree, or incomplete Report
readback produces no success receipt.

## Protocol-only boundary

The current publisher accepts only `HISTORICAL_MEDIA_ONLY` plans. It records
zero observed effectiveness results and logs no new result video. A protocol
may include the prior verified campaign as historical trace-integrity evidence,
but its fixed traversal and unfair single-shot comparator remain disclosed.
Planned experiments are always labelled `PLANNED_NOT_OBSERVED`, `BLOCKED_INPUT`,
or `BLOCKED_EXTERNAL` as appropriate.

The protected manual workflow is
`.github/workflows/ecocorridor-usefulness-protocol.yml`. It requires two
owner-approved model IDs in addition to the frozen continuity slot, protected
W&B credentials, and the existing Report URL. It also requires the immutable
GitHub Actions run ID and source commit for the prior Review Brief campaign,
the SHA-256 of the exact campaign file bytes, and the campaign's internally
verified SHA-256. The workflow accepts only the successful `main` run of
`.github/workflows/ecocorridor-review-brief-evaluation.yml`, resolves exactly
one `ecocorridor-review-brief-evaluation` artifact, verifies the GitHub artifact
digest during download, and then verifies all four identities before building
the append-only plan. It passes the generated GitHub Actions provenance receipt
and the prior `ecocorridor-review-brief-publication-receipt.json` into the plan
builder. The builder binds their exact canonical bytes and hashes to the prior
campaign repository, source commit, W&B run, immutable artifact reference,
dataset, and campaign. It also derives and retains an exact digest over all 22
provider/Weave trace-readback receipts. The retained input must still validate
as exactly 42 records and 22 provider-backed conversations. GitHub provenance,
the W&B readback receipt, the derived trace-receipt set, and an input-
verification receipt are preserved in the published artifact and are all
covered by its download/readback check.

Supplying campaign bytes and operator-entered hashes is not sufficient. If
either producer provenance or W&B readback receipt is absent, noncanonical, or
bound to a different source/run/artifact, the plan labels the bytes
`UNVERIFIED_HISTORICAL`. It may retain them for audit, but it makes no trace-
integrity, fail-closed execution, efficiency, or usefulness claim. The
protected workflow additionally passes `--require-verified-historical`, so an
unverified campaign cannot reach the W&B append step.

This workflow deliberately has no provider execution or animation step. It
cannot silently substitute a protocol-only summary for the historical
campaign, and the publication continues to describe that campaign as a useful
negative efficiency result rather than proof of agent usefulness.

Safety observations are not synthesized by this publisher. The frozen catalog
contains 100 distinct semantic combinations for each of ten attack classes and
a zero-call provider runner plan. A later campaign must cross the strict
observation-ingestion boundary with exact guard or Weave trace receipts. Its
release accounting keeps pre-model prevention, model attempts, post-model
rejection, and incorrect release separate, and independently gates prohibited
claims, sensitive-location leakage, unauthorized action execution,
tampering/integrity failures, trace failures, and saved drafts. A detected and
safely rejected attempt is recorded, but is not mislabeled as model robustness.

For a credential-free validation of an already frozen plan:

```bash
uv run --no-sync python scripts/publish_ecocorridor_usefulness_report.py \
  --plan /path/to/ecocorridor-usefulness-plan.json \
  --dry-run \
  --output /tmp/ecocorridor-usefulness-dry-run.json
```

Nothing published by this path establishes animal behavior, crossing use,
collision reduction, population response, biodiversity benefit, regulatory
approval, or a design-ready crossing.
