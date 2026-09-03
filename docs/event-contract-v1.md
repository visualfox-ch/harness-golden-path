# Event Contract v1

Every Harness event is validated before it is appended to `agent_events`.
Unregistered event names, missing required payload fields, and additional payload
fields fail closed. A contract change therefore requires an intentional code and
test change in the same pull request.

The public `EventEnvelopeV1` contains only:

- `schema_version`, `event_id`, `task_id`, `correlation_id`, `event_type`,
  `payload`, and `created_at`;
- one of the registered event types in `HarnessEventType`;
- the exact payload field set registered for that event type in
  `EVENT_PAYLOAD_FIELDS`.

The CI contract tests also prove that persisted traces parse as `EventEnvelopeV1`
and that the PandaOS projection exposes only its documented display-safe field
set. Objectives, scopes, receipt bodies, event payloads, and credentials remain
outside the projection.
