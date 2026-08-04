from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ecocorridor_core.contracts import (
    AssetKind,
    CloudDisposition,
    ConnectivityAsset,
    ModelRevision,
    RasterGrid,
    RasterValueKind,
    ResourcePolicy,
    SensitiveDataAuthorization,
    SensitivityClass,
)
from ecocorridor_core.digitization import (
    DigitizationCellDraft,
    DigitizationDraft,
    DigitizationError,
    DigitizationKind,
    DigitizationPolygonDraft,
    DigitizationPublishParams,
    DigitizationVertex,
    preview_digitization,
    publish_digitization,
)
from ecocorridor_core.grid import InMemoryRaster
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.raster_io import load_raster, write_esri_ascii
from ecocorridor_core.rpc_worker import RPCDispatcher

REVIEW_TIME = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def _authorization(asset_id: str) -> SensitiveDataAuthorization:
    return SensitiveDataAuthorization(
        asset_id=asset_id,
        ownership="Planning team",
        license_spdx_or_name="LicenseRef-Project-Data",
        sensitivity=SensitivityClass.INTERNAL,
        cloud_disposition=CloudDisposition.PROHIBITED,
        export_allowed=False,
        retention_policy="Retain with the reviewed planning project.",
        spatial_redaction="Do not export exact coordinates.",
        approved_deployments=["LOCAL_ONLY"],
        reviewer="Data steward",
        reviewed_at=REVIEW_TIME,
    )


def _project(tmp_path: Path, *, nodata: float = -9999) -> tuple[EcoProjectStore, ConnectivityAsset]:
    project = tmp_path / "planning-project"
    store = EcoProjectStore(project)
    revision = ModelRevision(
        revision_id="revision_initial",
        project_id="project_digitization",
        created_at=REVIEW_TIME,
        created_by="Planning team",
        reason="Create reviewed planning workspace.",
        model_sha256="1" * 64,
    )
    policy = ResourcePolicy(
        max_input_bytes=2_000_000,
        max_materialized_bytes=2_000_000,
        max_current_raster_bytes=2_000_000,
        max_direct_jobs=100,
        max_wall_seconds_per_job=30,
    )
    store.initialize(
        project_id="project_digitization",
        title="Digitization test",
        mode="PLANNING",
        skeleton={
            "revision": revision.model_dump(mode="json"),
            "resource_policy": policy.model_dump(mode="json"),
        },
    )
    grid = RasterGrid(
        grid_id="grid_reference",
        crs="EPSG:26917",
        width=4,
        height=4,
        transform=(10.0, 0.0, 0.0, 0.0, -10.0, 40.0),
        nodata=nodata,
        value_kind=RasterValueKind.CONTINUOUS,
        cell_area_m2=100.0,
    )
    source = project / "data_assets" / "reference.asc"
    source_sha256 = write_esri_ascii(
        source,
        InMemoryRaster(
            asset_id="asset_reference",
            grid=grid,
            values=(
                (1.0, 1.0, 1.0, 1.0),
                (1.0, nodata, 1.0, 1.0),
                (1.0, 1.0, 1.0, 1.0),
                (1.0, 1.0, 1.0, 1.0),
            ),
            source_sha256="2" * 64,
        ),
    )
    asset = ConnectivityAsset(
        asset_id="asset_reference",
        kind=AssetKind.RESISTANCE_RASTER,
        title="Reviewed reference grid",
        relative_path="data_assets/reference.asc",
        sha256=source_sha256,
        byte_size=source.stat().st_size,
        media_type="application/x-esri-ascii-grid",
        source="user-provided",
        vintage="2026-08-03",
        license="LicenseRef-Project-Data",
        grid=grid,
        authorization=_authorization("asset_reference"),
    )
    store.put_record("asset", asset.asset_id, asset.model_dump(mode="json"), allow_update=False)
    return store, asset


def _publish_params(
    *,
    project: Path,
    draft: DigitizationDraft,
    preview_sha256: str,
    asset_id: str,
    revision_id: str,
) -> DigitizationPublishParams:
    return DigitizationPublishParams(
        project_directory=str(project),
        draft=draft,
        expected_preview_sha256=preview_sha256,
        asset_id=asset_id,
        title="Reviewer-authored raster",
        relative_path=f"data_assets/digitized/{asset_id}.asc",
        license="LicenseRef-Project-Data",
        authorization=_authorization(asset_id),
        reviewer="Wildlife ecologist",
        reviewed_at=REVIEW_TIME,
        ecological_basis="Areas were explicitly delineated from the reviewed field map.",
        revision_id=revision_id,
        reason="Publish reviewer-authored cells without inferring ecological values.",
    )


