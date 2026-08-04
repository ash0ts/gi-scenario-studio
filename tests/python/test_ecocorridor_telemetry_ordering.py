from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from ecocorridor_core.contracts import ConnectivityRunPlan
from ecocorridor_core.observability import ECOCORRIDOR_APP_PROFILE
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.telemetry import EcoTelemetryProjector
from scenario_platform.cloud_sync import (
    CloudTransportError,
    CloudUploadReceipt,
    DurableOutboxConsumer,
    TelemetryProjection,
    WandbTarget,
)
from scenario_platform.observability import (
    ProjectScope,
    ProjectScopeEvidence,
    ScopeEvidenceKind,
)


def _target() -> WandbTarget:
    scope = ProjectScopeEvidence(
        entity="wildlife-team",
        project="restricted-connectivity",
        external_project_id="restricted-project-id",
        deployment="DEDICATED",
        api_base_url="https://api.wandb.example",
        scope=ProjectScope.RESTRICTED,
        evidence_kind=ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
        provider_field="documented_admin.visibility_scope",
    )
    return WandbTarget(
        entity=scope.entity,
        project=scope.project,
        external_project_id=scope.external_project_id,
        deployment=scope.deployment,
        api_base_url=scope.api_base_url,
        web_base_url="https://wandb.example",
        scope_evidence=scope,
    )


def _seed_two_running_jobs(root: Path) -> tuple[EcoProjectStore, dict[str, Any]]:
    fixture = build_ridgeway_fixture()
    bundle = fixture.bundle
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    alternative = next(
        item for item in fixture.portfolios if item.portfolio_id != baseline.portfolio_id
    )
    scenario = bundle.scenarios[0]
    plan = ConnectivityRunPlan(
        analysis_id="analysis-ordering",
        revision_id=bundle.revision.revision_id,
        scenario_ids=[scenario.scenario_id],
        portfolios=[baseline, alternative],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=2,
        solver_job_cap=2,
        search_method="ENUMERATION",
        seed=0,
        current_maps_for_portfolio_ids=[],
        engine_manifest_sha256="a" * 64,
    )
    store = EcoProjectStore(root)
    store.initialize(
        project_id=bundle.project_id,
        title=bundle.title,
        mode="PLANNING",
        skeleton={"test": True},
    )
    store.store_bundle(bundle)
    store.create_analysis(plan, bundle=bundle)
    store.start_analysis_with_direct_jobs(
        plan.analysis_id,
        [
            {
                "direct_job_id": "job-planned-first",
                "portfolio_id": baseline.portfolio_id,
                "scenario_id": scenario.scenario_id,
                "phase": "MATRIX",
                "sequence_number": 0,
            },
            {
                "direct_job_id": "job-planned-second",
                "portfolio_id": alternative.portfolio_id,
                "scenario_id": scenario.scenario_id,
                "phase": "MATRIX",
                "sequence_number": 1,
            },
        ],
    )
    assert store.claim_next_direct_job(plan.analysis_id)["direct_job_id"] == "job-planned-first"
    assert store.claim_next_direct_job(plan.analysis_id)["direct_job_id"] == "job-planned-second"
    sync_plan = {
        "allowed": True,
        "blockers": [],
        "projections": [
            {
                "source_asset_id": asset.asset_id,
                "uploaded_asset_id": asset.asset_id,
                "representation": "RAW",
                "bytes": asset.byte_size,
            }
            for asset in bundle.assets
        ],
    }
    return store, sync_plan


def _finalize_failed(
    root: Path,
    direct_job_id: str,
    *,
    wait_for: Event | None = None,
    release: Event | None = None,
) -> None:
    if wait_for is not None and not wait_for.wait(timeout=5):
        raise TimeoutError("terminal-order test barrier was not released")
    store = EcoProjectStore(root)
    try:
        store.finalize_direct_job(
            direct_job_id,
            "FAILED",
            error={"code": "TEST_TERMINAL_FAILURE"},
        )
    finally:
        store.close()
    if release is not None:
        release.set()


