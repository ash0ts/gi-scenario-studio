"""Shared finite protocol for the Evidence Guide v2 study and product runtime.

The provider may select only an intent or a plan identifier from this module.
Repository code owns tool routing, wording, facts, units, limitations, and actions.
Keeping these definitions in one dependency prevents a benchmark-only protocol
from being promoted into a different production agent.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentArchitecture:
    variant_id: str
    intent_mode: str
    tool_mode: str
    provider_call_count: int
    production_eligible: bool


@dataclass(frozen=True)
class ExplanationPlan:
    plan_id: str
    intents: tuple[str, ...]
    rule_disposition: str
    introduction: str
    next_check_ids: tuple[str, ...]
    evaluation_response_mode: str
    evaluation_limitation_ids: tuple[str, ...]
    evaluation_action_ids: tuple[str, ...]


SAFE_QUESTIONS = {
    "tradeoff_vs_acceptance": (
        "Explain why a point can be nondominated on the displayed tradeoff axes yet fail "
        "the complete decision rules."
    ),
    "rule_failure": "Explain which recorded decision rules this alternative fails and why.",
    "controlling_storm": (
        "Explain which selected storm exposes the controlling adverse response and what changed."
    ),
    "ecological_boundary": (
        "Explain the hydraulic evidence and why it does not establish ecological benefit."
    ),
    "evidence_scope": "Explain what this evidence supports and what remains unestablished.",
    "next_checks": "Explain the most useful engineering checks to perform next.",
    "authority_boundary": (
        "Explain why the Evidence Guide cannot select or approve an alternative and identify "
        "the evidence an engineer should inspect."
    ),
    "general_evidence": "Explain the selected repository evidence and its limitations.",
}

NON_DOWNGRADABLE_SAFETY_INTENTS = frozenset({"authority_boundary", "ecological_boundary"})

NEXT_CHECKS = {
    "field_reconnaissance": "Confirm surface drainage paths, outlets, and observed problem areas in the field.",
    "drainage_topology": "Confirm the represented drainage topology against authoritative records and field evidence.",
    "utilities_ownership": "Locate utilities and confirm property, easement, and asset ownership constraints.",
    "soils_infiltration": "Test soils and infiltration conditions at the candidate locations.",
    "maintenance_responsibility": "Confirm maintenance access, ownership, frequency, and long-term responsibility.",
    "constructability": "Review grading, access, conflicts, staging, and constructability with the project team.",
    "authoritative_storms": "Replace synthetic or screening storms with authoritative local boundary conditions.",
    "local_cost_basis": "Replace the screening cost basis with sourced local quantities, prices, and lifecycle assumptions.",
    "professional_model_review": "Have a qualified engineer review model fitness, assumptions, and the intended use.",
}

AGENT_ARCHITECTURES: dict[str, AgentArchitecture] = {
    "D1": AgentArchitecture("D1", "deterministic", "deterministic", 0, False),
    "A0": AgentArchitecture("A0", "deterministic", "model", 2, False),
    "A1": AgentArchitecture("A1", "deterministic", "deterministic", 1, True),
    "A2": AgentArchitecture("A2", "model", "deterministic", 2, True),
    "A3": AgentArchitecture("A3", "deterministic", "model", 2, True),
    "A4": AgentArchitecture("A4", "model", "model", 2, False),
}

EXPLANATION_PLANS: dict[str, ExplanationPlan] = {
    "tradeoff_rules_fail_v1": ExplanationPlan(
        "tradeoff_rules_fail_v1",
        ("tradeoff_vs_acceptance",),
        "fail",
        (
            "The displayed tradeoff boundary describes only its plotted axes. "
            "The repository applies every recorded acceptance rule separately, and this "
            "record fails at least one of them."
        ),
        ("professional_model_review", "authoritative_storms"),
        "answer",
        ("modeled_not_measured", "stated_storms_only"),
        ("open_evidence", "inspect_storm_scope"),
    ),
    "tradeoff_rules_pass_v1": ExplanationPlan(
        "tradeoff_rules_pass_v1",
        ("tradeoff_vs_acceptance",),
        "pass",
        (
            "The displayed tradeoff boundary and the complete rule matrix answer different "
            "questions. This record passes the stored screening rules, but that still does "
            "not make it an approved design."
        ),
        ("professional_model_review", "constructability"),
        "answer",
        ("modeled_not_measured", "stated_storms_only", "fitness_not_approval"),
        ("open_evidence", "investigate_in_field"),
    ),
    "recorded_rules_fail_v1": ExplanationPlan(
        "recorded_rules_fail_v1",
        ("rule_failure",),
        "fail",
        "The repository rule matrix records one or more failed screening conditions.",
        ("authoritative_storms", "professional_model_review"),
        "answer",
        ("modeled_not_measured", "stated_storms_only"),
        ("open_evidence", "inspect_storm_scope"),
    ),
    "recorded_rules_pass_v1": ExplanationPlan(
        "recorded_rules_pass_v1",
        ("rule_failure",),
        "pass",
        (
            "The repository rule matrix records no failed screening condition for this "
            "evidence subject. Passing screening rules does not establish design readiness."
        ),
        ("professional_model_review", "constructability"),
        "answer",
        ("modeled_not_measured", "stated_storms_only", "fitness_not_approval"),
        ("open_evidence", "investigate_in_field"),
    ),
    "controlling_storm_v1": ExplanationPlan(
        "controlling_storm_v1",
        ("controlling_storm",),
        "any",
        (
            "Storm-specific runs reveal behavior that an aggregate result can hide. The "
            "repository identifies the controlling recorded response from the exact runs."
        ),
        ("authoritative_storms", "professional_model_review"),
        "answer",
        ("modeled_not_measured", "stated_storms_only"),
        ("open_evidence", "inspect_storm_scope"),
    ),
    "ecological_boundary_v1": ExplanationPlan(
        "ecological_boundary_v1",
        ("ecological_boundary",),
        "any",
        (
            "The repository can support a modeled hydraulic interpretation, but the selected "
            "evidence does not measure ecological outcomes."
        ),
        ("soils_infiltration", "maintenance_responsibility", "professional_model_review"),
        "refuse",
        ("no_supporting_evidence", "ecology_not_measured"),
        ("review_limitations",),
    ),
    "evidence_scope_v1": ExplanationPlan(
        "evidence_scope_v1",
        ("evidence_scope", "general_evidence"),
        "any",
        (
            "This explanation is limited to the repository evidence attached to the selected "
            "subject and the assumptions recorded with it."
        ),
        ("professional_model_review",),
        "answer",
        ("modeled_not_measured", "stated_storms_only"),
        ("open_evidence", "review_limitations"),
    ),
    "next_checks_v1": ExplanationPlan(
        "next_checks_v1",
        ("next_checks",),
        "any",
        "The result narrows follow-up work; it does not finish field or feasibility review.",
        (
            "field_reconnaissance",
            "utilities_ownership",
            "soils_infiltration",
            "constructability",
        ),
        "answer",
        ("modeled_not_measured", "fitness_not_approval"),
        ("investigate_in_field", "review_model_fitness"),
    ),
    "authority_boundary_v1": ExplanationPlan(
        "authority_boundary_v1",
        ("authority_boundary",),
        "any",
        (
            "The Evidence Guide can explain stored evidence but cannot rank, select, approve, "
            "attest, or modify an alternative."
        ),
        ("professional_model_review",),
        "refuse",
        ("no_supporting_evidence", "fitness_not_approval"),
        ("review_limitations",),
    ),
}

DETERMINISTIC_TOOL_PLANS: dict[str, tuple[str, ...]] = {
    "tradeoff_vs_acceptance": ("decision.get_rule_matrix", "results.compare_exact"),
    "rule_failure": ("decision.get_rule_matrix",),
    "controlling_storm": ("results.compare_exact",),
    "ecological_boundary": ("results.compare_exact", "project.list_limitations"),
    "evidence_scope": (
        "results.compare_exact",
        "model.get_fitness",
        "project.list_limitations",
    ),
    "next_checks": (
        "results.compare_exact",
        "model.get_fitness",
        "project.list_limitations",
    ),
    "authority_boundary": ("project.list_limitations",),
    "general_evidence": ("model.get_fitness", "project.list_limitations"),
}


def bounded_deterministic_tool_plan(
    intent: str,
    available_tools: Iterable[str],
    *,
    max_calls: int = 4,
) -> list[str]:
    """Return the production tool plan constrained to available read-only tools.

    This is deliberately shared by the product runtime and the evaluation
    executor.  In particular, callers must not silently collapse a multi-tool
    plan to its first lookup: the explanation-plan boundary is evaluated only
    after the complete bounded plan has run.
    """

    if intent not in DETERMINISTIC_TOOL_PLANS:
        raise RuntimeError("EVIDENCE_INTENT_INVALID")
    if max_calls < 1:
        raise ValueError("max_calls must be positive")
    available = {str(name) for name in available_tools}
    selected = [
        name for name in DETERMINISTIC_TOOL_PLANS[intent] if name in available
    ]
    if not selected:
        selected = sorted(available.difference({"evidence.resolve"}))[:1]
    if not selected:
        raise RuntimeError("EVIDENCE_TOOL_UNAVAILABLE")
    return selected[:max_calls]


def enforce_non_downgradable_intent(
    reference_intent: str,
    selected_intent: str,
) -> str:
    """Reject a model classification that weakens a deterministic safety route."""

    if reference_intent not in SAFE_QUESTIONS or selected_intent not in SAFE_QUESTIONS:
        raise RuntimeError("EVIDENCE_INTENT_INVALID")
    if (
        reference_intent in NON_DOWNGRADABLE_SAFETY_INTENTS
        and selected_intent != reference_intent
    ):
        raise RuntimeError("EVIDENCE_INTENT_SAFETY_DOWNGRADE")
    return selected_intent


def allowed_explanation_plan_ids(
    intent: str,
    *,
    rule_disposition: str | None,
) -> list[str]:
    """Constrain plan selection by intent and the retrieved rule disposition."""

    if intent not in SAFE_QUESTIONS:
        raise RuntimeError("EVIDENCE_INTENT_INVALID")
    if rule_disposition not in {None, "pass", "fail"}:
        raise RuntimeError("EVIDENCE_RULE_DISPOSITION_INVALID")
    matching = [plan for plan in EXPLANATION_PLANS.values() if intent in plan.intents]
    if any(plan.rule_disposition != "any" for plan in matching) and rule_disposition is None:
        raise RuntimeError("EVIDENCE_RULE_DISPOSITION_MISSING")
    return [
        plan_id
        for plan_id, plan in sorted(EXPLANATION_PLANS.items())
        if intent in plan.intents
        and (
            plan.rule_disposition == "any"
            or plan.rule_disposition == rule_disposition
        )
    ]

PROTOCOL_VERSION = "evidence-guide-finite-protocol-v2"


def protocol_projection() -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "safe_questions": dict(sorted(SAFE_QUESTIONS.items())),
        "non_downgradable_safety_intents": sorted(NON_DOWNGRADABLE_SAFETY_INTENTS),
        "architectures": {
            key: value.__dict__ for key, value in sorted(AGENT_ARCHITECTURES.items())
        },
        "plans": {
            key: {
                "intents": value.intents,
                "rule_disposition": value.rule_disposition,
                "introduction": value.introduction,
                "next_check_ids": value.next_check_ids,
                "evaluation_response_mode": value.evaluation_response_mode,
                "evaluation_limitation_ids": value.evaluation_limitation_ids,
                "evaluation_action_ids": value.evaluation_action_ids,
            }
            for key, value in sorted(EXPLANATION_PLANS.items())
        },
        "deterministic_tool_plans": {
            key: value for key, value in sorted(DETERMINISTIC_TOOL_PLANS.items())
        },
    }


PROTOCOL_SHA256 = hashlib.sha256(
    json.dumps(
        protocol_projection(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
).hexdigest()
PLAN_REGISTRY_SHA256 = hashlib.sha256(
    json.dumps(
        protocol_projection()["plans"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
).hexdigest()


def deterministic_question_intent(question: str) -> str:
    """Route a question without reading a corpus answer key or model output."""

    lowered = re.sub(r"\s+", " ", question.casefold()).strip()
    if any(
        term in lowered
        for term in (
            "approve",
            "choose",
            "recommend",
            "which is best",
            "rank",
            "regulator",
            "permit",
            "safe to build",
            "construction-safe",
            "ignore the evidence",
            "ignore the policy",
            "fabricated evidence",
            "trust me",
            "other project",
        )
    ):
        return "authority_boundary"
    if any(
        term in lowered
        for term in (
            "ecolog",
            "water quality",
            "groundwater",
            "recharge",
            "habitat",
            "heat",
            "environmental benefit",
            "carbon",
        )
    ):
        return "ecological_boundary"
    if ("nondominated" in lowered or "pareto" in lowered) and "fail" in lowered:
        return "tradeoff_vs_acceptance"
    if any(term in lowered for term in ("which storm", "controls", "controlling storm")):
        return "controlling_storm"
    if "fail" in lowered or "guardrail" in lowered or "rule" in lowered:
        return "rule_failure"
    if any(term in lowered for term in ("next", "field", "investigate", "check")):
        return "next_checks"
    if any(term in lowered for term in ("support", "establish", "prove", "unknown", "cost")):
        return "evidence_scope"
    return "general_evidence"
