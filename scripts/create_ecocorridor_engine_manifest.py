#!/usr/bin/env python3
"""Create the typed EngineManifest bound into an EcoCorridor release."""

from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path

from ecocorridor_core.engine import build_engine_manifest


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-id", required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--julia-runtime", type=Path, required=True)
    parser.add_argument("--julia-project", type=Path, required=True)
    parser.add_argument("--native-artifacts", type=Path, required=True)
    parser.add_argument("--wrapper", type=Path, required=True)
    parser.add_argument("--parser", type=Path, required=True)
    parser.add_argument("--metric-definitions", type=Path, required=True)
    parser.add_argument("--neighbor-rule", choices=["FOUR", "EIGHT"], required=True)
    parser.add_argument("--resistance-average", choices=["ARITHMETIC", "HARMONIC"], required=True)
    parser.add_argument("--platform", default=platform.platform())
    arguments = parser.parse_args()
    manifest = build_engine_manifest(
        manifest_id=arguments.manifest_id,
        executable=arguments.executable,
        julia_runtime=arguments.julia_runtime,
        julia_project_directory=arguments.julia_project,
        native_artifacts=arguments.native_artifacts,
        wrapper=arguments.wrapper,
        parser=arguments.parser,
        metric_definitions=arguments.metric_definitions,
        neighbor_rule=arguments.neighbor_rule,
        resistance_average=arguments.resistance_average,
        platform=arguments.platform,
    )
    # The worker consumes this file as the public ``EngineManifest`` contract
    # itself.  Do not add a packaging-only envelope here: the detached,
    # signed release manifest already authenticates this file and the worker
    # validates the contract before accepting any analysis plan.
    payload = manifest.model_dump(mode="json")
    _atomic_write(
        arguments.output,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
