"""Fail-closed proof that reviewed intervention footprints cover replacement cells.

An action-effect reference is not itself evidence that its replacement cells lie
inside the reviewed footprint.  This module resolves the immutable footprint
bytes and proves coverage using one of two explicit, normalized semantics:

* a grid-aligned binary mask raster whose value is ``1`` for the complete cell;
* a single-layer GeoPackage containing valid polygonal geometry that covers the
  complete grid-cell polygon in the scenario CRS.

No reprojection, rasterization, resampling, geometry repair, or centre-point
test is performed here.  If exact coverage cannot be established, preparation
of the direct Circuitscape job must stop.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from .contracts import (
    ActionEffectRaster,
    AssetKind,
    ConnectivityAsset,
    ContractModel,
    RasterGrid,
    RasterValueKind,
    canonical_sha256,
)
from .grid import InMemoryRaster, _is_nodata, assert_same_grid, grid_identity
from .interventions import action_effect_sha256
from .raster_io import load_esri_ascii, load_raster


class FootprintCoverageError(ValueError):
    """The retained reviewed footprint cannot prove an action-cell boundary."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class FootprintCoverageProof(ContractModel):
    """Immutable proof projection retained with prepared-run provenance."""

    effect_id: str
    effect_sha256: str
    footprint_asset_id: str
    footprint_asset_sha256: str
    scenario_grid_identity_sha256: str
    semantics: Literal[
        "GRID_ALIGNED_BINARY_MASK_CELL_EQUALS_ONE_V1",
        "GEOPACKAGE_POLYGON_COVERS_COMPLETE_GRID_CELL_V1",
    ]
    covered_cells: tuple[tuple[int, int], ...] = Field(min_length=1)
    proof_sha256: str


class RetainedFootprintSource(ContractModel):
    """A normalized mask file bound to its reviewed logical asset contract."""

    footprint_asset_id: str
    reviewed_source_relative_path: str
    reviewed_source_sha256: str
    normalized_relative_path: str
    normalized_file_sha256: str
    normalized_logical_values_sha256: str
    grid: RasterGrid
    grid_identity_sha256: str
    semantics: Literal["NORMALIZED_ESRI_ASCII_BINARY_MASK_V1"] = (
        "NORMALIZED_ESRI_ASCII_BINARY_MASK_V1"
    )

    @model_validator(mode="after")
    def validate_identity(self) -> RetainedFootprintSource:
        for relative in (self.reviewed_source_relative_path, self.normalized_relative_path):
            path = PurePosixPath(relative)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("retained footprint path must be a contained relative path")
        for value in (
            self.reviewed_source_sha256,
            self.normalized_file_sha256,
            self.normalized_logical_values_sha256,
            self.grid_identity_sha256,
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("retained footprint identities must be lowercase SHA-256 digests")
        if self.grid.value_kind != RasterValueKind.MASK:
            raise ValueError("retained footprint sources require binary-mask grid semantics")
        if grid_identity(self.grid) != self.grid_identity_sha256:
            raise ValueError("retained footprint grid identity does not match its grid contract")
        return self


class FootprintProofDocument(ContractModel):
    """Canonical per-job authorization record retained beside direct evidence."""

    schema_version: Literal["ecocorridor-footprint-proofs/v1.0"] = (
        "ecocorridor-footprint-proofs/v1.0"
    )
    project_id: str
    revision_id: str
    portfolio_id: str
    scenario_id: str
    scenario_grid: RasterGrid
    scenario_grid_identity_sha256: str
    footprint_proof_sha256: str
    proofs: tuple[FootprintCoverageProof, ...]
    source_masks: tuple[RetainedFootprintSource, ...]
    document_content_sha256: str

    def content(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"document_content_sha256"})

    @model_validator(mode="after")
    def validate_document(self) -> FootprintProofDocument:
        if canonical_sha256(self.content()) != self.document_content_sha256:
            raise ValueError("footprint proof document content hash does not match")
        if footprint_proof_set_sha256(self.proofs) != self.footprint_proof_sha256:
            raise ValueError("footprint proof set hash does not match")
        if grid_identity(self.scenario_grid) != self.scenario_grid_identity_sha256:
            raise ValueError("footprint proof scenario-grid identity does not match")
        if any(
            proof.scenario_grid_identity_sha256 != self.scenario_grid_identity_sha256
            for proof in self.proofs
        ):
            raise ValueError("footprint proof uses another scenario grid")
        proof_assets = [proof.footprint_asset_id for proof in self.proofs]
        source_assets = [source.footprint_asset_id for source in self.source_masks]
        if len(source_assets) != len(set(source_assets)) or set(proof_assets) != set(source_assets):
            raise ValueError("footprint proof sources must exactly cover referenced assets")
        return self


