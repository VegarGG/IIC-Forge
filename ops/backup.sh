#!/usr/bin/env bash
# Create one authenticated local recovery point for both Compose volumes.
# Schedule every 50 minutes to leave operating margin inside the one-hour RPO.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
BACKUP_DIR="${IIC_BACKUP_DIR:-$PROJECT_ROOT/backups}"

command -v docker >/dev/null 2>&1 || {
  echo "fatal: docker is required" >&2
  exit 69
}
command -v flock >/dev/null 2>&1 || {
  echo "fatal: flock is required for overlapping-backup protection" >&2
  exit 69
}

install -d -m 0700 "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd -P)"
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
      echo "fatal: backup finished unsuccessfully and the Compose stack did not restart" >&2
      result=1
    fi
  fi
  exit "$result"
}
trap resume_stack EXIT INT TERM

docker compose config --quiet
docker compose stop --timeout 60
stack_stopped=1

docker compose --profile operations run --rm --no-deps backup-create

docker compose up -d
stack_stopped=0

docker compose --profile operations run --rm --no-deps backup-create \
  forge backup status --output-root /backups --max-age-minutes 60

echo "backup complete: encrypted SQLite/data and Redis recovery point verified"
