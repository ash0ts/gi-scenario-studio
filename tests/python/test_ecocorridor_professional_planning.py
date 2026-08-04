from __future__ import annotations

from pathlib import Path

import pytest
from ecocorridor_core.contracts import (
    ActionCellReplacement,
    ConnectivityRunPlan,
    InterventionKind,
)
from ecocorridor_core.interventions import action_effect_sha256
from ecocorridor_core.professional_validation import (
    validate_professional_bundle,
    validate_run_plan,
)
from ecocorridor_core.project_store import EcoProjectStore
from ecocorridor_core.ridgeway import build_ridgeway_fixture
from ecocorridor_core.rpc_worker import RPCDispatcher


def _codes(findings: object) -> set[str]:
    return {item.code for item in findings}  # type: ignore[union-attr]


def test_cross_contract_validation_covers_effect_bounds_overlap_and_safeguards() -> None:
    fixture = build_ridgeway_fixture()
    bundle = fixture.bundle
    baseline_findings = validate_professional_bundle(bundle)
    assert not [item for item in baseline_findings if item.severity == "BLOCKING"]

    modeled = [item for item in bundle.intervention_options if item.effects]
    first = modeled[0]
    first_effect = first.effects[0]
    outside = first_effect.model_copy(
        update={
            "cells": [
                ActionCellReplacement(
                    row=first_effect.cells[0].row + bundle.assets[0].grid.height,  # type: ignore[union-attr]
                    column=first_effect.cells[0].column,
                    resistance=first_effect.cells[0].resistance,
                )
            ]
        }
    )
    outside = outside.model_copy(update={"sha256": action_effect_sha256(outside)})
    out_of_bounds_option = first.model_copy(update={"effects": [outside, *first.effects[1:]]})
    out_of_bounds_bundle = bundle.model_copy(
        update={
            "intervention_options": [
                out_of_bounds_option if item.option_id == first.option_id else item
                for item in bundle.intervention_options
            ]
        }
    )
    assert "ACTION_EFFECT_CELL_OUT_OF_BOUNDS" in _codes(
        validate_professional_bundle(out_of_bounds_bundle)
    )

    second = next(item for item in modeled[1:] if item.site_id != first.site_id)
    second_effect = next(
        item for item in second.effects if item.scenario_id == first_effect.scenario_id
    )
    overlap = second_effect.model_copy(
        update={
            "cells": [
                ActionCellReplacement(
                    row=first_effect.cells[0].row,
                    column=first_effect.cells[0].column,
                    resistance=second_effect.cells[0].resistance,
                )
            ]
        }
    )
    overlap = overlap.model_copy(update={"sha256": action_effect_sha256(overlap)})
    overlapping_option = second.model_copy(
        update={
            "effects": [
                overlap if item.scenario_id == overlap.scenario_id else item
                for item in second.effects
            ]
        }
    )
    overlapping_bundle = bundle.model_copy(
        update={
            "intervention_options": [
                overlapping_option if item.option_id == second.option_id else item
                for item in bundle.intervention_options
            ],
            "safeguards": [],
        }
    )
    codes = _codes(validate_professional_bundle(overlapping_bundle))
    assert "SEPARATE_INTERVENTION_EFFECTS_OVERLAP" in codes
    assert "SAFEGUARD_COVERAGE_INCOMPLETE" in codes


def test_professional_preflight_stops_before_resolving_duplicate_scientific_ids() -> None:
    bundle = build_ridgeway_fixture().bundle
    duplicate = bundle.resistance_hypotheses[0].model_copy(
        update={"title": "Conflicting duplicate definition"}
    )
    ambiguous = bundle.model_copy(
        update={"resistance_hypotheses": [*bundle.resistance_hypotheses, duplicate]}
    )

    findings = validate_professional_bundle(ambiguous)

    assert len(findings) == 1
    assert findings[0].code == "DUPLICATE_STABLE_ID"
    assert findings[0].severity == "BLOCKING"
    assert "resistance hypothesis IDs must be unique" in findings[0].remediation


