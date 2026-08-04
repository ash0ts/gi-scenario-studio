from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ecocorridor_core.project_store as project_store_module
import ecocorridor_core.reference_matrix_parser as reference_parser_module
import pytest
from ecocorridor_core.contracts import (
    AssetKind,
    CloudDisposition,
    ConnectivityAsset,
    ConnectivityEvidenceClaim,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    DirectConnectivityRun,
    DirectRunState,
    EvidenceEdge,
    EvidenceNode,
    PairResistanceMetric,
    QAStatus,
    ReferenceComparisonPlan,
    ReferenceEvidenceClaimLink,
    ReferenceMetricTolerance,
    ReferencePairComparison,
    ReferenceScenarioBinding,
    SensitiveDataAuthorization,
    SensitivityClass,
    canonical_sha256,
)
from ecocorridor_core.engine import (
    EngineProtocolError,
    EngineProtocolResult,
    PreparedConnectivityRun,
    PreparedPair,
    PreparedResource,
    ProtocolPairMetric,
    effective_circuitscape_configuration_text,
    engine_request_payload,
    parse_circuitscape_matrix,
)
from ecocorridor_core.footprints import (
    create_professional_footprint_proof_document,
    footprint_proof_set_sha256,
)
from ecocorridor_core.metrics import CONNECTIVITY_LIMITATION
from ecocorridor_core.observability import ECOCORRIDOR_APP_PROFILE
from ecocorridor_core.project_store import (
    EcoProjectStore,
    _read_verified_regular_file,
)
from ecocorridor_core.reference_comparison import (
    assemble_reference_comparison_result,
    compare_reference_matrix,
)
from ecocorridor_core.reference_matrix_parser import (
    REFERENCE_MATRIX_PARSER_SHA256,
    parse_reference_matrix_bytes,
)
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.telemetry import EcoTelemetryProjector
from scenario_platform.cloud_sync import CloudIntegrityError, CloudTransportError
from scenario_platform.contracts import canonical_json
from scenario_platform.storage import ContentAddressedStore

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
ENGINE_SHA = "b" * 64


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_matrix(
    path: Path,
    pairs: list[PreparedPair],
    values: dict[str, float],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            f"{pair.from_focal_id} {pair.to_focal_id} {values[pair.pair_id]:.17g}\n"
            for pair in pairs
        ),
        encoding="utf-8",
    )
    return path


