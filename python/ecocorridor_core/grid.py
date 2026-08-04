"""Raster validation and explicit, reviewable grid alignment.

The module uses a compact in-memory raster representation for validation,
fixtures, and previews.  Production GeoTIFF I/O remains an adapter concern;
none of these functions silently transforms an on-disk source asset.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from datetime import datetime

from pydantic import field_validator, model_validator

from .contracts import (
    ContractModel,
    GridAlignmentApproval,
    GridAlignmentPlan,
    RasterGrid,
    RasterValueKind,
    ResamplingMethod,
    canonical_sha256,
)


class GridMismatchError(ValueError):
    """Raised when exact grid identity is required but not present."""


class AlignmentApprovalRequired(PermissionError):
    """Raised when a requested transformation lacks a matching approval."""


class InMemoryRaster(ContractModel):
    asset_id: str
    grid: RasterGrid
    values: tuple[tuple[float | int | None, ...], ...]
    source_sha256: str

    @field_validator("source_sha256")
    @classmethod
    def validate_source_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_shape_and_values(self) -> InMemoryRaster:
        if len(self.values) != self.grid.height:
            raise ValueError("raster row count does not match its grid")
        if any(len(row) != self.grid.width for row in self.values):
            raise ValueError("raster column count does not match its grid")
        validate_raster_values(self)
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(
            {
                "asset_id": self.asset_id,
                "grid": self.grid.model_dump(mode="json"),
                "values": self.values,
                "source_sha256": self.source_sha256,
            }
        )


def _is_nodata(value: float | None, nodata: float | None) -> bool:
    if value is None:
        return True
    if nodata is None:
        return False
    if isinstance(nodata, float) and math.isnan(nodata):
        return isinstance(value, float) and math.isnan(value)
    return value == nodata


def validate_raster_values(raster: InMemoryRaster) -> None:
    """Validate semantic values without changing or filling a cell."""

    observed_ids: set[int] = set()
    for row in raster.values:
        for value in row:
            if _is_nodata(value, raster.grid.nodata):
                continue
            if value is None or not math.isfinite(float(value)):
                raise ValueError("raster contains a non-finite data value")
            if raster.grid.value_kind == RasterValueKind.CONTINUOUS and float(value) <= 0:
                raise ValueError("resistance raster data cells must be strictly positive")
            if raster.grid.value_kind == RasterValueKind.IDENTIFIER:
                if int(value) != value or int(value) < 0:
                    raise ValueError("identifier raster cells must be non-negative integers")
                if int(value) > 0:
                    observed_ids.add(int(value))
            if raster.grid.value_kind == RasterValueKind.MASK and int(value) not in {0, 1}:
                raise ValueError("mask raster cells must be 0 or 1")
    if raster.grid.value_kind == RasterValueKind.IDENTIFIER and len(observed_ids) < 2:
        raise ValueError("a focal identifier raster must contain at least two positive IDs")


def grid_identity(grid: RasterGrid) -> str:
    # Value kind and NODATA semantics belong to the raster, not to its pixel
    # geometry.  A resistance grid and its focal-ID grid must be able to prove
    # exact cell alignment while retaining different semantic validation.
    return canonical_sha256(
        {
            "grid_id": grid.grid_id,
            "crs": grid.crs,
            "width": grid.width,
            "height": grid.height,
            "transform": grid.transform,
            "cell_area_m2": grid.cell_area_m2,
        }
    )


def assert_same_grid(*rasters: InMemoryRaster) -> str:
    if not rasters:
        raise ValueError("at least one raster is required")
    identity = grid_identity(rasters[0].grid)
    for raster in rasters[1:]:
        if grid_identity(raster.grid) != identity:
            raise GridMismatchError(
                f"{raster.asset_id} is not exactly aligned with {rasters[0].asset_id}; "
                "create and approve a GridAlignmentPlan"
            )
    return identity


def _require_axis_aligned(grid: RasterGrid) -> None:
    if grid.transform[1] != 0 or grid.transform[3] != 0:
        raise ValueError("the v0.1 preview supports axis-aligned affine grids only")


def _source_coordinate(grid: RasterGrid, x: float, y: float) -> tuple[float, float]:
    _require_axis_aligned(grid)
    column = (x - grid.transform[2]) / grid.transform[0] - 0.5
    row = (y - grid.transform[5]) / grid.transform[4] - 0.5
    return row, column


def _cell_center(grid: RasterGrid, row: int, column: int) -> tuple[float, float]:
    _require_axis_aligned(grid)
    x = grid.transform[2] + (column + 0.5) * grid.transform[0]
    y = grid.transform[5] + (row + 0.5) * grid.transform[4]
    return x, y


def _nearest(raster: InMemoryRaster, row: float, column: float) -> float | int | None:
    source_row = math.floor(row + 0.5)
    source_column = math.floor(column + 0.5)
    if not (0 <= source_row < raster.grid.height and 0 <= source_column < raster.grid.width):
        return raster.grid.nodata
    return raster.values[source_row][source_column]


def _bilinear(raster: InMemoryRaster, row: float, column: float) -> float | int | None:
    row0 = math.floor(row)
    column0 = math.floor(column)
    row1 = row0 + 1
    column1 = column0 + 1
    if row0 < 0 or column0 < 0 or row1 >= raster.grid.height or column1 >= raster.grid.width:
        return raster.grid.nodata
    values = (
        raster.values[row0][column0],
        raster.values[row0][column1],
        raster.values[row1][column0],
        raster.values[row1][column1],
    )
    if any(_is_nodata(value, raster.grid.nodata) for value in values):
        return raster.grid.nodata
    dr = row - row0
    dc = column - column0
    top = float(values[0]) * (1 - dc) + float(values[1]) * dc  # type: ignore[arg-type]
    bottom = float(values[2]) * (1 - dc) + float(values[3]) * dc  # type: ignore[arg-type]
    return top * (1 - dr) + bottom * dr


def _resample_values(
    raster: InMemoryRaster,
    target_grid: RasterGrid,
    method: ResamplingMethod,
) -> tuple[tuple[float | int | None, ...], ...]:
    if method == ResamplingMethod.NONE:
        if grid_identity(raster.grid) != grid_identity(target_grid):
            raise GridMismatchError("NONE cannot align different grids")
        return raster.values
    if raster.grid.crs != target_grid.crs:
        raise ValueError(
            "CRS reprojection requires the production geospatial adapter; this preview refuses "
            "to approximate it"
        )
    sample = _nearest if method == ResamplingMethod.NEAREST else _bilinear
    rows: list[tuple[float | int | None, ...]] = []
    for row in range(target_grid.height):
        values: list[float | int | None] = []
        for column in range(target_grid.width):
            x, y = _cell_center(target_grid, row, column)
            source_row, source_column = _source_coordinate(raster.grid, x, y)
            sampled = sample(raster, source_row, source_column)
            values.append(
                target_grid.nodata if _is_nodata(sampled, raster.grid.nodata) else sampled
            )
        rows.append(tuple(values))
    return tuple(rows)


def _nodata_count(values: Iterable[Iterable[float | int | None]], nodata: float | None) -> int:
    return sum(_is_nodata(value, nodata) for row in values for value in row)


def _categorical_counts(
    values: Iterable[Iterable[float | int | None]], nodata: float | None
) -> dict[str, int]:
    counts = Counter(
        str(int(value) if float(value).is_integer() else float(value))
        for row in values
        for value in row
        if value is not None and not _is_nodata(value, nodata)
    )
    return dict(sorted(counts.items()))


def preview_grid_alignment(
    raster: InMemoryRaster,
    target_grid: RasterGrid,
    method: ResamplingMethod,
    *,
    plan_id: str,
) -> GridAlignmentPlan:
    """Compute a deterministic preview; no derived asset is authorized or persisted."""

    if (
        raster.grid.value_kind
        in {
            RasterValueKind.IDENTIFIER,
            RasterValueKind.MASK,
            RasterValueKind.CATEGORICAL,
        }
        and method != ResamplingMethod.NEAREST
    ):
        raise ValueError("identifier, mask, and categorical rasters require nearest-neighbor")
    if raster.grid.value_kind == RasterValueKind.CONTINUOUS and method not in {
        ResamplingMethod.NEAREST,
        ResamplingMethod.BILINEAR,
        ResamplingMethod.NONE,
    }:
        raise ValueError("continuous resistance accepts only nearest or bilinear alignment")
    output = _resample_values(raster, target_grid, method)
    warnings: list[str] = []
    before_nodata = _nodata_count(raster.values, raster.grid.nodata)
    after_nodata = _nodata_count(output, target_grid.nodata)
    if after_nodata != before_nodata:
        warnings.append(f"NODATA cell count changes from {before_nodata} to {after_nodata}.")
    if raster.grid.width * raster.grid.height != target_grid.width * target_grid.height:
        warnings.append(
            "Cell count changes from "
            f"{raster.grid.width * raster.grid.height} to {target_grid.width * target_grid.height}."
        )
    before_counts: dict[str, int] = {}
    after_counts: dict[str, int] = {}
    if raster.grid.value_kind in {
        RasterValueKind.IDENTIFIER,
        RasterValueKind.MASK,
        RasterValueKind.CATEGORICAL,
    }:
        before_counts = _categorical_counts(raster.values, raster.grid.nodata)
        after_counts = _categorical_counts(output, target_grid.nodata)
        if before_counts != after_counts:
            warnings.append("Categorical cell counts change; inspect the preview before approval.")
    output_hash = canonical_sha256(
        {
            "source_sha256": raster.sha256,
            "target_grid": target_grid.model_dump(mode="json"),
            "output": output,
        }
    )
    return GridAlignmentPlan(
        plan_id=plan_id,
        source_asset_id=raster.asset_id,
        source_sha256=raster.sha256,
        source_grid=raster.grid,
        target_grid=target_grid,
        method=method,
        source_cell_count=raster.grid.width * raster.grid.height,
        target_cell_count=target_grid.width * target_grid.height,
        source_nodata_count=before_nodata,
        estimated_target_nodata_count=after_nodata,
        categorical_counts_before=before_counts,
        categorical_counts_after=after_counts,
        resulting_sha256=output_hash,
        warnings=warnings,
    )


def grid_alignment_plan_sha256(plan: GridAlignmentPlan) -> str:
    return canonical_sha256(plan.model_dump(mode="json"))


def approve_grid_alignment(
    plan: GridAlignmentPlan,
    *,
    approval_id: str,
    reviewer: str,
    approved_at: datetime,
    acknowledged_warnings: list[str],
    purpose: str,
) -> GridAlignmentApproval:
    missing = set(plan.warnings) - set(acknowledged_warnings)
    if missing:
        raise ValueError("every alignment warning must be explicitly acknowledged")
    return GridAlignmentApproval(
        approval_id=approval_id,
        plan_id=plan.plan_id,
        plan_sha256=grid_alignment_plan_sha256(plan),
        reviewer=reviewer,
        approved_at=approved_at,
        acknowledged_warnings=acknowledged_warnings,
        purpose=purpose,
    )


def apply_approved_grid_alignment(
    raster: InMemoryRaster,
    plan: GridAlignmentPlan,
    approval: GridAlignmentApproval,
    *,
    derived_asset_id: str,
) -> InMemoryRaster:
    """Materialize only the exact transformation represented by an approval."""

    if approval.plan_id != plan.plan_id or approval.plan_sha256 != grid_alignment_plan_sha256(plan):
        raise AlignmentApprovalRequired("approval does not match the immutable alignment plan")
    if raster.asset_id != plan.source_asset_id or raster.sha256 != plan.source_sha256:
        raise AlignmentApprovalRequired("source raster changed after alignment review")
    values = _resample_values(raster, plan.target_grid, plan.method)
    output_hash = canonical_sha256(
        {
            "source_sha256": raster.sha256,
            "target_grid": plan.target_grid.model_dump(mode="json"),
            "output": values,
        }
    )
    if output_hash != plan.resulting_sha256:
        raise AlignmentApprovalRequired("alignment result differs from the approved preview")
    return InMemoryRaster(
        asset_id=derived_asset_id,
        grid=plan.target_grid,
        values=values,
        source_sha256=output_hash,
    )
