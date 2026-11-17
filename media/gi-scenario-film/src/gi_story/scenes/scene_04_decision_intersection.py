"""Scene 04: show four simultaneous rules as one Boolean AND decision."""

from __future__ import annotations

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Create,
    FadeIn,
    FadeOut,
    Indicate,
    LaggedStart,
    Line,
    RoundedRectangle,
    SurroundingRectangle,
    VGroup,
)

from gi_story.claims import AlternativeClaim, load_claims
from gi_story.theme import (
    CAUTION_OCHRE,
    CHANNEL_TEAL,
    FLOOD_RED,
    FLOOD_WASH,
    LINE_GRAY,
    SURVEY_PAPER,
    TEAL_WASH,
    WATERSHED_INK,
    BaseFilmScene,
    heading,
    mono,
)


def _rule_card(label: str, detail: str) -> VGroup:
    shell = RoundedRectangle(
        width=2.45,
        height=1.45,
        corner_radius=0.14,
        stroke_color=LINE_GRAY,
        stroke_width=3,
        fill_color=SURVEY_PAPER,
        fill_opacity=1,
    )
    label_copy = heading(label, size=23)
    detail_copy = mono(detail, size=18, color=WATERSHED_INK)
    copy = VGroup(label_copy, detail_copy).arrange(DOWN, buff=0.1)
    if copy.width > shell.width - 0.28:
        copy.scale_to_fit_width(shell.width - 0.28)
    copy.move_to(shell)
    return VGroup(shell, copy)


def _status_value(value: str, status: str, *, passed: bool) -> VGroup:
    color = CHANNEL_TEAL if passed else FLOOD_RED
    value_copy = mono(value, size=19, color=WATERSHED_INK)
    status_copy = heading(status, size=17, color=color)
    return VGroup(value_copy, status_copy).arrange(DOWN, buff=0.04)


def _candidate_band(alternative: AlternativeClaim) -> tuple[VGroup, VGroup]:
    """Return one complete, value-bearing candidate truth row."""

    is_alt207 = alternative.id == "ALT-207"
    worst_delta = max(run.delta for run in alternative.storm_runs)
    all_qa = sum(run.qa_status == "accepted" for run in alternative.storm_runs)
    volume_pass = alternative.reduction >= 6.0
    cost_pass = alternative.cost <= 200.0
    peak_pass = worst_delta <= 0.0
    qa_pass = all_qa == 4
    statuses = (volume_pass, cost_pass, peak_pass, qa_pass)

    shell = RoundedRectangle(
        width=12.65,
        height=1.12,
        corner_radius=0.12,
        stroke_color=CHANNEL_TEAL if all(statuses) else FLOOD_RED,
        stroke_width=3,
        fill_color=TEAL_WASH if all(statuses) else FLOOD_WASH,
        fill_opacity=0.72,
    )
    shell.move_to(DOWN * 0.95)
    separators = VGroup(
        *(
            Line(
                [x, shell.get_bottom()[1] + 0.08, 0],
                [x, shell.get_top()[1] - 0.08, 0],
                color=LINE_GRAY,
                stroke_width=2,
            )
            for x in (-4.65, -2.0, 0.65, 3.3)
        )
    )
    id_copy = VGroup(
        heading(
            alternative.id,
            size=24,
            color=CHANNEL_TEAL if all(statuses) else CAUTION_OCHRE,
        ),
        heading(
            "ALL 4 PASS" if is_alt207 else "STOP",
            size=17,
            color=CHANNEL_TEAL if is_alt207 else FLOOD_RED,
        ),
    ).arrange(DOWN, buff=0.05)
    id_copy.move_to([-5.48, -0.95, 0])

    peak_status = "PASS" if peak_pass else "FAIL · 2 RISE"
    values = VGroup(
        _status_value(
            f"{alternative.reduction:.2f}%", "PASS" if volume_pass else "FAIL", passed=volume_pass
        ),
        _status_value(
            f"${alternative.cost:.2f}k", "PASS" if cost_pass else "FAIL", passed=cost_pass
        ),
        _status_value(
            f"worst Δ {worst_delta:+.2f} cfs",
            peak_status,
            passed=peak_pass,
        ),
        _status_value(
            f"{all_qa} / 4 accepted",
            "PASS" if qa_pass else "FAIL",
            passed=qa_pass,
        ),
    )
    for value, x in zip(values, (-3.33, -0.68, 1.98, 4.63)):
        if value.width > 2.35:
            value.scale_to_fit_width(2.35)
        value.move_to([x, -0.95, 0])
    band = VGroup(shell, separators, id_copy, values)
    return band, values


