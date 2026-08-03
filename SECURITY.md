# Security policy

GI Scenario Studio processes untrusted model bundles and invokes a hydraulic
solver. Security defects can be reported privately to the maintainers rather
than through a public issue containing a client model.

The application:

- extracts archives only after path, symlink, file-count, and expanded-size checks;
- invokes the pinned SWMM binary without a shell;
- uses isolated per-run directories and content-addressed immutable artifacts;
- performs no network access for existing-model analyses;
- requires explicit consent before a concept-model data provider is contacted;
- excludes model content from diagnostics unless the user opts in.

Do not include client models, credentials, exact asset coordinates, or permit
materials in a public vulnerability report.

## Dependency policy

Every pull request, merge-queue candidate, and push to `main` scans all locked
Python, JavaScript, and Rust dependencies with OSV-Scanner. New OSV findings
fail CI. Existing accepted findings must be listed by advisory ID in
[`osv-scanner.toml`](osv-scanner.toml), explain the dependency path and risk,
and include an expiry date; package-wide or permanent vulnerability suppressions
are not accepted.

The current `glib` exception is a temporary acceptance for Tauri's transitive
Linux GTK3 stack. It does not assert that the affected crate is safe, and it
expires with the maintenance-only gtk-rs, `proc-macro-error`, and `unic`
exceptions on 2026-11-01.

The same scan inventories all dependency licenses. CI rejects license
expressions containing AGPL or SSPL identifiers and reports unknown or
non-SPDX metadata for release SBOM and notice review. GitHub Dependency Review
is an optional additional gate when GitHub Advanced Security is enabled; the
OSV gate remains authoritative and does not depend on that repository feature.
