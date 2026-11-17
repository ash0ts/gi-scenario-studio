from __future__ import annotations

import builtins
import contextlib
import hashlib
import importlib.metadata
import io
import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from scripts import publish_ecocorridor_search_report as publisher

ROOT = Path(__file__).resolve().parents[2]
SEARCH_ROOT = ROOT / "benchmarks/ecocorridor/search"
DATA_MANIFEST = ROOT / "benchmarks/ecocorridor/data-manifest.json"


@pytest.fixture(autouse=True)
def _stable_git_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publisher, "_git_source", lambda _root: ("a" * 40, True))


def _plan(*, source_tree_clean: bool = True) -> tuple[publisher.VerifiedSearchInputs, dict]:
    inputs = publisher.verify_search_inputs(ROOT)
    return inputs, publisher.build_publication_plan(
        inputs,
        source_commit="a" * 40,
        source_tree_clean=source_tree_clean,
    )


def _copy_repository_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "repository"
    ecocorridor = destination / "benchmarks/ecocorridor"
    ecocorridor.mkdir(parents=True)
    shutil.copytree(SEARCH_ROOT, ecocorridor / "search")
    shutil.copy2(DATA_MANIFEST, ecocorridor / "data-manifest.json")
    return destination


def test_dry_run_is_deterministic_network_free_and_preserves_release_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (SEARCH_ROOT / "release-gate.json").read_bytes()
    monkeypatch.setattr(publisher, "_git_source", lambda _root: ("a" * 40, True))
    original_import = builtins.__import__

    def deny_wandb_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "wandb" or name.startswith("wandb_workspaces"):
            raise AssertionError("dry run imported a network SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_wandb_import)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    arguments = [
        "--repository-root",
        str(ROOT),
        "--source-commit",
        "a" * 40,
        "--dry-run",
    ]
    assert publisher.main([*arguments, "--output", str(first)]) == 0
    assert publisher.main([*arguments, "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    assert (SEARCH_ROOT / "release-gate.json").read_bytes() == before

    plan = json.loads(first.read_text(encoding="utf-8"))
    assert plan["disposition"] == "HISTORICAL_BENCHMARK_ONLY"
    assert plan["production_authorization"] == "PROHIBITED"
    assert plan["production_adaptive_enabled"] is False
    assert plan["production_search_method"] == "scrambled_sobol"
    assert plan["tables"]["search_benchmark_runs"]["row_count"] == 240
    assert plan["tables"]["search_method_aggregates"]["row_count"] == 6
    assert plan["tables"]["release_gate_metrics"]["row_count"] == 10
    assert plan["tables"]["resource_observation"]["row_count"] == 1
    assert "/Users/" not in json.dumps(plan)


@pytest.mark.filterwarnings("ignore:Pydantic V1 style.*")
def test_pinned_wandb_report_api_serializes_the_real_blocks_without_network() -> None:
    wandb = pytest.importorskip("wandb")
    wr = pytest.importorskip("wandb_workspaces.reports.v2")

    _, plan = _plan()
    assert importlib.metadata.version("wandb") == "0.28.1"
    assert importlib.metadata.version("wandb-workspaces") == "0.4.5"
    run_table = plan["tables"]["search_benchmark_runs"]
    table = wandb.Table(columns=run_table["columns"], data=run_table["rows"])
    assert len(table.data) == 240
    blocks = publisher._build_report_blocks(
        wr,
        plan,
        entity="wildlife-team",
        project="ecocorridor-public",
        run_url="https://wandb.example/wildlife-team/ecocorridor-public/runs/run-1",
        artifact_ref="wildlife-team/ecocorridor-public/search-gate:v7",
    )
    report = wr.Report(
        entity="wildlife-team",
        project="ecocorridor-public",
        title=plan["wandb_projection"]["report_title"],
        description="Historical synthetic search gate.",
        blocks=blocks,
        width="fluid",
    )
    assert report.entity == "wildlife-team"
    assert report.project == "ecocorridor-public"
    assert any(isinstance(block, wr.PanelGrid) for block in report.blocks)
    table_names = {
        panel.table_name
        for block in report.blocks
        if isinstance(block, wr.PanelGrid)
        for panel in block.panels
        if isinstance(panel, wr.WeavePanelSummaryTable)
    }
    assert "search_benchmark_runs" in table_names
    assert "release_gate_metrics" in table_names
    callouts = [block.text for block in report.blocks if isinstance(block, wr.CalloutBlock)]
    assert any("HISTORICAL_BENCHMARK_ONLY" in text for text in callouts)


@pytest.mark.filterwarnings("ignore:Pydantic V1 style.*")
def test_actual_public_api_missing_project_is_empty_first_publish_discovery() -> None:
    wandb_public = pytest.importorskip("wandb.apis.public")
    _, plan = _plan()

    class MissingProjectService:
        def execute_graphql(self, *_args: Any, **_kwargs: Any) -> dict[str, None]:
            return {"project": None}

    service = MissingProjectService()

    class MissingProjectApi:
        @staticmethod
        def runs(**kwargs: Any) -> Any:
            entity, project = kwargs["path"].split("/", 1)
            return wandb_public.Runs(
                service,
                entity,
                project,
                filters=kwargs["filters"],
                per_page=kwargs["per_page"],
            )

        @staticmethod
        def reports(**kwargs: Any) -> Any:
            entity, project = kwargs["path"].split("/", 1)
            return wandb_public.Reports(
                service,
                wandb_public.Project(service, entity, project, {}),
                per_page=kwargs["per_page"],
            )

    api = MissingProjectApi()
    assert (
        publisher._discover_deterministic_run(
            api,
            entity="wildlife-team",
            project="first-project",
            run_id=plan["wandb_projection"]["run_id"],
        )
        is None
    )
    assert (
        publisher._discover_report(
            api,
            SimpleNamespace(Report=object),
            plan,
            entity="wildlife-team",
            project="first-project",
            report_url=None,
        )
        is None
    )


def test_discovery_does_not_hide_other_api_value_errors() -> None:
    _, plan = _plan()

    class BrokenApi:
        @staticmethod
        def runs(**_kwargs: Any) -> list[Any]:
            raise ValueError("authorization failed")

        @staticmethod
        def reports(**_kwargs: Any) -> list[Any]:
            raise ValueError("authorization failed")

    with pytest.raises(ValueError, match="authorization failed"):
        publisher._discover_deterministic_run(
            BrokenApi(),
            entity="wildlife-team",
            project="first-project",
            run_id=plan["wandb_projection"]["run_id"],
        )
    with pytest.raises(ValueError, match="authorization failed"):
        publisher._discover_report(
            BrokenApi(),
            SimpleNamespace(Report=object),
            plan,
            entity="wildlife-team",
            project="first-project",
            report_url=None,
        )


@pytest.mark.parametrize(
    ("filename", "mutation", "error"),
    [
        (
            "release-gate.resources.json",
            lambda payload: payload["resource_observation"].__setitem__("energy_kwh", 0.0),
            "digest",
        ),
        (
            "manifest.json",
            lambda payload: payload["files"][0].__setitem__("public_replay_allowed", False),
            "digest",
        ),
        (
            "historical-provenance.json",
            lambda payload: payload.__setitem__("production_authorization", "ALLOWED"),
            "digest",
        ),
    ],
)
def test_input_verification_rejects_tampered_sidecar_manifest_and_provenance(
    tmp_path: Path,
    filename: str,
    mutation: Any,
    error: str,
) -> None:
    destination = _copy_repository_fixture(tmp_path)
    search = destination / "benchmarks/ecocorridor/search"
    path = search / filename
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        publisher.verify_search_inputs(destination)


def test_historical_campaign_cannot_be_relabelled_with_a_wandb_pin(tmp_path: Path) -> None:
    destination = _copy_repository_fixture(tmp_path)
    search = destination / "benchmarks/ecocorridor/search"
    path = search / "release-gate.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["pinned_wandb_artifact"] = "wandb-artifact://entity/project/benchmark:v7"
    payload["production_adaptive_enabled"] = False
    payload["campaign_sha256"] = publisher._canonical_sha256(
        {key: value for key, value in payload.items() if key != "campaign_sha256"}
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="provenance belongs to another campaign"):
        publisher.verify_search_inputs(destination)


class _Node:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeTable:
    def __init__(self, *, columns: list[str], data: list[list[object]]) -> None:
        self.columns = columns
        self.data = data


class _FakeArtifact:
    def __init__(self, name: str, *, type: str, metadata: dict[str, Any]) -> None:
        self.base_name = name
        self.type = type
        self.metadata = metadata
        self.generated: dict[str, str | bytes] = {}
        self.qualified_name = "wildlife-team/ecocorridor-public/placeholder:v0"
        self.url = (
            "https://wandb.example/wildlife-team/ecocorridor-public/artifacts/"
            f"{type}/placeholder/v0"
        )

    @contextlib.contextmanager
    def new_file(self, name: str, *, mode: str, **_kwargs: Any):
        stream: io.StringIO | io.BytesIO
        stream = io.BytesIO() if "b" in mode else io.StringIO()
        yield stream
        self.generated[name] = stream.getvalue()

    def wait(self) -> _FakeArtifact:
        return self


class _FakePublicRun:
    def __init__(
        self,
        *,
        run_id: str,
        state: str,
        config: dict[str, Any],
        summary: dict[str, Any],
        logged_artifacts: list[_FakeArtifact] | None = None,
        url: str | None = None,
    ) -> None:
        self.id = run_id
        self.state = state
        self.config = config
        self.summary = summary
        self._logged_artifacts = list(logged_artifacts or [])
        self.logged_artifact_calls = 0
        self.url = url or ("https://wandb.example/wildlife-team/ecocorridor-public/runs/" + run_id)

    def logged_artifacts(self) -> list[_FakeArtifact]:
        self.logged_artifact_calls += 1
        return list(self._logged_artifacts)


class _FakeBetaReport:
    def __init__(
        self,
        *,
        report_id: str,
        display_name: str,
        description: str,
        url: str,
    ) -> None:
        self.id = report_id
        self.display_name = display_name
        self.description = description
        self.url = url


class _FakeApi:
    def __init__(self) -> None:
        self.public_runs: list[_FakePublicRun] = []
        self.public_reports: list[_FakeBetaReport] = []
        self.runs_calls: list[dict[str, Any]] = []
        self.reports_calls: list[dict[str, Any]] = []

    def runs(self, **kwargs: Any) -> list[_FakePublicRun]:
        self.runs_calls.append(kwargs)
        run_id = kwargs["filters"]["name"]
        return [run for run in self.public_runs if run.id == run_id]

    def reports(self, **kwargs: Any) -> list[_FakeBetaReport]:
        self.reports_calls.append(kwargs)
        name = kwargs.get("name")
        return [
            report for report in self.public_reports if name is None or report.display_name == name
        ]


class _FakeRun:
    def __init__(self, sdk: _FakeWandb, init: dict[str, Any]) -> None:
        self._sdk = sdk
        self.id = str(init["id"])
        self.config = dict(init["config"])
        self.url = "https://wandb.example/wildlife-team/ecocorridor-public/runs/" + self.id
        self.logs: list[dict[str, Any]] = []
        self.summary: dict[str, Any] = {}
        self.artifact: _FakeArtifact | None = None
        self.artifact_log_kwargs: list[dict[str, Any]] = []
        self.finish_codes: list[int] = []

    def log(self, payload: dict[str, Any]) -> None:
        self.logs.append(payload)

    def log_artifact(self, artifact: _FakeArtifact, **kwargs: Any) -> _FakeArtifact:
        artifact.qualified_name = f"wildlife-team/ecocorridor-public/{artifact.base_name}:v7"
        artifact.url = (
            "https://wandb.example/wildlife-team/ecocorridor-public/artifacts/"
            f"{artifact.type}/{artifact.base_name}/v7"
        )
        self.artifact = artifact
        self.artifact_log_kwargs.append(kwargs)
        return artifact

    def finish(self, *, exit_code: int) -> None:
        self.finish_codes.append(exit_code)
        self._sdk.api.public_runs = [run for run in self._sdk.api.public_runs if run.id != self.id]
        self._sdk.api.public_runs.append(
            _FakePublicRun(
                run_id=self.id,
                state="finished" if exit_code == 0 else "failed",
                config=dict(self.config),
                summary=dict(self.summary),
                logged_artifacts=[self.artifact] if self.artifact is not None else [],
                url=self.url,
            )
        )


class _FakeWandb:
    Table = _FakeTable
    Artifact = _FakeArtifact

    def __init__(self) -> None:
        self.api = _FakeApi()
        self.run: _FakeRun | None = None
        self.init_calls: list[dict[str, Any]] = []

    @staticmethod
    def Settings(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    def Api(self) -> _FakeApi:
        return self.api

    def init(self, **kwargs: Any) -> _FakeRun:
        self.init_calls.append(kwargs)
        self.run = _FakeRun(self, kwargs)
        return self.run


class _FakeReport(_Node):
    created: ClassVar[list[_FakeReport]] = []
    explicit: ClassVar[dict[str, _FakeReport]] = {}
    api: ClassVar[_FakeApi | None] = None
    fail_after_persist: ClassVar[bool] = False
    save_events: ClassVar[list[str]] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.id = f"report-{len(self.__class__.created) + 1}"
        self.url = (
            f"https://wandb.example/{self.entity}/{self.project}/reports/"
            f"ecocorridor-search--{self.id}"
        )
        self.saved = 0
        self.__class__.created.append(self)

    @classmethod
    def from_url(cls, url: str) -> _FakeReport:
        if url in cls.explicit:
            return cls.explicit[url]
        assert cls.api is not None
        beta = next(report for report in cls.api.public_reports if report.url == url)
        report = cls.__new__(cls)
        _Node.__init__(
            report,
            entity="wildlife-team",
            project="ecocorridor-public",
            title=beta.display_name,
            description=beta.description,
            blocks=[],
            width="fluid",
        )
        report.id = beta.id
        report.url = beta.url
        report.saved = 0
        return report

    def save(self) -> _FakeReport:
        assert self.__class__.api is not None
        assert any(
            run.state == "finished"
            and run.summary.get("publisher/status") == publisher.RUN_COMPLETION_STATUS
            for run in self.__class__.api.public_runs
        ), "Report.save occurred before the completed run summary was finalized"
        self.saved += 1
        self.__class__.save_events.append("report.save")
        beta = _FakeBetaReport(
            report_id=self.id,
            display_name=self.title,
            description=self.description,
            url=self.url,
        )
        self.__class__.api.public_reports = [
            report for report in self.__class__.api.public_reports if report.id != self.id
        ]
        self.__class__.api.public_reports.append(beta)
        if self.__class__.fail_after_persist:
            raise RuntimeError("simulated Report.save failure after persistence")
        return self


class _FakeReports(SimpleNamespace):
    def __init__(self, api: _FakeApi) -> None:
        _FakeReport.created.clear()
        _FakeReport.explicit.clear()
        _FakeReport.api = api
        _FakeReport.fail_after_persist = False
        _FakeReport.save_events.clear()
        super().__init__(
            Report=_FakeReport,
            Runset=_Node,
            TableOfContents=_Node,
            H1=_Node,
            H2=_Node,
            CalloutBlock=_Node,
            MarkdownBlock=_Node,
            PanelGrid=_Node,
            ScalarChart=_Node,
            LinePlot=_Node,
            WeavePanelSummaryTable=_Node,
            Layout=_Node,
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, _FakeTable):
        return {"columns": value.columns, "data": value.data}
    if isinstance(value, _Node):
        return {key: _jsonable(item) for key, item in value.__dict__.items()}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    return value


def _add_completed_run(wandb: _FakeWandb, plan: dict[str, Any]) -> _FakePublicRun:
    artifact_ref = (
        f"wildlife-team/ecocorridor-public/{plan['wandb_projection']['artifact_name']}:v7"
    )
    artifact_url = (
        "https://wandb.example/wildlife-team/ecocorridor-public/artifacts/"
        f"{plan['wandb_projection']['artifact_type']}/"
        f"{plan['wandb_projection']['artifact_name']}/v7"
    )
    artifact = _FakeArtifact(
        plan["wandb_projection"]["artifact_name"],
        type=plan["wandb_projection"]["artifact_type"],
        metadata=publisher._expected_artifact_metadata(plan),
    )
    artifact.qualified_name = artifact_ref
    artifact.url = artifact_url
    run = _FakePublicRun(
        run_id=plan["wandb_projection"]["run_id"],
        state="finished",
        config=publisher._expected_run_config(plan),
        summary=publisher._completion_summary(
            plan, artifact_ref=artifact_ref, artifact_url=artifact_url
        ),
        logged_artifacts=[artifact],
    )
    wandb.api.public_runs.append(run)
    return run


def _add_marked_report(wandb: _FakeWandb, plan: dict[str, Any], *, report_id: str) -> None:
    wandb.api.public_reports.append(
        _FakeBetaReport(
            report_id=report_id,
            display_name=plan["wandb_projection"]["report_title"],
            description=publisher._report_description(plan),
            url=(
                "https://wandb.example/wildlife-team/ecocorridor-public/reports/"
                f"ecocorridor-search--{report_id}"
            ),
        )
    )


def test_publish_logs_one_run_typed_tables_artifact_and_narrative_report() -> None:
    inputs, plan = _plan()
    before = (SEARCH_ROOT / "release-gate.json").read_bytes()
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    receipt = publisher.publish_plan(
        plan,
        inputs,
        entity="wildlife-team",
        project="ecocorridor-public",
        authorize_synthetic_public_upload=True,
        wandb_module=wandb,
        reports_module=reports,
    )

    assert len(wandb.init_calls) == 1
    init = wandb.init_calls[0]
    assert init["job_type"] == "search_benchmark"
    assert init["config"]["run_kind"] == "release_gate"
    assert init["config"]["dataset_disposition"] == "HISTORICAL_BENCHMARK_ONLY"
    assert init["config"]["production_authorization"] == "PROHIBITED"
    assert init["config"]["production_adaptive_enabled"] is False
    assert init["config"]["publisher_schema_version"] == publisher.PUBLISHER_SCHEMA_VERSION
    assert init["resume"] == "never"

    assert wandb.run is not None
    table_logs = {
        key: value
        for log in wandb.run.logs
        for key, value in log.items()
        if isinstance(value, _FakeTable)
    }
    assert len(table_logs["search_benchmark_runs"].data) == 240
    assert len(table_logs["search_method_aggregates"].data) == 6
    assert len(table_logs["release_gate_metrics"].data) == 10
    assert len(table_logs["resource_observation"].data) == 1
    assert len(table_logs["validated_inputs"].data) == 10
    assert wandb.run.summary["release_gate/scientific_gate_passed"] == 0
    assert wandb.run.summary["release_gate/production_adaptive_enabled"] == 0
    assert wandb.run.summary["publisher/status"] == "COMPLETE"
    assert wandb.run.finish_codes == [0]

    artifact = wandb.run.artifact
    assert artifact is not None
    assert artifact.type == "benchmark-dataset"
    assert artifact.metadata["production_authorization"] == "PROHIBITED"
    assert artifact.metadata["production_adaptive_enabled"] is False
    assert artifact.metadata["data_manifest_sha256"] == inputs.data_manifest_sha256
    assert artifact.metadata["source_repository"] == plan["source"]["repository"]
    assert wandb.run.artifact_log_kwargs == [{}]
    input_names = [name for name in artifact.generated if name.startswith("inputs/")]
    assert len(input_names) == 10
    assert all(name.startswith("inputs/benchmarks/ecocorridor/search/") for name in input_names)
    assert all(not Path(name).is_absolute() for name in input_names)
    artifact_plan = json.loads(str(artifact.generated["report-plan.json"]))
    assert artifact_plan["plan_sha256"] == plan["plan_sha256"]

    assert len(_FakeReport.created) == 1
    report = _FakeReport.created[0]
    assert report.saved == 1
    blocks = report.blocks
    rendered = json.dumps(_jsonable(blocks), sort_keys=True)
    assert "HISTORICAL_BENCHMARK_ONLY" in rendered
    assert "Adaptive ML is disabled" in rendered
    assert "Product context (not build verification)" in rendered
    assert "UNVERIFIED_PRODUCT_CONTEXT" in rendered
    assert "Why this is useful" in rendered
    assert "240" in rendered
    assert "search_benchmark_runs" in rendered

    cloud_projection = {
        "config": init["config"],
        "tables": table_logs,
        "artifact_metadata": artifact.metadata,
        "artifact_names": input_names,
        "artifact_plan": artifact_plan,
        "report_blocks": blocks,
        "receipt": receipt,
    }
    serialized = json.dumps(_jsonable(cloud_projection), sort_keys=True)
    assert str(ROOT) not in serialized
    assert "/Users/" not in serialized
    assert "WANDB_API_KEY" not in serialized
    assert (SEARCH_ROOT / "release-gate.json").read_bytes() == before
    assert receipt["artifact_ref"].endswith(":v7")
    assert receipt["production_search_method"] == "scrambled_sobol"
    assert receipt["run_reused"] is False
    assert receipt["report_reused"] is False
    assert publisher._parse_report_marker(report.description) == publisher._report_marker(plan)


def test_publish_requires_explicit_target_authorization_and_clean_source() -> None:
    inputs, plan = _plan()
    with pytest.raises(PermissionError, match="authorize-synthetic-public-upload"):
        publisher.publish_plan(
            plan,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=False,
            wandb_module=object(),
            reports_module=object(),
        )

    _, dirty_plan = _plan(source_tree_clean=False)
    with pytest.raises(PermissionError, match="clean source tree"):
        publisher.publish_plan(
            dirty_plan,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=True,
            wandb_module=object(),
            reports_module=object(),
        )


def test_publish_rejects_a_rehashed_but_forged_table_projection() -> None:
    inputs, plan = _plan()
    forged = json.loads(json.dumps(plan))
    forged["tables"]["search_benchmark_runs"]["rows"][0][5] = 1.0
    table = forged["tables"]["search_benchmark_runs"]
    table["sha256"] = publisher._canonical_sha256(
        {key: value for key, value in table.items() if key != "sha256"}
    )
    forged["plan_sha256"] = publisher._canonical_sha256(
        {key: value for key, value in forged.items() if key != "plan_sha256"}
    )
    with pytest.raises(ValueError, match="differs from the verified benchmark projection"):
        publisher.publish_plan(
            forged,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=True,
            wandb_module=object(),
            reports_module=object(),
        )


def test_report_update_reuses_a_matching_destination() -> None:
    inputs, plan = _plan()
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    _add_completed_run(wandb, plan)
    existing = _FakeReport(
        entity="wildlife-team",
        project="ecocorridor-public",
        title="Old title",
        description=publisher._report_description(plan),
        blocks=[],
    )
    explicit_url = "https://wandb.example/wildlife-team/ecocorridor-public/reports/existing"
    existing.url = explicit_url
    _FakeReport.explicit[explicit_url] = existing
    receipt = publisher.publish_plan(
        plan,
        inputs,
        entity="wildlife-team",
        project="ecocorridor-public",
        authorize_synthetic_public_upload=True,
        report_url=explicit_url,
        wandb_module=wandb,
        reports_module=reports,
    )
    assert existing.saved == 1
    assert existing.title == plan["wandb_projection"]["report_title"]
    assert existing.blocks
    assert wandb.init_calls == []
    assert receipt["report_url"] == existing.url
    assert receipt["run_reused"] is True
    assert receipt["report_reused"] is True


def test_report_update_fails_before_wandb_upload_for_wrong_destination() -> None:
    inputs, plan = _plan()
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    existing = _FakeReport(
        entity="another-team",
        project="another-project",
        title="Existing",
        description="",
        blocks=[],
    )
    explicit_url = "https://wandb.example/another-team/another-project/reports/existing"
    _FakeReport.explicit[explicit_url] = existing
    with pytest.raises(PermissionError, match="different entity or project"):
        publisher.publish_plan(
            plan,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=True,
            report_url=explicit_url,
            wandb_module=wandb,
            reports_module=reports,
        )
    assert wandb.init_calls == []


def test_completed_deterministic_run_is_reused_without_relogging() -> None:
    inputs, plan = _plan()
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    public_run = _add_completed_run(wandb, plan)

    receipt = publisher.publish_plan(
        plan,
        inputs,
        entity="wildlife-team",
        project="ecocorridor-public",
        authorize_synthetic_public_upload=True,
        wandb_module=wandb,
        reports_module=reports,
    )

    assert wandb.init_calls == []
    assert receipt["run_reused"] is True
    assert receipt["report_reused"] is False
    assert receipt["run_url"] == public_run.url
    assert public_run.logged_artifact_calls == 1
    assert len(wandb.api.public_runs) == 1
    assert len(wandb.api.public_reports) == 1


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("summary_collection", "immutable artifact version"),
        ("missing_logged_version", "does not log exactly"),
        ("duplicate_logged_version", "does not log exactly"),
        ("artifact_type", "artifact type"),
        ("artifact_metadata", "artifact metadata"),
        ("artifact_destination", "another destination"),
        ("artifact_url_mismatch", "does not match the completed summary"),
    ],
)
def test_completed_run_requires_exact_logged_artifact_lineage(mode: str, error: str) -> None:
    inputs, plan = _plan()
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    public_run = _add_completed_run(wandb, plan)
    artifact = public_run._logged_artifacts[0]
    if mode == "summary_collection":
        public_run.summary["lineage/artifact_ref"] = (
            "wildlife-team/ecocorridor-public/unrelated-artifact:v7"
        )
    elif mode == "missing_logged_version":
        public_run._logged_artifacts = []
    elif mode == "duplicate_logged_version":
        public_run._logged_artifacts.append(artifact)
    elif mode == "artifact_type":
        artifact.type = "model"
    elif mode == "artifact_metadata":
        artifact.metadata = {**artifact.metadata, "source_commit": "b" * 40}
    elif mode == "artifact_destination":
        artifact.url = artifact.url.replace("wildlife-team", "another-team")
    else:
        artifact.url = artifact.url.replace("wandb.example", "other-wandb.example")

    with pytest.raises(RuntimeError, match=error):
        publisher.publish_plan(
            plan,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=True,
            wandb_module=wandb,
            reports_module=reports,
        )
    assert wandb.init_calls == []
    assert wandb.api.public_reports == []


@pytest.mark.parametrize("mode", ["partial", "mismatched"])
def test_existing_deterministic_run_without_exact_completion_blocks(mode: str) -> None:
    inputs, plan = _plan()
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    public_run = _add_completed_run(wandb, plan)
    if mode == "partial":
        public_run.state = "failed"
        public_run.summary = {}
    else:
        public_run.config = {**public_run.config, "source_commit": "b" * 40}

    with pytest.raises(RuntimeError, match="deterministic W&B run"):
        publisher.publish_plan(
            plan,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=True,
            wandb_module=wandb,
            reports_module=reports,
        )
    assert wandb.init_calls == []
    assert wandb.api.public_reports == []


def test_report_save_failure_retries_without_relogging_or_duplicating_report() -> None:
    inputs, plan = _plan()
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    _FakeReport.fail_after_persist = True
    with pytest.raises(RuntimeError, match="after persistence"):
        publisher.publish_plan(
            plan,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=True,
            wandb_module=wandb,
            reports_module=reports,
        )
    assert len(wandb.init_calls) == 1
    assert len(wandb.api.public_runs) == 1
    assert len(wandb.api.public_reports) == 1

    _FakeReport.fail_after_persist = False
    receipt = publisher.publish_plan(
        plan,
        inputs,
        entity="wildlife-team",
        project="ecocorridor-public",
        authorize_synthetic_public_upload=True,
        wandb_module=wandb,
        reports_module=reports,
    )
    assert len(wandb.init_calls) == 1
    assert len(wandb.api.public_runs) == 1
    assert len(wandb.api.public_reports) == 1
    assert receipt["run_reused"] is True
    assert receipt["report_reused"] is True


def test_duplicate_or_orphan_publisher_reports_block_before_upload() -> None:
    inputs, plan = _plan()
    for mode in ("duplicate", "orphan"):
        wandb = _FakeWandb()
        reports = _FakeReports(wandb.api)
        if mode == "duplicate":
            _add_completed_run(wandb, plan)
            _add_marked_report(wandb, plan, report_id="report-a")
            _add_marked_report(wandb, plan, report_id="report-b")
            error = "multiple W&B Reports"
        else:
            _add_marked_report(wandb, plan, report_id="report-a")
            error = "without its deterministic run"
        with pytest.raises(RuntimeError, match=error):
            publisher.publish_plan(
                plan,
                inputs,
                entity="wildlife-team",
                project="ecocorridor-public",
                authorize_synthetic_public_upload=True,
                wandb_module=wandb,
                reports_module=reports,
            )
        assert wandb.init_calls == []


def test_explicit_unrelated_report_cannot_be_clobbered() -> None:
    inputs, plan = _plan()
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    _add_completed_run(wandb, plan)
    unrelated = _FakeReport(
        entity="wildlife-team",
        project="ecocorridor-public",
        title="Unrelated",
        description="Not owned by this publisher.",
        blocks=[],
    )
    explicit_url = unrelated.url
    _FakeReport.explicit[explicit_url] = unrelated
    with pytest.raises(PermissionError, match="not owned by this exact publisher plan"):
        publisher.publish_plan(
            plan,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=True,
            report_url=explicit_url,
            wandb_module=wandb,
            reports_module=reports,
        )
    assert unrelated.saved == 0
    assert wandb.init_calls == []


def test_runset_is_bound_to_exact_run_plan_source_and_kind() -> None:
    _, plan = _plan()
    blocks = publisher._build_report_blocks(
        _FakeReports(_FakeApi()),
        plan,
        entity="wildlife-team",
        project="ecocorridor-public",
        run_url="https://wandb.example/wildlife-team/ecocorridor-public/runs/run-1",
        artifact_ref="wildlife-team/ecocorridor-public/search-gate:v7",
    )
    runsets = [runset for block in blocks if hasattr(block, "runsets") for runset in block.runsets]
    assert runsets
    filters = runsets[0].filters
    assert f"ID = '{plan['wandb_projection']['run_id']}'" in filters
    assert f"Config('plan_sha256') = '{plan['plan_sha256']}'" in filters
    assert f"Config('source_commit') = '{plan['source']['commit']}'" in filters
    assert "Config('run_kind') = 'release_gate'" in filters


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b'{"path":"/Users/alice/private/input.json"}', "local filesystem path"),
        (
            b"WANDB_" + b"API" + b"_KEY=" + b"0123456789abcdef" * 2 + b"01234567",
            "credential-shaped",
        ),
    ],
)
def test_raw_artifact_bytes_reject_local_paths_and_secrets(
    tmp_path: Path, payload: bytes, error: str
) -> None:
    inputs, _ = _plan()
    logical_name = inputs.files[0][0]
    path = tmp_path / "input.txt"
    path.write_bytes(payload)
    authorization = {
        **inputs.upload_authorizations[logical_name],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
    }
    modified = replace(
        inputs,
        files=((logical_name, path),),
        upload_authorizations={logical_name: authorization},
    )
    with pytest.raises(PermissionError, match=error):
        publisher._authorized_input_bytes(modified)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (
            b'{"path":"\\u002fUsers\\u002falice\\u002fprivate.json"}',
            "local filesystem path",
        ),
        (
            b'{"value":"sk-\\u0041BCDEFGHIJKLMNOPQRSTUVWXYZ123456"}',
            "credential-shaped material",
        ),
        (
            b'{"WANDB_\\u0041PI_KEY":"0123456789abcdef0123456789abcdef01234567"}',
            "credential-shaped mapping key",
        ),
    ],
)
def test_json_artifact_semantics_reject_escaped_paths_credentials_and_keys(
    tmp_path: Path, payload: bytes, error: str
) -> None:
    inputs, _ = _plan()
    logical_name = inputs.files[0][0]
    path = tmp_path / "input.json"
    path.write_bytes(payload)
    authorization = {
        **inputs.upload_authorizations[logical_name],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "media_type": "application/json",
    }
    modified = replace(
        inputs,
        files=((logical_name, path),),
        upload_authorizations={logical_name: authorization},
    )
    with pytest.raises(PermissionError, match=error):
        publisher._authorized_input_bytes(modified)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b'{"value":1,"value":2}', "duplicate object key"),
        (b'{"value":', "malformed"),
        (b'{"value":NaN}', "non-finite number"),
        (b'{"value":Infinity}', "non-finite number"),
        (b'{"value":-Infinity}', "non-finite number"),
    ],
)
def test_json_artifact_bytes_require_strict_json(
    tmp_path: Path, payload: bytes, error: str
) -> None:
    inputs, _ = _plan()
    logical_name = inputs.files[0][0]
    path = tmp_path / "input.json"
    path.write_bytes(payload)
    authorization = {
        **inputs.upload_authorizations[logical_name],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "media_type": "application/json",
    }
    modified = replace(
        inputs,
        files=((logical_name, path),),
        upload_authorizations={logical_name: authorization},
    )
    with pytest.raises(ValueError, match=error):
        publisher._authorized_input_bytes(modified)


