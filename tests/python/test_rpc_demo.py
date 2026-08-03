from __future__ import annotations

import hashlib
import io
import json
import socket
from pathlib import Path

import pytest
from gi_scenario_core.demo import create_demo_project
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.rpc_worker import (
    RPCDispatcher,
    _candidate_slots,
    _download_https_pinned,
    serve_jsonl,
)
from gi_scenario_core.validation import validate_model


def _executable(path, content: str = "#!/bin/sh\nexit 0\n"):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_demo_project_is_offline_self_contained_and_unassessed_demo(tmp_path):
    result = create_demo_project(tmp_path / "demo")
    assert result["project"]["project_kind"] == "demo"
    assert result["candidate_count"] == 4
    model_text = Path(result["model_path"]).read_text(encoding="utf-8")
    assert validate_model(model_text).blocking_count == 0
    assert (tmp_path / "demo" / "project.sqlite").is_file()


def test_ui_friendly_project_create_never_auto_assesses_model_fitness(tmp_path):
    dispatcher = RPCDispatcher(data_root=tmp_path)
    conceptual = dispatcher.dispatch("project.create", {"kind": "conceptual"})
    existing = dispatcher.dispatch("project.create", {"kind": "existing"})
    demo = dispatcher.dispatch("project.create", {"kind": "demo"})
    assert conceptual["kind"] == "conceptual"
    assert existing["kind"] == "existing"
    assert demo["kind"] == "demo"
    assert {conceptual["kind"], existing["kind"]} == {"conceptual", "existing"}
    assert all(
        item["fitnessDisposition"] == "not_assessed" for item in (conceptual, existing, demo)
    )


def test_unbound_guided_replay_stays_local_and_queues_no_cloud_work(tmp_path):
    dispatcher = RPCDispatcher(data_root=tmp_path)
    project = dispatcher.dispatch("project.create", {"kind": "demo"})

    opened = dispatcher.dispatch(
        "guided.open",
        {
            "project_directory": project["projectDirectory"],
            "project_id": project["id"],
        },
    )

    assert opened["current_step_id"] == "overview"
    assert opened["cloud_sync_state"] == "local_only"
    assert opened["fixture"]["fixture_id"] == "cedar_trace_exact_v2"
    repository = ProjectRepository(project["projectDirectory"])
    try:
        assert repository.list_guided_checkpoints(project["id"])
        assert repository.table_counts()["telemetry_outbox"] == 0
        with pytest.raises(KeyError):
            repository.get_observability_binding(project["id"])
    finally:
        repository.close()


def test_low_level_project_create_requires_explicit_project_kind(tmp_path):
    dispatcher = RPCDispatcher(data_root=tmp_path)
    with pytest.raises(Exception, match="project_kind"):
        dispatcher.dispatch(
            "project.create", {"project_directory": str(tmp_path / "p"), "title": "P"}
        )


def test_jsonl_response_and_direct_notification_envelopes(tmp_path):
    incoming = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "project.create",
                "params": {"kind": "conceptual"},
            }
        )
        + "\n"
    )
    outgoing = io.StringIO()
    serve_jsonl(incoming, outgoing, data_root=tmp_path)
    lines = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert lines[0]["method"] == "project.updated"
    assert lines[0]["params"]["sequence"] == 1
    assert lines[1]["id"] == 1
    assert lines[1]["result"]["kind"] == "conceptual"


def test_jsonl_parse_and_method_errors_are_standard_json_rpc():
    incoming = io.StringIO(
        "not-json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "no.such.method", "params": {}})
        + "\n"
    )
    outgoing = io.StringIO()
    serve_jsonl(incoming, outgoing)
    lines = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["error"]["code"] == -32601


