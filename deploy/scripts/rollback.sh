#!/usr/bin/env bash
# Roll the app and worker back to the image tagged ots-app:previous. Migrations are NOT reversed.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

docker image inspect ots-app:previous >/dev/null 2>&1 || die "no ots-app:previous image to roll back to"

log "re-tagging ots-app:previous as ots-app:latest"
docker tag ots-app:previous ots-app:latest
compose up -d --force-recreate --no-deps app worker

if [[ -f "$STATE_DIR/migrations-before.txt" ]]; then
  now_list="$(mktemp)"
  migration_list >"$now_list"
  if ! diff -q "$STATE_DIR/migrations-before.txt" "$now_list" >/dev/null; then
    log "WARNING: the database schema changed since the previous release and was NOT reversed."
    log "The old code may not run on the new schema. Follow Runbook E (docs/plan/22_RUNBOOKS.md):"
    log "restore the pre-migration dump from $OTS_HOST_DATA/backup-local/pre-migrate-*.dump."
  fi
  rm -f "$now_list"
fi

if wait_healthy 90; then log "rollback complete: previous version is serving"; else die "rolled back but /healthz is still unhealthy"; fi
