"""App-scoped observability names and payload minimization primitives."""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from pathlib import PurePath
from typing import Any, Protocol
from urllib.parse import urlsplit

from .contracts import ContractModel, Field, canonical_json

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_FILE_URI = re.compile(r"\bfile:(?://)?/[^\s\"']+", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(
    r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\|(?<![<:/A-Za-z0-9])/(?!/))[^\s\"']+"
)
_INLINE_SECRET = re.compile(
    r"(?i)\b(?:api[ _-]?key|authorization|cookie|credential|password|private[ _-]?key|"
    r"secret|token)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}")
_DIRECT_IDENTIFIER_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"),
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
)
_DEFAULT_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "environment",
        "localpath",
        "password",
        "privatekey",
        "secret",
        "stderr",
        "stdout",
        "token",
    }
)
_CONTAINS_SENSITIVE = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "environment",
        "localpath",
        "password",
        "privatekey",
        "secret",
    }
)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _normalized_api_base_url(value: str) -> str:
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
        raise ValueError(
            "scope-evidence API base URL must be credential-free HTTPS or loopback HTTP"
        )
    return normalized


def contains_local_absolute_path(value: str) -> bool:
    """Return whether text discloses a file URI or absolute local path.

    This intentionally shares the same broad matcher used by cloud-metadata
    redaction.  Callers that publish immutable evidence must reject these
    values rather than silently rewriting scientific artifacts.
    """

    return bool(_FILE_URI.search(value) or _ABSOLUTE_PATH.search(value))


class AppProfile(ContractModel):
    namespace: str
    api_version: str
    schema_version: str
    run_kinds: list[str] = Field(min_length=1)
    artifact_types: list[str] = Field(min_length=1)
    registry_names: dict[str, str] = Field(default_factory=dict)
    keychain_service: str
    app_data_root: str

    def _validate_contract(self) -> None:
        if not _NAME.fullmatch(self.namespace):
            raise ValueError("namespace must be a lowercase observability name")
        for label, values in (
            ("run kind", self.run_kinds),
            ("artifact type", self.artifact_types),
        ):
            if len(set(values)) != len(values) or any(
                not _NAME.fullmatch(value) for value in values
            ):
                raise ValueError(f"{label} values must be unique lowercase names")
        if any(not _NAME.fullmatch(key) for key in self.registry_names):
            raise ValueError("registry aliases must be lowercase names")
        if not self.api_version or not self.schema_version:
            raise ValueError("API and schema versions are required")
        if not self.keychain_service or not self.app_data_root:
            raise ValueError("keychain service and app-data root are required")

    def run_id(self, run_kind: str, subject_id: str) -> str:
        if run_kind not in self.run_kinds:
            raise ValueError(f"unsupported run kind for {self.namespace}: {run_kind}")
        digest = hashlib.sha256(
            canonical_json([self.namespace, self.schema_version, run_kind, subject_id]).encode()
        ).hexdigest()[:20]
        return f"{self.namespace}-{run_kind}-{digest}"

    def table_name(self, logical_name: str) -> str:
        if not _NAME.fullmatch(logical_name):
            raise ValueError("table logical name is invalid")
        return f"{self.namespace}_{logical_name}"

    def artifact_name(self, artifact_type: str, logical_id: str) -> str:
        if artifact_type not in self.artifact_types:
            raise ValueError(f"unsupported artifact type for {self.namespace}: {artifact_type}")
        digest = hashlib.sha256(logical_id.encode("utf-8")).hexdigest()[:16]
        return f"{self.namespace}-{artifact_type}-{digest}"

    def registry_name(self, alias: str) -> str:
        try:
            return self.registry_names[alias]
        except KeyError as exc:
            raise ValueError(f"unknown registry alias: {alias}") from exc


