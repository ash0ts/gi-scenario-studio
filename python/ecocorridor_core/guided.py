"""Command-line verification for a pinned Ridgeway direct-evidence bundle."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .guided_evidence import GuidedEvidenceResource


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecocorridor-fixture",
        description=(
            "Verify a Ridgeway projection, manifest, and retained direct-evidence archive "
            "against externally supplied SHA-256 identities."
        ),
    )
    parser.add_argument("--projection", type=Path, required=True)
    parser.add_argument("--projection-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument(
        "--materialize",
        type=Path,
        help="Optionally extract the verified archive under this local parent directory.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    resource = GuidedEvidenceResource.verify(
        projection_path=arguments.projection,
        projection_sha256=arguments.projection_sha256,
        manifest_path=arguments.manifest,
        manifest_sha256=arguments.manifest_sha256,
        archive_path=arguments.archive,
        archive_sha256=arguments.archive_sha256,
    )
    materialized = (
        str(resource.materialize(arguments.materialize)) if arguments.materialize else None
    )
    print(
        json.dumps(
            {
                "status": "VERIFIED_DIRECT_EVIDENCE",
                "fixture_id": resource.projection["fixtureId"],
                "matrix_job_count": resource.manifest["matrix_job_count"],
                "map_job_count": resource.manifest["map_job_count"],
                "manifest_sha256": resource.manifest_sha256,
                "archive_sha256": resource.archive_sha256,
                "materialized_path": materialized,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
