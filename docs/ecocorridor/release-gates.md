# Full-release gates

The repository distinguishes implemented source, locally verified behavior,
CI evidence, and external release evidence. A missing external prerequisite is
a blocker, not an invitation to weaken policy.

## Current disposition

**Full practitioner release: blocked.** A checksum-intact local Ridgeway
reference package is present, while the earlier v1.0 archive remains explicitly
historical. The current source-identity gate correctly reports `contracts`
drift after the decision-rule and synchronization-contract hardening in this
branch; a clean same-commit protected producer run and repack are required
before that reference can be promoted for this revision. The local package
cannot substitute for that execution attestation. The ecological
release-asset manifest is present, and the adaptive benchmark has correctly
selected Sobol because its historical ML gates failed. Draft PR #13 is pushed;
its actionable executable failures are being repaired while the live dependency
security gates correctly remain red. The protected cloud, provider-backed
assistant, signing, deployment, and practitioner gates below have not been
satisfied.

| Evidence area | Current state |
|---|---|
| Ridgeway 1,536 matrix + 60 map jobs | Prior-source local reference remains checksum-intact: 1,596 accepted jobs and archive `a0dc6b763347…`; current source-identity verification blocks on `contracts` drift pending a same-commit protected rerun and repack |
| Ridgeway execution provenance | Gate implemented, evidence blocked: the manual protected producer workflow must execute a clean same-commit 1,596-job study after green Eco CI, publish its immutable Actions artifact, and pass the complete metric/current-raster differential; no successful producer run exists yet |
| Ecological asset classification | Synthetic CC0 assets, including the manifest-bound local-reference trust record, are checksum- and relationship-checked; protected producer trust remains a separate unsatisfied gate |
| Display and redacted story evidence | Source gate implemented: Decision Board and Scenario Detail re-verify authoritative selected/baseline direct-run chains, Decision Board reducers and dispositions reconcile to those verified primitives, and the shared synthetic story carries claim/run/artifact/QA links while excluding raster cells and private data; protected producer provenance remains separately required |
| Adaptive search | Current v1.1 direct evidence can be fail-closed derived into separately versioned v2 tables, but no current campaign or immutable W&B pin exists; the historical campaign failed and remains `HISTORICAL_BENCHMARK_ONLY` / production `PROHIBITED`, so production remains scrambled Sobol |
| Julia dependency vulnerability policy | Blocked: the PR #13 scan with database timestamp 2026-08-04T01:17:42Z found 73 unwaived records (3 critical, 32 high, 29 medium, 9 low), including critical findings in `LibSSH2_jll` and `OpenSSL_jll`; the current Julia 1.12.6 dependency graph has not resolved them |
| Python release dependency policy | Release Ed25519 signing and verification now use pinned PyNaCl 1.6.2, and raw public/signature compatibility with the Rust host is regression-tested. The complete Presidio runtime (analyzer, anonymizer, and default NLP model) is not bundled, so EcoCorridor disables Weave's optional Presidio mode and uses a bounded deterministic offline redaction boundary instead. Release remains blocked because required Weave still resolves `polyfile-weave → pdfminer-six → cryptography 48.0.1`; the advisory remains visible to the unwaived dependency scan |
| Branch CI and target packaging | Source workflows exist; the latest GitHub Actions checks did not start jobs because of the repository account billing/spending-limit block, so no code executed and green draft-PR plus installed-target evidence are still required |
| W&B Restricted integration | Blocked pending a supported authoritative scope resolver and invited service account |
| Provider-backed Weave assistant evaluation | Native Agents adapter, bounded Review Brief tools, deterministic validators, 14-case × three-method campaign, immutable artifact verifier, claim-bound animation renderer, and append-only historical Report publisher are implemented; blocked pending approved provider/W&B configuration, a real protected campaign on `main`, exact Agents trace readback, Report publication, and practitioner usefulness evidence |
| Signed installers and update metadata | Blocked pending Apple and Windows signing channels |
| Windows Ridgeway evidence production | Blocked: the release-evidence runner requires confirmed-empty POSIX process groups; a Windows producer requires native Job Object ownership and active-process verification |
| Public replay deployment | Blocked pending GitHub Pages enablement and deployment |
| Immutable GitHub publication | Blocked pending repository release immutability, an active no-bypass all-tag update/deletion ruleset, and an admin-read policy token |
| Practitioner acceptance | Blocked pending six representative participants and a 5/6 pass result |

