#!/usr/bin/env python3
"""Generate EcoCorridor JSON Schema and matching strict Zod validators."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ecocorridor_core.contracts import PUBLIC_CONTRACTS
from ecocorridor_core.review_brief import (
    AuthorizedReviewSnapshot,
    ReviewBriefAgentOutput,
    ReviewBriefAttestation,
    ReviewBriefDraft,
    ReviewBriefItem,
    ReviewBriefPreflight,
    ReviewBriefRequest,
    ReviewBriefTraceReceipt,
    ReviewBriefValidation,
)
from ecocorridor_core.schema import (
    annotate_bundle_identity_schema,
    render_public_schemas,
    write_public_schemas,
)
from ecocorridor_core.usefulness_study import USEFULNESS_STUDY_CONTRACTS

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIRECTORY = ROOT / "schemas" / "ecocorridor" / "v1.0"
TYPESCRIPT_OUTPUT = ROOT / "apps" / "ecocorridor-desktop" / "src" / "generated" / "contracts.ts"

REVIEW_BRIEF_PUBLIC_CONTRACTS = (
    ("ReviewBriefRequest", ReviewBriefRequest),
    ("AuthorizedReviewSnapshot", AuthorizedReviewSnapshot),
    ("ReviewBriefAgentOutput", ReviewBriefAgentOutput),
    ("ReviewBriefDraft", ReviewBriefDraft),
    ("ReviewBriefItem", ReviewBriefItem),
    ("ReviewBriefPreflight", ReviewBriefPreflight),
    ("ReviewBriefTraceReceipt", ReviewBriefTraceReceipt),
    ("ReviewBriefValidation", ReviewBriefValidation),
    ("ReviewBriefAttestation", ReviewBriefAttestation),
)


def _named_contracts() -> tuple[tuple[str, type[Any]], ...]:
    return tuple((contract.__name__, contract) for contract in PUBLIC_CONTRACTS) + (
        REVIEW_BRIEF_PUBLIC_CONTRACTS
    ) + tuple((contract.__name__, contract) for contract in USEFULNESS_STUDY_CONTRACTS)


def _literal(value: Any) -> str:
    return f"z.literal({json.dumps(value, ensure_ascii=True)})"


def _zod(schema: dict[str, Any], definitions: dict[str, Any], stack: tuple[str, ...] = ()) -> str:
    if "$ref" in schema:
        reference = str(schema["$ref"])
        prefix = "#/$defs/"
        if not reference.startswith(prefix):
            raise ValueError(f"unsupported external schema reference: {reference}")
        name = reference.removeprefix(prefix)
        if name in stack:
            raise ValueError(
                f"cyclic schema reference is unsupported: {' -> '.join((*stack, name))}"
            )
        return _zod(definitions[name], definitions, (*stack, name))
    if "const" in schema:
        return _literal(schema["const"])
    if enum := schema.get("enum"):
        if all(isinstance(item, str) for item in enum):
            values = ", ".join(json.dumps(item) for item in enum)
            return f"z.enum([{values}])"
        return f"z.union([{', '.join(_literal(item) for item in enum)}])"
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if alternatives:
        rendered = [_zod(item, definitions, stack) for item in alternatives]
        if len(rendered) == 1:
            return rendered[0]
        return f"z.union([{', '.join(rendered)}])"
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return f"z.union([{', '.join(_zod({'type': item}, definitions, stack) for item in schema_type)}])"
    if schema_type == "null":
        return "z.null()"
    if schema_type == "boolean":
        return "z.boolean()"
    if schema_type in {"integer", "number"}:
        result = "z.number()"
        if schema_type == "integer":
            result += ".int()"
        for keyword, method in (
            ("minimum", "min"),
            ("exclusiveMinimum", "gt"),
            ("maximum", "max"),
            ("exclusiveMaximum", "lt"),
        ):
            if keyword in schema:
                result += f".{method}({schema[keyword]!r})"
        return result
    if schema_type == "string":
        result = "z.string()"
        if "minLength" in schema:
            result += f".min({int(schema['minLength'])})"
        if "maxLength" in schema:
            result += f".max({int(schema['maxLength'])})"
        if pattern := schema.get("pattern"):
            result += f".regex(new RegExp({json.dumps(pattern)}))"
        if schema.get("format") == "date-time":
            result += (
                ".refine((value) => !Number.isNaN(Date.parse(value)) "
                "&& /(?:Z|[+-]\\d{2}:\\d{2})$/.test(value), "
                '"Expected timezone-aware ISO date-time")'
            )
        return result
    if schema_type == "array":
        if prefix_items := schema.get("prefixItems"):
            rendered_items = ", ".join(_zod(item, definitions, stack) for item in prefix_items)
            result = f"z.tuple([{rendered_items}])"
            # JSON Schema 2020-12 permits additional tuple items. Pydantic's
            # fixed tuples explicitly cap maxItems, so only add a rest schema
            # when the source schema actually allows more positions.
            if schema.get("items") not in (None, False) and schema.get("maxItems") != len(
                prefix_items
            ):
                result += f".rest({_zod(schema['items'], definitions, stack)})"
            return result
        result = f"z.array({_zod(schema.get('items', {}), definitions, stack)})"
        if "minItems" in schema:
            result += f".min({int(schema['minItems'])})"
        if "maxItems" in schema:
            result += f".max({int(schema['maxItems'])})"
        if schema.get("uniqueItems") is True:
            result += (
                ".refine((items) => new Set(items.map((item) => "
                'JSON.stringify(item))).size === items.length, "Expected unique items")'
            )
        if unique_by := schema.get("x-uniqueBy"):
            if not isinstance(unique_by, str) or not unique_by:
                raise ValueError("x-uniqueBy must name one non-empty stable-ID property")
            encoded_property = json.dumps(unique_by)
            encoded_message = json.dumps(f"Expected unique {unique_by} values")
            result += (
                ".refine((items) => new Set(items.map((item) => "
                f"item[{encoded_property}])).size === items.length, {encoded_message})"
            )
        return result
    if schema_type == "object" or "properties" in schema or "additionalProperties" in schema:
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        if not properties:
            if isinstance(additional, dict):
                return f"z.record(z.string(), {_zod(additional, definitions, stack)})"
            return (
                "z.object({}).strict()"
                if additional is False
                else "z.record(z.string(), z.unknown())"
            )
        required = set(schema.get("required", []))
        fields = []
        for name, child in properties.items():
            rendered = _zod(child, definitions, stack)
            if name not in required:
                rendered += ".optional()"
            fields.append(f"  {json.dumps(name)}: {rendered},")
        result = "z.object({\n" + "\n".join(fields) + "\n})"
        if additional is False:
            result += ".strict()"
        elif isinstance(additional, dict):
            result += f".catchall({_zod(additional, definitions, stack)})"
        return result
    return "z.unknown()"


def render_typescript() -> str:
    lines = [
        "/* Generated by scripts/generate_ecocorridor_schemas.py. Do not edit. */",
        'import { z } from "zod";',
        "",
    ]
    names: list[str] = []
    for name, contract in _named_contracts():
        schema = contract.model_json_schema(mode="validation")
        if name == "ConnectivityProjectBundle":
            annotate_bundle_identity_schema(schema)
        names.append(name)
        validator = _zod(schema, schema.get("$defs", {}), (name,))
        lines.extend(
            [
                f"export const {name}Schema = {validator};",
                f"export type {name} = z.infer<typeof {name}Schema>;",
                "",
            ]
        )
    lines.append("export const EcoCorridorContractSchemas = {")
    lines.extend(f"  {name}: {name}Schema," for name in names)
    lines.extend(["} as const;", ""])
    return "\n".join(lines)


def _expected_artifacts() -> dict[Path, str]:
    rendered = render_public_schemas()
    index = json.loads(rendered["index.json"])
    extension_contracts = REVIEW_BRIEF_PUBLIC_CONTRACTS + tuple(
        (contract.__name__, contract) for contract in USEFULNESS_STUDY_CONTRACTS
    )
    for name, contract in extension_contracts:
        schema = contract.model_json_schema(mode="validation")
        schema["title"] = name
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://schemas.ecocorridor.local/1.0/{name}.schema.json"
        rendered[f"{name}.schema.json"] = json.dumps(schema, sort_keys=True, indent=2) + "\n"
        index["contracts"].append({"file": f"{name}.schema.json", "name": name})
    rendered["index.json"] = json.dumps(index, sort_keys=True, indent=2) + "\n"
    artifacts = {SCHEMA_DIRECTORY / filename: content for filename, content in rendered.items()}
    artifacts[TYPESCRIPT_OUTPUT] = render_typescript()
    return artifacts


def check_generated_artifacts() -> list[Path]:
    expected = _expected_artifacts()
    drifted = [
        path
        for path, content in expected.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    ]
    expected_schema_names = {path.name for path in expected if path.parent == SCHEMA_DIRECTORY}
    drifted.extend(
        path for path in SCHEMA_DIRECTORY.glob("*.json") if path.name not in expected_schema_names
    )
    return sorted(set(drifted))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail without writing when committed artifacts differ from the contracts",
    )
    arguments = parser.parse_args(argv)
    if arguments.check:
        drifted = check_generated_artifacts()
        if drifted:
            for path in drifted:
                print(f"schema drift: {path.relative_to(ROOT)}")
            return 1
        print("EcoCorridor public contract artifacts are current")
        return 0

    # The base writer deliberately prunes removed core contracts. Review Brief
    # schemas are then written from the same complete expected-artifact set.
    write_public_schemas(SCHEMA_DIRECTORY)
    expected = _expected_artifacts()
    schema_paths: list[Path] = []
    for path, content in expected.items():
        if path == TYPESCRIPT_OUTPUT:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_schema = path.with_suffix(path.suffix + ".tmp")
        temporary_schema.write_text(content, encoding="utf-8", newline="\n")
        temporary_schema.replace(path)
        schema_paths.append(path)
    TYPESCRIPT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = TYPESCRIPT_OUTPUT.with_suffix(".ts.tmp")
    temporary.write_text(render_typescript(), encoding="utf-8", newline="\n")
    temporary.replace(TYPESCRIPT_OUTPUT)
    print(f"Generated {len(schema_paths)} schema files and {TYPESCRIPT_OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
