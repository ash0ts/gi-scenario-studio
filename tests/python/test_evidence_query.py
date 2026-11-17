from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from gi_scenario_core.contracts import (
    AssistantAnswer,
    AssistantConversation,
    AssistantEvaluationRecord,
    AssistantEvidenceAnchor,
    AssistantTurn,
    ObservabilityBinding,
    ProjectDataPolicy,
    ProjectKind,
    ProviderPolicy,
    WeaveConversationReference,
)
from gi_scenario_core.evidence_guide import (
    RESPONSE_SCHEMA_SHA256,
    _claims_for_finite_plan,
    _execute_deterministic_tools,
    _provider_tool_result_projection,
)
from gi_scenario_core.evidence_query import PUBLIC_TOOL_NAMES, EvidenceQueryService
from gi_scenario_core.repository import ProjectRepository
from gi_scenario_core.rpc_worker import RPCDispatcher


@pytest.fixture
def guided_evidence(tmp_path):
    dispatcher = RPCDispatcher(data_root=tmp_path)
    project = dispatcher.dispatch("project.create", {"kind": "demo"})
    repository = ProjectRepository(project["projectDirectory"])
    try:
        yield project, repository, EvidenceQueryService(repository)
    finally:
        repository.close()
        dispatcher._executor.shutdown(wait=True)


def _packet(service: EvidenceQueryService, project_id: str, alternative_id: str):
    return service.build_packet(
        project_id,
        None,
        None,
        AssistantEvidenceAnchor(kind="alternative", identifier=alternative_id),
    )


def test_guided_alt_017_packet_and_tools_are_exact_and_deterministic(guided_evidence):
    project, _, service = guided_evidence
    packet = _packet(service, project["id"], "ALT-017")
    repeated = _packet(service, project["id"], "ALT-017")

    assert packet.model_dump(mode="json") == repeated.model_dump(mode="json")
    assert packet.capability_names == list(PUBLIC_TOOL_NAMES)
    assert {claim.authority for claim in packet.claims}.issuperset(
        {"exact_swmm", "derived_exact", "sourced_input", "decision_rule"}
    )
    assert all(capability["read_only"] for capability in service.capabilities(packet))

    comparison_response = service.execute("results.compare_exact", {}, packet)
    comparison = comparison_response["result"]
    packet_claims = {claim.claim_id: claim for claim in packet.claims}
    assert comparison_response["claim_receipts"]
    assert all(
        receipt["claim_id"] in packet_claims and len(receipt["claim_sha256"]) == 64
        for receipt in comparison_response["claim_receipts"]
    )
    limitation_ids = {claim.claim_id for claim in packet.claims if claim.claim_type == "limitation"}
    limitation_response = service.execute("project.list_limitations", {}, packet)
    assert {item["claim_id"] for item in limitation_response["claim_receipts"]} == (limitation_ids)
    assert not limitation_ids.intersection(
        item["claim_id"] for item in comparison_response["claim_receipts"]
    )
    assert comparison["outfall_volume_reduction_percent"] == 4.14
    changes = {item["storm_id"]: item for item in comparison["storms"]}
    assert changes["synthetic_long_2_5in"]["baseline_outfall_peak_cfs"] == 3.14
    assert changes["synthetic_long_2_5in"]["alternative_outfall_peak_cfs"] == 3.21
    assert changes["synthetic_long_2_5in"]["outfall_peak_delta_cfs"] == 0.07
    assert changes["synthetic_stress_4in"]["outfall_peak_delta_cfs"] == 0.01

    matrix = service.execute("decision.get_rule_matrix", {}, packet)["result"]
    checks = {item["rule_id"]: item for item in matrix["checks"]}
    assert matrix["meets_all_rules"] is False
    assert checks["aggregate_volume_reduction"]["passes"] is False
    assert checks["no_outfall_peak_increase"]["passes"] is False


