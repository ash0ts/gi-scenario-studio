from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core.contracts import (
    PUBLIC_CONTRACTS,
    CloudDisposition,
    ComputationalResourceObservation,
    ConnectivityModelFitness,
    ConnectivityScenario,
    EngineMode,
    RasterSemantics,
    ResourceObservationBoundary,
    ResourceObservationStatus,
    SensitiveDataAuthorization,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.schema import render_public_schemas, write_public_schemas
from pydantic import ValidationError

EXPECTED_PUBLIC_CONTRACTS = (
    "ConnectivityProjectBundle",
    "ConnectivityDecisionBrief",
    "ConnectivityModelFitness",
    "ResistanceHypothesis",
    "FocalAreaSet",
    "ConnectivityScenario",
    "InterventionSite",
    "InterventionOption",
    "ConnectivityCostBasis",
    "ActionEffectRaster",
    "ActionEffectBasis",
    "SafeguardRecord",
    "RiskReviewMask",
    "ConnectivityDecisionRule",
    "ConnectivityRunPlan",
    "DirectConnectivityRun",
    "PortfolioDecisionSummary",
    "ConnectivityEvidenceClaim",
    "GridAlignmentPlan",
    "GridAlignmentApproval",
    "SensitiveDataAuthorization",
    "EngineManifest",
    "ResourcePolicy",
    "ConnectivityExportEligibility",
    "ModelRevision",
    "SemanticModelDiff",
)


def test_public_contracts_emit_deterministic_draft_2020_schemas() -> None:
    assert tuple(contract.__name__ for contract in PUBLIC_CONTRACTS) == EXPECTED_PUBLIC_CONTRACTS
    rendered = render_public_schemas()
    expected = {f"{contract.__name__}.schema.json" for contract in PUBLIC_CONTRACTS} | {
        "index.json"
    }
    assert set(rendered) == expected
    assert '"additionalProperties": false' in rendered["ConnectivityProjectBundle.schema.json"]
    assert (
        '"$schema": "https://json-schema.org/draft/2020-12/schema"'
        in rendered["ConnectivityScenario.schema.json"]
    )
    assert rendered == render_public_schemas()


def test_committed_json_schema_and_zod_outputs_have_no_drift() -> None:
    repository = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "generate_ecocorridor_schemas.py"),
            "--check",
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_schema_writer_removes_only_stale_generator_owned_json(tmp_path: Path) -> None:
    stale = tmp_path / "RemovedContract.schema.json"
    unrelated = tmp_path / "README.txt"
    stale.write_text("{}\n", encoding="utf-8")
    unrelated.write_text("keep\n", encoding="utf-8")

    write_public_schemas(tmp_path)

    assert not stale.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep\n"


def test_generated_schemas_preserve_hash_time_and_engine_fail_closed_rules() -> None:
    schemas = render_public_schemas()
    scenario = json.loads(schemas["ConnectivityScenario.schema.json"])
    scenario_properties = scenario["properties"]
    assert scenario_properties["engine_mode"]["const"] == "RASTER_PAIRWISE"
    assert scenario_properties["raster_semantics"]["const"] == "RESISTANCE"
    manifest = json.loads(schemas["EngineManifest.schema.json"])
    digest = manifest["properties"]["julia_runtime_sha256"]
    assert digest["minLength"] == digest["maxLength"] == 64
    assert digest["pattern"] == "^[0-9a-f]{64}$"
    assert manifest["properties"]["created_at"]["format"] == "date-time"
    typescript = Path("apps/ecocorridor-desktop/src/generated/contracts.ts").read_text(
        encoding="utf-8"
    )
    assert 'z.literal("RASTER_PAIRWISE")' in typescript
    assert 'new RegExp("^[0-9a-f]{64}$")' in typescript
    assert "Expected timezone-aware ISO date-time" in typescript


