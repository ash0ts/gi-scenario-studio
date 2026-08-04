"""Within-scenario metrics, decision rules, QA, and evidence resolution."""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from .contracts import (
    CURRENT_RASTER_LIMITATION,
    FITNESS_LIMITATION,
    ConnectivityDecisionRule,
    ConnectivityEvidenceClaim,
    ConnectivityProjectBundle,
    ConnectivityScenario,
    DirectConnectivityRun,
    DirectRunState,
    EvidenceEdge,
    EvidenceNode,
    PairResistanceMetric,
    PortfolioDecisionSummary,
    PortfolioDisposition,
    PortfolioSelection,
    QAStatus,
    RuleDisposition,
    RuleMetric,
    RuleOperator,
)
from .engine import PreparedPair, parse_circuitscape_matrix
from .raster_io import compare_current_maps_in_mask, load_raster

PERMITTED_CLAIM_PREFIX = "Direct Circuitscape result under documented assumptions."
CURRENT_MAP_LIMITATION = "Current-density maps are diagnostic and do not predict animal use."
CONNECTIVITY_LIMITATION = (
    "Connectivity results are conditional on reviewed resistance, focal-area, scenario, and "
    "intervention-effect assumptions."
)

PROHIBITED_CLAIM_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        (
            r"\b(?:predict(?:s|ed|ing)?|forecast(?:s|ed|ing)?|estimate(?:s|d|ing)?|"
            r"demonstrate(?:s|d|ing)?|show(?:s|ed|ing)?|indicate(?:s|d|ing)?)\s+"
            r"(?:animal|wildlife)\s+(?:movement|use)\b"
        ),
        (
            r"\b(?:increase(?:s|d|ing)?|improve(?:s|d|ing)?|restore(?:s|d|ing)?|"
            r"enable(?:s|d|ing)?|support(?:s|ed|ing)?|facilitate(?:s|d|ing)?|"
            r"enhance(?:s|d|ing)?)\s+(?:animal|wildlife)\s+(?:movement|use)\b"
        ),
        (
            r"\b(?:predict(?:s|ed|ing)?|increase(?:s|d|ing)?|improve(?:s|d|ing)?|"
            r"restore(?:s|d|ing)?|support(?:s|ed|ing)?|facilitate(?:s|d|ing)?|"
            r"enhance(?:s|d|ing)?)\s+crossing\s+use\b"
        ),
        (
            r"\bcrossing\s+use\s+(?:will|would|can|could|is\s+expected\s+to)?\s*"
            r"(?:increase|improve|rise)\b"
        ),
        (
            r"\b(?:reduce(?:s|d|ing)?|decrease(?:s|d|ing)?|prevent(?:s|ed|ing)?|"
            r"avoid(?:s|ed|ing)?|lower(?:s|ed|ing)?)\s+"
            r"(?:wildlife[- ]vehicle\s+)?(?:collisions?|crashes?)\b"
        ),
        (
            r"\b(?:deliver(?:s|ed|ing)?|provide(?:s|d|ing)?|achieve(?:s|d|ing)?|"
            r"yield(?:s|ed|ing)?|create(?:s|d|ing)?)\s+"
            r"(?:wildlife[- ]vehicle\s+)?(?:collision|crash)(?:s)?\s+reduction\b"
        ),
        r"\b(?:collision|crash)(?:s)?\s+reduction\s+(?:is|was|will|would|can|could)\b",
        (
            r"\b(?:population|biodiversity)\s+"
            r"(?:benefit|gain|improvement|increase|viability|recovery)\b"
        ),
        (
            r"\b(?:benefit(?:s|ed|ing)?|improve(?:s|d|ing)?|increase(?:s|d|ing)?|"
            r"protect(?:s|ed|ing)?|restore(?:s|d|ing)?)\s+"
            r"(?:the\s+)?(?:population|biodiversity)\b"
        ),
        r"\bregulatory\s+(?:benefit|compliance|approval)\b",
        r"\bregulat(?:or|ory)[- ]approved\b",
        r"\b(?:best|optimal|optimized)\s+(?:portfolio|option|crossing|corridor|route|design)\b",
        (
            r"\b(?:approved|validated)\s+(?:wildlife\s+)?"
            r"(?:corridor|crossing|design|portfolio|option)\b"
        ),
        (
            r"\b(?:corridor|crossing|design|portfolio|option)\s+"
            r"(?:is|was|has\s+been)\s+(?:approved|validated)\b"
        ),
        r"\bdesign[- ]ready(?:\s+(?:crossing|fencing|design))?\b",
        r"\bguarantee(?:s|d|ing)?\b",
    )
)

_NEGATION_IMMEDIATELY_BEFORE_CLAIM = re.compile(
    r"(?:"
    r"\b(?:cannot|can't|never|without)\s+|"
    r"\b(?:doesn't|don't|didn't|isn't|aren't|wasn't|weren't|won't|wouldn't|couldn't)\s+|"
    r"\bno\s+(?:(?:an?|the)\s+)?|"
    r"\bnot\s+(?:(?:an?|the)\s+)?|"
    r"\b(?:does|do|did|is|are|was|were|will|would|can|could)\s+not\s+"
    r"(?:[A-Za-z][A-Za-z-]*\s+(?:or|nor)\s+)?"
    r")$",
    re.IGNORECASE,
)

_NEGATED_CLAIM_SCOPE = re.compile(
    r"(?:"
    r"\b(?:cannot|can't|never|without)\s+|"
    r"\b(?:doesn't|don't|didn't|isn't|aren't|wasn't|weren't|won't|wouldn't|couldn't)\s+|"
    r"\b(?:does|do|did|is|are|was|were|will|would|can|could)\s+not\s+|"
    r"\b(?:not|no)\s+(?:(?:an?|the)\s+)?"
    r")"
    r"(?:predict|forecast|estimate|demonstrate|show|indicate|observe|increase|improve|restore|"
    r"enable|support|facilitate|enhance|reduce|decrease|prevent|avoid|lower|deliver|provide|"
    r"achieve|yield|create|benefit|protect|guarantee|approve|validate|best|optimal|optimized|"
    r"design[- ]ready|population|biodiversity)\w*\b"
    r"[^.!?;]*\b(?:or|nor)\s+$",
    re.IGNORECASE,
)


def _explicitly_negated(text: str, match_start: int) -> bool:
    """Recognize a negation attached to this claim, not merely nearby text.

    Claim-bearing prose often contains a legitimate limitation followed by a
    separate positive assertion.  Treating any earlier ``not`` as negating the
    later assertion lets text such as ``not calibrated and guarantees ...``
    bypass the environmental claim gate.  The anchored expression accepts only
    a grammatical negator immediately governing the matched claim, including
    the common ``does not estimate or predict`` construction.
    """

    preceding = text[max(0, match_start - 96) : match_start]
    return bool(
        _NEGATION_IMMEDIATELY_BEFORE_CLAIM.search(preceding)
        or _NEGATED_CLAIM_SCOPE.search(preceding)
    )


def assert_safe_ecological_claim(text: str) -> None:
    for pattern in PROHIBITED_CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            if not _explicitly_negated(text, match.start()):
                raise ValueError(f"prohibited ecological claim: {pattern.pattern}")


