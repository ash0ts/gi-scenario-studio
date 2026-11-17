from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from ecocorridor_core.contracts import (
    ResourceObservationBoundary,
    ResourceObservationStatus,
)
from ecocorridor_core.resource_observability import (
    ObservedProcessCancelled,
    ObservedProcessTimeout,
    ProcessResourceRecorder,
    _MemoryRead,
    run_observed_process,
    unmeasured_cache_observation,
)


def _process_tree_command(
    marker: Path,
    *,
    ready_marker: Path,
    marker_delay: float = 0.45,
) -> list[str]:
    child = (
        "import pathlib,sys,time; "
        f"time.sleep({marker_delay!r}); "
        "pathlib.Path(sys.argv[1]).write_text('orphan survived', encoding='utf-8')"
    )
    parent = (
        "import pathlib,subprocess,sys,time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "print('parent-started', flush=True); "
        "pathlib.Path(sys.argv[3]).write_text('ready', encoding='utf-8'); "
        "time.sleep(30)"
    )
    return [sys.executable, "-c", parent, child, str(marker), str(ready_marker)]


def test_direct_process_memory_is_sampled_and_energy_is_explicitly_estimated(
    tmp_path: Path,
) -> None:
    observed = run_observed_process(
        [
            sys.executable,
            "-c",
            "import time; payload=bytearray(16*1024*1024); time.sleep(0.2); print(len(payload))",
        ],
        cwd=tmp_path,
        env=os.environ,
        timeout_seconds=10,
        observation_id="resource:test-direct",
        boundary_description="One isolated test process.",
        sample_interval_seconds=0.02,
    )
    assert observed.completed.returncode == 0
    assert observed.completed.stdout.strip() == b"16777216"
    resource = observed.observation
    assert resource.boundary == ResourceObservationBoundary.DIRECT_SOLVER
    assert resource.peak_memory_status == ResourceObservationStatus.MEASURED
    assert (resource.peak_memory_bytes or 0) >= 16 * 1024 * 1024
    assert resource.peak_memory_sample_count >= 1
    assert resource.peak_memory_is_lower_bound is True
    assert resource.energy_status == ResourceObservationStatus.ESTIMATED
    assert (resource.energy_estimate_lower_kwh or 0) <= (resource.energy_kwh or 0)
    assert (resource.energy_kwh or 0) <= (resource.energy_estimate_upper_kwh or 0)
    assert resource.energy_estimate_watts == 15.0
    assert "carbon" in " ".join(resource.exclusions).casefold()
    assert observed.process_exit.process_id > 0
    assert len(observed.process_exit.process_identity) == 64
    assert observed.process_exit.termination_reason == "natural_exit"
    if os.name == "posix":
        assert observed.process_exit.process_group_id == observed.process_exit.process_id
        assert observed.process_exit.tree_exit_confirmed is True
        assert (
            observed.process_exit.confirmation_method
            == "POSIX_PROCESS_GROUP_ABSENT_AFTER_NATURAL_EXIT"
        )


def test_unavailable_memory_api_stays_not_measured_while_energy_remains_estimated() -> None:
    recorder = ProcessResourceRecorder(
        pid=os.getpid(),
        observation_id="resource:test-search",
        boundary=ResourceObservationBoundary.SEARCH_PROPOSER,
        boundary_description="One bounded test proposal.",
        sample_interval_seconds=0.01,
        memory_reader=lambda _pid: None,
    )
    time.sleep(0.02)
    resource = recorder.finish()
    assert resource.peak_memory_status == ResourceObservationStatus.NOT_MEASURED
    assert resource.peak_memory_bytes is None
    assert resource.peak_memory_sample_count == 0
    assert resource.energy_status == ResourceObservationStatus.ESTIMATED
    assert (resource.energy_kwh or 0) > 0


def test_sampler_records_maximum_observed_value_not_last_value() -> None:
    values = iter((100, 900, 250, 250))

    def reader(_pid: int) -> _MemoryRead:
        return _MemoryRead(next(values, 250), "TEST_SAMPLED_RSS")

    recorder = ProcessResourceRecorder(
        pid=os.getpid(),
        observation_id="resource:test-peak",
        boundary=ResourceObservationBoundary.SEARCH_PROPOSER,
        boundary_description="One bounded test proposal.",
        sample_interval_seconds=0.005,
        memory_reader=reader,
    )
    time.sleep(0.02)
    resource = recorder.finish()
    assert resource.peak_memory_bytes == 900
    assert resource.peak_memory_method == "TEST_SAMPLED_RSS"


