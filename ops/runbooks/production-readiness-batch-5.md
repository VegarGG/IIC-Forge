# Production readiness: Batch 5 delivery outbox

Batch 5 closes the production alert-delivery gate for the private,
single-operator deployment. Every `event_alert` and `event_alert_light`
Telegram/email intent is committed to SQLite before any network attempt.
Quiet hours use `Asia/Shanghai`; transport outages never discard an intent.

Atomic ingestion is still outstanding and must be completed before the Docker
Compose batch. The combined USD20 Beijing-day LLM budget is also a later batch.

## Delivery contract

- The Secretary only enqueues alerts. The delivery worker alone performs
  Telegram and SMTP network I/O.
- The logical idempotency key is `brief:<brief_id>:channel:<channel>` and is
  unique. Re-enqueueing identical content returns the same job; changed content
  under the same key is rejected.
- One queue lease can be held by only one worker. Every state update is fenced
  by the opaque lease token.
- Transient failures use capped exponential backoff. The default sequence is
  30, 60, 120, 240, then 480 seconds, capped at 1800 seconds if the attempt
  count is raised.
- Exhausted transient failures become `dead`. Permanent configuration,
  credential, sender, or recipient failures become `blocked` immediately.
- A blocked or dead job cannot run again without an operator note. Active and
  sent jobs cannot be cancelled because the provider may already have accepted
  them.
- Queue recovery is at-least-once. SQLite guarantees one logical queue row and
  prevents simultaneous worker execution. Telegram and SMTP do not expose a
  common transactional acknowledgement, so a process killed after provider
  acceptance but before the SQLite acknowledgement can produce a duplicate.
  Email attempts reuse a stable `Message-ID` to help receivers deduplicate.
- Alert quiet hours are 22:00 inclusive through 07:00 exclusive Beijing Time.
  Alert intents remain `queued` with `available_ts` set to 07:00; the quiet-hour
  recheck refunds the lease attempt instead of consuming a retry.

## Schema version 4

Migration `0004_delivery_outbox_controls.sql` rebuilds `delivery_queue` to add:

| Field | Purpose |
|---|---|
| `idempotency_key` | stable logical-delivery identity |
| `error_category` | machine-readable failure classification |
| `blocked_ts` | time permanent operator intervention became necessary |
| `cancelled_ts` | time an inactive intent was cancelled |
| `operator_note` | most recent replay/cancellation explanation |

It widens the lifecycle constraint to `queued`, `running`, `sent`, `dead`,
`blocked`, and `cancelled`. `delivery_queue_events` is the append-only audit
stream for enqueue, deferral, retry, block, recovery, send, replay, and cancel
transitions.

A schema-v3 database receives one verified `pre-v0004` backup before the
transactional upgrade. The preproduction database is disposable by project
decision, but the migration is still tested so packaged deployments fail safe.

## Lifecycle

```text
enqueue -> queued -> running -> sent
              ^         |
              |         +-> queued   transient failure, attempts remain
              |         +-> dead     transient failure, attempts exhausted
              |         +-> blocked  permanent configuration/recipient failure
              |                         |
              +--- retry dead + note ---+
              +--- requeue blocked + note

queued/blocked/dead -> cancelled + note
running/sent        -> cancellation refused
```

Default classifications:

| Category | State | Operator response |
|---|---|---|
| `channel_disabled` | blocked | enable the intended transport, verify config, requeue |
| `credential_missing` | blocked | install the missing secret, requeue |
| `credential_rejected` | blocked | rotate/test the credential, requeue |
| `recipient_missing` | blocked | configure the private recipient, requeue |
| `recipient_invalid` / `recipient_rejected` | blocked | correct and verify the mailbox/chat, requeue |
| `telegram_rejected` / `smtp_rejected` | blocked | inspect provider rejection, correct, requeue |
| `unknown_channel` | blocked | correct the producer or deploy the channel handler |
| `transport_error` | retry, then dead | inspect network/provider availability |
| `worker_runtime` | retry, then dead | inspect worker logs and malformed queue content |
| `lease_expired` | retry, then dead | confirm predecessor death and single-worker deployment |

## Content and action safety

