"""Versioned scientific cache identities shared by execution and verification."""

from __future__ import annotations

from .contracts import canonical_sha256

RIDGEWAY_MODEL_RUN_CACHE_SCHEMA = "ecocorridor-model-run-cache/v1.0"
RIDGEWAY_MODEL_RUN_CACHE_COMPONENTS = (
    "mode",
    "raster_semantics",
    "resistance_sha256",
    "focal_sha256",
    "included_pairs_sha256",
    "pairs",
    "settings",
    "engine_manifest_sha256",
    "engine_environment_sha256",
    "workflow_manifest_sha256",
)


def ridgeway_model_run_cache_payload(
    *,
    mode: str,
    raster_semantics: str,
    resistance_sha256: str,
    focal_sha256: str,
    included_pairs_sha256: str,
    pairs: object,
    settings: object,
    engine_manifest_sha256: str,
    engine_environment_sha256: str,
    workflow_manifest_sha256: str,
) -> dict[str, object]:
    """Build the ordered, versioned identity for one direct Ridgeway run.

    ``components`` is deliberately an ordered list.  Its order is part of the
    public cache contract, so omitting or reordering a component changes the
    canonical digest and cannot silently reuse old scientific output.
    """

    return {
        "schema_version": RIDGEWAY_MODEL_RUN_CACHE_SCHEMA,
        "components": [
            ["mode", mode],
            ["raster_semantics", raster_semantics],
            ["resistance_sha256", resistance_sha256],
            ["focal_sha256", focal_sha256],
            ["included_pairs_sha256", included_pairs_sha256],
            ["pairs", pairs],
            ["settings", settings],
            ["engine_manifest_sha256", engine_manifest_sha256],
            ["engine_environment_sha256", engine_environment_sha256],
            ["workflow_manifest_sha256", workflow_manifest_sha256],
        ],
    }


def ridgeway_model_run_cache_key(
    *,
    mode: str,
    raster_semantics: str,
    resistance_sha256: str,
    focal_sha256: str,
    included_pairs_sha256: str,
    pairs: object,
    settings: object,
    engine_manifest_sha256: str,
    engine_environment_sha256: str,
    workflow_manifest_sha256: str,
) -> str:
    """Hash a complete cache identity; keyword validation is fail-closed."""

    return canonical_sha256(
        ridgeway_model_run_cache_payload(
            mode=mode,
            raster_semantics=raster_semantics,
            resistance_sha256=resistance_sha256,
            focal_sha256=focal_sha256,
            included_pairs_sha256=included_pairs_sha256,
            pairs=pairs,
            settings=settings,
            engine_manifest_sha256=engine_manifest_sha256,
            engine_environment_sha256=engine_environment_sha256,
            workflow_manifest_sha256=workflow_manifest_sha256,
        )
    )
