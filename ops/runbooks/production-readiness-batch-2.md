# Production readiness: Batch 2 database lifecycle

Batch 2 establishes a clean production bootstrap, immutable schema versions,
transactional future upgrades, and verified local migration recovery points.
It does not import or adopt any database created before this migration
framework. All earlier IIC-Forge database contents are test data and are
outside the production cutover.

Queue, ingestion, delivery, Compose, scheduled retention, and business-logic
schema changes remain outside Batch 2.

## Database state contract

`connect()` recognizes exactly three database states:

1. **Empty**: no application schema objects exist. All packaged migrations are
   applied in order in one transaction.
2. **Versioned**: `schema_migrations` exists and contains an unbroken history.
   The history and runtime schema are verified; pending migrations are backed
   up and applied transactionally.
3. **Non-empty and unversioned**: application objects exist without valid
   migration metadata. Startup fails without enabling WAL, creating migration
   metadata, backing up, adopting, or deleting the database.

The application never decides that an unversioned database "looks close
enough" to production. The operator must point production at an empty path or
empty Docker volume.

## Schema version contract

- Packaged migrations live under `tradingagents.persistence.migrations`.
- Filenames are immutable and ordered as `NNNN_name.sql`.
- Versions are contiguous from `0001`.
- Every executable SQL statement ends with a semicolon.
- SQLite's statement-completeness parser handles triggers and semicolons in
  string literals.
- The SHA-256 digest of each SQL file is recorded in `schema_migrations`.
- A changed name or checksum for an applied version is a startup failure.
- A gap in applied history is a startup failure.
- A database newer than the running application is a startup failure.
- The runtime schema is compared with the schema produced by all packaged
  migrations, including tables, views, triggers, indexes, columns, and
  sqlite-vec objects.

Migration `0001_baseline.sql` is the clean IIC-Forge production baseline. It
is not an adapter for databases created by earlier code.

## Fresh database behavior

For an empty database, `connect()`:

1. Loads sqlite-vec and configures foreign keys and the busy timeout.
2. Takes an immediate SQLite write lock.
3. Rechecks that the database is still empty.
4. Creates `schema_migrations`.
5. Applies every packaged migration in order.
6. Creates the sqlite-vec index required by migration `0001`.
7. Records each migration name, checksum, UTC application time, and
   application version.
8. Verifies schema shape, `PRAGMA integrity_check`, and
   `PRAGMA foreign_key_check`.
9. Commits the schema and migration history together.
10. Enables WAL only after state validation and successful migration.

Concurrent first connections serialize on the SQLite write lock. Later
connections observe the recorded migration instead of rerunning it. No backup
is created because a new database has no prior state to protect.

If initialization fails, SQLite rolls back the schema and migration records.
An empty database file may remain and can be retried safely.

## Non-empty unversioned behavior

Opening a non-empty database without `schema_migrations` raises
`MigrationPreflightError` with a clean-bootstrap instruction. The application:

- does not alter its journal mode;
- does not create migration metadata;
- does not inspect it for legacy compatibility;
- does not create a migration backup;
- does not delete or replace it.

This fail-closed behavior prevents an accidental production path from silently
reusing a test database.

## Future versioned upgrade behavior

When a database created by this framework has pending migrations, `connect()`:

1. Takes the SQLite write lock and re-reads migration history.
2. Verifies history names, checksums, ordering, and supported version.
3. Runs SQLite integrity and foreign-key checks.
4. Creates and verifies one local recovery point while retaining the write
   lock, preventing another migrator from racing the upgrade.
5. Applies pending SQL and its version rows in the same transaction.
6. Verifies the complete expected schema and database health.
7. Commits only after all checks pass.

If another process waited for the same upgrade, it observes the completed
version after receiving the lock and does not create a second recovery point.

## Migration recovery-point contract

Before changing a non-empty versioned database, the framework:

1. Requires free space equal to at least twice the database/WAL/SHM footprint,
   with a minimum of 10 MiB.
