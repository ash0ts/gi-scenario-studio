"""Geometry guards for reusable film cards."""

from __future__ import annotations

import pytest
from gi_story.components import (
    MIN_CARD_FONT_SIZE,
    TextFitError,
    assert_minimum_font_size,
    iter_text_mobjects,
    labeled_panel,
    status_card,
    wrapped_text,
)
from gi_story.theme import (
    LAYOUT_ZONES,
    LayoutViolation,
    assert_mobject_in_zone,
    assert_mobjects_separated,
    place_in_zone,
)
from manim import Rectangle


def _assert_copy_is_inside(box, copy) -> None:
    assert copy.get_left()[0] >= box.get_left()[0]
    assert copy.get_right()[0] <= box.get_right()[0]
    assert copy.get_bottom()[1] >= box.get_bottom()[1]
    assert copy.get_top()[1] <= box.get_top()[1]
    assert_minimum_font_size(copy)


@pytest.mark.parametrize(
    ("title", "detail"),
    [
        ("INTEGER MUTATION", "[0, 0, 1, 6] additional units"),
        ("MATERIALIZED INP", "sha256 927b87e15383…"),
        ("SWMM EXECUTION", "run_943bfb119139195dccd8"),
        ("REVEALED RESULT", "hydraulics · QA · hashes"),
    ],
)
def test_labeled_panel_copy_stays_inside_card(title: str, detail: str) -> None:
    card = labeled_panel(title, detail, width=3.35, height=1.1)
    box, _, copy = card

    _assert_copy_is_inside(box, copy)


@pytest.mark.parametrize("value", ["Δpeak +0.07 cfs", "Δpeak -0.82 cfs"])
def test_status_card_value_stays_inside_card(value: str) -> None:
    card = status_card("synthetic long-duration 2.5 in", value, status="fail", width=3.55)
    box, copy = card

    _assert_copy_is_inside(box, copy)


@pytest.mark.parametrize(
    ("title", "detail", "width", "height"),
    [
        (
            "ALT-207  [0, 3, 2, 6]",
            "6.42% · $190.25k · passes four teaching rules",
            8.0,
            1.65,
        ),
        ("COMPLETE BENCHMARK", "700 vectors × 4 storms = 2,800 jobs", 5.55, 1.65),
        ("NEW LOCAL RERUN", "execute all 884 with the expected binary", 5.25, 2.0),
        (
            "ALT-017 ADVERSE CHANGE",
            "Synthetic long-duration 2.5 in event: 3.14 → 3.21 cfs",
            6.85,
            1.4,
        ),
    ],
)
def test_longest_authored_panel_copy_is_measured_and_readable(
    title: str, detail: str, width: float, height: float
) -> None:
    card = labeled_panel(title, detail, width=width, height=height)
    box, _, copy = card

    _assert_copy_is_inside(box, copy)
    assert all(text.font_size >= MIN_CARD_FONT_SIZE for text in iter_text_mobjects(copy))


def test_wrapped_text_uses_multiple_final_size_text_lines() -> None:
    copy = wrapped_text(
        "The guided subset is deterministic, nonexhaustive, and not an adaptive-ML demonstration.",
        max_width=3.1,
        max_height=2.1,
        size=26,
        min_size=20,
    )

    lines = iter_text_mobjects(copy)
    assert len(lines) >= 2
    assert copy.width <= 3.1
    assert copy.height <= 2.1
    assert all(text.font_size >= 20 for text in lines)


def test_wrapped_text_fails_instead_of_creating_microcopy() -> None:
    with pytest.raises(TextFitError, match="hard minimum font size 20"):
        wrapped_text(
            "A complete engineering sentence cannot occupy an impossibly short region.",
            max_width=2.4,
            max_height=0.1,
            size=26,
            min_size=20,
        )


def test_named_zones_have_nonoverlapping_vertical_lanes() -> None:
    assert LAYOUT_ZONES["headline"].top < LAYOUT_ZONES["eyebrow"].bottom
    assert LAYOUT_ZONES["body"].top < LAYOUT_ZONES["headline"].bottom
    assert LAYOUT_ZONES["conclusion"].top < LAYOUT_ZONES["body"].bottom
    assert LAYOUT_ZONES["footer"].top < LAYOUT_ZONES["conclusion"].bottom


def test_zone_placement_and_bounds_fail_fast() -> None:
    card = Rectangle(width=3.0, height=1.0)
    place_in_zone(card, "body", anchor="top_left", padding=0.1)
    assert_mobject_in_zone(card, "body", padding=0.1)

    card.shift([20, 0, 0])
    with pytest.raises(LayoutViolation, match="leaves layout zone 'body'"):
        assert_mobject_in_zone(card, "body")


def test_peer_overlap_guard_reports_collisions() -> None:
    left = Rectangle(width=2, height=1).shift([-2, 0, 0])
    right = Rectangle(width=2, height=1).shift([2, 0, 0])
    assert_mobjects_separated(left, right, gap=0.2)

    right.move_to(left)
    with pytest.raises(LayoutViolation, match="rules overlaps gate"):
        assert_mobjects_separated(left, right, labels=("rules", "gate"))
