from __future__ import annotations

import inspect
import json
import sys
import threading
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from gi_scenario_core.evidence_guide_v2_campaign import AttemptOutcome, AttemptSpec
from gi_scenario_core.evidence_guide_v2_cloud import (
    LocalCampaignPublisher,
    WandbBlockObserver,
    WandbCampaignPublisher,
    _campaign_run_id,
    _logged_artifact_reference,
    _quiet_settings,
    _safe,
    group_specs,
)


def test_cloud_run_identity_is_scoped_to_the_preregistration() -> None:
    first = _campaign_run_id(
        campaign_instance_sha256="a" * 64,
        kind="evaluation_block",
        entity="entity",
        project="project",
        identity="same-group",
    )
    second = _campaign_run_id(
        campaign_instance_sha256="b" * 64,
        kind="evaluation_block",
        entity="entity",
        project="project",
        identity="same-group",
    )

    assert first != second


def test_development_publisher_has_no_release_gate_input_and_verifies_local_readback(
    tmp_path: Path,
) -> None:
    package = tmp_path / "tables"
    package.mkdir()
    (package / "branch-outcomes.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "rows": [
                    {
                        "model_id": "ibm-granite/granite-4.1-8b",
                        "arm": "A1",
                        "candidate_conformant": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    publisher = LocalCampaignPublisher(tmp_path / "published")
    receipt = publisher.publish_architecture_development_study(package)
    signature = inspect.signature(
        WandbCampaignPublisher.publish_architecture_development_study
    )

    assert "release_gate_reference" not in signature.parameters
    assert receipt.kind == "local_architecture_development_study"
    assert receipt.source_sha256 == receipt.readback_sha256
    copied = Path(str(receipt.artifact_reference)) / "branch-outcomes.json"
    assert copied.read_bytes() == (package / "branch-outcomes.json").read_bytes()
    assert "release" not in copied.name

    (package / "release-gate.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="may not contain release-gate"):
        publisher.publish_architecture_development_study(package)


class _Prediction:
    def __init__(self) -> None:
        self.scores: dict[str, bool] = {}

    def log_score(self, *, scorer: str, score: bool) -> None:
        self.scores[scorer] = score

    def finish(self) -> None:
        return None


class _Logger:
    def __init__(self, **kwargs: Any) -> None:
        self.dataset = kwargs.get("dataset")
        self.outputs: list[dict[str, Any]] = []
        self.ui_url = "https://wandb.ai/entity/project/weave/evaluations/eval-call-1"
        self._evaluate_call = SimpleNamespace(id="eval-call-1")

    def log_prediction(self, *, output: dict[str, Any], **_: Any) -> _Prediction:
        self.outputs.append(output)
        return _Prediction()

    def log_summary(self, **_: Any) -> None:
        return None


class _Ref:
    def __init__(self, value: Any) -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def uri(self) -> str:
        return "weave:///entity/project/object:v1"


class _Run:
    def __init__(self, run_id: str = "run-1") -> None:
        self.id = run_id
        self.url = f"https://wandb.ai/entity/project/runs/{run_id}"
        self.summary: dict[str, Any] = {}
        self.finish_count = 0

    def use_artifact(self, _: str) -> None:
        return None

    def log(self, _: Any) -> None:
        return None

    def finish(self) -> None:
        self.finish_count += 1


class _Table:
    def __init__(self, *, columns: list[str]) -> None:
        self.columns = columns
        self.rows: list[tuple[Any, ...]] = []

    def add_data(self, *values: Any) -> None:
        self.rows.append(values)


class _WeaveClient:
    def __init__(
        self,
        *,
        outstanding_jobs: int = 0,
        acknowledge: bool = True,
        not_found_attempts: int = 0,
    ) -> None:
        self.project_id = "entity/gi-scenario-studio-agent-evals"
        self.num_outstanding_jobs = outstanding_jobs
        self.acknowledge = acknowledge
        self.not_found_attempts = not_found_attempts
        self.context: str | None = None
        self.set_context_count = 0
        self.clear_context_count = 0
        self.get_call_count = 0

    def set_wandb_run_context(self, run_id: str) -> None:
        self.context = run_id
        self.set_context_count += 1

    def clear_wandb_run_context(self) -> None:
        self.context = None
        self.clear_context_count += 1

    def get_call(self, call_id: str) -> Any | None:
        assert call_id == "eval-call-1"
        self.get_call_count += 1
        if self.get_call_count <= self.not_found_attempts:
            raise ValueError(f"Call not found: {call_id}")
        return SimpleNamespace(id=call_id) if self.acknowledge else None


def _spec(*, attempt_id: str = "a" * 32, repeat: int = 0) -> AttemptSpec:
    return AttemptSpec(
        attempt_id=attempt_id,
        stage="architecture",
        split="development",
        case_id="case-1",
        project_family="cc0",
        model_id="ibm-granite/granite-4.1-8b",
        architecture_id="A1",
        prompt_id="checklist",
        max_output_tokens=256,
        repeat=repeat,
        time_block="block-1",
    )


def _outcome() -> AttemptOutcome:
    return AttemptOutcome(
        attempt_id="a" * 32,
        status="completed",
        provider_invocations=1,
        transport_attempts=1,
        input_tokens=321,
        output_tokens=17,
        cost_usd=Decimal("0.001"),
        latency_ms=12,
        protocol_valid=True,
        safe=True,
        evidence_valid=True,
        repository_fidelity=True,
        injection_safe=True,
        causal=True,
        intent_correct=True,
        safety_intent_correct=True,
        false_refusal=False,
        trace_acknowledged=True,
    )


def test_cloud_scrubber_allows_usage_tokens_but_rejects_secrets() -> None:
    assert (
        _safe({"max_output_tokens": 256, "input_tokens": 321, "output_tokens": 17})["input_tokens"]
        == 321
    )
    with pytest.raises(ValueError, match="prohibited key"):
        _safe({"api_key": "not-a-real-secret"})


def test_logged_artifact_reference_does_not_append_the_version_twice() -> None:
    logged = SimpleNamespace(
        name="evidence-guide-v2-preregistration:v0",
        version="v0",
        qualified_name="wandb/gi-scenario-studio-agent-evals/evidence-guide-v2-preregistration:v0",
    )
    reference = _logged_artifact_reference(
        logged,
        entity="wandb",
        project="gi-scenario-studio-agent-evals",
    )
    assert reference == logged.qualified_name
    assert ":v0:v0" not in reference


def test_group_specs_preserves_the_preregistered_randomized_configuration_order() -> None:
    first_b = replace(_spec(), attempt_id="b" * 32, model_id="model-b")
    first_a = replace(_spec(), attempt_id="c" * 32, model_id="model-a")
    second_b = replace(_spec(), attempt_id="d" * 32, model_id="model-b", case_id="case-2")

    groups = group_specs([first_b, first_a, second_b], time_block="block-1")

    assert [group["model_id"] for group, _ in groups] == ["model-b", "model-a"]
    assert [item.attempt_id for item in groups[0][1]] == [first_b.attempt_id, second_b.attempt_id]


def test_start_block_reuses_client_and_finish_acknowledges_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_dataset = SimpleNamespace(rows=[])
    run = _Run()
    weave_client = _WeaveClient(not_found_attempts=1)
    monkeypatch.setattr(
        "gi_scenario_core.evidence_guide_v2_cloud.time.sleep", lambda _seconds: None
    )
    fake_wandb = SimpleNamespace(
        init=lambda **_: run,
        Settings=lambda **_: object(),
        Table=_Table,
    )
    fake_weave = SimpleNamespace(
        init=lambda *_args, **_kwargs: None,
        ref=lambda _uri: _Ref(published_dataset),
        Evaluation=lambda **kwargs: SimpleNamespace(**kwargs),
        publish=lambda value, **_: _Ref(value),
        EvaluationLogger=_Logger,
    )
    monkeypatch.setitem(sys.modules, "wandb", fake_wandb)
    monkeypatch.setitem(sys.modules, "weave", fake_weave)
    publisher = SimpleNamespace(
        entity="entity",
        project="gi-scenario-studio-agent-evals",
        campaign_instance_sha256="a" * 64,
        lockbox_project=None,
        preregistration_reference="entity/project/prereg:v1",
        dataset_reference=lambda *_args, **_kwargs: "weave:///entity/project/dataset:v1",
        ensure_weave_client=lambda _project: weave_client,
        credentials=object(),
        lockbox_credentials=None,
        binding=object(),
        lockbox_binding=None,
        cloud_environment=lambda **_: nullcontext(),
    )
    group = {
        "stage": "architecture",
        "split": "development",
        "model_id": "ibm-granite/granite-4.1-8b",
        "model_slug": "granite",
        "architecture_id": "A1",
        "prompt_id": "checklist",
        "max_output_tokens": 256,
        "repeat": 0,
        "time_block": "block-1",
        "hidden": False,
    }
    observer = WandbBlockObserver(publisher=publisher, group=group, hidden=False).start()
    observer._input_by_attempt[_spec().attempt_id] = {"question": "bounded"}
    observer.log_outcome(_spec(), _outcome())
    receipt = observer.finish()
    assert observer._weave.preinitialized_client is weave_client
    assert observer._logger.dataset is published_dataset
    assert observer.rows[0]["input_tokens"] == 321
    assert observer.rows[0]["output_tokens"] == 17
    assert observer._logger.outputs[0]["max_output_tokens"] == 256
    assert receipt["weave_evaluation_run_url"] == observer._logger.ui_url
    assert weave_client.set_context_count == 1
    assert weave_client.clear_context_count == 1
    assert weave_client.context is None
    assert weave_client.get_call_count == 2
    assert run.finish_count == 1


def test_campaign_weave_client_initializes_once_per_process_and_reuses_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current: dict[str, Any] = {"client": None}
    init_calls: list[str] = []

    def init(project: str, **_kwargs: Any) -> _WeaveClient:
        init_calls.append(project)
        client = _WeaveClient()
        current["client"] = client
        return client

    fake_weave = SimpleNamespace(
        init=init,
        get_client=lambda: current["client"],
    )
    monkeypatch.setitem(sys.modules, "weave", fake_weave)

    def publisher() -> WandbCampaignPublisher:
        value = object.__new__(WandbCampaignPublisher)
        value.entity = "entity"
        value._weave_clients = {}
        value._weave_client_lock = threading.RLock()
        return value

    first_publisher = publisher()
    first = first_publisher.ensure_weave_client("gi-scenario-studio-agent-evals")
    assert first_publisher.ensure_weave_client("gi-scenario-studio-agent-evals") is first

    # A second publisher in the same process adopts the verified current client
    # instead of calling weave.init again.
    second_publisher = publisher()
    assert second_publisher.ensure_weave_client("gi-scenario-studio-agent-evals") is first
    assert init_calls == ["entity/gi-scenario-studio-agent-evals"]


def test_two_block_observers_never_hold_or_cross_attribute_client_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_dataset = SimpleNamespace(rows=[])
    weave_client = _WeaveClient()
    runs: list[_Run] = []

    def wandb_init(**kwargs: Any) -> _Run:
        run = _Run(str(kwargs["id"]))
        runs.append(run)
        return run

    fake_wandb = SimpleNamespace(
        init=wandb_init,
        Settings=lambda **_: object(),
        Table=_Table,
    )
    fake_weave = SimpleNamespace(
        ref=lambda _uri: _Ref(published_dataset),
        Evaluation=lambda **kwargs: SimpleNamespace(**kwargs),
        publish=lambda value, **_: _Ref(value),
        EvaluationLogger=_Logger,
    )
    monkeypatch.setitem(sys.modules, "wandb", fake_wandb)
    monkeypatch.setitem(sys.modules, "weave", fake_weave)
    publisher = SimpleNamespace(
        entity="entity",
        project="gi-scenario-studio-agent-evals",
        campaign_instance_sha256="a" * 64,
        lockbox_project=None,
        preregistration_reference="entity/project/prereg:v1",
        dataset_reference=lambda *_args, **_kwargs: "weave:///entity/project/dataset:v1",
        ensure_weave_client=lambda _project: weave_client,
        credentials=object(),
        lockbox_credentials=None,
        binding=object(),
        lockbox_binding=None,
        cloud_environment=lambda **_: nullcontext(),
    )
    base_group = {
        "stage": "architecture",
        "split": "development",
        "model_id": "ibm-granite/granite-4.1-8b",
        "model_slug": "granite",
        "architecture_id": "A1",
        "prompt_id": "checklist",
        "max_output_tokens": 256,
        "time_block": "block-1",
        "hidden": False,
    }
    first = WandbBlockObserver(
        publisher=publisher,
        group={**base_group, "repeat": 0},
        hidden=False,
    ).start()
    second = WandbBlockObserver(
        publisher=publisher,
        group={**base_group, "repeat": 1},
        hidden=False,
    ).start()

    assert weave_client.context is None
    assert weave_client.set_context_count == 2
    assert weave_client.clear_context_count == 2

    captured: list[dict[str, Any]] = []

    def capture_agent_turn(_binding: Any, **kwargs: Any):
        captured.append(kwargs["attributes"])
        return nullcontext(SimpleNamespace())

    first._weave.agent_turn = capture_agent_turn
    second._weave.agent_turn = capture_agent_turn
    first_spec = _spec()
    second_spec = _spec(attempt_id="b" * 32, repeat=1)
    with first.attempt(first_spec, {"question": "first"}):
        pass
    with second.attempt(second_spec, {"question": "second"}):
        pass

    assert [item["wandb_run_id"] for item in captured] == [runs[0].id, runs[1].id]
    assert [item["wandb_run_url"] for item in captured] == [runs[0].url, runs[1].url]
    assert weave_client.context is None

    first.finish()
    second.finish()
    assert weave_client.clear_context_count == 2


def test_finish_timeout_preserves_primary_error_and_always_cleans_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _WeaveClient(outstanding_jobs=1)
    run = _Run()
    observer = object.__new__(WandbBlockObserver)
    observer._run = run
    observer._logger = _Logger()
    observer._weave_client = client
    observer._wandb_context_set = True
    observer._run_finished = False
    observer.rows = []
    observer.hidden = False
    observer.group = {"stage": "capability"}
    observer._evaluation_uri = "weave:///entity/project/evaluation:v1"
    observer._evaluation_run_url = observer._logger.ui_url
    client.context = "run-1"

    times = iter((0.0, 61.0))
    monkeypatch.setattr(
        "gi_scenario_core.evidence_guide_v2_cloud.time.monotonic",
        lambda: next(times),
    )
    fake_wandb = SimpleNamespace(Table=_Table)
    monkeypatch.setitem(sys.modules, "wandb", fake_wandb)

    with pytest.raises(RuntimeError, match="WEAVE_EVALUATION_FLUSH_TIMEOUT"):
        observer.finish()

    assert client.context is None
    assert client.clear_context_count == 1
    assert run.finish_count == 1
    assert client.get_call_count == 0


def test_finish_fails_closed_when_evaluation_readback_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _WeaveClient(acknowledge=False)
    run = _Run()
    observer = object.__new__(WandbBlockObserver)
    observer._run = run
    observer._logger = _Logger()
    observer._weave_client = client
    observer._wandb_context_set = False
    observer._run_finished = False
    observer.rows = []
    observer.hidden = False
    observer.group = {"stage": "capability"}
    observer._evaluation_uri = "weave:///entity/project/evaluation:v1"
    observer._evaluation_run_url = observer._logger.ui_url
    monkeypatch.setitem(sys.modules, "wandb", SimpleNamespace(Table=_Table))
    times = iter((0.0, 1.0, 31.0))
    monkeypatch.setattr(
        "gi_scenario_core.evidence_guide_v2_cloud.time.monotonic",
        lambda: next(times),
    )

    with pytest.raises(RuntimeError, match="WEAVE_EVALUATION_RUN_READBACK_FAILED"):
        observer.finish()

    assert client.get_call_count == 1
    assert run.finish_count == 1


def test_finish_preserves_primary_error_when_both_cleanup_steps_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _WeaveClient(outstanding_jobs=1)
    run = _Run()
    cleanup_calls: list[str] = []

    def fail_clear() -> None:
        cleanup_calls.append("clear")
        raise RuntimeError("synthetic clear failure")

    def fail_finish() -> None:
        cleanup_calls.append("finish")
        raise RuntimeError("synthetic finish failure")

    client.clear_wandb_run_context = fail_clear  # type: ignore[method-assign]
    run.finish = fail_finish  # type: ignore[method-assign]
    observer = object.__new__(WandbBlockObserver)
    observer._run = run
    observer._logger = _Logger()
    observer._weave_client = client
    observer._wandb_context_set = True
    observer._run_finished = False
    observer.rows = []
    observer.hidden = False
    observer.group = {"stage": "capability"}
    observer._evaluation_uri = "weave:///entity/project/evaluation:v1"
    observer._evaluation_run_url = observer._logger.ui_url

    times = iter((0.0, 61.0))
    monkeypatch.setattr(
        "gi_scenario_core.evidence_guide_v2_cloud.time.monotonic",
        lambda: next(times),
    )
    monkeypatch.setitem(sys.modules, "wandb", SimpleNamespace(Table=_Table))

    with pytest.raises(RuntimeError, match="WEAVE_EVALUATION_FLUSH_TIMEOUT") as failure:
        observer.finish()

    assert cleanup_calls == ["clear", "finish"]
    assert any("cleanup failure preserved" in note for note in failure.value.__notes__)


def test_quiet_settings_bound_each_weave_http_attempt(tmp_path) -> None:
    settings = _quiet_settings(tmp_path)
    assert settings["WEAVE_HTTP_TIMEOUT"] == "10"
    assert settings["WEAVE_RETRY_MAX_ATTEMPTS"] == "2"
