from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from gi_scenario_core.contracts import (
    PUBLIC_CONTRACTS,
    SCHEMA_VERSION,
    AlternativeDecisionSummary,
    CloudArtifactManifest,
    DecisionBrief,
    DecisionCriterion,
    EvidenceClaim,
    FitnessDisposition,
    ModelFitnessCriterion,
    ModelFitnessPassport,
    ModelRevision,
    ObservabilityBinding,
    ProjectDataPolicy,
    ProjectKind,
    TelemetryEnvelope,
    WeaveTraceReference,
)
from gi_scenario_core.observability import CredentialSession, WandbUploadReceipt
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.rpc_worker import RPCDispatcher, RPCFault

SOLVER_SHA256 = "b" * 64
MODEL_SHA256 = "a" * 64
MODEL_REVISION_ID = "revision_1"
EXTRACTOR_VERSION = "test-report-extractor-v2"


class FakeWandb:
    def __init__(self, *, fail_upload: bool = False) -> None:
        self.fail_upload = fail_upload
        self.validation_calls = 0
        self.upload_calls: list[tuple[TelemetryEnvelope, dict[str, Path], Path]] = []

    def validate_binding(
        self,
        binding: ObservabilityBinding,
        policy: ProjectDataPolicy,
    ) -> ObservabilityBinding:
        self.validation_calls += 1
        assert binding.access == "Restricted"
        assert binding.deployment == policy.deployment
        payload = binding.model_dump(mode="json")
        payload.update(
            {
                "external_project_id": "wandb-project-verified-id",
                "verified": True,
                "verified_at": datetime.now(UTC),
            }
        )
        return ObservabilityBinding.model_validate(payload)

    def upload(
        self,
        envelope: TelemetryEnvelope,
        binding: ObservabilityBinding,
        artifact_paths: dict[str, Path],
        workspace: Path,
    ) -> WandbUploadReceipt:
        self.upload_calls.append((envelope, artifact_paths, workspace))
        if self.fail_upload:
            raise RuntimeError("SDK failure with /Users/private-account and a token")
        assert binding.verified
        assert all(path.is_file() for path in artifact_paths.values())
        return WandbUploadReceipt(
            run_id="wandb-run-1",
            url="https://wandb.ai/example/engineering/runs/wandb-run-1",
            artifact_ids=[f"remote-{item.artifact_id}" for item in envelope.artifacts],
        )


class FakeWeave:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def trace_call(
        self,
        binding: ObservabilityBinding,
        *,
        call_id: str,
        operation: str,
        inputs: dict[str, object],
        output: dict[str, object],
    ) -> WeaveTraceReference:
        if self.fail:
            raise RuntimeError("weave unavailable")
        self.calls.append(
            {"call_id": call_id, "operation": operation, "inputs": inputs, "output": output}
        )
        return WeaveTraceReference(
            project_id=binding.project_id,
            call_id=call_id,
            entity=binding.entity,
            external_project=binding.external_project,
            trace_id=f"trace-{call_id}",
            provider_call_id=f"provider-{call_id}",
            url=f"https://wandb.ai/{binding.entity}/{binding.external_project}/weave/calls/{call_id}",
        )


