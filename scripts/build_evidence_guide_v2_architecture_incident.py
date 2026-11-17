#!/usr/bin/env python3
"""Seal the aborted Evidence Guide architecture-development R1 checkpoint.

This command performs no network work and publishes nothing.  If the source
SQLite database has a non-empty WAL, it checkpoints only that WAL, verifies the
checkpoint is complete, and then delegates to an immutable read-only builder.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from gi_scenario_core.evidence_guide_v2_architecture_incident import (
    build_architecture_development_incident,
)
from gi_scenario_core.evidence_guide_v2_campaign import write_json_atomic

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAMPAIGN_ROOT = ROOT / "dist" / "evidence-guide-v2-architecture-dev-r1"
DEFAULT_OUTPUT = ROOT / "dist" / "evidence-guide-v2-architecture-dev-r1-incident.json"


def _checkpoint_wal_if_needed(database: Path) -> bool:
    """Checkpoint a material WAL so the subsequent read can be immutable."""

    if not database.is_file():
        raise FileNotFoundError(database)
    wal = database.with_name(f"{database.name}-wal")
    if not wal.exists() or wal.stat().st_size == 0:
        return False
    connection = sqlite3.connect(database)
    try:
        busy, _log_frames, _checkpointed_frames = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
    finally:
        connection.close()
    if busy or (wal.exists() and wal.stat().st_size):
        raise RuntimeError("architecture R1 WAL checkpoint did not complete")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, default=DEFAULT_CAMPAIGN_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    campaign_root = arguments.campaign_root.resolve()
    checkpointed = _checkpoint_wal_if_needed(campaign_root / "campaign.sqlite")
    receipt = build_architecture_development_incident(campaign_root)
    output = arguments.output.resolve()
    write_json_atomic(output, receipt)
    print(
        json.dumps(
            {
                "mode": "sealed_architecture_r1_incident",
                "network_calls": 0,
                "published": False,
                "wal_checkpointed": checkpointed,
                "output": str(output),
                "receipt_sha256": receipt["receipt_sha256"],
                "disposition": receipt["disposition"],
                "materialized_attempts": receipt["materialized_attempt_count"],
                "scientific_denominator_attempts": 0,
                "incident_usage": receipt["incident_usage"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

