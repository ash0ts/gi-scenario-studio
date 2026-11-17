from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ecocorridor_core.contracts import (
    AssetKind,
    ConnectivityAsset,
    InterventionKind,
)
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_worker import RPCDispatcher, RPCFault


def _initialized_store(root: Path, *, mode: str = "GUIDED") -> EcoProjectStore:
    fixture = build_ridgeway_fixture()
    store = EcoProjectStore(root)
    store.initialize(
        project_id=fixture.bundle.project_id,
        title=fixture.bundle.title,
        mode=mode,
        skeleton=fixture.bundle.model_dump(mode="json"),
    )
    store.store_bundle(fixture.bundle)
    return store


def test_model_validation_rejects_arbitrary_fitness_evidence_ids_with_remediation(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    project = tmp_path / "arbitrary-evidence"
    fitness = fixture.bundle.fitness.model_copy(
        update={
            "dimensions": [
                item.model_copy(update={"evidence_ids": ["reviewed-memo-that-does-not-exist"]})
                for item in fixture.bundle.fitness.dimensions
            ]
        }
    )
    bundle = fixture.bundle.model_copy(update={"fitness": fitness})
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=bundle.project_id,
            title=bundle.title,
            mode="GUIDED",
            skeleton=bundle.model_dump(mode="json"),
        )
        store.store_bundle(bundle)
    finally:
        store.close()

    result = RPCDispatcher(data_root=tmp_path).dispatch(
        "connectivity.model.validate", {"project_directory": str(project)}
    )["data"]
    unresolved = [
        item
        for item in result["findings"]
        if item["code"] == "MODEL_FITNESS_EVIDENCE_ID_UNRESOLVED"
    ]
    assert result["status"] == "BLOCKED"
    assert len(unresolved) == len(fitness.dimensions)
    assert all("active project revision" in item["remediation"] for item in unresolved)


def test_fitness_evidence_asset_requires_exact_active_bytes(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    project = tmp_path / "asset-evidence"
    reviewed_bytes = b"reviewed model-fitness memorandum\n"
    document = ConnectivityAsset(
        asset_id="asset_reviewed_fitness_memo",
        kind=AssetKind.DOCUMENT,
        title="Reviewed model-fitness memorandum",
        relative_path="documents/model-fitness-memo.txt",
        sha256=hashlib.sha256(reviewed_bytes).hexdigest(),
        byte_size=len(reviewed_bytes),
        media_type="text/plain",
        source="Reviewer-authored test memorandum",
        vintage="2026",
        license="CC0-1.0",
        authorization=fixture.bundle.assets[0].authorization.model_copy(
            update={
                "asset_id": "asset_reviewed_fitness_memo",
                "license_spdx_or_name": "CC0-1.0",
            }
        ),
    )
    fitness = fixture.bundle.fitness.model_copy(
        update={
            "dimensions": [
                item.model_copy(update={"evidence_ids": [document.asset_id]})
                for item in fixture.bundle.fitness.dimensions
            ]
        }
    )
    bundle = fixture.bundle.model_copy(
        update={"assets": [*fixture.bundle.assets, document], "fitness": fitness}
    )
    destination = project / document.relative_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(reviewed_bytes)
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=bundle.project_id,
            title=bundle.title,
            mode="IMPORTED",
            skeleton=bundle.model_dump(mode="json"),
        )
        store.store_bundle(bundle)
        assert not RPCDispatcher._model_fitness_evidence_findings(store, bundle)

        destination.write_bytes(b"changed after review\n")
        findings = RPCDispatcher._model_fitness_evidence_findings(store, bundle)
        assert {item.code for item in findings} == {"MODEL_FITNESS_EVIDENCE_BYTES_CHANGED"}
        assert all("exact active asset bytes" in item.remediation for item in findings)
    finally:
        store.close()


