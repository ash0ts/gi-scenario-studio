from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

import pytest
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.usefulness_study import (
    REQUIRED_FROZEN_INPUTS,
    AdjudicatedOutcome,
    AgentRetrievalObservation,
    CampaignArtifactReceipt,
    CampaignEvidenceArtifact,
    ClaimEvidenceArtifactType,
    ClaimGateEvidence,
    ClaimStatus,
    ConventionalPackEquivalenceReceipt,
    EffectConclusion,
    EffectDirection,
    ExternalStudyReadiness,
    PairedEffectEstimate,
    PractitionerSession,
    PractitionerStratum,
    PreregistrationPublicationReceipt,
    RetainedReadbackBlob,
    RetrievalStopReason,
    ReviewerHandoffRecord,
    SessionStatus,
    ShadowProjectRecord,
    StudyCaseManifest,
    StudyDataClassification,
    StudyDisposition,
    StudyPhase,
    TraceIntegrityCampaignSummary,
    VerifiedCampaignEvidence,
    WandbCampaignReadback,
    WeaveTraceReadback,
    agent_complexity_task_matrix,
    assert_no_provider_target_leakage,
    assign_profession_stratified_three_period_latin_square,
    assign_three_period_latin_square,
    brier_score,
    build_default_synthetic_protocol,
    build_experiment_claim_ledger,
    campaign_readback_verifier_provenance,
    choose_confirmatory_sample_size,
    deterministic_program_manifest,
    freeze_adjudicated_outcome,
    freeze_case_manifest,
    paired_bootstrap_effect,
    paired_median_percent_reduction,
    privacy_project_practitioner_session,
    privacy_project_reviewer_handoff_record,
    privacy_project_shadow_record,
    render_usefulness_study_schemas,
    safety_attack_task_matrix,
    seal_conventional_pack_equivalence,
    seal_reviewer_handoff,
    validate_case_adjudication,
    validate_fair_method_inputs,
    validate_wandb_public_handoff_records,
    validate_wandb_public_study_projection,
    verify_campaign_artifact_bytes,
    zero_failure_upper_bound,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _sha(character: str) -> str:
    return character * 64


def _protocol():
    return build_default_synthetic_protocol(
        source_commit="a" * 40,
        model_ids=("openai/gpt-oss-120b", "gateway/frontier", "gateway/efficient"),
        frozen_input_sha256s={
            name: canonical_sha256({"frozen_input": name}) for name in REQUIRED_FROZEN_INPUTS
        },
        preregistered_at=NOW,
        frozen_at=NOW + timedelta(minutes=1),
    )


def _case(landscape_id: str, index: int = 0) -> StudyCaseManifest:
    draft = StudyCaseManifest(
        case_id=f"case-{landscape_id}",
        landscape_id=landscape_id,
        title=landscape_id.replace("-", " ").title(),
        data_classification=StudyDataClassification.CC0_SYNTHETIC,
        cloud_authorized=True,
        cc0_license_sha256=canonical_sha256({"license": landscape_id}),
        model_revision_sha256=canonical_sha256({"revision": landscape_id}),
        direct_evidence_index_sha256=canonical_sha256({"evidence": landscape_id}),
        engine_manifest_sha256=canonical_sha256({"engine": landscape_id}),
        expected_outcome_sha256=canonical_sha256({"outcome": landscape_id}),
        portfolio_id=f"portfolio-{index}",
        scenario_count=12,
        direct_job_count=12,
        complete_target_and_safeguard_ensemble=True,
        required_finding_aliases=("finding:required", "finding:critical"),
        critical_finding_aliases=("finding:critical",),
        benign_finding_aliases=("finding:benign",),
        distractor_aliases=("finding:distractor",),
        planted_conditions=("ROAD_MASK_ADVISORY",),
    )
    return freeze_case_manifest(draft, at=NOW + timedelta(minutes=2 + index))


def _outcome(case: StudyCaseManifest, index: int = 0) -> AdjudicatedOutcome:
    assert case.case_manifest_sha256 is not None
    draft = AdjudicatedOutcome(
        outcome_id=f"outcome-{case.case_id}",
        case_id=case.case_id,
        case_manifest_sha256=case.case_manifest_sha256,
        direct_evidence_index_sha256=case.direct_evidence_index_sha256,
        disposition=StudyDisposition.REVIEW_REQUIRED,
        required_finding_aliases=case.required_finding_aliases,
        critical_finding_aliases=case.critical_finding_aliases,
        acceptable_next_action_kinds=("FIELD_REVIEW",),
        ecological_adjudicator_sha256=_sha("e"),
        transportation_adjudicator_sha256=_sha("f"),
        rubric_sha256=_sha("b"),
    )
    return freeze_adjudicated_outcome(draft, at=NOW + timedelta(minutes=20 + index))


def test_locked_protocol_is_hash_verified_immutable_and_has_300_tasks() -> None:
    protocol = _protocol()
    assert protocol.frozen is True
    assert protocol.protocol_sha256 == canonical_sha256(
        protocol.model_dump(mode="json", exclude={"protocol_sha256"})
    )
    assert protocol.agent_complexity_task_count == 300
    with pytest.raises(ValidationError):
        protocol.title = "changed"

    tampered = protocol.model_dump(mode="json")
    tampered["finding_counts"] = [5, 15]
    with pytest.raises(ValidationError, match="protocol_sha256"):
        type(protocol).model_validate(tampered)


def test_protocol_refuses_missing_freeze_input_or_unapproved_model_slate() -> None:
    inputs = {name: _sha("a") for name in REQUIRED_FROZEN_INPUTS}
    inputs.pop("rubric")
    with pytest.raises(ValidationError, match="pin every required"):
        build_default_synthetic_protocol(
            source_commit="a" * 40,
            model_ids=("openai/gpt-oss-120b", "frontier", "efficient"),
            frozen_input_sha256s=inputs,
            preregistered_at=NOW,
            frozen_at=NOW,
        )
    with pytest.raises(ValidationError, match="continuity model"):
        build_default_synthetic_protocol(
            source_commit="a" * 40,
            model_ids=("different", "frontier", "efficient"),
            frozen_input_sha256s={name: _sha("a") for name in REQUIRED_FROZEN_INPUTS},
            preregistered_at=NOW,
            frozen_at=NOW,
        )


def test_program_manifest_is_deterministic_and_keeps_missing_evidence_blocked() -> None:
    protocol = _protocol()
    first = deterministic_program_manifest(protocol)
    second = deterministic_program_manifest(protocol)
    assert first == second
    assert first["agent_complexity"]["task_instances"] == 300
    assert first["stages"] == {
        "protocol": "COMPLETE",
        "independent_landscape_truth": "BLOCKED_INPUT",
        "agent_complexity": "BLOCKED_INPUT",
        "safety": "BLOCKED_INPUT",
        "practitioner_crossover": "BLOCKED_EXTERNAL",
        "partner_shadow": "BLOCKED_EXTERNAL",
    }
    digest = first.pop("manifest_sha256")
    assert digest == canonical_sha256(first)


def test_program_manifest_advances_only_with_frozen_truth_preflight_and_external_evidence() -> None:
    protocol = _protocol()
    cases = tuple(
        _case(landscape_id, index) for index, landscape_id in enumerate(protocol.landscape_ids)
    )
    outcomes = tuple(_outcome(case, index) for index, case in enumerate(cases))
    external = ExternalStudyReadiness(
        model_preflight_sha256s={
            model_id: canonical_sha256(model_id) for model_id in protocol.model_ids
        },
        human_subjects_approval_sha256=_sha("1"),
        consent_template_sha256=_sha("2"),
        compensation_policy_sha256=_sha("3"),
        accessibility_plan_sha256=_sha("4"),
        retention_policy_sha256=_sha("5"),
        pilot_roster_count=12,
        ecological_adjudicator_sha256=_sha("6"),
        transportation_adjudicator_sha256=_sha("7"),
        authorized_partner_project_sha256s=(_sha("8"), _sha("9"), _sha("a")),
        completed_campaign_sha256s={"agent_complexity": _sha("b")},
    )
    manifest = deterministic_program_manifest(
        protocol,
        cases=cases,
        adjudications=outcomes,
        external_readiness=external,
    )
    assert manifest["stages"]["independent_landscape_truth"] == "COMPLETE"
    assert manifest["stages"]["agent_complexity"] == "COMPLETE"
    assert manifest["stages"]["safety"] == "READY_TO_RUN"
    assert manifest["stages"]["practitioner_crossover"] == "READY_TO_RUN"
    assert manifest["stages"]["partner_shadow"] == "READY_TO_RUN"


def test_case_and_independent_adjudication_freeze_and_reject_stale_evidence() -> None:
    case = _case("linear-bottleneck")
    outcome = _outcome(case)
    validate_case_adjudication(case, outcome)

    stale = outcome.model_dump(mode="json")
    stale["case_manifest_sha256"] = _sha("0")
    stale["outcome_sha256"] = None
    stale["frozen_at"] = None
    stale_outcome = freeze_adjudicated_outcome(
        AdjudicatedOutcome.model_validate(stale), at=NOW + timedelta(hours=1)
    )
    with pytest.raises(ValueError, match="stale case manifest"):
        validate_case_adjudication(case, stale_outcome)


def test_case_aliases_and_cloud_authority_fail_closed() -> None:
    payload = _case("linear-bottleneck").model_dump(mode="json")
    payload["case_manifest_sha256"] = None
    payload["frozen_at"] = None
    payload["critical_finding_aliases"] = ["not-required"]
    with pytest.raises(ValidationError, match="critical findings"):
        StudyCaseManifest.model_validate(payload)
    payload["critical_finding_aliases"] = ["finding:critical"]
    payload["cloud_authorized"] = False
    with pytest.raises(ValidationError, match="cloud authority"):
        StudyCaseManifest.model_validate(payload)


def test_task_and_attack_matrices_are_deterministic_identity_only() -> None:
    protocol = _protocol()
    tasks = agent_complexity_task_matrix(protocol)
    assert tasks == agent_complexity_task_matrix(protocol)
    assert len(tasks) == 300
    assert len({row["task_sha256"] for row in tasks}) == 300
    assert sum(row["stratified_repeat_count"] == 5 for row in tasks) == 60
    repeat_counts = Counter(
        (row["landscape_id"], row["finding_count"], row["distractor_share"])
        for row in tasks
        if row["stratified_repeat_count"] == 5
    )
    assert len(repeat_counts) == 60
    assert set(repeat_counts.values()) == {1}
    assert all(row["primary_analysis_equally_weighted"] is True for row in tasks)
    assert all(row["primary_analysis_weight_numerator"] == 1 for row in tasks)
    assert all(row["primary_analysis_weight_denominator"] == 300 for row in tasks)
    assert len({row["repeat_stratum_sha256"] for row in tasks}) == 60
    assert all("expected" not in key for row in tasks for key in row)

    attacks = safety_attack_task_matrix(protocol)
    assert len(attacks) == 1_000
    assert len({row["attack_task_sha256"] for row in attacks}) == 1_000


def test_fair_input_validator_rejects_answer_contracts_and_unequal_evidence() -> None:
    evidence = {"aliases": ["a", "b"], "limitations": ["screening only"]}
    schema = {"type": "object", "required": ["finding_aliases"]}
    evidence_hash, schema_hash = validate_fair_method_inputs(
        {"single": evidence, "agent": evidence},
        {"single": schema, "agent": schema},
    )
    assert evidence_hash == canonical_sha256(evidence)
    assert schema_hash == canonical_sha256(schema)
    with pytest.raises(ValueError, match="target leakage"):
        assert_no_provider_target_leakage(
            {"nested": {"AuthorizedAnswerContract": {"aliases": ["critical"]}}}
        )
    with pytest.raises(ValueError, match="different evidence"):
        validate_fair_method_inputs(
            {"single": evidence, "agent": {"aliases": ["a"]}},
            {"single": schema, "agent": schema},
        )


def test_agent_retrieval_observation_enforces_authorized_alias_partition() -> None:
    observation = AgentRetrievalObservation(
        invocation_id="invocation-1",
        case_id="case-1",
        arm_id="adaptive-agent",
        model_id="model-1",
        turn_index=2,
        tool_call_index=2,
        tool_name="review_brief_get_findings",
        available_aliases=("a", "b", "c"),
        requested_aliases=("a", "b"),
        returned_aliases=("a", "b"),
        relevant_aliases=("a",),
        irrelevant_aliases=("b",),
        seen_before_aliases=("b", "c"),
        duplicate_aliases=("b",),
        context_bytes_before=100,
        context_bytes_after=140,
        tool_result_bytes=40,
        provider_input_tokens=50,
        provider_output_tokens=10,
        latency_ms=12.5,
        cost_usd=0.001,
        stop_reason=RetrievalStopReason.CONTINUE,
        weave_trace_id="trace-1",
    )
    payload = observation.model_dump(mode="json")
    payload["requested_aliases"] = ["not-authorized"]
    with pytest.raises(ValidationError, match="outside its authorized"):
        AgentRetrievalObservation.model_validate(payload)


def test_three_period_assignment_is_reproducible_balanced_and_no_case_repeats() -> None:
    participants = tuple(canonical_sha256({"participant": index}) for index in range(12))
    assignments = assign_three_period_latin_square(
        participants,
        arm_ids=("conventional", "eco", "agent"),
        case_ids=("case-a", "case-b", "case-c"),
        seed=1789,
    )
    assert assignments == assign_three_period_latin_square(
        participants,
        arm_ids=("conventional", "eco", "agent"),
        case_ids=("case-a", "case-b", "case-c"),
        seed=1789,
    )
    by_participant = defaultdict(list)
    for item in assignments:
        by_participant[item.participant_pseudonym_sha256].append(item)
    assert all(len({row.arm_id for row in rows}) == 3 for rows in by_participant.values())
    assert all(len({row.case_id for row in rows}) == 3 for rows in by_participant.values())
    for period in (1, 2, 3):
        period_rows = [row for row in assignments if row.period == period]
        assert set(Counter(row.arm_id for row in period_rows).values()) == {4}
        assert set(Counter(row.case_id for row in period_rows).values()) == {4}
    assert set(Counter((row.arm_id, row.case_id) for row in assignments).values()) == {4}


def test_profession_stratified_williams_allocation_balances_confirmatory_study() -> None:
    participants_by_stratum = {
        stratum: tuple(
            canonical_sha256({"stratum": stratum.value, "participant": index})
            for index in range(10)
        )
        for stratum in PractitionerStratum
    }
    assignments = assign_profession_stratified_three_period_latin_square(
        participants_by_stratum,
        arm_ids=("conventional-pack", "eco-deterministic", "eco-agent"),
        case_ids=("case-a", "case-b", "case-c"),
        seed=1789,
        study_phase=StudyPhase.CONFIRMATORY,
    )
    assert assignments == assign_profession_stratified_three_period_latin_square(
        participants_by_stratum,
        arm_ids=("conventional-pack", "eco-deterministic", "eco-agent"),
        case_ids=("case-a", "case-b", "case-c"),
        seed=1789,
        study_phase=StudyPhase.CONFIRMATORY,
    )
    assert len(assignments) == 90
    assert {row.study_phase for row in assignments} == {StudyPhase.CONFIRMATORY}
    for stratum in PractitionerStratum:
        stratum_rows = [row for row in assignments if row.stratum == stratum]
        assert len({row.participant_pseudonym_sha256 for row in stratum_rows}) == 10
        for participant in {row.participant_pseudonym_sha256 for row in stratum_rows}:
            rows = [row for row in stratum_rows if row.participant_pseudonym_sha256 == participant]
            assert len({row.arm_id for row in rows}) == 3
            assert len({row.case_id for row in rows}) == 3
    for period in (1, 2, 3):
        period_rows = [row for row in assignments if row.period == period]
        assert set(Counter(row.arm_id for row in period_rows).values()) == {10}
        assert set(Counter(row.case_id for row in period_rows).values()) == {10}
    assert set(Counter((row.arm_id, row.case_id) for row in assignments).values()) == {10}


def test_profession_stratified_allocation_separates_pilot_and_rejects_imbalance() -> None:
    pilot = {
        stratum: tuple(
            canonical_sha256({"pilot_stratum": stratum.value, "participant": index})
            for index in range(4)
        )
        for stratum in PractitionerStratum
    }
    assignments = assign_profession_stratified_three_period_latin_square(
        pilot,
        arm_ids=("conventional-pack", "eco-deterministic", "eco-agent"),
        case_ids=("case-a", "case-b", "case-c"),
        seed=44,
        study_phase=StudyPhase.PILOT,
    )
    assert {row.study_phase for row in assignments} == {StudyPhase.PILOT}
    unbalanced = dict(pilot)
    unbalanced[PractitionerStratum.STATE_DOT] = (
        *unbalanced[PractitionerStratum.STATE_DOT],
        _sha("9"),
    )
    with pytest.raises(ValueError, match="equal profession strata"):
        assign_profession_stratified_three_period_latin_square(
            unbalanced,
            arm_ids=("conventional-pack", "eco-deterministic", "eco-agent"),
            case_ids=("case-a", "case-b", "case-c"),
            seed=44,
            study_phase=StudyPhase.PILOT,
        )


def test_pilot_sizing_uses_variance_and_reports_the_36_person_cap() -> None:
    precise = choose_confirmatory_sample_size(
        [1.0] * 12,
        minimum_meaningful_paired_effect=0.5,
    )
    assert precise.selected_sample_size == 24
    assert precise.underpowered_at_cap is False

    noisy = choose_confirmatory_sample_size(
        [-100.0, 100.0] * 6,
        minimum_meaningful_paired_effect=1.0,
    )
    assert noisy.selected_sample_size == 36
    assert noisy.underpowered_at_cap is True
    assert noisy.required_sample_size_unrounded > 36


def test_zero_failure_safety_bound_requires_at_least_100_trials_for_three_percent() -> None:
    assert zero_failure_upper_bound(trials=100) == pytest.approx(0.029513, rel=1e-5)
    assert zero_failure_upper_bound(trials=99) > 0.029513
    with pytest.raises(ValueError, match="at least one"):
        zero_failure_upper_bound(trials=0)


def test_paired_effects_are_deterministic_and_keep_direction_explicit() -> None:
    lower = paired_bootstrap_effect(
        [10, 11, 12, 13, 14, 15],
        [8, 9, 10, 11, 12, 13],
        effect_id="effect-misses",
        outcome_id="critical-misses",
        reference_arm_id="conventional",
        comparison_arm_id="eco",
        estimand="mean paired difference",
        unit="count",
        direction=EffectDirection.LOWER_IS_BETTER,
        seed=7,
        resamples=500,
    )
    assert lower.estimate == -2
    assert lower.conclusion == EffectConclusion.IMPROVED
    assert lower == paired_bootstrap_effect(
        [10, 11, 12, 13, 14, 15],
        [8, 9, 10, 11, 12, 13],
        effect_id="effect-misses",
        outcome_id="critical-misses",
        reference_arm_id="conventional",
        comparison_arm_id="eco",
        estimand="mean paired difference",
        unit="count",
        direction=EffectDirection.LOWER_IS_BETTER,
        seed=7,
        resamples=500,
    )
    time = paired_median_percent_reduction(
        [100, 120, 140, 160],
        [80, 96, 112, 128],
        effect_id="effect-time",
        outcome_id="verification-time",
        reference_arm_id="conventional",
        comparison_arm_id="eco",
        seed=11,
        resamples=500,
    )
    assert time.estimate == pytest.approx(20)
    assert time.conclusion == EffectConclusion.IMPROVED
    assert brier_score([0.8, 0.2], [True, False]) == pytest.approx(0.04)


def _completed_session(
    *, consent: bool = True, study_phase: StudyPhase = StudyPhase.CONFIRMATORY
) -> PractitionerSession:
    return PractitionerSession(
        session_id="local-session-1",
        participant_pseudonym_sha256=_sha("a"),
        study_phase=study_phase,
        stratum=PractitionerStratum.STATE_DOT,
        period=1,
        sequence_id="WILLIAMS-1-B1",
        arm_id="eco-deterministic",
        case_id="case-a",
        status=SessionStatus.COMPLETED,
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=10),
        critical_safeguard_misses=0,
        selected_disposition=StudyDisposition.REVIEW_REQUIRED,
        correct_disposition=True,
        correct_next_action=True,
        complete_evidence_reconstruction=True,
        false_alarms=0,
        verification_seconds=600,
        first_blocker_seconds=120,
        field_plan_seconds=240,
        prohibited_claim_errors=0,
        evidence_package_correct=True,
        edit_count=1,
        nasa_tlx=25,
        confidence=0.8,
        outcome_correct=True,
        overconfident_error=False,
        usefulness_positive=True,
        conventional_pack_equivalence_receipt_sha256=_sha("d"),
        consent_to_deidentified_export=consent,
        consent_record_sha256=_sha("b"),
        organizational_review_sha256=_sha("c"),
    )


