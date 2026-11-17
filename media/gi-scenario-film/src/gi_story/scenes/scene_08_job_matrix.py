"""Scene 08: build the verified 884-job record as a visible rectangle."""

from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Arrow,
    Brace,
    Create,
    FadeIn,
    FadeOut,
    LaggedStart,
    Line,
    Rectangle,
    Square,
    VGroup,
)

from gi_story.claims import load_claims
from gi_story.components import panel
from gi_story.theme import (
    CAUTION_OCHRE,
    CAUTION_WASH,
    CHANNEL_TEAL,
    INK_SOFT,
    LINE_GRAY,
    RAIN_BLUE,
    SURVEY_PAPER,
    TEAL_WASH,
    WATERSHED_INK,
    BaseFilmScene,
    body,
    heading,
    mono,
)

STORM_LABELS = ("BALANCED", "FRONT", "LONG", "STRESS")
STORM_COLUMN_X = (-2.1, -0.7, 0.7, 2.1)
SAMPLE_ALT_Y = (0.32, -0.15, -0.62, -1.09, -1.56)
RETAINED_ROW_LABELS = ("BASELINE", "ALT-017", "ALT-207", "ALT-109", "ALT-014")
RETAINED_ROW_Y = (0.5, 0.02, -0.46, -0.94, -1.42)


def _cell(*, fill=TEAL_WASH, stroke=CHANNEL_TEAL, size: float = 0.17) -> Square:
    return Square(
        side_length=size,
        stroke_color=stroke,
        stroke_width=1.4,
        fill_color=fill,
        fill_opacity=1,
    )


def _aligned_row(
    y_coordinate: float,
    *,
    fill=TEAL_WASH,
    stroke=CHANNEL_TEAL,
    size: float = 0.46,
) -> VGroup:
    """Return one four-cell row aligned exactly to the named storm columns."""

    row = VGroup()
    for x_coordinate in STORM_COLUMN_X:
        cell = _cell(fill=fill, stroke=stroke, size=size)
        cell.move_to(RIGHT * x_coordinate + UP * y_coordinate)
        row.add(cell)
    return row


def _dense_job_matrix() -> VGroup:
    """Render 44 compressed bands in each of four truthful storm columns."""

    top = 0.48
    bottom = -1.62
    step = (top - bottom) / 43
    columns = VGroup()
    for x_coordinate in STORM_COLUMN_X:
        column = VGroup()
        for band_index in range(44):
            band = Rectangle(
                width=0.72,
                height=0.045,
                stroke_color=CHANNEL_TEAL,
                stroke_width=0.65,
                fill_color=TEAL_WASH,
                fill_opacity=1,
            )
            band.move_to(RIGHT * x_coordinate + UP * (top - band_index * step))
            column.add(band)
        columns.add(column)
    return columns


def _retained_run_grid() -> tuple[VGroup, VGroup]:
    """Show the exact 5 model rows × 4 storms that retain full file sets."""

    grid = VGroup()
    labels = VGroup()
    for row_index, (label, y_coordinate) in enumerate(
        zip(RETAINED_ROW_LABELS, RETAINED_ROW_Y, strict=True)
    ):
        is_baseline = row_index == 0
        row = _aligned_row(
            y_coordinate,
            fill=CAUTION_WASH if is_baseline else TEAL_WASH,
            stroke=CAUTION_OCHRE if is_baseline else CHANNEL_TEAL,
            size=0.38,
        )
        label_copy = mono(
            label,
            size=18,
            color=CAUTION_OCHRE if is_baseline else CHANNEL_TEAL,
        )
        label_copy.move_to(LEFT * 4.55 + UP * y_coordinate)
        grid.add(row)
        labels.add(label_copy)
    return grid, labels