def _seed_project(tmp_path) -> tuple[Path, str, str, str]:
    root = tmp_path / "project"
    repository = ProjectRepository(root)
    repository.create_project(
        title="Drainage alternatives project",
        project_kind=ProjectKind.CONCEPTUAL,
        project_id="project_1",
    )
    analysis = repository.create_analysis(
        "project_1",
        analysis_id="analysis_1",
        payload={
            "contract": "exact-swmm-analysis-v2",
            "model_revision_id": MODEL_REVISION_ID,
            "model_sha256": MODEL_SHA256,
            "metric_extractor_version": EXTRACTOR_VERSION,
        },
    )
    job_id = "job_exact_1"
    input_bytes = b"[TITLE]\n;; portable exact input\n"
    report_bytes = b"Flow Routing Continuity\nContinuity Error (%) = 0.1\n"
    output_bytes = b"exact binary output"
    repository.create_solver_job(
        analysis_id=analysis["analysis_id"],
        design_id="design_1",
        storm_id="storm_1",
        physics_cache_key=hashlib.sha256(job_id.encode()).hexdigest(),
        payload={
            "solver_sha256": SOLVER_SHA256,
            "qa_status": "accepted",
            "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
            "model_revision_id": MODEL_REVISION_ID,
            "model_revision_sha256": MODEL_SHA256,
            "metric_extractor_version": EXTRACTOR_VERSION,
        },
        job_id=job_id,
    )
    repository.transition_solver_job(job_id, "QUEUED")
    repository.transition_solver_job(job_id, "RUNNING")
    repository.transition_solver_job(job_id, "SUCCEEDED")
    repository.add_run_artifact(job_id, "input", input_bytes)
    repository.add_run_artifact(job_id, "report", report_bytes)
    repository.add_run_artifact(job_id, "output", output_bytes)
    repository.add_run_metric(job_id, "flood_volume", 12.5, "m3")
    repository.add_run_metric(job_id, "peak_hgl", 101.25, "m", object_id="J1")
    repository.close()
    return root, "project_1", analysis["analysis_id"], job_id


def _policy(project_id: str = "project_1") -> dict[str, object]:
    return {
        "policy_id": "policy_1",
        "project_id": project_id,
        "classification": "standard_engineering",
        "deployment": "wandb_cloud",
        "permitted_categories": [
            "model",
            "gis",
            "storm",
            "solver_input",
            "solver_report",
            "solver_output",
            "result",
            "qa",
            "search_trace",
            "decision_brief",
            "export",
            "benchmark",
            "assistant_trace",
        ],
        "acknowledged_by": "Engineer A",
    }


def _binding(project_id: str = "project_1") -> dict[str, object]:
    return {
        "binding_id": "binding_1",
        "project_id": project_id,
        "data_policy_id": "policy_1",
        "entity": "example-team",
        "external_project": "project-one-evidence",
        "external_project_id": "pending",
        "access": "Restricted",
        "deployment": "wandb_cloud",
        "mode": "online",
    }


def _dispatcher(
    tmp_path,
    wandb: FakeWandb | None = None,
    weave: FakeWeave | None = None,
) -> RPCDispatcher:
    return RPCDispatcher(
        data_root=tmp_path,
        allowed_roots=[tmp_path],
        observability_adapters={"wandb": wandb or FakeWandb(), "weave": weave or FakeWeave()},
    )


def _account_connection_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "scope": "account",
        "api_key": "a" * 32,
        "credential_id": "wandb-wandb_cloud-api.wandb.ai",
        "deployment": "wandb_cloud",
        "api_base_url": "https://api.wandb.ai",
        "web_base_url": "https://wandb.ai",
        "entity": "example-team",
        "project": "restricted-engineering",
        "weave_project": "restricted-engineering",
        "project_visibility": "Restricted",
        "data_policy_acknowledged": True,
        "acknowledged_by": "Engineer A",
        "classification": "standard_engineering",
    }
    params.update(overrides)
    return params


def _connect(dispatcher: RPCDispatcher, root: Path) -> dict[str, object]:
    connected = dispatcher.dispatch(
        "observability.connect",
        {
            "scope": "project",
            "project_directory": str(root),
            "policy": _policy(),
            "binding": _binding(),
        },
    )
    repository = ProjectRepository(root)
    try:
        binding = repository.get_observability_binding("project_1")
        repository.store_model_revision(
            ModelRevision(
                revision_id=MODEL_REVISION_ID,
                project_id="project_1",
                reason="import",
                model_sha256=MODEL_SHA256,
                observability_binding_id=binding["binding_id"],
                created_by="test fixture",
            )
        )
    finally:
        repository.close()
    return connected


