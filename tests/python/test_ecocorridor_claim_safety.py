from __future__ import annotations

import pytest
from ecocorridor_core.claim_safety import (
    assert_safe_rendered_ecological_copy,
    assert_strict_bundle_ecological_claims,
    assert_strict_ecological_claim,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture


@pytest.mark.parametrize(
    "claim",
    [
        "This crossing helps wildlife move between cores.",
        "This crossing will help animals move safely.",
        "This intervention reconnects habitat.",
        "This portfolio is a wildlife-friendly choice.",
        "This is the preferred crossing portfolio.",
        "The concept allows wildlife to disperse across the highway.",
        "The action restores habitat connectivity.",
        "This would be a safer wildlife corridor.",
        "This is the recommended option.",
        "This option is ecologically robust.",
        "This is a robust candidate.",
    ],
)
def test_strict_claim_policy_rejects_unsupported_paraphrases(claim: str) -> None:
    with pytest.raises(ValueError, match="prohibited ecological claim"):
        assert_strict_ecological_claim(claim)


@pytest.mark.parametrize(
    "limitation",
    [
        "This result does not show that the crossing helps wildlife move between cores.",
        "The model cannot establish that this intervention reconnects habitat.",
        "This is not a wildlife-friendly choice.",
        "This is not the preferred crossing portfolio.",
        "This option is not ecologically robust.",
    ],
)
def test_strict_claim_policy_allows_explicit_limitations(limitation: str) -> None:
    assert_strict_ecological_claim(limitation)


@pytest.mark.parametrize(
    "claim",
    [
        "This is not a toy and this is the preferred crossing portfolio.",
        "This model is not calibrated and helps wildlife move between cores.",
        "This project not only studies roads; this intervention reconnects habitat.",
    ],
)
def test_unrelated_or_not_only_negation_cannot_hide_a_claim(claim: str) -> None:
    with pytest.raises(ValueError, match="prohibited ecological claim"):
        assert_strict_ecological_claim(claim)


def test_strict_bundle_policy_accepts_the_bounded_ridgeway_input_contract() -> None:
    assert_strict_bundle_ecological_claims(build_ridgeway_fixture().bundle)


def test_rendered_copy_policy_reports_the_nested_field() -> None:
    with pytest.raises(ValueError, match=r"\$\.portfolios\[0\]\.summary"):
        assert_safe_rendered_ecological_copy(
            {"portfolios": [{"summary": "This intervention reconnects habitat."}]}
        )


def test_rendered_copy_policy_treats_only_structural_non_goals_as_limitations() -> None:
    limitation = "Population viability or biodiversity benefit"
    assert_safe_rendered_ecological_copy({"decisionBrief": {"nonGoals": [limitation]}})
    with pytest.raises(ValueError, match="prohibited ecological claim"):
        assert_safe_rendered_ecological_copy({"decisionBrief": {"summary": limitation}})
