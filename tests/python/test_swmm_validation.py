from __future__ import annotations

import pytest
from gi_scenario_core.contracts import (
    EvidenceClaim,
    ExportEligibility,
    FitnessDisposition,
    ModelFitnessCriterion,
    ModelFitnessPassport,
)
from gi_scenario_core.demo import create_demo_project
from gi_scenario_core.swmm import SwmmDocument, SwmmMutationError, tokenize
from gi_scenario_core.validation import (
    validate_model,
)


def test_round_trip_preserves_crlf_unknown_sections_and_comments():
    text = "; preamble\r\n[OPTIONS]\r\nFLOW_UNITS  CFS ; keep\r\n\r\n[VENDOR_X]\r\nA  B\r\n"
    document = SwmmDocument.parse(text)
    assert document.render() == text
    assert document.section_names() == ["OPTIONS", "VENDOR_X"]


def test_tokenizer_preserves_windows_backslashes():
    tokens, _ = tokenize(r'RG1 INTENSITY FILE "C:\storm data\rain.dat"')
    assert tokens[-1] == r"C:\storm data\rain.dat"


def test_from_path_preserves_utf8_bom_bytes(tmp_path):
    raw = b"\xef\xbb\xbf[OPTIONS]\r\nFLOW_UNITS CFS\r\n"
    path = tmp_path / "bom.inp"
    path.write_bytes(raw)
    assert SwmmDocument.from_path(path).to_bytes() == raw


def test_typed_lid_mutation_preserves_comment():
    text = """[SUBCATCHMENTS]
S1 RG O1 1 50 100 1 0
[LID_CONTROLS]
Bio BC
Bio SURFACE 1 0.1 0.1 1 5
[LID_USAGE]
S1 Bio 2 100 10 0 100 0 * * 0 ; existing
"""
    document = SwmmDocument.parse(text)
    change = document.set_lid_unit_count("S1", "Bio", 5)
    assert change.before == "2"
    assert change.after == "5"
    assert "; existing" in document.render()
    assert document.render() != text


def test_add_lid_usage_requires_existing_control_and_subcatchment():
    document = SwmmDocument.parse("[SUBCATCHMENTS]\nS1 RG O1 1 50 100 1 0\n")
    with pytest.raises(SwmmMutationError, match="control"):
        document.add_lid_usage(subcatchment_id="S1", lid_control="Nope", number=1, area=10, width=2)


def test_generated_concept_passes_structural_validation(concept_result):
    report = validate_model(concept_result.model_text)
    assert report.blocking_count == 0
    assert report.structurally_runnable


def test_multilayer_lid_control_is_not_treated_as_duplicate(tmp_path):
    result = create_demo_project(tmp_path / "demo")
    with open(result["model_path"], encoding="utf-8") as model_file:
        report = validate_model(model_file.read())
    assert not [issue for issue in report.issues if issue.code == "duplicate_id"]


def test_disconnected_branch_is_detected_without_branch_cache_false_positive(concept_result):
    text = concept_result.model_text.replace("C1  J1  J2", "C1  J1  J2").replace(
        "C2  J2  J3", "C2  J2  J1"
    )
    report = validate_model(text)
    stranded = {
        issue.object_id for issue in report.issues if issue.code == "node_without_outfall_path"
    }
    assert {"J1", "J2"}.issubset(stranded)


def _fitness_evidence() -> EvidenceClaim:
    return EvidenceClaim(
        claim_id="claim_model_checks",
        project_id="project_1",
        claim_type="fitness",
        statement="Structural model checks completed without a blocking finding.",
        evidence_ids=["validation:structural"],
    )


def test_fit_for_stated_use_requires_every_criterion_to_pass(concept_result):
    assert validate_model(concept_result.model_text).structurally_runnable
    claim = _fitness_evidence()
    with pytest.raises(ValueError, match="every criterion to pass"):
        ModelFitnessPassport(
            project_id="project_1",
            model_revision_id="revision_1",
            intended_use="GI alternatives screening",
            disposition=FitnessDisposition.FIT_FOR_STATED_USE,
            criteria=[
                ModelFitnessCriterion(
                    criterion_id="topology",
                    label="Topology",
                    status="review",
                    finding="Outlet routing still requires engineer review.",
                    evidence_claim_ids=[claim.claim_id],
                )
            ],
            evidence_claims=[claim],
            reviewer="A. Engineer",
            reviewer_attested=True,
        )


def test_fit_for_stated_use_requires_attested_evidence():
    with pytest.raises(ValueError, match="attested reviewer evidence"):
        ModelFitnessPassport(
            project_id="project_1",
            model_revision_id="revision_1",
            intended_use="GI alternatives screening",
            disposition=FitnessDisposition.FIT_FOR_STATED_USE,
            criteria=[
                ModelFitnessCriterion(
                    criterion_id="topology",
                    label="Topology",
                    status="pass",
                    finding="Reviewed.",
                )
            ],
            reviewer="A. Engineer",
            reviewer_attested=True,
        )


def test_fit_with_limitations_requires_explicit_limitations():
    with pytest.raises(ValueError, match="requires limitations"):
        ModelFitnessPassport(
            project_id="project_1",
            model_revision_id="revision_1",
            intended_use="GI alternatives screening",
            disposition=FitnessDisposition.FIT_WITH_LIMITATIONS,
            reviewer="A. Engineer",
            reviewer_attested=True,
        )


def test_export_eligibility_cannot_be_allowed_with_blockers():
    with pytest.raises(ValueError, match="allowed export"):
        ExportEligibility(
            project_id="project_1",
            analysis_id="analysis_1",
            passport_id="passport_1",
            model_revision_id="revision_1",
            fitness_disposition=FitnessDisposition.NOT_ASSESSED,
            requested_package="learning",
            allowed=True,
            blockers=["Repository evidence is incomplete."],
        )