def test_practitioner_privacy_projection_excludes_identity_time_and_raw_material() -> None:
    projected = privacy_project_practitioner_session(
        _completed_session(),
        projection_salt=b"0123456789abcdef",
        preregistration=_preregistration(_protocol()),
    )
    serialized = str(projected)
    assert "local-session-1" not in serialized
    assert _sha("a") not in serialized
    assert "started_at" not in projected and "completed_at" not in projected
    assert "raw_note" not in {key.casefold() for key in projected}
    assert "recording" not in {key.casefold() for key in projected}
    assert projected["raw_notes_excluded"] is True
    assert projected["raw_recordings_excluded"] is True
    assert len(projected["participant_study_key"]) == 64
    assert projected["study_phase"] == "CONFIRMATORY"
    assert projected["field_plan_seconds"] == 240
    assert projected["overconfidence_threshold"] == 0.8
    assert projected["overconfident_error"] is False
    with pytest.raises(ValueError, match="did not consent"):
        privacy_project_practitioner_session(
            _completed_session(consent=False),
            projection_salt=b"0123456789abcdef",
            preregistration=_preregistration(_protocol()),
        )


def test_practitioner_session_distinguishes_pilot_and_derives_overconfident_error() -> None:
    pilot = _completed_session(study_phase=StudyPhase.PILOT)
    assert pilot.study_phase == StudyPhase.PILOT
    payload = pilot.model_dump(mode="json")
    payload["outcome_correct"] = False
    with pytest.raises(ValidationError, match="overconfident_error"):
        PractitionerSession.model_validate(payload)
    payload["overconfident_error"] = True
    parsed = PractitionerSession.model_validate(payload)
    assert parsed.overconfident_error is True


