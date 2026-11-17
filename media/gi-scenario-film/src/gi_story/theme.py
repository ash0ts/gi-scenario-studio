"""Shared visual language and pacing for every film insert."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from manim import (
    DOWN,
    LEFT,
    RIGHT,
    FadeIn,
    FullScreenRectangle,
    Line,
    ManimColor,
    Mobject,
    Scene,
    Text,
    VGroup,
    register_font,
)

SURVEY_PAPER = ManimColor("#F5F7F6")
WATERSHED_INK = ManimColor("#17313A")
INK_SOFT = ManimColor("#425A62")
CHANNEL_TEAL = ManimColor("#08766F")
TEAL_WASH = ManimColor("#DCEDEA")
RAIN_BLUE = ManimColor("#2F6F9F")
RAIN_WASH = ManimColor("#E2ECF3")
CAUTION_OCHRE = ManimColor("#A96315")
CAUTION_WASH = ManimColor("#F4E8D9")
FLOOD_RED = ManimColor("#B43D47")
FLOOD_WASH = ManimColor("#F4E0E2")
LINE_GRAY = ManimColor("#C8D2D0")
MUTED = ManimColor("#667B81")
WHITE = ManimColor("#FFFFFF")

FILM_ROOT = Path(__file__).resolve().parents[2]
FONT_ROOT = FILM_ROOT / "assets" / "fonts"
FONT_FILES = (
    FONT_ROOT / "IBMPlexSansCondensed-SemiBold.ttf",
    FONT_ROOT / "AtkinsonHyperlegible-Regular.ttf",
    FONT_ROOT / "AtkinsonHyperlegible-Bold.ttf",
    FONT_ROOT / "IBMPlexMono-Regular.ttf",
    FONT_ROOT / "IBMPlexMono-Medium.ttf",
)
STORY_MANIFEST = FILM_ROOT / "story" / "story-manifest.json"


def _load_edit_handles() -> tuple[float, float]:
    story = json.loads(STORY_MANIFEST.read_text(encoding="utf-8"))
    handles = story.get("edit_handles")
    if not isinstance(handles, dict):
        raise TypeError("Story manifest must define edit_handles")
    head = float(handles.get("head_seconds", 0.0))
    tail = float(handles.get("tail_seconds", 0.0))
    if head <= 0 or tail <= 0:
        raise ValueError("Story manifest edit handles must be positive")
    return head, tail


HEAD_HANDLE_SECONDS, TAIL_HANDLE_SECONDS = _load_edit_handles()

HEADING_FONT = "IBM Plex Sans Condensed"
BODY_FONT = "Atkinson Hyperlegible"
MONO_FONT = "IBM Plex Mono"


@dataclass(frozen=True, slots=True)
class LayoutZone:
    """A named axis-aligned safe area in Manim scene coordinates."""

    name: str
    left: float
    right: float
    bottom: float
    top: float

    def __post_init__(self) -> None:
        if self.left >= self.right or self.bottom >= self.top:
            raise ValueError(f"Invalid layout zone {self.name!r}")

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def center(self) -> tuple[float, float, float]:
        return ((self.left + self.right) / 2, (self.bottom + self.top) / 2, 0.0)


@dataclass(frozen=True, slots=True)
class MobjectBounds:
    """Serializable bounds used by layout assertions and test diagnostics."""

    left: float
    right: float
    bottom: float
    top: float


class LayoutViolation(ValueError):
    """Raised when authored geometry crosses a declared visual boundary."""


# The frame is 14.22 x 8 Manim units.  These zones preserve at least a
# 48-pixel outer gutter at 1080p and stop title, body, conclusion, and footer
# copy from silently competing for the same pixels.
LAYOUT_ZONES = MappingProxyType(
    {
        "frame": LayoutZone("frame", -6.75, 6.75, -3.64, 3.56),
        "eyebrow": LayoutZone("eyebrow", -6.55, 0.85, 3.00, 3.50),
        "badge": LayoutZone("badge", 1.15, 6.55, 3.00, 3.50),
        "headline": LayoutZone("headline", -6.55, 6.55, 2.10, 2.94),
        "title": LayoutZone("title", -6.55, 6.55, 2.10, 3.50),
        "body": LayoutZone("body", -6.55, 6.55, -2.40, 2.00),
        "conclusion": LayoutZone("conclusion", -6.45, 6.45, -3.16, -2.52),
        "footer": LayoutZone("footer", -6.55, 6.55, -3.60, -3.24),
    }
)


def layout_zone(zone: str | LayoutZone) -> LayoutZone:
    """Resolve a public zone name while preserving custom-zone support."""

    if isinstance(zone, LayoutZone):
        return zone
    try:
        return LAYOUT_ZONES[zone]
    except KeyError as error:
        choices = ", ".join(sorted(LAYOUT_ZONES))
        raise KeyError(f"Unknown layout zone {zone!r}; expected one of: {choices}") from error


def mobject_bounds(mobject: Mobject) -> MobjectBounds:
    """Return an object's rendered axis-aligned bounds."""

    return MobjectBounds(
        left=float(mobject.get_left()[0]),
        right=float(mobject.get_right()[0]),
        bottom=float(mobject.get_bottom()[1]),
        top=float(mobject.get_top()[1]),
    )