@pytest.mark.parametrize(
    ("identity_kind", "expected_label"),
    (("effect", "action effect"), ("basis", "action-effect basis")),
)
def test_professional_preflight_rejects_duplicate_nested_action_evidence_ids(
    identity_kind: str,
    expected_label: str,
) -> None:
    bundle = build_ridgeway_fixture().bundle
    modeled = next(item for item in bundle.intervention_options if len(item.effects) >= 2)
    first, second, *remaining = modeled.effects
    if identity_kind == "effect":
        duplicate = second.model_copy(update={"effect_id": first.effect_id})
    else:
        duplicate = second.model_copy(
            update={"basis": second.basis.model_copy(update={"basis_id": first.basis.basis_id})}
        )
    ambiguous_option = modeled.model_copy(update={"effects": [first, duplicate, *remaining]})
    ambiguous = bundle.model_copy(
        update={
            "intervention_options": [
                ambiguous_option if item.option_id == modeled.option_id else item
                for item in bundle.intervention_options
            ]
        }
    )

    findings = validate_professional_bundle(ambiguous)

    assert len(findings) == 1
    assert findings[0].code == "DUPLICATE_STABLE_ID"
    assert findings[0].severity == "BLOCKING"
    assert f"{expected_label} IDs must be unique" in findings[0].remediation


def test_professional_preflight_requires_reviewed_action_effect_evidence() -> None:
    fixture = build_ridgeway_fixture()
    modeled = next(item for item in fixture.bundle.intervention_options if item.effects)
    effect = modeled.effects[0]
    incomplete_basis = effect.basis.model_copy(
        update={
            "purpose": " ",
            "evidence_source": "",
            "citation": " ",
            "reviewer": "",
            "assumptions": [],
            "limitations": [" "],
        }
    )
    incomplete_effect = effect.model_copy(update={"basis": incomplete_basis})
    incomplete_effect = incomplete_effect.model_copy(
        update={"sha256": action_effect_sha256(incomplete_effect)}
    )
    incomplete_option = modeled.model_copy(
        update={
            "reviewed_by": " ",
            "external_feasibility_evidence": [],
            "effects": [
                incomplete_effect if item.effect_id == effect.effect_id else item
                for item in modeled.effects
            ],
        }
    )
    incomplete_site = next(
        item for item in fixture.bundle.intervention_sites if item.site_id == modeled.site_id
    ).model_copy(update={"reviewed_by": ""})
    bundle = fixture.bundle.model_copy(
        update={
            "intervention_sites": [
                incomplete_site if item.site_id == incomplete_site.site_id else item
                for item in fixture.bundle.intervention_sites
            ],
            "intervention_options": [
                incomplete_option if item.option_id == incomplete_option.option_id else item
                for item in fixture.bundle.intervention_options
            ],
        }
    )

    findings = validate_professional_bundle(bundle)
    codes = _codes(findings)

    assert action_effect_sha256(incomplete_effect) == incomplete_effect.sha256
    assert "ACTION_EFFECT_HASH_MISMATCH" not in codes
    assert "ACTION_EFFECT_BASIS_INCOMPLETE" in codes
    assert "INTERVENTION_OPTION_REVIEW_MISSING" in codes
    assert "INTERVENTION_SITE_REVIEW_MISSING" in codes


def test_intervention_preview_cannot_save_an_undocumented_modeled_effect(
    tmp_path: Path,
) -> None:
    fixture = build_ridgeway_fixture()
    site = fixture.bundle.intervention_sites[0]
    options = [item for item in fixture.bundle.intervention_options if item.site_id == site.site_id]
    modeled = next(
        item
        for item in options
        if item.kind not in {InterventionKind.NO_ACTION, InterventionKind.SURVEY_FIRST}
    )
    first_effect = modeled.effects[0]
    incomplete_basis = first_effect.basis.model_copy(
        update={
            "purpose": "",
            "evidence_source": "",
            "citation": "",
            "reviewer": "",
            "assumptions": [],
        }
    )
    incomplete_effect = first_effect.model_copy(update={"basis": incomplete_basis})
    incomplete_modeled = modeled.model_copy(
        update={
            "reviewed_by": "",
            "external_feasibility_evidence": [],
            "effects": [
                incomplete_effect if item.effect_id == first_effect.effect_id else item
                for item in modeled.effects
            ],
        }
    )
    preview_options = [
        incomplete_modeled if item.option_id == modeled.option_id else item for item in options
    ]
    project = tmp_path / "undocumented-effect"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="PLANNING",
            skeleton={"test": "undocumented modeled effect"},
        )
        store.store_bundle(fixture.bundle)
    finally:
        store.close()

    option_drafts = []
    for option in preview_options:
        payload = option.model_dump(mode="json")
        for effect_payload in payload["effects"]:
            effect_payload.pop("sha256")
        option_drafts.append(payload)
    response = RPCDispatcher(data_root=tmp_path).dispatch(
        "connectivity.intervention.preview",
        {
            "project_directory": str(project),
            "site": site.model_dump(mode="json"),
            "options": option_drafts,
        },
    )["data"]

    assert response["can_save"] is False
    codes = {item["code"] for item in response["findings"]}
    assert "ACTION_EFFECT_BASIS_INCOMPLETE" in codes
    assert "INTERVENTION_OPTION_REVIEW_MISSING" in codes