def test_parallel_terminal_events_use_commit_barriers_not_planned_order(tmp_path: Path) -> None:
    root = tmp_path / "parallel-order"
    store, sync_plan = _seed_two_running_jobs(root)
    store.close()

    # Force reverse terminal commit order while both worker tasks are live. This
    # makes the planned sequence unusable as a provider-history clock.
    second_committed = Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _finalize_failed,
                root,
                "job-planned-first",
                wait_for=second_committed,
            ),
            pool.submit(
                _finalize_failed,
                root,
                "job-planned-second",
                release=second_committed,
            ),
        ]
        for future in futures:
            future.result()

    store = EcoProjectStore(root)
    try:
        rows = store.telemetry_rows(
            event_type="direct_job_terminal", analysis_id="analysis-ordering"
        )
        assert [row["stream_sequence"] for row in rows] == [2, 3]
        assert [row["subject_id"] for row in rows] == [
            "job-planned-second",
            "job-planned-first",
        ]
        assert len({row["outbox_sequence"] for row in store.telemetry_rows()}) == len(
            store.telemetry_rows()
        )
        upstream = store.claim_outbox(limit=2)
        for row in upstream:
            store.mark_outbox_synced(
                row["outbox_id"],
                {
                    "run_id": "upstream",
                    "envelope_id": row["outbox_id"],
                    "artifacts": [
                        {
                            "provider_artifact_ref": (
                                "wildlife-team/restricted-connectivity/analysis-plan:v1"
                            )
                        }
                    ],
                },
                lease_token=row["lease_token"],
            )
        projector = EcoTelemetryProjector(
            store=store,
            project_root=root,
            sync_plan=sync_plan,
            profile=ECOCORRIDOR_APP_PROFILE,
        )
        projections = [projector(row, row["payload"]) for row in rows]
        assert [item.event_sequence for item in projections] == [2, 3]
        assert [item.history["failed_jobs"] for item in projections] == [1, 2]
        projected_rows = [
            next(table for table in item.tables if table.logical_name == "solver_jobs")
            for item in projections
        ]
        for expected, table in zip((2, 3), projected_rows, strict=True):
            record = dict(zip(table.columns, table.rows[0], strict=True))
            assert record["terminal_sequence"] == expected
        assert {
            dict(zip(table.columns, table.rows[0], strict=True))["planned_sequence"]
            for table in projected_rows
        } == {0, 1}
    finally:
        store.close()


class _RetryUploader:
    def __init__(self) -> None:
        self.steps: list[tuple[str, int]] = []
        self.failed_once = False

    def verify_target(self, target: WandbTarget) -> ProjectScopeEvidence:
        return target.scope_evidence

    def upload(self, **values: Any) -> CloudUploadReceipt:
        projection: TelemetryProjection = values["projection"]
        self.steps.append((projection.event_type, projection.event_sequence))
        if (
            projection.event_type == "direct_job_terminal"
            and projection.event_sequence == 2
            and not self.failed_once
        ):
            self.failed_once = True
            raise CloudTransportError("SIMULATED_PROVIDER_FAILURE")
        return CloudUploadReceipt(
            run_id=f"run-{projection.run_subject_id}",
            run_url="https://wandb.example/run/test",
            envelope_id=values["envelope_id"],
        )


def _minimal_projection(row: dict[str, Any], _payload: dict[str, Any]) -> TelemetryProjection:
    run_kind = (
        "connectivity_analysis"
        if row["event_type"] in {"analysis_plan", "direct_job_terminal", "analysis_completion"}
        else "bundle_import"
    )
    subject = str(row["analysis_id"] or row["subject_id"])
    return TelemetryProjection(
        profile_namespace="ecocorridor",
        project_id=row["project_id"],
        run_kind=run_kind,
        run_subject_id=subject,
        event_type=row["event_type"],
        event_sequence=row["stream_sequence"],
    )


