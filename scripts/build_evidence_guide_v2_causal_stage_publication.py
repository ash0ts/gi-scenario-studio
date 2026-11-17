#!/usr/bin/env python3
"""Audit the live R3 causal stage and emit its fail-closed diagnostic package.

This command performs no network calls and never writes to the campaign
database.  It exits with status 2 when the scientific publication gate blocks,
which is the expected outcome for the preserved R3 checkpoint.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gi_scenario_core.evidence_guide_v2_causal_stage_publication import (
    BLOCKED_DISPOSITION,
    build_causal_stage_publication,
    validate_blocked_receipt,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAMPAIGN_ROOT = (
    ROOT / "dist" / "evidence-guide-v2-architecture-dev-r3" / "live"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, default=DEFAULT_CAMPAIGN_ROOT)
    parser.add_argument("--output-dir", type=Path)
    arguments = parser.parse_args()
    campaign_root = arguments.campaign_root.resolve()
    output_dir = (
        arguments.output_dir.resolve()
        if arguments.output_dir
        else campaign_root / "causal-stage-publication"
    )
    receipt = build_causal_stage_publication(
        campaign_root,
        output_dir=output_dir,
        repository_root=ROOT,
    )
    validate_blocked_receipt(receipt)
    print(
        json.dumps(
            {
                "mode": "read_only_r3_causal_stage_audit",
                "network_calls": 0,
                "database_modified": False,
                "publication_created": False,
                "output_dir": str(output_dir),
                "disposition": receipt["disposition"],
                "observed_causal_attempt_count": receipt[
                    "observed_causal_attempt_count"
                ],
                "dirty_triad_count": receipt["analyzer_gate"][
                    "dirty_triad_count"
                ],
                "receipt_sha256": receipt["receipt_sha256"],
            },
            sort_keys=True,
        )
    )
    if receipt["disposition"] == BLOCKED_DISPOSITION:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
