.PHONY: up test lint typecheck migrate worker e2e-failure e2e-docker shellcheck dev assets
up:
	docker compose -f deploy/compose.yaml up -d
test:
	uv run pytest -q
lint:
	uv run ruff check . && uv run ruff format --check . && python3 scripts/check_templates.py
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
dev:
	cd app \&\& DJANGO_DEBUG=true uv run python manage.py runserver 8765

assets:
	cd app/static_src \&\& npm install --no-audit --no-fund \&\& npm run build
