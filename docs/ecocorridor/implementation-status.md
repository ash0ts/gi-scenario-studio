# Implementation status

This document separates what is present in source, what has immutable local
evidence, and what still requires an external organization or credential. It
must not be used as a release announcement.

## Provenance and isolation

- EcoCorridor was branched from immutable GI squash-merge commit
  `28d8631986e07fc53872706dd12b7dcdb55359aa` in its own linked worktree.
- The Eco application uses its own desktop identifier, keychain service,
  application-data root, W&B namespace, contracts, worker methods, and release
  resources.
- GI regression locks are part of the Eco CI design. A green draft-PR run is
  still required before this branch can be described as CI-verified.

## Evidence already present

### Historical Ridgeway direct study

The earlier synthetic Ridgeway v1.0 evidence is retained and checksum-pinned:

- 128 portfolios × 12 scenarios = 1,536 direct raster-pairwise matrix jobs;
- baseline plus four direct-run teaching portfolios × 12 scenarios = 60 direct
  current-map jobs;
- retained archive SHA-256:
  `4858c0a48841e13aa395c007c72474bc1345a9de48495e77e70dc38dff512842`.

The repository gate verifies the archive bytes, members, safe paths, internal
index, and explicit `SUPERSEDED_NON_RELEASE` disposition. The archive does not
match the current model identity or v1.2 direct-evidence contract, cannot
authenticate the active replay, and remains historical benchmark input only.
Its earlier teaching outcomes are not promoted into the current projection.

### Current local Ridgeway reference package

The active synthetic replay is now backed by a locally verified current
package rather than an `ENGINE_RUN_REQUIRED` placeholder:

- 128 fixed portfolios × 12 scenarios = 1,536 accepted direct matrix jobs;
- baseline plus four evidence-selected teaching portfolios × 12 scenarios =
  60 accepted direct current-map jobs;
- 506 prior completion records were reused only after their immutable requests,
  results, artifacts, QA, and hashes were revalidated; the finished package
  still contains direct evidence and confirmed whole-process-tree exit records
  for all 1,596 jobs;
- current archive SHA-256:
  `a0dc6b7633475824d36cd9ab18ee38088208561ba680fed022fc7b6d65ac7168`;
- direct-evidence manifest SHA-256:
  `abf53dd7fa03956e8d076286e30d1ef3e5b35abf6a06952390b5366fdfef3c1e`;
- engine manifest SHA-256:
  `4fdf058be0bb2e33bc014858e01decb808ecc7296e1fca78fce58f62331a7a86`;
- workflow manifest SHA-256:
  `a12e7c05308f211a3e58969e476162dd902b9307ddbd210cebe673815dc82f6f`;
- evidence-index SHA-256:
  `ecb2c163b880d048372fd60a5e5e33b1a0d04295d22c4de2514e089c57c3f3ab`.

The package reconciles all 120 teaching map-run pair values to their study
matrix values with zero observed difference under the declared tolerances,
and retains 60 current rasters. This establishes checksum-pinned local
semantic reference evidence only. It is not an authenticated, clean,
same-commit protected producer execution. Pages, signed installers, and the
practitioner release remain blocked until that independent producer and the
other external release gates pass.

### Ecological release-asset inventory

`benchmarks/ecocorridor/data-manifest.json` classifies every current Ridgeway,
trust-policy, screenshot, and search asset. Every record pins bytes and SHA-256
and explicitly states CC0
ownership, self-authored synthetic provenance, `PUBLIC` sensitivity, absence
of real locations and species occurrences, and permitted repository use.

The checker treats every non-control file below `benchmarks/ecocorridor/`, every
desktop screenshot baseline, the public fixture, and any tracked spatial file
anywhere in the repository as requiring classification. New extensions do not
silently bypass the benchmark or screenshot boundaries.

### Computational resource observations

