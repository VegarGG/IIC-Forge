# Production readiness: Batch 8 encrypted local recovery

Batch 8 replaces the inherited host-specific copy script with authenticated,
Compose-aware recovery points for both persistent volumes. Backup storage
remains local by project decision. This batch does not claim recovery from
loss of the host, its backup disk, or the only encryption key.

## Production contract

- Recovery targets apply while the local backup filesystem and encryption key
  survive: recovery point objective (RPO) at most one hour and recovery time
  objective (RTO) at most four hours.
- One backup operation captures the complete `iic-data` volume and the Redis
  multi-part AOF volume. SQLite, raw and quarantined input, reports, run
  artifacts, Telegram session state, queued work, delivery intents, cursors,
  and budget rows therefore travel together.
- The host wrapper serializes backup and restore with `flock`, cleanly stops
  the Compose stack, creates the recovery point, and restarts the stack even
  when backup creation fails. No writer is permitted during capture.
- SQLite is opened read-only after every writer has stopped and must pass
  `integrity_check`, `foreign_key_check`, and migration inspection before
  archiving. Its database, WAL, and shared-memory files are captured without
  the backup container mutating their ownership or contents.
- Redis must contain its `appendonly.aof.manifest` after clean shutdown. Every
  AOF file and manifest is hashed as part of the encrypted archive.
- Backups use a random 256-bit key and streaming AES-256-GCM. Plaintext is
  never written to the backup filesystem. The small `latest.json` marker and
  SHA-256 sidecars contain only operational metadata, not application data.
- Every archive is decrypted and authenticated immediately after creation;
  every member hash is compared with the encrypted manifest and the extracted
  SQLite payload is verified again before the backup is published as latest.
- Source symbolic links, device files, sockets, unknown archive paths,
  duplicate members, missing members, wrong keys, modified ciphertext, and
  invalid SQLite or Redis state fail closed.
- Restore authenticates the selected archive before stopping production,
  creates a separate encrypted `pre-restore` backup without pruning, stages
  both volumes, re-verifies them, and swaps them into place. If the second
  volume swap or final validation fails, the first volume is rolled back.
- Retention keeps every recovery point for 48 hours, the newest point per day
  for 14 days, and the newest point per ISO week for eight weeks. The newest
  backup is always retained. Only recognized `.iicbak` files and their
  sidecars may be pruned.
- Backup/restore containers use the application image, have no network, use a
  read-only root filesystem, receive only the backup key secret, and exist
  behind the inactive `operations` Compose profile. The restore service alone
  receives ownership-changing capability.

## Local-only limitation

The default bind directory is `./backups`, but production should use a second
local filesystem such as `/srv/iic-forge-backups`. This protects against loss
of a Docker volume, not loss of the whole host. Off-host copies, cloud object
storage, remote key escrow, and geographic disaster recovery remain outside
scope.

Keep the encryption key outside `iic-data`, `iic-redis`, and the backup
directory. If the only key is lost, all backups are unrecoverable. If the host
and local backup disk are both lost, the approved local-only design has no
recovery path.

## Key and directory provisioning

From the repository root on the production host:

```bash
install -d -m 0700 secrets
umask 077
openssl rand -base64 32 > secrets/backup_encryption_key
chmod 0600 secrets/backup_encryption_key

sudo install -d -m 0700 -o "$(id -un)" -g "$(id -gn)" \
  /srv/iic-forge-backups
```

Do not print or commit the key. Confirm its decoded length without printing
the value:

```bash
python - <<'PY'
import base64
from pathlib import Path
value = base64.b64decode(
    b"".join(Path("secrets/backup_encryption_key").read_bytes().split()),
    validate=True,
)
assert len(value) == 32
print("backup key: valid 256-bit value")
PY
```

Set the local backup path in the operator shell or host service environment:

```bash
export IIC_BACKUP_DIR=/srv/iic-forge-backups
```

Compose receives the absolute path only for bind-mount interpolation. It does
not place the encryption key in an environment variable or `docker inspect`.
The operations container decrypts only the SQLite payload into an in-memory
`/tmp` for verification. Its default scratch ceiling is 4 GiB. Before release,
confirm `iic.db` is comfortably smaller; otherwise export a host-level value
such as `IIC_BACKUP_SCRATCH_SIZE=8g` based on measured database size and
available RAM. This variable, like `IIC_BACKUP_DIR`, is Compose interpolation
and does not belong in `.env.production`.

## Build and automated gates