def test_analysis_plan_requires_validation_evidence(tmp_path):
    dispatcher = RPCDispatcher(data_root=tmp_path)
    project = dispatcher.dispatch("project.create", {"kind": "conceptual"})
    with pytest.raises(Exception, match="validation evidence"):
        dispatcher.dispatch(
            "analysis.plan",
            {
                "project_directory": project["projectDirectory"],
                "project_id": project["id"],
                "candidate_slots": [],
                "design_vectors": [],
                "storm_cases": [],
            },
        )


def test_rpc_rejects_paths_outside_configured_roots(tmp_path):
    dispatcher = RPCDispatcher(data_root=tmp_path / "allowed")
    outside = tmp_path / "outside.inp"
    outside.write_text("[OPTIONS]\n", encoding="utf-8")
    with pytest.raises(Exception, match="allowed roots"):
        dispatcher.dispatch("model.validate", {"path": str(outside)})


def test_model_validation_exposes_only_parsed_setup_identifiers(tmp_path):
    project = create_demo_project(tmp_path / "demo")
    dispatcher = RPCDispatcher(data_root=tmp_path)

    result = dispatcher.dispatch("model.validate", {"path": project["model_path"]})

    assert result["setup_options"]["source"] == "parsed_model"
    assert result["setup_options"]["subcatchment_ids"] == ["S1", "S2"]
    assert result["setup_options"]["lid_control_ids"] == [
        "BARREL",
        "BIOCELL",
        "GREENROOF",
        "PAVEMENT",
        "RAINGARDEN",
        "ROOFDISC",
        "SWALE",
        "TRENCH",
    ]
    assert result["setup_options"]["existing_lid_usage"]


def test_exact_candidate_setup_rejects_implicit_engineering_defaults():
    with pytest.raises(ValueError, match="must explicitly supply"):
        _candidate_slots(
            [
                {
                    "slot_id": "slot_1",
                    "subcatchment_id": "S1",
                    "lid_control": "BIOCELL",
                    "max_additional_units": 1,
                    "unit_area": 100,
                    "surface_width": 10,
                    "feasibility_basis": "Engineer-reviewed fixture",
                }
            ]
        )


def test_project_create_rejects_symlinked_repository_layout(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    project_directory = allowed / "malicious-project"
    allowed.mkdir()
    outside.mkdir()
    project_directory.mkdir()
    try:
        (project_directory / "sources").symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privileges
        pytest.skip(f"symlinks are unavailable: {exc}")
    dispatcher = RPCDispatcher(data_root=allowed)

    with pytest.raises(Exception, match="repository path may not be a symbolic link"):
        dispatcher.dispatch(
            "project.create",
            {
                "kind": "conceptual",
                "project_directory": str(project_directory),
            },
        )
    assert list(outside.iterdir()) == []


def test_download_provider_allowlist_is_worker_owned(tmp_path, monkeypatch):
    monkeypatch.setenv("GI_SCENARIO_PROVIDER_HOSTS", "example.com")
    dispatcher = RPCDispatcher(data_root=tmp_path, provider_hosts=set())
    assert dispatcher.provider_hosts == set()
    with pytest.raises(Exception, match="worker-configured"):
        dispatcher.dispatch(
            "data_asset.download",
            {
                "consent": True,
                "url": "https://example.com/data.tif",
                "approved_hosts": ["example.com"],
                "project_directory": str(tmp_path),
                "license": "fixture",
                "max_bytes": -1,
            },
        )


def test_download_rejects_nonstandard_ports_before_network_access(tmp_path, monkeypatch):
    dispatcher = RPCDispatcher(data_root=tmp_path, provider_hosts={"example.com"})
    monkeypatch.setattr(
        "gi_scenario_core.rpc_worker._download_https_pinned",
        lambda *_args, **_kwargs: pytest.fail("network helper should not be called"),
    )
    with pytest.raises(Exception, match="port 443"):
        dispatcher.dispatch(
            "data_asset.download",
            {
                "consent": True,
                "url": "https://example.com:8443/data.tif",
                "project_directory": str(tmp_path),
                "license": "fixture",
            },
        )


def test_download_dns_pinning_rejects_non_global_shared_addresses(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443))
        ],
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("a non-global address must never be connected"),
    )
    with pytest.raises(ValueError, match="permitted public address"):
        _download_https_pinned("https://example.com/data.tif", max_bytes=1024)


