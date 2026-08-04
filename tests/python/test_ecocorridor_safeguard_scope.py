from __future__ import annotations

from ecocorridor_core.contracts import ConnectivityProjectBundle
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
