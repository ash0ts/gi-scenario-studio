"""Scene 02: follow water from rainfall to an outfall response."""

from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Arrow,
    Create,
    Dot,
    FadeIn,
    FadeOut,
    Indicate,
    LaggedStart,
    Line,
    MoveAlongPath,
    Polygon,
    Rectangle,
    Transform,
    TransformFromCopy,
    VGroup,
    VMobject,
)

from gi_story.claims import load_claims
from gi_story.theme import (
    CAUTION_OCHRE,
    CHANNEL_TEAL,
    INK_SOFT,
    LINE_GRAY,
    MUTED,
    RAIN_BLUE,
    RAIN_WASH,
    TEAL_WASH,
    WATERSHED_INK,
    BaseFilmScene,
    body,
    heading,
    mono,
)


def _rain_bars(origin, heights: tuple[float, ...]) -> VGroup:
    bars = VGroup()
    for index, height in enumerate(heights):
        bar = Rectangle(
            width=0.28,
            height=height,
            stroke_color=RAIN_BLUE,
            stroke_width=2,
            fill_color=RAIN_BLUE,
            fill_opacity=0.78,
        )
        bar.move_to(origin + RIGHT * index * 0.42 + UP * height / 2)
        bars.add(bar)
    return bars


def _hydrograph(axis_origin, points: tuple[tuple[float, float], ...]) -> VMobject:
    curve = VMobject(color=CHANNEL_TEAL, stroke_width=6)
    curve.set_points_smoothly([axis_origin + RIGHT * x + UP * y for x, y in points])
    return curve


