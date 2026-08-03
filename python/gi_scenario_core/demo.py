"""Create the deterministic, offline guided-example project."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .concept import generate_concept_model
from .contracts import (
    CandidateSlot,
    ConceptModelRecipe,
    CostBasis,
    DataAsset,
    ModelReadiness,
    ProjectBundle,
    ReadinessLevel,
    StormCase,
)
from .repository import ProjectRepository


def _teaching_lid_controls() -> str:
    return """
[LID_CONTROLS]
;; Curated teaching controls only; not construction specifications.
DemoBio  BC
DemoBio  SURFACE  150  0.15  0.25  1.0  5
DemoBio  SOIL     450  0.45  0.20  0.10  25  10  3.5
DemoBio  STORAGE  300  0.75  10  0
DemoBio  DRAIN    0  0.5  0  0
DemoPave  PP
DemoPave  SURFACE  25  0.10  0.10  1.0  5
DemoPave  PAVEMENT  100  0.15  0.20  150  0
DemoPave  STORAGE  250  0.75  10  0
DemoPave  DRAIN    0  0.5  0  0
"""


def create_demo_project(destination: str | Path) -> dict[str, Any]:
    destination = Path(destination).resolve()
    recipe = ConceptModelRecipe.model_validate(
        {
            "title": "Willow Creek GI teaching project",
            "study_area": {
                "name": "Willow Creek synthetic study area",
                "boundary": [
                    [0, 0],
                    [1200, 0],
                    [1200, 600],
                    [0, 600],
                    [0, 0],
                ],
                "crs": "LOCAL:METERS",
            },
            "outlets": [
                {
                    "outlet_id": "O1",
                    "x": 1200,
                    "y": 300,
                    "invert_elevation": 95,
                    "confirmed": True,
                }
            ],
            "subcatchment_count": 4,
            "flow_units": "CMS",
            "coordinate_units": "meters",
            "assumptions": {
                "impervious_percent": 58,
                "pipe_diameter": 0.75,
                "pipe_roughness": 0.013,
            },
            "seed": 20260802,
        }
    )
    generated = generate_concept_model(recipe)
    model_text = generated.model_text.replace(
        "\n[REPORT]\n", _teaching_lid_controls() + "\n[REPORT]\n"
    )
    repository = ProjectRepository(destination)
    try:
        project = repository.create_project(
            title=recipe.title,
            readiness=ReadinessLevel.DEMO,
            state={
                "workflow": "guided_example",
                "current_step": "check_model",
                "notice": "Synthetic teaching model; assumptions are intentionally visible.",
            },
        )
        source_directory = destination / "sources"
        model_path = source_directory / "willow-creek-demo.inp"
        model_path.write_text(model_text, encoding="utf-8", newline="\n")
        model_sha = hashlib.sha256(model_text.encode("utf-8")).hexdigest()
        recipe_path = destination / "project-recipe.json"
        recipe_path.write_text(
            json.dumps(recipe.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        candidates = [
            CandidateSlot(
                slot_id=f"slot_s{index}_bio",
                subcatchment_id=f"S{index}",
                lid_control="DemoBio",
                lid_type="BC",
                max_additional_units=4,
                unit_area=250,
                surface_width=12.5,
                from_impervious_percent=75,
                feasibility_basis="Synthetic teaching opportunity; inspect all assumptions.",
                engineer_approved=True,
            )
            for index in range(1, 5)
        ]
        candidates_path = destination / "candidate-slots.json"
        candidates_path.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in candidates],
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        costs = CostBasis(
            unit_capex={item.slot_id: 12_500 for item in candidates},
            annual_om={item.slot_id: 250 for item in candidates},
            price_vintage="Synthetic 2026 teaching values",
            source="GI Scenario Studio guided-example fixture; not for professional use",
            confirmed=True,
        )
        cost_path = destination / "teaching-cost-basis.json"
        cost_path.write_text(
            json.dumps(costs.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        storm = StormCase(
            storm_id="storm_demo",
            name="Illustrative two-hour storm",
            model_path=str(model_path.relative_to(destination)),
            role="teaching",
            authority_status="user_defined",
            source="GI Scenario Studio synthetic fixture",
            duration_hours=2,
            initial_conditions="Illustrative dry initial conditions",
            boundary_assumptions="Free outfall; not site-specific",
        )
        bundle = ProjectBundle(
            project_id=project["project_id"],
            title=recipe.title,
            base_model_path=str(model_path.relative_to(destination)),
            storm_cases=[storm],
            dependencies=[
                DataAsset(
                    path=str(model_path.relative_to(destination)),
                    role="swmm_model",
                    source="self-authored synthetic fixture",
                    license="CC0-1.0",
                    vintage="2026",
                    crs="LOCAL:METERS",
                    sha256=model_sha,
                    attribution="GI Scenario Studio contributors",
                )
            ],
            readiness=ModelReadiness(level=ReadinessLevel.DEMO),
        )
        bundle_path = destination / "project.json"
        bundle_path.write_text(
            json.dumps(bundle.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "project": project,
            "directory": str(destination),
            "model_path": str(model_path),
            "model_sha256": model_sha,
            "candidate_count": len(candidates),
            "files": [
                str(bundle_path),
                str(recipe_path),
                str(model_path),
                str(candidates_path),
                str(cost_path),
            ],
        }
    finally:
        repository.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="gi-scenario-demo", help="Demo project directory")
    args = parser.parse_args(argv)
    result = create_demo_project(args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
