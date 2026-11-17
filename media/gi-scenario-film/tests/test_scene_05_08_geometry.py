"""Regression guards for the production-layout corrections in scenes 05–08."""

from __future__ import annotations

import inspect

import pytest
from gi_story.claims import load_claims
from gi_story.scenes.scene_05_qa_vs_fitness import _evidence_step
from gi_story.scenes.scene_06_storm_response import _aggregate_card, _storm_comparison
from gi_story.scenes.scene_07_bounded_search_space import (
    Scene07BoundedSearchSpace,
    _known_question_deck,
    _question_card,
    _storm_tokens,
)
from gi_story.scenes.scene_08_job_matrix import (
    RETAINED_ROW_LABELS,
    STORM_COLUMN_X,
    Scene08JobMatrix,
    _aligned_row,
    _dense_job_matrix,
    _retained_run_grid,
)


def _assert_inside(shell, copy, *, padding: float = 0.04) -> None:
    assert copy.get_left()[0] >= shell.get_left()[0] + padding
    assert copy.get_right()[0] <= shell.get_right()[0] - padding
    assert copy.get_bottom()[1] >= shell.get_bottom()[1] + padding
    assert copy.get_top()[1] <= shell.get_top()[1] - padding


@pytest.mark.parametrize(
    "label",
    (
        "Parser accepted",
        "Direct SWMM execution",
        "Continuity 0.079% ≤ 2%",
        "Artifacts + SHA-256",
    ),
)
def test_scene_05_evidence_copy_stays_inside_its_card(label: str) -> None:
    shell, _, copy = _evidence_step(label)

    _assert_inside(shell, copy, padding=0.16)


def test_scene_06_aggregate_values_stay_inside_the_summary_card() -> None:
    card, value, reduction = _aggregate_card(0.717, 4.14)
    shell = card[0]

    _assert_inside(shell, value, padding=0.1)
    _assert_inside(shell, reduction, padding=0.1)


def test_scene_06_storm_rows_contain_names_durations_metrics_and_states() -> None:
    claims = load_claims()
    candidate = claims.teaching_by_id["ALT-017"]
    storms = {storm.storm_id: storm for storm in claims.storms}

    for run in candidate.storm_runs:
        storm = storms[run.storm_id]
        row, _, _ = _storm_comparison(
            run,
            depth_inches=storm.depth_inches,
            duration_hours=storm.duration_hours,
        )
        shell = row[0]
        for copy in row[2:]:
            _assert_inside(shell, copy, padding=0.06)


def test_scene_07_known_question_deck_has_three_separate_reading_lanes() -> None:
    shell, title, deck, note = _known_question_deck(700)

    _assert_inside(shell, title, padding=0.08)
    _assert_inside(shell, deck, padding=0.08)
    _assert_inside(shell, note, padding=0.08)
    assert title.get_bottom()[1] - deck.get_top()[1] >= 0.1
    assert deck.get_bottom()[1] - note.get_top()[1] >= 0.1


def test_scene_07_selected_question_and_storm_tokens_are_self_explanatory() -> None:
    shell, copy = _question_card("[0, 3, 2, 6]")
    _assert_inside(shell, copy, padding=0.12)

    tokens = _storm_tokens()
    assert [token[1].text for token in tokens] == ["B", "F", "L", "S"]
    for token in tokens:
        _assert_inside(token[0], token[1], padding=0.03)

    source = inspect.getsource(Scene07BoundedSearchSpace.construct)
    assert "Every hydraulic answer begins face-down" in source
    assert "benchmark-gated" not in source
    assert "reserved exploration" not in source


def test_scene_08_dense_matrix_has_one_aligned_column_per_storm() -> None:
    dense = _dense_job_matrix()
    baseline = _aligned_row(0.88, size=0.52)

    assert len(dense) == len(STORM_COLUMN_X) == 4
    assert all(len(column) == 44 for column in dense)
    for expected_x, column, baseline_cell in zip(STORM_COLUMN_X, dense, baseline, strict=True):
        assert column.get_x() == pytest.approx(expected_x)
        assert baseline_cell.get_x() == pytest.approx(expected_x)
    assert dense.get_top()[1] < baseline.get_bottom()[1]


def test_scene_08_retained_files_are_five_named_rows_by_four_storms() -> None:
    grid, labels = _retained_run_grid()

    assert len(grid) == len(RETAINED_ROW_LABELS) == 5
    assert sum(len(row) for row in grid) == 20
    assert [label.text for label in labels] == list(RETAINED_ROW_LABELS)
    for row in grid:
        assert [cell.get_x() for cell in row] == pytest.approx(STORM_COLUMN_X)

    source = inspect.getsource(Scene08JobMatrix.construct)
    assert "PRIOR COMPUTED RESULTS" in source
    assert "MEASUREMENTS" not in source
