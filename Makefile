
.PHONY: setup
setup:
	uv sync --group dev; \
	cp .env.example .env; \
	docker compose -f docker-compose-dev.yml down -v; \
	docker compose -f docker-compose-dev.yml up -d; \
	sleep 3; \
	$(MAKE) bootstrap; \
	$(MAKE) ENABLE_OBSERVABILITY=False run

.PHONY: format
format:
	uv run ruff format .

.PHONY: lint
lint:
	uv run ruff check . --fix

.PHONY: mypy
mypy:
	uv run mypy --strict --extra-checks --warn-unreachable --warn-unused-ignores src

.PHONY: test
test:
	uv run pytest --cov=src --cov-report=term --cov-report=html --cov-branch -v src/ && uv run coverage html --skip-covered

.PHONY: test-parallel
test-parallel:
	uv run pytest -n auto --cov=src --cov-report=term --cov-report=html --cov-branch -v src/ && uv run coverage html --skip-covered

.PHONY: test-failed
test-failed:
	uv run pytest --cov=src --cov-report=term --cov-report=html --cov-branch -v --last-failed src/ && uv run coverage html --skip-covered

.PHONY: test-pipeline
test-pipeline:
	uv run pytest --cov=src --cov-report=term --cov-report=html --cov-branch --cov-fail-under=100 -v src

.PHONY: test-functional
test-functional:
	uv run pytest --cov=functional_tests --cov-report=term --cov-report=html:functional_tests/htmlcov --cov-branch -v functional_tests/ && uv run coverage html --skip-covered

.PHONY: test-functional-failed
test-functional-failed:
	uv run pytest --cov=functional_tests --cov-report=term --cov-report=html:functional_tests/htmlcov --cov-branch -v --last-failed functional_tests/ && uv run coverage html --skip-covered


# Combined command: Runs format, lint, mypy, migration-check, i18n-check, and file-length in sequence
.PHONY: check
check: format lint mypy migration-check i18n-check file-length

.PHONY: file-length
file-length:
	@./scripts/check-file-length.sh 1000

.PHONY: migration-check
migration-check:
	@echo "Checking for missing migrations..."
	@cd src && uv run python manage.py makemigrations --check --dry-run --no-input && \
		echo "✅ No missing migrations."

.PHONY: db-diagram
db-diagram:
	uv run python src/manage.py graph_models api authentication common datastore onboarding tlsn l1 fireblocks site_settings -a -g -o database.png

.PHONY: bootstrap
bootstrap:
	uv run python src/manage.py bootstrap

.PHONY: seed
seed:
	uv run python src/manage.py seed --seed 999

.PHONY: bootstrap-tests
bootstrap-tests:
	uv run python src/manage.py bootstrap_tests

.PHONY: run
run:
	uv run python src/manage.py generate_test_jwts; \
	uv run python src/manage.py runserver

.PHONY: jwt
jwt:
	@if [ -z "$(EMAIL)" ]; then \
		echo "Usage: make jwt EMAIL=user@example.com"; \
		exit 1; \
	fi
	uv run python src/manage.py get_jwt $(EMAIL)


.PHONY: run-celery
run-celery:
	cd src && uv run celery -A revel worker -l INFO --concurrency=2 --pool=prefork

.PHONY: run-celery-beat
run-celery-beat:
	cd src && uv run celery -A revel beat -l INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler

.PHONY: run-telegram
run-telegram:
	cd src && uv run python manage.py run_telegram_bot

.PHONY: run-flower
run-flower:
	cd src && uv run celery -A revel flower

.PHONY: run-stripe
run-stripe:
	stripe listen --forward-to localhost:8000/api/stripe/webhook


.PHONY: restart
restart:
	@read -p "Are you sure you want to RESTART? This action will destroy the current database and recreate it from scratch. It cannot be undone. Type 'yes' to continue: " confirm && if [ "$$confirm" = "yes" ]; then \
		docker compose -f docker-compose-dev.yml down; \
		docker volume rm revel-backend_postgres_data; \
		docker volume rm revel-backend_minio_data; \
		docker compose -f docker-compose-dev.yml up -d; \
		sleep 3; \
		rm src/db.sqlite3; \
		rm -rf src/**/migrations/0*.py; \
		uv run python src/manage.py makemigrations; \
		uv run python src/manage.py bootstrap; \
	else \
		echo "Restart aborted."; \
	fi


.PHONY: reset-db
reset-db:
	uv run python src/manage.py reset_db


.PHONY: reset-events
reset-events:
	uv run python src/manage.py reset_events


