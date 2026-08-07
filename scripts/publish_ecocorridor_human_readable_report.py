#!/usr/bin/env python3
"""Verify sources, then create a separate plain-language EcoCorridor W&B Report.

The ``verify`` phase is read-only and writes a canonical plan.  The ``publish``
phase reads every source again, requires the exact frozen plan, and creates a
new Report.  It never opens the historical Report as a mutation target and it
never creates a W&B Run, Artifact, or media item.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:  # Direct script execution resolves the sibling without a package prefix.
    from append_ecocorridor_usefulness_narrative import require_publishable_source
except ModuleNotFoundError:  # Test/import execution resolves ``scripts`` as a namespace package.
    from scripts.append_ecocorridor_usefulness_narrative import require_publishable_source
from ecocorridor_core.human_readable_report import (
    HumanReadableReportPlan,
    VerifiedHumanReadableReportPublisher,
    canonical_report_plan_bytes,
    validate_report_plan_bytes,
)
from ecocorridor_core.usefulness_narrative import (
    WandbUsefulnessNarrativeVerifier,
    load_ridgeway_narrative_evidence,
)

EXPECTED_ENTITY = "wandb"
EXPECTED_PROJECT = "ecocorridor-scenario-studio"
MAX_PLAN_BYTES = 2 * 1024 * 1024


def _outside_repository(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    return False


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _load_plan(path: Path) -> HumanReadableReportPlan:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError("human-readable Report plan is missing, symlinked, or oversized")
    return validate_report_plan_bytes(path.read_bytes())


def _source_reader(
    *,
    root: Path,
    source_commit: str,
    entity: str,
    project: str,
    api_base_url: str,
    web_base_url: str,
    source_report_url: str,
    wandb_module: Any | None = None,
    reports_module: Any | None = None,
):
    ridgeway = load_ridgeway_narrative_evidence(root)
    verifier = WandbUsefulnessNarrativeVerifier(
        entity=entity,
        project=project,
        api_base_url=api_base_url,
        web_base_url=web_base_url,
        authorize_report_verification=True,
        wandb_module=wandb_module,
        reports_module=reports_module,
    )
    receipt = verifier.verify(
        report_url=source_report_url,
        source_commit=source_commit,
        ridgeway=ridgeway,
    )
    return receipt, ridgeway


def verify_sources(
    *,
    root: Path,
    entity: str,
    project: str,
    api_base_url: str,
    web_base_url: str,
    source_report_url: str,
) -> HumanReadableReportPlan:
    source_commit = require_publishable_source(root)
    receipt, ridgeway = _source_reader(
        root=root,
        source_commit=source_commit,
        entity=entity,
        project=project,
        api_base_url=api_base_url,
        web_base_url=web_base_url,
        source_report_url=source_report_url,
    )
    from ecocorridor_core.human_readable_report import HumanReadableSourceSnapshot

    source = HumanReadableSourceSnapshot.from_verified_receipt(receipt, ridgeway=ridgeway)
    return HumanReadableReportPlan.create(source)


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--entity", default=EXPECTED_ENTITY)
    parser.add_argument("--project", default=EXPECTED_PROJECT)
    parser.add_argument("--wandb-api-base-url", default="https://api.wandb.ai")
    parser.add_argument("--wandb-web-base-url", default="https://wandb.ai")
    parser.add_argument("--source-report-url", required=True)
    parser.add_argument("--output", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="read and freeze exact sources; no W&B write")
    _common_arguments(verify)
    verify.add_argument("--authorize-source-readback", action="store_true")

    publish = commands.add_parser(
        "publish",
        help="re-read exact sources and create or reuse a separate verified Report snapshot",
    )
    _common_arguments(publish)
    publish.add_argument("--plan", type=Path, required=True)
    publish.add_argument("--authorize-source-readback", action="store_true")
    publish.add_argument("--authorize-new-report", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.repository_root.resolve()
    if arguments.entity != EXPECTED_ENTITY or arguments.project != EXPECTED_PROJECT:
        raise SystemExit("human-readable Report is bound to wandb/ecocorridor-scenario-studio")
    if not arguments.authorize_source_readback:
        raise SystemExit("remote source verification requires --authorize-source-readback")
    if not os.environ.get("WANDB_API_KEY"):
        raise SystemExit("remote W&B verification requires WANDB_API_KEY in process memory")
    if not _outside_repository(arguments.output, root):
        raise SystemExit("Report plan and receipt outputs must be written outside the repository")

    if arguments.command == "verify":
        plan = verify_sources(
            root=root,
            entity=arguments.entity,
            project=arguments.project,
            api_base_url=arguments.wandb_api_base_url,
            web_base_url=arguments.wandb_web_base_url,
            source_report_url=arguments.source_report_url,
        )
        _atomic_write(arguments.output, canonical_report_plan_bytes(plan))
        observed = validate_report_plan_bytes(arguments.output.read_bytes())
        if observed != plan:
            raise RuntimeError("human-readable Report plan failed exact local readback")
        print(
            json.dumps(
                {
                    "status": "VERIFIED_READ_ONLY",
                    "plan_sha256": plan.plan_sha256,
                    "article_sha256": plan.article.article_sha256,
                    "source_report_url": plan.source.historical_report_url,
                    "source_report_blocks_sha256": (
                        plan.source.historical_report_blocks_sha256
                    ),
                    "new_report_created": False,
                    "source_report_mutated": False,
                },
                sort_keys=True,
            )
        )
        return 0

    if not arguments.authorize_new_report:
        raise SystemExit("new Report creation requires --authorize-new-report")
    frozen_plan = _load_plan(arguments.plan)
    source_commit = require_publishable_source(root)
    if source_commit != frozen_plan.source.source_commit:
        raise SystemExit("frozen Report plan belongs to another source commit")

    import wandb_workspaces.reports.v2 as reports

    import wandb

    def read_sources():
        return _source_reader(
            root=root,
            source_commit=source_commit,
            entity=arguments.entity,
            project=arguments.project,
            api_base_url=arguments.wandb_api_base_url,
            web_base_url=arguments.wandb_web_base_url,
            source_report_url=arguments.source_report_url,
            wandb_module=wandb,
            reports_module=reports,
        )

    publisher = VerifiedHumanReadableReportPublisher(
        entity=arguments.entity,
        project=arguments.project,
        web_base_url=arguments.wandb_web_base_url,
        authorize_new_report=True,
        api=wandb.Api(overrides={"base_url": arguments.wandb_api_base_url}),
        reports_module=reports,
        source_reader=read_sources,
    )
    receipt = publisher.publish(frozen_plan)
    payload = (
        json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    _atomic_write(arguments.output, payload)
    observed = type(receipt).model_validate_json(arguments.output.read_bytes())
    if observed != receipt:
        raise RuntimeError("human-readable Report receipt failed exact local readback")
    print(
        json.dumps(
            {
                "status": receipt.status,
                "report_url": receipt.report_url,
                "source_report_preserved": receipt.source_report_preserved,
                "new_wandb_run_created": receipt.new_wandb_run_created,
                "new_artifact_created": receipt.new_artifact_created,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
