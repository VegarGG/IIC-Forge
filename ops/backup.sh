#!/usr/bin/env bash
set -euo pipefail

COMPOSE_PROJECT=${COMPOSE_PROJECT:-iic-forge}
BACKUP_ROOT=${BACKUP_ROOT:-/srv/iic-forge/backups}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT_DIR="${BACKUP_ROOT}/${STAMP}"

mkdir -p "${OUT_DIR}"
docker compose -p "${COMPOSE_PROJECT}" exec -T redis redis-cli SAVE
docker run --rm \
  -v "${COMPOSE_PROJECT}_iic_redis_data:/redis:ro" \
  -v "${OUT_DIR}:/backup" \
  alpine:3.20 \
  sh -c 'cp /redis/dump.rdb /backup/redis-dump.rdb'
cp -a /srv/iic-forge/data "${OUT_DIR}/data"
echo "backup written to ${OUT_DIR}"
