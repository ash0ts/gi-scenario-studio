from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest
from ecocorridor_core import benchmark
from ecocorridor_core.benchmark import (
    BENCHMARK_SCHEMA_VERSION,
    DirectBenchmarkDataset,
    HiddenDirectOracle,
    assert_adaptive_production_authorized,
    build_campaign_resource_report,
    derive_ridgeway_benchmark_tables,
    evaluate_gate,
    finalize_dataset_payload,
    load_benchmark_dataset,
    run_campaign,
    run_replay,
    verify_campaign_report,
    verify_campaign_resource_report,
    verify_dataset_manifest,
    verify_historical_search_provenance,
)
from ecocorridor_core.contracts import (
    ComputationalResourceObservation,
    ResourceObservationBoundary,
    ResourceObservationStatus,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _dataset_payload(landscape: int, *, row_count: int = 128) -> dict:
    sites = [
        {"site_id": f"site_{index:02d}", "option_ids": ["no_action", "reviewed_action"]}
        for index in range(7)
    ]
    rows = []
    for value in range(row_count):
        vector = [(value >> bit) & 1 for bit in range(7)]
        cost = float(sum((bit + 1) * 11_000 * selected for bit, selected in enumerate(vector)))
        benefit = sum((bit + 1 + landscape * 0.3) * selected for bit, selected in enumerate(vector))
        benefit += (2.0 + landscape * 0.25) * vector[0] * vector[3]
        benefit -= (1.5 + landscape * 0.2) * vector[1] * vector[6]
        feasible = benefit >= 11 + landscape and cost <= 250_000
        adverse = bool((vector[1] and vector[6]) or (vector[2] and not vector[5]))
        portfolio_id = f"portfolio_{value:03d}"
        run_ids = [f"run_{landscape}_{value:03d}_{scenario}" for scenario in range(3)]
        result_shas = [_digest(run_id) for run_id in run_ids]
        safeguard_reduction = -1.0 if adverse else 0.0
        rows.append(
            {
                "portfolio_id": portfolio_id,
                "vector": vector,
                "objective_values": {
                    "cost_usd": cost,
                    "negative_worst_target_reduction_percent": round(-benefit, 8),
                },
                "feasible": feasible,
                "safeguard_adverse": adverse,
                "direct_run_ids": run_ids,
                "direct_result_sha256s": result_shas,
                "observed_scenario_pair_metrics": [
                    {
                        "scenario_id": scenario_id,
                        "guild_id": scenario_id,
                        "safeguard": scenario_id == "safeguard",
                        "pair_id": "pair_1_2",
                        "baseline_effective_resistance": 100.0,
                        "portfolio_effective_resistance": 100.0
                        * (
                            1
                            - (safeguard_reduction if scenario_id == "safeguard" else benefit) / 100
                        ),
                        "baseline_relative_reduction_percent": (
                            safeguard_reduction if scenario_id == "safeguard" else benefit
                        ),
                        "direct_run_id": run_id,
                        "direct_result_sha256": result_sha,
                    }
                    for scenario_id, run_id, result_sha in zip(
                        ("target_a", "target_b", "safeguard"),
                        run_ids,
                        result_shas,
                        strict=True,
                    )
                ],
            }
        )
    return finalize_dataset_payload(
        {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "dataset_id": f"synthetic_direct_{landscape}",
            "landscape_id": f"landscape_{landscape}",
            "title": f"Synthetic direct landscape {landscape}",
            "license": "CC0-1.0",
            "synthetic": True,
            "intended_use": "ALGORITHM_RELEASE_GATE_ONLY",
            "evidence_source": {
                "kind": "DIRECT_CIRCUITSCAPE",
                "engine_name": "Circuitscape",
                "engine_manifest_sha256": _digest("engine"),
                "workflow_manifest_sha256": _digest("workflow"),
                "source_evidence_index_sha256": _digest(f"index_{landscape}"),
                "model_sha256": _digest("model"),
                "landscape_sha256": _digest(f"landscape_{landscape}"),
                "scenario_ids": ["target_a", "target_b", "safeguard"],
            },
            "baseline_portfolio_id": "portfolio_000",
            "scenario_count": 3,
            "objective_directions": {
                "cost_usd": "minimize",
                "negative_worst_target_reduction_percent": "minimize",
            },
            "feasibility_definition": "Synthetic threshold and cost disposition.",
            "safeguard_adverse_definition": "Synthetic safeguard/adverse test label.",
            "site_options": sites,
            "universe_size": row_count,
            "rows": rows,
        }
    )


def _write_dataset(path: Path, landscape: int) -> DirectBenchmarkDataset:
    path.write_text(json.dumps(_dataset_payload(landscape), indent=2) + "\n", encoding="utf-8")
    return load_benchmark_dataset(path)


def test_dataset_digest_and_direct_ensemble_are_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "dataset.json"
    dataset = _write_dataset(path, 1)
    assert len(dataset.rows) == 128
    assert dataset.scenario_count == 3

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"][0]["objective_values"]["cost_usd"] += 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        load_benchmark_dataset(path)

    payload = _dataset_payload(1)
    payload["rows"][0]["direct_run_ids"].pop()
    payload = finalize_dataset_payload(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="one unique direct run per scenario"):
        load_benchmark_dataset(path)


def test_hidden_oracle_exposes_only_identity_until_unique_request(tmp_path: Path) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json", 1)
    oracle = HiddenDirectOracle(dataset.rows)
    assert set(oracle.catalog[0].__slots__) == {"portfolio_id", "vector"}
    observed = oracle.evaluate("portfolio_000")
    assert observed.objective_values == (0.0, 0.0)
    with pytest.raises(ValueError, match="twice"):
        oracle.evaluate("portfolio_000")
    with pytest.raises(ValueError, match="outside"):
        oracle.evaluate("invented")


def test_replays_are_deterministic_and_adaptive_reserves_sobol(tmp_path: Path) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json", 1)
    first = run_replay(dataset, method="sobol_extratrees", seed=7, budget=80)
    second = run_replay(dataset, method="sobol_extratrees", seed=7, budget=80)
    assert first == second
    assert len(first["selected_portfolio_ids"]) == 80
    assert len(set(first["selected_portfolio_ids"])) == 80
    assert first["selected_portfolio_ids"][0] == dataset.baseline_portfolio_id
    assert first["selection_counts"] == {"exploration": 68, "adaptive": 12}
    assert not any("predict" in key.lower() for key in first)
    assert not any(
        "predict" in key.lower() for checkpoint in first["history"] for key in checkpoint
    )
    assert first["summary"]["exact_job_equivalent"] == 240

    random_result = run_replay(dataset, method="random", seed=7, budget=80)
    sobol_result = run_replay(dataset, method="sobol", seed=7, budget=80)
    assert random_result["selection_counts"]["adaptive"] == 0
    assert sobol_result["selection_counts"]["adaptive"] == 0


def test_unseen_outcome_changes_cannot_change_the_first_adaptive_batch(tmp_path: Path) -> None:
    original_path = tmp_path / "original.json"
    original = _write_dataset(original_path, 1)
    initial = run_replay(original, method="sobol", seed=19, budget=64)
    revealed_ids = set(initial["selected_portfolio_ids"])

    altered_payload = json.loads(original_path.read_text(encoding="utf-8"))
    for row in altered_payload["rows"]:
        if row["portfolio_id"] not in revealed_ids:
            row["objective_values"]["cost_usd"] += 9_999_999
            row["feasible"] = not row["feasible"]
            row["safeguard_adverse"] = not row["safeguard_adverse"]
    altered_payload = finalize_dataset_payload(altered_payload)
    altered_path = tmp_path / "altered.json"
    altered_path.write_text(json.dumps(altered_payload), encoding="utf-8")
    altered = load_benchmark_dataset(altered_path)

    original_trace = run_replay(original, method="sobol_extratrees", seed=19, budget=80)
    altered_trace = run_replay(altered, method="sobol_extratrees", seed=19, budget=80)
    assert original_trace["selected_portfolio_ids"] == altered_trace["selected_portfolio_ids"]


def test_gate_uses_declared_thresholds_conservatively() -> None:
    records = []
    for landscape in range(3):
        for seed in range(10):
            for budget in (64, 96):
                for method in ("random", "sobol", "sobol_extratrees"):
                    records.append(
                        {
                            "landscape_id": f"landscape_{landscape}",
                            "seed": seed,
                            "budget": budget,
                            "method": method,
                            "score": {
                                "hypervolume_ratio": (0.8 if method == "sobol_extratrees" else 0.7),
                                "feasible_recall": (0.75 if method == "sobol_extratrees" else 0.7),
                                "safeguard_adverse_recall": (
                                    0.96 if method == "sobol_extratrees" else 0.8
                                ),
                            },
                        }
                    )
    gate = evaluate_gate(records, landscape_count=3, seeds_per_landscape=10, budgets=(64, 96))
    assert gate.production_enabled is True
    assert gate.safeguard_adverse_recall_percent == 96
    assert gate.feasible_recall_delta_percent == pytest.approx(5)

    records[-1]["score"]["safeguard_adverse_recall"] = 0.94
    failed = evaluate_gate(records, landscape_count=3, seeds_per_landscape=10, budgets=(64, 96))
    assert failed.production_enabled is False


def test_release_campaign_is_digested_and_unpinned_ml_stays_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark, "EXTRATREES_ESTIMATORS", 4)
    paths = [tmp_path / f"dataset-{index}.json" for index in range(3)]
    datasets = [_write_dataset(path, index) for index, path in enumerate(paths)]
    report = run_campaign(datasets, seeds=range(10), budgets=(80, 96))
    assert report["production_adaptive_enabled"] is False
    assert "No immutable W&B" in " ".join(report["production_blockers"])
    assert len(report["run_records"]) == 3 * 10 * 3 * 2
    assert len(report["traces"]) == 3 * 10 * 3

    report_path = tmp_path / "release-gate.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    verified = verify_campaign_report(report_path, paths)
    assert verified["campaign_sha256"] == report["campaign_sha256"]
    with pytest.raises(PermissionError, match="disabled"):
        assert_adaptive_production_authorized(
            report_path,
            paths,
            expected_campaign_sha256=report["campaign_sha256"],
        )


