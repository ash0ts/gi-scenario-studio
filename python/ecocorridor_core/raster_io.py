"""Strict raster I/O at the direct Circuitscape execution boundary.

Inputs are read without reprojection, resampling, NODATA filling, or semantic
conversion.  GeoTIFF support is provided by the pinned EcoCorridor packaging
extra; a source build without Rasterio fails explicitly instead of guessing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .contracts import ConnectivityAsset, RasterGrid, RasterValueKind
from .grid import InMemoryRaster, _is_nodata


class RasterIOError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class RasterInspection:
    """Evidence returned from reading staged raster bytes without transformation."""

    format: str
    sha256: str
    byte_size: int
    grid: RasterGrid
    data_cell_count: int
    nodata_cell_count: int
    minimum: float
    maximum: float
    unique_value_count: int

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["grid"] = self.grid.model_dump(mode="json")
        return payload


@dataclass(frozen=True, slots=True)
class CurrentMaskComparison:
    baseline_total: float
    portfolio_total: float
    reviewed_cell_count: int
    included_cell_count: int
    excluded_nodata_cell_count: int
    raster_nodata_cell_count: int
    nodata_topology_sha256: str


def _same_number(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, float) and math.isnan(left):
        return isinstance(right, float) and math.isnan(right)
    if isinstance(right, float) and math.isnan(right):
        return False
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)


def _assert_grid(actual: RasterGrid, expected: RasterGrid, *, source: Path) -> None:
    fields = ("width", "height", "crs", "value_kind")
    for field in fields:
        if getattr(actual, field) != getattr(expected, field):
            raise RasterIOError(f"{source} {field} differs from its reviewed grid contract")
    if any(
        not _same_number(left, right)
        for left, right in zip(actual.transform, expected.transform, strict=True)
    ):
        raise RasterIOError(f"{source} affine transform differs from its reviewed grid contract")
    if not _same_number(actual.nodata, expected.nodata):
        raise RasterIOError(f"{source} NODATA differs from its reviewed grid contract")


def _assert_geometry(actual: RasterGrid, expected: RasterGrid, *, source: Path) -> None:
    if actual.width != expected.width or actual.height != expected.height:
        raise RasterIOError(f"{source} dimensions differ from the configured review grid")
    if any(
        not _same_number(left, right)
        for left, right in zip(actual.transform, expected.transform, strict=True)
    ):
        raise RasterIOError(f"{source} transform differs from the configured review grid")


def _parse_ascii(path: Path) -> tuple[dict[str, float], tuple[tuple[float, ...], ...]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise RasterIOError(f"ESRI ASCII raster is not UTF-8 text: {path}") from exc
    if len(lines) < 7:
        raise RasterIOError(f"ESRI ASCII raster is incomplete: {path}")
    header: dict[str, float] = {}
    body_start = 0
    supported = {
        "ncols",
        "nrows",
        "xllcorner",
        "xllcenter",
        "yllcorner",
        "yllcenter",
        "cellsize",
        "nodata_value",
    }
    for index, line in enumerate(lines[:8]):
        parts = line.split()
        if len(parts) != 2 or parts[0].casefold() not in supported:
            body_start = index
            break
        key = parts[0].casefold()
        if key in header:
            raise RasterIOError(f"duplicate ESRI ASCII header key {key}: {path}")
        try:
            header[key] = float(parts[1])
        except ValueError as exc:
            raise RasterIOError(f"invalid ESRI ASCII header value for {key}: {path}") from exc
        body_start = index + 1
    required = {"ncols", "nrows", "cellsize", "nodata_value"}
    if not required.issubset(header):
        raise RasterIOError(f"ESRI ASCII header omits {sorted(required - set(header))}: {path}")
    if not ({"xllcorner", "xllcenter"} & set(header)) or not (
        {"yllcorner", "yllcenter"} & set(header)
    ):
        raise RasterIOError(f"ESRI ASCII header requires x/y lower-left coordinates: {path}")
    width = int(header["ncols"])
    height = int(header["nrows"])
    if header["ncols"] != width or header["nrows"] != height or width <= 0 or height <= 0:
        raise RasterIOError(f"invalid ESRI ASCII dimensions: {path}")
    rows: list[tuple[float, ...]] = []
    for line in lines[body_start:]:
        if not line.strip():
            continue
        try:
            row = tuple(float(value) for value in line.split())
        except ValueError as exc:
            raise RasterIOError(f"invalid ESRI ASCII data value: {path}") from exc
        if len(row) != width:
            raise RasterIOError(f"ESRI ASCII row width differs from ncols: {path}")
        rows.append(row)
    if len(rows) != height:
        raise RasterIOError(f"ESRI ASCII row count differs from nrows: {path}")
    return header, tuple(rows)


def _summarize(raster: InMemoryRaster, *, format_name: str, path: Path) -> RasterInspection:
    values = [
        float(value)
        for row in raster.values
        for value in row
        if value is not None and not _is_nodata(value, raster.grid.nodata)
    ]
    if not values:
        raise RasterIOError(f"raster contains no data cells: {path}")
    return RasterInspection(
        format=format_name,
        sha256=sha256_file(path),
        byte_size=path.stat().st_size,
        grid=raster.grid,
        data_cell_count=len(values),
        nodata_cell_count=raster.grid.width * raster.grid.height - len(values),
        minimum=min(values),
        maximum=max(values),
        unique_value_count=len(set(values)),
    )


def _ascii_grid(
    header: dict[str, float],
    *,
    grid_id: str,
    value_kind: RasterValueKind,
    crs: str,
    cell_area_m2: float,
) -> RasterGrid:
    if not crs.strip():
        raise RasterIOError("ESRI ASCII inspection requires an explicitly reviewed CRS")
    if not math.isfinite(cell_area_m2) or cell_area_m2 <= 0:
        raise RasterIOError("ESRI ASCII inspection requires a positive reviewed cell area")
    cell = header["cellsize"]
    if not math.isfinite(cell) or cell <= 0:
        raise RasterIOError("ESRI ASCII cellsize must be positive")
    x_corner = header.get("xllcorner")
    if x_corner is None:
        x_corner = header["xllcenter"] - cell / 2
    y_corner = header.get("yllcorner")
    if y_corner is None:
        y_corner = header["yllcenter"] - cell / 2
    height = int(header["nrows"])
    return RasterGrid(
        grid_id=grid_id,
        crs=crs,
        width=int(header["ncols"]),
        height=height,
        transform=(cell, 0.0, x_corner, 0.0, -cell, y_corner + height * cell),
        nodata=header["nodata_value"],
        value_kind=value_kind,
        cell_area_m2=cell_area_m2,
    )


def inspect_raster(
    path: Path,
    *,
    grid_id: str,
    value_kind: RasterValueKind,
    format_hint: str | None = None,
    esri_ascii_crs: str | None = None,
    esri_ascii_cell_area_m2: float | None = None,
    maximum_bytes: int | None = None,
    maximum_cells: int | None = None,
) -> RasterInspection:
    """Inspect staged GeoTIFF/ESRI ASCII bytes without altering them.

    GeoTIFF cell area is accepted only for a projected CRS with known linear
    units.  ESRI ASCII contains no CRS, so both CRS and square-metre cell area
    must be supplied and later preserved in the asset contract.
    """

    if not path.is_file() or path.is_symlink():
        raise RasterIOError(f"raster source is absent or unsafe: {path}")
    if maximum_bytes is not None and path.stat().st_size > maximum_bytes:
        raise RasterIOError("raster exceeds the reviewed project input-byte policy")
    suffix = path.suffix.casefold()
    normalized_hint = format_hint.casefold() if format_hint is not None else ""
    if suffix in {".asc", ".ascii", ".txt"} or normalized_hint == "esri_ascii":
        header, rows = _parse_ascii(path)
        if esri_ascii_crs is None or esri_ascii_cell_area_m2 is None:
            raise RasterIOError(
                "ESRI ASCII inspection requires reviewed CRS and cell_area_m2 values"
            )
        grid = _ascii_grid(
            header,
            grid_id=grid_id,
            value_kind=value_kind,
            crs=esri_ascii_crs,
            cell_area_m2=esri_ascii_cell_area_m2,
        )
        if maximum_cells is not None and grid.width * grid.height > maximum_cells:
            raise RasterIOError("raster cell count exceeds the reviewed materialization policy")
        raster = InMemoryRaster(
            asset_id="inspection",
            grid=grid,
            values=rows,
            source_sha256=sha256_file(path),
        )
        return _summarize(raster, format_name="ESRI_ASCII", path=path)
    if suffix not in {".tif", ".tiff"} and normalized_hint != "geotiff":
        raise RasterIOError("raster inspection supports only GeoTIFF or ESRI ASCII")
    try:
        import rasterio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - lean source installation
        raise RasterIOError(
            "GeoTIFF inspection requires the pinned EcoCorridor rasterio packaging extra"
        ) from exc
    with rasterio.open(path) as source:
        if source.count != 1:
            raise RasterIOError("GeoTIFF must contain exactly one reviewed raster band")
        if source.crs is None:
            raise RasterIOError("GeoTIFF has no CRS")
        if not source.crs.is_projected:
            raise RasterIOError(
                "GeoTIFF must use a reviewed projected CRS before cell_area_m2 can be asserted"
            )
        if maximum_cells is not None and source.width * source.height > maximum_cells:
            raise RasterIOError("raster cell count exceeds the reviewed materialization policy")
        try:
            linear_factor = float(source.crs.linear_units_factor[1])
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            raise RasterIOError("GeoTIFF CRS has no supported linear-unit conversion") from exc
        transform = source.transform
        map_unit_area = abs(transform.a * transform.e - transform.b * transform.d)
        cell_area_m2 = map_unit_area * linear_factor * linear_factor
        grid = RasterGrid(
            grid_id=grid_id,
            crs=source.crs.to_string(),
            width=source.width,
            height=source.height,
            transform=(
                transform.a,
                transform.b,
                transform.c,
                transform.d,
                transform.e,
                transform.f,
            ),
            nodata=source.nodata,
            value_kind=value_kind,
            cell_area_m2=cell_area_m2,
        )
        array = source.read(1, masked=False)
    rows = tuple(
        tuple(value.item() if hasattr(value, "item") else value for value in row) for row in array
    )
    raster = InMemoryRaster(
        asset_id="inspection",
        grid=grid,
        values=rows,
        source_sha256=sha256_file(path),
    )
    return _summarize(raster, format_name="GEOTIFF", path=path)


def load_esri_ascii(path: Path, asset: ConnectivityAsset) -> InMemoryRaster:
    if asset.grid is None:
        raise RasterIOError(f"raster asset {asset.asset_id} has no reviewed grid")
    header, rows = _parse_ascii(path)
    cell = header["cellsize"]
    if not math.isfinite(cell) or cell <= 0:
        raise RasterIOError(f"ESRI ASCII cellsize must be positive: {path}")
    x_corner = header.get("xllcorner")
    if x_corner is None:
        x_corner = header["xllcenter"] - cell / 2
    y_corner = header.get("yllcorner")
    if y_corner is None:
        y_corner = header["yllcenter"] - cell / 2
    height = int(header["nrows"])
    actual = asset.grid.model_copy(
        update={
            "width": int(header["ncols"]),
            "height": height,
            "transform": (cell, 0.0, x_corner, 0.0, -cell, y_corner + height * cell),
            "nodata": header["nodata_value"],
        }
    )
    _assert_grid(actual, asset.grid, source=path)
    return InMemoryRaster(
        asset_id=asset.asset_id,
        grid=asset.grid,
        values=rows,
        source_sha256=sha256_file(path),
    )


def load_geotiff(path: Path, asset: ConnectivityAsset) -> InMemoryRaster:
    if asset.grid is None:
        raise RasterIOError(f"raster asset {asset.asset_id} has no reviewed grid")
    try:
        import rasterio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised by lean source installations
        raise RasterIOError(
            "GeoTIFF execution requires the pinned EcoCorridor rasterio packaging extra"
        ) from exc
    with rasterio.open(path) as source:
        if source.count != 1:
            raise RasterIOError(f"GeoTIFF must contain exactly one reviewed raster band: {path}")
        if source.crs is None:
            raise RasterIOError(f"GeoTIFF has no CRS: {path}")
        try:
            expected_crs = rasterio.crs.CRS.from_user_input(asset.grid.crs)
        except Exception as exc:
            raise RasterIOError(f"reviewed grid CRS cannot be parsed: {asset.grid.crs}") from exc
        if source.crs != expected_crs:
            raise RasterIOError(f"{path} CRS differs from its reviewed grid contract")
        transform = source.transform
        actual = asset.grid.model_copy(
            update={
                "width": source.width,
                "height": source.height,
                "crs": asset.grid.crs,
                "transform": (
                    transform.a,
                    transform.b,
                    transform.c,
                    transform.d,
                    transform.e,
                    transform.f,
                ),
                "nodata": source.nodata,
            }
        )
        _assert_grid(actual, asset.grid, source=path)
        array = source.read(1, masked=False)
    rows = tuple(
        tuple(value.item() if hasattr(value, "item") else value for value in row) for row in array
    )
    return InMemoryRaster(
        asset_id=asset.asset_id,
        grid=asset.grid,
        values=rows,
        source_sha256=sha256_file(path),
    )


def load_raster(path: Path, asset: ConnectivityAsset) -> InMemoryRaster:
    if not path.is_file() or path.is_symlink():
        raise RasterIOError(f"raster source is absent or unsafe: {path}")
    if sha256_file(path) != asset.sha256 or path.stat().st_size != asset.byte_size:
        raise RasterIOError(f"raster source differs from reviewed bytes: {asset.asset_id}")
    suffix = path.suffix.casefold()
    media_type = asset.media_type.casefold()
    if suffix in {".asc", ".ascii", ".txt"} or "esri-ascii" in media_type:
        return load_esri_ascii(path, asset)
    if suffix in {".tif", ".tiff"} or media_type in {"image/tiff", "image/geotiff"}:
        return load_geotiff(path, asset)
    raise RasterIOError(
        f"unsupported direct raster format for {asset.asset_id}; use normalized ESRI ASCII or GeoTIFF"
    )


def inspect_reviewed_raster(path: Path, asset: ConnectivityAsset) -> RasterInspection:
    raster = load_raster(path, asset)
    suffix = path.suffix.casefold()
    format_name = (
        "GEOTIFF"
        if suffix in {".tif", ".tiff"}
        or asset.media_type.casefold() in {"image/tiff", "image/geotiff"}
        else "ESRI_ASCII"
    )
    return _summarize(raster, format_name=format_name, path=path)


def render_esri_ascii(raster: InMemoryRaster) -> bytes:
    transform = raster.grid.transform
    if transform[1] != 0 or transform[3] != 0:
        raise RasterIOError("Circuitscape ESRI ASCII materialization requires an axis-aligned grid")
    if transform[0] <= 0 or transform[4] >= 0 or not _same_number(transform[0], -transform[4]):
        raise RasterIOError(
            "Circuitscape ESRI ASCII materialization requires square north-up cells"
        )
    if raster.grid.nodata is None:
        raise RasterIOError("Circuitscape ESRI ASCII materialization requires explicit NODATA")
    y_lower = transform[5] + raster.grid.height * transform[4]

    def number(value: Any) -> str:
        return f"{float(value):.12g}"

    lines = [
        f"ncols {raster.grid.width}",
        f"nrows {raster.grid.height}",
        f"xllcorner {number(transform[2])}",
        f"yllcorner {number(y_lower)}",
        f"cellsize {number(transform[0])}",
        f"NODATA_value {number(raster.grid.nodata)}",
    ]
    for row in raster.values:
        lines.append(
            " ".join(
                number(raster.grid.nodata if _is_nodata(value, raster.grid.nodata) else value)
                for value in row
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_esri_ascii(path: Path, raster: InMemoryRaster) -> str:
    payload = render_esri_ascii(raster)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    return sha256_file(path)


def current_map_values_and_nodata(
    path: Path, *, expected_grid: RasterGrid | None = None
) -> tuple[tuple[tuple[float, ...], ...], float]:
    """Read a current raster while retaining its declared NODATA sentinel.

    Connectivity-current rasters are engine outputs rather than reviewed model
    assets.  Callers still need the output's own NODATA value so it can never be
    mistaken for a very large (or negative) current during a diagnostic sum.
    """

    header, values = _parse_ascii(path)
    if expected_grid is not None:
        cell = header["cellsize"]
        x_corner = header.get("xllcorner")
        if x_corner is None:
            x_corner = header["xllcenter"] - cell / 2
        y_corner = header.get("yllcorner")
        if y_corner is None:
            y_corner = header["yllcenter"] - cell / 2
        height = int(header["nrows"])
        actual = expected_grid.model_copy(
            update={
                "width": int(header["ncols"]),
                "height": height,
                "transform": (cell, 0.0, x_corner, 0.0, -cell, y_corner + height * cell),
                "nodata": header["nodata_value"],
            }
        )
        _assert_geometry(actual, expected_grid, source=path)
    return values, header["nodata_value"]


def current_map_values(
    path: Path, *, expected_grid: RasterGrid | None = None
) -> tuple[tuple[float, ...], ...]:
    values, _ = current_map_values_and_nodata(path, expected_grid=expected_grid)
    return values


def compare_current_maps_in_mask(
    baseline_path: Path,
    portfolio_path: Path,
    mask: InMemoryRaster,
) -> CurrentMaskComparison:
    """Compare two current rasters only after proving identical NODATA topology."""

    baseline, baseline_nodata = current_map_values_and_nodata(
        baseline_path, expected_grid=mask.grid
    )
    portfolio, portfolio_nodata = current_map_values_and_nodata(
        portfolio_path, expected_grid=mask.grid
    )

    def declared_nodata(value: float, sentinel: float) -> bool:
        return (math.isnan(sentinel) and math.isnan(value)) or math.isclose(
            value, sentinel, rel_tol=0.0, abs_tol=1e-9
        )

    topology: list[bool] = []
    baseline_total = 0.0
    portfolio_total = 0.0
    reviewed = 0
    included = 0
    excluded = 0
    for row in range(mask.grid.height):
        for column in range(mask.grid.width):
            baseline_value = baseline[row][column]
            portfolio_value = portfolio[row][column]
            baseline_missing = declared_nodata(baseline_value, baseline_nodata)
            portfolio_missing = declared_nodata(portfolio_value, portfolio_nodata)
            if baseline_missing != portfolio_missing:
                raise RasterIOError(
                    "baseline and portfolio current rasters have different NODATA topology"
                )
            topology.append(baseline_missing)
            if not baseline_missing and (
                not math.isfinite(baseline_value)
                or not math.isfinite(portfolio_value)
                or baseline_value < 0
                or portfolio_value < 0
            ):
                raise RasterIOError("current raster contains nonfinite or negative data")
            mask_value = mask.values[row][column]
            if _is_nodata(mask_value, mask.grid.nodata):
                continue
            if mask_value not in {0, 1}:
                raise RasterIOError("review mask contains a value other than zero or one")
            if int(mask_value) != 1:
                continue
            reviewed += 1
            if baseline_missing:
                excluded += 1
                continue
            included += 1
            baseline_total += baseline_value
            portfolio_total += portfolio_value
    if reviewed == 0:
        raise RasterIOError("review mask contains no reviewed cells")
    if included == 0:
        raise RasterIOError("review mask contains no common valid current cells")
    topology_sha256 = hashlib.sha256(
        json.dumps(topology, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CurrentMaskComparison(
        baseline_total=baseline_total,
        portfolio_total=portfolio_total,
        reviewed_cell_count=reviewed,
        included_cell_count=included,
        excluded_nodata_cell_count=excluded,
        raster_nodata_cell_count=sum(topology),
        nodata_topology_sha256=topology_sha256,
    )
