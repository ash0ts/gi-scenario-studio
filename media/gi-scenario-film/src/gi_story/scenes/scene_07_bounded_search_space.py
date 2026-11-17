"""Scene 07: frame bounded candidate selection as unanswered vector cards."""

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
    LaggedStart,
    Line,
    RoundedRectangle,
    SurroundingRectangle,
    TransformFromCopy,
    VGroup,
)

from gi_story.claims import load_claims
from gi_story.theme import (
    CAUTION_OCHRE,
    CAUTION_WASH,
    CHANNEL_TEAL,
    INK_SOFT,
    LINE_GRAY,
    RAIN_BLUE,
    RAIN_WASH,
    SURVEY_PAPER,
    WATERSHED_INK,
    BaseFilmScene,
    body,
    heading,
    mono,
)


def _choice_card(name: str, bounds: str, choices: int) -> VGroup:
    """One engineer-approved integer range at its final readable size."""

    shell = RoundedRectangle(
        width=2.82,
        height=1.55,
        corner_radius=0.11,
        stroke_color=RAIN_BLUE,
        stroke_width=2.5,
        fill_color=SURVEY_PAPER,
        fill_opacity=1,
    )
    name_copy = heading(name, size=24, color=WATERSHED_INK)
    name_copy.move_to(shell.get_center() + UP * 0.42)
    range_copy = mono(bounds, size=32, color=RAIN_BLUE)
    range_copy.move_to(shell.get_center() + UP * 0.02)
    count_copy = body(f"{choices} integer choices", size=20, color=INK_SOFT)
    count_copy.move_to(shell.get_center() + DOWN * 0.41)
    return VGroup(shell, name_copy, range_copy, count_copy)


def _card_back(*, width: float = 0.64, height: float = 0.35) -> VGroup:
    """A result-side-down candidate card with no illegible micro-copy."""

    shell = RoundedRectangle(
        width=width,
        height=height,
        corner_radius=0.07,
        stroke_color=CHANNEL_TEAL,
        stroke_width=2,
        fill_color=RAIN_WASH,
        fill_opacity=1,
    )
    thread = Line(
        shell.get_left() + RIGHT * 0.14,
        shell.get_right() + LEFT * 0.14,
        color=CHANNEL_TEAL,
        stroke_width=2,
    )
    return VGroup(shell, thread)


def _storm_tokens(*, width: float = 0.55, height: float = 0.34) -> VGroup:
    """Four labelled tokens that keep the selected-storm dimension explicit."""

    tokens = VGroup()
    for label in ("B", "F", "L", "S"):
        shell = RoundedRectangle(
            width=width,
            height=height,
            corner_radius=0.06,
            stroke_color=RAIN_BLUE,
            stroke_width=2,
            fill_color=RAIN_WASH,
            fill_opacity=1,
        )
        copy = mono(label, size=18, color=RAIN_BLUE).move_to(shell)
        tokens.add(VGroup(shell, copy))
    return tokens.arrange(RIGHT, buff=0.12)


def _known_question_deck(question_count: int) -> VGroup:
    """A readable card field with independent title, deck, and explanation lanes."""

    shell = RoundedRectangle(
        width=9.3,
        height=3.4,
        corner_radius=0.15,
        stroke_color=CHANNEL_TEAL,
        stroke_width=3,
        fill_color=SURVEY_PAPER,
        fill_opacity=1,
    )
    shell.move_to(DOWN * 0.28)
    title = heading(
        f"{question_count} KNOWN QUESTIONS",
        size=35,
        color=CHANNEL_TEAL,
    )
    title.move_to(shell.get_center() + UP * 1.23)
    deck = VGroup(*(_card_back() for _ in range(32)))
    deck.arrange_in_grid(rows=4, cols=8, buff=(0.14, 0.1))
    deck.move_to(shell.get_center() + DOWN * 0.1)
    note = body(
        "The vector is visible. Only its SWMM result is face-down.",
        size=23,
        color=INK_SOFT,
    )
    note.move_to(shell.get_center() + DOWN * 1.31)
    return VGroup(shell, title, deck, note)


def _study_panel(*, accent, title: str, formula: str, detail: str) -> VGroup:
    shell = RoundedRectangle(
        width=5.78,
        height=3.12,
        corner_radius=0.13,
        stroke_color=accent,
        stroke_width=3,
        fill_color=SURVEY_PAPER,
        fill_opacity=1,
    )
    title_copy = mono(title, size=22, color=accent)
    title_copy.move_to(shell.get_center() + UP * 1.03)
    formula_copy = mono(formula, size=34, color=WATERSHED_INK)
    formula_copy.move_to(shell.get_center() + UP * 0.34)
    detail_copy = body(detail, size=22, color=INK_SOFT)
    detail_copy.move_to(shell.get_center() + DOWN * 0.4)
    return VGroup(shell, title_copy, formula_copy, detail_copy)


