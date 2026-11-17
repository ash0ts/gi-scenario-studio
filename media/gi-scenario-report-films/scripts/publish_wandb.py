"""Publish the five analytical Canvas films to one exact Restricted W&B project.

The default mode is a local dry run. External writes occur only with
``--publish``. This script never reads dotenv files, accepts credentials on the
command line, or guesses a cloud destination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_MANIFEST = PACKAGE_ROOT / "dist" / "manifest.json"
DEFAULT_PLAN = PACKAGE_ROOT / "dist" / "wandb-publication-plan.json"
DEFAULT_RECEIPT = PACKAGE_ROOT / "dist" / "wandb-publication.json"
DEFAULT_BENCHMARK_RECEIPT = (
    REPOSITORY_ROOT / "dist" / "benchmark-publication" / "wandb-publication.json"
)
FILMS_SPEC = PACKAGE_ROOT / "story" / "films.json"
REPORT_MEDIA_SPEC = (
    REPOSITORY_ROOT / "media" / "gi-scenario-film" / "story" / "report-animation-media.json"
)
ANIMATION_DATA = PACKAGE_ROOT / "data" / "animation-data.json"

EXPECTED_ENTITY = "wandb"
EXPECTED_PROJECT = "gi-scenario-studio-article"
EXPECTED_CLIP_COUNT = 5
EXPECTED_FPS = 30
EXPECTED_WIDTH = 1920
EXPECTED_HEIGHT = 1080
PROHIBITED_RUN_FILES = frozenset({"output.log", "requirements.txt", "wandb-metadata.json"})
PROJECT_POLICY_GQL = """
query GIProjectPolicy($name: String!, $entity: String!) {
  project(name: $name, entityName: $entity) {
    id
    name
    entityName
    access
  }
}
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"required publication input is missing: {_display_path(path)}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object: {_display_path(path)}")
    return value


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def _safe_child(root: Path, relative_path: str) -> Path:
    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise RuntimeError(f"unsafe manifest path: {relative_path!r}")
    resolved = (root / Path(*candidate.parts)).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"manifest path escaped its package: {relative_path!r}") from exc
    return resolved


