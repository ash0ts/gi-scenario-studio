from __future__ import annotations

import pytest
from gi_scenario_core.concept import ConceptGenerationError, generate_concept_model
from gi_scenario_core.contracts import (
    CONCEPTUAL_NOTICE,
    Confidence,
    EvidenceClaim,
    FitnessDisposition,
    ModelFitnessCriterion,
    ModelFitnessPassport,
    ModelRevision,
    ProjectBundle,
    ProjectKind,
    SimulationBudget,
    StudyArea,
    public_schemas,
)
from gi_scenario_core.swmm import SwmmDocument


def test_budget_accounts_for_baseline_and_storm_cross_product():
    assert SimulationBudget(max_solver_jobs=1000).design_capacity(4) == 249
    assert SimulationBudget(max_solver_jobs=5).design_capacity(4) == 0


def test_study_area_requires_closed_ring():
    with pytest.raises(ValueError, match="closed ring"):
        StudyArea(boundary=[(0, 0), (1, 0), (1, 1), (0, 1)], crs="EPSG:3857")


def test_project_kind_does_not_imply_model_fitness():
    bundle = ProjectBundle(
        project_id="project_1",
        title="Imported network",
        base_model_path="network.inp",
        project_kind=ProjectKind.EXISTING,
        model_revision_id="revision_1",
    )
    assert bundle.project_kind == ProjectKind.EXISTING
    assert bundle.model_fitness_passport_id is None


def test_reference_comparison_is_evidence_not_an_ordinal_fitness_level():
    claim = EvidenceClaim(
        claim_id="claim_reference",
        project_id="project_1",
        claim_type="reference_comparison",
        statement="Approved reference results matched stated tolerances.",
        evidence_ids=["comparison:1"],
    )
    passport = ModelFitnessPassport(
        project_id="project_1",
        model_revision_id="revision_1",
        intended_use="GI alternatives screening",
        disposition=FitnessDisposition.FIT_FOR_STATED_USE,
        criteria=[
            ModelFitnessCriterion(
                criterion_id="reference",
                label="Reference comparison",
                status="pass",
                finding="Matched stated tolerances.",
                evidence_claim_ids=[claim.claim_id],
            )
        ],
        evidence_claims=[claim],
        reviewer="A. Engineer",
        reviewer_attested=True,
    )
    assert passport.disposition == FitnessDisposition.FIT_FOR_STATED_USE
    assert passport.evidence_claims[0].claim_type == "reference_comparison"


def test_model_revision_rejects_unverifiable_hashes():
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        ModelRevision(
            project_id="project_1",
            reason="import",
            model_sha256="not-a-digest",
            observability_binding_id="binding_1",
            created_by="bundle.import",
        )


def test_public_json_schemas_are_available():
    schemas = public_schemas()
    assert "CandidateSlot" in schemas
    assert schemas["CandidateSlot"]["type"] == "object"


def test_local_concept_generation_is_deterministic_and_parseable(conceptual_recipe):
    first = generate_concept_model(conceptual_recipe)
    second = generate_concept_model(conceptual_recipe)
    assert first.model_text == second.model_text
    assert first.model_sha256 == second.model_sha256
    assert CONCEPTUAL_NOTICE in first.model_text
    document = SwmmDocument.parse(first.model_text)
    assert {"OPTIONS", "SUBCATCHMENTS", "CONDUITS", "OUTFALLS"}.issubset(document.section_names())


def test_manual_entry_is_not_automatically_reviewed_or_confirmed(concept_result):
    impervious = next(
        item for item in concept_result.provenance if item.parameter == "impervious_percent"
    )
    assert impervious.origin.value == "manually_entered"
    assert impervious.confidence == Confidence.ASSUMED
    assert impervious.reviewed is False


def test_swmmanywhere_adapter_fails_explicitly_without_bridge(conceptual_recipe):
    recipe = dict(conceptual_recipe, synthesis_engine="swmmanywhere")
    with pytest.raises(ConceptGenerationError, match="not configured"):
        generate_concept_model(recipe)


def test_swmmanywhere_bridge_output_is_validated(conceptual_recipe):
    recipe = dict(conceptual_recipe, synthesis_engine="swmmanywhere")
    with pytest.raises(ConceptGenerationError, match="did not return"):
        generate_concept_model(recipe, swmmanywhere_bridge=lambda _: "not a model")
