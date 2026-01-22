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

## Prerequisites

1. **Backend running**: Django dev server or production-like environment
2. **Celery running**: For async email tasks
3. **Mailpit running**: For email verification tests (http://localhost:8025)
4. **Test data seeded**: Run the bootstrap command first

## Production-Like Environment (Recommended)

For realistic performance testing, use the included Docker Compose setup which mirrors production:

```bash
cd tests/performance

# Start the production-like environment
docker compose -f docker-compose-test.yaml --env-file .env.test up -d

# Wait for services to be healthy (especially ClamAV takes ~2 min)
docker compose -f docker-compose-test.yaml ps

# Run migrations and seed data
docker exec revel_perf_web python manage.py migrate
docker exec revel_perf_web python manage.py bootstrap
docker exec revel_perf_web python manage.py bootstrap_perf_tests

# Run Locust against the containerized backend
locust -f locustfile.py --host=http://localhost:8000/api

# Stop when done
docker compose -f docker-compose-test.yaml down
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

## Quick Start

### 1. Install Locust

```bash
uv add --dev locust
```

### 2. Seed Test Data

```bash
# From project root
python src/manage.py bootstrap_perf_tests

# To reset and reseed
python src/manage.py bootstrap_perf_tests --reset
```

### 3. Run Tests

```bash
# With Web UI
cd tests/performance
locust -f locustfile.py --host=http://localhost:8000/api

# Open http://localhost:8089 in browser

# Headless mode
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
├── locustfile.py               # Main entry point
├── config.py                   # Environment configuration
├── .env.example                # Locust environment variables
├── .env.test                   # Docker Compose environment
├── docker-compose-test.yaml    # Production-like test environment
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
docker compose -f docker-compose-test.yaml down
docker compose -f docker-compose-test.yaml --env-file .env.test up -d
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
docker compose -f docker-compose-test.yaml --env-file .env.test up -d
```
