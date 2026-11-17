# W&B historical search benchmark report

`scripts/publish_ecocorridor_search_report.py` turns the committed EcoCorridor
search-gate evidence into one deterministic W&B run and one publisher-owned
W&B Report. It is a publication step, not a benchmark rerun or an application
build verification.

The report is deliberately blunt about the result:

- dataset disposition: `HISTORICAL_BENCHMARK_ONLY`;
- production authorization: `PROHIBITED`;
- adaptive ML: disabled;
- production search: scrambled Sobol;
- product narrative: **unverified product context** tied to the named source
  commit, not proof that the application was built or tested and not
  signed-installer, cloud-policy, or practitioner-acceptance evidence.

This boundary matters. The committed benchmark showed that Sobol plus
ExtraTrees did not pass the declared decision-quality gates. Publishing the
result makes the negative result useful and auditable; it does not turn the
historical benchmark into a production authorization.

## What is published

The publisher creates one deterministic W&B Run with
`job_type=search_benchmark` and `run_kind=release_gate`. It never resumes or
appends to an existing run. It logs:

- `search_benchmark_runs`: all 240 landscape × seed × budget × method records;
- `search_method_aggregates`: six method-and-budget aggregate rows;
- `release_gate_metrics`: each declared threshold and its pass/fail result;
- `resource_observation`: the bounded historical execution observation;
- `validated_inputs`: relative logical names, byte counts, SHA-256 hashes,
  media types, asset roles, licenses, and the public-upload authorization.

It also logs one immutable `benchmark-dataset` artifact version containing the
verified campaign, resource sidecar, search manifest, historical provenance,
four CC0 synthetic benchmark tables, frozen source-identity snapshots, and the
deterministic report plan. Before upload, every artifact input must have the
exact hash and byte count recorded in `benchmarks/ecocorridor/data-manifest.json`
and must be `PUBLIC`, `CC0-1.0`, `SELF_AUTHORED_SYNTHETIC`, synthetic,
`BENCHMARK_ONLY`, and free of real locations and species occurrences. The
separate `public_replay_allowed` field remains replay policy and is not treated
as W&B-upload policy.

The publisher reads each authorized payload as raw bytes and scans UTF-8 text
for local machine paths and credential-shaped material. Non-UTF-8 payloads are
rejected unless their central manifest record has the explicit
`wandb_public_binary_upload_allowed: true` field. Cloud artifact names are
repository-relative and no absolute input path is handed to the artifact.