def _repository_source(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError("publication inputs must remain inside the repository") from exc


def _file_record(path: Path, logical_name: str) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"publication file is missing: {_display_path(path)}")
    safe_logical = PurePosixPath(logical_name)
    if safe_logical.is_absolute() or ".." in safe_logical.parts or not safe_logical.parts:
        raise RuntimeError(f"unsafe artifact path: {logical_name!r}")
    return {
        "source": _repository_source(path),
        "logical_name": safe_logical.as_posix(),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _source_path(record: dict[str, Any]) -> Path:
    return _safe_child(REPOSITORY_ROOT, str(record["source"]))


def _manifest_media_path(
    package_root: Path,
    value: Any,
    *,
    field: str,
    expected_suffix: str,
    expected_sha256: str | None = None,
) -> tuple[str, Path, str]:
    if isinstance(value, dict):
        relative_path = value.get("path") or value.get("output")
        declared_sha256 = value.get("sha256")
    else:
        relative_path = value
        declared_sha256 = None
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError(f"animation manifest {field} path is missing")
    path = _safe_child(package_root, relative_path)
    if path.suffix.lower() != expected_suffix:
        raise ValueError(f"animation manifest {field} must be {expected_suffix}")
    if not path.is_file():
        raise ValueError(f"animation manifest {field} is missing: {relative_path}")
    observed_sha256 = _sha256(path)
    required_sha256 = expected_sha256 or declared_sha256
    if required_sha256 and observed_sha256 != required_sha256:
        raise ValueError(f"animation manifest {field} hash drifted: {relative_path}")
    return relative_path, path, observed_sha256


def _validate_animation_sources(
    animation_data_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = _load_json(animation_data_path)
    source_hashes = data.get("source_hashes")
    if not isinstance(source_hashes, dict) or not source_hashes:
        raise ValueError("animation data does not contain its evidence source hashes")
    required_sources = {
        "apps/desktop/src/data/guidedFixture.json",
        "benchmarks/exhaustive_exact/dataset.json",
        "benchmarks/guided_exact/runs/run_ec6a03d02287f847394c/report.rpt",
        "benchmarks/guided_exact/runs/run_c8220fe9c2697d04c9e4/report.rpt",
        "dist/benchmark-publication/tables/campaign_aggregates.csv",
        "dist/benchmark-publication/tables/exact_alternatives.csv",
        "dist/benchmark-publication/tables/release_gate.csv",
        "dist/benchmark-publication/tables/storm_results.csv",
        "media/gi-scenario-report-films/story/films.json",
    }
    missing = sorted(required_sources.difference(source_hashes))
    if missing:
        raise ValueError(f"animation evidence source manifest is incomplete: {', '.join(missing)}")
    records: list[dict[str, Any]] = []
    for relative_path in sorted(source_hashes):
        path = _safe_child(REPOSITORY_ROOT, str(relative_path))
        expected = str(source_hashes[relative_path])
        if len(expected) != 64 or _sha256(path) != expected:
            raise ValueError(f"animation evidence source hash drifted: {relative_path}")
        records.append(_file_record(path, f"evidence/{relative_path}"))
    return data, records


def _validate_manifest(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    package_root: Path = PACKAGE_ROOT,
    films_spec_path: Path = FILMS_SPEC,
    media_spec_path: Path = REPORT_MEDIA_SPEC,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = _load_json(manifest_path)
    films_spec = _load_json(films_spec_path)
    media_spec = _load_json(media_spec_path)
    if int(manifest.get("width", 0)) != EXPECTED_WIDTH:
        raise ValueError("animation manifest width must be 1920")
    if int(manifest.get("height", 0)) != EXPECTED_HEIGHT:
        raise ValueError("animation manifest height must be 1080")
    fps = manifest.get("fps", manifest.get("frame_rate"))
    if int(fps or 0) != EXPECTED_FPS:
        raise ValueError("animation manifest frame rate must be 30 fps")
    rendered = manifest.get("clips")
    declared_films = films_spec.get("films")
    declared_media = media_spec.get("clips")
    if not all(isinstance(value, list) for value in (rendered, declared_films, declared_media)):
        raise TypeError("animation manifests must contain clip lists")
    if len(rendered) != EXPECTED_CLIP_COUNT:
        raise ValueError("animation manifest must contain exactly five clips")
    expected_keys = [item.get("key") for item in declared_media]
    if [item.get("key") for item in rendered] != expected_keys:
        raise ValueError("rendered animation order does not match report-animation-media.json")
    if [item.get("key") for item in declared_films] != expected_keys:
        raise ValueError("film story order does not match report-animation-media.json")

    clips: list[dict[str, Any]] = []
    artifact_records: list[dict[str, Any]] = []
    for expected_film, expected_media, clip in zip(
        declared_films, declared_media, rendered, strict=True
    ):
        key = str(expected_media["key"])
        for field in ("title", "slug", "claim_ids"):
            expected = expected_film[field]
            if clip.get(field) != expected:
                raise ValueError(f"animation {field} drifted: {key}")
        for field in ("title", "caption", "claim_ids"):
            expected = expected_media[field]
            if clip.get(field) != expected:
                raise ValueError(f"report animation {field} drifted: {key}")
        duration = float(clip.get("duration_seconds", clip.get("duration", 0)))
        if duration != float(expected_media["target_duration_seconds"]):
            raise ValueError(f"animation duration drifted: {key}")
        frame_count = int(clip.get("frame_count", 0))
        if frame_count != round(duration * EXPECTED_FPS):
            raise ValueError(f"animation frame count drifted: {key}")

        output_rel, output_path, output_sha = _manifest_media_path(
            package_root,
            clip.get("output"),
            field=f"{key} output",
            expected_suffix=".mp4",
            expected_sha256=clip.get("sha256"),
        )
        poster_rel, poster_path, poster_sha = _manifest_media_path(
            package_root,
            clip.get("poster"),
            field=f"{key} poster",
            expected_suffix=".png",
            expected_sha256=clip.get("poster_sha256"),
        )
        poster_scale_rel, poster_scale_path, poster_scale_sha = _manifest_media_path(
            package_root,
            clip.get("poster_report_scale"),
            field=f"{key} report-scale poster",
            expected_suffix=".png",
            expected_sha256=clip.get("poster_report_scale_sha256"),
        )
        captions_value = clip.get("captions", clip.get("subtitle"))
        captions_rel, captions_path, captions_sha = _manifest_media_path(
            package_root,
            captions_value,
            field=f"{key} captions",
            expected_suffix=".srt",
            expected_sha256=clip.get("captions_sha256", clip.get("subtitle_sha256")),
        )

        expected_checkpoints = expected_film.get("checkpoints")
        checkpoints = clip.get("checkpoints")
        if not isinstance(checkpoints, list) or not isinstance(expected_checkpoints, list):
            raise TypeError(f"animation checkpoints are missing: {key}")
        if [item.get("name") for item in checkpoints] != [
            item.get("name") for item in expected_checkpoints
        ]:
            raise ValueError(f"animation checkpoint order drifted: {key}")
        validated_checkpoints = []
        for expected_checkpoint, checkpoint in zip(expected_checkpoints, checkpoints, strict=True):
            time_seconds = float(checkpoint.get("time", checkpoint.get("time_seconds", -1)))
            if time_seconds != float(expected_checkpoint["time"]):
                raise ValueError(f"animation checkpoint time drifted: {key}")
            path_rel, path, digest = _manifest_media_path(
                package_root,
                checkpoint.get("path", checkpoint.get("output")),
                field=f"{key} checkpoint {checkpoint['name']}",
                expected_suffix=".png",
                expected_sha256=checkpoint.get("sha256"),
            )
            validated_checkpoints.append(
                {
                    "name": checkpoint["name"],
                    "time_seconds": time_seconds,
                    "path": path_rel,
                    "sha256": digest,
                }
            )
            artifact_records.append(
                _file_record(
                    path,
                    f"media/checkpoints/{expected_film['slug']}/{Path(path_rel).name}",
                )
            )
            report_scale_rel, report_scale_path, report_scale_digest = _manifest_media_path(
                package_root,
                checkpoint.get("report_scale_path"),
                field=f"{key} checkpoint {checkpoint['name']} report scale",
                expected_suffix=".png",
                expected_sha256=checkpoint.get("report_scale_sha256"),
            )
            validated_checkpoints[-1].update(
                {
                    "report_scale_path": report_scale_rel,
                    "report_scale_sha256": report_scale_digest,
                }
            )
            artifact_records.append(
                _file_record(
                    report_scale_path,
                    "media/checkpoints-report-scale/"
                    f"{expected_film['slug']}/{Path(report_scale_rel).name}",
                )
            )

        expected_transitions = expected_film.get("transition_checkpoints")
        transitions = clip.get("transitions")
        if not isinstance(transitions, list) or not isinstance(expected_transitions, list):
            raise TypeError(f"animation transition checkpoints are missing: {key}")
        if [item.get("name") for item in transitions] != [
            item.get("name") for item in expected_transitions
        ]:
            raise ValueError(f"animation transition checkpoint order drifted: {key}")
        validated_transitions = []
        for expected_transition, transition in zip(expected_transitions, transitions, strict=True):
            time_seconds = float(transition.get("time", transition.get("time_seconds", -1)))
            if time_seconds != float(expected_transition["time"]):
                raise ValueError(f"animation transition checkpoint time drifted: {key}")
            path_rel, path, digest = _manifest_media_path(
                package_root,
                transition.get("path", transition.get("output")),
                field=f"{key} transition {transition['name']}",
                expected_suffix=".png",
                expected_sha256=transition.get("sha256"),
            )
            report_scale_rel, report_scale_path, report_scale_digest = _manifest_media_path(
                package_root,
                transition.get("report_scale_path"),
                field=f"{key} transition {transition['name']} report scale",
                expected_suffix=".png",
                expected_sha256=transition.get("report_scale_sha256"),
            )
            validated_transitions.append(
                {
                    "name": transition["name"],
                    "time_seconds": time_seconds,
                    "path": path_rel,
                    "sha256": digest,
                    "report_scale_path": report_scale_rel,
                    "report_scale_sha256": report_scale_digest,
                }
            )
            artifact_records.extend(
                [
                    _file_record(
                        path,
                        f"media/transitions/{expected_film['slug']}/{Path(path_rel).name}",
                    ),
                    _file_record(
                        report_scale_path,
                        "media/transitions-report-scale/"
                        f"{expected_film['slug']}/{Path(report_scale_rel).name}",
                    ),
                ]
            )

        contact_rel, contact_path, contact_sha = _manifest_media_path(
            package_root,
            clip.get("contact_sheet"),
            field=f"{key} contact sheet",
            expected_suffix=".png",
            expected_sha256=clip.get("contact_sheet_sha256"),
        )

        artifact_records.extend(
            [
                _file_record(output_path, f"media/videos/{Path(output_rel).name}"),
                _file_record(poster_path, f"media/posters/{Path(poster_rel).name}"),
                _file_record(
                    poster_scale_path,
                    f"media/posters-report-scale/{Path(poster_scale_rel).name}",
                ),
                _file_record(captions_path, f"media/captions/{Path(captions_rel).name}"),
                _file_record(
                    contact_path,
                    f"media/contact-sheets/{expected_film['slug']}/{Path(contact_rel).name}",
                ),
            ]
        )
        clips.append(
            {
                **expected_media,
                "slug": expected_film["slug"],
                "duration_seconds": duration,
                "frame_count": frame_count,
                "output": output_rel,
                "sha256": output_sha,
                "poster": poster_rel,
                "poster_sha256": poster_sha,
                "poster_report_scale": poster_scale_rel,
                "poster_report_scale_sha256": poster_scale_sha,
                "captions": captions_rel,
                "captions_sha256": captions_sha,
                "checkpoints": validated_checkpoints,
                "transitions": validated_transitions,
                "contact_sheet": contact_rel,
                "contact_sheet_sha256": contact_sha,
            }
        )

    optional_declared = (
        ("review_reel", ".mp4", "media/review"),
        ("contact_sheet", ".png", "media/review"),
    )
    for field, suffix, logical_root in optional_declared:
        if manifest.get(field):
            relative, path, _ = _manifest_media_path(
                package_root,
                manifest[field],
                field=field,
                expected_suffix=suffix,
                expected_sha256=manifest.get(f"{field}_sha256"),
            )
            artifact_records.append(_file_record(path, f"{logical_root}/{Path(relative).name}"))
    return manifest, clips, artifact_records


def _validate_benchmark_receipt(receipt_path: Path, entity: str, project: str) -> dict[str, Any]:
    receipt = _load_json(receipt_path)
    required = (
        "entity",
        "project",
        "project_access",
        "dataset_artifact",
        "publication_artifact",
        "publication_id",
        "publication_digest",
        "search_run_count",
        "run_file_privacy_audit",
    )
    if any(receipt.get(name) in (None, "") for name in required):
        raise RuntimeError("benchmark W&B publication receipt is incomplete")
    if receipt["entity"] != entity or receipt["project"] != project:
        raise RuntimeError("benchmark W&B publication destination does not match the films")
    if str(receipt["project_access"]).upper() != "RESTRICTED":
        raise RuntimeError("benchmark publication was not verified in a RESTRICTED project")
    if int(receipt["search_run_count"]) != 120:
        raise RuntimeError("benchmark publication does not contain all 120 search replays")
    audit = receipt["run_file_privacy_audit"]
    if not isinstance(audit, dict) or int(audit.get("prohibited_files", -1)) != 0:
        raise RuntimeError("benchmark publication did not pass its run-file privacy audit")
    return receipt


def _deduplicate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    by_logical: dict[str, str] = {}
    for record in records:
        previous = by_logical.get(record["logical_name"])
        if previous is not None:
            if previous != record["sha256"]:
                raise RuntimeError(f"artifact logical path collision: {record['logical_name']}")
            continue
        by_logical[record["logical_name"]] = record["sha256"]
        result.append(record)
    return result


def _source_package_records(
    manifest_path: Path, render_manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    records = [
        _file_record(manifest_path, "manifest/animation-manifest.json"),
        _file_record(FILMS_SPEC, "source/story/films.json"),
        _file_record(REPORT_MEDIA_SPEC, "source/story/report-animation-media.json"),
        _file_record(ANIMATION_DATA, "source/data/animation-data.json"),
    ]
    optional_sources = (
        PACKAGE_ROOT / "data" / "animation-data.js",
        PACKAGE_ROOT / "README.md",
        PACKAGE_ROOT / "story" / "analysis-contract.md",
        PACKAGE_ROOT / "story" / "claim-ledger.md",
        PACKAGE_ROOT / "src" / "runtime.js",
        PACKAGE_ROOT / "src" / "styles.css",
    )
    for path in optional_sources:
        if path.is_file():
            records.append(
                _file_record(path, f"source/{path.relative_to(PACKAGE_ROOT).as_posix()}")
            )
    for path in sorted(PACKAGE_ROOT.glob("*.html")):
        records.append(_file_record(path, f"source/html/{path.name}"))
    render_sources = render_manifest.get("source_files")
    if not isinstance(render_sources, list) or not render_sources:
        raise ValueError("animation manifest does not contain render-source lineage")
    for source in render_sources:
        if not isinstance(source, dict) or not source.get("path") or not source.get("sha256"):
            raise ValueError("animation render-source record is incomplete")
        path = (PACKAGE_ROOT / str(source["path"])).resolve()
        try:
            repository_relative = path.relative_to(REPOSITORY_ROOT.resolve())
        except ValueError as exc:
            raise RuntimeError("animation render source escaped the repository") from exc
        if not path.is_file() or _sha256(path) != source["sha256"]:
            raise ValueError(f"animation render-source hash drifted: {source['path']}")
        records.append(
            _file_record(
                path,
                f"source/render-inputs/{repository_relative.as_posix()}",
            )
        )
    return records


def _deterministic_id(namespace: str, entity: str, project: str, digest: str) -> str:
    return hashlib.sha256(f"{namespace}\0{entity}\0{project}\0{digest}".encode()).hexdigest()[:24]


def build_plan(
    entity: str,
    project: str,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    benchmark_receipt_path: Path = DEFAULT_BENCHMARK_RECEIPT,
    plan_path: Path = DEFAULT_PLAN,
) -> dict[str, Any]:
    render_manifest, clips, media_records = _validate_manifest(manifest_path)
    animation_data, evidence_records = _validate_animation_sources(ANIMATION_DATA)
    benchmark_receipt = _validate_benchmark_receipt(benchmark_receipt_path, entity, project)
    output_records = _deduplicate_records(
        [*_source_package_records(manifest_path, render_manifest), *media_records]
    )
    guided_paths = {
        "apps/desktop/src/data/guidedFixture.json",
        "benchmarks/guided_exact/runs/run_ec6a03d02287f847394c/report.rpt",
        "benchmarks/guided_exact/runs/run_c8220fe9c2697d04c9e4/report.rpt",
    }
    guided_records = [record for record in evidence_records if record["source"] in guided_paths]
    guided_records.append(_file_record(ANIMATION_DATA, "projection/animation-data.json"))
    guided_records = _deduplicate_records(guided_records)

    digest_payload = {
        "animation_manifest_sha256": _sha256(manifest_path),
        "animation_data_sha256": _sha256(ANIMATION_DATA),
        "output_files": [(item["logical_name"], item["sha256"]) for item in output_records],
        "guided_evidence": [(item["logical_name"], item["sha256"]) for item in guided_records],
        "benchmark_dataset_artifact": benchmark_receipt["dataset_artifact"],
        "benchmark_publication_artifact": benchmark_receipt["publication_artifact"],
        "benchmark_publication_digest": benchmark_receipt["publication_digest"],
    }
    publication_digest = _canonical_sha256(digest_payload)
    guided_digest = _canonical_sha256(
        [(item["logical_name"], item["sha256"]) for item in guided_records]
    )
    publication_id = f"gi-report-animations-{publication_digest[:16]}"
    plan = {
        "schema_version": "1.0",
        "entity": entity,
        "project": project,
        "project_access_required": "RESTRICTED",
        "publication_id": publication_id,
        "publication_digest": publication_digest,
        "animation_manifest_sha256": _sha256(manifest_path),
        "animation_data_sha256": _sha256(ANIMATION_DATA),
        "run_id": _deterministic_id(
            "article-animation-package-v1", entity, project, publication_digest
        ),
        "run_name": f"article-animation-package-{publication_digest[:8]}",
        "artifact_name": "gi-scenario-report-analytical-films",
        "artifact_aliases": ["latest", f"sha-{publication_digest[:12]}"],
        "guided_evidence": {
            "digest": guided_digest,
            "artifact_name": "gi-cedar-trace-guided-animation-evidence",
            "artifact_aliases": ["latest", f"sha-{guided_digest[:12]}"],
            "files": guided_records,
        },
        "source_artifacts": {
            "benchmark_dataset_artifact": benchmark_receipt["dataset_artifact"],
            "benchmark_publication_artifact": benchmark_receipt["publication_artifact"],
        },
        "persistent_boundary": animation_data["persistent_notice"],
        "clip_count": len(clips),
        "clips": clips,
        "files": output_records,
    }
    _write_json_atomic(plan_path, plan)
    return plan


def _defensive_wandb_environment() -> None:
    cloud_root = PACKAGE_ROOT / "dist" / "wandb"
    for name, child in (
        ("WANDB_DIR", "run"),
        ("WANDB_CACHE_DIR", "cache"),
        ("WANDB_ARTIFACT_DIR", "artifacts"),
    ):
        path = cloud_root / child
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    for name, value in {
        "WANDB_CONSOLE": "off",
        "WANDB_DISABLE_CODE": "true",
        "WANDB_DISABLE_GIT": "true",
        "WANDB_ERROR_REPORTING": "false",
        "WANDB_SHOW_RUN": "false",
        "WANDB_SILENT": "true",
    }.items():
        os.environ[name] = value


def _wandb_settings(wandb: Any) -> Any:
    return wandb.Settings(
        console="off",
        disable_code=True,
        disable_git=True,
        save_code=False,
        show_errors=False,
        x_disable_machine_info=True,
        x_disable_meta=True,
        x_disable_stats=True,
        x_save_requirements=False,
    )


def _project_policy(api: Any, entity: str, project: str) -> dict[str, str] | None:
    response = api._service_api.execute_graphql(
        PROJECT_POLICY_GQL,
        {"name": project, "entity": entity},
    )
    record = response.get("project")
    if record is None:
        return None
    required = ("id", "name", "entityName", "access")
    if any(not record.get(field) for field in required):
        raise RuntimeError("W&B project policy response was incomplete; nothing was uploaded")
    if record["name"] != project or record["entityName"] != entity:
        raise RuntimeError("W&B project policy response did not match the requested destination")
    return {field: str(record[field]) for field in required}


def _get_restricted_project(api: Any, entity: str, project: str) -> Any:
    policy = _project_policy(api, entity, project)
    if policy is None:
        raise RuntimeError("the exact W&B destination does not exist; nothing was uploaded")
    if policy["access"].upper() != "RESTRICTED":
        raise RuntimeError(
            f"W&B project access is {policy['access'].upper()}, not RESTRICTED; nothing was uploaded"
        )
    selected = api.project(project, entity=entity)
    if str(selected.id) != policy["id"]:
        raise RuntimeError("W&B project identity changed during policy verification")
    return selected


def _assert_publish_destination(plan: dict[str, Any]) -> None:
    if plan["entity"] != EXPECTED_ENTITY or plan["project"] != EXPECTED_PROJECT:
        raise RuntimeError(
            "external publication is pinned to "
            f"{EXPECTED_ENTITY}/{EXPECTED_PROJECT}; nothing was uploaded"
        )


def _add_files(artifact: Any, records: list[dict[str, Any]]) -> None:
    for record in records:
        source = _source_path(record)
        if _sha256(source) != record["sha256"]:
            raise RuntimeError(f"source changed after validation: {record['source']}")
        artifact.add_file(str(source), name=record["logical_name"], policy="immutable")


def _verify_downloaded_artifact(logged: Any, records: list[dict[str, Any]], *, prefix: str) -> None:
    dist = PACKAGE_ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=dist) as temporary:
        download_root = Path(logged.download(root=temporary, skip_cache=True))
        for record in records:
            downloaded = _safe_child(download_root, record["logical_name"])
            if not downloaded.is_file() or _sha256(downloaded) != record["sha256"]:
                raise RuntimeError(f"W&B artifact roundtrip failed for {record['logical_name']}")


def _assert_run_file_policy(
    api: Any, entity: str, project: str, run_ids: list[str]
) -> dict[str, int]:
    affected: list[tuple[str, str]] = []
    for run_id in run_ids:
        run = api.run(f"{entity}/{project}/{run_id}")
        for file in run.files():
            if file.name in PROHIBITED_RUN_FILES:
                affected.append((run_id, file.name))
    if affected:
        names = sorted({name for _, name in affected})
        raise RuntimeError(
            "W&B run-file privacy audit failed for "
            f"{len({run_id for run_id, _ in affected})} runs: {', '.join(names)}"
        )
    return {"audited_runs": len(run_ids), "prohibited_files": 0}


def _publication_receipt(
    plan: dict[str, Any],
    *,
    run_url: str,
    artifact: str,
    guided_artifact: str,
    audit: dict[str, int],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "entity": plan["entity"],
        "project": plan["project"],
        "project_access": "RESTRICTED",
        "publication_id": plan["publication_id"],
        "publication_digest": plan["publication_digest"],
        "run_id": plan["run_id"],
        "run_url": run_url,
        "artifact": artifact,
        "animation_manifest_sha256": plan["animation_manifest_sha256"],
        "clip_count": plan["clip_count"],
        "source_artifacts": {
            **plan["source_artifacts"],
            "guided_evidence_artifact": guided_artifact,
        },
        "roundtrip_verification": {
            "analytical_film_artifact": "all files",
            "guided_evidence_artifact": "all files",
        },
        "run_file_privacy_audit": audit,
        "public_share_link_enabled": False,
    }


def _existing_complete_run(api: Any, wandb: Any, plan: dict[str, Any]) -> dict[str, str] | None:
    try:
        run = api.run(f"{plan['entity']}/{plan['project']}/{plan['run_id']}")
    except wandb.errors.CommError as exc:
        if "not found" in str(exc).lower():
            return None
        raise
    if run.summary.get("publisher_status") != "complete":
        return None
    if run.summary.get("publication_digest") != plan["publication_digest"]:
        raise RuntimeError(f"deterministic W&B run ID collision for {plan['run_id']}")
    required = ("artifact_qualified_name", "guided_evidence_artifact")
    if any(not run.summary.get(name) for name in required):
        raise RuntimeError("completed animation run is missing artifact lineage")
    return {
        "run_url": run.url,
        "artifact": str(run.summary["artifact_qualified_name"]),
        "guided_artifact": str(run.summary["guided_evidence_artifact"]),
    }


def publish(plan: dict[str, Any], *, receipt_path: Path = DEFAULT_RECEIPT) -> dict[str, Any]:
    _assert_publish_destination(plan)
    if not os.environ.get("WANDB_API_KEY"):
        raise RuntimeError("WANDB_API_KEY is not configured")
    _defensive_wandb_environment()
    try:
        import wandb
    except ImportError as exc:
        raise RuntimeError("install the observability dependency group before publishing") from exc

    base_url = os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai")
    api = wandb.Api(overrides={"base_url": base_url})
    exact_project = _get_restricted_project(api, plan["entity"], plan["project"])
    existing = _existing_complete_run(api, wandb, plan)
    if existing is not None:
        output = api.artifact(existing["artifact"])
        guided = api.artifact(existing["guided_artifact"])
        _verify_downloaded_artifact(output, plan["files"], prefix="gi-animation-output-roundtrip-")
        _verify_downloaded_artifact(
            guided,
            plan["guided_evidence"]["files"],
            prefix="gi-animation-evidence-roundtrip-",
        )
        audit = _assert_run_file_policy(api, plan["entity"], plan["project"], [plan["run_id"]])
        receipt = _publication_receipt(
            plan,
            run_url=existing["run_url"],
            artifact=existing["artifact"],
            guided_artifact=existing["guided_artifact"],
            audit=audit,
        )
        _write_json_atomic(receipt_path, receipt)
        return receipt

    settings = _wandb_settings(wandb)
    with wandb.init(
        entity=plan["entity"],
        project=plan["project"],
        id=plan["run_id"],
        name=plan["run_name"],
        job_type="article_animation_package",
        resume="allow",
        tags=[
            "synthetic",
            "evidence-grounded-animation",
            "canvas-film",
            "gi-scenario-studio",
        ],
        config={
            "publication_id": plan["publication_id"],
            "publication_digest": plan["publication_digest"],
            "animation_manifest_sha256": plan["animation_manifest_sha256"],
            "animation_data_sha256": plan["animation_data_sha256"],
            "clip_count": plan["clip_count"],
            "fps": EXPECTED_FPS,
            "width": EXPECTED_WIDTH,
            "height": EXPECTED_HEIGHT,
            "synthetic_teaching_evidence": True,
            "field_evidence": False,
            "energy_kwh_measured": False,
            "carbon_co2e_measured": False,
        },
        settings=settings,
    ) as run:
        run.use_artifact(
            plan["source_artifacts"]["benchmark_dataset_artifact"],
            use_as="exact-alternative-dataset",
        )
        run.use_artifact(
            plan["source_artifacts"]["benchmark_publication_artifact"],
            use_as="benchmark-publication",
        )
        guided = wandb.Artifact(
            name=plan["guided_evidence"]["artifact_name"],
            type="guided-fixture",
            metadata={
                "digest": plan["guided_evidence"]["digest"],
                "synthetic_teaching_evidence": True,
                "field_evidence": False,
                "purpose": "animation source lineage",
            },
        )
        _add_files(guided, plan["guided_evidence"]["files"])
        guided_used = run.use_artifact(
            guided,
            aliases=plan["guided_evidence"]["artifact_aliases"],
            use_as="guided-animation-evidence",
        )
        guided_used.wait()
        _verify_downloaded_artifact(
            guided_used,
            plan["guided_evidence"]["files"],
            prefix="gi-animation-evidence-roundtrip-",
        )

        media_payload: dict[str, Any] = {}
        checkpoint_rows: list[list[Any]] = []
        caption_rows: list[list[Any]] = []
        for clip in plan["clips"]:
            media_payload[clip["key"]] = wandb.Video(
                str(_safe_child(PACKAGE_ROOT, clip["output"])),
                caption=clip["caption"],
                format="mp4",
            )
            media_payload[f"{clip['key']}/poster"] = wandb.Image(
                str(_safe_child(PACKAGE_ROOT, clip["poster"])),
                caption=clip["title"],
            )
            captions_path = _safe_child(PACKAGE_ROOT, clip["captions"])
            caption_rows.append(
                [
                    clip["key"],
                    "srt",
                    captions_path.read_text(encoding="utf-8"),
                    clip["captions_sha256"],
                ]
            )
            for checkpoint_kind, checkpoint_group in (
                ("conclusion", clip["checkpoints"]),
                ("transition", clip["transitions"]),
            ):
                for checkpoint in checkpoint_group:
                    checkpoint_rows.append(
                        [
                            clip["key"],
                            checkpoint_kind,
                            checkpoint["name"],
                            checkpoint["time_seconds"],
                            wandb.Image(
                                str(_safe_child(PACKAGE_ROOT, checkpoint["path"])),
                                caption=f"{clip['title']} — {checkpoint['name']}",
                            ),
                            checkpoint["sha256"],
                        ]
                    )

        index = wandb.Table(
            columns=[
                "key",
                "title",
                "duration_seconds",
                "frame_count",
                "video_sha256",
                "poster_sha256",
                "caption",
                "claim_ids",
                "checkpoint_count",
                "evidence_boundary",
            ],
            data=[
                [
                    clip["key"],
                    clip["title"],
                    clip["duration_seconds"],
                    clip["frame_count"],
                    clip["sha256"],
                    clip["poster_sha256"],
                    clip["caption"],
                    ", ".join(clip["claim_ids"]),
                    len(clip["checkpoints"]) + len(clip["transitions"]),
                    plan["persistent_boundary"],
                ]
                for clip in plan["clips"]
            ],
        )
        checkpoints = wandb.Table(
            columns=["key", "kind", "checkpoint", "time_seconds", "image", "sha256"],
            data=checkpoint_rows,
        )
        captions = wandb.Table(
            columns=["key", "format", "content", "sha256"],
            data=caption_rows,
        )
        run.log(
            {
                **media_payload,
                "animation/index": index,
                "animation/checkpoints": checkpoints,
                "animation/captions": captions,
            }
        )

        artifact = wandb.Artifact(
            name=plan["artifact_name"],
            type="analytical-film",
            metadata={
                "publication_id": plan["publication_id"],
                "publication_digest": plan["publication_digest"],
                "animation_manifest_sha256": plan["animation_manifest_sha256"],
                "clip_count": plan["clip_count"],
                "source_artifacts": plan["source_artifacts"],
                "guided_evidence_artifact": guided_used.qualified_name,
                "synthetic_teaching_evidence": True,
                "field_evidence": False,
                "energy_kwh": "not measured",
                "carbon_co2e": "not measured",
            },
        )
        _add_files(artifact, plan["files"])
        logged = run.log_artifact(artifact, aliases=plan["artifact_aliases"])
        logged.wait()
        _verify_downloaded_artifact(logged, plan["files"], prefix="gi-animation-output-roundtrip-")
        run.summary.update(
            {
                "publisher_status": "complete",
                "publication_id": plan["publication_id"],
                "publication_digest": plan["publication_digest"],
                "animation_manifest_sha256": plan["animation_manifest_sha256"],
                "clip_count": plan["clip_count"],
                "artifact_qualified_name": logged.qualified_name,
                "guided_evidence_artifact": guided_used.qualified_name,
                "roundtrip_verified": True,
                "project_access_required": "RESTRICTED",
                "public_share_link_enabled": False,
            }
        )
        run_url = run.url
        artifact_name = str(logged.qualified_name)
        guided_artifact_name = str(guided_used.qualified_name)

    final_policy = _project_policy(api, plan["entity"], plan["project"])
    if final_policy is None or final_policy["access"].upper() != "RESTRICTED":
        raise RuntimeError("W&B project policy changed during animation publication")
    if str(exact_project.id) != final_policy["id"]:
        raise RuntimeError("W&B project identity changed during animation publication")
    audit = _assert_run_file_policy(api, plan["entity"], plan["project"], [plan["run_id"]])
    receipt = _publication_receipt(
        plan,
        run_url=run_url,
        artifact=artifact_name,
        guided_artifact=guided_artifact_name,
        audit=audit,
    )
    _write_json_atomic(receipt_path, receipt)
    return receipt


def audit_published_run_files(
    plan: dict[str, Any], *, receipt_path: Path = DEFAULT_RECEIPT
) -> dict[str, int]:
    if not os.environ.get("WANDB_API_KEY"):
        raise RuntimeError("WANDB_API_KEY is not configured")
    receipt = _load_json(receipt_path)
    if (
        receipt.get("entity") != plan["entity"]
        or receipt.get("project") != plan["project"]
        or receipt.get("publication_digest") != plan["publication_digest"]
        or receipt.get("project_access") != "RESTRICTED"
    ):
        raise RuntimeError("W&B receipt does not match the validated animation publication plan")
    _assert_publish_destination(plan)
    _defensive_wandb_environment()
    import wandb

    base_url = os.environ.get("WANDB_BASE_URL", "https://api.wandb.ai")
    api = wandb.Api(overrides={"base_url": base_url})
    _get_restricted_project(api, plan["entity"], plan["project"])
    audit = _assert_run_file_policy(api, plan["entity"], plan["project"], [plan["run_id"]])
    receipt["run_file_privacy_audit"] = audit
    _write_json_atomic(receipt_path, receipt)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--entity",
        default=os.environ.get("WANDB_REPORT_ENTITY") or os.environ.get("WANDB_ENTITY"),
    )
    parser.add_argument("--project", default=os.environ.get("WANDB_REPORT_PROJECT"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--benchmark-receipt", type=Path, default=DEFAULT_BENCHMARK_RECEIPT)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--publish", action="store_true", help="perform external W&B writes")
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="re-audit the published W&B run files and update the local receipt",
    )
    arguments = parser.parse_args()
    if not arguments.entity or not arguments.project:
        parser.error("--entity and --project are required; the publisher never guesses them")
    plan = build_plan(
        arguments.entity,
        arguments.project,
        manifest_path=arguments.manifest,
        benchmark_receipt_path=arguments.benchmark_receipt,
        plan_path=arguments.plan,
    )
    if arguments.audit_only:
        result = audit_published_run_files(plan, receipt_path=arguments.receipt)
        print(json.dumps({"run_file_privacy_audit": result}))
    elif arguments.publish:
        result = publish(plan, receipt_path=arguments.receipt)
        print(json.dumps({"run_url": result["run_url"], "artifact": result["artifact"]}))
    else:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "plan": _display_path(arguments.plan),
                    "publication_id": plan["publication_id"],
                    "run_id": plan["run_id"],
                    "clip_count": plan["clip_count"],
                    "destination": f"{plan['entity']}/{plan['project']}",
                }
            )
        )


if __name__ == "__main__":
    main()
