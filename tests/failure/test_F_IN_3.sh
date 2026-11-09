#!/usr/bin/env bash
# F-IN-3: a bad release is rolled back automatically and the previous version keeps serving.
# Needs the dev/prod stack running with the images ots-app:latest. Run: make e2e-docker
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
# shellcheck source=../../deploy/scripts/lib.sh
source deploy/scripts/lib.sh

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

wait_healthy 60 || fail "stack is not healthy before the test"
good_id="$(docker image inspect -f '{{.Id}}' ots-app:latest)"

# Build a deliberately broken release: identical, but /healthz always answers 503.
work="$(mktemp -d)"; trap 'rm -rf "$work"; docker rmi -f ots-app:broken >/dev/null 2>&1 || true' EXIT
printf 'FROM ots-app:latest\nUSER root\nRUN sed -i "s/status=503 if critical else 200/status=503/" /srv/app/ots/core/health.py \\\n && grep -q "status=503)" /srv/app/ots/core/health.py\nUSER ots\n' >"$work/Dockerfile"
docker build -q -t ots-app:broken "$work" >/dev/null

set +e
OTS_PREBUILT=1 OTS_HEALTH_TIMEOUT=30 deploy/scripts/deploy.sh broken >"$work/deploy.log" 2>&1
code=$?
set -e
tail -n 8 "$work/deploy.log"

[[ $code -eq 1 ]] && pass "deploy.sh exited 1 for the bad release" || fail "expected exit 1, got $code"
grep -q "rolling back" "$work/deploy.log" && pass "rollback was triggered automatically" || fail "no rollback in log"
wait_healthy 90 && pass "/healthz is healthy again" || fail "previous version is not serving"
[[ "$(docker image inspect -f '{{.Id}}' ots-app:latest)" == "$good_id" ]] && pass "ots-app:latest is the previous image" || fail "latest was not restored"
running="$(docker inspect -f '{{.Image}}' "$(compose ps -q app)")"
[[ "$running" == "$good_id" ]] && pass "the running app container uses the previous image" || fail "app is running the wrong image"
echo "F-IN-3 OK"
