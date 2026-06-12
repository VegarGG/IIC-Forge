#!/usr/bin/env bash
set -euo pipefail

REPO=${REPO:-/opt/iic-forge}
COMPOSE_PROJECT=${COMPOSE_PROJECT:-iic-forge}

cd "${REPO}"
docker compose -p "${COMPOSE_PROJECT}" config >/tmp/iic-forge-compose-rendered.yml
docker compose -p "${COMPOSE_PROJECT}" up -d redis
docker compose -p "${COMPOSE_PROJECT}" exec -T redis redis-cli ping
python scripts/focused_soak_gate.py --mode preflight --json
