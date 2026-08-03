# Scientific worker sidecar

Release builds place a standalone Python worker executable here using Tauri's
target-triple naming convention:

```text
gi-scenario-worker-aarch64-apple-darwin
gi-scenario-worker-x86_64-apple-darwin
gi-scenario-worker-x86_64-unknown-linux-gnu
gi-scenario-worker-x86_64-pc-windows-msvc.exe
```

The source executable name in `tauri.conf.json` deliberately omits the target
triple. Packaging automation must build the worker in a pinned Python
environment, copy it to the matching path, and record its SHA-256 and SBOM.

The worker contract is strict JSON-RPC 2.0 over newline-delimited UTF-8 stdio.
Stdout must contain protocol messages only. Human-readable logs go to stderr;
the Rust host does not forward stderr to the webview because it may contain
project paths or model content.