class ReviewedFootprintEvidenceSource(ContractModel):
    """Exact reviewed bytes supporting a professional footprint proof."""

    asset: ConnectivityAsset
    file_sha256: str
    byte_size: int = Field(ge=1)
    proof_semantics: Literal[
        "GRID_ALIGNED_BINARY_MASK_CELL_EQUALS_ONE_V1",
        "GEOPACKAGE_POLYGON_COVERS_COMPLETE_GRID_CELL_V1",
    ]

    @model_validator(mode="after")
    def binds_asset_bytes(self) -> ReviewedFootprintEvidenceSource:
        if self.file_sha256 != self.asset.sha256 or self.byte_size != self.asset.byte_size:
            raise ValueError("professional footprint source differs from reviewed asset bytes")
        return self


class ProfessionalFootprintProofDocument(ContractModel):
    schema_version: Literal["ecocorridor-professional-footprint-proofs/v1.0"] = (
        "ecocorridor-professional-footprint-proofs/v1.0"
    )
    project_id: str
    revision_id: str
    portfolio_id: str
    scenario_id: str
    scenario_grid: RasterGrid
    scenario_grid_identity_sha256: str
    footprint_proof_sha256: str
    proofs: tuple[FootprintCoverageProof, ...]
    sources: tuple[ReviewedFootprintEvidenceSource, ...]
    document_content_sha256: str

    def content(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"document_content_sha256"})

    @model_validator(mode="after")
    def validate_document(self) -> ProfessionalFootprintProofDocument:
        if canonical_sha256(self.content()) != self.document_content_sha256:
            raise ValueError("professional footprint proof document content hash differs")
        if footprint_proof_set_sha256(self.proofs) != self.footprint_proof_sha256:
            raise ValueError("professional footprint proof set hash differs")
        if grid_identity(self.scenario_grid) != self.scenario_grid_identity_sha256:
            raise ValueError("professional footprint scenario grid identity differs")
        proof_assets = [item.footprint_asset_id for item in self.proofs]
        source_assets = [item.asset.asset_id for item in self.sources]
        if len(source_assets) != len(set(source_assets)) or set(proof_assets) != set(source_assets):
            raise ValueError("professional footprint sources do not exactly cover proofs")
        semantics = {item.asset.asset_id: item.proof_semantics for item in self.sources}
        if any(semantics[item.footprint_asset_id] != item.semantics for item in self.proofs):
            raise ValueError("professional footprint source semantics differ from proof")
        return self


