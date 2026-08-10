# Production Readiness Batch 10 — Release Candidate Hardening

Batch 10 turns the earlier production controls into blocking release evidence.
It does not declare a release ready merely because unit tests pass. A candidate
is releasable only after CI, the disposable fault drill, a measured full-volume
restore, live private-channel checks, and the 72-hour Compose soak all pass for
the same commit and image digest.

## Production contract

- Scope remains the private, single-operator IIC-Forge Compose stack.
- Enabled ingestion connectors remain RSS, Telegram, and Polygon only.
- Telegram and email remain the only production delivery channels.
- Every alert remains durable in the delivery outbox, including Beijing quiet
  hours and transport outages.
- The combined paid-LLM ceiling remains USD20 per Beijing calendar day.
- Local encrypted backup remains the approved recovery scope. Loss of both the
  production host and its local backup storage is an accepted risk.
- No Batch 10 procedure silently deletes production volumes. Destructive fault
  injection is restricted to names beginning `iic-forge-batch10-drill` and an
  exact confirmation phrase.

## Blocking hosted gates

The `CI` workflow has four blocking jobs:

1. `Tests and build`
   - verifies `uv.lock`;
   - runs the complete hermetic suite;
   - builds wheel and sdist twice with one `SOURCE_DATE_EPOCH`;
   - requires byte-for-byte identical artifacts;
   - verifies packaged resources and an out-of-tree installed wheel.
2. `Blocking quality ratchet`
   - pins Ruff 0.6.9 and mypy 1.11.2;
   - permits the recorded 193 Ruff and 67 mypy findings only by normalized
     file/code/message fingerprint and multiplicity;
   - fails on any new or increased finding;
   - allows the baseline only to shrink.
3. `Dependencies, secrets, and Python SBOM`
   - exports the exact frozen production dependency graph;
   - runs `pip-audit` with no vulnerability allowlist;
   - runs repository secret and configuration scanning;
   - generates a deterministic CycloneDX 1.6 Python-environment SBOM.
4. `Production image, Compose, scan, and manifest`
   - builds the production image from digest-pinned Python and uv bases;
   - smoke-tests the installed image and embedded model;
   - performs clean-volume Compose initialization;
   - generates a complete image CycloneDX SBOM;
   - fails on any high or critical image vulnerability;
   - publishes a release manifest containing the Git tree, image ID, schema
     version, input hashes, artifact hashes, and SBOM/security-report hashes.

The Trivy action is pinned to the full verified v0.36.0 commit rather than a
mutable version tag. The scanner binary is also version-pinned. Do not loosen
`exit-code`, severity, or secret-scanning settings to make a candidate green.

## Quality ratchet maintenance

Run the blocking local check with the versions recorded in the baseline:

```bash
python scripts/quality_ratchet.py check --ruff ruff --mypy mypy
```

When an inherited finding is fixed, the check remains green and reports the
reduction. Update the baseline only in a dedicated reviewed quality change:

```bash
python scripts/quality_ratchet.py update --ruff ruff --mypy mypy
git diff -- quality/quality-baseline.json
```

An analyzer upgrade and a baseline change must be reviewed together. Never
increase a count or add a fingerprint merely to suppress a new error.

## Immutable release artifact

The application build inputs are frozen by `uv.lock`, digest-pinned base
images, a commit-pinned embedding model, and the release commit. Redis defaults
to the official multi-platform `7.4.9-alpine3.21` index digest.

Build with commit-derived labels:

```bash
export IIC_VCS_REF="$(git rev-parse HEAD)"
export IIC_BUILD_DATE="$(git show -s --format=%cI HEAD)"
docker compose build --pull
docker image inspect iic-forge:0.2.5 \
  --format '{{.Id}} {{json .Config.Labels}}'
```

For a registry-backed deployment, push once and deploy the immutable returned
digest, never the tag:

```bash
docker tag iic-forge:0.2.5 REGISTRY/IIC-FORGE:0.2.5
docker push REGISTRY/IIC-FORGE:0.2.5
docker image inspect REGISTRY/IIC-FORGE:0.2.5 \
  --format '{{index .RepoDigests 0}}'
export IIC_IMAGE='REGISTRY/IIC-FORGE@sha256:FULL_DIGEST'
```

If no registry is used, create a local frozen archive and record both hashes:

```bash
docker image save iic-forge:0.2.5 -o iic-forge-0.2.5.docker.tar
sha256sum iic-forge-0.2.5.docker.tar uv.lock
docker image inspect iic-forge:0.2.5 --format '{{.Id}}'
```

The archive is a release artifact, not a backup of production data.

## Disposable automated fault drill

The drill destroys only explicitly named disposable volumes. Choose a unique
suffix and use no production credentials:

```bash
export IIC_COMPOSE_PROJECT_NAME=iic-forge-batch10-drill-01
export IIC_DATA_VOLUME=iic-forge-batch10-drill-01-data
export IIC_REDIS_VOLUME=iic-forge-batch10-drill-01-redis
export IIC_FAULT_DRILL_CONFIRM='DESTROY IIC-FORGE BATCH10 DRILL'
./ops/fault-drill.sh
unset IIC_COMPOSE_PROJECT_NAME IIC_DATA_VOLUME IIC_REDIS_VOLUME
unset IIC_FAULT_DRILL_CONFIRM
```

