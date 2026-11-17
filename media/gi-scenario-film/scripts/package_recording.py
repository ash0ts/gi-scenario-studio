"""Validate and fingerprint normalized app captures and an optional final master."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from gi_story.render_provenance import sha256

ROOT = Path(__file__).resolve().parents[1]
STORY_PATH = ROOT / "story" / "story-manifest.json"
APP_CAPTURES = ROOT / "dist" / "app-captures"
OUTPUT_PATH = ROOT / "dist" / "recording-manifest.json"


def tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    homebrew = Path("/opt/homebrew/bin") / name
    if homebrew.exists():
        return str(homebrew)
    raise FileNotFoundError(name)


def probe(path: Path) -> dict:
    output = subprocess.check_output(
        [tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        text=True,
    )
    return json.loads(output)


def validate_video(path: Path, target_duration: float, tolerance: float) -> dict:
    metadata = probe(path)
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    duration = float(metadata["format"]["duration"])
    assert (video["width"], video["height"]) == (1920, 1080), path
    assert video["codec_name"] == "h264", path
    assert video["pix_fmt"] == "yuv420p", path
    assert video["r_frame_rate"] == video["avg_frame_rate"] == "30/1", path
    assert abs(duration - target_duration) <= tolerance, (path, duration, target_duration)
    subprocess.run(
        [tool("ffmpeg"), "-v", "error", "-i", path, "-f", "null", "-"],
        check=True,
    )
    try:
        manifest_path = str(path.relative_to(ROOT))
    except ValueError:
        manifest_path = path.name
    return {
        "path": manifest_path,
        "duration_seconds": duration,
        "frame_count": int(video.get("nb_frames") or round(duration * 30)),
        "sha256": sha256(path),
        "width": video["width"],
        "height": video["height"],
        "fps": video["avg_frame_rate"],
        "codec": video["codec_name"],
        "pixel_format": video["pix_fmt"],
        "has_audio": any(stream["codec_type"] == "audio" for stream in metadata["streams"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--master",
        type=Path,
        help="Optional assembled 27:50 master to validate and fingerprint.",
    )
    args = parser.parse_args()
    story = json.loads(STORY_PATH.read_text(encoding="utf-8"))
    application_segments = [
        segment for segment in story["segments"] if segment["kind"] == "application"
    ]
    missing = [
        APP_CAPTURES / f"{segment['id']}.mp4"
        for segment in application_segments
        if not (APP_CAPTURES / f"{segment['id']}.mp4").is_file()
    ]
    if missing:
        names = ", ".join(path.name for path in missing)
        raise SystemExit(f"Missing normalized application captures: {names}")

    captures = []
    for segment in application_segments:
        path = APP_CAPTURES / f"{segment['id']}.mp4"
        record = validate_video(path, float(segment["duration_seconds"]), tolerance=0.25)
        record.update(
            {
                "id": segment["id"],
                "start_seconds": segment["start_seconds"],
                "target_duration_seconds": segment["duration_seconds"],
            }
        )
        captures.append(record)

    output = {
        "schema_version": "1.0",
        "film_id": story["film_id"],
        "story_runtime_seconds": story["runtime_seconds"],
        "story_manifest_sha256": sha256(STORY_PATH),
        "animation_edit_manifest_sha256": sha256(ROOT / "dist" / "edit-manifest.json"),
        "combined_captions_sha256": sha256(ROOT / "story" / "combined.srt"),
        "performer_script_sha256": sha256(ROOT / "story" / "performer-script.md"),
        "editor_script_sha256": sha256(ROOT / "story" / "editor-script.md"),
        "application_captures": captures,
    }
    if args.master is not None:
        master = args.master.expanduser().resolve()
        output["master"] = validate_video(
            master,
            float(story["runtime_seconds"]),
            tolerance=0.5,
        )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_PATH.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_PATH)
    print(OUTPUT_PATH)


if __name__ == "__main__":
    main()
