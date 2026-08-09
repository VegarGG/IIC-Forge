#!/usr/bin/env bash
# Offline restore of one authenticated local IIC-Forge backup.
set -euo pipefail

CONFIRMATION="RESTORE_IIC_FORGE_LOCAL_BACKUP"
if [ "$#" -ne 3 ] || [ "$2" != "--confirm" ] || [ "$3" != "$CONFIRMATION" ]; then
  echo "usage: $0 <archive-basename.iicbak> --confirm $CONFIRMATION" >&2
  exit 64
fi

ARCHIVE_NAME="$1"
if [ "$(basename "$ARCHIVE_NAME")" != "$ARCHIVE_NAME" ]; then
  echo "fatal: archive must be a basename within IIC_BACKUP_DIR" >&2
  exit 64
fi
case "$ARCHIVE_NAME" in
  iic-forge-*.iicbak) ;;
  *)
    echo "fatal: archive name does not match the IIC-Forge backup format" >&2
    exit 64
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
BACKUP_DIR="${IIC_BACKUP_DIR:-$PROJECT_ROOT/backups}"

command -v docker >/dev/null 2>&1 || {
  echo "fatal: docker is required" >&2
  exit 69
}
command -v flock >/dev/null 2>&1 || {
  echo "fatal: flock is required for restore exclusion" >&2
  exit 69
}

BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"
ARCHIVE_PATH="$BACKUP_DIR/$ARCHIVE_NAME"
if [ ! -f "$ARCHIVE_PATH" ] || [ ! -f "$ARCHIVE_PATH.sha256" ]; then
  echo "fatal: backup archive or checksum sidecar is missing" >&2
  exit 66
fi
export IIC_BACKUP_DIR="$BACKUP_DIR"
cd "$PROJECT_ROOT"

exec 9>"$BACKUP_DIR/.operation.lock"
if ! flock -n 9; then
  echo "fatal: another backup or restore operation is already running" >&2
  exit 75
fi

stack_stopped=0
resume_stack() {
  result=$?
  trap - EXIT INT TERM
  if [ "$stack_stopped" -eq 1 ]; then
    if ! docker compose up -d; then
      echo "fatal: restore failed and the Compose stack did not restart" >&2
      result=1
    fi
  fi
  exit "$result"
}
trap resume_stack EXIT INT TERM

docker compose config --quiet

# Authenticate the selected recovery point before interrupting production.
docker compose --profile operations run --rm --no-deps backup-create \
  forge backup verify "/backups/$ARCHIVE_NAME" \
  --key-file /run/secrets/backup_encryption_key

docker compose stop --timeout 60
stack_stopped=1

# Preserve the immediately preceding state without pruning the selected source.
docker compose --profile operations run --rm --no-deps backup-create \
  forge backup create --data-root /source/data --redis-root /source/redis \
  --output-root /backups --key-file /run/secrets/backup_encryption_key \
  --label pre-restore --no-prune

docker compose --profile operations run --rm --no-deps backup-restore \
  forge backup restore "/backups/$ARCHIVE_NAME" \
  --data-root /target/data --redis-root /target/redis \
  --key-file /run/secrets/backup_encryption_key \
  --confirm "$CONFIRMATION"

docker compose up -d redis volume-init database-init ticker-seed
docker compose up -d
docker compose run --rm --no-deps database-init \
  forge runtime health --database --redis
stack_stopped=0

echo "restore complete: $ARCHIVE_NAME authenticated, restored, and health-checked"
