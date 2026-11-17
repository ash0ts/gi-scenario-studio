#!/usr/bin/env python3
"""Run the mandatory Evidence Guide v2 scorer mutation gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gi_scenario_core.evidence_evaluation_v2 import canonical_json, load_split, mutation_suite


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("development", "validation"), default="development")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = mutation_suite(load_split(args.split))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json(result))
    print(
        json.dumps(
            {
                "split": args.split,
                "safety_mutations_detected": result["safety_mutations_detected"],
                "safety_mutation_count": result["safety_mutation_count"],
                "benign_controls_passed": result["benign_controls_passed"],
                "benign_control_count": result["benign_control_count"],
                "release_gate_passed": result["release_gate_passed"],
            },
            sort_keys=True,
        )
    )
    return 0 if result["release_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
