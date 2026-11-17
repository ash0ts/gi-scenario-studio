#!/usr/bin/env python3
"""Seal the failed zero-inference R2 trace-canary checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gi_scenario_core.evidence_guide_v2_campaign import write_json_atomic
from gi_scenario_core.evidence_guide_v2_incident import build_trace_canary_incident

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign-root",
        type=Path,
        default=ROOT / "dist" / "evidence-guide-v2-campaign-r2",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "evidence-guide-v2-r2-canary-incident.json",
    )
    arguments = parser.parse_args()
    receipt = build_trace_canary_incident(arguments.campaign_root)
    write_json_atomic(arguments.output.resolve(), receipt)
    print(
        json.dumps(
            {
                "mode": "sealed_zero_inference_canary_incident",
                "output": str(arguments.output.resolve()),
                "receipt_sha256": receipt["receipt_sha256"],
                "provider_invocations": 0,
                "scientific_denominator_attempts": 0,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
