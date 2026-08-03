# GI Scenario Studio

Evidence-first green-infrastructure alternatives screening with EPA SWMM.

GI Scenario Studio is a local-first React/Tauri workspace for environmental,
stormwater, and water-resources engineers. It is designed to compare approved
green-infrastructure (GI) opportunities across multiple storm cases while
keeping the hydraulic model, assumptions, QA findings, and evidence trail
inspectable.

The central engineering invariant is strict:

> Every reported hydraulic value comes from a pinned SWMM execution. Machine
> learning may choose which exact candidate to simulate next; it never supplies
> a reportable hydraulic result.

> **Status: source MVP.** The exact guided example is operational and fully
> inspectable in the browser. Existing-model and conceptual-model workflows are
> visibly labelled setup previews until an authoritative desktop worker is
> connected. This repository does not contain a compiled or signed installer.

## At a glance

| Area | Current state |
|---|---|
| Primary use | Transparent GI alternatives screening with explicit model-readiness gates |
| Verified teaching path | 884 exact EPA SWMM 5.2.4 runs, all accepted by the fixture QA policy |
| Interface | React 19, TypeScript, Vite, and a persistent Evidence Rail |
| Desktop boundary | Tauri 2 host supervising a Python scientific worker over versioned JSON-RPC |
| Engineering core | Pydantic contracts, SQLite state, SWMM parsing/mutation, exact-run scheduling, search, and evidence export |
| Distribution | Source checkout only; release packaging and signing remain incomplete |
| License | Apache-2.0 application code; component and dataset terms remain attached to their manifests |

## Who it is for

GI Scenario Studio supports three starting points:

1. **Open an existing SWMM project** to inspect dependencies, reproduce a
   baseline, configure engineer-approved GI opportunities, and compare exact
   alternatives.
2. **Build a conceptual model** from user-provided or approved open geospatial
   data, with every derived or defaulted value retained in an assumption ledger.
3. **Run the guided example** to learn the evidence workflow without network
   access or client data.

The intended output is a reviewable alternatives-screening record: model and
storm provenance, exact solver runs, QA findings, semantic model changes, costs,
and limitations. It is not an automatic design, permitting, or compliance
decision.

## What works today

| Area | Included | Not yet claimed |
|---|---|---|
| Guided path | Executed 884-job exact fixture, accessible chart/table views, evidence hashes, retained artifacts, and an honest learning-summary download | A calibrated professional model, adopted storms, or full-network adverse-change claims for runs whose detailed artifacts were not retained |
| Scientific core | Versioned contracts, secure bundle inspection, line-preserving SWMM parsing, typed mutations, readiness gates, SQLite state machines, isolated solver execution, enumeration/QMC planning, optional ExtraTrees proposals, and repository-bound exports | A completed NSGA-II product loop or any prediction-derived hydraulic result or confidence claim |
| Evidence exports | Repository-verified JSON, CSV, Markdown, HTML, and selected INP/RPT/OUT files | Production DOCX/XLSX report templates |
| Concept starter | Deterministic local generator, per-object assumption provenance, validation, and an optional SWMManywhere adapter | A packaged live-provider workflow or inference of a real buried drainage network |
| Desktop host | Tauri 2 source, a narrow command allowlist, JSONL process supervision, CSP, and scoped capabilities | Compiled, signed, or generally installable desktop artifacts |

The browser never substitutes placeholder hydraulic numbers when an
authoritative worker or solver is unavailable. Professional hydraulic claims
require the packaged worker and a hash-pinned SWMM 5.2.4 executable.

## Exact guided evidence

[`benchmarks/guided_exact/manifest.json`](benchmarks/guided_exact/manifest.json)
records 884 exact EPA SWMM 5.2.4 jobs:

- 4 baseline jobs, one for each synthetic storm;
- 220 unique integer LID vectors evaluated across all 4 storms (880 jobs);
- 884 exact runs accepted under the documented runoff and flow-continuity
  policy; and
- 36 complete retained run sets: all 4 baselines plus 8 alternatives across all
  4 storms.

