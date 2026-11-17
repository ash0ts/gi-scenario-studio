"""Authoritative professional reference-comparison workflow helpers.

The public workflow deliberately has two barriers.  A baseline-only plan is
persisted before direct work starts, then a result is assembled only after the
durable scheduler has retained every exact matrix run.  No reference asset,
binding, or caller-supplied number is treated as a passed comparison by itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from scenario_platform.storage import contained_path

from .contracts import (
    ConnectivityEvidenceClaim,
    ConnectivityProjectBundle,
    ConnectivityRunPlan,
    DirectConnectivityRun,
    EvidenceEdge,
    EvidenceNode,
    InterventionKind,
    PortfolioSelection,
    ReferenceComparisonPlan,
    ReferenceComparisonResult,
    ReferenceEvidenceClaimLink,
    ReferenceScenarioComparison,
    canonical_sha256,
)
from .engine import PreparedPair
from .interventions import canonicalize_portfolio
from .metrics import CONNECTIVITY_LIMITATION
from .reference_comparison import (
    ReferenceComparisonError,
    assemble_reference_comparison_result,
    compare_reference_matrix,
)
from .reference_matrix_parser import REFERENCE_MATRIX_PARSER_SHA256


class ReferenceWorkflowError(ValueError):
    """A professional reference workflow cannot be proven from local evidence."""


def build_reference_comparison_plan(
    *,
    bundle: ConnectivityProjectBundle,
    analysis_id: str,
    engine_manifest_sha256: str,
    created_at: datetime,
) -> ReferenceComparisonPlan:
    """Build one immutable No Action ensemble for every reviewed scenario.

    Only scenarios with reviewed reference bindings enter this evidence run.
    It is not an alternatives analysis and therefore must not spend solver work
    on unbound scenarios or produce current-map outputs.
    """

    if not bundle.reference_bindings:
        raise ReferenceWorkflowError(
            "a reference comparison requires at least one reviewed scenario binding"
        )
    options_by_id = {item.option_id: item for item in bundle.intervention_options}
    selections: dict[str, str] = {}
    for site in sorted(bundle.intervention_sites, key=lambda item: item.site_id):
        no_action_ids = [
            option_id
            for option_id in site.option_ids
            if option_id in options_by_id
            and options_by_id[option_id].kind == InterventionKind.NO_ACTION
        ]
        if len(no_action_ids) != 1:
            raise ReferenceWorkflowError(
                f"site {site.site_id} requires exactly one reviewed No Action option"
            )
        selections[site.site_id] = no_action_ids[0]
    baseline = canonicalize_portfolio(
        bundle.intervention_sites,
        bundle.intervention_options,
        selections,
    )
    ordered_bindings = sorted(
        bundle.reference_bindings,
        key=lambda item: (item.scenario_id, item.binding_id),
    )
    scenario_ids = [item.scenario_id for item in ordered_bindings]
    if not scenario_ids:
        raise ReferenceWorkflowError("a reference comparison requires reviewed scenarios")
    if len(scenario_ids) > bundle.resource_policy.max_direct_jobs:
        raise ReferenceWorkflowError(
            "the baseline reference ensemble exceeds the reviewed direct-job resource policy"
        )
    run_plan = ConnectivityRunPlan(
        analysis_id=analysis_id,
        revision_id=bundle.revision.revision_id,
        scenario_ids=scenario_ids,
        portfolios=[baseline],
        baseline_portfolio_id=baseline.portfolio_id,
        direct_job_count=len(scenario_ids),
        solver_job_cap=len(scenario_ids),
        search_method="ENUMERATION",
        seed=0,
        current_maps_for_portfolio_ids=[],
        engine_manifest_sha256=engine_manifest_sha256,
    )
    plan_id = (
        "reference_plan_"
        + canonical_sha256(
            {
                "analysis_id": analysis_id,
                "revision_id": bundle.revision.revision_id,
                "model_sha256": bundle.revision.model_sha256,
                "engine_manifest_sha256": engine_manifest_sha256,
                "binding_ids": [item.binding_id for item in ordered_bindings],
                "bound_scenario_ids": [item.scenario_id for item in ordered_bindings],
                "run_plan": run_plan.model_dump(mode="json"),
            }
        )[:24]
    )
    return ReferenceComparisonPlan(
        plan_id=plan_id,
        analysis_id=analysis_id,
        revision_id=bundle.revision.revision_id,
        model_sha256=bundle.revision.model_sha256,
        engine_manifest_sha256=engine_manifest_sha256,
        binding_ids=[item.binding_id for item in ordered_bindings],
        bound_scenario_ids=[item.scenario_id for item in ordered_bindings],
        run_plan=run_plan,
        created_at=created_at,
    )


def assemble_and_store_reference_result(
    *,
    store: Any,
    plan: ReferenceComparisonPlan,
) -> tuple[ReferenceComparisonResult, list[ConnectivityEvidenceClaim], dict[str, Any]]:
    """Derive and atomically retain an exact result from completed direct jobs."""

    status = store.analysis(plan.analysis_id)
    if status["state"] != "COMPLETED":
        raise ReferenceWorkflowError(
            "reference comparison result requires a completed baseline direct ensemble"
        )
    bundle = store.analysis_bundle(plan.analysis_id)
    if (
        bundle.revision.revision_id != plan.revision_id
        or bundle.revision.model_sha256 != plan.model_sha256
    ):
        raise ReferenceWorkflowError(
            "reference comparison plan differs from its immutable model snapshot"
        )
    runs, _, _ = store.analysis_results(plan.analysis_id)
    run_by_scenario = _baseline_runs_by_scenario(runs, plan=plan)
    comparisons = _scenario_comparisons(
        store=store,
        bundle=bundle,
        plan=plan,
        run_by_scenario=run_by_scenario,
    )
    links = reference_evidence_claim_links(comparisons)
    completed_at = max(item.completed_at for item in comparisons)
    result_id = reference_result_id(
        plan_id=plan.plan_id,
        comparisons=comparisons,
        claim_links=links,
    )
    result = assemble_reference_comparison_result(
        result_id=result_id,
        plan=plan,
        scenario_comparisons=comparisons,
        evidence_claim_links=links,
        completed_at=completed_at,
        limitations=[
            limitation for comparison in comparisons for limitation in comparison.limitations
        ],
    )
    claims = build_reference_evidence_claims(
        bundle=bundle,
        plan=plan,
        result=result,
        runs=run_by_scenario,
    )
    receipt = store.store_reference_comparison_result(result, claims=claims)
    persisted, persisted_claims = store.reference_comparison_result(result.result_id)
    if persisted != result or persisted_claims != sorted(claims, key=lambda item: item.claim_id):
        raise ReferenceWorkflowError(
            "stored reference result did not read back as the exact canonical evidence"
        )
    return persisted, persisted_claims, receipt


def recover_pending_reference_results(
    *,
    store: Any,
    analysis_id: str | None = None,
) -> list[str]:
    """Materialize durable completion intents exactly once after crash/restart.

    Heavy matrix parsing remains outside the repository write transaction.
    Deterministic result/claim identities plus repository uniqueness make a
    replay after a crash byte-identical and idempotent.
    """

    pending = store.pending_reference_completions()
    if analysis_id is not None:
        pending = [item for item in pending if item == analysis_id]
    recovered: list[str] = []
    for pending_analysis_id in pending:
        entry = store.reference_comparison_plan_for_analysis(pending_analysis_id)
        if entry is None:
            raise ReferenceWorkflowError(
                "pending reference completion has no immutable comparison plan"
            )
        result, _, _ = assemble_and_store_reference_result(store=store, plan=entry[0])
        recovered.append(result.result_id)
    return recovered


def build_reference_evidence_claims(
    *,
    bundle: ConnectivityProjectBundle,
    plan: ReferenceComparisonPlan,
    result: ReferenceComparisonResult,
    runs: dict[str, DirectConnectivityRun],
) -> list[ConnectivityEvidenceClaim]:
    """Build the canonical per-pair claim graph accepted by persistence."""

    scenarios = {item.scenario_id: item for item in bundle.scenarios}
    assets = {item.asset_id: item for item in bundle.assets}
    comparison_by_id = {item.comparison_id: item for item in result.scenario_comparisons}
    claims: list[ConnectivityEvidenceClaim] = []
    for link in result.evidence_claim_links:
        comparison = comparison_by_id[link.comparison_id]
        pair = next(item for item in comparison.pair_comparisons if item.pair_id == link.pair_id)
        run = runs[link.scenario_id]
        scenario = scenarios[link.scenario_id]
        matrix_asset = assets[comparison.reference_matrix_asset_id]
        claims.append(
            _reference_claim(
                link=link,
                comparison=comparison,
                pair=pair,
                run=run,
                scenario=scenario,
                matrix_asset=matrix_asset,
                baseline=plan.run_plan.portfolios[0],
            )
        )
    return sorted(claims, key=lambda item: item.claim_id)


def _baseline_runs_by_scenario(
    runs: Sequence[DirectConnectivityRun],
    *,
    plan: ReferenceComparisonPlan,
) -> dict[str, DirectConnectivityRun]:
    expected_scenarios = set(plan.run_plan.scenario_ids)
    selected = [item for item in runs if item.portfolio_id == plan.run_plan.baseline_portfolio_id]
    by_scenario = {item.scenario_id: item for item in selected}
    if (
        len(by_scenario) != len(selected)
        or set(by_scenario) != expected_scenarios
        or len(runs) != len(selected)
    ):
        raise ReferenceWorkflowError(
            "reference analysis does not contain exactly one baseline run per planned scenario"
        )
    return by_scenario


def _scenario_comparisons(
    *,
    store: Any,
    bundle: ConnectivityProjectBundle,
    plan: ReferenceComparisonPlan,
    run_by_scenario: dict[str, DirectConnectivityRun],
) -> list[ReferenceScenarioComparison]:
    bindings = {item.binding_id: item for item in bundle.reference_bindings}
    scenarios = {item.scenario_id: item for item in bundle.scenarios}
    focal_sets = {item.focal_set_id: item for item in bundle.focal_area_sets}
    hypotheses = {item.hypothesis_id: item for item in bundle.resistance_hypotheses}
    assets = {item.asset_id: item for item in bundle.assets}
    comparisons: list[ReferenceScenarioComparison] = []
    for binding_id in plan.binding_ids:
        try:
            binding = bindings[binding_id]
            scenario = scenarios[binding.scenario_id]
            focal_set = focal_sets[scenario.focal_set_id]
            hypothesis = hypotheses[scenario.resistance_hypothesis_id]
            resistance_asset = assets[hypothesis.resistance_asset_id]
            matrix_asset = assets[binding.reference_matrix_asset_id]
            run = run_by_scenario[scenario.scenario_id]
        except KeyError as exc:
            raise ReferenceWorkflowError(
                "reference plan no longer resolves inside its immutable model snapshot"
            ) from exc
        if resistance_asset.grid is None:
            raise ReferenceWorkflowError("reference scenario resistance grid is unavailable")
        pair_by_id = {item.pair_id: item for item in focal_set.pairs}
        try:
            prepared_pairs = [
                PreparedPair(
                    pair_id=pair_id,
                    from_focal_id=pair_by_id[pair_id].from_focal_id,
                    to_focal_id=pair_by_id[pair_id].to_focal_id,
                )
                for pair_id in scenario.included_pair_ids
            ]
        except KeyError as exc:
            raise ReferenceWorkflowError(
                "reference scenario includes an unavailable focal pair"
            ) from exc
        matrix_records = [
            item for item in store.run_artifacts(run.direct_run_id) if item["role"] == "matrix"
        ]
        if len(matrix_records) != 1:
            raise ReferenceWorkflowError(
                "reference direct run must retain exactly one matrix artifact"
            )
        observed_path = contained_path(store.root, str(matrix_records[0]["cas_relative_path"]))
        reference_path = store.resolve_asset_path(matrix_asset.relative_path)
        current_asset = (
            assets[binding.reference_current_asset_id]
            if binding.reference_current_asset_id is not None
            else None
        )
        comparison_id = reference_scenario_comparison_id(
            plan_id=plan.plan_id,
            binding_id=binding.binding_id,
            scenario_id=scenario.scenario_id,
            direct_run_id=run.direct_run_id,
            result_sha256=run.result_sha256,
            reference_matrix_sha256=matrix_asset.sha256,
        )
        try:
            comparison = compare_reference_matrix(
                comparison_id=comparison_id,
                plan=plan,
                binding=binding,
                scenario=scenario,
                expected_scenario_grid=resistance_asset.grid,
                prepared_pairs=prepared_pairs,
                observed_run=run,
                observed_matrix_path=observed_path,
                reference_matrix_asset=matrix_asset,
                reference_matrix_path=reference_path,
                reference_current_asset=current_asset,
                completed_at=run.completed_at,
            )
        except (OSError, ReferenceComparisonError, ValueError) as exc:
            raise ReferenceWorkflowError(str(exc)) from exc
        comparisons.append(comparison)
    return sorted(comparisons, key=lambda item: (item.scenario_id, item.binding_id))


def reference_scenario_comparison_id(
    *,
    plan_id: str,
    binding_id: str,
    scenario_id: str,
    direct_run_id: str,
    result_sha256: str,
    reference_matrix_sha256: str,
) -> str:
    """Derive the immutable comparison identity from direct and reviewed evidence."""

    return (
        "reference_comparison_"
        + canonical_sha256(
            {
                "plan_id": plan_id,
                "binding_id": binding_id,
                "scenario_id": scenario_id,
                "direct_run_id": direct_run_id,
                "result_sha256": result_sha256,
                "reference_matrix_sha256": reference_matrix_sha256,
            }
        )[:24]
    )


def reference_evidence_claim_links(
    comparisons: Sequence[ReferenceScenarioComparison],
) -> list[ReferenceEvidenceClaimLink]:
    links = [
        ReferenceEvidenceClaimLink(
            claim_id="claim_reference_"
            + canonical_sha256(
                {
                    "comparison_id": comparison.comparison_id,
                    "scenario_id": comparison.scenario_id,
                    "pair_id": pair.pair_id,
                    "direct_run_id": comparison.direct_run_id,
                }
            )[:24],
            comparison_id=comparison.comparison_id,
            scenario_id=comparison.scenario_id,
            pair_id=pair.pair_id,
            direct_run_id=comparison.direct_run_id,
        )
        for comparison in comparisons
        for pair in comparison.pair_comparisons
    ]
    return sorted(
        links,
        key=lambda item: (
            item.scenario_id,
            item.pair_id,
            item.comparison_id,
            item.direct_run_id,
            item.claim_id,
        ),
    )


def reference_result_id(
    *,
    plan_id: str,
    comparisons: Sequence[ReferenceScenarioComparison],
    claim_links: Sequence[ReferenceEvidenceClaimLink],
) -> str:
    """Derive the immutable aggregate identity from canonical comparisons and links."""

    return (
        "reference_result_"
        + canonical_sha256(
            {
                "plan_id": plan_id,
                "comparison_sha256": [
                    canonical_sha256(item.model_dump(mode="json")) for item in comparisons
                ],
                "claim_links": [item.model_dump(mode="json") for item in claim_links],
            }
        )[:24]
    )


def _assumption_node_id(limitation: str) -> str:
    return f"assumption:{hashlib.sha256(limitation.encode('utf-8')).hexdigest()}"


def _reference_claim(
    *,
    link: ReferenceEvidenceClaimLink,
    comparison: ReferenceScenarioComparison,
    pair: Any,
    run: DirectConnectivityRun,
    scenario: Any,
    matrix_asset: Any,
    baseline: PortfolioSelection,
) -> ConnectivityEvidenceClaim:
    text = "Direct Circuitscape result under documented assumptions."
    limitations = list(dict.fromkeys([CONNECTIVITY_LIMITATION, *comparison.limitations]))
    comparison_node_id = f"reference-comparison:{comparison.comparison_id}"
    metric_node_id = f"metric:{comparison.comparison_id}:{link.pair_id}"
    run_node_id = f"run:{run.direct_run_id}"
    scenario_node_id = f"scenario:{link.scenario_id}"
    asset_node_id = f"asset:{matrix_asset.asset_id}"
    engine_node_id = f"engine:{run.engine_manifest_sha256}"
    qa_node_id = f"qa:{run.direct_run_id}"
    artifact_node_id = f"artifact:matrix:{run.direct_run_id}:{run.matrix_sha256}"
    authorization_node_id = f"authorization:{matrix_asset.authorization.asset_id}"
    assumption_nodes = [
        EvidenceNode(
            node_id=_assumption_node_id(limitation),
            node_type="ASSUMPTION",
            label=limitation,
        )
        for limitation in limitations
    ]
    nodes = [
        EvidenceNode(node_id=link.claim_id, node_type="CLAIM", label=text),
        EvidenceNode(
            node_id=comparison_node_id,
            node_type="REFERENCE_COMPARISON",
            label="Reviewed effective-resistance matrix comparison",
            sha256=canonical_sha256(comparison.model_dump(mode="json")),
            attributes={
                "comparison_id": comparison.comparison_id,
                "scenario_id": link.scenario_id,
                "pair_id": link.pair_id,
                "direct_run_id": link.direct_run_id,
                "binding_id": comparison.binding_id,
                "reference_matrix_asset_id": matrix_asset.asset_id,
                "status": comparison.status,
            },
        ),
        EvidenceNode(
            node_id=metric_node_id,
            node_type="METRIC",
            label="Reference tolerance disposition",
            attributes={
                "pair_id": link.pair_id,
                "reference_effective_resistance": pair.reference_effective_resistance,
                "observed_effective_resistance": pair.observed_effective_resistance,
                "absolute_difference": pair.absolute_difference,
                "relative_difference": pair.relative_difference,
                "allowed_difference": pair.allowed_difference,
                "within_tolerance": pair.within_tolerance,
                "units": "effective_resistance",
            },
        ),
        EvidenceNode(
            node_id=run_node_id,
            node_type="DIRECT_RUN",
            label=run.direct_run_id,
            sha256=run.result_sha256,
            attributes={
                "analysis_id": run.analysis_id,
                "portfolio_id": run.portfolio_id,
                "scenario_id": run.scenario_id,
            },
        ),
        EvidenceNode(
            node_id=scenario_node_id,
            node_type="SCENARIO",
            label=scenario.scenario_id,
            attributes={
                "scenario_id": scenario.scenario_id,
                "guild_id": scenario.guild_id,
                "resistance_hypothesis_id": scenario.resistance_hypothesis_id,
                "focal_set_id": scenario.focal_set_id,
                "safeguard": scenario.safeguard,
            },
        ),
        EvidenceNode(
            node_id=asset_node_id,
            node_type="ASSET",
            label=matrix_asset.title,
            sha256=matrix_asset.sha256,
            attributes={
                "asset_id": matrix_asset.asset_id,
                "kind": matrix_asset.kind.value,
                "authorization_id": matrix_asset.authorization.asset_id,
            },
        ),
        EvidenceNode(
            node_id=engine_node_id,
            node_type="ENGINE_MANIFEST",
            label="Pinned Circuitscape engine manifest",
            sha256=run.engine_manifest_sha256,
            attributes={
                "engine_name": "Circuitscape",
                "reference_matrix_parser_sha256": REFERENCE_MATRIX_PARSER_SHA256,
            },
        ),
        EvidenceNode(
            node_id=qa_node_id,
            node_type="QA",
            label=run.qa_status.value,
            attributes={
                "status": run.qa_status.value,
                "direct_run_id": run.direct_run_id,
                "findings_sha256": canonical_sha256(run.qa_findings),
            },
        ),
        EvidenceNode(
            node_id=artifact_node_id,
            node_type="ARTIFACT",
            label="Retained direct effective-resistance matrix",
            sha256=run.matrix_sha256,
            attributes={"role": "matrix", "direct_run_id": run.direct_run_id},
        ),
        EvidenceNode(
            node_id=authorization_node_id,
            node_type="AUTHORIZATION",
            label="Reviewed reference-data authorization",
            sha256=canonical_sha256(matrix_asset.authorization.model_dump(mode="json")),
            attributes={
                "asset_id": matrix_asset.authorization.asset_id,
                "cloud_disposition": matrix_asset.authorization.cloud_disposition.value,
                "export_allowed": matrix_asset.authorization.export_allowed,
                "reviewer": matrix_asset.authorization.reviewer,
            },
        ),
        *assumption_nodes,
    ]
    edges = [
        EvidenceEdge(from_node_id=link.claim_id, to_node_id=metric_node_id, relation="reports"),
        EvidenceEdge(
            from_node_id=metric_node_id,
            to_node_id=comparison_node_id,
            relation="derived_from",
        ),
        EvidenceEdge(
            from_node_id=comparison_node_id,
            to_node_id=run_node_id,
            relation="compares_direct_run",
        ),
        EvidenceEdge(
            from_node_id=comparison_node_id,
            to_node_id=asset_node_id,
            relation="compares_against",
        ),
        EvidenceEdge(from_node_id=run_node_id, to_node_id=scenario_node_id, relation="executes"),
        EvidenceEdge(from_node_id=run_node_id, to_node_id=engine_node_id, relation="executed_by"),
        EvidenceEdge(from_node_id=run_node_id, to_node_id=qa_node_id, relation="checked_by"),
        EvidenceEdge(from_node_id=run_node_id, to_node_id=artifact_node_id, relation="retained_as"),
        EvidenceEdge(
            from_node_id=asset_node_id,
            to_node_id=authorization_node_id,
            relation="authorized_by",
        ),
        *[
            EvidenceEdge(
                from_node_id=scenario_node_id,
                to_node_id=_assumption_node_id(limitation),
                relation="assumes",
            )
            for limitation in limitations
        ],
    ]
    return ConnectivityEvidenceClaim(
        claim_id=link.claim_id,
        claim_kind="DIRECT_REFERENCE_COMPARISON",
        text=text,
        value=pair.within_tolerance,
        units="within_tolerance",
        portfolio_id=baseline.portfolio_id,
        scenario_id=link.scenario_id,
        pair_id=link.pair_id,
        direct_run_ids=[link.direct_run_id],
        root_node_id=link.claim_id,
        nodes=nodes,
        edges=edges,
        limitations=limitations,
    )