def _envelope(root: Path, *, envelope_id: str = "envelope_1") -> dict[str, object]:
    repository = ProjectRepository(root)
    try:
        content = b"[TITLE]\n;; portable cloud model evidence\n"
        digest, _ = repository.store_blob(content)
        binding = repository.get_observability_binding("project_1")
    finally:
        repository.close()
    artifact = CloudArtifactManifest(
        artifact_id=f"artifact_{envelope_id}",
        project_id="project_1",
        category="model",
        logical_name="baseline.inp",
        sha256=digest,
        bytes=len(content),
        media_type="text/plain",
        source_object_id="model_revision_1",
        local_blob_sha256=digest,
    )
    return TelemetryEnvelope(
        envelope_id=envelope_id,
        project_id="project_1",
        analysis_id="analysis_1",
        binding_id=binding["binding_id"],
        category="engineering_evidence",
        event_type="model_revision_materialized",
        subject_id="model_revision_1",
        idempotency_key=f"sync-{envelope_id}",
        metadata={"project_id": "project_1", "model_revision_id": "model_revision_1"},
        artifacts=[artifact],
    ).model_dump(mode="json")


def test_v2_contract_surface_and_fitness_gate_are_independent() -> None:
    assert SCHEMA_VERSION == "2.0"
    required = {
        "ModelFitnessPassport",
        "ExportEligibility",
        "DecisionBrief",
        "FeasibilityRecord",
        "DecisionCriterion",
        "AlternativeDecisionSummary",
        "EvidenceClaim",
        "AnalysisRunPlan",
        "ProjectDataPolicy",
        "ObservabilityBinding",
        "CloudSyncState",
        "TelemetryEnvelope",
        "TelemetryOutboxItem",
        "CloudArtifactManifest",
        "WandbRunReference",
        "WeaveTraceReference",
        "BenchmarkRunEnvelope",
        "AssistantAnswer",
        "AssistantEvaluationRecord",
        "GuidedCheckpoint",
        "ModelRevision",
        "SemanticModelDiff",
    }
    assert required.issubset(PUBLIC_CONTRACTS)
    assert "ModelReadiness" not in PUBLIC_CONTRACTS
    assert "ReadinessAttestation" not in PUBLIC_CONTRACTS

    claim = EvidenceClaim(
        claim_id="claim_1",
        project_id="project_1",
        claim_type="fitness",
        statement="Topology review complete.",
        evidence_ids=["validation:topology"],
    )
    criterion = ModelFitnessCriterion(
        criterion_id="topology",
        label="Topology",
        status="pass",
        finding="Reviewed.",
        evidence_claim_ids=[claim.claim_id],
    )
    passport = ModelFitnessPassport(
        project_id="project_1",
        model_revision_id="revision_1",
        intended_use="GI alternatives screening",
        disposition=FitnessDisposition.FIT_FOR_STATED_USE,
        criteria=[criterion],
        evidence_claims=[claim],
        reviewer="Engineer A",
        reviewer_attested=True,
    )
    dumped = passport.model_dump(mode="json")
    assert "readiness" not in dumped
    assert dumped["disposition"] == "fit_for_stated_use"


def test_critical_infrastructure_requires_dedicated_or_self_managed() -> None:
    with pytest.raises(ValueError, match="Dedicated or Self-Managed"):
        ProjectDataPolicy(
            project_id="critical_1",
            classification="critical_infrastructure",
            deployment="wandb_cloud",
            acknowledged_by="Engineer A",
        )


def test_candidate_credential_rolls_back_without_destroying_prior_session() -> None:
    session = CredentialSession()
    session.configure("a" * 32, "https://api.wandb.ai")

    with (
        pytest.raises(RuntimeError, match="rejected"),
        session.candidate("b" * 32, "https://dedicated.example.com"),
    ):
        raise RuntimeError("rejected")

    with session.environment():
        assert session.configured
        assert os.environ["WANDB_API_KEY"] == "a" * 32
        assert os.environ["WANDB_BASE_URL"] == "https://api.wandb.ai"
    session.clear()


