"""Reusable, accessible Manim components for the GI film."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    Circle,
    Dot,
    Line,
    RoundedRectangle,
    Square,
    Text,
    Triangle,
    VGroup,
)

from .theme import (
    CAUTION_OCHRE,
    CAUTION_WASH,
    CHANNEL_TEAL,
    FLOOD_RED,
    FLOOD_WASH,
    INK_SOFT,
    LINE_GRAY,
    RAIN_BLUE,
    SURVEY_PAPER,
    TEAL_WASH,
    WATERSHED_INK,
    body,
    heading,
    mono,
)

MIN_CARD_FONT_SIZE = 18.0
MIN_ESSENTIAL_FONT_SIZE = 20.0


class TextFitError(ValueError):
    """Raised instead of silently shrinking copy below a readable size."""


@dataclass(frozen=True, slots=True)
class _CopySpec:
    text: str
    role: str
    preferred_size: float
    minimum_size: float
    color: object


def _render_text(text: str, *, role: str, size: float, color) -> Text:
    factories = {"heading": heading, "body": body, "mono": mono}
    try:
        return factories[role](text, size=size, color=color)
    except KeyError as error:
        raise ValueError(f"Unknown text role {role!r}") from error


def _wrapped_at_size(
    text: str,
    *,
    role: str,
    size: float,
    color,
    max_width: float,
    line_buff: float,
) -> VGroup | None:
    """Greedily wrap using Pango's measured glyph widths at one exact size."""

    lines: list[Text] = []

    def split_token(token: str) -> list[str] | None:
        if _render_text(token, role=role, size=size, color=color).width <= max_width:
            return [token]
        chunks: list[str] = []
        current = ""
        for character in token:
            trial = f"{current}{character}"
            if _render_text(trial, role=role, size=size, color=color).width <= max_width:
                current = trial
                continue
            if not current:
                return None
            chunks.append(current)
            current = character
        if current:
            chunks.append(current)
        return chunks

    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            continue
        current = ""
        for word in words:
            chunks = split_token(word)
            if chunks is None:
                return None
            for index, chunk in enumerate(chunks):
                force_break = index < len(chunks) - 1
                trial_text = f"{current} {chunk}".strip()
                trial = _render_text(trial_text, role=role, size=size, color=color)
                if current and trial.width > max_width:
                    lines.append(_render_text(current, role=role, size=size, color=color))
                    current = chunk
                else:
                    current = trial_text
                if force_break:
                    lines.append(_render_text(current, role=role, size=size, color=color))
                    current = ""
        if current:
            lines.append(_render_text(current, role=role, size=size, color=color))
    if not lines:
        return VGroup()
    return VGroup(*lines).arrange(DOWN, aligned_edge=LEFT, buff=line_buff)


def wrapped_text(
    text: str,
    *,
    max_width: float,
    max_height: float | None = None,
    role: str = "body",
    size: float = 22,
    min_size: float = MIN_ESSENTIAL_FONT_SIZE,
    color=INK_SOFT,
    line_buff: float = 0.06,
) -> VGroup:
    """Return measured wrapped copy without ever scaling below ``min_size``.

    The returned object contains real ``Text`` lines created at their final
    font size.  If the wording cannot fit, authors must shorten it or enlarge
    its visual region; the renderer will not hide the problem with microcopy.
    """

    if max_width <= 0 or (max_height is not None and max_height <= 0):
        raise ValueError("Text bounds must be positive")
    if min_size <= 0 or size < min_size:
        raise ValueError("size must be greater than or equal to min_size")
    for candidate_size in _font_sizes(size, min_size):
        wrapped = _wrapped_at_size(
            text,
            role=role,
            size=candidate_size,
            color=color,
            max_width=max_width,
            line_buff=line_buff,
        )
        if wrapped is None:
            continue
        if max_height is None or wrapped.height <= max_height:
            return wrapped
    height_copy = "unbounded" if max_height is None else f"{max_height:.2f}"
    raise TextFitError(
        f"Copy cannot fit {max_width:.2f} × {height_copy} at the hard minimum "
        f"font size {min_size:g}: {text!r}"
    )


def _font_sizes(preferred: float, minimum: float) -> tuple[float, ...]:
    sizes = []
    candidate = preferred
    while candidate > minimum:
        sizes.append(candidate)
        candidate -= 1.0
    sizes.append(minimum)
    return tuple(sizes)


def _fit_copy_stack(
    specs: Sequence[_CopySpec],
    *,
    max_width: float,
    max_height: float,
    buff: float,
) -> VGroup:
    visible = [spec for spec in specs if spec.text.strip()]
    if not visible:
        return VGroup()
    maximum_reduction = max(spec.preferred_size - spec.minimum_size for spec in visible)
    reduction = 0.0
    while reduction <= maximum_reduction + 1e-9:
        rendered = []
        failed = False
        for spec in visible:
            candidate_size = max(spec.minimum_size, spec.preferred_size - reduction)
            candidate = _wrapped_at_size(
                spec.text,
                role=spec.role,
                size=candidate_size,
                color=spec.color,
                max_width=max_width,
                line_buff=0.045,
            )
            if candidate is None:
                failed = True
                break
            rendered.append(candidate)
        if not failed:
            stack = VGroup(*rendered).arrange(DOWN, aligned_edge=LEFT, buff=buff)
            if stack.width <= max_width and stack.height <= max_height:
                return stack
        reduction += 1.0
    summary = " / ".join(spec.text for spec in visible)
    minimum = min(spec.minimum_size for spec in visible)
    raise TextFitError(
        f"Card copy cannot fit {max_width:.2f} × {max_height:.2f} without going "
        f"below font size {minimum:g}: {summary!r}"
    )


