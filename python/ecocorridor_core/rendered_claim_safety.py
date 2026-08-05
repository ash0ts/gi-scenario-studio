"""Presentation-only normalization around the immutable ecological claim policy."""

from __future__ import annotations

import unicodedata
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

from .claim_safety import assert_safe_rendered_ecological_copy

# Unicode DerivedCoreProperties.txt defines Default_Ignorable_Code_Point as
# these ranges. Several members are Mn, Lo, or reserved Cn code points rather
# than Cf format controls, so category-based filtering alone is insufficient.
# Keep this presentation-only list explicit: ordinary semantic combining marks
# must survive normalization.
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_DEFAULT_IGNORABLE_STARTS = tuple(start for start, _ in _DEFAULT_IGNORABLE_RANGES)


def _is_default_ignorable(character: str) -> bool:
    codepoint = ord(character)
    index = bisect_right(_DEFAULT_IGNORABLE_STARTS, codepoint) - 1
    return index >= 0 and codepoint <= _DEFAULT_IGNORABLE_RANGES[index][1]


def normalize_rendered_ecological_claim_text(text: str) -> str:
    """Return the browser-equivalent spelling used by presentation claim gates.

    Compatibility characters can make a prohibited claim look ordinary while
    evading an ASCII-oriented policy expression. Unicode default-ignorable
    code points, including COMBINING GRAPHEME JOINER, Hangul fillers, variation
    selectors, and bidi/zero-width controls, are likewise invisible in ordinary
    rendering and must not split a protected phrase. This wrapper stays outside
    the immutable direct-evidence calculation and report snapshot while giving
    source, artifact, and live-DOM gates one view.
    """

    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf" and not _is_default_ignorable(character)
    )
    return unicodedata.normalize("NFKC", normalized)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_rendered_ecological_claim_text(value)
    if isinstance(value, Mapping):
        return {key: _normalize_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_normalize_value(item) for item in value]
    return value


def assert_safe_normalized_rendered_ecological_copy(value: Any) -> None:
    """Apply the immutable claim policy after browser-equivalent normalization."""

    assert_safe_rendered_ecological_copy(_normalize_value(value))
