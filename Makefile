.PHONY: setup
setup:
	uv sync --group dev; \
	cp .env.example .env; \
	docker compose down -v; \
	docker compose up -d; \
	sleep 3; \
	$(MAKE) bootstrap; \
	$(MAKE) FEATURE_OBSERVABILITY=False run

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
# .mo binaries are not committed (ADR-0011); compile them so the i18n tests,
# which assert translated strings, resolve against the catalogs.
test: compilemessages
	@uv run pytest -n auto --cov=src --cov-report=term --cov-report=html --cov-branch -v src/ 2>&1 | tee .tests.output.full; \
	exit_code=$${PIPESTATUS[0]}; \
	if [ $$exit_code -eq 0 ]; then uv run coverage html --skip-covered; rm -f .tests.output.full .tests.output; \
	else sed -n '/^=* FAILURES =*$$/,$$p' .tests.output.full > .tests.output; rm -f .tests.output.full; \
	echo "\nTest failures saved to .tests.output"; fi; \
	exit $$exit_code

.PHONY: test-linear
test-linear: compilemessages
	uv run pytest --cov=src --cov-report=term --cov-report=html --cov-branch -v src/ && uv run coverage html --skip-covered

.PHONY: test-integration
test-integration: compilemessages
	uv run pytest -m integration -v src/

.PHONY: test-failed
test-failed: compilemessages
	uv run pytest --cov=src --cov-report=term --cov-report=html --cov-branch -v --last-failed src/ && uv run coverage html --skip-covered

# Refresh .test_durations (used by pytest-split to time-balance CI shards).
# CI refreshes it weekly (refresh-test-durations.yaml); use this for a manual
# refresh. Staleness degrades gracefully — tests missing from the file are
# distributed by count.
.PHONY: store-durations
store-durations: compilemessages
	uv run pytest -n auto --store-durations --clean-durations src/


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
# No --ignore-vuln entries currently: CVE-2026-49452 (weasyprint) is fixed in
# 69.0, and CVE-2025-69872 (diskcache) dropped out of the graph when instructor
# stopped pulling diskcache. Re-add a documented ignore only if a vuln with no
# viable fix reappears.
.PHONY: audit
audit:
	@uv export --quiet --locked --format requirements-txt --no-emit-project --no-hashes --group dev -o .audit-reqs.txt
	@trap 'rm -f .audit-reqs.txt' EXIT; uv run pip-audit --strict --no-deps --disable-pip -r .audit-reqs.txt

.PHONY: deps-check
deps-check: licensecheck audit

# Combined command: Runs format, lint, mypy, migration-check, i18n-check, file-length, and task-names in sequence
.PHONY: check
check: format lint mypy migration-check i18n-check file-length task-names

.PHONY: file-length
file-length:
	@./scripts/check-file-length.sh 1000

.PHONY: task-names
task-names:
	@uv run python scripts/check_task_names.py src

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
	uv run python src/manage.py bootstrap_test_events

.PHONY: run
run:
	uv run python src/manage.py generate_test_jwts; \
	uv run python src/manage.py runserver

# Prod-like server for frontend E2E: gunicorn (gthread, mirrors infra's command) behind
# PgBouncer, so parallel Playwright workers don't exhaust Postgres connections. Brings the
# pooler up, then routes the server through it via DB_USE_PGBOUNCER/DB_PORT set inline
# (os.environ wins over .env), leaving `make test` and `make run` on direct 5432.
# Override concurrency with GUNICORN_WORKERS / GUNICORN_THREADS.
# OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES: on macOS, gunicorn forking workers after an Apple
# framework was initialized (WeasyPrint/Core Text, _scproxy) trips the ObjC fork-safety guard,
# which SIGKILLs the workers. The var disables that guard; it's a no-op on Linux/CI.
# Shared gunicorn invocation for run-e2e / run-e2e-daemon — see the comment on
# run-e2e for why gthread+PgBouncer and the ObjC fork-safety var.
E2E_GUNICORN = DB_USE_PGBOUNCER=True DB_PORT=6432 OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES uv run gunicorn revel.wsgi:application \
	--worker-class gthread \
	--workers $${GUNICORN_WORKERS:-4} \
	--threads $${GUNICORN_THREADS:-4} \
	--bind 127.0.0.1:8000 \
	--max-requests 4000 \
	--max-requests-jitter 400 \
	--timeout 60 \
	--graceful-timeout 30

.PHONY: run-e2e
run-e2e:
	docker compose -f compose.yaml -f docker-compose-e2e.yml up -d --wait pgbouncer && \
	uv run python src/manage.py generate_test_jwts && \
	cd src && $(E2E_GUNICORN)

