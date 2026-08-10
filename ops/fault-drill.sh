#!/usr/bin/env bash
# Credential-free destructive fault drill against explicitly disposable volumes.
set -euo pipefail

CONFIRMATION="DESTROY IIC-FORGE BATCH10 DRILL"
if [ "${IIC_FAULT_DRILL_CONFIRM:-}" != "$CONFIRMATION" ]; then
  echo "fatal: export IIC_FAULT_DRILL_CONFIRM='$CONFIRMATION'" >&2
  exit 64
fi

PROJECT_NAME="${IIC_COMPOSE_PROJECT_NAME:-}"
DATA_VOLUME="${IIC_DATA_VOLUME:-}"
REDIS_VOLUME="${IIC_REDIS_VOLUME:-}"
case "$PROJECT_NAME:$DATA_VOLUME:$REDIS_VOLUME" in
  iic-forge-batch10-drill*:iic-forge-batch10-drill*-data:iic-forge-batch10-drill*-redis) ;;
  *)
    echo "fatal: project and volume names must use the iic-forge-batch10-drill prefix" >&2
    exit 64
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
TEMP_ROOT="$(mktemp -d -t iic-batch10-drill.XXXXXXXX)"
export IIC_ENV_FILE="${IIC_ENV_FILE:-$PROJECT_ROOT/.env.production.example}"
export IIC_SECRETS_DIR="$TEMP_ROOT/secrets"
export IIC_BACKUP_DIR="$TEMP_ROOT/backups"
EVIDENCE="${IIC_FAULT_DRILL_OUTPUT:-$PROJECT_ROOT/artifacts/release-candidate/fault-drill-$(date -u +%Y%m%dT%H%M%SZ).log}"
CONTAINER_NAME="${PROJECT_NAME}-worker-kill-probe"

