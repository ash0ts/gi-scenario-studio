from __future__ import annotations

import json
import shutil
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from gi_scenario_core.evidence_guide_r4c_publication_recovery import (
    PUBLICATION_FILES,
    RecoveryInputs,
    canonical_table_cell,
    publish_recovery,
    type_stable_table_projection,
)
from gi_scenario_core.evidence_guide_v2_campaign import file_sha256, json_sha256

ROOT = Path(__file__).resolve().parents[2]
AMENDMENT = (
    ROOT
    / "benchmarks"
    / "evidence_guide"
    / "r4"
    / "publication-amendment-r4c.json"
)


def test_publication_amendment_binds_failure_and_recovery_sources() -> None:
    amendment = json.loads(AMENDMENT.read_text(encoding="utf-8"))
    failure = amendment["failure"]
    normalized = {
        key: failure[key]
        for key in (
            "exception_type",
            "operation",
            "package_file",
            "offending_key",
            "first_inferred_mapping_keys",
            "incoming_extra_mapping_keys",
            "reason",
            "runner_callsite",
            "publisher_callsite",
            "wandb_callsite",
        )
    }
    assert failure["normalized_failure_sha256"] == json_sha256(normalized)
    assert failure["exception_type"] == "TypeError"
    assert failure["provider_call_failed"] is False
    assert failure["answer_released"] is False
    assert amendment["original_publication"]["file_count"] == 7
    assert amendment["claim_boundary"] == {
        "production_state": "DISABLE_LIVE_GUIDE",
        "production_release_eligible": False,
        "actual_answers_released": 0,
        "practitioner_usefulness_status": "not_measured",
        "hydraulic_or_ecological_claim_added": False,
        "publication_recovery_is_scientific_reanalysis": False,
    }
    for group in ("prior_frozen_sources", "recovery_sources"):
        for record in amendment[group].values():
            assert file_sha256(ROOT / record["relative_path"]) == record["sha256"]


def test_table_projection_uses_one_string_type_for_heterogeneous_cells() -> None:
    source = {
        "rows": [
            {
                "model": "granite",
                "error_codes": {
                    "decision_plan_mismatch": 1,
                    "evidence_binding_mismatch": 2,
                },
                "nullable": None,
            },
            {
                "model": "granite",
                "error_codes": {
                    "decision_plan_mismatch": 3,
                    "evidence_binding_mismatch": 4,
                    "limitation_ids_mismatch": 5,
                    "prose_quantity_unsupported": 6,
                    "response_not_json": 7,
                },
                "nullable": ["now", "a", "list"],
            },
        ]
    }
    projection = type_stable_table_projection(source)
    assert projection is not None
    assert all(
        isinstance(cell, str) for row in projection["rows"] for cell in row
    )
    error_index = projection["columns"].index("error_codes")
    assert json.loads(projection["rows"][1][error_index])[
        "response_not_json"
    ] == 7
    assert canonical_table_cell("ordinary string") == '"ordinary string"'
    assert canonical_table_cell(None) == "null"


class _FakeArtifact:
    def __init__(self, *, name: str, type: str, metadata: dict) -> None:
        self.name = name
        self.type = type
        self.metadata = metadata
        self.files: dict[str, Path] = {}

    def add_file(self, source: str, *, name: str) -> None:
        self.files[name] = Path(source)


class _FakeLoggedArtifact:
    version = "v0"
    id = "artifact-id"

    def __init__(self, artifact: _FakeArtifact, root: Path) -> None:
        self.artifact = artifact
        self.qualified_name = (
            "wandb/gi-scenario-studio-agent-evals/"
            f"{artifact.name}:v0"
        )
        self.root = root

    def wait(self) -> None:
        return None

    def download(self, *, root: str) -> str:
        destination = Path(root)
        for name, source in self.artifact.files.items():
            shutil.copyfile(source, destination / name)
        return str(destination)


