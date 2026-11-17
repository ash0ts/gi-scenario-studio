# Architecture and authority boundaries

```text
Eco React UI
    ↕ versioned JSON-RPC 1.0
Tauri host / scenario-host
    ↕ JSONL over stdio; credentials only during initialization
Python scientific worker + Eco SQLite
    ├── content-addressed artifacts
    ├── transactional W&B/Weave outbox
    └── isolated prepared-run directory
            ↕ strict JSON request/result files
        packaged Julia + Circuitscape 5.17.1
```

## Sources of authority

1. SQLite and content-addressed local artifacts are the engineering authority.
2. The Python worker is the sole writer of project state.
3. Circuitscape is the sole source of reportable connectivity values.
4. W&B is a lineage and collaboration mirror, never a scientific authority.
5. Weave is the mandatory trace and evaluation plane for assistants, never an
   alternative evidence source.
6. React state is disposable presentation state.

The generated JSON Schema, TypeScript, and Zod artifacts provide versioned
transport shape validation. Cross-record scientific invariants—such as exact
reference-claim coverage, derived pass/fail, mandatory limitations, and model
fitness—remain authoritative Pydantic validation in the Python worker. Every
RPC payload is revalidated there before it can change state or become evidence;
the renderer must never use a structurally valid Zod value to infer scientific
eligibility on its own.

## Direct-run transaction

One direct job is one portfolio × one complete ecological scenario. In one
SQLite transaction, `finalize_direct_job` writes the terminal state, immutable
artifact references, typed metrics, QA, cache entry, evidence nodes and edges,
and telemetry envelope. A cloud worker may upload only after that transaction
commits. Retry order and cloud availability cannot affect portfolio selection,
solver input, QA, decision results, or exports.

The production executor queues the baseline scenario ensemble first, then
orders portfolio and scenario IDs deterministically. It runs in a background
thread so JSON-RPC status and control calls remain responsive. Pause enters
`PAUSING` and takes effect after the active isolated job reaches a terminal
boundary; resume reuses successful immutable results; cancellation prevents new
jobs and records every queued or active job's terminal disposition. A graceful
worker shutdown leaves an active job in `RUNNING` only after terminating its
isolated process group/tree. Once every worker thread has joined, SQLite records
a one-use shutdown receipt binding the project path, analysis, exact job and
attempt, pre/post states, engine and workflow hashes, PID/process-group identity,
and confirmed tree-exit method. A replacement worker atomically consumes that
receipt while requeueing exactly the covered rows. A hard crash, missing receipt,
identity mismatch, unconfirmed tree exit, or already consumed generation instead
returns `ANALYSIS_ORPHAN_PROCESS_AUDIT_REQUIRED` without changing scientific
state. Earlier attempt directories remain retained for diagnostics.

The currently releasable proof is POSIX whole-process-group absence. The group
is checked after a natural exit and after bounded `SIGTERM`/`SIGKILL`
escalation; observing only the direct child, checking one PID, or successfully
sending a signal is not sufficient. Windows has no release-eligible equivalent
in this revision. A Windows launcher must place the solver in a native Job
Object at process creation and query active-process zero before terminal state,
shutdown receipt, recovery, packaging, or publication can proceed.

Source rasters are resolved from the project or imported bundle by reviewed
relative path, byte count, and SHA-256. The execution boundary accepts
normalized ESRI ASCII and the target-specific pinned Rasterio GeoTIFF adapter.
It never reprojects, resamples, fills NODATA, or changes alignment. Reviewed
scenario effects are applied as absolute cell replacements and materialized as
new immutable ESRI ASCII inputs.

## Engine boundary

```text
prepare(portfolio, scenario, revision)
    → immutable prepared-run manifest
execute(prepared-run, isolated directory)
    → process disposition + raw artifacts
extract(raw artifacts)
    → focal-pair metrics + diagnostic map summaries + QA
```

The executor invokes a verified resource path directly, never through a shell.
The engine manifest binds Julia, `Project.toml`, `Manifest.toml`, native
artifacts, wrapper source, precision, neighborhood rule, averaging rule, thread
settings, parser, and metric definitions.

Release-evidence packaging accepts only jobs carrying the exact process-tree
proof above. The clean protected Ridgeway producer reruns all jobs on the same
commit and publishes an independently resolvable artifact. Pages and signed
release consume only that exact protected run/attempt/artifact provenance;
neither the local package nor a value-equivalent archive is a substitute.

The Ridgeway overview does not extract its 14,000-plus retained evidence files
before rendering. Worker startup verifies the signed projection, manifest,
archive identity, internal index, and member digests; the verified projection
can then open immediately. Full evidence materialization is deferred until a
claim is opened in the Evidence Rail, where the retained artifacts are actually
needed. This changes only I/O timing, never a displayed value or evidence
identity.

## Shared-platform boundary

Only domain-neutral behavior belongs in `scenario_platform` or
`scenario-host`: identifiers, hashes, typed errors, secure archives, CAS,
atomic writes, direct-job state, categorical exploration, JSON-RPC framing,
outbox mechanics, and parameterized observability names. SWMM and connectivity
contracts, mutations, QA, costs, decisions, fixtures, and UI vocabulary remain
in their domain packages.
