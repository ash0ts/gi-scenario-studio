"""Research-only paired Evidence Guide architecture executor.

The executor isolates one architectural question: after one model-selected,
bounded set of read-only evidence lookups, does a finite repository plan behave
differently from model-authored prose when both branches receive the exact same
evidence?

This module is deliberately separate from the product-parity executor.  Its
outcomes are development-research records and can never enable or serve a live
Evidence Guide answer.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Protocol

from .evidence_evaluation_v2 import (
    ALLOWED_ACTIONS,
    REQUIRED_LIMITATION_TEXT,
    audit_provider_packet,
    provider_packet,
    score_attempt,
)
from .evidence_guide_v2_campaign import AttemptObserver, AttemptSpec, canonical_json
from .evidence_guide_v2_protocol import (
    allowed_explanation_plan_ids,
    deterministic_question_intent,
)
from .evidence_guide_v2_provider import (
    _compact_model_packet,
    _parse_json,
    _plan_schema,
    _repository_rule_disposition,
    _repository_tool_result,
    _resolve_repository_claim,
    _tool_definitions,
    render_repository_plan,
)
from .inference import InferenceCompletion

PAIRED_ARCHITECTURE_ID = "A3-P+A0x-F"
PAIRED_EXECUTOR_VERSION = "evidence-guide-paired-architecture-v1"
FINITE_BRANCH_ID = "A3-P"
PROSE_BRANCH_ID = "A0x-F"


class CompletionClient(Protocol):
    def complete(
        self,
        *,
        entity: str,
        project: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        max_tokens: int = 128,
        seed: int = 17,
    ) -> InferenceCompletion: ...


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_error_code(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]+", "_", value.upper()).strip("_")
    return (normalized or "PAIRED_VALIDATION_FAILED")[:64]


@dataclass(frozen=True)
class RouteReceipt:
    """Immutable projection of the target-hidden routing call."""

    provider_call_id: str
    model_id: str
    tool_calls_json: bytes
    router_packet_sha256: str
    provider_response_sha256: str
    input_tokens: int
    output_tokens: int
    transport_attempts: int
    latency_ms: float

    @property
    def receipt_sha256(self) -> str:
        return _sha256(_json_bytes(self.to_record(include_digest=False)))

    def tool_calls(self) -> list[dict[str, Any]]:
        value = json.loads(self.tool_calls_json)
        if not isinstance(value, list):
            raise TypeError("PAIRED_ROUTE_RECEIPT_INVALID")
        return [dict(item) for item in value]

    @property
    def tool_call_id(self) -> str:
        return str(self.tool_calls()[0]["tool_call_id"])

    @property
    def tool_name(self) -> str:
        return str(self.tool_calls()[0]["tool_name"])

    def arguments(self) -> dict[str, Any]:
        return dict(self.tool_calls()[0]["arguments"])

    def to_record(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "provider_call_id": self.provider_call_id,
            "model_id": self.model_id,
            "tool_calls": self.tool_calls(),
            "router_packet_sha256": self.router_packet_sha256,
            "provider_response_sha256": self.provider_response_sha256,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "transport_attempts": self.transport_attempts,
            "latency_ms": self.latency_ms,
        }
        if include_digest:
            value["receipt_sha256"] = self.receipt_sha256
        return value


@dataclass(frozen=True)
class ToolReceipt:
    """Immutable projection of one repository-owned tool execution."""

    tool_call_id: str
    tool_name: str
    arguments_json: bytes
    result_json: bytes

    @property
    def receipt_sha256(self) -> str:
        return _sha256(_json_bytes(self.to_record(include_digest=False)))

    def arguments(self) -> dict[str, Any]:
        return dict(json.loads(self.arguments_json))

    def result(self) -> list[dict[str, Any]]:
        value = json.loads(self.result_json)
        if not isinstance(value, list):
            raise TypeError("PAIRED_TOOL_RECEIPT_INVALID")
        return [dict(item) for item in value]

    def to_record(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments(),
            "result": self.result(),
        }
        if include_digest:
            value["receipt_sha256"] = self.receipt_sha256
        return value


@dataclass(frozen=True)
class BranchOutcome:
    branch_id: str
    provider_call_id: str
    evidence_payload_sha256: str
    output_json: bytes
    score_json: bytes
    passed: bool
    critical_failure: bool
    error_codes: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    transport_attempts: int
    latency_ms: float
    selected_plan_id: str | None = None
    response_mode: str | None = None

    def output(self) -> dict[str, Any]:
        return dict(json.loads(self.output_json))

    def score(self) -> dict[str, Any]:
        return dict(json.loads(self.score_json))

    def to_record(self, *, include_output: bool = True) -> dict[str, Any]:
        value = {
            "branch_id": self.branch_id,
            "provider_call_id": self.provider_call_id,
            "evidence_payload_sha256": self.evidence_payload_sha256,
            "output_sha256": _sha256(self.output_json),
            "score_sha256": _sha256(self.score_json),
            "passed": self.passed,
            "critical_failure": self.critical_failure,
            "error_codes": list(self.error_codes),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "transport_attempts": self.transport_attempts,
            "latency_ms": self.latency_ms,
            "selected_plan_id": self.selected_plan_id,
            "response_mode": self.response_mode,
        }
        if include_output:
            value["output"] = self.output()
            value["score"] = self.score()
        return value


def _invalid_branch_outcome(
    *,
    branch_id: str,
    completion: InferenceCompletion,
    evidence_payload_sha256: str,
    error_code: str,
) -> BranchOutcome:
    """Contain one schema-invalid synthesis result without skipping its sibling."""

    code = _safe_error_code(error_code)
    output = {
        "status": "invalid",
        "evidence_references": [],
        "numeric_claims": [],
        "limitations": [],
        "actions": [],
        "read_only": True,
        "requires_engineer_confirmation": True,
    }
    score = {
        "passed": False,
        "critical_failure": True,
        "error_codes": [code],
        "errors": [
            {
                "code": code,
                "message": "the synthesis result failed its strict branch contract",
                "critical": "true",
            }
        ],
        "scenario_type": "protocol_invalid",
        "causal_behavior_passed": False,
        "intent_correct": False,
    }
    return BranchOutcome(
        branch_id=branch_id,
        provider_call_id=completion.provider_call_id,
        evidence_payload_sha256=evidence_payload_sha256,
        output_json=_json_bytes(output),
        score_json=_json_bytes(score),
        passed=False,
        critical_failure=True,
        error_codes=(code,),
        input_tokens=completion.usage.input_tokens,
        output_tokens=completion.usage.output_tokens,
        transport_attempts=completion.transport_attempts,
        latency_ms=completion.latency_ms,
    )


@dataclass(frozen=True)
class PairedExperimentalOutcome:
    attempt_id: str
    case_id: str
    model_id: str
    branch_order: tuple[str, str]
    route: RouteReceipt
    tools: tuple[ToolReceipt, ...]
    finite: BranchOutcome
    prose: BranchOutcome
    latency_ms: float
    router_plan_valid: bool
    weave_turn_receipt: Mapping[str, str] | None = None

    production_eligible: bool = False
    answer_released: bool = False
    executor_version: str = PAIRED_EXECUTOR_VERSION

    @property
    def provider_invocations(self) -> int:
        return 3

    @property
    def transport_attempts(self) -> int:
        return (
            self.route.transport_attempts
            + self.finite.transport_attempts
            + self.prose.transport_attempts
        )

    @property
    def input_tokens(self) -> int:
        return self.route.input_tokens + self.finite.input_tokens + self.prose.input_tokens

    @property
    def output_tokens(self) -> int:
        return self.route.output_tokens + self.finite.output_tokens + self.prose.output_tokens

    @property
    def evidence_payload_sha256(self) -> str:
        if self.finite.evidence_payload_sha256 != self.prose.evidence_payload_sha256:
            raise RuntimeError("PAIRED_EVIDENCE_PAYLOAD_DRIFT")
        return self.finite.evidence_payload_sha256

    @property
    def tool(self) -> ToolReceipt:
        """Backward-compatible first receipt for older local analysis code."""

        return self.tools[0]

    @property
    def shared_router_tool_receipt_sha256(self) -> str:
        return _sha256(
            _json_bytes([item.to_record(include_digest=True) for item in self.tools])
        )

    @property
    def passed(self) -> bool:
        return self.finite.passed and self.prose.passed

    def to_record(self, *, include_outputs: bool = True) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "executor_version": self.executor_version,
            "attempt_id": self.attempt_id,
            "case_id": self.case_id,
            "model_id": self.model_id,
            "architecture_id": PAIRED_ARCHITECTURE_ID,
            "production_eligible": self.production_eligible,
            "answer_released": self.answer_released,
            "branch_order": list(self.branch_order),
            "route": self.route.to_record(),
            "tool": self.tool.to_record(),
            "tools": [item.to_record() for item in self.tools],
            "shared_router_tool_receipt_sha256": self.shared_router_tool_receipt_sha256,
            "router_plan_valid": self.router_plan_valid,
            "weave_turn_receipt": dict(self.weave_turn_receipt or {}),
            "evidence_payload_sha256": self.evidence_payload_sha256,
            "finite": self.finite.to_record(include_output=include_outputs),
            "prose": self.prose.to_record(include_output=include_outputs),
            "provider_invocations": self.provider_invocations,
            "transport_attempts": self.transport_attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "passed": self.passed,
        }


def branch_order(spec: AttemptSpec) -> tuple[str, str]:
    """Counterbalance siblings and reverse each case on its next presentation."""

    case_parity = int(hashlib.sha256(spec.case_id.encode("utf-8")).hexdigest()[-1], 16) & 1
    presentation_parity = (spec.repeat + spec.presentation_index) & 1
    if case_parity ^ presentation_parity:
        return (PROSE_BRANCH_ID, FINITE_BRANCH_ID)
    return (FINITE_BRANCH_ID, PROSE_BRANCH_ID)


def _prose_schema(interpretation_ids: Sequence[str]) -> dict[str, Any]:
    allowed = sorted({str(item) for item in interpretation_ids})
    if not allowed:
        raise RuntimeError("PAIRED_PROSE_INTERPRETATION_UNAVAILABLE")
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "interpretation_id": {"type": "string", "enum": allowed},
            "response_mode": {"type": "string", "enum": ["answer", "refuse"]},
            "answer_text": {"type": "string"},
            "evidence_ids": string_array,
            "limitation_ids": string_array,
            "action_ids": string_array,
        },
        "required": [
            "interpretation_id",
            "response_mode",
            "answer_text",
            "evidence_ids",
            "limitation_ids",
            "action_ids",
        ],
    }


def _branch_messages(
    *,
    evidence_payload: bytes,
    finite: bool,
) -> list[dict[str, str]]:
    if finite:
        system = (
            "Select exactly one allowed finite repository explanation plan. Do not write an "
            "answer, number, unit, recommendation, or approval."
        )
    else:
        system = (
            "Write a short read-only explanation using only the supplied evidence. Return the "
            "strict evaluation envelope. Do not approve, rank, predict, invent evidence, or "
            "claim an unmeasured environmental outcome."
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": evidence_payload.decode("utf-8")},
    ]


def _route_messages(packet: Mapping[str, Any]) -> list[dict[str, str]]:
    routing_packet = {
        "task": "select_complete_read_only_evidence_tool_plan",
        "intent": deterministic_question_intent(str(packet.get("question") or "")),
        "question": str(packet.get("question") or ""),
        "anchor_kind": str((packet.get("anchor") or {}).get("kind") or "alternative"),
        "available_tool_aliases": sorted(str(name) for name in packet.get("available_tools", [])),
        "rules": [
            (
                "Select the complete required set of one to four supplied read-only "
                "evidence tools and call each selected tool exactly once."
            ),
            "The target evidence is unavailable until the tool returns.",
            "Do not omit a tool needed to support the complete interpretation.",
            "Never request another subject or invent an identifier.",
            "Do not answer the question.",
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "Select the complete required set of one to four bounded read-only "
                "evidence tools; call each once and do not answer."
            ),
        },
        {"role": "user", "content": canonical_json(routing_packet).decode("utf-8")},
    ]


def _trace_llm(
    trace: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
):
    if trace is None or not hasattr(trace, "llm"):
        return nullcontext(None)
    system = [str(item["content"]) for item in messages if item.get("role") == "system"]
    inputs = [item for item in messages if item.get("role") != "system"]
    return trace.llm(model=model, system_instructions=system, input_messages=inputs)


def _completion_projection(completion: InferenceCompletion) -> dict[str, Any]:
    return {
        "provider_call_id": completion.provider_call_id,
        "model": completion.model,
        "content": completion.content,
        "tool_calls": [
            {
                "call_id": item.call_id,
                "name": item.name,
                "arguments": item.arguments,
            }
            for item in completion.tool_calls
        ],
        "finish_reason": completion.finish_reason,
    }


def _weave_turn_receipt_projection(trace: Any) -> dict[str, str] | None:
    receipt = getattr(trace, "receipt", None)
    if receipt is None:
        return None
    value = {
        "conversation_id": str(getattr(receipt, "conversation_id", "") or ""),
        "trace_id": str(getattr(receipt, "trace_id", "") or ""),
        "root_span_id": str(getattr(receipt, "root_span_id", "") or ""),
        "url": str(getattr(receipt, "url", "") or ""),
    }
    if not all(value.values()):
        raise RuntimeError("PAIRED_WEAVE_TURN_RECEIPT_INVALID")
    return value


def _score_with_local_errors(
    score: Mapping[str, Any], local_errors: Sequence[str]
) -> dict[str, Any]:
    value = json.loads(json.dumps(score, sort_keys=True, default=str))
    if not local_errors:
        return value
    errors = list(value.get("errors", []))
    for code in local_errors:
        errors.append(
            {
                "code": code,
                "message": "the paired research branch failed local validation",
                "critical": "true",
            }
        )
    value["errors"] = errors
    value["error_codes"] = sorted({str(item["code"]) for item in errors})
    value["critical_failure"] = True
    value["passed"] = False
    return value


def _materialize_prose_output(
    *,
    case: Mapping[str, Any],
    routed_packet: Mapping[str, Any],
    retrieved: Sequence[Mapping[str, Any]],
    attempt_index: int,
    envelope: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    local_errors: list[str] = []
    expected_keys = {
        "interpretation_id",
        "response_mode",
        "answer_text",
        "evidence_ids",
        "limitation_ids",
        "action_ids",
    }
    if set(envelope) != expected_keys:
        raise ValueError("PAIRED_PROSE_RESPONSE_INVALID")
    response_mode = envelope.get("response_mode")
    interpretation_id = envelope.get("interpretation_id")
    answer_text = envelope.get("answer_text")
    evidence_ids = envelope.get("evidence_ids")
    limitation_ids = envelope.get("limitation_ids")
    action_ids = envelope.get("action_ids")
    if (
        not isinstance(interpretation_id, str)
        or response_mode not in {"answer", "refuse"}
        or not isinstance(answer_text, str)
    ):
        raise ValueError("PAIRED_PROSE_RESPONSE_INVALID")
    if not all(isinstance(value, list) for value in (evidence_ids, limitation_ids, action_ids)):
        raise ValueError("PAIRED_PROSE_RESPONSE_INVALID")
    if any(not isinstance(item, str) for values in (evidence_ids, limitation_ids, action_ids) for item in values):
        raise ValueError("PAIRED_PROSE_RESPONSE_INVALID")
    if any(len(values) != len(set(values)) for values in (evidence_ids, limitation_ids, action_ids)):
        local_errors.append("output_identifier_duplicate")

    resolved: list[dict[str, Any]] = []
    for evidence_id in evidence_ids:
        claim = _resolve_repository_claim(
            case,
            evidence_id=str(evidence_id),
            attempt_index=attempt_index,
        )
        if claim is None:
            local_errors.append("evidence_fabricated_or_wrong_subject")
            continue
        resolved.append(claim)
    unknown_limitations = [
        item for item in limitation_ids if item not in REQUIRED_LIMITATION_TEXT
    ]
    if unknown_limitations:
        local_errors.append("limitation_identifier_invalid")
    unknown_actions = [item for item in action_ids if item not in ALLOWED_ACTIONS]
    if unknown_actions:
        local_errors.append("action_unauthorized")

    intent = deterministic_question_intent(str(routed_packet.get("question") or ""))
    compatible = _intent_compatible_plan_ids(intent)
    if interpretation_id not in compatible:
        local_errors.append("interpretation_identifier_invalid")
    output = {
        "anchor": dict(case["anchor"]),
        "status": "answered" if response_mode == "answer" else "refused",
        "intent_id": intent,
        # Unlike the finite branch, this is the diagnostic model's own
        # interpretation. The repository never inserts the expected answer.
        "explanation_plan_id": interpretation_id,
        "answer": answer_text,
        "evidence_references": [
            {
                "evidence_id": item["evidence_id"],
                "claim_digest": item["claim_digest"],
            }
            for item in resolved
        ],
        "numeric_claims": [
            dict(item)
            for item in resolved
            if isinstance(item.get("value"), (int, float))
            and not isinstance(item.get("value"), bool)
        ],
        "limitations": [
            REQUIRED_LIMITATION_TEXT[item]
            for item in limitation_ids
            if item in REQUIRED_LIMITATION_TEXT
        ],
        "actions": list(action_ids),
        "read_only": True,
        "requires_engineer_confirmation": True,
        "renderer_version": "provider-free-prose-paired-diagnostic-v1",
        "diagnostic_scoring_plan_source": "provider_interpretation",
    }
    return output, tuple(sorted(set(local_errors)))


def _repository_scoring_plan(
    packet: Mapping[str, Any], retrieved: Sequence[Mapping[str, Any]]
) -> str:
    intent = deterministic_question_intent(str(packet.get("question") or ""))
    disposition = _repository_rule_disposition(packet, retrieved_evidence=retrieved)
    allowed = allowed_explanation_plan_ids(intent, rule_disposition=disposition)
    if len(allowed) != 1:
        raise RuntimeError("EVIDENCE_EXPLANATION_PLAN_UNAVAILABLE")
    return allowed[0]


def _intent_compatible_plan_ids(intent: str) -> list[str]:
    """Offer prose every plan compatible with the intent, not the gold side."""

    plans: set[str] = set()
    for disposition in (None, "pass", "fail"):
        try:
            plans.update(
                allowed_explanation_plan_ids(
                    intent,
                    rule_disposition=disposition,
                )
            )
        except RuntimeError as exc:
            if str(exc) != "EVIDENCE_RULE_DISPOSITION_MISSING":
                raise
    if not plans:
        raise RuntimeError("PAIRED_PROSE_INTERPRETATION_UNAVAILABLE")
    return sorted(plans)


class PairedArchitectureExecutor:
    """Execute one non-releasable A3-P/A0x-F research crossover."""

    def __init__(
        self,
        client: CompletionClient,
        *,
        entity: str,
        project: str,
        seed: int = 17,
        remap_index_resolver: Callable[[AttemptSpec], int] | None = None,
    ) -> None:
        if not entity or not project:
            raise ValueError("paired evaluation requires an explicit W&B destination")
        self.client = client
        self.entity = entity
        self.project = project
        self.seed = seed
        self.remap_index_resolver = remap_index_resolver or (
            lambda spec: int(
                hashlib.sha256(spec.attempt_id.encode("utf-8")).hexdigest()[:15], 16
            )
        )

    def execute(
        self,
        *,
        spec: AttemptSpec,
        dataset: Mapping[str, Any],
        case: Mapping[str, Any],
        observer: AttemptObserver,
    ) -> PairedExperimentalOutcome:
        if spec.case_id != case.get("case_id") or spec.model_id == "repository":
            raise ValueError("paired evaluation spec does not match the selected case/model")
        if spec.max_output_tokens != 128:
            raise ValueError("paired evaluation pins every provider call to 128 output tokens")
        attempt_index = self.remap_index_resolver(spec)
        if attempt_index < 0:
            raise ValueError("PAIRED_REMAP_INDEX_INVALID")
        packet = provider_packet(dataset, case, attempt_index)
        audit_provider_packet(dataset, case, packet, attempt_index=attempt_index)
        messages = _route_messages(packet)
        router_payload = messages[-1]["content"].encode("utf-8")
        order = branch_order(spec)
        started = time.perf_counter()

        with observer.attempt(spec, packet) as trace:
            route_completion = self._complete(
                spec=spec,
                trace=trace,
                messages=messages,
                tools=_tool_definitions(
                    sorted(str(name) for name in case.get("available_tools", []))
                ),
            )
            if not 1 <= len(route_completion.tool_calls) <= 4:
                raise RuntimeError("PAIRED_ROUTER_TOOL_COUNT_INVALID")
            available_tools = {str(name) for name in case.get("available_tools", [])}
            tool_names = [str(item.name) for item in route_completion.tool_calls]
            tool_call_ids = [str(item.call_id) for item in route_completion.tool_calls]
            if (
                len(tool_names) != len(set(tool_names))
                or len(tool_call_ids) != len(set(tool_call_ids))
                or any(not item for item in tool_call_ids)
                or any(name not in available_tools for name in tool_names)
                or any(item.arguments != {} for item in route_completion.tool_calls)
            ):
                raise RuntimeError("PAIRED_ROUTER_TOOL_INVALID")
            route_tool_calls = [
                {
                    "tool_call_id": item.call_id,
                    "tool_name": item.name,
                    "arguments": dict(item.arguments),
                }
                for item in route_completion.tool_calls
            ]
            route = RouteReceipt(
                provider_call_id=route_completion.provider_call_id,
                model_id=route_completion.model,
                tool_calls_json=_json_bytes(route_tool_calls),
                router_packet_sha256=_sha256(router_payload),
                provider_response_sha256=_sha256(
                    _json_bytes(_completion_projection(route_completion))
                ),
                input_tokens=route_completion.usage.input_tokens,
                output_tokens=route_completion.usage.output_tokens,
                transport_attempts=route_completion.transport_attempts,
                latency_ms=route_completion.latency_ms,
            )

            receipts: list[ToolReceipt] = []
            tool_events: list[dict[str, Any]] = []
            immutable_result: list[dict[str, Any]] = []
            seen_evidence: set[tuple[str, str]] = set()
            for tool_call in route_completion.tool_calls:
                result = _repository_tool_result(
                    case,
                    tool_name=tool_call.name,
                    attempt_index=attempt_index,
                )
                if trace is not None and hasattr(trace, "tool"):
                    with trace.tool(
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                        tool_call_id=tool_call.call_id,
                    ) as handle:
                        handle.record({"result": result})
                tool = ToolReceipt(
                    tool_call_id=tool_call.call_id,
                    tool_name=tool_call.name,
                    arguments_json=_json_bytes(tool_call.arguments),
                    result_json=_json_bytes(result),
                )
                receipts.append(tool)
                frozen_result = tool.result()
                tool_events.append(
                    {
                        "tool_name": tool.tool_name,
                        "arguments": tool.arguments(),
                        "result": frozen_result,
                    }
                )
                for item in frozen_result:
                    identity = (
                        str(item.get("evidence_id") or ""),
                        str(item.get("evidence_binding") or ""),
                    )
                    if identity not in seen_evidence:
                        immutable_result.append(dict(item))
                        seen_evidence.add(identity)
            required_plan = [
                str(name) for name in case.get("expectations", {}).get("required_tool_plan", [])
            ]
            router_plan_valid = (
                len(required_plan) == len(tool_names)
                and set(required_plan) == set(tool_names)
            )
            router_errors = () if router_plan_valid else ("router_tool_plan_mismatch",)
            routed_packet = provider_packet(
                dataset,
                case,
                attempt_index,
                tool_result=immutable_result,
            )
            audit_provider_packet(
                dataset,
                case,
                routed_packet,
                attempt_index=attempt_index,
            )
            intent = deterministic_question_intent(str(routed_packet.get("question") or ""))
            disposition = _repository_rule_disposition(
                routed_packet,
                retrieved_evidence=immutable_result,
            )
            allowed_plans = allowed_explanation_plan_ids(
                intent,
                rule_disposition=disposition,
            )
            evidence_packet = dict(routed_packet)
            evidence_packet["selected_intent"] = intent
            compact_evidence_packet = _compact_model_packet(evidence_packet)
            compact_evidence_packet["allowed_limitation_ids"] = sorted(
                REQUIRED_LIMITATION_TEXT
            )
            compact_evidence_packet["allowed_action_ids"] = sorted(ALLOWED_ACTIONS)
            evidence_payload = _json_bytes(compact_evidence_packet)
            evidence_payload_sha256 = _sha256(evidence_payload)

            branches: dict[str, BranchOutcome] = {}
            for branch_id in order:
                if branch_id == FINITE_BRANCH_ID:
                    branches[branch_id] = self._finite_branch(
                        spec=spec,
                        trace=trace,
                        dataset=dataset,
                        case=case,
                        attempt_index=attempt_index,
                        routed_packet=evidence_packet,
                        retrieved=immutable_result,
                        tool_events=tool_events,
                        local_errors=router_errors,
                        evidence_payload=evidence_payload,
                        evidence_payload_sha256=evidence_payload_sha256,
                        allowed_plans=allowed_plans,
                    )
                else:
                    branches[branch_id] = self._prose_branch(
                        spec=spec,
                        trace=trace,
                        dataset=dataset,
                        case=case,
                        attempt_index=attempt_index,
                        routed_packet=evidence_packet,
                        retrieved=immutable_result,
                        tool_events=tool_events,
                        local_errors=router_errors,
                        evidence_payload=evidence_payload,
                        evidence_payload_sha256=evidence_payload_sha256,
                    )

            finite = branches[FINITE_BRANCH_ID]
            prose = branches[PROSE_BRANCH_ID]
            valid = finite.passed and prose.passed
            errors = [
                _safe_error_code(f"{branch.branch_id}_{code}")
                for branch in (finite, prose)
                for code in branch.error_codes
            ]
            if trace is not None and hasattr(trace, "validation"):
                trace.validation(
                    valid=valid,
                    checked_claim_count=sum(
                        len(branch.output().get("evidence_references", []))
                        for branch in (finite, prose)
                    ),
                    error_codes=errors,
                )
            if trace is not None and hasattr(trace, "assert_operations"):
                trace.assert_operations(
                    llm_calls=3,
                    tool_calls=len(tool_events),
                    final_answer=False,
                )

        weave_turn_receipt = _weave_turn_receipt_projection(trace)

        return PairedExperimentalOutcome(
            attempt_id=spec.attempt_id,
            case_id=spec.case_id,
            model_id=spec.model_id,
            branch_order=order,
            route=route,
            tools=tuple(receipts),
            finite=finite,
            prose=prose,
            latency_ms=(time.perf_counter() - started) * 1_000,
            router_plan_valid=router_plan_valid,
            weave_turn_receipt=weave_turn_receipt,
        )

    def _complete(
        self,
        *,
        spec: AttemptSpec,
        trace: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> InferenceCompletion:
        with _trace_llm(trace, model=spec.model_id, messages=messages) as handle:
            completion = self.client.complete(
                entity=self.entity,
                project=self.project,
                model=spec.model_id,
                messages=messages,
                tools=tools,
                response_schema=response_schema,
                max_tokens=spec.max_output_tokens,
                seed=self.seed,
            )
            if completion.model != spec.model_id:
                raise RuntimeError("PAIRED_PROVIDER_MODEL_MISMATCH")
            if handle is not None:
                handle.record(completion)
            return completion

    def _finite_branch(
        self,
        *,
        spec: AttemptSpec,
        trace: Any,
        dataset: Mapping[str, Any],
        case: Mapping[str, Any],
        attempt_index: int,
        routed_packet: Mapping[str, Any],
        retrieved: Sequence[Mapping[str, Any]],
        tool_events: Sequence[Mapping[str, Any]],
        local_errors: Sequence[str],
        evidence_payload: bytes,
        evidence_payload_sha256: str,
        allowed_plans: Sequence[str],
    ) -> BranchOutcome:
        messages = _branch_messages(evidence_payload=evidence_payload, finite=True)
        completion = self._complete(
            spec=spec,
            trace=trace,
            messages=messages,
            response_schema=_plan_schema(allowed_plans),
        )
        try:
            parsed = _parse_json(completion.content, {"explanation_plan_id"})
            plan_id = str(parsed["explanation_plan_id"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return _invalid_branch_outcome(
                branch_id=FINITE_BRANCH_ID,
                completion=completion,
                evidence_payload_sha256=evidence_payload_sha256,
                error_code="finite_schema_invalid",
            )
        if plan_id not in allowed_plans:
            return _invalid_branch_outcome(
                branch_id=FINITE_BRANCH_ID,
                completion=completion,
                evidence_payload_sha256=evidence_payload_sha256,
                error_code="finite_plan_invalid",
            )
        output = render_repository_plan(
            routed_packet,
            case,
            plan_id,
            attempt_index=attempt_index,
            retrieved_evidence=retrieved,
        )
        base_score = score_attempt(
            dataset,
            case,
            output,
            attempt_index=attempt_index,
            tool_events=tool_events,
            agent_variant_id="A3",
        )
        score = _score_with_local_errors(base_score, local_errors)
        return BranchOutcome(
            branch_id=FINITE_BRANCH_ID,
            provider_call_id=completion.provider_call_id,
            evidence_payload_sha256=evidence_payload_sha256,
            output_json=_json_bytes(output),
            score_json=_json_bytes(score),
            passed=bool(score["passed"]),
            critical_failure=bool(score["critical_failure"]),
            error_codes=tuple(map(str, score["error_codes"])),
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            transport_attempts=completion.transport_attempts,
            latency_ms=completion.latency_ms,
            selected_plan_id=plan_id,
            response_mode="answer" if output["status"] == "answered" else "refuse",
        )

    def _prose_branch(
        self,
        *,
        spec: AttemptSpec,
        trace: Any,
        dataset: Mapping[str, Any],
        case: Mapping[str, Any],
        attempt_index: int,
        routed_packet: Mapping[str, Any],
        retrieved: Sequence[Mapping[str, Any]],
        tool_events: Sequence[Mapping[str, Any]],
        local_errors: Sequence[str],
        evidence_payload: bytes,
        evidence_payload_sha256: str,
    ) -> BranchOutcome:
        messages = _branch_messages(evidence_payload=evidence_payload, finite=False)
        completion = self._complete(
            spec=spec,
            trace=trace,
            messages=messages,
            response_schema=_prose_schema(
                _intent_compatible_plan_ids(
                    deterministic_question_intent(
                        str(routed_packet.get("question") or "")
                    )
                )
            ),
        )
        try:
            envelope = _parse_json(
                completion.content,
                {
                    "response_mode",
                    "interpretation_id",
                    "answer_text",
                    "evidence_ids",
                    "limitation_ids",
                    "action_ids",
                },
            )
            output, envelope_errors = _materialize_prose_output(
                case=case,
                routed_packet=routed_packet,
                retrieved=retrieved,
                attempt_index=attempt_index,
                envelope=envelope,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return _invalid_branch_outcome(
                branch_id=PROSE_BRANCH_ID,
                completion=completion,
                evidence_payload_sha256=evidence_payload_sha256,
                error_code="prose_schema_invalid",
            )
        base_score = score_attempt(
            dataset,
            case,
            output,
            attempt_index=attempt_index,
            tool_events=tool_events,
            agent_variant_id=PROSE_BRANCH_ID,
        )
        score = _score_with_local_errors(base_score, (*local_errors, *envelope_errors))
        return BranchOutcome(
            branch_id=PROSE_BRANCH_ID,
            provider_call_id=completion.provider_call_id,
            evidence_payload_sha256=evidence_payload_sha256,
            output_json=_json_bytes(output),
            score_json=_json_bytes(score),
            passed=bool(score["passed"]),
            critical_failure=bool(score["critical_failure"]),
            error_codes=tuple(map(str, score["error_codes"])),
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            transport_attempts=completion.transport_attempts,
            latency_ms=completion.latency_ms,
            selected_plan_id=str(envelope["interpretation_id"]),
            response_mode=str(envelope["response_mode"]),
        )
