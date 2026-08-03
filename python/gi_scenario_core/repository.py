"""Durable SQLite project repository and explicit state machines."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import AnalysisState, ReadinessLevel, SolverJobState, new_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


ANALYSIS_TRANSITIONS: dict[AnalysisState, set[AnalysisState]] = {
    AnalysisState.DRAFT: {AnalysisState.VALIDATING, AnalysisState.CANCELLED},
    AnalysisState.VALIDATING: {AnalysisState.READY, AnalysisState.FAILED, AnalysisState.CANCELLED},
    AnalysisState.READY: {AnalysisState.RUNNING, AnalysisState.CANCELLED},
    AnalysisState.RUNNING: {
        AnalysisState.PAUSING,
        AnalysisState.COMPLETED,
        AnalysisState.FAILED,
        AnalysisState.CANCELLED,
    },
    AnalysisState.PAUSING: {AnalysisState.PAUSED, AnalysisState.FAILED, AnalysisState.CANCELLED},
    AnalysisState.PAUSED: {AnalysisState.RUNNING, AnalysisState.CANCELLED},
    AnalysisState.COMPLETED: set(),
    AnalysisState.FAILED: set(),
    AnalysisState.CANCELLED: set(),
}

JOB_TRANSITIONS: dict[SolverJobState, set[SolverJobState]] = {
    SolverJobState.PLANNED: {SolverJobState.QUEUED, SolverJobState.CANCELLED},
    SolverJobState.QUEUED: {SolverJobState.RUNNING, SolverJobState.CANCELLED},
    SolverJobState.RUNNING: {
        SolverJobState.SUCCEEDED,
        SolverJobState.QA_REJECTED,
        SolverJobState.FAILED,
        SolverJobState.CANCELLED,
        SolverJobState.QUEUED,
    },
    SolverJobState.SUCCEEDED: set(),
    SolverJobState.QA_REJECTED: set(),
    SolverJobState.FAILED: set(),
    SolverJobState.CANCELLED: set(),
}


def _safe_project_child(root: Path, relative: str) -> Path:
    """Reject repository layout components that traverse through symbolic links."""

    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"project repository path may not be a symbolic link: {relative}")
    candidate = current.resolve()
    if root not in candidate.parents:
        raise ValueError(f"project repository path escapes the project directory: {relative}")
    return candidate


MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    readiness TEXT NOT NULL CHECK(readiness IN ('DEMO','CONCEPTUAL','ENGINEER_REVIEWED','REFERENCE_MATCHED')),
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_assets (
    asset_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(project_id, path)
);
CREATE TABLE IF NOT EXISTS data_assets (
    asset_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_objects (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(project_id, object_type, object_id)
);
CREATE TABLE IF NOT EXISTS model_provenance (
    provenance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    parameter TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_attestations (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(project_id, key)
);
CREATE TABLE IF NOT EXISTS storm_cases (
    storm_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_slots (
    slot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_specs (
    metric_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cost_bases (
    cost_basis_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    confirmed INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    idempotency_key TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS designs (
    design_id TEXT NOT NULL,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    vector_json TEXT NOT NULL,
    source TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    PRIMARY KEY(analysis_id, design_id),
    UNIQUE(analysis_id, vector_json)
);
CREATE TABLE IF NOT EXISTS solver_jobs (
    job_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    design_id TEXT NOT NULL,
    storm_id TEXT NOT NULL,
    state TEXT NOT NULL,
    physics_cache_key TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(analysis_id, design_id, storm_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS solver_jobs_completed_cache
    ON solver_jobs(physics_cache_key) WHERE state = 'SUCCEEDED';
CREATE TABLE IF NOT EXISTS run_artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES solver_jobs(job_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, role)
);
CREATE TABLE IF NOT EXISTS run_metrics (
    job_id TEXT NOT NULL REFERENCES solver_jobs(job_id) ON DELETE CASCADE,
    metric_id TEXT NOT NULL,
    object_id TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL,
    units TEXT NOT NULL,
    PRIMARY KEY(job_id, metric_id, object_id)
);
CREATE TABLE IF NOT EXISTS qa_findings (
    finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES solver_jobs(job_id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_events (
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(analysis_id, sequence)
);
CREATE TABLE IF NOT EXISTS energy_observations (
    observation_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
    export_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS metrics_require_succeeded_job
BEFORE INSERT ON run_metrics
BEGIN
    SELECT CASE WHEN (SELECT state FROM solver_jobs WHERE job_id = NEW.job_id) != 'SUCCEEDED'
        THEN RAISE(ABORT, 'run metrics require a succeeded exact solver job') END;
END;
CREATE TRIGGER IF NOT EXISTS completed_artifacts_immutable_update
BEFORE UPDATE ON run_artifacts BEGIN
    SELECT RAISE(ABORT, 'completed run artifacts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS completed_artifacts_immutable_delete
BEFORE DELETE ON run_artifacts BEGIN
    SELECT RAISE(ABORT, 'completed run artifacts are immutable');
END;
"""


