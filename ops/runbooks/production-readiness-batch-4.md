# Production readiness: Batch 4 process-isolated analysis worker

Batch 4 closes the durable analysis-worker gate. Each leased analysis job runs
in a separate, terminable process while the long-lived parent exclusively owns
the queue lease and all queue lifecycle updates.

Batch 3 pulled the lease, retry, idempotency, and delivery-outbox foundations
forward from the original plan. This batch therefore concentrates on hard
process termination, parent reconciliation, permanent blocking, process audit
metadata, and operator-controlled replay. Atomic ingestion remains a separate
pending batch; it is not silently treated as complete here. The combined USD20
Beijing-day LLM budget is also outside this batch and must not be represented
as enforced by the legacy disabled UTC-day guard.

## Production contract

- The supported deployment runs exactly one analysis-worker parent with one
  in-flight analysis child (`max_concurrent_jobs=1`).
- Every queue completion and failure must match the opaque active lease token.
  There is no unfenced completion API.
- The parent leases work and is the only process allowed to mutate the
  `queue_jobs` lifecycle fields.
- The child opens its own SQLite connection, builds its own LLM/Secretary
  clients, performs the job, closes SQLite, and only then reports success.
- The parent acknowledges success only after receiving a valid result envelope
  and confirming that the child has exited or has been terminated.
- A job exceeding `worker_job_timeout_min` receives `SIGTERM`; after
  `worker_process_terminate_grace_seconds`, it receives `SIGKILL`. On POSIX,
  the parent targets the child's dedicated process group.
- In the Linux Docker target, the child arms `PR_SET_PDEATHSIG=SIGKILL`. A
  worker killed by SIGKILL or OOM therefore cannot leave its direct analysis
  child running and committing after the parent disappears.
- A graceful worker shutdown terminates the child before immediately
  re-queueing the fenced attempt.
- Because this is a single-worker deployment, every `running` job found during
  worker boot belongs to a dead predecessor and is reclaimed immediately.
- Retryable failures use bounded exponential backoff and become `error` after
  `max_attempts`. Invalid payloads, missing events, and unsupported job types
  become `blocked` immediately and do not burn retries indefinitely.
- Replaying an `error` or `blocked` job requires an operator note. The note and
  prior error remain in SQLite for audit.
- Enqueue remains idempotent: the same idempotency key and same content returns
  one job; reusing a key for changed content is rejected.

## Schema version 3

Migration `0003_analysis_worker_process.sql` adds:

| Field | Purpose |
|---|---|
| `error_category` | stable failure class such as `timeout`, `child_exit`, or `invalid_payload` |
| `operator_note` | reason supplied when a terminal job is manually replayed |
| `worker_pid` | PID of the currently registered child; cleared on every terminal/requeue transition |
| `last_exit_code` | most recent child exit status or terminating signal |
| `blocked_ts` | time a non-retryable job entered `blocked` |

The partial `idx_queue_jobs_blocked` index supports the operator's permanent
failure view. A schema-v2 database receives one verified `pre-v0003` backup and
is upgraded transactionally. Clean databases apply versions 1, 2, and 3 in one
bootstrap transaction.

## Job lifecycle

```text
                         transient failure
                        +-------------------+
                        v                   |
enqueue -> queued -> running -> done        |
              ^          |                  |
              |          +-> queued --------+
              |          +-> error  (attempts exhausted)
              |          +-> blocked (operator correction required)
              |                         |
              +----- manual retry + note+
```

Failure categories and default disposition:

| Category | Disposition | Operator action |
|---|---|---|
| `invalid_payload` | `blocked` | repair the producer/data mapping, then replay with a note |
| `missing_event` | `blocked` | restore/re-ingest the event or discard the job |
| `unknown_job_type` | `blocked` | deploy the matching handler or discard the job |
| `timeout` | retry, then `error` | inspect slow provider/analysis stage; increase timeout only with evidence |
| `worker_shutdown` | immediate retry | none after normal restart |
| `process_start` | retry, then `error` | inspect OS/container process limits and logs |
| `child_exit` | retry, then `error` | inspect `last_exit_code`, OOM state, and container logs |
| `runtime_error` | retry, then `error` | inspect provider, analysis, and SQLite error details |
| `database_close` | retry, then `error` | run SQLite integrity checks and inspect filesystem health |
| `lease_expired` | retry, then `error` | confirm predecessor death; investigate overlapping workers if unexpected |

## Operator commands

Inspect queue state, PID, category, and the most recent error or replay note:

```bash
tradingagents forge orchestrator status
```

Inspect terminal jobs in full:

