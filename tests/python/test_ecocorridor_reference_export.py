from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from ecocorridor_core.analysis_executor import DurableAnalysisExecutor
from ecocorridor_core.contracts import (
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    DirectConnectivityRun,
    DirectRunState,
    PairResistanceMetric,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    QAStatus,
    ReferencePairComparison,
    ReferenceScenarioComparison,
    canonical_sha256,
)
from ecocorridor_core.export import (
    ConnectivityExportManifest,
    EvidenceArtifactSource,
    EvidenceProjectAssetSource,
    ReferenceEvidenceExportSource,
    _materialize_reference_evidence,
    _reference_evidence_blockers,
    _verify_reference_values_from_retained_matrices,
    compute_export_eligibility,
)
from ecocorridor_core.observability import ECOCORRIDOR_APP_PROFILE
from ecocorridor_core.reference_comparison import assemble_reference_comparison_result
from ecocorridor_core.reference_workflow import (
    _reference_claim,
    build_reference_comparison_plan,
    reference_evidence_claim_links,
    reference_result_id,
    reference_scenario_comparison_id,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_worker import RPCDispatcher
from test_ecocorridor_reference_store import _build_authoritative_reference_case
from test_ecocorridor_reference_workflow_rpc import ENGINE_SHA, NOW, _bundle_and_result


def _matrix_bytes(bundle, scenario, values: dict[str, float]) -> bytes:
    focal_set = next(
        item for item in bundle.focal_area_sets if item.focal_set_id == scenario.focal_set_id
    )
    pairs = {item.pair_id: item for item in focal_set.pairs}
    return b"".join(
        (
            f"{pairs[pair_id].from_focal_id}\t{pairs[pair_id].to_focal_id}\t"
            f"{values[pair_id]:.17g}\n"
        ).encode()
        for pair_id in scenario.included_pair_ids
    )


def _reference_source(
    root: Path,
    *,
    identity: str,
    observed_delta: float,
) -> tuple[
    ConnectivityProjectBundle,
    ReferenceEvidenceExportSource,
    EvidenceProjectAssetSource,
]:
    bundle, template_result = _bundle_and_result(passed=True)
    scenario = bundle.scenarios[0]
    binding = bundle.reference_bindings[0]
    template_comparison = template_result.scenario_comparisons[0]
    reference_values = {
        item.pair_id: item.reference_effective_resistance
        for item in template_comparison.pair_comparisons
    }
    observed_values = {
        pair_id: value + observed_delta for pair_id, value in reference_values.items()
    }
    reference_payload = _matrix_bytes(bundle, scenario, reference_values)
    observed_payload = _matrix_bytes(bundle, scenario, observed_values)
    matrix_asset = next(
        item for item in bundle.assets if item.asset_id == binding.reference_matrix_asset_id
    ).model_copy(
        update={
            "relative_path": f"references/{identity}.tsv",
            "sha256": hashlib.sha256(reference_payload).hexdigest(),
            "byte_size": len(reference_payload),
        }
    )
    bundle = ConnectivityProjectBundle.model_validate(
        {
            **bundle.model_dump(mode="json"),
            "assets": [
                (matrix_asset if item.asset_id == matrix_asset.asset_id else item).model_dump(
                    mode="json"
                )
                for item in bundle.assets
            ],
        }
    )
    plan = build_reference_comparison_plan(
        bundle=bundle,
        analysis_id=f"reference-analysis-{identity}",
        engine_manifest_sha256=ENGINE_SHA,
        created_at=NOW,
    )
    direct_run_id = f"reference-run-{identity}"
    artifact_payloads = {
        "request": f"{direct_run_id}:request\n".encode(),
        "result_protocol": f"{direct_run_id}:result\n".encode(),
        "matrix": observed_payload,
        "circuitscape_configuration": b"output_file = outputs/connectivity\n",
        "action_raster_diff": b'{"changes":[]}\n',
        "footprint_proof_document": b'{"proofs":[]}\n',
        "input_resistance": b"ncols 1\nnrows 1\n1\n",
        "input_focal": b"ncols 1\nnrows 1\n1\n",
        "input_included_pairs": b"mode\tinclude\n1\t2\n",
    }
    retained = root / identity / "retained"
    retained.mkdir(parents=True)
    artifact_sources: list[EvidenceArtifactSource] = []
    for role, payload in artifact_payloads.items():
        path = retained / f"{role}.bin"
        path.write_bytes(payload)
        artifact_sources.append(
            EvidenceArtifactSource(
                direct_run_id=direct_run_id,
                role=role,
                source_path=path,
                sha256=hashlib.sha256(payload).hexdigest(),
                byte_size=len(payload),
                media_type=("application/json" if role.endswith("diff") else "text/plain"),
            )
        )
    run = DirectConnectivityRun(
        direct_run_id=direct_run_id,
        analysis_id=plan.analysis_id,
        revision_id=bundle.revision.revision_id,
        portfolio_id=plan.run_plan.baseline_portfolio_id,
        scenario_id=scenario.scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256=ENGINE_SHA,
        model_run_cache_key=hashlib.sha256(direct_run_id.encode()).hexdigest(),
        input_sha256=hashlib.sha256(artifact_payloads["request"]).hexdigest(),
        result_sha256=hashlib.sha256(artifact_payloads["result_protocol"]).hexdigest(),
        matrix_sha256=hashlib.sha256(observed_payload).hexdigest(),
        pair_metrics=[
            PairResistanceMetric(
                pair_id=pair_id,
                effective_resistance=observed_values[pair_id],
            )
            for pair_id in scenario.included_pair_ids
        ],
        qa_status=QAStatus.ACCEPTED,
        completed_at=NOW,
    )
    pair_comparisons = []
    for pair_id in scenario.included_pair_ids:
        reference_value = reference_values[pair_id]
        observed_value = observed_values[pair_id]
        difference = abs(observed_value - reference_value)
        allowed = binding.tolerance.absolute_tolerance + (
            binding.tolerance.relative_tolerance * reference_value
        )
        pair_comparisons.append(
            ReferencePairComparison(
                pair_id=pair_id,
                reference_effective_resistance=reference_value,
                observed_effective_resistance=observed_value,
                tolerance=binding.tolerance,
                absolute_difference=difference,
                relative_difference=difference / reference_value,
                allowed_difference=allowed,
                within_tolerance=difference <= allowed,
            )
        )
    comparison = ReferenceScenarioComparison(
        **{
            **template_comparison.model_dump(mode="python"),
            "comparison_id": reference_scenario_comparison_id(
                plan_id=plan.plan_id,
                binding_id=binding.binding_id,
                scenario_id=scenario.scenario_id,
                direct_run_id=direct_run_id,
                result_sha256=run.result_sha256,
                reference_matrix_sha256=matrix_asset.sha256,
            ),
            "analysis_id": plan.analysis_id,
            "direct_run_id": direct_run_id,
            "reference_matrix_sha256": matrix_asset.sha256,
            "observed_matrix_sha256": run.matrix_sha256,
            "pair_comparisons": pair_comparisons,
            "status": "PASSED",
        }
    )
    links = reference_evidence_claim_links([comparison])
    result = assemble_reference_comparison_result(
        result_id=reference_result_id(
            plan_id=plan.plan_id,
            comparisons=[comparison],
            claim_links=links,
        ),
        plan=plan,
        scenario_comparisons=[comparison],
        evidence_claim_links=links,
        completed_at=NOW,
        limitations=comparison.limitations,
    )
    pairs = {item.pair_id: item for item in comparison.pair_comparisons}
    baseline = plan.run_plan.portfolios[0]
    claims = tuple(
        _reference_claim(
            link=link,
            comparison=comparison,
            pair=pairs[link.pair_id],
            run=run,
            scenario=scenario,
            matrix_asset=matrix_asset,
            baseline=baseline,
        )
        for link in links
    )
    reference_path = root / identity / matrix_asset.relative_path
    reference_path.parent.mkdir(parents=True)
    reference_path.write_bytes(reference_payload)
    project_asset_source = EvidenceProjectAssetSource(
        asset_id=matrix_asset.asset_id,
        source_path=reference_path,
        sha256=matrix_asset.sha256,
        byte_size=matrix_asset.byte_size,
        media_type=matrix_asset.media_type,
    )
    source = ReferenceEvidenceExportSource(
        plan=plan,
        plan_sha256=canonical_sha256(plan.model_dump(mode="json")),
        result=result,
        result_sha256=canonical_sha256(result.model_dump(mode="json")),
        claims=claims,
        runs=(run,),
        artifact_sources=tuple(artifact_sources),
    )
    return bundle, source, project_asset_source


def test_reference_export_section_is_complete_explicit_and_path_safe(tmp_path: Path) -> None:
    bundle, source, project_asset = _reference_source(
        tmp_path,
        identity="first",
        observed_delta=0.1,
    )
    assert (
        _reference_evidence_blockers(
            bundle=bundle,
            expected_engine_manifest_sha256=ENGINE_SHA,
            sources=[source],
        )
        == []
    )
    package = tmp_path / "package"
    packaged_reference = package / "source-assets" / project_asset.asset_id / "asset.tsv"
    packaged_reference.parent.mkdir(parents=True)
    packaged_reference.write_bytes(project_asset.source_path.read_bytes())
    (package / "engine-manifest.json").write_text("{}\n", encoding="utf-8")
    section = _materialize_reference_evidence(
        export_directory=package,
        bundle=bundle,
        current_authorization_bundle=bundle,
        engine_manifest=SimpleNamespace(sha256=ENGINE_SHA),
        sources=[source],
        source_asset_index={
            project_asset.asset_id: packaged_reference.relative_to(package).as_posix()
        },
    )
    assert section is not None
    assert section.scientific_conflict_disposition == "ACTIVE_PASSED_NO_CONFLICT"
    assert section.complete_raw_artifact_sync is False
    assert "not a complete raw artifact sync" in section.artifact_completeness_statement
    assert {item.role for item in section.excluded_artifacts} == {"stdout", "stderr"}
    entry = section.entries[0]
    assert entry.plan_sha256 == source.plan_sha256
    assert entry.result_sha256 == source.result_sha256
    assert set(entry.artifact_files) == {source.runs[0].direct_run_id}
    for relative in (
        entry.plan_file,
        entry.result_file,
        entry.claims_file,
        entry.direct_runs_file,
        section.reference_assets_file,
        section.reference_matrix_parser_file,
    ):
        assert (package / relative).is_file()
    assert not list(package.rglob("stdout*"))
    assert not list(package.rglob("stderr*"))
    manifest_payload = {
        "export_id": "reference-export",
        "project_id": bundle.project_id,
        "revision_id": bundle.revision.revision_id,
        "baseline_portfolio_id": source.plan.run_plan.baseline_portfolio_id,
        "model_snapshot_sha256": "0" * 64,
        "engine_manifest_sha256": ENGINE_SHA,
        "run_plan_sha256": "1" * 64,
        "cost_basis_sha256": "2" * 64,
        "conceptual": False,
        "notice": "Connectivity screening limitation.",
        "files": {"engine-manifest.json": "3" * 64},
        "direct_run_ids": [],
        "evidence_claim_ids": [],
        "sync_state": "CONNECTED",
        "reference_evidence": section.model_dump(mode="json"),
    }
    v1_1 = ConnectivityExportManifest.model_validate(
        {**manifest_payload, "schema_version": "ecocorridor-export/v1.1"}
    )
    assert v1_1.schema_version == "ecocorridor-export/v1.1"
    with pytest.raises(ValueError, match="schema version"):
        ConnectivityExportManifest.model_validate(
            {**manifest_payload, "schema_version": "ecocorridor-export/v1.0"}
        )
    v1_0_payload = {
        key: value for key, value in manifest_payload.items() if key != "reference_evidence"
    }
    assert (
        ConnectivityExportManifest.model_validate(
            {**v1_0_payload, "schema_version": "ecocorridor-export/v1.0"}
        ).reference_evidence
        is None
    )


def test_professional_eligibility_requires_reference_package_evidence(tmp_path: Path) -> None:
    bundle, source, _ = _reference_source(
        tmp_path,
        identity="eligibility",
        observed_delta=0.1,
    )
    missing = compute_export_eligibility(
        bundle=bundle,
        current_authorization_bundle=bundle,
        summaries=[],
        runs=[],
        deployment="WANDB_CLOUD",
        sync_state="CONNECTED",
        professional=True,
        expected_engine_manifest_sha256=ENGINE_SHA,
    )
    assert any("active reference bindings require" in item for item in missing.fitness_blockers)
    supplied = compute_export_eligibility(
        bundle=bundle,
        current_authorization_bundle=bundle,
        summaries=[],
        runs=[],
        deployment="WANDB_CLOUD",
        sync_state="CONNECTED",
        professional=True,
        reference_evidence=[source],
        expected_engine_manifest_sha256=ENGINE_SHA,
    )
    assert not any("Reference evidence:" in item for item in supplied.fitness_blockers)


def test_reference_export_blocks_conflict_tamper_and_mutually_consistent_forgery(
    tmp_path: Path,
) -> None:
    bundle, first, reference_asset = _reference_source(
        tmp_path,
        identity="first",
        observed_delta=0.1,
    )
    _, second, _ = _reference_source(
        tmp_path,
        identity="second",
        observed_delta=0.2,
    )
    conflict = _reference_evidence_blockers(
        bundle=bundle,
        expected_engine_manifest_sha256=ENGINE_SHA,
        sources=[first, second],
    )
    assert any("scientifically conflicting" in item for item in conflict)

    renamed_result = first.result.model_copy(update={"result_id": "favorable-result"})
    renamed_source = replace(
        first,
        result=renamed_result,
        result_sha256=canonical_sha256(renamed_result.model_dump(mode="json")),
    )
    noncanonical = _reference_evidence_blockers(
        bundle=bundle,
        expected_engine_manifest_sha256=ENGINE_SHA,
        sources=[renamed_source],
    )
    assert any("noncanonical identity" in item for item in noncanonical)

    matrix = next(item for item in first.artifact_sources if item.role == "matrix")
    matrix.source_path.write_bytes(matrix.source_path.read_bytes() + b"tampered")
    tampered = _reference_evidence_blockers(
        bundle=bundle,
        expected_engine_manifest_sha256=ENGINE_SHA,
        sources=[first],
    )
    assert any("failed verification" in item for item in tampered)

    bundle, source, reference_asset = _reference_source(
        tmp_path,
        identity="forged",
        observed_delta=0.1,
    )
    matrix = next(item for item in source.artifact_sources if item.role == "matrix")
    rows = matrix.source_path.read_text(encoding="utf-8").splitlines()
    fields = rows[0].split("\t")
    fields[2] = f"{float(fields[2]) + 0.1:.17g}"
    rows[0] = "\t".join(fields)
    forged_payload = ("\n".join(rows) + "\n").encode()
    matrix.source_path.write_bytes(forged_payload)
    forged_matrix = replace(
        matrix,
        sha256=hashlib.sha256(forged_payload).hexdigest(),
        byte_size=len(forged_payload),
    )
    forged_run = source.runs[0].model_copy(update={"matrix_sha256": forged_matrix.sha256})
    comparison = source.result.scenario_comparisons[0].model_copy(
        update={"observed_matrix_sha256": forged_matrix.sha256}
    )
    forged_result = source.result.model_copy(update={"scenario_comparisons": [comparison]})
    forged_source = replace(
        source,
        result=forged_result,
        result_sha256=canonical_sha256(forged_result.model_dump(mode="json")),
        runs=(forged_run,),
        artifact_sources=tuple(
            forged_matrix if item.role == "matrix" else item for item in source.artifact_sources
        ),
    )
    with pytest.raises(PermissionError, match="differ from retained raw matrices"):
        _verify_reference_values_from_retained_matrices(
            bundle=bundle,
            sources=[forged_source],
            project_asset_sources=[reference_asset],
        )


def test_reference_structured_artifact_with_local_path_is_not_packaged(tmp_path: Path) -> None:
    bundle, source, project_asset = _reference_source(
        tmp_path,
        identity="path-leak",
        observed_delta=0.1,
    )
    request = next(item for item in source.artifact_sources if item.role == "request")
    payload = b'{"work_directory":"/Volumes/ecology/private/project"}\n'
    request.source_path.write_bytes(payload)
    leaked_request = replace(
        request,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
    )
    leaked_run = source.runs[0].model_copy(update={"input_sha256": leaked_request.sha256})
    leaked_source = replace(
        source,
        runs=(leaked_run,),
        artifact_sources=tuple(
            leaked_request if item.role == "request" else item for item in source.artifact_sources
        ),
    )
    package = tmp_path / "path-package"
    packaged_reference = package / "source-assets" / project_asset.asset_id / "asset.tsv"
    packaged_reference.parent.mkdir(parents=True)
    packaged_reference.write_bytes(project_asset.source_path.read_bytes())
    (package / "engine-manifest.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="local filesystem path"):
        _materialize_reference_evidence(
            export_directory=package,
            bundle=bundle,
            current_authorization_bundle=bundle,
            engine_manifest=SimpleNamespace(sha256=ENGINE_SHA),
            sources=[leaked_source],
            source_asset_index={
                project_asset.asset_id: packaged_reference.relative_to(package).as_posix()
            },
        )


def test_rpc_collector_recovers_and_verifies_every_reference_chain(tmp_path: Path) -> None:
    case = _build_authoritative_reference_case(tmp_path / "authoritative-reference")
    store = case["store"]
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    dispatcher.engine_manifest = SimpleNamespace(
        sha256=case["reference_plan"].engine_manifest_sha256
    )
    try:
        sources = dispatcher._reference_export_evidence_sources(
            store=store,
            bundle=store.effective_bundle(),
        )
        assert len(sources) == 1
        source = sources[0]
        assert source.result == case["result"]
        assert source.plan == case["reference_plan"]
        assert source.runs == (case["run"],)
        assert {item.claim_id for item in source.claims} == {
            item.claim_id for item in case["claims"]
        }
        assert {item.role for item in source.artifact_sources} >= {
            "request",
            "result_protocol",
            "matrix",
            "input_resistance",
            "input_focal",
            "input_included_pairs",
        }
        assert not {"stdout", "stderr"}.intersection(item.role for item in source.artifact_sources)
        persisted = store.reference_comparison_results(
            analysis_id=case["reference_plan"].analysis_id
        )
        assert persisted == [(source.result, list(source.claims))]
    finally:
        store.close()


def test_real_dispatcher_creates_v1_1_and_blocks_conflicting_reference_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, reference_source, reference_asset = _reference_source(
        tmp_path / "reference-one",
        identity="dispatcher-one",
        observed_delta=0.1,
    )
    scenario = next(
        item
        for item in bundle.scenarios
        if item.scenario_id in reference_source.plan.bound_scenario_ids
    )
    bundle = bundle.model_copy(
        update={
            "scenarios": [scenario],
            "cost_basis": bundle.cost_basis.model_copy(
                update={
                    "assumptions": [
                        "Analysis horizon years: 30",
                        "Real discount rate percent: 3.0",
                        "CAPEX basis: Reviewed synthetic dispatcher test values.",
                        "O&M assumptions: Included in the reviewed synthetic values.",
                        "Replacement assumptions: Included in the reviewed synthetic values.",
                        "Reviewer confirmation: Reviewed for this synthetic dispatcher test.",
                    ]
                }
            ),
        }
    )
    project = tmp_path / "dispatcher-project"
    fixture = build_ridgeway_fixture()
    fixture_payloads = {
        asset_id: json.dumps(raster.values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        for asset_id, raster in fixture.rasters.items()
    }
    for asset in bundle.assets:
        path = project / asset.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            reference_asset.source_path.read_bytes()
            if asset.asset_id == reference_asset.asset_id
            else fixture_payloads[asset.asset_id]
        )
        assert hashlib.sha256(payload).hexdigest() == asset.sha256
        path.write_bytes(payload)

    ordinary_run = reference_source.runs[0].model_copy(
        update={"direct_run_id": "ordinary-direct-run", "analysis_id": "ordinary-analysis"}
    )
    ordinary_artifacts = tuple(
        replace(item, direct_run_id=ordinary_run.direct_run_id)
        for item in reference_source.artifact_sources
    )
    ordinary_plan = ConnectivityRunPlan.model_validate(
        {
            **reference_source.plan.run_plan.model_dump(mode="json"),
            "analysis_id": ordinary_run.analysis_id,
        }
    )
    baseline = ordinary_plan.portfolios[0]
    cost = sum(bundle.cost_basis.option_costs[item] for item in baseline.canonical_option_ids)
    summary = PortfolioDecisionSummary(
        portfolio_id=baseline.portfolio_id,
        disposition=PortfolioDisposition.PASSES,
        cost=cost,
        currency=bundle.cost_basis.currency,
        rule_dispositions=[],
        controlling_scenario_id=scenario.scenario_id,
        controlling_pair_id=scenario.included_pair_ids[0],
        worst_resistance_reduction_percent=0,
        next_action=(
            "Candidate to carry forward for ecological, field, and engineering feasibility review."
        ),
        direct_run_ids=[ordinary_run.direct_run_id],
        disposition_evidence_claim_id="ordinary-disposition-claim",
    )
    qa_claim = DurableAnalysisExecutor._qa_claim(claim_id="ordinary-qa-claim", run=ordinary_run)
    cost_claim = DurableAnalysisExecutor._cost_claim(
        claim_id="ordinary-cost-claim",
        bundle=bundle,
        portfolio=baseline,
        cost=cost,
        currency=bundle.cost_basis.currency,
    )
    disposition_claim = DurableAnalysisExecutor._portfolio_disposition_claim(
        claim_id="ordinary-disposition-claim",
        summary=summary,
        runs=[ordinary_run],
        supporting_claim_ids=[qa_claim.claim_id, cost_claim.claim_id],
    )
    ordinary_claims = [qa_claim, cost_claim, disposition_claim]

    class EngineDouble:
        sha256 = ENGINE_SHA

        @staticmethod
        def model_dump(*, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"schema_version": "engine-test-double/v1.0", "sha256": ENGINE_SHA}

    def verified(run: DirectConnectivityRun, sources: tuple[EvidenceArtifactSource, ...]):
        return SimpleNamespace(
            run=run,
            artifacts=tuple(
                {
                    "role": item.role,
                    "sha256": item.sha256,
                    "byte_size": item.byte_size,
                    "media_type": item.media_type,
                }
                for item in sources
            ),
            artifact_paths_by_role={item.role: item.source_path for item in sources},
        )

    reference_sources = [reference_source]
    publications: list[dict[str, object]] = []

    class AuthoritativeDispatcherStore:
        root: Path

        @staticmethod
        def close() -> None:
            return None

        @staticmethod
        def project() -> dict[str, object]:
            return {"project_id": bundle.project_id, "mode": "IMPORTED"}

        @staticmethod
        def observability() -> dict[str, object]:
            return {
                "entity": "wildlife-team",
                "project": "restricted-connectivity",
                "external_project_id": "restricted-project-id",
                "deployment": "SELF_MANAGED",
                "profile_namespace": ECOCORRIDOR_APP_PROFILE.namespace,
                "state": "CONNECTED",
                "scope_evidence": {
                    "entity": "wildlife-team",
                    "project": "restricted-connectivity",
                    "external_project_id": "restricted-project-id",
                    "deployment": "SELF_MANAGED",
                    "api_base_url": "https://api.wandb.example",
                    "scope": "RESTRICTED",
                    "evidence_kind": "ADMIN_API_DOCUMENTED_EXACT",
                    "provider_field": "documented_admin.visibility_scope",
                },
            }

        @staticmethod
        def analysis_bundle(_analysis_id: str) -> ConnectivityProjectBundle:
            return bundle

        @staticmethod
        def effective_bundle() -> ConnectivityProjectBundle:
            return bundle

        @staticmethod
        def analysis(_analysis_id: str) -> dict[str, object]:
            return {"plan": ordinary_plan.model_dump(mode="json")}

        @staticmethod
        def analysis_results(_analysis_id: str):
            return [ordinary_run], [summary], ordinary_claims

        @staticmethod
        def pending_reference_completions() -> list[str]:
            return []

        @staticmethod
        def reference_comparison_results(*, exclude_result_ids=(), **_filters):
            return [
                (item.result, list(item.claims))
                for item in reference_sources
                if item.result.result_id not in set(exclude_result_ids)
            ]

        @staticmethod
        def reference_comparison_plan(plan_id: str, *, analysis_id: str):
            source = next(
                item
                for item in reference_sources
                if item.plan.plan_id == plan_id and item.plan.analysis_id == analysis_id
            )
            return source.plan, source.plan_sha256

        @staticmethod
        def resolve_evidence_claim(claim_id: str) -> dict[str, object]:
            source = next(
                item for item in reference_sources if claim_id in {c.claim_id for c in item.claims}
            )
            claim = next(item for item in source.claims if item.claim_id == claim_id)
            return {
                "claim": claim.model_dump(mode="json"),
                "reference_result": source.result.model_dump(mode="json"),
            }

        @staticmethod
        def verify_authoritative_direct_run(direct_run_id: str, *, expected_analysis_id: str):
            if direct_run_id == ordinary_run.direct_run_id:
                assert expected_analysis_id == ordinary_run.analysis_id
                return verified(ordinary_run, ordinary_artifacts)
            source = next(
                item
                for item in reference_sources
                if direct_run_id in {r.direct_run_id for r in item.runs}
            )
            run = next(item for item in source.runs if item.direct_run_id == direct_run_id)
            assert expected_analysis_id == run.analysis_id
            return verified(run, source.artifact_sources)

        @staticmethod
        def resolve_asset_path(relative_path: str) -> Path:
            return project / relative_path

        @staticmethod
        def get_record(_record_type: str, _record_id: str):
            raise KeyError

        @staticmethod
        def prepare_export_publication(
            export_id: str,
            analysis_id: str,
            manifest: dict[str, object],
            **_kwargs,
        ) -> None:
            publications.append(
                {"export_id": export_id, "analysis_id": analysis_id, "manifest": manifest}
            )

        @staticmethod
        def finalize_export_publication(export_id: str) -> None:
            assert publications[-1]["export_id"] == export_id
            publications[-1]["finalized"] = True

    store = AuthoritativeDispatcherStore()
    store.root = project
    dispatcher = RPCDispatcher(data_root=tmp_path / "app-data")
    dispatcher.engine_manifest = EngineDouble()  # type: ignore[assignment]
    monkeypatch.setattr(dispatcher, "_store", lambda _directory: store)
    monkeypatch.setattr(
        dispatcher,
        "_sync_plan",
        lambda *_args, **_kwargs: ({}, {"allowed": True, "blockers": []}),
    )
    request = {
        "project_directory": str(project),
        "analysis_id": ordinary_run.analysis_id,
        "portfolio_ids": [baseline.portfolio_id],
    }
    validated = dispatcher.dispatch("connectivity.export.validate", request)["data"]
    assert validated["eligibility"]["eligible"] is True
    created = dispatcher.dispatch(
        "connectivity.export.create",
        {**request, "export_id": "reference-export-v1-1"},
    )["data"]
    assert created["manifest"]["schema_version"] == "ecocorridor-export/v1.1"
    assert created["manifest"]["reference_evidence"]["entries"][0]["result_id"] == (
        reference_source.result.result_id
    )
    assert publications == [
        {
            "export_id": "reference-export-v1-1",
            "analysis_id": ordinary_run.analysis_id,
            "manifest": created["manifest"],
            "finalized": True,
        }
    ]

    _, conflicting, _ = _reference_source(
        tmp_path / "reference-two",
        identity="dispatcher-two",
        observed_delta=0.2,
    )
    reference_sources.append(conflicting)
    conflicted = dispatcher.dispatch("connectivity.export.validate", request)["data"]
    assert conflicted["eligibility"]["eligible"] is False
    assert any(
        "scientifically conflicting" in item
        for item in conflicted["eligibility"]["fitness_blockers"]
    )
