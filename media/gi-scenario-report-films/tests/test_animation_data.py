from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parents[1]
BUILDER = PACKAGE / "scripts/build_animation_data.py"
DATA = PACKAGE / "data/animation-data.json"


def _module():
    spec = importlib.util.spec_from_file_location("gi_report_animation_data", BUILDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_animation_projection_matches_authoritative_sources() -> None:
    expected = _module().build_payload()
    observed = json.loads(DATA.read_text(encoding="utf-8"))

    assert observed == expected


def test_all_five_films_are_large_silent_first_contracts() -> None:
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    films = payload["films"]

    assert payload["width"] == 1920
    assert payload["height"] == 1080
    assert payload["fps"] == 30
    assert len(films) == 5
    assert sum(item["duration"] for item in films) == 204
    assert all(item["duration"] * payload["fps"] == int(item["duration"] * 30) for item in films)
    assert all(item["checkpoints"] for item in films)
    assert len({item["slug"] for item in films}) == 5
    assert len({item["key"] for item in films}) == 5


def test_ecological_and_compute_boundaries_remain_explicit() -> None:
    payload = json.loads(DATA.read_text(encoding="utf-8"))

    assert payload["water_ledger"]["modeled_gallon_difference"] == 48_000
    assert "not measured groundwater recharge" in payload["water_ledger"][
        "infiltration_language"
    ]
    assert payload["search"]["energy_kwh"] is None
    assert payload["search"]["carbon_co2e"] is None
    assert "energy or carbon savings" in payload["forbidden_claims"]


def test_exact_story_numbers_are_bound_to_source_evidence() -> None:
    payload = json.loads(DATA.read_text(encoding="utf-8"))

    assert payload["guided"]["alt017"]["cost"] == 54.99
    assert payload["guided"]["alt017"]["reduction"] == 4.14
    assert [item["adverse_count"] for item in payload["storms"]["items"]] == [1, 0, 420, 47]
    assert payload["storms"]["unique_adverse"] == 421
    assert payload["decision_funnel"]["counts"] == {
        "alternatives": 699,
        "volume_pass": 297,
        "volume_cost_pass": 46,
        "feasible": 12,
        "frontier": 4,
    }
    assert payload["search"]["gate_pass_count"] == 1
    assert payload["search"]["gate_fail_count"] == 4