def create_professional_footprint_proof_document(
    *,
    project_id: str,
    revision_id: str,
    portfolio_id: str,
    scenario_id: str,
    baseline: InMemoryRaster,
    proofs: Iterable[FootprintCoverageProof],
    assets: Iterable[ConnectivityAsset],
    source_paths: dict[str, Path],
) -> ProfessionalFootprintProofDocument:
    proof_items = tuple(sorted(proofs, key=lambda item: item.effect_id))
    assets_by_id = {item.asset_id: item for item in assets}
    sources = []
    for asset_id in sorted({item.footprint_asset_id for item in proof_items}):
        asset = assets_by_id[asset_id]
        source = source_paths[asset_id]
        if not source.is_file() or source.is_symlink():
            raise FootprintCoverageError(f"reviewed footprint source is absent: {asset_id}")
        sources.append(
            ReviewedFootprintEvidenceSource(
                asset=asset,
                file_sha256=_sha256_file(source),
                byte_size=source.stat().st_size,
                proof_semantics=next(
                    item.semantics for item in proof_items if item.footprint_asset_id == asset_id
                ),
            )
        )
    content = {
        "schema_version": "ecocorridor-professional-footprint-proofs/v1.0",
        "project_id": project_id,
        "revision_id": revision_id,
        "portfolio_id": portfolio_id,
        "scenario_id": scenario_id,
        "scenario_grid": baseline.grid.model_dump(mode="json"),
        "scenario_grid_identity_sha256": grid_identity(baseline.grid),
        "footprint_proof_sha256": footprint_proof_set_sha256(proof_items),
        "proofs": [item.model_dump(mode="json") for item in proof_items],
        "sources": [item.model_dump(mode="json") for item in sources],
    }
    return ProfessionalFootprintProofDocument(
        **content,
        document_content_sha256=canonical_sha256(content),
    )


def verify_professional_footprint_proof_document(
    document: ProfessionalFootprintProofDocument,
    *,
    expected_proofs: Iterable[FootprintCoverageProof],
    source_paths: dict[str, Path],
) -> str:
    expected = tuple(sorted(expected_proofs, key=lambda item: item.effect_id))
    if document.proofs != expected:
        raise FootprintCoverageError("professional footprint proof records changed")
    for source in document.sources:
        path = source_paths[source.asset.asset_id]
        if (
            not path.is_file()
            or path.is_symlink()
            or _sha256_file(path) != source.file_sha256
            or path.stat().st_size != source.byte_size
        ):
            raise FootprintCoverageError(
                f"professional footprint source bytes changed: {source.asset.asset_id}"
            )
    return canonical_sha256(document.content())


def _proof(
    effect: ActionEffectRaster,
    asset: ConnectivityAsset,
    baseline: InMemoryRaster,
    *,
    semantics: Literal[
        "GRID_ALIGNED_BINARY_MASK_CELL_EQUALS_ONE_V1",
        "GEOPACKAGE_POLYGON_COVERS_COMPLETE_GRID_CELL_V1",
    ],
) -> FootprintCoverageProof:
    coordinates = tuple(sorted((cell.row, cell.column) for cell in effect.cells))
    content = {
        "effect_id": effect.effect_id,
        "effect_sha256": effect.sha256,
        "footprint_asset_id": asset.asset_id,
        "footprint_asset_sha256": asset.sha256,
        "scenario_grid_identity_sha256": grid_identity(baseline.grid),
        "semantics": semantics,
        "covered_cells": coordinates,
    }
    return FootprintCoverageProof(**content, proof_sha256=canonical_sha256(content))


def _prove_mask(
    effect: ActionEffectRaster,
    asset: ConnectivityAsset,
    baseline: InMemoryRaster,
    footprint: InMemoryRaster,
) -> FootprintCoverageProof:
    if asset.kind != AssetKind.ACTION_EFFECT:
        raise FootprintCoverageError(
            f"footprint {asset.asset_id} is not a normalized action-footprint mask"
        )
    if asset.grid is None or asset.grid.value_kind != RasterValueKind.MASK:
        raise FootprintCoverageError(
            f"footprint {asset.asset_id} lacks reviewed binary-mask grid semantics"
        )
    try:
        assert_same_grid(baseline, footprint)
    except ValueError as exc:
        raise FootprintCoverageError(
            f"footprint {asset.asset_id} is not exactly aligned with the scenario grid"
        ) from exc
    for replacement in effect.cells:
        coordinate = (replacement.row, replacement.column)
        if replacement.row >= footprint.grid.height or replacement.column >= footprint.grid.width:
            raise FootprintCoverageError(
                f"effect {effect.effect_id} cell {coordinate} lies outside its footprint grid"
            )
        value = footprint.values[replacement.row][replacement.column]
        if _is_nodata(value, footprint.grid.nodata) or value != 1:
            raise FootprintCoverageError(
                f"effect {effect.effect_id} cell {coordinate} is outside reviewed footprint "
                f"{asset.asset_id}"
            )
    return _proof(
        effect,
        asset,
        baseline,
        semantics="GRID_ALIGNED_BINARY_MASK_CELL_EQUALS_ONE_V1",
    )


