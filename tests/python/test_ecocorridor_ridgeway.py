from __future__ import annotations

import hashlib
import json
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core import direct_study
from ecocorridor_core.contracts import (
    DirectRunState,
    PairResistanceMetric,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    QAStatus,
    RuleDisposition,
    canonical_sha256,
)
from ecocorridor_core.engine import effective_circuitscape_configuration_text
from ecocorridor_core.fixture_pipeline import (
    materialize_ridgeway_ascii,
    render_esri_ascii,
)
from ecocorridor_core.ridgeway import (
    RIDGEWAY_NOTICE,
    build_public_ui_projection,
    build_ridgeway_fixture,
    select_teaching_portfolios,
    validate_ridgeway_direct_evidence,
)


def _summary(
    portfolio_id: str,
    *,
    disposition: PortfolioDisposition = PortfolioDisposition.INCOMPLETE,
    cost: float = 0,
    reductions: list[float] | None = None,
    failed_rules: set[str] | None = None,
    road_flag: bool = False,
    worst: float | None = None,
) -> PortfolioDecisionSummary:
    failed_rules = failed_rules or set()
    rules = [
        RuleDisposition(
            rule_id="rule_target_improvement",
            scenario_id=f"scenario_{index}",
            pair_id=f"pair_{index}",
            observed_value=value,
            passed=("rule_target_improvement" not in failed_rules and value >= 3),
            explanation="Synthetic selection test only.",
        )
        for index, value in enumerate(reductions or [])
    ]
    if "rule_cost" in failed_rules or disposition == PortfolioDisposition.PASSES:
        rules.append(
            RuleDisposition(
                rule_id="rule_cost",
                observed_value=cost,
                passed="rule_cost" not in failed_rules,
                explanation="Synthetic selection test only.",
            )
        )
    if road_flag:
        rules.append(
            RuleDisposition(
                rule_id="rule_review_mask",
                observed_value=20,
                passed=False,
                explanation="Diagnostic selection test only.",
            )
        )
    return PortfolioDecisionSummary(
        portfolio_id=portfolio_id,
        disposition=disposition,
        cost=cost,
        currency="USD",
        rule_dispositions=rules,
        worst_resistance_reduction_percent=worst,
        review_mask_triggered=road_flag,
        next_action="Inspect direct evidence and feasibility.",
    )


def test_ridgeway_is_deterministic_input_fixture_without_fabricated_outcomes() -> None:
    left = build_ridgeway_fixture()
    right = build_ridgeway_fixture()
    assert left.model_dump(mode="json") == right.model_dump(mode="json")
    assert left.notice == RIDGEWAY_NOTICE
    assert left.direct_matrix_job_count == 128 * 12
    assert left.teaching_map_job_count == 5 * 12
    assert len({item.portfolio_id for item in left.portfolios}) == 128
    assert validate_ridgeway_direct_evidence(left, []) is False
    projection = build_public_ui_projection(left)
    assert projection["schemaVersion"] == "ecocorridor-replay/v1.1"
    assert projection["evidenceState"] == "ENGINE_RUN_REQUIRED"
    assert projection["releaseReplayEligible"] is False
    assert projection["baselinePortfolioId"] is None
    assert projection["manifestSha256"] is None
    assert all(item["id"] is None for item in projection["portfolios"])
    assert all(item["scenarioResults"] == [] for item in projection["portfolios"])
    assert "not passed the v1.1 evidence contract" in projection["releaseBlocker"]


def test_release_evidence_producer_blocks_windows_without_job_object_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(direct_study.os, "name", "nt")
    with pytest.raises(direct_study.DirectStudyError, match="native Job Object"):
        direct_study.run_direct_study(
            output_directory=tmp_path / "evidence",
            julia_executable=tmp_path / "julia.exe",
            julia_project=tmp_path / "project",
            julia_depot=tmp_path / "depot",
        )


