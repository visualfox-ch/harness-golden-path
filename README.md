# harness-golden-path

Wegwerf-Testumgebung für den **Control Harness Golden Path** gemäss ADR-001:
ein minimaler, deterministischer Task-Harness (FastAPI + Pydantic v2 + PostgreSQL)
mit Task-State-Machine, atomarem Claim/Lease/Heartbeat, Idempotenz, Approval-Gate
und append-only Eventlog.

Keine Produktionsnähe. Dieses Repository existiert ausschliesslich, um den
Golden Path (TaskCard → Harness → Executor → PR → CI → Approval → Merge)
einmal vollständig und ehrlich instrumentiert zu durchlaufen.

## Architektur-Herkunft

- Statusmenge und Transitionsdisziplin: portiert aus der Zielarchitektur
  (Hermes / Panda OS / NAS, Stand 2026-09-03) und der `pandaos-hermes-bridge`.
- Policy: `api_metered` ist verboten; einzige zugelassene Modellroute ist die
  verifizierte Anthropic-OAuth-Route (`policies/model-catalog.yaml`).

## Entwicklung

```bash
uv venv && uv pip install -e '.[dev]'
docker run -d --name harness-pg -p 5433:5432 \
  -e POSTGRES_USER=harness -e POSTGRES_PASSWORD=harness_dev -e POSTGRES_DB=harness \
  postgres:16
pytest -q
uvicorn harness.app:app --port 8787
```

`harness_dev` ist ein lokales Wegwerf-Passwort für den Dev-Container, kein Secret.
