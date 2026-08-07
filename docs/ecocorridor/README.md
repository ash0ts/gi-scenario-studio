# EcoCorridor Scenario Studio

EcoCorridor Scenario Studio helps a DOT environmental team, wildlife agency,
or ecological consultant compare a bounded catalog of reviewed connectivity
interventions. Every reportable connectivity value must resolve to a direct,
pinned Circuitscape raster-pairwise execution.

> Direct Circuitscape results describe modeled electrical connectivity under
> documented resistance, focal-area, boundary, and intervention assumptions.
> They do not predict animal movement, crossing use, collision reduction,
> population viability, biodiversity benefit, regulatory acceptance, or a
> design-ready crossing.

The professional question is deliberately narrow:

> Which reviewed intervention portfolios remain acceptable across the required
> target and safeguard scenarios, and which should advance to ecological,
> field, and engineering feasibility review?

## Applications

- `apps/ecocorridor-desktop` is the local-authority professional desktop UI.
- `apps/ecocorridor-replay` is a no-login, synthetic-only public replay.
- `python/ecocorridor_core` owns ecological contracts, validation, direct-run
  preparation/extraction, decision rules, evidence, and exports.
- `python/scenario_platform` owns domain-neutral scheduling, storage, RPC, and
  observability primitives.
- `julia/EcoCorridorEngine` is the pinned Circuitscape process boundary.

The GI application remains independent in `apps/desktop` and
`python/gi_scenario_core`. Run `scripts/check_gi_regression_lock.py` before
reviewing any shared-platform change.

## Professional workflow

```text
Connect W&B → Open model → Decision brief → Model Fitness Passport
→ Ecological scenarios → Reviewed intervention packages
→ Safeguards and feasibility → Costs and rules → Run plan
→ Direct runs → Robustness Board → Evidence package
```

Portfolio comparison is withheld until every required target and safeguard
scenario succeeds with accepted numerical QA. Costs can rerank existing direct
results but never change the model-run cache identity. A professional CostBasis
is one confirmed semantic record: it names the source, currency, price vintage,
analysis horizon, real discount rate, CAPEX basis, O&M assumptions, replacement
assumptions, reviewer, review time, and exact cost of every retained option.
Missing or unconfirmed cost evidence blocks both the run plan and professional
export.

## Status

This is a source implementation with a checksum-pinned historical archive and
a separate, locally verified current direct-evidence v1.2 synthetic package—not
a published practitioner release. The current package drives a manifest-bound
v1.2 replay, but it is not a protected same-commit producer attestation. The
evidence-by-evidence status is in
[implementation-status.md](implementation-status.md). A distributable
professional release remains fail-closed until the executable and external
gates in [release-gates.md](release-gates.md) pass. In particular, no code path
treats W&B `PRIVATE` access as proof of `Restricted` visibility, and no
installer is called signed or notarized without real signing evidence. The
retained search campaign is historical and production-prohibited, and Windows
release-evidence generation remains blocked until a native Job Object proves
whole-tree exit.

- [Architecture](architecture.md)
- [Implementation status](implementation-status.md)
- [Scientific and claim contract](scientific-contract.md)
- [Data governance](data-governance.md)
- [Ridgeway guided example](guided-ridgeway.md)
- [Resource and energy observation method](resource-observability.md)
- [W&B historical search benchmark report](wandb-search-report.md)
- [Native Weave Review Brief Agent](review-brief-agent.md)
- [Review Brief agent evaluation and Report append](review-brief-agent-evaluation.md)
- [Usefulness evidence program](usefulness-evidence-program.md)
- [W&B usefulness narrative checkpoint](usefulness-narrative-checkpoint.md)
- [Human-readable, reader-first W&B article](human-readable-wandb-report.md)
- [Fair agent-complexity campaign](agent-complexity-campaign.md)
- [Usefulness study audit contracts](usefulness-study-audit-contract.md)
- [Usefulness protocol publication in W&B](usefulness-wandb-publication.md)
- [Usefulness animation render gate](usefulness-animation-storyboard.md)
- [Release gates](release-gates.md)
- [Practitioner acceptance protocol](practitioner-acceptance.md)
- [Threat model](threat-model.md)

## Run the source implementation

Use the EcoCorridor worktree, not the GI application worktree. The source tree
keeps Python, Node, Rust, Julia, generated contracts, caches, and app data
separate from GI Scenario Studio.

Prerequisites for the source checks are Python 3.12, Node.js 22, pnpm 11.9.0,
and `uv`. Rust 1.88.0 and Julia 1.12.6 are needed only for the native-host and
direct-engine boundaries.

```bash
uv sync --locked \
  --extra dev --extra science --extra ecocorridor \
  --extra ecocorridor-agents --extra ecocorridor-report \
  --extra export --extra observability --extra release
uv run --no-sync python scripts/install_ecocorridor_geospatial_runtime.py
pnpm install --frozen-lockfile

uv run --no-sync pytest tests/python
pnpm test:eco
pnpm build:eco
pnpm --dir apps/ecocorridor-desktop test:e2e
```

Run the professional desktop frontend in development mode with:

```bash
pnpm dev:eco
```

Run the synthetic, no-login public replay with:

```bash
pnpm dev:eco-replay
```

Browser development mode cannot open professional projects, use a keychain,
or establish a W&B binding. Those operations fail closed unless the verified
Tauri host and signed scientific resources are present. The public replay makes
no network call and contains only the CC0, direct-evidence-backed Ridgeway
projection. Its deployment workflow remains blocked until the independent
protected producer reruns the complete study on the exact deployment commit.

## Credentials and cloud verification

Never place credentials in repository files or renderer configuration.
Development and production credentials enter through the native keychain
boundary; protected CI uses GitHub Environment secrets. `.env.example` lists
variable names only. W&B synchronization remains unavailable until a supported
provider endpoint proves the exact `Restricted` project scope and every asset
has an allowed representation for that destination.

## Release-oriented checks

The executable source gates can be run independently before opening a pull
request:

```bash
uv run --no-sync python scripts/check_gi_regression_lock.py
uv run --no-sync python scripts/check_ecological_data_manifest.py
uv run --no-sync python scripts/check_repository_secrets.py
uv run --no-sync python scripts/check_workflows.py
uv run --no-sync python scripts/generate_ecocorridor_schemas.py --check
```

Target-specific engine, worker, installer, signing, notarization, protected
W&B/Weave, Pages, and practitioner-acceptance evidence is produced only by the
corresponding guarded workflows. Local ephemeral signatures are smoke-test
evidence, never release signatures.

Protected cross-workflow artifacts also carry a first-party producer identity.
The release verifies its run ID and attempt, workflow path/ref and workflow SHA,
and head SHA against independently resolved GitHub REST provenance before using
the artifact's evidence. Pages applies the same rule to the protected Ridgeway
producer. Local package verification is necessary but never replaces that exact
protected run and immutable artifact provenance.
