#!/usr/bin/env bash
# The repo's checks. Exit 0 only when the branch is fit to review.
#
# The gate runs this script from the main checkout with the job's worktree as
# the working directory. Mirrors CI (pytest against postgres:16) plus ruff,
# with a throwaway Postgres container on a free local port.
set -euo pipefail

DOCKER="$HOME/.orbstack/bin/docker"
UV=/opt/homebrew/bin/uv
NAME="factory-harness-pg-$$"

"$DOCKER" run -d --rm --name "$NAME" -p 127.0.0.1::5432 \
  -e POSTGRES_USER=harness -e POSTGRES_PASSWORD=harness_dev -e POSTGRES_DB=harness \
  postgres:16 >/dev/null
trap '"$DOCKER" rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

for _ in $(seq 60); do
  "$DOCKER" exec "$NAME" pg_isready -U harness -h 127.0.0.1 >/dev/null 2>&1 && break
  sleep 1
done
PORT="$("$DOCKER" port "$NAME" 5432/tcp | head -1 | sed 's/.*://')"
export HARNESS_DATABASE_URL="postgresql://harness:harness_dev@127.0.0.1:$PORT/harness"

# As CI: install from pyproject (uv.lock is not tracked on main).
[ -d .venv ] || "$UV" venv --quiet --python 3.12
"$UV" pip install --quiet -e '.[dev]' 'ruff>=0.16.6'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
