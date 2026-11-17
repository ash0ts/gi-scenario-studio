"""Validate saved GitHub immutable-release and tag-ruleset API responses."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

MAX_POLICY_BYTES = 1024 * 1024


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = nested
    return value


def load_json(path: Path) -> object:
    """Load one bounded regular JSON file without accepting duplicate fields."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("GitHub policy response must be a regular file")
    size = path.stat().st_size
    if not 1 <= size <= MAX_POLICY_BYTES:
        raise ValueError("GitHub policy response exceeds its byte boundary")
    try:
        text = path.read_bytes().decode("utf-8")
        return json.loads(text, object_pairs_hook=_pairs_without_duplicates)
    except UnicodeDecodeError as error:
        raise ValueError("GitHub policy response must be UTF-8 JSON") from error


def validate_release_policy(
    immutable_releases_payload: object,
    ruleset_payload: object,
    *,
    expected_ruleset_id: int,
) -> dict[str, Any]:
    """Fail closed unless immutable releases and exact tag protections are active."""

    if type(expected_ruleset_id) is not int or expected_ruleset_id <= 0:
        raise ValueError("expected ruleset ID must be a positive integer")

    immutable = _object(immutable_releases_payload, label="immutable releases response")
    if set(immutable) != {"enabled", "enforced_by_owner"}:
        raise ValueError("immutable releases response has an unexpected schema")
    if immutable.get("enabled") is not True:
        raise ValueError("immutable releases are not enabled")
    if type(immutable.get("enforced_by_owner")) is not bool:
        raise TypeError("immutable releases owner-enforcement field must be boolean")

    ruleset = _object(ruleset_payload, label="repository ruleset response")
    ruleset_id = ruleset.get("id")
    if type(ruleset_id) is not int or ruleset_id != expected_ruleset_id:
        raise ValueError("repository ruleset ID does not match the configured policy")
    if ruleset.get("target") != "tag":
        raise ValueError("release-protection ruleset must target tags")
    if ruleset.get("enforcement") != "active":
        raise ValueError("release-protection ruleset is not actively enforced")

    bypass_actors = ruleset.get("bypass_actors")
    if not isinstance(bypass_actors, list) or bypass_actors:
        raise ValueError("release-protection ruleset must not allow bypass actors")

    conditions = _object(ruleset.get("conditions"), label="ruleset conditions")
    ref_name = _object(conditions.get("ref_name"), label="ruleset ref-name condition")
    if set(ref_name) != {"include", "exclude"}:
        raise ValueError("ruleset ref-name condition has an unexpected schema")
    include = ref_name.get("include")
    exclude = ref_name.get("exclude")
    if include != ["~ALL"] or exclude != []:
        raise ValueError("release-protection ruleset must cover every tag without exclusions")

    rules = ruleset.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("release-protection ruleset must contain rules")
    rule_types: list[str] = []
    for index, rule_value in enumerate(rules):
        rule = _object(rule_value, label=f"ruleset rule {index}")
        rule_type = rule.get("type")
        if not isinstance(rule_type, str) or not rule_type:
            raise TypeError("every release-protection rule must have a string type")
        rule_types.append(rule_type)
    if rule_types.count("update") != 1 or rule_types.count("deletion") != 1:
        raise ValueError("release-protection ruleset must restrict both tag update and deletion")

    return {
        "schema_version": "ecocorridor-github-release-policy/v1.0",
        "immutable_releases": {
            "enabled": True,
            "enforced_by_owner": immutable["enforced_by_owner"],
        },
        "tag_ruleset": {
            "id": ruleset_id,
            "target": "tag",
            "enforcement": "active",
            "bypass_actor_count": 0,
            "ref_name": {"include": ["~ALL"], "exclude": []},
            "rule_types": sorted(rule_types),
        },
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ValueError("release-policy output may not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--immutable-releases-json", required=True, type=Path)
    parser.add_argument("--ruleset-json", required=True, type=Path)
    parser.add_argument("--expected-ruleset-id", required=True, type=int)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    result = validate_release_policy(
        load_json(arguments.immutable_releases_json),
        load_json(arguments.ruleset_json),
        expected_ruleset_id=arguments.expected_ruleset_id,
    )
    if arguments.output is not None:
        _write_json_atomic(arguments.output, result)
    print("GitHub immutable release and tag ruleset policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