.PHONY: nuke-db
nuke-db:
	@read -p "Are you sure you want to nuke the database? This action cannot be undone. Type 'yes' to continue: " confirm && if [ "$$confirm" = "yes" ]; then \
		rm src/db.sqlite3; \
		mv src/geo/migrations/0002_load_cities.py src/geo/migrations/0002_load_cities.tmp; \
		mv src/events/migrations/0002_add_cleanup_expired_payments_periodic_task.py src/events/migrations/0002_add_cleanup_expired_payments_periodic_task.tmp; \
		rm -rf src/**/migrations/0*.py; \
		uv run python src/manage.py makemigrations; \
		mv src/geo/migrations/0002_load_cities.tmp src/geo/migrations/0002_load_cities.py; \
		mv src/events/migrations/0002_add_cleanup_expired_payments_periodic_task.tmp src/events/migrations/0002_add_cleanup_expired_payments_periodic_task.py; \
		docker compose -f docker-compose-dev.yml down; \
		docker compose -f docker-compose-dev.yml up -d; \
		sleep 3; \
		uv run python src/manage.py migrate; \
	else \
		echo "Nuke database aborted."; \
	fi

.PHONY: shell
shell:
	uv run python src/manage.py shell


.PHONY: migrations
migrations:
	uv run python src/manage.py makemigrations


.PHONY: migrate
migrate:
	uv run python src/manage.py migrate


.PHONY: makemessages
makemessages:
	cd src && uv run python manage.py makemessages -l de -l it --no-location --no-obsolete

.PHONY: compilemessages
compilemessages:
	cd src && uv run python manage.py compilemessages

.PHONY: i18n-check
i18n-check:
	@echo "Checking if translation files are up to date..."
	@cd src && uv run python manage.py compilemessages > /dev/null 2>&1; \
	if ! git diff --exit-code locale/ > /dev/null 2>&1; then \
		echo "❌ Translation files (.mo) are out of sync with .po files."; \
		echo "   Run 'make compilemessages' and commit the updated .mo files."; \
		git diff locale/; \
		exit 1; \
	else \
		echo "✅ Translation files are up to date."; \
	fi


.PHONY: check-version
check-version:
	@echo "Checking versions..."
	@curl -s https://main.api.revel.io/api/v1/version | jq -r '"Main Prod: \(.version)"'
	@curl -s https://dev.api.revel.io/api/v1/version | jq -r '"Main Dev: \(.version)"'


VERSION_FILE = VERSION

.PHONY: bump-version bump-minor

bump-version:
	@current=$$(cat $(VERSION_FILE)); \
	major=$$(echo $$current | cut -d. -f1); \
	minor=$$(echo $$current | cut -d. -f2); \
	patch=$$(echo $$current | cut -d. -f3); \
	new_patch=$$((patch + 1)); \
	echo "$$major.$$minor.$$new_patch" > $(VERSION_FILE); \
	echo "New version: $$major.$$minor.$$new_patch"

bump-minor:
	@current=$$(cat $(VERSION_FILE)); \
	major=$$(echo $$current | cut -d. -f1); \
	minor=$$(echo $$current | cut -d. -f2); \
	new_minor=$$((minor + 1)); \
	echo "$$major.$$new_minor.0" > $(VERSION_FILE); \
	echo "New version: $$major.$$new_minor.0"


.PHONY: serve-docs
serve-docs:
	cd docs && uv run mkdocs serve


.PHONY: build-docs
build-docs:
	cd docs && uv run mkdocs build


.PHONY: flush
flush:
	uv run python src/manage.py flush


.PHONY: count-lines
count-lines:
	find src -name '*.py' -print0 | xargs -0 cat | grep -vE '^\s*($|#)' | wc -l


.PHONY: tree
tree:
	tree . -I "__pycache__" -I "*.png" -I "*.zip" -I "htmlcov"


.PHONY: dump-openapi
dump-openapi:
	uv run python src/manage.py dump_openapi


.PHONY: dump-issues
dump-issues:
	gh issue list --state open --limit 1000 --json number,title,labels,body,url --jq '.[] | "## \(.title) (#\(.number))\n\n- URL: \(.url)\n- Labels: \(.labels | map(.name) | join(", "))\n\n\(.body)\n\n---\n"' > issues.md


.PHONY: release
release:
	@VERSION=$$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/'); \
	echo "Current version: $$VERSION"; \
	read -p "Do you want to create a release v$$VERSION? (y/n): " confirm && if [ "$$confirm" = "y" ]; then \
		gh release create "v$$VERSION" --generate-notes; \
	else \
		echo "Release aborted."; \
	fi