class Scene08JobMatrix(BaseFilmScene):
    """Explain that the job count is a complete design-by-storm rectangle."""

    scene_id = "08-job-matrix"
    target_duration = 50.0

    def construct(self) -> None:
        claims = load_claims()
        title = self.title_block("The execution record", "884 is a rectangle, not magic")
        badge = self.limitation_badge("VERIFIED FIXTURE REPLAY")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=1.0)
        self.play_for(FadeIn(badge), seconds=0.6)
        self.hold(1.7)

        shell = panel(12.25, 4.35, fill=SURVEY_PAPER)
        shell.move_to(DOWN * 0.34)
        storm_names = VGroup()
        for label, x_coordinate in zip(STORM_LABELS, STORM_COLUMN_X, strict=True):
            copy = mono(label, size=22, color=RAIN_BLUE)
            copy.move_to(RIGHT * x_coordinate + UP * 1.42)
            storm_names.add(copy)
        storm_line = Line(
            RIGHT * (STORM_COLUMN_X[0] - 0.56) + UP * 1.16,
            RIGHT * (STORM_COLUMN_X[-1] + 0.56) + UP * 1.16,
            color=RAIN_BLUE,
            stroke_width=3,
        )

        baseline_y = 0.88
        baseline = _aligned_row(
            baseline_y,
            fill=CAUTION_WASH,
            stroke=CAUTION_OCHRE,
            size=0.52,
        )
        baseline_label = mono("UNTOUCHED", size=22, color=INK_SOFT)
        baseline_label.move_to(LEFT * 4.7 + UP * baseline_y)

        row_label_copy = ("ALT-001", "ALT-002", "ALT-003", "⋮", "ALT-220")
        row_labels = VGroup()
        sample_rows = VGroup()
        for index, (label, y_coordinate) in enumerate(
            zip(row_label_copy, SAMPLE_ALT_Y, strict=True)
        ):
            label_copy = mono(label, size=20 if label != "⋮" else 30, color=INK_SOFT)
            label_copy.move_to(LEFT * 4.7 + UP * y_coordinate)
            row_labels.add(label_copy)
            if index == 3:
                ellipsis = mono("⋯      ⋯      ⋯      ⋯", size=25, color=INK_SOFT)
                ellipsis.move_to(UP * y_coordinate)
                sample_rows.add(ellipsis)
            else:
                sample_rows.add(_aligned_row(y_coordinate))

        self.play_for(FadeIn(shell), FadeIn(storm_names), Create(storm_line), seconds=1.6)
        self.play_for(
            LaggedStart(*(FadeIn(cell, shift=DOWN * 0.08) for cell in baseline), lag_ratio=0.2),
            FadeIn(baseline_label),
            seconds=3.0,
        )
        baseline_note = VGroup(
            mono("4", size=39, color=CAUTION_OCHRE),
            body("baseline jobs", size=25),
        ).arrange(RIGHT, buff=0.18)
        baseline_note.move_to(RIGHT * 4.55 + UP * 0.88)
        self.play_for(FadeIn(baseline_note, shift=LEFT * 0.12), seconds=1.0)
        self.hold(1.8)

        self.play_for(
            LaggedStart(*(FadeIn(row, shift=DOWN * 0.1) for row in sample_rows), lag_ratio=0.15),
            FadeIn(row_labels),
            seconds=4.0,
        )
        rows_brace = Brace(sample_rows, RIGHT, color=CHANNEL_TEAL)
        rows_count = VGroup(
            mono("220", size=38, color=CHANNEL_TEAL),
            body("alternative vectors", size=24),
        ).arrange(DOWN, buff=0.04)
        rows_count.next_to(rows_brace, RIGHT, buff=0.18)
        self.play_for(Create(rows_brace), FadeIn(rows_count), seconds=1.3)
        self.hold(1.7)

        dense = _dense_job_matrix()
        matrix_label = VGroup(
            mono("44 BANDS × 5 ROWS", size=18, color=CHANNEL_TEAL),
            heading("220 ALTERNATIVES", size=25, color=WATERSHED_INK),
            mono("× 4 STORMS = 880 JOBS", size=18, color=CHANNEL_TEAL),
        ).arrange(DOWN, buff=0.09)
        matrix_label.move_to(RIGHT * 4.52 + DOWN * 0.18)
        self.play_for(
            FadeOut(sample_rows, row_labels, rows_brace, rows_count),
            FadeIn(dense),
            FadeIn(matrix_label),
            seconds=2.8,
        )

        equation = VGroup(
            mono("4", size=47, color=CAUTION_OCHRE),
            mono("+", size=42),
            mono("220 × 4", size=47, color=CHANNEL_TEAL),
            mono("=", size=42),
            mono("884", size=58, color=WATERSHED_INK),
        ).arrange(RIGHT, buff=0.32)
        equation.to_edge(DOWN, buff=0.58)
        self.play_for(
            LaggedStart(*(FadeIn(part, shift=UP * 0.08) for part in equation), lag_ratio=0.18),
            seconds=3.6,
        )
        self.hold(2.1)

        sweep = Rectangle(
            width=STORM_COLUMN_X[-1] - STORM_COLUMN_X[0] + 0.9,
            height=0.11,
            stroke_width=0,
            fill_color=CHANNEL_TEAL,
            fill_opacity=0.35,
        )
        sweep.move_to(UP * 0.48)
        self.add(sweep)
        self.play_for(sweep.animate.move_to(DOWN * 1.62), seconds=4.2)
        self.play_for(FadeOut(sweep), seconds=0.4)
        every_cell = VGroup(
            mono("884 JOB RECORDS", size=24, color=CHANNEL_TEAL),
            body("metrics  ·  QA  ·  hashes", size=23),
        ).arrange(DOWN, buff=0.08)
        every_cell.move_to(RIGHT * 4.52 + DOWN * 0.95)
        self.play_for(FadeOut(matrix_label), FadeIn(every_cell), seconds=1.0)
        self.hold(2.1)

        retained_files, retained_labels = _retained_run_grid()
        if sum(len(row) for row in retained_files) != claims.retained_jobs:
            raise ValueError("Retained-run grid no longer matches the fixture claim")
        retained_key = VGroup(
            heading(str(claims.retained_jobs), size=42, color=CAUTION_OCHRE),
            mono("RETAINED RUN SETS", size=20, color=CAUTION_OCHRE),
            mono("5 ROWS × 4 STORMS", size=18, color=WATERSHED_INK),
            body("1 baseline row", size=18, color=INK_SOFT),
            body("+ 4 teaching alternatives", size=18, color=INK_SOFT),
            mono("INP · RPT · OUT EACH", size=17, color=CHANNEL_TEAL),
        ).arrange(DOWN, buff=0.06)
        retained_key.move_to(RIGHT * 4.45 + DOWN * 0.38)
        self.play_for(
            FadeOut(baseline_note, every_cell, baseline, baseline_label, dense),
            seconds=0.8,
        )
        self.play_for(
            LaggedStart(
                *(FadeIn(file_token) for row in retained_files for file_token in row),
                lag_ratio=0.08,
            ),
            FadeIn(retained_labels),
            FadeIn(retained_key),
            seconds=3.4,
        )
        self.hold(2.1)

        mode_line = Line(equation.get_left(), equation.get_right(), color=LINE_GRAY, stroke_width=2)
        mode_line.next_to(equation, UP, buff=0.18)
        replay = VGroup(
            mono("OPEN FINISHED RECORD", size=23, color=CHANNEL_TEAL),
            body("verified replay", size=30, color=WATERSHED_INK),
        ).arrange(DOWN, buff=0.05)
        rerun = VGroup(
            mono("EXECUTE AGAIN", size=23, color=CAUTION_OCHRE),
            body("new local rerun", size=30, color=WATERSHED_INK),
        ).arrange(DOWN, buff=0.05)
        modes = VGroup(replay, rerun).arrange(RIGHT, buff=2.2)
        modes.to_edge(DOWN, buff=0.44)
        connector = Arrow(
            replay.get_right(), rerun.get_left(), buff=0.3, color=LINE_GRAY, stroke_width=3
        )
        self.play_for(FadeOut(equation), seconds=0.6)
        self.play_for(Create(mode_line), FadeIn(replay), seconds=1.1)
        self.play_for(Create(connector), FadeIn(rerun), seconds=1.7)
        self.hold(1.8)

        conclusion = heading(
            "A REPLAY OPENS PRIOR COMPUTED RESULTS",
            size=39,
            color=WATERSHED_INK,
        )
        sub = body("A rerun creates a new execution record.", size=29, color=INK_SOFT)
        conclusion_group = VGroup(conclusion, sub).arrange(DOWN, buff=0.1)
        conclusion_group.to_edge(DOWN, buff=0.52)
        self.play_for(FadeOut(mode_line, modes, connector), FadeIn(conclusion_group), seconds=1.5)
        self.hold(2.0)
        self.finish_timing()
