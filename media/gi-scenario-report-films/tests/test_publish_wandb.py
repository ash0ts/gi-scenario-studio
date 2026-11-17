from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
SCRIPT = PACKAGE_ROOT / "scripts" / "publish_wandb.py"
SPEC = importlib.util.spec_from_file_location("publish_report_animations", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


def _write(path: Path, value: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return publisher._sha256(path)


def _render_manifest(tmp_path: Path) -> Path:
    films = json.loads((PACKAGE_ROOT / "story" / "films.json").read_text())
    media = json.loads(
        (
            REPOSITORY_ROOT / "media" / "gi-scenario-film" / "story" / "report-animation-media.json"
        ).read_text()
    )
    clips = []
    for film, report_clip in zip(films["films"], media["clips"], strict=True):
        video = Path("dist") / film["slug"] / f"{film['slug']}.mp4"
        poster = Path("dist") / film["slug"] / "poster.png"
        poster_scale = Path("dist") / film["slug"] / "poster-50.png"
        contact_sheet = Path("dist") / film["slug"] / "contact-sheet.png"
        captions = Path("dist") / film["slug"] / f"{film['slug']}.srt"
        checkpoints = []
        for checkpoint in film["checkpoints"]:
            relative = Path("dist") / film["slug"] / "checkpoints" / f"{checkpoint['name']}.png"
            report_scale = (
                Path("dist") / film["slug"] / "checkpoints" / f"{checkpoint['name']}-50.png"
            )
            checkpoints.append(
                {
                    **checkpoint,
                    "path": relative.as_posix(),
                    "sha256": _write(tmp_path / relative, checkpoint["name"].encode()),
                    "report_scale_path": report_scale.as_posix(),
                    "report_scale_sha256": _write(
                        tmp_path / report_scale, f"{checkpoint['name']}-50".encode()
                    ),
                }
            )
        transitions = []
        for transition in film["transition_checkpoints"]:
            relative = Path("dist") / film["slug"] / "transitions" / f"{transition['name']}.png"
            report_scale = (
                Path("dist") / film["slug"] / "transitions" / f"{transition['name']}-50.png"
            )
            transitions.append(
                {
                    **transition,
                    "path": relative.as_posix(),
                    "sha256": _write(tmp_path / relative, transition["name"].encode()),
                    "report_scale_path": report_scale.as_posix(),
                    "report_scale_sha256": _write(
                        tmp_path / report_scale, f"{transition['name']}-50".encode()
                    ),
                }
            )
        clips.append(
            {
                "key": film["key"],
                "title": film["title"],
                "slug": film["slug"],
                "duration_seconds": film["duration"],
                "frame_count": film["duration"] * 30,
                "claim_ids": film["claim_ids"],
                "caption": report_clip["caption"],
                "output": video.as_posix(),
                "sha256": _write(tmp_path / video, film["slug"].encode()),
                "poster": poster.as_posix(),
                "poster_sha256": _write(tmp_path / poster, b"poster"),
                "poster_report_scale": poster_scale.as_posix(),
                "poster_report_scale_sha256": _write(tmp_path / poster_scale, b"poster-50"),
                "captions": captions.as_posix(),
                "captions_sha256": _write(
                    tmp_path / captions, b"1\n00:00:00,000 --> 00:00:01,000\nBoundary\n"
                ),
                "checkpoints": checkpoints,
                "transitions": transitions,
                "contact_sheet": contact_sheet.as_posix(),
                "contact_sheet_sha256": _write(tmp_path / contact_sheet, b"contact-sheet"),
            }
        )
    manifest_path = tmp_path / "dist" / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "clips": clips,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_animation_manifest_validates_all_five_media_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _render_manifest(tmp_path)
    monkeypatch.setattr(publisher, "REPOSITORY_ROOT", tmp_path)

    _, clips, records = publisher._validate_manifest(
        manifest_path,
        package_root=tmp_path,
    )

    assert [clip["key"] for clip in clips] == [
        "animation/incomplete_plot",
        "animation/water_ledger",
        "animation/storm_reveals",
        "animation/decision_funnel",
        "animation/search_release_gate",
    ]
    assert len([item for item in records if item["logical_name"].startswith("media/videos")]) == 5
    assert len([item for item in records if item["logical_name"].startswith("media/posters/")]) == 5
    assert len([item for item in records if item["logical_name"].startswith("media/captions")]) == 5
    assert (
        len([item for item in records if item["logical_name"].startswith("media/checkpoints/")])
        == 17
    )
    assert (
        len(
            [
                item
                for item in records
                if item["logical_name"].startswith("media/checkpoints-report-scale/")
            ]
        )
        == 17
    )
    assert len([item for item in records if "media/transitions/" in item["logical_name"]]) == 11


def test_animation_manifest_rejects_path_escape_and_hash_drift(tmp_path: Path) -> None:
    manifest_path = _render_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["clips"][0]["output"] = "../private.mp4"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unsafe manifest path"):
        publisher._validate_manifest(manifest_path, package_root=tmp_path)

    manifest_path = _render_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["clips"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="hash drifted"):
        publisher._validate_manifest(manifest_path, package_root=tmp_path)


def test_animation_source_projection_is_bound_to_current_evidence_hashes() -> None:
    data, records = publisher._validate_animation_sources(
        PACKAGE_ROOT / "data" / "animation-data.json"
    )

    assert data["search"]["campaign_replays"] == 120
    assert data["water_ledger"]["modeled_gallon_difference"] == 48000
    assert len(records) == len(data["source_hashes"])


def test_benchmark_receipt_requires_exact_restricted_complete_lineage(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    payload = {
        "entity": "team",
        "project": "study",
        "project_access": "RESTRICTED",
        "dataset_artifact": "team/study/exact:v0",
        "publication_artifact": "team/study/campaign:v0",
        "publication_id": "publication-v2",
        "publication_digest": "a" * 64,
        "search_run_count": 120,
        "run_file_privacy_audit": {"audited_runs": 122, "prohibited_files": 0},
    }
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    receipt = publisher._validate_benchmark_receipt(receipt_path, "team", "study")
    assert receipt["dataset_artifact"] == "team/study/exact:v0"

    payload["project_access"] = "OPEN"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="RESTRICTED"):
        publisher._validate_benchmark_receipt(receipt_path, "team", "study")


def test_wandb_settings_disable_automatic_host_and_environment_capture() -> None:
    class FakeWandb:
        @staticmethod
        def Settings(**values):
            return values

    settings = publisher._wandb_settings(FakeWandb())

    assert settings["console"] == "off"
    assert settings["disable_code"] is True
    assert settings["disable_git"] is True
    assert settings["save_code"] is False
    assert settings["x_disable_machine_info"] is True
    assert settings["x_disable_meta"] is True
    assert settings["x_disable_stats"] is True
    assert settings["x_save_requirements"] is False


def test_external_publication_is_pinned_to_the_exact_article_project() -> None:
    publisher._assert_publish_destination(
        {"entity": "wandb", "project": "gi-scenario-studio-article"}
    )
    with pytest.raises(RuntimeError, match="pinned"):
        publisher._assert_publish_destination({"entity": "wandb", "project": "lookalike"})


def test_publication_receipt_preserves_exact_source_artifact_lineage() -> None:
    plan = {
        "entity": "wandb",
        "project": "gi-scenario-studio-article",
        "publication_id": "animations-123",
        "publication_digest": "a" * 64,
        "run_id": "run-123",
        "animation_manifest_sha256": "b" * 64,
        "clip_count": 5,
        "source_artifacts": {
            "benchmark_dataset_artifact": "wandb/gi/exact:v0",
            "benchmark_publication_artifact": "wandb/gi/campaign:v0",
        },
    }

    receipt = publisher._publication_receipt(
        plan,
        run_url="https://wandb.example/runs/run-123",
        artifact="wandb/gi/films:v0",
        guided_artifact="wandb/gi/guided:v0",
        audit={"audited_runs": 1, "prohibited_files": 0},
    )

    assert receipt["project_access"] == "RESTRICTED"
    assert receipt["clip_count"] == 5
    assert receipt["source_artifacts"] == {
        "benchmark_dataset_artifact": "wandb/gi/exact:v0",
        "benchmark_publication_artifact": "wandb/gi/campaign:v0",
        "guided_evidence_artifact": "wandb/gi/guided:v0",
    }
    assert receipt["run_file_privacy_audit"]["prohibited_files"] == 0


class _PolicyService:
    def __init__(self, response):
        self.response = response

    def execute_graphql(self, query, variables):
        assert "project(name: $name, entityName: $entity)" in query
        assert variables == {"name": "study", "entity": "team"}
        return self.response


class _PolicyApi:
    def __init__(self, response):
        self._service_api = _PolicyService(response)


def test_project_policy_requires_exact_identity_and_access_fields() -> None:
    policy = publisher._project_policy(
        _PolicyApi(
            {
                "project": {
                    "id": "p-1",
                    "name": "study",
                    "entityName": "team",
                    "access": "RESTRICTED",
                }
            }
        ),
        "team",
        "study",
    )
    assert policy == {
        "id": "p-1",
        "name": "study",
        "entityName": "team",
        "access": "RESTRICTED",
    }

    with pytest.raises(RuntimeError, match="did not match"):
        publisher._project_policy(
            _PolicyApi(
                {
                    "project": {
                        "id": "p-1",
                        "name": "other",
                        "entityName": "team",
                        "access": "RESTRICTED",
                    }
                }
            ),
            "team",
            "study",
        )


def test_run_file_privacy_audit_rejects_automatic_metadata() -> None:
    class File:
        def __init__(self, name):
            self.name = name

    class Run:
        def __init__(self, names):
            self.names = names

        def files(self):
            return [File(name) for name in self.names]

    class Api:
        def __init__(self, names_by_run):
            self.names_by_run = names_by_run

        def run(self, path):
            return Run(self.names_by_run[path.rsplit("/", 1)[-1]])

    clean = publisher._assert_run_file_policy(
        Api({"run-1": ["config.yaml", "wandb-summary.json"]}),
        "team",
        "project",
        ["run-1"],
    )
    assert clean == {"audited_runs": 1, "prohibited_files": 0}

    with pytest.raises(RuntimeError, match="wandb-metadata.json"):
        publisher._assert_run_file_policy(
            Api({"run-1": ["wandb-metadata.json"]}),
            "team",
            "project",
            ["run-1"],
        )


def test_publisher_does_not_read_dotenv_or_accept_a_key_argument() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "load_dotenv" not in source
    assert "common_tools/.env" not in source
    assert 'add_argument("--api-key"' not in source
    assert "x_disable_meta=True" in source
    assert "x_disable_machine_info=True" in source
    assert "x_disable_stats=True" in source
    assert "x_save_requirements=False" in source
