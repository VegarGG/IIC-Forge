# Production readiness: Batch 3 durable queues

Batch 3 makes analysis jobs and outbound event alerts durable across process
failure, quiet hours, and transient transport outages. It does not change
event selection, ingestion connectors, LLM budget policy, backup retention, or
Docker Compose topology.

## Production contract

- Every `event_alert` and `event_alert_light` channel intent is written to
  SQLite before any Telegram, email, or CLI transport attempt.
- The brief, light-alert approval actions, ticker suppression, and all enabled
  channel intents commit atomically. A crash cannot suppress an alert without
  preserving its outbound work.
- Alert channel calls enqueue even when invoked outside the Secretary.
- Quiet hours are 22:00 through 07:00 in `Asia/Shanghai`, independent of the
  host or container timezone.
- Quiet-hours records remain `queued` with `available_ts` set to the next
  07:00 Beijing boundary. Quiet hours do not consume an attempt.
- Transport failures use bounded exponential backoff. After five attempts by
  default, the intent becomes `dead`; its content and last error remain
  inspectable and the operator can explicitly retry it.
- Analysis jobs use idempotency keys, scheduled availability, bounded
  attempts, lease expiry, and opaque fencing tokens.
- An expired worker cannot mark a newer attempt complete.
- Full-alert brief identity is deterministic per queue job. If a process dies
  after committing the brief/outbox but before acknowledging the job, the next
  attempt reuses the complete brief and its two channel intents.
- Expired leases are re-queued until their attempt budget is exhausted.
- Queue delivery is at least once. Telegram and SMTP do not share a durable
  idempotency primitive with SQLite, so a crash after provider acceptance but
  before the local acknowledgement can cause a duplicate. No alert is silently
  dropped to avoid that duplicate risk.

## Schema version 2

Migration `0002_queue_lifecycle.sql` adds the following analysis-job fields:

- `idempotency_key`
- `attempt_count` and `max_attempts`
- `available_ts`
- `lease_token` and `lease_expires_ts`
- `last_error_ts`

It also adds `delivery_queue`, the mutable outbox lifecycle table. The existing
`deliveries` table remains the append-only transport-attempt audit trail.

A database at schema version 1 is verified, backed up locally, and upgraded
transactionally to version 2 by the Batch 2 migration framework. A fresh
database applies both migrations in one bootstrap transaction and does not
create a pre-migration backup.

## Analysis-job lifecycle

```text
enqueue -> queued -> running -> done
                      |
                      +-> queued (retry with backoff)
                      +-> error  (attempts exhausted)
```

`lease_one()` atomically selects the oldest eligible row, increments its
attempt counter, and assigns a random lease token and expiry. Completion and
failure updates must match that token. Duplicate approved-study dispatch uses
`run_full_study:<action_id>` as its stable idempotency key. The legacy
auto-promoter path uses event and ticker identity.

Full-alert composition is also idempotent at the side-effect boundary. Its
analysis pack is created before the brief becomes deliverable, and a retry of
the same queue job returns the existing deterministic brief without rerunning
analysis, synthesis, or channel fan-out.

An operator may grant one terminal job one additional attempt:

```bash
tradingagents forge orchestrator retry JOB_ID
```

## Alert-delivery lifecycle

```text
brief transaction -> queued -> running -> sent
                                 |
                                 +-> queued (transport retry)
                                 +-> dead   (configuration error or exhaustion)
```

Each queue row stores the rendered channel body and the structured brief
payload needed for Telegram keyboards. `deliveries` records every actual
attempt and its provider reference, failure, or configuration skip.

Run the worker and inspect it with:

```bash
tradingagents forge delivery worker
tradingagents forge delivery status
tradingagents forge delivery retry DELIVERY_JOB_ID
```

The worker must run as a continuously restarted Docker Compose service in the
canonical deployment. Compose wiring and health checks land in the deployment
batch; until then the foreground command is the exact process entry point.

## Operator queries

Queue state summary:

```bash
sqlite3 /absolute/path/to/iic.db \
  "SELECT state, count(*) FROM queue_jobs GROUP BY state;"
sqlite3 /absolute/path/to/iic.db \
  "SELECT state, count(*) FROM delivery_queue GROUP BY state;"
```