def test_data_asset_add_rejects_symlinked_directory_escape(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    dispatcher = RPCDispatcher(data_root=allowed)
    project = dispatcher.dispatch("project.create", {"kind": "conceptual"})
    source = allowed / "source.tif"
    source.write_bytes(b"fixture")
    data_assets = Path(project["projectDirectory"]) / "data_assets"
    data_assets.rmdir()
    try:
        data_assets.symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privileges
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(Exception, match="escapes its configured parent"):
        dispatcher.dispatch(
            "data_asset.add",
            {
                "path": str(source),
                "project_directory": project["projectDirectory"],
                "license": "fixture",
            },
        )
    assert list(outside.iterdir()) == []


def test_data_asset_add_atomically_replaces_target_symlink(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    dispatcher = RPCDispatcher(data_root=allowed)
    project = dispatcher.dispatch("project.create", {"kind": "conceptual"})
    project_directory = Path(project["projectDirectory"])
    data_assets = project_directory / "data_assets"
    source = allowed / "source.tif"
    source.write_bytes(b"trusted asset bytes")
    digest = _sha256(source)
    outside_target = outside / "victim.txt"
    outside_target.write_bytes(b"must remain unchanged")
    target = data_assets / f"{digest[:16]}-source.tif"
    try:
        target.symlink_to(outside_target)
    except OSError as exc:  # pragma: no cover - Windows without symlink privileges
        pytest.skip(f"symlinks are unavailable: {exc}")

    result = dispatcher.dispatch(
        "data_asset.add",
        {
            "path": str(source),
            "project_directory": project["projectDirectory"],
            "license": "fixture",
        },
    )
    assert result["sha256"] == digest
    assert not target.is_symlink()
    assert target.read_bytes() == b"trusted asset bytes"
    assert outside_target.read_bytes() == b"must remain unchanged"


def test_data_asset_download_atomically_replaces_target_symlink(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    dispatcher = RPCDispatcher(data_root=allowed, provider_hosts={"example.com"})
    project = dispatcher.dispatch("project.create", {"kind": "conceptual"})
    content = b"downloaded trusted bytes"
    digest = hashlib.sha256(content).hexdigest()
    target = Path(project["projectDirectory"]) / "data_assets" / f"{digest[:16]}-data.tif"
    outside_target = outside / "victim.txt"
    outside_target.write_bytes(b"must remain unchanged")
    try:
        target.symlink_to(outside_target)
    except OSError as exc:  # pragma: no cover - Windows without symlink privileges
        pytest.skip(f"symlinks are unavailable: {exc}")
    monkeypatch.setattr(
        "gi_scenario_core.rpc_worker._download_https_pinned",
        lambda *_args, **_kwargs: content,
    )

    result = dispatcher.dispatch(
        "data_asset.download",
        {
            "consent": True,
            "url": "https://example.com/data.tif",
            "project_directory": project["projectDirectory"],
            "license": "fixture",
        },
    )
    assert result["sha256"] == digest
    assert not target.is_symlink()
    assert target.read_bytes() == content
    assert outside_target.read_bytes() == b"must remain unchanged"


def test_baseline_rejects_storm_identifier_path_traversal(tmp_path):
    executable = _executable(tmp_path / "swmm")
    model = tmp_path / "base.inp"
    model.write_text("[OPTIONS]\nFLOW_UNITS CFS\n", encoding="utf-8")
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        solver_executable=executable,
        solver_sha256=_sha256(executable),
    )
    with pytest.raises(Exception, match="unsafe storm"):
        dispatcher.dispatch(
            "baseline.start",
            {
                "solver_executable": str(executable),
                "work_directory": str(tmp_path / "runs"),
                "storm_cases": [{"storm_id": "../escape", "model_path": str(model)}],
            },
        )


def test_baseline_rejects_renderer_selected_executable_inside_allowed_root(tmp_path):
    trusted = _executable(tmp_path / "trusted-swmm")
    attacker = _executable(tmp_path / "uploaded-program", "#!/bin/sh\ntouch compromised\n")
    model = tmp_path / "base.inp"
    model.write_text("[OPTIONS]\nFLOW_UNITS CFS\n", encoding="utf-8")
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        solver_executable=trusted,
        solver_sha256=_sha256(trusted),
    )

    with pytest.raises(Exception, match="renderer-selected"):
        dispatcher.dispatch(
            "baseline.start",
            {
                "solver_executable": str(attacker),
                "work_directory": str(tmp_path / "runs"),
                "storm_cases": [{"storm_id": "storm-a", "model_path": str(model)}],
            },
        )


def test_baseline_fails_closed_without_host_configured_solver(tmp_path):
    executable = _executable(tmp_path / "renderer-program")
    model = tmp_path / "base.inp"
    model.write_text("[OPTIONS]\nFLOW_UNITS CFS\n", encoding="utf-8")
    dispatcher = RPCDispatcher(data_root=tmp_path)

    with pytest.raises(Exception, match="no configured pinned SWMM"):
        dispatcher.dispatch(
            "baseline.start",
            {
                "solver_executable": str(executable),
                "work_directory": str(tmp_path / "runs"),
                "storm_cases": [{"storm_id": "storm-a", "model_path": str(model)}],
            },
        )


def test_baseline_rechecks_configured_solver_digest(tmp_path):
    executable = _executable(tmp_path / "trusted-swmm")
    expected_sha256 = _sha256(executable)
    dispatcher = RPCDispatcher(
        data_root=tmp_path,
        solver_executable=executable,
        solver_sha256=expected_sha256,
    )
    executable.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    model = tmp_path / "base.inp"
    model.write_text("[OPTIONS]\nFLOW_UNITS CFS\n", encoding="utf-8")

    with pytest.raises(Exception, match="no longer matches"):
        dispatcher.dispatch(
            "baseline.start",
            {
                "work_directory": str(tmp_path / "runs"),
                "storm_cases": [{"storm_id": "storm-a", "model_path": str(model)}],
            },
        )


def test_baseline_rejects_duplicate_storm_ids_and_symlinked_run_escape(tmp_path):
    executable = _executable(tmp_path / "trusted-swmm")
    model = tmp_path / "base.inp"
    model.write_text("[OPTIONS]\nFLOW_UNITS CFS\n", encoding="utf-8")
    dispatcher = RPCDispatcher(
        data_root=tmp_path / "allowed",
        allowed_roots=[tmp_path / "allowed"],
        solver_executable=executable,
        solver_sha256=_sha256(executable),
    )
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    allowed_model = allowed / "base.inp"
    allowed_model.write_bytes(model.read_bytes())
    work_directory = allowed / "runs"
    work_directory.mkdir()

    duplicate = {"storm_id": "storm-a", "model_path": str(allowed_model)}
    with pytest.raises(Exception, match="duplicate storm"):
        dispatcher.dispatch(
            "baseline.start",
            {
                "work_directory": str(work_directory),
                "storm_cases": [duplicate, duplicate],
            },
        )

    outside = tmp_path / "outside-run"
    outside.mkdir()
    try:
        (work_directory / "storm-a").symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privileges
        pytest.skip(f"symlinks are unavailable: {exc}")
    with pytest.raises(Exception, match="escapes its configured parent"):
        dispatcher.dispatch(
            "baseline.start",
            {
                "work_directory": str(work_directory),
                "storm_cases": [duplicate],
            },
        )