def iter_bundle_ecological_claim_texts(
    bundle: ConnectivityProjectBundle,
) -> Iterator[tuple[str, str]]:
    """Yield authored narrative that can appear in professional UI or exports.

    Source names and citations are intentionally excluded: preserving the exact
    title of an external study is provenance, not an EcoCorridor claim. Explicit
    ``non_goals`` and action-basis ``limitations`` are also structural negation
    contexts. The fields below are project-authored interpretations,
    assumptions, decisions, or labels and therefore must obey the environmental
    claim boundary.
    """

    yield "project.title", bundle.title
    yield "revision.reason", bundle.revision.reason

    brief = bundle.decision_brief
    yield "decision_brief.title", brief.title
    yield "decision_brief.decision_question", brief.decision_question
    yield "decision_brief.intended_use", brief.intended_use
    yield "decision_brief.required_next_action", brief.required_next_action
    fitness = bundle.fitness
    yield "fitness.intended_use", fitness.intended_use
    for dimension in fitness.dimensions:
        yield f"fitness.dimension[{dimension.key}].notes", dimension.notes
    for index, value in enumerate(fitness.unresolved_assumptions):
        yield f"fitness.unresolved_assumptions[{index}]", value
    for index, value in enumerate(fitness.attestations):
        yield f"fitness.attestations[{index}]", value

    for asset in bundle.assets:
        yield f"asset[{asset.asset_id}].title", asset.title
        if asset.authorization.notes:
            yield f"asset[{asset.asset_id}].authorization.notes", asset.authorization.notes

    for hypothesis in bundle.resistance_hypotheses:
        prefix = f"resistance_hypothesis[{hypothesis.hypothesis_id}]"
        yield f"{prefix}.title", hypothesis.title
        yield f"{prefix}.rationale", hypothesis.rationale
        yield f"{prefix}.derivation", hypothesis.derivation

    for focal_set in bundle.focal_area_sets:
        prefix = f"focal_area_set[{focal_set.focal_set_id}]"
        yield f"{prefix}.title", focal_set.title
        yield f"{prefix}.rationale", focal_set.rationale

    for scenario in bundle.scenarios:
        yield f"scenario[{scenario.scenario_id}].title", scenario.title

    for binding in bundle.reference_bindings:
        yield (
            f"reference_binding[{binding.binding_id}].reference_basis",
            binding.reference_basis,
        )

    for site in bundle.intervention_sites:
        prefix = f"intervention_site[{site.site_id}]"
        yield f"{prefix}.title", site.title
        for index, value in enumerate(site.known_constraints):
            yield f"{prefix}.known_constraints[{index}]", value

    for option in bundle.intervention_options:
        prefix = f"intervention_option[{option.option_id}]"
        yield f"{prefix}.title", option.title
        yield f"{prefix}.land_access", option.land_access
        yield f"{prefix}.maintenance", option.maintenance
        for field, values in (
            ("dependencies", option.dependencies),
            ("exclusions", option.exclusions),
            ("external_feasibility_evidence", option.external_feasibility_evidence),
            ("unresolved_concerns", option.unresolved_concerns),
        ):
            for index, value in enumerate(values):
                yield f"{prefix}.{field}[{index}]", value
        for effect in option.effects:
            basis_prefix = f"{prefix}.effect[{effect.effect_id}].basis"
            yield f"{basis_prefix}.purpose", effect.basis.purpose
            for field, values in (("assumptions", effect.basis.assumptions),):
                for index, value in enumerate(values):
                    yield f"{basis_prefix}.{field}[{index}]", value

    if bundle.cost_basis is not None:
        for index, value in enumerate(bundle.cost_basis.assumptions):
            yield f"cost_basis.assumptions[{index}]", value

    for safeguard in bundle.safeguards:
        for index, value in enumerate(safeguard.concerns):
            yield f"safeguard[{safeguard.safeguard_id}].concerns[{index}]", value

    for review_mask in bundle.review_masks:
        prefix = f"review_mask[{review_mask.mask_id}]"
        yield f"{prefix}.title", review_mask.title
        yield f"{prefix}.interpretation", review_mask.interpretation

    for rule in bundle.decision_rules:
        prefix = f"decision_rule[{rule.rule_id}]"
        yield f"{prefix}.title", rule.title
        yield f"{prefix}.rationale", rule.rationale


def assert_safe_bundle_ecological_claims(bundle: ConnectivityProjectBundle) -> None:
    """Fail closed when any exported project-authored narrative is prohibited."""

    for reference, text in iter_bundle_ecological_claim_texts(bundle):
        try:
            assert_safe_ecological_claim(text)
        except ValueError as exc:
            raise ValueError(f"{reference}: {exc}") from exc


def relative_resistance_reduction_percent(baseline: float, portfolio: float) -> float:
    if baseline <= 0 or portfolio <= 0:
        raise ValueError("effective resistance values must be strictly positive")
    return (baseline - portfolio) / baseline * 100.0


def attach_baseline_metrics(
    run: DirectConnectivityRun,
    baseline_run: DirectConnectivityRun,
) -> DirectConnectivityRun:
    for role, direct_run in (("portfolio", run), ("baseline", baseline_run)):
        if direct_run.state != DirectRunState.SUCCEEDED:
            raise ValueError(f"{role} direct run did not succeed")
        if not direct_run.exact or direct_run.test_only or direct_run.engine_name != "Circuitscape":
            raise ValueError(f"{role} result is not a direct release Circuitscape execution")
        if direct_run.qa_status not in {QAStatus.ACCEPTED, QAStatus.WARNING_ACCEPTED}:
            raise ValueError(f"{role} direct-run QA is not accepted")
    if run.scenario_id != baseline_run.scenario_id:
        raise ValueError("baseline-relative metrics must stay within one scenario")
    if run.analysis_id != baseline_run.analysis_id:
        raise ValueError("baseline-relative metrics must stay within one analysis")
    if run.revision_id != baseline_run.revision_id:
        raise ValueError("baseline-relative metrics must use one model revision")
    if run.engine_manifest_sha256 != baseline_run.engine_manifest_sha256:
        raise ValueError("baseline-relative metrics must use one engine manifest")
    baseline = {item.pair_id: item.effective_resistance for item in baseline_run.pair_metrics}
    observed = {item.pair_id for item in run.pair_metrics}
    if observed != set(baseline):
        raise ValueError("portfolio and baseline pair sets differ")
    payload = run.model_dump(mode="python")
    payload["pair_metrics"] = [
        PairResistanceMetric(
            pair_id=item.pair_id,
            effective_resistance=item.effective_resistance,
            baseline_effective_resistance=baseline[item.pair_id],
            baseline_relative_change_percent=relative_resistance_reduction_percent(
                baseline[item.pair_id], item.effective_resistance
            ),
        )
        for item in run.pair_metrics
    ]
    return DirectConnectivityRun.model_validate(payload)


def validate_direct_run_qa(
    run: DirectConnectivityRun,
    scenario: ConnectivityScenario,
) -> list[str]:
    findings: list[str] = []
    if run.state != DirectRunState.SUCCEEDED:
        findings.append("Direct job did not succeed.")
    if not run.exact or run.test_only or run.engine_name != "Circuitscape":
        findings.append("Result is not a direct release Circuitscape execution.")
    if run.scenario_id != scenario.scenario_id:
        findings.append("Run scenario identity does not match the requested scenario.")
    expected = set(scenario.included_pair_ids)
    observed = {item.pair_id for item in run.pair_metrics}
    if expected != observed:
        findings.append("Run does not contain the complete included focal-pair set.")
    if run.qa_status not in {QAStatus.ACCEPTED, QAStatus.WARNING_ACCEPTED}:
        findings.append("Run QA is not accepted.")
    if any(item.effective_resistance <= 0 for item in run.pair_metrics):
        findings.append("Effective resistance must be positive.")
    return findings


def validate_complete_ensemble(
    runs: Iterable[DirectConnectivityRun],
    scenarios: Iterable[ConnectivityScenario],
    *,
    portfolio_id: str,
) -> tuple[bool, list[str]]:
    required = {item.scenario_id: item for item in scenarios}
    by_scenario: dict[str, DirectConnectivityRun] = {}
    errors: list[str] = []
    for run in runs:
        if run.portfolio_id != portfolio_id:
            errors.append(f"Run {run.direct_run_id} belongs to another portfolio.")
            continue
        if run.scenario_id in by_scenario:
            errors.append(f"Duplicate direct result for scenario {run.scenario_id}.")
        by_scenario[run.scenario_id] = run
    missing = sorted(set(required) - set(by_scenario))
    extra = sorted(set(by_scenario) - set(required))
    if missing:
        errors.append(f"Missing required scenarios: {missing}.")
    if extra:
        errors.append(f"Unexpected scenarios are not comparable: {extra}.")
    for scenario_id in sorted(set(required) & set(by_scenario)):
        errors.extend(validate_direct_run_qa(by_scenario[scenario_id], required[scenario_id]))
    return not errors, errors


def _passes(operator: RuleOperator, observed: float | bool, threshold: float | bool) -> bool:
    if operator == RuleOperator.AT_LEAST:
        return float(observed) >= float(threshold)
    if operator == RuleOperator.AT_MOST:
        return float(observed) <= float(threshold)
    return observed == threshold


def _scenario_scope(
    rule: ConnectivityDecisionRule, scenarios: list[ConnectivityScenario]
) -> set[str]:
    all_ids = {item.scenario_id for item in scenarios}
    selected = set(rule.scenario_ids) if rule.scenario_ids else all_ids
    unknown = selected - all_ids
    if unknown:
        raise ValueError(f"rule {rule.rule_id} references unknown scenarios: {sorted(unknown)}")
    return selected