def test_shadow_projection_is_aggregate_only_and_unlinked() -> None:
    record = ShadowProjectRecord(
        shadow_record_id="private-shadow-record",
        partner_pseudonym_sha256=_sha("1"),
        project_pseudonym_sha256=_sha("2"),
        data_classification=StudyDataClassification.DEIDENTIFIED_AUTHORIZED,
        authorization_sha256=_sha("3"),
        consent_to_approved_aggregate_export=True,
        consent_record_sha256=_sha("7"),
        organizational_review_sha256=_sha("8"),
        independent_panel_record_sha256=_sha("4"),
        historical_review_record_sha256=_sha("5"),
        approved_aggregate_sha256=_sha("6"),
        person_hours=12.5,
        review_cycles=2,
        discrepancies=1,
        surfaced_assumptions=3,
        disposition_agreement=True,
        all_discrepancies_adjudicated=True,
        completed_at=NOW,
    )
    projected = privacy_project_shadow_record(
        record,
        projection_salt=b"0123456789abcdef",
        preregistration=_preregistration(_protocol()),
    )
    serialized = str(projected)
    assert "private-shadow-record" not in serialized
    assert _sha("1") not in serialized and _sha("2") not in serialized
    assert "historical_review_record_sha256" not in projected
    assert projected["approved_aggregate_sha256"] == _sha("6")


