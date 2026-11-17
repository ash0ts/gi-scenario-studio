from __future__ import annotations

from pathlib import Path

import pytest
from gi_scenario_core.orchestrator import (
    MetricExtractionError,
    SwmmReportedQuantityPair,
    extract_runoff_quantity_ledger,
)

BASELINE_LONG_REPORT = Path("benchmarks/guided_exact/runs/run_ec6a03d02287f847394c/report.rpt")
ALT_207_LONG_REPORT = Path("benchmarks/guided_exact/runs/run_c8220fe9c2697d04c9e4/report.rpt")


def _assert_pair(
    pair: SwmmReportedQuantityPair,
    primary: float,
    secondary: float,
    *,
    primary_units: str,
    secondary_units: str,
) -> None:
    assert pair.primary.value == pytest.approx(primary)
    assert pair.primary.units == primary_units
    assert pair.secondary.value == pytest.approx(secondary)
    assert pair.secondary.units == secondary_units


def test_extracts_baseline_long_event_runoff_and_routing_ledgers() -> None:
    ledger = extract_runoff_quantity_ledger(BASELINE_LONG_REPORT.read_text())

    runoff_units = {"primary_units": "acre-feet", "secondary_units": "inches"}
    _assert_pair(ledger.initial_lid_storage, 0.001, 0.002, **runoff_units)
    _assert_pair(ledger.total_precipitation, 0.937, 2.500, **runoff_units)
    _assert_pair(ledger.evaporation_loss, 0.000, 0.000, **runoff_units)
    _assert_pair(ledger.infiltration_loss, 0.412, 1.098, **runoff_units)
    _assert_pair(ledger.surface_runoff, 0.514, 1.371, **runoff_units)
    assert ledger.lid_drainage is None
    _assert_pair(ledger.final_storage, 0.012, 0.033, **runoff_units)
    assert ledger.runoff_continuity_error.value == pytest.approx(-0.005)
    assert ledger.runoff_continuity_error.units == "percent"

    routing_units = {"primary_units": "acre-feet", "secondary_units": "10^6 gal"}
    _assert_pair(ledger.wet_weather_inflow, 0.514, 0.168, **routing_units)
    _assert_pair(ledger.external_outflow, 0.514, 0.167, **routing_units)
    assert ledger.routing_continuity_error.value == pytest.approx(0.000)
    assert ledger.routing_continuity_error.units == "percent"


def test_extracts_alt_207_long_event_lid_drainage_and_rounded_balance() -> None:
    ledger = extract_runoff_quantity_ledger(ALT_207_LONG_REPORT.read_text())

    runoff_units = {"primary_units": "acre-feet", "secondary_units": "inches"}
    _assert_pair(ledger.initial_lid_storage, 0.002, 0.006, **runoff_units)
    _assert_pair(ledger.total_precipitation, 0.937, 2.500, **runoff_units)
    _assert_pair(ledger.evaporation_loss, 0.000, 0.000, **runoff_units)
    _assert_pair(ledger.infiltration_loss, 0.436, 1.163, **runoff_units)
    _assert_pair(ledger.surface_runoff, 0.447, 1.191, **runoff_units)
    assert ledger.lid_drainage is not None
    _assert_pair(ledger.lid_drainage, 0.014, 0.036, **runoff_units)
    _assert_pair(ledger.final_storage, 0.043, 0.116, **runoff_units)
    assert ledger.runoff_continuity_error.value == pytest.approx(-0.012)

    routing_units = {"primary_units": "acre-feet", "secondary_units": "10^6 gal"}
    _assert_pair(ledger.wet_weather_inflow, 0.460, 0.150, **routing_units)
    _assert_pair(ledger.external_outflow, 0.460, 0.150, **routing_units)
    assert ledger.routing_continuity_error.value == pytest.approx(-0.031)

    # Adding SWMM's independently rounded display rows leaves a small residual.
    # Preserve that fact rather than altering a row to manufacture exact balance.
    rounded_residual = ledger.rounded_runoff_balance_residual()
    assert rounded_residual.value == pytest.approx(-0.001)
    assert rounded_residual.units == "acre-feet"
    assert rounded_residual.value != pytest.approx(ledger.runoff_continuity_error.value)


def test_baseline_rounded_runoff_rows_happen_to_reconcile_exactly() -> None:
    ledger = extract_runoff_quantity_ledger(BASELINE_LONG_REPORT.read_text())

    rounded_residual = ledger.rounded_runoff_balance_residual()
    assert rounded_residual.value == pytest.approx(0.0, abs=1e-12)
    assert rounded_residual.units == "acre-feet"
    # The report continuity value comes from SWMM's full-precision accounting.
    assert ledger.runoff_continuity_error.value == pytest.approx(-0.005)


def test_missing_required_runoff_row_fails_closed() -> None:
    report = ALT_207_LONG_REPORT.read_text()
    report = "\n".join(line for line in report.splitlines() if "Infiltration Loss" not in line)

    with pytest.raises(MetricExtractionError, match="required row is absent: Infiltration Loss"):
        extract_runoff_quantity_ledger(report)


def test_missing_required_routing_row_fails_closed() -> None:
    report = ALT_207_LONG_REPORT.read_text()
    report = "\n".join(line for line in report.splitlines() if "Wet Weather Inflow" not in line)

    with pytest.raises(MetricExtractionError, match="required row is absent: Wet Weather Inflow"):
        extract_runoff_quantity_ledger(report)
