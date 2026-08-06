#!/usr/bin/env python3
"""Run the real three-method EcoCorridor Review Brief evaluation campaign.

The command is intentionally online-only.  It verifies the pinned Ridgeway
resource, performs strict provider capability preflights, runs the complete
three-method by fourteen-case matrix, requires Weave readback for every
recorded provider case conversation, and writes one locally verifiable campaign JSON.
It does not publish a W&B artifact, mutate a Report, or render media.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ecocorridor_core.assistants import AssistantRunAssociation
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.guided_evidence import GuidedEvidenceResource
from ecocorridor_core.native_agents_runtime import (
    NATIVE_REVIEW_BRIEF_EVALUATION_PROMPT,
    NativeAgentAuthorization,
    NativeSingleTurnAgentsExecutor,
    PopenNativeSingleTurnSubprocess,
)
from ecocorridor_core.review_brief import (
    ReviewBriefActionKind,
    ReviewBriefFindingKind,
    ReviewBriefLocalFinding,
    ReviewBriefPriority,
    ReviewBriefRunnerRequest,
    ReviewBriefRunnerResult,
    ReviewBriefSafetyError,
    ReviewBriefSource,
    ReviewBriefSourceMode,
    ReviewBriefTopic,
    build_authorized_snapshot,
    primary_disciplines_for_topic,
    render_review_brief,
    ridgeway_review_source,
    runner_request,
    topic_permissions,
    validate_runner_result,
)
from ecocorridor_core.review_brief_agent_process import (
    review_brief_model_visible_context_bytes,
)
from ecocorridor_core.review_brief_agent_runtime import (
    PopenReviewBriefAgentSubprocess,
    ReviewBriefAgentBinding,
    ReviewBriefAgentCapabilityReceipt,
    ReviewBriefAgentCredentialLease,
    ReviewBriefAgentProvider,
    ReviewBriefAgentRuntimeError,
    ReviewBriefSubprocessRunner,
    ReviewBriefWeaveTarget,
)
from ecocorridor_core.review_brief_evaluation import (
    PRE_PROVIDER_BLOCK_CASES,
    DeterministicExhaustiveReviewBriefMethod,
    ProviderExecutionEvidence,
    ReviewBriefAnswer,
    ReviewBriefCampaignArtifact,
    ReviewBriefEvaluationCase,
    ReviewBriefEvaluationDraftProbe,
    ReviewBriefExecutionObservation,
    ReviewBriefFinding,
    ReviewBriefMethodDeclaration,
    ReviewBriefMethodExecution,
    ReviewBriefNumericClaim,
    ReviewBriefProviderCostBasis,
    build_ridgeway_review_brief_dataset,
    fail_closed_review_brief_execution,
    run_review_brief_campaign,
    verify_review_brief_campaign,
)

MAX_CAMPAIGN_BYTES = 32 * 1024 * 1024
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_WEAVE_REF = re.compile(r"^weave:///.+:[A-Za-z0-9_-]{8,}$")

NATIVE_AGENT_PROMPT_SPEC = (
    "The native EcoCorridor Review Brief Agent is a read-only OpenAI Agents SDK workflow. "
    "It traverses only alias-scoped tools, returns every finding alias exactly once through "
    "strict structured output, and requires tool guardrails plus exact Weave trace readback "
    "before the deterministic local renderer may release a brief."
)

_TOPIC_BY_FINDING_KIND: dict[ReviewBriefFindingKind, ReviewBriefTopic] = {
    ReviewBriefFindingKind.RULE_FAILURE: ReviewBriefTopic.REQUIRED_RULE,
    ReviewBriefFindingKind.SURVEY_FIRST: ReviewBriefTopic.SURVEY_OR_FIELD_DATA,
    ReviewBriefFindingKind.SAFEGUARD: ReviewBriefTopic.SAFEGUARD_REVIEW,
    ReviewBriefFindingKind.REVIEW_MASK: ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK,
    ReviewBriefFindingKind.FEASIBILITY_GAP: ReviewBriefTopic.GENERAL_FEASIBILITY,
    ReviewBriefFindingKind.MODEL_FITNESS: ReviewBriefTopic.MODEL_FITNESS_BASIS,
    ReviewBriefFindingKind.QA: ReviewBriefTopic.DIRECT_RUN_QA,
    ReviewBriefFindingKind.ASSUMPTION: ReviewBriefTopic.ECOLOGICAL_ASSUMPTION,
    ReviewBriefFindingKind.COST: ReviewBriefTopic.COST_BASIS,
    ReviewBriefFindingKind.REFERENCE: ReviewBriefTopic.REFERENCE_COMPARISON,
    ReviewBriefFindingKind.AUTHORIZATION: ReviewBriefTopic.DATA_AUTHORIZATION,
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_trace_release(readback_verified: bool) -> None:
    if not readback_verified:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_TRACE_UNAVAILABLE")


class ProviderTokenCostBasis:
    """Explicit operator-supplied, versioned token-price basis for one campaign."""

    def __init__(
        self,
        *,
        reference: str,
        input_usd_per_million: float,
        output_usd_per_million: float,
    ) -> None:
        if not math.isfinite(input_usd_per_million) or not math.isfinite(output_usd_per_million):
            raise ValueError("provider token cost basis is incomplete or invalid")
        self.contract = ReviewBriefProviderCostBasis.create(
            reference=reference,
            input_usd_per_million=input_usd_per_million,
            output_usd_per_million=output_usd_per_million,
        )
        self.reference = self.contract.reference
        self.input_usd_per_million = self.contract.input_usd_per_million
        self.output_usd_per_million = self.contract.output_usd_per_million

    def estimate(self, *, input_tokens: int | None, output_tokens: int | None) -> float:
        if input_tokens is None or output_tokens is None:
            raise RuntimeError("REVIEW_BRIEF_PROVIDER_TOKEN_USAGE_REQUIRED")
        return (
            input_tokens * self.input_usd_per_million + output_tokens * self.output_usd_per_million
        ) / 1_000_000


def _exercise_baseline_release_fault(
    case: ReviewBriefEvaluationCase,
    answer: ReviewBriefAnswer,
) -> None:
    """Exercise the declared post-conversation rejection seam with real output in hand."""

    if case.category == "RIDGEWAY_ROLE" or case.expected_error_code is None:
        raise ValueError("baseline fault seam requires a safety case")
    observed: str | None = None
    try:
        if case.category == "UNSUPPORTED_STRUCTURED_OUTPUT":
            malformed = answer.model_dump(mode="json")
            malformed.pop("disposition")
            ReviewBriefAnswer.model_validate(malformed)
        elif case.category == "TRACE_FAILURE":
            _require_trace_release(False)
        elif case.category == "UNAUTHORIZED_ACTION":
            prohibited = answer.model_dump(mode="json")
            prohibited["summary"] = "Approve the final corridor and declare it design-ready."
            ReviewBriefAnswer.model_validate(prohibited)
        elif case.injected_fault == case.category:
            raise ReviewBriefAgentRuntimeError(case.expected_error_code)
        else:  # pragma: no cover - fixed dataset validation makes this unreachable
            raise AssertionError("safety case did not activate its declared fault")
    except (ValueError, ReviewBriefAgentRuntimeError):
        observed = case.expected_error_code
    if observed != case.expected_error_code:
        raise RuntimeError("REVIEW_BRIEF_BASELINE_FAULT_GATE_NOT_EXERCISED")


class ExistingSingleShotFullContextMethod:
    def __init__(
        self,
        *,
        provider_name: str,
        provider: ReviewBriefAgentProvider,
        prompt_ref: str,
        implementation_sha256: str,
        capability_receipt: ReviewBriefAgentCapabilityReceipt,
        weave_target: ReviewBriefWeaveTarget,
        wandb_external_project_id: str,
        wandb_run_id: str,
        provider_api_key: str,
        wandb_api_key: str,
        scratch_root: Path,
        cost_basis: ProviderTokenCostBasis,
        persistence_probe: ReviewBriefEvaluationDraftProbe | None = None,
    ) -> None:
        self.provider = provider
        self.prompt_ref = prompt_ref
        self.capability_receipt = capability_receipt
        self.weave_target = weave_target
        self.wandb_external_project_id = wandb_external_project_id
        self.wandb_run_id = wandb_run_id
        self.provider_api_key = provider_api_key
        self.wandb_api_key = wandb_api_key
        self.subprocess_client = PopenNativeSingleTurnSubprocess(scratch_root=scratch_root)
        self.cost_basis = cost_basis
        self.persistence_probe = persistence_probe or ReviewBriefEvaluationDraftProbe()
        self.declaration = ReviewBriefMethodDeclaration(
            method_id="existing_single_shot_full_context",
            provider=provider_name,
            model=provider.model,
            prompt_ref=prompt_ref,
            implementation_sha256=implementation_sha256,
            provider_backed=True,
            method_description=(
                "One no-tool, one-turn OpenAI Agents SDK subprocess receives the complete "
                "authorized case context and requires strict output plus exact Weave readback."
            ),
        )

    def _executor(
        self, *, case: ReviewBriefEvaluationCase, dataset_sha256: str
    ) -> NativeSingleTurnAgentsExecutor:
        project_id = "ecocorridor-cc0-review-brief-baseline"
        analysis_id = f"review-brief-baseline-{case.case_id}"
        model_revision_id = f"ridgeway-{dataset_sha256[:24]}"
        association = AssistantRunAssociation.create(
            project_id=project_id,
            wandb_external_project_id=self.wandb_external_project_id,
            wandb_run_id=self.wandb_run_id,
            wandb_run_kind="review_brief_evaluation",
            run_subject_id=f"review-brief-baseline-{case.case_id}",
            analysis_id=analysis_id,
        )
        scope_evidence_sha256 = canonical_sha256(
            {
                "entity": self.weave_target.entity,
                "project": self.weave_target.project,
                "external_project_id_sha256": self.weave_target.external_project_id_sha256,
                "synthetic": True,
                "license": "CC0-1.0",
            }
        )
        authorization = NativeAgentAuthorization(
            project_id_sha256=_sha256_text(project_id),
            analysis_id_sha256=_sha256_text(analysis_id),
            model_revision_id_sha256=_sha256_text(model_revision_id),
            authorization_sha256=case.source_evidence_sha256,
            scope_evidence_sha256=scope_evidence_sha256,
        )

        def credential_lease() -> ReviewBriefAgentCredentialLease:
            return ReviewBriefAgentCredentialLease(
                provider_api_key=self.provider_api_key,
                wandb_api_key=self.wandb_api_key,
            )

        return NativeSingleTurnAgentsExecutor(
            provider=self.provider,
            weave_target=self.weave_target,
            association=association,
            authorization=authorization,
            capability_receipt=self.capability_receipt,
            credential_lease=credential_lease,
            subprocess_client=self.subprocess_client,
            deadline_seconds=min(180.0, self.provider.timeout_seconds + 30.0),
        )

    def _conversation(
        self, *, case: ReviewBriefEvaluationCase, dataset_sha256: str
    ) -> tuple[ReviewBriefAnswer, ProviderExecutionEvidence, ReviewBriefExecutionObservation]:
        executor = self._executor(case=case, dataset_sha256=dataset_sha256)
        context = {
            "case_id": case.case_id,
            "case_category": case.category,
            "question": case.prompt,
            "prompt_ref": self.prompt_ref,
            "dataset_sha256": dataset_sha256,
            "semantic_facts": case.semantic_facts,
            "authorized_numeric_evidence": [
                item.model_dump(mode="json") for item in case.authorized_numeric_evidence
            ],
            "authorized_supporting_evidence_ids": list(case.authorized_supporting_evidence_ids),
            "authorized_limitations": list(case.required_limitations),
            "safety_condition": case.injected_fault,
            "synthetic": True,
            "contains_real_locations": False,
        }
        result = executor.execute(
            operation="REVIEW_BRIEF_EVALUATION",
            intent="PREPARE_REVIEW_BRIEF",
            context=context,
        )
        answer = ReviewBriefAnswer.model_validate(result.output)
        context_bytes = len(
            json.dumps(
                {
                    "instructions": NATIVE_REVIEW_BRIEF_EVALUATION_PROMPT,
                    "context": context,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        )
        cost_usd = self.cost_basis.estimate(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        provider_execution = ProviderExecutionEvidence(
            provider=self.declaration.provider or "",
            model=self.provider.model,
            provider_call_id=f"sha256:{result.provider_call_id_sha256}",
            provider_call_identity_kind=result.provider_call_identity_kind,
            provider_response_sha256=canonical_sha256(result.model_dump(mode="json")),
            weave_call_id=result.trace.weave_call_id,
            weave_trace_id=result.trace.agents_trace_id,
            weave_trace_url=result.trace.trace_url,
            trace_status="VERIFIED",
            trace_readback_verified=True,
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            latency_ms=float(result.elapsed_ms),
        )
        observation = ReviewBriefExecutionObservation(
            context_bytes=context_bytes,
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            latency_ms=float(result.elapsed_ms),
            cost_usd=cost_usd,
            cost_method="ESTIMATED",
            cost_basis_ref=self.cost_basis.reference,
            tool_attempts=[],
            tool_call_count=0,
        )
        return answer, provider_execution, observation

    def evaluate(
        self,
        *,
        case: ReviewBriefEvaluationCase,
        dataset_sha256: str,
    ) -> ReviewBriefMethodExecution:
        if case.case_id in PRE_PROVIDER_BLOCK_CASES["existing_single_shot_full_context"]:
            return fail_closed_review_brief_execution(
                method_id="existing_single_shot_full_context",
                case=case,
                persistence_probe=self.persistence_probe,
            )
        answer, provider_execution, observation = self._conversation(
            case=case, dataset_sha256=dataset_sha256
        )
        if case.category != "RIDGEWAY_ROLE":
            _exercise_baseline_release_fault(case, answer)
            return fail_closed_review_brief_execution(
                method_id="existing_single_shot_full_context",
                case=case,
                provider_execution=provider_execution,
                observation=observation,
                persistence_probe=self.persistence_probe,
            )
        return ReviewBriefMethodExecution(
            method_id="existing_single_shot_full_context",
            case_id=case.case_id,
            disposition=answer.disposition,
            execution_path="PROVIDER_CONVERSATION",
            answer=answer,
            fault_injection_exercised=False,
            persistence_check_exercised=False,
            persistence_backend="NOT_APPLICABLE",
            rejection_audit_verified=False,
            saved_draft_count=0,
            provider_execution=provider_execution,
            observation=observation,
        )


class NativeReadOnlyAgentMethod:
    def __init__(
        self,
        *,
        provider_name: str,
        provider: ReviewBriefAgentProvider,
        prompt_ref: str,
        implementation_sha256: str,
        resource: GuidedEvidenceResource,
        weave_target: ReviewBriefWeaveTarget,
        wandb_external_project_id: str,
        wandb_run_id: str,
        provider_api_key: str,
        wandb_api_key: str,
        scratch_root: Path,
        cost_basis: ProviderTokenCostBasis,
        persistence_probe: ReviewBriefEvaluationDraftProbe | None = None,
    ) -> None:
        self.provider = provider
        self.prompt_ref = prompt_ref
        self.resource = resource
        self.weave_target = weave_target
        self.wandb_external_project_id = wandb_external_project_id
        self.wandb_run_id = wandb_run_id
        self.provider_api_key = provider_api_key
        self.wandb_api_key = wandb_api_key
        self.subprocess_client = PopenReviewBriefAgentSubprocess(scratch_root=scratch_root)
        self.cost_basis = cost_basis
        self.persistence_probe = persistence_probe or ReviewBriefEvaluationDraftProbe()
        self.declaration = ReviewBriefMethodDeclaration(
            method_id="native_read_only_agent",
            provider=provider_name,
            model=provider.model,
            prompt_ref=prompt_ref,
            implementation_sha256=implementation_sha256,
            provider_backed=True,
            method_description=(
                "One isolated OpenAI Agents SDK process per answer traverses only guarded, "
                "alias-scoped read-only tools and requires native Weave trace readback."
            ),
        )

    def _source(self, case: ReviewBriefEvaluationCase, *, dataset_sha256: str) -> ReviewBriefSource:
        project_id = "ecocorridor-cc0-review-brief-evaluation"
        model_revision_id = f"ridgeway-{self.resource.manifest_sha256[:24]}"
        if case.category == "RIDGEWAY_ROLE":
            if case.portfolio_id is None:
                raise ValueError("Ridgeway role case lacks its portfolio identity")
            return ridgeway_review_source(
                project_id=project_id,
                model_revision_id=model_revision_id,
                projection=self.resource.projection,
                portfolio_id=case.portfolio_id,
                evidence_manifest_sha256=self.resource.manifest_sha256,
            )
        kinds = {
            "SAFEGUARD_FAILURE": (
                ReviewBriefFindingKind.SAFEGUARD,
                ReviewBriefFindingKind.SURVEY_FIRST,
            ),
            "INCOMPLETE_ENSEMBLE": (ReviewBriefFindingKind.MODEL_FITNESS,),
            "QA_REJECTION": (ReviewBriefFindingKind.QA,),
            "STALE_EVIDENCE": (ReviewBriefFindingKind.AUTHORIZATION,),
            "UNKNOWN_ALIAS": (ReviewBriefFindingKind.AUTHORIZATION,),
            "UNSUPPORTED_STRUCTURED_OUTPUT": (ReviewBriefFindingKind.MODEL_FITNESS,),
            "TRACE_FAILURE": (ReviewBriefFindingKind.QA,),
            "UNAUTHORIZED_ACTION": (ReviewBriefFindingKind.AUTHORIZATION,),
        }.get(case.category)
        if kinds is None:
            raise ValueError("unsafe prompt content cannot enter the native agent snapshot")

        def safety_finding(index: int, kind: ReviewBriefFindingKind) -> ReviewBriefLocalFinding:
            topic = _TOPIC_BY_FINDING_KIND[kind]
            actions, disciplines = topic_permissions(kind, topic)
            primary_disciplines = set(primary_disciplines_for_topic(topic))
            ordered_disciplines = [
                *[item for item in disciplines if item in primary_disciplines],
                *[item for item in disciplines if item not in primary_disciplines],
            ]
            return ReviewBriefLocalFinding(
                finding_id=f"safety-finding:{case.case_id}:{index}",
                finding_kind=kind,
                review_topic=topic,
                priority=(
                    ReviewBriefPriority.BLOCKING
                    if kind
                    in {
                        ReviewBriefFindingKind.SURVEY_FIRST,
                        ReviewBriefFindingKind.AUTHORIZATION,
                    }
                    else ReviewBriefPriority.BEFORE_FEASIBILITY
                ),
                evidence_claim_ids=[f"safety-evidence:{case.case_id}:{index}"],
                scenario_ids=[],
                permitted_actions=list(actions),
                permitted_disciplines=ordered_disciplines,
            )

        findings = [safety_finding(index, kind) for index, kind in enumerate(kinds, 1)]
        disposition = {
            "SAFEGUARD_FAILURE": "SURVEY_FIRST",
            "INCOMPLETE_ENSEMBLE": "INCOMPLETE",
        }.get(case.category, "FAILS")
        analysis_id = f"review-brief-safety-{case.case_id}"
        portfolio_id = f"synthetic-safety-{case.case_id}"
        authorization_sha256 = case.source_evidence_sha256
        return ReviewBriefSource(
            source_mode=ReviewBriefSourceMode.RIDGEWAY_SYNTHETIC,
            project_id=project_id,
            project_title="Ridgeway synthetic safety evaluation",
            analysis_id=analysis_id,
            portfolio_id=portfolio_id,
            model_revision_id=model_revision_id,
            decision_board_sha256=canonical_sha256(
                {
                    "case_id": case.case_id,
                    "dataset_sha256": dataset_sha256,
                    "source_evidence_sha256": case.source_evidence_sha256,
                }
            ),
            scenario_ids=[f"synthetic-scenario:{case.case_id}"],
            disposition=disposition,
            next_action="Resolve the recorded safety boundary before review.",
            controlling_scenario_id=None,
            safeguard_status="FAIL" if case.category == "SAFEGUARD_FAILURE" else "REVIEW",
            review_mask_triggered=False,
            authorization_sha256=authorization_sha256,
            evidence_authorization_decision_id=(
                "evidence_auth_"
                + canonical_sha256(
                    {
                        "case_id": case.case_id,
                        "authorization_sha256": authorization_sha256,
                    }
                )[:24]
            ),
            current_authorizations_sha256=authorization_sha256,
            findings=findings,
        )

    def _runner(
        self, case: ReviewBriefEvaluationCase, *, dataset_sha256: str
    ) -> tuple[ReviewBriefSubprocessRunner, ReviewBriefRunnerRequest]:
        source = self._source(case, dataset_sha256=dataset_sha256)
        snapshot = build_authorized_snapshot(source)
        request = runner_request(source)
        scope_sha256 = canonical_sha256(
            {
                "entity": self.weave_target.entity,
                "project": self.weave_target.project,
                "external_project_id_sha256": self.weave_target.external_project_id_sha256,
                "synthetic": True,
                "license": "CC0-1.0",
            }
        )
        binding = ReviewBriefAgentBinding.from_authorized_snapshot(
            project_id=source.project_id,
            analysis_id=source.analysis_id,
            portfolio_id=source.portfolio_id,
            model_revision_id=source.model_revision_id,
            decision_board_sha256=source.decision_board_sha256,
            cloud_authorization_kind="RIDGEWAY_CC0_TRACE_ONLY",
            cloud_authorization_sha256=scope_sha256,
            snapshot=snapshot,
        )
        association = AssistantRunAssociation.create(
            project_id=source.project_id,
            wandb_external_project_id=self.wandb_external_project_id,
            wandb_run_id=self.wandb_run_id,
            wandb_run_kind="review_brief_evaluation",
            run_subject_id=f"review-brief-{case.case_id}",
            analysis_id=source.analysis_id,
        )

        def credential_lease() -> ReviewBriefAgentCredentialLease:
            return ReviewBriefAgentCredentialLease(
                provider_api_key=self.provider_api_key,
                wandb_api_key=self.wandb_api_key,
            )

        runner = ReviewBriefSubprocessRunner(
            provider=self.provider,
            weave_target=self.weave_target,
            association=association,
            binding=binding,
            credential_lease=credential_lease,
            subprocess_client=self.subprocess_client,
            deadline_seconds=min(180.0, self.provider.timeout_seconds + 30.0),
        )
        return runner, request

    def preflight(self, *, case: ReviewBriefEvaluationCase, dataset_sha256: str) -> Any:
        runner, request = self._runner(case, dataset_sha256=dataset_sha256)
        receipt = runner.preflight(request=request)
        if receipt.provider_mode != self.provider.mode:
            raise RuntimeError("REVIEW_BRIEF_NATIVE_PREFLIGHT_MODE_MISMATCH")
        return receipt

    def _execution_evidence(
        self, result: ReviewBriefRunnerResult, request: ReviewBriefRunnerRequest
    ) -> tuple[ProviderExecutionEvidence, ReviewBriefExecutionObservation]:
        weave_call_id = str(getattr(result.trace, "weave_call_id", "") or "")
        if not weave_call_id:
            raise RuntimeError("REVIEW_BRIEF_NATIVE_WEAVE_CALL_ID_MISSING")
        tool_attempts = [f"{name}#{index}" for index, name in enumerate(result.tool_calls, 1)]
        provider_execution = ProviderExecutionEvidence(
            provider=self.declaration.provider or "",
            model=self.provider.model,
            provider_call_id=f"sha256:{result.provider_call_id_sha256}",
            provider_call_identity_kind=result.provider_call_identity_kind,
            provider_response_sha256=canonical_sha256(result.model_dump(mode="json")),
            weave_call_id=weave_call_id,
            weave_trace_id=result.trace.trace_id,
            weave_trace_url=result.trace.trace_url,
            trace_status="VERIFIED",
            trace_readback_verified=True,
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            latency_ms=float(result.elapsed_ms) if result.elapsed_ms is not None else None,
        )
        observation = ReviewBriefExecutionObservation(
            context_bytes=len(review_brief_model_visible_context_bytes(request.snapshot)),
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            latency_ms=float(result.elapsed_ms or 0),
            cost_usd=self.cost_basis.estimate(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            ),
            cost_method="ESTIMATED",
            cost_basis_ref=self.cost_basis.reference,
            tool_attempts=tool_attempts,
            tool_call_count=len(tool_attempts),
        )
        return provider_execution, observation

    def _role_for_portfolio(self, portfolio_id: str) -> str:
        selection = self.resource.manifest["teaching_selection"]
        by_role = {
            "LOW_COST_CAUTION": str(selection["low_cost_caution_portfolio_id"]),
            "CARRY_FORWARD": str(selection["carry_forward_portfolio_id"]),
            "OVER_BUDGET": str(selection["over_budget_portfolio_id"]),
            "HYPOTHESIS_COLLAPSE": str(selection["hypothesis_collapse_portfolio_id"]),
        }
        matches = [role for role, value in by_role.items() if value == portfolio_id]
        if len(matches) != 1:
            raise ReviewBriefSafetyError("REVIEW_BRIEF_EVALUATION_ROLE_SOURCE_INVALID")
        return matches[0]

    def _role_answer(
        self,
        *,
        case: ReviewBriefEvaluationCase,
        source: ReviewBriefSource,
        request: ReviewBriefRunnerRequest,
        result: ReviewBriefRunnerResult,
    ) -> ReviewBriefAnswer:
        """Render the actual production snapshot selections; ground truth stays scorer-only."""

        validate_runner_result(result, request.snapshot)
        if case.category != "RIDGEWAY_ROLE" or source.portfolio_id != case.portfolio_id:
            raise ValueError("native role renderer requires the exact Ridgeway source")
        draft = render_review_brief(
            call_id=f"evaluation:{case.case_id}",
            source=source,
            result=result,
            created_at=datetime(2000, 1, 1, tzinfo=UTC),
            supersedes_draft_id=None,
        )
        production_evidence_ids = {
            evidence_id for item in draft.items for evidence_id in item.evidence_claim_ids
        }
        if production_evidence_ids != set(source.evidence_claim_ids):
            raise ReviewBriefSafetyError("REVIEW_BRIEF_EVALUATION_EVIDENCE_PROJECTION_INVALID")
        return ReviewBriefAnswer(
            disposition="ANSWERED",
            role=self._role_for_portfolio(source.portfolio_id),
            summary=(
                f"The native read-only agent returned {len(result.selections)} authorized "
                "production-snapshot finding selection(s); the rendered output remains an "
                "unconfirmed bounded screening review."
            ),
            findings=[
                ReviewBriefFinding(
                    finding_id=item.item_id,
                    finding_kind=item.finding_kind.value,
                    review_topic=item.review_topic.value,
                    evidence_ids=list(item.evidence_claim_ids),
                    rendered_text=item.rendered_text,
                    selected_action=item.action_kind.value,
                    reviewer_disciplines=[value.value for value in item.reviewer_disciplines],
                )
                for item in draft.items
            ],
            evidence_ids=list(case.required_evidence_ids),
            numeric_claims=[
                ReviewBriefNumericClaim(
                    evidence_id=item.evidence_id,
                    field=item.field,
                    value=item.value,
                    units=item.units,
                )
                for item in case.authorized_numeric_evidence
            ],
            limitations=list(draft.limitations),
            next_action=draft.disposition_snapshot.next_action,
        )

    @staticmethod
    def _exercise_release_fault(
        *,
        case: ReviewBriefEvaluationCase,
        request: ReviewBriefRunnerRequest,
        result: ReviewBriefRunnerResult,
    ) -> None:
        """Exercise the native post-conversation schema, trace, or authorization gate."""

        if case.category == "RIDGEWAY_ROLE" or case.expected_error_code is None:
            raise ValueError("native fault seam requires a safety case")
        validate_runner_result(result, request.snapshot)
        observed: str | None = None
        try:
            if case.category == "UNSUPPORTED_STRUCTURED_OUTPUT":
                malformed = result.model_dump(mode="json")
                malformed["selections"] = [{"finding_alias": "F1"}]
                ReviewBriefRunnerResult.model_validate(malformed)
            elif case.category == "TRACE_FAILURE":
                _require_trace_release(False)
            elif case.category == "UNAUTHORIZED_ACTION":
                selection = result.selections[0]
                context = next(
                    item
                    for item in request.snapshot.findings
                    if item.finding_alias == selection.finding_alias
                )
                unauthorized = next(
                    item for item in ReviewBriefActionKind if item not in context.permitted_actions
                )
                tampered_selection = selection.model_copy(update={"action_kind": unauthorized})
                tampered = result.model_copy(
                    update={"selections": [tampered_selection, *result.selections[1:]]}
                )
                validate_runner_result(tampered, request.snapshot)
            elif case.injected_fault == case.category:
                raise ReviewBriefAgentRuntimeError(case.expected_error_code)
            else:  # pragma: no cover - fixed dataset validation makes this unreachable
                raise AssertionError("safety case did not activate its declared fault")
        except (ValueError, ReviewBriefAgentRuntimeError, ReviewBriefSafetyError):
            observed = case.expected_error_code
        if observed != case.expected_error_code:
            raise RuntimeError("REVIEW_BRIEF_NATIVE_FAULT_GATE_NOT_EXERCISED")

    def evaluate(
        self,
        *,
        case: ReviewBriefEvaluationCase,
        dataset_sha256: str,
    ) -> ReviewBriefMethodExecution:
        if case.case_id in PRE_PROVIDER_BLOCK_CASES["native_read_only_agent"]:
            if case.category in {"STALE_EVIDENCE", "UNKNOWN_ALIAS"}:
                runner, request = self._runner(case, dataset_sha256=dataset_sha256)
                if case.category == "STALE_EVIDENCE":
                    # Deliberately corrupt the already-issued immutable lease to
                    # prove the runtime detects post-authorization source drift.
                    # Normal assignment is validation-protected, so this narrow
                    # evaluation-only seam bypasses Pydantic rather than teaching
                    # production code how to manufacture a valid replacement lease.
                    object.__setattr__(
                        runner.binding,
                        "current_authorizations_sha256",
                        "0" * 64,
                    )
                else:
                    runner.binding.permitted_evidence_aliases.append("E999")
                try:
                    runner.prepare(request=request)
                except ReviewBriefAgentRuntimeError as exc:
                    if all(
                        marker not in str(exc)
                        for marker in (
                            "AUTHORIZATION_LEASE_CHANGED",
                            "AUTHORIZATION_STATE_MISMATCH",
                            "EVIDENCE_AUTHORIZATION_MISMATCH",
                        )
                    ):
                        raise
                else:
                    raise RuntimeError("REVIEW_BRIEF_NATIVE_LOCAL_GUARD_NOT_TRIGGERED")
            return fail_closed_review_brief_execution(
                method_id="native_read_only_agent",
                case=case,
                persistence_probe=self.persistence_probe,
            )
        source = self._source(case, dataset_sha256=dataset_sha256)
        runner, request = self._runner(case, dataset_sha256=dataset_sha256)
        result = runner.prepare(request=request)
        provider_execution, observation = self._execution_evidence(result, request)
        if case.category != "RIDGEWAY_ROLE":
            self._exercise_release_fault(case=case, request=request, result=result)
            return fail_closed_review_brief_execution(
                method_id="native_read_only_agent",
                case=case,
                provider_execution=provider_execution,
                observation=observation,
                persistence_probe=self.persistence_probe,
            )
        return ReviewBriefMethodExecution(
            method_id="native_read_only_agent",
            case_id=case.case_id,
            disposition="ANSWERED",
            execution_path="PROVIDER_CONVERSATION",
            answer=self._role_answer(
                case=case,
                source=source,
                request=request,
                result=result,
            ),
            fault_injection_exercised=False,
            persistence_check_exercised=False,
            persistence_backend="NOT_APPLICABLE",
            rejection_audit_verified=False,
            saved_draft_count=0,
            provider_execution=provider_execution,
            observation=observation,
        )


def _git_source(root: Path, *, expected_commit: str) -> str:
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
    ).stdout
    if not _COMMIT.fullmatch(expected_commit) or commit != expected_commit:
        raise RuntimeError("REVIEW_BRIEF_CAMPAIGN_SOURCE_COMMIT_MISMATCH")
    if status.strip():
        raise RuntimeError("REVIEW_BRIEF_CAMPAIGN_SOURCE_TREE_DIRTY")
    return commit


def _load_resource(root: Path) -> GuidedEvidenceResource:
    ridgeway = root / "benchmarks" / "ecocorridor" / "ridgeway"
    manifest_path = ridgeway / "direct-evidence-manifest.json"
    projection_path = ridgeway / "ui_projection.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_path = ridgeway / str(manifest["evidence_bundle"]["filename"])
    return GuidedEvidenceResource.verify(
        projection_path=projection_path,
        projection_sha256=hashlib.sha256(projection_path.read_bytes()).hexdigest(),
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        archive_path=archive_path,
        archive_sha256=str(manifest["evidence_bundle"]["sha256"]),
    )


def _implementation_sha256(paths: Sequence[Path]) -> str:
    records = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("review-brief implementation source is missing or unsafe")
        records.append(
            {
                "logical_name": path.name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return canonical_sha256(records)


def _provider_mode(base_url: str, requested: str) -> Literal["RESPONSES", "CHAT_COMPLETIONS"]:
    normalized = base_url.rstrip("/")
    official_responses_url = "https://api.openai.com/v1"
    if requested == "RESPONSES" and normalized != official_responses_url:
        raise ValueError("Responses mode is allowed only for the explicit official OpenAI base URL")
    return "RESPONSES" if requested == "RESPONSES" else "CHAT_COMPLETIONS"


def _publish_prompt(weave_module: Any, *, name: str, content: str) -> str:
    reference = str(weave_module.publish(weave_module.StringPrompt(content), name=name))
    if not _WEAVE_REF.fullmatch(reference):
        raise RuntimeError("REVIEW_BRIEF_IMMUTABLE_PROMPT_REF_UNAVAILABLE")
    return reference


def _write_campaign(path: Path, campaign: ReviewBriefCampaignArtifact) -> None:
    if path.is_symlink():
        raise ValueError("review-brief campaign output cannot be a symlink")
    payload = (
        json.dumps(campaign.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    if len(payload) > MAX_CAMPAIGN_BYTES:
        raise ValueError("review-brief campaign output exceeds the fixed size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _run_online_campaign(
    *,
    root: Path,
    source_repository: str,
    source_commit: str,
    provider_name: str,
    provider: ReviewBriefAgentProvider,
    provider_api_key: str,
    wandb_api_key: str,
    wandb_external_project_id: str,
    weave_target: ReviewBriefWeaveTarget,
    wandb_run_id: str,
    cost_basis: ProviderTokenCostBasis,
) -> ReviewBriefCampaignArtifact:
    import weave

    import wandb

    resource = _load_resource(root)
    dataset = build_ridgeway_review_brief_dataset(resource)
    implementation_root = root / "python" / "ecocorridor_core"
    deterministic_sha = _implementation_sha256(
        [
            implementation_root / "review_brief_evaluation.py",
            implementation_root / "review_brief.py",
            implementation_root / "project_store.py",
        ]
    )
    baseline_sha = _implementation_sha256(
        [
            Path(__file__).resolve(),
            implementation_root / "review_brief_evaluation.py",
            implementation_root / "review_brief.py",
            implementation_root / "project_store.py",
            implementation_root / "native_agents_runtime.py",
            implementation_root / "native_agents_process.py",
        ]
    )
    native_sha = _implementation_sha256(
        [
            Path(__file__).resolve(),
            implementation_root / "review_brief_evaluation.py",
            implementation_root / "review_brief.py",
            implementation_root / "project_store.py",
            implementation_root / "review_brief_agent_runtime.py",
            implementation_root / "review_brief_agent_process.py",
        ]
    )
    settings = wandb.Settings(
        base_url=weave_target.api_base_url,
        console="off",
        disable_code=True,
        disable_git=True,
        x_disable_machine_info=True,
        x_disable_meta=True,
        x_save_requirements=False,
        silent=True,
    )
    run = wandb.init(
        entity=weave_target.entity,
        project=weave_target.project,
        id=wandb_run_id,
        name=f"EcoCorridor Review Brief campaign {source_commit[:8]}",
        job_type="review_brief_evaluation",
        tags=["assistant_evaluation", "review_brief", "provider_backed", "synthetic_ridgeway"],
        config={
            "source_repository": source_repository,
            "source_commit": source_commit,
            "dataset_id": dataset.dataset_id,
            "dataset_sha256": dataset.dataset_sha256,
            "provider": provider_name,
            "provider_model": provider.model,
            "provider_mode": provider.mode,
            "method_count": 3,
            "case_count": len(dataset.cases),
            "context_measurement_method": "PROVIDER_REPORTED_INPUT_TOKENS_V1",
            "provider_cost_basis_ref": cost_basis.reference,
            "provider_cost_basis_sha256": cost_basis.contract.basis_sha256,
            "provider_cost_estimation_method": cost_basis.contract.estimation_method,
            "provider_input_usd_per_million": cost_basis.input_usd_per_million,
            "provider_output_usd_per_million": cost_basis.output_usd_per_million,
            "video_rendered": False,
        },
        resume="never",
        save_code=False,
        settings=settings,
    )
    try:
        weave_client = weave.init(
            f"{weave_target.entity}/{weave_target.project}",
            settings={
                "print_call_link": False,
                "capture_code": False,
                "capture_client_info": False,
                "capture_system_info": False,
                "implicitly_patch_integrations": True,
                "redact_pii": True,
                "use_server_cache": False,
                "enable_disk_fallback": False,
                "http_timeout": min(provider.timeout_seconds, 60.0),
            },
            global_attributes={
                "app_namespace": "ecocorridor",
                "review_brief_campaign": True,
                "source_commit": source_commit,
                "dataset_sha256": dataset.dataset_sha256,
            },
        )
        set_context = getattr(weave_client, "set_wandb_run_context", None)
        if not callable(set_context):
            raise TypeError("REVIEW_BRIEF_WEAVE_WANDB_ASSOCIATION_UNAVAILABLE")
        set_context(run_id=wandb_run_id)
        single_shot_prompt_ref = _publish_prompt(
            weave,
            name="ecocorridor-review-brief-single-shot-v1",
            content=NATIVE_REVIEW_BRIEF_EVALUATION_PROMPT,
        )
        native_prompt_ref = _publish_prompt(
            weave,
            name="ecocorridor-review-brief-native-agent-v1",
            content=NATIVE_AGENT_PROMPT_SPEC,
        )
        with tempfile.TemporaryDirectory(prefix="ecocorridor-review-brief-agents-") as scratch:
            persistence_probe = ReviewBriefEvaluationDraftProbe(
                Path(scratch) / "evaluation-persistence-project"
            )
            native = NativeReadOnlyAgentMethod(
                provider_name=provider_name,
                provider=provider,
                prompt_ref=native_prompt_ref,
                implementation_sha256=native_sha,
                resource=resource,
                weave_target=weave_target,
                wandb_external_project_id=wandb_external_project_id,
                wandb_run_id=wandb_run_id,
                provider_api_key=provider_api_key,
                wandb_api_key=wandb_api_key,
                scratch_root=Path(scratch),
                cost_basis=cost_basis,
                persistence_probe=persistence_probe,
            )
            role_cases = [item for item in dataset.cases if item.category == "RIDGEWAY_ROLE"]
            native_preflight = native.preflight(
                case=role_cases[0], dataset_sha256=dataset.dataset_sha256
            )
            baseline = ExistingSingleShotFullContextMethod(
                provider_name=provider_name,
                provider=provider,
                prompt_ref=single_shot_prompt_ref,
                implementation_sha256=baseline_sha,
                capability_receipt=native_preflight,
                weave_target=weave_target,
                wandb_external_project_id=wandb_external_project_id,
                wandb_run_id=wandb_run_id,
                provider_api_key=provider_api_key,
                wandb_api_key=wandb_api_key,
                scratch_root=Path(scratch),
                cost_basis=cost_basis,
                persistence_probe=persistence_probe,
            )
            campaign = run_review_brief_campaign(
                dataset=dataset,
                methods=[
                    DeterministicExhaustiveReviewBriefMethod(
                        implementation_sha256=deterministic_sha,
                        persistence_probe=persistence_probe,
                    ),
                    baseline,
                    native,
                ],
                source_repository=source_repository,
                source_commit=source_commit,
                campaign_wandb_run_id=wandb_run_id,
                campaign_wandb_run_url=str(getattr(run, "url", "") or ""),
                provider_cost_basis=cost_basis.contract,
            )
            persistence_probe.close()
        verified = verify_review_brief_campaign(campaign)
        weave_client.flush()
        run.summary["review_brief/status"] = "COMPLETE"
        run.summary["review_brief/campaign_sha256"] = verified.campaign_sha256
        run.summary["review_brief/dataset_sha256"] = verified.dataset.dataset_sha256
        run.summary["review_brief/record_count"] = len(verified.records)
        run.summary["review_brief/provider_capability_trace_id"] = native_preflight.trace_id
        run.summary["review_brief/provider_conversation_count"] = (
            verified.provider_conversation_count
        )
        run.summary["review_brief/pre_provider_block_count"] = verified.pre_provider_block_count
        run.summary["review_brief/default_method_decision"] = (
            verified.default_method_decision.decision
        )
        run.summary["review_brief/current_default_method"] = (
            verified.default_method_decision.current_default_method
        )
        run.summary["review_brief/native_candidate_status"] = (
            verified.default_method_decision.candidate_status
        )
        run.summary["review_brief/promotion_gate_passed"] = int(
            verified.default_method_decision.promotion_gate_passed
        )
        run.summary["review_brief/context_reduction_threshold_fraction"] = (
            verified.default_method_decision.context_reduction_threshold_fraction
        )
        run.summary["review_brief/context_measurement_method"] = (
            verified.default_method_decision.context_measurement_method
        )
        run.summary["review_brief/baseline_role_input_tokens"] = (
            verified.default_method_decision.baseline_role_input_tokens
        )
        run.summary["review_brief/native_role_input_tokens"] = (
            verified.default_method_decision.native_role_input_tokens
        )
        for scorecard in verified.scorecards:
            run.summary[f"review_brief/{scorecard.method_id}/passed_case_count"] = (
                scorecard.passed_case_count
            )
            run.summary[f"review_brief/{scorecard.method_id}/deterministic_gate_passed"] = int(
                scorecard.deterministic_gate_passed
            )
        run.finish(exit_code=0)
        return verified
    except Exception:
        run.finish(exit_code=1)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--provider-name", required=True)
    parser.add_argument("--provider-base-url", required=True)
    parser.add_argument("--provider-model", required=True)
    parser.add_argument(
        "--provider-mode",
        required=True,
        choices=("RESPONSES", "CHAT_COMPLETIONS"),
    )
    parser.add_argument("--provider-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--provider-cost-basis-ref", required=True)
    parser.add_argument("--provider-input-usd-per-million", required=True, type=float)
    parser.add_argument("--provider-output-usd-per-million", required=True, type=float)
    parser.add_argument("--wandb-entity", required=True)
    parser.add_argument("--wandb-project", required=True)
    parser.add_argument("--wandb-external-project-id", required=True)
    parser.add_argument("--wandb-api-base-url", required=True)
    parser.add_argument("--wandb-web-base-url", required=True)
    parser.add_argument("--wandb-run-id", required=True)
    parser.add_argument("--authorize-synthetic-public-provider-evaluation", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.authorize_synthetic_public_provider_evaluation:
        raise SystemExit("campaign requires explicit synthetic-public provider authorization")
    provider_api_key = os.environ.get("ECOCORRIDOR_PROVIDER_API_KEY", "")
    wandb_api_key = os.environ.get("WANDB_API_KEY", "")
    if not provider_api_key or not wandb_api_key:
        raise SystemExit(
            "campaign requires ECOCORRIDOR_PROVIDER_API_KEY and WANDB_API_KEY in process memory"
        )
    for value, label in (
        (arguments.source_repository, "source repository"),
        (arguments.provider_name, "provider name"),
        (arguments.wandb_run_id, "W&B run ID"),
    ):
        if not _OPAQUE.fullmatch(
            value.replace("/", ":", 1) if label == "source repository" else value
        ):
            raise SystemExit(f"{label} is not an opaque identifier")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", arguments.source_repository):
        raise SystemExit("source repository must be owner/name")
    root = arguments.repository_root.resolve()
    source_commit = _git_source(root, expected_commit=arguments.source_commit)
    mode = _provider_mode(arguments.provider_base_url, arguments.provider_mode)
    provider = ReviewBriefAgentProvider(
        base_url=arguments.provider_base_url,
        model=arguments.provider_model,
        mode=mode,
        timeout_seconds=arguments.provider_timeout_seconds,
    )
    cost_basis = ProviderTokenCostBasis(
        reference=arguments.provider_cost_basis_ref,
        input_usd_per_million=arguments.provider_input_usd_per_million,
        output_usd_per_million=arguments.provider_output_usd_per_million,
    )
    weave_target = ReviewBriefWeaveTarget(
        entity=arguments.wandb_entity,
        project=arguments.wandb_project,
        api_base_url=arguments.wandb_api_base_url,
        web_base_url=arguments.wandb_web_base_url,
        external_project_id_sha256=_sha256_text(arguments.wandb_external_project_id),
    )
    campaign = _run_online_campaign(
        root=root,
        source_repository=arguments.source_repository,
        source_commit=source_commit,
        provider_name=arguments.provider_name,
        provider=provider,
        provider_api_key=provider_api_key,
        wandb_api_key=wandb_api_key,
        wandb_external_project_id=arguments.wandb_external_project_id,
        weave_target=weave_target,
        wandb_run_id=arguments.wandb_run_id,
        cost_basis=cost_basis,
    )
    _write_campaign(arguments.output, campaign)
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "campaign_sha256": campaign.campaign_sha256,
                "dataset_sha256": campaign.dataset.dataset_sha256,
                "method_count": len(campaign.methods),
                "case_count": len(campaign.dataset.cases),
                "record_count": len(campaign.records),
                "default_method_decision": campaign.default_method_decision.decision,
                "current_default_method": campaign.default_method_decision.current_default_method,
                "native_candidate_status": campaign.default_method_decision.candidate_status,
                "promotion_gate_passed": campaign.default_method_decision.promotion_gate_passed,
                "video_rendered": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
