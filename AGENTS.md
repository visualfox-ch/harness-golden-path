# harness-golden-path

Throwaway proof repo for the Control Harness Golden Path (ADR-001): a minimal
deterministic task harness (FastAPI, Pydantic v2, PostgreSQL) with a task state
machine, claim/lease/heartbeat, idempotency, approval gate and append-only
event log. See `README.md`.

## Setup of a fresh checkout

```bash
uv venv --python 3.12 && uv pip install -e '.[dev]' 'ruff>=0.16.6'
```

Tests need PostgreSQL 16. `factory/checks.sh` starts a throwaway container and
sets `HARNESS_DATABASE_URL`; do not start your own database for a factory job.

## Dev server

`.venv/bin/uvicorn harness.app:app --port <port>` (the factory gives each job a
port in its prompt; default 8787).

## Validation

`bash factory/checks.sh` runs `ruff check .`, `ruff format --check .` and
`pytest -q` against a throwaway Postgres. It must pass before review.

## Conventions

- SQL table aliases never use reserved keywords (`AS task`, `AS t`, never `AS at`).
- Docs are in German or English as in the surrounding file; keep it.

## Software factory

`/factory run` builds every open GitHub issue labeled `factory` through
intake → spec → build → gate → review → approve, one PR per issue. State is in
`factory/jobs/` (gitignored); `factory/bin/job` is its only writer. Checks:
`factory/checks.sh`. Dashboard: `cd factory && python3 -m http.server`, then
open http://localhost:8000/dashboard.html.

Each job works in its own git worktree, a full checkout of the repo at
`.worktrees/<job-id>` (gitignored). A tool run from the repo root that doesn't
read `.gitignore` sees those copies too; exclude `.worktrees/` in its config.

### Git command shapes for factory agents

A local approval hook on the maintainer's machine lets a factory agent commit and
push a `factory/*` branch only when each command has exactly one of these shapes.
Chained commands (`git add … && git commit …`) and flags such as `-q`/`-qm` are
refused. Run them as separate Bash calls:

1. `cd <worktree> && git add <file> <file>` (separate call, files named, never `-A`)
2. `cd <worktree> && git commit -m "<message>"` (double quotes, no `$`, backticks or backslashes in the message)
3. `cd <worktree> && git push -u origin <branch>` (only the job branch)

Then open the draft PR with the `gh pr create --draft …` command from the builder
instructions. If a command is refused, stop and put it under **Blocked** in
`build.md`; never look for another way around the hook.
