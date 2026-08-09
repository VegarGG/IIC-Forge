# Production readiness: Batch 6 canonical Docker Compose

Batch 6 makes Docker Compose the only supported production process topology
for the private, single-operator IIC-Forge deployment. It also closes the
ingestion crash window that Batch 5 explicitly left outstanding.

## Production contract

- The only ingestion connectors started are RSS, Telegram, and Polygon.
- Redis is reachable only on the private Compose network; no host port is
  published.
- Redis uses AOF, `noeviction`, and a named local volume.
- Before advancing a SQLite source cursor, every adapter:
  1. writes the raw payload as a private file;
  2. flushes the file and containing directory;
  3. appends the envelope to Redis;
  4. requires Redis 7.2+ `WAITAOF 1 0` to confirm a local AOF fsync;
  5. commits the new source cursor.
- A Redis/AOF failure never advances the source cursor. A crash after the AOF
  fsync but before the cursor commit can replay an envelope; it cannot lose it.
- Triage commits the event, fingerprints, vector, event-ticker links, and
  watchlist changes in one SQLite transaction. A failed transaction preserves
  the staging payload and leaves the Redis entry pending for retry.
- Triage repeats hash/external-id and semantic dedupe checks while holding the
  SQLite write lock. Concurrent consumers cannot both publish the same event
  as an original.
- Triage deletes an acknowledged Redis stream entry after its durable SQLite
  commit. A trim failure is logged but cannot undo or replay the committed
  event.
- Database initialization and migration verification finish successfully
  before any application process starts.
- Polygon/crypto ticker seeding finishes before triage or orchestration starts.
- Exactly one analysis worker is deployed. The scheduler never performs
  analysis itself; it only enqueues idempotent work for that worker.
- At 07:00 `Asia/Shanghai`, the scheduler enqueues exactly one deterministic
  morning-digest job per Beijing calendar date. Restarts cannot create a
  second logical digest for the same date.
- Every event alert and morning digest continues through the durable
  Telegram/email outbox from Batch 5. The brief and its two delivery intents
  commit in one SQLite transaction. Quiet hours remain 22:00-07:00 in
  `Asia/Shanghai`; the 07:00 digest is immediately eligible.
- Application containers run as UID/GID 1000, use a read-only root filesystem,
  drop Linux capabilities, set `no-new-privileges`, rotate logs, and persist
  only `/data`.
- Secrets are read from `/run/secrets` by the image entry point. Secret values
  are not present in the Compose environment or committed configuration.
- The two named volumes are local. Off-host backup storage remains outside the
  approved scope; local backup implementation is Batch 8.

## Service topology

| Service | Function | Persistent dependency |
|---|---|---|
| `redis` | durable ingestion stream and dedupe cache | `iic-forge-redis` |
| `volume-init` | one-shot ownership repair for UID 1000 | `iic-forge-data` |
| `database-init` | config preflight, migrations, integrity checks | `iic-forge-data` |
| `ticker-seed` | Polygon equities plus bundled crypto universe | SQLite |
| `scheduler` | Beijing 07:00 digest enqueue and hourly watchlist expiry | SQLite |
| `sense-rss` | RSS polling and durable envelope publication | Redis + SQLite cursor |
| `sense-telegram` | Telegram channel ingestion | Redis + SQLite cursor/session |
| `sense-polygon` | Polygon news polling | Redis + SQLite cursor |
| `triage` | dedupe, scoring, validation, atomic event persistence | Redis + SQLite |
| `promoter` | approval-gated alert promotion | SQLite |
| `analysis-worker` | one fenced, killable analysis worker | SQLite + `/data` |
| `delivery-worker` | durable Telegram/email outbox delivery | SQLite |
| `telegram-bot` | private callbacks and replies | SQLite |
| `action-handler` | accepted-action dispatch and expiry | SQLite |

There are no published ports. Every long-running service waits for healthy
Redis and successful completion of both initialization one-shots.

## Host prerequisites

Require all of the following before field deployment:

- Linux host with enough memory for the configured 8 GiB analysis-worker cap
  plus the 3 GiB triage cap;
- Docker Engine and Docker Compose v2 with support for long-form
  `depends_on.condition`;