cleanup() {
  result=$?
  trap - EXIT INT TERM
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  if [ "${IIC_KEEP_DRILL:-false}" != "true" ]; then
    docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -rf "$TEMP_ROOT"
  exit "$result"
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null 2>&1 || { echo "fatal: docker is required" >&2; exit 69; }
command -v openssl >/dev/null 2>&1 || { echo "fatal: openssl is required" >&2; exit 69; }
install -d -m 0700 "$IIC_SECRETS_DIR" "$IIC_BACKUP_DIR" "$(dirname "$EVIDENCE")"
for name in deepseek_api_key polygon_api_key telegram_api_hash telegram_bot_token smtp_user smtp_app_password operator_dashboard_password; do
  printf '%s\n' 'batch10-disposable-placeholder' > "$IIC_SECRETS_DIR/$name"
done
printf '%s\n' '12345' > "$IIC_SECRETS_DIR/telegram_api_id"
openssl rand -out "$IIC_SECRETS_DIR/backup_encryption_key" 32
chmod 0600 "$IIC_SECRETS_DIR"/*

cd "$PROJECT_ROOT"
exec > >(tee "$EVIDENCE") 2>&1
chmod 0600 "$EVIDENCE"
echo "batch10 fault drill start=$(date -u +%FT%TZ) commit=$(git rev-parse HEAD) project=$PROJECT_NAME"

docker compose config --quiet
if [ "${IIC_SKIP_BUILD:-false}" != "true" ]; then
  docker compose build --pull
fi
docker compose up -d --wait redis
docker compose run --rm --no-deps volume-init
docker compose run --rm --no-deps database-init

echo "stage=redis-restart"
STREAM_ID="$(docker compose exec -T redis redis-cli XADD batch10:canary '*' value persisted | tr -d '\r')"
docker compose exec -T redis redis-cli WAITAOF 1 0 5000
docker compose restart redis
docker compose up -d --wait redis
docker compose exec -T redis redis-cli XRANGE batch10:canary "$STREAM_ID" "$STREAM_ID" | grep -q persisted

echo "stage=sqlite-restart"
docker compose run --rm --no-deps --entrypoint python database-init -c \
  "from tradingagents.persistence.db import connect; c=connect('/data/iic.db'); c.execute(\"INSERT OR REPLACE INTO watchlist (ticker,added_ts,ttl_until,tags) VALUES ('BATCH10','2026-08-09T00:00:00+00:00',NULL,'[]')\"); c.commit()"
docker compose run --rm --no-deps database-init
docker compose run --rm --no-deps --entrypoint python database-init -c \
  "from tradingagents.persistence.db import connect; c=connect('/data/iic.db'); assert c.execute(\"SELECT ticker FROM watchlist WHERE ticker='BATCH10'\").fetchone()[0]=='BATCH10'"

echo "stage=worker-timeout"
docker compose run --rm --no-deps --volume "$PROJECT_ROOT:/workspace:ro" --entrypoint python database-init \
  /workspace/scripts/batch4_process_fault_probe.py timeout \
  --db /data/batch10-timeout.db --pid-file /tmp/batch10-timeout.pid

echo "stage=worker-sigkill-recovery"
docker compose run --detach --name "$CONTAINER_NAME" --no-deps \
  --volume "$PROJECT_ROOT:/workspace:ro" --entrypoint python database-init \
  /workspace/scripts/batch4_process_fault_probe.py hold \
  --db /data/batch10-kill.db --pid-file /tmp/batch10-kill.pid
for _ in $(seq 1 30); do
  docker logs "$CONTAINER_NAME" 2>&1 | grep -q "probe worker ready" && break
  sleep 1
done
docker logs "$CONTAINER_NAME" 2>&1 | grep -q "probe worker ready"
docker kill --signal KILL "$CONTAINER_NAME"
docker rm "$CONTAINER_NAME"
docker compose run --rm --no-deps --volume "$PROJECT_ROOT:/workspace:ro" --entrypoint python database-init \
  /workspace/scripts/batch4_process_fault_probe.py recover --db /data/batch10-kill.db

echo "stage=budget-and-quiet-hours"
docker compose run --rm --no-deps --volume "$PROJECT_ROOT:/workspace:ro" --entrypoint python database-init \
  /workspace/scripts/batch10_fault_probe.py budget --db /data/batch10-budget.db
docker compose run --rm --no-deps --volume "$PROJECT_ROOT:/workspace:ro" --entrypoint python database-init \
  /workspace/scripts/batch10_fault_probe.py quiet-hours --db /data/batch10-quiet.db

echo "stage=delivery-provider-failure"
docker compose run --rm --no-deps --entrypoint python database-init \
  -m tradingagents.delivery.field_probe enqueue --db /data/iic.db --channel telegram --ready-now
docker compose run --rm --no-deps --entrypoint python database-init \
  -m tradingagents.delivery.field_probe drain-one --db /data/iic.db
docker compose run --rm --no-deps --entrypoint python database-init -c \
  "from tradingagents.persistence.db import connect; c=connect('/data/iic.db'); r=c.execute('SELECT state,error_category FROM delivery_queue ORDER BY delivery_job_id DESC LIMIT 1').fetchone(); assert r['state']=='blocked' and r['error_category']=='credential_missing', dict(r)"

echo "stage=backup-and-isolated-restore"
docker compose stop redis
docker compose --profile operations run --rm --no-deps backup-create
docker compose up -d --wait redis
ARCHIVE="$(python -c "import json; print(json.load(open('$IIC_BACKUP_DIR/latest.json'))['archive'])")"
docker compose --profile operations run --rm --no-deps backup-restore \
  forge operator restore-drill "/backups/$ARCHIVE" \
  --key-file /run/secrets/backup_encryption_key \
  --note "Batch 10 disposable release fault drill" \
  --confirm "RUN RESTORE DRILL"

docker compose run --rm --no-deps database-init forge operator status --full-database-check --no-redis
echo "batch10 fault drill passed end=$(date -u +%FT%TZ) archive=$ARCHIVE"