def test_account_policy_block_is_visible_without_retaining_candidate_secret(tmp_path) -> None:
    dispatcher = _dispatcher(tmp_path)
    blocked = dispatcher.dispatch(
        "observability.connect",
        _account_connection_params(
            classification="critical_infrastructure",
            deployment="wandb_cloud",
        ),
    )

    assert blocked["state"] == "BLOCKED_PROJECT_POLICY"
    assert "Dedicated or Self-Managed" in blocked["message"]
    assert dispatcher.observability.credentials._api_key is None
    assert dispatcher.dispatch("observability.get_status", {"scope": "account"}) == blocked


def test_account_disconnect_clears_worker_session_and_credentials(tmp_path) -> None:
    dispatcher = _dispatcher(tmp_path)
    connected = dispatcher.dispatch("observability.connect", _account_connection_params())
    assert connected["state"] == "CONNECTED"
    assert dispatcher.observability.credentials._api_key is not None

    disconnected = dispatcher.dispatch("observability.disconnect", {"scope": "account"})

    assert disconnected["state"] == "LOCAL_ONLY"
    assert disconnected["mode"] == "local"
    assert disconnected["configured"] is True
    assert disconnected["provider"] == "none"
    assert disconnected["disclosureAccepted"] is False
    assert dispatcher.observability.credentials._api_key is None
    assert dispatcher._observability_account is None


def test_open_local_project_binds_and_local_mode_authoritatively_stops_sync(tmp_path) -> None:
    root, project_id, _, _ = _seed_project(tmp_path)
    dispatcher = _dispatcher(tmp_path)

    account = dispatcher.dispatch("observability.connect", _account_connection_params())
    assert account["state"] == "CONNECTED"
    connected = dispatcher.dispatch(
        "observability.bind_project",
        {"project_directory": str(root), "project_id": project_id},
    )
    assert connected["binding"]["verified"] is True
    assert connected["can_sync"] is True
    assert connected["can_use_assistant"] is True

    repository = ProjectRepository(root)
    try:
        binding_id = repository.get_observability_binding(project_id)["binding_id"]
        queued = repository.enqueue_telemetry_envelope(_envelope(root))
        assert queued["state"] == "pending"
        first_upload_at = repository.record_first_cloud_upload(project_id)["first_upload_at"]
    finally:
        repository.close()

    local = dispatcher.dispatch(
        "observability.disconnect",
        {
            "scope": "project",
            "project_directory": str(root),
            "project_id": project_id,
        },
    )

    assert local["state"] == "LOCAL_ONLY"
    assert local["binding"] is None
    assert local["can_sync"] is False
    assert local["can_use_assistant"] is False
    assert (
        dispatcher.dispatch("observability.get_status", {"scope": "account"})["state"]
        == "LOCAL_ONLY"
    )
    assert dispatcher.observability.credentials._api_key is None
    repository = ProjectRepository(root)
    try:
        with pytest.raises(KeyError, match="active observability binding"):
            repository.get_observability_binding(project_id)
        history = repository.list_observability_bindings(project_id)
        assert len(history) == 1
        assert history[0]["binding_id"] == binding_id
        assert history[0]["active"] is False
        blocked = repository.get_telemetry_outbox_item(queued["outbox_id"])
        assert blocked["state"] == "blocked"
        assert blocked["last_error_code"] == "LOCAL_MODE_ENABLED"
    finally:
        repository.close()

    with pytest.raises(RPCFault, match="reconnect this project"):
        dispatcher.dispatch(
            "observability.retry",
            {
                "project_directory": str(root),
                "project_id": project_id,
                "outbox_id": queued["outbox_id"],
            },
        )

    dispatcher.dispatch("observability.connect", _account_connection_params())
    reconnected = dispatcher.dispatch(
        "observability.bind_project",
        {"project_directory": str(root), "project_id": project_id},
    )
    assert reconnected["binding"]["binding_id"] == binding_id
    assert datetime.fromisoformat(
        reconnected["binding"]["first_upload_at"]
    ) == datetime.fromisoformat(first_upload_at)
    repository = ProjectRepository(root)
    try:
        assert len(repository.list_observability_bindings(project_id)) == 1
        assert repository.get_telemetry_outbox_item(queued["outbox_id"])["state"] == ("pending")
    finally:
        repository.close()
    dispatcher._executor.shutdown(wait=True)


