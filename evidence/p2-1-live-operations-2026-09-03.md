# P2-1 Live Operations — E2E-Evidenz

Stand: 2026-09-03, UTC

## Ziel

Den append-only Zustand des Golden-Path-Harness über eine read-only
PandaOS-Datenbankverbindung beobachten und bei einer belegten Änderung einen
Live-Operations-Snapshot auslösen.

## Aufbau

- Datenquelle: lokaler PostgreSQL-State-Store des Test-Harness
- Automation: `Harness Live Operations`
- Automation-ID: `e597d66c-4e91-4194-9b26-164b41a58a3e`
- Trigger: PandaOS `database.condition_matched`
- Abfrage: read-only `SELECT` über `agent_tasks` und `agent_events`
- Erkennung: `value_changed` auf dem technisch berechneten Snapshot
- Polling-Intervall: 5 Minuten
- Ausführung: zwei Knoten (`ai`, `output`), Ergebnis in neuem Chat
- Side Effects: keine ausser dem ausdrücklich angelegten Test-Task

Credentials und Connection-Strings sind nicht Bestandteil dieser Evidenz.

## Kontrollierter State-Wechsel

Nach der initialen Baseline wurde über `POST /v1/tasks` eine harmlose TaskCard
angelegt. Damit lief die Mutation durch Contract-, Policy- und Store-Code des
Harness; es gab keinen direkten SQL-Write.

| Feld | Wert |
|---|---|
| Task | `P2-1 Database trigger proof` |
| Task-ID | `ebd0b36d-a66c-4303-924d-9625cb9864be` |
| Correlation-ID | `cc122510-b415-46a7-b996-a81ecb6e7509` |
| Idempotency-Key | `sha256:p2-1-live-operations-trigger-proof-20260903` |
| Harness-Status | `ready` |
| Event | `82 / task_created` |
| Event-Zeit | `2026-09-03T14:21:02.214246Z` |

Die Task blieb absichtlich unangetastet auf `ready`: Sie beweist die
Beobachtung, nicht die Ausführung eines Arbeitsauftrags.

## Trigger- und Delivery-Timeline

| Zeitpunkt (UTC) | Nachweis |
|---|---|
| `14:20:59.724` | Baseline gespeichert |
| `14:21:02.214` | Event 82 append-only geschrieben |
| `14:25:59.856` | nächster Poll; Snapshot-Hash geändert; Trigger gefeuert |
| `14:26:19.723` | Output in neuen Chat ausgeliefert |
| `14:26:19.744` | Automation-Run erfolgreich abgeschlossen |

Run-Fakten:

- `trigger_source=app_event`
- `provider=database`
- `matched_count=1`
- `engine=codex`
- `node_count=2`
- `duration_ms=19822`
- Output-Chat: `4553051b-6708-4c76-a5d1-61f08c0d5457`

## Verifizierter Snapshot nach dem Trigger

| Metrik | Wert |
|---|---:|
| Tasks insgesamt | 4 |
| `done` | 2 |
| `ready` | 2 |
| `awaiting_approval` | 0 |
| append-only Events | 12 |

DEV-001 und DEV-002 waren `done`. Die bestehende Golden-Path-Docs-Task und
die Proof-Task waren `ready`. Statusfarben wurden nur aus diesen technischen
Statuswerten abgeleitet; geplante Tasks wurden nicht als ausgeführt gezeigt.

## Ergebnis

**PASS.** Ein echter, kontrollierter Harness-State-Wechsel wurde beim nächsten
Polling-Lauf genau einmal erkannt. Die Automation lief vollständig durch und
lieferte den verifizierten Snapshot in einen neuen PandaOS-Chat. Die Live-Sicht
wurde zusätzlich als `status_board` und `kpi_cards` gerendert.

## Evidenzgrenze

Der Trigger-State, der Automation-Run und die UI-Auslieferung sind technisch
belegt. Ein separater binärer Screenshot wurde nicht erzeugt; die visuellen
Karten existieren als PandaOS-Chat-UI und werden deshalb nicht als Bilddatei in
diesem Repository ausgegeben.