def redact_sensitive(
    value: Any,
    *,
    sensitive_keys: set[str] | frozenset[str] = _DEFAULT_SENSITIVE_KEYS,
    redact_local_paths: bool = True,
) -> Any:
    """Return a recursively redacted copy suitable for cloud metadata."""

    normalized_sensitive = {_normalized_key(key) for key in sensitive_keys}
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            rendered = str(key)
            normalized = _normalized_key(rendered)
            if any(
                normalized == secret or normalized.endswith(secret)
                for secret in normalized_sensitive
            ) or any(secret in normalized for secret in _CONTAINS_SENSITIVE):
                result[rendered] = "[REDACTED]"
            else:
                result[rendered] = redact_sensitive(
                    item,
                    sensitive_keys=sensitive_keys,
                    redact_local_paths=redact_local_paths,
                )
        return result
    if isinstance(value, (list, tuple)):
        return [
            redact_sensitive(
                item,
                sensitive_keys=sensitive_keys,
                redact_local_paths=redact_local_paths,
            )
            for item in value
        ]
    if isinstance(value, PurePath):
        return "[LOCAL_PATH]"
    if isinstance(value, str):
        redacted = _INLINE_SECRET.sub("[REDACTED]", value)
        redacted = _BEARER_SECRET.sub("[REDACTED]", redacted)
        if redact_local_paths:
            redacted = _FILE_URI.sub("[LOCAL_PATH]", redacted)
            redacted = _ABSOLUTE_PATH.sub("[LOCAL_PATH]", redacted)
        return redacted
    return value


def contains_direct_identifier(value: str) -> bool:
    """Return whether text contains a supported direct personal identifier.

    This deliberately bounded, dependency-free detector covers email addresses,
    North American telephone numbers, and U.S. Social Security numbers. Location
    names and coordinates remain domain-policy inputs because silently guessing
    whether scientific text is a place would be unsafe.
    """

    return any(pattern.search(value) for pattern in _DIRECT_IDENTIFIER_PATTERNS)


def _length_preserving_mask(value: str, marker: str) -> str:
    """Mask one string without invalidating reviewed character spans."""

    length = len(value)
    if length < len(marker):
        return "X" * length
    return marker + "_" * (length - len(marker))


def _length_preserving_match_mask(marker: str) -> Any:
    return lambda match: _length_preserving_mask(match.group(0), marker)


def redact_external_identifiers(value: Any) -> Any:
    """Minimize a payload immediately before an external provider boundary.

    Sensitive keys, inline credentials, bearer tokens, local paths, and
    supported direct identifiers are replaced locally and deterministically.
    Every string replacement preserves its Python character length so reviewed
    intake spans remain valid. The transformation is recursive, idempotent, and
    requires no model or network access.
    """

    normalized_sensitive = {_normalized_key(key) for key in _DEFAULT_SENSITIVE_KEYS}

    def redact_string(item: str) -> str:
        result = _INLINE_SECRET.sub(_length_preserving_match_mask("[REDACTED]"), item)
        result = _BEARER_SECRET.sub(_length_preserving_match_mask("[REDACTED]"), result)
        result = _FILE_URI.sub(_length_preserving_match_mask("[LOCAL_PATH]"), result)
        result = _ABSOLUTE_PATH.sub(_length_preserving_match_mask("[LOCAL_PATH]"), result)
        for pattern in _DIRECT_IDENTIFIER_PATTERNS:
            result = pattern.sub(_length_preserving_match_mask("[REDACTED_PII]"), result)
        return result

    def redact_external(item: Any) -> Any:
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for key, nested in item.items():
                rendered = str(key)
                normalized = _normalized_key(rendered)
                if any(
                    normalized == secret or normalized.endswith(secret)
                    for secret in normalized_sensitive
                ) or any(secret in normalized for secret in _CONTAINS_SENSITIVE):
                    result[rendered] = (
                        _length_preserving_mask(nested, "[REDACTED]")
                        if isinstance(nested, str)
                        else "[REDACTED]"
                    )
                else:
                    result[rendered] = redact_external(nested)
            return result
        if isinstance(item, (list, tuple)):
            return [redact_external(nested) for nested in item]
        if isinstance(item, PurePath):
            return _length_preserving_mask(str(item), "[LOCAL_PATH]")
        if isinstance(item, str):
            return redact_string(item)
        return item

    return redact_external(value)


