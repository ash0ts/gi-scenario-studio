from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ecocorridor_core.observability import ECOCORRIDOR_APP_PROFILE
from ecocorridor_core.project_store import EcoProjectStore
from scenario_platform.cloud_sync import (
    ArtifactProjection,
    CloudIntegrityError,
    CloudTransportError,
    CloudUploadReceipt,
    DurableOutboxConsumer,
    OutboxBatchPolicy,
    TableProjection,
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


def _seed_terminal_events(root: Path, count: int) -> EcoProjectStore:
    store = EcoProjectStore(root)
    store.initialize(
        project_id="ridgeway-batching",
        title="Ridgeway batching fixture",
        mode="GUIDED",
        skeleton={"synthetic": True},
    )
    with store.transaction() as connection:
        for sequence in range(1, count + 1):
            direct_job_id = f"ridgeway-direct-{sequence:04d}"
            store._enqueue_outbox(
                connection,
                project_id="ridgeway-batching",
                analysis_id=None,
                event_type="direct_job_terminal",
                subject_id=direct_job_id,
                stream_id="connectivity_analysis:ridgeway-study",
                payload={
                    "direct_job_id": direct_job_id,
                    "state": "SUCCEEDED",
                    "sequence": sequence,
                },
                idempotency_key=f"direct_job_terminal:{direct_job_id}",
            )
    return store


def _project_terminal(row: dict[str, Any], payload: dict[str, Any]) -> TelemetryProjection:
    sequence = int(row["stream_sequence"])
    return TelemetryProjection(
        profile_namespace="ecocorridor",
        project_id=str(row["project_id"]),
        run_kind="connectivity_analysis",
        run_subject_id="ridgeway-study",
        event_type="direct_job_terminal",
        event_sequence=sequence,
        config={"engine_manifest_sha256": "a" * 64},
        history={"terminal_job_count": sequence},
        summary={"last_terminal_job_id": payload["direct_job_id"]},
        tables=[
            TableProjection(
                logical_name="solver_jobs",
                columns=["direct_job_id", "terminal_sequence", "state"],
                rows=[[payload["direct_job_id"], sequence, payload["state"]]],
            )
        ],
    )


class _RecordingUploader:
    def __init__(self, *, fail_first: bool = False, block_every_upload: bool = False) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.fail_first = fail_first
        self.block_every_upload = block_every_upload

    def verify_target(self, target: WandbTarget) -> ProjectScopeEvidence:
        return target.scope_evidence

    def upload(self, **values: Any) -> CloudUploadReceipt:
        self.uploads.append(values)
        if self.block_every_upload:
            raise CloudIntegrityError("SIMULATED_IRREPARABLE_INTEGRITY_FAILURE")
        if self.fail_first:
            self.fail_first = False
            raise CloudTransportError("SIMULATED_BATCH_UPLOAD_FAILURE")
        return CloudUploadReceipt(
            run_id="ecocorridor-connectivity-analysis-ridgeway-study",
            run_url="https://wandb.example/run/ridgeway-study",
            envelope_id=str(values["envelope_id"]),
        )


def _consumer(
    root: Path,
    store: EcoProjectStore,
    uploader: _RecordingUploader,
    *,
    max_rows: int = 128,
    projector: Callable[[dict[str, Any], dict[str, Any]], TelemetryProjection] | None = None,
) -> DurableOutboxConsumer:
    return DurableOutboxConsumer(
        repository=store,
        uploader=uploader,
        profile=ECOCORRIDOR_APP_PROFILE,
        target=_target(),
        project_root=root,
        projector=projector or _project_terminal,
        batch_policy=OutboxBatchPolicy(
            event_type="direct_job_terminal",
            max_rows=max_rows,
        ),
    )


def test_single_event_over_artifact_batch_limit_remains_complete_and_uncombined(
    tmp_path: Path,
) -> None:
    root = tmp_path / "oversized-single"
    store = _seed_terminal_events(root, 2)
    uploader = _RecordingUploader()

    def oversized_first(row: dict[str, Any], payload: dict[str, Any]) -> TelemetryProjection:
        projection = _project_terminal(row, payload)
        size = 513 * 1024 * 1024 if int(row["stream_sequence"]) == 1 else 1
        digest = f"{int(row['stream_sequence']):064x}"
        value = projection.model_dump(mode="json")
        value["artifacts"] = [
            ArtifactProjection(
                local_artifact_id=f"direct:{payload['direct_job_id']}:result",
                artifact_type="direct_run_batch",
                logical_id=f"ridgeway-study:{payload['direct_job_id']}:result",
                logical_name=f"runs/{payload['direct_job_id']}/result.dat",
                source_relative_path=f"blobs/sha256/{digest}",
                sha256=digest,
                bytes=size,
                media_type="application/octet-stream",
            ).model_dump(mode="json")
        ]
        return TelemetryProjection.model_validate(value)

    report = _consumer(root, store, uploader, projector=oversized_first).drain(
        online=True,
        limit=1,
    )

    assert (report.claimed, report.synced, report.failed, report.blocked) == (2, 2, 0, 0)
    assert len(uploader.uploads) == 2
    first, second = (item["projection"] for item in uploader.uploads)
    assert first.artifacts[0].bytes == 513 * 1024 * 1024
    assert first.history["outbox_batch_event_count"] == 1
    assert second.history["outbox_batch_event_count"] == 1
    assert [item.tables[0].rows[0][0] for item in (first, second)] == [
        "ridgeway-direct-0001",
        "ridgeway-direct-0002",
    ]
    store.close()


def test_ridgeway_1596_terminal_events_emit_thirteen_bounded_envelopes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ridgeway-scale"
    store = _seed_terminal_events(root, 1_596)
    uploader = _RecordingUploader()
    consumer = _consumer(root, store, uploader)

    report = consumer.drain(online=True)

    expected_envelopes = math.ceil(1_596 / 128)
    assert expected_envelopes == 13
    assert (report.claimed, report.synced, report.failed, report.blocked) == (
        1_596,
        1_596,
        0,
        0,
    )
    assert len(uploader.uploads) == expected_envelopes
    projected_job_ids: list[str] = []
    for upload in uploader.uploads:
        projection: TelemetryProjection = upload["projection"]
        solver_jobs = next(
            table for table in projection.tables if table.logical_name == "solver_jobs"
        )
        assert 1 <= len(solver_jobs.rows) <= 128
        assert projection.history["outbox_batch_event_count"] == len(solver_jobs.rows)
        assert projection.summary["outbox_batch_event_count"] == len(solver_jobs.rows)
        assert projection.event_sequence == solver_jobs.rows[-1][1]
        projected_job_ids.extend(str(row[0]) for row in solver_jobs.rows)
    assert projected_job_ids == [f"ridgeway-direct-{item:04d}" for item in range(1, 1_597)]

    rows = store.telemetry_rows(event_type="direct_job_terminal")
    assert all(row["state"] == "SYNCED" for row in rows)
    assert len({row["external_reference"]["envelope_id"] for row in rows}) == expected_envelopes
    store.close()


def test_batch_acknowledgement_rolls_back_all_rows_on_sql_failure(tmp_path: Path) -> None:
    root = tmp_path / "atomic-batch"
    store = _seed_terminal_events(root, 3)
    claimed = store.claim_outbox(limit=3)
    members = tuple((row["outbox_id"], row["lease_token"]) for row in claimed)
    failed_id = claimed[1]["outbox_id"]
    store._connection.execute(
        "CREATE TRIGGER fail_second_batch_ack BEFORE UPDATE OF state "
        "ON eco_telemetry_outbox WHEN NEW.state='SYNCED' AND NEW.outbox_id='"
        + failed_id
        + "' BEGIN SELECT RAISE(ABORT, 'injected batch failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected batch failure"):
        store.mark_outbox_batch_synced(
            members,
            {"run_id": "ridgeway-study", "envelope_id": "outbox_batch_atomic"},
        )
    assert {row["state"] for row in store.telemetry_rows()} == {"UPLOADING"}

    store._connection.execute("DROP TRIGGER fail_second_batch_ack")
    store.mark_outbox_batch_synced(
        members,
        {"run_id": "ridgeway-study", "envelope_id": "outbox_batch_atomic"},
    )
    rows = store.telemetry_rows()
    assert {row["state"] for row in rows} == {"SYNCED"}
    assert {row["external_reference"]["envelope_id"] for row in rows} == {"outbox_batch_atomic"}
    store.close()


def test_failed_batch_retries_with_same_identity_and_does_not_absorb_new_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "retry-batch"
    store = _seed_terminal_events(root, 5)
    uploader = _RecordingUploader(fail_first=True)
    consumer = _consumer(root, store, uploader, max_rows=3)

    first = consumer.drain(online=True, limit=1)
    assert (first.claimed, first.failed, first.synced) == (3, 3, 0)
    failed_envelope_id = uploader.uploads[0]["envelope_id"]
    assert [row["state"] for row in store.telemetry_rows()] == [
        "FAILED",
        "FAILED",
        "FAILED",
        "PENDING",
        "PENDING",
    ]

    second = consumer.drain(online=True, limit=2)
    assert (second.claimed, second.failed, second.synced) == (5, 0, 5)
    assert uploader.uploads[1]["envelope_id"] == failed_envelope_id
    assert len(uploader.uploads[1]["projection"].tables[0].rows) == 3
    assert len(uploader.uploads[2]["projection"].tables[0].rows) == 2
    assert all(row["state"] == "SYNCED" for row in store.telemetry_rows())
    store.close()


def test_blocked_batch_requeues_after_revalidation_and_preserves_failure_audit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "revalidated-batch"
    store = _seed_terminal_events(root, 3)
    claimed = store.claim_outbox(limit=3)
    members = tuple((row["outbox_id"], row["lease_token"]) for row in claimed)
    store.mark_outbox_batch_failed(
        members,
        "CLOUD_ASSET_AUTHORIZATION_BLOCKED",
        blocked=True,
    )
    blocked_ids = [row["outbox_id"] for row in claimed]
    revalidation_sha256 = "b" * 64

    requeued = store.requeue_blocked_outbox_after_revalidation(
        blocked_ids,
        revalidation_sha256=revalidation_sha256,
    )
    assert requeued == blocked_ids
    assert {row["state"] for row in store.telemetry_rows()} == {"PENDING"}
    assert {
        (audit["outbox_id"], audit["blocked_error_code"], audit["revalidation_sha256"])
        for audit in store.outbox_requeue_audits()
    } == {
        (outbox_id, "CLOUD_ASSET_AUTHORIZATION_BLOCKED", revalidation_sha256)
        for outbox_id in blocked_ids
    }

    uploader = _RecordingUploader()
    report = _consumer(root, store, uploader).drain(online=True)
    assert (report.synced, report.failed, report.blocked) == (3, 0, 0)
    assert {row["state"] for row in store.telemetry_rows()} == {"SYNCED"}
    assert len(store.outbox_requeue_audits()) == 3
    with pytest.raises(sqlite3.IntegrityError, match="requeue audit is immutable"):
        store._connection.execute(
            "DELETE FROM eco_telemetry_outbox_requeue_audit WHERE outbox_id=?",
            (blocked_ids[0],),
        )
    store.close()


def test_requeued_irreparable_batch_reblocks_without_losing_prior_audit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "irreparable-batch"
    store = _seed_terminal_events(root, 2)
    claimed = store.claim_outbox(limit=2)
    members = tuple((row["outbox_id"], row["lease_token"]) for row in claimed)
    store.mark_outbox_batch_failed(members, "CLOUD_INTEGRITY_BLOCKED", blocked=True)
    blocked_ids = [row["outbox_id"] for row in claimed]
    store.requeue_blocked_outbox_after_revalidation(
        blocked_ids,
        revalidation_sha256="c" * 64,
    )

    uploader = _RecordingUploader(block_every_upload=True)
    report = _consumer(root, store, uploader).drain(online=True)
    assert (report.synced, report.failed, report.blocked) == (0, 0, 2)
    rows = store.telemetry_rows()
    assert {row["state"] for row in rows} == {"BLOCKED"}
    assert {row["last_error_code"] for row in rows} == {"SIMULATED_IRREPARABLE_INTEGRITY_FAILURE"}
    audits = store.outbox_requeue_audits()
    assert len(audits) == 2
    assert {audit["blocked_error_code"] for audit in audits} == {"CLOUD_INTEGRITY_BLOCKED"}
    store.close()
