"""Run and optionally publish the verified Ridgeway direct-evidence projection."""

from __future__ import annotations

import argparse
from pathlib import Path

from ecocorridor_core.fixture_pipeline import run_ridgeway_direct_study


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--julia", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--depot", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--job-limit", type=int)
    parser.add_argument("--publish-directory", type=Path)
    arguments = parser.parse_args()

    def progress(done: int, total: int, cached: bool) -> None:
        print(f"direct-job {done}/{total} ({'cache' if cached else 'executed'})", flush=True)

    result = run_ridgeway_direct_study(
        output_directory=arguments.output,
        julia_executable=arguments.julia,
        julia_project=arguments.project,
        julia_depot=arguments.depot,
        worker_count=arguments.workers,
        job_limit=arguments.job_limit,
        publish_directory=arguments.publish_directory,
        progress=progress,
    )
    print(result.model_dump_json(indent=2))
    return 0 if result.state == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