def test_campaign_resource_sidecar_is_separate_bound_and_fail_closed(tmp_path: Path) -> None:
    campaign = {"campaign_sha256": "a" * 64}
    observation = ComputationalResourceObservation(
        observation_id="resource:campaign",
        boundary=ResourceObservationBoundary.SEARCH_PROPOSER,
        proposer_wall_seconds=36.0,
        peak_memory_status=ResourceObservationStatus.MEASURED,
        peak_memory_bytes=123_456,
        peak_memory_method="TEST_PROCESS_RSS_SAMPLED",
        peak_memory_sample_count=3,
        peak_memory_sampling_interval_seconds=0.1,
        peak_memory_is_lower_bound=True,
        energy_status=ResourceObservationStatus.ESTIMATED,
        energy_kwh=0.00015,
        energy_method="SINGLE_THREAD_ACTIVE_POWER_PROXY_V1",
        energy_estimate_watts=15.0,
        energy_estimate_lower_watts=5.0,
        energy_estimate_upper_watts=40.0,
        energy_estimate_lower_kwh=0.00005,
        energy_estimate_upper_kwh=0.0004,
        hardware="os=test;arch=test;logical_cpus=2",
        boundary_description="One bounded search campaign.",
        assumptions=["One process."],
        exclusions=["No carbon conversion."],
    )
    sidecar = build_campaign_resource_report(campaign, observation.model_dump(mode="json"))
    path = tmp_path / "campaign.resources.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")
    verified = verify_campaign_resource_report(
        path, expected_campaign_sha256=campaign["campaign_sha256"]
    )
    assert verified["resource_observation"]["energy_status"] == "ESTIMATED"

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["resource_observation"]["energy_kwh"] = 0
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        verify_campaign_resource_report(path, expected_campaign_sha256=campaign["campaign_sha256"])


