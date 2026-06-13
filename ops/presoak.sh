#!/usr/bin/env bash
set -euo pipefail

REPO=${REPO:-/opt/iic-forge}
COMPOSE_PROJECT=${COMPOSE_PROJECT:-iic-forge}

cd "${REPO}"
docker compose -p "${COMPOSE_PROJECT}" config >/tmp/iic-forge-compose-rendered.yml
docker compose -p "${COMPOSE_PROJECT}" up -d redis
docker compose -p "${COMPOSE_PROJECT}" exec -T redis redis-cli ping
TRADINGAGENTS_IIC_DB_PATH=/srv/iic-forge/data/iic.db python scripts/focused_soak_gate.py --mode preflight --json