def test_focal_preview_is_pure_and_publish_is_immutable_and_auditable(tmp_path: Path) -> None:
    store, _ = _project(tmp_path)
    project = store.root
    draft = DigitizationDraft(
        draft_id="draft_focal",
        kind=DigitizationKind.FOCAL_AREAS,
        reference_asset_id="asset_reference",
        cells=[
            DigitizationCellDraft(feature_id="core-west", row=0, column=0, value=11),
            DigitizationCellDraft(feature_id="core-east", row=3, column=3, value=22),
        ],
    )
    records_before = store.list_records("digitization_provenance")
    preview = preview_digitization(store, draft)
    assert store.list_records("digitization_provenance") == records_before == []
    assert not (project / "data_assets" / "digitized").exists()
    assert preview.value_kind == RasterValueKind.IDENTIFIER
    assert preview.value_counts == {"11": 1, "22": 1}
    assert preview.affected_area_m2 == 200.0
    assert preview.nodata_cell_count == 1

    publication = publish_digitization(
        store,
        _publish_params(
            project=project,
            draft=draft,
            preview_sha256=preview.preview_sha256,
            asset_id="asset_focal_authored",
            revision_id="revision_focal_authored",
        ),
    )
    destination = project / publication.asset.relative_path
    assert destination.is_file() and not destination.is_symlink()
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == preview.materialized_sha256
    raster = load_raster(destination, publication.asset)
    assert raster.values[0][0] == 11
    assert raster.values[3][3] == 22
    assert raster.values[1][1] == -9999
    assert raster.values[0][1] == 0
    assert publication.revision.parent_revision_id == "revision_initial"
    assert publication.provenance.draft == draft
    assert publication.provenance.ecological_basis.startswith("Areas were explicitly")
    assert store.get_record("asset", "asset_focal_authored") == publication.asset.model_dump(
        mode="json"
    )
    assert store.get_record(
        "digitization_provenance", publication.provenance.provenance_id
    ) == publication.provenance.model_dump(mode="json")

    with pytest.raises(DigitizationError, match="already occupies"):
        publish_digitization(
            store,
            _publish_params(
                project=project,
                draft=draft,
                preview_sha256=preview.preview_sha256,
                asset_id="asset_focal_authored",
                revision_id="revision_focal_duplicate",
            ),
        )
    store.close()


def test_digitization_rpc_validates_preview_and_publication_shapes(tmp_path: Path) -> None:
    store, _ = _project(tmp_path)
    project = store.root
    store.close()
    draft = DigitizationDraft(
        draft_id="rpc-focal",
        kind=DigitizationKind.FOCAL_AREAS,
        reference_asset_id="asset_reference",
        cells=[
            DigitizationCellDraft(feature_id="one", row=0, column=0, value=101),
            DigitizationCellDraft(feature_id="two", row=3, column=3, value=202),
        ],
    )
    dispatcher = RPCDispatcher(data_root=tmp_path)
    preview_result = dispatcher.dispatch(
        "connectivity.digitization.preview",
        {"project_directory": str(project), "draft": draft.model_dump(mode="json")},
    )
    assert preview_result["method"] == "connectivity.digitization.preview"
    preview_payload = preview_result["data"]["preview"]
    assert preview_payload["value_counts"] == {"101": 1, "202": 1}
    publish = _publish_params(
        project=project,
        draft=draft,
        preview_sha256=preview_payload["preview_sha256"],
        asset_id="asset_rpc_focal",
        revision_id="revision_rpc_focal",
    )
    publication_result = dispatcher.dispatch(
        "connectivity.digitization.publish", publish.model_dump(mode="json")
    )
    assert publication_result["method"] == "connectivity.digitization.publish"
    assert publication_result["data"]["asset"]["asset_id"] == "asset_rpc_focal"
    assert (
        publication_result["data"]["provenance"]["preview_sha256"]
        == preview_payload["preview_sha256"]
    )
    assert publication_result["data"]["revision"]["parent_revision_id"] == "revision_initial"


@pytest.mark.parametrize(
    ("kind", "expected_kind"),
    [
        (DigitizationKind.INTERVENTION_FOOTPRINT, AssetKind.ACTION_EFFECT),
        (DigitizationKind.RISK_REVIEW_MASK, AssetKind.REVIEW_MASK),
    ],
)
def test_polygon_mask_preview_and_publish_use_cell_centers_only(
    tmp_path: Path, kind: DigitizationKind, expected_kind: AssetKind
) -> None:
    store, _ = _project(tmp_path)
    draft = DigitizationDraft(
        draft_id=f"draft_{kind.value.casefold()}",
        kind=kind,
        reference_asset_id="asset_reference",
        polygons=[
            DigitizationPolygonDraft(
                feature_id="reviewed-polygon",
                value=1,
                vertices=[
                    DigitizationVertex(x=20, y=40),
                    DigitizationVertex(x=40, y=40),
                    DigitizationVertex(x=40, y=20),
                    DigitizationVertex(x=20, y=20),
                ],
            )
        ],
    )
    preview = preview_digitization(store, draft)
    assert {(item.row, item.column) for item in preview.affected_cells} == {
        (0, 2),
        (0, 3),
        (1, 2),
        (1, 3),
    }
    assert preview.value_counts == {"1": 4}
    asset_id = f"asset_{kind.value.casefold()}"
    publication = publish_digitization(
        store,
        _publish_params(
            project=store.root,
            draft=draft,
            preview_sha256=preview.preview_sha256,
            asset_id=asset_id,
            revision_id=f"revision_{kind.value.casefold()}",
        ),
    )
    assert publication.asset.kind == expected_kind
    assert publication.asset.grid is not None
    assert publication.asset.grid.value_kind == RasterValueKind.MASK
    store.close()


