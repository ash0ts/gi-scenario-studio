#!/usr/bin/env python3
"""Render and validate media only after a complete provider-backed Review Brief campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_runtime_tools() -> tuple[dict[str, str], list[dict[str, str]]]:
    candidates = {
        "node": ("NODE_BIN", (), ("node",), ("--version",)),
        "ffmpeg": (
            "FFMPEG_BIN",
            ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"),
            ("ffmpeg",),
            ("-version",),
        ),
        "ffprobe": (
            "FFPROBE_BIN",
            ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"),
            ("ffprobe",),
            ("-version",),
        ),
        "chrome": (
            "CHROME_BIN",
            (
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ),
            ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
            ("--version",),
        ),
    }
    paths: dict[str, str] = {}
    evidence: list[dict[str, str]] = []
    for name, (environment_name, fixed, commands, version_arguments) in candidates.items():
        choices = [os.environ.get(environment_name), *fixed]
        choices.extend(shutil.which(command) for command in commands)
        selected: Path | None = None
        for choice in choices:
            if not choice:
                continue
            candidate = Path(choice).resolve()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                selected = candidate
                break
        if selected is None:
            raise RuntimeError(f"required animation runtime is unavailable: {name}")
        version = subprocess.run(
            [str(selected), *version_arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        version_text = (version.stdout or version.stderr).strip().splitlines()[0]
        if not version_text or len(version_text) > 512:
            raise RuntimeError(f"animation runtime version is invalid: {name}")
        paths[name] = str(selected)
        evidence.append(
            {
                "name": name,
                "version": version_text,
                "executable_sha256": _sha256_file(selected),
            }
        )
    return paths, sorted(evidence, key=lambda item: item["name"])


def _capture_encoding_inputs(
    *,
    auditor: Path,
    html_path: Path,
    output_directory: Path,
    runtime_paths: Mapping[str, str],
) -> tuple[str, int, list[dict[str, Any]]]:
    result = subprocess.run(
        [
            runtime_paths["node"],
            str(auditor),
            str(html_path),
            runtime_paths["chrome"],
            str(output_directory),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = json.loads(result.stdout)
    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("frames"), list):
        raise TypeError("JPEG frame auditor did not return an ordered encoding-input manifest")
    if parsed.get("format") != "jpeg" or parsed.get("jpeg_quality") != 92:
        raise ValueError("JPEG frame auditor changed its exact capture settings")
    frames = [dict(item) for item in parsed["frames"]]
    for index, frame in enumerate(frames):
        logical_name = f"frame-{index:06d}.jpg"
        if frame.get("frame_index") != index or frame.get("logical_name") != logical_name:
            raise ValueError("JPEG encoding-input manifest is not complete and ordered")
        path = output_directory / logical_name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != output_directory:
            raise ValueError("JPEG encoding input is missing or unsafe")
        if frame.get("bytes") != path.stat().st_size or frame.get("jpeg_sha256") != _sha256_file(
            path
        ):
            raise ValueError("JPEG encoding-input bytes differ from their manifest")
    return "jpeg", 92, frames


def _encode_video_from_inputs(
    *,
    frame_directory: Path,
    output_path: Path,
    fps: int,
    ffmpeg: str,
) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(frame_directory / "frame-%06d.jpg"),
            "-vf",
            "scale=in_range=pc:out_range=tv,format=yuv420p",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-an",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        check=True,
    )


def _run_renderer(
    *,
    root: Path,
    html_path: Path,
    output_directory: Path,
    runtime_paths: Mapping[str, str],
    runtime_tools: Sequence[Mapping[str, str]],
) -> Mapping[str, Any]:
    renderer = root / "scripts" / "render_ecocorridor_canvas_film.mjs"
    validator = root / "scripts" / "validate_ecocorridor_canvas_film.mjs"
    frame_auditor = root / "scripts" / "capture_ecocorridor_review_brief_jpeg_frames.mjs"
    environment = {
        **os.environ,
        "CHROME_BIN": runtime_paths["chrome"],
        "FFMPEG_BIN": runtime_paths["ffmpeg"],
        "FFPROBE_BIN": runtime_paths["ffprobe"],
        # Checkpoints, poster, and contact sheet still come from the shared
        # renderer. The two contracted auditor passes below own the final JPEG
        # sequence and avoid an otherwise redundant third full-frame capture.
        "ECOCORRIDOR_STILLS_ONLY": "1",
    }
    subprocess.run(
        [runtime_paths["node"], str(renderer), str(html_path), str(output_directory)],
        cwd=root,
        check=True,
        env=environment,
    )
    with (
        tempfile.TemporaryDirectory(prefix="ecocorridor-encoding-input-first-") as first_temporary,
        tempfile.TemporaryDirectory(
            prefix="ecocorridor-encoding-input-second-"
        ) as second_temporary,
    ):
        first_directory = Path(first_temporary).resolve()
        second_directory = Path(second_temporary).resolve()
        encoding_format, jpeg_quality, first_pass = _capture_encoding_inputs(
            auditor=frame_auditor,
            html_path=html_path,
            output_directory=first_directory,
            runtime_paths=runtime_paths,
        )
        second_format, second_quality, second_pass = _capture_encoding_inputs(
            auditor=frame_auditor,
            html_path=html_path,
            output_directory=second_directory,
            runtime_paths=runtime_paths,
        )
        if (second_format, second_quality) != (
            encoding_format,
            jpeg_quality,
        ) or second_pass != first_pass:
            raise RuntimeError("independent JPEG encoding-input capture differs")
        _encode_video_from_inputs(
            frame_directory=first_directory,
            output_path=output_directory / f"{FILM_SLUG}.mp4",
            fps=15,
            ffmpeg=runtime_paths["ffmpeg"],
        )
        result = subprocess.run(
            [runtime_paths["node"], str(validator), str(html_path), str(output_directory)],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        parsed = json.loads(result.stdout)
        if not isinstance(parsed, Mapping):
            raise TypeError("Canvas validator did not return a JSON object")
    return {
        **parsed,
        "encodingInputFormat": encoding_format,
        "encodingInputJpegQuality": jpeg_quality,
        "encodingInputFrames": first_pass,
        "secondPassEncodingInputFrames": second_pass,
        "frameAuditorSha256": _sha256_file(frame_auditor),
        "runtimeTools": [dict(item) for item in runtime_tools],
        "mp4ValidationMode": "DECODE_INTEGRITY_ONLY_NO_PIXEL_EQUIVALENCE",
    }


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
        runtime_paths, runtime_tools = _resolve_runtime_tools()
        raw_validation = _run_renderer(
            root=root,
            html_path=html_path,
            output_directory=staging,
            runtime_paths=runtime_paths,
            runtime_tools=runtime_tools,
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
