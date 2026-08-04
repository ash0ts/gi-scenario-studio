"""Evidence-derived export eligibility and deterministic JSON/CSV packages."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from .claim_safety import (
    assert_strict_bundle_ecological_claims,
    assert_strict_ecological_claim,
)
from .contracts import (
    CloudDisposition,
    ConnectivityEvidenceClaim,
    ConnectivityExportEligibility,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    ContractModel,
    DirectConnectivityRun,
    DirectRunState,
    EngineManifest,
    PortfolioDecisionSummary,
    QAStatus,
    SemanticModelDiff,
    Sha256Digest,
)
from .cost_basis import cost_basis_requirements
from .governance import SnapshotAssetAuthorizationError, reconcile_snapshot_assets
from .metrics import (
    validate_evidence_claim,
    validate_portfolio_disposition_support,
    verify_direct_claim_against_retained_evidence,
)


class ConnectivityExportManifest(ContractModel):
    schema_version: str = "ecocorridor-export/v1.0"
    export_id: str
    project_id: str
    revision_id: str
    baseline_portfolio_id: str
    model_snapshot_sha256: Sha256Digest
    engine_manifest_sha256: Sha256Digest
    semantic_diff_sha256: Sha256Digest | None = None
    run_plan_sha256: Sha256Digest
    cost_basis_sha256: Sha256Digest
    conceptual: bool
    notice: str
    files: dict[str, str] = Field(min_length=1)
    direct_run_ids: list[str]
    evidence_claim_ids: list[str]
    artifact_files: dict[str, dict[str, str]] = Field(default_factory=dict)
    source_asset_files: dict[str, str] = Field(default_factory=dict)
    sync_state: str
    redacted_static_story: str = "decision-story.html"
    sharing_review_required: bool = True


@dataclass(frozen=True, slots=True)
class EvidenceArtifactSource:
    """One immutable, project-contained artifact retained for a direct run.

    ``source_path`` is deliberately not a public contract field.  It exists only at
    the local export boundary and must already have been resolved from the
    authoritative run-artifact table by the worker.
    """

    direct_run_id: str
    role: str
    source_path: Path
    sha256: str
    byte_size: int
    media_type: str


@dataclass(frozen=True, slots=True)
class EvidenceProjectAssetSource:
    asset_id: str
    source_path: Path
    sha256: str
    byte_size: int
    media_type: str


_EXPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_ARTIFACT_FILENAMES = {
    "request": "request.json",
    "result_protocol": "result-protocol.json",
    "matrix": "effective-resistance-matrix.tsv",
    "current_map": "diagnostic-current-map.asc",
    "input_resistance": "input-resistance.asc",
    "input_focal": "input-focal.asc",
    "input_included_pairs": "input-included-pairs.tsv",
    "circuitscape_configuration": "circuitscape.ini",
    "action_raster_diff": "action-raster-diff.json",
    "footprint_proof_document": "footprint-proofs.json",
}
_REQUIRED_DIRECT_ARTIFACT_ROLES = frozenset(
    {
        "request",
        "result_protocol",
        "matrix",
        "input_resistance",
        "input_focal",
        "input_included_pairs",
        "circuitscape_configuration",
        "action_raster_diff",
        "footprint_proof_document",
    }
)


def _artifact_filename(role: str) -> Path:
    known = _ARTIFACT_FILENAMES.get(role)
    if known is not None:
        return Path(known)
    prefix = "reviewed_footprint_source:"
    if role.startswith(prefix):
        asset_id = role.removeprefix(prefix)
        if not _OPAQUE_ID.fullmatch(asset_id):
            raise PermissionError("reviewed footprint source has an unsafe asset identity")
        return Path("reviewed-footprint-sources") / f"{asset_id}.bin"
    raise PermissionError(f"unsupported direct-run artifact role: {role}")


def _deployment_authorized(
    bundle: ConnectivityProjectBundle,
    current_authorization_bundle: ConnectivityProjectBundle,
    deployment: str,
) -> list[str]:
    blockers: list[str] = []
    try:
        current_assets = reconcile_snapshot_assets(bundle, current_authorization_bundle)
    except SnapshotAssetAuthorizationError as exc:
        return [f"{exc.asset_id}: {exc.code}: {exc}"]
    current_asset_ids = {item.asset_id for item in current_authorization_bundle.assets}
    for snapshot_asset in bundle.assets:
        asset = current_assets[snapshot_asset.asset_id]
        authorization = asset.authorization
        if deployment not in authorization.approved_deployments:
            blockers.append(f"{asset.asset_id}: deployment {deployment} is not approved")
        if (
            authorization.cloud_disposition == CloudDisposition.PROHIBITED
            and deployment != "LOCAL_ONLY"
        ):
            blockers.append(f"{asset.asset_id}: cloud replication is prohibited")
        if (
            authorization.cloud_disposition == CloudDisposition.DEDICATED_OR_SELF_MANAGED_ONLY
            and deployment == "WANDB_CLOUD"
        ):
            blockers.append(f"{asset.asset_id}: requires Dedicated or Self-Managed deployment")
        if authorization.cloud_disposition == CloudDisposition.GENERALIZED_DERIVATIVE_ONLY:
            derivative = authorization.generalized_asset_id
            if not derivative or derivative not in current_asset_ids:
                blockers.append(f"{asset.asset_id}: approved generalized derivative is absent")
            elif deployment != "LOCAL_ONLY":
                blockers.append(
                    f"{asset.asset_id}: a complete evidence package would contain the raw "
                    f"generalized-only asset; use {derivative} in an explicit export revision"
                )
        if not authorization.export_allowed:
            blockers.append(f"{asset.asset_id}: export is not authorized")
    return sorted(set(blockers))


def compute_export_eligibility(
    *,
    bundle: ConnectivityProjectBundle,
    current_authorization_bundle: ConnectivityProjectBundle,
    summaries: list[PortfolioDecisionSummary],
    runs: list[DirectConnectivityRun],
    deployment: str,
    sync_state: str,
    professional: bool,
) -> ConnectivityExportEligibility:
    fitness_blockers = [
        f"{item.key}: {item.notes}"
        for item in bundle.fitness.dimensions
        if item.status == "BLOCKING"
    ]
    authorization_blockers = (
        _deployment_authorized(bundle, current_authorization_bundle, deployment)
        if professional
        else []
    )
    if professional:
        authorization_blockers.extend(
            item.message
            for item in cost_basis_requirements(bundle.cost_basis, bundle.intervention_options)
        )
    scenario_ids = {item.scenario_id for item in bundle.scenarios}
    selected_portfolios = {item.portfolio_id for item in summaries}
    by_portfolio: dict[str, set[str]] = {item: set() for item in selected_portfolios}
    qa_blockers: list[str] = []
    exact_only = True
    for run in runs:
        if run.portfolio_id not in selected_portfolios:
            continue
        if run.scenario_id in by_portfolio[run.portfolio_id]:
            qa_blockers.append(f"{run.portfolio_id}: duplicate run for scenario {run.scenario_id}")
        by_portfolio[run.portfolio_id].add(run.scenario_id)
        accepted = (
            run.state == DirectRunState.SUCCEEDED
            and run.exact
            and not run.test_only
            and run.engine_name == "Circuitscape"
            and run.qa_status == QAStatus.ACCEPTED
            and not run.qa_findings
        )
        exact_only &= accepted
        if not accepted:
            qa_blockers.append(f"{run.direct_run_id}: not accepted direct Circuitscape evidence")
    complete = bool(selected_portfolios) and all(
        observed == scenario_ids for observed in by_portfolio.values()
    )
    if not complete:
        qa_blockers.append(
            "Every exported portfolio requires its complete target-and-safeguard ensemble."
        )
    if not runs:
        exact_only = False
    return ConnectivityExportEligibility(
        eligible=(
            complete
            and exact_only
            and not fitness_blockers
            and not authorization_blockers
            and not qa_blockers
        ),
        professional=professional,
        complete_ensemble=complete,
        exact_direct_evidence_only=exact_only,
        fitness_blockers=fitness_blockers,
        authorization_blockers=authorization_blockers,
        qa_blockers=sorted(set(qa_blockers)),
        sync_state=sync_state,
    )


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _story_next_action(disposition: str) -> str:
    return {
        "PASSES": (
            "Candidate to carry forward for ecological, field, and engineering feasibility review."
        ),
        "SURVEY_FIRST": "Survey first; resolve the recorded feasibility unknowns before comparison.",
        "FAILS": "Do not carry forward under the current screening rules; review the evidence.",
        "INCOMPLETE": "Complete every required target and safeguard scenario before comparison.",
    }[disposition]


def _redacted_story_html(*, summaries: list[PortfolioDecisionSummary], notice: str) -> str:
    """Render a script-free story that intentionally omits spatial and evidence identities."""

    rows: list[str] = []
    for index, summary in enumerate(summaries, start=1):
        worst = (
            "Not available"
            if summary.worst_resistance_reduction_percent is None
            else f"{summary.worst_resistance_reduction_percent:.2f}%"
        )
        safeguard = (
            "Not evaluated"
            if summary.safeguard_passed is None
            else ("Pass" if summary.safeguard_passed else "Review required")
        )
        rows.append(
            "<tr>"
            f'<th scope="row">Alternative {index}</th>'
            f"<td>{html.escape(summary.disposition.value.replace('_', ' ').title())}</td>"
            f"<td>{html.escape(summary.currency)} {summary.cost:,.2f}</td>"
            f"<td>{html.escape(worst)}</td>"
            f"<td>{html.escape(safeguard)}</td>"
            f"<td>{'Review triggered' if summary.review_mask_triggered else 'No trigger reported'}</td>"
            f"<td>{html.escape(_story_next_action(summary.disposition.value))}</td>"
            "</tr>"
        )
    safe_notice = html.escape(notice)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'">
  <title>Redacted connectivity decision story</title>
  <style>
    :root {{ color-scheme: light; font: 16px/1.5 system-ui,sans-serif; color:#172b2b; background:#edf2ee }}
    body {{ max-width:1100px; margin:auto; padding:2rem }} h1 {{ margin-bottom:.25rem }}
    .notice {{ border-left:.35rem solid #b56b24; padding:1rem; background:#fff8ed }}
    table {{ width:100%; border-collapse:collapse; background:white; margin:1.5rem 0 }}
    th,td {{ border:1px solid #b9c7c1; padding:.65rem; text-align:left; vertical-align:top }}
    thead th {{ background:#172b2b; color:white }} code {{ font-family:monospace }}
  </style>
</head>
<body>
  <main>
    <p><strong>Redacted static screening story</strong></p>
    <h1>Connectivity alternatives under reviewed assumptions</h1>
    <p>Direct Circuitscape result under documented assumptions. Alternative, scenario, run,
    asset, focal-area, and location identifiers are intentionally omitted from this shareable view.</p>
    <p class="notice"><strong>Required limitation.</strong> {safe_notice}</p>
    <table>
      <caption>Directly evaluated alternatives; use the local evidence package for audit identities.</caption>
      <thead><tr><th>Alternative</th><th>Rule disposition</th><th>Screening cost</th><th>Worst modeled resistance reduction</th><th>Safeguard</th><th>Diagnostic mask</th><th>Next action</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <p><strong>Before sharing:</strong> a responsible reviewer must confirm that the remaining
    costs and ecological summaries are authorized for the intended audience.</p>
  </main>
</body>
</html>
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verified_tree_hashes(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise PermissionError("evidence export directory is absent or unsafe")
    output: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode):
            raise PermissionError("evidence export contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("evidence export contains a non-regular file")
        output[path.relative_to(root).as_posix()] = _sha256(path)
    return output


def _copy_verified_artifact(
    source: EvidenceArtifactSource | EvidenceProjectAssetSource, destination: Path
) -> None:
    """Copy without following links while verifying bytes during the copy."""

    metadata = os.lstat(source.source_path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PermissionError("a retained direct-run artifact is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(source.source_path, flags)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    byte_count = 0
    try:
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("a retained direct-run artifact changed during export")
        with (
            os.fdopen(source_descriptor, "rb", closefd=True) as input_stream,
            os.fdopen(temporary_descriptor, "wb", closefd=True) as output_stream,
        ):
            source_descriptor = -1
            temporary_descriptor = -1
            for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                digest.update(block)
                byte_count += len(block)
                output_stream.write(block)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if digest.hexdigest() != source.sha256 or byte_count != source.byte_size:
            raise PermissionError("a retained direct-run artifact failed hash verification")
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        temporary.unlink(missing_ok=True)


def _materialize_direct_artifacts(
    *,
    export_directory: Path,
    runs: list[DirectConnectivityRun],
    sources: list[EvidenceArtifactSource],
) -> dict[str, dict[str, str]]:
    selected = {item.direct_run_id: item for item in runs}
    if len(selected) != len(runs) or any(not _OPAQUE_ID.fullmatch(item) for item in selected):
        raise PermissionError("direct-run evidence identities must be unique safe opaque IDs")
    if not sources:
        raise PermissionError("an evidence package requires retained direct-run artifacts")
    grouped: dict[str, dict[str, EvidenceArtifactSource]] = {
        direct_run_id: {} for direct_run_id in selected
    }
    for source in sources:
        if source.direct_run_id not in selected:
            raise PermissionError("an artifact belongs to a run outside the export package")
        _artifact_filename(source.role)
        if source.role in grouped[source.direct_run_id]:
            raise PermissionError("a direct run contains duplicate artifact roles")
        if not re.fullmatch(r"[a-f0-9]{64}", source.sha256):
            raise PermissionError("a retained direct-run artifact has an invalid SHA-256")
        if source.byte_size < 0 or not source.media_type:
            raise PermissionError("a retained direct-run artifact has invalid metadata")
        grouped[source.direct_run_id][source.role] = source

    artifact_index: dict[str, dict[str, str]] = {}
    for direct_run_id, run in sorted(selected.items()):
        observed = set(grouped[direct_run_id])
        required = set(_REQUIRED_DIRECT_ARTIFACT_ROLES)
        if run.current_map_sha256 is not None:
            required.add("current_map")
        missing = required - observed
        if missing:
            raise PermissionError(
                f"direct run {direct_run_id} omits retained artifacts: {sorted(missing)}"
            )
        expected_hashes = {
            "request": run.input_sha256,
            "result_protocol": run.result_sha256,
            "matrix": run.matrix_sha256,
            "current_map": run.current_map_sha256,
        }
        for role, expected_hash in expected_hashes.items():
            if expected_hash is not None and (
                role not in grouped[direct_run_id]
                or grouped[direct_run_id][role].sha256 != expected_hash
            ):
                raise PermissionError(
                    f"direct run {direct_run_id} {role} artifact differs from its result contract"
                )
        artifact_index[direct_run_id] = {}
        for role, source in sorted(grouped[direct_run_id].items()):
            relative = Path("artifacts") / direct_run_id / _artifact_filename(role)
            _copy_verified_artifact(source, export_directory / relative)
            artifact_index[direct_run_id][role] = relative.as_posix()
    return artifact_index


def _materialize_project_assets(
    *,
    export_directory: Path,
    bundle: ConnectivityProjectBundle,
    sources: list[EvidenceProjectAssetSource],
) -> dict[str, str]:
    assets = {item.asset_id: item for item in bundle.assets}
    if len(assets) != len(bundle.assets):
        raise PermissionError("project source-asset identities are ambiguous")
    by_id = {item.asset_id: item for item in sources}
    if len(by_id) != len(sources) or set(by_id) != set(assets):
        raise PermissionError("the evidence package requires every retained project source asset")
    output: dict[str, str] = {}
    for asset_id, asset in sorted(assets.items()):
        if not _OPAQUE_ID.fullmatch(asset_id):
            raise PermissionError("project asset evidence identities must be safe opaque IDs")
        source = by_id[asset_id]
        if (
            source.sha256 != asset.sha256
            or source.byte_size != asset.byte_size
            or source.media_type != asset.media_type
        ):
            raise PermissionError(f"source asset {asset_id} differs from its reviewed contract")
        suffix = Path(asset.relative_path).suffix.casefold()
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            suffix = ".bin"
        relative = Path("source-assets") / asset_id / f"asset{suffix}"
        _copy_verified_artifact(source, export_directory / relative)
        output[asset_id] = relative.as_posix()
    return output


def create_evidence_export(
    *,
    output_root: Path,
    export_id: str,
    baseline_portfolio_id: str,
    bundle: ConnectivityProjectBundle,
    current_authorization_bundle: ConnectivityProjectBundle,
    deployment: str,
    engine_manifest: EngineManifest,
    run_plan: ConnectivityRunPlan,
    semantic_diff: SemanticModelDiff | None,
    eligibility: ConnectivityExportEligibility,
    summaries: list[PortfolioDecisionSummary],
    runs: list[DirectConnectivityRun],
    claims: list[ConnectivityEvidenceClaim],
    artifact_sources: list[EvidenceArtifactSource],
    project_asset_sources: list[EvidenceProjectAssetSource],
) -> Path:
    if not eligibility.eligible:
        blockers = (
            eligibility.fitness_blockers
            + eligibility.authorization_blockers
            + eligibility.qa_blockers
        )
        raise PermissionError(f"evidence export is blocked: {blockers}")
    if eligibility.professional:
        current_blockers = _deployment_authorized(
            bundle,
            current_authorization_bundle,
            deployment,
        )
        if current_blockers:
            raise PermissionError(
                "evidence export authorization changed after eligibility was computed: "
                f"{current_blockers}"
            )
    assert_strict_bundle_ecological_claims(bundle)
    for summary in summaries:
        assert_strict_ecological_claim(summary.next_action)
    if not claims:
        raise PermissionError("an evidence package requires resolved decision and numeric claims")
    for claim in claims:
        validate_evidence_claim(claim)
    try:
        validate_portfolio_disposition_support(
            summaries=summaries,
            runs=runs,
            claims=claims,
        )
    except ValueError as exc:
        raise PermissionError(str(exc)) from exc
    scenario_ids = {item.scenario_id for item in bundle.scenarios}
    selected_ids = {item.portfolio_id for item in summaries}
    run_ids = {item.direct_run_id for item in runs}
    if len(run_ids) != len(runs) or any(not _OPAQUE_ID.fullmatch(item) for item in run_ids):
        raise PermissionError("direct-run evidence identities must be unique safe opaque IDs")
    run_coverage = {
        portfolio_id: {item.scenario_id for item in runs if item.portfolio_id == portfolio_id}
        for portfolio_id in selected_ids
    }
    if any(coverage != scenario_ids for coverage in run_coverage.values()):
        raise PermissionError("export evidence changed after eligibility was computed")
    if any(set(claim.direct_run_ids) - run_ids for claim in claims):
        raise PermissionError("an evidence claim references a run outside the export package")
    runs_by_id = {item.direct_run_id: item for item in runs}
    artifact_paths = {
        (item.direct_run_id, item.role): item.source_path for item in artifact_sources
    }
    if len(artifact_paths) != len(artifact_sources):
        raise PermissionError("direct evidence contains duplicate artifact roles")
    asset_paths = {item.asset_id: item.source_path for item in project_asset_sources}
    if len(asset_paths) != len(project_asset_sources):
        raise PermissionError("project evidence contains duplicate asset identities")
    for claim in claims:
        if claim.portfolio_id not in selected_ids and not (
            claim.claim_kind == "DIRECT_QA" and claim.portfolio_id == baseline_portfolio_id
        ):
            raise PermissionError("an evidence claim belongs to an unselected portfolio")
        claim_runs = [runs_by_id[run_id] for run_id in claim.direct_run_ids]
        if claim.claim_kind in {"DIRECT_PAIR_METRIC", "DIRECT_MAP_DIAGNOSTIC"}:
            try:
                verify_direct_claim_against_retained_evidence(
                    claim=claim,
                    bundle=bundle,
                    baseline_portfolio_id=baseline_portfolio_id,
                    runs_by_id=runs_by_id,
                    artifact_paths=artifact_paths,
                    asset_paths=asset_paths,
                )
            except ValueError as exc:
                raise PermissionError(str(exc)) from exc
            if claim.scenario_id is None or any(
                item.scenario_id != claim.scenario_id for item in claim_runs
            ):
                raise PermissionError("direct numeric claim crosses ecological scenarios")
            subject_runs = [item for item in claim_runs if item.portfolio_id == claim.portfolio_id]
            baseline_runs = [
                item for item in claim_runs if item.portfolio_id == baseline_portfolio_id
            ]
            expected = {item.direct_run_id for item in subject_runs + baseline_runs}
            if (
                len(subject_runs) != 1
                or len(baseline_runs) != 1
                or set(claim.direct_run_ids) != expected
            ):
                raise PermissionError(
                    "direct metric claim must include one subject run and its baseline"
                )
        elif claim.claim_kind == "DIRECT_QA":
            if (
                claim.scenario_id is None
                or len(claim_runs) != 1
                or claim_runs[0].portfolio_id != claim.portfolio_id
                or claim_runs[0].scenario_id != claim.scenario_id
            ):
                raise PermissionError("QA claim must bind one subject direct run")
        elif claim.claim_kind in {"COST", "FEASIBILITY"} and claim.direct_run_ids:
            raise PermissionError("decision-source claim cannot masquerade as solver evidence")
        elif claim.claim_kind == "PORTFOLIO_DISPOSITION" and any(
            item.portfolio_id != claim.portfolio_id for item in claim_runs
        ):
            raise PermissionError("portfolio disposition references another portfolio's run")
    if any(item.engine_manifest_sha256 != engine_manifest.sha256 for item in runs):
        raise PermissionError("direct-run evidence differs from the exported EngineManifest")
    if bundle.revision.diff_id is not None:
        if semantic_diff is None or semantic_diff.diff_id != bundle.revision.diff_id:
            raise PermissionError("the active semantic model diff is absent from the export")
    elif semantic_diff is not None:
        raise PermissionError("an unrelated semantic model diff cannot enter the export")
    if run_plan.revision_id != bundle.revision.revision_id:
        raise PermissionError("export run plan differs from the active model revision")
    selected_plan_ids = {item.portfolio_id for item in run_plan.portfolios}
    if not selected_ids.issubset(selected_plan_ids):
        raise PermissionError("export selection is absent from the immutable run plan")
    if bundle.cost_basis is None:
        raise PermissionError("export requires a reviewed CostBasis")
    claims_by_id = {item.claim_id: item for item in claims}
    qa_claims_by_run: dict[str, list[ConnectivityEvidenceClaim]] = {
        run_id: [] for run_id in run_ids
    }
    for claim in claims:
        if claim.claim_kind == "DIRECT_QA" and len(claim.direct_run_ids) == 1:
            qa_claims_by_run.setdefault(claim.direct_run_ids[0], []).append(claim)
    for run in runs:
        qa_claims = qa_claims_by_run.get(run.direct_run_id, [])
        if (
            len(qa_claims) != 1
            or qa_claims[0].value is not True
            or qa_claims[0].units != "boolean"
            or run.qa_status != QAStatus.ACCEPTED
            or run.qa_findings
        ):
            raise PermissionError("every exported direct run requires one accepted QA claim")

    portfolios_by_id = {item.portfolio_id: item for item in run_plan.portfolios}
    for summary in summaries:
        disposition_claim = claims_by_id.get(summary.disposition_evidence_claim_id or "")
        if (
            disposition_claim is None
            or disposition_claim.claim_kind != "PORTFOLIO_DISPOSITION"
            or disposition_claim.portfolio_id != summary.portfolio_id
            or disposition_claim.value != summary.disposition.value
            or set(disposition_claim.direct_run_ids) != set(summary.direct_run_ids)
        ):
            raise PermissionError("portfolio disposition lacks an evidence claim")
        expected_summary_runs = {
            item.direct_run_id for item in runs if item.portfolio_id == summary.portfolio_id
        }
        if set(summary.direct_run_ids) != expected_summary_runs:
            raise PermissionError("portfolio summary omits part of its direct scenario ensemble")
        planned_portfolio = portfolios_by_id[summary.portfolio_id]
        selected_options = list(planned_portfolio.canonical_option_ids)
        try:
            expected_cost = sum(bundle.cost_basis.option_costs[item] for item in selected_options)
        except KeyError as exc:
            raise PermissionError("portfolio cost has no reviewed CostBasis entry") from exc
        if summary.currency != bundle.cost_basis.currency or not math.isclose(
            summary.cost, expected_cost, rel_tol=0.0, abs_tol=1e-9
        ):
            raise PermissionError("portfolio cost differs from its reviewed CostBasis")
        cost_claims = [
            item
            for item in claims
            if item.claim_kind == "COST" and item.portfolio_id == summary.portfolio_id
        ]
        if (
            len(cost_claims) != 1
            or cost_claims[0].units != summary.currency
            or not isinstance(cost_claims[0].value, (int, float))
            or not math.isclose(
                float(cost_claims[0].value), summary.cost, rel_tol=0.0, abs_tol=1e-9
            )
            or not any(
                node.node_type == "COST_BASIS" and node.sha256 == bundle.cost_basis.sha256
                for node in cost_claims[0].nodes
            )
        ):
            raise PermissionError("portfolio cost lacks exact reviewed-basis evidence")
        for disposition in summary.rule_dispositions:
            rule_claim = claims_by_id.get(disposition.evidence_claim_id or "")
            if (
                rule_claim is None
                or rule_claim.portfolio_id != summary.portfolio_id
                or not any(
                    node.node_type == "RULE" and node.node_id == f"rule:{disposition.rule_id}"
                    for node in rule_claim.nodes
                )
                or rule_claim.scenario_id != disposition.scenario_id
                or rule_claim.pair_id != disposition.pair_id
            ):
                raise PermissionError("a reported rule disposition lacks an evidence claim")
            observed = disposition.observed_value
            if isinstance(observed, (int, float)) and not isinstance(observed, bool):
                if (
                    not isinstance(rule_claim.value, (int, float))
                    or isinstance(rule_claim.value, bool)
                    or not math.isclose(
                        float(rule_claim.value), float(observed), rel_tol=0.0, abs_tol=1e-9
                    )
                ):
                    raise PermissionError("rule claim value differs from its disposition")
            elif rule_claim.value != observed:
                raise PermissionError("rule claim value differs from its disposition")
    if not _EXPORT_ID.fullmatch(export_id):
        raise ValueError("export_id must be a safe opaque identifier")
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink() or not output_root.is_dir():
        raise PermissionError("the authoritative export root is unsafe")
    export_directory = output_root / export_id
    if export_directory.is_symlink():
        raise PermissionError("the authoritative export target is a symlink")
    staging_directory = Path(tempfile.mkdtemp(prefix=f".{export_id}.", dir=output_root))
    notice = (
        eligibility.required_notice
        if bundle.decision_brief.conceptual
        else (
            "Connectivity screening is not observed animal movement, crossing use, collision "
            "reduction, population viability, an approved corridor, or a design recommendation."
        )
    )
    decision_path = staging_directory / "decision-board.json"
    evidence_path = staging_directory / "evidence-claims.json"
    runs_path = staging_directory / "direct-runs.json"
    table_path = staging_directory / "decision-table.csv"
    story_path = staging_directory / "decision-story.html"
    model_path = staging_directory / "connectivity-project-snapshot.json"
    engine_path = staging_directory / "engine-manifest.json"
    semantic_diff_path = staging_directory / "semantic-model-diff.json"
    run_plan_path = staging_directory / "analysis-run-plan.json"
    cost_basis_path = staging_directory / "cost-basis.json"
    try:
        artifact_index = _materialize_direct_artifacts(
            export_directory=staging_directory,
            runs=runs,
            sources=artifact_sources,
        )
        source_asset_index = _materialize_project_assets(
            export_directory=staging_directory,
            bundle=bundle,
            sources=project_asset_sources,
        )
        _atomic_json(
            model_path,
            bundle.model_dump(mode="json"),
        )
        _atomic_json(
            engine_path,
            engine_manifest.model_dump(mode="json"),
        )
        _atomic_json(run_plan_path, run_plan.model_dump(mode="json"))
        _atomic_json(cost_basis_path, bundle.cost_basis.model_dump(mode="json"))
        if semantic_diff is not None:
            _atomic_json(
                semantic_diff_path,
                semantic_diff.model_dump(mode="json"),
            )
        _atomic_json(
            decision_path,
            {
                "schema_version": "ecocorridor/v1.0",
                "project_id": bundle.project_id,
                "revision_id": bundle.revision.revision_id,
                "notice": notice,
                "summaries": [item.model_dump(mode="json") for item in summaries],
            },
        )
        _atomic_json(
            evidence_path,
            {
                "notice": notice,
                "claims": [item.model_dump(mode="json") for item in claims],
            },
        )
        _atomic_json(
            runs_path,
            {
                "notice": notice,
                "runs": [item.model_dump(mode="json") for item in runs],
            },
        )
        with table_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "portfolio_id",
                    "disposition",
                    "cost",
                    "currency",
                    "controlling_scenario_id",
                    "controlling_pair_id",
                    "worst_resistance_reduction_percent",
                    "safeguard_passed",
                    "review_mask_triggered",
                    "next_action",
                    "notice",
                ],
            )
            writer.writeheader()
            for summary in summaries:
                writer.writerow(
                    {
                        "portfolio_id": summary.portfolio_id,
                        "disposition": summary.disposition.value,
                        "cost": summary.cost,
                        "currency": summary.currency,
                        "controlling_scenario_id": summary.controlling_scenario_id,
                        "controlling_pair_id": summary.controlling_pair_id,
                        "worst_resistance_reduction_percent": summary.worst_resistance_reduction_percent,
                        "safeguard_passed": summary.safeguard_passed,
                        "review_mask_triggered": summary.review_mask_triggered,
                        "next_action": summary.next_action,
                        "notice": notice,
                    }
                )
            stream.flush()
            os.fsync(stream.fileno())
        _atomic_text(
            story_path,
            _redacted_story_html(summaries=summaries, notice=notice),
        )
        files = {
            path.relative_to(staging_directory).as_posix(): _sha256(path)
            for path in sorted(staging_directory.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
        manifest = ConnectivityExportManifest(
            export_id=export_id,
            project_id=bundle.project_id,
            revision_id=bundle.revision.revision_id,
            baseline_portfolio_id=baseline_portfolio_id,
            model_snapshot_sha256=_sha256(model_path),
            engine_manifest_sha256=engine_manifest.sha256,
            semantic_diff_sha256=(semantic_diff.sha256 if semantic_diff is not None else None),
            run_plan_sha256=_sha256(run_plan_path),
            cost_basis_sha256=bundle.cost_basis.sha256,
            conceptual=bundle.decision_brief.conceptual,
            notice=notice,
            files=files,
            direct_run_ids=sorted(item.direct_run_id for item in runs),
            evidence_claim_ids=sorted(item.claim_id for item in claims),
            artifact_files=artifact_index,
            source_asset_files=source_asset_index,
            sync_state=eligibility.sync_state,
        )
        manifest_path = staging_directory / "manifest.json"
        _atomic_json(manifest_path, manifest.model_dump(mode="json"))
        if export_directory.exists():
            if _verified_tree_hashes(export_directory) != _verified_tree_hashes(staging_directory):
                raise FileExistsError(
                    "an export with this ID exists but differs from the requested evidence"
                )
            shutil.rmtree(staging_directory)
        else:
            try:
                os.rename(staging_directory, export_directory)
            except FileExistsError:
                if _verified_tree_hashes(export_directory) != _verified_tree_hashes(
                    staging_directory
                ):
                    raise
                shutil.rmtree(staging_directory)
    except Exception:
        shutil.rmtree(staging_directory, ignore_errors=True)
        raise
    return export_directory