def _cell_polygon(grid: object, row: int, column: int):
    try:
        from shapely.geometry import Polygon
    except ImportError as exc:  # pragma: no cover - packaging gate supplies Shapely
        raise FootprintCoverageError(
            "GeoPackage footprint validation requires the pinned geospatial runtime"
        ) from exc
    transform = grid.transform

    def point(pixel_column: int, pixel_row: int) -> tuple[float, float]:
        return (
            transform[0] * pixel_column + transform[1] * pixel_row + transform[2],
            transform[3] * pixel_column + transform[4] * pixel_row + transform[5],
        )

    return Polygon(
        [
            point(column, row),
            point(column + 1, row),
            point(column + 1, row + 1),
            point(column, row + 1),
        ]
    )


def _prove_geopackage(
    effect: ActionEffectRaster,
    asset: ConnectivityAsset,
    baseline: InMemoryRaster,
    path: Path,
) -> FootprintCoverageProof:
    if asset.kind != AssetKind.VECTOR_CONTEXT:
        raise FootprintCoverageError(
            f"footprint {asset.asset_id} is not a normalized vector footprint"
        )
    if path.suffix.casefold() != ".gpkg" or asset.media_type.casefold() not in {
        "application/geopackage+sqlite3",
        "application/vnd.sqlite3",
    }:
        raise FootprintCoverageError(
            f"footprint {asset.asset_id} must be a normalized single-layer GeoPackage"
        )
    try:
        import geopandas
        import pyogrio
        from pyproj import CRS
    except ImportError as exc:  # pragma: no cover - packaging gate supplies geo dependencies
        raise FootprintCoverageError(
            "GeoPackage footprint validation requires the pinned geospatial runtime"
        ) from exc
    try:
        layers = pyogrio.list_layers(path)
        if len(layers) != 1:
            raise FootprintCoverageError(
                f"footprint {asset.asset_id} must contain exactly one reviewed vector layer"
            )
        layer_name = str(layers[0][0])
        frame = geopandas.read_file(path, layer=layer_name, engine="pyogrio")
        if frame.crs is None or CRS.from_user_input(frame.crs) != CRS.from_user_input(
            baseline.grid.crs
        ):
            raise FootprintCoverageError(
                f"footprint {asset.asset_id} CRS differs from the scenario grid"
            )
    except FootprintCoverageError:
        raise
    except Exception as exc:
        raise FootprintCoverageError(
            f"footprint {asset.asset_id} GeoPackage could not be read without transformation"
        ) from exc
    if frame.empty:
        raise FootprintCoverageError(f"footprint {asset.asset_id} contains no reviewed geometry")
    geometries = list(frame.geometry)
    if any(
        geometry is None
        or geometry.is_empty
        or not geometry.is_valid
        or geometry.geom_type not in {"Polygon", "MultiPolygon"}
        for geometry in geometries
    ):
        raise FootprintCoverageError(
            f"footprint {asset.asset_id} contains empty, invalid, or non-polygon geometry"
        )
    coverage = frame.geometry.union_all()
    for replacement in effect.cells:
        coordinate = (replacement.row, replacement.column)
        if replacement.row >= baseline.grid.height or replacement.column >= baseline.grid.width:
            raise FootprintCoverageError(
                f"effect {effect.effect_id} cell {coordinate} lies outside the scenario grid"
            )
        if not coverage.covers(_cell_polygon(baseline.grid, *coordinate)):
            raise FootprintCoverageError(
                f"effect {effect.effect_id} cell {coordinate} is not completely covered by "
                f"reviewed footprint {asset.asset_id}"
            )
    return _proof(
        effect,
        asset,
        baseline,
        semantics="GEOPACKAGE_POLYGON_COVERS_COMPLETE_GRID_CELL_V1",
    )


