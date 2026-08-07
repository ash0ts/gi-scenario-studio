from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.guided_evidence import GuidedEvidenceResource
from ecocorridor_core.usefulness_reporting import (
    HistoricalReviewBriefSummary,
    UsefulnessCostBasis,
    UsefulnessPublicationPlan,
    build_usefulness_publication_plan,
    publication_plan_json,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SOURCE_COMMIT = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
MODEL_IDS = (
    "openai/gpt-oss-120b",
    "gateway/frontier-fixture",
    "gateway/efficient-fixture",
)
FROZEN_TIME = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _prepare_module():
    module_name = "_usefulness_prepare_script"
    module = sys.modules.get(module_name)
    if module is None:
        path = ROOT / "scripts/prepare_ecocorridor_usefulness_program.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


def _historical_campaign_bytes() -> tuple[bytes, object]:
    module_path = ROOT / "tests/python/test_review_brief_evaluation_campaign.py"
    module_name = "_usefulness_historical_campaign_fixture"
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    ridgeway = ROOT / "benchmarks/ecocorridor/ridgeway"
    projection = ridgeway / "ui_projection.json"
    manifest_path = ridgeway / "direct-evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = ridgeway / str(manifest["evidence_bundle"]["filename"])
    resource = GuidedEvidenceResource.verify(
        projection_path=projection,
        projection_sha256=hashlib.sha256(projection.read_bytes()).hexdigest(),
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        archive_path=archive,
        archive_sha256=str(manifest["evidence_bundle"]["sha256"]),
    )
    dataset = module.build_ridgeway_review_brief_dataset(resource)
    campaign = module._perfect_campaign(dataset)
    payload = (
        json.dumps(campaign.model_dump(mode="json"), sort_keys=True, indent=2).encode(
            "utf-8"
        )
        + b"\n"
    )
    return payload, campaign


def _historical_receipts(campaign: object) -> tuple[bytes, bytes]:
    run_id = 987654
    github = {
        "schema_version": "ecocorridor-github-actions-provenance/v1.0",
        "repository": campaign.source_repository,
        "run_id": run_id,
        "run_attempt": 1,
        "workflow_id": 12345,
        "workflow_path": ".github/workflows/ecocorridor-review-brief-evaluation.yml",
        "head_branch": "main",
        "head_sha": campaign.source_commit,
        "event": "workflow_dispatch",
        "conclusion": "success",
        "url": (
            f"https://github.com/{campaign.source_repository}/actions/runs/{run_id}"
        ),
        "artifact": {
            "id": 45678,
            "name": "ecocorridor-review-brief-evaluation",
            "digest": f"sha256:{'1' * 64}",
            "size_in_bytes": 123456,
        },
    }
    wandb = {
        "schema_version": "ecocorridor-review-brief-publication-receipt/v1",
        "status": "PUBLISHED",
        "section_id": "review-brief-agent-evaluation/v1",
        "campaign_sha256": campaign.campaign_sha256,
        "dataset_sha256": campaign.dataset.dataset_sha256,
        "run_id": campaign.campaign_wandb_run_id,
        "run_url": campaign.campaign_wandb_run_url,
        "artifact_ref": "entity/project/review-brief:v3",
        "artifact_url": "https://wandb.ai/entity/project/artifacts/review-brief/v3",
        "report_url": "https://wandb.ai/entity/project/reports/article",
        "video_rendered": False,
        "video_logged": False,
    }
    def serialize(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"

    return serialize(github), serialize(wandb)


def _cost_basis() -> UsefulnessCostBasis:
    return UsefulnessCostBasis.create(
        reference="Frozen synthetic test price sheet",
        rates_by_model={model_id: (1.0, 2.0) for model_id in MODEL_IDS},
    )


def _plan() -> UsefulnessPublicationPlan:
    return build_usefulness_publication_plan(
        ROOT,
        source_repository="ash0ts/gi-scenario-studio",
        source_commit=SOURCE_COMMIT,
        source_tree_clean=False,
        model_ids=MODEL_IDS,
        cost_basis=_cost_basis(),
        preregistered_at=FROZEN_TIME,
        frozen_at=FROZEN_TIME,
        generated_at=FROZEN_TIME,
    )


def test_publication_plan_is_deterministic_and_reports_only_observed_evidence() -> None:
    first = _plan()
    second = _plan()

    assert first.plan_sha256 == second.plan_sha256
    assert publication_plan_json(first) == publication_plan_json(second)
    assert first.protocol.agent_complexity_task_count == 300
    assert first.tables["usefulness_agent_task_matrix"]["row_count"] == 300
    assert first.tables["usefulness_safety_classes"]["row_count"] == 10
    assert first.tables["usefulness_safety_attacks"]["row_count"] == 1_000
    assert first.tables["usefulness_safety_classes"]["columns"] == [
        "attack_class",
        "planned_variants",
        "prevented_before_model",
        "attempted_by_model",
        "rejected_after_model",
        "incorrectly_released",
        "prohibited_claims",
        "sensitive_location_leaks",
        "unauthorized_action_executions",
        "integrity_failures",
        "trace_failures",
        "saved_drafts",
        "status",
    ]
    assert all(
        row[2:-1] == [None] * 10 and row[-1] == "PLANNED_NOT_OBSERVED"
        for row in first.tables["usefulness_safety_classes"]["rows"]
    )
    assert first.historical_review_brief.evidence_status == "NOT_SUPPLIED"
    assert first.media_status == "HISTORICAL_MEDIA_ONLY"
    assert first.program_manifest["stages"] == {
        "protocol": "COMPLETE",
        "independent_landscape_truth": "BLOCKED_INPUT",
        "agent_complexity": "BLOCKED_INPUT",
        "safety": "BLOCKED_INPUT",
        "practitioner_crossover": "BLOCKED_EXTERNAL",
        "partner_shadow": "BLOCKED_EXTERNAL",
    }
    claims = {claim.claim_id: claim.status.value for claim in first.claim_ledger.claims}
    assert set(claims.values()) == {"BLOCKED"}
    assert "Planned cells are not observed results" in first.report_copy["boundary"]


def test_publication_plan_rejects_payload_and_digest_tampering() -> None:
    payload = _plan().model_dump(mode="json")
    payload["artifact_payloads"]["protocol.json"]["title"] = "tampered"
    with pytest.raises(ValidationError, match="artifact manifest differs"):
        UsefulnessPublicationPlan.model_validate(payload)

    payload = _plan().model_dump(mode="json")
    payload["report_copy"]["boundary"] = "unbounded replacement"
    with pytest.raises(ValidationError, match="plan digest differs"):
        UsefulnessPublicationPlan.model_validate(payload)


def test_cost_basis_requires_three_pinned_nonnegative_model_rates() -> None:
    with pytest.raises(ValidationError, match="exactly three"):
        UsefulnessCostBasis.create(
            reference="Incomplete basis",
            rates_by_model={MODEL_IDS[0]: (1.0, 2.0)},
        )

    payload = _cost_basis().model_dump(mode="json")
    payload["rates_by_model"][MODEL_IDS[0]][0] = 999.0
    with pytest.raises(ValidationError, match="digest differs"):
        UsefulnessCostBasis.model_validate(payload)


def test_prepare_cli_writes_an_exact_offline_plan(tmp_path: Path) -> None:
    output = tmp_path / "usefulness-plan.json"
    completed = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "scripts/prepare_ecocorridor_usefulness_program.py"),
            "--source-repository",
            "ash0ts/gi-scenario-studio",
            "--frontier-model",
            MODEL_IDS[1],
            "--efficient-model",
            MODEL_IDS[2],
            "--cost-basis-reference",
            "Frozen synthetic test price sheet",
            "--continuity-input-rate",
            "1",
            "--continuity-output-rate",
            "2",
            "--frontier-input-rate",
            "1",
            "--frontier-output-rate",
            "2",
            "--efficient-input-rate",
            "1",
            "--efficient-output-rate",
            "2",
            "--preregistered-at",
            "2026-08-07T12:00:00+00:00",
            "--frozen-at",
            "2026-08-07T12:00:00+00:00",
            "--generated-at",
            "2026-08-07T12:00:00+00:00",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    validated = UsefulnessPublicationPlan.model_validate(payload)
    assert validated.plan_sha256 == canonical_sha256(
        validated.model_dump(mode="json", exclude={"plan_sha256"})
    )
    assert "new_effectiveness_results=0" in completed.stdout


def test_historical_summary_accepts_only_the_exact_retained_campaign_shape() -> None:
    with pytest.raises(ValidationError, match="exactly 42 records"):
        HistoricalReviewBriefSummary(
            evidence_status="VERIFIED_HISTORICAL",
            campaign_sha256="a" * 64,
            campaign_file_sha256="b" * 64,
            dataset_sha256="9" * 64,
            source_repository="owner/repository",
            source_commit="b" * 40,
            run_id="historical-run",
            run_url="https://wandb.example/entity/project/runs/historical-run",
            github_actions_provenance_file_sha256="c" * 64,
            github_artifact_digest=f"sha256:{'d' * 64}",
            wandb_readback_receipt_file_sha256="e" * 64,
            provider_trace_receipts_sha256="f" * 64,
            trace_integrity_verified=True,
            record_count=41,
            provider_conversation_count=22,
            pre_provider_block_count=6,
            decision="KEEP_DETERMINISTIC",
            bounded_conclusion="Retained negative efficiency result.",
            known_design_limitations=("Synthetic historical evidence only.",),
        )


def test_operator_hashes_without_producer_and_cloud_receipts_remain_unverified(
    tmp_path: Path,
) -> None:
    campaign_bytes, campaign = _historical_campaign_bytes()
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_bytes(campaign_bytes)
    evidence, receipt = _prepare_module()._load_historical(
        campaign_path,
        expected_file_sha256=hashlib.sha256(campaign_bytes).hexdigest(),
        expected_campaign_sha256=campaign.campaign_sha256,
        expected_source_commit=campaign.source_commit,
        github_actions_provenance_path=None,
        wandb_readback_receipt_path=None,
    )

    assert evidence is not None and receipt is not None
    assert evidence.evidence_status == "UNVERIFIED_HISTORICAL"
    assert receipt["evidence_status"] == "UNVERIFIED_HISTORICAL"
    assert set(evidence.verification_failures) == {
        "MISSING_GITHUB_ACTIONS_PROVENANCE_RECEIPT",
        "MISSING_WANDB_READBACK_RECEIPT",
    }
    plan = build_usefulness_publication_plan(
        ROOT,
        source_repository=campaign.source_repository,
        source_commit=SOURCE_COMMIT,
        source_tree_clean=False,
        model_ids=MODEL_IDS,
        cost_basis=_cost_basis(),
        preregistered_at=FROZEN_TIME,
        frozen_at=FROZEN_TIME,
        generated_at=FROZEN_TIME,
        historical_campaign=evidence,
    )
    assert plan.historical_review_brief.evidence_status == "UNVERIFIED_HISTORICAL"
    assert plan.historical_review_brief.trace_integrity_verified is False
    assert plan.historical_review_brief.context_reduction_fraction is None
    assert plan.historical_review_brief.decision == "NOT_EVALUATED"
    assert "No trace-integrity claim" in plan.report_copy["historical"]


def test_malformed_wandb_receipt_stays_unverified_without_optimistic_copy(
    tmp_path: Path,
) -> None:
    campaign_bytes, campaign = _historical_campaign_bytes()
    github_bytes, _ = _historical_receipts(campaign)
    campaign_path = tmp_path / "campaign.json"
    github_path = tmp_path / "github.json"
    wandb_path = tmp_path / "wandb.json"
    campaign_path.write_bytes(campaign_bytes)
    github_path.write_bytes(github_bytes)
    wandb_path.write_bytes(b"{")

    evidence, _ = _prepare_module()._load_historical(
        campaign_path,
        expected_file_sha256=hashlib.sha256(campaign_bytes).hexdigest(),
        expected_campaign_sha256=campaign.campaign_sha256,
        expected_source_commit=campaign.source_commit,
        github_actions_provenance_path=github_path,
        wandb_readback_receipt_path=wandb_path,
    )

    assert evidence is not None
    assert evidence.evidence_status == "UNVERIFIED_HISTORICAL"
    assert set(evidence.verification_failures) == {
        "NONCANONICAL_WANDB_READBACK_RECEIPT_BYTES",
        "WANDB_READBACK_RECEIPT_IDENTITY_MISMATCH",
    }
    plan = build_usefulness_publication_plan(
        ROOT,
        source_repository=campaign.source_repository,
        source_commit=SOURCE_COMMIT,
        source_tree_clean=False,
        model_ids=MODEL_IDS,
        cost_basis=_cost_basis(),
        preregistered_at=FROZEN_TIME,
        frozen_at=FROZEN_TIME,
        generated_at=FROZEN_TIME,
        historical_campaign=evidence,
    )
    assert plan.historical_review_brief.trace_integrity_verified is False
    assert plan.historical_review_brief.context_reduction_fraction is None
    assert plan.historical_review_brief.decision == "NOT_EVALUATED"


def test_mismatched_wandb_receipt_cannot_create_trace_integrity(
    tmp_path: Path,
) -> None:
    campaign_bytes, campaign = _historical_campaign_bytes()
    github_bytes, wandb_bytes = _historical_receipts(campaign)
    mismatched = json.loads(wandb_bytes)
    mismatched["run_id"] = "another-run"
    mismatched_bytes = (
        json.dumps(mismatched, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )

    campaign_path = tmp_path / "campaign.json"
    github_path = tmp_path / "github.json"
    wandb_path = tmp_path / "wandb.json"
    campaign_path.write_bytes(campaign_bytes)
    github_path.write_bytes(github_bytes)
    wandb_path.write_bytes(mismatched_bytes)
    evidence, _ = _prepare_module()._load_historical(
        campaign_path,
        expected_file_sha256=hashlib.sha256(campaign_bytes).hexdigest(),
        expected_campaign_sha256=campaign.campaign_sha256,
        expected_source_commit=campaign.source_commit,
        github_actions_provenance_path=github_path,
        wandb_readback_receipt_path=wandb_path,
    )
    assert evidence is not None
    assert evidence.evidence_status == "UNVERIFIED_HISTORICAL"
    assert evidence.verification_failures == (
        "WANDB_READBACK_RECEIPT_IDENTITY_MISMATCH",
    )


def test_exact_producer_and_wandb_receipts_enable_bounded_historical_status(
    tmp_path: Path,
) -> None:
    campaign_bytes, campaign = _historical_campaign_bytes()
    github_bytes, wandb_bytes = _historical_receipts(campaign)
    campaign_path = tmp_path / "campaign.json"
    github_path = tmp_path / "github.json"
    wandb_path = tmp_path / "wandb.json"
    campaign_path.write_bytes(campaign_bytes)
    github_path.write_bytes(github_bytes)
    wandb_path.write_bytes(wandb_bytes)
    evidence, receipt = _prepare_module()._load_historical(
        campaign_path,
        expected_file_sha256=hashlib.sha256(campaign_bytes).hexdigest(),
        expected_campaign_sha256=campaign.campaign_sha256,
        expected_source_commit=campaign.source_commit,
        github_actions_provenance_path=github_path,
        wandb_readback_receipt_path=wandb_path,
    )
    assert evidence is not None and receipt is not None
    assert evidence.evidence_status == "VERIFIED_HISTORICAL"
    assert evidence.verification_failures == ()
    assert receipt["evidence_status"] == "VERIFIED_HISTORICAL"

    plan = build_usefulness_publication_plan(
        ROOT,
        source_repository=campaign.source_repository,
        source_commit=SOURCE_COMMIT,
        source_tree_clean=False,
        model_ids=MODEL_IDS,
        cost_basis=_cost_basis(),
        preregistered_at=FROZEN_TIME,
        frozen_at=FROZEN_TIME,
        generated_at=FROZEN_TIME,
        historical_campaign=evidence,
    )
    summary = plan.historical_review_brief
    assert summary.evidence_status == "VERIFIED_HISTORICAL"
    assert summary.trace_integrity_verified is True
    assert summary.github_actions_provenance_file_sha256 == hashlib.sha256(
        github_bytes
    ).hexdigest()
    assert summary.wandb_readback_receipt_file_sha256 == hashlib.sha256(
        wandb_bytes
    ).hexdigest()
    assert plan.artifact_payloads["historical-github-actions-provenance.json"] == (
        json.loads(github_bytes)
    )
    assert plan.artifact_payloads["historical-wandb-readback-receipt.json"] == (
        json.loads(wandb_bytes)
    )
    trace_payload = plan.artifact_payloads["historical-provider-trace-receipts.json"]
    assert trace_payload["provider_conversation_count"] == 22
    assert trace_payload["receipts_sha256"] == summary.provider_trace_receipts_sha256

    tampered = plan.model_dump(mode="json")
    tampered["artifact_payloads"]["historical-wandb-readback-receipt.json"][
        "run_id"
    ] = "different-run"
    changed_wandb_bytes = (
        json.dumps(
            tampered["artifact_payloads"]["historical-wandb-readback-receipt.json"],
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    changed_wandb_sha = hashlib.sha256(changed_wandb_bytes).hexdigest()
    tampered["historical_review_brief"][
        "wandb_readback_receipt_file_sha256"
    ] = changed_wandb_sha
    tampered["artifact_payloads"]["historical-review-brief.json"][
        "wandb_readback_receipt_file_sha256"
    ] = changed_wandb_sha
    artifact_rows = []
    for name, payload in sorted(tampered["artifact_payloads"].items()):
        content = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        artifact_rows.append(
            {
                "logical_name": name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    tampered["artifact_manifest_sha256"] = canonical_sha256(artifact_rows)
    tampered["plan_sha256"] = canonical_sha256(
        {key: value for key, value in tampered.items() if key != "plan_sha256"}
    )
    with pytest.raises(ValidationError, match="receipt identities are inconsistent"):
        UsefulnessPublicationPlan.model_validate(tampered)


def test_protected_workflow_requires_exact_historical_campaign_provenance() -> None:
    workflow = (
        ROOT / ".github/workflows/ecocorridor-usefulness-protocol.yml"
    ).read_text(encoding="utf-8")
    required = (
        "historical_evaluation_run_id:",
        "historical_evaluation_source_commit:",
        "historical_campaign_file_sha256:",
        "historical_campaign_sha256:",
        "scripts/resolve_github_actions_provenance.py",
        ".github/workflows/ecocorridor-review-brief-evaluation.yml",
        "--expected-artifact-name ecocorridor-review-brief-evaluation",
        "digest-mismatch: error",
        "--historical-campaign-file-sha256",
        "--historical-campaign-sha256",
        "--historical-campaign-source-commit",
        "--historical-github-actions-provenance",
        "--historical-wandb-readback-receipt",
        "--historical-verification-receipt",
        "--require-historical-campaign",
        "--require-verified-historical",
        "github-actions-provenance.json",
        "input-verification-receipt.json",
    )
    assert all(token in workflow for token in required)
    assert "actions: read" in workflow


def test_protected_prepare_mode_rejects_missing_historical_bytes(tmp_path: Path) -> None:
    output = tmp_path / "must-not-exist.json"
    completed = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "scripts/prepare_ecocorridor_usefulness_program.py"),
            "--source-repository",
            "ash0ts/gi-scenario-studio",
            "--frontier-model",
            MODEL_IDS[1],
            "--efficient-model",
            MODEL_IDS[2],
            "--cost-basis-reference",
            "Frozen synthetic test price sheet",
            "--continuity-input-rate",
            "1",
            "--continuity-output-rate",
            "2",
            "--frontier-input-rate",
            "1",
            "--frontier-output-rate",
            "2",
            "--efficient-input-rate",
            "1",
            "--efficient-output-rate",
            "2",
            "--preregistered-at",
            "2026-08-07T12:00:00+00:00",
            "--frozen-at",
            "2026-08-07T12:00:00+00:00",
            "--generated-at",
            "2026-08-07T12:00:00+00:00",
            "--require-historical-campaign",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "requires historical campaign bytes" in completed.stderr
    assert not output.exists()
