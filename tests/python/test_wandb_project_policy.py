from __future__ import annotations

import types

import pytest
from gi_scenario_core.observability import _resolve_exact_project_policy


class Service:
    def __init__(self, record):
        self.record = record
        self.queries: list[tuple[str, dict[str, str]]] = []

    def execute_graphql(self, query: str, variables: dict[str, str]):
        self.queries.append((query, variables))
        return {"project": self.record}


class Api:
    def __init__(self, *, access: str, record=None, service: bool = True):
        self.selected = types.SimpleNamespace(id="provider-project-id", access=access)
        if service:
            self._service_api = Service(record)

    def project(self, name: str, *, entity: str):
        assert name == "evidence-project"
        assert entity == "stormwater-team"
        return self.selected


def test_exact_policy_resolver_does_not_treat_private_as_restricted() -> None:
    api = Api(
        access="PRIVATE",
        record={
            "id": "provider-project-id",
            "name": "evidence-project",
            "entityName": "stormwater-team",
            "access": "RESTRICTED",
        },
    )
    policy = _resolve_exact_project_policy(api, "stormwater-team", "evidence-project")
    assert policy == {
        "id": "provider-project-id",
        "name": "evidence-project",
        "entityName": "stormwater-team",
        "access": "RESTRICTED",
    }
    assert len(api._service_api.queries) == 1


def test_exact_policy_resolver_prefers_supported_restricted_field() -> None:
    api = Api(access="RESTRICTED", service=False)
    assert _resolve_exact_project_policy(
        api, "stormwater-team", "evidence-project"
    )["access"] == "RESTRICTED"


def test_exact_policy_resolver_fails_closed_without_exact_field() -> None:
    api = Api(access="PRIVATE", service=False)
    with pytest.raises(ValueError, match="WANDB_RESTRICTED_SCOPE_UNVERIFIABLE"):
        _resolve_exact_project_policy(api, "stormwater-team", "evidence-project")


def test_exact_policy_resolver_rejects_identity_drift() -> None:
    api = Api(
        access="PRIVATE",
        record={
            "id": "different-id",
            "name": "evidence-project",
            "entityName": "stormwater-team",
            "access": "RESTRICTED",
        },
    )
    with pytest.raises(RuntimeError, match="WANDB_PROJECT_ID_CHANGED"):
        _resolve_exact_project_policy(api, "stormwater-team", "evidence-project")