- outbound HTTPS, Telegram, Polygon, LLM-provider, and SMTP connectivity;
- a private Telegram ingestion account already joined to every configured
  sensing channel;
- a private Telegram bot and operator chat id;
- an SMTP account with a provider-specific app password;
- local disk capacity for both named volumes and image/model layers.

Check the host:

```bash
docker version
docker compose version
docker info --format '{{json .SecurityOptions}}'
df -h .
```

## Configuration and secrets

From the repository root:

```bash
cp .env.production.example .env.production
chmod 0600 .env.production
install -d -m 0700 secrets
```

Edit `.env.production` with real non-secret feed, channel, chat, sender, and
recipient values. Create the seven files documented in `secrets/README.md`:

```bash
install -m 0600 /dev/null secrets/deepseek_api_key
install -m 0600 /dev/null secrets/polygon_api_key
install -m 0600 /dev/null secrets/telegram_api_id
install -m 0600 /dev/null secrets/telegram_api_hash
install -m 0600 /dev/null secrets/telegram_bot_token
install -m 0600 /dev/null secrets/smtp_user
install -m 0600 /dev/null secrets/smtp_app_password
```

Populate them with a protected editor. Do not use shell command-line arguments
that place values in history. Confirm only permissions and non-empty sizes:

```bash
find secrets -type f ! -name README.md -exec stat -c '%a %s %n' {} \;
```

Every secret must show mode `600` and a size greater than zero.

## Build and static Compose validation

```bash
docker compose config --quiet
docker compose build --pull
docker image inspect iic-forge:0.2.5 \
  --format '{{.Config.User}} {{json .Config.Entrypoint}}'
```

Require image user `appuser` or UID 1000 and entry point
`/app/docker-entrypoint.sh`. Do not deploy a mutable source bind mount.

## First clean-volume bootstrap

The old/pre-production database is disposable by project decision. Start with
the default empty named volumes:

```bash
docker compose up -d redis volume-init database-init ticker-seed
docker compose ps --all
docker compose logs --no-color database-init ticker-seed
```

Require:

- `redis` is healthy;
- `volume-init`, `database-init`, and `ticker-seed` exited `0`;
- database output reports migrations 1 through 4, `integrity=ok`, and zero
  foreign-key violations;
- ticker seeding reports both crypto and Polygon rows.

Inspect without installing SQLite on the host:

```bash
docker compose run --rm --no-deps database-init \
  forge runtime health --database --redis
docker compose run --rm --no-deps database-init \
  forge watchlist list
```

## Telegram ingestion session authorization

Do this once after initialization and before the full stack starts:

```bash
docker compose run --rm sense-telegram
```

Complete Telegram login/2FA in the attached terminal. After the adapter logs
that it started, press `Ctrl+C`. Confirm session files exist in the local data
volume without printing their contents:

```bash
docker compose run --rm --no-deps --entrypoint /bin/sh sense-telegram \
  -c 'find /data/telegram -maxdepth 1 -type f -printf "%m %s %f\n"'
```

The session must remain inside `/data/telegram`; never copy it into the image
or Git repository.

## Start and verify the canonical stack

```bash
docker compose up -d
docker compose ps
docker compose logs --no-color --since 10m \
  scheduler sense-rss sense-telegram sense-polygon triage promoter \
  analysis-worker delivery-worker telegram-bot action-handler
```

Require all long-running services to be `Up` and `healthy`, with no restart
loop, missing-config message, migration failure, or Redis AOF error.

Dependency and security checks:

```bash
docker compose exec redis redis-cli INFO persistence
docker compose exec redis redis-cli CONFIG GET maxmemory-policy
docker compose exec redis redis-cli CONFIG GET appendonly
docker compose ps --format json
docker port "$(docker compose ps -q redis)"
```

Require `aof_enabled:1`, `aof_last_write_status:ok`, `noeviction`,
`appendonly yes`, and no Redis host-port output.

## Disposable ingestion durability field test

Use separate volume names so this procedure cannot touch production data:

```bash
export IIC_COMPOSE_PROJECT_NAME=iic-forge-batch6-field
export IIC_DATA_VOLUME=iic-forge-batch6-field-data
export IIC_REDIS_VOLUME=iic-forge-batch6-field-redis
docker compose up -d redis volume-init database-init
```

