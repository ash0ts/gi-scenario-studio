"""Strict JSONL/stdio JSON-RPC worker for the Tauri desktop host."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from .bundle import inspect_bundle, safe_extract_bundle
from .concept import generate_concept_model
from .contracts import (
    PUBLIC_CONTRACTS,
    ExactRunResult,
    ModelObjectProvenance,
    ModelReadiness,
    ReadinessAttestation,
    ReadinessLevel,
    SimulationBudget,
    new_id,
    public_schemas,
)
from .demo import create_demo_project
from .export import create_evidence_bundle, validate_export
from .repository import ProjectRepository
from .search import make_search_plan, nondominated_indices
from .solver import SolverRunner
from .swmm import SwmmDocument
from .validation import ValidationReport, promote_readiness, validate_model

API_VERSION = "1.0"


class RPCFault(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


class RPCDispatcher:
    def __init__(
        self,
        *,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        data_root: str | Path | None = None,
        allowed_roots: list[str | Path] | None = None,
        provider_hosts: set[str] | None = None,
        solver_executable: str | Path | None = None,
        solver_sha256: str | None = None,
    ) -> None:
        self.event_sink = event_sink
        self.sequence = 0
        self.data_root = Path(data_root or _default_data_root()).resolve()
        self.allowed_roots = {
            self.data_root,
            *(Path(item).resolve() for item in (allowed_roots or [])),
        }
        configured_hosts = os.environ.get("GI_SCENARIO_PROVIDER_HOSTS", "")
        host_values = (
            provider_hosts
            if provider_hosts is not None
            else {item.strip() for item in configured_hosts.split(",") if item.strip()}
        )
        self.provider_hosts = {_normalize_provider_host(item) for item in host_values}
        self.solver_executable: Path | None = None
        self.solver_sha256: str | None = None
        if (solver_executable is None) != (solver_sha256 is None):
            raise ValueError(
                "the worker must configure both a SWMM executable and its pinned SHA-256"
            )
        if solver_executable is not None and solver_sha256 is not None:
            executable = Path(solver_executable).expanduser().resolve()
            if not executable.is_file():
                raise ValueError(f"configured SWMM executable was not found: {executable}")
            normalized_sha256 = solver_sha256.casefold()
            _require_sha256(normalized_sha256, label="configured SWMM executable hash")
            if _sha256_path(executable) != normalized_sha256:
                raise ValueError("configured SWMM executable does not match its pinned SHA-256")
            self.solver_executable = executable
            self.solver_sha256 = normalized_sha256
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gi-solver")
        self._background: dict[str, dict[str, Any]] = {}
        self._background_lock = threading.RLock()
        self._methods: dict[str, Callable[[dict[str, Any]], Any]] = {
            "system.ping": self.system_ping,
            "schema.list": self.schema_list,
            "schema.get": self.schema_get,
            "project.create": self.project_create,
            "project.open": self.project_open,
            "project.get_state": self.project_get_state,
            "bundle.inspect": self.bundle_inspect,
            "bundle.import": self.bundle_import,
            "data_asset.add": self.data_asset_add,
            "data_asset.download": self.data_asset_download,
            "concept.preview": self.concept_preview,
            "concept.generate": self.concept_generate,
            "concept.update_object": self.concept_update_object,
            "model.validate": self.model_validate,
            "model.attest": self.model_attest,
            "baseline.plan": self.baseline_plan,
            "baseline.start": self.baseline_start,
            "analysis.plan": self.analysis_plan,
            "analysis.start": self.analysis_start,
            "analysis.pause": self.analysis_pause,
            "analysis.resume": self.analysis_resume,
            "analysis.cancel": self.analysis_cancel,
            "results.query": self.results_query,
            "results.compare": self.results_compare,
            "evidence.resolve": self.evidence_resolve,
            "export.validate": self.export_validate,
            "export.create": self.export_create,
            "diagnostics.create_bundle": self.diagnostics_create_bundle,
            "demo.create": self.demo_create,
        }

    @property
    def methods(self) -> list[str]:
        return sorted(self._methods)

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        with self._background_lock:
            self.sequence += 1
            sequence = self.sequence
        if self.event_sink:
            self.event_sink({"sequence": sequence, "event": event, "payload": payload})

    def require_allowed_path(self, value: str | Path, *, purpose: str) -> Path:
        path = Path(value).expanduser().resolve()
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise ValueError(f"{purpose} is outside the worker's configured allowed roots")
        return path

    def require_contained_path(
        self,
        root: str | Path,
        relative: str | Path,
        *,
        purpose: str,
    ) -> Path:
        """Resolve a worker-created child and reject pre-existing symlink escapes."""

        resolved_root = self.require_allowed_path(root, purpose=purpose)
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"{purpose} contains an unsafe relative path")
        path = (resolved_root / relative_path).resolve()
        if path == resolved_root or resolved_root not in path.parents:
            raise ValueError(f"{purpose} escapes its configured parent directory")
        return path

    def require_configured_solver(self, supplied: Any = None) -> Path:
        """Return only the host-configured, digest-pinned SWMM executable."""

        if self.solver_executable is None or self.solver_sha256 is None:
            raise ValueError("worker has no configured pinned SWMM executable")
        if (
            supplied is not None
            and Path(str(supplied)).expanduser().resolve() != self.solver_executable
        ):
            raise ValueError("renderer-selected SWMM executables are not permitted")
        if _sha256_path(self.solver_executable) != self.solver_sha256:
            raise ValueError("configured SWMM executable no longer matches its pinned SHA-256")
        return self.solver_executable

    def require_project_directory(self, value: str | Path) -> Path:
        project_directory = self.require_allowed_path(value, purpose="project directory")
        database_link = project_directory / "project.sqlite"
        if database_link.is_symlink():
            raise ValueError("project database may not be a symbolic link")
        database = self.require_contained_path(
            project_directory,
            "project.sqlite",
            purpose="project database",
        )
        if not database.is_file():
            raise ValueError("project directory does not contain an authoritative repository")
        return project_directory

    def dispatch(self, method: str, params: dict[str, Any] | None = None) -> Any:
        handler = self._methods.get(method)
        if handler is None:
            raise RPCFault(-32601, f"Method not found: {method}", {"available": self.methods})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RPCFault(-32602, "Params must be an object")
        try:
            return handler(params)
        except RPCFault:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise RPCFault(-32602, str(exc), {"type": type(exc).__name__}) from exc

    def system_ping(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"api_version": API_VERSION, "status": "ready", "methods": self.methods}

    def schema_list(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"api_version": API_VERSION, "contracts": sorted(PUBLIC_CONTRACTS)}

    def schema_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _required(params, "name")
        schemas = public_schemas()
        if name not in schemas:
            raise KeyError(f"unknown public contract {name!r}")
        return {"name": name, "schema": schemas[name]}

    def project_create(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("kind")
        if kind is not None and kind not in {"existing", "conceptual", "demo"}:
            raise ValueError("kind must be existing, conceptual, or demo")
        if "project_directory" not in params and kind:
            self.data_root.mkdir(parents=True, exist_ok=True)
            directory = self.data_root / new_id("project")
        else:
            directory = self.require_allowed_path(
                _required(params, "project_directory"), purpose="project directory"
            )
        if kind == "demo":
            created = create_demo_project(directory)
            project = created["project"]
            summary = _project_summary(
                project, directory, kind="demo", model_file="willow-creek-demo.inp"
            )
            self.emit("project.updated", {"project_id": project["project_id"], "demo": True})
            return summary
        title = params.get("title") or (
            "New conceptual study" if kind == "conceptual" else "Imported SWMM project"
        )
        if "readiness" not in params and kind is None:
            raise ValueError("readiness must be explicit; projects are never auto-promoted")
        # `CONCEPTUAL` is an initial unverified classification, never an automatic
        # promotion. Professional output remains gated behind explicit attestation.
        readiness = params.get("readiness", "CONCEPTUAL")
        repository = ProjectRepository(directory)
        try:
            project = repository.create_project(
                title=title,
                readiness=readiness,
                project_id=params.get("project_id"),
                state=params.get("state"),
            )
        finally:
            repository.close()
        self.emit("project.updated", {"project_id": project["project_id"]})
        if kind:
            return _project_summary(
                project,
                directory,
                kind=kind,
                model_file="concept-draft.inp" if kind == "conceptual" else "pending-import.inp",
            )
        return project

    def project_open(self, params: dict[str, Any]) -> dict[str, Any]:
        repository = ProjectRepository(
            self.require_allowed_path(
                _required(params, "project_directory"), purpose="project directory"
            )
        )
        try:
            project_id = params.get("project_id")
            if project_id is None:
                row = repository._connection.execute(  # intentionally read-only internal lookup
                    "SELECT project_id FROM projects ORDER BY created_at LIMIT 1"
                ).fetchone()
                if row is None:
                    raise KeyError("project directory does not contain a project")
                project_id = row["project_id"]
            return repository.get_project(project_id)
        finally:
            repository.close()

    def project_get_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.project_open(params)

    def bundle_inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        archive = self.require_allowed_path(
            _required(params, "archive_path"), purpose="project bundle"
        )
        return inspect_bundle(archive).to_dict()

    def bundle_import(self, params: dict[str, Any]) -> dict[str, Any]:
        archive = self.require_allowed_path(
            _required(params, "archive_path"), purpose="project bundle"
        )
        destination = self.require_allowed_path(
            _required(params, "destination"), purpose="bundle destination"
        )
        inspection = safe_extract_bundle(archive, destination)
        self.emit("project.updated", {"bundle_valid": inspection.valid})
        return inspection.to_dict()

    def data_asset_add(self, params: dict[str, Any]) -> dict[str, Any]:
        source = self.require_allowed_path(_required(params, "path"), purpose="data asset")
        if not source.is_file():
            raise ValueError(f"data asset was not found: {source}")
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        target_directory = self.require_contained_path(
            project_directory,
            "data_assets",
            purpose="project data-asset directory",
        )
        target_directory.mkdir(parents=True, exist_ok=True)
        target_directory = self.require_contained_path(
            project_directory,
            "data_assets",
            purpose="project data-asset directory",
        )
        target, digest, size = _copy_content_addressed_asset(source, target_directory)
        payload = {
            "asset_id": f"asset_{digest[:16]}",
            "path": str(target),
            "role": params.get("role", "source_data"),
            "source": params.get("source", "user_provided"),
            "license": params.get("license", "user_provided"),
            "sha256": digest,
            "bytes": size,
        }
        self.emit("project.updated", {"data_asset": payload})
        return payload

    def data_asset_download(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("consent") is not True:
            raise ValueError("explicit download consent is required")
        url = str(_required(params, "url"))
        parsed = urllib.parse.urlparse(url)
        hostname = _normalize_provider_host(parsed.hostname or "")
        if (
            parsed.scheme != "https"
            or not hostname
            or hostname not in self.provider_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (parsed.port or 443) != 443
        ):
            raise ValueError(
                "download URL must use HTTPS on port 443 with a worker-configured provider host"
            )
        max_bytes = max(1, min(int(params.get("max_bytes", 100 * 1024 * 1024)), 500 * 1024 * 1024))
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        target_directory = self.require_contained_path(
            project_directory,
            "data_assets",
            purpose="project data-asset directory",
        )
        target_directory.mkdir(parents=True, exist_ok=True)
        target_directory = self.require_contained_path(
            project_directory,
            "data_assets",
            purpose="project data-asset directory",
        )
        content = _download_https_pinned(url, max_bytes=max_bytes)
        if len(content) > max_bytes:
            raise ValueError("download exceeds the approved size limit")
        digest = hashlib.sha256(content).hexdigest()
        filename = _safe_asset_filename(urllib.parse.unquote(parsed.path))
        target = target_directory / f"{digest[:16]}-{filename}"
        _atomic_write_bytes(target, content)
        payload = {
            "asset_id": f"asset_{digest[:16]}",
            "path": str(target),
            "source": url,
            "license": _required(params, "license"),
            "sha256": digest,
            "bytes": len(content),
        }
        self.emit("project.updated", {"data_asset": payload})
        return payload

    def concept_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        result = generate_concept_model(_required(params, "recipe"))
        payload = result.model_dump(mode="json")
        payload.pop("model_text", None)
        payload["model_preview"] = result.model_text[:4_096]
        return payload

    def concept_generate(self, params: dict[str, Any]) -> dict[str, Any]:
        result = generate_concept_model(_required(params, "recipe"))
        destination = params.get("destination")
        payload = result.model_dump(mode="json")
        if destination:
            path = self.require_allowed_path(destination, purpose="generated model destination")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.model_text, encoding="utf-8", newline="\n")
            payload["model_path"] = str(path)
            payload.pop("model_text", None)
        self.emit(
            "project.updated", {"recipe_id": result.recipe_id, "model_sha256": result.model_sha256}
        )
        return payload

    def concept_update_object(self, params: dict[str, Any]) -> dict[str, Any]:
        if "path" in params:
            params = dict(params)
            params["path"] = str(self.require_allowed_path(params["path"], purpose="SWMM model"))
        text = _model_text(params)
        document = SwmmDocument.parse(text)
        command = _required(params, "command")
        if command == "SetLIDUnitCount":
            document.set_lid_unit_count(
                _required(params, "subcatchment_id"),
                _required(params, "lid_control"),
                int(_required(params, "unit_count")),
            )
        elif command == "SetSimulationWindow":
            document.set_simulation_window(
                _required(params, "start_date"), _required(params, "end_date")
            )
        else:
            raise ValueError(f"unsupported typed mutation command: {command}")
        destination = params.get("destination")
        if destination:
            destination_path = self.require_allowed_path(destination, purpose="model destination")
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_text(document.render(), encoding="utf-8", newline="")
        return {
            "model_text": None if destination else document.render(),
            "model_path": str(destination_path) if destination else None,
            "changes": [asdict(item) for item in document.changes],
        }

    def model_validate(self, params: dict[str, Any]) -> dict[str, Any]:
        if "path" in params:
            params = dict(params)
            params["path"] = str(self.require_allowed_path(params["path"], purpose="SWMM model"))
        text = _model_text(params)
        report = validate_model(text)
        payload = report.model_dump(mode="json")
        payload.update(
            {
                "blocking_count": report.blocking_count,
                "warning_count": report.warning_count,
                "structurally_runnable": report.structurally_runnable,
            }
        )
        self.emit("validation.completed", payload)
        return payload

    def model_attest(self, params: dict[str, Any]) -> dict[str, Any]:
        current = ModelReadiness.model_validate(_required(params, "current"))
        validation_payload = dict(_required(params, "validation"))
        for computed in ("blocking_count", "warning_count", "structurally_runnable"):
            validation_payload.pop(computed, None)
        validation = ValidationReport.model_validate(validation_payload)
        promoted = promote_readiness(
            current,
            ReadinessLevel(_required(params, "target")),
            validation=validation,
            provenance=[
                ModelObjectProvenance.model_validate(item) for item in params.get("provenance", [])
            ],
            attestations=[
                ReadinessAttestation.model_validate(item) for item in params.get("attestations", [])
            ],
            reviewer=params.get("reviewer"),
            reference_comparison=params.get("reference_comparison"),
        )
        if "project_directory" in params or "project_id" in params:
            if "project_directory" not in params or "project_id" not in params:
                raise ValueError("persisting readiness requires project_directory and project_id")
            repository = ProjectRepository(
                self.require_allowed_path(params["project_directory"], purpose="project directory")
            )
            try:
                if promoted.level == ReadinessLevel.ENGINEER_REVIEWED:
                    gate_evidence = {
                        "validation": {"blocking_count": validation.blocking_count},
                        "attestations": [
                            item.model_dump(mode="json") for item in promoted.attestations
                        ],
                        "reviewer": promoted.reviewer,
                        "unreviewed_assumption_count": sum(
                            1
                            for item in params.get("provenance", [])
                            if not item.get("reviewed", False)
                        ),
                    }
                else:
                    gate_evidence = {"reference_comparison": params.get("reference_comparison")}
                repository.record_readiness(
                    params["project_id"], promoted.level, gate_evidence=gate_evidence
                )
            finally:
                repository.close()
        self.emit("project.updated", {"readiness": promoted.level.value})
        return promoted.model_dump(mode="json")

    def baseline_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        storms = params.get("storm_cases", [])
        return {
            "storm_count": len(storms),
            "baseline_solver_jobs": len(storms),
            "exact": True,
            "message": "One untouched exact SWMM run is planned for each storm case.",
        }

    def baseline_start(self, params: dict[str, Any]) -> dict[str, Any]:
        executable = self.require_configured_solver(params.get("solver_executable"))
        cases = _required(params, "storm_cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError("baseline requires at least one storm case")
        work_root = self.require_allowed_path(
            _required(params, "work_directory"), purpose="baseline work directory"
        )
        timeout = max(1, min(int(params.get("timeout_seconds", 3600)), 86_400))
        prepared = []
        seen_storm_ids: set[str] = set()
        for case in cases:
            if not isinstance(case, dict):
                raise TypeError("each storm case must be an object")
            storm_id = str(case["storm_id"])
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", storm_id):
                raise ValueError(f"unsafe storm identifier: {storm_id!r}")
            if storm_id in seen_storm_ids:
                raise ValueError(f"duplicate storm identifier: {storm_id!r}")
            seen_storm_ids.add(storm_id)
            model_path = self.require_allowed_path(case["model_path"], purpose="storm model")
            dependencies = {
                relative: str(self.require_allowed_path(source, purpose="storm dependency"))
                for relative, source in case.get("dependencies", {}).items()
            }
            run_directory = self.require_contained_path(
                work_root,
                storm_id,
                purpose=f"baseline run directory for storm {storm_id!r}",
            )
            prepared.append((storm_id, model_path, dependencies, run_directory))
        operation_id = str(params.get("analysis_id") or new_id("baseline"))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", operation_id):
            raise ValueError(f"unsafe baseline analysis identifier: {operation_id!r}")
        cancel_event = threading.Event()
        pause_event = threading.Event()

        def execute() -> None:
            outcomes = []
            try:
                runner = SolverRunner(executable, timeout_seconds=timeout)
                if runner.solver_sha256 != self.solver_sha256:
                    raise ValueError("configured SWMM executable changed before execution")
                for storm_id, model_path, dependencies, prepared_run_directory in prepared:
                    while pause_event.is_set() and not cancel_event.is_set():
                        time.sleep(0.1)
                    if cancel_event.is_set():
                        break
                    run_directory = self.require_contained_path(
                        work_root,
                        storm_id,
                        purpose=f"baseline run directory for storm {storm_id!r}",
                    )
                    if run_directory != prepared_run_directory:
                        raise ValueError(
                            f"baseline run directory changed after planning for storm {storm_id!r}"
                        )
                    self.emit(
                        "solver_job.started",
                        {"analysis_id": operation_id, "storm_id": storm_id},
                    )
                    outcome = runner.run(
                        model_bytes=model_path.read_bytes(),
                        work_directory=run_directory,
                        dependencies=dependencies,
                        qa_policy=params.get("qa_policy"),
                        cancel_event=cancel_event,
                    )
                    outcomes.append({"storm_id": storm_id, **outcome.model_dump(mode="json")})
                    self.emit(
                        "solver_job.completed",
                        {
                            "analysis_id": operation_id,
                            "storm_id": storm_id,
                            "qa_status": outcome.qa_status,
                        },
                    )
                state = "CANCELLED" if cancel_event.is_set() else "COMPLETED"
                self.emit(
                    "validation.completed",
                    {"analysis_id": operation_id, "state": state, "baseline_outcomes": outcomes},
                )
            except Exception as exc:  # noqa: BLE001 - background failures must become events
                self.emit(
                    "solver_job.failed",
                    {
                        "analysis_id": operation_id,
                        "state": "FAILED",
                        "error": str(exc),
                    },
                )

        with self._background_lock:
            existing = self._background.get(operation_id)
            if existing is not None:
                existing_future = existing.get("future")
                if existing_future is None or not existing_future.done():
                    raise ValueError(f"baseline analysis {operation_id!r} is already running")
            background = {
                "future": None,
                "cancel": cancel_event,
                "pause": pause_event,
            }
            self._background[operation_id] = background
        try:
            future = self._executor.submit(execute)
        except Exception:
            with self._background_lock:
                if self._background.get(operation_id) is background:
                    self._background.pop(operation_id, None)
            raise
        with self._background_lock:
            background["future"] = future
        return {
            "analysis_id": operation_id,
            "state": "RUNNING",
            "exact": True,
            "planned_solver_jobs": len(prepared),
        }

    def analysis_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        plan = make_search_plan(
            _required(params, "candidate_slots"),
            params.get("budget", SimulationBudget().model_dump(mode="json")),
            storm_count=int(_required(params, "storm_count")),
            analysis_id=params.get("analysis_id"),
            baseline_jobs=params.get("baseline_jobs"),
        )
        return plan.model_dump(mode="json")

    def analysis_start(self, params: dict[str, Any]) -> dict[str, Any]:
        validation = params.get("validation")
        if not isinstance(validation, dict) or validation.get("blocking_count") != 0:
            raise ValueError(
                "analysis start requires validation evidence with zero blocking issues"
            )
        repository = ProjectRepository(
            self.require_allowed_path(
                _required(params, "project_directory"), purpose="project directory"
            )
        )
        try:
            analysis = repository.create_analysis(
                _required(params, "project_id"),
                payload=params.get("payload"),
                idempotency_key=params.get("idempotency_key"),
                analysis_id=params.get("analysis_id"),
            )
            if analysis["state"] == "DRAFT":
                analysis = repository.transition_analysis(analysis["analysis_id"], "VALIDATING")
                analysis = repository.transition_analysis(analysis["analysis_id"], "READY")
                analysis = repository.transition_analysis(analysis["analysis_id"], "RUNNING")
        finally:
            repository.close()
        self.emit(
            "analysis.state_changed",
            {"analysis_id": analysis["analysis_id"], "state": analysis["state"]},
        )
        return analysis

    def _transition_analysis(self, params: dict[str, Any], state: str) -> dict[str, Any]:
        analysis_id = str(_required(params, "analysis_id"))
        with self._background_lock:
            background = self._background.get(analysis_id)
        if background is not None:
            if state == "PAUSING":
                background["pause"].set()
                result = {"analysis_id": analysis_id, "state": "PAUSING"}
            elif state == "RUNNING":
                background["pause"].clear()
                result = {"analysis_id": analysis_id, "state": "RUNNING"}
            elif state == "CANCELLED":
                background["cancel"].set()
                result = {"analysis_id": analysis_id, "state": "CANCELLED"}
            else:
                raise ValueError(f"unsupported background transition {state}")
            self.emit("analysis.state_changed", result)
            return result
        repository = ProjectRepository(
            self.require_allowed_path(
                _required(params, "project_directory"), purpose="project directory"
            )
        )
        try:
            analysis = repository.transition_analysis(analysis_id, state)
            if state == "PAUSING":
                analysis = repository.transition_analysis(analysis["analysis_id"], "PAUSED")
        finally:
            repository.close()
        self.emit(
            "analysis.state_changed",
            {"analysis_id": analysis["analysis_id"], "state": analysis["state"]},
        )
        return analysis

    def analysis_pause(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._transition_analysis(params, "PAUSING")

    def analysis_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._transition_analysis(params, "RUNNING")

    def analysis_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._transition_analysis(params, "CANCELLED")

    def results_query(self, params: dict[str, Any]) -> dict[str, Any]:
        repository = ProjectRepository(
            self.require_allowed_path(
                _required(params, "project_directory"), purpose="project directory"
            )
        )
        try:
            rows = repository._connection.execute(
                "SELECT j.job_id,j.analysis_id,j.design_id,j.storm_id,j.state,m.metric_id,m.object_id,m.value,m.units "
                "FROM solver_jobs j LEFT JOIN run_metrics m ON m.job_id=j.job_id "
                "WHERE j.analysis_id=? ORDER BY j.design_id,j.storm_id,m.metric_id,m.object_id",
                (_required(params, "analysis_id"),),
            ).fetchall()
            return {"rows": [dict(row) for row in rows]}
        finally:
            repository.close()

    def results_compare(self, params: dict[str, Any]) -> dict[str, Any]:
        rows = _required(params, "rows")
        directions = _required(params, "directions")
        indices = nondominated_indices(rows, directions)
        return {
            "nondominated_indices": indices,
            "nondominated_rows": [rows[index] for index in indices],
        }

    def evidence_resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "job_id"})
        project_directory = self.require_allowed_path(
            _required(params, "project_directory"), purpose="project directory"
        )
        if not (project_directory / "project.sqlite").is_file():
            raise ValueError("project directory does not contain an authoritative repository")
        repository = ProjectRepository(project_directory)
        try:
            authoritative = _load_authoritative_run(
                repository,
                str(_required(params, "job_id")),
            )
        finally:
            repository.close()
        result: ExactRunResult = authoritative["result"]
        payload = result.model_dump(mode="json")
        payload.update(
            {
                "state": "SUCCEEDED",
                "project_id": authoritative["project_id"],
                "physics_cache_key": authoritative["physics_cache_key"],
                "metric_records": authoritative["metric_records"],
                "qa_records": authoritative["qa_records"],
                "artifacts": authoritative["artifacts"],
                "hashes": {
                    "input": result.input_sha256,
                    "solver": result.solver_sha256,
                    "output": result.output_sha256,
                },
            }
        )
        return payload

    def export_validate(self, params: dict[str, Any]) -> dict[str, Any]:
        return validate_export(**params).model_dump(mode="json")

    def export_create(self, params: dict[str, Any]) -> dict[str, Any]:
        # A renderer may select completed job IDs, but it may never provide the
        # hydraulic values or hashes that an evidence package will claim.
        prohibited = {
            "results",
            "input_hashes",
            "solver_sha256",
            "results_verified",
            "attachments",
            "project_id",
            "project_title",
            "readiness",
        }
        supplied = sorted(prohibited.intersection(params))
        if supplied:
            raise ValueError(
                "export.create derives authoritative evidence fields from the project repository; "
                f"remove: {', '.join(supplied)}"
            )
        export_params = dict(params)
        export_params["destination"] = str(
            self.require_allowed_path(
                _required(export_params, "destination"), purpose="export destination"
            )
        )
        project_directory = self.require_allowed_path(
            _required(export_params, "project_directory"), purpose="project directory"
        )
        if not (project_directory / "project.sqlite").is_file():
            raise ValueError("project directory does not contain an authoritative repository")
        export_params.pop("project_directory")
        analysis_id = str(_required(export_params, "analysis_id"))
        raw_job_ids = _required(export_params, "job_ids")
        if not isinstance(raw_job_ids, list) or not raw_job_ids:
            raise ValueError("job_ids must be a non-empty array")
        if any(not isinstance(item, str) or not item for item in raw_job_ids):
            raise ValueError("every job_id must be a non-empty string")
        if len(set(raw_job_ids)) != len(raw_job_ids):
            raise ValueError("job_ids cannot contain duplicates")
        export_params.pop("job_ids")
        include_artifacts = export_params.pop("include_artifacts", False)
        if not isinstance(include_artifacts, bool):
            raise TypeError("include_artifacts must be a boolean")

        repository = ProjectRepository(project_directory)
        try:
            analysis_row = repository._connection.execute(
                "SELECT a.analysis_id,a.project_id,p.title,p.readiness "
                "FROM analyses a JOIN projects p ON p.project_id=a.project_id "
                "WHERE a.analysis_id=?",
                (analysis_id,),
            ).fetchone()
            if analysis_row is None:
                raise KeyError(f"analysis {analysis_id!r} was not found")
            authoritative_runs = [
                _load_authoritative_run(
                    repository,
                    job_id,
                    expected_analysis_id=analysis_id,
                    expected_project_id=analysis_row["project_id"],
                )
                for job_id in raw_job_ids
            ]
            solver_hashes = {item["result"].solver_sha256 for item in authoritative_runs}
            if len(solver_hashes) != 1:
                raise ValueError(
                    "one evidence package cannot combine runs from different SWMM binaries"
                )
            input_hashes = {
                f"{item['result'].run_id}:{artifact['role']}": artifact["sha256"]
                for item in authoritative_runs
                for artifact in item["artifacts"]
                if artifact["role"] not in {"output", "report", "solver"}
            }
            attachments: dict[str, bytes] = {}
            if include_artifacts:
                for item in authoritative_runs:
                    for artifact in item["artifacts"]:
                        filename = _export_artifact_name(item["result"].run_id, artifact["role"])
                        if filename is None:
                            continue
                        path = _artifact_path(repository.root, artifact["relative_path"])
                        content = path.read_bytes()
                        if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
                            raise ValueError(
                                f"artifact bytes changed while creating export for job "
                                f"{item['result'].run_id!r}"
                            )
                        attachments[filename] = content
        finally:
            repository.close()

        export_params.update(
            {
                "project_id": analysis_row["project_id"],
                "project_title": analysis_row["title"],
                "readiness": analysis_row["readiness"],
                "results": [item["result"] for item in authoritative_runs],
                "input_hashes": input_hashes,
                "solver_sha256": next(iter(solver_hashes)),
                "results_verified": True,
                "attachments": attachments or None,
            }
        )
        result = create_evidence_bundle(**export_params)
        self.emit(
            "export.completed", {"export_id": result.manifest.export_id, "files": result.files}
        )
        return result.model_dump(mode="json")

    def diagnostics_create_bundle(self, params: dict[str, Any]) -> dict[str, Any]:
        destination = self.require_allowed_path(
            _required(params, "destination"), purpose="diagnostics destination"
        )
        diagnostic = {
            "api_version": API_VERSION,
            "include_model_content": bool(params.get("include_model_content", False)),
            "events_emitted": self.sequence,
            "notes": params.get("notes", ""),
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(diagnostic, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {"path": str(destination), "redacted": not diagnostic["include_model_content"]}

    def demo_create(self, params: dict[str, Any]) -> dict[str, Any]:
        destination = self.require_allowed_path(
            _required(params, "destination"), purpose="demo destination"
        )
        result = create_demo_project(destination)
        self.emit("project.updated", {"project_id": result["project"]["project_id"], "demo": True})
        return result


def _required(params: dict[str, Any], key: str) -> Any:
    if key not in params:
        raise ValueError(f"missing required parameter: {key}")
    return params[key]


def _require_only(params: dict[str, Any], allowed: set[str]) -> None:
    unexpected = sorted(set(params).difference(allowed))
    if unexpected:
        raise ValueError(f"unexpected parameters: {', '.join(unexpected)}")


def _model_text(params: dict[str, Any]) -> str:
    if "text" in params:
        return str(params["text"])
    if "path" in params:
        return Path(params["path"]).read_text(encoding="utf-8", errors="replace")
    raise ValueError("provide either text or path")


def _default_data_root() -> Path:
    configured = os.environ.get("GI_SCENARIO_DATA_ROOT")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GI Scenario Studio" / "projects"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "GI Scenario Studio" / "projects"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "gi-scenario-studio" / "projects"


def _normalize_provider_host(value: str) -> str:
    host = str(value).strip().rstrip(".").casefold()
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid data-provider host: {value!r}") from exc
    if len(host) > 253 or any(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
        for label in host.split(".")
    ):
        raise ValueError(f"invalid data-provider host: {value!r}")
    return host


def _safe_asset_filename(value: str) -> str:
    leaf = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    normalized = re.sub(r"[^A-Za-z0-9._-]", "_", leaf).strip("._-")
    return normalized[:120] or "downloaded-data"


def _atomic_write_bytes(target: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".asset-",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _copy_content_addressed_asset(source: Path, target_directory: Path) -> tuple[Path, str, int]:
    digest = hashlib.sha256()
    size = 0
    temporary_path: Path | None = None
    try:
        with (
            source.open("rb") as source_handle,
            tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".asset-",
                dir=target_directory,
                delete=False,
            ) as target_handle,
        ):
            temporary_path = Path(target_handle.name)
            for block in iter(lambda: source_handle.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
                target_handle.write(block)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        checksum = digest.hexdigest()
        target = target_directory / f"{checksum[:16]}-{_safe_asset_filename(source.name)}"
        os.replace(temporary_path, target)
        temporary_path = None
        return target, checksum, size
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _download_https_pinned(url: str, *, max_bytes: int) -> bytes:
    """Download once without redirects, pinning DNS to a validated public address."""

    parsed = urllib.parse.urlparse(url)
    hostname = _normalize_provider_host(parsed.hostname or "")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.port or 443) != 443
        or any(character in url for character in "\r\n\0")
    ):
        raise ValueError("only canonical HTTPS data-provider URLs on port 443 are supported")
    port = parsed.port or 443
    addresses = []
    for result in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(result[4][0])
        if not address.is_global:
            continue
        normalized_address = str(address)
        if normalized_address not in addresses:
            addresses.append(normalized_address)
    if not addresses:
        raise ValueError("provider host did not resolve to a permitted public address")
    raw_socket = socket.create_connection((addresses[0], port), timeout=30)
    tls_socket = None
    try:
        context = ssl.create_default_context()
        tls_socket = context.wrap_socket(raw_socket, server_hostname=hostname)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            request_target = path.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("data-provider URL path and query must be percent-encoded") from exc
        if any(byte < 0x20 or byte == 0x7F for byte in request_target):
            raise ValueError("data-provider URL contains unsafe control characters")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostname}\r\n"
            "User-Agent: GI-Scenario-Studio/0.1\r\n"
            "Accept: application/octet-stream,*/*;q=0.1\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        tls_socket.sendall(request)
        response = http.client.HTTPResponse(tls_socket)
        response.begin()
        if 300 <= response.status < 400:
            raise ValueError("data-provider redirects are not accepted; configure the final URL")
        if response.status != 200:
            raise ValueError(f"data provider returned HTTP {response.status}")
        declared = response.getheader("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("download exceeds the approved size limit")
        content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError("download exceeds the approved size limit")
        return content
    finally:
        try:
            (tls_socket or raw_socket).close()
        except OSError:
            pass


def _project_summary(
    project: dict[str, Any],
    directory: Path,
    *,
    kind: str,
    model_file: str,
) -> dict[str, Any]:
    return {
        "id": project["project_id"],
        "name": project["title"],
        "kind": kind,
        "readiness": project["readiness"],
        "location": str(directory),
        "modelFile": model_file,
        "updatedAt": project["updated_at"],
    }


def _load_authoritative_run(
    repository: ProjectRepository,
    job_id: str,
    *,
    expected_analysis_id: str | None = None,
    expected_project_id: str | None = None,
) -> dict[str, Any]:
    """Rebuild one exact result exclusively from repository-owned state.

    The database identifies the run, metrics, and declared hashes. Every
    artifact is then checked against the bytes in the content-addressed store
    before any exact-result claim is returned or exported.
    """

    row = repository._connection.execute(
        "SELECT j.job_id,j.analysis_id,j.design_id,j.storm_id,j.state,"
        "j.physics_cache_key,j.payload_json,j.updated_at,a.project_id "
        "FROM solver_jobs j JOIN analyses a ON a.analysis_id=j.analysis_id "
        "WHERE j.job_id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"solver job {job_id!r} was not found")
    if row["state"] != "SUCCEEDED":
        raise ValueError(
            f"solver job {job_id!r} is {row['state']}, not an authoritative SUCCEEDED run"
        )
    if expected_analysis_id is not None and row["analysis_id"] != expected_analysis_id:
        raise ValueError(
            f"solver job {job_id!r} belongs to analysis {row['analysis_id']!r}, "
            f"not {expected_analysis_id!r}"
        )
    if expected_project_id is not None and row["project_id"] != expected_project_id:
        raise ValueError(f"solver job {job_id!r} belongs to a different project")
    try:
        job_payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"solver job {job_id!r} has invalid persisted payload JSON") from exc
    if not isinstance(job_payload, dict):
        raise TypeError(f"solver job {job_id!r} has a non-object persisted payload")

    solver_sha256 = job_payload.get("solver_sha256")
    _require_sha256(solver_sha256, label=f"solver hash for job {job_id!r}")

    artifact_rows = repository._connection.execute(
        "SELECT role,sha256,relative_path FROM run_artifacts WHERE job_id=? ORDER BY role",
        (job_id,),
    ).fetchall()
    artifacts: list[dict[str, Any]] = []
    artifact_hashes: dict[str, str] = {}
    for artifact_row in artifact_rows:
        role = str(artifact_row["role"])
        declared_sha256 = str(artifact_row["sha256"])
        _require_sha256(
            declared_sha256,
            label=f"{role} artifact hash for job {job_id!r}",
        )
        path = _artifact_path(repository.root, str(artifact_row["relative_path"]))
        actual_sha256 = _sha256_path(path)
        if actual_sha256 != declared_sha256:
            raise ValueError(
                f"{role} artifact bytes do not match the recorded hash for job {job_id!r}"
            )
        artifact_hashes[role] = declared_sha256
        artifacts.append(
            {
                "role": role,
                "sha256": declared_sha256,
                "relative_path": str(artifact_row["relative_path"]),
                "bytes": path.stat().st_size,
            }
        )
    missing_roles = sorted({"input", "output"}.difference(artifact_hashes))
    if missing_roles:
        raise ValueError(
            f"solver job {job_id!r} is missing required artifacts: {', '.join(missing_roles)}"
        )
    if "solver" in artifact_hashes and artifact_hashes["solver"] != solver_sha256:
        raise ValueError(
            f"solver artifact does not match the recorded solver hash for job {job_id!r}"
        )
    for payload_key, role in (("input_sha256", "input"), ("output_sha256", "output")):
        persisted = job_payload.get(payload_key)
        if persisted is not None and persisted != artifact_hashes[role]:
            raise ValueError(f"{payload_key} conflicts with the {role} artifact for job {job_id!r}")

    metric_rows = repository._connection.execute(
        "SELECT metric_id,object_id,value,units FROM run_metrics "
        "WHERE job_id=? ORDER BY metric_id,object_id",
        (job_id,),
    ).fetchall()
    metrics: dict[str, float] = {}
    metric_records: list[dict[str, Any]] = []
    for metric_row in metric_rows:
        metric_id = str(metric_row["metric_id"])
        object_id = str(metric_row["object_id"])
        value = float(metric_row["value"])
        if not math.isfinite(value):
            raise ValueError(f"metric {metric_id!r} for job {job_id!r} is not finite")
        key = metric_id if not object_id else f"{metric_id} [{object_id}]"
        if key in metrics:
            raise ValueError(f"job {job_id!r} contains ambiguous metric key {key!r}")
        metrics[key] = value
        metric_records.append(
            {
                "metric_id": metric_id,
                "object_id": object_id,
                "value": value,
                "units": str(metric_row["units"]),
            }
        )

    qa_rows = repository._connection.execute(
        "SELECT severity,code,payload_json FROM qa_findings WHERE job_id=? ORDER BY finding_id",
        (job_id,),
    ).fetchall()
    qa_records: list[dict[str, Any]] = []
    qa_findings: list[str] = []
    derived_qa = "accepted"
    rank = {"accepted": 0, "warning": 1, "rejected": 2}
    persisted_qa = str(job_payload.get("qa_status", "accepted")).casefold()
    if persisted_qa not in rank:
        raise ValueError(f"solver job {job_id!r} has invalid persisted QA status")
    derived_qa = persisted_qa
    for qa_row in qa_rows:
        try:
            finding_payload = json.loads(qa_row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"solver job {job_id!r} has invalid QA finding JSON") from exc
        if not isinstance(finding_payload, dict):
            raise TypeError(f"solver job {job_id!r} has a non-object QA finding")
        severity = str(qa_row["severity"]).casefold()
        code = str(qa_row["code"])
        status = (
            "rejected"
            if severity in {"blocking", "error", "critical", "rejected"}
            else "warning"
            if severity in {"warning", "warn"}
            else "accepted"
        )
        if rank[status] > rank[derived_qa]:
            derived_qa = status
        message = str(finding_payload.get("message") or finding_payload.get("user_message") or code)
        qa_findings.append(f"[{severity}] {code}: {message}")
        qa_records.append(
            {
                "severity": severity,
                "code": code,
                "details": finding_payload,
            }
        )

    result = ExactRunResult(
        run_id=row["job_id"],
        analysis_id=row["analysis_id"],
        design_id=row["design_id"],
        storm_id=row["storm_id"],
        metrics=metrics,
        qa_status=derived_qa,
        qa_findings=qa_findings,
        input_sha256=artifact_hashes["input"],
        solver_sha256=solver_sha256,
        output_sha256=artifact_hashes["output"],
        cache_hit=bool(job_payload.get("cache_hit", False)),
        completed_at=row["updated_at"],
    )
    return {
        "result": result,
        "project_id": row["project_id"],
        "physics_cache_key": row["physics_cache_key"],
        "metric_records": metric_records,
        "qa_records": qa_records,
        "artifacts": artifacts,
    }


def _artifact_path(project_root: Path, relative_path: str) -> Path:
    raw = Path(relative_path)
    if raw.is_absolute():
        raise ValueError("run artifact path must be relative to the project")
    root = project_root.resolve()
    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("run artifact path escapes the project directory")
    if not candidate.is_file():
        raise ValueError(f"run artifact is missing: {relative_path}")
    return candidate


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_sha256(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} is not a lowercase SHA-256 digest")


def _export_artifact_name(job_id: str, role: str) -> str | None:
    filenames = {"input": "input.inp", "report": "report.rpt", "output": "output.out"}
    filename = filenames.get(role)
    if filename is None:
        return None
    safe_job_id = re.sub(r"[^A-Za-z0-9._-]", "_", job_id).strip("._-") or "job"
    discriminator = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]
    return f"runs/{safe_job_id}-{discriminator}/{filename}"


def handle_request(dispatcher: RPCDispatcher, request: Any) -> dict[str, Any] | None:
    if (
        not isinstance(request, dict)
        or request.get("jsonrpc") != "2.0"
        or not isinstance(request.get("method"), str)
    ):
        raise RPCFault(-32600, "Invalid Request")
    request_id = request.get("id")
    result = dispatcher.dispatch(request["method"], request.get("params"))
    if "id" not in request:  # JSON-RPC notification
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, fault: RPCFault) -> dict[str, Any]:
    error: dict[str, Any] = {"code": fault.code, "message": fault.message}
    if fault.data is not None:
        error["data"] = fault.data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def serve_jsonl(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    data_root: str | Path | None = None,
    allowed_roots: list[str | Path] | None = None,
    provider_hosts: set[str] | None = None,
    solver_executable: str | Path | None = None,
    solver_sha256: str | None = None,
) -> int:
    write_lock = threading.Lock()

    def emit_event(params: dict[str, Any]) -> None:
        method = str(params["event"])
        event_params = {"sequence": params["sequence"], **params["payload"]}
        with write_lock:
            output_stream.write(
                json.dumps(
                    {"jsonrpc": "2.0", "method": method, "params": event_params},
                    separators=(",", ":"),
                )
                + "\n"
            )
            output_stream.flush()

    dispatcher = RPCDispatcher(
        event_sink=emit_event,
        data_root=data_root,
        allowed_roots=allowed_roots,
        provider_hosts=provider_hosts,
        solver_executable=solver_executable,
        solver_sha256=solver_sha256,
    )
    for line in input_stream:
        if not line.strip():
            continue
        request_id: Any = None
        try:
            request = json.loads(line)
            if isinstance(request, dict):
                request_id = request.get("id")
            response = handle_request(dispatcher, request)
        except json.JSONDecodeError as exc:
            response = _error_response(None, RPCFault(-32700, "Parse error", {"detail": str(exc)}))
        except RPCFault as fault:
            response = _error_response(request_id, fault)
        except Exception as exc:  # noqa: BLE001 - last-resort protocol containment
            traceback.print_exc(file=sys.stderr)
            response = _error_response(
                request_id,
                RPCFault(-32603, "Internal error", {"diagnostic": type(exc).__name__}),
            )
        if response is not None:
            with write_lock:
                output_stream.write(json.dumps(response, separators=(",", ":"), default=str) + "\n")
                output_stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["jsonl-stdio"], default="jsonl-stdio")
    parser.add_argument("--api-version", default=API_VERSION)
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--allowed-root", action="append", default=[])
    parser.add_argument("--provider-host", action="append", default=[])
    parser.add_argument("--solver-executable", default=None)
    parser.add_argument("--solver-sha256", default=None)
    args = parser.parse_args(argv)
    if args.api_version != API_VERSION:
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32001,
                        "message": "Unsupported API version",
                        "data": {"supported": API_VERSION, "requested": args.api_version},
                    },
                },
                separators=(",", ":"),
            )
        )
        return 2
    return serve_jsonl(
        sys.stdin,
        sys.stdout,
        data_root=args.data_root,
        allowed_roots=args.allowed_root,
        provider_hosts=set(args.provider_host) if args.provider_host else None,
        solver_executable=args.solver_executable,
        solver_sha256=args.solver_sha256,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
