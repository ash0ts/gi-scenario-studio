"""Materialize input-only EcoCorridor usefulness studies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ecocorridor_core.usefulness_landscapes import (
    materialize_usefulness_landscapes,
    usefulness_public_manifest,
)


def _write_public_manifest(path: Path) -> None:
    payload = usefulness_public_manifest()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create checksum-bound synthetic Circuitscape requests without generating outcomes."
        )
    )
    parser.add_argument("output_directory", type=Path, nargs="?")
    parser.add_argument("--public-manifest", type=Path)
    arguments = parser.parse_args()
    if arguments.output_directory is None and arguments.public_manifest is None:
        parser.error("provide an output directory, --public-manifest, or both")
    if arguments.public_manifest is not None:
        _write_public_manifest(arguments.public_manifest)
        print(f"Wrote result-free public manifest: {arguments.public_manifest}")
    if arguments.output_directory is not None:
        manifest = materialize_usefulness_landscapes(arguments.output_directory)
        print(
            f"Materialized {manifest.direct_job_count} pending direct jobs across "
            f"{manifest.landscape_count} CC0 landscapes; "
            f"state={manifest.direct_evidence_state}; release_eligible=false"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