Each run records the input, report, output, and solver hashes. The manifest also
pins the metric-extractor version, source-model hash, deterministic candidate
order, seed, storm definitions, and retention policy.

This fixture is `DEMO`, not reference matched. Its model, storms, opportunities,
and costs are self-authored teaching data. It makes no calibration,
recurrence-interval, global-optimality, regulatory, or real-world performance
claim.

To regenerate the evidence, use an official SWMM 5.2.4 console build and a new
or empty output directory:

```bash
python scripts/generate_guided_fixture.py \
  --solver /path/to/runswmm \
  --output /new/empty/output-directory
python scripts/generate_guided_ui_fixture.py \
  --manifest /new/empty/output-directory/manifest.json
```

The pinned source revision and the binary hash used for the checked-in fixture
are recorded in [`engine/swmm-5.2.4.json`](engine/swmm-5.2.4.json). The binary
itself is not treated as a portable release artifact.

## Quick start: inspect the guided application

Requirements:

- Node.js 22 (the CI reference runtime)
- pnpm 11.9.0 (pinned in `package.json`)

From the repository root:

```bash
pnpm install --frozen-lockfile
pnpm dev
```

Open <http://localhost:1420>. The exact guided fixture works without a desktop
worker. Real-project entry paths remain locked setup previews in a browser-only
session.

Build or test the webview with:

```bash
pnpm test:frontend
pnpm build
```

## Scientific core

Python 3.11 or newer is supported. The base install stays small; scientific,
geospatial, and document-export dependencies are optional extras.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest tests/python
gi-scenario-worker --help
```

Install optional features only when needed:

```bash
python -m pip install -e '.[dev,science]'
python -m pip install -e '.[dev,geo]'
python -m pip install -e '.[dev,export]'
```

The worker accepts newline-delimited JSON-RPC 2.0 on standard input. This
read-only smoke test exercises the same worker boundary used by the desktop
host:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"system.ping","params":{}}' \
  | gi-scenario-worker --transport jsonl-stdio --api-version 1.0
```

The public contracts are generated from Pydantic models into versioned JSON
Schema plus frontend TypeScript/Zod types:

```bash
python scripts/generate_schemas.py
```

Generated outputs live in [`schemas/v1.0/`](schemas/v1.0/) and
[`apps/desktop/src/generated/contracts.ts`](apps/desktop/src/generated/contracts.ts).
CI fails if these outputs drift from the Python contract definitions.

## Desktop development

The native host additionally requires Rust 1.88 or newer and a target-specific,
self-contained worker at:

```text
apps/desktop/src-tauri/binaries/gi-scenario-worker-$TARGET_TRIPLE[.exe]
```

With that worker present:

```bash
pnpm desktop
```

The missing-worker failure is intentional: an installer without its scientific
worker is not a valid engineering application. A release must also bundle and
hash the permitted SWMM executable, create an SBOM, publish checksums, and sign
the worker and installer. See the
[`desktop packaging contract`](docs/platform-packaging.md).

## Architecture and trust boundary

```text
React webview
  -> versioned Tauri commands
    -> Rust host and JSONL framing
      -> Python scientific worker (sole SQLite writer)
        -> one isolated SWMM subprocess per design x storm
```

The renderer selects evidence by identity; it cannot provide hydraulic values,
hashes, readiness, project identity, or export attachments. The worker resolves
completed repository jobs, verifies artifact bytes against their SHA-256 values,
and rejects cross-project or cross-analysis selections.

Large immutable inputs and outputs are content-addressed. SQLite stores
authoritative project state, provenance, job transitions, metrics, QA findings,
and artifact references. Frontend state is disposable view state.

The main design records are:

- [`docs/platform-architecture.md`](docs/platform-architecture.md) — process,
  protocol, lifecycle, evidence, and security boundaries;
- [`docs/product-principles.md`](docs/product-principles.md) — product claims and
  engineering invariants;
- [`docs/data-governance.md`](docs/data-governance.md) — provenance, licensing,
  retention, and client-data handling;
