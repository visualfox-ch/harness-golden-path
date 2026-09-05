# P2-3 Unknown Side-Effect Recovery Proof — Evidence

## Result

`PASS` — a controlled execution stopped at an intentionally unknown side-effect
boundary. The Harness classified the failure fail-closed as
`uncertain_side_effect`, transitioned the task to `recovery_required`, created
one open Recovery Card, and blocked any blind retry. The proof was closed as
Evidenz-Fixture on 2026-09-05 (`projection_kind: evidence`): the card remains
visible as evidence but no longer counts as an operational risk.

## Task

- Task: `P2-3 unknown side-effect recovery proof`
- Task ID: `572a23d3-f5a6-4d35-b754-9a6909ed1249`
- Correlation ID: `3825c353-f708-44cb-8c41-2bf1b4ebc0ac`
- Idempotency key: `sha256:p2-3-live-recovery-proof-20260903-v1`
- Owner instance: `p2-3-live-proof`
- Circuit key: `route:anthropic_oauth_reasoner`

## Exercise time

- Started: `2026-09-03T20:40:36Z` (event `task_created`)
- Final state: `recovery_required` — terminal by design (no blind retry)

## Event trace (append-only)

```text
task_created          2026-09-03T20:40:36Z
task_claimed          2026-09-03T20:40:36Z
failure_classified    failure_class=uncertain_side_effect side_effect_state=unknown
recovery_card_created recovery_id=4633a63f-b959-487e-987d-156377db9f25 trigger=uncertain_side_effect
```

## Recovery Card

- Recovery ID: `4633a63f-b959-487e-987d-156377db9f25`
- Trigger: `uncertain_side_effect`
- Reason: `Controlled proof: execution stopped after an intentionally unknown
  side-effect boundary.`
- Allowed actions: `inspect_read_only`, `confirm_side_effect`,
  `request_human_decision`
- Status: `open` — kept as evidence, not resolved

## Fixture mark (2026-09-05)

- TaskCard updated in the operating DB: `card.projection_kind = 'evidence'`
- Cockpit risks panel ignores proof fixtures (`projection_kind = 'operational'`
  only): `recovery_count=0`, `open_recovery_card_count=0` for the proof
- Regression: `test_cockpit_excludes_proof_fixture_recovery_from_risks`

## Automated tests (after the cockpit fix)

```text
96 passed, 2 warnings in ~8s
```

The warnings are upstream deprecations in FastAPI/Starlette test dependencies;
they do not affect the proof result.
