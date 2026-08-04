"""Deterministic JSON Schema emission for the public v1.0 contracts."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import ECOCORRIDOR_SCHEMA_VERSION, PUBLIC_CONTRACTS, public_schema_index


def render_public_schemas() -> dict[str, str]:
    rendered: dict[str, str] = {}
    for contract in PUBLIC_CONTRACTS:
        schema = contract.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = (
            f"https://schemas.ecocorridor.local/{ECOCORRIDOR_SCHEMA_VERSION}/"
            f"{contract.__name__}.schema.json"
        )
        rendered[f"{contract.__name__}.schema.json"] = (
            json.dumps(schema, sort_keys=True, indent=2) + "\n"
        )
    rendered["index.json"] = json.dumps(public_schema_index(), sort_keys=True, indent=2) + "\n"
    return rendered


def write_public_schemas(output_directory: Path) -> list[Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    rendered = render_public_schemas()
    expected = set(rendered)
    # A removed or renamed contract must not leave a stale schema that appears
    # to remain supported.  Restrict pruning to this version directory and the
    # generator-owned JSON files only.
    for existing in output_directory.glob("*.json"):
        if existing.name not in expected:
            existing.unlink()
    written = []
    for filename, content in rendered.items():
        path = output_directory / filename
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
        written.append(path)
    return sorted(written)