def test_repository_bootstraps_the_complete_v3_local_and_cloud_schema(tmp_path) -> None:
    root = tmp_path / "fresh-v2"
    repository = ProjectRepository(root)
    try:
        assert repository._connection.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {
            row[0]
            for row in repository._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "project_data_policies",
            "observability_bindings",
            "telemetry_outbox",
            "cloud_artifacts",
            "wandb_run_references",
            "weave_trace_references",
            "model_revisions",
            "model_fitness_passports",
            "decision_briefs",
            "guided_checkpoints",
        }.issubset(tables)
        revision_columns = {
            row["name"]: row
            for row in repository._connection.execute(
                "PRAGMA table_info(model_revisions)"
            ).fetchall()
        }
        assert revision_columns["observability_binding_id"]["notnull"] == 0
        assert repository._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        repository.close()


def test_local_work_is_available_and_verified_restricted_binding_unlocks_sync(tmp_path) -> None:
    root, project_id, _, _ = _seed_project(tmp_path)
    wandb = FakeWandb()
    dispatcher = _dispatcher(tmp_path, wandb)
    before = dispatcher.dispatch(
        "observability.get_status",
        {"scope": "project", "project_directory": str(root), "project_id": project_id},
    )
    assert before["state"] == "LOCAL_ONLY"
    assert before["binding_required"] is False
    assert before["binding"] is None
    assert before["can_enter_home"] is True
    assert before["can_open_project"] is True
    assert before["can_run_local"] is True
    assert before["can_sync"] is False
    assert before["can_use_assistant"] is False
    guided = dispatcher.dispatch(
        "guided.open",
        {"project_directory": str(root), "project_id": project_id},
    )
    assert guided["current_step_id"] == "overview"

    dispatcher.observability.credentials.configure("a" * 32, "https://api.wandb.ai")
    connected = _connect(dispatcher, root)
    assert connected["can_enter_home"] is True
    assert connected["binding_required"] is False
    assert connected["can_sync"] is True
    assert connected["binding"]["access"] == "Restricted"
    assert connected["binding"]["verified"] is True
    assert wandb.validation_calls == 1
    assert "api_key" not in str(connected).casefold()


def test_full_evidence_preview_sync_and_idempotency(tmp_path) -> None:
    root, _, _, _ = _seed_project(tmp_path)
    wandb = FakeWandb()
    dispatcher = _dispatcher(tmp_path, wandb)
    _connect(dispatcher, root)
    envelope = _envelope(root)
    preview = dispatcher.dispatch(
        "observability.preview_upload",
        {"project_directory": str(root), "envelope": envelope},
    )
    assert preview["blockers"] == []
    assert preview["artifact_categories"] == ["model"]
    assert preview["artifact_count"] == 1
    assert preview["excluded_categories"] == [
        "credentials",
        "environment_variables",
        "local_absolute_paths",
        "raw_console_logs",
    ]

    synced = dispatcher.dispatch(
        "observability.sync",
        {
            "project_directory": str(root),
            "envelope": envelope,
            "preview_sha256": preview["payload_sha256"],
        },
    )
    assert synced["state"] == "synced"
    assert synced["wandb_run"]["run_id"] == "wandb-run-1"
    assert synced["local_authority"] is True
    assert len(wandb.upload_calls) == 1

    duplicate = dispatcher.dispatch(
        "observability.sync",
        {
            "project_directory": str(root),
            "envelope": envelope,
            "preview_sha256": preview["payload_sha256"],
        },
    )
    assert duplicate["state"] == "synced"
    assert len(wandb.upload_calls) == 1


