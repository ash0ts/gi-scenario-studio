"""Scene 10: keep one claim visible while its evidence path unfolds."""

from __future__ import annotations

from itertools import pairwise

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Arrow,
    Circumscribe,
    Create,
    FadeIn,
    FadeOut,
    Indicate,
    LaggedStart,
    Line,
    RoundedRectangle,
    SurroundingRectangle,
    Transform,
    VGroup,
)

from gi_story.claims import load_claims
from gi_story.components import panel, wrapped_text
from gi_story.theme import (
    CAUTION_OCHRE,
    CAUTION_WASH,
    CHANNEL_TEAL,
    FLOOD_RED,
    FLOOD_WASH,
    INK_SOFT,
    SURVEY_PAPER,
    WATERSHED_INK,
    BaseFilmScene,
    body,
    heading,
    mono,
)


def _step(number: str, label: str, detail: str, *, accent=CHANNEL_TEAL) -> VGroup:
    shell = panel(2.75, 1.18, fill=SURVEY_PAPER, stroke=accent)
    index = mono(number, size=20, color=accent)
    title = wrapped_text(
        label,
        max_width=2.25,
        max_height=0.34,
        role="heading",
        size=24,
        min_size=22,
        color=WATERSHED_INK,
    )
    note = wrapped_text(
        detail,
        max_width=2.25,
        max_height=0.34,
        size=20,
        min_size=20,
        color=INK_SOFT,
    )
    copy = VGroup(index, title, note).arrange(DOWN, aligned_edge=LEFT, buff=0.04)
    copy.move_to(shell).align_to(shell, LEFT).shift(RIGHT * 0.2)
    return VGroup(shell, copy)