def _process_chip(label: str) -> VGroup:
    shell = RoundedRectangle(
        width=2.25,
        height=0.65,
        corner_radius=0.1,
        stroke_color=CHANNEL_TEAL,
        stroke_width=2,
        fill_color=SURVEY_PAPER,
        fill_opacity=1,
    )
    copy = mono(label, size=20, color=CHANNEL_TEAL).move_to(shell)
    return VGroup(shell, copy)


def _question_card(vector: str) -> VGroup:
    """A selected vector whose known question and unknown answer stay contained."""

    shell = RoundedRectangle(
        width=3.85,
        height=1.28,
        corner_radius=0.12,
        stroke_color=CAUTION_OCHRE,
        stroke_width=3,
        fill_color=CAUTION_WASH,
        fill_opacity=1,
    )
    copy = VGroup(
        mono("SEARCH ASKS", size=20, color=CAUTION_OCHRE),
        mono(f"x = {vector}", size=22, color=WATERSHED_INK),
        body("SWMM result still unknown", size=20, color=INK_SOFT),
    ).arrange(DOWN, buff=0.08)
    copy.move_to(shell)
    return VGroup(shell, copy)


class Scene07BoundedSearchSpace(BaseFilmScene):
    """Search chooses the next bounded question; SWMM reveals its answer."""

    scene_id = "07-bounded-search-space"
    target_duration = 75.0

    def construct(self) -> None:
        claims = load_claims()

        title = self.title_block(
            "Bounded search",
            "Every hydraulic answer begins face-down",
            title_size=54,
        )
        badge = self.limitation_badge("EXACT STUDY COUNTS · NO ML PERFORMANCE CLAIM")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=0.8)
        self.play_for(FadeIn(badge), seconds=0.5)
        self.hold(1.2)

        practice_specs = (
            ("Bioretention", "0 … 4", claims.candidate_choice_counts[0]),
            ("Permeable pavement", "0 … 3", claims.candidate_choice_counts[1]),
            ("Rain garden", "0 … 4", claims.candidate_choice_counts[2]),
            ("Rain barrels", "0 … 6", claims.candidate_choice_counts[3]),
        )
        practices = VGroup()
        count_labels = VGroup()
        x_positions = (-4.65, -1.55, 1.55, 4.65)
        for (name, bounds, choices), x_coordinate in zip(practice_specs, x_positions, strict=True):
            card = _choice_card(name, bounds, choices)
            card.move_to(RIGHT * x_coordinate + UP * 0.55)
            practices.add(card)
            count_labels.add(card[-1])
            self.play_for(FadeIn(card, shift=UP * 0.08), seconds=0.55)
            self.play_for(Indicate(card[2], color=RAIN_BLUE), seconds=0.45)
            self.hold(0.35)

        equation = VGroup(
            mono("5", size=43, color=RAIN_BLUE),
            mono("×", size=34),
            mono("4", size=43, color=RAIN_BLUE),
            mono("×", size=34),
            mono("5", size=43, color=RAIN_BLUE),
            mono("×", size=34),
            mono("7", size=43, color=RAIN_BLUE),
            mono("=", size=34),
            mono(str(claims.universe_vectors), size=50, color=CHANNEL_TEAL),
        ).arrange(RIGHT, buff=0.2)
        equation.move_to(DOWN * 1.28)
        equation_note = body(
            "engineer-bounded integer vectors",
            size=23,
            color=INK_SOFT,
        )
        equation_note.next_to(equation, DOWN, buff=0.18)
        self.play_for(
            LaggedStart(*(FadeIn(term) for term in equation), lag_ratio=0.08),
            seconds=1.2,
        )
        self.play_for(FadeIn(equation_note), seconds=0.5)
        self.hold(1.0)

        self.play_for(
            FadeOut(practices),
            FadeOut(equation),
            FadeOut(equation_note),
            seconds=0.7,
        )
        deck_scene = _known_question_deck(claims.universe_vectors)
        deck_shell, deck_title, deck, deck_note = deck_scene
        self.play_for(FadeIn(deck_shell), FadeIn(deck_title), seconds=0.7)
        self.play_for(
            LaggedStart(*(FadeIn(card) for card in deck), lag_ratio=0.025),
            seconds=1.3,
        )
        self.play_for(FadeIn(deck_note), Indicate(deck_title, color=CHANNEL_TEAL), seconds=0.7)
        self.hold(1.7)

        self.play_for(FadeOut(deck_scene), seconds=0.7)
        benchmark = _study_panel(
            accent=RAIN_BLUE,
            title="COMPLETE BENCHMARK DATASET",
            formula=f"{claims.universe_vectors} × {claims.storm_count} = {claims.exhaustive_jobs:,}",
            detail="all vectors × all four storms",
        )
        benchmark.move_to(LEFT * 3.08 + DOWN * 0.2)
        benchmark_jobs = mono("DIRECT SWMM JOBS", size=22, color=RAIN_BLUE)
        benchmark_jobs.move_to(benchmark.get_center() + DOWN * 1.02)
        benchmark_storms = _storm_tokens()
        benchmark[2].move_to(benchmark.get_center() + UP * 0.04)
        benchmark_storms.move_to(benchmark.get_center() + UP * 0.56)
        self.play_for(FadeIn(benchmark, shift=RIGHT * 0.1), seconds=0.7)
        self.play_for(
            LaggedStart(*(FadeIn(storm) for storm in benchmark_storms), lag_ratio=0.16),
            seconds=0.7,
        )
        self.play_for(FadeIn(benchmark_jobs), seconds=0.5)
        self.play_for(Indicate(benchmark[2], color=RAIN_BLUE), seconds=0.7)
        self.hold(1.5)
        self.play_for(Indicate(benchmark_jobs, color=RAIN_BLUE), seconds=0.7)
        self.hold(0.8)

        guided = _study_panel(
            accent=CAUTION_OCHRE,
            title="DETERMINISTIC GUIDED REPLAY",
            formula=f"{claims.storm_count} + {claims.guided_alternatives} × {claims.storm_count} = {claims.guided_jobs}",
            detail="baseline jobs + alternative jobs",
        )
        guided.move_to(RIGHT * 3.08 + DOWN * 0.2)
        guided_jobs = mono("DIRECT SWMM JOBS", size=22, color=CAUTION_OCHRE)
        guided_jobs.move_to(guided.get_center() + DOWN * 1.02)
        guided[2].move_to(guided.get_center() + UP * 0.04)
        guided_flags = mono(
            "NONEXHAUSTIVE · NOT ADAPTIVE ML",
            size=20,
            color=CAUTION_OCHRE,
        )
        guided_flags.move_to(guided.get_center() + UP * 0.58)
        divider = Line(UP * 1.75, DOWN * 2.0, color=LINE_GRAY, stroke_width=2)
        self.play_for(Create(divider), FadeIn(guided, shift=LEFT * 0.1), seconds=0.8)
        self.play_for(FadeIn(guided_flags), seconds=0.7)
        self.play_for(FadeIn(guided_jobs), seconds=0.5)
        self.play_for(Indicate(guided[2], color=CAUTION_OCHRE), seconds=0.7)
        self.hold(1.6)
        self.play_for(Indicate(guided_flags, color=CAUTION_OCHRE), seconds=0.7)
        self.hold(0.8)

        replay_arithmetic = VGroup(
            mono("4 BASELINE JOBS", size=21, color=RAIN_BLUE),
            mono("+", size=22, color=WATERSHED_INK),
            mono("220 × 4 ALTERNATIVE JOBS", size=21, color=CAUTION_OCHRE),
        ).arrange(RIGHT, buff=0.24)
        replay_arithmetic.move_to(DOWN * 2.33)
        self.play_for(FadeIn(replay_arithmetic, shift=UP * 0.08), seconds=0.7)
        self.play_for(Indicate(replay_arithmetic[-1], color=CAUTION_OCHRE), seconds=0.7)
        self.hold(1.2)
        self.play_for(Indicate(replay_arithmetic[0], color=RAIN_BLUE), seconds=0.7)
        self.hold(0.8)
        no_optimum = body(
            "A finite study record is not a global real-world optimum claim.",
            size=23,
            color=INK_SOFT,
        )
        no_optimum.move_to(DOWN * 2.82)
        self.play_for(FadeIn(no_optimum), seconds=0.7)
        self.hold(1.5)
        self.play_for(Indicate(no_optimum, color=CAUTION_OCHRE), seconds=0.7)
        self.hold(0.8)

        studies = VGroup(
            benchmark,
            benchmark_storms,
            benchmark_jobs,
            guided,
            guided_flags,
            guided_jobs,
            divider,
            replay_arithmetic,
            no_optimum,
        )
        self.play_for(FadeOut(studies), seconds=0.7)
        workflow_label = VGroup(
            mono("POSSIBLE FUTURE WORKFLOW", size=22, color=CAUTION_OCHRE),
            body("Search orders the next exact question — never invents its answer.", size=23),
        ).arrange(DOWN, buff=0.1)
        workflow_label.move_to(UP * 1.62)
        self.play_for(FadeIn(workflow_label), seconds=0.7)
        self.hold(0.8)

        small_deck = VGroup(*(_card_back(width=0.66, height=0.44) for _ in range(9)))
        small_deck.arrange_in_grid(rows=3, cols=3, buff=(0.13, 0.13))
        small_deck.move_to(LEFT * 5.25 + DOWN * 0.05)
        deck_caption = VGroup(
            mono("SEARCH", size=23, color=CAUTION_OCHRE),
            body("orders candidates", size=21, color=INK_SOFT),
        ).arrange(DOWN, buff=0.08)
        deck_caption.next_to(small_deck, UP, buff=0.26)
        search_modes = VGroup(
            mono("BENCHMARKS PASS", size=18, color=CAUTION_OCHRE),
            mono("KEEP EXPLORING", size=18, color=CAUTION_OCHRE),
        ).arrange(DOWN, buff=0.1)
        search_modes.next_to(small_deck, DOWN, buff=0.22)
        self.play_for(FadeIn(small_deck), FadeIn(deck_caption), seconds=0.7)
        self.play_for(FadeIn(search_modes), seconds=0.6)
        self.hold(1.0)
        for mode in search_modes:
            self.play_for(Indicate(mode, color=CAUTION_OCHRE), seconds=0.5)
            self.hold(0.5)

        scan_order = (small_deck[1], small_deck[7], small_deck[3])
        for card in scan_order:
            self.play_for(Indicate(card, color=CAUTION_OCHRE, scale_factor=1.08), seconds=0.55)
            self.hold(0.35)

        selected = small_deck[4]
        selector = SurroundingRectangle(
            selected,
            color=CAUTION_OCHRE,
            stroke_width=4,
            buff=0.08,
            corner_radius=0.08,
        )
        self.play_for(Create(selector), seconds=0.7)
        self.hold(0.7)

        question_card = _question_card("[0, 3, 2, 6]")
        question_card.move_to(LEFT * 2.05 + DOWN * 0.05)
        question_shell, question_copy = question_card
        self.play_for(TransformFromCopy(selected, question_shell), seconds=0.8)
        self.play_for(FadeIn(question_copy), seconds=0.6)
        self.hold(1.1)
        self.play_for(Indicate(question_copy[1], color=CAUTION_OCHRE), seconds=0.6)
        self.hold(0.7)

        swmm_shell = RoundedRectangle(
            width=2.15,
            height=1.48,
            corner_radius=0.12,
            stroke_color=CHANNEL_TEAL,
            stroke_width=3,
            fill_color=SURVEY_PAPER,
            fill_opacity=1,
        )
        swmm_shell.move_to(RIGHT * 1.5 + DOWN * 0.05)
        swmm_copy = VGroup(
            heading("SWMM", size=32, color=CHANNEL_TEAL),
            mono(claims.solver_version, size=21, color=WATERSHED_INK),
            body("direct execution", size=20, color=INK_SOFT),
        ).arrange(DOWN, buff=0.08)
        swmm_copy.move_to(swmm_shell)
        question_arrow = Arrow(
            question_shell.get_right(),
            swmm_shell.get_left(),
            buff=0.04,
            color=CHANNEL_TEAL,
            stroke_width=4,
            max_tip_length_to_length_ratio=0.3,
        )
        self.play_for(Create(question_arrow), FadeIn(swmm_shell), FadeIn(swmm_copy), seconds=0.8)
        self.play_for(Circumscribe(swmm_shell, color=CHANNEL_TEAL, buff=0.06), seconds=0.8)
        self.play_for(Indicate(swmm_copy[2], color=CHANNEL_TEAL), seconds=0.6)
        self.hold(0.7)

        storm_tokens = _storm_tokens(width=0.45, height=0.32)
        storm_context = VGroup(
            mono("4 STORMS", size=18, color=RAIN_BLUE),
            storm_tokens,
        ).arrange(RIGHT, buff=0.15)
        storm_context.next_to(swmm_shell, UP, buff=0.16)
        self.play_for(
            FadeIn(storm_context[0]),
            LaggedStart(*(FadeIn(token) for token in storm_tokens), lag_ratio=0.18),
            seconds=0.8,
        )
        self.hold(1.1)

        result_shell = RoundedRectangle(
            width=3.55,
            height=1.78,
            corner_radius=0.12,
            stroke_color=RAIN_BLUE,
            stroke_width=3,
            fill_color=RAIN_WASH,
            fill_opacity=1,
        )
        result_shell.move_to(RIGHT * 4.75 + DOWN * 0.05)
        result_copy = VGroup(
            mono("SWMM REVEALS", size=20, color=RAIN_BLUE),
            heading("MODELED", size=25, color=WATERSHED_INK),
            heading("HYDRAULICS", size=25, color=WATERSHED_INK),
            body("peak · volume · time series", size=20, color=INK_SOFT),
        ).arrange(DOWN, buff=0.04)
        result_copy.move_to(result_shell)
        result_arrow = Arrow(
            swmm_shell.get_right(),
            result_shell.get_left(),
            buff=0.04,
            color=CHANNEL_TEAL,
            stroke_width=4,
            max_tip_length_to_length_ratio=0.3,
        )
        self.play_for(Create(result_arrow), seconds=0.7)
        self.play_for(FadeIn(result_shell), FadeIn(result_copy, shift=RIGHT * 0.08), seconds=0.9)
        self.play_for(
            Indicate(VGroup(result_copy[1], result_copy[2]), color=RAIN_BLUE), seconds=0.7
        )
        self.hold(1.2)
        self.play_for(Indicate(result_copy[-1], color=RAIN_BLUE), seconds=0.6)
        self.hold(0.7)

        process = VGroup(
            _process_chip("BUDGET"),
            _process_chip("DEDUPLICATE"),
            _process_chip("CACHE"),
        ).arrange(RIGHT, buff=0.62)
        process.move_to(DOWN * 1.85)
        process_arrows = VGroup(
            Arrow(
                process[0].get_right(),
                process[1].get_left(),
                buff=0.08,
                color=CHANNEL_TEAL,
                stroke_width=3,
                max_tip_length_to_length_ratio=0.18,
            ),
            Arrow(
                process[1].get_right(),
                process[2].get_left(),
                buff=0.08,
                color=CHANNEL_TEAL,
                stroke_width=3,
                max_tip_length_to_length_ratio=0.18,
            ),
        )
        process_note = body("avoid repeated direct work", size=21, color=INK_SOFT)
        process_note.next_to(process, DOWN, buff=0.18)
        self.play_for(
            LaggedStart(*(FadeIn(chip) for chip in process), lag_ratio=0.18),
            seconds=0.7,
        )
        self.play_for(Create(process_arrows), FadeIn(process_note), seconds=0.7)
        self.hold(1.0)
        for chip in process:
            self.play_for(Circumscribe(chip, color=CHANNEL_TEAL, buff=0.04), seconds=0.5)
            self.hold(0.3)

        self.play_for(FadeOut(process), FadeOut(process_arrows), FadeOut(process_note), seconds=0.6)
        boundary = VGroup(
            heading("SEARCH CHOOSES THE CARD.", size=28, color=CAUTION_OCHRE),
            heading("SWMM COMPUTES THE ANSWER.", size=28, color=CHANNEL_TEAL),
        ).arrange(RIGHT, buff=0.45)
        boundary.move_to(DOWN * 2.02)
        self.play_for(FadeIn(boundary, shift=UP * 0.08), seconds=0.8)
        self.hold(1.2)
        for statement in boundary:
            self.play_for(Indicate(statement, scale_factor=1.015), seconds=0.55)
            self.hold(0.3)
        limitation = mono(
            "CEDAR TRACE: NO ADAPTIVE-ML PERFORMANCE OR CARBON CLAIM",
            size=20,
            color=CAUTION_OCHRE,
        )
        limitation.move_to(DOWN * 2.72)
        self.play_for(FadeIn(limitation), seconds=0.7)
        self.hold(1.3)
        self.play_for(Indicate(deck_caption, color=CAUTION_OCHRE), seconds=0.6)
        self.play_for(Circumscribe(swmm_shell, color=CHANNEL_TEAL, buff=0.06), seconds=0.6)
        self.play_for(Circumscribe(result_shell, color=RAIN_BLUE, buff=0.06), seconds=0.6)
        self.place_footer(
            "Search never supplies a displayed hydraulic value; every value comes from SWMM."
        )
        self.hold(0.8)
        self.finish_timing()
