"""Durable SQLite project repository and explicit state machines."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import (
    AnalysisState,
    AssistantAnswer,
    AssistantConversation,
    AssistantEvaluationRecord,
    AssistantTurn,
    DecisionBrief,
    ExportManifest,
    ExternalReference,
    GuidedCheckpoint,
    ModelFitnessPassport,
    ModelRevision,
    ObservabilityBinding,
    ObservabilityProvider,
    ProjectDataPolicy,
    ProjectKind,
    ProviderPolicy,
    SolverJobState,
    TelemetryEnvelope,
    TelemetryEvent,
    TelemetryState,
    WandbRunReference,
    WeaveConversationReference,
    WeaveTraceReference,
    new_id,
)


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
    project_kind TEXT NOT NULL CHECK(project_kind IN ('demo','conceptual','existing')),
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
CREATE INDEX IF NOT EXISTS solver_jobs_physics_cache
    ON solver_jobs(physics_cache_key, state);
CREATE TABLE IF NOT EXISTS physics_cache_entries (
    physics_cache_key TEXT PRIMARY KEY,
    source_job_id TEXT NOT NULL UNIQUE REFERENCES solver_jobs(job_id),
    created_at TEXT NOT NULL
);
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


MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS project_data_policies (
    policy_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL,
    UNIQUE(project_id)
);
CREATE TABLE IF NOT EXISTS observability_bindings (
    binding_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    data_policy_id TEXT NOT NULL REFERENCES project_data_policies(policy_id),
    entity TEXT NOT NULL,
    external_project TEXT NOT NULL,
    external_project_id TEXT NOT NULL,
    access TEXT NOT NULL CHECK(access = 'Restricted'),
    deployment TEXT NOT NULL CHECK(deployment IN ('wandb_cloud','dedicated_cloud','self_managed')),
    mode TEXT NOT NULL CHECK(mode IN ('online','offline_queue')),
    verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
    first_upload_at TEXT,
    previous_binding_id TEXT REFERENCES observability_bindings(binding_id),
    verified_at TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(entity, external_project_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_binding_per_project
    ON observability_bindings(project_id) WHERE active = 1;
CREATE TABLE IF NOT EXISTS telemetry_outbox (
    outbox_id TEXT PRIMARY KEY,
    envelope_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    binding_id TEXT NOT NULL REFERENCES observability_bindings(binding_id),
    category TEXT NOT NULL CHECK(category IN ('engineering_evidence','benchmark_run','assistant_trace','assistant_evaluation')),
    event_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','uploading','synced','failed','blocked')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    last_error_code TEXT,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS telemetry_outbox_project_state
    ON telemetry_outbox(project_id, state, created_at);
CREATE TABLE IF NOT EXISTS cloud_artifacts (
    artifact_id TEXT PRIMARY KEY,
    envelope_id TEXT NOT NULL REFERENCES telemetry_outbox(envelope_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    logical_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK(bytes >= 0),
    media_type TEXT NOT NULL,
    source_object_id TEXT NOT NULL,
    local_blob_sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(envelope_id, artifact_id)
);
CREATE TABLE IF NOT EXISTS wandb_run_references (
    reference_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    envelope_id TEXT NOT NULL REFERENCES telemetry_outbox(envelope_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(envelope_id),
    UNIQUE(run_id)
);
CREATE TABLE IF NOT EXISTS weave_trace_references (
    reference_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, call_id),
    UNIQUE(trace_id)
);
CREATE TABLE IF NOT EXISTS assistant_answers (
    answer_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, call_id)
);
CREATE TABLE IF NOT EXISTS assistant_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    answer_id TEXT NOT NULL REFERENCES assistant_answers(answer_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_revisions (
    revision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    parent_revision_id TEXT REFERENCES model_revisions(revision_id),
    observability_binding_id TEXT NOT NULL REFERENCES observability_bindings(binding_id),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_fitness_passports (
    passport_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    model_revision_id TEXT NOT NULL REFERENCES model_revisions(revision_id),
    payload_json TEXT NOT NULL,
    issued_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision_briefs (
    brief_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    model_revision_id TEXT NOT NULL REFERENCES model_revisions(revision_id),
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guided_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, step_id)
);
"""


MIGRATION_3 = """
CREATE TABLE model_revisions_v3 (
    revision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    parent_revision_id TEXT REFERENCES model_revisions(revision_id),
    observability_binding_id TEXT REFERENCES observability_bindings(binding_id),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO model_revisions_v3(
    revision_id,project_id,parent_revision_id,observability_binding_id,payload_json,created_at
)
SELECT revision_id,project_id,parent_revision_id,observability_binding_id,payload_json,created_at
FROM model_revisions;
DROP TABLE model_revisions;
ALTER TABLE model_revisions_v3 RENAME TO model_revisions;
"""


MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS assistant_provider_policies (
    policy_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_conversations (
    conversation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    model_revision_id TEXT REFERENCES model_revisions(revision_id),
    provider_policy_id TEXT NOT NULL REFERENCES assistant_provider_policies(policy_id),
    status TEXT NOT NULL CHECK(status IN ('active','closed')),
    next_turn_sequence INTEGER NOT NULL DEFAULT 0 CHECK(next_turn_sequence >= 0),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assistant_conversations_project_status
    ON assistant_conversations(project_id, status, updated_at);
CREATE TABLE IF NOT EXISTS assistant_turns (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES assistant_conversations(conversation_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    state TEXT NOT NULL CHECK(state IN ('pending','completed','blocked','failed')),
    answer_id TEXT REFERENCES assistant_answers(answer_id) ON DELETE SET NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(conversation_id, sequence)
);
CREATE INDEX IF NOT EXISTS assistant_turns_project_created
    ON assistant_turns(project_id, created_at);
CREATE TABLE IF NOT EXISTS weave_conversation_references (
    reference_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES assistant_conversations(conversation_id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES assistant_turns(turn_id) ON DELETE CASCADE,
    trace_id TEXT NOT NULL,
    root_span_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, turn_id),
    UNIQUE(trace_id),
    UNIQUE(root_span_id)
);
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
        # A live Evidence Guide turn is bounded to 60 seconds. Any turn that is
        # still pending after twice that budget belongs to a worker that exited
        # before it could persist a terminal state. Recover it on the next
        # project open without disturbing an in-flight turn in another RPC.
        self.recover_abandoned_assistant_turns(older_than_seconds=120)

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
            version = 1
        if version < 2:
            self._connection.executescript(MIGRATION_2)
            self._connection.execute("PRAGMA user_version=2")
            version = 2
        if version < 3:
            # SQLite cannot remove a NOT NULL constraint in place. Rebuild the
            # revision table while foreign-key enforcement is paused. Execute
            # each statement inside our own transaction so a failed rebuild or
            # graph audit cannot leave a committed, half-migrated version 3 DB.
            self._connection.execute("PRAGMA foreign_keys=OFF")
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for statement in MIGRATION_3.split(";"):
                    if statement.strip():
                        self._connection.execute(statement)
                violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError("schema migration found a foreign-key violation")
                self._connection.execute("PRAGMA user_version=3")
                self._connection.execute("COMMIT")
                version = 3
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            finally:
                self._connection.execute("PRAGMA foreign_keys=ON")
        if version < 4:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in MIGRATION_4.split(";"):
                    if statement.strip():
                        self._connection.execute(statement)
                violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError("schema migration found a foreign-key violation")
                self._connection.execute("PRAGMA user_version=4")
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("project database contains a foreign-key violation")

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
        project_kind: ProjectKind | str,
        project_id: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a project kind without inferring any model-fitness disposition."""

        project_id = project_id or new_id("project")
        project_kind = ProjectKind(project_kind)
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO projects(project_id,title,project_kind,state_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (project_id, title, project_kind.value, _json(state or {}), now, now),
            )
        return self.get_project(project_id)

    def create_project_with_observability(
        self,
        *,
        title: str,
        project_kind: ProjectKind | str,
        policy: ProjectDataPolicy | dict[str, Any],
        binding: ObservabilityBinding | dict[str, Any],
        project_id: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Atomically create a project and its verified Restricted W&B binding.

        Contract validation happens before the write transaction. The three
        database rows are then inserted in foreign-key order within one
        ``BEGIN IMMEDIATE`` transaction, so any constraint or I/O failure rolls
        back the project, its data policy, and its observability binding.
        """

        validated_policy = ProjectDataPolicy.model_validate(policy)
        validated_binding = ObservabilityBinding.model_validate(binding)
        resolved_project_id = project_id or validated_policy.project_id
        normalized_kind = ProjectKind(project_kind)
        policy_payload = validated_policy.model_dump(mode="json")
        binding_payload = validated_binding.model_dump(mode="json")
        _assert_no_secret_material(policy_payload, context="project data policy")
        _assert_no_secret_material(binding_payload, context="observability binding")

        if validated_policy.project_id != resolved_project_id:
            raise ValueError("project data policy must name the project being created")
        if validated_binding.project_id != resolved_project_id:
            raise ValueError("observability binding must name the project being created")
        if validated_binding.data_policy_id != validated_policy.policy_id:
            raise ValueError("observability binding must name the new project data policy")
        if validated_binding.deployment != validated_policy.deployment:
            raise ValueError("W&B deployment must match the project data policy")
        if not validated_binding.verified:
            raise ValueError("project creation requires a verified Restricted W&B binding")
        if validated_binding.first_upload_at is not None:
            raise ValueError("a new project binding cannot already have a first upload")
        if validated_binding.previous_binding_id is not None:
            raise ValueError("a new project binding cannot reference a previous binding")

        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO projects(project_id,title,project_kind,state_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    resolved_project_id,
                    title,
                    normalized_kind.value,
                    _json(state or {}),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO project_data_policies(policy_id,project_id,payload_json,acknowledged_at) "
                "VALUES(?,?,?,?)",
                (
                    policy_payload["policy_id"],
                    resolved_project_id,
                    _json(policy_payload),
                    policy_payload["acknowledged_at"],
                ),
            )
            connection.execute(
                "INSERT INTO observability_bindings("
                "binding_id,project_id,data_policy_id,entity,external_project,external_project_id,"
                "access,deployment,mode,verified,first_upload_at,previous_binding_id,verified_at,"
                "active,payload_json,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    binding_payload["binding_id"],
                    resolved_project_id,
                    binding_payload["data_policy_id"],
                    binding_payload["entity"],
                    binding_payload["external_project"],
                    binding_payload["external_project_id"],
                    binding_payload["access"],
                    binding_payload["deployment"],
                    binding_payload["mode"],
                    1,
                    None,
                    None,
                    binding_payload["verified_at"],
                    1,
                    _json(binding_payload),
                    binding_payload["updated_at"],
                    binding_payload["updated_at"],
                ),
            )

        return {
            "project": self.get_project(resolved_project_id),
            "policy": self.get_project_data_policy(resolved_project_id),
            "binding": self.get_observability_binding(resolved_project_id),
        }

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

    def record_project_asset(
        self,
        project_id: str,
        *,
        role: str,
        relative_path: str,
        sha256: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register a project-contained immutable source or evidence file."""

        self.get_project(project_id)
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("project asset requires a lowercase SHA-256")
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("project asset path must be a safe relative path")
        absolute = _safe_project_child(self.root, path.as_posix())
        if not absolute.is_file():
            raise ValueError("project asset file was not found")
        if hashlib.sha256(absolute.read_bytes()).hexdigest() != sha256:
            raise ValueError("project asset bytes do not match the declared SHA-256")
        asset_id = (
            "asset_"
            + hashlib.sha256(
                f"{project_id}|{role}|{path.as_posix()}|{sha256}".encode()
            ).hexdigest()[:20]
        )
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO project_assets"
                "(asset_id,project_id,role,path,sha256,payload_json) VALUES(?,?,?,?,?,?)",
                (
                    asset_id,
                    project_id,
                    role,
                    path.as_posix(),
                    sha256,
                    _json(payload or {}),
                ),
            )
        return {
            "asset_id": asset_id,
            "project_id": project_id,
            "role": role,
            "path": path.as_posix(),
            "sha256": sha256,
            "payload": payload or {},
        }

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

    def register_physics_cache_entry(self, job_id: str) -> dict[str, Any]:
        job = self.get_solver_job(job_id)
        if job["state"] != SolverJobState.SUCCEEDED.value:
            raise ValueError("only a succeeded exact job can populate the physics cache")
        artifact_roles = {
            row["role"]
            for row in self._connection.execute(
                "SELECT role FROM run_artifacts WHERE job_id=?", (job_id,)
            ).fetchall()
        }
        metric_count = self._connection.execute(
            "SELECT count(*) FROM run_metrics WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        if artifact_roles != {"input", "report", "output"} or metric_count < 1:
            raise ValueError("a physics cache entry requires complete exact evidence")
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO physics_cache_entries"
                "(physics_cache_key,source_job_id,created_at) VALUES(?,?,?)",
                (job["physics_cache_key"], job_id, _now()),
            )
        row = self._connection.execute(
            "SELECT * FROM physics_cache_entries WHERE physics_cache_key=?",
            (job["physics_cache_key"],),
        ).fetchone()
        return dict(row)

    def find_physics_cache_entry(
        self,
        physics_cache_key: str,
        *,
        exclude_job_id: str | None = None,
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT c.physics_cache_key,c.source_job_id,c.created_at "
            "FROM physics_cache_entries c JOIN solver_jobs j ON j.job_id=c.source_job_id "
            "WHERE c.physics_cache_key=? AND j.state='SUCCEEDED'",
            (physics_cache_key,),
        ).fetchone()
        if row is None or row["source_job_id"] == exclude_job_id:
            return None
        return dict(row)

    def complete_solver_job_from_cache(
        self,
        job_id: str,
        source_job_id: str,
    ) -> dict[str, Any]:
        target = self.get_solver_job(job_id)
        source = self.get_solver_job(source_job_id)
        if target["state"] != SolverJobState.QUEUED.value:
            raise ValueError("only a queued solver job can resolve from cache")
        if source["state"] != SolverJobState.SUCCEEDED.value:
            raise ValueError("physics cache source is not a succeeded exact job")
        if target["physics_cache_key"] != source["physics_cache_key"]:
            raise ValueError("physics cache source has a different cache key")
        artifacts = self._connection.execute(
            "SELECT role,sha256,relative_path FROM run_artifacts WHERE job_id=? ORDER BY role",
            (source_job_id,),
        ).fetchall()
        metrics = self._connection.execute(
            "SELECT metric_id,object_id,value,units FROM run_metrics WHERE job_id=?",
            (source_job_id,),
        ).fetchall()
        findings = self._connection.execute(
            "SELECT severity,code,payload_json FROM qa_findings WHERE job_id=?",
            (source_job_id,),
        ).fetchall()
        if {row["role"] for row in artifacts} != {"input", "report", "output"} or not metrics:
            raise ValueError("physics cache source is missing exact evidence")
        payload = dict(target["payload"])
        payload.update(
            {
                key: value
                for key, value in source["payload"].items()
                if key
                in {
                    "return_code",
                    "input_sha256",
                    "solver_sha256",
                    "report_sha256",
                    "output_sha256",
                    "qa_status",
                }
            }
        )
        payload.update({"cache_hit": True, "cache_source_job_id": source_job_id})
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE solver_jobs SET state='SUCCEEDED',payload_json=?,updated_at=? "
                "WHERE job_id=? AND state='QUEUED'",
                (_json(payload), now, job_id),
            )
            for artifact in artifacts:
                artifact_id = (
                    "artifact_"
                    + hashlib.sha256(
                        f"{job_id}|{artifact['role']}|{artifact['sha256']}".encode()
                    ).hexdigest()[:20]
                )
                connection.execute(
                    "INSERT INTO run_artifacts"
                    "(artifact_id,job_id,role,sha256,relative_path,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        artifact_id,
                        job_id,
                        artifact["role"],
                        artifact["sha256"],
                        artifact["relative_path"],
                        now,
                    ),
                )
            for metric in metrics:
                connection.execute(
                    "INSERT INTO run_metrics(job_id,metric_id,object_id,value,units) "
                    "VALUES(?,?,?,?,?)",
                    (
                        job_id,
                        metric["metric_id"],
                        metric["object_id"],
                        metric["value"],
                        metric["units"],
                    ),
                )
            for finding in findings:
                connection.execute(
                    "INSERT INTO qa_findings(job_id,severity,code,payload_json) VALUES(?,?,?,?)",
                    (
                        job_id,
                        finding["severity"],
                        finding["code"],
                        finding["payload_json"],
                    ),
                )
        return self.get_solver_job(job_id)

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

    def upsert_observability_binding(
        self,
        binding: ObservabilityBinding | dict[str, Any],
    ) -> dict[str, Any]:
        """Persist non-secret provider coordinates and the user's selected mode."""

        validated = ObservabilityBinding.model_validate(binding)
        payload = validated.model_dump(mode="json")
        _assert_no_secret_material(payload["settings"], context="observability settings")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO observability_bindings("
                "binding_id,project_id,provider,mode,entity,external_project,settings_json,enabled,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id,provider) DO UPDATE SET "
                "mode=excluded.mode,entity=excluded.entity,external_project=excluded.external_project,"
                "settings_json=excluded.settings_json,enabled=excluded.enabled,updated_at=excluded.updated_at",
                (
                    payload["binding_id"],
                    payload["project_id"],
                    payload["provider"],
                    payload["mode"],
                    payload["entity"],
                    payload["external_project"],
                    _json(payload["settings"]),
                    int(payload["enabled"]),
                    payload["updated_at"],
                ),
            )
        return self._v1_get_observability_binding(payload["project_id"], payload["provider"])

    def _v1_get_observability_binding(
        self,
        project_id: str,
        provider: ObservabilityProvider | str,
    ) -> dict[str, Any]:
        provider_value = ObservabilityProvider(provider).value
        row = self._connection.execute(
            "SELECT * FROM observability_bindings WHERE project_id=? AND provider=?",
            (project_id, provider_value),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"observability binding for project {project_id!r} and provider "
                f"{provider_value!r} was not found"
            )
        return _binding_dict(row)

    def _v1_list_observability_bindings(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM observability_bindings WHERE project_id=? ORDER BY provider",
            (project_id,),
        ).fetchall()
        return [_binding_dict(row) for row in rows]

    def enqueue_telemetry_event(
        self,
        event: TelemetryEvent | dict[str, Any],
    ) -> dict[str, Any]:
        """Add an immutable, idempotent export intent to the local outbox."""

        validated = TelemetryEvent.model_validate(event)
        payload = validated.model_dump(mode="json")
        _assert_no_secret_material(payload["payload"], context="telemetry payload")
        payload_json = _json(payload["payload"])
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        existing = self._connection.execute(
            "SELECT event_id,payload_sha256 FROM telemetry_outbox "
            "WHERE project_id=? AND provider=? AND idempotency_key=?",
            (payload["project_id"], payload["provider"], payload["idempotency_key"]),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != payload_sha256:
                raise ValueError("telemetry idempotency key was reused with a different payload")
            return self.get_telemetry_event(existing["event_id"])
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO telemetry_outbox("
                "event_id,project_id,analysis_id,provider,event_type,data_classification,"
                "redistributable,payload_json,payload_sha256,idempotency_key,state,"
                "attempt_count,last_error,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["event_id"],
                    payload["project_id"],
                    payload["analysis_id"],
                    payload["provider"],
                    payload["event_type"],
                    payload["data_classification"],
                    int(payload["redistributable"]),
                    payload_json,
                    payload_sha256,
                    payload["idempotency_key"],
                    TelemetryState.PENDING.value,
                    0,
                    None,
                    payload.get("created_at") or now,
                    now,
                ),
            )
        return self.get_telemetry_event(payload["event_id"])

    def get_telemetry_event(self, event_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM telemetry_outbox WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"telemetry event {event_id!r} was not found")
        return _telemetry_dict(row)

    def list_telemetry_events(
        self,
        project_id: str,
        *,
        provider: ObservabilityProvider | str | None = None,
        states: list[TelemetryState | str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id=?"]
        values: list[Any] = [project_id]
        if provider is not None:
            clauses.append("provider=?")
            values.append(ObservabilityProvider(provider).value)
        if states:
            normalized_states = [TelemetryState(item).value for item in states]
            clauses.append(f"state IN ({','.join('?' for _ in normalized_states)})")
            values.extend(normalized_states)
        rows = self._connection.execute(
            "SELECT * FROM telemetry_outbox WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at,event_id",
            tuple(values),
        ).fetchall()
        return [_telemetry_dict(row) for row in rows]

    def claim_telemetry_event(self, event_id: str) -> dict[str, Any]:
        """Atomically claim one pending/retryable export for a delivery attempt."""

        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM telemetry_outbox WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"telemetry event {event_id!r} was not found")
            if row["state"] not in {
                TelemetryState.PENDING.value,
                TelemetryState.FAILED.value,
                TelemetryState.STAGED.value,
            }:
                raise ValueError(
                    f"telemetry event {event_id!r} cannot be claimed from {row['state']!r}"
                )
            connection.execute(
                "UPDATE telemetry_outbox SET state=?,attempt_count=attempt_count+1,"
                "last_error=NULL,updated_at=? WHERE event_id=?",
                (TelemetryState.SYNCING.value, _now(), event_id),
            )
        return self.get_telemetry_event(event_id)

    def mark_telemetry_event(
        self,
        event_id: str,
        state: TelemetryState | str,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        target = TelemetryState(state)
        if target not in {
            TelemetryState.STAGED,
            TelemetryState.SYNCED,
            TelemetryState.FAILED,
            TelemetryState.BLOCKED,
        }:
            raise ValueError("delivery may finish only as staged, synced, failed, or blocked")
        current = self.get_telemetry_event(event_id)
        if current["state"] == TelemetryState.SYNCED.value:
            raise ValueError("a synced telemetry event is immutable")
        if target == TelemetryState.FAILED and not error:
            raise ValueError("failed telemetry events require a safe error message")
        persisted_error = _sanitize_persisted_error(error, project_root=self.root)
        with self.transaction() as connection:
            connection.execute(
                "UPDATE telemetry_outbox SET state=?,last_error=?,updated_at=? WHERE event_id=?",
                (target.value, persisted_error, _now(), event_id),
            )
        return self.get_telemetry_event(event_id)

    def retry_telemetry_event(self, event_id: str) -> dict[str, Any]:
        current = self.get_telemetry_event(event_id)
        if current["state"] not in {
            TelemetryState.FAILED.value,
            TelemetryState.STAGED.value,
            TelemetryState.BLOCKED.value,
        }:
            raise ValueError(
                f"telemetry event {event_id!r} cannot be retried from {current['state']!r}"
            )
        with self.transaction() as connection:
            connection.execute(
                "UPDATE telemetry_outbox SET state=?,last_error=NULL,updated_at=? WHERE event_id=?",
                (TelemetryState.PENDING.value, _now(), event_id),
            )
        return self.get_telemetry_event(event_id)

    def add_external_reference(
        self,
        reference: ExternalReference | dict[str, Any],
    ) -> dict[str, Any]:
        validated = ExternalReference.model_validate(reference)
        payload = validated.model_dump(mode="json")
        _assert_no_secret_material(payload["metadata"], context="external-reference metadata")
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO external_references("
                "reference_id,project_id,analysis_id,event_id,provider,resource_type,"
                "external_id,url,metadata_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["reference_id"],
                    payload["project_id"],
                    payload["analysis_id"],
                    payload["event_id"],
                    payload["provider"],
                    payload["resource_type"],
                    payload["external_id"],
                    payload["url"],
                    _json(payload["metadata"]),
                    payload["created_at"],
                ),
            )
        row = self._connection.execute(
            "SELECT * FROM external_references WHERE provider=? AND resource_type=? "
            "AND external_id=?",
            (payload["provider"], payload["resource_type"], payload["external_id"]),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert/select transaction contract
            raise RuntimeError("external reference was not persisted")
        return _external_reference_dict(row)

    def list_external_references(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM external_references WHERE project_id=? ORDER BY created_at,reference_id",
            (project_id,),
        ).fetchall()
        return [_external_reference_dict(row) for row in rows]

    def _v1_observability_status(self, project_id: str) -> dict[str, Any]:
        bindings = self._v1_list_observability_bindings(project_id)
        count_rows = self._connection.execute(
            "SELECT state,count(*) AS count FROM telemetry_outbox "
            "WHERE project_id=? GROUP BY state ORDER BY state",
            (project_id,),
        ).fetchall()
        last_attempt = self._connection.execute(
            "SELECT max(updated_at) FROM telemetry_outbox WHERE project_id=? AND attempt_count>0",
            (project_id,),
        ).fetchone()[0]
        external_count = int(
            self._connection.execute(
                "SELECT count(*) FROM external_references WHERE project_id=?", (project_id,)
            ).fetchone()[0]
        )
        return {
            "configured": any(item["enabled"] for item in bindings),
            "bindings": bindings,
            "outbox_counts": {row["state"]: int(row["count"]) for row in count_rows},
            "external_reference_count": external_count,
            "last_attempt_at": last_attempt,
        }

    # Schema-v2 cloud evidence methods.  The older telemetry-shaped methods
    # above remain readable for source compatibility during migration, but
    # these definitions are authoritative for the v2 worker.

    def upsert_project_data_policy(
        self,
        policy: ProjectDataPolicy | dict[str, Any],
    ) -> dict[str, Any]:
        validated = ProjectDataPolicy.model_validate(policy)
        payload = validated.model_dump(mode="json")
        _assert_no_secret_material(payload, context="project data policy")
        existing_binding = self._connection.execute(
            "SELECT first_upload_at FROM observability_bindings WHERE project_id=? AND active=1",
            (payload["project_id"],),
        ).fetchone()
        existing_policy = self._connection.execute(
            "SELECT payload_json FROM project_data_policies WHERE project_id=?",
            (payload["project_id"],),
        ).fetchone()
        if (
            existing_binding is not None
            and existing_binding["first_upload_at"]
            and existing_policy
            and json.loads(existing_policy["payload_json"]) != payload
        ):
            raise ValueError("project data policy is immutable after the first cloud upload")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO project_data_policies(policy_id,project_id,payload_json,acknowledged_at) "
                "VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                "payload_json=excluded.payload_json,acknowledged_at=excluded.acknowledged_at",
                (
                    payload["policy_id"],
                    payload["project_id"],
                    _json(payload),
                    payload["acknowledged_at"],
                ),
            )
        return self.get_project_data_policy(payload["project_id"])

    def get_project_data_policy(self, project_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM project_data_policies WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"project data policy for {project_id!r} was not found")
        return json.loads(row["payload_json"])

    def connect_observability_binding(
        self,
        binding: ObservabilityBinding | dict[str, Any],
    ) -> dict[str, Any]:
        validated = ObservabilityBinding.model_validate(binding)
        payload = validated.model_dump(mode="json")
        if not validated.verified:
            raise ValueError("only a verified Restricted W&B binding may be persisted")
        policy = ProjectDataPolicy.model_validate(
            self.get_project_data_policy(validated.project_id)
        )
        if policy.policy_id != validated.data_policy_id:
            raise ValueError("observability binding must name the project's data policy")
        if policy.deployment != validated.deployment:
            raise ValueError("W&B deployment must match the project data policy")
        current = self._connection.execute(
            "SELECT * FROM observability_bindings WHERE project_id=? AND active=1",
            (validated.project_id,),
        ).fetchone()
        if current is not None:
            same_destination = (
                current["entity"] == validated.entity
                and current["external_project_id"] == validated.external_project_id
            )
            if current["first_upload_at"] and not same_destination:
                raise ValueError(
                    "binding destination is immutable after first upload; create a rebind revision"
                )
            if not same_destination:
                raise ValueError("use rebind_observability_binding to change W&B destination")
            payload["binding_id"] = current["binding_id"]
            payload["first_upload_at"] = current["first_upload_at"]
            with self.transaction() as connection:
                connection.execute(
                    "UPDATE observability_bindings SET mode=?,verified=1,verified_at=?,"
                    "payload_json=?,updated_at=? WHERE binding_id=?",
                    (
                        payload["mode"],
                        payload["verified_at"],
                        _json(payload),
                        payload["updated_at"],
                        current["binding_id"],
                    ),
                )
            return self.get_observability_binding(validated.project_id)

        previous = self._connection.execute(
            "SELECT * FROM observability_bindings WHERE project_id=? ORDER BY created_at DESC",
            (validated.project_id,),
        ).fetchall()
        same_destination = next(
            (
                row
                for row in previous
                if row["entity"] == validated.entity
                and row["external_project_id"] == validated.external_project_id
            ),
            None,
        )
        if same_destination is not None:
            if same_destination["data_policy_id"] != validated.data_policy_id:
                raise ValueError("reactivated binding must retain the project's data policy")
            payload["binding_id"] = same_destination["binding_id"]
            payload["first_upload_at"] = same_destination["first_upload_at"]
            payload["previous_binding_id"] = same_destination["previous_binding_id"]
            with self.transaction() as connection:
                connection.execute(
                    "UPDATE observability_bindings SET mode=?,verified=1,verified_at=?,"
                    "active=1,payload_json=?,updated_at=? WHERE binding_id=?",
                    (
                        payload["mode"],
                        payload["verified_at"],
                        _json(payload),
                        payload["updated_at"],
                        same_destination["binding_id"],
                    ),
                )
                connection.execute(
                    "UPDATE telemetry_outbox SET state='pending',last_error_code=NULL,"
                    "next_attempt_at=NULL,updated_at=? WHERE project_id=? AND binding_id=? "
                    "AND state='blocked' AND last_error_code='LOCAL_MODE_ENABLED'",
                    (
                        _now(),
                        validated.project_id,
                        same_destination["binding_id"],
                    ),
                )
            return self.get_observability_binding(validated.project_id)

        if any(row["first_upload_at"] for row in previous):
            raise ValueError(
                "binding destination is immutable after first upload; create a rebind revision"
            )
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO observability_bindings("
                "binding_id,project_id,data_policy_id,entity,external_project,external_project_id,"
                "access,deployment,mode,verified,first_upload_at,previous_binding_id,verified_at,"
                "active,payload_json,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["binding_id"],
                    payload["project_id"],
                    payload["data_policy_id"],
                    payload["entity"],
                    payload["external_project"],
                    payload["external_project_id"],
                    payload["access"],
                    payload["deployment"],
                    payload["mode"],
                    1,
                    payload["first_upload_at"],
                    payload["previous_binding_id"],
                    payload["verified_at"],
                    1,
                    _json(payload),
                    payload["updated_at"],
                    payload["updated_at"],
                ),
            )
        return self.get_observability_binding(validated.project_id)

    def deactivate_observability_binding(self, project_id: str) -> dict[str, Any] | None:
        """Stop new project cloud work while preserving auditable binding history."""

        self.get_project(project_id)
        current = self._connection.execute(
            "SELECT * FROM observability_bindings WHERE project_id=? AND active=1",
            (project_id,),
        ).fetchone()
        if current is None:
            return None
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE telemetry_outbox SET state='blocked',"
                "last_error_code='LOCAL_MODE_ENABLED',next_attempt_at=NULL,updated_at=? "
                "WHERE project_id=? AND state!='synced'",
                (now, project_id),
            )
            connection.execute(
                "UPDATE observability_bindings SET active=0,updated_at=? WHERE binding_id=?",
                (now, current["binding_id"]),
            )
        payload = json.loads(current["payload_json"])
        payload.update(
            {
                "binding_id": current["binding_id"],
                "active": False,
                "first_upload_at": current["first_upload_at"],
            }
        )
        return payload

    def get_observability_binding(self, project_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM observability_bindings WHERE project_id=? AND active=1",
            (project_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"active observability binding for {project_id!r} was not found")
        payload = json.loads(row["payload_json"])
        payload["binding_id"] = row["binding_id"]
        payload["mode"] = row["mode"]
        payload["first_upload_at"] = row["first_upload_at"]
        payload["verified"] = bool(row["verified"])
        payload["verified_at"] = row["verified_at"]
        return payload

    def list_observability_bindings(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload_json,binding_id,active,first_upload_at FROM observability_bindings "
            "WHERE project_id=? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        results = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["binding_id"] = row["binding_id"]
            payload["active"] = bool(row["active"])
            payload["first_upload_at"] = row["first_upload_at"]
            results.append(payload)
        return results

    def rebind_observability_binding(
        self,
        binding: ObservabilityBinding | dict[str, Any],
        revision: ModelRevision | dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        validated = ObservabilityBinding.model_validate(binding)
        validated_revision = ModelRevision.model_validate(revision)
        current = self.get_observability_binding(validated.project_id)
        if not current.get("first_upload_at"):
            raise ValueError("a pre-upload destination can be replaced without a lineage rebind")
        if validated.previous_binding_id != current["binding_id"]:
            raise ValueError("rebind must reference the currently active binding")
        if validated_revision.reason != "rebind":
            raise ValueError("observability rebind requires a rebind model revision")
        if validated_revision.observability_binding_id != validated.binding_id:
            raise ValueError("rebind revision must identify the new binding")
        policy = ProjectDataPolicy.model_validate(
            self.get_project_data_policy(validated.project_id)
        )
        if (
            validated.data_policy_id != policy.policy_id
            or validated.deployment != policy.deployment
        ):
            raise ValueError("rebind destination conflicts with the project data policy")
        binding_payload = validated.model_dump(mode="json")
        revision_payload = validated_revision.model_dump(mode="json")
        with self.transaction() as connection:
            connection.execute(
                "UPDATE observability_bindings SET active=0,updated_at=? WHERE binding_id=?",
                (_now(), current["binding_id"]),
            )
            connection.execute(
                "INSERT INTO observability_bindings("
                "binding_id,project_id,data_policy_id,entity,external_project,external_project_id,"
                "access,deployment,mode,verified,first_upload_at,previous_binding_id,verified_at,"
                "active,payload_json,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    binding_payload["binding_id"],
                    binding_payload["project_id"],
                    binding_payload["data_policy_id"],
                    binding_payload["entity"],
                    binding_payload["external_project"],
                    binding_payload["external_project_id"],
                    binding_payload["access"],
                    binding_payload["deployment"],
                    binding_payload["mode"],
                    int(binding_payload["verified"]),
                    None,
                    binding_payload["previous_binding_id"],
                    binding_payload["verified_at"],
                    1,
                    _json(binding_payload),
                    binding_payload["updated_at"],
                    binding_payload["updated_at"],
                ),
            )
            connection.execute(
                "INSERT INTO model_revisions(revision_id,project_id,parent_revision_id,"
                "observability_binding_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    revision_payload["revision_id"],
                    revision_payload["project_id"],
                    revision_payload["parent_revision_id"],
                    revision_payload["observability_binding_id"],
                    _json(revision_payload),
                    revision_payload["created_at"],
                ),
            )
        return self.get_observability_binding(validated.project_id), revision_payload

    def enqueue_telemetry_envelope(
        self,
        envelope: TelemetryEnvelope | dict[str, Any],
    ) -> dict[str, Any]:
        validated = TelemetryEnvelope.model_validate(envelope)
        payload = validated.model_dump(mode="json")
        _assert_no_secret_material(payload["metadata"], context="cloud evidence metadata")
        _assert_no_absolute_paths(payload["metadata"], context="cloud evidence metadata")
        active = self.get_observability_binding(validated.project_id)
        if active["binding_id"] != validated.binding_id:
            raise ValueError("telemetry envelope must use the active W&B binding")
        policy = ProjectDataPolicy.model_validate(
            self.get_project_data_policy(validated.project_id)
        )
        for manifest in validated.artifacts:
            if manifest.project_id != validated.project_id:
                raise ValueError("cloud artifact belongs to a different project")
            if manifest.category not in policy.permitted_categories:
                raise ValueError(f"project data policy does not permit {manifest.category!r} sync")
            blob = self.root / "blobs" / "sha256" / manifest.local_blob_sha256
            if not blob.is_file():
                raise ValueError(
                    f"local content-addressed artifact {manifest.artifact_id!r} is missing"
                )
            if blob.stat().st_size != manifest.bytes or _sha256_file(blob) != manifest.sha256:
                raise ValueError(
                    f"cloud artifact {manifest.artifact_id!r} does not match local bytes"
                )
        envelope_json = _json(payload)
        payload_sha256 = hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()
        existing = self._connection.execute(
            "SELECT outbox_id,payload_sha256 FROM telemetry_outbox "
            "WHERE project_id=? AND idempotency_key=?",
            (validated.project_id, validated.idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["payload_sha256"] != payload_sha256:
                raise ValueError("cloud-sync idempotency key was reused with different evidence")
            return self.get_telemetry_outbox_item(existing["outbox_id"])
        outbox_id = new_id("outbox")
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO telemetry_outbox(outbox_id,envelope_id,project_id,analysis_id,"
                "binding_id,category,event_type,subject_id,envelope_json,payload_sha256,"
                "idempotency_key,state,attempt_count,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    outbox_id,
                    payload["envelope_id"],
                    payload["project_id"],
                    payload["analysis_id"],
                    payload["binding_id"],
                    payload["category"],
                    payload["event_type"],
                    payload["subject_id"],
                    envelope_json,
                    payload_sha256,
                    payload["idempotency_key"],
                    TelemetryState.PENDING.value,
                    0,
                    payload["created_at"],
                    now,
                ),
            )
            for manifest in validated.artifacts:
                manifest_payload = manifest.model_dump(mode="json")
                connection.execute(
                    "INSERT INTO cloud_artifacts(artifact_id,envelope_id,project_id,category,"
                    "logical_name,sha256,bytes,media_type,source_object_id,local_blob_sha256,"
                    "manifest_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        manifest.artifact_id,
                        validated.envelope_id,
                        manifest.project_id,
                        manifest.category,
                        manifest.logical_name,
                        manifest.sha256,
                        manifest.bytes,
                        manifest.media_type,
                        manifest.source_object_id,
                        manifest.local_blob_sha256,
                        _json(manifest_payload),
                        manifest_payload["created_at"],
                    ),
                )
        return self.get_telemetry_outbox_item(outbox_id)

    def get_telemetry_outbox_item(self, outbox_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM telemetry_outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"telemetry outbox item {outbox_id!r} was not found")
        return _cloud_outbox_dict(row)

    def list_telemetry_outbox(
        self,
        project_id: str,
        *,
        states: list[TelemetryState | str] | None = None,
    ) -> list[dict[str, Any]]:
        values: list[Any] = [project_id]
        predicate = "project_id=?"
        if states:
            normalized = [TelemetryState(item).value for item in states]
            predicate += f" AND state IN ({','.join('?' for _ in normalized)})"
            values.extend(normalized)
        rows = self._connection.execute(
            f"SELECT * FROM telemetry_outbox WHERE {predicate} ORDER BY created_at,outbox_id",
            tuple(values),
        ).fetchall()
        return [_cloud_outbox_dict(row) for row in rows]

    def claim_telemetry_outbox_item(self, outbox_id: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM telemetry_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"telemetry outbox item {outbox_id!r} was not found")
            if row["state"] not in {TelemetryState.PENDING.value, TelemetryState.FAILED.value}:
                raise ValueError(f"outbox item cannot upload from state {row['state']!r}")
            connection.execute(
                "UPDATE telemetry_outbox SET state=?,attempt_count=attempt_count+1,"
                "last_error_code=NULL,next_attempt_at=NULL,updated_at=? WHERE outbox_id=?",
                (TelemetryState.UPLOADING.value, _now(), outbox_id),
            )
        return self.get_telemetry_outbox_item(outbox_id)

    def mark_telemetry_outbox_item(
        self,
        outbox_id: str,
        state: TelemetryState | str,
        *,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        target = TelemetryState(state)
        if target not in {TelemetryState.SYNCED, TelemetryState.FAILED, TelemetryState.BLOCKED}:
            raise ValueError("outbox delivery may finish only as synced, failed, or blocked")
        if target in {TelemetryState.FAILED, TelemetryState.BLOCKED} and (
            not error_code or re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", error_code) is None
        ):
            raise ValueError("cloud failures persist a safe machine-readable error code only")
        now = datetime.now(UTC)
        next_attempt_at: str | None = None
        if target == TelemetryState.FAILED:
            row = self._connection.execute(
                "SELECT attempt_count FROM telemetry_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"telemetry outbox item {outbox_id!r} was not found")
            delay_seconds = min(3600, 5 * (2 ** min(int(row["attempt_count"]), 9)))
            next_attempt_at = (now + timedelta(seconds=delay_seconds)).isoformat()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE telemetry_outbox SET state=?,last_error_code=?,next_attempt_at=?,updated_at=? "
                "WHERE outbox_id=? AND state!='synced'",
                (target.value, error_code, next_attempt_at, now.isoformat(), outbox_id),
            )
        return self.get_telemetry_outbox_item(outbox_id)

    def recover_abandoned_telemetry_uploads(
        self,
        active_outbox_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Make interrupted uploads retryable without claiming cloud success."""

        now = _now()
        active = active_outbox_ids or set()
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT outbox_id FROM telemetry_outbox WHERE state='uploading'"
            ).fetchall()
            for row in rows:
                if row["outbox_id"] in active:
                    continue
                connection.execute(
                    "UPDATE telemetry_outbox SET state='failed',"
                    "last_error_code='UPLOAD_INTERRUPTED',next_attempt_at=?,updated_at=? "
                    "WHERE outbox_id=? AND state='uploading'",
                    (now, now, row["outbox_id"]),
                )
        return self.list_telemetry_outbox_due()

    def list_telemetry_outbox_due(self) -> list[dict[str, Any]]:
        now = _now()
        rows = self._connection.execute(
            "SELECT * FROM telemetry_outbox WHERE state='pending' OR "
            "(state='failed' AND (next_attempt_at IS NULL OR next_attempt_at<=?)) "
            "ORDER BY created_at,outbox_id",
            (now,),
        ).fetchall()
        return [_cloud_outbox_dict(row) for row in rows]

    def retry_telemetry_outbox_item(self, outbox_id: str) -> dict[str, Any]:
        item = self.get_telemetry_outbox_item(outbox_id)
        if item["state"] not in {TelemetryState.FAILED.value, TelemetryState.BLOCKED.value}:
            raise ValueError(f"outbox item cannot retry from state {item['state']!r}")
        with self.transaction() as connection:
            connection.execute(
                "UPDATE telemetry_outbox SET state=?,last_error_code=NULL,next_attempt_at=NULL,"
                "updated_at=? WHERE outbox_id=?",
                (TelemetryState.PENDING.value, _now(), outbox_id),
            )
        return self.get_telemetry_outbox_item(outbox_id)

    def record_first_cloud_upload(self, project_id: str) -> dict[str, Any]:
        binding = self.get_observability_binding(project_id)
        first_upload = binding.get("first_upload_at") or _now()
        binding["first_upload_at"] = first_upload
        with self.transaction() as connection:
            connection.execute(
                "UPDATE observability_bindings SET first_upload_at=?,payload_json=?,updated_at=? "
                "WHERE binding_id=?",
                (first_upload, _json(binding), _now(), binding["binding_id"]),
            )
        return self.get_observability_binding(project_id)

    def add_wandb_run_reference(
        self,
        reference: WandbRunReference | dict[str, Any],
    ) -> dict[str, Any]:
        validated = WandbRunReference.model_validate(reference)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO wandb_run_references(reference_id,project_id,envelope_id,"
                "run_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    payload["reference_id"],
                    payload["project_id"],
                    payload["envelope_id"],
                    payload["run_id"],
                    _json(payload),
                    payload["created_at"],
                ),
            )
        row = self._connection.execute(
            "SELECT payload_json FROM wandb_run_references WHERE envelope_id=?",
            (payload["envelope_id"],),
        ).fetchone()
        return json.loads(row["payload_json"])

    def get_wandb_run_reference(self, envelope_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM wandb_run_references WHERE envelope_id=?", (envelope_id,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_wandb_run_reference_for_analysis(
        self,
        analysis_id: str,
    ) -> dict[str, Any] | None:
        """Return the first confirmed W&B run associated with an analysis."""

        row = self._connection.execute(
            "SELECT w.payload_json FROM wandb_run_references w "
            "JOIN telemetry_outbox t ON t.envelope_id=w.envelope_id "
            "WHERE t.analysis_id=? ORDER BY t.created_at,w.created_at LIMIT 1",
            (analysis_id,),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def store_provider_policy(
        self,
        policy: ProviderPolicy | dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an immutable, secret-free provider-policy version."""

        validated = ProviderPolicy.model_validate(policy)
        payload = validated.model_dump(mode="json")
        _assert_no_secret_material(payload, context="assistant provider policy")
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (payload["project_id"],)
            ).fetchone() is None:
                raise KeyError(f"project {payload['project_id']!r} was not found")
            existing = connection.execute(
                "SELECT project_id,payload_json FROM assistant_provider_policies WHERE policy_id=?",
                (payload["policy_id"],),
            ).fetchone()
            if existing is not None:
                if existing["project_id"] != payload["project_id"]:
                    raise ValueError("assistant provider policy belongs to another project")
                prior = json.loads(existing["payload_json"])
                if prior != payload:
                    raise ValueError("assistant provider policy versions are immutable")
                return prior
            if payload["enabled"]:
                data_policy = connection.execute(
                    "SELECT payload_json FROM project_data_policies WHERE project_id=?",
                    (payload["project_id"],),
                ).fetchone()
                if data_policy is None:
                    raise ValueError("enabled assistant policy requires a project data policy")
                classification = json.loads(data_policy["payload_json"])["classification"]
                if classification == "critical_infrastructure":
                    raise ValueError(
                        "W&B Inference is prohibited for critical-infrastructure projects"
                    )
                binding = connection.execute(
                    "SELECT binding_id,external_project_id,deployment,verified,mode "
                    "FROM observability_bindings WHERE project_id=? AND active=1",
                    (payload["project_id"],),
                ).fetchone()
                if (
                    binding is None
                    or binding["binding_id"] != payload["observability_binding_id"]
                    or binding["external_project_id"] != payload["external_project_id"]
                    or not bool(binding["verified"])
                    or binding["mode"] != "online"
                    or binding["deployment"] != "wandb_cloud"
                ):
                    raise ValueError(
                        "enabled assistant policy requires the current verified W&B Cloud binding"
                    )
            connection.execute(
                "INSERT INTO assistant_provider_policies(policy_id,project_id,payload_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?)",
                (
                    payload["policy_id"],
                    payload["project_id"],
                    _json(payload),
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
        return payload

    def get_provider_policy(self, project_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM assistant_provider_policies WHERE project_id=? "
            "ORDER BY updated_at DESC,policy_id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_provider_policy_by_id(self, project_id: str, policy_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM assistant_provider_policies "
            "WHERE project_id=? AND policy_id=?",
            (project_id, policy_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"assistant provider policy {policy_id!r} was not found")
        return json.loads(row["payload_json"])

    def create_assistant_conversation(
        self,
        conversation: AssistantConversation | dict[str, Any],
    ) -> dict[str, Any]:
        validated = AssistantConversation.model_validate(conversation)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            policy_row = connection.execute(
                "SELECT project_id,payload_json FROM assistant_provider_policies WHERE policy_id=?",
                (payload["provider_policy_id"],),
            ).fetchone()
            if policy_row is None or policy_row["project_id"] != payload["project_id"]:
                raise ValueError("assistant conversation provider policy belongs to another project")
            if not ProviderPolicy.model_validate(json.loads(policy_row["payload_json"])).enabled:
                raise ValueError("assistant conversation requires an enabled provider policy")
            data_policy = connection.execute(
                "SELECT payload_json FROM project_data_policies WHERE project_id=?",
                (payload["project_id"],),
            ).fetchone()
            if data_policy is None:
                raise ValueError("assistant conversation requires a project data policy")
            if json.loads(data_policy["payload_json"])["classification"] == "critical_infrastructure":
                raise ValueError(
                    "W&B Inference is prohibited for critical-infrastructure projects"
                )
            if payload["status"] != "active":
                raise ValueError("new assistant conversations must start active")
            if payload["analysis_id"] is not None:
                analysis = connection.execute(
                    "SELECT project_id FROM analyses WHERE analysis_id=?",
                    (payload["analysis_id"],),
                ).fetchone()
                if analysis is None or analysis["project_id"] != payload["project_id"]:
                    raise ValueError("assistant conversation analysis belongs to another project")
            if payload["model_revision_id"] is not None:
                revision = connection.execute(
                    "SELECT project_id FROM model_revisions WHERE revision_id=?",
                    (payload["model_revision_id"],),
                ).fetchone()
                if revision is None or revision["project_id"] != payload["project_id"]:
                    raise ValueError("assistant conversation revision belongs to another project")
            connection.execute(
                "INSERT INTO assistant_conversations(conversation_id,project_id,analysis_id,"
                "model_revision_id,provider_policy_id,status,next_turn_sequence,payload_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["conversation_id"],
                    payload["project_id"],
                    payload["analysis_id"],
                    payload["model_revision_id"],
                    payload["provider_policy_id"],
                    payload["status"],
                    payload["next_turn_sequence"],
                    _json(payload),
                    payload["created_at"],
                    payload["updated_at"],
                ),
            )
        return payload

    def get_assistant_conversation(self, project_id: str, conversation_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM assistant_conversations "
            "WHERE project_id=? AND conversation_id=?",
            (project_id, conversation_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"assistant conversation {conversation_id!r} was not found")
        return json.loads(row["payload_json"])

    def list_assistant_conversations(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload_json FROM assistant_conversations WHERE project_id=? "
            "ORDER BY created_at,conversation_id",
            (project_id,),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def close_assistant_conversation(self, project_id: str, conversation_id: str) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM assistant_conversations "
                "WHERE project_id=? AND conversation_id=?",
                (project_id, conversation_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"assistant conversation {conversation_id!r} was not found")
            payload = json.loads(row["payload_json"])
            payload.update({"status": "closed", "updated_at": _now()})
            validated = AssistantConversation.model_validate(payload).model_dump(mode="json")
            connection.execute(
                "UPDATE assistant_conversations SET status='closed',payload_json=?,updated_at=? "
                "WHERE conversation_id=?",
                (_json(validated), validated["updated_at"], conversation_id),
            )
        return validated

    def store_assistant_turn(self, turn: AssistantTurn | dict[str, Any]) -> dict[str, Any]:
        validated = AssistantTurn.model_validate(turn)
        payload = validated.model_dump(mode="json")
        if payload["state"] != "pending":
            raise ValueError("new assistant turns must start pending")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT project_id,status,next_turn_sequence,payload_json "
                "FROM assistant_conversations WHERE conversation_id=?",
                (payload["conversation_id"],),
            ).fetchone()
            if row is None or row["project_id"] != payload["project_id"]:
                raise ValueError("assistant turn conversation belongs to another project")
            if row["status"] != "active":
                raise ValueError("assistant turn cannot be added to a closed conversation")
            if int(row["next_turn_sequence"]) != payload["sequence"]:
                raise ValueError("assistant turn sequence is not the next reserved sequence")
            conversation = AssistantConversation.model_validate(
                json.loads(row["payload_json"])
            )
            if (
                payload["evidence_packet_id"] != conversation.packet_id
                or payload["evidence_packet_sha256"] != conversation.packet_sha256
                or payload["anchor"] != conversation.anchor.model_dump(mode="json")
            ):
                raise ValueError("assistant turn does not match its conversation evidence scope")
            connection.execute(
                "INSERT INTO assistant_turns(turn_id,conversation_id,project_id,sequence,state,"
                "answer_id,payload_json,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    payload["turn_id"],
                    payload["conversation_id"],
                    payload["project_id"],
                    payload["sequence"],
                    payload["state"],
                    payload["answer_id"],
                    _json(payload),
                    payload["created_at"],
                    payload["completed_at"],
                ),
            )
            conversation_payload = conversation.model_dump(mode="json")
            conversation_payload.update(
                {"next_turn_sequence": payload["sequence"] + 1, "updated_at": _now()}
            )
            conversation_payload = AssistantConversation.model_validate(
                conversation_payload
            ).model_dump(mode="json")
            connection.execute(
                "UPDATE assistant_conversations SET next_turn_sequence=?,payload_json=?,updated_at=? "
                "WHERE conversation_id=?",
                (
                    conversation_payload["next_turn_sequence"],
                    _json(conversation_payload),
                    conversation_payload["updated_at"],
                    payload["conversation_id"],
                ),
            )
        return payload

    def get_assistant_turn(self, project_id: str, turn_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM assistant_turns WHERE project_id=? AND turn_id=?",
            (project_id, turn_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"assistant turn {turn_id!r} was not found")
        return json.loads(row["payload_json"])

    def list_assistant_turns(self, project_id: str, conversation_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload_json FROM assistant_turns WHERE project_id=? AND conversation_id=? "
            "ORDER BY sequence",
            (project_id, conversation_id),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def finish_assistant_turn(self, turn: AssistantTurn | dict[str, Any]) -> dict[str, Any]:
        validated = AssistantTurn.model_validate(turn)
        payload = validated.model_dump(mode="json")
        if payload["state"] == "pending":
            raise ValueError("assistant turn finish requires a terminal state")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT project_id,conversation_id,state FROM assistant_turns WHERE turn_id=?",
                (payload["turn_id"],),
            ).fetchone()
            if (
                row is None
                or row["project_id"] != payload["project_id"]
                or row["conversation_id"] != payload["conversation_id"]
            ):
                raise ValueError("assistant turn identity does not match the repository")
            if row["state"] != "pending":
                raise ValueError("assistant turn is already terminal")
            if payload["answer_id"] is not None and connection.execute(
                "SELECT 1 FROM assistant_answers WHERE answer_id=? AND project_id=?",
                (payload["answer_id"], payload["project_id"]),
            ).fetchone() is None:
                raise ValueError("assistant turn answer does not exist in this project")
            connection.execute(
                "UPDATE assistant_turns SET state=?,answer_id=?,payload_json=?,completed_at=? "
                "WHERE turn_id=?",
                (
                    payload["state"],
                    payload["answer_id"],
                    _json(payload),
                    payload["completed_at"],
                    payload["turn_id"],
                ),
            )
        return payload

    def add_weave_conversation_reference(
        self,
        reference: WeaveConversationReference | dict[str, Any],
    ) -> dict[str, Any]:
        validated = WeaveConversationReference.model_validate(reference)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            turn = connection.execute(
                "SELECT project_id,conversation_id,state FROM assistant_turns WHERE turn_id=?",
                (payload["turn_id"],),
            ).fetchone()
            if (
                turn is None
                or turn["project_id"] != payload["project_id"]
                or turn["conversation_id"] != payload["conversation_id"]
            ):
                raise ValueError("Weave conversation reference does not match its assistant turn")
            if turn["state"] != "pending":
                raise ValueError("Weave conversation references require a pending assistant turn")
            binding = connection.execute(
                "SELECT entity,external_project,payload_json FROM observability_bindings "
                "WHERE project_id=? AND active=1",
                (payload["project_id"],),
            ).fetchone()
            if (
                binding is None
                or payload["entity"] != binding["entity"]
                or payload["external_project"] != binding["external_project"]
            ):
                raise ValueError(
                    "Weave conversation reference does not match the active W&B binding"
                )
            binding_payload = json.loads(binding["payload_json"])
            expected_origin = urllib.parse.urlsplit(binding_payload["web_base_url"])
            actual_url = urllib.parse.urlsplit(payload["url"])
            expected_prefix = "/".join(
                (
                    expected_origin.path.rstrip("/"),
                    urllib.parse.quote(payload["entity"], safe=""),
                    urllib.parse.quote(payload["external_project"], safe=""),
                    "weave/agents/conversations",
                    urllib.parse.quote(payload["conversation_id"], safe=""),
                )
            )
            if (
                actual_url.scheme != expected_origin.scheme
                or actual_url.netloc != expected_origin.netloc
                or actual_url.path.rstrip("/") != expected_prefix.rstrip("/")
                or actual_url.query
                or actual_url.fragment
            ):
                raise ValueError(
                    "Weave conversation URL does not match the active W&B destination"
                )
            existing = connection.execute(
                "SELECT payload_json FROM weave_conversation_references "
                "WHERE project_id=? AND turn_id=?",
                (payload["project_id"], payload["turn_id"]),
            ).fetchone()
            if existing is not None:
                prior = json.loads(existing["payload_json"])
                if prior != payload:
                    raise ValueError("assistant turn already has a different Weave reference")
                return prior
            connection.execute(
                "INSERT INTO weave_conversation_references(reference_id,project_id,"
                "conversation_id,turn_id,trace_id,root_span_id,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    payload["reference_id"],
                    payload["project_id"],
                    payload["conversation_id"],
                    payload["turn_id"],
                    payload["trace_id"],
                    payload["root_span_id"],
                    _json(payload),
                    payload["created_at"],
                ),
            )
            conversation = connection.execute(
                "SELECT payload_json FROM assistant_conversations WHERE conversation_id=?",
                (payload["conversation_id"],),
            ).fetchone()
            if conversation is None:  # pragma: no cover - protected by foreign keys
                raise RuntimeError("assistant conversation disappeared during trace persistence")
            conversation_payload = json.loads(conversation["payload_json"])
            conversation_payload.update(
                {
                    "weave_reference_id": payload["reference_id"],
                    "weave_url": payload["url"],
                    "updated_at": _now(),
                }
            )
            conversation_payload = AssistantConversation.model_validate(
                conversation_payload
            ).model_dump(mode="json")
            connection.execute(
                "UPDATE assistant_conversations SET payload_json=?,updated_at=? "
                "WHERE conversation_id=?",
                (
                    _json(conversation_payload),
                    conversation_payload["updated_at"],
                    payload["conversation_id"],
                ),
            )
        return payload

    def get_weave_conversation_reference(
        self, project_id: str, turn_id: str
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM weave_conversation_references "
            "WHERE project_id=? AND turn_id=?",
            (project_id, turn_id),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def add_weave_trace_reference(
        self,
        reference: WeaveTraceReference | dict[str, Any],
    ) -> dict[str, Any]:
        validated = WeaveTraceReference.model_validate(reference)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO weave_trace_references(reference_id,project_id,call_id,"
                "trace_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    payload["reference_id"],
                    payload["project_id"],
                    payload["call_id"],
                    payload["trace_id"],
                    _json(payload),
                    payload["created_at"],
                ),
            )
        row = self._connection.execute(
            "SELECT payload_json FROM weave_trace_references WHERE project_id=? AND call_id=?",
            (payload["project_id"], payload["call_id"]),
        ).fetchone()
        return json.loads(row["payload_json"])

    def get_weave_trace_reference(self, project_id: str, call_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM weave_trace_references WHERE project_id=? AND call_id=?",
            (project_id, call_id),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def store_assistant_answer(self, answer: AssistantAnswer | dict[str, Any]) -> dict[str, Any]:
        validated = AssistantAnswer.model_validate(answer)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            turn_payload: dict[str, Any] | None = None
            if payload["conversation_id"] is not None:
                turn = connection.execute(
                    "SELECT project_id,conversation_id,state,payload_json FROM assistant_turns "
                    "WHERE turn_id=?",
                    (payload["turn_id"],),
                ).fetchone()
                if (
                    turn is None
                    or turn["project_id"] != payload["project_id"]
                    or turn["conversation_id"] != payload["conversation_id"]
                    or turn["state"] != "pending"
                ):
                    raise ValueError("conversation-backed answer does not match a pending turn")
                trace = connection.execute(
                    "SELECT payload_json FROM weave_conversation_references "
                    "WHERE project_id=? AND turn_id=?",
                    (payload["project_id"], payload["turn_id"]),
                ).fetchone()
                if (
                    trace is None
                    or payload["weave_conversation"] is None
                    or json.loads(trace["payload_json"]) != payload["weave_conversation"]
                ):
                    raise ValueError("conversation-backed answer requires its acknowledged Weave turn")
                turn_payload = json.loads(turn["payload_json"])
                if (
                    payload["evidence_packet_id"] != turn_payload["evidence_packet_id"]
                    or payload["evidence_packet_sha256"]
                    != turn_payload["evidence_packet_sha256"]
                ):
                    raise ValueError("assistant answer does not match its turn evidence packet")
            connection.execute(
                "INSERT INTO assistant_answers(answer_id,project_id,call_id,payload_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    payload["answer_id"],
                    payload["project_id"],
                    payload["call_id"],
                    _json(payload),
                    payload["created_at"],
                ),
            )
            if turn_payload is not None:
                turn_payload.update(
                    {
                        "state": "completed",
                        "answer_id": payload["answer_id"],
                        "completed_at": payload["created_at"],
                    }
                )
                completed = AssistantTurn.model_validate(turn_payload).model_dump(mode="json")
                connection.execute(
                    "UPDATE assistant_turns SET state='completed',answer_id=?,payload_json=?,"
                    "completed_at=? WHERE turn_id=?",
                    (
                        payload["answer_id"],
                        _json(completed),
                        completed["completed_at"],
                        payload["turn_id"],
                    ),
                )
        return payload

    def store_assistant_evaluation(
        self,
        evaluation: AssistantEvaluationRecord | dict[str, Any],
    ) -> dict[str, Any]:
        validated = AssistantEvaluationRecord.model_validate(evaluation)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO assistant_evaluations(evaluation_id,project_id,answer_id,payload_json,"
                "created_at) VALUES(?,?,?,?,?)",
                (
                    payload["evaluation_id"],
                    payload["project_id"],
                    payload["answer_id"],
                    _json(payload),
                    payload["created_at"],
                ),
            )
        return payload

    def store_validated_assistant_result(
        self,
        answer: AssistantAnswer | dict[str, Any],
        evaluation: AssistantEvaluationRecord | dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Atomically persist a validated answer, its evaluation, and turn state.

        A generated answer is not a durable product result until its deterministic
        evaluation and acknowledged Weave turn are committed in the same SQLite
        transaction. This prevents a crash between separate writes from leaving a
        completed-looking answer without its release-authority validation record.
        """

        validated_answer = AssistantAnswer.model_validate(answer)
        validated_evaluation = AssistantEvaluationRecord.model_validate(evaluation)
        answer_payload = validated_answer.model_dump(mode="json")
        evaluation_payload = validated_evaluation.model_dump(mode="json")
        if (
            not validated_evaluation.valid
            or validated_evaluation.answer_id != validated_answer.answer_id
            or validated_evaluation.project_id != validated_answer.project_id
            or validated_answer.weave_conversation is None
            or validated_evaluation.trace_id
            != validated_answer.weave_conversation.trace_id
            or validated_answer.conversation_id is None
            or validated_answer.turn_id is None
        ):
            raise ValueError(
                "validated assistant persistence requires one valid evaluation for "
                "the same answer, project, and acknowledged Weave turn"
            )

        with self.transaction() as connection:
            turn = connection.execute(
                "SELECT project_id,conversation_id,state,payload_json FROM assistant_turns "
                "WHERE turn_id=?",
                (validated_answer.turn_id,),
            ).fetchone()
            if (
                turn is None
                or turn["project_id"] != validated_answer.project_id
                or turn["conversation_id"] != validated_answer.conversation_id
                or turn["state"] != "pending"
            ):
                raise ValueError("validated assistant answer does not match a pending turn")
            turn_payload = json.loads(turn["payload_json"])
            if (
                answer_payload["evidence_packet_id"]
                != turn_payload["evidence_packet_id"]
                or answer_payload["evidence_packet_sha256"]
                != turn_payload["evidence_packet_sha256"]
            ):
                raise ValueError("validated assistant answer does not match its evidence packet")
            trace = connection.execute(
                "SELECT payload_json FROM weave_conversation_references "
                "WHERE project_id=? AND turn_id=?",
                (validated_answer.project_id, validated_answer.turn_id),
            ).fetchone()
            if trace is None or json.loads(trace["payload_json"]) != answer_payload[
                "weave_conversation"
            ]:
                raise ValueError(
                    "validated assistant answer requires its acknowledged Weave turn"
                )

            connection.execute(
                "INSERT INTO assistant_answers(answer_id,project_id,call_id,payload_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    answer_payload["answer_id"],
                    answer_payload["project_id"],
                    answer_payload["call_id"],
                    _json(answer_payload),
                    answer_payload["created_at"],
                ),
            )
            connection.execute(
                "INSERT INTO assistant_evaluations(evaluation_id,project_id,answer_id,payload_json,"
                "created_at) VALUES(?,?,?,?,?)",
                (
                    evaluation_payload["evaluation_id"],
                    evaluation_payload["project_id"],
                    evaluation_payload["answer_id"],
                    _json(evaluation_payload),
                    evaluation_payload["created_at"],
                ),
            )
            turn_payload.update(
                {
                    "state": "completed",
                    "answer_id": answer_payload["answer_id"],
                    "completed_at": answer_payload["created_at"],
                }
            )
            completed = AssistantTurn.model_validate(turn_payload).model_dump(mode="json")
            connection.execute(
                "UPDATE assistant_turns SET state='completed',answer_id=?,payload_json=?,"
                "completed_at=? WHERE turn_id=?",
                (
                    answer_payload["answer_id"],
                    _json(completed),
                    completed["completed_at"],
                    validated_answer.turn_id,
                ),
            )
        return {"answer": answer_payload, "evaluation": evaluation_payload}

    def recover_abandoned_assistant_turns(
        self,
        *,
        older_than_seconds: int = 120,
    ) -> int:
        """Fail stale pending assistant turns after an interrupted worker process."""

        if older_than_seconds < 60:
            raise ValueError("assistant turn recovery grace period must be at least 60 seconds")
        tables = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='assistant_turns'"
        ).fetchone()
        if tables is None:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(seconds=older_than_seconds)).isoformat()
        recovered = 0
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT turn_id,payload_json FROM assistant_turns "
                "WHERE state='pending' AND created_at<? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                payload.update(
                    {
                        "state": "failed",
                        "error_code": "ASSISTANT_WORKER_RESTARTED",
                        "completed_at": _now(),
                    }
                )
                failed = AssistantTurn.model_validate(payload).model_dump(mode="json")
                connection.execute(
                    "UPDATE assistant_turns SET state='failed',payload_json=?,completed_at=? "
                    "WHERE turn_id=? AND state='pending'",
                    (_json(failed), failed["completed_at"], row["turn_id"]),
                )
                recovered += 1
        return recovered

    def get_assistant_answer(self, project_id: str, answer_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM assistant_answers WHERE project_id=? AND answer_id=?",
            (project_id, answer_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"assistant answer {answer_id!r} was not found")
        return json.loads(row["payload_json"])

    def store_model_revision(self, revision: ModelRevision | dict[str, Any]) -> dict[str, Any]:
        validated = ModelRevision.model_validate(revision)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            if payload["observability_binding_id"] is not None:
                binding = connection.execute(
                    "SELECT project_id FROM observability_bindings WHERE binding_id=?",
                    (payload["observability_binding_id"],),
                ).fetchone()
                if binding is None or binding["project_id"] != payload["project_id"]:
                    raise ValueError("model revision binding must belong to the same project")
            if payload["parent_revision_id"] is not None:
                parent = connection.execute(
                    "SELECT project_id FROM model_revisions WHERE revision_id=?",
                    (payload["parent_revision_id"],),
                ).fetchone()
                if parent is None or parent["project_id"] != payload["project_id"]:
                    raise ValueError("model revision parent must belong to the same project")
            connection.execute(
                "INSERT INTO model_revisions(revision_id,project_id,parent_revision_id,"
                "observability_binding_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    payload["revision_id"],
                    payload["project_id"],
                    payload["parent_revision_id"],
                    payload["observability_binding_id"],
                    _json(payload),
                    payload["created_at"],
                ),
            )
        return payload

    def get_model_revision(self, revision_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM model_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"model revision {revision_id!r} was not found")
        return json.loads(row["payload_json"])

    def store_imported_model_record(
        self,
        revision: ModelRevision | dict[str, Any],
        passport: ModelFitnessPassport | dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Atomically record an imported revision, its unassessed passport, and project state."""

        validated_revision = ModelRevision.model_validate(revision)
        validated_passport = ModelFitnessPassport.model_validate(passport)
        revision_payload = validated_revision.model_dump(mode="json")
        passport_payload = validated_passport.model_dump(mode="json")
        if validated_passport.project_id != validated_revision.project_id:
            raise ValueError("model revision and fitness passport belong to different projects")
        if validated_passport.model_revision_id != validated_revision.revision_id:
            raise ValueError("fitness passport does not describe the imported model revision")
        if validated_passport.disposition.value != "not_assessed":
            raise ValueError("bundle import may create only an unassessed fitness passport")

        with self.transaction() as connection:
            if revision_payload["observability_binding_id"] is not None:
                binding = connection.execute(
                    "SELECT project_id FROM observability_bindings WHERE binding_id=?",
                    (revision_payload["observability_binding_id"],),
                ).fetchone()
                if binding is None or binding["project_id"] != revision_payload["project_id"]:
                    raise ValueError(
                        "imported model revision binding must belong to the same project"
                    )
            connection.execute(
                "INSERT INTO model_revisions(revision_id,project_id,parent_revision_id,"
                "observability_binding_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    revision_payload["revision_id"],
                    revision_payload["project_id"],
                    revision_payload["parent_revision_id"],
                    revision_payload["observability_binding_id"],
                    _json(revision_payload),
                    revision_payload["created_at"],
                ),
            )
            connection.execute(
                "INSERT INTO model_fitness_passports(passport_id,project_id,model_revision_id,"
                "payload_json,issued_at) VALUES(?,?,?,?,?)",
                (
                    passport_payload["passport_id"],
                    passport_payload["project_id"],
                    passport_payload["model_revision_id"],
                    _json(passport_payload),
                    passport_payload["issued_at"],
                ),
            )
            cursor = connection.execute(
                "UPDATE projects SET state_json=?, updated_at=? WHERE project_id=?",
                (_json(state), _now(), revision_payload["project_id"]),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"project {revision_payload['project_id']!r} was not found")
        return revision_payload, passport_payload, self.get_project(validated_revision.project_id)

    def store_model_fitness_passport(
        self,
        passport: ModelFitnessPassport | dict[str, Any],
    ) -> dict[str, Any]:
        validated = ModelFitnessPassport.model_validate(passport)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            revision = connection.execute(
                "SELECT project_id FROM model_revisions WHERE revision_id=?",
                (payload["model_revision_id"],),
            ).fetchone()
            if revision is None or revision["project_id"] != payload["project_id"]:
                raise ValueError("fitness passport must describe a revision in the same project")
            connection.execute(
                "INSERT INTO model_fitness_passports(passport_id,project_id,model_revision_id,"
                "payload_json,issued_at) VALUES(?,?,?,?,?)",
                (
                    payload["passport_id"],
                    payload["project_id"],
                    payload["model_revision_id"],
                    _json(payload),
                    payload["issued_at"],
                ),
            )
        return payload

    def get_model_fitness_passport(
        self,
        project_id: str,
        model_revision_id: str | None = None,
    ) -> dict[str, Any] | None:
        if model_revision_id is None:
            row = self._connection.execute(
                "SELECT payload_json FROM model_fitness_passports WHERE project_id=? "
                "ORDER BY issued_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT payload_json FROM model_fitness_passports "
                "WHERE project_id=? AND model_revision_id=? ORDER BY issued_at DESC LIMIT 1",
                (project_id, model_revision_id),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_model_fitness_passport_by_id(self, passport_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM model_fitness_passports WHERE passport_id=?",
            (passport_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"model fitness passport {passport_id!r} was not found")
        return json.loads(row["payload_json"])

    def store_decision_brief(self, brief: DecisionBrief | dict[str, Any]) -> dict[str, Any]:
        validated = DecisionBrief.model_validate(brief)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO decision_briefs(brief_id,project_id,analysis_id,model_revision_id,"
                "payload_json,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(brief_id) DO UPDATE SET payload_json=excluded.payload_json,"
                "updated_at=excluded.updated_at",
                (
                    payload["brief_id"],
                    payload["project_id"],
                    payload["analysis_id"],
                    payload["model_revision_id"],
                    _json(payload),
                    payload["updated_at"],
                ),
            )
        return payload

    def store_export_manifest(
        self,
        manifest: ExportManifest | dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one immutable export description after its files are written."""

        validated = ExportManifest.model_validate(manifest)
        payload = validated.model_dump(mode="json")
        analysis = self.get_analysis(validated.analysis_id)
        if analysis["project_id"] != validated.project_id:
            raise ValueError("export manifest analysis belongs to a different project")
        if analysis["payload"].get("model_revision_id") != validated.model_revision_id:
            raise ValueError("export manifest model revision does not match its analysis")
        passport = self.get_model_fitness_passport_by_id(validated.model_fitness_passport_id)
        if (
            passport["project_id"] != validated.project_id
            or passport["model_revision_id"] != validated.model_revision_id
        ):
            raise ValueError("export manifest fitness passport does not match its revision")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO exports(export_id,project_id,analysis_id,mode,manifest_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    payload["export_id"],
                    payload["project_id"],
                    payload["analysis_id"],
                    payload["package_type"],
                    _json(payload),
                    payload["created_at"],
                ),
            )
        return payload

    def get_decision_brief(
        self,
        project_id: str,
        analysis_id: str,
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM decision_briefs WHERE project_id=? AND analysis_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (project_id, analysis_id),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def upsert_guided_checkpoint(
        self,
        checkpoint: GuidedCheckpoint | dict[str, Any],
    ) -> dict[str, Any]:
        validated = GuidedCheckpoint.model_validate(checkpoint)
        payload = validated.model_dump(mode="json")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO guided_checkpoints(checkpoint_id,project_id,step_id,payload_json,"
                "updated_at) VALUES(?,?,?,?,?) ON CONFLICT(project_id,step_id) DO UPDATE SET "
                "payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (
                    payload["checkpoint_id"],
                    payload["project_id"],
                    payload["step_id"],
                    _json(payload),
                    payload["updated_at"],
                ),
            )
        return payload

    def list_guided_checkpoints(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload_json FROM guided_checkpoints WHERE project_id=? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def observability_status(self, project_id: str) -> dict[str, Any]:
        try:
            binding = self.get_observability_binding(project_id)
        except KeyError:
            binding = None
        counts = self._connection.execute(
            "SELECT state,count(*) AS count FROM telemetry_outbox WHERE project_id=? GROUP BY state",
            (project_id,),
        ).fetchall()
        pending_bytes = int(
            self._connection.execute(
                "SELECT coalesce(sum(a.bytes),0) FROM cloud_artifacts a "
                "JOIN telemetry_outbox o ON o.envelope_id=a.envelope_id "
                "WHERE o.project_id=? AND o.state!='synced'",
                (project_id,),
            ).fetchone()[0]
        )
        last_synced = self._connection.execute(
            "SELECT max(updated_at) FROM telemetry_outbox WHERE project_id=? AND state='synced'",
            (project_id,),
        ).fetchone()[0]
        last_error = self._connection.execute(
            "SELECT last_error_code FROM telemetry_outbox WHERE project_id=? "
            "AND last_error_code IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return {
            "binding": binding,
            "can_enter_home": True,
            "outbox_counts": {row["state"]: int(row["count"]) for row in counts},
            "pending_artifact_bytes": pending_bytes,
            "last_synced_at": last_synced,
            "last_error_code": last_error["last_error_code"] if last_error else None,
        }

    def table_counts(self) -> dict[str, int]:
        tables = [
            "projects",
            "analyses",
            "designs",
            "solver_jobs",
            "run_artifacts",
            "run_metrics",
            "exports",
            "observability_bindings",
            "telemetry_outbox",
            "project_data_policies",
            "cloud_artifacts",
            "wandb_run_references",
            "weave_trace_references",
            "assistant_answers",
            "assistant_evaluations",
            "model_revisions",
            "model_fitness_passports",
            "decision_briefs",
            "guided_checkpoints",
        ]
        return {
            table: int(self._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


def _binding_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["settings"] = json.loads(result.pop("settings_json"))
    result["enabled"] = bool(result["enabled"])
    return result


def _telemetry_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    result["redistributable"] = bool(result["redistributable"])
    result["attempts"] = result.pop("attempt_count")
    return result


def _external_reference_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json"))
    return result


_SECRET_KEY_FRAGMENTS = {
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "environment",
    "consolelog",
    "password",
    "privatekey",
    "secret",
    "sessiontoken",
    "stderr",
    "stdout",
    "token",
}


def _assert_no_secret_material(value: Any, *, context: str, path: str = "$") -> None:
    """Reject credential-shaped fields before they can reach SQLite."""

    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                raise ValueError(f"{context} may not persist secret field {path}.{key}")
            _assert_no_secret_material(item, context=context, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_secret_material(item, context=context, path=f"{path}[{index}]")


def _sanitize_persisted_error(value: str | None, *, project_root: Path) -> str | None:
    if value is None:
        return None
    sanitized = str(value).replace(str(project_root), "<project>")
    sanitized = sanitized.replace("\r", " ").replace("\n", " ").replace("\0", "")
    # Prevent common absolute-path forms from disclosing local account names.
    import re

    sanitized = re.sub(r"/Users/[^/\\\s]+", "/Users/<redacted>", sanitized)
    sanitized = re.sub(r"[A-Za-z]:\\\\Users\\\\[^\\\\\s]+", r"C:\\Users\\<redacted>", sanitized)
    return sanitized[:500]


def _cloud_outbox_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["envelope"] = json.loads(result.pop("envelope_json"))
    result["attempts"] = result.pop("attempt_count")
    return result


def _assert_no_absolute_paths(value: Any, *, context: str, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if normalized in {"path", "absolutepath", "localpath", "filepath"}:
                raise ValueError(f"{context} may not persist local path field {path}.{key}")
            _assert_no_absolute_paths(item, context=context, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_absolute_paths(item, context=context, path=f"{path}[{index}]")
    elif isinstance(value, str) and re.search(r"(?:^/|/Users/|[A-Za-z]:\\|\\\\)", value):
        raise ValueError(f"{context} may not persist a local absolute path at {path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