def test_guided_alt_207_runoff_ledger_preserves_scope_language(guided_evidence):
    project, _, service = guided_evidence
    packet = _packet(service, project["id"], "ALT-207")

    matrix = service.execute("decision.get_rule_matrix", {}, packet)["result"]
    assert matrix["meets_all_rules"] is True
    comparison = service.execute("results.compare_exact", {}, packet)["result"]
    assert comparison["outfall_volume_reduction_percent"] == 6.42

    runoff = service.execute(
        "results.get_runoff_ledger",
        {"storm_id": "synthetic_long_2_5in"},
        packet,
    )["result"]
    baseline = runoff["baseline"]["ledger"]
    alternative = runoff["alternative"]["ledger"]
    assert baseline["total_precipitation"]["primary"] == {
        "value": 0.937,
        "units": "acre-feet",
    }
    assert baseline["infiltration_loss"]["primary"]["value"] == 0.412
    assert alternative["infiltration_loss"]["primary"]["value"] == 0.436
    assert baseline["surface_runoff"]["primary"]["value"] == 0.514
    assert alternative["surface_runoff"]["primary"]["value"] == 0.447
    assert alternative["lid_drainage"]["primary"]["value"] == 0.014
    assert baseline["external_outflow"]["secondary"]["value"] == 0.167
    assert alternative["external_outflow"]["secondary"]["value"] == 0.15
    assert "not measured groundwater recharge" in runoff["term_note"]
    assert "not field evidence" in runoff["limitations"][0]


def test_packet_rejects_tampering_unknown_tools_and_unscoped_evidence(guided_evidence):
    project, repository, service = guided_evidence
    packet = _packet(service, project["id"], "ALT-017")

    tampered = packet.model_dump(mode="json")
    tampered["limitations"].append("fabricated")
    with pytest.raises(ValueError, match="digest"):
        service.execute("project.list_limitations", {}, tampered)
    with pytest.raises(ValueError, match="not allowlisted"):
        service.execute("filesystem.read", {}, packet)
    with pytest.raises(ValueError, match="unsupported arguments"):
        service.execute("model.get_fitness", {"sql": "select *"}, packet)
    with pytest.raises(KeyError, match="not present in the authorized packet"):
        service.execute("evidence.resolve", {"evidence_id": "fabricated:1"}, packet)

    current = repository.get_project(project["id"])
    repository.set_project_state(project["id"], {**current["state"], "ui_revision": 2})
    with pytest.raises(ValueError, match="stale for the current project record"):
        service.execute("project.list_limitations", {}, packet)


def test_finite_renderer_requires_exact_tool_claim_receipts(guided_evidence):
    project, _, service = guided_evidence
    packet = _packet(service, project["id"], "ALT-017")
    comparison = service.execute("results.compare_exact", {}, packet)
    claims = _claims_for_finite_plan(
        packet,
        "tradeoff_vs_acceptance",
        [{"tool": "results.compare_exact", "result": comparison}],
    )
    assert claims

    forged = {**comparison, "claim_receipts": [*comparison["claim_receipts"]]}
    forged["claim_receipts"][0] = {
        **forged["claim_receipts"][0],
        "claim_sha256": "0" * 64,
    }
    with pytest.raises(RuntimeError, match="CLAIM_RECEIPT_INVALID"):
        _claims_for_finite_plan(
            packet,
            "tradeoff_vs_acceptance",
            [{"tool": "results.compare_exact", "result": forged}],
        )

    limitations_only = service.execute("project.list_limitations", {}, packet)
    with pytest.raises(RuntimeError, match="REQUIRED_TOOL_EVIDENCE_MISSING"):
        _claims_for_finite_plan(
            packet,
            "tradeoff_vs_acceptance",
            [{"tool": "project.list_limitations", "result": limitations_only}],
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("missing_receipts", "CLAIM_RECEIPTS_MISSING"),
        ("empty_receipts", "TOOL_RESULT_NOT_BOUND"),
        ("wrong_packet", "TOOL_RESULT_NOT_BOUND"),
        ("wrong_tool", "TOOL_RESULT_NOT_BOUND"),
        ("wrong_claim_id", "CLAIM_RECEIPT_INVALID"),
    ],
)
def test_finite_renderer_rejects_unbound_same_tool_results(
    guided_evidence,
    mutation: str,
    error: str,
):
    project, _, service = guided_evidence
    packet = _packet(service, project["id"], "ALT-017")
    result = service.execute("results.compare_exact", {}, packet)
    changed = deepcopy(result)
    if mutation == "missing_receipts":
        changed.pop("claim_receipts")
    elif mutation == "empty_receipts":
        changed["claim_receipts"] = []
    elif mutation == "wrong_packet":
        changed["packet_sha256"] = "0" * 64
    elif mutation == "wrong_tool":
        changed["tool"] = "decision.get_rule_matrix"
    elif mutation == "wrong_claim_id":
        changed["claim_receipts"][0]["claim_id"] = "claim_fabricated"
    else:  # pragma: no cover - the parameter list is closed above
        raise AssertionError(mutation)

    with pytest.raises((RuntimeError, TypeError), match=error):
        _claims_for_finite_plan(
            packet,
            "tradeoff_vs_acceptance",
            [{"tool": "results.compare_exact", "result": changed}],
        )


