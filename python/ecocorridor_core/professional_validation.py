"""Cross-contract validation for professional connectivity projects.

Pydantic validates individual records and their immediate references.  A
professional analysis needs stronger, repository-derived checks across the
complete model: exact raster geometry, ecological scenario semantics,
safeguard coverage, mutually compatible intervention effects, and decision
rules.  These checks never repair or reinterpret an input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from .claim_safety import assert_strict_ecological_claim
from .contracts import (
    AssetKind,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    InterventionKind,
    InterventionOption,
    InterventionSite,
    PortfolioSelection,
    RasterValueKind,
    ReferenceComparisonResult,
    RuleMetric,
    RuleOperator,
    canonical_sha256,
)
from .grid import grid_identity
from .interventions import action_effect_sha256
from .metrics import iter_bundle_ecological_claim_texts

FindingSeverity = Literal["BLOCKING", "REVIEW_REQUIRED", "INFORMATION"]


@dataclass(frozen=True, slots=True)
class ModelFinding:
    code: str
    severity: FindingSeverity
    object_reference: str
    remediation: str

    def model_dump(self) -> dict[str, str]:
        return asdict(self)


_FITNESS_DIMENSIONS = {
    "intended_use",
    "movement_process",
    "focal_rationale",
    "resistance_derivation",
    "validation_evidence",
    "spatial_scale",
    "boundary_effects",
    "action_effect_evidence",
    "scenario_coverage",
    "sensitivity",
    "numerical_qa",
    "unresolved_assumptions",
}

_UNBOUND_REFERENCE_ASSET_KINDS = {
    AssetKind.REFERENCE_MATRIX,
    AssetKind.REFERENCE_CURRENT,
}


def reference_scientific_fingerprints(
    result: ReferenceComparisonResult,
) -> dict[str, str]:
    """Return per-binding identities that ignore workflow/run bookkeeping.

    Repeated executions with byte-identical scientific evidence collapse to the
    same fingerprint. Any change in the reviewed/observed matrices, compared
    pair values, tolerance, or scientific disposition remains distinguishable.
    """

    return {
        comparison.binding_id: canonical_sha256(
            {
                "revision_id": result.revision_id,
                "model_sha256": result.model_sha256,
                "engine_manifest_sha256": result.engine_manifest_sha256,
                "binding_id": comparison.binding_id,
                "scenario_id": comparison.scenario_id,
                "reference_matrix_sha256": comparison.reference_matrix_sha256,
                "observed_matrix_sha256": comparison.observed_matrix_sha256,
                "tolerance": comparison.tolerance.model_dump(mode="json"),
                "status": comparison.status,
                "pair_comparisons": [
                    item.model_dump(mode="json")
                    for item in sorted(
                        comparison.pair_comparisons,
                        key=lambda item: item.pair_id,
                    )
                ],
            }
        )
        for comparison in result.scenario_comparisons
    }


def _finding(
    code: str,
    object_reference: str,
    remediation: str,
    *,
    severity: FindingSeverity = "BLOCKING",
) -> ModelFinding:
    return ModelFinding(code, severity, object_reference, remediation)


def _require_nonblank(
    findings: list[ModelFinding], value: str, code: str, object_reference: str, label: str
) -> None:
    if not value.strip():
        findings.append(_finding(code, object_reference, f"Enter the reviewed {label}."))


def _require_safe_purpose(
    findings: list[ModelFinding], value: str, object_reference: str, label: str
) -> None:
    try:
        assert_strict_ecological_claim(value)
    except ValueError:
        findings.append(
            _finding(
                "PROHIBITED_ECOLOGICAL_PURPOSE",
                object_reference,
                (
                    f"Rewrite the {label} as a connectivity-screening question. Do not claim "
                    "animal movement, crossing use, collision reduction, population or "
                    "biodiversity benefit, regulatory status, approval, optimality, or design "
                    "readiness."
                ),
            )
        )


def _duplicate_identities(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        else:
            seen.add(value)
    return duplicates


def validate_professional_bundle(
    bundle: ConnectivityProjectBundle,
    *,
    authoritative_reference_results: Sequence[ReferenceComparisonResult] = (),
    expected_engine_manifest_sha256: str | None = None,
) -> list[ModelFinding]:
    """Return deterministic findings for a complete professional bundle.

    The function deliberately accumulates findings instead of stopping at the
    first one so the UI can take an engineer directly to each corrective
    action.  It does not mutate the bundle or approve an assumption.
    """

    findings: list[ModelFinding] = []
    assets = {item.asset_id: item for item in bundle.assets}
    hypotheses = {item.hypothesis_id: item for item in bundle.resistance_hypotheses}
    focal_sets = {item.focal_set_id: item for item in bundle.focal_area_sets}
    scenarios = {item.scenario_id: item for item in bundle.scenarios}
    options = {item.option_id: item for item in bundle.intervention_options}

    for value, label in (
        (bundle.decision_brief.title, "decision title"),
        (bundle.decision_brief.decision_question, "decision question"),
        (bundle.decision_brief.decision_owner, "decision owner"),
        (bundle.decision_brief.intended_use, "intended use"),
        (bundle.decision_brief.required_next_action, "required next action"),
    ):
        _require_nonblank(
            findings,
            value,
            "DECISION_BRIEF_INCOMPLETE",
            bundle.decision_brief.brief_id,
            label,
        )
    for reference, value in iter_bundle_ecological_claim_texts(bundle):
        _require_safe_purpose(
            findings,
            value,
            reference,
            reference,
        )
    if bundle.decision_brief.budget_amount is not None and (
        not bundle.decision_brief.currency or not bundle.decision_brief.cost_vintage
    ):
        findings.append(
            _finding(
                "DECISION_COST_BASIS_INCOMPLETE",
                bundle.decision_brief.brief_id,
                "Add currency and cost vintage for the screening budget.",
            )
        )

    observed_dimensions = {item.key for item in bundle.fitness.dimensions}
    for missing in sorted(_FITNESS_DIMENSIONS - observed_dimensions):
        findings.append(
            _finding(
                "FITNESS_DIMENSION_MISSING",
                missing,
                "Complete this independent Model Fitness Passport dimension.",
            )
        )
    for dimension in bundle.fitness.dimensions:
        if dimension.status == "BLOCKING":
            findings.append(_finding("FITNESS_BLOCKER", dimension.key, dimension.notes))
        elif dimension.status == "REVIEW_REQUIRED":
            findings.append(
                _finding(
                    "ECOLOGIST_REVIEW_REQUIRED",
                    dimension.key,
                    dimension.notes,
                    severity="REVIEW_REQUIRED",
                )
            )
    if (
        bundle.fitness.reviewed_by is None
        or bundle.fitness.reviewed_at is None
        or not bundle.fitness.attestations
    ):
        findings.append(
            _finding(
                "MODEL_FITNESS_ATTESTATION_MISSING",
                bundle.fitness.passport_id,
                "Complete the named Model Fitness Passport attestation without overriding blockers.",
            )
        )
    if bundle.fitness.revision_id != bundle.revision.revision_id:
        findings.append(
            _finding(
                "MODEL_FITNESS_REVISION_MISMATCH",
                bundle.fitness.passport_id,
                "Review and attest the Model Fitness Passport for the active semantic revision.",
            )
        )

    for asset in bundle.assets:
        authorization = asset.authorization
        for value, label in (
            (asset.title, "asset title"),
            (asset.source, "asset source"),
            (asset.vintage, "asset vintage"),
            (asset.license, "asset license"),
            (asset.media_type, "asset media type"),
        ):
            _require_nonblank(
                findings,
                value,
                "ASSET_PROVENANCE_INCOMPLETE",
                asset.asset_id,
                label,
            )
        if authorization.asset_id != asset.asset_id:
            findings.append(
                _finding(
                    "ASSET_AUTHORIZATION_ID_MISMATCH",
                    asset.asset_id,
                    "Create an authorization record for these exact asset bytes.",
                )
            )
        if authorization.license_spdx_or_name != asset.license:
            findings.append(
                _finding(
                    "ASSET_LICENSE_AUTHORIZATION_MISMATCH",
                    asset.asset_id,
                    "Reconcile the source license with the reviewed cloud/export authorization.",
                )
            )
        for value, label in (
            (authorization.ownership, "ownership"),
            (authorization.license_spdx_or_name, "license or use terms"),
            (authorization.retention_policy, "retention policy"),
            (authorization.spatial_redaction, "spatial-redaction decision"),
            (authorization.reviewer, "authorization reviewer"),
        ):
            _require_nonblank(
                findings,
                value,
                "ASSET_AUTHORIZATION_INCOMPLETE",
                asset.asset_id,
                label,
            )

    grid_identities: dict[str, dict[str, list[str]]] = {}
    for asset in bundle.assets:
        if asset.grid is None:
            continue
        grid_identities.setdefault(asset.grid.grid_id, {}).setdefault(
            grid_identity(asset.grid), []
        ).append(asset.asset_id)
    for grid_id, identities in sorted(grid_identities.items()):
        if len(identities) <= 1:
            continue
        affected_assets = sorted(
            asset_id for asset_ids in identities.values() for asset_id in asset_ids
        )
        findings.append(
            _finding(
                "GRID_ID_GEOMETRY_CONFLICT",
                grid_id,
                (
                    "Assign a new grid ID whenever CRS, extent, dimensions, affine transform, "
                    "or cell area changes, then preview and approve any explicit alignment. "
                    "The same grid ID currently names different cell geometries for: "
                    f"{', '.join(affected_assets)}."
                ),
            )
        )
    reference_bindings = list(bundle.reference_bindings)
    bound_reference_asset_ids = {
        binding.reference_matrix_asset_id for binding in reference_bindings
    } | {
        binding.reference_current_asset_id
        for binding in reference_bindings
        if binding.reference_current_asset_id is not None
    }
    for asset in bundle.assets:
        if (
            asset.kind in _UNBOUND_REFERENCE_ASSET_KINDS
            and asset.asset_id not in bound_reference_asset_ids
        ):
            findings.append(
                _finding(
                    "REFERENCE_ASSET_UNBOUND",
                    asset.asset_id,
                    (
                        "Bind these exact reference bytes to one reviewed scenario, metric "
                        "tolerance, basis, reviewer, and limitations. Asset presence alone is "
                        "not model-validation evidence."
                    ),
                )
            )

    duplicate_binding_ids = _duplicate_identities(
        [binding.binding_id for binding in reference_bindings]
    )
    duplicate_scenario_ids = _duplicate_identities(
        [binding.scenario_id for binding in reference_bindings]
    )
    duplicate_matrix_asset_ids = _duplicate_identities(
        [binding.reference_matrix_asset_id for binding in reference_bindings]
    )
    duplicate_current_asset_ids = _duplicate_identities(
        [
            binding.reference_current_asset_id
            for binding in reference_bindings
            if binding.reference_current_asset_id is not None
        ]
    )
    for label, identities in (
        ("binding ID", duplicate_binding_ids),
        ("scenario", duplicate_scenario_ids),
        ("reference matrix asset", duplicate_matrix_asset_ids),
        ("reference current asset", duplicate_current_asset_ids),
    ):
        for identity in sorted(identities):
            findings.append(
                _finding(
                    "REFERENCE_BINDING_DUPLICATE",
                    identity,
                    f"Assign each {label} to exactly one reference-scenario binding.",
                )
            )

    active_binding_ids = {item.binding_id for item in reference_bindings}
    exact_results: list[ReferenceComparisonResult] = []
    for result in authoritative_reference_results:
        if (
            result.revision_id != bundle.revision.revision_id
            or result.model_sha256 != bundle.revision.model_sha256
            or (
                expected_engine_manifest_sha256 is not None
                and result.engine_manifest_sha256 != expected_engine_manifest_sha256
            )
            or not result.exact_direct_evidence_only
            or result.current_raster_numeric_comparison_performed
        ):
            continue
        comparisons = {item.binding_id: item for item in result.scenario_comparisons}
        if set(comparisons) != active_binding_ids:
            continue
        exact_binding_identity = True
        for binding in reference_bindings:
            comparison = comparisons[binding.binding_id]
            matrix_asset = assets.get(binding.reference_matrix_asset_id)
            current_asset = (
                assets.get(binding.reference_current_asset_id)
                if binding.reference_current_asset_id is not None
                else None
            )
            if (
                comparison.scenario_id != binding.scenario_id
                or comparison.binding_id != binding.binding_id
                or comparison.reference_matrix_asset_id != binding.reference_matrix_asset_id
                or matrix_asset is None
                or comparison.reference_matrix_sha256 != matrix_asset.sha256
                or comparison.reference_current_asset_id != binding.reference_current_asset_id
                or comparison.reference_current_sha256
                != (current_asset.sha256 if current_asset is not None else None)
                or comparison.tolerance != binding.tolerance
                or comparison.revision_id != result.revision_id
                or comparison.model_sha256 != result.model_sha256
                or comparison.engine_manifest_sha256 != result.engine_manifest_sha256
            ):
                exact_binding_identity = False
                break
        if exact_binding_identity:
            exact_results.append(result)
    passed_reference_result = next(
        (item for item in exact_results if item.status == "PASSED"),
        None,
    )
    failed_reference_result = next(
        (item for item in exact_results if item.status == "FAILED"),
        None,
    )
    status_conflict = passed_reference_result is not None and failed_reference_result is not None
    fingerprints_by_binding: dict[str, set[str]] = {
        binding_id: set() for binding_id in active_binding_ids
    }
    for result in exact_results:
        for binding_id, fingerprint in reference_scientific_fingerprints(result).items():
            fingerprints_by_binding.setdefault(binding_id, set()).add(fingerprint)
    scientific_conflict = any(len(items) > 1 for items in fingerprints_by_binding.values())
    conflicting_reference_results = status_conflict or scientific_conflict

    valid_bound_scenario_ids: set[str] = set()
    for binding in reference_bindings:
        binding_valid = True
        if (
            binding.binding_id in duplicate_binding_ids
            or binding.scenario_id in duplicate_scenario_ids
            or binding.reference_matrix_asset_id in duplicate_matrix_asset_ids
            or (
                binding.reference_current_asset_id is not None
                and binding.reference_current_asset_id in duplicate_current_asset_ids
            )
        ):
            binding_valid = False

        scenario = scenarios.get(binding.scenario_id)
        if scenario is None:
            binding_valid = False
            findings.append(
                _finding(
                    "REFERENCE_BINDING_SCENARIO_MISSING",
                    binding.binding_id,
                    "Bind the reference evidence to an existing scenario in this revision.",
                )
            )

        matrix_asset = assets.get(binding.reference_matrix_asset_id)
        if matrix_asset is None:
            binding_valid = False
            findings.append(
                _finding(
                    "REFERENCE_BINDING_ASSET_MISSING",
                    binding.binding_id,
                    "Attach the exact reviewed reference matrix asset named by this binding.",
                )
            )
        elif matrix_asset.kind != AssetKind.REFERENCE_MATRIX:
            binding_valid = False
            findings.append(
                _finding(
                    "REFERENCE_BINDING_ASSET_KIND_INVALID",
                    binding.binding_id,
                    "Use a REFERENCE_MATRIX asset for the bound effective-resistance matrix.",
                )
            )

        if binding.reference_current_asset_id is not None:
            current_asset = assets.get(binding.reference_current_asset_id)
            expected_grid = None
            if scenario is not None:
                hypothesis = hypotheses.get(scenario.resistance_hypothesis_id)
                resistance_asset = (
                    assets.get(hypothesis.resistance_asset_id) if hypothesis is not None else None
                )
                expected_grid = resistance_asset.grid if resistance_asset is not None else None
            if current_asset is None:
                binding_valid = False
                findings.append(
                    _finding(
                        "REFERENCE_BINDING_ASSET_MISSING",
                        binding.binding_id,
                        "Attach the exact reference current raster named by this binding.",
                    )
                )
            elif current_asset.kind != AssetKind.REFERENCE_CURRENT:
                binding_valid = False
                findings.append(
                    _finding(
                        "REFERENCE_BINDING_ASSET_KIND_INVALID",
                        binding.binding_id,
                        "Use a REFERENCE_CURRENT asset for the optional current-raster attachment.",
                    )
                )
            elif scenario is not None and (
                current_asset.grid is None
                or current_asset.grid.value_kind != RasterValueKind.CONTINUOUS
                or expected_grid is None
                or grid_identity(current_asset.grid) != grid_identity(expected_grid)
            ):
                binding_valid = False
                findings.append(
                    _finding(
                        "REFERENCE_CURRENT_GRID_INVALID",
                        binding.binding_id,
                        (
                            "Use a continuous reference current raster with the bound scenario's "
                            "exact CRS, dimensions, affine cell alignment, and cell area. Preview "
                            "and approve any change through a GridAlignmentPlan."
                        ),
                    )
                )
            else:
                findings.append(
                    _finding(
                        "REFERENCE_CURRENT_DIAGNOSTIC_ONLY",
                        binding.reference_current_asset_id,
                        (
                            "Retain this current raster only for diagnostic inspection. "
                            "EcoCorridor v0.1 does not numerically compare current rasters or "
                            "use them to determine reference-validation status."
                        ),
                        severity="INFORMATION",
                    )
                )

        if not binding_valid:
            continue
        valid_bound_scenario_ids.add(binding.scenario_id)
        if conflicting_reference_results:
            findings.append(
                _finding(
                    "REFERENCE_COMPARISON_CONFLICT",
                    binding.binding_id,
                    (
                        "Scientifically different exact reference results exist for this "
                        "revision, model, engine, and binding set. Investigate the retained "
                        "matrices and comparisons and create a reviewed semantic revision; do "
                        "not select a favorable repeat."
                    ),
                )
            )
        elif passed_reference_result is not None:
            findings.append(
                _finding(
                    "REFERENCE_COMPARISON_PASSED",
                    binding.binding_id,
                    (
                        "The exact repository-backed baseline matrix comparison passed for "
                        "this binding, revision, model, engine, and complete binding set. This "
                        "does not attest Model Fitness or validate ecological assumptions."
                    ),
                    severity="INFORMATION",
                )
            )
        elif failed_reference_result is not None:
            findings.append(
                _finding(
                    "REFERENCE_COMPARISON_FAILED",
                    binding.binding_id,
                    (
                        "The exact repository-backed baseline matrix comparison failed its "
                        "reviewed tolerance. Review the direct run and reference evidence; do "
                        "not override or reinterpret the failed result."
                    ),
                )
            )
        else:
            findings.append(
                _finding(
                    "REFERENCE_COMPARISON_REQUIRED",
                    binding.binding_id,
                    (
                        "Run and retain the repository-backed baseline matrix comparison for "
                        "this exact analysis, semantic revision, model, engine, and complete "
                        "binding set. A missing, stale, partial, or unverified result cannot "
                        "satisfy this gate."
                    ),
                )
            )

    if valid_bound_scenario_ids and valid_bound_scenario_ids != set(scenarios):
        missing_reference_scenarios = sorted(set(scenarios) - valid_bound_scenario_ids)
        findings.append(
            _finding(
                "REFERENCE_BINDING_COVERAGE_INCOMPLETE",
                "reference_bindings",
                (
                    "Review whether reference coverage is intentionally partial; no approved "
                    "reference matrix is bound for: "
                    f"{', '.join(missing_reference_scenarios)}."
                ),
                severity="REVIEW_REQUIRED",
            )
        )

    if not bundle.scenarios:
        findings.append(
            _finding(
                "SCENARIO_ENSEMBLE_EMPTY",
                "scenarios",
                "Author every required target and safeguard ecological scenario.",
            )
        )

    for hypothesis in bundle.resistance_hypotheses:
        asset = assets.get(hypothesis.resistance_asset_id)
        if asset is None:
            continue  # immediate contract validation normally catches this
        if asset.kind != AssetKind.RESISTANCE_RASTER or asset.grid is None:
            findings.append(
                _finding(
                    "RESISTANCE_ASSET_SEMANTICS_INVALID",
                    hypothesis.hypothesis_id,
                    "Select a reviewed continuous resistance raster with an explicit grid.",
                )
            )
        elif asset.grid.value_kind != RasterValueKind.CONTINUOUS:
            findings.append(
                _finding(
                    "RESISTANCE_ASSET_VALUE_KIND_INVALID",
                    hypothesis.hypothesis_id,
                    "Resistance hypotheses require a continuous positive-value raster.",
                )
            )
        _require_nonblank(
            findings,
            hypothesis.derivation,
            "RESISTANCE_DERIVATION_MISSING",
            hypothesis.hypothesis_id,
            "resistance derivation",
        )

    required_pairs: set[str] = set()
    for focal_set in bundle.focal_area_sets:
        asset = assets.get(focal_set.asset_id)
        required_pairs.update(pair.pair_id for pair in focal_set.pairs if pair.required)
        if asset is None:
            continue
        if asset.kind != AssetKind.FOCAL_RASTER or asset.grid is None:
            findings.append(
                _finding(
                    "FOCAL_ASSET_SEMANTICS_INVALID",
                    focal_set.focal_set_id,
                    "Select a reviewed focal-identifier raster with an explicit grid.",
                )
            )
        elif asset.grid.value_kind != RasterValueKind.IDENTIFIER:
            findings.append(
                _finding(
                    "FOCAL_ASSET_VALUE_KIND_INVALID",
                    focal_set.focal_set_id,
                    "Focal areas require an integer identifier raster.",
                )
            )

    for scenario in bundle.scenarios:
        hypothesis = hypotheses.get(scenario.resistance_hypothesis_id)
        focal_set = focal_sets.get(scenario.focal_set_id)
        if hypothesis is None or focal_set is None:
            continue
        resistance_asset = assets.get(hypothesis.resistance_asset_id)
        focal_asset = assets.get(focal_set.asset_id)
        if (
            resistance_asset is None
            or resistance_asset.grid is None
            or focal_asset is None
            or focal_asset.grid is None
            or scenario.grid_id != hypothesis.grid_id
            or scenario.grid_id != focal_set.grid_id
            or resistance_asset.grid.grid_id != scenario.grid_id
            or focal_asset.grid.grid_id != scenario.grid_id
            or grid_identity(resistance_asset.grid) != grid_identity(focal_asset.grid)
        ):
            findings.append(
                _finding(
                    "SCENARIO_GRID_MISMATCH",
                    scenario.scenario_id,
                    "Preview and approve an explicit alignment; retain both source and derived assets.",
                )
            )
        # A reviewed resistance hypothesis may deliberately be shared across
        # guilds or life stages.  The focal set, however, is guild-specific,
        # while future condition and maturity horizon are intrinsic to the
        # hypothesis.  Do not silently reinterpret any of those bindings.
        if (
            scenario.guild_id != focal_set.guild_id
            or scenario.future_condition != hypothesis.future_condition
            or scenario.maturity_horizon != hypothesis.maturity_horizon
        ):
            findings.append(
                _finding(
                    "SCENARIO_ECOLOGICAL_BINDING_MISMATCH",
                    scenario.scenario_id,
                    "Make guild, life stage, future condition, and maturity horizon consistent with the reviewed hypothesis and focal set.",
                )
            )
        if scenario.guild_id != hypothesis.guild_id or scenario.life_stage != hypothesis.life_stage:
            findings.append(
                _finding(
                    "SHARED_RESISTANCE_HYPOTHESIS",
                    scenario.scenario_id,
                    "Confirm that this reviewed resistance hypothesis is intentionally shared "
                    "across the scenario's guild or life stage.",
                    severity="REVIEW_REQUIRED",
                )
            )
        pair_ids = {pair.pair_id for pair in focal_set.pairs}
        unknown_pairs = sorted(set(scenario.included_pair_ids) - pair_ids)
        if unknown_pairs:
            findings.append(
                _finding(
                    "SCENARIO_PAIR_REFERENCE_INVALID",
                    scenario.scenario_id,
                    f"Remove unknown focal pairs: {', '.join(unknown_pairs)}.",
                )
            )
        missing_required_pairs = sorted(
            pair.pair_id
            for pair in focal_set.pairs
            if pair.required and pair.pair_id not in scenario.included_pair_ids
        )
        if missing_required_pairs:
            findings.append(
                _finding(
                    "SCENARIO_REQUIRED_PAIR_COVERAGE_INCOMPLETE",
                    scenario.scenario_id,
                    f"Include required focal pairs: {', '.join(missing_required_pairs)}.",
                )
            )

    target_scenarios = {item.scenario_id for item in bundle.scenarios if not item.safeguard}
    safeguard_scenarios = {item.scenario_id for item in bundle.scenarios if item.safeguard}
    if not target_scenarios:
        findings.append(
            _finding(
                "TARGET_SCENARIO_MISSING",
                "scenarios",
                "Add at least one reviewed target-guild scenario.",
            )
        )
    if bundle.decision_brief.safeguard_guilds and not safeguard_scenarios:
        findings.append(
            _finding(
                "SAFEGUARD_SCENARIO_MISSING",
                "scenarios",
                "Add directly evaluated scenarios for every declared safeguard guild.",
            )
        )
    target_guilds = {item.guild_id for item in bundle.scenarios if not item.safeguard}
    missing_target_guilds = sorted(set(bundle.decision_brief.target_guilds) - target_guilds)
    if missing_target_guilds:
        findings.append(
            _finding(
                "TARGET_GUILD_COVERAGE_INCOMPLETE",
                bundle.decision_brief.brief_id,
                f"Add target scenarios for: {', '.join(missing_target_guilds)}.",
            )
        )
    observed_safeguard_guilds = {item.guild_id for item in bundle.scenarios if item.safeguard}
    missing_safeguard_guilds = sorted(
        set(bundle.decision_brief.safeguard_guilds) - observed_safeguard_guilds
    )
    if missing_safeguard_guilds:
        findings.append(
            _finding(
                "SAFEGUARD_GUILD_COVERAGE_INCOMPLETE",
                bundle.decision_brief.brief_id,
                f"Add safeguard scenarios for: {', '.join(missing_safeguard_guilds)}.",
            )
        )

    covered_safeguard_scenarios: set[str] = set()
    safeguard_owners: dict[str, str] = {}
    for safeguard in bundle.safeguards:
        declared = set(safeguard.scenario_ids)
        covered_safeguard_scenarios.update(declared)
        for scenario_id in declared:
            previous = safeguard_owners.get(scenario_id)
            if previous is not None and previous != safeguard.safeguard_id:
                findings.append(
                    _finding(
                        "SAFEGUARD_SCENARIO_DUPLICATE",
                        safeguard.safeguard_id,
                        f"Assign {scenario_id} to one reviewed safeguard record, not both {previous} and {safeguard.safeguard_id}.",
                    )
                )
            safeguard_owners[scenario_id] = safeguard.safeguard_id
        invalid = sorted(
            scenario_id
            for scenario_id in declared
            if scenario_id not in safeguard_scenarios
            or scenarios[scenario_id].guild_id != safeguard.guild_id
        )
        if invalid:
            findings.append(
                _finding(
                    "SAFEGUARD_REFERENCE_INVALID",
                    safeguard.safeguard_id,
                    f"Use safeguard scenarios for the declared guild: {', '.join(invalid)}.",
                )
            )
    uncovered = sorted(safeguard_scenarios - covered_safeguard_scenarios)
    if uncovered:
        findings.append(
            _finding(
                "SAFEGUARD_COVERAGE_INCOMPLETE",
                "safeguards",
                f"Add reviewed safeguard records for: {', '.join(uncovered)}.",
            )
        )

    # Options within one site are mutually exclusive and may share a footprint.
    # Options from different sites must never create ambiguous replacement order.
    occupied: dict[tuple[str, int, int], tuple[str, str]] = {}
    referenced_option_ids: set[str] = set()
    for site in bundle.intervention_sites:
        footprint = assets.get(site.footprint_asset_id)
        if footprint is None or footprint.kind not in {
            AssetKind.ACTION_EFFECT,
            AssetKind.VECTOR_CONTEXT,
        }:
            findings.append(
                _finding(
                    "INTERVENTION_FOOTPRINT_INVALID",
                    site.site_id,
                    "Attach the complete reviewed intervention footprint asset.",
                )
            )
        elif footprint.kind == AssetKind.ACTION_EFFECT and (
            footprint.grid is None or footprint.grid.value_kind != RasterValueKind.MASK
        ):
            findings.append(
                _finding(
                    "INTERVENTION_FOOTPRINT_MASK_SEMANTICS_MISSING",
                    site.site_id,
                    "Use a reviewed grid-aligned binary mask for this intervention footprint.",
                )
            )
        elif footprint.kind == AssetKind.VECTOR_CONTEXT and (
            not footprint.relative_path.casefold().endswith(".gpkg")
            or footprint.media_type.casefold()
            not in {"application/geopackage+sqlite3", "application/vnd.sqlite3"}
        ):
            findings.append(
                _finding(
                    "INTERVENTION_FOOTPRINT_VECTOR_NOT_NORMALIZED",
                    site.site_id,
                    "Normalize the reviewed polygon footprint to one GeoPackage layer.",
                )
            )
        site_options = [options[item] for item in site.option_ids if item in options]
        referenced_option_ids.update(item.option_id for item in site_options)
        no_action_count = sum(item.kind == InterventionKind.NO_ACTION for item in site_options)
        if no_action_count != 1:
            findings.append(
                _finding(
                    "INTERVENTION_NO_ACTION_INVALID",
                    site.site_id,
                    "Provide exactly one stable no-action option at this site.",
                )
            )
        if any(item.unresolved_concerns for item in site_options) and not any(
            item.kind == InterventionKind.SURVEY_FIRST for item in site_options
        ):
            findings.append(
                _finding(
                    "SURVEY_FIRST_OPTION_REQUIRED",
                    site.site_id,
                    "Add a Survey First option while field or feasibility concerns remain unresolved.",
                )
            )
        for option in site_options:
            if option.kind not in {InterventionKind.NO_ACTION, InterventionKind.SURVEY_FIRST}:
                covered = {effect.scenario_id for effect in option.effects}
                missing = sorted(set(scenarios) - covered)
                if missing:
                    findings.append(
                        _finding(
                            "ACTION_EFFECT_SCENARIOS_INCOMPLETE",
                            option.option_id,
                            f"Add a reviewer-approved absolute replacement effect for: {', '.join(missing)}.",
                        )
                    )
            for effect in option.effects:
                scenario = scenarios.get(effect.scenario_id)
                if scenario is None:
                    continue
                if effect.grid_id != scenario.grid_id:
                    findings.append(
                        _finding(
                            "ACTION_EFFECT_GRID_MISMATCH",
                            effect.effect_id,
                            "Re-author the effect on the exact approved scenario grid.",
                        )
                    )
                if effect.footprint_asset_id != site.footprint_asset_id:
                    findings.append(
                        _finding(
                            "ACTION_EFFECT_FOOTPRINT_MISMATCH",
                            effect.effect_id,
                            "Use the complete reviewed footprint assigned to this intervention site.",
                        )
                    )
                if action_effect_sha256(effect) != effect.sha256:
                    findings.append(
                        _finding(
                            "ACTION_EFFECT_HASH_MISMATCH",
                            effect.effect_id,
                            "Regenerate the immutable effect checksum from the reviewed replacements.",
                        )
                    )
                hypothesis = hypotheses.get(scenario.resistance_hypothesis_id)
                resistance = assets.get(hypothesis.resistance_asset_id) if hypothesis else None
                if resistance is not None and resistance.grid is not None:
                    out_of_bounds = any(
                        cell.row >= resistance.grid.height or cell.column >= resistance.grid.width
                        for cell in effect.cells
                    )
                    if out_of_bounds:
                        findings.append(
                            _finding(
                                "ACTION_EFFECT_CELL_OUT_OF_BOUNDS",
                                effect.effect_id,
                                "Keep every replacement cell inside the approved scenario grid.",
                            )
                        )
                for cell in effect.cells:
                    key = (effect.scenario_id, cell.row, cell.column)
                    previous = occupied.get(key)
                    if previous is not None and previous[0] != site.site_id:
                        findings.append(
                            _finding(
                                "SEPARATE_INTERVENTION_EFFECTS_OVERLAP",
                                effect.effect_id,
                                f"Author one reviewed composite option instead of overlapping {previous[1]} and {option.option_id}.",
                            )
                        )
                    else:
                        occupied[key] = (site.site_id, option.option_id)

    orphan_options = sorted(set(options) - referenced_option_ids)
    if orphan_options:
        findings.append(
            _finding(
                "ORPHAN_INTERVENTION_OPTIONS",
                "intervention_options",
                f"Attach every option to exactly one reviewed site: {', '.join(orphan_options)}.",
            )
        )
    if not bundle.intervention_sites:
        findings.append(
            _finding(
                "INTERVENTION_CATALOG_EMPTY",
                "intervention_sites",
                "Add at least one reviewed intervention site with No Action and bounded options.",
            )
        )

    for mask in bundle.review_masks:
        asset = assets.get(mask.asset_id)
        valid_mask_asset = not (
            asset is None
            or asset.kind != AssetKind.REVIEW_MASK
            or asset.grid is None
            or asset.grid.value_kind != RasterValueKind.MASK
            or asset.grid.grid_id != mask.grid_id
        )
        if not valid_mask_asset:
            findings.append(
                _finding(
                    "REVIEW_MASK_INVALID",
                    mask.mask_id,
                    "Use a reviewed binary mask on the exact scenario grid.",
                )
            )
            continue
        assert asset is not None and asset.grid is not None

        applicable_scenarios = [
            scenario for scenario in bundle.scenarios if scenario.grid_id == mask.grid_id
        ]
        if not applicable_scenarios:
            findings.append(
                _finding(
                    "REVIEW_MASK_SCENARIO_GRID_UNUSED",
                    mask.mask_id,
                    "Align the review mask to at least one directly evaluated scenario grid.",
                )
            )
            continue

        mismatched_scenarios: list[str] = []
        for scenario in applicable_scenarios:
            hypothesis = hypotheses.get(scenario.resistance_hypothesis_id)
            resistance_asset = (
                assets.get(hypothesis.resistance_asset_id) if hypothesis is not None else None
            )
            if (
                resistance_asset is not None
                and resistance_asset.grid is not None
                and grid_identity(asset.grid) != grid_identity(resistance_asset.grid)
            ):
                mismatched_scenarios.append(scenario.scenario_id)
        if mismatched_scenarios:
            findings.append(
                _finding(
                    "REVIEW_MASK_GRID_MISMATCH",
                    mask.mask_id,
                    (
                        "Use a review-mask asset with the exact CRS, extent, dimensions, affine "
                        "transform, and cell area of every applicable scenario resistance grid. "
                        "Assign changed geometry a new grid ID, then preview and approve the "
                        "explicit alignment for: "
                        f"{', '.join(sorted(mismatched_scenarios))}."
                    ),
                )
            )

    all_pair_ids = {
        pair.pair_id for focal_set in bundle.focal_area_sets for pair in focal_set.pairs
    }
    required_rules = [item for item in bundle.decision_rules if item.required]
    if not required_rules:
        findings.append(
            _finding(
                "REQUIRED_DECISION_RULE_MISSING",
                "decision_rules",
                "Author at least one required, evidence-resolvable decision rule.",
            )
        )
    required_safeguard_rules = [
        item for item in required_rules if item.metric == RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION
    ]
    if safeguard_scenarios and not required_safeguard_rules:
        findings.append(
            _finding(
                "SAFEGUARD_NONREGRESSION_RULE_MISSING",
                "decision_rules",
                (
                    "Author a required safeguard non-regression rule that covers every "
                    "safeguard scenario and every required included focal pair."
                ),
            )
        )
    else:
        for scenario_id in sorted(safeguard_scenarios):
            scenario = scenarios[scenario_id]
            applicable_rules = [
                rule
                for rule in required_safeguard_rules
                if not rule.scenario_ids or scenario_id in rule.scenario_ids
            ]
            if not applicable_rules:
                findings.append(
                    _finding(
                        "SAFEGUARD_RULE_SCENARIO_COVERAGE_INCOMPLETE",
                        scenario_id,
                        (
                            "Add this safeguard scenario to at least one required safeguard "
                            "non-regression rule."
                        ),
                    )
                )
                continue
            focal_set = focal_sets.get(scenario.focal_set_id)
            if focal_set is None:
                continue
            required_included_pairs = {
                pair.pair_id
                for pair in focal_set.pairs
                if pair.required and pair.pair_id in scenario.included_pair_ids
            }
            covered_pairs: set[str] = set()
            for rule in applicable_rules:
                covered_pairs.update(rule.pair_ids or scenario.included_pair_ids)
            uncovered_pairs = sorted(required_included_pairs - covered_pairs)
            if uncovered_pairs:
                findings.append(
                    _finding(
                        "SAFEGUARD_RULE_PAIR_COVERAGE_INCOMPLETE",
                        scenario_id,
                        (
                            "Add required safeguard non-regression rule coverage for focal "
                            f"pairs: {', '.join(uncovered_pairs)}."
                        ),
                    )
                )
    for rule in bundle.decision_rules:
        unknown_scenarios = sorted(set(rule.scenario_ids) - set(scenarios))
        unknown_pair_ids = sorted(set(rule.pair_ids) - all_pair_ids)
        if unknown_scenarios or unknown_pair_ids:
            findings.append(
                _finding(
                    "DECISION_RULE_REFERENCE_INVALID",
                    rule.rule_id,
                    "Limit the rule to existing scenarios and focal pairs.",
                )
            )
        if rule.pair_ids:
            scenario_scope = set(rule.scenario_ids) if rule.scenario_ids else set(scenarios)
            for scenario_id in sorted(scenario_scope & set(scenarios)):
                unavailable = sorted(
                    set(rule.pair_ids) - set(scenarios[scenario_id].included_pair_ids)
                )
                if unavailable:
                    findings.append(
                        _finding(
                            "DECISION_RULE_PAIR_SCOPE_INCOMPATIBLE",
                            f"{rule.rule_id}:{scenario_id}",
                            (
                                "Use only focal pairs included in this scoped scenario: "
                                f"{', '.join(unavailable)} are unavailable."
                            ),
                        )
                    )
        expected_operator = {
            RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT: RuleOperator.AT_LEAST,
            RuleMetric.MAX_RESISTANCE_INCREASE_PERCENT: RuleOperator.AT_MOST,
            RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT: RuleOperator.AT_MOST,
            RuleMetric.MAX_COST: RuleOperator.AT_MOST,
            RuleMetric.REQUIRE_ACCEPTED_QA: RuleOperator.EQUALS,
            RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION: RuleOperator.EQUALS,
        }[rule.metric]
        if rule.operator != expected_operator:
            findings.append(
                _finding(
                    "DECISION_RULE_OPERATOR_INVALID",
                    rule.rule_id,
                    f"Use {expected_operator.value} for {rule.metric.value}.",
                )
            )
        boolean_metric = rule.metric in {
            RuleMetric.REQUIRE_ACCEPTED_QA,
            RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION,
        }
        if boolean_metric != isinstance(rule.threshold, bool):
            findings.append(
                _finding(
                    "DECISION_RULE_THRESHOLD_TYPE_INVALID",
                    rule.rule_id,
                    "Use a Boolean threshold for requirement rules and a numeric threshold otherwise.",
                )
            )
        if (
            rule.metric == RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT
            and not bundle.review_masks
        ):
            findings.append(
                _finding(
                    "CURRENT_REDISTRIBUTION_RULE_MASK_MISSING",
                    rule.rule_id,
                    "Add a reviewed diagnostic mask before using a current-redistribution rule.",
                )
            )
        if rule.metric == RuleMetric.MAX_COST and bundle.decision_brief.budget_amount is None:
            findings.append(
                _finding(
                    "COST_RULE_BASIS_MISSING",
                    rule.rule_id,
                    "Add the screening budget, currency, and vintage to the Decision Brief.",
                )
            )

    currencies = {item.currency for item in bundle.intervention_options}
    cost_vintages = {item.cost_vintage for item in bundle.intervention_options}
    if len(currencies) > 1 or len(cost_vintages) > 1:
        findings.append(
            _finding(
                "INTERVENTION_COST_BASIS_INCOMPATIBLE",
                "intervention_options",
                "Normalize reviewed option costs to one currency and vintage outside the connectivity cache.",
            )
        )
    if (
        bundle.decision_brief.currency
        and currencies
        and currencies != {bundle.decision_brief.currency}
    ):
        findings.append(
            _finding(
                "INTERVENTION_CURRENCY_MISMATCH",
                bundle.decision_brief.brief_id,
                "Use the Decision Brief currency for every intervention option.",
            )
        )
    if (
        bundle.decision_brief.cost_vintage
        and cost_vintages
        and cost_vintages != {bundle.decision_brief.cost_vintage}
    ):
        findings.append(
            _finding(
                "INTERVENTION_COST_VINTAGE_MISMATCH",
                bundle.decision_brief.brief_id,
                "Use the Decision Brief cost vintage for every intervention option.",
            )
        )

    # Surface assumed-but-reviewable issues separately from blockers.
    if bundle.fitness.unresolved_assumptions:
        findings.append(
            _finding(
                "UNRESOLVED_MODEL_ASSUMPTIONS",
                bundle.fitness.passport_id,
                "Resolve or explicitly carry each assumption into the feasibility package.",
                severity="REVIEW_REQUIRED",
            )
        )
    if required_pairs and not any(rule.pair_ids for rule in required_rules):
        findings.append(
            _finding(
                "REQUIRED_PAIR_RULE_SCOPE_UNDISCLOSED",
                "decision_rules",
                "Confirm whether required focal pairs are evaluated individually or by an explicit reducer.",
                severity="REVIEW_REQUIRED",
            )
        )

    return sorted(
        findings,
        key=lambda item: (
            {"BLOCKING": 0, "REVIEW_REQUIRED": 1, "INFORMATION": 2}[item.severity],
            item.code,
            item.object_reference,
        ),
    )


def validate_run_plan(
    bundle: ConnectivityProjectBundle, plan: ConnectivityRunPlan
) -> list[ModelFinding]:
    """Validate a run plan against the exact reviewed bundle it claims to use."""

    findings: list[ModelFinding] = []
    sites = {item.site_id: item for item in bundle.intervention_sites}
    options = {item.option_id: item for item in bundle.intervention_options}
    scenario_ids = {item.scenario_id for item in bundle.scenarios}
    portfolio_ids = [item.portfolio_id for item in plan.portfolios]
    if len(portfolio_ids) != len(set(portfolio_ids)):
        findings.append(
            _finding(
                "PLAN_PORTFOLIO_ID_DUPLICATE",
                plan.analysis_id,
                "Give each canonical portfolio one unique evidence identity.",
            )
        )
    if set(plan.scenario_ids) != scenario_ids:
        findings.append(
            _finding(
                "ANALYSIS_SCENARIO_ENSEMBLE_INCOMPLETE",
                plan.analysis_id,
                "Include every reviewed target and safeguard scenario exactly once.",
            )
        )
    if len(plan.scenario_ids) != len(set(plan.scenario_ids)):
        findings.append(
            _finding(
                "ANALYSIS_SCENARIO_DUPLICATE",
                plan.analysis_id,
                "Remove duplicate scenarios from the direct-run plan.",
            )
        )
    if not set(plan.current_maps_for_portfolio_ids).issubset(portfolio_ids):
        findings.append(
            _finding(
                "CURRENT_MAP_PORTFOLIO_REFERENCE_INVALID",
                plan.analysis_id,
                "Request current rasters only for portfolios in this immutable run plan.",
            )
        )
    if len(plan.current_maps_for_portfolio_ids) != len(set(plan.current_maps_for_portfolio_ids)):
        findings.append(
            _finding(
                "CURRENT_MAP_PORTFOLIO_DUPLICATE",
                plan.analysis_id,
                "Request at most one current-map ensemble per portfolio.",
            )
        )

    for portfolio in plan.portfolios:
        findings.extend(_validate_portfolio(portfolio, sites=sites, options=options))

    baseline = next(
        (item for item in plan.portfolios if item.portfolio_id == plan.baseline_portfolio_id),
        None,
    )
    if baseline is not None:
        nonbaseline = [
            option_id
            for option_id in baseline.canonical_option_ids
            if options.get(option_id) is None
            or options[option_id].kind != InterventionKind.NO_ACTION
        ]
        if nonbaseline:
            findings.append(
                _finding(
                    "BASELINE_PORTFOLIO_NOT_NO_ACTION",
                    baseline.portfolio_id,
                    "The comparison baseline must select the reviewed No Action option at every site.",
                )
            )
    if plan.direct_job_count > bundle.resource_policy.max_direct_jobs:
        findings.append(
            _finding(
                "ANALYSIS_RESOURCE_POLICY_BLOCKED",
                plan.analysis_id,
                "Reduce the portfolio ensemble or explicitly revise the reviewed resource policy.",
            )
        )
    return sorted(
        findings,
        key=lambda item: (
            {"BLOCKING": 0, "REVIEW_REQUIRED": 1, "INFORMATION": 2}[item.severity],
            item.code,
            item.object_reference,
        ),
    )


def _validate_portfolio(
    portfolio: PortfolioSelection,
    *,
    sites: dict[str, InterventionSite],
    options: dict[str, InterventionOption],
) -> list[ModelFinding]:
    findings: list[ModelFinding] = []
    if set(portfolio.selections) != set(sites):
        findings.append(
            _finding(
                "PORTFOLIO_SITE_COVERAGE_INCOMPLETE",
                portfolio.portfolio_id,
                "Select exactly one reviewed option for every intervention site.",
            )
        )
        return findings
    canonical: list[str] = []
    contains_survey_first = False
    for site_id in sorted(sites):
        site = sites[site_id]
        option_id = portfolio.selections[site_id]
        option = options.get(option_id)
        if option is None or option_id not in site.option_ids or option.site_id != site_id:
            findings.append(
                _finding(
                    "PORTFOLIO_OPTION_REFERENCE_INVALID",
                    portfolio.portfolio_id,
                    f"Select a reviewed option belonging to site {site_id}.",
                )
            )
            continue
        canonical.append(option_id)
        contains_survey_first |= option.kind == InterventionKind.SURVEY_FIRST
    if tuple(canonical) != portfolio.canonical_option_ids:
        findings.append(
            _finding(
                "PORTFOLIO_CANONICAL_IDENTITY_MISMATCH",
                portfolio.portfolio_id,
                "Regenerate the portfolio identity from site-sorted stable option IDs.",
            )
        )
    if portfolio.contains_survey_first != contains_survey_first:
        findings.append(
            _finding(
                "PORTFOLIO_SURVEY_FIRST_MISMATCH",
                portfolio.portfolio_id,
                "Regenerate the feasibility disposition from the selected option catalog.",
            )
        )
    return findings


def validation_projection(
    bundle: ConnectivityProjectBundle,
    *,
    authoritative_reference_results: Sequence[ReferenceComparisonResult] = (),
    expected_engine_manifest_sha256: str | None = None,
) -> dict[str, object]:
    findings = validate_professional_bundle(
        bundle,
        authoritative_reference_results=authoritative_reference_results,
        expected_engine_manifest_sha256=expected_engine_manifest_sha256,
    )
    blockers = [item for item in findings if item.severity == "BLOCKING"]
    return {
        "project_id": bundle.project_id,
        "revision_id": bundle.revision.revision_id,
        "status": "BLOCKED" if blockers else "READY",
        "findings": [item.model_dump() for item in findings],
        "scenario_count": len(bundle.scenarios),
        "target_scenario_count": sum(not item.safeguard for item in bundle.scenarios),
        "safeguard_scenario_count": sum(item.safeguard for item in bundle.scenarios),
        "asset_count": len(bundle.assets),
        "intervention_site_count": len(bundle.intervention_sites),
    }
