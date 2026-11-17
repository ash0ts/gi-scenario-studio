#!/usr/bin/env python3
"""Build the immutable r2 amendment from the interrupted r1 campaign."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gi_scenario_core.evidence_guide_v2_amendment import (
    build_protocol_amendment,
    write_protocol_amendment,
)

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--r1-root",
        type=Path,
        default=ROOT / "dist" / "evidence-guide-v2-campaign",
    )
    parser.add_argument(
        "--preflight-audit",
        type=Path,
        default=ROOT / "dist" / "evidence-guide-v2-preflight-audit.json",
    )
    parser.add_argument(
        "--corpus-root",
        type=Path,
        default=ROOT / "benchmarks" / "evidence_guide" / "v2",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "evidence-guide-v2-protocol-amendment-r2.json",
    )
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    receipt = build_protocol_amendment(
        r1_root=arguments.r1_root,
        preflight_audit_path=arguments.preflight_audit,
        corpus_root=arguments.corpus_root,
    )
    write_protocol_amendment(arguments.output.resolve(), receipt)
    print(
        json.dumps(
            {
                "mode": "local_protocol_amendment",
                "network_calls": 0,
                "output": str(arguments.output.resolve()),
                "receipt_sha256": receipt["receipt_sha256"],
                "inherited_capability_attempts": receipt["capability_inheritance"][
                    "attempt_count"
                ],
                "excluded_architecture_attempts": receipt["architecture_exclusion"][
                    "attempt_count"
                ],
                "cumulative_prior_usage": receipt["cumulative_prior_usage"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
