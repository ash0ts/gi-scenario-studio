from __future__ import annotations

from copy import deepcopy
from typing import Any

from ecocorridor_core.cache_identity import (
    RIDGEWAY_MODEL_RUN_CACHE_COMPONENTS,
    ridgeway_model_run_cache_key,
    ridgeway_model_run_cache_payload,
)
from ecocorridor_core.contracts import canonical_sha256


def _components() -> dict[str, Any]:
    return {
        "mode": "pairwise",
        "raster_semantics": "resistance",
        "resistance_sha256": "1" * 64,
        "focal_sha256": "2" * 64,
        "included_pairs_sha256": "3" * 64,
        "pairs": [[1, 2], [3, 4]],
        "settings": {
            "precision": "double",
            "neighbor_rule": "EIGHT",
            "resistance_average": "ARITHMETIC",
            "emit_current_map": False,
        },
        "engine_manifest_sha256": "4" * 64,
        "engine_environment_sha256": "5" * 64,
        "workflow_manifest_sha256": "6" * 64,
    }


def test_ridgeway_cache_identity_is_complete_ordered_and_versioned() -> None:
    components = _components()
    payload = ridgeway_model_run_cache_payload(**components)
    key = ridgeway_model_run_cache_key(**components)

    assert payload["schema_version"] == "ecocorridor-model-run-cache/v1.0"
    assert tuple(item[0] for item in payload["components"]) == (RIDGEWAY_MODEL_RUN_CACHE_COMPONENTS)
    assert key == canonical_sha256(payload)

    omitted = deepcopy(payload)
    omitted["components"] = omitted["components"][:-1]
    reordered = deepcopy(payload)
    reordered["components"][7], reordered["components"][8] = (
        reordered["components"][8],
        reordered["components"][7],
    )
    assert canonical_sha256(omitted) != key
    assert canonical_sha256(reordered) != key


def test_ridgeway_cache_identity_binds_every_engine_and_workflow_digest() -> None:
    components = _components()
    expected = ridgeway_model_run_cache_key(**components)
    for field in (
        "engine_manifest_sha256",
        "engine_environment_sha256",
        "workflow_manifest_sha256",
    ):
        changed = {**components, field: "f" * 64}
        assert ridgeway_model_run_cache_key(**changed) != expected
