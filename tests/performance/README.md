# Performance Tests

Locust-based performance tests for the Revel backend API.

## Overview

These tests simulate realistic user behavior to identify performance bottlenecks
in the API. Tests target the following scenarios:

| Scenario | Weight | Description |
|----------|--------|-------------|
| EventBrowser | 30 | Anonymous event discovery |
| ExistingUserLogin | 20 | User authentication |
| DashboardUser | 15 | Dashboard aggregation queries |
| RSVPUser | 15 | RSVP flow (bottleneck) |
| FreeTicketUser | 10 | Free ticket checkout (bottleneck) |
| PWYCTicketUser | 5 | PWYC checkout (bottleneck) |
| QuestionnaireUser | 5 | Questionnaire submission (bottleneck) |
| NewUserRegistration | 5 | Registration with email verification |

## Quick Start

```bash
cd tests/performance

# Build images, start environment, seed data, run tests
make build setup
# Wait for services to be healthy (~2 min for ClamAV)
make seed
make test

# Stop when done
make shutdown
```

## Makefile Commands

| Command | Description |
|---------|-------------|
| `make help` | Show available commands |
| `make setup` | Start the Docker test environment |
| `make shutdown` | Stop and remove all containers |
| `make build` | Build/rebuild Docker images |
| `make seed` | Run migrations and seed test data (with --reset) |
| `make test` | Run load test: 100 users, 10/sec spawn, 5 min |
| `make test-500` | Run heavy load test: 500 users, 50/sec spawn, 5 min |

Results are saved to `results/` directory (CSV + HTML reports).

## Prerequisites

1. **Docker**: For running the production-like environment
2. **Locust**: Install with `uv add --dev locust`

## Production-Like Environment

The Docker Compose setup mirrors production for realistic performance testing:

```bash
cd tests/performance

# Start the production-like environment
make setup

# Wait for services to be healthy (especially ClamAV takes ~2 min)
docker compose -f docker-compose-test.yaml ps

# Run migrations and seed data
make seed

# Run Locust against the containerized backend
locust -f locustfile.py --host=http://localhost:8000/api

# Stop when done
make shutdown
```

### Services Included

| Service | Port | Description |
|---------|------|-------------|
| web | 8000 | Gunicorn (4 workers, 2 threads) |
| celery | - | Celery worker (4 concurrency) |
| beat | - | Celery beat scheduler |
| postgres | - | PostGIS 17 (internal) |
| pgbouncer | - | Connection pooler (internal) |
| redis | - | Cache & broker (internal) |
| mailpit | 8025, 1025 | Email testing UI & SMTP |
| clamav | - | Malware scanning (internal) |

### Key Differences from Production

- `DEBUG=True` (no HTTPS required)
- `SILK_PROFILER=False` (disabled for accurate timing)
- No observability stack (Grafana, Loki, Tempo)
- No Caddy reverse proxy
- Mailpit instead of real SMTP

## Manual Setup (Alternative)

### 1. Install Locust

```bash
uv add --dev locust
```

### 2. Seed Test Data

```bash
# Using Makefile (recommended)
make seed

# Or manually from project root
python src/manage.py bootstrap_perf_tests --reset
```

### 3. Run Tests

```bash
# Using Makefile (recommended)
make test      # 100 users
make test-500  # 500 users

# Or with Web UI
locust -f locustfile.py --host=http://localhost:8000/api
# Open http://localhost:8089 in browser

# Or headless mode
locust -f locustfile.py --host=http://localhost:8000/api \
    --headless -u 100 -r 10 --run-time 5m
```

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LOCUST_BACKEND_URL` | `http://localhost:8000/api` | Backend API URL |
| `LOCUST_MAILPIT_URL` | `http://localhost:8025` | Mailpit URL |
| `LOCUST_DEFAULT_PASSWORD` | `password123` | Test user password |
| `LOCUST_NUM_PRESEEDED_USERS` | `100` | Number of pre-seeded users |
| `LOCUST_EMAIL_POLL_TIMEOUT` | `10` | Email poll timeout (seconds) |
| `LOCUST_EMAIL_POLL_INTERVAL` | `0.5` | Email poll interval (seconds) |
| `LOCUST_REQUEST_TIMEOUT` | `30` | HTTP request timeout (seconds) |

### System Testing Mode

When `SYSTEM_TESTING=True` (enabled by default in `.env.test`), verification tokens
are returned in response headers instead of requiring email polling:

| Header | Endpoint |
|--------|----------|
| `X-Test-Verification-Token` | `/account/register` |
| `X-Test-Password-Reset-Token` | `/account/forgot-password` |
| `X-Test-Deletion-Token` | `/account/request-deletion` |

This eliminates the unreliable Mailpit polling and speeds up registration tests.