def test_retry_halts_later_same_stream_and_never_regresses_provider_steps(
    tmp_path: Path,
) -> None:
    root = tmp_path / "retry-order"
    store, _ = _seed_two_running_jobs(root)
    store.finalize_direct_job(
        "job-planned-second", "FAILED", error={"code": "TEST_TERMINAL_FAILURE"}
    )
    store.finalize_direct_job(
        "job-planned-first", "FAILED", error={"code": "TEST_TERMINAL_FAILURE"}
    )
    uploader = _RetryUploader()
    consumer = DurableOutboxConsumer(
        repository=store,
        uploader=uploader,
        profile=ECOCORRIDOR_APP_PROFILE,
        target=_target(),
        project_root=root,
        projector=_minimal_projection,
    )

    first = consumer.drain(online=True, limit=20)
    assert (first.synced, first.failed) == (2, 1)
    assert uploader.steps == [
        ("bundle_import", 1),
        ("analysis_plan", 1),
        ("direct_job_terminal", 2),
    ]
    terminal = store.telemetry_rows(event_type="direct_job_terminal")
    assert [row["state"] for row in terminal] == ["FAILED", "PENDING"]
    assert [row["attempt_count"] for row in terminal] == [1, 0]

    second = consumer.drain(online=True, limit=20)
    assert (second.synced, second.failed) == (2, 0)
    analysis_steps = [
        sequence
        for event_type, sequence in uploader.steps
        if event_type in {"analysis_plan", "direct_job_terminal"}
    ]
    assert analysis_steps == [1, 2, 2, 3]
    assert analysis_steps == sorted(analysis_steps)
    assert all(row["state"] == "SYNCED" for row in store.telemetry_rows())
    store.close()


def test_crash_recovery_reuses_sequences_and_blocks_out_of_order_acknowledgement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "crash-order"
    store, _ = _seed_two_running_jobs(root)
    store.finalize_direct_job(
        "job-planned-second", "FAILED", error={"code": "TEST_TERMINAL_FAILURE"}
    )
    store.finalize_direct_job(
        "job-planned-first", "FAILED", error={"code": "TEST_TERMINAL_FAILURE"}
    )
    claimed = store.claim_outbox(limit=20, lease_seconds=1)
    for row in claimed:
        if row["event_type"] in {"bundle_import", "analysis_plan"}:
            store.mark_outbox_synced(
                row["outbox_id"],
                {"run_id": "upstream", "envelope_id": row["outbox_id"]},
                lease_token=row["lease_token"],
            )
    original_terminal = [
        (row["outbox_id"], row["stream_sequence"])
        for row in claimed
        if row["event_type"] == "direct_job_terminal"
    ]
    store.close()

    reopened = EcoProjectStore(root)
    try:
        assert reopened.recover_expired_outbox_leases(now=datetime(2099, 1, 1, tzinfo=UTC)) == 2
        retried = reopened.claim_outbox(limit=20)
        terminal = [row for row in retried if row["event_type"] == "direct_job_terminal"]
        assert [(row["outbox_id"], row["stream_sequence"]) for row in terminal] == original_terminal
        with pytest.raises(ValueError, match="next monotonic"):
            reopened.mark_outbox_synced(
                terminal[1]["outbox_id"],
                {"run_id": "later", "envelope_id": terminal[1]["outbox_id"]},
                lease_token=terminal[1]["lease_token"],
            )
        for row in terminal:
            reopened.mark_outbox_synced(
                row["outbox_id"],
                {"run_id": "retry", "envelope_id": row["outbox_id"]},
                lease_token=row["lease_token"],
            )
        assert [
            row["stream_sequence"]
            for row in reopened.telemetry_rows(event_type="direct_job_terminal")
        ] == [2, 3]
    finally:
        reopened.close()


