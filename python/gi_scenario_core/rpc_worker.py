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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from .bundle import inspect_bundle, safe_extract_bundle
from .concept import generate_concept_model
from .contracts import (
    CONCEPTUAL_NOTICE,
    FULL_EVIDENCE_SYNC_CATEGORIES,
    PUBLIC_CONTRACTS,
    AnalysisRunPlan,
    AssistantAnswer,
    AssistantEvaluationRecord,
    AssistantEvidenceReference,
    AssistantNumericClaim,
    AssistantProposedAction,
    CandidateSlot,
    CloudArtifactManifest,
    DecisionBrief,
    ExactRunResult,
    GuidedCheckpoint,
    ModelFitnessPassport,
    ModelRevision,
    ObservabilityAccountState,
    ObservabilityBinding,
    ProjectDataPolicy,
    ProjectKind,
    SimulationBudget,
    TelemetryEnvelope,
    new_id,
    public_schemas,
)
from .demo import create_demo_project
from .export import create_evidence_bundle, validate_export
from .observability import ObservabilityService
from .orchestrator import AnalysisOrchestrator, StormModel
from .repository import ProjectRepository
from .search import nondominated_indices
from .solver import SolverRunner
from .swmm import SwmmDocument
from .validation import validate_model

API_VERSION = "2.0"
METRIC_EXTRACTOR_VERSION = "swmm-report-metrics-v2"
TELEMETRY_BATCH_JOB_LIMIT = 128
TELEMETRY_BATCH_BYTE_LIMIT = 512 * 1024**2


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
        guided_fixture_path: str | Path | None = None,
        guided_evidence_directory: str | Path | None = None,
        require_observability_session: bool = False,
        observability_adapters: dict[Any, Any] | None = None,
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
        repository_root = Path(__file__).resolve().parents[2]
        default_projection = (
            repository_root / "apps" / "desktop" / "src" / "data" / "guidedFixture.json"
        )
        default_evidence = repository_root / "benchmarks" / "guided_exact"
        self.guided_fixture_path = (
            Path(guided_fixture_path or default_projection).expanduser().resolve()
        )
        self.guided_evidence_directory = (
            Path(guided_evidence_directory or default_evidence).expanduser().resolve()
        )
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gi-solver")
        self._background: dict[str, dict[str, Any]] = {}
        self._background_lock = threading.RLock()
        self._cloud_lifecycle_lock = threading.RLock()
        self.require_observability_session = require_observability_session
        self._observability_account: dict[str, Any] | None = None
        self._observability_account_block: dict[str, Any] | None = None
        adapters = observability_adapters or {}
        self.observability = ObservabilityService(
            wandb_adapter=adapters.get("wandb"),
            weave_adapter=adapters.get("weave"),
        )
        self._methods: dict[str, Callable[[dict[str, Any]], Any]] = {
            "system.ping": self.system_ping,
            "schema.list": self.schema_list,
            "schema.get": self.schema_get,
            "project.create": self.project_create,
            "project.open": self.project_open,
            "project.get_state": self.project_get_state,
            "bundle.inspect": self.bundle_inspect,
            "bundle.import": self.bundle_import,
            "bundle.select_base_model": self.bundle_select_base_model,
            "data_asset.add": self.data_asset_add,
            "data_asset.download": self.data_asset_download,
            "concept.preview": self.concept_preview,
            "concept.generate": self.concept_generate,
            "concept.update_object": self.concept_update_object,
            "model.validate": self.model_validate,
            "baseline.plan": self.baseline_plan,
            "baseline.start": self.baseline_start,
            "analysis.plan": self.analysis_plan,
            "analysis.start": self.analysis_start,
            "analysis.pause": self.analysis_pause,
            "analysis.resume": self.analysis_resume,
            "analysis.cancel": self.analysis_cancel,
            "results.query": self.results_query,
            "results.compare": self.results_compare,
            "results.decision_board": self.results_decision_board,
            "evidence.resolve": self.evidence_resolve,
            "observability.connect": self.observability_connect,
            "observability.bind_project": self.observability_bind_project,
            "observability.disconnect": self.observability_disconnect,
            "observability.validate_binding": self.observability_validate_binding,
            "observability.get_status": self.observability_get_status,
            "observability.preview_upload": self.observability_preview_upload,
            "observability.sync": self.observability_sync,
            "observability.retry": self.observability_retry,
            "observability.open_run": self.observability_open_run,
            "observability.open_artifact": self.observability_open_artifact,
            "observability.open_trace": self.observability_open_trace,
            "assistant.explain": self.assistant_explain,
            "assistant.follow_up": self.assistant_follow_up,
            "assistant.feedback": self.assistant_feedback,
            "assistant.get_trace": self.assistant_get_trace,
            "guided.open": self.guided_open,
            "guided.get_state": self.guided_get_state,
            "guided.rerun": self.guided_rerun,
            "guided.pause": self.guided_pause,
            "guided.resume": self.guided_resume,
            "guided.cancel": self.guided_cancel,
            "decision_brief.get": self.decision_brief_get,
            "decision_brief.update": self.decision_brief_update,
            "model.get_fitness": self.model_get_fitness,
            "model.attest": self.model_attest,
            "export.validate": self.export_validate,
            "export.create": self.export_create,
            "diagnostics.create_bundle": self.diagnostics_create_bundle,
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

    def _read_guided_projection(self, path: Path | None = None) -> tuple[dict[str, Any], bytes]:
        source = path or self.guided_fixture_path
        if not source.is_file():
            raise ValueError("the hash-verified guided fixture projection is not installed")
        content = source.read_bytes()
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("the guided fixture projection is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise TypeError("the guided fixture projection must be a JSON object")
        accounting = payload.get("job_accounting")
        if (
            payload.get("schema_version") != "2.0"
            or payload.get("fixture_id") != "cedar_trace_exact_v2"
            or not isinstance(accounting, dict)
            or accounting.get("completed_jobs") != 884
            or accounting.get("expected_jobs") != 884
            or accounting.get("retained_jobs") != 20
        ):
            raise ValueError("the guided fixture projection failed its pinned shape checks")
        source_manifest_sha256 = payload.get("source_manifest_sha256")
        _require_sha256(source_manifest_sha256, label="guided source-manifest hash")
        return payload, content

    def _install_guided_evidence(
        self,
        project_directory: Path,
        project_id: str,
    ) -> dict[str, Any]:
        projection, projection_bytes = self._read_guided_projection()
        source_root = self.guided_evidence_directory
        manifest_path = source_root / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("the guided exact-run manifest is not installed")
        manifest_bytes = manifest_path.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_sha256 != projection["source_manifest_sha256"]:
            raise ValueError("guided projection and exact-run manifest hashes do not match")
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError as exc:
            raise ValueError("the guided exact-run manifest is not valid JSON") from exc
        target_root = self.require_contained_path(
            project_directory,
            "guided_fixture",
            purpose="guided fixture directory",
        )
        target_root.mkdir(parents=True, exist_ok=True)
        installed: list[tuple[str, str, str, dict[str, Any]]] = []

        def install(relative: str, content: bytes, role: str, payload: dict[str, Any]) -> None:
            destination = self.require_contained_path(
                target_root,
                relative,
                purpose=f"guided {role}",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(destination, content)
            digest = hashlib.sha256(content).hexdigest()
            installed.append(
                (
                    role,
                    destination.relative_to(project_directory).as_posix(),
                    digest,
                    payload,
                )
            )

        install("ui-projection.json", projection_bytes, "guided-fixture-projection", {})
        install("manifest.json", manifest_bytes, "guided-exact-manifest", {})
        story_path = source_root / "story.json"
        if story_path.is_file():
            install("story.json", story_path.read_bytes(), "guided-story", {})
        retained_runs = [
            item
            for item in manifest.get("runs", [])
            if isinstance(item, dict) and item.get("artifacts_retained") is True
        ]
        if len(retained_runs) != 20:
            raise ValueError("the guided manifest must retain exactly 20 complete run sets")
        for run in retained_runs:
            for role in ("input", "report", "output"):
                relative = run.get("artifacts", {}).get(role)
                expected = run.get("hashes", {}).get(f"{role}_sha256")
                _require_sha256(expected, label=f"guided {role} hash")
                if not isinstance(relative, str):
                    raise TypeError("retained guided evidence has a missing artifact path")
                source = (source_root / relative).resolve()
                if source_root.resolve() not in source.parents or not source.is_file():
                    raise ValueError("retained guided evidence contains an unsafe artifact path")
                artifact_bytes = source.read_bytes()
                if hashlib.sha256(artifact_bytes).hexdigest() != expected:
                    raise ValueError("retained guided artifact bytes failed SHA-256 verification")
                install(
                    relative,
                    artifact_bytes,
                    f"guided-exact-{role}",
                    {"run_id": run["run_id"], "storm_id": run["storm_id"]},
                )
        repository = ProjectRepository(project_directory)
        try:
            for role, relative, digest, payload in installed:
                repository.record_project_asset(
                    project_id,
                    role=role,
                    relative_path=relative,
                    sha256=digest,
                    payload=payload,
                )
            project = repository.get_project(project_id)
            state = dict(project["state"])
            state.update(
                {
                    "guided_fixture_sha256": hashlib.sha256(projection_bytes).hexdigest(),
                    "guided_manifest_sha256": manifest_sha256,
                    "guided_retained_run_sets": len(retained_runs),
                }
            )
            repository.set_project_state(project_id, state)
        finally:
            repository.close()
        return {
            "fixture": projection,
            "fixture_sha256": hashlib.sha256(projection_bytes).hexdigest(),
            "manifest_sha256": manifest_sha256,
            "retained_run_sets": len(retained_runs),
            "installed_assets": len(installed),
        }

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
        return {
            "api_version": API_VERSION,
            "status": "ready",
            "methods": self.methods,
            "capabilities": {
                "exact_solver_configured": self.solver_executable is not None,
                "guided_fixture_installed": (
                    self.guided_fixture_path.is_file()
                    and (self.guided_evidence_directory / "manifest.json").is_file()
                ),
                "required_observability": self.require_observability_session,
            },
        }

    def schema_list(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"api_version": API_VERSION, "contracts": sorted(PUBLIC_CONTRACTS)}

    def schema_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _required(params, "name")
        schemas = public_schemas()
        if name not in schemas:
            raise KeyError(f"unknown public contract {name!r}")
        return {"name": name, "schema": schemas[name]}

    def project_create(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("kind", params.get("project_kind"))
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
            existing_project_id: str | None = None
            prospective_project_id = str(params.get("project_id") or new_id("project"))
            documents = self._account_binding_documents(prospective_project_id)
            if documents is not None:
                existing_project_id = prospective_project_id
                policy, binding = documents
                repository = ProjectRepository(directory)
                try:
                    repository.create_project_with_observability(
                        title="Cedar Trace teaching watershed",
                        project_kind=ProjectKind.DEMO,
                        project_id=existing_project_id,
                        state={"workflow": "guided_example", "result_mode": "installing"},
                        policy=policy,
                        binding=binding,
                    )
                    self._observability_account["consumed_project_id"] = existing_project_id
                finally:
                    repository.close()
            created = create_demo_project(
                directory,
                existing_project_id=existing_project_id,
            )
            project = created["project"]
            installed = self._install_guided_evidence(directory, project["project_id"])
            if existing_project_id is None:
                repository = ProjectRepository(directory)
                try:
                    self._persist_account_binding(repository, project["project_id"])
                finally:
                    repository.close()
            summary = _project_summary(
                project, directory, kind="demo", model_file="cedar-trace-reference.inp"
            )
            summary["guidedFixtureSha256"] = installed["fixture_sha256"]
            self.emit("project.updated", {"project_id": project["project_id"], "demo": True})
            return summary
        title = params.get("title") or (
            "New conceptual study" if kind == "conceptual" else "Imported SWMM project"
        )
        if kind is None:
            raise ValueError("project_kind must be explicit; model fitness is assessed separately")
        repository = ProjectRepository(directory)
        try:
            project_id = str(params.get("project_id") or new_id("project"))
            documents = self._account_binding_documents(project_id)
            if documents is None:
                project = repository.create_project(
                    title=title,
                    project_kind=ProjectKind(kind),
                    project_id=project_id,
                    state=params.get("state"),
                )
            else:
                policy, binding = documents
                created = repository.create_project_with_observability(
                    title=title,
                    project_kind=ProjectKind(kind),
                    project_id=project_id,
                    state=params.get("state"),
                    policy=policy,
                    binding=binding,
                )
                project = created["project"]
                assert self._observability_account is not None
                self._observability_account["consumed_project_id"] = project_id
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

    def _persist_account_binding(
        self,
        repository: ProjectRepository,
        project_id: str,
    ) -> dict[str, Any] | None:
        account = self._observability_account
        if account is None:
            return None
        documents = self._account_binding_documents(project_id)
        if documents is None:
            return None
        policy, binding = documents
        repository.upsert_project_data_policy(policy)
        repository.connect_observability_binding(binding)
        account["consumed_project_id"] = project_id
        return self.observability.status(repository, project_id).model_dump(mode="json")

    def _account_binding_documents(
        self,
        project_id: str,
    ) -> tuple[ProjectDataPolicy, ObservabilityBinding] | None:
        account = self._observability_account
        if account is None:
            return None
        consumed_project_id = account.get("consumed_project_id")
        if consumed_project_id not in {None, project_id}:
            return None
        template = account["binding"]
        policy = ProjectDataPolicy(
            project_id=project_id,
            classification=account["classification"],
            deployment=template["deployment"],
            acknowledged_by=account["acknowledged_by"],
        )
        binding = ObservabilityBinding(
            project_id=project_id,
            data_policy_id=policy.policy_id,
            entity=template["entity"],
            external_project=template["external_project"],
            external_project_id=template["external_project_id"],
            api_base_url=template["api_base_url"],
            web_base_url=template["web_base_url"],
            deployment=template["deployment"],
            mode="online",
            verified=True,
            verified_at=template["verified_at"],
        )
        return policy, binding

    def project_open(self, params: dict[str, Any]) -> dict[str, Any]:
        project_directory = self.require_allowed_path(
            _required(params, "project_directory"), purpose="project directory"
        )
        repository = ProjectRepository(project_directory)
        try:
            project_id = params.get("project_id")
            if project_id is None:
                row = repository._connection.execute(  # intentionally read-only internal lookup
                    "SELECT project_id FROM projects ORDER BY created_at LIMIT 1"
                ).fetchone()
                if row is None:
                    raise KeyError("project directory does not contain a project")
                project_id = row["project_id"]
            project = repository.get_project(project_id)
            with self._background_lock:
                active_outbox_ids = {
                    operation_id.removeprefix("telemetry:")
                    for operation_id, background in self._background.items()
                    if operation_id.startswith("telemetry:")
                    and (background.get("future") is None or not background["future"].done())
                }
            due = repository.recover_abandoned_telemetry_uploads(active_outbox_ids)
            cloud_state = self.observability.status(repository, project_id)
        finally:
            repository.close()
        if cloud_state.can_sync:
            for item in due:
                if item["envelope"]["project_id"] == project_id:
                    self._schedule_telemetry_sync(project_directory, item["outbox_id"])
        return project

    def project_get_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.project_open(params)

    def bundle_inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"archive_path", "expected_sha256", "expected_bytes"})
        archive = self.require_allowed_path(
            _required(params, "archive_path"), purpose="project bundle"
        )
        expected_sha256, expected_bytes = _required_bundle_identity(params)
        return inspect_bundle(
            archive,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
        ).to_dict()

    def _activate_imported_base_model(
        self,
        *,
        repository: ProjectRepository,
        project_directory: Path,
        project_id: str,
        state: dict[str, Any],
        candidate: dict[str, Any],
        created_by: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create the immutable revision and default passport for one imported candidate."""

        project_path = str(candidate.get("project_path", ""))
        model_sha256 = str(candidate.get("model_sha256", ""))
        dependencies = candidate.get("dependencies")
        dependency_hashes = candidate.get("dependency_hashes")
        if (
            not project_path
            or not re.fullmatch(r"[0-9a-f]{64}", model_sha256)
            or not isinstance(dependencies, dict)
            or not isinstance(dependency_hashes, dict)
            or set(dependencies) != set(dependency_hashes)
        ):
            raise ValueError("the imported base-model candidate record is incomplete")

        base_model = self.require_contained_path(
            project_directory,
            project_path,
            purpose="imported base model",
        )
        if not base_model.is_file() or base_model.is_symlink():
            raise ValueError("the selected imported base model is not a regular project file")
        if _sha256_path(base_model) != model_sha256:
            raise ValueError("the selected imported base model changed after bundle import")

        normalized_dependencies: dict[str, str] = {}
        normalized_dependency_hashes: dict[str, str] = {}
        for reference, relative_source in dependencies.items():
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError("the imported dependency map contains an invalid target")
            if not isinstance(relative_source, str):
                raise TypeError("the imported dependency map contains an invalid source")
            expected_hash = dependency_hashes.get(reference)
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                raise ValueError("the imported dependency record has an invalid hash")
            dependency = self.require_contained_path(
                project_directory,
                relative_source,
                purpose="imported model dependency",
            )
            if not dependency.is_file() or dependency.is_symlink():
                raise ValueError("an imported model dependency is not a regular project file")
            if _sha256_path(dependency) != expected_hash:
                raise ValueError("an imported model dependency changed after bundle import")
            normalized_dependencies[reference] = dependency.relative_to(
                project_directory
            ).as_posix()
            normalized_dependency_hashes[reference] = expected_hash

        document = SwmmDocument.parse(base_model.read_text(encoding="utf-8-sig", errors="replace"))
        validation = validate_model(document)
        subcatchment_ids = sorted(
            {row.tokens[0] for row in document.data_rows("SUBCATCHMENTS") if row.tokens}
        )
        lid_control_ids = sorted(
            {row.tokens[0] for row in document.data_rows("LID_CONTROLS") if row.tokens}
        )
        existing_lid_usage: list[dict[str, Any]] = []
        for row in document.data_rows("LID_USAGE"):
            if len(row.tokens) < 3:
                continue
            try:
                parsed_count = float(row.tokens[2])
                unit_count = int(parsed_count) if parsed_count.is_integer() else None
            except ValueError:
                unit_count = None
            existing_lid_usage.append(
                {
                    "subcatchment_id": row.tokens[0],
                    "lid_control": row.tokens[1],
                    "unit_count": unit_count,
                    "raw_unit_count": row.tokens[2],
                }
            )
        state["selected_base_model"] = base_model.relative_to(project_directory).as_posix()
        state["selected_model_dependencies"] = normalized_dependencies
        state["base_model_selection_required"] = False
        state["imported_model_roles"] = {
            str(item["project_path"]): (
                "authoritative_base"
                if item.get("project_path") == state["selected_base_model"]
                else "unclassified_companion"
            )
            for item in state.get("base_model_candidates", [])
            if isinstance(item, dict) and isinstance(item.get("project_path"), str)
        }
        state["import_preflight"] = {
            "blocking_count": validation.blocking_count,
            "warning_count": validation.warning_count,
            "structurally_runnable": validation.structurally_runnable,
            "section_names": validation.section_names,
            "object_counts": validation.object_counts,
            "checks": [item.model_dump(mode="json") for item in validation.checks],
            "issues": [item.model_dump(mode="json") for item in validation.issues],
            "setup_options": {
                "source": "parsed_model",
                "subcatchment_ids": subcatchment_ids,
                "lid_control_ids": lid_control_ids,
                "existing_lid_usage": existing_lid_usage,
            },
        }
        try:
            binding_id = repository.get_observability_binding(project_id)["binding_id"]
        except KeyError:
            binding_id = None
        revision = ModelRevision(
            project_id=project_id,
            reason="import",
            model_sha256=model_sha256,
            dependency_hashes=normalized_dependency_hashes,
            observability_binding_id=binding_id,
            created_by=created_by,
        )
        passport = ModelFitnessPassport(
            project_id=project_id,
            model_revision_id=revision.revision_id,
            intended_use=(
                "Unreviewed imported SWMM model; exact runs may be used for learning "
                "evidence only until an engineer attests fitness for a stated use."
            ),
            permitted_outputs=["learning evidence package"],
            prohibited_uses=[
                "design",
                "permitting",
                "flood-risk determination",
                "regulatory approval",
            ],
            unresolved_limitations=[
                (
                    "Model fitness, calibration, reference reproduction, assumptions, "
                    "and intended-use suitability have not been assessed."
                )
            ],
        )
        state["current_model_revision_id"] = revision.revision_id
        state["current_model_sha256"] = model_sha256
        state["model_fitness_passport_id"] = passport.passport_id
        repository.store_imported_model_record(revision, passport, state)
        return state, state["import_preflight"]

    def bundle_select_base_model(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "project_id", "model_path"})
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        project_id = str(_required(params, "project_id"))
        requested_model_path = str(_required(params, "model_path"))
        repository = ProjectRepository(project_directory)
        cloud_sync: dict[str, Any] = {"state": "local_only"}
        try:
            project = repository.get_project(project_id)
            if project["project_kind"] != ProjectKind.EXISTING.value:
                raise ValueError("base-model selection requires an existing-model project")
            state = dict(project["state"])
            if not state.get("bundle_imported"):
                raise ValueError("base-model selection requires an imported portable bundle")
            candidates = state.get("base_model_candidates")
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("the imported bundle has no persisted base-model candidates")
            matching = [
                item
                for item in candidates
                if isinstance(item, dict) and item.get("project_path") == requested_model_path
            ]
            if len(matching) != 1:
                raise ValueError(
                    "the selected base model is not one of this project's imported candidates"
                )
            if not state.get("base_model_selection_required"):
                if state.get("selected_base_model") != requested_model_path:
                    raise ValueError(
                        "this project already has a different authoritative base model"
                    )
                return {
                    "project_state": state,
                    "model_validation": state.get("import_preflight"),
                }
            state, validation = self._activate_imported_base_model(
                repository=repository,
                project_directory=project_directory,
                project_id=project_id,
                state=state,
                candidate=matching[0],
                created_by="bundle.select_base_model",
            )
            try:
                binding = repository.get_observability_binding(project_id)
            except KeyError:
                pass
            else:
                try:
                    revision_id = str(state["current_model_revision_id"])
                    passport_id = str(state["model_fitness_passport_id"])
                    selected_source = self.require_contained_path(
                        project_directory,
                        str(matching[0]["project_path"]),
                        purpose="selected base-model cloud evidence",
                    )
                    manifests = [
                        self._cloud_manifest_for_file(
                            repository,
                            project_id=project_id,
                            category="model",
                            source_object_id=revision_id,
                            logical_name=f"model-revision/{revision_id}/base.inp",
                            source=selected_source,
                        )
                    ]
                    for index, relative_source in enumerate(
                        sorted(set(matching[0]["dependencies"].values()))
                    ):
                        dependency_source = self.require_contained_path(
                            project_directory,
                            relative_source,
                            purpose="selected base-model dependency cloud evidence",
                        )
                        manifests.append(
                            self._cloud_manifest_for_file(
                                repository,
                                project_id=project_id,
                                category="model",
                                source_object_id=revision_id,
                                logical_name=(
                                    f"model-revision/{revision_id}/dependencies/"
                                    f"{index:03d}-{dependency_source.name}"
                                ),
                                source=dependency_source,
                            )
                        )
                    model_rows = sorted(
                        [
                            [
                                str(item["project_path"]),
                                state["imported_model_roles"].get(
                                    str(item["project_path"]), "unclassified_companion"
                                ),
                                str(item["model_sha256"]),
                            ]
                            for item in candidates
                            if isinstance(item, dict) and isinstance(item.get("project_path"), str)
                        ],
                        key=lambda row: row[0],
                    )
                    stable = hashlib.sha256(
                        f"{project_id}|base-model-selection|{revision_id}".encode()
                    ).hexdigest()[:24]
                    envelope = TelemetryEnvelope(
                        envelope_id=f"envelope_{stable}",
                        project_id=project_id,
                        binding_id=binding["binding_id"],
                        category="engineering_evidence",
                        event_type="bundle_import",
                        subject_id=revision_id,
                        idempotency_key=f"base-model-selection:{revision_id}",
                        metadata={
                            "schema_version": "2.0",
                            "event_phase": "base_model_selection",
                            "selection_policy": (
                                "single_authoritative_base_with_unclassified_companions_blocked"
                            ),
                            "selected_base_model": state["selected_base_model"],
                            "model_revision_id": revision_id,
                            "model_sha256": state["current_model_sha256"],
                            "model_fitness_passport_id": passport_id,
                            "dependency_hashes": matching[0]["dependency_hashes"],
                            "tables": {
                                "imported_models": {
                                    "columns": ["project_path", "role", "sha256"],
                                    "rows": model_rows,
                                }
                            },
                        },
                        artifacts=manifests,
                    )
                    queued = self._enqueue_and_schedule_telemetry(repository, envelope)
                    cloud_sync = {
                        "outbox_id": queued["outbox_id"],
                        "envelope_id": envelope.envelope_id,
                        "state": queued["state"],
                    }
                except Exception as cloud_error:  # noqa: BLE001 - selection remains local truth
                    cloud_sync = {
                        "state": "projection_failed",
                        "error_code": type(cloud_error).__name__.upper()[:64],
                    }
        finally:
            repository.close()
        self.emit(
            "project.updated",
            {"project_id": project_id, "selected_base_model": requested_model_path},
        )
        return {
            "project_state": state,
            "model_validation": validation,
            "cloud_sync": cloud_sync,
        }

    def bundle_import(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(
            params,
            {
                "archive_path",
                "expected_sha256",
                "expected_bytes",
                "destination",
                "project_directory",
                "project_id",
            },
        )
        archive = self.require_allowed_path(
            _required(params, "archive_path"), purpose="project bundle"
        )
        expected_sha256, expected_bytes = _required_bundle_identity(params)
        project_directory_value = params.get("project_directory")
        if project_directory_value is not None:
            project_directory = self.require_project_directory(project_directory_value)
            destination = self.require_contained_path(
                project_directory,
                Path(str(_required(params, "destination"))).relative_to(project_directory),
                purpose="bundle destination",
            )
        else:
            project_directory = None
            destination = self.require_allowed_path(
                _required(params, "destination"), purpose="bundle destination"
            )
        preflight = inspect_bundle(
            archive,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
        )
        if project_directory is not None and not preflight.valid:
            missing = sorted({item for model in preflight.models for item in model.missing})
            detail = ", ".join(missing[:8]) if missing else "no SWMM .inp model was found"
            raise ValueError(f"project bundle is not dependency-complete: {detail}")
        inspection = safe_extract_bundle(
            archive,
            destination,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
        )
        payload = inspection.to_dict()
        if project_directory is not None:
            project_id = str(_required(params, "project_id"))
            repository = ProjectRepository(project_directory)
            try:
                project = repository.get_project(project_id)
                if project["project_kind"] != ProjectKind.EXISTING.value:
                    raise ValueError(
                        "portable SWMM bundle import requires an existing-model project"
                    )
                file_rows: list[list[Any]] = []
                for item in inspection.files:
                    extracted = self.require_contained_path(
                        destination,
                        item.path,
                        purpose="extracted bundle file",
                    )
                    relative = extracted.relative_to(project_directory).as_posix()
                    repository.record_project_asset(
                        project_id,
                        role=item.role,
                        relative_path=relative,
                        sha256=item.sha256,
                        payload={"bytes": item.size},
                    )
                    file_rows.append([item.path, item.role, item.size, item.sha256])
                file_hashes = {item.path: item.sha256 for item in inspection.files}
                base_model_candidates: list[dict[str, Any]] = []
                for model in inspection.models:
                    if len(model.references) != len(model.resolved):
                        raise ValueError("the imported dependency map is incomplete")
                    project_model = self.require_contained_path(
                        destination,
                        model.model_path,
                        purpose="imported base-model candidate",
                    )
                    dependencies = {
                        reference: self.require_contained_path(
                            destination,
                            resolved,
                            purpose="imported model dependency",
                        )
                        .relative_to(project_directory)
                        .as_posix()
                        for reference, resolved in zip(
                            model.references,
                            model.resolved,
                            strict=True,
                        )
                    }
                    base_model_candidates.append(
                        {
                            "model_path": model.model_path,
                            "project_path": project_model.relative_to(project_directory).as_posix(),
                            "model_sha256": file_hashes[model.model_path],
                            "dependencies": dependencies,
                            "dependency_hashes": {
                                reference: file_hashes[resolved]
                                for reference, resolved in zip(
                                    model.references,
                                    model.resolved,
                                    strict=True,
                                )
                            },
                        }
                    )
                state = dict(project["state"])
                state.update(
                    {
                        "bundle_imported": True,
                        "bundle_sha256": inspection.archive_sha256,
                        "base_models": [model.model_path for model in inspection.models],
                        "base_model_candidates": base_model_candidates,
                        "bundle_file_count": len(inspection.files),
                        "bundle_dependency_complete": inspection.valid,
                    }
                )
                if len(inspection.models) == 1:
                    state, payload["model_validation"] = self._activate_imported_base_model(
                        repository=repository,
                        project_directory=project_directory,
                        project_id=project_id,
                        state=state,
                        candidate=base_model_candidates[0],
                        created_by="bundle.import",
                    )
                else:
                    state["base_model_selection_required"] = True
                    state["imported_model_roles"] = {
                        str(item["project_path"]): "unclassified_companion"
                        for item in base_model_candidates
                    }
                    repository.set_project_state(project_id, state)
                payload["project_state"] = state
                try:
                    binding = repository.get_observability_binding(project_id)
                except KeyError:
                    payload["cloud_sync"] = {"state": "local_only"}
                else:
                    try:
                        manifests = [
                            self._cloud_manifest_for_file(
                                repository,
                                project_id=project_id,
                                category="model" if item.role == "swmm_model" else "storm",
                                source_object_id=f"bundle:{project_id}",
                                logical_name=f"source-bundle/{item.path}",
                                source=self.require_contained_path(
                                    destination,
                                    item.path,
                                    purpose="extracted bundle cloud evidence",
                                ),
                            )
                            for item in inspection.files
                        ]
                        envelope_id = (
                            "envelope_"
                            + hashlib.sha256(
                                f"{project_id}|bundle|{state['bundle_sha256']}".encode()
                            ).hexdigest()[:24]
                        )
                        envelope = TelemetryEnvelope(
                            envelope_id=envelope_id,
                            project_id=project_id,
                            binding_id=binding["binding_id"],
                            category="engineering_evidence",
                            event_type="bundle_import",
                            subject_id=f"bundle:{state['bundle_sha256']}",
                            idempotency_key=f"bundle-import:{state['bundle_sha256']}",
                            metadata={
                                "schema_version": "2.0",
                                "bundle_sha256": state["bundle_sha256"],
                                "file_count": len(inspection.files),
                                "model_count": len(inspection.models),
                                "dependency_complete": inspection.valid,
                                "model_revision_id": state.get("current_model_revision_id"),
                                "model_fitness_passport_id": state.get("model_fitness_passport_id"),
                                "tables": {
                                    "source_files": {
                                        "columns": ["path", "role", "bytes", "sha256"],
                                        "rows": file_rows,
                                    }
                                },
                            },
                            artifacts=manifests,
                        )
                        queued = self._enqueue_and_schedule_telemetry(repository, envelope)
                        payload["cloud_sync"] = {
                            "outbox_id": queued["outbox_id"],
                            "envelope_id": envelope.envelope_id,
                            "state": queued["state"],
                        }
                    except Exception as cloud_error:  # noqa: BLE001 - import remains authoritative
                        payload["cloud_sync"] = {
                            "state": "projection_failed",
                            "error_code": type(cloud_error).__name__.upper()[:64],
                        }
            finally:
                repository.close()
        self.emit("project.updated", {"bundle_valid": inspection.valid})
        return payload

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
        document = SwmmDocument.parse(text)
        report = validate_model(document)
        payload = report.model_dump(mode="json")
        subcatchment_ids = sorted(
            {row.tokens[0] for row in document.data_rows("SUBCATCHMENTS") if row.tokens}
        )
        lid_control_ids = sorted(
            {row.tokens[0] for row in document.data_rows("LID_CONTROLS") if row.tokens}
        )
        existing_lid_usage: list[dict[str, Any]] = []
        for row in document.data_rows("LID_USAGE"):
            if len(row.tokens) < 3:
                continue
            try:
                parsed_count = float(row.tokens[2])
                unit_count = int(parsed_count) if parsed_count.is_integer() else None
            except ValueError:
                unit_count = None
            existing_lid_usage.append(
                {
                    "subcatchment_id": row.tokens[0],
                    "lid_control": row.tokens[1],
                    "unit_count": unit_count,
                    "raw_unit_count": row.tokens[2],
                }
            )
        payload.update(
            {
                "blocking_count": report.blocking_count,
                "warning_count": report.warning_count,
                "structurally_runnable": report.structurally_runnable,
                # These are direct parser observations for setup controls, not
                # inferred opportunities or engineer approvals.
                "setup_options": {
                    "source": "parsed_model",
                    "subcatchment_ids": subcatchment_ids,
                    "lid_control_ids": lid_control_ids,
                    "existing_lid_usage": existing_lid_usage,
                },
            }
        )
        self.emit("validation.completed", payload)
        return payload

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
        _require_only(
            params,
            {
                "project_directory",
                "project_id",
                "candidate_slots",
                "design_vectors",
                "storm_cases",
                "budget",
                "qa_policy",
                "timeout_seconds",
                "analysis_id",
                "idempotency_key",
                "validation",
                "search_method",
                "search_reason",
            },
        )
        validation = params.get("validation")
        if not isinstance(validation, dict) or validation.get("blocking_count") != 0:
            raise ValueError(
                "exact planning requires validation evidence with zero blocking issues"
            )
        self.require_configured_solver()
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        slots = _candidate_slots(_required(params, "candidate_slots"))
        vectors = [
            list(vector)
            for vector in sorted(
                {
                    tuple(item)
                    for item in _design_vectors(_required(params, "design_vectors"), slots)
                }
            )
        ]
        storms, storm_payloads = self._prepare_exact_storms(
            project_directory, _required(params, "storm_cases")
        )
        budget = SimulationBudget.model_validate(params.get("budget") or {})
        alternative_count = len({tuple(item) for item in vectors if any(item)})
        total_jobs = len(storms) * (alternative_count + 1)
        if total_jobs > budget.max_solver_jobs:
            raise ValueError(
                f"exact run plan requires {total_jobs} jobs but the approved cap is "
                f"{budget.max_solver_jobs}"
            )
        timeout_seconds = max(1, min(int(params.get("timeout_seconds", 3600)), 86_400))
        repository = ProjectRepository(project_directory)
        try:
            project_id = str(_required(params, "project_id"))
            project = repository.get_project(project_id)
            if project["state"].get("base_model_selection_required"):
                raise ValueError(
                    "exact planning requires a persisted base-model selection and model revision"
                )
            model_revision_id = project["state"].get("current_model_revision_id")
            model_sha256 = project["state"].get("current_model_sha256")
            if not isinstance(model_revision_id, str) or not isinstance(model_sha256, str):
                raise TypeError(
                    "exact planning requires an active immutable ModelRevision for this project"
                )
            revision = repository.get_model_revision(model_revision_id)
            if revision["project_id"] != project_id or revision["model_sha256"] != model_sha256:
                raise ValueError(
                    "active model revision metadata is inconsistent with project state"
                )
            selected_base_model = project["state"].get("selected_base_model")
            if project["project_kind"] == ProjectKind.EXISTING.value:
                if not isinstance(selected_base_model, str):
                    raise TypeError(
                        "exact planning requires one persisted authoritative base model"
                    )
                selected_dependencies = project["state"].get("selected_model_dependencies", {})
                if not isinstance(selected_dependencies, dict):
                    raise TypeError("the authoritative base-model dependency record is invalid")
                for storm_payload in storm_payloads:
                    if storm_payload["model_path"] != selected_base_model:
                        raise ValueError(
                            "storm analysis cannot use an unclassified companion INP; "
                            "only the authoritative base model is eligible until a worker "
                            "semantic check proves storm-only equivalence"
                        )
                    if storm_payload["dependencies"] != selected_dependencies:
                        raise ValueError(
                            "storm analysis dependencies must match the authoritative base revision"
                        )
            passport_id = project["state"].get("model_fitness_passport_id")
            if passport_id is not None:
                if not isinstance(passport_id, str):
                    raise TypeError("the active model-fitness passport identifier is invalid")
                passport = repository.get_model_fitness_passport_by_id(passport_id)
                if (
                    passport["project_id"] != project_id
                    or passport["model_revision_id"] != model_revision_id
                ):
                    raise ValueError(
                        "active model-fitness passport is inconsistent with the model revision"
                    )
            analysis_payload = {
                "contract": "exact-swmm-analysis-v2",
                "model_revision_id": model_revision_id,
                "model_sha256": model_sha256,
                "model_fitness_passport_id": passport_id,
                "authoritative_base_model": selected_base_model,
                "metric_extractor_version": METRIC_EXTRACTOR_VERSION,
                "candidate_slots": [item.model_dump(mode="json") for item in slots],
                "design_vectors": vectors,
                "storm_cases": storm_payloads,
                "qa_policy": params.get("qa_policy") or {},
                "timeout_seconds": timeout_seconds,
                "search_method": str(params.get("search_method") or "engineer_selected_exact"),
                "search_reason": str(
                    params.get("search_reason")
                    or "Evaluate the approved finite candidate set with direct SWMM runs."
                ),
                "seed": budget.seed,
                "budget": budget.model_dump(mode="json"),
            }
            analysis = repository.create_analysis(
                project_id,
                payload=analysis_payload,
                idempotency_key=params.get("idempotency_key"),
                analysis_id=params.get("analysis_id"),
            )
            if analysis["payload"] != analysis_payload:
                raise ValueError(
                    "analysis idempotency key was already used for a different immutable run plan"
                )
            orchestrator = AnalysisOrchestrator(
                repository,
                self._solver_runner(timeout_seconds),
                qa_policy=analysis_payload["qa_policy"],
            )
            if analysis["state"] == "DRAFT":
                exact_plan = orchestrator.plan(
                    analysis["analysis_id"],
                    candidate_slots=slots,
                    design_vectors=vectors,
                    storms=storms,
                )
            else:
                exact_plan = orchestrator.status(analysis["analysis_id"])
                exact_plan.update(
                    {
                        "design_count": alternative_count + 1,
                        "alternative_count": alternative_count,
                        "storm_count": len(storms),
                        "planned_solver_jobs": total_jobs,
                    }
                )
        finally:
            repository.close()
        run_plan = AnalysisRunPlan(
            project_id=project_id,
            analysis_id=analysis["analysis_id"],
            model_revision_id=model_revision_id,
            model_sha256=model_sha256,
            baseline_jobs=len(storms),
            candidate_designs=alternative_count,
            storm_count=len(storms),
            total_exact_jobs=total_jobs,
            worker_count=1,
            search_method=analysis_payload["search_method"],
            search_reason=analysis_payload["search_reason"],
            seed=budget.seed,
            reportable_outputs=[
                "Exact direct-report hydraulic metrics",
                "Full-network adverse peak scan",
                "INP, RPT, OUT, hashes, and QA evidence",
            ],
        )
        self.emit(
            "analysis.state_changed",
            {"analysis_id": analysis["analysis_id"], "state": exact_plan["state"]},
        )
        return {**exact_plan, "run_plan": run_plan.model_dump(mode="json")}

    def analysis_start(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "analysis_id"})
        self.require_configured_solver()
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        analysis_id = str(_required(params, "analysis_id"))
        repository = ProjectRepository(project_directory)
        try:
            analysis = repository.get_analysis(analysis_id)
            if analysis["payload"].get("contract") != "exact-swmm-analysis-v2":
                raise ValueError("analysis has no durable schema-2.0 exact-run plan")
            if analysis["state"] == "COMPLETED":
                orchestrator = AnalysisOrchestrator(
                    repository,
                    self._solver_runner(int(analysis["payload"]["timeout_seconds"])),
                    qa_policy=analysis["payload"].get("qa_policy"),
                )
                return orchestrator.status(analysis_id)
            if analysis["state"] not in {"READY", "PAUSED", "RUNNING"}:
                raise ValueError(
                    f"analysis {analysis_id!r} cannot start from {analysis['state']!r}"
                )
        finally:
            repository.close()
        return self._start_exact_background(project_directory, analysis_id)

    def _solver_runner(self, timeout_seconds: int) -> SolverRunner:
        executable = self.require_configured_solver()
        runner = SolverRunner(executable, timeout_seconds=timeout_seconds)
        if runner.solver_sha256 != self.solver_sha256:
            raise ValueError("configured SWMM executable changed before exact planning")
        return runner

    def _prepare_exact_storms(
        self,
        project_directory: Path,
        raw_cases: Any,
    ) -> tuple[list[StormModel], list[dict[str, Any]]]:
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("exact analysis requires at least one storm case")
        storms: list[StormModel] = []
        payloads: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_case in raw_cases:
            if not isinstance(raw_case, dict):
                raise TypeError("each storm case must be an object")
            storm_id = str(_required(raw_case, "storm_id"))
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", storm_id):
                raise ValueError(f"unsafe storm identifier: {storm_id!r}")
            if storm_id in seen:
                raise ValueError(f"duplicate storm identifier: {storm_id!r}")
            seen.add(storm_id)
            model_path, model_relative = _project_input_file(
                project_directory,
                _required(raw_case, "model_path"),
                purpose=f"storm model {storm_id!r}",
            )
            dependencies: dict[str, Path] = {}
            dependency_payload: dict[str, str] = {}
            raw_dependencies = raw_case.get("dependencies", {})
            if not isinstance(raw_dependencies, dict):
                raise TypeError("storm dependencies must be an object")
            for target_name, source in raw_dependencies.items():
                target = Path(str(target_name))
                if target.is_absolute() or ".." in target.parts or not target.parts:
                    raise ValueError(f"unsafe dependency target path: {target_name!r}")
                source_path, source_relative = _project_input_file(
                    project_directory,
                    source,
                    purpose=f"dependency for storm {storm_id!r}",
                )
                normalized_target = target.as_posix()
                dependencies[normalized_target] = source_path
                dependency_payload[normalized_target] = source_relative
            storms.append(
                StormModel(
                    storm_id=storm_id,
                    model_bytes=model_path.read_bytes(),
                    dependencies=dependencies,
                )
            )
            payloads.append(
                {
                    "storm_id": storm_id,
                    "model_path": model_relative,
                    "model_sha256": _sha256_path(model_path),
                    "dependencies": dependency_payload,
                    "dependency_sha256": {
                        name: _sha256_path(path) for name, path in dependencies.items()
                    },
                }
            )
        return storms, payloads

    def _load_exact_plan(
        self,
        project_directory: Path,
        analysis_id: str,
    ) -> tuple[dict[str, Any], list[CandidateSlot], list[StormModel]]:
        repository = ProjectRepository(project_directory)
        try:
            analysis = repository.get_analysis(analysis_id)
            revision_id = analysis["payload"].get("model_revision_id")
            revision_sha256 = analysis["payload"].get("model_sha256")
            if not isinstance(revision_id, str) or not isinstance(revision_sha256, str):
                raise TypeError("exact analysis is not pinned to a model revision")
            revision = repository.get_model_revision(revision_id)
            if (
                revision["project_id"] != analysis["project_id"]
                or revision["model_sha256"] != revision_sha256
            ):
                raise ValueError("exact analysis model revision no longer matches its record")
        finally:
            repository.close()
        payload = analysis["payload"]
        slots = _candidate_slots(payload.get("candidate_slots"))
        storms, normalized = self._prepare_exact_storms(
            project_directory, payload.get("storm_cases")
        )
        if normalized != payload.get("storm_cases"):
            raise ValueError("storm inputs changed after exact planning")
        return analysis, slots, storms

    def _start_exact_background(
        self,
        project_directory: Path,
        analysis_id: str,
    ) -> dict[str, Any]:
        with self._background_lock:
            existing = self._background.get(analysis_id)
            if existing is not None:
                future = existing.get("future")
                if future is None or not future.done():
                    raise ValueError(f"analysis {analysis_id!r} is already running")

        cancel_event = threading.Event()
        pause_event = threading.Event()

        def execute() -> None:
            repository = ProjectRepository(project_directory)
            try:
                analysis, slots, storms = self._load_exact_plan(project_directory, analysis_id)
                orchestrator = AnalysisOrchestrator(
                    repository,
                    self._solver_runner(int(analysis["payload"]["timeout_seconds"])),
                    qa_policy=analysis["payload"].get("qa_policy"),
                )
                orchestrator.recover(analysis_id)

                def progress(event: dict[str, Any]) -> None:
                    event_name = str(event.get("event", "analysis.progress"))
                    self.emit(
                        event_name, {key: value for key, value in event.items() if key != "event"}
                    )

                result = orchestrator.run(
                    analysis_id,
                    candidate_slots=slots,
                    storms=storms,
                    should_pause=pause_event.is_set,
                    should_cancel=cancel_event.is_set,
                    cancel_event=cancel_event,
                    on_progress=progress,
                )
                self.emit("analysis.state_changed", result)
                if result["state"] == "COMPLETED":
                    project = repository.get_project(analysis["project_id"])
                    if (
                        project["project_kind"] == ProjectKind.DEMO.value
                        and analysis["payload"].get("search_method")
                        == "guided_seeded_sha256_exact_replay"
                    ):
                        differential = self._write_guided_differential(repository, analysis_id)
                        self.emit("validation.completed", differential)
                    _validate_authoritative_analysis_runs(repository, analysis_id)
                    self.emit("frontier.updated", orchestrator.decision_board(analysis_id))
                    try:
                        queued = self._queue_completed_analysis_telemetry(
                            repository,
                            analysis["project_id"],
                            analysis_id,
                        )
                        self.emit(
                            "project.updated",
                            {
                                "project_id": analysis["project_id"],
                                "cloud_batches_queued": len(queued),
                            },
                        )
                    except Exception as cloud_error:  # noqa: BLE001 - hydraulics remain authoritative
                        self.emit(
                            "project.updated",
                            {
                                "project_id": analysis["project_id"],
                                "cloud_sync_state": "failed",
                                "cloud_error_code": type(cloud_error).__name__.upper()[:64],
                            },
                        )
            except Exception as exc:  # noqa: BLE001 - background failure becomes a typed event
                self.emit(
                    "solver_job.failed",
                    {
                        "analysis_id": analysis_id,
                        "state": "FAILED",
                        "error": str(exc),
                    },
                )
            finally:
                repository.close()

        background = {
            "future": None,
            "cancel": cancel_event,
            "pause": pause_event,
            "execute": execute,
            "project_directory": project_directory,
            "kind": "exact-analysis",
        }
        with self._background_lock:
            self._background[analysis_id] = background
        try:
            future = self._executor.submit(execute)
        except Exception:
            with self._background_lock:
                if self._background.get(analysis_id) is background:
                    self._background.pop(analysis_id, None)
            raise
        with self._background_lock:
            background["future"] = future
        self.emit("analysis.state_changed", {"analysis_id": analysis_id, "state": "RUNNING"})
        return {"analysis_id": analysis_id, "state": "RUNNING", "exact": True}

    def _write_guided_differential(
        self,
        repository: ProjectRepository,
        analysis_id: str,
    ) -> dict[str, Any]:
        manifest_path = self.require_contained_path(
            repository.root,
            "guided_fixture/manifest.json",
            purpose="guided exact-run manifest",
        )
        manifest = json.loads(manifest_path.read_bytes())
        expected: dict[tuple[tuple[int, ...], str], dict[str, float]] = {}
        for run in manifest.get("runs", []):
            if not isinstance(run, dict):
                continue
            vector = tuple(int(value) for value in run.get("design_vector", []))
            metrics = run.get("metrics")
            if len(vector) == 4 and isinstance(metrics, dict):
                expected[(vector, str(run["storm_id"]))] = {
                    "runoff_continuity_error_percent": float(
                        metrics["runoff_continuity_error_percent"]
                    ),
                    "flow_continuity_error_percent": float(
                        metrics["flow_continuity_error_percent"]
                    ),
                    "system_flooding_loss_million_gallons": float(
                        metrics["system_flooding_loss_million_gallons"]
                    ),
                    "outfall_peak_cfs": float(metrics["outfall_peak_cfs"]),
                    "outfall_volume_million_gallons": float(
                        metrics["outfall_volume_million_gallons"]
                    ),
                }
        jobs = repository._connection.execute(
            "SELECT job_id,storm_id,state,payload_json FROM solver_jobs "
            "WHERE analysis_id=? ORDER BY job_id",
            (analysis_id,),
        ).fetchall()
        comparisons: list[dict[str, Any]] = []
        missing: list[str] = []
        metric_tolerances = {
            "runoff_continuity_error_percent": {"absolute": 0.001, "relative": 0.0001},
            "flow_continuity_error_percent": {"absolute": 0.001, "relative": 0.0001},
            "system_flooding_loss_million_gallons": {
                "absolute": 0.001,
                "relative": 0.0001,
            },
            "outfall_peak_cfs": {"absolute": 0.001, "relative": 0.0001},
            "outfall_volume_million_gallons": {
                "absolute": 0.001,
                "relative": 0.0001,
            },
        }
        for job in jobs:
            payload = json.loads(job["payload_json"])
            vector = tuple(int(value) for value in payload.get("design_vector", []))
            key = (vector, str(job["storm_id"]))
            expected_metrics = expected.get(key)
            if job["state"] != "SUCCEEDED" or expected_metrics is None:
                missing.append(str(job["job_id"]))
                continue
            actual_rows = repository._connection.execute(
                "SELECT metric_id,value FROM run_metrics WHERE job_id=? AND object_id=''",
                (job["job_id"],),
            ).fetchall()
            actual = {str(row["metric_id"]): float(row["value"]) for row in actual_rows}
            for metric_id, expected_value in expected_metrics.items():
                actual_value = actual.get(metric_id)
                if actual_value is None:
                    missing.append(f"{job['job_id']}:{metric_id}")
                    continue
                tolerance = metric_tolerances[metric_id]
                allowed = max(
                    tolerance["absolute"],
                    abs(expected_value) * tolerance["relative"],
                )
                delta = actual_value - expected_value
                comparisons.append(
                    {
                        "job_id": job["job_id"],
                        "storm_id": job["storm_id"],
                        "design_vector": list(vector),
                        "metric_id": metric_id,
                        "fixture_value": expected_value,
                        "rerun_value": actual_value,
                        "delta": delta,
                        "allowed_difference": allowed,
                        "within_tolerance": abs(delta) <= allowed,
                    }
                )
        outside = [item for item in comparisons if not item["within_tolerance"]]
        report = {
            "schema_version": "2.0",
            "analysis_id": analysis_id,
            "fixture_id": manifest.get("fixture_id"),
            "solver_sha256": self.solver_sha256,
            "metric_tolerances": metric_tolerances,
            "expected_jobs": 884,
            "completed_jobs": len(jobs),
            "comparison_count": len(comparisons),
            "missing_evidence": missing,
            "outside_tolerance_count": len(outside),
            "matches_fixture_within_tolerances": not missing and not outside,
            "comparisons": comparisons,
        }
        relative = f"guided_fixture/differentials/{analysis_id}.json"
        destination = self.require_contained_path(
            repository.root,
            relative,
            purpose="guided rerun differential",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
        _atomic_write_bytes(destination, content)
        repository.record_project_asset(
            repository.get_analysis(analysis_id)["project_id"],
            role="guided-rerun-differential",
            relative_path=relative,
            sha256=hashlib.sha256(content).hexdigest(),
            payload={
                "analysis_id": analysis_id,
                "matches_fixture_within_tolerances": report["matches_fixture_within_tolerances"],
            },
        )
        return {key: value for key, value in report.items() if key != "comparisons"} | {
            "path": relative
        }

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
                future = background.get("future")
                if (
                    background.get("kind") == "exact-analysis"
                    and future is not None
                    and future.done()
                ):
                    background["future"] = self._executor.submit(background["execute"])
                result = {"analysis_id": analysis_id, "state": "RUNNING"}
            elif state == "CANCELLED":
                background["cancel"].set()
                future = background.get("future")
                if (
                    background.get("kind") == "exact-analysis"
                    and future is not None
                    and future.done()
                ):
                    background["future"] = self._executor.submit(background["execute"])
                result = {"analysis_id": analysis_id, "state": "CANCELLED", "requested": True}
            else:
                raise ValueError(f"unsupported background transition {state}")
            self.emit("analysis.state_changed", result)
            return result
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        repository = ProjectRepository(project_directory)
        try:
            current = repository.get_analysis(analysis_id)
            is_exact = current["payload"].get("contract") == "exact-swmm-analysis-v2"
            if state == "RUNNING" and is_exact:
                if current["state"] not in {"READY", "PAUSED", "RUNNING"}:
                    raise ValueError(
                        f"analysis {analysis_id!r} cannot resume from {current['state']!r}"
                    )
                analysis = current
            elif state == "CANCELLED" and is_exact:
                orchestrator = AnalysisOrchestrator(
                    repository,
                    self._solver_runner(int(current["payload"]["timeout_seconds"])),
                    qa_policy=current["payload"].get("qa_policy"),
                )
                analysis = orchestrator.cancel(analysis_id)
            else:
                analysis = repository.transition_analysis(analysis_id, state)
                if state == "PAUSING":
                    analysis = repository.transition_analysis(analysis["analysis_id"], "PAUSED")
        finally:
            repository.close()
        if state == "RUNNING" and is_exact:
            return self._start_exact_background(project_directory, analysis_id)
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

    def results_decision_board(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "analysis_id", "adverse_tolerance"})
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            analysis = repository.get_analysis(str(_required(params, "analysis_id")))
            model_revision_id = analysis["payload"].get("model_revision_id")
            if not isinstance(model_revision_id, str) or not model_revision_id:
                raise ValueError("decision board requires an analysis pinned to a ModelRevision")
            _validate_authoritative_analysis_runs(repository, analysis["analysis_id"])
            board = AnalysisOrchestrator(repository).decision_board(
                analysis["analysis_id"],
                adverse_tolerance=float(params.get("adverse_tolerance", 0.0)),
            )
            board.update(
                {
                    "project_id": analysis["project_id"],
                    "model_fitness_passport": repository.get_model_fitness_passport(
                        analysis["project_id"], model_revision_id
                    ),
                    "decision_brief": repository.get_decision_brief(
                        analysis["project_id"], analysis["analysis_id"]
                    ),
                    # The board contains every complete exact ensemble. It does
                    # not receive objective directions, so it must not label
                    # every returned alternative as nondominated.
                    "claim": "Exact SWMM result under documented assumptions.",
                }
            )
        finally:
            repository.close()
        return board

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
                    "report": result.report_sha256,
                    "solver": result.solver_sha256,
                    "output": result.output_sha256,
                },
            }
        )
        return payload

    def observability_validate_binding(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"scope", "project_directory", "policy", "binding"})
        if params.get("scope") != "project":
            raise ValueError("observability.validate_binding scope must be project")
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            policy = ProjectDataPolicy.model_validate(_required(params, "policy"))
            repository.get_project(policy.project_id)
            validated = self.observability.validate_binding(policy, _required(params, "binding"))
        finally:
            repository.close()
        return validated.model_dump(mode="json")

    def observability_connect(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._cloud_lifecycle_lock:
            return self._observability_connect_locked(params)

    def _observability_connect_locked(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = str(_required(params, "scope"))
        if scope == "account":
            _require_only(
                params,
                {
                    "scope",
                    "api_key",
                    "credential_id",
                    "deployment",
                    "api_base_url",
                    "web_base_url",
                    "entity",
                    "project",
                    "weave_project",
                    "project_visibility",
                    "data_policy_acknowledged",
                    "acknowledged_by",
                    "classification",
                },
            )
            if params.get("data_policy_acknowledged") is not True:
                return self._block_account_observability(
                    "Full engineering-evidence synchronization has not been acknowledged."
                )
            if str(params.get("project_visibility")) != "Restricted":
                return self._block_account_observability(
                    "The selected W&B project is not verified as Restricted."
                )
            entity = str(_required(params, "entity"))
            external_project = str(_required(params, "project"))
            if str(params.get("weave_project") or external_project) != external_project:
                raise ValueError("W&B Models and Weave must use the same project")
            deployment = str(_required(params, "deployment"))
            api_base_url = str(_required(params, "api_base_url")).rstrip("/")
            web_base_url = str(_required(params, "web_base_url")).rstrip("/")
            classification = str(params.get("classification") or "standard_engineering")
            acknowledged_by = str(params.get("acknowledged_by") or entity)
            pending_project_id = "pending-observability-account"
            try:
                policy = ProjectDataPolicy(
                    project_id=pending_project_id,
                    classification=classification,
                    deployment=deployment,
                    acknowledged_by=acknowledged_by,
                )
            except ValueError as exc:
                return self._block_account_observability(str(exc))
            candidate = ObservabilityBinding(
                project_id=pending_project_id,
                data_policy_id=policy.policy_id,
                entity=entity,
                external_project=external_project,
                external_project_id="pending-validation",
                api_base_url=api_base_url,
                web_base_url=web_base_url,
                deployment=deployment,
                mode="online",
            )
            api_key = str(_required(params, "api_key"))
            try:
                with self.observability.candidate_credentials(api_key, api_base_url):
                    verified = self.observability.validate_binding(policy, candidate)
                    probe_payload = b"GI Scenario Studio disclosed W&B Models probe v2\\n"
                    probe_sha256 = hashlib.sha256(probe_payload).hexdigest()
                    probe_method = getattr(self.observability.wandb, "roundtrip_probe", None)
                    models_probe = (
                        probe_method(verified, probe_payload, probe_sha256).model_dump(mode="json")
                        if callable(probe_method)
                        else {"adapter_validated": True, "sha256": probe_sha256}
                    )
                    weave_probe = self.observability.weave.trace_call(
                        verified,
                        call_id=new_id("weaveprobe"),
                        operation="connection_probe",
                        inputs={"synthetic_probe": True, "schema_version": "2.0"},
                        output={"models_access": True, "weave_access": True},
                    )
            except ValueError as exc:
                if str(exc) == "WANDB_RESTRICTED_SCOPE_UNVERIFIABLE":
                    return self._block_account_observability(
                        "W&B did not provide an authoritative exact Restricted visibility scope. "
                        "No Models probe, Weave trace, or evidence upload was attempted."
                    )
                if "restricted" in str(exc).lower() or "policy" in str(exc).lower():
                    return self._block_account_observability(str(exc))
                raise
            session_id = new_id("obssession")
            self._observability_account = {
                "session_id": session_id,
                "binding": verified.model_dump(mode="json"),
                "classification": classification,
                "acknowledged_by": acknowledged_by,
                "models_probe": models_probe,
                "weave_probe": weave_probe.model_dump(mode="json"),
                "consumed_project_id": None,
            }
            self._observability_account_block = None
            return self._account_observability_status()
        if scope != "project":
            raise ValueError("observability.connect scope must be account or project")
        _require_only(params, {"scope", "project_directory", "policy", "binding", "revision"})
        with self._cloud_lifecycle_lock:
            repository = ProjectRepository(
                self.require_project_directory(_required(params, "project_directory"))
            )
            try:
                if params.get("revision") is not None:
                    state = self.observability.rebind(
                        repository,
                        policy=_required(params, "policy"),
                        binding=_required(params, "binding"),
                        revision=params["revision"],
                    )
                else:
                    state = self.observability.connect(
                        repository,
                        policy=_required(params, "policy"),
                        binding=_required(params, "binding"),
                    )
            finally:
                repository.close()
        self.emit(
            "project.updated",
            {
                "project_id": state.project_id,
                "cloud_binding_verified": state.can_enter_home,
            },
        )
        return state.model_dump(mode="json")

    def _block_account_observability(self, message: str) -> dict[str, Any]:
        self._observability_account_block = {
            "message": message,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        return self._account_observability_status()

    def observability_bind_project(self, params: dict[str, Any]) -> dict[str, Any]:
        """Bind an already-open local project to the exactly verified account target."""

        _require_only(params, {"project_directory", "project_id"})
        project_id = str(_required(params, "project_id"))
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        with self._cloud_lifecycle_lock:
            account = self._observability_account
            if account is None or not self.observability.credentials.configured:
                raise ValueError("connect and verify a Restricted W&B account before binding")
            consumed_project_id = account.get("consumed_project_id")
            if consumed_project_id not in {None, project_id}:
                raise ValueError("this W&B destination is already assigned to another GI project")
            repository = ProjectRepository(project_directory)
            try:
                repository.get_project(project_id)
                template = account["binding"]
                try:
                    policy = ProjectDataPolicy.model_validate(
                        repository.get_project_data_policy(project_id)
                    )
                except KeyError:
                    policy = ProjectDataPolicy(
                        project_id=project_id,
                        classification=account["classification"],
                        deployment=template["deployment"],
                        acknowledged_by=account["acknowledged_by"],
                    )
                if policy.classification != account["classification"]:
                    raise ValueError(
                        "the verified account classification conflicts with this project's data policy"
                    )
                if policy.deployment != template["deployment"]:
                    raise ValueError(
                        "the verified W&B deployment conflicts with this project's data policy"
                    )
                binding = ObservabilityBinding(
                    project_id=project_id,
                    data_policy_id=policy.policy_id,
                    entity=template["entity"],
                    external_project=template["external_project"],
                    external_project_id=template["external_project_id"],
                    api_base_url=template["api_base_url"],
                    web_base_url=template["web_base_url"],
                    deployment=template["deployment"],
                    mode="online",
                    verified=True,
                    verified_at=template["verified_at"],
                )
                state = self.observability.connect(
                    repository,
                    policy=policy,
                    binding=binding,
                )
                account["consumed_project_id"] = project_id
            finally:
                repository.close()
        self.emit(
            "project.updated",
            {
                "project_id": project_id,
                "cloud_binding_verified": state.can_sync,
                "cloud_sync_state": state.state,
            },
        )
        return state.model_dump(mode="json")

    def observability_disconnect(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = str(_required(params, "scope"))
        if scope == "account":
            _require_only(params, {"scope"})
            with self._cloud_lifecycle_lock:
                self.observability.clear_credentials()
                self._observability_account = None
                self._observability_account_block = None
            return self._account_observability_status()
        if scope != "project":
            raise ValueError("observability.disconnect scope must be account or project")
        _require_only(params, {"scope", "project_directory", "project_id"})
        project_id = str(_required(params, "project_id"))
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        with self._cloud_lifecycle_lock:
            repository = ProjectRepository(project_directory)
            try:
                repository.deactivate_observability_binding(project_id)
                state = self.observability.status(repository, project_id)
            finally:
                repository.close()
            self.observability.clear_credentials()
            self._observability_account = None
            self._observability_account_block = None
        self.emit(
            "project.updated",
            {
                "project_id": project_id,
                "cloud_binding_verified": False,
                "cloud_sync_state": "LOCAL_ONLY",
            },
        )
        return state.model_dump(mode="json")

    def _account_observability_status(self) -> dict[str, Any]:
        if self._observability_account_block is not None:
            return ObservabilityAccountState(
                state="BLOCKED_PROJECT_POLICY",
                mode="error",
                configured=False,
                provider="wandb-weave",
                disclosureAccepted=False,
                message=self._observability_account_block["message"],
                lastChecked=self._observability_account_block["recorded_at"],
            ).model_dump(mode="json")
        account = self._observability_account
        if account is None:
            return ObservabilityAccountState(
                state="LOCAL_ONLY",
                mode="local",
                configured=True,
                provider="none",
                disclosureAccepted=False,
                message=(
                    "Working locally. Exact SWMM runs and engineering evidence stay on this "
                    "device; W&B sync and the Weave assistant are off."
                ),
            ).model_dump(mode="json")
        binding = account["binding"]
        root = binding["web_base_url"].rstrip("/")
        project_url = f"{root}/{binding['entity']}/{binding['external_project']}"
        return ObservabilityAccountState(
            state="CONNECTED",
            mode="connected",
            configured=True,
            provider="wandb-weave",
            sessionId=account["session_id"],
            entity=binding["entity"],
            project=binding["external_project"],
            weaveProject=binding["external_project"],
            wandbUrl=project_url,
            weaveUrl=f"{project_url}/weave",
            deployment=binding["deployment"],
            apiBaseUrl=binding["api_base_url"],
            webBaseUrl=binding["web_base_url"],
            externalProjectId=binding["external_project_id"],
            disclosureAccepted=True,
            includedCategories=list(FULL_EVIDENCE_SYNC_CATEGORIES),
            excludedCategories=[
                "credentials",
                "environment_variables",
                "local_absolute_paths",
                "raw_console_logs",
            ],
            probes={"models": account["models_probe"], "weave": account["weave_probe"]},
            lastChecked=datetime.now(UTC),
            message="Restricted W&B Models and Weave access verified.",
        ).model_dump(mode="json")

    def observability_get_status(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = str(_required(params, "scope"))
        if scope == "account":
            _require_only(params, {"scope"})
            return self._account_observability_status()
        if scope != "project":
            raise ValueError("observability status scope must be account or project")
        _require_only(params, {"scope", "project_directory", "project_id"})
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            project_id = str(_required(params, "project_id"))
            state = self.observability.status(repository, project_id)
            due = repository.list_telemetry_outbox_due()
        finally:
            repository.close()
        if state.can_sync:
            for item in due:
                if item["envelope"]["project_id"] == project_id:
                    self._schedule_telemetry_sync(repository.root, item["outbox_id"])
        return state.model_dump(mode="json")

    def observability_preview_upload(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "envelope"})
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            return self.observability.preview_upload(repository, _required(params, "envelope"))
        finally:
            repository.close()

    def observability_sync(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "envelope", "preview_sha256"})
        with self._cloud_lifecycle_lock:
            repository = ProjectRepository(
                self.require_project_directory(_required(params, "project_directory"))
            )
            try:
                result = self.observability.sync(
                    repository,
                    envelope=_required(params, "envelope"),
                    preview_sha256=str(_required(params, "preview_sha256")),
                )
            finally:
                repository.close()
        self.emit(
            "project.updated",
            {"cloud_envelope_id": result["envelope_id"], "cloud_sync_state": result["state"]},
        )
        return result

    def observability_retry(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "project_id", "outbox_id"})
        with self._cloud_lifecycle_lock:
            repository = ProjectRepository(
                self.require_project_directory(_required(params, "project_directory"))
            )
            project_root = repository.root
            try:
                project_id = str(_required(params, "project_id"))
                status = self.observability.status(repository, project_id)
                if not status.can_sync:
                    raise ValueError("reconnect this project before retrying cloud evidence")
                if params.get("outbox_id") is not None:
                    requested = repository.get_telemetry_outbox_item(str(params["outbox_id"]))
                    if requested["envelope"]["project_id"] != project_id:
                        raise ValueError("cloud outbox item belongs to a different project")
                    result = self.observability.retry(repository, str(params["outbox_id"]))
                else:
                    failed = repository.list_telemetry_outbox(
                        project_id,
                        states=["failed", "blocked"],
                    )
                    retry_ids = [item["outbox_id"] for item in failed]
                    for outbox_id in retry_ids:
                        repository.retry_telemetry_outbox_item(outbox_id)
                    result = {
                        "project_id": project_id,
                        "state": "retry_scheduled" if retry_ids else "nothing_to_retry",
                        "scheduled_outbox_ids": retry_ids,
                    }
            finally:
                repository.close()
        for outbox_id in result.get("scheduled_outbox_ids", []):
            self._schedule_telemetry_sync(project_root, outbox_id)
        self.emit(
            "project.updated",
            {
                "cloud_envelope_id": result.get("envelope_id"),
                "cloud_sync_state": result["state"],
                "retry_count": len(result.get("scheduled_outbox_ids", [])),
            },
        )
        return result

    def observability_open_run(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "envelope_id"})
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            reference = repository.get_wandb_run_reference(str(_required(params, "envelope_id")))
        finally:
            repository.close()
        if reference is None:
            raise KeyError("W&B run reference was not found")
        return {"url": reference["url"], "run": reference}

    def observability_open_artifact(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "project_id", "artifact_id"})
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            binding = repository.get_observability_binding(str(_required(params, "project_id")))
            row = repository._connection.execute(
                "SELECT category,artifact_id FROM cloud_artifacts WHERE project_id=? AND artifact_id=?",
                (str(_required(params, "project_id")), str(_required(params, "artifact_id"))),
            ).fetchone()
        finally:
            repository.close()
        if row is None:
            raise KeyError("cloud artifact reference was not found")
        name = f"{row['category']}-{row['artifact_id']}"
        return {
            "artifact_id": row["artifact_id"],
            "url": (
                f"{binding['web_base_url'].rstrip('/')}/{binding['entity']}/"
                f"{binding['external_project']}"
                f"/artifacts/{row['category']}/{name}"
            ),
        }

    def observability_open_trace(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "project_id", "call_id"})
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            trace = repository.get_weave_trace_reference(
                str(_required(params, "project_id")), str(_required(params, "call_id"))
            )
        finally:
            repository.close()
        if trace is None:
            raise KeyError("Weave trace reference was not found")
        return {"url": trace["url"], "trace": trace}

    def _enqueue_and_schedule_telemetry(
        self,
        repository: ProjectRepository,
        envelope: TelemetryEnvelope,
    ) -> dict[str, Any]:
        existing = repository._connection.execute(
            "SELECT outbox_id FROM telemetry_outbox WHERE project_id=? AND idempotency_key=?",
            (envelope.project_id, envelope.idempotency_key),
        ).fetchone()
        if existing is None:
            queued = repository.enqueue_telemetry_envelope(envelope)
        else:
            queued = repository.get_telemetry_outbox_item(existing["outbox_id"])
            persisted = _telemetry_idempotency_projection(queued["envelope"])
            candidate = _telemetry_idempotency_projection(envelope.model_dump(mode="json"))
            if persisted != candidate:
                raise ValueError("telemetry idempotency key was reused with different evidence")
        status = self.observability.status(repository, envelope.project_id)
        if status.can_sync and queued["state"] != "synced":
            self._schedule_telemetry_sync(repository.root, queued["outbox_id"])
        return queued

    def _schedule_telemetry_sync(self, project_directory: Path, outbox_id: str) -> None:
        operation_id = f"telemetry:{outbox_id}"
        with self._background_lock:
            existing = self._background.get(operation_id)
            if existing is not None:
                future = existing.get("future")
                if future is None or not future.done():
                    return

        def execute() -> None:
            with self._cloud_lifecycle_lock:
                repository = ProjectRepository(project_directory)
                try:
                    item = repository.get_telemetry_outbox_item(outbox_id)
                    envelope = TelemetryEnvelope.model_validate(item["envelope"])
                    preview = self.observability.preview_upload(repository, envelope)
                    result = self.observability.sync(
                        repository,
                        envelope=envelope,
                        preview_sha256=preview["payload_sha256"],
                    )
                    self.emit(
                        "project.updated",
                        {
                            "project_id": envelope.project_id,
                            "cloud_envelope_id": envelope.envelope_id,
                            "cloud_sync_state": result["state"],
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - cloud failure is isolated
                    self.emit(
                        "project.updated",
                        {
                            "cloud_sync_state": "failed",
                            "cloud_error_code": type(exc).__name__.upper()[:64],
                        },
                    )
                finally:
                    repository.close()

        background = {"future": None, "kind": "telemetry-sync"}
        with self._background_lock:
            self._background[operation_id] = background
        try:
            future = self._executor.submit(execute)
        except Exception:
            with self._background_lock:
                self._background.pop(operation_id, None)
            raise
        with self._background_lock:
            background["future"] = future

    def _cloud_manifest_for_file(
        self,
        repository: ProjectRepository,
        *,
        project_id: str,
        category: str,
        source_object_id: str,
        logical_name: str,
        source: Path,
    ) -> CloudArtifactManifest:
        if not source.is_file():
            raise ValueError(f"cloud evidence source {logical_name!r} was not found")
        content = source.read_bytes()
        digest, _ = repository.store_blob(content)
        media_type = (
            "application/octet-stream"
            if source.suffix.lower() in {".out", ".bin", ".sqlite"}
            else "application/json"
            if source.suffix.lower() == ".json"
            else "text/plain"
        )
        stable = hashlib.sha256(
            f"{project_id}|{source_object_id}|{logical_name}|{digest}".encode()
        ).hexdigest()[:24]
        return CloudArtifactManifest(
            artifact_id=f"cloudartifact_{stable}",
            project_id=project_id,
            category=category,
            logical_name=logical_name,
            sha256=digest,
            bytes=len(content),
            media_type=media_type,
            source_object_id=source_object_id,
            local_blob_sha256=digest,
        )

    def _queue_guided_session_telemetry(
        self,
        repository: ProjectRepository,
        project_id: str,
        checkpoints: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any]]:
        session_id = new_id("guidedsession")
        assets = repository._connection.execute(
            "SELECT role,path FROM project_assets WHERE project_id=? ORDER BY role,path",
            (project_id,),
        ).fetchall()
        manifests: list[CloudArtifactManifest] = []
        for asset in assets:
            role = str(asset["role"])
            category = (
                "solver_input"
                if role.endswith("input")
                else "solver_report"
                if role.endswith("report")
                else "solver_output"
                if role.endswith("output")
                else "decision_brief"
                if role in {"guided-story", "guided-fixture-projection"}
                else "result"
            )
            relative = str(asset["path"])
            manifests.append(
                self._cloud_manifest_for_file(
                    repository,
                    project_id=project_id,
                    category=category,
                    source_object_id=session_id,
                    logical_name=relative,
                    source=self.require_contained_path(
                        repository.root,
                        relative,
                        purpose="guided cloud evidence",
                    ),
                )
            )
        binding = repository.get_observability_binding(project_id)
        table_rows = [
            [
                item.get("step_id"),
                item.get("status"),
                item.get("duration_seconds", 0),
                item.get("evidence_items_opened", 0),
            ]
            for item in checkpoints
        ]
        envelope = TelemetryEnvelope(
            envelope_id=f"envelope_{session_id}",
            project_id=project_id,
            binding_id=binding["binding_id"],
            category="engineering_evidence",
            event_type="guided_session",
            subject_id=session_id,
            idempotency_key=f"guided-session:{session_id}",
            metadata={
                "schema_version": "2.0",
                "fixture_id": "cedar_trace_exact_v2",
                "replay_mode": "verified_fixture_replay",
                "expected_exact_jobs": 884,
                "tables": {
                    "guided_checkpoints": {
                        "columns": [
                            "step_id",
                            "status",
                            "duration_seconds",
                            "evidence_items_opened",
                        ],
                        "rows": table_rows,
                    }
                },
            },
            artifacts=manifests,
        )
        return session_id, self._enqueue_and_schedule_telemetry(repository, envelope)

    def _queue_completed_analysis_telemetry(
        self,
        repository: ProjectRepository,
        project_id: str,
        analysis_id: str,
    ) -> list[dict[str, Any]]:
        try:
            binding = repository.get_observability_binding(project_id)
        except KeyError:
            return []
        analysis = repository.get_analysis(analysis_id)
        analysis_payload = analysis["payload"]
        jobs = repository._connection.execute(
            "SELECT j.job_id,j.design_id,j.storm_id,j.state,j.payload_json,d.sequence "
            "FROM solver_jobs j JOIN designs d ON d.analysis_id=j.analysis_id "
            "AND d.design_id=j.design_id WHERE j.analysis_id=? "
            "ORDER BY d.sequence,j.storm_id,j.job_id",
            (analysis_id,),
        ).fetchall()
        if not jobs:
            return []
        batches: list[list[Any]] = []
        current: list[Any] = []
        current_bytes = 0
        for job in jobs:
            artifact_bytes = sum(
                _artifact_path(repository.root, row["relative_path"]).stat().st_size
                for row in repository._connection.execute(
                    "SELECT relative_path FROM run_artifacts WHERE job_id=?",
                    (job["job_id"],),
                ).fetchall()
            )
            if current and (
                len(current) >= TELEMETRY_BATCH_JOB_LIMIT
                or current_bytes + artifact_bytes > TELEMETRY_BATCH_BYTE_LIMIT
            ):
                batches.append(current)
                current = []
                current_bytes = 0
            current.append(job)
            current_bytes += artifact_bytes
        if current:
            batches.append(current)

        alternatives = repository._connection.execute(
            "SELECT d.design_id,d.sequence,d.source,count(DISTINCT j.job_id) AS exact_jobs,"
            "count(DISTINCT CASE WHEN j.state='SUCCEEDED' THEN j.job_id END) AS accepted_jobs,"
            "count(DISTINCT CASE WHEN j.state='QA_REJECTED' THEN j.job_id END) AS rejected_jobs,"
            "sum(CASE WHEN m.metric_id='outfall_volume_million_gallons' THEN m.value ELSE 0 END) "
            "AS aggregate_outfall_volume,"
            "max(CASE WHEN m.metric_id='outfall_peak_cfs' THEN m.value END) AS controlling_peak "
            "FROM designs d LEFT JOIN solver_jobs j ON j.analysis_id=d.analysis_id "
            "AND j.design_id=d.design_id LEFT JOIN run_metrics m ON m.job_id=j.job_id "
            "AND m.object_id='' WHERE d.analysis_id=? GROUP BY d.design_id,d.sequence,d.source "
            "ORDER BY d.sequence,d.design_id",
            (analysis_id,),
        ).fetchall()
        alternative_rows = [
            [
                row["design_id"],
                int(row["sequence"]),
                row["source"],
                int(row["exact_jobs"]),
                int(row["accepted_jobs"] or 0),
                int(row["rejected_jobs"] or 0),
                float(row["aggregate_outfall_volume"] or 0.0),
                float(row["controlling_peak"] or 0.0),
            ]
            for row in alternatives
        ]
        queued: list[dict[str, Any]] = []
        for batch_index, batch in enumerate(batches):
            manifests: list[CloudArtifactManifest] = []
            solver_rows: list[list[Any]] = []
            qa_rows: list[list[Any]] = []
            artifact_bytes = 0
            for job in batch:
                payload = json.loads(job["payload_json"])
                artifacts = repository._connection.execute(
                    "SELECT artifact_id,role,sha256,relative_path FROM run_artifacts "
                    "WHERE job_id=? ORDER BY role",
                    (job["job_id"],),
                ).fetchall()
                for artifact in artifacts:
                    source = _artifact_path(repository.root, artifact["relative_path"])
                    artifact_bytes += source.stat().st_size
                    role = str(artifact["role"])
                    category = (
                        "solver_input"
                        if role == "input"
                        else "solver_report"
                        if role == "report"
                        else "solver_output"
                        if role == "output"
                        else "result"
                    )
                    suffix = {"input": "inp", "report": "rpt", "output": "out"}.get(role, "bin")
                    manifests.append(
                        self._cloud_manifest_for_file(
                            repository,
                            project_id=project_id,
                            category=category,
                            source_object_id=str(job["job_id"]),
                            logical_name=f"runs/{job['job_id']}/{role}.{suffix}",
                            source=source,
                        )
                    )
                solver_rows.append(
                    [
                        job["job_id"],
                        job["design_id"],
                        job["storm_id"],
                        job["state"],
                        payload.get("qa_status", "not_available"),
                        bool(payload.get("cache_hit", False)),
                        payload.get("solver_sha256"),
                    ]
                )
                findings = repository._connection.execute(
                    "SELECT severity,code FROM qa_findings WHERE job_id=? ORDER BY finding_id",
                    (job["job_id"],),
                ).fetchall()
                qa_rows.extend(
                    [[job["job_id"], item["severity"], item["code"]] for item in findings]
                )
            stable = hashlib.sha256(
                f"{analysis_id}|exact-run-batch|{batch_index}".encode()
            ).hexdigest()[:24]
            envelope = TelemetryEnvelope(
                envelope_id=f"envelope_{stable}",
                project_id=project_id,
                analysis_id=analysis_id,
                binding_id=binding["binding_id"],
                category="engineering_evidence",
                event_type="alternatives_analysis",
                subject_id=analysis_id,
                idempotency_key=f"analysis:{analysis_id}:exact-batch:{batch_index}",
                metadata={
                    "schema_version": "2.0",
                    "model_revision_id": analysis_payload.get("model_revision_id"),
                    "model_sha256": analysis_payload.get("model_sha256"),
                    "model_fitness_passport_id": analysis_payload.get("model_fitness_passport_id"),
                    "authoritative_base_model": analysis_payload.get("authoritative_base_model"),
                    "batch_index": batch_index,
                    "batch_count": len(batches),
                    "job_count": len(batch),
                    "artifact_bytes": artifact_bytes,
                    "tables": {
                        "solver_jobs": {
                            "columns": [
                                "job_id",
                                "design_id",
                                "storm_id",
                                "state",
                                "qa_status",
                                "cache_hit",
                                "solver_sha256",
                            ],
                            "rows": solver_rows,
                        },
                        "qa_findings": {
                            "columns": ["job_id", "severity", "code"],
                            "rows": qa_rows,
                        },
                        "evaluated_alternatives": {
                            "columns": [
                                "design_id",
                                "sequence",
                                "source",
                                "exact_jobs",
                                "accepted_jobs",
                                "rejected_jobs",
                                "aggregate_outfall_volume_million_gallons",
                                "controlling_peak_cfs",
                            ],
                            "rows": alternative_rows if batch_index == 0 else [],
                        },
                    },
                },
                artifacts=manifests,
            )
            queued.append(self._enqueue_and_schedule_telemetry(repository, envelope))
        return queued

    def assistant_explain(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(
            params,
            {"project_directory", "project_id", "intent", "job_id", "question"},
        )
        return self._assistant_answer(
            params,
            operation="explain",
            prior_answer_id=None,
        )

    def assistant_follow_up(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(
            params,
            {"project_directory", "project_id", "answer_id", "question"},
        )
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        repository = ProjectRepository(project_directory)
        try:
            prior = repository.get_assistant_answer(
                str(_required(params, "project_id")), str(_required(params, "answer_id"))
            )
        finally:
            repository.close()
        job_ids = [item["run_id"] for item in prior.get("numeric_claims", [])]
        derived = {
            "project_directory": str(project_directory),
            "project_id": prior["project_id"],
            "intent": prior["intent"],
            "job_id": job_ids[0] if job_ids else None,
            "question": _required(params, "question"),
        }
        return self._assistant_answer(
            derived,
            operation="follow_up",
            prior_answer_id=prior["answer_id"],
        )

    def _assistant_answer(
        self,
        params: dict[str, Any],
        *,
        operation: str,
        prior_answer_id: str | None,
    ) -> dict[str, Any]:
        project_id = str(_required(params, "project_id"))
        intent = str(_required(params, "intent"))
        if intent not in {"model_fitness", "exact_run"}:
            raise ValueError("assistant intent must be model_fitness or exact_run")
        if intent == "exact_run" and not params.get("job_id"):
            raise ValueError("exact_run assistant calls require job_id")
        question = str(params.get("question") or "Explain the evidence relevant to this view.")
        if len(question) > 2_000:
            raise ValueError("assistant question exceeds the supported length")
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            project = repository.get_project(project_id)
            cloud_status = self.observability.status(repository, project_id)
            if cloud_status.binding is None or not cloud_status.binding.verified:
                raise RPCFault(
                    -32010,
                    (
                        "Evidence Assistant is off until this project has an online, "
                        "exactly verified Restricted W&B binding and Weave access."
                    ),
                    {"code": "ASSISTANT_DISABLED_WEAVE_UNAVAILABLE"},
                )
            draft = _assistant_draft(
                repository,
                project,
                intent=intent,
                job_id=str(params["job_id"]) if params.get("job_id") else None,
            )
            prevalidation = _validate_assistant_draft(repository, project_id, draft)
            if prevalidation["errors"]:
                raise ValueError("assistant evidence template failed deterministic validation")
            call_id = new_id("assistantcall")
            trace_inputs = {
                "project_id": project_id,
                "intent": intent,
                "job_id": params.get("job_id"),
                "question": question,
                "prior_answer_id": prior_answer_id,
            }
            try:
                with self._cloud_lifecycle_lock:
                    trace = self.observability.trace_assistant_call(
                        repository,
                        project_id=project_id,
                        call_id=call_id,
                        operation=operation,
                        inputs=trace_inputs,
                        output={**draft, "validation": prevalidation},
                    )
            except Exception as exc:
                raise RPCFault(
                    -32010,
                    "Evidence Assistant is unavailable because a required Weave trace could not be created.",
                    {"code": "ASSISTANT_DISABLED_WEAVE_UNAVAILABLE"},
                ) from exc
            answer = AssistantAnswer(
                call_id=call_id,
                project_id=project_id,
                intent=intent,
                weave_trace=trace,
                **draft,
            )
            evaluation = AssistantEvaluationRecord(
                project_id=project_id,
                answer_id=answer.answer_id,
                trace_id=trace.trace_id,
                valid=True,
                checked_evidence_ids=prevalidation["checked_evidence_ids"],
            )
            repository.store_assistant_answer(answer)
            repository.store_assistant_evaluation(evaluation)
        finally:
            repository.close()
        return {
            "answer": answer.model_dump(mode="json"),
            "evaluation": evaluation.model_dump(mode="json"),
        }

    def assistant_feedback(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(
            params,
            {"project_directory", "project_id", "answer_id", "feedback", "notes"},
        )
        project_id = str(_required(params, "project_id"))
        feedback = str(_required(params, "feedback"))
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            answer = repository.get_assistant_answer(
                project_id, str(_required(params, "answer_id"))
            )
            call_id = new_id("assistantfeedback")
            try:
                with self._cloud_lifecycle_lock:
                    trace = self.observability.trace_assistant_call(
                        repository,
                        project_id=project_id,
                        call_id=call_id,
                        operation="feedback",
                        inputs={
                            "answer_id": answer["answer_id"],
                            "feedback": feedback,
                            "notes": params.get("notes"),
                        },
                        output={"recorded": True},
                    )
            except Exception as exc:
                raise RPCFault(
                    -32010,
                    "Assistant feedback was not recorded because its required Weave trace failed.",
                    {"code": "ASSISTANT_DISABLED_WEAVE_UNAVAILABLE"},
                ) from exc
            evaluation = AssistantEvaluationRecord(
                project_id=project_id,
                answer_id=answer["answer_id"],
                trace_id=trace.trace_id,
                valid=True,
                user_feedback=feedback,
                feedback_notes=params.get("notes"),
            )
            repository.store_assistant_evaluation(evaluation)
        finally:
            repository.close()
        return evaluation.model_dump(mode="json")

    def assistant_get_trace(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.observability_open_trace(params)

    def guided_open(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "project_id"})
        guided_telemetry: dict[str, Any] = {}
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            project_id = str(_required(params, "project_id"))
            status = self.observability.status(repository, project_id)
            project = repository.get_project(project_id)
            checkpoints = repository.list_guided_checkpoints(project_id)
            if not checkpoints:
                checkpoints = [
                    repository.upsert_guided_checkpoint(item)
                    for item in _guided_checkpoints(project)
                ]
            fixture_payload: dict[str, Any] = {}
            if project["project_kind"] == ProjectKind.DEMO.value:
                fixture_path = self.require_contained_path(
                    repository.root,
                    "guided_fixture/ui-projection.json",
                    purpose="guided fixture projection",
                )
                fixture, fixture_bytes = self._read_guided_projection(fixture_path)
                fixture_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
                if fixture_sha256 != project["state"].get("guided_fixture_sha256"):
                    raise ValueError("the project-owned guided fixture hash changed")
                manifest_path = self.require_contained_path(
                    repository.root,
                    "guided_fixture/manifest.json",
                    purpose="guided exact-run manifest",
                )
                if _sha256_path(manifest_path) != fixture["source_manifest_sha256"]:
                    raise ValueError("the project-owned guided manifest hash changed")
                fixture_payload = {
                    "fixture": fixture,
                    "fixture_sha256": fixture_sha256,
                    "preview_mode": None,
                }
                if status.binding is None or not status.binding.verified:
                    guided_telemetry = {"cloud_sync_state": "local_only"}
                else:
                    try:
                        session_id, outbox = self._queue_guided_session_telemetry(
                            repository, project_id, checkpoints
                        )
                        guided_telemetry = {
                            "guided_session_id": session_id,
                            "cloud_envelope_id": outbox["envelope"]["envelope_id"],
                            "cloud_sync_state": outbox["state"],
                        }
                    except Exception as cloud_error:  # noqa: BLE001 - replay remains local
                        guided_telemetry = {
                            "cloud_sync_state": "projection_failed",
                            "cloud_error_code": type(cloud_error).__name__.upper()[:64],
                        }
        finally:
            repository.close()
        return {
            **_guided_state_payload(project_id, checkpoints),
            **fixture_payload,
            **guided_telemetry,
        }

    def guided_get_state(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "project_id"})
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            project_id = str(_required(params, "project_id"))
            checkpoints = repository.list_guided_checkpoints(project_id)
            if not checkpoints:
                raise KeyError("guided workflow has not been opened")
        finally:
            repository.close()
        return _guided_state_payload(project_id, checkpoints)

    def guided_rerun(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(
            params,
            {"project_directory", "project_id", "analysis_id", "validation", "idempotency_key"},
        )
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        project_id = str(_required(params, "project_id"))
        repository = ProjectRepository(project_directory)
        try:
            project = repository.get_project(project_id)
            if project["project_kind"] != ProjectKind.DEMO.value:
                raise ValueError("guided.rerun is limited to the Cedar Trace teaching project")
        finally:
            repository.close()
        validation = _required(params, "validation")
        if not isinstance(validation, dict) or validation.get("blocking_count") != 0:
            raise ValueError("rerun requires validation evidence with zero blocking issues")
        candidates_path = self.require_contained_path(
            project_directory,
            "candidate-slots.json",
            purpose="guided candidate plan",
        )
        run_plan_path = self.require_contained_path(
            project_directory,
            "guided-run-plan.json",
            purpose="guided exact run plan",
        )
        bundle_path = self.require_contained_path(
            project_directory,
            "project.json",
            purpose="guided project bundle",
        )
        candidates = json.loads(candidates_path.read_bytes())
        run_plan = json.loads(run_plan_path.read_bytes())
        bundle = json.loads(bundle_path.read_bytes())
        guided_revision_id = str(bundle.get("model_revision_id", ""))
        guided_model_relative = str(bundle.get("base_model_path", ""))
        guided_model_path = self.require_contained_path(
            project_directory,
            guided_model_relative,
            purpose="guided source model revision",
        )
        guided_model_sha256 = _sha256_path(guided_model_path)
        repository = ProjectRepository(project_directory)
        try:
            project = repository.get_project(project_id)
            try:
                revision = repository.get_model_revision(guided_revision_id)
                if revision["model_sha256"] != guided_model_sha256:
                    raise ValueError("guided model revision hash changed")
            except KeyError:
                try:
                    binding_id = repository.get_observability_binding(project_id)["binding_id"]
                except KeyError:
                    binding_id = None
                revision_model = ModelRevision(
                    revision_id=guided_revision_id,
                    project_id=project_id,
                    reason="import",
                    model_sha256=guided_model_sha256,
                    observability_binding_id=binding_id,
                    created_by="guided-fixture",
                )
                passport = ModelFitnessPassport(
                    project_id=project_id,
                    model_revision_id=guided_revision_id,
                    intended_use="Synthetic Cedar Trace teaching replay only.",
                    permitted_outputs=["learning evidence package"],
                    prohibited_uses=[
                        "design",
                        "permitting",
                        "flood-risk determination",
                        "representation of a real municipality",
                    ],
                    unresolved_limitations=[
                        "No observed event, calibration record, or approved reference report was matched."
                    ],
                )
                state = dict(project["state"])
                state["current_model_revision_id"] = guided_revision_id
                state["current_model_sha256"] = guided_model_sha256
                state["model_fitness_passport_id"] = passport.passport_id
                repository.store_imported_model_record(revision_model, passport, state)
        finally:
            repository.close()
        design_order = run_plan.get("design_order")
        if (
            run_plan.get("schema_version") != "2.0"
            or run_plan.get("design_count") != 220
            or not isinstance(design_order, list)
            or len(design_order) != 220
        ):
            raise ValueError("the guided 220-design run plan failed integrity checks")
        storm_cases = [
            {
                "storm_id": item["storm_id"],
                "model_path": item["model_path"],
                "dependencies": {},
            }
            for item in bundle.get("storm_cases", [])
        ]
        planned = self.analysis_plan(
            {
                "project_directory": str(project_directory),
                "project_id": project_id,
                "candidate_slots": candidates,
                "design_vectors": [item["vector"] for item in design_order],
                "storm_cases": storm_cases,
                "budget": {"max_solver_jobs": 884, "seed": int(run_plan["seed"])},
                "validation": validation,
                "analysis_id": params.get("analysis_id"),
                "idempotency_key": params.get("idempotency_key"),
                "search_method": "guided_seeded_sha256_exact_replay",
                "search_reason": (
                    "Re-execute the pinned 220-design Cedar Trace teaching plan for a "
                    "tolerance-based differential report."
                ),
            }
        )
        started = self.analysis_start(
            {
                "project_directory": str(project_directory),
                "analysis_id": planned["analysis_id"],
            }
        )
        return {**started, "run_plan": planned["run_plan"], "replay_jobs": 884}

    def guided_pause(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._transition_analysis(
            {
                "project_directory": _required(params, "project_directory"),
                "analysis_id": _required(params, "analysis_id"),
            },
            "PAUSING",
        )

    def guided_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._transition_analysis(
            {
                "project_directory": _required(params, "project_directory"),
                "analysis_id": _required(params, "analysis_id"),
            },
            "RUNNING",
        )

    def guided_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._transition_analysis(
            {
                "project_directory": _required(params, "project_directory"),
                "analysis_id": _required(params, "analysis_id"),
            },
            "CANCELLED",
        )

    def decision_brief_get(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "project_id", "analysis_id"})
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            brief = repository.get_decision_brief(
                str(_required(params, "project_id")), str(_required(params, "analysis_id"))
            )
        finally:
            repository.close()
        return {"brief": brief}

    def decision_brief_update(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "brief"})
        brief = DecisionBrief.model_validate(_required(params, "brief"))
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            self._validate_decision_brief_update(repository, brief)
            stored = repository.store_decision_brief(brief)
        finally:
            repository.close()
        return stored

    @staticmethod
    def _validate_decision_brief_update(
        repository: ProjectRepository,
        brief: DecisionBrief,
    ) -> None:
        """Validate every decision reference against immutable local evidence.

        A DecisionBrief is renderer-authored review state.  It may describe
        authoritative jobs, but it cannot promote itself into authoritative
        evidence or unlock a professional export.
        """

        analysis = repository.get_analysis(brief.analysis_id)
        if analysis["project_id"] != brief.project_id:
            raise ValueError("decision brief and analysis belong to different projects")
        analysis_revision_id = analysis["payload"].get("model_revision_id")
        if not isinstance(analysis_revision_id, str) or not analysis_revision_id:
            raise ValueError("decision brief requires an analysis pinned to a model revision")
        if brief.model_revision_id != analysis_revision_id:
            raise ValueError("decision brief model revision does not match its analysis")
        revision = repository.get_model_revision(brief.model_revision_id)
        if revision["project_id"] != brief.project_id:
            raise ValueError("decision brief model revision belongs to a different project")

        passport = repository.get_model_fitness_passport_by_id(brief.model_fitness_passport_id)
        if passport["project_id"] != brief.project_id:
            raise ValueError("decision brief fitness passport belongs to a different project")
        if passport["model_revision_id"] != brief.model_revision_id:
            raise ValueError("decision brief fitness passport describes a different model revision")

        if brief.status != "draft" or brief.full_network_adverse_effects_reviewed:
            raise ValueError(
                "decision_brief.update can store only drafts until a repository-backed "
                "full-network adverse-effect review record is implemented"
            )

        alternative_ids = [item.design_id for item in brief.alternatives]
        if len(set(alternative_ids)) != len(alternative_ids):
            raise ValueError("decision brief alternatives must identify unique designs")

        claim_ids = {claim["claim_id"] for claim in passport.get("evidence_claims", [])}
        cited_job_ids: set[str] = set()
        qa_precedence = {"accepted": 0, "warning": 1, "rejected": 2}
        for alternative in brief.alternatives:
            if len(set(alternative.exact_run_ids)) != len(alternative.exact_run_ids):
                raise ValueError(
                    f"alternative {alternative.design_id!r} cites duplicate exact run IDs"
                )
            design_jobs = repository._connection.execute(
                "SELECT job_id,state FROM solver_jobs WHERE analysis_id=? AND design_id=? "
                "ORDER BY storm_id,job_id",
                (brief.analysis_id, alternative.design_id),
            ).fetchall()
            if not design_jobs:
                raise ValueError(
                    f"alternative {alternative.design_id!r} has no jobs in the cited analysis"
                )
            authoritative_job_ids = {row["job_id"] for row in design_jobs}
            supplied_job_ids = set(alternative.exact_run_ids)
            if supplied_job_ids != authoritative_job_ids:
                raise ValueError(
                    f"alternative {alternative.design_id!r} must cite its complete "
                    "design-by-storm job set"
                )
            if any(row["state"] != "SUCCEEDED" for row in design_jobs):
                raise ValueError(
                    f"alternative {alternative.design_id!r} cites a job that did not succeed"
                )

            resolved = [
                _load_authoritative_run(
                    repository,
                    job_id,
                    expected_analysis_id=brief.analysis_id,
                    expected_project_id=brief.project_id,
                )["result"]
                for job_id in sorted(supplied_job_ids)
            ]
            authoritative_qa = max(
                (item.qa_status for item in resolved),
                key=qa_precedence.__getitem__,
            )
            if alternative.qa_status != authoritative_qa:
                raise ValueError(
                    f"alternative {alternative.design_id!r} QA status does not match its "
                    "authoritative exact runs"
                )
            cited_job_ids.update(supplied_job_ids)
            unsupported_claim_ids = set(alternative.evidence_claim_ids).difference(
                claim_ids | supplied_job_ids
            )
            if unsupported_claim_ids:
                raise ValueError(
                    f"alternative {alternative.design_id!r} cites evidence IDs that are not "
                    "stored passport claims or its exact runs"
                )

        unsupported_brief_claim_ids = set(brief.evidence_claim_ids).difference(
            claim_ids | cited_job_ids
        )
        if unsupported_brief_claim_ids:
            raise ValueError(
                "decision brief cites evidence IDs that are not stored passport claims or "
                "authoritative exact runs"
            )

    def model_get_fitness(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(params, {"project_directory", "project_id"})
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            passport = repository.get_model_fitness_passport(str(_required(params, "project_id")))
        finally:
            repository.close()
        return {"passport": passport, "decision_gate": "ModelFitnessPassport"}

    def model_attest(self, params: dict[str, Any]) -> dict[str, Any]:
        """Persist an explicit engineer attestation for the active model revision."""

        _require_only(params, {"project_directory", "passport"})
        passport = ModelFitnessPassport.model_validate(_required(params, "passport"))
        if not passport.reviewer_attested or not passport.reviewer:
            raise ValueError("model.attest requires an identified engineer attestation")
        if passport.disposition.value == "not_assessed":
            raise ValueError("an attested passport must record an assessed disposition")
        if passport.disposition.value == "fit_for_stated_use":
            raise ValueError(
                "fit_for_stated_use is unavailable until a repository-backed successful "
                "reference-comparison service is implemented"
            )
        repository = ProjectRepository(
            self.require_project_directory(_required(params, "project_directory"))
        )
        try:
            project = repository.get_project(passport.project_id)
            revision = repository.get_model_revision(passport.model_revision_id)
            if revision["project_id"] != passport.project_id:
                raise ValueError("fitness passport and model revision belong to different projects")
            current_revision_id = project["state"].get("current_model_revision_id")
            if current_revision_id != passport.model_revision_id:
                raise ValueError("fitness attestation must describe the active model revision")
            stored = repository.store_model_fitness_passport(passport)
            state = dict(project["state"])
            state["model_fitness_passport_id"] = passport.passport_id
            repository.set_project_state(passport.project_id, state)
        finally:
            repository.close()
        self.emit(
            "model.fitness_updated",
            {
                "project_id": passport.project_id,
                "model_revision_id": passport.model_revision_id,
                "passport_id": passport.passport_id,
                "disposition": passport.disposition.value,
            },
        )
        return {"passport": stored, "decision_gate": "ModelFitnessPassport"}

    def export_validate(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_only(
            params,
            {
                "project_directory",
                "analysis_id",
                "job_ids",
                "mode",
                "include_costs",
                "cost_basis",
            },
        )
        project_directory = self.require_project_directory(_required(params, "project_directory"))
        if bool(params.get("include_costs")) or params.get("cost_basis") is not None:
            raise ValueError(
                "cost-bearing exports are unavailable until CostBasis is stored and "
                "revision-bound in the authoritative repository"
            )
        analysis_id = str(_required(params, "analysis_id"))
        job_ids = _required(params, "job_ids")
        if not isinstance(job_ids, list) or not job_ids:
            raise ValueError("job_ids must be a non-empty array")
        repository = ProjectRepository(project_directory)
        try:
            analysis = repository.get_analysis(analysis_id)
            model_revision_id = analysis["payload"].get("model_revision_id")
            if not isinstance(model_revision_id, str) or not model_revision_id:
                raise ValueError("export requires an analysis pinned to a ModelRevision")
            passport = repository.get_model_fitness_passport(
                analysis["project_id"], model_revision_id
            )
            if passport is None:
                raise ValueError("export requires a stored Model Fitness Passport")
            runs = [
                _load_authoritative_run(
                    repository,
                    str(job_id),
                    expected_analysis_id=analysis_id,
                    expected_project_id=analysis["project_id"],
                )["result"]
                for job_id in job_ids
            ]
        finally:
            repository.close()
        eligibility = validate_export(
            project_id=analysis["project_id"],
            analysis_id=analysis_id,
            mode=_required(params, "mode"),
            model_fitness_passport=passport,
            results=runs,
            include_costs=bool(params.get("include_costs", False)),
            cost_basis=params.get("cost_basis"),
            results_verified=True,
        )
        return eligibility.model_dump(mode="json")

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
            "model_fitness_passport",
            "model_revision_id",
            "extractor_version",
            "export_eligibility",
            "full_network_adverse_effects_reviewed",
        }
        supplied = sorted(prohibited.intersection(params))
        if supplied:
            raise ValueError(
                "export.create derives authoritative evidence fields from the project repository; "
                f"remove: {', '.join(supplied)}"
            )
        if bool(params.get("include_costs")) or params.get("cost_basis") is not None:
            raise ValueError(
                "cost-bearing exports are unavailable until CostBasis is stored and "
                "revision-bound in the authoritative repository"
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
                "SELECT a.analysis_id,a.project_id,a.payload_json,p.title "
                "FROM analyses a JOIN projects p ON p.project_id=a.project_id "
                "WHERE a.analysis_id=?",
                (analysis_id,),
            ).fetchone()
            if analysis_row is None:
                raise KeyError(f"analysis {analysis_id!r} was not found")
            try:
                analysis_payload = json.loads(analysis_row["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("analysis has invalid persisted evidence metadata") from exc
            if not isinstance(analysis_payload, dict):
                raise TypeError("analysis has non-object persisted evidence metadata")
            model_revision_id = analysis_payload.get("model_revision_id")
            extractor_version = analysis_payload.get("metric_extractor_version")
            if not isinstance(model_revision_id, str) or not model_revision_id:
                raise ValueError("export requires an analysis pinned to a ModelRevision")
            if not isinstance(extractor_version, str) or not extractor_version.strip():
                raise ValueError("export requires a pinned metric extractor version")
            passport = repository.get_model_fitness_passport(
                analysis_row["project_id"], model_revision_id
            )
            if passport is None:
                raise ValueError("export requires a stored Model Fitness Passport")
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
                "model_fitness_passport": passport,
                "results": [item["result"] for item in authoritative_runs],
                "input_hashes": input_hashes,
                "solver_sha256": next(iter(solver_hashes)),
                "extractor_version": extractor_version,
                "results_verified": True,
                "attachments": attachments or None,
            }
        )
        result = create_evidence_bundle(**export_params)
        repository = ProjectRepository(project_directory)
        try:
            repository.store_export_manifest(result.manifest)
        finally:
            repository.close()

        cloud_projection: dict[str, Any] | None = None
        repository = ProjectRepository(project_directory)
        try:
            try:
                binding = repository.get_observability_binding(analysis_row["project_id"])
            except KeyError:
                cloud_projection = {"state": "local_only"}
            else:
                try:
                    manifests = [
                        self._cloud_manifest_for_file(
                            repository,
                            project_id=analysis_row["project_id"],
                            category="export",
                            source_object_id=result.manifest.export_id,
                            logical_name=f"exports/{result.manifest.export_id}/{name}",
                            source=self.require_allowed_path(path, purpose="evidence export file"),
                        )
                        for name, path in sorted(result.files.items())
                    ]
                    envelope = TelemetryEnvelope(
                        envelope_id=f"envelope_{result.manifest.export_id}",
                        project_id=analysis_row["project_id"],
                        analysis_id=analysis_id,
                        binding_id=binding["binding_id"],
                        category="engineering_evidence",
                        event_type="evidence_export",
                        subject_id=result.manifest.export_id,
                        idempotency_key=f"evidence-export:{result.manifest.export_id}",
                        metadata={
                            "schema_version": "2.0",
                            "package_type": result.manifest.package_type,
                            "file_count": len(manifests),
                            "solver_sha256": result.manifest.solver_sha256,
                            "model_revision_id": result.manifest.model_revision_id,
                            "export_eligibility_id": result.manifest.export_eligibility_id,
                            "tables": {
                                "evidence_claims": {
                                    "columns": ["run_id", "qa_status", "storm_id"],
                                    "rows": [
                                        [
                                            item["result"].run_id,
                                            item["result"].qa_status,
                                            item["result"].storm_id,
                                        ]
                                        for item in authoritative_runs
                                    ],
                                }
                            },
                        },
                        artifacts=manifests,
                    )
                    queued = self._enqueue_and_schedule_telemetry(repository, envelope)
                    cloud_projection = {
                        "outbox_id": queued["outbox_id"],
                        "envelope_id": queued["envelope"]["envelope_id"],
                        "state": queued["state"],
                    }
                except Exception as cloud_error:  # noqa: BLE001 - local export stays authoritative
                    cloud_projection = {
                        "state": "projection_failed",
                        "error_code": type(cloud_error).__name__.upper()[:64],
                    }
        finally:
            repository.close()
        self.emit(
            "export.completed", {"export_id": result.manifest.export_id, "files": result.files}
        )
        payload = result.model_dump(mode="json")
        payload["cloud_sync"] = cloud_projection
        return payload

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


def _project_limitations(project: dict[str, Any]) -> list[str]:
    state = project.get("state") if isinstance(project.get("state"), dict) else {}
    raw = state.get("unresolved_limitations", state.get("limitations", []))
    limitations = [str(item) for item in raw] if isinstance(raw, list) else []
    if project["project_kind"] == ProjectKind.CONCEPTUAL.value:
        limitations.insert(0, CONCEPTUAL_NOTICE)
    elif project["project_kind"] == ProjectKind.DEMO.value:
        limitations.insert(0, "Bundled demo evidence is for learning, not a professional decision.")
    return list(dict.fromkeys(limitations))


def _assistant_draft(
    repository: ProjectRepository,
    project: dict[str, Any],
    *,
    intent: str,
    job_id: str | None,
) -> dict[str, Any]:
    project_id = project["project_id"]
    if intent == "model_fitness":
        passport = repository.get_model_fitness_passport(project_id)
        evidence_id = (
            f"model-fitness:{passport['passport_id']}"
            if passport
            else f"model-fitness:{project_id}:not-assessed"
        )
        disposition = passport["disposition"] if passport else "not_assessed"
        limitations = (
            list(passport.get("unresolved_limitations", []))
            if passport
            else ["No engineer-attested Model Fitness Passport is stored for this revision."]
        )
        return {
            "answer": (
                f"Model fitness for the stated use is {disposition}. This is an independent "
                "engineer-controlled decision gate; cloud sync state and the assistant cannot change it."
            ),
            "evidence_references": [
                AssistantEvidenceReference(
                    evidence_id=evidence_id,
                    kind="project",
                    label="Model Fitness Passport" if passport else "Fitness not yet assessed",
                    source_id=passport["passport_id"] if passport else project_id,
                ).model_dump(mode="json")
            ],
            "numeric_claims": [],
            "assumptions_used": [],
            "limitations": limitations,
            "proposed_actions": [
                AssistantProposedAction(
                    action="review",
                    reason="Review or complete the intended-use Model Fitness Passport.",
                    evidence_ids=[evidence_id],
                    object_ids=["model-fitness"],
                ).model_dump(mode="json")
            ],
        }

    authoritative = _load_authoritative_run(
        repository,
        str(job_id),
        expected_project_id=project_id,
    )
    result: ExactRunResult = authoritative["result"]
    references: list[dict[str, Any]] = [
        AssistantEvidenceReference(
            evidence_id=f"exact-run:{result.run_id}",
            kind="exact_run",
            label="Pinned exact SWMM execution",
            source_id=result.run_id,
        ).model_dump(mode="json")
    ]
    claims: list[dict[str, Any]] = []
    rendered: list[str] = []
    for record in authoritative["metric_records"]:
        metric_id = str(record["metric_id"])
        object_id = str(record["object_id"])
        label = metric_id if not object_id else f"{metric_id} at {object_id}"
        evidence_id = f"exact-metric:{result.run_id}:{metric_id}:{object_id or 'system'}"
        references.append(
            AssistantEvidenceReference(
                evidence_id=evidence_id,
                kind="exact_metric",
                label=label,
                source_id=result.run_id,
            ).model_dump(mode="json")
        )
        claims.append(
            AssistantNumericClaim(
                label=label,
                value=float(record["value"]),
                units=str(record["units"]),
                evidence_id=evidence_id,
                run_id=result.run_id,
                metric_id=metric_id,
                object_id=object_id,
            ).model_dump(mode="json")
        )
        rendered.append(f"{label} = {float(record['value']):.12g} {record['units']}")
    return {
        "answer": (
            "Exact SWMM result under documented assumptions: "
            + ("; ".join(rendered) if rendered else "no numeric metrics are stored")
            + f". QA status is {result.qa_status}."
        ),
        "evidence_references": references,
        "numeric_claims": claims,
        "assumptions_used": [
            "Stored input, report, output, and solver hashes identify this execution.",
            f"Hydraulic metrics were extracted with {result.extractor_version}.",
            "The result applies to the recorded design and storm case only.",
        ],
        "limitations": [
            "This answer does not infer field performance, approval, or regulatory acceptance."
        ],
        "proposed_actions": [
            AssistantProposedAction(
                action="open_evidence",
                reason="Inspect the exact run, QA findings, metrics, and hashes.",
                evidence_ids=[item["evidence_id"] for item in references],
                object_ids=[result.run_id],
            ).model_dump(mode="json")
        ],
    }


def _validate_assistant_draft(
    repository: ProjectRepository,
    project_id: str,
    draft: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    checked: set[str] = set()
    references = {
        item["evidence_id"]: AssistantEvidenceReference.model_validate(item)
        for item in draft.get("evidence_references", [])
    }
    claims = [
        AssistantNumericClaim.model_validate(item) for item in draft.get("numeric_claims", [])
    ]
    run_cache: dict[str, dict[str, Any]] = {}
    for claim in claims:
        reference = references.get(claim.evidence_id)
        if (
            reference is None
            or reference.kind != "exact_metric"
            or reference.source_id != claim.run_id
        ):
            errors.append(f"Numeric claim {claim.label!r} lacks exact-metric evidence.")
            continue
        if claim.run_id not in run_cache:
            try:
                run_cache[claim.run_id] = _load_authoritative_run(
                    repository, claim.run_id, expected_project_id=project_id
                )
            except (KeyError, TypeError, ValueError):
                errors.append(f"Numeric claim {claim.label!r} names a nonauthoritative run.")
                continue
        records = [
            item
            for item in run_cache[claim.run_id]["metric_records"]
            if item["metric_id"] == claim.metric_id and item["object_id"] == claim.object_id
        ]
        if len(records) != 1 or float(records[0]["value"]) != claim.value:
            errors.append(f"Numeric claim {claim.label!r} does not match stored exact evidence.")
        elif str(records[0]["units"]) != claim.units:
            errors.append(f"Numeric claim {claim.label!r} does not match stored units.")
        else:
            checked.add(claim.evidence_id)
    allowed_values = [claim.value for claim in claims]
    number_pattern = re.compile(r"(?<![\w.])-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![\w.])")
    blocked = [
        match.group(0)
        for match in number_pattern.finditer(str(draft.get("answer", "")))
        if float(match.group(0)) not in allowed_values
    ]
    if blocked:
        errors.append("Assistant prose contains numbers without exact evidence claims.")
    prohibited = {
        "real-world truth",
        "verified global optimum",
        "regulator approved",
        "filing ready",
        "guaranteed resilient",
    }
    lowered = str(draft.get("answer", "")).casefold()
    if any(phrase in lowered for phrase in prohibited):
        errors.append("Assistant prose contains a prohibited engineering claim.")
    return {
        "errors": errors,
        "blocked_numeric_claims": blocked,
        "checked_evidence_ids": sorted(checked or set(references)),
    }


def _guided_checkpoints(project: dict[str, Any]) -> list[GuidedCheckpoint]:
    definitions = [
        (
            "overview",
            "Orient the decision",
            "See what decision the current model revision can support and what evidence is missing.",
            "Read the Model Fitness Passport, intended outcome, and limitations before editing.",
            "You can state the intended screening decision and fitness disposition in plain language.",
            "Review model fitness",
            "model.get_fitness",
        ),
        (
            "model",
            "Establish a trustworthy baseline",
            "Review topology, parameters, dependencies, and assumptions for the stated use.",
            "Resolve blocking checks, inspect assumptions, and record engineer evidence in the passport.",
            "The passport has an explicit disposition, evidence, reviewer, and limitations.",
            "Run model checks",
            "model.validate",
        ),
        (
            "storms",
            "Define the tests",
            "Choose the rainfall and boundary cases every alternative must be tested against.",
            "Confirm source, role, units, initial conditions, and one exact untouched baseline per storm.",
            "Every storm has provenance and a reviewed baseline disposition.",
            "Plan baseline runs",
            "baseline.plan",
        ),
        (
            "opportunities",
            "Define feasible GI choices",
            "Restrict search to engineer-approved locations and native SWMM LID controls.",
            "Review footprint, displaced surface, unit bounds, conflicts, and feasibility evidence.",
            "Every searchable slot has a reviewed FeasibilityRecord.",
            "Review candidate feasibility",
            None,
        ),
        (
            "goals",
            "Make success measurable",
            "Translate the decision into exact hydraulic criteria, constraints, and sourced lifecycle cost.",
            "Define units, thresholds, directions, storm aggregation, and the cost basis.",
            "Every DecisionCriterion is measurable and every cost claim is sourced.",
            "Plan exact analysis",
            "analysis.plan",
        ),
        (
            "run",
            "Evaluate exact alternatives",
            "Execute every selected design against every required storm with pinned SWMM.",
            "Review total exact jobs, seed, method, cache, and storage before starting.",
            "Each displayed design has a complete exact design-by-storm evidence set or visible failure.",
            "Start exact runs",
            "analysis.start",
        ),
        (
            "compare",
            "Understand the tradeoffs",
            "Compare exact alternatives and check whether benefits move harm elsewhere in the network.",
            "Use the decision board, controlling storms, costs, adverse effects, and Evidence Rail.",
            "The shortlist has full-network adverse-effect review and exact evidence claims.",
            "Open decision board",
            "results.decision_board",
        ),
        (
            "export",
            "Hand off defensible evidence",
            "Create a package another engineer can reproduce without hidden assumptions.",
            "Resolve Model Fitness and export gates, then include manifests and exact artifacts.",
            "ExportEligibility is allowed and the package contains claims, QA, limitations, and hashes.",
            "Validate evidence package",
            "export.validate",
        ),
    ]
    return [
        GuidedCheckpoint(
            checkpoint_id=f"checkpoint:{project['project_id']}:{step_id}",
            project_id=project["project_id"],
            step_id=step_id,
            title=title,
            purpose=purpose,
            how_to_use=how,
            completion_signal=completion,
            status="current" if index == 0 else "pending",
            primary_action_label=action,
            primary_rpc_method=rpc,
        )
        for index, (step_id, title, purpose, how, completion, action, rpc) in enumerate(definitions)
    ]


def _guided_state_payload(project_id: str, checkpoints: list[dict[str, Any]]) -> dict[str, Any]:
    current = next(
        (item["step_id"] for item in checkpoints if item["status"] == "current"),
        next((item["step_id"] for item in checkpoints if item["status"] != "complete"), "export"),
    )
    return {
        "project_id": project_id,
        "current_step_id": current,
        "checkpoints": checkpoints,
        "outcome": (
            "A transparent GI shortlist whose hydraulic values resolve to exact pinned SWMM runs."
        ),
        "local_authority": True,
    }


def _candidate_slots(value: Any) -> list[CandidateSlot]:
    if not isinstance(value, list) or not value:
        raise ValueError("exact analysis requires at least one candidate slot")
    explicit_engineering_fields = {
        "base_units",
        "min_additional_units",
        "initial_saturation_percent",
        "from_impervious_percent",
        "from_pervious",
        "route_to_pervious",
        "displaced_surface_area",
        "engineer_approved",
    }
    slots: list[CandidateSlot] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise TypeError("candidate slots must be JSON objects")
        missing = sorted(explicit_engineering_fields.difference(item))
        if missing:
            raise ValueError(
                f"candidate slot {index + 1} must explicitly supply: {', '.join(missing)}"
            )
        slots.append(CandidateSlot.model_validate(item))
    identifiers = [item.slot_id for item in slots]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("candidate slot identifiers must be unique")
    return slots


def _design_vectors(value: Any, slots: list[CandidateSlot]) -> list[list[int]]:
    if not isinstance(value, list):
        raise TypeError("design_vectors must be an array")
    vectors: list[list[int]] = []
    for raw in value:
        if not isinstance(raw, list) or len(raw) != len(slots):
            raise ValueError("every design vector must match the candidate-slot count")
        vector: list[int] = []
        for slot, item in zip(slots, raw, strict=True):
            if isinstance(item, bool) or not isinstance(item, int):
                raise TypeError("design vectors may contain only integer unit counts")
            if not slot.min_additional_units <= item <= slot.max_additional_units:
                raise ValueError(f"design value for {slot.slot_id!r} is outside approved bounds")
            vector.append(item)
        vectors.append(vector)
    return vectors


def _project_input_file(
    project_directory: Path,
    value: Any,
    *,
    purpose: str,
) -> tuple[Path, str]:
    root = project_directory.resolve()
    supplied = Path(str(value))
    candidate = supplied if supplied.is_absolute() else root / supplied
    candidate = candidate.resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"{purpose} must be contained in the project directory")
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{purpose} may not traverse a symbolic link")
    if not candidate.is_file():
        raise ValueError(f"{purpose} was not found")
    return candidate, relative.as_posix()


def _required(params: dict[str, Any], key: str) -> Any:
    if key not in params:
        raise ValueError(f"missing required parameter: {key}")
    return params[key]


def _require_only(params: dict[str, Any], allowed: set[str]) -> None:
    unexpected = sorted(set(params).difference(allowed))
    if unexpected:
        raise ValueError(f"unexpected parameters: {', '.join(unexpected)}")


def _required_bundle_identity(params: dict[str, Any]) -> tuple[str, int]:
    expected_sha256 = _required(params, "expected_sha256")
    _require_sha256(expected_sha256, label="staged project bundle hash")
    expected_bytes = _required(params, "expected_bytes")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or not 0 < expected_bytes <= 2 * 1024 * 1024 * 1024
    ):
        raise ValueError("staged project bundle byte count must be between 1 byte and 2 GiB")
    return expected_sha256, expected_bytes


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
        "fitnessDisposition": "not_assessed",
        "location": (
            "Cedar Trace · synthetic study area"
            if kind == "demo"
            else "Study area not yet defined"
            if kind == "conceptual"
            else "SWMM bundle not yet imported"
        ),
        "projectDirectory": str(directory),
        "modelFile": model_file,
        "updatedAt": project["updated_at"],
    }


def _validate_authoritative_analysis_runs(
    repository: ProjectRepository,
    analysis_id: str,
) -> None:
    """Fail closed before projecting a board from persisted exact metrics."""

    analysis = repository.get_analysis(analysis_id)
    rows = repository._connection.execute(
        "SELECT job_id FROM solver_jobs WHERE analysis_id=? AND state='SUCCEEDED' ORDER BY job_id",
        (analysis_id,),
    ).fetchall()
    for row in rows:
        _load_authoritative_run(
            repository,
            str(row["job_id"]),
            expected_analysis_id=analysis_id,
            expected_project_id=analysis["project_id"],
        )


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
        "j.physics_cache_key,j.payload_json,j.updated_at,a.project_id,"
        "a.payload_json AS analysis_payload_json "
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
        analysis_payload = json.loads(row["analysis_payload_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"solver job {job_id!r} has invalid persisted evidence JSON") from exc
    if not isinstance(job_payload, dict) or not isinstance(analysis_payload, dict):
        raise TypeError(f"solver job {job_id!r} has non-object persisted evidence metadata")

    model_revision_id = analysis_payload.get("model_revision_id")
    model_revision_sha256 = analysis_payload.get("model_sha256")
    extractor_version = analysis_payload.get("metric_extractor_version")
    if not isinstance(model_revision_id, str) or not model_revision_id:
        raise ValueError(f"analysis for job {job_id!r} is not pinned to a model revision")
    _require_sha256(
        model_revision_sha256,
        label=f"model revision hash for job {job_id!r}",
    )
    if not isinstance(extractor_version, str) or not extractor_version.strip():
        raise ValueError(f"analysis for job {job_id!r} has no metric extractor version")
    revision = repository.get_model_revision(model_revision_id)
    if (
        revision["project_id"] != row["project_id"]
        or revision["model_sha256"] != model_revision_sha256
    ):
        raise ValueError(f"analysis for job {job_id!r} conflicts with its ModelRevision")
    if (
        job_payload.get("model_revision_id") != model_revision_id
        or job_payload.get("model_revision_sha256") != model_revision_sha256
        or job_payload.get("metric_extractor_version") != extractor_version
    ):
        raise ValueError(f"solver job {job_id!r} is not bound to its analysis evidence versions")

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
    missing_roles = sorted({"input", "report", "output"}.difference(artifact_hashes))
    if missing_roles:
        raise ValueError(
            f"solver job {job_id!r} is missing required artifacts: {', '.join(missing_roles)}"
        )
    if "solver" in artifact_hashes and artifact_hashes["solver"] != solver_sha256:
        raise ValueError(
            f"solver artifact does not match the recorded solver hash for job {job_id!r}"
        )
    for payload_key, role in (
        ("input_sha256", "input"),
        ("report_sha256", "report"),
        ("output_sha256", "output"),
    ):
        persisted = job_payload.get(payload_key)
        if persisted != artifact_hashes[role]:
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
        model_revision_id=model_revision_id,
        design_id=row["design_id"],
        storm_id=row["storm_id"],
        metrics=metrics,
        qa_status=derived_qa,
        qa_findings=qa_findings,
        input_sha256=artifact_hashes["input"],
        report_sha256=artifact_hashes["report"],
        solver_sha256=solver_sha256,
        output_sha256=artifact_hashes["output"],
        extractor_version=extractor_version,
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


def _telemetry_idempotency_projection(envelope: dict[str, Any]) -> dict[str, Any]:
    """Compare automatic telemetry by evidence, excluding generated timestamps only."""

    projected = json.loads(json.dumps(envelope, sort_keys=True, default=str))
    projected.pop("created_at", None)
    artifacts = projected.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict):
                artifact.pop("created_at", None)
    return projected


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
    guided_fixture_path: str | Path | None = None,
    guided_evidence_directory: str | Path | None = None,
    require_observability_session: bool = False,
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
        guided_fixture_path=guided_fixture_path,
        guided_evidence_directory=guided_evidence_directory,
        require_observability_session=require_observability_session,
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
    parser.add_argument("--guided-fixture", default=None)
    parser.add_argument("--guided-evidence-directory", default=None)
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
        guided_fixture_path=args.guided_fixture,
        guided_evidence_directory=args.guided_evidence_directory,
        require_observability_session=False,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