## Executable gates

- Complete GI regression lock and schema/fixture/result checks.
- Scenario-platform atomicity, crash recovery, idempotency, cache, and security
  tests.
- Eco contract, raster/grid, mutation, decision, evidence, export, and RPC
  tests.
- Adversarial rule-coverage tests proving that every target scenario and
  included focal pair is governed by a required target rule before comparison
  or professional export, plus retained-criterion UI tests for exact scope,
  stable identity, Undo, and edits made during an in-flight save.
- Julia/Circuitscape official fixtures plus hand-calculated series/parallel
  landscapes using the packaged resource.
- Worker-to-packaged-engine smoke tests on each target. The frozen worker must
  exact-match the locked professional runtime inventory and prove offline,
  credential-free Pydantic, Sobol, ExtraTrees, W&B Table/Artifact, and Weave
  tracing capabilities from isolated temporary directories. It must also
  perform the real local email, North American phone, U.S. Social Security
  number, sensitive-key, credential, bearer-token, and local-path
  transformation with DNS disabled; importing a callable redaction hook is not
  sufficient evidence. Configured sensitive-location terms and coordinates are
  covered by the separate EcoCorridor policy layer. This bounded control does
  not claim to infer every name, place, or other possible personal identifier.
- POSIX direct-evidence jobs must prove the complete isolated process group is
  absent after natural exit or bounded termination. A direct-child return code,
  PID-only check, sent signal, or unverified `taskkill` result cannot make a run
  reportable or authorize restart recovery. Windows release production remains
  blocked until a launcher-created native Job Object proves active-process zero
  across every exit path.
- Detached Ed25519 verification of the exact packaged resource tree before
  any smoke execution. The signed inventory binds the application version,
  exact source commit, and target; installed-app smoke repeats that check.
- Each installed native host is launched twice in a release-only health mode.
  Every launch starts the signed bundled worker, validates its authoritative
  ping and verified Ridgeway replay, and exercises the Eco keychain service
  with a fresh UUID account and random non-production value. The value is read
  back exactly, deleted, and confirmed absent; every failure path attempts
  cleanup and zeroizes in-memory values. Receipts contain only dispositions,
  never the temporary account or value.
- React type, unit, axe, keyboard, reduced-motion, compact, 200% zoom, and
  screenshot tests.
- Rust format, clippy, tests, path containment, keychain separation, resource
  hashing, and RPC allowlist tests.
- Actionlint, OSV, secret boundary, ecological-data manifest, license, and SBOM
  checks.
- A separate target-only Julia `Manifest.toml` scan using an immutable Trivy
  Action commit and exact scanner version. CI fetches current vulnerability
  data without cache, retains its database metadata and JSON report, rejects a
  database older than 48 hours or an unsupported/broadened scan, and fails on
  every reported severity. This job is intentionally red while the current
  73-record finding set remains;
  it is not waived to make the remaining executable checks appear green. The
  protected release prerequisites repeat the scan with a newly downloaded
  database rather than trusting an older PR result.
- W&B hash roundtrip/offline/expiry/quota checks and deterministic Weave
  evaluations in protected environments.
- Full-inventory oversize-plan tests covering deterministic byte accounting,
  plan staleness, immutable and restart-idempotent reviewer approval,
  producer-atomic approval/outbox writes, W&B audit projection, desktop
  acknowledgement, invariant pre/post approval digest and byte total through
  the bounded audit-projection reservation, per-envelope operational-metadata
  bounds, merged-batch framing, exact outbox-member authorization under a
  concurrent producer commit, strict worker/renderer receipt parity, and
  authoritative scope revalidation immediately before an online upload.
- Search evaluation across three fixed landscapes and ten seeds before an ML
  proposer can displace Sobol.

## External gates

- An upstream-compatible resolution of the remaining transitive
  `weave → polyfile-weave → pdfminer-six → cryptography` advisory. Removing
  Weave would violate the traced-assistant boundary, while forcing
  `cryptography 50` does not satisfy the audited macOS x86_64 wheel target.
  Neither dependency substitution nor a source build may silently remove that
  target. The locked dependency scan remains unwaived and fail closed.
