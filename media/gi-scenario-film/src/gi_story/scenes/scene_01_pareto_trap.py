"""Scene 01: reveal the engineering rule hidden by a two-axis projection."""

from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Arrow,
    Create,
    DashedLine,
    Dot,
    FadeIn,
    FadeOut,
    GrowFromCenter,
    Indicate,
    LaggedStart,
    Line,
    SurroundingRectangle,
    TransformFromCopy,
    VGroup,
    VMobject,
)

from gi_story.claims import load_claims
from gi_story.components import panel
from gi_story.theme import (
    CAUTION_OCHRE,
    CHANNEL_TEAL,
    FLOOD_RED,
    FLOOD_WASH,
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


class Scene01ParetoTrap(BaseFilmScene):
    """Keep ALT-017 visible while the omitted peak rule returns to the picture."""

    scene_id = "01-pareto-trap"
    target_duration = 55.0

    def construct(self) -> None:
        claims = load_claims()
        alt017 = claims.teaching_by_id["ALT-017"]
        worst_peak = max(alt017.peak_increases, key=lambda item: item.delta)
        volume_rule = claims.decision_rules[0]

        title = VGroup(
            mono(
                f"{claims.guided_alternatives} PLANS · FOUR RAIN TESTS EACH",
                size=20,
                color=CHANNEL_TEAL,
            ),
            heading("What did the plot forget?", size=58),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.22)
        title.to_edge(UP, buff=0.56).to_edge(LEFT, buff=0.76)
        badge = self.limitation_badge("SYNTHETIC TEACHING EVIDENCE")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=1.0)
        self.play_for(FadeIn(badge), seconds=0.7)
        self.hold(2.3)

        plot_shell = panel(10.9, 4.25, fill=SURVEY_PAPER)
        plot_shell.move_to(DOWN * 0.45)
        origin = plot_shell.get_corner(DOWN + LEFT) + RIGHT * 0.72 + UP * 0.62
        x_axis = Line(
            origin,
            plot_shell.get_corner(DOWN + RIGHT) + LEFT * 0.42 + UP * 0.62,
            color=WATERSHED_INK,
            stroke_width=3,
        )
        y_axis = Line(
            origin,
            plot_shell.get_corner(UP + LEFT) + RIGHT * 0.72 + DOWN * 0.42,
            color=WATERSHED_INK,
            stroke_width=3,
        )
        x_label = mono("30-year teaching cost  →", size=21, color=INK_SOFT)
        x_label.next_to(x_axis, DOWN, buff=0.12).align_to(x_axis, RIGHT)
        y_label = mono("less total outlet water  →", size=20, color=INK_SOFT)
        y_label.rotate(1.5708).next_to(y_axis, LEFT, buff=0.1)
        scope = body(
            (f"{claims.guided_alternatives} possible plans · {claims.storm_count} rain tests each"),
            size=22,
            color=INK_SOFT,
        )
        scope.move_to(plot_shell.get_top() + DOWN * 0.3)
        self.play_for(
            FadeIn(plot_shell),
            Create(x_axis),
            Create(y_axis),
            FadeIn(x_label),
            FadeIn(y_label),
            FadeIn(scope),
            seconds=1.4,
        )

        alternatives = claims.guided_points
        costs = [item.cost for item in alternatives]
        reductions = [item.reduction for item in alternatives]
        cost_min, cost_max = min(costs), max(costs)
        reduction_min, reduction_max = min(reductions), max(reductions)

        def plot_point(cost: float, reduction: float):
            x_fraction = (cost - cost_min) / max(cost_max - cost_min, 1.0)
            y_fraction = (reduction - reduction_min) / max(reduction_max - reduction_min, 1.0)
            return origin + RIGHT * (0.35 + 8.55 * x_fraction) + UP * (0.22 + 2.6 * y_fraction)

        points = VGroup()
        point_by_id: dict[str, Dot] = {}
        teaching_ids = set(claims.teaching_by_id)
        for alternative in alternatives:
            if alternative.id == alt017.id:
                color, radius = CAUTION_OCHRE, 0.11
            elif alternative.id in teaching_ids or alternative.nondominated:
                color, radius = CHANNEL_TEAL, 0.052
            else:
                color, radius = LINE_GRAY, 0.029
            point = Dot(
                plot_point(alternative.cost, alternative.reduction), radius=radius, color=color
            )
            points.add(point)
            point_by_id[alternative.id] = point
        self.play_for(
            LaggedStart(*(GrowFromCenter(point) for point in points), lag_ratio=0.025),
            seconds=2.5,
        )
        self.hold(2.5)

        better_arrow = Arrow(
            plot_shell.get_center() + RIGHT * 0.85 + DOWN * 0.15,
            plot_shell.get_center() + LEFT * 0.45 + UP * 0.95,
            color=CHANNEL_TEAL,
            stroke_width=5,
            max_tip_length_to_length_ratio=0.16,
        )
        better_copy = heading("more reduction\nfor less cost", size=26, color=CHANNEL_TEAL)
        better_copy.next_to(better_arrow, RIGHT, buff=0.12)
        self.play_for(Create(better_arrow), FadeIn(better_copy), seconds=1.0)
        self.hold(2.0)
        self.play_for(FadeOut(better_arrow, better_copy), seconds=0.6)

        target_y = plot_point(cost_min, float(volume_rule.threshold))[1]
        target_line = DashedLine(
            [origin[0], target_y, 0],
            [x_axis.get_end()[0], target_y, 0],
            color=FLOOD_RED,
            dash_length=0.12,
            stroke_width=3,
        )
        target_copy = mono(
            f"target: at least {volume_rule.threshold:g}% less total outlet water",
            size=21,
            color=FLOOD_RED,
        )
        target_copy.next_to(target_line, UP, buff=0.11).align_to(target_line, RIGHT)
        target_backing = SurroundingRectangle(
            target_copy,
            color=SURVEY_PAPER,
            buff=0.07,
            stroke_width=0,
            fill_color=SURVEY_PAPER,
            fill_opacity=0.96,
        )
        target_label = VGroup(target_backing, target_copy)
        self.play_for(Create(target_line), FadeIn(target_label), seconds=1.2)

        selected = point_by_id[alt017.id]
        selected_halo = SurroundingRectangle(
            selected,
            color=CAUTION_OCHRE,
            buff=0.16,
            corner_radius=0.08,
            stroke_width=3,
        )
        selected_copy = VGroup(
            mono(alt017.id, size=25, color=CAUTION_OCHRE),
            mono(f"${alt017.cost:.2f}k  ·  {alt017.reduction:.2f}%", size=25),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.08)
        selected_copy.next_to(selected_halo, RIGHT, buff=0.2).shift(UP * 0.2)
        self.play_for(Create(selected_halo), Indicate(selected, color=CAUTION_OCHRE), seconds=1.2)
        self.play_for(FadeIn(selected_copy, shift=RIGHT * 0.08), seconds=1.0)
        self.hold(3.2)

        plot_group = VGroup(
            plot_shell,
            x_axis,
            y_axis,
            x_label,
            y_label,
            scope,
            points,
            target_line,
            target_label,
            selected_halo,
            selected_copy,
        )
        self.play_for(plot_group.animate.scale(0.67).to_edge(LEFT, buff=0.42), seconds=1.6)

        peak_shell = panel(5.25, 3.9, fill=FLOOD_WASH, stroke=FLOOD_RED)
        peak_shell.move_to(RIGHT * 3.9 + DOWN * 0.35)
        peak_title = heading("Two peaks rise", size=34, color=FLOOD_RED)
        peak_title.move_to(peak_shell.get_top() + DOWN * 0.38)
        storm_copy = body("Worst: synthetic long event", size=22, color=INK_SOFT)
        storm_copy.next_to(peak_title, DOWN, buff=0.06)
        self.play_for(FadeIn(peak_shell), FadeIn(peak_title), FadeIn(storm_copy), seconds=1.0)

        number_start = peak_shell.get_left() + RIGHT * 0.62 + DOWN * 0.25
        number_end = peak_shell.get_right() + LEFT * 0.58 + DOWN * 0.25
        number_line = Line(number_start, number_end, color=WATERSHED_INK, stroke_width=3)
        scale_min, scale_max = 3.0, 3.25

        def peak_position(value: float):
            fraction = (value - scale_min) / (scale_max - scale_min)
            return number_start + (number_end - number_start) * fraction

        baseline_dot = Dot(peak_position(worst_peak.baseline_peak), radius=0.1, color=LINE_GRAY)
        alternative_dot = Dot(
            peak_position(worst_peak.alternative_peak), radius=0.13, color=FLOOD_RED
        )
        baseline_copy = VGroup(
            body("unchanged plan", size=20),
            mono(f"{worst_peak.baseline_peak:.2f} cfs", size=25),
        ).arrange(DOWN, buff=0.05)
        baseline_copy.next_to(baseline_dot, UP, buff=0.15)
        alternative_copy = VGroup(
            body(alt017.id, size=20, color=FLOOD_RED),
            mono(f"{worst_peak.alternative_peak:.2f} cfs", size=25, color=FLOOD_RED),
        ).arrange(DOWN, buff=0.05)
        alternative_copy.next_to(alternative_dot, DOWN, buff=0.15)
        zoom_label = mono("zoomed peak comparison", size=16, color=MUTED)
        zoom_label.next_to(number_line, DOWN, buff=0.78)
        self.play_for(Create(number_line), FadeIn(zoom_label), seconds=1.2)
        self.play_for(
            TransformFromCopy(selected, alternative_dot),
            FadeIn(baseline_dot),
            FadeIn(baseline_copy),
            FadeIn(alternative_copy),
            seconds=1.7,
        )
        delta_arrow = Arrow(
            baseline_dot.get_center(),
            alternative_dot.get_center(),
            buff=0.08,
            color=FLOOD_RED,
            stroke_width=5,
            max_tip_length_to_length_ratio=0.2,
        )
        delta_copy = mono(f"+{worst_peak.delta:.2f} cfs", size=30, color=FLOOD_RED)
        delta_copy.next_to(number_line, UP, buff=0.8)
        self.play_for(Create(delta_arrow), FadeIn(delta_copy), seconds=1.0)
        self.hold(3.0)

        second_peak = min(alt017.peak_increases, key=lambda item: item.delta)
        second_copy = body(
            f"Stress event also rises: +{second_peak.delta:.2f} cfs",
            size=22,
            color=FLOOD_RED,
        )
        second_copy.move_to(peak_shell.get_bottom() + UP * 0.28)
        self.play_for(FadeIn(second_copy, shift=UP * 0.08), seconds=0.8)
        self.play_for(Indicate(target_label, color=FLOOD_RED), seconds=0.9)
        self.hold(2.6)
        self.play_for(Indicate(second_copy, color=FLOOD_RED), seconds=0.9)
        self.hold(1.8)
        self.play_for(Indicate(selected_copy, color=CAUTION_OCHRE), seconds=0.9)
        self.hold(1.2)

        # Route the evidence thread below the selected value label. A direct
        # line from the point to the panel would strike through the cost and
        # reduction copy for the remainder of the scene.
        thread_start = selected_halo.get_bottom()
        thread_y = x_axis.get_center()[1] + 0.2
        thread_exit_x = plot_shell.get_right()[0] + 0.08
        thread_end = peak_shell.get_left() + DOWN * 0.28
        thread = VMobject(color=CHANNEL_TEAL, stroke_width=4)
        thread.set_points_as_corners(
            [
                thread_start,
                [thread_start[0], thread_y, 0],
                [thread_exit_x, thread_y, 0],
                thread_end,
            ]
        )
        self.play_for(Create(thread), Indicate(delta_copy, color=FLOOD_RED), seconds=1.0)
        self.hold(2.1)

        conclusion = VGroup(
            heading("ATTRACTIVE ON THIS CHART", size=34, color=CAUTION_OCHRE),
            mono("≠", size=38, color=FLOOD_RED),
            heading("ACCEPTABLE UNDER EVERY RULE", size=34, color=FLOOD_RED),
        ).arrange(RIGHT, buff=0.2)
        if conclusion.width > 12.45:
            conclusion.scale_to_fit_width(12.45)
        conclusion.to_edge(DOWN, buff=0.74)
        self.play_for(FadeIn(conclusion, shift=UP * 0.1), seconds=1.2)
        self.hold(3.2)
        self.play_for(Indicate(selected, color=CAUTION_OCHRE), seconds=1.0)
        self.hold(2.7)
        # Keep the final emphasis inside the protected frame margin.  Manim's
        # default Indicate scale is large enough to push this long sentence
        # through the encoded frame edge during the pulse.
        self.play_for(Indicate(conclusion, color=FLOOD_RED, scale_factor=1.02), seconds=1.0)
        self.hold(2.5)
        self.place_footer("Saved results from a synthetic drainage-model study.")
        self.finish_timing()