```bash
sqlite3 -header -column /absolute/path/to/iic.db \
  "SELECT job_id,job_type,state,attempt_count,max_attempts,error_category,last_exit_code,blocked_ts,operator_note,error FROM queue_jobs WHERE state IN ('blocked','error') ORDER BY job_id;"
```

Replay only after correcting the cause:

```bash
tradingagents forge orchestrator retry JOB_ID \
  --note "corrected RSS event mapping and verified source event"
```

Confirm no active lease is past its expiry:

```bash
sqlite3 -header -column /absolute/path/to/iic.db \
  "SELECT job_id,worker_pid,lease_expires_ts FROM queue_jobs WHERE state='running' AND datetime(lease_expires_ts)<=datetime('now');"
```

## Automated verification requirements

- process success is parent-acknowledged and clears the recorded PID;
- timeout kills the real spawned child before the job is made retryable;
- graceful shutdown kills the child and makes the job immediately available;
- a child exiting with status 17 becomes retryable with `child_exit` and the
  exact exit code;
- invalid work becomes `blocked` and cannot be leased again without a noted
  operator replay;
- stale tokens cannot complete or register a PID after lease recovery;
- a stale lease clears its PID and records `lease_expired`;
- retry backoff and attempt exhaustion remain bounded;
- duplicate idempotent enqueue creates exactly one job;
- schema-v2 to v3 upgrade creates and verifies one `pre-v0003` backup;
- fresh schema, wheel, and source distribution contain all three immutable
  migrations;
- the full isolated repository suite passes;
- Docker/Linux forced-death and live LLM checks are reported explicitly when
  they cannot be run locally.

## Credential-free Docker field tests

These procedures require Docker because Linux `PR_SET_PDEATHSIG`, PID namespace
behavior, image contents, and container stop semantics cannot be proven by the
macOS unit suite.

Build the exact candidate:

```bash
docker build --tag iic-forge:batch4 .
docker volume create iic-forge-batch4-faults
docker run --rm --user root --entrypoint chown \
  --volume iic-forge-batch4-faults:/data \
  iic-forge:batch4 1000:1000 /data
```

### Hard timeout

Mount only the credential-free probe from the checked-out candidate:

```bash
docker run --rm --entrypoint python --workdir /src \
  --volume "$PWD:/src:ro" \
  --volume iic-forge-batch4-faults:/data \
  iic-forge:batch4 \
  -m scripts.batch4_process_fault_probe timeout \
  --db /data/timeout.db --pid-file /data/timeout.pid
```

Require JSON containing `state="queued"`, `error_category="timeout"`,
`worker_pid=null`, `child_alive=false`, and a non-null `last_exit_code`.

### SIGKILL parent and restart reclamation

Start a parent with a deliberately hanging child:

```bash
docker run --name iic-forge-batch4-kill --entrypoint python --workdir /src \
  --volume "$PWD:/src:ro" \
  --volume iic-forge-batch4-faults:/data \
  iic-forge:batch4 \
  -m scripts.batch4_process_fault_probe hold \
  --db /data/kill.db --pid-file /data/kill.pid
```

After the log says `probe worker ready`, use a second shell:

```bash
docker top iic-forge-batch4-kill
docker kill --signal=KILL iic-forge-batch4-kill
docker wait iic-forge-batch4-kill
docker rm iic-forge-batch4-kill
docker run --rm --entrypoint python --workdir /src \
  --volume "$PWD:/src:ro" \
  --volume iic-forge-batch4-faults:/data \
  iic-forge:batch4 \
  -m scripts.batch4_process_fault_probe recover --db /data/kill.db
```

Require the killed container exit status to reflect SIGKILL and recovery JSON
containing `recovered=1`, `state="queued"`, `worker_pid=null`, and
`error_category="lease_expired"`. Starting the normal worker against that
database must lease attempt 2, never create a second queue row, and never
accept an acknowledgement carrying attempt 1's token.

Clean up only after capturing the evidence:

```bash
docker volume rm iic-forge-batch4-faults
```

## Live analysis field test

This procedure spends provider credit and must use a disposable event/job plus
a provider-side account limit below USD20. The production combined USD20
Beijing-day enforcement is not part of Batch 4, so do not rely on the legacy
disabled UTC-day application guard. Export the real DeepSeek credential only
in the field shell, start the foreground worker, approve one synthetic full
study, and require one completed queue row with one deterministic brief:

```bash
export DEEPSEEK_API_KEY='operator-live-test-key'
export TRADINGAGENTS_IIC_DB_PATH='/absolute/path/to/batch4-live.db'
tradingagents forge orchestrator worker
```

In a second shell, monitor:

```bash
tradingagents forge orchestrator status
sqlite3 -header -column "$TRADINGAGENTS_IIC_DB_PATH" \
  "SELECT job_id,state,attempt_count,worker_pid,last_exit_code,brief_id,cost_usd,error_category FROM queue_jobs ORDER BY job_id DESC LIMIT 1;"
```

