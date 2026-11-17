"""Validate bounded browser-rendered public-replay copy with the release claim policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ecocorridor_core.rendered_claim_safety import (
    assert_safe_normalized_rendered_ecological_copy,
)

MAX_INPUT_BYTES = 16 * 1024 * 1024
EXPECTED_COVERAGE = {
    "steps": 5,
    "portfolios": 4,
    "scenarios": 12,
    "portfolioScenarioStates": 48,
    "controllingEvidenceDialogs": 48,
    "pairEvidenceDialogs": 96,
    "evidenceDialogs": 144,
    "sliderStates": 8,
    "focusStates": 5,
    "hoverStates": 5,
    "compactDarkSteps": 5,
    "snapshots": 220,
}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return value


def _validate_snapshot(value: Any, index: int) -> None:
    snapshot = _object(value, f"snapshot {index + 1}")
    if set(snapshot) != {"label", "bodyText", "attributes", "pseudoContent"}:
        raise ValueError(f"snapshot {index + 1} has an unsupported shape")
    label = snapshot["label"]
    body = snapshot["bodyText"]
    attributes = snapshot["attributes"]
    pseudo = snapshot["pseudoContent"]
    if not isinstance(label, str) or not label or len(label) > 256:
        raise ValueError(f"snapshot {index + 1} has an invalid label")
    if not isinstance(body, str) or not body.strip():
        raise TypeError(f"snapshot {index + 1} bodyText must be a non-empty string")
    if not isinstance(attributes, list) or not all(isinstance(item, str) for item in attributes):
        raise ValueError(f"snapshot {index + 1} attributes must be strings")
    if not isinstance(pseudo, list) or not all(isinstance(item, str) for item in pseudo):
        raise ValueError(f"snapshot {index + 1} pseudoContent must be strings")
    try:
        assert_safe_normalized_rendered_ecological_copy(
            {"bodyText": body, "attributes": attributes, "pseudoContent": pseudo}
        )
    except ValueError as exc:
        raise ValueError(
            f"rendered snapshot {label!r} violates ecological claim policy: {exc}"
        ) from exc


def validate(payload: Any, *, claims_only: bool = False) -> dict[str, int]:
    document = _object(payload, "rendered-copy payload")
    expected_keys = {"snapshots"} if claims_only else {"coverage", "snapshots"}
    if set(document) != expected_keys:
        raise ValueError("rendered-copy payload has an unsupported shape")
    snapshots = document.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("rendered-copy payload must contain snapshots")
    if len(snapshots) > EXPECTED_COVERAGE["snapshots"]:
        raise ValueError("rendered-copy payload exceeds the bounded snapshot count")
    for index, snapshot in enumerate(snapshots):
        _validate_snapshot(snapshot, index)

    if claims_only:
        if len(snapshots) != 1 or "coverage" in document:
            raise ValueError("claim probes must contain exactly one snapshot and no coverage")
        return {"snapshots": 1}

    coverage = _object(document.get("coverage"), "rendered-copy coverage")
    if coverage != EXPECTED_COVERAGE:
        raise ValueError(
            "rendered-copy coverage is incomplete: "
            f"expected {EXPECTED_COVERAGE!r}, received {coverage!r}"
        )
    if len(snapshots) != EXPECTED_COVERAGE["snapshots"]:
        raise ValueError("rendered-copy snapshot total does not match coverage")
    return EXPECTED_COVERAGE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--claims-only", action="store_true")
    args = parser.parse_args()
    if args.input.is_symlink() or not args.input.is_file():
        raise ValueError("rendered-copy input must be a regular file")
    if args.input.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("rendered-copy input exceeds the bounded size limit")
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    summary = validate(payload, claims_only=args.claims_only)
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
