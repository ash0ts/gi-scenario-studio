"""Generic durable repository for directly evaluated scenario portfolios.

The database is authoritative. Cloud projection is represented by an outbox
written in the same transaction as each scientific producer record.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Self

from .contracts import (
    AnalysisState,
    DirectJobState,
    assert_transition,
    canonical_json,
    new_id,
    require_sha256,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    required INTEGER NOT NULL CHECK(required IN (0,1)),
    safeguard INTEGER NOT NULL CHECK(safeguard IN (0,1)),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    option_ids_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, option_ids_json)
);
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN (
        'DRAFT','VALIDATING','READY','RUNNING','PAUSING','PAUSED',
        'COMPLETED','FAILED','CANCELLED'
    )),
    idempotency_key TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS analysis_scenarios (
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    scenario_id TEXT NOT NULL REFERENCES scenarios(scenario_id),
    PRIMARY KEY(analysis_id, scenario_id)
);
CREATE TABLE IF NOT EXISTS direct_jobs (
    job_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    portfolio_id TEXT NOT NULL REFERENCES portfolios(portfolio_id),
    scenario_id TEXT NOT NULL REFERENCES scenarios(scenario_id),
    state TEXT NOT NULL CHECK(state IN (
        'PLANNED','QUEUED','RUNNING','SUCCEEDED','QA_REJECTED','FAILED','CANCELLED'
    )),
    model_run_cache_key TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
    payload_json TEXT NOT NULL,
    finalization_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(analysis_id, portfolio_id, scenario_id)
);
CREATE INDEX IF NOT EXISTS direct_jobs_cache_key
    ON direct_jobs(model_run_cache_key, state);
CREATE TABLE IF NOT EXISTS model_run_cache_entries (
    model_run_cache_key TEXT PRIMARY KEY,
    source_job_id TEXT NOT NULL UNIQUE REFERENCES direct_jobs(job_id),
    finalization_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS direct_job_artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES direct_jobs(job_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK(bytes >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(job_id, role)
);
CREATE TABLE IF NOT EXISTS direct_job_metrics (
    job_id TEXT NOT NULL REFERENCES direct_jobs(job_id) ON DELETE CASCADE,
    metric_id TEXT NOT NULL,
    object_id TEXT NOT NULL DEFAULT '',
    value REAL NOT NULL,
    units TEXT NOT NULL,
    PRIMARY KEY(job_id, metric_id, object_id)
);
CREATE TABLE IF NOT EXISTS direct_job_qa (
    finding_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES direct_jobs(job_id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_nodes (
    node_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES direct_jobs(job_id) ON DELETE CASCADE,
    node_type TEXT NOT NULL,
    is_claim INTEGER NOT NULL CHECK(is_claim IN (0,1)),
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_edges (
    edge_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES direct_jobs(job_id) ON DELETE CASCADE,
    source_node_id TEXT NOT NULL REFERENCES evidence_nodes(node_id),
    target_node_id TEXT NOT NULL REFERENCES evidence_nodes(node_id),
    relation TEXT NOT NULL,
    UNIQUE(source_node_id, target_node_id, relation)
);
CREATE TABLE IF NOT EXISTS producer_records (
    record_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    producer_kind TEXT NOT NULL CHECK(producer_kind IN (
        'bundle_import','guided_session','analysis_completion','evidence_export'
    )),
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, producer_kind, subject_id)
);
CREATE TABLE IF NOT EXISTS telemetry_outbox (
    outbox_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT REFERENCES analyses(analysis_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('PENDING','UPLOADING','SYNCED','FAILED','BLOCKED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    external_reference_json TEXT,
    last_error_code TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS telemetry_outbox_state
    ON telemetry_outbox(state, created_at);
CREATE TABLE IF NOT EXISTS event_sequences (
    stream_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL CHECK(last_sequence >= 0)
);

CREATE TRIGGER IF NOT EXISTS direct_job_cache_key_immutable
BEFORE UPDATE OF model_run_cache_key ON direct_jobs
WHEN OLD.model_run_cache_key != NEW.model_run_cache_key
BEGIN SELECT RAISE(ABORT, 'model-run cache key is immutable'); END;

CREATE TRIGGER IF NOT EXISTS terminal_direct_job_immutable
BEFORE UPDATE ON direct_jobs
WHEN OLD.state IN ('SUCCEEDED','QA_REJECTED','FAILED','CANCELLED')
BEGIN SELECT RAISE(ABORT, 'terminal direct job is immutable'); END;

CREATE TRIGGER IF NOT EXISTS metrics_require_success
BEFORE INSERT ON direct_job_metrics
WHEN (SELECT state FROM direct_jobs WHERE job_id = NEW.job_id) != 'SUCCEEDED'
BEGIN SELECT RAISE(ABORT, 'metrics require a successful direct job'); END;

CREATE TRIGGER IF NOT EXISTS claims_require_success
BEFORE INSERT ON evidence_nodes
WHEN (NEW.is_claim = 1 OR lower(NEW.node_type) IN ('claim','evidence_claim'))
  AND (SELECT state FROM direct_jobs WHERE job_id = NEW.job_id) != 'SUCCEEDED'
BEGIN SELECT RAISE(ABORT, 'evidence claims require a successful direct job'); END;

CREATE TRIGGER IF NOT EXISTS model_run_cache_immutable_update
BEFORE UPDATE ON model_run_cache_entries
BEGIN SELECT RAISE(ABORT, 'model-run cache entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS model_run_cache_immutable_delete
BEFORE DELETE ON model_run_cache_entries
BEGIN SELECT RAISE(ABORT, 'model-run cache entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS direct_job_artifacts_immutable_update
BEFORE UPDATE ON direct_job_artifacts
BEGIN SELECT RAISE(ABORT, 'direct-job artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS direct_job_artifacts_immutable_delete
BEFORE DELETE ON direct_job_artifacts
BEGIN SELECT RAISE(ABORT, 'direct-job artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS direct_job_metrics_immutable_update
BEFORE UPDATE ON direct_job_metrics
BEGIN SELECT RAISE(ABORT, 'direct-job metrics are immutable'); END;
CREATE TRIGGER IF NOT EXISTS direct_job_metrics_immutable_delete
BEFORE DELETE ON direct_job_metrics
BEGIN SELECT RAISE(ABORT, 'direct-job metrics are immutable'); END;
CREATE TRIGGER IF NOT EXISTS direct_job_qa_immutable_update
BEFORE UPDATE ON direct_job_qa
BEGIN SELECT RAISE(ABORT, 'direct-job QA is immutable'); END;
CREATE TRIGGER IF NOT EXISTS direct_job_qa_immutable_delete
BEFORE DELETE ON direct_job_qa
BEGIN SELECT RAISE(ABORT, 'direct-job QA is immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_nodes_immutable_update
BEFORE UPDATE ON evidence_nodes
BEGIN SELECT RAISE(ABORT, 'evidence nodes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_nodes_immutable_delete
BEFORE DELETE ON evidence_nodes
BEGIN SELECT RAISE(ABORT, 'evidence nodes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_edges_immutable_update
BEFORE UPDATE ON evidence_edges
BEGIN SELECT RAISE(ABORT, 'evidence edges are immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_edges_immutable_delete
BEFORE DELETE ON evidence_edges
BEGIN SELECT RAISE(ABORT, 'evidence edges are immutable'); END;
CREATE TRIGGER IF NOT EXISTS producer_records_immutable_update
BEFORE UPDATE ON producer_records
BEGIN SELECT RAISE(ABORT, 'producer records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS producer_records_immutable_delete
BEFORE DELETE ON producer_records
BEGIN SELECT RAISE(ABORT, 'producer records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS telemetry_outbox_evidence_immutable
BEFORE UPDATE OF project_id,analysis_id,event_type,subject_id,payload_json,payload_sha256,
                 idempotency_key,created_at ON telemetry_outbox
WHEN OLD.project_id IS NOT NEW.project_id
  OR OLD.analysis_id IS NOT NEW.analysis_id
  OR OLD.event_type IS NOT NEW.event_type
  OR OLD.subject_id IS NOT NEW.subject_id
  OR OLD.payload_json IS NOT NEW.payload_json
  OR OLD.payload_sha256 IS NOT NEW.payload_sha256
  OR OLD.idempotency_key IS NOT NEW.idempotency_key
  OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'telemetry evidence is immutable'); END;

CREATE VIEW IF NOT EXISTS comparable_portfolios AS
SELECT DISTINCT j.analysis_id, j.portfolio_id
FROM direct_jobs AS j
WHERE NOT EXISTS (
    SELECT 1 FROM analysis_scenarios AS required
    WHERE required.analysis_id = j.analysis_id
      AND NOT EXISTS (
          SELECT 1 FROM direct_jobs AS completed
          WHERE completed.analysis_id = required.analysis_id
            AND completed.portfolio_id = j.portfolio_id
            AND completed.scenario_id = required.scenario_id
            AND completed.state = 'SUCCEEDED'
      )
);
"""


