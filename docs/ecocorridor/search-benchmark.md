# Search benchmark and adaptive-release gate

EcoCorridor treats ML as a candidate-ordering aid, never as connectivity
evidence. The benchmark replays search against immutable tables produced from
complete direct Circuitscape scenario ensembles. A method sees categorical
portfolio identities only. A stored outcome is revealed only after the method
spends one portfolio evaluation, which is charged as one direct job for every
scenario in that landscape.

The release comparison includes random sampling, scrambled Sobol, and Sobol
plus CPU ExtraTrees. ExtraTrees starts after 64 complete portfolios, uses
barriered deterministic batches, and reserves at least 25% of each batch for
Sobol exploration. Replay artifacts contain selected portfolio IDs and observed
scores; they never contain predicted connectivity values or maps.

## Build the direct tables

Use the independently packaged current Ridgeway direct-evidence v1.2 study;
the retained v1.0 archive is benchmark-only and cannot serve as current model
evidence. Derive a new, separately versioned four-hypothesis campaign from the
current evidence index rather than relabelling the existing historical search
campaign:

```bash
ecocorridor-benchmark derive-ridgeway \
  --evidence-index /approved/ridgeway/evidence-index.json \
  --output-directory /approved/current-search-v1
```

The command accepts the current `ridgeway-evidence-index/v1.1` only when all
1,536 matrix jobs and all 60 retained map jobs are present. It checks the bound
physics, decision, workflow, engine-environment, and engine-manifest hashes,
unique confirmed process identities, natural process-tree exit evidence, QA,
and direct-result hashes before deriving anything. Partial, test-only,
QA-rejected, non-Circuitscape, or cross-revision inputs fail closed. Each
derived row retains the three direct target/safeguard run IDs and immutable
result hashes used for that resistance landscape. The resulting v2 tables are
CC0 synthetic algorithm-test data and are explicitly not ecological decision
evidence. Keep them outside the historical `benchmarks/ecocorridor/search`
directory until their separate campaign, resource sidecar, central data
classification, and immutable W&B lineage are complete.

## Inspect one replay

```bash
ecocorridor-benchmark replay \
  --dataset benchmarks/ecocorridor/search/h1_low_barrier.json \
  --method sobol_extratrees \
  --seed 0 \
  --budget 112 \
  --output /tmp/ecocorridor-replay.json
```

## Regenerate the release gate

The committed campaign uses all four direct tables, ten seeds, and the declared
80- and 112-portfolio budgets:

```bash
ecocorridor-benchmark campaign \
  --dataset benchmarks/ecocorridor/search/h1_low_barrier.json \
  --dataset benchmarks/ecocorridor/search/h2_moderate_barrier.json \
  --dataset benchmarks/ecocorridor/search/h3_high_barrier.json \
  --dataset benchmarks/ecocorridor/search/h4_future_pressure.json \
  --budget 80 \
  --budget 112 \
  --seed-start 0 \
  --seed-count 10 \
  --output benchmarks/ecocorridor/search/release-gate.json \
  --resource-output benchmarks/ecocorridor/search/release-gate.resources.json
```

The scientific campaign JSON remains deterministic. The second file is a
separately digested, campaign-SHA-bound execution observation: proposer wall
time, sampled process-memory lower bound, generalized hardware, and a
clearly-labelled active-power proxy energy estimate with 5/15/40 W lower,
nominal, and upper coefficients. The estimate is not a meter reading and is
never converted into carbon or used to authorize adaptive search.

Verify the committed digests without rerunning model selection:

```bash
ecocorridor-benchmark verify \
  --report benchmarks/ecocorridor/search/release-gate.json \
  --resource-report benchmarks/ecocorridor/search/release-gate.resources.json \
  --historical-provenance benchmarks/ecocorridor/search/historical-provenance.json \
  --manifest benchmarks/ecocorridor/search/manifest.json \
  --dataset benchmarks/ecocorridor/search/h1_low_barrier.json \
  --dataset benchmarks/ecocorridor/search/h2_moderate_barrier.json \
  --dataset benchmarks/ecocorridor/search/h3_high_barrier.json \
  --dataset benchmarks/ecocorridor/search/h4_future_pressure.json
```

## Fail-closed production rule

The scientific gate requires all of the following:

- At least three distinct direct landscapes and ten seeds.
- At least 5% median hypervolume improvement over equal-budget Sobol at two
  declared budgets.
- No more than 2% regression in tenth-percentile hypervolume.
- No feasible-alternative recall regression in any paired campaign result.
- At least 95% safeguard/adverse recall in every maximum-budget adaptive run.

For a current, production-eligible campaign, passing those thresholds is still
insufficient until the exact campaign is linked by an immutable W&B artifact version such as
`wandb-artifact://entity/project/name:v3` and its campaign SHA-256 is pinned in
the release configuration. An alias such as `latest`, a local checkbox, or an
unpinned report cannot authorize ExtraTrees. Sobol remains the production
method whenever any requirement is absent. The historical campaign documented
below is independently `PROHIBITED`, so adding a W&B pin cannot promote it.

Machine-dependent resource values remain outside the deterministic search
trace and scientific campaign digest. The bound sidecar uses an explicit
single-process active-duration proxy because no wall-plug meter is available;
its wide coefficient bounds, memory-sampling interval, included boundary, and
exclusions are preserved. Direct-study jobs use the same method per isolated
solver process. Neither observation supports a carbon-savings claim.

The committed campaign is explicitly `HISTORICAL_BENCHMARK_ONLY`, with
production authorization `PROHIBITED`. Its resource sidecar remains unchanged
and resolves the source hashes it recorded to passive `.py.txt` snapshots. The
snapshots establish byte identity only; they are not an authenticated execution
attestation. Current implementation files may evolve without rewriting that
historical claim. A future production benchmark must be derived and executed as
a separate, versioned campaign from current direct evidence.

## Current pinned source result

The committed unpinned campaign has campaign digest
`d43c377f886d753105f5fd8bf47e6763ddaa40763537740d1136b702cd39b509`.
Its search manifest file has SHA-256
`1b16e698afeec8a53e489761dad21a280ea838435180cc0c9957969897a96429`;
the complete release-gate report file has SHA-256
`36e32fa1d154a9151aa9317b46fbf667800533561a5140052454da9494581b8f`,
and its resource-observation sidecar has SHA-256
`b627ac329d4c0dc71c80236bbfe05f551417a800269b24b9e7e977726753488a`.
All three files and all four source tables are independently pinned by the
central ecological release-asset manifest.
It did **not** pass the scientific gate:

- Median hypervolume improvement was 0.895% at budget 80 and 0.726% at budget
  112, below the required 5% at each budget.
- Worst paired feasible-recall change was -1.818 percentage points.
- Minimum maximum-budget safeguard/adverse recall was 75.385%, below 95%.
- Tenth-percentile hypervolume regression was 0%.

No W&B artifact version is asserted. Therefore
`production_adaptive_enabled` is `false` for two independent reasons. The
production method remains scrambled Sobol; ExtraTrees remains experimental.