def test_model_validation_rejects_mismatched_retained_evidence_identity(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    project = tmp_path / "mismatched-evidence"
    evidence_id = "retained-review-record"
    fitness = fixture.bundle.fitness.model_copy(
        update={
            "dimensions": [
                item.model_copy(update={"evidence_ids": [evidence_id]})
                for item in fixture.bundle.fitness.dimensions
            ]
        }
    )
    bundle = fixture.bundle.model_copy(update={"fitness": fitness})
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=bundle.project_id,
            title=bundle.title,
            mode="GUIDED",
            skeleton=bundle.model_dump(mode="json"),
        )
        store.store_bundle(bundle)
        content = {"review": "retained but bound to another identity"}
        store.put_record(
            "model_fitness_evidence",
            evidence_id,
            {
                "schema_version": "ecocorridor-model-fitness-evidence/v1.0",
                "evidence_id": "different-evidence-id",
                "revision_id": bundle.revision.revision_id,
                "model_sha256": bundle.revision.model_sha256,
                "content_sha256": hashlib.sha256(b"not-the-canonical-content").hexdigest(),
                "content": content,
                "source_asset_ids": [],
            },
        )
    finally:
        store.close()

    result = RPCDispatcher(data_root=tmp_path).dispatch(
        "connectivity.model.validate", {"project_directory": str(project)}
    )["data"]
    mismatches = [
        item
        for item in result["findings"]
        if item["code"] == "MODEL_FITNESS_EVIDENCE_RECORD_MISMATCH"
    ]
    assert result["status"] == "BLOCKED"
    assert len(mismatches) == len(fitness.dimensions)
    assert all("content hash and identity" in item["remediation"] for item in mismatches)


