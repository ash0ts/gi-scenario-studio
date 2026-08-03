"""Structural model validation used as evidence in an independent fitness passport."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Any

from .contracts import (
    ContractModel,
    Severity,
)
from .swmm import SwmmDocument, parse_float


class ModelIssue(ContractModel):
    code: str
    severity: Severity
    message: str
    object_type: str | None = None
    object_id: str | None = None
    section: str | None = None
    remediation: str | None = None


class ModelCheck(ContractModel):
    key: str
    label: str
    status: str
    detail: str


class ValidationReport(ContractModel):
    issues: list[ModelIssue]
    checks: list[ModelCheck]
    section_names: list[str]
    object_counts: dict[str, int]

    @property
    def blocking_count(self) -> int:
        return sum(item.severity == Severity.BLOCKING for item in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(item.severity == Severity.WARNING for item in self.issues)

    @property
    def structurally_runnable(self) -> bool:
        return self.blocking_count == 0


KNOWN_SECTIONS = {
    "TITLE",
    "OPTIONS",
    "EVAPORATION",
    "TEMPERATURE",
    "RAINGAGES",
    "SUBCATCHMENTS",
    "SUBAREAS",
    "INFILTRATION",
    "AQUIFERS",
    "GROUNDWATER",
    "SNOWPACKS",
    "JUNCTIONS",
    "OUTFALLS",
    "DIVIDERS",
    "STORAGE",
    "CONDUITS",
    "PUMPS",
    "ORIFICES",
    "WEIRS",
    "OUTLETS",
    "XSECTIONS",
    "TRANSECTS",
    "LOSSES",
    "CONTROLS",
    "POLLUTANTS",
    "LANDUSES",
    "COVERAGES",
    "LOADINGS",
    "BUILDUP",
    "WASHOFF",
    "TREATMENT",
    "INFLOWS",
    "DWF",
    "PATTERNS",
    "RDII",
    "HYDROGRAPHS",
    "CURVES",
    "TIMESERIES",
    "REPORT",
    "TAGS",
    "MAP",
    "COORDINATES",
    "VERTICES",
    "POLYGONS",
    "SYMBOLS",
    "LABELS",
    "BACKDROP",
    "PROFILES",
    "LID_CONTROLS",
    "LID_USAGE",
    "ADJUSTMENTS",
    "EVENTS",
    "FILES",
    "END",
}

OBJECT_SECTIONS = (
    "SUBCATCHMENTS",
    "JUNCTIONS",
    "OUTFALLS",
    "DIVIDERS",
    "STORAGE",
    "CONDUITS",
    "PUMPS",
    "ORIFICES",
    "WEIRS",
    "OUTLETS",
)


def _issue(
    code: str,
    severity: Severity,
    message: str,
    *,
    section: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    remediation: str | None = None,
) -> ModelIssue:
    return ModelIssue(
        code=code,
        severity=severity,
        message=message,
        section=section,
        object_type=object_type,
        object_id=object_id,
        remediation=remediation,
    )


def validate_model(document: SwmmDocument | str) -> ValidationReport:
    if isinstance(document, str):
        document = SwmmDocument.parse(document)
    issues: list[ModelIssue] = []
    counts = {section: len(list(document.data_rows(section))) for section in OBJECT_SECTIONS}
    counts["LID_CONTROLS"] = len(list(document.data_rows("LID_CONTROLS")))

    for required in ("OPTIONS", "SUBCATCHMENTS", "RAINGAGES"):
        if document.get_section(required) is None:
            issues.append(
                _issue(
                    "missing_section",
                    Severity.BLOCKING,
                    f"Required section [{required}] is missing.",
                    section=required,
                    remediation=f"Add a valid [{required}] section in EPA SWMM.",
                )
            )
    node_sections = ("JUNCTIONS", "OUTFALLS", "DIVIDERS", "STORAGE")
    nodes = {
        row.tokens[0].casefold(): (section, row)
        for section in node_sections
        for row in document.data_rows(section)
    }
    outfalls = {row.tokens[0].casefold() for row in document.data_rows("OUTFALLS")}
    if not outfalls:
        issues.append(
            _issue(
                "missing_outfall",
                Severity.BLOCKING,
                "The drainage network has no outfall.",
                section="OUTFALLS",
                remediation="Add and verify at least one outlet boundary.",
            )
        )

    for section in OBJECT_SECTIONS:
        for duplicate in document.duplicate_ids(section):
            issues.append(
                _issue(
                    "duplicate_id",
                    Severity.BLOCKING,
                    f"{section} contains duplicate identifier {duplicate!r}.",
                    section=section,
                    object_id=duplicate,
                    remediation="Rename or remove the duplicate object.",
                )
            )

    reverse_graph: dict[str, list[str]] = defaultdict(list)
    for section in ("CONDUITS", "PUMPS", "ORIFICES", "WEIRS", "OUTLETS"):
        for row in document.data_rows(section):
            if len(row.tokens) < 3:
                issues.append(
                    _issue(
                        "malformed_link",
                        Severity.BLOCKING,
                        f"{section} row {row.line_index + 1} does not define endpoints.",
                        section=section,
                        remediation="Complete the link definition in EPA SWMM.",
                    )
                )
                continue
            link_id, upstream, downstream = row.tokens[:3]
            reverse_graph[downstream.casefold()].append(upstream.casefold())
            for endpoint, role in ((upstream, "upstream"), (downstream, "downstream")):
                if endpoint.casefold() not in nodes:
                    issues.append(
                        _issue(
                            "unknown_link_node",
                            Severity.BLOCKING,
                            f"Link {link_id!r} references unknown {role} node {endpoint!r}.",
                            section=section,
                            object_type="link",
                            object_id=link_id,
                            remediation="Connect the link to an existing node or add the missing node.",
                        )
                    )

    # Reverse reachability avoids falsely marking an entire explored branch as
    # connected merely because a different branch eventually found an outfall.
    reaches_any_outfall: set[str] = set(outfalls)
    queue = deque(sorted(outfalls))
    while queue:
        downstream = queue.popleft()
        for upstream in reverse_graph.get(downstream, []):
            if upstream not in reaches_any_outfall:
                reaches_any_outfall.add(upstream)
                queue.append(upstream)

    for node_id in nodes:
        if node_id not in reaches_any_outfall:
            original = nodes[node_id][1].tokens[0]
            issues.append(
                _issue(
                    "node_without_outfall_path",
                    Severity.BLOCKING,
                    f"Node {original!r} has no directed path to an outfall.",
                    object_type="node",
                    object_id=original,
                    remediation="Connect the stranded network or confirm the intended outlet.",
                )
            )

    subcatchment_rows = list(document.data_rows("SUBCATCHMENTS"))
    subcatchment_ids = {row.tokens[0].casefold() for row in subcatchment_rows if row.tokens}
    for row in subcatchment_rows:
        if len(row.tokens) < 3:
            issues.append(
                _issue(
                    "malformed_subcatchment",
                    Severity.BLOCKING,
                    f"Subcatchment row {row.line_index + 1} is incomplete.",
                    section="SUBCATCHMENTS",
                )
            )
            continue
        sid, outlet = row.tokens[0], row.tokens[2]
        if outlet.casefold() not in nodes and outlet.casefold() not in subcatchment_ids:
            issues.append(
                _issue(
                    "unknown_subcatchment_outlet",
                    Severity.BLOCKING,
                    f"Subcatchment {sid!r} drains to unknown object {outlet!r}.",
                    section="SUBCATCHMENTS",
                    object_type="subcatchment",
                    object_id=sid,
                    remediation="Assign an existing node or downstream subcatchment.",
                )
            )

    xsections = {row.tokens[0].casefold() for row in document.data_rows("XSECTIONS")}
    for row in document.data_rows("CONDUITS"):
        if not row.tokens:
            continue
        conduit = row.tokens[0]
        if conduit.casefold() not in xsections:
            issues.append(
                _issue(
                    "missing_xsection",
                    Severity.BLOCKING,
                    f"Conduit {conduit!r} has no cross-section.",
                    section="XSECTIONS",
                    object_type="link",
                    object_id=conduit,
                    remediation="Define the conduit shape and dimensions.",
                )
            )
        if len(row.tokens) < 5:
            issues.append(
                _issue(
                    "missing_conduit_roughness",
                    Severity.BLOCKING,
                    f"Conduit {conduit!r} lacks length or roughness.",
                    section="CONDUITS",
                    object_type="link",
                    object_id=conduit,
                )
            )
        else:
            try:
                length = parse_float(row.tokens[3], context=f"{conduit} length")
                roughness = parse_float(row.tokens[4], context=f"{conduit} roughness")
                if length <= 0 or roughness <= 0:
                    issues.append(
                        _issue(
                            "invalid_conduit_geometry",
                            Severity.BLOCKING,
                            f"Conduit {conduit!r} has non-positive length or roughness.",
                            section="CONDUITS",
                            object_type="link",
                            object_id=conduit,
                            remediation="Enter verified positive length and Manning roughness.",
                        )
                    )
            except ValueError as exc:
                issues.append(
                    _issue(
                        "invalid_conduit_number",
                        Severity.BLOCKING,
                        str(exc),
                        section="CONDUITS",
                        object_id=conduit,
                    )
                )

    elevations: dict[str, float] = {}
    for section in node_sections:
        for row in document.data_rows(section):
            if len(row.tokens) >= 2:
                try:
                    elevations[row.tokens[0].casefold()] = float(row.tokens[1])
                except ValueError:
                    pass
    for row in document.data_rows("CONDUITS"):
        if len(row.tokens) < 7:
            continue
        link, upstream, downstream = row.tokens[:3]
        if upstream.casefold() not in elevations or downstream.casefold() not in elevations:
            continue
        try:
            upstream_invert = elevations[upstream.casefold()] + float(row.tokens[5])
            downstream_invert = elevations[downstream.casefold()] + float(row.tokens[6])
        except ValueError:
            continue
        if upstream_invert <= downstream_invert:
            issues.append(
                _issue(
                    "nonpositive_conduit_slope",
                    Severity.WARNING,
                    f"Conduit {link!r} has zero or adverse invert slope.",
                    section="CONDUITS",
                    object_type="link",
                    object_id=link,
                    remediation="Verify surveyed inverts, offsets, direction, and pump requirements.",
                )
            )

    area_by_subcatchment: dict[str, float] = {}
    flow_units = next(
        (
            row.tokens[1].upper()
            for row in document.data_rows("OPTIONS")
            if len(row.tokens) > 1 and row.tokens[0].upper() == "FLOW_UNITS"
        ),
        "CFS",
    )
    area_factor = 43_560 if flow_units in {"CFS", "GPM", "MGD"} else 10_000
    for row in subcatchment_rows:
        if len(row.tokens) >= 4:
            try:
                area_by_subcatchment[row.tokens[0].casefold()] = float(row.tokens[3]) * area_factor
            except ValueError:
                pass
    lid_footprint: dict[str, float] = defaultdict(float)
    for row in document.data_rows("LID_USAGE"):
        if len(row.tokens) >= 5:
            try:
                lid_footprint[row.tokens[0].casefold()] += float(row.tokens[2]) * float(
                    row.tokens[3]
                )
            except ValueError:
                continue
    for sid, footprint in lid_footprint.items():
        available = area_by_subcatchment.get(sid)
        if available is not None and footprint > available + 1e-9:
            issues.append(
                _issue(
                    "lid_footprint_exceeds_subcatchment",
                    Severity.BLOCKING,
                    f"LID footprint {_format_number(footprint)} exceeds subcatchment area "
                    f"{_format_number(available)} in model area units.",
                    section="LID_USAGE",
                    object_type="subcatchment",
                    object_id=sid,
                    remediation="Reduce units/area or correct the subcatchment area and unit system.",
                )
            )

    for name in document.section_names():
        if name not in KNOWN_SECTIONS:
            issues.append(
                _issue(
                    "uninterpreted_section",
                    Severity.WARNING,
                    f"Section [{name}] will be preserved but is not interpreted.",
                    section=name,
                    remediation="Review the section in its authoring application before analysis.",
                )
            )

    checks = [
        ModelCheck(
            key="syntax_and_sections",
            label="Required SWMM sections",
            status="pass" if not any(i.code == "missing_section" for i in issues) else "block",
            detail="Required simulation, rainfall, and subcatchment sections are present.",
        ),
        ModelCheck(
            key="connected_to_outlet",
            label="Network connectivity",
            status=(
                "pass"
                if not any(
                    i.code in {"missing_outfall", "node_without_outfall_path", "unknown_link_node"}
                    for i in issues
                )
                else "block"
            ),
            detail="Each routing node has a directed path to an outfall.",
        ),
        ModelCheck(
            key="conduit_geometry",
            label="Conduit geometry",
            status=(
                "block"
                if any(i.code in {"missing_xsection", "invalid_conduit_geometry"} for i in issues)
                else "review"
                if any(i.code == "nonpositive_conduit_slope" for i in issues)
                else "pass"
            ),
            detail="Conduits have positive length/roughness and a defined cross-section.",
        ),
        ModelCheck(
            key="solver_baseline",
            label="Baseline solver QA",
            status="not_run",
            detail="Run the pinned SWMM engine to assess continuity, stability, flooding, and surcharge.",
        ),
        ModelCheck(
            key="parameter_review",
            label="Assumption review",
            status="review",
            detail="Confirm all assumed or inferred model parameters in the assumption ledger.",
        ),
    ]
    return ValidationReport(
        issues=issues,
        checks=checks,
        section_names=document.section_names(),
        object_counts=counts,
    )


def _format_number(value: float) -> str:
    return f"{value:.6g}"


def compare_reference_metrics(
    observed: dict[str, float],
    reference: dict[str, float],
    *,
    absolute_tolerance: float,
    relative_tolerance: float,
) -> dict[str, Any]:
    rows = []
    success = True
    for metric in sorted(set(observed) | set(reference)):
        actual = observed.get(metric)
        expected = reference.get(metric)
        if actual is None or expected is None:
            passed = False
            abs_error = None
            rel_error = None
        else:
            abs_error = abs(actual - expected)
            denominator = max(abs(expected), absolute_tolerance)
            rel_error = abs_error / denominator
            passed = math.isclose(
                actual,
                expected,
                rel_tol=relative_tolerance,
                abs_tol=absolute_tolerance,
            )
        success = success and passed
        rows.append(
            {
                "metric": metric,
                "observed": actual,
                "reference": expected,
                "absolute_error": abs_error,
                "relative_error": rel_error,
                "passed": passed,
            }
        )
    return {"matched": success, "metrics": rows}