def iter_text_mobjects(mobject) -> tuple[Text, ...]:
    """Return all authored Text roots below a component group."""

    found: list[Text] = []

    def visit(current) -> None:
        if isinstance(current, Text):
            found.append(current)
            return
        for child in current.submobjects:
            visit(child)

    visit(mobject)
    return tuple(found)


def assert_minimum_font_size(mobject, *, minimum: float = MIN_CARD_FONT_SIZE) -> None:
    """Guard component copy against unreadable authored font sizes."""

    undersized = [text for text in iter_text_mobjects(mobject) if text.font_size < minimum - 1e-6]
    if undersized:
        descriptions = ", ".join(f"{text.text!r} ({text.font_size:g})" for text in undersized)
        raise TextFitError(f"Text falls below font size {minimum:g}: {descriptions}")


def panel(
    width: float, height: float, *, fill=SURVEY_PAPER, stroke=LINE_GRAY, radius: float = 0.12
):
    return RoundedRectangle(
        width=width,
        height=height,
        corner_radius=radius,
        stroke_color=stroke,
        stroke_width=2,
        fill_color=fill,
        fill_opacity=1,
    )


def labeled_panel(
    title: str, detail: str, *, width: float = 3.2, height: float = 1.55, accent=CHANNEL_TEAL
):
    box = panel(width, height)
    bar = Line(
        box.get_corner(UP + LEFT) + RIGHT * 0.12,
        box.get_corner(DOWN + LEFT) + RIGHT * 0.12,
        color=accent,
        stroke_width=7,
    )
    copy = _fit_copy_stack(
        (
            _CopySpec(title, "heading", 32, 18, WATERSHED_INK),
            _CopySpec(detail, "body", 22, 18, INK_SOFT),
        ),
        max_width=box.width - 0.55,
        max_height=box.height - 0.14,
        buff=0.06,
    )
    copy.move_to(box).align_to(box, LEFT).shift(RIGHT * 0.28)
    return VGroup(box, bar, copy)


def status_card(label: str, value: str, *, status: str = "neutral", width: float = 2.7):
    roles = {
        "pass": (CHANNEL_TEAL, TEAL_WASH, "PASS"),
        "fail": (FLOOD_RED, FLOOD_WASH, "FAIL"),
        "caution": (CAUTION_OCHRE, CAUTION_WASH, "CHECK"),
        "neutral": (WATERSHED_INK, SURVEY_PAPER, "INFO"),
    }
    color, wash, marker = roles[status]
    box = panel(width, 1.3, fill=wash, stroke=color)
    copy = _fit_copy_stack(
        (
            _CopySpec(marker, "mono", 18, 18, color),
            _CopySpec(label, "body", 22, 18, INK_SOFT),
            _CopySpec(value, "mono", 28, 20, color),
        ),
        max_width=box.width - 0.40,
        max_height=box.height - 0.14,
        buff=0.055,
    )
    copy.move_to(box).align_to(box, LEFT).shift(RIGHT * 0.2)
    return VGroup(box, copy)


def rule_card(
    number: str, label: str, threshold: str, *, status: str = "neutral", width: float = 3.0
):
    card = status_card(label, threshold, status=status, width=width)
    number_mob = mono(number, size=18, color=INK_SOFT)
    number_mob.move_to(card[0].get_corner(UP + RIGHT)).shift(LEFT * 0.24 + DOWN * 0.18)
    card.add(number_mob)
    return card


def flow_node(label: str, *, kind: str = "circle", color=CHANNEL_TEAL):
    if kind == "square":
        shape = Square(side_length=0.72, color=color, stroke_width=3)
    elif kind == "triangle":
        shape = Triangle(color=color, stroke_width=3).scale(0.48)
    else:
        shape = Circle(radius=0.38, color=color, stroke_width=3)
    label_mob = mono(label, size=20, color=WATERSHED_INK)
    label_mob.next_to(shape, DOWN, buff=0.12)
    return VGroup(shape, label_mob)


def discrete_dots(count: int, *, active: int | None = None, color=RAIN_BLUE, spacing: float = 0.24):
    dots = VGroup()
    for index in range(count):
        fill = color if active is None or index <= active else SURVEY_PAPER
        dots.add(Dot(radius=0.065, color=color, fill_color=fill, fill_opacity=1))
    dots.arrange(RIGHT, buff=spacing)
    return dots


def simple_raindrop(color=RAIN_BLUE):
    circle = Circle(radius=0.18, color=color, fill_color=color, fill_opacity=1)
    tip = Triangle(color=color, fill_color=color, fill_opacity=1).scale(0.19).rotate(3.14159)
    tip.next_to(circle, UP, buff=-0.12)
    return VGroup(circle, tip).scale(0.8)


def matrix_cells(
    rows: int, cols: int, *, cell: float = 0.12, gap: float = 0.025, color=CHANNEL_TEAL
):
    grid = VGroup()
    for row in range(rows):
        for col in range(cols):
            square = Square(
                side_length=cell,
                stroke_color=color,
                stroke_width=0.8,
                fill_color=color,
                fill_opacity=0.18,
            )
            square.move_to([col * (cell + gap), -row * (cell + gap), 0])
            grid.add(square)
    grid.center()
    return grid


def evidence_chain(labels: Sequence[str], *, width: float = 11.0):
    nodes = VGroup(*(labeled_panel(label, "", width=1.4, height=0.78) for label in labels))
    nodes.arrange(RIGHT, buff=0.28)
    if nodes.width > width:
        nodes.scale(width / nodes.width)
    links = VGroup()
    for left_node, right_node in pairwise(nodes):
        links.add(
            Line(left_node.get_right(), right_node.get_left(), color=CHANNEL_TEAL, stroke_width=3)
        )
    return VGroup(links, nodes)
