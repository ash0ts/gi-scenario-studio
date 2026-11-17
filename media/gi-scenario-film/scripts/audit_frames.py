"""Build dense, timestamped contact sheets for human review of every rendered scene."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "story" / "story-manifest.json"
CLIPS = ROOT / "dist" / "clips"
OUTPUT = ROOT / "dist" / "audit"
FONT = ROOT / "assets" / "fonts" / "IBMPlexMono-Medium.ttf"


def tool(name: str) -> str:
    executable = shutil.which(name)
    if executable:
        return executable
    homebrew = Path("/opt/homebrew/bin") / name
    if homebrew.exists():
        return str(homebrew)
    raise FileNotFoundError(name)


def duration_seconds(path: Path) -> float:
    result = subprocess.check_output(
        [
            tool("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    )
    return float(result.strip())


def extract_frame(path: Path, at_seconds: float) -> Image.Image:
    payload = subprocess.check_output(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-ss",
            f"{at_seconds:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
    )
    from io import BytesIO

    return Image.open(BytesIO(payload)).convert("RGB")


def timestamp(seconds: float) -> str:
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes):02d}:{remainder:04.1f}"


def make_sheet(scene_id: str, clip: Path, interval: float, columns: int) -> Path:
    duration = duration_seconds(clip)
    times = [0.1]
    sample = interval
    while sample < duration - 0.5:
        times.append(sample)
        sample += interval
    times.append(max(0.1, duration - 0.2))

    tile_width = 640
    frame_height = 360
    label_height = 38
    tile_height = frame_height + label_height
    rows = math.ceil(len(times) / columns)
    sheet = Image.new("RGB", (columns * tile_width, rows * tile_height), "#17313A")
    font = ImageFont.truetype(str(FONT), 24)

    for index, at_seconds in enumerate(times):
        frame = extract_frame(clip, at_seconds)
        frame.thumbnail((tile_width, frame_height), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (tile_width, tile_height), "#F5F7F6")
        tile.paste(frame, ((tile_width - frame.width) // 2, 0))
        draw = ImageDraw.Draw(tile)
        draw.rectangle((0, frame_height, tile_width, tile_height), fill="#17313A")
        draw.text(
            (12, frame_height + 5),
            f"{scene_id}  {timestamp(at_seconds)}",
            fill="#F5F7F6",
            font=font,
        )
        x = (index % columns) * tile_width
        y = (index // columns) * tile_height
        sheet.paste(tile, (x, y))

    destination = OUTPUT / f"{scene_id}-every-{interval:g}s.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=92, subsampling=0)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--columns", type=int, default=3)
    parser.add_argument("--scene", default="all")
    args = parser.parse_args()
    if args.interval <= 0:
        raise ValueError("--interval must be positive")
    if args.columns <= 0:
        raise ValueError("--columns must be positive")

    story = json.loads(MANIFEST.read_text(encoding="utf-8"))
    segments = [segment for segment in story["segments"] if segment["kind"] == "animation"]
    if args.scene != "all":
        segments = [segment for segment in segments if segment["id"] == args.scene]
    if not segments:
        raise ValueError(f"Unknown scene: {args.scene}")

    for segment in segments:
        clip = CLIPS / segment["output_file"]
        if not clip.exists():
            raise FileNotFoundError(clip)
        print(make_sheet(segment["id"], clip, args.interval, args.columns))


if __name__ == "__main__":
    main()