def test_cache_lookup_does_not_borrow_solver_energy_or_memory() -> None:
    resource = unmeasured_cache_observation(
        observation_id="resource:test-cache",
        wall_seconds=0.001,
    )
    assert resource.boundary == ResourceObservationBoundary.CACHE_REUSE
    assert resource.cache_lookup_wall_seconds == 0.001
    assert resource.peak_memory_status == ResourceObservationStatus.NOT_MEASURED
    assert resource.energy_status == ResourceObservationStatus.NOT_MEASURED
    assert resource.energy_kwh is None


def test_timed_out_process_retains_estimated_resource_observation(tmp_path: Path) -> None:
    with pytest.raises(ObservedProcessTimeout) as captured:
        run_observed_process(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            cwd=tmp_path,
            env=os.environ,
            timeout_seconds=0.05,
            observation_id="resource:test-timeout",
            boundary_description="One isolated timed-out test process.",
            sample_interval_seconds=0.01,
        )
    observation = captured.value.resource_observation
    assert observation.boundary == ResourceObservationBoundary.DIRECT_SOLVER
    assert observation.energy_status == ResourceObservationStatus.ESTIMATED
    assert (observation.energy_kwh or 0) > 0
    assert captured.value.process_exit.process_id > 0
    assert len(captured.value.process_exit.process_identity) == 64
    assert captured.value.process_exit.termination_reason == "timeout"
    if os.name == "posix":
        assert captured.value.process_exit.tree_exit_confirmed is True
        assert "PROCESS_GROUP_ABSENT" in captured.value.process_exit.confirmation_method


def test_cancelled_process_terminates_its_tree_and_retains_output_and_observation(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "post-cancel-marker.txt"
    ready_marker = tmp_path / "parent-ready.txt"
    ready_seen_at: float | None = None

    def cancel_after_parent_ready() -> bool:
        nonlocal ready_seen_at
        if not ready_marker.exists():
            return False
        if ready_seen_at is None:
            ready_seen_at = time.monotonic()
            return False
        return time.monotonic() - ready_seen_at >= 0.05

    marker_delay = 0.75
    started = time.monotonic()
    with pytest.raises(ObservedProcessCancelled) as captured:
        run_observed_process(
            _process_tree_command(
                marker,
                ready_marker=ready_marker,
                marker_delay=marker_delay,
            ),
            cwd=tmp_path,
            env=os.environ,
            timeout_seconds=10,
            observation_id="resource:test-cancel-tree",
            boundary_description="One cancelled process-tree test.",
            sample_interval_seconds=0.01,
            cancel_requested=cancel_after_parent_ready,
            poll_interval_seconds=0.01,
            termination_grace_seconds=0.1,
            kill_wait_seconds=0.5,
        )
    assert ready_marker.is_file()
    assert time.monotonic() - started < 5
    assert b"parent-started" in captured.value.output
    assert captured.value.resource_observation.boundary == (
        ResourceObservationBoundary.DIRECT_SOLVER
    )
    assert captured.value.resource_observation.energy_status == (
        ResourceObservationStatus.ESTIMATED
    )
    assert captured.value.process_exit.process_id > 0
    assert captured.value.process_exit.process_group_id == captured.value.process_exit.process_id
    assert len(captured.value.process_exit.process_identity) == 64
    assert captured.value.process_exit.termination_reason == "cancelled"
    if os.name == "posix":
        assert captured.value.process_exit.tree_exit_confirmed is True
        assert "PROCESS_GROUP_ABSENT" in captured.value.process_exit.confirmation_method
    time.sleep(marker_delay + 0.2)
    assert not marker.exists(), "a cancelled solver descendant survived its process group"


def test_timeout_terminates_descendant_tree_before_it_can_write(tmp_path: Path) -> None:
    marker = tmp_path / "post-timeout-marker.txt"
    ready_marker = tmp_path / "parent-ready.txt"
    marker_delay = 3.0
    with pytest.raises(ObservedProcessTimeout) as captured:
        run_observed_process(
            _process_tree_command(
                marker,
                ready_marker=ready_marker,
                marker_delay=marker_delay,
            ),
            cwd=tmp_path,
            env=os.environ,
            timeout_seconds=1.5,
            observation_id="resource:test-timeout-tree",
            boundary_description="One timed-out process-tree test.",
            sample_interval_seconds=0.01,
            poll_interval_seconds=0.01,
            termination_grace_seconds=0.1,
            kill_wait_seconds=0.5,
        )
    assert ready_marker.is_file()
    assert b"parent-started" in (captured.value.output or b"")
    assert captured.value.process_exit.process_id > 0
    assert captured.value.process_exit.process_group_id == captured.value.process_exit.process_id
    assert captured.value.process_exit.termination_reason == "timeout"
    if os.name == "posix":
        assert captured.value.process_exit.tree_exit_confirmed is True
    time.sleep(marker_delay + 0.2)
    assert not marker.exists(), "a timed-out solver descendant survived its process group"
