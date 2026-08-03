from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from gi_scenario_core.contracts import ObservabilityBinding, ProjectDataPolicy
from gi_scenario_core.repository import ProjectRepository

VERIFIED_AT = datetime(2026, 8, 3, tzinfo=UTC)


def _policy(
    project_id: str,
    *,
    policy_id: str,
) -> ProjectDataPolicy:
    return ProjectDataPolicy(
        policy_id=policy_id,
        project_id=project_id,
        classification="standard_engineering",
        deployment="wandb_cloud",
        acknowledged_by="engineer@example.test",
        acknowledged_at=VERIFIED_AT,
    )


def _binding(
    project_id: str,
    *,
    policy_id: str,
    binding_id: str,
    external_project: str,
    external_project_id: str,
) -> ObservabilityBinding:
    return ObservabilityBinding(
        binding_id=binding_id,
        project_id=project_id,
        data_policy_id=policy_id,
        entity="stormwater-team",
        external_project=external_project,
        external_project_id=external_project_id,
        deployment="wandb_cloud",
        verified=True,
        verified_at=VERIFIED_AT,
        updated_at=VERIFIED_AT,
    )


def _create_bound(
    repository: ProjectRepository,
    project_id: str,
    *,
    policy_id: str,
    binding_id: str,
    external_project: str,
    external_project_id: str,
) -> dict[str, dict[str, object]]:
    return repository.create_project_with_observability(
        title=f"Project {project_id}",
        project_kind="existing",
        project_id=project_id,
        state={"onboarding": "complete"},
        policy=_policy(project_id, policy_id=policy_id),
        binding=_binding(
            project_id,
            policy_id=policy_id,
            binding_id=binding_id,
            external_project=external_project,
            external_project_id=external_project_id,
        ),
    )


def test_create_project_with_observability_commits_all_three_records(tmp_path) -> None:
    repository = ProjectRepository(tmp_path / "project")
    try:
        created = _create_bound(
            repository,
            "project_1",
            policy_id="policy_1",
            binding_id="binding_1",
            external_project="gi-scenario-project-1",
            external_project_id="wandb-project-1",
        )

        assert created["project"]["project_id"] == "project_1"
        assert created["project"]["project_kind"] == "existing"
        assert created["project"]["state"] == {"onboarding": "complete"}
        assert created["policy"]["policy_id"] == "policy_1"
        assert created["binding"]["binding_id"] == "binding_1"
        assert created["binding"]["access"] == "Restricted"
        assert created["binding"]["verified"] is True
        counts = repository.table_counts()
        assert counts["projects"] == 1
        assert counts["project_data_policies"] == 1
        assert counts["observability_bindings"] == 1
    finally:
        repository.close()


def test_atomic_project_creation_rolls_back_when_policy_insert_fails(tmp_path) -> None:
    repository = ProjectRepository(tmp_path / "project")
    try:
        _create_bound(
            repository,
            "project_1",
            policy_id="policy_shared",
            binding_id="binding_1",
            external_project="gi-scenario-project-1",
            external_project_id="wandb-project-1",
        )

        with pytest.raises(sqlite3.IntegrityError):
            _create_bound(
                repository,
                "project_2",
                policy_id="policy_shared",
                binding_id="binding_2",
                external_project="gi-scenario-project-2",
                external_project_id="wandb-project-2",
            )

        with pytest.raises(KeyError):
            repository.get_project("project_2")
        with pytest.raises(KeyError):
            repository.get_project_data_policy("project_2")
        assert repository.table_counts()["projects"] == 1
        assert repository.table_counts()["project_data_policies"] == 1
        assert repository.table_counts()["observability_bindings"] == 1
    finally:
        repository.close()


def test_atomic_project_creation_rolls_back_when_binding_insert_fails(tmp_path) -> None:
    repository = ProjectRepository(tmp_path / "project")
    try:
        _create_bound(
            repository,
            "project_1",
            policy_id="policy_1",
            binding_id="binding_1",
            external_project="gi-scenario-project-1",
            external_project_id="wandb-destination-shared",
        )

        with pytest.raises(sqlite3.IntegrityError):
            _create_bound(
                repository,
                "project_2",
                policy_id="policy_2",
                binding_id="binding_2",
                external_project="gi-scenario-project-1",
                external_project_id="wandb-destination-shared",
            )

        with pytest.raises(KeyError):
            repository.get_project("project_2")
        with pytest.raises(KeyError):
            repository.get_project_data_policy("project_2")
        assert repository.table_counts()["projects"] == 1
        assert repository.table_counts()["project_data_policies"] == 1
        assert repository.table_counts()["observability_bindings"] == 1
        assert repository.get_observability_binding("project_1")["binding_id"] == "binding_1"
    finally:
        repository.close()
