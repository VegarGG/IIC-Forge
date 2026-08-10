#!/usr/bin/env bash
# Record and evaluate a production-like 72-hour Docker Compose soak.
set -euo pipefail

CONFIRMATION="RUN IIC-FORGE 72H SOAK"
if [ "${IIC_SOAK_CONFIRM:-}" != "$CONFIRMATION" ]; then
  echo "fatal: export IIC_SOAK_CONFIRM='$CONFIRMATION'" >&2
  exit 64
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
DURATION="${IIC_SOAK_SECONDS:-259200}"
INTERVAL="${IIC_SOAK_INTERVAL_SECONDS:-300}"
MAX_GAP="${IIC_SOAK_MAXIMUM_GAP_SECONDS:-$((INTERVAL * 3))}"

case "$DURATION:$INTERVAL:$MAX_GAP" in
  *[!0-9:]*|0:*|*:0:*|*:0)
    echo "fatal: soak duration, interval, and maximum gap must be positive integers" >&2
    exit 64
    ;;
esac
if [ "$DURATION" -lt 259200 ] && [ "${IIC_SOAK_ALLOW_SHORT:-false}" != "true" ]; then
  echo "fatal: the release gate requires 259200 seconds; set IIC_SOAK_ALLOW_SHORT=true only for script smoke tests" >&2
  exit 64
fi

command -v docker >/dev/null 2>&1 || { echo "fatal: docker is required" >&2; exit 69; }
command -v python >/dev/null 2>&1 || { echo "fatal: python is required" >&2; exit 69; }

cd "$PROJECT_ROOT"
docker compose config --quiet
docker compose ps

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE="${IIC_SOAK_OUTPUT:-$PROJECT_ROOT/artifacts/release-candidate/soak-$STAMP.ndjson}"
SUMMARY="${EVIDENCE%.ndjson}-summary.json"
install -d -m 0700 "$(dirname "$EVIDENCE")"
if [ -e "$EVIDENCE" ] || [ -e "$SUMMARY" ]; then
  echo "fatal: refusing to overwrite soak evidence: $EVIDENCE" >&2
  exit 73
fi

START="$(date +%s)"
END=$((START + DURATION))
while :; do
  python scripts/soak_evidence.py collect >> "$EVIDENCE"
  chmod 0600 "$EVIDENCE"
  NOW="$(date +%s)"
  if [ "$NOW" -ge "$END" ]; then
    break
  fi
  REMAINING=$((END - NOW))
  WAIT="$INTERVAL"
  if [ "$REMAINING" -lt "$WAIT" ]; then
    WAIT="$REMAINING"
  fi
  sleep "$WAIT"
done

python scripts/soak_evidence.py evaluate "$EVIDENCE" \
  --minimum-duration-seconds "$DURATION" \
  --maximum-gap-seconds "$MAX_GAP" | tee "$SUMMARY"
chmod 0600 "$SUMMARY"
echo "soak evidence: $EVIDENCE"
echo "soak summary:  $SUMMARY"