def test_finite_renderer_rejects_a_valid_receipt_from_another_subject(guided_evidence):
    project, _, service = guided_evidence
    packet = _packet(service, project["id"], "ALT-017")
    other_packet = _packet(service, project["id"], "ALT-207")
    other = service.execute("results.compare_exact", {}, other_packet)

    with pytest.raises(RuntimeError, match="TOOL_RESULT_NOT_BOUND"):
        _claims_for_finite_plan(
            packet,
            "tradeoff_vs_acceptance",
            [{"tool": "results.compare_exact", "result": other}],
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("empty", "empty or malformed"),
        ("wrong_subject", "another subject"),
        ("perturbed_value", "conflicts with its packet claim"),
        ("perturbed_rule", "conflicts with its packet claim"),
    ],
)
def test_tool_receipts_are_not_minted_for_a_perturbed_handler_projection(
    guided_evidence,
    mutation: str,
    error: str,
):
    project, _, service = guided_evidence
    packet = _packet(service, project["id"], "ALT-017")
    original = service._handlers["results.compare_exact"]

    def changed(packet_value, arguments):
        result = deepcopy(original(packet_value, arguments))
        if mutation == "empty":
            return {}
        if mutation == "wrong_subject":
            result["alternative_id"] = "ALT-207"
        elif mutation == "perturbed_value":
            result["storms"][2]["alternative_outfall_peak_cfs"] += 1
        elif mutation == "perturbed_rule":
            result["screening"]["checks"][0]["passes"] = True
        return result

    service._handlers["results.compare_exact"] = changed
    with pytest.raises(RuntimeError, match=error):
        service.execute("results.compare_exact", {}, packet)


def test_non_demo_packet_redacts_project_title_and_arbitrary_state_notes(tmp_path):
    repository = ProjectRepository(tmp_path / "project")
    try:
        project = repository.create_project(
            project_id="project_sensitive",
            title="Secret Client Watershed",
            project_kind=ProjectKind.EXISTING,
            state={"limitations": ["Unreviewed confidential free-form note"]},
        )
        packet = EvidenceQueryService(repository).build_packet(
            project["project_id"],
            None,
            None,
            {"kind": "project", "identifier": project["project_id"]},
        )
        serialized = str(packet.model_dump(mode="json"))
        assert "Secret Client Watershed" not in serialized
        assert "confidential free-form note" not in serialized
        assert "Current engineering project" in serialized
        assert packet.limitations == [
            (
                "Hydraulic results are model outputs under documented assumptions; they are not "
                "field truth, regulatory approval, or proof of ecological benefit."
            ),
            "No engineer-attested Model Fitness Passport is stored for this revision.",
            (
                "The retained evidence does not measure water quality, groundwater recharge, "
                "habitat, heat, real-world flooding, maintenance performance, or lifecycle impacts."
            ),
        ]
    finally:
        repository.close()


def test_standard_project_tool_projection_excludes_engineer_free_text(guided_evidence):
    project, _, service = guided_evidence
    demo_packet = _packet(service, project["id"], "ALT-207")
    packet = demo_packet.model_copy(update={"project_kind": "existing"})
    canaries = [
        "Jane Engineer",
        "17 Private Watershed Road",
        "/Users/private/customer/model.inp",
        "WANDB_API_KEY=do-not-send",
        "customer-48291",
    ]
    local_result = {
        "disposition": "not_assessed",
        "criteria": [
            {
                "status": "review",
                "finding": canaries[0],
                "remediation": " ".join(canaries[1:]),
            }
        ],
        "limitations": ["Unrestricted note " + " ".join(canaries)],
        "reviewer_attested": False,
        "engineer_controlled": True,
    }

    projected = _provider_tool_result_projection(
        "model.get_fitness",
        local_result,
        packet=packet,
    )
    serialized = str(projected)
    assert all(canary not in serialized for canary in canaries)
    assert projected == {
        "disposition": "not_assessed",
        "criterion_status_counts": {"review": 1},
        "reviewer_attested": False,
        "engineer_controlled": True,
        "unresolved_limitation_count": 1,
    }
    assert local_result["criteria"][0]["finding"] == canaries[0]