class ScenarioRepository:
    """Single-writer SQLite repository with explicit transactional producers."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        user_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version not in {0, 1, 2}:
            self._connection.close()
            raise RuntimeError(f"unsupported scenario-platform database version: {user_version}")
        self._connection.executescript(_SCHEMA)
        if user_version < 2:
            columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(telemetry_outbox)")
            }
            if "lease_token" not in columns:
                self._connection.execute("ALTER TABLE telemetry_outbox ADD COLUMN lease_token TEXT")
            if "lease_expires_at" not in columns:
                self._connection.execute(
                    "ALTER TABLE telemetry_outbox ADD COLUMN lease_expires_at TEXT"
                )
        self._connection.execute("PRAGMA user_version=2")
        self.recover_expired_outbox_leases()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def create_project(
        self,
        title: str,
        *,
        payload: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> str:
        if not title.strip():
            raise ValueError("project title is required")
        project_id = project_id or new_id("project")
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO projects(project_id,title,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (project_id, title.strip(), canonical_json(payload or {}), now, now),
            )
        return project_id

    def add_scenario(
        self,
        project_id: str,
        scenario_id: str,
        payload: dict[str, Any],
        *,
        required: bool = True,
        safeguard: bool = False,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO scenarios(scenario_id,project_id,required,safeguard,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    scenario_id,
                    project_id,
                    int(required),
                    int(safeguard),
                    canonical_json(payload),
                    _now(),
                ),
            )

    def add_portfolio(
        self,
        project_id: str,
        option_ids: Sequence[str],
        *,
        payload: dict[str, Any] | None = None,
        portfolio_id: str | None = None,
    ) -> str:
        if not option_ids or any(not option_id for option_id in option_ids):
            raise ValueError("portfolio must contain one stable option ID per site")
        portfolio_id = portfolio_id or new_id("portfolio")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO portfolios(portfolio_id,project_id,option_ids_json,payload_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    portfolio_id,
                    project_id,
                    canonical_json(list(option_ids)),
                    canonical_json(payload or {}),
                    _now(),
                ),
            )
        return portfolio_id

    def create_analysis(
        self,
        project_id: str,
        scenario_ids: Sequence[str],
        *,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        analysis_id: str | None = None,
    ) -> str:
        if not scenario_ids or len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("analysis requires a unique, non-empty scenario ensemble")
        analysis_id = analysis_id or new_id("analysis")
        now = _now()
        with self.transaction() as connection:
            owned = connection.execute(
                f"SELECT scenario_id FROM scenarios WHERE project_id=? "
                f"AND scenario_id IN ({','.join('?' for _ in scenario_ids)})",
                (project_id, *scenario_ids),
            ).fetchall()
            if {row["scenario_id"] for row in owned} != set(scenario_ids):
                raise ValueError("all analysis scenarios must belong to the project")
            connection.execute(
                "INSERT INTO analyses(analysis_id,project_id,state,idempotency_key,payload_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    analysis_id,
                    project_id,
                    AnalysisState.DRAFT.value,
                    idempotency_key,
                    canonical_json(payload or {}),
                    now,
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO analysis_scenarios(analysis_id,scenario_id) VALUES(?,?)",
                [(analysis_id, scenario_id) for scenario_id in scenario_ids],
            )
        return analysis_id

    def transition_analysis(self, analysis_id: str, target: AnalysisState | str) -> None:
        destination = AnalysisState(target)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if row is None:
                raise KeyError(analysis_id)
            assert_transition(row["state"], destination, kind="analysis")
            connection.execute(
                "UPDATE analyses SET state=?,updated_at=? WHERE analysis_id=?",
                (destination.value, _now(), analysis_id),
            )

    def plan_direct_job(
        self,
        analysis_id: str,
        portfolio_id: str,
        scenario_id: str,
        model_run_cache_key: str,
        *,
        payload: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> str:
        require_sha256(model_run_cache_key, label="model_run_cache_key")
        job_id = job_id or new_id("job")
        now = _now()
        with self.transaction() as connection:
            ownership = connection.execute(
                "SELECT a.project_id AS analysis_project,p.project_id AS portfolio_project,"
                "s.project_id AS scenario_project,axs.scenario_id AS selected_scenario "
                "FROM analyses a JOIN portfolios p ON p.portfolio_id=? "
                "JOIN scenarios s ON s.scenario_id=? "
                "LEFT JOIN analysis_scenarios axs ON axs.analysis_id=a.analysis_id "
                "AND axs.scenario_id=s.scenario_id WHERE a.analysis_id=?",
                (portfolio_id, scenario_id, analysis_id),
            ).fetchone()
            if ownership is None or ownership["selected_scenario"] is None:
                raise ValueError("job scenario is not part of the analysis ensemble")
            if (
                len(
                    {
                        ownership["analysis_project"],
                        ownership["portfolio_project"],
                        ownership["scenario_project"],
                    }
                )
                != 1
            ):
                raise ValueError("job analysis, portfolio, and scenario must share a project")
            existing = connection.execute(
                "SELECT job_id,model_run_cache_key FROM direct_jobs "
                "WHERE analysis_id=? AND portfolio_id=? AND scenario_id=?",
                (analysis_id, portfolio_id, scenario_id),
            ).fetchone()
            if existing is not None:
                if existing["model_run_cache_key"] != model_run_cache_key:
                    raise ValueError("idempotent job plan changed its immutable cache key")
                return str(existing["job_id"])
            connection.execute(
                "INSERT INTO direct_jobs(job_id,analysis_id,portfolio_id,scenario_id,state,"
                "model_run_cache_key,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    analysis_id,
                    portfolio_id,
                    scenario_id,
                    DirectJobState.PLANNED.value,
                    model_run_cache_key,
                    canonical_json(payload or {}),
                    now,
                    now,
                ),
            )
        return job_id

    def transition_direct_job(self, job_id: str, target: DirectJobState | str) -> None:
        destination = DirectJobState(target)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM direct_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            assert_transition(row["state"], destination, kind="direct_job")
            connection.execute(
                "UPDATE direct_jobs SET state=?,updated_at=? WHERE job_id=?",
                (destination.value, _now(), job_id),
            )

    @staticmethod
    def _validate_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
        required = {"role", "sha256", "relative_path", "media_type", "bytes"}
        if missing := required - set(artifact):
            raise ValueError(f"artifact is missing fields: {', '.join(sorted(missing))}")
        require_sha256(str(artifact["sha256"]), label="artifact sha256")
        path = PurePosixPath(str(artifact["relative_path"]))
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("artifact path must be contained and relative")
        if int(artifact["bytes"]) < 0:
            raise ValueError("artifact byte count cannot be negative")
        return artifact

    def _insert_outbox(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        analysis_id: str | None,
        event_type: str,
        subject_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> str:
        if not event_type or not subject_id or not idempotency_key:
            raise ValueError("outbox event, subject, and idempotency key are required")
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()
        existing = connection.execute(
            "SELECT outbox_id,payload_sha256,event_type,subject_id FROM telemetry_outbox "
            "WHERE project_id=? AND idempotency_key=?",
            (project_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if (
                existing["payload_sha256"] != payload_sha256
                or existing["event_type"] != event_type
                or existing["subject_id"] != subject_id
            ):
                raise ValueError("idempotency key was reused for a different telemetry envelope")
            return str(existing["outbox_id"])
        outbox_id = new_id("outbox")
        now = _now()
        connection.execute(
            "INSERT INTO telemetry_outbox(outbox_id,project_id,analysis_id,event_type,subject_id,"
            "payload_json,payload_sha256,idempotency_key,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                outbox_id,
                project_id,
                analysis_id,
                event_type,
                subject_id,
                payload_json,
                payload_sha256,
                idempotency_key,
                "PENDING",
                now,
                now,
            ),
        )
        return outbox_id

    def finalize_direct_job(
        self,
        job_id: str,
        target: DirectJobState | str,
        *,
        artifacts: Sequence[dict[str, Any]],
        metrics: Sequence[dict[str, Any]],
        qa_findings: Sequence[dict[str, Any]],
        evidence_nodes: Sequence[dict[str, Any]],
        evidence_edges: Sequence[dict[str, Any]],
        telemetry_payload: dict[str, Any],
        telemetry_idempotency_key: str,
    ) -> str:
        """Finalize science and enqueue its cloud mirror in one transaction."""

        destination = DirectJobState(target)
        if destination not in {
            DirectJobState.SUCCEEDED,
            DirectJobState.QA_REJECTED,
            DirectJobState.FAILED,
            DirectJobState.CANCELLED,
        }:
            raise ValueError("finalize_direct_job requires a terminal state")
        validated_artifacts = [self._validate_artifact(dict(item)) for item in artifacts]
        for metric in metrics:
            if not math.isfinite(float(metric["value"])) or not metric.get("metric_id"):
                raise ValueError("metric values must be finite and identified")
            if not metric.get("units"):
                raise ValueError("metric units are required")
        if destination is not DirectJobState.SUCCEEDED and (
            metrics
            or any(
                bool(node.get("is_claim"))
                or str(node.get("node_type", "")).casefold() in {"claim", "evidence_claim"}
                for node in evidence_nodes
            )
        ):
            raise ValueError("only successful direct jobs may produce metrics or evidence claims")
        finalization = {
            "target": destination.value,
            "artifacts": list(validated_artifacts),
            "metrics": list(metrics),
            "qa_findings": list(qa_findings),
            "evidence_nodes": list(evidence_nodes),
            "evidence_edges": list(evidence_edges),
            "telemetry_payload": telemetry_payload,
            "telemetry_idempotency_key": telemetry_idempotency_key,
        }
        finalization_digest = _digest(finalization)
        with self.transaction() as connection:
            job = connection.execute(
                "SELECT j.*,a.project_id FROM direct_jobs j JOIN analyses a "
                "ON a.analysis_id=j.analysis_id WHERE j.job_id=?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["finalization_digest"] is not None:
                if job["finalization_digest"] != finalization_digest:
                    raise ValueError(
                        "terminal direct job cannot be finalized with different evidence"
                    )
                outbox = connection.execute(
                    "SELECT outbox_id FROM telemetry_outbox WHERE project_id=? AND idempotency_key=?",
                    (job["project_id"], telemetry_idempotency_key),
                ).fetchone()
                if outbox is None:
                    raise RuntimeError("atomic finalization invariant is broken: outbox is missing")
                return str(outbox["outbox_id"])
            assert_transition(job["state"], destination, kind="direct_job")
            connection.execute(
                "UPDATE direct_jobs SET state=?,finalization_digest=?,updated_at=? WHERE job_id=?",
                (destination.value, finalization_digest, _now(), job_id),
            )
            for artifact in validated_artifacts:
                connection.execute(
                    "INSERT INTO direct_job_artifacts(artifact_id,job_id,role,sha256,relative_path,"
                    "media_type,bytes,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        artifact.get("artifact_id") or new_id("artifact"),
                        job_id,
                        artifact["role"],
                        artifact["sha256"],
                        artifact["relative_path"],
                        artifact["media_type"],
                        int(artifact["bytes"]),
                        _now(),
                    ),
                )
            for metric in metrics:
                connection.execute(
                    "INSERT INTO direct_job_metrics(job_id,metric_id,object_id,value,units) "
                    "VALUES(?,?,?,?,?)",
                    (
                        job_id,
                        metric["metric_id"],
                        metric.get("object_id", ""),
                        float(metric["value"]),
                        metric["units"],
                    ),
                )
            for finding in qa_findings:
                connection.execute(
                    "INSERT INTO direct_job_qa(finding_id,job_id,code,severity,payload_json) "
                    "VALUES(?,?,?,?,?)",
                    (
                        finding.get("finding_id") or new_id("finding"),
                        job_id,
                        finding["code"],
                        finding["severity"],
                        canonical_json(finding.get("payload", {})),
                    ),
                )
            node_ids: set[str] = set()
            for node in evidence_nodes:
                node_id = node.get("node_id") or new_id("evidence")
                if node_id in node_ids:
                    raise ValueError("evidence node IDs must be unique within a finalization")
                node_ids.add(node_id)
                node_type = str(node["node_type"])
                is_claim = bool(node.get("is_claim")) or node_type.casefold() in {
                    "claim",
                    "evidence_claim",
                }
                connection.execute(
                    "INSERT INTO evidence_nodes(node_id,job_id,node_type,is_claim,payload_json) "
                    "VALUES(?,?,?,?,?)",
                    (
                        node_id,
                        job_id,
                        node_type,
                        int(is_claim),
                        canonical_json(node.get("payload", {})),
                    ),
                )
            for edge in evidence_edges:
                if edge["source_node_id"] not in node_ids or edge["target_node_id"] not in node_ids:
                    raise ValueError("evidence edges must reference nodes in the same direct job")
                connection.execute(
                    "INSERT INTO evidence_edges(edge_id,job_id,source_node_id,target_node_id,relation) "
                    "VALUES(?,?,?,?,?)",
                    (
                        edge.get("edge_id") or new_id("edge"),
                        job_id,
                        edge["source_node_id"],
                        edge["target_node_id"],
                        edge["relation"],
                    ),
                )
            if destination is DirectJobState.SUCCEEDED:
                connection.execute(
                    "INSERT INTO model_run_cache_entries(model_run_cache_key,source_job_id,"
                    "finalization_digest,created_at) VALUES(?,?,?,?)",
                    (
                        job["model_run_cache_key"],
                        job_id,
                        finalization_digest,
                        _now(),
                    ),
                )
            return self._insert_outbox(
                connection,
                project_id=job["project_id"],
                analysis_id=job["analysis_id"],
                event_type="direct_job.finalized",
                subject_id=job_id,
                payload=telemetry_payload,
                idempotency_key=telemetry_idempotency_key,
            )

    def record_producer(
        self,
        project_id: str,
        producer_kind: Literal[
            "bundle_import", "guided_session", "analysis_completion", "evidence_export"
        ],
        subject_id: str,
        payload: dict[str, Any],
        *,
        telemetry_payload: dict[str, Any],
        telemetry_idempotency_key: str,
        analysis_id: str | None = None,
    ) -> tuple[str, str]:
        """Atomically persist a non-job producer snapshot and telemetry envelope."""

        record_id = new_id("producer")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT record_id,payload_json FROM producer_records "
                "WHERE project_id=? AND producer_kind=? AND subject_id=?",
                (project_id, producer_kind, subject_id),
            ).fetchone()
            payload_json = canonical_json(payload)
            if existing is not None:
                if existing["payload_json"] != payload_json:
                    raise ValueError(
                        "producer subject was reused with a different immutable payload"
                    )
                record_id = str(existing["record_id"])
            else:
                if producer_kind == "analysis_completion":
                    if analysis_id is None:
                        raise ValueError("analysis completion requires analysis_id")
                    analysis = connection.execute(
                        "SELECT project_id,state FROM analyses WHERE analysis_id=?",
                        (analysis_id,),
                    ).fetchone()
                    if analysis is None or analysis["project_id"] != project_id:
                        raise ValueError("completed analysis must belong to the producer project")
                    assert_transition(analysis["state"], AnalysisState.COMPLETED, kind="analysis")
                    connection.execute(
                        "UPDATE analyses SET state='COMPLETED',updated_at=? WHERE analysis_id=?",
                        (_now(), analysis_id),
                    )
                connection.execute(
                    "INSERT INTO producer_records(record_id,project_id,analysis_id,producer_kind,"
                    "subject_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        record_id,
                        project_id,
                        analysis_id,
                        producer_kind,
                        subject_id,
                        payload_json,
                        _now(),
                    ),
                )
            outbox_id = self._insert_outbox(
                connection,
                project_id=project_id,
                analysis_id=analysis_id,
                event_type=f"{producer_kind}.recorded",
                subject_id=subject_id,
                payload=telemetry_payload,
                idempotency_key=telemetry_idempotency_key,
            )
        return record_id, outbox_id

    def recover_expired_outbox_leases(self, *, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)).isoformat()
        with self.transaction() as connection:
            return int(
                connection.execute(
                    "UPDATE telemetry_outbox SET state='PENDING',lease_token=NULL,"
                    "lease_expires_at=NULL,last_error_code='UPLOAD_LEASE_EXPIRED',updated_at=? "
                    "WHERE state='UPLOADING' AND lease_expires_at IS NOT NULL "
                    "AND lease_expires_at<=?",
                    (cutoff, cutoff),
                ).rowcount
            )

    def claim_outbox(self, *, limit: int = 100, lease_seconds: int = 300) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("outbox claim limit must be positive")
        if lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("outbox lease must be between 1 second and 24 hours")
        with self.transaction() as connection:
            claimed_at = datetime.now(UTC)
            now = claimed_at.isoformat()
            connection.execute(
                "UPDATE telemetry_outbox SET state='PENDING',lease_token=NULL,"
                "lease_expires_at=NULL,last_error_code='UPLOAD_LEASE_EXPIRED',updated_at=? "
                "WHERE state='UPLOADING' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at<=?",
                (now, now),
            )
            rows = connection.execute(
                "SELECT * FROM telemetry_outbox WHERE state IN ('PENDING','FAILED') "
                "ORDER BY created_at,outbox_id LIMIT ?",
                (limit,),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                token = secrets.token_urlsafe(24)
                expires_at = (claimed_at + timedelta(seconds=lease_seconds)).isoformat()
                updated = connection.execute(
                    "UPDATE telemetry_outbox SET state='UPLOADING',attempt_count=attempt_count+1,"
                    "lease_token=?,lease_expires_at=?,updated_at=? WHERE outbox_id=? "
                    "AND state IN ('PENDING','FAILED')",
                    (token, expires_at, now, row["outbox_id"]),
                ).rowcount
                if updated == 1:
                    claimed.append(
                        {
                            **dict(row),
                            "state": "UPLOADING",
                            "attempt_count": int(row["attempt_count"]) + 1,
                            "lease_token": token,
                            "lease_expires_at": expires_at,
                            "updated_at": now,
                        }
                    )
        return claimed

    def mark_outbox_synced(
        self,
        outbox_id: str,
        external_reference: dict[str, Any],
        *,
        lease_token: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            clause = "outbox_id=? AND state='UPLOADING'"
            parameters: list[Any] = [canonical_json(external_reference), _now(), outbox_id]
            if lease_token is not None:
                clause += " AND lease_token=?"
                parameters.append(lease_token)
            updated = connection.execute(
                "UPDATE telemetry_outbox SET state='SYNCED',external_reference_json=?,"
                "last_error_code=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE "
                + clause,
                parameters,
            ).rowcount
            if updated != 1:
                raise ValueError("only the active outbox lease may be marked synced")

    def mark_outbox_failed(
        self,
        outbox_id: str,
        error_code: str,
        *,
        blocked: bool = False,
        lease_token: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            clause = "outbox_id=? AND state='UPLOADING'"
            parameters: list[Any] = [
                "BLOCKED" if blocked else "FAILED",
                error_code,
                _now(),
                outbox_id,
            ]
            if lease_token is not None:
                clause += " AND lease_token=?"
                parameters.append(lease_token)
            updated = connection.execute(
                "UPDATE telemetry_outbox SET state=?,last_error_code=?,lease_token=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE " + clause,
                parameters,
            ).rowcount
            if updated != 1:
                raise ValueError("only the active outbox lease may fail")

    def recover_abandoned(self) -> dict[str, int]:
        """Requeue process-owned states after a worker crash."""

        with self.transaction() as connection:
            jobs = connection.execute(
                "UPDATE direct_jobs SET state='QUEUED',attempt=attempt+1,updated_at=? "
                "WHERE state='RUNNING'",
                (_now(),),
            ).rowcount
            outbox = connection.execute(
                "UPDATE telemetry_outbox SET state='PENDING',lease_token=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE state='UPLOADING'",
                (_now(),),
            ).rowcount
        return {"direct_jobs": jobs, "outbox_items": outbox}

    def next_event_sequence(self, stream_id: str) -> int:
        if not stream_id:
            raise ValueError("stream_id is required")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT last_sequence FROM event_sequences WHERE stream_id=?", (stream_id,)
            ).fetchone()
            sequence = (int(row["last_sequence"]) if row else 0) + 1
            connection.execute(
                "INSERT INTO event_sequences(stream_id,last_sequence) VALUES(?,?) "
                "ON CONFLICT(stream_id) DO UPDATE SET last_sequence=excluded.last_sequence",
                (stream_id, sequence),
            )
        return sequence

    def comparable_portfolio_ids(self, analysis_id: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT portfolio_id FROM comparable_portfolios WHERE analysis_id=? ORDER BY portfolio_id",
            (analysis_id,),
        ).fetchall()
        return [str(row["portfolio_id"]) for row in rows]

    def cache_source(self, model_run_cache_key: str) -> dict[str, Any] | None:
        require_sha256(model_run_cache_key, label="model_run_cache_key")
        row = self._connection.execute(
            "SELECT * FROM model_run_cache_entries WHERE model_run_cache_key=?",
            (model_run_cache_key,),
        ).fetchone()
        return dict(row) if row else None

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM direct_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def get_analysis(self, analysis_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM analyses WHERE analysis_id=?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise KeyError(analysis_id)
        return dict(row)

    def count(self, table: str) -> int:
        allowed = {
            "projects",
            "scenarios",
            "portfolios",
            "analyses",
            "direct_jobs",
            "direct_job_artifacts",
            "direct_job_metrics",
            "direct_job_qa",
            "evidence_nodes",
            "evidence_edges",
            "model_run_cache_entries",
            "producer_records",
            "telemetry_outbox",
        }
        if table not in allowed:
            raise ValueError("table is not in the diagnostic allowlist")
        return int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