def test_reviewer_handoff_is_blinded_hash_bound_and_requires_independent_dyad() -> None:
    sealed = seal_reviewer_handoff(
        ReviewerHandoffRecord(
            handoff_id="handoff-1",
            dyad_id="dyad-1",
            first_reviewer_pseudonym_sha256=_sha("1"),
            second_reviewer_pseudonym_sha256=_sha("2"),
            first_reviewer_stratum=PractitionerStratum.STATE_DOT,
            second_reviewer_stratum=PractitionerStratum.WILDLIFE_AGENCY,
            case_manifest_sha256=_sha("3"),
            adjudicated_outcome_sha256=_sha("4"),
            evidence_package_sha256=_sha("5"),
            stale_artifact_sha256=_sha("6"),
            consent_to_deidentified_export=True,
            consent_record_sha256=_sha("7"),
            organizational_review_sha256=_sha("8"),
            disposition_verified=True,
            claim_chain_reconstructed=True,
            stale_artifact_found=True,
            correct_next_action_identified=True,
            provenance_completeness=1,
            wrong_source_acceptance_count=0,
            unresolved_question_count=0,
            verification_seconds=420,
            disposition_reproducible=True,
            completed_at=NOW,
        )
    )
    assert sealed.record_sha256 == canonical_sha256(
        sealed.model_dump(mode="json", exclude={"record_sha256"})
    )
    projection = privacy_project_reviewer_handoff_record(
        sealed,
        projection_salt=b"0123456789abcdef",
        preregistration=_preregistration(_protocol()),
    )
    assert "handoff-1" not in str(projection)
    assert _sha("1") not in str(projection) and _sha("2") not in str(projection)
    assert projection["raw_identifiers_excluded"] is True
    validated = validate_wandb_public_handoff_records(
        [{"record_id": projection["handoff_key"], "handoff_projection": projection}],
        preregistration_receipt_sha256=projection["preregistration_receipt_sha256"],
    )
    assert len(validated) == 1
    with pytest.raises(ValueError, match="raw or unknown"):
        validate_wandb_public_handoff_records(
            [
                {
                    "record_id": projection["handoff_key"],
                    "handoff_projection": projection,
                    "completed_at": NOW.isoformat(),
                }
            ],
            preregistration_receipt_sha256=projection["preregistration_receipt_sha256"],
        )
    duplicate = sealed.model_dump(mode="json")
    duplicate["record_sha256"] = None
    duplicate["second_reviewer_pseudonym_sha256"] = _sha("1")
    with pytest.raises(ValidationError, match="independent reviewers"):
        ReviewerHandoffRecord.model_validate(duplicate)