class ProjectRepository:
    def __init__(self, project_directory: str | Path) -> None:
        self.root = Path(project_directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in (
            "sources",
            "data_assets",
            "materialized_models",
            "blobs/sha256",
            "runs",
            "exports",
            "logs",
        ):
            _safe_project_child(self.root, name).mkdir(parents=True, exist_ok=True)
        for database_sidecar in (
            "project.sqlite",
            "project.sqlite-journal",
            "project.sqlite-shm",
            "project.sqlite-wal",
        ):
            _safe_project_child(self.root, database_sidecar)
        self.database_path = self.root / "project.sqlite"
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")

    def _migrate(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version < 1:
            # executescript manages its own transaction boundary in sqlite3.
            self._connection.executescript(MIGRATION_1)
            self._connection.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def close(self) -> None:
        self._connection.close()

    def create_project(
        self,
        *,
        title: str,
        readiness: ReadinessLevel | str,
        project_id: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a project at an explicitly supplied readiness; never promote it."""

        project_id = project_id or new_id("project")
        readiness = ReadinessLevel(readiness)
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO projects(project_id,title,readiness,state_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (project_id, title, readiness.value, _json(state or {}), now, now),
            )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"project {project_id!r} was not found")
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json"))
        return result

    def set_project_state(self, project_id: str, state: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE projects SET state_json=?, updated_at=? WHERE project_id=?",
                (_json(state), _now(), project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"project {project_id!r} was not found")
        return self.get_project(project_id)

    def record_readiness(
        self,
        project_id: str,
        readiness: ReadinessLevel | str,
        *,
        gate_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a readiness decision already validated by validation.promote_readiness."""

        readiness = ReadinessLevel(readiness)
        if not gate_evidence:
            raise ValueError("readiness changes require recorded gate evidence")
        project = self.get_project(project_id)
        current = ReadinessLevel(project["readiness"])
        rank = {
            ReadinessLevel.DEMO: 0,
            ReadinessLevel.CONCEPTUAL: 1,
            ReadinessLevel.ENGINEER_REVIEWED: 2,
            ReadinessLevel.REFERENCE_MATCHED: 3,
        }
        if rank[readiness] <= rank[current]:
            raise ValueError("readiness changes must be a forward promotion to a new level")
        if rank[readiness] != rank[current] + 1:
            raise ValueError("readiness levels cannot be skipped")
        if readiness == ReadinessLevel.CONCEPTUAL:
            if not gate_evidence.get("generation_recipe_id") or not gate_evidence.get(
                "conceptual_notice_acknowledged"
            ):
                raise ValueError(
                    "CONCEPTUAL promotion requires a recipe and notice acknowledgement"
                )
        elif readiness == ReadinessLevel.ENGINEER_REVIEWED:
            validation = gate_evidence.get("validation", {})
            attestations = gate_evidence.get("attestations", [])
            required = {"topology", "geometry", "hydrology", "storms", "limitations"}
            accepted = {
                item.get("key")
                for item in attestations
                if isinstance(item, dict) and item.get("accepted") is True
            }
            if validation.get("blocking_count") != 0:
                raise ValueError("ENGINEER_REVIEWED requires zero blocking validation issues")
            if accepted != required or len(attestations) != len(required):
                raise ValueError("ENGINEER_REVIEWED requires the complete accepted checklist")
            if not gate_evidence.get("reviewer"):
                raise ValueError("ENGINEER_REVIEWED requires a named reviewer")
            if gate_evidence.get("unreviewed_assumption_count") != 0:
                raise ValueError("ENGINEER_REVIEWED requires all assumptions to be reviewed")
        elif readiness == ReadinessLevel.REFERENCE_MATCHED:
            comparison = gate_evidence.get("reference_comparison", {})
            if comparison.get("matched") is not True:
                raise ValueError("REFERENCE_MATCHED requires a successful comparison")
            if not comparison.get("comparison_id") or not comparison.get("metrics"):
                raise ValueError("reference comparison ID and metric evidence are required")
        state = project["state"]
        state["readiness_evidence"] = gate_evidence
        with self.transaction() as connection:
            connection.execute(
                "UPDATE projects SET readiness=?,state_json=?,updated_at=? WHERE project_id=?",
                (readiness.value, _json(state), _now(), project_id),
            )
        return self.get_project(project_id)

    def create_analysis(
        self,
        project_id: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        analysis_id: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key:
            existing = self._connection.execute(
                "SELECT analysis_id FROM analyses WHERE project_id=? AND idempotency_key=?",
                (project_id, idempotency_key),
            ).fetchone()
            if existing:
                return self.get_analysis(existing["analysis_id"])
        analysis_id = analysis_id or new_id("analysis")
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO analyses(analysis_id,project_id,state,idempotency_key,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    analysis_id,
                    project_id,
                    AnalysisState.DRAFT.value,
                    idempotency_key,
                    _json(payload or {}),
                    now,
                    now,
                ),
            )
        return self.get_analysis(analysis_id)

    def get_analysis(self, analysis_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM analyses WHERE analysis_id=?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"analysis {analysis_id!r} was not found")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def transition_analysis(
        self,
        analysis_id: str,
        target: AnalysisState | str,
    ) -> dict[str, Any]:
        target = AnalysisState(target)
        current = AnalysisState(self.get_analysis(analysis_id)["state"])
        if target not in ANALYSIS_TRANSITIONS[current]:
            raise ValueError(f"invalid analysis transition {current.value} -> {target.value}")
        with self.transaction() as connection:
            connection.execute(
                "UPDATE analyses SET state=?,updated_at=? WHERE analysis_id=? AND state=?",
                (target.value, _now(), analysis_id, current.value),
            )
        return self.get_analysis(analysis_id)

    def add_designs(self, analysis_id: str, designs: list[dict[str, Any]]) -> None:
        with self.transaction() as connection:
            for design in designs:
                connection.execute(
                    "INSERT OR IGNORE INTO designs(design_id,analysis_id,vector_json,source,sequence) "
                    "VALUES(?,?,?,?,?)",
                    (
                        design["design_id"],
                        analysis_id,
                        _json(design["vector"]),
                        design.get("source", "unknown"),
                        int(design.get("sequence", 0)),
                    ),
                )

    def create_solver_job(
        self,
        *,
        analysis_id: str,
        design_id: str,
        storm_id: str,
        physics_cache_key: str,
        payload: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        existing = self._connection.execute(
            "SELECT * FROM solver_jobs WHERE analysis_id=? AND design_id=? AND storm_id=?",
            (analysis_id, design_id, storm_id),
        ).fetchone()
        if existing:
            return _job_dict(existing)
        job_id = job_id or new_id("job")
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO solver_jobs(job_id,analysis_id,design_id,storm_id,state,physics_cache_key,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    analysis_id,
                    design_id,
                    storm_id,
                    SolverJobState.PLANNED.value,
                    physics_cache_key,
                    _json(payload or {}),
                    now,
                    now,
                ),
            )
        return self.get_solver_job(job_id)

    def get_solver_job(self, job_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM solver_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"solver job {job_id!r} was not found")
        return _job_dict(row)

    def transition_solver_job(
        self,
        job_id: str,
        target: SolverJobState | str,
        *,
        payload_update: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = self.get_solver_job(job_id)
        current = SolverJobState(job["state"])
        target = SolverJobState(target)
        if target not in JOB_TRANSITIONS[current]:
            raise ValueError(f"invalid solver-job transition {current.value} -> {target.value}")
        payload = job["payload"]
        payload.update(payload_update or {})
        attempt_increment = 1 if target == SolverJobState.RUNNING else 0
        with self.transaction() as connection:
            connection.execute(
                "UPDATE solver_jobs SET state=?,payload_json=?,attempt=attempt+?,updated_at=? "
                "WHERE job_id=? AND state=?",
                (target.value, _json(payload), attempt_increment, _now(), job_id, current.value),
            )
        return self.get_solver_job(job_id)

    def recover_abandoned_jobs(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT job_id FROM solver_jobs WHERE state='RUNNING' ORDER BY job_id"
        ).fetchall()
        recovered = []
        for row in rows:
            self.transition_solver_job(
                row["job_id"],
                SolverJobState.QUEUED,
                payload_update={"recovered_at": _now()},
            )
            recovered.append(row["job_id"])
        return recovered

    def store_blob(self, content: bytes) -> tuple[str, str]:
        sha256 = hashlib.sha256(content).hexdigest()
        destination = self.root / "blobs" / "sha256" / sha256
        if not destination.exists():
            fd, temporary_name = tempfile.mkstemp(prefix=".blob-", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary_name, destination)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return sha256, str(destination.relative_to(self.root))

    def add_run_artifact(
        self,
        job_id: str,
        role: str,
        content: bytes,
    ) -> dict[str, Any]:
        sha256, relative_path = self.store_blob(content)
        artifact_id = new_id("artifact")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO run_artifacts(artifact_id,job_id,role,sha256,relative_path,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (artifact_id, job_id, role, sha256, relative_path, _now()),
            )
        return {
            "artifact_id": artifact_id,
            "job_id": job_id,
            "role": role,
            "sha256": sha256,
            "relative_path": relative_path,
        }

    def add_run_metric(
        self,
        job_id: str,
        metric_id: str,
        value: float,
        units: str,
        *,
        object_id: str = "",
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO run_metrics(job_id,metric_id,object_id,value,units) VALUES(?,?,?,?,?)",
                (job_id, metric_id, object_id, value, units),
            )

    def table_counts(self) -> dict[str, int]:
        tables = [
            "projects",
            "analyses",
            "designs",
            "solver_jobs",
            "run_artifacts",
            "run_metrics",
            "exports",
        ]
        return {
            table: int(self._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result
