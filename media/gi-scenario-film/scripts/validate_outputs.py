"""Validate, fingerprint, and package rendered film clips."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path

from gi_story.render_provenance import render_input_fingerprint, render_input_hashes, sha256
from PIL import Image, ImageChops, ImageDraw, ImageStat

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "dist" / "clips"
CHECKPOINTS = ROOT / "dist" / "checkpoints"
MANIFEST = ROOT / "story" / "story-manifest.json"


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
        [tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        text=True,
    )
    return json.loads(output)


def render_source_hashes(story: dict, segments: list[dict]) -> dict[str, str]:
    combined: dict[str, str] = {}
    for segment in segments:
        for path, digest in render_input_hashes(ROOT, story, segment).items():
            assert path not in combined or combined[path] == digest
            combined[path] = digest
    return dict(sorted(combined.items()))


def decoded_frame(path: Path, at_seconds: float, width: int, height: int) -> Image.Image:
    payload = subprocess.check_output(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(path),
            "-ss",
            f"{at_seconds:.6f}",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    expected_bytes = width * height * 3
    assert len(payload) == expected_bytes, (path, at_seconds, len(payload), expected_bytes)
    return Image.frombytes("RGB", (width, height), payload)


def assert_static_handle(
    path: Path,
    first_seconds: float,
    second_seconds: float,
    width: int,
    height: int,
) -> None:
    first = decoded_frame(path, first_seconds, width, height)
    second = decoded_frame(path, second_seconds, width, height)
    difference = ImageChops.difference(first, second)
    extrema = difference.getextrema()
    stats = ImageStat.Stat(difference)
    thresholded = difference.point(lambda value: 255 if value > 16 else 0)
    unchanged_pixels = thresholded.convert("L").histogram()[0]
    changed_ratio = 1 - unchanged_pixels / (width * height)

    # H.264 can quantize an otherwise static frame differently across a GOP,
    # especially around text edges. These combined limits tolerate that sparse,
    # sub-perceptual noise while still rejecting a moving cursor or scene object.
    assert (
        # A few isolated antialiased text-edge pixels can exceed 64 code
        # values even when two decoded frames are visually identical.  The
        # mean, RMS, and changed-area limits below keep this sparse excursion
        # from masking authored movement.
        max(channel_max for _, channel_max in extrema) <= 72
        # Dense text can produce diffuse one-code-value GOP shimmer even when
        # the source frame is static.  The RMS and >16-code changed-pixel
        # limits still reject authored motion; these mean/RMS ceilings merely
        # admit that visually lossless encoder noise.
        and max(stats.mean) <= 1.0
        and max(stats.rms) <= 2.25
        and changed_ratio <= 0.001
    ), (
        path,
        first_seconds,
        second_seconds,
        extrema,
        stats.mean,
        stats.rms,
        changed_ratio,
    )


def extract_end_frame(clip: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            tool("ffmpeg"),
            "-y",
            "-sseof",
            "-0.5",
            "-i",
            str(clip),
            "-frames:v",
            "1",
            "-update",
            "1",
            str(destination),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def extract_frame(clip: Path, destination: Path, at_seconds: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            tool("ffmpeg"),
            "-y",
            "-ss",
            f"{at_seconds:.6f}",
            "-i",
            str(clip),
            "-frames:v",
            "1",
            "-update",
            "1",
            str(destination),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def make_contact_sheet(images: list[Path], destination: Path) -> None:
    thumbs = []
    for path in images:
        with Image.open(path) as image:
            copy = image.convert("RGB")
            copy.thumbnail((480, 270))
            thumbs.append((path.stem, copy.copy()))
    columns = 3
    rows = math.ceil(len(thumbs) / columns)
    sheet = Image.new("RGB", (columns * 500, rows * 310), "#F5F7F6")
    draw = ImageDraw.Draw(sheet)
    for index, (label, image) in enumerate(thumbs):
        x = (index % columns) * 500 + 10
        y = (index // columns) * 310 + 10
        sheet.paste(image, (x, y))
        draw.text((x, y + 275), label, fill="#17313A")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination)


def image_record(path: Path) -> dict:
    with Image.open(path) as image:
        width, height = image.size
    return {
        "path": str(path.relative_to(ROOT)),
        "sha256": sha256(path),
        "width": width,
        "height": height,
    }


def concat_review(clips: list[Path], destination: Path) -> None:
    concat_file = destination.with_suffix(".txt")
    concat_file.parent.mkdir(parents=True, exist_ok=True)
    concat_file.write_text(
        "".join(f"file '{clip.resolve()}'\n" for clip in clips), encoding="utf-8"
    )
    subprocess.run(
        [
            tool("ffmpeg"),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(destination),
        ],
        check=True,
    )
    concat_file.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("production", "draft", "smoke"), default="production")
    args = parser.parse_args()
    expected = {
        "production": (1920, 1080, "30/1"),
        "draft": (1280, 720, "24/1"),
        "smoke": (854, 480, "8/1"),
    }[args.profile]
    story = json.loads(MANIFEST.read_text(encoding="utf-8"))
    edit_handles = story["edit_handles"]
    assert float(edit_handles["head_seconds"]) > 0
    assert float(edit_handles["tail_seconds"]) > 0
    for stale_checkpoint in CHECKPOINTS.glob("*.png"):
        stale_checkpoint.unlink()
    segments = [item for item in story.get("segments", story) if item.get("kind") == "animation"]
    records = []
    stills = []
    review_stills = []
    ordered_clips = []
    for segment in segments:
        clip = CLIPS / segment["output_file"]
        if not clip.exists():
            raise FileNotFoundError(clip)
        sidecar_path = clip.with_suffix(".render.json")
        if not sidecar_path.exists():
            raise FileNotFoundError(sidecar_path)
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        current_inputs = render_input_hashes(ROOT, story, segment)
        current_fingerprint = render_input_fingerprint(current_inputs)
        assert sidecar["quality"] == args.profile
        assert sidecar["clip"] == clip.name
        assert sidecar["clip_sha256"] == sha256(clip)
        assert sidecar["render_input_hashes"] == current_inputs
        assert sidecar["render_input_fingerprint"] == current_fingerprint
        metadata = probe(clip)
        video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
        audio = [stream for stream in metadata["streams"] if stream["codec_type"] == "audio"]
        width, height, fps = expected
        assert (video["width"], video["height"]) == (width, height)
        assert video["codec_name"] == "h264"
        assert video["pix_fmt"] == "yuv420p"
        assert video["r_frame_rate"] == fps
        assert video["avg_frame_rate"] == fps
        assert not audio
        subprocess.run(
            [tool("ffmpeg"), "-v", "error", "-i", str(clip), "-f", "null", "-"],
            check=True,
        )
        actual_duration = float(metadata["format"]["duration"])
        still = CHECKPOINTS / f"{segment['id']}-end.png"
        extract_end_frame(clip, still)
        stills.append(still)
        early = CHECKPOINTS / f"{segment['id']}-33.png"
        middle = CHECKPOINTS / f"{segment['id']}-66.png"
        extract_frame(clip, early, actual_duration * 0.33)
        extract_frame(clip, middle, actual_duration * 0.66)
        review_stills.extend((early, middle, still))
        ordered_clips.append(clip)
        frame_count = int(video.get("nb_frames") or round(actual_duration * int(fps.split("/")[0])))
        expected_duration = float(segment["duration_seconds"])
        if args.profile != "smoke":
            assert abs(actual_duration - expected_duration) <= 0.2, (
                segment["id"],
                actual_duration,
                expected_duration,
            )
            expected_frames = round(expected_duration * int(fps.split("/")[0]))
            frame_tolerance = math.ceil(0.2 * int(fps.split("/")[0])) + 1
            assert abs(frame_count - expected_frames) <= frame_tolerance
        if args.profile == "production":
            head_seconds = float(edit_handles["head_seconds"])
            tail_seconds = float(edit_handles["tail_seconds"])
            assert_static_handle(
                clip,
                head_seconds * 0.2,
                head_seconds * 0.8,
                width,
                height,
            )
            assert_static_handle(
                clip,
                actual_duration - tail_seconds * 0.8,
                actual_duration - tail_seconds * 0.2,
                width,
                height,
            )
        records.append(
            {
                "id": segment["id"],
                "path": str(clip.relative_to(ROOT)),
                "target_duration_seconds": expected_duration,
                "duration_seconds": actual_duration,
                "duration_delta_seconds": actual_duration - expected_duration,
                "frame_count": frame_count,
                "sha256": sha256(clip),
                "render_sidecar": str(sidecar_path.relative_to(ROOT)),
                "render_input_fingerprint": current_fingerprint,
                "tool_versions": sidecar["tool_versions"],
                "width": width,
                "height": height,
                "fps": fps,
                "codec": video["codec_name"],
                "pixel_format": video["pix_fmt"],
                "head_handle_seconds": float(edit_handles["head_seconds"]),
                "tail_handle_seconds": float(edit_handles["tail_seconds"]),
                "app_before": segment.get("app_before"),
                "app_after": segment.get("app_after"),
                "entry_frame": segment.get("entry_frame"),
                "exit_frame": segment.get("exit_frame"),
                "checkpoints": {
                    "33_percent": image_record(early),
                    "66_percent": image_record(middle),
                    "end": image_record(still),
                },
            }
        )
    contact_sheet = ROOT / "dist" / "contact-sheet.jpg"
    review_contact_sheet = ROOT / "dist" / "review-contact-sheet.jpg"
    make_contact_sheet(stills, contact_sheet)
    make_contact_sheet(review_stills, review_contact_sheet)
    review_reel = ROOT / "dist" / "animation-review-reel.mp4"
    concat_review(ordered_clips, review_reel)
    review_metadata = probe(review_reel)
    review_video = next(
        stream for stream in review_metadata["streams"] if stream["codec_type"] == "video"
    )
    assert not [stream for stream in review_metadata["streams"] if stream["codec_type"] == "audio"]
    assert (review_video["width"], review_video["height"]) == expected[:2]
    assert review_video["codec_name"] == "h264"
    assert review_video["pix_fmt"] == "yuv420p"
    assert review_video["r_frame_rate"] == expected[2]
    assert review_video["avg_frame_rate"] == expected[2]
    review_duration = float(review_metadata["format"]["duration"])
    assert abs(review_duration - sum(record["duration_seconds"] for record in records)) <= 0.2
    output = {
        "story_version": story.get("schema_version", "1.0"),
        "film_id": story.get("film_id"),
        "profile": args.profile,
        "story_runtime_seconds": story.get("runtime_seconds"),
        "target_animation_runtime_seconds": sum(
            float(segment["duration_seconds"]) for segment in segments
        ),
        "rendered_animation_runtime_seconds": sum(record["duration_seconds"] for record in records),
        "edit_handles": edit_handles,
        "render_source_hashes": render_source_hashes(story, segments),
        "clips": records,
        "review_reel": {
            "path": "dist/animation-review-reel.mp4",
            "duration_seconds": review_duration,
            "frame_count": int(
                review_video.get("nb_frames")
                or round(review_duration * int(expected[2].split("/")[0]))
            ),
            "sha256": sha256(review_reel),
        },
        "contact_sheet": image_record(contact_sheet),
        "review_contact_sheet": image_record(review_contact_sheet),
    }
    (ROOT / "dist" / "edit-manifest.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
