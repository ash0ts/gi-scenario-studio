"""Reviewed professional cost-basis ledger helpers.

The public v1.0 ``ConnectivityCostBasis`` contract deliberately keeps its
assumption ledger as ordered text.  Professional authoring still needs a
strict, durable interpretation of the decision assumptions before a cost can
enter analysis evidence.  This module defines that interpretation without
changing the frozen Ridgeway scientific contracts during a protected study.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .contracts import ConnectivityCostBasis, InterventionOption

ANALYSIS_HORIZON_PREFIX = "Analysis horizon years:"
REAL_DISCOUNT_RATE_PREFIX = "Real discount rate percent:"
CAPEX_BASIS_PREFIX = "CAPEX basis:"
OM_ASSUMPTIONS_PREFIX = "O&M assumptions:"
REPLACEMENT_ASSUMPTIONS_PREFIX = "Replacement assumptions:"
REVIEWER_CONFIRMATION_PREFIX = "Reviewer confirmation:"

REQUIRED_LEDGER_PREFIXES = (
    ANALYSIS_HORIZON_PREFIX,
    REAL_DISCOUNT_RATE_PREFIX,
    CAPEX_BASIS_PREFIX,
    OM_ASSUMPTIONS_PREFIX,
    REPLACEMENT_ASSUMPTIONS_PREFIX,
    REVIEWER_CONFIRMATION_PREFIX,
)


@dataclass(frozen=True, slots=True)
class CostBasisRequirement:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ReviewedCostBasisLedger:
    analysis_horizon_years: int
    real_discount_rate_percent: float
    capex_basis: str
    om_assumptions: str
    replacement_assumptions: str
    reviewer_confirmation: str


def _ledger_values(assumptions: list[str]) -> tuple[dict[str, str], list[CostBasisRequirement]]:
    values: dict[str, str] = {}
    findings: list[CostBasisRequirement] = []
    for prefix in REQUIRED_LEDGER_PREFIXES:
        matching = [item.strip() for item in assumptions if item.strip().startswith(prefix)]
        if not matching:
            findings.append(
                CostBasisRequirement(
                    "COST_BASIS_LEDGER_FIELD_MISSING",
                    f"Add one reviewed `{prefix}` entry to the CostBasis assumption ledger.",
                )
            )
            continue
        if len(matching) != 1:
            findings.append(
                CostBasisRequirement(
                    "COST_BASIS_LEDGER_FIELD_DUPLICATED",
                    f"Keep exactly one reviewed `{prefix}` entry in the CostBasis assumption ledger.",
                )
            )
            continue
        value = matching[0][len(prefix) :].strip()
        if not value:
            findings.append(
                CostBasisRequirement(
                    "COST_BASIS_LEDGER_FIELD_EMPTY",
                    f"Complete the reviewed value after `{prefix}`.",
                )
            )
            continue
        values[prefix] = value
    return values, findings


def cost_basis_requirements(
    basis: ConnectivityCostBasis | None,
    options: list[InterventionOption],
) -> list[CostBasisRequirement]:
    """Return deterministic professional blockers for a reviewed cost basis."""

    if basis is None:
        return [
            CostBasisRequirement(
                "COST_BASIS_MISSING",
                "Save and explicitly confirm a reviewed CostBasis before planning direct runs.",
            )
        ]

    findings: list[CostBasisRequirement] = []
    for field, label in (
        (basis.source, "source"),
        (basis.currency, "currency"),
        (basis.vintage, "price vintage"),
        (basis.reviewer, "reviewer"),
    ):
        if not field.strip():
            findings.append(
                CostBasisRequirement(
                    "COST_BASIS_REQUIRED_FIELD_EMPTY",
                    f"Enter the reviewed CostBasis {label}.",
                )
            )

    expected_costs = {item.option_id: item.cost for item in options}
    if basis.option_costs != expected_costs:
        findings.append(
            CostBasisRequirement(
                "COST_BASIS_OPTION_COVERAGE_MISMATCH",
                "CostBasis option costs must exactly cover every retained intervention option.",
            )
        )
    if any(item.currency != basis.currency for item in options):
        findings.append(
            CostBasisRequirement(
                "COST_BASIS_CURRENCY_MISMATCH",
                "Every retained intervention option must use the reviewed CostBasis currency.",
            )
        )
    if any(item.cost_vintage != basis.vintage for item in options):
        findings.append(
            CostBasisRequirement(
                "COST_BASIS_VINTAGE_MISMATCH",
                "Every retained intervention option must use the reviewed CostBasis price vintage.",
            )
        )

    values, ledger_findings = _ledger_values(basis.assumptions)
    findings.extend(ledger_findings)
    if ledger_findings:
        return findings

    horizon_text = values[ANALYSIS_HORIZON_PREFIX]
    if re.fullmatch(r"[1-9][0-9]*", horizon_text) is None:
        findings.append(
            CostBasisRequirement(
                "COST_BASIS_HORIZON_INVALID",
                "Analysis horizon years must be a positive whole number.",
            )
        )
    else:
        horizon = int(horizon_text)
        if horizon > 200:
            findings.append(
                CostBasisRequirement(
                    "COST_BASIS_HORIZON_INVALID",
                    "Analysis horizon years must not exceed 200.",
                )
            )

    try:
        discount_rate = float(values[REAL_DISCOUNT_RATE_PREFIX])
    except ValueError:
        discount_rate = math.nan
    if not math.isfinite(discount_rate) or not 0 <= discount_rate <= 100:
        findings.append(
            CostBasisRequirement(
                "COST_BASIS_DISCOUNT_RATE_INVALID",
                "Real discount rate percent must be a finite value from 0 through 100.",
            )
        )
    return findings


def parse_reviewed_cost_basis(
    basis: ConnectivityCostBasis,
    options: list[InterventionOption],
) -> ReviewedCostBasisLedger:
    findings = cost_basis_requirements(basis, options)
    if findings:
        raise ValueError("; ".join(item.message for item in findings))
    values, _ = _ledger_values(basis.assumptions)
    return ReviewedCostBasisLedger(
        analysis_horizon_years=int(values[ANALYSIS_HORIZON_PREFIX]),
        real_discount_rate_percent=float(values[REAL_DISCOUNT_RATE_PREFIX]),
        capex_basis=values[CAPEX_BASIS_PREFIX],
        om_assumptions=values[OM_ASSUMPTIONS_PREFIX],
        replacement_assumptions=values[REPLACEMENT_ASSUMPTIONS_PREFIX],
        reviewer_confirmation=values[REVIEWER_CONFIRMATION_PREFIX],
    )


def with_reviewer_confirmation(
    basis: ConnectivityCostBasis,
    confirmation_statement: str,
) -> ConnectivityCostBasis:
    statement = confirmation_statement.strip()
    if len(statement) < 20:
        raise ValueError("reviewer confirmation must be at least 20 characters")
    assumptions = [
        item.strip()
        for item in basis.assumptions
        if item.strip() and not item.strip().startswith(REVIEWER_CONFIRMATION_PREFIX)
    ]
    assumptions.append(f"{REVIEWER_CONFIRMATION_PREFIX} {statement}")
    return basis.model_copy(update={"assumptions": assumptions})