def test_standard_project_keeps_canonical_tool_receipts_local(tmp_path):
    class Sink:
        def __init__(self) -> None:
            self.values = []

        def record(self, value) -> None:
            self.values.append(value)

    class Recorder:
        def __init__(self) -> None:
            self.records = []

        @contextmanager
        def tool(self, **_):
            sink = Sink()
            yield sink
            self.records.extend(sink.values)

    repository = ProjectRepository(tmp_path / "project")
    try:
        project = repository.create_project(
            project_id="project_standard",
            title="Private drainage study",
            project_kind=ProjectKind.EXISTING,
        )
        service = EvidenceQueryService(repository)
        packet = service.build_packet(
            project["project_id"],
            None,
            None,
            {"kind": "project", "identifier": project["project_id"]},
        )
        recorder = Recorder()
        results, names = _execute_deterministic_tools(
            query=service,
            packet=packet,
            project_record=repository.get_project(project["project_id"]),
            recorder=recorder,
            tool_names=["model.get_fitness", "project.list_limitations"],
        )

        assert names == ["model.get_fitness", "project.list_limitations"]
        assert all(item["result"]["packet_id"] == packet.packet_id for item in results)
        assert all(item["result"]["claim_receipts"] for item in results)
        assert _claims_for_finite_plan(packet, "general_evidence", results)
        trace_text = str(recorder.records)
        assert packet.packet_id not in trace_text
        assert project["project_id"] not in trace_text
        assert "Private drainage study" not in trace_text
    finally:
        repository.close()


def _enabled_policy(
    project_id: str,
    *,
    policy_id: str = "provider_policy_1",
    binding_id: str = "binding_1",
    external_project_id: str = "restricted-project-id",
) -> ProviderPolicy:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    return ProviderPolicy(
        policy_id=policy_id,
        project_id=project_id,
        enabled=True,
        observability_binding_id=binding_id,
        external_project_id=external_project_id,
        model_id="openai/gpt-5.1",
        prompt_version="evidence-guide-prompt-v1",
        response_schema_version="evidence-guide-answer-schema-v1",
        response_schema_sha256=RESPONSE_SCHEMA_SHA256,
        tool_contract_version="evidence-tools-v1",
        validator_version="evidence-validator-v1",
        inference_consent=True,
        content_logging_consent=False,
        consent_actor="Engineer A",
        consented_at=now,
        created_at=now,
        updated_at=now,
    )