def test_committed_historical_resource_sidecar_uses_frozen_sources_not_current_code() -> None:
    root = Path(__file__).resolve().parents[2]
    provenance_path = root / "benchmarks/ecocorridor/search/historical-provenance.json"
    campaign = json.loads(
        (root / "benchmarks/ecocorridor/search/release-gate.json").read_text(encoding="utf-8")
    )
    provenance = verify_historical_search_provenance(
        provenance_path,
        expected_campaign_sha256=campaign["campaign_sha256"],
    )
    frozen_sources = {
        identity: record["sha256"]
        for identity, record in provenance["generator_source_snapshots"].items()
    }
    verified = verify_campaign_resource_report(
        root / "benchmarks/ecocorridor/search/release-gate.resources.json",
        expected_campaign_sha256=campaign["campaign_sha256"],
        expected_execution_method_sources=frozen_sources,
    )
    assert verified["execution_method_sources"] == frozen_sources

    with pytest.raises(ValueError, match="declared observation sources"):
        verify_campaign_resource_report(
            root / "benchmarks/ecocorridor/search/release-gate.resources.json",
            expected_campaign_sha256=campaign["campaign_sha256"],
        )


def test_committed_verify_cli_requires_explicit_historical_provenance() -> None:
    root = Path(__file__).resolve().parents[2]
    search_root = root / "benchmarks/ecocorridor/search"
    arguments = [
        "verify",
        "--report",
        str(search_root / "release-gate.json"),
        "--resource-report",
        str(search_root / "release-gate.resources.json"),
        "--manifest",
        str(search_root / "manifest.json"),
    ]
    for dataset_name in (
        "h1_low_barrier.json",
        "h2_moderate_barrier.json",
        "h3_high_barrier.json",
        "h4_future_pressure.json",
    ):
        arguments.extend(("--dataset", str(search_root / dataset_name)))

    with pytest.raises(ValueError, match="declared observation sources"):
        benchmark.main(arguments)

    assert (
        benchmark.main(
            [
                *arguments,
                "--historical-provenance",
                str(search_root / "historical-provenance.json"),
            ]
        )
        == 0
    )