def test_digitization_rejects_nodata_bounds_overlap_and_stale_preview(tmp_path: Path) -> None:
    store, _ = _project(tmp_path)

    def focal(*cells: DigitizationCellDraft) -> DigitizationDraft:
        return DigitizationDraft(
            draft_id="draft_invalid",
            kind=DigitizationKind.FOCAL_AREAS,
            reference_asset_id="asset_reference",
            cells=list(cells),
        )

    with pytest.raises(DigitizationError) as nodata:
        preview_digitization(
            store,
            focal(
                DigitizationCellDraft(feature_id="one", row=1, column=1, value=1),
                DigitizationCellDraft(feature_id="two", row=3, column=3, value=2),
            ),
        )
    assert nodata.value.code == "DIGITIZATION_NODATA_SELECTED"

    with pytest.raises(DigitizationError) as bounds:
        preview_digitization(
            store,
            focal(
                DigitizationCellDraft(feature_id="one", row=0, column=0, value=1),
                DigitizationCellDraft(feature_id="two", row=4, column=0, value=2),
            ),
        )
    assert bounds.value.code == "DIGITIZATION_CELL_OUT_OF_BOUNDS"

    with pytest.raises(DigitizationError) as overlap:
        preview_digitization(
            store,
            focal(
                DigitizationCellDraft(feature_id="one", row=0, column=0, value=1),
                DigitizationCellDraft(feature_id="two", row=0, column=0, value=2),
            ),
        )
    assert overlap.value.code == "DIGITIZATION_AMBIGUOUS_OVERLAP"

    polygon_outside = DigitizationDraft(
        draft_id="polygon-outside",
        kind=DigitizationKind.RISK_REVIEW_MASK,
        reference_asset_id="asset_reference",
        polygons=[
            DigitizationPolygonDraft(
                feature_id="outside",
                value=1,
                vertices=[
                    DigitizationVertex(x=-1, y=40),
                    DigitizationVertex(x=10, y=40),
                    DigitizationVertex(x=10, y=30),
                    DigitizationVertex(x=-1, y=30),
                ],
            )
        ],
    )
    with pytest.raises(DigitizationError) as polygon_bounds:
        preview_digitization(store, polygon_outside)
    assert polygon_bounds.value.code == "DIGITIZATION_POLYGON_OUT_OF_BOUNDS"

    mixed_overlap = DigitizationDraft(
        draft_id="mixed-overlap",
        kind=DigitizationKind.INTERVENTION_FOOTPRINT,
        reference_asset_id="asset_reference",
        cells=[DigitizationCellDraft(feature_id="cell", row=0, column=2, value=1)],
        polygons=[
            DigitizationPolygonDraft(
                feature_id="polygon",
                value=1,
                vertices=[
                    DigitizationVertex(x=20, y=40),
                    DigitizationVertex(x=40, y=40),
                    DigitizationVertex(x=40, y=20),
                    DigitizationVertex(x=20, y=20),
                ],
            )
        ],
    )
    with pytest.raises(DigitizationError) as mixed:
        preview_digitization(store, mixed_overlap)
    assert mixed.value.code == "DIGITIZATION_AMBIGUOUS_OVERLAP"

    valid = focal(
        DigitizationCellDraft(feature_id="one", row=0, column=0, value=1),
        DigitizationCellDraft(feature_id="two", row=3, column=3, value=2),
    )
    preview = preview_digitization(store, valid)
    params = _publish_params(
        project=store.root,
        draft=valid.model_copy(
            update={
                "cells": [
                    DigitizationCellDraft(feature_id="one", row=0, column=1, value=1),
                    DigitizationCellDraft(feature_id="two", row=3, column=3, value=2),
                ]
            }
        ),
        preview_sha256=preview.preview_sha256,
        asset_id="asset_stale",
        revision_id="revision_stale",
    )
    with pytest.raises(DigitizationError) as stale:
        publish_digitization(store, params)
    assert stale.value.code == "DIGITIZATION_PREVIEW_STALE"
    assert not (store.root / "data_assets" / "digitized" / "asset_stale.asc").exists()
    assert store.list_records("digitization_provenance") == []
    store.close()