- Complete PyNaCl signature, manifest, packaging, and installed-application
  verification. The protected secret
  `ECOCORRIDOR_RESOURCE_SIGNING_SEED_HEX` must contain exactly 64 lowercase
  hexadecimal characters encoding one 32-byte Ed25519 seed. The workflow
  materializes one trailing line feed without logging the value; PEM, base64,
  uppercase, expanded private-key, and whitespace variants fail closed.
- An upstream-compatible Julia/JLL dependency resolution, followed by a genuine
  package resolution and regeneration of the engine manifest, packaged engine,
  Ridgeway fixtures, direct evidence, hashes, and differential acceptance
  evidence. Hand-editing the Julia Manifest or suppressing findings is not a
  release path.
- A successful run of the protected
  `.github/workflows/ecocorridor-ridgeway-evidence.yml` workflow on the exact
  release commit. It first requires a green same-commit Eco CI run, then starts
  with an empty evidence directory, executes all 1,536 matrix and 60 map jobs,
  semantically verifies both packages, and compares every pair metric and every
  retained current-raster cell within the declared absolute/relative
  tolerances. The artifact embeds the independently resolvable run attempt,
  workflow ref/SHA, complete producer package, differential, and an attestation
  binding the committed and producer archive hashes. The local v1.2 packager proves
  artifact completeness, hashes, reviewed source identity, model semantics,
  focal-pair values, map/matrix coherence, and QA; by itself it is not a
  cryptographic attestation that a particular operating-system process emitted
  those bytes.
- The protected producer is POSIX-only until the Windows Job Object boundary is
  implemented and independently tested. Pages and signed release must resolve
  the exact successful protected run, attempt, workflow identity, head SHA,
  immutable artifact ID, and server-reported digest. A local package—even one
  whose values and hashes are internally consistent—cannot satisfy this gate.