class _FakeTable:
    def __init__(self, *, columns: list[str]) -> None:
        self.columns = columns
        self.rows: list[tuple[str, ...]] = []

    def add_data(self, *values: str) -> None:
        assert all(isinstance(value, str) for value in values)
        self.rows.append(values)


class _FakeRun:
    def __init__(self, state: SimpleNamespace) -> None:
        self.id = "fake-recovery-run"
        self.url = "https://wandb.invalid/fake-recovery-run"
        self.summary: dict = {}
        self.state = state

    def use_artifact(self, reference: str):
        if reference in self.state.logged:
            return self.state.logged[reference]
        self.state.inputs.append(reference)
        return reference

    def log(self, value: dict) -> None:
        self.state.tables.update(value)

    def log_artifact(self, artifact: _FakeArtifact) -> _FakeLoggedArtifact:
        logged = _FakeLoggedArtifact(artifact, self.state.root)
        self.state.logged[logged.qualified_name] = logged
        return logged

    def finish(self) -> None:
        self.state.finished = True


def test_cloud_recovery_logs_stable_tables_and_unchanged_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publication = tmp_path / "publication"
    publication.mkdir()
    for index, name in enumerate(PUBLICATION_FILES):
        payload = (
            {
                "rows": [
                    {"name": "first", "errors": {"a": index}},
                    {"name": "second", "errors": {"a": index, "b": 2}},
                ]
            }
            if name != "analysis.json"
            else {"analysis_sha256": "a" * 64}
        )
        (publication / name).write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
    files = {name: file_sha256(publication / name) for name in PUBLICATION_FILES}
    inputs = RecoveryInputs(
        campaign_root=tmp_path,
        publication_dir=publication,
        preregistration_path=tmp_path / "preregistration.json",
        preregistration_receipt_path=tmp_path / "preregistration-receipt.json",
        amendment_path=tmp_path / "amendment.json",
        package_files=files,
        package_sha256=json_sha256(
            {"kind": "r4c_original_seven_file_publication", "files": files}
        ),
        analysis_file_sha256=files["analysis.json"],
        analysis_sha256="a" * 64,
        study_instance_sha256="b" * 64,
        preregistration_sha256="c" * 64,
        preregistration_artifact_reference="entity/project/prereg:v0",
        amendment_sha256="d" * 64,
        recovery_identity_sha256="e" * 64,
        table_row_counts={Path(name).stem: 2 for name in PUBLICATION_FILES[1:]},
    )
    state = SimpleNamespace(
        root=tmp_path,
        inputs=[],
        tables={},
        logged={},
        finished=False,
    )

    class CommError(Exception):
        pass

    class FakeApi:
        def run(self, _: str):
            raise CommError("not found")

    fake_wandb = SimpleNamespace(
        Api=FakeApi,
        Table=_FakeTable,
        Artifact=_FakeArtifact,
        Settings=lambda **kwargs: kwargs,
        init=lambda **kwargs: _FakeRun(state),
    )
    monkeypatch.setitem(sys.modules, "wandb", fake_wandb)
    publisher = SimpleNamespace(
        campaign_instance_sha256="b" * 64,
        preregistration_reference="entity/project/prereg:v0",
        entity="wandb",
        project="gi-scenario-studio-agent-evals",
        cloud_environment=lambda: nullcontext(),
    )
    receipt = publish_recovery(
        publisher=publisher,
        inputs=inputs,
        amendment_reference="entity/project/amendment:v0",
    )
    assert receipt.kind == "r4c_publication_recovery"
    assert state.finished is True
    assert len(state.tables) == 6
    assert all(
        isinstance(cell, str)
        for table in state.tables.values()
        for row in table.rows
        for cell in row
    )
    logged = next(iter(state.logged.values()))
    assert set(logged.artifact.files) == set(PUBLICATION_FILES)
    assert {
        name: file_sha256(path) for name, path in logged.artifact.files.items()
    } == files
    assert "entity/project/prereg:v0" in state.inputs
    assert "entity/project/amendment:v0" in state.inputs
