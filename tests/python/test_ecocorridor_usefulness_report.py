from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar

import pytest
from ecocorridor_core.usefulness_reporting import (
    UsefulnessCostBasis,
    build_usefulness_publication_plan,
)

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publisher = _script("publish_ecocorridor_usefulness_report")


@pytest.fixture(scope="module")
def source_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def plan(source_commit: str):
    models = ("openai/gpt-oss-120b", "owner/frontier", "owner/efficient")
    cost_basis = UsefulnessCostBasis.create(
        reference="frozen fake-W&B test basis",
        rates_by_model={model: (1.0, 2.0) for model in models},
    )
    instant = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    return build_usefulness_publication_plan(
        ROOT,
        source_repository="ash0ts/gi-scenario-studio",
        source_commit=source_commit,
        source_tree_clean=True,
        model_ids=models,
        cost_basis=cost_basis,
        preregistered_at=instant,
        frozen_at=instant,
        generated_at=instant,
    )


class _NotFound(RuntimeError):
    def __init__(self) -> None:
        super().__init__("not found")
        self.response = SimpleNamespace(status_code=404)


class _Node:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._spec = {"args": list(args), **kwargs}
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeTable:
    def __init__(self, *, columns: list[str], data: list[list[Any]]) -> None:
        self.columns = list(columns)
        self.data = list(data)


class _FakeArtifact:
    def __init__(self, name: str, *, type: str, metadata: dict[str, Any]) -> None:
        self.base_name = name
        self.type = type
        self.metadata = dict(metadata)
        self.files: dict[str, bytes] = {}
        self.qualified_name = ""
        self.url = ""
        self.corrupt_download = False

    @contextlib.contextmanager
    def new_file(self, name: str, *, mode: str, **_kwargs: Any):
        assert mode == "wb"
        stream = io.BytesIO()
        yield stream
        self.files[name] = stream.getvalue()

    def wait(self):
        return self

    def download(self, *, root: str) -> str:
        destination = Path(root) / "artifact"
        for name, payload in self.files.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if self.corrupt_download and name == min(self.files):
                payload += b"tampered"
            path.write_bytes(payload)
        return str(destination)


class _FakePublicRun:
    def __init__(
        self,
        *,
        run_id: str,
        url: str,
        state: str,
        config: dict[str, Any],
        summary: dict[str, Any],
    ) -> None:
        self.id = run_id
        self.url = url
        self.state = state
        self.config = config
        self.summary = summary


class _FakeApi:
    def __init__(self) -> None:
        self.runs: dict[str, _FakePublicRun] = {}
        self.artifacts: dict[str, _FakeArtifact] = {}

    def run(self, path: str) -> _FakePublicRun:
        if path not in self.runs:
            raise _NotFound()
        return self.runs[path]

    def artifact(self, ref: str) -> _FakeArtifact:
        if ref not in self.artifacts:
            raise _NotFound()
        return self.artifacts[ref]


class _FakeRun:
    def __init__(self, sdk: _FakeWandb, arguments: dict[str, Any]) -> None:
        self.sdk = sdk
        self.id = str(arguments["id"])
        self.config = dict(arguments["config"])
        self.url = "https://wandb.example/wildlife-team/ecocorridor-scenario-studio/runs/" + self.id
        self.summary: dict[str, Any] = {}
        self.logs: list[dict[str, Any]] = []
        self.artifact: _FakeArtifact | None = None
        self.finish_codes: list[int] = []

    def log(self, value: dict[str, Any]) -> None:
        self.logs.append(value)

    def log_artifact(self, artifact: _FakeArtifact) -> _FakeArtifact:
        artifact.qualified_name = (
            f"wildlife-team/ecocorridor-scenario-studio/{artifact.base_name}:v0"
        )
        artifact.url = (
            "https://wandb.example/wildlife-team/ecocorridor-scenario-studio/artifacts/"
            f"{artifact.type}/{artifact.base_name}/v0"
        )
        artifact.corrupt_download = self.sdk.corrupt_artifact_download
        self.sdk.api.artifacts[artifact.qualified_name] = artifact
        self.artifact = artifact
        return artifact

    def finish(self, *, exit_code: int) -> None:
        self.finish_codes.append(exit_code)
        state = "finished" if exit_code == 0 else "failed"
        path = f"wildlife-team/ecocorridor-scenario-studio/{self.id}"
        self.sdk.api.runs[path] = _FakePublicRun(
            run_id=self.id,
            url=self.url,
            state=state,
            config=dict(self.config),
            summary=dict(self.summary),
        )


