# Desktop platform architecture

## Responsibility boundary

The Tauri host is deliberately small. It owns the native application window,
the scientific-worker process, protocol framing, native dialogs, and shutdown.
It never interprets a SWMM model or computes an engineering result. The Python
worker is the sole authority for projects, SQLite state, model validation,
solver jobs, search, and evidence exports.

The renderer can call only four application-defined Rust commands:

- `worker_status`
- `worker_start`
- `worker_request`
- `worker_stop`

`worker_request` accepts one of the versioned domain methods in the Rust
allowlist. It does not expose an arbitrary process, filesystem, or shell API.
The renderer receives worker notifications on `worker://event` and lifecycle
updates on `worker://status`.

The renderer calls the worker through Tauri IPC, not a global JavaScript shim:

```ts
import { invoke } from "@tauri-apps/api/core";

const envelope = await invoke<JsonRpcResponse>("worker_request", {
  request: {
    method: "project.get_state",
    params: { project_directory: projectDirectory, project_id: projectId },
  },
});
```

Rust returns the worker's complete JSON-RPC response envelope. The renderer
unwraps `result` or presents the worker's structured `error`; it must not treat a
successful Tauri invocation as proof that the domain operation succeeded.

## Transport contract

The bundled `gi-scenario-worker` process is started once and communicates over
newline-delimited UTF-8 JSON-RPC 2.0 on stdin/stdout. Each line is at most 8 MiB.
Stdout is protocol-only; any non-JSON line is a protocol failure. Stderr is not
forwarded to the renderer because it can contain sensitive model paths or
content.

The host deliberately leaves plugin-shell raw output disabled. Under the
[official `CommandEvent` contract](https://docs.rs/tauri-plugin-shell/latest/tauri_plugin_shell/process/enum.CommandEvent.html),
each `Stdout` event contains bytes through one newline or carriage-return
boundary, with the delimiter removed. It is therefore one JSONL frame, not an
arbitrary byte chunk. Enabling `set_raw_out(true)` would require a separate
incremental framer and is prohibited without updating the protocol tests.

Requests use numeric IDs assigned by Rust:

```json
{"jsonrpc":"2.0","id":1,"method":"project.get_state","params":{"project_directory":"/selected/project","project_id":"project_1"}}
```

The worker must respond with the same ID. Unsolicited progress uses JSON-RPC
notifications. Rust wraps every notification with a host-monotonic sequence and
worker generation before emitting it to the renderer:

```json
{
  "sequence": 18,
  "workerGeneration": 1,
  "payload": {
    "jsonrpc": "2.0",
    "method": "solver_job.completed",
    "params": {
      "sequence": 31,
      "analysis_id": "analysis_1",
      "storm_id": "storm_1",
      "qa_status": "accepted"
    }
  }
}
```

Commands are acknowledgements, not long-running HTTP-style calls. An
`analysis.start` response should confirm durable state quickly; subsequent
progress arrives through notifications. Rust caps acknowledgement timeouts at
five minutes and does not assume that a timeout cancelled a durable solver job.

## Authoritative evidence contract

The renderer selects evidence by identity; it does not submit hydraulic values
or hashes. `evidence.resolve` accepts only `project_directory` and `job_id`.
The worker requires a `SUCCEEDED` repository job, reconstructs its metrics and
QA findings, and verifies every recorded artifact against its SHA-256 before
returning an exact-result payload.

Likewise, `export.create` accepts `project_directory`, `analysis_id`, and a
non-empty `job_ids` array. All selected jobs must belong to that analysis and
project. Project identity, title, readiness, input hashes, solver hash, exact
results, and optional INP/RPT/OUT attachments are derived from repository state.
The method rejects renderer-supplied `results`, `input_hashes`,
`solver_sha256`, `results_verified`, `attachments`, project metadata, and
readiness. `export.validate` is a pure preview helper; passing that preview does
not authorize or create an evidence package.

## Lifecycle and recovery

The worker lifecycle is `STOPPED`, `STARTING`, `READY`, or `FAULTED`. The host
starts it during application setup but keeps the window usable if launch fails.
The UI can call `worker_start` to recover. A process exit fails pending requests,
increments the generation on the next start, and does not delete project state.

On application exit, Rust terminates the worker. Worker-side SQLite transactions
and the durable solver state machine remain responsible for crash recovery;
platform code does not infer whether a scientific job succeeded from process
exit alone.

## Security model

- The webview receives no shell permission and cannot spawn the bundled sidecar
  itself.
- Native-dialog permission is scoped to the local `main` window.
- Project files are passed to typed worker methods; the frontend has no broad
  filesystem permission.
- The Content Security Policy permits only bundled application resources and
  Tauri IPC. It does not permit arbitrary network requests or remote scripts.
- The external worker is named in `bundle.externalBin`, built per target, hashed,
  and included in the release SBOM.
- The worker starts with a cleared environment. Rust restores only the small set
  of locale, timezone, user-data, and temporary-directory variables required by
  desktop runtimes and forces unbuffered UTF-8 Python I/O. It does not inherit
  `PYTHONPATH`, dynamic-loader injection variables, or the user's `PATH`.
- Worker stderr is not exposed through global webview events.
- Protocol method and message-size allowlists limit the renderer/worker boundary.

Optional open-data access belongs inside the Python `DataAssetService`, which
must require per-download consent and provider allowlisting. It does not require
weakening Tauri's renderer capabilities.

## Error boundary

Platform errors use the same user-facing shape as domain errors:

```text
code
category
severity
user_message
technical_message
object_reference
remediation
retryable
diagnostic_id
```

The Rust layer emits only host-level categories. Model-quality, solver, and data
provider errors stay structured in JSON-RPC responses from the Python worker.