def test_conventional_pack_equivalence_requires_every_scientific_component() -> None:
    protocol = _protocol()
    receipt = _sealed_equivalence(protocol)
    assert receipt.receipt_sha256 is not None
    incomplete = receipt.model_dump(mode="json")
    incomplete["receipt_sha256"] = None
    incomplete["case_component_sha256s"]["case-a"].pop("qa")
    with pytest.raises(ValidationError, match="every case must pin"):
        ConventionalPackEquivalenceReceipt.model_validate(incomplete)


def _sealed_equivalence(protocol) -> ConventionalPackEquivalenceReceipt:
    assert protocol.protocol_sha256 is not None
    components = {
        case_id: {
            component: canonical_sha256({"case": case_id, "component": component})
            for component in ("matrices", "rasters", "costs", "assumptions", "qa", "hashes")
        }
        for case_id in ("case-a", "case-b", "case-c")
    }
    return seal_conventional_pack_equivalence(
        ConventionalPackEquivalenceReceipt(
            protocol_sha256=protocol.protocol_sha256,
            source_commit=protocol.source_commit,
            case_component_sha256s=components,
            conventional_pack_manifest_sha256=_sha("1"),
            ecocorridor_evidence_manifest_sha256=_sha("2"),
            independent_verifier_sha256=_sha("3"),
            verified_at=NOW,
        )
    )


def _effect(
    outcome_id: str,
    *,
    reference: str,
    comparison: str,
    direction: EffectDirection,
    low: float,
    estimate: float,
    high: float,
    margin: float | None = None,
) -> PairedEffectEstimate:
    if direction == EffectDirection.LOWER_IS_BETTER:
        if high < 0:
            conclusion = EffectConclusion.IMPROVED
        elif margin is not None and high <= margin:
            conclusion = EffectConclusion.NONINFERIOR
        else:
            conclusion = EffectConclusion.INCONCLUSIVE
    else:
        conclusion = EffectConclusion.IMPROVED if low > 0 else EffectConclusion.INCONCLUSIVE
    return PairedEffectEstimate(
        effect_id=f"effect-{outcome_id}",
        outcome_id=outcome_id,
        reference_arm_id=reference,
        comparison_arm_id=comparison,
        estimand="preregistered paired effect",
        unit="proportion" if "time" not in outcome_id and "burden" not in outcome_id else "percent",
        direction=direction,
        pair_count=30,
        estimate=estimate,
        confidence_interval_low=low,
        confidence_interval_high=high,
        bootstrap_seed=71,
        bootstrap_resamples=10_000,
        noninferiority_margin=margin,
        conclusion=conclusion,
    )