The default trap removes the disposable stack and volumes. Set
`IIC_KEEP_DRILL=true` only when retaining a failed stack for diagnosis, then
resolve and inspect all three names before manual cleanup.

Acceptance requires every stage to pass:

- Redis canary survives AOF fsync and Redis restart;
- SQLite canary survives container restart/reopen;
- timed-out analysis child is dead before the job is requeued;
- SIGKILL-abandoned lease is reclaimed once and fenced;
- the 21st USD1 reservation is refused at the USD20 Beijing-day limit;
- a 23:00 Beijing alert remains queued until 07:00;
- missing Telegram credentials produce durable `blocked/credential_missing`;
- encrypted data/Redis backup verifies;
- isolated restore drill passes and records timing/checksum evidence.

This credential-free drill does not replace live provider/channel tests.

## Credentialed network and clock fault gates

Run these only against a second disposable Compose project with private test
recipients and protected test credentials:

1. Send one real Telegram and one real email using the Batch 5 field probe;
   require one received message and one `sent` row per channel.
2. Block Telegram egress, enqueue an alert, and require retry scheduling without
   loss. Restore egress and require the same logical delivery to reach `sent`.
3. Point SMTP at a closed disposable endpoint, require bounded retry/dead or
   blocked state, correct configuration, then use the audited replay command.
4. Interrupt RSS, Telegram ingestion, Polygon, DeepSeek, and market-data egress
   separately. Require connector/operational status to identify the fault and
   no committed cursor/job/delivery to disappear.
5. During 22:00–07:00 Beijing time, enqueue a real alert. Require both channel
   rows to remain queued and release after 07:00. Confirm the private recipient
   sees no early message.
6. Reserve the remaining Beijing-day budget with disposable calls. Require all
   roles, retries, and fallback paths to defer before USD20, then verify normal
   operation after Beijing midnight without altering ledger history.
7. Restart each long-running service and then the full stack. Compare queue,
   cursor, delivery, and Redis-stream counts before and after.

Never use the production recipient, database, volumes, or paid budget for
failure injection.

## Full-volume restore and measured RTO

The automated fault drill performs a real isolated archive restore, but the
release gate also requires the Batch 8 disposable full-volume drill. Follow
`production-readiness-batch-8.md` using names beginning
`iic-forge-batch10-drill`, record:

- commit and image digest;
- Compose project/data/Redis volume names;
- archive name, size, and ciphertext SHA-256;
- backup elapsed seconds;
- restore elapsed seconds;
- post-restore SQLite integrity and foreign-key results;
- post-restore Redis AOF state;
- restored canary and service health.

RTO passes below 14,400 seconds. RPO requires 25 hours of observed 50-minute
backup scheduling with no interval over one hour. Because storage is local,
these results make no host-loss recovery claim.

## 72-hour production-like soak

Use the final candidate image, the intended production host, private test
recipients, and the normal RSS/Telegram/Polygon workload. First complete a
current backup and strict operator preflight. Then:

```bash
export IIC_SOAK_CONFIRM='RUN IIC-FORGE 72H SOAK'
export IIC_SOAK_SECONDS=259200
export IIC_SOAK_INTERVAL_SECONDS=300
systemd-inhibit --what=sleep:idle --why='IIC-Forge release soak' \
  ./ops/soak.sh
```

The evidence file contains only service/container metadata and the bounded
operator snapshot; it excludes queue payloads, source text, message bodies,
credentials, and backup contents. The final evaluator requires:

- at least 72 hours of evidence and no gap over 15 minutes;
- every expected long-running service present and running;
- no unhealthy sample and no restart-count increase;
- no analysis `error/blocked` or delivery `dead/blocked` state;
- no USD20 budget breach;
- median last-quarter active queue depth no more than 25 above the first;
- unchanged image identity and complete memory evidence in every sample;
- median last-quarter memory no more than 256 MiB above the first per service;
- data-volume growth no more than 5 GiB.

The queue, disk, and memory thresholds are release defaults for this private
single-operator workload. Tighten them when observed normal traffic supports
it. Any need to loosen them requires documented capacity evidence and a
reviewed code change; do not override them ad hoc during a gate run.

## Final go/no-go

Use the four checklists in `ops/checklists/`. Release only when all refer to the
same commit and image digest and every checkbox has attached evidence.

No-go conditions include:

- any unresolved P0/P1 correctness or security issue;
- any lost committed work, unbounded retry/restart loop, or unfenced late
  completion;
- any failed dependency, secret, high/critical image, quality, test, Compose,
  live-channel, restore, or soak gate;
- RPO over one hour or RTO over four hours;
- a mutable image reference or release manifest produced with tracked source
  changes outside its recorded commit.

The inherited Ruff/mypy baseline is accepted technical debt, not a claim of
zero debt. The narrow at-least-once provider acknowledgement window and local
backup host-loss limitation remain documented accepted risks; neither permits
silent message loss or misleading recovery claims.

## Rollback compatibility

Batch 10 adds no database migration. The prior Batch 9 application can read
schema version 6, and Redis 7.4.9 remains within the tested 7.4 line. Prefer an
application-image rollback without volume replacement. Restore a pre-release
backup only if persistent state is corrupt or an incompatible future migration
has occurred. Never delete migration rows or volumes to force a rollback.