class _FakeWandb:
    Table = _FakeTable
    Artifact = _FakeArtifact

    def __init__(self) -> None:
        self.api = _FakeApi()
        self.init_calls: list[dict[str, Any]] = []
        self.active_runs: list[_FakeRun] = []
        self.corrupt_artifact_download = False

    @staticmethod
    def Settings(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    def Api(self, **_kwargs: Any) -> _FakeApi:
        return self.api

    def init(self, **kwargs: Any) -> _FakeRun:
        assert kwargs["resume"] == "never"
        self.init_calls.append(kwargs)
        run = _FakeRun(self, kwargs)
        self.active_runs.append(run)
        return run


class _FakeReport:
    reports: ClassVar[dict[str, _FakeReport]] = {}

    def __init__(self, *, url: str, blocks: list[Any]) -> None:
        self.url = url
        self.entity = "wildlife-team"
        self.project = "ecocorridor-scenario-studio"
        self.blocks = list(blocks)
        self.saved = 0
        self.mutate_historical_on_save = False
        self.__class__.reports[url] = self

    @classmethod
    def from_url(cls, url: str):
        return cls.reports[url]

    def save(self):
        self.saved += 1
        if self.mutate_historical_on_save:
            self.blocks[0] = _Node(text="historical content was changed")
        return self


class _FakeReports(SimpleNamespace):
    def __init__(self) -> None:
        _FakeReport.reports.clear()
        super().__init__(
            Report=_FakeReport,
            H1=_Node,
            H2=_Node,
            CalloutBlock=_Node,
            MarkdownBlock=_Node,
        )


def _adapter(
    wandb: _FakeWandb,
    reports: _FakeReports,
    *,
    authorized: bool = True,
):
    return publisher.WandbUsefulnessPublisher(
        entity="wildlife-team",
        project="ecocorridor-scenario-studio",
        api_base_url="https://api.wandb.example",
        web_base_url="https://wandb.example",
        authorize_synthetic_public_upload=authorized,
        wandb_module=wandb,
        reports_module=reports,
    )


def test_publish_logs_typed_tables_immutable_payloads_and_append_only_report(plan) -> None:
    wandb = _FakeWandb()
    reports = _FakeReports()
    report_url = "https://wandb.example/wildlife-team/ecocorridor-scenario-studio/reports/article"
    historical = _Node(text="historical result stays byte-for-byte represented")
    report = _FakeReport(url=report_url, blocks=[historical])

    receipt = publisher.publish_usefulness_report(
        plan=plan,
        report_url=report_url,
        publisher=_adapter(wandb, reports),
    )

    assert receipt.status == "COMPLETE"
    assert receipt.observed_effectiveness_result_count == 0
    assert receipt.new_result_media_logged is False
    assert receipt.run_readback_verified
    assert receipt.artifact_readback_verified
    assert receipt.report_readback_verified
    assert len(wandb.init_calls) == 1
    assert wandb.init_calls[0]["resume"] == "never"
    assert wandb.init_calls[0]["config"]["observed_effectiveness_result_count"] == 0
    assert "planned_not_observed" in wandb.init_calls[0]["tags"]

    run = wandb.active_runs[0]
    assert run.finish_codes == [0]
    table_log = run.logs[0]
    assert set(table_log) == set(plan.tables)
    assert all(isinstance(value, _FakeTable) for value in table_log.values())
    assert len(table_log["usefulness_safety_attacks"].data) == 1_000
    assert all(
        row[-1] == "PLANNED_NOT_OBSERVED" for row in table_log["usefulness_safety_attacks"].data
    )
    assert run.summary["usefulness/status"] == "COMPLETE"
    assert run.summary["usefulness/new_result_media_logged"] == 0
    assert run.artifact is not None
    assert run.artifact.type == "usefulness-protocol"
    assert run.artifact.metadata["observed_effectiveness_result_count"] == 0
    assert run.artifact.files == {
        name: publisher._json_bytes(payload) for name, payload in plan.artifact_payloads.items()
    }

    assert report.blocks[0] is historical
    assert report.saved == 1
    report_text = "\n".join(str(getattr(block, "text", "")) for block in report.blocks)
    assert "These are study inputs, not observed usefulness results" in report_text
    assert "No new effectiveness animation or result media" in report_text
    assert "animal movement" in report_text


def test_exact_retry_reuses_run_artifact_and_report_section(plan) -> None:
    wandb = _FakeWandb()
    reports = _FakeReports()
    report_url = "https://wandb.example/wildlife-team/ecocorridor-scenario-studio/reports/article"
    report = _FakeReport(url=report_url, blocks=[_Node(text="historical")])
    adapter = _adapter(wandb, reports)

    first = publisher.publish_usefulness_report(plan=plan, report_url=report_url, publisher=adapter)
    block_count = len(report.blocks)
    second = publisher.publish_usefulness_report(
        plan=plan, report_url=report_url, publisher=adapter
    )

    assert first == second
    assert len(wandb.init_calls) == 1
    assert report.saved == 1
    assert len(report.blocks) == block_count


def test_publication_requires_explicit_synthetic_authorization() -> None:
    with pytest.raises(PermissionError, match="explicit CC0 synthetic"):
        _adapter(_FakeWandb(), _FakeReports(), authorized=False)


def test_artifact_payload_namespace_is_closed(plan) -> None:
    unsafe = plan.model_copy(
        update={"artifact_payloads": {**plan.artifact_payloads, "../escape.json": {}}}
    )
    with pytest.raises(ValueError, match="approved protocol"):
        publisher._validate_artifact_payloads(unsafe)


def test_artifact_byte_tampering_prevents_report_mutation(plan) -> None:
    wandb = _FakeWandb()
    wandb.corrupt_artifact_download = True
    reports = _FakeReports()
    report_url = "https://wandb.example/wildlife-team/ecocorridor-scenario-studio/reports/article"
    report = _FakeReport(url=report_url, blocks=[_Node(text="historical")])

    with pytest.raises(RuntimeError, match="artifact failed exact byte readback"):
        publisher.publish_usefulness_report(
            plan=plan,
            report_url=report_url,
            publisher=_adapter(wandb, reports),
        )

    assert report.saved == 0


def test_report_historical_mutation_fails_exact_readback(plan, monkeypatch) -> None:
    monkeypatch.setattr(publisher, "REPORT_READBACK_TIMEOUT_SECONDS", 0.0)
    wandb = _FakeWandb()
    reports = _FakeReports()
    report_url = "https://wandb.example/wildlife-team/ecocorridor-scenario-studio/reports/article"
    report = _FakeReport(url=report_url, blocks=[_Node(text="historical")])
    report.mutate_historical_on_save = True

    with pytest.raises(RuntimeError, match="append-only section readback"):
        publisher.publish_usefulness_report(
            plan=plan,
            report_url=report_url,
            publisher=_adapter(wandb, reports),
        )


def test_dry_run_is_network_free_and_never_claims_observed_results(
    plan, tmp_path: Path, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    output = tmp_path / "dry-run.json"
    plan_path.write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")

    assert (
        publisher.main(
            [
                "--repository-root",
                str(ROOT),
                "--plan",
                str(plan_path),
                "--dry-run",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "DRY_RUN"
    assert result["observed_effectiveness_result_count"] == 0
    assert result["new_result_media_logged"] is False
    assert "DRY_RUN" in capsys.readouterr().out


def test_online_source_gate_requires_both_frozen_and_current_clean_state(plan, monkeypatch) -> None:
    monkeypatch.setattr(
        publisher,
        "_git_source",
        lambda _root: (plan.source_commit, False),
    )
    with pytest.raises(PermissionError, match="exact clean source tree"):
        publisher.require_online_source(plan, repository_root=ROOT)