def test_historical_provenance_rejects_production_even_for_an_enabled_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    provenance_path = root / "benchmarks/ecocorridor/search/historical-provenance.json"
    campaign_sha = "a" * 64
    monkeypatch.setattr(
        benchmark,
        "verify_campaign_report",
        lambda *_args, **_kwargs: {
            "campaign_sha256": campaign_sha,
            "production_adaptive_enabled": True,
        },
    )
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["campaign"]["campaign_sha256"] = campaign_sha
    unsigned = {key: value for key, value in provenance.items() if key != "provenance_sha256"}
    provenance["provenance_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    temporary = tmp_path / "historical-provenance.json"
    temporary.write_text(json.dumps(provenance), encoding="utf-8")
    with pytest.raises(PermissionError, match="historical benchmark provenance prohibits"):
        assert_adaptive_production_authorized(
            "ignored-report.json",
            [],
            expected_campaign_sha256=campaign_sha,
            provenance_path=temporary,
        )


def test_ridgeway_derivation_requires_and_preserves_direct_evidence(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    jobs = []
    scenario_order = {
        scenario.scenario_id: index for index, scenario in enumerate(fixture.bundle.scenarios)
    }
    for portfolio_index, portfolio in enumerate(fixture.portfolios):
        action_count = sum(
            option.endswith("_reviewed_action") for option in portfolio.selections.values()
        )
        for scenario in fixture.bundle.scenarios:
            scenario_index = scenario_order[scenario.scenario_id]
            baseline = 100.0 + scenario_index
            reduction = action_count * 0.8
            if scenario.safeguard and portfolio_index % 17 == 0:
                reduction = -0.2
            observed = baseline * (1 - reduction / 100)
            run_id = f"run_{portfolio.portfolio_id}_{scenario.scenario_id}"
            jobs.append(
                {
                    "direct_run_id": run_id,
                    "portfolio_id": portfolio.portfolio_id,
                    "scenario_id": scenario.scenario_id,
                    "state": "SUCCEEDED",
                    "exact": True,
                    "engine_name": "Circuitscape",
                    "engine_manifest_sha256": "a" * 64,
                    "qa_status": "ACCEPTED",
                    "test_only": False,
                    "result_sha256": _digest(f"result_{run_id}"),
                    "matrix_sha256": _digest(f"matrix_{run_id}"),
                    "pair_metrics": [{"pair_id": "pair_1_2", "effective_resistance": observed}],
                }
            )
    evidence = {
        "schema_version": "ridgeway-evidence-index/v1.0",
        "engine_manifest_sha256": "a" * 64,
        "workflow_manifest_sha256": "b" * 64,
        "model_sha256": fixture.bundle.revision.model_sha256,
        "matrix_jobs": jobs,
        "map_jobs": [],
    }
    evidence_path = tmp_path / "evidence-index.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    output = tmp_path / "search"
    paths = derive_ridgeway_benchmark_tables(evidence_path, output)
    assert len(paths) == 4
    manifest_path = output / "manifest.json"
    assert manifest_path.is_file()
    verify_dataset_manifest(manifest_path, paths)
    derived = [load_benchmark_dataset(path) for path in paths]
    assert len({item.landscape_sha256 for item in derived}) == 4
    assert all(len(item.rows) == 128 and item.scenario_count == 3 for item in derived)


def test_current_ridgeway_archive_derives_only_with_bound_execution_provenance(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    archive = (
        root / "benchmarks/ecocorridor/ridgeway" / "ridgeway-direct-evidence-a0dc6b763347.tar.gz"
    )
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.extractfile("evidence-index.json")
        assert member is not None
        evidence = json.loads(member.read().decode("utf-8"))

    evidence_path = tmp_path / "evidence-index.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    output = tmp_path / "current-search"
    paths = derive_ridgeway_benchmark_tables(evidence_path, output)
    assert len(paths) == 4
    verify_dataset_manifest(output / "manifest.json", paths)
    derived = [load_benchmark_dataset(path) for path in paths]
    assert all(item.dataset_id.endswith("_direct_v2") for item in derived)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "ecocorridor-search-manifest/v1.1"
    downgraded = {**manifest, "schema_version": "ecocorridor-search-manifest/v1.0"}
    downgraded["manifest_sha256"] = benchmark._canonical_sha256(
        {key: value for key, value in downgraded.items() if key != "manifest_sha256"}
    )
    downgraded_path = tmp_path / "downgraded-manifest.json"
    downgraded_path.write_text(json.dumps(downgraded), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot use the historical manifest schema"):
        verify_dataset_manifest(downgraded_path, paths)

    evidence["matrix_jobs"][0]["decision_snapshot_sha256"] = "0" * 64
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(ValueError, match="bound execution provenance"):
        derive_ridgeway_benchmark_tables(evidence_path, tmp_path / "rejected-search")
