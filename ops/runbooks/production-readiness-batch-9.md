# Production Readiness Batch 9 — Observability and Operator Controls

Batch 9 adds the private operator surface, durable service telemetry, durable
operational alerts, machine-readable preflight, audited recovery controls,
structured redacted logs, and preview-first retention. It does not add remote
backup storage; backups remain local as approved.

## Safety contract

- The dashboard publishes only on `127.0.0.1` and requires the dedicated
  `operator_dashboard_password` Docker secret.
- The dashboard receives no provider, Telegram, SMTP, or backup encryption
  secret and does not join the backend network.
- The operator monitor receives no secrets. It reads SQLite/Redis health and
  the read-only backup directory, then queues Telegram/email intentions. The
  existing delivery worker alone owns transport credentials.
- Status and diagnostic queries never select analysis payloads, delivery
  bodies, raw event text, or secret values.
- Operational alerts use the normal leased delivery outbox. Quiet hours remain
  `22:00-07:00 Asia/Shanghai`; an outage leaves queued/dead/blocked evidence.
- Retention is preview-only by default. Applying it requires an operator note
  and the exact phrase `APPLY IIC-FORGE RETENTION`.
- LLM budget releases are append-only. The original ledger row is never
  updated or deleted, only a stale `reserved` call can be released, and the
  exact phrase is `RELEASE <call-id>`.
- Restore-drill success is recorded only after a real restore into disposable
  roots and post-restore SQLite/Redis verification.

## First deployment

Create the ninth mode-0600 secret:

```sh
openssl rand -base64 32 > secrets/operator_dashboard_password
chmod 0600 secrets/operator_dashboard_password
```

Start the stack and wait for health:

```sh
docker compose up -d --build
docker compose ps
```

The root-run backup container preserves the host backup-directory owner and
grants only the application data group read/traverse access (`0750` directory,
`0640` encrypted archive/marker/checksum). This lets the non-root monitor read
age/status without receiving the encryption key.

The first start applies additive migration
`0006_operator_observability.sql`. It creates:

- `service_heartbeats`
- `operational_alerts`
- `operator_actions`
- `llm_budget_releases`
- `recovery_drills`

Open `http://127.0.0.1:8501` on the Docker host and authenticate with the
dashboard password. Do not publish or reverse-proxy this port to an untrusted
network without adding an independently reviewed TLS/access-control layer.

## Routine inspection

Cheap dependency health (used by container health checks):

```sh
docker compose exec analysis-worker iic-forge forge runtime health
```

Bounded machine-readable snapshot:

```sh
docker compose exec analysis-worker \
  iic-forge forge operator status --full-database-check
```

Strict field preflight:

```sh
docker compose exec analysis-worker \
  iic-forge forge operator preflight \
  --require-production-config --require-all-services
```

Inspect open alert state and dedupe counts:

```sh
docker compose exec analysis-worker iic-forge forge operator alerts --open
```

## What the monitor evaluates

- durable heartbeat age/status for every long-lived service;
- analysis queued/running/retrying/error/blocked state and oldest pending age;
- delivery queued/running/retrying/dead/blocked state and oldest pending age;
- RSS, Telegram, and Polygon cursor time and latest accepted ingestion time;
- Redis PING, AOF write status, and `noeviction` policy;
- packaged SQLite migration match; strict preflight also runs integrity and
  foreign-key checks;
- data/backup disk free bytes and percentage;
- latest verified local-backup marker, archive presence, and age;
- Beijing-day paid-LLM utilization against USD 20;
- latest verified restore-drill result.

A heartbeat proves process liveness, not connector success. Connector status is
reported separately. A live RSS process with no accepted entries is therefore
distinguishable from an RSS process that stopped heartbeating.

## Recovery action matrix

| Condition | Inspect | Recover | Clear condition |
|---|---|---|---|
| Missing/stale service heartbeat | `docker compose ps`; service logs | restart only the named service | next fresh heartbeat |
| Redis health/AOF failure | `forge runtime health`; Redis logs | stop producers, repair/recreate Redis from a verified local backup | successful PING/AOF/policy check |
| SQLite integrity/migration failure | strict preflight; migration backup directory | stop stack; use Batch 8 restore procedure | full integrity and FK checks pass |
| Analysis `error`/`blocked` | `forge orchestrator inspect <id>` | correct cause, then `retry <id> --note ...`; or `cancel` inactive work | no remaining terminal job |
| Delivery `dead` | `forge delivery inspect <id>` | `retry <id> --note ...` | job becomes queued/sent |
| Delivery `blocked` | inspect error category; correct credential/recipient/config | `requeue <id> --note ...` | job becomes queued/sent |
| Low disk | operator status; retention preview | expand disk or apply reviewed retention | free space above both thresholds |
| Backup missing/stale | `forge backup status` and backup-create logs | run stopped-stack backup-create | current verified marker/archive |
| LLM budget near/exhausted | operator status; inspect ledger metadata | normally wait for Beijing reset; release only proven abandoned reservation | utilization below threshold/new day |
| Restore drill missing/failed | latest drill row and command output | repair backup/restore cause and repeat isolated drill | latest drill is `passed` |