def test_preview_rechecks_exact_retained_reference_bytes(tmp_path: Path) -> None:
    store, asset = _project(tmp_path)
    draft = DigitizationDraft(
        draft_id="exact-reference",
        kind=DigitizationKind.FOCAL_AREAS,
        reference_asset_id=asset.asset_id,
        cells=[
            DigitizationCellDraft(feature_id="one", row=0, column=0, value=1),
            DigitizationCellDraft(feature_id="two", row=3, column=3, value=2),
        ],
    )
    preview_digitization(store, draft)
    with (store.root / asset.relative_path).open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(DigitizationError) as changed:
        preview_digitization(store, draft)
    assert changed.value.code == "DIGITIZATION_REFERENCE_BYTES_INVALID"
    store.close()


def test_digitization_rejects_nodata_value_collisions(tmp_path: Path) -> None:
    store, asset = _project(tmp_path, nodata=0)
    draft = DigitizationDraft(
        draft_id="nodata-collision",
        kind=DigitizationKind.FOCAL_AREAS,
        reference_asset_id=asset.asset_id,
        cells=[
            DigitizationCellDraft(feature_id="one", row=0, column=0, value=1),
            DigitizationCellDraft(feature_id="two", row=3, column=3, value=2),
        ],
    )
    with pytest.raises(DigitizationError) as collision:
        preview_digitization(store, draft)
    assert collision.value.code == "DIGITIZATION_NODATA_VALUE_COLLISION"
    store.close()


def test_mask_values_are_user_entered_binary_and_polygon_boundaries_are_explicit(
    tmp_path: Path,
) -> None:
    store, _ = _project(tmp_path)
    with pytest.raises(ValueError, match="binary value 1"):
        DigitizationDraft(
            draft_id="bad-value",
            kind=DigitizationKind.RISK_REVIEW_MASK,
            reference_asset_id="asset_reference",
            cells=[DigitizationCellDraft(feature_id="mask", row=0, column=0, value=2)],
        )
    with pytest.raises(ValueError, match="valid integer"):
        DigitizationCellDraft.model_validate(
            {"feature_id": "mask", "row": 0, "column": 0, "value": True}
        )
    boundary_draft = DigitizationDraft(
        draft_id="boundary",
        kind=DigitizationKind.INTERVENTION_FOOTPRINT,
        reference_asset_id="asset_reference",
        polygons=[
            DigitizationPolygonDraft(
                feature_id="edge-through-centers",
                value=1,
                vertices=[
                    DigitizationVertex(x=5, y=40),
                    DigitizationVertex(x=25, y=40),
                    DigitizationVertex(x=25, y=20),
                    DigitizationVertex(x=5, y=20),
                ],
            )
        ],
    )
    with pytest.raises(DigitizationError) as boundary:
        preview_digitization(store, boundary_draft)
    assert boundary.value.code == "DIGITIZATION_POLYGON_CELL_BOUNDARY_AMBIGUOUS"
    store.close()


def test_digitization_database_failure_rolls_back_all_records_and_exact_file(
    tmp_path: Path,
) -> None:
    store, _ = _project(tmp_path)
    draft = DigitizationDraft(
        draft_id="atomic",
        kind=DigitizationKind.FOCAL_AREAS,
        reference_asset_id="asset_reference",
        cells=[
            DigitizationCellDraft(feature_id="one", row=0, column=0, value=1),
            DigitizationCellDraft(feature_id="two", row=3, column=3, value=2),
        ],
    )
    preview = preview_digitization(store, draft)
    # Force the asset insert to conflict after provenance has tentatively been
    # written; the transaction and exact file must both roll back.
    store.put_record(
        "asset",
        "asset_atomic",
        {"asset_id": "asset_atomic", "sentinel": "existing immutable record"},
        allow_update=False,
    )
    params = _publish_params(
        project=store.root,
        draft=draft,
        preview_sha256=preview.preview_sha256,
        asset_id="asset_atomic",
        revision_id="revision_atomic",
    )
    with pytest.raises(ValueError, match="immutable asset"):
        publish_digitization(store, params)
    assert not (store.root / params.relative_path).exists()
    assert store.get_record("asset", "asset_atomic") == {
        "asset_id": "asset_atomic",
        "sentinel": "existing immutable record",
    }
    assert store.list_records("digitization_provenance") == []
    assert not [
        item
        for item in store.list_records("semantic_diff")
        if item["to_revision_id"] == "revision_atomic"
    ]
    with pytest.raises(KeyError):
        store.get_record("revision", "revision_atomic")
    store.close()