```bash
python -m compileall -q cli tradingagents scripts
ruff check tradingagents/backup tests/backup tests/ops/test_backup_controls.py
mypy --follow-imports=skip tradingagents/backup/archive.py
pytest -q
env UV_CACHE_DIR=/tmp/iic-forge-batch8-uv-cache uv lock --check
docker compose --profile operations config --quiet
docker compose build --pull
```

Require a green full suite, a frozen lock, both operations services behind the
  profile, no network on either service, read-only data and Redis source mounts
  for backup, and no backup key on ordinary application services.

## First verified backup

With the normal production stack healthy:

```bash
export IIC_BACKUP_DIR=/srv/iic-forge-backups
time ./ops/backup.sh
docker compose ps --all
```

The wrapper briefly stops the stack. Require all long-running services to
return healthy, then inspect metadata only:

```bash
stat -c '%a %s %n' "$IIC_BACKUP_DIR" \
  "$IIC_BACKUP_DIR"/latest.json \
  "$IIC_BACKUP_DIR"/*.iicbak \
  "$IIC_BACKUP_DIR"/*.iicbak.sha256

docker compose --profile operations run --rm --no-deps backup-create \
  forge backup status --output-root /backups --max-age-minutes 60
```

Require the directory to be mode `0700`, files to be mode `0600`, status
`current`, and backup age at most 60 minutes. Never run `strings`, `tar`, or
other plaintext inspection against the encrypted file.

Perform an explicit full authentication check at least weekly:

```bash
archive=$(python - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ["IIC_BACKUP_DIR"])
print(json.loads((root / "latest.json").read_text())["archive"])
PY
)
docker compose --profile operations run --rm --no-deps backup-create \
  forge backup verify "/backups/$archive" \
  --key-file /run/secrets/backup_encryption_key
```

Require `status=verified`, the expected file count, and migrations 1 through
5. The command does not contact an external service.

## RPO-safe host schedule

Use a host systemd timer rather than a container loop. The timer must run as
the private operator account that can access Docker. A 50-minute interval
leaves margin for service stop and timer accuracy inside the one-hour RPO.
Create a service with the real repository and backup paths substituted:

```ini
# /etc/systemd/system/iic-forge-backup.service
[Unit]
Description=IIC-Forge authenticated local backup
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
User=IIC_OPERATOR
Group=IIC_OPERATOR_GROUP
WorkingDirectory=/opt/iic-forge
Environment=IIC_BACKUP_DIR=/srv/iic-forge-backups
ExecStart=/opt/iic-forge/ops/backup.sh
ExecStopPost=/usr/bin/docker compose up -d
TimeoutStartSec=45min
```

`ExecStopPost` is a second restart fence if the wrapper is terminated by
systemd after it stopped the stack. It relies on the declared
`WorkingDirectory`.

```ini
# /etc/systemd/system/iic-forge-backup.timer
[Unit]
Description=Hourly IIC-Forge local recovery point

[Timer]
OnBootSec=5min
OnUnitActiveSec=50min
AccuracySec=1min
RandomizedDelaySec=0
Unit=iic-forge-backup.service

[Install]
WantedBy=timers.target
```

Install and verify:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now iic-forge-backup.timer
systemctl list-timers iic-forge-backup.timer
sudo systemctl start iic-forge-backup.service
systemctl status iic-forge-backup.service --no-pager
journalctl -u iic-forge-backup.service -n 100 --no-pager
```

Observe at least 25 consecutive hours before release. There must be no gap
greater than 55 minutes between `created_utc` values while the host is
operational, and status must never exceed its 60-minute ceiling. `OnBootSec`
creates a fresh point shortly after host startup; the normal Compose deployment
should also be enabled to start at boot.

## Disposable full restore drill

Never perform the first restore drill against production volumes. Use distinct
Compose and volume names plus a separate local backup directory:

```bash
export IIC_COMPOSE_PROJECT_NAME=iic-forge-batch8-drill
export IIC_DATA_VOLUME=iic-forge-batch8-drill-data
export IIC_REDIS_VOLUME=iic-forge-batch8-drill-redis
export IIC_BACKUP_DIR=/srv/iic-forge-backups/batch8-drill
install -d -m 0700 "$IIC_BACKUP_DIR"

