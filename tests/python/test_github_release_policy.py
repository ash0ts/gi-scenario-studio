from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "validate_github_release_policy.py"
    spec = importlib.util.spec_from_file_location("github_release_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _immutable() -> dict[str, object]:
    return {"enabled": True, "enforced_by_owner": False}


def _ruleset() -> dict[str, object]:
    return {
        "id": 9182,
        "name": "Protect release tags",
        "target": "tag",
        "source_type": "Repository",
        "source": "ash0ts/gi-scenario-studio",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": ["~ALL"],
                "exclude": [],
            }
        },
        "rules": [
            {"type": "update"},
            {"type": "deletion"},
            {"type": "required_signatures"},
        ],
    }


def test_release_policy_requires_exact_active_all_tag_protection() -> None:
    policy = _load()

    result = policy.validate_release_policy(
        _immutable(),
        _ruleset(),
        expected_ruleset_id=9182,
    )

    assert result["immutable_releases"] == {
        "enabled": True,
        "enforced_by_owner": False,
    }
    assert result["tag_ruleset"]["id"] == 9182
    assert result["tag_ruleset"]["ref_name"] == {
        "include": ["~ALL"],
        "exclude": [],
    }
    assert result["tag_ruleset"]["rule_types"] == [
        "deletion",
        "required_signatures",
        "update",
    ]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("enabled", False, "not enabled"),
        ("enabled", 1, "not enabled"),
        ("enforced_by_owner", "false", "must be boolean"),
    ],
)
def test_release_policy_rejects_invalid_immutable_release_response(
    field: str,
    value: object,
    message: str,
) -> None:
    policy = _load()
    immutable = _immutable()
    immutable[field] = value

    with pytest.raises((TypeError, ValueError), match=message):
        policy.validate_release_policy(
            immutable,
            _ruleset(),
            expected_ruleset_id=9182,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("id", 9183), "ID does not match"),
        (("id", True), "ID does not match"),
        (("target", "branch"), "must target tags"),
        (("enforcement", "evaluate"), "not actively enforced"),
        (("bypass_actors", [{}]), "must not allow bypass actors"),
        (("bypass_actors", None), "must not allow bypass actors"),
    ],
)
def test_release_policy_rejects_wrong_ruleset_identity_or_enforcement(
    mutation: tuple[str, object],
    message: str,
) -> None:
    policy = _load()
    ruleset = _ruleset()
    ruleset[mutation[0]] = mutation[1]

    with pytest.raises(ValueError, match=message):
        policy.validate_release_policy(
            _immutable(),
            ruleset,
            expected_ruleset_id=9182,
        )


@pytest.mark.parametrize(
    ("include", "exclude"),
    [
        (["refs/tags/v*"], []),
        (["~ALL", "refs/tags/v*"], []),
        (["~ALL"], ["refs/tags/test*"]),
        ("~ALL", []),
    ],
)
def test_release_policy_requires_exact_all_tag_scope(
    include: object,
    exclude: object,
) -> None:
    policy = _load()
    ruleset = _ruleset()
    conditions = copy.deepcopy(ruleset["conditions"])
    conditions["ref_name"] = {"include": include, "exclude": exclude}
    ruleset["conditions"] = conditions

    with pytest.raises(ValueError, match="cover every tag"):
        policy.validate_release_policy(
            _immutable(),
            ruleset,
            expected_ruleset_id=9182,
        )


@pytest.mark.parametrize(
    "rules",
    [
        [{"type": "update"}],
        [{"type": "deletion"}],
        [{"type": "update"}, {"type": "update"}, {"type": "deletion"}],
        [],
    ],
)
def test_release_policy_requires_one_update_and_one_deletion_rule(
    rules: list[dict[str, object]],
) -> None:
    policy = _load()
    ruleset = _ruleset()
    ruleset["rules"] = rules

    with pytest.raises(ValueError, match="restrict both tag update and deletion|contain rules"):
        policy.validate_release_policy(
            _immutable(),
            ruleset,
            expected_ruleset_id=9182,
        )


def test_policy_loader_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    policy = _load()
    response = tmp_path / "immutable.json"
    response.write_text(
        '{"enabled":true,"enabled":true,"enforced_by_owner":false}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON field"):
        policy.load_json(response)


def test_cli_writes_a_normalized_policy_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = _load()
    immutable = tmp_path / "immutable.json"
    ruleset = tmp_path / "ruleset.json"
    output = tmp_path / "validated.json"
    immutable.write_text(json.dumps(_immutable()), encoding="utf-8")
    ruleset.write_text(json.dumps(_ruleset()), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_github_release_policy.py",
            "--immutable-releases-json",
            str(immutable),
            "--ruleset-json",
            str(ruleset),
            "--expected-ruleset-id",
            "9182",
            "--output",
            str(output),
        ],
    )

    assert policy.main() == 0
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["schema_version"] == "ecocorridor-github-release-policy/v1.0"
    assert record["tag_ruleset"]["id"] == 9182
