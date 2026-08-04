"""Strict parser and source-derived identity for retained reference matrices.

This parser is intentionally separate from the reviewed Ridgeway direct-study
workflow.  Reference comparisons bind their own parser provenance without
changing the pinned public fixture workflow or its manifest.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import PreparedPair


# BEGIN REFERENCE_MATRIX_PARSER_SOURCE
def parse_reference_matrix_bytes(
    payload: bytes,
    pairs: Sequence[PreparedPair],
) -> dict[str, float]:
    """Parse one retained Circuitscape three-column resistance matrix."""

    import math

    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("Circuitscape matrix is not UTF-8 text") from exc
    requested: dict[tuple[int, int], PreparedPair] = {}
    pair_ids: set[str] = set()
    for pair in pairs:
        key = tuple(sorted((pair.from_focal_id, pair.to_focal_id)))
        if pair.from_focal_id == pair.to_focal_id:
            raise ValueError("requested focal pairs must use two distinct IDs")
        if pair.pair_id in pair_ids or key in requested:
            raise ValueError("requested focal-pair identities are ambiguous")
        pair_ids.add(pair.pair_id)
        requested[key] = pair
    values: dict[tuple[int, int], float] = {}
    observed_keys: set[tuple[int, int]] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        fields = line.strip().split()
        if len(fields) != 3:
            raise ValueError(f"Circuitscape matrix row {line_number} is not exactly three columns")
        try:
            left_value, right_value, resistance = map(float, fields)
        except ValueError as exc:
            raise ValueError(f"Circuitscape matrix row {line_number} is not numeric") from exc
        if not all(math.isfinite(value) for value in (left_value, right_value, resistance)):
            raise ValueError(f"Circuitscape matrix row {line_number} contains a nonfinite value")
        if (
            not left_value.is_integer()
            or not right_value.is_integer()
            or left_value <= 0
            or right_value <= 0
            or left_value == right_value
        ):
            raise ValueError(f"Circuitscape matrix row {line_number} has invalid focal IDs")
        key = tuple(sorted((int(left_value), int(right_value))))
        if key in observed_keys:
            raise ValueError("Circuitscape matrix contains a duplicate focal pair")
        observed_keys.add(key)
        if key in requested:
            if resistance <= 0:
                raise ValueError(
                    "Circuitscape matrix has no positive result for requested pair "
                    f"{requested[key].pair_id}"
                )
            values[key] = resistance
        elif resistance != -1.0:
            raise ValueError(
                "Circuitscape matrix contains an unexpected positive or undocumented result"
            )
    output: dict[str, float] = {}
    for pair in pairs:
        key = tuple(sorted((pair.from_focal_id, pair.to_focal_id)))
        if key not in values:
            raise ValueError(f"Circuitscape matrix omits requested pair {pair.pair_id}")
        output[pair.pair_id] = values[key]
    return output


# END REFERENCE_MATRIX_PARSER_SOURCE


# SHA-256 of ``b"ecocorridor/reference-matrix-parser-source/v1\0"`` plus the
# UTF-8, LF-normalized source slice between the markers above, stripped and
# terminated by one LF.  This binds only the parser function and is stable
# across Python grammar/AST implementation versions.  The focused source-drift
# test independently recomputes it from the checked-in module.
REFERENCE_MATRIX_PARSER_SHA256 = "474ce17140268b6edb3df5ecccd69c11ee9b84cbc01fff870cda406ca7426405"