# Canonical E2E reseed, in the one order that works (see revel-frontend
# tests/e2e/README.md): reset_events already re-runs bootstrap_events (a
# following `make bootstrap` would fail on duplicates), and seed must run
# BEFORE bootstrap-tests — the seeder's payment/quantity sweeps are global and
# would mutate the test fixtures (e.g. randomly refunding the sold-out event's
# tickets). Never reorder. Brings up postgres/pgbouncer first (cold-start
# safety) — e2e-setup in revel-frontend calls this before run-e2e-daemon.
.PHONY: e2e-seed
e2e-seed:
	docker compose -f compose.yaml -f docker-compose-e2e.yml up -d --wait pgbouncer
	uv run python src/manage.py reset_events --no-input
	$(MAKE) seed
	$(MAKE) bootstrap-tests

# Stop the daemonized e2e backend. Idempotent: pid-file kill first, then any
# stray listener on :8000 (stale gunicorn/runserver — the "stale backend
# serving 200s" trap), then wait for the port to free up. Kills only processes
# that look like our backend (gunicorn/manage.py/python) — a foreign process
# holding :8000 is reported and left alone, and a pid file pointing at a
# reused (non-gunicorn) pid is discarded without killing. The drain wait is
# 35s because gunicorn's --graceful-timeout is 30: TERM lets in-flight
# requests finish before workers exit.
.PHONY: stop-e2e
stop-e2e:
	@if [ -f .e2e-gunicorn.pid ]; then \
		pid=$$(cat .e2e-gunicorn.pid); \
		if ps -p $$pid -o command= 2>/dev/null | grep -q gunicorn; then \
			echo "Stopping e2e gunicorn (pid $$pid)"; \
			kill $$pid 2>/dev/null || true; \
		else \
			echo "Stale pid file (pid $$pid is gone or not gunicorn) — removing"; \
		fi; \
		rm -f .e2e-gunicorn.pid; \
	fi
	@for pid in $$(lsof -ti:8000 2>/dev/null); do \
		cmd=$$(ps -p $$pid -o command= 2>/dev/null); \
		if echo "$$cmd" | grep -Eq 'gunicorn|manage\.py|python'; then \
			echo "Killing stale backend on :8000 (pid $$pid)"; \
			kill $$pid 2>/dev/null || true; \
		else \
			echo "❌ :8000 is held by a non-backend process (pid $$pid): $$cmd"; \
			echo "   Not killing it — stop it yourself and re-run."; \
			exit 1; \
		fi; \
	done
	@i=0; while [ $$i -lt 35 ] && [ -n "$$(lsof -ti:8000 2>/dev/null)" ]; do \
		i=$$((i+1)); sleep 1; \
	done; \
	if [ -n "$$(lsof -ti:8000 2>/dev/null)" ]; then \
		echo "❌ :8000 still busy after 35s (gunicorn --graceful-timeout is 30)"; exit 1; \
	fi

# run-e2e, daemonized: for `make e2e-setup` in revel-frontend. Kills any stale
# backend first, writes .e2e-gunicorn.pid/.log at the repo root, and only
# returns 0 once GET /api/version actually answers.
.PHONY: run-e2e-daemon
run-e2e-daemon: stop-e2e
	docker compose -f compose.yaml -f docker-compose-e2e.yml up -d --wait pgbouncer
	uv run python src/manage.py generate_test_jwts
	cd src && $(E2E_GUNICORN) \
		--daemon \
		--pid $(CURDIR)/.e2e-gunicorn.pid \
		--error-logfile $(CURDIR)/.e2e-gunicorn.log
	@echo "Waiting for backend on http://localhost:8000/api/version ..."; \
	i=0; while [ $$i -lt 60 ]; do \
		if curl -fsS http://localhost:8000/api/version >/dev/null 2>&1; then \
			echo "✅ Backend up (pid $$(cat .e2e-gunicorn.pid), log: .e2e-gunicorn.log)"; \
			exit 0; \
		fi; \
		i=$$((i+1)); sleep 1; \
	done; \
	echo "❌ Backend did not answer within 60s — see .e2e-gunicorn.log"; \
	$(MAKE) stop-e2e; \
	exit 1

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
	cd src && uv run python manage.py makemessages -l de -l it -l fr -l es -l pt --no-location --no-obsolete
	# Strip the POT-Creation-Date header: gettext stamps it on every run, which is
	# the only remaining diff churn once --no-location is set. Removing it keeps
	# `make makemessages` deterministic (diffs only on real string changes).
	@find src/locale -name django.po -exec sed -i.bak '/^"POT-Creation-Date:/d' {} \; -exec rm -f {}.bak \;

.PHONY: compilemessages
compilemessages:
	cd src && uv run python manage.py compilemessages

.PHONY: i18n-check
i18n-check:
	@echo "Checking translation catalog (keys extracted + translated)..."
	@uv run python scripts/check_translations.py


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
