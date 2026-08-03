"""Conceptual SWMM model generation with explicit assumptions and provenance."""

from __future__ import annotations

import hashlib
import importlib.util
import math
from collections.abc import Callable
from typing import Any

from .contracts import (
    CONCEPTUAL_NOTICE,
    ConceptModelRecipe,
    Confidence,
    ContractModel,
    ModelObjectProvenance,
    ParameterOrigin,
)


class ConceptGenerationError(RuntimeError):
    pass


class ConceptModelResult(ContractModel):
    recipe_id: str
    engine: str
    model_text: str
    model_sha256: str
    provenance: list[ModelObjectProvenance]
    warnings: list[str]
    notice: str = CONCEPTUAL_NOTICE


def _polygon_area(points: list[tuple[float, float]]) -> float:
    return (
        abs(
            sum(
                points[index][0] * points[index + 1][1] - points[index + 1][0] * points[index][1]
                for index in range(len(points) - 1)
            )
        )
        / 2
    )


def _fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


class LocalConceptGenerator:
    """Produce a small deterministic planning model from a study-area recipe.

    It intentionally creates only a drainage *concept*: a linear tree with
    auditable defaults.  It does not pretend to infer a buried pipe network.
    """

    engine_name = "local_deterministic"

    def generate(self, recipe: ConceptModelRecipe) -> ConceptModelResult:
        boundary = recipe.study_area.boundary
        xs = [point[0] for point in boundary]
        ys = [point[1] for point in boundary]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        if math.isclose(min_x, max_x) or math.isclose(min_y, max_y):
            raise ConceptGenerationError("study area must have non-zero width and height")
        polygon_area = _polygon_area(boundary)
        if polygon_area <= 0:
            raise ConceptGenerationError("study area polygon has zero area")
        n = recipe.subcatchment_count
        outlet = recipe.outlets[0]
        metric = recipe.coordinate_units == "meters"
        area_divisor = 10_000 if metric else 43_560
        area_each = polygon_area / area_divisor / n
        width_each = max(math.sqrt(polygon_area / n), 1.0)

        assumptions = {
            "impervious_percent": float(recipe.assumptions.get("impervious_percent", 45.0)),
            "subcatchment_slope_percent": float(
                recipe.assumptions.get("subcatchment_slope_percent", 1.0)
            ),
            "manning_n_impervious": float(recipe.assumptions.get("manning_n_impervious", 0.015)),
            "manning_n_pervious": float(recipe.assumptions.get("manning_n_pervious", 0.15)),
            "depression_storage_impervious": float(
                recipe.assumptions.get("depression_storage_impervious", 1.27 if metric else 0.05)
            ),
            "depression_storage_pervious": float(
                recipe.assumptions.get("depression_storage_pervious", 2.54 if metric else 0.1)
            ),
            "pipe_diameter": float(recipe.assumptions.get("pipe_diameter", 0.6 if metric else 2.0)),
            "pipe_roughness": float(recipe.assumptions.get("pipe_roughness", 0.013)),
            "junction_max_depth": float(
                recipe.assumptions.get("junction_max_depth", 3.0 if metric else 10.0)
            ),
            "outlet_invert": float(
                outlet.invert_elevation
                if outlet.invert_elevation is not None
                else recipe.assumptions.get("outlet_invert", 95.0)
            ),
        }

        # Place conceptual nodes along a line toward the engineer-selected outlet.
        center_y = (min_y + max_y) / 2
        nodes: list[tuple[str, float, float, float]] = []
        for index in range(n):
            fraction = (index + 0.5) / n
            x = min_x + fraction * (max_x - min_x)
            y = center_y
            distance_rank = n - index
            invert = assumptions["outlet_invert"] + distance_rank * (0.2 if metric else 0.65)
            nodes.append((f"J{index + 1}", x, y, invert))

        subcatchments = []
        subareas = []
        infiltration = []
        polygons = []
        for index in range(n):
            sid = f"S{index + 1}"
            junction = nodes[index][0]
            subcatchments.append(
                f"{sid}  RG1  {junction}  {_fmt(area_each)}  "
                f"{_fmt(assumptions['impervious_percent'])}  {_fmt(width_each)}  "
                f"{_fmt(assumptions['subcatchment_slope_percent'])}  0  "
            )
            subareas.append(
                f"{sid}  {_fmt(assumptions['manning_n_impervious'])}  "
                f"{_fmt(assumptions['manning_n_pervious'])}  "
                f"{_fmt(assumptions['depression_storage_impervious'])}  "
                f"{_fmt(assumptions['depression_storage_pervious'])}  25  OUTLET"
            )
            infiltration.append(f"{sid}  75  10  4  7  0")
            left = min_x + index * (max_x - min_x) / n
            right = min_x + (index + 1) * (max_x - min_x) / n
            for px, py in ((left, min_y), (right, min_y), (right, max_y), (left, max_y)):
                polygons.append(f"{sid}  {_fmt(px)}  {_fmt(py)}")

        junction_lines = [
            f"{node_id}  {_fmt(invert)}  {_fmt(assumptions['junction_max_depth'])}  0  0  0"
            for node_id, _, _, invert in nodes
        ]
        coordinates = [f"{node_id}  {_fmt(x)}  {_fmt(y)}" for node_id, x, y, _ in nodes]
        coordinates.append(f"{outlet.outlet_id}  {_fmt(outlet.x)}  {_fmt(outlet.y)}")
        conduits = []
        xsections = []
        for index, (node_id, x, y, _) in enumerate(nodes):
            downstream = nodes[index + 1][0] if index + 1 < n else outlet.outlet_id
            dx = (nodes[index + 1][1] if index + 1 < n else outlet.x) - x
            dy = (nodes[index + 1][2] if index + 1 < n else outlet.y) - y
            length = max(math.hypot(dx, dy), 1.0)
            cid = f"C{index + 1}"
            conduits.append(
                f"{cid}  {node_id}  {downstream}  {_fmt(length)}  "
                f"{_fmt(assumptions['pipe_roughness'])}  0  0  0  0"
            )
            xsections.append(f"{cid}  CIRCULAR  {_fmt(assumptions['pipe_diameter'])}  0  0  0  1")

        model = f"""[TITLE]
;; GI Scenario Studio conceptual planning model
{recipe.title}
;; {CONCEPTUAL_NOTICE}

[OPTIONS]
FLOW_UNITS           {recipe.flow_units}
INFILTRATION         HORTON
FLOW_ROUTING         DYNWAVE
LINK_OFFSETS         DEPTH
MIN_SLOPE            0
ALLOW_PONDING        NO
SKIP_STEADY_STATE    NO
START_DATE           01/01/2020
START_TIME           00:00:00
REPORT_START_DATE    01/01/2020
REPORT_START_TIME    00:00:00
END_DATE             01/01/2020
END_TIME             06:00:00
REPORT_STEP          00:05:00
WET_STEP             00:05:00
DRY_STEP             01:00:00
ROUTING_STEP         00:00:30

[RAINGAGES]
RG1  INTENSITY  0:05  1.0  TIMESERIES  DESIGN_STORM

[SUBCATCHMENTS]
;;Name  RainGage  Outlet  Area  %Imperv  Width  %Slope  CurbLen
{chr(10).join(subcatchments)}

[SUBAREAS]
;;Name  N-Imperv  N-Perv  S-Imperv  S-Perv  PctZero  RouteTo
{chr(10).join(subareas)}

[INFILTRATION]
;;Name  MaxRate  MinRate  Decay  DryTime  MaxInfil
{chr(10).join(infiltration)}

[JUNCTIONS]
;;Name  Elevation  MaxDepth  InitDepth  SurDepth  Aponded
{chr(10).join(junction_lines)}

[OUTFALLS]
;;Name  Elevation  Type  StageData  Gated  RouteTo
{outlet.outlet_id}  {_fmt(assumptions["outlet_invert"])}  FREE  NO

[CONDUITS]
;;Name  FromNode  ToNode  Length  Roughness  InOffset  OutOffset  InitFlow  MaxFlow
{chr(10).join(conduits)}

[XSECTIONS]
;;Link  Shape  Geom1  Geom2  Geom3  Geom4  Barrels
{chr(10).join(xsections)}

[TIMESERIES]
DESIGN_STORM  01/01/2020  00:00  0
DESIGN_STORM  01/01/2020  00:30  {10 if metric else 0.4}
DESIGN_STORM  01/01/2020  01:00  {25 if metric else 1.0}
DESIGN_STORM  01/01/2020  01:30  {10 if metric else 0.4}
DESIGN_STORM  01/01/2020  02:00  0

[COORDINATES]
{chr(10).join(coordinates)}

[POLYGONS]
{chr(10).join(polygons)}

[REPORT]
INPUT  NO
CONTROLS  NO
SUBCATCHMENTS  ALL
NODES  ALL
LINKS  ALL

[TAGS]
;; Object  ID  Tag
{chr(10).join(f"Subcatch  S{i + 1}  conceptual" for i in range(n))}

[END]
"""
        provenance: list[ModelObjectProvenance] = []
        for name, value in assumptions.items():
            origin = (
                ParameterOrigin.MANUAL
                if name in recipe.assumptions
                or (name == "outlet_invert" and outlet.invert_elevation is not None)
                else ParameterOrigin.DEFAULTED
            )
            # Entry origin and review confidence are intentionally independent.
            # Typing a value does not make it observed, verified, or reviewed.
            confidence = Confidence.ASSUMED
            provenance.append(
                ModelObjectProvenance(
                    object_type="model",
                    object_id=recipe.recipe_id,
                    parameter=name,
                    value=value,
                    origin=origin,
                    confidence=confidence,
                    reviewed=False,
                    formula="local deterministic concept generator v1",
                    notes="Conceptual input; verify against survey, GIS, and engineering records.",
                )
            )
        warnings = [
            "The generated drainage network is a conceptual linear tree, not an inferred buried network.",
            "Pipe sizes, inverts, roughness, infiltration, imperviousness, and initial conditions require review.",
            "The illustrative storm must be replaced with an engineer-approved storm case.",
        ]
        if any(not item.confirmed for item in recipe.outlets):
            warnings.append("One or more selected outlets have not been confirmed.")
        return ConceptModelResult(
            recipe_id=recipe.recipe_id,
            engine=self.engine_name,
            model_text=model,
            model_sha256=hashlib.sha256(model.encode("utf-8")).hexdigest(),
            provenance=provenance,
            warnings=warnings,
        )


