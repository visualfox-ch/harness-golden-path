# Harness State Store Backup/Restore Runbook v1

## Scope

This runbook proves logical backup, restore, and expired-lease recovery in the
isolated `harness-p2-6` Docker stack. It never connects to the P2-4 NAS pilot or
to a production database.

Isolation boundaries:

- dedicated Compose project `harness-p2-6`;
- dedicated source and restore volumes;
- internal-only network with no published database ports;
- fixture commands fail unless `P2_6_FIXTURE_MODE=1` and the database host is
  exactly `source` or `restore`;
- no credentials, Docker socket, or repository write access inside the
  exercise containers.

## Execute

From the repository root:

```bash
bash scripts/p2-6-backup-restore.sh
```

The logical custom-format backup is written to
`/private/tmp/harness-p2-6.backup` by default. Override only with an explicit
test path using `P2_6_BACKUP_PATH`.

The script intentionally resets only the `public` schemas of the two isolated
exercise databases. It does not remove containers or volumes automatically.

## PASS criteria

All criteria must hold:

1. The backup file is non-empty and has a reported SHA-256 digest.
2. Source and restore manifests have the same digest and row counts across all
   seven authoritative Harness tables.
3. Exactly one expired lease changes to `recovery_required`.
4. The fixture worker was actually terminated with `SIGKILL` and exit code
   `137` while its task was `claimed`.
5. Exactly one `lease_expired` recovery card exists after two expiry scans.
6. The recovered task remains at `attempt_count=1`.
7. A blind claim by another worker returns no task.
8. The script terminates with `RESULT|PASS`.

Any mismatch is a FAIL. A successful restore is not permission to enable a
productive write path.

## Inspection and cleanup

Inspect without mutation:

```bash
docker compose -p harness-p2-6 -f docker-compose.p2-6.yml ps
```

Cleanup deletes the isolated exercise databases and therefore requires an
explicit operator decision:

```bash
docker compose -p harness-p2-6 -f docker-compose.p2-6.yml down --volumes
```