def test_non_utf8_artifact_input_requires_explicit_binary_authorization(tmp_path: Path) -> None:
    inputs, _ = _plan()
    logical_name = inputs.files[0][0]
    path = tmp_path / "input.bin"
    payload = b"\xff\xfe\x00"
    path.write_bytes(payload)
    authorization = {
        **inputs.upload_authorizations[logical_name],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "media_type": "application/octet-stream",
    }
    modified = replace(
        inputs,
        files=((logical_name, path),),
        upload_authorizations={logical_name: authorization},
    )
    with pytest.raises(PermissionError, match="non-UTF-8"):
        publisher._authorized_input_bytes(modified)

    authorization[publisher.BINARY_UPLOAD_AUTHORIZATION_FIELD] = True
    allowed = replace(modified, upload_authorizations={logical_name: authorization})
    assert publisher._authorized_input_bytes(allowed)[logical_name] == payload


def test_json_artifact_cannot_use_binary_authorization_to_bypass_utf8(
    tmp_path: Path,
) -> None:
    inputs, _ = _plan()
    logical_name = inputs.files[0][0]
    path = tmp_path / "input.json"
    payload = b"\xff\xfe\x00"
    path.write_bytes(payload)
    authorization = {
        **inputs.upload_authorizations[logical_name],
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "media_type": "application/json",
        publisher.BINARY_UPLOAD_AUTHORIZATION_FIELD: True,
    }
    modified = replace(
        inputs,
        files=((logical_name, path),),
        upload_authorizations={logical_name: authorization},
    )
    with pytest.raises(ValueError, match="not valid UTF-8"):
        publisher._authorized_input_bytes(modified)