class Scene02RainfallToRunoff(BaseFilmScene):
    """Preserve the identity of water as it infiltrates, stores, and runs off."""

    scene_id = "02-rainfall-to-runoff"
    target_duration = 60.0

    def construct(self) -> None:
        claims = load_claims()
        title = VGroup(
            mono("CONCEPTUAL WATER PATH", size=22, color=CHANNEL_TEAL),
            heading("Where does the water go?", size=56),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.22)
        title.to_edge(UP, buff=0.56).to_edge(LEFT, buff=0.76)
        badge = self.limitation_badge("CONCEPTUAL · NOT MEASURED")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=1.0)
        self.play_for(FadeIn(badge), seconds=0.7)
        self.hold(2.1)

        ground = Polygon(
            LEFT * 6.2 + DOWN * 0.05,
            LEFT * 3.9 + UP * 0.15,
            LEFT * 1.15 + DOWN * 0.75,
            LEFT * 1.15 + DOWN * 2.1,
            LEFT * 6.2 + DOWN * 2.1,
            color=WATERSHED_INK,
            stroke_width=3,
            fill_color=RAIN_WASH,
            fill_opacity=0.45,
        )
        ground_label = heading("catchment", size=28)
        ground_label.move_to(LEFT * 5.35 + DOWN * 1.68)
        rain_axis = Line(LEFT * 5.95 + UP * 0.55, LEFT * 3.0 + UP * 0.55, color=LINE_GRAY)
        bars_origin = rain_axis.get_left() + RIGHT * 0.1
        bars = _rain_bars(bars_origin, (0.3, 0.55, 0.85, 1.25, 0.9, 0.55, 0.3))
        rain_copy = heading("rainfall over time", size=24, color=RAIN_BLUE)
        rain_copy.move_to(LEFT * 2.15 + UP * 1.55)
        self.play_for(Create(ground), FadeIn(ground_label), seconds=1.3)
        self.play_for(Create(rain_axis), seconds=0.5)
        self.play_for(
            LaggedStart(*(FadeIn(bar, shift=UP * 0.12) for bar in bars), lag_ratio=0.1),
            FadeIn(rain_copy),
            seconds=2.6,
        )
        self.hold(2.4)

        storage = Rectangle(
            width=1.05,
            height=0.42,
            stroke_color=CHANNEL_TEAL,
            stroke_width=3,
            fill_color=TEAL_WASH,
            fill_opacity=1,
        )
        storage.move_to(LEFT * 3.2 + DOWN * 0.55)
        outlet = Dot(LEFT * 1.35 + DOWN * 1.65, radius=0.12, color=CHANNEL_TEAL)
        outlet_copy = VGroup(
            body("outfall", size=18, color=INK_SOFT),
            mono("OUT1", size=19, color=CHANNEL_TEAL),
        ).arrange(DOWN, buff=0.02)
        outlet_copy.next_to(outlet, RIGHT, buff=0.14)
        runoff_path = VMobject(color=RAIN_BLUE, stroke_width=5)
        runoff_path.set_points_smoothly(
            [
                LEFT * 5.15 + DOWN * 0.05,
                LEFT * 4.2 + DOWN * 0.25,
                LEFT * 3.2 + DOWN * 0.55,
                LEFT * 2.2 + DOWN * 1.05,
                outlet.get_center(),
            ]
        )
        runoff_tip = Arrow(
            runoff_path.point_from_proportion(0.78),
            outlet.get_center(),
            buff=0.13,
            color=RAIN_BLUE,
            stroke_width=5,
            max_tip_length_to_length_ratio=0.22,
        )
        self.play_for(FadeIn(storage), seconds=0.8)
        self.play_for(
            Create(runoff_path),
            Create(runoff_tip),
            FadeIn(outlet),
            FadeIn(outlet_copy),
            seconds=1.5,
        )

        water = VGroup(*(Dot(radius=0.095, color=RAIN_BLUE) for _ in range(9)))
        water.arrange(RIGHT, buff=0.13)
        water.move_to(LEFT * 4.55 + UP * 0.55)
        self.play_for(
            LaggedStart(*(FadeIn(drop, shift=DOWN * 0.32) for drop in water), lag_ratio=0.08),
            seconds=1.5,
        )
        self.hold(1.8)

        infiltration_path = Line(LEFT * 4.75 + DOWN * 0.05, LEFT * 4.75 + DOWN * 1.6)
        storage_path = Line(LEFT * 3.85 + DOWN * 0.05, storage.get_center())
        runoff_motion_path = runoff_path.copy().set_stroke(opacity=0)
        infiltrating = water[:3]
        stored = water[3:5]
        running = water[5:]
        self.play_for(
            *(MoveAlongPath(drop, infiltration_path.copy()) for drop in infiltrating),
            seconds=3.1,
        )
        infiltrate_label = VGroup(
            Arrow(
                infiltration_path.get_start(),
                infiltration_path.get_end(),
                buff=0.08,
                color=CAUTION_OCHRE,
                stroke_width=4,
            ),
            heading("infiltrate", size=24, color=CAUTION_OCHRE),
        )
        infiltrate_label[1].next_to(infiltrate_label[0], LEFT, buff=0.12)
        self.play_for(FadeIn(infiltrate_label), seconds=0.7)
        self.play_for(
            *(MoveAlongPath(drop, storage_path.copy()) for drop in stored),
            seconds=2.2,
        )
        store_label = heading("temporary storage", size=22, color=CHANNEL_TEAL)
        store_label.next_to(storage, UP, buff=0.08)
        self.play_for(FadeIn(store_label), seconds=0.5)
        self.play_for(
            LaggedStart(
                *(MoveAlongPath(drop, runoff_motion_path.copy()) for drop in running),
                lag_ratio=0.12,
            ),
            seconds=3.5,
        )
        runoff_label = heading("runoff to outfall", size=20, color=RAIN_BLUE)
        runoff_label.move_to(LEFT * 2.55 + DOWN * 1.23)
        self.play_for(FadeIn(runoff_label), seconds=0.6)
        self.play_for(Indicate(infiltrate_label, color=CAUTION_OCHRE), seconds=0.8)
        self.hold(1.2)
        self.play_for(Indicate(store_label, color=CHANNEL_TEAL), seconds=0.8)
        self.hold(1.2)
        self.play_for(Indicate(runoff_label, color=RAIN_BLUE), seconds=0.8)
        self.hold(2.6)

        axis_origin = RIGHT * 1.35 + DOWN * 1.25
        x_axis = Line(axis_origin, RIGHT * 6.1 + DOWN * 1.25, color=WATERSHED_INK, stroke_width=3)
        y_axis = Line(axis_origin, RIGHT * 1.35 + UP * 1.55, color=WATERSHED_INK, stroke_width=3)
        curve_points = (
            (0.08, 0.04),
            (0.65, 0.1),
            (1.25, 0.52),
            (1.95, 2.25),
            (2.55, 1.35),
            (3.4, 0.46),
            (4.45, 0.1),
        )
        curve = _hydrograph(axis_origin, curve_points)
        area = Polygon(
            axis_origin + RIGHT * 0.08,
            *[axis_origin + RIGHT * x + UP * y for x, y in curve_points],
            axis_origin + RIGHT * 4.45,
            color=CHANNEL_TEAL,
            stroke_width=0,
            fill_color=TEAL_WASH,
            fill_opacity=0.72,
        )
        area.set_stroke(opacity=0)
        graph_copy = VGroup(
            heading("outfall OUT1 flow", size=24, color=CHANNEL_TEAL),
            mono("time  →", size=20),
        )
        graph_copy[0].next_to(y_axis, UP, buff=0.12).align_to(y_axis, LEFT)
        graph_copy[1].next_to(x_axis, DOWN, buff=0.1).align_to(x_axis, RIGHT)
        self.play_for(Create(x_axis), Create(y_axis), FadeIn(graph_copy), seconds=1.2)
        self.play_for(TransformFromCopy(running[-1], curve), FadeIn(area), seconds=3.2)

        peak_point = axis_origin + RIGHT * 1.95 + UP * 2.25
        peak_dot = Dot(peak_point, radius=0.11, color=RAIN_BLUE)
        peak_line = Line(
            peak_point, [peak_point[0], axis_origin[1], 0], color=RAIN_BLUE, stroke_width=2
        )
        peak_copy = heading("peak flow", size=25, color=RAIN_BLUE)
        peak_copy.next_to(peak_dot, RIGHT, buff=0.14).shift(DOWN * 0.2)
        volume_copy = heading("event volume", size=25, color=CHANNEL_TEAL)
        volume_copy.move_to(axis_origin + RIGHT * 2.75 + UP * 0.58)
        self.play_for(Create(peak_line), FadeIn(peak_dot), FadeIn(peak_copy), seconds=1.0)
        self.play_for(FadeIn(volume_copy), seconds=0.7)
        self.hold(3.0)

        second_bars = _rain_bars(bars_origin, (1.3, 1.1, 0.8, 0.52, 0.32, 0.18, 0.1))
        second_curve_points = (
            (0.08, 0.04),
            (0.48, 0.28),
            (0.95, 1.2),
            (1.4, 2.55),
            (2.0, 1.15),
            (2.9, 0.36),
            (4.45, 0.08),
        )
        second_curve = _hydrograph(axis_origin, second_curve_points)
        second_area = Polygon(
            axis_origin + RIGHT * 0.08,
            *[axis_origin + RIGHT * x + UP * y for x, y in second_curve_points],
            axis_origin + RIGHT * 4.45,
            color=CHANNEL_TEAL,
            stroke_width=0,
            fill_color=TEAL_WASH,
            fill_opacity=0.72,
        )
        second_area.set_stroke(opacity=0)
        changed_copy = heading(
            "same catchment · changed storm · changed response",
            size=29,
            color=RAIN_BLUE,
        )
        changed_copy.to_edge(DOWN, buff=0.72)
        self.play_for(Transform(bars, second_bars), seconds=2.3)

        # Retain the earlier response as a labelled comparison instead of an
        # unexplained ghost curve during the storm-shape transition.
        previous_curve = curve.copy().set_color(LINE_GRAY).set_stroke(width=4, opacity=0.95)
        previous_curve.set_z_index(1)
        curve.set_z_index(2)
        self.add(previous_curve)
        response_legend = VGroup(
            VGroup(
                Line(LEFT * 0.22, RIGHT * 0.22, color=LINE_GRAY, stroke_width=4),
                body("earlier", size=18, color=MUTED),
            ).arrange(RIGHT, buff=0.08),
            VGroup(
                Line(LEFT * 0.22, RIGHT * 0.22, color=CHANNEL_TEAL, stroke_width=5),
                body("changed", size=18, color=CHANNEL_TEAL),
            ).arrange(RIGHT, buff=0.08),
        ).arrange(RIGHT, buff=0.28)
        response_legend.move_to(axis_origin + RIGHT * 3.45 + UP * 0.22)
        new_peak_point = axis_origin + RIGHT * 1.4 + UP * 2.55
        second_peak_copy = heading("peak flow", size=25, color=RAIN_BLUE)
        second_peak_copy.next_to(new_peak_point, RIGHT, buff=0.14).shift(DOWN * 0.48)
        self.play_for(
            Transform(curve, second_curve),
            Transform(area, second_area),
            peak_dot.animate.move_to(new_peak_point),
            peak_line.animate.put_start_and_end_on(
                new_peak_point,
                axis_origin + RIGHT * 1.4,
            ),
            Transform(peak_copy, second_peak_copy),
            FadeIn(response_legend),
            seconds=2.7,
        )
        self.play_for(FadeIn(changed_copy, shift=UP * 0.1), seconds=0.8)
        self.hold(3.0)

        conclusion = VGroup(
            heading("Rainfall is the input.", size=35, color=RAIN_BLUE),
            heading("Runoff is the modeled response.", size=35, color=CHANNEL_TEAL),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.08)
        conclusion.move_to(DOWN * 2.65)
        self.play_for(
            FadeOut(changed_copy, previous_curve, response_legend),
            FadeIn(conclusion, shift=UP * 0.08),
            seconds=1.0,
        )
        self.play_for(Indicate(peak_copy, color=RAIN_BLUE), seconds=0.8)
        self.hold(2.2)
        self.play_for(Indicate(volume_copy, color=CHANNEL_TEAL), seconds=0.8)
        self.hold(2.0)
        self.place_footer(
            f"Conceptual mechanism only · the study uses {claims.storm_count} selected synthetic storms."
        )
        self.finish_timing()
