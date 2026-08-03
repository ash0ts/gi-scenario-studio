"""Create the deterministic, offline Cedar Trace guided-example project.

The guided project and the exact-result UI projection share the definitions in
this module.  In particular, there is one versioned synthetic ``CostBasis``;
hydraulic values never live here and must come from the exact SWMM manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import (
    CandidateSlot,
    CostBasis,
    DataAsset,
    ProjectBundle,
    ProjectKind,
    StormCase,
)
from .repository import ProjectRepository
from .search import design_id, enumerate_vectors
from .swmm import SwmmDocument, tokenize

GUIDED_PROJECT_NAME = "Cedar Trace teaching watershed"
GUIDED_STORY_ID = "cedar-trace-screening-v1"
GUIDED_COST_BASIS_ID = "cedar-trace-synthetic-cost-basis-v1"
GUIDED_NOTICE = (
    "Synthetic teaching model and storms — not for design, permitting, flood-risk "
    "determination, or representation of an existing drainage system."
)
GUIDED_SOURCE_MODEL_SHA256 = "ef119599902439228db710b868a96f4cc6d90f793e92112513c47511b41fc79e"
GUIDED_SEARCH_SEED = 424_242
GUIDED_DESIGN_COUNT = 220

# The exact fixture generator starts from these self-authored storm definitions.
# Keeping them here also lets the offline demo project include one runnable model
# per storm without relying on a network download.
GUIDED_STORM_SPECS: tuple[dict[str, Any], ...] = (
    {
        "storm_id": "synthetic_balanced_1in",
        "name": "Synthetic balanced 1.0 in event",
        "description": (
            "One-hour symmetric teaching event; not derived from an observed or adopted storm."
        ),
        "depth_inches": 1.0,
        "interval_minutes": 5,
        "weights": (1, 2, 3, 4, 5, 6, 6, 5, 4, 3, 2, 1),
    },
    {
        "storm_id": "synthetic_front_loaded_2in",
        "name": "Synthetic front-loaded 2.0 in event",
        "description": "One-hour early-peak stress shape created only for workflow demonstration.",
        "depth_inches": 2.0,
        "interval_minutes": 5,
        "weights": (9, 8, 7, 6, 5, 4, 3, 2, 1, 1, 1, 1),
    },
    {
        "storm_id": "synthetic_long_2_5in",
        "name": "Synthetic long-duration 2.5 in event",
        "description": "Four-hour low-intensity teaching event with a broad middle pulse.",
        "depth_inches": 2.5,
        "interval_minutes": 5,
        "weights": (1,) * 12 + (2,) * 12 + (3,) * 12 + (1,) * 12,
    },
    {
        "storm_id": "synthetic_stress_4in",
        "name": "Synthetic symmetric 4.0 in stress event",
        "description": (
            "Two-hour severe synthetic sensitivity case; not an adopted return-period event."
        ),
        "depth_inches": 4.0,
        "interval_minutes": 5,
        "weights": tuple(range(1, 13)) + tuple(range(12, 0, -1)),
    },
)


def guided_cost_basis() -> CostBasis:
    """Return the sole cost basis used by the Cedar Trace teaching story.

    Values are intentionally synthetic and are confirmed only as stable inputs
    to the demo arithmetic.  They are not a market estimate or bid basis.
    """

    return CostBasis(
        unit_capex={
            "S1_BIO": 20_000.0,
            "S1_PP": 28_000.0,
            "S2_RG": 15_000.0,
            "S2_RB": 2_500.0,
        },
        annual_om={
            "S1_BIO": 600.0,
            "S1_PP": 450.0,
            "S2_RG": 500.0,
            "S2_RB": 80.0,
        },
        replacement_cost={"S2_RB": 1_500.0},
        replacement_years={"S2_RB": [15]},
        analysis_horizon_years=30,
        discount_rate=0.03,
        currency="USD",
        price_vintage="Synthetic 2026 teaching values",
        source=(
            "GI Scenario Studio self-authored CC0 teaching assumptions; "
            "not a bid, estimate, or market source"
        ),
        confirmed=True,
    )


def guided_cost_basis_payload() -> dict[str, Any]:
    """Project the versioned cost basis without creating a second set of values."""

    basis = guided_cost_basis()
    slot_ids = tuple(basis.unit_capex)
    return {
        "schema_version": "2.0",
        "basis_id": GUIDED_COST_BASIS_ID,
        "currency": "synthetic 2026 USD",
        "currency_code": basis.currency,
        "price_vintage": basis.price_vintage,
        "analysis_horizon_years": basis.analysis_horizon_years,
        "real_discount_rate_percent": basis.discount_rate * 100,
        "source": basis.source,
        "confirmed_for_demo_arithmetic_only": basis.confirmed,
        "limitations": [
            "Synthetic assumptions are used only to teach present-value comparison.",
            "The values are not a quantity takeoff, vendor quote, bid, or location-specific estimate.",
        ],
        "unit_costs_thousand_usd": {
            slot_id: {
                "capex": basis.unit_capex[slot_id] / 1_000,
                "annual_om": basis.annual_om.get(slot_id, 0.0) / 1_000,
                "replacement_years": basis.replacement_years.get(slot_id, []),
                "replacement": basis.replacement_cost.get(slot_id, 0.0) / 1_000,
            }
            for slot_id in slot_ids
        },
    }


def present_value_unit_costs_thousand_usd() -> dict[str, float]:
    """Calculate unit present values from the versioned teaching cost basis."""

    basis = guided_cost_basis()
    rate = basis.discount_rate
    horizon = basis.analysis_horizon_years
    annuity = horizon if rate == 0 else (1 - (1 + rate) ** -horizon) / rate
    values: dict[str, float] = {}
    for slot_id, capex in basis.unit_capex.items():
        replacement = sum(
            basis.replacement_cost.get(slot_id, 0.0) / (1 + rate) ** year
            for year in basis.replacement_years.get(slot_id, [])
        )
        values[slot_id] = (
            capex + basis.annual_om.get(slot_id, 0.0) * annuity + replacement
        ) / 1_000
    return values


def guided_candidate_slots() -> list[CandidateSlot]:
    """Return the four bounded, demo-only opportunity slots used by the fixture."""

    basis = "Self-authored CC0 teaching fixture; approved only for this synthetic demo"
    return [
        CandidateSlot(
            slot_id="S1_BIO",
            subcatchment_id="S1",
            lid_control="BIOCELL",
            lid_type="BC",
            base_units=1,
            max_additional_units=4,
            unit_area=200.0,
            surface_width=20.0,
            from_impervious_percent=15.0,
            drain_to="J1",
            feasibility_basis=basis,
            engineer_approved=True,
        ),
        CandidateSlot(
            slot_id="S1_PP",
            subcatchment_id="S1",
            lid_control="PAVEMENT",
            lid_type="PP",
            max_additional_units=3,
            unit_area=500.0,
            surface_width=35.0,
            from_impervious_percent=10.0,
            drain_to="J1",
            feasibility_basis=basis,
            engineer_approved=True,
        ),
        CandidateSlot(
            slot_id="S2_RG",
            subcatchment_id="S2",
            lid_control="RAINGARDEN",
            lid_type="RG",
            max_additional_units=4,
            unit_area=225.0,
            surface_width=20.0,
            from_impervious_percent=20.0,
            route_to_pervious=True,
            drain_to="J2",
            feasibility_basis=basis,
            engineer_approved=True,
        ),
        CandidateSlot(
            slot_id="S2_RB",
            subcatchment_id="S2",
            lid_control="BARREL",
            lid_type="RB",
            max_additional_units=6,
            unit_area=16.0,
            surface_width=4.0,
            from_impervious_percent=5.0,
            drain_to="J2",
            feasibility_basis=basis,
            engineer_approved=True,
        ),
    ]


def guided_design_plan() -> list[dict[str, Any]]:
    """Return the exact deterministic 220-vector teaching run order."""

    slots = guided_candidate_slots()
    zero = tuple(0 for _ in slots)
    universe = [tuple(vector) for vector in enumerate_vectors(slots)]
    universe = [vector for vector in universe if vector != zero]

    def rank(vector: tuple[int, ...]) -> str:
        payload = json.dumps(
            {"seed": GUIDED_SEARCH_SEED, "vector": vector},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    selected = sorted(universe, key=lambda vector: (rank(vector), vector))[:GUIDED_DESIGN_COUNT]
    return [
        {
            "sequence": sequence,
            "design_id": design_id(vector),
            "vector": list(vector),
            "selection_rank_sha256": rank(vector),
        }
        for sequence, vector in enumerate(selected)
    ]


def guided_story_definition() -> dict[str, Any]:
    """Return non-hydraulic teaching metadata shared by project and UI fixture."""

    return {
        "schema_version": "2.0",
        "story_id": GUIDED_STORY_ID,
        "project_name": GUIDED_PROJECT_NAME,
        "audience": "Stormwater engineers learning a transparent GI alternatives-screening workflow.",
        "decision_brief": (
            "Decide which Cedar Trace GI concept, if any, should advance to field and "
            "site-feasibility review."
        ),
        "carry_forward_action": (
            "Advance a screening candidate only to drainage confirmation, field reconnaissance, "
            "utility review, soils and infiltration testing, maintenance review, and constructability review."
        ),
        "rules": [
            {
                "rule_id": "aggregate_volume_reduction",
                "label": "Four-storm OUT1 volume reduction",
                "metric": "four_storm_outfall_volume_reduction_percent",
                "operator": "at_least",
                "threshold": 6.0,
                "units": "percent",
                "scope": "Sum of OUT1 event volume across the four selected synthetic storms.",
            },
            {
                "rule_id": "present_value_cost_cap",
                "label": "Synthetic present-value screening cost",
                "metric": "present_value_cost_thousand_usd",
                "operator": "at_most",
                "threshold": 200.0,
                "units": "thousand USD",
                "cost_basis_id": GUIDED_COST_BASIS_ID,
            },
            {
                "rule_id": "no_outfall_peak_increase",
                "label": "No OUT1 peak increase in a selected storm",
                "metric": "worst_storm_outfall_peak_delta_cfs",
                "operator": "at_most",
                "threshold": 0.0,
                "units": "cfs",
                "scope": "Checked separately against the matching baseline for every selected storm.",
            },
            {
                "rule_id": "solver_qa",
                "label": "Accepted exact-run QA",
                "metric": "all_storm_runs_qa_status",
                "operator": "equals",
                "threshold": "accepted",
                "units": "status",
            },
        ],
        "non_goals": [
            "Do not infer calibration, field validation, regulatory compliance, or flood-risk reduction.",
            "Do not treat synthetic costs as a bid or location-specific estimate.",
            "Do not treat nondominance among evaluated alternatives as proof of a global optimum.",
        ],
        "teaching_alternatives": [
            {
                "alternative_id": "ALT-017",
                "design_sequence": 16,
                "expected_vector": [0, 0, 1, 6],
                "role": "low_cost_caution",
                "label": "Low-cost caution",
                "why": (
                    "Shows that a low-cost nondominated point can still miss the decision target "
                    "and worsen a storm-specific peak."
                ),
            },
            {
                "alternative_id": "ALT-207",
                "design_sequence": 206,
                "expected_vector": [0, 3, 2, 6],
                "role": "carry_forward_candidate",
                "label": "Carry-forward candidate",
                "why": "Meets all four synthetic teaching rules without implying design approval.",
            },
            {
                "alternative_id": "ALT-109",
                "design_sequence": 108,
                "expected_vector": [1, 3, 2, 6],
                "role": "stronger_over_budget",
                "label": "Stronger, over budget",
                "why": "Makes the marginal performance-versus-cost tradeoff visible.",
            },
            {
                "alternative_id": "ALT-014",
                "design_sequence": 13,
                "expected_vector": [4, 3, 4, 5],
                "role": "observed_performance_extreme",
                "label": "Observed performance extreme",
                "why": (
                    "Shows that the strongest observed volume reduction in the evaluated set is "
                    "not automatically the practical screening choice."
                ),
            },
        ],
        "teaching_selection_policy": (
            "A declared pedagogical shortlist applied to existing exact results. Selection is "
            "result-aware for explanation and is not part of the search benchmark method."
        ),
        "execution_modes": {
            "default": "verified_replay",
            "verified_replay": {
                "availability": "provided_by_bundled_exact_fixture",
                "executes_solver": False,
                "description": (
                    "Read integrity-checked exact-result records and retained artifacts without "
                    "executing SWMM. This does not establish calibration or reference matching."
                ),
            },
            "optional_rerun": {
                "availability": "conditional_on_configured_pinned_solver",
                "executes_solver": True,
                "description": (
                    "Create new isolated evidence only when the host supplies the required "
                    "digest-pinned SWMM 5.2.4 executable."
                ),
            },
        },
        "checkpoints": [
            {
                "sequence": 1,
                "checkpoint_id": "decision",
                "title": "Name the decision",
                "question": "What decision is this analysis allowed to support?",
                "learning_outcome": "Restate the screening target, cost cap, guardrail, and next action.",
            },
            {
                "sequence": 2,
                "checkpoint_id": "fitness",
                "title": "Read the model passport",
                "question": "What does the not-assessed Model Fitness Passport leave unproven?",
                "learning_outcome": "Separate solver QA from calibration and real-world model fitness.",
            },
            {
                "sequence": 3,
                "checkpoint_id": "baseline",
                "title": "Inspect the untouched baseline",
                "question": "Did every baseline storm produce an accepted exact run?",
                "learning_outcome": "Check run provenance and continuity findings before comparing designs.",
            },
            {
                "sequence": 4,
                "checkpoint_id": "storms",
                "title": "Confirm storm scope",
                "question": "Which synthetic events are tested, and which regulatory claims are excluded?",
                "learning_outcome": "Treat selected cases as teaching sensitivities, not return-period storms.",
            },
            {
                "sequence": 5,
                "checkpoint_id": "opportunities",
                "title": "Review allowed GI changes",
                "question": "Which inputs are fixed and which integer unit counts may vary?",
                "learning_outcome": "Recognize that the search does not invent LID layer parameters.",
            },
            {
                "sequence": 6,
                "checkpoint_id": "run_plan",
                "title": "Read the exact-run plan",
                "question": "How do four baselines and 220 designs become 884 SWMM jobs?",
                "learning_outcome": "Understand that search chooses runs; SWMM supplies the reported values.",
            },
            {
                "sequence": 7,
                "checkpoint_id": "screen",
                "title": "Apply every rule",
                "question": "Which alternatives pass volume, cost, peak, and QA together?",
                "learning_outcome": "Use the rule matrix before interpreting the Pareto chart.",
            },
            {
                "sequence": 8,
                "checkpoint_id": "adverse_change",
                "title": "Find the adverse change",
                "question": "Why does ALT-017 fail despite its attractive cost-volume position?",
                "learning_outcome": "Inspect the worst storm-specific delta rather than only aggregate metrics.",
            },
            {
                "sequence": 9,
                "checkpoint_id": "evidence",
                "title": "Resolve the evidence",
                "question": "Which exact runs, hashes, assumptions, and limitations support the comparison?",
                "learning_outcome": "Distinguish manifest-backed metrics from locally retained artifact files.",
            },
            {
                "sequence": 10,
                "checkpoint_id": "next_action",
                "title": "Choose the next engineering action",
                "question": "What must happen before a screening candidate can inform design?",
                "learning_outcome": "Carry the concept into field, geotechnical, utility, O&M, and constructability review.",
            },
        ],
        "claim_language": {
            "allowed": [
                "Direct SWMM 5.2.4 result under the documented synthetic assumptions.",
                "Nondominated among the exact alternatives evaluated for the stated objectives.",
                "Meets the teaching screening rules across the four selected synthetic storm cases.",
                "Candidate to carry forward for site and feasibility review.",
            ],
            "avoid": [
                "Validated or calibrated model",
                "Return-period or regulatory design storm",
                "Reduces real-world flood risk",
                "No adverse impacts",
                "Best or globally optimal design",
                "Accurate cost estimate",
            ],
        },
    }


def _storm_manifest(storm: dict[str, Any]) -> dict[str, Any]:
    interval_minutes = int(storm["interval_minutes"])
    weights = tuple(float(value) for value in storm["weights"])
    interval_hours = interval_minutes / 60
    scale = float(storm["depth_inches"]) / (sum(weights) * interval_hours)
    intensities = [value * scale for value in weights]
    return {
        "storm_id": storm["storm_id"],
        "name": storm["name"],
        "synthetic": True,
        "authority_status": "user_defined",
        "source": "Self-authored CC0 teaching hyetograph; no regulatory authority",
        "description": storm["description"],
        "interval_minutes": interval_minutes,
        "duration_hours": len(weights) * interval_hours,
        "depth_inches": storm["depth_inches"],
        "represented_depth_inches": sum(intensities) * interval_hours,
        "intensities_inches_per_hour": intensities,
    }


def guided_storms() -> list[dict[str, Any]]:
    """Return public, JSON-compatible metadata for the four teaching storms."""

    return [_storm_manifest(storm) for storm in GUIDED_STORM_SPECS]


def _clock(minutes_after_midnight: int) -> str:
    hours, minutes = divmod(minutes_after_midnight, 60)
    return f"{hours:02d}:{minutes:02d}"


def _materialize_storm(base_model: str, storm: dict[str, Any]) -> bytes:
    document = SwmmDocument.parse(base_model)
    section = document.require_section("TIMESERIES")
    first_target_index: int | None = None
    retained: list[str] = []
    for line in section.lines:
        tokens, _ = tokenize(line)
        if tokens and tokens[0].casefold() == "design_storm":
            if first_target_index is None:
                first_target_index = len(retained)
            continue
        retained.append(line)
    if first_target_index is None:
        first_target_index = len(retained)

    rows = [
        f"{'DESIGN_STORM':<24} 01/01/2026  "
        f"{_clock(index * int(storm['interval_minutes']))}  {intensity:.9f}"
        for index, intensity in enumerate(_storm_manifest(storm)["intensities_inches_per_hour"])
    ]
    rows.append(
        f"{'DESIGN_STORM':<24} 01/01/2026  "
        f"{_clock(len(storm['weights']) * int(storm['interval_minutes']))}  0.000000000"
    )
    section.lines = retained[:first_target_index] + rows + retained[first_target_index:]
    document.require_section("TITLE").lines.append(
        f";;Storm case: {storm['name']} — SYNTHETIC / NOT AN ADOPTED EVENT"
    )
    return document.to_bytes()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def create_demo_project(
    destination: str | Path,
    *,
    existing_project_id: str | None = None,
) -> dict[str, Any]:
    """Create an offline Cedar Trace project without executing or inventing results."""

    destination = Path(destination).resolve()
    source_model = _CEDAR_TRACE_REFERENCE_MODEL.encode("utf-8")
    model_sha = hashlib.sha256(source_model).hexdigest()
    if model_sha != GUIDED_SOURCE_MODEL_SHA256:
        raise RuntimeError("embedded Cedar Trace source model does not match the exact fixture")

    repository = ProjectRepository(destination)
    try:
        story = guided_story_definition()
        state = {
            "workflow": "guided_example",
            "current_step": "decision",
            "notice": GUIDED_NOTICE,
            "story_id": GUIDED_STORY_ID,
            "result_mode": "verified_replay",
            "decision_rules": story["rules"],
        }
        if existing_project_id is None:
            project = repository.create_project(
                title=GUIDED_PROJECT_NAME,
                project_kind=ProjectKind.DEMO,
                state=state,
            )
        else:
            project = repository.get_project(existing_project_id)
            if project["project_kind"] != ProjectKind.DEMO.value:
                raise ValueError("an existing guided project must use the demo project kind")
            project = repository.set_project_state(existing_project_id, state)
        source_directory = destination / "sources"
        model_path = source_directory / "cedar-trace-reference.inp"
        model_path.write_bytes(source_model)

        assets = [
            DataAsset(
                path=str(model_path.relative_to(destination)),
                role="swmm_source_model",
                source="self-authored Cedar Trace CC0 teaching fixture",
                license="CC0-1.0",
                vintage="0.1.0",
                crs="LOCAL:FEET",
                sha256=model_sha,
                attribution="GI Scenario Studio contributors",
            )
        ]
        storm_cases: list[StormCase] = []
        storm_directory = source_directory / "storms"
        storm_directory.mkdir(parents=True, exist_ok=True)
        for storm_spec, storm in zip(GUIDED_STORM_SPECS, guided_storms(), strict=True):
            storm_path = storm_directory / f"{storm['storm_id']}.inp"
            storm_bytes = _materialize_storm(_CEDAR_TRACE_REFERENCE_MODEL, storm_spec)
            storm_path.write_bytes(storm_bytes)
            storm_sha = hashlib.sha256(storm_bytes).hexdigest()
            relative_storm_path = str(storm_path.relative_to(destination))
            storm_cases.append(
                StormCase(
                    storm_id=storm["storm_id"],
                    name=storm["name"],
                    model_path=relative_storm_path,
                    role="teaching_screening",
                    authority_status="user_defined",
                    source=storm["source"],
                    source_version=GUIDED_STORY_ID,
                    duration_hours=storm["duration_hours"],
                    initial_conditions="Synthetic dry initial conditions",
                    boundary_assumptions="Free OUT1 outfall; not site-specific",
                )
            )
            assets.append(
                DataAsset(
                    path=relative_storm_path,
                    role="synthetic_storm_model",
                    source=storm["source"],
                    license="CC0-1.0",
                    vintage=GUIDED_STORY_ID,
                    crs="LOCAL:FEET",
                    sha256=storm_sha,
                    attribution="GI Scenario Studio contributors",
                )
            )

        candidates = guided_candidate_slots()
        candidates_path = destination / "candidate-slots.json"
        _write_json(candidates_path, [item.model_dump(mode="json") for item in candidates])

        cost_path = destination / "cost-basis.json"
        _write_json(cost_path, guided_cost_basis_payload())

        story_path = destination / "guided-story.json"
        _write_json(story_path, story)

        run_plan_path = destination / "guided-run-plan.json"
        _write_json(
            run_plan_path,
            {
                "schema_version": "2.0",
                "seed": GUIDED_SEARCH_SEED,
                "design_count": GUIDED_DESIGN_COUNT,
                "design_order": guided_design_plan(),
            },
        )

        bundle = ProjectBundle(
            project_id=project["project_id"],
            title=GUIDED_PROJECT_NAME,
            base_model_path=str(model_path.relative_to(destination)),
            storm_cases=storm_cases,
            dependencies=assets,
            project_kind=ProjectKind.DEMO,
            model_revision_id="revision_cedar_trace_source_v2",
            model_fitness_passport_id=None,
            limitations=[
                GUIDED_NOTICE,
                "No observed event, calibration record, or approved reference report was matched.",
                "The bundled exact results belong only to the unchanged guided fixture inputs.",
            ],
        )
        bundle_path = destination / "project.json"
        _write_json(bundle_path, bundle.model_dump(mode="json"))
        return {
            "project": project,
            "directory": str(destination),
            "model_path": str(model_path),
            "model_sha256": model_sha,
            "candidate_count": len(candidates),
            "storm_count": len(storm_cases),
            "story_id": GUIDED_STORY_ID,
            "cost_basis_id": GUIDED_COST_BASIS_ID,
            "files": [
                str(bundle_path),
                str(model_path),
                *(str(destination / case.model_path) for case in storm_cases),
                str(candidates_path),
                str(cost_path),
                str(story_path),
                str(run_plan_path),
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


_CEDAR_TRACE_REFERENCE_MODEL = """[TITLE]
;;Project Title/Notes
GI Scenario Studio CC0 teaching network — NOT FOR DESIGN