Executed direct jobs now retain monotonic process time, sampled process-memory
lower bounds, generalized hardware, and an explicitly estimated active-power
energy value with committed 5/15/40 W scenario coefficients. Search plans use
the same bounded observation, while release benchmark execution writes a
separately digested sidecar bound to the deterministic campaign SHA-256.
These records are not meter readings, carbon estimates, or portfolio evidence;
their exact boundary and exclusions are documented in
`resource-observability.md`. The committed search sidecar is immutable evidence
for the historical campaign, not evidence produced by the current source tree.
Its declared generator hashes resolve to passive, byte-identical `.py.txt`
snapshots under an explicit `HISTORICAL_BENCHMARK_ONLY` provenance record. That
record is a digest-matched source recovery—not an authenticated execution
attestation—and sets production authorization to `PROHIBITED`. Ridgeway direct
evidence has now been regenerated into the current local reference package,
but that package does not retroactively make the historical search campaign
current or production-eligible. The benchmark derivation boundary now consumes
the current v1.1 evidence index only after checking the complete 1,596-job
study, its bound scientific/runtime identities, and confirmed process exits;
it emits separately versioned v2 tables. A new campaign still must be executed
from those tables under its own versioned provenance and immutable W&B lineage.

### Search release decision

The committed four-landscape, ten-seed campaign did not pass the adaptive-ML
gate. Its campaign digest is
`d43c377f886d753105f5fd8bf47e6763ddaa40763537740d1136b702cd39b509`.
It is also derived from superseded evidence and is explicitly
`HISTORICAL_BENCHMARK_ONLY`, with production authorization `PROHIBITED`. The
source release therefore keeps scrambled Sobol as the production search;
ExtraTrees remains experimental and cannot be enabled by a checkbox, current
source drift, a rewritten sidecar, or an unpinned artifact alias. A future
production evaluation must be a separate versioned campaign derived from
current direct evidence.

## Implemented source surfaces awaiting full release evidence

- Versioned Eco contracts and runtime-validated JSON-RPC methods.
- Eco SQLite authority, semantic revisions, durable direct-job state,
  content-addressed artifacts, evidence graph, and producer-atomic telemetry
  outbox.
- Strict raster-pairwise resistance preparation, isolated Circuitscape process
  protocol, direct metric extraction, QA, and evidence resolution.
- Reviewed planning/import authoring surfaces, explicit grid-alignment
  approval, scenario-specific intervention effects, safeguards, decision
  rules, and a repository-backed CostBasis workflow. Professional cost
  evidence now requires exact option coverage, lifecycle assumptions, named
  review, and explicit confirmation in one semantic revision; export
  eligibility and package creation remain evidence-derived and fail closed.
- Imported reference matrices can now be bound to exactly one ecological
  scenario, declared effective-resistance tolerances, reviewed basis, reviewer,
  and limitations. The pure comparison boundary reparses both retained matrices
  with the strict Circuitscape parser, binds each direct-evidence claim to its
  exact comparison, scenario, focal pair, and direct-run identity, and derives
  pass/fail from the documented absolute-plus-relative tolerance formula.
  Optional reference current rasters must match the full reviewed CRS, extent,
  dimensions, affine cell alignment, and cell area; they remain diagnostic-only
  and are never compared numerically. Authored reference-basis text passes
  through the same prohibited ecological-claim gate as other project prose.
  Professional validation still blocks on `REFERENCE_COMPARISON_REQUIRED`
  until an authoritative, revision-bound repository result is persisted and
  consumed; neither asset presence nor a binding can imply a pass or attest
  model fitness.
- Worker-backed Ridgeway replay and durable local rerun controls, plus a
  synthetic-only no-login web replay.
- Distinct accessible desktop UI, responsive screenshot baselines, and local
  Evidence Rail.
- Parameterized W&B/Weave adapters, sensitive-data authorization, separate
  native credential boundaries, and deterministic assistant response checks.
- W&B delivery now treats a deterministic immutable telemetry-projection
  Artifact—not mutable Run summary markers—as cloud completion authority. A
  retry validates and re-downloads that exact canonical projection plus every
  retained output Artifact before local rows can become synced. History,
  Tables, input edges, and Registry links are explicitly reconstructable views
  of that payload. Direct terminal rows are mirrored in deterministic batches
  of at most 128 rows or 512 MiB and acknowledged atomically; the 1,596-job
  Ridgeway scale gate requires exactly 13 envelopes. `CONNECTED` is impossible
  while any authoritative outbox row remains pending, uploading, failed, or
  blocked. Explicit retry revalidates scope and asset policy before creating an
  immutable blocked-row requeue audit, and professional packages are stamped
  queued until their own atomic `evidence_export` envelope is confirmed.
