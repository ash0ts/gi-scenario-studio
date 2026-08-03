# Cedar Trace exhaustive exact benchmark

`dataset.json` is the immutable schema-2.0 oracle for search-method evaluation.
It contains all 700 bounded integer vectors in the Cedar Trace teaching space:

- one baseline vector evaluated in four synthetic storms;
- 699 nonbaseline vectors evaluated in the same four storms; and
- 2,800 direct EPA SWMM 5.2.4 jobs in total.

Each row retains the exact per-storm metrics and input/report/output hashes. The
2,800 duplicate INP/RPT/OUT file sets are intentionally not committed here; the
four baselines and four guided teaching alternatives retain complete files in
`../guided_exact/`.

The table is *hidden by the replay harness*, not confidential. A search method
can reveal a row only after requesting that vector, and every request is charged
as four direct-SWMM-job equivalents. This prevents benchmark methods from
peeking at unevaluated outcomes while avoiding wasteful hydraulic reruns.

Regenerate with an official 5.2.4 console binary:

```bash
PYTHONPATH=python python scripts/generate_exhaustive_benchmark.py \
  --solver /path/to/runswmm \
  --output /new/empty/directory
```

Replay a method locally:

```bash
PYTHONPATH=python python -m gi_scenario_core.benchmark \
  --dataset benchmarks/exhaustive_exact/dataset.json \
  --method sobol \
  --budget 128 \
  --seed 42
```

The fixture is synthetic CC0 teaching evidence. It is not a calibrated model,
an adopted design storm, a flood-risk determination, or design guidance.
