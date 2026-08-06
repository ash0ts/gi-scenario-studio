"""Deployment-bound W&B endpoint validation for EcoCorridor.

Multi-tenant W&B Cloud has one authoritative API origin and one authoritative
web origin.  Accepting a caller-supplied lookalike host while labelling the
deployment ``WANDB_CLOUD`` would send credentials and evidence to the wrong
service.  Dedicated and Self-Managed deployments retain their explicit custom
endpoint support.
"""

from __future__ import annotations

from urllib.parse import urlsplit

WANDB_CLOUD_API_ORIGIN = "https://api.wandb.ai"
WANDB_CLOUD_WEB_ORIGIN = "https://wandb.ai"

_SUPPORTED_DEPLOYMENTS = frozenset({"WANDB_CLOUD", "DEDICATED", "SELF_MANAGED"})


def _normalize_secure_endpoint(value: str, *, label: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not loopback)
    ):
        raise ValueError(f"{label} must be HTTPS or explicit loopback HTTP")
    return normalized


def validate_wandb_deployment_origins(
    *,
    deployment: str,
    api_base_url: str,
    web_base_url: str | None,
) -> tuple[str, str]:
    """Return normalized endpoints after enforcing the deployment identity.

    A missing web endpoint resolves to the official W&B web origin for Cloud
    and to the explicit API endpoint for Dedicated/Self-Managed, preserving the
    existing custom-deployment behavior.
    """

    normalized_deployment = deployment.strip().upper().replace("-", "_")
    if normalized_deployment not in _SUPPORTED_DEPLOYMENTS:
        raise ValueError("unsupported W&B deployment")
    api_origin = _normalize_secure_endpoint(api_base_url, label="W&B API base URL")
    resolved_web = (
        WANDB_CLOUD_WEB_ORIGIN
        if web_base_url is None and normalized_deployment == "WANDB_CLOUD"
        else web_base_url or api_origin
    )
    web_origin = _normalize_secure_endpoint(resolved_web, label="W&B web base URL")
    if normalized_deployment == "WANDB_CLOUD" and (
        api_origin != WANDB_CLOUD_API_ORIGIN or web_origin != WANDB_CLOUD_WEB_ORIGIN
    ):
        raise ValueError(
            "WANDB_CLOUD requires the official https://api.wandb.ai API origin "
            "and https://wandb.ai web origin"
        )
    return api_origin, web_origin


__all__ = [
    "WANDB_CLOUD_API_ORIGIN",
    "WANDB_CLOUD_WEB_ORIGIN",
    "validate_wandb_deployment_origins",
]