@pytest.mark.parametrize(
    "grid_change",
    [
        {"crs": "EPSG:26917"},
        {"width": 13},
        {"height": 13},
        {"transform": (25.0, 0.0, 25.0, 0.0, -25.0, 300.0)},
        {"cell_area_m2": 626.0},
    ],
    ids=("crs", "extent-width", "extent-height", "transform", "cell-area"),
)
def test_professional_preflight_rejects_review_mask_grid_identity_drift(
    grid_change: dict[str, object],
) -> None:
    fixture = build_ridgeway_fixture()
    mask = fixture.bundle.review_masks[0]
    mask_asset = next(item for item in fixture.bundle.assets if item.asset_id == mask.asset_id)
    assert mask_asset.grid is not None
    changed_mask_asset = mask_asset.model_copy(
        update={"grid": mask_asset.grid.model_copy(update=grid_change)}
    )
    bundle = fixture.bundle.model_copy(
        update={
            "assets": [
                changed_mask_asset if item.asset_id == mask.asset_id else item
                for item in fixture.bundle.assets
            ]
        }
    )

    findings = validate_professional_bundle(bundle)

    assert "REVIEW_MASK_GRID_MISMATCH" in _codes(findings)
    conflict = next(item for item in findings if item.code == "GRID_ID_GEOMETRY_CONFLICT")
    assert conflict.object_reference == mask.grid_id
    assert "new grid ID" in conflict.remediation


def test_decision_rule_pairs_must_exist_in_every_scoped_scenario() -> None:
    fixture = build_ridgeway_fixture()
    guild_a = next(item for item in fixture.bundle.scenarios if item.guild_id == "target_guild_a")
    guild_b = next(item for item in fixture.bundle.scenarios if item.guild_id == "target_guild_b")
    incompatible = fixture.bundle.decision_rules[0].model_copy(
        update={
            "scenario_ids": [guild_b.scenario_id],
            "pair_ids": [guild_a.included_pair_ids[0]],
        }
    )
    bundle = fixture.bundle.model_copy(
        update={
            "decision_rules": [
                incompatible if item.rule_id == incompatible.rule_id else item
                for item in fixture.bundle.decision_rules
            ]
        }
    )

    findings = validate_professional_bundle(bundle)
    assert "DECISION_RULE_PAIR_SCOPE_INCOMPATIBLE" in _codes(findings)
    finding = next(
        item for item in findings if item.code == "DECISION_RULE_PAIR_SCOPE_INCOMPATIBLE"
    )
    assert finding.object_reference == f"{incompatible.rule_id}:{guild_b.scenario_id}"


def test_decision_brief_rejects_prohibited_ecological_outcomes() -> None:
    fixture = build_ridgeway_fixture()
    decision_brief = fixture.bundle.decision_brief.model_copy(
        update={
            "decision_question": "Which portfolio reduces wildlife-vehicle collisions?",
            "intended_use": "Select a crossing to reduce collisions.",
        }
    )
    bundle = fixture.bundle.model_copy(update={"decision_brief": decision_brief})

    findings = validate_professional_bundle(bundle)
    prohibited = [item for item in findings if item.code == "PROHIBITED_ECOLOGICAL_PURPOSE"]
    assert len(prohibited) == 2
    assert all(item.severity == "BLOCKING" for item in prohibited)


def test_professional_validation_rejects_prohibited_rule_narrative() -> None:
    fixture = build_ridgeway_fixture()
    unsafe = fixture.bundle.decision_rules[0].model_copy(
        update={"rationale": "This guarantees collision reduction and is the optimal corridor."}
    )
    bundle = fixture.bundle.model_copy(
        update={
            "decision_rules": [
                unsafe if item.rule_id == unsafe.rule_id else item
                for item in fixture.bundle.decision_rules
            ]
        }
    )

    prohibited = [
        item
        for item in validate_professional_bundle(bundle)
        if item.code == "PROHIBITED_ECOLOGICAL_PURPOSE"
    ]
    assert [item.object_reference for item in prohibited] == [
        f"decision_rule[{unsafe.rule_id}].rationale"
    ]


