# P2-6 Backup/Restore and Lease-Recovery Evidence

## Result

`PASS` — logical PostgreSQL backup and restore reproduced the authoritative
Harness state exactly. A real fixture worker was terminated with `SIGKILL`;
after restore, its expired lease created one Recovery Card and could not be
blindly claimed again.

This evidence covers only the isolated P2-6 test stack. It does not touch the
P2-4 NAS pilot and does not authorise a productive write path.

## Test environment

- Exercise time: `2026-09-03T23:03:12Z`
- Compose project: `harness-p2-6`
- PostgreSQL: `postgres:16-alpine`
- Source: dedicated volume, internal-only network, no published port
- Restore: dedicated volume, internal-only network, no published port
- Backup: `/private/tmp/harness-p2-6.backup`
- Backup SHA-256: `37141696532c799f0f394875ee625feb3188a9ef2cfd342d8e4bfa897c750b33`

## Command

```bash
bash scripts/p2-6-backup-restore.sh
```

## Verified output

```text
WORKER_CRASH|signal=SIGKILL|exit=137|task_status_before_backup=claimed
SOURCE_MANIFEST|{"counts":{"agent_events":6,"agent_tasks":3,"approvals":0,"circuit_breakers":0,"dead_letters":0,"recovery_cards":0,"routing_receipts":1},"database":"harness","max_event_id":6,"schema_version":1,"sha256":"5cc35953baf17e568d3c55ca24c246fd3d1f6b22aa26f8165241c7f1f8b3d7cb"}
RESTORE_MANIFEST|{"counts":{"agent_events":6,"agent_tasks":3,"approvals":0,"circuit_breakers":0,"dead_letters":0,"recovery_cards":0,"routing_receipts":1},"database":"harness","max_event_id":6,"schema_version":1,"sha256":"5cc35953baf17e568d3c55ca24c246fd3d1f6b22aa26f8165241c7f1f8b3d7cb"}
PARITY|PASS
LEASE_RECOVERY|{"attempt_count":1,"blind_retry_claimed":false,"expired_count":1,"recovery_card_count":1,"recovery_trigger":"lease_expired","second_expiry_count":0,"status":"recovery_required"}
RESULT|PASS
```

## Automated tests

```text
74 passed, 2 warnings in 7.46s
```

The warnings are upstream deprecations in FastAPI/Starlette test dependencies;
they do not affect the exercise result.