Require `done`, `worker_pid` cleared, `last_exit_code=0`, one brief identity,
and provider-side spend below USD20. Then unset the credential:

```bash
unset DEEPSEEK_API_KEY
```

## Verification result

- Full repository suite: `957 passed, 2 skipped, 2 warnings, 78 subtests
  passed` in 86.14 seconds. The first sandboxed attempt denied loopback socket
  binding to 26 local HTTP-stub tests; the identical suite passed after local
  loopback permission was granted. This was an execution-sandbox restriction,
  not a product failure.
- The final Batch 4 queue/process/migration/CLI group passed 70 tests before
  the full-suite run. It includes real spawned-process timeout, shutdown,
  exit-17 crash, invalid result envelope, blocked replay, immediate boot
  reclamation, stale-token/PID fencing, fail-closed unkillable-child handling,
  and schema-v2 upgrade coverage.
- The standalone credential-free timeout probe passed on macOS: the child PID
  was absent before requeue, with `state=queued`, `error_category=timeout`,
  `worker_pid=null`, and `last_exit_code=-15`.
- Ruff passed for every changed Python file. Targeted mypy passed for the five
  changed runtime/CLI/probe modules with `--follow-imports=skip`.
- `compileall`, `git diff --check`, and the tracked high-entropy secret/private
  key scan passed.
- `uv lock --check` resolved the existing lock at 202 packages without change.
- The wheel and source distribution each passed the release-content verifier
  with 22 required runtime resources, including all three migrations.
- A no-dependencies wheel install was imported from outside the repository. It
  bootstrapped versions `(1, baseline)`, `(2, queue_lifecycle)`, and
  `(3, analysis_worker_process)`, returned `integrity_check=ok`, had no foreign
  key violations, and exercised PID registration followed by a fenced
  transition to `blocked`.

The two warnings come from optional packages in the workstation Conda
environment: pandas recommends `numexpr>=2.10.2` but finds 2.8.7, and recommends
`bottleneck>=1.4.2` but finds 1.3.7. Neither package is in IIC-Forge's locked
production runtime. They are not Batch 4 red flags.

## Skipped checks and exact field procedures

### Live DeepSeek tests

The two automated tests below were skipped because no real exported
`DEEPSEEK_API_KEY` was provided; the test fixture's placeholder key is
intentionally rejected:

1. `tests/smoke/test_f1_exit_gate.py::test_f1_exit_gate_deepdive_aapl`
2. `tests/test_deepseek_reasoning.py::TestDeepSeekLiveStructuredOutput::test_v4_flash_returns_structured_output`

Run them on-field with the provider-side account limit below USD20:

```bash
export DEEPSEEK_API_KEY='operator-live-test-key'
python -m pytest -q -m integration --allow-external-network
unset DEEPSEEK_API_KEY
```

Then execute the single-job live analysis procedure in the preceding section.

### Docker/Linux process-death and image checks

The Docker image build, Linux `PR_SET_PDEATHSIG`, forced-parent-SIGKILL, restart
reclamation, and clean-volume schema bootstrap were skipped because the
`docker` command is not installed on this workstation. These are field gates,
not emulatable macOS assertions. Run both credential-free Docker procedures
above, then confirm the clean volume records schema versions 1, 2, and 3:

```bash
docker run --rm --entrypoint python \
  --volume iic-forge-batch4-faults:/data \
  iic-forge:batch4 \
  -c "from tradingagents.persistence.db import connect; c=connect('/data/clean.db'); print(c.execute('SELECT version,name FROM schema_migrations ORDER BY version').fetchall()); print(c.execute('PRAGMA integrity_check').fetchone()); print(c.execute('PRAGMA foreign_key_check').fetchall()); c.close()"
```

Require all three versions, `integrity_check=ok`, and no foreign-key failures.

### Live Telegram and SMTP delivery

Live transport delivery was not repeated because Batch 4 does not modify the
Batch 3 delivery outbox and no real recipients or credentials were supplied.
Before deployment, repeat the Telegram, SMTP, quiet-boundary, and outage
recovery procedures in `production-readiness-batch-3.md` against private test
recipients.

### Hosted GitHub Actions

Hosted CI was not run because Batch 4 remains uncommitted and unpushed. After
an approved commit and push, require the blocking `test` and `image-build` jobs
to pass on that exact commit before deployment.

Old pre-production database adoption is not a skipped test. The approved
contract treats all existing test data as disposable. Atomic ingestion and the
combined USD20 Beijing-day budget remain explicitly scheduled work outside
Batch 4 rather than hidden omissions from this gate.
