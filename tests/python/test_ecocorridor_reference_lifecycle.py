from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from ecocorridor_core.rpc_response_contracts import (
    PendingRecoveryAnalysisData,
    ProjectAnalysisStatusData,
)
from ecocorridor_core.rpc_worker import ProjectAnalysisParams, RPCDispatcher, RPCFault
from pydantic import ValidationError

ENGINE_SHA256 = "e" * 64
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def test_project_state_contract_keeps_reference_identity_separate_from_alternatives() -> None:
    reference = ProjectAnalysisStatusData(
        analysis_id="reference-analysis",
        analysis_kind="REFERENCE",
        plan_id="reference-plan",
        revision_id="revision-reviewed",
        engine_manifest_sha256=ENGINE_SHA256,
        state="RUNNING",
        created_at=NOW,
        updated_at=NOW,
    )
    alternatives = ProjectAnalysisStatusData(
        analysis_id="alternatives-analysis",
        analysis_kind="ALTERNATIVES",
        plan_id=None,
        revision_id="revision-reviewed",
        engine_manifest_sha256=ENGINE_SHA256,
        state="COMPLETED",
        created_at=NOW,
        updated_at=NOW,
    )
    recovery = PendingRecoveryAnalysisData(
        analysis_id=reference.analysis_id,
        analysis_kind="REFERENCE",
        plan_id=reference.plan_id,
        revision_id=reference.revision_id,
        analysis_state="RUNNING",
        executor_active=False,
        recovery_required=True,
        running_job_ids=["reference-job-1"],
    )

    assert reference.plan_id == recovery.plan_id
    assert alternatives.plan_id is None
    with pytest.raises(ValidationError, match="only reference analyses"):
        ProjectAnalysisStatusData.model_validate(
            {**reference.model_dump(mode="json"), "analysis_kind": "ALTERNATIVES"}
        )
    with pytest.raises(ValidationError, match="only recovering reference analyses"):
        PendingRecoveryAnalysisData.model_validate(
            {**recovery.model_dump(mode="json"), "plan_id": None}
        )


@pytest.mark.parametrize("operation", ["pause", "resume", "cancel"])
def test_generic_lifecycle_commands_verify_reference_plan_before_engine_access(
    tmp_path: Path,
    operation: str,
) -> None:
    class MissingPlanStore:
        root = tmp_path

        @staticmethod
        def analysis(_analysis_id: str) -> dict[str, str]:
            return {"analysis_kind": "REFERENCE", "state": "RUNNING"}

        @staticmethod
        def reference_comparison_plan_for_analysis(_analysis_id: str):
            return None

        @staticmethod
        def close() -> None:
            return None

    dispatcher = RPCDispatcher.__new__(RPCDispatcher)
    store = MissingPlanStore()
    dispatcher._store = MethodType(lambda _self, _directory: store, dispatcher)
    dispatcher._require_engine = MethodType(
        lambda _self: (_ for _ in ()).throw(AssertionError("engine accessed before plan check")),
        dispatcher,
    )
    params = ProjectAnalysisParams(
        project_directory=str(tmp_path),
        analysis_id="reference-analysis",
    )

    method = getattr(dispatcher, f"_analysis_{operation}")
    with pytest.raises(RPCFault) as rejected:
        method(params)

    assert rejected.value.error.code == "REFERENCE_CONTROL_PLAN_MISSING"


def test_reference_resume_rechecks_plan_and_pinned_engine_before_executor(
    tmp_path: Path,
) -> None:
    plan = SimpleNamespace(run_plan=SimpleNamespace(engine_manifest_sha256=ENGINE_SHA256))

    class ValidPlanStore:
        root = tmp_path

        def __init__(self) -> None:
            self.state = "PAUSED"
            self.plan_checks = 0

        @staticmethod
        def analysis(_analysis_id: str) -> dict[str, str]:
            return {"analysis_kind": "REFERENCE", "state": store.state}

        @staticmethod
        def reference_comparison_plan_for_analysis(_analysis_id: str):
            return plan, {"plan_sha256": "a" * 64}

        def store_reference_comparison_plan(self, candidate) -> None:
            assert candidate is plan
            self.plan_checks += 1

        def transition_analysis(self, _analysis_id: str, state) -> None:
            self.state = state.value

        @staticmethod
        def close() -> None:
            return None

    class Executor:
        def __init__(self) -> None:
            self.resumed = False

        def resume(self, *, project_directory: Path, analysis_id: str) -> None:
            assert project_directory == tmp_path
            assert analysis_id == "reference-analysis"
            self.resumed = True

    store = ValidPlanStore()
    executor = Executor()
    dispatcher = RPCDispatcher.__new__(RPCDispatcher)
    dispatcher._store = MethodType(lambda _self, _directory: store, dispatcher)
    dispatcher._require_engine = MethodType(
        lambda _self: (tmp_path / "engine", ENGINE_SHA256, executor), dispatcher
    )
    dispatcher._require_plan_engine_manifest = MethodType(
        lambda _self, run_plan: (
            None
            if run_plan.engine_manifest_sha256 == ENGINE_SHA256
            else (_ for _ in ()).throw(AssertionError("wrong engine"))
        ),
        dispatcher,
    )
    dispatcher._result = MethodType(
        lambda _self, _method, data: SimpleNamespace(data=SimpleNamespace(**data)),
        dispatcher,
    )

    response = dispatcher._analysis_resume(
        ProjectAnalysisParams(
            project_directory=str(tmp_path),
            analysis_id="reference-analysis",
        )
    )

    assert response.data.state == "RUNNING"
    assert store.plan_checks == 1
    assert executor.resumed is True