def test_preview_blocks_secrets_paths_and_nonportable_text_artifacts(tmp_path) -> None:
    root, _, _, _ = _seed_project(tmp_path)
    dispatcher = _dispatcher(tmp_path)
    _connect(dispatcher, root)
    envelope = _envelope(root)
    envelope["metadata"] = {
        "environment_variables": {"WANDB_API_KEY": "never"},
        "source": "/Users/private-account/model.inp",
    }
    preview = dispatcher.dispatch(
        "observability.preview_upload",
        {"project_directory": str(root), "envelope": envelope},
    )
    assert preview["blockers"]
    assert any("prohibited field" in item for item in preview["blockers"])

    repository = ProjectRepository(root)
    try:
        content = b'[FILES]\nRAINFALL "/Users/private-account/rain.dat"\n'
        digest, _ = repository.store_blob(content)
        binding = repository.get_observability_binding("project_1")
    finally:
        repository.close()
    bad_artifact = CloudArtifactManifest(
        project_id="project_1",
        category="model",
        logical_name="bad.inp",
        sha256=digest,
        bytes=len(content),
        media_type="text/plain",
        source_object_id="revision_bad",
        local_blob_sha256=digest,
    )
    bad_envelope = TelemetryEnvelope(
        project_id="project_1",
        binding_id=binding["binding_id"],
        category="engineering_evidence",
        event_type="model_revision_materialized",
        subject_id="revision_bad",
        idempotency_key="bad-portable-path",
        artifacts=[bad_artifact],
    )
    bad_preview = dispatcher.dispatch(
        "observability.preview_upload",
        {
            "project_directory": str(root),
            "envelope": bad_envelope.model_dump(mode="json"),
        },
    )
    assert any("local absolute path" in item for item in bad_preview["blockers"])


def test_wandb_failure_only_changes_outbox_and_offline_queue_is_durable(tmp_path) -> None:
    root, _, analysis_id, job_id = _seed_project(tmp_path)
    wandb = FakeWandb(fail_upload=True)
    dispatcher = _dispatcher(tmp_path, wandb)
    _connect(dispatcher, root)
    envelope = _envelope(root)
    preview = dispatcher.dispatch(
        "observability.preview_upload",
        {"project_directory": str(root), "envelope": envelope},
    )
    failed = dispatcher.dispatch(
        "observability.sync",
        {
            "project_directory": str(root),
            "envelope": envelope,
            "preview_sha256": preview["payload_sha256"],
        },
    )
    assert failed["state"] == "failed"
    assert failed["last_error_code"] == "WANDB_UPLOAD_RUNTIMEERROR"
    assert "private-account" not in str(failed)

    # Engineering evidence is unchanged and still resolves locally.
    exact = dispatcher.dispatch(
        "evidence.resolve",
        {"project_directory": str(root), "job_id": job_id},
    )
    assert exact["analysis_id"] == analysis_id
    assert exact["metrics"]["flood_volume"] == 12.5

    repository = ProjectRepository(root)
    try:
        binding = repository.get_observability_binding("project_1")
        binding.update({"mode": "offline_queue", "updated_at": datetime.now(UTC)})
        repository.connect_observability_binding(binding)
    finally:
        repository.close()
    queued_envelope = _envelope(root, envelope_id="envelope_offline")
    queued_preview = dispatcher.dispatch(
        "observability.preview_upload",
        {"project_directory": str(root), "envelope": queued_envelope},
    )
    queued = dispatcher.dispatch(
        "observability.sync",
        {
            "project_directory": str(root),
            "envelope": queued_envelope,
            "preview_sha256": queued_preview["payload_sha256"],
        },
    )
    assert queued["state"] == "pending"


def test_binding_is_immutable_after_first_upload(tmp_path) -> None:
    root, _, _, _ = _seed_project(tmp_path)
    dispatcher = _dispatcher(tmp_path)
    _connect(dispatcher, root)
    envelope = _envelope(root)
    preview = dispatcher.dispatch(
        "observability.preview_upload",
        {"project_directory": str(root), "envelope": envelope},
    )
    dispatcher.dispatch(
        "observability.sync",
        {
            "project_directory": str(root),
            "envelope": envelope,
            "preview_sha256": preview["payload_sha256"],
        },
    )
    repository = ProjectRepository(root)
    try:
        current = repository.get_observability_binding("project_1")
        replacement = dict(current)
        replacement.update(
            {
                "binding_id": "binding_replacement",
                "external_project": "different-project",
                "external_project_id": "different-id",
            }
        )
        with pytest.raises(ValueError, match="immutable after first upload"):
            repository.connect_observability_binding(replacement)
    finally:
        repository.close()


