"""Durable direct Circuitscape analysis orchestration.

The executor is deliberately sequential in v0.1.  Deterministic baseline-first
ordering, transactionally persisted job boundaries, and an isolated process per
portfolio/scenario are more important than speculative throughput.  The RPC
thread only schedules work; all direct execution happens in a background worker.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scenario_platform.contracts import AnalysisState, DirectJobState
from scenario_platform.process_lock import FileLock, FileLockTimeout
from scenario_platform.storage import (
    ContentAddressedStore,
    PathSecurityError,
    atomic_write_bytes,
    contained_path,
)

from .contracts import (
    ConnectivityAsset,
    ConnectivityDecisionRule,
    ConnectivityEvidenceClaim,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    ConnectivityScenario,
    DiagnosticMapSummary,
    DirectConnectivityRun,
    EngineManifest,
    EvidenceEdge,
    EvidenceNode,
    PairResistanceMetric,
    PortfolioDecisionSummary,
    QAStatus,
    RuleMetric,
    RuleOperator,
    canonical_sha256,
)
from .cost_basis import ANALYSIS_HORIZON_PREFIX, parse_reviewed_cost_basis
from .engine import (
    CircuitscapeAdapter,
    EngineExecution,
    EngineProtocolError,
    EngineProtocolResult,
    PreparedConnectivityRun,
    PreparedPair,
    PreparedResource,
    contained_regular_file,
    engine_request_payload,
    parse_circuitscape_matrix,
    sha256_file,
    validate_effective_circuitscape_configuration,
)
from .footprints import (
    create_professional_footprint_proof_document,
    footprint_proof_set_sha256,
    prove_action_effect_footprints,
    verify_professional_footprint_proof_document,
)
from .grid import InMemoryRaster, assert_same_grid
from .guided_evidence import GuidedEvidenceResource
from .guided_rerun import build_differential_report, build_pinned_reference, matrix_summaries
from .interventions import (
    apply_absolute_replacements,
    portfolio_cost,
    scenario_effects_for_portfolio,
    unresolved_portfolio_concerns,
)
from .metrics import (
    CONNECTIVITY_LIMITATION,
    CURRENT_MAP_LIMITATION,
    PERMITTED_CLAIM_PREFIX,
    attach_baseline_metrics,
    build_pair_metric_claim,
    evaluate_portfolio,
    validate_direct_run_qa,
    validate_evidence_claim,
)
from .project_store import EcoProjectStore, SolverShutdownReceiptError
from .raster_io import (
    RasterIOError,
    compare_current_maps_in_mask,
    current_map_values_and_nodata,
    load_raster,
    write_esri_ascii,
)
from .resource_observability import ObservedProcessCancelled, unmeasured_cache_observation
from .ridgeway import RidgewayFixture, build_ridgeway_fixture
from .semantic_diff import (
    canonical_semantic_raster_diff_bytes,
    semantic_raster_diff_payload,
    verify_semantic_raster_diff,
)


class DurableAnalysisError(RuntimeError):
    pass


class AnalysisOwnershipError(DurableAnalysisError):
    """Another live worker owns the project's scientific execution lease."""


class OrphanProcessAuditError(DurableAnalysisError):
    """A prior worker may have left a still-live solver process tree."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class CurrentMapQAError(DurableAnalysisError):
    """An engine current raster cannot support a diagnostic comparison."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class _Control:
    project_directory: Path
    analysis_id: str
    pause_requested: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    shutdown_requested: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    engine_executable: Path | None = None
    project_lock: FileLock | None = None
    shutdown_pre_jobs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    process_exit_evidence: dict[str, dict[str, Any]] = field(default_factory=dict)


