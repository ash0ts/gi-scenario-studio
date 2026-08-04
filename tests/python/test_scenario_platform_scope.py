from __future__ import annotations

import pytest
from scenario_platform.observability import (
    ProjectScope,
    ProjectScopeEvidence,
    ProjectScopeResolver,
    ScopeEvidenceKind,
    UnsupportedWandbCloudScopeProvider,
)


class _DocumentedProvider:
    def __init__(self, kind: ScopeEvidenceKind) -> None:
        self.kind = kind

    def resolve_exact_scope(self, *, entity: str, project: str, deployment: str, api_base_url: str):
        return ProjectScopeEvidence(
            entity=entity,
            project=project,
            external_project_id="project-immutable-id",
            deployment=deployment,
            api_base_url=api_base_url,
            scope=ProjectScope.RESTRICTED,
            evidence_kind=self.kind,
            provider_field="visibility_scope",
        )


def test_current_wandb_cloud_provider_fails_closed() -> None:
    resolver = ProjectScopeResolver(UnsupportedWandbCloudScopeProvider())
    with pytest.raises(RuntimeError, match="WANDB_RESTRICTED_SCOPE_UNVERIFIABLE"):
        resolver.resolve(
            entity="team",
            project="project",
            deployment="wandb_cloud",
            api_base_url="https://api.wandb.ai",
        )


def test_cloud_rejects_admin_or_user_declared_substitute() -> None:
    resolver = ProjectScopeResolver(
        _DocumentedProvider(ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT)
    )
    with pytest.raises(RuntimeError, match="WANDB_RESTRICTED_SCOPE_UNVERIFIABLE"):
        resolver.resolve(
            entity="team",
            project="project",
            deployment="wandb_cloud",
            api_base_url="https://api.wandb.ai",
        )


def test_dedicated_accepts_documented_admin_exact_scope() -> None:
    resolver = ProjectScopeResolver(
        _DocumentedProvider(ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT)
    )
    evidence = resolver.resolve(
        entity="team",
        project="project",
        deployment="dedicated_cloud",
        api_base_url="https://wandb.example/api",
    )
    assert evidence.scope == ProjectScope.RESTRICTED
    assert evidence.external_project_id == "project-immutable-id"


def test_scope_evidence_cannot_be_replayed_against_another_api_endpoint() -> None:
    class WrongEndpointProvider(_DocumentedProvider):
        def resolve_exact_scope(self, **request: str) -> ProjectScopeEvidence:
            evidence = super().resolve_exact_scope(**request)
            return evidence.model_copy(update={"api_base_url": "https://other.wandb.example"})

    resolver = ProjectScopeResolver(
        WrongEndpointProvider(ScopeEvidenceKind.ADMIN_API_DOCUMENTED_EXACT)
    )
    with pytest.raises(RuntimeError, match="WANDB_PROJECT_ENDPOINT_MISMATCH"):
        resolver.resolve(
            entity="team",
            project="project",
            deployment="dedicated",
            api_base_url="https://api.wandb.example",
        )
