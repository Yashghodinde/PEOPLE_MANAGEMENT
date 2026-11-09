#!/usr/bin/env bash
# Deploy a git ref: build, back up the database, migrate, restart, verify, auto-rollback on failure.
# Usage: deploy.sh <git-ref>        (OTS_PREBUILT=1 skips the build and uses an existing ots-app:<ref>)
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

REF="${1:?usage: deploy.sh <git-ref>}"
# Resolve a git ref to its short SHA so image tags identify the exact commit; otherwise sanitise the name.
if sha="$(git -C "$ROOT" rev-parse --short "$REF^{commit}" 2>/dev/null)"; then TAG="$sha"
else TAG="$(printf '%s' "$REF" | tr -c 'A-Za-z0-9_.-' '-')"; fi
mkdir -p "$STATE_DIR" "$OTS_HOST_DATA/backup-local"

log "1/7 tagging current image as ots-app:previous"
if docker image inspect ots-app:latest >/dev/null 2>&1; then docker tag ots-app:latest ots-app:previous; fi

log "2/7 building ots-app:$TAG"
if [[ "${OTS_PREBUILT:-0}" == "1" ]]; then
  docker image inspect "ots-app:$TAG" >/dev/null 2>&1 || die "OTS_PREBUILT=1 but ots-app:$TAG does not exist"
else
  build_dir="$(mktemp -d)"
  trap 'rm -rf "$build_dir"' EXIT
  git -C "$ROOT" archive "$REF" | tar -x -C "$build_dir"
  OTS_TAG="$TAG" docker compose --env-file "$OTS_ENV_FILE" -f "$build_dir/deploy/compose.yaml" build app db
fi
docker tag "ots-app:$TAG" ots-app:latest

log "3/7 pre-migration database dump"
migration_list >"$STATE_DIR/migrations-before.txt"
dump="$OTS_HOST_DATA/backup-local/pre-migrate-$(date +%Y%m%d-%H%M%S).dump"
if compose exec -T db pg_dump -U "$OTS_DB_USER" -Fc "$OTS_DB_NAME" >"$dump"; then
  log "dump written: $dump"
else
  rm -f "$dump"
  die "pg_dump failed; refusing to migrate without a backup"
fi
# keep only the last 3 dumps
# shellcheck disable=SC2012
ls -1t "$OTS_HOST_DATA"/backup-local/pre-migrate-*.dump 2>/dev/null | tail -n +4 | while read -r old; do rm -f "$old"; done

log "4/7 running migrations"
run_migrations || { log "migration failed"; "$(dirname "${BASH_SOURCE[0]}")/rollback.sh" || true; exit 1; }

log "5/7 starting the new version"
compose up -d --force-recreate --no-deps app worker

log "6/7 waiting up to ${OTS_HEALTH_TIMEOUT}s for /healthz"
if wait_healthy "$OTS_HEALTH_TIMEOUT"; then
  log "7/7 deploy of $REF succeeded"
  exit 0
fi

log "7/7 /healthz did not become healthy: rolling back"
"$(dirname "${BASH_SOURCE[0]}")/rollback.sh" || true
exit 1
