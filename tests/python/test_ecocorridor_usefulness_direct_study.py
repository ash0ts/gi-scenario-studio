from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core import usefulness_direct_study as direct_study
from ecocorridor_core.contracts import EngineManifest, canonical_sha256
from ecocorridor_core.engine import sha256_file
from ecocorridor_core.usefulness_direct_study import (
    SourceRepositoryIdentity,
    UsefulnessDirectStudyError,
    verify_complete_usefulness_ensemble,
)
from ecocorridor_core.usefulness_direct_study import (
    run_usefulness_direct_study as _run_usefulness_direct_study,
)
from ecocorridor_core.usefulness_landscapes import materialize_usefulness_landscapes

ROOT = Path(__file__).resolve().parents[2]
FAKE_SOURCE = SourceRepositoryIdentity(
    source_repository="https://github.com/ash0ts/gi-scenario-studio",
    source_commit="a" * 40,
    source_tree_clean=True,
)


def run_usefulness_direct_study(**kwargs):
    """Unit fake engines are always explicit, non-publishable test evidence."""

    kwargs.setdefault("test_only_engine", True)
    return _run_usefulness_direct_study(**kwargs)


@pytest.fixture(scope="module")
def materialized(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("usefulness-materialized") / "inputs"
    materialize_usefulness_landscapes(target)
    return target


def _fake_engine(
    path: Path,
    *,
    corrupt_current: bool = False,
    zero_current: bool = False,
    zero_outside_source_mask: bool = True,
) -> str:
    python_source = textwrap.dedent(
        f"""\
        import argparse
        import hashlib
        import json
        from pathlib import Path

        parser = argparse.ArgumentParser()
        parser.add_argument("--request", required=True)
        parser.add_argument("--result", required=True)
        arguments = parser.parse_args()
        request = json.loads(Path(arguments.request).read_text(encoding="utf-8"))
        output = Path(request["output_directory"])
        output.mkdir(parents=True, exist_ok=True)
        resources = request["resources"]
        settings = request["settings"]
        current = "True" if settings["emit_current_map"] else "False"
        four = "True" if settings["neighbor_rule"] == "FOUR" else "False"
        arithmetic = (
            "True" if settings["resistance_average"] == "ARITHMETIC" else "False"
        )
        lines = [
            "[Options for advanced mode]",
            "ground_file_is_resistances = True",
            "source_file = (Browse for a valid file)",
            "ground_file = (Browse for a valid file)",
            "use_unit_currents = False",
            "use_direct_grounds = False",
            "remove_src_or_gnd = keepall",
            "[Mask file]",
            "use_mask = False",
            "mask_file = (Browse for a valid file)",
            "[Calculation options]",
            "low_memory_mode = False",
            "parallelize = False",
            "max_parallel = 1",
            "solver = cg+amg",
            "print_timings = False",
            "preemptive_memory_release = True",
            "print_rusages = False",
            "scenario = pairwise",
            "[Short circuit regions (aka polygons)]",
            "use_polygons = False",
            "polygon_file = (Browse for a valid file)",
            "[Options for one-to-all and all-to-one modes]",
            "use_variable_source_strengths = False",
            "variable_source_file = (Browse for a valid file)",
            "[Output options]",
            "set_null_currents_to_nodata = False",
            "set_null_voltages_to_nodata = False",
            "compress_grids = True",
            f"write_cur_maps = {{current}}",
            f"write_cum_cur_map_only = {{current}}",
            "log_transform_maps = False",
            "write_volt_maps = False",
            f"output_file = {{request['output_directory'].rstrip('/')}}/connectivity",
            "[Options for pairwise and one-to-all and all-to-one modes]",
            f"included_pairs_file = {{resources['included_pairs']}}",
            "use_included_pairs = True",
            f"point_file = {{resources['focal']}}",
            "[Connection scheme for raster habitat data]",
            f"connect_four_neighbors_only = {{four}}",
            f"connect_using_avg_resistances = {{arithmetic}}",
            "[Habitat raster or graph]",
            "habitat_map_is_resistances = True",
            f"habitat_file = {{resources['resistance']}}",
        ]
        Path("circuitscape.ini").write_text("\\n".join(lines) + "\\n", encoding="utf-8")

        matrix_relative = "outputs/connectivity_resistances_3columns.out"
        current_relative = "outputs/connectivity_cum_curmap.asc"
        metrics = []
        matrix_rows = []
        for pair in request["pairs"]:
            digest = hashlib.sha256(
                f"{{request['prepared_run_id']}}:{{pair['pair_id']}}".encode("utf-8")
            ).hexdigest()
            value = round(1.0 + int(digest[:8], 16) / 100000000.0, 6)
            metrics.append(
                {{
                    "pair_id": pair["pair_id"],
                    "from_focal_id": pair["from_focal_id"],
                    "to_focal_id": pair["to_focal_id"],
                    "effective_resistance": value,
                }}
            )
            matrix_rows.append(
                f"{{pair['from_focal_id']}} {{pair['to_focal_id']}} {{value:.6f}}"
            )
        Path(matrix_relative).write_text("\\n".join(matrix_rows) + "\\n", encoding="utf-8")

        resistance = Path(resources["resistance"]).read_text(encoding="utf-8").splitlines()
        current_rows = resistance[:6]
        for row in resistance[6:]:
            values = []
            for value in row.split():
                numeric = float(value)
                if numeric == -9999:
                    values.append("0" if {zero_outside_source_mask!r} else "-9999")
                else:
                    values.append(
                        "-1"
                        if {corrupt_current!r}
                        else ("0" if {zero_current!r} else "0.5")
                    )
            current_rows.append(" ".join(values))
        Path(current_relative).write_text(
            "\\n".join(current_rows) + "\\n", encoding="utf-8"
        )
        result = {{
            "protocol_version": "1.0",
            "status": "SUCCEEDED",
            "engine_name": "Circuitscape",
            "circuitscape_version": "5.17.1",
            "pair_metrics": metrics,
            "matrix_path": matrix_relative,
            "current_map_path": current_relative,
            "review_mask_summaries": [],
            "warnings": [],
            "failure_message": None,
        }}
        Path(arguments.result).write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\\n",
            encoding="utf-8",
        )
        """
    )
    source = (
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} - "$@" <<\'PY\'\n'
        f"{python_source}PY\n"
    )
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_engine_manifest(
    path: Path,
    *,
    engine_sha256: str,
    wrapper_sha256: str | None = None,
) -> EngineManifest:
    manifest = EngineManifest(
        manifest_id="test-only-fake-engine",
        executable_sha256=engine_sha256,
        julia_runtime_sha256="1" * 64,
        julia_manifest_sha256=sha256_file(
            ROOT / "julia/EcoCorridorEngine/Manifest.toml"
        ),
        native_artifacts_sha256="2" * 64,
        wrapper_sha256=wrapper_sha256
        or sha256_file(ROOT / "julia/EcoCorridorEngine/src/EcoCorridorEngine.jl"),
        parser_sha256=sha256_file(ROOT / "python/ecocorridor_core/engine.py"),
        metric_definitions_sha256=sha256_file(ROOT / "python/ecocorridor_core/metrics.py"),
        neighbor_rule="EIGHT",
        resistance_average="ARITHMETIC",
        platform="test-only-fake-engine",
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
    )
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return manifest


