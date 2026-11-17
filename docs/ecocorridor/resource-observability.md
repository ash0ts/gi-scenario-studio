# Resource and energy observation method

EcoCorridor records computation without turning a software estimate into an
environmental benefit claim. Resource observations are diagnostic evidence;
they never change a portfolio, direct result, QA disposition, search order, or
export eligibility.

## Direct Circuitscape jobs

Each executed portfolio × scenario job receives one process-scoped record:

- monotonic wall time from process launch through result serialization;
- generalized OS, architecture, and logical-CPU context (no machine serial or
  local path);
- resident memory sampled every 100 ms with the platform process API;
- the maximum observed sample, explicitly marked as a lower bound because a
  shorter peak may occur between samples; and
- an `ESTIMATED` energy value based on active process duration.

Linux uses `/proc/<pid>/status` (`VmHWM`, falling back to `VmRSS`), macOS uses
`proc_pid_rusage` physical footprint, and Windows uses
`GetProcessMemoryInfo` peak working set. If that process API cannot be read,
memory remains `NOT_MEASURED`; it is never written as zero.

The v1 energy proxy is:

```text
estimated kWh = solver wall seconds × 15 W / 3,600,000
scenario lower = solver wall seconds × 5 W / 3,600,000
scenario upper = solver wall seconds × 40 W / 3,600,000
```

Those coefficients are committed methodological assumptions for one active,
single-threaded solver process. They are intentionally broad and are not a
hardware calibration, TDP measurement, wall-plug meter, or probabilistic
confidence interval. The boundary includes Julia startup, input parsing,
Circuitscape solve, and result serialization. It excludes Python orchestration,
cloud synchronization, display, networking, cooling, idle host demand, and
embodied energy. Concurrent jobs are estimated independently from their own
active durations.

Cache reuse records only lookup wall time. It does not borrow the source
solver's memory or energy observation and does not claim a zero-cost cache hit.

### Process-tree completion boundary

Resource sampling and scientific completion are separate claims. On POSIX, a
direct job is reportable only after the complete isolated process group is
confirmed absent. Natural exit and bounded termination record distinct methods;
a direct-child return code, PID-only lookup, or signal delivery cannot prove
that descendants stopped writing outputs. Restart recovery uses the same exact
attempt-bound proof.

Windows memory sampling is supported, but Windows release-evidence production
is not. `CREATE_NEW_PROCESS_GROUP`, `taskkill /T`, and waiting for the direct
child do not provide the required authoritative descendant-count proof. Release
support requires launcher-owned native Job Object assignment from process
creation and an active-process-zero query for every exit path.

## Search proposals and benchmark campaign

Interactive portfolio-plan proposals use the same process sampler around the
bounded proposer. ExtraTrees is configured with `n_jobs=1`. Search memory is
the whole worker's resident footprint during that interval—not an incremental
allocation measurement—and its record says that unrelated concurrent worker
activity may be included. The immutable
search release campaign keeps machine-dependent values outside its scientific
campaign digest. Its CLI writes a separately digested
`release-gate.resources.json` sidecar that pins the deterministic campaign
SHA-256 and records campaign-process timing, sampled memory, and the same
clearly labelled energy proxy.

This separation preserves repeatable scientific scores while retaining honest
execution evidence. The committed sidecar belongs to a historical campaign and
resolves its recorded generator hashes to passive byte-identical snapshots; it
is not rebound to current source when that source changes. Its provenance is
explicitly `HISTORICAL_BENCHMARK_ONLY` with production authorization
`PROHIBITED`. The snapshots prove byte identity, not authenticated execution.
A new production candidate requires a separate current campaign, a passing
scientific gate, and immutable W&B lineage.

## Claim boundary

- `MEASURED` applies to monotonic time and sampled process-memory readings.
- `ESTIMATED` applies to the active-power energy proxy.
- `NOT_MEASURED` remains valid when a platform process API or approved energy
  method is unavailable.
- No observation is converted to CO₂e or presented as carbon saved.
- Sums are labelled as sums of process-duration estimates, not facility or
  wall-plug electricity use.

A deployment with a real meter should record a separate measured method and
its exact electrical boundary rather than relabelling this estimate.