def test_repository_persists_conversation_scope_and_acknowledged_weave_turn(tmp_path):
    repository = ProjectRepository(tmp_path / "project")
    try:
        project = repository.create_project(
            project_id="project_1",
            title="Test",
            project_kind=ProjectKind.EXISTING,
        )
        repository.upsert_project_data_policy(
            data_policy := ProjectDataPolicy(
                project_id=project["project_id"],
                classification="standard_engineering",
                deployment="wandb_cloud",
                acknowledged_by="Engineer A",
            )
        )
        repository.connect_observability_binding(
            ObservabilityBinding(
                binding_id="binding_1",
                project_id=project["project_id"],
                data_policy_id=data_policy.policy_id,
                entity="team",
                external_project="gi-project",
                external_project_id="restricted-project-id",
                deployment="wandb_cloud",
                verified=True,
                verified_at=datetime.now(UTC),
            )
        )
        policy = _enabled_policy(project["project_id"])
        repository.store_provider_policy(policy)
        assert repository.get_provider_policy_by_id(
            project["project_id"], policy.policy_id
        ) == policy.model_dump(mode="json")
        anchor = AssistantEvidenceAnchor(kind="project", identifier=project["project_id"])
        packet_id = "packet_0123456789abcdef01234567"
        packet_sha256 = "a" * 64
        conversation = AssistantConversation(
            conversation_id="conversation_1",
            project_id=project["project_id"],
            provider_policy_id=policy.policy_id,
            packet_id=packet_id,
            packet_sha256=packet_sha256,
            anchor=anchor,
            title="Evidence Guide",
        )
        repository.create_assistant_conversation(conversation)
        mismatched = AssistantTurn(
            turn_id="turn_bad_scope",
            conversation_id=conversation.conversation_id,
            project_id=project["project_id"],
            sequence=0,
            user_message="What is known?",
            anchor=anchor,
            evidence_packet_id="packet_wrong",
            evidence_packet_sha256=packet_sha256,
        )
        with pytest.raises(ValueError, match="conversation evidence scope"):
            repository.store_assistant_turn(mismatched)
        turn = AssistantTurn(
            turn_id="turn_1",
            conversation_id=conversation.conversation_id,
            project_id=project["project_id"],
            sequence=0,
            user_message="What is known?",
            anchor=anchor,
            evidence_packet_id=packet_id,
            evidence_packet_sha256=packet_sha256,
        )
        repository.store_assistant_turn(turn)
        reference = WeaveConversationReference(
            reference_id="weave_ref_1",
            project_id=project["project_id"],
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            entity="team",
            external_project="gi-project",
            trace_id="trace_1",
            root_span_id="span_1",
            url=("https://wandb.ai/team/gi-project/weave/agents/conversations/conversation_1"),
            agent_id="gi-evidence-guide",
            agent_version="1.0.0",
        )
        repository.add_weave_conversation_reference(reference)
        answer = AssistantAnswer(
            answer_id="answer_1",
            call_id="call_1",
            project_id=project["project_id"],
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            evidence_packet_id=packet_id,
            evidence_packet_sha256=packet_sha256,
            intent="explain evidence",
            answer="The local record contains no reviewed fitness passport.",
            limitations=["No engineer-attested Model Fitness Passport is stored."],
            weave_conversation=reference,
            provider_model_id=policy.model_id,
            provider_call_ids=["provider_call_1", "provider_call_2"],
            provider_input_tokens=10,
            provider_output_tokens=5,
            provider_total_tokens=15,
            provider_transport_attempts=2,
            provider_latency_ms=125.0,
            evidence_tool_calls=["model.get_fitness"],
            prompt_version=policy.prompt_version,
            response_schema_version=policy.response_schema_version,
            response_schema_sha256=policy.response_schema_sha256,
            tool_contract_version=policy.tool_contract_version,
            validator_version=policy.validator_version,
        )
        evaluation = AssistantEvaluationRecord(
            evaluation_id="evaluation_1",
            project_id=project["project_id"],
            answer_id=answer.answer_id,
            trace_id=reference.trace_id,
            valid=True,
            validator_version=policy.validator_version,
        )
        repository.store_validated_assistant_result(answer, evaluation)

        stored_conversation = repository.get_assistant_conversation(
            project["project_id"], conversation.conversation_id
        )
        assert stored_conversation["packet_id"] == packet_id
        assert stored_conversation["anchor"] == anchor.model_dump(mode="json")
        assert stored_conversation["status"] == "active"
        assert stored_conversation["weave_reference_id"] == reference.reference_id
        assert stored_conversation["weave_url"] == reference.url
        stored_turn = repository.get_assistant_turn(project["project_id"], turn.turn_id)
        assert stored_turn["state"] == "completed"
        assert stored_turn["answer_id"] == answer.answer_id
        assert repository.table_counts()["assistant_evaluations"] == 1
        assert repository._connection.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        repository.close()


def test_repository_blocks_wandb_inference_for_critical_infrastructure(tmp_path):
    repository = ProjectRepository(tmp_path / "project")
    try:
        project = repository.create_project(
            project_id="project_critical",
            title="Critical",
            project_kind=ProjectKind.EXISTING,
        )
        repository.upsert_project_data_policy(
            ProjectDataPolicy(
                project_id=project["project_id"],
                classification="critical_infrastructure",
                deployment="dedicated_cloud",
                acknowledged_by="Engineer A",
            )
        )
        with pytest.raises(ValueError, match="prohibited for critical-infrastructure"):
            repository.store_provider_policy(_enabled_policy(project["project_id"]))
    finally:
        repository.close()
