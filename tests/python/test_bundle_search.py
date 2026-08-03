from __future__ import annotations

import hashlib
import stat
import zipfile

import pytest
from gi_scenario_core.bundle import BundleSecurityError, inspect_bundle, safe_extract_bundle
from gi_scenario_core.search import (
    make_search_plan,
    nondominated_indices,
    present_value_cost,
    propose_adaptive,
)


def _zip(path, members):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)


def test_bundle_inspection_hashes_and_resolves_windows_file_reference(tmp_path):
    archive = tmp_path / "project.zip"
    model = """[RAINGAGES]
RG1 INTENSITY 0:05 1 FILE "rain\\event.dat" STA MM
[OPTIONS]
FLOW_UNITS CFS
"""
    _zip(archive, {"model/base.inp": model, "model/rain/event.dat": "rain"})
    inspection = inspect_bundle(archive)
    assert inspection.valid
    assert inspection.models[0].references == [r"rain\event.dat"]
    assert inspection.models[0].resolved == ["model/rain/event.dat"]
    assert all(len(item.sha256) == 64 for item in inspection.files)
    assert inspection.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert inspection.archive_bytes == archive.stat().st_size


def test_bundle_identity_fails_closed_when_staged_bytes_change(tmp_path):
    archive = tmp_path / "project.zip"
    _zip(archive, {"base.inp": "[OPTIONS]\nFLOW_UNITS CFS\n"})
    expected_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    expected_bytes = archive.stat().st_size
    _zip(archive, {"base.inp": "[OPTIONS]\nFLOW_UNITS CMS\n"})

    with pytest.raises(BundleSecurityError, match="hash changed"):
        inspect_bundle(
            archive,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
        )


def test_bundle_reports_missing_dependency(tmp_path):
    archive = tmp_path / "project.zip"
    _zip(archive, {"base.inp": '[TIMESERIES]\nTS1 FILE "missing.dat"\n'})
    inspection = inspect_bundle(archive)
    assert not inspection.valid
    assert inspection.models[0].missing == ["missing.dat"]


@pytest.mark.parametrize("name", ["../escape.inp", "/absolute.inp", "C:\\evil.inp"])
def test_bundle_rejects_unsafe_paths(tmp_path, name):
    archive = tmp_path / "bad.zip"
    _zip(archive, {name: "[OPTIONS]\n"})
    with pytest.raises(BundleSecurityError):
        inspect_bundle(archive)


def test_bundle_rejects_executables_and_symlinks(tmp_path):
    executable = tmp_path / "exec.zip"
    _zip(executable, {"model.inp": "[OPTIONS]\n", "payload.exe": b"MZ"})
    with pytest.raises(BundleSecurityError, match="executable"):
        inspect_bundle(executable)

    symlink = tmp_path / "link.zip"
    with zipfile.ZipFile(symlink, "w") as archive:
        info = zipfile.ZipInfo("model.inp")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    with pytest.raises(BundleSecurityError, match="links"):
        inspect_bundle(symlink)


def test_safe_extract_writes_only_inspected_content(tmp_path):
    archive = tmp_path / "good.zip"
    _zip(archive, {"nested/base.inp": "[OPTIONS]\nFLOW_UNITS CFS\n"})
    destination = tmp_path / "extracted"
    safe_extract_bundle(archive, destination)
    assert (destination / "nested" / "base.inp").is_file()


def test_search_enumerates_when_universe_fits_budget(approved_slots):
    plan = make_search_plan(
        approved_slots,
        {"max_solver_jobs": 30, "seed": 7},
        storm_count=2,
    )
    assert plan.method == "enumeration"
    assert plan.finite_universe_size == 12
    assert len(plan.candidates) == 12
    assert plan.planned_solver_jobs == 26


def test_qmc_plan_is_deterministic_and_never_exceeds_cross_product_budget(approved_slots):
    budget = {"max_solver_jobs": 10, "seed": 999}
    first = make_search_plan(approved_slots, budget, storm_count=2)
    second = make_search_plan(approved_slots, budget, storm_count=2)
    assert [item.vector for item in first.candidates] == [item.vector for item in second.candidates]
    assert first.planned_solver_jobs <= first.max_solver_jobs
    assert first.method == "qmc"


def test_search_rejects_unapproved_candidate(approved_slots):
    approved_slots[0]["engineer_approved"] = False
    with pytest.raises(ValueError, match="engineer-approved"):
        make_search_plan(approved_slots, {"max_solver_jobs": 10}, storm_count=1)


def test_nondominated_set_respects_maximize_and_minimize():
    rows = [
        {"cost": 10, "reduction": 5},
        {"cost": 12, "reduction": 8},
        {"cost": 15, "reduction": 4},
    ]
    assert nondominated_indices(rows, {"cost": "minimize", "reduction": "maximize"}) == [0, 1]


def test_adaptive_count_one_returns_exactly_one_without_sklearn_dependency(approved_slots):
    candidates, diagnostics = propose_adaptive(
        approved_slots,
        [{"vector": [0, 0], "metrics": {"flood": 10}}],
        objective_directions={"flood": "minimize"},
        count=1,
        seed=5,
    )
    assert len(candidates) == 1
    assert diagnostics["method"] == "qmc_production_gate"
    assert diagnostics["ml_eligible"] is False


def test_present_value_cost_requires_confirmed_complete_cost_basis(approved_slots):
    with pytest.raises(ValueError, match="confirmed"):
        present_value_cost(
            [1, 1],
            approved_slots,
            {
                "unit_capex": {"slot_a": 100, "slot_b": 200},
                "annual_om": {"slot_a": 0, "slot_b": 0},
                "price_vintage": "2026",
                "source": "fixture",
            },
        )
    with pytest.raises(ValueError, match="missing"):
        present_value_cost(
            [1, 1],
            approved_slots,
            {
                "unit_capex": {"slot_a": 100},
                "annual_om": {"slot_a": 0, "slot_b": 0},
                "price_vintage": "2026",
                "source": "fixture",
                "confirmed": True,
            },
        )
    cost = present_value_cost(
        [1, 1],
        approved_slots,
        {
            "unit_capex": {"slot_a": 100, "slot_b": 200},
            "annual_om": {"slot_a": 0, "slot_b": 0},
            "price_vintage": "2026",
            "source": "fixture",
            "confirmed": True,
        },
    )
    assert cost == 300
