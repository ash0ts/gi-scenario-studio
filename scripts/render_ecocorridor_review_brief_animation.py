#!/usr/bin/env python3
"""Render and validate media only after a complete provider-backed Review Brief campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ecocorridor_core.review_brief_animation import (
    FILM_SLUG,
    build_review_brief_animation_media_manifest,
    build_review_brief_animation_render_contract,
    build_review_brief_animation_validation_receipt,
    load_verified_review_brief_campaign,
    render_review_brief_animation_html,
    render_review_brief_animation_transcript,
    validate_review_brief_animation_media_manifest,
)

MANIFEST_NAME = "review-brief-animation-media-manifest.json"


def _git_source(root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("repository HEAD is not a full Git commit")
    return commit, not bool(status.strip())


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_bytes(json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def _run_renderer(*, root: Path, html_path: Path, output_directory: Path) -> Mapping[str, Any]:
    renderer = root / "scripts" / "render_ecocorridor_canvas_film.mjs"
    validator = root / "scripts" / "validate_ecocorridor_canvas_film.mjs"
    subprocess.run(
        ["node", str(renderer), str(html_path), str(output_directory)],
        cwd=root,
        check=True,
    )
    result = subprocess.run(
        ["node", str(validator), str(html_path), str(output_directory)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, Mapping):
        raise TypeError("Canvas validator did not return a JSON object")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--authorize-verified-provider-media-render", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.repository_root.resolve()
    campaign = load_verified_review_brief_campaign(arguments.campaign, repository_root=root)
    commit, clean = _git_source(root)
    if campaign.source_commit != commit:
        raise SystemExit("animation campaign source commit must equal repository HEAD")
    if not clean:
        raise SystemExit("animation rendering requires a clean source tree")
    contract = build_review_brief_animation_render_contract(campaign)
    if not arguments.authorize_verified_provider_media_render:
        raise SystemExit("animation rendering requires --authorize-verified-provider-media-render")
    output = arguments.output_directory.resolve()
    if output.exists():
        raise SystemExit("animation output directory must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=str(output.parent))).resolve()
    try:
        contract_path = staging / "review-brief-animation-render-contract.json"
        _write_json(contract_path, contract.model_dump(mode="json"))
        transcript = render_review_brief_animation_transcript(contract)
        transcript_path = staging / "review-brief-animation-transcript.txt"
        transcript_path.write_text(transcript, encoding="utf-8")
        if hashlib.sha256(transcript_path.read_bytes()).hexdigest() != contract.transcript_sha256:
            raise RuntimeError("animation transcript digest differs before render")
        html_path = staging / f"{FILM_SLUG}.html"
        html_path.write_text(render_review_brief_animation_html(contract), encoding="utf-8")
        html_sha256 = hashlib.sha256(html_path.read_bytes()).hexdigest()
        raw_validation = _run_renderer(
            root=root,
            html_path=html_path,
            output_directory=staging,
        )
        validation = build_review_brief_animation_validation_receipt(
            contract=contract,
            html_sha256=html_sha256,
            raw_validator_result=raw_validation,
        )
        validation_path = staging / "review-brief-animation-validation.json"
        _write_json(validation_path, validation.model_dump(mode="json"))
        manifest = build_review_brief_animation_media_manifest(
            campaign=campaign,
            contract=contract,
            validation_receipt=validation,
            media_directory=staging,
        )
        manifest_path = staging / MANIFEST_NAME
        _write_json(manifest_path, manifest.model_dump(mode="json"))
        validate_review_brief_animation_media_manifest(
            manifest,
            campaign=campaign,
            media_directory=staging,
        )
        staging.replace(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(
        json.dumps(
            {
                "status": "VALIDATED",
                "campaign_sha256": campaign.campaign_sha256,
                "story_sha256": contract.story_sha256,
                "media_manifest_sha256": manifest.media_manifest_sha256,
                "media_manifest": str(output / MANIFEST_NAME),
                "video_rendered": True,
                "video_logged": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