[OPTIONS]
FLOW_UNITS           CFS
INFILTRATION         GREEN_AMPT
FLOW_ROUTING         DYNWAVE
START_DATE           01/01/2026
START_TIME           00:00:00
REPORT_START_DATE    01/01/2026
REPORT_START_TIME    00:00:00
END_DATE             01/01/2026
END_TIME             06:00:00
SWEEP_START          01/01
SWEEP_END            12/31
DRY_DAYS             0
REPORT_STEP          00:05:00
WET_STEP             00:01:00
DRY_STEP             01:00:00
ROUTING_STEP         00:00:15

[RAINGAGES]
;;Name           Format    Interval SCF      Source
GAGE              INTENSITY 0:05     1.0      TIMESERIES DESIGN_STORM

[SUBCATCHMENTS]
;;Name           Rain Gage        Outlet          Area     %Imperv  Width    %Slope   CurbLen  SnowPack
S1                GAGE              J1              2.00     65       350      1.5      0
S2                GAGE              J2              2.50     45       420      1.2      0

[SUBAREAS]
;;Subcatchment   N-Imperv N-Perv   S-Imperv S-Perv   PctZero  RouteTo  PctRouted
S1                0.014    0.20     0.05     0.10     25       OUTLET
S2                0.014    0.20     0.05     0.10     25       OUTLET

