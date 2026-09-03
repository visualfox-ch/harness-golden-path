# P2-4 Hermes-NAS Read-only Pilot — Startnachweis

Stand: 2026-09-03, UTC

## Ergebnis

**PASS mit zwei dokumentierten Plattform-Warnungen.** Der isolierte
`svc-hermes-nas`-Worker läuft auf dem NAS, hat genau einen Task geclaimt und
liefert allowlist-basierte Read-only-Reports. Der Credential-Widerruf wurde
live erkannt; der Worker brach mit HTTP 401 ab und nahm denselben Task nach
dem Neustart mit dem neuen Token wieder auf. Es entstand kein Doppel-Claim.

## Gebundener Stand

| Feld | Wert |
|---|---|
| Pull Request | `#9` |
| Deployment-Commit | `7c3714e` |
| CI | `test` erfolgreich, 27 Sekunden |
| CI-Run | `33811093018` |
| Task-ID | `1e6e2903-d46c-4109-b0ed-ad1daca842f9` |
| Correlation-ID | `f6f04447-2d47-4d12-94d1-8fb5c6d1c32a` |
| Worker | `svc-hermes-nas:p2-4` |
| Pilot-Ende | `2026-09-10T21:59:51Z` |
| Regelintervall | 300 Sekunden |

PR #9 ist noch nicht gemergt. Der Pilot ist deshalb bewusst an den exakten,
CI-grünen Feature-Commit gebunden; das Merge-Gate bleibt unverändert.

## Laufzeitgrenzen

- eigener Compose-Stack `harness-p2-4`
- eigener PostgreSQL-State-Store und eigene Docker-Netze
- API und Worker als UID/GID `10001:10001`
- read-only Root-Filesystem, `cap_drop: ALL`, `no-new-privileges`
- kein Docker-Socket und keine Host-Bind-Mounts im Worker
- GitHub-Probe fest auf `visualfox-ch/harness-golden-path` begrenzt
- keine Repository-, Deployment-, Secret- oder IAM-Schreibfunktion
- Secrets liegen ausserhalb des Git-Archivs in einem Host-Verzeichnis mit
  Modus `0700`; die einzelnen bind-gemounteten Dateien sind wegen des alten
  NAS-Docker-Modells innerhalb dieses Verzeichnisses `0444`

Auf diesem Synology-Docker wurde der deklarierte Loopback-Port nicht
veröffentlicht (`NetworkSettings.Ports={}`). Die API ist daher zur Laufzeit
nur im internen Pilot-Netz erreichbar, also enger als die deklarierte
Loopback-Grenze.

## Revocation-Negativtest

| Zeitpunkt (UTC) | Nachweis |
|---|---|
| `2026-09-03T22:05:36Z` | NAS-Worker-Token atomar rotiert |
| unmittelbar danach | laufender Worker meldet `credential_revoked` nach HTTP 401 |
| `2026-09-03T22:05:39Z` | Neustart liest neues Token und setzt Reporting fort |
| `2026-09-03T22:06:10Z` | Worker läuft; Restart-Zähler `1` |

Store-Zustand nach dem Test:

| Feld | Wert |
|---|---|
| Status | `claimed` |
| Owner | `svc-hermes-nas:p2-4` |
| Attempt Count | `1` |
| `task_created` Events | `1` |
| `task_claimed` Events | `1` |

Damit ist belegt: Ein altes Credential verliert beim nächsten API-Aufruf
sofort seine Wirkung; der Neustart erzeugt weder einen zweiten Task noch einen
zweiten Claim.

## Pilotstart

Nach dem Negativtest wurde der Worker mit dem neuen Token und 300-Sekunden-
Intervall neu erstellt. Verifikation um `2026-09-03T22:06:46Z`:

- PostgreSQL: healthy
- Harness API: healthy
- Watcher: running
- Task: `claimed`, Attempt Count `1`
- erster Regelintervall-Report: `2026-09-03T22:06:28.780426Z`
- beobachteter `main`-SHA: `80aea8db455a25f5eea8420ff1e1965c5687a70e`
- beobachteter CI-Status: `completed/success`

## Automatische Abschlussprüfung

Die aktive PandaOS-Automation `P2-4 NAS Pilot Abschlussprüfung`
(`2fe7668b-a2a6-4d08-b8ed-2448d79d5ed7`) läuft einmalig am
`2026-09-10T22:09:51Z` und liefert das Ergebnis in einen neuen Chat. Sie darf
über eine exakte Tool-Regel ausschliesslich den read-only Audit-Entry-Point
`hermes-panda-os/scripts/p2-4-final-audit.sh` ausführen. Catch-up ist aktiv.

Die Automation validierte schema-konform. PandaOS weist darauf hin, dass der
`rules`-Modus für unbeaufsichtigte Läufe funktionierende Claude-Credentials
benötigt; dies bleibt eine Ausführungsvoraussetzung für die Ergebniszustellung.

## Plattform-Warnungen

1. Das NAS-Buildx ist älter als die von Compose verlangte Version 0.17. Der
   identische Dockerfile wurde deshalb mit dem vorhandenen klassischen Docker-
   Builder gebaut. Es wurde kein NAS-Upgrade durchgeführt.
2. Der NAS-Kernel verwirft `pids_limit`; read-only Root-Filesystem,
   Capability-Drop und `no-new-privileges` sind aktiv. Diese Abweichung bleibt
   für die Abschlussauswertung sichtbar.

## Evidenzgrenze

Dieser Nachweis belegt Start, Rollenbegrenzung, Laufzeithärtung und Widerruf.
Die geforderte Stabilität über sieben Tage kann erst nach dem konfigurierten
Pilot-Ende bewertet werden.