def test_direct_study_workflow_manifest_binds_scientific_python_sources() -> None:
    sources = direct_study._workflow_python_sources()

    assert {
        "footprint_authorization",
        "current_raster_parser",
        "metric_definitions",
        "intervention_mutator",
        "model_run_cache_identity",
        "python_protocol_parser",
    }.issubset(sources)
    assert all(path.is_file() for path in sources.values())
    component_hashes = {label: direct_study.sha256_file(path) for label, path in sources.items()}
    original_workflow_sha256 = canonical_sha256(component_hashes)
    changed_cache_contract = {
        **component_hashes,
        "model_run_cache_identity": "f" * 64,
    }
    assert canonical_sha256(changed_cache_contract) != original_workflow_sha256


def test_direct_study_resolves_all_runtime_paths_before_isolated_job_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "runtime" / "julia"
    executable.parent.mkdir()
    executable.write_bytes(b"julia")
    project = tmp_path / "engine-project"
    project.mkdir()
    depot = tmp_path / "julia-depot"
    depot.mkdir()
    monkeypatch.chdir(tmp_path)

    manifest = type("Manifest", (), {"sha256": "a" * 64})()

    def snapshot(evidence_root: Path, source_project: Path) -> Path:
        assert evidence_root.is_absolute()
        assert source_project.is_absolute()
        target = evidence_root / "engine-snapshot"
        target.mkdir(parents=True)
        return target

    def execute_phase_jobs(**kwargs: object) -> tuple[list[object], int]:
        assert Path(str(kwargs["evidence_root"])).is_absolute()
        assert Path(str(kwargs["sources"])).is_absolute()
        assert Path(str(kwargs["julia_executable"])).is_absolute()
        assert Path(str(kwargs["julia_project"])).is_absolute()
        assert Path(str(kwargs["julia_depot"])).is_absolute()
        return [], 0

    monkeypatch.setattr(direct_study, "_ensure_engine_snapshot", snapshot)
    monkeypatch.setattr(
        direct_study,
        "_validate_engine_environment",
        lambda *_args: (manifest, "b" * 64, "c" * 64, {}),
    )
    monkeypatch.setattr(direct_study, "_ensure_engine_manifest_record", lambda *_args: {})
    monkeypatch.setattr(direct_study, "_ensure_workflow_snapshot", lambda *_args: {})
    monkeypatch.setattr(
        direct_study,
        "_ensure_sources",
        lambda root, _fixture: root / "sources",
    )
    monkeypatch.setattr(direct_study, "_execute_phase_jobs", execute_phase_jobs)

    result = direct_study.run_direct_study(
        output_directory=Path("relative-evidence"),
        julia_executable=Path("runtime/julia"),
        julia_project=Path("engine-project"),
        julia_depot=Path("julia-depot"),
        job_limit=0,
    )

    assert result.state == "INCOMPLETE"