- Telegram transport escapes the complete rendered message as MarkdownV2.
  Templates contain plain text, so generated brackets, links, underscores,
  backticks, and punctuation cannot alter formatting or create injected links.
  Escaped messages are safely truncated to the Bot API 4096-character limit.
- Email templates use Jinja autoescaping. The SMTP boundary additionally strips
  scripts, styles, iframes, SVG, event attributes, inline CSS, and unsafe URL
  schemes with a strict HTML allow-list.
- Email sender and recipient mailboxes reject control characters and malformed
  addresses before SMTP connection.
- Telegram callbacks only accept known action types and valid button values.
  Pending actions are expired at callback time, and the response transition is
  conditional on both `state='pending'` and `expires_at > responded_at`.
  Expired or already-used buttons cannot create replacement actions.

## Operator commands

Show counts and the ten newest intents:

```bash
iic-forge forge delivery status
```

Inspect one full intent and its lifecycle events:

```bash
iic-forge forge delivery inspect JOB_ID
```

After a transient outage is fixed, give a `dead` job one more attempt:

```bash
iic-forge forge delivery retry JOB_ID \
  --note "SMTP outage resolved and connection verified"
```

After correcting a permanent configuration problem, replay a `blocked` job:

```bash
iic-forge forge delivery requeue JOB_ID \
  --note "Telegram recipient configured and verified with test bot"
```

Cancel an inactive intent:

```bash
iic-forge forge delivery cancel JOB_ID \
  --note "synthetic validation alert no longer required"
```

Do not change queue state directly with SQLite in production. Direct SQL below
is read-only diagnostics only:

```bash
sqlite3 -header -column /absolute/path/to/iic.db \
  "SELECT delivery_job_id,idempotency_key,state,attempt_count,max_attempts,error_category,available_ts,blocked_ts,last_error FROM delivery_queue ORDER BY delivery_job_id;"
```

## Automated gate

Batch 5 requires:

- clean bootstrap and v3-to-v4 verified migration backup;
- stable enqueue identity and rejection of changed content;
- 22:00-07:00 Beijing scheduling with no discarded or consumed attempt;
- one lease across concurrent workers;
- capped retry, attempt exhaustion, blocked classification, and lease recovery;
- restart/resume from the same SQLite queue;
- audited retry, requeue, inspect, and cancel controls;
- refusal to cancel running or sent jobs;
- safe Telegram MarkdownV2 and length handling;
- autoescaped and sanitized email HTML;
- stable email `Message-ID` across attempts;
- expired callback refusal without replacement action creation;
- full repository tests and source/wheel resource verification.

## Docker field-test preparation

Batch 6 will make Compose canonical. Until then, test the exact Batch 5 image
directly. Build it and create a disposable local volume:

```bash
docker build --tag iic-forge:batch5 .
docker volume create iic-forge-batch5-field
docker run --rm --user root --entrypoint chown \
  --volume iic-forge-batch5-field:/data \
  iic-forge:batch5 1000:1000 /data
```

Create `.env.batch5-field` outside source control, set permissions to `0600`,
and add only the channel under test. Never paste secrets into command history.

Telegram variables:

```text
IIC_TELEGRAM_BOT_TOKEN=<real test bot token>
TELEGRAM_BOT_ALLOWED_CHAT_IDS=<private operator chat id>
```

Email variables:

```text
IIC_SMTP_ENABLED=true
IIC_SMTP_HOST=<real SMTP host>
IIC_SMTP_PORT=587
IIC_SMTP_USER=<real SMTP user>
IIC_SMTP_APP_PASSWORD=<real app password>
IIC_SMTP_FROM_ADDR=<verified sender>
IIC_SMTP_TO_ADDRS=<private operator mailbox>
```

## Live Telegram field test

Run between 07:00 and 22:00 Beijing Time, or use `--ready-now` exactly as below
to test transport separately from quiet-hour scheduling:

```bash
docker run --rm --env-file .env.batch5-field \
  --volume iic-forge-batch5-field:/data --entrypoint python \
  iic-forge:batch5 -m tradingagents.delivery.field_probe enqueue \
  --db /data/telegram.db --channel telegram --ready-now
```

Record the returned `delivery_job_id`, then attempt exactly one delivery:

```bash
docker run --rm --env-file .env.batch5-field \
  --volume iic-forge-batch5-field:/data --entrypoint python \
  iic-forge:batch5 -m tradingagents.delivery.field_probe drain-one \
  --db /data/telegram.db
```

Require `state="sent"`, one `deliveries.status="sent"`, and one private chat
message. The literal text `[PROBE] _Markdown_!` must appear without becoming an
injected link or italic text. The inline keyboard must be present.

To test callback expiry/acceptance, start the polling bot against the same DB:

```bash
docker run --name iic-forge-batch5-telegram-bot \
  --env-file .env.batch5-field \
  --env TRADINGAGENTS_IIC_DB_PATH=/data/telegram.db \
  --volume iic-forge-batch5-field:/data --entrypoint python \
  iic-forge:batch5 -m tradingagents.delivery.telegram_bot
```

Click `Run Backtest` once. Stop the bot with `docker stop`, then inspect:

```bash
docker stop iic-forge-batch5-telegram-bot
docker rm iic-forge-batch5-telegram-bot
docker run --rm --volume iic-forge-batch5-field:/data --entrypoint python \
  iic-forge:batch5 -m tradingagents.delivery.field_probe inspect \
  --db /data/telegram.db --job-id JOB_ID
```

Require exactly one `run_backtest` action in `accepted`. For the expiry test,
seed a second alert, leave the button untouched until its configured TTL has
passed, run the polling bot, click once, and require `expired` plus the Telegram
answer `Expired or already handled`; no second action row may appear.

## Live email field test

```bash
docker run --rm --env-file .env.batch5-field \
  --volume iic-forge-batch5-field:/data --entrypoint python \
  iic-forge:batch5 -m tradingagents.delivery.field_probe enqueue \
  --db /data/email.db --channel email --ready-now
docker run --rm --env-file .env.batch5-field \
  --volume iic-forge-batch5-field:/data --entrypoint python \
  iic-forge:batch5 -m tradingagents.delivery.field_probe drain-one \
  --db /data/email.db
```

Require `state="sent"`, one message in the private mailbox, correct sender and
subject, no remote/active content, and a `Message-ID` beginning with `iic-`.
Inspect the job with the field probe and retain the JSON as evidence.

## Outage, restart, and blocked-recovery field tests

For a transient SMTP outage, use a second protected env file containing valid
dummy login values but `IIC_SMTP_HOST=127.0.0.1` and `IIC_SMTP_PORT=9`. Enqueue
an email probe with `--ready-now`, then run `drain-one` with that env file.
Require `state="queued"`, `attempt_count=1`,
`error_category="transport_error"`, and a future `available_ts`. Stop all test containers.
After the backoff time, run `drain-one` in a new container with the real SMTP
env file. Require the same `delivery_job_id`, `attempt_count=2`, and `sent`.

For permanent blocking, enqueue a Telegram probe and run `drain-one` with the
allowed chat configured but without `IIC_TELEGRAM_BOT_TOKEN`. Require
`state="blocked"` and `error_category="credential_missing"`. Restore the token,
then run:

```bash
docker run --rm --env-file .env.batch5-field \
  --env TRADINGAGENTS_IIC_DB_PATH=/data/blocked.db \
  --volume iic-forge-batch5-field:/data \
  iic-forge:batch5 forge delivery requeue JOB_ID \
  --note "test bot token restored and verified"
```

Run `drain-one` in a new container and require `sent`. Inspect events and
require `blocked`, `operator_requeue`, then `sent` in that order.

## Real quiet-hours field test

Between 22:00 and 07:00 Beijing Time, enqueue without `--ready-now`, then run
`drain-one`. Require `state="queued"`, `attempt_count=0`, no delivery attempt,
and `available_ts` equal to the next 07:00 Beijing boundary converted to UTC.
After 07:00, run `drain-one` in a new container and require the same queue row
to become `sent` with `attempt_count=1`.

## Cleanup

After retaining redacted evidence, remove the disposable local volume and the
protected field env file:

```bash
docker volume rm iic-forge-batch5-field
```

Delete `.env.batch5-field` with the operator's normal secure-file procedure.