@pytest.mark.parametrize("field", ["sha256", "sensitivity", "media_type", "missing"])
def test_data_manifest_must_exactly_authorize_every_artifact_input(
    tmp_path: Path, field: str
) -> None:
    destination = _copy_repository_fixture(tmp_path)
    manifest_path = destination / "benchmarks/ecocorridor/data-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    target_path = "benchmarks/ecocorridor/search/release-gate.json"
    record = next(item for item in payload["files"] if item["path"] == target_path)
    if field == "sha256":
        record["sha256"] = "0" * 64
    elif field == "sensitivity":
        record["sensitivity"] = "RESTRICTED"
    elif field == "media_type":
        record["media_type"] = "text/plain"
    else:
        payload["files"].remove(record)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PermissionError, match="W&B artifact input"):
        publisher.verify_search_inputs(destination)


def test_public_replay_flag_is_independent_of_wandb_upload_authorization() -> None:
    inputs, plan = _plan()
    provenance_path = "benchmarks/ecocorridor/search/historical-provenance.json"
    assert inputs.upload_authorizations[provenance_path]["public_replay_allowed"] is False
    assert (
        next(item for item in plan["artifact_inputs"] if item["logical_name"] == provenance_path)[
            "upload_authorized"
        ]
        is True
    )


def test_git_remote_must_match_the_reported_source_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, plan = _plan()
    assert (
        publisher._normalize_github_repository("ssh://git@github.com/ash0ts/gi-scenario-studio.git")
        == "ash0ts/gi-scenario-studio"
    )
    monkeypatch.setattr(publisher, "_git_source", lambda _root: ("b" * 40, True))
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    with pytest.raises(PermissionError, match="checked-out Git commit"):
        publisher.publish_plan(
            plan,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=True,
            wandb_module=wandb,
            reports_module=reports,
        )
    assert wandb.init_calls == []

    monkeypatch.setattr(publisher, "_git_source", lambda _root: ("a" * 40, True))
    monkeypatch.setattr(publisher, "_git_remote_repository", lambda _root: "other/repository")
    wandb = _FakeWandb()
    reports = _FakeReports(wandb.api)
    with pytest.raises(PermissionError, match="Git origin"):
        publisher.publish_plan(
            plan,
            inputs,
            entity="wildlife-team",
            project="ecocorridor-public",
            authorize_synthetic_public_upload=True,
            wandb_module=wandb,
            reports_module=reports,
        )
    assert wandb.init_calls == []
