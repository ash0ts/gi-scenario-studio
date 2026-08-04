"""Stable-identity checks shared by professional import and execution boundaries.

This module is intentionally separate from the pinned Ridgeway scientific
contract snapshot.  Adding an import safety check must not relabel previously
executed direct Circuitscape evidence as though its contract bytes were
unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from .contracts import ConnectivityProjectBundle


def _require_unique(label: str, identities: Sequence[str]) -> None:
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} IDs must be unique")


def assert_unique_bundle_identities(bundle: ConnectivityProjectBundle) -> None:
    """Reject ambiguous stable IDs before preflight, persistence, or execution."""

    effects = [effect for option in bundle.intervention_options for effect in option.effects]

    collections = (
        ("asset", [item.asset_id for item in bundle.assets]),
        (
            "resistance hypothesis",
            [item.hypothesis_id for item in bundle.resistance_hypotheses],
        ),
        ("focal-area set", [item.focal_set_id for item in bundle.focal_area_sets]),
        ("scenario", [item.scenario_id for item in bundle.scenarios]),
        ("reference binding", [item.binding_id for item in bundle.reference_bindings]),
        ("intervention site", [item.site_id for item in bundle.intervention_sites]),
        ("intervention option", [item.option_id for item in bundle.intervention_options]),
        ("action effect", [item.effect_id for item in effects]),
        ("action-effect basis", [item.basis.basis_id for item in effects]),
        ("safeguard", [item.safeguard_id for item in bundle.safeguards]),
        ("review mask", [item.mask_id for item in bundle.review_masks]),
        ("decision rule", [item.rule_id for item in bundle.decision_rules]),
    )
    for label, identities in collections:
        _require_unique(label, identities)