def test_effective_bundle_overlays_every_professional_save_record(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    project = tmp_path / "overlay-project"
    store = EcoProjectStore(project)
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="IMPORTED",
            skeleton={"source": "test"},
        )
        store.store_bundle(fixture.bundle)
    finally:
        store.close()

    dispatcher = RPCDispatcher(data_root=tmp_path)
    scenario = fixture.bundle.scenarios[0].model_copy(update={"title": "Reviewed scenario"})
    safeguard = fixture.bundle.safeguards[0].model_copy(
        update={"nonregression_threshold_percent": 1.25}
    )
    review_mask = fixture.bundle.review_masks[0].model_copy(update={"review_trigger_percent": 3.5})
    decision_rule = fixture.bundle.decision_rules[0].model_copy(
        update={"title": "Reviewed pair rule"}
    )
    decision_brief = fixture.bundle.decision_brief.model_copy(
        update={"decision_question": "Which reviewed portfolio advances to field review?"}
    )
    resource_policy = fixture.bundle.resource_policy.model_copy(update={"max_direct_jobs": 1_700})
    calls = (
        ("connectivity.scenario.save", {"scenario": scenario.model_dump(mode="json")}),
        (
            "connectivity.safeguard.save",
            {"safeguard": safeguard.model_dump(mode="json")},
        ),
        (
            "connectivity.review_mask.save",
            {"review_mask": review_mask.model_dump(mode="json")},
        ),
        (
            "connectivity.decision_rule.save",
            {"decision_rule": decision_rule.model_dump(mode="json")},
        ),
        (
            "connectivity.decision_brief.save",
            {"decision_brief": decision_brief.model_dump(mode="json")},
        ),
        (
            "connectivity.resource_policy.save",
            {"resource_policy": resource_policy.model_dump(mode="json")},
        ),
    )
    for method, payload in calls:
        result = dispatcher.dispatch(method, {"project_directory": str(project), **payload})
        assert result["data"]["revision"] == 1

    store = EcoProjectStore(project)
    try:
        effective = store.effective_bundle()
    finally:
        store.close()
    assert (
        next(item for item in effective.scenarios if item.scenario_id == scenario.scenario_id)
        == scenario
    )
    assert effective.safeguards[0] == safeguard
    assert effective.review_masks[0] == review_mask
    assert (
        next(item for item in effective.decision_rules if item.rule_id == decision_rule.rule_id)
        == decision_rule
    )
    assert effective.decision_brief == decision_brief
    assert effective.resource_policy == resource_policy


def test_run_plan_requires_canonical_complete_ensemble_and_no_action_baseline() -> None:
    fixture = build_ridgeway_fixture()
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    plan = ConnectivityRunPlan(
        analysis_id="reviewed-plan",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=len(fixture.bundle.scenarios),
        solver_job_cap=len(fixture.bundle.scenarios),
        search_method="ENUMERATION",
        seed=7,
        engine_manifest_sha256="a" * 64,
    )
    assert not [
        item for item in validate_run_plan(fixture.bundle, plan) if item.severity == "BLOCKING"
    ]

    selected = fixture.portfolios[1]
    invalid = plan.model_copy(
        update={
            "portfolios": [selected],
            "baseline_portfolio_id": selected.portfolio_id,
        }
    )
    assert "BASELINE_PORTFOLIO_NOT_NO_ACTION" in _codes(validate_run_plan(fixture.bundle, invalid))


def test_analysis_model_snapshot_is_immutable_after_project_edits(tmp_path: Path) -> None:
    fixture = build_ridgeway_fixture()
    baseline = next(
        item for item in fixture.portfolios if item.portfolio_id == fixture.baseline_portfolio_id
    )
    plan = ConnectivityRunPlan(
        analysis_id="snapshot-analysis",
        revision_id=fixture.bundle.revision.revision_id,
        scenario_ids=[item.scenario_id for item in fixture.bundle.scenarios],
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=len(fixture.bundle.scenarios),
        solver_job_cap=len(fixture.bundle.scenarios),
        search_method="ENUMERATION",
        seed=8,
        engine_manifest_sha256="b" * 64,
    )
    store = EcoProjectStore(tmp_path / "snapshot-project")
    try:
        store.initialize(
            project_id=fixture.bundle.project_id,
            title=fixture.bundle.title,
            mode="IMPORTED",
            skeleton={"source": "test"},
        )
        store.store_bundle(fixture.bundle)
        store.create_analysis(plan, bundle=fixture.bundle)
        changed = fixture.bundle.decision_brief.model_copy(
            update={"decision_question": "A later, different decision question"}
        )
        store.put_record("decision_brief", changed.brief_id, changed.model_dump(mode="json"))
        assert store.effective_bundle().decision_brief == changed
        assert (
            store.analysis_bundle(plan.analysis_id).decision_brief == fixture.bundle.decision_brief
        )
    finally:
        store.close()
