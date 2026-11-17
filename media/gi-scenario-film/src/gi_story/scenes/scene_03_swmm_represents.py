"""Scene 03: separate the model SWMM solves from evidence built afterward."""

from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Arrow,
    Create,
    FadeIn,
    FadeOut,
    Indicate,
    LaggedStart,
    Line,
    Polygon,
    Rectangle,
    RoundedRectangle,
    VGroup,
)

from gi_story.claims import load_claims
from gi_story.components import labeled_panel
from gi_story.theme import (
    CAUTION_OCHRE,
    CAUTION_WASH,
    CHANNEL_TEAL,
    INK_SOFT,
    LINE_GRAY,
    RAIN_BLUE,
    RAIN_WASH,
    SURVEY_PAPER,
    TEAL_WASH,
    WATERSHED_INK,
    BaseFilmScene,
    body,
    heading,
    mono,
)


def _evidence_row(label: str, detail: str) -> VGroup:
    """A readable post-processing result row, distinct from SWMM output."""

    shell = RoundedRectangle(
        width=3.9,
        height=0.52,
        corner_radius=0.08,
        stroke_color=LINE_GRAY,
        stroke_width=2,
        fill_color=SURVEY_PAPER,
        fill_opacity=1,
    )
    label_copy = heading(label, size=20, color=RAIN_BLUE)
    detail_copy = body(detail, size=18, color=INK_SOFT)
    copy = VGroup(label_copy, detail_copy).arrange(RIGHT, buff=0.18)
    if copy.width > shell.width - 0.3:
        copy.scale_to_fit_width(shell.width - 0.3)
    copy.move_to(shell)
    return VGroup(shell, copy)


