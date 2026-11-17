"""Scene 05: separate solver-run QA from engineering model fitness."""

from __future__ import annotations

from math import pi

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Arrow,
    Circle,
    Create,
    DashedLine,
    Dot,
    FadeIn,
    FadeOut,
    Indicate,
    Line,
    RoundedRectangle,
    VGroup,
)

from gi_story.claims import load_claims
from gi_story.components import status_card
from gi_story.theme import (
    CAUTION_OCHRE,
    CHANNEL_TEAL,
    FLOOD_RED,
    INK_SOFT,
    LINE_GRAY,
    MUTED,
    SURVEY_PAPER,
    WATERSHED_INK,
    BaseFilmScene,
    body,
    heading,
    mono,
)


def _evidence_step(label: str, *, width: float = 3.95) -> VGroup:
    """A fixed-size run-evidence milestone; no text group is scaled."""

    shell = RoundedRectangle(
        width=width,
        height=0.58,
        corner_radius=0.09,
        stroke_color=CHANNEL_TEAL,
        stroke_width=2,
        fill_color=SURVEY_PAPER,
        fill_opacity=1,
    )
    marker = Circle(
        radius=0.11,
        stroke_color=CHANNEL_TEAL,
        stroke_width=2,
        fill_color=CHANNEL_TEAL,
        fill_opacity=1,
    )
    marker.move_to(shell.get_left() + RIGHT * 0.25)
    copy = body(label, size=21, color=WATERSHED_INK)
    copy.move_to(shell).align_to(shell, LEFT).shift(RIGHT * 0.48)
    return VGroup(shell, marker, copy)


def _unmet_site_step(label: str) -> VGroup:
    """A hollow site-evidence checkpoint with a redundant not-provided mark."""

    ring = Circle(
        radius=0.16,
        stroke_color=CAUTION_OCHRE,
        stroke_width=3,
        fill_color=SURVEY_PAPER,
        fill_opacity=1,
    )
    cross = VGroup(
        Line(LEFT * 0.09, RIGHT * 0.09, color=CAUTION_OCHRE, stroke_width=3),
        Line(DOWN * 0.09, UP * 0.09, color=CAUTION_OCHRE, stroke_width=3),
    ).rotate(pi / 4)
    cross.move_to(ring)
    copy = body(label, size=20, color=INK_SOFT)
    copy.next_to(ring, DOWN, buff=0.16)
    return VGroup(ring, cross, copy)


