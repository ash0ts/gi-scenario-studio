# Oversize synchronization approval

Professional W&B synchronization is fail-closed. `ResourcePolicy` fixes
`require_explicit_oversize_approval` to `true`; neither JSON-RPC nor a stored
project can opt out.

The upload preview binds the active model revision, exact Restricted destination
identity (`entity`, project name, and immutable external project ID), authorized
input projection, current-raster estimate, and every
committed telemetry envelope and retained artifact. Exact duplicate content is
counted once by SHA-256 and byte size. A newly finalized direct run or any other
new producer event changes the inventory digest and makes an earlier approval
stale.

## Approval audit reservation

Recording approval itself creates a W&B lineage event. To avoid silently adding
bytes after review, a plan requiring approval contains one versioned 16 KiB
reservation for that event before it is shown to the reviewer. The actual
no-artifact audit projection must validate and fit inside the reservation. On
commit it replaces the same ordinal reservation.

Provider artifact references and `sync_failures` rows are operational metadata,
not scientific evidence identity. They are never omitted from byte
authorization: each projected envelope carries a versioned 64 KiB operational
metadata reservation. A projection contains only the durable prior failure for
its own outbox row. Barrier batching merges those bounded rows once per member;
it never repeats a project-wide failure snapshot in every envelope. The local
SQLite outbox and immutable revalidation audits remain the complete diagnostic
record. Both inventory construction and the final single or barrier-batched
upload reject a projection whose full canonical bytes exceed its stable
envelope bytes plus the applicable per-member reservations. Malformed or
oversized current-row metadata fails closed, while accumulated failures on
unrelated rows cannot consume the current row's reservation or prevent retry.
Merged barrier framing receives no implicit allowance: its final canonical
bytes must fit inside the sum of the already inventoried member envelopes and
their per-member reservations.

## Exact upload membership

The inventory also commits the outbox ID, producer stream and sequence, payload
digest, stable projection digest and size, and artifact-set digest for every
eligible envelope. Online synchronization claims only those exact IDs and
revalidates each claimed member before an upload. If a direct run or another
producer commits after preview, that new row stays pending and changes the next
inventory; it cannot ride the earlier approval through a timing window. A
mismatched authorized row is blocked before W&B receives its projection.

Consequently these preview fields are identical before and after approval:

- `sync_plan_sha256`
- `model_revision_id` and `model_sha256`
- `telemetry_inventory_sha256`
- `input_bytes`, `estimated_current_raster_bytes`,
  `telemetry_projected_bytes`, and `estimated_total_bytes`
- `telemetry_artifact_count`
- approval-audit and operational-metadata reservation byte/count pairs

Each prior approval event occupies its own reserved ordinal. If evidence changes,
the next preview contains all prior audit reservations plus exactly one new
reservation for the next approval.

## Atomic and idempotent behavior

The immutable approval record and its redacted telemetry event commit in one
SQLite transaction. A failed outbox write rolls back both. Retrying the exact
request after a lost response returns the first reviewer and timestamp and does
not create another event. Reusing the same plan digest with a different reviewer
or acknowledgement fails with `OVERSIZE_SYNC_APPROVAL_CONFLICT`.

When earlier consent exists but no longer matches the current destination,
model, policy, estimate, or producer-event inventory, the preview reports
`STALE`. The current stale plan can receive a new exact approval after every
non-approval blocker is resolved; the old plan digest can never mutate or create
new consent.

W&B receives the approval ID, reviewed plan digest, reviewer, approval time,
destination, and model revision through a `release_gate` Run and the
`oversize_sync_approvals` Table. Credentials, raw binding secrets, and the full
acknowledgement text are never projected.