def evaluate_portfolio(
    *,
    portfolio: PortfolioSelection,
    runs: list[DirectConnectivityRun],
    baseline_runs: list[DirectConnectivityRun],
    scenarios: list[ConnectivityScenario],
    rules: list[ConnectivityDecisionRule],
    cost: float,
    currency: str,
    unresolved_feasibility: list[str],
    safeguard_tolerances: dict[str, float] | None = None,
) -> PortfolioDecisionSummary:
    complete, ensemble_errors = validate_complete_ensemble(
        runs, scenarios, portfolio_id=portfolio.portfolio_id
    )
    baseline_portfolio_ids = {item.portfolio_id for item in baseline_runs}
    baseline_complete = False
    if len(baseline_portfolio_ids) != 1:
        ensemble_errors.append(
            "Baseline direct runs must belong to exactly one baseline portfolio."
        )
    else:
        baseline_complete, baseline_findings = validate_complete_ensemble(
            baseline_runs,
            scenarios,
            portfolio_id=next(iter(baseline_portfolio_ids)),
        )
        ensemble_errors.extend(f"Baseline: {item}" for item in baseline_findings)
    baseline_by_scenario = {item.scenario_id: item for item in baseline_runs}
    complete = complete and baseline_complete
    attached: dict[str, DirectConnectivityRun] = {}
    if complete:
        for run in runs:
            try:
                attached[run.scenario_id] = attach_baseline_metrics(
                    run, baseline_by_scenario[run.scenario_id]
                )
            except (KeyError, ValueError) as exc:
                ensemble_errors.append(
                    f"Baseline is not comparable for scenario {run.scenario_id}: {exc}."
                )
                complete = False
    if not complete:
        return PortfolioDecisionSummary(
            portfolio_id=portfolio.portfolio_id,
            disposition=PortfolioDisposition.INCOMPLETE,
            cost=cost,
            currency=currency,
            rule_dispositions=[],
            unresolved_feasibility=sorted(set(unresolved_feasibility + ensemble_errors)),
            next_action="Resolve incomplete or rejected direct runs before comparison.",
            direct_run_ids=[item.direct_run_id for item in runs],
        )
    scenario_by_id = {item.scenario_id: item for item in scenarios}
    dispositions: list[RuleDisposition] = []
    safeguard_tolerances = safeguard_tolerances or {}
    for rule in rules:
        disposition_start = len(dispositions)
        scope = _scenario_scope(rule, scenarios)
        if rule.metric == RuleMetric.MAX_COST:
            observed: float | bool = cost
            dispositions.append(
                RuleDisposition(
                    rule_id=rule.rule_id,
                    observed_value=observed,
                    passed=_passes(rule.operator, observed, rule.threshold),
                    explanation=f"Portfolio screening cost is {cost:g} {currency}.",
                )
            )
            continue
        if rule.metric == RuleMetric.REQUIRE_ACCEPTED_QA:
            for scenario_id in sorted(scope):
                accepted = attached[scenario_id].qa_status in {
                    QAStatus.ACCEPTED,
                    QAStatus.WARNING_ACCEPTED,
                }
                dispositions.append(
                    RuleDisposition(
                        rule_id=rule.rule_id,
                        scenario_id=scenario_id,
                        observed_value=accepted,
                        passed=_passes(rule.operator, accepted, rule.threshold),
                        explanation="Direct-run QA disposition.",
                    )
                )
            continue
        if rule.metric == RuleMetric.MAX_REVIEW_MASK_CURRENT_INCREASE_PERCENT:
            for scenario_id in sorted(scope):
                diagnostic_maps = attached[scenario_id].diagnostic_maps
                if rule.required and not diagnostic_maps:
                    dispositions.append(
                        RuleDisposition(
                            rule_id=rule.rule_id,
                            scenario_id=scenario_id,
                            observed_value=False,
                            passed=False,
                            explanation=(
                                "Required review-mask current evidence is missing for this "
                                "scenario."
                            ),
                        )
                    )
                for summary in diagnostic_maps:
                    if summary.relative_change_percent is None:
                        raise ValueError("review-mask rule requires a baseline-relative diagnostic")
                    observed = summary.relative_change_percent
                    dispositions.append(
                        RuleDisposition(
                            rule_id=rule.rule_id,
                            scenario_id=scenario_id,
                            observed_value=observed,
                            passed=_passes(rule.operator, observed, rule.threshold),
                            explanation=(
                                f"Diagnostic current redistribution in mask {summary.mask_id}; "
                                "not predicted animal use."
                            ),
                        )
                    )
            continue
        for scenario_id in sorted(scope):
            scenario = scenario_by_id[scenario_id]
            pair_scope = set(rule.pair_ids) or set(scenario.included_pair_ids)
            metrics = {item.pair_id: item for item in attached[scenario_id].pair_metrics}
            if not pair_scope.issubset(metrics):
                raise ValueError(f"rule {rule.rule_id} references unavailable focal pairs")
            for pair_id in sorted(pair_scope):
                reduction = metrics[pair_id].baseline_relative_change_percent
                if reduction is None:
                    raise ValueError("rule evaluation requires a within-scenario baseline")
                if rule.metric == RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT:
                    observed = reduction
                elif rule.metric == RuleMetric.MAX_RESISTANCE_INCREASE_PERCENT:
                    observed = max(0.0, -reduction)
                elif rule.metric == RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION:
                    if not scenario.safeguard:
                        continue
                    tolerance = safeguard_tolerances.get(scenario_id, 0.0)
                    observed = max(0.0, -reduction)
                    passed = observed <= tolerance
                    dispositions.append(
                        RuleDisposition(
                            rule_id=rule.rule_id,
                            scenario_id=scenario_id,
                            pair_id=pair_id,
                            observed_value=observed,
                            passed=passed,
                            explanation=(
                                "Safeguard within-scenario resistance increase compared with its "
                                "own baseline."
                            ),
                        )
                    )
                    continue
                else:
                    raise ValueError(f"unsupported rule metric: {rule.metric.value}")
                dispositions.append(
                    RuleDisposition(
                        rule_id=rule.rule_id,
                        scenario_id=scenario_id,
                        pair_id=pair_id,
                        observed_value=observed,
                        passed=_passes(rule.operator, observed, rule.threshold),
                        explanation="Within-scenario baseline-relative effective-resistance result.",
                    )
                )
        if rule.required and len(dispositions) == disposition_start:
            dispositions.append(
                RuleDisposition(
                    rule_id=rule.rule_id,
                    observed_value=False,
                    passed=False,
                    explanation=(
                        "Required rule produced no applicable target or safeguard observation."
                    ),
                )
            )
    reductions = [
        (item.baseline_relative_change_percent, scenario_id, item.pair_id)
        for scenario_id, run in attached.items()
        for item in run.pair_metrics
        if item.baseline_relative_change_percent is not None
    ]
    worst_value, worst_scenario, worst_pair = min(reductions, default=(None, None, None))
    required_failed = {
        item.rule_id
        for item in dispositions
        if not item.passed and next(rule for rule in rules if rule.rule_id == item.rule_id).required
    }
    safeguard_results = [
        item.passed
        for item in dispositions
        if next(rule for rule in rules if rule.rule_id == item.rule_id).metric
        == RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION
    ]
    if portfolio.contains_survey_first or unresolved_feasibility:
        disposition = PortfolioDisposition.SURVEY_FIRST
        next_action = "Resolve field, ecological, engineering, and ownership feasibility gaps."
    elif required_failed:
        disposition = PortfolioDisposition.FAILS
        next_action = "Inspect failed rules and controlling assumptions before carry-forward."
    else:
        disposition = PortfolioDisposition.PASSES
        next_action = (
            "Candidate to carry forward for ecological, field, and engineering feasibility review."
        )
    return PortfolioDecisionSummary(
        portfolio_id=portfolio.portfolio_id,
        disposition=disposition,
        cost=cost,
        currency=currency,
        rule_dispositions=dispositions,
        controlling_scenario_id=worst_scenario,
        controlling_pair_id=worst_pair,
        worst_resistance_reduction_percent=worst_value,
        safeguard_passed=all(safeguard_results) if safeguard_results else None,
        review_mask_triggered=any(
            summary.review_triggered for run in attached.values() for summary in run.diagnostic_maps
        ),
        unresolved_feasibility=sorted(set(unresolved_feasibility)),
        next_action=next_action,
        direct_run_ids=sorted(item.direct_run_id for item in runs),
    )


