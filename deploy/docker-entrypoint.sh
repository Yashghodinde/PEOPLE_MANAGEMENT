#!/bin/sh
set -eu
case "${1:-app}" in
  app) exec uvicorn config.asgi:application --host 0.0.0.0 --port 8000 --workers 1 --timeout-keep-alive 75 ;;
  worker) exec python manage.py run_worker ;;
  *) exec "$@" ;;
esac