class Scene10EvidenceChain(BaseFilmScene):
    """Trace ALT-017's worst peak increase without ever losing the claim itself."""

    scene_id = "10-evidence-chain"
    target_duration = 60.0

    def construct(self) -> None:
        claims = load_claims()
        candidate = claims.teaching_by_id["ALT-017"]
        run = max(candidate.peak_increases, key=lambda item: item.delta)

        title = self.title_block("Evidence", "Can this number survive a question?")
        badge = self.limitation_badge("RETAINED DIRECT-RUN EVIDENCE")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=1.0)
        self.play_for(FadeIn(badge), seconds=0.6)
        self.hold(2.4)

        claim_shell = RoundedRectangle(
            width=4.35,
            height=2.35,
            corner_radius=0.16,
            stroke_color=FLOOD_RED,
            stroke_width=4,
            fill_color=FLOOD_WASH,
            fill_opacity=1,
        )
        claim_shell.move_to(LEFT * 3.9 + DOWN * 0.05)
        claim_label = mono("ALT-017 · LONG EVENT", size=23, color=FLOOD_RED)
        claim_value = heading(f"+{run.delta:.2f} cfs", size=66, color=FLOOD_RED)
        claim_note = body("OUT1 peak change", size=27, color=INK_SOFT)
        claim_copy = VGroup(claim_label, claim_value, claim_note).arrange(DOWN, buff=0.1)
        claim_copy.move_to(claim_shell)
        self.play_for(FadeIn(claim_shell, shift=UP * 0.1), FadeIn(claim_copy), seconds=2.0)
        self.hold(3.2)

        question = heading("Compared with what?", size=43, color=WATERSHED_INK)
        question.move_to(RIGHT * 2.15 + UP * 0.95)
        comparison = VGroup(
            VGroup(
                body("matching baseline", size=24),
                mono(f"{run.baseline_peak:.2f} cfs", size=38),
            ).arrange(DOWN, buff=0.06),
            mono("→", size=46, color=CHANNEL_TEAL),
            VGroup(
                body(candidate.id, size=24),
                mono(f"{run.alternative_peak:.2f} cfs", size=38, color=FLOOD_RED),
            ).arrange(DOWN, buff=0.06),
        ).arrange(RIGHT, buff=0.36)
        comparison.move_to(RIGHT * 2.15 + DOWN * 0.15)
        storm = mono(run.name, size=23, color=CAUTION_OCHRE)
        storm.next_to(comparison, DOWN, buff=0.28)
        self.play_for(FadeIn(question, shift=LEFT * 0.1), seconds=1.4)
        self.play_for(FadeIn(comparison), seconds=2.2)
        self.play_for(FadeIn(storm), seconds=1.2)
        self.hold(3.0)

        # Keep the claim anchored while the comparison becomes the first evidence layer.
        claim_group = VGroup(claim_shell, claim_copy)
        token_shell = RoundedRectangle(
            width=3.25,
            height=1.02,
            corner_radius=0.13,
            stroke_color=FLOOD_RED,
            stroke_width=3,
            fill_color=FLOOD_WASH,
            fill_opacity=1,
        )
        token_shell.move_to(LEFT * 4.75 + UP * 1.3)
        token_copy = VGroup(
            mono("ALT-017 · LONG", size=18, color=FLOOD_RED),
            heading(f"+{run.delta:.2f} cfs", size=38, color=FLOOD_RED),
        ).arrange(DOWN, buff=0.04)
        token_copy.move_to(token_shell)
        self.play_for(
            Transform(claim_shell, token_shell),
            Transform(claim_copy, token_copy),
            FadeOut(question, comparison, storm),
            seconds=2.2,
        )

        # The bottom row is laid out in reverse so the numbered path snakes
        # continuously: 01 → 02 → 03 → 04, then down to 05 and back left to 08.
        steps = VGroup(
            _step("01", "CHANGE", "[0, 0, 1, 6]"),
            _step("02", "INPUT", "materialized INP"),
            _step("03", "SOLVE", f"SWMM {claims.solver_version}", accent=CAUTION_OCHRE),
            _step("04", "FILES", "RPT + OUT"),
            _step("08", "BOUND", "synthetic basis", accent=CAUTION_OCHRE),
            _step("07", "CLASSIFY", "QA accepted"),
            _step("06", "EXTRACT", "OUT1 peak", accent=FLOOD_RED),
            _step("05", "IDENTITY", "SHA-256 matched"),
        ).arrange_in_grid(rows=2, cols=4, buff=(0.48, 0.56))
        # Keep the surrounding evidence-path outline outside the production
        # edge-exclusion band once the four wide cards are enclosed.
        steps.move_to(RIGHT * 0.30 + DOWN * 0.95)
        links = VGroup()
        ordered_indices = (0, 1, 2, 3, 7, 6, 5, 4)
        for left_index, right_index in pairwise(ordered_indices):
            start = (
                steps[left_index].get_right() if left_index < 4 else steps[left_index].get_left()
            )
            end = (
                steps[right_index].get_left() if right_index < 4 else steps[right_index].get_right()
            )
            if left_index == 3:
                start = steps[3].get_bottom()
                end = steps[7].get_top()
            links.add(
                Arrow(
                    start,
                    end,
                    buff=0.04,
                    color=CHANNEL_TEAL,
                    stroke_width=4,
                    max_tip_length_to_length_ratio=0.32,
                )
            )

        claim_link = Arrow(
            claim_group.get_bottom(),
            steps[0].get_top(),
            buff=0.06,
            color=CHANNEL_TEAL,
            stroke_width=4,
            max_tip_length_to_length_ratio=0.22,
        )

        self.play_for(
            LaggedStart(*(FadeIn(step, shift=RIGHT * 0.08) for step in steps[:4]), lag_ratio=0.18),
            seconds=3.6,
        )
        self.play_for(
            Create(claim_link),
            LaggedStart(*(Create(link) for link in links[:3]), lag_ratio=0.2),
            seconds=2.6,
        )
        self.hold(1.7)
        bottom_steps = (steps[7], steps[6], steps[5], steps[4])
        self.play_for(
            LaggedStart(
                *(FadeIn(step, shift=LEFT * 0.08) for step in bottom_steps),
                lag_ratio=0.18,
            ),
            seconds=3.6,
        )
        self.play_for(
            LaggedStart(*(Create(link) for link in links[3:]), lag_ratio=0.2),
            seconds=2.6,
        )
        # Let the viewer's eye traverse the completed causal chain instead of
        # holding on a dense static grid. The order follows the visible snake.
        self.play_for(
            LaggedStart(
                *(
                    Circumscribe(
                        steps[index],
                        color=CHANNEL_TEAL,
                        buff=0.04,
                        stroke_width=5,
                    )
                    for index in ordered_indices
                ),
                lag_ratio=0.14,
            ),
            seconds=2.7,
        )

        teal_path = SurroundingRectangle(
            VGroup(steps, links),
            color=CHANNEL_TEAL,
            buff=0.16,
            corner_radius=0.12,
            stroke_width=4,
        )
        path_label = VGroup(
            mono("RETRACEABLE", size=24, color=CHANNEL_TEAL),
            body("from declared change to extracted claim", size=28),
        ).arrange(DOWN, buff=0.04)
        path_label.to_edge(DOWN, buff=0.5)
        self.play_for(Create(teal_path), seconds=1.6)
        self.play_for(FadeIn(path_label), seconds=1.1)
        self.hold(2.5)

        limitation = VGroup(
            VGroup(
                mono("ESTABLISHES", size=21, color=CHANNEL_TEAL),
                body("what ran · what changed · how the metric formed", size=27),
            ).arrange(DOWN, buff=0.05),
            VGroup(
                mono("DOES NOT ESTABLISH", size=21, color=CAUTION_OCHRE),
                body("field truth · storm authority · constructability", size=27),
            ).arrange(DOWN, buff=0.05),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.28)
        limitation.move_to(RIGHT * 1.4 + DOWN * 0.25)
        # Do not crossfade the limitation copy over the dense evidence chain:
        # both states contain text in the same region, so a simultaneous fade
        # creates an unreadable collision. Preserve the 1.8-second handoff as
        # two clean beats instead.
        self.play_for(
            FadeOut(steps, links, claim_link, teal_path, path_label),
            seconds=0.8,
        )
        self.play_for(FadeIn(limitation, shift=UP * 0.08), seconds=1.0)
        self.hold(3.0)

        # Rotate the same evidence into the application's rail proportions.
        rail = panel(4.15, 4.55, fill=CAUTION_WASH, stroke=CHANNEL_TEAL)
        rail.move_to(RIGHT * 3.75 + DOWN * 0.3)
        rail_title = mono("EVIDENCE RAIL", size=25, color=CHANNEL_TEAL)
        rail_title.move_to(rail.get_top() + DOWN * 0.36).align_to(rail, LEFT).shift(RIGHT * 0.28)
        rail_rows = VGroup(
            body("Long event", size=27, color=WATERSHED_INK),
            mono(
                f"{run.baseline_peak:.2f} → {run.alternative_peak:.2f} cfs",
                size=27,
                color=FLOOD_RED,
            ),
            body("Added 1 rain garden + 6 barrels", size=22),
            body("Input · report · output kept", size=23),
            body("Digital fingerprints match", size=23),
            body("Run check accepted", size=23, color=CHANNEL_TEAL),
            body("Synthetic limitation", size=25, color=CAUTION_OCHRE),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.18)
        rail_rows.move_to(rail).align_to(rail, LEFT).shift(RIGHT * 0.28 + DOWN * 0.14)
        self.play_for(
            FadeOut(limitation),
            claim_group.animate.move_to(LEFT * 3.95),
            seconds=1.6,
        )
        connector = Line(
            claim_group.get_right(), rail.get_left(), color=CHANNEL_TEAL, stroke_width=5
        )
        self.play_for(Create(connector), FadeIn(rail), FadeIn(rail_title), seconds=1.2)
        self.play_for(
            LaggedStart(*(FadeIn(row, shift=RIGHT * 0.06) for row in rail_rows), lag_ratio=0.12),
            seconds=3.4,
        )
        self.hold(1.2)
        self.play_for(Indicate(rail_rows[1], scale_factor=1.02), seconds=0.9)
        self.hold(0.6)

        conclusion = VGroup(
            heading("PROVENANCE KEEPS THE CLAIM", size=36),
            heading("INSIDE ITS ACTUAL BOUNDARY", size=36, color=CHANNEL_TEAL),
        ).arrange(DOWN, buff=0.04)
        conclusion.to_edge(DOWN, buff=0.50)
        self.play_for(FadeIn(conclusion, shift=UP * 0.08), seconds=1.4)
        self.hold(0.2)
        # Let the closing thought land in two visual beats: first the exact
        # claim, then the boundary that keeps the claim honest.  This avoids a
        # dead final slide while preserving a clean edit handle.
        self.play_for(Indicate(claim_group, color=FLOOD_RED, scale_factor=1.04), seconds=0.7)
        self.hold(0.2)
        self.play_for(
            Indicate(conclusion[1], color=CHANNEL_TEAL, scale_factor=1.04),
            seconds=0.7,
        )
        self.hold(0.8)
        self.finish_timing()
