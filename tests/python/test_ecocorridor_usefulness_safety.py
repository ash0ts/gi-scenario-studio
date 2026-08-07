from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ecocorridor_core.contracts import canonical_sha256
from ecocorridor_core.usefulness_safety import (
    SafetyAttackRecipe,
    SafetyObservationBatch,
    build_safety_attack_catalog,
    build_safety_provider_runner_plan,
    create_safety_observation,
    ingest_safety_observation_batch,
    safety_catalog_json,
    summarize_safety_campaign,
)
from ecocorridor_core.usefulness_study import build_default_synthetic_protocol
from pydantic import ValidationError


def _protocol():
    frozen = {
        name: character * 64
        for name, character in zip(
            (
                "fixtures",
                "rubric",
                "randomization",
                "model_slate",
                "prompts",
                "tools",
                "validators",
                "cost_basis",
                "statistical_analysis",
                "source_commit",
            ),
            "0123456789",
            strict=True,
        )
    }
    return build_default_synthetic_protocol(
        source_commit="a" * 40,
        model_ids=(
            "openai/gpt-oss-120b",
            "gateway/frontier-fixture",
            "gateway/efficient-fixture",
        ),
        frozen_input_sha256s=frozen,
        preregistered_at=datetime(2026, 8, 7, tzinfo=UTC),
        frozen_at=datetime(2026, 8, 7, tzinfo=UTC),
    )


def test_safety_catalog_is_deterministic_complete_and_contains_no_real_locations() -> None:
    first = build_safety_attack_catalog(_protocol())
    second = build_safety_attack_catalog(_protocol())

    assert first == second
    assert len(first) == 1_000
    assert len({item.attack_task_id for item in first}) == 1_000
    assert {item.attack_class for item in first} == set(_protocol().safety_attack_classes)
    assert all(item.synthetic_only and not item.contains_real_location for item in first)
    for attack_class in _protocol().safety_attack_classes:
        variants = [item for item in first if item.attack_class == attack_class]
        assert (
            len(
                {
                    (item.recipe["delivery_pattern"], item.recipe["mutation_pattern"])
                    for item in variants
                }
            )
            == 100
        )
    serialized = safety_catalog_json(first)
    assert "catalog_sha256" in serialized
    assert "observed_failures" not in serialized


def test_safety_recipe_and_observation_tampering_fail_closed() -> None:
    recipe = build_safety_attack_catalog(_protocol())[0]
    payload = recipe.model_dump(mode="json")
    payload["variant_index"] = 99
    with pytest.raises(ValidationError, match="task identity differs"):
        SafetyAttackRecipe.model_validate(payload)

    with pytest.raises(ValidationError, match="draft cannot be saved"):
        create_safety_observation(
            observation_id="obs-1",
            attack_task_id=recipe.attack_task_id,
            recipe_sha256=recipe.recipe_sha256,
            attack_class=recipe.attack_class,
            model_id="gateway/model",
            method_arm_id="adaptive-agent",
            observation_origin="PROVIDER_TRACE_READBACK",
            observed_at=datetime(2026, 8, 7, tzinfo=UTC),
            provider_invoked=True,
            prevented_before_model=False,
            attempted_by_model=True,
            rejected_after_model=False,
            incorrectly_released=False,
            draft_saved=True,
            exact_trace_readback=False,
            prohibited_claim_detected=False,
            sensitive_location_leak_detected=False,
            unauthorized_action_attempted=False,
            unauthorized_action_executed=False,
            tampering_attempt_detected=False,
            integrity_failure=False,
            trace_failure=True,
            provider_request_sha256="1" * 64,
            provider_response_sha256="2" * 64,
        )


def test_safety_summary_separates_local_guards_from_provider_behavior() -> None:
    protocol = _protocol()
    recipes = build_safety_attack_catalog(protocol)
    observations = []
    for recipe in recipes:
        pre_model = recipe.boundary == "AUTHORIZATION"
        observations.append(
            create_safety_observation(
                observation_id=f"observation-{recipe.attack_task_id}",
                attack_task_id=recipe.attack_task_id,
                recipe_sha256=recipe.recipe_sha256,
                attack_class=recipe.attack_class,
                model_id="gateway/model",
                method_arm_id="adaptive-agent",
                observation_origin=(
                    "PRE_PROVIDER_GUARD_AUDIT" if pre_model else "PROVIDER_TRACE_READBACK"
                ),
                observed_at=datetime(2026, 8, 7, tzinfo=UTC),
                provider_invoked=not pre_model,
                prevented_before_model=pre_model,
                attempted_by_model=not pre_model,
                rejected_after_model=not pre_model,
                incorrectly_released=False,
                draft_saved=False,
                exact_trace_readback=not pre_model,
                prohibited_claim_detected=False,
                sensitive_location_leak_detected=False,
                unauthorized_action_attempted=(
                    recipe.attack_class == "UNAUTHORIZED_APPROVAL_OR_MUTATION" and not pre_model
                ),
                unauthorized_action_executed=False,
                tampering_attempt_detected=(
                    recipe.attack_class == "TOOL_RESULT_TAMPERING_OR_REORDERING"
                ),
                integrity_failure=False,
                trace_failure=False,
                weave_trace_id=None if pre_model else f"trace-{recipe.attack_task_id}",
                wandb_run_id=None if pre_model else "safety-campaign-run",
                provider_request_sha256=None if pre_model else "1" * 64,
                provider_response_sha256=None if pre_model else "2" * 64,
                local_guard_trigger="ALIAS_OR_REVISION_GUARD" if pre_model else None,
                validation_error_code=None if pre_model else "SYNTHETIC_REJECTION",
            )
        )

    summary = summarize_safety_campaign(protocol, recipes, observations)
    assert summary.total_trials == 1_000
    assert summary.incorrectly_released == 0
    assert summary.saved_drafts == 0
    assert summary.safety_gate_passed is False
    assert summary.provider_backed_coverage_complete is False
    assert summary.exact_trace_integrity is True
    assert summary.prohibited_claims == 0
    assert summary.sensitive_location_leaks == 0
    assert summary.unauthorized_action_attempts == 100
    assert summary.unauthorized_action_executions == 0
    assert summary.tampering_attempts_detected == 100
    assert summary.integrity_failures == 0
    assert summary.trace_failures == 0
    assert all(
        0.029 < item.one_sided_95_percent_release_failure_upper_bound < 0.03
        for item in summary.class_summaries
    )
    prevented = {item.attack_class: item.prevented_before_model for item in summary.class_summaries}
    assert prevented["UNKNOWN_OR_COLLIDING_ALIAS"] == 100
    assert prevented["MIXED_STALE_AND_CURRENT_REVISION"] == 100
    assert prevented["MALFORMED_STRUCTURED_OUTPUT"] == 0
    unauthorized = {
        item.attack_class: item.unauthorized_action_attempts for item in summary.class_summaries
    }
    assert unauthorized["UNAUTHORIZED_APPROVAL_OR_MUTATION"] == 100
    tampering = {
        item.attack_class: item.tampering_attempts_detected for item in summary.class_summaries
    }
    assert tampering["TOOL_RESULT_TAMPERING_OR_REORDERING"] == 100


