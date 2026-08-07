#!/usr/bin/env python3
"""Freeze the EcoCorridor usefulness protocol and publication plan offline."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from ecocorridor_core.usefulness_reporting import (
    HistoricalCampaignEvidence,
    UsefulnessCostBasis,
    build_usefulness_publication_plan,
    publication_plan_json,
)

ROOT = Path(__file__).resolve().parents[1]
MAX_HISTORICAL_CAMPAIGN_BYTES = 32 * 1024 * 1024
MAX_HISTORICAL_RECEIPT_BYTES = 4 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _git_source(root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("checked-out source is not a full Git commit")
    return commit, not bool(status)


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("study timestamps must include a timezone")
    return parsed


def _load_historical(
    path: Path | None,
    *,
    expected_file_sha256: str | None,
    expected_campaign_sha256: str | None,
    expected_source_commit: str | None,
    github_actions_provenance_path: Path | None,
    wandb_readback_receipt_path: Path | None,
) -> tuple[HistoricalCampaignEvidence | None, dict[str, object] | None]:
    if path is None:
        if any(
            value is not None
            for value in (
                expected_file_sha256,
                expected_campaign_sha256,
                expected_source_commit,
                github_actions_provenance_path,
                wandb_readback_receipt_path,
            )
        ):
            raise ValueError("historical campaign identity was supplied without campaign bytes")
        return None, None
    if (
        expected_file_sha256 is None
        or expected_campaign_sha256 is None
        or expected_source_commit is None
        or not SHA256_PATTERN.fullmatch(expected_file_sha256)
        or not SHA256_PATTERN.fullmatch(expected_campaign_sha256)
        or not COMMIT_PATTERN.fullmatch(expected_source_commit)
    ):
        raise ValueError(
            "historical campaign requires exact file, campaign, and source-commit identities"
        )
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_HISTORICAL_CAMPAIGN_BYTES:
        raise ValueError("historical campaign is missing, symlinked, or oversized")

    def read_receipt(receipt_path: Path | None, *, label: str) -> bytes | None:
        if receipt_path is None:
            return None
        if (
            receipt_path.is_symlink()
            or not receipt_path.is_file()
            or receipt_path.stat().st_size > MAX_HISTORICAL_RECEIPT_BYTES
        ):
            raise ValueError(f"{label} is missing, symlinked, or oversized")
        return receipt_path.read_bytes()

    campaign_bytes = path.read_bytes()
    observed_file_sha256 = hashlib.sha256(campaign_bytes).hexdigest()
    if not hmac.compare_digest(observed_file_sha256, expected_file_sha256):
        raise ValueError("historical campaign file digest differs from the protected input")
    historical_evidence = HistoricalCampaignEvidence.from_bytes(
        campaign_bytes,
        github_actions_provenance_bytes=read_receipt(
            github_actions_provenance_path,
            label="historical GitHub Actions provenance receipt",
        ),
        wandb_readback_receipt_bytes=read_receipt(
            wandb_readback_receipt_path,
            label="historical W&B readback receipt",
        ),
    )
    verified = historical_evidence.campaign
    if not hmac.compare_digest(verified.campaign_sha256, expected_campaign_sha256):
        raise ValueError("historical campaign internal digest differs from the protected input")
    if verified.source_commit != expected_source_commit:
        raise ValueError("historical campaign source commit differs from its workflow run")
    if len(verified.records) != 42 or verified.provider_conversation_count != 22:
        raise ValueError(
            "historical campaign must contain exactly 42 records and 22 provider conversations"
        )
    receipt: dict[str, object] = {
        "schema_version": "ecocorridor-historical-campaign-input-receipt/v1.0",
        "file_sha256": observed_file_sha256,
        "campaign_sha256": verified.campaign_sha256,
        "source_commit": verified.source_commit,
        "record_count": len(verified.records),
        "provider_conversation_count": verified.provider_conversation_count,
        "wandb_run_id": verified.campaign_wandb_run_id,
        "wandb_run_url": verified.campaign_wandb_run_url,
        "evidence_status": historical_evidence.evidence_status,
        "github_actions_provenance_file_sha256": (
            historical_evidence.github_actions_provenance_file_sha256
        ),
        "wandb_readback_receipt_file_sha256": (
            historical_evidence.wandb_readback_receipt_file_sha256
        ),
        "provider_trace_receipts_sha256": (
            historical_evidence.provider_trace_receipts_sha256
        ),
        "verification_failures": list(historical_evidence.verification_failures),
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return historical_evidence, receipt


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-commit")
    parser.add_argument("--continuity-model", default="openai/gpt-oss-120b")
    parser.add_argument("--frontier-model", required=True)
    parser.add_argument("--efficient-model", required=True)
    parser.add_argument("--cost-basis-reference", required=True)
    parser.add_argument("--continuity-input-rate", type=float, required=True)
    parser.add_argument("--continuity-output-rate", type=float, required=True)
    parser.add_argument("--frontier-input-rate", type=float, required=True)
    parser.add_argument("--frontier-output-rate", type=float, required=True)
    parser.add_argument("--efficient-input-rate", type=float, required=True)
    parser.add_argument("--efficient-output-rate", type=float, required=True)
    parser.add_argument("--preregistered-at", type=_aware, required=True)
    parser.add_argument("--frozen-at", type=_aware, required=True)
    parser.add_argument("--generated-at", type=_aware, required=True)
    parser.add_argument("--historical-campaign", type=Path)
    parser.add_argument("--historical-campaign-file-sha256")
    parser.add_argument("--historical-campaign-sha256")
    parser.add_argument("--historical-campaign-source-commit")
    parser.add_argument("--historical-github-actions-provenance", type=Path)
    parser.add_argument("--historical-wandb-readback-receipt", type=Path)
    parser.add_argument("--historical-verification-receipt", type=Path)
    parser.add_argument("--require-historical-campaign", action="store_true")
    parser.add_argument("--require-verified-historical", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)

    repository_root = arguments.repository_root.resolve()
    checked_out_commit, clean = _git_source(repository_root)
    source_commit = arguments.source_commit or checked_out_commit
    if source_commit != checked_out_commit:
        raise ValueError("usefulness protocol source commit differs from checked-out HEAD")
    model_ids = (
        arguments.continuity_model,
        arguments.frontier_model,
        arguments.efficient_model,
    )
    if arguments.continuity_model != "openai/gpt-oss-120b":
        raise ValueError("the continuity model slot is fixed to openai/gpt-oss-120b")
    cost_basis = UsefulnessCostBasis.create(
        reference=arguments.cost_basis_reference,
        rates_by_model={
            arguments.continuity_model: (
                arguments.continuity_input_rate,
                arguments.continuity_output_rate,
            ),
            arguments.frontier_model: (
                arguments.frontier_input_rate,
                arguments.frontier_output_rate,
            ),
            arguments.efficient_model: (
                arguments.efficient_input_rate,
                arguments.efficient_output_rate,
            ),
        },
    )
    historical_campaign, historical_receipt = _load_historical(
        arguments.historical_campaign,
        expected_file_sha256=arguments.historical_campaign_file_sha256,
        expected_campaign_sha256=arguments.historical_campaign_sha256,
        expected_source_commit=arguments.historical_campaign_source_commit,
        github_actions_provenance_path=(
            arguments.historical_github_actions_provenance
        ),
        wandb_readback_receipt_path=(
            arguments.historical_wandb_readback_receipt
        ),
    )
    if arguments.require_historical_campaign and historical_campaign is None:
        raise ValueError("protected usefulness publication requires historical campaign bytes")
    if arguments.require_verified_historical and (
        historical_campaign is None
        or historical_campaign.evidence_status != "VERIFIED_HISTORICAL"
    ):
        failures = (
            historical_campaign.verification_failures
            if historical_campaign is not None
            else ("HISTORICAL_CAMPAIGN_NOT_SUPPLIED",)
        )
        raise ValueError(
            "protected usefulness publication requires verified historical receipts: "
            + ", ".join(failures)
        )
    if historical_receipt is not None and arguments.historical_verification_receipt is None:
        raise ValueError("historical campaign verification requires a durable receipt output")
    if historical_receipt is None and arguments.historical_verification_receipt is not None:
        raise ValueError("historical receipt output requires verified campaign bytes")
    plan = build_usefulness_publication_plan(
        repository_root,
        source_repository=arguments.source_repository,
        source_commit=source_commit,
        source_tree_clean=clean,
        model_ids=model_ids,
        cost_basis=cost_basis,
        preregistered_at=arguments.preregistered_at,
        frozen_at=arguments.frozen_at,
        generated_at=arguments.generated_at,
        historical_campaign=historical_campaign,
    )
    _atomic_text(arguments.output, publication_plan_json(plan))
    if historical_receipt is not None:
        assert arguments.historical_verification_receipt is not None
        _atomic_text(
            arguments.historical_verification_receipt,
            json.dumps(historical_receipt, sort_keys=True, indent=2) + "\n",
        )
        if json.loads(
            arguments.historical_verification_receipt.read_text(encoding="utf-8")
        ) != historical_receipt:
            raise RuntimeError("historical campaign verification receipt failed exact readback")
    observed = json.loads(arguments.output.read_text(encoding="utf-8"))
    if observed["plan_sha256"] != plan.plan_sha256:
        raise RuntimeError("written usefulness plan failed exact readback")
    print(
        f"Prepared frozen usefulness program {plan.plan_sha256[:16]}; "
        f"agent_tasks={plan.protocol.agent_complexity_task_count}; "
        f"historical={plan.historical_review_brief.evidence_status}; "
        "new_effectiveness_results=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
