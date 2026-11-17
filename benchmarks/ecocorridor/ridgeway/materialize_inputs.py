"""Materialize deterministic Ridgeway source inputs; never generates outcomes."""

from __future__ import annotations

import argparse
from pathlib import Path

from ecocorridor_core.fixture_pipeline import materialize_ridgeway_ascii


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_directory", type=Path)
    arguments = parser.parse_args()
    manifest = materialize_ridgeway_ascii(arguments.output_directory)
    print(
        f"Materialized {len(manifest.files)} source files; "
        f"state={manifest.execution_state}; release_replay_eligible=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
