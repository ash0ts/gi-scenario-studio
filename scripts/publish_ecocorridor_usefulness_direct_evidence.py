#!/usr/bin/env python3
"""Publish one exact five-landscape direct-evidence package to W&B.

The command publishes computational evidence completeness only. Outcome
analysis, independent adjudication, and usefulness claims remain blocked.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ecocorridor_core.usefulness_direct_reporting import (
    DIRECT_UPLOAD_AUTHORIZATION,
    DirectEvidencePublicationReceipt,
    WandbDirectEvidencePublisher,
    prepare_direct_evidence_publication,
    publish_verified_direct_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--engine-manifest", required=True, type=Path)
    parser.add_argument("--engine-provenance", required=True, type=Path)
    parser.add_argument("--engine-attestation-bundle", type=Path)
    parser.add_argument("--engine-sha256", required=True)
    parser.add_argument(
        "--repository-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--entity")
    parser.add_argument("--project")
    parser.add_argument("--wandb-api-base-url")
    parser.add_argument("--wandb-web-base-url")
    parser.add_argument("--report-url")
    parser.add_argument(
        "--upload-authorization",
        help=f"must equal {DIRECT_UPLOAD_AUTHORIZATION}",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    plan = prepare_direct_evidence_publication(
        manifest_path=arguments.manifest,
        archive_path=arguments.archive,
        engine_manifest_path=arguments.engine_manifest,
        engine_provenance_path=arguments.engine_provenance,
        engine_attestation_bundle_path=arguments.engine_attestation_bundle,
        expected_engine_sha256=arguments.engine_sha256,
        repository_root=arguments.repository_root,
        upload_authorization=arguments.upload_authorization or "",
    )
    if arguments.dry_run:
        result = {
            "status": "DRY_RUN_VERIFIED",
            "plan_sha256": plan.plan_sha256,
            "run_id": plan.run_id,
            "job_count": 160,
            "typed_table_row_counts": {
                table.name: len(table.rows) for table in plan.tables
            },
            "direct_evidence_complete": True,
            "ecological_outcome_value_count": 0,
            "new_result_media_logged": False,
            "outcome_analysis": "BLOCKED",
            "adjudication": "BLOCKED",
            "usefulness_claims": "BLOCKED",
        }
        _atomic_json(arguments.output, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    required = (
        arguments.entity,
        arguments.project,
        arguments.wandb_api_base_url,
        arguments.wandb_web_base_url,
        arguments.report_url,
    )
    if not all(required):
        raise SystemExit(
            "online publication requires --entity, --project, --wandb-api-base-url, "
            "--wandb-web-base-url, and --report-url"
        )
    if not os.environ.get("WANDB_API_KEY"):
        raise SystemExit("online publication requires WANDB_API_KEY in process memory")
    publisher = WandbDirectEvidencePublisher(
        entity=arguments.entity,
        project=arguments.project,
        api_base_url=arguments.wandb_api_base_url,
        web_base_url=arguments.wandb_web_base_url,
        upload_authorization=arguments.upload_authorization,
    )
    receipt = publish_verified_direct_evidence(
        plan=plan,
        report_url=arguments.report_url,
        publisher=publisher,
    )
    _atomic_json(arguments.output, receipt.model_dump(mode="json"))
    observed = DirectEvidencePublicationReceipt.model_validate_json(
        arguments.output.read_bytes()
    )
    if observed != receipt:
        raise RuntimeError("direct-evidence publication receipt failed local readback")
    print(
        json.dumps(
            {
                "status": receipt.status,
                "run_url": receipt.run_url,
                "artifact_url": receipt.artifact_url,
                "report_url": receipt.report_url,
                "direct_job_count": 160,
                "outcome_analysis": "BLOCKED",
                "adjudication": "BLOCKED",
                "usefulness_claims": "BLOCKED",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
