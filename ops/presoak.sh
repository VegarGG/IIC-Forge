#!/usr/bin/env bash
# Compatibility entry point; production soak behavior is defined by ops/soak.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [ -n "${PRESOAK_SECONDS:-}" ] && [ -z "${IIC_SOAK_SECONDS:-}" ]; then
  export IIC_SOAK_SECONDS="$PRESOAK_SECONDS"
  export IIC_SOAK_ALLOW_SHORT=true
fi
exec "$SCRIPT_DIR/soak.sh" "$@"
