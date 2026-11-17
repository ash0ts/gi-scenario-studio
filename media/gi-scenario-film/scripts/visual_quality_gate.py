"""Sample rendered clips densely for edge contact and long static-slide holds."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "dist" / "clips"
REPORT = ROOT / "dist" / "visual-quality-report.json"
BACKGROUND = (245, 247, 246)


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    homebrew = Path("/opt/homebrew/bin") / name
    if homebrew.exists():
        return str(homebrew)
    raise FileNotFoundError(name)


def _duration(path: Path) -> float:
    return float(
        subprocess.check_output(
            [
                _tool("ffprobe"),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            text=True,
        ).strip()
    )


def _difference_score(first: Image.Image, second: Image.Image) -> float:
    difference = ImageChops.difference(first.convert("RGB"), second.convert("RGB"))
    return max(ImageStat.Stat(difference).mean)


def _edge_pixels(frame: Image.Image, border: int = 24, threshold: int = 14) -> int:
    rgb = frame.convert("RGB")
    background = Image.new("RGB", rgb.size, BACKGROUND)
    difference = ImageChops.difference(rgb, background)
    mask = difference.convert("L").point(lambda value: 255 if value > threshold else 0)
    width, height = mask.size
    regions = (
        mask.crop((0, 0, width, border)),
        mask.crop((0, height - border, width, height)),
        mask.crop((0, border, border, height - border)),
        mask.crop((width - border, border, width, height - border)),
    )
    return sum(region.histogram()[255] for region in regions)


def inspect_clip(
    path: Path,
    *,
    sample_fps: int,
    max_static_seconds: float,
    min_edge_pixels: int,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="gi-film-audit-") as temporary:
        pattern = Path(temporary) / "frame-%05d.png"
        subprocess.run(
            [
                _tool("ffmpeg"),
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                f"fps={sample_fps},scale=960:540:flags=lanczos",
                str(pattern),
            ],
            check=True,
        )
        frame_paths = sorted(Path(temporary).glob("frame-*.png"))
        edge_contacts: list[dict] = []
        longest_static_samples = 0
        static_samples = 0
        previous: Image.Image | None = None
        for index, frame_path in enumerate(frame_paths):
            with Image.open(frame_path) as opened:
                frame = opened.convert("RGB")
            edge_count = _edge_pixels(frame)
            # A handful of isolated pixels can be H.264 ringing against the
            # survey-paper background.  Require a contiguous-looking signal
            # before classifying the frame as edge contact.
            if edge_count >= min_edge_pixels:
                edge_contacts.append(
                    {
                        "seconds": round(index / sample_fps, 3),
                        "pixels": edge_count,
                    }
                )
            if previous is not None and _difference_score(previous, frame) < 0.2:
                static_samples += 1
                longest_static_samples = max(longest_static_samples, static_samples)
            else:
                static_samples = 0
            previous = frame
    longest_static = longest_static_samples / sample_fps
    return {
        "clip": path.name,
        "duration_seconds": _duration(path),
        "sample_fps": sample_fps,
        "frames_sampled": len(frame_paths),
        "edge_contacts": edge_contacts,
        "longest_static_seconds": longest_static,
        "passes": not edge_contacts and longest_static <= max_static_seconds,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-fps", type=int, default=2)
    parser.add_argument("--max-static-seconds", type=float, default=5.0)
    parser.add_argument("--min-edge-pixels", type=int, default=20)
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()
    if args.sample_fps <= 0:
        raise SystemExit("--sample-fps must be positive")
    if args.min_edge_pixels <= 0:
        raise SystemExit("--min-edge-pixels must be positive")
    clips = sorted(CLIPS.glob("*.mp4"))
    if not clips:
        raise SystemExit(f"No clips found under {CLIPS}")
    records = [
        inspect_clip(
            clip,
            sample_fps=args.sample_fps,
            max_static_seconds=args.max_static_seconds,
            min_edge_pixels=args.min_edge_pixels,
        )
        for clip in clips
    ]
    payload = {
        "schema_version": "1.0",
        "sample_fps": args.sample_fps,
        "max_static_seconds": args.max_static_seconds,
        "min_edge_pixels": args.min_edge_pixels,
        "clips": records,
        "passes": all(record["passes"] for record in records),
    }
    REPORT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(REPORT)
    for record in records:
        print(
            f"{record['clip']}: edge_frames={len(record['edge_contacts'])} "
            f"longest_static={record['longest_static_seconds']:.1f}s"
        )
    if not payload["passes"] and not args.no_fail:
        raise SystemExit("Visual quality gate failed")


if __name__ == "__main__":
    main()