docker compose up -d redis volume-init database-init ticker-seed
docker compose up -d
```

Create a durable canary, allow Redis to fsync it, and capture a backup:

```bash
docker compose run --rm --no-deps --entrypoint python database-init - <<'PY'
from tradingagents.default_config import DEFAULT_CONFIG as C
from tradingagents.persistence.db import connect
conn = connect(C["iic_db_path"])
conn.execute(
    "INSERT OR REPLACE INTO watchlist "
    "(ticker, added_ts, ttl_until, tags) VALUES "
    "('BATCH8', '2026-08-09T00:00:00+00:00', NULL, '[]')"
)
conn.commit()
print(conn.execute(
    "SELECT ticker FROM watchlist WHERE ticker='BATCH8'"
).fetchone()[0])
PY

./ops/backup.sh
archive=$(python - <<'PY'
import json, os
from pathlib import Path
root = Path(os.environ["IIC_BACKUP_DIR"])
print(json.loads((root / "latest.json").read_text())["archive"])
PY
)
```

Mutate the state after the recovery point:

```bash
docker compose run --rm --no-deps --entrypoint python database-init - <<'PY'
from tradingagents.default_config import DEFAULT_CONFIG as C
from tradingagents.persistence.db import connect
conn = connect(C["iic_db_path"])
conn.execute("DELETE FROM watchlist WHERE ticker='BATCH8'")
conn.commit()
assert conn.execute(
    "SELECT ticker FROM watchlist WHERE ticker='BATCH8'"
).fetchone() is None
PY
```

Measure the guarded restore:

```bash
SECONDS=0
./ops/restore.sh "$archive" \
  --confirm RESTORE_IIC_FORGE_LOCAL_BACKUP
echo "restore_seconds=$SECONDS"
```

Require `restore_seconds` below 14,400, all services healthy, and the canary
restored:

```bash
docker compose ps
docker compose run --rm --no-deps --entrypoint python database-init - <<'PY'
from tradingagents.default_config import DEFAULT_CONFIG as C
from tradingagents.persistence.db import connect
conn = connect(C["iic_db_path"])
assert conn.execute(
    "SELECT ticker FROM watchlist WHERE ticker='BATCH8'"
).fetchone()[0] == "BATCH8"
print("Batch 8 restore canary: present")
PY
```

Confirm that a newer `pre-restore` encrypted archive exists. This is the
rollback point for the state that the restore replaced.

## Failure drills

Run these only with the disposable project and volumes:

1. Copy an archive and its sidecar, flip one byte in the copy, then run
   `forge backup verify`. Require failure before either volume changes.
2. Generate a disposable wrong key and verify the intact archive with it.
   Require `backup authentication failed` and no mutation.
3. Start restore with any confirmation other than
   `RESTORE_IIC_FORGE_LOCAL_BACKUP`. Require refusal before the stack stops.
4. Hold the operation lock with `flock` and start another backup. Require exit
   code 75 without stopping the stack.
5. Stop the backup service midway with `systemctl kill`. Require
   `ExecStopPost` and the wrapper restart fence to return the stack. A SIGKILL
   of both fences is outside normal operation; if it occurs, immediately run
   `docker compose up -d` and treat the missed backup as an incident.
6. Fill a disposable backup filesystem until capacity preflight fails. Require
   no published `latest.json` change and a restarted healthy stack.

Do not deliberately damage production backup media or use the production
encryption key in a disposable test artifact.

## Cleanup of the disposable drill

Resolve the exact project and volume names before deletion, then run:

```bash
docker compose down --volumes --remove-orphans
unset IIC_COMPOSE_PROJECT_NAME IIC_DATA_VOLUME IIC_REDIS_VOLUME IIC_BACKUP_DIR
```

Remove only the explicitly named `/srv/iic-forge-backups/batch8-drill`
directory after saving the non-sensitive timing and pass/fail evidence. Never
use `docker compose down --volumes` with production volume names.

## Release gate and locally skipped checks

Automated tests cover encryption, immediate verification, ciphertext and
wrong-key rejection, restore confirmation, staging, cross-volume rollback,
symlink rejection, Redis-manifest enforcement, age monitoring, retention,
Compose isolation, and wrapper contracts.

The following require the Linux field host and must be reported as skipped
when Docker or real volumes are unavailable:

- image build and both profiled backup containers;
- a real stopped-stack data plus Redis AOF recovery point;
- 25 hours of 50-minute timer/RPO evidence;
- a measured disposable full-volume restore proving RTO below four hours;
- service restart after interruption, capacity exhaustion, and host reboot;
- operator confirmation that the encryption key and backup directory are not
  located inside either protected volume.

For every skipped field gate, record timestamp, commit and image digest,
Compose project and volume names, archive size/checksum, elapsed backup and
restore time, observed service health, and pass/fail. Never record the key,
raw event content, Telegram session files, or decrypted archive members.
