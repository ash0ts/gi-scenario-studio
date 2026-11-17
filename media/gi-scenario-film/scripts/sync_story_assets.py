"""Regenerate scripts and caption tracks from the authoritative story manifest.

The manifest owns narration and cue timing.  This command deliberately keeps
performer copy, editor-facing narration, per-segment SRTs, and the combined SRT
as projections so wording cannot drift between the film and recording script.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORY = ROOT / "story"
MANIFEST = STORY / "story-manifest.json"


def _timecode(seconds: float, *, milliseconds: bool = False) -> str:
    whole = int(seconds)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if not milliseconds:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    millis = round((seconds - whole) * 1000)
    if millis == 1000:
        whole += 1
        hours, remainder = divmod(whole, 3600)
        minutes, secs = divmod(remainder, 60)
        millis = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _segment_title(segment: dict) -> str:
    if segment["kind"] == "application":
        return f"App {str(segment['screen']).replace('_', ' ').title()}"
    return str(segment["id"]).replace("_", " ").title()


def _srt(cues: list[dict], *, offset: float = 0.0) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            "\n".join(
                (
                    str(index),
                    (
                        f"{_timecode(offset + float(cue['start']), milliseconds=True)} --> "
                        f"{_timecode(offset + float(cue['end']), milliseconds=True)}"
                    ),
                    str(cue["text"]),
                )
            )
        )
    return "\n\n".join(blocks) + "\n"


def _caption_chunks(narration: str) -> list[str]:
    """Split narration into readable two-line cues without changing a word."""

    words = narration.split()
    chunks: list[str] = []
    cursor = 0
    while cursor < len(words):
        end = min(cursor + 15, len(words))
        while end > cursor + 1:
            candidate = " ".join(words[cursor:end])
            wrapped = textwrap.wrap(
                candidate,
                width=48,
                break_long_words=False,
                break_on_hyphens=False,
            )
            if len(wrapped) <= 2 and max(map(len, wrapped)) <= 54:
                break
            end -= 1
        candidate = " ".join(words[cursor:end])
        wrapped = textwrap.wrap(
            candidate,
            width=48,
            break_long_words=False,
            break_on_hyphens=False,
        )
        chunks.append("\n".join(wrapped))
        cursor = end
    return chunks


def _reflow_cues(segment: dict) -> list[dict]:
    chunks = _caption_chunks(str(segment["narration"]))
    start = 0.5
    finish = float(segment["duration_seconds"]) - 2.0
    weights = [max(len(chunk.replace("\n", " ")), 1) for chunk in chunks]
    total_weight = sum(weights)
    cursor = start
    cues = []
    for index, (chunk, weight) in enumerate(zip(chunks, weights, strict=True)):
        end = (
            finish
            if index == len(chunks) - 1
            else cursor + (finish - start) * weight / total_weight
        )
        cues.append({"start": round(cursor, 3), "end": round(end, 3), "text": chunk})
        cursor = end
    return cues


def _write_captions(story: dict) -> None:
    combined: list[dict] = []
    for segment in story["segments"]:
        cues = segment["caption_cues"]
        path = STORY / segment["caption_file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_srt(cues), encoding="utf-8")
        for cue in cues:
            combined.append(
                {
                    "start": float(segment["start_seconds"]) + float(cue["start"]),
                    "end": float(segment["start_seconds"]) + float(cue["end"]),
                    "text": cue["text"],
                }
            )
    (STORY / "combined.srt").write_text(_srt(combined), encoding="utf-8")


def _write_performer(story: dict) -> None:
    lines = [
        "# GI Scenario Studio — performer script",
        "",
        f"**Target runtime:** {story['runtime_timecode'][3:]}",
        "**Audience:** a smart first-time viewer who may know nothing about drainage modeling, SWMM, or engineering search.",
        "**Delivery:** warm, curious, and concrete. Explain the human problem before naming the technical idea.",
        "**Pace:** conversational. Pause after questions, comparisons, and short conclusion lines; let the visuals answer with you.",
        "",
        "Read only the narration. Headings and bracketed cues are not spoken.",
        "",
    ]
    for segment in story["segments"]:
        start = float(segment["start_seconds"])
        end = start + float(segment["duration_seconds"])
        lines.extend(
            (
                f"## {_timecode(start)}–{_timecode(end)} · {_segment_title(segment)}",
                "",
                str(segment["narration"]),
                "",
            )
        )
    (STORY / "performer-script.md").write_text("\n".join(lines), encoding="utf-8")


def _write_integrated(story: dict) -> None:
    lines = [
        "# GI Scenario Studio — integrated recording script",
        "",
        f"**Runtime:** {story['runtime_timecode']}",
        "",
        "The story assumes no SWMM or hydraulic-modeling background. Each animation makes one idea intuitive; the application then shows where that idea appears in the evidence workflow.",
        "",
    ]
    for segment in story["segments"]:
        start = float(segment["start_seconds"])
        end = start + float(segment["duration_seconds"])
        lines.extend(
            (
                f"## {_timecode(start)}–{_timecode(end)} · {_segment_title(segment)}",
                "",
            )
        )
        if segment["kind"] == "application":
            lines.append("### Application actions")
            lines.append("")
            for action in segment.get("actions", []):
                lines.append(f"- {action}")
            lines.append("")
        else:
            lines.extend(
                (
                    "### Animation handoff",
                    "",
                    f"- Entry: {segment.get('entry_frame', 'See story manifest.')}",
                    f"- Exit: {segment.get('exit_frame', 'See story manifest.')}",
                    "",
                )
            )
        lines.extend(("### Narration", "", f"> {segment['narration']}", ""))
    (STORY / "integrated-recording-script.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reflow-animation-captions",
        action="store_true",
        help="Regenerate animation caption cues after approved narration changes.",
    )
    parser.add_argument(
        "--reflow-all-captions",
        action="store_true",
        help="Regenerate every caption cue after approved film-wide narration changes.",
    )
    args = parser.parse_args()
    story = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.reflow_animation_captions or args.reflow_all_captions:
        for segment in story["segments"]:
            if args.reflow_all_captions or segment["kind"] == "animation":
                segment["caption_cues"] = _reflow_cues(segment)
        MANIFEST.write_text(json.dumps(story, indent=2) + "\n", encoding="utf-8")
    _write_captions(story)
    _write_performer(story)
    _write_integrated(story)
    print("Synchronized captions and recording scripts from story-manifest.json")


if __name__ == "__main__":
    main()
