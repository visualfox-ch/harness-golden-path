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

## Golden Path Status

Der erste End-to-End-Durchlauf (DEV-001, 2026-09-03) hat den vollständigen
Pfad TaskCard → atomarer Claim/Lease → isolierter Worktree → Docs-Änderung →
gezielte Tests → Pull Request → CI → ResultReceipt → Approval-Gate durchlaufen.

Verifizierte Gates: 17/17 Harness-Tests grün, direkter Push auf `main` wird
durch das Ruleset abgewiesen (GH013), Merge erfolgt ausschliesslich nach
menschlicher Freigabe über eine ApprovalCard. Modellroute: ausschliesslich
`anthropic_oauth_reasoner` (subscription_oauth, inkrementelle Kosten 0.00 CHF).

**Status 2026-09-03:** Der Golden Path ist über den Erstnachweis hinaus als
versionierter Feature-Delivery-Workflow verfügbar. Live-Operations-Snapshots,
ein evidenzgebundenes System-Cockpit sowie fail-closed Retry-, Circuit-Breaker-,
Dead-Letter- und Recovery-Card-Pfade sind implementiert und auf `main`
verifiziert. Proof-Fixtures bleiben als Evidenz sichtbar, werden jedoch nicht
als operative Arbeit oder Architektur-Roadmap interpretiert.

## Live Operations

P2-1 verbindet den Harness-State read-only mit einer PandaOS-Automation. Ein
kontrollierter `task_created`-Event wurde beim nächsten Datenbank-Poll erkannt;
der resultierende Snapshot wurde erfolgreich als neuer Chat sowie als
Status-/KPI-Karten ausgeliefert. Der vollständige, timestamped Nachweis steht
unter [P2-1 Live Operations](evidence/p2-1-live-operations-2026-09-03.md).

## P2-4 Hermes-NAS Read-only Pilot

`docker-compose.p2-4.yml` betreibt einen isolierten Pilot-State-Store, eine
token-geschützte Harness-API und den Worker `svc-hermes-nas:p2-4`. Der Worker
hat ausschliesslich Scopes für Task-Lesen, Claim, Heartbeat und Receipt. Seine
Probes sind im Code auf öffentliche Read-only-Metadaten dieses Testrepos
begrenzt; Repository-Schreiben, Docker-Socket-Zugriff und Deploy-Aktionen sind
nicht vorhanden.

Tokens liegen nur in gemounteten Dateien und werden von der API pro Request neu
gelesen. Rotation widerruft daher den bereits geladenen Worker-Token beim
nächsten Heartbeat; der erwartete HTTP-401-Abbruch ist Teil des Pilotnachweises.
Die API wird auf dem NAS ausschliesslich an `127.0.0.1:18787` veröffentlicht.
Der timestamped Start- und Revocation-Nachweis steht unter
[P2-4 NAS Read-only Pilot](evidence/p2-4-nas-readonly-pilot-start-2026-09-03.md).

## Policy- und Task-Autorität

TaskCards tragen eine `task_class` und `data_classification`. Der Harness
verwirft Tasks, wenn Rolle, zentrale Route, Datenklasse oder erforderliche
Approval-Aktion nicht zusammenpassen. Secrets sind nur als Referenzen erlaubt.

Der [Task Authority Contract](docs/task-authority-contract.md) legt fest:
Harness ist die einzige Runtime-Wahrheit; PandaOS-Tasks sind eine Projektion
ohne Status-Writeback.

Der [PandaOS Projection Adapter](docs/pandaos-projection-adapter.md) stellt
dafür einen read-only Incremental-/Rebuild-Feed mit Event-Cursor bereit. Das
deterministische Consumer-Mapping erkennt veraltete Events und Drift, liefert
aber keinen PandaOS→Harness-Statuspfad.
