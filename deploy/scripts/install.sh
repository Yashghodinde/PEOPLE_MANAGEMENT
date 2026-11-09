#!/usr/bin/env bash
# First install: directories, config, images, migrations, admin accounts, start the stack.
# Needs root on the server (to set owners); on a developer Mac run without root.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

AS_ROOT=0
[[ "$(id -u)" == "0" ]] && AS_ROOT=1

log "1/6 creating data directories under $OTS_HOST_DATA"
for d in pg wal-spool wal-local blobs staging selfies backup-staging backup-local; do
  mkdir -p "$OTS_HOST_DATA/$d"
done
if ((AS_ROOT)); then
  # app/worker run as uid 1000; PostgreSQL as uid 999. WAL dirs are shared between them.
  chown 1000:1000 "$OTS_HOST_DATA"/{blobs,staging,selfies,backup-staging,backup-local}
  chown 999:999 "$OTS_HOST_DATA/pg"
  chown 999:1000 "$OTS_HOST_DATA"/{wal-spool,wal-local}
  chmod 2770 "$OTS_HOST_DATA"/{wal-spool,wal-local}
  chmod 750 "$OTS_HOST_DATA"/{blobs,staging,selfies}
  chmod 700 "$OTS_HOST_DATA/pg"
fi

log "2/6 config in $OTS_HOST_ETC"
mkdir -p "$OTS_HOST_ETC"
if [[ ! -f "$OTS_ENV_FILE" ]]; then
  cp "$ROOT/deploy/.env.example" "$OTS_ENV_FILE"
  log "created $OTS_ENV_FILE from the template: EDIT IT (secrets, hostnames), then re-run install.sh"
  chmod 600 "$OTS_ENV_FILE"
  exit 2
fi
chmod 600 "$OTS_ENV_FILE"
chmod 700 "$OTS_HOST_ETC"
grep -q 'CHANGE_ME' "$OTS_ENV_FILE" && die "$OTS_ENV_FILE still contains CHANGE_ME placeholders"

log "3/6 building images"
compose build app db

log "4/6 starting the database and migrating"
compose up -d db
for _ in $(seq 1 30); do
  compose exec -T db pg_isready -U "$OTS_DB_USER" -d "$OTS_DB_NAME" >/dev/null 2>&1 && break
  sleep 2
done
run_migrations

log "5/6 creating the first Super Admin and the break-glass account"
if compose run --rm --no-deps -T worker python manage.py help bootstrap_admins >/dev/null 2>&1; then
  compose run --rm --no-deps -T worker python manage.py bootstrap_admins
else
  log "bootstrap_admins is not available yet (it arrives with M02); skipping"
fi

log "6/6 starting the stack"
compose up -d
if wait_healthy 120; then log "install complete: $OTS_HEALTH_URL is healthy"; else die "stack started but /healthz is unhealthy"; fi
