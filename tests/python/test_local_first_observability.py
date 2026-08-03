from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from gi_scenario_core.contracts import ModelFitnessPassport, ModelRevision
from gi_scenario_core.repository import MIGRATION_1, MIGRATION_2, ProjectRepository
from pydantic import ValidationError


def _seed_bound_v2_database(root) -> None:
    root.mkdir(parents=True)
    database = sqlite3.connect(root / "project.sqlite", isolation_level=None)
    now = datetime(2026, 8, 3, tzinfo=UTC).isoformat()
    try:
        database.execute("PRAGMA foreign_keys=ON")
        database.executescript(MIGRATION_1)
        database.execute("PRAGMA user_version=1")
        database.executescript(MIGRATION_2)
        database.execute("PRAGMA user_version=2")
        database.execute(
            "INSERT INTO projects(project_id,title,project_kind,state_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("project_v2", "Existing v2 project", "existing", "{}", now, now),
        )
        database.execute(
            "INSERT INTO project_data_policies(policy_id,project_id,payload_json,acknowledged_at) "
            "VALUES(?,?,?,?)",
            ("policy_v2", "project_v2", "{}", now),
        )
        database.execute(
            "INSERT INTO observability_bindings("
            "binding_id,project_id,data_policy_id,entity,external_project,external_project_id,"
            "access,deployment,mode,verified,first_upload_at,previous_binding_id,verified_at,"
            "active,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "binding_v2",
                "project_v2",
                "policy_v2",
                "stormwater-team",
                "v2-project",
                "provider-project-v2",
                "Restricted",
                "wandb_cloud",
                "online",
                1,
                None,
                None,
                now,
                1,
                "{}",
                now,
                now,
            ),
        )
        parent = {
            "revision_id": "revision_parent",
            "project_id": "project_v2",
            "parent_revision_id": None,
            "reason": "import",
            "model_sha256": "a" * 64,
            "dependency_hashes": {},
            "semantic_diff": None,
            "observability_binding_id": "binding_v2",
            "created_by": "v2 fixture",
            "created_at": now,
        }
        child = {
            **parent,
            "revision_id": "revision_child",
            "parent_revision_id": "revision_parent",
            "reason": "manual_edit",
            "model_sha256": "b" * 64,
        }
        for revision in (parent, child):
            database.execute(
                "INSERT INTO model_revisions(revision_id,project_id,parent_revision_id,"
                "observability_binding_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    revision["revision_id"],
                    revision["project_id"],
                    revision["parent_revision_id"],
                    revision["observability_binding_id"],
                    json.dumps(revision),
                    now,
                ),
            )
        database.execute(
            "INSERT INTO model_fitness_passports(passport_id,project_id,model_revision_id,"
            "payload_json,issued_at) VALUES(?,?,?,?,?)",
            ("passport_v2", "project_v2", "revision_child", "{}", now),
        )
    finally:
        database.close()


def test_v2_bound_revisions_migrate_and_local_revisions_persist(tmp_path) -> None:
    root = tmp_path / "project"
    _seed_bound_v2_database(root)

    repository = ProjectRepository(root)
    try:
        assert repository._connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert repository._connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert repository.get_model_revision("revision_child")["parent_revision_id"] == (
            "revision_parent"
        )
        assert repository.get_model_fitness_passport_by_id("passport_v2") == {}

        repository.create_project(
            title="Local-only project",
            project_kind="existing",
            project_id="project_local",
        )
        revision = ModelRevision(
            project_id="project_local",
            reason="import",
            model_sha256="c" * 64,
            created_by="local import",
        )
        passport = ModelFitnessPassport(
            project_id="project_local",
            model_revision_id=revision.revision_id,
            intended_use="Local GI alternatives screening",
        )
        state = {
            "current_model_revision_id": revision.revision_id,
            "current_model_sha256": revision.model_sha256,
            "model_fitness_passport_id": passport.passport_id,
        }
        repository.store_imported_model_record(revision, passport, state)

        stored = repository.get_model_revision(revision.revision_id)
        assert stored["observability_binding_id"] is None
        assert repository.get_model_fitness_passport("project_local")["passport_id"] == (
            passport.passport_id
        )
        assert repository.get_project("project_local")["state"] == state
        assert repository._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        repository.close()


def test_rebind_revision_still_requires_a_cloud_binding() -> None:
    with pytest.raises(ValidationError, match="rebind revision must identify"):
        ModelRevision(
            project_id="project_local",
            reason="rebind",
            model_sha256="d" * 64,
            created_by="test",
        )
