# Usefulness-landscape input program

This program supplies five independent, self-authored CC0 test landscapes for
the EcoCorridor usefulness studies. They are input fixtures, not ecological
evidence. No committed record in this program contains an effective-resistance
result, a current-density result, or a portfolio disposition.

## Structural families

| Family | Test structure | Why it is included |
|---|---|---|
| Linear bottleneck | One high-resistance barrier with a narrow opening | Simple bottleneck and clean-negative controls |
| Braided dual barrier | Two offset barriers plus a diagonal low-resistance branch | Alternative paths, review-mask diagnostics, and hypothesis ordering |
| Fragmented peri-urban mosaic | Orthogonal high-resistance strips and blocks | Fragmentation, costs, and unauthorized-effect controls |
| Boundary-sensitive | Edge routes, central resistance, and retained NODATA notches | Boundary, hash, and QA failure handling |
| Asymmetric multi-core | Four uneven focal cores and several required pairs | Multi-pair completeness and asymmetric hypothesis behavior |

Every family has one exact self-authored test grid, identical NODATA topology across its
resistance, focal, and review-mask rasters, four scenarios including a safeguard,
three non-overlapping self-authored action sites, and the exhaustive eight-portfolio
binary universe. The full input program therefore plans:

```text
5 landscapes × 8 portfolios × 4 scenarios = 160 direct jobs
```

All 160 requests ask the pinned raster-pairwise engine for both a matrix and a
current raster. The current raster is diagnostic only and cannot be interpreted
as animal use.

## Materialize executable inputs

From the repository root:

```bash
PYTHONPATH=python .venv/bin/python \
  scripts/materialize_usefulness_landscapes.py \
  /an/empty/output/directory
```

Each generated run directory contains `request.json` and isolated `inputs/`
resources accepted by the existing packaged `ecocorridor-engine` protocol:

```bash
cd /an/empty/output/directory/<family>/runs/<direct-run-id>
/path/to/ecocorridor-engine --request request.json --result result.json
```

Materialization intentionally creates no `result.json` or `outputs/` directory.
The committed benchmark manifest and generated study plans remain
`PENDING_DIRECT_CIRCUITSCAPE`. Running the executable is only the first step;
the retained result still must pass the normal parser, QA, complete-ensemble,
artifact-hash, and evidence-resolution gates.

## Execute and verify direct evidence

The release executor requires the explicit packaged-engine path and its
approved SHA-256 identity. It never discovers an engine from `PATH`, never
uses a shell, and runs at most eight isolated single-threaded jobs at once:

```bash
PYTHONPATH=python .venv/bin/python scripts/run_usefulness_direct_study.py run \
  --materialized /path/to/materialized-inputs \
  --output /path/to/separate/evidence-directory \
  --engine /path/to/packaged/ecocorridor-engine \
  --engine-manifest /path/to/packaged/engine-manifest.json \
  --engine-provenance /path/to/attested/engine-provenance.json \
  --engine-sha256 <approved-lowercase-sha256> \
  --workers 4
```

Use `--job-limit N` with `N < 160` for a non-release smoke run. Successful
jobs are resumed only after their completion record, request, raw matrix,
current raster, effective configuration, resource observation, and every
retained artifact are revalidated. A complete package is refused unless the
producer repository is clean and committed at both the start and end of the
run. The package records `source_repository`, `source_commit`, and
`source_tree_clean=true`; changing source during execution blocks packaging.
Before creating any production job, the runner also requires the typed engine manifest to
bind the selected executable and the current reviewed Julia manifest, Julia
wrapper, Python protocol parser, and metric definitions. A stale packaged
wrapper is rejected; the strict relative-path effective INI contract is not
relaxed to accommodate an old bundle. The typed `engine_manifest_sha256` is
recorded in every completion, cache key, evidence index, and public package.
That manifest is necessary but not sufficient: a fake executable can hash
itself into a self-consistent manifest. Production execution therefore also
verifies a GitHub/Sigstore attestation over an engine-provenance receipt emitted
by the protected signed-release workflow. The receipt binds the exact
executable and manifest to the signed release inventory and exact packaged
results for the 4-ohm series case, 1-ohm parallel case, and Circuitscape 5.17.1
official `sgVerify4` fixture. The repository, protected workflow, main-branch
source digest, and non-self-hosted builder policy are all enforced.

Local PackageCompiler builds and fake runners must use `--test-only-engine`.
Even if all 160 jobs pass numerical QA, that mode returns
`COMPLETED_TEST_ONLY_UNPUBLISHABLE`, writes no public evidence package, and
cannot be consumed by the production verifier or W&B publisher.

The CLI returns `0` only for a complete package or successful independent
verification, `2` for an intentionally incomplete smoke run or a completed
ensemble whose source became dirty before packaging, and `3` for a rejected
preflight, direct job, QA result, cache record, or package.

Independently verify all 160 archived jobs with:

```bash
PYTHONPATH=python .venv/bin/python scripts/run_usefulness_direct_study.py verify \
  --manifest /path/to/direct-evidence-manifest.json \
  --archive /path/to/usefulness-direct-evidence-<hash>.tar.gz \
  --engine-manifest /path/to/packaged/engine-manifest.json \
  --engine-provenance /path/to/attested/engine-provenance.json \
  --engine-sha256 <approved-lowercase-sha256>
```

The executor retains direct evidence but deliberately leaves outcome promotion
at `BLOCKED_PENDING_ANALYSIS_AND_ADJUDICATION`. The test suite exercises the
complete 160-job path with an explicitly fake engine; those test values are
quarantined and are not Circuitscape evidence.

## Case-truth boundary

Input-policy controls are known before solving: cost arithmetic, incomplete
safeguard blocking, stale hashes, unauthorized effects, rejected QA, and benign
unchanged metadata. Conditions that depend on numerical results—clean modeled
disposition, review-mask advisory, and hypothesis-sensitive ordering—are
explicitly pending. They may be assigned only after all relevant direct
matrices and current rasters exist and pass QA.

The planned acceptance pass must additionally verify:

- every portfolio has a successful, QA-accepted target-and-safeguard ensemble;
- raw matrices reparse to the stored pair values;
- current-map and matrix executions retain one shared request identity;
- action replacements never modify the grid or NODATA topology;
- current-map QA uses the retained resistance raster as the authoritative mask:
  source-NODATA cells must be either NODATA or exactly zero current, and the
  accepted encoding is recorded rather than described as identical topology;
- official Circuitscape fixtures and hand-calculated series/parallel checks pass;
- no generated copy implies animal movement, crossing use, collision reduction,
  population response, biodiversity benefit, approval, or a design-ready action.

The canonical repository metadata is
[`benchmarks/ecocorridor/usefulness/manifest.json`](../../benchmarks/ecocorridor/usefulness/manifest.json).
Its `program_sha256`, per-landscape definition hashes, and run-plan hashes bind
the exact authored inputs that must be executed. These inputs are unadjudicated;
independent ecological and transportation adjudication remains required before
they can serve as frozen case truth for a practitioner study.
