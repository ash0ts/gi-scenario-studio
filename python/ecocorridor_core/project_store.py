"""Authoritative EcoCorridor project state persisted in project-local SQLite."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from scenario_platform.cloud_sync import CloudUploadReceipt
from scenario_platform.contracts import (
    AnalysisState,
    DirectJobState,
    assert_transition,
    canonical_json,
)
from scenario_platform.storage import PathSecurityError, contained_path

from .contracts import (
    CURRENT_RASTER_LIMITATION,
    FITNESS_LIMITATION,
    AssetKind,
    ComputationalResourceObservation,
    ConnectivityAsset,
    ConnectivityEvidenceClaim,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    DirectConnectivityRun,
    EvidenceEdge,
    EvidenceNode,
    ModelRevision,
    OversizeSyncApproval,
    PortfolioDecisionSummary,
    QAStatus,
    ReferenceComparisonPlan,
    ReferenceComparisonResult,
    ReferencePairComparison,
    ReferenceScenarioComparison,
    SemanticChange,
    SemanticModelDiff,
    canonical_sha256,
)
from .engine import (
    EngineProtocolResult,
    PreparedConnectivityRun,
    PreparedPair,
    engine_request_payload,
    parse_circuitscape_matrix,
    validate_effective_circuitscape_configuration,
)
from .footprints import ProfessionalFootprintProofDocument
from .governance import oversize_sync_approval_event_payload
from .interventions import canonicalize_portfolio
from .metrics import (
    CONNECTIVITY_LIMITATION,
    validate_evidence_claim,
    validate_portfolio_disposition_support,
    verify_direct_claim_against_retained_evidence,
)
from .reference_comparison import assemble_reference_comparison_result
from .reference_matrix_parser import (
    REFERENCE_MATRIX_PARSER_SHA256,
    parse_reference_matrix_bytes,
)
from .reference_workflow import (
    build_reference_comparison_plan,
    reference_evidence_claim_links,
    reference_result_id,
    reference_scenario_comparison_id,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_verified_regular_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_byte_size: int,
    capture_bytes: bool,
) -> bytes | None:
    """Hash one descriptor-backed read and optionally retain those exact bytes.

    Path checks performed before ``open`` are inherently racy.  Open the
    resolved path without following a final-component symlink where the host
    supports it, reconcile the descriptor with ``lstat``, and hash only bytes
    read from that descriptor.  Matrix parsing receives the captured bytes;
    it never reopens the mutable path after checksum verification.
    """

    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError("retained evidence bytes are absent") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ValueError("retained evidence path is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("retained evidence path could not be opened safely") from exc
    digest = hashlib.sha256()
    captured: list[bytes] | None = [] if capture_bytes else None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise ValueError("retained evidence path changed before descriptor capture")
        byte_count = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            byte_count += len(block)
            digest.update(block)
            if captured is not None:
                captured.append(block)
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(opened, field) != getattr(after, field) for field in stable_fields):
            raise ValueError("retained evidence changed during descriptor capture")
    finally:
        os.close(descriptor)
    if byte_count != expected_byte_size or digest.hexdigest() != expected_sha256:
        raise ValueError("retained evidence bytes failed their checksum")
    return b"".join(captured) if captured is not None else None


def _assumption_node_id(limitation: str) -> str:
    return f"assumption:{hashlib.sha256(limitation.encode('utf-8')).hexdigest()}"


_RENDERER_LOCAL_PATH = re.compile(
    r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\|/(?:Users|home|var|private|tmp|opt|etc)/)"
    r"[^\s\"']+"
)


def _assert_renderer_safe(value: Any) -> None:
    """Reject internal paths before an Evidence Rail projection leaves the worker."""

    if isinstance(value, dict):
        forbidden_keys = {
            "cas_relative_path",
            "relative_path",
            "source_path",
            "project_directory",
            "work_directory",
        }
        if forbidden_keys.intersection(value):
            raise ValueError("renderer evidence contains an internal path field")
        for item in value.values():
            _assert_renderer_safe(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_renderer_safe(item)
    elif isinstance(value, Path) or (isinstance(value, str) and _RENDERER_LOCAL_PATH.search(value)):
        raise ValueError("renderer evidence contains a local filesystem path")


@dataclass(frozen=True, slots=True)
class VerifiedDirectRunEvidence:
    """Fully revalidated local chain for one successful durable direct run.

    Paths remain worker-local. Callers may use them for a second descriptor-backed
    export/upload read, but they must never serialize this object across RPC.
    """

    run: DirectConnectivityRun
    prepared: PreparedConnectivityRun
    artifacts: tuple[dict[str, Any], ...]
    artifact_paths_by_role: dict[str, Path]
    evidence_nodes: tuple[dict[str, Any], ...]
    qa_status: QAStatus
    qa_findings: tuple[str, ...]
    resource_observation: ComputationalResourceObservation | None
    terminal_sha256: str
    phase: str
    cached: bool


@dataclass(frozen=True, slots=True)
class ReferenceValidationSeal:
    """Lightweight database identity captured around heavy reference verification."""

    project_id: str
    claims_by_id: dict[str, ConnectivityEvidenceClaim]
    persistence_sha256: str
    verified_runs: dict[str, VerifiedDirectRunEvidence]


_ASSISTANT_AUDIT_ERROR = re.compile(r"[A-Z][A-Z0-9_]{2,127}\Z")


def _legacy_outbox_stream_id(row: sqlite3.Row) -> str:
    """Derive the stable W&B run stream for pre-sequence outbox rows.

    This is used only by the schema-109 migration. New producer calls supply
    the stream identity explicitly so future event types cannot silently join
    the wrong provider run.
    """

    event_type = str(row["event_type"])
    project_id = str(row["project_id"])
    analysis_id = str(row["analysis_id"] or "")
    subject_id = str(row["subject_id"])
    if event_type in {"analysis_plan", "direct_job_terminal", "analysis_completion"}:
        if not analysis_id:
            raise RuntimeError("analysis telemetry row has no analysis identity")
        return f"connectivity_analysis:{analysis_id}"
    if event_type in {"semantic_revision", "model_attestation"}:
        return f"planning_model:{project_id}"
    if event_type == "bundle_import":
        return f"bundle_import:{project_id}"
    if event_type == "guided_session":
        return f"guided_session:{subject_id}"
    if event_type == "guided_checkpoint":
        try:
            session_id = str(json.loads(str(row["payload_json"]))["session_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("guided telemetry row has no session identity") from exc
        return f"guided_session:{session_id}"
    if event_type == "evidence_export":
        return f"evidence_export:{subject_id}"
    raise RuntimeError(f"cannot migrate unsupported telemetry event: {event_type}")


def _reference_completion_manifest(
    *,
    plan: ReferenceComparisonPlan,
    direct_runs: Sequence[dict[str, Any]],
) -> tuple[str, dict[str, Any], str]:
    """Build the deterministic producer identity for a completed direct ensemble."""

    ordered = sorted(
        (
            {
                "direct_run_id": str(item["direct_run_id"]),
                "portfolio_id": str(item["portfolio_id"]),
                "scenario_id": str(item["scenario_id"]),
                "terminal_sha256": str(item["terminal_sha256"]),
                "result_sha256": str(item["result_sha256"]),
            }
            for item in direct_runs
        ),
        key=lambda item: (item["portfolio_id"], item["scenario_id"], item["direct_run_id"]),
    )
    payload = {
        "schema_version": "ecocorridor/reference-completion/v1.0",
        "analysis_id": plan.analysis_id,
        "plan_id": plan.plan_id,
        "revision_id": plan.revision_id,
        "model_sha256": plan.model_sha256,
        "engine_manifest_sha256": plan.engine_manifest_sha256,
        "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
        "direct_runs": ordered,
    }
    payload_json = canonical_json(payload)
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    completion_id = f"reference_completion_{payload_sha256[:24]}"
    return completion_id, payload, payload_sha256


class SolverShutdownReceiptError(RuntimeError):
    """A potentially live orphan solver prevents automatic job recovery."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS eco_projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('IMPORTED','PLANNING','GUIDED')),
    skeleton_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_records (
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,record_type,record_id)
);
CREATE TABLE IF NOT EXISTS eco_record_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(project_id,record_type,record_id,revision)
);
CREATE TABLE IF NOT EXISTS eco_analyses (
    analysis_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    analysis_kind TEXT NOT NULL DEFAULT 'ALTERNATIVES'
      CHECK(analysis_kind IN ('ALTERNATIVES','REFERENCE')),
    state TEXT NOT NULL CHECK(state IN (
      'DRAFT','VALIDATING','READY','RUNNING','PAUSING','PAUSED','COMPLETED','FAILED','CANCELLED'
    )),
    plan_json TEXT NOT NULL,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_analysis_models (
    analysis_id TEXT PRIMARY KEY REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    bundle_sha256 TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_analysis_runs (
    direct_run_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    portfolio_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state = 'SUCCEEDED'),
    model_run_cache_key TEXT NOT NULL,
    result_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(analysis_id,portfolio_id,scenario_id)
);
CREATE TABLE IF NOT EXISTS eco_direct_jobs (
    direct_job_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    portfolio_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('MATRIX','MAP')),
    sequence_number INTEGER NOT NULL CHECK(sequence_number >= 0),
    state TEXT NOT NULL CHECK(state IN (
      'PLANNED','QUEUED','RUNNING','SUCCEEDED','QA_REJECTED','FAILED','CANCELLED'
    )),
    emit_current_map INTEGER NOT NULL CHECK(emit_current_map IN (0,1)),
    prepared_json TEXT,
    model_run_cache_key TEXT,
    terminal_sha256 TEXT,
    resource_observation_json TEXT,
    error_json TEXT,
    cached INTEGER NOT NULL DEFAULT 0 CHECK(cached IN (0,1)),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(analysis_id,portfolio_id,scenario_id,phase),
    UNIQUE(analysis_id,sequence_number)
);
CREATE INDEX IF NOT EXISTS eco_direct_jobs_claim_idx
ON eco_direct_jobs(analysis_id,state,sequence_number);
CREATE TABLE IF NOT EXISTS eco_run_artifacts (
    direct_run_id TEXT NOT NULL REFERENCES eco_analysis_runs(direct_run_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    cas_relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    PRIMARY KEY(direct_run_id,role)
);
CREATE TABLE IF NOT EXISTS eco_terminal_job_artifacts (
    direct_job_id TEXT NOT NULL REFERENCES eco_direct_jobs(direct_job_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    cas_relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    PRIMARY KEY(direct_job_id,role)
);
CREATE TABLE IF NOT EXISTS eco_run_metrics (
    direct_run_id TEXT NOT NULL REFERENCES eco_analysis_runs(direct_run_id) ON DELETE CASCADE,
    pair_id TEXT NOT NULL,
    effective_resistance REAL NOT NULL CHECK(effective_resistance > 0),
    units TEXT NOT NULL CHECK(units = 'effective_resistance'),
    PRIMARY KEY(direct_run_id,pair_id)
);
CREATE TABLE IF NOT EXISTS eco_run_qa (
    direct_run_id TEXT PRIMARY KEY REFERENCES eco_analysis_runs(direct_run_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('ACCEPTED','WARNING_ACCEPTED')),
    findings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_run_evidence (
    direct_run_id TEXT NOT NULL REFERENCES eco_analysis_runs(direct_run_id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(direct_run_id,node_id)
);
CREATE TABLE IF NOT EXISTS eco_model_run_cache (
    model_run_cache_key TEXT PRIMARY KEY,
    result_sha256 TEXT NOT NULL,
    source_direct_run_id TEXT NOT NULL REFERENCES eco_analysis_runs(direct_run_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_analysis_summaries (
    analysis_id TEXT NOT NULL REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    portfolio_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(analysis_id,portfolio_id)
);
CREATE TABLE IF NOT EXISTS eco_evidence_claims (
    claim_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_reference_comparison_plans (
    plan_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL,
    model_sha256 TEXT NOT NULL,
    engine_manifest_sha256 TEXT NOT NULL,
    reference_matrix_parser_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id,analysis_id)
);
CREATE TABLE IF NOT EXISTS eco_reference_completion_intents (
    completion_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL UNIQUE REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(plan_id,analysis_id)
      REFERENCES eco_reference_comparison_plans(plan_id,analysis_id)
);
CREATE TABLE IF NOT EXISTS eco_reference_comparison_results (
    result_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    model_sha256 TEXT NOT NULL,
    engine_manifest_sha256 TEXT NOT NULL,
    reference_matrix_parser_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PASSED','FAILED')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(analysis_id,plan_id),
    FOREIGN KEY(plan_id,analysis_id)
      REFERENCES eco_reference_comparison_plans(plan_id,analysis_id)
);
CREATE TABLE IF NOT EXISTS eco_reference_comparison_claims (
    claim_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL REFERENCES eco_reference_comparison_results(result_id)
      ON DELETE CASCADE,
    analysis_id TEXT NOT NULL REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    comparison_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    direct_run_id TEXT NOT NULL REFERENCES eco_analysis_runs(direct_run_id),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(result_id,comparison_id,scenario_id,pair_id,direct_run_id)
);
CREATE TABLE IF NOT EXISTS eco_observability (
    project_id TEXT PRIMARY KEY REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_synthetic_trace_bindings (
    project_id TEXT PRIMARY KEY REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256) = 64),
    payload_json TEXT NOT NULL,
    connected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_guided_sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    projection_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_guided_checkpoints (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES eco_guided_sessions(session_id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL CHECK(sequence_number > 0),
    checkpoint_id TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK(event_kind IN (
      'ENTERED','COMPLETED','BACKTRACKED','EVIDENCE_OPENED',
      'ALTERNATIVE_COMPARED','RERUN_REQUESTED','EXPORT_COMPLETED'
    )),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id,sequence_number)
);
CREATE TABLE IF NOT EXISTS eco_assistant_records (
    call_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    answer_json TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_assistant_rejection_audits (
    call_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK(operation IN ('evidence.explain','evidence_intake.extract')),
    error_code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_review_brief_drafts (
    draft_id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    model_revision_id TEXT NOT NULL,
    decision_board_sha256 TEXT NOT NULL,
    supersedes_draft_id TEXT UNIQUE REFERENCES eco_review_brief_drafts(draft_id),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    runner_json TEXT NOT NULL,
    runner_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS eco_review_brief_scope_idx
ON eco_review_brief_drafts(project_id,analysis_id,portfolio_id,created_at,draft_id);
CREATE UNIQUE INDEX IF NOT EXISTS eco_review_brief_draft_project_key
ON eco_review_brief_drafts(draft_id,project_id);
CREATE TABLE IF NOT EXISTS eco_review_brief_items (
    draft_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_sequence INTEGER NOT NULL CHECK(item_sequence > 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(
      length(payload_sha256)=64
      AND payload_sha256=lower(payload_sha256)
      AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY(draft_id,item_id),
    UNIQUE(draft_id,item_sequence),
    FOREIGN KEY(draft_id,project_id)
      REFERENCES eco_review_brief_drafts(draft_id,project_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS eco_review_brief_finding_selections (
    draft_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    finding_alias TEXT NOT NULL,
    item_id TEXT NOT NULL,
    selection_sequence INTEGER NOT NULL CHECK(selection_sequence > 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(
      length(payload_sha256)=64
      AND payload_sha256=lower(payload_sha256)
      AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY(draft_id,finding_alias),
    UNIQUE(draft_id,item_id),
    UNIQUE(draft_id,selection_sequence),
    FOREIGN KEY(draft_id,project_id)
      REFERENCES eco_review_brief_drafts(draft_id,project_id) ON DELETE CASCADE,
    FOREIGN KEY(draft_id,item_id)
      REFERENCES eco_review_brief_items(draft_id,item_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS eco_review_brief_evidence_edges (
    edge_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    edge_sequence INTEGER NOT NULL CHECK(edge_sequence > 0),
    edge_kind TEXT NOT NULL CHECK(edge_kind IN ('EVIDENCE_CLAIM','DIRECT_RUN','SCENARIO')),
    target_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(
      length(payload_sha256)=64
      AND payload_sha256=lower(payload_sha256)
      AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE(draft_id,item_id,edge_sequence),
    UNIQUE(draft_id,item_id,edge_kind,target_id),
    FOREIGN KEY(draft_id,project_id)
      REFERENCES eco_review_brief_drafts(draft_id,project_id) ON DELETE CASCADE,
    FOREIGN KEY(draft_id,item_id)
      REFERENCES eco_review_brief_items(draft_id,item_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS eco_review_brief_agent_executions (
    execution_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL UNIQUE,
    call_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL CHECK(
      length(snapshot_sha256)=64
      AND snapshot_sha256=lower(snapshot_sha256)
      AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(
      length(payload_sha256)=64
      AND payload_sha256=lower(payload_sha256)
      AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY(draft_id,project_id)
      REFERENCES eco_review_brief_drafts(draft_id,project_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS eco_review_brief_tool_receipts (
    execution_id TEXT NOT NULL REFERENCES eco_review_brief_agent_executions(execution_id)
      ON DELETE CASCADE,
    tool_sequence INTEGER NOT NULL CHECK(tool_sequence > 0),
    tool_name TEXT NOT NULL CHECK(tool_name IN (
      'review_brief_get_scope','review_brief_list_findings','review_brief_get_findings',
      'review_brief_list_option_constraints','review_brief_list_model_fitness_gaps',
      'review_brief_resolve_evidence_aliases','review_brief_get_reference_status',
      'review_brief_get_authorization_status'
    )),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(
      length(payload_sha256)=64
      AND payload_sha256=lower(payload_sha256)
      AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY(execution_id,tool_sequence)
);
CREATE TABLE IF NOT EXISTS eco_review_brief_validations (
    draft_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(
      length(payload_sha256)=64
      AND payload_sha256=lower(payload_sha256)
      AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY(draft_id,project_id)
      REFERENCES eco_review_brief_drafts(draft_id,project_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS eco_review_brief_trace_references (
    draft_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    weave_call_id TEXT NOT NULL,
    wandb_run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(
      length(payload_sha256)=64
      AND payload_sha256=lower(payload_sha256)
      AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    UNIQUE(project_id,trace_id),
    UNIQUE(project_id,weave_call_id),
    FOREIGN KEY(draft_id,project_id)
      REFERENCES eco_review_brief_drafts(draft_id,project_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS eco_review_brief_rejections (
    call_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    error_code TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_review_brief_attestations (
    acknowledgement_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL UNIQUE REFERENCES eco_review_brief_drafts(draft_id),
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_review_brief_feedback (
    feedback_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES eco_review_brief_drafts(draft_id),
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_exports (
    export_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT NOT NULL REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eco_solver_shutdown_receipts (
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT NOT NULL REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK(generation > 0),
    receipt_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('UNUSED','CONSUMED')),
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    PRIMARY KEY(project_id,analysis_id,generation),
    UNIQUE(project_id,analysis_id,receipt_sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS eco_one_unused_solver_shutdown_receipt
ON eco_solver_shutdown_receipts(project_id,analysis_id)
WHERE state='UNUSED';
CREATE TABLE IF NOT EXISTS eco_telemetry_outbox (
    outbox_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    analysis_id TEXT REFERENCES eco_analyses(analysis_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    stream_sequence INTEGER NOT NULL CHECK(stream_sequence > 0),
    outbox_sequence INTEGER NOT NULL CHECK(outbox_sequence > 0),
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
    UNIQUE(project_id,idempotency_key),
    UNIQUE(project_id,stream_id,stream_sequence),
    UNIQUE(project_id,outbox_sequence)
);
CREATE TABLE IF NOT EXISTS eco_telemetry_outbox_requeue_audit (
    audit_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
    outbox_id TEXT NOT NULL REFERENCES eco_telemetry_outbox(outbox_id) ON DELETE CASCADE,
    blocked_error_code TEXT NOT NULL,
    blocked_attempt_count INTEGER NOT NULL CHECK(blocked_attempt_count > 0),
    revalidation_sha256 TEXT NOT NULL,
    requeued_at TEXT NOT NULL,
    UNIQUE(outbox_id,blocked_attempt_count,revalidation_sha256)
);
"""


_MIGRATION_105_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS eco_record_history_immutable_update
BEFORE UPDATE ON eco_record_history
BEGIN SELECT RAISE(ABORT, 'record history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_record_history_immutable_delete
BEFORE DELETE ON eco_record_history
BEGIN SELECT RAISE(ABORT, 'record history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS eco_analysis_plan_immutable
BEFORE UPDATE OF project_id,analysis_kind,plan_json,created_at ON eco_analyses
WHEN OLD.project_id IS NOT NEW.project_id
  OR OLD.analysis_kind IS NOT NEW.analysis_kind
  OR OLD.plan_json IS NOT NEW.plan_json
  OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'analysis plan is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_analysis_model_immutable_update
BEFORE UPDATE ON eco_analysis_models
BEGIN SELECT RAISE(ABORT, 'analysis model snapshot is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_analysis_model_immutable_delete
BEFORE DELETE ON eco_analysis_models
BEGIN SELECT RAISE(ABORT, 'analysis model snapshot is immutable'); END;

CREATE TRIGGER IF NOT EXISTS eco_prepared_job_immutable
BEFORE UPDATE OF prepared_json,model_run_cache_key ON eco_direct_jobs
WHEN OLD.prepared_json IS NOT NULL
 AND (OLD.prepared_json IS NOT NEW.prepared_json
      OR OLD.model_run_cache_key IS NOT NEW.model_run_cache_key)
BEGIN SELECT RAISE(ABORT, 'prepared direct job is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_terminal_job_immutable
BEFORE UPDATE ON eco_direct_jobs
WHEN OLD.state IN ('SUCCEEDED','QA_REJECTED','FAILED','CANCELLED')
BEGIN SELECT RAISE(ABORT, 'terminal direct job is immutable'); END;

CREATE TRIGGER IF NOT EXISTS eco_analysis_run_immutable_update
BEFORE UPDATE ON eco_analysis_runs
BEGIN SELECT RAISE(ABORT, 'direct run is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_analysis_run_requires_prepared_job
BEFORE INSERT ON eco_analysis_runs
WHEN NOT EXISTS (
  SELECT 1 FROM eco_direct_jobs AS job
  WHERE job.direct_job_id=NEW.direct_run_id
    AND job.analysis_id=NEW.analysis_id
    AND job.portfolio_id=NEW.portfolio_id
    AND job.scenario_id=NEW.scenario_id
    AND job.state='RUNNING'
    AND job.prepared_json IS NOT NULL
    AND job.model_run_cache_key=NEW.model_run_cache_key
)
BEGIN SELECT RAISE(ABORT, 'direct run requires its running prepared durable job'); END;
CREATE TRIGGER IF NOT EXISTS eco_analysis_run_immutable_delete
BEFORE DELETE ON eco_analysis_runs
BEGIN SELECT RAISE(ABORT, 'direct run is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_run_artifact_immutable_update
BEFORE UPDATE ON eco_run_artifacts
BEGIN SELECT RAISE(ABORT, 'direct-run artifact is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_run_artifact_immutable_delete
BEFORE DELETE ON eco_run_artifacts
BEGIN SELECT RAISE(ABORT, 'direct-run artifact is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_terminal_job_artifact_immutable_update
BEFORE UPDATE ON eco_terminal_job_artifacts
BEGIN SELECT RAISE(ABORT, 'terminal-job artifact is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_terminal_job_artifact_immutable_delete
BEFORE DELETE ON eco_terminal_job_artifacts
BEGIN SELECT RAISE(ABORT, 'terminal-job artifact is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_run_metric_immutable_update
BEFORE UPDATE ON eco_run_metrics
BEGIN SELECT RAISE(ABORT, 'direct-run metric is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_run_metric_immutable_delete
BEFORE DELETE ON eco_run_metrics
BEGIN SELECT RAISE(ABORT, 'direct-run metric is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_run_qa_immutable_update
BEFORE UPDATE ON eco_run_qa
BEGIN SELECT RAISE(ABORT, 'direct-run QA is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_run_qa_immutable_delete
BEFORE DELETE ON eco_run_qa
BEGIN SELECT RAISE(ABORT, 'direct-run QA is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_run_evidence_immutable_update
BEFORE UPDATE ON eco_run_evidence
BEGIN SELECT RAISE(ABORT, 'direct-run evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_run_evidence_immutable_delete
BEFORE DELETE ON eco_run_evidence
BEGIN SELECT RAISE(ABORT, 'direct-run evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_model_run_cache_immutable_update
BEFORE UPDATE ON eco_model_run_cache
BEGIN SELECT RAISE(ABORT, 'model-run cache entry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_model_run_cache_immutable_delete
BEFORE DELETE ON eco_model_run_cache
BEGIN SELECT RAISE(ABORT, 'model-run cache entry is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_analysis_summary_immutable_update
BEFORE UPDATE ON eco_analysis_summaries
BEGIN SELECT RAISE(ABORT, 'analysis summary is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_analysis_summary_immutable_delete
BEFORE DELETE ON eco_analysis_summaries
BEGIN SELECT RAISE(ABORT, 'analysis summary is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_evidence_claim_immutable_update
BEFORE UPDATE ON eco_evidence_claims
BEGIN SELECT RAISE(ABORT, 'evidence claim is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_evidence_claim_immutable_delete
BEFORE DELETE ON eco_evidence_claims
BEGIN SELECT RAISE(ABORT, 'evidence claim is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_reference_plan_immutable_update
BEFORE UPDATE ON eco_reference_comparison_plans
BEGIN SELECT RAISE(ABORT, 'reference comparison plan is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_reference_plan_immutable_delete
BEFORE DELETE ON eco_reference_comparison_plans
BEGIN SELECT RAISE(ABORT, 'reference comparison plan is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_reference_completion_immutable_update
BEFORE UPDATE ON eco_reference_completion_intents
BEGIN SELECT RAISE(ABORT, 'reference completion intent is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_reference_completion_immutable_delete
BEFORE DELETE ON eco_reference_completion_intents
BEGIN SELECT RAISE(ABORT, 'reference completion intent is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_reference_result_immutable_update
BEFORE UPDATE ON eco_reference_comparison_results
BEGIN SELECT RAISE(ABORT, 'reference comparison result is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_reference_result_immutable_delete
BEFORE DELETE ON eco_reference_comparison_results
BEGIN SELECT RAISE(ABORT, 'reference comparison result is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_reference_claim_immutable_update
BEFORE UPDATE ON eco_reference_comparison_claims
BEGIN SELECT RAISE(ABORT, 'reference comparison claim is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_reference_claim_immutable_delete
BEFORE DELETE ON eco_reference_comparison_claims
BEGIN SELECT RAISE(ABORT, 'reference comparison claim is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_reference_claim_excludes_analysis_claim
BEFORE INSERT ON eco_reference_comparison_claims
WHEN EXISTS (SELECT 1 FROM eco_evidence_claims WHERE claim_id=NEW.claim_id)
BEGIN SELECT RAISE(ABORT, 'reference claim identity collides with analysis evidence'); END;
CREATE TRIGGER IF NOT EXISTS eco_analysis_claim_excludes_reference_claim
BEFORE INSERT ON eco_evidence_claims
WHEN EXISTS (SELECT 1 FROM eco_reference_comparison_claims WHERE claim_id=NEW.claim_id)
BEGIN SELECT RAISE(ABORT, 'analysis claim identity collides with reference evidence'); END;

CREATE TRIGGER IF NOT EXISTS eco_synthetic_trace_binding_immutable_update
BEFORE UPDATE ON eco_synthetic_trace_bindings
BEGIN SELECT RAISE(ABORT, 'synthetic trace binding is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_synthetic_trace_binding_immutable_delete
BEFORE DELETE ON eco_synthetic_trace_bindings
BEGIN SELECT RAISE(ABORT, 'synthetic trace binding is immutable'); END;

CREATE TRIGGER IF NOT EXISTS eco_outbox_evidence_immutable
BEFORE UPDATE OF project_id,analysis_id,event_type,subject_id,stream_id,stream_sequence,
                 outbox_sequence,payload_json,payload_sha256,
                 idempotency_key,created_at ON eco_telemetry_outbox
WHEN OLD.project_id IS NOT NEW.project_id
  OR OLD.analysis_id IS NOT NEW.analysis_id
  OR OLD.event_type IS NOT NEW.event_type
  OR OLD.subject_id IS NOT NEW.subject_id
  OR OLD.stream_id IS NOT NEW.stream_id
  OR OLD.stream_sequence IS NOT NEW.stream_sequence
  OR OLD.outbox_sequence IS NOT NEW.outbox_sequence
  OR OLD.payload_json IS NOT NEW.payload_json
  OR OLD.payload_sha256 IS NOT NEW.payload_sha256
  OR OLD.idempotency_key IS NOT NEW.idempotency_key
  OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'telemetry evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_outbox_sequence_required
BEFORE INSERT ON eco_telemetry_outbox
WHEN NEW.stream_id IS NULL OR NEW.stream_id='' OR NEW.stream_sequence IS NULL
  OR NEW.stream_sequence<1 OR NEW.outbox_sequence IS NULL OR NEW.outbox_sequence<1
BEGIN SELECT RAISE(ABORT, 'telemetry sequence evidence is required'); END;
CREATE TRIGGER IF NOT EXISTS eco_outbox_immutable_delete
BEFORE DELETE ON eco_telemetry_outbox
BEGIN SELECT RAISE(ABORT, 'telemetry evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_outbox_requeue_audit_immutable_update
BEFORE UPDATE ON eco_telemetry_outbox_requeue_audit
BEGIN SELECT RAISE(ABORT, 'telemetry requeue audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_outbox_requeue_audit_immutable_delete
BEFORE DELETE ON eco_telemetry_outbox_requeue_audit
BEGIN SELECT RAISE(ABORT, 'telemetry requeue audit is immutable'); END;

CREATE TRIGGER IF NOT EXISTS eco_assistant_record_immutable_update
BEFORE UPDATE ON eco_assistant_records
BEGIN SELECT RAISE(ABORT, 'assistant answer is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_assistant_record_immutable_delete
BEFORE DELETE ON eco_assistant_records
BEGIN SELECT RAISE(ABORT, 'assistant answer is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_assistant_rejection_immutable_update
BEFORE UPDATE ON eco_assistant_rejection_audits
BEGIN SELECT RAISE(ABORT, 'assistant rejection audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_assistant_rejection_immutable_delete
BEFORE DELETE ON eco_assistant_rejection_audits
BEGIN SELECT RAISE(ABORT, 'assistant rejection audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_assistant_answer_excludes_rejection
BEFORE INSERT ON eco_assistant_records
WHEN EXISTS (SELECT 1 FROM eco_assistant_rejection_audits WHERE call_id=NEW.call_id)
BEGIN SELECT RAISE(ABORT, 'rejected assistant call cannot publish an answer'); END;
CREATE TRIGGER IF NOT EXISTS eco_assistant_rejection_excludes_answer
BEFORE INSERT ON eco_assistant_rejection_audits
WHEN EXISTS (SELECT 1 FROM eco_assistant_records WHERE call_id=NEW.call_id)
BEGIN SELECT RAISE(ABORT, 'answered assistant call cannot become rejected'); END;

CREATE TRIGGER IF NOT EXISTS eco_review_brief_draft_immutable_update
BEFORE UPDATE ON eco_review_brief_drafts
BEGIN SELECT RAISE(ABORT, 'review brief draft is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_draft_immutable_delete
BEFORE DELETE ON eco_review_brief_drafts
BEGIN SELECT RAISE(ABORT, 'review brief draft is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_item_immutable_update
BEFORE UPDATE ON eco_review_brief_items
BEGIN SELECT RAISE(ABORT, 'review brief item is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_item_immutable_delete
BEFORE DELETE ON eco_review_brief_items
BEGIN SELECT RAISE(ABORT, 'review brief item is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_selection_immutable_update
BEFORE UPDATE ON eco_review_brief_finding_selections
BEGIN SELECT RAISE(ABORT, 'review brief finding selection is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_selection_immutable_delete
BEFORE DELETE ON eco_review_brief_finding_selections
BEGIN SELECT RAISE(ABORT, 'review brief finding selection is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_evidence_edge_immutable_update
BEFORE UPDATE ON eco_review_brief_evidence_edges
BEGIN SELECT RAISE(ABORT, 'review brief evidence edge is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_evidence_edge_immutable_delete
BEFORE DELETE ON eco_review_brief_evidence_edges
BEGIN SELECT RAISE(ABORT, 'review brief evidence edge is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_execution_immutable_update
BEFORE UPDATE ON eco_review_brief_agent_executions
BEGIN SELECT RAISE(ABORT, 'review brief agent execution is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_execution_immutable_delete
BEFORE DELETE ON eco_review_brief_agent_executions
BEGIN SELECT RAISE(ABORT, 'review brief agent execution is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_tool_receipt_immutable_update
BEFORE UPDATE ON eco_review_brief_tool_receipts
BEGIN SELECT RAISE(ABORT, 'review brief tool receipt is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_tool_receipt_immutable_delete
BEFORE DELETE ON eco_review_brief_tool_receipts
BEGIN SELECT RAISE(ABORT, 'review brief tool receipt is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_validation_immutable_update
BEFORE UPDATE ON eco_review_brief_validations
BEGIN SELECT RAISE(ABORT, 'review brief validation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_validation_immutable_delete
BEFORE DELETE ON eco_review_brief_validations
BEGIN SELECT RAISE(ABORT, 'review brief validation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_trace_reference_immutable_update
BEFORE UPDATE ON eco_review_brief_trace_references
BEGIN SELECT RAISE(ABORT, 'review brief trace reference is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_trace_reference_immutable_delete
BEFORE DELETE ON eco_review_brief_trace_references
BEGIN SELECT RAISE(ABORT, 'review brief trace reference is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_rejection_immutable_update
BEFORE UPDATE ON eco_review_brief_rejections
BEGIN SELECT RAISE(ABORT, 'review brief rejection is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_rejection_immutable_delete
BEFORE DELETE ON eco_review_brief_rejections
BEGIN SELECT RAISE(ABORT, 'review brief rejection is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_attestation_immutable_update
BEFORE UPDATE ON eco_review_brief_attestations
BEGIN SELECT RAISE(ABORT, 'review brief attestation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_attestation_immutable_delete
BEFORE DELETE ON eco_review_brief_attestations
BEGIN SELECT RAISE(ABORT, 'review brief attestation is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_feedback_immutable_update
BEFORE UPDATE ON eco_review_brief_feedback
BEGIN SELECT RAISE(ABORT, 'review brief feedback is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_feedback_immutable_delete
BEFORE DELETE ON eco_review_brief_feedback
BEGIN SELECT RAISE(ABORT, 'review brief feedback is immutable'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_draft_excludes_rejection
BEFORE INSERT ON eco_review_brief_drafts
WHEN EXISTS (SELECT 1 FROM eco_review_brief_rejections WHERE call_id=NEW.call_id)
BEGIN SELECT RAISE(ABORT, 'rejected review brief call cannot publish a draft'); END;
CREATE TRIGGER IF NOT EXISTS eco_review_brief_rejection_excludes_draft
BEFORE INSERT ON eco_review_brief_rejections
WHEN EXISTS (SELECT 1 FROM eco_review_brief_drafts WHERE call_id=NEW.call_id)
BEGIN SELECT RAISE(ABORT, 'published review brief call cannot become rejected'); END;
"""


_MIGRATION_106_EVENT_POLICY = """
BEGIN IMMEDIATE;
DROP TRIGGER IF EXISTS eco_outbox_event_type_insert;
DROP TRIGGER IF EXISTS eco_outbox_event_type_update;
CREATE TRIGGER IF NOT EXISTS eco_outbox_event_type_insert
BEFORE INSERT ON eco_telemetry_outbox
WHEN NEW.event_type NOT IN (
  'bundle_import','semantic_revision','model_attestation','analysis_plan','guided_session','guided_checkpoint',
  'direct_job_terminal','analysis_completion','reference_comparison','evidence_export',
  'oversize_sync_approval','review_brief_draft'
)
BEGIN SELECT RAISE(ABORT, 'unsupported telemetry event type'); END;
CREATE TRIGGER IF NOT EXISTS eco_outbox_event_type_update
BEFORE UPDATE OF event_type ON eco_telemetry_outbox
WHEN NEW.event_type NOT IN (
  'bundle_import','semantic_revision','model_attestation','analysis_plan','guided_session','guided_checkpoint',
  'direct_job_terminal','analysis_completion','reference_comparison','evidence_export',
  'oversize_sync_approval','review_brief_draft'
)
BEGIN SELECT RAISE(ABORT, 'unsupported telemetry event type'); END;
COMMIT;
"""


_SEMANTIC_RECORD_TYPES = frozenset(
    {
        "asset",
        "hypothesis",
        "focal_area_set",
        "scenario",
        "intervention_site",
        "intervention_option",
        "intervention_option_tombstone",
        "safeguard",
        "review_mask",
        "decision_rule",
        "decision_brief",
        "cost_basis",
        "resource_policy",
    }
)


class EcoProjectStore:
    """Single-writer domain store; renderer state is never authoritative."""

    def __init__(self, project_directory: str | Path) -> None:
        self.root = Path(project_directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "project.sqlite"
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
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {
            0,
            101,
            102,
            103,
            104,
            105,
            106,
            107,
            108,
            109,
            110,
            111,
            112,
            113,
            114,
            115,
            116,
        }:
            self._connection.close()
            raise RuntimeError(f"unsupported EcoCorridor database version: {version}")
        self._connection.executescript(_SCHEMA)
        if version == 110:
            reference_tables = (
                "eco_reference_comparison_plans",
                "eco_reference_comparison_results",
            )
            missing_parser_columns = [
                table
                for table in reference_tables
                if "reference_matrix_parser_sha256"
                not in {
                    str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({table})")
                }
            ]
            # Old reference evidence cannot be attributed to the new parser
            # after the fact.  Preflight both tables before changing either:
            # empty tables migrate atomically; any populated unprovenanced
            # table remains untouched and must be recreated from direct runs.
            if any(
                self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in missing_parser_columns
            ):
                self._connection.close()
                raise RuntimeError(
                    "schema-110 reference evidence has no cryptographic parser provenance; "
                    "recreate the reference comparison before upgrading"
                )
            if missing_parser_columns:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    for table in missing_parser_columns:
                        self._connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN reference_matrix_parser_sha256 "
                            f"TEXT NOT NULL DEFAULT '{REFERENCE_MATRIX_PARSER_SHA256}'"
                        )
                except BaseException:
                    self._connection.rollback()
                    self._connection.close()
                    raise
                else:
                    self._connection.commit()
        if version in {101, 102}:
            guided_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(eco_guided_sessions)")
            }
            if "revision" not in guided_columns:
                self._connection.execute(
                    "ALTER TABLE eco_guided_sessions "
                    "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)"
                )
        if version < 105:
            outbox_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(eco_telemetry_outbox)")
            }
            if "lease_token" not in outbox_columns:
                self._connection.execute(
                    "ALTER TABLE eco_telemetry_outbox ADD COLUMN lease_token TEXT"
                )
            if "lease_expires_at" not in outbox_columns:
                self._connection.execute(
                    "ALTER TABLE eco_telemetry_outbox ADD COLUMN lease_expires_at TEXT"
                )
        if version < 107:
            direct_job_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(eco_direct_jobs)")
            }
            if "resource_observation_json" not in direct_job_columns:
                self._connection.execute(
                    "ALTER TABLE eco_direct_jobs ADD COLUMN resource_observation_json TEXT"
                )
        if version < 109:
            outbox_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(eco_telemetry_outbox)")
            }
            if "stream_id" not in outbox_columns:
                self._connection.execute(
                    "ALTER TABLE eco_telemetry_outbox ADD COLUMN stream_id TEXT"
                )
            if "stream_sequence" not in outbox_columns:
                self._connection.execute(
                    "ALTER TABLE eco_telemetry_outbox ADD COLUMN stream_sequence INTEGER"
                )
            if "outbox_sequence" not in outbox_columns:
                self._connection.execute(
                    "ALTER TABLE eco_telemetry_outbox ADD COLUMN outbox_sequence INTEGER"
                )
            # ``rowid`` is the only durable insertion/commit order available to
            # pre-109 databases.  Wall-clock timestamps can move backwards and
            # must never become a provider-history clock during migration.
            rows = self._connection.execute(
                "SELECT * FROM eco_telemetry_outbox ORDER BY rowid"
            ).fetchall()
            stream_sequences: dict[tuple[str, str], int] = {}
            project_sequences: dict[str, int] = {}
            for row in rows:
                project_id = str(row["project_id"])
                stream_id = _legacy_outbox_stream_id(row)
                stream_key = (project_id, stream_id)
                stream_sequence = stream_sequences.get(stream_key, 0) + 1
                outbox_sequence = project_sequences.get(project_id, 0) + 1
                self._connection.execute(
                    "UPDATE eco_telemetry_outbox SET stream_id=?,stream_sequence=?,"
                    "outbox_sequence=? WHERE outbox_id=?",
                    (stream_id, stream_sequence, outbox_sequence, row["outbox_id"]),
                )
                stream_sequences[stream_key] = stream_sequence
                project_sequences[project_id] = outbox_sequence
            if self._connection.execute(
                "SELECT COUNT(*) FROM eco_telemetry_outbox WHERE stream_id IS NULL "
                "OR stream_sequence IS NULL OR outbox_sequence IS NULL"
            ).fetchone()[0]:
                raise RuntimeError("EcoCorridor telemetry sequence migration is incomplete")
        if version < 112:
            analysis_columns = {
                str(row[1]) for row in self._connection.execute("PRAGMA table_info(eco_analyses)")
            }
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if "analysis_kind" not in analysis_columns:
                    self._connection.execute(
                        "ALTER TABLE eco_analyses ADD COLUMN analysis_kind TEXT NOT NULL "
                        "DEFAULT 'ALTERNATIVES' CHECK(analysis_kind IN ('ALTERNATIVES','REFERENCE'))"
                    )
                self._connection.execute(
                    "UPDATE eco_analyses SET analysis_kind='REFERENCE' WHERE EXISTS ("
                    "SELECT 1 FROM eco_reference_comparison_plans AS plan "
                    "WHERE plan.analysis_id=eco_analyses.analysis_id)"
                )
                completed_reference_rows = self._connection.execute(
                    "SELECT plan.payload_json FROM eco_reference_comparison_plans AS plan "
                    "JOIN eco_analyses AS analysis ON analysis.analysis_id=plan.analysis_id "
                    "WHERE analysis.state='COMPLETED' ORDER BY plan.analysis_id"
                ).fetchall()
                for completed_row in completed_reference_rows:
                    reference_plan = ReferenceComparisonPlan.model_validate_json(
                        str(completed_row["payload_json"])
                    )
                    run_rows = self._reference_completion_rows(
                        self._connection,
                        reference_plan.analysis_id,
                    )
                    expected = {
                        (portfolio.portfolio_id, scenario_id)
                        for portfolio in reference_plan.run_plan.portfolios
                        for scenario_id in reference_plan.run_plan.scenario_ids
                    }
                    observed = {
                        (str(row["portfolio_id"]), str(row["scenario_id"])) for row in run_rows
                    }
                    durable_job_count = int(
                        self._connection.execute(
                            "SELECT COUNT(*) FROM eco_direct_jobs WHERE analysis_id=?",
                            (reference_plan.analysis_id,),
                        ).fetchone()[0]
                    )
                    if (
                        observed != expected
                        or len(run_rows) != len(expected)
                        or durable_job_count != len(expected)
                        or any(
                            row["phase"] != "MATRIX"
                            or row["state"] != DirectJobState.SUCCEEDED.value
                            or not re.fullmatch(r"[0-9a-f]{64}", str(row["terminal_sha256"] or ""))
                            or not re.fullmatch(r"[0-9a-f]{64}", str(row["result_sha256"] or ""))
                            for row in run_rows
                        )
                    ):
                        raise RuntimeError(
                            "completed legacy reference analysis lacks its exact terminal run set"
                        )
                    completion_id, completion_payload, completion_sha256 = (
                        _reference_completion_manifest(
                            plan=reference_plan,
                            direct_runs=[dict(row) for row in run_rows],
                        )
                    )
                    completion_json = canonical_json(completion_payload)
                    self._connection.execute(
                        "INSERT OR IGNORE INTO eco_reference_completion_intents("
                        "completion_id,analysis_id,plan_id,payload_json,payload_sha256,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            completion_id,
                            reference_plan.analysis_id,
                            reference_plan.plan_id,
                            completion_json,
                            completion_sha256,
                            _now(),
                        ),
                    )
                    project_row = self._connection.execute(
                        "SELECT project_id FROM eco_analyses WHERE analysis_id=?",
                        (reference_plan.analysis_id,),
                    ).fetchone()
                    if project_row is None:
                        raise RuntimeError("reference completion migration lost its project")
                    self._enqueue_outbox(
                        self._connection,
                        project_id=str(project_row["project_id"]),
                        analysis_id=reference_plan.analysis_id,
                        event_type="analysis_completion",
                        subject_id=reference_plan.analysis_id,
                        stream_id=f"connectivity_analysis:{reference_plan.analysis_id}",
                        payload={
                            "analysis_id": reference_plan.analysis_id,
                            "completion_kind": "REFERENCE_RESULT_PENDING",
                            "completion_id": completion_id,
                            "plan_id": reference_plan.plan_id,
                            "reference_completion_sha256": completion_sha256,
                            "direct_run_ids": sorted(str(row["direct_run_id"]) for row in run_rows),
                        },
                        # Schema 111 completed reference-shaped analyses through the generic
                        # completion producer, which already used ``analysis_completion:<id>``.
                        # Append a cryptographically bound corrective event instead of mutating
                        # or deleting that immutable telemetry history.
                        idempotency_key=(
                            f"reference_analysis_completion:{reference_plan.analysis_id}:"
                            f"{completion_sha256}"
                        ),
                    )
            except BaseException:
                self._connection.rollback()
                self._connection.close()
                raise
            else:
                self._connection.commit()
        self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS eco_telemetry_outbox_stream_sequence "
            "ON eco_telemetry_outbox(project_id,stream_id,stream_sequence)"
        )
        self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS eco_telemetry_outbox_project_sequence "
            "ON eco_telemetry_outbox(project_id,outbox_sequence)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS eco_telemetry_outbox_stream_state "
            "ON eco_telemetry_outbox(project_id,stream_id,stream_sequence,state)"
        )
        orphaned_run = self._connection.execute(
            "SELECT run.direct_run_id FROM eco_analysis_runs AS run "
            "LEFT JOIN eco_direct_jobs AS job ON job.direct_job_id=run.direct_run_id "
            "WHERE job.direct_job_id IS NULL OR job.analysis_id!=run.analysis_id "
            "OR job.portfolio_id!=run.portfolio_id OR job.scenario_id!=run.scenario_id "
            "OR job.state!='SUCCEEDED' OR job.prepared_json IS NULL "
            "OR job.model_run_cache_key!=run.model_run_cache_key "
            "OR job.terminal_sha256 IS NULL OR job.terminal_sha256='' "
            "OR job.completed_at IS NULL LIMIT 1"
        ).fetchone()
        if orphaned_run is not None:
            self._connection.close()
            raise RuntimeError(
                "legacy direct-run evidence lacks a successful prepared durable job; "
                "recreate the analysis through the durable scheduler"
            )
        regressed = self._connection.execute(
            "SELECT later.outbox_id FROM eco_telemetry_outbox AS later "
            "WHERE later.state='SYNCED' AND EXISTS ("
            "SELECT 1 FROM eco_telemetry_outbox AS prior "
            "WHERE prior.project_id=later.project_id AND prior.stream_id=later.stream_id "
            "AND prior.stream_sequence<later.stream_sequence AND prior.state!='SYNCED') LIMIT 1"
        ).fetchone()
        if regressed is not None:
            self._connection.close()
            raise RuntimeError(
                "EcoCorridor telemetry history contains an out-of-order provider acknowledgement"
            )
        if version < 109:
            self._connection.execute("DROP TRIGGER IF EXISTS eco_outbox_evidence_immutable")
        if version < 116:
            # Schema 115 retained the complete draft and runner receipts only as
            # checked blobs. Backfill the normalized immutable evidence graph in
            # one transaction so an interrupted upgrade can never leave a draft
            # that appears partially published.
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for row in self._connection.execute(
                    "SELECT * FROM eco_review_brief_drafts ORDER BY created_at,draft_id"
                ).fetchall():
                    draft = self._review_brief_draft_from_row(row)
                    runner_json = str(row["runner_json"])
                    if hashlib.sha256(runner_json.encode("utf-8")).hexdigest() != str(
                        row["runner_sha256"]
                    ):
                        raise RuntimeError(
                            "schema-115 review brief runner failed its retained checksum"
                        )
                    from .review_brief import ReviewBriefRunnerResult

                    runner = ReviewBriefRunnerResult.model_validate_json(runner_json)
                    self._insert_review_brief_normalized(
                        self._connection,
                        project_id=str(row["project_id"]),
                        call_id=str(row["call_id"]),
                        draft=draft,
                        runner=runner,
                    )
            except BaseException:
                self._connection.rollback()
                self._connection.close()
                raise
            else:
                self._connection.commit()
        # The v112 definition also freezes ``analysis_kind``. Recreate rather
        # than relying on ``IF NOT EXISTS`` against a pre-v112 trigger body.
        self._connection.execute("DROP TRIGGER IF EXISTS eco_analysis_plan_immutable")
        self._connection.executescript(_MIGRATION_105_TRIGGERS)
        self._connection.executescript(_MIGRATION_106_EVENT_POLICY)
        self._connection.execute("PRAGMA user_version=116")
        # Expired leases are process-abandoned cloud work, not scientific state.
        # Releasing them at open makes retry durable without stealing a live lease.
        self.recover_expired_outbox_leases()

    def close(self) -> None:
        self._connection.close()

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

    def initialize(
        self,
        *,
        project_id: str,
        title: str,
        mode: str,
        skeleton: dict[str, Any],
    ) -> None:
        now = _now()
        payload = canonical_json(skeleton)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT title,mode,skeleton_json FROM eco_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["title"] != title
                    or existing["mode"] != mode
                    or existing["skeleton_json"] != payload
                ):
                    raise ValueError(
                        "project identity already exists with different authoritative state"
                    )
                return
            connection.execute(
                "INSERT INTO eco_projects(project_id,title,mode,skeleton_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (project_id, title, mode, payload, now, now),
            )

    def project(self) -> dict[str, Any]:
        rows = self._connection.execute("SELECT * FROM eco_projects ORDER BY created_at").fetchall()
        if len(rows) != 1:
            raise KeyError(
                "project is not initialized" if not rows else "multiple project identities"
            )
        row = dict(rows[0])
        row["skeleton"] = json.loads(row.pop("skeleton_json"))
        return row

    def retained_input_bytes(
        self,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """Return authoritative retained-input usage, including CAS orphans.

        Managed local and provider bytes are counted physically by inode, not
        only from published asset records. This deliberately charges an exact
        retained orphan after a later SQLite publication failure while counting
        hard-linked aliases once. Effective asset contracts supplement missing,
        damaged, or securely imported paths so quota cannot recover silently.

        When ``connection`` is supplied the caller must already hold the
        store's ``BEGIN IMMEDIATE`` transaction.  That form is used to
        serialize the final filesystem-retention and semantic-publication
        decision.
        """

        active_connection = connection or self._connection
        project_id = self.project()["project_id"]
        project = active_connection.execute(
            "SELECT skeleton_json FROM eco_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise KeyError(project_id)
        bundle = active_connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type='bundle' "
            "ORDER BY rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        base = json.loads(
            str(bundle["payload_json"] if bundle is not None else project["skeleton_json"])
        )
        assets = {
            str(item["asset_id"]): item
            for item in base.get("assets", [])
            if isinstance(item, dict) and "asset_id" in item
        }
        for row in active_connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type='asset'",
            (project_id,),
        ):
            item = json.loads(str(row["payload_json"]))
            assets[str(item["asset_id"])] = item

        physical_bytes: dict[tuple[object, ...], int] = {}
        managed_paths: dict[Path, tuple[object, ...]] = {}

        def record_file(path: Path, *, context: str) -> None:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"{context} contains an unsafe file entry")
            identity: tuple[object, ...] = (
                ("inode", int(metadata.st_dev), int(metadata.st_ino))
                if metadata.st_ino
                else ("path", path.as_posix())
            )
            physical_bytes[identity] = max(physical_bytes.get(identity, 0), int(metadata.st_size))
            managed_paths[path] = identity

        data_assets_root = self.root / "data_assets"
        provider_root = data_assets_root / "provider"
        if data_assets_root.is_symlink():
            raise ValueError("managed input root must not be a symbolic link")
        if data_assets_root.exists():
            if not data_assets_root.is_dir():
                raise ValueError("managed input root must be a directory")
            for directory, directories, filenames in os.walk(data_assets_root, followlinks=False):
                current = Path(directory)
                for name in directories:
                    child = current / name
                    metadata = child.lstat()
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                        raise ValueError("managed inputs contain an unsafe directory entry")
                if current == data_assets_root:
                    directories[:] = [
                        name for name in directories if name not in {".downloads", "provider"}
                    ]
                for name in filenames:
                    if current == data_assets_root and name == ".provider-download.lock":
                        continue
                    record_file(current / name, context="managed inputs")

        if provider_root.is_symlink():
            raise ValueError("provider CAS root must not be a symbolic link")
        if provider_root.exists():
            if not provider_root.is_dir():
                raise ValueError("provider CAS root must be a directory")
            for directory, directories, filenames in os.walk(provider_root, followlinks=False):
                current = Path(directory)
                for name in directories:
                    child = current / name
                    metadata = child.lstat()
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                        raise ValueError("provider CAS contains an unsafe directory entry")
                for name in filenames:
                    record_file(current / name, context="provider CAS")

        logical_bytes: dict[str, int] = {}
        for item in assets.values():
            byte_size = item.get("byte_size")
            if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
                raise ValueError("retained asset has an invalid byte-size contract")
            relative = PurePosixPath(str(item.get("relative_path", "")))
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("retained asset has an invalid relative path")
            direct_path = self.root.joinpath(*relative.parts)
            identity = managed_paths.get(direct_path)
            if identity is None:
                # Secure imports are materialized outside the managed local
                # tree, and missing/damaged retained paths must not make quota
                # silently recover. Preserve their reviewed contract charge.
                logical_key = relative.as_posix()
                logical_bytes[logical_key] = max(logical_bytes.get(logical_key, 0), byte_size)
            else:
                physical_bytes[identity] = max(physical_bytes[identity], byte_size)
        return sum(logical_bytes.values()) + sum(physical_bytes.values())

    def effective_asset(
        self,
        asset_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ConnectivityAsset | None:
        """Resolve one asset head without requiring a complete model graph."""

        active_connection = connection or self._connection
        project_id = self.project()["project_id"]
        imported = active_connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type='bundle' "
            "ORDER BY rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        candidate: dict[str, Any] | None = None
        if imported is not None:
            bundle = json.loads(str(imported["payload_json"]))
            candidate = next(
                (
                    item
                    for item in bundle.get("assets", [])
                    if isinstance(item, dict) and item.get("asset_id") == asset_id
                ),
                None,
            )
        overlay = active_connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type='asset' "
            "AND record_id=?",
            (project_id, asset_id),
        ).fetchone()
        if overlay is not None:
            candidate = json.loads(str(overlay["payload_json"]))
        return ConnectivityAsset.model_validate(candidate) if candidate is not None else None

    @staticmethod
    def _put_record_in_transaction(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        record_type: str,
        record_id: str,
        payload_json: str,
        allow_update: bool,
        now: str,
    ) -> tuple[int, bool]:
        existing = connection.execute(
            "SELECT revision,payload_json FROM eco_records "
            "WHERE project_id=? AND record_type=? AND record_id=?",
            (project_id, record_type, record_id),
        ).fetchone()
        if existing is not None and existing["payload_json"] == payload_json:
            return int(existing["revision"]), False
        if existing is not None and not allow_update:
            raise ValueError(f"immutable {record_type} record already exists: {record_id}")
        revision = int(existing["revision"]) + 1 if existing else 1
        if existing:
            connection.execute(
                "UPDATE eco_records SET revision=?,payload_json=?,updated_at=? "
                "WHERE project_id=? AND record_type=? AND record_id=?",
                (revision, payload_json, now, project_id, record_type, record_id),
            )
        else:
            connection.execute(
                "INSERT INTO eco_records(project_id,record_type,record_id,revision,payload_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, record_type, record_id, revision, payload_json, now, now),
            )
        connection.execute(
            "INSERT INTO eco_record_history(project_id,record_type,record_id,revision,"
            "payload_json,recorded_at) VALUES(?,?,?,?,?,?)",
            (project_id, record_type, record_id, revision, payload_json, now),
        )
        connection.execute(
            "UPDATE eco_projects SET updated_at=? WHERE project_id=?", (now, project_id)
        )
        return revision, True

    def put_record(
        self,
        record_type: str,
        record_id: str,
        payload: dict[str, Any],
        *,
        allow_update: bool = True,
    ) -> int:
        project_id = self.project()["project_id"]
        payload_json = canonical_json(payload)
        now = _now()
        with self.transaction() as connection:
            revision, _ = self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type=record_type,
                record_id=record_id,
                payload_json=payload_json,
                allow_update=allow_update,
                now=now,
            )
        return revision

    def put_records_atomic(
        self,
        records: Sequence[tuple[str, str, dict[str, Any], bool]],
    ) -> dict[tuple[str, str], int]:
        """Publish a related record set without exposing partial model state."""

        if not records:
            raise ValueError("an atomic record set cannot be empty")
        identities = [(record_type, record_id) for record_type, record_id, _, _ in records]
        if len(identities) != len(set(identities)):
            raise ValueError("an atomic record set contains duplicate identities")
        project_id = self.project()["project_id"]
        now = _now()
        revisions: dict[tuple[str, str], int] = {}
        with self.transaction() as connection:
            for record_type, record_id, payload, allow_update in records:
                revision, _ = self._put_record_in_transaction(
                    connection,
                    project_id=project_id,
                    record_type=record_type,
                    record_id=record_id,
                    payload_json=canonical_json(payload),
                    allow_update=allow_update,
                    now=now,
                )
                revisions[(record_type, record_id)] = revision
        return revisions

    def retain_model_fitness_evidence(
        self,
        *,
        evidence_id: str,
        revision: ModelRevision,
        content: dict[str, Any],
        source_asset_ids: Sequence[str] = (),
    ) -> int:
        """Retain an immutable-content evidence record for one model revision.

        Public projects normally reference a checksum-verified ``ConnectivityAsset``
        directly from each fitness dimension.  This record form exists for
        worker-owned evidence such as the synthetic Ridgeway assumption ledger,
        whose complete JSON is retained in SQLite instead of being represented as
        a user asset.  Updating the same evidence ID for a later model revision
        preserves the earlier payload in ``eco_record_history``.
        """

        if not evidence_id.strip():
            raise ValueError("model-fitness evidence ID is required")
        project_id = self.project()["project_id"]
        normalized_asset_ids = sorted({str(item).strip() for item in source_asset_ids})
        if any(not item for item in normalized_asset_ids):
            raise ValueError("model-fitness source asset IDs must be nonblank")
        payload = {
            "schema_version": "ecocorridor-model-fitness-evidence/v1.0",
            "evidence_id": evidence_id.strip(),
            "revision_id": revision.revision_id,
            "model_sha256": revision.model_sha256,
            "content_sha256": canonical_sha256(content),
            "content": content,
            "source_asset_ids": normalized_asset_ids,
        }
        payload_json = canonical_json(payload)
        now = _now()
        with self.transaction() as connection:
            active = self._active_revision_in_transaction(connection, project_id)
            if (
                revision.revision_id != active.revision_id
                or revision.model_sha256 != active.model_sha256
            ):
                raise ValueError("model-fitness evidence must target the active model revision")
            existing_row = connection.execute(
                "SELECT payload_json FROM eco_records WHERE project_id=? "
                "AND record_type='model_fitness_evidence' AND record_id=?",
                (project_id, evidence_id.strip()),
            ).fetchone()
            existing = (
                json.loads(str(existing_row["payload_json"])) if existing_row is not None else None
            )
            if (
                existing is not None
                and existing.get("revision_id") == revision.revision_id
                and existing != payload
            ):
                raise ValueError(
                    "model-fitness evidence content is immutable within one semantic revision"
                )
            record_revision, _ = self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type="model_fitness_evidence",
                record_id=evidence_id.strip(),
                payload_json=payload_json,
                allow_update=True,
                now=now,
            )
        return record_revision

    def get_record(self, record_type: str, record_id: str) -> dict[str, Any]:
        project_id = self.project()["project_id"]
        row = self._connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type=? AND record_id=?",
            (project_id, record_type, record_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"{record_type}:{record_id}")
        return json.loads(row["payload_json"])

    def resolve_model_fitness_evidence(self, evidence_id: str) -> dict[str, Any]:
        """Resolve one active Passport evidence reference without treating it as a solver claim.

        Fitness dimensions may cite either an active checksum-verified project asset or an
        immutable worker-retained evidence record.  This resolver accepts only IDs actually
        named by the active Model Fitness Passport and verifies every retained source byte
        before returning a local, claim-shaped evidence projection.
        """

        bundle = self.effective_bundle()
        authorized_ids = {
            item for dimension in bundle.fitness.dimensions for item in dimension.evidence_ids
        }
        if evidence_id not in authorized_ids:
            raise KeyError(evidence_id)
        assets = {item.asset_id: item for item in bundle.assets}

        def verify_asset(asset: ConnectivityAsset) -> dict[str, Any]:
            path = self.resolve_asset_path(asset.relative_path)
            _read_verified_regular_file(
                path,
                expected_sha256=asset.sha256,
                expected_byte_size=asset.byte_size,
                capture_bytes=False,
            )
            return asset.model_dump(mode="json")

        if evidence_id in assets:
            source_assets = [verify_asset(assets[evidence_id])]
            return {
                "evidence_id": evidence_id,
                "evidence_kind": "ACTIVE_ASSET",
                "revision_id": bundle.revision.revision_id,
                "model_sha256": bundle.revision.model_sha256,
                "content_sha256": assets[evidence_id].sha256,
                "source_assets": source_assets,
            }

        # A reviewed Passport may cite an authoritative reference-comparison
        # result (or one of its retained comparison claims).  Resolve those
        # through the repository's full immutable validation path rather than
        # treating the opaque ID as a generic record.  The caller still applies
        # the model-fitness/reference-policy gate before using this projection.
        for result, claims in self.reference_comparison_results(
            revision_id=bundle.revision.revision_id,
            model_sha256=bundle.revision.model_sha256,
        ):
            reference_ids = {result.result_id, *(claim.claim_id for claim in claims)}
            if evidence_id not in reference_ids or result.status != "PASSED":
                continue
            source_asset_ids: set[str] = set()
            for claim in claims:
                for node in claim.nodes:
                    if node.node_type != "ASSET":
                        continue
                    candidate = node.node_id.removeprefix("asset:")
                    if candidate in assets:
                        source_asset_ids.add(candidate)
                    elif str(node.label) in assets:
                        source_asset_ids.add(str(node.label))
            source_assets = [verify_asset(assets[item]) for item in sorted(source_asset_ids)]
            reference_payload = {
                "result": result.model_dump(mode="json"),
                "claims": [item.model_dump(mode="json") for item in claims],
            }
            return {
                "evidence_id": evidence_id,
                "evidence_kind": "REFERENCE_COMPARISON",
                "revision_id": bundle.revision.revision_id,
                "model_sha256": bundle.revision.model_sha256,
                "content_sha256": canonical_sha256(reference_payload),
                "source_assets": source_assets,
            }

        retained = self.get_record("model_fitness_evidence", evidence_id)
        content = retained.get("content")
        source_asset_ids = retained.get("source_asset_ids")
        if (
            retained.get("schema_version") != "ecocorridor-model-fitness-evidence/v1.0"
            or retained.get("evidence_id") != evidence_id
            or retained.get("revision_id") != bundle.revision.revision_id
            or retained.get("model_sha256") != bundle.revision.model_sha256
            or not isinstance(content, dict)
            or retained.get("content_sha256") != canonical_sha256(content)
            or not isinstance(source_asset_ids, list)
            or any(not isinstance(item, str) or not item for item in source_asset_ids)
        ):
            raise ValueError("model-fitness evidence record failed active-revision integrity")
        if len(source_asset_ids) != len(set(source_asset_ids)):
            raise ValueError("model-fitness evidence source assets are duplicated")
        try:
            source_assets = [verify_asset(assets[item]) for item in source_asset_ids]
        except KeyError as exc:
            raise ValueError("model-fitness evidence cites a non-active source asset") from exc
        return {
            "evidence_id": evidence_id,
            "evidence_kind": "RETAINED_RECORD",
            "revision_id": bundle.revision.revision_id,
            "model_sha256": bundle.revision.model_sha256,
            "content_sha256": str(retained["content_sha256"]),
            "source_assets": source_assets,
            "record": retained,
        }

    def get_record_revision(
        self, record_type: str, record_id: str, revision: int
    ) -> dict[str, Any]:
        if revision < 1:
            raise ValueError("record revision must be positive")
        project_id = self.project()["project_id"]
        row = self._connection.execute(
            "SELECT payload_json FROM eco_record_history WHERE project_id=? AND record_type=? "
            "AND record_id=? AND revision=?",
            (project_id, record_type, record_id, revision),
        ).fetchone()
        if row is None:
            raise KeyError(f"{record_type}:{record_id}:revision:{revision}")
        return json.loads(row["payload_json"])

    def list_records(self, record_type: str) -> list[dict[str, Any]]:
        project_id = self.project()["project_id"]
        rows = self._connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type=? "
            "ORDER BY record_id",
            (project_id, record_type),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def latest_record(self, record_type: str) -> dict[str, Any] | None:
        """Return the most recently changed singleton-style record."""

        project_id = self.project()["project_id"]
        row = self._connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type=? "
            "ORDER BY updated_at DESC, record_id DESC LIMIT 1",
            (project_id, record_type),
        ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    @staticmethod
    def _active_revision_in_transaction(
        connection: sqlite3.Connection, project_id: str
    ) -> ModelRevision:
        row = connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type='revision' "
            "ORDER BY rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if row is not None:
            return ModelRevision.model_validate_json(str(row["payload_json"]))
        bundle = connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type='bundle' "
            "ORDER BY rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if bundle is not None:
            return ConnectivityProjectBundle.model_validate_json(
                str(bundle["payload_json"])
            ).revision
        project = connection.execute(
            "SELECT skeleton_json FROM eco_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise KeyError(project_id)
        try:
            return ModelRevision.model_validate(json.loads(project["skeleton_json"])["revision"])
        except (KeyError, TypeError) as exc:
            raise ValueError("project has no retained semantic model revision") from exc

    @staticmethod
    def _semantic_snapshot_in_transaction(
        connection: sqlite3.Connection,
        project_id: str,
        overlays: dict[tuple[str, str], str] | None = None,
    ) -> dict[str, Any]:
        """Return the canonical model-affecting heads, excluding revision metadata."""

        project = connection.execute(
            "SELECT title,mode,skeleton_json FROM eco_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise KeyError(project_id)
        bundle = connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type='bundle' "
            "ORDER BY rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        heads = {
            (str(row["record_type"]), str(row["record_id"])): str(row["payload_json"])
            for row in connection.execute(
                "SELECT record_type,record_id,payload_json FROM eco_records WHERE project_id=?",
                (project_id,),
            )
            if str(row["record_type"]) in _SEMANTIC_RECORD_TYPES
        }
        if overlays:
            heads.update(overlays)
        projection = {
            "schema": "ecocorridor-semantic-snapshot/v1",
            "project_id": project_id,
            "title": str(project["title"]),
            "mode": str(project["mode"]),
            "base": (
                json.loads(str(bundle["payload_json"]))
                if bundle is not None
                else json.loads(str(project["skeleton_json"]))
            ),
            "record_heads": [
                {
                    "record_type": record_type,
                    "record_id": record_id,
                    "payload": json.loads(payload_json),
                }
                for (record_type, record_id), payload_json in sorted(heads.items())
            ],
        }
        return projection

    def publish_semantic_change(
        self,
        records: Sequence[tuple[str, str, dict[str, Any]]],
        *,
        created_by: str,
        reason: str,
        created_at: datetime | str | None = None,
        proposed_revision_id: str | None = None,
        expected_parent_revision_id: str | None = None,
        extra_records: Sequence[tuple[str, str, dict[str, Any], bool]] = (),
        transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
    ) -> dict[str, Any]:
        """Atomically publish model heads, revision, diff, and telemetry.

        Equality is an idempotent no-op.  A changed payload always advances the
        semantic revision and model hash in the same SQLite transaction; callers
        can never expose a changed effective model under an earlier revision.
        ``extra_records`` are immutable provenance records committed with the
        publication but excluded from the scientific model hash.
        ``transaction_guard`` runs only after ``BEGIN IMMEDIATE`` and before
        any authoritative row changes. It exists for policy decisions whose
        filesystem side effect (such as a no-clobber CAS link) must serialize
        with publication; retained immutable bytes are not rolled back or
        unlinked if a later SQL/outbox write fails.
        """

        if not records:
            raise ValueError("a semantic publication requires model-affecting records")
        if not created_by.strip() or not reason.strip():
            raise ValueError("semantic publication requires author and reason")
        identities = [(kind, record_id) for kind, record_id, _ in records]
        if len(identities) != len(set(identities)):
            raise ValueError("semantic publication contains duplicate record identities")
        unsupported = sorted({kind for kind, _, _ in records} - _SEMANTIC_RECORD_TYPES)
        if unsupported:
            raise ValueError("record types are not model-affecting: " + ", ".join(unsupported))
        extra_identities = [(kind, record_id) for kind, record_id, _, _ in extra_records]
        if len(extra_identities) != len(set(extra_identities)):
            raise ValueError("semantic publication contains duplicate provenance identities")
        project_id = self.project()["project_id"]
        timestamp = (
            created_at.isoformat()
            if isinstance(created_at, datetime)
            else str(created_at or _now())
        )
        with self.transaction() as connection:
            if transaction_guard is not None:
                transaction_guard(connection)
            active = self._active_revision_in_transaction(connection, project_id)
            if (
                expected_parent_revision_id is not None
                and active.revision_id != expected_parent_revision_id
            ):
                raise ValueError("semantic publication parent revision is stale")
            changes: list[SemanticChange] = []
            overlays: dict[tuple[str, str], str] = {}
            current_revisions: dict[tuple[str, str], int] = {}
            for record_type, record_id, payload in records:
                payload_json = canonical_json(payload)
                existing = connection.execute(
                    "SELECT revision,payload_json FROM eco_records WHERE project_id=? "
                    "AND record_type=? AND record_id=?",
                    (project_id, record_type, record_id),
                ).fetchone()
                if existing is not None:
                    current_revisions[(record_type, record_id)] = int(existing["revision"])
                if existing is not None and str(existing["payload_json"]) == payload_json:
                    continue
                overlays[(record_type, record_id)] = payload_json
                changes.append(
                    SemanticChange(
                        object_type=record_type,
                        object_id=record_id,
                        field="$record",
                        before=(str(existing["payload_json"]) if existing is not None else None),
                        after=payload_json,
                        rationale=reason.strip(),
                    )
                )
            if not changes:
                return {
                    "changed": False,
                    "revision": active.model_dump(mode="json"),
                    "semantic_diff": None,
                    "record_revisions": {
                        f"{kind}:{record_id}": revision
                        for (kind, record_id), revision in sorted(current_revisions.items())
                    },
                    "outbox_id": None,
                }
            snapshot = self._semantic_snapshot_in_transaction(
                connection, project_id, overlays=overlays
            )
            model_sha256 = canonical_sha256(snapshot)
            revision_identity = canonical_sha256(
                {
                    "project_id": project_id,
                    "parent_revision_id": active.revision_id,
                    "model_sha256": model_sha256,
                    "changes": [item.model_dump(mode="json") for item in changes],
                }
            )
            revision_id = proposed_revision_id or f"revision_{revision_identity[:24]}"
            if not revision_id.strip():
                raise ValueError("semantic revision ID is required")
            diff_id = f"diff_{revision_identity[:24]}"
            diff_payload = {
                "diff_id": diff_id,
                "from_revision_id": active.revision_id,
                "to_revision_id": revision_id,
                "changes": [item.model_dump(mode="json") for item in changes],
            }
            semantic_diff = SemanticModelDiff(
                **diff_payload,
                sha256=canonical_sha256(diff_payload),
            )
            revision = ModelRevision(
                revision_id=revision_id,
                parent_revision_id=active.revision_id,
                project_id=project_id,
                created_at=timestamp,
                created_by=created_by.strip(),
                reason=reason.strip(),
                model_sha256=model_sha256,
                diff_id=semantic_diff.diff_id,
            )
            published_revisions: dict[tuple[str, str], int] = {}
            for record_type, record_id, payload in records:
                record_revision, _ = self._put_record_in_transaction(
                    connection,
                    project_id=project_id,
                    record_type=record_type,
                    record_id=record_id,
                    payload_json=canonical_json(payload),
                    allow_update=True,
                    now=timestamp,
                )
                published_revisions[(record_type, record_id)] = record_revision
            for record_type, record_id, payload, allow_update in extra_records:
                self._put_record_in_transaction(
                    connection,
                    project_id=project_id,
                    record_type=record_type,
                    record_id=record_id,
                    payload_json=canonical_json(payload),
                    allow_update=allow_update,
                    now=timestamp,
                )
            self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type="semantic_diff",
                record_id=semantic_diff.diff_id,
                payload_json=canonical_json(semantic_diff.model_dump(mode="json")),
                allow_update=False,
                now=timestamp,
            )
            self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type="revision",
                record_id=revision.revision_id,
                payload_json=canonical_json(revision.model_dump(mode="json")),
                allow_update=False,
                now=timestamp,
            )
            outbox_id = self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=None,
                event_type="semantic_revision",
                subject_id=revision.revision_id,
                stream_id=f"planning_model:{project_id}",
                payload={
                    "revision": revision.model_dump(mode="json"),
                    "semantic_diff": semantic_diff.model_dump(mode="json"),
                    "record_revisions": [
                        {
                            "record_type": kind,
                            "record_id": record_id,
                            "revision": record_revision,
                        }
                        for (kind, record_id), record_revision in sorted(
                            published_revisions.items()
                        )
                    ],
                },
                idempotency_key=f"semantic_revision:{revision.revision_id}",
            )
        return {
            "changed": True,
            "revision": revision.model_dump(mode="json"),
            "semantic_diff": semantic_diff.model_dump(mode="json"),
            "record_revisions": {
                f"{kind}:{record_id}": record_revision
                for (kind, record_id), record_revision in sorted(published_revisions.items())
            },
            "outbox_id": outbox_id,
        }

    def record_validation(
        self,
        *,
        revision: ModelRevision,
        validation: dict[str, Any],
        validated_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Retain an immutable, revision-bound validation result."""

        project_id = self.project()["project_id"]
        active = self._active_revision_in_transaction(self._connection, project_id)
        if (
            revision.revision_id != active.revision_id
            or revision.model_sha256 != active.model_sha256
        ):
            raise ValueError("validation must target the active semantic revision and model hash")
        timestamp = (
            validated_at.isoformat()
            if isinstance(validated_at, datetime)
            else str(validated_at or _now())
        )
        payload = {
            **validation,
            "revision_id": revision.revision_id,
            "model_sha256": revision.model_sha256,
            "validated_at": timestamp,
        }
        validation_id = "validation_" + canonical_sha256(payload)[:24]
        payload["validation_id"] = validation_id
        self.put_records_atomic(
            [
                ("validation", validation_id, payload, False),
                ("validation", "latest", payload, True),
            ]
        )
        return payload

    def latest_ready_validation(self) -> dict[str, Any] | None:
        """Return READY evidence only when it is the newest retained validation."""

        latest = self.latest_record("validation")
        if latest is None or latest.get("status") != "READY":
            return None
        project_id = self.project()["project_id"]
        active = self._active_revision_in_transaction(self._connection, project_id)
        if (
            latest.get("revision_id") != active.revision_id
            or latest.get("model_sha256") != active.model_sha256
        ):
            return None
        return latest

    def publish_fitness_attestation(
        self,
        *,
        fitness: dict[str, Any],
        reviewer: str,
        attestation: str,
        validation_id: str,
        candidate_fitness_sha256: str,
        recorded_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Atomically retain governance attestation and its cloud envelope.

        Model Fitness describes review of a scientific revision; it is not a
        numerical model input.  The passport therefore remains bound to the
        exact active revision rather than creating a self-invalidating child
        revision whose passport would immediately point at its parent.
        """

        if not reviewer.strip() or not attestation.strip():
            raise ValueError("fitness attestation requires reviewer and statement")
        project_id = self.project()["project_id"]
        timestamp = (
            recorded_at.isoformat()
            if isinstance(recorded_at, datetime)
            else str(recorded_at or _now())
        )
        fitness_json = canonical_json(fitness)
        if canonical_sha256(fitness) != candidate_fitness_sha256:
            raise ValueError("attested fitness differs from the READY validation candidate")
        with self.transaction() as connection:
            active = self._active_revision_in_transaction(connection, project_id)
            if fitness.get("revision_id") != active.revision_id:
                raise ValueError("fitness attestation does not target the active revision")
            validation = connection.execute(
                "SELECT payload_json FROM eco_records WHERE project_id=? "
                "AND record_type='validation' AND record_id=?",
                (project_id, validation_id),
            ).fetchone()
            if validation is None:
                raise ValueError("fitness attestation requires retained READY validation")
            validation_payload = json.loads(str(validation["payload_json"]))
            if (
                validation_payload.get("status") != "READY"
                or validation_payload.get("revision_id") != active.revision_id
                or validation_payload.get("model_sha256") != active.model_sha256
                or validation_payload.get("candidate_fitness_sha256") != candidate_fitness_sha256
            ):
                raise ValueError("fitness attestation validation is stale or mismatched")
            fitness_revision, _ = self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type="fitness",
                record_id=str(fitness["passport_id"]),
                payload_json=fitness_json,
                allow_update=True,
                now=timestamp,
            )
            attestation_id = (
                f"{fitness['passport_id']}:{canonical_sha256([attestation, validation_id])[:16]}"
            )
            attestation_payload = {
                "attestation_id": attestation_id,
                "passport_id": fitness["passport_id"],
                "reviewer": reviewer.strip(),
                "attestation": attestation.strip(),
                "recorded_at": timestamp,
                "fitness_revision": fitness_revision,
                "validation_id": validation_id,
                "validated_revision_id": active.revision_id,
                "validated_model_sha256": active.model_sha256,
                "candidate_fitness_sha256": candidate_fitness_sha256,
            }
            self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type="attestation",
                record_id=attestation_id,
                payload_json=canonical_json(attestation_payload),
                allow_update=False,
                now=timestamp,
            )
            outbox_id = self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=None,
                event_type="model_attestation",
                subject_id=attestation_id,
                stream_id=f"planning_model:{project_id}",
                payload={
                    "attestation_id": attestation_id,
                    "passport_id": fitness["passport_id"],
                    "fitness_record_revision": fitness_revision,
                    "validation_id": validation_id,
                    "revision_id": active.revision_id,
                    "model_sha256": active.model_sha256,
                    "candidate_fitness_sha256": candidate_fitness_sha256,
                },
                idempotency_key=f"model_attestation:{attestation_id}",
            )
        return {
            "fitness_revision": fitness_revision,
            "attestation_id": attestation_id,
            "outbox_id": outbox_id,
        }

    def pending_recovery_state(
        self, *, live_analysis_ids: set[str] | frozenset[str] = frozenset()
    ) -> dict[str, Any]:
        """Describe durable process-owned state and distinguish live from abandoned work."""

        analyses = [
            {
                "analysis_id": str(row["analysis_id"]),
                "analysis_kind": str(row["analysis_kind"]),
                "plan_id": str(row["plan_id"]) if row["plan_id"] is not None else None,
                "revision_id": ConnectivityRunPlan.model_validate_json(
                    str(row["plan_json"])
                ).revision_id,
                "analysis_state": str(row["state"]),
                "executor_active": str(row["analysis_id"]) in live_analysis_ids,
                "recovery_required": str(row["analysis_id"]) not in live_analysis_ids,
                "running_job_ids": [
                    str(item["direct_job_id"])
                    for item in self._connection.execute(
                        "SELECT direct_job_id FROM eco_direct_jobs WHERE analysis_id=? "
                        "AND state='RUNNING' ORDER BY sequence_number",
                        (row["analysis_id"],),
                    )
                ],
            }
            for row in self._connection.execute(
                "SELECT analysis.analysis_id,analysis.analysis_kind,analysis.plan_json,analysis.state,"
                "reference.plan_id FROM eco_analyses AS analysis "
                "LEFT JOIN eco_reference_comparison_plans AS reference "
                "ON reference.analysis_id=analysis.analysis_id "
                "WHERE analysis.state IN ('RUNNING','PAUSING') "
                "ORDER BY analysis.created_at,analysis.analysis_id"
            )
        ]
        interrupted = [item for item in analyses if item["recovery_required"]]
        return {
            "required": bool(interrupted),
            "analyses": analyses,
            "next_action": (
                "Resume or cancel each interrupted analysis; resume requeues abandoned jobs."
                if interrupted
                else (
                    "The active worker owns all running analyses; no recovery action is required."
                    if analyses
                    else "No interrupted analysis requires recovery."
                )
            ),
        }

    def resolve_asset_path(self, relative_path: str) -> Path:
        """Resolve a retained asset in a planning or securely imported project."""

        candidates = [relative_path]
        for manifest in self.list_records("import_manifest"):
            materialized = str(manifest.get("materialized_directory", "")).strip()
            if materialized:
                candidates.append(f"{materialized.rstrip('/')}/{relative_path}")
        for candidate in candidates:
            try:
                resolved = contained_path(self.root, candidate)
            except PathSecurityError as exc:
                raise ValueError("asset path escapes the project") from exc
            if resolved.is_file() and not resolved.is_symlink():
                return resolved
        raise FileNotFoundError(relative_path)

    def store_alignment_revision(
        self,
        *,
        approval: dict[str, Any],
        derived_asset: dict[str, Any],
        semantic_diff: dict[str, Any],
        revision: dict[str, Any],
    ) -> None:
        """Atomically publish an approved derived asset and semantic revision."""

        project_id = self.project()["project_id"]
        now = _now()
        records = (
            (
                "grid_alignment_approval",
                str(approval["approval_id"]),
                approval,
                False,
            ),
            ("asset", str(derived_asset["asset_id"]), derived_asset, False),
            ("semantic_diff", str(semantic_diff["diff_id"]), semantic_diff, False),
            ("revision", str(revision["revision_id"]), revision, False),
        )
        with self.transaction() as connection:
            for record_type, record_id, payload, allow_update in records:
                self._put_record_in_transaction(
                    connection,
                    project_id=project_id,
                    record_type=record_type,
                    record_id=record_id,
                    payload_json=canonical_json(payload),
                    allow_update=allow_update,
                    now=now,
                )

    def store_digitization_revision(
        self,
        *,
        provenance: dict[str, Any],
        derived_asset: dict[str, Any],
        semantic_diff: dict[str, Any],
        revision: dict[str, Any],
    ) -> None:
        """Atomically publish a reviewer-authored raster and its provenance.

        The caller materializes the exact previewed bytes at an exclusive path
        immediately before this transaction and removes them if this method
        fails.  All authoritative database records therefore become visible in
        one transaction, and none may silently update an earlier publication.
        """

        project_id = self.project()["project_id"]
        now = _now()
        records = (
            (
                "digitization_provenance",
                str(provenance["provenance_id"]),
                provenance,
                False,
            ),
            ("asset", str(derived_asset["asset_id"]), derived_asset, False),
            ("semantic_diff", str(semantic_diff["diff_id"]), semantic_diff, False),
            ("revision", str(revision["revision_id"]), revision, False),
        )
        with self.transaction() as connection:
            for record_type, record_id, payload, allow_update in records:
                self._put_record_in_transaction(
                    connection,
                    project_id=project_id,
                    record_type=record_type,
                    record_id=record_id,
                    payload_json=canonical_json(payload),
                    allow_update=allow_update,
                    now=now,
                )

    def store_bundle(
        self,
        bundle: ConnectivityProjectBundle,
        *,
        import_manifest_id: str | None = None,
        import_manifest: dict[str, Any] | None = None,
    ) -> None:
        project = self.project()
        if bundle.project_id != project["project_id"]:
            raise ValueError("bundle project identity differs from the project store")
        if (import_manifest_id is None) != (import_manifest is None):
            raise ValueError("import manifest identity and payload must be supplied together")
        payload = bundle.model_dump(mode="json")
        payload_json = canonical_json(payload)
        bundle_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = _now()
        with self.transaction() as connection:
            revision, _ = self._put_record_in_transaction(
                connection,
                project_id=bundle.project_id,
                record_type="bundle",
                record_id=bundle.project_id,
                payload_json=payload_json,
                allow_update=True,
                now=now,
            )
            manifest_reference: dict[str, Any] | None = None
            if import_manifest_id is not None and import_manifest is not None:
                manifest_json = canonical_json(import_manifest)
                manifest_revision, _ = self._put_record_in_transaction(
                    connection,
                    project_id=bundle.project_id,
                    record_type="import_manifest",
                    record_id=import_manifest_id,
                    payload_json=manifest_json,
                    allow_update=False,
                    now=now,
                )
                manifest_reference = {
                    "record_id": import_manifest_id,
                    "record_revision": manifest_revision,
                    "sha256": hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
                }
            self._enqueue_outbox(
                connection,
                project_id=bundle.project_id,
                analysis_id=None,
                event_type="bundle_import",
                subject_id=bundle.project_id,
                stream_id=f"bundle_import:{bundle.project_id}",
                payload={
                    "project_id": bundle.project_id,
                    "revision_id": bundle.revision.revision_id,
                    "record_revision": revision,
                    "bundle_sha256": bundle_sha256,
                    "assets": [
                        {
                            "asset_id": asset.asset_id,
                            "sha256": asset.sha256,
                            "byte_size": asset.byte_size,
                        }
                        for asset in sorted(bundle.assets, key=lambda item: item.asset_id)
                    ],
                    "import_manifest": manifest_reference,
                },
                idempotency_key=f"bundle_import:{bundle.project_id}:{bundle_sha256}",
            )

    def effective_bundle(self) -> ConnectivityProjectBundle:
        project = self.project()
        imported = self.list_records("bundle")
        skeleton = project["skeleton"]
        try:
            if imported:
                payload = dict(imported[-1])
            else:
                payload = {
                    "schema_version": "1.0",
                    "project_id": project["project_id"],
                    "title": project["title"],
                    "revision": skeleton["revision"],
                    "decision_brief": skeleton["decision_brief"],
                    "assets": [],
                    "resistance_hypotheses": [],
                    "focal_area_sets": [],
                    "scenarios": skeleton.get("scenarios", []),
                    "intervention_sites": [],
                    "intervention_options": [],
                    "cost_basis": None,
                    "safeguards": skeleton.get("safeguards", []),
                    "review_masks": skeleton.get("review_masks", []),
                    "decision_rules": skeleton.get("decision_rules", []),
                    "fitness": skeleton.get("fitness"),
                    "resource_policy": skeleton["resource_policy"],
                }

            def overlay(record_type: str, field: str, identity: str) -> None:
                merged = {str(item[identity]): item for item in payload.get(field, [])}
                for item in self.list_records(record_type):
                    merged[str(item[identity])] = item
                payload[field] = [merged[key] for key in sorted(merged)]

            overlay("asset", "assets", "asset_id")
            overlay("hypothesis", "resistance_hypotheses", "hypothesis_id")
            overlay("focal_area_set", "focal_area_sets", "focal_set_id")
            overlay("scenario", "scenarios", "scenario_id")
            overlay("intervention_site", "intervention_sites", "site_id")
            overlay("intervention_option", "intervention_options", "option_id")
            tombstones = {
                str(item.get("option_id", "")): item
                for item in self.list_records("intervention_option_tombstone")
            }
            option_heads = {
                str(item["option_id"]): item for item in payload["intervention_options"]
            }
            site_heads = {str(item["site_id"]): item for item in payload["intervention_sites"]}
            known_revision_ids = {
                str(payload["revision"]["revision_id"]),
                *(str(item["revision_id"]) for item in self.list_records("revision")),
            }
            malformed_tombstones: list[str] = []
            for option_id, item in tombstones.items():
                option = option_heads.get(option_id)
                site_id = str(item.get("site_id", ""))
                removed_cost = item.get("removed_cost_basis_amount")
                if (
                    not option_id
                    or item.get("schema_version")
                    != "ecocorridor-intervention-option-tombstone/v1.0"
                    or item.get("state") not in {"REMOVED", "RESTORED"}
                    or not site_id
                    or site_id not in site_heads
                    or option is None
                    or str(option.get("site_id", "")) != site_id
                    or item.get("option_sha256") != canonical_sha256(option)
                    or item.get("parent_revision_id") not in known_revision_ids
                    or not str(item.get("changed_by", "")).strip()
                    or not str(item.get("changed_at", "")).strip()
                    or not str(item.get("reason", "")).strip()
                    or (
                        removed_cost is not None
                        and (
                            isinstance(removed_cost, bool)
                            or not isinstance(removed_cost, (int, float))
                            or removed_cost < 0
                        )
                    )
                ):
                    malformed_tombstones.append(option_id)
            malformed_tombstones.sort()
            if malformed_tombstones:
                raise ValueError(
                    "intervention option tombstones are malformed: "
                    + ", ".join(malformed_tombstones)
                )
            removed_option_ids = {
                option_id for option_id, item in tombstones.items() if item["state"] == "REMOVED"
            }
            payload["intervention_options"] = [
                item
                for item in payload["intervention_options"]
                if str(item["option_id"]) not in removed_option_ids
            ]
            overlay("safeguard", "safeguards", "safeguard_id")
            overlay("review_mask", "review_masks", "mask_id")
            overlay("decision_rule", "decision_rules", "rule_id")
            for record_type, field in (
                ("revision", "revision"),
                ("decision_brief", "decision_brief"),
                ("cost_basis", "cost_basis"),
                ("fitness", "fitness"),
                ("resource_policy", "resource_policy"),
            ):
                latest = self.latest_record(record_type)
                if latest is not None:
                    payload[field] = latest
        except (KeyError, IndexError) as exc:
            raise ValueError("planning project is incomplete") from exc
        return ConnectivityProjectBundle.model_validate(payload)

    def create_analysis(
        self,
        plan: ConnectivityRunPlan,
        *,
        bundle: ConnectivityProjectBundle,
    ) -> None:
        project_id = self.project()["project_id"]
        if bundle.project_id != project_id:
            raise ValueError("analysis bundle belongs to another project")
        if bundle.revision.revision_id != plan.revision_id:
            raise ValueError("analysis bundle revision differs from the immutable plan")
        canonical_vectors: dict[tuple[str, ...], str] = {}
        for portfolio in plan.portfolios:
            expected = canonicalize_portfolio(
                bundle.intervention_sites,
                bundle.intervention_options,
                portfolio.selections,
            )
            if portfolio != expected:
                raise ValueError(
                    "analysis portfolio identity differs from its canonical reviewed option vector"
                )
            previous = canonical_vectors.get(expected.canonical_option_ids)
            if previous is not None:
                raise ValueError(
                    "analysis contains duplicate canonical portfolio vectors: "
                    f"{previous}, {portfolio.portfolio_id}"
                )
            canonical_vectors[expected.canonical_option_ids] = portfolio.portfolio_id
        bundle_json = canonical_json(bundle.model_dump(mode="json"))
        bundle_sha256 = hashlib.sha256(bundle_json.encode("utf-8")).hexdigest()
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO eco_analyses(analysis_id,project_id,analysis_kind,state,plan_json,"
                "created_at,updated_at) VALUES(?,?,?, ?,?,?,?)",
                (
                    plan.analysis_id,
                    project_id,
                    "ALTERNATIVES",
                    AnalysisState.READY.value,
                    canonical_json(plan.model_dump(mode="json")),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO eco_analysis_models(analysis_id,bundle_sha256,bundle_json,created_at) "
                "VALUES(?,?,?,?)",
                (plan.analysis_id, bundle_sha256, bundle_json, now),
            )
            self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=plan.analysis_id,
                event_type="analysis_plan",
                subject_id=plan.analysis_id,
                stream_id=f"connectivity_analysis:{plan.analysis_id}",
                payload={
                    "analysis_id": plan.analysis_id,
                    "revision_id": plan.revision_id,
                    "bundle_sha256": bundle_sha256,
                },
                idempotency_key=f"analysis_plan:{plan.analysis_id}:{bundle_sha256}",
            )

    def analysis_bundle(self, analysis_id: str) -> ConnectivityProjectBundle:
        row = self._connection.execute(
            "SELECT bundle_sha256,bundle_json FROM eco_analysis_models WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"analysis model snapshot:{analysis_id}")
        payload = str(row["bundle_json"])
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != row["bundle_sha256"]:
            raise ValueError("analysis model snapshot failed its immutable checksum")
        bundle = ConnectivityProjectBundle.model_validate_json(payload)
        analysis = self.analysis(analysis_id)
        if bundle.revision.revision_id != analysis["plan"]["revision_id"]:
            raise ValueError("analysis model snapshot differs from the immutable plan")
        return bundle

    def analysis(self, analysis_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise KeyError(analysis_id)
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        result["error"] = json.loads(result.pop("error_json")) if result["error_json"] else None
        return result

    def transition_analysis(self, analysis_id: str, target: AnalysisState | str) -> None:
        destination = AnalysisState(target)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if row is None:
                raise KeyError(analysis_id)
            assert_transition(row["state"], destination, kind="analysis")
            connection.execute(
                "UPDATE eco_analyses SET state=?,updated_at=? WHERE analysis_id=?",
                (destination.value, _now(), analysis_id),
            )

    def fail_analysis(self, analysis_id: str, error: dict[str, Any]) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if row is None:
                raise KeyError(analysis_id)
            assert_transition(row["state"], AnalysisState.FAILED, kind="analysis")
            connection.execute(
                "UPDATE eco_analyses SET state='FAILED',error_json=?,updated_at=? WHERE analysis_id=?",
                (canonical_json(error), _now(), analysis_id),
            )

    def pause_analysis(self, analysis_id: str) -> None:
        """Compatibility helper for synchronous executors.

        Production execution uses ``request_pause`` followed by ``mark_paused``
        after the active direct job reaches a terminal state.
        """

        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if row is None:
                raise KeyError(analysis_id)
            assert_transition(row["state"], AnalysisState.PAUSING, kind="analysis")
            assert_transition(AnalysisState.PAUSING, AnalysisState.PAUSED, kind="analysis")
            connection.execute(
                "UPDATE eco_analyses SET state='PAUSED',updated_at=? WHERE analysis_id=?",
                (_now(), analysis_id),
            )

    def request_pause(self, analysis_id: str) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if row is None:
                raise KeyError(analysis_id)
            if row["state"] == AnalysisState.PAUSING.value:
                return
            assert_transition(row["state"], AnalysisState.PAUSING, kind="analysis")
            connection.execute(
                "UPDATE eco_analyses SET state='PAUSING',updated_at=? WHERE analysis_id=?",
                (_now(), analysis_id),
            )

    def mark_paused(self, analysis_id: str) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if row is None:
                raise KeyError(analysis_id)
            if row["state"] == AnalysisState.PAUSED.value:
                return
            assert_transition(row["state"], AnalysisState.PAUSED, kind="analysis")
            connection.execute(
                "UPDATE eco_analyses SET state='PAUSED',updated_at=? WHERE analysis_id=?",
                (_now(), analysis_id),
            )

    @staticmethod
    def _normalized_direct_jobs(
        plan: ConnectivityRunPlan, jobs: Sequence[dict[str, Any]]
    ) -> list[tuple[str, str, str, str, int, int]]:
        if not jobs:
            raise ValueError("an analysis requires at least one direct job")
        expected = {
            (portfolio.portfolio_id, scenario_id)
            for portfolio in plan.portfolios
            for scenario_id in plan.scenario_ids
        }
        observed = {(str(job["portfolio_id"]), str(job["scenario_id"])) for job in jobs}
        sequences = [int(job["sequence_number"]) for job in jobs]
        if (
            observed != expected
            or len(observed) != len(jobs)
            or sorted(sequences) != list(range(len(jobs)))
        ):
            raise ValueError("direct-job queue must exactly match the immutable run plan")
        normalized = [
            (
                str(job["direct_job_id"]),
                str(job["portfolio_id"]),
                str(job["scenario_id"]),
                str(job.get("phase", "MATRIX")),
                int(job["sequence_number"]),
                int(bool(job.get("emit_current_map", False))),
            )
            for job in sorted(jobs, key=lambda item: int(item["sequence_number"]))
        ]
        if any(item[3] != "MATRIX" for item in normalized):
            raise ValueError("professional analysis plans contain matrix jobs only")
        return normalized

    @staticmethod
    def _insert_direct_jobs(
        connection: sqlite3.Connection,
        analysis_id: str,
        normalized: Sequence[tuple[str, str, str, str, int, int]],
        now: str,
    ) -> None:
        for direct_job_id, portfolio_id, scenario_id, phase, sequence, emit_map in normalized:
            connection.execute(
                "INSERT INTO eco_direct_jobs(direct_job_id,analysis_id,portfolio_id,"
                "scenario_id,phase,sequence_number,state,emit_current_map,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'QUEUED',?,?,?)",
                (
                    direct_job_id,
                    analysis_id,
                    portfolio_id,
                    scenario_id,
                    phase,
                    sequence,
                    emit_map,
                    now,
                    now,
                ),
            )

    def schedule_direct_jobs(self, analysis_id: str, jobs: Sequence[dict[str, Any]]) -> None:
        """Persist the immutable portfolio x scenario queue in deterministic order."""

        with self.transaction() as connection:
            analysis = connection.execute(
                "SELECT plan_json FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if analysis is None:
                raise KeyError(analysis_id)
            plan = ConnectivityRunPlan.model_validate(json.loads(analysis["plan_json"]))
            normalized = self._normalized_direct_jobs(plan, jobs)
            existing = connection.execute(
                "SELECT direct_job_id,portfolio_id,scenario_id,phase,sequence_number,"
                "emit_current_map FROM eco_direct_jobs WHERE analysis_id=? ORDER BY sequence_number",
                (analysis_id,),
            ).fetchall()
            if existing:
                if [tuple(row) for row in existing] != normalized:
                    raise ValueError("an analysis direct-job queue is immutable once planned")
                return
            self._insert_direct_jobs(connection, analysis_id, normalized, _now())

    def start_analysis_with_direct_jobs(
        self, analysis_id: str, jobs: Sequence[dict[str, Any]]
    ) -> None:
        """Atomically publish the immutable queue and transition READY to RUNNING."""

        with self.transaction() as connection:
            analysis = connection.execute(
                "SELECT plan_json,state FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if analysis is None:
                raise KeyError(analysis_id)
            plan = ConnectivityRunPlan.model_validate(json.loads(analysis["plan_json"]))
            normalized = self._normalized_direct_jobs(plan, jobs)
            existing = connection.execute(
                "SELECT direct_job_id,portfolio_id,scenario_id,phase,sequence_number,"
                "emit_current_map FROM eco_direct_jobs WHERE analysis_id=? ORDER BY sequence_number",
                (analysis_id,),
            ).fetchall()
            if existing:
                if [tuple(row) for row in existing] != normalized:
                    raise ValueError("an analysis direct-job queue is immutable once planned")
                if analysis["state"] != AnalysisState.RUNNING.value:
                    raise ValueError("an existing direct queue is not in its running state")
                return
            if analysis["state"] != AnalysisState.READY.value:
                raise ValueError("a new direct queue requires a READY analysis")
            now = _now()
            self._insert_direct_jobs(connection, analysis_id, normalized, now)
            assert_transition(analysis["state"], AnalysisState.RUNNING, kind="analysis")
            connection.execute(
                "UPDATE eco_analyses SET state='RUNNING',updated_at=? WHERE analysis_id=?",
                (now, analysis_id),
            )

    def recover_abandoned_direct_jobs(
        self, analysis_id: str, *, process_tree_exit_confirmed: bool = False
    ) -> int:
        """Requeue jobs only inside a live worker that confirmed tree termination.

        Startup recovery must use ``consume_solver_shutdown_receipt`` instead.
        """

        if not process_tree_exit_confirmed:
            raise SolverShutdownReceiptError(
                "ORPHAN_PROCESS_AUDIT_REQUIRED",
                "direct jobs cannot be requeued without confirmed process-tree exit",
            )

        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE eco_direct_jobs SET state='QUEUED',started_at=NULL,updated_at=? "
                "WHERE analysis_id=? AND state='RUNNING'",
                (_now(), analysis_id),
            )
            return int(cursor.rowcount)

    def requeue_unlaunched_direct_job(self, direct_job_id: str, *, attempt_count: int) -> bool:
        """Requeue one claimed job before any solver process has been launched."""

        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE eco_direct_jobs SET state='QUEUED',started_at=NULL,updated_at=? "
                "WHERE direct_job_id=? AND state='RUNNING' AND attempt_count=?",
                (_now(), direct_job_id, attempt_count),
            )
            return cursor.rowcount == 1

    def direct_job_state_snapshot(self, analysis_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT direct_job_id,state,attempt_count FROM eco_direct_jobs "
            "WHERE analysis_id=? ORDER BY sequence_number",
            (analysis_id,),
        ).fetchall()
        return [
            {
                "direct_job_id": str(row["direct_job_id"]),
                "state": str(row["state"]),
                "attempt_count": int(row["attempt_count"]),
            }
            for row in rows
        ]

    @staticmethod
    def _receipt_sha256(value: object, *, label: str) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise SolverShutdownReceiptError(
                "ORPHAN_RECEIPT_INVALID", f"{label} is not a SHA-256 digest"
            )
        try:
            int(value, 16)
        except ValueError as exc:
            raise SolverShutdownReceiptError(
                "ORPHAN_RECEIPT_INVALID", f"{label} is not a SHA-256 digest"
            ) from exc
        return value.casefold()

    def record_solver_shutdown_receipt(
        self,
        analysis_id: str,
        *,
        project_path_sha256: str,
        engine_manifest_sha256: str,
        workflow_identity_sha256: str,
        pre_jobs: Sequence[dict[str, Any]],
        post_jobs: Sequence[dict[str, Any]],
        process_exits: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Record one generation only for fully terminated covered process trees."""

        project_path_sha256 = self._receipt_sha256(
            project_path_sha256, label="project path identity"
        )
        engine_manifest_sha256 = self._receipt_sha256(
            engine_manifest_sha256, label="engine manifest identity"
        )
        workflow_identity_sha256 = self._receipt_sha256(
            workflow_identity_sha256, label="workflow identity"
        )
        pre_by_id = {str(item["direct_job_id"]): item for item in pre_jobs}
        post_running = [item for item in post_jobs if item.get("state") == "RUNNING"]
        if not post_running:
            return None
        if len(pre_by_id) != len(pre_jobs):
            raise SolverShutdownReceiptError(
                "ORPHAN_RECEIPT_INVALID", "shutdown pre-state contains duplicate direct jobs"
            )
        covered_jobs: list[dict[str, Any]] = []
        for post in post_running:
            direct_job_id = str(post["direct_job_id"])
            pre = pre_by_id.get(direct_job_id)
            process_exit = process_exits.get(direct_job_id)
            if pre is None or process_exit is None:
                raise SolverShutdownReceiptError(
                    "ORPHAN_TREE_EXIT_UNCONFIRMED",
                    "a RUNNING direct job lacks complete pre-state or process-tree evidence",
                )
            if process_exit.get("tree_exit_confirmed") is not True:
                raise SolverShutdownReceiptError(
                    "ORPHAN_TREE_EXIT_UNCONFIRMED",
                    "a RUNNING direct job's process tree has not been confirmed absent",
                )
            pre_state = str(pre["state"])
            pre_attempt_count = int(pre["attempt_count"])
            post_attempt_count = int(post["attempt_count"])
            valid_transition = (
                pre_state == "RUNNING" and pre_attempt_count == post_attempt_count
            ) or (pre_state == "QUEUED" and pre_attempt_count + 1 == post_attempt_count)
            if not valid_transition:
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_STATE_MISMATCH",
                    "shutdown pre/post state does not identify the covered job attempt",
                )
            if (
                process_exit.get("direct_job_id") != direct_job_id
                or process_exit.get("attempt_count") != post_attempt_count
            ):
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_IDENTITY_MISMATCH",
                    "process-tree evidence belongs to another direct-job attempt",
                )
            process_id = process_exit.get("process_id")
            process_group_id = process_exit.get("process_group_id")
            process_identity = self._receipt_sha256(
                process_exit.get("process_identity"), label="solver process identity"
            )
            if (
                not isinstance(process_id, int)
                or isinstance(process_id, bool)
                or process_id <= 0
                or not isinstance(process_group_id, int)
                or isinstance(process_group_id, bool)
                or process_group_id <= 0
            ):
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_INVALID", "solver PID or process-group ID is invalid"
                )
            covered_jobs.append(
                {
                    "direct_job_id": direct_job_id,
                    "pre_state": pre_state,
                    "pre_attempt_count": pre_attempt_count,
                    "post_state": "RUNNING",
                    "post_attempt_count": post_attempt_count,
                    "process_id": process_id,
                    "process_group_id": process_group_id,
                    "process_identity": process_identity,
                    "termination_reason": str(process_exit["termination_reason"]),
                    "confirmation_method": str(process_exit["confirmation_method"]),
                    "tree_exit_confirmed": True,
                    "launched_at": str(process_exit["launched_at"]),
                    "confirmed_at": str(process_exit["confirmed_at"]),
                }
            )
        covered_jobs.sort(key=lambda item: item["direct_job_id"])
        project_id = self.project()["project_id"]
        now = _now()
        with self.transaction() as connection:
            analysis = connection.execute(
                "SELECT project_id,state FROM eco_analyses WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
            if analysis is None or str(analysis["project_id"]) != project_id:
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_INVALID", "shutdown receipt analysis is unavailable"
                )
            if str(analysis["state"]) not in {"RUNNING", "PAUSING"}:
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_INVALID", "shutdown receipt analysis is not active"
                )
            current_rows = connection.execute(
                "SELECT direct_job_id,state,attempt_count FROM eco_direct_jobs "
                "WHERE analysis_id=? AND state='RUNNING' ORDER BY direct_job_id",
                (analysis_id,),
            ).fetchall()
            current = [
                (str(row["direct_job_id"]), str(row["state"]), int(row["attempt_count"]))
                for row in current_rows
            ]
            expected = [
                (item["direct_job_id"], item["post_state"], item["post_attempt_count"])
                for item in covered_jobs
            ]
            if current != expected:
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_STATE_MISMATCH",
                    "direct-job state changed before shutdown receipt persistence",
                )
            outstanding = connection.execute(
                "SELECT generation,receipt_sha256,payload_json FROM "
                "eco_solver_shutdown_receipts WHERE project_id=? AND analysis_id=? "
                "AND state='UNUSED'",
                (project_id, analysis_id),
            ).fetchone()
            if outstanding is not None:
                existing_payload = json.loads(str(outstanding["payload_json"]))
                same_shutdown = (
                    existing_payload.get("project_path_sha256") == project_path_sha256
                    and existing_payload.get("engine_manifest_sha256") == engine_manifest_sha256
                    and existing_payload.get("workflow_identity_sha256") == workflow_identity_sha256
                    and existing_payload.get("jobs") == covered_jobs
                )
                if not same_shutdown:
                    raise SolverShutdownReceiptError(
                        "ORPHAN_RECEIPT_UNCONSUMED",
                        "another unconsumed shutdown generation already covers this analysis",
                    )
                return {
                    **existing_payload,
                    "receipt_sha256": str(outstanding["receipt_sha256"]),
                    "state": "UNUSED",
                }
            generation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation),0)+1 FROM eco_solver_shutdown_receipts "
                    "WHERE project_id=? AND analysis_id=?",
                    (project_id, analysis_id),
                ).fetchone()[0]
            )
            payload = {
                "schema_version": "ecocorridor-solver-shutdown-receipt/v1.0",
                "project_id": project_id,
                "project_path_sha256": project_path_sha256,
                "analysis_id": analysis_id,
                "generation": generation,
                "engine_manifest_sha256": engine_manifest_sha256,
                "workflow_identity_sha256": workflow_identity_sha256,
                "jobs": covered_jobs,
                "created_at": now,
            }
            payload_json = canonical_json(payload)
            receipt_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO eco_solver_shutdown_receipts("
                "project_id,analysis_id,generation,receipt_sha256,payload_json,state,created_at"
                ") VALUES(?,?,?,?,?,'UNUSED',?)",
                (
                    project_id,
                    analysis_id,
                    generation,
                    receipt_sha256,
                    payload_json,
                    now,
                ),
            )
        return {**payload, "receipt_sha256": receipt_sha256, "state": "UNUSED"}

    def consume_solver_shutdown_receipt(
        self,
        analysis_id: str,
        *,
        project_path_sha256: str,
        engine_manifest_sha256: str,
        workflow_identity_sha256: str,
    ) -> int:
        """Atomically consume exactly one matching receipt and requeue its covered rows."""

        expected_identities = {
            "project_path_sha256": self._receipt_sha256(
                project_path_sha256, label="project path identity"
            ),
            "engine_manifest_sha256": self._receipt_sha256(
                engine_manifest_sha256, label="engine manifest identity"
            ),
            "workflow_identity_sha256": self._receipt_sha256(
                workflow_identity_sha256, label="workflow identity"
            ),
        }
        project_id = self.project()["project_id"]
        with self.transaction() as connection:
            running_rows = connection.execute(
                "SELECT direct_job_id,state,attempt_count FROM eco_direct_jobs "
                "WHERE analysis_id=? AND state='RUNNING' ORDER BY direct_job_id",
                (analysis_id,),
            ).fetchall()
            if not running_rows:
                return 0
            receipt = connection.execute(
                "SELECT generation,receipt_sha256,payload_json,state "
                "FROM eco_solver_shutdown_receipts WHERE project_id=? AND analysis_id=? "
                "ORDER BY generation DESC LIMIT 1",
                (project_id, analysis_id),
            ).fetchone()
            if receipt is None:
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_MISSING",
                    "RUNNING direct jobs have no confirmed graceful-shutdown receipt",
                )
            payload_json = str(receipt["payload_json"])
            if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(
                receipt["receipt_sha256"]
            ):
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_INVALID", "shutdown receipt digest is invalid"
                )
            if str(receipt["state"]) != "UNUSED":
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_ALREADY_CONSUMED",
                    "the latest graceful-shutdown receipt has already been consumed",
                )
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_INVALID", "shutdown receipt JSON is invalid"
                ) from exc
            if (
                payload.get("schema_version") != "ecocorridor-solver-shutdown-receipt/v1.0"
                or payload.get("project_id") != project_id
                or payload.get("analysis_id") != analysis_id
                or payload.get("generation") != int(receipt["generation"])
                or any(payload.get(key) != value for key, value in expected_identities.items())
            ):
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_IDENTITY_MISMATCH",
                    "shutdown receipt belongs to another project, engine, or workflow",
                )
            jobs = payload.get("jobs")
            if not isinstance(jobs, list) or not jobs:
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_INVALID", "shutdown receipt has no covered direct jobs"
                )
            expected_rows: list[tuple[str, str, int]] = []
            for item in jobs:
                if not isinstance(item, dict) or item.get("tree_exit_confirmed") is not True:
                    raise SolverShutdownReceiptError(
                        "ORPHAN_TREE_EXIT_UNCONFIRMED",
                        "shutdown receipt does not confirm every covered process tree",
                    )
                self._receipt_sha256(item.get("process_identity"), label="solver process identity")
                process_id = item.get("process_id")
                process_group_id = item.get("process_group_id")
                pre_state = item.get("pre_state")
                pre_attempt_count = item.get("pre_attempt_count")
                post_attempt_count = item.get("post_attempt_count")
                if (
                    not isinstance(process_id, int)
                    or isinstance(process_id, bool)
                    or process_id <= 0
                    or not isinstance(process_group_id, int)
                    or isinstance(process_group_id, bool)
                    or process_group_id <= 0
                    or not isinstance(pre_attempt_count, int)
                    or isinstance(pre_attempt_count, bool)
                    or not isinstance(post_attempt_count, int)
                    or isinstance(post_attempt_count, bool)
                    or not (
                        (pre_state == "RUNNING" and pre_attempt_count == post_attempt_count)
                        or (pre_state == "QUEUED" and pre_attempt_count + 1 == post_attempt_count)
                    )
                    or not str(item.get("termination_reason", ""))
                    or not str(item.get("confirmation_method", ""))
                    or not str(item.get("launched_at", ""))
                    or not str(item.get("confirmed_at", ""))
                ):
                    raise SolverShutdownReceiptError(
                        "ORPHAN_RECEIPT_INVALID",
                        "shutdown receipt process or attempt evidence is invalid",
                    )
                if item.get("post_state") != "RUNNING":
                    raise SolverShutdownReceiptError(
                        "ORPHAN_RECEIPT_INVALID",
                        "shutdown receipt covers a non-RUNNING post-state",
                    )
                expected_rows.append(
                    (
                        str(item["direct_job_id"]),
                        "RUNNING",
                        int(item["post_attempt_count"]),
                    )
                )
            expected_rows.sort()
            current_rows = [
                (str(row["direct_job_id"]), str(row["state"]), int(row["attempt_count"]))
                for row in running_rows
            ]
            if current_rows != expected_rows:
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_STATE_MISMATCH",
                    "current RUNNING rows do not exactly match the shutdown receipt",
                )
            for direct_job_id, _, attempt_count in expected_rows:
                cursor = connection.execute(
                    "UPDATE eco_direct_jobs SET state='QUEUED',started_at=NULL,updated_at=? "
                    "WHERE direct_job_id=? AND analysis_id=? AND state='RUNNING' "
                    "AND attempt_count=?",
                    (_now(), direct_job_id, analysis_id, attempt_count),
                )
                if cursor.rowcount != 1:
                    raise SolverShutdownReceiptError(
                        "ORPHAN_RECEIPT_STATE_MISMATCH",
                        "a covered direct job changed during receipt consumption",
                    )
            cursor = connection.execute(
                "UPDATE eco_solver_shutdown_receipts SET state='CONSUMED',consumed_at=? "
                "WHERE project_id=? AND analysis_id=? AND generation=? AND state='UNUSED'",
                (_now(), project_id, analysis_id, int(receipt["generation"])),
            )
            if cursor.rowcount != 1:
                raise SolverShutdownReceiptError(
                    "ORPHAN_RECEIPT_ALREADY_CONSUMED",
                    "shutdown receipt was consumed concurrently",
                )
            return len(expected_rows)

    def solver_shutdown_receipts(self, analysis_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT generation,receipt_sha256,payload_json,state,created_at,consumed_at "
            "FROM eco_solver_shutdown_receipts WHERE analysis_id=? ORDER BY generation",
            (analysis_id,),
        ).fetchall()
        return [
            {
                "generation": int(row["generation"]),
                "receipt_sha256": str(row["receipt_sha256"]),
                "payload": json.loads(str(row["payload_json"])),
                "state": str(row["state"]),
                "created_at": str(row["created_at"]),
                "consumed_at": str(row["consumed_at"]) if row["consumed_at"] else None,
            }
            for row in rows
        ]

    def claim_next_direct_job(self, analysis_id: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            analysis = connection.execute(
                "SELECT state FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if analysis is None:
                raise KeyError(analysis_id)
            if analysis["state"] != AnalysisState.RUNNING.value:
                return None
            row = connection.execute(
                "SELECT * FROM eco_direct_jobs WHERE analysis_id=? AND state='QUEUED' "
                "ORDER BY sequence_number LIMIT 1",
                (analysis_id,),
            ).fetchone()
            if row is None:
                return None
            assert_transition(row["state"], DirectJobState.RUNNING, kind="direct_job")
            now = _now()
            cursor = connection.execute(
                "UPDATE eco_direct_jobs SET state='RUNNING',attempt_count=attempt_count+1,"
                "started_at=?,updated_at=? WHERE direct_job_id=? AND state='QUEUED'",
                (now, now, row["direct_job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = dict(row)
            claimed["state"] = DirectJobState.RUNNING.value
            claimed["attempt_count"] = int(row["attempt_count"]) + 1
            claimed["started_at"] = now
            return claimed

    def set_direct_job_prepared(
        self,
        direct_job_id: str,
        prepared: dict[str, Any],
        model_run_cache_key: str,
    ) -> None:
        prepared_json = canonical_json(prepared)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT state,prepared_json,model_run_cache_key FROM eco_direct_jobs "
                "WHERE direct_job_id=?",
                (direct_job_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(direct_job_id)
            if existing["prepared_json"] is not None:
                if (
                    str(existing["prepared_json"]) != prepared_json
                    or str(existing["model_run_cache_key"]) != model_run_cache_key
                ):
                    raise ValueError("prepared direct-job evidence is immutable")
                return
            cursor = connection.execute(
                "UPDATE eco_direct_jobs SET prepared_json=?,model_run_cache_key=?,updated_at=? "
                "WHERE direct_job_id=? AND state='RUNNING' AND prepared_json IS NULL",
                (prepared_json, model_run_cache_key, _now(), direct_job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("only a running direct job may be prepared")

    def direct_jobs(self, analysis_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM eco_direct_jobs WHERE analysis_id=? ORDER BY sequence_number",
            (analysis_id,),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["prepared"] = (
                json.loads(item.pop("prepared_json")) if item["prepared_json"] else None
            )
            item["error"] = json.loads(item.pop("error_json")) if item["error_json"] else None
            item["resource_observation"] = (
                json.loads(item.pop("resource_observation_json"))
                if item["resource_observation_json"]
                else None
            )
            item["terminal_artifacts"] = [
                dict(artifact)
                for artifact in self._connection.execute(
                    "SELECT role,sha256,byte_size,cas_relative_path,media_type "
                    "FROM eco_terminal_job_artifacts WHERE direct_job_id=? ORDER BY role",
                    (item["direct_job_id"],),
                )
            ]
            output.append(item)
        return output

    def direct_job_counts(self, analysis_id: str) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT state,COUNT(*) AS count FROM eco_direct_jobs WHERE analysis_id=? GROUP BY state",
            (analysis_id,),
        ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def cached_direct_run(
        self, model_run_cache_key: str
    ) -> tuple[DirectConnectivityRun, list[dict[str, Any]]] | None:
        row = self._connection.execute(
            "SELECT r.payload_json,c.result_sha256 FROM eco_model_run_cache c "
            "JOIN eco_analysis_runs r ON r.direct_run_id=c.source_direct_run_id "
            "WHERE c.model_run_cache_key=?",
            (model_run_cache_key,),
        ).fetchone()
        if row is None:
            return None
        run = DirectConnectivityRun.model_validate(json.loads(row["payload_json"]))
        if (
            run.result_sha256 != row["result_sha256"]
            or run.model_run_cache_key != model_run_cache_key
        ):
            raise ValueError("immutable model-run cache metadata is inconsistent")
        artifacts = [
            dict(item)
            for item in self._connection.execute(
                "SELECT role,sha256,byte_size,cas_relative_path,media_type "
                "FROM eco_run_artifacts WHERE direct_run_id=? ORDER BY role",
                (run.direct_run_id,),
            )
        ]
        if not artifacts:
            raise ValueError("cached direct result has no retained immutable artifacts")
        return run, artifacts

    def run_artifacts(self, direct_run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._connection.execute(
                "SELECT role,sha256,byte_size,cas_relative_path,media_type "
                "FROM eco_run_artifacts WHERE direct_run_id=? ORDER BY role",
                (direct_run_id,),
            )
        ]

    def verify_authoritative_direct_run(
        self,
        direct_run_id: str,
        *,
        expected_analysis_id: str | None = None,
    ) -> VerifiedDirectRunEvidence:
        """Rebuild and verify the complete durable chain for one successful run.

        This is the read-side counterpart to :meth:`finalize_direct_job`. It
        descriptor-hashes every retained artifact (including console artifacts
        that are later omitted from professional exports), reconstructs the
        terminal payload hash, and reconciles the run, prepared request, result
        protocol, raw matrix, metrics, QA, resource observation, provenance
        nodes, cache entry, and outbox identity.
        """

        with self._lock:
            job = self._connection.execute(
                "SELECT * FROM eco_direct_jobs WHERE direct_job_id=?",
                (direct_run_id,),
            ).fetchone()
            run_row = self._connection.execute(
                "SELECT * FROM eco_analysis_runs WHERE direct_run_id=?",
                (direct_run_id,),
            ).fetchone()
            if job is None or run_row is None:
                raise ValueError("direct evidence has no successful durable direct job and run")
            if expected_analysis_id is not None and str(job["analysis_id"]) != expected_analysis_id:
                raise ValueError("direct evidence belongs to a different analysis")

            run_json = str(run_row["payload_json"])
            run = DirectConnectivityRun.model_validate_json(run_json)
            analysis_row = self._connection.execute(
                "SELECT plan_json FROM eco_analyses WHERE analysis_id=?",
                (run.analysis_id,),
            ).fetchone()
            if analysis_row is None:
                raise ValueError("direct evidence has no immutable analysis plan")
            analysis_plan = ConnectivityRunPlan.model_validate_json(str(analysis_row["plan_json"]))
            if canonical_json(run.model_dump(mode="json")) != run_json:
                raise ValueError("direct-run payload is not canonical immutable evidence")
            if (
                job["state"] != DirectJobState.SUCCEEDED.value
                or run_row["state"] != DirectJobState.SUCCEEDED.value
                or run.state.value != DirectJobState.SUCCEEDED.value
                or str(job["direct_job_id"]) != run.direct_run_id
                or str(job["analysis_id"]) != run.analysis_id
                or str(job["portfolio_id"]) != run.portfolio_id
                or str(job["scenario_id"]) != run.scenario_id
                or str(job["phase"]) not in {"MATRIX", "MAP"}
                or str(job["model_run_cache_key"]) != run.model_run_cache_key
                or str(run_row["analysis_id"]) != run.analysis_id
                or str(run_row["portfolio_id"]) != run.portfolio_id
                or str(run_row["scenario_id"]) != run.scenario_id
                or str(run_row["model_run_cache_key"]) != run.model_run_cache_key
                or str(run_row["result_sha256"]) != run.result_sha256
                or not run.exact
                or run.test_only
                or run.engine_name != "Circuitscape"
                or run.engine_manifest_sha256 != analysis_plan.engine_manifest_sha256
                or run.revision_id != analysis_plan.revision_id
                or run.portfolio_id not in {item.portfolio_id for item in analysis_plan.portfolios}
                or run.scenario_id not in analysis_plan.scenario_ids
                or run.qa_status not in {QAStatus.ACCEPTED, QAStatus.WARNING_ACCEPTED}
                or job["completed_at"] is None
                or not re.fullmatch(r"[0-9a-f]{64}", str(job["terminal_sha256"] or ""))
            ):
                raise ValueError("direct evidence differs from its terminal durable job")

            prepared_json = job["prepared_json"]
            if not isinstance(prepared_json, str):
                raise TypeError("successful direct evidence has no prepared-run manifest")
            prepared = PreparedConnectivityRun.model_validate_json(prepared_json)
            if canonical_json(prepared.model_dump(mode="json")) != prepared_json or (
                prepared.prepared_run_id != run.direct_run_id
                or prepared.analysis_id != run.analysis_id
                or prepared.revision_id != run.revision_id
                or prepared.portfolio_id != run.portfolio_id
                or prepared.scenario.scenario_id != run.scenario_id
                or prepared.engine_manifest_sha256 != run.engine_manifest_sha256
                or prepared.model_run_cache_key != run.model_run_cache_key
            ):
                raise ValueError("prepared-run evidence differs from its durable direct run")

            run_artifact_rows = self._connection.execute(
                "SELECT rowid,role,sha256,byte_size,cas_relative_path,media_type "
                "FROM eco_run_artifacts WHERE direct_run_id=? ORDER BY rowid",
                (direct_run_id,),
            ).fetchall()
            terminal_artifact_rows = self._connection.execute(
                "SELECT rowid,role,sha256,byte_size,cas_relative_path,media_type "
                "FROM eco_terminal_job_artifacts WHERE direct_job_id=? ORDER BY rowid",
                (direct_run_id,),
            ).fetchall()
            run_artifacts = [
                {
                    key: row[key]
                    for key in ("role", "sha256", "byte_size", "cas_relative_path", "media_type")
                }
                for row in run_artifact_rows
            ]
            terminal_artifacts = [
                {
                    key: row[key]
                    for key in ("role", "sha256", "byte_size", "cas_relative_path", "media_type")
                }
                for row in terminal_artifact_rows
            ]
            roles = [str(item["role"]) for item in run_artifacts]
            required_roles = {
                "request",
                "result_protocol",
                "matrix",
                "circuitscape_configuration",
                "action_raster_diff",
                "footprint_proof_document",
                "input_resistance",
                "input_focal",
                "input_included_pairs",
            }
            if (
                not run_artifacts
                or run_artifacts != terminal_artifacts
                or len(roles) != len(set(roles))
                or required_roles - set(roles)
            ):
                raise ValueError("run and terminal artifact manifests are incomplete or detached")

            artifact_paths: dict[str, Path] = {}
            captured: dict[str, bytes] = {}
            capture_roles = {
                "request",
                "result_protocol",
                "matrix",
                "circuitscape_configuration",
                "action_raster_diff",
                "footprint_proof_document",
            }
            for artifact in run_artifacts:
                role = str(artifact["role"])
                digest = str(artifact["sha256"])
                media_type = str(artifact["media_type"])
                try:
                    byte_size = int(artifact["byte_size"])
                    path = contained_path(self.root, str(artifact["cas_relative_path"]))
                except (PathSecurityError, TypeError, ValueError) as exc:
                    raise ValueError("retained direct artifact metadata is unsafe") from exc
                if not re.fullmatch(r"[0-9a-f]{64}", digest) or byte_size < 0 or not media_type:
                    raise ValueError("retained direct artifact metadata is invalid")
                payload = _read_verified_regular_file(
                    path,
                    expected_sha256=digest,
                    expected_byte_size=byte_size,
                    capture_bytes=role in capture_roles,
                )
                artifact_paths[role] = path
                if payload is not None:
                    captured[role] = payload

            artifacts_by_role = {str(item["role"]): item for item in run_artifacts}
            if (
                artifacts_by_role["request"]["sha256"] != run.input_sha256
                or artifacts_by_role["result_protocol"]["sha256"] != run.result_sha256
                or artifacts_by_role["matrix"]["sha256"] != run.matrix_sha256
                or (
                    run.current_map_sha256 is not None
                    and artifacts_by_role.get("current_map", {}).get("sha256")
                    != run.current_map_sha256
                )
                or (run.current_map_sha256 is None and "current_map" in artifacts_by_role)
            ):
                raise ValueError("direct-run hashes differ from retained artifact identities")
            for resource in prepared.resources:
                artifact = artifacts_by_role.get(f"input_{resource.logical_name}")
                if artifact is None or artifact["sha256"] != resource.sha256:
                    raise ValueError("prepared resource differs from its retained input artifact")
            if (
                artifacts_by_role["action_raster_diff"]["sha256"] != prepared.action_raster_sha256
                or artifacts_by_role["footprint_proof_document"]["sha256"]
                != hashlib.sha256(captured["footprint_proof_document"]).hexdigest()
            ):
                raise ValueError("prepared semantic evidence differs from retained artifacts")

            evidence_rows = self._connection.execute(
                "SELECT rowid,node_id,payload_json FROM eco_run_evidence "
                "WHERE direct_run_id=? ORDER BY rowid",
                (direct_run_id,),
            ).fetchall()
            evidence_nodes: list[dict[str, Any]] = []
            for row in evidence_rows:
                payload_json = str(row["payload_json"])
                node = json.loads(payload_json)
                if (
                    not isinstance(node, dict)
                    or canonical_json(node) != payload_json
                    or row["node_id"] != node.get("node_id")
                ):
                    raise ValueError("direct-run provenance node differs from its stored payload")
                evidence_nodes.append(node)
            node_ids = [str(item.get("node_id", "")) for item in evidence_nodes]
            if not node_ids or len(node_ids) != len(set(node_ids)):
                raise ValueError("direct-run provenance node identities are incomplete")
            subject_nodes = [
                item
                for item in evidence_nodes
                if item.get("node_type") == "DIRECT_RUN" and not item.get("cache_source")
            ]
            scenario_nodes = [
                item for item in evidence_nodes if item.get("node_type") == "SCENARIO"
            ]
            engine_nodes = [
                item for item in evidence_nodes if item.get("node_type") == "ENGINE_MANIFEST"
            ]
            qa_nodes = [
                item
                for item in evidence_nodes
                if item.get("node_type") == "QA" and not item.get("cache_source")
            ]
            diff_nodes = [item for item in evidence_nodes if item.get("node_type") == "RASTER_DIFF"]
            proof_nodes = [
                item for item in evidence_nodes if item.get("node_type") == "FOOTPRINT_PROOF"
            ]
            artifact_nodes = [
                item for item in evidence_nodes if item.get("node_type") == "ARTIFACT"
            ]
            expected_artifact_nodes = {
                (f"artifact:{item['role']}:{item['sha256']}", item["role"], item["sha256"])
                for item in run_artifacts
            }
            observed_artifact_nodes = {
                (item.get("node_id"), item.get("role"), item.get("sha256"))
                for item in artifact_nodes
            }
            if (
                subject_nodes
                != [
                    {
                        "node_id": f"run:{run.direct_run_id}",
                        "node_type": "DIRECT_RUN",
                        "sha256": run.result_sha256,
                    }
                ]
                or scenario_nodes
                != [{"node_id": f"scenario:{run.scenario_id}", "node_type": "SCENARIO"}]
                or engine_nodes
                != [
                    {
                        "node_id": f"engine:{run.engine_manifest_sha256}",
                        "node_type": "ENGINE_MANIFEST",
                        "sha256": run.engine_manifest_sha256,
                    }
                ]
                or len(qa_nodes) != 1
                or qa_nodes[0].get("node_id") != f"qa:{run.direct_run_id}"
                or qa_nodes[0].get("status") != run.qa_status.value
                or len(diff_nodes) != 1
                or diff_nodes[0].get("node_id") != f"diff:{prepared.action_raster_sha256}"
                or diff_nodes[0].get("sha256") != prepared.action_raster_sha256
                or diff_nodes[0].get("footprint_proof_sha256") != prepared.footprint_proof_sha256
                or len(proof_nodes) != 1
                or proof_nodes[0].get("sha256")
                != artifacts_by_role["footprint_proof_document"]["sha256"]
                or observed_artifact_nodes != expected_artifact_nodes
                or len(artifact_nodes) != len(expected_artifact_nodes)
                or not any(item.get("node_type") == "ASSET" for item in evidence_nodes)
            ):
                raise ValueError("direct-run provenance graph differs from retained evidence")

            qa_row = self._connection.execute(
                "SELECT status,findings_json FROM eco_run_qa WHERE direct_run_id=?",
                (direct_run_id,),
            ).fetchone()
            metric_rows = self._connection.execute(
                "SELECT pair_id,effective_resistance,units FROM eco_run_metrics "
                "WHERE direct_run_id=? ORDER BY pair_id",
                (direct_run_id,),
            ).fetchall()
            if qa_row is None:
                raise ValueError("direct-run QA record is absent")
            qa_status = QAStatus(str(qa_row["status"]))
            qa_findings = json.loads(str(qa_row["findings_json"]))
            if (
                not isinstance(qa_findings, list)
                or canonical_json(qa_findings) != str(qa_row["findings_json"])
                or qa_status != run.qa_status
                or qa_findings != run.qa_findings
                or any(str(item["units"]) != "effective_resistance" for item in metric_rows)
                or {
                    str(item["pair_id"]): float(item["effective_resistance"])
                    for item in metric_rows
                }
                != {item.pair_id: item.effective_resistance for item in run.pair_metrics}
            ):
                raise ValueError("direct-run metrics or QA findings differ from retained evidence")

            resource_payload = (
                json.loads(str(job["resource_observation_json"]))
                if job["resource_observation_json"] is not None
                else None
            )
            resource_observation = (
                ComputationalResourceObservation.model_validate(resource_payload)
                if resource_payload is not None
                else None
            )
            if (
                (
                    resource_payload is not None
                    and canonical_json(resource_payload) != job["resource_observation_json"]
                )
                or resource_observation != run.resource_observation
                or job["error_json"] is not None
            ):
                raise ValueError(
                    "direct-run resource or error evidence differs from its terminal job"
                )

            try:
                request = json.loads(captured["request"])
                protocol = EngineProtocolResult.model_validate_json(captured["result_protocol"])
                diff = json.loads(captured["action_raster_diff"])
                proof = ProfessionalFootprintProofDocument.model_validate_json(
                    captured["footprint_proof_document"]
                )
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    "retained direct protocol or semantic evidence is invalid"
                ) from exc
            cache_lineage = [
                item for item in evidence_nodes if item.get("node_type") == "CACHE_LINEAGE"
            ]
            request_run_id = run.direct_run_id
            if bool(job["cached"]):
                if (
                    len(cache_lineage) != 1
                    or cache_lineage[0].get("requested_direct_run_id") != run.direct_run_id
                ):
                    raise ValueError("cached direct evidence has no exact source-run lineage")
                request_run_id = str(cache_lineage[0].get("source_direct_run_id", ""))
            elif cache_lineage:
                raise ValueError("uncached direct evidence carries unexpected cache lineage")
            if request != engine_request_payload(prepared, prepared_run_id=request_run_id):
                raise ValueError("retained request differs from the immutable prepared run")
            parsed_matrix = parse_reference_matrix_bytes(captured["matrix"], prepared.pairs)
            protocol_metrics = {
                item.pair_id: item.effective_resistance for item in protocol.pair_metrics
            }
            if (
                protocol.status != "SUCCEEDED"
                or protocol.warnings
                or protocol_metrics != parsed_matrix
                or parsed_matrix
                != {item.pair_id: item.effective_resistance for item in run.pair_metrics}
                or bool(protocol.current_map_path) != bool(run.current_map_sha256)
                or diff.get("revision_id") != prepared.revision_id
                or diff.get("portfolio_id") != prepared.portfolio_id
                or diff.get("scenario_id") != prepared.scenario.scenario_id
                or diff.get("prepared_resistance_sha256") != prepared.prepared_resistance_sha256
                or proof.revision_id != prepared.revision_id
                or proof.portfolio_id != prepared.portfolio_id
                or proof.scenario_id != prepared.scenario.scenario_id
                or proof.footprint_proof_sha256 != prepared.footprint_proof_sha256
            ):
                raise ValueError("retained direct outputs differ from prepared evidence")

            cache_row = self._connection.execute(
                "SELECT result_sha256,source_direct_run_id FROM eco_model_run_cache "
                "WHERE model_run_cache_key=?",
                (run.model_run_cache_key,),
            ).fetchone()
            expected_cache_source_id = request_run_id if bool(job["cached"]) else run.direct_run_id
            if (
                cache_row is None
                or str(cache_row["result_sha256"]) != run.result_sha256
                or str(cache_row["source_direct_run_id"]) != expected_cache_source_id
                or run.cache_source_direct_run_id
                != (expected_cache_source_id if bool(job["cached"]) else None)
            ):
                raise ValueError("direct-run cache identity is absent or inconsistent")

            terminal_payload = {
                "state": DirectJobState.SUCCEEDED.value,
                "run": run.model_dump(mode="json"),
                "artifacts": terminal_artifacts,
                "evidence_nodes": evidence_nodes,
                "resource_observation": (
                    resource_observation.model_dump(mode="json")
                    if resource_observation is not None
                    else None
                ),
                "error": None,
                "cached": bool(job["cached"]),
            }
            terminal_sha256 = hashlib.sha256(
                canonical_json(terminal_payload).encode("utf-8")
            ).hexdigest()
            if terminal_sha256 != str(job["terminal_sha256"]):
                raise ValueError("durable direct-job terminal hash failed reconstruction")

            analysis_project = self._connection.execute(
                "SELECT project_id FROM eco_analyses WHERE analysis_id=?",
                (run.analysis_id,),
            ).fetchone()
            if analysis_project is None:
                raise ValueError("direct-run terminal telemetry has no analysis project")
            outbox_rows = self._connection.execute(
                "SELECT project_id,analysis_id,event_type,subject_id,stream_id,payload_json,"
                "payload_sha256,idempotency_key FROM eco_telemetry_outbox "
                "WHERE project_id=? AND idempotency_key=?",
                (
                    analysis_project["project_id"],
                    f"direct_job_terminal:{run.direct_run_id}",
                ),
            ).fetchall()
            if len(outbox_rows) != 1:
                raise ValueError("direct-run terminal telemetry identity is missing or ambiguous")
            outbox_row = outbox_rows[0]
            outbox_json = str(outbox_row["payload_json"])
            outbox_payload = json.loads(outbox_json)
            if outbox_payload != {
                "cached": bool(job["cached"]),
                "direct_job_id": run.direct_run_id,
                "portfolio_id": run.portfolio_id,
                "result_sha256": run.result_sha256,
                "scenario_id": run.scenario_id,
                "state": DirectJobState.SUCCEEDED.value,
            } or (
                canonical_json(outbox_payload) != outbox_json
                or hashlib.sha256(outbox_json.encode("utf-8")).hexdigest()
                != outbox_row["payload_sha256"]
                or outbox_row["project_id"] != analysis_project["project_id"]
                or outbox_row["analysis_id"] != run.analysis_id
                or outbox_row["event_type"] != "direct_job_terminal"
                or outbox_row["subject_id"] != run.direct_run_id
                or outbox_row["stream_id"] != f"connectivity_analysis:{run.analysis_id}"
                or outbox_row["idempotency_key"] != f"direct_job_terminal:{run.direct_run_id}"
            ):
                raise ValueError("direct-run terminal telemetry differs from local science")

            return VerifiedDirectRunEvidence(
                run=run,
                prepared=prepared,
                artifacts=tuple(sorted(run_artifacts, key=lambda item: str(item["role"]))),
                artifact_paths_by_role=artifact_paths,
                evidence_nodes=tuple(evidence_nodes),
                qa_status=qa_status,
                qa_findings=tuple(str(item) for item in qa_findings),
                resource_observation=resource_observation,
                terminal_sha256=terminal_sha256,
                phase=str(job["phase"]),
                cached=bool(job["cached"]),
            )

    def finalize_direct_job(
        self,
        direct_job_id: str,
        state: DirectJobState | str,
        *,
        run: DirectConnectivityRun | None = None,
        artifacts: Sequence[dict[str, Any]] = (),
        evidence_nodes: Sequence[dict[str, Any]] = (),
        resource_observation: ComputationalResourceObservation | None = None,
        error: dict[str, Any] | None = None,
        cached: bool = False,
    ) -> None:
        """Atomically persist terminal science, lineage, QA, and its outbox envelope."""

        target = DirectJobState(state)
        if target not in {
            DirectJobState.SUCCEEDED,
            DirectJobState.QA_REJECTED,
            DirectJobState.FAILED,
            DirectJobState.CANCELLED,
        }:
            raise ValueError("direct-job finalization requires a terminal state")
        if (target == DirectJobState.SUCCEEDED) != (run is not None):
            raise ValueError("only a succeeded direct job may persist a release run")
        if run is not None:
            if (
                resource_observation is not None
                and resource_observation != run.resource_observation
            ):
                raise ValueError("separate resource observation differs from succeeded run")
            resource_observation = run.resource_observation
        if target == DirectJobState.SUCCEEDED and (not artifacts or not evidence_nodes):
            raise ValueError("successful direct jobs require artifacts and evidence nodes")
        roles = [str(item.get("role", "")) for item in artifacts]
        if len(roles) != len(set(roles)) or any(not role for role in roles):
            raise ValueError("terminal direct-job artifact roles must be non-empty and unique")
        artifact_paths_by_role: dict[str, Path] = {}
        for artifact in artifacts:
            digest = str(artifact["sha256"])
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("artifact digest must be lowercase SHA-256")
            try:
                path = contained_path(self.root, str(artifact["cas_relative_path"]))
            except PathSecurityError as exc:
                raise ValueError("artifact CAS path escapes the project") from exc
            if not path.is_file() or path.is_symlink():
                raise ValueError("artifact CAS path is absent or unsafe")
            actual = _sha256_file(path)
            if actual != digest or path.stat().st_size != int(artifact["byte_size"]):
                raise ValueError("artifact CAS bytes differ from finalized evidence")
            if not str(artifact.get("media_type", "")).strip():
                raise ValueError("terminal artifacts require media types")
            artifact_paths_by_role[str(artifact["role"])] = path
        if run is not None:
            artifact_by_role = {str(item["role"]): item for item in artifacts}
            required_artifact_roles = {
                "request",
                "result_protocol",
                "matrix",
                "circuitscape_configuration",
                "action_raster_diff",
                "footprint_proof_document",
                "input_resistance",
                "input_focal",
                "input_included_pairs",
            }
            if required_artifact_roles - set(artifact_by_role):
                raise ValueError("successful direct jobs omit required immutable artifacts")
            if artifact_by_role["request"]["sha256"] != run.input_sha256:
                raise ValueError("request artifact differs from the direct-run input hash")
            if artifact_by_role["result_protocol"]["sha256"] != run.result_sha256:
                raise ValueError("result protocol artifact differs from the direct-run hash")
            if artifact_by_role["matrix"]["sha256"] != run.matrix_sha256:
                raise ValueError("matrix artifact differs from the direct-run hash")
            if run.current_map_sha256 is not None and (
                artifact_by_role.get("current_map", {}).get("sha256") != run.current_map_sha256
            ):
                raise ValueError("current-map artifact differs from the direct-run hash")
            node_ids = [str(item.get("node_id", "")) for item in evidence_nodes]
            if not node_ids or len(node_ids) != len(set(node_ids)):
                raise ValueError("direct-job evidence node IDs must be unique")
            required_node_types = {
                "DIRECT_RUN",
                "SCENARIO",
                "RASTER_DIFF",
                "ASSET",
                "ENGINE_MANIFEST",
                "QA",
                "ARTIFACT",
                "FOOTPRINT_PROOF",
            }
            observed_node_types = {str(item.get("node_type", "")) for item in evidence_nodes}
            if not required_node_types.issubset(observed_node_types):
                raise ValueError("direct-job evidence graph omits required provenance nodes")
            footprint_nodes = [
                item for item in evidence_nodes if item.get("node_type") == "FOOTPRINT_PROOF"
            ]
            diff_nodes = [item for item in evidence_nodes if item.get("node_type") == "RASTER_DIFF"]
            if (
                len(footprint_nodes) != 1
                or footprint_nodes[0].get("sha256")
                != artifact_by_role["footprint_proof_document"]["sha256"]
                or len(diff_nodes) != 1
                or diff_nodes[0].get("sha256") != artifact_by_role["action_raster_diff"]["sha256"]
            ):
                raise ValueError("direct-job provenance differs from retained proof artifacts")
        terminal_payload = {
            "state": target.value,
            "run": run.model_dump(mode="json") if run else None,
            "artifacts": list(artifacts),
            "evidence_nodes": list(evidence_nodes),
            "resource_observation": (
                resource_observation.model_dump(mode="json")
                if resource_observation is not None
                else None
            ),
            "error": error,
            "cached": cached,
        }
        terminal_sha256 = hashlib.sha256(
            canonical_json(terminal_payload).encode("utf-8")
        ).hexdigest()
        with self.transaction() as connection:
            job = connection.execute(
                "SELECT * FROM eco_direct_jobs WHERE direct_job_id=?", (direct_job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(direct_job_id)
            if job["state"] in {
                DirectJobState.SUCCEEDED.value,
                DirectJobState.QA_REJECTED.value,
                DirectJobState.FAILED.value,
                DirectJobState.CANCELLED.value,
            }:
                if job["terminal_sha256"] != terminal_sha256:
                    raise ValueError("terminal direct-job evidence is immutable")
                return
            assert_transition(job["state"], target, kind="direct_job")
            if run is not None:
                if not job["prepared_json"]:
                    raise ValueError("successful direct job lacks immutable prepared evidence")
                analysis_row = connection.execute(
                    "SELECT plan_json FROM eco_analyses WHERE analysis_id=?",
                    (str(job["analysis_id"]),),
                ).fetchone()
                if analysis_row is None:
                    raise ValueError("successful direct job lacks an immutable analysis plan")
                analysis_plan = ConnectivityRunPlan.model_validate_json(
                    str(analysis_row["plan_json"])
                )
                if (
                    run.analysis_id != analysis_plan.analysis_id
                    or run.revision_id != analysis_plan.revision_id
                    or run.engine_manifest_sha256 != analysis_plan.engine_manifest_sha256
                    or run.portfolio_id
                    not in {item.portfolio_id for item in analysis_plan.portfolios}
                    or run.scenario_id not in analysis_plan.scenario_ids
                ):
                    raise ValueError(
                        "finalized direct run differs from its immutable analysis plan or engine"
                    )
                prepared = PreparedConnectivityRun.model_validate_json(job["prepared_json"])
                if (
                    prepared.prepared_run_id != direct_job_id
                    or prepared.analysis_id != job["analysis_id"]
                    or prepared.portfolio_id != job["portfolio_id"]
                    or prepared.scenario.scenario_id != job["scenario_id"]
                    or prepared.revision_id != run.revision_id
                    or prepared.engine_manifest_sha256 != run.engine_manifest_sha256
                    or prepared.model_run_cache_key != run.model_run_cache_key
                ):
                    raise ValueError("prepared evidence differs from its durable direct job")
                artifacts_by_role = {str(item["role"]): item for item in artifacts}
                for resource in prepared.resources:
                    artifact = artifacts_by_role.get(f"input_{resource.logical_name}")
                    if artifact is None or artifact["sha256"] != resource.sha256:
                        raise ValueError(
                            "finalized input artifact differs from immutable prepared resources"
                        )
                resistance = next(
                    (item for item in prepared.resources if item.logical_name == "resistance"),
                    None,
                )
                if resistance is None or resistance.sha256 != prepared.prepared_resistance_sha256:
                    raise ValueError("prepared resistance identity is inconsistent")
                diff_artifact = artifacts_by_role["action_raster_diff"]
                if diff_artifact["sha256"] != prepared.action_raster_sha256:
                    raise ValueError("finalized semantic diff differs from prepared evidence")
                try:
                    diff_payload = json.loads(
                        artifact_paths_by_role["action_raster_diff"].read_text(encoding="utf-8")
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("finalized semantic diff is not valid JSON") from exc
                if (
                    diff_payload.get("revision_id") != prepared.revision_id
                    or diff_payload.get("portfolio_id") != prepared.portfolio_id
                    or diff_payload.get("scenario_id") != prepared.scenario.scenario_id
                    or diff_payload.get("prepared_resistance_sha256")
                    != prepared.prepared_resistance_sha256
                ):
                    raise ValueError("finalized semantic diff has detached prepared identities")
                try:
                    proof = ProfessionalFootprintProofDocument.model_validate_json(
                        artifact_paths_by_role["footprint_proof_document"].read_text(
                            encoding="utf-8"
                        )
                    )
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ValueError("finalized footprint proof document is invalid") from exc
                if (
                    proof.revision_id != prepared.revision_id
                    or proof.portfolio_id != prepared.portfolio_id
                    or proof.scenario_id != prepared.scenario.scenario_id
                    or proof.footprint_proof_sha256 != prepared.footprint_proof_sha256
                ):
                    raise ValueError("finalized footprint proof differs from prepared evidence")
                expected_footprint_sources = {
                    f"reviewed_footprint_source:{source.asset.asset_id}": source.file_sha256
                    for source in proof.sources
                }
                observed_footprint_sources = {
                    role: str(artifact["sha256"])
                    for role, artifact in artifacts_by_role.items()
                    if role.startswith("reviewed_footprint_source:")
                }
                if observed_footprint_sources != expected_footprint_sources:
                    raise ValueError(
                        "finalized reviewed footprint sources differ from the proof document"
                    )
                try:
                    request_payload = json.loads(
                        artifact_paths_by_role["request"].read_text(encoding="utf-8")
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("finalized engine request is not valid JSON") from exc
                if request_payload != engine_request_payload(prepared):
                    raise ValueError("finalized engine request differs from prepared evidence")
                try:
                    protocol = EngineProtocolResult.model_validate_json(
                        artifact_paths_by_role["result_protocol"].read_text(encoding="utf-8")
                    )
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ValueError("finalized result protocol is invalid") from exc
                reparsed = parse_circuitscape_matrix(
                    artifact_paths_by_role["matrix"], prepared.pairs
                )
                protocol_metrics = [
                    item.model_dump(mode="json")
                    for item in sorted(protocol.pair_metrics, key=lambda item: item.pair_id)
                ]
                matrix_metrics = [
                    item.model_dump(mode="json")
                    for item in sorted(reparsed, key=lambda item: item.pair_id)
                ]
                run_metrics = {item.pair_id: item.effective_resistance for item in run.pair_metrics}
                if (
                    protocol.status != "SUCCEEDED"
                    or protocol.warnings
                    or protocol_metrics != matrix_metrics
                    or run_metrics != {item.pair_id: item.effective_resistance for item in reparsed}
                    or bool(protocol.current_map_path) != bool(run.current_map_sha256)
                ):
                    raise ValueError(
                        "finalized result protocol, raw matrix, and direct metrics differ"
                    )
                validate_effective_circuitscape_configuration(
                    artifact_paths_by_role["circuitscape_configuration"],
                    resource_paths={
                        item.logical_name: f"inputs/{item.target_filename}"
                        for item in prepared.resources
                    },
                    output_directory="outputs",
                    neighbor_rule=prepared.scenario.neighbor_rule.value,
                    resistance_average=prepared.scenario.resistance_average.value,
                    emit_current_map=prepared.emit_current_map,
                )
            analysis = connection.execute(
                "SELECT project_id FROM eco_analyses WHERE analysis_id=?", (job["analysis_id"],)
            ).fetchone()
            if analysis is None:
                raise KeyError(job["analysis_id"])
            for artifact in artifacts:
                connection.execute(
                    "INSERT INTO eco_terminal_job_artifacts(direct_job_id,role,sha256,byte_size,"
                    "cas_relative_path,media_type) VALUES(?,?,?,?,?,?)",
                    (
                        direct_job_id,
                        artifact["role"],
                        artifact["sha256"],
                        int(artifact["byte_size"]),
                        artifact["cas_relative_path"],
                        artifact["media_type"],
                    ),
                )
            if run is not None:
                if (
                    run.direct_run_id != direct_job_id
                    or run.analysis_id != job["analysis_id"]
                    or run.portfolio_id != job["portfolio_id"]
                    or run.scenario_id != job["scenario_id"]
                    or run.model_run_cache_key != job["model_run_cache_key"]
                ):
                    raise ValueError("direct-run evidence differs from its durable job identity")
                connection.execute(
                    "INSERT INTO eco_analysis_runs(direct_run_id,analysis_id,portfolio_id,"
                    "scenario_id,state,model_run_cache_key,result_sha256,payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        run.direct_run_id,
                        run.analysis_id,
                        run.portfolio_id,
                        run.scenario_id,
                        run.state.value,
                        run.model_run_cache_key,
                        run.result_sha256,
                        canonical_json(run.model_dump(mode="json")),
                    ),
                )
                for artifact in artifacts:
                    connection.execute(
                        "INSERT INTO eco_run_artifacts(direct_run_id,role,sha256,byte_size,"
                        "cas_relative_path,media_type) VALUES(?,?,?,?,?,?)",
                        (
                            run.direct_run_id,
                            artifact["role"],
                            artifact["sha256"],
                            int(artifact["byte_size"]),
                            artifact["cas_relative_path"],
                            artifact["media_type"],
                        ),
                    )
                for metric in run.pair_metrics:
                    connection.execute(
                        "INSERT INTO eco_run_metrics(direct_run_id,pair_id,effective_resistance,"
                        "units) VALUES(?,?,?,'effective_resistance')",
                        (run.direct_run_id, metric.pair_id, metric.effective_resistance),
                    )
                connection.execute(
                    "INSERT INTO eco_run_qa(direct_run_id,status,findings_json) VALUES(?,?,?)",
                    (
                        run.direct_run_id,
                        run.qa_status.value if run.qa_status else "ACCEPTED",
                        canonical_json(run.qa_findings),
                    ),
                )
                for node in evidence_nodes:
                    connection.execute(
                        "INSERT INTO eco_run_evidence(direct_run_id,node_id,payload_json) "
                        "VALUES(?,?,?)",
                        (run.direct_run_id, node["node_id"], canonical_json(node)),
                    )
                cached_row = connection.execute(
                    "SELECT result_sha256 FROM eco_model_run_cache WHERE model_run_cache_key=?",
                    (run.model_run_cache_key,),
                ).fetchone()
                if cached_row is None:
                    connection.execute(
                        "INSERT INTO eco_model_run_cache(model_run_cache_key,result_sha256,"
                        "source_direct_run_id,created_at) VALUES(?,?,?,?)",
                        (
                            run.model_run_cache_key,
                            run.result_sha256,
                            run.direct_run_id,
                            _now(),
                        ),
                    )
                elif cached_row["result_sha256"] != run.result_sha256:
                    raise ValueError("immutable model-run cache key produced different evidence")
            now = _now()
            connection.execute(
                "UPDATE eco_direct_jobs SET state=?,terminal_sha256=?,resource_observation_json=?,"
                "error_json=?,cached=?,"
                "completed_at=?,updated_at=? WHERE direct_job_id=?",
                (
                    target.value,
                    terminal_sha256,
                    (
                        canonical_json(resource_observation.model_dump(mode="json"))
                        if resource_observation is not None
                        else None
                    ),
                    canonical_json(error) if error else None,
                    int(cached),
                    now,
                    now,
                    direct_job_id,
                ),
            )
            self._enqueue_outbox(
                connection,
                project_id=str(analysis["project_id"]),
                analysis_id=str(job["analysis_id"]),
                event_type="direct_job_terminal",
                subject_id=direct_job_id,
                stream_id=f"connectivity_analysis:{job['analysis_id']}",
                payload={
                    "direct_job_id": direct_job_id,
                    "portfolio_id": job["portfolio_id"],
                    "scenario_id": job["scenario_id"],
                    "state": target.value,
                    "result_sha256": run.result_sha256 if run else None,
                    "cached": cached,
                },
                idempotency_key=f"direct_job_terminal:{direct_job_id}",
            )

    def cancel_queued_direct_jobs(self, analysis_id: str) -> int:
        rows = self._connection.execute(
            "SELECT direct_job_id FROM eco_direct_jobs WHERE analysis_id=? "
            "AND state IN ('PLANNED','QUEUED') ORDER BY sequence_number",
            (analysis_id,),
        ).fetchall()
        for row in rows:
            self.finalize_direct_job(
                str(row["direct_job_id"]),
                DirectJobState.CANCELLED,
                error={"code": "ANALYSIS_CANCELLED"},
            )
        return len(rows)

    @staticmethod
    def _enqueue_outbox(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        analysis_id: str | None,
        event_type: str,
        subject_id: str,
        stream_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> str:
        if (
            not stream_id
            or len(stream_id) > 512
            or any(character in stream_id for character in ("\x00", "\r", "\n"))
        ):
            raise ValueError("telemetry stream identity is invalid")
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        existing = connection.execute(
            "SELECT outbox_id,payload_sha256,event_type,subject_id,stream_id "
            "FROM eco_telemetry_outbox WHERE project_id=? AND idempotency_key=?",
            (project_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if (
                existing["payload_sha256"] != payload_sha256
                or existing["event_type"] != event_type
                or existing["subject_id"] != subject_id
                or existing["stream_id"] != stream_id
            ):
                raise ValueError("telemetry idempotency key was reused for different evidence")
            return str(existing["outbox_id"])
        outbox_id = (
            "outbox_"
            + hashlib.sha256(
                canonical_json(
                    [project_id, event_type, subject_id, idempotency_key, payload_sha256]
                ).encode("utf-8")
            ).hexdigest()[:24]
        )
        now = _now()
        outbox_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(outbox_sequence),0)+1 FROM eco_telemetry_outbox "
                "WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
        )
        stream_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(stream_sequence),0)+1 FROM eco_telemetry_outbox "
                "WHERE project_id=? AND stream_id=?",
                (project_id, stream_id),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO eco_telemetry_outbox(outbox_id,project_id,analysis_id,event_type,"
            "subject_id,stream_id,stream_sequence,outbox_sequence,payload_json,payload_sha256,"
            "idempotency_key,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                outbox_id,
                project_id,
                analysis_id,
                event_type,
                subject_id,
                stream_id,
                stream_sequence,
                outbox_sequence,
                payload_json,
                payload_sha256,
                idempotency_key,
                "PENDING",
                now,
                now,
            ),
        )
        return outbox_id

    @staticmethod
    def _validate_claim_run_references(
        *,
        plan: ConnectivityRunPlan,
        runs: Sequence[DirectConnectivityRun],
        claims: Sequence[ConnectivityEvidenceClaim],
    ) -> None:
        """Require the exact subject and plan-baseline run behind each delta claim."""

        portfolio_ids = {item.portfolio_id for item in plan.portfolios}
        run_by_id = {item.direct_run_id: item for item in runs}
        run_by_subject = {(item.portfolio_id, item.scenario_id): item for item in runs}
        claims_by_id = {item.claim_id: item for item in claims}
        if len(run_by_id) != len(runs) or len(run_by_subject) != len(runs):
            raise ValueError("completed analysis contains duplicate direct-run evidence")
        for claim in claims:
            validate_evidence_claim(claim)
            if claim.claim_kind == "DIRECT_REFERENCE_COMPARISON":
                raise ValueError(
                    "reference claims require an authoritative, atomically stored "
                    "ReferenceComparisonResult before repository acceptance"
                )
            if claim.portfolio_id not in portfolio_ids:
                raise ValueError("evidence claim references an unplanned portfolio")
            referenced = set(claim.direct_run_ids)
            if len(referenced) != len(claim.direct_run_ids) or any(
                run_id not in run_by_id for run_id in referenced
            ):
                raise ValueError("evidence claim references unavailable direct evidence")
            if claim.claim_kind == "AGGREGATE_RULE":
                supporting_ids = {
                    str(node.attributes.get("claim_id"))
                    for node in claim.nodes
                    if node.node_type == "SUPPORTING_CLAIM"
                    and isinstance(node.attributes.get("claim_id"), str)
                }
                if not supporting_ids or not supporting_ids.issubset(claims_by_id):
                    raise ValueError("aggregate evidence references unavailable primitive claims")
                supporting = [claims_by_id[item] for item in supporting_ids]
                allowed_aggregate_primitive_kinds = {
                    "DIRECT_PAIR_METRIC",
                    "DIRECT_MAP_DIAGNOSTIC",
                    "DIRECT_QA",
                }
                if any(
                    item.claim_kind not in allowed_aggregate_primitive_kinds
                    or item.portfolio_id != claim.portfolio_id
                    for item in supporting
                ):
                    raise ValueError("aggregate evidence must reduce subject primitive claims")
                expected_runs = {run_id for item in supporting for run_id in item.direct_run_ids}
                if (
                    not expected_runs
                    or referenced != expected_runs
                    or any(
                        run_by_id[run_id].portfolio_id
                        not in {claim.portfolio_id, plan.baseline_portfolio_id}
                        for run_id in referenced
                    )
                ):
                    raise ValueError(
                        "aggregate evidence run ensemble differs from its primitive claims"
                    )
                continue
            if claim.scenario_id is None:
                if any(
                    run_by_id[run_id].portfolio_id != claim.portfolio_id for run_id in referenced
                ):
                    raise ValueError(
                        "non-scenario evidence claims may reference only their subject portfolio"
                    )
                continue
            if claim.claim_kind == "DIRECT_QA":
                subject = run_by_subject.get((claim.portfolio_id, claim.scenario_id))
                if subject is None or referenced != {subject.direct_run_id}:
                    raise ValueError("QA evidence must reference exactly its subject direct run")
                continue
            try:
                subject = run_by_subject[(claim.portfolio_id, claim.scenario_id)]
                baseline = run_by_subject[(plan.baseline_portfolio_id, claim.scenario_id)]
            except KeyError as exc:
                raise ValueError(
                    "within-scenario evidence requires subject and plan-baseline direct runs"
                ) from exc
            required = {subject.direct_run_id, baseline.direct_run_id}
            if referenced != required:
                raise ValueError(
                    "within-scenario evidence must reference exactly its subject run and the "
                    "plan baseline run for the same scenario"
                )
            if claim.pair_id is not None and any(
                claim.pair_id not in {metric.pair_id for metric in run.pair_metrics}
                for run in (subject, baseline)
            ):
                raise ValueError(
                    "pair evidence must exist in both subject and baseline direct runs"
                )

    @staticmethod
    def _load_reference_comparison_plan(
        connection: sqlite3.Connection,
        *,
        plan_id: str,
        analysis_id: str | None = None,
    ) -> ReferenceComparisonPlan:
        query = "SELECT * FROM eco_reference_comparison_plans WHERE plan_id=?"
        parameters: tuple[str, ...] = (plan_id,)
        if analysis_id is not None:
            query += " AND analysis_id=?"
            parameters = (plan_id, analysis_id)
        row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise ValueError("reference comparison requires an immutable stored plan")
        payload_json = str(row["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != row["payload_sha256"]:
            raise ValueError("reference comparison plan failed its immutable checksum")
        plan = ReferenceComparisonPlan.model_validate_json(payload_json)
        if (
            canonical_json(plan.model_dump(mode="json")) != payload_json
            or row["plan_id"] != plan.plan_id
            or row["analysis_id"] != plan.analysis_id
            or row["revision_id"] != plan.revision_id
            or row["model_sha256"] != plan.model_sha256
            or row["engine_manifest_sha256"] != plan.engine_manifest_sha256
            or row["reference_matrix_parser_sha256"] != REFERENCE_MATRIX_PARSER_SHA256
        ):
            raise ValueError("reference comparison plan row differs from its payload")
        return plan

    def create_reference_analysis(
        self,
        reference_plan: ReferenceComparisonPlan,
        *,
        bundle: ConnectivityProjectBundle,
    ) -> tuple[ReferenceComparisonPlan, dict[str, Any]]:
        """Atomically create a READY reference analysis, snapshot, plan, and outbox.

        This is the only supported transition into ``analysis_kind=REFERENCE``.
        The transaction removes the former window in which generic analysis
        execution could queue work before the reference plan existed.
        """

        reference_plan = ReferenceComparisonPlan.model_validate(
            reference_plan.model_dump(mode="json")
        )
        bundle = ConnectivityProjectBundle.model_validate(bundle.model_dump(mode="json"))
        project_id = self.project()["project_id"]
        if bundle.project_id != project_id:
            raise ValueError("reference analysis bundle belongs to another project")
        if (
            bundle.revision.revision_id != reference_plan.revision_id
            or bundle.revision.model_sha256 != reference_plan.model_sha256
        ):
            raise ValueError("reference plan differs from its immutable model snapshot")
        canonical_plan = build_reference_comparison_plan(
            bundle=bundle,
            analysis_id=reference_plan.analysis_id,
            engine_manifest_sha256=reference_plan.engine_manifest_sha256,
            created_at=reference_plan.created_at,
        )
        if canonical_plan != reference_plan:
            raise ValueError(
                "reference plan is not the canonical complete No Action binding ensemble"
            )

        plan_json = canonical_json(reference_plan.run_plan.model_dump(mode="json"))
        bundle_json = canonical_json(bundle.model_dump(mode="json"))
        bundle_sha256 = hashlib.sha256(bundle_json.encode("utf-8")).hexdigest()
        reference_json = canonical_json(reference_plan.model_dump(mode="json"))
        reference_sha256 = hashlib.sha256(reference_json.encode("utf-8")).hexdigest()
        now = _now()
        with self.transaction() as connection:
            analysis = connection.execute(
                "SELECT * FROM eco_analyses WHERE analysis_id=?",
                (reference_plan.analysis_id,),
            ).fetchone()
            replayed = analysis is not None
            authoritative_state = (
                str(analysis["state"]) if analysis is not None else AnalysisState.READY.value
            )
            if analysis is None:
                connection.execute(
                    "INSERT INTO eco_analyses(analysis_id,project_id,analysis_kind,state,plan_json,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        reference_plan.analysis_id,
                        project_id,
                        "REFERENCE",
                        AnalysisState.READY.value,
                        plan_json,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO eco_analysis_models(analysis_id,bundle_sha256,bundle_json,"
                    "created_at) VALUES(?,?,?,?)",
                    (reference_plan.analysis_id, bundle_sha256, bundle_json, now),
                )
                connection.execute(
                    "INSERT INTO eco_reference_comparison_plans(plan_id,analysis_id,revision_id,"
                    "model_sha256,engine_manifest_sha256,reference_matrix_parser_sha256,"
                    "payload_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        reference_plan.plan_id,
                        reference_plan.analysis_id,
                        reference_plan.revision_id,
                        reference_plan.model_sha256,
                        reference_plan.engine_manifest_sha256,
                        REFERENCE_MATRIX_PARSER_SHA256,
                        reference_json,
                        reference_sha256,
                        now,
                    ),
                )
                self._enqueue_outbox(
                    connection,
                    project_id=project_id,
                    analysis_id=reference_plan.analysis_id,
                    event_type="analysis_plan",
                    subject_id=reference_plan.analysis_id,
                    stream_id=f"connectivity_analysis:{reference_plan.analysis_id}",
                    payload={
                        "analysis_id": reference_plan.analysis_id,
                        "revision_id": reference_plan.revision_id,
                        "bundle_sha256": bundle_sha256,
                    },
                    idempotency_key=(f"analysis_plan:{reference_plan.analysis_id}:{bundle_sha256}"),
                )
                persisted_plan = reference_plan
            else:
                model_row = connection.execute(
                    "SELECT bundle_sha256,bundle_json FROM eco_analysis_models WHERE analysis_id=?",
                    (reference_plan.analysis_id,),
                ).fetchone()
                stored_plan_rows = connection.execute(
                    "SELECT plan_id FROM eco_reference_comparison_plans WHERE analysis_id=?",
                    (reference_plan.analysis_id,),
                ).fetchall()
                if (
                    analysis["project_id"] != project_id
                    or analysis["analysis_kind"] != "REFERENCE"
                    or analysis["plan_json"] != plan_json
                    or model_row is None
                    or model_row["bundle_sha256"] != bundle_sha256
                    or model_row["bundle_json"] != bundle_json
                    or len(stored_plan_rows) != 1
                ):
                    raise ValueError(
                        "reference analysis identity was reused for different or incomplete evidence"
                    )
                persisted_plan = self._load_reference_comparison_plan(
                    connection,
                    plan_id=str(stored_plan_rows[0]["plan_id"]),
                    analysis_id=reference_plan.analysis_id,
                )
                comparable = reference_plan.model_copy(
                    update={"created_at": persisted_plan.created_at}
                )
                if comparable != persisted_plan:
                    raise ValueError(
                        "reference analysis identity was reused for different immutable evidence"
                    )
                reference_json = canonical_json(persisted_plan.model_dump(mode="json"))
                reference_sha256 = hashlib.sha256(reference_json.encode("utf-8")).hexdigest()
                self._enqueue_outbox(
                    connection,
                    project_id=project_id,
                    analysis_id=reference_plan.analysis_id,
                    event_type="analysis_plan",
                    subject_id=reference_plan.analysis_id,
                    stream_id=f"connectivity_analysis:{reference_plan.analysis_id}",
                    payload={
                        "analysis_id": reference_plan.analysis_id,
                        "revision_id": reference_plan.revision_id,
                        "bundle_sha256": bundle_sha256,
                    },
                    idempotency_key=(f"analysis_plan:{reference_plan.analysis_id}:{bundle_sha256}"),
                )
        return persisted_plan, {
            "plan_id": persisted_plan.plan_id,
            "plan_sha256": reference_sha256,
            "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
            "replayed": replayed,
            "state": authoritative_state,
        }

    def store_reference_comparison_plan(
        self,
        reference_plan: ReferenceComparisonPlan,
    ) -> dict[str, Any]:
        """Persist the reviewed baseline-only plan before any direct work begins."""

        reference_plan = ReferenceComparisonPlan.model_validate(
            reference_plan.model_dump(mode="json")
        )
        payload_json = canonical_json(reference_plan.model_dump(mode="json"))
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            analysis = connection.execute(
                "SELECT analysis_kind,state,plan_json FROM eco_analyses WHERE analysis_id=?",
                (reference_plan.analysis_id,),
            ).fetchone()
            if analysis is None:
                raise KeyError(reference_plan.analysis_id)
            if analysis["analysis_kind"] != "REFERENCE":
                raise ValueError("reference plans require an atomically created reference analysis")
            if canonical_json(reference_plan.run_plan.model_dump(mode="json")) != canonical_json(
                ConnectivityRunPlan.model_validate_json(str(analysis["plan_json"])).model_dump(
                    mode="json"
                )
            ):
                raise ValueError("reference plan differs from the immutable analysis run plan")
            model_row = connection.execute(
                "SELECT bundle_sha256,bundle_json FROM eco_analysis_models WHERE analysis_id=?",
                (reference_plan.analysis_id,),
            ).fetchone()
            if model_row is None:
                raise ValueError("reference plan has no immutable analysis model snapshot")
            bundle_json = str(model_row["bundle_json"])
            if (
                hashlib.sha256(bundle_json.encode("utf-8")).hexdigest()
                != model_row["bundle_sha256"]
            ):
                raise ValueError("analysis model snapshot failed its immutable checksum")
            bundle = ConnectivityProjectBundle.model_validate_json(bundle_json)
            try:
                canonical_plan = build_reference_comparison_plan(
                    bundle=bundle,
                    analysis_id=reference_plan.analysis_id,
                    engine_manifest_sha256=reference_plan.engine_manifest_sha256,
                    created_at=reference_plan.created_at,
                )
            except ValueError as exc:
                raise ValueError(
                    "reference plan cannot be reconstructed from its immutable model snapshot"
                ) from exc
            if reference_plan != canonical_plan:
                raise ValueError(
                    "reference plan is not the canonical complete No Action binding ensemble"
                )
            existing = connection.execute(
                "SELECT * FROM eco_reference_comparison_plans WHERE plan_id=?",
                (reference_plan.plan_id,),
            ).fetchone()
            replayed = existing is not None
            if existing is not None:
                if (
                    existing["analysis_id"] != reference_plan.analysis_id
                    or existing["revision_id"] != reference_plan.revision_id
                    or existing["model_sha256"] != reference_plan.model_sha256
                    or existing["engine_manifest_sha256"] != reference_plan.engine_manifest_sha256
                    or existing["reference_matrix_parser_sha256"] != REFERENCE_MATRIX_PARSER_SHA256
                    or existing["payload_json"] != payload_json
                    or existing["payload_sha256"] != payload_sha256
                ):
                    raise ValueError(
                        "reference comparison plan identity was reused for different evidence"
                    )
            else:
                raise ValueError(
                    "reference plan is missing from its atomic reference-analysis creation"
                )
        return {
            "plan_id": reference_plan.plan_id,
            "plan_sha256": payload_sha256,
            "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
            "replayed": replayed,
        }

    def reference_comparison_plan(
        self,
        plan_id: str,
        *,
        analysis_id: str | None = None,
    ) -> tuple[ReferenceComparisonPlan, str]:
        """Return the exact canonical stored plan and its immutable payload digest."""

        with self._lock:
            plan = self._load_reference_comparison_plan(
                self._connection,
                plan_id=plan_id,
                analysis_id=analysis_id,
            )
            payload_sha256 = hashlib.sha256(
                canonical_json(plan.model_dump(mode="json")).encode("utf-8")
            ).hexdigest()
            return plan, payload_sha256

    def reference_comparison_plan_for_analysis(
        self,
        analysis_id: str,
    ) -> tuple[ReferenceComparisonPlan, str] | None:
        """Return the sole immutable reference plan bound to an analysis.

        Ambiguous plan ownership is treated as integrity failure instead of
        selecting an arbitrary row. The workflow uses this readback before it
        schedules or completes direct work.
        """

        with self._lock:
            rows = self._connection.execute(
                "SELECT plan_id FROM eco_reference_comparison_plans "
                "WHERE analysis_id=? ORDER BY plan_id",
                (analysis_id,),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise ValueError("analysis has ambiguous immutable reference plans")
            return self.reference_comparison_plan(
                str(rows[0]["plan_id"]),
                analysis_id=analysis_id,
            )

    @staticmethod
    def _reference_completion_rows(
        connection: sqlite3.Connection,
        analysis_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT run.direct_run_id,run.portfolio_id,run.scenario_id,"
            "job.phase,job.state,job.terminal_sha256,run.result_sha256 "
            "FROM eco_analysis_runs AS run JOIN eco_direct_jobs AS job "
            "ON job.direct_job_id=run.direct_run_id WHERE run.analysis_id=? "
            "ORDER BY run.portfolio_id,run.scenario_id,run.direct_run_id",
            (analysis_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _load_reference_completion_intent(
        connection: sqlite3.Connection,
        *,
        analysis_id: str,
    ) -> tuple[str, dict[str, Any], str] | None:
        row = connection.execute(
            "SELECT completion_id,plan_id,payload_json,payload_sha256 "
            "FROM eco_reference_completion_intents WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        if row is None:
            return None
        payload_json = str(row["payload_json"])
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("reference completion intent is not valid JSON") from exc
        if (
            canonical_json(payload) != payload_json
            or row["payload_sha256"] != payload_sha256
            or payload.get("analysis_id") != analysis_id
            or payload.get("plan_id") != row["plan_id"]
            or row["completion_id"] != f"reference_completion_{payload_sha256[:24]}"
        ):
            raise ValueError("reference completion intent failed its immutable identity")
        return str(row["completion_id"]), payload, payload_sha256

    def reference_completion_intent(
        self,
        analysis_id: str,
    ) -> tuple[str, dict[str, Any], str] | None:
        with self._lock:
            return self._load_reference_completion_intent(
                self._connection,
                analysis_id=analysis_id,
            )

    def pending_reference_completions(self) -> list[str]:
        """Return completed reference analyses whose canonical result is not retained yet."""

        with self._lock:
            return [
                str(row["analysis_id"])
                for row in self._connection.execute(
                    "SELECT intent.analysis_id FROM eco_reference_completion_intents AS intent "
                    "LEFT JOIN eco_reference_comparison_results AS result "
                    "ON result.analysis_id=intent.analysis_id AND result.plan_id=intent.plan_id "
                    "WHERE result.result_id IS NULL ORDER BY intent.analysis_id"
                )
            ]

    def complete_reference_direct_analysis(self, analysis_id: str) -> dict[str, Any]:
        """Atomically commit terminal state plus a recoverable producer identity.

        Descriptor hashing and protocol parsing happen before the write
        transaction. The short commit re-derives the exact immutable run
        manifest before changing state and enqueuing completion telemetry.
        """

        with self._lock:
            analysis = self._connection.execute(
                "SELECT project_id,analysis_kind,state,plan_json FROM eco_analyses "
                "WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
            if analysis is None:
                raise KeyError(analysis_id)
            if analysis["analysis_kind"] != "REFERENCE":
                raise ValueError("reference completion requires a reference analysis")
            if analysis["state"] not in {
                AnalysisState.RUNNING.value,
                AnalysisState.COMPLETED.value,
            }:
                raise ValueError("reference completion requires a running analysis")
            entry = self.reference_comparison_plan_for_analysis(analysis_id)
            if entry is None:
                raise ValueError("reference completion requires an immutable reference plan")
            reference_plan, _ = entry
            analysis_plan = ConnectivityRunPlan.model_validate_json(str(analysis["plan_json"]))
            if canonical_json(analysis_plan.model_dump(mode="json")) != canonical_json(
                reference_plan.run_plan.model_dump(mode="json")
            ):
                raise ValueError("reference completion plan differs from the analysis plan")
            expected = {
                (portfolio.portfolio_id, scenario_id)
                for portfolio in analysis_plan.portfolios
                for scenario_id in analysis_plan.scenario_ids
            }
            completion_rows = self._reference_completion_rows(self._connection, analysis_id)
            observed = {
                (str(row["portfolio_id"]), str(row["scenario_id"])) for row in completion_rows
            }
            if (
                observed != expected
                or len(completion_rows) != len(expected)
                or any(
                    row["phase"] != "MATRIX"
                    or row["state"] != DirectJobState.SUCCEEDED.value
                    or not re.fullmatch(r"[0-9a-f]{64}", str(row["terminal_sha256"] or ""))
                    for row in completion_rows
                )
            ):
                raise ValueError(
                    "reference completion requires every planned durable matrix job to succeed"
                )
            verified = {
                str(row["direct_run_id"]): self.verify_authoritative_direct_run(
                    str(row["direct_run_id"]),
                    expected_analysis_id=analysis_id,
                )
                for row in completion_rows
            }
            if any(
                verified[str(row["direct_run_id"])].run.result_sha256 != row["result_sha256"]
                for row in completion_rows
            ):
                raise ValueError("reference completion run manifest changed during verification")
            completion_id, completion_payload, completion_sha256 = _reference_completion_manifest(
                plan=reference_plan,
                direct_runs=completion_rows,
            )

        completion_json = canonical_json(completion_payload)
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT project_id,analysis_kind,state,plan_json FROM eco_analyses "
                "WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
            if (
                current is None
                or current["analysis_kind"] != "REFERENCE"
                or current["plan_json"] != canonical_json(analysis_plan.model_dump(mode="json"))
            ):
                raise ValueError("reference completion analysis identity changed before commit")
            committed_plan = self._load_reference_comparison_plan(
                connection,
                plan_id=reference_plan.plan_id,
                analysis_id=analysis_id,
            )
            current_rows = self._reference_completion_rows(connection, analysis_id)
            if current_rows != completion_rows:
                raise ValueError("reference completion durable rows changed before commit")
            current_id, current_payload, current_sha256 = _reference_completion_manifest(
                plan=committed_plan,
                direct_runs=current_rows,
            )
            if (
                current_id != completion_id
                or current_payload != completion_payload
                or current_sha256 != completion_sha256
            ):
                raise ValueError("reference completion evidence changed before commit")
            existing = self._load_reference_completion_intent(
                connection,
                analysis_id=analysis_id,
            )
            replayed = existing is not None
            if existing is not None:
                if existing != (completion_id, completion_payload, completion_sha256):
                    raise ValueError(
                        "reference completion identity was reused for different evidence"
                    )
                if current["state"] != AnalysisState.COMPLETED.value:
                    raise ValueError("reference completion intent exists before terminal state")
            else:
                if current["state"] != AnalysisState.RUNNING.value:
                    raise ValueError("completed reference analysis lacks its producer identity")
                assert_transition(current["state"], AnalysisState.COMPLETED, kind="analysis")
                connection.execute(
                    "INSERT INTO eco_reference_completion_intents(completion_id,analysis_id,plan_id,"
                    "payload_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        completion_id,
                        analysis_id,
                        reference_plan.plan_id,
                        completion_json,
                        completion_sha256,
                        _now(),
                    ),
                )
                connection.execute(
                    "UPDATE eco_analyses SET state='COMPLETED',updated_at=? WHERE analysis_id=?",
                    (_now(), analysis_id),
                )
            outbox_id = self._enqueue_outbox(
                connection,
                project_id=str(current["project_id"]),
                analysis_id=analysis_id,
                event_type="analysis_completion",
                subject_id=analysis_id,
                stream_id=f"connectivity_analysis:{analysis_id}",
                payload={
                    "analysis_id": analysis_id,
                    "completion_kind": "REFERENCE_RESULT_PENDING",
                    "completion_id": completion_id,
                    "plan_id": reference_plan.plan_id,
                    "reference_completion_sha256": completion_sha256,
                    "direct_run_ids": sorted(verified),
                },
                idempotency_key=f"analysis_completion:{analysis_id}",
            )
        return {
            "completion_id": completion_id,
            "reference_completion_sha256": completion_sha256,
            "outbox_id": outbox_id,
            "replayed": replayed,
        }

    @staticmethod
    def _reference_persistence_sha256(
        connection: sqlite3.Connection,
        *,
        analysis_id: str,
    ) -> str:
        """Hash immutable database identities used by reference verification.

        Cloud-delivery state is intentionally excluded: leases and receipts may
        advance while scientific evidence is being descriptor-verified.
        """

        tables: list[tuple[str, str, tuple[Any, ...]]] = [
            (
                "analysis",
                (
                    "SELECT analysis_id,project_id,analysis_kind,state,plan_json,error_json,"
                    "created_at FROM eco_analyses WHERE analysis_id=?"
                ),
                (analysis_id,),
            ),
            (
                "model",
                (
                    "SELECT analysis_id,bundle_sha256,bundle_json,created_at "
                    "FROM eco_analysis_models WHERE analysis_id=?"
                ),
                (analysis_id,),
            ),
            (
                "reference_plan",
                "SELECT * FROM eco_reference_comparison_plans WHERE analysis_id=? ORDER BY plan_id",
                (analysis_id,),
            ),
            (
                "reference_completion",
                (
                    "SELECT * FROM eco_reference_completion_intents WHERE analysis_id=? "
                    "ORDER BY completion_id"
                ),
                (analysis_id,),
            ),
            (
                "jobs",
                (
                    "SELECT direct_job_id,analysis_id,portfolio_id,scenario_id,phase,"
                    "sequence_number,state,emit_current_map,prepared_json,model_run_cache_key,"
                    "terminal_sha256,resource_observation_json,error_json,cached,attempt_count,"
                    "created_at,started_at,completed_at FROM eco_direct_jobs WHERE analysis_id=? "
                    "ORDER BY sequence_number"
                ),
                (analysis_id,),
            ),
            (
                "runs",
                "SELECT * FROM eco_analysis_runs WHERE analysis_id=? ORDER BY direct_run_id",
                (analysis_id,),
            ),
            (
                "artifacts",
                (
                    "SELECT artifact.* FROM eco_run_artifacts AS artifact "
                    "JOIN eco_analysis_runs AS run ON run.direct_run_id=artifact.direct_run_id "
                    "WHERE run.analysis_id=? ORDER BY artifact.direct_run_id,artifact.role"
                ),
                (analysis_id,),
            ),
            (
                "terminal_artifacts",
                (
                    "SELECT artifact.* FROM eco_terminal_job_artifacts AS artifact "
                    "JOIN eco_direct_jobs AS job ON job.direct_job_id=artifact.direct_job_id "
                    "WHERE job.analysis_id=? ORDER BY artifact.direct_job_id,artifact.role"
                ),
                (analysis_id,),
            ),
            (
                "metrics",
                (
                    "SELECT metric.* FROM eco_run_metrics AS metric JOIN eco_analysis_runs AS run "
                    "ON run.direct_run_id=metric.direct_run_id WHERE run.analysis_id=? "
                    "ORDER BY metric.direct_run_id,metric.pair_id"
                ),
                (analysis_id,),
            ),
            (
                "qa",
                (
                    "SELECT qa.* FROM eco_run_qa AS qa JOIN eco_analysis_runs AS run "
                    "ON run.direct_run_id=qa.direct_run_id WHERE run.analysis_id=? "
                    "ORDER BY qa.direct_run_id"
                ),
                (analysis_id,),
            ),
            (
                "evidence",
                (
                    "SELECT evidence.* FROM eco_run_evidence AS evidence "
                    "JOIN eco_analysis_runs AS run ON run.direct_run_id=evidence.direct_run_id "
                    "WHERE run.analysis_id=? ORDER BY evidence.direct_run_id,evidence.node_id"
                ),
                (analysis_id,),
            ),
            (
                "cache",
                (
                    "SELECT DISTINCT cache.* FROM eco_model_run_cache AS cache "
                    "JOIN eco_analysis_runs AS run "
                    "ON run.model_run_cache_key=cache.model_run_cache_key WHERE run.analysis_id=? "
                    "ORDER BY cache.model_run_cache_key"
                ),
                (analysis_id,),
            ),
            (
                "outbox",
                (
                    "SELECT project_id,analysis_id,event_type,subject_id,stream_id,"
                    "stream_sequence,outbox_sequence,payload_json,payload_sha256,idempotency_key,"
                    "created_at FROM eco_telemetry_outbox WHERE analysis_id=? AND event_type IN "
                    "('analysis_plan','direct_job_terminal','analysis_completion') "
                    "ORDER BY outbox_sequence,outbox_id"
                ),
                (analysis_id,),
            ),
        ]
        snapshot = {
            name: [dict(row) for row in connection.execute(sql, parameters).fetchall()]
            for name, sql, parameters in tables
        }
        return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()

    def _preflight_reference_result(
        self,
        *,
        result: ReferenceComparisonResult,
        claims: Sequence[ConnectivityEvidenceClaim],
    ) -> tuple[ReferenceComparisonPlan, ReferenceValidationSeal]:
        """Perform descriptor-heavy validation without holding a writer lock."""

        with self._lock:
            reference_plan = self._load_reference_comparison_plan(
                self._connection,
                plan_id=result.plan_id,
                analysis_id=result.analysis_id,
            )
            completion = self._load_reference_completion_intent(
                self._connection,
                analysis_id=result.analysis_id,
            )
            if completion is None or completion[1].get("plan_id") != result.plan_id:
                raise ValueError(
                    "reference result requires its atomic terminal completion identity"
                )
            before = self._reference_persistence_sha256(
                self._connection,
                analysis_id=result.analysis_id,
            )
            verified_runs: dict[str, VerifiedDirectRunEvidence] = {}
            project_id, claims_by_id = self._validate_reference_comparison_evidence(
                self._connection,
                reference_plan=reference_plan,
                result=result,
                claims=claims,
                verified_runs=verified_runs,
            )
            after = self._reference_persistence_sha256(
                self._connection,
                analysis_id=result.analysis_id,
            )
            if before != after:
                raise ValueError("reference evidence changed during descriptor verification")
            return reference_plan, ReferenceValidationSeal(
                project_id=project_id,
                claims_by_id=claims_by_id,
                persistence_sha256=after,
                verified_runs=verified_runs,
            )

    def _validate_reference_comparison_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        reference_plan: ReferenceComparisonPlan,
        result: ReferenceComparisonResult,
        claims: Sequence[ConnectivityEvidenceClaim],
        verified_runs: dict[str, VerifiedDirectRunEvidence] | None = None,
    ) -> tuple[str, dict[str, ConnectivityEvidenceClaim]]:
        """Reconcile a reference result with immutable local science and source bytes."""

        # ``model_copy`` can bypass model validators.  Persistence is a trust
        # boundary, so deserialize every caller-supplied contract again.
        reference_plan = ReferenceComparisonPlan.model_validate(
            reference_plan.model_dump(mode="json")
        )
        result = ReferenceComparisonResult.model_validate(result.model_dump(mode="json"))
        validated_claims = [
            ConnectivityEvidenceClaim.model_validate(item.model_dump(mode="json"))
            for item in claims
        ]
        claims_by_id = {item.claim_id: item for item in validated_claims}
        if len(claims_by_id) != len(validated_claims):
            raise ValueError("reference comparison contains duplicate evidence claim IDs")
        run_evidence = verified_runs if verified_runs is not None else {}

        def verified_direct_run(direct_run_id: str) -> VerifiedDirectRunEvidence:
            existing = run_evidence.get(direct_run_id)
            if existing is not None:
                if existing.run.analysis_id != result.analysis_id:
                    raise ValueError("cached direct evidence belongs to another analysis")
                return existing
            verified = self.verify_authoritative_direct_run(
                direct_run_id,
                expected_analysis_id=result.analysis_id,
            )
            run_evidence[direct_run_id] = verified
            return verified

        analysis = connection.execute(
            "SELECT project_id,analysis_kind,state,plan_json FROM eco_analyses WHERE analysis_id=?",
            (result.analysis_id,),
        ).fetchone()
        if analysis is None:
            raise KeyError(result.analysis_id)
        if analysis["state"] != AnalysisState.COMPLETED.value:
            raise ValueError(
                "reference comparison result requires a completed baseline direct ensemble"
            )
        if analysis["analysis_kind"] != "REFERENCE":
            raise ValueError("reference comparison result belongs to a non-reference analysis")
        completion = self._load_reference_completion_intent(
            connection,
            analysis_id=result.analysis_id,
        )
        if completion is None or completion[1].get("plan_id") != result.plan_id:
            raise ValueError("reference comparison result has no terminal producer identity")
        analysis_plan = ConnectivityRunPlan.model_validate_json(str(analysis["plan_json"]))
        model_row = connection.execute(
            "SELECT bundle_sha256,bundle_json FROM eco_analysis_models WHERE analysis_id=?",
            (result.analysis_id,),
        ).fetchone()
        if model_row is None:
            raise ValueError("reference comparison has no immutable analysis model snapshot")
        bundle_json = str(model_row["bundle_json"])
        if hashlib.sha256(bundle_json.encode("utf-8")).hexdigest() != model_row["bundle_sha256"]:
            raise ValueError("analysis model snapshot failed its immutable checksum")
        bundle = ConnectivityProjectBundle.model_validate_json(bundle_json)
        if (
            reference_plan.plan_id != result.plan_id
            or reference_plan.analysis_id != result.analysis_id
            or reference_plan.revision_id != result.revision_id
            or reference_plan.model_sha256 != result.model_sha256
            or reference_plan.engine_manifest_sha256 != result.engine_manifest_sha256
            or canonical_json(reference_plan.run_plan.model_dump(mode="json"))
            != canonical_json(analysis_plan.model_dump(mode="json"))
            or analysis_plan.analysis_id != result.analysis_id
            or analysis_plan.revision_id != result.revision_id
            or bundle.revision.revision_id != result.revision_id
            or bundle.revision.model_sha256 != result.model_sha256
            or analysis_plan.engine_manifest_sha256 != result.engine_manifest_sha256
        ):
            raise ValueError(
                "reference plan or result differs from its analysis, revision, model, or engine"
            )
        try:
            canonical_plan = build_reference_comparison_plan(
                bundle=bundle,
                analysis_id=reference_plan.analysis_id,
                engine_manifest_sha256=reference_plan.engine_manifest_sha256,
                created_at=reference_plan.created_at,
            )
        except ValueError as exc:
            raise ValueError(
                "reference plan cannot be reconstructed from its immutable model snapshot"
            ) from exc
        if reference_plan != canonical_plan:
            raise ValueError(
                "stored reference plan is not the canonical complete No Action binding ensemble"
            )

        expected_direct_subjects = {
            (portfolio.portfolio_id, scenario_id)
            for portfolio in analysis_plan.portfolios
            for scenario_id in analysis_plan.scenario_ids
        }
        job_rows = connection.execute(
            "SELECT direct_job_id,portfolio_id,scenario_id,phase,state "
            "FROM eco_direct_jobs WHERE analysis_id=? ORDER BY sequence_number",
            (result.analysis_id,),
        ).fetchall()
        observed_job_subjects = {
            (str(row["portfolio_id"]), str(row["scenario_id"])) for row in job_rows
        }
        if (
            observed_job_subjects != expected_direct_subjects
            or len(job_rows) != len(expected_direct_subjects)
            or any(
                row["phase"] != "MATRIX" or row["state"] != DirectJobState.SUCCEEDED.value
                for row in job_rows
            )
        ):
            raise ValueError(
                "reference result requires exact successful durable coverage of its run plan"
            )
        run_rows = connection.execute(
            "SELECT direct_run_id,portfolio_id,scenario_id FROM eco_analysis_runs "
            "WHERE analysis_id=? ORDER BY direct_run_id",
            (result.analysis_id,),
        ).fetchall()
        observed_run_subjects = {
            (str(row["portfolio_id"]), str(row["scenario_id"])) for row in run_rows
        }
        if (
            observed_run_subjects != expected_direct_subjects
            or len(run_rows) != len(expected_direct_subjects)
            or {str(row["direct_job_id"]) for row in job_rows}
            != {str(row["direct_run_id"]) for row in run_rows}
        ):
            raise ValueError(
                "reference result requires one exact durable run for every planned job"
            )
        for run_row in run_rows:
            verified_direct_run(str(run_row["direct_run_id"]))

        scenarios = {item.scenario_id: item for item in bundle.scenarios}
        planned_binding_ids = set(reference_plan.binding_ids)
        bindings = {
            item.scenario_id: item
            for item in bundle.reference_bindings
            if item.binding_id in planned_binding_ids
        }
        if (
            set(bindings) != set(reference_plan.bound_scenario_ids)
            or {item.binding_id for item in bindings.values()} != planned_binding_ids
        ):
            raise ValueError(
                "reference plan binding scope differs from the immutable analysis model"
            )
        comparison_by_scenario = {item.scenario_id: item for item in result.scenario_comparisons}
        if set(comparison_by_scenario) != set(reference_plan.bound_scenario_ids):
            raise ValueError("reference result coverage differs from its immutable reference plan")
        if not set(comparison_by_scenario).issubset(analysis_plan.scenario_ids):
            raise ValueError("reference comparison contains an unplanned scenario")

        assets = {item.asset_id: item for item in bundle.assets}
        focal_sets = {item.focal_set_id: item for item in bundle.focal_area_sets}
        canonical_comparisons: dict[str, Any] = {}
        pair_comparison_by_subject: dict[tuple[str, str], ReferencePairComparison] = {}
        run_by_scenario: dict[str, DirectConnectivityRun] = {}
        matrix_asset_by_scenario: dict[str, Any] = {}

        def verify_asset_bytes(
            asset_id: str,
            expected_kind: AssetKind,
            *,
            capture_bytes: bool,
        ) -> tuple[Any, bytes | None]:
            asset = assets.get(asset_id)
            if asset is None or asset.kind != expected_kind:
                raise ValueError(
                    f"reference comparison requires a retained {expected_kind.value} asset"
                )
            try:
                path = self.resolve_asset_path(asset.relative_path)
            except FileNotFoundError as exc:
                raise ValueError("reference comparison source asset bytes are absent") from exc
            return asset, _read_verified_regular_file(
                path,
                expected_sha256=asset.sha256,
                expected_byte_size=asset.byte_size,
                capture_bytes=capture_bytes,
            )

        for scenario_id, comparison in comparison_by_scenario.items():
            scenario = scenarios.get(scenario_id)
            binding = bindings.get(scenario_id)
            if scenario is None or binding is None:
                raise ValueError("reference comparison scenario or binding is unavailable")
            if (
                comparison.analysis_id != result.analysis_id
                or comparison.revision_id != result.revision_id
                or comparison.model_sha256 != result.model_sha256
                or comparison.engine_manifest_sha256 != result.engine_manifest_sha256
                or comparison.binding_id != binding.binding_id
                or comparison.reference_matrix_asset_id != binding.reference_matrix_asset_id
                or comparison.tolerance != binding.tolerance
                or comparison.reference_current_asset_id != binding.reference_current_asset_id
                or comparison.current_raster_disposition != binding.current_raster_disposition
            ):
                raise ValueError("reference scenario comparison differs from its reviewed binding")

            focal_set = focal_sets.get(scenario.focal_set_id)
            if focal_set is None:
                raise ValueError("reference scenario focal-area set is unavailable")
            focal_pairs = {item.pair_id: item for item in focal_set.pairs}
            if set(scenario.included_pair_ids) - set(focal_pairs):
                raise ValueError("reference scenario includes an unavailable focal pair")
            prepared_pairs = [
                PreparedPair(
                    pair_id=pair_id,
                    from_focal_id=focal_pairs[pair_id].from_focal_id,
                    to_focal_id=focal_pairs[pair_id].to_focal_id,
                )
                for pair_id in scenario.included_pair_ids
            ]

            matrix_asset, reference_matrix_bytes = verify_asset_bytes(
                binding.reference_matrix_asset_id,
                AssetKind.REFERENCE_MATRIX,
                capture_bytes=True,
            )
            if reference_matrix_bytes is None:
                raise AssertionError("captured reference matrix bytes are unavailable")
            if comparison.reference_matrix_sha256 != matrix_asset.sha256:
                raise ValueError("reference matrix comparison hash differs from its source asset")
            parsed_reference = parse_reference_matrix_bytes(
                reference_matrix_bytes,
                prepared_pairs,
            )
            current_asset = None
            if comparison.reference_current_asset_id is not None:
                current_asset, _ = verify_asset_bytes(
                    comparison.reference_current_asset_id,
                    AssetKind.REFERENCE_CURRENT,
                    capture_bytes=False,
                )
                if comparison.reference_current_sha256 != current_asset.sha256:
                    raise ValueError(
                        "reference current diagnostic hash differs from its source asset"
                    )
            elif comparison.reference_current_sha256 is not None:
                raise ValueError("reference current hash has no reviewed source asset")

            verified_run = verified_direct_run(comparison.direct_run_id)
            run = verified_run.run
            if (
                run.analysis_id != result.analysis_id
                or run.revision_id != result.revision_id
                or run.portfolio_id != analysis_plan.baseline_portfolio_id
                or run.scenario_id != scenario_id
                or run.engine_manifest_sha256 != result.engine_manifest_sha256
                or not run.exact
                or run.test_only
                or run.engine_name != "Circuitscape"
                or run.qa_status not in {QAStatus.ACCEPTED, QAStatus.WARNING_ACCEPTED}
                or run.qa_status != comparison.qa_status
                or run.matrix_sha256 != comparison.observed_matrix_sha256
            ):
                raise ValueError(
                    "reference comparison differs from its exact stored successful direct run"
                )
            prepared = verified_run.prepared
            if (
                prepared.prepared_run_id != run.direct_run_id
                or prepared.analysis_id != run.analysis_id
                or prepared.revision_id != run.revision_id
                or prepared.portfolio_id != run.portfolio_id
                or prepared.scenario != scenario
                or prepared.pairs != prepared_pairs
                or prepared.engine_manifest_sha256 != run.engine_manifest_sha256
                or prepared.model_run_cache_key != run.model_run_cache_key
            ):
                raise ValueError(
                    "reference comparison prepared evidence differs from its direct run"
                )
            run_metrics = {item.pair_id: item.effective_resistance for item in run.pair_metrics}
            artifact_row = next(
                (item for item in verified_run.artifacts if item["role"] == "matrix"),
                None,
            )
            if artifact_row is None or artifact_row["sha256"] != run.matrix_sha256:
                raise ValueError("reference comparison direct run has no exact matrix artifact")
            observed_matrix_path = verified_run.artifact_paths_by_role["matrix"]
            observed_matrix_bytes = _read_verified_regular_file(
                observed_matrix_path,
                expected_sha256=run.matrix_sha256,
                expected_byte_size=int(artifact_row["byte_size"]),
                capture_bytes=True,
            )
            if observed_matrix_bytes is None:
                raise AssertionError("captured observed matrix bytes are unavailable")
            parsed_observed = parse_reference_matrix_bytes(
                observed_matrix_bytes,
                prepared_pairs,
            )
            comparison_pairs = {item.pair_id: item for item in comparison.pair_comparisons}
            expected_pair_ids = set(scenario.included_pair_ids)
            if (
                set(run_metrics) != expected_pair_ids
                or set(parsed_observed) != expected_pair_ids
                or set(parsed_reference) != expected_pair_ids
                or set(comparison_pairs) != expected_pair_ids
            ):
                raise ValueError(
                    "reference comparison does not cover the complete direct-run focal-pair set"
                )
            if run_metrics != parsed_observed:
                raise ValueError(
                    "stored direct metrics differ from the exact retained matrix bytes"
                )
            canonical_pairs: list[ReferencePairComparison] = []
            for pair_id in sorted(expected_pair_ids):
                reference_value = parsed_reference[pair_id]
                observed_value = parsed_observed[pair_id]
                absolute_difference = abs(observed_value - reference_value)
                relative_difference = absolute_difference / abs(reference_value)
                allowed_difference = binding.tolerance.absolute_tolerance + (
                    binding.tolerance.relative_tolerance * abs(reference_value)
                )
                canonical_pair = ReferencePairComparison(
                    pair_id=pair_id,
                    reference_effective_resistance=reference_value,
                    observed_effective_resistance=observed_value,
                    tolerance=binding.tolerance,
                    absolute_difference=absolute_difference,
                    relative_difference=relative_difference,
                    allowed_difference=allowed_difference,
                    within_tolerance=absolute_difference <= allowed_difference,
                )
                canonical_pairs.append(canonical_pair)
                pair_comparison_by_subject[(scenario_id, pair_id)] = canonical_pair
            canonical_comparison = ReferenceScenarioComparison(
                comparison_id=reference_scenario_comparison_id(
                    plan_id=reference_plan.plan_id,
                    binding_id=binding.binding_id,
                    scenario_id=scenario_id,
                    direct_run_id=run.direct_run_id,
                    result_sha256=run.result_sha256,
                    reference_matrix_sha256=matrix_asset.sha256,
                ),
                analysis_id=reference_plan.analysis_id,
                revision_id=reference_plan.revision_id,
                model_sha256=reference_plan.model_sha256,
                binding_id=binding.binding_id,
                scenario_id=scenario_id,
                direct_run_id=run.direct_run_id,
                reference_matrix_asset_id=matrix_asset.asset_id,
                reference_matrix_sha256=matrix_asset.sha256,
                observed_matrix_sha256=run.matrix_sha256,
                engine_manifest_sha256=run.engine_manifest_sha256,
                tolerance=binding.tolerance,
                pair_comparisons=canonical_pairs,
                qa_status=run.qa_status,
                status=(
                    "PASSED" if all(item.within_tolerance for item in canonical_pairs) else "FAILED"
                ),
                reference_current_asset_id=(
                    current_asset.asset_id if current_asset is not None else None
                ),
                reference_current_sha256=(
                    current_asset.sha256 if current_asset is not None else None
                ),
                current_raster_disposition=binding.current_raster_disposition,
                current_raster_numeric_comparison_performed=False,
                exact_direct_evidence_only=True,
                completed_at=run.completed_at,
                limitations=list(
                    dict.fromkeys(
                        [
                            *binding.limitations,
                            CURRENT_RASTER_LIMITATION,
                            FITNESS_LIMITATION,
                        ]
                    )
                ),
            )
            if canonical_json(comparison.model_dump(mode="json")) != canonical_json(
                canonical_comparison.model_dump(mode="json")
            ):
                raise ValueError(
                    "reference comparison is not the canonical result of retained matrix bytes"
                )
            canonical_comparisons[scenario_id] = canonical_comparison
            run_by_scenario[scenario_id] = run
            matrix_asset_by_scenario[scenario_id] = matrix_asset

        ordered_comparisons = sorted(
            canonical_comparisons.values(),
            key=lambda item: (item.scenario_id, item.binding_id, item.comparison_id),
        )
        canonical_links = reference_evidence_claim_links(ordered_comparisons)
        canonical_result = assemble_reference_comparison_result(
            result_id=reference_result_id(
                plan_id=reference_plan.plan_id,
                comparisons=ordered_comparisons,
                claim_links=canonical_links,
            ),
            plan=reference_plan,
            scenario_comparisons=ordered_comparisons,
            evidence_claim_links=canonical_links,
            completed_at=max(item.completed_at for item in ordered_comparisons),
            limitations=[
                limitation
                for comparison in ordered_comparisons
                for limitation in comparison.limitations
            ],
        )
        if canonical_json(result.model_dump(mode="json")) != canonical_json(
            canonical_result.model_dump(mode="json")
        ):
            raise ValueError(
                "reference result identity, time, links, limitations, ordering, or status differs "
                "from canonical direct evidence"
            )

        links_by_claim_id = {item.claim_id: item for item in canonical_links}
        if set(claims_by_id) != set(links_by_claim_id):
            raise ValueError(
                "reference comparison claims differ from its authoritative evidence links"
            )

        for claim_id, link in links_by_claim_id.items():
            claim = claims_by_id[claim_id]
            validate_evidence_claim(claim)
            canonical_claim_text = "Direct Circuitscape result under documented assumptions."
            comparison = canonical_comparisons.get(link.scenario_id)
            pair_comparison = pair_comparison_by_subject.get((link.scenario_id, link.pair_id))
            run = run_by_scenario.get(link.scenario_id)
            matrix_asset = matrix_asset_by_scenario.get(link.scenario_id)
            if comparison is None or pair_comparison is None or run is None or matrix_asset is None:
                raise ValueError("reference evidence link has no matching comparison evidence")
            if (
                link.comparison_id != comparison.comparison_id
                or link.direct_run_id != comparison.direct_run_id
                or claim.claim_kind != "DIRECT_REFERENCE_COMPARISON"
                or claim.text != canonical_claim_text
                or claim.root_node_id != claim.claim_id
                or claim.portfolio_id != analysis_plan.baseline_portfolio_id
                or claim.scenario_id != link.scenario_id
                or claim.pair_id != link.pair_id
                or claim.direct_run_ids != [link.direct_run_id]
                or type(claim.value) is not bool
                or claim.value != pair_comparison.within_tolerance
                or claim.units != "within_tolerance"
            ):
                raise ValueError(
                    "reference evidence claim subject or value differs from its result link"
                )
            canonical_claim_limitations = list(
                dict.fromkeys([CONNECTIVITY_LIMITATION, *comparison.limitations])
            )
            if claim.limitations != canonical_claim_limitations:
                raise ValueError(
                    "reference evidence claim limitations differ from its reviewed comparison"
                )
            scenario = scenarios[link.scenario_id]
            comparison_node_id = f"reference-comparison:{comparison.comparison_id}"
            metric_node_id = f"metric:{comparison.comparison_id}:{link.pair_id}"
            run_node_id = f"run:{run.direct_run_id}"
            scenario_node_id = f"scenario:{scenario.scenario_id}"
            asset_node_id = f"asset:{matrix_asset.asset_id}"
            engine_node_id = f"engine:{result.engine_manifest_sha256}"
            qa_node_id = f"qa:{run.direct_run_id}"
            artifact_node_id = (
                f"artifact:matrix:{run.direct_run_id}:{comparison.observed_matrix_sha256}"
            )
            authorization_node_id = f"authorization:{matrix_asset.authorization.asset_id}"
            expected_nodes = [
                EvidenceNode(
                    node_id=claim.claim_id,
                    node_type="CLAIM",
                    label=canonical_claim_text,
                ),
                EvidenceNode(
                    node_id=comparison_node_id,
                    node_type="REFERENCE_COMPARISON",
                    label="Reviewed effective-resistance matrix comparison",
                    sha256=canonical_sha256(comparison.model_dump(mode="json")),
                    attributes={
                        "comparison_id": comparison.comparison_id,
                        "scenario_id": scenario.scenario_id,
                        "pair_id": link.pair_id,
                        "direct_run_id": run.direct_run_id,
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
                        "reference_effective_resistance": (
                            pair_comparison.reference_effective_resistance
                        ),
                        "observed_effective_resistance": (
                            pair_comparison.observed_effective_resistance
                        ),
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
                    label=scenario.scenario_id,
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
                    sha256=result.engine_manifest_sha256,
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
                    node_id=artifact_node_id,
                    node_type="ARTIFACT",
                    label="Retained direct effective-resistance matrix",
                    sha256=comparison.observed_matrix_sha256,
                    attributes={"role": "matrix", "direct_run_id": run.direct_run_id},
                ),
                EvidenceNode(
                    node_id=authorization_node_id,
                    node_type="AUTHORIZATION",
                    label="Reviewed reference-data authorization",
                    sha256=canonical_sha256(matrix_asset.authorization.model_dump(mode="json")),
                    attributes={
                        "asset_id": matrix_asset.authorization.asset_id,
                        "cloud_disposition": (matrix_asset.authorization.cloud_disposition.value),
                        "export_allowed": matrix_asset.authorization.export_allowed,
                        "reviewer": matrix_asset.authorization.reviewer,
                    },
                ),
                *[
                    EvidenceNode(
                        node_id=_assumption_node_id(limitation),
                        node_type="ASSUMPTION",
                        label=limitation,
                    )
                    for limitation in canonical_claim_limitations
                ],
            ]
            expected_edges = [
                EvidenceEdge(
                    from_node_id=claim.claim_id,
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
                        to_node_id=_assumption_node_id(limitation),
                        relation="assumes",
                    )
                    for limitation in canonical_claim_limitations
                ],
            ]
            expected_nodes_by_id = {
                item.node_id: canonical_json(item.model_dump(mode="json"))
                for item in expected_nodes
            }
            actual_nodes_by_id = {
                item.node_id: canonical_json(item.model_dump(mode="json")) for item in claim.nodes
            }
            expected_edge_payloads = {
                canonical_json(item.model_dump(mode="json")) for item in expected_edges
            }
            actual_edge_payloads = [
                canonical_json(item.model_dump(mode="json")) for item in claim.edges
            ]
            if expected_nodes_by_id != actual_nodes_by_id:
                raise ValueError("reference claim nodes differ from canonical evidence semantics")
            if (
                len(actual_edge_payloads) != len(set(actual_edge_payloads))
                or set(actual_edge_payloads) != expected_edge_payloads
            ):
                raise ValueError("reference claim edges differ from canonical evidence topology")

        return str(analysis["project_id"]), claims_by_id

    def store_reference_comparison_result(
        self,
        result: ReferenceComparisonResult,
        *,
        claims: Sequence[ConnectivityEvidenceClaim],
    ) -> dict[str, Any]:
        """Atomically persist a direct reference result, its claims, and outbox event."""

        # Canonical model validation and descriptor-heavy evidence verification
        # happen before the write transaction. The commit compares a lightweight
        # identity seal over every immutable database input.
        result = ReferenceComparisonResult.model_validate(result.model_dump(mode="json"))
        claims = tuple(
            ConnectivityEvidenceClaim.model_validate(item.model_dump(mode="json"))
            for item in claims
        )
        result_json = canonical_json(result.model_dump(mode="json"))
        result_sha256 = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        claim_json_by_id = {
            item.claim_id: canonical_json(item.model_dump(mode="json")) for item in claims
        }
        if len(claim_json_by_id) != len(claims):
            raise ValueError("reference comparison contains duplicate evidence claim IDs")
        links_by_id = {item.claim_id: item for item in result.evidence_claim_links}
        reference_plan, validation_seal = self._preflight_reference_result(
            result=result,
            claims=claims,
        )
        plan_sha256 = hashlib.sha256(
            canonical_json(reference_plan.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        project_id = validation_seal.project_id
        claims_by_id = validation_seal.claims_by_id
        with self.transaction() as connection:
            committed_plan = self._load_reference_comparison_plan(
                connection,
                plan_id=result.plan_id,
                analysis_id=result.analysis_id,
            )
            completion = self._load_reference_completion_intent(
                connection,
                analysis_id=result.analysis_id,
            )
            if (
                committed_plan != reference_plan
                or completion is None
                or completion[1].get("plan_id") != result.plan_id
                or self._reference_persistence_sha256(
                    connection,
                    analysis_id=result.analysis_id,
                )
                != validation_seal.persistence_sha256
            ):
                raise ValueError("reference evidence changed before atomic result commit")
            existing = connection.execute(
                "SELECT * FROM eco_reference_comparison_results WHERE result_id=?",
                (result.result_id,),
            ).fetchone()
            conflicting_plan = connection.execute(
                "SELECT result_id FROM eco_reference_comparison_results "
                "WHERE analysis_id=? AND plan_id=? AND result_id!=?",
                (result.analysis_id, result.plan_id, result.result_id),
            ).fetchone()
            if conflicting_plan is not None:
                raise ValueError(
                    "reference comparison plan already has a different immutable result"
                )
            replayed = existing is not None
            if existing is not None:
                if (
                    existing["analysis_id"] != result.analysis_id
                    or existing["plan_id"] != result.plan_id
                    or existing["revision_id"] != result.revision_id
                    or existing["model_sha256"] != result.model_sha256
                    or existing["engine_manifest_sha256"] != result.engine_manifest_sha256
                    or existing["reference_matrix_parser_sha256"] != REFERENCE_MATRIX_PARSER_SHA256
                    or existing["status"] != result.status
                    or existing["payload_json"] != result_json
                    or existing["payload_sha256"] != result_sha256
                ):
                    raise ValueError(
                        "reference comparison result identity was reused for different evidence"
                    )
                persisted_claims = connection.execute(
                    "SELECT * FROM eco_reference_comparison_claims WHERE result_id=? "
                    "ORDER BY claim_id",
                    (result.result_id,),
                ).fetchall()
                if len(persisted_claims) != len(claims_by_id):
                    raise ValueError("stored reference comparison claim set is incomplete")
                for row in persisted_claims:
                    claim_id = str(row["claim_id"])
                    link = links_by_id.get(claim_id)
                    claim_json = claim_json_by_id.get(claim_id)
                    if (
                        link is None
                        or claim_json is None
                        or (
                            row["analysis_id"] != result.analysis_id
                            or row["comparison_id"] != link.comparison_id
                            or row["scenario_id"] != link.scenario_id
                            or row["pair_id"] != link.pair_id
                            or row["direct_run_id"] != link.direct_run_id
                            or row["payload_json"] != claim_json
                            or row["payload_sha256"]
                            != hashlib.sha256(claim_json.encode("utf-8")).hexdigest()
                        )
                    ):
                        raise ValueError(
                            "reference comparison replay differs from stored claim evidence"
                        )
            else:
                for claim_id in claims_by_id:
                    claimed_elsewhere = connection.execute(
                        "SELECT claim_id FROM eco_evidence_claims WHERE claim_id=? "
                        "UNION ALL SELECT claim_id FROM eco_reference_comparison_claims "
                        "WHERE claim_id=? LIMIT 1",
                        (claim_id, claim_id),
                    ).fetchone()
                    if claimed_elsewhere is not None:
                        raise ValueError("reference claim identity collides with stored evidence")
                now = _now()
                connection.execute(
                    "INSERT INTO eco_reference_comparison_results(result_id,analysis_id,plan_id,"
                    "revision_id,model_sha256,engine_manifest_sha256,"
                    "reference_matrix_parser_sha256,status,payload_json,payload_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        result.result_id,
                        result.analysis_id,
                        result.plan_id,
                        result.revision_id,
                        result.model_sha256,
                        result.engine_manifest_sha256,
                        REFERENCE_MATRIX_PARSER_SHA256,
                        result.status,
                        result_json,
                        result_sha256,
                        now,
                    ),
                )
                for claim_id in sorted(claims_by_id):
                    link = links_by_id[claim_id]
                    claim_json = claim_json_by_id[claim_id]
                    connection.execute(
                        "INSERT INTO eco_reference_comparison_claims(claim_id,result_id,"
                        "analysis_id,comparison_id,scenario_id,pair_id,direct_run_id,payload_json,"
                        "payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            claim_id,
                            result.result_id,
                            result.analysis_id,
                            link.comparison_id,
                            link.scenario_id,
                            link.pair_id,
                            link.direct_run_id,
                            claim_json,
                            hashlib.sha256(claim_json.encode("utf-8")).hexdigest(),
                            now,
                        ),
                    )
            outbox_id = self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=result.analysis_id,
                event_type="reference_comparison",
                subject_id=result.result_id,
                stream_id=f"reference_comparison:{result.analysis_id}",
                payload={
                    "schema_version": "ecocorridor/v1.0",
                    "result_id": result.result_id,
                    "plan_id": result.plan_id,
                    "analysis_id": result.analysis_id,
                    "revision_id": result.revision_id,
                    "model_sha256": result.model_sha256,
                    "engine_manifest_sha256": result.engine_manifest_sha256,
                    "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
                    "status": result.status,
                    "plan_payload_sha256": plan_sha256,
                    "result_payload_sha256": result_sha256,
                    "comparison_ids": sorted(
                        item.comparison_id for item in result.scenario_comparisons
                    ),
                    "claim_ids": sorted(claims_by_id),
                    "direct_run_ids": sorted(
                        item.direct_run_id for item in result.scenario_comparisons
                    ),
                },
                idempotency_key=f"reference_comparison:{result.result_id}",
            )
        return {
            "result_id": result.result_id,
            "plan_sha256": plan_sha256,
            "result_sha256": result_sha256,
            "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
            "outbox_id": outbox_id,
            "replayed": replayed,
        }

    def reference_comparison_result(
        self,
        result_id: str,
    ) -> tuple[ReferenceComparisonResult, list[ConnectivityEvidenceClaim]]:
        """Read and fully revalidate immutable reference-comparison evidence."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM eco_reference_comparison_results WHERE result_id=?",
                (result_id,),
            ).fetchone()
            if row is None:
                raise KeyError(result_id)
            payload_json = str(row["payload_json"])
            if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != row["payload_sha256"]:
                raise ValueError("reference comparison result failed its immutable checksum")
            result = ReferenceComparisonResult.model_validate_json(payload_json)
            if (
                row["analysis_id"] != result.analysis_id
                or row["plan_id"] != result.plan_id
                or row["revision_id"] != result.revision_id
                or row["model_sha256"] != result.model_sha256
                or row["engine_manifest_sha256"] != result.engine_manifest_sha256
                or row["reference_matrix_parser_sha256"] != REFERENCE_MATRIX_PARSER_SHA256
                or row["status"] != result.status
            ):
                raise ValueError("reference comparison row differs from its payload")
            claim_rows = self._connection.execute(
                "SELECT * FROM eco_reference_comparison_claims WHERE result_id=? ORDER BY claim_id",
                (result_id,),
            ).fetchall()
            claims: list[ConnectivityEvidenceClaim] = []
            links_by_id = {item.claim_id: item for item in result.evidence_claim_links}
            if len(claim_rows) != len(links_by_id):
                raise ValueError("stored reference comparison claim set is incomplete")
            for claim_row in claim_rows:
                claim_json = str(claim_row["payload_json"])
                if (
                    hashlib.sha256(claim_json.encode("utf-8")).hexdigest()
                    != claim_row["payload_sha256"]
                ):
                    raise ValueError("reference comparison claim failed its immutable checksum")
                claim = ConnectivityEvidenceClaim.model_validate_json(claim_json)
                link = links_by_id.get(claim.claim_id)
                if link is None or (
                    claim_row["analysis_id"] != result.analysis_id
                    or claim_row["comparison_id"] != link.comparison_id
                    or claim_row["scenario_id"] != link.scenario_id
                    or claim_row["pair_id"] != link.pair_id
                    or claim_row["direct_run_id"] != link.direct_run_id
                ):
                    raise ValueError("reference comparison claim row differs from its link")
                claims.append(claim)
            self._validate_reference_comparison_evidence(
                self._connection,
                reference_plan=self._load_reference_comparison_plan(
                    self._connection,
                    plan_id=result.plan_id,
                    analysis_id=result.analysis_id,
                ),
                result=result,
                claims=claims,
            )
            return result, claims

    def reference_comparison_results(
        self,
        *,
        analysis_id: str | None = None,
        revision_id: str | None = None,
        model_sha256: str | None = None,
        engine_manifest_sha256: str | None = None,
        exclude_result_ids: Sequence[str] = (),
    ) -> list[tuple[ReferenceComparisonResult, list[ConnectivityEvidenceClaim]]]:
        """List immutable reference results through the full validated read path."""

        clauses: list[str] = []
        parameters: list[str] = []
        for column, value in (
            ("analysis_id", analysis_id),
            ("revision_id", revision_id),
            ("model_sha256", model_sha256),
            ("engine_manifest_sha256", engine_manifest_sha256),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        excluded = sorted(set(exclude_result_ids))
        if excluded:
            clauses.append(f"result_id NOT IN ({','.join('?' for _ in excluded)})")
            parameters.extend(excluded)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT result_id FROM eco_reference_comparison_results{where} ORDER BY result_id",
                parameters,
            ).fetchall()
            return [self.reference_comparison_result(str(row["result_id"])) for row in rows]

    def store_results(
        self,
        analysis_id: str,
        *,
        runs: Sequence[DirectConnectivityRun],
        summaries: Sequence[PortfolioDecisionSummary],
        claims: Sequence[ConnectivityEvidenceClaim],
    ) -> None:
        """Reject the pre-durable bulk result path.

        Successful science must be persisted through ``finalize_direct_job``
        before ``complete_analysis``.  Keeping this explicit failure preserves
        a clear upgrade error for callers without retaining a second path that
        could create evidence without terminal direct jobs.
        """

        del analysis_id, runs, summaries, claims
        raise RuntimeError(
            "store_results is unsupported; finalize every durable direct job and then call "
            "complete_analysis"
        )

    def complete_analysis(
        self,
        analysis_id: str,
        *,
        summaries: Sequence[PortfolioDecisionSummary],
        claims: Sequence[ConnectivityEvidenceClaim],
    ) -> None:
        """Finalize an analysis whose successful runs were persisted job-by-job."""

        if not summaries:
            raise ValueError("completed analysis requires portfolio summaries")
        with self.transaction() as connection:
            analysis = connection.execute(
                "SELECT project_id,analysis_kind,state,plan_json FROM eco_analyses "
                "WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
            if analysis is None or analysis["state"] != AnalysisState.RUNNING.value:
                raise ValueError("analysis completion requires a running authoritative analysis")
            if analysis["analysis_kind"] != "ALTERNATIVES":
                raise ValueError("reference analyses require reference completion semantics")
            plan = ConnectivityRunPlan.model_validate(json.loads(analysis["plan_json"]))
            rows = connection.execute(
                "SELECT direct_run_id,portfolio_id,scenario_id,payload_json "
                "FROM eco_analysis_runs WHERE analysis_id=? ORDER BY direct_run_id",
                (analysis_id,),
            ).fetchall()
            runs = [
                DirectConnectivityRun.model_validate(json.loads(row["payload_json"]))
                for row in rows
            ]
            expected = {
                (portfolio.portfolio_id, scenario_id)
                for portfolio in plan.portfolios
                for scenario_id in plan.scenario_ids
            }
            observed = {(run.portfolio_id, run.scenario_id) for run in runs}
            if observed != expected or len(observed) != len(runs):
                raise ValueError("analysis completion requires every planned direct scenario job")
            job_states = connection.execute(
                "SELECT state,COUNT(*) AS count FROM eco_direct_jobs WHERE analysis_id=? "
                "GROUP BY state",
                (analysis_id,),
            ).fetchall()
            if {str(row["state"]) for row in job_states} != {DirectJobState.SUCCEEDED.value}:
                raise ValueError("analysis completion requires all durable direct jobs to succeed")
            portfolio_ids = {item.portfolio_id for item in plan.portfolios}
            if {item.portfolio_id for item in summaries} != portfolio_ids:
                raise ValueError("analysis completion requires one summary per portfolio")
            run_by_id = {item.direct_run_id: item for item in runs}
            for summary in summaries:
                expected_run_ids = {
                    run.direct_run_id for run in runs if run.portfolio_id == summary.portfolio_id
                }
                if set(summary.direct_run_ids) != expected_run_ids:
                    raise ValueError("portfolio summary omits part of its direct scenario ensemble")
            self._validate_claim_run_references(plan=plan, runs=runs, claims=claims)
            validate_portfolio_disposition_support(
                summaries=summaries,
                runs=runs,
                claims=claims,
            )
            for summary in summaries:
                connection.execute(
                    "INSERT INTO eco_analysis_summaries(analysis_id,portfolio_id,payload_json) "
                    "VALUES(?,?,?)",
                    (
                        analysis_id,
                        summary.portfolio_id,
                        canonical_json(summary.model_dump(mode="json")),
                    ),
                )
            for claim in claims:
                connection.execute(
                    "INSERT INTO eco_evidence_claims(claim_id,analysis_id,payload_json) "
                    "VALUES(?,?,?)",
                    (claim.claim_id, analysis_id, canonical_json(claim.model_dump(mode="json"))),
                )
            connection.execute(
                "UPDATE eco_analyses SET state='COMPLETED',updated_at=? WHERE analysis_id=?",
                (_now(), analysis_id),
            )
            self._enqueue_outbox(
                connection,
                project_id=str(analysis["project_id"]),
                analysis_id=analysis_id,
                event_type="analysis_completion",
                subject_id=analysis_id,
                stream_id=f"connectivity_analysis:{analysis_id}",
                payload={
                    "analysis_id": analysis_id,
                    "direct_run_ids": sorted(run_by_id),
                    "portfolio_ids": sorted(portfolio_ids),
                    "evidence_claim_ids": sorted(item.claim_id for item in claims),
                },
                idempotency_key=f"analysis_completion:{analysis_id}",
            )

    def analysis_results(
        self, analysis_id: str
    ) -> tuple[
        list[DirectConnectivityRun], list[PortfolioDecisionSummary], list[ConnectivityEvidenceClaim]
    ]:
        self.analysis(analysis_id)
        runs = [
            DirectConnectivityRun.model_validate(json.loads(row["payload_json"]))
            for row in self._connection.execute(
                "SELECT payload_json FROM eco_analysis_runs WHERE analysis_id=? ORDER BY direct_run_id",
                (analysis_id,),
            )
        ]
        summaries = [
            PortfolioDecisionSummary.model_validate(json.loads(row["payload_json"]))
            for row in self._connection.execute(
                "SELECT payload_json FROM eco_analysis_summaries WHERE analysis_id=? ORDER BY portfolio_id",
                (analysis_id,),
            )
        ]
        claims = [
            ConnectivityEvidenceClaim.model_validate(json.loads(row["payload_json"]))
            for row in self._connection.execute(
                "SELECT payload_json FROM eco_evidence_claims WHERE analysis_id=? ORDER BY claim_id",
                (analysis_id,),
            )
        ]
        return runs, summaries, claims

    def _evidence_claim_owner(self, claim_id: str) -> tuple[str, sqlite3.Row]:
        ordinary = self._connection.execute(
            "SELECT analysis_id,payload_json,NULL AS payload_sha256,NULL AS result_id "
            "FROM eco_evidence_claims WHERE claim_id=?",
            (claim_id,),
        ).fetchall()
        reference = self._connection.execute(
            "SELECT analysis_id,payload_json,payload_sha256,result_id "
            "FROM eco_reference_comparison_claims WHERE claim_id=?",
            (claim_id,),
        ).fetchall()
        owners = [("ORDINARY", row) for row in ordinary] + [("REFERENCE", row) for row in reference]
        if not owners:
            raise KeyError(claim_id)
        if len(owners) != 1:
            raise ValueError("evidence claim identity is ambiguous across authoritative stores")
        return owners[0]

    @staticmethod
    def _validated_claim_row(row: sqlite3.Row, *, reference: bool) -> ConnectivityEvidenceClaim:
        payload_json = str(row["payload_json"])
        if reference:
            payload_sha256 = str(row["payload_sha256"] or "")
            if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha256:
                raise ValueError("reference evidence claim failed its immutable checksum")
        claim = ConnectivityEvidenceClaim.model_validate_json(payload_json)
        if canonical_json(claim.model_dump(mode="json")) != payload_json:
            raise ValueError("evidence claim payload is not canonical immutable evidence")
        return claim

    def evidence_claim(self, claim_id: str) -> ConnectivityEvidenceClaim:
        with self._lock:
            owner, row = self._evidence_claim_owner(claim_id)
            claim = self._validated_claim_row(row, reference=owner == "REFERENCE")
            if owner == "REFERENCE":
                result, claims = self.reference_comparison_result(str(row["result_id"]))
                del result
                authoritative = {item.claim_id: item for item in claims}
                if authoritative.get(claim_id) != claim:
                    raise ValueError(
                        "reference evidence claim differs from its authoritative comparison result"
                    )
            return claim

    def evidence_claim_analysis_id(self, claim_id: str) -> str:
        """Return the authoritative analysis that owns a persisted evidence claim."""

        with self._lock:
            owner, row = self._evidence_claim_owner(claim_id)
            claim = self._validated_claim_row(row, reference=owner == "REFERENCE")
            if claim.claim_id != claim_id:
                raise ValueError("evidence claim row has a different immutable identity")
            if owner == "REFERENCE":
                self.reference_comparison_result(str(row["result_id"]))
            return str(row["analysis_id"])

    def _resolve_reference_evidence_claim(
        self,
        claim_id: str,
        claim_row: sqlite3.Row,
    ) -> dict[str, Any]:
        """Build one renderer-safe, fully revalidated reference Evidence Rail."""

        result, claims = self.reference_comparison_result(str(claim_row["result_id"]))
        claims_by_id = {item.claim_id: item for item in claims}
        claim = claims_by_id.get(claim_id)
        if claim is None or claim != self._validated_claim_row(claim_row, reference=True):
            raise ValueError("reference claim is detached from its comparison result")
        link = next(
            (item for item in result.evidence_claim_links if item.claim_id == claim_id),
            None,
        )
        if link is None:
            raise ValueError("reference claim has no authoritative comparison link")
        comparison = next(
            (
                item
                for item in result.scenario_comparisons
                if item.comparison_id == link.comparison_id
                and item.scenario_id == link.scenario_id
                and item.direct_run_id == link.direct_run_id
            ),
            None,
        )
        if comparison is None:
            raise ValueError("reference claim comparison link is detached from its result")
        pair_comparison = next(
            (item for item in comparison.pair_comparisons if item.pair_id == link.pair_id),
            None,
        )
        if pair_comparison is None or (
            type(claim.value) is not bool
            or claim.value != pair_comparison.within_tolerance
            or claim.units != "within_tolerance"
        ):
            raise ValueError("reference claim value differs from its exact pair comparison")

        reference_plan, reference_plan_sha256 = self.reference_comparison_plan(
            result.plan_id,
            analysis_id=result.analysis_id,
        )
        result_payload_sha256 = hashlib.sha256(
            canonical_json(result.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        verified = self.verify_authoritative_direct_run(
            link.direct_run_id,
            expected_analysis_id=result.analysis_id,
        )
        run = verified.run
        bundle = self.analysis_bundle(result.analysis_id)
        scenarios = {item.scenario_id: item for item in bundle.scenarios}
        scenario = scenarios.get(link.scenario_id)
        if scenario is None or verified.prepared.scenario != scenario:
            raise ValueError("reference claim scenario differs from its immutable model snapshot")
        model_row = self._connection.execute(
            "SELECT bundle_sha256 FROM eco_analysis_models WHERE analysis_id=?",
            (result.analysis_id,),
        ).fetchone()
        if model_row is None:
            raise ValueError("reference claim has no immutable model snapshot")

        artifacts = [
            {
                "artifact_id": f"artifact:{run.direct_run_id}:{item['role']}",
                "direct_run_id": run.direct_run_id,
                "role": str(item["role"]),
                "sha256": str(item["sha256"]),
                "byte_size": int(item["byte_size"]),
                "media_type": str(item["media_type"]),
            }
            for item in verified.artifacts
        ]
        prepared_run = {
            "prepared_run_id": verified.prepared.prepared_run_id,
            "direct_run_id": run.direct_run_id,
            "portfolio_id": run.portfolio_id,
            "scenario_id": run.scenario_id,
            "phase": "MATRIX",
            "action_raster_sha256": verified.prepared.action_raster_sha256,
            "footprint_proof_sha256": verified.prepared.footprint_proof_sha256,
            "engine_manifest_sha256": verified.prepared.engine_manifest_sha256,
            "model_run_cache_key": verified.prepared.model_run_cache_key,
            "emit_current_map": verified.prepared.emit_current_map,
            "cached": verified.cached,
            "resources": [
                {
                    "logical_name": item.logical_name,
                    "sha256": item.sha256,
                    "target_filename": item.target_filename,
                }
                for item in verified.prepared.resources
            ],
        }

        asset_ids = {
            comparison.reference_matrix_asset_id,
            *(
                [comparison.reference_current_asset_id]
                if comparison.reference_current_asset_id is not None
                else []
            ),
        }
        for node in (*verified.evidence_nodes, *claim.model_dump(mode="json")["nodes"]):
            if node.get("node_type") != "ASSET":
                continue
            node_id = str(node.get("node_id", ""))
            asset_id = node_id.removeprefix("asset:")
            if asset_id and asset_id != node_id:
                asset_ids.add(asset_id)
            attributed_id = node.get("attributes", {}).get("asset_id")
            if isinstance(attributed_id, str) and attributed_id:
                asset_ids.add(attributed_id)
        resource_hashes = {item.sha256 for item in verified.prepared.resources}
        asset_ids.update(item.asset_id for item in bundle.assets if item.sha256 in resource_hashes)
        assets_by_id = {item.asset_id: item for item in bundle.assets}
        source_assets: list[dict[str, Any]] = []
        for asset_id in sorted(asset_ids):
            asset = assets_by_id.get(asset_id)
            if asset is None:
                raise ValueError("reference evidence graph names an unavailable source asset")
            retained = self.resolve_asset_path(asset.relative_path)
            _read_verified_regular_file(
                retained,
                expected_sha256=asset.sha256,
                expected_byte_size=asset.byte_size,
                capture_bytes=False,
            )
            authorization = asset.authorization
            source_assets.append(
                {
                    "asset_id": asset.asset_id,
                    "kind": asset.kind.value,
                    "title": asset.title,
                    "sha256": asset.sha256,
                    "byte_size": asset.byte_size,
                    "media_type": asset.media_type,
                    "vintage": asset.vintage,
                    "license": asset.license,
                    "authorization": {
                        "asset_id": authorization.asset_id,
                        "ownership": authorization.ownership,
                        "license_spdx_or_name": authorization.license_spdx_or_name,
                        "sensitivity": authorization.sensitivity.value,
                        "cloud_disposition": authorization.cloud_disposition.value,
                        "export_allowed": authorization.export_allowed,
                        "retention_policy": authorization.retention_policy,
                        "spatial_redaction": authorization.spatial_redaction,
                        "approved_deployments": authorization.approved_deployments,
                        "reviewer": authorization.reviewer,
                        "reviewed_at": authorization.reviewed_at.isoformat(),
                        "generalized_asset_id": authorization.generalized_asset_id,
                    },
                }
            )

        cloud_references: dict[str, Any] | None = None
        for row in reversed(
            self.telemetry_rows(
                event_type="reference_comparison",
                analysis_id=result.analysis_id,
                subject_id=result.result_id,
            )
        ):
            reference = row.get("external_reference")
            if row.get("state") != "SYNCED" or not isinstance(reference, dict):
                continue
            try:
                receipt = CloudUploadReceipt.model_validate(reference)
            except (TypeError, ValueError):
                continue
            if receipt.envelope_id != row["outbox_id"]:
                continue
            cloud_references = {
                "state": "SYNCED",
                "analysis_run_id": receipt.run_id,
                "artifacts": [
                    {
                        "local_artifact_id": item.local_artifact_id,
                        "provider_artifact_id": item.provider_artifact_id,
                        "provider_artifact_ref": item.provider_artifact_ref,
                        "sha256": item.sha256,
                    }
                    for item in receipt.artifacts
                ],
            }
            break

        projection = {
            "claim": claim.model_dump(mode="json"),
            "analysis_id": result.analysis_id,
            "analysis_plan_sha256": canonical_sha256(
                reference_plan.run_plan.model_dump(mode="json")
            ),
            "model_revision": bundle.revision.model_dump(mode="json"),
            "model_bundle_sha256": str(model_row["bundle_sha256"]),
            "reference_plan": reference_plan.model_dump(mode="json"),
            "reference_plan_sha256": reference_plan_sha256,
            "reference_result": result.model_dump(mode="json"),
            "reference_result_sha256": result_payload_sha256,
            "reference_comparison": comparison.model_dump(mode="json"),
            "reference_pair_comparison": pair_comparison.model_dump(mode="json"),
            "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
            "reference_claim_link": link.model_dump(mode="json"),
            "direct_runs": [{**run.model_dump(mode="json"), "role": "REFERENCE_BASELINE"}],
            "baseline_runs": [{**run.model_dump(mode="json"), "role": "REFERENCE_BASELINE"}],
            "portfolio_runs": [],
            "prepared_runs": [prepared_run],
            "artifact_records": artifacts,
            "map_descriptors": [],
            "source_assets": source_assets,
            "scenarios": [scenario.model_dump(mode="json")],
            "engine_manifest_sha256": result.engine_manifest_sha256,
            "qa_records": [
                {
                    "direct_run_id": run.direct_run_id,
                    "status": verified.qa_status.value,
                    "findings": list(verified.qa_findings),
                }
            ],
            "assumptions": [node.label for node in claim.nodes if node.node_type == "ASSUMPTION"],
            "cloud_references": cloud_references,
            "limitations": claim.limitations,
        }
        _assert_renderer_safe(projection)
        return projection

    def resolve_evidence_claim(self, claim_id: str) -> dict[str, Any]:
        """Resolve a claim against retained authoritative rows and verified bytes.

        The response is deliberately safe for the renderer: project-relative CAS
        paths and prepared-resource source paths never cross the RPC boundary.
        Every retained artifact and source asset used by the claim is rehashed so
        the Evidence Rail cannot present a graph detached from the local record.
        """

        owner, row = self._evidence_claim_owner(claim_id)
        if owner == "REFERENCE":
            return self._resolve_reference_evidence_claim(claim_id, row)
        analysis_id = str(row["analysis_id"])
        claim = self._validated_claim_row(row, reference=False)
        validate_evidence_claim(claim)
        verified_by_id = {
            direct_run_id: self.verify_authoritative_direct_run(
                direct_run_id,
                expected_analysis_id=analysis_id,
            )
            for direct_run_id in claim.direct_run_ids
        }
        run_by_id = {key: item.run for key, item in verified_by_id.items()}

        analysis = self.analysis(analysis_id)
        plan = ConnectivityRunPlan.model_validate(analysis["plan"])
        bundle = self.analysis_bundle(analysis_id)
        if claim.portfolio_id not in {item.portfolio_id for item in plan.portfolios}:
            raise ValueError("evidence claim portfolio is absent from its immutable plan")

        artifacts: list[dict[str, Any]] = []
        artifact_paths: dict[tuple[str, str], Path] = {}
        map_descriptors: list[dict[str, Any]] = []
        for direct_run_id in claim.direct_run_ids:
            verified = verified_by_id[direct_run_id]
            for artifact in verified.artifacts:
                path = verified.artifact_paths_by_role[str(artifact["role"])]
                public_record = {
                    "artifact_id": f"artifact:{direct_run_id}:{artifact['role']}",
                    "direct_run_id": direct_run_id,
                    "role": str(artifact["role"]),
                    "sha256": str(artifact["sha256"]),
                    "byte_size": int(artifact["byte_size"]),
                    "media_type": str(artifact["media_type"]),
                }
                artifacts.append(public_record)
                artifact_paths[(direct_run_id, str(artifact["role"]))] = path
                if artifact["role"] == "current_map":
                    map_descriptors.append(
                        {
                            "descriptor_id": f"map:{direct_run_id}",
                            "label": "Retained diagnostic current-density raster",
                            "artifact_id": public_record["artifact_id"],
                            "direct_run_id": direct_run_id,
                            "sha256": public_record["sha256"],
                            "interpretation": (
                                "Diagnostic current redistribution; not observed or predicted "
                                "animal use."
                            ),
                        }
                    )

        asset_ids = {
            node.node_id.removeprefix("asset:")
            for node in claim.nodes
            if node.node_type == "ASSET" and node.node_id.startswith("asset:")
        }
        for verified in verified_by_id.values():
            for node in verified.evidence_nodes:
                if node.get("node_type") == "ASSET" and str(node.get("node_id", "")).startswith(
                    "asset:"
                ):
                    asset_ids.add(str(node["node_id"]).removeprefix("asset:"))
        resource_hashes = {
            resource.sha256
            for verified in verified_by_id.values()
            for resource in verified.prepared.resources
        }
        asset_ids.update(item.asset_id for item in bundle.assets if item.sha256 in resource_hashes)
        assets_by_id = {item.asset_id: item for item in bundle.assets}
        source_assets: list[dict[str, Any]] = []
        source_asset_paths: dict[str, Path] = {}
        for asset_id in sorted(asset_ids):
            asset = assets_by_id.get(asset_id)
            if asset is None:
                raise ValueError("evidence graph references an asset outside its model snapshot")
            retained = self.resolve_asset_path(asset.relative_path)
            _read_verified_regular_file(
                retained,
                expected_sha256=asset.sha256,
                expected_byte_size=asset.byte_size,
                capture_bytes=False,
            )
            authorization = asset.authorization
            source_assets.append(
                {
                    "asset_id": asset.asset_id,
                    "kind": asset.kind.value,
                    "title": asset.title,
                    "sha256": asset.sha256,
                    "byte_size": asset.byte_size,
                    "media_type": asset.media_type,
                    "vintage": asset.vintage,
                    "license": asset.license,
                    "authorization": {
                        "asset_id": authorization.asset_id,
                        "ownership": authorization.ownership,
                        "license_spdx_or_name": authorization.license_spdx_or_name,
                        "sensitivity": authorization.sensitivity.value,
                        "cloud_disposition": authorization.cloud_disposition.value,
                        "export_allowed": authorization.export_allowed,
                        "retention_policy": authorization.retention_policy,
                        "spatial_redaction": authorization.spatial_redaction,
                        "approved_deployments": authorization.approved_deployments,
                        "reviewer": authorization.reviewer,
                        "reviewed_at": authorization.reviewed_at.isoformat(),
                        "generalized_asset_id": authorization.generalized_asset_id,
                    },
                }
            )
            source_asset_paths[asset_id] = retained

        prepared_runs: list[dict[str, Any]] = []
        for direct_run_id in claim.direct_run_ids:
            verified = verified_by_id[direct_run_id]
            prepared = verified.prepared
            prepared_runs.append(
                {
                    "prepared_run_id": prepared.prepared_run_id,
                    "direct_run_id": direct_run_id,
                    "portfolio_id": prepared.portfolio_id,
                    "scenario_id": prepared.scenario.scenario_id,
                    "phase": verified.phase,
                    "action_raster_sha256": prepared.action_raster_sha256,
                    "footprint_proof_sha256": prepared.footprint_proof_sha256,
                    "engine_manifest_sha256": prepared.engine_manifest_sha256,
                    "model_run_cache_key": prepared.model_run_cache_key,
                    "emit_current_map": prepared.emit_current_map,
                    "cached": verified.cached,
                    "resources": [
                        {
                            "logical_name": resource.logical_name,
                            "sha256": resource.sha256,
                            "target_filename": resource.target_filename,
                        }
                        for resource in prepared.resources
                    ],
                }
            )

        prepared_by_run_id = {item["direct_run_id"]: item for item in prepared_runs}
        if claim.claim_kind in {"DIRECT_PAIR_METRIC", "DIRECT_MAP_DIAGNOSTIC"}:
            subject_run_ids = [
                direct_run_id
                for direct_run_id in claim.direct_run_ids
                if run_by_id[direct_run_id].portfolio_id == claim.portfolio_id
                and run_by_id[direct_run_id].scenario_id == claim.scenario_id
            ]
            if len(subject_run_ids) != 1 or subject_run_ids[0] not in prepared_by_run_id:
                raise ValueError("evidence claim lacks one immutable prepared subject run")
            expected_diff_sha256 = str(
                prepared_by_run_id[subject_run_ids[0]]["action_raster_sha256"]
            )
            raster_diff_nodes = [node for node in claim.nodes if node.node_type == "RASTER_DIFF"]
            if (
                len(raster_diff_nodes) != 1
                or raster_diff_nodes[0].node_id != f"diff:{expected_diff_sha256}"
                or raster_diff_nodes[0].sha256 != expected_diff_sha256
            ):
                raise ValueError(
                    "evidence claim raster diff differs from its immutable prepared subject run"
                )
            verify_direct_claim_against_retained_evidence(
                claim=claim,
                bundle=bundle,
                baseline_portfolio_id=plan.baseline_portfolio_id,
                runs_by_id=run_by_id,
                artifact_paths=artifact_paths,
                asset_paths=source_asset_paths,
            )
        elif claim.claim_kind == "COST":
            if bundle.cost_basis is None:
                raise ValueError("cost evidence has no reviewed CostBasis")
            basis_nodes = [node for node in claim.nodes if node.node_type == "COST_BASIS"]
            portfolio = next(
                item for item in plan.portfolios if item.portfolio_id == claim.portfolio_id
            )
            expected_cost = sum(
                bundle.cost_basis.option_costs[option_id]
                for option_id in portfolio.canonical_option_ids
            )
            if (
                len(basis_nodes) != 1
                or basis_nodes[0].sha256 != bundle.cost_basis.sha256
                or not isinstance(claim.value, (int, float))
                or float(claim.value) != expected_cost
                or claim.units != bundle.cost_basis.currency
            ):
                raise ValueError("cost evidence differs from the reviewed CostBasis")
        elif claim.claim_kind == "PORTFOLIO_DISPOSITION":
            summary_row = self._connection.execute(
                "SELECT payload_json FROM eco_analysis_summaries "
                "WHERE analysis_id=? AND portfolio_id=?",
                (analysis_id, claim.portfolio_id),
            ).fetchone()
            if summary_row is None:
                raise ValueError("portfolio disposition has no authoritative summary")
            summary = PortfolioDecisionSummary.model_validate(
                json.loads(summary_row["payload_json"])
            )
            if (
                summary.disposition.value != claim.value
                or summary.disposition_evidence_claim_id != claim.claim_id
            ):
                raise ValueError("portfolio disposition claim differs from its summary")

        direct_runs: list[dict[str, Any]] = []
        for direct_run_id in claim.direct_run_ids:
            run = run_by_id[direct_run_id]
            role = (
                "WITHIN_SCENARIO_BASELINE"
                if run.portfolio_id == plan.baseline_portfolio_id
                else "PORTFOLIO_SUBJECT"
            )
            direct_runs.append({**run.model_dump(mode="json"), "role": role})
        engine_hashes = {run.engine_manifest_sha256 for run in run_by_id.values()}
        if claim.direct_run_ids and len(engine_hashes) != 1:
            raise ValueError("evidence claim mixes incompatible engine manifests")

        scenario_ids = {run.scenario_id for run in run_by_id.values()}
        scenarios = [
            item.model_dump(mode="json")
            for item in bundle.scenarios
            if item.scenario_id in scenario_ids
        ]
        if {item["scenario_id"] for item in scenarios} != scenario_ids:
            raise ValueError("evidence claim scenario is absent from its model snapshot")
        model_row = self._connection.execute(
            "SELECT bundle_sha256 FROM eco_analysis_models WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        if model_row is None:
            raise ValueError("evidence claim has no immutable model snapshot")

        cloud_references: dict[str, Any] | None = None
        completion_rows = self.telemetry_rows(
            event_type="analysis_completion", analysis_id=analysis_id
        )
        for completion in reversed(completion_rows):
            reference = completion.get("external_reference")
            if completion.get("state") != "SYNCED" or not isinstance(reference, dict):
                continue
            try:
                receipt = CloudUploadReceipt.model_validate(reference)
            except (TypeError, ValueError):
                continue
            if receipt.envelope_id != completion["outbox_id"]:
                continue
            cloud_references = {
                "state": "SYNCED",
                "analysis_run_id": receipt.run_id,
                "artifacts": [
                    {
                        "local_artifact_id": item.local_artifact_id,
                        "provider_artifact_id": item.provider_artifact_id,
                        "provider_artifact_ref": item.provider_artifact_ref,
                        "sha256": item.sha256,
                    }
                    for item in receipt.artifacts
                ],
            }
            break

        projection = {
            "claim": claim.model_dump(mode="json"),
            "analysis_id": analysis_id,
            "analysis_plan_sha256": hashlib.sha256(
                canonical_json(plan.model_dump(mode="json")).encode("utf-8")
            ).hexdigest(),
            "model_revision": bundle.revision.model_dump(mode="json"),
            "model_bundle_sha256": str(model_row["bundle_sha256"]),
            "direct_runs": direct_runs,
            "baseline_runs": [
                item for item in direct_runs if item["role"] == "WITHIN_SCENARIO_BASELINE"
            ],
            "portfolio_runs": [item for item in direct_runs if item["role"] == "PORTFOLIO_SUBJECT"],
            "prepared_runs": prepared_runs,
            "artifact_records": sorted(
                artifacts, key=lambda item: (item["direct_run_id"], item["role"])
            ),
            "map_descriptors": sorted(map_descriptors, key=lambda item: item["direct_run_id"]),
            "source_assets": source_assets,
            "scenarios": scenarios,
            "engine_manifest_sha256": next(iter(engine_hashes), None),
            "qa_records": [
                {
                    "direct_run_id": run.direct_run_id,
                    "status": run.qa_status.value if run.qa_status else None,
                    "findings": run.qa_findings,
                }
                for run in run_by_id.values()
            ],
            "assumptions": [node.label for node in claim.nodes if node.node_type == "ASSUMPTION"],
            "cloud_references": cloud_references,
            "limitations": claim.limitations,
        }
        _assert_renderer_safe(projection)
        return projection

    def set_observability(self, payload: dict[str, Any]) -> None:
        project_id = self.project()["project_id"]
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO eco_observability(project_id,payload_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET payload_json=excluded.payload_json,"
                "updated_at=excluded.updated_at",
                (project_id, canonical_json(payload), _now()),
            )

    def record_oversize_sync_approval(self, approval: OversizeSyncApproval) -> int:
        """Persist consent and its cloud-lineage event in one transaction."""

        project_id = self.project()["project_id"]
        if approval.project_id != project_id:
            raise ValueError("oversize approval belongs to another project")
        now = _now()
        with self.transaction() as connection:
            active = self._active_revision_in_transaction(connection, project_id)
            if (
                approval.model_revision_id != active.revision_id
                or approval.model_sha256 != active.model_sha256
            ):
                raise ValueError("oversize approval does not target the active model revision")
            revision, _ = self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type="oversize_sync_approval",
                record_id=approval.approval_id,
                payload_json=canonical_json(approval.model_dump(mode="json")),
                allow_update=False,
                now=now,
            )
            self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=None,
                event_type="oversize_sync_approval",
                subject_id=approval.approval_id,
                stream_id=f"release_gate:{approval.approval_id}",
                payload=oversize_sync_approval_event_payload(approval),
                idempotency_key=f"oversize_sync_approval:{approval.approval_id}",
            )
        return revision

    def oversize_sync_approval(self, sync_plan_sha256: str) -> OversizeSyncApproval | None:
        """Return consent only under the deterministic exact-plan identity."""

        if not re.fullmatch(r"[0-9a-f]{64}", sync_plan_sha256):
            raise ValueError("sync plan digest must be a lowercase SHA-256 digest")
        approval_id = f"oversize_approval_{sync_plan_sha256}"
        try:
            payload = self.get_record("oversize_sync_approval", approval_id)
        except KeyError:
            return None
        approval = OversizeSyncApproval.model_validate(payload)
        if approval.sync_plan_sha256 != sync_plan_sha256:
            raise ValueError("oversize approval record has inconsistent plan identity")
        return approval

    def oversize_sync_approvals(self) -> list[OversizeSyncApproval]:
        """Return immutable approvals in deterministic record identity order."""

        project_id = self.project()["project_id"]
        rows = self._connection.execute(
            "SELECT payload_json FROM eco_records WHERE project_id=? "
            "AND record_type='oversize_sync_approval' ORDER BY record_id",
            (project_id,),
        ).fetchall()
        return [OversizeSyncApproval.model_validate_json(str(row["payload_json"])) for row in rows]

    def observability(self) -> dict[str, Any] | None:
        project_id = self.project()["project_id"]
        row = self._connection.execute(
            "SELECT payload_json FROM eco_observability WHERE project_id=?", (project_id,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def set_synthetic_trace_binding(self, payload: dict[str, Any]) -> str:
        """Persist an immutable Ridgeway trace-only destination outside full sync.

        The record is intentionally separate from ``eco_observability`` so no
        professional upload, artifact, run, or scope-verification path can
        interpret CC0 consent as a Restricted project binding.
        """

        project = self.project()
        if project["mode"] != "GUIDED":
            raise ValueError("synthetic trace binding requires a guided project")
        if payload.get("policy") != "RIDGEWAY_CC0_TRACE_ONLY":
            raise ValueError("synthetic trace binding policy is invalid")
        if (
            payload.get("visibility_claim") != "NONE"
            or payload.get("restricted_verified") is not False
            or payload.get("full_sync_allowed") is not False
            or payload.get("artifact_upload_allowed") is not False
        ):
            raise ValueError("synthetic trace binding cannot grant cloud visibility or sync")
        project_id = str(project["project_id"])
        payload_json = canonical_json(payload)
        binding_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT binding_sha256,payload_json FROM eco_synthetic_trace_bindings "
                "WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["binding_sha256"]) != binding_sha256
                    or str(existing["payload_json"]) != payload_json
                ):
                    raise ValueError(
                        "synthetic trace destination is immutable for this guided project"
                    )
                return binding_sha256
            connection.execute(
                "INSERT INTO eco_synthetic_trace_bindings("
                "project_id,binding_sha256,payload_json,connected_at) VALUES(?,?,?,?)",
                (project_id, binding_sha256, payload_json, _now()),
            )
        return binding_sha256

    def synthetic_trace_binding(self) -> dict[str, Any] | None:
        project_id = str(self.project()["project_id"])
        row = self._connection.execute(
            "SELECT binding_sha256,payload_json,connected_at "
            "FROM eco_synthetic_trace_bindings WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        payload_json = str(row["payload_json"])
        binding_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if binding_sha256 != str(row["binding_sha256"]):
            raise RuntimeError("synthetic trace binding integrity check failed")
        payload = json.loads(payload_json)
        payload["binding_sha256"] = binding_sha256
        payload["connected_at"] = str(row["connected_at"])
        return payload

    def set_guided(self, session_id: str, state: str, projection: dict[str, Any]) -> int:
        project_id = self.project()["project_id"]
        projection_json = canonical_json(projection)
        projection_sha256 = hashlib.sha256(projection_json.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT project_id,state,projection_json,revision FROM eco_guided_sessions "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if existing is not None and existing["project_id"] != project_id:
                raise ValueError("guided session belongs to another project")
            changed = existing is None or (
                existing["state"] != state or existing["projection_json"] != projection_json
            )
            revision = 1 if existing is None else int(existing["revision"]) + int(changed)
            now = _now()
            connection.execute(
                "INSERT INTO eco_guided_sessions(session_id,project_id,state,projection_json,"
                "revision,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "state=excluded.state,projection_json=excluded.projection_json,"
                "revision=excluded.revision,updated_at=excluded.updated_at",
                (session_id, project_id, state, projection_json, revision, now),
            )
            self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=None,
                event_type="guided_session",
                subject_id=session_id,
                stream_id=f"guided_session:{session_id}",
                payload={
                    "session_id": session_id,
                    "state": state,
                    "revision": revision,
                    "projection_sha256": projection_sha256,
                    "projection": projection,
                },
                idempotency_key=f"guided_session:{session_id}:{revision}",
            )
        return revision

    def guided(self, session_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM eco_guided_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        result = dict(row)
        result["projection"] = json.loads(result.pop("projection_json"))
        return result

    def record_guided_rerun_completion(
        self,
        *,
        session_id: str,
        analysis_id: str,
        map_analysis_id: str,
        completed_metadata: dict[str, Any],
        differential: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically publish a completed guided rerun and its cloud envelope.

        The verified fixture projection is intentionally retained byte-for-byte.
        Rerun evidence is a separate immutable CAS object and is attached to the
        next guided-session telemetry revision rather than replacing the fixture.
        """

        if (
            analysis_id == map_analysis_id
            or completed_metadata.get("session_id") != session_id
            or completed_metadata.get("analysis_id") != analysis_id
            or completed_metadata.get("map_analysis_id") != map_analysis_id
            or completed_metadata.get("phase") != "COMPLETE"
            or completed_metadata.get("differential") != differential
            or completed_metadata.get("failure") is not None
        ):
            raise ValueError("guided completion metadata differs from its durable identity")
        artifact_sha256 = str(differential.get("artifact_sha256", ""))
        if len(artifact_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in artifact_sha256
        ):
            raise ValueError("guided differential requires a lowercase CAS SHA-256")
        report = {key: value for key, value in differential.items() if key != "artifact_sha256"}
        artifact_relative_path = (
            PurePosixPath("blobs") / "sha256" / artifact_sha256[:2] / artifact_sha256[2:]
        ).as_posix()
        artifact_path = contained_path(self.root, artifact_relative_path)
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise ValueError("guided differential CAS artifact is absent or unsafe")
        if _sha256_file(artifact_path) != artifact_sha256:
            raise ValueError("guided differential CAS artifact failed hash verification")
        try:
            retained_report = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("guided differential CAS artifact is not valid JSON") from exc
        if retained_report != report:
            raise ValueError("guided differential record differs from its CAS artifact")

        project_id = self.project()["project_id"]
        now = _now()
        with self.transaction() as connection:
            session = connection.execute(
                "SELECT project_id,state,projection_json,revision FROM eco_guided_sessions "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if session is None or session["project_id"] != project_id:
                raise ValueError("guided rerun completion requires a local guided session")
            durable_rerun = connection.execute(
                "SELECT payload_json FROM eco_records WHERE project_id=? AND record_type="
                "'guided_rerun' AND record_id=?",
                (project_id, analysis_id),
            ).fetchone()
            if durable_rerun is None:
                raise ValueError("guided rerun completion requires durable rerun metadata")
            current_metadata = json.loads(durable_rerun["payload_json"])
            if (
                current_metadata.get("session_id") != session_id
                or current_metadata.get("analysis_id") != analysis_id
                or current_metadata.get("map_analysis_id") != map_analysis_id
                or current_metadata.get("phase") not in {"DIFFERENTIAL", "COMPLETE"}
            ):
                raise ValueError("guided rerun durable phase cannot be completed")
            for required_analysis_id in (analysis_id, map_analysis_id):
                analysis = connection.execute(
                    "SELECT state FROM eco_analyses WHERE analysis_id=? AND project_id=?",
                    (required_analysis_id, project_id),
                ).fetchone()
                if analysis is None or analysis["state"] != AnalysisState.COMPLETED.value:
                    raise ValueError("guided completion requires both direct-run barriers")

            self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type="guided_differential",
                record_id=analysis_id,
                payload_json=canonical_json(differential),
                allow_update=False,
                now=now,
            )
            self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type="guided_rerun",
                record_id=analysis_id,
                payload_json=canonical_json(completed_metadata),
                allow_update=True,
                now=now,
            )
            projection = json.loads(session["projection_json"])
            projection_sha256 = hashlib.sha256(
                session["projection_json"].encode("utf-8")
            ).hexdigest()
            changed = session["state"] != "COMPLETED"
            revision = int(session["revision"]) + int(changed)
            connection.execute(
                "UPDATE eco_guided_sessions SET state='COMPLETED',revision=?,updated_at=? "
                "WHERE session_id=?",
                (revision, now, session_id),
            )
            completion = {
                "analysis_id": analysis_id,
                "map_analysis_id": map_analysis_id,
                "total_jobs": int(completed_metadata["total_jobs"]),
                "matrix_jobs": int(completed_metadata["matrix_jobs"]),
                "map_jobs": int(completed_metadata["map_jobs"]),
                "differential_sha256": artifact_sha256,
                "differential_cas_relative_path": artifact_relative_path,
                "differential": report,
            }
            outbox_id = self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=analysis_id,
                event_type="guided_session",
                subject_id=session_id,
                stream_id=f"guided_session:{session_id}",
                payload={
                    "session_id": session_id,
                    "state": "COMPLETED",
                    "revision": revision,
                    "projection_sha256": projection_sha256,
                    "projection": projection,
                    "rerun_completion": completion,
                },
                idempotency_key=f"guided_session:{session_id}:{revision}",
            )
        return {"revision": revision, "outbox_id": outbox_id, **completion}

    def record_guided_checkpoint(
        self,
        *,
        session_id: str,
        event_id: str,
        sequence_number: int,
        checkpoint_id: str,
        event_kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if sequence_number < 1:
            raise ValueError("guided checkpoint sequence must be positive")
        payload_json = canonical_json(payload)
        project_id = self.project()["project_id"]
        with self.transaction() as connection:
            session = connection.execute(
                "SELECT project_id FROM eco_guided_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if session is None or session["project_id"] != project_id:
                raise ValueError("guided checkpoint requires a local guided session")
            existing = connection.execute(
                "SELECT session_id,sequence_number,checkpoint_id,event_kind,payload_json,created_at "
                "FROM eco_guided_checkpoints WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["session_id"] != session_id
                    or int(existing["sequence_number"]) != sequence_number
                    or existing["checkpoint_id"] != checkpoint_id
                    or existing["event_kind"] != event_kind
                    or existing["payload_json"] != payload_json
                ):
                    raise ValueError("guided event identity was reused for different evidence")
                created_at = str(existing["created_at"])
            else:
                created_at = _now()
                connection.execute(
                    "INSERT INTO eco_guided_checkpoints(event_id,session_id,sequence_number,"
                    "checkpoint_id,event_kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        event_id,
                        session_id,
                        sequence_number,
                        checkpoint_id,
                        event_kind,
                        payload_json,
                        created_at,
                    ),
                )
            telemetry = {
                "event_id": event_id,
                "session_id": session_id,
                "sequence_number": sequence_number,
                "checkpoint_id": checkpoint_id,
                "event_kind": event_kind,
                "payload": payload,
                "created_at": created_at,
            }
            self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=None,
                event_type="guided_checkpoint",
                subject_id=event_id,
                stream_id=f"guided_session:{session_id}",
                payload=telemetry,
                idempotency_key=f"guided_checkpoint:{event_id}",
            )
        return telemetry

    def guided_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for row in self._connection.execute(
            "SELECT * FROM eco_guided_checkpoints WHERE session_id=? "
            "ORDER BY sequence_number,event_id",
            (session_id,),
        ):
            record = dict(row)
            record["payload"] = json.loads(record.pop("payload_json"))
            records.append(record)
        return records

    def store_assistant_answer(self, call_id: str, answer: dict[str, Any], trace_id: str) -> None:
        if (
            not call_id
            or answer.get("call_id") != call_id
            or answer.get("trace_id") != trace_id
            or answer.get("trace_readback_verified") is not True
        ):
            raise ValueError("assistant answer lacks exact completed-trace evidence")
        project_id = self.project()["project_id"]
        answer_json = canonical_json(answer)
        with self.transaction() as connection:
            rejected = connection.execute(
                "SELECT 1 FROM eco_assistant_rejection_audits WHERE call_id=?", (call_id,)
            ).fetchone()
            if rejected is not None:
                raise ValueError("rejected assistant call cannot publish an answer")
            existing = connection.execute(
                "SELECT project_id,answer_json,trace_id FROM eco_assistant_records WHERE call_id=?",
                (call_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["project_id"]) != project_id
                    or str(existing["answer_json"]) != answer_json
                    or str(existing["trace_id"]) != trace_id
                ):
                    raise ValueError("assistant call identity was reused for a different answer")
                return
            connection.execute(
                "INSERT INTO eco_assistant_records(call_id,project_id,answer_json,trace_id,created_at) "
                "VALUES(?,?,?,?,?)",
                (call_id, project_id, answer_json, trace_id, _now()),
            )

    def record_assistant_rejection(
        self,
        *,
        call_id: str,
        operation: str,
        error_code: str,
        provider_call_id_sha256: str,
        provider_model: str,
        association: dict[str, Any],
        provider_abort_trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a checksum-verified, answer-exclusive post-provider rejection."""

        if operation not in {"evidence.explain", "evidence_intake.extract"}:
            raise ValueError("assistant rejection operation is unsupported")
        if not _ASSISTANT_AUDIT_ERROR.fullmatch(error_code):
            raise ValueError("assistant rejection requires a stable error code")
        if (
            len(provider_call_id_sha256) != 64
            or any(character not in "0123456789abcdef" for character in provider_call_id_sha256)
            or not provider_model.strip()
            or len(provider_model) > 512
        ):
            raise ValueError("assistant rejection provider identity is invalid")
        project_id = self.project()["project_id"]
        if association.get("project_id") != project_id:
            raise ValueError("assistant rejection association belongs to another project")
        if provider_abort_trace is not None:
            completed = provider_abort_trace.get("completed")
            if (
                provider_abort_trace.get("call_id") != call_id
                or completed not in {True, False}
                or provider_abort_trace.get("persisted") is not True
                or provider_abort_trace.get("trace_readback_verified") is not True
                or provider_abort_trace.get("association") != association
                or (
                    completed is True
                    and (
                        provider_abort_trace.get("answer_returned") is not False
                        or not provider_abort_trace.get("trace_id")
                        or not provider_abort_trace.get("weave_call_id")
                    )
                )
            ):
                raise ValueError("assistant rejection trace is not verified provider evidence")
        completed_trace_verified = bool(
            provider_abort_trace is not None and provider_abort_trace.get("completed") is True
        )
        payload = {
            "schema_version": "ecocorridor-assistant-rejection-audit/v1.1",
            "call_id": call_id,
            "project_id": project_id,
            "operation": operation,
            "disposition": "REJECTED",
            "error_code": error_code,
            "provider_completed": True,
            "provider_call_id_sha256": provider_call_id_sha256,
            "provider_model": provider_model,
            "association": association,
            "provider_abort_trace": provider_abort_trace,
            "provider_abort_trace_verified": provider_abort_trace is not None,
            "completed_trace_readback_verified": completed_trace_verified,
            "answer_returned": False,
        }
        payload_json = canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            answered = connection.execute(
                "SELECT 1 FROM eco_assistant_records WHERE call_id=?", (call_id,)
            ).fetchone()
            if answered is not None:
                raise ValueError("answered assistant call cannot become rejected")
            existing = connection.execute(
                "SELECT project_id,operation,error_code,payload_json,payload_sha256 "
                "FROM eco_assistant_rejection_audits WHERE call_id=?",
                (call_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["project_id"]) != project_id
                    or str(existing["operation"]) != operation
                    or str(existing["error_code"]) != error_code
                    or str(existing["payload_json"]) != payload_json
                    or str(existing["payload_sha256"]) != payload_sha256
                ):
                    raise ValueError("assistant call identity was reused for a different rejection")
            else:
                connection.execute(
                    "INSERT INTO eco_assistant_rejection_audits(call_id,project_id,operation,"
                    "error_code,payload_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        call_id,
                        project_id,
                        operation,
                        error_code,
                        payload_json,
                        payload_sha256,
                        _now(),
                    ),
                )
        return self.assistant_rejection_audit(call_id)

    def assistant_rejection_audit(self, call_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json,payload_sha256 FROM eco_assistant_rejection_audits "
            "WHERE call_id=?",
            (call_id,),
        ).fetchone()
        if row is None:
            raise KeyError(call_id)
        payload_json = str(row["payload_json"])
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if payload_sha256 != str(row["payload_sha256"]):
            raise ValueError("assistant rejection audit failed its local checksum")
        payload = json.loads(payload_json)
        if (
            payload.get("call_id") != call_id
            or payload.get("disposition") != "REJECTED"
            or payload.get("provider_completed") is not True
            or payload.get("completed_trace_readback_verified")
            is not bool(
                isinstance(payload.get("provider_abort_trace"), dict)
                and payload["provider_abort_trace"].get("completed") is True
            )
            or payload.get("answer_returned") is not False
        ):
            raise ValueError("assistant rejection audit has an invalid safety disposition")
        return {
            **payload,
            "audit_sha256": payload_sha256,
            "local_audit_verified": True,
        }

    def assistant_call_record(self, call_id: str) -> dict[str, Any] | None:
        answer = self._connection.execute(
            "SELECT answer_json,trace_id FROM eco_assistant_records WHERE call_id=?", (call_id,)
        ).fetchone()
        rejection = self._connection.execute(
            "SELECT 1 FROM eco_assistant_rejection_audits WHERE call_id=?", (call_id,)
        ).fetchone()
        if answer is not None and rejection is not None:
            raise ValueError("assistant call has conflicting terminal dispositions")
        if answer is not None:
            return {
                "disposition": "ANSWERED",
                "answer": json.loads(answer["answer_json"]),
                "trace_id": str(answer["trace_id"]),
            }
        if rejection is not None:
            return {
                "disposition": "REJECTED",
                "audit": self.assistant_rejection_audit(call_id),
            }
        return None

    def assistant_answer(self, call_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT answer_json,trace_id FROM eco_assistant_records WHERE call_id=?", (call_id,)
        ).fetchone()
        if row is None:
            raise KeyError(call_id)
        return {"answer": json.loads(row["answer_json"]), "trace_id": row["trace_id"]}

    def assistant_answer_by_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Resolve one retained assistant response by confirmed Weave trace identity.

        Review Briefs retain their exact trace URL inside the immutable draft
        rather than the general-purpose assistant-answer table. Return the same
        narrow answer-shaped projection so the observability adapter can apply
        its existing destination URL allowlist without knowing the domain table.
        """

        answer_rows = self._connection.execute(
            "SELECT answer_json,trace_id FROM eco_assistant_records WHERE trace_id=? "
            "ORDER BY call_id LIMIT 2",
            (trace_id,),
        ).fetchall()
        draft_rows = self._connection.execute(
            "SELECT * FROM eco_review_brief_drafts ORDER BY created_at,draft_id"
        ).fetchall()
        matching_drafts = [
            draft
            for row in draft_rows
            if (
                draft := self.review_brief_draft_record(str(row["draft_id"]))["draft"]
            ).trace.trace_id
            == trace_id
        ]
        if len(answer_rows) + len(matching_drafts) > 1:
            raise ValueError("assistant trace identity resolves to multiple retained answers")
        if answer_rows:
            return {
                "answer": json.loads(answer_rows[0]["answer_json"]),
                "trace_id": answer_rows[0]["trace_id"],
            }
        if not matching_drafts:
            return None
        draft = matching_drafts[0]
        return {
            "answer": {
                "trace_id": draft.trace.trace_id,
                "weave_call_id": draft.trace.weave_call_id,
                "trace_url": draft.trace.trace_url,
            },
            "trace_id": draft.trace.trace_id,
        }

    @staticmethod
    def _review_brief_normalized_rows(
        *,
        project_id: str,
        call_id: str,
        draft: Any,
        runner: Any,
    ) -> dict[str, list[dict[str, Any]]]:
        """Create the canonical relational projection of one accepted agent result."""

        if len(draft.items) != len(runner.selections):
            raise ValueError("review brief items differ from the agent selections")
        if runner.trace != draft.trace:
            raise ValueError("review brief agent execution has a different trace")

        def sealed(payload: dict[str, Any]) -> tuple[str, str]:
            payload_json = canonical_json(payload)
            return payload_json, hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        created_at = draft.created_at.isoformat()
        execution_id = (
            "review_execution_"
            + canonical_sha256(
                {
                    "call_id": call_id,
                    "draft_id": draft.draft_id,
                    "snapshot_sha256": runner.snapshot_sha256,
                    "provider_call_id_sha256": runner.provider_call_id_sha256,
                    "trace_id": runner.trace.trace_id,
                }
            )[:24]
        )
        rows: dict[str, list[dict[str, Any]]] = {
            "items": [],
            "selections": [],
            "edges": [],
            "executions": [],
            "tools": [],
            "validations": [],
            "traces": [],
        }
        for item_sequence, (item, selection) in enumerate(
            zip(draft.items, runner.selections, strict=True),
            start=1,
        ):
            item_payload = {
                "schema_version": "ecocorridor-review-brief-item/v1.0",
                "draft_id": draft.draft_id,
                "item_sequence": item_sequence,
                "item": item.model_dump(mode="json"),
            }
            item_json, item_sha256 = sealed(item_payload)
            rows["items"].append(
                {
                    "draft_id": draft.draft_id,
                    "project_id": project_id,
                    "item_id": item.item_id,
                    "item_sequence": item_sequence,
                    "payload_json": item_json,
                    "payload_sha256": item_sha256,
                }
            )
            selection_payload = {
                "schema_version": "ecocorridor-review-brief-finding-selection/v1.0",
                "draft_id": draft.draft_id,
                "item_id": item.item_id,
                "selection_sequence": item_sequence,
                "selection": selection.model_dump(mode="json"),
            }
            selection_json, selection_sha256 = sealed(selection_payload)
            rows["selections"].append(
                {
                    "draft_id": draft.draft_id,
                    "project_id": project_id,
                    "finding_alias": selection.finding_alias,
                    "item_id": item.item_id,
                    "selection_sequence": item_sequence,
                    "payload_json": selection_json,
                    "payload_sha256": selection_sha256,
                }
            )
            edge_sequence = 0
            for edge_kind, targets in (
                ("EVIDENCE_CLAIM", item.evidence_claim_ids),
                ("DIRECT_RUN", item.direct_run_ids),
                ("SCENARIO", item.scenario_ids),
            ):
                for target_id in targets:
                    edge_sequence += 1
                    edge_identity = {
                        "draft_id": draft.draft_id,
                        "item_id": item.item_id,
                        "edge_kind": edge_kind,
                        "target_id": target_id,
                    }
                    edge_id = f"review_edge_{canonical_sha256(edge_identity)[:24]}"
                    edge_payload = {
                        "schema_version": "ecocorridor-review-brief-evidence-edge/v1.0",
                        "edge_id": edge_id,
                        "edge_sequence": edge_sequence,
                        **edge_identity,
                    }
                    edge_json, edge_sha256 = sealed(edge_payload)
                    rows["edges"].append(
                        {
                            "edge_id": edge_id,
                            "draft_id": draft.draft_id,
                            "project_id": project_id,
                            "item_id": item.item_id,
                            "edge_sequence": edge_sequence,
                            "edge_kind": edge_kind,
                            "target_id": target_id,
                            "payload_json": edge_json,
                            "payload_sha256": edge_sha256,
                        }
                    )

        execution_payload = {
            "schema_version": "ecocorridor-review-brief-agent-execution/v1.0",
            "execution_id": execution_id,
            "draft_id": draft.draft_id,
            "call_id": call_id,
            "snapshot_sha256": runner.snapshot_sha256,
            "runner_kind": runner.runner_kind,
            "provider_call_id_sha256": runner.provider_call_id_sha256,
            "provider_model": runner.provider_model,
            "input_tokens": runner.input_tokens,
            "output_tokens": runner.output_tokens,
            "elapsed_ms": runner.elapsed_ms,
            "selection_count": len(runner.selections),
            "tool_call_count": len(runner.tool_calls),
            "created_at": created_at,
        }
        execution_json, execution_sha256 = sealed(execution_payload)
        rows["executions"].append(
            {
                "execution_id": execution_id,
                "draft_id": draft.draft_id,
                "call_id": call_id,
                "project_id": project_id,
                "snapshot_sha256": runner.snapshot_sha256,
                "payload_json": execution_json,
                "payload_sha256": execution_sha256,
                "created_at": created_at,
            }
        )
        for tool_sequence, tool_name in enumerate(runner.tool_calls, start=1):
            tool_payload = {
                "schema_version": "ecocorridor-review-brief-tool-receipt/v1.0",
                "execution_id": execution_id,
                "draft_id": draft.draft_id,
                "snapshot_sha256": runner.snapshot_sha256,
                "trace_id": runner.trace.trace_id,
                "tool_sequence": tool_sequence,
                "tool_name": tool_name,
            }
            tool_json, tool_sha256 = sealed(tool_payload)
            rows["tools"].append(
                {
                    "execution_id": execution_id,
                    "tool_sequence": tool_sequence,
                    "tool_name": tool_name,
                    "payload_json": tool_json,
                    "payload_sha256": tool_sha256,
                }
            )
        validation_payload = {
            "schema_version": "ecocorridor-review-brief-validation-record/v1.0",
            "draft_id": draft.draft_id,
            "validation": draft.validation.model_dump(mode="json"),
        }
        validation_json, validation_sha256 = sealed(validation_payload)
        rows["validations"].append(
            {
                "draft_id": draft.draft_id,
                "project_id": project_id,
                "payload_json": validation_json,
                "payload_sha256": validation_sha256,
                "created_at": created_at,
            }
        )
        trace_payload = {
            "schema_version": "ecocorridor-review-brief-trace-reference/v1.0",
            "draft_id": draft.draft_id,
            "trace": draft.trace.model_dump(mode="json"),
        }
        trace_json, trace_sha256 = sealed(trace_payload)
        rows["traces"].append(
            {
                "draft_id": draft.draft_id,
                "project_id": project_id,
                "trace_id": draft.trace.trace_id,
                "weave_call_id": draft.trace.weave_call_id,
                "wandb_run_id": draft.trace.wandb_run_id,
                "payload_json": trace_json,
                "payload_sha256": trace_sha256,
                "created_at": created_at,
            }
        )
        return rows

    def _verify_review_brief_normalized(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        call_id: str,
        draft: Any,
        runner: Any,
    ) -> None:
        expected = self._review_brief_normalized_rows(
            project_id=project_id,
            call_id=call_id,
            draft=draft,
            runner=runner,
        )
        specifications = (
            (
                "items",
                "eco_review_brief_items",
                "draft_id=?",
                (draft.draft_id,),
                "item_sequence",
                (
                    "draft_id",
                    "project_id",
                    "item_id",
                    "item_sequence",
                    "payload_json",
                    "payload_sha256",
                ),
            ),
            (
                "selections",
                "eco_review_brief_finding_selections",
                "draft_id=?",
                (draft.draft_id,),
                "selection_sequence",
                (
                    "draft_id",
                    "project_id",
                    "finding_alias",
                    "item_id",
                    "selection_sequence",
                    "payload_json",
                    "payload_sha256",
                ),
            ),
            (
                "edges",
                "eco_review_brief_evidence_edges",
                "draft_id=?",
                (draft.draft_id,),
                "item_id,edge_sequence",
                (
                    "edge_id",
                    "draft_id",
                    "project_id",
                    "item_id",
                    "edge_sequence",
                    "edge_kind",
                    "target_id",
                    "payload_json",
                    "payload_sha256",
                ),
            ),
            (
                "executions",
                "eco_review_brief_agent_executions",
                "draft_id=?",
                (draft.draft_id,),
                "execution_id",
                (
                    "execution_id",
                    "draft_id",
                    "call_id",
                    "project_id",
                    "snapshot_sha256",
                    "payload_json",
                    "payload_sha256",
                    "created_at",
                ),
            ),
            (
                "tools",
                "eco_review_brief_tool_receipts",
                (
                    "execution_id=(SELECT execution_id FROM "
                    "eco_review_brief_agent_executions WHERE draft_id=?)"
                ),
                (draft.draft_id,),
                "tool_sequence",
                (
                    "execution_id",
                    "tool_sequence",
                    "tool_name",
                    "payload_json",
                    "payload_sha256",
                ),
            ),
            (
                "validations",
                "eco_review_brief_validations",
                "draft_id=?",
                (draft.draft_id,),
                "draft_id",
                (
                    "draft_id",
                    "project_id",
                    "payload_json",
                    "payload_sha256",
                    "created_at",
                ),
            ),
            (
                "traces",
                "eco_review_brief_trace_references",
                "draft_id=?",
                (draft.draft_id,),
                "draft_id",
                (
                    "draft_id",
                    "project_id",
                    "trace_id",
                    "weave_call_id",
                    "wandb_run_id",
                    "payload_json",
                    "payload_sha256",
                    "created_at",
                ),
            ),
        )
        for key, table, where, parameters, order_by, columns in specifications:
            actual_rows = connection.execute(
                f"SELECT {','.join(columns)} FROM {table} WHERE {where} ORDER BY {order_by}",
                parameters,
            ).fetchall()
            actual = [{column: row[column] for column in columns} for row in actual_rows]
            if actual != expected[key]:
                raise ValueError(f"review brief normalized {key} failed integrity validation")
            for row in actual:
                payload_json = str(row["payload_json"])
                if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(
                    row["payload_sha256"]
                ):
                    raise ValueError(f"review brief normalized {key} failed its checksum")

    def _insert_review_brief_normalized(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        call_id: str,
        draft: Any,
        runner: Any,
    ) -> None:
        expected = self._review_brief_normalized_rows(
            project_id=project_id,
            call_id=call_id,
            draft=draft,
            runner=runner,
        )
        existing = sum(
            int(connection.execute(query, parameters).fetchone()[0])
            for query, parameters in (
                (
                    "SELECT COUNT(*) FROM eco_review_brief_items WHERE draft_id=?",
                    (draft.draft_id,),
                ),
                (
                    "SELECT COUNT(*) FROM eco_review_brief_finding_selections WHERE draft_id=?",
                    (draft.draft_id,),
                ),
                (
                    "SELECT COUNT(*) FROM eco_review_brief_evidence_edges WHERE draft_id=?",
                    (draft.draft_id,),
                ),
                (
                    "SELECT COUNT(*) FROM eco_review_brief_agent_executions WHERE draft_id=?",
                    (draft.draft_id,),
                ),
                (
                    "SELECT COUNT(*) FROM eco_review_brief_validations WHERE draft_id=?",
                    (draft.draft_id,),
                ),
                (
                    "SELECT COUNT(*) FROM eco_review_brief_trace_references WHERE draft_id=?",
                    (draft.draft_id,),
                ),
            )
        )
        if existing:
            self._verify_review_brief_normalized(
                connection,
                project_id=project_id,
                call_id=call_id,
                draft=draft,
                runner=runner,
            )
            return

        insertions = (
            (
                "items",
                (
                    "INSERT INTO eco_review_brief_items(draft_id,project_id,item_id,"
                    "item_sequence,payload_json,payload_sha256) VALUES(?,?,?,?,?,?)"
                ),
                (
                    "draft_id",
                    "project_id",
                    "item_id",
                    "item_sequence",
                    "payload_json",
                    "payload_sha256",
                ),
            ),
            (
                "selections",
                (
                    "INSERT INTO eco_review_brief_finding_selections(draft_id,project_id,"
                    "finding_alias,item_id,selection_sequence,payload_json,payload_sha256) "
                    "VALUES(?,?,?,?,?,?,?)"
                ),
                (
                    "draft_id",
                    "project_id",
                    "finding_alias",
                    "item_id",
                    "selection_sequence",
                    "payload_json",
                    "payload_sha256",
                ),
            ),
            (
                "edges",
                (
                    "INSERT INTO eco_review_brief_evidence_edges(edge_id,draft_id,project_id,"
                    "item_id,edge_sequence,edge_kind,target_id,payload_json,payload_sha256) "
                    "VALUES(?,?,?,?,?,?,?,?,?)"
                ),
                (
                    "edge_id",
                    "draft_id",
                    "project_id",
                    "item_id",
                    "edge_sequence",
                    "edge_kind",
                    "target_id",
                    "payload_json",
                    "payload_sha256",
                ),
            ),
            (
                "executions",
                (
                    "INSERT INTO eco_review_brief_agent_executions(execution_id,draft_id,call_id,"
                    "project_id,snapshot_sha256,payload_json,payload_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)"
                ),
                (
                    "execution_id",
                    "draft_id",
                    "call_id",
                    "project_id",
                    "snapshot_sha256",
                    "payload_json",
                    "payload_sha256",
                    "created_at",
                ),
            ),
            (
                "tools",
                (
                    "INSERT INTO eco_review_brief_tool_receipts(execution_id,tool_sequence,"
                    "tool_name,payload_json,payload_sha256) VALUES(?,?,?,?,?)"
                ),
                (
                    "execution_id",
                    "tool_sequence",
                    "tool_name",
                    "payload_json",
                    "payload_sha256",
                ),
            ),
            (
                "validations",
                (
                    "INSERT INTO eco_review_brief_validations(draft_id,project_id,payload_json,"
                    "payload_sha256,created_at) VALUES(?,?,?,?,?)"
                ),
                ("draft_id", "project_id", "payload_json", "payload_sha256", "created_at"),
            ),
            (
                "traces",
                (
                    "INSERT INTO eco_review_brief_trace_references(draft_id,project_id,trace_id,"
                    "weave_call_id,wandb_run_id,payload_json,payload_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)"
                ),
                (
                    "draft_id",
                    "project_id",
                    "trace_id",
                    "weave_call_id",
                    "wandb_run_id",
                    "payload_json",
                    "payload_sha256",
                    "created_at",
                ),
            ),
        )
        for key, statement, columns in insertions:
            for row in expected[key]:
                connection.execute(statement, tuple(row[column] for column in columns))
        self._verify_review_brief_normalized(
            connection,
            project_id=project_id,
            call_id=call_id,
            draft=draft,
            runner=runner,
        )

    def publish_review_brief_draft(
        self,
        *,
        call_id: str,
        draft: Any,
        runner_result: Any,
        source: Any,
    ) -> Any:
        """Atomically publish one immutable, trace-verified unconfirmed draft.

        The provider result is retained for deterministic replay, but only the
        locally rendered draft is exposed to the renderer. A call ID can end in
        exactly one draft or one rejection audit.
        """

        from .review_brief import (
            ReviewBriefDraft,
            ReviewBriefRunnerResult,
            ReviewBriefSource,
            validate_rendered_review_brief,
        )

        validated_draft = ReviewBriefDraft.model_validate(draft)
        validated_runner = ReviewBriefRunnerResult.model_validate(runner_result)
        if not isinstance(source, ReviewBriefSource):
            raise TypeError("review brief persistence requires its authoritative source")
        validate_rendered_review_brief(
            validated_draft,
            source=source,
            result=validated_runner,
        )
        if validated_draft.status != "UNCONFIRMED":
            raise ValueError("only an unconfirmed review brief can be persisted")
        if validated_runner.trace != validated_draft.trace:
            raise ValueError("review brief draft does not retain the verified runner trace")
        project_id = str(self.project()["project_id"])
        if (
            source.project_id != project_id
            or source.source_mode != validated_draft.source_mode
            or source.analysis_id != validated_draft.analysis_id
            or source.portfolio_id != validated_draft.portfolio_id
            or source.model_revision_id != validated_draft.model_revision_id
            or source.decision_board_sha256 != validated_draft.decision_board_sha256
        ):
            raise ValueError("review brief source identity differs from the project draft")
        payload_json = canonical_json(validated_draft.model_dump(mode="json"))
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        runner_json = canonical_json(validated_runner.model_dump(mode="json"))
        runner_sha256 = hashlib.sha256(runner_json.encode("utf-8")).hexdigest()
        run_kind = (
            "guided_session"
            if validated_draft.source_mode.value == "RIDGEWAY_SYNTHETIC"
            else "connectivity_analysis"
        )
        run_subject_id = (
            "guided_ridgeway_v1"
            if validated_draft.source_mode.value == "RIDGEWAY_SYNTHETIC"
            else validated_draft.analysis_id
        )
        outbox_payload = {
            "schema_version": "ecocorridor-review-brief-draft-event/v1.0",
            "draft_id": validated_draft.draft_id,
            "source_mode": validated_draft.source_mode.value,
            "analysis_id": validated_draft.analysis_id,
            "portfolio_id": validated_draft.portfolio_id,
            "model_revision_id": validated_draft.model_revision_id,
            "decision_board_sha256": validated_draft.decision_board_sha256,
            "draft_sha256": payload_sha256,
            "runner_receipt_sha256": runner_sha256,
            "trace_receipt_sha256": canonical_sha256(validated_draft.trace.model_dump(mode="json")),
            "item_count": len(validated_draft.items),
            "evidence_claim_set_sha256": canonical_sha256(
                sorted(
                    {
                        claim_id
                        for item in validated_draft.items
                        for claim_id in item.evidence_claim_ids
                    }
                )
            ),
            "created_at": validated_draft.created_at.isoformat(),
        }
        with self.transaction() as connection:
            rejected = connection.execute(
                "SELECT 1 FROM eco_review_brief_rejections WHERE call_id=?",
                (call_id,),
            ).fetchone()
            if rejected is not None:
                raise ValueError("rejected review brief call cannot publish a draft")
            existing = connection.execute(
                "SELECT * FROM eco_review_brief_drafts WHERE call_id=?",
                (call_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["project_id"]) != project_id
                    or str(existing["draft_id"]) != validated_draft.draft_id
                    or str(existing["payload_json"]) != payload_json
                    or str(existing["payload_sha256"]) != payload_sha256
                    or str(existing["runner_json"]) != runner_json
                    or str(existing["runner_sha256"]) != runner_sha256
                ):
                    raise ValueError("review brief call identity was reused")
                self._verify_review_brief_normalized(
                    connection,
                    project_id=project_id,
                    call_id=call_id,
                    draft=validated_draft,
                    runner=validated_runner,
                )
                if validated_draft.source_mode.value == "PROFESSIONAL":
                    self._enqueue_outbox(
                        connection,
                        project_id=project_id,
                        analysis_id=validated_draft.analysis_id,
                        event_type="review_brief_draft",
                        subject_id=validated_draft.draft_id,
                        stream_id=f"{run_kind}:{run_subject_id}",
                        payload=outbox_payload,
                        idempotency_key=f"review_brief_draft:{validated_draft.draft_id}",
                    )
                return validated_draft

            latest = connection.execute(
                "SELECT draft.draft_id FROM eco_review_brief_drafts AS draft "
                "WHERE draft.project_id=? AND draft.analysis_id=? AND draft.portfolio_id=? "
                "AND NOT EXISTS (SELECT 1 FROM eco_review_brief_drafts AS child "
                "WHERE child.supersedes_draft_id=draft.draft_id) "
                "ORDER BY draft.created_at DESC,draft.draft_id DESC LIMIT 1",
                (project_id, validated_draft.analysis_id, validated_draft.portfolio_id),
            ).fetchone()
            expected_parent = str(latest["draft_id"]) if latest is not None else None
            if validated_draft.supersedes_draft_id != expected_parent:
                raise ValueError("review brief supersedes identity is not the current draft")
            if expected_parent is not None:
                parent = connection.execute(
                    "SELECT analysis_id,portfolio_id FROM eco_review_brief_drafts WHERE draft_id=?",
                    (expected_parent,),
                ).fetchone()
                if parent is None or (
                    str(parent["analysis_id"]) != validated_draft.analysis_id
                    or str(parent["portfolio_id"]) != validated_draft.portfolio_id
                ):
                    raise ValueError("review brief supersedes another decision scope")
            connection.execute(
                "INSERT INTO eco_review_brief_drafts("
                "draft_id,call_id,project_id,analysis_id,portfolio_id,model_revision_id,"
                "decision_board_sha256,supersedes_draft_id,payload_json,payload_sha256,"
                "runner_json,runner_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    validated_draft.draft_id,
                    call_id,
                    project_id,
                    validated_draft.analysis_id,
                    validated_draft.portfolio_id,
                    validated_draft.model_revision_id,
                    validated_draft.decision_board_sha256,
                    validated_draft.supersedes_draft_id,
                    payload_json,
                    payload_sha256,
                    runner_json,
                    runner_sha256,
                    validated_draft.created_at.isoformat(),
                ),
            )
            self._insert_review_brief_normalized(
                connection,
                project_id=project_id,
                call_id=call_id,
                draft=validated_draft,
                runner=validated_runner,
            )
            if validated_draft.source_mode.value == "PROFESSIONAL":
                self._enqueue_outbox(
                    connection,
                    project_id=project_id,
                    analysis_id=validated_draft.analysis_id,
                    event_type="review_brief_draft",
                    subject_id=validated_draft.draft_id,
                    stream_id=f"{run_kind}:{run_subject_id}",
                    payload=outbox_payload,
                    idempotency_key=f"review_brief_draft:{validated_draft.draft_id}",
                )
        return validated_draft

    def review_brief_draft_record(self, draft_id: str) -> dict[str, Any]:
        """Return a checksum-verified draft plus its alias-only runner receipt."""

        from .review_brief import ReviewBriefRunnerResult

        row = self._connection.execute(
            "SELECT * FROM eco_review_brief_drafts WHERE draft_id=? AND project_id=?",
            (draft_id, self.project()["project_id"]),
        ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        draft = self._review_brief_draft_from_row(row)
        runner_json = str(row["runner_json"])
        runner_sha256 = hashlib.sha256(runner_json.encode("utf-8")).hexdigest()
        if runner_sha256 != str(row["runner_sha256"]):
            raise ValueError("review brief runner receipt failed its local checksum")
        runner = ReviewBriefRunnerResult.model_validate_json(runner_json)
        if runner.trace != draft.trace:
            raise ValueError("review brief runner receipt has a different trace")
        self._verify_review_brief_normalized(
            self._connection,
            project_id=str(row["project_id"]),
            call_id=str(row["call_id"]),
            draft=draft,
            runner=runner,
        )
        return {
            "draft": draft,
            "runner": runner,
            "draft_sha256": str(row["payload_sha256"]),
            "runner_sha256": runner_sha256,
        }

    @staticmethod
    def _review_brief_draft_from_row(row: sqlite3.Row) -> Any:
        from .review_brief import ReviewBriefDraft

        payload_json = str(row["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(row["payload_sha256"]):
            raise ValueError("review brief draft failed its local checksum")
        draft = ReviewBriefDraft.model_validate_json(payload_json)
        if (
            draft.draft_id != str(row["draft_id"])
            or draft.analysis_id != str(row["analysis_id"])
            or draft.portfolio_id != str(row["portfolio_id"])
            or draft.model_revision_id != str(row["model_revision_id"])
            or draft.decision_board_sha256 != str(row["decision_board_sha256"])
        ):
            raise ValueError("review brief draft identity is inconsistent")
        return draft

    def review_brief_draft(self, draft_id: str) -> Any:
        return self.review_brief_draft_record(draft_id)["draft"]

    def review_brief_draft_by_trace(self, trace_id: str) -> Any:
        rows = self._connection.execute(
            "SELECT * FROM eco_review_brief_drafts WHERE project_id=? ORDER BY created_at,draft_id",
            (self.project()["project_id"],),
        ).fetchall()
        drafts = [self.review_brief_draft_record(str(row["draft_id"]))["draft"] for row in rows]
        matches = [draft for draft in drafts if draft.trace.trace_id == trace_id]
        if len(matches) != 1:
            raise KeyError(trace_id)
        return matches[0]

    def list_review_brief_drafts(
        self,
        *,
        analysis_id: str | None = None,
        portfolio_id: str | None = None,
    ) -> list[Any]:
        project_id = str(self.project()["project_id"])
        clauses = ["project_id=?"]
        parameters: list[str] = [project_id]
        if analysis_id is not None:
            clauses.append("analysis_id=?")
            parameters.append(analysis_id)
        if portfolio_id is not None:
            clauses.append("portfolio_id=?")
            parameters.append(portfolio_id)
        rows = self._connection.execute(
            "SELECT * FROM eco_review_brief_drafts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC,draft_id DESC",
            tuple(parameters),
        ).fetchall()
        return [self.review_brief_draft_record(str(row["draft_id"]))["draft"] for row in rows]

    def latest_review_brief_draft(self, *, analysis_id: str, portfolio_id: str) -> Any | None:
        rows = self.list_review_brief_drafts(
            analysis_id=analysis_id,
            portfolio_id=portfolio_id,
        )
        if not rows:
            return None
        child_parent_ids = {
            draft.supersedes_draft_id for draft in rows if draft.supersedes_draft_id is not None
        }
        tips = [draft for draft in rows if draft.draft_id not in child_parent_ids]
        if len(tips) != 1:
            raise ValueError("review brief version chain has multiple current drafts")
        return tips[0]

    def record_review_brief_rejection(self, audit: Any) -> Any:
        from .review_brief import ReviewBriefRejectionAudit

        validated = ReviewBriefRejectionAudit.model_validate(audit)
        project_id = str(self.project()["project_id"])
        if validated.project_id != project_id:
            raise ValueError("review brief rejection belongs to another project")
        base = validated.model_copy(update={"audit_sha256": None, "local_audit_verified": False})
        payload_json = canonical_json(base.model_dump(mode="json"))
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            published = connection.execute(
                "SELECT 1 FROM eco_review_brief_drafts WHERE call_id=?",
                (validated.call_id,),
            ).fetchone()
            if published is not None:
                raise ValueError("published review brief call cannot become rejected")
            existing = connection.execute(
                "SELECT * FROM eco_review_brief_rejections WHERE call_id=?",
                (validated.call_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["project_id"]) != project_id
                    or str(existing["payload_json"]) != payload_json
                    or str(existing["payload_sha256"]) != payload_sha256
                ):
                    raise ValueError("review brief call identity was reused")
            else:
                connection.execute(
                    "INSERT INTO eco_review_brief_rejections("
                    "call_id,project_id,analysis_id,portfolio_id,error_code,payload_json,"
                    "payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        validated.call_id,
                        project_id,
                        validated.analysis_id,
                        validated.portfolio_id,
                        validated.error_code,
                        payload_json,
                        payload_sha256,
                        validated.created_at.isoformat(),
                    ),
                )
        return self.review_brief_rejection(validated.call_id)

    def review_brief_rejection(self, call_id: str) -> Any:
        from .review_brief import ReviewBriefRejectionAudit

        row = self._connection.execute(
            "SELECT payload_json,payload_sha256 FROM eco_review_brief_rejections "
            "WHERE call_id=? AND project_id=?",
            (call_id, self.project()["project_id"]),
        ).fetchone()
        if row is None:
            raise KeyError(call_id)
        payload_json = str(row["payload_json"])
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if digest != str(row["payload_sha256"]):
            raise ValueError("review brief rejection failed its local checksum")
        audit = ReviewBriefRejectionAudit.model_validate_json(payload_json)
        return audit.model_copy(update={"audit_sha256": digest, "local_audit_verified": True})

    def review_brief_call_record(self, call_id: str) -> dict[str, Any] | None:
        draft = self._connection.execute(
            "SELECT * FROM eco_review_brief_drafts WHERE call_id=? AND project_id=?",
            (call_id, self.project()["project_id"]),
        ).fetchone()
        rejected = self._connection.execute(
            "SELECT 1 FROM eco_review_brief_rejections WHERE call_id=? AND project_id=?",
            (call_id, self.project()["project_id"]),
        ).fetchone()
        if draft is not None and rejected is not None:
            raise ValueError("review brief call has conflicting terminal dispositions")
        if draft is not None:
            return {
                "disposition": "DRAFTED",
                "draft": self.review_brief_draft_record(str(draft["draft_id"]))["draft"],
            }
        if rejected is not None:
            return {"disposition": "REJECTED", "audit": self.review_brief_rejection(call_id)}
        return None

    def attest_review_brief(
        self,
        *,
        acknowledgement: Any,
        current_model_revision_id: str,
        current_decision_board_sha256: str,
    ) -> Any:
        from .review_brief import ReviewBriefAcknowledgement

        validated = ReviewBriefAcknowledgement.model_validate(acknowledgement)
        project_id = str(self.project()["project_id"])
        payload_json = canonical_json(validated.model_dump(mode="json"))
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

        def intent(value: ReviewBriefAcknowledgement) -> dict[str, Any]:
            return value.model_dump(mode="json", exclude={"acknowledged_at"})

        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM eco_review_brief_drafts WHERE draft_id=? AND project_id=?",
                (validated.draft_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(validated.draft_id)
            draft = self.review_brief_draft_record(validated.draft_id)["draft"]
            if (
                draft.status != "UNCONFIRMED"
                or draft.model_revision_id != current_model_revision_id
                or draft.decision_board_sha256 != current_decision_board_sha256
            ):
                raise ValueError("a stale review brief cannot be acknowledged")
            existing = connection.execute(
                "SELECT * FROM eco_review_brief_attestations WHERE draft_id=?",
                (validated.draft_id,),
            ).fetchone()
            if existing is not None:
                retained = self.review_brief_acknowledgement(validated.draft_id)
                if retained is None or intent(retained) != intent(validated):
                    raise ValueError("review brief was already acknowledged differently")
                return retained
            identity_collision = connection.execute(
                "SELECT draft_id FROM eco_review_brief_attestations WHERE acknowledgement_id=?",
                (validated.acknowledgement_id,),
            ).fetchone()
            if identity_collision is not None:
                raise ValueError("review brief acknowledgement identity belongs to another draft")
            connection.execute(
                "INSERT INTO eco_review_brief_attestations("
                "acknowledgement_id,draft_id,project_id,payload_json,payload_sha256,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    validated.acknowledgement_id,
                    validated.draft_id,
                    project_id,
                    payload_json,
                    payload_sha256,
                    validated.acknowledged_at.isoformat(),
                ),
            )
        retained = self.review_brief_acknowledgement(validated.draft_id)
        if retained is None or intent(retained) != intent(validated):
            raise ValueError("review brief acknowledgement failed authoritative readback")
        return retained

    def review_brief_acknowledgement(self, draft_id: str) -> Any | None:
        from .review_brief import ReviewBriefAcknowledgement

        project_id = str(self.project()["project_id"])
        self.review_brief_draft_record(draft_id)
        row = self._connection.execute(
            "SELECT acknowledgement_id,draft_id,project_id,payload_json,payload_sha256,created_at "
            "FROM eco_review_brief_attestations "
            "WHERE draft_id=? AND project_id=?",
            (draft_id, project_id),
        ).fetchone()
        if row is None:
            return None
        payload_json = str(row["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(row["payload_sha256"]):
            raise ValueError("review brief acknowledgement failed its local checksum")
        acknowledgement = ReviewBriefAcknowledgement.model_validate_json(payload_json)
        if (
            acknowledgement.acknowledgement_id != str(row["acknowledgement_id"])
            or acknowledgement.draft_id != str(row["draft_id"])
            or acknowledgement.draft_id != draft_id
            or str(row["project_id"]) != project_id
            or acknowledgement.acknowledged_at.isoformat() != str(row["created_at"])
        ):
            raise ValueError("review brief acknowledgement identity is inconsistent")
        return acknowledgement

    def record_review_brief_feedback(self, feedback: Any) -> Any:
        from .review_brief import ReviewBriefFeedbackRecord

        validated = ReviewBriefFeedbackRecord.model_validate(feedback)
        project_id = str(self.project()["project_id"])
        payload_json = canonical_json(validated.model_dump(mode="json"))
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            draft_row = connection.execute(
                "SELECT * FROM eco_review_brief_drafts WHERE draft_id=? AND project_id=?",
                (validated.draft_id, project_id),
            ).fetchone()
            if draft_row is None:
                raise KeyError(validated.draft_id)
            draft = self.review_brief_draft_record(validated.draft_id)["draft"]
            if (
                validated.trace_receipt.trace_id != draft.trace.trace_id
                or validated.trace_receipt.weave_call_id != draft.trace.weave_call_id
                or validated.trace_receipt.wandb_run_id != draft.trace.wandb_run_id
            ):
                raise ValueError("review brief feedback receipt differs from the draft trace")
            existing = connection.execute(
                "SELECT * FROM eco_review_brief_feedback WHERE feedback_id=?",
                (validated.feedback_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["draft_id"]) != validated.draft_id
                    or str(existing["payload_json"]) != payload_json
                    or str(existing["payload_sha256"]) != payload_sha256
                ):
                    raise ValueError("review brief feedback identity was reused")
                return validated
            connection.execute(
                "INSERT INTO eco_review_brief_feedback("
                "feedback_id,draft_id,project_id,payload_json,payload_sha256,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    validated.feedback_id,
                    validated.draft_id,
                    project_id,
                    payload_json,
                    payload_sha256,
                    validated.recorded_at.isoformat(),
                ),
            )
        return validated

    def review_brief_feedback(self, feedback_id: str) -> Any | None:
        from .review_brief import ReviewBriefFeedbackRecord

        row = self._connection.execute(
            "SELECT payload_json,payload_sha256 FROM eco_review_brief_feedback "
            "WHERE feedback_id=? AND project_id=?",
            (feedback_id, self.project()["project_id"]),
        ).fetchone()
        if row is None:
            return None
        payload_json = str(row["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(row["payload_sha256"]):
            raise ValueError("review brief feedback failed its local checksum")
        feedback = ReviewBriefFeedbackRecord.model_validate_json(payload_json)
        if feedback.feedback_id != feedback_id:
            raise ValueError("review brief feedback identity is inconsistent")
        draft = self.review_brief_draft_record(feedback.draft_id)["draft"]
        if (
            feedback.trace_receipt.trace_id != draft.trace.trace_id
            or feedback.trace_receipt.weave_call_id != draft.trace.weave_call_id
            or feedback.trace_receipt.wandb_run_id != draft.trace.wandb_run_id
        ):
            raise ValueError("review brief feedback receipt differs from the draft trace")
        return feedback

    def _validated_export_package(
        self,
        *,
        export_id: str,
        payload: dict[str, Any],
        package_files: Sequence[dict[str, Any]],
        package_root: Path,
    ) -> list[dict[str, Any]]:
        """Validate one complete package against its final project-relative inventory.

        ``package_root`` may be an unpublished staging directory or the final
        ``exports/<export_id>`` directory.  Package-file identities always use
        their final paths so a durable publication intent remains stable across
        the atomic directory rename.
        """

        if not package_files:
            raise ValueError("an evidence export requires project-contained package files")
        if not package_root.is_dir() or package_root.is_symlink():
            raise ValueError("an evidence export package root is absent or unsafe")
        normalized_files: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        expected_root = PurePosixPath("exports") / export_id
        manifest_files = payload.get("files")
        if not isinstance(manifest_files, dict) or not manifest_files:
            raise ValueError("an evidence export manifest must enumerate package evidence")
        expected_paths = {expected_root / "manifest.json"}
        for relative_name, digest in manifest_files.items():
            relative_file = PurePosixPath(str(relative_name))
            if (
                relative_file.is_absolute()
                or any(part in {"", ".", ".."} for part in relative_file.parts)
                or not isinstance(digest, str)
            ):
                raise ValueError("export manifest contains an unsafe evidence path")
            expected_paths.add(expected_root / relative_file)
        for item in package_files:
            relative = PurePosixPath(str(item.get("relative_path", "")))
            if (
                relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
                or expected_root not in relative.parents
                or relative.as_posix() in seen_paths
            ):
                raise ValueError("export package files must be unique and recursively contained")
            package_relative = relative.relative_to(expected_root)
            try:
                path = contained_path(package_root, package_relative.as_posix())
            except PathSecurityError as exc:
                raise ValueError("export package contains an unsafe evidence path") from exc
            digest = str(item.get("sha256", ""))
            byte_size = int(item.get("byte_size", -1))
            media_type = str(item.get("media_type", ""))
            if (
                not path.is_file()
                or path.is_symlink()
                or _sha256_file(path) != digest
                or path.stat().st_size != byte_size
                or not media_type
            ):
                raise ValueError("export package file differs from its committed manifest")
            if package_relative == PurePosixPath("manifest.json"):
                if json.loads(path.read_text(encoding="utf-8")) != payload:
                    raise ValueError("export package manifest differs from committed metadata")
            elif manifest_files.get(package_relative.as_posix()) != digest:
                raise ValueError("export evidence file differs from the package manifest")
            seen_paths.add(relative.as_posix())
            normalized_files.append(
                {
                    "relative_path": relative.as_posix(),
                    "sha256": digest,
                    "byte_size": byte_size,
                    "media_type": media_type,
                }
            )
        if {PurePosixPath(item) for item in seen_paths} != expected_paths:
            raise ValueError("export package files differ from the manifest inventory")
        return sorted(normalized_files, key=lambda item: item["relative_path"])

    def prepare_export_publication(
        self,
        export_id: str,
        analysis_id: str,
        payload: dict[str, Any],
        *,
        package_files: Sequence[dict[str, Any]],
        staging_relative_path: str,
    ) -> None:
        """Persist an immutable intent before a staged package becomes visible.

        Expensive file creation and hashing happen before this short transaction.
        Once this method returns, either the staged directory or its atomically
        renamed final directory can be recovered and finalized after a crash.
        """

        project_id = self.project()["project_id"]
        expected_final = PurePosixPath("exports") / export_id
        staging_relative = PurePosixPath(staging_relative_path)
        if (
            staging_relative.is_absolute()
            or staging_relative.parent != PurePosixPath("exports")
            or not staging_relative.name.startswith(f".{export_id}.")
            or any(part in {"", ".", ".."} for part in staging_relative.parts)
        ):
            raise ValueError("export staging directory identity is unsafe")
        staging = contained_path(self.root, staging_relative.as_posix())
        normalized_files = self._validated_export_package(
            export_id=export_id,
            payload=payload,
            package_files=package_files,
            package_root=staging,
        )
        if payload.get("export_id") != export_id or payload.get("project_id") != project_id:
            raise ValueError("export publication intent differs from project identity")
        intent = {
            "schema_version": "ecocorridor-export-publication-intent/v1.0",
            "export_id": export_id,
            "analysis_id": analysis_id,
            "project_id": project_id,
            "staging_relative_path": staging_relative.as_posix(),
            "final_relative_path": expected_final.as_posix(),
            "manifest": payload,
            "package_files": normalized_files,
        }
        intent_json = canonical_json(intent)
        now = _now()
        with self.transaction() as connection:
            analysis = connection.execute(
                "SELECT project_id FROM eco_analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if analysis is None or str(analysis["project_id"]) != project_id:
                raise ValueError("export publication requires a local authoritative analysis")
            completed = connection.execute(
                "SELECT project_id,analysis_id,payload_json FROM eco_exports WHERE export_id=?",
                (export_id,),
            ).fetchone()
            if completed is not None:
                if (
                    str(completed["project_id"]) != project_id
                    or str(completed["analysis_id"]) != analysis_id
                    or str(completed["payload_json"]) != canonical_json(payload)
                ):
                    raise ValueError("export ID was reused for different evidence")
                retained_intent = connection.execute(
                    "SELECT 1 FROM eco_records WHERE project_id=? "
                    "AND record_type='export_publication_intent' AND record_id=?",
                    (project_id, export_id),
                ).fetchone()
                if retained_intent is not None:
                    return
            self._put_record_in_transaction(
                connection,
                project_id=project_id,
                record_type="export_publication_intent",
                record_id=export_id,
                payload_json=intent_json,
                allow_update=False,
                now=now,
            )

    def finalize_export_publication(self, export_id: str) -> None:
        """Atomically commit one published package and its telemetry envelope."""

        project_id = self.project()["project_id"]
        intent = self.get_record("export_publication_intent", export_id)
        if (
            intent.get("schema_version") != "ecocorridor-export-publication-intent/v1.0"
            or intent.get("export_id") != export_id
            or intent.get("project_id") != project_id
        ):
            raise ValueError("export publication intent is invalid")
        analysis_id = str(intent.get("analysis_id", ""))
        payload = intent.get("manifest")
        package_files = intent.get("package_files")
        if not isinstance(payload, dict) or not isinstance(package_files, list):
            raise TypeError("export publication intent omits package evidence")
        expected_final = PurePosixPath("exports") / export_id
        if intent.get("final_relative_path") != expected_final.as_posix():
            raise ValueError("export publication intent targets an unsafe final directory")
        final_directory = contained_path(self.root, expected_final.as_posix())
        normalized_files = self._validated_export_package(
            export_id=export_id,
            payload=payload,
            package_files=package_files,
            package_root=final_directory,
        )
        payload_json = canonical_json(payload)
        with self.transaction() as connection:
            retained_intent = connection.execute(
                "SELECT payload_json FROM eco_records WHERE project_id=? "
                "AND record_type='export_publication_intent' AND record_id=?",
                (project_id, export_id),
            ).fetchone()
            if retained_intent is None or json.loads(retained_intent["payload_json"]) != intent:
                raise ValueError("export publication intent changed before finalization")
            existing = connection.execute(
                "SELECT project_id,analysis_id,payload_json FROM eco_exports WHERE export_id=?",
                (export_id,),
            ).fetchone()
            if existing is not None and (
                str(existing["project_id"]) != project_id
                or str(existing["analysis_id"]) != analysis_id
                or str(existing["payload_json"]) != payload_json
            ):
                raise ValueError("export ID was reused for different evidence")
            if existing is None:
                connection.execute(
                    "INSERT INTO eco_exports(export_id,project_id,analysis_id,payload_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (export_id, project_id, analysis_id, payload_json, _now()),
                )
            self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=analysis_id,
                event_type="evidence_export",
                subject_id=export_id,
                stream_id=f"evidence_export:{export_id}",
                payload={"manifest": payload, "package_files": normalized_files},
                idempotency_key=f"evidence_export:{export_id}",
            )

    def recover_export_publications(self) -> list[str]:
        """Finish every durable publication intent lacking an export/outbox commit."""

        project_id = self.project()["project_id"]
        rows = self._connection.execute(
            "SELECT record_id,payload_json FROM eco_records AS intent "
            "WHERE project_id=? AND record_type='export_publication_intent' "
            "AND NOT EXISTS (SELECT 1 FROM eco_exports AS exported "
            "WHERE exported.export_id=intent.record_id) ORDER BY record_id",
            (project_id,),
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            export_id = str(row["record_id"])
            intent = json.loads(row["payload_json"])
            if intent.get("export_id") != export_id:
                raise RuntimeError("export publication intent identity is corrupt")
            staging = contained_path(self.root, str(intent.get("staging_relative_path", "")))
            final = contained_path(self.root, str(intent.get("final_relative_path", "")))
            if final.exists():
                if not final.is_dir() or final.is_symlink():
                    raise RuntimeError("pending export final path is unsafe")
            elif staging.is_dir() and not staging.is_symlink():
                final.parent.mkdir(parents=True, exist_ok=True)
                os.rename(staging, final)
            else:
                raise RuntimeError(
                    f"pending export publication {export_id} has no recoverable package bytes"
                )
            self.finalize_export_publication(export_id)
            recovered.append(export_id)
        return recovered

    def store_export(
        self,
        export_id: str,
        analysis_id: str,
        payload: dict[str, Any],
        *,
        package_files: Sequence[dict[str, Any]],
    ) -> None:
        """Commit export metadata and its upload envelope as one local transaction.

        Export bytes are created beneath ``exports/<export_id>`` before this call.  The
        producer records only project-relative, hash-verified files; a later native
        "save a copy" operation may copy these bytes elsewhere but is never cloud input.
        """

        expected_root = PurePosixPath("exports") / export_id
        normalized_files = self._validated_export_package(
            export_id=export_id,
            payload=payload,
            package_files=package_files,
            package_root=contained_path(self.root, expected_root.as_posix()),
        )
        project_id = self.project()["project_id"]
        with self.transaction() as connection:
            payload_json = canonical_json(payload)
            existing = connection.execute(
                "SELECT project_id,analysis_id,payload_json FROM eco_exports WHERE export_id=?",
                (export_id,),
            ).fetchone()
            if existing is not None and (
                str(existing["project_id"]) != project_id
                or str(existing["analysis_id"]) != analysis_id
                or str(existing["payload_json"]) != payload_json
            ):
                raise ValueError("export ID was reused for different evidence")
            if existing is None:
                connection.execute(
                    "INSERT INTO eco_exports(export_id,project_id,analysis_id,payload_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (export_id, project_id, analysis_id, payload_json, _now()),
                )
            self._enqueue_outbox(
                connection,
                project_id=project_id,
                analysis_id=analysis_id,
                event_type="evidence_export",
                subject_id=export_id,
                stream_id=f"evidence_export:{export_id}",
                payload={
                    "manifest": payload,
                    "package_files": sorted(
                        normalized_files, key=lambda item: item["relative_path"]
                    ),
                },
                idempotency_key=f"evidence_export:{export_id}",
            )

    def telemetry_rows(
        self,
        *,
        event_type: str | None = None,
        analysis_id: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return committed telemetry metadata for deterministic projection/lineage."""

        project_id = self.project()["project_id"]
        clauses = ["project_id=?"]
        parameters: list[Any] = [project_id]
        for column, value in (
            ("event_type", event_type),
            ("analysis_id", analysis_id),
            ("subject_id", subject_id),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        rows = self._connection.execute(
            "SELECT * FROM eco_telemetry_outbox WHERE "
            + " AND ".join(clauses)
            + " ORDER BY outbox_sequence,outbox_id",
            parameters,
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record.pop("payload_json"))
            external = record.pop("external_reference_json")
            record["external_reference"] = json.loads(external) if external else None
            records.append(record)
        return records

    def pending_outbox(self) -> list[dict[str, Any]]:
        project_id = self.project()["project_id"]
        records: list[dict[str, Any]] = []
        for row in self._connection.execute(
            "SELECT * FROM eco_telemetry_outbox WHERE project_id=? "
            "AND state IN ('PENDING','FAILED') ORDER BY outbox_sequence,outbox_id",
            (project_id,),
        ):
            record = dict(row)
            record["payload"] = json.loads(record.pop("payload_json"))
            records.append(record)
        return records

    def recover_expired_outbox_leases(self, *, now: datetime | None = None) -> int:
        """Release only expired cloud-upload leases after a crash or timeout."""

        cutoff = (now or datetime.now(UTC)).isoformat()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE eco_telemetry_outbox SET state='PENDING',lease_token=NULL,"
                "lease_expires_at=NULL,last_error_code='UPLOAD_LEASE_EXPIRED',updated_at=? "
                "WHERE state='UPLOADING' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at<=?",
                (cutoff, cutoff),
            )
            return int(cursor.rowcount)

    def requeue_blocked_outbox_after_revalidation(
        self,
        outbox_ids: Sequence[str],
        *,
        revalidation_sha256: str,
    ) -> list[str]:
        """Requeue selected blocked envelopes after caller policy revalidation.

        Policy and evidence repair remain caller-owned decisions. The store
        requires their digest and atomically records each row's prior terminal
        error before returning it to ``PENDING``. ``last_error_code`` remains
        visible until a later upload succeeds or records a new failure; the
        immutable audit survives either outcome.
        """

        normalized = [str(outbox_id) for outbox_id in outbox_ids]
        if (
            not normalized
            or len(normalized) > 4_096
            or len(set(normalized)) != len(normalized)
            or any(not outbox_id for outbox_id in normalized)
        ):
            raise ValueError("blocked outbox requeue requires unique non-empty row IDs")
        if not re.fullmatch(r"[0-9a-f]{64}", revalidation_sha256):
            raise ValueError("blocked outbox requeue requires a lowercase SHA-256 revalidation")

        project_id = self.project()["project_id"]
        placeholders = ",".join("?" for _ in normalized)
        requeued_at = _now()
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT outbox_id,last_error_code,attempt_count FROM eco_telemetry_outbox "
                "WHERE project_id=? AND outbox_id IN ("
                + placeholders
                + ") AND state='BLOCKED' ORDER BY outbox_sequence,outbox_id",
                [project_id, *normalized],
            ).fetchall()
            if len(rows) != len(normalized):
                raise ValueError(
                    "only explicitly selected blocked rows in this project may be requeued"
                )
            ordered_ids = [str(row["outbox_id"]) for row in rows]
            for row in rows:
                error_code = str(row["last_error_code"] or "")
                attempt_count = int(row["attempt_count"])
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", error_code) or attempt_count < 1:
                    raise ValueError("blocked outbox row has no auditable terminal failure")
                audit_payload = {
                    "schema_version": "ecocorridor-outbox-requeue-audit/v1.0",
                    "project_id": project_id,
                    "outbox_id": str(row["outbox_id"]),
                    "blocked_error_code": error_code,
                    "blocked_attempt_count": attempt_count,
                    "revalidation_sha256": revalidation_sha256,
                    "requeued_at": requeued_at,
                }
                audit_id = (
                    "outbox_requeue_"
                    + hashlib.sha256(canonical_json(audit_payload).encode("utf-8")).hexdigest()
                )
                connection.execute(
                    "INSERT INTO eco_telemetry_outbox_requeue_audit("
                    "audit_id,project_id,outbox_id,blocked_error_code,blocked_attempt_count,"
                    "revalidation_sha256,requeued_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        audit_id,
                        project_id,
                        row["outbox_id"],
                        error_code,
                        attempt_count,
                        revalidation_sha256,
                        requeued_at,
                    ),
                )
            cursor = connection.execute(
                "UPDATE eco_telemetry_outbox SET state='PENDING',lease_token=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE project_id=? AND state='BLOCKED' "
                "AND outbox_id IN (" + placeholders + ")",
                [requeued_at, project_id, *ordered_ids],
            )
            if cursor.rowcount != len(ordered_ids):
                raise RuntimeError("atomic blocked outbox requeue was incomplete")
        return ordered_ids

    def outbox_requeue_audits(self, *, outbox_id: str | None = None) -> list[dict[str, Any]]:
        """Return immutable blocked-row revalidation boundaries for diagnostics."""

        project_id = self.project()["project_id"]
        if outbox_id is None:
            rows = self._connection.execute(
                "SELECT * FROM eco_telemetry_outbox_requeue_audit WHERE project_id=? "
                "ORDER BY requeued_at,audit_id",
                (project_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM eco_telemetry_outbox_requeue_audit WHERE project_id=? "
                "AND outbox_id=? ORDER BY requeued_at,audit_id",
                (project_id, outbox_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_outbox(self, *, limit: int = 100, lease_seconds: int = 300) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("outbox claim limit must be positive")
        if lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("outbox lease must be between 1 second and 24 hours")
        project_id = self.project()["project_id"]
        with self.transaction() as connection:
            claimed_at = datetime.now(UTC)
            now = claimed_at.isoformat()
            connection.execute(
                "UPDATE eco_telemetry_outbox SET state='PENDING',lease_token=NULL,"
                "lease_expires_at=NULL,last_error_code='UPLOAD_LEASE_EXPIRED',updated_at=? "
                "WHERE project_id=? AND state='UPLOADING' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at<=?",
                (now, project_id, now),
            )
            rows = connection.execute(
                "SELECT * FROM eco_telemetry_outbox WHERE project_id=? "
                "AND state IN ('PENDING','FAILED') AND NOT EXISTS ("
                "SELECT 1 FROM eco_telemetry_outbox AS prior "
                "WHERE prior.project_id=eco_telemetry_outbox.project_id "
                "AND prior.stream_id=eco_telemetry_outbox.stream_id "
                "AND prior.stream_sequence<eco_telemetry_outbox.stream_sequence "
                "AND prior.state IN ('UPLOADING','BLOCKED')) "
                "ORDER BY outbox_sequence,outbox_id LIMIT ?",
                (project_id, limit),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                lease_token = secrets.token_urlsafe(24)
                lease_expires_at = (claimed_at + timedelta(seconds=lease_seconds)).isoformat()
                cursor = connection.execute(
                    "UPDATE eco_telemetry_outbox SET state='UPLOADING',attempt_count=attempt_count+1,"
                    "lease_token=?,lease_expires_at=?,updated_at=? WHERE outbox_id=? "
                    "AND state IN ('PENDING','FAILED')",
                    (lease_token, lease_expires_at, now, row["outbox_id"]),
                )
                if cursor.rowcount == 1:
                    claimed.append(
                        {
                            **dict(row),
                            "state": "UPLOADING",
                            "attempt_count": int(row["attempt_count"]) + 1,
                            "lease_token": lease_token,
                            "lease_expires_at": lease_expires_at,
                            "updated_at": now,
                        }
                    )
        return claimed

    def claim_authorized_outbox(
        self,
        *,
        authorized_outbox_ids: Sequence[str],
        limit: int = 100,
        lease_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        """Claim only members present in one exact sync-inventory snapshot.

        Rows committed after that snapshot remain pending for the next preview.
        An unauthorized earlier member also prevents later members in its
        ordered producer stream from being claimed under stale authority.
        """

        if limit < 1:
            raise ValueError("outbox claim limit must be positive")
        if lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("outbox lease must be between 1 second and 24 hours")
        normalized = tuple(str(item) for item in authorized_outbox_ids)
        if len(set(normalized)) != len(normalized) or any(not item for item in normalized):
            raise ValueError("authorized outbox identities must be unique and non-empty")
        authorized = set(normalized)
        if not authorized:
            return []

        project_id = self.project()["project_id"]
        with self.transaction() as connection:
            claimed_at = datetime.now(UTC)
            now = claimed_at.isoformat()
            connection.execute(
                "UPDATE eco_telemetry_outbox SET state='PENDING',lease_token=NULL,"
                "lease_expires_at=NULL,last_error_code='UPLOAD_LEASE_EXPIRED',updated_at=? "
                "WHERE project_id=? AND state='UPLOADING' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at<=?",
                (now, project_id, now),
            )
            rows = connection.execute(
                "SELECT * FROM eco_telemetry_outbox WHERE project_id=? "
                "AND state IN ('PENDING','FAILED') AND NOT EXISTS ("
                "SELECT 1 FROM eco_telemetry_outbox AS prior "
                "WHERE prior.project_id=eco_telemetry_outbox.project_id "
                "AND prior.stream_id=eco_telemetry_outbox.stream_id "
                "AND prior.stream_sequence<eco_telemetry_outbox.stream_sequence "
                "AND prior.state IN ('UPLOADING','BLOCKED')) "
                "ORDER BY outbox_sequence,outbox_id",
                (project_id,),
            ).fetchall()
            selected: list[sqlite3.Row] = []
            unauthorized_streams: set[str] = set()
            for row in rows:
                stream_id = str(row["stream_id"])
                if stream_id in unauthorized_streams:
                    continue
                if str(row["outbox_id"]) not in authorized:
                    unauthorized_streams.add(stream_id)
                    continue
                selected.append(row)
                if len(selected) >= limit:
                    break

            claimed: list[dict[str, Any]] = []
            for row in selected:
                lease_token = secrets.token_urlsafe(24)
                lease_expires_at = (claimed_at + timedelta(seconds=lease_seconds)).isoformat()
                cursor = connection.execute(
                    "UPDATE eco_telemetry_outbox SET state='UPLOADING',"
                    "attempt_count=attempt_count+1,lease_token=?,lease_expires_at=?,updated_at=? "
                    "WHERE outbox_id=? AND state IN ('PENDING','FAILED')",
                    (lease_token, lease_expires_at, now, row["outbox_id"]),
                )
                if cursor.rowcount == 1:
                    claimed.append(
                        {
                            **dict(row),
                            "state": "UPLOADING",
                            "attempt_count": int(row["attempt_count"]) + 1,
                            "lease_token": lease_token,
                            "lease_expires_at": lease_expires_at,
                            "updated_at": now,
                        }
                    )
        return claimed

    def release_outbox(
        self,
        outbox_id: str,
        *,
        lease_token: str | None = None,
    ) -> None:
        """Return an unattempted claimed row to pending without counting an attempt."""

        with self.transaction() as connection:
            clause = "outbox_id=? AND state='UPLOADING'"
            parameters: list[Any] = [_now(), outbox_id]
            if lease_token is not None:
                clause += " AND lease_token=?"
                parameters.append(lease_token)
            cursor = connection.execute(
                "UPDATE eco_telemetry_outbox SET state='PENDING',"
                "attempt_count=attempt_count-1,lease_token=NULL,lease_expires_at=NULL,"
                "updated_at=? WHERE " + clause + " AND attempt_count>0",
                parameters,
            )
            if cursor.rowcount != 1:
                raise ValueError("only the active outbox lease may be released")

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
            cursor = connection.execute(
                "UPDATE eco_telemetry_outbox SET state='SYNCED',external_reference_json=?,"
                "last_error_code=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE "
                + clause
                + " AND NOT EXISTS (SELECT 1 FROM eco_telemetry_outbox AS prior "
                "WHERE prior.project_id=eco_telemetry_outbox.project_id "
                "AND prior.stream_id=eco_telemetry_outbox.stream_id "
                "AND prior.stream_sequence<eco_telemetry_outbox.stream_sequence "
                "AND prior.state!='SYNCED')",
                parameters,
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "only the active outbox lease at the next monotonic sequence may be marked synced"
                )

    @staticmethod
    def _validate_claimed_outbox_batch(
        connection: sqlite3.Connection,
        members: Sequence[tuple[str, str]],
        *,
        require_next_sequence: bool,
    ) -> list[sqlite3.Row]:
        normalized = [(str(outbox_id), str(lease_token)) for outbox_id, lease_token in members]
        if not 2 <= len(normalized) <= 128:
            raise ValueError("outbox batch must contain between 2 and 128 rows")
        outbox_ids = [item[0] for item in normalized]
        if len(set(outbox_ids)) != len(outbox_ids) or any(
            not outbox_id or not lease_token for outbox_id, lease_token in normalized
        ):
            raise ValueError("outbox batch members and active leases must be unique and non-empty")
        placeholders = ",".join("?" for _ in outbox_ids)
        rows = connection.execute(
            "SELECT * FROM eco_telemetry_outbox WHERE outbox_id IN ("
            + placeholders
            + ") ORDER BY stream_sequence,outbox_id",
            outbox_ids,
        ).fetchall()
        if len(rows) != len(normalized):
            raise ValueError("outbox batch contains an unknown row")
        ordered_ids = [str(row["outbox_id"]) for row in rows]
        if ordered_ids != outbox_ids:
            raise ValueError("outbox batch members must follow their committed stream sequence")
        expected_tokens = dict(normalized)
        if any(
            row["state"] != "UPLOADING"
            or str(row["lease_token"] or "") != expected_tokens[str(row["outbox_id"])]
            for row in rows
        ):
            raise ValueError("only active outbox batch leases may finish")
        identity = {
            (row["project_id"], row["analysis_id"], row["stream_id"], row["event_type"])
            for row in rows
        }
        sequences = [int(row["stream_sequence"]) for row in rows]
        attempts = {int(row["attempt_count"]) for row in rows}
        if (
            len(identity) != 1
            or len(attempts) != 1
            or sequences != list(range(sequences[0], sequences[0] + len(sequences)))
        ):
            raise ValueError("outbox batch rows must be consecutive in one producer stream")
        if require_next_sequence:
            first = rows[0]
            prior = connection.execute(
                "SELECT 1 FROM eco_telemetry_outbox WHERE project_id=? AND stream_id=? "
                "AND stream_sequence<? AND state!='SYNCED' LIMIT 1",
                (first["project_id"], first["stream_id"], first["stream_sequence"]),
            ).fetchone()
            if prior is not None:
                raise ValueError("outbox batch is not the next monotonic stream sequence")
        return rows

    def mark_outbox_batch_synced(
        self,
        members: Sequence[tuple[str, str]],
        external_reference: dict[str, Any],
    ) -> None:
        """Acknowledge one provider envelope for all of its rows atomically."""

        external_json = canonical_json(external_reference)
        with self.transaction() as connection:
            rows = self._validate_claimed_outbox_batch(
                connection,
                members,
                require_next_sequence=True,
            )
            outbox_ids = [str(row["outbox_id"]) for row in rows]
            placeholders = ",".join("?" for _ in outbox_ids)
            cursor = connection.execute(
                "UPDATE eco_telemetry_outbox SET state='SYNCED',external_reference_json=?,"
                "last_error_code=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE state='UPLOADING' AND outbox_id IN (" + placeholders + ")",
                [external_json, _now(), *outbox_ids],
            )
            if cursor.rowcount != len(outbox_ids):
                raise RuntimeError("atomic outbox batch acknowledgement was incomplete")

    def mark_outbox_batch_failed(
        self,
        members: Sequence[tuple[str, str]],
        error_code: str,
        *,
        blocked: bool = False,
    ) -> None:
        """Fail one provider envelope without leaving member leases split."""

        if not error_code:
            raise ValueError("outbox batch failure requires a stable error code")
        with self.transaction() as connection:
            rows = self._validate_claimed_outbox_batch(
                connection,
                members,
                require_next_sequence=False,
            )
            outbox_ids = [str(row["outbox_id"]) for row in rows]
            placeholders = ",".join("?" for _ in outbox_ids)
            cursor = connection.execute(
                "UPDATE eco_telemetry_outbox SET state=?,last_error_code=?,lease_token=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE state='UPLOADING' AND outbox_id IN ("
                + placeholders
                + ")",
                ["BLOCKED" if blocked else "FAILED", error_code, _now(), *outbox_ids],
            )
            if cursor.rowcount != len(outbox_ids):
                raise RuntimeError("atomic outbox batch failure was incomplete")

    def mark_outbox_failed(
        self,
        outbox_id: str,
        error_code: str,
        *,
        blocked: bool = False,
        lease_token: str | None = None,
    ) -> None:
        if not error_code:
            raise ValueError("outbox failure requires a stable error code")
        state = "BLOCKED" if blocked else "FAILED"
        with self.transaction() as connection:
            clause = "outbox_id=? AND state='UPLOADING'"
            parameters: list[Any] = [state, error_code, _now(), outbox_id]
            if lease_token is not None:
                clause += " AND lease_token=?"
                parameters.append(lease_token)
            cursor = connection.execute(
                "UPDATE eco_telemetry_outbox SET state=?,last_error_code=?,lease_token=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE " + clause,
                parameters,
            )
            if cursor.rowcount != 1:
                raise ValueError("only the active outbox lease may be marked failed")
