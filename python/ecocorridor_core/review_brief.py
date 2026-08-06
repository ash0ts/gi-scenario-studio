"""Fail-closed, read-only Review Brief contracts and deterministic rendering.

The injected agent receives only opaque aliases and closed vocabularies.  It
may order findings and choose from pre-authorized review actions/disciplines;
it cannot author prose, emit scientific values, name locations, select a
portfolio, or alter model state.  All renderer-visible copy is produced here
from authoritative local evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .claim_safety import assert_safe_rendered_ecological_copy
from .contracts import (
    ConnectivityEvidenceClaim,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    ContractModel,
    PortfolioDecisionSummary,
    canonical_sha256,
)
from .metrics import CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION

REVIEW_BRIEF_ACKNOWLEDGEMENT = (
    "I reviewed the cited evidence and limitations. This records review of the brief only; "
    "it does not select, approve, attest, or design a portfolio."
)
REVIEW_BRIEF_SCOPE_LIMITATION = (
    "This brief organizes cited evidence for human review; it does not select, approve, "
    "attest, design, or modify a portfolio."
)
REVIEW_BRIEF_FIELD_LIMITATION = (
    "Ecological, field, engineering, ownership, maintenance, cultural, cost, and permitting "
    "review remains required."
)
REVIEW_BRIEF_DIRECT_QUALIFICATION = "Direct Circuitscape result under documented assumptions."
REVIEW_BRIEF_VALIDATOR_VERSION = "review-brief-validator-v1"
REVIEW_BRIEF_CONTEXT_VERSION = "review-brief-agent-context/v1"
RIDGEWAY_MODEL_FITNESS_EVIDENCE_ID = "ridgeway_assumption_ledger_v1"
RIDGEWAY_COST_EVIDENCE_PREFIX = "ECO-COST-"
RIDGEWAY_HYPOTHESIS_EVIDENCE_PREFIX = "ECO-HYPOTHESIS-"
REVIEW_BRIEF_MAX_REQUIRED_TOOL_CALLS = 8
REVIEW_BRIEF_MAX_FINDING_ALIASES = 64
REVIEW_BRIEF_MAX_EVIDENCE_ALIASES = 64
REVIEW_BRIEF_MAX_SCENARIO_ALIASES = 64
REVIEW_BRIEF_AGENT_TOOLS = (
    "review_brief_get_scope",
    "review_brief_list_findings",
    "review_brief_get_findings",
    "review_brief_list_option_constraints",
    "review_brief_list_model_fitness_gaps",
    "review_brief_resolve_evidence_aliases",
    "review_brief_get_reference_status",
    "review_brief_get_authorization_status",
)

_DIGEST = r"^[0-9a-f]{64}$"
_ALIAS = re.compile(r"^(?:F|E|S)[1-9][0-9]{0,3}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_LOCAL_PATH = re.compile(
    r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\|/(?:Users|home|var|private|tmp|opt|etc)/)"
)


class ReviewBriefSafetyError(RuntimeError):
    """A brief could not satisfy its authorization or validation boundary."""


class ReviewBriefSourceMode(str, Enum):
    RIDGEWAY_SYNTHETIC = "RIDGEWAY_SYNTHETIC"
    PROFESSIONAL = "PROFESSIONAL"


class ReviewBriefCloudBoundary(str, Enum):
    CC0_SYNTHETIC = "CC0_SYNTHETIC"
    RESTRICTED_VERIFIED = "RESTRICTED_VERIFIED"
    BLOCKED = "BLOCKED"


class ReviewBriefFailureStage(str, Enum):
    LOCAL_PRECONDITION = "LOCAL_PRECONDITION"
    AUTHORIZATION = "AUTHORIZATION"
    PROVIDER_PREFLIGHT = "PROVIDER_PREFLIGHT"
    AGENT_EXECUTION = "AGENT_EXECUTION"
    OUTPUT_VALIDATION = "OUTPUT_VALIDATION"
    TRACE_READBACK = "TRACE_READBACK"
    PERSISTENCE = "PERSISTENCE"


class ReviewBriefFindingKind(str, Enum):
    RULE_FAILURE = "RULE_FAILURE"
    SURVEY_FIRST = "SURVEY_FIRST"
    SAFEGUARD = "SAFEGUARD"
    REVIEW_MASK = "REVIEW_MASK"
    FEASIBILITY_GAP = "FEASIBILITY_GAP"
    MODEL_FITNESS = "MODEL_FITNESS"
    QA = "QA"
    ASSUMPTION = "ASSUMPTION"
    COST = "COST"
    REFERENCE = "REFERENCE"
    AUTHORIZATION = "AUTHORIZATION"


class ReviewBriefTopic(str, Enum):
    """Closed, non-narrative facet used to route a finding to the right review."""

    REQUIRED_RULE = "REQUIRED_RULE"
    SURVEY_OR_FIELD_DATA = "SURVEY_OR_FIELD_DATA"
    SAFEGUARD_REVIEW = "SAFEGUARD_REVIEW"
    CURRENT_REDISTRIBUTION_MASK = "CURRENT_REDISTRIBUTION_MASK"
    GENERAL_FEASIBILITY = "GENERAL_FEASIBILITY"
    LAND_ACCESS_OR_OWNERSHIP = "LAND_ACCESS_OR_OWNERSHIP"
    MAINTENANCE_RESPONSIBILITY = "MAINTENANCE_RESPONSIBILITY"
    CULTURAL_REVIEW = "CULTURAL_REVIEW"
    PERMITTING_REVIEW = "PERMITTING_REVIEW"
    TRAFFIC_OR_STRUCTURAL_FEASIBILITY = "TRAFFIC_OR_STRUCTURAL_FEASIBILITY"
    ECOLOGICAL_FIELD_RISK = "ECOLOGICAL_FIELD_RISK"
    MODEL_FITNESS_BASIS = "MODEL_FITNESS_BASIS"
    DIRECT_RUN_QA = "DIRECT_RUN_QA"
    ECOLOGICAL_ASSUMPTION = "ECOLOGICAL_ASSUMPTION"
    COST_BASIS = "COST_BASIS"
    REFERENCE_COMPARISON = "REFERENCE_COMPARISON"
    DATA_AUTHORIZATION = "DATA_AUTHORIZATION"


class ReviewBriefReasonFacet(str, Enum):
    """Closed reason codes; never model-authored prose or scientific values."""

    CONFIGURED_RULE_NOT_MET = "CONFIGURED_RULE_NOT_MET"
    SURVEY_EVIDENCE_GAP = "SURVEY_EVIDENCE_GAP"
    SAFEGUARD_NONREGRESSION_NOT_MET = "SAFEGUARD_NONREGRESSION_NOT_MET"
    REVIEW_MASK_THRESHOLD_TRIGGERED = "REVIEW_MASK_THRESHOLD_TRIGGERED"
    FEASIBILITY_BASIS_INCOMPLETE = "FEASIBILITY_BASIS_INCOMPLETE"
    LAND_ACCESS_UNRESOLVED = "LAND_ACCESS_UNRESOLVED"
    MAINTENANCE_RESPONSIBILITY_UNRESOLVED = "MAINTENANCE_RESPONSIBILITY_UNRESOLVED"
    CULTURAL_REVIEW_UNRESOLVED = "CULTURAL_REVIEW_UNRESOLVED"
    PERMITTING_REVIEW_UNRESOLVED = "PERMITTING_REVIEW_UNRESOLVED"
    TRAFFIC_OR_STRUCTURAL_REVIEW_UNRESOLVED = "TRAFFIC_OR_STRUCTURAL_REVIEW_UNRESOLVED"
    ECOLOGICAL_FIELD_RISK_UNRESOLVED = "ECOLOGICAL_FIELD_RISK_UNRESOLVED"
    MODEL_FITNESS_BASIS_INCOMPLETE = "MODEL_FITNESS_BASIS_INCOMPLETE"
    DIRECT_RUN_QA_NOT_ACCEPTED = "DIRECT_RUN_QA_NOT_ACCEPTED"
    ECOLOGICAL_ASSUMPTION_UNRESOLVED = "ECOLOGICAL_ASSUMPTION_UNRESOLVED"
    HYPOTHESIS_SENSITIVITY = "HYPOTHESIS_SENSITIVITY"
    COST_RULE_EXCEEDED = "COST_RULE_EXCEEDED"
    REFERENCE_COMPARISON_NOT_ACCEPTED = "REFERENCE_COMPARISON_NOT_ACCEPTED"
    DATA_AUTHORIZATION_NOT_VERIFIED = "DATA_AUTHORIZATION_NOT_VERIFIED"


class ReviewBriefEvidenceClass(str, Enum):
    """Closed provenance branch visible to the agent instead of raw evidence."""

    DIRECT_CIRCUITSCAPE_RESULT = "DIRECT_CIRCUITSCAPE_RESULT"
    DIRECT_CURRENT_DIAGNOSTIC = "DIRECT_CURRENT_DIAGNOSTIC"
    RETAINED_DECISION_RULE = "RETAINED_DECISION_RULE"
    RETAINED_MODEL_FITNESS = "RETAINED_MODEL_FITNESS"
    RETAINED_FEASIBILITY = "RETAINED_FEASIBILITY"
    RETAINED_COST_BASIS = "RETAINED_COST_BASIS"
    RETAINED_HYPOTHESIS_COMPARISON = "RETAINED_HYPOTHESIS_COMPARISON"
    RETAINED_REFERENCE_COMPARISON = "RETAINED_REFERENCE_COMPARISON"
    RETAINED_DATA_AUTHORIZATION = "RETAINED_DATA_AUTHORIZATION"


class ReviewBriefScenarioBreadth(str, Enum):
    PROJECT_WIDE = "PROJECT_WIDE"
    SINGLE_SCENARIO = "SINGLE_SCENARIO"
    SCENARIO_SUBSET = "SCENARIO_SUBSET"
    COMPLETE_ENSEMBLE = "COMPLETE_ENSEMBLE"


class ReviewBriefPriority(str, Enum):
    BLOCKING = "BLOCKING"
    BEFORE_FEASIBILITY = "BEFORE_FEASIBILITY"
    FOLLOW_UP = "FOLLOW_UP"


class ReviewBriefActionKind(str, Enum):
    INSPECT_EVIDENCE = "INSPECT_EVIDENCE"
    FIELD_VERIFY = "FIELD_VERIFY"
    OBTAIN_QUALIFIED_REVIEW = "OBTAIN_QUALIFIED_REVIEW"
    RESOLVE_LAND_OR_OWNERSHIP = "RESOLVE_LAND_OR_OWNERSHIP"
    RESOLVE_AUTHORIZATION = "RESOLVE_AUTHORIZATION"
    COMPLETE_COST_BASIS = "COMPLETE_COST_BASIS"
    REVISE_MODEL_AND_RERUN = "REVISE_MODEL_AND_RERUN"


class ReviewerDiscipline(str, Enum):
    ECOLOGY = "ECOLOGY"
    FIELD = "FIELD"
    ENGINEERING = "ENGINEERING"
    LAND = "LAND"
    MAINTENANCE = "MAINTENANCE"
    CULTURAL = "CULTURAL"
    PERMITTING = "PERMITTING"
    COST = "COST"


ReviewBriefToolName = Literal[
    "review_brief_get_scope",
    "review_brief_list_findings",
    "review_brief_get_findings",
    "review_brief_list_option_constraints",
    "review_brief_list_model_fitness_gaps",
    "review_brief_resolve_evidence_aliases",
    "review_brief_get_reference_status",
    "review_brief_get_authorization_status",
]


class ReviewBriefDeterministicItem(ContractModel):
    """Locally rendered default checklist item; never provider-authored."""

    item_id: str = Field(min_length=1)
    finding_kind: ReviewBriefFindingKind
    review_topic: ReviewBriefTopic
    priority: ReviewBriefPriority
    action_kind: ReviewBriefActionKind
    reviewer_disciplines: list[ReviewerDiscipline] = Field(min_length=1)
    evidence_claim_ids: list[str] = Field(min_length=1)
    direct_run_ids: list[str] = Field(default_factory=list)
    scenario_ids: list[str] = Field(default_factory=list)
    rendered_text: str = Field(min_length=1)


class ReviewBriefPreflight(ContractModel):
    source_mode: ReviewBriefSourceMode
    project_title: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    model_revision_id: str = Field(min_length=1)
    decision_board_sha256: str = Field(pattern=_DIGEST)
    scenario_count: int = Field(gt=0)
    evidence_claim_count: int = Field(ge=0)
    deterministic_available: bool = False
    deterministic_items: list[ReviewBriefDeterministicItem] = Field(default_factory=list)
    disposition: Literal["PASSES", "FAILS", "SURVEY_FIRST", "INCOMPLETE"]
    next_action: str = Field(min_length=1)
    cloud_boundary: ReviewBriefCloudBoundary
    provider_model: str | None = None
    provider_ready: bool
    weave_ready: bool
    agent_release_status: Literal["EXPERIMENTAL", "BENCHMARK_APPROVED"] = "EXPERIMENTAL"
    production_default: Literal["DETERMINISTIC_CHECKLIST", "REVIEW_BRIEF_AGENT"] = (
        "DETERMINISTIC_CHECKLIST"
    )
    experimental_acknowledgement_required: bool = True
    eligible: bool
    blocked_reason: str | None = None

    @model_validator(mode="after")
    def validate_eligibility(self) -> ReviewBriefPreflight:
        boundary_allowed = (
            self.source_mode == ReviewBriefSourceMode.RIDGEWAY_SYNTHETIC
            and self.cloud_boundary == ReviewBriefCloudBoundary.CC0_SYNTHETIC
        ) or (
            self.source_mode == ReviewBriefSourceMode.PROFESSIONAL
            and self.cloud_boundary == ReviewBriefCloudBoundary.RESTRICTED_VERIFIED
        )
        expected = bool(
            boundary_allowed
            and self.provider_ready
            and self.weave_ready
            and self.evidence_claim_count > 0
        )
        if self.eligible != expected:
            raise ValueError("review-brief preflight eligibility is inconsistent")
        if self.eligible == bool(self.blocked_reason):
            raise ValueError("only an ineligible review brief requires a blocked reason")
        if self.provider_ready != bool(self.provider_model):
            raise ValueError("provider readiness requires an exact configured model")
        if self.deterministic_available != bool(self.deterministic_items):
            raise ValueError("deterministic checklist availability is inconsistent")
        if self.deterministic_items:
            evidence_ids = {
                evidence_id
                for item in self.deterministic_items
                for evidence_id in item.evidence_claim_ids
            }
            if len(evidence_ids) != self.evidence_claim_count:
                raise ValueError("deterministic checklist evidence coverage is inconsistent")
            if len({item.item_id for item in self.deterministic_items}) != len(
                self.deterministic_items
            ):
                raise ValueError("deterministic checklist item identities must be unique")
        if (
            self.production_default == "REVIEW_BRIEF_AGENT"
            and self.agent_release_status != "BENCHMARK_APPROVED"
        ):
            raise ValueError("only a benchmark-approved agent may become the production default")
        if self.agent_release_status == "EXPERIMENTAL" and (
            self.production_default != "DETERMINISTIC_CHECKLIST"
            or not self.experimental_acknowledgement_required
        ):
            raise ValueError("an experimental agent requires opt-in and a deterministic default")
        return self


class ReviewBriefDispositionSnapshot(ContractModel):
    disposition: Literal["PASSES", "FAILS", "SURVEY_FIRST", "INCOMPLETE"]
    next_action: str = Field(min_length=1)
    controlling_scenario_id: str | None = None
    safeguard_status: Literal["PASS", "FAIL", "REVIEW"]
    review_mask_triggered: bool


class ReviewBriefItem(ContractModel):
    item_id: str = Field(min_length=1)
    finding_kind: ReviewBriefFindingKind
    review_topic: ReviewBriefTopic
    priority: ReviewBriefPriority
    action_kind: ReviewBriefActionKind
    reviewer_disciplines: list[ReviewerDiscipline] = Field(min_length=1)
    evidence_claim_ids: list[str] = Field(min_length=1)
    direct_run_ids: list[str] = Field(default_factory=list)
    scenario_ids: list[str] = Field(default_factory=list)
    rendered_text: str = Field(min_length=1)
    state: Literal["OPEN"] = "OPEN"

    @model_validator(mode="after")
    def validate_unique_references(self) -> ReviewBriefItem:
        for values, label in (
            (self.reviewer_disciplines, "reviewer disciplines"),
            (self.evidence_claim_ids, "evidence claims"),
            (self.direct_run_ids, "direct runs"),
            (self.scenario_ids, "scenarios"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"review-brief item {label} must be unique")
        return self


class ReviewBriefTrace(ContractModel):
    trace_id: str = Field(min_length=1)
    weave_call_id: str = Field(min_length=1)
    association_id: str = Field(min_length=1)
    wandb_run_id: str = Field(min_length=1)
    flushed: Literal[True] = True
    readback_verified: Literal[True] = True
    trace_url: str | None = None

    @field_validator("trace_id", "weave_call_id", "association_id", "wandb_run_id")
    @classmethod
    def validate_opaque_ids(cls, value: str) -> str:
        if not _OPAQUE.fullmatch(value):
            raise ValueError("review-brief trace identities must be opaque identifiers")
        return value

    @field_validator("trace_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("review-brief trace URL must be HTTP(S)")
        return value


class ReviewBriefValidation(ContractModel):
    validator_version: Literal[REVIEW_BRIEF_VALIDATOR_VERSION] = REVIEW_BRIEF_VALIDATOR_VERSION
    evidence_ids_valid: Literal[True] = True
    numeric_and_unit_faithful: Literal[True] = True
    required_limitations_present: Literal[True] = True
    unsupported_findings: Literal[0] = 0
    prohibited_claims: Literal[0] = 0
    sensitive_tokens: Literal[0] = 0
    unauthorized_actions: Literal[0] = 0
    source_snapshot_revalidated: Literal[True] = True


class ReviewBriefDraft(ContractModel):
    schema_version: Literal["review-brief-draft/v1"] = "review-brief-draft/v1"
    draft_id: str = Field(min_length=1)
    status: Literal["UNCONFIRMED", "STALE"] = "UNCONFIRMED"
    source_mode: ReviewBriefSourceMode
    analysis_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    model_revision_id: str = Field(min_length=1)
    decision_board_sha256: str = Field(pattern=_DIGEST)
    created_at: AwareDatetime
    supersedes_draft_id: str | None = None
    disposition_snapshot: ReviewBriefDispositionSnapshot
    items: list[ReviewBriefItem]
    limitations: list[str] = Field(min_length=1)
    trace: ReviewBriefTrace
    validation: ReviewBriefValidation

    @model_validator(mode="after")
    def validate_draft(self) -> ReviewBriefDraft:
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("review-brief item IDs must be unique")
        for item in self.items:
            if item.rendered_text not in _allowed_rendered_findings(item.review_topic):
                raise ValueError("review-brief prose must come from the deterministic renderer")
            topic_actions, topic_disciplines = topic_permissions(
                item.finding_kind,
                item.review_topic,
            )
            if item.action_kind not in topic_actions:
                raise ValueError("review-brief action is not permitted for its finding kind")
            if not set(item.reviewer_disciplines).issubset(topic_disciplines):
                raise ValueError(
                    "review-brief disciplines are not permitted for their finding kind"
                )
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("review-brief limitations must be unique")
        required_limitations = {
            REVIEW_BRIEF_DIRECT_QUALIFICATION,
            CONNECTIVITY_LIMITATION,
            CURRENT_MAP_LIMITATION,
            REVIEW_BRIEF_SCOPE_LIMITATION,
            REVIEW_BRIEF_FIELD_LIMITATION,
        }
        if not required_limitations.issubset(self.limitations):
            raise ValueError("review brief omits a required scientific or non-approval boundary")
        if self.supersedes_draft_id == self.draft_id:
            raise ValueError("a review brief cannot supersede itself")
        return self


class ReviewBriefAcknowledgement(ContractModel):
    acknowledgement_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    statement: Literal[REVIEW_BRIEF_ACKNOWLEDGEMENT]
    acknowledged_at: AwareDatetime
    model_unchanged: Literal[True] = True
    portfolio_selected: Literal[False] = False


class ReviewBriefFeedbackTraceReceipt(ContractModel):
    weave_feedback_id: str = Field(min_length=1, max_length=256)
    trace_id: str = Field(min_length=1, max_length=256)
    weave_call_id: str = Field(min_length=1, max_length=256)
    wandb_run_id: str = Field(min_length=1, max_length=256)
    verified: Literal[True] = True

    @field_validator("weave_feedback_id", "trace_id", "weave_call_id", "wandb_run_id")
    @classmethod
    def validate_opaque_ids(cls, value: str) -> str:
        if not _OPAQUE.fullmatch(value):
            raise ValueError("review-brief feedback identities must be opaque identifiers")
        return value


class ReviewBriefFeedbackRecord(ContractModel):
    feedback_id: str = Field(min_length=1)
    draft_id: str = Field(min_length=1)
    rating: Literal["HELPFUL", "NOT_HELPFUL"]
    comment: str = Field(default="", max_length=1_024)
    recorded_at: AwareDatetime
    trace_receipt: ReviewBriefFeedbackTraceReceipt


class ReviewBriefRejectedRunnerReceipt(ContractModel):
    """Safe receipt retained when a traced runner result is rejected locally."""

    provider_call_id_sha256: str = Field(pattern=_DIGEST)
    provider_model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
    tool_calls: list[ReviewBriefToolName] = Field(
        min_length=1, max_length=REVIEW_BRIEF_MAX_REQUIRED_TOOL_CALLS
    )
    trace: ReviewBriefTrace
    verified: Literal[True] = True


class ReviewBriefRejectionAudit(ContractModel):
    schema_version: Literal["review-brief-rejection/v1"] = "review-brief-rejection/v1"
    call_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    portfolio_id: str = Field(min_length=1)
    operation: Literal["assistant.review_brief.prepare"] = "assistant.review_brief.prepare"
    disposition: Literal["REJECTED"] = "REJECTED"
    error_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    failure_stage: ReviewBriefFailureStage | None = None
    decision_board_sha256: str | None = Field(default=None, pattern=_DIGEST)
    runner_invoked: bool
    runner_receipt: ReviewBriefRejectedRunnerReceipt | None = None
    answer_returned: Literal[False] = False
    created_at: AwareDatetime
    audit_sha256: str | None = Field(default=None, pattern=_DIGEST)
    local_audit_verified: bool = False

    @model_validator(mode="after")
    def validate_runner_receipt(self) -> ReviewBriefRejectionAudit:
        if self.runner_receipt is not None and not self.runner_invoked:
            raise ValueError("a rejected runner receipt requires an invoked runner")
        return self


class ReviewBriefEvidenceBranch(ContractModel):
    """One opaque provider alias classified without revealing its local identity."""

    evidence_alias: str = Field(pattern=r"^E[1-9][0-9]{0,3}$")
    evidence_class: ReviewBriefEvidenceClass


class ReviewBriefLocalFinding(ContractModel):
    """Worker-local binding; actual evidence/scenario IDs never reach the runner."""

    finding_id: str
    finding_kind: ReviewBriefFindingKind
    review_topic: ReviewBriefTopic | None = None
    reason_facets: list[ReviewBriefReasonFacet] = Field(default_factory=list)
    evidence_classes: list[ReviewBriefEvidenceClass] = Field(default_factory=list)
    priority: ReviewBriefPriority
    evidence_claim_ids: list[str] = Field(min_length=1)
    direct_run_ids: list[str] = Field(default_factory=list)
    scenario_ids: list[str] = Field(default_factory=list)
    permitted_actions: list[ReviewBriefActionKind] = Field(min_length=1)
    permitted_disciplines: list[ReviewerDiscipline] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_local_finding(self) -> ReviewBriefLocalFinding:
        topic = self.review_topic or _DEFAULT_TOPIC_BY_KIND[self.finding_kind]
        object.__setattr__(self, "review_topic", topic)
        reasons = self.reason_facets or [_DEFAULT_REASON_BY_TOPIC[topic]]
        evidence_classes = self.evidence_classes or list(
            _finding_evidence_classes(
                topic=topic,
                evidence_ids=self.evidence_claim_ids,
                direct_run_ids=self.direct_run_ids,
            )
        )
        object.__setattr__(self, "reason_facets", reasons)
        object.__setattr__(self, "evidence_classes", evidence_classes)
        for values, label in (
            (self.reason_facets, "reason facet"),
            (self.evidence_classes, "evidence class"),
            (self.evidence_claim_ids, "evidence"),
            (self.direct_run_ids, "direct run"),
            (self.scenario_ids, "scenario"),
            (self.permitted_actions, "action"),
            (self.permitted_disciplines, "discipline"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"review finding {label} values must be unique")
        if not set(self.reason_facets).issubset(_TOPIC_REASON_FACETS[topic]):
            raise ValueError("review finding reason facet is inappropriate for its topic")
        if not set(self.permitted_actions).issubset(_TOPIC_ACTIONS[topic]):
            raise ValueError("review finding actions are inappropriate for its review topic")
        if not set(self.permitted_disciplines).issubset(_TOPIC_DISCIPLINES[topic]):
            raise ValueError("review finding disciplines are inappropriate for its review topic")
        if not set(self.permitted_disciplines).intersection(_TOPIC_PRIMARY_DISCIPLINES[topic]):
            raise ValueError("review finding omits a primary discipline for its review topic")
        return self


class ReviewBriefFindingContext(ContractModel):
    finding_alias: str = Field(pattern=r"^F[1-9][0-9]{0,3}$")
    finding_kind: ReviewBriefFindingKind
    review_topic: ReviewBriefTopic | None = None
    reason_facets: list[ReviewBriefReasonFacet] = Field(default_factory=list)
    evidence_classes: list[ReviewBriefEvidenceClass] = Field(default_factory=list)
    priority: ReviewBriefPriority
    evidence_aliases: list[str] = Field(min_length=1, max_length=REVIEW_BRIEF_MAX_EVIDENCE_ALIASES)
    evidence_branches: list[ReviewBriefEvidenceBranch] = Field(
        default_factory=list, max_length=REVIEW_BRIEF_MAX_EVIDENCE_ALIASES
    )
    scenario_aliases: list[str] = Field(
        default_factory=list, max_length=REVIEW_BRIEF_MAX_SCENARIO_ALIASES
    )
    scenario_breadth: ReviewBriefScenarioBreadth | None = None
    direct_run_count: int = Field(default=0, ge=0)
    permitted_actions: list[ReviewBriefActionKind] = Field(min_length=1)
    permitted_disciplines: list[ReviewerDiscipline] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_topic_permissions(self) -> ReviewBriefFindingContext:
        topic = self.review_topic or _DEFAULT_TOPIC_BY_KIND[self.finding_kind]
        object.__setattr__(self, "review_topic", topic)
        reasons = self.reason_facets or [_DEFAULT_REASON_BY_TOPIC[topic]]
        evidence_classes = self.evidence_classes or list(
            _finding_evidence_classes(
                topic=topic,
                evidence_ids=self.evidence_aliases,
                direct_run_ids=(("DIRECT",) if self.direct_run_count else ()),
            )
        )
        evidence_branches = self.evidence_branches or [
            ReviewBriefEvidenceBranch(
                evidence_alias=alias,
                evidence_class=evidence_classes[0],
            )
            for alias in self.evidence_aliases
        ]
        scenario_breadth = self.scenario_breadth or (
            ReviewBriefScenarioBreadth.PROJECT_WIDE
            if not self.scenario_aliases
            else ReviewBriefScenarioBreadth.SINGLE_SCENARIO
            if len(self.scenario_aliases) == 1
            else ReviewBriefScenarioBreadth.SCENARIO_SUBSET
        )
        object.__setattr__(self, "reason_facets", reasons)
        object.__setattr__(self, "evidence_classes", evidence_classes)
        object.__setattr__(self, "evidence_branches", evidence_branches)
        object.__setattr__(self, "scenario_breadth", scenario_breadth)
        for values, label in (
            (self.reason_facets, "reason facets"),
            (self.evidence_classes, "evidence classes"),
            (self.evidence_aliases, "evidence aliases"),
            (self.scenario_aliases, "scenario aliases"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"review context {label} must be unique")
        branch_aliases = [item.evidence_alias for item in self.evidence_branches]
        if len(branch_aliases) != len(set(branch_aliases)) or set(branch_aliases) != set(
            self.evidence_aliases
        ):
            raise ValueError("review context evidence branches must cover each evidence alias")
        if {item.evidence_class for item in self.evidence_branches} != set(self.evidence_classes):
            raise ValueError("review context evidence classes differ from its branches")
        if not set(self.reason_facets).issubset(_TOPIC_REASON_FACETS[topic]):
            raise ValueError("review context reason facet is inappropriate for its topic")
        scenario_count = len(self.scenario_aliases)
        if (
            (self.scenario_breadth == ReviewBriefScenarioBreadth.PROJECT_WIDE and scenario_count)
            or (
                self.scenario_breadth == ReviewBriefScenarioBreadth.SINGLE_SCENARIO
                and scenario_count != 1
            )
            or (
                self.scenario_breadth == ReviewBriefScenarioBreadth.SCENARIO_SUBSET
                and scenario_count < 2
            )
            or (
                self.scenario_breadth == ReviewBriefScenarioBreadth.COMPLETE_ENSEMBLE
                and scenario_count < 1
            )
        ):
            raise ValueError("review context scenario breadth is inconsistent")
        if not set(self.permitted_actions).issubset(_TOPIC_ACTIONS[topic]):
            raise ValueError("review context actions are inappropriate for its review topic")
        if not set(self.permitted_disciplines).issubset(_TOPIC_DISCIPLINES[topic]):
            raise ValueError("review context disciplines are inappropriate for its review topic")
        if not set(self.permitted_disciplines).intersection(_TOPIC_PRIMARY_DISCIPLINES[topic]):
            raise ValueError("review context omits a primary discipline for its review topic")
        return self


class ReviewBriefAuthorizedSnapshot(ContractModel):
    schema_version: Literal[REVIEW_BRIEF_CONTEXT_VERSION] = REVIEW_BRIEF_CONTEXT_VERSION
    snapshot_sha256: str = Field(pattern=_DIGEST)
    source_mode: ReviewBriefSourceMode
    disposition: Literal["PASSES", "FAILS", "SURVEY_FIRST", "INCOMPLETE"]
    analysis_id_sha256: str = Field(pattern=_DIGEST)
    model_revision_id_sha256: str = Field(pattern=_DIGEST)
    evidence_authorization_decision_id: str = Field(pattern=r"^evidence_auth_[0-9a-f]{24}$")
    current_authorizations_sha256: str = Field(pattern=_DIGEST)
    authorization_sha256: str = Field(pattern=_DIGEST)
    findings: list[ReviewBriefFindingContext] = Field(
        min_length=1, max_length=REVIEW_BRIEF_MAX_FINDING_ALIASES
    )

    @model_validator(mode="after")
    def validate_identity(self) -> ReviewBriefAuthorizedSnapshot:
        aliases = [item.finding_alias for item in self.findings]
        if len(aliases) != len(set(aliases)):
            raise ValueError("review finding aliases must be unique")
        evidence_aliases = [alias for item in self.findings for alias in item.evidence_aliases]
        scenario_aliases = [alias for item in self.findings for alias in item.scenario_aliases]
        if len(set(evidence_aliases)) > REVIEW_BRIEF_MAX_EVIDENCE_ALIASES:
            raise ValueError("review context exceeds the one-call evidence alias budget")
        if len(set(scenario_aliases)) > REVIEW_BRIEF_MAX_SCENARIO_ALIASES:
            raise ValueError("review context exceeds the scenario alias budget")
        if any(not _ALIAS.fullmatch(item) for item in (*evidence_aliases, *scenario_aliases)):
            raise ValueError("review context contains a non-opaque alias")
        identity = self.model_dump(mode="json", exclude={"snapshot_sha256"})
        if canonical_sha256(identity) != self.snapshot_sha256:
            raise ValueError("review context digest is inconsistent")
        return self

    @classmethod
    def create(cls, **payload: Any) -> ReviewBriefAuthorizedSnapshot:
        normalized = {
            **payload,
            "schema_version": REVIEW_BRIEF_CONTEXT_VERSION,
            "source_mode": ReviewBriefSourceMode(payload["source_mode"]).value,
            "findings": [
                ReviewBriefFindingContext.model_validate(item).model_dump(mode="json")
                for item in payload["findings"]
            ],
        }
        return cls.model_validate({**normalized, "snapshot_sha256": canonical_sha256(normalized)})


class ReviewBriefRunnerRequest(ContractModel):
    call_alias: Literal["CALL1"] = "CALL1"
    operation: Literal["REVIEW_BRIEF_PREPARE"] = "REVIEW_BRIEF_PREPARE"
    snapshot: ReviewBriefAuthorizedSnapshot


class ReviewBriefFindingSelection(ContractModel):
    finding_alias: str = Field(pattern=r"^F[1-9][0-9]{0,3}$")
    action_kind: ReviewBriefActionKind
    reviewer_disciplines: list[ReviewerDiscipline] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_disciplines(self) -> ReviewBriefFindingSelection:
        if len(self.reviewer_disciplines) != len(set(self.reviewer_disciplines)):
            raise ValueError("selected reviewer disciplines must be unique")
        return self


class ReviewBriefRunnerResult(ContractModel):
    snapshot_sha256: str = Field(pattern=_DIGEST)
    selections: list[ReviewBriefFindingSelection] = Field(min_length=1)
    runner_kind: Literal["OPENAI_AGENTS_SDK"] = "OPENAI_AGENTS_SDK"
    provider_call_id_sha256: str = Field(pattern=_DIGEST)
    provider_model: str
    tool_calls: list[ReviewBriefToolName] = Field(
        min_length=1, max_length=REVIEW_BRIEF_MAX_REQUIRED_TOOL_CALLS
    )
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    trace: ReviewBriefTrace

    @field_validator("provider_model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not _MODEL.fullmatch(value):
            raise ValueError("review runner model must be an opaque model token")
        return value

    @model_validator(mode="after")
    def validate_aliases(self) -> ReviewBriefRunnerResult:
        aliases = [item.finding_alias for item in self.selections]
        if len(aliases) != len(set(aliases)):
            raise ValueError("review runner finding selections must be unique")
        return self


class ReviewBriefRunner(Protocol):
    """Injected process boundary used by ``assistant.review_brief.prepare``."""

    def prepare(self, *, request: ReviewBriefRunnerRequest) -> ReviewBriefRunnerResult: ...


class ReviewBriefSource:
    """Authoritative local facts used to preflight and deterministically render a brief."""

    def __init__(
        self,
        *,
        source_mode: ReviewBriefSourceMode,
        project_id: str,
        project_title: str,
        analysis_id: str,
        portfolio_id: str,
        model_revision_id: str,
        decision_board_sha256: str,
        scenario_ids: Sequence[str],
        disposition: str,
        next_action: str,
        controlling_scenario_id: str | None,
        safeguard_status: Literal["PASS", "FAIL", "REVIEW"],
        review_mask_triggered: bool,
        authorization_sha256: str,
        evidence_authorization_decision_id: str,
        current_authorizations_sha256: str,
        findings: Sequence[ReviewBriefLocalFinding],
    ) -> None:
        self.source_mode = ReviewBriefSourceMode(source_mode)
        self.project_id = project_id
        self.project_title = project_title
        self.analysis_id = analysis_id
        self.portfolio_id = portfolio_id
        self.model_revision_id = model_revision_id
        self.decision_board_sha256 = decision_board_sha256
        self.scenario_ids = tuple(scenario_ids)
        self.disposition = disposition
        self.next_action = next_action
        self.controlling_scenario_id = controlling_scenario_id
        self.safeguard_status = safeguard_status
        self.review_mask_triggered = review_mask_triggered
        self.authorization_sha256 = authorization_sha256
        self.evidence_authorization_decision_id = evidence_authorization_decision_id
        self.current_authorizations_sha256 = current_authorizations_sha256
        self.findings = tuple(findings)
        try:
            # Only renderer-facing narrative belongs at the ecological-claim boundary.
            # Opaque IDs are deliberately excluded so token fragments inside an ID
            # cannot produce a substring false positive.
            assert_safe_rendered_ecological_copy(
                {
                    "project_title": self.project_title,
                    "next_action": self.next_action,
                }
            )
        except ValueError as exc:
            raise ReviewBriefSafetyError("REVIEW_BRIEF_SOURCE_PROHIBITED_CLAIM") from exc
        ReviewBriefPreflight(
            source_mode=self.source_mode,
            project_title=self.project_title,
            analysis_id=self.analysis_id,
            portfolio_id=self.portfolio_id,
            model_revision_id=self.model_revision_id,
            decision_board_sha256=self.decision_board_sha256,
            scenario_count=len(self.scenario_ids),
            evidence_claim_count=len(self.evidence_claim_ids),
            disposition=self.disposition,
            next_action=self.next_action,
            cloud_boundary=(
                ReviewBriefCloudBoundary.CC0_SYNTHETIC
                if self.source_mode == ReviewBriefSourceMode.RIDGEWAY_SYNTHETIC
                else ReviewBriefCloudBoundary.RESTRICTED_VERIFIED
            ),
            provider_model="validation-model",
            provider_ready=True,
            weave_ready=True,
            eligible=bool(self.findings),
            blocked_reason=None if self.findings else "No cited review finding is available.",
        )

    @property
    def evidence_claim_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                claim_id for finding in self.findings for claim_id in finding.evidence_claim_ids
            )
        )

    @property
    def model_fitness_evidence_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                evidence_id
                for finding in self.findings
                if finding.review_topic
                in {
                    ReviewBriefTopic.MODEL_FITNESS_BASIS,
                    ReviewBriefTopic.ECOLOGICAL_ASSUMPTION,
                }
                for evidence_id in finding.evidence_claim_ids
                if not evidence_id.startswith(
                    (RIDGEWAY_COST_EVIDENCE_PREFIX, RIDGEWAY_HYPOTHESIS_EVIDENCE_PREFIX)
                )
            )
        )

    @property
    def decision_record_evidence_ids(self) -> tuple[str, ...]:
        """Non-claim Ridgeway records that require a typed local resolver."""

        return tuple(
            item
            for item in self.evidence_claim_ids
            if item.startswith((RIDGEWAY_COST_EVIDENCE_PREFIX, RIDGEWAY_HYPOTHESIS_EVIDENCE_PREFIX))
        )

    @property
    def connectivity_claim_ids(self) -> tuple[str, ...]:
        retained_record_ids = {
            *self.model_fitness_evidence_ids,
            *self.decision_record_evidence_ids,
        }
        return tuple(item for item in self.evidence_claim_ids if item not in retained_record_ids)


_KIND_ACTIONS: Mapping[ReviewBriefFindingKind, tuple[ReviewBriefActionKind, ...]] = {
    ReviewBriefFindingKind.RULE_FAILURE: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.REVISE_MODEL_AND_RERUN,
    ),
    ReviewBriefFindingKind.SURVEY_FIRST: (ReviewBriefActionKind.FIELD_VERIFY,),
    ReviewBriefFindingKind.SAFEGUARD: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
    ),
    ReviewBriefFindingKind.REVIEW_MASK: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.FIELD_VERIFY,
    ),
    ReviewBriefFindingKind.FEASIBILITY_GAP: (
        ReviewBriefActionKind.FIELD_VERIFY,
        ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
        ReviewBriefActionKind.RESOLVE_LAND_OR_OWNERSHIP,
    ),
    ReviewBriefFindingKind.MODEL_FITNESS: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.REVISE_MODEL_AND_RERUN,
    ),
    ReviewBriefFindingKind.QA: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.REVISE_MODEL_AND_RERUN,
    ),
    ReviewBriefFindingKind.ASSUMPTION: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
    ),
    ReviewBriefFindingKind.COST: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.COMPLETE_COST_BASIS,
    ),
    ReviewBriefFindingKind.REFERENCE: (ReviewBriefActionKind.INSPECT_EVIDENCE,),
    ReviewBriefFindingKind.AUTHORIZATION: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.RESOLVE_AUTHORIZATION,
    ),
}

_KIND_DISCIPLINES: Mapping[ReviewBriefFindingKind, tuple[ReviewerDiscipline, ...]] = {
    ReviewBriefFindingKind.RULE_FAILURE: (
        ReviewerDiscipline.ECOLOGY,
        ReviewerDiscipline.ENGINEERING,
    ),
    ReviewBriefFindingKind.SURVEY_FIRST: (ReviewerDiscipline.ECOLOGY, ReviewerDiscipline.FIELD),
    ReviewBriefFindingKind.SAFEGUARD: (ReviewerDiscipline.ECOLOGY,),
    ReviewBriefFindingKind.REVIEW_MASK: (
        ReviewerDiscipline.ECOLOGY,
        ReviewerDiscipline.FIELD,
        ReviewerDiscipline.ENGINEERING,
    ),
    ReviewBriefFindingKind.FEASIBILITY_GAP: (
        ReviewerDiscipline.FIELD,
        ReviewerDiscipline.ENGINEERING,
        ReviewerDiscipline.LAND,
        ReviewerDiscipline.MAINTENANCE,
        ReviewerDiscipline.CULTURAL,
        ReviewerDiscipline.PERMITTING,
    ),
    ReviewBriefFindingKind.MODEL_FITNESS: (ReviewerDiscipline.ECOLOGY,),
    ReviewBriefFindingKind.QA: (ReviewerDiscipline.ECOLOGY, ReviewerDiscipline.ENGINEERING),
    ReviewBriefFindingKind.ASSUMPTION: (ReviewerDiscipline.ECOLOGY,),
    ReviewBriefFindingKind.COST: (ReviewerDiscipline.COST, ReviewerDiscipline.MAINTENANCE),
    ReviewBriefFindingKind.REFERENCE: (ReviewerDiscipline.ECOLOGY,),
    ReviewBriefFindingKind.AUTHORIZATION: (
        ReviewerDiscipline.LAND,
        ReviewerDiscipline.CULTURAL,
        ReviewerDiscipline.PERMITTING,
    ),
}

_DEFAULT_TOPIC_BY_KIND: Mapping[ReviewBriefFindingKind, ReviewBriefTopic] = {
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

_DEFAULT_REASON_BY_TOPIC: Mapping[ReviewBriefTopic, ReviewBriefReasonFacet] = {
    ReviewBriefTopic.REQUIRED_RULE: ReviewBriefReasonFacet.CONFIGURED_RULE_NOT_MET,
    ReviewBriefTopic.SURVEY_OR_FIELD_DATA: ReviewBriefReasonFacet.SURVEY_EVIDENCE_GAP,
    ReviewBriefTopic.SAFEGUARD_REVIEW: (ReviewBriefReasonFacet.SAFEGUARD_NONREGRESSION_NOT_MET),
    ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK: (
        ReviewBriefReasonFacet.REVIEW_MASK_THRESHOLD_TRIGGERED
    ),
    ReviewBriefTopic.GENERAL_FEASIBILITY: ReviewBriefReasonFacet.FEASIBILITY_BASIS_INCOMPLETE,
    ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP: ReviewBriefReasonFacet.LAND_ACCESS_UNRESOLVED,
    ReviewBriefTopic.MAINTENANCE_RESPONSIBILITY: (
        ReviewBriefReasonFacet.MAINTENANCE_RESPONSIBILITY_UNRESOLVED
    ),
    ReviewBriefTopic.CULTURAL_REVIEW: ReviewBriefReasonFacet.CULTURAL_REVIEW_UNRESOLVED,
    ReviewBriefTopic.PERMITTING_REVIEW: ReviewBriefReasonFacet.PERMITTING_REVIEW_UNRESOLVED,
    ReviewBriefTopic.TRAFFIC_OR_STRUCTURAL_FEASIBILITY: (
        ReviewBriefReasonFacet.TRAFFIC_OR_STRUCTURAL_REVIEW_UNRESOLVED
    ),
    ReviewBriefTopic.ECOLOGICAL_FIELD_RISK: (
        ReviewBriefReasonFacet.ECOLOGICAL_FIELD_RISK_UNRESOLVED
    ),
    ReviewBriefTopic.MODEL_FITNESS_BASIS: (ReviewBriefReasonFacet.MODEL_FITNESS_BASIS_INCOMPLETE),
    ReviewBriefTopic.DIRECT_RUN_QA: ReviewBriefReasonFacet.DIRECT_RUN_QA_NOT_ACCEPTED,
    ReviewBriefTopic.ECOLOGICAL_ASSUMPTION: (
        ReviewBriefReasonFacet.ECOLOGICAL_ASSUMPTION_UNRESOLVED
    ),
    ReviewBriefTopic.COST_BASIS: ReviewBriefReasonFacet.COST_RULE_EXCEEDED,
    ReviewBriefTopic.REFERENCE_COMPARISON: (
        ReviewBriefReasonFacet.REFERENCE_COMPARISON_NOT_ACCEPTED
    ),
    ReviewBriefTopic.DATA_AUTHORIZATION: (ReviewBriefReasonFacet.DATA_AUTHORIZATION_NOT_VERIFIED),
}

_TOPIC_REASON_FACETS: Mapping[ReviewBriefTopic, frozenset[ReviewBriefReasonFacet]] = {
    topic: frozenset({reason}) for topic, reason in _DEFAULT_REASON_BY_TOPIC.items()
}
_TOPIC_REASON_FACETS = {
    **_TOPIC_REASON_FACETS,
    ReviewBriefTopic.ECOLOGICAL_ASSUMPTION: frozenset(
        {
            ReviewBriefReasonFacet.ECOLOGICAL_ASSUMPTION_UNRESOLVED,
            ReviewBriefReasonFacet.HYPOTHESIS_SENSITIVITY,
        }
    ),
}


def _evidence_class_for_id(
    *,
    topic: ReviewBriefTopic,
    evidence_id: str,
    has_direct_runs: bool,
) -> ReviewBriefEvidenceClass:
    if evidence_id.startswith(RIDGEWAY_COST_EVIDENCE_PREFIX):
        return ReviewBriefEvidenceClass.RETAINED_COST_BASIS
    if evidence_id.startswith(RIDGEWAY_HYPOTHESIS_EVIDENCE_PREFIX):
        return ReviewBriefEvidenceClass.RETAINED_HYPOTHESIS_COMPARISON
    if topic == ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK:
        return ReviewBriefEvidenceClass.DIRECT_CURRENT_DIAGNOSTIC
    if topic == ReviewBriefTopic.COST_BASIS:
        return ReviewBriefEvidenceClass.RETAINED_COST_BASIS
    if topic in {
        ReviewBriefTopic.MODEL_FITNESS_BASIS,
        ReviewBriefTopic.ECOLOGICAL_ASSUMPTION,
    }:
        return ReviewBriefEvidenceClass.RETAINED_MODEL_FITNESS
    if topic in {
        ReviewBriefTopic.SURVEY_OR_FIELD_DATA,
        ReviewBriefTopic.GENERAL_FEASIBILITY,
        ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP,
        ReviewBriefTopic.MAINTENANCE_RESPONSIBILITY,
        ReviewBriefTopic.CULTURAL_REVIEW,
        ReviewBriefTopic.PERMITTING_REVIEW,
        ReviewBriefTopic.TRAFFIC_OR_STRUCTURAL_FEASIBILITY,
        ReviewBriefTopic.ECOLOGICAL_FIELD_RISK,
    }:
        return ReviewBriefEvidenceClass.RETAINED_FEASIBILITY
    if topic == ReviewBriefTopic.REFERENCE_COMPARISON:
        return ReviewBriefEvidenceClass.RETAINED_REFERENCE_COMPARISON
    if topic == ReviewBriefTopic.DATA_AUTHORIZATION:
        return ReviewBriefEvidenceClass.RETAINED_DATA_AUTHORIZATION
    if has_direct_runs:
        return ReviewBriefEvidenceClass.DIRECT_CIRCUITSCAPE_RESULT
    return ReviewBriefEvidenceClass.RETAINED_DECISION_RULE


def _finding_evidence_classes(
    *,
    topic: ReviewBriefTopic,
    evidence_ids: Sequence[str],
    direct_run_ids: Sequence[str],
) -> tuple[ReviewBriefEvidenceClass, ...]:
    return tuple(
        dict.fromkeys(
            _evidence_class_for_id(
                topic=topic,
                evidence_id=evidence_id,
                has_direct_runs=bool(direct_run_ids),
            )
            for evidence_id in evidence_ids
        )
    )


_TOPIC_ACTIONS: Mapping[ReviewBriefTopic, tuple[ReviewBriefActionKind, ...]] = {
    ReviewBriefTopic.REQUIRED_RULE: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.REVISE_MODEL_AND_RERUN,
    ),
    ReviewBriefTopic.SURVEY_OR_FIELD_DATA: (ReviewBriefActionKind.FIELD_VERIFY,),
    ReviewBriefTopic.SAFEGUARD_REVIEW: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
    ),
    ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.FIELD_VERIFY,
    ),
    ReviewBriefTopic.GENERAL_FEASIBILITY: (
        ReviewBriefActionKind.FIELD_VERIFY,
        ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
    ),
    ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP: (
        ReviewBriefActionKind.FIELD_VERIFY,
        ReviewBriefActionKind.RESOLVE_LAND_OR_OWNERSHIP,
    ),
    ReviewBriefTopic.MAINTENANCE_RESPONSIBILITY: (
        ReviewBriefActionKind.FIELD_VERIFY,
        ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
    ),
    ReviewBriefTopic.CULTURAL_REVIEW: (ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,),
    ReviewBriefTopic.PERMITTING_REVIEW: (ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,),
    ReviewBriefTopic.TRAFFIC_OR_STRUCTURAL_FEASIBILITY: (
        ReviewBriefActionKind.FIELD_VERIFY,
        ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
    ),
    ReviewBriefTopic.ECOLOGICAL_FIELD_RISK: (
        ReviewBriefActionKind.FIELD_VERIFY,
        ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
    ),
    ReviewBriefTopic.MODEL_FITNESS_BASIS: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.REVISE_MODEL_AND_RERUN,
    ),
    ReviewBriefTopic.DIRECT_RUN_QA: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.REVISE_MODEL_AND_RERUN,
    ),
    ReviewBriefTopic.ECOLOGICAL_ASSUMPTION: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.OBTAIN_QUALIFIED_REVIEW,
    ),
    ReviewBriefTopic.COST_BASIS: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.COMPLETE_COST_BASIS,
    ),
    ReviewBriefTopic.REFERENCE_COMPARISON: (ReviewBriefActionKind.INSPECT_EVIDENCE,),
    ReviewBriefTopic.DATA_AUTHORIZATION: (
        ReviewBriefActionKind.INSPECT_EVIDENCE,
        ReviewBriefActionKind.RESOLVE_AUTHORIZATION,
    ),
}

_TOPIC_DISCIPLINES: Mapping[ReviewBriefTopic, tuple[ReviewerDiscipline, ...]] = {
    ReviewBriefTopic.REQUIRED_RULE: (
        ReviewerDiscipline.ECOLOGY,
        ReviewerDiscipline.ENGINEERING,
    ),
    ReviewBriefTopic.SURVEY_OR_FIELD_DATA: (
        ReviewerDiscipline.ECOLOGY,
        ReviewerDiscipline.FIELD,
    ),
    ReviewBriefTopic.SAFEGUARD_REVIEW: (ReviewerDiscipline.ECOLOGY,),
    ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK: (
        ReviewerDiscipline.ECOLOGY,
        ReviewerDiscipline.FIELD,
        ReviewerDiscipline.ENGINEERING,
    ),
    ReviewBriefTopic.GENERAL_FEASIBILITY: (
        ReviewerDiscipline.FIELD,
        ReviewerDiscipline.ENGINEERING,
        ReviewerDiscipline.LAND,
        ReviewerDiscipline.MAINTENANCE,
        ReviewerDiscipline.CULTURAL,
        ReviewerDiscipline.PERMITTING,
    ),
    ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP: (
        ReviewerDiscipline.FIELD,
        ReviewerDiscipline.LAND,
    ),
    ReviewBriefTopic.MAINTENANCE_RESPONSIBILITY: (
        ReviewerDiscipline.ENGINEERING,
        ReviewerDiscipline.MAINTENANCE,
    ),
    ReviewBriefTopic.CULTURAL_REVIEW: (ReviewerDiscipline.CULTURAL,),
    ReviewBriefTopic.PERMITTING_REVIEW: (ReviewerDiscipline.PERMITTING,),
    ReviewBriefTopic.TRAFFIC_OR_STRUCTURAL_FEASIBILITY: (
        ReviewerDiscipline.FIELD,
        ReviewerDiscipline.ENGINEERING,
    ),
    ReviewBriefTopic.ECOLOGICAL_FIELD_RISK: (
        ReviewerDiscipline.ECOLOGY,
        ReviewerDiscipline.FIELD,
    ),
    ReviewBriefTopic.MODEL_FITNESS_BASIS: (ReviewerDiscipline.ECOLOGY,),
    ReviewBriefTopic.DIRECT_RUN_QA: (
        ReviewerDiscipline.ECOLOGY,
        ReviewerDiscipline.ENGINEERING,
    ),
    ReviewBriefTopic.ECOLOGICAL_ASSUMPTION: (ReviewerDiscipline.ECOLOGY,),
    ReviewBriefTopic.COST_BASIS: (
        ReviewerDiscipline.COST,
        ReviewerDiscipline.MAINTENANCE,
    ),
    ReviewBriefTopic.REFERENCE_COMPARISON: (ReviewerDiscipline.ECOLOGY,),
    ReviewBriefTopic.DATA_AUTHORIZATION: (
        ReviewerDiscipline.LAND,
        ReviewerDiscipline.CULTURAL,
        ReviewerDiscipline.PERMITTING,
    ),
}

_TOPIC_PRIMARY_DISCIPLINES: Mapping[ReviewBriefTopic, tuple[ReviewerDiscipline, ...]] = {
    **_TOPIC_DISCIPLINES,
    ReviewBriefTopic.SURVEY_OR_FIELD_DATA: (ReviewerDiscipline.FIELD,),
    ReviewBriefTopic.GENERAL_FEASIBILITY: (
        ReviewerDiscipline.FIELD,
        ReviewerDiscipline.ENGINEERING,
    ),
    ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP: (ReviewerDiscipline.LAND,),
    ReviewBriefTopic.MAINTENANCE_RESPONSIBILITY: (ReviewerDiscipline.MAINTENANCE,),
    ReviewBriefTopic.TRAFFIC_OR_STRUCTURAL_FEASIBILITY: (ReviewerDiscipline.ENGINEERING,),
    ReviewBriefTopic.ECOLOGICAL_FIELD_RISK: (ReviewerDiscipline.ECOLOGY,),
    ReviewBriefTopic.DIRECT_RUN_QA: (ReviewerDiscipline.ENGINEERING,),
    ReviewBriefTopic.COST_BASIS: (ReviewerDiscipline.COST,),
}


def topic_permissions(
    kind: ReviewBriefFindingKind,
    topic: ReviewBriefTopic,
) -> tuple[tuple[ReviewBriefActionKind, ...], tuple[ReviewerDiscipline, ...]]:
    """Return the safe intersection of finding-kind and topical permissions."""

    actions = tuple(value for value in _KIND_ACTIONS[kind] if value in _TOPIC_ACTIONS[topic])
    disciplines = tuple(
        value for value in _KIND_DISCIPLINES[kind] if value in _TOPIC_DISCIPLINES[topic]
    )
    if not actions or not disciplines:
        raise ReviewBriefSafetyError("REVIEW_BRIEF_TOPIC_KIND_INCOMPATIBLE")
    return actions, disciplines


def primary_disciplines_for_topic(
    topic: ReviewBriefTopic,
) -> tuple[ReviewerDiscipline, ...]:
    """Return the disciplines that make a selection topically meaningful."""

    return _TOPIC_PRIMARY_DISCIPLINES[ReviewBriefTopic(topic)]


_OPTION_CONSTRAINT_TOPICS = frozenset(
    {
        ReviewBriefTopic.SURVEY_OR_FIELD_DATA,
        ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK,
        ReviewBriefTopic.GENERAL_FEASIBILITY,
        ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP,
        ReviewBriefTopic.MAINTENANCE_RESPONSIBILITY,
        ReviewBriefTopic.CULTURAL_REVIEW,
        ReviewBriefTopic.PERMITTING_REVIEW,
        ReviewBriefTopic.TRAFFIC_OR_STRUCTURAL_FEASIBILITY,
        ReviewBriefTopic.ECOLOGICAL_FIELD_RISK,
        ReviewBriefTopic.COST_BASIS,
        ReviewBriefTopic.DATA_AUTHORIZATION,
    }
)
_MODEL_FITNESS_TOPICS = frozenset(
    {
        ReviewBriefTopic.MODEL_FITNESS_BASIS,
        ReviewBriefTopic.DIRECT_RUN_QA,
        ReviewBriefTopic.ECOLOGICAL_ASSUMPTION,
    }
)


def required_review_brief_tools(
    snapshot: ReviewBriefAuthorizedSnapshot,
) -> tuple[str, ...]:
    """Exact bounded traversal required before one agent selection is accepted."""

    topics = {item.review_topic for item in snapshot.findings}
    ordered = [
        "review_brief_get_scope",
        "review_brief_list_findings",
        "review_brief_get_findings",
    ]
    if topics.intersection(_OPTION_CONSTRAINT_TOPICS):
        ordered.append("review_brief_list_option_constraints")
    if topics.intersection(_MODEL_FITNESS_TOPICS):
        ordered.append("review_brief_list_model_fitness_gaps")
    if ReviewBriefTopic.REFERENCE_COMPARISON in topics:
        ordered.append("review_brief_get_reference_status")
    if ReviewBriefTopic.DATA_AUTHORIZATION in topics:
        ordered.append("review_brief_get_authorization_status")
    ordered.append("review_brief_resolve_evidence_aliases")
    if len(ordered) > REVIEW_BRIEF_MAX_REQUIRED_TOOL_CALLS:
        raise ReviewBriefSafetyError("REVIEW_BRIEF_REQUIRED_TRAVERSAL_EXCEEDS_TURN_LIMIT")
    return tuple(ordered)


def validate_selection_appropriateness(
    *,
    context: ReviewBriefFindingContext,
    selection: ReviewBriefFindingSelection,
) -> None:
    """Reject a syntactically allowed choice that does not fit the closed topic."""

    topic = context.review_topic
    assert topic is not None
    if (
        selection.action_kind not in context.permitted_actions
        or selection.action_kind not in _TOPIC_ACTIONS[topic]
    ):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RUNNER_ACTION_INAPPROPRIATE")
    disciplines = set(selection.reviewer_disciplines)
    if (
        not disciplines.issubset(context.permitted_disciplines)
        or not disciplines.issubset(_TOPIC_DISCIPLINES[topic])
        or not disciplines.intersection(primary_disciplines_for_topic(topic))
    ):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RUNNER_DISCIPLINE_INAPPROPRIATE")


_RENDERED_REASON_FACET: Mapping[ReviewBriefReasonFacet, str] = {
    ReviewBriefReasonFacet.CONFIGURED_RULE_NOT_MET: (
        "Inspect the cited configured-rule evidence and resolve the recorded rule failure before "
        "feasibility review."
    ),
    ReviewBriefReasonFacet.SURVEY_EVIDENCE_GAP: (
        "Complete the cited field-data or survey gap before any carry-forward decision."
    ),
    ReviewBriefReasonFacet.SAFEGUARD_NONREGRESSION_NOT_MET: (
        "Inspect the cited safeguard non-regression evidence and resolve the recorded condition "
        "before feasibility review."
    ),
    ReviewBriefReasonFacet.REVIEW_MASK_THRESHOLD_TRIGGERED: (
        "Inspect the configured current-redistribution review-mask diagnostic and its cited "
        "limitations before feasibility review."
    ),
    ReviewBriefReasonFacet.FEASIBILITY_BASIS_INCOMPLETE: (
        "Complete the cited feasibility basis with the indicated qualified reviewers before a "
        "carry-forward decision."
    ),
    ReviewBriefReasonFacet.LAND_ACCESS_UNRESOLVED: (
        "Resolve the cited land-access or ownership gap before field or engineering feasibility "
        "work proceeds."
    ),
    ReviewBriefReasonFacet.MAINTENANCE_RESPONSIBILITY_UNRESOLVED: (
        "Establish the cited maintenance responsibility, access, and long-term stewardship basis "
        "before feasibility review."
    ),
    ReviewBriefReasonFacet.CULTURAL_REVIEW_UNRESOLVED: (
        "Complete the cited cultural or Tribal review with the authorized qualified parties before "
        "feasibility work proceeds."
    ),
    ReviewBriefReasonFacet.PERMITTING_REVIEW_UNRESOLVED: (
        "Complete the cited permitting review and document unresolved dependencies before "
        "feasibility work proceeds."
    ),
    ReviewBriefReasonFacet.TRAFFIC_OR_STRUCTURAL_REVIEW_UNRESOLVED: (
        "Complete the cited traffic and structural feasibility review before the concept is carried "
        "forward."
    ),
    ReviewBriefReasonFacet.ECOLOGICAL_FIELD_RISK_UNRESOLVED: (
        "Investigate the cited ecological field-risk gap with ecology and field reviewers before "
        "feasibility review."
    ),
    ReviewBriefReasonFacet.MODEL_FITNESS_BASIS_INCOMPLETE: (
        "Review the cited Model Fitness Passport gap before relying on this screening result."
    ),
    ReviewBriefReasonFacet.DIRECT_RUN_QA_NOT_ACCEPTED: (
        "Inspect the cited direct-run QA finding and resolve its disposition before using the "
        "associated result."
    ),
    ReviewBriefReasonFacet.ECOLOGICAL_ASSUMPTION_UNRESOLVED: (
        "Review the cited ecological modeling assumption and document its treatment before "
        "feasibility review."
    ),
    ReviewBriefReasonFacet.HYPOTHESIS_SENSITIVITY: (
        "Compare the cited within-scenario results across the retained resistance hypotheses and "
        "document how that sensitivity affects feasibility review."
    ),
    ReviewBriefReasonFacet.COST_RULE_EXCEEDED: (
        "Review the cited screening cost rule, basis, assumptions, and price vintage before any "
        "carry-forward decision."
    ),
    ReviewBriefReasonFacet.REFERENCE_COMPARISON_NOT_ACCEPTED: (
        "Inspect the cited reference comparison and resolve its stated tolerances or limitations "
        "before relying on the associated result."
    ),
    ReviewBriefReasonFacet.DATA_AUTHORIZATION_NOT_VERIFIED: (
        "Resolve the cited data authorization for the intended destination before further cloud "
        "use or export."
    ),
}

_RENDERED_FACET_PRECEDENCE = (
    ReviewBriefReasonFacet.HYPOTHESIS_SENSITIVITY,
    *(
        facet
        for facet in ReviewBriefReasonFacet
        if facet != ReviewBriefReasonFacet.HYPOTHESIS_SENSITIVITY
    ),
)


def _allowed_rendered_findings(topic: ReviewBriefTopic) -> frozenset[str]:
    return frozenset(_RENDERED_REASON_FACET[facet] for facet in _TOPIC_REASON_FACETS[topic])


def _render_finding_instruction(
    topic: ReviewBriefTopic,
    reason_facets: Sequence[ReviewBriefReasonFacet],
) -> str:
    """Render one issue-specific instruction from the closed local reason vocabulary."""

    allowed = _TOPIC_REASON_FACETS[topic]
    selected = next(
        (facet for facet in _RENDERED_FACET_PRECEDENCE if facet in reason_facets),
        None,
    )
    if selected is None or selected not in allowed:
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RENDER_REASON_FACET_INVALID")
    return _RENDERED_REASON_FACET[selected]


def build_deterministic_review_items(
    source: ReviewBriefSource,
) -> list[ReviewBriefDeterministicItem]:
    """Render the production-default checklist without a model or cloud call."""

    items: list[ReviewBriefDeterministicItem] = []
    for finding in source.findings:
        topic = finding.review_topic
        if topic is None:  # the contract normalizes this, retain an explicit guard
            raise ReviewBriefSafetyError("REVIEW_BRIEF_TOPIC_REQUIRED")
        primary = [
            discipline
            for discipline in finding.permitted_disciplines
            if discipline in primary_disciplines_for_topic(topic)
        ]
        if not primary:
            raise ReviewBriefSafetyError("REVIEW_BRIEF_PRIMARY_DISCIPLINE_REQUIRED")
        action = finding.permitted_actions[0]
        item_id = (
            "review_default_"
            + canonical_sha256(
                {
                    "finding_id": finding.finding_id,
                    "action_kind": action.value,
                    "reviewer_disciplines": [item.value for item in primary],
                    "decision_board_sha256": source.decision_board_sha256,
                }
            )[:24]
        )
        items.append(
            ReviewBriefDeterministicItem(
                item_id=item_id,
                finding_kind=finding.finding_kind,
                review_topic=topic,
                priority=finding.priority,
                action_kind=action,
                reviewer_disciplines=primary,
                evidence_claim_ids=list(finding.evidence_claim_ids),
                direct_run_ids=list(finding.direct_run_ids),
                scenario_ids=list(finding.scenario_ids),
                rendered_text=_render_finding_instruction(topic, finding.reason_facets),
            )
        )
    return items


def _aggregate_key(claim: ConnectivityEvidenceClaim) -> str | None:
    for node in claim.nodes:
        if node.node_type == "REDUCER":
            value = node.attributes.get("aggregate_key")
            if isinstance(value, str):
                return value
    return None


def _claim_is_failure(claim: ConnectivityEvidenceClaim) -> bool:
    if type(claim.value) is bool:
        return claim.value is False
    if isinstance(claim.value, str):
        return claim.value.upper() in {
            "BLOCKED",
            "FAIL",
            "FAILED",
            "INCOMPLETE",
            "NOT_ACCEPTED",
            "REJECTED",
            "REVIEW_REQUIRED",
            "SURVEY_FIRST",
        }
    return False


def _rule_finding(metric: str) -> tuple[ReviewBriefFindingKind, ReviewBriefTopic]:
    if metric == "REQUIRE_SAFEGUARD_NONREGRESSION":
        return ReviewBriefFindingKind.SAFEGUARD, ReviewBriefTopic.SAFEGUARD_REVIEW
    if metric == "REQUIRE_ACCEPTED_QA":
        return ReviewBriefFindingKind.QA, ReviewBriefTopic.DIRECT_RUN_QA
    if metric == "MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT":
        return (
            ReviewBriefFindingKind.REVIEW_MASK,
            ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK,
        )
    if metric == "MAX_COST":
        return ReviewBriefFindingKind.COST, ReviewBriefTopic.COST_BASIS
    return ReviewBriefFindingKind.RULE_FAILURE, ReviewBriefTopic.REQUIRED_RULE


def _contains_bounded_phrase(text: str, phrase: str) -> bool:
    escaped = re.escape(phrase.casefold()).replace(r"\ ", r"[\s-]+")
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text.casefold()) is not None


def _feasibility_topics(text: str) -> tuple[ReviewBriefTopic, ...]:
    topics: list[ReviewBriefTopic] = []
    token_topics = (
        (
            ("land", "ownership", "access", "right-of-way"),
            ReviewBriefTopic.LAND_ACCESS_OR_OWNERSHIP,
        ),
        (("maintenance", "maintain"), ReviewBriefTopic.MAINTENANCE_RESPONSIBILITY),
        (("cultural", "tribal", "heritage"), ReviewBriefTopic.CULTURAL_REVIEW),
        (("permit", "permits", "permitting", "regulatory"), ReviewBriefTopic.PERMITTING_REVIEW),
        (
            ("traffic", "structural", "constructability", "construction"),
            ReviewBriefTopic.TRAFFIC_OR_STRUCTURAL_FEASIBILITY,
        ),
        (
            (
                "invasive",
                "predator",
                "predation",
                "disease",
                "habitat",
                "ecological",
            ),
            ReviewBriefTopic.ECOLOGICAL_FIELD_RISK,
        ),
        (
            ("survey", "field", "unknown", "uncertain", "uncertainty", "data gap"),
            ReviewBriefTopic.SURVEY_OR_FIELD_DATA,
        ),
    )
    for tokens, topic in token_topics:
        if any(_contains_bounded_phrase(text, token) for token in tokens):
            topics.append(topic)
    return tuple(dict.fromkeys(topics)) or (ReviewBriefTopic.GENERAL_FEASIBILITY,)


def _local_finding(
    *,
    identity_prefix: str,
    kind: ReviewBriefFindingKind,
    topic: ReviewBriefTopic,
    evidence_claim_ids: Sequence[str],
    direct_run_ids: Sequence[str],
    scenario_ids: Sequence[str],
    reason_facets: Sequence[ReviewBriefReasonFacet] = (),
) -> ReviewBriefLocalFinding:
    evidence = sorted(set(evidence_claim_ids))
    runs = sorted(set(direct_run_ids))
    scenarios = sorted(set(scenario_ids))
    reasons = list(dict.fromkeys(reason_facets)) or [_DEFAULT_REASON_BY_TOPIC[topic]]
    actions, disciplines = topic_permissions(kind, topic)
    return ReviewBriefLocalFinding(
        finding_id=(
            f"{identity_prefix}_"
            + canonical_sha256(
                {
                    "kind": kind.value,
                    "topic": topic.value,
                    "reason_facets": [item.value for item in reasons],
                    "evidence": evidence,
                    "runs": runs,
                    "scenarios": scenarios,
                }
            )[:24]
        ),
        finding_kind=kind,
        review_topic=topic,
        reason_facets=reasons,
        priority=_priority(kind),
        evidence_claim_ids=evidence,
        direct_run_ids=runs,
        scenario_ids=scenarios,
        permitted_actions=list(actions),
        permitted_disciplines=list(disciplines),
    )


def _grouped_claim_findings(
    groups: Mapping[
        tuple[ReviewBriefFindingKind, ReviewBriefTopic],
        Sequence[ConnectivityEvidenceClaim],
    ],
) -> list[ReviewBriefLocalFinding]:
    findings: list[ReviewBriefLocalFinding] = []
    for (kind, topic), claims in sorted(
        groups.items(), key=lambda item: (item[0][0].value, item[0][1].value)
    ):
        unique = {item.claim_id: item for item in claims}
        ordered = [unique[key] for key in sorted(unique)]
        findings.append(
            _local_finding(
                identity_prefix="finding",
                kind=kind,
                topic=topic,
                evidence_claim_ids=[item.claim_id for item in ordered],
                direct_run_ids=[run_id for item in ordered for run_id in item.direct_run_ids],
                scenario_ids=[item.scenario_id for item in ordered if item.scenario_id is not None],
            )
        )
    return findings


def _priority(kind: ReviewBriefFindingKind) -> ReviewBriefPriority:
    if kind in {
        ReviewBriefFindingKind.RULE_FAILURE,
        ReviewBriefFindingKind.SURVEY_FIRST,
        ReviewBriefFindingKind.AUTHORIZATION,
    }:
        return ReviewBriefPriority.BLOCKING
    if kind == ReviewBriefFindingKind.ASSUMPTION:
        return ReviewBriefPriority.FOLLOW_UP
    return ReviewBriefPriority.BEFORE_FEASIBILITY


def professional_review_source(
    *,
    bundle: ConnectivityProjectBundle,
    plan: ConnectivityRunPlan,
    summary: PortfolioDecisionSummary,
    claims: Sequence[ConnectivityEvidenceClaim],
    active_model_revision_id: str,
    verified_direct_run_sha256: Mapping[str, str],
    authorization_sha256: str,
    evidence_authorization_decision_id: str,
    current_authorizations_sha256: str,
) -> ReviewBriefSource:
    selected = sorted(
        (item for item in claims if item.portfolio_id == summary.portfolio_id),
        key=lambda item: item.claim_id,
    )
    direct_claims = [item for item in selected if item.direct_run_ids]
    if not direct_claims:
        raise ReviewBriefSafetyError("REVIEW_BRIEF_DIRECT_EVIDENCE_REQUIRED")
    verified_ids = set(verified_direct_run_sha256)
    cited_run_ids = {
        direct_run_id for item in direct_claims for direct_run_id in item.direct_run_ids
    }
    if (
        not summary.direct_run_ids
        or not set(summary.direct_run_ids).issubset(verified_ids)
        or not cited_run_ids
        or not cited_run_ids.issubset(verified_ids)
    ):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_DIRECT_RUN_VERIFICATION_REQUIRED")
    claim_by_id = {item.claim_id: item for item in selected}
    rule_by_id = {item.rule_id: item for item in bundle.decision_rules}
    groups: dict[
        tuple[ReviewBriefFindingKind, ReviewBriefTopic],
        list[ConnectivityEvidenceClaim],
    ] = {}

    def add(
        kind: ReviewBriefFindingKind,
        topic: ReviewBriefTopic,
        claim: ConnectivityEvidenceClaim,
    ) -> None:
        groups.setdefault((kind, topic), []).append(claim)

    # Rule dispositions are the authoritative pass/fail surface. A normal
    # direct metric is never promoted merely because its evidence graph names
    # an assumption or authorization record.
    for disposition in summary.rule_dispositions:
        if disposition.passed or disposition.evidence_claim_id is None:
            continue
        claim = claim_by_id.get(disposition.evidence_claim_id)
        rule = rule_by_id.get(disposition.rule_id)
        if claim is None or rule is None:
            continue
        kind, topic = _rule_finding(rule.metric.value)
        add(kind, topic, claim)

    aggregate_by_key = {
        key: item
        for item in selected
        if item.claim_kind == "AGGREGATE_RULE" and (key := _aggregate_key(item)) is not None
    }
    review_aggregate = aggregate_by_key.get("review_mask_trigger")
    if (
        summary.review_mask_triggered
        and review_aggregate is not None
        and bool(review_aggregate.value)
    ):
        add(
            ReviewBriefFindingKind.REVIEW_MASK,
            ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK,
            review_aggregate,
        )
    safeguard_aggregate = aggregate_by_key.get("safeguard_nonregression")
    if summary.safeguard_passed is False and safeguard_aggregate is not None:
        add(
            ReviewBriefFindingKind.SAFEGUARD,
            ReviewBriefTopic.SAFEGUARD_REVIEW,
            safeguard_aggregate,
        )

    for claim in selected:
        if claim.claim_kind == "DIRECT_QA" and _claim_is_failure(claim):
            add(ReviewBriefFindingKind.QA, ReviewBriefTopic.DIRECT_RUN_QA, claim)
        elif claim.claim_kind == "DIRECT_REFERENCE_COMPARISON" and _claim_is_failure(claim):
            add(
                ReviewBriefFindingKind.REFERENCE,
                ReviewBriefTopic.REFERENCE_COMPARISON,
                claim,
            )
        elif claim.claim_kind == "FEASIBILITY":
            for topic in _feasibility_topics(claim.text):
                kind = (
                    ReviewBriefFindingKind.SURVEY_FIRST
                    if topic == ReviewBriefTopic.SURVEY_OR_FIELD_DATA
                    else ReviewBriefFindingKind.FEASIBILITY_GAP
                )
                add(kind, topic, claim)

    disposition_claim = (
        claim_by_id.get(summary.disposition_evidence_claim_id)
        if summary.disposition_evidence_claim_id
        else None
    )
    fitness_findings: list[ReviewBriefLocalFinding] = []
    unresolved_dimension = next(
        item for item in bundle.fitness.dimensions if item.key == "unresolved_assumptions"
    )
    if bundle.fitness.unresolved_assumptions or unresolved_dimension.status != "ACCEPTED":
        if not unresolved_dimension.evidence_ids:
            raise ReviewBriefSafetyError("REVIEW_BRIEF_MODEL_FITNESS_EVIDENCE_REQUIRED")
        fitness_findings.append(
            _local_finding(
                identity_prefix="fitness",
                kind=ReviewBriefFindingKind.ASSUMPTION,
                topic=ReviewBriefTopic.ECOLOGICAL_ASSUMPTION,
                evidence_claim_ids=unresolved_dimension.evidence_ids,
                direct_run_ids=(),
                scenario_ids=(),
            )
        )
    review_dimensions = [
        item
        for item in bundle.fitness.dimensions
        if item.key != "unresolved_assumptions" and item.status != "ACCEPTED"
    ]
    if review_dimensions:
        fitness_evidence_ids = sorted(
            {evidence_id for item in review_dimensions for evidence_id in item.evidence_ids}
        )
        if not fitness_evidence_ids:
            raise ReviewBriefSafetyError("REVIEW_BRIEF_MODEL_FITNESS_EVIDENCE_REQUIRED")
        fitness_findings.append(
            _local_finding(
                identity_prefix="fitness",
                kind=ReviewBriefFindingKind.MODEL_FITNESS,
                topic=ReviewBriefTopic.MODEL_FITNESS_BASIS,
                evidence_claim_ids=fitness_evidence_ids,
                direct_run_ids=(),
                scenario_ids=(),
            )
        )
    if (
        not groups
        and not fitness_findings
        and summary.disposition.value != "PASSES"
        and disposition_claim is not None
    ):
        fallback_kind = (
            ReviewBriefFindingKind.QA
            if summary.disposition.value == "INCOMPLETE"
            else ReviewBriefFindingKind.SURVEY_FIRST
            if summary.disposition.value == "SURVEY_FIRST"
            else ReviewBriefFindingKind.RULE_FAILURE
        )
        fallback_topic = _DEFAULT_TOPIC_BY_KIND[fallback_kind]
        add(fallback_kind, fallback_topic, disposition_claim)
    findings = [*_grouped_claim_findings(groups), *fitness_findings]
    board_identity = {
        "analysis_id": plan.analysis_id,
        "portfolio_summary": summary.model_dump(mode="json"),
        "claims": [item.model_dump(mode="json") for item in selected],
        "model_fitness": bundle.fitness.model_dump(mode="json"),
        "run_plan": plan.model_dump(mode="json"),
        "analysis_model_revision_id": bundle.revision.revision_id,
        "active_model_revision_id": active_model_revision_id,
        "verified_direct_runs": dict(sorted(verified_direct_run_sha256.items())),
        "authorization_sha256": authorization_sha256,
    }
    return ReviewBriefSource(
        source_mode=ReviewBriefSourceMode.PROFESSIONAL,
        project_id=bundle.project_id,
        project_title=bundle.title,
        analysis_id=plan.analysis_id,
        portfolio_id=summary.portfolio_id,
        model_revision_id=active_model_revision_id,
        decision_board_sha256=canonical_sha256(board_identity),
        scenario_ids=plan.scenario_ids,
        disposition=summary.disposition.value,
        next_action=summary.next_action,
        controlling_scenario_id=summary.controlling_scenario_id,
        safeguard_status=(
            "PASS"
            if summary.safeguard_passed is True
            else "FAIL"
            if summary.safeguard_passed is False
            else "REVIEW"
        ),
        review_mask_triggered=summary.review_mask_triggered,
        authorization_sha256=authorization_sha256,
        evidence_authorization_decision_id=evidence_authorization_decision_id,
        current_authorizations_sha256=current_authorizations_sha256,
        findings=findings,
    )


def ridgeway_review_source(
    *,
    project_id: str,
    model_revision_id: str,
    projection: Mapping[str, Any],
    portfolio_id: str,
    evidence_manifest_sha256: str,
) -> ReviewBriefSource:
    if projection.get("releaseReplayEligible") is not True or projection.get("cc0") is not True:
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_DIRECT_EVIDENCE_REQUIRED")
    portfolios = [item for item in projection.get("portfolios", []) if isinstance(item, dict)]
    matches = [item for item in portfolios if item.get("id") == portfolio_id]
    if len(matches) != 1:
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_PORTFOLIO_NOT_FOUND")
    portfolio = matches[0]
    scenarios = {
        str(item["id"]): item
        for item in projection.get("scenarios", [])
        if isinstance(item, dict) and item.get("id")
    }
    # Ridgeway is a pinned teaching fixture. Read the reviewed mask threshold from
    # its domain contract instead of reverse-engineering it from display values.
    from .ridgeway import build_ridgeway_fixture

    fixture = build_ridgeway_fixture()
    if len(fixture.bundle.review_masks) != 1:
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_REVIEW_MASK_INVALID")
    review_trigger_percent = fixture.bundle.review_masks[0].review_trigger_percent
    scenario_records: list[dict[str, Any]] = []
    for scenario_result in portfolio.get("scenarioResults", []):
        if not isinstance(scenario_result, dict):
            continue
        pair_results = [
            item for item in scenario_result.get("pairResults", []) if isinstance(item, dict)
        ]
        evidence_ids = [str(item["claimId"]) for item in pair_results if item.get("claimId")]
        direct_run_ids = sorted(
            {
                str(value)
                for pair in pair_results
                for value in (pair.get("baselineRunId"), pair.get("portfolioRunId"))
                if value
            }
        )
        scenario_id = str(scenario_result.get("scenarioId", ""))
        if (
            scenario_id not in scenarios
            or not pair_results
            or len(evidence_ids) != len(pair_results)
            or any(type(item.get("passed")) is not bool for item in pair_results)
        ):
            raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_SCENARIO_EVIDENCE_INVALID")
        mask_change = scenario_result.get("reviewMaskChangePct")
        if (
            isinstance(mask_change, bool)
            or not isinstance(mask_change, (int, float))
            or not math.isfinite(float(mask_change))
        ):
            raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_MAP_EVIDENCE_INVALID")
        map_run_ids = [
            str(value)
            for value in (
                scenario_result.get("baselineMapRunId"),
                scenario_result.get("portfolioMapRunId"),
            )
            if value
        ]
        if len(map_run_ids) != 2:
            raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_MAP_EVIDENCE_INVALID")
        purpose = str(scenarios.get(scenario_id, {}).get("purpose", ""))
        scenario_records.append(
            {
                "scenario_id": scenario_id,
                "purpose": purpose,
                "status": str(scenario_result.get("ruleStatus", "")),
                "evidence_ids": evidence_ids,
                "direct_run_ids": direct_run_ids,
                "pair_passes": [bool(item["passed"]) for item in pair_results],
                "map_triggered": float(mask_change) > review_trigger_percent,
                "map_evidence_ids": [f"ECO-MAP-{portfolio_id}-{scenario_id}"],
                "map_run_ids": map_run_ids,
            }
        )
    if {str(item["scenario_id"]) for item in scenario_records} != set(scenarios):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_SCENARIO_ENSEMBLE_INCOMPLETE")

    def finding_for_records(
        *,
        kind: ReviewBriefFindingKind,
        topic: ReviewBriefTopic,
        records: Sequence[Mapping[str, Any]],
        extra_run_ids: Sequence[str] = (),
    ) -> ReviewBriefLocalFinding:
        return _local_finding(
            identity_prefix="ridgeway",
            kind=kind,
            topic=topic,
            evidence_claim_ids=[
                str(evidence_id) for record in records for evidence_id in record["evidence_ids"]
            ],
            direct_run_ids=[
                *extra_run_ids,
                *[str(run_id) for record in records for run_id in record["direct_run_ids"]],
            ],
            scenario_ids=[str(record["scenario_id"]) for record in records],
        )

    local_findings: list[ReviewBriefLocalFinding] = []
    safeguard_records = [
        item
        for item in scenario_records
        if item["purpose"] == "safeguard" and not all(item["pair_passes"])
    ]
    if safeguard_records:
        local_findings.append(
            finding_for_records(
                kind=ReviewBriefFindingKind.SAFEGUARD,
                topic=ReviewBriefTopic.SAFEGUARD_REVIEW,
                records=safeguard_records,
            )
        )
    failed_target_records = [
        item
        for item in scenario_records
        if item["purpose"] != "safeguard" and not all(item["pair_passes"])
    ]
    if failed_target_records:
        local_findings.append(
            finding_for_records(
                kind=ReviewBriefFindingKind.RULE_FAILURE,
                topic=ReviewBriefTopic.REQUIRED_RULE,
                records=failed_target_records,
            )
        )
    triggered_map_records = [item for item in scenario_records if item["map_triggered"]]
    if bool(portfolio.get("roadMaskFlag")) != bool(triggered_map_records):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_MAP_DISPOSITION_MISMATCH")
    if triggered_map_records:
        local_findings.append(
            _local_finding(
                identity_prefix="ridgeway",
                kind=ReviewBriefFindingKind.REVIEW_MASK,
                topic=ReviewBriefTopic.CURRENT_REDISTRIBUTION_MASK,
                evidence_claim_ids=[
                    str(evidence_id)
                    for record in triggered_map_records
                    for evidence_id in record["map_evidence_ids"]
                ],
                direct_run_ids=[
                    str(run_id)
                    for record in triggered_map_records
                    for run_id in record["map_run_ids"]
                ],
                scenario_ids=[str(record["scenario_id"]) for record in triggered_map_records],
            )
        )
    decision_brief = projection.get("decisionBrief")
    if not isinstance(decision_brief, Mapping):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_DECISION_BRIEF_INVALID")
    budget = decision_brief.get("budgetUsd")
    cost = portfolio.get("costUsd")
    if (
        isinstance(budget, bool)
        or isinstance(cost, bool)
        or not isinstance(budget, (int, float))
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(budget))
        or not math.isfinite(float(cost))
    ):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_COST_EVIDENCE_INVALID")
    if float(cost) > float(budget):
        local_findings.append(
            _local_finding(
                identity_prefix="ridgeway_cost",
                kind=ReviewBriefFindingKind.COST,
                topic=ReviewBriefTopic.COST_BASIS,
                evidence_claim_ids=(f"{RIDGEWAY_COST_EVIDENCE_PREFIX}{portfolio_id}",),
                direct_run_ids=(),
                scenario_ids=(),
            )
        )
    assumption_evidence_ids = [RIDGEWAY_MODEL_FITNESS_EVIDENCE_ID]
    assumption_scenario_ids: list[str] = []
    assumption_reason_facets = [ReviewBriefReasonFacet.ECOLOGICAL_ASSUMPTION_UNRESOLVED]
    if portfolio.get("projectionKey") == "single-hypothesis-collapse":
        ranks = portfolio.get("hypothesisRanks")
        if (
            not isinstance(ranks, Mapping)
            or len(ranks) < 2
            or any(
                not isinstance(value, Mapping)
                or isinstance(value.get("rank"), bool)
                or not isinstance(value.get("rank"), int)
                for value in ranks.values()
            )
        ):
            raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_HYPOTHESIS_EVIDENCE_INVALID")
        rank_values = [int(value["rank"]) for value in ranks.values()]
        if max(rank_values) == min(rank_values):
            raise ReviewBriefSafetyError("REVIEW_BRIEF_RIDGEWAY_HYPOTHESIS_EVIDENCE_INVALID")
        assumption_evidence_ids.append(f"{RIDGEWAY_HYPOTHESIS_EVIDENCE_PREFIX}{portfolio_id}")
        assumption_reason_facets.append(ReviewBriefReasonFacet.HYPOTHESIS_SENSITIVITY)
        assumption_scenario_ids.extend(
            str(record["scenario_id"])
            for record in scenario_records
            if record["purpose"] != "safeguard"
        )
    local_findings.append(
        _local_finding(
            identity_prefix="ridgeway_fitness",
            kind=ReviewBriefFindingKind.ASSUMPTION,
            topic=ReviewBriefTopic.ECOLOGICAL_ASSUMPTION,
            evidence_claim_ids=assumption_evidence_ids,
            direct_run_ids=(),
            scenario_ids=assumption_scenario_ids,
            reason_facets=assumption_reason_facets,
        )
    )
    rule_status = str(portfolio.get("ruleStatus"))
    disposition = {"pass": "PASSES", "fail": "FAILS"}.get(rule_status, "INCOMPLETE")
    safeguard_results = [
        item
        for item in portfolio.get("scenarioResults", [])
        if isinstance(item, dict)
        and str(scenarios.get(str(item.get("scenarioId")), {}).get("purpose")) == "safeguard"
    ]
    safeguard_status: Literal["PASS", "FAIL", "REVIEW"] = (
        "FAIL"
        if any(
            any(
                isinstance(pair, Mapping) and pair.get("passed") is False
                for pair in item.get("pairResults", [])
            )
            for item in safeguard_results
        )
        else "PASS"
        if safeguard_results
        and all(
            item.get("pairResults")
            and all(
                isinstance(pair, Mapping) and pair.get("passed") is True
                for pair in item.get("pairResults", [])
            )
            for item in safeguard_results
        )
        else "REVIEW"
    )
    board_identity = {
        "projection_schema": projection.get("schemaVersion"),
        "projection_manifest_sha256": projection.get("manifestSha256"),
        "evidence_manifest_sha256": evidence_manifest_sha256,
        "portfolio": portfolio,
        "model_revision_id": model_revision_id,
    }
    return ReviewBriefSource(
        source_mode=ReviewBriefSourceMode.RIDGEWAY_SYNTHETIC,
        project_id=project_id,
        project_title=str(projection.get("title") or "Ridgeway Crossing"),
        analysis_id="ridgeway-verified-replay-v1.2",
        portfolio_id=portfolio_id,
        model_revision_id=model_revision_id,
        decision_board_sha256=canonical_sha256(board_identity),
        scenario_ids=tuple(scenarios),
        disposition=disposition,
        next_action=str(portfolio.get("nextAction") or "Continue qualified review."),
        controlling_scenario_id=(
            str(portfolio["worstScenarioId"]) if portfolio.get("worstScenarioId") else None
        ),
        safeguard_status=safeguard_status,
        review_mask_triggered=bool(portfolio.get("roadMaskFlag")),
        authorization_sha256=evidence_manifest_sha256,
        evidence_authorization_decision_id=(
            "evidence_auth_"
            + canonical_sha256(
                {
                    "source_mode": "RIDGEWAY_SYNTHETIC",
                    "project_id_sha256": hashlib.sha256(project_id.encode("utf-8")).hexdigest(),
                    "evidence_manifest_sha256": evidence_manifest_sha256,
                }
            )[:24]
        ),
        current_authorizations_sha256=evidence_manifest_sha256,
        findings=local_findings,
    )


def _finding_scenario_breadth(
    *, finding_scenario_count: int, ensemble_scenario_count: int
) -> ReviewBriefScenarioBreadth:
    if finding_scenario_count == 0:
        return ReviewBriefScenarioBreadth.PROJECT_WIDE
    if finding_scenario_count == 1:
        return ReviewBriefScenarioBreadth.SINGLE_SCENARIO
    if finding_scenario_count == ensemble_scenario_count:
        return ReviewBriefScenarioBreadth.COMPLETE_ENSEMBLE
    return ReviewBriefScenarioBreadth.SCENARIO_SUBSET


def build_authorized_snapshot(source: ReviewBriefSource) -> ReviewBriefAuthorizedSnapshot:
    evidence_alias_by_id = {
        evidence_id: f"E{index}"
        for index, evidence_id in enumerate(source.evidence_claim_ids, start=1)
    }
    scenario_ids = tuple(dict.fromkeys(source.scenario_ids))
    scenario_alias_by_id = {
        scenario_id: f"S{index}" for index, scenario_id in enumerate(scenario_ids, start=1)
    }
    finding_contexts = [
        ReviewBriefFindingContext(
            finding_alias=f"F{index}",
            finding_kind=item.finding_kind,
            review_topic=item.review_topic,
            reason_facets=item.reason_facets,
            evidence_classes=item.evidence_classes,
            priority=item.priority,
            evidence_aliases=[evidence_alias_by_id[value] for value in item.evidence_claim_ids],
            evidence_branches=[
                ReviewBriefEvidenceBranch(
                    evidence_alias=evidence_alias_by_id[evidence_id],
                    evidence_class=_evidence_class_for_id(
                        topic=item.review_topic,
                        evidence_id=evidence_id,
                        has_direct_runs=bool(item.direct_run_ids),
                    ),
                )
                for evidence_id in item.evidence_claim_ids
            ],
            scenario_aliases=[scenario_alias_by_id[value] for value in item.scenario_ids],
            scenario_breadth=_finding_scenario_breadth(
                finding_scenario_count=len(item.scenario_ids),
                ensemble_scenario_count=len(scenario_ids),
            ),
            direct_run_count=len(item.direct_run_ids),
            permitted_actions=item.permitted_actions,
            permitted_disciplines=item.permitted_disciplines,
        )
        for index, item in enumerate(source.findings, start=1)
    ]
    snapshot = ReviewBriefAuthorizedSnapshot.create(
        source_mode=source.source_mode,
        disposition=source.disposition,
        analysis_id_sha256=hashlib.sha256(source.analysis_id.encode("utf-8")).hexdigest(),
        model_revision_id_sha256=hashlib.sha256(
            source.model_revision_id.encode("utf-8")
        ).hexdigest(),
        evidence_authorization_decision_id=source.evidence_authorization_decision_id,
        current_authorizations_sha256=source.current_authorizations_sha256,
        authorization_sha256=source.authorization_sha256,
        findings=finding_contexts,
    )
    provider_strings = set(_iter_strings(snapshot.model_dump(mode="json")))
    forbidden = {
        source.project_id,
        source.project_title,
        source.analysis_id,
        source.portfolio_id,
        source.model_revision_id,
        *source.scenario_ids,
        *source.evidence_claim_ids,
        *(item.finding_id for item in source.findings),
    }
    serialized = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True)
    if _LOCAL_PATH.search(serialized) or any(
        item and item in provider_strings for item in forbidden
    ):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_ALIAS_ONLY_BOUNDARY_FAILED")
    return snapshot


def _iter_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(nested for item in value.values() for nested in _iter_strings(item))
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(nested for item in value for nested in _iter_strings(item))
    return ()


def runner_request(source: ReviewBriefSource) -> ReviewBriefRunnerRequest:
    return ReviewBriefRunnerRequest(snapshot=build_authorized_snapshot(source))


def validate_runner_result(
    result: ReviewBriefRunnerResult,
    snapshot: ReviewBriefAuthorizedSnapshot,
) -> None:
    if result.snapshot_sha256 != snapshot.snapshot_sha256:
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RUNNER_SNAPSHOT_MISMATCH")
    if tuple(result.tool_calls) != required_review_brief_tools(snapshot):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RUNNER_REQUIRED_TRAVERSAL_MISSING")
    context_by_alias = {item.finding_alias: item for item in snapshot.findings}
    selection_by_alias = {item.finding_alias: item for item in result.selections}
    if set(context_by_alias) != set(selection_by_alias):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RUNNER_FINDING_COVERAGE_INVALID")
    for alias, selection in selection_by_alias.items():
        context = context_by_alias[alias]
        validate_selection_appropriateness(context=context, selection=selection)


def _render_items(
    *,
    source: ReviewBriefSource,
    snapshot: ReviewBriefAuthorizedSnapshot,
    result: ReviewBriefRunnerResult,
) -> list[ReviewBriefItem]:
    validate_runner_result(result, snapshot)
    local_by_alias = {
        context.finding_alias: finding
        for context, finding in zip(snapshot.findings, source.findings, strict=True)
    }
    items: list[ReviewBriefItem] = []
    for selection in result.selections:
        finding = local_by_alias[selection.finding_alias]
        item_id = (
            "review_item_"
            + canonical_sha256(
                {
                    "finding_id": finding.finding_id,
                    "action_kind": selection.action_kind.value,
                    "reviewer_disciplines": [item.value for item in selection.reviewer_disciplines],
                }
            )[:24]
        )
        items.append(
            ReviewBriefItem(
                item_id=item_id,
                finding_kind=finding.finding_kind,
                review_topic=finding.review_topic,
                priority=finding.priority,
                action_kind=selection.action_kind,
                reviewer_disciplines=selection.reviewer_disciplines,
                evidence_claim_ids=list(finding.evidence_claim_ids),
                direct_run_ids=list(finding.direct_run_ids),
                scenario_ids=list(finding.scenario_ids),
                rendered_text=_render_finding_instruction(
                    finding.review_topic,
                    finding.reason_facets,
                ),
                state="OPEN",
            )
        )
    return items


def render_review_brief(
    *,
    call_id: str,
    source: ReviewBriefSource,
    result: ReviewBriefRunnerResult,
    created_at: datetime,
    supersedes_draft_id: str | None,
) -> ReviewBriefDraft:
    snapshot = build_authorized_snapshot(source)
    items = _render_items(source=source, snapshot=snapshot, result=result)
    limitations = [
        REVIEW_BRIEF_DIRECT_QUALIFICATION,
        CONNECTIVITY_LIMITATION,
        CURRENT_MAP_LIMITATION,
        REVIEW_BRIEF_SCOPE_LIMITATION,
        REVIEW_BRIEF_FIELD_LIMITATION,
    ]
    identity = {
        "call_id": call_id,
        "decision_board_sha256": source.decision_board_sha256,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "provider_call_id_sha256": result.provider_call_id_sha256,
        "trace_id": result.trace.trace_id,
        "weave_call_id": result.trace.weave_call_id,
        "supersedes_draft_id": supersedes_draft_id,
    }
    draft = ReviewBriefDraft(
        draft_id=f"review_brief_{canonical_sha256(identity)[:24]}",
        status="UNCONFIRMED",
        source_mode=source.source_mode,
        analysis_id=source.analysis_id,
        portfolio_id=source.portfolio_id,
        model_revision_id=source.model_revision_id,
        decision_board_sha256=source.decision_board_sha256,
        created_at=created_at,
        supersedes_draft_id=supersedes_draft_id,
        disposition_snapshot=ReviewBriefDispositionSnapshot(
            disposition=source.disposition,
            next_action=source.next_action,
            controlling_scenario_id=source.controlling_scenario_id,
            safeguard_status=source.safeguard_status,
            review_mask_triggered=source.review_mask_triggered,
        ),
        items=items,
        limitations=limitations,
        trace=result.trace,
        validation=ReviewBriefValidation(),
    )
    validate_rendered_review_brief(draft, source=source, result=result)
    return draft


def validate_rendered_review_brief(
    draft: ReviewBriefDraft,
    *,
    source: ReviewBriefSource,
    result: ReviewBriefRunnerResult,
) -> None:
    snapshot = build_authorized_snapshot(source)
    expected_items = _render_items(source=source, snapshot=snapshot, result=result)
    if (
        draft.status != "UNCONFIRMED"
        or draft.source_mode != source.source_mode
        or draft.analysis_id != source.analysis_id
        or draft.portfolio_id != source.portfolio_id
        or draft.model_revision_id != source.model_revision_id
        or draft.decision_board_sha256 != source.decision_board_sha256
        or draft.items != expected_items
        or {item for row in draft.items for item in row.evidence_claim_ids}
        != set(source.evidence_claim_ids)
    ):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RENDER_NOT_DETERMINISTIC")
    serialized = json.dumps(draft.model_dump(mode="json"), sort_keys=True)
    if _LOCAL_PATH.search(serialized):
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RENDER_LOCAL_PATH")
    try:
        assert_safe_rendered_ecological_copy(draft.model_dump(mode="json"))
    except ValueError as exc:
        raise ReviewBriefSafetyError("REVIEW_BRIEF_RENDER_PROHIBITED_CLAIM") from exc


def mark_stale(draft: ReviewBriefDraft, *, current_decision_board_sha256: str) -> ReviewBriefDraft:
    return draft.model_copy(
        update={
            "status": (
                "UNCONFIRMED"
                if draft.decision_board_sha256 == current_decision_board_sha256
                else "STALE"
            )
        }
    )


def rejection_error_code(error: BaseException) -> str:
    code = str(error).strip()
    return code if _ERROR.fullmatch(code) else "REVIEW_BRIEF_PREPARE_FAILED"


# Stable public names used by the process/runtime boundary and generated schemas.
# These are aliases, not duplicate contracts, so validation cannot diverge.
AuthorizedReviewSnapshot = ReviewBriefAuthorizedSnapshot
ReviewBriefAgentOutput = ReviewBriefRunnerResult
ReviewBriefRequest = ReviewBriefRunnerRequest
ReviewBriefTraceReceipt = ReviewBriefTrace
ReviewBriefAttestation = ReviewBriefAcknowledgement


__all__ = [
    "REVIEW_BRIEF_ACKNOWLEDGEMENT",
    "REVIEW_BRIEF_AGENT_TOOLS",
    "REVIEW_BRIEF_CONTEXT_VERSION",
    "REVIEW_BRIEF_DIRECT_QUALIFICATION",
    "REVIEW_BRIEF_FIELD_LIMITATION",
    "REVIEW_BRIEF_MAX_EVIDENCE_ALIASES",
    "REVIEW_BRIEF_MAX_FINDING_ALIASES",
    "REVIEW_BRIEF_MAX_REQUIRED_TOOL_CALLS",
    "REVIEW_BRIEF_MAX_SCENARIO_ALIASES",
    "REVIEW_BRIEF_SCOPE_LIMITATION",
    "REVIEW_BRIEF_VALIDATOR_VERSION",
    "AuthorizedReviewSnapshot",
    "ReviewBriefAcknowledgement",
    "ReviewBriefActionKind",
    "ReviewBriefAgentOutput",
    "ReviewBriefAttestation",
    "ReviewBriefAuthorizedSnapshot",
    "ReviewBriefCloudBoundary",
    "ReviewBriefDeterministicItem",
    "ReviewBriefDraft",
    "ReviewBriefEvidenceBranch",
    "ReviewBriefEvidenceClass",
    "ReviewBriefFailureStage",
    "ReviewBriefFeedbackRecord",
    "ReviewBriefFeedbackTraceReceipt",
    "ReviewBriefFindingKind",
    "ReviewBriefFindingSelection",
    "ReviewBriefItem",
    "ReviewBriefLocalFinding",
    "ReviewBriefPreflight",
    "ReviewBriefPriority",
    "ReviewBriefReasonFacet",
    "ReviewBriefRejectedRunnerReceipt",
    "ReviewBriefRejectionAudit",
    "ReviewBriefRequest",
    "ReviewBriefRunner",
    "ReviewBriefRunnerRequest",
    "ReviewBriefRunnerResult",
    "ReviewBriefSafetyError",
    "ReviewBriefScenarioBreadth",
    "ReviewBriefSource",
    "ReviewBriefSourceMode",
    "ReviewBriefToolName",
    "ReviewBriefTopic",
    "ReviewBriefTrace",
    "ReviewBriefTraceReceipt",
    "ReviewBriefValidation",
    "ReviewerDiscipline",
    "build_authorized_snapshot",
    "build_deterministic_review_items",
    "mark_stale",
    "primary_disciplines_for_topic",
    "professional_review_source",
    "rejection_error_code",
    "render_review_brief",
    "required_review_brief_tools",
    "ridgeway_review_source",
    "runner_request",
    "topic_permissions",
    "validate_rendered_review_brief",
    "validate_runner_result",
    "validate_selection_appropriateness",
]
