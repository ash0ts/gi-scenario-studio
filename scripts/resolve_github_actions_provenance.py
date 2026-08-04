"""Resolve and verify one immutable GitHub Actions artifact provenance record."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Collection
from pathlib import Path
from typing import Any

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_WORKFLOW_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _positive_integer(value: Any, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _workflow_path(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("workflow run path must be a string")
    normalized = value.split("@", 1)[0]
    if not _WORKFLOW_PATH.fullmatch(normalized):
        raise ValueError("workflow run path is malformed")
    return normalized


def resolve_run(
    run_payload: object,
    *,
    repository: str,
    expected_run_id: int,
    expected_sha: str,
    expected_workflow_path: str,
    expected_events: Collection[str] = ("workflow_dispatch",),
) -> dict[str, Any]:
    """Validate one REST workflow-run record and bind its immutable identity."""

    run = _object(run_payload, label="workflow run")
    if not _SHA.fullmatch(expected_sha):
        raise ValueError("expected source commit is malformed")
    if not _WORKFLOW_PATH.fullmatch(expected_workflow_path):
        raise ValueError("expected workflow path is malformed")
    events = frozenset(expected_events)
    if not events or not events <= {"push", "workflow_dispatch"}:
        raise ValueError("expected workflow events are invalid")
    repository_record = _object(run.get("repository"), label="workflow repository")
    run_id = _positive_integer(run.get("id"), label="workflow run ID")
    workflow_id = _positive_integer(run.get("workflow_id"), label="workflow ID")
    run_attempt = _positive_integer(run.get("run_attempt"), label="workflow run attempt")
    workflow_path = _workflow_path(run.get("path"))
    url = run.get("html_url")
    canonical_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    accepted_urls = {canonical_url, f"{canonical_url}/attempts/{run_attempt}"}
    if (
        run_id != expected_run_id
        or repository_record.get("full_name") != repository
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("event") not in events
        or run.get("head_branch") != "main"
        or run.get("head_sha") != expected_sha
        or workflow_path != expected_workflow_path
        or not isinstance(url, str)
        or url not in accepted_urls
    ):
        raise ValueError("workflow run does not match the exact protected source")

    return {
        "schema_version": "ecocorridor-github-actions-run-provenance/v1.0",
        "repository": repository,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow_id": workflow_id,
        "workflow_path": workflow_path,
        "head_branch": "main",
        "head_sha": expected_sha,
        "event": run["event"],
        "conclusion": "success",
        "url": url,
    }


def resolve(
    run_payload: object,
    artifacts_payload: object,
    *,
    repository: str,
    expected_run_id: int,
    expected_sha: str,
    expected_workflow_path: str,
    expected_artifact_name: str,
    expected_events: Collection[str] = ("workflow_dispatch",),
) -> dict[str, Any]:
    """Validate REST run/artifact records and bind the selected artifact."""

    provenance = resolve_run(
        run_payload,
        repository=repository,
        expected_run_id=expected_run_id,
        expected_sha=expected_sha,
        expected_workflow_path=expected_workflow_path,
        expected_events=expected_events,
    )
    artifacts_envelope = _object(artifacts_payload, label="artifact response")

    artifacts = artifacts_envelope.get("artifacts")
    if (
        artifacts_envelope.get("total_count") != 1
        or not isinstance(artifacts, list)
        or len(artifacts) != 1
    ):
        raise ValueError("expected exactly one named workflow artifact")
    artifact = _object(artifacts[0], label="workflow artifact")
    artifact_id = _positive_integer(artifact.get("id"), label="artifact ID")
    workflow_run = _object(artifact.get("workflow_run"), label="artifact workflow run")
    digest = artifact.get("digest")
    size = _positive_integer(artifact.get("size_in_bytes"), label="artifact byte size")
    if (
        artifact.get("name") != expected_artifact_name
        or artifact.get("expired") is not False
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
        or workflow_run.get("id") != provenance["run_id"]
        or workflow_run.get("head_branch") != "main"
        or workflow_run.get("head_sha") != expected_sha
    ):
        raise ValueError("artifact does not belong to the exact protected workflow run")

    return {
        "schema_version": "ecocorridor-github-actions-provenance/v1.0",
        **{key: value for key, value in provenance.items() if key != "schema_version"},
        "artifact": {
            "id": artifact_id,
            "name": expected_artifact_name,
            "digest": digest,
            "size_in_bytes": size,
        },
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", required=True, type=Path)
    parser.add_argument("--artifacts-json", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--expected-run-id", required=True, type=int)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-workflow-path", required=True)
    parser.add_argument("--expected-artifact-name")
    parser.add_argument(
        "--expected-event",
        action="append",
        choices=("push", "workflow_dispatch"),
        dest="expected_events",
    )
    parser.add_argument("--run-only", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--github-output", type=Path)
    arguments = parser.parse_args()

    expected_events = arguments.expected_events or ["workflow_dispatch"]
    run_payload = json.loads(arguments.run_json.read_text(encoding="utf-8"))
    if arguments.run_only:
        if arguments.artifacts_json is not None or arguments.expected_artifact_name is not None:
            parser.error("run-only provenance cannot include artifact arguments")
        provenance = resolve_run(
            run_payload,
            repository=arguments.repository,
            expected_run_id=arguments.expected_run_id,
            expected_sha=arguments.expected_sha,
            expected_workflow_path=arguments.expected_workflow_path,
            expected_events=expected_events,
        )
    else:
        if arguments.artifacts_json is None or arguments.expected_artifact_name is None:
            parser.error("artifact provenance requires artifact JSON and an artifact name")
        provenance = resolve(
            run_payload,
            json.loads(arguments.artifacts_json.read_text(encoding="utf-8")),
            repository=arguments.repository,
            expected_run_id=arguments.expected_run_id,
            expected_sha=arguments.expected_sha,
            expected_workflow_path=arguments.expected_workflow_path,
            expected_artifact_name=arguments.expected_artifact_name,
            expected_events=expected_events,
        )
    if arguments.compare is not None:
        expected = json.loads(arguments.compare.read_text(encoding="utf-8"))
        if provenance != expected:
            raise SystemExit("GitHub Actions provenance differs from the preserved record")
    _write_json_atomic(arguments.output, provenance)
    if arguments.github_output is not None:
        if "artifact" not in provenance:
            raise SystemExit("run-only provenance has no artifact ID output")
        with arguments.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"artifact_id={provenance['artifact']['id']}\n")
    print(provenance.get("artifact", {}).get("digest", provenance["run_attempt"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
