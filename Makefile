.PHONY: setup
setup:
	uv sync --group dev; \
	cp .env.example .env; \
	docker compose down -v; \
	docker compose up -d; \
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
	@uv run pytest -n auto --cov=src --cov-report=term --cov-report=html --cov-branch -v src/ 2>&1 | tee .tests.output.full; \
	exit_code=$${PIPESTATUS[0]}; \
	if [ $$exit_code -eq 0 ]; then uv run coverage html --skip-covered; rm -f .tests.output.full .tests.output; \
	else sed -n '/^=* FAILURES =*$$/,$$p' .tests.output.full > .tests.output; rm -f .tests.output.full; \
	echo "\nTest failures saved to .tests.output"; fi; \
	exit $$exit_code

.PHONY: test-linear
test-linear:
	uv run pytest --cov=src --cov-report=term --cov-report=html --cov-branch -v src/ && uv run coverage html --skip-covered

.PHONY: test-integration
test-integration:
	uv run pytest -m integration -v src/

.PHONY: test-failed
test-failed:
	uv run pytest --cov=src --cov-report=term --cov-report=html --cov-branch -v --last-failed src/ && uv run coverage html --skip-covered


.PHONY: bandit
bandit:
	uv run bandit -c pyproject.toml -r src/ -ll -ii

.PHONY: licensecheck
licensecheck:
	# `-r pyproject.toml` is mandatory: without it, licensecheck inspects
	# stdin when stdin isn't a tty (i.e. always under `make` / CI). With an
	# empty stdin it silently falls back to a partial resolution that only
	# walks [project] — missing [dependency-groups] plus any package whose
	# license metadata it can't resolve locally. The explicit -r forces the
	# uv resolver to walk the full graph including --group dev.
	uv run licensecheck -r pyproject.toml

# pip-audit operates on a resolved requirements file rather than the project
# directly: our own package is installed editable (has [build-system]) which
# pip-audit --strict chokes on. `uv export --no-emit-project` writes the resolved
# graph without the self-package, then --no-deps stops pip-audit from re-resolving.
#
# --disable-pip is required in CI: `pip-audit -r <file>` always spawns a venv
# and calls `python -m ensurepip` inside it. uv's managed Python doesn't ship
# `ensurepip`, so CI fails with exit 127. --disable-pip skips the venv bootstrap
# (safe because --no-deps means pip-audit doesn't need pip to resolve anything).
#
# --ignore-vuln rationale (re-review quarterly):
#   CVE-2025-69872 (diskcache): no fix published. Transitive via `instructor`
#       (LLM client lib). Attack requires write access to the local cache
#       directory; if an attacker already has that level of filesystem access
#       on our backend host, they have much larger problems. Revisit when a
#       diskcache release ships a fix.
#   CVE-2026-3219 (pip): no fix published as of pip 26.0.1. pip handles
#       polyglot tar+ZIP archives as ZIP regardless of filename, which could
#       install "incorrect" files. Not exploitable here: production runs
#       `uv sync` against `uv.lock` with hash-verified PyPI artefacts, and
#       pip is only present in the locked graph as a transitive dep of
#       `pip-audit` itself (via `pip-api`) — self-referential audit failure.
#       We never run `pip install` against attacker-controlled archives.
#       Revisit when a fixed pip release ships.
.PHONY: audit
audit:
	@uv export --quiet --locked --format requirements-txt --no-emit-project --no-hashes --group dev -o .audit-reqs.txt
	@trap 'rm -f .audit-reqs.txt' EXIT; uv run pip-audit --strict --no-deps --disable-pip -r .audit-reqs.txt \
		--ignore-vuln CVE-2025-69872 \
		--ignore-vuln CVE-2026-3219

.PHONY: deps-check
deps-check: licensecheck audit

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
	uv run python src/manage.py graph_models accounts events questionnaires notifications wallet geo telegram api common -a -g -o database.png

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
		docker compose down; \
		docker volume rm revel-backend_postgres_data; \
		docker volume rm revel-backend_minio_data; \
		docker compose up -d; \
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
		docker compose down; \
		docker compose up -d; \
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


.PHONY: serve-docs
serve-docs:
	uv run mkdocs serve -a localhost:8800


.PHONY: build-docs
build-docs:
	uv run mkdocs build


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
