from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

FILM_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = FILM_ROOT / "story" / "story-manifest.json"
CLAIM_LEDGER = FILM_ROOT / "story" / "claim-ledger.md"
REPOSITORY_ROOT = FILM_ROOT.parents[1]


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _normalize(text: str) -> str:
    return " ".join(text.replace("\n", " ").split())


def _srt_seconds(value: str) -> float:
    hours, minutes, remainder = value.split(":")
    seconds, milliseconds = remainder.split(",")
    return round(
        int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000,
        3,
    )


def _parse_srt(path: Path) -> list[tuple[float, float, str]]:
    parsed = []
    for block in path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = block.splitlines()
        assert lines[0].isdigit()
        start, end = lines[1].split(" --> ")
        parsed.append((_srt_seconds(start), _srt_seconds(end), "\n".join(lines[2:])))
    return parsed


def _performer_narrations() -> list[str]:
    lines = (FILM_ROOT / "story" / "performer-script.md").read_text(encoding="utf-8").splitlines()
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("## "):
            current = []
            sections.append(current)
        elif current is not None and line.strip() and not line.startswith("["):
            current.append(line)
    return [_normalize(" ".join(section)) for section in sections]


def _integrated_narrations() -> list[str]:
    lines = (
        (FILM_ROOT / "story" / "integrated-recording-script.md")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    narrations: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index] != "### Narration":
            index += 1
            continue
        index += 1
        quoted: list[str] = []
        while index < len(lines) and not lines[index].startswith("### "):
            if lines[index].startswith(">"):
                quoted.append(lines[index].removeprefix("> ").removeprefix(">"))
            index += 1
        narrations.append(_normalize(" ".join(quoted)))
    return narrations


def test_timeline_is_contiguous_and_matches_27_50() -> None:
    manifest = _manifest()
    segments = manifest["segments"]
    cursor = 0
    for segment in segments:
        assert segment["start_seconds"] == cursor
        assert segment["duration_seconds"] > 0
        cursor += segment["duration_seconds"]
    assert len(segments) == 23
    assert cursor == manifest["runtime_seconds"] == 1670
    assert manifest["runtime_timecode"] == "00:27:50"


def test_animation_and_application_runtime_split() -> None:
    segments = _manifest()["segments"]
    animations = [segment for segment in segments if segment["kind"] == "animation"]
    applications = [segment for segment in segments if segment["kind"] == "application"]
    assert len(animations) == 11
    assert len(applications) == 12
    assert sum(segment["duration_seconds"] for segment in animations) == 590
    assert sum(segment["duration_seconds"] for segment in applications) == 1080
    assert len({segment["output_file"] for segment in animations}) == 11


def test_manifest_pins_positive_edit_handles_and_claim_sources() -> None:
    manifest = _manifest()
    assert manifest["edit_handles"] == {"head_seconds": 0.5, "tail_seconds": 0.6}
    expected_paths = {
        "guided_fixture": "apps/desktop/src/data/guidedFixture.json",
        "exhaustive_benchmark": "benchmarks/exhaustive_exact/dataset.json",
    }
    assert set(manifest["claim_sources"]) == set(expected_paths)
    for source_id, relative_path in expected_paths.items():
        source = REPOSITORY_ROOT / relative_path
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert manifest["claim_sources"][source_id] == {
            "path": relative_path,
            "sha256": digest,
        }


def test_every_animation_points_to_a_real_scene_class() -> None:
    for segment in _manifest()["segments"]:
        if segment["kind"] != "animation":
            continue
        source = FILM_ROOT / segment["source_file"]
        assert source.is_file(), source
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        assert segment["scene_class"] in classes
        assert segment["output_file"] == f"{segment['id']}.mp4"


def test_scenes_use_pango_text_without_latex_objects() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (FILM_ROOT / "src" / "gi_story").rglob("*.py")
    )
    assert not re.search(r"\b(?:MathTex|Tex|TexTemplate|SingleStringMathTex)\b", source)


def test_caption_cues_are_word_exact_and_bounded() -> None:
    manifest = _manifest()
    total_cues = 0
    for segment in manifest["segments"]:
        cues = segment["caption_cues"]
        total_cues += len(cues)
        assert cues
        previous_end = 0.0
        cue_text = []
        for cue in cues:
            assert 0 <= cue["start"] < cue["end"] <= segment["duration_seconds"]
            assert cue["start"] >= previous_end
            assert max(len(line) for line in cue["text"].splitlines()) <= 54
            previous_end = cue["end"]
            cue_text.append(cue["text"])
        assert _normalize(" ".join(cue_text)) == _normalize(segment["narration"])
        caption_file = FILM_ROOT / "story" / segment["caption_file"]
        assert caption_file.is_file()
        assert caption_file.read_text(encoding="utf-8").count(" --> ") == len(cues)
    assert total_cues >= len(manifest["segments"])
    assert (FILM_ROOT / "story" / "combined.srt").read_text(encoding="utf-8").count(
        " --> "
    ) == total_cues