def test_committed_ridgeway_projection_and_data_manifest_are_hash_locked() -> None:
    fixture = build_ridgeway_fixture()
    repository_root = Path(__file__).resolve().parents[2]
    root = repository_root / "benchmarks" / "ecocorridor"
    manifest = json.loads((root / "ridgeway" / "manifest.json").read_text(encoding="utf-8"))
    projection = json.loads((root / "ridgeway" / "ui_projection.json").read_text(encoding="utf-8"))
    assert manifest["model_sha256"] == fixture.bundle.revision.model_sha256
    assert manifest["portfolio_ids_sha256"] == canonical_sha256(
        [item.portfolio_id for item in fixture.portfolios]
    )
    assert manifest["scenario_ids_sha256"] == canonical_sha256(
        [item.scenario_id for item in fixture.bundle.scenarios]
    )

    direct_manifest_path = root / "ridgeway" / "direct-evidence-manifest.json"
    direct_manifest = json.loads(direct_manifest_path.read_text(encoding="utf-8"))
    assert direct_manifest["release_replay_eligible"] is True
    assert direct_manifest["schema_version"] == "ridgeway-direct-evidence/v1.2"
    assert direct_manifest["matrix_job_count"] == 1536
    assert direct_manifest["map_job_count"] == 60
    assert direct_manifest["model_sha256"] == fixture.bundle.revision.model_sha256
    assert hashlib.sha256(direct_manifest_path.read_bytes()).hexdigest() == (
        "abf53dd7fa03956e8d076286e30d1ef3e5b35abf6a06952390b5366fdfef3c1e"
    )
    assert direct_manifest["cost_basis_sha256"] == fixture.cost_basis_sha256
    assert direct_manifest["assumption_ledger_sha256"] == fixture.assumption_ledger_sha256
    bundle = direct_manifest["evidence_bundle"]
    assert bundle["filename"] == "ridgeway-direct-evidence-a0dc6b763347.tar.gz"
    assert bundle["sha256"] == ("a0dc6b7633475824d36cd9ab18ee38088208561ba680fed022fc7b6d65ac7168")
    assert projection["schemaVersion"] == "ecocorridor-replay/v1.2"
    assert projection["evidenceState"] == "VERIFIED_DIRECT_EVIDENCE"
    assert projection["releaseReplayEligible"] is True
    assert projection["manifestSha256"] == (
        "abf53dd7fa03956e8d076286e30d1ef3e5b35abf6a06952390b5366fdfef3c1e"
    )
    assert projection["baselinePortfolioId"] == "portfolio_7c18e581b57ca9363bb7"
    assert projection["study"]["portfolioCount"] == 128
    assert projection["study"]["scenarioCount"] == 12
    assert projection["study"]["matrixJobCount"] == 1536
    assert projection["study"]["mapJobCount"] == 60
    assert {item["role"]: item["id"] for item in projection["portfolios"]} == {
        "caution": "portfolio_cef73d6212fedba4dcc6",
        "carry-forward": "portfolio_d3aa8b1e170ffd76b322",
        "over-budget": "portfolio_9b7cb6c531a53fcaffa6",
        "brittle": "portfolio_3ae12008aed85b1fc1a6",
    }
    for portfolio in projection["portfolios"]:
        assert portfolio["id"].startswith("portfolio_")
        assert portfolio["costUsd"] >= 0
        assert portfolio["passCount"] >= 0
        assert portfolio["requiredCount"] == 12
        assert portfolio["worstResistanceChangePct"] is not None
        assert portfolio["roadMaskFlag"] is True
        assert portfolio["hypothesisRanks"]
        assert len(portfolio["scenarioResults"]) == 12
        assert sum(len(scenario["pairResults"]) for scenario in portfolio["scenarioResults"]) == 24
    assert len(projection["currentRasters"]) == 60

    desktop_projection = (
        repository_root / "apps" / "ecocorridor-desktop" / "src" / "data" / "ridgewayFixture.json"
    )
    assert (
        desktop_projection.read_bytes() == (root / "ridgeway" / "ui_projection.json").read_bytes()
    )
    trust = json.loads(
        desktop_projection.with_name("ridgewayReleaseTrust.json").read_text(encoding="utf-8")
    )
    assert trust["trusted"] is True
    assert trust["evidenceManifestSha256"] == projection["manifestSha256"]

    bundle = direct_manifest["evidence_bundle"]
    archive_path = root / "ridgeway" / bundle["filename"]
    assert archive_path.stat().st_size == bundle["byte_size"]
    assert archive_path.stat().st_size < 100_000_000
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == bundle["sha256"]
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        assert len(members) == bundle["file_count"]
        assert all(item.isfile() and not item.issym() and not item.islnk() for item in members)
        names = [item.name for item in members]
        assert len(names) == len(set(names))
        index_payload = archive.extractfile(bundle["internal_index_path"])
        assert index_payload is not None
        index_bytes = index_payload.read()
        assert hashlib.sha256(index_bytes).hexdigest() == bundle["internal_index_sha256"]
        index = json.loads(index_bytes)
        assert index["matrix_job_count"] == 1536
        assert index["map_job_count"] == 60
        assert {"bundle-index.json"} | {item["path"] for item in index["files"]} == set(names)
        for record in index["files"]:
            stream = archive.extractfile(record["path"])
            assert stream is not None
            payload = stream.read()
            assert len(payload) == record["byte_size"]
            assert hashlib.sha256(payload).hexdigest() == record["sha256"]

    data_manifest = json.loads((root / "data-manifest.json").read_text(encoding="utf-8"))
    for record in data_manifest["files"]:
        path = repository_root / record["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        assert record["synthetic"] is True
        assert record["license"] == "CC0-1.0"
        assert record["sensitivity"] == "PUBLIC"


def test_teaching_roles_are_selected_from_observed_evidence_not_fixed_ids() -> None:
    fixture = build_ridgeway_fixture()
    summaries = [_summary(item.portfolio_id) for item in fixture.portfolios]
    caution_id = fixture.portfolios[1].portfolio_id
    carry_id = fixture.portfolios[2].portfolio_id
    over_id = fixture.portfolios[3].portfolio_id
    collapse_id = fixture.portfolios[4].portfolio_id
    summaries[1] = _summary(
        caution_id,
        disposition=PortfolioDisposition.FAILS,
        cost=50_000,
        reductions=[4, 5],
        failed_rules={"rule_review_mask"},
        road_flag=True,
        worst=4,
    )
    summaries[2] = _summary(
        carry_id,
        disposition=PortfolioDisposition.PASSES,
        cost=300_000,
        reductions=[6, 5],
        worst=5,
    )
    summaries[3] = _summary(
        over_id,
        disposition=PortfolioDisposition.FAILS,
        cost=600_000,
        reductions=[8, 7],
        failed_rules={"rule_cost"},
        worst=7,
    )
    summaries[4] = _summary(
        collapse_id,
        disposition=PortfolioDisposition.FAILS,
        cost=200_000,
        reductions=[12, -4],
        failed_rules={"rule_target_improvement"},
        worst=-4,
    )
    selection = select_teaching_portfolios(fixture, summaries, test_only=True)
    assert selection.low_cost_caution_portfolio_id == caution_id
    assert selection.carry_forward_portfolio_id == carry_id
    assert selection.over_budget_portfolio_id == over_id
    assert selection.hypothesis_collapse_portfolio_id == collapse_id
    assert selection.map_rerun_portfolio_ids[0] == fixture.baseline_portfolio_id
    assert selection.release_eligible is False
    assert selection.evidence_mode == "TEST_ONLY"


def test_ridgeway_esri_ascii_materialization_is_deterministic_and_has_no_results(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = materialize_ridgeway_ascii(first_root)
    second = materialize_ridgeway_ascii(second_root)
    assert first == second
    assert first.execution_state == "INPUTS_MATERIALIZED"
    assert first.release_replay_eligible is False
    assert len(first.files) == 48
    for record in first.files:
        assert (first_root / record.relative_path).read_bytes() == (
            second_root / record.relative_path
        ).read_bytes()
    resistance = (first_root / "resistance" / "h1_low_barrier.asc").read_text(encoding="utf-8")
    assert resistance.startswith(
        "ncols 20\nnrows 12\nxllcorner 0\nyllcorner 0\ncellsize 25\nNODATA_value -9999\n"
    )
    study_plan = json.loads((first_root / "study-plan.json").read_text(encoding="utf-8"))
    assert study_plan["direct_matrix_job_count"] == 1536
    assert "results" not in study_plan
    pair_source = next((first_root / "pairs").glob("*.tsv"))
    assert pair_source.read_text(encoding="utf-8").splitlines()[0] == "mode\tinclude"


def test_guided_map_reducer_validates_grid_nodata_and_current_values(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    mask = fixture.rasters[fixture.bundle.review_masks[0].asset_id]
    active_cells = [
        (row, column)
        for row in range(mask.grid.height)
        for column in range(mask.grid.width)
        if mask.values[row][column] == 1
    ]
    assert len(active_cells) > 1

    def write_current(path: Path, *, value: float, nodata_cell: tuple[int, int] | None = None):
        values = [[value for _ in range(mask.grid.width)] for _ in range(mask.grid.height)]
        if nodata_cell is not None:
            row, column = nodata_cell
            values[row][column] = mask.grid.nodata
        current = mask.model_copy(
            update={
                "asset_id": path.stem,
                "values": tuple(tuple(row) for row in values),
            }
        )
        path.write_text(render_esri_ascii(current), encoding="utf-8")

    baseline_path = tmp_path / "baseline.asc"
    portfolio_path = tmp_path / "portfolio.asc"
    write_current(baseline_path, value=1)
    write_current(portfolio_path, value=2, nodata_cell=active_cells[0])
    with pytest.raises(direct_study.DirectStudyError, match="different NODATA topology"):
        direct_study._review_mask_current_totals(
            baseline_path=baseline_path,
            portfolio_path=portfolio_path,
            mask=mask,
        )
    write_current(baseline_path, value=1, nodata_cell=active_cells[0])
    baseline_total, portfolio_total = direct_study._review_mask_current_totals(
        baseline_path=baseline_path,
        portfolio_path=portfolio_path,
        mask=mask,
    )
    assert baseline_total == len(active_cells) - 1
    assert portfolio_total == 2 * (len(active_cells) - 1)

    misaligned = tmp_path / "misaligned.asc"
    misaligned.write_text(
        baseline_path.read_text(encoding="utf-8").replace("cellsize 25", "cellsize 30"),
        encoding="utf-8",
    )
    with pytest.raises(direct_study.DirectStudyError, match="grid or data failed validation"):
        direct_study._review_mask_current_totals(
            baseline_path=misaligned,
            portfolio_path=portfolio_path,
            mask=mask,
        )

    write_current(baseline_path, value=1)
    write_current(portfolio_path, value=2)
    lines = portfolio_path.read_text(encoding="utf-8").splitlines()
    row, column = active_cells[0]
    data = [line.split() for line in lines[6:]]
    data[row][column] = "-1"
    portfolio_path.write_text(
        "\n".join(lines[:6] + [" ".join(item) for item in data]) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(direct_study.DirectStudyError, match="nonfinite or negative"):
        direct_study._review_mask_current_totals(
            baseline_path=baseline_path,
            portfolio_path=portfolio_path,
            mask=mask,
        )


def test_direct_study_attempt_directories_are_collision_safe(tmp_path: Path) -> None:
    job_root = tmp_path / "job"
    with ThreadPoolExecutor(max_workers=8) as executor:
        attempts = list(executor.map(lambda _: direct_study._attempt_directory(job_root), range(8)))
    assert len({item.name for item in attempts}) == 8
    assert all((item / "inputs").is_dir() and (item / "outputs").is_dir() for item in attempts)


def test_direct_study_cache_requires_current_physics_and_semantic_identity(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    job_root = evidence_root / "jobs" / "matrix" / "portfolio-a" / "scenario-a"
    attempt = direct_study._attempt_directory(job_root)
    fixture = build_ridgeway_fixture()
    resistance_payload = render_esri_ascii(next(iter(fixture.rasters.values()))).encode()
    request_payload = b'{"protocol_version":"1.0"}\n'
    semantic_diff_payload = b'{"schema_version":"test-diff"}\n'
    footprint_payload = b'{"schema_version":"test-proof"}\n'
    focal_payload = b"reviewed focal bytes\n"
    pair_payload = b"1\t2\n"
    matrix_payload = b"1 2 1.0\n"
    result_payload = (
        json.dumps(
            {
                "protocol_version": "1.0",
                "status": "SUCCEEDED",
                "engine_name": "Circuitscape",
                "circuitscape_version": "5.17.1",
                "pair_metrics": [
                    {
                        "pair_id": "pair-a",
                        "from_focal_id": 1,
                        "to_focal_id": 2,
                        "effective_resistance": 1.0,
                    }
                ],
                "matrix_path": "outputs/connectivity_resistances_3columns.out",
                "current_map_path": None,
                "review_mask_summaries": [],
                "warnings": [],
                "failure_message": None,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    artifacts = {
        "request.json": request_payload,
        "result.json": result_payload,
        "stdout.log": b"",
        "stderr.log": b"",
        "action-raster-diff.json": semantic_diff_payload,
        "footprint-proofs.json": footprint_payload,
        "circuitscape.ini": effective_circuitscape_configuration_text(
            resource_paths={
                "resistance": "inputs/resistance.asc",
                "focal": "inputs/focal.asc",
                "included_pairs": "inputs/pairs.tsv",
            },
            output_directory="outputs",
            neighbor_rule="EIGHT",
            resistance_average="ARITHMETIC",
            emit_current_map=False,
        ).encode(),
        "inputs/resistance.asc": resistance_payload,
        "inputs/focal.asc": focal_payload,
        "inputs/pairs.tsv": pair_payload,
        "outputs/connectivity_resistances_3columns.out": matrix_payload,
    }
    for relative, value in artifacts.items():
        path = attempt / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    artifact_directory = attempt.relative_to(evidence_root).as_posix()
    digest = lambda value: hashlib.sha256(value).hexdigest()
    expected = direct_study.ExpectedFixtureJobIdentity(
        direct_run_id="matrix_portfolio-a_scenario-a",
        revision_id="revision-a",
        portfolio_id="portfolio-a",
        scenario_id="scenario-a",
        phase="matrix",
        engine_manifest_sha256="1" * 64,
        engine_environment_sha256="2" * 64,
        workflow_manifest_sha256="3" * 64,
        physics_model_sha256="4" * 64,
        decision_snapshot_sha256="5" * 64,
        model_run_cache_key="6" * 64,
        request_sha256=digest(request_payload),
        action_raster_sha256=digest(semantic_diff_payload),
        prepared_resistance_sha256=digest(resistance_payload),
        footprint_proof_sha256="5" * 64,
        footprint_proof_document_sha256=digest(footprint_payload),
        pair_ids=frozenset({"pair-a"}),
    )
    job = direct_study.ExactFixtureJob(
        direct_run_id=expected.direct_run_id,
        analysis_id="analysis-a",
        revision_id=expected.revision_id,
        portfolio_id=expected.portfolio_id,
        scenario_id=expected.scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256=expected.engine_manifest_sha256,
        engine_environment_sha256=expected.engine_environment_sha256,
        workflow_manifest_sha256=expected.workflow_manifest_sha256,
        physics_model_sha256=expected.physics_model_sha256,
        decision_snapshot_sha256=expected.decision_snapshot_sha256,
        model_run_cache_key=expected.model_run_cache_key,
        input_sha256=expected.request_sha256,
        result_sha256=digest(result_payload),
        matrix_sha256=digest(matrix_payload),
        pair_metrics=[PairResistanceMetric(pair_id="pair-a", effective_resistance=1)],
        qa_status=QAStatus.ACCEPTED,
        completed_at=datetime(2026, 8, 3, tzinfo=UTC),
        phase="matrix",
        request_sha256=expected.request_sha256,
        action_raster_sha256=expected.action_raster_sha256,
        prepared_resistance_sha256=expected.prepared_resistance_sha256,
        footprint_proof_sha256=expected.footprint_proof_sha256,
        footprint_proof_document_sha256=expected.footprint_proof_document_sha256,
        artifact_directory=artifact_directory,
        process_exit_evidence=direct_study.SuccessfulProcessTreeExit(
            process_id=41001,
            process_group_id=41001,
            process_identity="a" * 64,
            launched_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
            termination_reason="natural_exit",
            tree_exit_confirmed=True,
            confirmation_method="POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT",
            confirmed_at=datetime(2026, 8, 3, 12, 0, 1, tzinfo=UTC),
        ),
    )
    direct_study._json(
        job_root / "complete.json",
        {
            "job": job.model_dump(mode="json"),
            "artifact_hashes": {
                (attempt / relative).relative_to(evidence_root).as_posix(): digest(value)
                for relative, value in artifacts.items()
            },
        },
    )

    cache_kwargs = {
        "request_payload": request_payload,
        "resistance_payload": resistance_payload,
        "semantic_diff_payload": semantic_diff_payload,
        "footprint_document_payload": footprint_payload,
        "focal_source": attempt / "inputs/focal.asc",
        "pair_source": attempt / "inputs/pairs.tsv",
        "prepared_pairs": [
            direct_study.PreparedPair(pair_id="pair-a", from_focal_id=1, to_focal_id=2)
        ],
        "expected_grid": next(iter(fixture.rasters.values())).grid,
        "emit_current_map": False,
        "neighbor_rule": "EIGHT",
        "resistance_average": "ARITHMETIC",
    }
    cached = direct_study._load_exact_job(job_root, evidence_root, expected, **cache_kwargs)
    assert cached is not None and cached.cached

    changed_envelope = replace(
        expected,
        revision_id="revision-b",
        engine_manifest_sha256="7" * 64,
        workflow_manifest_sha256="8" * 64,
        decision_snapshot_sha256="9" * 64,
    )
    reenveloped = direct_study._load_exact_job(
        job_root, evidence_root, changed_envelope, **cache_kwargs
    )
    assert reenveloped is not None and reenveloped.cached
    assert reenveloped.artifact_directory != artifact_directory
    completion = json.loads((job_root / "complete.json").read_text(encoding="utf-8"))
    assert completion["cache_lineage"]["source_artifact_directory"] == artifact_directory
    lineage = evidence_root / completion["cache_lineage"]["source_completion_relative_path"]
    assert (
        hashlib.sha256(lineage.read_bytes()).hexdigest()
        == completion["cache_lineage"]["source_completion_sha256"]
    )

    changed_request = replace(expected, request_sha256="a" * 64)
    assert (
        direct_study._load_exact_job(
            job_root,
            evidence_root,
            changed_request,
            **{**cache_kwargs, "request_payload": b"changed request\n"},
        )
        is None
    )

    retained_matrix = (
        evidence_root
        / reenveloped.artifact_directory
        / "outputs/connectivity_resistances_3columns.out"
    )
    retained_matrix.write_bytes(b"1 2 9.0\n")
    with pytest.raises(direct_study.DirectStudyError, match="raw matrix|artifact failed"):
        direct_study._load_exact_job(job_root, evidence_root, changed_envelope, **cache_kwargs)


def test_direct_study_cancels_queued_jobs_after_first_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fake_execute_job(**kwargs: object) -> object:
        portfolio_id = str(kwargs["portfolio_id"])
        calls.append(portfolio_id)
        if portfolio_id == "fail":
            raise direct_study.DirectStudyError("expected test failure")
        time.sleep(0.05)
        return object()

    monkeypatch.setattr(direct_study, "_execute_job", fake_execute_job)
    with pytest.raises(direct_study.DirectStudyError, match="expected test failure"):
        direct_study._execute_phase_jobs(
            evidence_root=tmp_path,
            sources=tmp_path,
            fixture=build_ridgeway_fixture(),
            specs=[("fail", "scenario")] + [(f"queued_{index}", "scenario") for index in range(20)],
            phase="matrix",
            engine_manifest_sha256="0" * 64,
            engine_environment_sha256="1" * 64,
            workflow_manifest_sha256="2" * 64,
            julia_executable=tmp_path / "julia",
            julia_project=tmp_path,
            julia_depot=tmp_path,
            worker_count=2,
            timeout_seconds=1,
            progress=None,
            progress_offset=0,
            progress_total=21,
        )
    assert len(calls) < 21