- Ecological validation now requires exact grid identity and geometry across
  scenario assets, focal assets, resistance hypotheses, action effects, and
  applicable review masks. A changed geometry cannot reuse a grid identity,
  and an approved alignment plan creates a semantic revision rather than a
  hidden transformation.
- Portable bundles reject duplicate scientific stable IDs before preflight or
  execution can resolve them differently. The same per-array identity rule is
  emitted in public JSON Schema and generated Zod validators, while the worker
  remains the authoritative import boundary.
- Circuitscape startup and packaging now require the exact package UUID,
  version `5.17.1`, and source-tree hash recorded in the committed Julia
  manifest, reject path/repository-tracked substitutions, and verify the loaded
  runtime package only after the manifest provenance checks pass.
- The GI regression boundary now verifies the complete pnpm dependency closure
  reachable from the preserved GI importer, including package metadata,
  dependency edges, approved optional-peer context rewrites, root pnpm policy,
  workspace configuration, and absence of repository `.npmrc` overrides.
- Target-specific worker/Julia staging, resource hashing, installer workflows,
  source/target-bound detached resource signatures, native GDAL/PROJ/GEOS
  inventory, SBOM generation, and installed-smoke entry points. The packaged
  worker smoke boundary now exact-matches the locked geospatial, Pydantic,
  SciPy, scikit-learn, W&B, and Weave versions and exercises credential-free,
  offline Pydantic contracts, scrambled Sobol, ExtraTrees, W&B Table/Artifact,
  and Weave trace/redaction capabilities in isolated temporary storage.

These are source capabilities, not a claim that the complete practitioner
workflow has passed acceptance. They do not establish green remote CI, a
successful protected cloud integration, signed installers, deployment, or
practitioner usability on their own.

The clean 1,596-job release-evidence producer is explicitly POSIX-only in this
revision. Every accepted job persists a unique PID/process-group execution
identity and a confirmed-absent natural-exit record for the whole isolated
process group. Direct-child exit, a PID lookup, or merely sending a termination
signal is insufficient; descendants could still be writing evidence. The
independent packager and guided verifier reject missing, copied, mismatched, or
unconfirmed records. Windows release-evidence production remains blocked until
the launcher owns a native Job Object from process creation and verifies its
active-process count reaches zero for natural exit and termination paths. A
`taskkill` return code or direct-child wait is not equivalent. This does not
silently downgrade the evidence contract.

### Local packaged-boundary performance observation

An unsigned arm64 smoke bundle was measured on local APFS with the real Julia
1.12.6/Circuitscape 5.17.1 application. Complete verification of the signed-test
resource inventory took 5.96 seconds warm (8.36 seconds on an earlier pass), a
fresh worker ping took 3.06 seconds, and the hand-calculated direct run took
3.24 seconds. Ridgeway overview opening now defers full evidence extraction;
opening an individual claim remains the point where retained artifact bytes are
materialized and verified. These local measurements are diagnostic only. Each
signed target still requires installed-application timing and a clear, truthful
startup state before release.

## Release blockers that remain

1. **Julia runtime dependency vulnerabilities.** Target-only scans with the
   exact CI scanner (Trivy 0.70.0) and an independent local Trivy 0.72.0,
   using vulnerability data updated 2026-07-28, each found the same 49 records
   in the committed `julia/EcoCorridorEngine/Manifest.toml`: 2 critical,
   18 high, 22 medium, and 7 low. An end-to-end run of the exact CI scanner
   against a newly downloaded database updated 2026-08-03 found 57 records:
   2 critical, 24 high, 24 medium, and 7 low. PR #13 then refreshed the database
   at 2026-08-04T01:17:42Z and found 73 records: 3 critical, 32 high, 29 medium,
   and 9 low. The current critical records affect `LibSSH2_jll 1.11.3+1`
   (`CVE-2026-55200`) and `OpenSSL_jll 3.5.4+0` (`CVE-2026-31789` and
   `CVE-2026-34182`). A genuine
   Julia 1.12.6 `Pkg.update()` against the available General registry, performed
   only on a temporary copy, changed no packages; several affected JLL updates
   are constrained by the current Julia/GDAL dependency graph. The committed
   Project or Manifest was not hand-edited. PR CI now scans only this Julia
   environment with an immutable Trivy Action commit and exact scanner version,
   refreshes the vulnerability database without cache, retains scanner/database
   metadata, rejects database data older than 48 hours, and fails on every
   severity or invalid report. The protected release prerequisites repeat the
   same fresh-database scan so post-merge disclosures cannot bypass the gate.
   Because live vulnerability intelligence can add or revise records, 49, 57,
   and 73 are dated observations rather than hard-coded expected CI counts.
   Release requires an upstream-compatible dependency/runtime resolution
   followed by complete engine, fixture, package, and evidence regeneration;
   no waiver is present.