The W&B Report uses the current
[Reports and Workspaces API](https://docs.wandb.ai/models/ref/wandb_workspaces)
pattern: `wandb_workspaces.reports.v2.Report`, narrative blocks, a filtered
`Runset`, charts, and summary-table panels. The Runset is constrained to the
exact run ID, plan SHA-256, campaign SHA-256, source repository and commit, and
run kind. W&B Tables provide the complete record views, and the Report and
receipt reference only the immutable artifact `:vN` qualified name. The
publisher does not create or rely on a mutable custom artifact alias.

## Why this report is useful

EcoCorridor is meant to help transportation, ecological, and wildlife teams
screen pre-reviewed connectivity intervention portfolios across uncertain
resistance hypotheses. Direct Circuitscape results—not surrogate predictions—
remain the reportable evidence.

The W&B report answers three practical questions in one place:

1. **What product context is being described?** An evidence-first
   portfolio-screening application with direct-result contracts, bounded
   categorical search, QA and assumption provenance, and inspectable cloud
   lineage. This is unverified context, not build evidence.
2. **What experiment was run?** A hidden-outcome offline replay over four
   synthetic landscapes, ten seeds, three methods, and two budgets, producing
   240 comparable records.
3. **What did the experiment justify?** It justified keeping deterministic
   scrambled Sobol. It did not justify enabling adaptive ExtraTrees.

That negative gate is environmentally important: an ML feature does not get to
influence the next evaluated portfolio merely because it looks sophisticated.
It must demonstrate better decision quality without degrading feasible-option
or safeguard/adverse recall.

## Network-free dry run

Install the locked EcoCorridor publisher and shared observability dependencies:

```bash
uv sync --locked --extra observability --extra ecocorridor-report
```

Run the full local verification and emit a deterministic plan before providing
credentials:

```bash
PYTHONPATH=python .venv/bin/python \
  scripts/publish_ecocorridor_search_report.py \
  --dry-run \
  --output /tmp/ecocorridor-wandb-search-report-plan.json
```

Dry-run mode does not import `wandb` or `wandb_workspaces`, does not inspect an
API key, and does not make a network request. It validates the campaign digest,
the 240-record matrix, exact-job arithmetic, the resource sidecar and its frozen
source identities, the central ecological data-manifest authorization and file
hashes, the raw artifact payload safety scan, and the historical `PROHIBITED`
provenance.

## Publish or update

Publication requires a clean Git tree, a source commit equal to checked-out
`HEAD`, an `origin` GitHub remote that exactly matches the `owner/repository`
named in the plan, an explicit entity and project, and an explicit authorization
for the allowlisted public synthetic files. Configure `WANDB_API_KEY` through
the approved environment or credential mechanism; never pass it as a
command-line argument.

Create or safely reuse the deterministic publication:

```bash
PYTHONPATH=python .venv/bin/python \
  scripts/publish_ecocorridor_search_report.py \
  --entity YOUR_ENTITY \
  --project YOUR_PROJECT \
  --authorize-synthetic-public-upload \
  --output /tmp/ecocorridor-wandb-search-report-receipt.json
```

Normally no Report URL is necessary. The publisher discovers the exact Report
using `wandb.Api.reports`, its deterministic title, and a description marker
that binds publisher schema version, plan SHA-256, and campaign SHA-256. For an
explicit recovery or migration, the same exact marked Report may be named by
URL:

```bash
PYTHONPATH=python .venv/bin/python \
  scripts/publish_ecocorridor_search_report.py \
  --entity YOUR_ENTITY \
  --project YOUR_PROJECT \
  --authorize-synthetic-public-upload \
  --report-url https://wandb.ai/YOUR_ENTITY/YOUR_PROJECT/reports/TITLE--REPORT_ID \
  --output /tmp/ecocorridor-wandb-search-report-receipt.json
```

The report URL must be credential-free HTTPS, resolve to the same explicit
entity/project, and carry the exact publisher marker. An unrelated Report is
never overwritten. Multiple matching Reports, a mismatched marker, or a marked
Report without its deterministic run blocks publication.

## Retry and recovery semantics

Before logging, the publisher queries the public API for the deterministic run
ID and the deterministic Report. The only reusable run state is `finished` with
the exact expected config, an immutable artifact `:vN` reference, and the full
publisher completion summary for this schema, plan, campaign, and central
manifest. If that exact state exists, a retry skips all Table and artifact
logging and only creates or refreshes the one owned Report.

The completion summary is written and the run is successfully finalized before
`Report.save()` is called. Consequently, a Report-save failure can be retried
without creating another run or artifact version. If a run exists in any other
state—including failed, running, crashed, a config mismatch, or a missing
completion marker—the publisher stops. It does not use `resume="allow"`, append
Tables, or guess whether a partial upload is safe. An operator must inspect and
remediate that deterministic run before trying again.

## Fail-closed guarantees

- Online mode cannot infer entity, project, or upload authorization from the
  environment.
- A dirty source tree cannot be published as a committed build snapshot.
- A Git origin that differs from the source repository named in the plan blocks
  before W&B API use.
- Tampered campaign, sidecar, manifest, provenance, source snapshot, dataset,
  trace reference, or exact-job arithmetic fails before W&B initialization.
- Every artifact input requires exact central-manifest authorization; a missing
  record, changed hash or size, non-public/non-CC0 classification, real-location
  or species flag, local path, credential-shaped value, or unauthorized binary
  file fails before W&B API use.
- The historical campaign must remain unpinned, scientifically failed, and
  production-disabled.
- The publisher snapshots every authorized input and the central policy
  manifest, then checks them during and after publication. It never writes
  `pinned_wandb_artifact`.
- Existing partial or mismatched deterministic runs, duplicate marked Reports,
  orphan Reports, and unrelated explicit Reports all block before new logging.
- W&B credentials are never accepted as arguments, included in the plan,
  printed, logged as run config, or written to the receipt.
- The resource observation remains an active-duration energy proxy and sampled
  memory lower bound. It is not measured electricity and is never converted
  into a carbon-savings claim.

The Report API is currently Public Preview, so its pinned dependency and the
publisher's fake-SDK contract tests should be reviewed whenever
`wandb-workspaces` is upgraded. See the official
[Reports reference](https://docs.wandb.ai/models/ref/wandb_workspaces/reports)
and [report creation guide](https://docs.wandb.ai/models/reports/create-a-report).