First prove the success path advances the cursor only after `WAITAOF`:

```bash
docker compose run --rm --no-deps --entrypoint python database-init - <<'PY'
import asyncio
from datetime import datetime, timezone
from tradingagents.default_config import DEFAULT_CONFIG as C
from tradingagents.persistence.db import connect
from tradingagents.sensing.adapters.base import EnvelopeWriter
from tradingagents.sensing.envelope import Envelope
from tradingagents.sensing.redis_client import make_redis

async def main():
    conn = connect(C["iic_db_path"])
    redis = make_redis(C["sensing_redis_url"])
    writer = EnvelopeWriter(
        source="batch6_probe", redis=redis, conn=conn,
        stream=C["sensing_ingest_stream"],
        staging_root=f'{C["iic_data_dir"]}/events/staging',
        require_aof_fsync=True,
    )
    env = Envelope(
        source="rss", ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="batch6-probe-success", text="batch6 durable ingestion probe",
        source_tags={}, raw_path="",
    )
    await writer.write(env, raw_payload={"probe": True}, cursor="success")
    row = conn.execute(
        "SELECT cursor FROM ingest_cursor WHERE source='batch6_probe'"
    ).fetchone()
    print({"cursor": row[0], "stream_length": await redis.xlen(C["sensing_ingest_stream"])})
    await redis.aclose()
    conn.close()

asyncio.run(main())
PY
```

Require `cursor=success` and `stream_length=1`.

Then prove an AOF refusal does not advance a new cursor:

```bash
docker compose exec redis redis-cli CONFIG SET appendonly no
docker compose run --rm --no-deps --entrypoint python database-init - <<'PY'
import asyncio
from datetime import datetime, timezone
from tradingagents.default_config import DEFAULT_CONFIG as C
from tradingagents.persistence.db import connect
from tradingagents.sensing.adapters.base import EnvelopeWriter
from tradingagents.sensing.envelope import Envelope
from tradingagents.sensing.redis_client import make_redis

async def main():
    conn = connect(C["iic_db_path"])
    redis = make_redis(C["sensing_redis_url"])
    writer = EnvelopeWriter(
        source="batch6_probe_failure", redis=redis, conn=conn,
        stream=C["sensing_ingest_stream"],
        staging_root=f'{C["iic_data_dir"]}/events/staging',
        require_aof_fsync=True, aof_fsync_timeout_ms=1000,
    )
    env = Envelope(
        source="rss", ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="batch6-probe-failure", text="must not advance cursor",
        source_tags={}, raw_path="",
    )
    try:
        await writer.write(env, raw_payload={"probe": True}, cursor="forbidden")
    except Exception as exc:
        print(type(exc).__name__, str(exc))
    row = conn.execute(
        "SELECT cursor FROM ingest_cursor WHERE source='batch6_probe_failure'"
    ).fetchone()
    print({"cursor": None if row is None else row[0]})
    await redis.aclose()
    conn.close()

asyncio.run(main())
PY
```

Require an error and `cursor=None`. Destroy the disposable volumes:

```bash
docker compose down --volumes --remove-orphans
unset IIC_COMPOSE_PROJECT_NAME IIC_DATA_VOLUME IIC_REDIS_VOLUME
```

## Restart and persistence field test

Against production, record queue and stream counts, restart every process, and
verify the volumes and queue state remain:

```bash
docker compose run --rm --no-deps database-init forge runtime health
docker compose exec redis redis-cli XLEN ingest:raw
docker compose exec delivery-worker iic-forge forge delivery status
docker compose restart sense-rss sense-telegram sense-polygon triage promoter \
  scheduler analysis-worker delivery-worker telegram-bot action-handler
docker compose ps
docker compose run --rm --no-deps database-init forge runtime health
docker compose exec delivery-worker iic-forge forge delivery status
```

No queued or blocked delivery may disappear. A job running during restart may
return to `queued` through its existing fenced recovery path.

## Scheduled morning-digest field gate

Leave the complete stack running across 07:00 Beijing time. Before the
boundary, record the date and queue state:

```bash
docker compose exec scheduler date -Iseconds
docker compose exec analysis-worker iic-forge forge orchestrator status
docker compose exec delivery-worker iic-forge forge delivery status
```