def test_stale_retained_fitness_evidence_blocks_attestation_with_typed_fault(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    project = tmp_path / "stale-evidence"
    store = _initialized_store(project)
    try:
        store.retain_model_fitness_evidence(
            evidence_id="fitness-review-memo-v1",
            revision=fixture.bundle.revision,
            content={"review": "applies only to revision one"},
        )
        with pytest.raises(ValueError, match="immutable within one semantic revision"):
            store.retain_model_fitness_evidence(
                evidence_id="fitness-review-memo-v1",
                revision=fixture.bundle.revision,
                content={"review": "silently replaced content"},
            )
        changed_brief = fixture.bundle.decision_brief.model_copy(
            update={"decision_question": "Which reviewed concept remains robust?"}
        )
        publication = store.publish_semantic_change(
            [
                (
                    "decision_brief",
                    changed_brief.brief_id,
                    changed_brief.model_dump(mode="json"),
                )
            ],
            created_by="Reviewer",
            reason="Change the active decision question.",
        )
        active_revision_id = publication["revision"]["revision_id"]
    finally:
        store.close()

    reviewed_at = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
    candidate = fixture.bundle.fitness.model_copy(
        update={
            "revision_id": active_revision_id,
            "dimensions": [
                item.model_copy(
                    update={
                        "evidence_ids": ["fitness-review-memo-v1"],
                        "reviewer": "Reviewer",
                    }
                )
                for item in fixture.bundle.fitness.dimensions
            ],
            "reviewed_by": "Reviewer",
            "reviewed_at": reviewed_at,
            "attestations": [],
        }
    )
    with pytest.raises(RPCFault) as raised:
        RPCDispatcher(data_root=tmp_path).dispatch(
            "connectivity.model.attest",
            {
                "project_directory": str(project),
                "fitness": candidate.model_dump(mode="json"),
                "reviewer": "Reviewer",
                "attestation": "Reviewed for this connectivity-screening purpose.",
            },
        )
    assert raised.value.error.code == "MODEL_FITNESS_EVIDENCE_ID_STALE"
    assert raised.value.error.object_reference.startswith("action_effect_evidence:")
    assert "active semantic revision" in (raised.value.error.remediation or "")


def test_intervention_removal_tombstone_survives_restart_and_readd_is_idempotent(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    project = tmp_path / "intervention-tombstone"
    site = fixture.bundle.intervention_sites[0]
    active_options = [
        item for item in fixture.bundle.intervention_options if item.site_id == site.site_id
    ]
    no_action = next(item for item in active_options if item.kind == InterventionKind.NO_ACTION)
    survey_first = next(
        item for item in active_options if item.kind == InterventionKind.SURVEY_FIRST
    )
    historical = survey_first
    excluded_option_ids = {
        item.option_id
        for item in active_options
        if item.option_id not in {no_action.option_id, survey_first.option_id}
    }
    site = site.model_copy(update={"option_ids": [no_action.option_id, survey_first.option_id]})
    assert fixture.bundle.cost_basis is not None
    initial_cost_basis = fixture.bundle.cost_basis.model_copy(
        update={
            "option_costs": {
                option_id: cost
                for option_id, cost in fixture.bundle.cost_basis.option_costs.items()
                if option_id not in excluded_option_ids
            }
        }
    )
    initial_bundle = fixture.bundle.model_copy(
        update={
            "intervention_sites": [
                site if item.site_id == site.site_id else item
                for item in fixture.bundle.intervention_sites
            ],
            "intervention_options": [
                item
                for item in fixture.bundle.intervention_options
                if item.option_id not in excluded_option_ids
            ],
            "cost_basis": initial_cost_basis,
        }
    )
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=initial_bundle.project_id,
            title=initial_bundle.title,
            mode="GUIDED",
            skeleton=initial_bundle.model_dump(mode="json"),
        )
        store.store_bundle(initial_bundle)
    finally:
        store.close()

    dispatcher = RPCDispatcher(data_root=tmp_path)
    removed_at = site.reviewed_at + timedelta(minutes=1)
    reduced_site = site.model_copy(
        update={"option_ids": [no_action.option_id], "reviewed_at": removed_at}
    )
    remove_params = {
        "project_directory": str(project),
        "site": reduced_site.model_dump(mode="json"),
        "options": [no_action.model_dump(mode="json")],
    }
    first_remove = dispatcher.dispatch("connectivity.intervention.save", remove_params)["data"]
    assert first_remove["option_ids"] == [no_action.option_id]

    store = EcoProjectStore(project)
    try:
        after_remove = store.effective_bundle()
        removed_revision = after_remove.revision.revision_id
        assert historical.option_id not in {
            item.option_id for item in after_remove.intervention_options
        }
        tombstone = store.get_record("intervention_option_tombstone", historical.option_id)
        assert tombstone["state"] == "REMOVED"
        diff = store.get_record("semantic_diff", after_remove.revision.diff_id or "")
        assert any(
            item["object_type"] == "intervention_option_tombstone"
            and item["object_id"] == historical.option_id
            for item in diff["changes"]
        )
    finally:
        store.close()

    assert (
        dispatcher.dispatch("connectivity.intervention.save", remove_params)["data"] == first_remove
    )
    store = EcoProjectStore(project)
    try:
        assert store.effective_bundle().revision.revision_id == removed_revision
        with pytest.raises(KeyError):
            store.get_record_revision("intervention_option_tombstone", historical.option_id, 2)
    finally:
        store.close()

    restored_at = removed_at + timedelta(minutes=1)
    restored_site = site.model_copy(update={"reviewed_at": restored_at})
    restore_params = {
        "project_directory": str(project),
        "site": restored_site.model_dump(mode="json"),
        "options": [
            no_action.model_dump(mode="json"),
            survey_first.model_dump(mode="json"),
        ],
    }
    first_restore = dispatcher.dispatch("connectivity.intervention.save", restore_params)["data"]
    store = EcoProjectStore(project)
    try:
        after_restore = store.effective_bundle()
        restored_revision = after_restore.revision.revision_id
        assert historical.option_id in {
            item.option_id for item in after_restore.intervention_options
        }
        assert (
            store.get_record("intervention_option_tombstone", historical.option_id)["state"]
            == "RESTORED"
        )
        restored_tombstone = store.get_record("intervention_option_tombstone", historical.option_id)
        assert restored_tombstone["parent_revision_id"] == removed_revision
        restored_diff = store.get_record("semantic_diff", after_restore.revision.diff_id or "")
        assert any(
            item["object_type"] == "intervention_option_tombstone"
            and item["object_id"] == historical.option_id
            for item in restored_diff["changes"]
        )
        assert (
            store.get_record_revision("intervention_option_tombstone", historical.option_id, 1)[
                "state"
            ]
            == "REMOVED"
        )
        assert (
            store.get_record_revision("intervention_option_tombstone", historical.option_id, 2)[
                "state"
            ]
            == "RESTORED"
        )
    finally:
        store.close()

    assert (
        dispatcher.dispatch("connectivity.intervention.save", restore_params)["data"]
        == first_restore
    )
    store = EcoProjectStore(project)
    try:
        assert store.effective_bundle().revision.revision_id == restored_revision
        with pytest.raises(KeyError):
            store.get_record_revision("intervention_option_tombstone", historical.option_id, 3)
    finally:
        store.close()
