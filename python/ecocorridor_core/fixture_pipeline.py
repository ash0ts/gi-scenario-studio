"""Deterministic Ridgeway source materialization and direct-study entry point."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import Field

from .contracts import AssetKind, ContractModel, RasterValueKind, canonical_sha256
from .grid import InMemoryRaster, grid_identity
from .ridgeway import (
    RidgewayFixture,
    build_ridgeway_fixture,
    ridgeway_assumption_ledger_payload,
    ridgeway_cost_basis_payload,
)


class MaterializedFixtureFile(ContractModel):
    relative_path: str
    sha256: str
    byte_size: int = Field(ge=0)
    role: str
    source_asset_id: str | None = None
    reviewed_content_sha256: str | None = None
    grid_identity_sha256: str | None = None


class RidgewayMaterializationManifest(ContractModel):
    schema_version: str = "ridgeway-materialization/v1.0"
    fixture_version: str
    project_id: str
    model_sha256: str
    materializer_sha256: str
    files: list[MaterializedFixtureFile]
    direct_matrix_job_count: int
    teaching_map_job_count: int
    execution_state: str = "INPUTS_MATERIALIZED"
    release_replay_eligible: bool = False
    release_blocker: str = (
        "Materialized inputs are not evidence. Complete the pinned direct Circuitscape study."
    )


def _format_number(value: float | None, nodata: float | None) -> str:
    if value is None:
        value = nodata
    if value is None:
        raise ValueError("ESRI ASCII output requires an explicit NODATA value")
    return f"{float(value):.12g}"


def render_esri_ascii(raster: InMemoryRaster) -> str:
    transform = raster.grid.transform
    if transform[1] != 0 or transform[3] != 0:
        raise ValueError("ESRI ASCII materialization requires an axis-aligned grid")
    if transform[0] <= 0 or transform[4] >= 0 or transform[0] != abs(transform[4]):
        raise ValueError("ESRI ASCII materialization requires square north-up cells")
    if raster.grid.nodata is None:
        raise ValueError("ESRI ASCII materialization requires an explicit NODATA value")
    xllcorner = transform[2]
    yllcorner = transform[5] + raster.grid.height * transform[4]
    lines = [
        f"ncols {raster.grid.width}",
        f"nrows {raster.grid.height}",
        f"xllcorner {_format_number(xllcorner, None)}",
        f"yllcorner {_format_number(yllcorner, None)}",
        f"cellsize {_format_number(transform[0], None)}",
        f"NODATA_value {_format_number(raster.grid.nodata, None)}",
    ]
    lines.extend(
        " ".join(_format_number(value, raster.grid.nodata) for value in row)
        for row in raster.values
    )
    return "\n".join(lines) + "\n"


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _file_record(
    root: Path,
    path: Path,
    role: str,
    *,
    source_asset_id: str | None = None,
    reviewed_content_sha256: str | None = None,
    grid_identity_sha256: str | None = None,
) -> MaterializedFixtureFile:
    payload = path.read_bytes()
    return MaterializedFixtureFile(
        relative_path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        role=role,
        source_asset_id=source_asset_id,
        reviewed_content_sha256=reviewed_content_sha256,
        grid_identity_sha256=grid_identity_sha256,
    )


def ridgeway_model_snapshot_payload(
    fixture: RidgewayFixture,
    normalized_sources: list[MaterializedFixtureFile],
) -> dict[str, object]:
    """Return the canonical, independently inspectable Ridgeway decision model.

    The snapshot intentionally contains contracts and decision provenance, not
    solver outcomes. Normalized raster bytes remain separate archive members and
    are bound here by both their file digest and reviewed logical-content digest.
    """

    source_records = sorted(
        (
            item.model_dump(mode="json")
            for item in normalized_sources
            if item.source_asset_id is not None
        ),
        key=lambda item: (str(item["source_asset_id"]), str(item["relative_path"])),
    )
    payload: dict[str, object] = {
        "schema_version": "ridgeway-model-decision-snapshot/v1.0",
        "fixture": {
            "fixture_version": fixture.fixture_version,
            "project_id": fixture.bundle.project_id,
            "license": fixture.license,
            "notice": fixture.notice,
            "baseline_portfolio_id": fixture.baseline_portfolio_id,
            "physics_model_sha256": fixture.physics_model_sha256,
            "decision_snapshot_sha256": fixture.decision_snapshot_sha256,
            "cost_basis_sha256": fixture.cost_basis_sha256,
            "assumption_ledger_sha256": fixture.assumption_ledger_sha256,
            "direct_matrix_job_count": fixture.direct_matrix_job_count,
            "teaching_map_job_count": fixture.teaching_map_job_count,
        },
        "bundle": fixture.bundle.model_dump(mode="json"),
        "portfolios": [item.model_dump(mode="json") for item in fixture.portfolios],
        "normalized_sources": source_records,
        "evidence_ledgers": sorted(
            (
                item.model_dump(mode="json")
                for item in normalized_sources
                if item.role in {"cost_basis", "assumption_ledger"}
            ),
            key=lambda item: str(item["relative_path"]),
        ),
    }
    payload["snapshot_content_sha256"] = canonical_sha256(payload)
    return payload


def materialize_ridgeway_ascii(
    output_directory: Path,
    *,
    fixture: RidgewayFixture | None = None,
) -> RidgewayMaterializationManifest:
    """Write normalized source rasters and pair files, but no result values."""

    fixture = fixture or build_ridgeway_fixture()
    if output_directory.exists() and any(output_directory.iterdir()):
        raise FileExistsError("Ridgeway materialization target must be absent or empty")
    output_directory.mkdir(parents=True, exist_ok=True)
    records: list[MaterializedFixtureFile] = []
    assets_by_id = {item.asset_id: item for item in fixture.bundle.assets}
    for asset in sorted(fixture.bundle.assets, key=lambda item: item.asset_id):
        raster = fixture.rasters.get(asset.asset_id)
        if raster is None:
            raise ValueError(
                f"Ridgeway bundle asset has no retained authored bytes: {asset.asset_id}"
            )
        payload = json.dumps(
            raster.values,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(payload).hexdigest() != asset.sha256 or len(payload) != asset.byte_size:
            raise ValueError(
                f"Ridgeway authored asset contract differs from bytes: {asset.asset_id}"
            )
        path = output_directory / asset.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
        records.append(
            _file_record(
                output_directory,
                path,
                "reviewed_asset_source",
                source_asset_id=asset.asset_id,
                reviewed_content_sha256=asset.sha256,
                grid_identity_sha256=(
                    grid_identity(asset.grid) if asset.grid is not None else None
                ),
            )
        )
    hypothesis_by_id = {item.hypothesis_id: item for item in fixture.bundle.resistance_hypotheses}
    focal_by_id = {item.focal_set_id: item for item in fixture.bundle.focal_area_sets}
    for hypothesis in sorted(
        fixture.bundle.resistance_hypotheses, key=lambda item: item.hypothesis_id
    ):
        raster = fixture.rasters[hypothesis.resistance_asset_id]
        if raster.grid.value_kind != RasterValueKind.CONTINUOUS:
            raise ValueError("Ridgeway resistance source has the wrong raster kind")
        path = output_directory / "resistance" / f"{hypothesis.hypothesis_id}.asc"
        _atomic_text(path, render_esri_ascii(raster))
        asset = assets_by_id[hypothesis.resistance_asset_id]
        records.append(
            _file_record(
                output_directory,
                path,
                "resistance_raster",
                source_asset_id=asset.asset_id,
                reviewed_content_sha256=asset.sha256,
                grid_identity_sha256=grid_identity(raster.grid),
            )
        )
    for focal_set in sorted(fixture.bundle.focal_area_sets, key=lambda item: item.focal_set_id):
        raster = fixture.rasters[focal_set.asset_id]
        if raster.grid.value_kind != RasterValueKind.IDENTIFIER:
            raise ValueError("Ridgeway focal source has the wrong raster kind")
        path = output_directory / "focal" / f"{focal_set.focal_set_id}.asc"
        _atomic_text(path, render_esri_ascii(raster))
        asset = assets_by_id[focal_set.asset_id]
        records.append(
            _file_record(
                output_directory,
                path,
                "focal_raster",
                source_asset_id=asset.asset_id,
                reviewed_content_sha256=asset.sha256,
                grid_identity_sha256=grid_identity(raster.grid),
            )
        )
    footprint_asset_ids = sorted(
        {item.footprint_asset_id for item in fixture.bundle.intervention_sites}
    )
    for asset_id in footprint_asset_ids:
        asset = assets_by_id[asset_id]
        raster = fixture.rasters[asset_id]
        if asset.kind != AssetKind.ACTION_EFFECT or raster.grid.value_kind != RasterValueKind.MASK:
            raise ValueError("Ridgeway reviewed footprint source has the wrong raster semantics")
        path = output_directory / "footprints" / f"{asset_id}.asc"
        _atomic_text(path, render_esri_ascii(raster))
        records.append(
            _file_record(
                output_directory,
                path,
                "reviewed_footprint_mask",
                source_asset_id=asset.asset_id,
                reviewed_content_sha256=asset.sha256,
                grid_identity_sha256=grid_identity(raster.grid),
            )
        )
    for review_mask in sorted(fixture.bundle.review_masks, key=lambda item: item.mask_id):
        asset = assets_by_id[review_mask.asset_id]
        raster = fixture.rasters[asset.asset_id]
        if asset.kind != AssetKind.REVIEW_MASK or raster.grid.value_kind != RasterValueKind.MASK:
            raise ValueError("Ridgeway review-mask source has the wrong raster semantics")
        path = output_directory / "review-masks" / f"{asset.asset_id}.asc"
        _atomic_text(path, render_esri_ascii(raster))
        records.append(
            _file_record(
                output_directory,
                path,
                "risk_review_mask",
                source_asset_id=asset.asset_id,
                reviewed_content_sha256=asset.sha256,
                grid_identity_sha256=grid_identity(raster.grid),
            )
        )
    ledgers = (
        (
            "ledgers/ridgeway-cost-basis.json",
            "cost_basis",
            ridgeway_cost_basis_payload(fixture.bundle.intervention_options),
        ),
        (
            "ledgers/ridgeway-assumption-ledger.json",
            "assumption_ledger",
            ridgeway_assumption_ledger_payload(
                fixture.bundle.intervention_options,
                fixture.bundle.fitness,
            ),
        ),
    )
    for relative, role, payload in ledgers:
        path = output_directory / relative
        _atomic_text(path, json.dumps(payload, sort_keys=True, indent=2) + "\n")
        records.append(_file_record(output_directory, path, role))
    for scenario in sorted(fixture.bundle.scenarios, key=lambda item: item.scenario_id):
        focal_set = focal_by_id[scenario.focal_set_id]
        pairs = {item.pair_id: item for item in focal_set.pairs}
        # Circuitscape 5.17.1 `read_included_pairs` requires this mode header,
        # followed by a two-column list of focal IDs.
        content = "mode\tinclude\n" + "".join(
            f"{pairs[pair_id].from_focal_id}\t{pairs[pair_id].to_focal_id}\n"
            for pair_id in scenario.included_pair_ids
        )
        path = output_directory / "pairs" / f"{scenario.scenario_id}.tsv"
        _atomic_text(path, content)
        records.append(_file_record(output_directory, path, "included_pairs_source"))
    study_plan = {
        "schema_version": "ridgeway-study/v1.0",
        "project_id": fixture.bundle.project_id,
        "revision_id": fixture.bundle.revision.revision_id,
        "model_sha256": fixture.bundle.revision.model_sha256,
        "baseline_portfolio_id": fixture.baseline_portfolio_id,
        "portfolio_ids": [item.portfolio_id for item in fixture.portfolios],
        "scenarios": [
            {
                "scenario_id": scenario.scenario_id,
                "resistance_file": (
                    "resistance/"
                    f"{hypothesis_by_id[scenario.resistance_hypothesis_id].hypothesis_id}.asc"
                ),
                "focal_file": f"focal/{scenario.focal_set_id}.asc",
                "included_pairs_file": f"pairs/{scenario.scenario_id}.tsv",
                "safeguard": scenario.safeguard,
            }
            for scenario in fixture.bundle.scenarios
        ],
        "direct_matrix_job_count": fixture.direct_matrix_job_count,
        "teaching_map_job_count": fixture.teaching_map_job_count,
        "execution_state": "INPUTS_MATERIALIZED",
    }
    plan_path = output_directory / "study-plan.json"
    _atomic_text(plan_path, json.dumps(study_plan, sort_keys=True, indent=2) + "\n")
    records.append(_file_record(output_directory, plan_path, "study_plan"))
    snapshot_path = output_directory / "model-decision-snapshot.json"
    _atomic_text(
        snapshot_path,
        json.dumps(ridgeway_model_snapshot_payload(fixture, records), sort_keys=True, indent=2)
        + "\n",
    )
    records.append(_file_record(output_directory, snapshot_path, "model_decision_snapshot"))
    manifest = RidgewayMaterializationManifest(
        fixture_version=fixture.fixture_version,
        project_id=fixture.bundle.project_id,
        model_sha256=fixture.bundle.revision.model_sha256,
        materializer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        files=sorted(records, key=lambda item: item.relative_path),
        direct_matrix_job_count=fixture.direct_matrix_job_count,
        teaching_map_job_count=fixture.teaching_map_job_count,
    )
    manifest_path = output_directory / "materialization-manifest.json"
    _atomic_text(
        manifest_path, json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    )
    return manifest


def run_ridgeway_direct_study(**kwargs: object):
    """Run the resumable direct study through the separately testable executor."""

    from .direct_study import run_direct_study

    return run_direct_study(**kwargs)
