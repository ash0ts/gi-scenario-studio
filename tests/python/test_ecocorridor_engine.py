from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ecocorridor_core.contracts import (
    EngineManifest,
    EngineMode,
    NeighborRule,
    RasterSemantics,
    ResistanceAverage,
)
from ecocorridor_core.engine import (
    EngineManifestGateError,
    EngineProtocolError,
    PreparedPair,
    TestOnlyCircuitEngine,
    UnsupportedConnectivityFeature,
    assert_supported_settings,
    build_engine_manifest,
    contained_regular_file,
    effective_circuitscape_configuration_text,
    parse_circuitscape_matrix,
    validate_effective_circuitscape_configuration,
)


def test_hand_calculated_series_and_parallel_are_quarantined_test_results() -> None:
    series = TestOnlyCircuitEngine.solve(
        [(1, 2, 2.0), (2, 3, 3.0)],
        {"pair_1_3": (1, 3)},
        allow_test_engine=True,
    )
    parallel = TestOnlyCircuitEngine.solve(
        [(1, 2, 2.0), (1, 2, 2.0)],
        {"pair_1_2": (1, 2)},
        allow_test_engine=True,
    )
    assert series.pair_results[0].effective_resistance == pytest.approx(5.0)
    assert parallel.pair_results[0].effective_resistance == pytest.approx(1.0)
    assert series.test_only is True
    assert series.engine_name != "Circuitscape"
    assert "Cannot be used" in series.limitations[1]


def test_test_engine_requires_explicit_opt_in() -> None:
    with pytest.raises(PermissionError, match="test-only"):
        TestOnlyCircuitEngine.solve([(1, 2, 1.0)], {"p": (1, 2)})


def test_engine_settings_reject_every_unsupported_mode() -> None:
    assert_supported_settings(EngineMode.RASTER_PAIRWISE, RasterSemantics.RESISTANCE)
    with pytest.raises(UnsupportedConnectivityFeature, match="pairwise"):
        assert_supported_settings(EngineMode.NETWORK_PAIRWISE, RasterSemantics.RESISTANCE)
    with pytest.raises(UnsupportedConnectivityFeature, match="resistance"):
        assert_supported_settings(EngineMode.RASTER_PAIRWISE, RasterSemantics.CONDUCTANCE)


def test_release_manifest_cannot_be_fabricated_without_julia_manifest(tmp_path: Path) -> None:
    project = tmp_path / "julia"
    project.mkdir()
    placeholder = tmp_path / "placeholder"
    placeholder.write_text("real input", encoding="utf-8")
    with pytest.raises(EngineManifestGateError, match="Manifest.toml is absent"):
        build_engine_manifest(
            manifest_id="manifest",
            executable=placeholder,
            julia_runtime=placeholder,
            julia_project_directory=project,
            native_artifacts=placeholder,
            wrapper=placeholder,
            parser=placeholder,
            metric_definitions=placeholder,
            neighbor_rule="EIGHT",
            resistance_average="ARITHMETIC",
            platform="test",
        )


def test_engine_identity_excludes_packaging_timestamp() -> None:
    created_at = datetime(2026, 8, 3, tzinfo=UTC)
    payload = {
        "manifest_id": "manifest",
        "executable_sha256": "0" * 64,
        "julia_runtime_sha256": "a" * 64,
        "julia_manifest_sha256": "b" * 64,
        "native_artifacts_sha256": "c" * 64,
        "wrapper_sha256": "d" * 64,
        "parser_sha256": "e" * 64,
        "metric_definitions_sha256": "f" * 64,
        "neighbor_rule": NeighborRule.EIGHT,
        "resistance_average": ResistanceAverage.ARITHMETIC,
        "platform": "test",
    }
    first = EngineManifest(**payload, created_at=created_at)
    second = EngineManifest(**payload, created_at=created_at + timedelta(hours=1))
    assert first.created_at != second.created_at
    assert first.sha256 == second.sha256


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("1 2 -1\n", "no positive result"),
        ("1 2 4\n1 2 4\n", "duplicate"),
        ("1.5 2 4\n", "invalid focal IDs"),
        ("1 2 nan\n", "nonfinite"),
        ("1 2\n", "exactly three columns"),
        ("1 3 7\n", "unexpected positive"),
        ("", "omits requested pair"),
    ],
)
def test_matrix_parser_fails_closed_on_ambiguous_or_unrequested_rows(
    tmp_path: Path, payload: str, message: str
) -> None:
    path = tmp_path / "matrix.out"
    path.write_text(payload, encoding="utf-8")
    pairs = [PreparedPair(pair_id="pair-1-2", from_focal_id=1, to_focal_id=2)]
    with pytest.raises(EngineProtocolError, match=message):
        parse_circuitscape_matrix(path, pairs)


def test_matrix_parser_rederives_only_requested_positive_values(tmp_path: Path) -> None:
    path = tmp_path / "matrix.out"
    path.write_text("1 2 4.25\n1 3 -1\n2 3 -1\n", encoding="utf-8")
    parsed = parse_circuitscape_matrix(
        path, [PreparedPair(pair_id="pair-1-2", from_focal_id=1, to_focal_id=2)]
    )
    assert [(item.pair_id, item.effective_resistance) for item in parsed] == [("pair-1-2", 4.25)]


def test_effective_configuration_is_byte_and_section_exact(tmp_path: Path) -> None:
    arguments = {
        "resource_paths": {
            "resistance": "inputs/resistance.asc",
            "focal": "inputs/focal.asc",
            "included_pairs": "inputs/pairs.tsv",
        },
        "output_directory": "outputs",
        "neighbor_rule": "EIGHT",
        "resistance_average": "ARITHMETIC",
        "emit_current_map": True,
    }
    path = tmp_path / "circuitscape.ini"
    canonical = effective_circuitscape_configuration_text(**arguments)
    path.write_text(canonical, encoding="utf-8")
    validate_effective_circuitscape_configuration(path, **arguments)

    # All requested key/value text remains visible, but the output keys now sit
    # under the wrong section. A section-blind validator would accept this.
    path.write_text(
        canonical.replace("[Output options]", "[Calculation options]", 1),
        encoding="utf-8",
    )
    with pytest.raises(EngineProtocolError, match="differs from request"):
        validate_effective_circuitscape_configuration(path, **arguments)


def test_contained_artifact_rejects_symlink_in_any_component(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "matrix.out").write_text("1 2 1\n", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(EngineProtocolError, match="symlinked"):
        contained_regular_file(root, "linked/matrix.out")