class SwmmAnywhereAdapter:
    """Optional boundary around SWMManywhere.

    API churn is isolated here.  Packaged builds can inject a tested bridge
    callable; lean/offline builds fail explicitly instead of silently switching
    generation algorithms.
    """

    engine_name = "swmmanywhere"

    def __init__(self, bridge: Callable[[ConceptModelRecipe], str] | None = None) -> None:
        self.bridge = bridge

    @property
    def available(self) -> bool:
        return self.bridge is not None or importlib.util.find_spec("swmmanywhere") is not None

    def generate(self, recipe: ConceptModelRecipe) -> ConceptModelResult:
        if self.bridge is None:
            raise ConceptGenerationError(
                "SWMManywhere is not configured in this build. Install the optional adapter "
                "and supply its tested bridge, or use local_deterministic generation."
            )
        model = self.bridge(recipe)
        if not isinstance(model, str) or "[OPTIONS]" not in model.upper():
            raise ConceptGenerationError("SWMManywhere adapter did not return a SWMM input model")
        return ConceptModelResult(
            recipe_id=recipe.recipe_id,
            engine=self.engine_name,
            model_text=model,
            model_sha256=hashlib.sha256(model.encode("utf-8")).hexdigest(),
            provenance=[],
            warnings=[
                "Synthetic drainage network requires topology, outlet, invert, and parameter review."
            ],
        )


def generate_concept_model(
    recipe: ConceptModelRecipe | dict[str, Any],
    *,
    swmmanywhere_bridge: Callable[[ConceptModelRecipe], str] | None = None,
) -> ConceptModelResult:
    recipe = ConceptModelRecipe.model_validate(recipe)
    if recipe.synthesis_engine == "swmmanywhere":
        return SwmmAnywhereAdapter(swmmanywhere_bridge).generate(recipe)
    return LocalConceptGenerator().generate(recipe)
