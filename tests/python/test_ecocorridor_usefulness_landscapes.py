from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from ecocorridor_core.claim_safety import assert_safe_rendered_ecological_copy
from ecocorridor_core.grid import grid_identity
from ecocorridor_core.usefulness_landscapes import (
    ALL_CASE_CONDITIONS,
    ALL_FAMILIES,
    DIRECT_RESULT_CONDITIONS,
    CaseTruthCondition,
    UsefulnessLandscape,
    build_usefulness_catalog,
    landscape_definition_sha256,
    materialize_usefulness_landscapes,
    usefulness_program_sha256,
    usefulness_public_manifest,
    usefulness_run_plan_rows,
    usefulness_run_plan_sha256,
)

ROOT = Path(__file__).resolve().parents[2]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ascii_nodata_topology(path: Path) -> tuple[tuple[bool, ...], ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    nodata = float(lines[5].split()[1])
    return tuple(
        tuple(float(value) == nodata for value in line.split()) for line in lines[6:]
    )


def test_catalog_has_five_distinct_cc0_structures_and_no_outcomes() -> None:
    catalog = build_usefulness_catalog()

    assert tuple(landscape.family for landscape in catalog.landscapes) == ALL_FAMILIES
    assert catalog.direct_job_count == 160
    assert catalog.evidence_state == "DIRECT_EVIDENCE_PENDING"
    assert catalog.release_eligible is False
    assert all(landscape.reportable_outcomes is None for landscape in catalog.landscapes)
    assert all(
        assumption.authorship == "SELF_AUTHORED_CC0_SYNTHETIC"
        and assumption.adjudication_status == "PENDING_INDEPENDENT_ADJUDICATION"
        for landscape in catalog.landscapes
        for assumption in landscape.assumptions
    )
    assert len({landscape_definition_sha256(item) for item in catalog.landscapes}) == 5
    assert len({usefulness_program_sha256(catalog), usefulness_run_plan_sha256(catalog)}) == 2


def test_required_case_truth_is_complete_and_outcome_truth_stays_pending() -> None:
    catalog = build_usefulness_catalog()
    truths = [truth for landscape in catalog.landscapes for truth in landscape.case_truths]

    assert {truth.kind for truth in truths} == ALL_CASE_CONDITIONS
    for truth in truths:
        if truth.kind in DIRECT_RESULT_CONDITIONS:
            assert truth.truth_basis == "DIRECT_CIRCUITSCAPE_REQUIRED"
            assert truth.truth_status == "PENDING_DIRECT_CIRCUITSCAPE"
            assert truth.expected_gate == "PENDING"
        else:
            assert truth.truth_basis == "DETERMINISTIC_INPUT_POLICY"

    with pytest.raises(ValueError, match="cannot be pre-authored"):
        CaseTruthCondition(
            condition_id="forbidden_pre_authored_result",
            kind="ROAD_MASK_ADVISORY",
            truth_basis="DIRECT_CIRCUITSCAPE_REQUIRED",
            truth_status="READY_FOR_POLICY_ASSERTION",
            expected_gate="PENDING",
            subject_id="portfolio_x",
            assertion="A current-raster condition has already been observed.",
        )


def test_every_landscape_preserves_grid_and_nodata_identity() -> None:
    for landscape in build_usefulness_catalog().landscapes:
        rasters = tuple(landscape.rasters.values())
        assert len({grid_identity(raster.grid) for raster in rasters}) == 1
        first = tuple(
            tuple(value is None or value == rasters[0].grid.nodata for value in row)
            for row in rasters[0].values
        )
        for raster in rasters[1:]:
            topology = tuple(
                tuple(value is None or value == raster.grid.nodata for value in row)
                for row in raster.values
            )
            assert topology == first

        payload = landscape.model_dump(mode="json")
        first_asset_id = next(iter(payload["rasters"]))
        payload["rasters"][first_asset_id]["grid"]["transform"][2] += 1
        with pytest.raises(ValueError, match="exactly aligned"):
            UsefulnessLandscape.model_validate(payload)


def test_portfolio_universes_and_scenario_ensembles_are_complete() -> None:
    catalog = build_usefulness_catalog()
    rows = usefulness_run_plan_rows(catalog)
    by_family: dict[str, list] = defaultdict(list)
    for row in rows:
        by_family[row.family].append(row)
        assert row.execution_state == "PENDING_DIRECT_CIRCUITSCAPE"
        assert row.reportable_values is None

    assert len(rows) == len({row.direct_run_id for row in rows}) == 160
    for landscape in catalog.landscapes:
        assert len(landscape.action_sites) == 3
        assert len(landscape.portfolios) == 8
        assert len(landscape.scenarios) == 4
        assert landscape.direct_job_count == 32
        assert Counter(row.scenario_id for row in by_family[landscape.family]) == {
            scenario.scenario_id: 8 for scenario in landscape.scenarios
        }
        assert Counter(row.portfolio_id for row in by_family[landscape.family]) == {
            portfolio.portfolio_id: 4 for portfolio in landscape.portfolios
        }
        assert any(row.safeguard for row in by_family[landscape.family])


def test_public_manifest_is_checksum_bound_safe_and_result_free() -> None:
    catalog = build_usefulness_catalog()
    manifest = usefulness_public_manifest(catalog)
    static = json.loads(
        (ROOT / "benchmarks/ecocorridor/usefulness/manifest.json").read_text(encoding="utf-8")
    )

    assert manifest == static
    asserted_sha = manifest.pop("manifest_sha256")
    assert asserted_sha == hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert static["truth_status"] == "PENDING_DIRECT_CIRCUITSCAPE"
    assert static["authorship"] == "SELF_AUTHORED_CC0_SYNTHETIC"
    assert static["adjudication_status"] == "PENDING_INDEPENDENT_ADJUDICATION"
    assert static["release_eligible"] is False
    assert "pair_metrics" not in static
    assert "effective_resistance" not in static
    assert "current_density" not in static
    assert_safe_rendered_ecological_copy(static)


def test_materializer_writes_executable_requests_but_no_result_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "usefulness"
    catalog = build_usefulness_catalog()
    planned = {row.direct_run_id: row for row in usefulness_run_plan_rows(catalog)}
    manifest = materialize_usefulness_landscapes(output, catalog=catalog)

    assert manifest.direct_job_count == len(planned) == 160
    assert manifest.direct_evidence_state == "PENDING_DIRECT_CIRCUITSCAPE"
    assert manifest.result_file_count == 0
    assert manifest.program_sha256 == usefulness_program_sha256(catalog)
    assert manifest.run_plan_sha256 == usefulness_run_plan_sha256(catalog)
    assert manifest.files_sha256 == hashlib.sha256(
        json.dumps(
            [item.model_dump(mode="json") for item in manifest.files],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    for record in manifest.files:
        path = output / record.relative_path
        assert path.is_file() and not path.is_symlink()
        assert path.stat().st_size == record.byte_size
        assert _digest(path) == record.sha256

    request_paths = sorted(output.glob("*/runs/*/request.json"))
    assert len(request_paths) == 160
    assert not list(output.glob("*/runs/*/result.json"))
    assert not list(output.glob("*/runs/*/outputs"))
    for request_path in request_paths:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        row = planned[request["prepared_run_id"]]
        assert _digest(request_path) == row.request_sha256
        assert request["mode"] == "raster_pairwise"
        assert request["raster_semantics"] == "resistance"
        assert request["settings"] == {
            "blas_threads": 1,
            "emit_current_map": True,
            "julia_threads": 1,
            "neighbor_rule": "EIGHT",
            "omp_threads": 1,
            "precision": "double",
            "resistance_average": "ARITHMETIC",
        }
        run_root = request_path.parent
        for relative in request["resources"].values():
            assert (run_root / relative).is_file()
        assert _ascii_nodata_topology(run_root / "inputs/resistance.asc") == (
            _ascii_nodata_topology(run_root / "inputs/focal.asc")
        )
        assert _ascii_nodata_topology(run_root / "inputs/resistance.asc") == (
            _ascii_nodata_topology(run_root / "inputs/review-mask.asc")
        )

    with pytest.raises(FileExistsError, match="absent or empty"):
        materialize_usefulness_landscapes(output, catalog=catalog)


def test_materialized_program_manifest_does_not_claim_execution(tmp_path: Path) -> None:
    output = tmp_path / "study"
    manifest = materialize_usefulness_landscapes(output)
    stored = json.loads(
        (output / "materialization-manifest.json").read_text(encoding="utf-8")
    )

    assert stored == manifest.model_dump(mode="json")
    assert stored["execution_state"] == "INPUTS_MATERIALIZED"
    assert stored["direct_evidence_state"] == "PENDING_DIRECT_CIRCUITSCAPE"
    assert stored["result_file_count"] == 0
    assert stored["release_eligible"] is False
