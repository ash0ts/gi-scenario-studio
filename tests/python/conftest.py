from __future__ import annotations

import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "python"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


@pytest.fixture
def conceptual_recipe() -> dict:
    return {
        "title": "Test watershed",
        "study_area": {
            "name": "Fixture",
            "boundary": [[0, 0], [1000, 0], [1000, 500], [0, 500], [0, 0]],
            "crs": "EPSG:3857",
        },
        "outlets": [
            {
                "outlet_id": "O1",
                "x": 1000,
                "y": 250,
                "invert_elevation": 95,
                "confirmed": True,
            }
        ],
        "subcatchment_count": 4,
        "assumptions": {"impervious_percent": 55},
    }


@pytest.fixture
def concept_result(conceptual_recipe):
    from gi_scenario_core.concept import generate_concept_model

    return generate_concept_model(conceptual_recipe)


@pytest.fixture
def approved_slots():
    return [
        {
            "slot_id": "slot_a",
            "subcatchment_id": "S1",
            "lid_control": "Bio",
            "lid_type": "BC",
            "min_additional_units": 0,
            "max_additional_units": 2,
            "unit_area": 100,
            "surface_width": 10,
            "feasibility_basis": "fixture",
            "engineer_approved": True,
        },
        {
            "slot_id": "slot_b",
            "subcatchment_id": "S2",
            "lid_control": "Pave",
            "lid_type": "PP",
            "min_additional_units": 0,
            "max_additional_units": 3,
            "unit_area": 100,
            "surface_width": 10,
            "feasibility_basis": "fixture",
            "engineer_approved": True,
        },
    ]


@pytest.fixture
def exact_result() -> dict:
    return {
        "run_id": "job_exact_1",
        "analysis_id": "analysis_1",
        "model_revision_id": "revision_1",
        "design_id": "design_1",
        "storm_id": "storm_1",
        "exact": True,
        "metrics": {"flood_volume_m3": 12.5, "peak_flow_cms": 1.2},
        "qa_status": "accepted",
        "input_sha256": "a" * 64,
        "report_sha256": "d" * 64,
        "solver_sha256": "b" * 64,
        "output_sha256": "c" * 64,
        "extractor_version": "test-report-extractor-v2",
        "completed_at": "2026-01-01T00:00:00+00:00",
    }
