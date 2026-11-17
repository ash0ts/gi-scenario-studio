"""Hash-bound, publication-only recovery for the completed R4c study.

The live R4c campaign completed all provider work and wrote its frozen analysis
package before W&B rejected a heterogeneous mapping in a Table cell.  This
module is deliberately unable to execute inference.  It validates the existing
campaign and republishes the original seven JSON files byte-for-byte while
projecting every Table cell to a canonical JSON string.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence_guide_v2_campaign import (
    TARGET_PROJECT,
    canonical_json,
    file_sha256,
    json_sha256,
    write_json_atomic,
)
from .evidence_guide_v2_cloud import (
    CloudReceipt,
    WandbCampaignPublisher,
    _campaign_run_id,
    _logged_artifact_reference,
    _safe,
    _wandb_run_settings,
)

STUDY_VERSION = "gi-evidence-guide-grounding-r4c-final"
RECOVERY_VERSION = "gi-evidence-guide-r4c-publication-recovery-v1"
EXPECTED_ATTEMPTS = 672
EXPECTED_TIME_BLOCKS = ("block-1", "block-2", "block-3")
EXPECTED_STAGES = ("grounding", "prose_diagnosis")
PUBLICATION_FILES = (
    "analysis.json",
    "attempt-diagnostics.json",
    "attempts.json",
    "grounding-mechanisms.json",
    "grounding-primary.json",
    "prose-cap-mcnemar.json",
    "prose-cap-primary.json",
)
TABLE_PROJECTION_VERSION = "canonical-json-string-per-cell-v1"


@dataclass(frozen=True)
class RecoveryInputs:
    campaign_root: Path
    publication_dir: Path
    preregistration_path: Path
    preregistration_receipt_path: Path
    amendment_path: Path
    package_files: Mapping[str, str]
    package_sha256: str
    analysis_file_sha256: str
    analysis_sha256: str
    study_instance_sha256: str
    preregistration_sha256: str
    preregistration_artifact_reference: str
    amendment_sha256: str
    recovery_identity_sha256: str
    table_row_counts: Mapping[str, int]


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {path.name}")
    return value


def _relative_source(root: Path, relative_path: str, expected_sha256: str) -> None:
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError("R4C_RECOVERY_SOURCE_PATH_ESCAPES_REPOSITORY") from exc
    if file_sha256(path) != expected_sha256:
        raise RuntimeError(f"R4C_RECOVERY_SOURCE_HASH_DRIFT:{relative_path}")


def _validate_embedded_source_capsule(preregistration: Mapping[str, Any]) -> None:
    capsule = preregistration.get("embedded_source_capsule")
    source_hashes = preregistration.get("source_hashes")
    if not isinstance(capsule, Mapping) or not isinstance(source_hashes, Mapping):
        raise TypeError("R4C_RECOVERY_SOURCE_CAPSULE_MISSING")
    files = capsule.get("files")
    if not isinstance(files, Mapping) or len(files) != int(capsule.get("file_count") or -1):
        raise RuntimeError("R4C_RECOVERY_SOURCE_CAPSULE_SHAPE_INVALID")
    capsule_body = {key: value for key, value in capsule.items() if key != "capsule_sha256"}
    if capsule.get("capsule_sha256") != json_sha256(capsule_body):
        raise RuntimeError("R4C_RECOVERY_SOURCE_CAPSULE_HASH_INVALID")
    if set(files) != set(source_hashes):
        raise RuntimeError("R4C_RECOVERY_SOURCE_CAPSULE_INVENTORY_DRIFT")
    for logical_name, raw_record in files.items():
        if not isinstance(raw_record, Mapping) or raw_record.get("encoding") != "base64":
            raise RuntimeError("R4C_RECOVERY_SOURCE_CAPSULE_RECORD_INVALID")
        try:
            payload = base64.b64decode(str(raw_record["content"]), validate=True)
        except (KeyError, ValueError) as exc:
            raise RuntimeError("R4C_RECOVERY_SOURCE_CAPSULE_CONTENT_INVALID") from exc
        digest = hashlib.sha256(payload).hexdigest()
        if (
            digest != raw_record.get("sha256")
            or digest != source_hashes.get(logical_name)
            or len(payload) != int(raw_record.get("utf8_bytes") or -1)
        ):
            raise RuntimeError(
                f"R4C_RECOVERY_SOURCE_CAPSULE_FILE_DRIFT:{logical_name}"
            )


def _read_campaign_rows(database_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        attempt_records = list(
            connection.execute(
                "SELECT attempt_id, stage, hidden, record_json FROM attempts "
                "ORDER BY attempt_id"
            )
        )
        ledger = list(
            connection.execute(
                "SELECT attempt_id, state, outcome_sha256 FROM attempt_execution_ledger "
                "ORDER BY attempt_id"
            )
        )
        sessions = list(
            connection.execute(
                "SELECT stage, time_block, completed_at FROM stage_sessions "
                "ORDER BY stage, time_block"
            )
        )
        receipts = {
            str(row["name"]): json.loads(row["receipt_json"])
            for row in connection.execute(
                "SELECT name, receipt_json FROM receipts ORDER BY name"
            )
        }
        context_row = connection.execute(
            "SELECT value_json FROM campaign_meta WHERE key = 'r4_execution_context'"
        ).fetchone()
        if context_row is None:
            raise RuntimeError("R4C_RECOVERY_EXECUTION_CONTEXT_MISSING")
        context = json.loads(context_row["value_json"])
    finally:
        connection.close()
    if len(attempt_records) != EXPECTED_ATTEMPTS or len(ledger) != EXPECTED_ATTEMPTS:
        raise RuntimeError("R4C_RECOVERY_ATTEMPT_COUNT_INVALID")
    rows: list[dict[str, Any]] = []
    for attempt, ledger_row in zip(attempt_records, ledger, strict=True):
        row = json.loads(attempt["record_json"])
        if (
            attempt["attempt_id"] != ledger_row["attempt_id"]
            or row.get("attempt_id") != attempt["attempt_id"]
            or ledger_row["state"] != "completed"
            or ledger_row["outcome_sha256"] != json_sha256(row)
        ):
            raise RuntimeError("R4C_RECOVERY_ATTEMPT_LEDGER_DRIFT")
        rows.append(row)
    expected_sessions = {
        (stage, block) for stage in EXPECTED_STAGES for block in EXPECTED_TIME_BLOCKS
    }
    observed_sessions = {(row["stage"], row["time_block"]) for row in sessions}
    if observed_sessions != expected_sessions or any(
        not row["completed_at"] for row in sessions
    ):
        raise RuntimeError("R4C_RECOVERY_STAGE_SESSION_INCOMPLETE")
    if set(receipts) != {f"block-{block}" for block in EXPECTED_TIME_BLOCKS}:
        raise RuntimeError("R4C_RECOVERY_BLOCK_RECEIPT_INVENTORY_INVALID")
    return rows, {"context": context, "receipts": receipts}


def _validate_block_receipts(
    campaign_root: Path,
    *,
    schedule_rows: Sequence[Mapping[str, Any]],
    database_receipts: Mapping[str, Any],
) -> None:
    for block in EXPECTED_TIME_BLOCKS:
        receipt_name = f"block-{block}"
        path = campaign_root / f"{receipt_name}-receipt.json"
        disk = _load_object(path)
        if disk != database_receipts.get(receipt_name):
            raise RuntimeError("R4C_RECOVERY_BLOCK_RECEIPT_DATABASE_DRIFT")
        body = {key: value for key, value in disk.items() if key != "receipt_sha256"}
        selected_ids = [
            str(row["attempt_id"])
            for row in schedule_rows
            if row.get("time_block") == block
        ]
        if (
            disk.get("receipt_sha256") != json_sha256(body)
            or int(disk.get("attempt_count") or 0) != 224
            or int(disk.get("attempts_completed") or 0) != 224
            or int(disk.get("attempts_reused") or 0) != 0
            or disk.get("attempt_ids_sha256") != json_sha256(selected_ids)
        ):
            raise RuntimeError(f"R4C_RECOVERY_BLOCK_RECEIPT_INVALID:{block}")


def canonical_table_cell(value: Any) -> str:
    """Return one unambiguous string type for every W&B Table cell."""

    safe_value = _safe(value)
    return canonical_json(safe_value).decode("utf-8")


def type_stable_table_projection(value: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = value.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    if any(not isinstance(row, Mapping) for row in rows):
        raise TypeError("R4C recovery Table rows must contain objects")
    columns = sorted({str(key) for row in rows for key in row})
    projected_rows = [
        [canonical_table_cell(row.get(column)) for column in columns] for row in rows
    ]
    if any(
        not isinstance(cell, str) for row in projected_rows for cell in row
    ):
        raise RuntimeError("R4C_RECOVERY_TABLE_PROJECTION_NOT_TYPE_STABLE")
    return {
        "columns": columns,
        "rows": projected_rows,
        "source_row_count": len(rows),
        "projection_version": TABLE_PROJECTION_VERSION,
        "projection_sha256": json_sha256(
            {"columns": columns, "rows": projected_rows}
        ),
    }


def validate_recovery_inputs(
    *,
    repository_root: Path,
    campaign_root: Path,
    amendment_path: Path,
) -> RecoveryInputs:
    repository_root = repository_root.resolve()
    campaign_root = campaign_root.resolve()
    amendment_path = amendment_path.resolve()
    amendment = _load_object(amendment_path)
    preregistration_path = campaign_root / "preregistration.json"
    preregistration_receipt_path = campaign_root / "preregistration-cloud-receipt.json"
    publication_dir = campaign_root / "publication"
    preregistration = _load_object(preregistration_path)
    preregistration_receipt = _load_object(preregistration_receipt_path)
    if (
        amendment.get("study_version") != STUDY_VERSION
        or amendment.get("recovery_version") != RECOVERY_VERSION
        or preregistration.get("study_version") != STUDY_VERSION
    ):
        raise RuntimeError("R4C_RECOVERY_STUDY_IDENTITY_MISMATCH")
    _validate_embedded_source_capsule(preregistration)
    frozen_sources = amendment.get("prior_frozen_sources")
    recovery_sources = amendment.get("recovery_sources")
    if not isinstance(frozen_sources, Mapping) or not isinstance(
        recovery_sources, Mapping
    ):
        raise TypeError("R4C_RECOVERY_SOURCE_SEAL_MISSING")
    for record in (*frozen_sources.values(), *recovery_sources.values()):
        if not isinstance(record, Mapping):
            raise TypeError("R4C_RECOVERY_SOURCE_SEAL_INVALID")
        _relative_source(
            repository_root,
            str(record.get("relative_path") or ""),
            str(record.get("sha256") or ""),
        )
    if preregistration.get("source_hashes", {}).get("cloud_observability") != frozen_sources[
        "cloud_publisher"
    ]["sha256"] or preregistration.get("source_hashes", {}).get("runner") != frozen_sources[
        "r4c_runner"
    ]["sha256"]:
        raise RuntimeError("R4C_RECOVERY_PRIOR_SOURCE_FREEZE_DRIFT")

    schedule_payload = _load_object(campaign_root / "attempt-schedule.json")
    schedule_rows = schedule_payload.get("rows")
    if (
        not isinstance(schedule_rows, list)
        or len(schedule_rows) != EXPECTED_ATTEMPTS
        or schedule_payload.get("schedule_sha256") != json_sha256(schedule_rows)
        or preregistration.get("schedule_sha256") != json_sha256(schedule_rows)
    ):
        raise RuntimeError("R4C_RECOVERY_SCHEDULE_DRIFT")
    campaign_rows, database = _read_campaign_rows(campaign_root / "campaign.sqlite")
    if [row.get("attempt_id") for row in campaign_rows] != sorted(
        str(row["attempt_id"]) for row in schedule_rows
    ):
        raise RuntimeError("R4C_RECOVERY_SCHEDULE_ATTEMPT_MISMATCH")
    context = database["context"]
    if (
        context.get("study_version") != STUDY_VERSION
        or context.get("study_instance_sha256")
        != preregistration.get("study_instance_sha256")
        or context.get("preregistration_sha256")
        != preregistration.get("preregistration_sha256")
        or int(context.get("attempt_count") or 0) != EXPECTED_ATTEMPTS
    ):
        raise RuntimeError("R4C_RECOVERY_EXECUTION_CONTEXT_DRIFT")
    _validate_block_receipts(
        campaign_root,
        schedule_rows=schedule_rows,
        database_receipts=database["receipts"],
    )
    if any(
        row.get("status") != "completed"
        or row.get("details", {}).get("provider_response_received") is not True
        or row.get("trace_acknowledged") is not True
        or row.get("details", {}).get("answer_released") is not False
        for row in campaign_rows
    ):
        raise RuntimeError("R4C_RECOVERY_ATTEMPT_RESPONSE_TRACE_OR_RELEASE_INVALID")

    observed_names = tuple(sorted(path.name for path in publication_dir.glob("*.json")))
    if observed_names != tuple(sorted(PUBLICATION_FILES)):
        raise RuntimeError("R4C_RECOVERY_PUBLICATION_INVENTORY_DRIFT")
    package_files = {
        name: file_sha256(publication_dir / name) for name in PUBLICATION_FILES
    }
    original = amendment.get("original_publication")
    if not isinstance(original, Mapping) or package_files != original.get("files"):
        raise RuntimeError("R4C_RECOVERY_ORIGINAL_PACKAGE_HASH_DRIFT")
    package_sha256 = json_sha256(
        {"kind": "r4c_original_seven_file_publication", "files": package_files}
    )
    if package_sha256 != original.get("package_sha256"):
        raise RuntimeError("R4C_RECOVERY_ORIGINAL_PACKAGE_IDENTITY_DRIFT")
    analysis = _load_object(publication_dir / "analysis.json")
    analysis_body = {key: value for key, value in analysis.items() if key != "analysis_sha256"}
    if (
        analysis.get("analysis_sha256") != json_sha256(analysis_body)
        or analysis.get("analysis_sha256") != original.get("analysis_sha256")
        or file_sha256(publication_dir / "analysis.json")
        != original.get("analysis_file_sha256")
        or int(analysis.get("attempt_count") or 0) != EXPECTED_ATTEMPTS
        or analysis.get("actual_answers_released") != 0
        or analysis.get("production_state") != "DISABLE_LIVE_GUIDE"
    ):
        raise RuntimeError("R4C_RECOVERY_ANALYSIS_HASH_OR_BOUNDARY_DRIFT")
    attempts_payload = _load_object(publication_dir / "attempts.json")
    if attempts_payload.get("rows") != campaign_rows:
        raise RuntimeError("R4C_RECOVERY_ATTEMPT_PACKAGE_DATABASE_DRIFT")
    execution_receipt_path = campaign_root / "execution-receipt.json"
    execution_receipt = _load_object(execution_receipt_path)
    stale_receipt = amendment.get("pre_recovery_execution_receipt")
    execution_body = {
        key: value for key, value in execution_receipt.items() if key != "receipt_sha256"
    }
    if (
        not isinstance(stale_receipt, Mapping)
        or file_sha256(execution_receipt_path) != stale_receipt.get("file_sha256")
        or execution_receipt.get("receipt_sha256") != json_sha256(execution_body)
        or execution_receipt.get("receipt_sha256")
        != stale_receipt.get("receipt_sha256")
        or execution_receipt.get("attempt_count")
        != stale_receipt.get("attempt_count")
        or execution_receipt.get("completed_all")
        != stale_receipt.get("completed_all")
        or execution_receipt.get("actual_answers_released") != 0
    ):
        raise RuntimeError("R4C_RECOVERY_RELEASE_BOUNDARY_DRIFT")
    if (
        preregistration_receipt.get("entity") != amendment.get("destination", {}).get("entity")
        or preregistration_receipt.get("project") != TARGET_PROJECT
        or preregistration_receipt.get("source_sha256") != file_sha256(preregistration_path)
        or preregistration_receipt.get("readback_sha256")
        != file_sha256(preregistration_path)
        or not preregistration_receipt.get("artifact_reference")
    ):
        raise RuntimeError("R4C_RECOVERY_PREREGISTRATION_RECEIPT_DRIFT")

    row_counts: dict[str, int] = {}
    for name in PUBLICATION_FILES:
        value = _load_object(publication_dir / name)
        projection = type_stable_table_projection(value)
        if projection is not None:
            row_counts[Path(name).stem] = int(projection["source_row_count"])
    amendment_sha256 = file_sha256(amendment_path)
    identity_payload = {
        "recovery_version": RECOVERY_VERSION,
        "study_instance_sha256": preregistration["study_instance_sha256"],
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "amendment_sha256": amendment_sha256,
        "package_sha256": package_sha256,
        "table_projection_version": TABLE_PROJECTION_VERSION,
        "destination": amendment["destination"],
    }
    return RecoveryInputs(
        campaign_root=campaign_root,
        publication_dir=publication_dir,
        preregistration_path=preregistration_path,
        preregistration_receipt_path=preregistration_receipt_path,
        amendment_path=amendment_path,
        package_files=package_files,
        package_sha256=package_sha256,
        analysis_file_sha256=file_sha256(publication_dir / "analysis.json"),
        analysis_sha256=str(analysis["analysis_sha256"]),
        study_instance_sha256=str(preregistration["study_instance_sha256"]),
        preregistration_sha256=str(preregistration["preregistration_sha256"]),
        preregistration_artifact_reference=str(
            preregistration_receipt["artifact_reference"]
        ),
        amendment_sha256=amendment_sha256,
        recovery_identity_sha256=json_sha256(identity_payload),
        table_row_counts=row_counts,
    )


def recovery_manifest(inputs: RecoveryInputs, *, amendment_reference: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "recovery_version": RECOVERY_VERSION,
        "kind": "architecture_development_publication_recovery",
        "study_version": STUDY_VERSION,
        "study_instance_sha256": inputs.study_instance_sha256,
        "preregistration_sha256": inputs.preregistration_sha256,
        "preregistration_artifact_reference": inputs.preregistration_artifact_reference,
        "publication_amendment_sha256": inputs.amendment_sha256,
        "publication_amendment_reference": amendment_reference,
        "original_package_sha256": inputs.package_sha256,
        "original_files": dict(inputs.package_files),
        "original_analysis_file_sha256": inputs.analysis_file_sha256,
        "original_analysis_sha256": inputs.analysis_sha256,
        "table_projection_version": TABLE_PROJECTION_VERSION,
        "table_row_counts": dict(inputs.table_row_counts),
        "artifact_file_count": len(inputs.package_files),
        "original_files_uploaded_byte_for_byte": True,
        "provider_calls_performed": 0,
        "inference_reexecuted": False,
        "analysis_recomputed": False,
        "actual_answers_released": 0,
        "release_eligible": False,
        "production_state": "DISABLE_LIVE_GUIDE",
        "practitioner_usefulness_status": "not_measured",
        "destination_access_required": "Restricted",
    }


def _artifact_readback(
    *,
    artifact: Any,
    files: Mapping[str, str],
) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="gi-r4c-recovery-readback-") as temporary:
        downloaded = Path(artifact.download(root=temporary))
        return {name: file_sha256(downloaded / name) for name in files}


def publish_recovery(
    *,
    publisher: WandbCampaignPublisher,
    inputs: RecoveryInputs,
    amendment_reference: str,
) -> CloudReceipt:
    """Publish only Tables and the unchanged seven-file artifact to W&B."""

    import wandb  # type: ignore[import-not-found]

    if publisher.campaign_instance_sha256 != inputs.study_instance_sha256:
        raise RuntimeError("R4C_RECOVERY_PUBLISHER_STUDY_IDENTITY_DRIFT")
    if publisher.preregistration_reference != inputs.preregistration_artifact_reference:
        raise RuntimeError("R4C_RECOVERY_PREREGISTRATION_NOT_REUSED")
    manifest = recovery_manifest(inputs, amendment_reference=amendment_reference)
    # The identity is fixed before any cloud write. The acknowledged amendment
    # reference is recorded as lineage, while the amendment's local SHA-256 is
    # already part of this deterministic identity.
    identity = inputs.recovery_identity_sha256
    run_id = _campaign_run_id(
        campaign_instance_sha256=inputs.study_instance_sha256,
        kind="r4c_publication_recovery",
        entity=publisher.entity,
        project=publisher.project,
        identity=identity,
    )
    with publisher.cloud_environment():
        api = wandb.Api()
        existing = None
        try:
            existing = api.run(f"{publisher.entity}/{publisher.project}/{run_id}")
        except Exception as exc:  # W&B exposes version-specific not-found classes.
            if type(exc).__name__ not in {"CommError", "UsageError", "ValueError"}:
                raise
        if existing is not None and existing.summary.get("publisher_status") == "complete":
            if existing.summary.get("recovery_identity_sha256") != identity:
                raise RuntimeError("R4C_RECOVERY_COMPLETED_RUN_IDENTITY_DRIFT")
            reference = str(existing.summary.get("artifact_reference") or "")
            if not reference:
                raise RuntimeError("R4C_RECOVERY_COMPLETED_RUN_ARTIFACT_MISSING")
            readback = _artifact_readback(artifact=api.artifact(reference), files=inputs.package_files)
            if readback != dict(inputs.package_files):
                raise RuntimeError("R4C_RECOVERY_EXISTING_ARTIFACT_HASH_DRIFT")
            return CloudReceipt(
                kind="r4c_publication_recovery",
                entity=publisher.entity,
                project=publisher.project,
                run_id=run_id,
                run_url=str(getattr(existing, "url", "") or ""),
                artifact_id=str(existing.summary.get("artifact_id") or "") or None,
                artifact_reference=reference,
                source_sha256=identity,
                readback_sha256=json_sha256(
                    {"manifest": manifest, "files": readback}
                ),
            )

        run = wandb.init(
            entity=publisher.entity,
            project=publisher.project,
            id=run_id,
            resume="allow",
            name=f"r4c-publication-recovery-{identity[:12]}",
            job_type="evidence_guide_r4c_publication_recovery",
            config={
                "recovery_version": RECOVERY_VERSION,
                "campaign_instance_sha256": inputs.study_instance_sha256,
                "recovery_identity_sha256": identity,
                "original_package_sha256": inputs.package_sha256,
                "required_access": "Restricted",
                "provider_calls_performed": 0,
                "release_eligible": False,
            },
            reinit="create_new",
            settings=_wandb_run_settings(wandb),
        )
        try:
            run.use_artifact(inputs.preregistration_artifact_reference)
            run.use_artifact(amendment_reference)
            table_count = 0
            table_projection_hashes: dict[str, str] = {}
            for name in PUBLICATION_FILES:
                value = _load_object(inputs.publication_dir / name)
                projection = type_stable_table_projection(value)
                if projection is None:
                    continue
                table = wandb.Table(columns=projection["columns"])
                for row in projection["rows"]:
                    table.add_data(*row)
                run.log({f"r4c-publication-recovery/{Path(name).stem}": table})
                table_count += 1
                table_projection_hashes[Path(name).stem] = str(
                    projection["projection_sha256"]
                )
            artifact = wandb.Artifact(
                name=f"evidence-guide-r4c-publication-{identity[:16]}",
                type="research-development-evidence",
                metadata={
                    **manifest,
                    "recovery_identity_sha256": identity,
                    "table_projection_hashes": table_projection_hashes,
                },
            )
            for name in PUBLICATION_FILES:
                artifact.add_file(str(inputs.publication_dir / name), name=name)
            logged = run.log_artifact(artifact)
            logged.wait()
            reference = _logged_artifact_reference(
                logged,
                entity=publisher.entity,
                project=publisher.project,
            )
            readback = _artifact_readback(
                artifact=run.use_artifact(reference), files=inputs.package_files
            )
            if readback != dict(inputs.package_files):
                raise RuntimeError("R4C_RECOVERY_ARTIFACT_READBACK_HASH_MISMATCH")
            run.summary.update(
                {
                    "publisher_status": "complete",
                    "recovery_identity_sha256": identity,
                    "original_package_sha256": inputs.package_sha256,
                    "artifact_reference": reference,
                    "artifact_id": str(logged.id),
                    "table_count": table_count,
                    "table_projection_hashes": table_projection_hashes,
                    "provider_calls_performed": 0,
                    "actual_answers_released": 0,
                    "release_eligible": False,
                    "production_state": "DISABLE_LIVE_GUIDE",
                }
            )
            return CloudReceipt(
                kind="r4c_publication_recovery",
                entity=publisher.entity,
                project=publisher.project,
                run_id=str(run.id),
                run_url=str(getattr(run, "url", "") or ""),
                artifact_id=str(logged.id),
                artifact_reference=reference,
                source_sha256=identity,
                readback_sha256=json_sha256(
                    {"manifest": manifest, "files": readback}
                ),
            )
        finally:
            run.finish()


def rehearse_recovery(
    *, inputs: RecoveryInputs, output_dir: Path
) -> dict[str, Any]:
    """Exercise projections and byte-for-byte artifact readback without a network."""

    output_dir = output_dir.resolve()
    artifact_dir = output_dir / "artifact"
    projections_dir = output_dir / "table-projections"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    projections_dir.mkdir(parents=True, exist_ok=True)
    table_hashes: dict[str, str] = {}
    for name in PUBLICATION_FILES:
        shutil.copyfile(inputs.publication_dir / name, artifact_dir / name)
        value = _load_object(inputs.publication_dir / name)
        projection = type_stable_table_projection(value)
        if projection is not None:
            write_json_atomic(projections_dir / name, projection)
            table_hashes[name] = str(projection["projection_sha256"])
    readback = {name: file_sha256(artifact_dir / name) for name in PUBLICATION_FILES}
    if readback != dict(inputs.package_files):
        raise RuntimeError("R4C_RECOVERY_REHEARSAL_READBACK_HASH_MISMATCH")
    body = {
        "schema_version": "1.0",
        "recovery_version": RECOVERY_VERSION,
        "mode": "offline_fake_rehearsal",
        "network_calls": 0,
        "provider_calls": 0,
        "inference_reexecuted": False,
        "attempts_validated": EXPECTED_ATTEMPTS,
        "provider_responses_validated": EXPECTED_ATTEMPTS,
        "trace_acknowledgements_validated": EXPECTED_ATTEMPTS,
        "block_receipts_validated": len(EXPECTED_TIME_BLOCKS),
        "actual_answers_released": 0,
        "original_package_sha256": inputs.package_sha256,
        "original_files": readback,
        "analysis_file_sha256": inputs.analysis_file_sha256,
        "analysis_sha256": inputs.analysis_sha256,
        "table_projection_version": TABLE_PROJECTION_VERSION,
        "table_projection_hashes": table_hashes,
        "recovery_identity_sha256": inputs.recovery_identity_sha256,
        "production_state": "DISABLE_LIVE_GUIDE",
        "practitioner_usefulness_status": "not_measured",
    }
    receipt = {**body, "receipt_sha256": json_sha256(body)}
    write_json_atomic(output_dir / "rehearsal-receipt.json", receipt)
    return receipt


__all__ = [
    "PUBLICATION_FILES",
    "RECOVERY_VERSION",
    "RecoveryInputs",
    "canonical_table_cell",
    "publish_recovery",
    "recovery_manifest",
    "rehearse_recovery",
    "type_stable_table_projection",
    "validate_recovery_inputs",
]