- A documented authoritative W&B `Restricted` project-scope resolver.
- An explicitly invited service account and protected W&B environment.
- Separate provider credential and passing traced assistant evaluation.
- Apple signing/notarization and Windows code-signing credentials.
- GitHub Pages enablement for the manifest-approved public replay.
- GitHub immutable releases enabled, an active no-bypass tag ruleset covering
  all tags with update and deletion restrictions, its exact ruleset ID, and a
  protected admin-read token that can query both policies. Configure the
  protected Environment secret `ECOCORRIDOR_RELEASE_POLICY_TOKEN` and variable
  `ECOCORRIDOR_RELEASE_TAG_RULESET_ID`; the workflow never enables or edits
  repository policy itself. See GitHub's
  [immutable-release API](https://docs.github.com/en/rest/repos/repos#check-if-immutable-releases-are-enabled-for-a-repository)
  and [draft-first publication guidance](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository#creating-a-release).
- Six representative DOT/wildlife practitioners, with at least five completing
  the declared comprehension tasks without coaching under the
  [practitioner acceptance protocol](practitioner-acceptance.md).

## Release evidence

Each target release publishes signed installers/update metadata, SHA-256
checksums, CycloneDX SBOM, third-party notices, Julia/Circuitscape/GDAL
inventory, benchmark digests, W&B release-gate report, screenshots, offline
installed smoke-test results, and the redacted assistant evaluation records,
signed rating envelope, detached signature, public verification key, three
assistant-stage provenance records, and exact Eco CI, general CI, protected
Ridgeway producer, and Pages
provenance. Until those artifacts exist, documentation must say “source
implementation” rather than “signed practitioner release.”

The final release retains both complete Ridgeway packages: the committed
reference archive/projection/trust record and the independently executed
producer archive/projection/trust record. The protected differential and
attestation bind both manifest and archive hashes. The expiring Actions artifact
is therefore not the sole long-term copy needed to reproduce the comparison.

The protected release also requires a successful, manually dispatched
`EcoCorridor CI` run on `main` whose head SHA exactly matches the release
commit. Branch protection or a green run for another commit is not substituted
for this evidence. The release resolves the exact `.github/workflows/ecocorridor-ci.yml`
and `.github/workflows/ci.yml` REST records, including workflow ID and run
attempt; a matching display name is not evidence. Pages is manually dispatched
with the exact protected Ridgeway run ID, independently downloads that immutable
artifact, and rechecks the complete differential before building. It cannot
deploy on a source push alone. The Pages prerequisite is then bound to
`.github/workflows/ecocorridor-pages.yml` and release downloads only
the selected immutable artifact ID with a verified server-reported digest. The
Pages archive contains a producer identity created before upload; release
validation extracts it from the tar and matches its run attempt, workflow ref,
workflow SHA, and head SHA to the REST record. All
four normalized provenance records ship with the release. Each target publishes
its signed Python, GDAL, PROJ, GEOS, Julia, and Circuitscape inventory, and those
three inventories are inputs to the final CycloneDX SBOM.

The protected release workflow accepts practitioner acceptance as signed
external evidence. Assistant evidence comes from two ordered protected runs on
the exact release commit: stage one generates immutable provider/Weave case
records without ratings, and stage two resolves that exact successful Actions
run and binds later signed practitioner feedback without rerunning the provider.
The payloads enter stage two through protected Environment secrets; URLs and
credentials are never persistent workflow-dispatch inputs. Every boundary binds
the exact workflow path/ID, run attempt, artifact ID, and server-reported digest.
Because GitHub's artifact REST object does not itself expose run attempt, each
producer embeds a strict identity record before upload; consumers validate that
record after immutable-ID download. A stale artifact from an earlier rerun
attempt therefore cannot inherit the latest attempt's provenance.
Ratings supplied before the answers exist and a W&B/Weave transport probe are
never accepted as an assistant-quality evaluation. Published release tags and
signed assets are immutable rather than overwritten on a rerun. Publication
first verifies GitHub's authoritative immutable-release endpoint and an active
no-bypass all-tag update/deletion ruleset. It resolves an existing lightweight
tag or atomically creates one at the exact release commit, rejects annotated or
mismatched tags, and uses `--verify-tag`. Assets upload to a draft; their exact
names, byte sizes, and server-reported SHA-256 digests must match the local
manifest with no omissions or extras. The workflow rechecks policy and tag
identity immediately before publication, publishes the verified draft, then
requires the live release to report `immutable=true` and repeats the exact asset
and tag checks. `gh release --target` is not trusted to repair or override an
existing tag.

Provider-backed assistant evidence must include the pinned local and Weave
dataset identities, raw per-case records and Merkle root, evaluation URL and
call ID, 100% deterministic numeric/unit/limitation and adversarial-case gates,
byte-exact deterministic worker-render faithfulness, zero unauthorized actions
or sensitive-location leaks, confirmed trace
readback, and at least 80% positive usefulness over at least five signed,
redacted case ratings. Each rating row is bound to the exact case-input hash,
immutable case-record hash, answer hash, trace ID, provider-call ID, provider
model, W&B run, and Weave evaluation call. A rating collected for an earlier or
unrelated answer therefore cannot pass. The validator recomputes every
aggregate and verifies the detached Ed25519 feedback signature against the
protected approved key. The release contains the pending evaluation, final
report, raw cases, signed ratings, detached signature, raw Ed25519 public key,
exact stage-one and finalization Actions provenance, and an independent
stage-one recheck record so this gate can be reproduced. The stage-one,
stage-two, and Pages producer identity records are also published and covered by
release checksums. Release reruns the deterministic finalizer from the
independently downloaded stage-one files plus the signed ratings, requires a
byte-for-byte match with the stage-two report, and publishes that independently
re-finalized copy.

Rejected prompt-injection and fabricated-evidence cases do not receive trace
credit merely because an exception was raised. Their raw case record must
contain the aborted root call's provider-persisted trace ID, HTTPS trace URL,
error code, completed=false gate, ordered steps, W&B association, and an exact
post-flush readback. Missing or corrupt abort readback fails the evaluation.
Validate it with:

```bash
uv run --no-sync python scripts/validate_ecocorridor_assistant_evaluation.py \
  assistant-evaluation.json \
  assistant-evaluation-cases.json \
  --ratings assistant-ratings.json \
  --ratings-signature assistant-ratings.sig \
  --ratings-public-key-b64 "$APPROVED_RATINGS_PUBLIC_KEY_B64" \
  --expected-ratings-key-sha256 "$APPROVED_RATINGS_PUBLIC_KEY_SHA256" \
  --expected-commit "$RELEASE_COMMIT"
```
