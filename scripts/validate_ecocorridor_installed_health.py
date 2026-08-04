"""Validate two receipts emitted by the actual installed EcoCorridor host."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

MAX_RECEIPT_BYTES = 16 * 1024
SCHEMA_VERSION = "ecocorridor-installed-host-health/v1.0"
RECEIPT_FIELDS = {
    "schema_version",
    "health_run_id",
    "application_id",
    "application_version",
    "source_commit_sha",
    "release_target",
    "release_manifest_sha256",
    "release_manifest_verified",
    "worker_started",
    "system_ping",
    "guided_open",
    "keychain",
}


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _reject_sensitive_receipt_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold()
            if any(
                token in normalized for token in ("secret", "password", "credential", "account")
            ):
                raise ValueError("installed-host receipt contains a prohibited sensitive field")
            _reject_sensitive_receipt_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_receipt_fields(nested)


def load_receipt(
    path: Path,
    *,
    expected_version: str,
    expected_source_commit: str,
    expected_target: str,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("installed-host health receipt must be a regular file")
    if path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ValueError("installed-host health receipt exceeds its byte limit")
    receipt = _object(json.loads(path.read_text(encoding="utf-8")), label="receipt")
    if set(receipt) != RECEIPT_FIELDS:
        raise ValueError("installed-host receipt fields do not match the bounded schema")
    _reject_sensitive_receipt_fields(receipt)
    expected = {
        "schema_version": SCHEMA_VERSION,
        "application_id": "org.ecocorridor.scenariostudio",
        "application_version": expected_version,
        "source_commit_sha": expected_source_commit,
        "release_target": expected_target,
        "release_manifest_verified": True,
        "worker_started": True,
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ValueError(f"installed-host receipt has invalid {field}")
    for field in ("health_run_id", "release_manifest_sha256"):
        value = receipt.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"installed-host receipt is missing {field}")
    manifest_sha = receipt["release_manifest_sha256"]
    if len(manifest_sha) != 64 or any(char not in "0123456789abcdef" for char in manifest_sha):
        raise ValueError("installed-host release manifest hash is malformed")

    ping = _object(receipt.get("system_ping"), label="system_ping")
    if (
        ping.get("api_version") != "1.0"
        or ping.get("schema_version") != "1.0"
        or ping.get("engine_configured") is not True
        or not isinstance(ping.get("method_count"), int)
        or ping["method_count"] < 2
    ):
        raise ValueError("installed host did not return an authoritative engine-backed ping")
    guided = _object(receipt.get("guided_open"), label="guided_open")
    if guided.get("state") != "VERIFIED_FIXTURE_REPLAY":
        raise ValueError("installed host did not open the verified Ridgeway replay")
    evidence_sha = guided.get("evidence_manifest_sha256")
    if (
        not isinstance(evidence_sha, str)
        or len(evidence_sha) != 64
        or any(char not in "0123456789abcdef" for char in evidence_sha)
    ):
        raise ValueError("installed host returned an invalid guided evidence hash")
    keychain = _object(receipt.get("keychain"), label="keychain")
    if set(keychain) != {
        "write_succeeded",
        "exact_read_succeeded",
        "delete_succeeded",
        "post_delete_absent",
    } or any(keychain.get(field) is not True for field in keychain):
        raise ValueError("installed host did not complete the isolated keychain roundtrip")
    return receipt


def validate_restart(receipts: list[dict[str, Any]]) -> None:
    if len(receipts) != 2:
        raise ValueError("exactly two installed-host receipts are required")
    if receipts[0]["health_run_id"] == receipts[1]["health_run_id"]:
        raise ValueError("restart verification requires two distinct native host launches")
    if receipts[0]["release_manifest_sha256"] != receipts[1]["release_manifest_sha256"]:
        raise ValueError("native host restarts verified different release manifests")
    if (
        receipts[0]["guided_open"]["evidence_manifest_sha256"]
        != receipts[1]["guided_open"]["evidence_manifest_sha256"]
    ):
        raise ValueError("native host restarts opened different guided evidence")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", action="append", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--expected-target", required=True)
    arguments = parser.parse_args()
    receipts = [
        load_receipt(
            path,
            expected_version=arguments.expected_version,
            expected_source_commit=arguments.expected_source_commit,
            expected_target=arguments.expected_target,
        )
        for path in arguments.receipt
    ]
    validate_restart(receipts)
    print("EcoCorridor installed native host health and restart receipts passed.")


if __name__ == "__main__":
    main()
