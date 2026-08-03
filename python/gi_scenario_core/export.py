"""Deterministic evidence exports gated by an intended-use fitness passport."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .contracts import (
    SCHEMA_VERSION,
    ContractModel,
    CostBasis,
    ExactRunResult,
    ExportEligibility,
    ExportManifest,
    FitnessDisposition,
    ModelFitnessPassport,
)


class ExportGateError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class EvidenceExport(ContractModel):
    output_directory: str
    manifest: ExportManifest
    files: dict[str, str]


def validate_export(
    *,
    project_id: str,
    analysis_id: str,
    mode: Literal["learning", "screening", "verified_alternatives"],
    model_fitness_passport: ModelFitnessPassport | dict[str, Any],
    results: list[ExactRunResult | dict[str, Any]],
    include_costs: bool = False,
    cost_basis: CostBasis | dict[str, Any] | None = None,
    results_verified: bool = False,
    full_network_adverse_effects_reviewed: bool = False,
    checked_at: datetime | None = None,
) -> ExportEligibility:
    passport = ModelFitnessPassport.model_validate(model_fitness_passport)
    if passport.project_id != project_id:
        raise ValueError("Model Fitness Passport belongs to a different project")
    parsed_results = [ExactRunResult.model_validate(item) for item in results]
    blockers: list[str] = []
    warnings: list[str] = []
    if not parsed_results:
        blockers.append("Select at least one exact result before creating an evidence bundle.")
    if any(not item.exact for item in parsed_results):  # defensive for future schema changes
        blockers.append("Every exported hydraulic value must come from an exact SWMM run.")
    if any(item.qa_status == "rejected" for item in parsed_results):
        blockers.append("QA-rejected runs cannot be exported as accepted alternatives.")
    if not results_verified:
        blockers.append("Exported results must be reconstructed from the project repository.")
    if mode in {"screening", "verified_alternatives"}:
        if passport.disposition not in {
            FitnessDisposition.FIT_FOR_STATED_USE,
            FitnessDisposition.FIT_WITH_LIMITATIONS,
        }:
            blockers.append(
                "Professional exports require a Model Fitness Passport that is fit for the stated use."
            )
        # ``full_network_adverse_effects_reviewed`` is intentionally not an
        # export authority.  In schema 2.0 it is still a renderer-authored
        # DecisionBrief field; no repository record yet proves which scan was
        # reviewed, by whom, or against which immutable analysis revision.
        # Fail closed until that evidence service exists rather than allowing a
        # boolean supplied through an RPC payload to unlock a professional
        # package.
        blockers.append(
            "Professional exports are unavailable in this source MVP until a "
            "repository-backed full-network adverse-effect review record is implemented."
        )
    if mode == "verified_alternatives":
        if passport.disposition != FitnessDisposition.FIT_FOR_STATED_USE:
            blockers.append(
                "Verified-alternatives exports require fit_for_stated_use without unresolved review criteria."
            )
        # A claim whose type happens to be ``reference_comparison`` is not an
        # authoritative comparison result.  The source MVP has no persisted,
        # revision-bound comparison service, so verified exports must remain
        # impossible even when such a claim appears in a passport.
        blockers.append(
            "Verified-alternatives exports are unavailable until a repository-backed "
            "successful reference comparison is implemented."
        )
    if include_costs:
        if cost_basis is None:
            blockers.append("Cost results require a documented CostBasis.")
        elif not CostBasis.model_validate(cost_basis).confirmed:
            blockers.append("CostBasis must be explicitly confirmed before export.")
    if passport.disposition == FitnessDisposition.NOT_ASSESSED:
        warnings.append(
            "Model fitness has not been assessed; this package is limited to learning evidence."
        )
    if passport.disposition == FitnessDisposition.NOT_FIT:
        warnings.append("The model is not fit for the stated use recorded in the passport.")
    warnings.extend(passport.unresolved_limitations)
    if any(item.qa_status == "warning" for item in parsed_results):
        warnings.append("One or more exact runs contain acknowledged QA warnings.")
    required_evidence_ids = sorted(
        {
            *(claim.claim_id for claim in passport.evidence_claims),
            *(result.run_id for result in parsed_results),
        }
    )
    eligibility_seed = json.dumps(
        {
            "project_id": project_id,
            "analysis_id": analysis_id,
            "passport_id": passport.passport_id,
            "mode": mode,
            "runs": [result.run_id for result in parsed_results],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return ExportEligibility(
        eligibility_id=f"eligibility_{hashlib.sha256(eligibility_seed.encode()).hexdigest()[:24]}",
        project_id=project_id,
        analysis_id=analysis_id,
        passport_id=passport.passport_id,
        model_revision_id=passport.model_revision_id,
        fitness_disposition=passport.disposition,
        requested_package=mode,
        allowed=not blockers,
        blockers=blockers,
        warnings=warnings,
        required_evidence_ids=required_evidence_ids,
        checked_at=checked_at or datetime.now(UTC),
    )


def create_evidence_bundle(
    destination: str | Path,
    *,
    project_id: str,
    project_title: str,
    analysis_id: str,
    mode: Literal["learning", "screening", "verified_alternatives"],
    model_fitness_passport: ModelFitnessPassport | dict[str, Any],
    results: list[ExactRunResult | dict[str, Any]],
    input_hashes: dict[str, str],
    solver_sha256: str,
    extractor_version: str,
    template_version: str = "evidence-v1",
    assumptions: list[str] | None = None,
    accepted_exceptions: list[str] | None = None,
    include_costs: bool = False,
    cost_basis: CostBasis | dict[str, Any] | None = None,
    results_verified: bool = False,
    full_network_adverse_effects_reviewed: bool = False,
    attachments: dict[str, bytes] | None = None,
    created_at: datetime | None = None,
) -> EvidenceExport:
    created_at = created_at or datetime.now(UTC)
    validation = validate_export(
        mode=mode,
        project_id=project_id,
        analysis_id=analysis_id,
        model_fitness_passport=model_fitness_passport,
        results=results,
        include_costs=include_costs,
        cost_basis=cost_basis,
        results_verified=results_verified,
        full_network_adverse_effects_reviewed=full_network_adverse_effects_reviewed,
        checked_at=created_at,
    )
    if not validation.allowed:
        raise ExportGateError(validation.blockers)
    passport = ModelFitnessPassport.model_validate(model_fitness_passport)
    parsed_results = sorted(
        [ExactRunResult.model_validate(item) for item in results],
        key=lambda item: (item.design_id, item.storm_id, item.run_id),
    )
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    notice = _fitness_notice(passport)
    claims = [
        "Exact SWMM result under documented assumptions.",
        "Nondominated labels, when present, apply only among exact alternatives evaluated.",
        "Alternatives were tested against the selected storm cases.",
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "project": {"project_id": project_id, "title": project_title},
        "analysis_id": analysis_id,
        "mode": mode,
        "model_fitness_passport": passport.model_dump(mode="json"),
        "export_eligibility": validation.model_dump(mode="json"),
        "created_at": created_at.isoformat(),
        "notice": notice,
        "claims": claims,
        "warnings": validation.warnings,
        "assumptions": assumptions or [],
        "accepted_exceptions": accepted_exceptions or [],
        "results": [item.model_dump(mode="json") for item in parsed_results],
        "cost_basis": (
            CostBasis.model_validate(cost_basis).model_dump(mode="json")
            if include_costs and cost_basis is not None
            else None
        ),
    }
    content: dict[str, bytes] = {}
    content["results.json"] = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    content["results.csv"] = _results_csv(parsed_results, notice).encode("utf-8")
    markdown = _render_markdown(payload)
    content["screening-appendix.md"] = markdown.encode("utf-8")
    content["screening-appendix.html"] = _render_html(payload).encode("utf-8")
    for name, value in (attachments or {}).items():
        safe_name = _safe_attachment_name(name)
        if safe_name in content:
            raise ValueError(f"duplicate export attachment {safe_name!r}")
        content[safe_name] = value

    file_hashes = {name: hashlib.sha256(value).hexdigest() for name, value in content.items()}
    manifest = ExportManifest(
        project_id=project_id,
        analysis_id=analysis_id,
        model_revision_id=passport.model_revision_id,
        model_fitness_passport_id=passport.passport_id,
        export_eligibility_id=validation.eligibility_id,
        package_type=mode,
        created_at=created_at,
        input_hashes=dict(sorted(input_hashes.items())),
        solver_sha256=solver_sha256,
        extractor_version=extractor_version,
        template_version=template_version,
        files=dict(sorted(file_hashes.items())),
        assumptions=assumptions or [],
        accepted_exceptions=accepted_exceptions or [],
        fitness_notice=notice,
    )
    content["manifest.json"] = (
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    paths: dict[str, str] = {}
    for name in sorted(content):
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, content[name])
        paths[name] = str(path)
    return EvidenceExport(
        output_directory=str(destination),
        manifest=manifest,
        files=paths,
    )


def _results_csv(results: list[ExactRunResult], notice: str | None) -> str:
    metric_names = sorted({name for item in results for name in item.metrics})
    output = io.StringIO(newline="")
    if notice:
        output.write(f"# {notice}\r\n")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        [
            "run_id",
            "analysis_id",
            "design_id",
            "storm_id",
            "exact",
            "qa_status",
            "model_revision_id",
            "input_sha256",
            "report_sha256",
            "solver_sha256",
            "output_sha256",
            "extractor_version",
            *metric_names,
        ]
    )
    for item in results:
        writer.writerow(
            [
                item.run_id,
                item.analysis_id,
                item.design_id,
                item.storm_id,
                "true",
                item.qa_status,
                item.model_revision_id,
                item.input_sha256,
                item.report_sha256,
                item.solver_sha256,
                item.output_sha256,
                item.extractor_version,
                *(item.metrics.get(metric, "") for metric in metric_names),
            ]
        )
    return output.getvalue()


def _fitness_notice(passport: ModelFitnessPassport) -> str | None:
    if passport.disposition == FitnessDisposition.NOT_ASSESSED:
        return "Model fitness not assessed — learning evidence only."
    if passport.disposition == FitnessDisposition.NOT_FIT:
        return "Model not fit for the stated use — do not rely on this package for that decision."
    if passport.unresolved_limitations:
        return "Fit for the stated use with the limitations recorded in the Model Fitness Passport."
    return None


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# {payload['project']['title']} — alternatives screening appendix",
        "",
        (f"**Model fitness:** `{payload['model_fitness_passport']['disposition']}`  "),
        f"**Intended use:** {payload['model_fitness_passport']['intended_use']}  ",
        f"**Analysis:** `{payload['analysis_id']}`  ",
        f"**Created:** {payload['created_at']}",
        "",
    ]
    if payload["notice"]:
        lines.extend([f"> **{payload['notice']}**", ""])
    lines.extend(["## Evidence claims", ""])
    lines.extend(f"- {claim}" for claim in payload["claims"])
    lines.extend(["", "## Selected exact runs", ""])
    lines.append("| Design | Storm | QA | Exact | Metrics |")
    lines.append("|---|---|---|---|---|")
    for result in payload["results"]:
        metrics = "; ".join(f"{key}={value:g}" for key, value in sorted(result["metrics"].items()))
        lines.append(
            f"| `{result['design_id']}` | `{result['storm_id']}` | {result['qa_status']} "
            f"| yes | {metrics} |"
        )
    if payload["assumptions"]:
        lines.extend(["", "## Assumptions", ""])
        lines.extend(f"- {item}" for item in payload["assumptions"])
    if payload["accepted_exceptions"]:
        lines.extend(["", "## Accepted exceptions", ""])
        lines.extend(f"- {item}" for item in payload["accepted_exceptions"])
    return "\n".join(lines) + "\n"


def _render_html(payload: dict[str, Any]) -> str:
    notice = (
        f'<div class="notice" role="note">{html.escape(payload["notice"])}</div>'
        if payload["notice"]
        else ""
    )
    rows = []
    for result in payload["results"]:
        metrics = "<br>".join(
            f"<code>{html.escape(key)}</code> = {value:g}"
            for key, value in sorted(result["metrics"].items())
        )
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(result['design_id'])}</code></td>"
            f"<td><code>{html.escape(result['storm_id'])}</code></td>"
            f"<td>{html.escape(result['qa_status'])}</td><td>Yes</td><td>{metrics}</td>"
            "</tr>"
        )
    claims = "".join(f"<li>{html.escape(item)}</li>" for item in payload["claims"])
    assumptions = "".join(f"<li>{html.escape(item)}</li>" for item in payload["assumptions"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(payload["project"]["title"])} — screening appendix</title>
<style>
:root{{--ink:#17313A;--paper:#F5F7F6;--teal:#08766F;--ochre:#A96315}}
body{{font:16px/1.5 system-ui,sans-serif;color:var(--ink);background:var(--paper);margin:0}}
main{{max-width:1080px;margin:auto;padding:3rem}} h1,h2{{letter-spacing:.01em}}
.meta{{font-family:ui-monospace,monospace}} .notice{{border:2px solid var(--ochre);padding:1rem;margin:1.5rem 0;font-weight:700}}
table{{border-collapse:collapse;width:100%;background:white}} th,td{{border:1px solid #b9c4c4;padding:.65rem;text-align:left;vertical-align:top}}
th{{background:#e4efed}} code{{font-family:ui-monospace,monospace}}
@media print{{body{{background:white}} main{{max-width:none;padding:0}}}}
</style></head><body><main>
<h1>{html.escape(payload["project"]["title"])}</h1>
<p class="meta">Model fitness: {html.escape(payload["model_fitness_passport"]["disposition"])}<br>Intended use: {html.escape(payload["model_fitness_passport"]["intended_use"])}<br>Analysis: {html.escape(payload["analysis_id"])}<br>Created: {html.escape(payload["created_at"])}</p>
{notice}<h2>Evidence claims</h2><ul>{claims}</ul>
<h2>Selected exact runs</h2><table><thead><tr><th>Design</th><th>Storm</th><th>QA</th><th>Exact</th><th>Metrics</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<h2>Assumptions</h2><ul>{assumptions or "<li>See manifest and project assumption ledger.</li>"}</ul>
</main></body></html>"""


def _safe_attachment_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or ".." in normalized.split("/"):
        raise ValueError(f"unsafe attachment name {name!r}")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", normalized):
        raise ValueError(f"attachment name contains unsupported characters: {name!r}")
    suffix = Path(normalized).suffix.casefold()
    if suffix not in {".inp", ".rpt", ".out", ".json", ".csv", ".txt"}:
        raise ValueError(f"unsupported evidence attachment type: {suffix or '(none)'}")
    return normalized


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.partial")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