## Test Data

The `bootstrap_perf_tests` command creates:

### Organization
- **Slug**: `perf-test-org`
- **Admin**: `perf-admin@test.com` / `password123`
- **Staff**: `perf-staff@test.com` / `password123`

### Pre-seeded Users
- **Count**: 100 (configurable)
- **Pattern**: `perf-user-{0..99}@test.com`
- **Password**: `password123`
- **Status**: All verified (no email verification needed)

### Test Events
| Slug | Type | Purpose |
|------|------|---------|
| `perf-rsvp-event` | RSVP, unlimited | RSVP flow testing |
| `perf-rsvp-limited-event` | RSVP, limited capacity | Capacity stress testing |
| `perf-ticket-free-event` | Free tickets | Free checkout testing |
| `perf-ticket-pwyc-event` | PWYC tickets | PWYC checkout testing |
| `perf-questionnaire-event` | With questionnaire | Questionnaire submission testing |

## Running Specific Scenarios

```bash
# Single scenario
locust -f locustfile.py --host=http://localhost:8000/api \
    --class RSVPUser

# Multiple scenarios
locust -f locustfile.py --host=http://localhost:8000/api \
    --class RSVPUser --class FreeTicketUser

# All bottleneck endpoints
locust -f locustfile.py --host=http://localhost:8000/api \
    --class RSVPUser --class FreeTicketUser --class PWYCTicketUser \
    --class QuestionnaireUser
```

## Monitoring Results

### Web UI (Recommended)
Access http://localhost:8089 to see:
- Real-time request statistics
- Response time distributions
- Failure rates
- Charts and graphs

### Headless Output
```bash
locust -f locustfile.py --host=http://localhost:8000/api \
    --headless -u 100 -r 10 --run-time 5m \
    --csv=results/perf_test
```

This creates:
- `results/perf_test_stats.csv` - Request statistics
- `results/perf_test_failures.csv` - Failure details
- `results/perf_test_stats_history.csv` - Time-series data

## Bottleneck Endpoints

The following endpoints are marked as bottlenecks and receive focused testing:

1. **`/events/{id}/my-status`** - Complex eligibility calculation
2. **`/events/{id}/rsvp/{status}`** - RSVP with capacity checks
3. **`/events/{id}/tickets/{tier}/checkout`** - Ticket purchase
4. **`/events/{id}/tickets/{tier}/checkout/pwyc`** - PWYC checkout
5. **`/events/{id}/questionnaire/{qid}/submit`** - Questionnaire submission

## File Structure

```
tests/performance/
├── __init__.py
├── README.md                   # This file
├── Makefile                    # Build/test automation
├── locustfile.py               # Main entry point
├── config.py                   # Environment configuration
├── .env.test                   # All environment variables (Django + Locust)
├── docker-compose-test.yaml    # Production-like test environment
├── results/                    # Test output (CSV, HTML reports)
├── clients/
│   ├── __init__.py
│   ├── api_client.py           # HTTP client with auth
│   └── mailpit_client.py       # Email verification
├── scenarios/
│   ├── __init__.py
│   ├── base.py                 # Base user classes
│   ├── auth_scenarios.py       # Login/registration
│   ├── discovery_scenarios.py
│   ├── dashboard_scenarios.py
│   ├── rsvp_scenarios.py
│   ├── ticket_scenarios.py
│   └── questionnaire_scenarios.py
└── data/
    ├── __init__.py
    └── generators.py           # Test data generation
```

## Troubleshooting

### Tests fail with 401 Unauthorized
- Ensure test data is seeded: `python src/manage.py bootstrap_perf_tests`
- Check that users are verified in the database

### Email verification tests timeout
- Ensure Mailpit is running at configured URL
- Check Celery worker is processing email tasks
- Increase `LOCUST_EMAIL_POLL_TIMEOUT` if needed

### No events found
- Verify bootstrap command completed successfully
- Check organization and events exist in database

### Connection refused
- Ensure backend is running at configured URL
- Check firewall/network settings

### Docker Compose Issues

**Services not starting:**
```bash
# Check service logs
docker compose -f docker-compose-test.yaml logs web
docker compose -f docker-compose-test.yaml logs celery

# Restart all services
make shutdown
make setup
```

**ClamAV takes too long to start:**
- ClamAV downloads virus definitions on first start (~2 min)
- Check health: `docker exec revel_perf_clamav clamdscan --version`

**Database connection issues:**
- Ensure postgres is healthy before pgbouncer starts
- Check: `docker exec revel_perf_pgbouncer pg_isready -h localhost -p 6432`

**Clean reset:**
```bash
# Remove all volumes and start fresh
docker compose -f docker-compose-test.yaml down -v
make setup
make seed
```