After 07:00, require exactly one new `morning_digest` analysis job. Wait for it
to reach `done`, then require exactly two `morning_digest` delivery rows: one
Telegram and one email. Both must reach `sent`, and the operator must receive
both messages. Restart `scheduler` twice and confirm counts remain unchanged:

```bash
docker compose logs --no-color --since 30m scheduler analysis-worker delivery-worker
docker compose exec analysis-worker iic-forge forge orchestrator status
docker compose exec delivery-worker iic-forge forge delivery status
docker compose restart scheduler
docker compose restart scheduler
docker compose exec analysis-worker iic-forge forge orchestrator status
docker compose exec delivery-worker iic-forge forge delivery status
```

The `morning_digest` job count for that Beijing date and its delivery-row count
must remain one and two respectively. This is an on-field test because it
requires a real clock boundary, LLM analysis, and private Telegram/SMTP
recipients.

## Live Telegram and email gate

Use private test recipients and the Batch 5 probe inside the canonical image:

```bash
docker compose run --rm --no-deps --entrypoint python delivery-worker \
  -m tradingagents.delivery.field_probe enqueue \
  --db /data/iic.db --channel telegram --ready-now
docker compose run --rm --no-deps --entrypoint python delivery-worker \
  -m tradingagents.delivery.field_probe drain-one --db /data/iic.db

docker compose run --rm --no-deps --entrypoint python delivery-worker \
  -m tradingagents.delivery.field_probe enqueue \
  --db /data/iic.db --channel email --ready-now
docker compose run --rm --no-deps --entrypoint python delivery-worker \
  -m tradingagents.delivery.field_probe drain-one --db /data/iic.db
```

Require one real private Telegram message, one real private email, `sent` queue
states, and corresponding append-only delivery audit rows. Repeat the quiet
boundary and outage procedures from the Batch 5 runbook; Compose is now the
process supervisor for those tests.

## Normal operations

```bash
docker compose ps
docker compose logs --since 30m SERVICE
docker compose exec delivery-worker iic-forge forge delivery status
docker compose exec delivery-worker iic-forge forge delivery inspect JOB_ID
docker compose exec analysis-worker iic-forge forge orchestrator status
docker compose stop
docker compose start
```

Do not use `docker compose down --volumes` in production. That command deletes
the local data and Redis volumes and is reserved for explicitly disposable
field stacks.

## Upgrade and rollback

For an upgrade, build the candidate image, stop writers, and start through the
same one-shot gates:

```bash
docker compose build --pull
docker compose stop sense-rss sense-telegram sense-polygon triage promoter \
  scheduler analysis-worker delivery-worker telegram-bot action-handler
docker compose up -d database-init ticker-seed
docker compose up -d
docker compose ps
```

Batch 6 adds no database migration, so the Batch 5 image remains schema
compatible. To roll back this batch, stop the Batch 6 stack and redeploy the
previous approved image/process definition without deleting either named
volume. If any integrity or migration check fails, keep all writers stopped
and follow the offline restore procedure in the Batch 2 runbook.

## Automated gate

Batch 6 is complete only when all of the following pass:

- durable raw-file write and AOF-before-cursor ordering;
- cursor refusal when AOF fsync is not confirmed;
- atomic triage rollback with staging preservation;
- late dedupe recheck within the transaction;
- idempotent Beijing-date morning scheduling through the single analysis
  worker and atomic Telegram/email digest enqueue;
- exact Compose service and connector contract;
- no published Redis port;
- non-root/read-only/capability-restricted application services;
- secret-file entry point contract;
- clean runtime initialization and private file permissions;
- focused and full repository tests;
- Ruff, scoped mypy, compile, lock, distribution, and installed-wheel gates;
- Docker image build, Compose config, clean-volume initialization, service
  health, restart persistence, and live channel checks where field facilities
  are available.

Every skipped automated or field check must be reported with its reason and
the exact on-field command or procedure from this runbook.

The Streamlit dashboard is intentionally not part of the Batch 6 Compose
topology. Its authenticated/private operator surface and host binding are
Batch 9 operator-controls work; the current dashboard remains a development
command only until that gate is complete.
