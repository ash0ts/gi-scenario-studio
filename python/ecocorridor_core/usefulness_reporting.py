"""Fail-closed W&B projection for the EcoCorridor usefulness evidence program.

This module produces a deterministic, credential-free publication plan.  It
may project completed historical evidence, but it never turns a planned study
or an external prerequisite into an observed result.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, model_validator

from .contracts import ContractModel, canonical_sha256
from .review_brief_evaluation import (
    ReviewBriefCampaignArtifact,
    verify_review_brief_campaign,
)
from .usefulness_landscapes import (
    UsefulnessCatalog,
    build_usefulness_catalog,
    usefulness_program_sha256,
    usefulness_public_manifest,
)
from .usefulness_safety import build_safety_attack_catalog, safety_catalog_payload
from .usefulness_study import (
    ClaimGateEvidence,
    ExperimentClaimLedger,
    UsefulnessStudyProtocol,
    agent_complexity_task_matrix,
    build_default_synthetic_protocol,
    build_experiment_claim_ledger,
    deterministic_program_manifest,
    safety_attack_task_matrix,
)

USEFULNESS_REPORT_SCHEMA_VERSION = "ecocorridor-usefulness-publication/v1.0"
USEFULNESS_SECTION_ID = "usefulness-evidence-program/v1"
USEFULNESS_ARTIFACT_TYPE = "usefulness-protocol"
USEFULNESS_RUN_KIND = "usefulness_report"


def _canonical_receipt_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode(
        "utf-8"
    )


def _credential_free_https(value: object) -> bool:
    parsed = urlsplit(str(value or ""))
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _trace_receipts(campaign: ReviewBriefCampaignArtifact) -> tuple[dict[str, Any], ...]:
    retained: list[dict[str, Any]] = []
    for record in campaign.model_dump(mode="json")["records"]:
        provider = record["execution"].get("provider_execution")
        if provider is None:
            continue
        retained.append(
            {
                "case_id": record["case_id"],
                "method_id": record["method_id"],
                "provider_call_id": provider["provider_call_id"],
                "provider_response_sha256": provider["provider_response_sha256"],
                "weave_call_id": provider["weave_call_id"],
                "weave_trace_id": provider["weave_trace_id"],
                "weave_trace_url": provider["weave_trace_url"],
                "trace_status": provider["trace_status"],
                "trace_readback_verified": provider["trace_readback_verified"],
            }
        )
    return tuple(sorted(retained, key=lambda item: (item["method_id"], item["case_id"])))


def _historical_verification_failures(
    campaign: ReviewBriefCampaignArtifact,
    github_provenance: Mapping[str, Any] | None,
    github_provenance_file_sha256: str | None,
    wandb_receipt: Mapping[str, Any] | None,
    wandb_receipt_file_sha256: str | None,
) -> tuple[str, ...]:
    failures: list[str] = []
    if github_provenance is None or github_provenance_file_sha256 is None:
        failures.append("MISSING_GITHUB_ACTIONS_PROVENANCE_RECEIPT")
    else:
        canonical_sha = hashlib.sha256(
            _canonical_receipt_bytes(github_provenance)
        ).hexdigest()
        if canonical_sha != github_provenance_file_sha256:
            failures.append("NONCANONICAL_GITHUB_ACTIONS_PROVENANCE_BYTES")
        artifact = github_provenance.get("artifact")
        expected_url = (
            f"https://github.com/{campaign.source_repository}/actions/runs/"
            f"{github_provenance.get('run_id')}"
        )
        if (
            github_provenance.get("schema_version")
            != "ecocorridor-github-actions-provenance/v1.0"
            or github_provenance.get("repository") != campaign.source_repository
            or github_provenance.get("workflow_path")
            != ".github/workflows/ecocorridor-review-brief-evaluation.yml"
            or github_provenance.get("head_branch") != "main"
            or github_provenance.get("head_sha") != campaign.source_commit
            or github_provenance.get("event") != "workflow_dispatch"
            or github_provenance.get("conclusion") != "success"
            or not isinstance(github_provenance.get("run_id"), int)
            or github_provenance.get("run_id", 0) <= 0
            or not isinstance(github_provenance.get("run_attempt"), int)
            or github_provenance.get("run_attempt", 0) <= 0
            or not isinstance(github_provenance.get("workflow_id"), int)
            or github_provenance.get("workflow_id", 0) <= 0
            or github_provenance.get("url")
            not in {
                expected_url,
                f"{expected_url}/attempts/{github_provenance.get('run_attempt')}",
            }
            or not isinstance(artifact, Mapping)
            or artifact.get("name") != "ecocorridor-review-brief-evaluation"
            or not isinstance(artifact.get("id"), int)
            or artifact.get("id", 0) <= 0
            or not isinstance(artifact.get("size_in_bytes"), int)
            or artifact.get("size_in_bytes", 0) <= 0
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest", "")))
        ):
            failures.append("GITHUB_ACTIONS_PROVENANCE_IDENTITY_MISMATCH")
    if wandb_receipt is None or wandb_receipt_file_sha256 is None:
        failures.append("MISSING_WANDB_READBACK_RECEIPT")
    else:
        canonical_sha = hashlib.sha256(_canonical_receipt_bytes(wandb_receipt)).hexdigest()
        if canonical_sha != wandb_receipt_file_sha256:
            failures.append("NONCANONICAL_WANDB_READBACK_RECEIPT_BYTES")
        if (
            wandb_receipt.get("schema_version")
            != "ecocorridor-review-brief-publication-receipt/v1"
            or wandb_receipt.get("status") != "PUBLISHED"
            or wandb_receipt.get("section_id") != "review-brief-agent-evaluation/v1"
            or wandb_receipt.get("campaign_sha256") != campaign.campaign_sha256
            or wandb_receipt.get("dataset_sha256") != campaign.dataset.dataset_sha256
            or wandb_receipt.get("run_id") != campaign.campaign_wandb_run_id
            or wandb_receipt.get("run_url") != campaign.campaign_wandb_run_url
            or not re.fullmatch(
                r"[^/\s]+/[^/\s]+/[^:\s]+:v[0-9]+",
                str(wandb_receipt.get("artifact_ref", "")),
            )
            or not all(
                _credential_free_https(wandb_receipt.get(field))
                for field in ("run_url", "artifact_url", "report_url")
            )
        ):
            failures.append("WANDB_READBACK_RECEIPT_IDENTITY_MISMATCH")
    traces = _trace_receipts(campaign)
    if (
        len(traces) != campaign.provider_conversation_count
        or any(
            item["trace_status"] != "VERIFIED"
            or item["trace_readback_verified"] is not True
            or not item["weave_call_id"]
            or not item["weave_trace_id"]
            or not _credential_free_https(item["weave_trace_url"])
            for item in traces
        )
    ):
        failures.append("PROVIDER_TRACE_READBACK_RECEIPTS_INCOMPLETE")
    return tuple(sorted(set(failures)))


class UsefulnessCostBasis(ContractModel):
    schema_version: Literal["ecocorridor-usefulness-cost-basis/v1"] = (
        "ecocorridor-usefulness-cost-basis/v1"
    )
    reference: str = Field(min_length=3, max_length=512)
    currency: Literal["USD"] = "USD"
    rates_by_model: dict[str, tuple[float, float]]
    basis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_basis(self) -> Self:
        if len(self.rates_by_model) != 3:
            raise ValueError("usefulness cost basis requires exactly three model rates")
        if any(
            input_rate < 0 or output_rate < 0
            for input_rate, output_rate in self.rates_by_model.values()
        ):
            raise ValueError("provider rates cannot be negative")
        payload = self.model_dump(mode="json", exclude={"basis_sha256"})
        if self.basis_sha256 != canonical_sha256(payload):
            raise ValueError("usefulness cost-basis digest differs")
        return self

    @classmethod
    def create(
        cls,
        *,
        reference: str,
        rates_by_model: Mapping[str, tuple[float, float]],
    ) -> UsefulnessCostBasis:
        payload = {
            "schema_version": "ecocorridor-usefulness-cost-basis/v1",
            "reference": reference.strip(),
            "currency": "USD",
            "rates_by_model": dict(sorted(rates_by_model.items())),
        }
        return cls(**payload, basis_sha256=canonical_sha256(payload))


class HistoricalCampaignEvidence(ContractModel):
    """Exact local bytes and producer/cloud receipts for one prior campaign."""

    campaign: ReviewBriefCampaignArtifact
    campaign_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    github_actions_provenance: dict[str, Any] | None = None
    github_actions_provenance_file_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    wandb_readback_receipt: dict[str, Any] | None = None
    wandb_readback_receipt_file_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    provider_trace_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_status: Literal["UNVERIFIED_HISTORICAL", "VERIFIED_HISTORICAL"]
    verification_failures: tuple[str, ...]

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        verified = verify_review_brief_campaign(self.campaign)
        campaign_bytes = _canonical_receipt_bytes(verified.model_dump(mode="json"))
        if hashlib.sha256(campaign_bytes).hexdigest() != self.campaign_file_sha256:
            raise ValueError("historical campaign file digest differs from canonical bytes")
        traces_sha256 = canonical_sha256(list(_trace_receipts(verified)))
        if traces_sha256 != self.provider_trace_receipts_sha256:
            raise ValueError("historical provider trace-receipt digest differs")
        failures = _historical_verification_failures(
            verified,
            self.github_actions_provenance,
            self.github_actions_provenance_file_sha256,
            self.wandb_readback_receipt,
            self.wandb_readback_receipt_file_sha256,
        )
        if failures != self.verification_failures:
            raise ValueError("historical verification findings differ from retained receipts")
        expected_status = (
            "VERIFIED_HISTORICAL" if not failures else "UNVERIFIED_HISTORICAL"
        )
        if self.evidence_status != expected_status:
            raise ValueError("historical evidence status differs from receipt verification")
        return self

    @classmethod
    def from_bytes(
        cls,
        campaign_bytes: bytes,
        *,
        github_actions_provenance_bytes: bytes | None = None,
        wandb_readback_receipt_bytes: bytes | None = None,
    ) -> HistoricalCampaignEvidence:
        campaign = verify_review_brief_campaign(
            ReviewBriefCampaignArtifact.model_validate_json(campaign_bytes)
        )
        canonical_campaign = _canonical_receipt_bytes(campaign.model_dump(mode="json"))
        if campaign_bytes != canonical_campaign:
            raise ValueError("historical campaign bytes are not the canonical producer output")

        def parse_receipt(
            payload: bytes | None,
            *,
            label: str,
            receipt_kind: str,
        ) -> dict[str, Any] | None:
            if payload is None:
                return None
            try:
                observed = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {
                    "schema_version": "ecocorridor-unparseable-historical-receipt/v1.0",
                    "receipt_kind": receipt_kind,
                    "input_file_sha256": hashlib.sha256(payload).hexdigest(),
                    "parse_status": "INVALID_JSON",
                }
            if not isinstance(observed, dict):
                return {
                    "schema_version": "ecocorridor-unparseable-historical-receipt/v1.0",
                    "receipt_kind": receipt_kind,
                    "input_file_sha256": hashlib.sha256(payload).hexdigest(),
                    "parse_status": "NOT_A_JSON_OBJECT",
                    "label": label,
                }
            return observed

        github = parse_receipt(
            github_actions_provenance_bytes,
            label="historical GitHub Actions provenance receipt",
            receipt_kind="GITHUB_ACTIONS_PROVENANCE",
        )
        wandb = parse_receipt(
            wandb_readback_receipt_bytes,
            label="historical W&B readback receipt",
            receipt_kind="WANDB_READBACK",
        )
        github_sha = (
            hashlib.sha256(github_actions_provenance_bytes).hexdigest()
            if github_actions_provenance_bytes is not None
            else None
        )
        wandb_sha = (
            hashlib.sha256(wandb_readback_receipt_bytes).hexdigest()
            if wandb_readback_receipt_bytes is not None
            else None
        )
        failures = _historical_verification_failures(
            campaign,
            github,
            github_sha,
            wandb,
            wandb_sha,
        )
        return cls(
            campaign=campaign,
            campaign_file_sha256=hashlib.sha256(campaign_bytes).hexdigest(),
            github_actions_provenance=github,
            github_actions_provenance_file_sha256=github_sha,
            wandb_readback_receipt=wandb,
            wandb_readback_receipt_file_sha256=wandb_sha,
            provider_trace_receipts_sha256=canonical_sha256(
                list(_trace_receipts(campaign))
            ),
            evidence_status=(
                "VERIFIED_HISTORICAL" if not failures else "UNVERIFIED_HISTORICAL"
            ),
            verification_failures=failures,
        )


class HistoricalReviewBriefSummary(ContractModel):
    evidence_status: Literal[
        "NOT_SUPPLIED", "UNVERIFIED_HISTORICAL", "VERIFIED_HISTORICAL"
    ]
    campaign_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    campaign_file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_repository: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
    )
    source_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    run_id: str | None = None
    run_url: str | None = None
    github_actions_provenance_file_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    github_artifact_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    wandb_readback_receipt_file_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    provider_trace_receipts_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    trace_integrity_verified: bool = False
    verification_failures: tuple[str, ...] = ()
    record_count: int = Field(ge=0)
    provider_conversation_count: int = Field(ge=0)
    pre_provider_block_count: int = Field(ge=0)
    context_reduction_fraction: float | None = None
    decision: Literal["KEEP_DETERMINISTIC", "PROMOTE_NATIVE", "NOT_EVALUATED"]
    bounded_conclusion: str
    known_design_limitations: tuple[str, ...]

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        supplied = self.evidence_status != "NOT_SUPPLIED"
        verified = self.evidence_status == "VERIFIED_HISTORICAL"
        fields = (
            self.campaign_sha256,
            self.campaign_file_sha256,
            self.dataset_sha256,
            self.source_repository,
            self.source_commit,
            self.run_id,
            self.run_url,
            self.provider_trace_receipts_sha256,
        )
        if supplied != all(value is not None for value in fields):
            raise ValueError("historical evidence identity is incomplete")
        if not supplied and any(
            (
                self.record_count,
                self.provider_conversation_count,
                self.pre_provider_block_count,
            )
        ):
            raise ValueError("an absent historical campaign cannot report counts")
        if supplied and (
            self.record_count != 42 or self.provider_conversation_count != 22
        ):
            raise ValueError(
                "the retained historical campaign must contain exactly 42 records "
                "and 22 provider-backed conversations"
            )
        receipt_fields = (
            self.github_actions_provenance_file_sha256,
            self.github_artifact_digest,
            self.wandb_readback_receipt_file_sha256,
        )
        if verified != all(value is not None for value in receipt_fields):
            raise ValueError("verified historical evidence requires both exact readback receipts")
        if verified != self.trace_integrity_verified:
            raise ValueError("historical trace-integrity status differs from receipt verification")
        if verified and self.verification_failures:
            raise ValueError("verified historical evidence cannot retain verification failures")
        if self.evidence_status == "UNVERIFIED_HISTORICAL" and not self.verification_failures:
            raise ValueError("unverified historical evidence requires explicit failure reasons")
        return self


class UsefulnessPublicationPlan(ContractModel):
    schema_version: Literal["ecocorridor-usefulness-publication/v1.0"] = (
        USEFULNESS_REPORT_SCHEMA_VERSION
    )
    section_id: Literal["usefulness-evidence-program/v1"] = USEFULNESS_SECTION_ID
    source_repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree_clean: bool
    generated_at: AwareDatetime
    protocol: UsefulnessStudyProtocol
    cost_basis: UsefulnessCostBasis
    landscape_manifest: dict[str, Any]
    program_manifest: dict[str, Any]
    historical_review_brief: HistoricalReviewBriefSummary
    claim_ledger: ExperimentClaimLedger
    tables: dict[str, dict[str, Any]]
    artifact_payloads: dict[str, dict[str, Any]]
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_copy: dict[str, str]
    wandb_projection: dict[str, Any]
    media_status: Literal["HISTORICAL_MEDIA_ONLY", "RESULT_MEDIA_ATTACHED"]
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if not self.protocol.frozen or self.protocol.protocol_sha256 is None:
            raise ValueError("publication requires a frozen usefulness protocol")
        if self.protocol.source_commit != self.source_commit:
            raise ValueError("protocol and publication commits differ")
        if self.landscape_manifest.get("program_sha256") != usefulness_program_sha256(
            build_usefulness_catalog()
        ):
            raise ValueError("publication landscape program differs from source")
        expected_artifacts = canonical_sha256(
            [
                {
                    "logical_name": name,
                    "bytes": len(_artifact_bytes(payload)),
                    "sha256": hashlib.sha256(_artifact_bytes(payload)).hexdigest(),
                }
                for name, payload in sorted(self.artifact_payloads.items())
            ]
        )
        if self.artifact_manifest_sha256 != expected_artifacts:
            raise ValueError("usefulness artifact manifest differs from its payloads")
        historical = self.historical_review_brief
        if self.artifact_payloads.get("historical-review-brief.json") != (
            historical.model_dump(mode="json")
        ):
            raise ValueError("historical summary artifact differs from the plan")
        if (
            historical.evidence_status != "NOT_SUPPLIED"
            and historical.source_repository != self.source_repository
        ):
            raise ValueError("historical campaign belongs to a different source repository")
        github_payload = self.artifact_payloads.get(
            "historical-github-actions-provenance.json"
        )
        wandb_payload = self.artifact_payloads.get(
            "historical-wandb-readback-receipt.json"
        )
        trace_payload = self.artifact_payloads.get(
            "historical-provider-trace-receipts.json"
        )
        if not isinstance(github_payload, Mapping) or not isinstance(
            wandb_payload, Mapping
        ) or not isinstance(trace_payload, Mapping):
            raise TypeError("historical receipt payloads are absent")
        if historical.evidence_status == "VERIFIED_HISTORICAL":
            github_sha = hashlib.sha256(_artifact_bytes(github_payload)).hexdigest()
            wandb_sha = hashlib.sha256(_artifact_bytes(wandb_payload)).hexdigest()
            if (
                github_sha != historical.github_actions_provenance_file_sha256
                or wandb_sha != historical.wandb_readback_receipt_file_sha256
            ):
                raise ValueError("verified historical receipt bytes differ from their identities")
            artifact = github_payload.get("artifact")
            traces = trace_payload.get("receipts")
            github_run_id = github_payload.get("run_id")
            github_attempt = github_payload.get("run_attempt")
            expected_github_url = (
                f"https://github.com/{historical.source_repository}/actions/runs/"
                f"{github_run_id}"
            )
            if (
                github_payload.get("schema_version")
                != "ecocorridor-github-actions-provenance/v1.0"
                or github_payload.get("repository") != historical.source_repository
                or github_payload.get("head_sha") != historical.source_commit
                or github_payload.get("workflow_path")
                != ".github/workflows/ecocorridor-review-brief-evaluation.yml"
                or github_payload.get("head_branch") != "main"
                or github_payload.get("event") != "workflow_dispatch"
                or github_payload.get("conclusion") != "success"
                or not isinstance(github_run_id, int)
                or github_run_id <= 0
                or not isinstance(github_attempt, int)
                or github_attempt <= 0
                or github_payload.get("url")
                not in {
                    expected_github_url,
                    f"{expected_github_url}/attempts/{github_attempt}",
                }
                or not isinstance(artifact, Mapping)
                or artifact.get("name") != "ecocorridor-review-brief-evaluation"
                or artifact.get("digest") != historical.github_artifact_digest
                or not isinstance(artifact.get("id"), int)
                or artifact.get("id", 0) <= 0
                or not isinstance(artifact.get("size_in_bytes"), int)
                or artifact.get("size_in_bytes", 0) <= 0
                or wandb_payload.get("schema_version")
                != "ecocorridor-review-brief-publication-receipt/v1"
                or wandb_payload.get("section_id")
                != "review-brief-agent-evaluation/v1"
                or wandb_payload.get("campaign_sha256") != historical.campaign_sha256
                or wandb_payload.get("dataset_sha256") != historical.dataset_sha256
                or wandb_payload.get("run_id") != historical.run_id
                or wandb_payload.get("run_url") != historical.run_url
                or wandb_payload.get("status") != "PUBLISHED"
                or not re.fullmatch(
                    r"[^/\s]+/[^/\s]+/[^:\s]+:v[0-9]+",
                    str(wandb_payload.get("artifact_ref", "")),
                )
                or not all(
                    _credential_free_https(wandb_payload.get(field))
                    for field in ("run_url", "artifact_url", "report_url")
                )
                or trace_payload.get("schema_version")
                != "ecocorridor-historical-trace-receipts/v1.0"
                or trace_payload.get("evidence_status") != "VERIFIED_HISTORICAL"
                or trace_payload.get("campaign_sha256") != historical.campaign_sha256
                or trace_payload.get("provider_conversation_count")
                != historical.provider_conversation_count
                or trace_payload.get("receipts_sha256")
                != historical.provider_trace_receipts_sha256
                or not isinstance(traces, list)
                or len(traces) != historical.provider_conversation_count
                or canonical_sha256(traces)
                != historical.provider_trace_receipts_sha256
                or any(
                    item.get("trace_status") != "VERIFIED"
                    or item.get("trace_readback_verified") is not True
                    for item in traces
                    if isinstance(item, Mapping)
                )
                or any(not isinstance(item, Mapping) for item in traces)
            ):
                raise ValueError("verified historical receipt identities are inconsistent")
        elif historical.trace_integrity_verified:
            raise ValueError("unverified historical evidence cannot claim trace integrity")
        payload = self.model_dump(mode="json", exclude={"plan_sha256"})
        if self.plan_sha256 != canonical_sha256(payload):
            raise ValueError("usefulness publication plan digest differs")
        return self


def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _artifact_bytes(value: Mapping[str, Any]) -> bytes:
    return _json_text(value).encode("utf-8")


def _source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def derive_frozen_input_sha256s(
    repository_root: Path,
    *,
    source_commit: str,
    model_ids: Sequence[str],
    cost_basis: UsefulnessCostBasis,
    catalog: UsefulnessCatalog,
) -> dict[str, str]:
    """Bind every preregistered input without recording an absolute path."""

    study_source = repository_root / "python/ecocorridor_core/usefulness_study.py"
    safety_source = repository_root / "python/ecocorridor_core/usefulness_safety.py"
    agent_source = repository_root / "python/ecocorridor_core/review_brief_agent_process.py"
    tool_source = repository_root / "python/ecocorridor_core/review_brief.py"
    evaluator_source = repository_root / "python/ecocorridor_core/review_brief_evaluation.py"
    for source in (study_source, safety_source, agent_source, tool_source, evaluator_source):
        if source.is_symlink() or not source.is_file():
            raise ValueError("a frozen usefulness source is absent or unsafe")
    return {
        "fixtures": usefulness_program_sha256(catalog),
        "rubric": canonical_sha256(
            {
                "rubric": "ecocorridor-usefulness-adjudication/v1",
                "study": _source_sha256(study_source),
                "safety_recipes": _source_sha256(safety_source),
            }
        ),
        "randomization": canonical_sha256(
            {"design": "THREE_PERIOD_WILLIAMS_LATIN_SQUARE", "seeds": [1103, 2207, 3301, 4409, 5501]}
        ),
        "model_slate": canonical_sha256(list(model_ids)),
        "prompts": _source_sha256(agent_source),
        "tools": _source_sha256(tool_source),
        "validators": canonical_sha256(
            {"review_brief": _source_sha256(evaluator_source), "study": _source_sha256(study_source)}
        ),
        "cost_basis": cost_basis.basis_sha256,
        "statistical_analysis": canonical_sha256(
            {"source": _source_sha256(study_source), "bootstrap_resamples": 10_000, "confidence": 0.95}
        ),
        "source_commit": canonical_sha256({"source_commit": source_commit}),
    }


def summarize_historical_campaign(
    campaign: (
        HistoricalCampaignEvidence
        | ReviewBriefCampaignArtifact
        | Mapping[str, Any]
        | None
    ),
) -> HistoricalReviewBriefSummary:
    if campaign is None:
        return HistoricalReviewBriefSummary(
            evidence_status="NOT_SUPPLIED",
            record_count=0,
            provider_conversation_count=0,
            pre_provider_block_count=0,
            decision="NOT_EVALUATED",
            bounded_conclusion="No historical provider campaign was supplied to this plan.",
            known_design_limitations=(
                "No provider-backed result may be inferred from a protocol-only publication.",
            ),
        )
    if isinstance(campaign, HistoricalCampaignEvidence):
        evidence = campaign
    else:
        verified_campaign = verify_review_brief_campaign(campaign)
        evidence = HistoricalCampaignEvidence.from_bytes(
            _canonical_receipt_bytes(verified_campaign.model_dump(mode="json"))
        )
    verified = evidence.campaign
    if len(verified.records) != 42 or verified.provider_conversation_count != 22:
        raise ValueError(
            "the retained historical campaign must contain exactly 42 records "
            "and 22 provider-backed conversations"
        )
    decision = verified.default_method_decision
    provenance = evidence.github_actions_provenance or {}
    artifact = provenance.get("artifact")
    github_artifact_digest = (
        str(artifact.get("digest")) if isinstance(artifact, Mapping) else None
    )
    is_verified = evidence.evidence_status == "VERIFIED_HISTORICAL"
    return HistoricalReviewBriefSummary(
        evidence_status=evidence.evidence_status,
        campaign_sha256=verified.campaign_sha256,
        campaign_file_sha256=evidence.campaign_file_sha256,
        dataset_sha256=verified.dataset.dataset_sha256,
        source_repository=verified.source_repository,
        source_commit=verified.source_commit,
        run_id=verified.campaign_wandb_run_id,
        run_url=verified.campaign_wandb_run_url,
        github_actions_provenance_file_sha256=(
            evidence.github_actions_provenance_file_sha256 if is_verified else None
        ),
        github_artifact_digest=(github_artifact_digest if is_verified else None),
        wandb_readback_receipt_file_sha256=(
            evidence.wandb_readback_receipt_file_sha256 if is_verified else None
        ),
        provider_trace_receipts_sha256=evidence.provider_trace_receipts_sha256,
        trace_integrity_verified=is_verified,
        verification_failures=evidence.verification_failures,
        record_count=len(verified.records),
        provider_conversation_count=verified.provider_conversation_count,
        pre_provider_block_count=verified.pre_provider_block_count,
        context_reduction_fraction=(
            decision.context_reduction_fraction if is_verified else None
        ),
        decision=(decision.decision if is_verified else "NOT_EVALUATED"),
        bounded_conclusion=(
            "The historical campaign demonstrated exact trace/fail-closed integration and "
            "retained the deterministic production default."
            if is_verified
            else "Campaign bytes were supplied, but producer and cloud readback receipts did "
            "not jointly verify; no trace-integrity conclusion is available."
        ),
        known_design_limitations=(
            "The historical native workflow followed a fixed traversal order.",
            "The historical single-shot context included the expected answer contract.",
            "One synthetic landscape and one provider attempt do not establish usefulness.",
        ),
    )


def _table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> dict[str, Any]:
    return {"columns": list(columns), "rows": [list(row) for row in rows], "row_count": len(rows)}


def _build_tables(
    protocol: UsefulnessStudyProtocol,
    landscape_manifest: Mapping[str, Any],
    program_manifest: Mapping[str, Any],
    ledger: ExperimentClaimLedger,
) -> dict[str, dict[str, Any]]:
    task_rows = agent_complexity_task_matrix(protocol)
    safety_rows = safety_attack_task_matrix(protocol)
    return {
        "usefulness_experiment_tracks": _table(
            ["track", "status", "external"],
            [
                [name, status, status == "BLOCKED_EXTERNAL"]
                for name, status in sorted(program_manifest["stages"].items())
            ],
        ),
        "usefulness_landscapes": _table(
            ["family", "scenario_count", "portfolio_count", "direct_job_count", "evidence_state", "definition_sha256"],
            [
                [
                    item["family"],
                    len(item["scenario_ids"]),
                    len(item["portfolio_ids"]),
                    item["direct_job_count"],
                    landscape_manifest["evidence_state"],
                    item["definition_sha256"],
                ]
                for item in landscape_manifest["landscapes"]
            ],
        ),
        "usefulness_method_arms": _table(
            ["arm_id", "kind", "provider_backed", "agentic", "diagnostic_control"],
            [
                [arm.arm_id, arm.kind.value, arm.provider_backed, arm.agentic, arm.diagnostic_control]
                for arm in protocol.method_arms
            ],
        ),
        "usefulness_agent_task_matrix": _table(
            [
                "task_id",
                "task_sha256",
                "landscape_id",
                "finding_count",
                "distractor_share",
                "case_seed",
                "scored_invocations",
                "stratified_repeat_count",
            ],
            [
                [
                    row["task_id"],
                    row["task_sha256"],
                    row["landscape_id"],
                    row["finding_count"],
                    row["distractor_share"],
                    row["case_seed"],
                    row["scored_invocations"],
                    row["stratified_repeat_count"],
                ]
                for row in task_rows
            ],
        ),
        "usefulness_safety_classes": _table(
            [
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
            ],
            [
                [
                    name,
                    protocol.safety_variants_per_class,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "PLANNED_NOT_OBSERVED",
                ]
                for name in protocol.safety_attack_classes
            ],
        ),
        "usefulness_safety_attacks": _table(
            [
                "attack_task_id",
                "attack_task_sha256",
                "attack_class",
                "variant_index",
                "status",
            ],
            [
                [
                    row["attack_task_id"],
                    row["attack_task_sha256"],
                    row["attack_class"],
                    row["variant_index"],
                    "PLANNED_NOT_OBSERVED",
                ]
                for row in safety_rows
            ],
        ),
        "usefulness_model_replicates": _table(
            [
                "invocation_id",
                "task_id",
                "arm_id",
                "model_id",
                "repeat_index",
                "trace_id",
                "status",
            ],
            [],
        ),
        "usefulness_agent_retrievals": _table(
            [
                "invocation_id",
                "turn_index",
                "tool_call_index",
                "requested_aliases",
                "relevant_aliases",
                "irrelevant_aliases",
                "duplicate_aliases",
                "context_bytes",
                "provider_input_tokens",
            ],
            [],
        ),
        "usefulness_turn_context": _table(
            [
                "invocation_id",
                "turn_index",
                "authorized_context_sha256",
                "context_bytes",
                "input_tokens",
                "output_tokens",
                "latency_ms",
                "cost_usd",
            ],
            [],
        ),
        "usefulness_practitioner_outcomes": _table(
            [
                "session_id",
                "stratum",
                "period",
                "arm_id",
                "case_id",
                "critical_misses",
                "correct_disposition",
                "evidence_chain_complete",
                "false_alarms",
                "verification_seconds",
            ],
            [],
        ),
        "usefulness_paired_effects": _table(
            [
                "effect_id",
                "outcome_id",
                "reference_arm_id",
                "comparison_arm_id",
                "estimate",
                "ci_low",
                "ci_high",
                "conclusion",
            ],
            [],
        ),
        "usefulness_failures": _table(
            [
                "campaign_kind",
                "case_id",
                "failure_stage",
                "failure_kind",
                "released",
                "evidence_sha256",
            ],
            [],
        ),
        "usefulness_handoff_outcomes": _table(
            [
                "dyad_id",
                "case_id",
                "verification_seconds",
                "provenance_complete",
                "wrong_source_accepted",
                "unresolved_questions",
                "disposition_reproduced",
            ],
            [],
        ),
        "usefulness_shadow_projects": _table(
            [
                "shadow_record_id",
                "person_hours",
                "review_cycles",
                "discrepancies",
                "surfaced_assumptions",
                "disposition_agreement",
            ],
            [],
        ),
        "usefulness_claim_ledger": _table(
            ["claim_id", "level", "status", "bounded_claim", "reason", "ledger_sha256"],
            [
                [
                    claim.claim_id,
                    claim.level.value,
                    claim.status.value,
                    claim.bounded_claim,
                    claim.reason,
                    ledger.ledger_sha256,
                ]
                for claim in ledger.claims
            ],
        ),
    }


def build_usefulness_publication_plan(
    repository_root: Path,
    *,
    source_repository: str,
    source_commit: str,
    source_tree_clean: bool,
    model_ids: Sequence[str],
    cost_basis: UsefulnessCostBasis,
    preregistered_at: datetime,
    frozen_at: datetime,
    generated_at: datetime,
    historical_campaign: (
        HistoricalCampaignEvidence
        | ReviewBriefCampaignArtifact
        | Mapping[str, Any]
        | None
    ) = None,
) -> UsefulnessPublicationPlan:
    if len(model_ids) != 3 or len(set(model_ids)) != 3:
        raise ValueError("usefulness publication requires three distinct model IDs")
    catalog = build_usefulness_catalog()
    landscape_manifest = usefulness_public_manifest(catalog)
    frozen_inputs = derive_frozen_input_sha256s(
        repository_root,
        source_commit=source_commit,
        model_ids=model_ids,
        cost_basis=cost_basis,
        catalog=catalog,
    )
    protocol = build_default_synthetic_protocol(
        source_commit=source_commit,
        source_repository=source_repository,
        model_ids=model_ids,
        frozen_input_sha256s=frozen_inputs,
        preregistered_at=preregistered_at,
        frozen_at=frozen_at,
    )
    program_manifest = deterministic_program_manifest(protocol)
    safety_catalog = safety_catalog_payload(build_safety_attack_catalog(protocol))
    historical = summarize_historical_campaign(historical_campaign)
    historical_evidence = (
        historical_campaign
        if isinstance(historical_campaign, HistoricalCampaignEvidence)
        else None
    )
    # The prior 42-record campaign is historical integration evidence, not a
    # sealed receipt for any new usefulness claim in this preregistration.
    claim_evidence = ClaimGateEvidence()
    ledger = build_experiment_claim_ledger(
        protocol,
        claim_evidence,
        ledger_id="ecocorridor-usefulness-claim-ledger-v1",
        generated_at=generated_at,
    )
    tables = _build_tables(protocol, landscape_manifest, program_manifest, ledger)
    payloads = {
        "protocol.json": protocol.model_dump(mode="json"),
        "landscapes.json": dict(landscape_manifest),
        "program-status.json": dict(program_manifest),
        "historical-review-brief.json": historical.model_dump(mode="json"),
        "claim-ledger.json": ledger.model_dump(mode="json"),
        "cost-basis.json": cost_basis.model_dump(mode="json"),
        "safety-attack-recipes.json": safety_catalog,
        "historical-github-actions-provenance.json": (
            historical_evidence.github_actions_provenance
            if historical_evidence is not None
            and historical_evidence.github_actions_provenance is not None
            else {
                "schema_version": "ecocorridor-historical-receipt-absence/v1.0",
                "receipt_kind": "GITHUB_ACTIONS_PROVENANCE",
                "evidence_status": historical.evidence_status,
            }
        ),
        "historical-wandb-readback-receipt.json": (
            historical_evidence.wandb_readback_receipt
            if historical_evidence is not None
            and historical_evidence.wandb_readback_receipt is not None
            else {
                "schema_version": "ecocorridor-historical-receipt-absence/v1.0",
                "receipt_kind": "WANDB_READBACK",
                "evidence_status": historical.evidence_status,
            }
        ),
        "historical-provider-trace-receipts.json": (
            {
                "schema_version": "ecocorridor-historical-trace-receipts/v1.0",
                "campaign_sha256": historical_evidence.campaign.campaign_sha256,
                "provider_conversation_count": (
                    historical_evidence.campaign.provider_conversation_count
                ),
                "receipts": list(_trace_receipts(historical_evidence.campaign)),
                "receipts_sha256": (
                    historical_evidence.provider_trace_receipts_sha256
                ),
                "evidence_status": historical_evidence.evidence_status,
            }
            if historical_evidence is not None
            else {
                "schema_version": "ecocorridor-historical-receipt-absence/v1.0",
                "receipt_kind": "PROVIDER_TRACE_READBACK",
                "evidence_status": historical.evidence_status,
            }
        ),
    }
    artifact_manifest_sha256 = canonical_sha256(
        [
            {
                "logical_name": name,
                "bytes": len(_artifact_bytes(payload)),
                "sha256": hashlib.sha256(_artifact_bytes(payload)).hexdigest(),
            }
            for name, payload in sorted(payloads.items())
        ]
    )
    provisional = {
        "schema_version": USEFULNESS_REPORT_SCHEMA_VERSION,
        "section_id": USEFULNESS_SECTION_ID,
        "source_repository": source_repository,
        "source_commit": source_commit,
        "source_tree_clean": source_tree_clean,
        "generated_at": generated_at,
        "protocol": protocol,
        "cost_basis": cost_basis,
        "landscape_manifest": landscape_manifest,
        "program_manifest": program_manifest,
        "historical_review_brief": historical,
        "claim_ledger": ledger,
        "tables": tables,
        "artifact_payloads": payloads,
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "report_copy": {
            "boundary": "This section publishes a frozen usefulness-study protocol and current evidence status. Planned cells are not observed results.",
            "historical": (
                "The previous campaign is retained as a negative efficiency result and "
                "trace-integrity example, not proof of adaptive retrieval or practitioner utility."
                if historical.evidence_status == "VERIFIED_HISTORICAL"
                else "No trace-integrity claim is made unless exact producer provenance and "
                "W&B run/artifact readback receipts jointly verify."
            ),
            "external": "Practitioner, reviewer, and partner evidence remains blocked until independently authorized records exist.",
            "forbidden": "Nothing here establishes animal movement, crossing use, collision reduction, population response, biodiversity benefit, approval, or design readiness.",
        },
        "wandb_projection": {},
        "media_status": "HISTORICAL_MEDIA_ONLY",
    }
    identity = canonical_sha256(
        {
            "protocol_sha256": protocol.protocol_sha256,
            "artifact_manifest_sha256": artifact_manifest_sha256,
            "historical_campaign_sha256": historical.campaign_sha256,
        }
    )
    provisional["wandb_projection"] = {
        "run_id": f"usefulness-program-{identity[:16]}",
        "run_name": "EcoCorridor usefulness evidence program",
        "job_type": USEFULNESS_RUN_KIND,
        "artifact_name": f"ecocorridor-usefulness-program-{identity[:12]}",
        "artifact_type": USEFULNESS_ARTIFACT_TYPE,
    }
    serialized = UsefulnessPublicationPlan.model_construct(
        **provisional,
        plan_sha256="0" * 64,
    ).model_dump(mode="json", exclude={"plan_sha256"})
    return UsefulnessPublicationPlan(
        **serialized,
        plan_sha256=canonical_sha256(serialized),
    )


def publication_plan_json(plan: UsefulnessPublicationPlan) -> str:
    return plan.model_dump_json(indent=2, exclude_none=False) + "\n"


__all__ = [
    "HistoricalCampaignEvidence",
    "HistoricalReviewBriefSummary",
    "UsefulnessCostBasis",
    "UsefulnessPublicationPlan",
    "build_usefulness_publication_plan",
    "derive_frozen_input_sha256s",
    "publication_plan_json",
    "summarize_historical_campaign",
]
