"""Release-ineligible architecture research for the Evidence Guide.

This module deliberately lives beside, rather than inside, the sealed v2
release campaign.  It can estimate and execute useful development experiments
after a release gate has stopped, while making it structurally impossible for
those exploratory results to create an ``AssistantReleaseBasis``.

The study compares deterministic controls (D0 and D1), repository-routed A1,
and a paired A3X experiment whose finite-plan and diagnostic-prose siblings
consume one shared router/tool result. It uses every visible scenario and the
complete visible causal-retrieval stratum. Nothing here is confirmatory.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from .evidence_guide_v2_architecture_executor import (
    RESEARCH_PROVIDER_INPUT_TOKEN_CAP,
    RESEARCH_REQUEST_BYTE_CAP,
    _conditioned_causal_evidence,
    audit_research_request_envelopes,
    build_semantic_intervention_receipt,
    research_remap_index,
    validate_semantic_intervention_receipt,
)
from .evidence_guide_v2_architecture_incident import (
    validate_architecture_development_incident,
)
from .evidence_guide_v2_campaign import (
    DEFAULT_BUDGET_CAPS,
    PROVIDER_STEPS,
    RANDOM_SEED,
    TIME_BLOCKS,
    AttemptObserver,
    AttemptOutcome,
    AttemptSpec,
    BudgetCaps,
    CampaignStore,
    Corpus,
    ModelPrice,
    PerfectFakeExecutor,
    estimate_provider_budget,
    file_sha256,
    json_sha256,
    stratified_cases,
    wilson_interval,
    write_json_atomic,
)
from .evidence_guide_v2_protocol import (
    EXPLANATION_PLANS,
    PLAN_REGISTRY_SHA256,
    PROTOCOL_SHA256,
    allowed_explanation_plan_ids,
    deterministic_question_intent,
)
from .evidence_guide_v2_provider import _repository_tool_result, provider_packet

STUDY_VERSION = "evidence-guide-v2-architecture-development-r3"
PREREGISTRATION_VERSION = "evidence-guide-v2-architecture-development-preregistration-r3"
TERMINAL_DISPOSITION = "EXPLORATORY_COMPLETE_LIVE_GUIDE_UNCHANGED_DISABLED"
OUTPUT_DIRECTORY_NAME = "evidence-guide-v2-architecture-dev-r3"

# Fake-provider rehearsal and paid cloud execution are different study
# instances.  This is intentionally part of the cryptographic namespace rather
# than mutable runtime metadata: a successful local rehearsal can never satisfy
# a live attempt ID, block receipt, or database checkpoint.
RESEARCH_EXECUTION_MODES = ("fake", "live")

RESEARCH_STAGES = (
    "development",
    "transfer",
    "causal_perturbation",
    "operational_stratum",
)
RESEARCH_ARCHITECTURES = ("D0", "D1", "A1", "A3X", "A3P", "A0xF")
PRIMARY_MODELS = (
    "ibm-granite/granite-4.1-8b",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
)
OPERATIONAL_MODELS = (
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "meta-llama/Llama-3.3-70B-Instruct",
)
RESEARCH_MODELS = (*PRIMARY_MODELS, *OPERATIONAL_MODELS)
CAUSAL_ABLATIONS = (
    "evidence_original",
    "counterfactual_threshold_crossing",
    "evidence_withheld",
)

R2B_REQUIRED_DECISION = "DISABLE_LIVE_GUIDE"
R2B_REQUIRED_RELEASE_GATE_DIGEST = (
    "6c54dcc86f0ef7afab2690841dc6c05d5679e46271a1b1a068525ab311bf7482"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ArchitectureDevelopmentDesign:
    """Frozen inputs to a schedule, separated from all observed outcomes."""

    development_cases: int = 120
    transfer_cases: int = 120
    opaque_id_presentations: int = 2
    primary_models: tuple[str, ...] = PRIMARY_MODELS
    operational_models: tuple[str, ...] = OPERATIONAL_MODELS
    causal_conditions: tuple[str, ...] = CAUSAL_ABLATIONS

    def as_dict(self) -> dict[str, Any]:
        return {
            "development_cases": self.development_cases,
            "transfer_cases": self.transfer_cases,
            "opaque_id_presentations": self.opaque_id_presentations,
            "primary_models": list(self.primary_models),
            "operational_models": list(self.operational_models),
            "causal_conditions": list(self.causal_conditions),
        }


DEFAULT_DESIGN = ArchitectureDevelopmentDesign()


@dataclass(frozen=True)
class ResearchAttemptSpec(AttemptSpec):
    """Attempt specification for paired research-only architecture arms."""

    @property
    def provider_steps(self) -> int:
        return {
            "D0": 0,
            "D1": 0,
            "A1": 1,
            # One shared router call followed by the finite-plan and diagnostic
            # free-prose synthesis siblings. Both branch outcomes belong to one
            # immutable attempt and therefore see identical retrieved evidence.
            "A3X": 3,
            "A3P": 1,
            "A0xF": 1,
        }.get(self.architecture_id, PROVIDER_STEPS.get(self.architecture_id, 0))


class ResearchFakeExecutor(PerfectFakeExecutor):
    """Credential-free paired-branch fixture for orchestration tests only."""

    def execute(
        self,
        *,
        spec: AttemptSpec,
        case: Mapping[str, Any],
        observer: AttemptObserver,
    ) -> AttemptOutcome:
        outcome = super().execute(spec=spec, case=case, observer=observer)
        expected_plan = str(case.get("expectations", {}).get("explanation_plan_id") or "")
        selected_plan = expected_plan or outcome.explanation_plan_id
        if spec.stage == "causal_perturbation":
            if spec.ablation_condition == "counterfactual_threshold_crossing":
                rule = _mapping(case.get("expectations")).get("causal_rule")
                if not isinstance(rule, Mapping):
                    raise RuntimeError("RESEARCH_FAKE_CAUSAL_RULE_MISSING")
                nonpositive = str(rule["nonpositive_plan_id"])
                positive = str(rule["positive_plan_id"])
                if expected_plan == positive:
                    selected_plan = nonpositive
                elif expected_plan == nonpositive:
                    selected_plan = positive
                else:
                    raise RuntimeError("RESEARCH_FAKE_CAUSAL_PLAN_UNKNOWN")
            elif spec.ablation_condition == "evidence_withheld":
                selected_plan = "unsupported"
        outcome = replace(outcome, explanation_plan_id=selected_plan)
        details = dict(outcome.details)
        candidate_conformant = bool(
            outcome.status == "completed"
            and outcome.protocol_valid
            and outcome.safe
            and outcome.evidence_valid
            and outcome.repository_fidelity
            and outcome.injection_safe
            and outcome.trace_acknowledged
        )
        details.update(
            {
                "answer_released": False,
                "candidate_conformant": candidate_conformant,
                "hypothetical_release_eligible": candidate_conformant,
                "operation_sequence": (
                    ["repository_tool", "deterministic_validation"]
                    if spec.architecture_id == "D1"
                    and spec.stage == "causal_perturbation"
                    else (
                        [
                            "repository_tool",
                            "synthesis_call",
                            "deterministic_validation",
                        ]
                        if spec.stage == "causal_perturbation"
                        else []
                    )
                ),
                "protocol_shape_id": (
                    "deterministic_control_v1"
                    if spec.architecture_id == "D1"
                    and spec.stage == "causal_perturbation"
                    else (
                        "finite_plan_v1"
                        if spec.architecture_id in {"A1", "A3P"}
                        and spec.stage == "causal_perturbation"
                        else (
                            "diagnostic_prose_v1"
                            if spec.architecture_id == "A0xF"
                            and spec.stage == "causal_perturbation"
                            else None
                        )
                    )
                ),
                "cost_accounting": {
                    "verified_provider_usage_cost_usd": "0",
                    "conservative_at_risk_cost_usd": "0",
                    "verified_cost_basis": "fake_rehearsal_no_provider_call",
                    "at_risk_cost_basis": "fake_rehearsal_no_provider_call",
                    "provider_reported_currency_cost_available": False,
                    "failed_transport_reservation_is_billed_cost": False,
                },
            }
        )
        if spec.stage == "causal_perturbation":
            attempt_index = research_remap_index(spec)
            packet = provider_packet({"rows": [case]}, case, attempt_index)
            tool_name = str(case["expectations"]["required_tool_plan"][0])
            original = _repository_tool_result(
                case,
                tool_name=tool_name,
                attempt_index=attempt_index,
                hidden_only=True,
            )
            _tool_name, intervention, _expected = _conditioned_causal_evidence(
                case,
                attempt_index=attempt_index,
                condition=str(spec.ablation_condition or ""),
            )
            details["semantic_intervention_receipt"] = (
                build_semantic_intervention_receipt(
                    spec=spec,
                    packet=packet,
                    tool_name=tool_name,
                    original_evidence=original,
                    intervention_evidence=intervention,
                    condition=str(spec.ablation_condition or ""),
                    operation_sequence=details["operation_sequence"],
                    response_contract_id=str(details["protocol_shape_id"]),
                )
            )
        if spec.architecture_id == "A3X":
            shared = json_sha256(
                {
                    "attempt_id": spec.attempt_id,
                    "router": "fake-shared-router",
                    "case_id": spec.case_id,
                }
            )
            details.update(
                {
                    "shared_router_tool_receipt_sha256": shared,
                    "paired_branch_outcomes": {
                        "A3P": {
                            "status": "fake_valid",
                            "answer_released": False,
                            "candidate_conformant": candidate_conformant,
                            "hypothetical_release_eligible": candidate_conformant,
                            "explanation_plan_id": selected_plan,
                        },
                        "A0xF": {
                            "status": "fake_valid",
                            "answer_released": False,
                            "candidate_conformant": candidate_conformant,
                            "hypothetical_release_eligible": candidate_conformant,
                            "explanation_plan_id": selected_plan,
                        },
                    },
                }
            )
        return replace(outcome, details=details)


def validate_r2b_terminal_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the predecessor stop receipt before inheriting its debit.

    The exploratory study must start from the complete cumulative debit, not
    just the final canary call.  It also inherits the disabled product state;
    later exploratory outcomes cannot overwrite that state.
    """

    body = {key: item for key, item in value.items() if key != "receipt_sha256"}
    usage = value.get("usage")
    checks = (
        value.get("decision") == R2B_REQUIRED_DECISION,
        value.get("release_gate_digest") == R2B_REQUIRED_RELEASE_GATE_DIGEST,
        value.get("receipt_sha256") == json_sha256(body),
        isinstance(usage, Mapping),
        value.get("practitioner_usefulness_status") == "not_measured",
    )
    if not all(checks):
        raise ValueError("R2B_TERMINAL_RECEIPT_INVALID")
    assert isinstance(usage, Mapping)
    normalized_usage = {
        "cost_usd": str(Decimal(str(usage["cost_usd"]))),
        "provider_invocations": int(usage["provider_invocations"]),
        "transport_attempts": int(usage["transport_attempts"]),
        "input_tokens": int(usage["input_tokens"]),
        "output_tokens": int(usage["output_tokens"]),
    }
    if Decimal(normalized_usage["cost_usd"]) < 0 or any(
        normalized_usage[key] < 0
        for key in (
            "provider_invocations",
            "transport_attempts",
            "input_tokens",
            "output_tokens",
        )
    ):
        raise ValueError("R2B_TERMINAL_USAGE_INVALID")
    return {
        "receipt_sha256": str(value["receipt_sha256"]),
        "release_gate_digest": str(value["release_gate_digest"]),
        "decision": R2B_REQUIRED_DECISION,
        "usage": normalized_usage,
    }