class ProjectScope(str, Enum):
    RESTRICTED = "RESTRICTED"
    UNVERIFIABLE = "UNVERIFIABLE"


class ScopeEvidenceKind(str, Enum):
    PUBLIC_API_DOCUMENTED_EXACT = "PUBLIC_API_DOCUMENTED_EXACT"
    ADMIN_API_DOCUMENTED_EXACT = "ADMIN_API_DOCUMENTED_EXACT"


class ProjectScopeEvidence(ContractModel):
    entity: str
    project: str
    external_project_id: str
    deployment: str
    api_base_url: str
    scope: ProjectScope
    evidence_kind: ScopeEvidenceKind
    provider_field: str

    def _validate_contract(self) -> None:
        if not self.entity or not self.project or not self.external_project_id:
            raise ValueError("project-scope evidence requires an immutable project identity")
        if self.scope != ProjectScope.RESTRICTED:
            raise ValueError("only exact Restricted scope is acceptable evidence")
        if _normalized_api_base_url(self.api_base_url) != self.api_base_url:
            raise ValueError("project-scope evidence API base URL must be normalized")
        if not self.provider_field or any(character.isspace() for character in self.provider_field):
            raise ValueError("project-scope evidence requires a documented provider field")


class ProjectScopeProvider(Protocol):
    """Server-side provider boundary; renderer declarations are never accepted."""

    def resolve_exact_scope(
        self,
        *,
        entity: str,
        project: str,
        deployment: str,
        api_base_url: str,
    ) -> ProjectScopeEvidence: ...


class ProjectScopeResolver:
    """Fail closed unless a documented provider endpoint returns exact scope."""

    def __init__(self, provider: ProjectScopeProvider | None = None) -> None:
        self.provider = provider

    def resolve(
        self,
        *,
        entity: str,
        project: str,
        deployment: str,
        api_base_url: str,
    ) -> ProjectScopeEvidence:
        if self.provider is None:
            raise RuntimeError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE")
        normalized_api_base_url = _normalized_api_base_url(api_base_url)
        evidence = ProjectScopeEvidence.model_validate(
            self.provider.resolve_exact_scope(
                entity=entity,
                project=project,
                deployment=deployment,
                api_base_url=normalized_api_base_url,
            )
        )
        if evidence.entity != entity or evidence.project != project:
            raise RuntimeError("WANDB_PROJECT_IDENTITY_MISMATCH")
        normalized_deployment = deployment.casefold().replace("-", "_")
        if evidence.deployment.casefold().replace("-", "_") != normalized_deployment:
            raise RuntimeError("WANDB_PROJECT_DEPLOYMENT_MISMATCH")
        if evidence.api_base_url != normalized_api_base_url:
            raise RuntimeError("WANDB_PROJECT_ENDPOINT_MISMATCH")
        if normalized_deployment in {"wandb_cloud", "cloud"}:
            if evidence.evidence_kind != ScopeEvidenceKind.PUBLIC_API_DOCUMENTED_EXACT:
                raise RuntimeError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE")
        elif evidence.evidence_kind not in {
            ScopeEvidenceKind.PUBLIC_API_DOCUMENTED_EXACT,
            ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT,
        }:
            raise RuntimeError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE")
        return evidence


class UnsupportedWandbCloudScopeProvider:
    """Current documented W&B Public API has no exact visibility field.

    This concrete default intentionally cannot be configured with a UI checkbox,
    legacy `PRIVATE` value, membership result, or successful upload probe.
    """

    def resolve_exact_scope(
        self,
        *,
        entity: str,
        project: str,
        deployment: str,
        api_base_url: str,
    ) -> ProjectScopeEvidence:
        del entity, project, deployment, api_base_url
        raise RuntimeError("WANDB_RESTRICTED_SCOPE_UNVERIFIABLE")
