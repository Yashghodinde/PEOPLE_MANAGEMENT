#!/usr/bin/env bash
# Run migrations with the migrator credentials (the only place DATABASE_MIGRATOR_URL is used).
set -euo pipefail
: "${DATABASE_MIGRATOR_URL:?DATABASE_MIGRATOR_URL must be set}"
cd "$(dirname "$0")/../../app"
DATABASE_URL="$DATABASE_MIGRATOR_URL" python manage.py migrate --noinput "$@"