Dead deliveries with their last attempt:

```bash
sqlite3 -header -column /absolute/path/to/iic.db \
  "SELECT delivery_job_id, brief_id, channel, attempt_count, max_attempts, available_ts, last_error FROM delivery_queue WHERE state='dead' ORDER BY delivery_job_id;"
```

Stale running leases should normally be empty:

```bash
sqlite3 -header -column /absolute/path/to/iic.db \
  "SELECT job_id, lease_expires_ts FROM queue_jobs WHERE state='running' AND datetime(lease_expires_ts) <= datetime('now');"
sqlite3 -header -column /absolute/path/to/iic.db \
  "SELECT delivery_job_id, lease_expires_ts FROM delivery_queue WHERE state='running' AND datetime(lease_expires_ts) <= datetime('now');"
```

## Verification requirements

- clean bootstrap records schema versions 1 and 2;
- a version-1 database receives one verified `pre-v0002` backup;
- duplicate enqueue returns the original job and changed content is rejected;
- a retry after full-alert commit reuses one brief and one intent per channel;
- two workers cannot lease the same row;
- stale tokens cannot complete recovered work;
- retry delay and terminal exhaustion are deterministic;
- a quiet-hours alert becomes eligible at 07:00 Beijing;
- transport failure is audited, retried, then sent or dead-lettered;
- a forced alert-outbox failure rolls back the brief, actions, suppression, and
  every channel intent;
- package distributions contain both immutable migrations;
- the full isolated repository suite passes;
- live Telegram and SMTP field delivery is explicitly reported if skipped.

## Verification result

- Full repository suite: `945 passed, 2 skipped, 2 warnings, 78 subtests
  passed` in 87.90 seconds.
- Queue and migration coverage: all 20 migration tests and all 13 dedicated
  Batch 3 queue, delivery-worker, and delivery-CLI tests passed as part of the
  full suite.
- Ruff passed for every changed Python file.
- Targeted mypy passed for the six new or substantially changed queue/runtime
  modules with `--follow-imports=skip`. The existing repository-wide type debt
  remains outside this batch.
- `compileall`, `git diff --check`, the tracked-secret pattern scan, and
  `uv lock --check` passed. The lock contains 202 packages.
- The wheel and source distribution each passed the release-content verifier
  with 21 required runtime resources.
- A no-dependencies wheel install was loaded from outside the repository using
  the locked project runtime. It created and reopened a clean database with
  migrations `(1, baseline)` and `(2, queue_lifecycle)`, `integrity_check=ok`,
  no foreign-key violations, and a successfully leased delivery row with a
  fencing token.

The two warnings come from optional packages already present in the workstation
Conda environment: `numexpr 2.8.7` is older than pandas' recommended `2.10.2`,
and `bottleneck 1.3.7` is older than the recommended `1.4.2`. Neither package
is in IIC-Forge's locked production runtime, so these are workstation warnings,
not Batch 3 release red flags.

## Skipped checks and exact field procedures

### Live DeepSeek integrations

The following automated tests were skipped because no real exported
`DEEPSEEK_API_KEY` was provided:

1. `tests/smoke/test_f1_exit_gate.py::test_f1_exit_gate_deepdive_aapl`
2. `tests/test_deepseek_reasoning.py::TestDeepSeekLiveStructuredOutput::test_v4_flash_returns_structured_output`

Run them on-field from the repository root:

```bash
export DEEPSEEK_API_KEY='operator-live-test-key'
python -m pytest -q -m integration --allow-external-network
unset DEEPSEEK_API_KEY
```

### Production-container clean-volume smoke

Docker, Podman, and BuildKit are unavailable on this workstation, so the image
build and clean-volume queue bootstrap were skipped. Run this on the Docker
host from the repository root:

```bash
docker build --tag iic-forge:batch3 .
docker volume create iic-forge-batch3-smoke
docker run --rm --user root --entrypoint chown \
  --volume iic-forge-batch3-smoke:/data \
  iic-forge:batch3 1000:1000 /data
docker run --rm --entrypoint python \
  --volume iic-forge-batch3-smoke:/data \
  iic-forge:batch3 \
  -c "from tradingagents.persistence.db import connect; c=connect('/data/iic.db'); print(c.execute('SELECT version,name FROM schema_migrations ORDER BY version').fetchall()); print(c.execute('PRAGMA integrity_check').fetchone()); print(c.execute('PRAGMA foreign_key_check').fetchall()); print(c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name IN ('queue_jobs','delivery_queue') ORDER BY name\").fetchall()); c.close()"
docker run --rm --entrypoint python \
  --volume iic-forge-batch3-smoke:/data \
  iic-forge:batch3 \
  -c "from tradingagents.persistence.db import connect; c=connect('/data/iic.db'); print(c.execute('SELECT count(*) FROM schema_migrations').fetchone()); c.close()"
docker volume rm iic-forge-batch3-smoke
```

Require versions 1 and 2, `integrity_check=ok`, no foreign-key violations,
both queue tables, and a migration count of 2 after reopen.

### Live Telegram, SMTP, quiet-boundary, and outage recovery

These checks were skipped because credentials were not provided and they cause
real external messages or intentional network disruption. Execute the four
procedures below against a disposable field database and private test
recipients. The deterministic quiet-boundary and outage-retry equivalents did
pass in the automated suite.

### Hosted GitHub Actions

Hosted CI was not run because Batch 3 is still uncommitted and unpushed. After
an approved commit and push, require the blocking `test` and `image-build` jobs
to pass on that exact commit before deployment.

Old-database adoption is not a skipped test. Pre-production data is disposable
under the approved production contract.

## Field tests

### Telegram delivery and approval keyboard

This sends a real test message to the configured private operator chat.

```bash
export IIC_TELEGRAM_BOT_TOKEN='operator-test-bot-token'
export TELEGRAM_BOT_ALLOWED_CHAT_IDS='operator-test-chat-id'
export TRADINGAGENTS_IIC_DB_PATH='/absolute/path/to/field-test.db'
tradingagents forge delivery worker
```

In a second terminal, start the callback process so approval buttons are also
tested:

```bash
export IIC_TELEGRAM_BOT_TOKEN='operator-test-bot-token'
export TELEGRAM_BOT_ALLOWED_CHAT_IDS='operator-test-chat-id'
export TRADINGAGENTS_IIC_DB_PATH='/absolute/path/to/field-test.db'
python -m tradingagents.delivery.telegram_bot
```

In a third terminal, create an alert through the normal synthetic F4/F5 field
procedure or approved event path, then require:

```bash
tradingagents forge delivery status
sqlite3 "$TRADINGAGENTS_IIC_DB_PATH" \
  "SELECT q.state,d.status,d.channel_ref FROM delivery_queue q LEFT JOIN deliveries d ON d.delivery_id=q.last_delivery_id WHERE q.channel='telegram' ORDER BY q.delivery_job_id DESC LIMIT 1;"
```

The row must be `sent`, the Telegram message must appear once under normal
operation, and its approval buttons must resolve to the correct brief.

### Email delivery

This sends a real email through the operator SMTP account.

```bash
export IIC_SMTP_USER='operator-smtp-user'
export IIC_SMTP_APP_PASSWORD='operator-smtp-app-password'
export IIC_SMTP_ENABLED='true'
export IIC_SMTP_HOST='smtp.gmail.com'
export IIC_SMTP_PORT='587'
export IIC_SMTP_FROM_ADDR='operator@gmail.com'
export IIC_SMTP_TO_ADDRS='private-test-recipient@example.com'
export TRADINGAGENTS_IIC_DB_PATH='/absolute/path/to/field-test.db'
tradingagents forge delivery worker
```

Enqueue a synthetic event alert through the normal path, and require the newest
email outbox row and attempt row to be `sent` with a non-empty Message-ID.

### Quiet-hours release

On a disposable field database, enqueue one synthetic alert between 22:00 and
07:00 Beijing. Confirm `attempt_count=0`, `state='queued'`, and `available_ts`
equals the next 07:00 Beijing boundary converted to UTC. Keep the delivery
worker running and confirm it sends after the boundary without manual action.

### Transport-outage retry

Temporarily block the disposable field worker's SMTP or Telegram egress. Enqueue
one synthetic alert, require a `failed` attempt and a future `available_ts`,
restore egress, and confirm the same `delivery_job_id` reaches `sent`. Do not
perform this test against the production database or production recipient.
