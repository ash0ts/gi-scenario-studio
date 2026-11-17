from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "publish_benchmark_study.py"
SPEC = importlib.util.spec_from_file_location("publish_benchmark_study", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


def test_exact_dataset_passes_publication_boundary_checks():
    payload = publisher._validate_dataset(ROOT / "benchmarks" / "exhaustive_exact" / "dataset.json")

    assert payload["universe_size"] == 700
    assert payload["job_accounting"]["direct_swmm_jobs"] == 2800
    assert len({run["run_id"] for row in payload["rows"] for run in row["runs"]}) == 2800


def test_deterministic_run_ids_are_destination_scoped():
    first = publisher._deterministic_id("search", "team", "project", "a" * 64)

    assert first == publisher._deterministic_id("search", "team", "project", "a" * 64)
    assert first != publisher._deterministic_id("search", "team", "another", "a" * 64)
    assert len(first) == 24


def test_wandb_settings_disable_host_and_environment_capture():
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


def test_run_file_privacy_audit_rejects_automatic_metadata():
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


def test_manifest_child_rejects_path_traversal(tmp_path):
    valid = publisher._safe_child(tmp_path, "campaign/result.json")

    assert valid == tmp_path / "campaign" / "result.json"
    with pytest.raises(RuntimeError, match="unsafe manifest path"):
        publisher._safe_child(tmp_path, "../credential.txt")
    with pytest.raises(RuntimeError, match="unsafe manifest path"):
        publisher._safe_child(tmp_path, "/tmp/result.json")


def test_typed_csv_preserves_numeric_and_boolean_fields(tmp_path):
    path = tmp_path / "table.csv"
    path.write_text("name,count,accepted\ntrial,7,False\n", encoding="utf-8")

    columns, rows = publisher._read_typed_csv(
        path,
        {
            "name": str,
            "count": int,
            "accepted": publisher._parse_bool,
        },
    )

    assert columns == ["name", "count", "accepted"]
    assert rows == [["trial", 7, False]]


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


def test_project_policy_requires_the_exact_requested_destination():
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
