# Performance Tests Makefile
# Usage: make <target>

COMPOSE_FILE := docker-compose-test.yaml
ENV_FILE := .env.test
HOST := http://localhost:8000/api
RESULTS_DIR := results

.PHONY: help setup shutdown build seed test test-500 clean-results

help:
	@echo "Performance Test Commands:"
	@echo "  make setup      - Start the production-like test environment"
	@echo "  make shutdown   - Stop and remove all containers"
	@echo "  make build      - Build/rebuild Docker images"
	@echo "  make seed       - Run migrations and seed test data"
	@echo "  make test       - Run load test with 100 users"
	@echo "  make test-500   - Run heavy load test with 500 users"
	@echo ""
	@echo "Full workflow:"
	@echo "  make build setup seed test"

# Start the test environment
setup:
	docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) up -d
	@echo ""
	@echo "Waiting for services to be healthy..."
	@echo "Check status with: docker compose -f $(COMPOSE_FILE) ps"
	@echo "ClamAV may take ~2 minutes to initialize."

# Stop the test environment
shutdown:
	docker compose -f $(COMPOSE_FILE) down

# Build/rebuild Docker images
build:
	docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) build

# Run migrations and seed test data
seed:
	docker exec revel_perf_web python manage.py migrate
	docker exec revel_perf_web python manage.py bootstrap
	docker exec revel_perf_web python manage.py bootstrap_perf_tests --reset

# Clean results directory
clean-results:
	rm -rf $(RESULTS_DIR)/*
	@mkdir -p $(RESULTS_DIR)

# Run load test with 100 users (10 users/sec spawn rate, 5 minutes)
test: clean-results
	locust -f locustfile.py --host=$(HOST) \
		--headless \
		-u 100 \
		-r 10 \
		-t 5m \
		--csv=$(RESULTS_DIR)/test \
		--html=$(RESULTS_DIR)/test.html

# Run heavy load test with 500 users (50 users/sec spawn rate, 5 minutes)
test-500: clean-results
	locust -f locustfile.py --host=$(HOST) \
		--headless \
		-u 500 \
		-r 50 \
		-t 5m \
		--csv=$(RESULTS_DIR)/heavy_checkout \
		--html=$(RESULTS_DIR)/heavy_checkout.html
