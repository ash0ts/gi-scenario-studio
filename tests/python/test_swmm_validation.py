from __future__ import annotations

import pytest
from gi_scenario_core.contracts import (
    ModelReadiness,
    ReadinessLevel,
)
from gi_scenario_core.demo import create_demo_project
from gi_scenario_core.swmm import SwmmDocument, SwmmMutationError, tokenize
from gi_scenario_core.validation import (
    default_attestations,
    promote_readiness,
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


def test_engineer_promotion_requires_complete_exact_checklist(concept_result):
    validation = validate_model(concept_result.model_text)
    attestations = default_attestations()
    for item in attestations[:-1]:
        item.accepted = True
    current = ModelReadiness(level=ReadinessLevel.CONCEPTUAL)
    with pytest.raises(ValueError, match="complete"):
        promote_readiness(
            current,
            ReadinessLevel.ENGINEER_REVIEWED,
            validation=validation,
            attestations=attestations[:-1],
            reviewer="A. Engineer",
        )


def test_engineer_promotion_rejects_unreviewed_assumptions(concept_result):
    validation = validate_model(concept_result.model_text)
    attestations = default_attestations()
    for item in attestations:
        item.accepted = True
    with pytest.raises(ValueError, match="unreviewed"):
        promote_readiness(
            ModelReadiness(level=ReadinessLevel.CONCEPTUAL),
            ReadinessLevel.ENGINEER_REVIEWED,
            validation=validation,
            provenance=concept_result.provenance,
            attestations=attestations,
            reviewer="A. Engineer",
        )


def test_reference_promotion_requires_successful_comparison(concept_result):
    validation = validate_model(concept_result.model_text)
    attestations = default_attestations()
    for item in attestations:
        item.accepted = True
    reviewed_provenance = [
        item.model_copy(update={"reviewed": True}) for item in concept_result.provenance
    ]
    reviewed = promote_readiness(
        ModelReadiness(level=ReadinessLevel.CONCEPTUAL),
        ReadinessLevel.ENGINEER_REVIEWED,
        validation=validation,
        provenance=reviewed_provenance,
        attestations=attestations,
        reviewer="A. Engineer",
    )
    with pytest.raises(ValueError, match="successful"):
        promote_readiness(
            reviewed,
            ReadinessLevel.REFERENCE_MATCHED,
            validation=validation,
            reference_comparison={"comparison_id": "cmp", "matched": False, "metrics": [{}]},
        )
    matched = promote_readiness(
        reviewed,
        ReadinessLevel.REFERENCE_MATCHED,
        validation=validation,
        reference_comparison={
            "comparison_id": "cmp",
            "matched": True,
            "metrics": [{"passed": True}],
        },
    )
    assert matched.reference_comparison_id == "cmp"
