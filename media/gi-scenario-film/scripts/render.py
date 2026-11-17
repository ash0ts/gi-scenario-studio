"""Render one or all modular Manim scenes into editor-ready clips."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from gi_story.render_provenance import render_input_fingerprint, render_input_hashes, sha256

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "story" / "story-manifest.json"
WORK = ROOT / "dist" / "manim-work"
CLIPS = ROOT / "dist" / "clips"


def animation_segments(story: dict) -> list[dict]:
    segments = story.get("segments", story)
    return [segment for segment in segments if segment.get("kind") == "animation"]


def ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def command_for(segment: dict, quality: str) -> tuple[list[str], dict[str, str]]:
    resolution, fps = {
        "smoke": ("854,480", "8"),
        "draft": ("1280,720", "24"),
        "production": ("1920,1080", "30"),
    }[quality]
    env = os.environ.copy()
    if quality == "smoke":
        env["GI_FILM_SMOKE"] = "1"
    command = [
        sys.executable,
        "-m",
        "manim",
        "--config_file",
        str(ROOT / "manim.cfg"),
        "--renderer",
        "cairo",
        "--resolution",
        resolution,
        "--fps",
        fps,
        "--media_dir",
        str(WORK),
        "--format",
        "mp4",
        "--disable_caching",
        str(ROOT / segment["source_file"]),
        segment["scene_class"],
        "--output_file",
        segment["output_file"],
    ]
    return command, env


def find_render(output_file: str) -> Path:
    matches = sorted(WORK.glob(f"videos/**/{output_file}"), key=lambda item: item.stat().st_mtime)
    if not matches:
        raise FileNotFoundError(f"Manim did not create {output_file} under {WORK}")
    return matches[-1]


def normalize(source: Path, destination: Path, *, fps: int) -> None:
    temporary = destination.with_suffix(".tmp.mp4")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_path(),
            "-y",
            "-i",
            str(source),
            "-an",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
        check=True,
    )
    temporary.replace(destination)


def write_render_sidecar(
    destination: Path,
    hashes: dict[str, str],
    quality: str,
) -> None:
    ffmpeg_version = subprocess.check_output([ffmpeg_path(), "-version"], text=True).splitlines()[0]
    payload = {
        "schema_version": "1.0",
        "clip": destination.name,
        "quality": quality,
        "clip_sha256": sha256(destination),
        "render_input_fingerprint": render_input_fingerprint(hashes),
        "render_input_hashes": hashes,
        "tool_versions": {
            "python": platform.python_version(),
            "manim": version("manim"),
            "ffmpeg": ffmpeg_version,
        },
    }
    sidecar = destination.with_suffix(".render.json")
    temporary = sidecar.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(sidecar)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quality", choices=("smoke", "draft", "production"), default="draft")
    parser.add_argument("--scene", default="all", help="Scene id, such as 01_pareto_trap, or all")
    args = parser.parse_args()
    story = json.loads(MANIFEST.read_text(encoding="utf-8"))
    segments = animation_segments(story)
    if args.scene != "all":
        segments = [segment for segment in segments if segment["id"] == args.scene]
        if not segments:
            raise SystemExit(f"Unknown scene id: {args.scene}")
    for segment in segments:
        input_hashes_before = render_input_hashes(ROOT, story, segment)
        command, env = command_for(segment, args.quality)
        print(f"Rendering {segment['id']} ({args.quality})")
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        source = find_render(segment["output_file"])
        destination = CLIPS / segment["output_file"]
        normalize(
            source,
            destination,
            fps=30 if args.quality == "production" else (8 if args.quality == "smoke" else 24),
        )
        input_hashes_after = render_input_hashes(ROOT, story, segment)
        if input_hashes_after != input_hashes_before:
            raise RuntimeError(f"Render inputs changed while {segment['id']} was rendering")
        write_render_sidecar(destination, input_hashes_before, args.quality)
        print(destination)


if __name__ == "__main__":
    main()