def test_unsupported_modes_and_conductance_fail_closed() -> None:
    base = {
        "scenario_id": "s1",
        "title": "Unsupported",
        "guild_id": "g1",
        "life_stage": "adult",
        "resistance_hypothesis_id": "h1",
        "focal_set_id": "f1",
        "future_condition": "current",
        "maturity_horizon": "mature",
        "included_pair_ids": ["p1"],
        "grid_id": "grid",
    }
    with pytest.raises(ValidationError, match="RASTER_PAIRWISE"):
        ConnectivityScenario.model_validate({**base, "engine_mode": EngineMode.ONE_TO_ALL})
    with pytest.raises(ValidationError, match="RESISTANCE"):
        ConnectivityScenario.model_validate(
            {**base, "raster_semantics": RasterSemantics.CONDUCTANCE}
        )


def test_generalized_cloud_authorization_requires_a_reviewed_derivative() -> None:
    payload = {
        "asset_id": "sensitive_raster",
        "ownership": "Agency",
        "license_spdx_or_name": "Restricted",
        "sensitivity": "SENSITIVE_LOCATION",
        "cloud_disposition": CloudDisposition.GENERALIZED_DERIVATIVE_ONLY,
        "export_allowed": True,
        "retention_policy": "Delete after project",
        "spatial_redaction": "Generalize to 10 km",
        "approved_deployments": ["DEDICATED"],
        "reviewer": "Data steward",
        "reviewed_at": datetime(2026, 8, 3, tzinfo=UTC),
    }
    with pytest.raises(ValidationError, match="generalized_asset_id"):
        SensitiveDataAuthorization.model_validate(payload)


def test_fitness_passport_requires_every_independent_dimension() -> None:
    fitness = build_ridgeway_fixture().bundle.fitness.model_dump(mode="python")
    fitness["dimensions"] = fitness["dimensions"][:-1]
    with pytest.raises(ValidationError, match="requires every dimension"):
        ConnectivityModelFitness.model_validate(fitness)


def test_audit_timestamps_require_an_explicit_timezone() -> None:
    payload = build_ridgeway_fixture().bundle.fitness.model_dump(mode="python")
    payload["reviewed_at"] = "2026-08-03T00:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        ConnectivityModelFitness.model_validate(payload)


def test_ridgeway_bundle_cross_references_validate() -> None:
    fixture = build_ridgeway_fixture()
    assert fixture.bundle.schema_version == "1.0"
    assert len(fixture.bundle.resistance_hypotheses) == 4
    assert len(fixture.bundle.focal_area_sets) == 3
    assert len(fixture.bundle.scenarios) == 12
    assert all(
        len(option.effects) == 12
        for option in fixture.bundle.intervention_options
        if option.kind.value not in {"NO_ACTION", "SURVEY_FIRST"}
    )


def test_contracts_reject_unknown_fields() -> None:
    scenario = build_ridgeway_fixture().bundle.scenarios[0].model_dump(mode="python")
    scenario["silent_new_setting"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        ConnectivityScenario.model_validate(scenario)


def test_resource_observation_never_turns_missing_energy_or_memory_into_zero() -> None:
    observation = ComputationalResourceObservation(
        observation_id="resource:direct-1",
        boundary=ResourceObservationBoundary.DIRECT_SOLVER,
        solver_wall_seconds=1.25,
        peak_memory_status=ResourceObservationStatus.NOT_MEASURED,
        peak_memory_method="NOT_COLLECTED_NO_PER_PROCESS_SAMPLER",
        energy_status=ResourceObservationStatus.NOT_MEASURED,
        energy_method="NOT_COLLECTED_NO_ENERGY_METER",
        hardware="os=test;arch=test;logical_cpus=2",
        boundary_description="One isolated direct solver process.",
    )
    assert observation.peak_memory_bytes is None
    assert observation.energy_kwh is None

    with pytest.raises(ValidationError, match="unmeasured energy"):
        ComputationalResourceObservation.model_validate(
            {**observation.model_dump(mode="python"), "energy_kwh": 0.0}
        )
    with pytest.raises(ValidationError, match="boundary-specific wall time"):
        ComputationalResourceObservation.model_validate(
            {
                **observation.model_dump(mode="python"),
                "boundary": ResourceObservationBoundary.SEARCH_PROPOSER,
            }
        )
