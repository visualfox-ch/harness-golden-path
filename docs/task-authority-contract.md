# Task Authority Contract

## Why this boundary exists

PandaOS session tasks are useful for planning and user-visible progress, while
the Control Harness owns atomic claims, leases, attempts, approvals, receipts,
and recovery. Allowing both systems to change runtime status would create two
conflicting histories and make retry or approval decisions unsafe.

## Binding decision

The Control Harness is the only runtime authority. PandaOS session tasks are a
projection and may not write runtime status back into the Harness.

PandaOS may submit two explicit inputs through Harness APIs:

1. a new task intent before execution,
2. a human approval decision.

All runtime fields — including status, owner, lease, attempts, failures,
approvals, and receipts — flow from Harness events to projections. If a PandaOS
task differs from the Harness, the Harness wins and the projection is marked
stale. There is no automatic reverse synchronization.

## Current implementation boundary

`policies/task-authority.yaml` is loaded during Harness startup and task
creation fails closed if the authority or writeback boundary is weakened.
The read-only event-to-PandaOS source, mapping, ordering, drift detection, and
snapshot rebuild are implemented in P2-7. The PandaOS-hosted consumer remains
separate because PandaOS exposes no external session-task API to the Harness.
This contract does not claim that bidirectional synchronization exists.
