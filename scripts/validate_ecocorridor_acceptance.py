#!/usr/bin/env python3
"""Validate signed, case-level practitioner-acceptance release evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

REQUIRED_TASKS = {
    "decision",
    "controlling_assumption",
    "safeguard_limitation",
    "evidence_source",
    "wandb_lineage",
    "next_feasibility_action",
}
REQUIRED_KEYS = {
    "schema_version",
    "protocol_id",
    "source_commit_sha",
    "application_version",
    "participant_count",
    "successful_participant_count",
    "tasks",
    "cases",
    "anonymous",
    "contains_personal_data",
    "coordinator_key_sha256",
    "study_coordinator_attestation",
    "completed_at",
}
REQUIRED_CASE_KEYS = {
    "participant_id",
    "eligible_role_attested",
    "uncoached",
    "facilitator_help",
    "task_outcomes",
    "case_record_sha256",
    "completed_at",
}
PROTOCOL_ID = "ecocorridor-practitioner-acceptance-v1"
ATTESTATION = (
    "An independent study coordinator attests that the six anonymous case records "
    "were collected under the published uncoached protocol."
)
GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
PARTICIPANT_ID = re.compile(r"participant-[0-9a-f]{12}\Z")


def _utc_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field} requires a UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} has an invalid timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{field} timestamp must be UTC.")
    return parsed


def _validate_case(payload: object, *, study_completed_at: datetime) -> tuple[str, str, bool]:
    if not isinstance(payload, dict) or set(payload) != REQUIRED_CASE_KEYS:
        raise ValueError("Each practitioner case must contain exactly the redacted case fields.")
    participant_id = payload.get("participant_id")
    record_sha = payload.get("case_record_sha256")
    if not isinstance(participant_id, str) or not PARTICIPANT_ID.fullmatch(participant_id):
        raise ValueError("Practitioner cases require anonymous, non-sequential participant IDs.")
    if not isinstance(record_sha, str) or not SHA256.fullmatch(record_sha):
        raise ValueError("Practitioner cases require a retained-record SHA-256.")
    if record_sha == "0" * 64:
        raise ValueError("Practitioner case record hashes cannot be placeholders.")
    if payload.get("eligible_role_attested") is not True:
        raise ValueError("Every practitioner case requires role-eligibility attestation.")
    if payload.get("uncoached") is not True:
        raise ValueError("Every practitioner case must be completed uncoached.")
    if type(payload.get("facilitator_help")) is not bool:
        raise ValueError("Every practitioner case must record facilitator-help status.")
    outcomes = payload.get("task_outcomes")
    if (
        not isinstance(outcomes, dict)
        or set(outcomes) != REQUIRED_TASKS
        or any(type(value) is not bool for value in outcomes.values())
    ):
        raise ValueError("Every practitioner case requires a Boolean outcome for every task.")
    completed_at = _utc_timestamp(payload.get("completed_at"), field="Practitioner case completion")
    if completed_at > study_completed_at:
        raise ValueError("A practitioner case cannot complete after the study record.")
    successful = not payload["facilitator_help"] and all(outcomes.values())
    return participant_id, record_sha, successful


def validate(
    payload: object,
    *,
    expected_commit: str | None = None,
    expected_version: str | None = None,
    expected_coordinator_key_sha256: str,
) -> tuple[int, int]:
    """Recompute acceptance from six signed-envelope case records."""

    if not isinstance(payload, dict) or set(payload) != REQUIRED_KEYS:
        raise ValueError("Practitioner acceptance must contain exactly the public release fields.")
    if payload.get("schema_version") != "1.1" or payload.get("protocol_id") != PROTOCOL_ID:
        raise ValueError("Practitioner acceptance uses an unsupported schema or protocol.")
    commit = payload.get("source_commit_sha")
    version = payload.get("application_version")
    if not isinstance(commit, str) or not GIT_COMMIT.fullmatch(commit):
        raise ValueError("Practitioner acceptance requires the exact lowercase source commit SHA.")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ValueError("Practitioner acceptance requires a semantic application version.")
    if expected_commit is not None and commit != expected_commit:
        raise ValueError("Practitioner acceptance belongs to a different source commit.")
    if expected_version is not None and version != expected_version.removeprefix("v"):
        raise ValueError("Practitioner acceptance belongs to a different application version.")
    if (
        not SHA256.fullmatch(expected_coordinator_key_sha256)
        or payload.get("coordinator_key_sha256") != expected_coordinator_key_sha256
    ):
        raise ValueError("Practitioner acceptance is not bound to the approved coordinator key.")
    if payload.get("study_coordinator_attestation") != ATTESTATION:
        raise ValueError("Practitioner acceptance is missing the exact coordinator attestation.")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or any(not isinstance(task, str) for task in tasks):
        raise ValueError("Practitioner acceptance tasks must be a string list.")
    if set(tasks) != REQUIRED_TASKS or len(tasks) != len(REQUIRED_TASKS):
        raise ValueError(
            "Practitioner acceptance does not cover every required comprehension task."
        )
    if payload.get("anonymous") is not True or payload.get("contains_personal_data") is not False:
        raise ValueError("Published practitioner evidence must be anonymous and redacted.")
    study_completed_at = _utc_timestamp(
        payload.get("completed_at"), field="Practitioner acceptance completion"
    )
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != 6:
        raise ValueError("Practitioner acceptance requires exactly six case-level records.")
    validated = [_validate_case(case, study_completed_at=study_completed_at) for case in cases]
    participant_ids = [item[0] for item in validated]
    record_hashes = [item[1] for item in validated]
    if len(set(participant_ids)) != 6 or len(set(record_hashes)) != 6:
        raise ValueError("Practitioner case IDs and retained-record hashes must be unique.")
    participants = len(validated)
    successful = sum(item[2] for item in validated)
    if payload.get("participant_count") != participants:
        raise ValueError("Participant count does not reconcile with the case records.")
    if payload.get("successful_participant_count") != successful:
        raise ValueError("Successful-participant count does not reconcile with task outcomes.")
    if successful not in {5, 6}:
        raise ValueError("Release requires five or six successful participants out of exactly six.")
    return participants, successful


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-version")
    parser.add_argument("--approved-coordinator-public-key", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        payload = json.loads(arguments.evidence.read_text(encoding="utf-8"))
        coordinator_key_sha256 = hashlib.sha256(
            arguments.approved_coordinator_public_key.read_bytes()
        ).hexdigest()
        participants, successful = validate(
            payload,
            expected_commit=arguments.expected_commit,
            expected_version=arguments.expected_version,
            expected_coordinator_key_sha256=coordinator_key_sha256,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        "Practitioner acceptance passed: "
        f"{successful}/{participants} signed-envelope cases completed every task."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