class Scene04DecisionIntersection(BaseFilmScene):
    """Required rules combine with AND; they are not an ordered obstacle course."""

    scene_id = "04-decision-intersection"
    target_duration = 40.0

    def construct(self) -> None:
        claims = load_claims()
        alt017 = claims.teaching_by_id["ALT-017"]
        alt207 = claims.teaching_by_id["ALT-207"]
        volume_rule, cost_rule, peak_rule, _qa_rule = claims.decision_rules

        title = VGroup(
            mono("FOUR REQUIRED RULES", size=22, color=CHANNEL_TEAL),
            heading("Every required rule must be true", size=55),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.22)
        title.to_edge(UP, buff=0.56).to_edge(LEFT, buff=0.76)
        self.reveal_title(title, seconds=0.9)
        self.hold(1.8)

        rules = VGroup(
            _rule_card(
                "OUT1 VOLUME",
                f"aggregate reduction\n≥ {volume_rule.threshold:g}%",
            ),
            _rule_card(
                "SCREENING COST",
                f"30-year PV\n≤ ${cost_rule.threshold:g}k",
            ),
            _rule_card(
                "STORM PEAKS",
                f"every Δ\n≤ {peak_rule.threshold:g} cfs",
            ),
            _rule_card("DIRECT-RUN QA", "4 of 4\naccepted"),
        ).arrange(RIGHT, buff=0.2)
        rules.move_to(RIGHT * 0.65 + UP * 0.95)
        all_required = heading("ALL FOUR CONDITIONS", size=24, color=CHANNEL_TEAL)
        all_required.next_to(rules, UP, buff=0.12)
        self.play_for(
            LaggedStart(*(FadeIn(rule, shift=DOWN * 0.08) for rule in rules), lag_ratio=0.14),
            FadeIn(all_required),
            seconds=2.3,
        )
        self.hold(1.6)

        rail_y = 0.08
        rail = Line(
            [rules[0].get_center()[0], rail_y, 0],
            [rules[-1].get_center()[0], rail_y, 0],
            color=CAUTION_OCHRE,
            stroke_width=4,
        )
        stems = VGroup(
            *(
                Line(
                    rule.get_bottom(),
                    [rule.get_center()[0], rail_y, 0],
                    color=CAUTION_OCHRE,
                    stroke_width=3,
                )
                for rule in rules
            )
        )
        and_copy = mono("AND · no averaging", size=20, color=CAUTION_OCHRE)
        and_backing = SurroundingRectangle(
            and_copy,
            buff=0.08,
            color=SURVEY_PAPER,
            stroke_width=0,
            fill_color=SURVEY_PAPER,
            fill_opacity=1,
        )
        and_label = VGroup(and_backing, and_copy)
        and_label.move_to([0.65, rail_y, 0])
        self.play_for(Create(stems), Create(rail), FadeIn(and_label), seconds=0.9)
        self.hold(1.4)
        for rule in rules:
            self.play_for(Indicate(rule, color=CHANNEL_TEAL, scale_factor=1.02), seconds=0.65)
            self.hold(0.25)

        alt207_band, alt207_values = _candidate_band(alt207)
        self.play_for(
            FadeIn(alt207_band, shift=UP * 0.08),
            *(rule[0].animate.set_stroke(CHANNEL_TEAL) for rule in rules),
            seconds=1.0,
        )
        self.hold(1.2)
        for value in alt207_values:
            self.play_for(Indicate(value, color=CHANNEL_TEAL, scale_factor=1.03), seconds=0.65)
            self.hold(0.2)
        self.play_for(Indicate(alt207_band[2], color=CHANNEL_TEAL), seconds=0.8)
        self.hold(2.4)

        alt017_band, alt017_values = _candidate_band(alt017)
        self.play_for(FadeOut(alt207_band), seconds=0.5)
        self.play_for(
            FadeIn(alt017_band, shift=UP * 0.08),
            rules[0][0].animate.set_stroke(FLOOD_RED),
            rules[1][0].animate.set_stroke(CHANNEL_TEAL),
            rules[2][0].animate.set_stroke(FLOOD_RED),
            rules[3][0].animate.set_stroke(CHANNEL_TEAL),
            seconds=1.0,
        )
        self.hold(1.0)
        self.play_for(Indicate(alt017_values[0], color=FLOOD_RED), seconds=0.8)
        self.hold(0.8)
        self.play_for(Indicate(alt017_values[2], color=FLOOD_RED), seconds=0.8)
        self.hold(1.2)
        self.play_for(
            Indicate(alt017_values[1], color=CHANNEL_TEAL),
            Indicate(alt017_values[3], color=CHANNEL_TEAL),
            seconds=0.8,
        )
        self.hold(1.0)
        self.play_for(Indicate(alt017_band[2], color=FLOOD_RED), seconds=0.8)
        self.hold(2.4)

        rule_summary = VGroup(
            heading("A required failure cannot be averaged away.", size=33),
            heading("One FALSE makes the whole AND statement FALSE.", size=35, color=FLOOD_RED),
        ).arrange(DOWN, buff=0.08)
        if rule_summary.width > 12.2:
            rule_summary.scale_to_fit_width(12.2)
        rule_summary.move_to(DOWN * 2.66)
        self.play_for(FadeIn(rule_summary, shift=UP * 0.1), seconds=1.2)
        self.play_for(Indicate(and_label, color=CAUTION_OCHRE), seconds=0.8)
        self.hold(1.4)
        self.play_for(Indicate(rule_summary, color=FLOOD_RED, scale_factor=1.02), seconds=0.8)
        self.hold(1.5)
        self.place_footer("ALT-207 passes all four rules; ALT-017 fails volume and peak.")
        self.finish_timing()