class Scene05QaVsFitness(BaseFilmScene):
    """Move Cedar Trace only along the execution axis, never the fitness axis."""

    scene_id = "05-qa-vs-fitness"
    target_duration = 40.0

    def construct(self) -> None:
        claims = load_claims()
        within_policy = claims.largest_baseline_continuity_error <= claims.qa_warning_threshold

        title = self.title_block(
            "Two independent axes",
            "A clean run is not a fit model",
            title_size=54,
        )
        badge = self.limitation_badge("SYNTHETIC MODEL · FITNESS NOT ASSESSED")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=0.8)
        self.play_for(FadeIn(badge), seconds=0.5)
        self.hold(1.2)

        origin = LEFT * 4.55 + DOWN * 2.05
        horizontal = Arrow(
            origin,
            RIGHT * 5.75 + DOWN * 2.05,
            buff=0,
            color=CAUTION_OCHRE,
            stroke_width=4,
            max_tip_length_to_length_ratio=0.025,
        )
        vertical = Arrow(
            origin,
            LEFT * 4.55 + UP * 1.62,
            buff=0,
            color=CHANNEL_TEAL,
            stroke_width=4,
            max_tip_length_to_length_ratio=0.04,
        )
        horizontal_label = mono("MODEL FITNESS / SITE EVIDENCE →", size=22, color=CAUTION_OCHRE)
        horizontal_label.next_to(horizontal, DOWN, buff=0.48).shift(RIGHT * 3.1)
        vertical_label = mono("EXECUTION QA →", size=21, color=CHANNEL_TEAL).rotate(pi / 2)
        vertical_label.next_to(vertical, LEFT, buff=0.25)
        origin_label = mono("CEDAR TRACE", size=20, color=WATERSHED_INK)
        origin_label.next_to(origin, UP, buff=0.15).shift(RIGHT * 0.86)
        self.play_for(Create(horizontal), Create(vertical), seconds=1.1)
        self.play_for(
            FadeIn(horizontal_label),
            FadeIn(vertical_label),
            FadeIn(origin_label),
            seconds=0.7,
        )
        self.hold(1.2)

        point = Dot(origin, radius=0.12, color=CHANNEL_TEAL)
        point_ring = Circle(radius=0.24, color=CHANNEL_TEAL, stroke_width=3).move_to(point)
        self.play_for(FadeIn(point), Create(point_ring), seconds=0.7)
        self.hold(0.8)

        step_specs = (
            ("Parser accepted", -1.35),
            ("Direct SWMM execution", -0.58),
            (
                (
                    f"Continuity {claims.largest_baseline_continuity_error:.3f}% ≤ "
                    f"{claims.qa_warning_threshold:g}%"
                ),
                0.19,
            ),
            ("Artifacts + SHA-256", 0.96),
        )
        run_steps = VGroup()
        trajectory = VGroup()
        previous = origin
        for label, y_coordinate in step_specs:
            target = LEFT * 4.55 + UP * y_coordinate
            segment = Line(previous, target, color=CHANNEL_TEAL, stroke_width=7)
            step = _evidence_step(label)
            step.move_to(LEFT * 2.32 + UP * y_coordinate)
            trajectory.add(segment)
            run_steps.add(step)
            self.play_for(Create(segment), seconds=0.55)
            self.play_for(
                point.animate.move_to(target),
                point_ring.animate.move_to(target),
                seconds=0.65,
            )
            self.play_for(FadeIn(step, shift=RIGHT * 0.1), seconds=0.5)
            self.play_for(Indicate(step, color=CHANNEL_TEAL, scale_factor=1.015), seconds=0.55)
            self.hold(1.6)
            previous = target

        policy_marker = mono(
            "WITHIN DECLARED RUN POLICY" if within_policy else "OUTSIDE RUN POLICY",
            size=21,
            color=CHANNEL_TEAL if within_policy else FLOOD_RED,
        )
        policy_marker.move_to(LEFT * 1.95 + UP * 1.43)
        self.play_for(FadeIn(policy_marker, shift=DOWN * 0.08), seconds=0.6)
        self.hold(0.8)

        site_positions = (-2.3, 0.05, 2.4, 4.75)
        site_labels = ("Survey", "Observed events", "Calibration", "Reference match")
        site_steps = VGroup()
        for x_coordinate, label in zip(site_positions, site_labels, strict=True):
            tick = Line(
                RIGHT * x_coordinate + DOWN * 2.18,
                RIGHT * x_coordinate + DOWN * 1.92,
                color=CAUTION_OCHRE,
                stroke_width=3,
            )
            step = _unmet_site_step(label)
            step.move_to(RIGHT * x_coordinate + DOWN * 2.04)
            step[-1].next_to(step[0], DOWN, buff=0.16)
            site_steps.add(VGroup(tick, step))
            self.play_for(Create(tick), FadeIn(step), seconds=0.65)
            self.play_for(Indicate(step[0], color=CAUTION_OCHRE), seconds=0.5)
            self.hold(0.55)

        possible_motion = DashedLine(
            point.get_center(),
            RIGHT * 4.95 + UP * 0.96,
            color=LINE_GRAY,
            stroke_width=3,
            dash_length=0.12,
        )
        missing_note = VGroup(
            mono("NO RIGHTWARD MOVE", size=22, color=CAUTION_OCHRE),
            body("Site evidence was not assessed for this fixture.", size=22, color=MUTED),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.1)
        missing_note.move_to(RIGHT * 2.55 + UP * 1.18)
        self.play_for(FadeOut(policy_marker), Create(possible_motion), seconds=0.7)
        self.play_for(FadeIn(missing_note, shift=LEFT * 0.1), seconds=0.7)
        self.play_for(Indicate(point_ring, color=CHANNEL_TEAL), seconds=0.7)
        self.hold(1.0)

        detail = VGroup(run_steps, site_steps, policy_marker, possible_motion, missing_note)
        self.play_for(FadeOut(detail), FadeOut(origin_label), seconds=0.7)
        qa_result = status_card(
            "Execution QA",
            "WITHIN POLICY" if within_policy else "OUTSIDE POLICY",
            status="pass" if within_policy else "fail",
            width=3.8,
        )
        fitness_result = status_card(
            "Model fitness",
            claims.fitness_disposition.replace("_", " ").upper(),
            status="caution",
            width=3.8,
        )
        qa_result.move_to(LEFT * 1.55 + UP * 0.52)
        fitness_result.move_to(RIGHT * 2.55 + UP * 0.52)
        self.play_for(FadeIn(qa_result, shift=UP * 0.1), seconds=0.7)
        self.play_for(FadeIn(fitness_result, shift=UP * 0.1), seconds=0.7)
        conclusion = heading("UP IS NOT RIGHT", size=42, color=WATERSHED_INK)
        conclusion.move_to(RIGHT * 0.6 + DOWN * 0.72)
        self.play_for(FadeIn(conclusion, shift=UP * 0.08), seconds=0.7)
        self.play_for(
            Indicate(qa_result, color=CHANNEL_TEAL, scale_factor=1.025),
            Indicate(fitness_result, color=CAUTION_OCHRE, scale_factor=1.025),
            seconds=0.8,
        )
        self.place_footer("Cedar Trace moved up the run-evidence axis — never to the right.")
        self.hold(1.1)
        self.finish_timing()