def _patch_source(monkeypatch: pytest.MonkeyPatch, identity: SourceRepositoryIdentity) -> None:
    monkeypatch.setattr(direct_study, "_source_repository_identity", lambda: identity)


def test_dirty_source_allows_smoke_and_cache_but_blocks_complete_release(
    tmp_path: Path,
    materialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = tmp_path / "fake-circuitscape"
    engine_sha = _fake_engine(engine)
    engine_manifest_path = tmp_path / "engine-manifest.json"
    _fake_engine_manifest(engine_manifest_path, engine_sha256=engine_sha)
    output = tmp_path / "evidence"
    dirty = FAKE_SOURCE.model_copy(update={"source_tree_clean": False})
    _patch_source(monkeypatch, dirty)

    first = run_usefulness_direct_study(
        materialized_root=materialized,
        output_directory=output,
        engine_executable=engine,
        engine_manifest_path=engine_manifest_path,
        expected_engine_sha256=engine_sha,
        worker_count=2,
        job_limit=4,
    )
    second = run_usefulness_direct_study(
        materialized_root=materialized,
        output_directory=output,
        engine_executable=engine,
        engine_manifest_path=engine_manifest_path,
        expected_engine_sha256=engine_sha,
        worker_count=2,
        job_limit=4,
    )

    assert first.state == second.state == "INCOMPLETE"
    assert first.completed_jobs == second.completed_jobs == 4
    assert first.cached_jobs == 0
    assert second.cached_jobs == 4
    assert len(list(output.glob("jobs/*/attempts/attempt-*"))) == 4
    assert not (output / "public").exists()
    with pytest.raises(UsefulnessDirectStudyError, match="protected-build"):
        _run_usefulness_direct_study(
            materialized_root=materialized,
            output_directory=tmp_path / "blocked-release",
            engine_executable=engine,
            engine_manifest_path=engine_manifest_path,
            expected_engine_sha256=engine_sha,
            worker_count=2,
        )


def test_complete_fake_engine_study_is_qa_gated_but_categorically_unpublishable(
    tmp_path: Path,
    materialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = tmp_path / "fake-circuitscape"
    engine_sha = _fake_engine(engine)
    engine_manifest_path = tmp_path / "engine-manifest.json"
    typed_engine_manifest = _fake_engine_manifest(
        engine_manifest_path, engine_sha256=engine_sha
    )
    output = tmp_path / "evidence"
    _patch_source(monkeypatch, FAKE_SOURCE)

    first = run_usefulness_direct_study(
        materialized_root=materialized,
        output_directory=output,
        engine_executable=engine,
        engine_manifest_path=engine_manifest_path,
        expected_engine_sha256=engine_sha,
        worker_count=8,
    )

    assert first.state == "COMPLETED_TEST_ONLY_UNPUBLISHABLE"
    assert first.completed_jobs == 160
    assert first.cached_jobs == 0
    assert first.engine_trust_profile == "TEST_ONLY_UNPUBLISHABLE"
    assert first.engine_provenance_receipt_sha256 is None
    assert first.evidence_manifest_path is None
    assert first.evidence_archive_path is None
    assert typed_engine_manifest.sha256 == first.engine_manifest_sha256
    first_completion = json.loads(
        min(output.glob("jobs/*/complete.json")).read_text(encoding="utf-8")
    )
    quantitative_qa = first_completion["job"]["matrix_current_coherence"]
    assert quantitative_qa["matrix_pair_count"] > 0
    assert 0 < quantitative_qa["current_positive_cell_count"] <= (
        quantitative_qa["current_valid_cell_count"]
    )
    assert quantitative_qa["current_sum"] > 0
    assert quantitative_qa["current_max"] > 0
    assert quantitative_qa["source_nodata_cell_count"] > 0
    assert quantitative_qa["source_nodata_zero_cell_count"] == quantitative_qa[
        "source_nodata_cell_count"
    ]
    assert quantitative_qa["current_nodata_cell_count"] == 0
    assert quantitative_qa["nodata_encoding"] == "ZERO_OUTSIDE_RETAINED_SOURCE_MASK"
    assert quantitative_qa["qa_sha256"] == canonical_sha256(
        {
            key: value
            for key, value in quantitative_qa.items()
            if key != "qa_sha256"
        }
    )

    second = run_usefulness_direct_study(
        materialized_root=materialized,
        output_directory=output,
        engine_executable=engine,
        engine_manifest_path=engine_manifest_path,
        expected_engine_sha256=engine_sha,
        worker_count=4,
    )
    assert second.state == "COMPLETED_TEST_ONLY_UNPUBLISHABLE"
    assert second.cached_jobs == 160
    assert len(list(output.glob("jobs/*/attempts/attempt-*"))) == 160
    assert not (output / "public").exists()


def test_failed_current_map_qa_never_creates_a_completion_record(
    tmp_path: Path,
    materialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = tmp_path / "bad-circuitscape"
    engine_sha = _fake_engine(engine, corrupt_current=True)
    engine_manifest_path = tmp_path / "engine-manifest.json"
    _fake_engine_manifest(engine_manifest_path, engine_sha256=engine_sha)
    output = tmp_path / "evidence"
    _patch_source(monkeypatch, FAKE_SOURCE.model_copy(update={"source_tree_clean": False}))

    with pytest.raises(UsefulnessDirectStudyError, match="direct job QA rejected"):
        run_usefulness_direct_study(
            materialized_root=materialized,
            output_directory=output,
            engine_executable=engine,
            engine_manifest_path=engine_manifest_path,
            expected_engine_sha256=engine_sha,
            worker_count=1,
            job_limit=1,
        )
    assert not list(output.glob("jobs/*/complete.json"))
    failures = list(output.glob("jobs/*/attempts/*/failure.json"))
    assert len(failures) == 1
    assert json.loads(failures[0].read_text(encoding="utf-8"))["kind"] == "QA_REJECTED"

    zero_engine = tmp_path / "zero-current-circuitscape"
    zero_engine_sha = _fake_engine(zero_engine, zero_current=True)
    zero_manifest = tmp_path / "zero-current-engine-manifest.json"
    _fake_engine_manifest(zero_manifest, engine_sha256=zero_engine_sha)
    zero_output = tmp_path / "zero-current-evidence"
    with pytest.raises(UsefulnessDirectStudyError, match="direct job QA rejected"):
        run_usefulness_direct_study(
            materialized_root=materialized,
            output_directory=zero_output,
            engine_executable=zero_engine,
            engine_manifest_path=zero_manifest,
            expected_engine_sha256=zero_engine_sha,
            worker_count=1,
            job_limit=1,
        )
    zero_failure = json.loads(
        next(zero_output.glob("jobs/*/attempts/*/failure.json")).read_text(encoding="utf-8")
    )
    assert "nonzero diagnostic current support" in zero_failure["message"]


def test_cache_tampering_and_incomplete_ensemble_fail_closed(
    tmp_path: Path,
    materialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = tmp_path / "fake-circuitscape"
    engine_sha = _fake_engine(engine)
    engine_manifest_path = tmp_path / "engine-manifest.json"
    _fake_engine_manifest(engine_manifest_path, engine_sha256=engine_sha)
    output = tmp_path / "evidence"
    _patch_source(monkeypatch, FAKE_SOURCE.model_copy(update={"source_tree_clean": False}))
    run_usefulness_direct_study(
        materialized_root=materialized,
        output_directory=output,
        engine_executable=engine,
        engine_manifest_path=engine_manifest_path,
        expected_engine_sha256=engine_sha,
        worker_count=1,
        job_limit=2,
    )
    completion = next(output.glob("jobs/*/complete.json"))
    job = json.loads(completion.read_text(encoding="utf-8"))["job"]
    matrix = output / next(
        artifact["relative_path"] for artifact in job["artifacts"] if artifact["role"] == "matrix"
    )
    matrix.write_text("1 2 999.0\n", encoding="utf-8")
    with pytest.raises(UsefulnessDirectStudyError, match="immutable hash"):
        run_usefulness_direct_study(
            materialized_root=materialized,
            output_directory=output,
            engine_executable=engine,
            engine_manifest_path=engine_manifest_path,
            expected_engine_sha256=engine_sha,
            worker_count=1,
            job_limit=2,
        )
    with pytest.raises(UsefulnessDirectStudyError, match="all 160"):
        verify_complete_usefulness_ensemble([])


def test_engine_identity_worker_bound_source_url_and_cli_contract(
    tmp_path: Path,
    materialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = tmp_path / "fake-circuitscape"
    engine_sha = _fake_engine(engine)
    engine_manifest_path = tmp_path / "engine-manifest.json"
    _fake_engine_manifest(engine_manifest_path, engine_sha256=engine_sha)
    _patch_source(monkeypatch, FAKE_SOURCE.model_copy(update={"source_tree_clean": False}))
    with pytest.raises(UsefulnessDirectStudyError, match="worker count"):
        run_usefulness_direct_study(
            materialized_root=materialized,
            output_directory=tmp_path / "worker-bound",
            engine_executable=engine,
            engine_manifest_path=engine_manifest_path,
            expected_engine_sha256=engine_sha,
            worker_count=9,
            job_limit=1,
        )
    with pytest.raises(UsefulnessDirectStudyError, match="engine hash"):
        run_usefulness_direct_study(
            materialized_root=materialized,
            output_directory=tmp_path / "wrong-engine",
            engine_executable=engine,
            engine_manifest_path=engine_manifest_path,
            expected_engine_sha256="0" * 64,
            job_limit=1,
        )
    stale_manifest = tmp_path / "stale-engine-manifest.json"
    _fake_engine_manifest(
        stale_manifest,
        engine_sha256=engine_sha,
        wrapper_sha256="0" * 64,
    )
    stale_output = tmp_path / "stale-engine-output"
    with pytest.raises(UsefulnessDirectStudyError, match="stale.*wrapper_sha256"):
        run_usefulness_direct_study(
            materialized_root=materialized,
            output_directory=stale_output,
            engine_executable=engine,
            engine_manifest_path=stale_manifest,
            expected_engine_sha256=engine_sha,
            job_limit=1,
        )
    assert not stale_output.exists()
    wrong_binding = tmp_path / "wrong-binding-engine-manifest.json"
    _fake_engine_manifest(wrong_binding, engine_sha256="3" * 64)
    with pytest.raises(UsefulnessDirectStudyError, match="does not bind"):
        direct_study.validate_usefulness_engine_manifest(
            manifest_path=wrong_binding,
            expected_engine_sha256=engine_sha,
            executable_path=engine,
        )
    assert direct_study._safe_repository_url("git@github.com:ash0ts/example.git") == (
        "https://github.com/ash0ts/example"
    )
    with pytest.raises(UsefulnessDirectStudyError, match="credential-free"):
        direct_study._safe_repository_url("https://token@example.test/owner/repository.git")

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "python")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_usefulness_direct_study.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0
    assert "execute or resume the 160 direct jobs" in completed.stdout

    smoke = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/run_usefulness_direct_study.py"),
            "run",
            "--materialized",
            str(materialized),
            "--output",
            str(tmp_path / "cli-smoke"),
            "--engine",
            str(engine),
            "--engine-manifest",
            str(engine_manifest_path),
            "--engine-sha256",
            engine_sha,
            "--workers",
            "1",
            "--job-limit",
            "1",
            "--test-only-engine",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert smoke.returncode == 2
    assert '"state": "INCOMPLETE"' in smoke.stdout
    rejected = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/run_usefulness_direct_study.py"),
            "run",
            "--materialized",
            str(materialized),
            "--output",
            str(tmp_path / "cli-rejected"),
            "--engine",
            str(engine),
            "--engine-manifest",
            str(engine_manifest_path),
            "--engine-sha256",
            "0" * 64,
            "--job-limit",
            "1",
            "--test-only-engine",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert rejected.returncode == 3
    assert rejected.stderr.startswith("direct-evidence rejected:")
    assert "Traceback" not in rejected.stderr