Never retry or cancel a currently running analysis/delivery job. The controls
refuse active work rather than terminating a process with unknown side effects.

To wait for analysis work to finish, first stop producers (`scheduler`,
`promoter`, `action-handler` as appropriate), then:

```sh
docker compose exec analysis-worker \
  iic-forge forge orchestrator drain --timeout-seconds 900
```

## Audited administrative controls

Inspect or cancel without exposing the job payload:

```sh
iic-forge forge orchestrator inspect 42
iic-forge forge orchestrator cancel 42 --note "obsolete duplicate trigger"
```

Release an abandoned reservation only after proving no provider request could
be billed:

```sh
iic-forge forge operator release-budget CALL_ID \
  --minimum-age-hours 1 \
  --note "worker died before provider request" \
  --evidence "request id absent from provider/gateway audit" \
  --confirm "RELEASE CALL_ID"
```

The original `llm_budget_ledger` row remains `reserved`; the release appears in
`llm_budget_releases` and `operator_actions`. Settled/charged/recent calls and
repeat releases are refused.

Preview retention:

```sh
iic-forge forge operator retention --preview
```

Apply the exact preview recalculated at command time:

```sh
iic-forge forge operator retention --apply \
  --note "reviewed 90d quarantine, 730d discarded events, 30d logs" \
  --confirm "APPLY IIC-FORGE RETENTION"
```

Retention never recursively deletes directories. It removes only safe regular
files under the data root and eligible database rows. Briefs, reports,
backtests, accepted/active events, and unknown files are preserved.

## Restore drill

Run against an encrypted archive while the production stack remains untouched:

```sh
docker compose --profile operations run --rm backup-restore \
  forge operator restore-drill /backups/ARCHIVE.iicbak \
  --key-file /run/secrets/backup_encryption_key \
  --note "quarterly local restore drill" \
  --confirm "RUN RESTORE DRILL"
```

The command decrypts and restores into disposable temporary roots, validates
SQLite migrations/integrity and Redis persistence, destroys the temporary
roots, then records duration and ciphertext SHA-256. A failed drill is also
recorded and exits non-zero.

## Field acceptance tests

Perform these on a disposable Compose deployment before declaring production:

1. Stop `delivery-worker`, trigger one monitor condition, and verify two
   `operational_alert` rows remain queued. Restart the worker and require both
   channels to send or expose an actionable blocked state.
2. Stop one service for more than 90 seconds. Require a stale heartbeat alert,
   restart it, and require the monitor to resolve the alert.
3. Break one SMTP/Telegram credential on the disposable stack. Require bounded
   retries followed by blocked/dead visibility; correct it and use the audited
   requeue/retry command.
4. During Beijing quiet hours, create an operational alert. Require it to stay
   queued until the calculated next allowed UTC time.
5. Visit the dashboard without a password, with a wrong password, and with the
   correct password. Require loopback-only reachability and verify the
   dashboard container has only its password secret.
6. Run strict preflight with all services healthy and a current backup. Require
   exit 0 and `status=ok`.
7. Run the restore drill above and require the latest `recovery_drills` row to
   be `passed`.
8. Put a known canary credential in an induced exception. Require Docker JSON
   logs to be valid JSON and contain `[REDACTED]`, never the canary.
9. Preview retention and compare every candidate path/ID before applying on
   disposable data.

## Rollback

Application rollback to a build that supports only schema version 5 is not
safe after migration 6; the older runtime will correctly refuse a newer
database. Roll back by stopping the stack and restoring the verified
pre-migration database/volume backup created by the migration system, or roll
forward with the Batch 9 image. Do not delete migration rows or tables by hand.

The dashboard and monitor can be stopped independently without affecting
ingestion or delivery:

```sh
docker compose stop dashboard operator-monitor
```
