"""Short-lived native Agents SDK child for one Review Brief invocation.

The module deliberately has no project-store, filesystem-tool, solver, web,
MCP, or generic RPC imports.  Its runtime filesystem use is limited to SDK
scratch/cache files inside the isolated working directory selected by the
parent.  All callable agent tools operate on one validated in-memory,
alias-only snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn
from urllib.parse import urlsplit

from agents import RunContextWrapper
from pydantic import Field, model_validator

from .contracts import ContractModel, canonical_sha256
from .review_brief import (
    REVIEW_BRIEF_MAX_EVIDENCE_ALIASES,
    REVIEW_BRIEF_MAX_FINDING_ALIASES,
    ReviewBriefActionKind,
    ReviewBriefAuthorizedSnapshot,
    ReviewBriefEvidenceClass,
    ReviewBriefFindingKind,
    ReviewBriefFindingSelection,
    ReviewBriefPriority,
    ReviewBriefReasonFacet,
    ReviewBriefRunnerRequest,
    ReviewBriefRunnerResult,
    ReviewBriefSafetyError,
    ReviewBriefScenarioBreadth,
    ReviewBriefSourceMode,
    ReviewBriefTopic,
    ReviewBriefTrace,
    ReviewerDiscipline,
    required_review_brief_tools,
    validate_selection_appropriateness,
)
from .review_brief_agent_runtime import (
    REVIEW_BRIEF_AGENT_PROTOCOL,
    REVIEW_BRIEF_AGENT_TOOLS,
    REVIEW_BRIEF_MAX_CONTEXT_BYTES,
    REVIEW_BRIEF_MAX_TOOL_CALLS,
    REVIEW_BRIEF_MAX_TURNS,
    ReviewBriefAgentCapabilityReceipt,
    ReviewBriefAgentChildRequest,
    ReviewBriefAgentChildResponse,
    ReviewBriefAgentRuntimeError,
    _read_frame,
    _wandb_inference_project,
    _write_frame,
    safe_child_error_code,
)

_ALIAS = re.compile(r"^(?:F|E|S)[1-9][0-9]{0,3}$")
_SAFE_MODEL_INPUT = (
    "Prepare the bounded review-finding selection. Use only the registered "
    "read-only tools and return only the required structured output."
)
REVIEW_BRIEF_MODEL_VISIBLE_CONTEXT_VERSION = "review-brief-model-visible-context/v1"
REVIEW_BRIEF_MAX_TOOL_OUTPUT_BYTES = 48 * 1024


class _FindingQuery(ContractModel):
    finding_aliases: list[str] = Field(min_length=1, max_length=REVIEW_BRIEF_MAX_FINDING_ALIASES)

    @model_validator(mode="after")
    def validate_aliases(self) -> _FindingQuery:
        if len(self.finding_aliases) != len(set(self.finding_aliases)) or any(
            not re.fullmatch(r"F[1-9][0-9]{0,3}", value) for value in self.finding_aliases
        ):
            raise ValueError("finding query must contain unique opaque aliases")
        return self


class _EvidenceQuery(ContractModel):
    evidence_aliases: list[str] = Field(min_length=1, max_length=REVIEW_BRIEF_MAX_EVIDENCE_ALIASES)

    @model_validator(mode="after")
    def validate_aliases(self) -> _EvidenceQuery:
        if len(self.evidence_aliases) != len(set(self.evidence_aliases)) or any(
            not re.fullmatch(r"E[1-9][0-9]{0,3}", value) for value in self.evidence_aliases
        ):
            raise ValueError("evidence query must contain unique opaque aliases")
        return self


class _AgentSelectionOutput(ContractModel):
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selections: list[ReviewBriefFindingSelection] = Field(
        min_length=1, max_length=REVIEW_BRIEF_MAX_FINDING_ALIASES
    )

    @model_validator(mode="after")
    def validate_unique_findings(self) -> _AgentSelectionOutput:
        aliases = [item.finding_alias for item in self.selections]
        if len(aliases) != len(set(aliases)):
            raise ValueError("agent selection contains duplicate finding aliases")
        return self


class _CapabilityOutput(ContractModel):
    scope_tool_used: Literal[True] = True
    structured_output_supported: Literal[True] = True


def _extract_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _extract_strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _extract_strings(nested)]
    return []


_CLOSED_TOOL_VALUE_TOKENS = frozenset(
    {
        *(item.value for item in ReviewBriefSourceMode),
        *(item.value for item in ReviewBriefFindingKind),
        *(item.value for item in ReviewBriefTopic),
        *(item.value for item in ReviewBriefReasonFacet),
        *(item.value for item in ReviewBriefEvidenceClass),
        *(item.value for item in ReviewBriefScenarioBreadth),
        *(item.value for item in ReviewBriefPriority),
        *(item.value for item in ReviewBriefActionKind),
        *(item.value for item in ReviewerDiscipline),
        *REVIEW_BRIEF_AGENT_TOOLS,
        "PASSES",
        "FAILS",
        "SURVEY_FIRST",
        "INCOMPLETE",
    }
)


@dataclass(slots=True)
class _InvocationContext:
    request: ReviewBriefRunnerRequest
    child: ReviewBriefAgentChildRequest
    tool_calls: list[str] = field(default_factory=list)
    inspected_finding_aliases: set[str] = field(default_factory=set)
    resolved_evidence_aliases: set[str] = field(default_factory=set)
    output_validated: bool = False
    output_sha256: str | None = None
    binding_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.binding_sha256 = canonical_sha256(self.child.binding.model_dump(mode="json"))

    @property
    def snapshot(self) -> ReviewBriefAuthorizedSnapshot:
        return self.request.snapshot

    def revalidate(self, *, tool_name: str, raw_arguments: str) -> None:
        self.child.binding.assert_authorization_lease_active()
        if tool_name not in REVIEW_BRIEF_AGENT_TOOLS:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_NOT_ALLOWED")
        if len(self.tool_calls) >= REVIEW_BRIEF_MAX_TOOL_CALLS:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_LIMIT_EXCEEDED")
        required = required_review_brief_tools(self.snapshot)
        if len(self.tool_calls) >= len(required) or tool_name != required[len(self.tool_calls)]:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_ORDER_INVALID")
        expected_snapshot = canonical_sha256(
            self.snapshot.model_dump(mode="json", exclude={"snapshot_sha256"})
        )
        if expected_snapshot != self.snapshot.snapshot_sha256:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_SNAPSHOT_CHANGED")
        binding = self.child.binding
        if canonical_sha256(binding.model_dump(mode="json")) != self.binding_sha256:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_BINDING_CHANGED")
        if self.snapshot.authorization_sha256 != binding.authorization_sha256:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_AUTHORIZATION_CHANGED")
        if self.snapshot.analysis_id_sha256 != binding.analysis_id_sha256:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_ANALYSIS_CHANGED")
        if self.snapshot.model_revision_id_sha256 != binding.model_revision_id_sha256:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_REVISION_CHANGED")
        if (
            self.snapshot.evidence_authorization_decision_id
            != binding.evidence_authorization_decision_id
        ):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_AUTHORIZATION_CHANGED")
        if self.snapshot.current_authorizations_sha256 != binding.current_authorizations_sha256:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_AUTHORIZATION_CHANGED")
        finding_aliases = {item.finding_alias for item in self.snapshot.findings}
        evidence_aliases = {
            alias for item in self.snapshot.findings for alias in item.evidence_aliases
        }
        scenario_aliases = {
            alias for item in self.snapshot.findings for alias in item.scenario_aliases
        }
        if finding_aliases != set(binding.permitted_finding_aliases):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FINDING_SCOPE_CHANGED")
        if evidence_aliases != set(binding.permitted_evidence_aliases):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_EVIDENCE_SCOPE_CHANGED")
        if scenario_aliases != set(binding.permitted_scenario_aliases):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_SCENARIO_SCOPE_CHANGED")
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_ARGUMENTS_INVALID") from exc
        if not isinstance(arguments, dict):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_ARGUMENTS_INVALID")
        inspected_findings: set[str] = set()
        resolved_evidence: set[str] = set()
        if tool_name == "review_brief_get_findings":
            if set(arguments) != {"query"}:
                raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_ARGUMENTS_INVALID")
            query = _FindingQuery.model_validate(arguments["query"])
            inspected_findings.update(query.finding_aliases)
        elif tool_name == "review_brief_resolve_evidence_aliases":
            if set(arguments) != {"query"}:
                raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_ARGUMENTS_INVALID")
            query = _EvidenceQuery.model_validate(arguments["query"])
            resolved_evidence.update(query.evidence_aliases)
        elif arguments:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_ARGUMENTS_INVALID")
        argument_aliases = {
            value for value in _extract_strings(arguments) if _ALIAS.fullmatch(value)
        }
        permitted = finding_aliases | evidence_aliases | scenario_aliases
        if not argument_aliases.issubset(permitted):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_ALIAS_NOT_AUTHORIZED")
        self.inspected_finding_aliases.update(inspected_findings)
        self.resolved_evidence_aliases.update(resolved_evidence)
        self.tool_calls.append(tool_name)

    def validate_selection(self, output: _AgentSelectionOutput) -> None:
        if output.snapshot_sha256 != self.snapshot.snapshot_sha256:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_OUTPUT_SNAPSHOT_MISMATCH")
        findings = {item.finding_alias: item for item in self.snapshot.findings}
        selected = {item.finding_alias: item for item in output.selections}
        if set(selected) != set(findings):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_REQUIRED_FINDING_RECALL_FAILED")
        required_tools = list(required_review_brief_tools(self.snapshot))
        if self.tool_calls != required_tools:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_REQUIRED_TRAVERSAL_MISSING")
        expected_findings = set(findings)
        expected_evidence = {
            alias for item in self.snapshot.findings for alias in item.evidence_aliases
        }
        if self.inspected_finding_aliases != expected_findings:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FINDING_TRAVERSAL_INCOMPLETE")
        if self.resolved_evidence_aliases != expected_evidence:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_EVIDENCE_TRAVERSAL_INCOMPLETE")
        for alias, selection in selected.items():
            context = findings[alias]
            try:
                validate_selection_appropriateness(context=context, selection=selection)
            except ReviewBriefSafetyError as exc:
                code = str(exc).replace("REVIEW_BRIEF_RUNNER_", "REVIEW_BRIEF_AGENT_")
                raise ReviewBriefAgentRuntimeError(code) from exc
        self.output_sha256 = canonical_sha256(output.model_dump(mode="json"))
        self.output_validated = True


def _finding_payload(item: Any) -> dict[str, Any]:
    return {
        "finding_alias": item.finding_alias,
        "finding_kind": item.finding_kind.value,
        "review_topic": item.review_topic.value,
        "reason_facets": [value.value for value in item.reason_facets],
        "priority": item.priority.value,
        "evidence_aliases": list(item.evidence_aliases),
        "evidence_count": len(item.evidence_aliases),
        "scenario_aliases": list(item.scenario_aliases),
        "scenario_count": len(item.scenario_aliases),
        "scenario_breadth": item.scenario_breadth.value,
        "direct_run_count": item.direct_run_count,
        "permitted_actions": [value.value for value in item.permitted_actions],
        "permitted_disciplines": [value.value for value in item.permitted_disciplines],
    }


def _branch_payload(item: Any) -> dict[str, Any]:
    return {
        "finding_alias": item.finding_alias,
        "review_topic": item.review_topic.value,
        "reason_facets": [value.value for value in item.reason_facets],
        "evidence_classes": [value.value for value in item.evidence_classes],
        "evidence_count": len(item.evidence_aliases),
        "scenario_count": len(item.scenario_aliases),
        "scenario_breadth": item.scenario_breadth.value,
        "direct_run_count": item.direct_run_count,
    }


_OPTION_BRANCH_TOPICS = frozenset(
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
_MODEL_BRANCH_TOPICS = frozenset(
    {
        ReviewBriefTopic.MODEL_FITNESS_BASIS,
        ReviewBriefTopic.ECOLOGICAL_ASSUMPTION,
        ReviewBriefTopic.DIRECT_RUN_QA,
    }
)


def _scope_payload(snapshot: ReviewBriefAuthorizedSnapshot) -> dict[str, Any]:
    required = required_review_brief_tools(snapshot)
    return {
        "source_mode": snapshot.source_mode.value,
        "disposition": snapshot.disposition,
        "finding_count": len(snapshot.findings),
        "required_branch_tools": [
            name
            for name in required
            if name
            not in {
                "review_brief_get_scope",
                "review_brief_list_findings",
                "review_brief_get_findings",
                "review_brief_resolve_evidence_aliases",
            }
        ],
    }


def _findings_payload(
    snapshot: ReviewBriefAuthorizedSnapshot, finding_aliases: list[str]
) -> dict[str, Any]:
    available = {item.finding_alias: item for item in snapshot.findings}
    if not set(finding_aliases).issubset(available):
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_FINDING_ALIAS_NOT_FOUND")
    return {"findings": [_finding_payload(available[alias]) for alias in finding_aliases]}


def _topic_branch_payload(
    snapshot: ReviewBriefAuthorizedSnapshot, topics: frozenset[ReviewBriefTopic]
) -> dict[str, Any]:
    return {
        "findings": [
            _branch_payload(item) for item in snapshot.findings if item.review_topic in topics
        ]
    }


def _evidence_payload(
    snapshot: ReviewBriefAuthorizedSnapshot, evidence_aliases: list[str]
) -> dict[str, Any]:
    permitted = {alias for item in snapshot.findings for alias in item.evidence_aliases}
    if not set(evidence_aliases).issubset(permitted):
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_EVIDENCE_ALIAS_NOT_FOUND")
    return {
        "evidence": [
            {
                "evidence_alias": evidence_alias,
                "evidence_classes": sorted(
                    {
                        branch.evidence_class.value
                        for item in snapshot.findings
                        for branch in item.evidence_branches
                        if branch.evidence_alias == evidence_alias
                    }
                ),
                "finding_aliases": [
                    item.finding_alias
                    for item in snapshot.findings
                    if evidence_alias in item.evidence_aliases
                ],
            }
            for evidence_alias in evidence_aliases
        ]
    }


def _reference_payload(snapshot: ReviewBriefAuthorizedSnapshot) -> dict[str, Any]:
    return {
        "review_topic": ReviewBriefTopic.REFERENCE_COMPARISON.value,
        **_topic_branch_payload(snapshot, frozenset({ReviewBriefTopic.REFERENCE_COMPARISON})),
    }


def _authorization_payload(snapshot: ReviewBriefAuthorizedSnapshot) -> dict[str, Any]:
    return {
        "authorized": True,
        "review_topic": ReviewBriefTopic.DATA_AUTHORIZATION.value,
        **_topic_branch_payload(snapshot, frozenset({ReviewBriefTopic.DATA_AUTHORIZATION})),
    }


def _build_tools(context: _InvocationContext) -> list[Any]:
    # Imports stay inside the child execution path; the authoritative worker does
    # not initialize the process-global Agents tracing provider.
    from agents import function_tool
    from agents.tool_guardrails import (
        ToolGuardrailFunctionOutput,
        ToolInputGuardrail,
        ToolInputGuardrailData,
        ToolOutputGuardrail,
        ToolOutputGuardrailData,
    )

    async def input_guard(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        data.context.context.revalidate(
            tool_name=data.context.tool_name,
            raw_arguments=data.context.tool_arguments,
        )
        return ToolGuardrailFunctionOutput.allow({"authorized": True})

    async def output_guard(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        serialized = json.dumps(data.output, sort_keys=True, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > REVIEW_BRIEF_MAX_TOOL_OUTPUT_BYTES:
            return ToolGuardrailFunctionOutput.raise_exception(
                {"error_code": "REVIEW_BRIEF_AGENT_TOOL_OUTPUT_TOO_LARGE"}
            )
        if any(
            value.startswith(("/Users/", "/home/", "/private/", "file://"))
            for value in _extract_strings(data.output)
        ):
            return ToolGuardrailFunctionOutput.raise_exception(
                {"error_code": "REVIEW_BRIEF_AGENT_TOOL_OUTPUT_PATH_REJECTED"}
            )
        binding = data.context.context.child.binding
        allowed_values = {
            *_CLOSED_TOOL_VALUE_TOKENS,
            *binding.permitted_finding_aliases,
            *binding.permitted_evidence_aliases,
            *binding.permitted_scenario_aliases,
        }
        if not set(_extract_strings(data.output)).issubset(allowed_values):
            return ToolGuardrailFunctionOutput.raise_exception(
                {"error_code": "REVIEW_BRIEF_AGENT_TOOL_OUTPUT_NOT_CLOSED"}
            )
        return ToolGuardrailFunctionOutput.allow({"alias_only": True})

    guards_in = [ToolInputGuardrail(input_guard, name="review_brief_authorization")]
    guards_out = [ToolOutputGuardrail(output_guard, name="review_brief_alias_only_output")]

    def decorate(name: str, description: str) -> Any:
        return function_tool(
            name_override=name,
            description_override=description,
            strict_mode=True,
            tool_input_guardrails=guards_in,
            tool_output_guardrails=guards_out,
            timeout=5.0,
            timeout_behavior="raise_exception",
        )

    @decorate(
        "review_brief_get_scope",
        "Return only bounded task scope and available review branches.",
    )
    async def get_scope(wrapper: RunContextWrapper[_InvocationContext]) -> dict[str, Any]:
        return _scope_payload(wrapper.context.snapshot)

    @decorate(
        "review_brief_list_findings",
        "List authorized finding aliases and priorities without branch or evidence detail.",
    )
    async def list_findings(wrapper: RunContextWrapper[_InvocationContext]) -> dict[str, Any]:
        return {
            "findings": [
                {"finding_alias": item.finding_alias, "priority": item.priority.value}
                for item in wrapper.context.snapshot.findings
            ]
        }

    @decorate(
        "review_brief_get_findings",
        "Return full closed-vocabulary detail for a batch of authorized finding aliases.",
    )
    async def get_findings(
        wrapper: RunContextWrapper[_InvocationContext], query: _FindingQuery
    ) -> dict[str, Any]:
        return _findings_payload(wrapper.context.snapshot, query.finding_aliases)

    @decorate(
        "review_brief_list_option_constraints",
        "List finding aliases that constrain feasibility, authorization, survey, or cost review.",
    )
    async def list_option_constraints(
        wrapper: RunContextWrapper[_InvocationContext],
    ) -> dict[str, Any]:
        return _topic_branch_payload(wrapper.context.snapshot, _OPTION_BRANCH_TOPICS)

    @decorate(
        "review_brief_list_model_fitness_gaps",
        "List authorized model-fitness, assumption, and QA finding aliases.",
    )
    async def list_model_fitness_gaps(
        wrapper: RunContextWrapper[_InvocationContext],
    ) -> dict[str, Any]:
        return _topic_branch_payload(wrapper.context.snapshot, _MODEL_BRANCH_TOPICS)

    @decorate(
        "review_brief_resolve_evidence_aliases",
        "Resolve a batch of authorized evidence aliases to finding aliases only.",
    )
    async def resolve_evidence_aliases(
        wrapper: RunContextWrapper[_InvocationContext], query: _EvidenceQuery
    ) -> dict[str, Any]:
        permitted = set(wrapper.context.child.binding.permitted_evidence_aliases)
        if not set(query.evidence_aliases).issubset(permitted):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_EVIDENCE_ALIAS_NOT_FOUND")
        return _evidence_payload(wrapper.context.snapshot, query.evidence_aliases)

    @decorate(
        "review_brief_get_reference_status",
        "Return reference-related finding aliases and no narrative claim.",
    )
    async def get_reference_status(
        wrapper: RunContextWrapper[_InvocationContext],
    ) -> dict[str, Any]:
        return _reference_payload(wrapper.context.snapshot)

    @decorate(
        "review_brief_get_authorization_status",
        "Confirm the immutable authorization digest and list authorization findings.",
    )
    async def get_authorization_status(
        wrapper: RunContextWrapper[_InvocationContext],
    ) -> dict[str, Any]:
        return _authorization_payload(wrapper.context.snapshot)

    return [
        get_scope,
        list_findings,
        get_findings,
        list_option_constraints,
        list_model_fitness_gaps,
        resolve_evidence_aliases,
        get_reference_status,
        get_authorization_status,
    ]


class _ReceiptWeaveTracingProcessor:
    """Delegating processor that captures and gates the exact Weave root call."""

    def __init__(self, *, context: _InvocationContext) -> None:
        from weave.integrations.openai_agents.openai_agents import WeaveTracingProcessor

        self._delegate = WeaveTracingProcessor()
        self.context = context
        self.root_call_id: str | None = None

    def on_trace_start(self, trace: Any) -> None:
        self._delegate.on_trace_start(trace)
        call = self._delegate._trace_calls.get(trace.trace_id)
        call_id = str(getattr(call, "id", "") or "") if call is not None else ""
        if not call_id:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_WEAVE_ROOT_MISSING")
        self.root_call_id = call_id

    def on_trace_end(self, trace: Any) -> None:
        trace_data = self._delegate._trace_data.get(trace.trace_id)
        if trace_data is None:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_WEAVE_TRACE_MISSING")
        trace_data.setdefault("metadata", {})["ecocorridor_trace_gate"] = {
            "protocol_sha256": hashlib.sha256(
                REVIEW_BRIEF_AGENT_PROTOCOL.encode("utf-8")
            ).hexdigest(),
            "snapshot_sha256": self.context.snapshot.snapshot_sha256,
            "project_id_sha256": self.context.child.binding.project_id_sha256,
            "analysis_id_sha256": self.context.child.binding.analysis_id_sha256,
            "portfolio_id_sha256": self.context.child.binding.portfolio_id_sha256,
            "model_revision_id_sha256": self.context.child.binding.model_revision_id_sha256,
            "decision_board_sha256": self.context.child.binding.decision_board_sha256,
            "authorization_sha256": self.context.child.binding.authorization_sha256,
            "authorization_lease_sha256": (self.context.child.binding.authorization_lease_sha256),
            "cloud_authorization_kind_sha256": hashlib.sha256(
                self.context.child.binding.cloud_authorization_kind.encode("utf-8")
            ).hexdigest(),
            "cloud_authorization_sha256": (self.context.child.binding.cloud_authorization_sha256),
            "current_authorizations_sha256": (
                self.context.child.binding.current_authorizations_sha256
            ),
            "evidence_authorization_decision_id_sha256": hashlib.sha256(
                self.context.child.binding.evidence_authorization_decision_id.encode("utf-8")
            ).hexdigest(),
            "association_id_sha256": self.context.child.trace.association_id_sha256,
            "wandb_run_id_sha256": self.context.child.trace.wandb_run_id_sha256,
            "wandb_run_lifecycle_sha256": hashlib.sha256(
                self.context.child.trace.wandb_run_lifecycle.encode("utf-8")
            ).hexdigest(),
            "tool_calls_sha256": canonical_sha256(list(self.context.tool_calls)),
            "traversal_sha256": canonical_sha256(
                {
                    "inspected_finding_count": len(self.context.inspected_finding_aliases),
                    "resolved_evidence_count": len(self.context.resolved_evidence_aliases),
                }
            ),
            "output_sha256": self.context.output_sha256 or canonical_sha256(None),
            "output_validation_sha256": canonical_sha256(
                {"output_validated": self.context.output_validated}
            ),
            "access_mode_sha256": hashlib.sha256(b"READ_ONLY").hexdigest(),
        }
        self._delegate.on_trace_end(trace)

    def on_span_start(self, span: Any) -> None:
        self._delegate.on_span_start(span)

    def on_span_end(self, span: Any) -> None:
        self._delegate.on_span_end(span)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self) -> None:
        self._delegate.force_flush()


def _value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _trace_gate(record: Any) -> dict[str, Any]:
    output = _value(record, "output", {})
    metadata = _value(output, "metadata", {})
    gate = _value(metadata, "ecocorridor_trace_gate", {})
    if not isinstance(gate, dict):
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TRACE_GATE_MISSING")
    return gate


def _validate_trace_url(url: str, child: ReviewBriefAgentChildRequest) -> str:
    parsed = urlsplit(url)
    expected = urlsplit(child.weave_target.web_base_url)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or child.weave_target.entity not in parsed.path.split("/")
        or child.weave_target.project not in parsed.path.split("/")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TRACE_URL_INVALID")
    return url


def _readback_trace(
    *,
    client: Any,
    processor: _ReceiptWeaveTracingProcessor,
    context: _InvocationContext,
) -> tuple[str, str, str]:
    processor.force_flush()
    client.flush()
    if not processor.root_call_id:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_WEAVE_ROOT_MISSING")
    record = client.get_call(processor.root_call_id)
    attributes = _value(record, "attributes", {})
    if _value(attributes, "agent_trace_id") != context.child.trace.trace_id:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TRACE_ID_MISMATCH")
    expected_wandb_run_id = (
        f"{context.child.weave_target.entity}/"
        f"{context.child.weave_target.project}/"
        f"{context.child.trace.wandb_run_id}"
    )
    if _value(record, "wb_run_id") != expected_wandb_run_id:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_WANDB_ASSOCIATION_MISMATCH")
    output = _value(record, "output", {})
    if _value(output, "status") != "completed":
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TRACE_INCOMPLETE")
    gate = _trace_gate(record)
    expected = {
        "protocol_sha256": hashlib.sha256(REVIEW_BRIEF_AGENT_PROTOCOL.encode("utf-8")).hexdigest(),
        "snapshot_sha256": context.snapshot.snapshot_sha256,
        "project_id_sha256": context.child.binding.project_id_sha256,
        "analysis_id_sha256": context.child.binding.analysis_id_sha256,
        "portfolio_id_sha256": context.child.binding.portfolio_id_sha256,
        "model_revision_id_sha256": context.child.binding.model_revision_id_sha256,
        "decision_board_sha256": context.child.binding.decision_board_sha256,
        "authorization_sha256": context.child.binding.authorization_sha256,
        "authorization_lease_sha256": context.child.binding.authorization_lease_sha256,
        "cloud_authorization_kind_sha256": hashlib.sha256(
            context.child.binding.cloud_authorization_kind.encode("utf-8")
        ).hexdigest(),
        "cloud_authorization_sha256": context.child.binding.cloud_authorization_sha256,
        "current_authorizations_sha256": context.child.binding.current_authorizations_sha256,
        "evidence_authorization_decision_id_sha256": hashlib.sha256(
            context.child.binding.evidence_authorization_decision_id.encode("utf-8")
        ).hexdigest(),
        "association_id_sha256": context.child.trace.association_id_sha256,
        "wandb_run_id_sha256": context.child.trace.wandb_run_id_sha256,
        "wandb_run_lifecycle_sha256": hashlib.sha256(
            context.child.trace.wandb_run_lifecycle.encode("utf-8")
        ).hexdigest(),
        "tool_calls_sha256": canonical_sha256(context.tool_calls),
        "traversal_sha256": canonical_sha256(
            {
                "inspected_finding_count": len(context.inspected_finding_aliases),
                "resolved_evidence_count": len(context.resolved_evidence_aliases),
            }
        ),
        "output_sha256": context.output_sha256 or canonical_sha256(None),
        "output_validation_sha256": canonical_sha256({"output_validated": True}),
        "access_mode_sha256": hashlib.sha256(b"READ_ONLY").hexdigest(),
    }
    if gate != expected or any(
        not key.endswith("_sha256") or not re.fullmatch(r"[0-9a-f]{64}", str(value))
        for key, value in gate.items()
    ):
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TRACE_GATE_MISMATCH")
    url = str(_value(record, "ui_url", "") or "")
    if not url:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TRACE_URL_MISSING")
    return (
        context.child.trace.trace_id,
        processor.root_call_id,
        _validate_trace_url(url, context.child),
    )


def _configure_environment(child: ReviewBriefAgentChildRequest) -> None:
    runtime = Path.cwd().resolve()
    if runtime.is_symlink():
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_RUNTIME_ROOT_UNSAFE")
    for name in ("wandb", "cache", "artifacts", "data", "weave-cache"):
        directory = runtime / name
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or runtime not in directory.resolve().parents:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_RUNTIME_PATH_UNSAFE")
    os.environ.update(
        {
            "WANDB_API_KEY": child.wandb_api_key.get_secret_value(),
            "WANDB_BASE_URL": child.weave_target.api_base_url,
            "WANDB_MODE": "online",
            "WANDB_DIR": str(runtime / "wandb"),
            "WANDB_CACHE_DIR": str(runtime / "cache"),
            "WANDB_ARTIFACT_DIR": str(runtime / "artifacts"),
            "WANDB_DATA_DIR": str(runtime / "data"),
            "WANDB_CONSOLE": "off",
            "WANDB_DISABLE_CODE": "true",
            "WANDB_DISABLE_GIT": "true",
            "WANDB_ERROR_REPORTING": "false",
            "WANDB_SHOW_RUN": "false",
            "WANDB_SILENT": "true",
            "WEAVE_PRINT_CALL_LINK": "false",
            "WEAVE_CAPTURE_CODE": "false",
            "WEAVE_CAPTURE_CLIENT_INFO": "false",
            "WEAVE_CAPTURE_SYSTEM_INFO": "false",
            "WEAVE_REDACT_PII": "true",
            "WEAVE_SERVER_CACHE_DIR": str(runtime / "weave-cache"),
            "WEAVE_ENABLE_DISK_FALLBACK": "false",
        }
    )


def _model(child: ReviewBriefAgentChildRequest) -> Any:
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    from agents.models.openai_responses import OpenAIResponsesModel
    from openai import AsyncOpenAI

    routing_project = _wandb_inference_project(
        provider_base_url=child.provider.base_url,
        weave_target=child.weave_target,
    )
    client_kwargs: dict[str, Any] = {
        "api_key": child.provider_api_key.get_secret_value(),
        "base_url": child.provider.base_url,
        "timeout": child.provider.timeout_seconds,
        "max_retries": 0,
    }
    if routing_project is not None:
        client_kwargs["project"] = routing_project
    client = AsyncOpenAI(**client_kwargs)
    if child.provider.mode == "RESPONSES":
        return OpenAIResponsesModel(
            model=child.provider.model,
            openai_client=client,
            model_is_explicit=True,
        )
    return OpenAIChatCompletionsModel(
        model=child.provider.model,
        openai_client=client,
        strict_feature_validation=True,
        buffer_streamed_tool_calls=False,
    )


def _raise_stage_failure(exc: BaseException, *, fallback_code: str) -> NoReturn:
    """Preserve a finite provider code or replace internals with a safe stage code."""

    mapped = safe_child_error_code(exc)
    if mapped == "REVIEW_BRIEF_AGENT_INVOCATION_FAILED":
        mapped = fallback_code
    raise ReviewBriefAgentRuntimeError(mapped) from exc


def _usage(result: Any) -> tuple[int | None, int | None, str]:
    responses = list(result.raw_responses)
    if not 1 <= len(responses) <= REVIEW_BRIEF_MAX_TURNS:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_MODEL_TURN_LIMIT_EXCEEDED")
    input_tokens = sum(int(response.usage.input_tokens) for response in responses)
    output_tokens = sum(int(response.usage.output_tokens) for response in responses)
    response_ids = [
        str(response.response_id or response.request_id or "") for response in responses
    ]
    provider_call_id = next((value for value in reversed(response_ids) if value), "")
    if not provider_call_id:
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_PROVIDER_CALL_ID_MISSING")
    return input_tokens, output_tokens, provider_call_id


def _instructions_for_snapshot(snapshot: ReviewBriefAuthorizedSnapshot) -> str:
    required = " then ".join(required_review_brief_tools(snapshot))
    return (
        "You are a read-only ecological review-brief finding selector. You cannot choose, "
        "approve, rank, attest, design, or modify a portfolio. Call exactly once, in this "
        f"order: {required}. Batch every finding returned by review_brief_list_findings in "
        "review_brief_get_findings and every "
        "listed evidence alias in review_brief_resolve_evidence_aliases. Do not call any other "
        "tool. Inspect the closed reason_facets, evidence_classes, scenario_breadth, and branch "
        "counts. The closed review_topic and reason facets determine which action and reviewer "
        "disciplines are appropriate; choose only the permitted values that match them. Return "
        "every listed "
        "finding alias exactly once. Return no prose, scientific value, unit, location, "
        "recommendation, raw evidence ID, or name. Set snapshot_sha256 exactly to "
        f"{snapshot.snapshot_sha256}."
    )


def _instructions(context: _InvocationContext) -> str:
    return _instructions_for_snapshot(context.snapshot)


def review_brief_model_visible_context(
    snapshot: ReviewBriefAuthorizedSnapshot,
) -> dict[str, Any]:
    """Canonical dynamic content authored by this app and visible to the model.

    Static SDK tool schemas are deliberately outside this manifest. The fixed input,
    exact instructions, ordered tool arguments, and deterministic tool outputs are
    produced by the same helpers used by the live agent tools.
    """

    finding_aliases = [item.finding_alias for item in snapshot.findings]
    evidence_aliases = list(
        dict.fromkeys(alias for item in snapshot.findings for alias in item.evidence_aliases)
    )
    exchanges: list[dict[str, Any]] = []
    for tool_name in required_review_brief_tools(snapshot):
        arguments: dict[str, Any]
        output: dict[str, Any]
        if tool_name == "review_brief_get_scope":
            arguments = {}
            output = _scope_payload(snapshot)
        elif tool_name == "review_brief_list_findings":
            arguments = {}
            output = {
                "findings": [
                    {"finding_alias": item.finding_alias, "priority": item.priority.value}
                    for item in snapshot.findings
                ]
            }
        elif tool_name == "review_brief_get_findings":
            arguments = {"query": {"finding_aliases": finding_aliases}}
            output = _findings_payload(snapshot, finding_aliases)
        elif tool_name == "review_brief_list_option_constraints":
            arguments = {}
            output = _topic_branch_payload(snapshot, _OPTION_BRANCH_TOPICS)
        elif tool_name == "review_brief_list_model_fitness_gaps":
            arguments = {}
            output = _topic_branch_payload(snapshot, _MODEL_BRANCH_TOPICS)
        elif tool_name == "review_brief_get_reference_status":
            arguments = {}
            output = _reference_payload(snapshot)
        elif tool_name == "review_brief_get_authorization_status":
            arguments = {}
            output = _authorization_payload(snapshot)
        elif tool_name == "review_brief_resolve_evidence_aliases":
            arguments = {"query": {"evidence_aliases": evidence_aliases}}
            output = _evidence_payload(snapshot, evidence_aliases)
        else:  # pragma: no cover - required traversal is a closed local contract
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_NOT_ALLOWED")
        output_bytes = json.dumps(
            output, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(output_bytes) > REVIEW_BRIEF_MAX_TOOL_OUTPUT_BYTES:
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_TOOL_OUTPUT_TOO_LARGE")
        exchanges.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "output": output,
                "output_bytes": len(output_bytes),
            }
        )
    content = {
        "schema_version": REVIEW_BRIEF_MODEL_VISIBLE_CONTEXT_VERSION,
        "fixed_input": _SAFE_MODEL_INPUT,
        "instructions": _instructions_for_snapshot(snapshot),
        "ordered_tool_exchanges": exchanges,
    }
    return {**content, "context_sha256": canonical_sha256(content)}


def review_brief_model_visible_context_bytes(
    snapshot: ReviewBriefAuthorizedSnapshot,
) -> bytes:
    return json.dumps(
        review_brief_model_visible_context(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


@contextmanager
def _associated_wandb_run(child: ReviewBriefAgentChildRequest) -> Iterator[Any]:
    """Create only invocation-owned runs; campaign runs remain parent-owned.

    The short-lived child is the only process allowed to initialize the owned
    invocation run. Its config contains digests only and it never logs files or
    artifacts. Evaluation campaigns pass ``ATTACH_EXISTING`` because the campaign
    process already owns and finishes that run.
    """

    if child.trace.wandb_run_lifecycle == "ATTACH_EXISTING":
        yield None
        return

    import wandb

    snapshot_sha256 = str(child.runner_request.get("snapshot", {}).get("snapshot_sha256", ""))
    config = {
        "protocol_sha256": hashlib.sha256(REVIEW_BRIEF_AGENT_PROTOCOL.encode("utf-8")).hexdigest(),
        "action_sha256": hashlib.sha256(child.action.encode("utf-8")).hexdigest(),
        "snapshot_sha256": snapshot_sha256,
        "project_id_sha256": child.binding.project_id_sha256,
        "analysis_id_sha256": child.binding.analysis_id_sha256,
        "portfolio_id_sha256": child.binding.portfolio_id_sha256,
        "model_revision_id_sha256": child.binding.model_revision_id_sha256,
        "decision_board_sha256": child.binding.decision_board_sha256,
        "authorization_sha256": child.binding.authorization_sha256,
        "authorization_lease_sha256": child.binding.authorization_lease_sha256,
        "cloud_authorization_kind_sha256": hashlib.sha256(
            child.binding.cloud_authorization_kind.encode("utf-8")
        ).hexdigest(),
        "cloud_authorization_sha256": child.binding.cloud_authorization_sha256,
        "association_id_sha256": child.trace.association_id_sha256,
        "wandb_run_id_sha256": child.trace.wandb_run_id_sha256,
    }
    if any(not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in config.values()):
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_WANDB_CONFIG_INVALID")
    settings = wandb.Settings(
        base_url=child.weave_target.api_base_url,
        console="off",
        disable_code=True,
        disable_git=True,
        x_disable_machine_info=True,
        x_disable_meta=True,
        x_save_requirements=False,
        silent=True,
    )
    run = wandb.init(
        entity=child.weave_target.entity,
        project=child.weave_target.project,
        id=child.trace.wandb_run_id,
        job_type="review_brief_agent",
        tags=["review_brief_agent", "agents_sdk", "hash_only"],
        config=config,
        resume="allow",
        save_code=False,
        settings=settings,
    )
    if run is None or str(getattr(run, "id", "")) != child.trace.wandb_run_id:
        if run is not None:
            run.finish(exit_code=1)
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_WANDB_RUN_ID_MISMATCH")
    try:
        yield run
    except BaseException:
        run.finish(exit_code=1)
        raise
    else:
        try:
            run.finish(exit_code=0)
        except Exception as exc:
            raise ReviewBriefAgentRuntimeError(
                "REVIEW_BRIEF_AGENT_WANDB_RUN_FINISH_FAILED"
            ) from exc


async def _execute_agent(
    child: ReviewBriefAgentChildRequest,
    request: ReviewBriefRunnerRequest,
) -> ReviewBriefAgentChildResponse:
    import weave
    from agents import (
        Agent,
        GuardrailFunctionOutput,
        ModelSettings,
        RunConfig,
        Runner,
        ToolExecutionConfig,
        input_guardrail,
        output_guardrail,
        set_trace_processors,
    )

    context = _InvocationContext(request=request, child=child)
    try:
        client = weave.init(
            f"{child.weave_target.entity}/{child.weave_target.project}",
            settings={
                "print_call_link": False,
                "capture_code": False,
                "capture_client_info": False,
                "capture_system_info": False,
                # The Agents SDK emits the model/tool spans consumed by the sole
                # WeaveTracingProcessor installed below. Avoid a second automatic
                # provider patch and its duplicate, non-agent call tree.
                "implicitly_patch_integrations": False,
                "redact_pii": True,
                "use_server_cache": False,
                "enable_disk_fallback": False,
                "http_timeout": min(child.provider.timeout_seconds, 60.0),
            },
            global_attributes={
                "app_namespace_sha256": hashlib.sha256(b"ecocorridor").hexdigest(),
                "agent_kind_sha256": hashlib.sha256(b"review_brief_agent").hexdigest(),
                "project_id_sha256": child.binding.project_id_sha256,
                "analysis_id_sha256": child.binding.analysis_id_sha256,
                "portfolio_id_sha256": child.binding.portfolio_id_sha256,
            },
        )
    except Exception as exc:  # noqa: BLE001 - stage boundary emits only a finite code
        _raise_stage_failure(exc, fallback_code="REVIEW_BRIEF_AGENT_TRACE_SETUP_FAILED")
    try:
        set_run_context = getattr(client, "set_wandb_run_context", None)
        if not callable(set_run_context):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_WANDB_ASSOCIATION_UNAVAILABLE")
        set_run_context(run_id=child.trace.wandb_run_id)
        processor = _ReceiptWeaveTracingProcessor(context=context)
        # Replaces the SDK's default exporter and any processor auto-installed by Weave.
        set_trace_processors([processor])
    except ReviewBriefAgentRuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - stage boundary emits only a finite code
        _raise_stage_failure(
            exc,
            fallback_code="REVIEW_BRIEF_AGENT_TRACE_ASSOCIATION_FAILED",
        )

    @input_guardrail(name="review_brief_fixed_task", run_in_parallel=False)
    async def fixed_input_guard(_wrapper: Any, _agent: Any, value: Any) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info={"fixed_task": value == _SAFE_MODEL_INPUT},
            tripwire_triggered=value != _SAFE_MODEL_INPUT,
        )

    @output_guardrail(name="review_brief_closed_output")
    async def closed_output_guard(
        _wrapper: Any, _agent: Any, value: Any
    ) -> GuardrailFunctionOutput:
        if child.action == "PREPARE" and isinstance(value, _AgentSelectionOutput):
            context.validate_selection(value)
            return GuardrailFunctionOutput(
                output_info={"validated": True}, tripwire_triggered=False
            )
        if child.action == "PREFLIGHT" and isinstance(value, _CapabilityOutput):
            context.output_validated = bool(
                value.scope_tool_used
                and value.structured_output_supported
                and context.tool_calls == ["review_brief_get_scope"]
            )
            context.output_sha256 = (
                canonical_sha256(value.model_dump(mode="json"))
                if context.output_validated
                else None
            )
            return GuardrailFunctionOutput(
                output_info={"validated": context.output_validated},
                tripwire_triggered=not context.output_validated,
            )
        return GuardrailFunctionOutput(output_info={"validated": False}, tripwire_triggered=True)

    try:
        tools = _build_tools(context)
    except Exception as exc:  # noqa: BLE001 - stage boundary emits only a finite code
        _raise_stage_failure(exc, fallback_code="REVIEW_BRIEF_AGENT_TOOL_SETUP_FAILED")
    if child.action == "PREFLIGHT":
        tools = [tool for tool in tools if tool.name == "review_brief_get_scope"]
        output_type: type[ContractModel] = _CapabilityOutput
        instructions = (
            "Call review_brief_get_scope exactly once, then return the strict structured "
            "capability output with both fields true. Return no prose."
        )
    else:
        output_type = _AgentSelectionOutput
        instructions = _instructions(context)
    try:
        agent = Agent[_InvocationContext](
            name="EcoCorridor Review Brief Agent",
            instructions=instructions,
            model=_model(child),
            model_settings=ModelSettings(
                temperature=0,
                parallel_tool_calls=False,
                include_usage=True,
                store=False,
            ),
            tools=tools,
            output_type=output_type,
            input_guardrails=[fixed_input_guard],
            output_guardrails=[closed_output_guard],
            reset_tool_choice=True,
        )
    except Exception as exc:  # noqa: BLE001 - stage boundary emits only a finite code
        _raise_stage_failure(exc, fallback_code="REVIEW_BRIEF_AGENT_CONFIGURATION_FAILED")
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                _SAFE_MODEL_INPUT,
                context=context,
                max_turns=REVIEW_BRIEF_MAX_TURNS,
                session=None,
                previous_response_id=None,
                conversation_id=None,
                run_config=RunConfig(
                    trace_include_sensitive_data=False,
                    workflow_name="EcoCorridor Review Brief Agent",
                    trace_id=child.trace.trace_id,
                    group_id=child.trace.wandb_run_id_sha256,
                    trace_metadata={
                        "snapshot_sha256": context.snapshot.snapshot_sha256,
                        "project_id_sha256": child.binding.project_id_sha256,
                        "analysis_id_sha256": child.binding.analysis_id_sha256,
                        "portfolio_id_sha256": child.binding.portfolio_id_sha256,
                        "association_id_sha256": child.trace.association_id_sha256,
                    },
                    tool_execution=ToolExecutionConfig(
                        max_function_tool_concurrency=1,
                        pre_approval_tool_input_guardrails=True,
                    ),
                    tool_not_found_behavior="raise_error",
                ),
            ),
            timeout=child.provider.timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - stage boundary emits only a finite code
        _raise_stage_failure(exc, fallback_code="REVIEW_BRIEF_AGENT_EXECUTION_FAILED")
    elapsed_ms = round((time.monotonic() - started) * 1000)
    try:
        trace_id, weave_call_id, trace_url = _readback_trace(
            client=client,
            processor=processor,
            context=context,
        )
    except ReviewBriefAgentRuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - stage boundary emits only a finite code
        _raise_stage_failure(exc, fallback_code="REVIEW_BRIEF_AGENT_TRACE_READBACK_FAILED")
    try:
        input_tokens, output_tokens, provider_call_id = _usage(result)
    except ReviewBriefAgentRuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - stage boundary emits only a finite code
        _raise_stage_failure(exc, fallback_code="REVIEW_BRIEF_AGENT_USAGE_EXTRACTION_FAILED")
    if child.action == "PREFLIGHT":
        if not isinstance(result.final_output, _CapabilityOutput):
            raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_PREFLIGHT_OUTPUT_INVALID")
        return ReviewBriefAgentChildResponse(
            ok=True,
            capability=ReviewBriefAgentCapabilityReceipt(
                provider_mode=child.provider.mode,
                trace_id=trace_id,
                trace_url=trace_url,
            ),
        )
    if not isinstance(result.final_output, _AgentSelectionOutput):
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_OUTPUT_INVALID")
    runner_result = ReviewBriefRunnerResult(
        snapshot_sha256=result.final_output.snapshot_sha256,
        selections=result.final_output.selections,
        provider_call_id_sha256=hashlib.sha256(provider_call_id.encode("utf-8")).hexdigest(),
        provider_model=child.provider.model,
        tool_calls=list(context.tool_calls),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        elapsed_ms=elapsed_ms,
        trace=ReviewBriefTrace(
            trace_id=trace_id,
            weave_call_id=weave_call_id,
            association_id=child.trace.association_id,
            wandb_run_id=child.trace.wandb_run_id,
            trace_url=trace_url,
        ),
    )
    return ReviewBriefAgentChildResponse(ok=True, result=runner_result.model_dump(mode="json"))


async def _execute(child: ReviewBriefAgentChildRequest) -> ReviewBriefAgentChildResponse:
    request = ReviewBriefRunnerRequest.model_validate(child.runner_request)
    # Reject a snapshot whose mandatory one-call batches cannot fit before the
    # provider, tracing client, or owned W&B run receives any request.
    if len(review_brief_model_visible_context_bytes(request.snapshot)) > (
        REVIEW_BRIEF_MAX_CONTEXT_BYTES
    ):
        raise ReviewBriefAgentRuntimeError("REVIEW_BRIEF_AGENT_CONTEXT_TOO_LARGE")
    child.binding.assert_authorization_lease_active()
    _configure_environment(child)
    with _associated_wandb_run(child):
        return await _execute_agent(child, request)


def main() -> int:
    try:
        payload = _read_frame(sys.stdin.buffer)
        child = ReviewBriefAgentChildRequest.model_validate_json(payload)
        response = asyncio.run(_execute(child))
    except Exception as exc:  # noqa: BLE001 - process boundary must emit only a safe code
        response = ReviewBriefAgentChildResponse(
            protocol_version=REVIEW_BRIEF_AGENT_PROTOCOL,
            ok=False,
            error_code=safe_child_error_code(exc),
        )
    _write_frame(sys.stdout.buffer, response.model_dump_json().encode("utf-8"))
    # A valid framed rejection is a successful transport exchange; the parent
    # raises from ``error_code`` and persists no draft. Non-zero is reserved for
    # crashes that prevented a framed response.
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess smoke tests
    raise SystemExit(main())


__all__ = [
    "REVIEW_BRIEF_MODEL_VISIBLE_CONTEXT_VERSION",
    "main",
    "review_brief_model_visible_context",
    "review_brief_model_visible_context_bytes",
]
