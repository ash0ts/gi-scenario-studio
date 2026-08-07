"""Run or independently verify the CC0 usefulness direct-evidence study."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ecocorridor_core.usefulness_direct_study import (
    UsefulnessDirectStudyError,
    run_usefulness_direct_study,
    verify_usefulness_evidence_package,
)

EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 2
EXIT_REJECTED = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="execute or resume the 160 direct jobs")
    run.add_argument("--materialized", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--engine", type=Path, required=True)
    run.add_argument("--engine-manifest", type=Path, required=True)
    run.add_argument("--engine-sha256", required=True)
    run.add_argument("--engine-provenance", type=Path)
    run.add_argument("--engine-attestation-bundle", type=Path)
    run.add_argument(
        "--test-only-engine",
        action="store_true",
        help="allow a local/fake engine only as categorically non-publishable evidence",
    )
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--timeout-seconds", type=float, default=300.0)
    run.add_argument("--job-limit", type=int)

    verify = commands.add_parser("verify", help="verify a completed immutable package")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--engine-sha256", required=True)
    verify.add_argument("--engine-manifest", type=Path, required=True)
    verify.add_argument("--engine-provenance", type=Path, required=True)
    verify.add_argument("--engine-attestation-bundle", type=Path)
    return parser


def _execute(arguments: argparse.Namespace) -> int:
    if arguments.command == "verify":
        receipt = verify_usefulness_evidence_package(
            manifest_path=arguments.manifest,
            archive_path=arguments.archive,
            expected_engine_sha256=arguments.engine_sha256,
            engine_manifest_path=arguments.engine_manifest,
            engine_provenance_path=arguments.engine_provenance,
            engine_attestation_bundle_path=arguments.engine_attestation_bundle,
        )
        print(receipt.model_dump_json(indent=2))
        return EXIT_COMPLETE

    def progress(completed: int, total: int, cached: bool) -> None:
        print(
            f"direct-job {completed}/{total} ({'cache' if cached else 'executed'})",
            flush=True,
        )

    result = run_usefulness_direct_study(
        materialized_root=arguments.materialized,
        output_directory=arguments.output,
        engine_executable=arguments.engine,
        engine_manifest_path=arguments.engine_manifest,
        expected_engine_sha256=arguments.engine_sha256,
        engine_provenance_path=arguments.engine_provenance,
        engine_attestation_bundle_path=arguments.engine_attestation_bundle,
        test_only_engine=arguments.test_only_engine,
        worker_count=arguments.workers,
        timeout_seconds=arguments.timeout_seconds,
        job_limit=arguments.job_limit,
        progress=progress,
    )
    print(result.model_dump_json(indent=2))
    return EXIT_COMPLETE if result.state == "COMPLETED_QA_ACCEPTED" else EXIT_INCOMPLETE


def main() -> int:
    arguments = _parser().parse_args()
    try:
        return _execute(arguments)
    except UsefulnessDirectStudyError as exc:
        print(f"direct-evidence rejected: {exc}", file=sys.stderr)
        return EXIT_REJECTED


if __name__ == "__main__":
    raise SystemExit(main())
