# PandaOS Projection Adapter

The adapter exposes Harness task state as a one-way, read-only PandaOS session
task projection. It never accepts PandaOS runtime status as input.

## Source endpoint

`GET /v1/projections/pandaos?after_event_id=0` returns the tasks changed after
the supplied authoritative event cursor. `full=true` ignores a stale local
cursor and returns a rebuild snapshot from event zero.
The response includes the event cutoff that must become the consumer's next
cursor only after the batch is applied successfully.

Only display-safe fields are projected. Objectives, scopes, data contents,
receipts, event payloads, and credentials are excluded.

## Consumer algorithm

For every `projection_key`, the PandaOS consumer stores its session task ID,
last `source_event_id`, and fingerprint. It uses `plan_projection` to choose:

- `create` when no target exists;
- `update` for a newer visible Harness state;
- `repair` when PandaOS differs from the recorded projection;
- `advance_cursor` when only non-visible Harness events changed;
- `noop` for a repeated delivery;
- `ignore_old` for an out-of-order batch.

The consumer updates only `subject`, `status`, and `activeForm` in PandaOS.
Proof fixtures use explicit `projection_kind: evidence`; they always have
`action_required: false` and are never inferred from their title.

## Failure behavior

- A cursor ahead of Harness returns HTTP 409 and requires a full rebuild.
- PandaOS downtime does not stop Harness execution.
- A failed batch is retried from the previous committed cursor.
- A full snapshot repairs missing or manually changed projections.
- Harness remains authoritative in every conflict.

## Current boundary

The Harness source endpoint, mapping, ordering, idempotency, drift detection,
and rebuild behavior are implemented here. PandaOS does not currently publish
an external session-task API for this service to call. The small consumer that
invokes PandaOS's internal `session_task_create` and `session_task_update`
actions therefore remains a PandaOS-hosted integration step.