def build_pair_metric_claim(
    *,
    claim_id: str,
    run: DirectConnectivityRun,
    baseline_run: DirectConnectivityRun,
    metric: PairResistanceMetric,
    rule: ConnectivityDecisionRule,
    raster_diff_sha256: str,
    source_asset_ids: list[str],
    assumption_ids: list[str],
    authorization_ids: list[str],
    reported_value: float | None = None,
    reported_metric_label: str | None = None,
) -> ConnectivityEvidenceClaim:
    if len(raster_diff_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in raster_diff_sha256
    ):
        raise ValueError("claim raster diff must be the prepared action-raster SHA-256")
    if run.state != DirectRunState.SUCCEEDED or not run.exact or run.test_only:
        raise ValueError("evidence claims require a successful direct Circuitscape run")
    if (
        baseline_run.state != DirectRunState.SUCCEEDED
        or not baseline_run.exact
        or baseline_run.test_only
    ):
        raise ValueError("evidence claims require a successful direct baseline run")
    if run.scenario_id != baseline_run.scenario_id:
        raise ValueError("evidence claims require a within-scenario direct baseline")
    if run.engine_manifest_sha256 != baseline_run.engine_manifest_sha256:
        raise ValueError("portfolio and baseline evidence require the same pinned engine")
    if metric.baseline_relative_change_percent is None:
        raise ValueError("claim metric requires a within-scenario baseline")
    baseline_metrics = {item.pair_id: item for item in baseline_run.pair_metrics}
    baseline_metric = baseline_metrics.get(metric.pair_id)
    if (
        baseline_metric is None
        or metric.baseline_effective_resistance is None
        or metric.baseline_effective_resistance != baseline_metric.effective_resistance
    ):
        raise ValueError("claim baseline value differs from its direct baseline run")
    if rule.scenario_ids and run.scenario_id not in rule.scenario_ids:
        raise ValueError("claim rule does not apply to the direct-run scenario")
    if rule.pair_ids and metric.pair_id not in rule.pair_ids:
        raise ValueError("claim rule does not apply to the focal pair")
    if not source_asset_ids or len(source_asset_ids) != len(authorization_ids):
        raise ValueError("every source asset requires one explicit authorization node")
    if not assumption_ids:
        raise ValueError("numeric evidence requires at least one documented assumption")
    value = metric.baseline_relative_change_percent if reported_value is None else reported_value
    label = reported_metric_label or (
        "Within-scenario baseline-relative effective-resistance change"
    )
    text = (
        f"{PERMITTED_CLAIM_PREFIX} Focal pair {metric.pair_id} has {label.lower()} of {value:.6g}%."
    )
    assert_safe_ecological_claim(text)
    nodes = [
        EvidenceNode(node_id=claim_id, node_type="CLAIM", label=text),
        EvidenceNode(node_id=f"rule:{rule.rule_id}", node_type="RULE", label=rule.title),
        EvidenceNode(
            node_id=f"metric:{run.direct_run_id}:{metric.pair_id}",
            node_type="METRIC",
            label=label,
            attributes={
                "value": value,
                "units": "percent",
                "pair_id": metric.pair_id,
            },
        ),
        EvidenceNode(
            node_id=f"run:{run.direct_run_id}",
            node_type="DIRECT_RUN",
            label=run.direct_run_id,
            sha256=run.result_sha256,
        ),
        EvidenceNode(
            node_id=f"scenario:{run.scenario_id}", node_type="SCENARIO", label=run.scenario_id
        ),
        EvidenceNode(
            node_id=f"diff:{raster_diff_sha256}",
            node_type="RASTER_DIFF",
            label="Prepared scenario-specific resistance raster",
            sha256=raster_diff_sha256,
        ),
        EvidenceNode(
            node_id=f"engine:{run.engine_manifest_sha256}",
            node_type="ENGINE_MANIFEST",
            label="Pinned Circuitscape engine manifest",
            sha256=run.engine_manifest_sha256,
        ),
        EvidenceNode(
            node_id=f"qa:{run.direct_run_id}",
            node_type="QA",
            label=(run.qa_status.value if run.qa_status else "MISSING"),
        ),
        EvidenceNode(
            node_id=f"artifact:matrix:{run.direct_run_id}:{run.matrix_sha256}",
            node_type="ARTIFACT",
            label="Retained direct effective-resistance matrix",
            sha256=run.matrix_sha256,
            attributes={"role": "matrix", "direct_run_id": run.direct_run_id},
        ),
    ]
    if baseline_run.direct_run_id != run.direct_run_id:
        nodes.extend(
            [
                EvidenceNode(
                    node_id=f"run:{baseline_run.direct_run_id}",
                    node_type="DIRECT_RUN",
                    label=baseline_run.direct_run_id,
                    sha256=baseline_run.result_sha256,
                    attributes={"evidence_role": "within_scenario_baseline"},
                ),
                EvidenceNode(
                    node_id=f"qa:{baseline_run.direct_run_id}",
                    node_type="QA",
                    label=(baseline_run.qa_status.value if baseline_run.qa_status else "MISSING"),
                ),
                EvidenceNode(
                    node_id=(
                        f"artifact:matrix:{baseline_run.direct_run_id}:{baseline_run.matrix_sha256}"
                    ),
                    node_type="ARTIFACT",
                    label="Retained within-scenario baseline effective-resistance matrix",
                    sha256=baseline_run.matrix_sha256,
                    attributes={
                        "role": "matrix",
                        "direct_run_id": baseline_run.direct_run_id,
                    },
                ),
            ]
        )
    nodes.extend(
        EvidenceNode(node_id=f"asset:{item}", node_type="ASSET", label=item)
        for item in source_asset_ids
    )
    nodes.extend(
        EvidenceNode(node_id=f"assumption:{item}", node_type="ASSUMPTION", label=item)
        for item in assumption_ids
    )
    nodes.extend(
        EvidenceNode(node_id=f"authorization:{item}", node_type="AUTHORIZATION", label=item)
        for item in authorization_ids
    )
    metric_id = f"metric:{run.direct_run_id}:{metric.pair_id}"
    run_id = f"run:{run.direct_run_id}"
    scenario_id = f"scenario:{run.scenario_id}"
    diff_id = f"diff:{raster_diff_sha256}"
    edges = [
        EvidenceEdge(
            from_node_id=claim_id, to_node_id=f"rule:{rule.rule_id}", relation="evaluates"
        ),
        EvidenceEdge(from_node_id=claim_id, to_node_id=metric_id, relation="reports"),
        EvidenceEdge(
            from_node_id=metric_id,
            to_node_id=run_id,
            relation="derived_from_portfolio_run",
        ),
        EvidenceEdge(from_node_id=run_id, to_node_id=scenario_id, relation="executes"),
        EvidenceEdge(from_node_id=run_id, to_node_id=diff_id, relation="uses"),
        EvidenceEdge(
            from_node_id=run_id,
            to_node_id=f"engine:{run.engine_manifest_sha256}",
            relation="executed_by",
        ),
        EvidenceEdge(
            from_node_id=run_id, to_node_id=f"qa:{run.direct_run_id}", relation="checked_by"
        ),
        EvidenceEdge(
            from_node_id=run_id,
            to_node_id=f"artifact:matrix:{run.direct_run_id}:{run.matrix_sha256}",
            relation="retained_as",
        ),
    ]
    if baseline_run.direct_run_id != run.direct_run_id:
        baseline_run_id = f"run:{baseline_run.direct_run_id}"
        edges.extend(
            [
                EvidenceEdge(
                    from_node_id=metric_id,
                    to_node_id=baseline_run_id,
                    relation="derived_from_baseline_run",
                ),
                EvidenceEdge(
                    from_node_id=baseline_run_id,
                    to_node_id=scenario_id,
                    relation="executes",
                ),
                EvidenceEdge(
                    from_node_id=baseline_run_id,
                    to_node_id=f"engine:{baseline_run.engine_manifest_sha256}",
                    relation="executed_by",
                ),
                EvidenceEdge(
                    from_node_id=baseline_run_id,
                    to_node_id=f"qa:{baseline_run.direct_run_id}",
                    relation="checked_by",
                ),
                EvidenceEdge(
                    from_node_id=baseline_run_id,
                    to_node_id=(
                        f"artifact:matrix:{baseline_run.direct_run_id}:{baseline_run.matrix_sha256}"
                    ),
                    relation="retained_as",
                ),
            ]
        )
    edges.extend(
        EvidenceEdge(from_node_id=diff_id, to_node_id=f"asset:{item}", relation="derived_from")
        for item in source_asset_ids
    )
    edges.extend(
        EvidenceEdge(from_node_id=scenario_id, to_node_id=f"assumption:{item}", relation="assumes")
        for item in assumption_ids
    )
    edges.extend(
        EvidenceEdge(
            from_node_id=f"asset:{asset}",
            to_node_id=f"authorization:{authorization}",
            relation="authorized_by",
        )
        for asset, authorization in zip(source_asset_ids, authorization_ids, strict=False)
    )
    claim = ConnectivityEvidenceClaim(
        claim_id=claim_id,
        claim_kind="DIRECT_PAIR_METRIC",
        text=text,
        value=value,
        units="percent",
        portfolio_id=run.portfolio_id,
        scenario_id=run.scenario_id,
        pair_id=metric.pair_id,
        rule_id=rule.rule_id,
        direct_run_ids=list(dict.fromkeys([run.direct_run_id, baseline_run.direct_run_id])),
        root_node_id=claim_id,
        nodes=nodes,
        edges=edges,
        limitations=[CONNECTIVITY_LIMITATION, CURRENT_MAP_LIMITATION],
    )
    validate_evidence_claim(claim)
    return claim


