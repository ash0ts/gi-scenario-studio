"""Scene 11: turn a passing screen into three larger field questions."""

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
    LaggedStart,
    Line,
    VGroup,
)

from gi_story.claims import load_claims
from gi_story.components import labeled_panel, panel
from gi_story.theme import (
    CAUTION_OCHRE,
    CHANNEL_TEAL,
    INK_SOFT,
    LINE_GRAY,
    RAIN_BLUE,
    SURVEY_PAPER,
    BaseFilmScene,
    body,
    heading,
    mono,
)


class Scene11EngineerDecides(BaseFilmScene):
    """A passing screen creates a better field question, not a recommendation."""

    scene_id = "11-engineer-decides"
    target_duration = 40.0

    def construct(self) -> None:
        claims = load_claims()
        candidate = claims.teaching_by_id["ALT-207"]

        title = self.title_block(
            "After screening", "A passing screen creates better field questions", title_size=54
        )
        badge = self.limitation_badge("NOT A DESIGN RECOMMENDATION")
        badge.to_edge(UP, buff=0.58).to_edge(RIGHT, buff=0.72)
        self.reveal_title(title, seconds=1.0)
        self.play_for(FadeIn(badge), seconds=0.6)
        self.hold(1.8)

        candidate_shell = panel(8.7, 1.65, fill=SURVEY_PAPER, stroke=CHANNEL_TEAL)
        candidate_shell.move_to(UP * 0.55)
        candidate_id = mono(
            f"{candidate.id}  [{', '.join(str(value) for value in candidate.vector)}]",
            size=29,
            color=CHANNEL_TEAL,
        )
        candidate_metrics = body(
            f"{candidate.reduction:.2f}% volume reduction  ·  ${candidate.cost:.2f}k  ·  4/4 rules",
            size=27,
            color=INK_SOFT,
        )
        candidate_copy = VGroup(candidate_id, candidate_metrics).arrange(DOWN, buff=0.12)
        candidate_copy.move_to(candidate_shell)
        disposition = VGroup(
            mono("SCREENING DISPOSITION", size=22, color=CHANNEL_TEAL),
            heading("Candidate for site and feasibility review", size=38),
        ).arrange(DOWN, buff=0.08)
        disposition.next_to(candidate_shell, DOWN, buff=0.3)
        self.play_for(
            FadeIn(candidate_shell, shift=UP * 0.1),
            FadeIn(candidate_copy),
            seconds=1.8,
        )
        self.play_for(FadeIn(disposition, shift=UP * 0.08), seconds=1.6)
        self.hold(2.8)

        approval = VGroup(
            body("DESIGN APPROVAL", size=27, color=CAUTION_OCHRE),
            Line(LEFT * 1.6, RIGHT * 1.6, color=CAUTION_OCHRE, stroke_width=5),
        )
        approval[1].move_to(approval[0])
        approval.next_to(disposition, DOWN, buff=0.3)
        self.play_for(FadeIn(approval), seconds=1.3)
        self.hold(1.8)

        self.play_for(
            FadeOut(disposition, approval),
            candidate_shell.animate.shift(UP * 0.58),
            candidate_copy.animate.shift(UP * 0.58),
            seconds=1.8,
        )
        branches = VGroup(
            labeled_panel(
                "PHYSICAL FEASIBILITY",
                "topology · available area · soils · utilities",
                width=3.75,
                height=1.75,
                accent=CAUTION_OCHRE,
            ),
            labeled_panel(
                "DELIVERY + CARE",
                "constructability · ownership · maintenance",
                width=3.75,
                height=1.75,
                accent=CAUTION_OCHRE,
            ),
            labeled_panel(
                "EVIDENCE FITNESS",
                "authoritative storms · costs · model review",
                width=3.75,
                height=1.75,
                accent=CHANNEL_TEAL,
            ),
        ).arrange(RIGHT, buff=0.32)
        branches.move_to(DOWN * 1.05)
        arrows = VGroup(
            *(
                Arrow(
                    candidate_shell.get_bottom(),
                    branch.get_top(),
                    buff=0.1,
                    color=CHANNEL_TEAL,
                    stroke_width=4,
                    max_tip_length_to_length_ratio=0.14,
                )
                for branch in branches
            )
        )
        self.play_for(
            LaggedStart(*(Create(arrow) for arrow in arrows), lag_ratio=0.2),
            LaggedStart(*(FadeIn(branch, shift=UP * 0.08) for branch in branches), lag_ratio=0.2),
            seconds=3.6,
        )
        self.hold(2.8)

        evidence_line = Line(
            branches.get_left(), branches.get_right(), color=CHANNEL_TEAL, stroke_width=5
        )
        evidence_line.next_to(branches, DOWN, buff=0.28)
        evidence_note = body(
            "The shortlist focuses field effort; it does not replace it.",
            size=29,
            color=INK_SOFT,
        )
        evidence_note.next_to(evidence_line, DOWN, buff=0.18)
        self.play_for(Create(evidence_line), seconds=1.4)
        self.play_for(FadeIn(evidence_note), seconds=1.3)
        self.hold(2.8)

        self.play_for(
            FadeOut(
                candidate_shell, candidate_copy, branches, arrows, evidence_line, evidence_note
            ),
            seconds=1.5,
        )
        statements = VGroup(
            VGroup(mono("SWMM", size=29, color=RAIN_BLUE), heading("computes.", size=45)).arrange(
                RIGHT, buff=0.24
            ),
            VGroup(
                mono("SEARCH", size=29, color=CAUTION_OCHRE),
                heading("allocates attention.", size=45),
            ).arrange(RIGHT, buff=0.24),
            VGroup(
                mono("EVIDENCE", size=29, color=CHANNEL_TEAL),
                heading("preserves the path.", size=45),
            ).arrange(RIGHT, buff=0.24),
            VGroup(mono("THE ENGINEER", size=29), heading("decides.", size=45)).arrange(
                RIGHT, buff=0.24
            ),
        ).arrange(DOWN, aligned_edge=LEFT, buff=0.25)
        statements.move_to(DOWN * 0.12)
        self.play_for(FadeIn(statements[0], shift=RIGHT * 0.12), seconds=1.1)
        self.play_for(FadeIn(statements[1], shift=RIGHT * 0.12), seconds=1.1)
        self.play_for(FadeIn(statements[2], shift=RIGHT * 0.12), seconds=1.1)
        self.play_for(FadeIn(statements[3], shift=RIGHT * 0.12), seconds=1.1)
        self.hold(2.8)

        close_line = Line(
            statements.get_left(), statements.get_right(), color=LINE_GRAY, stroke_width=2
        )
        close_line.next_to(statements, DOWN, buff=0.28)
        close = body(
            "Passing creates a better question—not a finished design.",
            size=29,
            color=INK_SOFT,
        )
        close.next_to(close_line, DOWN, buff=0.18)
        self.play_for(Create(close_line), FadeIn(close), seconds=1.4)
        self.hold(2.4)
        self.finish_timing()
