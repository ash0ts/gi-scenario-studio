"""Strict ecological-claim validation for user-visible and AI-authored copy.

This module deliberately lives outside the pinned scientific calculation path.
It extends the established claim checks at presentation, assistant, validation,
and export boundaries without changing any direct Circuitscape value or study
identity.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from .contracts import ConnectivityProjectBundle
from .metrics import assert_safe_ecological_claim as _assert_legacy_safe_ecological_claim
from .metrics import iter_bundle_ecological_claim_texts

_STRICT_PROHIBITED_CLAIM_PATTERNS = tuple(
    (code, re.compile(pattern, re.IGNORECASE))
    for code, pattern in (
        (
            "WILDLIFE_MOVEMENT_ENABLEMENT",
            (
                r"\b(?:help|allow|enable|guide|let)(?:s|ed|ing)?\s+"
                r"(?:wildlife|animals?)\s+(?:to\s+)?"
                r"(?:move|travel|cross|disperse|migrate|roam)\b"
            ),
        ),
        (
            "WILDLIFE_MOVEMENT_ENABLEMENT",
            (
                r"\b(?:help|allow|enable|support)(?:s|ed|ing)?\s+"
                r"(?:the\s+)?(?:movement|travel|dispersal|migration)\s+of\s+"
                r"(?:wildlife|animals?)\b"
            ),
        ),
        (
            "HABITAT_RECONNECTION",
            (
                r"\b(?:reconnect|connect|link|reunite|restore)(?:s|ed|ing)?\s+"
                r"(?:fragmented\s+)?(?:habitat|habitat\s+cores?|wildlife\s+corridors?)\b"
            ),
        ),
        (
            "HABITAT_RECONNECTION",
            r"\b(?:restore|improve|increase)(?:s|d|ing)?\s+habitat\s+connectivity\b",
        ),
        (
            "UNSUPPORTED_WILDLIFE_SAFETY",
            r"\bwildlife[- ]friendly\b",
        ),
        (
            "UNSUPPORTED_WILDLIFE_SAFETY",
            (
                r"\b(?:safe|safer)\s+(?:wildlife\s+)?"
                r"(?:crossing|corridor|route|portfolio|option|choice)\b"
            ),
        ),
        (
            "UNAUTHORIZED_PREFERENCE",
            (
                r"\b(?:preferred|recommended|superior|ideal|winning)\s+"
                r"(?:crossing|corridor|portfolio|option|project|route|design|choice)\b"
            ),
        ),
        (
            "UNAUTHORIZED_PREFERENCE",
            (
                r"\b(?:crossing|corridor|portfolio|option|project|route|design|choice)\s+"
                r"(?:is|was|would\s+be|should\s+be)\s+(?:the\s+)?"
                r"(?:preferred|recommended|superior|ideal|winning)\b"
            ),
        ),
        (
            "UNBOUNDED_ECOLOGICAL_ROBUSTNESS",
            r"\b(?:ecologically\s+robust|robust\s+ecological)\b",
        ),
        (
            "UNBOUNDED_ECOLOGICAL_ROBUSTNESS",
            r"\brobust\s+(?:candidate|portfolio|option|choice)\b|\brobust\s+but\b",
        ),
        (
            "UNSUPPORTED_ECOLOGICAL_BENEFIT",
            r"\becological\s+(?:benefit|gain|improvement|success)\b",
        ),
    )
)
_STRUCTURAL_LIMITATION_PATH = re.compile(r"(?:^|\.)(?:nonGoals|non_goals)\[\d+\]\Z")
_IMMEDIATE_NEGATION = re.compile(
    r"(?:\b(?:does|do|did|is|are|was|were|will|would|can|could)\s+not\s+|"
    r"\b(?:doesn't|don't|didn't|isn't|aren't|wasn't|weren't|won't|wouldn't|"
    r"can't|couldn't|cannot|never|without|not)\s+)"
    r"(?:(?:an?|the)\s+)?\Z",
    re.IGNORECASE,
)
_NEGATED_REPORTING_SCOPE = re.compile(
    r"\b(?:(?:does|do|did|will|would|can|could)\s+not|doesn't|don't|didn't|"
    r"won't|wouldn't|couldn't|cannot|can't|never)\s+"
    r"(?:show|demonstrate|establish|mean|indicate|prove|predict|support|confirm|"
    r"imply|conclude|assert)\s+(?:that\s+)?(?:(?:this|the|a|an)\s+)?"
    r"(?:[A-Za-z][A-Za-z'-]*\s+){0,5}\Z",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(r"[.!?;\n]|\b(?:but|however|although)\b", re.IGNORECASE)


def _strictly_negated(text: str, match_start: int) -> bool:
    preceding = text[max(0, match_start - 192) : match_start]
    clause = _CLAUSE_BOUNDARY.split(preceding)[-1]
    if re.search(r"\bnot\s+only\b", clause, re.IGNORECASE):
        return False
    return bool(_IMMEDIATE_NEGATION.search(clause) or _NEGATED_REPORTING_SCOPE.search(clause))


def assert_strict_ecological_claim(text: str) -> None:
    """Reject prohibited ecological meaning, including common safe-sounding paraphrases."""

    _assert_legacy_safe_ecological_claim(text)
    for code, pattern in _STRICT_PROHIBITED_CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            if not _strictly_negated(text, match.start()):
                raise ValueError(f"prohibited ecological claim: {code}")


def assert_strict_bundle_ecological_claims(bundle: ConnectivityProjectBundle) -> None:
    """Validate every project-authored narrative that can reach UI or exports."""

    for reference, text in iter_bundle_ecological_claim_texts(bundle):
        try:
            assert_strict_ecological_claim(text)
        except ValueError as exc:
            raise ValueError(f"{reference}: {exc}") from exc


def _rendered_strings(value: Any, *, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _rendered_strings(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            yield from _rendered_strings(item, path=f"{path}[{index}]")


def assert_safe_rendered_ecological_copy(value: Any) -> None:
    """Recursively validate strings in a renderer-facing JSON-like value."""

    for path, text in _rendered_strings(value):
        if _STRUCTURAL_LIMITATION_PATH.search(path):
            continue
        try:
            assert_strict_ecological_claim(text)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
