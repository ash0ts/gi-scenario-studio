#!/usr/bin/env python3
"""Audit, rehearse, or publish the R4c publication-only recovery.

This command has no inference adapter and cannot create experiment attempts.
It consumes the completed, immutable R4c files and an explicit publication
amendment.  Network writes occur only with ``--publish``.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gi_scenario_core.evidence_guide_r4c_publication_recovery import (
    RECOVERY_VERSION,
    publish_recovery,
    rehearse_recovery,
    validate_recovery_inputs,
)
from gi_scenario_core.evidence_guide_v2_campaign import (
    TARGET_PROJECT,
    json_sha256,
    write_json_atomic,
)
from gi_scenario_core.evidence_guide_v2_cloud import WandbCampaignPublisher
from gi_scenario_core.observability import CredentialSession

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAMPAIGN_ROOT = (
    ROOT / "dist" / "evidence-guide-r4c-grounding-final-r1" / "live"
)
DEFAULT_AMENDMENT = (
    ROOT
    / "benchmarks"
    / "evidence_guide"
    / "r4"
    / "publication-amendment-r4c.json"
)
DEFAULT_OUTPUT = DEFAULT_CAMPAIGN_ROOT / "publication-recovery"
PUBLISH_CONFIRMATION = "R4C-PUBLICATION-ONLY"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit-only", action="store_true")
    mode.add_argument("--fake-rehearsal", action="store_true")
    mode.add_argument("--publish", action="store_true")
    parser.add_argument("--campaign-root", type=Path, default=DEFAULT_CAMPAIGN_ROOT)
    parser.add_argument("--amendment", type=Path, default=DEFAULT_AMENDMENT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--entity", default=os.environ.get("WANDB_ENTITY"))
    parser.add_argument("--project", default=TARGET_PROJECT)
    parser.add_argument("--confirm-publication-only")
    return parser


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {path.name}")
    return value


def _write_or_verify(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        if _load_object(path) != dict(value):
            raise RuntimeError(f"R4C_RECOVERY_RECEIPT_DRIFT:{path.name}")
        return
    write_json_atomic(path, dict(value))


def _audit_summary(inputs: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "recovery_version": RECOVERY_VERSION,
        "mode": "audit_only",
        "network_calls": 0,
        "provider_calls": 0,
        "inference_reexecuted": False,
        "attempts_validated": 672,
        "provider_responses_validated": 672,
        "trace_acknowledgements_validated": 672,
        "block_receipts_validated": 3,
        "actual_answers_released": 0,
        "original_package_sha256": inputs.package_sha256,
        "original_files": dict(inputs.package_files),
        "analysis_file_sha256": inputs.analysis_file_sha256,
        "analysis_sha256": inputs.analysis_sha256,
        "preregistration_sha256": inputs.preregistration_sha256,
        "publication_amendment_sha256": inputs.amendment_sha256,
        "recovery_identity_sha256": inputs.recovery_identity_sha256,
        "production_state": "DISABLE_LIVE_GUIDE",
        "practitioner_usefulness_status": "not_measured",
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.project != TARGET_PROJECT:
        raise RuntimeError(f"R4c recovery requires exact project {TARGET_PROJECT}")
    if arguments.publish:
        if (
            not arguments.entity
            or arguments.confirm_publication_only != PUBLISH_CONFIRMATION
        ):
            raise RuntimeError(
                "publication requires --entity and "
                f"--confirm-publication-only {PUBLISH_CONFIRMATION}"
            )
    elif arguments.confirm_publication_only is not None:
        raise RuntimeError("publication confirmation is accepted only with --publish")

    inputs = validate_recovery_inputs(
        repository_root=ROOT,
        campaign_root=arguments.campaign_root,
        amendment_path=arguments.amendment,
    )
    if arguments.audit_only:
        print(json.dumps(_audit_summary(inputs), indent=2, sort_keys=True))
        return 0
    if arguments.fake_rehearsal:
        receipt = rehearse_recovery(
            inputs=inputs, output_dir=arguments.output_dir / "fake"
        )
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    output_dir = arguments.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    publisher = WandbCampaignPublisher(
        entity=str(arguments.entity),
        project=arguments.project,
        workspace=output_dir / "cloud",
        credentials=CredentialSession(),
        campaign_instance_sha256=inputs.study_instance_sha256,
    )
    preregistration_receipt = _load_object(inputs.preregistration_receipt_path)
    publisher.reuse_preregistration(
        preregistration_receipt, path=inputs.preregistration_path
    )
    amendment_receipt_path = output_dir / "amendment-cloud-receipt.json"
    if amendment_receipt_path.exists():
        amendment_receipt = _load_object(amendment_receipt_path)
        amendment_reference = publisher.reuse_protocol_amendment(
            amendment_receipt, path=inputs.amendment_path
        )
    else:
        amendment_receipt = publisher.publish_protocol_amendment(
            inputs.amendment_path
        ).as_dict()
        _write_or_verify(amendment_receipt_path, amendment_receipt)
        amendment_reference = str(amendment_receipt["artifact_reference"])
    cloud = publish_recovery(
        publisher=publisher,
        inputs=inputs,
        amendment_reference=amendment_reference,
    ).as_dict()
    body = {
        **_audit_summary(inputs),
        "mode": "wandb_publication_recovery",
        "network_calls": "publication_only",
        "amendment_cloud_receipt": amendment_receipt,
        "publication_cloud_receipt": cloud,
    }
    receipt = {**body, "receipt_sha256": json_sha256(body)}
    _write_or_verify(output_dir / "publication-recovery-cloud-receipt.json", receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
