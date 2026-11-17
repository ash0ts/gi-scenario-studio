"""Create and verify an artifact's first-party GitHub Actions producer identity.

GitHub's artifact REST representation binds an artifact to a workflow run ID,
but it does not expose the run attempt that produced the artifact.  The
producer therefore writes this record into the artifact before upload.  A
consumer resolves the immutable artifact ID and digest independently, then
checks this record against that REST provenance before trusting the payload.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ecocorridor-github-actions-producer-identity/v1.0"

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_WORKFLOW_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")
_GIT_REF = re.compile(r"refs/(?:heads|tags)/[A-Za-z0-9._/-]+")
_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "repository",
        "run_id",
        "run_attempt",
        "workflow_path",
        "git_ref",
        "workflow_ref",
        "workflow_sha",
        "head_sha",
    }
)


def _positive_integer(value: Any, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _string(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _validate_ref(value: Any) -> str:
    value = _string(value, label="Git ref")
    if not _GIT_REF.fullmatch(value) or ".." in value or "//" in value or value.endswith("/"):
        raise ValueError("Git ref is malformed")
    return value


def create_identity(
    *,
    repository: str,
    run_id: int,
    run_attempt: int,
    workflow_path: str,
    git_ref: str,
    workflow_ref: str,
    workflow_sha: str,
    head_sha: str,
) -> dict[str, object]:
    """Return a strict producer record from GitHub's immutable runtime values."""

    repository = _string(repository, label="repository")
    workflow_path = _string(workflow_path, label="workflow path")
    workflow_ref = _string(workflow_ref, label="workflow ref")
    workflow_sha = _string(workflow_sha, label="workflow SHA")
    head_sha = _string(head_sha, label="head SHA")
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be an owner/name pair")
    _positive_integer(run_id, label="run ID")
    _positive_integer(run_attempt, label="run attempt")
    if not _WORKFLOW_PATH.fullmatch(workflow_path):
        raise ValueError("workflow path is malformed")
    _validate_ref(git_ref)
    expected_workflow_ref = f"{repository}/{workflow_path}@{git_ref}"
    if workflow_ref != expected_workflow_ref:
        raise ValueError("workflow ref does not match repository, path, and Git ref")
    if not _SHA.fullmatch(workflow_sha) or not _SHA.fullmatch(head_sha):
        raise ValueError("workflow and head SHAs must be exact lowercase commit hashes")

    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow_path": workflow_path,
        "git_ref": git_ref,
        "workflow_ref": workflow_ref,
        "workflow_sha": workflow_sha,
        "head_sha": head_sha,
    }


def verify_identity(identity_payload: object, provenance_payload: object) -> dict[str, object]:
    """Verify an embedded producer record against independently resolved REST evidence."""

    if not isinstance(identity_payload, dict) or set(identity_payload) != _IDENTITY_KEYS:
        raise ValueError("producer identity must contain exactly the versioned fields")
    if not isinstance(provenance_payload, dict):
        raise TypeError("GitHub Actions provenance must be a JSON object")
    identity = create_identity(
        repository=identity_payload.get("repository"),  # type: ignore[arg-type]
        run_id=identity_payload.get("run_id"),  # type: ignore[arg-type]
        run_attempt=identity_payload.get("run_attempt"),  # type: ignore[arg-type]
        workflow_path=identity_payload.get("workflow_path"),  # type: ignore[arg-type]
        git_ref=identity_payload.get("git_ref"),  # type: ignore[arg-type]
        workflow_ref=identity_payload.get("workflow_ref"),  # type: ignore[arg-type]
        workflow_sha=identity_payload.get("workflow_sha"),  # type: ignore[arg-type]
        head_sha=identity_payload.get("head_sha"),  # type: ignore[arg-type]
    )
    if identity_payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("producer identity schema is unsupported")

    provenance_schema = provenance_payload.get("schema_version")
    if provenance_schema not in {
        "ecocorridor-github-actions-provenance/v1.0",
        "ecocorridor-github-actions-run-provenance/v1.0",
    }:
        raise ValueError("GitHub Actions provenance schema is unsupported")
    head_branch = provenance_payload.get("head_branch")
    if not isinstance(head_branch, str) or not head_branch:
        raise ValueError("GitHub Actions provenance has no head branch")
    expected_git_ref = f"refs/heads/{head_branch}"
    expected = {
        "repository": provenance_payload.get("repository"),
        "run_id": provenance_payload.get("run_id"),
        "run_attempt": provenance_payload.get("run_attempt"),
        "workflow_path": provenance_payload.get("workflow_path"),
        "git_ref": expected_git_ref,
        "workflow_ref": (
            f"{provenance_payload.get('repository')}/"
            f"{provenance_payload.get('workflow_path')}@{expected_git_ref}"
        ),
        # Protected evidence workflows are accepted only from the exact main
        # commit, so the workflow source and checked-out head must coincide.
        "workflow_sha": provenance_payload.get("head_sha"),
        "head_sha": provenance_payload.get("head_sha"),
    }
    mismatches = sorted(key for key, value in expected.items() if identity.get(key) != value)
    if mismatches:
        raise ValueError(
            "producer identity differs from independently resolved provenance: "
            + ", ".join(mismatches)
        )
    return identity


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
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
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create an embedded producer identity")
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--repository", required=True)
    create.add_argument("--run-id", required=True, type=int)
    create.add_argument("--run-attempt", required=True, type=int)
    create.add_argument("--workflow-path", required=True)
    create.add_argument("--git-ref", required=True)
    create.add_argument("--workflow-ref", required=True)
    create.add_argument("--workflow-sha", required=True)
    create.add_argument("--head-sha", required=True)

    verify = subparsers.add_parser("verify", help="verify identity against REST provenance")
    verify.add_argument("--identity", required=True, type=Path)
    verify.add_argument("--provenance", required=True, type=Path)

    arguments = parser.parse_args()
    try:
        if arguments.command == "create":
            payload = create_identity(
                repository=arguments.repository,
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                workflow_path=arguments.workflow_path,
                git_ref=arguments.git_ref,
                workflow_ref=arguments.workflow_ref,
                workflow_sha=arguments.workflow_sha,
                head_sha=arguments.head_sha,
            )
            _write_json_atomic(arguments.output, payload)
        else:
            payload = verify_identity(
                _read_json(arguments.identity),
                _read_json(arguments.provenance),
            )
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"Producer identity verified for run {payload['run_id']} attempt {payload['run_attempt']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
