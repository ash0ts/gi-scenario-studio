#!/usr/bin/env python3
"""Build the hash-bound audit receipt for invalid Evidence Guide v2 preflights."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gi_scenario_core.evidence_guide_v2_preflight_audit import (
    build_preflight_audit,
    write_preflight_audit,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIRST = ROOT / "dist" / "evidence-guide-v2-campaign-preflight-failed-artifact-ref"
DEFAULT_SECOND = (
    ROOT / "dist" / "evidence-guide-v2-campaign-preflight-failed-weave-evaluation-name-collision"
)
DEFAULT_THIRD = (
    ROOT / "dist" / "evidence-guide-v2-campaign-preflight-failed-weave-reinitialization-flush"
)
DEFAULT_OUTPUT = ROOT / "dist" / "evidence-guide-v2-preflight-audit.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-readback-root", type=Path, default=DEFAULT_FIRST)
    parser.add_argument("--weave-collision-root", type=Path, default=DEFAULT_SECOND)
    parser.add_argument("--weave-reinitialization-root", type=Path, default=DEFAULT_THIRD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    receipt = build_preflight_audit(
        artifact_readback_root=arguments.artifact_readback_root,
        weave_collision_root=arguments.weave_collision_root,
        weave_reinitialization_root=arguments.weave_reinitialization_root,
    )
    write_preflight_audit(arguments.output.resolve(), receipt)
    print(
        json.dumps(
            {
                "mode": "local_preflight_audit",
                "network_calls": 0,
                "output": str(arguments.output.resolve()),
                "receipt_sha256": receipt["receipt_sha256"],
                "scientific_status": receipt["scientific_status"],
                "capability_denominator_attempts": receipt["capability_denominator_attempts"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
