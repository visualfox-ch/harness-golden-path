# Task Authority Contract

## Why this boundary exists

PandaOS session tasks are useful for planning and user-visible progress, while
the Control Harness owns atomic claims, leases, attempts, approvals, receipts,
and recovery. Allowing both systems to change runtime status would create two
conflicting histories and make retry or approval decisions unsafe.

## Binding decision

The Control Harness is the only runtime authority. PandaOS session tasks are a
projection and may not write runtime status back into the Harness.

PandaOS may submit three explicit inputs through Harness APIs:

1. a new task intent before execution,
2. a priority change before claim,
3. a human approval decision.

All runtime fields — including status, owner, lease, attempts, failures,
approvals, and receipts — flow from Harness events to projections. If a PandaOS
task differs from the Harness, the Harness wins and the projection is marked
stale. There is no automatic reverse synchronization.

## Current implementation boundary

`policies/task-authority.yaml` is loaded during Harness startup and task
creation fails closed if the authority or writeback boundary is weakened. The
actual event-to-PandaOS projection adapter is future work; this contract does
not claim that bidirectional synchronization exists.
