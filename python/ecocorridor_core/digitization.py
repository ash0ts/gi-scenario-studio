"""Auditable, reviewer-authored raster digitization for planning projects.

The editor protocol deliberately works on an existing retained raster grid.  It
does not infer focal IDs, intervention footprints, risk masks, resistance
values, or ecological meaning.  A preview is a pure projection over the exact
reviewed reference bytes; publishing replays that projection, requires the
exact preview hash, and records the authored geometry and ecological basis in a
semantic model revision.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections import Counter
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import AwareDatetime, Field, StrictInt, field_validator, model_validator
from scenario_platform.storage import PathSecurityError, contained_path

from .contracts import (
    AssetKind,
    ConnectivityAsset,
    ContractModel,
    ModelRevision,
    RasterGrid,
    RasterValueKind,
    ResourcePolicy,
    SemanticChange,
    SemanticModelDiff,
    SensitiveDataAuthorization,
    canonical_sha256,
)
from .grid import InMemoryRaster, _is_nodata, grid_identity
from .project_store import EcoProjectStore
from .raster_io import RasterIOError, load_raster, render_esri_ascii


class DigitizationError(ValueError):
    """A stable, user-correctable digitization failure."""

    def __init__(self, code: str, message: str, *, object_reference: str | None = None) -> None:
        self.code = code
        self.object_reference = object_reference
        super().__init__(message)


class DigitizationKind(str, Enum):
    FOCAL_AREAS = "FOCAL_AREAS"
    INTERVENTION_FOOTPRINT = "INTERVENTION_FOOTPRINT"
    RISK_REVIEW_MASK = "RISK_REVIEW_MASK"


class DigitizationVertex(ContractModel):
    x: float
    y: float

    @model_validator(mode="after")
    def finite_coordinates(self) -> DigitizationVertex:
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise ValueError("digitized vertices must contain finite coordinates")
        return self


class DigitizationCellDraft(ContractModel):
    feature_id: str = Field(min_length=1)
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    value: StrictInt


class DigitizationPolygonDraft(ContractModel):
    feature_id: str = Field(min_length=1)
    value: StrictInt
    vertices: list[DigitizationVertex] = Field(min_length=3)

    @model_validator(mode="after")
    def validate_ring(self) -> DigitizationPolygonDraft:
        coordinates = [(item.x, item.y) for item in self.vertices]
        if coordinates[0] == coordinates[-1]:
            coordinates = coordinates[:-1]
        if len(coordinates) < 3 or len(set(coordinates)) < 3:
            raise ValueError("a digitized polygon requires three distinct vertices")
        for index, coordinate in enumerate(coordinates):
            if coordinate == coordinates[(index + 1) % len(coordinates)]:
                raise ValueError("a digitized polygon cannot contain a zero-length edge")
        _validate_simple_polygon(coordinates)
        return self

    @property
    def open_ring(self) -> tuple[tuple[float, float], ...]:
        coordinates = tuple((item.x, item.y) for item in self.vertices)
        return coordinates[:-1] if coordinates[0] == coordinates[-1] else coordinates


class DigitizationDraft(ContractModel):
    draft_id: str = Field(min_length=1)
    kind: DigitizationKind
    reference_asset_id: str = Field(min_length=1)
    cells: list[DigitizationCellDraft] = Field(default_factory=list)
    polygons: list[DigitizationPolygonDraft] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> DigitizationDraft:
        if not self.cells and not self.polygons:
            raise ValueError("a digitization draft requires at least one cell or polygon")
        _validate_values(self.kind, [item.value for item in [*self.cells, *self.polygons]])
        return self


class DigitizedCell(ContractModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    value: int
    feature_id: str
    source: Literal["CELL", "POLYGON"]


class DigitizationPreview(ContractModel):
    draft_id: str
    kind: DigitizationKind
    reference_asset_id: str
    reference_asset_sha256: str
    grid: RasterGrid
    grid_identity_sha256: str
    value_kind: RasterValueKind
    affected_cells: list[DigitizedCell] = Field(min_length=1)
    affected_cell_count: int = Field(gt=0)
    affected_area_m2: float = Field(gt=0)
    value_counts: dict[str, int]
    explicit_cell_count: int = Field(ge=0)
    polygon_count: int = Field(ge=0)
    background_cell_count: int = Field(ge=0)
    nodata_cell_count: int = Field(ge=0)
    nodata_topology_sha256: str
    materialized_sha256: str
    estimated_materialized_bytes: int = Field(gt=0)
    preview_sha256: str
    notices: list[str]

    @model_validator(mode="after")
    def validate_counts(self) -> DigitizationPreview:
        if self.affected_cell_count != len(self.affected_cells):
            raise ValueError("affected cell count differs from the preview cells")
        if sum(self.value_counts.values()) != self.affected_cell_count:
            raise ValueError("digitization value counts differ from the preview cells")
        for digest in (
            self.reference_asset_sha256,
            self.grid_identity_sha256,
            self.nodata_topology_sha256,
            self.materialized_sha256,
            self.preview_sha256,
        ):
            _require_sha256(digest)
        return self


class DigitizationPreviewParams(ContractModel):
    project_directory: str
    draft: DigitizationDraft


class DigitizationPublishParams(ContractModel):
    project_directory: str
    draft: DigitizationDraft
    expected_preview_sha256: str
    asset_id: str
    title: str
    relative_path: str
    license: str
    authorization: SensitiveDataAuthorization
    reviewer: str
    reviewed_at: AwareDatetime
    ecological_basis: str
    revision_id: str
    reason: str

    @field_validator("expected_preview_sha256")
    @classmethod
    def valid_expected_digest(cls, value: str) -> str:
        _require_sha256(value)
        return value

    @field_validator("relative_path")
    @classmethod
    def valid_destination(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        candidate = PurePosixPath(normalized)
        if (
            candidate.is_absolute()
            or normalized != candidate.as_posix()
            or not normalized.startswith("data_assets/digitized/")
            or candidate.suffix.casefold() != ".asc"
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ValueError(
                "digitized raster paths must be contained .asc files under data_assets/digitized"
            )
        return normalized

    @model_validator(mode="after")
    def validate_review(self) -> DigitizationPublishParams:
        for label, value in (
            ("reviewer", self.reviewer),
            ("ecological_basis", self.ecological_basis),
            ("reason", self.reason),
            ("asset title", self.title),
            ("license", self.license),
        ):
            if not value.strip():
                raise ValueError(f"{label} is required")
        if self.authorization.asset_id != self.asset_id:
            raise ValueError("digitized asset authorization must reference the new asset ID")
        if self.authorization.license_spdx_or_name != self.license:
            raise ValueError("digitized asset license must match its reviewed authorization")
        return self


class DigitizationProvenance(ContractModel):
    provenance_id: str
    provenance_sha256: str
    draft: DigitizationDraft
    preview_sha256: str
    reference_asset_id: str
    reference_asset_sha256: str
    nodata_topology_sha256: str
    materialized_sha256: str
    affected_cell_count: int = Field(gt=0)
    value_counts: dict[str, int]
    ecological_basis: str
    reviewer: str
    reviewed_at: AwareDatetime
    authorization_asset_id: str
    authorization_sha256: str

    @model_validator(mode="after")
    def validate_hashes(self) -> DigitizationProvenance:
        _require_sha256(self.provenance_sha256)
        _require_sha256(self.authorization_sha256)
        return self


class DigitizationPublication(ContractModel):
    preview: DigitizationPreview
    asset: ConnectivityAsset
    provenance: DigitizationProvenance
    semantic_diff: SemanticModelDiff
    revision: ModelRevision


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("value must be a lowercase SHA-256 digest")


def _validate_values(kind: DigitizationKind, values: list[int]) -> None:
    if kind == DigitizationKind.FOCAL_AREAS:
        if any(isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("focal-area values must be user-entered positive integer IDs")
    elif any(isinstance(value, bool) or value != 1 for value in values):
        raise ValueError("intervention footprints and risk masks accept only the binary value 1")


def _orientation(
    first: tuple[float, float], second: tuple[float, float], third: tuple[float, float]
) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (
        third[0] - first[0]
    )


def _point_on_segment(
    point: tuple[float, float], first: tuple[float, float], second: tuple[float, float]
) -> bool:
    tolerance = 1e-10 * max(
        1.0,
        abs(point[0]),
        abs(point[1]),
        abs(first[0]),
        abs(first[1]),
        abs(second[0]),
        abs(second[1]),
    )
    return abs(_orientation(first, second, point)) <= tolerance and (
        min(first[0], second[0]) - tolerance <= point[0] <= max(first[0], second[0]) + tolerance
        and min(first[1], second[1]) - tolerance <= point[1] <= max(first[1], second[1]) + tolerance
    )


def _segments_intersect(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    orientations = (
        _orientation(first_start, first_end, second_start),
        _orientation(first_start, first_end, second_end),
        _orientation(second_start, second_end, first_start),
        _orientation(second_start, second_end, first_end),
    )
    if (orientations[0] > 0 > orientations[1] or orientations[0] < 0 < orientations[1]) and (
        orientations[2] > 0 > orientations[3] or orientations[2] < 0 < orientations[3]
    ):
        return True
    return any(
        _point_on_segment(point, start, end)
        for point, start, end in (
            (second_start, first_start, first_end),
            (second_end, first_start, first_end),
            (first_start, second_start, second_end),
            (first_end, second_start, second_end),
        )
    )


def _validate_simple_polygon(coordinates: list[tuple[float, float]]) -> None:
    area = sum(
        coordinates[index][0] * coordinates[(index + 1) % len(coordinates)][1]
        - coordinates[(index + 1) % len(coordinates)][0] * coordinates[index][1]
        for index in range(len(coordinates))
    )
    if math.isclose(area, 0.0, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("a digitized polygon must enclose a non-zero area")
    edge_count = len(coordinates)
    for first_index in range(edge_count):
        first = (coordinates[first_index], coordinates[(first_index + 1) % edge_count])
        for second_index in range(first_index + 1, edge_count):
            if second_index in {
                first_index,
                (first_index + 1) % edge_count,
                (first_index - 1) % edge_count,
            }:
                continue
            second = (
                coordinates[second_index],
                coordinates[(second_index + 1) % edge_count],
            )
            if _segments_intersect(first[0], first[1], second[0], second[1]):
                raise ValueError("a digitized polygon cannot self-intersect")


def _point_in_polygon(
    point: tuple[float, float], polygon: tuple[tuple[float, float], ...]
) -> bool | None:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if _point_on_segment(point, previous, current):
            return None
        if (current[1] > point[1]) != (previous[1] > point[1]):
            crossing_x = (previous[0] - current[0]) * (point[1] - current[1]) / (
                previous[1] - current[1]
            ) + current[0]
            if point[0] < crossing_x:
                inside = not inside
        previous = current
    return inside


def _cell_center(grid: RasterGrid, row: int, column: int) -> tuple[float, float]:
    x = grid.transform[2] + (column + 0.5) * grid.transform[0] + (row + 0.5) * grid.transform[1]
    y = grid.transform[5] + (column + 0.5) * grid.transform[3] + (row + 0.5) * grid.transform[4]
    return x, y


def _grid_extent(grid: RasterGrid) -> tuple[float, float, float, float]:
    corners = (
        (grid.transform[2], grid.transform[5]),
        (
            grid.transform[2] + grid.width * grid.transform[0],
            grid.transform[5] + grid.width * grid.transform[3],
        ),
        (
            grid.transform[2] + grid.height * grid.transform[1],
            grid.transform[5] + grid.height * grid.transform[4],
        ),
        (
            grid.transform[2] + grid.width * grid.transform[0] + grid.height * grid.transform[1],
            grid.transform[5] + grid.width * grid.transform[3] + grid.height * grid.transform[4],
        ),
    )
    return (
        min(item[0] for item in corners),
        min(item[1] for item in corners),
        max(item[0] for item in corners),
        max(item[1] for item in corners),
    )


def _find_asset(store: EcoProjectStore, asset_id: str) -> ConnectivityAsset:
    try:
        payload = store.get_record("asset", asset_id)
    except KeyError:
        try:
            matches = [
                item for item in store.effective_bundle().assets if item.asset_id == asset_id
            ]
        except (KeyError, ValueError):
            matches = []
        if len(matches) != 1:
            raise DigitizationError(
                "DIGITIZATION_REFERENCE_ASSET_MISSING",
                "Select an existing retained raster as the digitization reference.",
                object_reference=asset_id,
            ) from None
        return matches[0]
    return ConnectivityAsset.model_validate(payload)


def _resource_policy(store: EcoProjectStore) -> ResourcePolicy:
    payload = store.latest_record("resource_policy")
    if payload is None:
        payload = store.project()["skeleton"].get("resource_policy")
    if payload is None:
        raise DigitizationError(
            "DIGITIZATION_RESOURCE_POLICY_REQUIRED",
            "Set a project resource policy before previewing a digitized raster.",
        )
    return ResourcePolicy.model_validate(payload)


def _current_revision(store: EcoProjectStore) -> ModelRevision:
    payload = store.latest_record("revision")
    if payload is None:
        bundles = store.list_records("bundle")
        if bundles:
            payload = bundles[-1].get("revision")
    if payload is None:
        payload = store.project()["skeleton"].get("revision")
    if payload is None:
        raise DigitizationError(
            "DIGITIZATION_MODEL_REVISION_REQUIRED",
            "Create a model revision before publishing digitized assets.",
        )
    return ModelRevision.model_validate(payload)


def _prepare_digitization(
    store: EcoProjectStore, draft: DigitizationDraft
) -> tuple[DigitizationPreview, InMemoryRaster, ConnectivityAsset]:
    reference = _find_asset(store, draft.reference_asset_id)
    if reference.grid is None:
        raise DigitizationError(
            "DIGITIZATION_REFERENCE_NOT_RASTER",
            "The selected reference has no reviewed RasterGrid.",
            object_reference=reference.asset_id,
        )
    try:
        reference_raster = load_raster(store.resolve_asset_path(reference.relative_path), reference)
    except (FileNotFoundError, RasterIOError, ValueError) as exc:
        raise DigitizationError(
            "DIGITIZATION_REFERENCE_BYTES_INVALID",
            "The retained reference raster no longer matches its reviewed bytes and grid.",
            object_reference=reference.asset_id,
        ) from exc
    grid = reference.grid
    if grid.transform[1] != 0 or grid.transform[3] != 0:
        raise DigitizationError(
            "DIGITIZATION_ROTATED_GRID_UNSUPPORTED",
            "Digitization to ESRI ASCII requires an axis-aligned reviewed grid.",
            object_reference=grid.grid_id,
        )
    policy = _resource_policy(store)
    if grid.width * grid.height * 24 > policy.max_materialized_bytes:
        raise DigitizationError(
            "DIGITIZATION_RESOURCE_POLICY_BLOCKED",
            "The proposed raster exceeds the reviewed materialization policy.",
            object_reference=grid.grid_id,
        )

    selected: dict[tuple[int, int], DigitizedCell] = {}

    def add_cell(cell: DigitizedCell) -> None:
        coordinate = (cell.row, cell.column)
        previous = selected.get(coordinate)
        if previous is not None:
            raise DigitizationError(
                "DIGITIZATION_AMBIGUOUS_OVERLAP",
                "Two authored features select the same cell; merge or redraw them explicitly.",
                object_reference=f"row={cell.row},column={cell.column}",
            )
        if not (0 <= cell.row < grid.height and 0 <= cell.column < grid.width):
            raise DigitizationError(
                "DIGITIZATION_CELL_OUT_OF_BOUNDS",
                "A selected cell lies outside the reviewed reference grid.",
                object_reference=f"row={cell.row},column={cell.column}",
            )
        reference_value = reference_raster.values[cell.row][cell.column]
        if _is_nodata(reference_value, grid.nodata):
            raise DigitizationError(
                "DIGITIZATION_NODATA_SELECTED",
                "A selected cell is NODATA in the reference; NODATA topology cannot change.",
                object_reference=f"row={cell.row},column={cell.column}",
            )
        selected[coordinate] = cell

    for cell in draft.cells:
        add_cell(
            DigitizedCell(
                row=cell.row,
                column=cell.column,
                value=cell.value,
                feature_id=cell.feature_id,
                source="CELL",
            )
        )

    extent = _grid_extent(grid)
    for polygon in draft.polygons:
        for vertex in polygon.open_ring:
            if not (extent[0] <= vertex[0] <= extent[2] and extent[1] <= vertex[1] <= extent[3]):
                raise DigitizationError(
                    "DIGITIZATION_POLYGON_OUT_OF_BOUNDS",
                    "Every polygon vertex must remain inside the reviewed reference extent.",
                    object_reference=polygon.feature_id,
                )
        polygon_cells = 0
        for row in range(grid.height):
            for column in range(grid.width):
                containment = _point_in_polygon(_cell_center(grid, row, column), polygon.open_ring)
                if containment is None:
                    raise DigitizationError(
                        "DIGITIZATION_POLYGON_CELL_BOUNDARY_AMBIGUOUS",
                        "A polygon edge crosses a cell center; adjust the geometry before review.",
                        object_reference=polygon.feature_id,
                    )
                if containment:
                    add_cell(
                        DigitizedCell(
                            row=row,
                            column=column,
                            value=polygon.value,
                            feature_id=polygon.feature_id,
                            source="POLYGON",
                        )
                    )
                    polygon_cells += 1
        if polygon_cells == 0:
            raise DigitizationError(
                "DIGITIZATION_POLYGON_SELECTS_NO_CELLS",
                "A polygon does not contain any reference-grid cell centers.",
                object_reference=polygon.feature_id,
            )

    canonical_cells = sorted(selected.values(), key=lambda item: (item.row, item.column))
    _validate_values(draft.kind, [item.value for item in canonical_cells])
    if _is_nodata(0, grid.nodata) or any(
        _is_nodata(item.value, grid.nodata) for item in canonical_cells
    ):
        raise DigitizationError(
            "DIGITIZATION_NODATA_VALUE_COLLISION",
            "The reference NODATA value collides with an authored ID or the required zero background.",
            object_reference=grid.grid_id,
        )
    if (
        draft.kind == DigitizationKind.FOCAL_AREAS
        and len({item.value for item in canonical_cells}) < 2
    ):
        raise DigitizationError(
            "DIGITIZATION_FOCAL_IDS_INCOMPLETE",
            "A focal-area raster requires at least two user-authored positive focal IDs.",
            object_reference=draft.draft_id,
        )

    output_kind = (
        RasterValueKind.IDENTIFIER
        if draft.kind == DigitizationKind.FOCAL_AREAS
        else RasterValueKind.MASK
    )
    output_grid = grid.model_copy(update={"value_kind": output_kind})
    rows: list[tuple[float | int | None, ...]] = []
    nodata_coordinates: list[tuple[int, int]] = []
    for row in range(grid.height):
        output_row: list[float | int | None] = []
        for column in range(grid.width):
            reference_value = reference_raster.values[row][column]
            if _is_nodata(reference_value, grid.nodata):
                output_row.append(grid.nodata)
                nodata_coordinates.append((row, column))
            else:
                cell = selected.get((row, column))
                output_row.append(cell.value if cell is not None else 0)
        rows.append(tuple(output_row))
    output = InMemoryRaster(
        asset_id=f"preview_{draft.draft_id}",
        grid=output_grid,
        values=tuple(rows),
        source_sha256=reference.sha256,
    )
    try:
        materialized = render_esri_ascii(output)
    except RasterIOError as exc:
        raise DigitizationError(
            "DIGITIZATION_ESRI_ASCII_UNSUPPORTED",
            "The reviewed grid cannot be represented losslessly as the required ESRI ASCII asset.",
            object_reference=grid.grid_id,
        ) from exc
    if len(materialized) > policy.max_materialized_bytes:
        raise DigitizationError(
            "DIGITIZATION_RESOURCE_POLICY_BLOCKED",
            "The exact digitized ESRI ASCII asset exceeds the reviewed materialization policy.",
            object_reference=grid.grid_id,
        )
    value_counts = dict(sorted(Counter(str(item.value) for item in canonical_cells).items()))
    data_cell_count = grid.width * grid.height - len(nodata_coordinates)
    materialized_sha256 = hashlib.sha256(materialized).hexdigest()
    topology_sha256 = canonical_sha256(nodata_coordinates)
    hash_payload = {
        "semantics": "reviewer_authored_grid_digitization_v1",
        "draft": draft.model_dump(mode="json"),
        "reference_asset_id": reference.asset_id,
        "reference_asset_sha256": reference.sha256,
        "grid": output_grid.model_dump(mode="json"),
        "grid_identity_sha256": grid_identity(output_grid),
        "affected_cells": [item.model_dump(mode="json") for item in canonical_cells],
        "nodata_topology_sha256": topology_sha256,
        "materialized_sha256": materialized_sha256,
    }
    preview = DigitizationPreview(
        draft_id=draft.draft_id,
        kind=draft.kind,
        reference_asset_id=reference.asset_id,
        reference_asset_sha256=reference.sha256,
        grid=output_grid,
        grid_identity_sha256=grid_identity(output_grid),
        value_kind=output_kind,
        affected_cells=canonical_cells,
        affected_cell_count=len(canonical_cells),
        affected_area_m2=len(canonical_cells) * grid.cell_area_m2,
        value_counts=value_counts,
        explicit_cell_count=len(draft.cells),
        polygon_count=len(draft.polygons),
        background_cell_count=data_cell_count - len(canonical_cells),
        nodata_cell_count=len(nodata_coordinates),
        nodata_topology_sha256=topology_sha256,
        materialized_sha256=materialized_sha256,
        estimated_materialized_bytes=len(materialized),
        preview_sha256=canonical_sha256(hash_payload),
        notices=[
            "Values are reviewer-authored; the worker did not infer ecological meaning.",
            "NODATA cells and reference-grid topology are unchanged.",
            (
                "Current redistribution inside this mask is diagnostic, not predicted animal use."
                if draft.kind == DigitizationKind.RISK_REVIEW_MASK
                else "This footprint does not establish intervention effectiveness or feasibility."
                if draft.kind == DigitizationKind.INTERVENTION_FOOTPRINT
                else "Focal IDs identify reviewed areas; they are not observed animal locations."
            ),
        ],
    )
    return preview, output, reference


def preview_digitization(store: EcoProjectStore, draft: DigitizationDraft) -> DigitizationPreview:
    """Create a deterministic, non-mutating preview from retained reference bytes."""

    preview, _, _ = _prepare_digitization(store, draft)
    return preview


def _exclusive_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def publish_digitization(
    store: EcoProjectStore, params: DigitizationPublishParams
) -> DigitizationPublication:
    """Publish exact reviewed bytes, provenance, and a semantic revision once."""

    preview, output, reference = _prepare_digitization(store, params.draft)
    if preview.preview_sha256 != params.expected_preview_sha256:
        raise DigitizationError(
            "DIGITIZATION_PREVIEW_STALE",
            "The reviewed preview no longer matches the exact draft and retained reference bytes.",
            object_reference=params.draft.draft_id,
        )
    current_revision = _current_revision(store)
    if params.revision_id == current_revision.revision_id:
        raise DigitizationError(
            "DIGITIZATION_REVISION_ID_REUSED",
            "Publishing a digitized asset requires a new semantic revision ID.",
            object_reference=params.revision_id,
        )
    try:
        destination = contained_path(store.root, params.relative_path)
    except PathSecurityError as exc:
        raise DigitizationError(
            "DIGITIZATION_PATH_OUTSIDE_PROJECT",
            "The digitized raster path escapes the project.",
            object_reference=params.relative_path,
        ) from exc
    if destination.exists() or destination.is_symlink():
        raise DigitizationError(
            "DIGITIZATION_DESTINATION_CONFLICT",
            "A file already occupies the immutable digitized raster path.",
            object_reference=params.relative_path,
        )
    materialized = render_esri_ascii(output)
    if hashlib.sha256(materialized).hexdigest() != preview.materialized_sha256:
        raise RuntimeError("digitization materialization changed after exact preview validation")

    asset_kind = {
        DigitizationKind.FOCAL_AREAS: AssetKind.FOCAL_RASTER,
        DigitizationKind.INTERVENTION_FOOTPRINT: AssetKind.ACTION_EFFECT,
        DigitizationKind.RISK_REVIEW_MASK: AssetKind.REVIEW_MASK,
    }[params.draft.kind]
    asset = ConnectivityAsset(
        asset_id=params.asset_id,
        kind=asset_kind,
        title=params.title,
        relative_path=params.relative_path,
        sha256=preview.materialized_sha256,
        byte_size=len(materialized),
        media_type="application/x-esri-ascii-grid",
        source=(f"digitized:{reference.asset_id}:preview:{preview.preview_sha256}"),
        vintage=params.reviewed_at.date().isoformat(),
        license=params.license,
        grid=preview.grid,
        authorization=params.authorization,
    )
    authorization_sha256 = canonical_sha256(params.authorization.model_dump(mode="json"))
    provenance_content = {
        "draft": params.draft,
        "preview_sha256": preview.preview_sha256,
        "reference_asset_id": reference.asset_id,
        "reference_asset_sha256": reference.sha256,
        "nodata_topology_sha256": preview.nodata_topology_sha256,
        "materialized_sha256": preview.materialized_sha256,
        "affected_cell_count": preview.affected_cell_count,
        "value_counts": preview.value_counts,
        "ecological_basis": params.ecological_basis,
        "reviewer": params.reviewer,
        "reviewed_at": params.reviewed_at,
        "authorization_asset_id": params.authorization.asset_id,
        "authorization_sha256": authorization_sha256,
    }
    # Convert the nested contract only at the hashing boundary.  The stored
    # provenance still retains the full authored draft as a typed object.
    provenance_hash_content = {
        **provenance_content,
        "draft": params.draft.model_dump(mode="json"),
    }
    provenance_id = f"digitization_{canonical_sha256(provenance_hash_content)[:20]}"
    provenance_sha256 = canonical_sha256(
        {"provenance_id": provenance_id, **provenance_hash_content}
    )
    provenance = DigitizationProvenance(
        provenance_id=provenance_id,
        provenance_sha256=provenance_sha256,
        **provenance_content,
    )
    changes = [
        SemanticChange(
            object_type="ConnectivityAsset",
            object_id=asset.asset_id,
            field="reviewer_authored_digitization",
            before=reference.asset_id,
            after=asset.asset_id,
            rationale=params.reason,
        ),
        SemanticChange(
            object_type="RasterGrid",
            object_id=preview.grid.grid_id,
            field="value_semantics",
            before=reference.grid.value_kind.value if reference.grid is not None else None,
            after=preview.value_kind.value,
            rationale="Grid geometry and NODATA topology retained; only reviewed cells were authored.",
        ),
        SemanticChange(
            object_type="DigitizationProvenance",
            object_id=provenance.provenance_id,
            field="reviewed_basis_sha256",
            before=None,
            after=provenance.provenance_sha256,
            rationale="Bind the reviewer, ecological basis, authorization, and exact draft to this revision.",
        ),
    ]
    diff_seed = {
        "from_revision_id": current_revision.revision_id,
        "to_revision_id": params.revision_id,
        "changes": [item.model_dump(mode="json") for item in changes],
        "preview_sha256": preview.preview_sha256,
        "asset_sha256": asset.sha256,
        "provenance_sha256": provenance.provenance_sha256,
    }
    diff_id = f"diff_{canonical_sha256(diff_seed)[:20]}"
    semantic_diff = SemanticModelDiff(
        diff_id=diff_id,
        from_revision_id=current_revision.revision_id,
        to_revision_id=params.revision_id,
        changes=changes,
        sha256=canonical_sha256(
            {
                "diff_id": diff_id,
                "from_revision_id": current_revision.revision_id,
                "to_revision_id": params.revision_id,
                "changes": [item.model_dump(mode="json") for item in changes],
            }
        ),
    )
    revision = ModelRevision(
        revision_id=params.revision_id,
        parent_revision_id=current_revision.revision_id,
        project_id=current_revision.project_id,
        created_at=params.reviewed_at,
        created_by=params.reviewer,
        reason=params.reason,
        model_sha256=canonical_sha256(
            {
                "parent_model_sha256": current_revision.model_sha256,
                "semantic_diff_sha256": semantic_diff.sha256,
                "asset_contract_sha256": canonical_sha256(asset.model_dump(mode="json")),
                "preview_sha256": preview.preview_sha256,
                "provenance_sha256": provenance.provenance_sha256,
            }
        ),
        diff_id=semantic_diff.diff_id,
    )

    created = False
    try:
        _exclusive_write(destination, materialized)
        created = True
        store.store_digitization_revision(
            provenance=provenance.model_dump(mode="json"),
            derived_asset=asset.model_dump(mode="json"),
            semantic_diff=semantic_diff.model_dump(mode="json"),
            revision=revision.model_dump(mode="json"),
        )
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return DigitizationPublication(
        preview=preview,
        asset=asset,
        provenance=provenance,
        semantic_diff=semantic_diff,
        revision=revision,
    )
