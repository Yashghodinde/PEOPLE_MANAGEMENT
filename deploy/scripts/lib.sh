#!/usr/bin/env bash
# Shared settings for install.sh / deploy.sh / rollback.sh. Every path is overridable so the same scripts run
# on the Linux server (defaults) and on a developer Mac (OTS_HOST_DATA, OTS_HOST_ETC, OTS_ENV_FILE).
# shellcheck disable=SC2034  # variables are consumed by the scripts that source this file

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OTS_HOST_DATA="${OTS_HOST_DATA:-/srv/data}"
OTS_HOST_ETC="${OTS_HOST_ETC:-/etc/ots}"
OTS_ENV_FILE="${OTS_ENV_FILE:-$OTS_HOST_ETC/.env}"
OTS_HEALTH_URL="${OTS_HEALTH_URL:-https://localhost/healthz}"
OTS_HEALTH_TIMEOUT="${OTS_HEALTH_TIMEOUT:-90}"
OTS_DB_USER="${OTS_DB_USER:-ots_migrator}"
OTS_DB_NAME="${OTS_DB_NAME:-ots}"
STATE_DIR="$OTS_HOST_DATA/backup-local/.deploy-state"
export OTS_HOST_DATA OTS_HOST_ETC OTS_ENV_FILE

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

compose() {
  docker compose --env-file "$OTS_ENV_FILE" -f "$ROOT/deploy/compose.yaml" "$@"
}

# Poll /healthz until HTTP 200 or the timeout (seconds) passes.
wait_healthy() {
  local deadline=$((SECONDS + ${1:-$OTS_HEALTH_TIMEOUT}))
  while ((SECONDS < deadline)); do
    if curl -fsk --max-time 5 "$OTS_HEALTH_URL" >/dev/null 2>&1; then return 0; fi
    sleep 3
  done
  return 1
}

# Applied migrations, one "app name" per line (used to detect schema changes across a rollback).
migration_list() {
  compose exec -T db psql -U "$OTS_DB_USER" -d "$OTS_DB_NAME" -Atc \
    "SELECT app || ' ' || name FROM django_migrations ORDER BY id" 2>/dev/null || true
}

run_migrations() {
  # shellcheck disable=SC2016  # the variables must expand inside the container, not here
  compose run --rm --no-deps -T worker sh -c \
    'DATABASE_URL="${DATABASE_MIGRATOR_URL:-$DATABASE_URL}" python manage.py migrate --noinput'
}
