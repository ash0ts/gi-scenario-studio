#!/usr/bin/env python3
"""Package and validate the real guided-application recordings used by the article."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MEDIA_SPEC = ROOT / "story" / "article-media.json"
SESSION_MANIFEST = ROOT / "dist" / "article-recording" / "session-markers.json"
OUTPUT_MANIFEST = ROOT / "dist" / "article-clips" / "manifest.json"
FIXTURE = ROOT.parents[1] / "apps" / "desktop" / "src" / "data" / "guidedFixture.json"
FPS = 30


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def _probe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    video_streams = [stream for stream in payload["streams"] if stream["codec_type"] == "video"]
    audio_streams = [stream for stream in payload["streams"] if stream["codec_type"] == "audio"]
    if len(video_streams) != 1 or audio_streams:
        raise ValueError(f"{path}: expected one silent video stream")
    stream = video_streams[0]
    frame_count = int(stream.get("nb_frames") or 0)
    if not frame_count:
        duration = float(stream.get("duration") or payload["format"]["duration"])
        frame_count = round(duration * FPS)
    return {
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": stream["r_frame_rate"],
        "codec": stream["codec_name"],
        "pixel_format": stream["pix_fmt"],
        "frame_count": frame_count,
        "duration_seconds": frame_count / FPS,
    }


def _validate_video(path: Path, expected_frames: int) -> dict[str, Any]:
    facts = _probe(path)
    expected = {
        "width": 1440,
        "height": 900,
        "fps": "30/1",
        "codec": "h264",
        "pixel_format": "yuv420p",
        "frame_count": expected_frames,
    }
    mismatches = {
        key: {"expected": value, "actual": facts[key]}
        for key, value in expected.items()
        if facts[key] != value
    }
    if mismatches:
        raise ValueError(f"{path}: invalid application clip: {mismatches}")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        cwd=ROOT,
        check=True,
    )
    return facts


def _validate_session(spec: dict[str, Any], session: dict[str, Any]) -> Path:
    if (
        session.get("preview_mode") != "interface-only"
        or session.get("live_solver_execution") is not False
    ):
        raise ValueError("article recordings must remain labelled as an interface-only replay")
    if session.get("viewport") != {
        "width": spec["capture"]["viewport_width"],
        "height": spec["capture"]["viewport_height"],
    }:
        raise ValueError("application recording viewport drifted from the media contract")
    if session.get("media_spec_sha256") != _sha256(MEDIA_SPEC):
        raise ValueError("application recording used a different article media contract")
    if session.get("source_fixture_sha256") != _sha256(FIXTURE):
        raise ValueError("application recording used a different guided fixture")
    markers = session.get("markers") or {}
    if list(markers) != [clip["marker"] for clip in spec["clips"]]:
        raise ValueError("application recording marker order drifted")
    master = ROOT / str(session["master"])
    if not master.is_file() or _sha256(master) != session.get("master_sha256"):
        raise ValueError("application master recording is missing or has changed")
    return master


def _render_clip(
    clip: dict[str, Any], marker: dict[str, Any], source: Path, source_sha256: str
) -> dict[str, Any]:
    start_seconds = float(marker["start_seconds"])
    end_seconds = float(marker["end_seconds"])
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError(f"{clip['key']}: invalid recording boundary")
    expected_frames = max(1, round((end_seconds - start_seconds) * FPS))
    output = ROOT / clip["output"]
    poster = ROOT / clip["poster"]
    output.parent.mkdir(parents=True, exist_ok=True)
    poster.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix(".tmp.mp4")
    temporary_poster = poster.with_suffix(".tmp.png")
    end_frame_time = start_seconds + expected_frames / FPS
    filter_graph = (
        f"trim=start={start_seconds:.6f}:end={end_frame_time:.6f},"
        # Browser video finalization can end a few source frames before the last
        # wall-clock marker. Pad generously, then cap with -frames:v below so
        # every declared clip still has one deterministic frame count.
        "setpts=PTS-STARTPTS,fps=30,tpad=stop_mode=clone:stop_duration=0.5,format=yuv420p"
    )
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            filter_graph,
            "-frames:v",
            str(expected_frames),
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-preset",
            "slow",
            "-crf",
            "18",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-fps_mode",
            "cfr",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            str(temporary_output),
        ]
    )
    temporary_output.replace(output)
    poster_frame = max(0, expected_frames - 45)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(output),
            "-vf",
            f"select=eq(n\\,{poster_frame})",
            "-frames:v",
            "1",
            str(temporary_poster),
        ]
    )
    temporary_poster.replace(poster)
    facts = _validate_video(output, expected_frames)
    return {
        **clip,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "source_sha256": source_sha256,
        "sha256": _sha256(output),
        "poster_sha256": _sha256(poster),
        **facts,
    }


def extract() -> dict[str, Any]:
    spec = _load_json(MEDIA_SPEC)
    session = _load_json(SESSION_MANIFEST)
    source = _validate_session(spec, session)
    source_sha256 = _sha256(source)
    rendered = [
        _render_clip(clip, session["markers"][clip["marker"]], source, source_sha256)
        for clip in spec["clips"]
    ]
    article = ROOT / spec["article"]
    payload = {
        "schema_version": spec["schema_version"],
        "media_kind": spec["media_kind"],
        "preview_mode": session["preview_mode"],
        "live_solver_execution": session["live_solver_execution"],
        "article": spec["article"],
        "article_sha256": _sha256(article),
        "media_spec_sha256": _sha256(MEDIA_SPEC),
        "session_manifest_sha256": _sha256(SESSION_MANIFEST),
        "master_sha256": source_sha256,
        "clip_count": len(rendered),
        "clips": rendered,
    }
    OUTPUT_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT_MANIFEST.with_suffix(".tmp.json")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT_MANIFEST)
    return payload


def check() -> dict[str, Any]:
    spec = _load_json(MEDIA_SPEC)
    session = _load_json(SESSION_MANIFEST)
    source = _validate_session(spec, session)
    manifest = _load_json(OUTPUT_MANIFEST)
    if manifest["article_sha256"] != _sha256(ROOT / spec["article"]):
        raise ValueError("application clips were generated from a different article revision")
    if manifest["media_spec_sha256"] != _sha256(MEDIA_SPEC):
        raise ValueError("application clips were generated from a different media specification")
    if manifest["session_manifest_sha256"] != _sha256(SESSION_MANIFEST):
        raise ValueError("application clips were generated from a different recording session")
    if manifest["master_sha256"] != _sha256(source):
        raise ValueError("application clips were generated from a different master video")
    if manifest["clip_count"] != len(spec["clips"]):
        raise ValueError("application clip count does not match the media specification")
    for expected, rendered in zip(spec["clips"], manifest["clips"], strict=True):
        if expected != {key: rendered[key] for key in expected}:
            raise ValueError(f"application clip metadata drifted: {expected['key']}")
        output = ROOT / rendered["output"]
        poster = ROOT / rendered["poster"]
        _validate_video(output, int(rendered["frame_count"]))
        if _sha256(output) != rendered["sha256"] or _sha256(poster) != rendered["poster_sha256"]:
            raise ValueError(f"application clip hash mismatch: {rendered['key']}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate existing app recordings")
    arguments = parser.parse_args()
    payload = check() if arguments.check else extract()
    print(json.dumps({"clip_count": payload["clip_count"], "manifest": str(OUTPUT_MANIFEST)}))


if __name__ == "__main__":
    main()