2. Uses SQLite's online backup API to copy the last committed state.
3. Reopens the copy and verifies integrity and foreign keys.
4. Writes a SHA-256 sidecar.
5. Stores the directory as mode `0700` and the backup and sidecar as mode
   `0600`.

The default location is `migration-backups/` beside the SQLite database. These
files are short-term local migration recovery points. They are not the
encrypted scheduled backup and retention system planned for Batch 8, and
Batch 2 does not delete them automatically.

## Upgrade failure and rollback

If migration SQL or post-migration validation fails:

- SQLite rolls back every statement in that upgrade transaction;
- migration history remains at the previous version;
- partially created schema objects are absent;
- the verified pre-upgrade recovery point remains available.

`restore_database()` performs an offline restore. It verifies the backup
checksum and database health, refuses a target with live WAL/SHM sidecars,
optionally preserves the current healthy target, restores into a private
temporary file, verifies it, atomically replaces the target, and fsyncs the
parent directory.

## First production cutover

There is no legacy migration step. On the production host:

1. Stop every IIC-Forge process or container.
2. Resolve the exact database path or Docker volume that production will use.
3. Confirm that any existing database contains test data only.
4. Remove that exact disposable database together with its `-wal` and `-shm`
   sidecars, or create a new empty Docker volume. The application will not do
   this automatically.
5. Start the production artifact against the empty location.
6. Query migration history:

   ```bash
   sqlite3 /absolute/path/to/iic.db \
     'SELECT version, name, checksum, applied_ts, app_version FROM schema_migrations ORDER BY version;'
   ```

7. Verify database health:

   ```bash
   sqlite3 /absolute/path/to/iic.db 'PRAGMA integrity_check;'
   sqlite3 /absolute/path/to/iic.db 'PRAGMA foreign_key_check;'
   ```

8. Restart the artifact and confirm migration history is unchanged.
9. Confirm no `migration-backups/` directory was created by the fresh
   bootstrap.

The current release should report version `1` with migration name `baseline`.

## Future production migration procedure

Do not allow an old writer to run while a new artifact migrates the database.

1. Stop all writer containers.
2. Confirm the database path and local free space.
3. Run one migration connection using the exact new production artifact:

   ```bash
   python - /absolute/path/to/iic.db <<'PY'
   import sys
   from tradingagents.persistence.db import connect

   connection = connect(sys.argv[1])
   try:
       rows = connection.execute(
           "SELECT version, name, checksum, applied_ts, app_version "
           "FROM schema_migrations ORDER BY version"
       ).fetchall()
       for row in rows:
           print(tuple(row))
   finally:
       connection.close()
   PY
   ```

4. Confirm the expected version and a new verified `pre-vNNNN` recovery point.
5. Repeat the SQLite integrity and foreign-key checks.
6. Start only the new application version and monitor health.

The first incompatible future migration must also define a deployment rule
that prevents an older artifact from writing after the upgrade.

## Offline restore procedure

Stop all writers and confirm that clean shutdown removed the target WAL and SHM
sidecars. Then run:

```bash
python - /absolute/path/to/backup.db /absolute/path/to/iic.db <<'PY'
import sys
from tradingagents.persistence.db import restore_database

rollback_backup = restore_database(sys.argv[1], sys.argv[2])
print(f"pre-restore rollback backup: {rollback_backup}")
PY
```

Repeat the SQLite integrity and foreign-key checks before restarting services.

## Adding a migration

1. Add the next contiguous file, such as `0002_event_retry_state.sql`.
2. Never edit an applied migration.
3. Keep one migration focused on one schema concern.
4. Use SQLite-supported transactional DDL.
5. End every executable statement with a semicolon.
6. Add fresh-latest, versioned-upgrade, concurrent-upgrade, forced-failure,
   schema-shape, and restore tests.
7. Verify the new migration is present in the wheel and source distribution.

## Verification requirements

Batch 2 is complete only when all of the following pass:

- fresh initialization and idempotent reconnect;
- concurrent fresh initialization;
- unversioned-database refusal without mutation;
- migration checksum, gap, and future-version rejection;
- runtime schema-drift rejection;
- successful and concurrent simulated version `0002` upgrades;
- forced migration failure and transactional rollback;
- preflight integrity, foreign-key, and disk-capacity refusal;
- verified backup creation, tamper rejection, and offline restore;
- full hermetic repository suite;
- changed-file Ruff and mypy;
- frozen lock verification;
- wheel and source-distribution resource verification;
- installed-wheel smoke outside the source tree;
- clean-volume container smoke where Docker is available.

Every skipped automated or field check must be reported with its reason and
the exact on-field execution procedure.

## Verification result

- Migration suite: 19 passed.
- Full hermetic suite: 928 passed, 2 expected live-credential skips,
  2 warnings, and 78 unittest subtests passed.
- Successful and failed fresh bootstrap: passed.
- Concurrent fresh bootstrap: passed.
- Unversioned database refusal without mutation: passed.
- Successful, concurrent, and forced-failure version `0002` simulations:
  passed.
- Migration history, checksum, future-version, and schema-drift refusal:
  passed.
- Backup capacity, verification, tamper rejection, and restore: passed.
- Batch 2 changed-file Ruff: passed.
- Focused mypy for the migration engine: passed.
- Python compilation and whitespace validation: passed.
- Frozen lock check: passed with 202 resolved packages.
- Wheel and source distribution: built successfully.
- All 20 runtime resources, including migration `0001`: present in both
  distributions.
- Installed-wheel smoke outside the source tree: passed, including clean
  database bootstrap.

The two warnings come from optional pandas acceleration packages in the
workstation's global Conda environment: `numexpr` 2.8.7 and `bottleneck`
1.3.7. The frozen production environment does not install either optional
package, so these are not production dependency or Batch 2 failures. Unknown
future Anthropic model fixtures were removed; the effort allowlist now
contains only models present in the application model catalog.

## Skipped automated and field checks

### Live DeepSeek integrations

The following tests were skipped because no real exported
`DEEPSEEK_API_KEY` was provided:

1. `tests/smoke/test_f1_exit_gate.py::test_f1_exit_gate_deepdive_aapl`
2. `tests/test_deepseek_reasoning.py::TestDeepSeekLiveStructuredOutput::test_v4_flash_returns_structured_output`

Run them on-field with the key exported before pytest starts:

```bash
export DEEPSEEK_API_KEY='operator-live-test-key'
python -m pytest -q -m integration --allow-external-network
unset DEEPSEEK_API_KEY
```

### Local production-container bootstrap

Docker, Podman, and BuildKit are unavailable in this workstation execution
environment. On the Docker host:

```bash
docker build --tag iic-forge:batch2 .
docker volume create iic-forge-batch2-smoke
docker run --rm --entrypoint python \
  --volume iic-forge-batch2-smoke:/data \
  iic-forge:batch2 \
  -c "from tradingagents.persistence.db import connect; c=connect('/data/iic.db'); print(c.execute('SELECT version,name FROM schema_migrations').fetchall()); c.close()"
docker run --rm --entrypoint python \
  --volume iic-forge-batch2-smoke:/data \
  iic-forge:batch2 \
  -c "from tradingagents.persistence.db import connect; c=connect('/data/iic.db'); print(c.execute('PRAGMA integrity_check').fetchone()); print(c.execute('PRAGMA foreign_key_check').fetchall()); c.close()"
docker volume rm iic-forge-batch2-smoke
```

The first run must report migration `(1, 'baseline')`; the second must report
`ok`, an empty foreign-key result, and no additional migration row.

### Hosted GitHub Actions

Hosted CI cannot be executed in the local validation environment. After push,
require the blocking `test` and `image-build` jobs to pass on the exact pushed
commit before deployment.

Old-database adoption is not a skipped test. It was deliberately removed from
the production contract because all pre-production data is disposable.
