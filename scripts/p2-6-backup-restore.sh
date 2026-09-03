#!/usr/bin/env bash
set -euo pipefail

readonly COMPOSE_FILE="docker-compose.p2-6.yml"
readonly PROJECT="harness-p2-6"
readonly BACKUP_PATH="${P2_6_BACKUP_PATH:-/private/tmp/harness-p2-6.backup}"

compose() {
  docker compose --project-name "${PROJECT}" --file "${COMPOSE_FILE}" "$@"
}

reset_public_schema() {
  local service="$1"
  compose exec -T "${service}" psql -v ON_ERROR_STOP=1 -U harness -d harness \
    -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'
}

date -u '+EXERCISE_AT|%Y-%m-%dT%H:%M:%SZ'
echo "ISOLATION|project=${PROJECT}|network=internal|published_ports=none"

compose --profile exercise build fixture-source
compose up -d --wait source restore
reset_public_schema source
reset_public_schema restore

compose --profile exercise run --rm fixture-source >/dev/null
compose --profile exercise up -d crash-worker
worker_id="$(compose ps -q crash-worker)"
test -n "${worker_id}"
for _ in {1..20}; do
  worker_status="$(compose exec -T source psql -U harness -d harness -Atc \
    "SELECT status FROM agent_tasks WHERE task_id='26000000-0000-4000-8000-000000000002'")"
  [[ "${worker_status}" == "claimed" ]] && break
  sleep 0.25
done
test "${worker_status}" = "claimed"
compose kill -s SIGKILL crash-worker >/dev/null
worker_exit="$(docker inspect --format '{{.State.ExitCode}}' "${worker_id}")"
test "${worker_exit}" = "137"

source_manifest="$(compose --profile exercise run --rm fixture-source \
  python -m harness.continuity manifest)"
compose exec -T source pg_dump -U harness -d harness \
  --format=custom --no-owner --no-acl > "${BACKUP_PATH}"
test -s "${BACKUP_PATH}"
backup_sha="$(shasum -a 256 "${BACKUP_PATH}" | awk '{print $1}')"

compose exec -T restore pg_restore -v -U harness -d harness \
  --no-owner --no-acl < "${BACKUP_PATH}" >/dev/null
restore_manifest="$(compose --profile exercise run --rm fixture-restore)"

if [[ "${source_manifest}" != "${restore_manifest}" ]]; then
  echo "PARITY|FAIL"
  echo "SOURCE_MANIFEST|${source_manifest}"
  echo "RESTORE_MANIFEST|${restore_manifest}"
  exit 1
fi

recovery="$(compose --profile exercise run --rm fixture-restore \
  python -m harness.continuity recover)"

echo "BACKUP|path=${BACKUP_PATH}|sha256=${backup_sha}"
echo "WORKER_CRASH|signal=SIGKILL|exit=${worker_exit}|task_status_before_backup=claimed"
echo "SOURCE_MANIFEST|${source_manifest}"
echo "RESTORE_MANIFEST|${restore_manifest}"
echo "PARITY|PASS"
echo "LEASE_RECOVERY|${recovery}"
echo "RESULT|PASS"
