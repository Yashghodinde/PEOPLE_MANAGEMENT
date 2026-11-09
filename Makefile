.PHONY: up test lint typecheck migrate worker e2e-failure e2e-docker shellcheck
up:
	docker compose -f deploy/compose.yaml up -d
test:
	uv run pytest -q
lint:
	uv run ruff check . && uv run ruff format --check .
typecheck:
	uv run mypy app/ots/core app/ots/storage app/ots/backup
migrate:
	uv run python app/manage.py migrate
worker:
	uv run python app/manage.py run_worker
e2e-failure:
	uv run pytest tests/failure -q

e2e-docker:
	tests/failure/test_F_IN_3.sh
shellcheck:
	shellcheck -x --source-path=SCRIPTDIR deploy/scripts/*.sh tests/failure/*.sh
