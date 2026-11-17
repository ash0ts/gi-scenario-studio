# Practitioner acceptance protocol

This gate tests whether EcoCorridor communicates a defensible screening
decision to its intended audience. It is not a satisfaction survey and it does
not validate the ecological model.

## Participants

Recruit six people whose current work includes at least one of these roles:

- state DOT environmental review;
- wildlife-agency connectivity review;
- ecological connectivity consulting; or
- transportation ecology and crossing feasibility.

Do not publish names, employers, project locations, recordings, raw notes, or
other personal data in release evidence. A study coordinator retains any
consent records outside this repository.

## Uncoached task

Give each participant the installed application and the Ridgeway synthetic
fixture. After the permanent synthetic-data notice, ask them to complete the
workflow without product coaching. Product defects may be observed, but the
facilitator must not explain the decision logic.

The participant succeeds only if they can identify all six items from the
application's evidence:

1. the screening decision being made;
2. the controlling resistance assumption or scenario;
3. the safeguard or road-mask limitation;
4. the direct-run evidence source for a reported value;
5. the corresponding W&B lineage location in the professional desktop; and
6. the correct next ecological, field, or engineering feasibility action.

Record task-level outcomes before discussing the interface. Then collect
qualitative friction, misleading wording, navigation failures, and missing
evidence. A participant is not counted as successful if a facilitator supplied
one of the six answers.

## Release record

At least five of six participants must complete every task. Aggregate counts
are not release evidence. The independent study coordinator produces six
anonymous case records, one per participant, and signs the exact JSON bytes
with the coordinator Ed25519 key whose public half is anchored in the protected
`ecocorridor-release` GitHub environment.

The redacted JSON uses schema `1.1`. Each item in `cases` contains exactly:

- a random `participant-` identifier with 12 lowercase hexadecimal characters;
- `eligible_role_attested: true`, `uncoached: true`, and the Boolean
  `facilitator_help` observation;
- an exact Boolean outcome map for all six published tasks;
- the SHA-256 of the coordinator-retained redacted case record; and
- its UTC completion time.

The envelope contains the following public fields; `cases` must contain exactly
six distinct records rather than the one illustrative record shown here:

```json
{
  "schema_version": "1.1",
  "protocol_id": "ecocorridor-practitioner-acceptance-v1",
  "source_commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "application_version": "0.1.0",
  "participant_count": 6,
  "successful_participant_count": 5,
  "tasks": [
    "decision",
    "controlling_assumption",
    "safeguard_limitation",
    "evidence_source",
    "wandb_lineage",
    "next_feasibility_action"
  ],
  "cases": [
    {
      "participant_id": "participant-04a91d2f38bc",
      "eligible_role_attested": true,
      "uncoached": true,
      "facilitator_help": false,
      "task_outcomes": {
        "decision": true,
        "controlling_assumption": true,
        "safeguard_limitation": true,
        "evidence_source": true,
        "wandb_lineage": true,
        "next_feasibility_action": true
      },
      "case_record_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "completed_at": "YYYY-MM-DDTHH:MM:SSZ"
    }
  ],
  "anonymous": true,
  "contains_personal_data": false,
  "coordinator_key_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "study_coordinator_attestation": "An independent study coordinator attests that the six anonymous case records were collected under the published uncoached protocol.",
  "completed_at": "YYYY-MM-DDTHH:MM:SSZ"
}
```

The coordinator signs the exact evidence bytes and retains the underlying
redacted case records outside the public release:

```bash
openssl pkeyutl -sign -inkey coordinator-private-key.pem -rawin \
  -in practitioner-acceptance.json -out practitioner-acceptance.sig
```

Validate the case-level record against the approved public key before supplying
the HTTPS locations and SHA-256 values for both evidence and detached signature
to the protected release workflow:

```bash
python3 scripts/validate_ecocorridor_acceptance.py \
  practitioner-acceptance.json \
  --expected-commit "$RELEASE_COMMIT" \
  --expected-version 0.1.0 \
  --approved-coordinator-public-key coordinator-public-key.pem
```

The protected workflow independently verifies the detached Ed25519 signature,
the anchored public-key digest, all six unique case-record hashes, task-level
outcomes, and recomputed 5/6 threshold. Synthetic, self-reported,
aggregate-only, unsigned, or developer-authored results do not satisfy this
gate.
