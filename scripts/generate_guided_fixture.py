#!/usr/bin/env python3
"""Generate the guided-example exact-run evidence fixture with EPA SWMM 5.2.4.

This is deliberately an execution tool, not a fixture synthesizer.  It accepts no
precomputed hydraulic values: every metric written to the manifest is extracted
from the report produced by the supplied, hashed SWMM executable.

Run from the repository root with::

    PYTHONPATH=python python3 scripts/generate_guided_fixture.py \
        --solver /path/to/swmm5 --output /path/to/fixture

The complete suite is 884 serial solver jobs (four baselines plus 220 integer
LID designs evaluated under four synthetic storms).  The output directory must
be empty.  The final ``manifest.json`` is published with an atomic rename only
after every required run and metric has been produced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "python"))

from gi_scenario_core.bundle import discover_external_references
from gi_scenario_core.contracts import CandidateSlot, QAPolicy
from gi_scenario_core.search import design_id, enumerate_vectors
from gi_scenario_core.solver import SolverOutcome, SolverRunner, sha256_file
from gi_scenario_core.swmm import SwmmDocument, tokenize

SCHEMA_VERSION = "2.0"
EXTRACTOR_VERSION = "guided-rpt-us-5.2.4-v1"
FIXTURE_ID = "cedar_trace_exact_v2"
SEARCH_SEED = 424_242
DESIGN_COUNT = 220
STORM_COUNT = 4
BASELINE_JOB_COUNT = STORM_COUNT
EXPECTED_JOB_COUNT = BASELINE_JOB_COUNT + DESIGN_COUNT * STORM_COUNT
RAIN_SERIES_NAME = "DESIGN_STORM"
SYNTHETIC_DATE = date(2026, 1, 1)
# The public guided flow teaches four explicit engineering decisions. These
# alternatives were selected pedagogically after inspecting the original exact
# fixture; they are not an unbiased benchmark shortlist. Keep their artifacts
# so every teaching claim can be inspected in EPA SWMM: ALT-014, ALT-017,
# ALT-109, and ALT-207 (one-based UI identifiers).
SHORTLIST_DESIGN_SEQUENCES = frozenset({13, 16, 108, 206})

DEFAULT_MODEL = REPOSITORY_ROOT / "benchmarks" / "cc0_reference" / "reference.inp"

FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")
CONTINUITY_RE = re.compile(
    r"Continuity\s+Error\s*\(%\)\s*[.:= ]+\s*"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)",
    re.IGNORECASE,
)
VERSION_RE = re.compile(r"(?<!\d)5\.2\.4(?!\d)")


class FixtureError(RuntimeError):
    """A reproducibility or evidence invariant was not satisfied."""


@dataclass(frozen=True, slots=True)
class SyntheticStorm:
    storm_id: str
    name: str
    description: str
    depth_inches: float
    weights: tuple[float, ...]
    interval_minutes: int = 5

    @property
    def duration_hours(self) -> float:
        return len(self.weights) * self.interval_minutes / 60.0

    def intensities_in_per_hour(self) -> tuple[float, ...]:
        """Scale the dimensionless shape to the stated event depth."""

        weight_sum = sum(self.weights)
        if weight_sum <= 0:
            raise FixtureError(f"synthetic storm {self.storm_id} has no rainfall weight")
        interval_hours = self.interval_minutes / 60.0
        scale = self.depth_inches / (weight_sum * interval_hours)
        return tuple(weight * scale for weight in self.weights)

    def to_manifest(self) -> dict[str, Any]:
        intensities = self.intensities_in_per_hour()
        represented_depth = sum(intensities) * self.interval_minutes / 60.0
        return {
            "storm_id": self.storm_id,
            "name": self.name,
            "synthetic": True,
            "authority_status": "user_defined",
            "source": "Self-authored CC0 teaching hyetograph; no regulatory authority",
            "description": self.description,
            "interval_minutes": self.interval_minutes,
            "duration_hours": self.duration_hours,
            "depth_inches": self.depth_inches,
            "represented_depth_inches": represented_depth,
            "intensities_inches_per_hour": list(intensities),
        }


STORMS: tuple[SyntheticStorm, ...] = (
    SyntheticStorm(
        storm_id="synthetic_balanced_1in",
        name="Synthetic balanced 1.0 in event",
        description=(
            "One-hour symmetric teaching event; not derived from an observed or adopted storm."
        ),
        depth_inches=1.0,
        weights=(1, 2, 3, 4, 5, 6, 6, 5, 4, 3, 2, 1),
    ),
    SyntheticStorm(
        storm_id="synthetic_front_loaded_2in",
        name="Synthetic front-loaded 2.0 in event",
        description="One-hour early-peak stress shape created only for workflow demonstration.",
        depth_inches=2.0,
        weights=(9, 8, 7, 6, 5, 4, 3, 2, 1, 1, 1, 1),
    ),
    SyntheticStorm(
        storm_id="synthetic_long_2_5in",
        name="Synthetic long-duration 2.5 in event",
        description="Four-hour low-intensity teaching event with a broad middle pulse.",
        depth_inches=2.5,
        weights=(1,) * 12 + (2,) * 12 + (3,) * 12 + (1,) * 12,
    ),
    SyntheticStorm(
        storm_id="synthetic_stress_4in",
        name="Synthetic symmetric 4.0 in stress event",
        description=(
            "Two-hour severe synthetic sensitivity case; not an adopted return-period event."
        ),
        depth_inches=4.0,
        weights=tuple(range(1, 13)) + tuple(range(12, 0, -1)),
    ),
)


def candidate_slots() -> list[CandidateSlot]:
    """Return immutable, self-authored LID placements for the CC0 teaching model.

    ``base_units`` reflects the single BIOCELL already present in reference.inp.
    Each design value is the integer count of *additional* identical units.
    These approvals are scoped to the synthetic demo and are not professional
    engineering attestations.
    """

    basis = "Self-authored CC0 teaching fixture; approved only for this synthetic demo"
    return [
        CandidateSlot(
            slot_id="S1_BIO",
            subcatchment_id="S1",
            lid_control="BIOCELL",
            lid_type="BC",
            base_units=1,
            min_additional_units=0,
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
            base_units=0,
            min_additional_units=0,
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
            base_units=0,
            min_additional_units=0,
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
            base_units=0,
            min_additional_units=0,
            max_additional_units=6,
            unit_area=16.0,
            surface_width=4.0,
            from_impervious_percent=5.0,
            drain_to="J2",
            feasibility_basis=basis,
            engineer_approved=True,
        ),
    ]


def deterministic_designs(
    slots: Sequence[CandidateSlot],
    *,
    seed: int = SEARCH_SEED,
    count: int = DESIGN_COUNT,
) -> list[dict[str, Any]]:
    """Select a stable, dependency-independent exact design order.

    The zero vector is excluded because it is already represented by the four
    baseline jobs.  Candidates are ranked by SHA-256(seed, vector), avoiding an
    environment-dependent optional QMC backend while retaining a seeded,
    reproducible spread over the finite integer universe.
    """

    zero = tuple(0 for _ in slots)
    universe = [tuple(vector) for vector in enumerate_vectors(list(slots))]
    universe = [vector for vector in universe if vector != zero]
    if count > len(universe):
        raise FixtureError(
            f"requested {count} designs from a {len(universe)}-design nonbaseline universe"
        )

    def rank(vector: tuple[int, ...]) -> str:
        payload = json.dumps(
            {"seed": seed, "vector": vector}, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    selected = sorted(universe, key=lambda vector: (rank(vector), vector))[:count]
    if len(set(selected)) != count or zero in selected:
        raise FixtureError("deterministic design selection violated uniqueness invariants")
    return [
        {
            "sequence": sequence,
            "design_id": design_id(vector),
            "vector": list(vector),
            "selection_rank_sha256": rank(vector),
        }
        for sequence, vector in enumerate(selected)
    ]


def _restricted_environment() -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "LANG", "LC_ALL")
        if key in os.environ
    }
    environment["OMP_NUM_THREADS"] = "1"
    return environment


def verify_solver_version(solver: str | Path) -> tuple[Path, str, str, int]:
    executable = Path(solver).expanduser().resolve()
    if not executable.is_file():
        raise FixtureError(f"SWMM executable was not found: {executable}")
    try:
        process = subprocess.run(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_restricted_environment(),
            shell=False,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FixtureError(f"could not query the SWMM executable version: {exc}") from exc
    output = (process.stdout + b"\n" + process.stderr).decode("utf-8", errors="replace").strip()
    if not VERSION_RE.search(output):
        excerpt = output[:500] or "<no version output>"
        raise FixtureError(
            f"the solver version response did not contain 5.2.4; received: {excerpt!r}"
        )
    return executable, output[:4096], sha256_file(executable), process.returncode


def _read_base_model(path: str | Path) -> tuple[SwmmDocument, bytes]:
    model_path = Path(path).expanduser().resolve()
    if not model_path.is_file():
        raise FixtureError(f"guided model was not found: {model_path}")
    document = SwmmDocument.from_path(model_path)
    external = sorted(discover_external_references(document))
    if external:
        raise FixtureError(
            "the fixture generator has no dependency-path input; use a self-contained model. "
            f"External references found: {', '.join(external)}"
        )
    flow_units = None
    for row in document.data_rows("OPTIONS"):
        if len(row.tokens) >= 2 and row.tokens[0].upper() == "FLOW_UNITS":
            flow_units = row.tokens[1].upper()
            break
    if flow_units != "CFS":
        raise FixtureError(
            "the guided fixture currently requires FLOW_UNITS CFS so report metrics are "
            "unambiguously CFS and million gallons"
        )
    gages = list(document.data_rows("RAINGAGES"))
    if not any(
        len(row.tokens) >= 6
        and row.tokens[-2].upper() == "TIMESERIES"
        and row.tokens[-1].casefold() == RAIN_SERIES_NAME.casefold()
        for row in gages
    ):
        raise FixtureError(f"a [RAINGAGES] row must use TIMESERIES {RAIN_SERIES_NAME}")
    if document.get_section("TIMESERIES") is None:
        raise FixtureError("the model does not contain a [TIMESERIES] section")
    return document, model_path.read_bytes()


def _copy_document(document: SwmmDocument) -> SwmmDocument:
    clone = SwmmDocument.parse(document.render())
    clone.encoding = document.encoding
    clone.bom = document.bom
    return clone


def _clock(minutes_after_midnight: int) -> str:
    hours, minutes = divmod(minutes_after_midnight, 60)
    return f"{hours:02d}:{minutes:02d}"


def materialize_storm(base: SwmmDocument, storm: SyntheticStorm) -> SwmmDocument:
    document = _copy_document(base)
    section = document.require_section("TIMESERIES")
    first_target_index: int | None = None
    retained: list[str] = []
    for line in section.lines:
        tokens, _ = tokenize(line)
        if tokens and tokens[0].casefold() == RAIN_SERIES_NAME.casefold():
            if first_target_index is None:
                first_target_index = len(retained)
            continue
        retained.append(line)
    if first_target_index is None:
        first_target_index = len(retained)

    date_text = SYNTHETIC_DATE.strftime("%m/%d/%Y")
    rows = [
        f"{RAIN_SERIES_NAME:<24} {date_text}  "
        f"{_clock(index * storm.interval_minutes)}  {intensity:.9f}"
        for index, intensity in enumerate(storm.intensities_in_per_hour())
    ]
    rows.append(
        f"{RAIN_SERIES_NAME:<24} {date_text}  "
        f"{_clock(len(storm.weights) * storm.interval_minutes)}  0.000000000"
    )
    section.lines = retained[:first_target_index] + rows + retained[first_target_index:]

    title = document.require_section("TITLE")
    title.lines.append(f";;Storm case: {storm.name} — SYNTHETIC / NOT AN ADOPTED EVENT")
    return document


def validate_candidate_slots(document: SwmmDocument, slots: Sequence[CandidateSlot]) -> None:
    subcatchments = {value.casefold() for value in document.object_ids("SUBCATCHMENTS")}
    controls = {row.tokens[0].casefold() for row in document.data_rows("LID_CONTROLS")}
    usage: dict[tuple[str, str], int] = {}
    for row in document.data_rows("LID_USAGE"):
        if len(row.tokens) >= 3:
            try:
                count = int(row.tokens[2])
            except ValueError as exc:
                raise FixtureError(f"noninteger baseline LID count in row: {row.raw}") from exc
            usage[(row.tokens[0].casefold(), row.tokens[1].casefold())] = count
    for slot in slots:
        if slot.subcatchment_id.casefold() not in subcatchments:
            raise FixtureError(f"candidate subcatchment is absent: {slot.subcatchment_id}")
        if slot.lid_control.casefold() not in controls:
            raise FixtureError(f"candidate LID control is absent: {slot.lid_control}")
        actual_base = usage.get((slot.subcatchment_id.casefold(), slot.lid_control.casefold()), 0)
        if actual_base != slot.base_units:
            raise FixtureError(
                f"candidate {slot.slot_id} expects {slot.base_units} baseline units but "
                f"the model contains {actual_base}"
            )


def materialize_design(
    storm_document: SwmmDocument,
    slots: Sequence[CandidateSlot],
    vector: Sequence[int],
) -> bytes:
    if len(vector) != len(slots):
        raise FixtureError("design vector length does not match candidate slots")
    document = _copy_document(storm_document)
    for slot, additional_units in zip(slots, vector, strict=True):
        if not isinstance(additional_units, int):
            raise FixtureError(f"candidate {slot.slot_id} received a noninteger unit count")
        if not slot.min_additional_units <= additional_units <= slot.max_additional_units:
            raise FixtureError(f"candidate {slot.slot_id} unit count is outside its bounds")
        total_units = slot.base_units + additional_units
        if slot.base_units and additional_units:
            document.set_lid_unit_count(slot.subcatchment_id, slot.lid_control, total_units)
        elif additional_units:
            document.add_lid_usage(
                subcatchment_id=slot.subcatchment_id,
                lid_control=slot.lid_control,
                number=additional_units,
                area=slot.unit_area,
                width=slot.surface_width,
                initial_saturation=slot.initial_saturation_percent,
                from_impervious=slot.from_impervious_percent,
                route_to_pervious=slot.route_to_pervious,
                report_file=slot.report_file or "*",
                drain_to=slot.drain_to or "*",
                from_pervious_flag=slot.from_pervious,
            )
    # Reparse before execution: malformed materialization must never reach SWMM.
    materialized = document.to_bytes()
    reparsed = SwmmDocument.parse(materialized.decode(document.encoding).lstrip("\ufeff"))
    if reparsed.section_names() != document.section_names():
        raise FixtureError("materialized design failed the parser round-trip check")
    return materialized


def _extract_continuity(report_text: str) -> tuple[float | None, float | None]:
    section = "unknown"
    values: dict[str, float] = {}
    for line in report_text.splitlines():
        upper = line.upper()
        if "RUNOFF QUANTITY CONTINUITY" in upper:
            section = "runoff"
        elif "FLOW ROUTING CONTINUITY" in upper:
            section = "flow"
        match = CONTINUITY_RE.search(line)
        if match and section in {"runoff", "flow"}:
            values[section] = float(match.group(1))
    return values.get("runoff"), values.get("flow")


def _extract_flooding_loss(report_text: str) -> float | None:
    for line in report_text.splitlines():
        if "FLOODING LOSS" not in line.upper():
            continue
        label_end = line.upper().find("FLOODING LOSS") + len("FLOODING LOSS")
        values = [float(item) for item in FLOAT_RE.findall(line[label_end:])]
        if values:
            # In a US-units SWMM report the last Flow Routing Continuity column is 10^6 gal.
            return values[-1]
    return None


def _extract_outfall_rows(
    report_text: str, outfall_ids: Sequence[str]
) -> dict[str, dict[str, float]]:
    lines = report_text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if "OUTFALL LOADING SUMMARY" in line.upper()),
        None,
    )
    if start is None:
        return {}
    expected = {item.casefold(): item for item in outfall_ids}
    results: dict[str, dict[str, float]] = {}
    for line in lines[start + 1 :]:
        tokens = line.split()
        if not tokens or tokens[0].casefold() not in expected:
            continue
        numeric: list[float] = []
        for token in tokens[1:]:
            if FLOAT_RE.fullmatch(token):
                numeric.append(float(token))
        if len(numeric) < 4:
            continue
        object_id = expected[tokens[0].casefold()]
        results[object_id] = {
            "flow_frequency_percent": numeric[0],
            "average_flow_cfs": numeric[1],
            "peak_flow_cfs": numeric[2],
            "volume_million_gallons": numeric[3],
        }
        if len(results) == len(expected):
            break
    return results


def parse_required_metrics(
    report_text: str,
    *,
    outfall_ids: Sequence[str],
) -> dict[str, Any]:
    runoff, flow = _extract_continuity(report_text)
    flooding = _extract_flooding_loss(report_text)
    outfalls = _extract_outfall_rows(report_text, outfall_ids)
    missing: list[str] = []
    if runoff is None:
        missing.append("runoff continuity error")
    if flow is None:
        missing.append("flow-routing continuity error")
    if flooding is None:
        missing.append("system flooding loss")
    absent_outfalls = [item for item in outfall_ids if item not in outfalls]
    if absent_outfalls:
        missing.append(f"outfall summary rows ({', '.join(absent_outfalls)})")
    if missing:
        raise FixtureError("required SWMM report metrics were absent: " + "; ".join(missing))
    return {
        "runoff_continuity_error_percent": runoff,
        "flow_continuity_error_percent": flow,
        "system_flooding_loss_million_gallons": flooding,
        "outfall_peak_cfs": max(row["peak_flow_cfs"] for row in outfalls.values()),
        "outfall_volume_million_gallons": sum(
            row["volume_million_gallons"] for row in outfalls.values()
        ),
        "outfall_aggregation": {
            "peak": "maximum peak flow across modeled outfalls",
            "volume": "sum of event volume across modeled outfalls",
        },
        "outfalls": outfalls,
    }


def _qa_manifest(outcome: SolverOutcome) -> dict[str, Any]:
    return {
        "status": outcome.qa_status,
        "policy": {
            "runoff_continuity_warning_percent": 2.0,
            "flow_continuity_warning_percent": 2.0,
        },
        "findings": [item.model_dump(mode="json") for item in outcome.findings],
    }


def _prepare_output(path: str | Path) -> Path:
    output = Path(path).expanduser().resolve()
    if output.exists():
        if not output.is_dir():
            raise FixtureError(f"output path is not a directory: {output}")
        if any(output.iterdir()):
            raise FixtureError(
                "output directory must be empty; existing evidence is never overwritten"
            )
    else:
        output.mkdir(parents=True)
    return output


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.partial")
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with temporary.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _run_id(sequence: int, kind: str, design: str, storm_id: str) -> str:
    payload = f"{FIXTURE_ID}|{sequence}|{kind}|{design}|{storm_id}".encode()
    return "run_" + hashlib.sha256(payload).hexdigest()[:20]


def _portable_source_path(path: str | Path) -> str:
    """Record useful provenance without leaking a workstation path."""

    resolved = Path(path).expanduser().resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return f"external/{resolved.name}"


def _retained_artifacts(work: Path, output: Path) -> dict[str, str]:
    return {
        "input": (work / "input.inp").relative_to(output).as_posix(),
        "report": (work / "report.rpt").relative_to(output).as_posix(),
        "output": (work / "output.out").relative_to(output).as_posix(),
    }


def _execute_one(
    *,
    runner: SolverRunner,
    output: Path,
    scratch: Path,
    sequence: int,
    kind: str,
    design: str,
    design_sequence: int | None,
    vector: Sequence[int],
    slot_ids: Sequence[str],
    storm: SyntheticStorm,
    model_bytes: bytes,
    outfall_ids: Sequence[str],
    retain: bool,
) -> dict[str, Any]:
    run_id = _run_id(sequence, kind, design, storm.storm_id)
    if retain:
        work = output / "runs" / run_id
        if work.exists():
            raise FixtureError(f"refusing to overwrite retained run directory: {work}")
        temporary_context = None
        work.mkdir(parents=True)
    else:
        temporary_context = tempfile.TemporaryDirectory(prefix=f"{run_id}-", dir=scratch)
        work = Path(temporary_context.name)

    try:
        outcome = runner.run(
            model_bytes=model_bytes,
            work_directory=work,
            dependencies=None,
            qa_policy=QAPolicy(
                runoff_continuity_warning_percent=2.0,
                flow_continuity_warning_percent=2.0,
            ),
        )
        if outcome.return_code != 0:
            raise FixtureError(
                f"SWMM failed for {run_id} with exit status {outcome.return_code}: "
                f"{outcome.stderr[-1000:]}"
            )
        if not outcome.report_sha256 or not outcome.output_sha256:
            raise FixtureError(f"SWMM did not create complete exact artifacts for {run_id}")
        report_path = work / "report.rpt"
        report_text = report_path.read_text(encoding="utf-8", errors="replace")
        metrics = parse_required_metrics(report_text, outfall_ids=outfall_ids)
        artifacts = (
            _retained_artifacts(work, output)
            if retain
            else {
                "input": None,
                "report": None,
                "output": None,
            }
        )
        return {
            "sequence": sequence,
            "run_id": run_id,
            "kind": kind,
            "exact": True,
            "design_id": design,
            "design_sequence": design_sequence,
            "design_vector": list(vector),
            "additional_lid_units": dict(zip(slot_ids, vector, strict=True)),
            "storm_id": storm.storm_id,
            "hashes": {
                "input_sha256": outcome.input_sha256,
                "report_sha256": outcome.report_sha256,
                "output_sha256": outcome.output_sha256,
                "solver_sha256": outcome.solver_sha256,
            },
            "metrics": metrics,
            "qa": _qa_manifest(outcome),
            "artifacts_retained": retain,
            "artifacts": artifacts,
        }
    finally:
        if temporary_context is not None:
            temporary_context.cleanup()


def generate_fixture(
    *,
    solver: str | Path,
    model: str | Path,
    output: str | Path,
) -> Path:
    executable, version_output, solver_hash, version_return_code = verify_solver_version(solver)
    base_document, original_model_bytes = _read_base_model(model)
    slots = candidate_slots()
    validate_candidate_slots(base_document, slots)
    designs = deterministic_designs(slots)
    if len(STORMS) != STORM_COUNT or len(designs) != DESIGN_COUNT:
        raise FixtureError("fixture constants do not produce the required job accounting")
    output_path = _prepare_output(output)
    scratch = output_path / ".scratch"
    scratch.mkdir()
    runner = SolverRunner(executable)
    if runner.solver_sha256 != solver_hash:
        raise FixtureError("the solver changed between version verification and runner creation")
    outfall_ids = base_document.object_ids("OUTFALLS")
    if not outfall_ids:
        raise FixtureError("the guided model has no outfalls to report")
    storm_documents = {storm.storm_id: materialize_storm(base_document, storm) for storm in STORMS}
    slot_ids = [slot.slot_id for slot in slots]
    zero_vector = [0] * len(slots)
    runs: list[dict[str, Any]] = []
    run_sequence = 0

    try:
        for storm in STORMS:
            baseline_bytes = materialize_design(storm_documents[storm.storm_id], slots, zero_vector)
            runs.append(
                _execute_one(
                    runner=runner,
                    output=output_path,
                    scratch=scratch,
                    sequence=run_sequence,
                    kind="baseline",
                    design="baseline",
                    design_sequence=None,
                    vector=zero_vector,
                    slot_ids=slot_ids,
                    storm=storm,
                    model_bytes=baseline_bytes,
                    outfall_ids=outfall_ids,
                    retain=True,
                )
            )
            run_sequence += 1

        for design in designs:
            vector = design["vector"]
            retain = design["sequence"] in SHORTLIST_DESIGN_SEQUENCES
            for storm in STORMS:
                model_bytes = materialize_design(storm_documents[storm.storm_id], slots, vector)
                runs.append(
                    _execute_one(
                        runner=runner,
                        output=output_path,
                        scratch=scratch,
                        sequence=run_sequence,
                        kind="alternative",
                        design=design["design_id"],
                        design_sequence=design["sequence"],
                        vector=vector,
                        slot_ids=slot_ids,
                        storm=storm,
                        model_bytes=model_bytes,
                        outfall_ids=outfall_ids,
                        retain=retain,
                    )
                )
                run_sequence += 1
                if run_sequence % 25 == 0 or run_sequence == EXPECTED_JOB_COUNT:
                    print(
                        f"completed {run_sequence}/{EXPECTED_JOB_COUNT} exact SWMM jobs",
                        file=sys.stderr,
                        flush=True,
                    )
    finally:
        if scratch.exists() and not any(scratch.iterdir()):
            scratch.rmdir()

    if run_sequence != EXPECTED_JOB_COUNT or len(runs) != EXPECTED_JOB_COUNT:
        raise FixtureError(
            f"job accounting mismatch: expected {EXPECTED_JOB_COUNT}, completed {len(runs)}"
        )
    if len({item["run_id"] for item in runs}) != EXPECTED_JOB_COUNT:
        raise FixtureError("run identifiers are not unique")
    if any(item["hashes"]["solver_sha256"] != solver_hash for item in runs):
        raise FixtureError("one or more runs used a different solver hash")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "fixture_id": FIXTURE_ID,
        "title": "GI Scenario Studio guided CC0 exact-run fixture",
        "license": "CC0-1.0",
        "attribution": "GI Scenario Studio contributors",
        "redistribution": "permitted",
        "modification_notice": (
            "Generated from the repository's self-authored CC0 model and synthetic storms; "
            "no observed or third-party hydraulic data is incorporated."
        ),
        "project_kind": "demo",
        "fitness_disposition": "not_assessed",
        "reference_matched": False,
        "reference_comparison": None,
        "claim": "Direct SWMM 5.2.4 results under the documented synthetic assumptions.",
        "notice": (
            "Synthetic teaching model and storms — not for design, permitting, flood-risk "
            "determination, or representation of an existing drainage system."
        ),
        "limitations": [
            "The model, storms, candidate slots, and costs are self-authored teaching data.",
            (
                "No observed event, calibration record, approved reference report, or field "
                "condition was matched."
            ),
            (
                "The 220 alternatives are a deterministic hash-ranked subset, not a "
                "global-optimum claim."
            ),
            (
                "Only the baseline and four pedagogically selected teaching alternatives "
                "retain full INP/RPT/OUT artifacts."
            ),
            "Metric extraction supports the US-unit SWMM 5.2.4 report layout used by this fixture.",
        ],
        "source_model": {
            "origin": "self-authored",
            "version": "0.1.0",
            "path_supplied": _portable_source_path(model),
            "sha256": hashlib.sha256(original_model_bytes).hexdigest(),
            "external_dependencies": [],
            "flow_units": "CFS",
        },
        "solver": {
            "required_version": "5.2.4",
            "version_command": [executable.name, "--version"],
            "version_command_return_code": version_return_code,
            "version_output": version_output,
            "sha256": solver_hash,
            "environment": {"OMP_NUM_THREADS": "1"},
            "invocation": "direct subprocess without a shell",
        },
        "extractor": {
            "name": "GI Scenario Studio guided SWMM report parser",
            "version": EXTRACTOR_VERSION,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "supported_layout": "EPA SWMM 5.2.4 report tables in US customary units",
        },
        "search": {
            "seed": SEARCH_SEED,
            "method": "seeded_sha256_rank_over_finite_integer_universe",
            "baseline_vector": zero_vector,
            "baseline_excluded_from_alternatives": True,
            "design_order": designs,
            "shortlist_design_sequences": sorted(SHORTLIST_DESIGN_SEQUENCES),
            "shortlist_policy": (
                "pedagogical alternatives selected after the original exact fixture; "
                "not benchmark or optimization evidence"
            ),
        },
        "candidate_slots": [slot.model_dump(mode="json") for slot in slots],
        "storm_cases": [storm.to_manifest() for storm in STORMS],
        "job_accounting": {
            "baseline_jobs": BASELINE_JOB_COUNT,
            "unique_alternative_designs": DESIGN_COUNT,
            "alternative_jobs": DESIGN_COUNT * STORM_COUNT,
            "storm_cases": STORM_COUNT,
            "expected_jobs": EXPECTED_JOB_COUNT,
            "completed_jobs": len(runs),
            "retained_jobs": sum(1 for item in runs if item["artifacts_retained"]),
            "execution_order": "four baselines in storm order, then design-major/storm-minor",
        },
        "metric_definitions": {
            "runoff_continuity_error_percent": "signed SWMM Runoff Quantity Continuity error",
            "flow_continuity_error_percent": "signed SWMM Flow Routing Continuity error",
            "system_flooding_loss_million_gallons": (
                "Flooding Loss, last US-units column in Flow Routing Continuity"
            ),
            "outfall_peak_cfs": "maximum reported peak flow across modeled outfalls",
            "outfall_volume_million_gallons": (
                "sum of reported event volumes across modeled outfalls"
            ),
        },
        "runs": runs,
    }
    manifest_path = output_path / "manifest.json"
    _atomic_json(manifest_path, manifest)
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Execute the 884-job SWMM 5.2.4 guided-example suite and write an atomic "
            "exact-run evidence manifest."
        )
    )
    parser.add_argument(
        "--solver",
        required=True,
        help="Path to an EPA SWMM console executable whose --version output contains 5.2.4.",
    )
    parser.add_argument(
        "--model",
        default=str(DEFAULT_MODEL),
        help=f"Self-contained guided SWMM model (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="New or empty output directory for retained runs and manifest.json.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = generate_fixture(
            solver=arguments.solver,
            model=arguments.model,
            output=arguments.output,
        )
    except (FixtureError, ValueError, OSError) as exc:
        print(f"guided fixture generation failed: {exc}", file=sys.stderr)
        return 2
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