def test_every_assistant_answer_requires_weave_and_exact_claim_validation(tmp_path) -> None:
    root, project_id, _, job_id = _seed_project(tmp_path)
    weave = FakeWeave()
    dispatcher = _dispatcher(tmp_path, weave=weave)
    _connect(dispatcher, root)
    response = dispatcher.dispatch(
        "assistant.explain",
        {
            "project_directory": str(root),
            "project_id": project_id,
            "intent": "exact_run",
            "job_id": job_id,
            "question": "What happened at the selected node?",
        },
    )
    assert response["evaluation"]["valid"] is True
    assert "12.5 m3" in response["answer"]["answer"]
    assert "101.25 m" in response["answer"]["answer"]
    assert response["answer"]["weave_trace"]["trace_id"].startswith("trace-")
    assert response["answer"]["read_only"] is True
    assert len(weave.calls) == 1

    failing = _dispatcher(tmp_path, weave=FakeWeave(fail=True))
    with pytest.raises(RPCFault, match="required Weave trace"):
        failing.dispatch(
            "assistant.explain",
            {
                "project_directory": str(root),
                "project_id": project_id,
                "intent": "exact_run",
                "job_id": job_id,
            },
        )


def test_guided_workflow_explains_why_how_and_completion(tmp_path) -> None:
    root, project_id, _, _ = _seed_project(tmp_path)
    dispatcher = _dispatcher(tmp_path)
    _connect(dispatcher, root)
    guided = dispatcher.dispatch(
        "guided.open",
        {"project_directory": str(root), "project_id": project_id},
    )
    assert guided["current_step_id"] == "overview"
    assert len(guided["checkpoints"]) == 8
    for checkpoint in guided["checkpoints"]:
        assert checkpoint["purpose"]
        assert checkpoint["how_to_use"]
        assert checkpoint["completion_signal"]
        assert checkpoint["primary_action_label"]


def test_decision_brief_requires_fitness_and_full_network_review() -> None:
    criterion = DecisionCriterion(
        label="Flood volume",
        category="hydraulic",
        units="m3",
        direction="minimize",
        rationale="Reduce flooding under selected storms.",
    )
    alternative = AlternativeDecisionSummary(
        design_id="design_1",
        label="Distributed GI",
        qa_status="accepted",
        exact_run_ids=["run_1"],
        evidence_claim_ids=["claim_1"],
    )
    with pytest.raises(ValueError, match="full-network"):
        DecisionBrief(
            project_id="project_1",
            analysis_id="analysis_1",
            model_revision_id="revision_1",
            title="Decision brief",
            status="review_ready",
            baseline_design_id="baseline",
            intended_decision="Select alternatives for field investigation",
            model_fitness_passport_id="passport_1",
            criteria=[criterion],
            alternatives=[alternative],
            evidence_claim_ids=["claim_1"],
        )


def test_required_v2_rpc_methods_are_dispatched(tmp_path) -> None:
    dispatcher = _dispatcher(tmp_path)
    required = {
        "observability.connect",
        "observability.disconnect",
        "observability.validate_binding",
        "observability.get_status",
        "observability.preview_upload",
        "observability.sync",
        "observability.retry",
        "observability.open_run",
        "observability.open_artifact",
        "observability.open_trace",
        "guided.open",
        "guided.get_state",
        "guided.rerun",
        "guided.pause",
        "guided.resume",
        "guided.cancel",
        "assistant.explain",
        "assistant.follow_up",
        "assistant.feedback",
        "assistant.get_trace",
        "decision_brief.get",
        "decision_brief.update",
        "model.attest",
        "model.get_fitness",
        "analysis.plan",
        "analysis.start",
        "analysis.pause",
        "analysis.resume",
        "analysis.cancel",
        "results.decision_board",
        "evidence.resolve",
        "export.validate",
        "export.create",
    }
    assert required.issubset(dispatcher.methods)
