from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
from gi_scenario_core.evidence_guide_v2_causal_stage_publication import (
    ANALYZER_ERROR,
    BLOCKED_DISPOSITION,
    EXPECTED_CAUSAL_ATTEMPTS,
    CausalStagePublicationError,
    _diagnose_semantic_triads,
    build_causal_stage_publication,
    validate_blocked_receipt,
)
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
LIVE_R3 = ROOT / "dist" / "evidence-guide-v2-architecture-dev-r3" / "live"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _wal_sha256(path: Path) -> str:
    return _sha256(path) if path.is_file() else hashlib.sha256(b"").hexdigest()


def _triad(*, missing_orchestration: bool) -> list[dict[str, object]]:
    output = []
    original = 3.14
    for condition, value, available in (
        ("evidence_original", original, True),
        ("counterfactual_threshold_crossing", 3.21, True),
        ("evidence_withheld", None, False),
    ):
        receipt = {
            "condition": condition,
            "anchor_sha256": "a" * 64,
            "tool_name": "decision.get_rule_matrix",
            "opaque_evidence_id": "ev_opaque",
            "units": "cfs",
            "evidence_binding": "b" * 64,
            "orchestration_contract_sha256": "c" * 64,
            "triad_identity": {
                "model_id": "model",
                "split": "development",
                "case_id": "case",
                "presentation": 0,
                "architecture_id": "A0xF",
            },
            "original_value": original,
            "intervention_value": value,
            "evidence_available": available,
        }
        output.append(
            {
                "model_id": "model",
                "split": "development",
                "case_id": "case",
                "presentation": 0,
                "arm": "A0xF",
                "scenario_type": "causal_retrieval",
                "stage": "causal_perturbation",
                "ablation_condition": condition,
                "operation_sequence": (
                    []
                    if missing_orchestration
                    else [
                        "repository_tool",
                        "synthesis_call",
                        "deterministic_validation",
                    ]
                ),
                "protocol_shape_id": (
                    None if missing_orchestration else "diagnostic_prose_v1"
                ),
                "semantic_intervention_receipt": receipt,
                "semantic_intervention_receipt_valid": True,
            }
        )
    return output


def test_semantic_diagnostic_detects_unprovable_orchestration() -> None:
    diagnostic = _diagnose_semantic_triads(_triad(missing_orchestration=True))

    assert diagnostic["triad_count"] == 1
    assert diagnostic["dirty_triad_count"] == 1
    assert diagnostic["all_triads_complete"] is True
    assert diagnostic["reason_counts"] == {
        "orchestration_equivalence_unproven": 1
    }
    assert diagnostic["dirty_breakdown"] == [
        {
            "model_id": "model",
            "split": "development",
            "arm": "A0xF",
            "dirty_triads": 1,
        }
    ]


def test_semantic_diagnostic_accepts_complete_matched_metadata() -> None:
    diagnostic = _diagnose_semantic_triads(_triad(missing_orchestration=False))

    assert diagnostic["triad_count"] == 1
    assert diagnostic["clean_triad_count"] == 1
    assert diagnostic["dirty_triad_count"] == 0
    assert diagnostic["all_semantic_interventions_clean"] is True


def test_blocked_receipt_rejects_tampering() -> None:
    receipt = {
        "disposition": BLOCKED_DISPOSITION,
        "publication_created": False,
        "article_publication_permitted": False,
        "release_eligible": False,
        "assistant_release_basis_created": False,
        "production_decision": "DISABLE_LIVE_GUIDE",
        "observed_causal_attempt_count": EXPECTED_CAUSAL_ATTEMPTS,
        "analyzer_gate": {
            "error_code": ANALYZER_ERROR,
            "dirty_triad_count": 1,
        },
    }
    from gi_scenario_core.evidence_guide_v2_campaign import json_sha256

    receipt["receipt_sha256"] = json_sha256(receipt)
    validate_blocked_receipt(receipt)
    tampered = copy.deepcopy(receipt)
    tampered["release_eligible"] = True
    with pytest.raises(CausalStagePublicationError, match="receipt drifted"):
        validate_blocked_receipt(tampered)


@pytest.mark.skipif(
    not (LIVE_R3 / "campaign.sqlite").is_file(),
    reason="sealed live R3 checkpoint is not available in this checkout",
)
def test_live_r3_fails_closed_without_modifying_database(tmp_path: Path) -> None:
    database = LIVE_R3 / "campaign.sqlite"
    wal = LIVE_R3 / "campaign.sqlite-wal"
    before = {
        "database": _sha256(database),
        "wal": _wal_sha256(wal),
    }

    receipt = build_causal_stage_publication(
        LIVE_R3,
        output_dir=tmp_path / "causal-stage-publication",
        repository_root=ROOT,
    )

    after = {
        "database": _sha256(database),
        "wal": _wal_sha256(wal),
    }
    assert before == after
    assert receipt["disposition"] == BLOCKED_DISPOSITION
    assert receipt["observed_causal_attempt_count"] == 1_344
    assert receipt["analyzer_gate"]["matched_triad_count"] == 448
    assert receipt["analyzer_gate"]["dirty_triad_count"] == 121
    assert receipt["analyzer_gate"]["causal_conformance_tables_emitted"] is False
    assert receipt["production_decision"] == "DISABLE_LIVE_GUIDE"
    assert receipt["release_eligible"] is False
    validate_blocked_receipt(receipt)

    panel = Image.open(tmp_path / "causal-stage-publication" / "article-panel.png")
    assert panel.size == (1920, 1080)
    assert panel.mode == "RGB"
    assert (tmp_path / "causal-stage-publication" / "article-panel.csv").is_file()
    assert (tmp_path / "causal-stage-publication" / "dirty-triads.csv").read_text(
        encoding="utf-8"
    ).count("\n") == 122
