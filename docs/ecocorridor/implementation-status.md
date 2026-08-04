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
match the current model identity or v1.1 direct-evidence contract, cannot
authenticate the active replay, and remains historical benchmark input only.
Its earlier teaching outcomes are not promoted into the current projection.

### Current local Ridgeway reference package

The active synthetic replay is now backed by a locally verified current
package rather than an `ENGINE_RUN_REQUIRED` placeholder:

- 128 fixed portfolios × 12 scenarios = 1,536 accepted direct matrix jobs;
- baseline plus four evidence-selected teaching portfolios × 12 scenarios =
  60 accepted direct current-map jobs;
- 264 prior completion records were reused only after their immutable requests,
  results, artifacts, QA, and hashes were revalidated; the finished package
  still contains direct evidence and confirmed whole-process-tree exit records
  for all 1,596 jobs;
- current archive SHA-256:
  `f93ad36f3cdc36b702a3cb612cbd52c306f5b231f353982de0adaee6174e5e24`;
- direct-evidence manifest SHA-256:
  `433acb8e9b5c98e6b73f570891cd79a8b02c99221d4395d00fb1d2e1ab8d3afb`;
- engine manifest SHA-256:
  `5997a813487ea3ebbf9fac4b850d59f348fc0d9625ceebb3b92118f8c21afcdc`;
- workflow manifest SHA-256:
  `c875c9ac3bdfb1a7921843c1e0f926cacfc6979e964b2fd546fa6a62313eac6f`;
- evidence-index SHA-256:
  `b4e1c893cf7ed37adfa6de7be427cdd2793504358caaefbc83333b280144b28c`.

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
current or production-eligible. A new search campaign must be derived from the
current evidence under its own versioned provenance.

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
- Target-specific worker/Julia staging, resource hashing, installer workflows,
  source/target-bound detached resource signatures, native GDAL/PROJ/GEOS
  inventory, SBOM generation, and installed-smoke entry points.

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
2. **Python release-signing dependency vulnerability.** The 2026-08-04 PR scan
   found three newly published advisories in the pinned `cryptography 48.0.1`.
   Two are fixed by 49.0.0, but
   [GHSA-g6cj-pr64-35w5](https://github.com/advisories/GHSA-g6cj-pr64-35w5)
   affects every release from 44.0.0 through 49.x and names 50.0.0 as the first
   patched version. The latest release published on
   [PyPI](https://pypi.org/project/cryptography/) is 49.0.0. The application
   therefore retains the failing dependency gate until a patched upstream
   release exists and passes the full release-signing and manifest suite; it
   does not suppress the advisory or perform a still-vulnerable upgrade.
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

The pushed draft PR still needs every actionable executable failure repaired;
full CI remains legitimately blocked by the unwaived upstream dependency
findings above. None of
the blockers above may be replaced with fixtures, self-attestation, a
manual checkbox, or undocumented provider behavior.