def validate_evidence_claim(claim: ConnectivityEvidenceClaim) -> None:
    assert_safe_ecological_claim(claim.text)
    if claim.claim_kind.startswith("DIRECT_") and not claim.text.startswith(PERMITTED_CLAIM_PREFIX):
        raise ValueError("direct numeric claims must use the direct-result qualification")
    if CONNECTIVITY_LIMITATION not in claim.limitations:
        raise ValueError("claim omits the required connectivity limitation")
    nodes = {item.node_id: item for item in claim.nodes}
    if len(nodes) != len(claim.nodes) or claim.root_node_id not in nodes:
        raise ValueError("evidence graph node identities are invalid")
    for edge in claim.edges:
        if edge.from_node_id not in nodes or edge.to_node_id not in nodes:
            raise ValueError("evidence graph edge references a missing node")
    required_by_kind = {
        "DIRECT_PAIR_METRIC": {
            "CLAIM",
            "RULE",
            "METRIC",
            "DIRECT_RUN",
            "SCENARIO",
            "RASTER_DIFF",
            "ASSET",
            "ENGINE_MANIFEST",
            "QA",
            "ASSUMPTION",
            "AUTHORIZATION",
            "ARTIFACT",
        },
        "DIRECT_REFERENCE_COMPARISON": {
            "CLAIM",
            "REFERENCE_COMPARISON",
            "METRIC",
            "DIRECT_RUN",
            "SCENARIO",
            "ASSET",
            "ENGINE_MANIFEST",
            "QA",
            "ASSUMPTION",
            "AUTHORIZATION",
            "ARTIFACT",
        },
        "DIRECT_MAP_DIAGNOSTIC": {
            "CLAIM",
            "RULE",
            "METRIC",
            "DIRECT_RUN",
            "SCENARIO",
            "RASTER_DIFF",
            "ASSET",
            "ENGINE_MANIFEST",
            "QA",
            "ASSUMPTION",
            "AUTHORIZATION",
            "CURRENT_MAP",
            "ARTIFACT",
        },
        "DIRECT_QA": {"CLAIM", "RULE", "DIRECT_RUN", "QA", "ENGINE_MANIFEST"},
        "COST": {"CLAIM", "RULE", "COST_BASIS", "PORTFOLIO", "OPTION"},
        "FEASIBILITY": {"CLAIM", "PORTFOLIO", "FEASIBILITY"},
        "AGGREGATE_RULE": {
            "CLAIM",
            "RULE",
            "REDUCER",
            "DIRECT_RUN",
            "SUPPORTING_CLAIM",
        },
        "PORTFOLIO_DISPOSITION": {
            "CLAIM",
            "PORTFOLIO",
            "DISPOSITION",
            "SUPPORTING_CLAIM",
        },
    }
    required_types = required_by_kind[claim.claim_kind]
    observed_types = {item.node_type for item in claim.nodes}
    if not required_types.issubset(observed_types):
        raise ValueError(
            f"evidence graph omits required node types: {sorted(required_types - observed_types)}"
        )
    if claim.claim_kind in {"DIRECT_PAIR_METRIC", "DIRECT_MAP_DIAGNOSTIC"}:
        raster_diffs = [item for item in claim.nodes if item.node_type == "RASTER_DIFF"]
        if (
            len(raster_diffs) != 1
            or raster_diffs[0].sha256 is None
            or raster_diffs[0].node_id != f"diff:{raster_diffs[0].sha256}"
        ):
            raise ValueError("direct evidence graph requires one SHA-bound prepared raster diff")
    if claim.claim_kind == "DIRECT_REFERENCE_COMPARISON":
        if (
            claim.scenario_id is None
            or claim.pair_id is None
            or len(claim.direct_run_ids) != 1
            or type(claim.value) is not bool
            or claim.units != "within_tolerance"
        ):
            raise ValueError(
                "reference claims require one scenario, pair, direct run, and Boolean "
                "within_tolerance value"
            )
        if not {CURRENT_RASTER_LIMITATION, FITNESS_LIMITATION}.issubset(claim.limitations):
            raise ValueError(
                "reference claims require the current-raster and model-fitness limitations"
            )
        comparison_nodes = [
            item for item in claim.nodes if item.node_type == "REFERENCE_COMPARISON"
        ]
        if len(comparison_nodes) != 1:
            raise ValueError("reference evidence requires one comparison node")
        comparison = comparison_nodes[0]
        comparison_id = comparison.attributes.get("comparison_id")
        if (
            not isinstance(comparison_id, str)
            or not comparison_id
            or comparison.node_id != f"reference-comparison:{comparison_id}"
            or comparison.attributes.get("scenario_id") != claim.scenario_id
            or comparison.attributes.get("pair_id") != claim.pair_id
            or comparison.attributes.get("direct_run_id") != claim.direct_run_ids[0]
        ):
            raise ValueError("reference comparison node does not match the claim subject")
        direct_run_nodes = {item.node_id for item in claim.nodes if item.node_type == "DIRECT_RUN"}
        if direct_run_nodes != {f"run:{claim.direct_run_ids[0]}"}:
            raise ValueError("reference claim graph differs from its exact direct run")
    if claim.claim_kind == "AGGREGATE_RULE":
        if claim.rule_id is None or not claim.rule_id.startswith("aggregate:"):
            raise ValueError("aggregate evidence requires an explicit aggregate rule identity")
        expected_rule_id = f"rule:{claim.rule_id}"
        rule_nodes = [
            item
            for item in claim.nodes
            if item.node_type == "RULE" and item.node_id == expected_rule_id
        ]
        reducer_nodes = [item for item in claim.nodes if item.node_type == "REDUCER"]
        expected_reducer_id = f"reducer:{claim.claim_id}"
        if len(rule_nodes) != 1 or len(reducer_nodes) != 1:
            raise ValueError("aggregate evidence requires one aggregate rule and reducer")
        reducer = reducer_nodes[0]
        if (
            reducer.node_id != expected_reducer_id
            or reducer.attributes.get("aggregate_key") != claim.rule_id.removeprefix("aggregate:")
            or not isinstance(reducer.attributes.get("semantics"), str)
            or not reducer.attributes.get("semantics")
        ):
            raise ValueError("aggregate reducer identity or semantics are invalid")
        support_nodes = [item for item in claim.nodes if item.node_type == "SUPPORTING_CLAIM"]
        support_ids: set[str] = set()
        for node in support_nodes:
            supporting_claim_id = node.attributes.get("claim_id")
            if (
                not isinstance(supporting_claim_id, str)
                or not supporting_claim_id
                or node.node_id != f"supporting-claim:{supporting_claim_id}"
                or supporting_claim_id == claim.claim_id
            ):
                raise ValueError("aggregate evidence has an invalid supporting claim node")
            support_ids.add(supporting_claim_id)
        if len(support_ids) != len(support_nodes):
            raise ValueError("aggregate evidence has duplicate supporting claim identities")
        if reducer.attributes.get("supporting_claim_count") != len(
            support_nodes
        ) or reducer.attributes.get("direct_run_count") != len(claim.direct_run_ids):
            raise ValueError("aggregate reducer counts differ from its evidence graph")
        support_node_ids = {item.node_id for item in support_nodes}
        reducer_support_edges = {
            edge.to_node_id
            for edge in claim.edges
            if edge.from_node_id == expected_reducer_id and edge.relation == "reduces_claim"
        }
        if reducer_support_edges != support_node_ids:
            raise ValueError("aggregate reducer does not link every supporting claim")
        expected_run_nodes = {f"run:{item}" for item in claim.direct_run_ids}
        observed_run_nodes = {
            item.node_id for item in claim.nodes if item.node_type == "DIRECT_RUN"
        }
        if observed_run_nodes != expected_run_nodes:
            raise ValueError("aggregate graph direct-run nodes differ from its run identities")
        support_run_edges = {
            edge.from_node_id: set()
            for edge in claim.edges
            if edge.from_node_id in support_node_ids and edge.relation == "supported_by_direct_run"
        }
        for edge in claim.edges:
            if edge.from_node_id in support_node_ids and edge.relation == "supported_by_direct_run":
                support_run_edges[edge.from_node_id].add(edge.to_node_id)
        if (
            set(support_run_edges) != support_node_ids
            or any(not targets for targets in support_run_edges.values())
            or set().union(*support_run_edges.values()) != expected_run_nodes
            or any(
                not targets.issubset(expected_run_nodes) for targets in support_run_edges.values()
            )
        ):
            raise ValueError("aggregate supporting claims do not cover its exact direct runs")
        if not any(
            edge.from_node_id == claim.claim_id
            and edge.to_node_id == expected_rule_id
            and edge.relation == "evaluates"
            for edge in claim.edges
        ) or not any(
            edge.from_node_id == claim.claim_id
            and edge.to_node_id == expected_reducer_id
            and edge.relation == "reports"
            for edge in claim.edges
        ):
            raise ValueError("aggregate claim is detached from its rule or reducer")
    if claim.claim_kind == "PORTFOLIO_DISPOSITION":
        disposition_id = f"disposition:{claim.portfolio_id}"
        support_nodes = [item for item in claim.nodes if item.node_type == "SUPPORTING_CLAIM"]
        support_ids: set[str] = set()
        for node in support_nodes:
            supporting_claim_id = node.attributes.get("claim_id")
            if (
                not isinstance(supporting_claim_id, str)
                or not supporting_claim_id
                or node.node_id != f"supporting-claim:{supporting_claim_id}"
                or supporting_claim_id == claim.claim_id
            ):
                raise ValueError("portfolio disposition has an invalid supporting claim node")
            support_ids.add(supporting_claim_id)
        if len(support_ids) != len(support_nodes):
            raise ValueError("portfolio disposition has duplicate supporting claim identities")
        support_edges = {
            edge.to_node_id
            for edge in claim.edges
            if edge.from_node_id == disposition_id and edge.relation == "supported_by_claim"
        }
        if support_edges != {item.node_id for item in support_nodes}:
            raise ValueError("portfolio disposition does not link every supporting claim")
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in claim.edges:
        adjacency[edge.from_node_id].append(edge.to_node_id)
    reached = {claim.root_node_id}
    queue = deque([claim.root_node_id])
    while queue:
        current = queue.popleft()
        for target in adjacency[current]:
            if target not in reached:
                reached.add(target)
                queue.append(target)
    if reached != set(nodes):
        raise ValueError("every evidence node must be reachable from the claim")


