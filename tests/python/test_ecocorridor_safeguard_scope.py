from __future__ import annotations

from ecocorridor_core.contracts import ConnectivityProjectBundle, RuleMetric
from ecocorridor_core.professional_validation import validate_professional_bundle
from ecocorridor_core.ridgeway import build_ridgeway_fixture


def _blocking_codes(bundle: ConnectivityProjectBundle) -> set[str]:
    return {
        finding.code
        for finding in validate_professional_bundle(bundle)
        if finding.severity == "BLOCKING"
    }


def test_safeguard_record_cannot_claim_a_scenario_from_another_guild() -> None:
    fixture = build_ridgeway_fixture()
    retained = fixture.bundle.safeguards[0]
    cross_guild = retained.model_copy(
        update={
            "safeguard_id": "safeguard_cross_guild",
            "guild_id": "target_guild_a",
        }
    )
    bundle = fixture.bundle.model_copy(update={"safeguards": [cross_guild]})

    assert "SAFEGUARD_REFERENCE_INVALID" in _blocking_codes(bundle)
    finding = next(
        item
        for item in validate_professional_bundle(bundle)
        if item.code == "SAFEGUARD_REFERENCE_INVALID"
    )
    assert finding.object_reference == cross_guild.safeguard_id
    assert set(retained.scenario_ids).issubset(
        finding.remediation.split(": ", 1)[1].rstrip(".").split(", ")
    )


def test_safeguard_scenario_cannot_be_owned_by_overlapping_records() -> None:
    fixture = build_ridgeway_fixture()
    retained = fixture.bundle.safeguards[0]
    overlapping_scenario_id = retained.scenario_ids[0]
    overlapping = retained.model_copy(
        update={
            "safeguard_id": "safeguard_overlapping_owner",
            "scenario_ids": [overlapping_scenario_id],
        }
    )
    bundle = fixture.bundle.model_copy(update={"safeguards": [retained, overlapping]})

    assert "SAFEGUARD_SCENARIO_DUPLICATE" in _blocking_codes(bundle)
    finding = next(
        item
        for item in validate_professional_bundle(bundle)
        if item.code == "SAFEGUARD_SCENARIO_DUPLICATE"
    )
    assert finding.object_reference == overlapping.safeguard_id
    assert overlapping_scenario_id in finding.remediation
    assert retained.safeguard_id in finding.remediation


def test_safeguard_scenarios_require_a_required_nonregression_rule() -> None:
    fixture = build_ridgeway_fixture()
    bundle = fixture.bundle.model_copy(
        update={
            "decision_rules": [
                rule
                for rule in fixture.bundle.decision_rules
                if rule.metric != RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION
            ]
        }
    )

    findings = validate_professional_bundle(bundle)
    missing = [item for item in findings if item.code == "SAFEGUARD_NONREGRESSION_RULE_MISSING"]

    assert len(missing) == 1
    assert missing[0].severity == "BLOCKING"
    assert missing[0].object_reference == "decision_rules"
    assert "every safeguard scenario" in missing[0].remediation
    assert "every required included focal pair" in missing[0].remediation


def test_required_safeguard_rule_must_cover_every_scenario_and_required_pair() -> None:
    fixture = build_ridgeway_fixture()
    safeguard_rule = next(
        rule
        for rule in fixture.bundle.decision_rules
        if rule.metric == RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION
    )
    covered_scenario_id = safeguard_rule.scenario_ids[0]
    covered_scenario = next(
        item for item in fixture.bundle.scenarios if item.scenario_id == covered_scenario_id
    )
    partially_covered_rule = safeguard_rule.model_copy(
        update={
            "scenario_ids": [covered_scenario_id],
            "pair_ids": [covered_scenario.included_pair_ids[0]],
        }
    )
    bundle = fixture.bundle.model_copy(
        update={
            "decision_rules": [
                partially_covered_rule if rule.rule_id == safeguard_rule.rule_id else rule
                for rule in fixture.bundle.decision_rules
            ]
        }
    )

    findings = validate_professional_bundle(bundle)
    scenario_findings = [
        item for item in findings if item.code == "SAFEGUARD_RULE_SCENARIO_COVERAGE_INCOMPLETE"
    ]
    pair_findings = [
        item for item in findings if item.code == "SAFEGUARD_RULE_PAIR_COVERAGE_INCOMPLETE"
    ]

    assert {item.object_reference for item in scenario_findings} == set(
        safeguard_rule.scenario_ids[1:]
    )
    assert all(item.severity == "BLOCKING" for item in scenario_findings)
    assert len(pair_findings) == 1
    assert pair_findings[0].object_reference == covered_scenario_id
    assert covered_scenario.included_pair_ids[1] in pair_findings[0].remediation


def test_target_scenarios_require_a_required_connectivity_rule() -> None:
    fixture = build_ridgeway_fixture()
    target_metrics = {
        RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT,
        RuleMetric.MAX_RESISTANCE_INCREASE_PERCENT,
    }
    bundle = fixture.bundle.model_copy(
        update={
            "decision_rules": [
                rule for rule in fixture.bundle.decision_rules if rule.metric not in target_metrics
            ]
        }
    )

    findings = validate_professional_bundle(bundle)
    missing = [item for item in findings if item.code == "TARGET_CONNECTIVITY_RULE_MISSING"]

    assert len(missing) == 1
    assert missing[0].severity == "BLOCKING"
    assert missing[0].object_reference == "decision_rules"
    assert "every target scenario" in missing[0].remediation
    assert "every required included focal pair" in missing[0].remediation


def test_required_target_rule_must_cover_every_scenario_and_required_pair() -> None:
    fixture = build_ridgeway_fixture()
    target_rule = next(
        rule
        for rule in fixture.bundle.decision_rules
        if rule.metric == RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT
    )
    target_scenarios = [item for item in fixture.bundle.scenarios if not item.safeguard]
    covered_scenario = target_scenarios[0]
    partially_covered_rule = target_rule.model_copy(
        update={
            "scenario_ids": [covered_scenario.scenario_id],
            "pair_ids": [covered_scenario.included_pair_ids[0]],
        }
    )
    bundle = fixture.bundle.model_copy(
        update={
            "decision_rules": [
                partially_covered_rule if rule.rule_id == target_rule.rule_id else rule
                for rule in fixture.bundle.decision_rules
            ]
        }
    )

    findings = validate_professional_bundle(bundle)
    scenario_findings = [
        item for item in findings if item.code == "TARGET_RULE_SCENARIO_COVERAGE_INCOMPLETE"
    ]
    pair_findings = [
        item for item in findings if item.code == "TARGET_RULE_PAIR_COVERAGE_INCOMPLETE"
    ]

    assert {item.object_reference for item in scenario_findings} == {
        item.scenario_id for item in target_scenarios[1:]
    }
    assert all(item.severity == "BLOCKING" for item in scenario_findings)
    assert len(pair_findings) == 1
    assert pair_findings[0].object_reference == covered_scenario.scenario_id
    assert covered_scenario.included_pair_ids[1] in pair_findings[0].remediation
