"""Scene 09: reveal a Pareto frontier as a lossy shadow of the decision space."""

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
    LaggedStart,
    Line,
    SurroundingRectangle,
    Transform,
    VGroup,
)

from gi_story.claims import AlternativeClaim, load_claims
from gi_story.components import panel
from gi_story.theme import (
    CAUTION_OCHRE,
    CHANNEL_TEAL,
    FLOOD_RED,
    FLOOD_WASH,
    INK_SOFT,
    LINE_GRAY,
    SURVEY_PAPER,
    WATERSHED_INK,
    BaseFilmScene,
    body,
    heading,
    mono,
)


class Scene09HiddenDimension(BaseFilmScene):
    """Project full rule records to cost-volume coordinates, then restore peak."""

    scene_id = "09-hidden-dimension"
    target_duration = 60.0

    def construct(self) -> None:
        claims = load_claims()
        alt017 = claims.teaching_by_id["ALT-017"]
        worst_peak = max(alt017.peak_increases, key=lambda item: item.delta)

        title = self.title_block("The geometry of a shortlist", "A frontier is a shadow")
        badge = self.limitation_badge("AMONG 220 DIRECTLY EVALUATED")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=1.0)
        self.play_for(FadeIn(badge), seconds=0.6)
        self.hold(2.4)

        candidate_order = ("ALT-017", "ALT-207", "ALT-109", "ALT-014")
        candidates = [claims.teaching_by_id[identifier] for identifier in candidate_order]
        columns = VGroup()
        for candidate in candidates:
            shell = panel(2.75, 3.65, fill=SURVEY_PAPER)
            identifier = mono(
                candidate.id,
                size=28,
                color=CAUTION_OCHRE if candidate.id == "ALT-017" else CHANNEL_TEAL,
            )
            values = (
                ("VOLUME", f"{candidate.reduction:.2f}%", candidate.reduction >= 6.0),
                ("COST", f"${candidate.cost:.2f}k", candidate.cost <= 200.0),
                (
                    "PEAK",
                    "no rise"
                    if not candidate.peak_increases
                    else f"+{max(run.delta for run in candidate.peak_increases):.2f} cfs",
                    not candidate.peak_increases,
                ),
                ("QA", "4 / 4", True),
            )
            gauges = VGroup()
            for label, value, passed in values:
                mark_color = CHANNEL_TEAL if passed else FLOOD_RED
                mark = Dot(radius=0.09, color=mark_color)
                copy = VGroup(
                    mono(label, size=18, color=INK_SOFT),
                    mono(value, size=23, color=mark_color),
                ).arrange(DOWN, aligned_edge=LEFT, buff=0.02)
                gauges.add(VGroup(mark, copy).arrange(RIGHT, buff=0.14))
            gauges.arrange(DOWN, aligned_edge=LEFT, buff=0.22)
            group = VGroup(identifier, gauges).arrange(DOWN, aligned_edge=LEFT, buff=0.28)
            group.move_to(shell).align_to(shell, LEFT).shift(RIGHT * 0.28)
            columns.add(VGroup(shell, group))
        columns.arrange(RIGHT, buff=0.22)
        columns.move_to(DOWN * 0.33)
        full_space = mono("FULL DECISION RECORD", size=23, color=WATERSHED_INK)
        full_space.next_to(columns, UP, buff=0.16)
        self.play_for(FadeIn(full_space), seconds=1.0)
        self.play_for(
            LaggedStart(*(FadeIn(column, shift=UP * 0.12) for column in columns), lag_ratio=0.2),
            seconds=5.5,
        )
        self.hold(3.0)

        alt017_column = columns[0]
        failures = VGroup(
            SurroundingRectangle(
                alt017_column[1][1][0], color=FLOOD_RED, buff=0.08, stroke_width=3
            ),
            SurroundingRectangle(
                alt017_column[1][1][2], color=FLOOD_RED, buff=0.08, stroke_width=3
            ),
        )
        failure_note = body("fails volume + peak", size=27, color=FLOOD_RED)
        failure_note.next_to(alt017_column, DOWN, buff=0.14)
        self.play_for(Create(failures[0]), seconds=1.4)
        self.play_for(Create(failures[1]), FadeIn(failure_note), seconds=1.4)
        self.hold(2.8)

        # Collapse the full records into the two coordinates shown by the chart.
        self.play_for(FadeOut(full_space, failures, failure_note), seconds=1.0)
        projection_arrow = Arrow(
            UP * 1.35,
            DOWN * 1.15,
            color=CAUTION_OCHRE,
            stroke_width=5,
            max_tip_length_to_length_ratio=0.14,
        )
        projection_label = VGroup(
            mono("KEEP", size=20, color=CAUTION_OCHRE),
            body("cost + aggregate volume", size=26),
        ).arrange(DOWN, buff=0.04)
        projection_label.next_to(projection_arrow, RIGHT, buff=0.25)
        self.play_for(FadeOut(columns), seconds=1.5)
        self.play_for(FadeIn(projection_label), Create(projection_arrow), seconds=2.0)

        plot_shell = panel(11.5, 4.45, fill=SURVEY_PAPER)
        plot_shell.move_to(DOWN * 0.3)
        x_axis = Line(
            plot_shell.get_corner(DOWN + LEFT) + RIGHT * 0.85 + UP * 0.62,
            plot_shell.get_corner(DOWN + RIGHT) + LEFT * 0.45 + UP * 0.62,
            color=WATERSHED_INK,
            stroke_width=3,
        )
        y_axis = Line(
            x_axis.get_start(),
            plot_shell.get_corner(UP + LEFT) + RIGHT * 0.85 + DOWN * 0.4,
            color=WATERSHED_INK,
            stroke_width=3,
        )
        x_label = body("less cost  ←", size=27, color=INK_SOFT)
        x_label.next_to(x_axis, DOWN, buff=0.13).align_to(x_axis, LEFT)
        y_label = body("more reduction  ↑", size=26, color=INK_SOFT)
        # Keep the label visibly beside the axis; centering it near the axis
        # made the vertical stroke cut through the word "more".
        y_label.next_to(y_axis.get_top(), RIGHT, buff=0.18).shift(DOWN * 0.22)
        shown = mono("THE SHADOW KEEPS TWO COORDINATES", size=22, color=CAUTION_OCHRE)
        shown.move_to(plot_shell.get_top() + DOWN * 0.3).align_to(plot_shell, RIGHT).shift(
            LEFT * 0.3
        )

        costs = [point.cost for point in claims.guided_points]
        reductions = [point.reduction for point in claims.guided_points]
        min_cost, max_cost = min(costs), max(costs)
        min_reduction, max_reduction = min(reductions), max(reductions)

        def location(candidate: AlternativeClaim):
            cost_fraction = (candidate.cost - min_cost) / (max_cost - min_cost)
            reduction_fraction = (candidate.reduction - min_reduction) / (
                max_reduction - min_reduction
            )
            return (
                x_axis.get_start()
                + RIGHT * (0.55 + cost_fraction * 8.35)
                + UP * (0.38 + reduction_fraction * 2.55)
            )

        background_points = VGroup()
        for point in claims.guided_points:
            x_fraction = (point.cost - min_cost) / (max_cost - min_cost)
            y_fraction = (point.reduction - min_reduction) / (max_reduction - min_reduction)
            background_points.add(
                Dot(
                    x_axis.get_start()
                    + RIGHT * (0.55 + x_fraction * 8.35)
                    + UP * (0.38 + y_fraction * 2.55),
                    radius=0.025,
                    color=LINE_GRAY,
                )
            )
        point_groups = VGroup()
        point_by_id: dict[str, Dot] = {}
        for candidate in candidates:
            color = CAUTION_OCHRE if candidate.id == "ALT-017" else CHANNEL_TEAL
            dot = Dot(location(candidate), radius=0.105, color=color)
            label = mono(candidate.id, size=21, color=color)
            label_offsets = {
                "ALT-017": LEFT * 0.45 + DOWN * 0.18,
                "ALT-207": LEFT * 0.12 + DOWN * 0.28,
                "ALT-109": RIGHT * 0.2 + UP * 0.28,
                "ALT-014": RIGHT * 0.48 + DOWN * 0.04,
            }
            label.move_to(dot.get_center() + label_offsets[candidate.id])
            point_groups.add(VGroup(dot, label))
            point_by_id[candidate.id] = dot

        shadow_plane = VGroup(plot_shell, x_axis, y_axis, x_label, y_label, shown)
        self.play_for(
            # `columns` has already left the scene. Asking Manim to fade it a
            # second time re-adds the cards for a frame before fading them,
            # producing a severe ghost collision with the projection graphic.
            FadeOut(projection_arrow, projection_label),
            FadeIn(shadow_plane),
            seconds=2.4,
        )
        self.play_for(FadeIn(background_points), seconds=2.5)
        self.play_for(
            LaggedStart(
                *(FadeIn(point, shift=DOWN * 0.1) for point in point_groups), lag_ratio=0.2
            ),
            seconds=3.7,
        )
        frontier = Line(
            point_by_id["ALT-017"].get_center(),
            point_by_id["ALT-014"].get_center(),
            color=CHANNEL_TEAL,
            stroke_width=4,
        )
        frontier.set_z_index(-1)
        frontier_note = VGroup(
            mono("NONDOMINATED", size=22, color=CHANNEL_TEAL),
            body("in this two-coordinate view", size=25),
        ).arrange(DOWN, buff=0.04)
        frontier_note.move_to(plot_shell.get_right() + LEFT * 2.15 + DOWN * 1.15)
        self.play_for(Create(frontier), FadeIn(frontier_note), seconds=2.0)
        self.hold(3.0)

        alt017_dot = point_by_id["ALT-017"]
        depth_line = DashedLine(
            alt017_dot.get_center(),
            alt017_dot.get_center() + UP * 1.35 + RIGHT * 0.7,
            color=FLOOD_RED,
            stroke_width=5,
        )
        pulled_dot = Dot(depth_line.get_end(), radius=0.14, color=FLOOD_RED)
        peak_card = panel(4.0, 1.35, fill=FLOOD_WASH, stroke=FLOOD_RED)
        peak_card.move_to(plot_shell.get_right() + LEFT * 2.1 + DOWN * 0.95)
        peak_copy = VGroup(
            mono("RESTORE PEAK", size=18, color=FLOOD_RED),
            mono(
                f"{worst_peak.baseline_peak:.2f} → {worst_peak.alternative_peak:.2f} cfs",
                size=28,
                color=FLOOD_RED,
            ),
            body("two selected-storm increases", size=21, color=FLOOD_RED),
        ).arrange(DOWN, buff=0.07)
        peak_copy.move_to(peak_card)
        self.play_for(
            FadeOut(frontier_note),
            Create(depth_line),
            Transform(alt017_dot.copy(), pulled_dot),
            seconds=2.9,
        )
        self.play_for(FadeIn(peak_card), FadeIn(peak_copy), seconds=1.8)
        self.hold(3.0)

        hidden_tokens = VGroup(
            mono("4 PEAK DELTAS", size=23, color=FLOOD_RED),
            mono("QA", size=23, color=CHANNEL_TEAL),
            mono("MODEL FITNESS", size=23, color=CAUTION_OCHRE),
        ).arrange(RIGHT, buff=0.65)
        hidden_tokens.to_edge(DOWN, buff=0.58)
        hidden_lead = body("The shadow had discarded:", size=27, color=INK_SOFT)
        hidden_lead.next_to(hidden_tokens, UP, buff=0.12)
        self.play_for(FadeIn(hidden_lead), seconds=1.2)
        self.play_for(
            LaggedStart(
                *(FadeIn(token, shift=UP * 0.08) for token in hidden_tokens), lag_ratio=0.25
            ),
            seconds=2.8,
        )
        self.hold(2.8)

        conclusion = heading("NONDOMINATED  ≠  ACCEPTABLE", size=44, color=FLOOD_RED)
        explanation = body(
            "The frontier maps chosen tradeoffs; the brief defines acceptance.",
            size=28,
            color=INK_SOFT,
        )
        conclusion_group = VGroup(conclusion, explanation).arrange(DOWN, buff=0.1)
        conclusion_group.to_edge(DOWN, buff=0.38)
        self.play_for(FadeOut(hidden_lead, hidden_tokens), seconds=0.7)
        self.play_for(FadeIn(conclusion_group, shift=UP * 0.08), seconds=1.5)
        self.hold(3.0)
        self.finish_timing()