def assert_mobject_in_zone(
    mobject: Mobject,
    zone: str | LayoutZone = "frame",
    *,
    padding: float = 0.0,
    label: str | None = None,
) -> Mobject:
    """Fail fast when a rendered object crosses a named safe zone."""

    resolved = layout_zone(zone)
    bounds = mobject_bounds(mobject)
    failures = []
    epsilon = 1e-6
    if bounds.left < resolved.left + padding - epsilon:
        failures.append(f"left {bounds.left:.3f} < {resolved.left + padding:.3f}")
    if bounds.right > resolved.right - padding + epsilon:
        failures.append(f"right {bounds.right:.3f} > {resolved.right - padding:.3f}")
    if bounds.bottom < resolved.bottom + padding - epsilon:
        failures.append(f"bottom {bounds.bottom:.3f} < {resolved.bottom + padding:.3f}")
    if bounds.top > resolved.top - padding + epsilon:
        failures.append(f"top {bounds.top:.3f} > {resolved.top - padding:.3f}")
    if failures:
        object_label = label or type(mobject).__name__
        raise LayoutViolation(
            f"{object_label} leaves layout zone {resolved.name!r}: " + "; ".join(failures)
        )
    return mobject


def assert_mobjects_separated(
    first: Mobject,
    second: Mobject,
    *,
    gap: float = 0.0,
    labels: tuple[str, str] = ("first", "second"),
) -> None:
    """Reject axis-aligned overlap between peer visual regions."""

    first_bounds = mobject_bounds(first)
    second_bounds = mobject_bounds(second)
    separated = (
        first_bounds.right + gap <= second_bounds.left
        or second_bounds.right + gap <= first_bounds.left
        or first_bounds.top + gap <= second_bounds.bottom
        or second_bounds.top + gap <= first_bounds.bottom
    )
    if not separated:
        raise LayoutViolation(
            f"{labels[0]} overlaps {labels[1]} with required gap {gap:.3f}; "
            f"bounds={first_bounds!r}, {second_bounds!r}"
        )


def place_in_zone(
    mobject: Mobject,
    zone: str | LayoutZone,
    *,
    anchor: Literal["center", "top_left", "top_right", "bottom_left", "bottom_right"] = "center",
    padding: float = 0.0,
) -> Mobject:
    """Place an object against a zone anchor, then validate its final bounds."""

    resolved = layout_zone(zone)
    half_width = mobject.width / 2
    half_height = mobject.height / 2
    x_positions = {
        "center": (resolved.left + resolved.right) / 2,
        "top_left": resolved.left + padding + half_width,
        "bottom_left": resolved.left + padding + half_width,
        "top_right": resolved.right - padding - half_width,
        "bottom_right": resolved.right - padding - half_width,
    }
    y_positions = {
        "center": (resolved.bottom + resolved.top) / 2,
        "top_left": resolved.top - padding - half_height,
        "top_right": resolved.top - padding - half_height,
        "bottom_left": resolved.bottom + padding + half_height,
        "bottom_right": resolved.bottom + padding + half_height,
    }
    if anchor not in x_positions:
        raise ValueError(f"Unknown zone anchor {anchor!r}")
    mobject.move_to((x_positions[anchor], y_positions[anchor], 0.0))
    return assert_mobject_in_zone(mobject, resolved, padding=padding)


def _text(
    text: str, *, role: str = "body", size: float = 34, color=WATERSHED_INK, **kwargs
) -> Text:
    font = {"heading": HEADING_FONT, "body": BODY_FONT, "mono": MONO_FONT}[role]
    missing = [path for path in FONT_FILES if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Film fonts are missing. Run scripts/fetch_fonts.py before rendering: "
            + ", ".join(path.name for path in missing)
        )
    with ExitStack() as stack:
        for path in FONT_FILES:
            stack.enter_context(register_font(path))
        return Text(text, font=font, font_size=size, color=color, **kwargs)


def heading(text: str, size: float = 62, color=WATERSHED_INK, **kwargs) -> Text:
    return _text(text, role="heading", size=size, color=color, weight="SEMIBOLD", **kwargs)


def body(text: str, size: float = 32, color=INK_SOFT, **kwargs) -> Text:
    return _text(text, role="body", size=size, color=color, **kwargs)


def mono(text: str, size: float = 30, color=WATERSHED_INK, **kwargs) -> Text:
    return _text(text, role="mono", size=size, color=color, **kwargs)


def evidence_thread(start, end, *, color=CHANNEL_TEAL, width: float = 5) -> Line:
    return Line(start, end, color=color, stroke_width=width)


def stack_text(lines: Iterable[str], *, size: float = 30, buff: float = 0.18) -> VGroup:
    return VGroup(*(body(line, size=size) for line in lines)).arrange(
        DOWN, aligned_edge=LEFT, buff=buff
    )