def test_schema_109_migration_backfills_deterministic_stream_sequences(tmp_path: Path) -> None:
    root = tmp_path / "migration-order"
    root.mkdir()
    database = root / "project.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE eco_projects (
                project_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                mode TEXT NOT NULL,
                skeleton_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE eco_telemetry_outbox (
                outbox_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
                analysis_id TEXT,
                event_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                external_reference_json TEXT,
                last_error_code TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id,idempotency_key)
            );
            """
        )
        connection.execute(
            "INSERT INTO eco_projects VALUES(?,?,?,?,?,?)",
            ("migration-project", "Migration", "PLANNING", "{}", "t0", "t0"),
        )
        # Timestamps intentionally regress: migration must use durable insertion
        # order, not a wall-clock field, as the provider-history clock.
        events = [
            ("outbox-bundle", "bundle_import", "migration-project", "{}", "t2"),
            (
                "outbox-guided-open",
                "guided_session",
                "guided-session",
                '{"session_id":"guided-session"}',
                "t0",
            ),
            (
                "outbox-guided-complete",
                "guided_session",
                "guided-session",
                '{"session_id":"guided-session"}',
                "t1",
            ),
        ]
        for outbox_id, event_type, subject_id, payload_json, timestamp in events:
            connection.execute(
                "INSERT INTO eco_telemetry_outbox(outbox_id,project_id,analysis_id,event_type,"
                "subject_id,payload_json,payload_sha256,idempotency_key,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    outbox_id,
                    "migration-project",
                    None,
                    event_type,
                    subject_id,
                    payload_json,
                    hashlib.sha256(payload_json.encode()).hexdigest(),
                    outbox_id,
                    "PENDING",
                    timestamp,
                    timestamp,
                ),
            )
        connection.execute("PRAGMA user_version=108")
        connection.commit()
    finally:
        connection.close()

    migrated = EcoProjectStore(root)
    try:
        rows = migrated.telemetry_rows()
        assert [row["outbox_sequence"] for row in rows] == list(range(1, len(rows) + 1))
        assert [row["outbox_id"] for row in rows] == [
            "outbox-bundle",
            "outbox-guided-open",
            "outbox-guided-complete",
        ]
        guided_rows = [row for row in rows if row["stream_id"] == "guided_session:guided-session"]
        assert [row["stream_sequence"] for row in guided_rows] == [1, 2]
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == 112
    finally:
        migrated.close()


def test_schema_110_migration_recreates_pre_reference_event_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "migration-reference-event"
    root.mkdir()
    connection = sqlite3.connect(root / "project.sqlite")
    try:
        connection.executescript(
            """
            CREATE TABLE eco_projects (
                project_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                mode TEXT NOT NULL,
                skeleton_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE eco_telemetry_outbox (
                outbox_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES eco_projects(project_id) ON DELETE CASCADE,
                analysis_id TEXT,
                event_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                stream_sequence INTEGER NOT NULL,
                outbox_sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
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
            CREATE TRIGGER eco_outbox_event_type_insert
            BEFORE INSERT ON eco_telemetry_outbox
            WHEN NEW.event_type NOT IN (
              'bundle_import','semantic_revision','model_attestation','analysis_plan',
              'guided_session','guided_checkpoint','direct_job_terminal',
              'analysis_completion','evidence_export'
            )
            BEGIN SELECT RAISE(ABORT, 'unsupported telemetry event type'); END;
            CREATE TRIGGER eco_outbox_event_type_update
            BEFORE UPDATE OF event_type ON eco_telemetry_outbox
            WHEN NEW.event_type NOT IN (
              'bundle_import','semantic_revision','model_attestation','analysis_plan',
              'guided_session','guided_checkpoint','direct_job_terminal',
              'analysis_completion','evidence_export'
            )
            BEGIN SELECT RAISE(ABORT, 'unsupported telemetry event type'); END;
            PRAGMA user_version=109;
            """
        )
        connection.execute(
            "INSERT INTO eco_projects VALUES(?,?,?,?,?,?)",
            ("migration-project", "Migration", "PLANNING", "{}", "t0", "t0"),
        )
        connection.commit()
    finally:
        connection.close()

    migrated = EcoProjectStore(root)
    try:
        trigger_sql = {
            str(row["name"]): str(row["sql"])
            for row in migrated._connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN ('eco_outbox_event_type_insert','eco_outbox_event_type_update')"
            )
        }
        assert set(trigger_sql) == {
            "eco_outbox_event_type_insert",
            "eco_outbox_event_type_update",
        }
        assert all("reference_comparison" in value for value in trigger_sql.values())
        with migrated.transaction() as transaction:
            outbox_id = migrated._enqueue_outbox(
                transaction,
                project_id="migration-project",
                analysis_id=None,
                event_type="reference_comparison",
                subject_id="reference-result",
                stream_id="reference_comparison:analysis",
                payload={"result_id": "reference-result"},
                idempotency_key="reference-comparison:migration",
            )
        assert migrated.telemetry_rows()[0]["outbox_id"] == outbox_id
        assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == 112
    finally:
        migrated.close()