[INFILTRATION]
;;Subcatchment   Suction  HydCon   IMDmax
S1                3.5      0.5      0.25
S2                3.5      0.5      0.25

[JUNCTIONS]
;;Name           Elevation  MaxDepth   InitDepth  SurDepth   Aponded
J1                100.0      5.0        0          0          0
J2                98.0       5.0        0          0          0

[OUTFALLS]
;;Name           Elevation  Type       Stage Data       Gated    Route To
OUT1              95.0       FREE                        NO

[CONDUITS]
;;Name           From Node  To Node    Length   Roughness  InOffset OutOffset InitFlow MaxFlow
C1                J1         J2         500      0.013      0        0         0        0
C2                J2         OUT1       600      0.013      0        0         0        0

[XSECTIONS]
;;Link           Shape       Geom1      Geom2     Geom3     Geom4     Barrels  Culvert
C1                CIRCULAR    1.5        0         0         0         1
C2                CIRCULAR    2.0        0         0         0         1

[LID_CONTROLS]
;;Name           Type/Layer Parameters
BIOCELL            BC
BIOCELL            SURFACE    6        0.1      0.15     1        5
BIOCELL            SOIL       18       0.45     0.20     0.10     0.5      10       3.5
BIOCELL            STORAGE    12       0.75     0.5      0
BIOCELL            DRAIN      0.25     0.5      0        6
RAINGARDEN         RG
RAINGARDEN         SURFACE    6        0.1      0.15     1        5
RAINGARDEN         SOIL       18       0.45     0.20     0.10     0.5      10       3.5
GREENROOF          GR
GREENROOF          SURFACE    3        0.1      0.15     1        5
GREENROOF          SOIL       4        0.45     0.20     0.10     0.5      10       3.5
GREENROOF          DRAINMAT   3        0.5      0.1
TRENCH             IT
TRENCH             SURFACE    6        0.1      0.15     1        5
TRENCH             STORAGE    24       0.40     0.5      0
TRENCH             DRAIN      0.25     0.5      0        6
PAVEMENT           PP
PAVEMENT           SURFACE    2        0.1      0.15     1        5
PAVEMENT           PAVEMENT   6        0.15     0.10     100      0
PAVEMENT           STORAGE    12       0.75     0.5      0
PAVEMENT           DRAIN      0.25     0.5      0        6
BARREL             RB
BARREL             STORAGE    48       0.90     0        0
BARREL             DRAIN      0.10     0.5      0        0
ROOFDISC           RD
ROOFDISC           SURFACE    1        0.1      0.15     1        5
SWALE              VS
SWALE              SURFACE    6        0.1      0.15     1        5

[LID_USAGE]
;;Subcatchment   LID Process     Number  Area     Width    InitSat  FromImp  ToPerv   RptFile  DrainTo  FromPerv
S1                BIOCELL          1       200      20       0        15       0

[TIMESERIES]
;;Name           Date       Time       Value
DESIGN_STORM                 00:00      0.0
DESIGN_STORM                 00:30      1.2
DESIGN_STORM                 01:00      2.4
DESIGN_STORM                 01:30      1.2
DESIGN_STORM                 02:00      0.0

[REPORT]
INPUT             NO
CONTROLS          NO
SUBCATCHMENTS     ALL
NODES             ALL
LINKS             ALL

[COORDINATES]
;;Node           X-Coord            Y-Coord
J1                1000               1200
J2                1600               900
OUT1              2200               600

[POLYGONS]
;;Subcatchment   X-Coord            Y-Coord
S1                700                1400
S1                1300               1400
S1                1300               900
S1                700                900
S2                1300               1100
S2                2100               1100
S2                2100               500
S2                1300               500

[TAGS]
SUBCATCHMENT S1 DEMO
SUBCATCHMENT S2 DEMO

"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