- [`docs/research-basis.md`](docs/research-basis.md) — technical decisions and
  source basis; and
- [`docs/proposed-improvements.md`](docs/proposed-improvements.md) — researched
  product and staff-engineering plan beyond the source MVP.

## Model readiness

Every project has one visible readiness level:

| Level | Intended use |
|---|---|
| `DEMO` | Bundled teaching and interface-evaluation material |
| `CONCEPTUAL` | Exploratory comparison with unverified assumptions |
| `ENGINEER_REVIEWED` | Draft alternatives screening after explicit attestations |
| `REFERENCE_MATCHED` | Evidence package after an approved reference comparison passes stated tolerances |

Readiness is never inferred from a score or advanced automatically. Promotion
requires a recorded human decision; `REFERENCE_MATCHED` additionally requires a
successful metric-specific comparison. Conceptual outputs retain this notice:

> Conceptual planning model — not for design, permitting, flood-risk
> determination, or representation of an existing drainage system.

## Repository map

```text
apps/desktop/              React interface and Tauri host
assets/                    Source artwork for generated desktop icons
python/gi_scenario_core/   Scientific worker and engineering domain services
schemas/v1.0/              Generated public JSON Schemas
tests/python/              Core, cross-layer, solver, evidence, and export tests
benchmarks/                CC0 reference assets and exact guided evidence
engine/                    SWMM pin and packaging metadata
scripts/                   Schema and guided-fixture generators
docs/                      Architecture, governance, packaging, and research notes
.github/workflows/ci.yml   Required source verification workflow
```

## Verification and CI

Before proposing a change, run the relevant local checks from an activated
Python environment:

```bash
python -m pytest tests/python
python -m ruff check python tests scripts
python -m ruff format --check python tests scripts
pnpm test:frontend
pnpm build
```

The GitHub Actions workflow in [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
runs on pushes, pull requests, and merge queues. It verifies Python lint,
formatting and tests; generated-contract and guided-fixture consistency;
retained evidence hashes; frontend tests and production build; and Rust
formatting, strict Clippy, and host tests against lockfile-pinned dependencies.
The workflow uses read-only repository permissions and does not require client
models or external data-provider access.

## Security and provenance

This application processes untrusted project bundles and launches a hydraulic
solver, so security behavior is part of engineering correctness:

- archive paths, symlinks, expanded size, and file counts are validated before
  extraction;
- solver processes are invoked without a shell in isolated run directories;
- the desktop renderer has no arbitrary shell or broad filesystem capability;
- existing-model analysis requires no network access;
- optional data-provider access is allowlisted, consented to, and recorded; and
- diagnostic bundles exclude model content unless the user explicitly opts in.

Do not commit client models, credentials, exact asset coordinates, or permit
materials. Report sensitive defects privately as described in
[`SECURITY.md`](SECURITY.md). Dataset visibility is never treated as permission
to redistribute it; see [`benchmarks/README.md`](benchmarks/README.md) and the
asset manifests.

## Scope and limitations

GI Scenario Studio is an alternatives-screening application. It is not a
replacement for calibration, survey, geotechnical investigation, utility
locating, official SWMM editing, or professional judgment. The conceptual
generator exposes planning defaults and does not claim to infer a real buried
network.

Automatic calibration, advanced controls, treatment, water quality, real-time
control, detailed hydraulic structures, 2D flooding, regulatory determinations,
and construction design remain outside v0.1. SWMManywhere is isolated behind an
optional adapter and cannot bypass standard import, validation, provenance, or
readiness gates.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before changing engineering behavior.
Contributions must preserve exact-result provenance, visible conceptual-model
limitations, typed and reversible mutations, and the separation between ML
proposals and reportable results. Add a golden, integration, or property test
when changing parsers, mutations, cache keys, budget arithmetic, readiness, or
exports.

## License and notices

Application code is licensed under [Apache-2.0](LICENSE). EPA SWMM is public
domain. SWMManywhere is BSD-3-Clause and optional. Dataset and benchmark terms
remain attached to their individual manifests. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and [`NOTICE`](NOTICE).