def prove_action_effect_footprints(
    baseline: InMemoryRaster,
    effects: Iterable[ActionEffectRaster],
    assets: Iterable[ConnectivityAsset],
    *,
    resolve_path: Callable[[ConnectivityAsset], Path] | None = None,
    normalized_rasters: dict[str, InMemoryRaster] | None = None,
) -> list[FootprintCoverageProof]:
    """Prove every replacement cell is inside its immutable reviewed footprint.

    ``resolve_path`` must return the retained source file after enforcing the
    asset's path, byte-size, symlink, and SHA-256 boundary.  The only alternate
    source is ``normalized_rasters``, used by the signed synthetic fixture: its
    raster must exactly match the asset contract's grid and content digest.
    A caller cannot substitute a renderer-provided geometry or an unreviewed
    derived mask.
    """

    assets_by_id = {asset.asset_id: asset for asset in assets}
    proofs: list[FootprintCoverageProof] = []
    for effect in effects:
        asset = assets_by_id.get(effect.footprint_asset_id)
        if asset is None:
            raise FootprintCoverageError(
                f"effect {effect.effect_id} references an unavailable reviewed footprint"
            )
        if asset.kind == AssetKind.ACTION_EFFECT:
            normalized = (normalized_rasters or {}).get(asset.asset_id)
            if normalized is not None:
                if (
                    normalized.asset_id != asset.asset_id
                    or normalized.grid != asset.grid
                    or normalized.source_sha256 != asset.sha256
                ):
                    raise FootprintCoverageError(
                        f"normalized footprint {asset.asset_id} differs from its asset contract"
                    )
                footprint = normalized
            else:
                if resolve_path is None:
                    raise FootprintCoverageError(
                        f"retained footprint bytes are unavailable for {asset.asset_id}"
                    )
                try:
                    footprint = load_raster(resolve_path(asset), asset)
                except Exception as exc:
                    raise FootprintCoverageError(
                        f"retained footprint bytes are unavailable for {asset.asset_id}"
                    ) from exc
            proof = _prove_mask(effect, asset, baseline, footprint)
        elif asset.kind == AssetKind.VECTOR_CONTEXT:
            if resolve_path is None:
                raise FootprintCoverageError(
                    f"retained footprint bytes are unavailable for {asset.asset_id}"
                )
            try:
                path = resolve_path(asset)
            except Exception as exc:
                raise FootprintCoverageError(
                    f"retained footprint bytes are unavailable for {asset.asset_id}"
                ) from exc
            proof = _prove_geopackage(effect, asset, baseline, path)
        else:
            raise FootprintCoverageError(
                f"footprint {asset.asset_id} has unsupported kind {asset.kind.value}"
            )
        proofs.append(proof)
    return sorted(proofs, key=lambda item: (item.effect_id, item.footprint_asset_id))


