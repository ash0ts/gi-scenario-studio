#!/usr/bin/env python3
"""Validate and summarize a completed, offline agent-complexity campaign.

The command performs no provider, W&B, or Weave calls.  Its input must contain
the frozen plan and all immutable execution records exported by the campaign
orchestrator.  Incomplete campaigns fail instead of producing a partial result.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ecocorridor_core.usefulness_agent_campaign import (
    AGENT_COMPLEXITY_SCHEMA_VERSION,
    AgentComplexityCampaignPlan,
    AgentComplexityInvocationRecord,
    summarize_completed_agent_complexity_campaign,
)


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--completed-at must include a timezone")
    return parsed


def _read_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("campaign payload must be a JSON object")
    if payload.get("schema_version") != AGENT_COMPLEXITY_SCHEMA_VERSION:
        raise ValueError("campaign payload schema version is unsupported")
    return payload


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--completed-at", type=_aware_datetime, required=True)
    parser.add_argument("--bootstrap-seed", type=int, default=73_021)
    parser.add_argument("--bootstrap-resamples", type=int, default=10_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    payload = _read_payload(args.campaign.resolve())
    plan = AgentComplexityCampaignPlan.model_validate(payload.get("plan"))
    records_payload = payload.get("records")
    if not isinstance(records_payload, list):
        raise TypeError("campaign records must be a JSON array")
    records = tuple(AgentComplexityInvocationRecord.model_validate(item) for item in records_payload)
    summary = summarize_completed_agent_complexity_campaign(
        plan=plan,
        records=records,
        completed_at=args.completed_at,
        bootstrap_seed=args.bootstrap_seed,
        bootstrap_resamples=args.bootstrap_resamples,
    )
    rendered = json.dumps(
        summary.model_dump(mode="json"),
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
    )
    _atomic_write(args.output.resolve(), f"{rendered}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
