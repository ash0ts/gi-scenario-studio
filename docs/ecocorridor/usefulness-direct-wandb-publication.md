# Verified direct-evidence W&B publication

`publish_ecocorridor_usefulness_direct_evidence.py` is the only publisher for
the five-landscape, 160-job direct-evidence package. It is intentionally
separate from the broader usefulness-report publisher: completing direct
Circuitscape execution is not outcome analysis, independent adjudication, or a
usefulness result.

## What may be published

The command accepts only a package that the local
`verify_usefulness_evidence_package` gate revalidates against the supplied
executable hash and typed engine manifest. Before any W&B operation it also
requires:

- an exact protected-release engine provenance receipt whose GitHub/Sigstore
  attestation binds the approved repository, release workflow, main-branch
  source commit, executable, EngineManifest, signed inventory, and complete
  series/parallel/official-fixture conformance suite;
- the package's exact source commit to be the checked-out clean `HEAD`;
- the configured upstream branch to resolve to that same commit through
  authoritative `git ls-remote` readback;
- the package to retain a credential-free HTTPS identity; a standard local Git
  SSH origin is canonicalized to that HTTPS identity for equality checking,
  while authoritative `ls-remote` still uses the package's credential-free
  HTTPS URL; and
- the exact authorization token
  `PUBLIC_CC0_SELF_AUTHORED_USEFULNESS_DIRECT_EVIDENCE_ONLY_NO_REAL_LOCATIONS_OR_SPECIES`.

The deterministic `usefulness_fixture_generation` W&B Run logs two typed
Tables, each with exactly 160 rows:

- `usefulness_direct_jobs`: solver identity, input/output artifact hashes,
  accepted QA identity, and isolated process-exit confirmation.
- `usefulness_direct_resource_qa`: solver time, memory/energy measurement
  status, methods, and the exact resource-observation hash.

The Tables deliberately omit effective-resistance values, current-density
values, portfolio rankings, dispositions, or other ecological outcomes. The
immutable W&B artifact contains the exact local direct-evidence manifest,
archive, typed engine manifest, and attested engine provenance receipt.
Publication succeeds only after downloading
that artifact and matching every byte hash, then reading back a finished Run
whose config and summary preserve the blocked claim boundary.

## Report behavior

The publisher appends one separately marked **current verified direct
evidence** section to the existing W&B Report. It does not rewrite historical
sections and logs no media. Repeating the command reuses the deterministic Run,
artifact, and exact Report section. A conflicting Run identity, artifact, or
different current section fails closed.

The section may state only:

> Direct evidence is complete. Outcome analysis, independent adjudication, and
> usefulness claims remain BLOCKED.

It does not establish ecological validity, animal movement, crossing use,
collision reduction, biodiversity benefit, portfolio approval, practitioner
improvement, or agent usefulness.

## Invocation

Run from the exact clean, pushed source commit with `WANDB_API_KEY` available
only in process memory:

```text
python scripts/publish_ecocorridor_usefulness_direct_evidence.py \
  --manifest /approved/direct-evidence-manifest.json \
  --archive /approved/usefulness-direct-evidence-<hash>.tar.gz \
  --engine-manifest /approved/engine-manifest.json \
  --engine-provenance /approved/attested-engine-provenance.json \
  --engine-sha256 <exact-executable-sha256> \
  --entity <wandb-entity> \
  --project ecocorridor-scenario-studio \
  --wandb-api-base-url https://api.wandb.ai \
  --wandb-web-base-url https://wandb.ai \
  --report-url https://wandb.ai/<entity>/ecocorridor-scenario-studio/reports/<report> \
  --upload-authorization PUBLIC_CC0_SELF_AUTHORED_USEFULNESS_DIRECT_EVIDENCE_ONLY_NO_REAL_LOCATIONS_OR_SPECIES \
  --output /approved/usefulness-direct-publication-receipt.json
```

The command is not currently runnable from a dirty or unpushed implementation
worktree. That is a deliberate publication gate, not an instruction to bypass
or weaken source provenance.
