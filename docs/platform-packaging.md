# Desktop packaging contract

## Worker artifact

Tauri expects a self-contained worker executable at:

```text
apps/desktop/src-tauri/binaries/gi-scenario-worker-$TARGET_TRIPLE[.exe]
```

The executable must accept:

```text
--transport jsonl-stdio --api-version 1.0
```

It must require no system Python and must write JSON-RPC messages only to
stdout. Package it from the locked scientific-worker environment using a pinned
PyInstaller (or equivalent) configuration. Do not dynamically download a Python
runtime or dependencies on first launch.

For local development, place a developer build at the same target-triple path.
This intentionally uses the release launch path as well, preventing a
development-only `python` fallback from hiding packaging defects or expanding
the executable allowlist.

## Build order

1. Resolve the Rust target triple with `rustc --print host-tuple`.
2. Build and smoke-test the Python worker in an offline environment.
3. Copy it to the target-triple path above and make it executable on Unix.
4. Record its SHA-256, Python lock digest, and component manifest.
5. Run Rust protocol tests and the desktop end-to-end test.
6. Build the Tauri installer.
7. Generate a CycloneDX SBOM covering Rust, JavaScript, Python, SWMM, and bundled
   data licenses.
8. Sign the worker and final installer in release CI. Signing credentials stay
   in the CI secret store and are not represented in repository configuration.

The Tauri bundle cannot be produced until the target-specific worker artifact
exists. That is an intentional failure: an installer without its scientific
worker is not a valid application.

## Release checks

- Launch and complete the guided example on a clean machine with network access
  disabled.
- Confirm no Python, Node.js, Rust, Docker, or administrator setup is required.
- Confirm frontend code cannot invoke the shell plugin directly.
- Kill the worker during an exact run, restart the app, and verify worker-side
  durable recovery without duplicate solver jobs.
- Verify checksums and signatures before publishing the installer.
- Exercise filenames containing spaces, Unicode, and long Windows paths.
- Create a diagnostics bundle and confirm model content is excluded unless the
  user explicitly selects it.