def _downgrade_reference_tables_to_pre_parser_schema(root: Path) -> None:
    connection = sqlite3.connect(root / "project.sqlite")
    try:
        connection.executescript(
            """
            ALTER TABLE eco_reference_comparison_results
              DROP COLUMN reference_matrix_parser_sha256;
            ALTER TABLE eco_reference_comparison_plans
              DROP COLUMN reference_matrix_parser_sha256;
            PRAGMA user_version=110;
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_schema_110_empty_reference_tables_gain_parser_provenance_atomically(
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty-schema-110"
    store = EcoProjectStore(root)
    store.close()
    _downgrade_reference_tables_to_pre_parser_schema(root)

    migrated = EcoProjectStore(root)
    try:
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == 111
        for table in (
            "eco_reference_comparison_plans",
            "eco_reference_comparison_results",
        ):
            columns = {
                str(row["name"]): row
                for row in migrated._connection.execute(f"PRAGMA table_info({table})")
            }
            parser_column = columns["reference_matrix_parser_sha256"]
            assert parser_column["notnull"] == 1
            assert str(parser_column["dflt_value"]).strip("'") == (REFERENCE_MATRIX_PARSER_SHA256)
    finally:
        migrated.close()


@pytest.mark.parametrize(
    ("case_name", "payload", "valid"),
    [
        ("valid_with_sentinel", b"1 2 10\n1 3 -1\n2 3 20\n", True),
        ("requested_sentinel", b"1 2 -1\n1 3 -1\n2 3 20\n", False),
        ("duplicate", b"1 2 10\n2 1 10\n2 3 20\n", False),
        ("malformed", b"1 2\n2 3 20\n", False),
        ("nonfinite", b"1 2 nan\n2 3 20\n", False),
        ("unexpected_pair", b"1 2 10\n1 3 5\n2 3 20\n", False),
        ("missing_pair", b"1 2 10\n1 3 -1\n", False),
    ],
)
def test_reference_bytes_parser_matches_pinned_path_parser(
    tmp_path: Path,
    case_name: str,
    payload: bytes,
    valid: bool,
) -> None:
    pairs = [
        PreparedPair(pair_id="pair-1-2", from_focal_id=1, to_focal_id=2),
        PreparedPair(pair_id="pair-2-3", from_focal_id=2, to_focal_id=3),
    ]
    path = tmp_path / f"{case_name}.txt"
    path.write_bytes(payload)
    if valid:
        pinned = {
            item.pair_id: item.effective_resistance
            for item in parse_circuitscape_matrix(path, pairs)
        }
        assert parse_reference_matrix_bytes(payload, pairs) == pinned
        return

    with pytest.raises(EngineProtocolError) as pinned_error:
        parse_circuitscape_matrix(path, pairs)
    with pytest.raises(ValueError) as bytes_error:
        parse_reference_matrix_bytes(payload, pairs)
    assert str(bytes_error.value) == str(pinned_error.value)


def test_reference_matrix_parser_digest_matches_only_its_normalized_source() -> None:
    source_path = Path(reference_parser_module.__file__).resolve()
    source = source_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    start_marker = "# BEGIN REFERENCE_MATRIX_PARSER_SOURCE\n"
    end_marker = "# END REFERENCE_MATRIX_PARSER_SOURCE"
    assert source.count(start_marker) == 1
    assert source.count(end_marker) == 1
    parser_source = source.split(start_marker, 1)[1].split(end_marker, 1)[0].strip() + "\n"
    assert parser_source.startswith("def parse_reference_matrix_bytes(")
    runtime_source = inspect.getsource(reference_parser_module.parse_reference_matrix_bytes)
    assert runtime_source.replace("\r\n", "\n").strip() + "\n" == parser_source
    digest_payload = b"ecocorridor/reference-matrix-parser-source/v1\0" + parser_source.encode(
        "utf-8"
    )
    assert hashlib.sha256(digest_payload).hexdigest() == (REFERENCE_MATRIX_PARSER_SHA256)


def test_descriptor_snapshot_rejects_symlinks_and_parses_captured_bytes(tmp_path: Path) -> None:
    original = b"1 2 10\n2 3 20\n"
    source = tmp_path / "matrix.txt"
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    captured = _read_verified_regular_file(
        source,
        expected_sha256=digest,
        expected_byte_size=len(original),
        capture_bytes=True,
    )
    source.write_bytes(b"1 2 999\n2 3 999\n")
    pairs = [
        PreparedPair(pair_id="pair-1-2", from_focal_id=1, to_focal_id=2),
        PreparedPair(pair_id="pair-2-3", from_focal_id=2, to_focal_id=3),
    ]
    assert parse_reference_matrix_bytes(captured, pairs) == {
        "pair-1-2": 10.0,
        "pair-2-3": 20.0,
    }

    symlink = tmp_path / "matrix-link.txt"
    symlink.symlink_to(source)
    with pytest.raises(ValueError, match="regular file|opened safely"):
        _read_verified_regular_file(
            symlink,
            expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_byte_size=source.stat().st_size,
            capture_bytes=True,
        )


def test_repository_parses_same_snapshot_when_path_changes_between_capture_and_parse(
    reference_case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference_path = reference_case["reference_path"]
    original_bytes = reference_path.read_bytes()
    parse_bytes = project_store_module.parse_reference_matrix_bytes
    replaced = False

    def replace_then_parse(payload: bytes, pairs):
        nonlocal replaced
        if not replaced:
            replaced = True
            replacement = reference_path.with_suffix(".replacement")
            replacement.write_bytes(b"1 2 999\n")
            replacement.replace(reference_path)
            try:
                return parse_bytes(payload, pairs)
            finally:
                reference_path.write_bytes(original_bytes)
        return parse_bytes(payload, pairs)

    monkeypatch.setattr(
        project_store_module,
        "parse_reference_matrix_bytes",
        replace_then_parse,
    )
    receipt = reference_case["store"].store_reference_comparison_result(
        reference_case["result"],
        claims=reference_case["claims"],
    )
    assert replaced is True
    assert receipt["replayed"] is False


def _reference_claim(
    *,
    link: ReferenceEvidenceClaimLink,
    comparison: Any,
    pair_comparison: Any,
    run: DirectConnectivityRun,
    scenario: Any,
    matrix_asset: ConnectivityAsset,
    baseline_portfolio_id: str,
) -> ConnectivityEvidenceClaim:
    text = "Direct Circuitscape result under documented assumptions."
    limitations = list(dict.fromkeys([CONNECTIVITY_LIMITATION, *comparison.limitations]))
    comparison_node_id = f"reference-comparison:{comparison.comparison_id}"
    metric_node_id = f"metric:{comparison.comparison_id}:{link.pair_id}"
    run_node_id = f"run:{run.direct_run_id}"
    scenario_node_id = f"scenario:{link.scenario_id}"
    asset_node_id = f"asset:{matrix_asset.asset_id}"
    engine_node_id = f"engine:{run.engine_manifest_sha256}"
    qa_node_id = f"qa:{run.direct_run_id}"
    artifact_node_id = f"artifact:matrix:{run.direct_run_id}:{run.matrix_sha256}"
    authorization_node_id = f"authorization:{matrix_asset.authorization.asset_id}"
    assumption_node_ids = {
        limitation: f"assumption:{hashlib.sha256(limitation.encode('utf-8')).hexdigest()}"
        for limitation in limitations
    }
    supporting = [
        EvidenceNode(
            node_id=comparison_node_id,
            node_type="REFERENCE_COMPARISON",
            label="Reviewed effective-resistance matrix comparison",
            sha256=canonical_sha256(comparison.model_dump(mode="json")),
            attributes={
                "comparison_id": comparison.comparison_id,
                "scenario_id": link.scenario_id,
                "pair_id": link.pair_id,
                "direct_run_id": link.direct_run_id,
                "binding_id": comparison.binding_id,
                "reference_matrix_asset_id": matrix_asset.asset_id,
                "status": comparison.status,
            },
        ),
        EvidenceNode(
            node_id=metric_node_id,
            node_type="METRIC",
            label="Reference tolerance disposition",
            attributes={
                "pair_id": link.pair_id,
                "reference_effective_resistance": (pair_comparison.reference_effective_resistance),
                "observed_effective_resistance": (pair_comparison.observed_effective_resistance),
                "absolute_difference": pair_comparison.absolute_difference,
                "relative_difference": pair_comparison.relative_difference,
                "allowed_difference": pair_comparison.allowed_difference,
                "within_tolerance": pair_comparison.within_tolerance,
                "units": "effective_resistance",
            },
        ),
        EvidenceNode(
            node_id=run_node_id,
            node_type="DIRECT_RUN",
            label=run.direct_run_id,
            sha256=run.result_sha256,
            attributes={
                "analysis_id": run.analysis_id,
                "portfolio_id": run.portfolio_id,
                "scenario_id": run.scenario_id,
            },
        ),
        EvidenceNode(
            node_id=scenario_node_id,
            node_type="SCENARIO",
            label=link.scenario_id,
            attributes={
                "scenario_id": scenario.scenario_id,
                "guild_id": scenario.guild_id,
                "resistance_hypothesis_id": scenario.resistance_hypothesis_id,
                "focal_set_id": scenario.focal_set_id,
                "safeguard": scenario.safeguard,
            },
        ),
        EvidenceNode(
            node_id=asset_node_id,
            node_type="ASSET",
            label=matrix_asset.title,
            sha256=matrix_asset.sha256,
            attributes={
                "asset_id": matrix_asset.asset_id,
                "kind": matrix_asset.kind.value,
                "authorization_id": matrix_asset.authorization.asset_id,
            },
        ),
        EvidenceNode(
            node_id=engine_node_id,
            node_type="ENGINE_MANIFEST",
            label="Pinned Circuitscape engine manifest",
            sha256=run.engine_manifest_sha256,
            attributes={
                "engine_name": "Circuitscape",
                "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
            },
        ),
        EvidenceNode(
            node_id=qa_node_id,
            node_type="QA",
            label=run.qa_status.value,
            attributes={
                "status": run.qa_status.value,
                "direct_run_id": run.direct_run_id,
                "findings_sha256": canonical_sha256(run.qa_findings),
            },
        ),
        EvidenceNode(
            node_id=authorization_node_id,
            node_type="AUTHORIZATION",
            label="Reviewed reference-data authorization",
            sha256=canonical_sha256(matrix_asset.authorization.model_dump(mode="json")),
            attributes={
                "asset_id": matrix_asset.authorization.asset_id,
                "cloud_disposition": matrix_asset.authorization.cloud_disposition.value,
                "export_allowed": matrix_asset.authorization.export_allowed,
                "reviewer": matrix_asset.authorization.reviewer,
            },
        ),
        EvidenceNode(
            node_id=artifact_node_id,
            node_type="ARTIFACT",
            label="Retained direct effective-resistance matrix",
            sha256=run.matrix_sha256,
            attributes={"role": "matrix", "direct_run_id": run.direct_run_id},
        ),
        *[
            EvidenceNode(
                node_id=assumption_node_ids[limitation],
                node_type="ASSUMPTION",
                label=limitation,
            )
            for limitation in sorted(limitations)
        ],
    ]
    return ConnectivityEvidenceClaim(
        claim_id=link.claim_id,
        claim_kind="DIRECT_REFERENCE_COMPARISON",
        text=text,
        value=pair_comparison.within_tolerance,
        units="within_tolerance",
        portfolio_id=baseline_portfolio_id,
        scenario_id=link.scenario_id,
        pair_id=link.pair_id,
        direct_run_ids=[link.direct_run_id],
        root_node_id=link.claim_id,
        nodes=[
            EvidenceNode(
                node_id=link.claim_id,
                node_type="CLAIM",
                label=text,
            ),
            *supporting,
        ],
        edges=[
            EvidenceEdge(
                from_node_id=link.claim_id,
                to_node_id=metric_node_id,
                relation="reports",
            ),
            EvidenceEdge(
                from_node_id=metric_node_id,
                to_node_id=comparison_node_id,
                relation="derived_from",
            ),
            EvidenceEdge(
                from_node_id=comparison_node_id,
                to_node_id=run_node_id,
                relation="compares_direct_run",
            ),
            EvidenceEdge(
                from_node_id=comparison_node_id,
                to_node_id=asset_node_id,
                relation="compares_against",
            ),
            EvidenceEdge(
                from_node_id=run_node_id,
                to_node_id=scenario_node_id,
                relation="executes",
            ),
            EvidenceEdge(
                from_node_id=run_node_id,
                to_node_id=engine_node_id,
                relation="executed_by",
            ),
            EvidenceEdge(
                from_node_id=run_node_id,
                to_node_id=qa_node_id,
                relation="checked_by",
            ),
            EvidenceEdge(
                from_node_id=run_node_id,
                to_node_id=artifact_node_id,
                relation="retained_as",
            ),
            EvidenceEdge(
                from_node_id=asset_node_id,
                to_node_id=authorization_node_id,
                relation="authorized_by",
            ),
            *[
                EvidenceEdge(
                    from_node_id=scenario_node_id,
                    to_node_id=assumption_node_ids[limitation],
                    relation="assumes",
                )
                for limitation in sorted(limitations)
            ],
        ],
        limitations=limitations,
    )


def _rename_claim(claim: ConnectivityEvidenceClaim, claim_id: str) -> ConnectivityEvidenceClaim:
    payload = claim.model_dump(mode="json")
    old_id = claim.claim_id
    payload["claim_id"] = claim_id
    payload["root_node_id"] = claim_id
    for node in payload["nodes"]:
        if node["node_id"] == old_id:
            node["node_id"] = claim_id
    for edge in payload["edges"]:
        if edge["from_node_id"] == old_id:
            edge["from_node_id"] = claim_id
        if edge["to_node_id"] == old_id:
            edge["to_node_id"] = claim_id
    return ConnectivityEvidenceClaim.model_validate(payload)


def _claim_without_limitation(
    claim: ConnectivityEvidenceClaim,
    limitation: str,
    *,
    comparison: Any | None = None,
) -> ConnectivityEvidenceClaim:
    payload = deepcopy(claim.model_dump(mode="json"))
    payload["limitations"] = [item for item in payload["limitations"] if item != limitation]
    assumption_node_id = f"assumption:{hashlib.sha256(limitation.encode('utf-8')).hexdigest()}"
    payload["nodes"] = [item for item in payload["nodes"] if item["node_id"] != assumption_node_id]
    payload["edges"] = [
        item
        for item in payload["edges"]
        if item["from_node_id"] != assumption_node_id and item["to_node_id"] != assumption_node_id
    ]
    if comparison is not None:
        comparison_node = next(
            item for item in payload["nodes"] if item["node_type"] == "REFERENCE_COMPARISON"
        )
        comparison_node["sha256"] = canonical_sha256(comparison.model_dump(mode="json"))
    return ConnectivityEvidenceClaim.model_validate(payload)


def _build_authoritative_reference_case(
    root: Path,
    *,
    persist_reference_plan: bool = True,
) -> dict[str, Any]:
    fixture = build_ridgeway_fixture()
    scenario = fixture.bundle.scenarios[0]
    focal_set = next(
        item
        for item in fixture.bundle.focal_area_sets
        if item.focal_set_id == scenario.focal_set_id
    )
    pairs_by_id = {item.pair_id: item for item in focal_set.pairs}
    pairs = [
        PreparedPair(
            pair_id=pair_id,
            from_focal_id=pairs_by_id[pair_id].from_focal_id,
            to_focal_id=pairs_by_id[pair_id].to_focal_id,
        )
        for pair_id in scenario.included_pair_ids
    ]
    reference_values = {
        scenario.included_pair_ids[0]: 100.0,
        scenario.included_pair_ids[1]: 200.0,
    }
    observed_values = {
        scenario.included_pair_ids[0]: 100.4,
        scenario.included_pair_ids[1]: 203.0,
    }
    reference_path = _write_matrix(
        root / "reference" / "approved-matrix.txt",
        pairs,
        reference_values,
    )
    observed_work_path = _write_matrix(
        root / "runs" / "direct-baseline" / "matrix.txt",
        pairs,
        observed_values,
    )
    asset_id = "reference_matrix_fixture"
    authorization = SensitiveDataAuthorization(
        asset_id=asset_id,
        ownership="Synthetic test owner",
        license_spdx_or_name="CC0-1.0",
        sensitivity=SensitivityClass.PUBLIC,
        cloud_disposition=CloudDisposition.RAW_ALLOWED,
        export_allowed=True,
        retention_policy="Retain with the synthetic test fixture.",
        spatial_redaction="No real locations are present.",
        approved_deployments=["LOCAL_ONLY"],
        reviewer="Test reviewer",
        reviewed_at=NOW,
    )
    matrix_asset = ConnectivityAsset(
        asset_id=asset_id,
        kind=AssetKind.REFERENCE_MATRIX,
        title="Approved synthetic reference matrix",
        relative_path="reference/approved-matrix.txt",
        sha256=_sha256(reference_path),
        byte_size=reference_path.stat().st_size,
        media_type="text/plain",
        source="Synthetic test evidence",
        vintage="2026",
        license="CC0-1.0",
        authorization=authorization,
    )
    binding = ReferenceScenarioBinding(
        binding_id="binding_fixture_reference",
        scenario_id=scenario.scenario_id,
        reference_matrix_asset_id=matrix_asset.asset_id,
        tolerance=ReferenceMetricTolerance(
            absolute_tolerance=0.5,
            relative_tolerance=0.01,
        ),
        reference_basis="Approved synthetic direct-run reference.",
        reviewed_by="Test reviewer",
        reviewed_at=NOW,
        limitations=["Synthetic reference evidence."],
    )
    bundle_payload = fixture.bundle.model_dump(mode="json")
    bundle_payload["assets"].append(matrix_asset.model_dump(mode="json"))
    bundle_payload["reference_bindings"] = [binding.model_dump(mode="json")]
    bundle = ConnectivityProjectBundle.model_validate(bundle_payload)

    store = EcoProjectStore(root)
    store.initialize(
        project_id=bundle.project_id,
        title=bundle.title,
        mode="GUIDED",
        skeleton=bundle.model_dump(mode="json"),
    )
    store.store_bundle(bundle)
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    run_plan = ConnectivityRunPlan(
        analysis_id="analysis_reference_fixture",
        revision_id=bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=1,
        solver_job_cap=1,
        search_method="ENUMERATION",
        seed=0,
        engine_manifest_sha256=ENGINE_SHA,
    )
    reference_plan = ReferenceComparisonPlan(
        plan_id="reference_plan_fixture",
        analysis_id=run_plan.analysis_id,
        revision_id=run_plan.revision_id,
        model_sha256=bundle.revision.model_sha256,
        engine_manifest_sha256=ENGINE_SHA,
        binding_ids=[binding.binding_id],
        bound_scenario_ids=[scenario.scenario_id],
        run_plan=run_plan,
        created_at=NOW,
    )
    store.create_analysis(run_plan, bundle=bundle)
    if persist_reference_plan:
        store.store_reference_comparison_plan(reference_plan)

    hypothesis = next(
        item
        for item in bundle.resistance_hypotheses
        if item.hypothesis_id == scenario.resistance_hypothesis_id
    )
    resistance_asset = next(
        item for item in bundle.assets if item.asset_id == hypothesis.resistance_asset_id
    )
    direct_run_id = "direct_reference_fixture"
    store.start_analysis_with_direct_jobs(
        run_plan.analysis_id,
        [
            {
                "direct_job_id": direct_run_id,
                "portfolio_id": baseline.portfolio_id,
                "scenario_id": scenario.scenario_id,
                "phase": "MATRIX",
                "sequence_number": 0,
                "emit_current_map": False,
            }
        ],
    )
    assert store.claim_next_direct_job(run_plan.analysis_id)["direct_job_id"] == direct_run_id

    input_resistance = (
        b"ncols 1\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 1\nNODATA_value -9999\n1\n"
    )
    input_focal = b"ncols 1\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 1\nNODATA_value -9999\n1\n"
    input_pairs = b"mode\tinclude\n" + b"".join(
        f"{item.from_focal_id}\t{item.to_focal_id}\n".encode() for item in pairs
    )
    prepared_resistance_sha256 = hashlib.sha256(input_resistance).hexdigest()
    diff_payload = (
        canonical_json(
            {
                "schema_version": "ecocorridor-semantic-raster-diff/v1.0",
                "project_id": bundle.project_id,
                "revision_id": bundle.revision.revision_id,
                "model_identity_sha256": bundle.revision.model_sha256,
                "portfolio_id": baseline.portfolio_id,
                "scenario_id": scenario.scenario_id,
                "baseline_asset_id": hypothesis.resistance_asset_id,
                "baseline_reviewed_content_sha256": resistance_asset.sha256,
                "prepared_resistance_sha256": prepared_resistance_sha256,
                "changes": [],
            }
        )
        + "\n"
    ).encode("utf-8")
    proof_document = create_professional_footprint_proof_document(
        project_id=bundle.project_id,
        revision_id=bundle.revision.revision_id,
        portfolio_id=baseline.portfolio_id,
        scenario_id=scenario.scenario_id,
        baseline=fixture.rasters[hypothesis.resistance_asset_id],
        proofs=[],
        assets=bundle.assets,
        source_paths={},
    )
    proof_payload = (canonical_json(proof_document.model_dump(mode="json")) + "\n").encode("utf-8")
    prepared = PreparedConnectivityRun(
        prepared_run_id=direct_run_id,
        analysis_id=run_plan.analysis_id,
        revision_id=run_plan.revision_id,
        portfolio_id=baseline.portfolio_id,
        scenario=scenario,
        pairs=pairs,
        resources=[
            PreparedResource(
                logical_name="resistance",
                source_path=root / "prepared-resistance.asc",
                sha256=prepared_resistance_sha256,
                target_filename="resistance.asc",
            ),
            PreparedResource(
                logical_name="focal",
                source_path=root / "prepared-focal.asc",
                sha256=hashlib.sha256(input_focal).hexdigest(),
                target_filename="focal.asc",
            ),
            PreparedResource(
                logical_name="included_pairs",
                source_path=root / "prepared-pairs.tsv",
                sha256=hashlib.sha256(input_pairs).hexdigest(),
                target_filename="included-pairs.tsv",
            ),
        ],
        action_raster_sha256=hashlib.sha256(diff_payload).hexdigest(),
        action_raster_diff_path=root / "action-raster-diff.json",
        prepared_resistance_sha256=prepared_resistance_sha256,
        footprint_proof_sha256=footprint_proof_set_sha256([]),
        engine_manifest_sha256=run_plan.engine_manifest_sha256,
        model_run_cache_key="c" * 64,
    )
    request_payload = (canonical_json(engine_request_payload(prepared)) + "\n").encode("utf-8")
    result_protocol_payload = (
        canonical_json(
            EngineProtocolResult(
                protocol_version="1.0",
                status="SUCCEEDED",
                engine_name="Circuitscape",
                circuitscape_version="5.17.1",
                pair_metrics=[
                    ProtocolPairMetric(
                        pair_id=item.pair_id,
                        from_focal_id=item.from_focal_id,
                        to_focal_id=item.to_focal_id,
                        effective_resistance=observed_values[item.pair_id],
                    )
                    for item in pairs
                ],
                matrix_path="outputs/connectivity_resistances_3columns.out",
                current_map_path=None,
            ).model_dump(mode="json")
        )
        + "\n"
    ).encode("utf-8")
    artifact_payloads = {
        "request": request_payload,
        "result_protocol": result_protocol_payload,
        "matrix": observed_work_path.read_bytes(),
        "circuitscape_configuration": effective_circuitscape_configuration_text(
            resource_paths={
                "resistance": "inputs/resistance.asc",
                "focal": "inputs/focal.asc",
                "included_pairs": "inputs/included-pairs.tsv",
            },
            output_directory="outputs",
            neighbor_rule=scenario.neighbor_rule.value,
            resistance_average=scenario.resistance_average.value,
            emit_current_map=False,
        ).encode("utf-8"),
        "action_raster_diff": diff_payload,
        "footprint_proof_document": proof_payload,
        "input_resistance": input_resistance,
        "input_focal": input_focal,
        "input_included_pairs": input_pairs,
    }
    cas = ContentAddressedStore(root / "blobs")
    artifact_records: list[dict[str, Any]] = []
    for role, payload in artifact_payloads.items():
        digest = cas.put_bytes(payload)
        artifact_records.append(
            {
                "role": role,
                "sha256": digest,
                "byte_size": len(payload),
                "cas_relative_path": cas.path_for(digest).relative_to(root).as_posix(),
                "media_type": (
                    "application/json"
                    if role in {"request", "result_protocol", "action_raster_diff"}
                    else "text/plain"
                ),
            }
        )
    artifacts_by_role = {item["role"]: item for item in artifact_records}
    observed_path = cas.path_for(artifacts_by_role["matrix"]["sha256"])
    store.set_direct_job_prepared(
        direct_run_id,
        prepared.model_dump(mode="json"),
        prepared.model_run_cache_key,
    )
    run = DirectConnectivityRun(
        direct_run_id=direct_run_id,
        analysis_id=run_plan.analysis_id,
        revision_id=run_plan.revision_id,
        portfolio_id=baseline.portfolio_id,
        scenario_id=scenario.scenario_id,
        state=DirectRunState.SUCCEEDED,
        exact=True,
        engine_name="Circuitscape",
        engine_manifest_sha256=ENGINE_SHA,
        model_run_cache_key="c" * 64,
        input_sha256=artifacts_by_role["request"]["sha256"],
        result_sha256=artifacts_by_role["result_protocol"]["sha256"],
        matrix_sha256=_sha256(observed_path),
        pair_metrics=[
            PairResistanceMetric(pair_id=pair_id, effective_resistance=observed_values[pair_id])
            for pair_id in scenario.included_pair_ids
        ],
        qa_status=QAStatus.ACCEPTED,
        completed_at=NOW,
    )
    store.finalize_direct_job(
        direct_run_id,
        "SUCCEEDED",
        run=run,
        artifacts=artifact_records,
        evidence_nodes=[
            {"node_id": "node-direct-run", "node_type": "DIRECT_RUN", "label": direct_run_id},
            {"node_id": "node-scenario", "node_type": "SCENARIO", "label": scenario.scenario_id},
            {
                "node_id": "node-raster-diff",
                "node_type": "RASTER_DIFF",
                "label": "Reviewed action raster diff",
                "sha256": artifacts_by_role["action_raster_diff"]["sha256"],
            },
            {"node_id": "node-asset", "node_type": "ASSET", "label": resistance_asset.asset_id},
            {
                "node_id": "node-engine",
                "node_type": "ENGINE_MANIFEST",
                "label": "Pinned Circuitscape engine manifest",
                "sha256": run_plan.engine_manifest_sha256,
            },
            {"node_id": "node-qa", "node_type": "QA", "label": QAStatus.ACCEPTED.value},
            {"node_id": "node-artifact", "node_type": "ARTIFACT", "label": "Matrix"},
            {
                "node_id": "node-footprint-proof",
                "node_type": "FOOTPRINT_PROOF",
                "label": "Reviewed footprint proof",
                "sha256": artifacts_by_role["footprint_proof_document"]["sha256"],
            },
        ],
    )
    comparison = compare_reference_matrix(
        comparison_id="comparison_reference_fixture",
        plan=reference_plan,
        binding=binding,
        scenario=scenario,
        expected_scenario_grid=resistance_asset.grid,
        prepared_pairs=pairs,
        observed_run=run,
        observed_matrix_path=observed_path,
        reference_matrix_asset=matrix_asset,
        reference_matrix_path=reference_path,
        reference_current_asset=None,
        completed_at=NOW,
    )
    links = [
        ReferenceEvidenceClaimLink(
            claim_id=f"claim_reference_{pair.pair_id}",
            comparison_id=comparison.comparison_id,
            scenario_id=scenario.scenario_id,
            pair_id=pair.pair_id,
            direct_run_id=run.direct_run_id,
        )
        for pair in comparison.pair_comparisons
    ]
    result = assemble_reference_comparison_result(
        result_id="reference_result_fixture",
        plan=reference_plan,
        scenario_comparisons=[comparison],
        evidence_claim_links=links,
        completed_at=NOW,
    )
    comparison_pairs = {item.pair_id: item for item in comparison.pair_comparisons}
    claims = [
        _reference_claim(
            link=link,
            comparison=comparison,
            pair_comparison=comparison_pairs[link.pair_id],
            run=run,
            scenario=scenario,
            matrix_asset=matrix_asset,
            baseline_portfolio_id=baseline.portfolio_id,
        )
        for link in links
    ]
    return {
        "store": store,
        "result": result,
        "reference_plan": reference_plan,
        "claims": claims,
        "reference_path": reference_path,
        "run": run,
    }


@pytest.fixture
def reference_case(tmp_path: Path):
    case = _build_authoritative_reference_case(tmp_path / "project")
    try:
        yield case
    finally:
        case["store"].close()


def test_schema_110_nonempty_reference_rows_fail_without_provenance_stamping(
    reference_case,
) -> None:
    store = reference_case["store"]
    root = store.root
    store.store_reference_comparison_result(
        reference_case["result"],
        claims=reference_case["claims"],
    )
    store.close()
    _downgrade_reference_tables_to_pre_parser_schema(root)

    with pytest.raises(RuntimeError, match="recreate the reference comparison"):
        EcoProjectStore(root)

    connection = sqlite3.connect(root / "project.sqlite")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 110
        for table in (
            "eco_reference_comparison_plans",
            "eco_reference_comparison_results",
        ):
            columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            assert "reference_matrix_parser_sha256" not in columns
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        connection.close()


def test_reference_result_roundtrip_and_byte_identical_replay(reference_case) -> None:
    store = reference_case["store"]
    result = reference_case["result"]
    claims = reference_case["claims"]

    first = store.store_reference_comparison_result(result, claims=claims)
    store.close()
    store = EcoProjectStore(store.root)
    reference_case["store"] = store
    replay = store.store_reference_comparison_result(result, claims=list(reversed(claims)))
    loaded_result, loaded_claims = store.reference_comparison_result(result.result_id)

    assert first["replayed"] is False
    expected_plan_sha256 = hashlib.sha256(
        canonical_json(reference_case["reference_plan"].model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    assert first["plan_sha256"] == expected_plan_sha256
    assert replay == {**first, "replayed": True}
    assert loaded_result == result
    assert {item.claim_id: item for item in loaded_claims} == {
        item.claim_id: item for item in claims
    }
    assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 111
    assert first["reference_matrix_parser_sha256"] == REFERENCE_MATRIX_PARSER_SHA256
    assert (
        store._connection.execute(
            "SELECT reference_matrix_parser_sha256 FROM eco_reference_comparison_plans"
        ).fetchone()[0]
        == REFERENCE_MATRIX_PARSER_SHA256
    )
    assert (
        store._connection.execute(
            "SELECT reference_matrix_parser_sha256 FROM eco_reference_comparison_results"
        ).fetchone()[0]
        == REFERENCE_MATRIX_PARSER_SHA256
    )
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_reference_comparison_results"
        ).fetchone()[0]
        == 1
    )
    assert store._connection.execute(
        "SELECT COUNT(*) FROM eco_reference_comparison_claims"
    ).fetchone()[0] == len(claims)
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_telemetry_outbox WHERE event_type='reference_comparison'"
        ).fetchone()[0]
        == 1
    )
    reference_event = store.telemetry_rows(event_type="reference_comparison")[0]
    assert reference_event["payload"]["plan_payload_sha256"] == expected_plan_sha256
    assert (
        reference_event["payload"]["reference_matrix_parser_sha256"]
        == REFERENCE_MATRIX_PARSER_SHA256
    )


def test_reference_plan_is_checksummed_idempotent_and_run_plan_bound(reference_case) -> None:
    store = reference_case["store"]
    plan = reference_case["reference_plan"]

    replay = store.store_reference_comparison_plan(plan)
    loaded_plan, loaded_sha256 = store.reference_comparison_plan(
        plan.plan_id,
        analysis_id=plan.analysis_id,
    )
    assert replay["replayed"] is True
    assert (
        replay["plan_sha256"]
        == hashlib.sha256(canonical_json(plan.model_dump(mode="json")).encode("utf-8")).hexdigest()
    )
    assert loaded_plan == plan
    assert loaded_sha256 == replay["plan_sha256"]
    assert (
        store._connection.execute("SELECT COUNT(*) FROM eco_reference_comparison_plans").fetchone()[
            0
        ]
        == 1
    )

    drifted_run_plan = plan.run_plan.model_copy(update={"seed": plan.run_plan.seed + 1})
    drifted = plan.model_copy(update={"run_plan": drifted_run_plan})
    with pytest.raises(ValueError, match="immutable analysis run plan"):
        store.store_reference_comparison_plan(drifted)

    detached_binding = plan.model_copy(update={"binding_ids": ["unknown-binding"]})
    with pytest.raises(ValueError, match="binding scope differs"):
        store.store_reference_comparison_plan(detached_binding)

    changed_payload = plan.model_copy(
        update={"created_at": datetime(2026, 8, 4, 13, 0, tzinfo=UTC)}
    )
    with pytest.raises(ValueError, match="identity was reused for different evidence"):
        store.store_reference_comparison_plan(changed_payload)


def test_reference_result_requires_plan_persisted_before_direct_work(tmp_path: Path) -> None:
    case = _build_authoritative_reference_case(
        tmp_path / "missing-plan",
        persist_reference_plan=False,
    )
    store = case["store"]
    try:
        with pytest.raises(ValueError, match="immutable stored plan"):
            store.store_reference_comparison_result(case["result"], claims=case["claims"])
        with pytest.raises(ValueError, match="after direct work exists"):
            store.store_reference_comparison_plan(case["reference_plan"])
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM eco_reference_comparison_plans"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.close()


def test_reference_plan_row_is_immutable_and_checksum_verified(reference_case) -> None:
    store = reference_case["store"]
    plan = reference_case["reference_plan"]
    with pytest.raises(sqlite3.IntegrityError, match="reference comparison plan is immutable"):
        store._connection.execute(
            "UPDATE eco_reference_comparison_plans SET payload_sha256=? WHERE plan_id=?",
            ("0" * 64, plan.plan_id),
        )

    store._connection.execute("DROP TRIGGER eco_reference_plan_immutable_update")
    store._connection.execute(
        "UPDATE eco_reference_comparison_plans SET payload_sha256=? WHERE plan_id=?",
        ("0" * 64, plan.plan_id),
    )
    with pytest.raises(ValueError, match="plan failed its immutable checksum"):
        store.store_reference_comparison_result(
            reference_case["result"],
            claims=reference_case["claims"],
        )


@pytest.mark.parametrize("row_kind", ["plan", "result"])
def test_reference_rows_reject_detached_parser_provenance(reference_case, row_kind: str) -> None:
    store = reference_case["store"]
    if row_kind == "plan":
        store._connection.execute("DROP TRIGGER eco_reference_plan_immutable_update")
        store._connection.execute(
            "UPDATE eco_reference_comparison_plans SET reference_matrix_parser_sha256=?",
            ("0" * 64,),
        )
        with pytest.raises(ValueError, match="row differs from its payload"):
            store.reference_comparison_plan(reference_case["reference_plan"].plan_id)
        return

    store.store_reference_comparison_result(
        reference_case["result"],
        claims=reference_case["claims"],
    )
    store._connection.execute("DROP TRIGGER eco_reference_result_immutable_update")
    store._connection.execute(
        "UPDATE eco_reference_comparison_results SET reference_matrix_parser_sha256=?",
        ("0" * 64,),
    )
    with pytest.raises(ValueError, match="row differs from its payload"):
        store.reference_comparison_result(reference_case["result"].result_id)


@pytest.mark.parametrize("job_tamper", ["missing", "not_succeeded"])
def test_reference_result_requires_matching_successful_durable_job(
    reference_case,
    job_tamper: str,
) -> None:
    store = reference_case["store"]
    run = reference_case["run"]
    if job_tamper == "missing":
        store._connection.execute("DROP TRIGGER eco_terminal_job_artifact_immutable_delete")
        store._connection.execute(
            "DELETE FROM eco_direct_jobs WHERE direct_job_id=?",
            (run.direct_run_id,),
        )
        expected = "no successful durable direct job"
    else:
        store._connection.execute("DROP TRIGGER eco_terminal_job_immutable")
        store._connection.execute(
            "UPDATE eco_direct_jobs SET state='FAILED' WHERE direct_job_id=?",
            (run.direct_run_id,),
        )
        expected = "differs from its terminal durable job"

    with pytest.raises(ValueError, match=expected):
        store.store_reference_comparison_result(
            reference_case["result"],
            claims=reference_case["claims"],
        )
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_reference_comparison_results"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("job_tamper", ["missing", "not_succeeded"])
def test_reference_result_readback_rechecks_successful_durable_job(
    reference_case,
    job_tamper: str,
) -> None:
    store = reference_case["store"]
    run = reference_case["run"]
    result = reference_case["result"]
    store.store_reference_comparison_result(result, claims=reference_case["claims"])
    if job_tamper == "missing":
        store._connection.execute("DROP TRIGGER eco_terminal_job_artifact_immutable_delete")
        store._connection.execute(
            "DELETE FROM eco_direct_jobs WHERE direct_job_id=?",
            (run.direct_run_id,),
        )
        expected = "no successful durable direct job"
    else:
        store._connection.execute("DROP TRIGGER eco_terminal_job_immutable")
        store._connection.execute(
            "UPDATE eco_direct_jobs SET state='FAILED' WHERE direct_job_id=?",
            (run.direct_run_id,),
        )
        expected = "differs from its terminal durable job"

    with pytest.raises(ValueError, match=expected):
        store.reference_comparison_result(result.result_id)


def _tamper_retained_qa_findings(
    store: EcoProjectStore,
    run: DirectConnectivityRun,
    target: str,
) -> None:
    forged_findings = ["Forged accepted warning."]
    if target == "qa_row":
        store._connection.execute("DROP TRIGGER eco_run_qa_immutable_update")
        store._connection.execute(
            "UPDATE eco_run_qa SET findings_json=? WHERE direct_run_id=?",
            (canonical_json(forged_findings), run.direct_run_id),
        )
        return
    if target == "run_payload":
        row = store._connection.execute(
            "SELECT payload_json FROM eco_analysis_runs WHERE direct_run_id=?",
            (run.direct_run_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        payload["qa_findings"] = forged_findings
        store._connection.execute("DROP TRIGGER eco_analysis_run_immutable_update")
        store._connection.execute(
            "UPDATE eco_analysis_runs SET payload_json=? WHERE direct_run_id=?",
            (canonical_json(payload), run.direct_run_id),
        )
        return
    raise AssertionError(f"unknown QA tamper target: {target}")


@pytest.mark.parametrize("target", ["qa_row", "run_payload"])
def test_reference_result_rejects_qa_findings_divergence(
    reference_case,
    target: str,
) -> None:
    store = reference_case["store"]
    _tamper_retained_qa_findings(store, reference_case["run"], target)

    with pytest.raises(ValueError, match="QA findings"):
        store.store_reference_comparison_result(
            reference_case["result"],
            claims=reference_case["claims"],
        )
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_reference_comparison_results"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("target", ["qa_row", "run_payload"])
def test_reference_result_readback_rejects_qa_findings_divergence(
    reference_case,
    target: str,
) -> None:
    store = reference_case["store"]
    result = reference_case["result"]
    store.store_reference_comparison_result(result, claims=reference_case["claims"])
    _tamper_retained_qa_findings(store, reference_case["run"], target)

    with pytest.raises(ValueError, match="QA findings"):
        store.reference_comparison_result(result.result_id)


def test_legacy_bulk_result_path_is_explicitly_unsupported(reference_case) -> None:
    with pytest.raises(RuntimeError, match="finalize every durable direct job"):
        reference_case["store"].store_results(
            reference_case["result"].analysis_id,
            runs=[],
            summaries=[],
            claims=[],
        )


def test_reference_result_replay_rejects_changed_claim_payload(reference_case) -> None:
    store = reference_case["store"]
    result = reference_case["result"]
    claims = reference_case["claims"]
    store.store_reference_comparison_result(result, claims=claims)
    changed_payload = claims[0].model_dump(mode="json")
    changed_payload["text"] = (
        "Direct Circuitscape result under documented assumptions. Reworded claim."
    )
    next(item for item in changed_payload["nodes"] if item["node_id"] == claims[0].claim_id)[
        "label"
    ] = changed_payload["text"]
    changed = ConnectivityEvidenceClaim.model_validate(changed_payload)

    with pytest.raises(ValueError, match="replay differs from stored claim evidence"):
        store.store_reference_comparison_result(result, claims=[changed, *claims[1:]])

    changed_result = result.model_copy(
        update={"limitations": [*result.limitations, "Additional replay-only limitation."]}
    )
    with pytest.raises(ValueError, match="identity was reused for different evidence"):
        store.store_reference_comparison_result(changed_result, claims=claims)


@pytest.mark.parametrize("case_name", ["missing", "unrelated", "swapped", "duplicate"])
def test_reference_result_rejects_detached_claim_sets(reference_case, case_name: str) -> None:
    store = reference_case["store"]
    result = reference_case["result"]
    claims = reference_case["claims"]
    if case_name == "missing":
        invalid = claims[:-1]
    elif case_name == "unrelated":
        payload = claims[0].model_dump(mode="json")
        payload["pair_id"] = "unrelated-pair"
        comparison_node = next(
            item for item in payload["nodes"] if item["node_type"] == "REFERENCE_COMPARISON"
        )
        comparison_node["attributes"]["pair_id"] = "unrelated-pair"
        invalid = [ConnectivityEvidenceClaim.model_validate(payload), *claims[1:]]
    elif case_name == "swapped":
        invalid = [
            _rename_claim(claims[0], claims[1].claim_id),
            _rename_claim(claims[1], claims[0].claim_id),
        ]
    else:
        invalid = [claims[0], claims[0]]

    with pytest.raises(ValueError):
        store.store_reference_comparison_result(result, claims=invalid)
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_reference_comparison_results"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "case_name",
    [
        "qa_label",
        "missing_assumptions",
        "authorization_identity",
        "root_label",
        "edge_relation",
        "reversed_edge",
        "duplicate_edge",
        "extra_node",
        "disconnected_node",
        "shortcut_star",
    ],
)
def test_reference_result_rejects_noncanonical_claim_graphs(
    reference_case,
    case_name: str,
) -> None:
    store = reference_case["store"]
    claims = reference_case["claims"]
    payload = deepcopy(claims[0].model_dump(mode="json"))
    if case_name == "qa_label":
        next(item for item in payload["nodes"] if item["node_type"] == "QA")["label"] = "FAILED"
    elif case_name == "missing_assumptions":
        assumption_ids = {
            item["node_id"] for item in payload["nodes"] if item["node_type"] == "ASSUMPTION"
        }
        payload["nodes"] = [
            item for item in payload["nodes"] if item["node_id"] not in assumption_ids
        ]
        payload["edges"] = [
            item
            for item in payload["edges"]
            if item["from_node_id"] not in assumption_ids
            and item["to_node_id"] not in assumption_ids
        ]
    elif case_name == "authorization_identity":
        authorization = next(
            item for item in payload["nodes"] if item["node_type"] == "AUTHORIZATION"
        )
        old_id = authorization["node_id"]
        authorization["node_id"] = "authorization:detached"
        for edge in payload["edges"]:
            if edge["to_node_id"] == old_id:
                edge["to_node_id"] = authorization["node_id"]
    elif case_name == "root_label":
        next(item for item in payload["nodes"] if item["node_type"] == "CLAIM")["label"] = (
            "Detached label"
        )
    elif case_name == "edge_relation":
        payload["edges"][0]["relation"] = "unreviewed_shortcut"
    elif case_name == "reversed_edge":
        edge = payload["edges"][0]
        edge["from_node_id"], edge["to_node_id"] = (
            edge["to_node_id"],
            edge["from_node_id"],
        )
    elif case_name == "duplicate_edge":
        payload["edges"].append(deepcopy(payload["edges"][0]))
    elif case_name in {"extra_node", "disconnected_node"}:
        payload["nodes"].append(
            EvidenceNode(
                node_id="assumption:unrelated",
                node_type="ASSUMPTION",
                label="Unrelated assumption",
            ).model_dump(mode="json")
        )
        if case_name == "extra_node":
            scenario_id = next(
                item["node_id"] for item in payload["nodes"] if item["node_type"] == "SCENARIO"
            )
            payload["edges"].append(
                EvidenceEdge(
                    from_node_id=scenario_id,
                    to_node_id="assumption:unrelated",
                    relation="assumes",
                ).model_dump(mode="json")
            )
    else:
        root_node_id = payload["root_node_id"]
        payload["edges"] = [
            EvidenceEdge(
                from_node_id=root_node_id,
                to_node_id=node["node_id"],
                relation="traces_to",
            ).model_dump(mode="json")
            for node in payload["nodes"]
            if node["node_id"] != root_node_id
        ]
    changed_claim = ConnectivityEvidenceClaim.model_validate(payload)

    with pytest.raises(ValueError):
        store.store_reference_comparison_result(
            reference_case["result"],
            claims=[changed_claim, *claims[1:]],
        )
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_reference_comparison_results"
        ).fetchone()[0]
        == 0
    )


def test_reference_result_rejects_removed_reviewed_binding_limitation(reference_case) -> None:
    store = reference_case["store"]
    result = reference_case["result"]
    reviewed_limitation = "Synthetic reference evidence."
    comparison = result.scenario_comparisons[0]
    stripped_comparison = type(comparison).model_validate(
        {
            **comparison.model_dump(mode="json"),
            "limitations": [item for item in comparison.limitations if item != reviewed_limitation],
        }
    )
    stripped_result = type(result).model_validate(
        {
            **result.model_dump(mode="json"),
            "scenario_comparisons": [stripped_comparison.model_dump(mode="json")],
        }
    )
    stripped_claims = [
        _claim_without_limitation(
            claim,
            reviewed_limitation,
            comparison=stripped_comparison,
        )
        for claim in reference_case["claims"]
    ]

    with pytest.raises(ValueError, match="canonical result of retained matrix bytes"):
        store.store_reference_comparison_result(stripped_result, claims=stripped_claims)
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_reference_comparison_results"
        ).fetchone()[0]
        == 0
    )


def test_reference_claim_rejects_removed_reviewed_binding_limitation(reference_case) -> None:
    store = reference_case["store"]
    reviewed_limitation = "Synthetic reference evidence."
    stripped_claims = [
        _claim_without_limitation(claim, reviewed_limitation) for claim in reference_case["claims"]
    ]

    with pytest.raises(ValueError, match="limitations differ from its reviewed comparison"):
        store.store_reference_comparison_result(
            reference_case["result"],
            claims=stripped_claims,
        )
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_reference_comparison_claims"
        ).fetchone()[0]
        == 0
    )


def test_reference_result_rejects_bypassed_result_identity_and_tampered_bytes(
    reference_case,
) -> None:
    store = reference_case["store"]
    result = reference_case["result"]
    claims = reference_case["claims"]
    comparison = result.scenario_comparisons[0].model_copy(update={"model_sha256": "f" * 64})
    detached = result.model_copy(
        update={
            "model_sha256": "f" * 64,
            "scenario_comparisons": [comparison],
        }
    )
    with pytest.raises(ValueError, match="analysis, revision, model, or engine"):
        store.store_reference_comparison_result(detached, claims=claims)

    bypassed_empty_result = result.model_copy(update={"scenario_comparisons": []})
    with pytest.raises(ValueError, match="at least 1 item"):
        store.store_reference_comparison_result(bypassed_empty_result, claims=claims)

    reference_case["reference_path"].write_text("1 2 999\n", encoding="utf-8")
    with pytest.raises(ValueError, match="retained evidence bytes failed their checksum"):
        store.store_reference_comparison_result(result, claims=claims)
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_reference_comparison_results"
        ).fetchone()[0]
        == 0
    )


def test_reference_result_rejects_sub_tolerance_forgery_and_noncanonical_order(
    reference_case,
) -> None:
    store = reference_case["store"]
    result = reference_case["result"]
    comparison = result.scenario_comparisons[0]
    pair = comparison.pair_comparisons[0]
    forged_observed = pair.observed_effective_resistance + 5e-14
    forged_absolute = abs(forged_observed - pair.reference_effective_resistance)
    forged_pair = ReferencePairComparison(
        pair_id=pair.pair_id,
        reference_effective_resistance=pair.reference_effective_resistance,
        observed_effective_resistance=forged_observed,
        tolerance=pair.tolerance,
        absolute_difference=forged_absolute,
        relative_difference=forged_absolute / abs(pair.reference_effective_resistance),
        allowed_difference=pair.allowed_difference,
        within_tolerance=forged_absolute <= pair.allowed_difference,
    )
    forged_comparison = type(comparison).model_validate(
        {
            **comparison.model_dump(mode="json"),
            "pair_comparisons": [
                forged_pair.model_dump(mode="json"),
                *[item.model_dump(mode="json") for item in comparison.pair_comparisons[1:]],
            ],
        }
    )
    forged_result = type(result).model_validate(
        {
            **result.model_dump(mode="json"),
            "scenario_comparisons": [forged_comparison.model_dump(mode="json")],
        }
    )
    with pytest.raises(ValueError, match="canonical result of retained matrix bytes"):
        store.store_reference_comparison_result(
            forged_result,
            claims=reference_case["claims"],
        )

    reversed_comparison = type(comparison).model_validate(
        {
            **comparison.model_dump(mode="json"),
            "pair_comparisons": [
                item.model_dump(mode="json") for item in reversed(comparison.pair_comparisons)
            ],
        }
    )
    reversed_result = type(result).model_validate(
        {
            **result.model_dump(mode="json"),
            "scenario_comparisons": [reversed_comparison.model_dump(mode="json")],
            "evidence_claim_links": [
                item.model_dump(mode="json") for item in reversed(result.evidence_claim_links)
            ],
        }
    )
    with pytest.raises(ValueError, match="canonical result of retained matrix bytes"):
        store.store_reference_comparison_result(
            reversed_result,
            claims=reference_case["claims"],
        )


def test_reference_result_rolls_back_result_claims_and_outbox_on_late_failure(
    reference_case,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = reference_case["store"]

    def fail_outbox(*_args, **_kwargs):
        raise RuntimeError("simulated outbox failure")

    monkeypatch.setattr(store, "_enqueue_outbox", fail_outbox)
    with pytest.raises(RuntimeError, match="simulated outbox failure"):
        store.store_reference_comparison_result(
            reference_case["result"],
            claims=reference_case["claims"],
        )
    for table in (
        "eco_reference_comparison_results",
        "eco_reference_comparison_claims",
    ):
        assert store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert (
        store._connection.execute(
            "SELECT COUNT(*) FROM eco_telemetry_outbox WHERE event_type='reference_comparison'"
        ).fetchone()[0]
        == 0
    )


def _reference_sync_plan(store: EcoProjectStore, analysis_id: str) -> dict[str, Any]:
    bundle = store.analysis_bundle(analysis_id)
    return {
        "allowed": True,
        "blockers": [],
        "projections": [
            {
                "source_asset_id": asset.asset_id,
                "uploaded_asset_id": asset.asset_id,
                "representation": "RAW",
                "bytes": asset.byte_size,
            }
            for asset in bundle.assets
        ],
    }


def _store_reference_with_synced_inputs(reference_case) -> tuple[dict[str, Any], dict[str, Any]]:
    store = reference_case["store"]
    result = reference_case["result"]
    run = reference_case["run"]
    with store.transaction() as connection:
        store._enqueue_outbox(
            connection,
            project_id=store.project()["project_id"],
            analysis_id=result.analysis_id,
            event_type="direct_job_terminal",
            subject_id=run.direct_run_id,
            stream_id=f"connectivity_analysis:{result.analysis_id}",
            payload={
                "direct_job_id": run.direct_run_id,
                "state": "SUCCEEDED",
                "cached": False,
                "result_sha256": run.result_sha256,
            },
            idempotency_key=f"test-direct-job:{run.direct_run_id}",
        )
    claimed = store.claim_outbox(limit=20)
    for item in claimed:
        artifact_name = {
            "bundle_import": "connectivity-project",
            "analysis_plan": "analysis-project",
            "direct_job_terminal": "direct-matrix",
        }[item["event_type"]]
        store.mark_outbox_synced(
            item["outbox_id"],
            {
                "run_id": f"run-{item['event_type']}",
                "envelope_id": item["outbox_id"],
                "artifacts": [
                    {
                        "provider_artifact_ref": (
                            f"wildlife-team/restricted-connectivity/{artifact_name}:v1"
                        )
                    }
                ],
            },
            lease_token=item["lease_token"],
        )
    receipt = store.store_reference_comparison_result(
        result,
        claims=reference_case["claims"],
    )
    row = store.telemetry_rows(
        event_type="reference_comparison",
        analysis_id=result.analysis_id,
        subject_id=result.result_id,
    )[0]
    assert row["outbox_id"] == receipt["outbox_id"]
    return row, _reference_sync_plan(store, result.analysis_id)


def test_reference_telemetry_projection_is_deterministic_and_path_safe(reference_case) -> None:
    store = reference_case["store"]
    result = reference_case["result"]
    row, sync_plan = _store_reference_with_synced_inputs(reference_case)
    projector = EcoTelemetryProjector(
        store=store,
        project_root=store.root,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
    )

    first = projector(row, row["payload"])
    second = projector(row, row["payload"])

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.run_kind == "connectivity_analysis"
    assert first.run_subject_id == f"{result.analysis_id}:reference-comparison"
    assert first.event_type == "reference_comparison"
    assert first.event_sequence == 1
    assert first.summary["reference_status"] == result.status
    assert first.summary["reference_matrix_parser_sha256"] == REFERENCE_MATRIX_PARSER_SHA256
    assert first.summary["model_fitness_attested_by_reference_match"] is False
    assert first.summary["current_raster_numeric_comparison_performed"] is False
    assert first.input_artifact_refs == [
        "wildlife-team/restricted-connectivity/analysis-project:v1",
        "wildlife-team/restricted-connectivity/direct-matrix:v1",
    ]

    tables = {item.logical_name: item for item in first.tables}
    assert set(tables) == {
        "reference_plan",
        "reference_comparisons",
        "reference_pair_comparisons",
        "reference_evidence_claims",
    }
    expected_plan_sha256 = hashlib.sha256(
        canonical_json(reference_case["reference_plan"].model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    assert first.config["reference_plan_payload_sha256"] == expected_plan_sha256
    assert first.config["reference_matrix_parser_sha256"] == REFERENCE_MATRIX_PARSER_SHA256
    plan_table = tables["reference_plan"]
    plan_record = dict(zip(plan_table.columns, plan_table.rows[0], strict=True))
    assert plan_record["plan_id"] == reference_case["reference_plan"].plan_id
    assert plan_record["payload_sha256"] == expected_plan_sha256
    assert plan_record["reference_matrix_parser_sha256"] == REFERENCE_MATRIX_PARSER_SHA256
    assert plan_record["baseline_portfolio_id"] == (
        reference_case["reference_plan"].run_plan.baseline_portfolio_id
    )
    pair_table = tables["reference_pair_comparisons"]
    pair_records = [
        dict(zip(pair_table.columns, values, strict=True)) for values in pair_table.rows
    ]
    expected_pairs = result.scenario_comparisons[0].pair_comparisons
    assert [item["pair_id"] for item in pair_records] == sorted(
        item.pair_id for item in expected_pairs
    )
    assert [item["within_tolerance"] for item in pair_records] == [True, False]
    assert all(item["evidence_claim_id"] for item in pair_records)

    assert len(first.artifacts) == 1
    artifact = first.artifacts[0]
    assert artifact.artifact_type == "decision_evidence"
    assert artifact.logical_name == "reference/reference-comparison.json"
    assert artifact.source_relative_path.startswith(
        ".scenario-cloud/ecocorridor/projections/reference-comparison/"
    )
    assert ".." not in Path(artifact.source_relative_path).parts
    artifact_path = (store.root / artifact.source_relative_path).resolve()
    assert artifact_path.is_relative_to(store.root.resolve())
    assert artifact_path.is_file() and not artifact_path.is_symlink()
    assert _sha256(artifact_path) == artifact.sha256
    assert str(store.root) not in artifact_path.read_text(encoding="utf-8")
    snapshot = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert snapshot["reference_matrix_parser_sha256"] == REFERENCE_MATRIX_PARSER_SHA256
    assert snapshot["plan_payload_sha256"] == expected_plan_sha256
    assert snapshot["plan"] == reference_case["reference_plan"].model_dump(mode="json")
    assert snapshot["result"] == result.model_dump(mode="json")
    assert [item["claim_id"] for item in snapshot["claims"]] == sorted(
        item.claim_id for item in reference_case["claims"]
    )


@pytest.mark.parametrize(
    "case_name",
    [
        "unexpected_local_path",
        "plan_hash",
        "parser_hash",
        "result_hash",
        "claim_set",
        "direct_run_set",
        "schema_version",
        "stream_id",
        "stream_sequence",
    ],
)
def test_reference_telemetry_rejects_malformed_or_detached_outbox_payloads(
    reference_case,
    case_name: str,
) -> None:
    store = reference_case["store"]
    row, sync_plan = _store_reference_with_synced_inputs(reference_case)
    projector = EcoTelemetryProjector(
        store=store,
        project_root=store.root,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
    )
    tampered_row = deepcopy(row)
    tampered_payload = deepcopy(row["payload"])
    if case_name == "unexpected_local_path":
        tampered_payload["local_path"] = "/Users/example/secret-reference.tif"
    elif case_name == "plan_hash":
        tampered_payload["plan_payload_sha256"] = "0" * 64
    elif case_name == "parser_hash":
        tampered_payload["reference_matrix_parser_sha256"] = "0" * 64
    elif case_name == "result_hash":
        tampered_payload["result_payload_sha256"] = "0" * 64
    elif case_name == "claim_set":
        tampered_payload["claim_ids"] = tampered_payload["claim_ids"][:-1]
    elif case_name == "direct_run_set":
        tampered_payload["direct_run_ids"] = ["detached-direct-run"]
    elif case_name == "schema_version":
        tampered_payload["schema_version"] = "ecocorridor/v999"
    elif case_name == "stream_id":
        tampered_row["stream_id"] = "connectivity_analysis:detached"
    else:
        tampered_row["stream_sequence"] = 0

    with pytest.raises(CloudIntegrityError):
        projector(tampered_row, tampered_payload)


def test_reference_telemetry_detects_conflicting_staged_projection(reference_case) -> None:
    store = reference_case["store"]
    row, sync_plan = _store_reference_with_synced_inputs(reference_case)
    projector = EcoTelemetryProjector(
        store=store,
        project_root=store.root,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
    )
    projection = projector(row, row["payload"])
    staged_path = store.root / projection.artifacts[0].source_relative_path
    staged_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(CloudIntegrityError, match="ECO_STAGED_PROJECTION_CONFLICT"):
        projector(row, row["payload"])


@pytest.mark.parametrize("row_kind", ["plan", "result"])
def test_reference_telemetry_rejects_database_parser_identity_drift(
    reference_case,
    row_kind: str,
) -> None:
    store = reference_case["store"]
    row, sync_plan = _store_reference_with_synced_inputs(reference_case)
    if row_kind == "plan":
        store._connection.execute("DROP TRIGGER eco_reference_plan_immutable_update")
        table = "eco_reference_comparison_plans"
    else:
        store._connection.execute("DROP TRIGGER eco_reference_result_immutable_update")
        table = "eco_reference_comparison_results"
    store._connection.execute(
        f"UPDATE {table} SET reference_matrix_parser_sha256=?",
        ("0" * 64,),
    )
    projector = EcoTelemetryProjector(
        store=store,
        project_root=store.root,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
    )

    with pytest.raises(
        CloudIntegrityError,
        match="ECO_REFERENCE_COMPARISON_EVIDENCE_INVALID",
    ):
        projector(row, row["payload"])


@pytest.mark.parametrize("tamper_kind", ["metadata", "run_plan"])
def test_reference_telemetry_rejects_tampered_stored_plan_lineage(
    reference_case,
    tamper_kind: str,
) -> None:
    store = reference_case["store"]
    row, sync_plan = _store_reference_with_synced_inputs(reference_case)
    plan = reference_case["reference_plan"]
    if tamper_kind == "metadata":
        changed_plan = plan.model_copy(
            update={"created_at": datetime(2026, 8, 4, 13, 0, tzinfo=UTC)}
        )
        expected_error = "ECO_REFERENCE_COMPARISON_OUTBOX_MISMATCH"
    else:
        changed_plan = plan.model_copy(
            update={"run_plan": plan.run_plan.model_copy(update={"seed": plan.run_plan.seed + 1})}
        )
        expected_error = "ECO_REFERENCE_COMPARISON_EVIDENCE_INVALID"
    changed_json = canonical_json(changed_plan.model_dump(mode="json"))
    changed_sha256 = hashlib.sha256(changed_json.encode("utf-8")).hexdigest()
    store._connection.execute("DROP TRIGGER eco_reference_plan_immutable_update")
    store._connection.execute(
        "UPDATE eco_reference_comparison_plans SET payload_json=?,payload_sha256=? WHERE plan_id=?",
        (changed_json, changed_sha256, plan.plan_id),
    )
    projector = EcoTelemetryProjector(
        store=store,
        project_root=store.root,
        sync_plan=sync_plan,
        profile=ECOCORRIDOR_APP_PROFILE,
    )

    with pytest.raises(CloudIntegrityError, match=expected_error):
        projector(row, row["payload"])


def test_reference_telemetry_requires_synced_analysis_and_direct_run_lineage(
    reference_case,
) -> None:
    store = reference_case["store"]
    result = reference_case["result"]
    store.store_reference_comparison_result(result, claims=reference_case["claims"])
    row = store.telemetry_rows(
        event_type="reference_comparison",
        analysis_id=result.analysis_id,
        subject_id=result.result_id,
    )[0]
    projector = EcoTelemetryProjector(
        store=store,
        project_root=store.root,
        sync_plan=_reference_sync_plan(store, result.analysis_id),
        profile=ECOCORRIDOR_APP_PROFILE,
    )

    with pytest.raises(
        CloudTransportError,
        match="ECO_REQUIRED_ARTIFACT_LINEAGE_NOT_SYNCED",
    ):
        projector(row, row["payload"])