def disposition_supporting_claim_ids(claim: ConnectivityEvidenceClaim) -> set[str]:
    """Return the exact supporting claim identities embedded in a disposition graph."""

    if claim.claim_kind != "PORTFOLIO_DISPOSITION":
        raise ValueError("supporting claim identities belong only to disposition claims")
    validate_evidence_claim(claim)
    return {
        str(node.attributes["claim_id"])
        for node in claim.nodes
        if node.node_type == "SUPPORTING_CLAIM"
    }


def aggregate_supporting_claim_ids(claim: ConnectivityEvidenceClaim) -> set[str]:
    """Return exact primitive claim identities embedded in an aggregate graph."""

    if claim.claim_kind != "AGGREGATE_RULE":
        raise ValueError("aggregate support identities belong only to aggregate claims")
    validate_evidence_claim(claim)
    return {
        str(node.attributes["claim_id"])
        for node in claim.nodes
        if node.node_type == "SUPPORTING_CLAIM"
    }


def _verified_evidence_file(path: Path, expected_sha256: str, label: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"retained {label} artifact is absent or unsafe")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise ValueError(f"retained {label} artifact differs from its direct-run hash")
    return path


def verify_direct_claim_against_retained_evidence(
    *,
    claim: ConnectivityEvidenceClaim,
    bundle: ConnectivityProjectBundle,
    baseline_portfolio_id: str,
    runs_by_id: Mapping[str, DirectConnectivityRun],
    artifact_paths: Mapping[tuple[str, str], Path],
    asset_paths: Mapping[str, Path],
) -> None:
    """Recompute reportable direct values from retained solver and raster bytes."""

    if claim.claim_kind not in {"DIRECT_PAIR_METRIC", "DIRECT_MAP_DIAGNOSTIC"}:
        return
    validate_evidence_claim(claim)
    try:
        claim_runs = [runs_by_id[item] for item in claim.direct_run_ids]
    except KeyError as exc:
        raise ValueError("direct claim references unavailable retained runs") from exc
    subject_runs = [
        item
        for item in claim_runs
        if item.portfolio_id == claim.portfolio_id and item.scenario_id == claim.scenario_id
    ]
    baseline_runs = [
        item
        for item in claim_runs
        if item.portfolio_id == baseline_portfolio_id and item.scenario_id == claim.scenario_id
    ]
    if len(subject_runs) != 1 or len(baseline_runs) != 1:
        raise ValueError("direct claim lacks one subject and one within-scenario baseline")
    subject = subject_runs[0]
    baseline = baseline_runs[0]
    scenario = next(
        (item for item in bundle.scenarios if item.scenario_id == claim.scenario_id), None
    )
    if scenario is None:
        raise ValueError("direct claim scenario is absent from the model snapshot")

    if claim.claim_kind == "DIRECT_PAIR_METRIC":
        focal_set = next(
            (item for item in bundle.focal_area_sets if item.focal_set_id == scenario.focal_set_id),
            None,
        )
        if focal_set is None:
            raise ValueError("direct claim focal set is absent from the model snapshot")
        focal_pairs = {item.pair_id: item for item in focal_set.pairs}
        try:
            prepared_pairs = [
                PreparedPair(
                    pair_id=pair_id,
                    from_focal_id=focal_pairs[pair_id].from_focal_id,
                    to_focal_id=focal_pairs[pair_id].to_focal_id,
                )
                for pair_id in scenario.included_pair_ids
            ]
        except KeyError as exc:
            raise ValueError("scenario includes an unavailable focal pair") from exc

        raw_by_run: dict[str, dict[str, float]] = {}
        for run in {item.direct_run_id: item for item in (subject, baseline)}.values():
            if run.matrix_sha256 is None:
                raise ValueError("direct claim run has no retained matrix identity")
            path = artifact_paths.get((run.direct_run_id, "matrix"))
            if path is None:
                raise ValueError("direct claim run has no retained matrix artifact")
            _verified_evidence_file(path, run.matrix_sha256, "matrix")
            parsed = parse_circuitscape_matrix(path, prepared_pairs)
            raw = {item.pair_id: item.effective_resistance for item in parsed}
            stored = {item.pair_id: item.effective_resistance for item in run.pair_metrics}
            if raw != stored:
                raise ValueError("stored direct metrics differ from the retained raw matrix")
            raw_by_run[run.direct_run_id] = raw
        if claim.pair_id is None:
            raise ValueError("direct pair claim omits its focal-pair identity")
        subject_value = raw_by_run[subject.direct_run_id][claim.pair_id]
        baseline_value = raw_by_run[baseline.direct_run_id][claim.pair_id]
        reduction = (baseline_value - subject_value) / baseline_value * 100.0
        rule = next((item for item in bundle.decision_rules if item.rule_id == claim.rule_id), None)
        if rule is None and claim.rule_id == "evidence:within-scenario-resistance-change":
            semantic_rule_nodes = [
                item
                for item in claim.nodes
                if item.node_type == "RULE"
                and item.node_id == "rule:evidence:within-scenario-resistance-change"
            ]
            if len(semantic_rule_nodes) != 1:
                raise ValueError("semantic direct-pair evidence omits its exact rule node")
            expected_value = reduction
        elif rule is None:
            raise ValueError("direct pair claim references an unavailable rule")
        elif rule.metric == RuleMetric.MIN_RESISTANCE_REDUCTION_PERCENT:
            expected_value = reduction
        elif rule.metric in {
            RuleMetric.MAX_RESISTANCE_INCREASE_PERCENT,
            RuleMetric.REQUIRE_SAFEGUARD_NONREGRESSION,
        }:
            expected_value = max(0.0, -reduction)
        else:
            raise ValueError("direct pair claim is attached to an incompatible rule")
        metric_nodes = [item for item in claim.nodes if item.node_type == "METRIC"]
        if (
            claim.units != "percent"
            or not isinstance(claim.value, (int, float))
            or isinstance(claim.value, bool)
            or not math.isclose(float(claim.value), expected_value, rel_tol=1e-12, abs_tol=1e-12)
            or len(metric_nodes) != 1
            or metric_nodes[0].attributes.get("pair_id") != claim.pair_id
            or not isinstance(metric_nodes[0].attributes.get("value"), (int, float))
            or not math.isclose(
                float(metric_nodes[0].attributes["value"]),
                expected_value,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("direct pair claim differs from retained raw matrices")
        matrix_nodes = {
            (str(item.attributes.get("direct_run_id")), item.sha256)
            for item in claim.nodes
            if item.node_type == "ARTIFACT" and item.attributes.get("role") == "matrix"
        }
        expected_matrix_nodes = {
            (item.direct_run_id, item.matrix_sha256)
            for item in {run.direct_run_id: run for run in (subject, baseline)}.values()
        }
        if matrix_nodes != expected_matrix_nodes:
            raise ValueError("direct pair claim graph omits its retained raw matrices")
        return

    metric_nodes = [item for item in claim.nodes if item.node_type == "METRIC"]
    if len(metric_nodes) != 1:
        raise ValueError("direct map claim requires one diagnostic metric node")
    mask_id = metric_nodes[0].attributes.get("mask_id")
    if not isinstance(mask_id, str):
        raise TypeError("direct map claim omits its review-mask identity")
    review = next((item for item in bundle.review_masks if item.mask_id == mask_id), None)
    if review is None or review.grid_id != scenario.grid_id:
        raise ValueError("direct map claim references an unavailable review mask")
    assets = {item.asset_id: item for item in bundle.assets}
    mask_asset = assets.get(review.asset_id)
    mask_path = asset_paths.get(review.asset_id)
    if mask_asset is None or mask_path is None:
        raise ValueError("direct map claim has no retained review-mask source")
    _verified_evidence_file(mask_path, mask_asset.sha256, "review mask")
    mask = load_raster(mask_path, mask_asset)
    current_paths: dict[str, Path] = {}
    for run in {item.direct_run_id: item for item in (subject, baseline)}.values():
        if run.current_map_sha256 is None:
            raise ValueError("direct map claim run has no current-raster identity")
        path = artifact_paths.get((run.direct_run_id, "current_map"))
        if path is None:
            raise ValueError("direct map claim run has no retained current raster")
        current_paths[run.direct_run_id] = _verified_evidence_file(
            path, run.current_map_sha256, "current map"
        )
    comparison = compare_current_maps_in_mask(
        current_paths[baseline.direct_run_id], current_paths[subject.direct_run_id], mask
    )
    if comparison.baseline_total <= 0:
        raise ValueError("retained baseline current is zero inside the review mask")
    expected_value = (
        (comparison.portfolio_total - comparison.baseline_total) / comparison.baseline_total * 100.0
    )
    stored = next((item for item in subject.diagnostic_maps if item.mask_id == mask_id), None)
    expected_attributes: dict[str, float | int | str] = {
        "value": expected_value,
        "baseline_total_current": comparison.baseline_total,
        "portfolio_total_current": comparison.portfolio_total,
        "reviewed_cell_count": comparison.reviewed_cell_count,
        "included_cell_count": comparison.included_cell_count,
        "excluded_nodata_cell_count": comparison.excluded_nodata_cell_count,
        "raster_nodata_cell_count": comparison.raster_nodata_cell_count,
        "nodata_topology_sha256": comparison.nodata_topology_sha256,
    }
    if (
        stored is None
        or stored.nodata_topology_sha256 != comparison.nodata_topology_sha256
        or stored.reviewed_cell_count != comparison.reviewed_cell_count
        or stored.included_cell_count != comparison.included_cell_count
        or stored.excluded_nodata_cell_count != comparison.excluded_nodata_cell_count
        or stored.raster_nodata_cell_count != comparison.raster_nodata_cell_count
        or stored.baseline_total_current != comparison.baseline_total
        or stored.portfolio_total_current != comparison.portfolio_total
        or stored.relative_change_percent is None
        or not math.isclose(
            stored.relative_change_percent, expected_value, rel_tol=1e-12, abs_tol=1e-12
        )
        or claim.units != "percent"
        or not isinstance(claim.value, (int, float))
        or isinstance(claim.value, bool)
        or not math.isclose(float(claim.value), expected_value, rel_tol=1e-12, abs_tol=1e-12)
    ):
        raise ValueError("direct map claim differs from retained current rasters")
    for key, expected in expected_attributes.items():
        observed = metric_nodes[0].attributes.get(key)
        if isinstance(expected, float):
            if (
                not isinstance(observed, (int, float))
                or isinstance(observed, bool)
                or not math.isclose(float(observed), expected, rel_tol=1e-12, abs_tol=1e-12)
            ):
                raise ValueError("direct map claim metric differs from retained current rasters")
        elif observed != expected:
            raise ValueError("direct map claim metric differs from retained current rasters")


def validate_aggregate_claim_support(
    *,
    summaries: Iterable[PortfolioDecisionSummary],
    runs: Iterable[DirectConnectivityRun],
    claims: Iterable[ConnectivityEvidenceClaim],
) -> None:
    """Recompute every aggregate from its exact persisted primitive support.

    Structural graph validation alone cannot prove a reducer: a payload could
    consistently rewrite its value and its decorative graph.  This validator
    binds each aggregate to the complete direct primitive collection and, where
    applicable, to the summary rule observations it reduces.
    """

    summary_list = list(summaries)
    run_list = list(runs)
    claim_list = list(claims)
    summaries_by_portfolio = {item.portfolio_id: item for item in summary_list}
    runs_by_id = {item.direct_run_id: item for item in run_list}
    claims_by_id = {item.claim_id: item for item in claim_list}
    if (
        len(summaries_by_portfolio) != len(summary_list)
        or len(runs_by_id) != len(run_list)
        or len(claims_by_id) != len(claim_list)
    ):
        raise ValueError("aggregate validation requires unique persisted identities")

    aggregate_keys_by_portfolio: set[tuple[str, str]] = set()
    for aggregate in (item for item in claim_list if item.claim_kind == "AGGREGATE_RULE"):
        validate_evidence_claim(aggregate)
        summary = summaries_by_portfolio.get(aggregate.portfolio_id)
        if summary is None:
            raise ValueError("aggregate evidence has no persisted portfolio summary")
        aggregate_key = str(aggregate.rule_id).removeprefix("aggregate:")
        aggregate_identity = (aggregate.portfolio_id, aggregate_key)
        if aggregate_identity in aggregate_keys_by_portfolio:
            raise ValueError("portfolio has duplicate aggregate evidence")
        aggregate_keys_by_portfolio.add(aggregate_identity)
        support_ids = aggregate_supporting_claim_ids(aggregate)
        if not support_ids.issubset(claims_by_id):
            raise ValueError("aggregate evidence references unavailable primitive claims")
        supporting = [claims_by_id[item] for item in sorted(support_ids)]
        allowed_primitive_kinds = {
            "DIRECT_PAIR_METRIC",
            "DIRECT_MAP_DIAGNOSTIC",
            "DIRECT_QA",
        }
        if not supporting or any(
            item.claim_kind not in allowed_primitive_kinds
            or item.portfolio_id != aggregate.portfolio_id
            for item in supporting
        ):
            raise ValueError("aggregate evidence must reduce subject direct primitives")
        expected_run_ids = {run_id for item in supporting for run_id in item.direct_run_ids}
        if not expected_run_ids or expected_run_ids != set(aggregate.direct_run_ids):
            raise ValueError("aggregate direct-run ensemble differs from its primitive claims")
        if not expected_run_ids.issubset(runs_by_id):
            raise ValueError("aggregate references unavailable direct-run evidence")

        reducer = next(item for item in aggregate.nodes if item.node_type == "REDUCER")
        semantics = reducer.attributes.get("semantics")
        if aggregate_key == "worst_controlling_resistance_change":
            expected_semantics = "MIN_BY_NUMERIC_VALUE_OVER_ALL_COMPLETE_SCENARIO_PAIR_PRIMITIVES"
            if semantics != expected_semantics or aggregate.units != "percent":
                raise ValueError("worst-case aggregate has incorrect reducer semantics")
            if any(
                item.claim_kind != "DIRECT_PAIR_METRIC"
                or item.rule_id != "evidence:within-scenario-resistance-change"
                or item.scenario_id is None
                or item.pair_id is None
                for item in supporting
            ):
                raise ValueError("worst-case aggregate support is not intrinsic pair evidence")
            subject_runs = [
                runs_by_id[item] for item in summary.direct_run_ids if item in runs_by_id
            ]
            if len(subject_runs) != len(summary.direct_run_ids) or any(
                item.portfolio_id != aggregate.portfolio_id for item in subject_runs
            ):
                raise ValueError("worst-case aggregate lacks its complete subject-run ensemble")
            expected_scope = {
                (run.scenario_id, metric.pair_id)
                for run in subject_runs
                for metric in run.pair_metrics
            }
            support_by_scope = {
                (str(item.scenario_id), str(item.pair_id)): item for item in supporting
            }
            if len(support_by_scope) != len(supporting) or set(support_by_scope) != expected_scope:
                raise ValueError(
                    "worst-case reducer does not cover every complete scenario/pair primitive"
                )
            observations: list[tuple[float, str, str]] = []
            for run in subject_runs:
                for metric in run.pair_metrics:
                    primitive = support_by_scope[(run.scenario_id, metric.pair_id)]
                    primitive_runs = [runs_by_id[item] for item in primitive.direct_run_ids]
                    subject_matches = [
                        item
                        for item in primitive_runs
                        if item.portfolio_id == aggregate.portfolio_id
                        and item.scenario_id == run.scenario_id
                    ]
                    baseline_matches = [
                        item
                        for item in primitive_runs
                        if item.scenario_id == run.scenario_id and item not in subject_matches
                    ]
                    if len(subject_matches) != 1:
                        raise ValueError("worst-case primitive lacks its exact subject run")
                    if not baseline_matches and len(primitive_runs) == 1:
                        baseline_matches = subject_matches
                    if len(baseline_matches) != 1:
                        raise ValueError("worst-case primitive lacks its within-scenario baseline")
                    subject_metric = next(
                        (
                            item
                            for item in subject_matches[0].pair_metrics
                            if item.pair_id == metric.pair_id
                        ),
                        None,
                    )
                    baseline_metric = next(
                        (
                            item
                            for item in baseline_matches[0].pair_metrics
                            if item.pair_id == metric.pair_id
                        ),
                        None,
                    )
                    if subject_metric is None or baseline_metric is None:
                        raise ValueError("worst-case primitive omits a direct focal-pair metric")
                    expected_primitive_value = (
                        (baseline_metric.effective_resistance - subject_metric.effective_resistance)
                        / baseline_metric.effective_resistance
                        * 100.0
                    )
                    if (
                        not isinstance(primitive.value, (int, float))
                        or isinstance(primitive.value, bool)
                        or not math.isclose(
                            float(primitive.value),
                            expected_primitive_value,
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                    ):
                        raise ValueError(
                            "worst-case primitive differs from its direct-run pair metric"
                        )
                    observations.append(
                        (
                            expected_primitive_value,
                            run.scenario_id,
                            metric.pair_id,
                        )
                    )
            expected_value, expected_scenario, expected_pair = min(observations)
            if (
                not isinstance(aggregate.value, (int, float))
                or isinstance(aggregate.value, bool)
                or not math.isclose(
                    float(aggregate.value), expected_value, rel_tol=0.0, abs_tol=1e-12
                )
                or aggregate.scenario_id != expected_scenario
                or aggregate.pair_id != expected_pair
                or summary.controlling_scenario_id != expected_scenario
                or summary.controlling_pair_id != expected_pair
                or summary.worst_resistance_reduction_percent is None
                or not math.isclose(
                    summary.worst_resistance_reduction_percent,
                    expected_value,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                raise ValueError("worst-case aggregate does not equal its direct argmin")
            continue

        if aggregate_key == "safeguard_nonregression":
            if (
                semantics != "ALL_REQUIRED_SAFEGUARD_RULE_OBSERVATIONS_PASS"
                or aggregate.units != "boolean"
                or any(item.claim_kind != "DIRECT_PAIR_METRIC" for item in supporting)
            ):
                raise ValueError("safeguard aggregate has incorrect primitive semantics")
            reducer_id = f"reducer:{aggregate.claim_id}"
            source_rule_ids = {
                edge.to_node_id.removeprefix("rule:")
                for edge in aggregate.edges
                if edge.from_node_id == reducer_id and edge.relation == "applies_rule"
            }
            if not source_rule_ids:
                raise ValueError("safeguard aggregate omits its source decision rules")
            dispositions = [
                item for item in summary.rule_dispositions if item.rule_id in source_rule_ids
            ]
            if (
                not dispositions
                or any(item.evidence_claim_id is None for item in dispositions)
                or {str(item.evidence_claim_id) for item in dispositions} != support_ids
            ):
                raise ValueError("safeguard aggregate omits exact rule observations")
            for disposition in dispositions:
                primitive = claims_by_id[str(disposition.evidence_claim_id)]
                observed = disposition.observed_value
                if (
                    not isinstance(observed, (int, float))
                    or isinstance(observed, bool)
                    or not isinstance(primitive.value, (int, float))
                    or isinstance(primitive.value, bool)
                    or not math.isclose(
                        float(primitive.value), float(observed), rel_tol=0.0, abs_tol=1e-12
                    )
                ):
                    raise ValueError("safeguard primitive differs from its rule observation")
            expected = all(item.passed for item in dispositions)
            if aggregate.value is not expected or summary.safeguard_passed is not expected:
                raise ValueError("safeguard aggregate differs from its rule observations")
            continue

        if aggregate_key == "review_mask_trigger":
            if (
                semantics != "ANY_REVIEW_MASK_PRIMITIVE_HAS_REVIEW_TRIGGERED_TRUE"
                or aggregate.units != "boolean"
                or any(item.claim_kind != "DIRECT_MAP_DIAGNOSTIC" for item in supporting)
            ):
                raise ValueError("review-mask aggregate has incorrect primitive semantics")
            expected_map_claims = {
                item.claim_id
                for item in claim_list
                if item.portfolio_id == aggregate.portfolio_id
                and item.claim_kind == "DIRECT_MAP_DIAGNOSTIC"
            }
            if support_ids != expected_map_claims:
                raise ValueError("review-mask aggregate omits retained map primitives")
            triggers: list[bool] = []
            for primitive in supporting:
                metric_nodes = [item for item in primitive.nodes if item.node_type == "METRIC"]
                if len(metric_nodes) != 1 or not isinstance(
                    metric_nodes[0].attributes.get("review_triggered"), bool
                ):
                    raise ValueError("review-mask primitive omits its trigger disposition")
                triggers.append(bool(metric_nodes[0].attributes["review_triggered"]))
            expected = any(triggers)
            if aggregate.value is not expected or summary.review_mask_triggered is not expected:
                raise ValueError("review-mask aggregate differs from its exact primitives")
            continue

        raise ValueError(f"unsupported aggregate reducer: {aggregate_key}")


def validate_portfolio_disposition_support(
    *,
    summaries: Iterable[PortfolioDecisionSummary],
    runs: Iterable[DirectConnectivityRun],
    claims: Iterable[ConnectivityEvidenceClaim],
) -> None:
    """Bind each portfolio disposition to all of its persisted decision evidence."""

    summary_list = list(summaries)
    run_list = list(runs)
    claim_list = list(claims)
    validate_aggregate_claim_support(
        summaries=summary_list,
        runs=run_list,
        claims=claim_list,
    )
    claims_by_id = {item.claim_id: item for item in claim_list}
    if len(claims_by_id) != len(claim_list):
        raise ValueError("analysis evidence contains duplicate claim identities")
    for summary in summary_list:
        disposition_id = summary.disposition_evidence_claim_id
        disposition = claims_by_id.get(disposition_id or "")
        if (
            disposition is None
            or disposition.claim_kind != "PORTFOLIO_DISPOSITION"
            or disposition.portfolio_id != summary.portfolio_id
            or disposition.value != summary.disposition.value
            or set(disposition.direct_run_ids) != set(summary.direct_run_ids)
        ):
            raise ValueError("portfolio summary lacks its exact disposition evidence claim")
        observed_support = disposition_supporting_claim_ids(disposition)
        expected_support = {
            item.claim_id
            for item in claim_list
            if item.portfolio_id == summary.portfolio_id
            and item.claim_kind != "PORTFOLIO_DISPOSITION"
        }
        if observed_support != expected_support:
            raise ValueError(
                "portfolio disposition support does not exactly cover its decision claims"
            )
        supporting = [claims_by_id[item] for item in sorted(observed_support)]
        if not any(item.claim_kind == "COST" for item in supporting):
            raise ValueError("portfolio disposition support omits reviewed cost evidence")
        qa_runs = {
            item.direct_run_ids[0]
            for item in supporting
            if item.claim_kind == "DIRECT_QA" and len(item.direct_run_ids) == 1
        }
        if qa_runs != set(summary.direct_run_ids):
            raise ValueError("portfolio disposition support omits direct-run QA evidence")
        rule_claim_ids = {
            item.evidence_claim_id
            for item in summary.rule_dispositions
            if item.evidence_claim_id is not None
        }
        if rule_claim_ids != {
            item.evidence_claim_id for item in summary.rule_dispositions
        } or not rule_claim_ids.issubset(observed_support):
            raise ValueError("portfolio disposition support omits rule evidence")
        feasibility_claims = {
            item.claim_id for item in supporting if item.claim_kind == "FEASIBILITY"
        }
        if (summary.disposition == PortfolioDisposition.SURVEY_FIRST) != bool(feasibility_claims):
            raise ValueError("SURVEY_FIRST disposition and feasibility evidence must agree")