class BaseFilmScene(Scene):
    """Base class with deterministic styling and explicit scene-duration accounting."""

    target_duration: float = 10.0
    scene_id: str = "scene"
    head_handle_seconds: float = HEAD_HANDLE_SECONDS
    tail_handle_seconds: float = TAIL_HANDLE_SECONDS

    def setup(self) -> None:
        self.camera.background_color = SURVEY_PAPER
        self._background_plate = FullScreenRectangle(
            stroke_width=0,
            fill_color=SURVEY_PAPER,
            fill_opacity=1,
        )
        self.add(self._background_plate)
        self._film_elapsed = 0.0
        self._timing_scale = 0.04 if os.environ.get("GI_FILM_SMOKE") == "1" else 1.0
        self._head_handle_applied = False

    def play_for(self, *animations, seconds: float = 1.0, **kwargs) -> None:
        minimum = 0.125 if self._timing_scale < 1.0 else 0.04
        self.play(*animations, run_time=max(minimum, seconds * self._timing_scale), **kwargs)
        self._film_elapsed += seconds

    def hold(self, seconds: float = 0.6) -> None:
        minimum = 0.125 if self._timing_scale < 1.0 else 0.04
        self.wait(max(minimum, seconds * self._timing_scale))
        self._film_elapsed += seconds

    def finish_timing(self, *, minimum_tail: float | None = None) -> None:
        minimum_tail = self.tail_handle_seconds if minimum_tail is None else minimum_tail
        remaining = self.target_duration - self._film_elapsed
        if os.environ.get("GI_FILM_REPORT_TIMING") == "1":
            print(
                f"GI_FILM_TIMING {self.scene_id} elapsed={self._film_elapsed:.3f} "
                f"final_hold={remaining:.3f} target={self.target_duration:.3f}"
            )
        if remaining < minimum_tail - 1e-6:
            raise ValueError(
                f"{self.scene_id} exceeds target duration before its tail handle: "
                f"elapsed={self._film_elapsed:.3f}, target={self.target_duration:.3f}"
            )
        if os.environ.get("GI_FILM_STRICT_TIMING") == "1" and remaining > 4.0:
            raise ValueError(
                f"{self.scene_id} would end on a {remaining:.3f}s static slide; "
                "add explanatory motion or paced reveals"
            )
        self.hold(max(minimum_tail, remaining))

    @staticmethod
    def zone(name: str) -> LayoutZone:
        """Return a named authoring zone for scene-local geometry."""

        return layout_zone(name)

    def assert_in_zone(
        self,
        mobject: Mobject,
        zone: str | LayoutZone = "frame",
        *,
        padding: float = 0.0,
        label: str | None = None,
    ) -> Mobject:
        """Scene-friendly wrapper around :func:`assert_mobject_in_zone`."""

        return assert_mobject_in_zone(mobject, zone, padding=padding, label=label)

    def assert_live_layout(
        self,
        zone: str | LayoutZone = "frame",
        *,
        padding: float = 0.0,
    ) -> None:
        """Validate every live top-level object except the background plate."""

        for index, mobject in enumerate(self.mobjects):
            if mobject is self._background_plate:
                continue
            assert_mobject_in_zone(
                mobject,
                zone,
                padding=padding,
                label=f"live mobject {index} ({type(mobject).__name__})",
            )

    def title_block(self, eyebrow: str, title: str, *, title_size: float = 62) -> VGroup:
        eyebrow_mob = mono(eyebrow.upper(), size=22, color=CHANNEL_TEAL)
        title_mob = heading(title, size=title_size)
        # The upper-right limitation badge owns the other half of the eyebrow
        # row.  Long scene labels and titles shrink deterministically instead
        # of clipping or colliding with that badge.
        if eyebrow_mob.width > 5.0:
            eyebrow_mob.scale_to_fit_width(5.0)
        if title_mob.width > 12.55:
            title_mob.scale_to_fit_width(12.55)
        group = VGroup(eyebrow_mob, title_mob).arrange(DOWN, aligned_edge=LEFT, buff=0.32)
        place_in_zone(group, "title", anchor="top_left", padding=0.0)
        return group

    def limitation_badge(self, text: str = "SYNTHETIC TEACHING EVIDENCE") -> VGroup:
        from manim import RoundedRectangle

        label = mono(text, size=18, color=CAUTION_OCHRE)
        box = RoundedRectangle(
            width=label.width + 0.36,
            height=label.height + 0.2,
            corner_radius=0.08,
            stroke_color=CAUTION_OCHRE,
            stroke_width=2,
            fill_color=CAUTION_WASH,
            fill_opacity=1,
        )
        return VGroup(box, label)

    def reveal_title(self, block: VGroup, *, seconds: float = 0.8) -> None:
        if not self._head_handle_applied:
            self.hold(self.head_handle_seconds)
            self._head_handle_applied = True
        self.play_for(FadeIn(block, shift=RIGHT * 0.12), seconds=seconds)

    def place_footer(self, text: str) -> Text:
        footer = body(text, size=21, color=MUTED)
        place_in_zone(footer, "footer", anchor="bottom_left", padding=0.0)
        self.add(footer)
        return footer
