# Routing- und Qualitätsmetriken

`GET /v1/operations/metrics` liefert ausschliesslich aus dem Harness-State-Store
abgeleitete Evidenz. Die Quelle sind `routing_receipts` und die zugehörigen
`agent_tasks`; der Endpoint schreibt nichts. Es werden ausschliesslich
`projection_kind: operational` gezählt. Proof-Fixtures (`evidence`) bleiben
prüfbar im Eventlog, beeinflussen aber keine Betriebskennzahl.

| Kennzahl | Bedeutung | Verhalten ohne Evidenz |
| --- | --- | --- |
| First-pass-Rate | abgeschlossene Receipts mit genau einem Attempt / alle terminalen Receipts | `unavailable` |
| Retry-Rate | terminale Receipts mit mehr als einem Attempt / alle terminalen Receipts | `unavailable` |
| Routen | Counts, Outcomes und inkrementelle Kosten je Modell/Providerklasse | leere Liste |
| Validation | persistierte Lint-/Test-Ergebnisse aus Receipts | Zähler bleiben bei null |
| Eskalation, Rework | benötigen ein eigenes normalisiertes Ereignis bzw. Dauerfeld | explizit `unavailable` |

Damit macht der Cockpit-Pfad keine Qualitäts- oder Kostenbehauptung, für die
kein persistierter Nachweis vorliegt. P3-2 darf erst auf dieser Basis und nach
mindestens 20 realen Tasks Routing-Entscheide vergleichen.

## Beispielaufruf (lokal)

Ohne Authentifizierung, wenn der Harness lokal mit `uvicorn harness.app:app --port 8787` läuft:

```bash
curl -s http://127.0.0.1:8787/v1/operations/metrics
```

Dabei ist kein Header nötig: Ist `HARNESS_AUTH_REQUIRED` nicht `1` und keine Token-Datei konfiguriert, ist der `TokenAuthorizer` (`src/harness/auth.py`) abgeschaltet.

Mit aktivierter Authentifizierung wird das Token aus der Token-Datei gelesen:

```bash
curl -s -H "Authorization: Bearer $(cat "$HARNESS_ORCHESTRATOR_TOKEN_FILE")" \
  http://127.0.0.1:8787/v1/operations/metrics
```

Der Endpoint braucht den Scope `operations:read`; von den beiden konfigurierten Identitäten hat ihn nur `svc-panda-orchestrator` (`HARNESS_ORCHESTRATOR_TOKEN_FILE`).

Das Worker-Token `svc-hermes-nas` erhält HTTP 403, ein fehlendes oder ungültiges Token HTTP 401.