class Scene03SwmmRepresents(BaseFilmScene):
    """Show where direct SWMM execution stops and evidence processing begins."""

    scene_id = "03-swmm-represents"
    target_duration = 60.0

    def construct(self) -> None:
        claims = load_claims()
        title = VGroup(
            mono("MODEL BOUNDARY", size=22, color=CHANNEL_TEAL),
            heading("What information does SWMM receive?", size=50),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.22)
        title.to_edge(UP, buff=0.56).to_edge(LEFT, buff=0.76)
        badge = self.limitation_badge("MODEL ≠ FIELD TRUTH")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=1.0)
        self.play_for(FadeIn(badge), seconds=0.7)
        self.hold(2.2)

        terrain = VGroup(
            Polygon(
                LEFT * 5.8 + UP * 0.9,
                LEFT * 1.1 + UP * 0.45,
                LEFT * 1.1 + DOWN * 1.2,
                LEFT * 5.8 + DOWN * 1.2,
                color=RAIN_BLUE,
                stroke_width=3,
                fill_color=RAIN_WASH,
                fill_opacity=0.7,
            ),
            Polygon(
                LEFT * 1.1 + UP * 0.45,
                RIGHT * 3.8 + UP * 0.75,
                RIGHT * 3.8 + DOWN * 1.2,
                LEFT * 1.1 + DOWN * 1.2,
                color=CHANNEL_TEAL,
                stroke_width=3,
                fill_color=TEAL_WASH,
                fill_opacity=0.65,
            ),
        )
        buildings = VGroup()
        for x in (-4.8, -3.5, -2.0, 0.2, 1.7, 3.0):
            building = Rectangle(
                width=0.62,
                height=0.48,
                stroke_color=WATERSHED_INK,
                stroke_width=2,
                fill_color=SURVEY_PAPER,
                fill_opacity=1,
            )
            building.move_to(RIGHT * x + UP * 0.2)
            buildings.add(building)
        surface_path = Line(
            LEFT * 5.2 + DOWN * 0.55,
            RIGHT * 3.2 + DOWN * 0.72,
            color=RAIN_BLUE,
            stroke_width=8,
        )
        physical_label = VGroup(
            heading("the neighborhood we want to understand", size=31),
            body("land · buildings · a drainage path", size=23),
        ).arrange(DOWN, buff=0.08)
        physical_label.to_edge(DOWN, buff=0.76)
        self.play_for(Create(terrain), seconds=2.2)
        self.play_for(
            LaggedStart(*(FadeIn(item, shift=UP * 0.12) for item in buildings), lag_ratio=0.1),
            Create(surface_path),
            seconds=2.8,
        )
        self.play_for(FadeIn(physical_label), seconds=0.7)
        self.hold(2.6)

        object_data = (
            ("RUNOFF AREA", "area · slope · losses", RAIN_BLUE),
            ("JUNCTION", "invert · storage", CHANNEL_TEAL),
            ("PIPE", "diameter · slope · roughness", CHANNEL_TEAL),
            ("OUTFALL", "downstream boundary", CAUTION_OCHRE),
        )
        model_objects = VGroup(
            *(
                labeled_panel(label, detail, width=2.9, height=1.55, accent=color)
                for label, detail, color in object_data
            )
        ).arrange(RIGHT, buff=0.22)
        model_objects.move_to(DOWN * 0.25)
        representation_copy = heading(
            "the engineer declares model objects",
            size=35,
            color=CHANNEL_TEAL,
        )
        representation_copy.next_to(model_objects, UP, buff=0.4)

        # A staged cut keeps every intermediate frame intelligible. Complex
        # terrain polygons do not morph into letter outlines or card geometry.
        self.play_for(FadeOut(physical_label), seconds=0.4)
        self.play_for(FadeOut(terrain, buildings, surface_path, shift=DOWN * 0.08), seconds=0.9)
        self.play_for(
            LaggedStart(*(FadeIn(item, shift=UP * 0.12) for item in model_objects), lag_ratio=0.14),
            FadeIn(representation_copy, shift=UP * 0.08),
            seconds=2.2,
        )
        self.hold(2.8)

        rain_token = labeled_panel(
            "RAIN THROUGH TIME",
            "one selected storm",
            width=3.1,
            height=1.25,
            accent=RAIN_BLUE,
        )
        lid_token = labeled_panel(
            "APPROVED GI RULES",
            "fixed properties + routing",
            width=3.45,
            height=1.25,
            accent=CHANNEL_TEAL,
        )
        vector_token = labeled_panel(
            "CANDIDATE COUNTS",
            "units at each approved site",
            width=3.15,
            height=1.25,
            accent=CAUTION_OCHRE,
        )
        extra_inputs = VGroup(rain_token, lid_token, vector_token).arrange(RIGHT, buff=0.32)
        extra_inputs.move_to(UP * 0.75)
        self.play_for(
            model_objects.animate.scale(0.72).to_edge(DOWN, buff=1.35),
            FadeOut(representation_copy),
            seconds=1.2,
        )
        self.play_for(
            LaggedStart(
                *(FadeIn(item, shift=DOWN * 0.12) for item in extra_inputs), lag_ratio=0.16
            ),
            seconds=2.2,
        )
        declared_copy = heading("other declared inputs", size=31, color=CAUTION_OCHRE)
        declared_copy.next_to(extra_inputs, UP, buff=0.18)
        self.play_for(FadeIn(declared_copy), seconds=0.5)
        self.hold(2.7)

        self.play_for(
            FadeOut(declared_copy, extra_inputs, model_objects),
            seconds=1.2,
        )

        solver_boundary = RoundedRectangle(
            width=7.55,
            height=2.45,
            corner_radius=0.18,
            stroke_color=CHANNEL_TEAL,
            stroke_width=4,
            fill_opacity=0,
        )
        solver_boundary.move_to(LEFT * 2.55 + UP * 0.55)
        solver_label = heading("DIRECT MODEL RUN", size=25, color=CHANNEL_TEAL)
        solver_label.move_to(solver_boundary.get_top() + DOWN * 0.28)
        self.play_for(Create(solver_boundary), FadeIn(solver_label), seconds=0.9)

        input_card = labeled_panel(
            "MODEL + RAIN + PLAN",
            "one direct run",
            width=2.75,
            height=1.25,
            accent=RAIN_BLUE,
        )
        input_card.move_to(LEFT * 4.92 + UP * 0.28)
        swmm_card = labeled_panel(
            f"EPA SWMM {claims.solver_version}",
            "solves declared model",
            width=2.35,
            height=1.25,
            accent=CHANNEL_TEAL,
        )
        swmm_card.move_to(LEFT * 2.15 + UP * 0.28)
        raw_card = labeled_panel(
            "RESULT FILES",
            "report + output",
            width=2.0,
            height=1.25,
            accent=CAUTION_OCHRE,
        )
        raw_card.move_to(RIGHT * 0.12 + UP * 0.28)
        arrow_one = Arrow(
            input_card.get_right(),
            swmm_card.get_left(),
            buff=0.12,
            color=CHANNEL_TEAL,
            stroke_width=4,
        )
        arrow_two = Arrow(
            swmm_card.get_right(),
            raw_card.get_left(),
            buff=0.12,
            color=CHANNEL_TEAL,
            stroke_width=4,
        )
        self.play_for(FadeIn(input_card, shift=RIGHT * 0.08), seconds=0.8)
        self.play_for(Create(arrow_one), FadeIn(swmm_card, shift=RIGHT * 0.08), seconds=1.4)
        self.play_for(Create(arrow_two), FadeIn(raw_card, shift=RIGHT * 0.08), seconds=1.2)
        self.hold(2.4)

        evidence_boundary = RoundedRectangle(
            width=4.65,
            height=2.45,
            corner_radius=0.18,
            stroke_color=RAIN_BLUE,
            stroke_width=4,
            fill_color=RAIN_WASH,
            fill_opacity=0.22,
        )
        evidence_boundary.move_to(RIGHT * 4.05 + DOWN * 0.55)
        evidence_label = heading("READ + CHECK", size=24, color=RAIN_BLUE)
        evidence_label.move_to(evidence_boundary.get_top() + DOWN * 0.28)
        evidence_detail = body(
            "reads files · never invents flows",
            size=18,
            color=INK_SOFT,
        )
        evidence_detail.next_to(evidence_label, DOWN, buff=0.03)
        evidence_rows = VGroup(
            _evidence_row("RESULTS", "volume · peak"),
            _evidence_row("RUN CHECK", "accepted / rejected"),
            _evidence_row("DIGITAL FINGERPRINTS", "file IDs"),
        ).arrange(DOWN, buff=0.08)
        evidence_rows.move_to(RIGHT * 4.05 + DOWN * 0.88)
        read_arrow = Arrow(
            raw_card.get_right(),
            evidence_boundary.get_left() + UP * 0.3,
            buff=0.08,
            color=RAIN_BLUE,
            stroke_width=4,
        )
        self.play_for(
            Create(evidence_boundary),
            FadeIn(evidence_label, evidence_detail),
            seconds=1.0,
        )
        self.play_for(Create(read_arrow), seconds=0.9)
        self.play_for(
            LaggedStart(
                *(FadeIn(row, shift=RIGHT * 0.08) for row in evidence_rows), lag_ratio=0.16
            ),
            seconds=2.0,
        )
        self.hold(2.5)
        self.play_for(Indicate(raw_card, color=CAUTION_OCHRE), seconds=0.8)
        self.hold(1.2)
        self.play_for(Indicate(evidence_rows, color=RAIN_BLUE), seconds=0.8)
        self.hold(1.2)

        pipeline = VGroup(
            solver_boundary,
            solver_label,
            input_card,
            arrow_one,
            swmm_card,
            arrow_two,
            raw_card,
            evidence_boundary,
            evidence_label,
            evidence_detail,
            read_arrow,
            evidence_rows,
        )
        self.play_for(pipeline.animate.shift(UP * 0.3), seconds=0.8)

        outside_heading = body("field evidence remains outside both boundaries", size=22)
        outside_heading.move_to(DOWN * 1.78)
        outside_tokens = VGroup(
            mono("survey", size=22, color=CAUTION_OCHRE),
            mono("field soils", size=22, color=CAUTION_OCHRE),
            mono("calibration", size=22, color=CAUTION_OCHRE),
            mono("utilities", size=22, color=CAUTION_OCHRE),
        ).arrange(RIGHT, buff=0.62)
        outside_tokens.next_to(outside_heading, DOWN, buff=0.1)
        outside_backing = RoundedRectangle(
            width=outside_tokens.width + 0.45,
            height=outside_heading.height + outside_tokens.height + 0.34,
            corner_radius=0.1,
            stroke_color=CAUTION_OCHRE,
            stroke_width=2,
            fill_color=CAUTION_WASH,
            fill_opacity=0.55,
        )
        outside_backing.move_to(VGroup(outside_heading, outside_tokens))
        outside_group = VGroup(outside_backing, outside_heading, outside_tokens)
        self.play_for(FadeIn(outside_group, shift=UP * 0.08), seconds=1.8)
        self.hold(2.6)

        conclusion = VGroup(
            heading("Exact calculation", size=35, color=CHANNEL_TEAL),
            mono("≠", size=37, color=CAUTION_OCHRE),
            heading("field truth", size=35),
        ).arrange(RIGHT, buff=0.24)
        conclusion.to_edge(DOWN, buff=0.7)
        self.play_for(FadeIn(conclusion, shift=UP * 0.1), seconds=1.3)
        self.play_for(
            solver_boundary.animate.set_stroke(width=6),
            evidence_boundary.animate.set_stroke(width=6),
            seconds=0.4,
        )
        self.play_for(
            solver_boundary.animate.set_stroke(width=4),
            evidence_boundary.animate.set_stroke(width=4),
            seconds=0.4,
        )
        self.hold(2.4)
        self.play_for(Indicate(outside_group, color=CAUTION_OCHRE), seconds=0.9)
        self.hold(2.0)
        self.place_footer(
            "Topology, dimensions, losses, and boundary conditions remain engineering assumptions."
        )
        self.finish_timing()
