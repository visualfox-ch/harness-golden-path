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
