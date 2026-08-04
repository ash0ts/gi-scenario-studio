from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from ecocorridor_core.guided import main

ROOT = Path(__file__).resolve().parents[2]
RIDGEWAY = ROOT / "benchmarks" / "ecocorridor" / "ridgeway"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_guided_console_entrypoint_verifies_current_pinned_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = RIDGEWAY / "direct-evidence-manifest.json"
    declaration = json.loads(manifest.read_text(encoding="utf-8"))
    projection = RIDGEWAY / "ui_projection.json"
    archive = RIDGEWAY / declaration["evidence_bundle"]["filename"]

    assert (
        main(
            [
                "--projection",
                str(projection),
                "--projection-sha256",
                _digest(projection),
                "--manifest",
                str(manifest),
                "--manifest-sha256",
                _digest(manifest),
                "--archive",
                str(archive),
                "--archive-sha256",
                _digest(archive),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "archive_sha256": _digest(archive),
        "fixture_id": "ridgeway_crossing",
        "manifest_sha256": _digest(manifest),
        "map_job_count": 60,
        "materialized_path": None,
        "matrix_job_count": 1_536,
        "status": "VERIFIED_DIRECT_EVIDENCE",
    }
