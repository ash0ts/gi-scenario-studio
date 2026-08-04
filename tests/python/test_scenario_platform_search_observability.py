from __future__ import annotations

import pytest
from scenario_platform.observability import AppProfile, redact_sensitive
from scenario_platform.search import categorical_qmc, nondominated_indices


def test_categorical_qmc_is_deterministic_distinct_and_constraint_aware() -> None:
    space = [["none", "crossing"], ["none", "restore", "survey"]]

    def repair(portfolio: tuple[str, ...]) -> tuple[str, ...] | None:
        return None if portfolio == ("crossing", "restore") else portfolio

    first, first_engine = categorical_qmc(space, 5, 42, repair=repair)
    second, second_engine = categorical_qmc(space, 5, 42, repair=repair)
    assert first == second
    assert first_engine == second_engine
    assert len(first) == len(set(first)) == 5
    assert ("crossing", "restore") not in first


def test_nondominated_frontier_preserves_stable_row_indices() -> None:
    rows = [
        {"cost": 10.0, "benefit": 2.0},
        {"cost": 12.0, "benefit": 4.0},
        {"cost": 13.0, "benefit": 3.0},
        {"cost": 10.0, "benefit": 2.0},
    ]
    assert nondominated_indices(rows, {"cost": "minimize", "benefit": "maximize"}) == [0, 1, 3]
    with pytest.raises(ValueError, match="missing metrics"):
        nondominated_indices([{"cost": 1.0}], {"cost": "minimize", "benefit": "maximize"})


def test_app_profile_names_are_scoped_and_payloads_are_redacted() -> None:
    profile = AppProfile(
        namespace="ecocorridor",
        api_version="1.0",
        schema_version="1.0",
        run_kinds=["analysis", "guided_session"],
        artifact_types=["direct_run", "evidence_package"],
        registry_names={"fixtures": "Datasets / eco-guided-fixture"},
        keychain_service="org.ecocorridor.scenariostudio",
        app_data_root="EcoCorridorScenarioStudio",
    )
    assert profile.run_id("analysis", "analysis-1").startswith("ecocorridor-analysis-")
    assert profile.table_name("direct_jobs") == "ecocorridor_direct_jobs"
    assert profile.artifact_name("direct_run", "job-1").startswith("ecocorridor-direct_run-")
    assert profile.registry_name("fixtures") == "Datasets / eco-guided-fixture"
    with pytest.raises(ValueError, match="unsupported run kind"):
        profile.run_id("swmm", "one")

    redacted = redact_sensitive(
        {
            "api_key": "secret-value",
            "nested": {"Authorization": "Bearer value"},
            "provider_credential_id": "credential-17",
            "message": "loaded /Users/person/private/grid.tif",
            "safe": "scenario-17",
        }
    )
    assert redacted == {
        "api_key": "[REDACTED]",
        "nested": {"Authorization": "[REDACTED]"},
        "provider_credential_id": "[REDACTED]",
        "message": "loaded [LOCAL_PATH]",
        "safe": "scenario-17",
    }
