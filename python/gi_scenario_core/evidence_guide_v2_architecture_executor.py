"""Release-ineligible real executor for the Evidence Guide architecture study.

The adapter keeps the sealed release campaign untouched.  It dispatches the
product-parity controls, the paired A3X crossover, and the causal synthesis
cells while guaranteeing that no generated answer is released or promoted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from .evidence_evaluation_v2 import (
    ALLOWED_ACTIONS,
    REQUIRED_LIMITATION_TEXT,
    _prohibited_claim_hits,
    audit_provider_packet,
    extract_quantities,
    provider_packet,
    unexplained_numeric_tokens,
)
from .evidence_guide_v2_campaign import (
    MAX_INPUT_TOKENS_PER_CALL,
    AttemptObserver,
    AttemptOutcome,
    AttemptSpec,
    Corpus,
    ModelPrice,
    canonical_json,
    json_sha256,
)
from .evidence_guide_v2_paired import (
    PROSE_BRANCH_ID,
    PairedArchitectureExecutor,
    PairedExperimentalOutcome,
    _branch_messages,
    _compact_model_packet,
    _intent_compatible_plan_ids,
    _parse_json,
    _plan_schema,
    _prose_schema,
    _repository_tool_result,
    _trace_llm,
    _weave_turn_receipt_projection,
)
from .evidence_guide_v2_protocol import EXPLANATION_PLANS, deterministic_question_intent
from .evidence_guide_v2_provider import EvidenceGuideV2ProviderExecutor
from .inference import (
    InferenceCompletion,
    InferenceRequestError,
    InferenceToolCall,
    InferenceUsage,
    build_inference_request_payload,
)

RESEARCH_EXECUTOR_VERSION = "evidence-guide-architecture-development-executor-r3"
RESEARCH_REQUEST_BYTE_CAP = 64 * 1024
RESEARCH_PROVIDER_INPUT_TOKEN_CAP = MAX_INPUT_TOKENS_PER_CALL
CAUSAL_ARCHITECTURES = frozenset({"D1", "A1", "A3P", "A0xF"})
_FAILED_CODE = re.compile(r"[^A-Z0-9_]+")


def research_remap_index(spec: AttemptSpec) -> int:
    """Use one opaque-ID namespace across architecture siblings.

    Architecture, stage, causal condition, global schedule ordinal, and time
    block are deliberately excluded. The same model/case/presentation triad
    therefore keeps one opaque namespace across its original, counterfactual,
    and withheld-evidence siblings.
    """

    digest = json_sha256(
        {
            "split": spec.split,
            "case_id": spec.case_id,
            "project_family": spec.project_family,
            "model_id": spec.model_id,
            "repeat": spec.repeat,
        }
    )
    return int(digest[:15], 16)


def _safe_error_code(error: BaseException) -> str:
    if isinstance(error, InferenceRequestError):
        return str(error.code)[:64]
    text = str(error)
    if text and len(text) <= 64 and text.replace("_", "").isalnum():
        return text
    normalized = _FAILED_CODE.sub("_", type(error).__name__.upper()).strip("_")
    return f"RESEARCH_{normalized or 'EXECUTION_FAILED'}"[:64]


class _AttemptMeter:
    """Per-attempt client wrapper retaining safe billing and retry facts."""

    def __init__(
        self,
        client: Any,
        *,
        max_output_tokens: int,
        max_input_tokens: int = MAX_INPUT_TOKENS_PER_CALL,
    ) -> None:
        if max_input_tokens <= 0 or max_output_tokens <= 0:
            raise ValueError("attempt token limits must be positive")
        self.client = client
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.logical_calls = 0
        self.transport_attempts = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.input_token_envelope = 0
        self.output_token_envelope = 0
        self.request_bytes = 0
        self.request_bytes_transport_envelope = 0
        self.completions: list[InferenceCompletion] = []

    def complete(self, **kwargs: Any) -> InferenceCompletion:
        self.logical_calls += 1
        try:
            completion = self.client.complete(**kwargs)
        except InferenceRequestError as exc:
            self.transport_attempts += exc.transport_attempts
            # A failed transport has no authoritative provider token report.
            # Reserve the independently pinned token cap for every transport;
            # request bytes remain a separate transport diagnostic.
            self.input_token_envelope += self.max_input_tokens * exc.transport_attempts
            self.request_bytes += exc.request_bytes
            self.request_bytes_transport_envelope += (
                exc.request_bytes * exc.transport_attempts
            )
            self.output_token_envelope += (
                self.max_output_tokens * exc.transport_attempts
            )
            raise
        self.completions.append(completion)
        self.transport_attempts += completion.transport_attempts
        self.input_tokens += completion.usage.input_tokens
        self.output_tokens += completion.usage.output_tokens
        self.input_token_envelope += (
            completion.usage.input_tokens * completion.transport_attempts
        )
        self.request_bytes += completion.request_bytes
        self.request_bytes_transport_envelope += (
            completion.request_bytes * completion.transport_attempts
        )
        self.output_token_envelope += (
            self.max_output_tokens * completion.transport_attempts
        )
        if completion.usage.input_tokens > self.max_input_tokens:
            raise _PostReceiptResourceViolation(
                resource="input_tokens",
                observed=completion.usage.input_tokens,
                limit=self.max_input_tokens,
                completion=completion,
            )
        if completion.usage.output_tokens > self.max_output_tokens:
            raise _PostReceiptResourceViolation(
                resource="output_tokens",
                observed=completion.usage.output_tokens,
                limit=self.max_output_tokens,
                completion=completion,
            )
        return completion

    def request_accounting(self) -> dict[str, Any]:
        return {
            "request_utf8_bytes_observed": self.request_bytes,
            "request_utf8_bytes_transport_envelope": (
                self.request_bytes_transport_envelope
            ),
            "input_token_envelope_basis": (
                "reported_input_tokens_times_transport_attempts_on_completion; "
                f"{self.max_input_tokens}_tokens_per_transport_attempt_on_provider_error"
            ),
            "maximum_input_tokens_per_transport": self.max_input_tokens,
            "maximum_output_tokens_per_transport": self.max_output_tokens,
            "request_bytes_used_as_token_count": False,
        }

    def verified_usage_cost(self, price: ModelPrice) -> Decimal:
        """Price only provider-reported usage from completed responses.

        The provider reports tokens, not an invoice line item, so this is a
        price-snapshot projection over verified response usage.  Failed
        transports deliberately contribute zero here because their billable
        usage is unknown.
        """

        return sum(
            (
                price.worst_case(
                    input_tokens=item.usage.input_tokens,
                    output_tokens=item.usage.output_tokens,
                )
                for item in self.completions
            ),
            Decimal(0),
        )

    def conservative_at_risk_cost(self, price: ModelPrice) -> Decimal:
        """Price the retry/error token envelopes without calling them billed."""

        return price.worst_case(
            input_tokens=self.input_token_envelope,
            output_tokens=self.output_token_envelope,
        )

    def cost_accounting(self, price: ModelPrice) -> dict[str, Any]:
        return {
            "verified_provider_usage_cost_usd": str(self.verified_usage_cost(price)),
            "conservative_at_risk_cost_usd": str(
                self.conservative_at_risk_cost(price)
            ),
            "verified_cost_basis": (
                "pricing_snapshot_times_provider_reported_tokens_from_completed_responses"
            ),
            "at_risk_cost_basis": (
                "pricing_snapshot_times_retry_and_error_token_envelopes"
            ),
            "provider_reported_currency_cost_available": False,
            "failed_transport_reservation_is_billed_cost": False,
        }

    # Compatibility for AttemptOutcome.cost_usd.  Its R3 interpretation is the
    # verified provider-usage projection, never the conservative reservation.
    def cost(self, price: ModelPrice) -> Decimal:
        return self.verified_usage_cost(price)


class _PostReceiptResourceViolation(RuntimeError):
    """A completed provider response that violates a local resource policy.

    The completion is intentionally retained on the exception.  Callers that
    distinguish scientific model behavior from operational policy can trace,
    parse, and score the immutable response while still marking the complete
    system outcome as resource-policy invalid.
    """

    def __init__(
        self,
        *,
        resource: str,
        observed: int,
        limit: int,
        completion: InferenceCompletion,
    ) -> None:
        if resource not in {"input_tokens", "output_tokens"}:
            raise ValueError("unsupported post-receipt resource")
        self.resource = resource
        self.observed = observed
        self.limit = limit
        self.completion = completion
        legacy_code = (
            "R2_PROVIDER_INPUT_TOKEN_CAP_EXCEEDED"
            if resource == "input_tokens"
            else "R2_PROVIDER_OUTPUT_TOKEN_CAP_EXCEEDED"
        )
        self.error_code = f"post_receipt_{resource}_cap_exceeded"
        super().__init__(legacy_code)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "resource": self.resource,
            "observed": self.observed,
            "limit": self.limit,
            "provider_response_preserved": True,
        }


def _branch_cost(price: ModelPrice, outcome: Any) -> str:
    return str(
        price.worst_case(
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
        )
    )


def build_semantic_intervention_receipt(
    *,
    spec: AttemptSpec,
    packet: Mapping[str, Any],
    tool_name: str,
    original_evidence: Sequence[Mapping[str, Any]],
    intervention_evidence: Sequence[Mapping[str, Any]],
    condition: str,
    operation_sequence: Sequence[str],
    response_contract_id: str,
) -> dict[str, Any]:
    """Seal the exact semantic delta used by one causal-stage cell.

    This is a domain-separated content-hash seal, not an identity signature.
    Its purpose is reproducible intervention integrity: triad analysis can
    reject any row where an anchor, opaque identifier, unit, binding, tool, or
    orchestration contract changed alongside the intended value/availability.
    """

    if condition not in {
        "evidence_original",
        "counterfactual_threshold_crossing",
        "evidence_withheld",
    }:
        raise RuntimeError("RESEARCH_SEMANTIC_INTERVENTION_CONDITION_INVALID")
    if len(original_evidence) != 1:
        raise RuntimeError("RESEARCH_SEMANTIC_INTERVENTION_ORIGINAL_INVALID")
    original = dict(original_evidence[0])
    intervention = [dict(item) for item in intervention_evidence]
    if condition == "evidence_withheld":
        intended_delta = "availability_only"
        delta_valid = not intervention
    elif len(intervention) != 1:
        intended_delta = "value_only" if condition.startswith("counterfactual") else "none"
        delta_valid = False
    else:
        intended_delta = (
            "value_only"
            if condition == "counterfactual_threshold_crossing"
            else "none"
        )
        candidate = intervention[0]
        non_value_original = {key: value for key, value in original.items() if key != "value"}
        non_value_candidate = {
            key: value for key, value in candidate.items() if key != "value"
        }
        delta_valid = bool(
            non_value_candidate == non_value_original
            and (
                candidate.get("value") != original.get("value")
                if intended_delta == "value_only"
                else candidate.get("value") == original.get("value")
            )
        )
    anchor = packet.get("anchor")
    if not isinstance(anchor, Mapping):
        raise TypeError("RESEARCH_SEMANTIC_INTERVENTION_ANCHOR_INVALID")
    required_anchor = ("project_id", "revision_id", "analysis_id", "subject_id")
    anchor_valid = all(isinstance(anchor.get(key), str) and anchor.get(key) for key in required_anchor)
    tool_valid = tool_name in tuple(
        str(item) for item in packet.get("available_tools", ())
    )
    identity_valid = all(
        isinstance(original.get(key), str) and original.get(key)
        for key in ("evidence_id", "units", "evidence_binding")
    )
    orchestration_contract = {
        "operation_sequence": list(operation_sequence),
        "response_contract_id": response_contract_id,
        "condition_label_visible_to_model": False,
    }
    invariants = {
        "anchor_complete": anchor_valid,
        "opaque_evidence_id_present": bool(original.get("evidence_id")),
        "unit_present": bool(original.get("units")),
        "evidence_binding_present": bool(original.get("evidence_binding")),
        "required_tool_available": tool_valid,
        "only_intended_semantic_delta": delta_valid,
        "condition_label_visible_to_model": False,
    }
    clean = identity_valid and all(
        value is True for key, value in invariants.items() if key != "condition_label_visible_to_model"
    ) and invariants["condition_label_visible_to_model"] is False
    body = {
        "schema_version": "1.0",
        "receipt_version": "semantic-intervention-r3",
        "condition": condition,
        "triad_identity": {
            "model_id": spec.model_id,
            "split": spec.split,
            "case_id": spec.case_id,
            "presentation": spec.repeat,
            "architecture_id": spec.architecture_id,
        },
        "anchor": {key: str(anchor.get(key) or "") for key in required_anchor},
        "anchor_sha256": json_sha256(dict(anchor)),
        "tool_name": tool_name,
        "opaque_evidence_id": str(original.get("evidence_id") or ""),
        "units": str(original.get("units") or ""),
        "evidence_binding": str(original.get("evidence_binding") or ""),
        "original_value": original.get("value"),
        "intervention_value": (
            intervention[0].get("value") if len(intervention) == 1 else None
        ),
        "evidence_available": bool(intervention),
        "intended_delta": intended_delta,
        "orchestration_contract": orchestration_contract,
        "orchestration_contract_sha256": json_sha256(orchestration_contract),
        "invariants": invariants,
        "clean": clean,
        "seal_kind": "domain_separated_sha256_content_seal_not_identity_signature",
    }
    body_sha256 = json_sha256(body)
    seal = hashlib.sha256(
        b"gi-scenario-studio:semantic-intervention-r3:\x00"
        + canonical_json(body)
    ).hexdigest()
    receipt = {**body, "body_sha256": body_sha256, "integrity_seal_sha256": seal}
    if not clean:
        raise RuntimeError("RESEARCH_SEMANTIC_INTERVENTION_DIRTY")
    return receipt


def validate_semantic_intervention_receipt(value: Mapping[str, Any]) -> bool:
    """Validate a semantic intervention seal before scientific analysis."""

    body = {
        key: item
        for key, item in value.items()
        if key not in {"body_sha256", "integrity_seal_sha256"}
    }
    expected_seal = hashlib.sha256(
        b"gi-scenario-studio:semantic-intervention-r3:\x00"
        + canonical_json(body)
    ).hexdigest()
    return bool(
        value.get("clean") is True
        and value.get("body_sha256") == json_sha256(body)
        and value.get("integrity_seal_sha256") == expected_seal
        and value.get("seal_kind")
        == "domain_separated_sha256_content_seal_not_identity_signature"
    )


def _branch_record(
    branch: Any,
    *,
    trace_acknowledged: bool,
    price: ModelPrice,
    router_accuracy: bool,
) -> dict[str, Any]:
    score = branch.score()
    output = branch.output()
    errors = {str(item) for item in branch.error_codes}
    false_refusal = bool(
        output.get("status") == "refused"
        and score.get("scenario_type") not in {"unsupported", "injection_fabrication"}
    )
    plan_accuracy = "explanation_plan_mismatch" not in errors
    evidence_valid = not any(
        marker in code
        for code in errors
        for marker in ("evidence", "anchor", "required_tool")
    )
    candidate_conformant = bool(branch.passed and trace_acknowledged)
    return {
        "status": "valid" if branch.passed else "invalid",
        "protocol_valid": True,
        "raw_schema_valid": True,
        "raw_protocol_valid": True,
        "raw_invalid": not branch.passed,
        "raw_candidate_invalid": not branch.passed,
        "canonical_correct": branch.passed,
        "safe": not branch.critical_failure,
        "evidence_valid": evidence_valid,
        "repository_fidelity": branch.passed,
        "injection_safe": branch.passed,
        "answer_released": False,
        "candidate_conformant": candidate_conformant,
        "hypothetical_release_eligible": candidate_conformant,
        "false_refusal": false_refusal,
        "causal": bool(score.get("causal_behavior_passed")),
        "intent_correct": bool(score.get("intent_correct")),
        "router_accuracy": router_accuracy,
        "tool_accuracy": router_accuracy,
        "plan_accuracy": plan_accuracy,
        "explanation_plan_id": branch.selected_plan_id,
        "interpretation_id": (
            branch.selected_plan_id if branch.branch_id == PROSE_BRANCH_ID else None
        ),
        "trace_acknowledged": trace_acknowledged,
        "usage": {
            "input_tokens": branch.input_tokens,
            "output_tokens": branch.output_tokens,
            "cost_usd": _branch_cost(price, branch),
        },
        "latency_ms": branch.latency_ms,
        "error_codes": list(branch.error_codes),
        "output_sha256": hashlib.sha256(branch.output_json).hexdigest(),
        "score_sha256": hashlib.sha256(branch.score_json).hexdigest(),
    }


def _paired_outcome(
    spec: AttemptSpec,
    paired: PairedExperimentalOutcome,
    *,
    meter: _AttemptMeter,
    price: ModelPrice,
    case: Mapping[str, Any],
) -> AttemptOutcome:
    trace_acknowledged = paired.weave_turn_receipt is not None
    finite = _branch_record(
        paired.finite,
        trace_acknowledged=trace_acknowledged,
        price=price,
        router_accuracy=paired.router_plan_valid,
    )
    prose = _branch_record(
        paired.prose,
        trace_acknowledged=trace_acknowledged,
        price=price,
        router_accuracy=paired.router_plan_valid,
    )
    errors = tuple(
        sorted(
            {
                f"{name}_{code}".upper().replace("-", "_")
                for name, branch in (("A3P", paired.finite), ("A0xF", paired.prose))
                for code in branch.error_codes
            }
        )
    )
    safety_critical = case.get("scenario_type") in {
        "unsupported",
        "injection_fabrication",
    }
    branch_rows = {"A3P": finite, "A0xF": prose}
    all_safe = all(bool(item["safe"]) for item in branch_rows.values())
    all_evidence = all(bool(item["evidence_valid"]) for item in branch_rows.values())
    all_fidelity = all(bool(item["repository_fidelity"]) for item in branch_rows.values())
    all_causal = all(bool(item["causal"]) for item in branch_rows.values())
    all_intent = all(bool(item["intent_correct"]) for item in branch_rows.values())
    candidate_conformant = all(
        bool(item["candidate_conformant"]) for item in branch_rows.values()
    )
    return AttemptOutcome(
        attempt_id=spec.attempt_id,
        status="completed",
        provider_invocations=meter.logical_calls,
        transport_attempts=meter.transport_attempts,
        input_tokens=meter.input_tokens,
        output_tokens=meter.output_tokens,
        cost_usd=meter.cost(price),
        latency_ms=paired.latency_ms,
        protocol_valid=True,
        safe=all_safe,
        evidence_valid=all_evidence,
        repository_fidelity=all_fidelity,
        injection_safe=(all_safe if safety_critical else True),
        causal=(all_causal if case.get("scenario_type") == "causal_retrieval" else None),
        intent_correct=all_intent,
        safety_intent_correct=(all_intent if safety_critical else None),
        false_refusal=any(bool(item["false_refusal"]) for item in branch_rows.values()),
        trace_acknowledged=trace_acknowledged,
        input_token_envelope=meter.input_token_envelope,
        output_token_envelope=meter.output_token_envelope,
        explanation_plan_id=paired.finite.selected_plan_id,
        error_codes=errors,
        details={
            "executor_version": RESEARCH_EXECUTOR_VERSION,
            "production_eligible": False,
            "answer_released": False,
            "candidate_conformant": candidate_conformant,
            "hypothetical_release_eligible": candidate_conformant,
            "raw_candidate_invalid": not candidate_conformant,
            "shared_router_tool_receipt_sha256": paired.shared_router_tool_receipt_sha256,
            "shared_router_usage": {
                "input_tokens": paired.route.input_tokens,
                "output_tokens": paired.route.output_tokens,
                "cost_usd": _branch_cost(price, paired.route),
                "latency_ms": paired.route.latency_ms,
            },
            "router_plan_valid": paired.router_plan_valid,
            "tool_receipts": [item.to_record() for item in paired.tools],
            "paired_branch_outcomes": branch_rows,
            "weave_turn_receipt": dict(paired.weave_turn_receipt or {}),
            "request_accounting": meter.request_accounting(),
            "cost_accounting": meter.cost_accounting(price),
            "answer_release_blocked_by_research_protocol": True,
        },
    )


def _conditioned_causal_evidence(
    case: Mapping[str, Any],
    *,
    attempt_index: int,
    condition: str,
) -> tuple[str, list[dict[str, Any]], str]:
    tool_name = str(case["expectations"]["required_tool_plan"][0])
    native = _repository_tool_result(
        case,
        tool_name=tool_name,
        attempt_index=attempt_index,
        # Causal fixtures deliberately place exactly one target behind the
        # repository query while catalog claims remain visible distractors.
        # Ordinary tool execution may return both partitions; the intervention
        # must mutate only the hidden target.
        hidden_only=True,
    )
    if condition == "evidence_original":
        if len(native) != 1:
            raise RuntimeError("RESEARCH_CAUSAL_CONDITION_INVALID")
        return (
            tool_name,
            native,
            str(case["expectations"]["explanation_plan_id"]),
        )
    if condition == "evidence_withheld":
        return tool_name, [], "unsupported"
    if condition != "counterfactual_threshold_crossing" or len(native) != 1:
        raise RuntimeError("RESEARCH_CAUSAL_CONDITION_INVALID")
    threshold = float(case["expectations"]["causal_rule"]["threshold"])
    original = float(native[0]["value"])
    margin = max(abs(original - threshold), 0.01)
    if original > threshold:
        crossed = threshold - margin
        expected = str(case["expectations"]["causal_rule"]["nonpositive_plan_id"])
    else:
        crossed = threshold + margin
        expected = str(case["expectations"]["causal_rule"]["positive_plan_id"])
    evidence = dict(native[0])
    evidence["value"] = crossed
    return tool_name, [evidence], expected


class ArchitectureDevelopmentExecutor:
    """Composite AttemptExecutor for the permanently exploratory study."""

    def __init__(
        self,
        *,
        corpus: Corpus,
        provider: Any,
        entity: str,
        project: str,
        pricing: Mapping[str, ModelPrice],
    ) -> None:
        self.corpus = corpus
        self.provider = provider
        self.entity = entity
        self.project = project
        self.pricing = dict(pricing)
        self.product = EvidenceGuideV2ProviderExecutor(
            corpus=corpus,
            provider=provider,
            entity=entity,
            project=project,
            pricing=pricing,
            remap_index_resolver=research_remap_index,
            allow_answer_release=False,
        )

    def execute(
        self,
        *,
        spec: AttemptSpec,
        case: Mapping[str, Any],
        observer: AttemptObserver,
    ) -> AttemptOutcome:
        if spec.model_id != "repository" and spec.model_id not in self.pricing:
            raise RuntimeError("V2_MODEL_PRICE_UNAVAILABLE")
        if spec.stage == "causal_perturbation" and spec.architecture_id in CAUSAL_ARCHITECTURES:
            return self._execute_causal(spec=spec, case=case, observer=observer)
        if spec.architecture_id == "A3X":
            return self._execute_paired(spec=spec, case=case, observer=observer)
        if spec.architecture_id not in {"D0", "D1", "A1"}:
            raise RuntimeError("RESEARCH_ARCHITECTURE_UNSUPPORTED")
        outcome = self.product.execute(spec=spec, case=case, observer=observer)
        candidate_conformant = self._candidate_conformant(outcome)
        price = self.pricing.get(spec.model_id)
        if price is None:
            cost_accounting = {
                "verified_provider_usage_cost_usd": "0",
                "conservative_at_risk_cost_usd": "0",
                "verified_cost_basis": "no_provider_call",
                "at_risk_cost_basis": "no_provider_call",
                "provider_reported_currency_cost_available": False,
                "failed_transport_reservation_is_billed_cost": False,
            }
        else:
            cost_accounting = {
                "verified_provider_usage_cost_usd": str(outcome.cost_usd),
                "conservative_at_risk_cost_usd": str(
                    price.worst_case(
                        input_tokens=outcome.input_token_envelope,
                        output_tokens=outcome.output_token_envelope,
                    )
                ),
                "verified_cost_basis": (
                    "pricing_snapshot_times_provider_reported_tokens_from_completed_responses"
                ),
                "at_risk_cost_basis": (
                    "pricing_snapshot_times_retry_and_error_token_envelopes"
                ),
                "provider_reported_currency_cost_available": False,
                "failed_transport_reservation_is_billed_cost": False,
            }
        details = {
            **dict(outcome.details),
            "upstream_answer_release_flag": bool(
                outcome.details.get("answer_released")
            ),
            "executor_version": RESEARCH_EXECUTOR_VERSION,
            "production_eligible": False,
            "answer_released": False,
            "candidate_conformant": candidate_conformant,
            "hypothetical_release_eligible": candidate_conformant,
            "raw_candidate_invalid": not candidate_conformant,
            "cost_accounting": cost_accounting,
            "answer_release_blocked_by_research_protocol": True,
        }
        return AttemptOutcome(**{**outcome.__dict__, "details": details})

    def _execute_paired(
        self,
        *,
        spec: AttemptSpec,
        case: Mapping[str, Any],
        observer: AttemptObserver,
    ) -> AttemptOutcome:
        meter = _AttemptMeter(self.provider, max_output_tokens=spec.max_output_tokens)
        price = self.pricing[spec.model_id]
        try:
            paired = PairedArchitectureExecutor(
                meter,
                entity=self.entity,
                project=self.project,
                remap_index_resolver=research_remap_index,
            ).execute(
                spec=spec,
                dataset=self.corpus.datasets[spec.split],
                case=case,
                observer=observer,
            )
            return _paired_outcome(spec, paired, meter=meter, price=price, case=case)
        except Exception as exc:  # noqa: BLE001 - research faults always fail closed
            return self._failed_outcome(spec, meter=meter, price=price, error=exc)

    def _execute_causal(
        self,
        *,
        spec: AttemptSpec,
        case: Mapping[str, Any],
        observer: AttemptObserver,
    ) -> AttemptOutcome:
        if spec.architecture_id == "D1":
            return self._execute_causal_d1(spec=spec, case=case, observer=observer)
        meter = _AttemptMeter(self.provider, max_output_tokens=spec.max_output_tokens)
        price = self.pricing[spec.model_id]
        started = time.perf_counter()
        attempt_index = research_remap_index(spec)
        dataset = self.corpus.datasets[spec.split]
        packet = provider_packet(dataset, case, attempt_index)
        audit_provider_packet(dataset, case, packet, attempt_index=attempt_index)
        condition = str(spec.ablation_condition or "")
        semantic_intervention_receipt: dict[str, Any] | None = None
        try:
            original_evidence = _repository_tool_result(
                case,
                tool_name=str(case["expectations"]["required_tool_plan"][0]),
                attempt_index=attempt_index,
                hidden_only=True,
            )
            tool_name, evidence, expected_interpretation = _conditioned_causal_evidence(
                case,
                attempt_index=attempt_index,
                condition=condition,
            )
            semantic_intervention_receipt = build_semantic_intervention_receipt(
                spec=spec,
                packet=packet,
                tool_name=tool_name,
                original_evidence=original_evidence,
                intervention_evidence=evidence,
                condition=condition,
                operation_sequence=(
                    "repository_tool",
                    "synthesis_call",
                    "deterministic_validation",
                ),
                response_contract_id=(
                    "finite_plan_v1"
                    if spec.architecture_id in {"A1", "A3P"}
                    else "diagnostic_prose_v1"
                ),
            )
            routed_packet = provider_packet(
                dataset,
                case,
                attempt_index,
                tool_result=evidence,
            )
            audit_provider_packet(
                dataset,
                case,
                routed_packet,
                attempt_index=attempt_index,
            )
            intent = deterministic_question_intent(str(packet.get("question") or ""))
            evidence_packet = _compact_model_packet(
                {
                    **routed_packet,
                    "selected_intent": intent,
                }
            )
            evidence_packet["allowed_limitation_ids"] = sorted(
                REQUIRED_LIMITATION_TEXT
            )
            evidence_packet["allowed_action_ids"] = sorted(ALLOWED_ACTIONS)
            evidence_payload = canonical_json(evidence_packet)
            shared_digest = hashlib.sha256(evidence_payload).hexdigest()
            with observer.attempt(spec, packet) as trace:
                call_id = f"research-tool-{shared_digest[:20]}"
                if trace is not None and hasattr(trace, "tool"):
                    with trace.tool(
                        name=tool_name,
                        arguments={},
                        tool_call_id=call_id,
                    ) as handle:
                        handle.record({"result": evidence})
                completion, interpretation, raw = self._causal_completion(
                    meter=meter,
                    spec=spec,
                    trace=trace,
                    intent=intent,
                    evidence_payload=evidence_payload,
                    condition=condition,
                    expected_interpretation=expected_interpretation,
                )
                validation = self._validate_causal_response(
                    spec=spec,
                    case=case,
                    evidence=evidence,
                    expected_interpretation=expected_interpretation,
                    interpretation=interpretation,
                    raw=raw,
                )
                if trace is not None and hasattr(trace, "validation"):
                    trace.validation(
                        valid=validation["passed"],
                        checked_claim_count=len(evidence),
                        error_codes=validation["error_codes"],
                    )
                if trace is not None and hasattr(trace, "assert_operations"):
                    trace.assert_operations(llm_calls=1, tool_calls=1, final_answer=False)
            receipt = _weave_turn_receipt_projection(trace)
            trace_acknowledged = receipt is not None
            usage = {
                "input_tokens": completion.usage.input_tokens,
                "output_tokens": completion.usage.output_tokens,
                "cost_usd": str(meter.cost(price)),
            }
            branch = {
                "status": "valid" if validation["passed"] else "invalid",
                "protocol_valid": True,
                "raw_schema_valid": True,
                "raw_protocol_valid": True,
                "raw_invalid": not validation["passed"],
                "raw_candidate_invalid": not validation["passed"],
                "canonical_correct": validation["passed"],
                "safe": validation["safe"],
                "evidence_valid": validation["evidence_valid"],
                "repository_fidelity": validation["passed"],
                "injection_safe": validation["safe"],
                "answer_released": False,
                "candidate_conformant": bool(
                    validation["passed"] and trace_acknowledged
                ),
                "hypothetical_release_eligible": bool(
                    validation["passed"] and trace_acknowledged
                ),
                "false_refusal": validation["false_refusal"],
                "causal": validation["causal"],
                "intent_correct": True,
                "router_accuracy": True,
                "tool_accuracy": True,
                "plan_accuracy": validation["causal"],
                "explanation_plan_id": (
                    interpretation if spec.architecture_id != "A0xF" else None
                ),
                "interpretation_id": interpretation,
                "trace_acknowledged": trace_acknowledged,
                "usage": usage,
                "latency_ms": completion.latency_ms,
                "error_codes": validation["error_codes"],
                "raw_response_sha256": hashlib.sha256(completion.content.encode()).hexdigest(),
            }
            return AttemptOutcome(
                attempt_id=spec.attempt_id,
                status="completed",
                provider_invocations=meter.logical_calls,
                transport_attempts=meter.transport_attempts,
                input_tokens=meter.input_tokens,
                output_tokens=meter.output_tokens,
                cost_usd=meter.cost(price),
                latency_ms=(time.perf_counter() - started) * 1_000,
                protocol_valid=True,
                safe=validation["safe"],
                evidence_valid=validation["evidence_valid"],
                repository_fidelity=validation["passed"],
                injection_safe=validation["safe"],
                causal=validation["causal"],
                intent_correct=True,
                safety_intent_correct=None,
                false_refusal=validation["false_refusal"],
                trace_acknowledged=trace_acknowledged,
                input_token_envelope=meter.input_token_envelope,
                output_token_envelope=meter.output_token_envelope,
                explanation_plan_id=(
                    interpretation if interpretation != "unsupported" else None
                ),
                error_codes=tuple(validation["error_codes"]),
                details={
                    "executor_version": RESEARCH_EXECUTOR_VERSION,
                    "production_eligible": False,
                    "answer_released": False,
                    "candidate_conformant": bool(
                        validation["passed"] and trace_acknowledged
                    ),
                    "hypothetical_release_eligible": bool(
                        validation["passed"] and trace_acknowledged
                    ),
                    "research_condition": condition,
                    "shared_evidence_payload_sha256": shared_digest,
                    "shared_remap_index_sha256": hashlib.sha256(
                        str(attempt_index).encode()
                    ).hexdigest(),
                    "operation_sequence": [
                        "repository_tool",
                        "synthesis_call",
                        "deterministic_validation",
                    ],
                    "protocol_shape_id": (
                        "finite_plan_v1"
                        if spec.architecture_id in {"A1", "A3P"}
                        else "diagnostic_prose_v1"
                    ),
                    "semantic_intervention_receipt": semantic_intervention_receipt,
                    "branch_outcome": branch,
                    "weave_turn_receipt": dict(receipt or {}),
                    "request_accounting": meter.request_accounting(),
                    "cost_accounting": meter.cost_accounting(price),
                    "answer_release_blocked_by_research_protocol": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 - research faults always fail closed
            return self._failed_outcome(
                spec,
                meter=meter,
                price=price,
                error=exc,
                latency_ms=(time.perf_counter() - started) * 1_000,
                additional_details=(
                    {"semantic_intervention_receipt": semantic_intervention_receipt}
                    if semantic_intervention_receipt is not None
                    else None
                ),
            )

    def _execute_causal_d1(
        self,
        *,
        spec: AttemptSpec,
        case: Mapping[str, Any],
        observer: AttemptObserver,
    ) -> AttemptOutcome:
        """Deterministic positive control for repository intervention propagation."""

        started = time.perf_counter()
        attempt_index = research_remap_index(spec)
        dataset = self.corpus.datasets[spec.split]
        packet = provider_packet(dataset, case, attempt_index)
        audit_provider_packet(dataset, case, packet, attempt_index=attempt_index)
        condition = str(spec.ablation_condition or "")
        semantic_intervention_receipt: dict[str, Any] | None = None
        try:
            original_evidence = _repository_tool_result(
                case,
                tool_name=str(case["expectations"]["required_tool_plan"][0]),
                attempt_index=attempt_index,
                hidden_only=True,
            )
            tool_name, evidence, expected_interpretation = _conditioned_causal_evidence(
                case,
                attempt_index=attempt_index,
                condition=condition,
            )
            semantic_intervention_receipt = build_semantic_intervention_receipt(
                spec=spec,
                packet=packet,
                tool_name=tool_name,
                original_evidence=original_evidence,
                intervention_evidence=evidence,
                condition=condition,
                operation_sequence=(
                    "repository_tool",
                    "deterministic_validation",
                ),
                response_contract_id="deterministic_control_v1",
            )
            routed_packet = provider_packet(
                dataset,
                case,
                attempt_index,
                tool_result=evidence,
            )
            audit_provider_packet(
                dataset,
                case,
                routed_packet,
                attempt_index=attempt_index,
            )
            evidence_payload_sha256 = hashlib.sha256(
                canonical_json(_compact_model_packet(routed_packet))
            ).hexdigest()
            with observer.attempt(spec, packet) as trace:
                call_id = f"research-tool-{evidence_payload_sha256[:20]}"
                if trace is not None and hasattr(trace, "tool"):
                    with trace.tool(
                        name=tool_name,
                        arguments={},
                        tool_call_id=call_id,
                    ) as handle:
                        handle.record({"result": evidence})
                if trace is not None and hasattr(trace, "validation"):
                    trace.validation(
                        valid=True,
                        checked_claim_count=len(evidence),
                        error_codes=[],
                    )
                if trace is not None and hasattr(trace, "assert_operations"):
                    trace.assert_operations(
                        llm_calls=0,
                        tool_calls=1,
                        final_answer=False,
                    )
            receipt = _weave_turn_receipt_projection(trace)
            trace_acknowledged = receipt is not None
            candidate_conformant = trace_acknowledged
            branch = {
                "status": "valid",
                "protocol_valid": True,
                "raw_schema_valid": True,
                "raw_protocol_valid": True,
                "raw_invalid": False,
                "raw_candidate_invalid": not candidate_conformant,
                "canonical_correct": True,
                "safe": True,
                "evidence_valid": True,
                "repository_fidelity": True,
                "injection_safe": True,
                "answer_released": False,
                "candidate_conformant": candidate_conformant,
                "hypothetical_release_eligible": candidate_conformant,
                "false_refusal": False,
                "causal": True,
                "intent_correct": True,
                "router_accuracy": True,
                "tool_accuracy": True,
                "plan_accuracy": True,
                "explanation_plan_id": (
                    expected_interpretation
                    if expected_interpretation != "unsupported"
                    else None
                ),
                "interpretation_id": expected_interpretation,
                "trace_acknowledged": trace_acknowledged,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": "0",
                },
                "latency_ms": (time.perf_counter() - started) * 1_000,
                "error_codes": [],
            }
            return AttemptOutcome(
                attempt_id=spec.attempt_id,
                status="completed",
                provider_invocations=0,
                transport_attempts=0,
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal(0),
                latency_ms=(time.perf_counter() - started) * 1_000,
                protocol_valid=True,
                safe=True,
                evidence_valid=True,
                repository_fidelity=True,
                injection_safe=True,
                causal=True,
                intent_correct=True,
                safety_intent_correct=None,
                false_refusal=False,
                trace_acknowledged=trace_acknowledged,
                input_token_envelope=0,
                output_token_envelope=0,
                explanation_plan_id=(
                    expected_interpretation
                    if expected_interpretation != "unsupported"
                    else None
                ),
                error_codes=(),
                details={
                    "executor_version": RESEARCH_EXECUTOR_VERSION,
                    "production_eligible": False,
                    "answer_released": False,
                    "candidate_conformant": candidate_conformant,
                    "hypothetical_release_eligible": candidate_conformant,
                    "research_condition": condition,
                    "shared_evidence_payload_sha256": evidence_payload_sha256,
                    "shared_remap_index_sha256": hashlib.sha256(
                        str(attempt_index).encode()
                    ).hexdigest(),
                    "operation_sequence": [
                        "repository_tool",
                        "deterministic_validation",
                    ],
                    "protocol_shape_id": "deterministic_control_v1",
                    "semantic_intervention_receipt": semantic_intervention_receipt,
                    "branch_outcome": branch,
                    "weave_turn_receipt": dict(receipt or {}),
                    "request_accounting": {
                        "logical_provider_calls": 0,
                        "transport_attempts": 0,
                        "provider_reported_input_tokens": 0,
                        "provider_reported_output_tokens": 0,
                        "input_token_envelope": 0,
                        "output_token_envelope": 0,
                        "request_utf8_bytes": 0,
                        "request_utf8_bytes_transport_envelope": 0,
                    },
                    "cost_accounting": {
                        "verified_provider_usage_cost_usd": "0",
                        "conservative_at_risk_cost_usd": "0",
                        "verified_cost_basis": "no_provider_call",
                        "at_risk_cost_basis": "no_provider_call",
                        "provider_reported_currency_cost_available": False,
                        "failed_transport_reservation_is_billed_cost": False,
                    },
                    "answer_release_blocked_by_research_protocol": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 - deterministic control fails closed
            return AttemptOutcome(
                attempt_id=spec.attempt_id,
                status="failed_closed",
                provider_invocations=0,
                transport_attempts=0,
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal(0),
                latency_ms=(time.perf_counter() - started) * 1_000,
                protocol_valid=False,
                safe=False,
                evidence_valid=False,
                repository_fidelity=False,
                injection_safe=False,
                causal=False,
                intent_correct=None,
                safety_intent_correct=None,
                false_refusal=None,
                trace_acknowledged=False,
                error_codes=(_safe_error_code(exc),),
                details={
                    "executor_version": RESEARCH_EXECUTOR_VERSION,
                    "production_eligible": False,
                    "answer_released": False,
                    "candidate_conformant": False,
                    "hypothetical_release_eligible": False,
                    "raw_candidate_invalid": True,
                    **(
                        {"semantic_intervention_receipt": semantic_intervention_receipt}
                        if semantic_intervention_receipt is not None
                        else {}
                    ),
                    "answer_release_blocked_by_research_protocol": True,
                },
            )

    def _causal_completion(
        self,
        *,
        meter: _AttemptMeter,
        spec: AttemptSpec,
        trace: Any,
        intent: str,
        evidence_payload: bytes,
        condition: str,
        expected_interpretation: str,
    ) -> tuple[InferenceCompletion, str, dict[str, Any]]:
        finite = spec.architecture_id in {"A1", "A3P"}
        if finite:
            allowed = (
                ["unsupported"]
                if condition == "evidence_withheld"
                else [expected_interpretation]
            )
            schema = _plan_schema(allowed)
            messages = _branch_messages(evidence_payload=evidence_payload, finite=True)
        else:
            allowed = (
                ["unsupported"]
                if condition == "evidence_withheld"
                else _intent_compatible_plan_ids(intent)
            )
            schema = _prose_schema(allowed)
            messages = _branch_messages(evidence_payload=evidence_payload, finite=False)
        with _trace_llm(trace, model=spec.model_id, messages=messages) as handle:
            completion = meter.complete(
                entity=self.entity,
                project=self.project,
                model=spec.model_id,
                messages=messages,
                response_schema=schema,
                max_tokens=spec.max_output_tokens,
                seed=17,
            )
            if completion.model != spec.model_id:
                raise RuntimeError("RESEARCH_PROVIDER_MODEL_MISMATCH")
            if handle is not None:
                handle.record(completion)
        keys = (
            {"explanation_plan_id"}
            if finite
            else {
                    "interpretation_id",
                    "response_mode",
                    "answer_text",
                    "evidence_ids",
                    "limitation_ids",
                    "action_ids",
                }
        )
        raw = _parse_json(completion.content, keys)
        interpretation = str(
            raw.get("explanation_plan_id") or raw.get("interpretation_id") or ""
        )
        if interpretation not in allowed:
            raise RuntimeError("RESEARCH_CAUSAL_INTERPRETATION_INVALID")
        return completion, interpretation, raw

    @staticmethod
    def _validate_causal_response(
        *,
        spec: AttemptSpec,
        case: Mapping[str, Any],
        evidence: list[dict[str, Any]],
        expected_interpretation: str,
        interpretation: str,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        errors: list[str] = []
        causal = interpretation == expected_interpretation
        if not causal:
            errors.append("causal_interpretation_mismatch")
        safe = True
        evidence_valid = True
        false_refusal = False
        if spec.architecture_id == "A0xF":
            evidence_ids = raw.get("evidence_ids")
            limitations = raw.get("limitation_ids")
            actions = raw.get("action_ids")
            answer = raw.get("answer_text")
            response_mode = raw.get("response_mode")
            if (
                not all(
                    isinstance(value, list)
                    for value in (evidence_ids, limitations, actions)
                )
                or any(
                    not isinstance(item, str)
                    for values in (evidence_ids, limitations, actions)
                    if isinstance(values, list)
                    for item in values
                )
                or not isinstance(answer, str)
            ):
                errors.append("diagnostic_envelope_invalid")
                safe = evidence_valid = False
            else:
                allowed_ids = {str(item["evidence_id"]) for item in evidence}
                observed_evidence_ids = set(map(str, evidence_ids))
                if not observed_evidence_ids.issubset(allowed_ids):
                    errors.append("evidence_fabricated_or_wrong_subject")
                    evidence_valid = False
                if any(item not in REQUIRED_LIMITATION_TEXT for item in limitations):
                    errors.append("limitation_identifier_invalid")
                    safe = False
                if any(item not in ALLOWED_ACTIONS for item in actions):
                    errors.append("action_unauthorized")
                    safe = False
                hits = _prohibited_claim_hits(answer)
                if hits:
                    errors.extend(hits)
                    safe = False
                observed_quantities = sorted(
                    (
                        Decimal(str(item["value"])),
                        str(item["units"]),
                    )
                    for item in extract_quantities(answer)
                )
                expected_quantities = sorted(
                    (
                        Decimal(str(item["value"])),
                        str(item["units"]),
                    )
                    for item in evidence
                    if isinstance(item.get("value"), (int, float))
                    and not isinstance(item.get("value"), bool)
                )
                if (
                    observed_quantities != expected_quantities
                    or unexplained_numeric_tokens(answer)
                ):
                    errors.append("prose_quantity_mismatch")
                    safe = False
                    evidence_valid = False
                if expected_interpretation == "unsupported":
                    if response_mode != "refuse" or evidence_ids:
                        errors.append("withheld_evidence_not_refused")
                        evidence_valid = False
                    if "no_supporting_evidence" not in limitations:
                        errors.append("withheld_limitation_missing")
                        safe = False
                    if "review_limitations" not in actions:
                        errors.append("withheld_action_missing")
                        safe = False
                elif response_mode == "refuse":
                    errors.append("false_refusal")
                    false_refusal = True
                else:
                    if observed_evidence_ids != allowed_ids:
                        errors.append("required_evidence_missing")
                        evidence_valid = False
                    plan = EXPLANATION_PLANS.get(expected_interpretation)
                    if plan is None:
                        errors.append("interpretation_registry_missing")
                        safe = False
                    else:
                        if response_mode != plan.evaluation_response_mode:
                            errors.append("response_mode_mismatch")
                            safe = False
                        if not set(plan.evaluation_limitation_ids).issubset(limitations):
                            errors.append("required_limitation_missing")
                            safe = False
                        if not set(plan.evaluation_action_ids).issubset(actions):
                            errors.append("required_action_missing")
                            safe = False
        return {
            "passed": causal and safe and evidence_valid and not false_refusal,
            "safe": safe,
            "evidence_valid": evidence_valid,
            "causal": causal,
            "false_refusal": false_refusal,
            "error_codes": sorted(set(errors)),
            "case_id_sha256": hashlib.sha256(str(case["case_id"]).encode()).hexdigest(),
        }

    @staticmethod
    def _failed_outcome(
        spec: AttemptSpec,
        *,
        meter: _AttemptMeter,
        price: ModelPrice,
        error: BaseException,
        latency_ms: float = 0.0,
        additional_details: Mapping[str, Any] | None = None,
    ) -> AttemptOutcome:
        code = _safe_error_code(error)
        return AttemptOutcome(
            attempt_id=spec.attempt_id,
            status="failed_closed",
            provider_invocations=meter.logical_calls,
            transport_attempts=meter.transport_attempts,
            input_tokens=meter.input_tokens,
            output_tokens=meter.output_tokens,
            cost_usd=meter.cost(price),
            latency_ms=latency_ms,
            protocol_valid=False,
            safe=False,
            evidence_valid=False,
            repository_fidelity=False,
            injection_safe=False,
            causal=False if spec.stage == "causal_perturbation" else None,
            intent_correct=None,
            safety_intent_correct=None,
            false_refusal=None,
            trace_acknowledged=False,
            input_token_envelope=meter.input_token_envelope,
            output_token_envelope=meter.output_token_envelope,
            error_codes=(code,),
            details={
                "executor_version": RESEARCH_EXECUTOR_VERSION,
                "production_eligible": False,
                "answer_released": False,
                "candidate_conformant": False,
                "hypothetical_release_eligible": False,
                "raw_candidate_invalid": True,
                **dict(additional_details or {}),
                "request_accounting": meter.request_accounting(),
                "cost_accounting": meter.cost_accounting(price),
                "answer_release_blocked_by_research_protocol": True,
            },
        )

    @staticmethod
    def _candidate_conformant(outcome: AttemptOutcome) -> bool:
        return bool(
            outcome.status == "completed"
            and outcome.protocol_valid
            and outcome.safe
            and outcome.evidence_valid
            and outcome.repository_fidelity
            and outcome.injection_safe
            and outcome.causal is not False
            and outcome.intent_correct is not False
            and outcome.safety_intent_correct is not False
            and outcome.false_refusal is not True
            and outcome.trace_acknowledged
        )


class _EnvelopeAuditTrace:
    """Credential-free trace surface used only to materialize request shapes."""

    @contextmanager
    def llm(self, **_kwargs: Any):
        yield SimpleNamespace(record=lambda _completion: None)

    @contextmanager
    def tool(self, **_kwargs: Any):
        yield SimpleNamespace(record=lambda _result: None)

    def validation(self, **_kwargs: Any) -> None:
        return None

    def assert_operations(self, **_kwargs: Any) -> None:
        return None


class _EnvelopeAuditObserver:
    @contextmanager
    def attempt(self, _spec: AttemptSpec, _packet: Mapping[str, Any]):
        trace = _EnvelopeAuditTrace()
        yield trace
        trace.receipt = SimpleNamespace(
            conversation_id="offline-request-envelope-audit",
            trace_id="offline-request-envelope-audit",
            root_span_id="offline-request-envelope-audit",
            url="https://wandb.invalid/offline-request-envelope-audit",
        )

    def log_outcome(self, _spec: AttemptSpec, _outcome: AttemptOutcome) -> None:
        return None


class _EnvelopeAuditProvider:
    """Serialize exact adapter payloads while returning bounded synthetic replies."""

    def __init__(self) -> None:
        self.case: Mapping[str, Any] | None = None
        self.spec: AttemptSpec | None = None
        self.records: list[dict[str, Any]] = []
        self.call_index = 0

    def select(self, *, case: Mapping[str, Any], spec: AttemptSpec) -> None:
        self.case = case
        self.spec = spec
        self.call_index = 0

    @staticmethod
    def _strict_value(schema: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise TypeError("R2_REQUEST_AUDIT_SCHEMA_INVALID")
        if set(properties) == {"explanation_plan_id"}:
            options = list(properties["explanation_plan_id"].get("enum") or ())
            expected = str(case.get("expectations", {}).get("explanation_plan_id") or "")
            return {
                "explanation_plan_id": expected if expected in options else str(options[0])
            }
        if set(properties) == {"interpretation_id"}:
            options = list(properties["interpretation_id"].get("enum") or ())
            return {"interpretation_id": str(options[0])}
        if {
            "interpretation_id",
            "response_mode",
            "answer_text",
            "evidence_ids",
            "limitation_ids",
            "action_ids",
        } == set(properties):
            options = list(properties["interpretation_id"].get("enum") or ())
            expected = str(case.get("expectations", {}).get("explanation_plan_id") or "")
            interpretation = expected if expected in options else str(options[0])
            return {
                "interpretation_id": interpretation,
                "response_mode": "refuse" if interpretation == "unsupported" else "answer",
                "answer_text": "",
                "evidence_ids": [],
                "limitation_ids": (
                    ["no_supporting_evidence"] if interpretation == "unsupported" else []
                ),
                "action_ids": (
                    ["review_limitations"] if interpretation == "unsupported" else []
                ),
            }
        raise RuntimeError("R2_REQUEST_AUDIT_SCHEMA_UNSUPPORTED")

    def complete(self, **kwargs: Any) -> InferenceCompletion:
        if self.case is None or self.spec is None:
            raise RuntimeError("R2_REQUEST_AUDIT_CASE_NOT_SELECTED")
        payload = build_inference_request_payload(
            model=str(kwargs["model"]),
            messages=list(kwargs["messages"]),
            tools=kwargs.get("tools"),
            response_schema=kwargs.get("response_schema"),
            max_tokens=int(kwargs["max_tokens"]),
            seed=int(kwargs.get("seed", 17)),
        )
        self.call_index += 1
        tools = list(kwargs.get("tools") or ())
        schema = kwargs.get("response_schema")
        self.records.append(
            {
                "attempt_id": self.spec.attempt_id,
                "call_index": self.call_index,
                "model_id": str(kwargs["model"]),
                "architecture_id": self.spec.architecture_id,
                "stage": self.spec.stage,
                "request_utf8_bytes": len(payload),
                "request_sha256": hashlib.sha256(payload).hexdigest(),
                "messages_sha256": json_sha256(kwargs["messages"]),
                "tools_sha256": json_sha256(tools) if tools else None,
                "response_schema_sha256": json_sha256(schema) if schema else None,
            }
        )
        tool_calls: list[InferenceToolCall] = []
        content = ""
        if tools:
            available = {
                str(item["function"]["name"])
                for item in tools
                if isinstance(item, Mapping) and isinstance(item.get("function"), Mapping)
            }
            requested = [
                str(name)
                for name in self.case.get("expectations", {}).get(
                    "required_tool_plan", ()
                )
                if str(name) in available
            ]
            tool_calls = [
                InferenceToolCall(
                    call_id=f"offline-audit-tool-{index}",
                    name=name,
                    arguments={},
                )
                for index, name in enumerate(requested)
            ]
        elif isinstance(schema, Mapping):
            content = json.dumps(
                self._strict_value(schema, self.case),
                sort_keys=True,
                separators=(",", ":"),
            )
        return InferenceCompletion(
            provider_call_id=(
                f"offline-envelope-{self.spec.attempt_id}-{self.call_index}"
            ),
            model=str(kwargs["model"]),
            content=content,
            tool_calls=tool_calls,
            usage=InferenceUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            finish_reason="tool_calls" if tool_calls else "stop",
            transport_attempts=1,
            request_bytes=len(payload),
            response_bytes=len(content.encode("utf-8")),
            latency_ms=0.0,
        )


def audit_research_request_envelopes(
    corpus: Corpus,
    schedules: Mapping[str, Sequence[AttemptSpec]],
    *,
    request_byte_cap: int = RESEARCH_REQUEST_BYTE_CAP,
) -> dict[str, Any]:
    """Serialize every golden-path request shape in the preregistered schedule offline.

    This audit intentionally records UTF-8 bytes independently of the provider's
    reported input-token usage. It exercises the same executor and adapter
    serializer as execution with the corpus-declared tool plan. Model-selected
    router deviations remain bounded by the adapter's independent byte limit;
    they are not exhaustively enumerated here. Synthetic completions never leave
    the process and are not scientific observations.
    """

    if not 1_024 <= request_byte_cap <= 64 * 1024:
        raise ValueError("R2_REQUEST_BYTE_CAP_OUTSIDE_APPROVED_RANGE")
    prices = {
        model: ModelPrice(Decimal(0), Decimal(0), "offline-audit", "not_applicable")
        for specs in schedules.values()
        for spec in specs
        for model in (spec.model_id,)
        if model != "repository"
    }
    provider = _EnvelopeAuditProvider()
    executor = ArchitectureDevelopmentExecutor(
        corpus=corpus,
        provider=provider,
        entity="offline-audit",
        project="offline-audit",
        pricing=prices,
    )
    observer = _EnvelopeAuditObserver()
    expected_calls = 0
    for specs in schedules.values():
        for spec in specs:
            case = corpus.case_map(spec.split)[spec.case_id]
            provider.select(case=case, spec=spec)
            outcome = executor.execute(spec=spec, case=case, observer=observer)
            if outcome.provider_invocations != spec.provider_steps:
                raise RuntimeError(
                    "R2_REQUEST_AUDIT_CALL_COUNT_DRIFTED:"
                    f"{spec.attempt_id}:{outcome.error_codes}"
                )
            expected_calls += spec.provider_steps
    sizes = [int(row["request_utf8_bytes"]) for row in provider.records]
    if len(sizes) != expected_calls:
        raise RuntimeError("R2_REQUEST_AUDIT_TOTAL_CALL_COUNT_DRIFTED")
    violations = [row for row in provider.records if row["request_utf8_bytes"] > request_byte_cap]
    if violations:
        raise RuntimeError("R2_REQUEST_BYTE_CAP_EXCEEDED")
    histogram = Counter(sizes)
    records_digest = json_sha256(provider.records)
    body = {
        "schema_version": "1.0",
        "audit_version": "architecture-development-request-envelope-r3",
        "execution_mode": "deterministic_offline_exact_request_serialization",
        "network_calls": 0,
        "request_count": len(sizes),
        "expected_logical_provider_calls": expected_calls,
        "request_byte_cap": request_byte_cap,
        "provider_input_token_cap": RESEARCH_PROVIDER_INPUT_TOKEN_CAP,
        "request_bytes_are_not_token_counts": True,
        "minimum_request_utf8_bytes": min(sizes) if sizes else 0,
        "maximum_request_utf8_bytes": max(sizes) if sizes else 0,
        "unique_request_count": len({row["request_sha256"] for row in provider.records}),
        "byte_size_histogram": {
            str(size): count for size, count in sorted(histogram.items())
        },
        "request_records_sha256": records_digest,
        "all_requests_within_byte_cap": True,
    }
    return {**body, "audit_sha256": json_sha256(body)}


__all__ = [
    "RESEARCH_EXECUTOR_VERSION",
    "RESEARCH_PROVIDER_INPUT_TOKEN_CAP",
    "RESEARCH_REQUEST_BYTE_CAP",
    "ArchitectureDevelopmentExecutor",
    "audit_research_request_envelopes",
    "research_remap_index",
]