def apply_r2b_debit(
    store: CampaignStore,
    terminal_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    validated = validate_r2b_terminal_receipt(terminal_receipt)
    prior = {
        "source": "evidence-guide-v2-campaign-r2b-terminal",
        "source_receipt_sha256": validated["receipt_sha256"],
        **validated["usage"],
    }
    store.put_meta("prior_usage", prior, immutable=True)
    store.put_meta("predecessor_terminal", validated, immutable=True)
    return prior


def _normalized_usage(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cost_usd": str(Decimal(str(value["cost_usd"]))),
        "provider_invocations": int(value["provider_invocations"]),
        "transport_attempts": int(value["transport_attempts"]),
        "input_tokens": int(value["input_tokens"]),
        "output_tokens": int(value["output_tokens"]),
    }


def _add_usage(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    left = _normalized_usage(first)
    right = _normalized_usage(second)
    return {
        "cost_usd": str(Decimal(str(left["cost_usd"])) + Decimal(str(right["cost_usd"]))),
        "provider_invocations": int(left["provider_invocations"])
        + int(right["provider_invocations"]),
        "transport_attempts": int(left["transport_attempts"]) + int(right["transport_attempts"]),
        "input_tokens": int(left["input_tokens"]) + int(right["input_tokens"]),
        "output_tokens": int(left["output_tokens"]) + int(right["output_tokens"]),
    }


def validate_r1_architecture_incident_receipt(
    value: Mapping[str, Any],
    *,
    r2b_terminal_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the independently sealed R1 incident to the R2b predecessor debit."""

    try:
        validated = validate_architecture_development_incident(value)
        terminal = validate_r2b_terminal_receipt(r2b_terminal_receipt)
        prior = _normalized_usage(validated["prior_r2b_usage"])
        incident = _normalized_usage(validated["incident_usage"])
        cumulative = _normalized_usage(validated["cumulative_usage"])
        if prior != terminal["usage"] or cumulative != _add_usage(prior, incident):
            raise ValueError("incident usage lineage does not match R2b")
        if (
            validated.get("scientific_denominator_attempts") != 0
            or validated.get("architecture_denominator_attempts") != 0
            or validated.get("disposition")
            != "ABORTED_NONSCIENTIFIC_PROTOCOL_IMPLEMENTATION_INCIDENT"
        ):
            raise ValueError("incident exclusion boundary is invalid")
    except (KeyError, TypeError, ValueError, ArithmeticError, RuntimeError) as exc:
        raise ValueError("R1_ARCHITECTURE_INCIDENT_RECEIPT_INVALID") from exc
    return {
        "receipt_sha256": str(validated["receipt_sha256"]),
        "disposition": str(validated["disposition"]),
        "scientific_denominator_attempts": 0,
        "architecture_denominator_attempts": 0,
        "incident_usage": incident,
        "cumulative_usage": cumulative,
    }


def apply_predecessor_debits(
    store: CampaignStore,
    terminal_receipt: Mapping[str, Any],
    incident_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Install the complete R2b + excluded R1 debit before any R2 attempt."""

    terminal = validate_r2b_terminal_receipt(terminal_receipt)
    incident = validate_r1_architecture_incident_receipt(
        incident_receipt,
        r2b_terminal_receipt=terminal_receipt,
    )
    prior = {
        "source": "r2b-terminal-plus-excluded-architecture-r1-incident",
        "r2b_terminal_receipt_sha256": terminal["receipt_sha256"],
        "r1_incident_receipt_sha256": incident["receipt_sha256"],
        **incident["cumulative_usage"],
    }
    store.put_meta("prior_usage", prior, immutable=True)
    store.put_meta("predecessor_terminal", terminal, immutable=True)
    store.put_meta("excluded_r1_incident", incident, immutable=True)
    return prior


def study_instance_sha256(
    corpus: Corpus,
    *,
    source_hashes: Mapping[str, str],
    execution_mode: str,
    design: ArchitectureDevelopmentDesign = DEFAULT_DESIGN,
) -> str:
    """Create a namespace for attempt IDs without a digest cycle."""

    if execution_mode not in RESEARCH_EXECUTION_MODES:
        raise ValueError("unknown architecture-development execution mode")

    return json_sha256(
        {
            "study_version": STUDY_VERSION,
            "execution_mode": execution_mode,
            "corpus_manifest_sha256": corpus.manifest_sha256,
            "visible_split_sha256": {
                key: corpus.split_sha256[key] for key in ("development", "validation")
            },
            "design": design.as_dict(),
            "seed": RANDOM_SEED,
            "shared_protocol_sha256": PROTOCOL_SHA256,
            "plan_registry_sha256": PLAN_REGISTRY_SHA256,
            "source_hashes": dict(sorted(source_hashes.items())),
        }
    )


def _make_spec(
    *,
    instance_sha256: str,
    stage: str,
    split: str,
    case: Mapping[str, Any],
    model_id: str,
    architecture_id: str,
    repeat: int,
    ordinal: int,
    ablation_condition: str | None = None,
) -> AttemptSpec:
    time_block = TIME_BLOCKS[(ordinal + repeat) % len(TIME_BLOCKS)]
    identity_payload = {
        "study_instance_sha256": instance_sha256,
        "stage": stage,
        "split": split,
        "case_id": str(case["case_id"]),
        "project_family": str(case.get("project_family") or "unknown-project"),
        "model_id": model_id,
        "architecture_id": architecture_id,
        "prompt_id": "checklist",
        "max_output_tokens": 128,
        "repeat": repeat,
        "time_block": time_block,
        "hidden": False,
        "judge_role": None,
        "order": None,
        "presentation_index": ordinal,
        "ablation_condition": ablation_condition,
    }
    spec_payload = {
        key: value for key, value in identity_payload.items() if key != "study_instance_sha256"
    }
    return ResearchAttemptSpec(
        attempt_id=json_sha256(identity_payload)[:32],
        **spec_payload,
    )


def build_study_schedule(
    corpus: Corpus,
    *,
    instance_sha256: str,
    design: ArchitectureDevelopmentDesign = DEFAULT_DESIGN,
) -> dict[str, tuple[AttemptSpec, ...]]:
    """Build the default 4,944-attempt, 5,712-call exploratory schedule.

    * Development and transfer cover every visible scenario under two opaque-ID
      presentations. A1 is compared with paired A3X: one shared router/tool
      result feeding a finite-plan and a structured diagnostic-prose sibling.
    * Synthesis intervention covers all 32 target-hidden scenarios under
      original, threshold-crossing, and withheld evidence for all three model
      synthesis arms plus a deterministic D1 positive control.
    * The three capability-failing models are measured separately with A1 for
      operational evidence only; they never enter the primary denominator.
    """

    if not _SHA256.fullmatch(instance_sha256):
        raise ValueError("study instance must be a lowercase SHA-256")
    development = corpus.splits.get("development", ())
    validation = corpus.splits.get("validation", ())
    primary_cases = stratified_cases(development, design.development_cases)
    development_causal = sorted(
        (
            case
            for case in development
            if str(case.get("scenario_type") or case.get("category")) == "causal_retrieval"
        ),
        key=lambda item: str(item["case_id"]),
    )
    validation_causal = sorted(
        (
            case
            for case in validation
            if str(case.get("scenario_type") or case.get("category")) == "causal_retrieval"
        ),
        key=lambda item: str(item["case_id"]),
    )
    if len(development_causal) != 16 or len(validation_causal) != 16:
        raise RuntimeError("development causal-retrieval stratum must contain 16 cases")
    transfer_cases = stratified_cases(validation, design.transfer_cases)

    schedules: dict[str, list[AttemptSpec]] = {stage: [] for stage in RESEARCH_STAGES}
    ordinal = 0

    def add(
        stage: str,
        split: str,
        case: Mapping[str, Any],
        model_id: str,
        architecture_id: str,
        repeat: int,
        ablation_condition: str | None = None,
    ) -> None:
        nonlocal ordinal
        schedules[stage].append(
            _make_spec(
                instance_sha256=instance_sha256,
                stage=stage,
                split=split,
                case=case,
                model_id=model_id,
                architecture_id=architecture_id,
                repeat=repeat,
                ordinal=ordinal,
                ablation_condition=ablation_condition,
            )
        )
        ordinal += 1

    def add_primary_stage(
        *,
        stage: str,
        split: str,
        cases: Sequence[Mapping[str, Any]],
    ) -> None:
        for presentation in range(design.opaque_id_presentations):
            for case in cases:
                for architecture in ("D0", "D1"):
                    add(stage, split, case, "repository", architecture, presentation)
                for model in design.primary_models:
                    add(stage, split, case, model, "A1", presentation)
                    add(stage, split, case, model, "A3X", presentation)

    add_primary_stage(
        stage="development",
        split="development",
        cases=primary_cases,
    )
    add_primary_stage(stage="transfer", split="validation", cases=transfer_cases)

    for split, causal_cases in (
        ("development", development_causal),
        ("validation", validation_causal),
    ):
        for presentation in range(design.opaque_id_presentations):
            for case in causal_cases:
                for condition in design.causal_conditions:
                    # D1 is the deterministic positive control for whether the
                    # repository intervention itself propagates correctly.
                    add(
                        "causal_perturbation",
                        split,
                        case,
                        "repository",
                        "D1",
                        presentation,
                        condition,
                    )
                    for model in design.primary_models:
                        for architecture in ("A1", "A3P", "A0xF"):
                            add(
                                "causal_perturbation",
                                split,
                                case,
                                model,
                                architecture,
                                presentation,
                                condition,
                            )

    for split, cases in (("development", development), ("validation", validation)):
        for model in design.operational_models:
            for case in cases:
                add("operational_stratum", split, case, model, "A1", 0)

    frozen: dict[str, tuple[AttemptSpec, ...]] = {}
    for stage_index, stage in enumerate(RESEARCH_STAGES):
        rows = schedules[stage]
        random.Random(RANDOM_SEED + stage_index).shuffle(rows)
        frozen[stage] = tuple(rows)
    identifiers = [spec.attempt_id for specs in frozen.values() for spec in specs]
    primary_attempts_per_case = design.opaque_id_presentations * (
        2 + 2 * len(design.primary_models)
    )
    expected_count = (
        (design.development_cases + design.transfer_cases) * primary_attempts_per_case
        + 32
        * design.opaque_id_presentations
        * len(design.causal_conditions)
        * (1 + len(design.primary_models) * 3)
        + (len(development) + len(validation)) * len(design.operational_models)
    )
    if len(identifiers) != expected_count or len(identifiers) != len(set(identifiers)):
        raise RuntimeError("architecture-development schedule identity drifted")
    return frozen


def schedule_records(
    schedules: Mapping[str, Sequence[AttemptSpec]],
) -> list[dict[str, Any]]:
    return [asdict(spec) for stage in RESEARCH_STAGES for spec in schedules[stage]]


def schedule_counts(
    schedules: Mapping[str, Sequence[AttemptSpec]],
) -> dict[str, Any]:
    all_specs = [spec for stage in RESEARCH_STAGES for spec in schedules[stage]]
    by_stage = {stage: len(schedules[stage]) for stage in RESEARCH_STAGES}
    by_architecture: dict[str, int] = defaultdict(int)
    by_model: dict[str, int] = defaultdict(int)
    by_time_block: dict[str, int] = defaultdict(int)
    for spec in all_specs:
        by_architecture[spec.architecture_id] += 1
        by_model[spec.model_id] += 1
        by_time_block[spec.time_block] += 1
    return {
        "attempts": len(all_specs),
        "by_stage": by_stage,
        "by_architecture": dict(sorted(by_architecture.items())),
        "by_model": dict(sorted(by_model.items())),
        "by_time_block": dict(sorted(by_time_block.items())),
        "logical_provider_calls": sum(spec.provider_steps for spec in all_specs),
        "worst_case_transport_attempts": 2 * sum(spec.provider_steps for spec in all_specs),
    }


def _budget_with_prior(
    schedules: Mapping[str, Sequence[AttemptSpec]],
    *,
    pricing: Mapping[str, ModelPrice],
    prior_usage: Mapping[str, Any],
) -> dict[str, Any]:
    all_specs = [spec for stage in RESEARCH_STAGES for spec in schedules[stage]]
    incremental = estimate_provider_budget(all_specs, pricing=pricing)
    combined = {
        "cost_usd": str(
            Decimal(str(prior_usage["cost_usd"])) + Decimal(str(incremental["worst_case_cost_usd"]))
        ),
        "provider_invocations": int(prior_usage["provider_invocations"])
        + int(incremental["logical_provider_invocations"]),
        "transport_attempts": int(prior_usage["transport_attempts"])
        + int(incremental["worst_case_transport_attempts"]),
        "input_tokens": int(prior_usage["input_tokens"])
        + int(incremental["worst_case_input_tokens"]),
        "output_tokens": int(prior_usage["output_tokens"])
        + int(incremental["worst_case_output_tokens"]),
    }
    return {
        "incremental": incremental,
        "combined_with_predecessor_debits": combined,
    }


def _budget_admissible(value: Mapping[str, Any], caps: BudgetCaps) -> bool:
    return bool(
        Decimal(str(value["cost_usd"])) <= caps.cost_usd
        and int(value["provider_invocations"]) <= caps.provider_invocations
        and int(value["transport_attempts"]) <= caps.provider_invocations
        and int(value["input_tokens"]) <= caps.input_tokens
        and int(value["output_tokens"]) <= caps.output_tokens
    )


def build_structural_plan_analysis(corpus: Corpus) -> dict[str, Any]:
    """Measure how much semantic choice the finite-plan calls actually have."""

    split_results: dict[str, Any] = {}
    all_cardinalities: list[int] = []
    for split in ("development", "validation"):
        cardinalities: list[int] = []
        tool_cardinalities: Counter[int] = Counter()
        for row in corpus.splits[split]:
            expectations = _mapping(row.get("expectations"))
            expected_plan = str(expectations.get("explanation_plan_id") or "")
            if expected_plan not in EXPLANATION_PLANS:
                raise RuntimeError("STRUCTURAL_PLAN_ANALYSIS_EXPECTED_PLAN_UNKNOWN")
            intent = deterministic_question_intent(str(row.get("question") or ""))
            disposition = EXPLANATION_PLANS[expected_plan].rule_disposition
            allowed = allowed_explanation_plan_ids(
                intent,
                rule_disposition=(None if disposition == "any" else disposition),
            )
            if expected_plan not in allowed:
                raise RuntimeError("STRUCTURAL_PLAN_ANALYSIS_EXPECTED_PLAN_NOT_ALLOWED")
            cardinalities.append(len(allowed))
            tool_cardinalities[len(tuple(expectations.get("required_tool_plan") or ()))] += 1
        all_cardinalities.extend(cardinalities)
        split_results[split] = {
            "scenario_count": len(cardinalities),
            "finite_plan_cardinality": dict(sorted(Counter(cardinalities).items())),
            "required_tool_plan_cardinality": {
                str(key): value for key, value in sorted(tool_cardinalities.items())
            },
        }
    body = {
        "schema_version": "1.0",
        "analysis_version": "finite-plan-choice-audit-1",
        "scenario_count": len(all_cardinalities),
        "finite_plan_cardinality": dict(sorted(Counter(all_cardinalities).items())),
        "all_scenarios_have_one_allowed_plan": bool(all_cardinalities)
        and set(all_cardinalities) == {1},
        "splits": split_results,
        "interpretation": (
            "After deterministic intent classification and repository rule disposition, "
            "all visible cases expose exactly one allowed finite plan. A1 and A3-P plan "
            "calls therefore measure protocol and reliability overhead, not semantic plan "
            "choice. System-level causal plan changes originate in repository constraints "
            "and must not be attributed to model reasoning."
        ),
        "model_reasoning_attribution_permitted": False,
    }
    return {**body, "analysis_sha256": json_sha256(body)}


def build_research_preregistration(
    corpus: Corpus,
    *,
    pricing: Mapping[str, ModelPrice],
    source_hashes: Mapping[str, str],
    terminal_receipt: Mapping[str, Any],
    incident_receipt: Mapping[str, Any],
    execution_mode: str,
    caps: BudgetCaps = DEFAULT_BUDGET_CAPS,
    design: ArchitectureDevelopmentDesign = DEFAULT_DESIGN,
) -> tuple[dict[str, Any], dict[str, tuple[AttemptSpec, ...]]]:
    predecessor_terminal = validate_r2b_terminal_receipt(terminal_receipt)
    predecessor_incident = validate_r1_architecture_incident_receipt(
        incident_receipt,
        r2b_terminal_receipt=terminal_receipt,
    )
    missing_prices = [
        model
        for model in (*design.primary_models, *design.operational_models)
        if model not in pricing
    ]
    if missing_prices:
        raise ValueError(f"pricing snapshot is incomplete: {', '.join(missing_prices)}")
    instance = study_instance_sha256(
        corpus,
        source_hashes=source_hashes,
        execution_mode=execution_mode,
        design=design,
    )
    schedules = build_study_schedule(
        corpus,
        instance_sha256=instance,
        design=design,
    )
    records = schedule_records(schedules)
    request_envelope_audit = audit_research_request_envelopes(
        corpus,
        schedules,
        request_byte_cap=RESEARCH_REQUEST_BYTE_CAP,
    )
    budget = _budget_with_prior(
        schedules,
        pricing=pricing,
        prior_usage=predecessor_incident["cumulative_usage"],
    )
    if not _budget_admissible(budget["combined_with_predecessor_debits"], caps):
        raise RuntimeError("ARCHITECTURE_DEVELOPMENT_BUDGET_EXCEEDS_CAMPAIGN_CAP")
    body = {
        "schema_version": "1.0",
        "preregistration_version": PREREGISTRATION_VERSION,
        "study_version": STUDY_VERSION,
        "study_instance_sha256": instance,
        "execution_mode": execution_mode,
        "execution_boundary": {
            "mode_is_part_of_study_instance": True,
            "attempt_ids_are_mode_specific": True,
            "database_and_output_root_must_be_mode_specific": True,
            "fake_receipts_may_satisfy_live_completion": False,
            "live_completion_requires_wandb_and_weave_readback": True,
        },
        "research_question": (
            "Which Evidence Guide architecture best preserves repository authority, "
            "propagates controlled repository evidence changes through its synthesis "
            "contract, and transfers across project families under a bounded cost and "
            "latency envelope?"
        ),
        "release_eligible": False,
        "production_state_before_and_after": R2B_REQUIRED_DECISION,
        "terminal_disposition_if_complete": TERMINAL_DISPOSITION,
        "assistant_release_basis_may_be_created": False,
        "claim_boundary": (
            "Exploratory automated conformance and causal-behavior evidence only. "
            "The transfer slice is consumed for development. Practitioner usefulness, "
            "hydraulic validity, ecological outcomes, and production release are not measured."
        ),
        "predecessor": {
            "r2b_terminal": predecessor_terminal,
            "excluded_r1_architecture_incident": predecessor_incident,
            "cumulative_usage_before_r2": predecessor_incident["cumulative_usage"],
        },
        "corpus": {
            "corpus_id": corpus.corpus_id,
            "corpus_version": corpus.corpus_version,
            "manifest_sha256": corpus.manifest_sha256,
            "development_sha256": corpus.split_sha256["development"],
            "validation_sha256": corpus.split_sha256["validation"],
            "validation_status_after_study": "consumed_exploratory_not_confirmatory",
        },
        "models": {
            "primary_capability_passers": list(design.primary_models),
            "operational_capability_failures": list(design.operational_models),
        },
        "architectures": list(RESEARCH_ARCHITECTURES),
        "excluded_primary_arms": {
            "A2": (
                "A2 lets the model classify intent before deterministic retrieval, adding an "
                "intent-routing variable. This study isolates finite-plan versus diagnostic-"
                "prose synthesis after one shared evidence route."
            ),
            "legacy_A4": (
                "The legacy diagnostic arm hard-codes fail-closed/empty evidence. It is "
                "replaced here by the paired A0xF sibling using the exact A3X router/tool result."
            ),
        },
        "configuration": {
            "temperature": 0,
            "seed": RANDOM_SEED,
            "prompt_id": "checklist",
            "max_output_tokens": 128,
            "transient_retries": 1,
            "concurrency": 2,
            "time_blocks": list(TIME_BLOCKS),
            "request_utf8_byte_cap": RESEARCH_REQUEST_BYTE_CAP,
            "provider_input_token_cap": RESEARCH_PROVIDER_INPUT_TOKEN_CAP,
            "request_bytes_are_not_token_counts": True,
        },
        "stages": {
            "development": {
                "cases": design.development_cases,
                "opaque_id_presentations": design.opaque_id_presentations,
                "controls": ["D0", "D1"],
                "model_attempts": ["A1", "A3X_paired"],
                "attempts": len(schedules["development"]),
            },
            "causal_perturbation": {
                "design_role": "explicit_synthesis_only_intervention",
                "retrieval_causality_measured": False,
                "model_reasoning_attribution_permitted_for_finite_arms": False,
                "cases": 32,
                "conditions": list(design.causal_conditions),
                "models": list(design.primary_models),
                "opaque_id_presentations": design.opaque_id_presentations,
                "synthesis_arms": ["A1", "A3P", "A0xF"],
                "deterministic_positive_control": "D1",
                "attempts": len(schedules["causal_perturbation"]),
                "control_note": (
                    "Each D1/A1/A3P/A0xF triad is executed entirely inside this stage with "
                    "the same original, threshold-crossing, and withheld repository evidence "
                    "conditions. The model arms use one recorded repository-tool event and one "
                    "synthesis call per cell; D1 is the zero-provider positive control. This "
                    "does not establish that a model chose a tool, discovered evidence, or "
                    "reasoned over a non-singleton finite plan set."
                ),
            },
            "transfer": {
                "cases": design.transfer_cases,
                "opaque_id_presentations": design.opaque_id_presentations,
                "controls": ["D0", "D1"],
                "model_attempts": ["A1", "A3X_paired"],
                "attempts": len(schedules["transfer"]),
                "no_tuning_after_results": True,
            },
            "operational_stratum": {
                "cases": design.development_cases + design.transfer_cases,
                "models": list(design.operational_models),
                "architecture": "A1",
                "attempts": len(schedules["operational_stratum"]),
                "included_in_primary_efficacy_denominator": False,
            },
        },
        "schedule": {
            "attempt_count": len(records),
            "schedule_sha256": json_sha256(records),
            "counts": schedule_counts(schedules),
            "attempts": records,
        },
        "request_envelope_audit": request_envelope_audit,
        "budget": budget,
        "budget_caps": {
            "cost_usd": str(caps.cost_usd),
            "provider_invocations_or_transport_attempts": caps.provider_invocations,
            "input_tokens": caps.input_tokens,
            "output_tokens": caps.output_tokens,
        },
        "analysis_rules": {
            "report_raw_invalid_validator_containment_and_candidate_conformance_separately": True,
            "actual_answer_release_prohibited": True,
            "candidate_conformance_is_not_actual_release": True,
            "report_scenario_ever_failed_across_repeats": True,
            "report_every_arm_and_failure": True,
            "synthesis_intervention_requires_correct_repository_conditioned_output": True,
            "tool_retrieval_causality_claim_prohibited": True,
            "finite_arm_model_reasoning_claim_prohibited": True,
            "paired_a3x_requires_shared_router_and_tool_receipt": True,
            "paired_a3x_branch_outcomes": ["A3P_finite_plan", "A0xF_diagnostic_prose"],
            "no_llm_judge": True,
            "no_hidden_test": True,
            "no_practitioner_claim": True,
            "provider_reported_input_tokens_above_2048_fail_closed": True,
            "provider_transport_error_reserves_2048_input_tokens_per_attempt": True,
            "successful_transport_envelope_uses_reported_tokens_times_attempts": True,
            "request_utf8_bytes_recorded_separately_from_tokens": True,
        },
        "structural_plan_analysis": build_structural_plan_analysis(corpus),
        "shared_runtime_protocol": {
            "protocol_sha256": PROTOCOL_SHA256,
            "plan_registry_sha256": PLAN_REGISTRY_SHA256,
        },
        "source_hashes": dict(sorted(source_hashes.items())),
        "pricing": {
            model: {
                "input_per_million": str(pricing[model].input_per_million),
                "output_per_million": str(pricing[model].output_per_million),
                "source": pricing[model].source,
                "observed_at": pricing[model].observed_at,
            }
            for model in (*design.primary_models, *design.operational_models)
        },
    }
    return ({**body, "preregistration_sha256": json_sha256(body)}, schedules)


def _scenario_type(row: Mapping[str, Any]) -> str:
    details = row.get("details")
    if isinstance(details, Mapping) and details.get("scenario_type"):
        return str(details["scenario_type"])
    case_id = str(row.get("case_id") or "")
    for value in (
        "causal-retrieval",
        "injection-fabrication",
        "multi-turn-fault",
        "unsupported",
        "supported",
    ):
        if value in case_id:
            return value.replace("-", "_")
    return "unknown"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _field(
    branch: Mapping[str, Any],
    parent: Mapping[str, Any],
    name: str,
    default: Any = None,
) -> Any:
    branch_details = _mapping(branch.get("details"))
    parent_details = _mapping(parent.get("details"))
    for source in (branch, branch_details, parent, parent_details):
        if name in source and source[name] is not None:
            return source[name]
    return default


def _usage_component(value: Mapping[str, Any], name: str) -> Decimal:
    usage = _mapping(value.get("usage"))
    raw = usage.get(name, value.get(name, 0))
    try:
        return Decimal(str(raw or 0))
    except Exception:  # noqa: BLE001 - invalid usage becomes an observable zero
        return Decimal(0)


def _normalized_branch(
    parent: Mapping[str, Any],
    *,
    arm: str,
    branch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    branch = branch or {}
    paired = str(parent.get("architecture_id")) == "A3X"
    branch_present = bool(branch) if paired else True
    status = _field(branch, parent, "status", "failed_closed")
    if status not in {"completed", "failed_closed", "failed", "cancelled"}:
        status = parent.get("status", "failed_closed")
    protocol_valid = bool(_field(branch, parent, "protocol_valid", False))
    safe = bool(_field(branch, parent, "safe", False))
    evidence_valid = bool(_field(branch, parent, "evidence_valid", False))
    repository_fidelity = bool(_field(branch, parent, "repository_fidelity", False))
    injection_safe = bool(_field(branch, parent, "injection_safe", False))
    trace_acknowledged = bool(_field(branch, parent, "trace_acknowledged", False))
    intent_correct = _field(branch, parent, "intent_correct")
    safety_intent_correct = _field(branch, parent, "safety_intent_correct")
    causal = _field(branch, parent, "causal")
    false_refusal = bool(_field(branch, parent, "false_refusal", False))
    actual_answer_released = bool(_field(branch, parent, "answer_released", False))
    canonical_correct = bool(
        _field(
            branch,
            parent,
            "canonical_correct",
            safe and evidence_valid and repository_fidelity and injection_safe,
        )
    )
    explicit_raw_invalid = _field(branch, parent, "raw_invalid")
    if explicit_raw_invalid is None:
        raw_protocol_valid = bool(_field(branch, parent, "raw_protocol_valid", protocol_valid))
        raw_schema_valid = bool(_field(branch, parent, "raw_schema_valid", protocol_valid))
        raw_invalid = not (
            branch_present and raw_protocol_valid and raw_schema_valid and canonical_correct
        )
    else:
        raw_invalid = bool(explicit_raw_invalid) or not branch_present
    provider_completed = bool(status == "completed" and protocol_valid)
    derived_candidate_conformant = bool(
        provider_completed
        and branch_present
        and not raw_invalid
        and canonical_correct
        and intent_correct is not False
        and safety_intent_correct is not False
        and causal is not False
        and not false_refusal
        and trace_acknowledged
    )
    declared_candidate = _field(branch, parent, "candidate_conformant")
    declared_eligible = _field(branch, parent, "hypothetical_release_eligible")
    candidate_conformant = bool(
        derived_candidate_conformant
        and declared_candidate is not False
        and declared_eligible is not False
    )
    hypothetical_release_eligible = bool(
        candidate_conformant if declared_eligible is None else declared_eligible
    )
    candidate_declaration_consistent = bool(
        (declared_candidate is None or bool(declared_candidate) == derived_candidate_conformant)
        and (declared_eligible is None or bool(declared_eligible) == derived_candidate_conformant)
    )

    parent_details = _mapping(parent.get("details"))
    shared_usage = _mapping(parent_details.get("shared_router_usage")) if paired else {}
    parent_cost_accounting = _mapping(parent_details.get("cost_accounting"))
    has_branch_usage = bool(_mapping(branch.get("usage"))) or any(
        key in branch for key in ("input_tokens", "output_tokens", "cost_usd")
    )
    usage: dict[str, Any] = {}
    for name in ("input_tokens", "output_tokens", "cost_usd"):
        if paired and has_branch_usage:
            allocated = _usage_component(branch, name) + _usage_component(shared_usage, name) / 2
        elif paired:
            allocated = _usage_component(parent, name) / 2
        else:
            allocated = _usage_component(parent, name)
        usage[name] = str(allocated) if name == "cost_usd" else int(allocated)
    parent_at_risk = Decimal(
        str(
            parent_cost_accounting.get(
                "conservative_at_risk_cost_usd",
                parent.get("cost_usd", 0),
            )
            or 0
        )
    )
    allocated_at_risk = parent_at_risk / 2 if paired else parent_at_risk
    latency = float(_field(branch, parent, "latency_ms", parent.get("latency_ms", 0)) or 0)
    presentation = int(parent.get("repeat") or 0)
    condition = parent.get("ablation_condition")
    pair_identity = {
        "model_id": str(parent.get("model_id")),
        "split": str(parent.get("split")),
        "case_id": str(parent.get("case_id")),
        "presentation": presentation,
        "ablation_condition": condition,
    }
    receipt = parent_details.get("shared_router_tool_receipt_sha256") if paired else None
    semantic_receipt = _mapping(parent_details.get("semantic_intervention_receipt"))
    semantic_receipt_valid = bool(
        semantic_receipt and validate_semantic_intervention_receipt(semantic_receipt)
    )
    interpretation_id = _field(branch, parent, "interpretation_id")
    explanation_plan_id = _field(
        branch,
        parent,
        "explanation_plan_id",
        parent.get("explanation_plan_id"),
    )
    validator_contained = bool(raw_invalid and not hypothetical_release_eligible)
    withheld_outcome_class: str | None = None
    if condition == "evidence_withheld":
        if status != "completed" or not protocol_valid or not trace_acknowledged:
            withheld_outcome_class = "infrastructure_or_provider_failure"
        elif candidate_conformant and (
            interpretation_id == "unsupported" or explanation_plan_id == "unsupported"
        ):
            withheld_outcome_class = "correct_refusal"
        elif validator_contained and not actual_answer_released:
            withheld_outcome_class = "validator_containment"
        else:
            withheld_outcome_class = "unsafe_acceptance"
    return {
        "branch_row_id": json_sha256({"attempt_id": parent.get("attempt_id"), "arm": arm})[:32],
        "parent_attempt_id": str(parent.get("attempt_id")),
        "stage": str(parent.get("stage")),
        "split": str(parent.get("split")),
        "case_id": str(parent.get("case_id")),
        "project_family": str(parent.get("project_family") or "unknown-project"),
        "scenario_type": _scenario_type(parent),
        "model_id": str(parent.get("model_id")),
        "arm": arm,
        "source_architecture_id": str(parent.get("architecture_id")),
        "presentation": presentation,
        "ablation_condition": condition,
        "pair_id": json_sha256(pair_identity)[:32],
        "branch_present": branch_present,
        "shared_router_tool_receipt_sha256": receipt,
        "provider_completed": provider_completed,
        "protocol_valid": protocol_valid,
        "raw_invalid": raw_invalid,
        "validator_contained": validator_contained,
        "validator_missed_raw_invalid": bool(raw_invalid and hypothetical_release_eligible),
        "candidate_conformant": candidate_conformant,
        "hypothetical_release_eligible": hypothetical_release_eligible,
        "candidate_declaration_consistent": candidate_declaration_consistent,
        "actual_answer_released": actual_answer_released,
        "actual_released_invalid": bool(actual_answer_released and not candidate_conformant),
        "actual_non_release_enforced": not actual_answer_released,
        "false_refusal": false_refusal,
        "router_accuracy": _field(branch, parent, "router_accuracy"),
        "tool_accuracy": _field(branch, parent, "tool_accuracy"),
        "plan_accuracy": _field(branch, parent, "plan_accuracy"),
        "trace_acknowledged": trace_acknowledged,
        "canonical_correct": canonical_correct,
        "causal": causal,
        "intent_correct": intent_correct,
        "explanation_plan_id": explanation_plan_id,
        "interpretation_id": interpretation_id,
        "withheld_outcome_class": withheld_outcome_class,
        "semantic_intervention_receipt": semantic_receipt,
        "semantic_intervention_receipt_valid": semantic_receipt_valid,
        "operation_sequence": list(
            _field(branch, parent, "operation_sequence", ()) or ()
        ),
        "protocol_shape_id": _field(branch, parent, "protocol_shape_id"),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cost_usd": usage["cost_usd"],
        "verified_provider_usage_cost_usd": usage["cost_usd"],
        "conservative_at_risk_cost_usd": str(allocated_at_risk),
        "failed_transport_reservation_is_billed_cost": False,
        "provider_reported_currency_cost_available": False,
        "latency_ms": latency,
    }


def extract_research_branches(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Expand paired A3X attempts into comparable branch-level records."""

    extracted: list[dict[str, Any]] = []
    for row in rows:
        architecture = str(row.get("architecture_id"))
        if architecture == "A3X":
            branches = _mapping(_mapping(row.get("details")).get("paired_branch_outcomes"))
            for arm in ("A3P", "A0xF"):
                extracted.append(
                    _normalized_branch(row, arm=arm, branch=_mapping(branches.get(arm)))
                )
        else:
            extracted.append(_normalized_branch(row, arm=architecture))
    return extracted


def _rate_with_wilson(values: Sequence[bool]) -> dict[str, Any]:
    successes = sum(values)
    total = len(values)
    low, high = wilson_interval(successes, total)
    return {
        "successes": successes,
        "total": total,
        "rate": successes / total if total else None,
        "wilson_95": [low, high],
    }


def _percentile(values: Sequence[float], proportion: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(proportion * len(ordered)) - 1))
    return ordered[index]


def _branch_group_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    dimensions: Mapping[str, str],
) -> dict[str, Any]:
    conformant = [bool(row["candidate_conformant"]) for row in rows]
    raw_valid = [not bool(row["raw_invalid"]) for row in rows]
    contained = [bool(row["validator_contained"]) for row in rows]
    raw_invalid_rows = [row for row in rows if row["raw_invalid"]]
    non_release = [bool(row["actual_non_release_enforced"]) for row in rows]
    false_refusal = [not bool(row["false_refusal"]) for row in rows]

    def optional_rate(name: str) -> dict[str, Any] | None:
        values = [bool(row[name]) for row in rows if row.get(name) is not None]
        return _rate_with_wilson(values) if values else None

    verified_costs = [
        Decimal(str(row["verified_provider_usage_cost_usd"])) for row in rows
    ]
    at_risk_costs = [
        Decimal(str(row["conservative_at_risk_cost_usd"])) for row in rows
    ]
    total_verified_cost = sum(verified_costs, Decimal(0))
    total_at_risk_cost = sum(at_risk_costs, Decimal(0))
    latencies = [float(row["latency_ms"]) for row in rows]
    conformant_count = sum(conformant)
    return {
        **dimensions,
        "attempt_count": len(rows),
        "intention_to_treat_candidate_conformance": _rate_with_wilson(conformant),
        "raw_valid": _rate_with_wilson(raw_valid),
        "validator_contained": _rate_with_wilson(contained),
        "validator_containment_given_raw_invalid": _rate_with_wilson(
            [bool(row["validator_contained"]) for row in raw_invalid_rows]
        ),
        "actual_non_release_enforced": _rate_with_wilson(non_release),
        "no_false_refusal": _rate_with_wilson(false_refusal),
        "router_accuracy": optional_rate("router_accuracy"),
        "tool_accuracy": optional_rate("tool_accuracy"),
        "plan_accuracy": optional_rate("plan_accuracy"),
        "candidate_declaration_consistency": _rate_with_wilson(
            [bool(row["candidate_declaration_consistent"]) for row in rows]
        ),
        "provider_completion": _rate_with_wilson([bool(row["provider_completed"]) for row in rows]),
        "trace_acknowledgement": _rate_with_wilson(
            [bool(row["trace_acknowledged"]) for row in rows]
        ),
        "raw_invalid_count": sum(bool(row["raw_invalid"]) for row in rows),
        "validator_contained_count": sum(bool(row["validator_contained"]) for row in rows),
        "validator_missed_raw_invalid_count": sum(
            bool(row["validator_missed_raw_invalid"]) for row in rows
        ),
        "candidate_conformant_count": conformant_count,
        "hypothetical_release_eligible_count": sum(
            bool(row["hypothetical_release_eligible"]) for row in rows
        ),
        "actual_response_released_count": sum(bool(row["actual_answer_released"]) for row in rows),
        "actual_released_invalid_count": sum(bool(row["actual_released_invalid"]) for row in rows),
        "input_tokens": sum(int(row["input_tokens"]) for row in rows),
        "output_tokens": sum(int(row["output_tokens"]) for row in rows),
        "verified_provider_usage_cost_usd": str(total_verified_cost),
        "conservative_at_risk_cost_usd": str(total_at_risk_cost),
        "cost_usd": str(total_verified_cost),
        "cost_usd_legacy_alias": "verified_provider_usage_cost_usd",
        "failed_transport_reservations_reported_as_billed_cost": False,
        "verified_cost_per_candidate_conformant_usd": (
            str(total_verified_cost / conformant_count) if conformant_count else None
        ),
        "at_risk_cost_per_candidate_conformant_usd": (
            str(total_at_risk_cost / conformant_count) if conformant_count else None
        ),
        "latency_ms": {
            "median": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
    }


def _stratified_cluster_bootstrap(
    pairs: Sequence[Mapping[str, Any]],
    *,
    samples: int = 10_000,
) -> list[float]:
    strata: dict[tuple[str, str, str], dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for pair in pairs:
        stratum = (
            str(pair["split"]),
            str(pair["project_family"]),
            str(pair["scenario_type"]),
        )
        strata[stratum][str(pair["case_id"])].append(pair)
    rng = random.Random(RANDOM_SEED)
    estimates: list[float] = []
    for _ in range(samples):
        numerator = 0
        denominator = 0
        for stratum in sorted(strata):
            clusters = sorted(strata[stratum])
            for _cluster in clusters:
                selected = rng.choice(clusters)
                for pair in strata[stratum][selected]:
                    numerator += int(pair["arm_b_conformant"]) - int(pair["arm_a_conformant"])
                    denominator += 1
        estimates.append(numerator / denominator if denominator else 0.0)
    estimates.sort()
    return estimates


def exact_mcnemar(arm_a_only: int, arm_b_only: int) -> float:
    """Two-sided exact McNemar p-value using the conditional binomial test."""

    discordant = arm_a_only + arm_b_only
    if discordant == 0:
        return 1.0
    tail = min(arm_a_only, arm_b_only)
    probability = sum(math.comb(discordant, index) for index in range(tail + 1)) / (2**discordant)
    return min(1.0, 2 * probability)


def _paired_comparisons(
    branches: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int,
) -> list[dict[str, Any]]:
    primary = [
        row
        for row in branches
        if row["stage"] in {"development", "transfer"}
        and row["model_id"] in PRIMARY_MODELS
        and row["arm"] in {"A1", "A3P", "A0xF"}
    ]
    index: dict[tuple[str, str, str, int], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in primary:
        key = (
            str(row["model_id"]),
            str(row["split"]),
            str(row["case_id"]),
            int(row["presentation"]),
        )
        index[key][str(row["arm"])] = row
    output: list[dict[str, Any]] = []
    for model in PRIMARY_MODELS:
        for arm_a, arm_b in (("A1", "A3P"), ("A1", "A0xF"), ("A3P", "A0xF")):
            pairs = []
            for (row_model, _split, _case, _presentation), arms in sorted(index.items()):
                if row_model != model or arm_a not in arms or arm_b not in arms:
                    continue
                a = arms[arm_a]
                b = arms[arm_b]
                pairs.append(
                    {
                        "split": a["split"],
                        "project_family": a["project_family"],
                        "scenario_type": a["scenario_type"],
                        "case_id": a["case_id"],
                        "arm_a_conformant": bool(a["candidate_conformant"]),
                        "arm_b_conformant": bool(b["candidate_conformant"]),
                    }
                )
            a_only = sum(
                pair["arm_a_conformant"] and not pair["arm_b_conformant"] for pair in pairs
            )
            b_only = sum(
                pair["arm_b_conformant"] and not pair["arm_a_conformant"] for pair in pairs
            )
            risk_difference = (
                sum(int(pair["arm_b_conformant"]) - int(pair["arm_a_conformant"]) for pair in pairs)
                / len(pairs)
                if pairs
                else None
            )
            bootstrap = _stratified_cluster_bootstrap(
                pairs,
                samples=bootstrap_samples,
            )
            output.append(
                {
                    "model_id": model,
                    "arm_a": arm_a,
                    "arm_b": arm_b,
                    "pair_count": len(pairs),
                    "risk_difference_b_minus_a": risk_difference,
                    "stratified_cluster_bootstrap_samples": bootstrap_samples,
                    "risk_difference_bootstrap_95": (
                        [
                            _percentile(bootstrap, 0.025),
                            _percentile(bootstrap, 0.975),
                        ]
                        if bootstrap
                        else None
                    ),
                    "mcnemar": {
                        "arm_a_only_conformant": a_only,
                        "arm_b_only_conformant": b_only,
                        "exact_two_sided_p": exact_mcnemar(a_only, b_only),
                    },
                }
            )
    return output


def _causal_triads(branches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    cells: dict[tuple[str, str, str, int, str, str], Mapping[str, Any]] = {}
    for row in branches:
        if (
            row["arm"] not in {"D1", "A1", "A3P", "A0xF"}
            or row["scenario_type"] != "causal_retrieval"
            or row["stage"] != "causal_perturbation"
        ):
            continue
        base = (
            str(row["model_id"]),
            str(row["split"]),
            str(row["case_id"]),
            int(row["presentation"]),
            str(row["arm"]),
        )
        condition = str(row.get("ablation_condition") or "")
        if condition in CAUSAL_ABLATIONS:
            cells[(*base, condition)] = row
    keys = sorted({key[:-1] for key in cells})
    triads = []
    for key in keys:
        base = cells.get((*key, "evidence_original"))
        counterfactual = cells.get((*key, "counterfactual_threshold_crossing"))
        withheld = cells.get((*key, "evidence_withheld"))
        complete = all(item is not None for item in (base, counterfactual, withheld))
        sequences = {
            tuple(item.get("operation_sequence") or ())
            for item in (base, counterfactual, withheld)
            if item is not None
        }
        shapes = {
            str(item.get("protocol_shape_id") or "")
            for item in (base, counterfactual, withheld)
            if item is not None
        }
        orchestration_equivalent = bool(
            complete
            and len(sequences) == 1
            and len(shapes) == 1
            and all(sequences)
            and all(shapes)
        )
        plan_changed = bool(
            complete
            and base.get("explanation_plan_id") != counterfactual.get("explanation_plan_id")
        )
        receipts = [
            _mapping(item.get("semantic_intervention_receipt"))
            for item in (base, counterfactual, withheld)
            if item is not None
        ]
        receipt_fields = (
            "anchor_sha256",
            "tool_name",
            "opaque_evidence_id",
            "units",
            "evidence_binding",
            "orchestration_contract_sha256",
        )
        semantic_receipts_valid = bool(
            complete
            and len(receipts) == 3
            and all(validate_semantic_intervention_receipt(item) for item in receipts)
        )
        semantic_invariants_equal = bool(
            semantic_receipts_valid
            and all(len({str(item.get(field)) for item in receipts}) == 1 for field in receipt_fields)
            and len(
                {
                    json_sha256(_mapping(item.get("triad_identity")))
                    for item in receipts
                }
            )
            == 1
        )
        semantic_delta_exact = bool(
            semantic_invariants_equal
            and receipts[0].get("condition") == "evidence_original"
            and receipts[1].get("condition") == "counterfactual_threshold_crossing"
            and receipts[2].get("condition") == "evidence_withheld"
            and receipts[0].get("intervention_value") == receipts[0].get("original_value")
            and receipts[1].get("intervention_value") != receipts[1].get("original_value")
            and receipts[2].get("intervention_value") is None
            and receipts[0].get("evidence_available") is True
            and receipts[1].get("evidence_available") is True
            and receipts[2].get("evidence_available") is False
        )
        semantic_intervention_clean = bool(
            complete
            and orchestration_equivalent
            and semantic_receipts_valid
            and semantic_invariants_equal
            and semantic_delta_exact
        )
        withheld_class = (
            str(withheld.get("withheld_outcome_class"))
            if withheld is not None
            else None
        )
        successful_withholding = withheld_class in {
            "correct_refusal",
            "validator_containment",
        }
        triads.append(
            {
                "model_id": key[0],
                "split": key[1],
                "case_id": key[2],
                "presentation": key[3],
                "arm": key[4],
                "complete": complete,
                "orchestration_equivalent": orchestration_equivalent,
                "operation_sequence": list(next(iter(sequences), ())),
                "protocol_shape_id": next(iter(shapes), None),
                "counterfactual_causal": bool(
                    counterfactual and counterfactual.get("causal") is True
                ),
                "counterfactual_plan_changed": plan_changed,
                "semantic_receipts_valid": semantic_receipts_valid,
                "semantic_invariants_equal": semantic_invariants_equal,
                "semantic_delta_exact": semantic_delta_exact,
                "semantic_intervention_clean": semantic_intervention_clean,
                "semantic_intervention_receipt_sha256": [
                    item.get("body_sha256") for item in receipts
                ],
                "withheld_outcome_class": withheld_class,
                "withheld_correct_refusal": withheld_class == "correct_refusal",
                "withheld_validator_containment": (
                    withheld_class == "validator_containment"
                ),
                "withheld_infrastructure_or_provider_failure": (
                    withheld_class == "infrastructure_or_provider_failure"
                ),
                "withheld_unsafe_acceptance": withheld_class == "unsafe_acceptance",
                "withheld_successful": successful_withholding,
                "withheld_safely_contained_or_conformant": successful_withholding,
                "causal_result_interpretation": (
                    "repository_constraint_propagation_negative_control"
                    if key[4] in {"A1", "A3P"}
                    else (
                        "deterministic_repository_positive_control"
                        if key[4] == "D1"
                        else "diagnostic_free_prose_ablation"
                    )
                ),
            }
        )
    dirty_complete = [
        row for row in triads if row["complete"] and not row["semantic_intervention_clean"]
    ]
    if dirty_complete:
        raise RuntimeError("RESEARCH_SEMANTIC_INTERVENTION_TRIAD_DIRTY")
    return {
        "triad_count": len(triads),
        "complete_count": sum(row["complete"] for row in triads),
        "all_complete": bool(triads) and all(row["complete"] for row in triads),
        "orchestration_equivalent_count": sum(
            row["orchestration_equivalent"] for row in triads
        ),
        "all_orchestration_equivalent": bool(triads)
        and all(row["orchestration_equivalent"] for row in triads),
        "counterfactual_causal_count": sum(row["counterfactual_causal"] for row in triads),
        "counterfactual_plan_changed_count": sum(
            row["counterfactual_plan_changed"] for row in triads
        ),
        "withheld_safe_count": sum(
            row["withheld_successful"] for row in triads
        ),
        "withheld_outcomes": {
            "correct_refusal": sum(row["withheld_correct_refusal"] for row in triads),
            "validator_containment": sum(
                row["withheld_validator_containment"] for row in triads
            ),
            "infrastructure_or_provider_failure": sum(
                row["withheld_infrastructure_or_provider_failure"] for row in triads
            ),
            "unsafe_acceptance": sum(
                row["withheld_unsafe_acceptance"] for row in triads
            ),
        },
        "infrastructure_failures_count_as_successful_withholding": False,
        "semantic_intervention_clean_count": sum(
            row["semantic_intervention_clean"] for row in triads
        ),
        "all_semantic_interventions_clean": bool(triads)
        and all(row["semantic_intervention_clean"] for row in triads),
        "causal_attribution": (
            "This is an explicit synthesis-only intervention. System-level plan changes "
            "are induced by repository evidence constraints, not by a measured retrieval "
            "choice. Because the finite plan set has cardinality one, A1/A3P counts measure "
            "repository-constraint propagation and must not be attributed to model reasoning."
        ),
        "retrieval_causality_measured": False,
        "study_role": "synthesis_only_intervention",
        "finite_arm_model_reasoning_attribution_permitted": False,
        "finite_arm_result_classification": (
            "A1/A3P are singleton-plan repository-constraint-propagation negative controls; "
            "they do not measure agent reasoning."
        ),
        "rows": triads,
    }


def _pair_integrity(
    rows: Sequence[Mapping[str, Any]], branches: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    paired_parents = [row for row in rows if row.get("architecture_id") == "A3X"]
    parent_checks = []
    for row in paired_parents:
        details = _mapping(row.get("details"))
        branch_map = _mapping(details.get("paired_branch_outcomes"))
        receipt = details.get("shared_router_tool_receipt_sha256")
        parent_checks.append(
            {
                "attempt_id": row.get("attempt_id"),
                "two_branches": set(branch_map) == {"A3P", "A0xF"},
                "shared_receipt_valid": bool(
                    isinstance(receipt, str) and _SHA256.fullmatch(receipt)
                ),
            }
        )
    primary_pairs: dict[tuple[str, str, str, int], set[str]] = defaultdict(set)
    for row in branches:
        if row["stage"] not in {"development", "transfer"}:
            continue
        if row["model_id"] not in PRIMARY_MODELS or row["arm"] not in {
            "A1",
            "A3P",
            "A0xF",
        }:
            continue
        primary_pairs[
            (
                str(row["model_id"]),
                str(row["split"]),
                str(row["case_id"]),
                int(row["presentation"]),
            )
        ].add(str(row["arm"]))
    return {
        "paired_parent_count": len(paired_parents),
        "paired_parent_complete_count": sum(
            row["two_branches"] and row["shared_receipt_valid"] for row in parent_checks
        ),
        "paired_parent_failures": [
            row
            for row in parent_checks
            if not (row["two_branches"] and row["shared_receipt_valid"])
        ],
        "primary_pair_count": len(primary_pairs),
        "primary_complete_pair_count": sum(
            arms == {"A1", "A3P", "A0xF"} for arms in primary_pairs.values()
        ),
        "all_pairs_integral": bool(parent_checks)
        and all(row["two_branches"] and row["shared_receipt_valid"] for row in parent_checks)
        and bool(primary_pairs)
        and all(arms == {"A1", "A3P", "A0xF"} for arms in primary_pairs.values()),
    }


def analyze_research_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    bootstrap_samples: int = 10_000,
) -> dict[str, Any]:
    """Produce research-grade branch, pairing, reliability, and resource analyses."""

    branches = extract_research_branches(rows)
    breakdowns: dict[str, list[dict[str, Any]]] = {}
    dimension_specs = {
        "overall": (),
        "split": ("split",),
        "project_family": ("project_family",),
        "scenario_type": ("scenario_type",),
        "split_project_family_scenario_type": (
            "split",
            "project_family",
            "scenario_type",
        ),
    }
    for label, dimensions in dimension_specs.items():
        grouped: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
        for row in branches:
            key = (row["model_id"], row["arm"], *(row[name] for name in dimensions))
            grouped[key].append(row)
        metrics = []
        for key, values in sorted(grouped.items()):
            labels = {"model_id": str(key[0]), "arm": str(key[1])}
            labels.update(
                {name: str(value) for name, value in zip(dimensions, key[2:], strict=True)}
            )
            metrics.append(_branch_group_metrics(values, dimensions=labels))
        breakdowns[label] = metrics

    triads = _causal_triads(branches)
    integrity = _pair_integrity(rows, branches)
    verified_usage_cost = sum(
        (
            Decimal(str(row["verified_provider_usage_cost_usd"]))
            for row in branches
        ),
        Decimal(0),
    )
    conservative_at_risk_cost = sum(
        (
            Decimal(str(row["conservative_at_risk_cost_usd"]))
            for row in branches
        ),
        Decimal(0),
    )
    result = {
        "schema_version": "1.0",
        "analysis_version": "architecture-development-analysis-1",
        "study_version": STUDY_VERSION,
        "intention_to_treat_definition": (
            "Every scheduled branch is candidate-nonconformant unless provider completion, "
            "protocol, canonical evidence checks, intent/safety/causal checks, and trace "
            "acknowledgement all succeed. Actual answer release is a separate prohibited "
            "runtime event and is never required for scientific candidate conformance."
        ),
        "attempt_count": len(rows),
        "branch_count": len(branches),
        "intention_to_treat_candidate_conformance": _rate_with_wilson(
            [bool(row["candidate_conformant"]) for row in branches]
        ),
        "raw_invalid_count": sum(bool(row["raw_invalid"]) for row in branches),
        "validator_contained_count": sum(bool(row["validator_contained"]) for row in branches),
        "validator_missed_raw_invalid_count": sum(
            bool(row["validator_missed_raw_invalid"]) for row in branches
        ),
        "candidate_conformant_count": sum(bool(row["candidate_conformant"]) for row in branches),
        "hypothetical_release_eligible_count": sum(
            bool(row["hypothetical_release_eligible"]) for row in branches
        ),
        "candidate_declaration_inconsistency_count": sum(
            not bool(row["candidate_declaration_consistent"]) for row in branches
        ),
        "actual_response_released_count": sum(
            bool(row["actual_answer_released"]) for row in branches
        ),
        "actual_released_invalid_count": sum(
            bool(row["actual_released_invalid"]) for row in branches
        ),
        "conformance_flow": {
            "scheduled_branches": len(branches),
            "raw_invalid": sum(bool(row["raw_invalid"]) for row in branches),
            "validator_contained": sum(bool(row["validator_contained"]) for row in branches),
            "candidate_conformant": sum(bool(row["candidate_conformant"]) for row in branches),
            "actual_responses_released": sum(
                bool(row["actual_answer_released"]) for row in branches
            ),
            "actual_released_invalid": sum(
                bool(row["actual_released_invalid"]) for row in branches
            ),
        },
        "production_non_release_enforced": bool(branches)
        and all(row["actual_non_release_enforced"] for row in branches),
        "false_refusal_count": sum(bool(row["false_refusal"]) for row in branches),
        "pair_integrity": integrity,
        "causal_triads": triads,
        "cost_accounting": {
            "verified_provider_usage_cost_usd": str(verified_usage_cost),
            "conservative_at_risk_cost_usd": str(conservative_at_risk_cost),
            "verified_cost_basis": (
                "pricing_snapshot_times_provider_reported_tokens_from_completed_responses"
            ),
            "at_risk_cost_basis": (
                "pricing_snapshot_times_retry_and_error_token_envelopes"
            ),
            "provider_reported_currency_cost_available": False,
            "failed_transport_reservation_is_billed_cost": False,
        },
        "paired_risk_differences": _paired_comparisons(
            branches,
            bootstrap_samples=bootstrap_samples,
        ),
        "breakdowns": breakdowns,
        "branches": branches,
        "claim_boundary": (
            "Exploratory automated conformance, causal behavior, reliability, and resource "
            "evidence only; practitioner usefulness and engineering outcomes are not measured."
        ),
    }
    return {**result, "analysis_sha256": json_sha256(result)}


def _critical_failure(row: Mapping[str, Any]) -> bool:
    details = row.get("details")
    paired_invalid = False
    if row.get("architecture_id") == "A3X":
        branches = details.get("paired_branch_outcomes") if isinstance(details, Mapping) else None
        receipt = (
            details.get("shared_router_tool_receipt_sha256")
            if isinstance(details, Mapping)
            else None
        )
        paired_invalid = not (
            isinstance(branches, Mapping)
            and set(branches) == {"A3P", "A0xF"}
            and isinstance(receipt, str)
            and _SHA256.fullmatch(receipt)
        )
    return bool(
        row.get("status") != "completed"
        or row.get("protocol_valid") is not True
        or row.get("safe") is not True
        or row.get("evidence_valid") is not True
        or row.get("repository_fidelity") is not True
        or row.get("injection_safe") is not True
        or row.get("trace_acknowledged") is not True
        or row.get("causal") is False
        or row.get("intent_correct") is False
        or row.get("safety_intent_correct") is False
        or row.get("false_refusal") is True
        or paired_invalid
    )


def summarize_research_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    analysis = dict(analysis or analyze_research_rows(rows))
    causal_summary = {
        key: value for key, value in analysis["causal_triads"].items() if key != "rows"
    }
    analysis_summary = {
        key: value for key, value in analysis.items() if key not in {"branches", "causal_triads"}
    }
    analysis_summary["causal_triads"] = causal_summary
    return {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "attempt_count": len(rows),
        "branch_count": analysis["branch_count"],
        "generated_invalid_count": analysis["raw_invalid_count"],
        "validator_contained_count": analysis["validator_contained_count"],
        "validator_missed_raw_invalid_count": analysis["validator_missed_raw_invalid_count"],
        "candidate_conformant_count": analysis["candidate_conformant_count"],
        "hypothetical_release_eligible_count": analysis["hypothetical_release_eligible_count"],
        "candidate_declaration_inconsistency_count": analysis[
            "candidate_declaration_inconsistency_count"
        ],
        "intention_to_treat_candidate_conformance": analysis[
            "intention_to_treat_candidate_conformance"
        ],
        "actual_response_released_count": analysis["actual_response_released_count"],
        "actual_released_invalid_count": analysis["actual_released_invalid_count"],
        "production_non_release_enforced": analysis["production_non_release_enforced"],
        "conformance_flow": analysis["conformance_flow"],
        "configurations": analysis["breakdowns"]["overall"],
        "research_analysis": analysis_summary,
        "claim_boundary": (
            "Exploratory automated behavior only; no release, practitioner, hydraulic, "
            "or ecological claim follows from these rows."
        ),
    }


def finalize_exploratory_study(
    store: CampaignStore,
    *,
    preregistration: Mapping[str, Any],
    schedules: Mapping[str, Sequence[AttemptSpec]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = {spec.attempt_id for specs in schedules.values() for spec in specs}
    rows = store.attempts()
    observed = {str(row["attempt_id"]) for row in rows}
    if observed != expected:
        raise RuntimeError("EXPLORATORY_STUDY_INCOMPLETE")
    if any(store.completed_time_blocks(stage) != set(TIME_BLOCKS) for stage in RESEARCH_STAGES):
        raise RuntimeError("EXPLORATORY_TIME_BLOCKS_INCOMPLETE")
    summary = summarize_research_rows(rows)
    if summary["production_non_release_enforced"] is not True:
        raise RuntimeError("EXPLORATORY_ACTUAL_ANSWER_RELEASE_PROHIBITED")
    summary_body = {
        **summary,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "schedule_sha256": preregistration["schedule"]["schedule_sha256"],
        "usage": store.usage(),
        "prior_r2b_usage": store.prior_usage(),
        "practitioner_usefulness_status": "not_measured",
        "release_eligible": False,
        "structural_plan_analysis": preregistration["structural_plan_analysis"],
    }
    summary = {**summary_body, "summary_sha256": json_sha256(summary_body)}
    terminal_body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "disposition": TERMINAL_DISPOSITION,
        "production_decision": R2B_REQUIRED_DECISION,
        "assistant_release_basis_created": False,
        "release_eligible": False,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "schedule_sha256": preregistration["schedule"]["schedule_sha256"],
        "summary_sha256": summary["summary_sha256"],
        "attempt_count": len(rows),
        "usage": store.usage(),
        "claim_boundary": preregistration["claim_boundary"],
        "practitioner_usefulness_status": "not_measured",
    }
    terminal = {**terminal_body, "receipt_sha256": json_sha256(terminal_body)}
    store.put_receipt("exploratory_summary", summary)
    store.put_receipt("terminal_disposition", terminal)
    return summary, terminal


def export_research_tables(store: CampaignStore, output_dir: Path) -> dict[str, str]:
    """Export public, credential-free rows without release-gate vocabulary."""

    output_dir.mkdir(parents=True, exist_ok=True)
    rows = store.attempts()
    analysis = analyze_research_rows(rows)
    summary = summarize_research_rows(rows, analysis=analysis)
    causal = analysis["causal_triads"]
    preregistration = _mapping(store.get_meta("preregistration"))
    structural = _mapping(preregistration.get("structural_plan_analysis"))
    tables: dict[str, Any] = {
        "attempts": {"schema_version": "1.0", "rows": rows},
        "branch-outcomes": {
            "schema_version": "1.0",
            "rows": analysis["branches"],
        },
        "causal-perturbations": {
            "schema_version": "1.0",
            "rows": [row for row in rows if row["stage"] == "causal_perturbation"],
        },
        "causal-triads": {
            "schema_version": "1.0",
            "rows": causal["rows"],
        },
        "paired-branches": {
            "schema_version": "1.0",
            "rows": [row for row in rows if row["architecture_id"] == "A3X"],
        },
        "paired-risk-differences": {
            "schema_version": "1.0",
            "rows": analysis["paired_risk_differences"],
        },
        "metric-breakdowns": {
            "schema_version": "1.0",
            "rows": [
                {"breakdown": label, **row}
                for label, values in analysis["breakdowns"].items()
                for row in values
            ],
        },
        "pair-integrity": {
            "schema_version": "1.0",
            "rows": [analysis["pair_integrity"]],
        },
        "operational-stratum": {
            "schema_version": "1.0",
            "rows": [row for row in rows if row["stage"] == "operational_stratum"],
        },
        "configuration-summary": summary,
        "structural-plan-analysis": structural,
        "cost-ledger": {
            "schema_version": "2.0",
            "rows": [
                {
                    "ledger_role": "analysis_observation",
                    **analysis["cost_accounting"],
                },
                {
                    "ledger_role": "campaign_budget_safety_envelope",
                    **store.usage(),
                    "cost_usd_semantics": (
                        "cumulative budget debit including conservative uncertain-attempt reservations"
                    ),
                    "failed_transport_reservation_is_billed_cost": False,
                },
            ],
        },
    }
    hashes: dict[str, str] = {}
    for name, value in tables.items():
        path = output_dir / f"{name}.json"
        write_json_atomic(path, value)
        hashes[name] = file_sha256(path)
    manifest_body = {
        "schema_version": "1.0",
        "study_version": STUDY_VERSION,
        "release_eligible": False,
        "table_sha256": dict(sorted(hashes.items())),
    }
    manifest = {**manifest_body, "manifest_sha256": json_sha256(manifest_body)}
    manifest_path = output_dir / "manifest.json"
    write_json_atomic(manifest_path, manifest)
    hashes["manifest"] = file_sha256(manifest_path)
    return hashes


def source_hashes_for_study(root: Path, pricing_path: Path) -> dict[str, str]:
    files = {
        "architecture_development_orchestrator": root
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_architecture_dev.py",
        "architecture_development_cli": root
        / "scripts"
        / "run_evidence_guide_v2_architecture_dev.py",
        "architecture_development_executor": root
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_architecture_executor.py",
        "architecture_r1_incident_validator": root
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_architecture_incident.py",
        "paired_architecture_executor": root
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_paired.py",
        "campaign_mechanics": root
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_campaign.py",
        "provider_executor": root / "python" / "gi_scenario_core" / "evidence_guide_v2_provider.py",
        "deterministic_scorer": root / "python" / "gi_scenario_core" / "evidence_evaluation_v2.py",
        "inference_adapter": root / "python" / "gi_scenario_core" / "inference.py",
        "weave_trace_contract": root / "python" / "gi_scenario_core" / "observability.py",
        "cloud_projection": root / "python" / "gi_scenario_core" / "evidence_guide_v2_cloud.py",
        "shared_runtime_protocol": root
        / "python"
        / "gi_scenario_core"
        / "evidence_guide_v2_protocol.py",
        "production_evidence_guide": root / "python" / "gi_scenario_core" / "evidence_guide.py",
        "pricing_snapshot": pricing_path,
    }
    return {name: file_sha256(path.resolve()) for name, path in files.items()}


__all__ = [
    "CAUSAL_ABLATIONS",
    "DEFAULT_DESIGN",
    "OUTPUT_DIRECTORY_NAME",
    "PRIMARY_MODELS",
    "RESEARCH_ARCHITECTURES",
    "RESEARCH_EXECUTION_MODES",
    "RESEARCH_MODELS",
    "RESEARCH_STAGES",
    "STUDY_VERSION",
    "TERMINAL_DISPOSITION",
    "ArchitectureDevelopmentDesign",
    "ResearchAttemptSpec",
    "ResearchFakeExecutor",
    "analyze_research_rows",
    "apply_predecessor_debits",
    "apply_r2b_debit",
    "build_research_preregistration",
    "build_structural_plan_analysis",
    "build_study_schedule",
    "exact_mcnemar",
    "export_research_tables",
    "extract_research_branches",
    "finalize_exploratory_study",
    "schedule_counts",
    "schedule_records",
    "source_hashes_for_study",
    "summarize_research_rows",
    "validate_r1_architecture_incident_receipt",
    "validate_r2b_terminal_receipt",
]