class DurableAnalysisExecutor:
    """Background, crash-recoverable implementation of the RPC executor protocol."""

    def __init__(
        self,
        engine_manifest: EngineManifest,
        *,
        lock_root: Path,
        engine_executable: Path | None = None,
        engine_sha256: str | None = None,
    ) -> None:
        self.engine_manifest = engine_manifest
        self._lock_root = self._prepare_lock_root(lock_root)
        self._configured_engine = engine_executable.resolve() if engine_executable else None
        self._configured_engine_sha256 = engine_sha256
        if (self._configured_engine is None) != (self._configured_engine_sha256 is None):
            raise ValueError("executor engine path and SHA-256 must be configured together")
        if self._configured_engine is not None and self._configured_engine_sha256 is not None:
            self._validate_engine(self._configured_engine, self._configured_engine_sha256)
        self._controls: dict[tuple[Path, str], _Control] = {}
        self._project_controls: dict[Path, _Control] = {}
        self._lock = threading.RLock()
        self._shutdown_requested = threading.Event()

    @staticmethod
    def _prepare_lock_root(lock_root: Path) -> Path:
        """Create a private, trusted directory for process-ownership locks."""

        candidate = Path(lock_root).expanduser()
        if candidate.exists() and candidate.is_symlink():
            raise DurableAnalysisError("analysis lock root cannot be a symbolic link")
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = os.lstat(candidate)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise DurableAnalysisError("analysis lock root must be a real directory")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise DurableAnalysisError("analysis lock root must be owned by the current user")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise DurableAnalysisError("analysis lock root must not be accessible to other users")
        return candidate.resolve(strict=True)

    def _project_lock_path(self, project_directory: Path) -> Path:
        identity = hashlib.sha256(os.fsencode(str(project_directory.resolve()))).hexdigest()
        return self._lock_root / f"{identity}.lock"

    @staticmethod
    def _key(project_directory: Path, analysis_id: str) -> tuple[Path, str]:
        return project_directory.resolve(), analysis_id

    def _acquire_project_control(
        self, project_directory: Path, analysis_id: str
    ) -> tuple[_Control, bool]:
        """Exclusively own scientific execution for one project process at a time."""

        root = project_directory.resolve()
        key = self._key(root, analysis_id)
        with self._lock:
            if self._shutdown_requested.is_set():
                raise DurableAnalysisError("the scientific worker is shutting down")
            owner = self._project_controls.get(root)
            if owner is not None and owner.project_lock is not None:
                if owner.analysis_id == analysis_id:
                    if owner.thread is not None and owner.thread.is_alive():
                        return owner, False
                    return owner, True
                raise AnalysisOwnershipError(
                    "another direct analysis is already running for this project"
                )
            if owner is not None:
                self._project_controls.pop(root, None)
                if owner.project_lock is not None:
                    owner.project_lock.release()
                    owner.project_lock = None
            # The RPC thread acquires ownership and the background solver
            # thread releases it, so the lock context must be process-shared.
            project_lock = FileLock(str(self._project_lock_path(root)), thread_local=False)
            try:
                # A PAUSED state commits immediately before the worker's
                # finally block releases its OS lock. Permit that bounded
                # handoff without allowing a second live owner.
                project_lock.acquire(timeout=1)
            except FileLockTimeout as exc:
                raise AnalysisOwnershipError(
                    "another application process owns this project's direct analysis"
                ) from exc
            control = self._controls.get(key) or _Control(root, analysis_id)
            control.project_directory = root
            control.analysis_id = analysis_id
            control.project_lock = project_lock
            self._controls[key] = control
            self._project_controls[root] = control
            return control, True

    def _release_project_control(self, control: _Control) -> None:
        with self._lock:
            project_lock = control.project_lock
            if project_lock is not None:
                project_lock.release()
                control.project_lock = None
            if self._project_controls.get(control.project_directory) is control:
                self._project_controls.pop(control.project_directory, None)

    def is_active(self, *, project_directory: Path, analysis_id: str) -> bool:
        """Return whether this worker currently owns the analysis execution thread.

        Guided matrix and map barriers share one primary control.  Resolve that
        alias from SQLite through a read-only connection so a status check never
        performs startup recovery or otherwise changes authoritative state.
        """

        root = project_directory.resolve()
        primary_analysis_id = analysis_id
        database = root / "project.sqlite"
        if database.is_file() and not database.is_symlink():
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
                rows = connection.execute(
                    "SELECT record_id,payload_json FROM eco_records "
                    "WHERE record_type='guided_rerun'"
                ).fetchall()
                for record_id, payload_json in rows:
                    payload = json.loads(payload_json)
                    if analysis_id in {
                        str(payload.get("analysis_id", "")),
                        str(payload.get("map_analysis_id", "")),
                    }:
                        primary_analysis_id = str(record_id)
                        break
            except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError):
                return False
            finally:
                if connection is not None:
                    connection.close()
        with self._lock:
            control = self._controls.get(self._key(root, primary_analysis_id))
            return bool(control and control.thread and control.thread.is_alive())

    @staticmethod
    def _project_path_identity(project_directory: Path) -> str:
        return hashlib.sha256(os.fsencode(str(project_directory.resolve()))).hexdigest()

    @staticmethod
    def _workflow_identity(store: EcoProjectStore, analysis_id: str) -> str:
        analysis = store.analysis(analysis_id)
        bundle = store.analysis_bundle(analysis_id)
        return canonical_sha256(
            {
                "analysis_id": analysis_id,
                "plan": analysis["plan"],
                "bundle": bundle.model_dump(mode="json"),
            }
        )

    def _consume_shutdown_recovery(self, store: EcoProjectStore, analysis_id: str) -> int:
        if not any(
            item["state"] == DirectJobState.RUNNING.value
            for item in store.direct_job_state_snapshot(analysis_id)
        ):
            return 0
        try:
            return store.consume_solver_shutdown_receipt(
                analysis_id,
                project_path_sha256=self._project_path_identity(store.root),
                engine_manifest_sha256=self.engine_manifest.sha256,
                workflow_identity_sha256=self._workflow_identity(store, analysis_id),
            )
        except SolverShutdownReceiptError as exc:
            raise OrphanProcessAuditError(exc.code, str(exc)) from exc

    @staticmethod
    def _remember_process_exit(
        control: _Control,
        job: dict[str, Any],
        evidence: dict[str, Any] | None,
    ) -> None:
        """Bind process-tree evidence to the exact claimed direct-job attempt."""

        if evidence is None:
            return
        control.process_exit_evidence[str(job["direct_job_id"])] = {
            **evidence,
            "direct_job_id": str(job["direct_job_id"]),
            "attempt_count": int(job["attempt_count"]),
        }

    @classmethod
    def _require_confirmed_process_exit(
        cls,
        control: _Control,
        job: dict[str, Any],
        evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Fail closed unless the exact solver tree is authoritatively gone.

        A direct child exiting is insufficient: descendants may still be
        writing outputs while a later attempt begins.  Only POSIX process-group
        absence, or a future native Windows Job Object active-process-zero
        receipt, may authorize a terminal scientific result.
        """

        cls._remember_process_exit(control, job, evidence)
        bound = control.process_exit_evidence.get(str(job["direct_job_id"]))
        allowed_methods = {
            "POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT",
            "POSIX_PROCESS_GROUP_ABSENT_AFTER_SIGTERM",
            "POSIX_PROCESS_GROUP_ABSENT_AFTER_SIGKILL",
            "WINDOWS_JOB_OBJECT_ACTIVE_PROCESS_ZERO",
        }
        process_id = bound.get("process_id") if bound is not None else None
        process_group_id = bound.get("process_group_id") if bound is not None else None
        process_identity = bound.get("process_identity") if bound is not None else None
        method = bound.get("confirmation_method") if bound is not None else None
        identity_valid = isinstance(process_identity, str) and len(process_identity) == 64
        if identity_valid:
            try:
                int(process_identity, 16)
            except ValueError:
                identity_valid = False
        if (
            bound is None
            or bound.get("tree_exit_confirmed") is not True
            or bound.get("direct_job_id") != str(job["direct_job_id"])
            or bound.get("attempt_count") != int(job["attempt_count"])
            or not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id <= 0
            or not isinstance(process_group_id, int)
            or isinstance(process_group_id, bool)
            or process_group_id <= 0
            or not identity_valid
            or method not in allowed_methods
        ):
            raise OrphanProcessAuditError(
                "ORPHAN_TREE_EXIT_UNCONFIRMED",
                "the direct solver result is non-reportable because its complete process "
                "tree was not authoritatively confirmed absent",
            )
        return bound

    @staticmethod
    def _confirmed_running_attempts(
        store: EcoProjectStore,
        control: _Control,
        analysis_id: str,
    ) -> bool:
        """Return true only when every RUNNING row has exact tree-exit evidence."""

        running = [
            item
            for item in store.direct_job_state_snapshot(analysis_id)
            if item["state"] == DirectJobState.RUNNING.value
        ]
        for item in running:
            evidence = control.process_exit_evidence.get(str(item["direct_job_id"]))
            if (
                evidence is None
                or evidence.get("tree_exit_confirmed") is not True
                or evidence.get("direct_job_id") != item["direct_job_id"]
                or evidence.get("attempt_count") != item["attempt_count"]
            ):
                return False
        return True

    def _active_control_analysis(self, store: EcoProjectStore, control: _Control) -> str:
        guided = self._guided_metadata(store, control.analysis_id)
        return (
            self._guided_active_analysis(guided[1]) if guided is not None else control.analysis_id
        )

    def _persist_shutdown_receipts(self, controls: list[_Control]) -> bool:
        safe = True
        for control in controls:
            store = EcoProjectStore(control.project_directory)
            try:
                targets = set(control.shutdown_pre_jobs)
                try:
                    targets.add(self._active_control_analysis(store, control))
                except KeyError:
                    continue
                for analysis_id in sorted(targets):
                    try:
                        post_jobs = store.direct_job_state_snapshot(analysis_id)
                    except KeyError:
                        continue
                    if not any(item["state"] == "RUNNING" for item in post_jobs):
                        continue
                    pre_jobs = control.shutdown_pre_jobs.get(analysis_id, [])
                    try:
                        store.record_solver_shutdown_receipt(
                            analysis_id,
                            project_path_sha256=self._project_path_identity(
                                control.project_directory
                            ),
                            engine_manifest_sha256=self.engine_manifest.sha256,
                            workflow_identity_sha256=self._workflow_identity(store, analysis_id),
                            pre_jobs=pre_jobs,
                            post_jobs=post_jobs,
                            process_exits=control.process_exit_evidence,
                        )
                    except (SolverShutdownReceiptError, KeyError, OSError, ValueError):
                        # No receipt is safer than one that might authorize a
                        # duplicate solve while an orphan process is still live.
                        safe = False
            finally:
                store.close()
        return safe

    def shutdown(self, *, timeout_seconds: float = 5.0) -> bool:
        """Stop process-owned execution without changing durable user intent.

        This signal is deliberately distinct from ``cancel``. Active subprocesses
        are interrupted through the existing process-group cancellation callback.
        Only after every owned thread joins may exact tree-exit receipts authorize
        a later worker to requeue the still-RUNNING rows.
        """

        if timeout_seconds < 0:
            raise ValueError("shutdown timeout cannot be negative")
        self._shutdown_requested.set()
        with self._lock:
            controls = list({id(item): item for item in self._controls.values()}.values())
            for control in controls:
                control.shutdown_requested.set()
            threads = [
                control.thread
                for control in controls
                if control.thread is not None and control.thread.is_alive()
            ]
        for control in controls:
            store = EcoProjectStore(control.project_directory)
            try:
                try:
                    analysis_id = self._active_control_analysis(store, control)
                    control.shutdown_pre_jobs[analysis_id] = store.direct_job_state_snapshot(
                        analysis_id
                    )
                except KeyError:
                    continue
            finally:
                store.close()
        deadline = time.monotonic() + timeout_seconds
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
        if not all(not thread.is_alive() for thread in threads):
            return False
        return self._persist_shutdown_receipts(controls)

    @staticmethod
    def _direct_job_id(analysis_id: str, portfolio_id: str, scenario_id: str) -> str:
        digest = canonical_sha256(
            {
                "analysis_id": analysis_id,
                "portfolio_id": portfolio_id,
                "scenario_id": scenario_id,
                "phase": "MATRIX",
            }
        )
        return f"direct_{digest[:24]}"

    def _validate_plan(self, bundle: ConnectivityProjectBundle, plan: ConnectivityRunPlan) -> None:
        if plan.engine_manifest_sha256 != self.engine_manifest.sha256:
            raise DurableAnalysisError("run plan references another EngineManifest")
        scenario_by_id = {item.scenario_id: item for item in bundle.scenarios}
        if set(plan.scenario_ids) != set(scenario_by_id):
            raise DurableAnalysisError("run plan omits a required target or safeguard scenario")
        for scenario in scenario_by_id.values():
            if (
                scenario.neighbor_rule != self.engine_manifest.neighbor_rule
                or scenario.resistance_average != self.engine_manifest.resistance_average
                or scenario.precision != self.engine_manifest.precision
                or scenario.julia_threads != self.engine_manifest.julia_threads
                or scenario.blas_threads != self.engine_manifest.blas_threads
            ):
                raise DurableAnalysisError(
                    f"scenario {scenario.scenario_id} differs from the pinned engine settings"
                )
        portfolio_ids = {item.portfolio_id for item in plan.portfolios}
        current_ids = set(plan.current_maps_for_portfolio_ids)
        if not current_ids.issubset(portfolio_ids):
            raise DurableAnalysisError("current-map requests reference an unplanned portfolio")
        current_rules = [
            item
            for item in bundle.decision_rules
            if item.metric == RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT and item.required
        ]
        if current_rules:
            if not bundle.review_masks:
                raise DurableAnalysisError(
                    "a required current-map rule has no configured review mask"
                )
            if current_ids != portfolio_ids:
                raise DurableAnalysisError(
                    "a required current-map rule requires maps for every compared portfolio"
                )

    def _job_specs(self, plan: ConnectivityRunPlan) -> list[dict[str, Any]]:
        by_id = {item.portfolio_id: item for item in plan.portfolios}
        portfolio_order = [plan.baseline_portfolio_id] + sorted(
            set(by_id) - {plan.baseline_portfolio_id}
        )
        emit_ids = set(plan.current_maps_for_portfolio_ids)
        if emit_ids:
            emit_ids.add(plan.baseline_portfolio_id)
        jobs: list[dict[str, Any]] = []
        for portfolio_id in portfolio_order:
            for scenario_id in sorted(plan.scenario_ids):
                jobs.append(
                    {
                        "direct_job_id": self._direct_job_id(
                            plan.analysis_id, portfolio_id, scenario_id
                        ),
                        "portfolio_id": portfolio_id,
                        "scenario_id": scenario_id,
                        "phase": "MATRIX",
                        "sequence_number": len(jobs),
                        "emit_current_map": portfolio_id in emit_ids,
                    }
                )
        return jobs

    def start(
        self,
        *,
        project_directory: Path,
        bundle: ConnectivityProjectBundle,
        plan: ConnectivityRunPlan,
        engine_executable: Path,
        engine_sha256: str,
    ) -> dict[str, Any]:
        project_directory = project_directory.resolve()
        self._validate_engine(engine_executable, engine_sha256)
        self._configured_engine = engine_executable.resolve()
        self._configured_engine_sha256 = engine_sha256
        self._validate_plan(bundle, plan)
        control, acquired = self._acquire_project_control(project_directory, plan.analysis_id)
        if not acquired:
            return {"state": "RUNNING"}
        try:
            store = EcoProjectStore(project_directory)
            try:
                store.start_analysis_with_direct_jobs(plan.analysis_id, self._job_specs(plan))
                self._consume_shutdown_recovery(store, plan.analysis_id)
            finally:
                store.close()
            self._launch(project_directory, plan.analysis_id, engine_executable)
        except BaseException:
            self._release_project_control(control)
            raise
        return {"state": "RUNNING"}

    def start_guided(
        self,
        *,
        project_directory: Path,
        bundle: ConnectivityProjectBundle,
        plan: ConnectivityRunPlan,
        engine_executable: Path,
        engine_sha256: str,
        session_id: str,
        guided_evidence: GuidedEvidenceResource,
    ) -> dict[str, Any]:
        """Start the complete 1,536 matrix + 60 map Ridgeway direct study.

        The two barriers are linked but deliberately stored as two analyses.
        This preserves the professional invariant of one direct result per
        portfolio/scenario while retaining the 60 additional map executions as
        independent direct evidence.
        """

        project_directory = project_directory.resolve()
        self._validate_engine(engine_executable, engine_sha256)
        self._configured_engine = engine_executable.resolve()
        self._configured_engine_sha256 = engine_sha256
        fixture = build_ridgeway_fixture()
        if bundle != fixture.bundle:
            raise DurableAnalysisError("guided rerun bundle differs from the pinned Ridgeway model")
        if (
            plan.engine_manifest_sha256 != self.engine_manifest.sha256
            or plan.revision_id != fixture.bundle.revision.revision_id
            or plan.baseline_portfolio_id != fixture.baseline_portfolio_id
            or plan.direct_job_count != fixture.direct_matrix_job_count
            or plan.current_maps_for_portfolio_ids
            or [item.scenario_id for item in fixture.bundle.scenarios] != plan.scenario_ids
            or {item.portfolio_id for item in plan.portfolios}
            != {item.portfolio_id for item in fixture.portfolios}
        ):
            raise DurableAnalysisError(
                "guided rerun plan is not the fixed 1,536-job Ridgeway study"
            )
        reference = build_pinned_reference(
            guided_evidence,
            fixture,
            materialize_parent=project_directory / "pinned_reference",
        )
        map_analysis_id = f"{plan.analysis_id}_maps"
        metadata = {
            "schema_version": "ridgeway-guided-rerun/v1.0",
            "session_id": session_id,
            "analysis_id": plan.analysis_id,
            "map_analysis_id": map_analysis_id,
            "phase": "MATRIX",
            "total_jobs": 1_596,
            "matrix_jobs": 1_536,
            "map_jobs": 60,
            "reference": reference,
            "differential": None,
            "failure": None,
        }
        control, acquired = self._acquire_project_control(project_directory, plan.analysis_id)
        if not acquired:
            return {"state": "RUNNING"}
        try:
            store = EcoProjectStore(project_directory)
            try:
                if store.project()["mode"] != "GUIDED":
                    raise DurableAnalysisError("a Ridgeway rerun requires a guided project")
                store.put_record("guided_rerun", plan.analysis_id, metadata, allow_update=False)
                self._update_guided_session(store, metadata, "RUNNING")
                store.start_analysis_with_direct_jobs(plan.analysis_id, self._job_specs(plan))
                self._consume_shutdown_recovery(store, plan.analysis_id)
            finally:
                store.close()
            self._launch_guided(project_directory, plan.analysis_id, engine_executable)
        except BaseException:
            self._release_project_control(control)
            raise
        return {"state": "RUNNING"}

    def _launch_guided(self, project_directory: Path, analysis_id: str, executable: Path) -> None:
        key = self._key(project_directory, analysis_id)
        control, acquired = self._acquire_project_control(key[0], analysis_id)
        if not acquired:
            return
        try:
            self._publish_control_thread(
                control,
                target=self._run_guided_loop,
                executable=executable,
                name=f"ecocorridor-guided-{analysis_id}",
            )
        except BaseException:
            self._release_project_control(control)
            raise

    @staticmethod
    def _guided_metadata(
        store: EcoProjectStore, analysis_id: str
    ) -> tuple[str, dict[str, Any]] | None:
        try:
            return analysis_id, store.get_record("guided_rerun", analysis_id)
        except KeyError:
            for record in store.list_records("guided_rerun"):
                if record.get("map_analysis_id") == analysis_id:
                    return str(record["analysis_id"]), record
        return None

    @staticmethod
    def _guided_active_analysis(metadata: dict[str, Any]) -> str:
        return (
            str(metadata["analysis_id"])
            if metadata["phase"] == "MATRIX"
            else str(metadata["map_analysis_id"])
        )

    @staticmethod
    def _update_guided_session(
        store: EcoProjectStore, metadata: dict[str, Any], state: str
    ) -> None:
        session = store.guided(str(metadata["session_id"]))
        store.set_guided(str(metadata["session_id"]), state, session["projection"])

    def _settle_pause_intent(
        self,
        *,
        store: EcoProjectStore,
        control: _Control,
        analysis_id: str,
        guided_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Commit a user pause even when process shutdown interrupts an active job."""

        state = AnalysisState(store.analysis(analysis_id)["state"])
        if not (control.pause_requested.is_set() or state == AnalysisState.PAUSING):
            return False
        running = any(
            item["state"] == DirectJobState.RUNNING.value
            for item in store.direct_job_state_snapshot(analysis_id)
        )
        if running and not self._confirmed_running_attempts(store, control, analysis_id):
            raise OrphanProcessAuditError(
                "ORPHAN_TREE_EXIT_UNCONFIRMED",
                "a paused direct job cannot be requeued until its exact process tree is "
                "confirmed absent",
            )
        if state == AnalysisState.RUNNING:
            store.request_pause(analysis_id)
        if running:
            store.recover_abandoned_direct_jobs(
                analysis_id,
                process_tree_exit_confirmed=True,
            )
        store.mark_paused(analysis_id)
        if guided_metadata is not None:
            self._update_guided_session(store, guided_metadata, "PAUSED")
        return True

    def guided_status(self, *, project_directory: Path, analysis_id: str) -> dict[str, Any]:
        store = EcoProjectStore(project_directory)
        try:
            located = self._guided_metadata(store, analysis_id)
            if located is None:
                raise DurableAnalysisError("guided rerun metadata is unavailable")
            primary_id, metadata = located
            matrix_counts = store.direct_job_counts(primary_id)
            try:
                map_counts = store.direct_job_counts(str(metadata["map_analysis_id"]))
            except KeyError:
                map_counts = {}
            matrix_completed = int(matrix_counts.get(DirectJobState.SUCCEEDED.value, 0))
            map_completed = int(map_counts.get(DirectJobState.SUCCEEDED.value, 0))
            cached = sum(
                int(item["cached"])
                for run_id in (primary_id, str(metadata["map_analysis_id"]))
                for item in store.direct_jobs(run_id)
            )
            phase = str(metadata["phase"])
            if phase == "COMPLETE":
                state = "COMPLETED"
            elif metadata.get("failure") is not None:
                state = (
                    "CANCELLED"
                    if metadata["failure"].get("code") == "GUIDED_RERUN_CANCELLED"
                    else "FAILED"
                )
            elif phase == "DIFFERENTIAL":
                state = "RUNNING"
            else:
                active_id = self._guided_active_analysis(metadata)
                state = str(store.analysis(active_id)["state"])
            return {
                "session_id": str(metadata["session_id"]),
                "analysis_id": primary_id,
                "state": state,
                "phase": phase,
                "completed_jobs": matrix_completed + map_completed,
                "total_jobs": 1_596,
                "matrix_completed": matrix_completed,
                "map_completed": map_completed,
                "cached_jobs": cached,
                "differential": metadata.get("differential"),
            }
        finally:
            store.close()

    def _validate_engine(self, executable: Path, expected_sha256: str) -> None:
        metadata = os.lstat(executable)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise DurableAnalysisError("packaged Circuitscape engine is absent or unsafe")
        if sha256_file(executable) != expected_sha256:
            raise DurableAnalysisError("packaged Circuitscape engine hash changed")

    def _launch(self, project_directory: Path, analysis_id: str, executable: Path) -> None:
        key = self._key(project_directory, analysis_id)
        control, acquired = self._acquire_project_control(key[0], analysis_id)
        if not acquired:
            return
        try:
            self._publish_control_thread(
                control,
                target=self._run_loop,
                executable=executable,
                name=f"ecocorridor-{analysis_id}",
            )
        except BaseException:
            self._release_project_control(control)
            raise

    def _publish_control_thread(
        self,
        control: _Control,
        *,
        target: Any,
        executable: Path,
        name: str,
    ) -> None:
        """Atomically publish a worker thread or observe process shutdown.

        Shutdown snapshots live threads while holding the same lock. It can
        therefore never return successfully while a launch that started first
        remains unpublished and unjoined.
        """

        with self._lock:
            if self._shutdown_requested.is_set() or control.shutdown_requested.is_set():
                raise DurableAnalysisError("the scientific worker is shutting down")
            control.pause_requested.clear()
            control.cancel_requested.clear()
            thread = threading.Thread(
                target=target,
                args=(control, executable.resolve()),
                name=name,
                daemon=True,
            )
            control.thread = thread
            thread.start()

    def pause(self, *, project_directory: Path, analysis_id: str) -> None:
        key = self._key(project_directory, analysis_id)
        target_analysis_id = analysis_id
        primary_analysis_id = analysis_id
        store = EcoProjectStore(key[0])
        try:
            guided = self._guided_metadata(store, analysis_id)
            if guided is not None:
                primary_analysis_id, metadata = guided
                target_analysis_id = self._guided_active_analysis(metadata)
        finally:
            store.close()
        key = self._key(project_directory, primary_analysis_id)
        with self._lock:
            control = self._controls.get(key)
            if control is None:
                control = _Control(key[0], primary_analysis_id)
                self._controls[key] = control
            control.pause_requested.set()
        store = EcoProjectStore(key[0])
        try:
            state = AnalysisState(store.analysis(target_analysis_id)["state"])
            if state == AnalysisState.RUNNING:
                store.request_pause(target_analysis_id)
        finally:
            store.close()

    def resume(self, *, project_directory: Path, analysis_id: str) -> None:
        key = self._key(project_directory, analysis_id)
        store = EcoProjectStore(key[0])
        try:
            guided = self._guided_metadata(store, analysis_id)
            primary_analysis_id = guided[0] if guided is not None else analysis_id
            target_analysis_id = (
                self._guided_active_analysis(guided[1]) if guided is not None else analysis_id
            )
        finally:
            store.close()
        control, acquired = self._acquire_project_control(key[0], primary_analysis_id)
        if not acquired:
            if control.thread is not None:
                control.thread.join(timeout=2)
            control, acquired = self._acquire_project_control(key[0], primary_analysis_id)
            if not acquired:
                raise DurableAnalysisError("the pausing analysis worker did not release ownership")
        try:
            store = EcoProjectStore(key[0])
            try:
                state = AnalysisState(store.analysis(target_analysis_id)["state"])
                if state == AnalysisState.PAUSED:
                    store.transition_analysis(target_analysis_id, AnalysisState.RUNNING)
                elif state != AnalysisState.RUNNING:
                    raise DurableAnalysisError(f"analysis cannot resume from {state.value}")
                if guided is not None:
                    self._update_guided_session(store, guided[1], "RUNNING")
            finally:
                store.close()
            control.pause_requested.clear()
            control.cancel_requested.clear()
            executable = self._engine_from_prepared_jobs(key[0], primary_analysis_id)
            if guided is not None:
                self._launch_guided(key[0], primary_analysis_id, executable)
            else:
                self._launch(key[0], primary_analysis_id, executable)
        except BaseException:
            self._release_project_control(control)
            raise

    def _engine_from_prepared_jobs(self, project_directory: Path, analysis_id: str) -> Path:
        """Resolve the immutable executable recorded by the current worker instance."""

        # The executable is process configuration, never project-controlled state.
        with self._lock:
            control = self._controls.get(self._key(project_directory, analysis_id))
            if control is not None and control.engine_executable is not None:
                return control.engine_executable
            if self._configured_engine is not None and self._configured_engine_sha256 is not None:
                self._validate_engine(self._configured_engine, self._configured_engine_sha256)
                return self._configured_engine
        raise DurableAnalysisError(
            "resume after worker restart requires the RPC host to call recover with its verified engine"
        )

    def recover(
        self,
        *,
        project_directory: Path,
        analysis_id: str,
        engine_executable: Path,
        engine_sha256: str,
    ) -> None:
        self._validate_engine(engine_executable, engine_sha256)
        store = EcoProjectStore(project_directory)
        try:
            guided = self._guided_metadata(store, analysis_id)
            primary_analysis_id = guided[0] if guided is not None else analysis_id
            key = self._key(project_directory, primary_analysis_id)
            target_analysis_id = (
                self._guided_active_analysis(guided[1]) if guided is not None else analysis_id
            )
        finally:
            store.close()
        control, acquired = self._acquire_project_control(key[0], primary_analysis_id)
        if not acquired:
            return
        try:
            store = EcoProjectStore(project_directory)
            try:
                state = AnalysisState(store.analysis(target_analysis_id)["state"])
                recoverable_states = {AnalysisState.RUNNING, AnalysisState.PAUSED}
                # A worker may stop after atomically completing one guided barrier
                # but before advancing the durable guided phase.  The completed
                # barrier is valid recovery input: the guided loop will verify its
                # exact coverage, create/finish the next barrier, and never rerun or
                # mutate the completed direct evidence.
                if guided is not None:
                    recoverable_states.add(AnalysisState.COMPLETED)
                if state not in recoverable_states:
                    raise DurableAnalysisError(f"analysis cannot recover from {state.value}")
                self._consume_shutdown_recovery(store, target_analysis_id)
                if state == AnalysisState.PAUSED:
                    store.transition_analysis(target_analysis_id, AnalysisState.RUNNING)
                if guided is not None:
                    self._update_guided_session(store, guided[1], "RUNNING")
            finally:
                store.close()
            if guided is not None:
                self._launch_guided(
                    project_directory.resolve(), primary_analysis_id, engine_executable
                )
            else:
                self._launch(project_directory.resolve(), primary_analysis_id, engine_executable)
        except BaseException:
            self._release_project_control(control)
            raise

    def cancel(self, *, project_directory: Path, analysis_id: str) -> None:
        key = self._key(project_directory, analysis_id)
        store = EcoProjectStore(key[0])
        try:
            guided = self._guided_metadata(store, analysis_id)
            primary_analysis_id = guided[0] if guided is not None else analysis_id
            target_analysis_id = (
                self._guided_active_analysis(guided[1]) if guided is not None else analysis_id
            )
        finally:
            store.close()
        key = self._key(project_directory, primary_analysis_id)
        with self._lock:
            control = self._controls.get(key)
            if control is None:
                control = _Control(key[0], primary_analysis_id)
                self._controls[key] = control
            control.cancel_requested.set()
        store = EcoProjectStore(key[0])
        try:
            store.cancel_queued_direct_jobs(target_analysis_id)
            state = AnalysisState(store.analysis(target_analysis_id)["state"])
            if state in {AnalysisState.RUNNING, AnalysisState.PAUSING, AnalysisState.PAUSED}:
                store.transition_analysis(target_analysis_id, AnalysisState.CANCELLED)
            if guided is not None:
                metadata = dict(guided[1])
                metadata["failure"] = {"code": "GUIDED_RERUN_CANCELLED"}
                store.put_record("guided_rerun", primary_analysis_id, metadata)
                self._update_guided_session(store, metadata, "CANCELLED")
        finally:
            store.close()

    def _create_guided_map_analysis(
        self,
        *,
        store: EcoProjectStore,
        fixture: RidgewayFixture,
        metadata: dict[str, Any],
    ) -> ConnectivityRunPlan:
        selection = metadata["reference"]["teaching_selection"]
        portfolio_ids = [str(item) for item in selection["map_rerun_portfolio_ids"]]
        if portfolio_ids[0] != fixture.baseline_portfolio_id or len(set(portfolio_ids)) != 5:
            raise DurableAnalysisError("pinned Ridgeway map portfolio selection changed")
        by_id = {item.portfolio_id: item for item in fixture.portfolios}
        try:
            portfolios = [by_id[item] for item in portfolio_ids]
        except KeyError as exc:
            raise DurableAnalysisError("pinned Ridgeway map portfolio is unavailable") from exc
        plan = ConnectivityRunPlan(
            analysis_id=str(metadata["map_analysis_id"]),
            revision_id=fixture.bundle.revision.revision_id,
            scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
            portfolios=portfolios,
            baseline_portfolio_id=fixture.baseline_portfolio_id,
            direct_job_count=fixture.teaching_map_job_count,
            solver_job_cap=fixture.teaching_map_job_count,
            search_method="ENUMERATION",
            seed=0,
            current_maps_for_portfolio_ids=portfolio_ids,
            engine_manifest_sha256=self.engine_manifest.sha256,
        )
        self._validate_plan(fixture.bundle, plan)
        try:
            existing = ConnectivityRunPlan.model_validate(store.analysis(plan.analysis_id)["plan"])
        except KeyError:
            store.create_analysis(plan, bundle=fixture.bundle)
            store.start_analysis_with_direct_jobs(plan.analysis_id, self._job_specs(plan))
        else:
            if existing != plan:
                raise DurableAnalysisError("durable Ridgeway map plan changed")
            existing_state = AnalysisState(store.analysis(plan.analysis_id)["state"])
            if existing_state == AnalysisState.READY:
                store.start_analysis_with_direct_jobs(plan.analysis_id, self._job_specs(plan))
            elif existing_state not in {
                AnalysisState.RUNNING,
                AnalysisState.PAUSED,
                AnalysisState.COMPLETED,
            }:
                raise DurableAnalysisError(
                    f"durable Ridgeway map barrier cannot recover from {existing_state.value}"
                )
        if not store.direct_jobs(plan.analysis_id):
            raise DurableAnalysisError("durable Ridgeway map barrier has no direct-job queue")
        self._consume_shutdown_recovery(store, plan.analysis_id)
        return plan

    def _advance_guided_matrix_barrier(
        self,
        *,
        store: EcoProjectStore,
        fixture: RidgewayFixture,
        plan: ConnectivityRunPlan,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if AnalysisState(store.analysis(plan.analysis_id)["state"]) == AnalysisState.RUNNING:
            runs, _, _ = store.analysis_results(plan.analysis_id)
            if len(runs) != fixture.direct_matrix_job_count:
                raise DurableAnalysisError("Ridgeway matrix barrier is incomplete")
            store.complete_analysis(
                plan.analysis_id,
                summaries=matrix_summaries(fixture, runs),
                claims=[],
            )
        self._create_guided_map_analysis(store=store, fixture=fixture, metadata=metadata)
        updated = dict(metadata)
        updated["phase"] = "MAP"
        updated["failure"] = None
        store.put_record("guided_rerun", plan.analysis_id, updated)
        return updated

    def _complete_guided_map_barrier(
        self,
        *,
        store: EcoProjectStore,
        fixture: RidgewayFixture,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        map_analysis_id = str(metadata["map_analysis_id"])
        map_plan = ConnectivityRunPlan.model_validate(store.analysis(map_analysis_id)["plan"])
        if AnalysisState(store.analysis(map_analysis_id)["state"]) == AnalysisState.RUNNING:
            self._complete_analysis(store, fixture.bundle, map_plan)
        comparing = dict(metadata)
        comparing["phase"] = "DIFFERENTIAL"
        store.put_record("guided_rerun", str(metadata["analysis_id"]), comparing)
        report = build_differential_report(
            store=store,
            fixture=fixture,
            matrix_analysis_id=str(metadata["analysis_id"]),
            map_analysis_id=map_analysis_id,
            local_engine_manifest_sha256=self.engine_manifest.sha256,
            reference=metadata["reference"],
        )
        cas = ContentAddressedStore(store.root / "blobs")
        artifact_sha256 = cas.put_json(report)
        report_record = {**report, "artifact_sha256": artifact_sha256}
        completed = dict(comparing)
        completed["phase"] = "COMPLETE"
        completed["differential"] = report_record
        completed["failure"] = None
        store.record_guided_rerun_completion(
            session_id=str(metadata["session_id"]),
            analysis_id=str(metadata["analysis_id"]),
            map_analysis_id=map_analysis_id,
            completed_metadata=completed,
            differential=report_record,
        )
        return completed

    def _run_guided_loop(self, control: _Control, executable: Path) -> None:
        """Execute both Ridgeway barriers sequentially and recoverably."""

        control.engine_executable = executable
        store = EcoProjectStore(control.project_directory)
        recovered_analyses: set[str] = set()
        try:
            fixture = build_ridgeway_fixture()
            adapter = CircuitscapeAdapter(executable, self.engine_manifest)
            while True:
                located = self._guided_metadata(store, control.analysis_id)
                if located is None:
                    raise DurableAnalysisError("guided rerun metadata disappeared")
                primary_id, metadata = located
                phase = str(metadata["phase"])
                if phase == "COMPLETE":
                    return
                active_id = self._guided_active_analysis(metadata)
                analysis = store.analysis(active_id)
                if active_id not in recovered_analyses:
                    if AnalysisState(analysis["state"]) == AnalysisState.RUNNING:
                        self._consume_shutdown_recovery(store, active_id)
                    recovered_analyses.add(active_id)
                    analysis = store.analysis(active_id)
                if phase == "MATRIX":
                    store.schedule_direct_jobs(
                        active_id,
                        self._job_specs(ConnectivityRunPlan.model_validate(analysis["plan"])),
                    )
                state = AnalysisState(analysis["state"])
                if control.cancel_requested.is_set() or state == AnalysisState.CANCELLED:
                    store.cancel_queued_direct_jobs(active_id)
                    cancelled = dict(metadata)
                    cancelled["failure"] = {"code": "GUIDED_RERUN_CANCELLED"}
                    store.put_record("guided_rerun", primary_id, cancelled)
                    self._update_guided_session(store, cancelled, "CANCELLED")
                    return
                if self._settle_pause_intent(
                    store=store,
                    control=control,
                    analysis_id=active_id,
                    guided_metadata=metadata,
                ):
                    return
                if control.shutdown_requested.is_set():
                    return
                if phase == "MATRIX" and state == AnalysisState.COMPLETED:
                    self._advance_guided_matrix_barrier(
                        store=store,
                        fixture=fixture,
                        plan=ConnectivityRunPlan.model_validate(analysis["plan"]),
                        metadata=metadata,
                    )
                    continue
                if phase in {"MAP", "DIFFERENTIAL"} and state == AnalysisState.COMPLETED:
                    self._complete_guided_map_barrier(
                        store=store, fixture=fixture, metadata=metadata
                    )
                    return
                if state != AnalysisState.RUNNING:
                    return
                plan = ConnectivityRunPlan.model_validate(analysis["plan"])
                job = store.claim_next_direct_job(active_id)
                if job is None:
                    counts = store.direct_job_counts(active_id)
                    if (
                        counts.get(DirectJobState.QUEUED.value, 0) == 0
                        and counts.get(DirectJobState.RUNNING.value, 0) == 0
                    ):
                        if set(counts) != {DirectJobState.SUCCEEDED.value}:
                            raise DurableAnalysisError(
                                "Ridgeway direct barrier contains rejected jobs"
                            )
                        if phase == "MATRIX":
                            self._advance_guided_matrix_barrier(
                                store=store,
                                fixture=fixture,
                                plan=plan,
                                metadata=metadata,
                            )
                            continue
                        self._complete_guided_map_barrier(
                            store=store, fixture=fixture, metadata=metadata
                        )
                    return
                succeeded = self._execute_one(
                    store=store,
                    bundle=fixture.bundle,
                    plan=plan,
                    adapter=adapter,
                    job=job,
                    control=control,
                    fixture_rasters=fixture.rasters,
                )
                if not succeeded:
                    current_state = AnalysisState(store.analysis(active_id)["state"])
                    if (
                        control.cancel_requested.is_set()
                        or current_state == AnalysisState.CANCELLED
                    ):
                        continue
                    if self._settle_pause_intent(
                        store=store,
                        control=control,
                        analysis_id=active_id,
                        guided_metadata=metadata,
                    ):
                        return
                    if control.shutdown_requested.is_set():
                        return
                    if current_state != AnalysisState.FAILED:
                        store.fail_analysis(
                            active_id,
                            {
                                "code": "GUIDED_DIRECT_JOB_FAILED",
                                "direct_job_id": job["direct_job_id"],
                            },
                        )
                    raise DurableAnalysisError("a Ridgeway direct job failed")
        except OrphanProcessAuditError:
            # The authoritative rows remain untouched. The next foreground
            # start/recover call returns the typed audit blocker to the user.
            return
        except Exception as exc:  # noqa: BLE001 - background boundary must persist failure
            try:
                located = self._guided_metadata(store, control.analysis_id)
                if located is None:
                    return
                primary_id, metadata = located
                active_id = self._guided_active_analysis(metadata)
                try:
                    state = AnalysisState(store.analysis(active_id)["state"])
                except KeyError:
                    state = AnalysisState.FAILED
                if self._settle_pause_intent(
                    store=store,
                    control=control,
                    analysis_id=active_id,
                    guided_metadata=metadata,
                ):
                    return
                if control.shutdown_requested.is_set():
                    return
                if state == AnalysisState.PAUSING:
                    store.mark_paused(active_id)
                    self._update_guided_session(store, metadata, "PAUSED")
                    return
                if state == AnalysisState.RUNNING:
                    store.fail_analysis(
                        active_id,
                        {
                            "code": "GUIDED_RERUN_FAILED",
                            "technical_type": type(exc).__name__,
                        },
                    )
                failed = dict(metadata)
                failed["failure"] = {
                    "code": "GUIDED_RERUN_FAILED",
                    "technical_type": type(exc).__name__,
                }
                store.put_record("guided_rerun", primary_id, failed)
                self._update_guided_session(store, failed, "FAILED")
            except (KeyError, OSError, ValueError):
                return
        finally:
            store.close()
            self._release_project_control(control)

    def _run_loop(self, control: _Control, executable: Path) -> None:
        # Retain the verified process resource only in memory for resume.
        control.engine_executable = executable
        store = EcoProjectStore(control.project_directory)
        try:
            analysis = store.analysis(control.analysis_id)
            if AnalysisState(analysis["state"]) == AnalysisState.RUNNING:
                self._consume_shutdown_recovery(store, control.analysis_id)
                analysis = store.analysis(control.analysis_id)
            plan = ConnectivityRunPlan.model_validate(analysis["plan"])
            # Every direct job must use the model snapshot committed with the
            # immutable run plan. Project edits made after planning must never
            # alter a queued, resumed, or recovered scientific execution.
            bundle = store.analysis_bundle(control.analysis_id)
            adapter = CircuitscapeAdapter(executable, self.engine_manifest)
            while True:
                state = AnalysisState(store.analysis(control.analysis_id)["state"])
                if control.cancel_requested.is_set() or state == AnalysisState.CANCELLED:
                    store.cancel_queued_direct_jobs(control.analysis_id)
                    return
                if self._settle_pause_intent(
                    store=store,
                    control=control,
                    analysis_id=control.analysis_id,
                ):
                    return
                if control.shutdown_requested.is_set():
                    return
                if state != AnalysisState.RUNNING:
                    return
                job = store.claim_next_direct_job(control.analysis_id)
                if job is None:
                    state = AnalysisState(store.analysis(control.analysis_id)["state"])
                    if state == AnalysisState.PAUSING or control.pause_requested.is_set():
                        if state == AnalysisState.RUNNING:
                            store.request_pause(control.analysis_id)
                        store.mark_paused(control.analysis_id)
                        return
                    if state == AnalysisState.CANCELLED or control.cancel_requested.is_set():
                        return
                    counts = store.direct_job_counts(control.analysis_id)
                    if (
                        counts.get(DirectJobState.QUEUED.value, 0) == 0
                        and counts.get(DirectJobState.RUNNING.value, 0) == 0
                    ):
                        self._complete_analysis(store, bundle, plan)
                    return
                succeeded = self._execute_one(
                    store=store,
                    bundle=bundle,
                    plan=plan,
                    adapter=adapter,
                    job=job,
                    control=control,
                )
                if not succeeded:
                    current_state = AnalysisState(store.analysis(control.analysis_id)["state"])
                    if (
                        control.cancel_requested.is_set()
                        or current_state == AnalysisState.CANCELLED
                    ):
                        return
                    if self._settle_pause_intent(
                        store=store,
                        control=control,
                        analysis_id=control.analysis_id,
                    ):
                        return
                    if control.shutdown_requested.is_set():
                        return
                    if current_state not in {
                        AnalysisState.CANCELLED,
                        AnalysisState.FAILED,
                    }:
                        store.fail_analysis(
                            control.analysis_id,
                            {"code": "DIRECT_JOB_FAILED", "direct_job_id": job["direct_job_id"]},
                        )
                    return
        except OrphanProcessAuditError:
            # Never turn a missing or mismatched process-exit receipt into a
            # scientific failure or an implicit recovery mutation.
            return
        except Exception as exc:  # noqa: BLE001 - background process boundary must fail closed
            try:
                state = AnalysisState(store.analysis(control.analysis_id)["state"])
                if self._settle_pause_intent(
                    store=store,
                    control=control,
                    analysis_id=control.analysis_id,
                ):
                    return
                if control.shutdown_requested.is_set():
                    return
                if state == AnalysisState.RUNNING:
                    store.fail_analysis(
                        control.analysis_id,
                        {
                            "code": "DURABLE_ANALYSIS_FAILED",
                            "technical_type": type(exc).__name__,
                        },
                    )
            except (KeyError, OSError, ValueError):
                return
        finally:
            store.close()
            self._release_project_control(control)

    def _asset_path(self, store: EcoProjectStore, asset: ConnectivityAsset) -> Path:
        roots = [store.root]
        for record in store.list_records("import_manifest"):
            relative_root = str(record.get("materialized_directory", ""))
            if relative_root:
                roots.append(contained_path(store.root, relative_root))
        matches: list[Path] = []
        for root in roots:
            if not root.exists() or root.is_symlink():
                continue
            try:
                candidate = contained_path(root, asset.relative_path)
            except PathSecurityError:
                continue
            if not candidate.exists():
                continue
            metadata = os.lstat(candidate)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise DurableAnalysisError(f"asset path is not a regular file: {asset.asset_id}")
            if metadata.st_size == asset.byte_size and sha256_file(candidate) == asset.sha256:
                matches.append(candidate)
        if not matches:
            raise DurableAnalysisError(
                f"reviewed asset bytes are unavailable or changed: {asset.asset_id}"
            )
        return min(matches, key=lambda item: item.as_posix())

    def _source_raster(
        self,
        store: EcoProjectStore,
        asset: ConnectivityAsset,
        fixture_rasters: dict[str, InMemoryRaster] | None,
    ) -> InMemoryRaster:
        if fixture_rasters is not None and asset.asset_id in fixture_rasters:
            raster = fixture_rasters[asset.asset_id]
            if raster.grid != asset.grid:
                raise DurableAnalysisError(
                    f"guided raster grid differs from the reviewed asset: {asset.asset_id}"
                )
            return raster
        return load_raster(self._asset_path(store, asset), asset)

    @staticmethod
    def _pair_bytes(scenario: ConnectivityScenario, focal_set: Any) -> bytes:
        pair_by_id = {item.pair_id: item for item in focal_set.pairs}
        try:
            rows = [pair_by_id[pair_id] for pair_id in scenario.included_pair_ids]
        except KeyError as exc:
            raise DurableAnalysisError(
                f"scenario {scenario.scenario_id} references an unavailable focal pair"
            ) from exc
        content = "mode\tinclude\n" + "".join(
            f"{pair.from_focal_id}\t{pair.to_focal_id}\n" for pair in rows
        )
        return content.encode("utf-8")

    def _prepare(
        self,
        *,
        store: EcoProjectStore,
        bundle: ConnectivityProjectBundle,
        plan: ConnectivityRunPlan,
        adapter: CircuitscapeAdapter,
        job: dict[str, Any],
        fixture_rasters: dict[str, InMemoryRaster] | None = None,
    ) -> tuple[PreparedConnectivityRun, dict[str, Any]]:
        scenario = next(item for item in bundle.scenarios if item.scenario_id == job["scenario_id"])
        portfolio = next(
            item for item in plan.portfolios if item.portfolio_id == job["portfolio_id"]
        )
        hypothesis = next(
            item
            for item in bundle.resistance_hypotheses
            if item.hypothesis_id == scenario.resistance_hypothesis_id
        )
        focal_set = next(
            item for item in bundle.focal_area_sets if item.focal_set_id == scenario.focal_set_id
        )
        assets = {item.asset_id: item for item in bundle.assets}
        resistance_asset = assets[hypothesis.resistance_asset_id]
        focal_asset = assets[focal_set.asset_id]
        baseline = self._source_raster(store, resistance_asset, fixture_rasters)
        focal = self._source_raster(store, focal_asset, fixture_rasters)
        assert_same_grid(baseline, focal)
        effects = scenario_effects_for_portfolio(portfolio, bundle.intervention_options, scenario)
        footprint_proofs = prove_action_effect_footprints(
            baseline,
            effects,
            bundle.assets,
            resolve_path=lambda asset: self._asset_path(store, asset),
            normalized_rasters=fixture_rasters,
        )
        footprint_source_paths = {
            asset_id: self._asset_path(store, assets[asset_id])
            for asset_id in sorted({item.footprint_asset_id for item in footprint_proofs})
        }
        resistance = apply_absolute_replacements(
            baseline,
            effects,
            derived_asset_id=f"prepared_{job['direct_job_id']}",
        )
        materialized = store.root / "materialized_models" / plan.analysis_id / job["direct_job_id"]
        materialized.mkdir(parents=True, exist_ok=True)
        resistance_path = materialized / "resistance.asc"
        focal_path = materialized / "focal.asc"
        pairs_path = materialized / "included-pairs.tsv"
        resistance_sha = write_esri_ascii(resistance_path, resistance)
        physics_identity = canonical_sha256(
            {
                "scenario": scenario.model_dump(mode="json"),
                "baseline_asset_sha256": resistance_asset.sha256,
                "effects": [item.model_dump(mode="json") for item in effects],
            }
        )
        semantic_diff = semantic_raster_diff_payload(
            project_id=bundle.project_id,
            revision_id=plan.revision_id,
            model_identity_sha256=physics_identity,
            portfolio_id=portfolio.portfolio_id,
            scenario_id=scenario.scenario_id,
            baseline=baseline,
            effects=effects,
            prepared_resistance_sha256=resistance_sha,
        )
        verify_semantic_raster_diff(
            semantic_diff,
            baseline=baseline,
            effects=effects,
            prepared=resistance,
            expected_project_id=bundle.project_id,
            expected_revision_id=plan.revision_id,
            expected_model_identity_sha256=physics_identity,
            expected_portfolio_id=portfolio.portfolio_id,
            expected_scenario_id=scenario.scenario_id,
            prepared_resistance_sha256=resistance_sha,
        )
        semantic_diff_path = materialized / "action-raster-diff.json"
        atomic_write_bytes(
            semantic_diff_path,
            canonical_semantic_raster_diff_bytes(semantic_diff),
            mode=0o600,
        )
        semantic_diff_sha = sha256_file(semantic_diff_path)
        footprint_document = create_professional_footprint_proof_document(
            project_id=bundle.project_id,
            revision_id=plan.revision_id,
            portfolio_id=portfolio.portfolio_id,
            scenario_id=scenario.scenario_id,
            baseline=baseline,
            proofs=footprint_proofs,
            assets=bundle.assets,
            source_paths=footprint_source_paths,
        )
        verify_professional_footprint_proof_document(
            footprint_document,
            expected_proofs=footprint_proofs,
            source_paths=footprint_source_paths,
        )
        footprint_document_path = materialized / "footprint-proofs.json"
        atomic_write_bytes(
            footprint_document_path,
            (
                json.dumps(footprint_document.model_dump(mode="json"), sort_keys=True, indent=2)
                + "\n"
            ).encode("utf-8"),
            mode=0o600,
        )
        focal_sha = write_esri_ascii(focal_path, focal)
        pair_payload = self._pair_bytes(scenario, focal_set)
        atomic_write_bytes(pairs_path, pair_payload, mode=0o600)
        pairs_sha = sha256_file(pairs_path)
        pairs = [
            PreparedPair(
                pair_id=pair_id,
                from_focal_id=next(
                    item.from_focal_id for item in focal_set.pairs if item.pair_id == pair_id
                ),
                to_focal_id=next(
                    item.to_focal_id for item in focal_set.pairs if item.pair_id == pair_id
                ),
            )
            for pair_id in scenario.included_pair_ids
        ]
        prepared = adapter.prepare(
            prepared_run_id=job["direct_job_id"],
            analysis_id=plan.analysis_id,
            revision_id=plan.revision_id,
            portfolio_id=portfolio.portfolio_id,
            scenario=scenario,
            pairs=pairs,
            resources=[
                PreparedResource(
                    logical_name="resistance",
                    source_path=resistance_path,
                    sha256=resistance_sha,
                    target_filename="resistance.asc",
                ),
                PreparedResource(
                    logical_name="focal",
                    source_path=focal_path,
                    sha256=focal_sha,
                    target_filename="focal.asc",
                ),
                PreparedResource(
                    logical_name="included_pairs",
                    source_path=pairs_path,
                    sha256=pairs_sha,
                    target_filename="included-pairs.tsv",
                ),
            ],
            action_raster_sha256=semantic_diff_sha,
            action_raster_diff_path=semantic_diff_path,
            prepared_resistance_sha256=resistance_sha,
            footprint_proof_sha256=footprint_proof_set_sha256(footprint_proofs),
            emit_current_map=bool(job["emit_current_map"]),
        )
        provenance = {
            "scenario": scenario,
            "portfolio": portfolio,
            "hypothesis": hypothesis,
            "focal_set": focal_set,
            "resistance_asset": resistance_asset,
            "focal_asset": focal_asset,
            "effects": effects,
            "footprint_assets": [assets[effect.footprint_asset_id] for effect in effects],
            "footprint_proofs": footprint_proofs,
            "footprint_proof_document": footprint_document,
            "footprint_proof_document_path": footprint_document_path,
            "footprint_source_paths": footprint_source_paths,
            "semantic_diff": semantic_diff,
            "semantic_diff_path": semantic_diff_path,
        }
        return prepared, provenance

    @staticmethod
    def _safe_execution_artifact(execution: EngineExecution, relative: str) -> Path:
        try:
            return contained_regular_file(execution.work_directory, relative)
        except EngineProtocolError as exc:
            raise DurableAnalysisError(
                "engine artifact escaped its isolated direct-job directory"
            ) from exc

    def _artifact_records(
        self,
        store: EcoProjectStore,
        prepared: PreparedConnectivityRun,
        execution: EngineExecution,
        provenance: dict[str, Any],
        run: DirectConnectivityRun,
    ) -> list[dict[str, Any]]:
        cas = ContentAddressedStore(store.root / "blobs")
        protocol = execution.protocol_result
        sources: list[tuple[str, Path, str, str]] = [
            ("request", execution.request_path, "application/json", run.input_sha256),
            (
                "result_protocol",
                execution.result_path,
                "application/json",
                str(run.result_sha256),
            ),
            (
                "circuitscape_configuration",
                execution.configuration_path,
                "text/plain",
                sha256_file(execution.configuration_path),
            ),
            (
                "action_raster_diff",
                prepared.action_raster_diff_path,
                "application/json",
                prepared.action_raster_sha256,
            ),
            (
                "footprint_proof_document",
                provenance["footprint_proof_document_path"],
                "application/json",
                sha256_file(provenance["footprint_proof_document_path"]),
            ),
            (
                "stdout",
                execution.work_directory / "stdout.log",
                "text/plain",
                execution.stdout_sha256,
            ),
            (
                "stderr",
                execution.work_directory / "stderr.log",
                "text/plain",
                execution.stderr_sha256,
            ),
            (
                "matrix",
                self._safe_execution_artifact(execution, protocol.matrix_path or ""),
                "text/plain",
                str(run.matrix_sha256),
            ),
        ]
        if protocol.current_map_path:
            sources.append(
                (
                    "current_map",
                    self._safe_execution_artifact(execution, protocol.current_map_path),
                    "text/plain",
                    str(run.current_map_sha256),
                )
            )
        for resource in prepared.resources:
            sources.append(
                (
                    f"input_{resource.logical_name}",
                    execution.work_directory / "inputs" / resource.target_filename,
                    "text/plain",
                    resource.sha256,
                )
            )
        for asset_id, source_path in sorted(provenance["footprint_source_paths"].items()):
            sources.append(
                (
                    f"reviewed_footprint_source:{asset_id}",
                    source_path,
                    next(
                        item.media_type
                        for item in provenance["footprint_assets"]
                        if item.asset_id == asset_id
                    ),
                    next(
                        item.sha256
                        for item in provenance["footprint_assets"]
                        if item.asset_id == asset_id
                    ),
                )
            )
        records: list[dict[str, Any]] = []
        for role, path, media_type, expected_sha256 in sources:
            digest = cas.put_file(path, expected_sha256=expected_sha256)
            records.append(
                {
                    "role": role,
                    "sha256": digest,
                    "byte_size": path.stat().st_size,
                    "cas_relative_path": cas.path_for(digest).relative_to(store.root).as_posix(),
                    "media_type": media_type,
                }
            )
        return sorted(records, key=lambda item: item["role"])

    def _terminal_artifact_records(
        self,
        store: EcoProjectStore,
        work_directory: Path | None,
        prepared: PreparedConnectivityRun | None,
    ) -> list[dict[str, Any]]:
        """Retain only allowlisted passive files from a rejected/failed attempt."""

        if work_directory is None or not work_directory.is_dir() or work_directory.is_symlink():
            return []
        root = work_directory.resolve()
        candidates: list[tuple[str, Path, str]] = [
            ("request", root / "request.json", "application/json"),
            ("result_protocol", root / "result.json", "application/json"),
            ("stdout", root / "stdout.log", "text/plain"),
            ("stderr", root / "stderr.log", "text/plain"),
        ]
        if prepared is not None:
            candidates.extend(
                (
                    f"input_{resource.logical_name}",
                    root / "inputs" / resource.target_filename,
                    "text/plain",
                )
                for resource in prepared.resources
            )
        cas = ContentAddressedStore(store.root / "blobs")
        output: list[dict[str, Any]] = []
        for role, path, media_type in candidates:
            try:
                metadata = os.lstat(path)
            except FileNotFoundError:
                continue
            resolved = path.resolve()
            if (
                root not in resolved.parents
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise DurableAnalysisError("terminal attempt artifact escaped its job directory")
            digest = cas.put_file(resolved)
            output.append(
                {
                    "role": role,
                    "sha256": digest,
                    "byte_size": metadata.st_size,
                    "cas_relative_path": cas.path_for(digest).relative_to(store.root).as_posix(),
                    "media_type": media_type,
                }
            )
        return sorted(output, key=lambda item: item["role"])

    @staticmethod
    def _artifact_path(store: EcoProjectStore, artifacts: list[dict[str, Any]], role: str) -> Path:
        record = next((item for item in artifacts if item["role"] == role), None)
        if record is None:
            raise DurableAnalysisError(f"retained direct result omits {role}")
        path = contained_path(store.root, record["cas_relative_path"])
        if sha256_file(path) != record["sha256"]:
            raise DurableAnalysisError(f"retained {role} artifact failed hash verification")
        return path

    def _validate_cached_run_artifacts(
        self,
        *,
        store: EcoProjectStore,
        source: DirectConnectivityRun,
        prepared: PreparedConnectivityRun,
        artifacts: list[dict[str, Any]],
        provenance: dict[str, Any],
    ) -> list[PairResistanceMetric]:
        if (
            source.model_run_cache_key != prepared.model_run_cache_key
            or source.engine_manifest_sha256 != prepared.engine_manifest_sha256
            or source.state.value != "SUCCEEDED"
            or not source.exact
            or source.test_only
            or source.engine_name != "Circuitscape"
            or source.qa_status != QAStatus.ACCEPTED
            or source.qa_findings
        ):
            raise DurableAnalysisError("cached direct-run metadata is not reusable exact evidence")
        roles = {item["role"] for item in artifacts}
        required = {
            "request",
            "result_protocol",
            "circuitscape_configuration",
            "matrix",
            "input_resistance",
            "input_focal",
            "input_included_pairs",
        }
        if prepared.emit_current_map:
            required.add("current_map")
        if required - roles:
            raise DurableAnalysisError("cached direct evidence omits a required physics artifact")
        request_path = self._artifact_path(store, artifacts, "request")
        result_path = self._artifact_path(store, artifacts, "result_protocol")
        matrix_path = self._artifact_path(store, artifacts, "matrix")
        configuration_path = self._artifact_path(store, artifacts, "circuitscape_configuration")
        expected_request = engine_request_payload(prepared, prepared_run_id=source.direct_run_id)
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            protocol = EngineProtocolResult.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise DurableAnalysisError("cached direct protocol artifacts are invalid") from exc
        if request != expected_request or sha256_file(request_path) != source.input_sha256:
            raise DurableAnalysisError("cached request differs from the prepared physics request")
        if (
            protocol.status != "SUCCEEDED"
            or protocol.warnings
            or protocol.review_mask_summaries
            or sha256_file(result_path) != source.result_sha256
            or sha256_file(matrix_path) != source.matrix_sha256
        ):
            raise DurableAnalysisError("cached result protocol differs from exact run metadata")
        reparsed = parse_circuitscape_matrix(matrix_path, prepared.pairs)
        if [
            item.model_dump(mode="json") for item in sorted(reparsed, key=lambda item: item.pair_id)
        ] != [
            item.model_dump(mode="json")
            for item in sorted(protocol.pair_metrics, key=lambda item: item.pair_id)
        ]:
            raise DurableAnalysisError("cached raw matrix differs from its result protocol")
        source_metrics = {item.pair_id: item.effective_resistance for item in source.pair_metrics}
        if source_metrics != {item.pair_id: item.effective_resistance for item in reparsed}:
            raise DurableAnalysisError("cached derived metrics differ from the raw matrix")
        validate_effective_circuitscape_configuration(
            configuration_path,
            resource_paths=dict(expected_request["resources"]),
            output_directory="outputs",
            neighbor_rule=prepared.scenario.neighbor_rule.value,
            resistance_average=prepared.scenario.resistance_average.value,
            emit_current_map=prepared.emit_current_map,
        )
        for resource in prepared.resources:
            artifact = self._artifact_path(store, artifacts, f"input_{resource.logical_name}")
            if sha256_file(artifact) != resource.sha256:
                raise DurableAnalysisError(
                    f"cached input differs from prepared resource: {resource.logical_name}"
                )
        if prepared.emit_current_map:
            current = self._artifact_path(store, artifacts, "current_map")
            if (
                protocol.current_map_path is None
                or sha256_file(current) != source.current_map_sha256
            ):
                raise DurableAnalysisError("cached current map differs from exact run metadata")
            try:
                values, nodata = current_map_values_and_nodata(
                    current, expected_grid=provenance["resistance_asset"].grid
                )
            except RasterIOError as exc:
                raise DurableAnalysisError("cached current map is malformed") from exc
            valid = [
                value
                for row in values
                for value in row
                if not (
                    (math.isnan(nodata) and math.isnan(value))
                    or math.isclose(value, nodata, rel_tol=0.0, abs_tol=1e-9)
                )
            ]
            if not valid or any(not math.isfinite(value) or value < 0 for value in valid):
                raise DurableAnalysisError("cached current map has invalid current values")
        elif protocol.current_map_path is not None or source.current_map_sha256 is not None:
            raise DurableAnalysisError("cached physics includes an unrequested current map")
        return [
            PairResistanceMetric(
                pair_id=item.pair_id,
                effective_resistance=item.effective_resistance,
            )
            for item in sorted(reparsed, key=lambda item: item.pair_id)
        ]

    def _map_diagnostics(
        self,
        *,
        store: EcoProjectStore,
        bundle: ConnectivityProjectBundle,
        plan: ConnectivityRunPlan,
        scenario: ConnectivityScenario,
        portfolio_id: str,
        current_path: Path,
        fixture_rasters: dict[str, InMemoryRaster] | None = None,
    ) -> list[DiagnosticMapSummary]:
        applicable_masks = [
            item for item in bundle.review_masks if item.grid_id == scenario.grid_id
        ]
        required_rules = [
            item
            for item in bundle.decision_rules
            if item.required
            and item.metric == RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT
            and (not item.scenario_ids or scenario.scenario_id in item.scenario_ids)
        ]
        if required_rules and not applicable_masks:
            raise CurrentMapQAError(
                "CURRENT_MAP_REQUIRED_MASK_MISSING",
                f"scenario {scenario.scenario_id} has a required current-map rule but no "
                "review mask on its grid",
            )
        if not applicable_masks:
            return []
        if portfolio_id == plan.baseline_portfolio_id:
            baseline_path = current_path
        else:
            baseline_id = self._direct_job_id(
                plan.analysis_id, plan.baseline_portfolio_id, scenario.scenario_id
            )
            baseline_path = self._artifact_path(
                store, store.run_artifacts(baseline_id), "current_map"
            )
        assets = {item.asset_id: item for item in bundle.assets}
        output: list[DiagnosticMapSummary] = []
        for review in sorted(applicable_masks, key=lambda item: item.mask_id):
            mask_asset = assets[review.asset_id]
            mask = self._source_raster(store, mask_asset, fixture_rasters)
            try:
                comparison = compare_current_maps_in_mask(baseline_path, current_path, mask)
            except RasterIOError as exc:
                raise CurrentMapQAError(
                    "CURRENT_MAP_TOPOLOGY_OR_VALUE_INVALID",
                    f"current rasters cannot be compared in {review.mask_id}: {exc}",
                ) from exc
            if comparison.baseline_total <= 0:
                raise CurrentMapQAError(
                    "CURRENT_MAP_BASELINE_EMPTY",
                    f"baseline current is zero inside review mask {review.mask_id}",
                )
            relative = (
                (comparison.portfolio_total - comparison.baseline_total)
                / comparison.baseline_total
                * 100.0
            )
            output.append(
                DiagnosticMapSummary(
                    mask_id=review.mask_id,
                    baseline_total_current=comparison.baseline_total,
                    portfolio_total_current=comparison.portfolio_total,
                    relative_change_percent=relative,
                    review_triggered=relative > review.review_trigger_percent,
                    reviewed_cell_count=comparison.reviewed_cell_count,
                    included_cell_count=comparison.included_cell_count,
                    excluded_nodata_cell_count=comparison.excluded_nodata_cell_count,
                    raster_nodata_cell_count=comparison.raster_nodata_cell_count,
                    nodata_topology_sha256=comparison.nodata_topology_sha256,
                )
            )
        return output

    def _evidence_nodes(
        self,
        *,
        run: DirectConnectivityRun,
        prepared: PreparedConnectivityRun,
        provenance: dict[str, Any],
        artifacts: list[dict[str, Any]],
        cache_source_run: DirectConnectivityRun | None = None,
    ) -> list[dict[str, Any]]:
        asset_ids = [
            provenance["resistance_asset"].asset_id,
            provenance["focal_asset"].asset_id,
            *(effect.footprint_asset_id for effect in provenance["effects"]),
        ]
        assets_by_id = {
            provenance["resistance_asset"].asset_id: provenance["resistance_asset"],
            provenance["focal_asset"].asset_id: provenance["focal_asset"],
        }
        assets_by_id.update({asset.asset_id: asset for asset in provenance["footprint_assets"]})
        proof_artifact = next(
            item for item in artifacts if item["role"] == "footprint_proof_document"
        )
        nodes = [
            {
                "node_id": f"run:{run.direct_run_id}",
                "node_type": "DIRECT_RUN",
                "sha256": run.result_sha256,
            },
            {
                "node_id": f"scenario:{run.scenario_id}",
                "node_type": "SCENARIO",
            },
            {
                "node_id": f"diff:{prepared.action_raster_sha256}",
                "node_type": "RASTER_DIFF",
                "sha256": prepared.action_raster_sha256,
                "footprint_proof_sha256": prepared.footprint_proof_sha256,
            },
            {
                "node_id": f"engine:{run.engine_manifest_sha256}",
                "node_type": "ENGINE_MANIFEST",
                "sha256": run.engine_manifest_sha256,
            },
            {
                "node_id": f"footprint-proof:{proof_artifact['sha256']}",
                "node_type": "FOOTPRINT_PROOF",
                "sha256": proof_artifact["sha256"],
                "footprint_proof_set_sha256": prepared.footprint_proof_sha256,
                "project_id": provenance["footprint_proof_document"].project_id,
                "revision_id": provenance["footprint_proof_document"].revision_id,
                "portfolio_id": provenance["footprint_proof_document"].portfolio_id,
                "scenario_id": provenance["footprint_proof_document"].scenario_id,
            },
            {
                "node_id": f"qa:{run.direct_run_id}",
                "node_type": "QA",
                "status": run.qa_status.value if run.qa_status else None,
            },
        ]
        nodes.extend(
            {
                "node_id": f"asset:{asset_id}",
                "node_type": "ASSET",
                "sha256": assets_by_id[asset_id].sha256,
            }
            for asset_id in sorted(set(asset_ids))
        )
        nodes.extend(
            {
                "node_id": f"artifact:{item['role']}:{item['sha256']}",
                "node_type": "ARTIFACT",
                "role": item["role"],
                "sha256": item["sha256"],
            }
            for item in artifacts
        )
        if cache_source_run is not None:
            if cache_source_run.direct_run_id == run.direct_run_id:
                raise DurableAnalysisError("cache source cannot be the requested direct-job alias")
            nodes.extend(
                [
                    {
                        "node_id": f"run:{cache_source_run.direct_run_id}",
                        "node_type": "DIRECT_RUN",
                        "sha256": cache_source_run.result_sha256,
                        "cache_source": True,
                    },
                    {
                        "node_id": f"qa:{cache_source_run.direct_run_id}",
                        "node_type": "QA",
                        "status": (
                            cache_source_run.qa_status.value if cache_source_run.qa_status else None
                        ),
                        "cache_source": True,
                    },
                    {
                        "node_id": (
                            f"cache-lineage:{run.direct_run_id}:{cache_source_run.direct_run_id}"
                        ),
                        "node_type": "CACHE_LINEAGE",
                        "requested_direct_run_id": run.direct_run_id,
                        "source_direct_run_id": cache_source_run.direct_run_id,
                    },
                ]
            )
        return nodes

    def _clone_cached_run(
        self,
        source: DirectConnectivityRun,
        prepared: PreparedConnectivityRun,
        direct_job_id: str,
        *,
        input_sha256: str,
        cache_lookup_wall_seconds: float,
        rederived_pair_metrics: list[PairResistanceMetric],
    ) -> DirectConnectivityRun:
        payload = source.model_dump(mode="python")
        payload.update(
            {
                "direct_run_id": direct_job_id,
                "analysis_id": prepared.analysis_id,
                "revision_id": prepared.revision_id,
                "portfolio_id": prepared.portfolio_id,
                "scenario_id": prepared.scenario.scenario_id,
                "model_run_cache_key": prepared.model_run_cache_key,
                "input_sha256": input_sha256,
                "cache_source_direct_run_id": source.direct_run_id,
                "pair_metrics": rederived_pair_metrics,
                "diagnostic_maps": [],
                "resource_observation": unmeasured_cache_observation(
                    observation_id=f"resource:{direct_job_id}",
                    wall_seconds=cache_lookup_wall_seconds,
                ),
                "completed_at": datetime.now(UTC),
            }
        )
        return DirectConnectivityRun.model_validate(payload)

    def _finalize_cancelled_active_job(
        self,
        *,
        store: EcoProjectStore,
        plan: ConnectivityRunPlan,
        job: dict[str, Any],
        work_directory: Path | None,
        prepared: PreparedConnectivityRun | None,
        resource_observation: Any,
        code: str,
    ) -> None:
        current = next(
            (
                item
                for item in store.direct_jobs(plan.analysis_id)
                if item["direct_job_id"] == job["direct_job_id"]
            ),
            None,
        )
        if current and current["state"] == DirectJobState.RUNNING.value:
            store.finalize_direct_job(
                job["direct_job_id"],
                DirectJobState.CANCELLED,
                artifacts=self._terminal_artifact_records(store, work_directory, prepared),
                resource_observation=resource_observation,
                error={
                    "code": code,
                    "category": "RESOURCE_LIMIT",
                    "retryable": False,
                },
            )

    def _execute_one(
        self,
        *,
        store: EcoProjectStore,
        bundle: ConnectivityProjectBundle,
        plan: ConnectivityRunPlan,
        adapter: CircuitscapeAdapter,
        job: dict[str, Any],
        control: _Control,
        fixture_rasters: dict[str, InMemoryRaster] | None = None,
    ) -> bool:
        prepared: PreparedConnectivityRun | None = None
        work_directory: Path | None = None
        artifacts: list[dict[str, Any]] = []
        resource_observation = None
        solver_invocation_started = False
        control.process_exit_evidence.pop(str(job["direct_job_id"]), None)
        try:
            prepared, provenance = self._prepare(
                store=store,
                bundle=bundle,
                plan=plan,
                adapter=adapter,
                job=job,
                fixture_rasters=fixture_rasters,
            )
            store.set_direct_job_prepared(
                job["direct_job_id"],
                prepared.model_dump(mode="json"),
                prepared.model_run_cache_key,
            )
            if control.cancel_requested.is_set():
                self._finalize_cancelled_active_job(
                    store=store,
                    plan=plan,
                    job=job,
                    work_directory=work_directory,
                    prepared=prepared,
                    resource_observation=resource_observation,
                    code="ANALYSIS_CANCELLED_BEFORE_ACTIVE_JOB",
                )
                return False
            if control.shutdown_requested.is_set():
                if not store.requeue_unlaunched_direct_job(
                    job["direct_job_id"], attempt_count=int(job["attempt_count"])
                ):
                    raise OrphanProcessAuditError(
                        "ORPHAN_RECEIPT_STATE_MISMATCH",
                        "an unlaunched direct job changed before shutdown requeue",
                    )
                return False
            cache_lookup_started = time.perf_counter()
            cached = store.cached_direct_run(prepared.model_run_cache_key)
            cache_lookup_wall_seconds = time.perf_counter() - cache_lookup_started
            if cached is not None:
                source, artifacts = cached
                rederived_pair_metrics = self._validate_cached_run_artifacts(
                    store=store,
                    source=source,
                    prepared=prepared,
                    artifacts=artifacts,
                    provenance=provenance,
                )
                # Semantic diff is analysis/revision evidence rather than a
                # physics-cache input. Replace the source analysis' diff with
                # the current, independently reconstructed record and retain
                # explicit cache-source lineage on the cloned direct run.
                artifacts = [
                    item
                    for item in artifacts
                    if item["role"]
                    not in {"request", "action_raster_diff", "footprint_proof_document"}
                    and not str(item["role"]).startswith("reviewed_footprint_source:")
                ]
                cas = ContentAddressedStore(store.root / "blobs")
                request_path = prepared.action_raster_diff_path.parent / "cache-request.json"
                atomic_write_bytes(
                    request_path,
                    (
                        json.dumps(
                            engine_request_payload(prepared),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8"),
                    mode=0o600,
                )
                request_digest = cas.put_file(request_path)
                artifacts.append(
                    {
                        "role": "request",
                        "sha256": request_digest,
                        "byte_size": request_path.stat().st_size,
                        "cas_relative_path": cas.path_for(request_digest)
                        .relative_to(store.root)
                        .as_posix(),
                        "media_type": "application/json",
                    }
                )
                diff_digest = cas.put_file(
                    prepared.action_raster_diff_path,
                    expected_sha256=prepared.action_raster_sha256,
                )
                if diff_digest != prepared.action_raster_sha256:
                    raise DurableAnalysisError("cached semantic raster diff hash changed")
                artifacts.append(
                    {
                        "role": "action_raster_diff",
                        "sha256": diff_digest,
                        "byte_size": prepared.action_raster_diff_path.stat().st_size,
                        "cas_relative_path": cas.path_for(diff_digest)
                        .relative_to(store.root)
                        .as_posix(),
                        "media_type": "application/json",
                    }
                )
                proof_path = provenance["footprint_proof_document_path"]
                proof_digest = sha256_file(proof_path)
                cas.put_file(proof_path, expected_sha256=proof_digest)
                artifacts.append(
                    {
                        "role": "footprint_proof_document",
                        "sha256": proof_digest,
                        "byte_size": proof_path.stat().st_size,
                        "cas_relative_path": cas.path_for(proof_digest)
                        .relative_to(store.root)
                        .as_posix(),
                        "media_type": "application/json",
                    }
                )
                assets_by_id = {item.asset_id: item for item in provenance["footprint_assets"]}
                for asset_id, source_path in sorted(provenance["footprint_source_paths"].items()):
                    asset = assets_by_id[asset_id]
                    source_digest = cas.put_file(source_path, expected_sha256=asset.sha256)
                    artifacts.append(
                        {
                            "role": f"reviewed_footprint_source:{asset_id}",
                            "sha256": source_digest,
                            "byte_size": source_path.stat().st_size,
                            "cas_relative_path": cas.path_for(source_digest)
                            .relative_to(store.root)
                            .as_posix(),
                            "media_type": asset.media_type,
                        }
                    )
                artifacts.sort(key=lambda item: item["role"])
                for artifact in artifacts:
                    self._artifact_path(store, artifacts, artifact["role"])
                run = self._clone_cached_run(
                    source,
                    prepared,
                    job["direct_job_id"],
                    input_sha256=request_digest,
                    cache_lookup_wall_seconds=cache_lookup_wall_seconds,
                    rederived_pair_metrics=rederived_pair_metrics,
                )
                resource_observation = run.resource_observation
                if prepared.emit_current_map:
                    current = self._artifact_path(store, artifacts, "current_map")
                    run = run.model_copy(
                        update={
                            "diagnostic_maps": self._map_diagnostics(
                                store=store,
                                bundle=bundle,
                                plan=plan,
                                scenario=prepared.scenario,
                                portfolio_id=prepared.portfolio_id,
                                current_path=current,
                                fixture_rasters=fixture_rasters,
                            )
                        }
                    )
                nodes = self._evidence_nodes(
                    run=run,
                    prepared=prepared,
                    provenance=provenance,
                    artifacts=artifacts,
                    cache_source_run=source,
                )
                if control.cancel_requested.is_set():
                    self._finalize_cancelled_active_job(
                        store=store,
                        plan=plan,
                        job=job,
                        work_directory=work_directory,
                        prepared=prepared,
                        resource_observation=resource_observation,
                        code="ANALYSIS_CANCELLED_BEFORE_CACHED_RESULT",
                    )
                    return False
                store.finalize_direct_job(
                    job["direct_job_id"],
                    DirectJobState.SUCCEEDED,
                    run=run,
                    artifacts=artifacts,
                    evidence_nodes=nodes,
                    cached=True,
                )
                return True
            attempt = int(job["attempt_count"])
            work_directory = store.root / "runs" / job["direct_job_id"] / f"attempt-{attempt:04d}"
            solver_invocation_started = True
            execution = adapter.execute(
                prepared,
                work_directory,
                timeout_seconds=bundle.resource_policy.max_wall_seconds_per_job,
                cancel_requested=lambda: (
                    control.cancel_requested.is_set() or control.shutdown_requested.is_set()
                ),
            )
            resource_observation = execution.resource_observation
            process_exit_evidence = self._require_confirmed_process_exit(
                control, job, execution.process_exit_evidence
            )
            if control.cancel_requested.is_set():
                self._finalize_cancelled_active_job(
                    store=store,
                    plan=plan,
                    job=job,
                    work_directory=work_directory,
                    prepared=prepared,
                    resource_observation=resource_observation,
                    code="ANALYSIS_CANCELLED_AFTER_ACTIVE_JOB",
                )
                return False
            if control.shutdown_requested.is_set():
                return False
            run = adapter.extract(
                prepared,
                execution,
                direct_run_id=job["direct_job_id"],
            )
            run = run.model_copy(update={"resource_observation": execution.resource_observation})
            artifacts = self._artifact_records(store, prepared, execution, provenance, run)
            if prepared.emit_current_map:
                current = self._artifact_path(store, artifacts, "current_map")
                run = run.model_copy(
                    update={
                        "diagnostic_maps": self._map_diagnostics(
                            store=store,
                            bundle=bundle,
                            plan=plan,
                            scenario=prepared.scenario,
                            portfolio_id=prepared.portfolio_id,
                            current_path=current,
                            fixture_rasters=fixture_rasters,
                        )
                    }
                )
            if control.cancel_requested.is_set():
                self._finalize_cancelled_active_job(
                    store=store,
                    plan=plan,
                    job=job,
                    work_directory=work_directory,
                    prepared=prepared,
                    resource_observation=resource_observation,
                    code="ANALYSIS_CANCELLED_AFTER_ACTIVE_JOB",
                )
                return False
            if control.shutdown_requested.is_set():
                return False
            qa_findings = validate_direct_run_qa(run, prepared.scenario)
            if qa_findings:
                store.finalize_direct_job(
                    job["direct_job_id"],
                    DirectJobState.QA_REJECTED,
                    artifacts=artifacts,
                    resource_observation=resource_observation,
                    error={
                        "code": "DIRECT_QA_REJECTED",
                        "category": "MODEL_QUALITY",
                        "retryable": False,
                        "findings": qa_findings,
                    },
                )
                return False
            nodes = self._evidence_nodes(
                run=run,
                prepared=prepared,
                provenance=provenance,
                artifacts=artifacts,
            )
            # The successful direct run and its process-tree boundary are
            # committed atomically.  This is passive provenance only; it never
            # substitutes for the retained Circuitscape result artifacts.
            qa_node = next(item for item in nodes if item.get("node_type") == "QA")
            qa_node["process_tree_exit"] = process_exit_evidence
            store.finalize_direct_job(
                job["direct_job_id"],
                DirectJobState.SUCCEEDED,
                run=run,
                artifacts=artifacts,
                evidence_nodes=nodes,
            )
            return True
        except ObservedProcessCancelled as exc:
            resource_observation = exc.resource_observation
            self._require_confirmed_process_exit(control, job, exc.process_exit.as_dict())
            if control.cancel_requested.is_set():
                self._finalize_cancelled_active_job(
                    store=store,
                    plan=plan,
                    job=job,
                    work_directory=work_directory,
                    prepared=prepared,
                    resource_observation=resource_observation,
                    code="ANALYSIS_CANCELLED_DURING_ACTIVE_JOB",
                )
            elif control.shutdown_requested.is_set():
                return False
            return False
        except OrphanProcessAuditError:
            # Keep the claimed attempt RUNNING and stop the queue.  Reclassifying
            # it as failed, cancelled, or succeeded would assert a process-tree
            # boundary that this worker cannot prove.
            raise
        except CurrentMapQAError as exc:
            if control.cancel_requested.is_set():
                self._finalize_cancelled_active_job(
                    store=store,
                    plan=plan,
                    job=job,
                    work_directory=work_directory,
                    prepared=prepared,
                    resource_observation=resource_observation,
                    code="ANALYSIS_CANCELLED_DURING_ACTIVE_JOB",
                )
                return False
            if control.shutdown_requested.is_set():
                return False
            current = next(
                (
                    item
                    for item in store.direct_jobs(plan.analysis_id)
                    if item["direct_job_id"] == job["direct_job_id"]
                ),
                None,
            )
            if current and current["state"] == DirectJobState.RUNNING.value:
                store.finalize_direct_job(
                    job["direct_job_id"],
                    DirectJobState.QA_REJECTED,
                    artifacts=artifacts
                    or self._terminal_artifact_records(store, work_directory, prepared),
                    resource_observation=resource_observation,
                    error={
                        "code": exc.code,
                        "category": "MODEL_QUALITY",
                        "retryable": False,
                    },
                )
            return False
        except Exception as exc:  # noqa: BLE001 - one direct-job boundary records typed failure
            process_exit = getattr(exc, "process_exit_evidence", None)
            if process_exit is None:
                observed_exit = getattr(exc, "process_exit", None)
                if observed_exit is not None:
                    process_exit = observed_exit.as_dict()
            if solver_invocation_started:
                self._require_confirmed_process_exit(control, job, process_exit)
            else:
                self._remember_process_exit(control, job, process_exit)
            if control.cancel_requested.is_set():
                self._finalize_cancelled_active_job(
                    store=store,
                    plan=plan,
                    job=job,
                    work_directory=work_directory,
                    prepared=prepared,
                    resource_observation=resource_observation,
                    code="ANALYSIS_CANCELLED_DURING_ACTIVE_JOB",
                )
                return False
            if control.shutdown_requested.is_set():
                if not solver_invocation_started:
                    store.requeue_unlaunched_direct_job(
                        job["direct_job_id"], attempt_count=int(job["attempt_count"])
                    )
                return False
            if resource_observation is None:
                resource_observation = getattr(exc, "resource_observation", None)
            current = next(
                (
                    item
                    for item in store.direct_jobs(plan.analysis_id)
                    if item["direct_job_id"] == job["direct_job_id"]
                ),
                None,
            )
            if current and current["state"] == DirectJobState.RUNNING.value:
                store.finalize_direct_job(
                    job["direct_job_id"],
                    DirectJobState.FAILED,
                    artifacts=artifacts
                    or self._terminal_artifact_records(store, work_directory, prepared),
                    resource_observation=resource_observation,
                    error={
                        "code": "DIRECT_EXECUTION_FAILED",
                        "category": "ENGINE_FAILURE",
                        "retryable": False,
                        "technical_type": type(exc).__name__,
                    },
                )
            return False

    @staticmethod
    def _safeguard_tolerances(bundle: ConnectivityProjectBundle) -> dict[str, float]:
        return {
            scenario_id: safeguard.nonregression_threshold_percent
            for safeguard in bundle.safeguards
            for scenario_id in safeguard.scenario_ids
        }

    def _claim_sources(
        self,
        bundle: ConnectivityProjectBundle,
        scenario: ConnectivityScenario,
        portfolio: Any,
    ) -> tuple[list[str], list[str]]:
        hypothesis = next(
            item
            for item in bundle.resistance_hypotheses
            if item.hypothesis_id == scenario.resistance_hypothesis_id
        )
        focal = next(
            item for item in bundle.focal_area_sets if item.focal_set_id == scenario.focal_set_id
        )
        effects = scenario_effects_for_portfolio(portfolio, bundle.intervention_options, scenario)
        assets = sorted(
            {
                hypothesis.resistance_asset_id,
                focal.asset_id,
                *(item.footprint_asset_id for item in effects),
            }
        )
        assumptions = [hypothesis.hypothesis_id, *(item.basis.basis_id for item in effects)]
        if not assumptions:
            assumptions = [scenario.resistance_hypothesis_id]
        return assets, assumptions

    @staticmethod
    def _with_cache_lineage(
        claim: ConnectivityEvidenceClaim,
        *runs: DirectConnectivityRun,
    ) -> ConnectivityEvidenceClaim:
        nodes = list(claim.nodes)
        edges = list(claim.edges)
        node_ids = {item.node_id for item in nodes}
        edge_ids = {(item.from_node_id, item.to_node_id, item.relation) for item in edges}
        for run in runs:
            source_id = run.cache_source_direct_run_id
            if source_id is None:
                continue
            source_node_id = f"run:{source_id}"
            source_qa_id = f"qa:{source_id}"
            lineage_node_id = f"cache-lineage:{run.direct_run_id}:{source_id}"
            additions = [
                EvidenceNode(
                    node_id=source_node_id,
                    node_type="DIRECT_RUN",
                    label=f"Cache source direct execution {source_id}",
                    sha256=run.result_sha256,
                ),
                EvidenceNode(
                    node_id=source_qa_id,
                    node_type="QA",
                    label=run.qa_status.value if run.qa_status else "MISSING",
                ),
                EvidenceNode(
                    node_id=lineage_node_id,
                    node_type="CACHE_LINEAGE",
                    label="Requested run reused this immutable direct result.",
                    attributes={
                        "requested_direct_run_id": run.direct_run_id,
                        "source_direct_run_id": source_id,
                    },
                ),
            ]
            for node in additions:
                if node.node_id not in node_ids:
                    nodes.append(node)
                    node_ids.add(node.node_id)
            edge_additions = [
                EvidenceEdge(
                    from_node_id=f"run:{run.direct_run_id}",
                    to_node_id=lineage_node_id,
                    relation="resolved_through_cache",
                ),
                EvidenceEdge(
                    from_node_id=lineage_node_id,
                    to_node_id=source_node_id,
                    relation="reuses_direct_execution",
                ),
                EvidenceEdge(
                    from_node_id=source_node_id,
                    to_node_id=source_qa_id,
                    relation="checked_by",
                ),
            ]
            for edge in edge_additions:
                identity = (edge.from_node_id, edge.to_node_id, edge.relation)
                if identity not in edge_ids:
                    edges.append(edge)
                    edge_ids.add(identity)
        enriched = claim.model_copy(update={"nodes": nodes, "edges": edges})
        validate_evidence_claim(enriched)
        return enriched

    @staticmethod
    def _claim_rule_ids(
        *,
        rule_id: str | None,
        rule_ids: list[str] | None,
        fallback_rule_id: str,
    ) -> tuple[list[str], list[str], str | None]:
        """Return real rule IDs, graph rule IDs, and the singular public rule ID.

        A primitive engineering fact exists independently of how many decision
        rules consume it.  The graph therefore carries every consuming rule,
        while the legacy singular ``rule_id`` field is populated only when the
        relationship is genuinely one-to-one.  A deterministic evidence-meaning
        rule keeps zero-consumer primitive graphs explicit and reachable.
        """

        real_rule_ids = list(rule_ids or [])
        if rule_id is not None:
            real_rule_ids.append(rule_id)
        if any(not item for item in real_rule_ids):
            raise DurableAnalysisError("evidence rule IDs cannot be empty")
        real_rule_ids = sorted(set(real_rule_ids))
        graph_rule_ids = real_rule_ids or [fallback_rule_id]
        singular_rule_id = real_rule_ids[0] if len(real_rule_ids) == 1 else None
        return real_rule_ids, graph_rule_ids, singular_rule_id

    def _diagnostic_claim(
        self,
        *,
        claim_id: str,
        run: DirectConnectivityRun,
        baseline_run: DirectConnectivityRun,
        diagnostic: DiagnosticMapSummary,
        source_asset_ids: list[str],
        assumption_ids: list[str],
        raster_diff_sha256: str,
        run_artifacts: list[dict[str, Any]],
        baseline_artifacts: list[dict[str, Any]],
        rule_id: str | None = None,
        rule_ids: list[str] | None = None,
    ) -> ConnectivityEvidenceClaim:
        if (
            baseline_run.scenario_id != run.scenario_id
            or baseline_run.engine_manifest_sha256 != run.engine_manifest_sha256
        ):
            raise DurableAnalysisError(
                "diagnostic claim baseline differs from its scenario or engine"
            )
        if diagnostic.relative_change_percent is None:
            raise DurableAnalysisError("diagnostic claim has no baseline-relative value")
        _, graph_rule_ids, singular_rule_id = self._claim_rule_ids(
            rule_id=rule_id,
            rule_ids=rule_ids,
            fallback_rule_id="evidence:review-mask-current-change",
        )
        mask_id = diagnostic.mask_id
        value = float(diagnostic.relative_change_percent)
        run_current = next((item for item in run_artifacts if item["role"] == "current_map"), None)
        baseline_current = next(
            (item for item in baseline_artifacts if item["role"] == "current_map"), None
        )
        if run_current is None or baseline_current is None:
            raise DurableAnalysisError("diagnostic claim omits retained current-map artifacts")
        text = (
            f"{PERMITTED_CLAIM_PREFIX} Review-mask current changed by {value:.6g}% relative "
            "to the within-scenario baseline; this is diagnostic only and not an animal-use "
            "estimate."
        )
        metric_id = f"metric:{run.direct_run_id}:{mask_id}"
        run_id = f"run:{run.direct_run_id}"
        baseline_run_id = f"run:{baseline_run.direct_run_id}"
        nodes = [
            EvidenceNode(node_id=claim_id, node_type="CLAIM", label=text),
            *[
                EvidenceNode(node_id=f"rule:{item}", node_type="RULE", label=item)
                for item in graph_rule_ids
            ],
            EvidenceNode(
                node_id=metric_id,
                node_type="METRIC",
                label="Within-scenario review-mask current redistribution",
                attributes={
                    "value": value,
                    "units": "percent",
                    "mask_id": mask_id,
                    "baseline_total_current": diagnostic.baseline_total_current,
                    "portfolio_total_current": diagnostic.portfolio_total_current,
                    "reviewed_cell_count": diagnostic.reviewed_cell_count,
                    "included_cell_count": diagnostic.included_cell_count,
                    "excluded_nodata_cell_count": diagnostic.excluded_nodata_cell_count,
                    "raster_nodata_cell_count": diagnostic.raster_nodata_cell_count,
                    "nodata_topology_sha256": diagnostic.nodata_topology_sha256,
                    "review_triggered": diagnostic.review_triggered,
                },
            ),
            EvidenceNode(
                node_id=f"current-map:{run.direct_run_id}",
                node_type="CURRENT_MAP",
                label="Portfolio cumulative current raster",
                sha256=run_current["sha256"],
            ),
            EvidenceNode(
                node_id=f"artifact:current-map:{run.direct_run_id}:{run_current['sha256']}",
                node_type="ARTIFACT",
                label="Retained portfolio current-map artifact",
                sha256=run_current["sha256"],
            ),
            EvidenceNode(
                node_id=run_id,
                node_type="DIRECT_RUN",
                label=run.direct_run_id,
                sha256=run.result_sha256,
            ),
            EvidenceNode(
                node_id=f"scenario:{run.scenario_id}",
                node_type="SCENARIO",
                label=run.scenario_id,
            ),
            EvidenceNode(
                node_id=f"diff:{raster_diff_sha256}",
                node_type="RASTER_DIFF",
                label="Prepared scenario-specific resistance raster",
                sha256=raster_diff_sha256,
            ),
            EvidenceNode(
                node_id=f"engine:{run.engine_manifest_sha256}",
                node_type="ENGINE_MANIFEST",
                label="Pinned Circuitscape engine manifest",
                sha256=run.engine_manifest_sha256,
            ),
            EvidenceNode(
                node_id=f"qa:{run.direct_run_id}",
                node_type="QA",
                label=run.qa_status.value if run.qa_status else "MISSING",
            ),
            *[
                EvidenceNode(node_id=f"asset:{item}", node_type="ASSET", label=item)
                for item in source_asset_ids
            ],
            *[
                EvidenceNode(node_id=f"assumption:{item}", node_type="ASSUMPTION", label=item)
                for item in assumption_ids
            ],
            *[
                EvidenceNode(node_id=f"authorization:{item}", node_type="AUTHORIZATION", label=item)
                for item in source_asset_ids
            ],
        ]
        if baseline_run.direct_run_id != run.direct_run_id:
            nodes.extend(
                [
                    EvidenceNode(
                        node_id=f"current-map:{baseline_run.direct_run_id}",
                        node_type="CURRENT_MAP",
                        label="Within-scenario baseline cumulative current raster",
                        sha256=baseline_current["sha256"],
                    ),
                    EvidenceNode(
                        node_id=(
                            f"artifact:current-map:{baseline_run.direct_run_id}:"
                            f"{baseline_current['sha256']}"
                        ),
                        node_type="ARTIFACT",
                        label="Retained baseline current-map artifact",
                        sha256=baseline_current["sha256"],
                    ),
                    EvidenceNode(
                        node_id=baseline_run_id,
                        node_type="DIRECT_RUN",
                        label=baseline_run.direct_run_id,
                        sha256=baseline_run.result_sha256,
                    ),
                    EvidenceNode(
                        node_id=f"qa:{baseline_run.direct_run_id}",
                        node_type="QA",
                        label=(
                            baseline_run.qa_status.value if baseline_run.qa_status else "MISSING"
                        ),
                    ),
                ]
            )
        edges = [
            *[
                EvidenceEdge(
                    from_node_id=claim_id,
                    to_node_id=f"rule:{item}",
                    relation=(
                        "evaluates"
                        if not item.startswith("evidence:")
                        else "defines_evidence_meaning"
                    ),
                )
                for item in graph_rule_ids
            ],
            EvidenceEdge(from_node_id=claim_id, to_node_id=metric_id, relation="reports"),
            EvidenceEdge(from_node_id=metric_id, to_node_id=run_id, relation="derived_from"),
            EvidenceEdge(
                from_node_id=metric_id,
                to_node_id=f"current-map:{run.direct_run_id}",
                relation="reduced_from",
            ),
            EvidenceEdge(
                from_node_id=f"current-map:{run.direct_run_id}",
                to_node_id=(f"artifact:current-map:{run.direct_run_id}:{run_current['sha256']}"),
                relation="retained_as",
            ),
            EvidenceEdge(
                from_node_id=run_id,
                to_node_id=f"scenario:{run.scenario_id}",
                relation="executes",
            ),
            EvidenceEdge(
                from_node_id=run_id,
                to_node_id=f"diff:{raster_diff_sha256}",
                relation="uses",
            ),
            EvidenceEdge(
                from_node_id=run_id,
                to_node_id=f"engine:{run.engine_manifest_sha256}",
                relation="executed_by",
            ),
            EvidenceEdge(
                from_node_id=run_id,
                to_node_id=f"qa:{run.direct_run_id}",
                relation="checked_by",
            ),
        ]
        if baseline_run.direct_run_id != run.direct_run_id:
            edges.extend(
                [
                    EvidenceEdge(
                        from_node_id=metric_id,
                        to_node_id=baseline_run_id,
                        relation="derived_from",
                    ),
                    EvidenceEdge(
                        from_node_id=metric_id,
                        to_node_id=f"current-map:{baseline_run.direct_run_id}",
                        relation="reduced_from_baseline",
                    ),
                    EvidenceEdge(
                        from_node_id=f"current-map:{baseline_run.direct_run_id}",
                        to_node_id=(
                            f"artifact:current-map:{baseline_run.direct_run_id}:"
                            f"{baseline_current['sha256']}"
                        ),
                        relation="retained_as",
                    ),
                    EvidenceEdge(
                        from_node_id=baseline_run_id,
                        to_node_id=f"scenario:{run.scenario_id}",
                        relation="executes",
                    ),
                    EvidenceEdge(
                        from_node_id=baseline_run_id,
                        to_node_id=f"engine:{run.engine_manifest_sha256}",
                        relation="executed_by",
                    ),
                    EvidenceEdge(
                        from_node_id=baseline_run_id,
                        to_node_id=f"qa:{baseline_run.direct_run_id}",
                        relation="checked_by",
                    ),
                ]
            )
        edges.extend(
            EvidenceEdge(
                from_node_id=f"diff:{raster_diff_sha256}",
                to_node_id=f"asset:{item}",
                relation="derived_from",
            )
            for item in source_asset_ids
        )
        edges.extend(
            EvidenceEdge(
                from_node_id=f"scenario:{run.scenario_id}",
                to_node_id=f"assumption:{item}",
                relation="assumes",
            )
            for item in assumption_ids
        )
        edges.extend(
            EvidenceEdge(
                from_node_id=f"asset:{item}",
                to_node_id=f"authorization:{item}",
                relation="authorized_by",
            )
            for item in source_asset_ids
        )
        claim = ConnectivityEvidenceClaim(
            claim_id=claim_id,
            claim_kind="DIRECT_MAP_DIAGNOSTIC",
            text=text,
            value=value,
            units="percent",
            portfolio_id=run.portfolio_id,
            scenario_id=run.scenario_id,
            rule_id=singular_rule_id,
            direct_run_ids=list(dict.fromkeys([run.direct_run_id, baseline_run.direct_run_id])),
            root_node_id=claim_id,
            nodes=nodes,
            edges=edges,
            limitations=[CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION],
        )
        validate_evidence_claim(claim)
        return claim

    @staticmethod
    def _cost_claim(
        *,
        claim_id: str,
        bundle: ConnectivityProjectBundle,
        portfolio: Any,
        cost: float,
        currency: str,
        rule_id: str | None = None,
        rule_ids: list[str] | None = None,
    ) -> ConnectivityEvidenceClaim:
        basis = bundle.cost_basis
        if basis is None:
            raise DurableAnalysisError(
                "a reviewed CostBasis is required before a cost claim can be reported"
            )
        selected = list(portfolio.canonical_option_ids)
        recomputed = sum(basis.option_costs[item] for item in selected)
        if not math.isclose(recomputed, cost, rel_tol=0.0, abs_tol=1e-9):
            raise DurableAnalysisError("portfolio cost differs from its reviewed CostBasis")
        _, graph_rule_ids, singular_rule_id = DurableAnalysisExecutor._claim_rule_ids(
            rule_id=rule_id,
            rule_ids=rule_ids,
            fallback_rule_id="evidence:screening-cost",
        )
        text = (
            f"Screening cost under reviewed basis {basis.cost_basis_id} is "
            f"{cost:.6g} {currency}; it is not a bid or construction estimate."
        )
        basis_attributes: dict[str, str | int | float | bool | None] = {
            "currency": basis.currency,
            "reviewer": basis.reviewer,
            "reviewed_at": basis.reviewed_at.isoformat(),
        }
        if any(item.strip().startswith(ANALYSIS_HORIZON_PREFIX) for item in basis.assumptions):
            try:
                ledger = parse_reviewed_cost_basis(basis, bundle.intervention_options)
            except ValueError as exc:
                raise DurableAnalysisError(
                    "professional cost evidence requires a complete confirmed lifecycle ledger"
                ) from exc
            basis_attributes.update(
                {
                    "analysis_horizon_years": ledger.analysis_horizon_years,
                    "real_discount_rate_percent": ledger.real_discount_rate_percent,
                    "capex_basis": ledger.capex_basis,
                    "om_assumptions": ledger.om_assumptions,
                    "replacement_assumptions": ledger.replacement_assumptions,
                    "reviewer_confirmation": ledger.reviewer_confirmation,
                }
            )
        nodes = [
            EvidenceNode(node_id=claim_id, node_type="CLAIM", label=text),
            *[
                EvidenceNode(node_id=f"rule:{item}", node_type="RULE", label=item)
                for item in graph_rule_ids
            ],
            EvidenceNode(
                node_id=f"cost-basis:{basis.cost_basis_id}",
                node_type="COST_BASIS",
                label=f"{basis.source} ({basis.vintage})",
                sha256=basis.sha256,
                attributes=basis_attributes,
            ),
            EvidenceNode(
                node_id=f"portfolio:{portfolio.portfolio_id}",
                node_type="PORTFOLIO",
                label=portfolio.portfolio_id,
                attributes={"selected_option_count": len(selected)},
            ),
            *[
                EvidenceNode(
                    node_id=f"option:{option_id}",
                    node_type="OPTION",
                    label=option_id,
                    attributes={"cost": basis.option_costs[option_id], "currency": currency},
                )
                for option_id in selected
            ],
        ]
        edges = [
            *[
                EvidenceEdge(
                    from_node_id=claim_id,
                    to_node_id=f"rule:{item}",
                    relation=(
                        "evaluates"
                        if not item.startswith("evidence:")
                        else "defines_evidence_meaning"
                    ),
                )
                for item in graph_rule_ids
            ],
            EvidenceEdge(
                from_node_id=claim_id,
                to_node_id=f"portfolio:{portfolio.portfolio_id}",
                relation="reports_for",
            ),
            EvidenceEdge(
                from_node_id=f"portfolio:{portfolio.portfolio_id}",
                to_node_id=f"cost-basis:{basis.cost_basis_id}",
                relation="priced_under",
            ),
            *[
                EvidenceEdge(
                    from_node_id=f"portfolio:{portfolio.portfolio_id}",
                    to_node_id=f"option:{option_id}",
                    relation="selects",
                )
                for option_id in selected
            ],
            *[
                EvidenceEdge(
                    from_node_id=f"option:{option_id}",
                    to_node_id=f"cost-basis:{basis.cost_basis_id}",
                    relation="priced_by",
                )
                for option_id in selected
            ],
        ]
        claim = ConnectivityEvidenceClaim(
            claim_id=claim_id,
            claim_kind="COST",
            text=text,
            value=cost,
            units=currency,
            portfolio_id=portfolio.portfolio_id,
            rule_id=singular_rule_id,
            direct_run_ids=[],
            root_node_id=claim_id,
            nodes=nodes,
            edges=edges,
            limitations=[
                CONNECTIVITY_LIMITATION,
                "Screening costs are conditional on the reviewed source, vintage, and assumptions.",
            ],
        )
        validate_evidence_claim(claim)
        return claim

    @staticmethod
    def _qa_claim(
        *,
        claim_id: str,
        run: DirectConnectivityRun,
        rule_id: str | None = None,
        rule_ids: list[str] | None = None,
    ) -> ConnectivityEvidenceClaim:
        _, graph_rule_ids, singular_rule_id = DurableAnalysisExecutor._claim_rule_ids(
            rule_id=rule_id,
            rule_ids=rule_ids,
            fallback_rule_id="evidence:direct-qa",
        )
        accepted = run.qa_status in {QAStatus.ACCEPTED, QAStatus.WARNING_ACCEPTED}
        text = (
            f"{PERMITTED_CLAIM_PREFIX} Direct-run QA for {run.scenario_id} is "
            f"{'accepted' if accepted else 'not accepted'}."
        )
        nodes = [
            EvidenceNode(node_id=claim_id, node_type="CLAIM", label=text),
            *[
                EvidenceNode(node_id=f"rule:{item}", node_type="RULE", label=item)
                for item in graph_rule_ids
            ],
            EvidenceNode(
                node_id=f"run:{run.direct_run_id}",
                node_type="DIRECT_RUN",
                label=run.direct_run_id,
                sha256=run.result_sha256,
            ),
            EvidenceNode(
                node_id=f"qa:{run.direct_run_id}",
                node_type="QA",
                label=run.qa_status.value if run.qa_status else "MISSING",
                attributes={"finding_count": len(run.qa_findings)},
            ),
            EvidenceNode(
                node_id=f"engine:{run.engine_manifest_sha256}",
                node_type="ENGINE_MANIFEST",
                label="Pinned Circuitscape engine manifest",
                sha256=run.engine_manifest_sha256,
            ),
        ]
        edges = [
            *[
                EvidenceEdge(
                    from_node_id=claim_id,
                    to_node_id=f"rule:{item}",
                    relation=(
                        "evaluates"
                        if not item.startswith("evidence:")
                        else "defines_evidence_meaning"
                    ),
                )
                for item in graph_rule_ids
            ],
            EvidenceEdge(
                from_node_id=claim_id, to_node_id=f"run:{run.direct_run_id}", relation="reports"
            ),
            EvidenceEdge(
                from_node_id=f"run:{run.direct_run_id}",
                to_node_id=f"qa:{run.direct_run_id}",
                relation="checked_by",
            ),
            EvidenceEdge(
                from_node_id=f"run:{run.direct_run_id}",
                to_node_id=f"engine:{run.engine_manifest_sha256}",
                relation="executed_by",
            ),
        ]
        claim = ConnectivityEvidenceClaim(
            claim_id=claim_id,
            claim_kind="DIRECT_QA",
            text=text,
            value=accepted,
            units="boolean",
            portfolio_id=run.portfolio_id,
            scenario_id=run.scenario_id,
            rule_id=singular_rule_id,
            direct_run_ids=[run.direct_run_id],
            root_node_id=claim_id,
            nodes=nodes,
            edges=edges,
            limitations=[CONNECTIVITY_LIMITATION],
        )
        validate_evidence_claim(claim)
        return claim

    @staticmethod
    def _aggregate_rule_claim(
        *,
        claim_id: str,
        portfolio_id: str,
        aggregate_key: str,
        text: str,
        value: float | bool,
        units: str,
        reducer_semantics: str,
        supporting_claims: list[ConnectivityEvidenceClaim],
        runs: list[DirectConnectivityRun],
        source_rule_ids: list[str] | None = None,
        scenario_id: str | None = None,
        pair_id: str | None = None,
        limitations: list[str] | None = None,
    ) -> ConnectivityEvidenceClaim:
        """Build a reportable reducer claim over exact primitive evidence.

        Aggregate facts never float above the direct evidence graph.  The
        supplied primitive claims must be unique direct pair/map/QA facts, and
        ``runs`` must be the exact union of their direct-run identities.  This
        makes a stale, omitted, or padded support set a hard error.
        """

        if not aggregate_key or not reducer_semantics:
            raise DurableAnalysisError("aggregate evidence requires explicit reducer semantics")
        support_ids = [item.claim_id for item in supporting_claims]
        if not support_ids or len(support_ids) != len(set(support_ids)):
            raise DurableAnalysisError(
                "aggregate evidence requires unique supporting primitive claims"
            )
        allowed_primitive_kinds = {
            "DIRECT_PAIR_METRIC",
            "DIRECT_MAP_DIAGNOSTIC",
            "DIRECT_QA",
        }
        if any(item.claim_kind not in allowed_primitive_kinds for item in supporting_claims):
            raise DurableAnalysisError("aggregate evidence can reduce only direct primitive claims")
        if any(item.portfolio_id != portfolio_id for item in supporting_claims):
            raise DurableAnalysisError(
                "aggregate evidence cannot mix primitive claims from different portfolios"
            )
        expected_run_ids = sorted(
            {direct_run_id for item in supporting_claims for direct_run_id in item.direct_run_ids}
        )
        if not expected_run_ids:
            raise DurableAnalysisError("aggregate primitive claims omit direct-run evidence")
        run_ids = [item.direct_run_id for item in runs]
        if len(run_ids) != len(set(run_ids)) or sorted(run_ids) != expected_run_ids:
            raise DurableAnalysisError(
                "aggregate direct runs must exactly match primitive-claim support"
            )
        run_by_id = {item.direct_run_id: item for item in runs}
        if any(
            run_by_id[item].state.value != "SUCCEEDED"
            or not run_by_id[item].exact
            or run_by_id[item].test_only
            or run_by_id[item].engine_name != "Circuitscape"
            or run_by_id[item].result_sha256 is None
            for item in expected_run_ids
        ):
            raise DurableAnalysisError("aggregate evidence requires successful direct results")

        aggregate_rule_id = f"aggregate:{aggregate_key}"
        reducer_id = f"reducer:{claim_id}"
        source_rules = sorted(set(source_rule_ids or []))
        ordered_support = sorted(supporting_claims, key=lambda item: item.claim_id)
        ordered_runs = [run_by_id[item] for item in expected_run_ids]
        nodes = [
            EvidenceNode(node_id=claim_id, node_type="CLAIM", label=text),
            EvidenceNode(
                node_id=f"rule:{aggregate_rule_id}",
                node_type="RULE",
                label=aggregate_key.replace("_", " "),
            ),
            EvidenceNode(
                node_id=reducer_id,
                node_type="REDUCER",
                label=reducer_semantics,
                attributes={
                    "aggregate_key": aggregate_key,
                    "semantics": reducer_semantics,
                    "supporting_claim_count": len(ordered_support),
                    "direct_run_count": len(ordered_runs),
                },
            ),
            *[
                EvidenceNode(
                    node_id=f"rule:{item}",
                    node_type="RULE",
                    label=item,
                )
                for item in source_rules
                if item != aggregate_rule_id
            ],
            *[
                EvidenceNode(
                    node_id=f"supporting-claim:{item.claim_id}",
                    node_type="SUPPORTING_CLAIM",
                    label=item.claim_id,
                    attributes={
                        "claim_id": item.claim_id,
                        "claim_kind": item.claim_kind,
                    },
                )
                for item in ordered_support
            ],
            *[
                EvidenceNode(
                    node_id=f"run:{item.direct_run_id}",
                    node_type="DIRECT_RUN",
                    label=item.direct_run_id,
                    sha256=item.result_sha256,
                )
                for item in ordered_runs
            ],
        ]
        edges = [
            EvidenceEdge(
                from_node_id=claim_id,
                to_node_id=f"rule:{aggregate_rule_id}",
                relation="evaluates",
            ),
            EvidenceEdge(
                from_node_id=claim_id,
                to_node_id=reducer_id,
                relation="reports",
            ),
            *[
                EvidenceEdge(
                    from_node_id=reducer_id,
                    to_node_id=f"rule:{item}",
                    relation="applies_rule",
                )
                for item in source_rules
                if item != aggregate_rule_id
            ],
            *[
                EvidenceEdge(
                    from_node_id=reducer_id,
                    to_node_id=f"supporting-claim:{item.claim_id}",
                    relation="reduces_claim",
                )
                for item in ordered_support
            ],
            *[
                EvidenceEdge(
                    from_node_id=f"supporting-claim:{item.claim_id}",
                    to_node_id=f"run:{direct_run_id}",
                    relation="supported_by_direct_run",
                )
                for item in ordered_support
                for direct_run_id in sorted(item.direct_run_ids)
            ],
        ]
        claim = ConnectivityEvidenceClaim(
            claim_id=claim_id,
            claim_kind="AGGREGATE_RULE",
            text=text,
            value=value,
            units=units,
            portfolio_id=portfolio_id,
            scenario_id=scenario_id,
            pair_id=pair_id,
            rule_id=aggregate_rule_id,
            direct_run_ids=expected_run_ids,
            root_node_id=claim_id,
            nodes=nodes,
            edges=edges,
            limitations=list(limitations or [CONNECTIVITY_LIMITATION]),
        )
        validate_evidence_claim(claim)
        return claim

    @staticmethod
    def _feasibility_claim(
        *, claim_id: str, portfolio_id: str, concerns: list[str]
    ) -> ConnectivityEvidenceClaim:
        text = (
            "Feasibility disposition requires survey or review before carry-forward: "
            + "; ".join(concerns)
        )
        nodes = [
            EvidenceNode(node_id=claim_id, node_type="CLAIM", label=text),
            EvidenceNode(
                node_id=f"portfolio:{portfolio_id}",
                node_type="PORTFOLIO",
                label=portfolio_id,
            ),
            EvidenceNode(
                node_id=f"feasibility:{claim_id}",
                node_type="FEASIBILITY",
                label="Unresolved feasibility record",
                attributes={"concern_count": len(concerns)},
            ),
        ]
        claim = ConnectivityEvidenceClaim(
            claim_id=claim_id,
            claim_kind="FEASIBILITY",
            text=text,
            value="SURVEY_FIRST",
            units="categorical",
            portfolio_id=portfolio_id,
            direct_run_ids=[],
            root_node_id=claim_id,
            nodes=nodes,
            edges=[
                EvidenceEdge(
                    from_node_id=claim_id,
                    to_node_id=f"portfolio:{portfolio_id}",
                    relation="reports_for",
                ),
                EvidenceEdge(
                    from_node_id=f"portfolio:{portfolio_id}",
                    to_node_id=f"feasibility:{claim_id}",
                    relation="has_unresolved",
                ),
            ],
            limitations=[CONNECTIVITY_LIMITATION],
        )
        validate_evidence_claim(claim)
        return claim

    @staticmethod
    def _portfolio_disposition_claim(
        *,
        claim_id: str,
        summary: PortfolioDecisionSummary,
        runs: list[DirectConnectivityRun],
        supporting_claim_ids: list[str],
    ) -> ConnectivityEvidenceClaim:
        supporting_ids = sorted(set(supporting_claim_ids))
        if not supporting_ids or len(supporting_ids) != len(supporting_claim_ids):
            raise DurableAnalysisError(
                "portfolio disposition requires unique supporting evidence claims"
            )
        text = (
            f"Portfolio {summary.portfolio_id} screening disposition is "
            f"{summary.disposition.value}. Next action: {summary.next_action}"
        )
        nodes = [
            EvidenceNode(node_id=claim_id, node_type="CLAIM", label=text),
            EvidenceNode(
                node_id=f"portfolio:{summary.portfolio_id}",
                node_type="PORTFOLIO",
                label=summary.portfolio_id,
                attributes={"cost": summary.cost, "currency": summary.currency},
            ),
            EvidenceNode(
                node_id=f"disposition:{summary.portfolio_id}",
                node_type="DISPOSITION",
                label=summary.disposition.value,
                attributes={
                    "next_action": summary.next_action,
                    "review_mask_triggered": summary.review_mask_triggered,
                    "safeguard_passed": summary.safeguard_passed,
                    "supporting_claim_count": len(supporting_ids),
                },
            ),
            *[
                EvidenceNode(
                    node_id=f"run:{run.direct_run_id}",
                    node_type="DIRECT_RUN",
                    label=run.direct_run_id,
                    sha256=run.result_sha256,
                )
                for run in sorted(runs, key=lambda item: item.direct_run_id)
            ],
            *[
                EvidenceNode(
                    node_id=f"supporting-claim:{supporting_claim_id}",
                    node_type="SUPPORTING_CLAIM",
                    label=supporting_claim_id,
                    attributes={"claim_id": supporting_claim_id},
                )
                for supporting_claim_id in supporting_ids
            ],
        ]
        edges = [
            EvidenceEdge(
                from_node_id=claim_id,
                to_node_id=f"portfolio:{summary.portfolio_id}",
                relation="reports_for",
            ),
            EvidenceEdge(
                from_node_id=f"portfolio:{summary.portfolio_id}",
                to_node_id=f"disposition:{summary.portfolio_id}",
                relation="has_disposition",
            ),
            *[
                EvidenceEdge(
                    from_node_id=f"disposition:{summary.portfolio_id}",
                    to_node_id=f"run:{run.direct_run_id}",
                    relation="supported_by_direct_ensemble",
                )
                for run in sorted(runs, key=lambda item: item.direct_run_id)
            ],
            *[
                EvidenceEdge(
                    from_node_id=f"disposition:{summary.portfolio_id}",
                    to_node_id=f"supporting-claim:{supporting_claim_id}",
                    relation="supported_by_claim",
                )
                for supporting_claim_id in supporting_ids
            ],
        ]
        claim = ConnectivityEvidenceClaim(
            claim_id=claim_id,
            claim_kind="PORTFOLIO_DISPOSITION",
            text=text,
            value=summary.disposition.value,
            units="categorical",
            portfolio_id=summary.portfolio_id,
            direct_run_ids=sorted(run.direct_run_id for run in runs),
            root_node_id=claim_id,
            nodes=nodes,
            edges=edges,
            limitations=[
                CONNECTIVITY_LIMITATION,
                "A screening disposition is not an approved corridor or design decision.",
            ],
        )
        validate_evidence_claim(claim)
        return claim

    def _complete_analysis(
        self,
        store: EcoProjectStore,
        bundle: ConnectivityProjectBundle,
        plan: ConnectivityRunPlan,
    ) -> None:
        runs, _, _ = store.analysis_results(plan.analysis_id)
        prepared_by_run: dict[str, PreparedConnectivityRun] = {}
        for job in store.direct_jobs(plan.analysis_id):
            prepared_payload = job.get("prepared")
            if prepared_payload is None:
                continue
            prepared_by_run[str(job["direct_job_id"])] = PreparedConnectivityRun.model_validate(
                prepared_payload
            )
        if {run.direct_run_id for run in runs} - set(prepared_by_run):
            raise DurableAnalysisError(
                "successful direct runs are missing immutable prepared-raster evidence"
            )
        baseline_runs = [item for item in runs if item.portfolio_id == plan.baseline_portfolio_id]
        baseline_by_scenario = {item.scenario_id: item for item in baseline_runs}
        scenarios = [
            next(item for item in bundle.scenarios if item.scenario_id == scenario_id)
            for scenario_id in plan.scenario_ids
        ]
        summaries: list[PortfolioDecisionSummary] = []
        claims: list[ConnectivityEvidenceClaim] = []
        all_runs_by_id = {item.direct_run_id: item for item in runs}
        for portfolio in plan.portfolios:
            portfolio_runs = [item for item in runs if item.portfolio_id == portfolio.portfolio_id]
            cost, currency, _ = portfolio_cost(portfolio, bundle.intervention_options)
            unresolved = unresolved_portfolio_concerns(portfolio, bundle.intervention_options)
            summary = evaluate_portfolio(
                portfolio=portfolio,
                runs=portfolio_runs,
                baseline_runs=baseline_runs,
                scenarios=scenarios,
                rules=bundle.decision_rules,
                cost=cost,
                currency=currency,
                unresolved_feasibility=unresolved,
                safeguard_tolerances=self._safeguard_tolerances(bundle),
            )
            claim_by_scope: dict[tuple[str, str, str | None], list[str]] = {}
            portfolio_claims: list[ConnectivityEvidenceClaim] = []

            # Screening cost is one reviewed-basis fact per portfolio.  Decision
            # rules consume that fact; they do not multiply or suppress it.
            cost_rules = sorted(
                (rule for rule in bundle.decision_rules if rule.metric == RuleMetric.MAX_COST),
                key=lambda item: item.rule_id,
            )
            cost_claim_id = (
                "claim_"
                + canonical_sha256([plan.analysis_id, portfolio.portfolio_id, "primitive", "cost"])[
                    :24
                ]
            )
            cost_claim = self._cost_claim(
                claim_id=cost_claim_id,
                bundle=bundle,
                portfolio=portfolio,
                rule_ids=[item.rule_id for item in cost_rules],
                cost=cost,
                currency=currency,
            )
            claims.append(cost_claim)
            portfolio_claims.append(cost_claim)
            for rule in cost_rules:
                claim_by_scope.setdefault((rule.rule_id, "", None), []).append(cost_claim_id)

            attached_by_scenario: dict[str, DirectConnectivityRun] = {}
            review_primitive_claims: list[ConnectivityEvidenceClaim] = []
            for raw_run in sorted(portfolio_runs, key=lambda item: item.scenario_id):
                attached = attach_baseline_metrics(
                    raw_run, baseline_by_scenario[raw_run.scenario_id]
                )
                attached_by_scenario[raw_run.scenario_id] = attached
                scenario = next(
                    item for item in scenarios if item.scenario_id == raw_run.scenario_id
                )
                source_assets, assumptions = self._claim_sources(bundle, scenario, portfolio)
                raster_diff_sha256 = prepared_by_run[raw_run.direct_run_id].action_raster_sha256

                # QA is one primitive fact per requested direct run, even when
                # there is no QA rule or several rules consume the same fact.
                qa_rules = sorted(
                    (
                        rule
                        for rule in bundle.decision_rules
                        if rule.metric == RuleMetric.REQUIRE_ACCEPTED_QA
                        and (not rule.scenario_ids or scenario.scenario_id in rule.scenario_ids)
                    ),
                    key=lambda item: item.rule_id,
                )
                qa_claim_id = (
                    "claim_"
                    + canonical_sha256(
                        [
                            plan.analysis_id,
                            portfolio.portfolio_id,
                            scenario.scenario_id,
                            "primitive",
                            "qa",
                        ]
                    )[:24]
                )
                qa_claim = self._qa_claim(
                    claim_id=qa_claim_id,
                    run=attached,
                    rule_ids=[item.rule_id for item in qa_rules],
                )
                qa_claim = self._with_cache_lineage(qa_claim, attached)
                claims.append(qa_claim)
                portfolio_claims.append(qa_claim)
                for rule in qa_rules:
                    claim_by_scope.setdefault(
                        (rule.rule_id, scenario.scenario_id, None), []
                    ).append(qa_claim_id)

                # Each retained mask comparison is likewise one primitive fact.
                # It remains available for the reportable portfolio-level trigger
                # even when no threshold rule consumes it.
                review_rules = sorted(
                    (
                        rule
                        for rule in bundle.decision_rules
                        if rule.metric == RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT
                        and (not rule.scenario_ids or scenario.scenario_id in rule.scenario_ids)
                    ),
                    key=lambda item: item.rule_id,
                )
                for diagnostic in attached.diagnostic_maps:
                    if diagnostic.relative_change_percent is None:
                        continue
                    diagnostic_claim_id = (
                        "claim_"
                        + canonical_sha256(
                            [
                                plan.analysis_id,
                                portfolio.portfolio_id,
                                scenario.scenario_id,
                                diagnostic.mask_id,
                                "primitive",
                                "review_mask_current_change",
                            ]
                        )[:24]
                    )
                    diagnostic_claim = self._diagnostic_claim(
                        claim_id=diagnostic_claim_id,
                        run=attached,
                        baseline_run=baseline_by_scenario[raw_run.scenario_id],
                        rule_ids=[item.rule_id for item in review_rules],
                        diagnostic=diagnostic,
                        source_asset_ids=sorted(
                            set(
                                source_assets
                                + [
                                    next(
                                        item.asset_id
                                        for item in bundle.review_masks
                                        if item.mask_id == diagnostic.mask_id
                                    )
                                ]
                            )
                        ),
                        assumption_ids=assumptions,
                        raster_diff_sha256=raster_diff_sha256,
                        run_artifacts=store.run_artifacts(raw_run.direct_run_id),
                        baseline_artifacts=store.run_artifacts(
                            baseline_by_scenario[raw_run.scenario_id].direct_run_id
                        ),
                    )
                    diagnostic_claim = self._with_cache_lineage(
                        diagnostic_claim,
                        attached,
                        baseline_by_scenario[raw_run.scenario_id],
                    )
                    claims.append(diagnostic_claim)
                    portfolio_claims.append(diagnostic_claim)
                    review_primitive_claims.append(diagnostic_claim)
                    for rule in review_rules:
                        claim_by_scope.setdefault(
                            (rule.rule_id, scenario.scenario_id, None), []
                        ).append(diagnostic_claim_id)

                for rule in bundle.decision_rules:
                    if rule.scenario_ids and scenario.scenario_id not in rule.scenario_ids:
                        continue
                    if rule.metric in {
                        RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT,
                        RuleMetric.MAX_RESISTANCE_INCREASE_PERCENT,
                        RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION,
                    }:
                        if (
                            rule.metric == RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION
                            and not scenario.safeguard
                        ):
                            continue
                        for metric in attached.pair_metrics:
                            if rule.pair_ids and metric.pair_id not in rule.pair_ids:
                                continue
                            claim_id = (
                                "claim_"
                                + canonical_sha256(
                                    [
                                        plan.analysis_id,
                                        portfolio.portfolio_id,
                                        scenario.scenario_id,
                                        metric.pair_id,
                                        rule.rule_id,
                                    ]
                                )[:24]
                            )
                            claim = build_pair_metric_claim(
                                claim_id=claim_id,
                                run=attached,
                                baseline_run=baseline_by_scenario[raw_run.scenario_id],
                                metric=metric,
                                rule=rule,
                                raster_diff_sha256=raster_diff_sha256,
                                source_asset_ids=source_assets,
                                assumption_ids=assumptions,
                                authorization_ids=source_assets,
                                reported_value=(
                                    metric.baseline_relative_change_percent
                                    if rule.metric == RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT
                                    else max(
                                        0.0,
                                        -float(metric.baseline_relative_change_percent),
                                    )
                                ),
                                reported_metric_label=(
                                    "Within-scenario baseline-relative effective-resistance reduction"
                                    if rule.metric == RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT
                                    else (
                                        "Within-scenario safeguard effective-resistance increase"
                                        if rule.metric == RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION
                                        else "Within-scenario effective-resistance increase"
                                    )
                                ),
                            )
                            claim = self._with_cache_lineage(
                                claim,
                                attached,
                                baseline_by_scenario[raw_run.scenario_id],
                            )
                            claims.append(claim)
                            portfolio_claims.append(claim)
                            claim_by_scope.setdefault(
                                (rule.rule_id, scenario.scenario_id, metric.pair_id), []
                            ).append(claim_id)

            # The headline worst/controlling value is evaluated over the full
            # comparable ensemble, not only over whichever thresholds happened
            # to be configured.  Retain one intrinsic direct primitive for
            # *every* complete scenario/pair observation.  The aggregate can
            # then prove both the reduction scope and its selected argmin;
            # retaining only the selected minimum would make ``MIN_OVER_ALL``
            # an unsupported assertion.
            worst_primitive_claims: list[ConnectivityEvidenceClaim] = []
            worst_scope = (
                summary.controlling_scenario_id,
                summary.controlling_pair_id,
                summary.worst_resistance_reduction_percent,
            )
            if any(item is None for item in worst_scope) and not all(
                item is None for item in worst_scope
            ):
                raise DurableAnalysisError(
                    "worst/controlling summary has an incomplete evidence scope"
                )
            observed_scope: list[tuple[float, str, str, ConnectivityEvidenceClaim]] = []
            for scenario in sorted(scenarios, key=lambda item: item.scenario_id):
                attached_run = attached_by_scenario[scenario.scenario_id]
                source_assets, assumptions = self._claim_sources(bundle, scenario, portfolio)
                for metric in sorted(attached_run.pair_metrics, key=lambda item: item.pair_id):
                    observed = metric.baseline_relative_change_percent
                    if observed is None:
                        raise DurableAnalysisError(
                            "full-ensemble primitive lacks its within-scenario baseline"
                        )
                    primitive_rule = ConnectivityDecisionRule(
                        rule_id="evidence:within-scenario-resistance-change",
                        title="Within-scenario resistance-change evidence meaning",
                        metric=RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT,
                        operator=RuleOperator.AT_LEAST,
                        threshold=0.0,
                        scenario_ids=[scenario.scenario_id],
                        pair_ids=[metric.pair_id],
                        required=False,
                        rationale=(
                            "Primitive evidence meaning for the full-ensemble controlling "
                            "reducer; not a decision threshold."
                        ),
                    )
                    primitive_claim_id = (
                        "claim_"
                        + canonical_sha256(
                            [
                                plan.analysis_id,
                                portfolio.portfolio_id,
                                scenario.scenario_id,
                                metric.pair_id,
                                "primitive",
                                "within_scenario_resistance_change",
                            ]
                        )[:24]
                    )
                    primitive_claim = build_pair_metric_claim(
                        claim_id=primitive_claim_id,
                        run=attached_run,
                        baseline_run=baseline_by_scenario[scenario.scenario_id],
                        metric=metric,
                        rule=primitive_rule,
                        raster_diff_sha256=prepared_by_run[
                            attached_run.direct_run_id
                        ].action_raster_sha256,
                        source_asset_ids=source_assets,
                        assumption_ids=assumptions,
                        authorization_ids=source_assets,
                    )
                    primitive_claim = self._with_cache_lineage(
                        primitive_claim,
                        attached_run,
                        baseline_by_scenario[scenario.scenario_id],
                    )
                    claims.append(primitive_claim)
                    portfolio_claims.append(primitive_claim)
                    worst_primitive_claims.append(primitive_claim)
                    observed_scope.append(
                        (observed, scenario.scenario_id, metric.pair_id, primitive_claim)
                    )

            if summary.controlling_scenario_id is not None:
                assert summary.controlling_pair_id is not None
                assert summary.worst_resistance_reduction_percent is not None
                observed, observed_scenario, observed_pair, _ = min(observed_scope)
                if (
                    observed_scenario != summary.controlling_scenario_id
                    or observed_pair != summary.controlling_pair_id
                    or not math.isclose(
                        observed,
                        summary.worst_resistance_reduction_percent,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                ):
                    raise DurableAnalysisError(
                        "worst/controlling summary differs from its full direct primitive set"
                    )
            elif observed_scope:
                raise DurableAnalysisError(
                    "complete full-ensemble primitives lack a controlling summary"
                )

            dispositions = []
            for disposition in summary.rule_dispositions:
                scope = (
                    disposition.rule_id,
                    disposition.scenario_id or "",
                    disposition.pair_id,
                )
                candidates = claim_by_scope.get(scope, [])
                evidence_claim_id = candidates.pop(0) if candidates else None
                dispositions.append(
                    disposition.model_copy(update={"evidence_claim_id": evidence_claim_id})
                )
            updated_summary = summary.model_copy(update={"rule_dispositions": dispositions})
            portfolio_claim_by_id = {item.claim_id: item for item in portfolio_claims}

            def aggregate_runs_for(
                primitive_claims: list[ConnectivityEvidenceClaim],
            ) -> list[DirectConnectivityRun]:
                direct_run_ids = sorted(
                    {
                        direct_run_id
                        for item in primitive_claims
                        for direct_run_id in item.direct_run_ids
                    }
                )
                try:
                    return [all_runs_by_id[item] for item in direct_run_ids]
                except KeyError as exc:
                    raise DurableAnalysisError(
                        "aggregate primitive references an unavailable direct run"
                    ) from exc

            if worst_primitive_claims:
                worst_claim_id = (
                    "claim_"
                    + canonical_sha256(
                        [
                            plan.analysis_id,
                            portfolio.portfolio_id,
                            "aggregate",
                            "worst_controlling_resistance_change",
                        ]
                    )[:24]
                )
                worst_claim = self._aggregate_rule_claim(
                    claim_id=worst_claim_id,
                    portfolio_id=portfolio.portfolio_id,
                    aggregate_key="worst_controlling_resistance_change",
                    text=(
                        f"{PERMITTED_CLAIM_PREFIX} The worst within-scenario "
                        "baseline-relative effective-resistance change is "
                        f"{summary.worst_resistance_reduction_percent:.6g}% for scenario "
                        f"{summary.controlling_scenario_id} and focal pair "
                        f"{summary.controlling_pair_id}; this is the controlling modeled case."
                    ),
                    value=summary.worst_resistance_reduction_percent,
                    units="percent",
                    reducer_semantics=(
                        "MIN_BY_NUMERIC_VALUE_OVER_ALL_COMPLETE_SCENARIO_PAIR_PRIMITIVES"
                    ),
                    supporting_claims=worst_primitive_claims,
                    runs=aggregate_runs_for(worst_primitive_claims),
                    scenario_id=summary.controlling_scenario_id,
                    pair_id=summary.controlling_pair_id,
                )
                claims.append(worst_claim)
                portfolio_claims.append(worst_claim)
                portfolio_claim_by_id[worst_claim.claim_id] = worst_claim

            rule_by_id = {item.rule_id: item for item in bundle.decision_rules}
            safeguard_dispositions = [
                item
                for item in dispositions
                if rule_by_id[item.rule_id].metric == RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION
            ]
            if summary.safeguard_passed is not None:
                if not safeguard_dispositions or any(
                    item.evidence_claim_id is None for item in safeguard_dispositions
                ):
                    raise DurableAnalysisError(
                        "reportable safeguard aggregate lacks primitive rule evidence"
                    )
                safeguard_support_ids = sorted(
                    {str(item.evidence_claim_id) for item in safeguard_dispositions}
                )
                try:
                    safeguard_support = [
                        portfolio_claim_by_id[item] for item in safeguard_support_ids
                    ]
                except KeyError as exc:
                    raise DurableAnalysisError(
                        "safeguard aggregate references an unavailable primitive claim"
                    ) from exc
                expected_safeguard = all(item.passed for item in safeguard_dispositions)
                if summary.safeguard_passed != expected_safeguard:
                    raise DurableAnalysisError(
                        "safeguard summary differs from its exact rule dispositions"
                    )
                safeguard_claim_id = (
                    "claim_"
                    + canonical_sha256(
                        [
                            plan.analysis_id,
                            portfolio.portfolio_id,
                            "aggregate",
                            "safeguard_nonregression",
                        ]
                    )[:24]
                )
                safeguard_claim = self._aggregate_rule_claim(
                    claim_id=safeguard_claim_id,
                    portfolio_id=portfolio.portfolio_id,
                    aggregate_key="safeguard_nonregression",
                    text=(
                        f"{PERMITTED_CLAIM_PREFIX} The required safeguard nonregression "
                        f"aggregate is {'passed' if expected_safeguard else 'not passed'} "
                        "across its exact scenario and focal-pair observations."
                    ),
                    value=expected_safeguard,
                    units="boolean",
                    reducer_semantics="ALL_REQUIRED_SAFEGUARD_RULE_OBSERVATIONS_PASS",
                    supporting_claims=safeguard_support,
                    runs=aggregate_runs_for(safeguard_support),
                    source_rule_ids=sorted({item.rule_id for item in safeguard_dispositions}),
                )
                claims.append(safeguard_claim)
                portfolio_claims.append(safeguard_claim)
                portfolio_claim_by_id[safeguard_claim.claim_id] = safeguard_claim

            observed_review_trigger = any(
                item.review_triggered
                for run in attached_by_scenario.values()
                for item in run.diagnostic_maps
            )
            if observed_review_trigger != summary.review_mask_triggered:
                raise DurableAnalysisError(
                    "review-mask summary differs from retained diagnostic primitives"
                )
            if review_primitive_claims:
                review_claim_id = (
                    "claim_"
                    + canonical_sha256(
                        [
                            plan.analysis_id,
                            portfolio.portfolio_id,
                            "aggregate",
                            "review_mask_trigger",
                        ]
                    )[:24]
                )
                review_claim = self._aggregate_rule_claim(
                    claim_id=review_claim_id,
                    portfolio_id=portfolio.portfolio_id,
                    aggregate_key="review_mask_trigger",
                    text=(
                        f"{PERMITTED_CLAIM_PREFIX} The review-mask diagnostic aggregate is "
                        f"{'triggered' if observed_review_trigger else 'not triggered'} across "
                        "retained current-map comparisons; this is diagnostic only and not "
                        "predicted animal use."
                    ),
                    value=observed_review_trigger,
                    units="boolean",
                    reducer_semantics=("ANY_REVIEW_MASK_PRIMITIVE_HAS_REVIEW_TRIGGERED_TRUE"),
                    supporting_claims=review_primitive_claims,
                    runs=aggregate_runs_for(review_primitive_claims),
                    source_rule_ids=sorted(
                        {
                            item.rule_id
                            for item in bundle.decision_rules
                            if item.metric == RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT
                        }
                    ),
                    limitations=[CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION],
                )
                claims.append(review_claim)
                portfolio_claims.append(review_claim)
                portfolio_claim_by_id[review_claim.claim_id] = review_claim

            supporting_ids = [claim.claim_id for claim in portfolio_claims]
            if unresolved:
                feasibility_claim_id = (
                    "claim_"
                    + canonical_sha256(
                        [plan.analysis_id, portfolio.portfolio_id, "feasibility", unresolved]
                    )[:24]
                )
                claims.append(
                    self._feasibility_claim(
                        claim_id=feasibility_claim_id,
                        portfolio_id=portfolio.portfolio_id,
                        concerns=unresolved,
                    )
                )
                supporting_ids.append(feasibility_claim_id)
            disposition_claim_id = (
                "claim_"
                + canonical_sha256(
                    [
                        plan.analysis_id,
                        portfolio.portfolio_id,
                        "portfolio_disposition",
                        updated_summary.model_dump(mode="json"),
                        sorted(supporting_ids),
                    ]
                )[:24]
            )
            claims.append(
                self._portfolio_disposition_claim(
                    claim_id=disposition_claim_id,
                    summary=updated_summary,
                    runs=portfolio_runs,
                    supporting_claim_ids=sorted(supporting_ids),
                )
            )
            summaries.append(
                updated_summary.model_copy(
                    update={"disposition_evidence_claim_id": disposition_claim_id}
                )
            )
        store.complete_analysis(plan.analysis_id, summaries=summaries, claims=claims)
