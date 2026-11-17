"""Scene 06: preserve every storm response beside the useful aggregate."""

from __future__ import annotations

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
    Line,
    Mobject,
    RoundedRectangle,
    SurroundingRectangle,
    TransformFromCopy,
    VGroup,
)

from gi_story.claims import StormRun, load_claims
from gi_story.components import labeled_panel
from gi_story.theme import (
    CAUTION_OCHRE,
    CHANNEL_TEAL,
    FLOOD_RED,
    FLOOD_WASH,
    INK_SOFT,
    RAIN_BLUE,
    SURVEY_PAPER,
    WATERSHED_INK,
    BaseFilmScene,
    body,
    heading,
    mono,
)


def _short_storm_name(run: StormRun) -> str:
    names = {
        "synthetic_balanced_1in": "Balanced",
        "synthetic_front_loaded_2in": "Front-loaded",
        "synthetic_long_2_5in": "Long event",
        "synthetic_stress_4in": "Stress",
    }
    return names[run.storm_id]


def _storm_comparison(
    run: StormRun,
    *,
    depth_inches: float,
    duration_hours: float,
) -> tuple[VGroup, Mobject, Mobject]:
    """Build one large comparison row and return its volume and delta labels."""

    passed = run.delta <= 0.0
    accent = CHANNEL_TEAL if passed else FLOOD_RED
    fill = SURVEY_PAPER if passed else FLOOD_WASH
    shell = RoundedRectangle(
        width=7.9,
        height=0.95,
        corner_radius=0.1,
        stroke_color=accent,
        stroke_width=2.5,
        fill_color=fill,
        fill_opacity=1,
    )
    accent_line = Line(
        shell.get_corner(UP + LEFT) + RIGHT * 0.12,
        shell.get_corner(DOWN + LEFT) + RIGHT * 0.12,
        color=accent,
        stroke_width=7,
    )
    name = heading(
        f"{_short_storm_name(run)} · {duration_hours:g} h",
        size=23,
        color=WATERSHED_INK,
    )
    name.move_to(shell.get_center() + UP * 0.18)
    name.align_to(shell, LEFT).shift(RIGHT * 0.34)
    volume = mono(
        f"{depth_inches:.1f} in · V {run.volume:.3f} Mgal",
        size=18,
        color=RAIN_BLUE,
    )
    volume.move_to(shell.get_center() + DOWN * 0.2)
    volume.align_to(shell, LEFT).shift(RIGHT * 0.34)
    comparison = mono(
        f"{run.baseline_peak:.2f} → {run.alternative_peak:.2f} cfs",
        size=23,
        color=WATERSHED_INK,
    )
    comparison.move_to(shell.get_center() + RIGHT * 1.1 + UP * 0.13)
    delta = mono(f"Δpeak {run.delta:+.2f} cfs", size=20, color=accent)
    delta.move_to(shell.get_center() + RIGHT * 1.1 + DOWN * 0.23)
    state = mono("PASS" if passed else "FAIL", size=20, color=accent)
    state.move_to(shell.get_right() + LEFT * 0.48)
    return VGroup(shell, accent_line, name, volume, comparison, delta, state), volume, delta


def _aggregate_card(volume: float, reduction_percent: float) -> tuple[VGroup, Mobject, Mobject]:
    """One measured aggregate whose authored copy is contained by its shell."""

    shell = RoundedRectangle(
        width=3.1,
        height=1.25,
        corner_radius=0.1,
        stroke_color=RAIN_BLUE,
        stroke_width=3,
        fill_color=SURVEY_PAPER,
        fill_opacity=1,
    )
    aggregate_value = mono(f"ΣV  {volume:.3f} Mgal", size=23, color=RAIN_BLUE)
    aggregate_value.move_to(shell.get_center() + UP * 0.13)
    reduction = mono(f"{reduction_percent:.2f}% reduction", size=20, color=CAUTION_OCHRE)
    reduction.move_to(shell.get_center() + DOWN * 0.25)
    return VGroup(shell, aggregate_value, reduction), aggregate_value, reduction