def _json_blob(value: dict[str, object]) -> RetainedReadbackBlob:
    return RetainedReadbackBlob.from_json_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    )


def _wandb_readback(
    run_id: str,
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> WandbCampaignReadback:
    started = started_at or NOW - timedelta(hours=1)
    finished = finished_at or NOW
    committed = finished - timedelta(minutes=5)
    run_blob = _json_blob(
        {
            "run_id": run_id,
            "state": "FINISHED",
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "summary": {"retained": True},
        }
    )
    artifact_ref = f"wandb/ecocorridor/{run_id}:v0"
    artifact_blob = _json_blob(
        {
            "artifact_ref": artifact_ref,
            "state": "COMMITTED",
            "committed_at": committed.isoformat(),
            "manifest": {"retained": True},
        }
    )
    values = {
        "run_id": run_id,
        "run_url": f"https://wandb.ai/wandb/ecocorridor/runs/{run_id}",
        "run_state": "FINISHED",
        "run_started_at": started,
        "run_finished_at": finished,
        "run_summary_sha256": run_blob.content_sha256,
        "artifact_ref": artifact_ref,
        "artifact_url": f"https://wandb.ai/wandb/ecocorridor/artifacts/{run_id}/v0",
        "artifact_state": "COMMITTED",
        "artifact_committed_at": committed,
        "artifact_manifest_sha256": artifact_blob.content_sha256,
        "retained_run_response": run_blob,
        "retained_artifact_manifest_response": artifact_blob,
        "verifier": campaign_readback_verifier_provenance(verified_at=finished),
    }
    draft = WandbCampaignReadback.model_construct(**values, readback_sha256=_sha("0"))
    serialized = draft.model_dump(mode="json", exclude={"readback_sha256"})
    return WandbCampaignReadback(
        **serialized,
        readback_sha256=canonical_sha256(serialized),
    )


def _trace_readback(run_id: str, trace_id: str) -> WeaveTraceReadback:
    request = {"trace": trace_id, "request": True}
    response = {"trace": trace_id, "response": True}
    completed = NOW - timedelta(minutes=10)
    trace_blob = _json_blob(
        {
            "trace_id": trace_id,
            "root_call_id": f"root-{trace_id}",
            "wandb_run_id": run_id,
            "status": "FINISHED",
            "completed_at": completed.isoformat(),
            "provider_request": request,
            "provider_response": response,
        }
    )
    values = {
        "trace_id": trace_id,
        "root_call_id": f"root-{trace_id}",
        "wandb_run_id": run_id,
        "status": "FINISHED",
        "completed_at": completed,
        "provider_request_sha256": canonical_sha256(request),
        "provider_response_sha256": canonical_sha256(response),
        "trace_content_sha256": trace_blob.content_sha256,
        "retained_trace_response": trace_blob,
        "verifier": campaign_readback_verifier_provenance(verified_at=NOW),
    }
    draft = WeaveTraceReadback.model_construct(**values, readback_sha256=_sha("0"))
    serialized = draft.model_dump(mode="json", exclude={"readback_sha256"})
    return WeaveTraceReadback(
        **serialized,
        readback_sha256=canonical_sha256(serialized),
    )


def _preregistration(protocol) -> PreregistrationPublicationReceipt:
    assert protocol.protocol_sha256 is not None
    wandb = _wandb_readback(
        "preregistration-run",
        started_at=NOW - timedelta(hours=4),
        finished_at=NOW - timedelta(hours=2),
    )
    published = RetainedReadbackBlob.from_json_bytes(
        json.dumps(
            protocol.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    values = {
        "protocol_sha256": protocol.protocol_sha256,
        "source_repository": protocol.source_repository,
        "source_commit": protocol.source_commit,
        "published_at": wandb.artifact_committed_at,
        "frozen_plan_sha256s": {},
        "published_protocol": published,
        "wandb": wandb,
        "verifier": campaign_readback_verifier_provenance(verified_at=NOW),
    }
    draft = PreregistrationPublicationReceipt.model_construct(**values, receipt_sha256=_sha("0"))
    serialized = draft.model_dump(mode="json", exclude={"receipt_sha256"})
    return PreregistrationPublicationReceipt(
        **serialized,
        receipt_sha256=canonical_sha256(serialized),
    )


def _verified_raw_campaign(
    protocol,
    artifact_type: ClaimEvidenceArtifactType,
    records: list[dict[str, object]],
    *,
    traces: tuple[WeaveTraceReadback, ...] = (),
    source_commit: str | None = None,
    source_repository: str | None = None,
) -> VerifiedCampaignEvidence:
    assert protocol.protocol_sha256 is not None
    run_id = f"run-{artifact_type.value}"
    artifact = CampaignEvidenceArtifact(
        artifact_type=artifact_type,
        artifact_name=f"ecocorridor-{artifact_type.value}",
        artifact_version="v0",
        protocol_sha256=protocol.protocol_sha256,
        source_repository=source_repository or protocol.source_repository,
        source_commit=source_commit or protocol.source_commit,
        preregistration=_preregistration(protocol),
        wandb=_wandb_readback(run_id),
        records=tuple(records),
        weave_traces=traces,
        records_sha256=canonical_sha256(records),
    )
    return verify_campaign_artifact_bytes(
        artifact.model_dump_json().encode(),
        protocol=protocol,
        verified_at=NOW,
    )


def _passing_trace_campaign(protocol) -> VerifiedCampaignEvidence:
    run_id = f"run-{ClaimEvidenceArtifactType.TRACE_INTEGRITY_SUMMARY.value}"
    trace = _trace_readback(run_id, "trace-001")
    return _verified_raw_campaign(
        protocol,
        ClaimEvidenceArtifactType.TRACE_INTEGRITY_SUMMARY,
        [
            {
                "record_id": "trace-record-001",
                "provider_invoked": True,
                "released": True,
                "weave_trace_ids": [trace.trace_id],
            }
        ],
        traces=(trace,),
    )


def _passing_synthetic_campaign(protocol) -> VerifiedCampaignEvidence:
    records = [
        {
            "record_id": f"synthetic-{index}",
            "direct_landscape_sha256": canonical_sha256({"landscape": index}),
            "required_finding_aliases": [f"finding-{index}"],
            "critical_finding_aliases": [f"finding-{index}"],
            "selected_finding_aliases": [f"finding-{index}"],
            "relevant_evidence_aliases": [f"evidence-{index}"],
            "selected_evidence_aliases": [f"evidence-{index}"],
            "prohibited_claim_ids": [],
            "expected_fail_closed": False,
            "draft_saved": False,
            "adjudicated_truth_sha256": canonical_sha256({"truth": index}),
            "weave_trace_ids": [],
        }
        for index in range(5)
    ]
    return _verified_raw_campaign(
        protocol,
        ClaimEvidenceArtifactType.SYNTHETIC_TASK_SUMMARY,
        records,
    )


def test_claim_ledger_requires_raw_verifier_evidence_and_defaults_blocked() -> None:
    protocol = _protocol()
    blocked = build_experiment_claim_ledger(
        protocol,
        ClaimGateEvidence(),
        ledger_id="claim-ledger-empty",
        generated_at=NOW,
    )
    assert {claim.status for claim in blocked.claims} == {ClaimStatus.BLOCKED}
    with pytest.raises(ValidationError, match="Extra inputs"):
        ClaimGateEvidence(provider_invocations=100)

    evidence = ClaimGateEvidence(
        campaigns=(_passing_trace_campaign(protocol), _passing_synthetic_campaign(protocol))
    )
    ledger = build_experiment_claim_ledger(
        protocol,
        evidence,
        ledger_id="claim-ledger-verified-raw",
        generated_at=NOW,
    )
    by_id = {claim.claim_id: claim for claim in ledger.claims}
    assert by_id["trace-integrity"].status == ClaimStatus.SUPPORTED
    assert by_id["synthetic-task-correctness"].status == ClaimStatus.SUPPORTED
    assert by_id["adaptive-agent-efficiency"].status == ClaimStatus.BLOCKED
    assert by_id["practitioner-workflow-improvement"].status == ClaimStatus.BLOCKED
    assert ledger.ledger_sha256 == canonical_sha256(
        ledger.model_dump(mode="json", exclude={"ledger_sha256"})
    )


def test_self_hashed_perfect_summary_and_dummy_receipt_cannot_promote_claim() -> None:
    protocol = _protocol()
    valid = _passing_trace_campaign(protocol)
    perfect_values = {
        "provider_invocations": 1_000_000,
        "exact_trace_readback_count": 1_000_000,
        "released_without_trace_count": 0,
        "trace_index_sha256": _sha("f"),
    }
    perfect_summary = TraceIntegrityCampaignSummary(
        **perfect_values,
        summary_sha256=canonical_sha256({"campaign_kind": "TRACE_INTEGRITY", **perfect_values}),
    )
    receipt_values = valid.artifact_receipt.model_dump(mode="json")
    receipt_values["summary_sha256"] = perfect_summary.summary_sha256
    receipt_values["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in receipt_values.items() if key != "receipt_sha256"}
    )
    forged_receipt = CampaignArtifactReceipt.model_validate(receipt_values)
    forged = VerifiedCampaignEvidence.model_construct(
        raw_artifact_json=valid.raw_artifact_json,
        summary=perfect_summary,
        artifact_receipt=forged_receipt,
    )
    ledger = build_experiment_claim_ledger(
        protocol,
        ClaimGateEvidence(campaigns=(forged,)),
        ledger_id="self-hashed-forgery",
        generated_at=NOW,
    )
    assert {claim.status for claim in ledger.claims} == {ClaimStatus.BLOCKED}


def test_claim_ledger_rejects_unavailable_or_tampered_raw_bytes() -> None:
    protocol = _protocol()
    valid = _passing_trace_campaign(protocol)
    tampered = VerifiedCampaignEvidence.model_construct(
        raw_artifact_json='{"records": []}',
        summary=valid.summary,
        artifact_receipt=valid.artifact_receipt,
    )
    ledger = build_experiment_claim_ledger(
        protocol,
        ClaimGateEvidence.model_construct(campaigns=(tampered,)),
        ledger_id="tampered-raw-bytes",
        generated_at=NOW,
    )
    assert {claim.status for claim in ledger.claims} == {ClaimStatus.BLOCKED}


def test_cloud_and_trace_readbacks_require_exact_retained_bytes() -> None:
    wandb = _wandb_readback("readback-tamper")
    payload = wandb.model_dump(mode="json")
    payload["retained_run_response"]["content_base64"] = "e30="
    with pytest.raises(ValidationError, match="byte count differs|digest differs"):
        WandbCampaignReadback.model_validate(payload)

    trace = _trace_readback("readback-tamper", "trace-tamper")
    trace_payload = trace.model_dump(mode="json")
    trace_payload["retained_trace_response"]["content_sha256"] = _sha("0")
    with pytest.raises(ValidationError, match="digest differs"):
        WeaveTraceReadback.model_validate(trace_payload)


def test_campaign_rejects_stale_readback_verifier_and_late_preregistration() -> None:
    protocol = _protocol()
    valid = _passing_trace_campaign(protocol)
    raw = json.loads(valid.raw_artifact_json)
    raw["wandb"]["verifier"]["verifier_source_sha256"] = _sha("0")
    readback = raw["wandb"]
    readback["readback_sha256"] = canonical_sha256(
        {key: value for key, value in readback.items() if key != "readback_sha256"}
    )
    raw["records_sha256"] = canonical_sha256(raw["records"])
    with pytest.raises(ValueError, match="current retained-byte verifier"):
        verify_campaign_artifact_bytes(json.dumps(raw).encode(), protocol=protocol, verified_at=NOW)

    prereg = _preregistration(protocol).model_dump(mode="json")
    campaign = _wandb_readback("late-prereg-campaign")
    prereg["published_at"] = campaign.run_started_at.isoformat()
    prereg["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in prereg.items() if key != "receipt_sha256"}
    )
    artifact_payload = json.loads(valid.raw_artifact_json)
    artifact_payload["preregistration"] = prereg
    with pytest.raises(ValidationError, match="publication time differs|must predate"):
        CampaignEvidenceArtifact.model_validate(artifact_payload)


def test_generic_agent_claim_path_rejects_legacy_summary_rows_without_full_plan() -> None:
    protocol = _protocol()
    with pytest.raises(TypeError, match="complete frozen campaign plan"):
        _verified_raw_campaign(
            protocol,
            ClaimEvidenceArtifactType.AGENT_COMPLEXITY_SUMMARY,
            [
                {
                    "record_id": "legacy-perfect-agent-row",
                    "task_id": "made-up",
                    "model_id": protocol.model_ids[0],
                    "replicate_index": 0,
                    "arms": {},
                    "weave_trace_ids": [],
                }
            ],
        )


def test_wandb_public_study_ingestion_rejects_raw_identity_time_and_notes() -> None:
    projection = privacy_project_practitioner_session(
        _completed_session(),
        projection_salt=b"0123456789abcdef",
        preregistration=_preregistration(_protocol()),
    )
    assert validate_wandb_public_study_projection(projection).raw_identifiers_excluded
    for forbidden in (
        {"participant_pseudonym_sha256": _sha("a")},
        {"started_at": NOW.isoformat()},
        {"notes": "raw participant note"},
    ):
        with pytest.raises(ValueError, match="forbidden"):
            validate_wandb_public_study_projection({**projection, **forbidden})


@pytest.mark.parametrize(
    ("source_commit", "source_repository"),
    (("b" * 40, None), (None, "other/repository")),
)
def test_verifier_rejects_different_source_identity(
    source_commit: str | None, source_repository: str | None
) -> None:
    protocol = _protocol()
    run_id = f"run-{ClaimEvidenceArtifactType.TRACE_INTEGRITY_SUMMARY.value}"
    trace = _trace_readback(run_id, "trace-stale")
    with pytest.raises(ValueError, match="another protocol/source"):
        _verified_raw_campaign(
            protocol,
            ClaimEvidenceArtifactType.TRACE_INTEGRITY_SUMMARY,
            [
                {
                    "record_id": "stale-source",
                    "provider_invoked": True,
                    "released": True,
                    "weave_trace_ids": [trace.trace_id],
                }
            ],
            traces=(trace,),
            source_commit=source_commit,
            source_repository=source_repository,
        )


def test_campaign_receipt_rejects_legacy_self_attestation_fields() -> None:
    protocol = _protocol()
    valid = _passing_trace_campaign(protocol)
    payload = valid.artifact_receipt.model_dump(mode="json")
    payload["receipt_sha256"] = None
    payload["artifact_readback_verified"] = True
    payload["source_run_sha256"] = _sha("0")
    with pytest.raises(ValidationError, match="Extra inputs"):
        CampaignArtifactReceipt.model_validate(payload)


def test_agent_promotion_remains_blocked_without_full_safety_campaign() -> None:
    protocol = _protocol()
    ledger = build_experiment_claim_ledger(
        protocol,
        ClaimGateEvidence(
            campaigns=(_passing_trace_campaign(protocol), _passing_synthetic_campaign(protocol))
        ),
        ledger_id="missing-safety-campaign",
        generated_at=NOW,
    )
    by_id = {claim.claim_id: claim for claim in ledger.claims}
    assert by_id["adaptive-agent-efficiency"].status == ClaimStatus.BLOCKED
    assert by_id["practitioner-agent-assistance"].status == ClaimStatus.BLOCKED


def test_study_contract_schemas_are_versioned_strict_and_deterministic() -> None:
    rendered = render_usefulness_study_schemas()
    assert rendered == render_usefulness_study_schemas()
    expected = {
        "UsefulnessStudyProtocol.schema.json",
        "StudyCaseManifest.schema.json",
        "StudyMethodArm.schema.json",
        "AgentRetrievalObservation.schema.json",
        "PractitionerSession.schema.json",
        "ConventionalPackEquivalenceReceipt.schema.json",
        "RetainedReadbackBlob.schema.json",
        "ReadbackVerifierProvenance.schema.json",
        "WandbCampaignReadback.schema.json",
        "WeaveTraceReadback.schema.json",
        "PreregistrationPublicationReceipt.schema.json",
        "CampaignEvidenceArtifact.schema.json",
        "CampaignArtifactReceipt.schema.json",
        "VerifiedCampaignEvidence.schema.json",
        "ReviewerHandoffRecord.schema.json",
        "PractitionerPublicProjection.schema.json",
        "ReviewerHandoffPublicProjection.schema.json",
        "AdjudicatedOutcome.schema.json",
        "PairedEffectEstimate.schema.json",
        "ShadowProjectRecord.schema.json",
        "ShadowProjectPublicProjection.schema.json",
        "ExperimentClaimLedger.schema.json",
        "index.json",
    }
    assert set(rendered) == expected
    assert '"additionalProperties": false' in rendered["UsefulnessStudyProtocol.schema.json"]
    assert (
        '"$schema": "https://json-schema.org/draft/2020-12/schema"'
        in rendered["ExperimentClaimLedger.schema.json"]
    )
