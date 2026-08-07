#!/usr/bin/env python3
"""Verify the no-result EcoCorridor checkpoint already published in the W&B Report."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ecocorridor_core.usefulness_direct_reporting import (
    GitPushedSourceVerifier,
    PushedSourceReceipt,
    PushedSourceVerifier,
)
from ecocorridor_core.usefulness_narrative import (
    IMPLEMENTATION_CHECKPOINT_COMMIT,
    SOURCE_REPOSITORY,
    WandbUsefulnessNarrativeVerifier,
    atomic_write_receipt,
    load_ridgeway_narrative_evidence,
)

EXPECTED_BRANCH = "codex/ecocorridor-scenario-studio"
EXPECTED_UPSTREAM_REF = f"refs/heads/{EXPECTED_BRANCH}"
SOURCE_REPOSITORY_URL = f"https://github.com/{SOURCE_REPOSITORY}"


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def require_publishable_source(
    root: Path,
    *,
    branch: str = EXPECTED_BRANCH,
    source_verifier: PushedSourceVerifier | None = None,
) -> str:
    if branch != EXPECTED_BRANCH:
        raise PermissionError("narrative publication branch cannot be overridden")
    source_commit = _git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise PermissionError("source HEAD is not a full Git commit")
    if _git(root, "branch", "--show-current") != branch:
        raise PermissionError("narrative publication requires the isolated Eco branch")
    verifier = source_verifier or GitPushedSourceVerifier()
    try:
        pushed = PushedSourceReceipt.model_validate(
            verifier.verify(
                repository_root=root,
                source_repository=SOURCE_REPOSITORY_URL,
                source_commit=source_commit,
            )
        )
    except (TypeError, ValueError) as exc:
        raise PermissionError("pushed-source verifier returned an invalid receipt") from exc
    if pushed.source_repository != SOURCE_REPOSITORY_URL:
        raise PermissionError("pushed-source receipt belongs to another repository")
    if pushed.source_commit != source_commit or pushed.upstream_commit != source_commit:
        raise PermissionError("pushed-source receipt belongs to another commit")
    if pushed.verifier != "GIT_LS_REMOTE_EXACT_HEAD":
        raise PermissionError("pushed-source receipt used an unsupported verifier")
    if pushed.upstream_ref != EXPECTED_UPSTREAM_REF:
        raise PermissionError("pushed-source receipt names the wrong Eco upstream branch")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", IMPLEMENTATION_CHECKPOINT_COMMIT, source_commit],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestry.returncode != 0:
        raise PermissionError("source commit does not contain the implementation checkpoint")
    return source_commit


def _outside_repository(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--entity", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--wandb-api-base-url", default="https://api.wandb.ai")
    parser.add_argument("--wandb-web-base-url", default="https://wandb.ai")
    parser.add_argument("--report-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--authorize-report-verification", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.repository_root.resolve()
    source_commit = require_publishable_source(root)
    if not _outside_repository(arguments.output, root):
        raise SystemExit("publication receipt must be written outside the repository")
    if not os.environ.get("WANDB_API_KEY"):
        raise SystemExit("online W&B Report verification requires WANDB_API_KEY in process memory")
    verifier = WandbUsefulnessNarrativeVerifier(
        entity=arguments.entity,
        project=arguments.project,
        api_base_url=arguments.wandb_api_base_url,
        web_base_url=arguments.wandb_web_base_url,
        authorize_report_verification=arguments.authorize_report_verification,
    )
    ridgeway = load_ridgeway_narrative_evidence(root)
    receipt = verifier.verify(
        report_url=arguments.report_url,
        source_commit=source_commit,
        ridgeway=ridgeway,
    )
    atomic_write_receipt(arguments.output, receipt)
    print(
        json.dumps(
            {
                "status": receipt.status,
                "report_url": receipt.report_url,
                "source_commit": receipt.source_commit,
                "historical_run_readback_verified": True,
                "report_readback_verified": True,
                "corrected_campaign_result_count": 0,
                "new_wandb_run_created": False,
                "new_artifact_created": False,
                "new_result_media_logged": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