def test_model_safety_gate_requires_provider_readback_for_every_attack_class() -> None:
    protocol = _protocol()
    recipes = build_safety_attack_catalog(protocol)
    observations = tuple(
        create_safety_observation(
            observation_id=f"provider-{recipe.attack_task_id}",
            attack_task_id=recipe.attack_task_id,
            recipe_sha256=recipe.recipe_sha256,
            attack_class=recipe.attack_class,
            model_id="gateway/model",
            method_arm_id="adaptive-agent",
            observation_origin="PROVIDER_TRACE_READBACK",
            observed_at=datetime(2026, 8, 7, tzinfo=UTC),
            provider_invoked=True,
            prevented_before_model=False,
            attempted_by_model=False,
            rejected_after_model=True,
            incorrectly_released=False,
            draft_saved=False,
            exact_trace_readback=True,
            prohibited_claim_detected=False,
            sensitive_location_leak_detected=False,
            unauthorized_action_attempted=False,
            unauthorized_action_executed=False,
            tampering_attempt_detected=False,
            integrity_failure=False,
            trace_failure=False,
            weave_trace_id=f"trace-{recipe.attack_task_id}",
            wandb_run_id="safety-provider-run",
            provider_request_sha256="1" * 64,
            provider_response_sha256="2" * 64,
            validation_error_code="SAFE_REJECTION",
        )
        for recipe in recipes
    )
    summary = summarize_safety_campaign(protocol, recipes, observations)
    assert summary.provider_backed_coverage_complete is True
    assert summary.safety_gate_passed is True
    assert all(item.provider_backed_trials == 100 for item in summary.class_summaries)
    assert all(item.pre_provider_guard_trials == 0 for item in summary.class_summaries)


def test_safety_summary_rejects_partial_campaigns() -> None:
    protocol = _protocol()
    recipes = build_safety_attack_catalog(protocol)
    with pytest.raises(ValueError, match="partial observation"):
        summarize_safety_campaign(protocol, recipes, ())


def test_provider_plan_is_observation_free_and_strict_ingestion_rejects_partial_rows() -> None:
    protocol = _protocol()
    recipes = build_safety_attack_catalog(protocol)
    plan = build_safety_provider_runner_plan(
        protocol,
        recipes,
        model_id="gateway/model",
        method_arm_id="adaptive-agent",
    )
    assert plan.execution_state == "PLANNED_NOT_EXECUTED"
    assert plan.provider_calls_made == 0
    assert plan.maximum_provider_invocations == 1_000

    recipe = recipes[0]
    observation = create_safety_observation(
        observation_id="strict-ingestion-fixture",
        attack_task_id=recipe.attack_task_id,
        recipe_sha256=recipe.recipe_sha256,
        attack_class=recipe.attack_class,
        model_id=plan.model_id,
        method_arm_id=plan.method_arm_id,
        observation_origin="PRE_PROVIDER_GUARD_AUDIT",
        observed_at=datetime(2026, 8, 7, tzinfo=UTC),
        provider_invoked=False,
        prevented_before_model=True,
        attempted_by_model=False,
        rejected_after_model=False,
        incorrectly_released=False,
        draft_saved=False,
        exact_trace_readback=False,
        prohibited_claim_detected=False,
        sensitive_location_leak_detected=False,
        unauthorized_action_attempted=False,
        unauthorized_action_executed=False,
        tampering_attempt_detected=True,
        integrity_failure=False,
        trace_failure=False,
        local_guard_trigger="STRICT_INGESTION_TEST_GUARD",
    )
    provisional = {
        "schema_version": "ecocorridor-usefulness-safety-observation-batch/v1.0",
        "protocol_sha256": protocol.protocol_sha256,
        "recipe_catalog_sha256": plan.recipe_catalog_sha256,
        "provider_plan_sha256": plan.plan_sha256,
        "model_id": plan.model_id,
        "method_arm_id": plan.method_arm_id,
        "observations": [observation.model_dump(mode="json")],
    }
    batch = SafetyObservationBatch(
        **provisional,
        batch_sha256=canonical_sha256(provisional),
    )
    with pytest.raises(ValueError, match="frozen provider plan"):
        ingest_safety_observation_batch(
            batch.model_dump(mode="json"),
            protocol=protocol,
            recipes=recipes,
            provider_plan=plan,
        )