2. **Python release-signing security migration verification.** Source signing
   and verification now use pinned PyNaCl 1.6.2, while preserving the native
   Rust boundary's raw 32-byte public key and raw 64-byte detached-signature
   formats. The unused Presidio anonymizer is removed; Presidio Analyzer and
   the existing deterministic redaction boundary remain. Required Weave still
   resolves `polyfile-weave → pdfminer-six → cryptography 48.0.1`, however, so
   this change does not claim that the vulnerable transitive package vanished.
   Forcing `cryptography 50` does not satisfy the audited macOS x86_64 wheel
   target, and removing Weave would weaken the traced-assistant contract. The
   unwaived dependency scan therefore remains a release blocker. The release
   script accepts only a canonical raw 32-byte Ed25519 seed encoded as 64
   lowercase hexadecimal characters plus one line feed. Complete same-commit
   dependency scanning, release-signing tests, target packaging, and
   installed-application verification remain required; this source change is
   not itself protected release evidence.
3. **Protected Ridgeway producer execution.** The source now includes a manual,
   environment-protected full-study workflow that requires green same-commit
   Eco CI, starts from an empty evidence directory, executes all 1,596 direct
   Circuitscape jobs, and compares every pair metric and retained current-raster
   cell against the committed package. No successful immutable producer
   artifact exists yet. Local semantic packaging is not a substitute for this
   authenticated execution record; Pages and signed release require its exact
   same-commit run ID and immutable artifact identity. Neither consumer may use
   a locally installed package, a source-branch artifact, or a semantically
   equivalent archive in place of the independently resolved protected
   producer provenance.
4. **Authoritative W&B scope and protected integration.** W&B's supported
   multi-tenant Public API still does not expose an authoritative exact
   `Restricted` scope. Professional synchronization remains blocked until a
   documented resolver is available and an explicitly invited service account
   passes the protected hash-roundtrip workflow against that destination.
5. **Provider-backed Weave evaluation.** A separately supplied model-provider
   credential, real flushed/read-back Weave traces, the pinned evaluation
   dataset, deterministic scorer results, and practitioner usefulness ratings
   are still required. Evaluation is deliberately two-stage:
   `--evaluation-only` publishes immutable case records explicitly marked
   `release_eligible=false`, then the validator binds signed feedback to those
   exact case/answer hashes, provider calls/models, Weave traces, W&B run, and
   evaluation call without rerunning the provider. Missing, blank, fallback,
   duplicate, or different-model provider identities fail closed. Rejected
   adversarial cases require typed, persisted abort readback with an exact
   ordered trace-step prefix.
6. **Release signing.** Apple signing/notarization and Windows code-signing
   credentials must produce real signed installers and signed update metadata.
7. **Public deployment.** GitHub Pages must be enabled and the synthetic-only
   build deployed from the manifest-approved artifact.
8. **Immutable release policy.** GitHub immutable releases, an active no-bypass
   all-tag update/deletion ruleset, its protected ruleset ID, and an admin-read
   policy token must be configured before the verified draft can publish.
9. **Practitioner acceptance.** Six representative DOT/wildlife practitioners
   must complete the declared uncoached protocol, with at least five passing
   every comprehension task.

The pushed draft PR still needs every actionable executable failure repaired.
Its latest GitHub Actions checks have not started jobs because the repository
account is blocked by a billing or Actions spending-limit condition. That
infrastructure failure is not code execution and is not green CI. After the
account condition is resolved, the unwaived upstream dependency findings above
are still expected to block the full release. None of
the blockers above may be replaced with fixtures, self-attestation, a
manual checkbox, or undocumented provider behavior.