def test_scripts_and_srt_tracks_are_exact_manifest_projections() -> None:
    segments = _manifest()["segments"]
    expected_narrations = [_normalize(segment["narration"]) for segment in segments]
    assert _performer_narrations() == expected_narrations
    assert _integrated_narrations() == expected_narrations

    combined_expected = []
    for segment in segments:
        segment_expected = [
            (round(cue["start"], 3), round(cue["end"], 3), cue["text"])
            for cue in segment["caption_cues"]
        ]
        caption_path = FILM_ROOT / "story" / segment["caption_file"]
        assert _parse_srt(caption_path) == segment_expected
        combined_expected.extend(
            (
                round(segment["start_seconds"] + cue["start"], 3),
                round(segment["start_seconds"] + cue["end"], 3),
                cue["text"],
            )
            for cue in segment["caption_cues"]
        )
    assert _parse_srt(FILM_ROOT / "story" / "combined.srt") == combined_expected


def test_every_manifest_claim_is_grounded_in_the_ledger() -> None:
    ledger = CLAIM_LEDGER.read_text(encoding="utf-8")
    grounded = set(re.findall(r"`(C-[A-Z0-9-]+)`", ledger))
    referenced = {
        claim_id for segment in _manifest()["segments"] for claim_id in segment.get("claim_ids", [])
    }
    assert referenced
    assert referenced <= grounded


def test_critical_narration_matches_the_retention_and_job_boundaries() -> None:
    segments = {segment["id"]: segment for segment in _manifest()["segments"]}
    scene_07 = segments["07_bounded_search_space"]
    scene_08 = segments["08_job_matrix"]
    opening = segments["01_pareto_trap"]["narration"]
    closing = segments["app_closing"]["narration"]

    assert "C-BENCHMARK-2800" in scene_07["claim_ids"]
    assert "2,800 SWMM runs" in scene_07["narration"]
    assert "884 runs in all" in scene_07["narration"]
    assert (
        "Each square keeps the result, the numerical check, and digital fingerprints of its files."
        in scene_08["narration"]
    )
    assert "complete input, report, and output files" in scene_08["narration"]
    assert "20 full evidence sets" in scene_08["narration"]
    assert "3.14 to 3.21 cubic feet per second" in opening
    assert "A second storm rises too." in opening
    assert "fails one storm" not in closing

    all_narration = " ".join(segment["narration"] for segment in segments.values()).lower()
    assert "all 884 records retain complete files" not in all_narration
    assert "all eight hundred eighty-four records retain complete files" not in all_narration


def test_language_keeps_search_and_engineering_authority_separate() -> None:
    narration = " ".join(segment["narration"] for segment in _manifest()["segments"])
    assert "That choice is a question, not an answer." in narration
    assert "Only a direct SWMM run can reveal the modeled hydraulics." in narration
    assert "Search decides where to look. SWMM supplies what we see." in narration
    assert "candidate to carry forward for site and feasibility review." in narration.lower()
    assert "ALT-207 is the best" not in narration
    assert "ALT-207 is optimal" not in narration
    assert "ALT-207 is approved" not in narration


def test_beginner_terms_arrive_after_the_plain_language_idea() -> None:
    segments = {segment["id"]: segment for segment in _manifest()["segments"]}
    opening = " ".join([segments["01_pareto_trap"]["narration"], segments["app_home"]["narration"]])

    assert "pareto" not in opening.lower()
    assert "nondominated" not in opening.lower()
    assert "W&B" not in segments["app_home"]["narration"]
    assert "Restricted" not in segments["app_home"]["narration"]
    assert "Weave" not in segments["app_home"]["narration"]
    assert "engineers call that area a catchment" in segments["02_rainfall_to_runoff"]["narration"]
    assert (
        "simply the point where water leaves the drainage model"
        in segments["02_rainfall_to_runoff"]["narration"]
    )
    assert "the EPA's Storm Water Management Model" in segments["03_swmm_represents"]["narration"]
    assert (
        "Engineers call a point like that nondominated"
        in segments["09_hidden_dimension"]["narration"]
    )
    assert "That chain is called provenance" in segments["10_evidence_chain"]["narration"]