class Scene06FourResponses(BaseFilmScene):
    """Show that summing volumes must not erase an every-storm peak test."""

    scene_id = "06-four-responses"
    target_duration = 50.0

    def construct(self) -> None:
        claims = load_claims()
        candidate = claims.teaching_by_id["ALT-017"]
        worst_run = max(candidate.peak_increases, key=lambda run: run.delta)

        title = self.title_block(
            "One deployment vector",
            "One design is not one outcome",
            title_size=54,
        )
        badge = self.limitation_badge("FOUR SELECTED SYNTHETIC STORMS")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=0.8)
        self.play_for(FadeIn(badge), seconds=0.5)
        self.hold(1.2)

        vector_card = labeled_panel(
            candidate.id,
            "[BIO, PP, RG, RB]\n[0, 0, 1, 6]",
            width=3.1,
            height=1.5,
            accent=RAIN_BLUE,
        )
        vector_card.move_to(LEFT * 4.95 + UP * 0.62)
        vector_note = VGroup(
            mono("ONE QUESTION", size=21, color=RAIN_BLUE),
            body("same deployment", size=21, color=INK_SOFT),
        ).arrange(DOWN, buff=0.08)
        vector_note.next_to(vector_card, UP, buff=0.22)
        self.play_for(FadeIn(vector_card, shift=RIGHT * 0.1), seconds=0.7)
        self.play_for(FadeIn(vector_note), seconds=0.5)
        self.hold(1.0)

        rows = VGroup()
        volume_labels: list[Mobject] = []
        delta_labels: list[Mobject] = []
        row_by_run_id: dict[str, VGroup] = {}
        storms_by_id = {storm.storm_id: storm for storm in claims.storms}
        row_centers = (1.25, 0.22, -0.81, -1.84)
        for run, y_coordinate in zip(candidate.storm_runs, row_centers, strict=True):
            storm = storms_by_id[run.storm_id]
            row, volume_label, delta_label = _storm_comparison(
                run,
                depth_inches=storm.depth_inches,
                duration_hours=storm.duration_hours,
            )
            row.move_to(RIGHT * 2.05 + UP * y_coordinate)
            rows.add(row)
            volume_labels.append(volume_label)
            delta_labels.append(delta_label)
            row_by_run_id[run.run_id] = row

        branch_x = -3.05
        trunk_start = vector_card.get_right()
        trunk_end = RIGHT * branch_x + UP * 0.55
        trunk = Line(trunk_start, trunk_end, color=RAIN_BLUE, stroke_width=4)
        spine = Line(
            RIGHT * branch_x + UP * row_centers[0],
            RIGHT * branch_x + UP * row_centers[-1],
            color=RAIN_BLUE,
            stroke_width=4,
        )
        self.play_for(Create(trunk), Create(spine), seconds=0.6)

        branch_lines = VGroup()
        for row in rows:
            branch = Arrow(
                RIGHT * branch_x + UP * row.get_y(),
                row.get_left(),
                buff=0.05,
                color=RAIN_BLUE,
                stroke_width=3,
                max_tip_length_to_length_ratio=0.15,
            )
            branch_lines.add(branch)
            self.play_for(Create(branch), seconds=0.35)
            self.play_for(FadeIn(row, shift=RIGHT * 0.1), seconds=0.65)
            self.play_for(
                Circumscribe(row, color=RAIN_BLUE, buff=0.05, fade_out=True),
                seconds=0.4,
            )
            self.hold(0.55)
        self.hold(1.2)

        sum_label = VGroup(
            mono("SUM THE VOLUMES", size=21, color=RAIN_BLUE),
            body("for the aggregate target", size=21, color=INK_SOFT),
        ).arrange(DOWN, buff=0.08)
        sum_label.move_to(LEFT * 4.95 + DOWN * 0.55)
        aggregate_card, aggregate_value, reduction = _aggregate_card(
            candidate.volume,
            candidate.reduction,
        )
        aggregate_card.move_to(LEFT * 4.95 + DOWN * 1.55)
        aggregate_shell = aggregate_card[0]
        self.play_for(FadeIn(sum_label), FadeIn(aggregate_shell), seconds=0.6)
        sum_terms = VGroup(
            *(mono(f"{run.volume:.3f}", size=19, color=RAIN_BLUE) for run in candidate.storm_runs)
        ).arrange_in_grid(rows=2, cols=2, buff=(0.25, 0.08))
        sum_terms.move_to(aggregate_shell)
        for volume_label, sum_term in zip(volume_labels, sum_terms, strict=True):
            self.play_for(TransformFromCopy(volume_label, sum_term), seconds=0.45)
        self.play_for(
            FadeOut(sum_terms),
            FadeIn(aggregate_value),
            FadeIn(reduction),
            seconds=0.7,
        )
        self.hold(1.5)
        self.play_for(Indicate(aggregate_value, color=RAIN_BLUE), seconds=0.7)
        self.hold(0.6)
        self.play_for(Indicate(reduction, color=CAUTION_OCHRE), seconds=0.6)
        self.hold(0.4)

        target_note = mono("TARGET ≥ 6%", size=21, color=CAUTION_OCHRE)
        target_note.next_to(aggregate_shell, DOWN, buff=0.16)
        self.play_for(FadeIn(target_note), seconds=0.6)
        self.hold(1.0)

        guardrail = VGroup(
            mono("TEST EACH PEAK", size=22, color=FLOOD_RED),
            mono("Δpeak ≤ 0 cfs", size=23, color=FLOOD_RED),
        ).arrange(RIGHT, buff=0.28)
        guardrail.move_to(RIGHT * 2.25 + UP * 1.86)
        guardrail_rule = Line(
            LEFT * 1.25 + UP * 1.67,
            RIGHT * 5.55 + UP * 1.67,
            color=FLOOD_RED,
            stroke_width=3,
        )
        self.play_for(Create(guardrail_rule), FadeIn(guardrail), seconds=0.7)
        self.hold(0.8)
        self.play_for(Indicate(guardrail, color=FLOOD_RED), seconds=0.7)
        self.hold(0.4)
        for delta_label in delta_labels:
            self.play_for(Indicate(delta_label, scale_factor=1.05), seconds=0.65)
            self.hold(0.45)

        worst_row = row_by_run_id[worst_run.run_id]
        worst_outline = SurroundingRectangle(
            worst_row,
            color=FLOOD_RED,
            stroke_width=4,
            buff=0.06,
            corner_radius=0.12,
        )
        worst_callout = VGroup(
            mono("WORST RESPONSE", size=21, color=FLOOD_RED),
            mono(
                f"{worst_run.baseline_peak:.2f} → {worst_run.alternative_peak:.2f} cfs",
                size=23,
                color=FLOOD_RED,
            ),
        ).arrange(DOWN, buff=0.08)
        worst_callout.move_to(LEFT * 4.95 + UP * 0.82)
        self.play_for(Create(worst_outline), seconds=0.7)
        self.play_for(
            FadeOut(vector_note),
            FadeOut(vector_card),
            FadeOut(trunk),
            FadeIn(worst_callout, shift=RIGHT * 0.1),
            seconds=0.7,
        )
        self.hold(1.3)

        stress_row = next(
            row
            for row, run in zip(rows, candidate.storm_runs, strict=True)
            if run.storm_id == "synthetic_stress_4in"
        )
        stress_note = mono("ALSO +0.01 cfs", size=20, color=FLOOD_RED)
        stress_note.next_to(worst_callout, DOWN, buff=0.2)
        self.play_for(
            Circumscribe(stress_row, color=FLOOD_RED, buff=0.05, fade_out=True),
            FadeIn(stress_note),
            seconds=0.7,
        )
        self.hold(0.8)

        projection_window = SurroundingRectangle(
            VGroup(sum_label, aggregate_shell, aggregate_value, reduction, target_note),
            color=CAUTION_OCHRE,
            stroke_width=4,
            buff=0.14,
            corner_radius=0.12,
        )
        projection_label = mono("KEEP ONLY ΣV?", size=22, color=CAUTION_OCHRE)
        projection_label.next_to(projection_window, UP, buff=0.12)
        self.play_for(Create(projection_window), FadeIn(projection_label), seconds=0.7)
        self.play_for(
            rows.animate.set_opacity(0.16),
            branch_lines.animate.set_opacity(0.16),
            spine.animate.set_opacity(0.16),
            guardrail.animate.set_opacity(0.16),
            guardrail_rule.animate.set_opacity(0.16),
            worst_outline.animate.set_opacity(0.16),
            seconds=0.9,
        )
        self.hold(1.2)
        self.play_for(
            Circumscribe(aggregate_shell, color=CAUTION_OCHRE, buff=0.05, fade_out=True),
            seconds=0.8,
        )
        self.hold(1.0)
        hidden_note = body(
            "The peak guardrail has vanished from this projection.",
            size=22,
            color=FLOOD_RED,
        )
        hidden_note.move_to(RIGHT * 2.05 + DOWN * 2.55)
        self.play_for(FadeIn(hidden_note, shift=UP * 0.08), seconds=0.6)
        self.hold(1.0)

        self.play_for(
            rows.animate.set_opacity(1),
            branch_lines.animate.set_opacity(1),
            spine.animate.set_opacity(1),
            guardrail.animate.set_opacity(1),
            guardrail_rule.animate.set_opacity(1),
            worst_outline.animate.set_opacity(1),
            FadeOut(projection_window),
            FadeOut(projection_label),
            FadeOut(hidden_note),
            seconds=0.9,
        )
        self.play_for(worst_outline.animate.set_stroke(width=8), seconds=0.35)
        self.play_for(worst_outline.animate.set_stroke(width=4), seconds=0.35)
        self.hold(1.0)

        conclusion = heading(
            "SUM VOLUME. TEST PEAKS ONE BY ONE.",
            size=37,
            color=WATERSHED_INK,
        )
        conclusion.move_to(DOWN * 2.78)
        self.play_for(FadeIn(conclusion, shift=UP * 0.08), seconds=0.7)
        self.hold(1.2)
        self.place_footer("Aggregation is a projection — the engineer chooses what may be lost.")
        for delta_label in delta_labels:
            self.play_for(Indicate(delta_label, scale_factor=1.035), seconds=0.5)
            self.hold(0.25)
        self.play_for(worst_outline.animate.set_stroke(width=8), seconds=0.35)
        self.play_for(worst_outline.animate.set_stroke(width=4), seconds=0.35)
        self.hold(0.5)
        self.finish_timing()