def footprint_proof_set_sha256(proofs: Iterable[FootprintCoverageProof]) -> str:
    """Return an order-independent digest for prepared-run evidence provenance."""

    return canonical_sha256(
        [
            item.model_dump(mode="json")
            for item in sorted(proofs, key=lambda item: item.proof_sha256)
        ]
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_footprint_proof_document_bytes(document: FootprintProofDocument) -> bytes:
    """Serialize a proof record deterministically for hashing and publication."""

    return (json.dumps(document.model_dump(mode="json"), sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def create_footprint_proof_document(
    *,
    project_id: str,
    revision_id: str,
    portfolio_id: str,
    scenario_id: str,
    baseline: InMemoryRaster,
    proofs: Iterable[FootprintCoverageProof],
    assets: Iterable[ConnectivityAsset],
    source_paths: dict[str, Path],
    evidence_root: Path,
) -> FootprintProofDocument:
    """Bind every proof to the exact normalized mask bytes retained in evidence."""

    root = evidence_root.resolve()
    ordered_proofs = tuple(sorted(proofs, key=lambda item: (item.effect_id, item.proof_sha256)))
    assets_by_id = {item.asset_id: item for item in assets}
    sources: list[RetainedFootprintSource] = []
    for asset_id in sorted({item.footprint_asset_id for item in ordered_proofs}):
        asset = assets_by_id.get(asset_id)
        source = source_paths.get(asset_id)
        if asset is None or asset.grid is None or source is None:
            raise FootprintCoverageError(f"retained footprint source is absent for {asset_id}")
        source = source.resolve()
        if root not in source.parents or not source.is_file() or source.is_symlink():
            raise FootprintCoverageError(f"retained footprint source is unsafe for {asset_id}")
        sources.append(
            RetainedFootprintSource(
                footprint_asset_id=asset_id,
                reviewed_source_relative_path=(
                    PurePosixPath("sources") / PurePosixPath(asset.relative_path)
                ).as_posix(),
                reviewed_source_sha256=asset.sha256,
                normalized_relative_path=source.relative_to(root).as_posix(),
                normalized_file_sha256=_file_sha256(source),
                normalized_logical_values_sha256=asset.sha256,
                grid=asset.grid,
                grid_identity_sha256=grid_identity(asset.grid),
            )
        )
    content: dict[str, object] = {
        "schema_version": "ecocorridor-footprint-proofs/v1.0",
        "project_id": project_id,
        "revision_id": revision_id,
        "portfolio_id": portfolio_id,
        "scenario_id": scenario_id,
        "scenario_grid": baseline.grid.model_dump(mode="json"),
        "scenario_grid_identity_sha256": grid_identity(baseline.grid),
        "footprint_proof_sha256": footprint_proof_set_sha256(ordered_proofs),
        "proofs": [item.model_dump(mode="json") for item in ordered_proofs],
        "source_masks": [item.model_dump(mode="json") for item in sources],
    }
    return FootprintProofDocument(
        **content,
        document_content_sha256=canonical_sha256(content),
    )


def verify_footprint_proof_document(
    document: FootprintProofDocument,
    *,
    evidence_root: Path,
    effects: Iterable[ActionEffectRaster],
    assets: Iterable[ConnectivityAsset],
) -> str:
    """Recompute proof coverage solely from retained masks and reviewed contracts.

    This verifier deliberately performs no reprojection, resampling, NODATA
    repair, or geometry inference. Any mismatch in the model snapshot, proof
    record, source bytes, or binary mask fails the release boundary.
    """

    root = evidence_root.resolve()
    effect_records = list(effects)
    effects_by_id = {item.effect_id: item for item in effect_records}
    assets_by_id = {item.asset_id: item for item in assets}
    if len(effects_by_id) != len(effect_records):
        raise FootprintCoverageError("retained action effects contain duplicate IDs")
    if set(effects_by_id) != {item.effect_id for item in document.proofs}:
        raise FootprintCoverageError("retained proof effects differ from the model snapshot")
    masks: dict[str, InMemoryRaster] = {}
    for source in document.source_masks:
        asset = assets_by_id.get(source.footprint_asset_id)
        if (
            asset is None
            or asset.kind != AssetKind.ACTION_EFFECT
            or asset.grid is None
            or asset.sha256 != source.reviewed_source_sha256
            or asset.sha256 != source.normalized_logical_values_sha256
            or asset.grid != source.grid
        ):
            raise FootprintCoverageError(
                f"retained footprint contract differs for {source.footprint_asset_id}"
            )
        reviewed_path = (root / source.reviewed_source_relative_path).resolve()
        if (
            root not in reviewed_path.parents
            or not reviewed_path.is_file()
            or reviewed_path.is_symlink()
            or _file_sha256(reviewed_path) != source.reviewed_source_sha256
        ):
            raise FootprintCoverageError(
                f"retained reviewed footprint source differs for {source.footprint_asset_id}"
            )
        path = (root / source.normalized_relative_path).resolve()
        if root not in path.parents or not path.is_file() or path.is_symlink():
            raise FootprintCoverageError(
                f"retained footprint source is unsafe for {source.footprint_asset_id}"
            )
        if _file_sha256(path) != source.normalized_file_sha256:
            raise FootprintCoverageError(
                f"retained footprint source hash differs for {source.footprint_asset_id}"
            )
        try:
            mask = load_esri_ascii(path, asset)
        except Exception as exc:
            raise FootprintCoverageError(
                f"retained footprint source cannot be parsed for {source.footprint_asset_id}"
            ) from exc
        normalized_values: list[list[int]] = []
        for row in mask.values:
            normalized_row: list[int] = []
            for value in row:
                if value is None or int(value) != value or int(value) not in {0, 1}:
                    raise FootprintCoverageError(
                        f"retained footprint {source.footprint_asset_id} is not binary"
                    )
                normalized_row.append(int(value))
            normalized_values.append(normalized_row)
        if canonical_sha256(normalized_values) != source.normalized_logical_values_sha256:
            raise FootprintCoverageError(
                f"retained footprint logical content differs for {source.footprint_asset_id}"
            )
        masks[source.footprint_asset_id] = mask
    recomputed: list[FootprintCoverageProof] = []
    for proof in document.proofs:
        effect = effects_by_id[proof.effect_id]
        asset = assets_by_id[proof.footprint_asset_id]
        mask = masks[proof.footprint_asset_id]
        if (
            effect.scenario_id != document.scenario_id
            or effect.footprint_asset_id != proof.footprint_asset_id
            or action_effect_sha256(effect) != effect.sha256
            or effect.sha256 != proof.effect_sha256
        ):
            raise FootprintCoverageError(f"retained action effect differs for {effect.effect_id}")
        verification_baseline = InMemoryRaster(
            asset_id="retained-proof-scenario-grid",
            grid=document.scenario_grid,
            values=tuple(
                tuple(1.0 for _ in range(document.scenario_grid.width))
                for _ in range(document.scenario_grid.height)
            ),
            source_sha256="0" * 64,
        )
        recomputed_proof = _prove_mask(effect, asset, verification_baseline, mask)
        if recomputed_proof != proof:
            raise FootprintCoverageError(
                f"footprint proof cannot be recomputed for {effect.effect_id}"
            )
        recomputed.append(recomputed_proof)
    recomputed_sha = footprint_proof_set_sha256(recomputed)
    if recomputed_sha != document.footprint_proof_sha256:
        raise FootprintCoverageError("recomputed footprint proof set differs")
    return recomputed_sha


__all__ = [
    "FootprintCoverageError",
    "FootprintCoverageProof",
    "FootprintProofDocument",
    "ProfessionalFootprintProofDocument",
    "RetainedFootprintSource",
    "ReviewedFootprintEvidenceSource",
    "canonical_footprint_proof_document_bytes",
    "create_footprint_proof_document",
    "create_professional_footprint_proof_document",
    "footprint_proof_set_sha256",
    "prove_action_effect_footprints",
    "verify_footprint_proof_document",
    "verify_professional_footprint_proof_document",
]
