"""Durable, exact SWMM analysis orchestration.

This module is intentionally a thin domain layer over :class:`ProjectRepository`,
the typed SWMM document, and :class:`SolverRunner`.  It does not estimate any
hydraulic value: metrics are extracted only from the report emitted by a direct
solver job.  The public methods return plain dictionaries so the RPC worker can
adopt this slice without introducing a second contract surface.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .contracts import AnalysisState, CandidateSlot, QAPolicy, SolverJobState
from .repository import ProjectRepository
from .search import design_id
from .solver import SolverOutcome, physics_cache_key, sha256_file
from .swmm import SwmmDocument, SwmmMutationError
from .validation import validate_model

_FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")
_CONTINUITY_RE = re.compile(
    r"Continuity\s+Error\s*\(\%\)\s*[.:= ]+\s*"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)",
    re.IGNORECASE,
)


class AnalysisOrchestrationError(RuntimeError):
    """The exact analysis could not preserve an engineering invariant."""


class MetricExtractionError(AnalysisOrchestrationError):
    """A required value was absent from the direct SWMM report."""


class ExactSolver(Protocol):
    """Dependency-injection boundary implemented by ``SolverRunner``."""

    solver_sha256: str

    def run(
        self,
        *,
        model_bytes: bytes,
        work_directory: str | Path,
        dependencies: dict[str, str | Path] | None = None,
        qa_policy: QAPolicy | dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SolverOutcome: ...


@dataclass(frozen=True, slots=True)
class StormModel:
    """A storm-specific SWMM input and its already-resolved dependencies."""

    storm_id: str
    model_bytes: bytes
    dependencies: Mapping[str, str | Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.storm_id or not self.storm_id.strip():
            raise ValueError("storm_id must not be empty")
        if not self.model_bytes:
            raise ValueError(f"storm {self.storm_id!r} has no SWMM model bytes")


@dataclass(frozen=True, slots=True)
class ExtractedMetric:
    metric_id: str
    value: float
    units: str
    object_id: str = ""

    def __post_init__(self) -> None:
        if not self.metric_id or not self.units:
            raise ValueError("an extracted metric requires an ID and units")
        if not math.isfinite(self.value):
            raise ValueError(f"metric {self.metric_id!r} is not finite")


MetricExtractor = Callable[[str, SwmmDocument], Sequence[ExtractedMetric]]
ModelMaterializer = Callable[[StormModel, Sequence[CandidateSlot], Sequence[int]], bytes]
ProgressCallback = Callable[[dict[str, Any]], None]


class _CallbackCancelEvent:
    """Duck-typed ``threading.Event`` that also polls a cancellation callback."""

    def __init__(
        self,
        event: threading.Event | None,
        callback: Callable[[], bool],
    ) -> None:
        self._event = event
        self._callback = callback

    def is_set(self) -> bool:
        return bool((self._event and self._event.is_set()) or self._callback())


def _parse_model_bytes(content: bytes) -> SwmmDocument:
    bom = content.startswith(b"\xef\xbb\xbf")
    try:
        text = content[3:].decode("utf-8") if bom else content.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = content.decode("cp1252")
        encoding = "cp1252"
        bom = False
    document = SwmmDocument.parse(text)
    document.encoding = encoding
    document.bom = bom
    return document


def _validated_materialized_document(content: bytes) -> SwmmDocument:
    document = _parse_model_bytes(content)
    report = validate_model(document)
    if report.blocking_count:
        codes = sorted(
            {issue.code for issue in report.issues if issue.severity.value == "blocking"}
        )
        raise AnalysisOrchestrationError(
            "materialized model has blocking validation findings: " + ", ".join(codes)
        )
    return document


def materialize_lid_design(
    storm: StormModel,
    slots: Sequence[CandidateSlot],
    vector: Sequence[int],
) -> bytes:
    """Apply only approved, bounded integer LID usage changes and reparse them."""

    if len(vector) != len(slots):
        raise ValueError("design vector length does not match the candidate-slot count")
    document = _parse_model_bytes(storm.model_bytes)
    for slot, additional in zip(slots, vector, strict=True):
        if isinstance(additional, bool) or not isinstance(additional, int):
            raise TypeError(f"candidate {slot.slot_id!r} requires an integer unit count")
        if not slot.engineer_approved:
            raise ValueError(f"candidate {slot.slot_id!r} is not engineer approved")
        if not slot.min_additional_units <= additional <= slot.max_additional_units:
            raise ValueError(f"candidate {slot.slot_id!r} is outside its approved bounds")

        matching = [
            row
            for row in document.data_rows("LID_USAGE")
            if len(row.tokens) >= 3
            and row.tokens[0].casefold() == slot.subcatchment_id.casefold()
            and row.tokens[1].casefold() == slot.lid_control.casefold()
        ]
        if len(matching) > 1:
            raise SwmmMutationError(f"multiple LID usage rows match candidate {slot.slot_id!r}")
        if matching:
            try:
                existing_units = int(matching[0].tokens[2])
            except ValueError as exc:
                raise SwmmMutationError(
                    f"candidate {slot.slot_id!r} has a noninteger baseline unit count"
                ) from exc
            if existing_units != slot.base_units:
                raise SwmmMutationError(
                    f"candidate {slot.slot_id!r} expects {slot.base_units} baseline units, "
                    f"but the storm model contains {existing_units}"
                )
            if additional:
                document.set_lid_unit_count(
                    slot.subcatchment_id,
                    slot.lid_control,
                    slot.base_units + additional,
                )
        elif slot.base_units:
            raise SwmmMutationError(
                f"candidate {slot.slot_id!r} declares baseline units but has no LID usage row"
            )
        elif additional:
            document.add_lid_usage(
                subcatchment_id=slot.subcatchment_id,
                lid_control=slot.lid_control,
                number=additional,
                area=slot.unit_area,
                width=slot.surface_width,
                initial_saturation=slot.initial_saturation_percent,
                from_impervious=slot.from_impervious_percent,
                route_to_pervious=slot.route_to_pervious,
                report_file=slot.report_file or "*",
                drain_to=slot.drain_to or "*",
                from_pervious_flag=slot.from_pervious,
            )

    materialized = document.to_bytes()
    reparsed = _validated_materialized_document(materialized)
    if reparsed.section_names() != document.section_names():
        raise AnalysisOrchestrationError("materialized model failed its parser round trip")
    return materialized


def _continuity_values(report_text: str) -> dict[str, float]:
    section = "unknown"
    values: dict[str, float] = {}
    for line in report_text.splitlines():
        upper = line.upper()
        if "RUNOFF QUANTITY CONTINUITY" in upper:
            section = "runoff"
        elif "FLOW ROUTING CONTINUITY" in upper:
            section = "flow"
        match = _CONTINUITY_RE.search(line)
        if match and section in {"runoff", "flow"}:
            values[section] = float(match.group(1))
    return values


def _flooding_loss(report_text: str) -> float | None:
    for line in report_text.splitlines():
        upper = line.upper()
        if "FLOODING LOSS" not in upper:
            continue
        values = _FLOAT_RE.findall(line[upper.find("FLOODING LOSS") + len("FLOODING LOSS") :])
        if values:
            return float(values[-1])
    return None


def _outfall_rows(
    report_text: str,
    outfall_ids: Sequence[str],
) -> dict[str, tuple[float, float]]:
    lines = report_text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if "OUTFALL LOADING SUMMARY" in line.upper()),
        None,
    )
    if start is None:
        return {}
    expected = {item.casefold(): item for item in outfall_ids}
    rows: dict[str, tuple[float, float]] = {}
    for line in lines[start + 1 :]:
        tokens = line.split()
        if not tokens or tokens[0].casefold() not in expected:
            continue
        numeric = [float(token) for token in tokens[1:] if _FLOAT_RE.fullmatch(token)]
        if len(numeric) >= 4:
            rows[expected[tokens[0].casefold()]] = (numeric[2], numeric[3])
        if len(rows) == len(expected):
            break
    return rows


def _summary_numeric_rows(
    report_text: str,
    heading: str,
    object_ids: Sequence[str],
) -> dict[str, list[float]]:
    """Read object rows from one named SWMM report summary, if it is present."""

    lines = report_text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if heading in line.upper()),
        None,
    )
    if start is None:
        return {}
    expected = {item.casefold(): item for item in object_ids}
    results: dict[str, list[float]] = {}
    banner_seen = False
    for line in lines[start + 1 :]:
        if re.match(r"^\s*\*{5,}", line):
            if banner_seen:
                break
            banner_seen = True
            continue
        tokens = line.split()
        if len(tokens) < 3 or tokens[0].casefold() not in expected:
            continue
        # Token 1 is the SWMM object type. A day count is numeric while the
        # hr:min occurrence token is intentionally ignored by full matching.
        numeric = [float(token) for token in tokens[2:] if _FLOAT_RE.fullmatch(token)]
        if numeric:
            results[expected[tokens[0].casefold()]] = numeric
    return results


def _report_unit_contract(document: SwmmDocument) -> dict[str, str]:
    flow_units = next(
        (
            row.tokens[1].upper()
            for row in document.data_rows("OPTIONS")
            if len(row.tokens) >= 2 and row.tokens[0].upper() == "FLOW_UNITS"
        ),
        None,
    )
    contracts = {
        "CFS": ("cfs", "million_gallons", "ft"),
        "GPM": ("gpm", "million_gallons", "ft"),
        "MGD": ("mgd", "million_gallons", "ft"),
        "CMS": ("cms", "million_liters", "m"),
        "LPS": ("lps", "million_liters", "m"),
        "MLD": ("mld", "million_liters", "m"),
    }
    if flow_units not in contracts:
        raise MetricExtractionError(
            f"unsupported or missing SWMM FLOW_UNITS for report extraction: {flow_units!r}"
        )
    flow, volume, depth = contracts[flow_units]
    return {"flow": flow, "volume": volume, "depth": depth}


def extract_required_report_metrics(
    report_text: str,
    document: SwmmDocument,
) -> list[ExtractedMetric]:
    """Extract a conservative unit-aware result set from a direct SWMM report.

    The parser fails closed when continuity, system flooding loss, or an outfall
    row is absent.  It never substitutes a default hydraulic value.
    """

    units = _report_unit_contract(document)
    flow_units = units["flow"]
    volume_units = units["volume"]
    depth_units = units["depth"]
    continuity = _continuity_values(report_text)
    flooding = _flooding_loss(report_text)
    outfall_ids = document.object_ids("OUTFALLS")
    outfalls = _outfall_rows(report_text, outfall_ids)
    missing: list[str] = []
    if "runoff" not in continuity:
        missing.append("runoff continuity error")
    if "flow" not in continuity:
        missing.append("flow-routing continuity error")
    if flooding is None:
        missing.append("system flooding loss")
    absent = [object_id for object_id in outfall_ids if object_id not in outfalls]
    if absent:
        missing.append("outfall rows: " + ", ".join(absent))
    if missing:
        raise MetricExtractionError(
            "required direct-report metrics are absent: " + "; ".join(missing)
        )

    metrics = [
        ExtractedMetric(
            "runoff_continuity_error_percent",
            continuity["runoff"],
            "percent",
        ),
        ExtractedMetric(
            "flow_continuity_error_percent",
            continuity["flow"],
            "percent",
        ),
        ExtractedMetric(
            f"system_flooding_loss_{volume_units}",
            flooding,
            volume_units,
        ),
    ]
    for object_id, (peak, volume) in sorted(outfalls.items()):
        metrics.extend(
            [
                ExtractedMetric(f"outfall_peak_{flow_units}", peak, flow_units, object_id),
                ExtractedMetric(
                    f"outfall_volume_{volume_units}",
                    volume,
                    volume_units,
                    object_id,
                ),
            ]
        )
    metrics.extend(
        [
            ExtractedMetric(
                f"outfall_peak_{flow_units}",
                max(peak for peak, _volume in outfalls.values()),
                flow_units,
            ),
            ExtractedMetric(
                f"outfall_volume_{volume_units}",
                sum(volume for _peak, volume in outfalls.values()),
                volume_units,
            ),
        ]
    )

    node_ids = sorted(
        {
            *document.object_ids("JUNCTIONS"),
            *document.object_ids("STORAGE"),
            *document.object_ids("DIVIDERS"),
            *document.object_ids("OUTFALLS"),
        }
    )
    for object_id, values in sorted(
        _summary_numeric_rows(report_text, "NODE DEPTH SUMMARY", node_ids).items()
    ):
        # Average depth, maximum depth, maximum HGL, day, reported max depth.
        if len(values) >= 3:
            metrics.extend(
                [
                    ExtractedMetric(
                        f"node_peak_depth_{depth_units}",
                        values[1],
                        depth_units,
                        object_id,
                    ),
                    ExtractedMetric(
                        f"node_peak_hgl_{depth_units}",
                        values[2],
                        depth_units,
                        object_id,
                    ),
                ]
            )
    for object_id, values in sorted(
        _summary_numeric_rows(report_text, "NODE INFLOW SUMMARY", node_ids).items()
    ):
        # Maximum lateral, maximum total, day, lateral volume, total volume, balance.
        if len(values) >= 5:
            metrics.extend(
                [
                    ExtractedMetric(
                        f"node_peak_total_inflow_{flow_units}",
                        values[1],
                        flow_units,
                        object_id,
                    ),
                    ExtractedMetric(
                        f"node_total_inflow_volume_{volume_units}",
                        values[4],
                        volume_units,
                        object_id,
                    ),
                ]
            )
    link_ids = sorted(
        {
            *document.object_ids("CONDUITS"),
            *document.object_ids("PUMPS"),
            *document.object_ids("ORIFICES"),
            *document.object_ids("WEIRS"),
            *document.object_ids("OUTLETS"),
        }
    )
    for object_id, values in sorted(
        _summary_numeric_rows(report_text, "LINK FLOW SUMMARY", link_ids).items()
    ):
        # Maximum absolute flow, day, maximum velocity, max/full flow, max/full depth.
        if values:
            metrics.append(
                ExtractedMetric(f"link_peak_flow_{flow_units}", values[0], flow_units, object_id)
            )
    return metrics


class AnalysisOrchestrator:
    """Plan and execute durable exact design-by-storm SWMM ensembles."""

    def __init__(
        self,
        repository: ProjectRepository,
        solver: ExactSolver | None = None,
        *,
        metric_extractor: MetricExtractor = extract_required_report_metrics,
        materializer: ModelMaterializer = materialize_lid_design,
        qa_policy: QAPolicy | dict[str, Any] | None = None,
    ) -> None:
        self.repository = repository
        self.solver = solver
        self.metric_extractor = metric_extractor
        self.materializer = materializer
        self.qa_policy = QAPolicy.model_validate(qa_policy or {})

    def _require_solver(self) -> ExactSolver:
        if self.solver is None:
            raise AnalysisOrchestrationError("this orchestrator has no configured exact solver")
        return self.solver

    def plan(
        self,
        analysis_id: str,
        *,
        candidate_slots: Sequence[CandidateSlot | dict[str, Any]],
        design_vectors: Sequence[Sequence[int]],
        storms: Sequence[StormModel],
        include_baseline: bool = True,
    ) -> dict[str, Any]:
        """Validate and persist the complete deterministic job plan before execution."""

        analysis = self.repository.get_analysis(analysis_id)
        solver = self._require_solver()
        if analysis["state"] != AnalysisState.DRAFT.value:
            raise ValueError("an exact analysis may be planned only from DRAFT")
        slots = tuple(CandidateSlot.model_validate(slot) for slot in candidate_slots)
        model_revision_id = analysis["payload"].get("model_revision_id")
        model_revision_sha256 = analysis["payload"].get("model_sha256")
        metric_extractor_version = analysis["payload"].get("metric_extractor_version")
        storm_map = self._storm_map(storms)
        vectors = self._normalized_vectors(slots, design_vectors, include_baseline)
        self.repository.transition_analysis(analysis_id, AnalysisState.VALIDATING)
        try:
            designs = [
                {
                    "design_id": design_id(vector),
                    "vector": list(vector),
                    "source": "baseline" if all(value == 0 for value in vector) else "exact_plan",
                    "sequence": sequence,
                }
                for sequence, vector in enumerate(vectors)
            ]
            self.repository.add_designs(analysis_id, designs)

            jobs: list[dict[str, Any]] = []
            for design in designs:
                for storm_id, storm in storm_map.items():
                    materialized = self.materializer(storm, slots, design["vector"])
                    # The materializer contract requires a successful reparse even
                    # when a custom materializer is injected.
                    _validated_materialized_document(materialized)
                    dependency_hashes = {
                        relative: sha256_file(path)
                        for relative, path in sorted(storm.dependencies.items())
                    }
                    cache_key = physics_cache_key(
                        model_bytes=materialized,
                        dependency_hashes=dependency_hashes,
                        solver_sha256=solver.solver_sha256,
                        solver_options={"omp_num_threads": 1},
                    )
                    job_id = self._job_id(analysis_id, design["design_id"], storm_id)
                    job = self.repository.create_solver_job(
                        analysis_id=analysis_id,
                        design_id=design["design_id"],
                        storm_id=storm_id,
                        physics_cache_key=cache_key,
                        job_id=job_id,
                        payload={
                            "model_revision_id": model_revision_id,
                            "model_revision_sha256": model_revision_sha256,
                            "metric_extractor_version": metric_extractor_version,
                            "design_vector": design["vector"],
                            "design_sequence": design["sequence"],
                            "model_sha256": hashlib.sha256(materialized).hexdigest(),
                            "dependency_hashes": dependency_hashes,
                            "qa_policy": self.qa_policy.model_dump(mode="json"),
                        },
                    )
                    jobs.append(job)
            # No execution can start until every design and job is durable.
            for job in jobs:
                if job["state"] == SolverJobState.PLANNED.value:
                    self.repository.transition_solver_job(job["job_id"], SolverJobState.QUEUED)
            self.repository.transition_analysis(analysis_id, AnalysisState.READY)
        except Exception:
            current = self.repository.get_analysis(analysis_id)["state"]
            if current == AnalysisState.VALIDATING.value:
                self.repository.transition_analysis(analysis_id, AnalysisState.FAILED)
            raise

        return {
            "analysis_id": analysis_id,
            "state": AnalysisState.READY.value,
            "exact": True,
            "design_count": len(designs),
            "alternative_count": sum(1 for item in designs if item["source"] != "baseline"),
            "storm_count": len(storm_map),
            "planned_solver_jobs": len(jobs),
            "job_ids": [job["job_id"] for job in jobs],
        }

    def recover(self, analysis_id: str) -> dict[str, Any]:
        """Requeue abandoned RUNNING jobs using the repository state machine."""

        recovered = self.repository.recover_abandoned_jobs()
        owned = {
            row["job_id"]
            for row in self.repository._connection.execute(
                "SELECT job_id FROM solver_jobs WHERE analysis_id=?", (analysis_id,)
            ).fetchall()
        }
        return {
            "analysis_id": analysis_id,
            "requeued_job_ids": sorted(set(recovered).intersection(owned)),
        }

    def cancel(self, analysis_id: str) -> dict[str, Any]:
        """Cancel every unfinished exact job and persist the terminal analysis state."""

        self._cancel_analysis(analysis_id)
        return self.status(analysis_id)

    def run(
        self,
        analysis_id: str,
        *,
        candidate_slots: Sequence[CandidateSlot | dict[str, Any]],
        storms: Sequence[StormModel],
        should_pause: Callable[[], bool] = lambda: False,
        should_cancel: Callable[[], bool] = lambda: False,
        cancel_event: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Execute queued exact jobs in persisted design/storm order."""

        slots = tuple(CandidateSlot.model_validate(slot) for slot in candidate_slots)
        self._require_solver()
        storm_map = self._storm_map(storms)
        analysis = self.repository.get_analysis(analysis_id)
        if analysis["state"] == AnalysisState.COMPLETED.value:
            return self.status(analysis_id, cache_hits=self._succeeded_job_count(analysis_id))
        if (
            analysis["state"] == AnalysisState.READY.value
            or analysis["state"] == AnalysisState.PAUSED.value
        ):
            self.repository.transition_analysis(analysis_id, AnalysisState.RUNNING)
        elif analysis["state"] != AnalysisState.RUNNING.value:
            raise ValueError(f"analysis {analysis_id!r} cannot run from {analysis['state']!r}")

        cache_hits = self._succeeded_job_count(analysis_id)
        jobs = self._jobs(analysis_id)
        callback_event = _CallbackCancelEvent(cancel_event, should_cancel)
        for job in jobs:
            if job["state"] in {
                SolverJobState.SUCCEEDED.value,
                SolverJobState.QA_REJECTED.value,
                SolverJobState.CANCELLED.value,
            }:
                continue
            if callback_event.is_set():
                self._cancel_analysis(analysis_id)
                break
            if should_pause():
                self.repository.transition_analysis(analysis_id, AnalysisState.PAUSING)
                self.repository.transition_analysis(analysis_id, AnalysisState.PAUSED)
                self._emit(on_progress, analysis_id, "analysis.paused")
                break
            storm = storm_map.get(job["storm_id"])
            if storm is None:
                self._fail_analysis(analysis_id)
                raise KeyError(f"planned storm {job['storm_id']!r} was not supplied")
            try:
                job_result = self.run_job(
                    job["job_id"],
                    candidate_slots=slots,
                    storm=storm,
                    cancel_event=callback_event,  # type: ignore[arg-type]
                    on_progress=on_progress,
                )
                cache_hits += int(bool(job_result.get("cache_hit")))
            except Exception:
                self._fail_analysis(analysis_id)
                raise
            if callback_event.is_set():
                self._cancel_analysis(analysis_id)
                break

        current = self.repository.get_analysis(analysis_id)["state"]
        remaining = self._unfinished_jobs(analysis_id)
        if current == AnalysisState.RUNNING.value:
            states = {job["state"] for job in self._jobs(analysis_id)}
            if SolverJobState.FAILED.value in states:
                self.repository.transition_analysis(analysis_id, AnalysisState.FAILED)
            elif SolverJobState.CANCELLED.value in states:
                self._cancel_analysis(analysis_id)
            elif not remaining:
                self.repository.transition_analysis(analysis_id, AnalysisState.COMPLETED)
                self._emit(on_progress, analysis_id, "analysis.completed")
        return self.status(
            analysis_id,
            cache_hits=cache_hits,
        )

    def run_job(
        self,
        job_id: str,
        *,
        candidate_slots: Sequence[CandidateSlot | dict[str, Any]],
        storm: StormModel,
        cancel_event: threading.Event | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Run one isolated exact SWMM job and persist its immutable evidence."""

        slots = tuple(CandidateSlot.model_validate(slot) for slot in candidate_slots)
        solver = self._require_solver()
        job = self.repository.get_solver_job(job_id)
        if job["storm_id"] != storm.storm_id:
            raise ValueError("the supplied storm does not match the persisted solver job")
        if job["state"] == SolverJobState.SUCCEEDED.value:
            return {"job_id": job_id, "state": job["state"], "cache_hit": True}
        if job["state"] == SolverJobState.PLANNED.value:
            job = self.repository.transition_solver_job(job_id, SolverJobState.QUEUED)
        if job["state"] != SolverJobState.QUEUED.value:
            raise ValueError(f"solver job {job_id!r} cannot run from {job['state']!r}")

        try:
            vector = job["payload"].get("design_vector")
            if not isinstance(vector, list):
                raise AnalysisOrchestrationError("solver job has no persisted design vector")
            model_bytes = self.materializer(storm, slots, vector)
            document = _validated_materialized_document(model_bytes)
            model_hash = hashlib.sha256(model_bytes).hexdigest()
            if model_hash != job["payload"].get("model_sha256"):
                raise AnalysisOrchestrationError("materialized model changed after planning")
            dependency_hashes = {
                relative: sha256_file(path) for relative, path in sorted(storm.dependencies.items())
            }
            actual_cache_key = physics_cache_key(
                model_bytes=model_bytes,
                dependency_hashes=dependency_hashes,
                solver_sha256=solver.solver_sha256,
                solver_options={"omp_num_threads": 1},
            )
            if actual_cache_key != job["physics_cache_key"]:
                raise AnalysisOrchestrationError("physics inputs changed after planning")

            cache_entry = self.repository.find_physics_cache_entry(
                actual_cache_key,
                exclude_job_id=job_id,
            )
            if cache_entry is not None:
                source_job = self.repository.get_solver_job(cache_entry["source_job_id"])
                target_derivations = {
                    "metric_extractor_version": job["payload"].get("metric_extractor_version"),
                    "qa_policy": job["payload"].get("qa_policy"),
                }
                source_derivations = {
                    "metric_extractor_version": source_job["payload"].get(
                        "metric_extractor_version"
                    ),
                    "qa_policy": source_job["payload"].get("qa_policy"),
                }
                # The physics key intentionally excludes derived-metric and QA
                # versions. Reusing their stored rows is safe only when those
                # derivation contracts are byte-for-byte equivalent.
                if source_derivations != target_derivations:
                    cache_entry = None
            if cache_entry is not None:
                result = self.repository.complete_solver_job_from_cache(
                    job_id,
                    cache_entry["source_job_id"],
                )
                self._emit(
                    on_progress,
                    job["analysis_id"],
                    "solver_job.completed",
                    job_id=job_id,
                    cache_hit=True,
                    cache_source_job_id=cache_entry["source_job_id"],
                )
                return {
                    "job_id": job_id,
                    "state": result["state"],
                    "qa_status": result["payload"].get("qa_status"),
                    "metric_count": len(self._metric_records(job_id)),
                    "cache_hit": True,
                    "cache_source_job_id": cache_entry["source_job_id"],
                }

            job = self.repository.transition_solver_job(job_id, SolverJobState.RUNNING)
            self._emit(on_progress, job["analysis_id"], "solver_job.started", job_id=job_id)

            work = self.repository.root / "runs" / job_id
            outcome = solver.run(
                model_bytes=model_bytes,
                work_directory=work,
                dependencies=dict(storm.dependencies),
                qa_policy=self.qa_policy,
                cancel_event=cancel_event,
            )
            artifact_bytes = self._read_solver_artifacts(work)
            self._verify_outcome(outcome, artifact_bytes, model_bytes)
            self._persist_artifacts(job_id, artifact_bytes)
            self._persist_qa_findings(job_id, outcome)

            if cancel_event is not None and cancel_event.is_set():
                result = self.repository.transition_solver_job(
                    job_id,
                    SolverJobState.CANCELLED,
                    payload_update={"qa_status": "rejected", "cancelled": True},
                )
                return {"job_id": job_id, "state": result["state"], "cache_hit": False}
            if outcome.qa_status == "rejected" or outcome.return_code != 0:
                result = self.repository.transition_solver_job(
                    job_id,
                    SolverJobState.QA_REJECTED,
                    payload_update=self._outcome_payload(outcome),
                )
                self._emit(
                    on_progress,
                    job["analysis_id"],
                    "solver_job.qa_rejected",
                    job_id=job_id,
                )
                return {"job_id": job_id, "state": result["state"], "cache_hit": False}

            report_text = artifact_bytes["report"].decode("utf-8", errors="replace")
            metrics = list(self.metric_extractor(report_text, document))
            if not metrics:
                raise MetricExtractionError("the exact report yielded no reportable metrics")
            self._validate_unique_metrics(metrics)
            result = self.repository.transition_solver_job(
                job_id,
                SolverJobState.SUCCEEDED,
                payload_update=self._outcome_payload(outcome),
            )
            self._persist_metrics(job_id, metrics)
            self.repository.register_physics_cache_entry(job_id)
            self._emit(
                on_progress,
                job["analysis_id"],
                "solver_job.completed",
                job_id=job_id,
            )
            return {
                "job_id": job_id,
                "state": result["state"],
                "qa_status": outcome.qa_status,
                "metric_count": len(metrics),
                "cache_hit": False,
            }
        except Exception as exc:
            current = self.repository.get_solver_job(job_id)
            if current["state"] == SolverJobState.QUEUED.value:
                current = self.repository.transition_solver_job(job_id, SolverJobState.RUNNING)
            if current["state"] == SolverJobState.RUNNING.value:
                self.repository.transition_solver_job(
                    job_id,
                    SolverJobState.FAILED,
                    payload_update={"error_code": type(exc).__name__},
                )
            raise

    def decision_board(
        self,
        analysis_id: str,
        *,
        adverse_tolerance: float = 0.0,
    ) -> dict[str, Any]:
        """Project only complete exact ensembles and direct-run adverse peak changes."""

        if adverse_tolerance < 0:
            raise ValueError("adverse_tolerance must be nonnegative")
        jobs = self._jobs(analysis_id)
        storms = sorted({job["storm_id"] for job in jobs})
        design_rows = self.repository._connection.execute(
            "SELECT design_id,vector_json,source,sequence FROM designs "
            "WHERE analysis_id=? ORDER BY sequence,design_id",
            (analysis_id,),
        ).fetchall()
        by_design = {
            row["design_id"]: [job for job in jobs if job["design_id"] == row["design_id"]]
            for row in design_rows
        }
        complete: dict[str, dict[str, Any]] = {}
        excluded: list[dict[str, Any]] = []
        for row in design_rows:
            design_jobs = by_design[row["design_id"]]
            succeeded = {
                item["storm_id"]: item
                for item in design_jobs
                if item["state"] == SolverJobState.SUCCEEDED.value
            }
            if set(succeeded) != set(storms):
                excluded.append(
                    {
                        "design_id": row["design_id"],
                        "reason": "incomplete_exact_ensemble",
                        "job_states": {item["storm_id"]: item["state"] for item in design_jobs},
                    }
                )
                continue
            storm_metrics = {
                storm_id: self._metric_records(job["job_id"])
                for storm_id, job in sorted(succeeded.items())
            }
            complete[row["design_id"]] = {
                "design_id": row["design_id"],
                "vector": json.loads(row["vector_json"]),
                "source": row["source"],
                "sequence": int(row["sequence"]),
                "job_ids": {storm_id: job["job_id"] for storm_id, job in sorted(succeeded.items())},
                "storm_metrics": storm_metrics,
                "metric_summaries": self._metric_summaries(storm_metrics),
            }

        baseline = next(
            (
                design
                for design in complete.values()
                if design["source"] == "baseline" or not any(design["vector"])
            ),
            None,
        )
        blockers: list[str] = []
        if baseline is None:
            blockers.append("No complete exact baseline ensemble is available.")
        alternatives: list[dict[str, Any]] = []
        if baseline is not None:
            for design in sorted(complete.values(), key=lambda item: item["sequence"]):
                if design["design_id"] == baseline["design_id"]:
                    continue
                design["adverse_peak_changes"] = self._adverse_peak_changes(
                    baseline,
                    design,
                    adverse_tolerance,
                )
                alternatives.append(design)
        return {
            "analysis_id": analysis_id,
            "exact_only": True,
            "storm_ids": storms,
            "baseline": baseline,
            "alternatives": alternatives,
            "excluded_designs": excluded,
            "blockers": blockers,
        }

    def status(self, analysis_id: str, *, cache_hits: int = 0) -> dict[str, Any]:
        jobs = self._jobs(analysis_id)
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["state"]] = counts.get(job["state"], 0) + 1
        return {
            "analysis_id": analysis_id,
            "state": self.repository.get_analysis(analysis_id)["state"],
            "exact": True,
            "job_count": len(jobs),
            "job_state_counts": counts,
            # A repeated call may reuse only this analysis's already-completed
            # jobs. Cross-analysis cache linking needs a repository contract that
            # does not exist yet and is deliberately not emulated here.
            "cache_hits": cache_hits,
        }

    @staticmethod
    def _storm_map(storms: Sequence[StormModel]) -> dict[str, StormModel]:
        result: dict[str, StormModel] = {}
        for storm in sorted(storms, key=lambda item: item.storm_id):
            if storm.storm_id in result:
                raise ValueError(f"duplicate storm ID {storm.storm_id!r}")
            result[storm.storm_id] = storm
        if not result:
            raise ValueError("at least one storm is required")
        return result

    @staticmethod
    def _normalized_vectors(
        slots: Sequence[CandidateSlot],
        vectors: Sequence[Sequence[int]],
        include_baseline: bool,
    ) -> list[tuple[int, ...]]:
        normalized: set[tuple[int, ...]] = set()
        for vector in vectors:
            item = tuple(vector)
            if len(item) != len(slots):
                raise ValueError("design vector length does not match candidate slots")
            for slot, value in zip(slots, item, strict=True):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError("design vectors must contain only integers")
                if not slot.min_additional_units <= value <= slot.max_additional_units:
                    raise ValueError(f"design value for {slot.slot_id!r} is outside bounds")
            normalized.add(item)
        baseline = tuple(0 for _ in slots)
        if include_baseline:
            if any(
                not slot.min_additional_units <= 0 <= slot.max_additional_units for slot in slots
            ):
                raise ValueError("candidate bounds do not permit an all-zero baseline")
            normalized.add(baseline)
        if not normalized:
            raise ValueError("at least one design is required")
        return sorted(normalized, key=lambda item: (item != baseline, item))

    @staticmethod
    def _job_id(analysis_id: str, exact_design_id: str, storm_id: str) -> str:
        payload = f"{analysis_id}|{exact_design_id}|{storm_id}".encode()
        return "job_" + hashlib.sha256(payload).hexdigest()[:20]

    def _jobs(self, analysis_id: str) -> list[dict[str, Any]]:
        rows = self.repository._connection.execute(
            "SELECT j.* FROM solver_jobs j "
            "JOIN designs d ON d.analysis_id=j.analysis_id AND d.design_id=j.design_id "
            "WHERE j.analysis_id=? ORDER BY d.sequence,j.storm_id,j.job_id",
            (analysis_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def _unfinished_jobs(self, analysis_id: str) -> list[dict[str, Any]]:
        terminal = {
            SolverJobState.SUCCEEDED.value,
            SolverJobState.QA_REJECTED.value,
            SolverJobState.FAILED.value,
            SolverJobState.CANCELLED.value,
        }
        return [job for job in self._jobs(analysis_id) if job["state"] not in terminal]

    def _succeeded_job_count(self, analysis_id: str) -> int:
        return sum(
            job["state"] == SolverJobState.SUCCEEDED.value for job in self._jobs(analysis_id)
        )

    def _cancel_analysis(self, analysis_id: str) -> None:
        for job in self._jobs(analysis_id):
            if job["state"] in {
                SolverJobState.PLANNED.value,
                SolverJobState.QUEUED.value,
            }:
                self.repository.transition_solver_job(job["job_id"], SolverJobState.CANCELLED)
        state = self.repository.get_analysis(analysis_id)["state"]
        if state in {
            AnalysisState.DRAFT.value,
            AnalysisState.VALIDATING.value,
            AnalysisState.READY.value,
            AnalysisState.RUNNING.value,
            AnalysisState.PAUSING.value,
            AnalysisState.PAUSED.value,
        }:
            self.repository.transition_analysis(analysis_id, AnalysisState.CANCELLED)

    def _fail_analysis(self, analysis_id: str) -> None:
        state = self.repository.get_analysis(analysis_id)["state"]
        if state in {
            AnalysisState.VALIDATING.value,
            AnalysisState.RUNNING.value,
            AnalysisState.PAUSING.value,
        }:
            self.repository.transition_analysis(analysis_id, AnalysisState.FAILED)

    @staticmethod
    def _read_solver_artifacts(work: Path) -> dict[str, bytes]:
        files = {
            "input": work / "input.inp",
            "report": work / "report.rpt",
            "output": work / "output.out",
        }
        return {role: path.read_bytes() for role, path in files.items() if path.is_file()}

    def _persist_artifacts(self, job_id: str, artifacts: Mapping[str, bytes]) -> None:
        existing = {
            row["role"]: row["sha256"]
            for row in self.repository._connection.execute(
                "SELECT role,sha256 FROM run_artifacts WHERE job_id=?", (job_id,)
            ).fetchall()
        }
        for role, content in artifacts.items():
            digest = hashlib.sha256(content).hexdigest()
            if role in existing:
                if existing[role] != digest:
                    raise AnalysisOrchestrationError(
                        f"persisted {role} artifact changed for exact job {job_id!r}"
                    )
                continue
            self.repository.add_run_artifact(job_id, role, content)

    def _persist_metrics(self, job_id: str, metrics: Sequence[ExtractedMetric]) -> None:
        existing = {
            (row["metric_id"], row["object_id"]): (float(row["value"]), row["units"])
            for row in self.repository._connection.execute(
                "SELECT metric_id,object_id,value,units FROM run_metrics WHERE job_id=?",
                (job_id,),
            ).fetchall()
        }
        for metric in metrics:
            key = (metric.metric_id, metric.object_id)
            if key in existing:
                if existing[key] != (metric.value, metric.units):
                    raise AnalysisOrchestrationError(
                        f"persisted exact metric changed for {job_id!r}: {key!r}"
                    )
                continue
            self.repository.add_run_metric(
                job_id,
                metric.metric_id,
                metric.value,
                metric.units,
                object_id=metric.object_id,
            )

    def _persist_qa_findings(self, job_id: str, outcome: SolverOutcome) -> None:
        existing = {
            (row["severity"], row["code"], row["payload_json"])
            for row in self.repository._connection.execute(
                "SELECT severity,code,payload_json FROM qa_findings WHERE job_id=?",
                (job_id,),
            ).fetchall()
        }
        with self.repository.transaction() as connection:
            for finding in outcome.findings:
                payload_json = json.dumps(
                    finding.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                key = (finding.severity, finding.code, payload_json)
                if key not in existing:
                    connection.execute(
                        "INSERT INTO qa_findings(job_id,severity,code,payload_json) "
                        "VALUES(?,?,?,?)",
                        (job_id, finding.severity, finding.code, payload_json),
                    )

    def _verify_outcome(
        self,
        outcome: SolverOutcome,
        artifacts: Mapping[str, bytes],
        model_bytes: bytes,
    ) -> None:
        if outcome.input_sha256 != hashlib.sha256(model_bytes).hexdigest():
            raise AnalysisOrchestrationError("solver outcome input hash does not match the model")
        if outcome.solver_sha256 != self._require_solver().solver_sha256:
            raise AnalysisOrchestrationError("solver hash changed during exact execution")
        if outcome.qa_status != "rejected":
            missing = sorted({"input", "report", "output"}.difference(artifacts))
            if missing:
                raise AnalysisOrchestrationError(
                    "successful exact job is missing artifacts: " + ", ".join(missing)
                )
        expected = {
            "report": outcome.report_sha256,
            "output": outcome.output_sha256,
        }
        for role, declared in expected.items():
            if role in artifacts and declared != hashlib.sha256(artifacts[role]).hexdigest():
                raise AnalysisOrchestrationError(
                    f"solver outcome {role} hash does not match retained bytes"
                )

    @staticmethod
    def _outcome_payload(outcome: SolverOutcome) -> dict[str, Any]:
        return {
            "input_sha256": outcome.input_sha256,
            "report_sha256": outcome.report_sha256,
            "output_sha256": outcome.output_sha256,
            "solver_sha256": outcome.solver_sha256,
            "qa_status": outcome.qa_status,
            "cache_hit": False,
        }

    @staticmethod
    def _validate_unique_metrics(metrics: Sequence[ExtractedMetric]) -> None:
        keys = [(item.metric_id, item.object_id) for item in metrics]
        if len(keys) != len(set(keys)):
            raise MetricExtractionError("the report extractor returned duplicate metric keys")

    def _metric_records(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.repository._connection.execute(
            "SELECT metric_id,object_id,value,units FROM run_metrics "
            "WHERE job_id=? ORDER BY metric_id,object_id",
            (job_id,),
        ).fetchall()
        return [
            {
                "metric_id": row["metric_id"],
                "object_id": row["object_id"],
                "value": float(row["value"]),
                "units": row["units"],
            }
            for row in rows
        ]

    @staticmethod
    def _metric_summaries(
        storm_metrics: Mapping[str, Sequence[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], list[tuple[str, float]]] = {}
        for storm_id, records in storm_metrics.items():
            for record in records:
                key = (record["metric_id"], record["object_id"], record["units"])
                grouped.setdefault(key, []).append((storm_id, record["value"]))
        summaries: list[dict[str, Any]] = []
        for (metric_id, object_id, units), values in sorted(grouped.items()):
            if "volume" in metric_id.casefold():
                aggregation = "sum_across_selected_storms"
                value = sum(item[1] for item in values)
            elif "peak" in metric_id.casefold():
                aggregation = "maximum_across_selected_storms"
                value = max(item[1] for item in values)
            else:
                aggregation = "per_storm_only"
                value = None
            summaries.append(
                {
                    "metric_id": metric_id,
                    "object_id": object_id,
                    "units": units,
                    "aggregation": aggregation,
                    "value": value,
                    "source_storms": [item[0] for item in values],
                }
            )
        return summaries

    @staticmethod
    def _adverse_peak_changes(
        baseline: Mapping[str, Any],
        design: Mapping[str, Any],
        tolerance: float,
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for storm_id, records in design["storm_metrics"].items():
            baseline_index = {
                (item["metric_id"], item["object_id"]): item
                for item in baseline["storm_metrics"][storm_id]
            }
            for record in records:
                if "peak" not in record["metric_id"].casefold():
                    continue
                key = (record["metric_id"], record["object_id"])
                reference = baseline_index.get(key)
                if reference is None or reference["units"] != record["units"]:
                    continue
                delta = record["value"] - reference["value"]
                if delta > tolerance:
                    changes.append(
                        {
                            "storm_id": storm_id,
                            "metric_id": record["metric_id"],
                            "object_id": record["object_id"],
                            "baseline_value": reference["value"],
                            "alternative_value": record["value"],
                            "delta": delta,
                            "units": record["units"],
                            "baseline_job_id": baseline["job_ids"][storm_id],
                            "alternative_job_id": design["job_ids"][storm_id],
                        }
                    )
        return changes

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        analysis_id: str,
        event: str,
        **payload: Any,
    ) -> None:
        if callback is not None:
            callback({"event": event, "analysis_id": analysis_id, **payload})
